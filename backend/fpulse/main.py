"""
F-Pulse — AI-native, human-governed data pipeline builder.

Start: python -m fpulse.main

Stage 1 — Lifespan migration (2026-04-19)
─────────────────────────────────────────
Heavy work that used to run at module-import time has been moved into a
proper FastAPI ``lifespan`` async context manager. The module-level
``app_state`` dict is now an EMPTY pointer at import; every store, manager,
scheduler, and pool is instantiated INSIDE the lifespan startup phase, and
torn down INSIDE the shutdown phase in reverse order.

Why "Option A" (keep app_state as a module global)?
    26 router files import ``from fpulse.main import app_state``. Replacing
    the dict object would break those imports. We instead keep the dict
    identity stable and populate it via key assignment inside lifespan, so
    every existing router sees the populated state once lifespan startup
    completes — zero churn to the 29 routers.

Shutdown ordering (per reviewer 2):
    worker pool → scheduler → backup_scheduler → THEN database close.
    Closing the DB before workers drain risks mid-write-to-closed-DB
    crashes during scheduled rollouts and Docker stop signals.

Dev-seed (admin password reset):
    No longer runs on every import. Use the CLI:
        python -m fpulse seed-admin

This file no longer uses ``@app.on_event("startup"/"shutdown")`` —
those are deprecated in modern FastAPI/Starlette and don't compose with
lifespan-driven warmup tasks (Stage 2).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback as _tb
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from fpulse import feature_flags
from fpulse.feature_flags import FeatureDisabledError

from fpulse.storage.database import Database
from fpulse.ir.versioning import WorkflowStore
from fpulse.ir.lifecycle import LifecycleStore
from fpulse.projects.store import ProjectStore
from fpulse.folders.store import FolderStore
from fpulse.workspaces.store import WorkspaceStore
from fpulse.scheduling.store import ScheduleStore
from fpulse.alerts.store import AlertStore
from fpulse.monitoring.store import ExecutionStore
from fpulse.auth.store import UserStore
from fpulse.variables.store import VariableStore
from fpulse.credentials.store import CredentialStore
from fpulse.connections.store import ConnectionStore
from fpulse.datastore.store import DataStore
from fpulse.ai_config.store import AIConfigStore
from fpulse.intelligence.schema_contract import SchemaContractStore
from fpulse.intelligence.schema_history import SchemaHistoryStore
from fpulse.engine.execution_log import ExecutionLogger
from fpulse.engine.step_output_store import StepOutputStore
# 2026-05-26 — typed event bus + built-in Prometheus consumer.
# Wired here at the lifespan boundary so every store/router shares
# one bus instance via app_state["event_bus"].
from fpulse.events import get_event_bus
from fpulse.events.consumers import AuditConsumer, MetricsConsumer
from fpulse.nodes.registry import get_registry
from fpulse.scheduling.scheduler import PipelineScheduler
from fpulse.alerts.notifier import NotificationService
from fpulse.notifications.store import NotificationStore
from fpulse.notifications.service import ApprovalNotifier
from fpulse.engine.worker_pool import WorkerPool
from fpulse.engine.execution_manager import ExecutionManager
from fpulse.lineage import LineageStore
from fpulse.marketplace import MarketplaceStore
from fpulse.collaboration import CollaborationStore
from fpulse.gateway import GatewayStore
from fpulse.plugins import PluginManager
from fpulse.api import (
    workflows_router,
    execution_router,
    backfills_router,
    planner_router,
    projects_router,
    folders_router,
    workspaces_router,
    schedules_router,
    alerts_router,
    monitor_router,
    dashboard_router,
    auth_router,
    variables_router,
    credentials_router,
    intelligence_router,
    contracts_router,
    schema_history_router,
    connections_router,
    extraction_router,
    auth_health_router,
    system_router,
    pipeline_health_router,
    pipeline_health_per_router,
    types_meta_router,
    expressions_router,
    steward_router,
    backup_router,
    ws_router,
    ws_info_router,
    logs_router,
    ai_router,
    ai_config_router,
    templates_router,
    exports_router,
    notifications_router,
    pool_router,
    lineage_router,
    marketplace_router,
    collaboration_router,
    gateway_router,
    plugins_router,
    uploads_router,
    storage_router,
    health_memory_router,
    execution_manager_router,
    reports_router,
    pool_allocation_router,
    workspace_settings_router,
    ai_cost_rates_router,
    deployments_router,
    recipes_router,
    agent_router,
    ollama_router,
    pre_publish_router,
    catalog_router,
    mcp_router,
    activity_router,
    cert_matrix_router,
    sync_state_router,
    trust_router,
    product_knowledge_router,
    connector_authoring_router,
    connector_drafts_router,
    app_meta_router,
)


logger = logging.getLogger("fpulse")


# ── Worker-role guard (anti-footgun) ────────────────────────────────────
# FPULSE_ROLE=worker is scaffolding for the future multi-worker deployment
# (Stage 5, F-Pulse+). Running this binary in worker mode against the same
# SQLite database as the API will produce "database is locked" errors and
# corrupted state. Refuse to start unless the operator explicitly
# acknowledges the placeholder.
_role = os.environ.get("FPULSE_ROLE", "api").lower()
if _role == "worker" and os.environ.get("FPULSE_WORKER_PLACEHOLDER_ACK") != "1":
    sys.stderr.write(
        "\n"
        "FPULSE_ROLE=worker is not supported in this build. Running this image\n"
        "as a worker would share a SQLite database with the API instance and\n"
        "produce 'database is locked' errors plus corrupted state.\n"
        "\n"
        "  - For single-node deployment: unset FPULSE_ROLE (or set to 'api').\n"
        "  - For multi-worker testing: set FPULSE_WORKER_PLACEHOLDER_ACK=1\n"
        "    (only safe with an isolated database — not the API's SQLite).\n"
    )
    sys.exit(78)  # EX_CONFIG


# ── Module-level app_state pointer (Option A) ────────────────────────────
#
# This dict is INTENTIONALLY EMPTY at import time. The 26 router modules
# do `from fpulse.main import app_state` to capture a reference; lifespan
# startup then populates this same dict by key assignment, so routers see
# the populated state once startup completes. NEVER REASSIGN this dict —
# routers hold a pointer to the original object.
#
# Stage 0 health-memory endpoint reads this dict to report ``loaded_stores``,
# which is how we'll observe Stage 2 feature-flag gating actually shrinking
# the loaded surface (fewer keys when a feature is disabled).
app_state: dict = {}


# ── Resolve data directory once, deterministically ───────────────────────
def _resolve_data_dir() -> str:
    """Resolution priority (highest first):

      1. UI-saved override (~/.fpulse/storage_settings.json). Z27 lets
         users relocate storage from the Settings page; the override is
         stored OUTSIDE the data tree so it survives moving the tree.
      2. ``FPULSE_DATA_DIR`` env var — the long-standing operator knob.
      3. ``<cwd>/data`` default.

    Why the override wins over the env var: the user explicitly clicked
    Save in the UI. If they ALSO set the env var to something different,
    the most-recent intent (the saved file) wins. To go back to the env
    var, the user clicks "Discard pending change" in Settings, which
    deletes the override file — this is an idempotent operator action.
    """
    override_dir: str | None = None
    try:
        from fpulse.storage.storage_settings import load_override  # noqa: WPS433
        rec = load_override()
        if rec:
            cand = (rec.get("data_dir") or "").strip()
            if cand:
                override_dir = cand
    except Exception:
        # storage_settings import failure must never block boot —
        # fall through to env / default.
        override_dir = None

    data_dir = override_dir or os.environ.get(
        "FPULSE_DATA_DIR",
        os.path.join(os.getcwd(), "data"),
    )
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


# ── Demo-data seeding ─────────────────────────────────────────────────────
def _seed_demo_data(data_dir: str) -> None:
    """Copy bundled demo CSVs into ``{data_dir}/samples/`` on first run.

    Idempotent: each target file is copied only when missing. An operator
    who edits or deletes a sample on their host won't have it silently
    overwritten on the next restart.

    Powers the "First pipeline (CSV → CSV)" template in the OSS Templates
    gallery so a fresh install has a runnable demo with zero external
    dependencies — no Postgres, no API, no S3.
    """
    import shutil
    from fpulse.seed_data import SEED_DATA_DIR

    target_dir = os.path.join(data_dir, "samples")
    os.makedirs(target_dir, exist_ok=True)

    seeded: list[str] = []
    for src in SEED_DATA_DIR.glob("*.csv"):
        dst = os.path.join(target_dir, src.name)
        if os.path.exists(dst):
            continue
        shutil.copyfile(src, dst)
        seeded.append(src.name)

    if seeded:
        logger.info("Seeded demo data: %s -> %s", ", ".join(seeded), target_dir)


# ── Bootstrap-password security check ─────────────────────────────────────
def _warn_if_bootstrap_password_lingers(data_dir: str) -> None:
    """Emit a security warning if the bootstrap admin password file still
    exists on disk.

    The bootstrap flow writes a one-time admin password to
    ``{data_dir}/INITIAL_ADMIN_PASSWORD.txt`` so a fresh operator can find
    it on first boot. The expected lifecycle is: sign in → rotate → delete
    the file. ``api/auth.py:change_my_password`` deletes it automatically
    when the bootstrap admin (admin@fpulse.local) successfully rotates
    their password — but if the operator changed passwords through a
    different path, used a different admin email, or simply forgot, the
    file lingers indefinitely on disk in plaintext.

    A lingering file means anyone with read access to the data dir can
    still discover the original admin password — even if it's been
    rotated since (the file is never re-read by the server). Calling this
    out loudly at every startup is the cheapest way to nudge operators
    toward fixing it.
    """
    password_file = os.path.join(data_dir, "INITIAL_ADMIN_PASSWORD.txt")
    if not os.path.exists(password_file):
        return
    logger.warning(
        "SECURITY: bootstrap admin password file still present at %s. "
        "If you have already signed in and rotated the password, delete "
        "this file — its contents are no longer used by F-Pulse but remain "
        "readable to anyone with access to the data directory. If you "
        "haven't rotated yet, sign in with the credentials inside, change "
        "the password from the Account page, then delete the file.",
        password_file,
    )


def _build_encryptor():
    """Construct the always-on Fernet encryptor for credentials + AI
    provider API keys. May 4 2026: this replaces the previous Plus-gated
    path where OSS Free stored secrets in plaintext.

    Reads or generates `~/.fpulse/secret.key` (or
    `$FPULSE_DATA_DIR/secret.key`). Refuses to start on world-readable
    POSIX permissions — fail-closed. Disable with
    `FPULSE_DISABLE_ENCRYPTION=1` for testing only (logs a warning;
    NEVER use in production)."""
    if os.environ.get("FPULSE_DISABLE_ENCRYPTION", "").strip().lower() in ("1", "true", "yes"):
        logger.warning(
            "FPULSE_DISABLE_ENCRYPTION=1 — credentials and API keys WILL "
            "be stored in plaintext. Test-only mode; do not run in production."
        )
        return None
    from fpulse.security import Encryptor
    return Encryptor.from_master_key()


def _populate_state(data_dir: str) -> None:
    """Instantiate every store and manager INTO the module-global app_state.

    Mutates ``app_state`` in-place — never reassigns. Called from lifespan
    startup so the cost is paid once, after the FastAPI app is ready to
    accept requests but before they hit the routers.

    Stage 2: optional enterprise stores (marketplace, lineage, collaboration,
    plugins) are gated by FPULSE_ENABLE_* env vars via feature_flags.

    Stage 3b: the database layer is now built via build_database(), which
    returns the SQLite handle (always) plus an optional PostgresDatabase
    handle (when FPULSE_DB_URL is set and the `pg` extra is installed).
    No store has been migrated to PG yet — every store still uses the
    SQLite handle. The PG handle sits in app_state['pg'] waiting for
    the first store migration to use it.
    """
    from fpulse.storage.database_factory import build_database
    db, _pg_handle_unused = build_database(data_dir)

    # ── Always-on core ────────────────────────────────────────────────
    app_state.update({
        "db": db,
        "store": WorkflowStore(db=db),
        "project_store": ProjectStore(db=db),
        "folder_store": FolderStore(db=db),
        "workspace_store": WorkspaceStore(db=db),
        "schedule_store": ScheduleStore(db=db),
        "alert_store": AlertStore(db=db),
        "execution_store": ExecutionStore(db=db),
        "user_store": UserStore(db=db),
        "variable_store": VariableStore(db=db),
        "credential_store": CredentialStore(db=db),
        "connection_store": ConnectionStore(db=db),
        # 2026-05-23 (Y1): workspace datastore — indexes uploaded files,
        # pipeline outputs, and managed Parquet tables. Backs the
        # Storage page and the local_table_source / local_table_sink
        # IR nodes. Bytes live under FPULSE_DATA_DIR; this store is
        # the metadata index.
        "datastore": DataStore(db=db),
        "lifecycle_store": LifecycleStore(db=db),
        "contract_store": SchemaContractStore(db=db),
        # 2026-05-27 — append-only audit log of managed-table schema
        # evolutions, written by sinks AFTER schema_policy applies a
        # change. See fpulse/intelligence/schema_history.py.
        "schema_history_store": SchemaHistoryStore(db=db),
        "execution_log_store": ExecutionLogger(db=db),
        "step_output_store": StepOutputStore(db=db),
        "scheduler": PipelineScheduler(check_interval_seconds=30),
        "notifier": NotificationService(),
        "notification_store": NotificationStore(db=db),
        "worker_pool": WorkerPool(),  # auto-detects worker count
        "gateway_store": GatewayStore(db=db),
        # Encryptor — Fernet-backed, always-on (Free + Plus). May 4 2026:
        # replaces the previous Plus-gated path where OSS Free stored
        # credentials in plaintext on disk. Master key file lives at
        # ~/.fpulse/secret.key (or $FPULSE_DATA_DIR/secret.key). Created
        # on first run; chmod 600; F-Pulse refuses to start if world-readable.
        "encryptor": _build_encryptor(),
        "data_dir": data_dir,
        # Warmup state machine: pending → ok | failed
        # Set to "pending" right before asyncio.create_task(_warmup()) below.
        "warmup_status": "not_applicable",
    })

    # AI config store with the always-on encryptor — keeps API keys
    # for Anthropic/OpenAI/etc. encrypted at rest in OSS Free, not just
    # Plus. Constructed AFTER app_state is built so we can pass the
    # encryptor from the same dict.
    app_state["ai_config_store"] = AIConfigStore(
        db=db, encryptor=app_state["encryptor"],
    )

    # v32 (2026-06-17): let an AI provider import its key from the central
    # credential store (Insights → Credentials) instead of holding a second
    # inline copy. The resolver decrypts the referenced credential's secret
    # at request time, so the key has exactly one governed home (expiry,
    # vault source, audit, "used by"). Injected rather than imported so the
    # AI config store stays decoupled from the credential store.
    def _resolve_ai_credential_key(credential_id: str, workspace_id):
        try:
            cs = app_state.get("credential_store")
            if cs is None or not credential_id:
                return None
            cred = cs.get_raw(credential_id, workspace_id=workspace_id)
            if cred is None:
                return None
            cfg = getattr(cred, "config", None) or {}
            # Tolerant of the field name the operator used on the credential.
            for k in ("api_key", "key", "token", "secret", "password"):
                v = cfg.get(k)
                if v:
                    return v
            return None
        except Exception:
            return None

    app_state["ai_config_store"].set_credential_resolver(_resolve_ai_credential_key)

    # ExecutionManager wraps WorkerPool as its pipeline implementation.
    app_state["execution_manager"] = ExecutionManager.initialize(
        worker_pool=app_state["worker_pool"],
    )

    # ── Agent trace store (Step 1.5b-3) ────────────────────────────────
    # Persists every AgentRunner.run() result to SQLite for replay + audit.
    # Best-effort writes — agent runs never break if the store fails. Schema
    # is created idempotently on first construction.
    from fpulse.ai.trace_store import TraceStore
    app_state["trace_store"] = TraceStore(db=db)

    # ── Pipeline checkpoint store (Sprint 1 / Gate 1) ──────────────────
    # Records per-(run_id, step_id) outcomes so the executor's
    # "Resume from step X" feature can pick up from the failure boundary.
    # Wired to the module-level singleton so the executor doesn't have to
    # know about app_state — consistent with how StepCache is wired.
    from fpulse.engine.checkpoint_store import checkpoint_store
    checkpoint_store.set_db(db)
    app_state["checkpoint_store"] = checkpoint_store

    # ── Backfill store (schema v29, 2026-05-27) ────────────────────────
    # Chunked re-execution of a pipeline over a historical date range.
    # Same module-singleton pattern as checkpoint_store so the API and
    # orchestrator can grab it without threading the dict through.
    from fpulse.backfills.store import get_backfill_store
    _bf_store = get_backfill_store()
    _bf_store.set_db(db)
    app_state["backfill_store"] = _bf_store

    # ── Sync state store (schema v31, 2026-05-30) ─────────────────────
    # Per-(workflow, source-step) cursor watermark for incremental
    # ingestion. Same singleton + set_db pattern as checkpoint_store.
    # The db_source node (and other incremental sources, follow-up)
    # reads from this at the start of each run when sync_mode=
    # "incremental" and writes back MAX(cursor_column) at the end.
    # See fpulse/engine/sync_state_store.py for the full contract.
    from fpulse.engine.sync_state_store import sync_state_store
    sync_state_store.set_db(db)
    app_state["sync_state_store"] = sync_state_store

    # ── Sink idempotency dedupe store (schema v30, 2026-05-27) ─────────
    # Per-(pipeline, sink_step, key_hash) marker that an external sink
    # (email/webhook/api/kafka/slack) has already fired its side effect.
    # When users set `idempotency_key` on a sink, the helper in
    # fpulse/sinks/idempotency_helper.py consults this store to skip
    # already-sent rows on re-run / retry / acknowledged backfill.
    # Same module-singleton pattern as checkpoint_store so the sinks
    # can grab it via get_dedupe_store() without threading app_state.
    from fpulse.sinks.dedupe_store import get_dedupe_store
    _dedupe = get_dedupe_store()
    _dedupe.set_db(db)
    app_state["sink_dedupe_store"] = _dedupe

    # ── Connection pool (Critical #5 Phase 2) ─────────────────────────
    # Per-run driver-connection cache. Currently wired into Postgres
    # path only (db_source._query_postgresql); other dialects still use
    # the direct-connect path. The pool is OPTIONAL — every consumer
    # falls back gracefully if it's missing. See
    # DESIGN_CONNECTION_POOLING.md for the full design + rollout plan.
    from fpulse.engine.connection_pool import ConnectionPool
    app_state["connection_pool"] = ConnectionPool()
    logger.info("Connection pool installed (per-run, max %d per connection_id).",
                app_state["connection_pool"]._max)

    # ── Wallet + dry-run guards (Step 1.5b-4) ──────────────────────────
    # WalletGuard: per-user + per-workspace daily token caps + rate limit.
    # DryRunPromoter: forces dry-run for new write tools until N successful runs.
    # Both schemas init idempotently; both are best-effort writers.
    from fpulse.ai.wallet import WalletGuard
    from fpulse.ai.dry_run_promoter import DryRunPromoter
    app_state["wallet_guard"] = WalletGuard(_db=db)
    app_state["dry_run_promoter"] = DryRunPromoter(_db=db)

    # ── RAG layer ──────────────────────────────────────────────────────
    # Embedder + vector store always installed; daily indexer scheduled in
    # the lifespan startup phase. Disable indexing entirely with
    # FPULSE_DISABLE_RAG=1 (the embedder + store are still constructed so
    # the recall_history tool returns empty rather than erroring).
    try:
        from fpulse.ai.rag.embedder import Embedder
        from fpulse.ai.rag.store import VectorStore
        app_state["rag_embedder"] = Embedder()
        app_state["rag_store"] = VectorStore(
            db_path=os.path.join(data_dir, "rag.db"),
        )
    except Exception as exc:
        logger.warning("RAG init failed (recall_history will be disabled): %s", exc)

    # Approval notifier needs user_store + db to already be in state.
    app_state["approval_notifier"] = ApprovalNotifier(
        notification_store=app_state["notification_store"],
        user_store=app_state["user_store"],
        db=db,
    )

    # Wire the long-running notifier into the worker pool watchdog
    # (May 3 2026). The pool's _timeout_watchdog_loop now detects
    # pipelines exceeding the admin-configured threshold and dispatches
    # a one-shot alert via ApprovalNotifier.on_long_running.
    try:
        app_state["worker_pool"].set_long_running_notifier(app_state["approval_notifier"])
    except Exception as exc:
        logger.warning("Could not wire long-running notifier: %s", exc)

    # Wire schedule-miss notifier into the scheduler. Fires when an
    # interval schedule is severely overdue (>= 2x interval).
    try:
        scheduler = app_state.get("scheduler")
        if scheduler is not None and hasattr(scheduler, "set_miss_notifier"):
            scheduler.set_miss_notifier(app_state["approval_notifier"])
    except Exception as exc:
        logger.warning("Could not wire schedule-miss notifier: %s", exc)

    # ── Optional enterprise stores (flag-gated) ───────────────────────
    # Reviewer 2 discipline: when a flag is OFF we DO NOT register a
    # placeholder None — the key is simply absent. Gated routes call
    # feature_flags.require() which raises FeatureDisabledError before
    # any KeyError can happen, so the failure mode is loud and explicit.
    if feature_flags.is_enabled("marketplace"):
        app_state["marketplace_store"] = MarketplaceStore(db=db)
    if feature_flags.is_enabled("lineage"):
        app_state["lineage_store"] = LineageStore(db=db)
    if feature_flags.is_enabled("collaboration"):
        app_state["collaboration_store"] = CollaborationStore(db=db)
    if feature_flags.is_enabled("plugins"):
        app_state["plugin_manager"] = PluginManager(
            plugins_dir=os.path.join(data_dir, "plugins"), db=db,
        )


async def _warmup() -> None:
    """Background warmup — pre-import heavy libraries AND pre-load the node
    registry AFTER the server is ready, so the FIRST user-facing request
    that touches pandas / duckdb / pyarrow / node types doesn't pay the
    import cost.

    Reviewer 3 refinement: the state field progression is honest —
    ``pending`` while running, then ``ok`` with no error, OR ``failed``
    with ``warmup_error`` populated.

    Stage 2.5 — FPULSE_WARMUP_HEAVY env var:
      • "1" (default for OSS / monolith): import duckdb / pyarrow / pandas
        AND pre-load the full node registry. Pays ~50 MB RSS + ~600 MB
        VMS up-front in exchange for instant first-request response.
        Right for the OSS single-binary install.
      • "0" (recommended for Plus fpulse-api container): skip the heavy
        imports. The fpulse-worker container does execution and pays
        the duckdb cost there. fpulse-api stays at the orchestration
        cost (~90 MB RSS). The first user request to /api/node-types
        pays a one-time ~150ms registry load.

    Honest measurement (2026-04-19):
      • Stage 1 baseline 90 MB was BEFORE registry load — misleading
      • Stage 2 with HEAVY=1 shows the true monolith cost ~141 MB
      • Stage 2 with HEAVY=0 should land back near 90 MB
    """
    heavy = os.environ.get("FPULSE_WARMUP_HEAVY", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }

    if not heavy:
        # Light warmup path — Plus API container, OSS dev with
        # explicit opt-out. We still mark warmup ok so the
        # operator-visible status reflects "warmup ran, just
        # didn't pre-import heavy libs by design."
        app_state["warmup_status"] = "ok"
        app_state["warmup_mode"] = "light"
        app_state.pop("warmup_error", None)
        logger.info(
            "Warmup complete (light mode — heavy libs deferred to first use)",
        )
        return

    try:
        # 1) Heavy libs — make sure they're in sys.modules
        import pandas  # noqa: F401
        import duckdb  # noqa: F401
        import pyarrow  # noqa: F401

        # 2) Node registry — triggers all 35 duckdb-importing node modules
        # to load. Wrapped in its own try/except so a single broken node
        # module doesn't fail the whole warmup; the registry already
        # silently swallows connector-import failures.
        try:
            get_registry()
        except Exception as reg_exc:
            logger.warning(
                "Node registry warmup failed (non-fatal): %s", reg_exc,
            )

        app_state["warmup_status"] = "ok"
        app_state["warmup_mode"] = "heavy"
        app_state.pop("warmup_error", None)  # clear any prior error on retry
        logger.info(
            "Warmup complete (heavy mode — pandas/duckdb/pyarrow + registry loaded)",
        )
    except Exception as exc:
        app_state["warmup_status"] = "failed"
        app_state["warmup_mode"] = "heavy"
        app_state["warmup_error"] = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "Warmup failed (heavy libs may not be installed): %s", exc,
        )


async def _rag_indexer_loop() -> None:
    """Daily RAG indexer — sleeps until 03:00 UTC, indexes all known
    workspaces, repeats. Best-effort: any failure is logged and the loop
    continues. Disabled when FPULSE_DISABLE_RAG=1.
    """
    from datetime import datetime, timezone, timedelta

    if os.environ.get("FPULSE_DISABLE_RAG", "").strip().lower() in ("1", "true", "yes"):
        logger.info("RAG indexer disabled via FPULSE_DISABLE_RAG")
        return

    while True:
        try:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            sleep_s = (target - now).total_seconds()
            await asyncio.sleep(sleep_s)

            embedder = app_state.get("rag_embedder")
            store = app_state.get("rag_store")
            workspace_store = app_state.get("workspace_store")
            if embedder is None or store is None:
                continue

            from fpulse.ai.rag.indexer import RAGIndexer
            indexer = RAGIndexer(embedder=embedder, vector_store=store)

            workspace_ids: list[str] = ["default"]
            try:
                if workspace_store is not None:
                    rows = workspace_store.list_all() or []
                    workspace_ids = [r.get("id", "default") for r in rows] or ["default"]
            except Exception:
                pass

            docs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "docs")
            for ws_id in workspace_ids:
                try:
                    await indexer.index_workspace(
                        ws_id, app_state=app_state, docs_dir=docs_dir,
                    )
                except Exception as exc:
                    logger.warning("RAG indexing failed for workspace=%s: %s", ws_id, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("RAG indexer loop error (will retry): %s", exc)
            await asyncio.sleep(3600)


async def _step_output_pruner_loop() -> None:
    """Daily TTL pruner for the step_outputs replay store.

    Drops `sample_rows` on records older than the OSS TTL (30 days);
    `row_count` and schema metadata are retained indefinitely so the
    historical lineage view still resolves even after the data sample
    has aged out. Disabled via FPULSE_DISABLE_STEP_OUTPUT_PRUNE=1.
    """
    from datetime import datetime, timezone, timedelta
    from fpulse.engine.step_output_store import SAMPLE_TTL_DAYS

    if os.environ.get("FPULSE_DISABLE_STEP_OUTPUT_PRUNE", "").strip().lower() in ("1", "true", "yes"):
        logger.info("Step-output pruner disabled via FPULSE_DISABLE_STEP_OUTPUT_PRUNE")
        return

    while True:
        try:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=3, minute=30, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            sleep_s = (target - now).total_seconds()
            await asyncio.sleep(sleep_s)

            store = app_state.get("step_output_store")
            if store is None:
                continue

            pruned = store.prune_samples()
            if pruned > 0:
                logger.info(
                    "Step-output pruner: cleared samples on %d rows (TTL=%d days)",
                    pruned, SAMPLE_TTL_DAYS,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Step-output pruner error (will retry): %s", exc)
            await asyncio.sleep(3600)


# ── Lifespan: replaces @app.on_event("startup"/"shutdown") ───────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Single source of startup / shutdown truth.

    Startup phase (Stage 2 reordering):
      1. Resolve data dir, instantiate stores into app_state (flag-gated)
      2. Auto-backup SQLite (best-effort)
      3. (deferred) Node registry → background _warmup() task
      4. Start worker pool (best-effort)
      5. Start scheduler (best-effort)
      6. Load plugins (best-effort, only if FPULSE_ENABLE_PLUGINS=1)
      7. Start backup scheduler (best-effort)
      8. Run audit retention (best-effort)
      9. Schedule warmup task (sets warmup_status=pending, then ok|failed)
     10. Print operator banner
     → yield (server READY; warmup runs in background)

    Shutdown phase (REVERSED order — reviewer 2):
      1. Stop worker pool (drain in-flight jobs)
      2. Stop scheduler (no new jobs)
      3. Stop backup scheduler
      4. Close database  (LAST — workers may still write during stop)

    Each step is wrapped in its own try/except so a failure in one step
    can't prevent later steps from running. This matters: if the backup
    scheduler fails to start we still want the worker pool and scheduler
    running, and on shutdown we MUST close the DB even if a worker
    refuses to drain cleanly.
    """
    # ── STARTUP ──────────────────────────────────────────────────────
    data_dir = _resolve_data_dir()

    # Populate app_state and expose via app.state so health_memory.py and
    # any future router can read state via Request without importing main.
    _populate_state(data_dir)
    app.state.fpulse_state = app_state

    # 0) Seed bundled demo data — copies fpulse/seed_data/orders.csv into
    # FPULSE_DATA_DIR/samples/orders.csv on first run. Idempotent: only
    # copies when the target is missing, so an operator who replaced the
    # sample on their host won't have it silently overwritten. Powers the
    # "First pipeline" template in the OSS Templates gallery.
    try:
        _seed_demo_data(data_dir)
    except Exception as exc:
        logger.warning("Demo-data seeding skipped: %s", exc)

    # 0.5) Security hygiene — warn if INITIAL_ADMIN_PASSWORD.txt is still
    # present. The bootstrap flow writes a one-time admin password to disk
    # so a fresh operator can find it; once they've signed in and rotated,
    # the file should be deleted (api/auth.py:change_my_password does that
    # automatically when admin@fpulse.local rotates). A lingering file is
    # a real security gap on shared hosts.
    try:
        _warn_if_bootstrap_password_lingers(data_dir)
    except Exception as exc:
        logger.warning("Bootstrap-password check skipped: %s", exc)

    # 0.6) Datastore reconcile — back-fill storage_objects rows for any
    # files that exist on disk but predate the v25 index (legacy uploads,
    # outputs from runs that pre-dated this code). Sentinel-gated so a
    # normal boot skips the scan; idempotent so a forced rescan never
    # double-indexes a path the row already covers. See
    # fpulse.datastore.reconcile for the policy.
    try:
        from fpulse.datastore.reconcile import reconcile_all
        reconcile_all(app_state["datastore"], data_dir)
    except Exception as exc:
        logger.warning("Datastore reconcile skipped: %s", exc)

    # 1) Auto-backup SQLite before anything writes to it
    try:
        from fpulse.storage.backup import backup_database
        database: Database = app_state["db"]
        backup_path = backup_database(database.db_path)
        if backup_path:
            print(f"  Backup:    {backup_path}")
    except Exception as exc:
        logger.warning("Auto-backup skipped: %s", exc)

    # 2) Node registry — DEFERRED to background _warmup().
    # The registry's import chain pulls duckdb in via 35 node modules, which
    # was previously paid synchronously here and counted toward "lifespan
    # complete" RSS. Moving it to warmup means readiness is signaled fast
    # and the duckdb import cost lands on a background task. The frontend's
    # first /api/node-types call (page load takes ~1s) will almost always
    # arrive after warmup completes.
    pass

    # 3) Start worker pool
    try:
        worker_pool: WorkerPool = app_state["worker_pool"]
        worker_pool.start()
    except Exception as exc:
        logger.error("Worker pool failed to start: %s", exc)

    # 4) Start background scheduler
    try:
        scheduler: PipelineScheduler = app_state["scheduler"]
        scheduler.start()
    except Exception as exc:
        logger.error("Scheduler failed to start: %s", exc)

    # 5) Load plugins (only if plugin_manager was instantiated — flag-gated)
    if "plugin_manager" in app_state:
        try:
            plugin_mgr: PluginManager = app_state["plugin_manager"]
            plugin_result = plugin_mgr.load_all()
            if plugin_result.get("loaded"):
                print(
                    f"  Plugins:   {plugin_result['loaded']} loaded "
                    f"({plugin_result['nodes']} nodes)",
                )
        except Exception as exc:
            logger.warning("Plugin loading failed: %s", exc)

    # 6) Warmup — schedule AFTER all critical startup so the import
    # cost lands on a thread that can't block readiness. The status field
    # is initialized to "pending" BEFORE create_task so an operator who
    # hits /api/health/memory in the first millisecond after startup
    # sees an honest pending state, never a stale "not_applicable".
    app_state["warmup_status"] = "pending"
    asyncio.create_task(_warmup())

    # 6b) RAG indexer — daily 03:00 UTC over all known workspaces.
    # Best-effort, never blocks startup. Disabled via FPULSE_DISABLE_RAG=1.
    if os.environ.get("FPULSE_DISABLE_RAG", "").strip().lower() not in ("1", "true", "yes"):
        app_state["rag_indexer_task"] = asyncio.create_task(_rag_indexer_loop())
        logger.info("RAG indexer scheduled (daily 03:00 UTC)")

    # 6b-bis) Step-output TTL pruner — daily 03:30 UTC. Drops aged
    # data samples from the execution-replay store; counts + schema
    # are retained indefinitely. Disabled via FPULSE_DISABLE_STEP_OUTPUT_PRUNE=1.
    if os.environ.get("FPULSE_DISABLE_STEP_OUTPUT_PRUNE", "").strip().lower() not in ("1", "true", "yes"):
        app_state["step_output_pruner_task"] = asyncio.create_task(_step_output_pruner_loop())
        logger.info("Step-output pruner scheduled (daily 03:30 UTC, 30-day TTL on samples)")

    # 6c) Product knowledge — Layer 2 of the chat knowledge architecture.
    # Indexes curated `docs/product_facts/*.md` so the AI Copilot can
    # answer F-Pulse-specific questions ("how do I use SCD2?", "what is
    # DEV vs PROD?") without fine-tuning. Idempotent — re-runs replace
    # existing chunks. Best-effort; failures don't block startup.
    if os.environ.get("FPULSE_DISABLE_PRODUCT_KNOWLEDGE", "").strip().lower() not in ("1", "true", "yes"):
        async def _index_product_knowledge_task():
            import time as _time
            t0 = _time.perf_counter()
            try:
                embedder = app_state.get("rag_embedder")
                store = app_state.get("rag_store")
                if embedder is None or store is None:
                    return
                from fpulse.ai.product_knowledge import index_product_knowledge
                from fpulse.api.product_knowledge import record_startup_reindex
                counts = await index_product_knowledge(
                    embedder=embedder, vector_store=store,
                )
                elapsed_ms = int((_time.perf_counter() - t0) * 1000)
                logger.info(
                    "Product knowledge indexed: %d chunks from %d files in %dms",
                    counts.get("chunks", 0), counts.get("files", 0), elapsed_ms,
                )
                # Publish to /api/ai/product-knowledge/status so admins see
                # the live counts without needing to grep logs.
                try:
                    record_startup_reindex(counts, elapsed_ms)
                except Exception:
                    pass
            except Exception as exc:
                logger.warning("Product knowledge indexing failed: %s", exc)
                try:
                    from fpulse.api.product_knowledge import record_startup_failure
                    record_startup_failure(str(exc))
                except Exception:
                    pass

        app_state["product_knowledge_task"] = asyncio.create_task(
            _index_product_knowledge_task(),
        )

    # 7) Operator banner
    try:
        smtp_status = (
            "configured"
            if app_state["notifier"].smtp_host
            else "dry-run (set SMTP_HOST to enable)"
        )
        database = app_state["db"]
        worker_pool = app_state["worker_pool"]

        _docs_line = (
            "  API:       http://localhost:8001/docs\n"
            if _api_docs_enabled
            else "  API:       http://localhost:8001  (Swagger /docs disabled; set FPULSE_ENABLE_API_DOCS=1 to enable)\n"
        )
        print(
            f"\n"
            f"  F-Pulse v1.0.0\n"
            f"  Open-source data pipeline builder\n"
            f"{_docs_line}"
            f"  Data:      {app_state['data_dir']}\n"
            f"  Database:  {database.db_path}\n"
            f"  Projects:  {app_state['project_store'].count()}\n"
            f"  Workers:   {worker_pool.max_workers} concurrent (priority P1-P5)\n"
            f"  Scheduler: running (30s check interval)\n"
            f"  Alerts:    {smtp_status}\n"
            f"  Storage:   SQLite (persistent — your data survives restarts)\n"
        )
    except Exception as exc:
        logger.warning("Banner print failed (non-fatal): %s", exc)

    # 8) Telemetry — emit a startup event if the operator has opted in.
    # Gated inside send_event by is_telemetry_enabled(db); the call is
    # cheap and silent when consent is off. Fire-and-forget so a slow
    # send never blocks the ready signal.
    try:
        from fpulse.telemetry import send_event as _send_telemetry
        asyncio.create_task(_send_telemetry(
            "startup",
            db=app_state["db"],
            fpulse_version="1.0.0",
            app_state=app_state,
        ))
    except Exception as exc:  # noqa: BLE001 — never block startup on telemetry
        logger.debug("telemetry startup event failed (non-fatal): %s", exc)

    # 10) Event bus + built-in Prometheus consumer.
    # The bus is the single pub/sub spine for pipeline state,
    # approval events, alerts, audit, and metrics. OSS gets the
    # in-process SQLite-backed implementation; Plus flips to NATS
    # via FPULSE_EVENT_BUS=nats (see fpulse.events.factory).
    # Failure here is non-fatal — observability is a side concern;
    # if the bus fails to start, runs still execute via the legacy
    # on_event callback path.
    try:
        bus = get_event_bus()
        metrics_consumer = MetricsConsumer()
        metrics_consumer.install(bus)
        # Audit consumer — append-only JSONL of every durable event.
        # File lives in the data dir so it ships alongside the SQLite
        # DB on backups; operator can point logrotate / SIEM at it.
        audit_path = os.path.join(data_dir, "audit.jsonl")
        audit_consumer = AuditConsumer(path=audit_path)
        audit_consumer.install(bus)
        app_state["event_bus"] = bus
        app_state["metrics_consumer"] = metrics_consumer
        app_state["audit_consumer"] = audit_consumer
        logger.info(
            "event bus started; /metrics endpoint enabled; audit log -> %s",
            audit_path,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("event bus init failed (non-fatal): %s", exc)
        app_state["event_bus"] = None
        app_state["metrics_consumer"] = None
        app_state["audit_consumer"] = None

    # ── READY ────────────────────────────────────────────────────────
    yield

    # ── SHUTDOWN (reverse order — reviewer 2) ────────────────────────
    # Hard exit guarantee (2026-06-18). The worker pool's ThreadPoolExecutor
    # threads are non-daemon, so a single stuck/long job wedges interpreter
    # exit AFTER uvicorn has already released the socket — the classic
    # "port is free but the process won't die" hang that breaks restarts.
    # Arm a daemon force-exit timer up front: the graceful teardown below
    # runs first and, when it's clean, the process exits normally well
    # before this fires; if anything wedges, os._exit guarantees we still
    # go down. Tune/disable with FPULSE_SHUTDOWN_GRACE_S (0 = disabled).
    import os as _os
    import threading as _threading
    try:
        _grace = float(_os.environ.get("FPULSE_SHUTDOWN_GRACE_S", "15"))
    except (TypeError, ValueError):
        _grace = 15.0
    if _grace > 0:
        _forced = _threading.Timer(_grace, lambda: _os._exit(0))
        _forced.daemon = True
        _forced.start()

    # 0) ExecutionManager — cancels every live subprocess runner before
    #    pool.stop(). PR5 step 5: this is how uvicorn's SIGTERM/SIGINT
    #    propagates into our task registry. shutdown() internally calls
    #    pool.stop(), so the explicit call below is belt-and-suspenders
    #    for the case where execution_manager failed to initialize.
    try:
        execution_manager = app_state.get("execution_manager")
        if execution_manager:
            execution_manager.shutdown(timeout_s=30)
    except Exception as exc:
        logger.error("ExecutionManager shutdown error: %s", exc)

    # 1) Worker pool — drain in-flight jobs. Idempotent after manager shutdown.
    try:
        worker_pool = app_state.get("worker_pool")
        if worker_pool:
            worker_pool.stop()
    except Exception as exc:
        logger.error("Worker pool shutdown error: %s", exc)

    # 2) Scheduler — stop scheduling new work
    try:
        scheduler = app_state.get("scheduler")
        if scheduler:
            scheduler.stop()
    except Exception as exc:
        logger.error("Scheduler shutdown error: %s", exc)

    # 3) Event bus — drain pending publishes, close transport.
    # Before DB close so any final "shutdown" event lands first.
    try:
        bus = app_state.get("event_bus")
        if bus is not None:
            bus.close()
    except Exception as exc:
        logger.error("Event bus close error: %s", exc)

    # 4) Database LAST — at this point nothing should still be writing.
    try:
        database = app_state.get("db")
        if database:
            database.close()
    except Exception as exc:
        logger.error("Database close error: %s", exc)


# ── FastAPI app ──────────────────────────────────────────────────────────
# Swagger UI (`/docs`) + OpenAPI document (`/openapi.json`) policy:
#
#   - Enabled by default when FPULSE_MODE=dev. The dev posture explicitly
#     opts into operator-visible internals (and the frontend uses the
#     OpenAPI doc to generate typed API client code via
#     `npm run codegen:api`, which closes a real source of frontend/backend
#     contract drift).
#   - Disabled by default in any other mode (prod, staging) — docs would
#     otherwise be anonymously reachable and enumerate every mounted router.
#   - Explicit override via FPULSE_ENABLE_API_DOCS=1 (force on) or =0
#     (force off) wins over the mode-based default.
#
# Updated 2026-05-22 to support OpenAPI-driven frontend type generation.
_FPULSE_MODE = os.environ.get("FPULSE_MODE", "").strip().lower()
_api_docs_override = os.environ.get("FPULSE_ENABLE_API_DOCS", "").strip().lower()
if _api_docs_override in {"1", "true", "yes", "on"}:
    _api_docs_enabled = True
elif _api_docs_override in {"0", "false", "no", "off"}:
    _api_docs_enabled = False
else:
    _api_docs_enabled = _FPULSE_MODE == "dev"

# Self-hosted Swagger UI / ReDoc assets.
#
# 2026-06-10: FastAPI's built-in /docs + /redoc load the swagger-ui / redoc
# JS+CSS from cdn.jsdelivr.net at runtime. That leaves the docs page blank on
# any air-gapped, firewalled, or web-filtered machine — e.g. Kaspersky Web
# Anti-Virus returns HTTP 503 for the CDN, so `SwaggerUIBundle` is never
# defined and the page renders white. F-Pulse positions itself as a
# sovereign, offline-capable tool, so the API docs must not depend on an
# external CDN. We vendor the assets under fpulse/static/swagger-ui/ and
# serve /docs + /redoc from same-origin URLs instead (no network egress).
_SWAGGER_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static", "swagger-ui")
_swagger_assets_present = os.path.isfile(
    os.path.join(_SWAGGER_STATIC_DIR, "swagger-ui-bundle.js")
)

# docs_url / redoc_url are forced to None so FastAPI does not register its
# CDN-backed defaults; we add same-origin routes below. openapi_url stays
# gated by the same mode/override policy.
app = FastAPI(
    title="F-Pulse",
    description="AI-native, human-governed data pipeline builder",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url="/openapi.json" if _api_docs_enabled else None,
)

if _api_docs_enabled and _swagger_assets_present:
    # Serve Swagger UI + ReDoc from same-origin vendored assets (no CDN).
    # Registered here — before the frontend catch-all StaticFiles mount at
    # "/" — so the asset mount wins route precedence. See fpulse.docs_static.
    from fpulse.docs_static import mount_self_hosted_docs

    mount_self_hosted_docs(app, _SWAGGER_STATIC_DIR)
elif _api_docs_enabled and not _swagger_assets_present:
    logger.warning(
        "API docs enabled but vendored swagger-ui assets are missing at %s; "
        "/docs and /redoc will return 404. Re-add fpulse/static/swagger-ui/.",
        _SWAGGER_STATIC_DIR,
    )

# CORS configuration.
#
# `allow_origins=["*"]` + `allow_credentials=True` is silently rejected by
# every browser, which is why cross-origin POSTs from the Vite preview
# (5173/5174) to the uvicorn backend (8001) used to fail with 401 — the
# session cookie/Authorization header was never sent.
#
# Production operators set FPULSE_CORS_ORIGINS to a comma-separated allowlist.
# In dev (no env var), we name the common dev-server ports and allow any
# localhost origin via regex so credentials actually flow.
import os as _os  # noqa: E402

_cors_origins_env = _os.environ.get("FPULSE_CORS_ORIGINS", "").strip()
if _cors_origins_env:
    _cors_origins = [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
    _cors_origin_regex: str | None = None
else:
    from fpulse import runtime_config as _rc_cors
    if _rc_cors.IS_SERVER_MODE:
        # Server mode: NO permissive localhost wildcard. Same-origin only
        # (the backend serves the frontend same-origin in a real deployment)
        # unless the operator explicitly names origins via FPULSE_CORS_ORIGINS.
        _cors_origins = []
        _cors_origin_regex = None
        logger.warning(
            "SECURITY: FPULSE_SECURITY_MODE=server with no FPULSE_CORS_ORIGINS "
            "— cross-origin requests are blocked (same-origin only). Set "
            "FPULSE_CORS_ORIGINS to a comma-separated allowlist if a separate "
            "frontend origin needs access."
        )
    else:
        _cors_origins = [
            "http://localhost:5173",
            "http://localhost:5174",
            "http://localhost:3000",
            "http://localhost:8001",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:5174",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:8001",
        ]
        _cors_origin_regex = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=_cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Prometheus instrumentation. The middleware records every
# HTTP request's count + duration into the metrics registry. The
# /api/metrics route exposes the registry. Both no-op gracefully when
# prometheus_client isn't installed (OSS install path).
from fpulse.api.metrics import (  # noqa: E402
    PrometheusMetricsMiddleware,
    router as metrics_router,
)
app.add_middleware(PrometheusMetricsMiddleware)

# Security response headers (HSTS / CSP / X-Frame-Options / nosniff /
# Referrer-Policy / Permissions-Policy + Server fingerprint strip). Added
# last so it wraps the outermost response — any header set by a downstream
# middleware or route is preserved, only missing ones are filled in.
from fpulse.api.security_headers import SecurityHeadersMiddleware  # noqa: E402
app.add_middleware(SecurityHeadersMiddleware)

# 2026-06-02 OSS-local hardening: cross-origin guard for loopback-bound
# installs (DNS-rebinding defense) + bind-info endpoint that powers the
# UI "exposed on LAN" banner. No-op when backend is bound to non-loopback
# (Plus server deployments) — the existing CORS middleware handles
# legitimate cross-origin traffic there.
from fpulse.api.local_hardening import (  # noqa: E402
    LocalOriginGuardMiddleware,
    router as local_hardening_router,
)
app.add_middleware(LocalOriginGuardMiddleware)
app.include_router(local_hardening_router, prefix="/api", tags=["health"])


# ── BFF CSRF guard (Phase 6) ─────────────────────────────────────────────
# Double-submit CSRF check enforced ONLY for cookie-authenticated,
# state-changing requests. Bearer / API-key callers (CLI, service,
# programmatic) are exempt — they can't be driven cross-site — so this is a
# no-op while the frontend still sends a bearer token. Login/register are
# exempt as the entry points that mint the CSRF token.
@app.middleware("http")
async def _csrf_guard(request, call_next):
    import hmac
    if request.method not in ("GET", "HEAD", "OPTIONS", "TRACE"):
        path = request.url.path
        exempt = path.startswith("/api/auth/login") or path.startswith("/api/auth/register")
        if (
            not exempt
            and not request.headers.get("Authorization", "").startswith("Bearer ")
            and request.cookies.get("fpulse_session")
        ):
            sent = request.headers.get("X-CSRF-Token", "")
            expected = request.cookies.get("fpulse_csrf", "")
            if not (sent and expected and hmac.compare_digest(sent, expected)):
                from starlette.responses import JSONResponse
                return JSONResponse(
                    {"detail": "CSRF token missing or invalid"}, status_code=403
                )
    return await call_next(request)


# ── Feature-flag handler (Stage 2) ───────────────────────────────────────
# A disabled feature returns 503 with the explicit env var name to flip,
# NOT 500 — an operator who turned a feature off should see "yes that's
# off" not "something blew up". Listed BEFORE the catch-all Exception
# handler so it wins.
@app.exception_handler(FeatureDisabledError)
async def _feature_disabled_handler(request: Request, exc: FeatureDisabledError):
    return JSONResponse(
        status_code=503,
        content={
            "detail": "feature_disabled",
            "feature": exc.feature,
            "message": str(exc),
            "path": str(request.url.path),
        },
    )


# ── Global Exception Handler ─────────────────────────────────────────────
# Catches ANY unhandled exception so the server stays alive. Without this,
# an uncaught error in a route kills the whole process.
@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    tb_text = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
    logger.critical(
        "Unhandled exception on %s %s: %s\n%s",
        request.method, request.url.path, exc, tb_text,
    )
    # Best-effort telemetry crash report. Gated by consent inside send_event;
    # never raises into the handler. The sanitizer drops user paths and env-var
    # values from the traceback before queuing.
    try:
        from fpulse.telemetry import send_event as _send_telemetry
        db = app_state.get("db") if isinstance(app_state, dict) else None
        if db is not None:
            asyncio.create_task(_send_telemetry(
                "crash",
                db=db,
                fpulse_version="1.0.0",
                exception_type=f"{type(exc).__module__}.{type(exc).__name__}",
                stack_trace=tb_text,
                app_state=app_state,
            ))
    except Exception:  # noqa: BLE001 — telemetry is never allowed to break the handler
        pass
    # Full trace is in the server log + (consented) telemetry above. The
    # response body intentionally omits the exception string because it
    # routinely embeds SQL fragments, internal file paths and class names
    # that should not reach API consumers.
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "path": str(request.url.path),
        },
    )


