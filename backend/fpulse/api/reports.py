"""System Inventory Report + built-in documentation API.

Generates beautiful, industry-style documents describing the live
state of an F-Pulse installation. Admins get a full view; other users
get an ACL-filtered view of just what they can see.

Also exposes the /api/docs endpoints that the Help page uses to
render the minimum enterprise documentation (docs/readme.md, user
guides, admin runbook) inside the application without the user
leaving the UI.

Formats: docx (Microsoft Word), pdf (ReportLab), json (programmatic).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from fpulse.auth.deps import current_workspace_id, require_auth

logger = logging.getLogger("fpulse.api.reports")


def _persist_report_to_storage(
    workspace_id: str,
    blob: bytes,
    base_name: str,
    ext: str,
    scope: str,
    env: str,
) -> tuple[str | None, str | None]:
    """Write a generated report into the workspace storage tree so it
    appears in the Storage page (Files tab) for later re-download.

    Returns ``(object_id, error)``:
      * On success: ``(obj.id, None)``
      * On failure: ``(None, "stage: message")`` — the caller surfaces
        the error via the ``X-Storage-Error`` response header so frontend
        and ops can diagnose without trawling backend logs. The download
        path keeps working either way.

    Reports land under the uploads/ tree with kind=file. They are
    distinguished from user uploads by the ``report`` tag and a
    descriptive name.
    """
    # Stage by stage so the error message tells us exactly where it broke.
    try:
        from fpulse.datastore.store import get_store
        from fpulse.datastore.models import StorageObject, OBJECT_KIND_FILE
        from fpulse.datastore.paths import workspace_paths, format_from_filename
        from fpulse.main import app_state
    except Exception as exc:
        msg = f"import: {type(exc).__name__}: {exc}"
        logger.exception(msg)
        return None, msg

    try:
        data_dir = app_state.get("data_dir")
        if not data_dir:
            return None, "data_dir: app_state has no 'data_dir' key"
        paths = workspace_paths(data_dir, workspace_id).ensure()
        stored = f"{base_name}{ext}"
        abs_path = paths.upload_abs(stored)
    except Exception as exc:
        msg = f"paths: {type(exc).__name__}: {exc}"
        logger.exception(msg)
        return None, msg

    try:
        with open(abs_path, "wb") as out:
            out.write(blob)
    except Exception as exc:
        msg = f"write: {type(exc).__name__}: {exc}"
        logger.exception(msg)
        return None, msg

    try:
        obj = StorageObject(
            workspace_id=workspace_id,
            kind=OBJECT_KIND_FILE,
            name=f"{base_name}{ext}",
            path=paths.relative_to_data_dir(abs_path),
            format=format_from_filename(stored),
            size_bytes=len(blob),
            tags=["report", f"scope:{scope}", f"env:{env}"],
            description=(
                f"Generated inventory report — scope={scope}, env={env}"
            ),
        )
    except Exception as exc:
        msg = f"model: {type(exc).__name__}: {exc}"
        logger.exception(msg)
        return None, msg

    try:
        get_store().save_object(obj)
    except Exception as exc:
        msg = f"save: {type(exc).__name__}: {exc}"
        logger.exception(msg)
        return None, msg

    return obj.id, None

router = APIRouter(prefix="/api/reports", tags=["reports"])


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            500, f"workspace resolve failed: {type(exc).__name__}: {exc}",
        )


_ADMIN_ROLES = {"super_admin", "admin"}
_PROD_VIEWING_ROLES = {"super_admin", "admin", "lead"}
# Apr 27 2026: dropped 'all'. DEV and PROD reports show fundamentally
# different sections (build phase vs operate phase / audit), so the
# choice is binary. Legacy callers passing 'all' get downgraded to 'dev'
# for back-compat rather than a hard 4xx — same data they'd see today
# is still in DEV mode.
_ALLOWED_ENVS = {"dev", "prod"}


def _detect_tier() -> str:
    """Return "plus" or "free" based on the license manager state.

    Free-tier users still get a fully-formed report — just without the
    Plus-only sections (Users, Approval Gates) and with an Upgrade CTA.
    """
    try:
        from fpulse.main import app_state
        lm = app_state.get("license_manager")
        return "plus" if (lm and getattr(lm, "is_plus", False)) else "free"
    except Exception:
        return "free"


def _can_see_prod(user) -> bool:
    """Governance rule: developers are DEV-only by default.

    True iff the caller is an admin/lead OR has been explicitly granted
    `prod_permissions.can_view` by an admin. Everyone else sees DEV
    only — this matches the rest of the product (DEV/PROD env split on
    the main pages) and prevents developers from side-channelling PROD
    data out via the report endpoint.
    """
    role = getattr(user, "role", None) or ""
    if role in _PROD_VIEWING_ROLES:
        return True
    prod_perms = getattr(user, "prod_permissions", None) or {}
    if isinstance(prod_perms, dict) and prod_perms.get("can_view"):
        return True
    return False


def _enforce_env_access(user, requested_env: str) -> str:
    """Silently downgrade the caller's env choice to 'dev' if they are
    not allowed to see PROD. Returns the env_filter that will actually
    be applied.

    Front-ends also hide the PROD option for restricted users, but this
    is the security-critical enforcement — a caller hitting the API
    directly still ends up with DEV-only data.
    """
    if _can_see_prod(user):
        return requested_env
    # Developer without PROD permission → force DEV.
    return "dev"


@router.get("/inventory")
async def generate_inventory(
    format: str = "docx",
    scope: str = "admin",
    env: str = "all",
    scope_id: str | None = None,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Generate the System Inventory Report.

    Query params:
      format    = docx | pdf | json                  (default: docx)
      scope     = admin | user | project | pipeline  (default: admin)
                  - admin: full workspace; admin role required
                  - user: caller's view only
                  - project: filter to a single project (requires scope_id)
                  - pipeline: filter to a single pipeline (requires scope_id)
      scope_id  = the project_id / pipeline_id / user_id when scope ∈
                  {project, pipeline, user-with-explicit-target}

    Non-admin callers requesting `admin` scope are silently downgraded
    to `user`. project / pipeline scope filters use the InventoryCollector's
    existing scope_id_filter param.
    """
    # Normalise scope — extended set: admin, user, project, pipeline.
    normalized_scope = scope.lower()
    if normalized_scope not in ("admin", "user", "project", "pipeline"):
        raise HTTPException(400, "scope must be 'admin', 'user', 'project', or 'pipeline'")
    if normalized_scope == "admin" and getattr(user, "role", None) not in _ADMIN_ROLES:
        normalized_scope = "user"
    # project / pipeline require a scope_id
    if normalized_scope in ("project", "pipeline") and not scope_id:
        raise HTTPException(400, f"scope_id is required when scope='{normalized_scope}'")

    normalized_format = format.lower()
    if normalized_format not in ("docx", "pdf", "json"):
        raise HTTPException(400, "format must be 'docx', 'pdf', or 'json'")

    # Environment / report mode — dev|prod. Each renders a different
    # template (build phase vs operate phase / audit). Legacy 'all' is
    # silently downgraded to 'dev' for back-compat with old curl scripts.
    normalized_env = env.lower()
    if normalized_env == "all":
        normalized_env = "dev"
    if normalized_env not in _ALLOWED_ENVS:
        raise HTTPException(400, "env must be 'dev' or 'prod'")

    # Governance: developers are DEV-only by default (see _enforce_env_access).
    # The client ALSO hides the PROD button for restricted users, but this
    # is the security-critical layer — a developer cURLing the endpoint
    # still gets a DEV-only document, never PROD data.
    normalized_env = _enforce_env_access(user, normalized_env)

    # Detect tier — drives which sections render. Free tier skips Users
    # and Approval Gates and appends an Upgrade CTA.
    tier = _detect_tier()

    # Assemble the report.
    from fpulse.main import app_state
    from fpulse.reports.inventory import InventoryCollector

    try:
        collector = InventoryCollector(
            app_state=app_state,
            caller=user if normalized_scope == "user" else None,
            scope=normalized_scope,
            workspace_id=workspace_id,
            tier=tier,
            env_filter=normalized_env,
            # New (Apr 27 2026): for project/pipeline scopes the collector
            # filters its rollups against scope_id. Legacy admin/user paths
            # ignore it (kwarg defaults to None inside the collector).
            scope_id_filter=scope_id if normalized_scope in ("project", "pipeline", "user") else None,
        )
        report = collector.collect()
    except Exception as exc:
        logger.exception("inventory collection failed")
        raise HTTPException(500, "inventory collection failed") from exc

    # JSON: short-circuit, no rendering.
    if normalized_format == "json":
        return JSONResponse(report.to_dict())

    # File formats — render and stream.
    filename_stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    safe_ws = "".join(
        c if c.isalnum() or c in ("-", "_") else "_"
        for c in (report.workspace_name or "workspace")
    )
    base_name = f"fpulse-inventory-{safe_ws}-{filename_stamp}"

    if normalized_format == "docx":
        try:
            from fpulse.reports.inventory_docx import render_docx
            blob = render_docx(report)
        except Exception as exc:
            logger.exception("docx render failed")
            raise HTTPException(500, "docx render failed") from exc
        # Persist a copy into Storage so the user can re-download or
        # share it later from the Storage page without regenerating.
        # Storage write is best-effort; the immediate download path keeps
        # working even if persistence fails.
        object_id, storage_err = _persist_report_to_storage(
            workspace_id, blob, base_name, ".docx",
            normalized_scope, normalized_env,
        )
        resp_headers = {
            "Content-Disposition":
                f'attachment; filename="{base_name}.docx"',
        }
        exposed = ["Content-Disposition"]
        if object_id:
            resp_headers["X-Storage-Object-Id"] = object_id
            exposed.append("X-Storage-Object-Id")
        if storage_err:
            resp_headers["X-Storage-Error"] = storage_err[:256]
            exposed.append("X-Storage-Error")
        resp_headers["Access-Control-Expose-Headers"] = ", ".join(exposed)
        return Response(
            content=blob,
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            headers=resp_headers,
        )

    # PDF
    try:
        from fpulse.reports.inventory_pdf import render_pdf
        blob = render_pdf(report)
    except Exception as exc:
        logger.exception("pdf render failed")
        raise HTTPException(500, "pdf render failed") from exc
    object_id, storage_err = _persist_report_to_storage(
        workspace_id, blob, base_name, ".pdf",
        normalized_scope, normalized_env,
    )
    resp_headers = {
        "Content-Disposition": f'attachment; filename="{base_name}.pdf"',
    }
    exposed = ["Content-Disposition"]
    if object_id:
        resp_headers["X-Storage-Object-Id"] = object_id
        exposed.append("X-Storage-Object-Id")
    if storage_err:
        resp_headers["X-Storage-Error"] = storage_err[:256]
        exposed.append("X-Storage-Error")
    resp_headers["Access-Control-Expose-Headers"] = ", ".join(exposed)
    return Response(
        content=blob,
        media_type="application/pdf",
        headers=resp_headers,
    )


