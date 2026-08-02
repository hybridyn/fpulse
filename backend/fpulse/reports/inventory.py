"""System Inventory data collector.

Single-pass traversal of every live store in the F-Pulse backend to
assemble a structured `InventoryReport` that the docx/pdf renderers
then turn into a beautiful document.

Design rules:
  - Bounded: every per-pipeline sublist (executions, lifecycle) is
    capped so a pipeline with 100k runs doesn't blow up the report.
  - ACL-aware: `scope="user"` runs the same project visibility rule
    as api/projects.py (_can_see) so developers get a filtered report.
  - Pure read: never writes, never triggers side effects. Safe to
    call as often as desired.
  - Single pass: every store is listed at most once; per-pipeline
    rollups use the already-loaded lists.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("fpulse.reports.inventory")

# ── Bounded list caps (report-size discipline) ───────────────────────
MAX_EXECUTIONS_PER_PIPELINE = 5       # last N runs shown per pipeline
MAX_LIFECYCLE_PER_PIPELINE = 5        # last N lifecycle events
MAX_NODE_DETAIL_PER_PIPELINE = 20     # truncate huge pipeline DAGs
MAX_PIPELINES_PER_PROJECT_DETAIL = 50 # above this, rollup only


# Roles that bypass the project ACL (admins see everything).
_ADMIN_ROLES = {"super_admin", "admin"}


# ═══════════════════════════════════════════════════════════════════════
# Data model — what the report actually holds
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class PipelineInventory:
    id: str
    name: str
    description: str
    project_id: str
    status: str
    deployed_version: int | None
    latest_version: int
    owner: str
    approval_status: str
    submitted_by: str
    approved_by: str
    step_count: int
    connection_count: int
    node_types: list[str] = field(default_factory=list)
    connections_used: list[dict] = field(default_factory=list)     # [{id,name,type}, ...]
    schedules: list[dict] = field(default_factory=list)            # [{cron, enabled, next_fire}]
    alert_rules: list[dict] = field(default_factory=list)          # [{name, condition, channels}]
    last_runs: list[dict] = field(default_factory=list)            # last N executions summary
    lifecycle: list[dict] = field(default_factory=list)            # last N lifecycle events
    content_hash: str = ""
    tags: list[str] = field(default_factory=list)
    # ── Documentation (self-documenting pipelines) — the "what & why" ──
    # Folded into the inventory report so a reader gets the pipeline's
    # stated purpose (and any README) alongside its structure, without
    # opening the editor. business_purpose is the one-line WHY that the
    # publish gate requires; readme is freeform Markdown notes.
    business_purpose: str = ""
    readme: str = ""
    # ── Prominent operational signals (gap 4) ─────────────────────────
    # Derived from last_runs[0] and schedules[0] so renderers can show a
    # coloured pill at the top of each pipeline block instead of forcing
    # the reader to scan a table to find the last-run status.
    last_run_status: str = ""        # "success" | "error" | "timeout" | "cancelled" | ""
    last_run_at: str = ""            # ISO timestamp of most recent execution
    next_run_at: str = ""            # ISO timestamp of next scheduled fire (if scheduled)
    # ── Environment bucketing (gap 1) ─────────────────────────────────
    # A pipeline always exists in DEV; a pipeline with deployed_version
    # set is ALSO live in PROD. Rendering this as a badge makes the
    # DEV/PROD split — F-Pulse's differentiator — visually obvious.
    environments: list[str] = field(default_factory=list)  # ["DEV"] or ["DEV", "PROD"]


@dataclass
class ProjectInventory:
    id: str
    name: str
    description: str
    owner: str
    owner_id: str
    approval_status: str
    color: str
    icon: str
    members: list[str] = field(default_factory=list)       # user IDs
    member_names: list[str] = field(default_factory=list)  # resolved display names
    approver: str = ""                                      # resolved from approval gate
    approvers: list[str] = field(default_factory=list)     # full list from gate
    pipeline_count: int = 0
    pipelines: list[PipelineInventory] = field(default_factory=list)
    connection_count: int = 0
    created_at: str = ""


@dataclass
class UserInventory:
    id: str
    email: str
    name: str
    role: str
    is_active: bool
    projects_allowed: list[str] = field(default_factory=list)
    last_login_at: str = ""
    prod_permissions: dict = field(default_factory=dict)


@dataclass
class ConnectionInventory:
    id: str
    name: str
    type: str
    project_id: str
    environment: str
    capabilities: list[str] = field(default_factory=list)
    has_credential_ref: bool = False          # True = in Vault
    has_inline_creds: bool = False            # True = legacy, migrate candidate
    used_by_pipelines: list[str] = field(default_factory=list)  # pipeline IDs
    # ── Redaction marker (gap 2) ──────────────────────────────────────
    # The raw Vault ID is safe to show (it's a pointer, not a secret) —
    # renderers use this to display "Credentials: [Vault: cred_xxx]
    # (redacted)" so a reader knows the document is safe to share.
    credential_ref: str = ""                  # masked vault ID (e.g. "cred_ab***xy")


@dataclass
class ScheduleInventory:
    id: str
    workflow_id: str
    workflow_name: str
    cron_expression: str
    enabled: bool
    timezone: str
    next_fire_at: str = ""
    environment: str = "DEV"


@dataclass
class AlertInventory:
    id: str
    workflow_id: str
    workflow_name: str
    name: str
    condition: str            # human description
    enabled: bool
    channels: list[str] = field(default_factory=list)      # slack, email, webhook


@dataclass
class ApprovalGateInventory:
    id: str
    scope: str                # pipeline | project | global
    scope_id: str
    enabled: bool
    min_approvals: int
    approvers: list[str]
    notify_channels: list[str]


# ── Operational Audit (gap 3) ────────────────────────────────────────
# Review 2 requested a dedicated ops-focused section. Computed at the
# end of collect() from data already loaded — one pass, no extra queries.


@dataclass
class NextScheduledRun:
    workflow_id: str
    workflow_name: str
    cron_expression: str
    environment: str
    next_fire_at: str


@dataclass
class RecentFailure:
    workflow_id: str
    workflow_name: str
    failed_at: str
    error: str


@dataclass
class OperationalAudit:
    window_hours: int = 24
    total_executions: int = 0
    successful_executions: int = 0
    failed_executions: int = 0
    success_rate_pct: float = 0.0
    avg_duration_ms: int = 0
    # List of up to the next 10 scheduled firings across the workspace.
    next_runs: list[NextScheduledRun] = field(default_factory=list)
    # Pipelines whose most recent execution failed (ordered by time).
    recent_failures: list[RecentFailure] = field(default_factory=list)


@dataclass
class FailingPipelineRollup:
    """One row in the failure-analysis table."""
    pipeline_id: str
    pipeline_name: str
    failure_count: int
    last_failure_at: str
    last_error: str
    failure_rate_pct: float = 0.0  # failed / total runs in window
    total_runs: int = 0


@dataclass
class ErrorPatternRollup:
    """One row in the 'most common errors' table — error message → count."""
    error_signature: str       # short normalized form of the error
    count: int
    affected_pipelines: list[str] = field(default_factory=list)


@dataclass
class FailureAnalysis:
    """30-day failure analysis section. Computed from execution_log."""
    window_days: int = 30
    total_failures: int = 0
    unique_failing_pipelines: int = 0
    top_failing: list[FailingPipelineRollup] = field(default_factory=list)
    top_errors: list[ErrorPatternRollup] = field(default_factory=list)
    # Per-day failure count for the trend mini-chart in the renderer.
    failures_by_day: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PipelineDurationRow:
    pipeline_id: str
    pipeline_name: str
    runs: int
    avg_ms: int
    p95_ms: int
    last_ms: int
    # True when the most-recent run is >= 1.5× the rolling average
    # (a soft "slower than usual" hint — same data the bot's
    # diagnose path could surface).
    regression: bool = False


@dataclass
class DurationAnalysis:
    window_days: int = 30
    total_pipelines: int = 0
    total_runs: int = 0
    rows: list[PipelineDurationRow] = field(default_factory=list)
    slowest_pipeline: str = ""        # name of pipeline with highest p95
    regressions_count: int = 0


@dataclass
class InsightItem:
    """One actionable insight rendered at the top of the report."""
    severity: str            # "critical" | "warning" | "info" | "ok"
    icon: str                # emoji or short symbol
    headline: str            # one-line summary (rendered prominently)
    detail: str = ""         # optional extra sentence
    pipeline_ids: list[str] = field(default_factory=list)


@dataclass
class InsightsHeadline:
    """Operational headline at the top of the inventory report:
    'what should the reader fix first?'. Computed from data already
    collected — no extra queries. Counts are over deployed/published
    pipelines only (matches the report-scope filter applied earlier)."""

    failing_count: int = 0
    healthy_count: int = 0
    stale_count: int = 0       # no run in last 7 days
    items: list[InsightItem] = field(default_factory=list)
    top_action: str = ""       # one-line prescription for the reader


@dataclass
class InventoryReport:
    """The root object passed to renderers."""

    # Header
    generated_at: str
    generated_by: str
    scope: str                          # "admin" or "user"
    workspace_id: str
    workspace_name: str
    fpulse_version: str
    schema_version: int
    # Tier & environment filter (free vs plus; dev/prod/all).
    # Free-tier renderers skip Users + Approval Gates sections,
    # show an Upgrade CTA, and use lighter branding. env_filter="all"
    # keeps every pipeline/connection; "dev" keeps DEV-scoped only;
    # "prod" keeps PROD-deployed only.
    tier: str = "plus"                  # "plus" | "free"
    env_filter: str = "all"             # "all" | "dev" | "prod"

    # Rollups
    totals: dict[str, int] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)

    # Detail sections
    projects: list[ProjectInventory] = field(default_factory=list)
    connections: list[ConnectionInventory] = field(default_factory=list)
    users: list[UserInventory] = field(default_factory=list)
    schedules: list[ScheduleInventory] = field(default_factory=list)
    alerts: list[AlertInventory] = field(default_factory=list)
    approval_gates: list[ApprovalGateInventory] = field(default_factory=list)
    operational_audit: OperationalAudit = field(default_factory=OperationalAudit)
    # "What should I fix first?" — computed last, from data already in
    # `projects`/`pipelines`. Empty in fail-safe mode if computation
    # fails so the report always renders.
    insights: InsightsHeadline = field(default_factory=InsightsHeadline)
    # 30-day failure analysis (May 6 2026). Top failing pipelines +
    # most common error patterns + per-day trend.
    failure_analysis: FailureAnalysis = field(default_factory=FailureAnalysis)
    # 30-day duration analysis (May 6 2026). Avg / p95 per pipeline,
    # plus a regression flag when the latest run is much slower than usual.
    duration_analysis: DurationAnalysis = field(default_factory=DurationAnalysis)
    # PROD-mode-only sections (Apr 27 2026 — DEV/PROD report split).
    # Empty in DEV mode; populated in PROD mode for compliance/audit.
    sandbox_runs: list[dict[str, Any]] = field(default_factory=list)
    lifecycle_requests: list[dict[str, Any]] = field(default_factory=list)
    pool_allocation: dict[str, Any] = field(default_factory=dict)
    license_summary: dict[str, Any] = field(default_factory=dict)
    # 2026-06-05 — Steward findings rollup. Ships in OSS (not gated).
    # Lets a downloaded inventory PDF show "your Steward currently sees
    # N findings (P1: x, P2: y, P3: z)" so reviewers / auditors have a
    # snapshot they can attach to change-management tickets. Detection
    # runs in-process at collect-time; no separate request needed.
    steward_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialisable dict — used by the JSON export mode and tests."""
        from dataclasses import asdict
        return asdict(self)