# ── Register API routes ──────────────────────────────────────────────────
app.include_router(workflows_router)
app.include_router(execution_router)
app.include_router(backfills_router)
app.include_router(planner_router)
app.include_router(projects_router)
app.include_router(folders_router)
app.include_router(workspaces_router)
app.include_router(schedules_router)
app.include_router(alerts_router)
app.include_router(monitor_router)
app.include_router(dashboard_router)
app.include_router(auth_router)
app.include_router(variables_router)
app.include_router(credentials_router)
app.include_router(intelligence_router)
app.include_router(contracts_router)
app.include_router(schema_history_router)
app.include_router(connections_router)
app.include_router(extraction_router)
app.include_router(auth_health_router)
app.include_router(system_router)
app.include_router(pipeline_health_router)
app.include_router(pipeline_health_per_router)
app.include_router(types_meta_router)
app.include_router(expressions_router)
# 2026-06-05 — Steward (Archeologist). Read-only background reliability
# layer. See backend/fpulse/steward/__init__.py for the architectural
# invariants — never inline mutate workflows or connections.
app.include_router(steward_router)
app.include_router(backup_router)
app.include_router(ws_router)
app.include_router(ws_info_router)
app.include_router(logs_router)
app.include_router(ai_router)
app.include_router(ai_config_router)
app.include_router(agent_router)
app.include_router(ollama_router)
app.include_router(pre_publish_router)
app.include_router(catalog_router)
app.include_router(mcp_router)
app.include_router(activity_router)
app.include_router(cert_matrix_router)
app.include_router(sync_state_router)
app.include_router(trust_router)
app.include_router(product_knowledge_router)
app.include_router(connector_authoring_router)
app.include_router(connector_drafts_router)
app.include_router(app_meta_router)
app.include_router(templates_router)
app.include_router(exports_router)
app.include_router(notifications_router)
app.include_router(pool_router)
app.include_router(lineage_router)
from fpulse.api.router_audit import router as router_audit_router  # noqa: E402
app.include_router(router_audit_router, prefix="/api")
from fpulse.api.agent_action import router as agent_action_router  # noqa: E402
app.include_router(agent_action_router)
from fpulse.api.router_tests import router as router_tests_router  # noqa: E402
app.include_router(router_tests_router, prefix="/api")
from fpulse.api.router_coverage import router as router_coverage_router  # noqa: E402
app.include_router(router_coverage_router, prefix="/api")
from fpulse.api.router_telemetry import router as router_telemetry_router  # noqa: E402
app.include_router(router_telemetry_router, prefix="/api")
app.include_router(marketplace_router)
app.include_router(collaboration_router)
app.include_router(gateway_router)
app.include_router(plugins_router)
app.include_router(uploads_router)
app.include_router(storage_router)
app.include_router(health_memory_router)
app.include_router(execution_manager_router)
app.include_router(reports_router)
app.include_router(pool_allocation_router)
app.include_router(workspace_settings_router)
# Per-workspace AI cost rate table — admin-editable in Settings.
app.include_router(ai_cost_rates_router)
app.include_router(deployments_router)
app.include_router(recipes_router)
app.include_router(metrics_router)