@router.get("/inventory/summary")
async def inventory_summary(
    scope: str = "admin",
    env: str = "dev",
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Lightweight inventory summary — totals + health, no per-project detail.

    Used by the Reports page to show a preview before the user downloads
    the full document. ~10x faster than the full collection because it
    skips per-pipeline enrichment.
    """
    normalized_scope = scope.lower()
    if normalized_scope == "admin" and getattr(user, "role", None) not in _ADMIN_ROLES:
        normalized_scope = "user"
    # Mode is binary now (dev|prod). Legacy 'all' downgrades to 'dev'.
    raw_env = env.lower()
    if raw_env == "all":
        raw_env = "dev"
    normalized_env = raw_env if raw_env in _ALLOWED_ENVS else "dev"
    # Apply the same governance enforcement the full-report endpoint uses,
    # so the preview reflects exactly what the download will contain.
    normalized_env = _enforce_env_access(user, normalized_env)
    tier = _detect_tier()

    from fpulse.main import app_state
    from fpulse.reports.inventory import InventoryCollector

    collector = InventoryCollector(
        app_state=app_state,
        caller=user if normalized_scope == "user" else None,
        scope=normalized_scope,
        workspace_id=workspace_id,
        tier=tier,
        env_filter=normalized_env,
    )
    report = collector.collect()
    audit = report.operational_audit
    return {
        "workspace_id": report.workspace_id,
        "workspace_name": report.workspace_name,
        "generated_at": report.generated_at,
        "generated_by": report.generated_by,
        "scope": report.scope,
        "tier": report.tier,
        "env_filter": report.env_filter,
        # Governance envelope — the UI uses this to disable the PROD
        # button and show a restriction note for DEV-locked users.
        "env_restrictions": {
            "can_see_prod": _can_see_prod(user),
            "role": getattr(user, "role", ""),
        },
        "fpulse_version": report.fpulse_version,
        "schema_version": report.schema_version,
        "totals": report.totals,
        "health": report.health,
        "project_count": len(report.projects),
        "connection_count": len(report.connections),
        "user_count": len(report.users),
        # Compact operational snapshot — preview only; the full
        # Operational Audit section lives in the downloaded report.
        "operational": {
            "window_hours": audit.window_hours,
            "total_executions": audit.total_executions,
            "successful_executions": audit.successful_executions,
            "failed_executions": audit.failed_executions,
            "success_rate_pct": audit.success_rate_pct,
            "recent_failure_count": len(audit.recent_failures),
            "upcoming_run_count": len(audit.next_runs),
            "next_run_at": audit.next_runs[0].next_fire_at if audit.next_runs else "",
        },
    }


# ═══════════════════════════════════════════════════════════════════════
# Built-in documentation endpoints — serves the six minimum docs to
# the Help page so users can read them without leaving the UI.
# ═══════════════════════════════════════════════════════════════════════


# Fixed catalogue: path (relative to repo docs/) → title + audience.
# Explicit allow-list so a user-supplied `path` query can never read
# arbitrary files off disk.
#
# Tier-gating (May 3 2026 fix):
#   - admin_only=True → only super_admin/admin/lead see in catalog + content
#   - plus_only=True  → only F-Pulse+ instances see at all (OSS Free hides
#     entirely; otherwise the OSS solo-developer admin would see Plus-only
#     content describing 5-tier RBAC / approval gates / DEV→PROD that don't
#     exist in their installation)
_DOC_CATALOG: list[dict] = [
    {
        "path": "README.md",
        "title": "Documentation Overview",
        "audience": "Everyone",
        "summary": "Entry point — map of every available document.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "quickstart.md",
        "title": "Quickstart",
        "audience": "All users",
        "summary": "Install, run your first pipeline in 5 minutes, set up AI.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "architecture.md",
        "title": "Architecture overview",
        "audience": "All users",
        "summary": "How F-Pulse fits together: IR-first, DuckDB execution, lifespan ordering, open-core boundary.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "editions.md",
        "title": "F-Pulse vs F-Pulse+",
        "audience": "All users",
        "summary": "Open-core boundary — what's free, what's paid, when to upgrade.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "user-guides/projects.md",
        "title": "Projects",
        "audience": "All users",
        "summary": "Create, manage, share, archive, and delete projects.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "user-guides/pipelines.md",
        "title": "Pipelines",
        "audience": "All users",
        "summary": "Build, test, validate, run, schedule, monitor, archive, clone, export.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "user-guides/connections.md",
        "title": "Connections",
        "audience": "All users",
        "summary": "Create, test, scope by project. Connector status badges (Certified / Beta).",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "user-guides/external-triggers.md",
        "title": "Triggering Pipelines from External Systems",
        "audience": "Integrators, developers, CI/CD owners",
        "summary": "Run pipelines from external apps via the authenticated API or public webhook, with parameter passing and declared inputs.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "storage.md",
        "title": "Storage — workspace data home",
        "audience": "All users",
        "summary": "Files, managed Parquet tables, and pipeline outputs. Upload with Global / Project / Folder scope. Replace bytes in place. Promote a file to a managed table. Usage tracking shows what would break before you delete.",
        "admin_only": False,
        "plus_only": False,
    },
    # Node reference is its own top-level Help tab — removed from the
    # Documentation sidebar to avoid the same content appearing twice
    # (May 6 2026, user-reported redundancy).
    {
        "path": "connectors.md",
        "title": "Connector catalog",
        "audience": "All users",
        "summary": "Connector depth scores. Certified, Beta, F-Pulse+ enterprise.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "connector-authoring.md",
        "title": "Author a connector with AI",
        "audience": "All users",
        "summary": "Generate a v2 manifest from an OpenAPI spec or sample API responses. Paste-and-go starter — no hand-written JSON.",
        "admin_only": False,
        "plus_only": False,
    },
    # 2026-05-29: extensibility doc set — surfaces the "you can build any
    # connector / node you need" path as a first-class story in Help, not
    # just a deep link buried in connector-authoring.md. Theme: OSS flips
    # the connector-gap weakness into a strength.
    {
        "path": "extend/build-a-connector.md",
        "title": "Build your own connector (30 min)",
        "audience": "All users",
        "summary": "Three first-class paths to add any connector F-Pulse doesn't ship: 90-second OpenAPI generator, 10-minute sample-response generator, or a 30-minute hand-authored manifest. End-to-end tutorial with the existing /api/connectors/author/* path.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "extend/build-a-node.md",
        "title": "Add a custom node",
        "audience": "Developers",
        "summary": "Try the SQL Transform (DuckDB) node first; for reusable custom logic, write a first-class node type (subclass + StepType enum + registered). When to pick which.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        # 2026-06-01: added to catalog. The 130 KB exhaustive node
        # reference is linked from extend/build-a-node.md (line: "see
        # ../nodes-and-canvas-reference.md") but was missing — clicks
        # surfaced "isn't part of the in-app documentation catalog".
        "path": "nodes-and-canvas-reference.md",
        "title": "Node + Canvas reference (exhaustive)",
        "audience": "Pipeline authors",
        "summary": "Every node type's input/output contract, every canvas affordance, every keyboard shortcut. The long-form reference behind the in-app palette + the Help → Node Reference tab.",
        "admin_only": False,
        "plus_only": False,
    },
    # 2026-06-05 — Steward (Archeologist 1.1). The OSS headline
    # differentiator: a read-only background reliability + learning
    # layer that every other open-source orchestrator lacks. Two docs
    # — `overview.md` for users (what + how to use) and
    # `architecture.md` for builders / reviewers (why + design
    # rationale). Surfaced under "Core Concepts" so evaluators land on
    # it next to architecture / editions, not buried elsewhere.
    {
        "path": "steward/overview.md",
        "title": "Steward — overview",
        "audience": "All users",
        "summary": "Read-only workspace observer. Active detectors today span architecture (duplicate-source + duplicate-pipeline), connector health, data (schema drift + quality + volume anomaly), node empty-output, cost warehouse-waste, and governance (env-crossing / unapproved-destination / PII-leak), plus user-defined rules — with time-clamped severity escalation, rebound state for regressions, dismiss-with-reason + reason-sanitizer for tribal-knowledge capture, a durable Memory Layer of human-approved lessons (10 lesson types), and notification-bell integration with strict (user, finding, severity, rebound) de-dup. The multi-level contract covers all finding kinds across 7 layers; the remaining structural / pipeline specialists (Sentinel 1.2, Foreseer / Cost Steward / Architecture Steward 1.3, Governor / Curator 1.4) land progressively. OSS-first, never paywalled.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "steward/architecture.md",
        "title": "Steward — architecture & design",
        "audience": "Contributors, evaluators, technical reviewers",
        "summary": "In-depth design reference. The three architectural bets, the five hard rules, the specialist-module decomposition, code walkthrough of detection + learning, the de-dup invariant in the notification bridge, the OSS vs Plus tier line, extension model, performance characteristics, reviewer concerns + mitigations.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "steward/memory-layer.md",
        "title": "F-Pulse Memory Layer",
        "audience": "All users",
        "summary": "Durable, human-approved lessons distilled from operator decisions. Ten lesson categories (source quirks, failure patterns, retry rules, cost anomalies, intentional duplicates, …) with explicit propose → approve → revalidate workflow. YAML on disk for hand-review. Drives the failure-recovery search when a pipeline fails.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "steward/positioning.md",
        "title": "Steward — positioning",
        "audience": "Evaluators, partners, recruiting",
        "summary": "60-second pitch. Three things F-Pulse Steward gives users that other orchestrators don't. The OSS-vs-Plus horizontal split. The TL;DR for buyers. Suitable for sharing with non-engineering audiences.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        # 2026-06-06 — Validation matrix. Pack v1 (12 named scenarios)
        # + every gap closed. Useful for QA + customer trust.
        "path": "steward/validation-scenarios.md",
        "title": "Steward - validation scenario matrix",
        "audience": "QA, contributors, technical evaluators",
        "summary": "Pack v1 of 12 named Given/When/Then scenarios covering duplicates, escalation, time-clamp, rebound, dismiss-with-reason, lesson lifecycle, notification de-dup, per-workspace isolation, corrupt-journal resilience, and master kill-switch. Every gap from the original reviewer list closed with named tests. 75 unit tests + 12 scenarios all green.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "vs-talend.md",
        "title": "Comparison with other orchestrators",
        "audience": "Evaluators",
        "summary": "Side-by-side comparison for teams evaluating F-Pulse against another orchestrator at single-machine workload scale. Honest about where each tool wins, when to pick which, and how the connector-gap question changes with an open extension framework.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        # 2026-06-01: added to catalog. Was referenced from vs-talend.md
        # (See also section, line 103) but missing here — clicking the link
        # showed "isn't part of the in-app documentation catalog".
        "path": "vs-airbyte.md",
        "title": "Comparison with other orchestrators (companion)",
        "audience": "Evaluators",
        "summary": "Companion side-by-side comparison for teams evaluating F-Pulse OSS against another orchestrator at single-machine workload scale. Honest about where each tool wins and when to pick which.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        # 2026-06-01: added to catalog. Referenced from README.md ("nodes.md")
        # but missing — clicking from Documentation surfaced the not-in-catalog
        # banner.
        "path": "nodes.md",
        "title": "Node reference",
        "audience": "Pipeline authors",
        "summary": "Per-node-type reference: what each node does, when to use it, configuration shape, and gotchas. Complements the in-app Node palette.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        # 2026-06-01: added to catalog. Referenced from connectors.md and
        # faq.md but missing — explains the idempotency-key + sink-dedupe
        # story that operators ask about when scheduling pipelines.
        "path": "idempotency.md",
        "title": "Idempotency & sink dedupe",
        "audience": "Operators",
        "summary": "How F-Pulse prevents duplicate side effects on retry / backfill: idempotency_key on sink steps, the dedupe store, and which sink types ship with safe defaults.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "ai.md",
        "title": "AI guide",
        "audience": "All users",
        "summary": "Local LLM (Ollama) setup, cloud providers, agent governance, troubleshooting.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "scaling.md",
        "title": "Vertical scaling guide",
        "audience": "Operators",
        "summary": "DuckDB tuning knobs, governor tiers, spill-disk health, reference configs (laptop / VPS / production).",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "dashboard-metrics.md",
        "title": "Dashboard metrics reference",
        "audience": "All users",
        "summary": "Every Dashboard KPI explained — formula, source endpoint, edge cases. Read this when a number surprises you.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "deployment.md",
        "title": "Deployment & upgrade runbook",
        "audience": "Operators",
        "summary": "Canonical install, three-component upgrade flow (F-Pulse / Ollama / models), backup, disaster recovery.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "security-deployment.md",
        "title": "Secure deployment guide",
        "audience": "Operators",
        "summary": "TLS termination, nginx/Caddy snippets, security-relevant env vars, master-key permissions, HSTS forwarding, CSP overrides for iframe embedding. Read before exposing F-Pulse beyond localhost.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "api.md",
        "title": "API reference",
        "audience": "Developers",
        "summary": "Every HTTP endpoint, request and response shapes, status codes.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "dev-guide.md",
        "title": "Developer guide",
        "audience": "Developers",
        "summary": "Architecture, conventions, how to extend F-Pulse with custom nodes.",
        "admin_only": False,
        "plus_only": False,
    },
    # NOTE: execution-architecture.md was catalogued here until May 9 2026,
    # then withdrawn because it misled OSS users — it read as a Plus-tier
    # enterprise workload pitch: it claimed process-per-pipeline isolation
    # (the not-yet-shipped F-Pulse+ Stage 5 design; OSS runs a THREAD pool),
    # and listed RBAC + audit trail + PROD env as included. The file itself
    # was deleted on 2026-07-16 — withdrawing it from this catalog had left
    # it readable in the repo, still asserting isolation guarantees the
    # engine does not provide ("Pipeline A can't crash Pipeline B" is not
    # true of threads). If the OSS execution model is documented again, it
    # must describe the thread pool that actually ships.
    {
        "path": "testing.md",
        "title": "Testing guide",
        "audience": "Developers",
        "summary": "How tests are organised and how to add new ones.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        # CHANGELOG.md lives at the repo root (one level above docs/) so the
        # content handler reads it via the `repo_root` flag below. Surfacing
        # it here gives the Help → Documentation tab a "what's new" entry.
        "path": "CHANGELOG.md",
        "title": "Release notes (CHANGELOG)",
        "audience": "All users",
        "summary": "Recent changes, version history, tested-with matrix.",
        "admin_only": False,
        "plus_only": False,
        "repo_root": True,
    },
    {
        "path": "eval-harness.md",
        "title": "AI eval harness",
        "audience": "All users",
        "summary": "Reproducible AI quality benchmark — 14 cases × 5 categories, deterministic judges, CLI.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "trust.md",
        "title": "Trust posture",
        "audience": "All users",
        "summary": "What we do and don't do with your data — sanitization, no-train flags, telemetry policy.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "ai-boundary-contract.md",
        "title": "AI boundary contract",
        "audience": "Developers",
        "summary": "10 architecture invariants the agent code must satisfy.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "performance.md",
        "title": "Performance budgets",
        "audience": "Operators",
        "summary": "Per-tool latency targets, perf regressions, hardware sizing.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "faq.md",
        "title": "FAQ",
        "audience": "All users",
        "summary": "Common questions: install, pipelines, connectors, AI, operations, privacy, contributing.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "supported-models.md",
        "title": "Supported AI models policy",
        "audience": "Operators",
        "summary": "Authoritative list of recommended local models per hardware tier; cloud opt-in policy; deprecated recommendations.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "compliance.md",
        "title": "Compliance one-pager",
        "audience": "Compliance reviewers",
        "summary": "Data residency, authn, cryptography, network posture — every claim links to a verifiable artifact.",
        "admin_only": False,
        "plus_only": False,
    },
    # 2026-06-02: install + operations + roadmap surface.
    # These shipped in this release and were missing from the Help →
    # Documentation tab catalog. Adding them so the in-app docs reader
    # is the complete entry point for everything user-facing in docs/.
    {
        "path": "install/security-hardening.md",
        "title": "Local security hardening",
        "audience": "Operators",
        "summary": "Loopback default, Host header allowlist + Origin pinning, opt-in LAN binding via FPULSE_ALLOW_LAN, dev-auth guard, exposed-on-LAN banner — the OSS-local security posture in one page.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "install/database-drivers.md",
        "title": "Database driver installation",
        "audience": "Operators",
        "summary": "Per-database `pip install fpulse[X]` matrix + OS-level requirements (Microsoft ODBC Driver, Oracle Instant Client, IBM DSDriver). Covers postgres, mysql, mssql, oracle, snowflake, bigquery, databricks, etc.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "run-as-service.md",
        "title": "Run as a service",
        "audience": "Operators",
        "summary": "Five paths to run F-Pulse beyond the terminal — `fpulse install-service` (Windows / macOS / Linux), packaged installers, Docker Compose, NSSM, system-wide systemd. Decision tree included.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "customer-faq.md",
        "title": "Customer / buyer FAQ",
        "audience": "Evaluators, buyers",
        "summary": "Buyer-facing Q&A — privacy posture, license terms, what's OSS vs Plus, what F-Pulse does NOT do, security disclosure path. Pairs with the operator FAQ.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "ai-ops-contract.md",
        "title": "AI operations contract",
        "audience": "Developers, Operators",
        "summary": "Operational invariants the AI layer must satisfy — provider isolation, audit logging, rate-limit policy, budget enforcement. Pairs with the AI boundary contract.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "releases/release-notes-1.0.md",
        "title": "Release notes — 1.0",
        "audience": "All users",
        "summary": "What shipped in F-Pulse OSS 1.0: launcher, tier system, hardening, REST framework upgrade, Avro/ORC readers, driver extras. Also the explicit \"what 1.0 deliberately does NOT promise\" section.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "roadmap/oss-1-1.md",
        "title": "Roadmap — OSS 1.1",
        "audience": "All users",
        "summary": "Items intentionally deferred from 1.0 — desktop packaging, Plus license implementation, Verified-tier connector candidates, connector SDK extraction, AI-Native pipeline construction, streaming connectors. Roadmap, not promise.",
        "admin_only": False,
        "plus_only": False,
    },
    {
        "path": "roadmap/reliability-sprint.md",
        "title": "Roadmap — reliability sprint",
        "audience": "All users",
        "summary": "The 4-6 week post-1.0 reliability workstream — first 10 Verified connectors, scheduler 30-day soak, incremental cursor end-to-end tests, retry/backoff coverage, alerting plumbing, 7-day VPS soak. Earned trust before AI-Native v1.1.",
        "admin_only": False,
        "plus_only": False,
    },
    # Plus-only docs — these describe features that don't exist in OSS Free
    # (5-tier RBAC, approval gates, sessions, IP restriction, audit log
    # retention, vault rotation). Hide them entirely on Free; admins on
    # Plus see them. Defence in depth: content endpoint also refuses on Free.
    {
        "path": "user-guides/users-and-access.md",
        "title": "Users & Access Control",
        "audience": "Administrators",
        "summary": "5-tier RBAC, environment permissions, approval gates, sessions, IP restriction.",
        "admin_only": True,
        "plus_only": True,
    },
    {
        "path": "admin/runbook.md",
        "title": "Administrator Runbook",
        "audience": "Operators, SREs",
        "summary": "Install, backup, retention, drift, observability, upgrade.",
        "admin_only": True,
        "plus_only": True,
    },
]


def _is_admin_like(user) -> bool:
    """Matches the Reports-side rule (_PROD_VIEWING_ROLES) so Documentation
    and Reports agree on who counts as an admin-like caller."""
    return getattr(user, "role", "") in _PROD_VIEWING_ROLES


def _docs_root() -> Path:
    """Resolve the docs/ directory next to the running backend."""
    # backend/fpulse/api/reports.py → backend/fpulse/api
    # → ../../.. = repo root; repo_root/docs.
    here = Path(__file__).resolve()
    repo_root = here.parents[3]   # fpulse-f-pulse/
    return repo_root / "docs"


@router.get("/docs/catalog")
async def list_docs(user=Depends(require_auth)):
    """List every markdown doc available via the Help page.

    Tier + role filtering (May 3 2026):
      - plus_only docs   → hidden on F-Pulse Free (entire installation lacks
                           the features they describe)
      - admin_only docs  → hidden for non-admin callers
    Both filters apply: a Plus-tier admin sees plus_only+admin_only docs;
    a Free admin sees neither.
    """
    is_admin = _is_admin_like(user)
    is_plus = _detect_tier() == "plus"
    visible = [
        d for d in _DOC_CATALOG
        if (is_admin or not d.get("admin_only", False))
        and (is_plus or not d.get("plus_only", False))
    ]
    return {"docs": visible}


@router.get("/docs/content")
async def get_doc_content(
    path: str = Query(..., description="Path from the catalog (e.g. 'user-guides/projects.md')"),
    user=Depends(require_auth),
):
    """Return raw markdown for one of the catalogued docs.

    Only paths listed in _DOC_CATALOG are servable — prevents any
    ../ traversal or arbitrary-file-read attack. Admin-only and plus-only
    docs are refused with 404 (not 403) for ineligible callers so the
    existence of those paths doesn't leak via status-code probing.
    """
    entry = next((d for d in _DOC_CATALOG if d["path"] == path), None)
    if entry is None:
        raise HTTPException(404, "Document not found in catalog")
    if entry.get("admin_only") and not _is_admin_like(user):
        raise HTTPException(404, "Document not found in catalog")
    if entry.get("plus_only") and _detect_tier() != "plus":
        raise HTTPException(404, "Document not found in catalog")

    # Most catalog entries live under docs/. A small allow-list of repo-root
    # files (e.g. CHANGELOG.md) is opted in via `repo_root: True` so they
    # can be served alongside the docs/ tree without losing the traversal
    # guard — the resolved path is still constrained to its declared root.
    root = _docs_root().parent if entry.get("repo_root") else _docs_root()
    target = root / path
    # Belt and braces — resolve and verify the file is inside the
    # entry-declared root.
    try:
        resolved = target.resolve()
        if not str(resolved).startswith(str(root.resolve())):
            raise HTTPException(404, "Document not found")
    except (OSError, RuntimeError):
        raise HTTPException(404, "Document not found")

    if not resolved.is_file():
        raise HTTPException(404, "Document file missing on disk")

    try:
        content = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        logger.exception("Failed to read doc %s", path)
        raise HTTPException(500, "Failed to read document") from exc

    return PlainTextResponse(content, media_type="text/markdown; charset=utf-8")