# ═══════════════════════════════════════════════════════════════════════
# Collector
# ═══════════════════════════════════════════════════════════════════════


class InventoryCollector:
    """Single-shot collector. Construct once, call .collect() once.

    Never mutates state. Safe to run concurrently with normal traffic.
    """

    def __init__(self, app_state: dict, *, caller=None, scope: str = "admin",
                 workspace_id: str = "default", tier: str = "plus",
                 env_filter: str = "all", scope_id_filter: str | None = None):
        self._state = app_state
        self._caller = caller                # User object if scope="user"
        self._scope = scope
        self._workspace_id = workspace_id
        # Tier controls which sections render; env_filter controls which
        # pipelines + connections appear in the Projects/Connections
        # sections (but NOT in the totals — totals always reflect the
        # full workspace so the reader understands what was excluded).
        self._tier = tier if tier in ("plus", "free") else "plus"
        self._env_filter = env_filter if env_filter in ("all", "dev", "prod") else "all"
        # Apr 27 2026: scope_id_filter — narrows the report to a single
        # project_id / pipeline_id / user_id when scope is project/pipeline/user.
        # Honored downstream by the project + pipeline collectors. None = unfiltered.
        self._scope_id_filter = scope_id_filter
        # Lazy caches populated in .collect()
        self._users_by_id: dict[str, dict] = {}
        self._connections_by_id: dict[str, dict] = {}

    # ── Main entry point ─────────────────────────────────────────────

    def collect(self) -> InventoryReport:
        """Run the single-pass traversal and return the report."""
        report = InventoryReport(
            generated_at=datetime.now(timezone.utc).isoformat(),
            generated_by=(getattr(self._caller, "email", "system")
                          if self._caller else "system"),
            scope=self._scope,
            workspace_id=self._workspace_id,
            workspace_name=self._workspace_name(),
            fpulse_version=self._version(),
            schema_version=self._schema_version(),
            tier=self._tier,
            env_filter=self._env_filter,
        )

        # Order matters: users/connections first so project/pipeline
        # sections can enrich with names instead of bare IDs.
        # Users are skipped on free tier — it's single-user or has
        # no user-management surface to describe.
        if self._tier != "free":
            self._collect_users(report)
        self._collect_connections(report)
        # ── Mode-gated sections (Apr 27 2026 split) ──
        # PROD reports are operate-phase / audit-phase: deployed
        # inventory, approval audit trail, sandbox evidence, lifecycle
        # decisions, pool allocation history, license + seats.
        # DEV reports are build-phase: drafts, dev runs, personal stats.
        # Approval gates are part of the PROD audit story — skip them in
        # DEV mode AND on free tier (no approvals exist there either).
        is_prod_mode = self._env_filter == "prod"
        if is_prod_mode and self._tier != "free":
            self._collect_approval_gates(report)
            # PROD-mode rich sections (Apr 27 2026)
            self._collect_sandbox_runs(report)
            self._collect_lifecycle_requests(report)
            self._collect_pool_allocation(report)
            self._collect_license_summary(report)
        self._collect_schedules(report)
        self._collect_alerts(report)
        self._collect_projects_and_pipelines(report)
        self._apply_env_filter(report)
        self._backfill_connection_usage(report)
        # Narrow ancillary sections (schedules / alerts / connections /
        # approval_gates / users) to surviving entities when scope is
        # 'project' or 'pipeline'. Projects + workflows were already
        # narrowed inside _collect_projects_and_pipelines; this pass
        # propagates the narrowing to the rest of the report so a
        # pipeline-scope report doesn't render the whole workspace's
        # schedules and connections.
        self._apply_scope_id_filter_post_collect(report)
        self._compute_operational_audit(report)
        self._compute_totals(report)
        self._compute_health(report)
        # ── "What should I fix first?" headline (May 6 2026) ─────────
        # Computed last so it can lean on every other section being
        # populated. Wrapped in try/except so a bug here can't break
        # an otherwise-valid report.
        try:
            self._compute_insights(report)
        except Exception as exc:  # noqa: BLE001
            logger.warning("inventory: insights computation failed: %s", exc)
        # ── 30-day failure analysis (May 6 2026) ─────────────────────
        try:
            self._compute_failure_analysis(report)
        except Exception as exc:  # noqa: BLE001
            logger.warning("inventory: failure analysis failed: %s", exc)
        # ── 30-day duration analysis (May 6 2026) ────────────────────
        try:
            self._compute_duration_analysis(report)
        except Exception as exc:  # noqa: BLE001
            logger.warning("inventory: duration analysis failed: %s", exc)
        # ── Steward findings snapshot (2026-06-05) ───────────────────
        # Includes a count + breakdown of currently-open Steward
        # findings so the downloaded inventory PDF/DOCX is a
        # self-contained reliability snapshot at report time. Always
        # wrapped in try/except — the inventory must render even if
        # the Steward subsystem is misconfigured.
        try:
            self._compute_steward_summary(report)
        except Exception as exc:  # noqa: BLE001
            logger.warning("inventory: steward summary failed: %s", exc)
        return report

    # ── PROD-mode rich sections (Apr 27 2026 — DEV/PROD split) ─────────

    def _collect_sandbox_runs(self, report: InventoryReport) -> None:
        """Last 30 days of sandbox runs — auditor evidence for Gate 2 approvals."""
        try:
            db = self._state.get("db")
            if db is None:
                return
            with db.connect() as conn:
                cur = conn.execute(
                    "SELECT id, approval_id, workflow_id, scratch_namespace, "
                    "status, triggered_by, triggered_at, finished_at, row_limit "
                    "FROM sandbox_runs ORDER BY triggered_at DESC LIMIT 200"
                )
                rows = cur.fetchall()
            for r in rows:
                d = dict(r) if hasattr(r, "keys") else dict(zip(
                    ["id", "approval_id", "workflow_id", "scratch_namespace",
                     "status", "triggered_by", "triggered_at", "finished_at",
                     "row_limit"], r,
                ))
                report.sandbox_runs.append(d)
        except Exception as exc:
            logger.warning("inventory: sandbox_runs scan failed: %s", exc)

    def _collect_lifecycle_requests(self, report: InventoryReport) -> None:
        """All lifecycle (activate/deactivate) requests — audit trail."""
        try:
            db = self._state.get("db")
            if db is None:
                return
            with db.connect() as conn:
                cur = conn.execute(
                    "SELECT id, workflow_id, action, target_env, requested_by, "
                    "requested_at, reason, status, decided_by, decided_at, "
                    "decision_notes FROM lifecycle_toggle_requests "
                    "WHERE workspace_id = ? ORDER BY requested_at DESC LIMIT 200",
                    (self._workspace_id,),
                )
                rows = cur.fetchall()
            for r in rows:
                d = dict(r) if hasattr(r, "keys") else dict(zip(
                    ["id", "workflow_id", "action", "target_env", "requested_by",
                     "requested_at", "reason", "status", "decided_by",
                     "decided_at", "decision_notes"], r,
                ))
                report.lifecycle_requests.append(d)
        except Exception as exc:
            logger.warning("inventory: lifecycle_requests scan failed: %s", exc)

    def _collect_pool_allocation(self, report: InventoryReport) -> None:
        """Current pool allocation snapshot — capacity + last-changed evidence."""
        try:
            db = self._state.get("db")
            if db is None:
                return
            with db.connect() as conn:
                cur = conn.execute(
                    "SELECT prod_reserved_pct, dev_reserved_pct, burst_pct, "
                    "updated_at, updated_by FROM pool_allocations "
                    "WHERE workspace_id = ?",
                    (self._workspace_id,),
                )
                row = cur.fetchone()
            if row:
                report.pool_allocation = dict(row) if hasattr(row, "keys") else {
                    "prod_reserved_pct": row[0], "dev_reserved_pct": row[1],
                    "burst_pct": row[2], "updated_at": row[3], "updated_by": row[4],
                }
            else:
                # Default 60/20/20 — same as the runtime fallback.
                report.pool_allocation = {
                    "prod_reserved_pct": 60, "dev_reserved_pct": 20,
                    "burst_pct": 20, "updated_at": "", "updated_by": "(default)",
                }
        except Exception as exc:
            logger.warning("inventory: pool_allocation scan failed: %s", exc)

    def _collect_license_summary(self, report: InventoryReport) -> None:
        """License + seat summary for procurement / compliance."""
        try:
            lm = self._state.get("license_manager")
            if not lm:
                return
            report.license_summary = {
                "is_plus": bool(getattr(lm, "is_plus", False)),
                "tier": "plus" if getattr(lm, "is_plus", False) else "free",
                "seats": getattr(lm, "seats", None),
                "org": getattr(lm, "org", None),
                "expires_at": getattr(lm, "expires_at", None),
            }
        except Exception as exc:
            logger.warning("inventory: license_summary scan failed: %s", exc)

    # ── Environment filter (gap 1 + free-tier support) ──────────────

    def _apply_env_filter(self, report: InventoryReport) -> None:
        """Prune projects/pipelines/connections according to env_filter.

        Rules:
          all  — no filtering
          dev  — show every pipeline (every pipeline exists in DEV);
                 show DEV connections and shared connections (env="")
          prod — show only pipelines with deployed_version set;
                 show PROD connections and shared connections
        """
        if self._env_filter == "all":
            return

        if self._env_filter == "dev":
            # Keep every pipeline (all live in DEV); just drop PROD
            # from the environments tag for display consistency.
            for proj in report.projects:
                for p in proj.pipelines:
                    p.environments = [e for e in p.environments if e == "DEV"] or ["DEV"]
            report.connections = [
                c for c in report.connections
                if (c.environment or "").upper() in ("", "DEV")
            ]
            return

        # env_filter == "prod"
        for proj in report.projects:
            proj.pipelines = [p for p in proj.pipelines if "PROD" in p.environments]
            proj.pipeline_count = len(proj.pipelines)
        # Drop projects that have zero pipelines after filtering — but
        # keep projects that had pipelines before (so the reader sees
        # the workspace shape, not an empty report).
        report.projects = [p for p in report.projects if p.pipelines]
        report.connections = [
            c for c in report.connections
            if (c.environment or "").upper() in ("", "PROD")
        ]

    # ── Subcollectors ────────────────────────────────────────────────

    def _collect_users(self, report: InventoryReport) -> None:
        store = self._state.get("user_store")
        if not store:
            return
        try:
            rows = store.list_users() or []
        except Exception as exc:
            logger.warning("inventory: user_store.list_users failed: %s", exc)
            return

        for r in rows:
            if isinstance(r, dict):
                data = r
            else:
                # pydantic model
                try:
                    data = r.model_dump(mode="json")
                except Exception:
                    data = dict(getattr(r, "__dict__", {}))

            uid = data.get("id") or ""
            if not uid:
                continue

            self._users_by_id[uid] = data

            report.users.append(UserInventory(
                id=uid,
                email=data.get("email", ""),
                name=data.get("name", "") or data.get("email", ""),
                role=data.get("role", "viewer"),
                is_active=bool(data.get("is_active", True)),
                projects_allowed=data.get("projects", []) or [],
                last_login_at=data.get("last_login_at", "") or "",
                prod_permissions=data.get("prod_permissions", {}) or {},
            ))

    def _collect_connections(self, report: InventoryReport) -> None:
        store = self._state.get("connection_store")
        if not store:
            return
        try:
            rows = store.list_all(workspace_id=self._workspace_id) or []
        except Exception as exc:
            logger.warning("inventory: connection_store.list_all failed: %s", exc)
            return

        # Apply ACL for user scope — connections without a project are
        # globally visible; project-scoped connections require project
        # visibility.
        visible_project_ids = self._visible_project_ids()

        for r in rows:
            data = r if isinstance(r, dict) else self._model_to_dict(r)
            cid = data.get("id", "")
            if not cid:
                continue

            proj_id = data.get("project_id") or ""
            if self._scope == "user" and proj_id and proj_id not in visible_project_ids:
                continue

            # Detect inline-creds: credential_id missing/empty AND config
            # contains secret-looking keys.
            has_ref = bool(data.get("credential_id") or data.get("credentials_ref"))
            config = data.get("config") or {}
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except (ValueError, TypeError):
                    config = {}
            has_inline = (not has_ref) and self._has_any_secret_key(config)

            # Vault pointer — safe to show (it's a reference, not the secret
            # itself). We mask the middle so even the pointer doesn't leak
            # cleanly if the report is shared widely.
            raw_ref = (
                data.get("credential_id")
                or data.get("credentials_ref")
                or ""
            )
            masked_ref = _mask_ref(raw_ref) if raw_ref else ""

            conn_inv = ConnectionInventory(
                id=cid,
                name=data.get("name", cid),
                type=data.get("type", "?"),
                project_id=proj_id,
                environment=data.get("environment", "") or "",
                capabilities=data.get("capabilities", []) or [],
                has_credential_ref=has_ref,
                has_inline_creds=has_inline,
                credential_ref=masked_ref,
            )
            self._connections_by_id[cid] = {
                "name": conn_inv.name, "type": conn_inv.type,
                "ref": conn_inv,
            }
            report.connections.append(conn_inv)

    def _collect_approval_gates(self, report: InventoryReport) -> None:
        db = self._state.get("db")
        if not db:
            return
        try:
            cursor = db.execute_with_retry(
                "SELECT id, scope, scope_id, enabled, min_approvals, "
                "approvers, notify_channels FROM approval_gates "
                "WHERE workspace_id = ?",
                [self._workspace_id],
            )
            rows = cursor.fetchall() if cursor else []
        except Exception as exc:
            logger.warning("inventory: approval_gates scan failed: %s", exc)
            return

        for r in rows:
            try:
                approvers = json.loads(r[5] or "[]")
                channels = json.loads(r[6] or "[]")
            except (ValueError, TypeError):
                approvers, channels = [], []
            report.approval_gates.append(ApprovalGateInventory(
                id=r[0], scope=r[1], scope_id=r[2],
                enabled=bool(r[3]), min_approvals=r[4],
                approvers=approvers, notify_channels=channels,
            ))

    def _collect_schedules(self, report: InventoryReport) -> None:
        store = self._state.get("schedule_store")
        wf_store = self._state.get("store")
        if not store:
            return
        try:
            rows = store.list_all(workspace_id=self._workspace_id) or []
        except Exception as exc:
            logger.warning("inventory: schedule_store.list_all failed: %s", exc)
            return

        visible_wf_ids = self._visible_workflow_ids(wf_store) if self._scope == "user" else None

        for r in rows:
            data = r if isinstance(r, dict) else self._model_to_dict(r)
            wf_id = data.get("workflow_id", "")
            if visible_wf_ids is not None and wf_id not in visible_wf_ids:
                continue
            report.schedules.append(ScheduleInventory(
                id=data.get("id", ""),
                workflow_id=wf_id,
                workflow_name=self._workflow_name(wf_store, wf_id),
                cron_expression=data.get("cron_expression", ""),
                enabled=bool(data.get("enabled", True)),
                timezone=data.get("timezone", "UTC"),
                next_fire_at=data.get("next_fire_at", "") or "",
                environment=data.get("environment", "DEV") or "DEV",
            ))

    def _collect_alerts(self, report: InventoryReport) -> None:
        store = self._state.get("alert_store")
        wf_store = self._state.get("store")
        if not store:
            return
        try:
            rows = store.list_rules(workspace_id=self._workspace_id) or []
        except Exception as exc:
            logger.warning("inventory: alert_store.list_rules failed: %s", exc)
            return

        visible_wf_ids = self._visible_workflow_ids(wf_store) if self._scope == "user" else None

        for r in rows:
            data = r if isinstance(r, dict) else self._model_to_dict(r)
            wf_id = data.get("workflow_id", "") or ""
            if visible_wf_ids is not None and wf_id and wf_id not in visible_wf_ids:
                continue

            cond = data.get("condition", "") or self._format_alert_condition(data)
            report.alerts.append(AlertInventory(
                id=data.get("id", ""),
                workflow_id=wf_id,
                workflow_name=self._workflow_name(wf_store, wf_id),
                name=data.get("name", "(unnamed)"),
                condition=cond,
                enabled=bool(data.get("enabled", True)),
                channels=data.get("channels", []) or data.get("notify_channels", []) or [],
            ))

    def _collect_projects_and_pipelines(self, report: InventoryReport) -> None:
        proj_store = self._state.get("project_store")
        wf_store = self._state.get("store")
        if not proj_store or not wf_store:
            return

        # Load all projects & all workflows once.
        try:
            projects = proj_store.list_all(workspace_id=self._workspace_id) or []
        except Exception as exc:
            logger.warning("inventory: project_store.list_all failed: %s", exc)
            projects = []
        try:
            workflows = wf_store.list_all(workspace_id=self._workspace_id) or []
        except Exception as exc:
            logger.warning("inventory: workflow_store.list_all failed: %s", exc)
            workflows = []

        # May 6 2026 — reports show ONLY deployed or published pipelines.
        # Drafts and archived rows are noise in a governance/audit doc;
        # what matters is what's actually live or has been promoted.
        # A pipeline qualifies if ANY of these is true:
        #   * status is "published" / "testing" / "active"
        #   * deployed_version is set (means it shipped to PROD at least once)
        #   * published_at is set (means it cleared a publish action)
        def _is_publishable(w: dict) -> bool:
            status_raw = w.get("status") if isinstance(w, dict) else getattr(w, "status", None)
            status = ""
            if isinstance(status_raw, str):
                status = status_raw.lower()
            elif status_raw is not None:
                status = str(getattr(status_raw, "value", status_raw)).lower()
            if status in ("published", "testing", "active", "deployed"):
                return True
            getter = (lambda k: w.get(k)) if isinstance(w, dict) else (lambda k: getattr(w, k, None))
            if getter("deployed_version") not in (None, 0):
                return True
            if getter("published_at"):
                return True
            return False

        before_count = len(workflows)
        workflows = [w for w in workflows if _is_publishable(w)]
        logger.info(
            "inventory: %d/%d workflows kept after deployed/published filter",
            len(workflows), before_count,
        )

        # Apr 27 2026 — apply scope_id_filter narrowing.
        # 'project' scope: keep only the picked project + its pipelines.
        # 'pipeline' scope: keep only the picked pipeline + its parent project.
        # Other scopes ignore the filter (legacy behavior).
        if self._scope_id_filter:
            sid = self._scope_id_filter
            if self._scope == "project":
                projects = [p for p in projects if (
                    (p.get("id") if isinstance(p, dict) else getattr(p, "id", "")) == sid
                )]
                workflows = [w for w in workflows if (
                    (w.get("project_id") if isinstance(w, dict) else getattr(w, "project_id", "")) == sid
                )]
            elif self._scope == "pipeline":
                workflows = [w for w in workflows if (
                    (w.get("id") if isinstance(w, dict) else getattr(w, "id", "")) == sid
                )]
                # Keep only the parent project of the surviving pipeline.
                if workflows:
                    parent_pid = (workflows[0].get("project_id") if isinstance(workflows[0], dict)
                                  else getattr(workflows[0], "project_id", "default")) or "default"
                    projects = [p for p in projects if (
                        (p.get("id") if isinstance(p, dict) else getattr(p, "id", "")) == parent_pid
                    )]
                else:
                    projects = []

        # Bucket workflows by project_id for O(1) lookup.
        wfs_by_project: dict[str, list[dict]] = {}
        for w in workflows:
            pid = (w.get("project_id") if isinstance(w, dict)
                   else getattr(w, "project_id", "default")) or "default"
            wfs_by_project.setdefault(pid, []).append(
                w if isinstance(w, dict) else self._model_to_dict(w)
            )

        # Index approval gates by scope for quick resolution.
        gates_by_scope: dict[tuple[str, str], ApprovalGateInventory] = {}
        for g in report.approval_gates:
            gates_by_scope[(g.scope, g.scope_id)] = g
        global_gate = gates_by_scope.get(("global", ""))

        # Schedules/alerts by workflow_id for fast rollup.
        schedules_by_wf: dict[str, list[dict]] = {}
        for s in report.schedules:
            schedules_by_wf.setdefault(s.workflow_id, []).append({
                "cron": s.cron_expression, "enabled": s.enabled,
                "timezone": s.timezone, "next_fire": s.next_fire_at,
                "environment": s.environment,
            })
        alerts_by_wf: dict[str, list[dict]] = {}
        for a in report.alerts:
            alerts_by_wf.setdefault(a.workflow_id, []).append({
                "name": a.name, "condition": a.condition,
                "enabled": a.enabled, "channels": a.channels,
            })

        for p in projects:
            pdict = p if isinstance(p, dict) else self._model_to_dict(p)
            if self._scope == "user" and not self._can_see_project(pdict):
                continue

            approver_name, approvers = self._resolve_project_approver(
                pdict["id"], gates_by_scope, global_gate,
            )

            proj_inv = ProjectInventory(
                id=pdict.get("id", ""),
                name=pdict.get("name", ""),
                description=pdict.get("description", "") or "",
                owner=pdict.get("owner", "") or "",
                owner_id=pdict.get("owner_id", "") or "",
                approval_status=pdict.get("approval_status", "none") or "none",
                color=pdict.get("color", "") or "",
                icon=pdict.get("icon", "") or "",
                members=pdict.get("members", []) or [],
                approver=approver_name,
                approvers=approvers,
                created_at=pdict.get("created_at", "") or "",
            )

            # Resolve member display names.
            proj_inv.member_names = [
                self._user_display_name(mid) for mid in proj_inv.members
            ]

            # Enrich pipelines belonging to this project.
            wf_list = wfs_by_project.get(pdict["id"], [])
            proj_inv.pipeline_count = len(wf_list)

            # Scope the pipelines we expand in detail; over MAX we only rollup.
            detail_slice = wf_list[:MAX_PIPELINES_PER_PROJECT_DETAIL]
            for w in detail_slice:
                p_inv = self._build_pipeline_inventory(
                    w, wf_store, schedules_by_wf, alerts_by_wf,
                )
                proj_inv.pipelines.append(p_inv)

            # Per-project connection count (project-scoped conns + globals used).
            proj_inv.connection_count = sum(
                1 for c in report.connections
                if not c.project_id or c.project_id == pdict["id"]
            )

            report.projects.append(proj_inv)

    def _build_pipeline_inventory(
        self, w: dict, wf_store, schedules_by_wf: dict, alerts_by_wf: dict,
    ) -> PipelineInventory:
        wf_id = w.get("id", "")
        name = w.get("name", wf_id)

        # Steps summary — walk the DAG blob.
        dag = w.get("data") or w
        if isinstance(dag, str):
            try:
                dag = json.loads(dag)
            except (ValueError, TypeError):
                dag = {}
        steps = dag.get("steps", []) or dag.get("nodes", []) or []
        node_types = sorted({
            (s.get("type") if isinstance(s, dict) else getattr(s, "type", ""))
            for s in steps
        })
        # step_count is count of actual steps (truncated at MAX for node_types display).
        # list_all() returns a listing-summary shape that carries a scalar
        # `step_count` and strips the `steps[]` array (see versioning.list_all),
        # so `steps` is empty here and len(steps) would render "0 steps" for
        # every pipeline. Fall back to the pre-counted value when we don't
        # have the expanded step list.
        step_count = len(steps) or int(w.get("step_count", 0) or 0)
        connections = dag.get("connections", []) or []

        # Extract connection references from source/sink steps.
        conns_used: list[dict] = []
        seen_conn_ids: set[str] = set()
        for s in steps:
            sd = s if isinstance(s, dict) else self._model_to_dict(s)
            params = sd.get("params", {}) or {}
            cid = (
                params.get("connection_id")
                or params.get("credential_id")
                or params.get("connection_name")
            )
            if cid and cid not in seen_conn_ids:
                seen_conn_ids.add(cid)
                meta = self._connections_by_id.get(cid)
                conns_used.append({
                    "id": cid,
                    "name": meta["name"] if meta else cid,
                    "type": meta["type"] if meta else "?",
                })

        # Last N executions summary.
        last_runs = self._fetch_last_runs(wf_id)
        # Last N lifecycle events.
        lifecycle = self._fetch_last_lifecycle(wf_id)

        # Owner / approval info come from the workflow IR.
        owner = (
            w.get("owner_name") or w.get("owner_id")
            or (w.get("metadata", {}) or {}).get("owner", "")
            or ""
        )

        # ── Derive prominent signals (gap 4) ──────────────────────────
        schedule_rows = schedules_by_wf.get(wf_id, [])
        last_run_status = last_runs[0].get("status", "") if last_runs else ""
        last_run_at = last_runs[0].get("started_at", "") if last_runs else ""
        next_run_at = ""
        # Pick the earliest future next_fire across all schedules.
        future_fires = [s.get("next_fire", "") for s in schedule_rows
                        if s.get("next_fire")]
        if future_fires:
            next_run_at = min(future_fires)

        # ── Derive environment bucket (gap 1) ─────────────────────────
        # Always DEV; also PROD if a specific version is deployed.
        deployed_version = w.get("deployed_version")
        environments = ["DEV"]
        if deployed_version:
            environments.append("PROD")

        return PipelineInventory(
            id=wf_id,
            name=name,
            description=w.get("description", "") or "",
            project_id=w.get("project_id", "default") or "default",
            status=(w.get("status") if isinstance(w.get("status"), str)
                    else getattr(w.get("status"), "value", "")) or "draft",
            deployed_version=deployed_version,
            latest_version=int(w.get("version", 1) or 1),
            owner=str(owner),
            approval_status=w.get("approval_status", "") or "",
            submitted_by=w.get("submitted_by", "") or "",
            approved_by=w.get("approved_by", "") or "",
            step_count=step_count,
            connection_count=len(connections),
            node_types=node_types[:MAX_NODE_DETAIL_PER_PIPELINE],
            connections_used=conns_used,
            schedules=schedule_rows,
            alert_rules=alerts_by_wf.get(wf_id, []),
            last_runs=last_runs,
            lifecycle=lifecycle,
            content_hash=w.get("content_hash", "") or "",
            tags=w.get("tags", []) or [],
            business_purpose=w.get("business_purpose", "") or "",
            readme=w.get("readme", "") or "",
            last_run_status=last_run_status,
            last_run_at=last_run_at,
            next_run_at=next_run_at,
            environments=environments,
        )

    def _fetch_last_runs(self, wf_id: str) -> list[dict]:
        exe_store = self._state.get("execution_store")
        if not exe_store:
            return []
        try:
            runs = exe_store.list_by_workflow(
                wf_id, limit=MAX_EXECUTIONS_PER_PIPELINE,
                workspace_id=self._workspace_id,
            ) or []
        except Exception:
            return []
        out = []
        for r in runs:
            d = r if isinstance(r, dict) else self._model_to_dict(r)
            out.append({
                "id": d.get("id", ""),
                "status": d.get("status", ""),
                "duration_ms": d.get("duration_ms", 0),
                "rows": d.get("rows_processed_total") or d.get("rows_processed") or 0,
                "started_at": d.get("started_at", "") or "",
                "triggered_by": d.get("triggered_by", "") or "",
            })
        return out

    def _fetch_last_lifecycle(self, wf_id: str) -> list[dict]:
        ls = self._state.get("lifecycle_store")
        if not ls:
            return []
        try:
            events = ls.get_events(wf_id, workspace_id=self._workspace_id) or []
        except Exception:
            return []
        # Latest first, capped.
        events = events[-MAX_LIFECYCLE_PER_PIPELINE:][::-1]
        out = []
        for e in events:
            d = e if isinstance(e, dict) else self._model_to_dict(e)
            out.append({
                "kind": d.get("kind", "") or d.get("event_type", ""),
                "message": d.get("message", "") or d.get("description", ""),
                "at": d.get("created_at", "") or d.get("timestamp", "") or "",
            })
        return out

    def _scope_filtered_workflow_ids(self, report: InventoryReport) -> set[str] | None:
        """When scope is 'project' or 'pipeline' AND a scope_id was supplied,
        return the set of pipeline IDs that survived the narrowing inside
        ``_collect_projects_and_pipelines``. Otherwise return None — the
        signal callers use to skip post-collect narrowing entirely.
        """
        if not self._scope_id_filter or self._scope not in ("project", "pipeline"):
            return None
        return {p.id for proj in report.projects for p in proj.pipelines}

    def _apply_scope_id_filter_post_collect(self, report: InventoryReport) -> None:
        """Narrow ancillary report sections to surviving entities for
        project / pipeline scope. Operates on the already-narrowed
        ``report.projects`` produced by ``_collect_projects_and_pipelines``.

        Sections affected:
          - ``report.schedules``      — keep only those firing surviving workflows
          - ``report.alerts``         — keep only those tied to surviving workflows
          - ``report.connections``    — keep only those used by at least one
                                        surviving pipeline (relies on
                                        ``_backfill_connection_usage`` having
                                        run first)
          - ``report.approval_gates`` — keep global gates + gates scoped to
                                        surviving project / pipeline
          - ``report.users``          — emptied for pipeline scope (a
                                        single-pipeline report has no
                                        meaningful user roster)
        """
        surviving_wf = self._scope_filtered_workflow_ids(report)
        if surviving_wf is None:
            return

        # ProjectInventory exposes the project_id under .id (set in
        # _collect_projects_and_pipelines). Walk the narrowed projects
        # to derive the matching project-id set for approval_gates.
        surviving_project_ids: set[str] = {
            getattr(proj, "id", "") for proj in report.projects
        } - {""}

        report.schedules = [
            s for s in report.schedules if s.workflow_id in surviving_wf
        ]
        report.alerts = [
            a for a in report.alerts if a.workflow_id in surviving_wf
        ]
        # used_by_pipelines was populated by _backfill_connection_usage from
        # the already-narrowed report.projects, so an empty list means the
        # connection is not referenced at the chosen scope.
        report.connections = [c for c in report.connections if c.used_by_pipelines]

        scoped_gate_ids = surviving_wf | surviving_project_ids
        report.approval_gates = [
            g for g in report.approval_gates
            if g.scope == "global" or g.scope_id in scoped_gate_ids
        ]

        if self._scope == "pipeline":
            report.users = []

    def _backfill_connection_usage(self, report: InventoryReport) -> None:
        """Second pass — now that pipelines are built, mark each
        connection with which pipelines reference it."""
        used: dict[str, list[str]] = {}
        for proj in report.projects:
            for p in proj.pipelines:
                for cu in p.connections_used:
                    used.setdefault(cu["id"], []).append(p.name)
        for c in report.connections:
            c.used_by_pipelines = used.get(c.id, [])

    def _compute_totals(self, report: InventoryReport) -> None:
        pipeline_total = sum(p.pipeline_count for p in report.projects)
        deployed_total = sum(
            1 for proj in report.projects for p in proj.pipelines
            if p.deployed_version
        )
        # Environment split (gap 1) — compute once, render in exec summary.
        pipelines_dev_only = sum(
            1 for proj in report.projects for p in proj.pipelines
            if "PROD" not in p.environments
        )
        pipelines_in_prod = sum(
            1 for proj in report.projects for p in proj.pipelines
            if "PROD" in p.environments
        )
        connections_dev = sum(
            1 for c in report.connections
            if (c.environment or "").upper() == "DEV"
        )
        connections_prod = sum(
            1 for c in report.connections
            if (c.environment or "").upper() == "PROD"
        )
        connections_shared = sum(
            1 for c in report.connections
            if not (c.environment or "").strip()
        )
        report.totals = {
            "projects": len(report.projects),
            "pipelines": pipeline_total,
            "pipelines_deployed": deployed_total,
            "pipelines_dev_only": pipelines_dev_only,
            "pipelines_in_prod": pipelines_in_prod,
            "connections": len(report.connections),
            "connections_dev": connections_dev,
            "connections_prod": connections_prod,
            "connections_shared": connections_shared,
            "connections_inline_creds": sum(1 for c in report.connections if c.has_inline_creds),
            "users": len(report.users),
            "users_active": sum(1 for u in report.users if u.is_active),
            "schedules": len(report.schedules),
            "schedules_enabled": sum(1 for s in report.schedules if s.enabled),
            "alerts": len(report.alerts),
            "alerts_enabled": sum(1 for a in report.alerts if a.enabled),
            "approval_gates": len(report.approval_gates),
        }

    def _compute_insights(self, report: InventoryReport) -> None:
        """Build the 'what should I fix first?' headline.

        Pure derivation from data already in ``report.projects`` —
        no extra queries. Buckets every (deployed/published) pipeline
        into one of: failing, healthy, stale, unknown.

        Categories:
          * **failing** — last_run_status in {error, failed, timeout}
          * **stale**   — last_run_at older than 7 days, or never run
          * **healthy** — last_run_status == 'success' and recent
          * **unknown** — has no run history; counted under stale
        """
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        stale_cutoff = now - timedelta(days=7)

        failing: list[PipelineInventory] = []
        stale: list[PipelineInventory] = []
        healthy: list[PipelineInventory] = []

        for proj in report.projects:
            for p in proj.pipelines:
                status = (p.last_run_status or "").lower()
                if status in ("error", "failed", "timeout"):
                    failing.append(p)
                    continue
                # Parse last_run_at; treat empty/unparseable as stale.
                is_stale = True
                if p.last_run_at:
                    try:
                        ts = datetime.fromisoformat(p.last_run_at.replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        is_stale = ts < stale_cutoff
                    except Exception:  # noqa: BLE001
                        is_stale = True
                if status == "success" and not is_stale:
                    healthy.append(p)
                else:
                    stale.append(p)

        items: list[InsightItem] = []
        if failing:
            top = failing[: min(5, len(failing))]
            items.append(InsightItem(
                severity="critical",
                icon="🔴",
                headline=f"{len(failing)} pipeline{'s' if len(failing) != 1 else ''} failing",
                detail="Recent runs errored: " + ", ".join(p.name for p in top)
                       + ("…" if len(failing) > len(top) else ""),
                pipeline_ids=[p.id for p in failing],
            ))
        if stale:
            top = stale[: min(5, len(stale))]
            items.append(InsightItem(
                severity="warning",
                icon="⏳",
                headline=f"{len(stale)} pipeline{'s' if len(stale) != 1 else ''} stale or never run",
                detail="No execution in the last 7 days: "
                       + ", ".join(p.name for p in top)
                       + ("…" if len(stale) > len(top) else ""),
                pipeline_ids=[p.id for p in stale],
            ))
        if healthy:
            items.append(InsightItem(
                severity="ok",
                icon="🟢",
                headline=f"{len(healthy)} pipeline{'s' if len(healthy) != 1 else ''} healthy",
                detail="Last run succeeded within the past week.",
                pipeline_ids=[p.id for p in healthy],
            ))

        # Top action — what should the reader actually do next?
        if failing:
            top_action = (
                f"Fix the {len(failing)} failing pipeline"
                f"{'s' if len(failing) != 1 else ''} first — "
                f"start with **{failing[0].name}**."
            )
        elif stale:
            top_action = (
                f"Investigate the {len(stale)} stale pipeline"
                f"{'s' if len(stale) != 1 else ''} — "
                f"either retire or schedule them."
            )
        elif healthy:
            top_action = "All pipelines are healthy. No action needed."
        else:
            top_action = (
                "No deployed pipelines yet. Publish your first pipeline "
                "to populate this report."
            )

        report.insights = InsightsHeadline(
            failing_count=len(failing),
            healthy_count=len(healthy),
            stale_count=len(stale),
            items=items,
            top_action=top_action,
        )

    def _compute_steward_summary(self, report: InventoryReport) -> None:
        """Steward findings snapshot at report-generation time.

        Runs the same detection path the live UI uses (no caching, sub-50ms
        on typical workspaces) and produces a small rollup:
          * total open findings
          * breakdown by severity (P1 / P2 / P3)
          * breakdown by kind (duplicate_source / duplicate_pipeline / …)
          * memory journal stats (total scans, dismisses, resolves)

        We intentionally do NOT embed full finding bodies in the
        inventory — those can change between report generation and
        report read; a count + kind breakdown is the right granularity
        for a snapshot artifact. Users who need detail open the live
        Steward dropdown.
        """
        try:
            from fpulse.steward import (
                StewardMemory,
                SettingsStore,
                apply_learning,
                detect_duplicate_sources,
            )
        except ImportError:
            return  # steward package not installed — silently skip

        # Workflow snapshot — mirror the api/steward.py normalisation
        try:
            from fpulse.api.workflows import get_store
            wf_rows = get_store().list_all(workspace_id=self._workspace_id) or []
        except Exception:
            return
        workflows: list[dict[str, Any]] = []
        for row in wf_rows:
            if hasattr(row, "model_dump"):
                d = row.model_dump()
            elif isinstance(row, dict):
                d = row
            else:
                continue
            nodes = (
                d.get("nodes")
                or (d.get("graph") or {}).get("nodes")
                or d.get("steps")
                or []
            )
            workflows.append({
                "id": d.get("id") or "",
                "name": d.get("name") or "",
                "nodes": nodes,
            })

        # Per-workspace settings + suppression load
        from pathlib import Path
        data_dir = Path(self._state.get("data_dir") or ".")
        steward_dir = data_dir / "steward" / self._workspace_id
        settings = SettingsStore(steward_dir / "settings.json").load()
        if not settings.enabled:
            report.steward_summary = {"enabled": False}
            return

        # Suppressions
        import json
        suppressed: set[str] = set()
        sup_path = steward_dir / "suppressions.json"
        if sup_path.is_file():
            try:
                with sup_path.open("r", encoding="utf-8") as fp:
                    data = json.load(fp)
                suppressed = set(data.get("suppressed_signatures") or [])
            except Exception:
                pass

        findings = detect_duplicate_sources(
            workflows,
            workspace_id=self._workspace_id,
            suppressed_signatures=suppressed,
        )
        memory = StewardMemory(steward_dir / "memory.jsonl")
        findings = apply_learning(
            findings,
            memory,
            escalate_after_n_occurrences=settings.escalate_after_n_occurrences,
            escalate_min_hours_since_first=settings.escalate_min_hours_since_first,
        )

        # Severity / kind / level / status / confidence rollups.
        # Updated 2026-06-06 to match the post-R4 model: 7 levels,
        # 8 status values, confidence richness on every finding.
        by_sev: dict[str, int] = {"p1": 0, "p2": 0, "p3": 0}
        by_kind: dict[str, int] = {}
        by_level: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_confidence: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
        for f in findings:
            by_sev[f.severity.value] = by_sev.get(f.severity.value, 0) + 1
            by_kind[f.kind.value] = by_kind.get(f.kind.value, 0) + 1
            by_level[f.level.value] = by_level.get(f.level.value, 0) + 1
            by_status[f.status.value] = by_status.get(f.status.value, 0) + 1
            conf = getattr(f, "confidence", "high")
            by_confidence[conf] = by_confidence.get(conf, 0) + 1

        report.steward_summary = {
            "enabled": True,
            "total_open_findings": len(findings),
            "by_severity": by_sev,
            "by_kind": by_kind,
            "by_level": by_level,             # NEW (R3/R4 7-level taxonomy)
            "by_status": by_status,           # NEW (R4 expanded 8-state lifecycle)
            "by_confidence": by_confidence,   # NEW (R4 confidence richness)
            "memory_stats": memory.stats(),
            "settings": {
                "min_severity": settings.min_severity,
                "escalate_after_n_occurrences": settings.escalate_after_n_occurrences,
                "escalate_min_hours_since_first": settings.escalate_min_hours_since_first,  # NEW (R1)
                "notify_on_finding": settings.notify_on_finding,
                "notify_min_severity": settings.notify_min_severity,
            },
        }

    def _compute_duration_analysis(self, report: InventoryReport) -> None:
        """Per-pipeline run-duration rollup over the last 30 days.

        For each pipeline with ≥3 runs:
          * avg(duration_ms)
          * p95(duration_ms) — the slow-tail signal that catches
            occasional bad runs the average smooths over
          * last run duration
          * regression flag = last_ms ≥ 1.5 × avg_ms (soft signal)

        Sorted by p95 descending so the slowest pipelines are at the top.
        """
        from datetime import datetime, timedelta, timezone
        from collections import defaultdict

        db = self._state.get("db")
        if db is None:
            return

        window_days = 30
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        ws_id = self._workspace_id

        try:
            rows = db.execute(
                """SELECT workflow_id, workflow_name, duration_ms, started_at
                   FROM execution_logs
                   WHERE workspace_id = ?
                     AND started_at >= ?
                     AND duration_ms IS NOT NULL
                     AND duration_ms > 0
                   ORDER BY started_at ASC
                   LIMIT 5000""",
                (ws_id, cutoff),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("duration_analysis: query failed: %s", exc)
            return

        per_wf_durations: dict[str, list[float]] = defaultdict(list)
        per_wf_name: dict[str, str] = {}
        per_wf_last: dict[str, float] = {}

        def _row_get(r, key: str):
            if isinstance(r, dict):
                return r.get(key)
            try:
                return r[key]
            except (KeyError, IndexError, TypeError):
                return None

        # Scope-narrowing: project / pipeline reports only count rows
        # belonging to surviving workflows.
        surviving_wf = self._scope_filtered_workflow_ids(report)

        for r in rows:
            wf_id = _row_get(r, "workflow_id") or ""
            if surviving_wf is not None and wf_id not in surviving_wf:
                continue
            wf_name = _row_get(r, "workflow_name") or wf_id or "(unknown)"
            dur = _row_get(r, "duration_ms")
            try:
                ms = float(dur)
            except (TypeError, ValueError):
                continue
            per_wf_durations[wf_id].append(ms)
            per_wf_name[wf_id] = wf_name
            per_wf_last[wf_id] = ms  # last because rows are ordered ASC

        out_rows: list[PipelineDurationRow] = []
        regressions = 0
        for wf_id, durs in per_wf_durations.items():
            if len(durs) < 3:
                continue
            durs_sorted = sorted(durs)
            avg = sum(durs) / len(durs)
            p95_idx = max(0, int(round(0.95 * (len(durs_sorted) - 1))))
            p95 = durs_sorted[p95_idx]
            last_ms = per_wf_last.get(wf_id, durs[-1])
            is_regression = avg > 0 and last_ms >= avg * 1.5
            if is_regression:
                regressions += 1
            out_rows.append(PipelineDurationRow(
                pipeline_id=wf_id,
                pipeline_name=per_wf_name.get(wf_id, wf_id),
                runs=len(durs),
                avg_ms=int(avg),
                p95_ms=int(p95),
                last_ms=int(last_ms),
                regression=is_regression,
            ))

        out_rows.sort(key=lambda r: r.p95_ms, reverse=True)
        slowest = out_rows[0].pipeline_name if out_rows else ""

        report.duration_analysis = DurationAnalysis(
            window_days=window_days,
            total_pipelines=len(out_rows),
            total_runs=sum(r.runs for r in out_rows),
            rows=out_rows[:20],
            slowest_pipeline=slowest,
            regressions_count=regressions,
        )

    def _compute_failure_analysis(self, report: InventoryReport) -> None:
        """30-day failure-analysis section.

        One bounded scan of execution_logs:
          1. Top failing pipelines (by failure count) + their failure rate.
          2. Most common error signatures (normalized first 80 chars of
             error_summary) — collapses minor variants into the same row.
          3. Per-day failure count for a 30-day mini-trend.

        All three sets are derived from one SELECT to keep the report
        cheap to render even on a large execution history.
        """
        from datetime import datetime, timedelta, timezone
        from collections import Counter, defaultdict
        import re as _re

        db = self._state.get("db")
        if db is None:
            return

        window_days = 30
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        ws_id = self._workspace_id

        # Bounded fetch — cap at 5000 rows so a runaway workspace doesn't
        # bloat the report. Failure rates beyond that are still
        # representative for ranking.
        try:
            rows = db.execute(
                """SELECT workflow_id, workflow_name, status, started_at,
                          duration_ms, error_summary
                   FROM execution_logs
                   WHERE workspace_id = ?
                     AND started_at >= ?
                   ORDER BY started_at DESC
                   LIMIT 5000""",
                (ws_id, cutoff),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("failure_analysis: query failed: %s", exc)
            return

        # Bucket by pipeline; count failures + total runs separately so
        # the failure rate is meaningful (3 fails / 100 runs ≠ 3 fails / 3 runs).
        per_wf_total: Counter = Counter()
        per_wf_fail: Counter = Counter()
        per_wf_name: dict[str, str] = {}
        per_wf_last_failure: dict[str, dict] = {}
        # Error signatures + which pipelines they affect.
        error_counter: Counter = Counter()
        error_pipelines: dict[str, set[str]] = defaultdict(set)
        # Per-day failure count.
        per_day_fail: Counter = Counter()

        def _row_get(r, key: str):
            if isinstance(r, dict):
                return r.get(key)
            try:
                return r[key]
            except (KeyError, IndexError, TypeError):
                return None

        def _normalize_error(err: str) -> str:
            """Collapse common variations (timestamps, IDs, paths) so
            'connection refused at 10.0.1.5:5432' and 'connection
            refused at 10.0.2.7:5432' end up in the same bucket."""
            if not err:
                return "(no message)"
            s = str(err).strip()
            # IPs / hex IDs / quoted paths → placeholders
            s = _re.sub(r"\b\d+\.\d+\.\d+\.\d+(:\d+)?\b", "<addr>", s)
            s = _re.sub(r"\b[0-9a-f]{8,}\b", "<id>", s, flags=_re.I)
            s = _re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", "<ts>", s)
            s = _re.sub(r"\s+", " ", s)
            return s[:120]

        # Scope-narrowing: project / pipeline reports only count rows
        # belonging to surviving workflows.
        surviving_wf = self._scope_filtered_workflow_ids(report)

        for r in rows:
            wf_id = _row_get(r, "workflow_id") or ""
            if surviving_wf is not None and wf_id not in surviving_wf:
                continue
            wf_name = _row_get(r, "workflow_name") or wf_id or "(unknown)"
            status = (_row_get(r, "status") or "").lower()
            started = _row_get(r, "started_at") or ""
            err = _row_get(r, "error_summary") or ""
            per_wf_total[wf_id] += 1
            per_wf_name[wf_id] = wf_name
            if status in ("error", "failed", "timeout"):
                per_wf_fail[wf_id] += 1
                # Record the most-recent failure for the rollup table.
                if (
                    wf_id not in per_wf_last_failure
                    or started > per_wf_last_failure[wf_id].get("at", "")
                ):
                    per_wf_last_failure[wf_id] = {"at": started, "err": err}
                # Error signature buckets.
                sig = _normalize_error(err)
                error_counter[sig] += 1
                error_pipelines[sig].add(wf_name)
                # Per-day bucket (date prefix only).
                day = started[:10] if started else ""
                if day:
                    per_day_fail[day] += 1

        # Top failing pipelines — sort by failure count, descending.
        top_failing: list[FailingPipelineRollup] = []
        for wf_id, fail_count in per_wf_fail.most_common(15):
            total = per_wf_total[wf_id] or 0
            rate = (fail_count / total * 100.0) if total else 0.0
            last = per_wf_last_failure.get(wf_id, {})
            top_failing.append(FailingPipelineRollup(
                pipeline_id=wf_id,
                pipeline_name=per_wf_name.get(wf_id, wf_id),
                failure_count=fail_count,
                last_failure_at=last.get("at", "") or "",
                last_error=(last.get("err", "") or "")[:200],
                failure_rate_pct=round(rate, 1),
                total_runs=total,
            ))

        # Top error patterns
        top_errors: list[ErrorPatternRollup] = []
        for sig, count in error_counter.most_common(10):
            top_errors.append(ErrorPatternRollup(
                error_signature=sig,
                count=count,
                affected_pipelines=sorted(error_pipelines.get(sig, set()))[:8],
            ))

        # Per-day trend — fill gaps with 0 for cleaner charts.
        day_series: list[dict[str, Any]] = []
        today = datetime.now(timezone.utc).date()
        for offset in range(window_days, -1, -1):
            d = (today - timedelta(days=offset)).isoformat()
            day_series.append({"date": d, "failures": per_day_fail.get(d, 0)})

        report.failure_analysis = FailureAnalysis(
            window_days=window_days,
            total_failures=sum(per_wf_fail.values()),
            unique_failing_pipelines=len(per_wf_fail),
            top_failing=top_failing,
            top_errors=top_errors,
            failures_by_day=day_series,
        )

    def _compute_operational_audit(self, report: InventoryReport) -> None:
        """Build the OperationalAudit section (gap 3).

        Uses data already loaded by the project/pipeline traversal:
          - last_runs on each PipelineInventory → 24h success-rate window
          - schedules → next scheduled firings
          - last_run_status on each pipeline → recent-failure roster
        Plus one bounded scan of the execution store for runs outside
        the per-pipeline cap that still fall inside the 24h window.
        """
        window_hours = 24
        audit = OperationalAudit(window_hours=window_hours)

        cutoff = datetime.now(timezone.utc).timestamp() - window_hours * 3600

        total_runs = 0
        successes = 0
        failures = 0
        durations: list[float] = []
        failure_roster: list[RecentFailure] = []

        # One more bounded pull from the execution store — each pipeline's
        # `last_runs` is capped at MAX_EXECUTIONS_PER_PIPELINE so it may
        # miss busy pipelines. A fresh list_all(limit=500) covers that.
        # When scope is 'project' or 'pipeline', filter rows to surviving
        # workflows so the audit numbers reflect the chosen scope.
        surviving_wf = self._scope_filtered_workflow_ids(report)
        exe_store = self._state.get("execution_store")
        if exe_store is not None:
            try:
                rows = exe_store.list_all(
                    limit=500, workspace_id=self._workspace_id,
                ) or []
            except Exception:
                rows = []
            for r in rows:
                d = r if isinstance(r, dict) else self._model_to_dict(r)
                if surviving_wf is not None:
                    wf_id = d.get("workflow_id") or d.get("pipeline_id") or ""
                    if wf_id not in surviving_wf:
                        continue
                started = d.get("started_at") or d.get("created_at") or ""
                ts = _parse_iso_ts(started)
                if ts is None or ts < cutoff:
                    continue
                total_runs += 1
                status = (d.get("status") or "").lower()
                if status == "success":
                    successes += 1
                elif status in ("error", "failed", "timeout"):
                    failures += 1
                dur = d.get("duration_ms")
                if isinstance(dur, (int, float)) and dur > 0:
                    durations.append(float(dur))

        # Pipelines whose *most recent* run failed, regardless of window.
        for proj in report.projects:
            for p in proj.pipelines:
                if p.last_run_status and p.last_run_status.lower() in (
                    "error", "failed", "timeout"
                ):
                    err = ""
                    if p.last_runs:
                        err = p.last_runs[0].get("error", "") or ""
                    failure_roster.append(RecentFailure(
                        workflow_id=p.id,
                        workflow_name=p.name,
                        failed_at=p.last_run_at,
                        error=err[:200] if err else "",
                    ))

        # Next 10 scheduled runs across the workspace, sorted ascending.
        upcoming: list[NextScheduledRun] = []
        for s in report.schedules:
            if not s.next_fire_at:
                continue
            upcoming.append(NextScheduledRun(
                workflow_id=s.workflow_id,
                workflow_name=s.workflow_name or s.workflow_id,
                cron_expression=s.cron_expression,
                environment=s.environment,
                next_fire_at=s.next_fire_at,
            ))
        upcoming.sort(key=lambda x: x.next_fire_at)
        audit.next_runs = upcoming[:10]

        audit.total_executions = total_runs
        audit.successful_executions = successes
        audit.failed_executions = failures
        audit.success_rate_pct = (
            round(100 * successes / total_runs, 1) if total_runs else 0.0
        )
        audit.avg_duration_ms = int(sum(durations) / len(durations)) if durations else 0
        # Keep the failure roster bounded — renderers truncate too, but
        # this caps memory in pathological cases.
        audit.recent_failures = failure_roster[:25]

        report.operational_audit = audit

    def _compute_health(self, report: InventoryReport) -> None:
        """Top-level installation health summary."""
        totals = report.totals
        issues: list[str] = []
        if totals.get("connections_inline_creds", 0) > 0:
            if self._tier == "free":
                # OSS Free has no Vault — pointing at /api/plus/vault/* would
                # 404. Surface the count plainly and mention Plus as the
                # remediation path without claiming a non-existent endpoint.
                issues.append(
                    f"{totals['connections_inline_creds']} connection(s) hold inline "
                    "credentials. F-Pulse+ adds Vault-backed credential storage."
                )
            else:
                issues.append(
                    f"{totals['connections_inline_creds']} connection(s) still hold inline "
                    "credentials — migrate to Vault (POST /api/plus/vault/migrate-all)."
                )
        # Admin-roster + approval-gate checks are Plus-only concepts.
        # OSS Free is a single bootstrap user with no admin/non-admin
        # distinction and no approvals surface, so these rules would
        # always fire and mislead the reader.
        if self._tier != "free":
            users_admin = sum(1 for u in report.users
                              if u.role in ("super_admin", "admin"))
            if users_admin == 0:
                issues.append("No administrators configured — assign at least one `admin`.")
            elif users_admin == 1:
                issues.append("Only one administrator — consider a second for redundancy.")

        pipelines_undeployed_prod = sum(
            1 for proj in report.projects for p in proj.pipelines
            if p.status == "published" and not p.deployed_version
        )
        if pipelines_undeployed_prod > 0:
            issues.append(
                f"{pipelines_undeployed_prod} pipeline(s) are PUBLISHED but never deployed "
                "— schedules will not fire in PROD."
            )
        if self._tier != "free" and totals.get("approval_gates", 0) == 0:
            issues.append(
                "No approval gates configured — PROD deploys fall back to notifying "
                "all admins. Configure gates per-project for tighter governance."
            )

        report.health = {
            "issues": issues,
            "issue_count": len(issues),
            "score": max(0, 100 - 10 * len(issues)),
        }

    # ── Helpers ──────────────────────────────────────────────────────

    def _can_see_project(self, pdict: dict) -> bool:
        """ACL mirror of api/projects._can_see. Used only for scope="user"."""
        if self._caller is None:
            return True
        if getattr(self._caller, "role", None) in _ADMIN_ROLES:
            return True
        user_projects = getattr(self._caller, "projects", None) or []
        owner_id = pdict.get("owner_id", "")
        members = pdict.get("members", []) or []
        pid = pdict.get("id", "")
        uid = getattr(self._caller, "id", "")

        if not user_projects:
            if members or owner_id:
                return uid == owner_id or uid in members
            return True
        if pid in user_projects:
            return True
        if uid == owner_id:
            return True
        if uid in members:
            return True
        return False

    def _visible_project_ids(self) -> set[str]:
        proj_store = self._state.get("project_store")
        if not proj_store:
            return set()
        try:
            all_projects = proj_store.list_all(workspace_id=self._workspace_id) or []
        except Exception:
            return set()
        out: set[str] = set()
        for p in all_projects:
            pd = p if isinstance(p, dict) else self._model_to_dict(p)
            if self._can_see_project(pd):
                out.add(pd.get("id", ""))
        return out

    def _visible_workflow_ids(self, wf_store) -> set[str]:
        if not wf_store:
            return set()
        visible_proj = self._visible_project_ids()
        try:
            wfs = wf_store.list_all(workspace_id=self._workspace_id) or []
        except Exception:
            return set()
        out: set[str] = set()
        for w in wfs:
            pid = (w.get("project_id") if isinstance(w, dict)
                   else getattr(w, "project_id", "")) or "default"
            if pid in visible_proj or pid == "default":
                wid = w.get("id") if isinstance(w, dict) else getattr(w, "id", "")
                if wid:
                    out.add(wid)
        return out

    def _workflow_name(self, wf_store, wf_id: str) -> str:
        if not wf_store or not wf_id:
            return wf_id or ""
        try:
            v = wf_store.get(wf_id, workspace_id=self._workspace_id)
            return v.workflow.name if v and v.workflow else wf_id
        except Exception:
            return wf_id

    def _resolve_project_approver(
        self, project_id: str,
        gates_by_scope: dict, global_gate,
    ) -> tuple[str, list[str]]:
        """Pick the approver for a project: project-scope gate wins, then global."""
        gate = gates_by_scope.get(("project", project_id)) or global_gate
        if not gate:
            # Fallback: all admins/leads from user roster.
            admin_names = [
                u.email or u.name for u in (
                    UserInventory(id=uid, email=d.get("email", ""), name=d.get("name", ""),
                                  role=d.get("role", ""), is_active=True)
                    for uid, d in self._users_by_id.items()
                ) if u.role in ("super_admin", "admin", "lead")
            ]
            return ("(all admins)", admin_names)
        if not gate.approvers:
            return ("(all admins)", [])
        names = [self._user_display_name(a) for a in gate.approvers]
        first = names[0] if names else ""
        if len(names) > 1:
            first = f"{first} (+{len(names) - 1} more)"
        return (first, names)

    def _user_display_name(self, user_id: str) -> str:
        u = self._users_by_id.get(user_id)
        if not u:
            return user_id
        return u.get("name") or u.get("email") or user_id

    def _workspace_name(self) -> str:
        ws_store = self._state.get("workspace_store")
        if not ws_store:
            return self._workspace_id
        try:
            ws = ws_store.get(self._workspace_id)
            if ws:
                return getattr(ws, "name", self._workspace_id) or self._workspace_id
        except Exception:
            pass
        return self._workspace_id

    def _version(self) -> str:
        try:
            from fpulse import __version__
            return __version__
        except Exception:
            return "unknown"

    def _schema_version(self) -> int:
        db = self._state.get("db")
        if not db:
            return 0
        try:
            return int(getattr(db, "schema_version", 0) or 0)
        except Exception:
            return 0

    @staticmethod
    def _model_to_dict(obj) -> dict:
        if hasattr(obj, "model_dump"):
            try:
                return obj.model_dump(mode="json")
            except Exception:
                pass
        return dict(getattr(obj, "__dict__", {}))

    @staticmethod
    def _has_any_secret_key(config: dict) -> bool:
        """Match vault._is_secret_key's detection surface."""
        try:
            from fpulse.storage.vault import _is_secret_key, NESTED_SECRET_WRAPPERS
        except ImportError:
            return False
        if not isinstance(config, dict):
            return False
        for k, v in config.items():
            if _is_secret_key(k) and v not in (None, ""):
                return True
            if isinstance(v, dict) and k.lower() in NESTED_SECRET_WRAPPERS:
                for nk, nv in v.items():
                    if _is_secret_key(nk) and nv not in (None, ""):
                        return True
        return False

    @staticmethod
    def _format_alert_condition(data: dict) -> str:
        """Best-effort human string describing an alert rule."""
        trigger = data.get("trigger_type") or data.get("trigger") or data.get("type") or ""
        threshold = data.get("threshold") or data.get("value") or ""
        if trigger and threshold:
            return f"{trigger} {threshold}"
        return trigger or "custom"


# ── Module-level helpers (used by collector + renderers) ─────────────


def _mask_ref(raw: str) -> str:
    """Mask a vault credential reference ID so even the pointer doesn't
    leak in a shared document. `cred_abc123xyz` → `cred_a***xyz`.
    The reference is safe in principle, but masking it signals the
    report was prepared for sharing."""
    if not raw:
        return ""
    if len(raw) <= 8:
        return raw[:2] + "***"
    return raw[:6] + "***" + raw[-3:]


def _parse_iso_ts(s: str) -> float | None:
    """Parse an ISO-8601 string to epoch seconds, tolerating the common
    trailing-Z / +00:00 variants. Returns None if unparseable."""
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
        return datetime.fromisoformat(s2).timestamp()
    except (ValueError, TypeError):
        return None