# ── Inline endpoints (kept here because they reach into app_state) ──────
@app.get("/api/executions/")
async def list_executions_alias(
    workflow_id: str | None = None,
    project_id: str | None = None,
    limit: int = 200,
):
    """Alias for /api/monitor/executions — used by ExecutionsPage."""
    store = app_state["execution_store"]
    if workflow_id:
        return store.list_by_workflow(workflow_id, limit)
    if project_id:
        return store.list_by_project(project_id, limit)
    return store.list_all(limit)


@app.get("/api/health")
async def health():
    """Liveness probe — lightweight, always 200 if the process is alive.

    Kubernetes / Docker liveness checks hit this. It must NEVER call the
    database or any external system — a slow DB must not trick the
    orchestrator into killing a perfectly healthy process.
    """
    from fpulse import runtime_config
    return {
        "status": "ok",
        "version": "1.0.0",
        "product": "F-Pulse OSS",
        "mode": runtime_config.MODE,
    }


# ── OSS stub for the Plus license endpoint ──────────────────────────────
# F-Pulse Plus exposes /api/plus/license to report tier + entitlements.
# OSS has no Plus router at all (see frontend/src/api/client.ts:228 for
# the matching "negative cache" payload), so without this stub every
# license probe (Dashboard mount, Sidebar tier chip, Account page,
# App.tsx boot) wrote a 404 line to backend logs — noise that erodes
# log signal-to-noise during launch demos.
#
# Returning a real 200 with the canonical "no license" shape:
#   * silences the 404s
#   * still routes the frontend through its tier=free code path
#   * matches the shape `_NEGATIVE_LICENSE_PAYLOAD` already synthesizes
#     on 404, so behaviour is byte-identical from the UI's perspective.
#
# Plus installs override this by mounting a real router on the same
# path BEFORE this fallback registers — FastAPI uses first-match
# routing, so the override wins.
@app.get("/api/plus/audit/events")
async def plus_audit_events_oss_stub(request: Request):
    """W2 (2026-05-30) — OSS-tier stub for /api/plus/audit/events.

    The Plus tier ships a richer audit-log explorer that supersedes
    OSS's basic audit_log table. In OSS we still want the path to
    EXIST so:

      * Anonymous callers get a clean 401 (not 404) — the cert /
        security tests expect this. A 404 leaks "no such endpoint";
        a 401 says "you need to be logged in." Same information
        disclosure as every other guarded route.
      * Authenticated callers get a 402 (Payment Required) with a
        clear "Plus tier only" message so the UI can show the upgrade
        prompt without ambiguity.

    The Plus build replaces this stub with the real handler — same
    URL, same auth-shape contract.
    """
    from fpulse.auth.deps import current_user_optional
    user = current_user_optional(request)
    if user is None:
        raise HTTPException(401, "Authentication required")
    raise HTTPException(
        402,
        {
            "code": "plus_tier_required",
            "message": (
                "The audit-events explorer is a F-Pulse+ feature. "
                "The basic audit log is available at the dashboard's "
                "Activity panel in OSS."
            ),
        },
    )


@app.get("/api/plus/license")
async def plus_license_oss_stub():
    """OSS-only fallback for /api/plus/license — see comment above.

    Plus replaces this with the real entitlement-checking handler.
    Keep the response shape stable so frontend cache + UI gating
    keep working unchanged across the OSS/Plus boundary.
    """
    return {
        "tier": "free",
        "active": False,
        "source": "oss-no-license-endpoint",
    }


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus text-format metrics endpoint.

    Convention: Prometheus scrapers expect ``/metrics`` (no /api/
    prefix). The data is produced by ``MetricsConsumer`` — a bus
    subscriber installed at startup. The executor publishes typed
    events; the consumer counts them. No executor code path needs to
    know Prometheus exists.

    Returns ``# event bus disabled\\n`` (200) if the bus failed to
    initialise at startup. Scrapers will see a syntactically valid
    empty payload and not crash — operators see the state in
    ``/api/health/ready``.
    """
    from fastapi.responses import PlainTextResponse
    consumer = app_state.get("metrics_consumer")
    if consumer is None:
        return PlainTextResponse(
            "# event bus disabled — see startup logs\n",
            media_type="text/plain; version=0.0.4",
        )
    return PlainTextResponse(
        consumer.render(),
        media_type="text/plain; version=0.0.4",
    )


@app.get("/api/health/ready")
async def health_ready():
    """Readiness probe — returns 200 only when the backend can serve real work.

    Checks:
      - SQLite DB reachable (SELECT 1)
      - Scheduler alive
      - Runtime config snapshot (so operators can verify limits)

    If ANY check fails the response is 503 with details so the load
    balancer (or start.ps1 healthcheck) knows we're degraded. Also
    returns 503 if lifespan startup hasn't populated app_state yet.
    """
    from fpulse import runtime_config

    if not app_state.get("db"):
        return JSONResponse(
            status_code=503,
            content={
                "status": "starting",
                "detail": "lifespan startup has not completed",
            },
        )

    scheduler: PipelineScheduler = app_state["scheduler"]
    notifier: NotificationService = app_state["notifier"]
    database: Database = app_state["db"]

    checks: dict = {}
    ready = True

    # 1) SQLite reachable
    try:
        database.fetchone("SELECT 1 AS ping")
        checks["sqlite"] = {"status": "ok", "path": database.db_path}
    except Exception as exc:
        checks["sqlite"] = {"status": "error", "detail": str(exc)}
        ready = False

    # 2) Scheduler
    checks["scheduler"] = {
        "status": "ok" if scheduler.is_running else "degraded",
        "active_jobs": scheduler.active_jobs,
    }

    # 3) SMTP (non-critical) — rebuild a fresh notifier so the UI's
    # smtp_configured flag reflects what the user just saved in
    # Settings → Notifications, not whatever was loaded at startup.
    try:
        _live_smtp = NotificationService._load_smtp_config()
        checks["notifications"] = {"smtp_configured": bool(_live_smtp.get("host"))}
    except Exception:
        checks["notifications"] = {"smtp_configured": bool(notifier.smtp_host)}

    # Stage 2: read raw registry dict instead of get_registry().all_types()
    # — calling get_registry() would trigger the deferred duckdb import
    # storm just to satisfy a readiness probe, which would defeat the
    # warmup deferral. Raw _REGISTRY count is 0 until warmup completes;
    # operators can correlate with warmup_status in /api/health/memory.
    from fpulse.nodes import registry as _registry_mod
    node_types_loaded = len(getattr(_registry_mod, "_REGISTRY", {}))

    result = {
        "status": "ok" if ready else "degraded",
        "version": "1.0.0",
        "product": "F-Pulse OSS",
        "runtime": runtime_config.snapshot(),
        "persistence": checks.get("sqlite", {}),
        "projects": app_state["project_store"].count(),
        "node_types": node_types_loaded,
        "warmup_status": app_state.get("warmup_status", "not_applicable"),
        "scheduler": checks["scheduler"],
        "notifications": checks["notifications"],
    }

    if not ready:
        return JSONResponse(content=result, status_code=503)
    return result


@app.get("/api/system/metrics")
async def system_metrics():
    """Return system resource usage — CPU, memory, disk, process info."""
    import time
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(os.getcwd())
        return {
            "cpu": {
                "percent": psutil.cpu_percent(interval=0),
                "cores": psutil.cpu_count(),
                "load_avg": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            },
            "memory": {
                "total_mb": round(mem.total / 1048576),
                "used_mb": round(mem.used / 1048576),
                "available_mb": round(mem.available / 1048576),
                "percent": mem.percent,
            },
            "disk": {
                "total_gb": round(disk.total / 1073741824, 1),
                "used_gb": round(disk.used / 1073741824, 1),
                "free_gb": round(disk.free / 1073741824, 1),
                "percent": disk.percent,
            },
            "process": {
                "pid": proc.pid,
                "memory_mb": round(proc.memory_info().rss / 1048576, 1),
                "cpu_percent": proc.cpu_percent(interval=0),
                "threads": proc.num_threads(),
                "uptime_seconds": round(time.time() - proc.create_time()),
            },
        }
    except ImportError:
        return {
            "cpu": {"percent": 0, "cores": os.cpu_count() or 1, "load_avg": None},
            "memory": {"total_mb": 0, "used_mb": 0, "available_mb": 0, "percent": 0},
            "disk": {"total_gb": 0, "used_gb": 0, "free_gb": 0, "percent": 0},
            "process": {"pid": os.getpid(), "memory_mb": 0, "cpu_percent": 0, "threads": 0, "uptime_seconds": 0},
            "_note": "Install psutil for real metrics: pip install psutil",
        }


@app.get("/api/system/resource-alerts")
async def check_resource_alerts():
    """Check system resources against thresholds and return any violations.

    Resource pressure is system-wide — it affects ALL running pipelines,
    not just one. This endpoint returns violations + list of currently
    running pipelines that may be impacted.
    """
    import time
    violations = []
    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(os.getcwd())
        proc = psutil.Process(os.getpid())

        if cpu_pct >= 90:
            violations.append({
                "resource": "cpu", "current": round(cpu_pct, 1),
                "threshold": 90, "severity": "P1" if cpu_pct >= 95 else "P2",
                "message": f"CPU usage at {cpu_pct:.1f}% — all running pipelines may slow down or timeout",
            })
        if mem.percent >= 85:
            violations.append({
                "resource": "memory", "current": round(mem.percent, 1),
                "threshold": 85, "severity": "P1" if mem.percent >= 95 else "P2",
                "message": f"Memory at {mem.percent:.1f}% ({round(mem.used/1073741824,1)}GB / {round(mem.total/1073741824,1)}GB) — running pipelines risk OOM kill",
            })
        if disk.percent >= 90:
            violations.append({
                "resource": "disk", "current": round(disk.percent, 1),
                "threshold": 90, "severity": "P1" if disk.percent >= 95 else "P2",
                "message": f"Disk at {disk.percent:.1f}% — pipeline outputs and logs may fail to write",
            })
        proc_mem = round(proc.memory_info().rss / 1048576, 1)
        if proc_mem > 1024:
            violations.append({
                "resource": "process_memory", "current": proc_mem,
                "threshold": 1024, "severity": "P2",
                "message": f"F-Pulse process using {proc_mem}MB RAM — affects all pipeline execution capacity",
            })
    except ImportError:
        pass

    running_pipelines = []
    try:
        exec_store = app_state["execution_store"]
        all_execs = exec_store.list_all(limit=50)
        running_pipelines = [
            {
                "id": e.get("id", ""),
                "workflow_id": e.get("workflow_id", ""),
                "workflow_name": e.get("workflow_name", "Unknown"),
                "started_at": e.get("started_at", ""),
                "steps_completed": e.get("steps_completed", 0),
                "steps_total": e.get("steps_total", 0),
            }
            for e in all_execs
            if e.get("status") == "running"
        ]
    except Exception:
        pass

    return {
        "violations": violations,
        "running_pipelines": running_pipelines,
        "running_count": len(running_pipelines),
        "checked_at": time.time(),
        "has_violations": len(violations) > 0,
    }


@app.get("/api/system/update-readiness")
async def update_readiness():
    """Check if the system is safe to update/restart.

    Returns current running workloads and whether it's safe to perform
    a zero-downtime update. The admin should:
    1. Check this endpoint — if running_count > 0, wait or drain
    2. Pause the scheduler (stop new runs)
    3. Wait for running pipelines to complete
    4. Update F-Pulse binary/code
    5. Restart — existing pipeline definitions are safe in SQLite
    """
    import time

    exec_store = app_state["execution_store"]
    scheduler: PipelineScheduler = app_state["scheduler"]

    all_execs = exec_store.list_all(limit=50)
    running = [
        {
            "id": e.get("id"),
            "workflow_id": e.get("workflow_id"),
            "workflow_name": e.get("workflow_name", "Unknown"),
            "started_at": e.get("started_at"),
            "steps_completed": e.get("steps_completed", 0),
            "steps_total": e.get("steps_total", 0),
            "triggered_by": e.get("triggered_by", "unknown"),
        }
        for e in all_execs
        if e.get("status") == "running"
    ]

    safe = len(running) == 0

    return {
        "safe_to_update": safe,
        "running_count": len(running),
        "running_pipelines": running,
        "scheduler_active": scheduler.is_running,
        "active_jobs": scheduler.active_jobs,
        "recommendation": (
            "Safe to update — no pipelines running."
            if safe
            else f"Wait for {len(running)} running pipeline(s) to complete, or pause scheduler first."
        ),
        "update_steps": [
            "1. Check /api/system/update-readiness (this endpoint)",
            "2. Pause scheduler: stop new scheduled runs",
            "3. Wait for running pipelines to finish (or cancel non-critical ones)",
            "4. Update F-Pulse code/binary",
            "5. Restart process — all pipeline definitions, history, and credentials survive (SQLite)",
            "6. Verify /api/health returns ok",
        ],
        "checked_at": time.time(),
    }


@app.get("/api/scheduler/status")
async def scheduler_status():
    """Get background scheduler status and active jobs."""
    scheduler: PipelineScheduler = app_state["scheduler"]
    schedule_store = app_state["schedule_store"]
    all_schedules = schedule_store.list_all()
    enabled_count = sum(1 for s in all_schedules if s.get("enabled"))

    return {
        "running": scheduler.is_running,
        "active_jobs": scheduler.active_jobs,
        "total_schedules": len(all_schedules),
        "enabled_schedules": enabled_count,
    }


@app.get("/api/node-types")
async def node_types():
    """Return all available node types for the canvas palette + their
    canonical contract.

    Each entry now carries the metadata the frontend would otherwise have
    to hand-maintain in parallel:

      * ``arity`` — ``{required, optional, variadic}`` input cardinality.
        Lets the canvas draw the right number of input handles and lets
        ``validateWorkflow`` count incoming edges precisely.
      * ``side_effects`` — ``passthrough | transforming | terminal``
        (or ``null`` for pure nodes). Drives the side-effect badge on
        the canvas and the impact-card replay safety check.
      * ``deprecated`` + ``replaced_by`` — surfaces the
        ``DEPRECATED_STEP_TYPES`` registry so the UI can hide retired
        types from the palette while still rendering them on existing
        workflows that haven't been re-saved through the migration.

    This is the **canonical contract** the migrations doc calls out —
    every consumer (palette, validator, agent atlas, conformance test)
    reads from this one endpoint instead of maintaining its own copy.
    """
    from fpulse.ir.node_metadata import contract_for, output_kind_for, side_effect_class_for
    from fpulse.ir.migrations import DEPRECATED_STEP_TYPES

    registry = get_registry()
    types = registry.all_types()
    for entry in types:
        step_type = entry.get("type")
        if not step_type:
            continue
        contract = contract_for(step_type)
        entry["arity"] = {
            "required": contract["required"],
            "optional": contract["optional"],
            "variadic": contract["variadic"],
        }
        entry["side_effects"] = side_effect_class_for(step_type)
        entry["output_kind"] = output_kind_for(step_type)
        dep = DEPRECATED_STEP_TYPES.get(step_type)
        entry["deprecated"] = dep is not None
        entry["deprecation_reason"] = dep.reason if dep else None
        entry["replaced_by"] = dep.replaced_by if dep else None
        # Generic Source/Destination delegate by connector_type; expose the
        # per-connector field set (the concrete node's param_schema) so the
        # frontend/validator/AI see the real contract for each connector
        # instead of just the bare connector_type select.
        if step_type == "source":
            from fpulse.nodes.generic import GenericSourceNode
            entry["connector_schemas"] = GenericSourceNode.connector_schemas()
        elif step_type == "destination":
            from fpulse.nodes.generic import GenericDestinationNode
            entry["connector_schemas"] = GenericDestinationNode.connector_schemas()

    # R7 (2026-05-30) — Macro entries. Any workflow whose
    # `metadata.published_as_node` is set surfaces here as a virtual
    # palette tile typed `execute_pipeline:<wf_id>`. Dragging it onto
    # the canvas creates an execute_pipeline step with pipeline_id
    # pre-filled, and the macro's WorkflowParameter list becomes the
    # parameter contract surfaced by ConfigPanel.
    try:
        wf_store = app_state.get("store")
        if wf_store is not None:
            for v in wf_store.list_all(workspace_id=None):
                wf = v.workflow if hasattr(v, "workflow") else None
                if wf is None:
                    continue
                meta = getattr(wf, "metadata", None) or {}
                if not (isinstance(meta, dict) and meta.get("published_as_node")):
                    continue
                params_contract = [
                    {
                        "name": p.name, "type": p.type,
                        "default": p.default, "description": p.description,
                        "required": p.required,
                    }
                    for p in (getattr(wf, "parameters", None) or [])
                ]
                types.append({
                    "type": f"execute_pipeline:{wf.id}",
                    # R7b — `base_type` is the canonical StepType the
                    # frontend's addNode falls back to. Without this
                    # the dragged macro would persist with the virtual
                    # type and the executor would reject it as unknown.
                    "base_type": "execute_pipeline",
                    "label": (meta.get("published_label") or wf.name or wf.id),
                    "category": (meta.get("published_category") or "macro"),
                    "description": meta.get("published_description")
                        or f"Run the saved pipeline `{wf.name}` as a sub-step. (Macro)",
                    "default_params": {
                        "pipeline_id": wf.id,
                        "wait_for_completion": True,
                        "parameters": {p["name"]: p.get("default") for p in params_contract},
                    },
                    "param_schema": [
                        {"name": "pipeline_id", "type": "hidden", "default": wf.id},
                    ] + [
                        {"name": f"parameters.{p['name']}", "type": p["type"],
                         "label": p["name"], "default": p.get("default"),
                         "required": p.get("required", False),
                         "description": p.get("description", "")}
                        for p in params_contract
                    ],
                    "arity": {"required": 1, "optional": 0, "variadic": False},
                    "side_effects": "transforming",
                    "output_kind": "dataset",
                    "deprecated": False,
                    "macro": True,
                    "macro_workflow_id": wf.id,
                })
    except Exception:  # noqa: BLE001 — macros are an enhancement, not a blocker
        # Macro discovery is best-effort. If the store isn't ready or a
        # workflow has malformed metadata, fall back silently and let
        # the core types[] still render.
        pass

    return types


@app.get("/api/saas/manifests")
async def saas_manifests(refresh: bool = False):
    """Return loaded SaaS connector manifests for the universal saas_connector node.

    Pass `?refresh=true` to force a re-scan of the manifests/ directory after
    adding or editing connector JSON files without restarting the server.
    """
    from fpulse.connectors.rest_framework import list_manifests, load_manifests
    if refresh:
        load_manifests(force=True)
    out = []
    for m in list_manifests():
        out.append({
            "id": m.id,
            "name": m.name,
            "description": m.description,
            "category": m.category,
            "params": m.params,
            "streams": [{"name": s.get("name"), "label": s.get("label", s.get("name"))} for s in (m.streams or [])],
        })
    return out


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), replaces: str | None = None):
    """Upload a CSV/JSON/Parquet file to the data directory.

    When ``replaces`` is provided (the previous filename the same node
    pointed at) and points to a real file inside the data directory, it
    is deleted *after* the new upload succeeds — so a failed upload
    never strands the previous file. This keeps ``data_dir`` from
    accumulating orphans when a node's file is swapped.
    """
    allowed_ext = {".csv", ".json", ".parquet", ".tsv", ".txt", ".xlsx", ".xls", ".xml"}
    name = file.filename or "upload.csv"
    ext = os.path.splitext(name)[1].lower()
    if ext not in allowed_ext:
        return JSONResponse(status_code=400, content={"detail": f"Unsupported file type: {ext}"})

    dest_dir = app_state["data_dir"]
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)

    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)

    # Best-effort cleanup of the previous file. Path must resolve inside
    # data_dir (no traversal) and not be the file we just wrote.
    replaced = False
    if replaces:
        try:
            prev_abs = os.path.normpath(os.path.join(dest_dir, replaces))
            data_dir_norm = os.path.normpath(dest_dir)
            if (prev_abs.startswith(data_dir_norm + os.sep)
                    and prev_abs != os.path.normpath(dest)
                    and os.path.isfile(prev_abs)):
                os.remove(prev_abs)
                replaced = True
        except OSError:
            pass

    return {"filename": name, "path": name, "size": len(content), "replaced_previous": replaced}


# Data-file picker allowlist + secret denylist. The data dir doubles as
# the bootstrap-secret location (INITIAL_ADMIN_PASSWORD.txt, the master
# secret key), so an unfiltered listing leaks the initial admin password
# into the Source node's file picker. Exclude dotfiles and any
# secret-bearing / F-Pulse-internal file regardless of extension.
_LISTABLE_DATA_EXTS = {".csv", ".json", ".parquet", ".tsv", ".txt", ".xlsx", ".xls", ".xml"}
_SENSITIVE_BASENAMES = {"initial_admin_password.txt", "secret.key", ".env"}


def _is_listable_data_file(name: str) -> bool:
    """True if `name` is a user data file safe to surface in the picker."""
    low = name.lower()
    if name.startswith(".") or low in _SENSITIVE_BASENAMES:
        return False
    if "password" in low or "secret" in low or low.endswith(".key"):
        return False
    return os.path.splitext(low)[1] in _LISTABLE_DATA_EXTS


@app.get("/api/files")
async def list_files():
    """List user data files in the data dir for the Source node picker.

    Secret-bearing / F-Pulse-internal files are filtered out via
    `_is_listable_data_file`. This endpoint previously listed every
    `.txt` (etc.) in the data dir, which leaked INITIAL_ADMIN_PASSWORD.txt
    because the data dir is also where that one-time bootstrap file lands.
    """
    dest_dir = app_state["data_dir"]
    if not os.path.isdir(dest_dir):
        return []
    files = []
    for f in sorted(os.listdir(dest_dir)):
        if not _is_listable_data_file(f):
            continue
        fpath = os.path.join(dest_dir, f)
        files.append({"name": f, "size": os.path.getsize(fpath)})
    return files


class _SPAStaticFiles(StaticFiles):
    """Serve the built SPA, but force the browser to revalidate the HTML shell
    on every load (``Cache-Control: no-cache``).

    Hashed asset files (``index-<hash>.js``) keep their default long-lived
    caching — they're content-addressed and immutable, so re-fetching them is
    pure waste. Only the tiny ``index.html`` shell, which points at the current
    asset hashes, is forced to revalidate. Without this, browsers — and Edge's
    persistent WebView2/app profile especially — happily serve a stale
    ``index.html`` that references old asset hashes, so a freshly built UI never
    shows up until the user manually hard-reloads. This makes every plain reload
    pick up the latest build with no cache-bust query and no Python launcher.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        ctype = response.headers.get("content-type", "")
        if (
            path in ("", ".", "/", "index.html")
            or path.endswith(".html")
            or ctype.startswith("text/html")
        ):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


# Serve the frontend SPA. Two resolution paths, both real:
#
#   1. fpulse/frontend_dist/ — the PACKAGED build. This is what a
#      `pip install fpulse` user gets. scripts/stage_frontend.py copies
#      frontend/dist here before the wheel is built.
#   2. ../../frontend/dist — the repo checkout, for `pip install -e .`,
#      running from source, and the Docker image.
#
# Until this was fixed, only (2) existed. It resolves correctly from a
# checkout but points outside site-packages when installed from a wheel,
# so `pip install fpulse && fpulse open` — the README's headline command —
# booted the API and opened a browser onto nothing. It went unnoticed
# because every way the team tested (source checkout, `pip install -e .`,
# Docker, the desktop installers) hits path (2) and works. Only strangers
# installing from PyPI hit the broken path.
#
# The old check was a bare `os.path.isdir()` with no else branch, so a
# missing UI was *silent*. It is now loud: check for index.html (a dir can
# exist and be empty), and say so at ERROR when nothing is found.
_PACKAGED_DIST = os.path.join(os.path.dirname(__file__), "frontend_dist")
_SOURCE_DIST = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist")


def _resolve_frontend_dist() -> str | None:
    for candidate in (_PACKAGED_DIST, _SOURCE_DIST):
        if os.path.isfile(os.path.join(candidate, "index.html")):
            return candidate
    return None


frontend_dist = _resolve_frontend_dist()
if frontend_dist:
    app.mount("/", _SPAStaticFiles(directory=frontend_dist, html=True), name="frontend")
else:
    logger.error(
        "F-Pulse: no frontend build found — the API is running but the UI "
        "will NOT load (requests to / will 404). Looked for index.html in:\n"
        "  packaged: %s\n"
        "  source:   %s\n"
        "From a source checkout: cd frontend && npm install && npm run build\n"
        "If you installed from PyPI, this is a packaging bug — the wheel "
        "should carry frontend_dist/. Please report it.",
        os.path.normpath(_PACKAGED_DIST),
        os.path.normpath(_SOURCE_DIST),
    )


def _resolve_bind_host() -> str:
    """Resolve the bind address for the OSS local launcher.

    Default: ``127.0.0.1`` (loopback only — invisible to coworkers, hotel
    WiFi, conference floors). 2026-06-02 hardening flipped this from
    ``0.0.0.0`` after the security review caught the LAN-exposure risk.

    Operators who genuinely need LAN exposure (containerised deploys,
    on-prem multi-user setups) must opt in EXPLICITLY:

        FPULSE_BIND_HOST=0.0.0.0    # name a host directly
        FPULSE_ALLOW_LAN=1          # convenience flag → 0.0.0.0

    The convenience flag exists because typing the literal address has
    historically been mistyped (`0.0.0.0` vs `0.0.0.O`).

    Side-effect: also writes the resolved host into
    ``FPULSE_RESOLVED_BIND_HOST`` so middleware + the /api/health/bind-info
    endpoint can read it without re-doing the env-var dance.
    """
    explicit = os.environ.get("FPULSE_BIND_HOST", "").strip()
    if explicit:
        resolved = explicit
    elif os.environ.get("FPULSE_ALLOW_LAN", "").strip() in {"1", "true", "yes", "on"}:
        resolved = "0.0.0.0"
    else:
        resolved = "127.0.0.1"
    os.environ["FPULSE_RESOLVED_BIND_HOST"] = resolved
    return resolved


def cli():
    """CLI entry point — delegates to full CLI module if args present."""
    if len(sys.argv) > 1 and sys.argv[1] != "--reload":
        from fpulse.cli import main as cli_main
        cli_main()
    else:
        import uvicorn
        port = int(os.environ.get("FPULSE_PORT", "8001"))
        host = _resolve_bind_host()
        # Surface the bind choice prominently so the operator can't miss
        # whether they're loopback-only or LAN-exposed.
        if host == "127.0.0.1":
            print(f"F-Pulse listening on http://127.0.0.1:{port}  (loopback only — safe)")
        else:
            print(
                f"\n[WARNING] F-Pulse listening on {host}:{port} — "
                "reachable from your network. Anyone on the same LAN "
                "can hit the API. Set FPULSE_BIND_HOST=127.0.0.1 or "
                "unset FPULSE_ALLOW_LAN to disable LAN exposure.\n"
            )
        uvicorn.run("fpulse.main:app", host=host, port=port)


if __name__ == "__main__":
    cli()
