"""Pipeline monitoring API."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from fpulse.auth.deps import current_workspace_id, require_auth

logger = logging.getLogger(__name__)

# 2026-05-30 (Track S P1): monitor surfaces real pipeline activity —
# anonymous callers should not be able to enumerate executions /
# logs / system status. Reads need any authenticated user.
_AUTH = Depends(require_auth)

router = APIRouter(
    prefix="/api/monitor",
    tags=["monitor"],
    dependencies=[_AUTH],
)


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


def get_execution_store():
    # Delegate to fpulse.state for raise-on-missing semantics
    # (2026-05-22 — see fpulse/state.py docstring).
    from fpulse.state import get_execution_store as _impl
    return _impl()


def get_schedule_store():
    from fpulse.main import app_state
    return app_state["schedule_store"]


@router.get("/executions")
async def list_executions(
    workflow_id: str | None = None,
    project_id: str | None = None,
    limit: int = 200,
    workspace_id: str = Depends(_safe_workspace_id),
):
    store = get_execution_store()
    if workflow_id:
        return store.list_by_workflow(workflow_id, limit, workspace_id=workspace_id)
    if project_id:
        return store.list_by_project(project_id, limit, workspace_id=workspace_id)
    return store.list_all(limit, workspace_id=workspace_id)


@router.get("/recent-statuses")
async def recent_statuses(
    workflow_ids: str = "",
    limit_per_workflow: int = 14,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Return recent execution statuses for a set of workflows.

    Powers the RunStatusSparkline component on the Pipelines page —
    one colored dot per recent run. The endpoint accepts a comma-
    separated `workflow_ids` list and returns up to `limit_per_workflow`
    most-recent statuses per workflow, newest-first.

    Response shape:
        {
          "version": 1,
          "by_workflow": {
            "wf_abc123": ["success", "success", "error", "success", ...],
            "wf_def456": ["error", "running"],
            ...
          },
          "limit_per_workflow": 14
        }

    Implementation note: looks up each workflow via list_by_workflow.
    Cheap up to a few dozen workflows; pages with hundreds will want a
    bulk endpoint in a follow-up.
    """
    ids = [s.strip() for s in workflow_ids.split(",") if s.strip()]
    limit_per_workflow = max(1, min(int(limit_per_workflow or 14), 200))
    store = get_execution_store()
    by_workflow: dict[str, list[str]] = {}
    for wf_id in ids[:200]:  # hard cap to keep accidental N=10k fan-out bounded
        try:
            rows = store.list_by_workflow(wf_id, limit_per_workflow, workspace_id=workspace_id)
        except Exception:
            by_workflow[wf_id] = []
            continue
        # `rows` can be a list of dicts or pydantic objects depending
        # on the store impl; normalise to strings either way.
        statuses: list[str] = []
        for r in (rows or []):
            status = None
            if isinstance(r, dict):
                status = r.get("status")
            else:
                status = getattr(r, "status", None)
            statuses.append(str(status or "unknown").lower())
        by_workflow[wf_id] = statuses
    return {
        "version": 1,
        "by_workflow": by_workflow,
        "limit_per_workflow": limit_per_workflow,
    }


@router.get("/executions/{execution_id}")
async def get_execution_detail(
    execution_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Get execution detail with step-by-step logs — workspace-scoped.

    2026-05-25 — also enriches with any Storage outputs produced by this
    run (`storage_outputs: [{id, name, path, size_bytes, format}]`) so
    the Executions detail panel can link straight to the Storage page
    instead of leaving users guessing where the bytes landed.
    """
    store = get_execution_store()
    exe = store.get(execution_id, workspace_id=workspace_id)
    if not exe:
        raise HTTPException(404, "Execution not found")
    payload = exe.model_dump(mode="json")
    # Storage-output join — best-effort.
    try:
        from fpulse.datastore.store import get_store as _get_datastore
        from fpulse.datastore.models import OBJECT_KIND_OUTPUT
        ds = _get_datastore()
        # list_objects supports run_id filter via direct kwarg or we
        # filter in-process; do the latter for portability across older
        # store interfaces.
        all_outs = ds.list_objects(workspace_id, kind=OBJECT_KIND_OUTPUT, include_deleted=False)
        matched = [
            {
                "id": getattr(o, "id", ""),
                "name": getattr(o, "name", ""),
                "path": getattr(o, "path", ""),
                "size_bytes": int(getattr(o, "size_bytes", 0) or 0),
                "format": getattr(o, "format", None),
            }
            for o in all_outs
            if str(getattr(o, "run_id", "") or "") == str(execution_id)
        ]
        payload["storage_outputs"] = matched
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "execution storage_outputs join failed for %s", execution_id,
        )
    return payload


@router.get("/stats")
async def get_stats(
    hours: int = 24,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Get execution statistics for the dashboard — workspace-scoped."""
    store = get_execution_store()
    return store.get_stats(hours, workspace_id=workspace_id)


@router.get("/stats/multi")
async def get_multi_stats(
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Get stats for 24h, 7d, and 30d periods — workspace-scoped."""
    store = get_execution_store()
    return {
        "24h": store.get_stats(24, workspace_id=workspace_id),
        "7d": store.get_stats(168, workspace_id=workspace_id),
        "30d": store.get_stats(720, workspace_id=workspace_id),
    }


@router.get("/active-schedules")
async def active_schedules(
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Get all enabled schedules for the dashboard — workspace-scoped."""
    store = get_schedule_store()
    all_schedules = store.list_all(workspace_id=workspace_id)
    return [s for s in all_schedules if s.get("enabled", False)]


@router.get("/failed")
async def failed_pipelines(
    limit: int = 20,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Pipelines that are CURRENTLY failing — their most recent run failed —
    so "Needs Attention" reflects live health, not history.

    2026-06-17 — semantics fix (user-reported): this used to return any
    pipeline with a failed run anywhere in the last 500 runs. Two problems:
      (a) it wasn't current-state — a failure from weeks ago kept showing
          even though the dashboard frames it next to a 24h failure KPI; and
      (b) a pipeline that FAILED and then ran SUCCESSFULLY still appeared,
          even though it had recovered.
    Now it's STATE-based: we take each pipeline's MOST RECENT run and include
    it only if that run failed. A pipeline that recovered (latest run is a
    success) drops off automatically — answering "then what?": once it runs
    clean, it leaves Needs Attention on the next dashboard refresh.

    This is deliberately distinct from the 24h "failed runs" KPI, which counts
    failures in a time window regardless of current state — a pipeline broken
    days ago (and not re-run) legitimately still needs attention here while
    contributing 0 to the 24h count.

    One row per pipeline (its latest run), enriched with ``failure_count`` =
    the consecutive failing streak from the most recent run, so callers can
    render "1 pipeline · 6 failures".
    """
    from fpulse.monitoring.status import is_failed
    store = get_execution_store()
    all_execs = store.list_all(500, workspace_id=workspace_id)  # started_at DESC

    # Most-recent run per workflow (first seen, since list_all is DESC) plus
    # the per-workflow run list for the consecutive-failure streak.
    latest_by_wf: dict[str, dict] = {}
    runs_by_wf: dict[str, list] = {}
    for e in all_execs:
        wf_id = e.get("workflow_id") or ""
        if not wf_id:
            continue
        runs_by_wf.setdefault(wf_id, []).append(e)
        if wf_id not in latest_by_wf:
            latest_by_wf[wf_id] = e

    out: list[dict] = []
    for wf_id, latest in latest_by_wf.items():
        # Only currently-broken pipelines: the LATEST run failed. A more
        # recent success means it recovered → not "needs attention".
        if not is_failed(latest.get("status")):
            continue
        streak = 0
        for e in runs_by_wf[wf_id]:  # DESC — count failures back to last success
            if is_failed(e.get("status")):
                streak += 1
            else:
                break
        row = dict(latest)
        row["failure_count"] = streak
        out.append(row)

    return out[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# PROD glance — read-only, dev-safe summary of what's live in production.
#
# Why this exists:
#   Developers working in DEV still need to know "what's running in PROD right
#   now" — deployed pipelines, active schedules, last 24h runs, open alerts —
#   without switching environments and without being able to mutate anything.
#   Admins/leads see the same data plus richer detail on the PROD pages.
#
# What counts as "in PROD":
#   A workflow is considered live when `deployed_version` is set OR
#   `published_at` is non-null.
#
# Security:
#   - Auth required (any role with a valid session)
#   - No credentials, secrets, or raw logs are returned
#   - Every call is audit-logged with action=view_prod_from_dev
#   - Mutating operations remain gated by the existing PROD middleware
# ─────────────────────────────────────────────────────────────────────────────


def _is_deployed(wf: dict) -> bool:
    """A workflow counts as "in production" when it has been deployed once."""
    return bool(wf.get("deployed_version") or wf.get("deployed_at") or wf.get("published_at"))


@router.get("/prod-glance")
async def prod_glance(request: Request):
    """Read-only summary of production state.

    Why Plus-only:
        PROD is itself a Plus feature (the deploy button is gated, the
        environment toggle is gated, the audit trail is gated). Showing a
        live PROD summary on the DEV dashboard only makes sense once the
        organisation has a license — otherwise it would tease a feature
        that can't be used. Free-tier callers get a 402 with the standard
        upgrade message so the frontend can render an "Upgrade" CTA if
        desired (or, more commonly, simply hide the panel).

    Returns a single payload bundling deployed pipelines, active schedules,
    24h run stats, open alert count, and a health score. The shape is stable
    so the frontend ProdGlancePanel can render it with one round-trip.
    """
    from fpulse.main import app_state

    # ─── Tier gate ────────────────────────────────────────────────
    # Free tier sees 402 — same shape as every other Plus-gated endpoint.
    license_mgr = app_state.get("license_manager")
    is_plus = bool(license_mgr and getattr(license_mgr, "is_plus", False))
    if not is_plus:
        raise HTTPException(
            status_code=402,
            detail={
                "detail": "Production visibility is not available in this build.",
                "feature": "prod_glance",
                "tier": "free",
            },
        )

    # Resolve current user from session token. Anonymous callers are allowed
    # in OSS/local-dev mode where auth may be disabled — they get the same
    # read-only payload but no audit attribution.
    user = None
    try:
        from fpulse.api.auth import _current_user  # reuse helper
        user = _current_user(request)
    except HTTPException:
        # If a token was supplied but invalid, surface the auth error.
        if request.headers.get("Authorization"):
            raise
        user = None
    except Exception:
        user = None

    # 1) Deployed pipelines ----------------------------------------------------
    wf_store = app_state["store"]
    all_wfs = wf_store.list_all() or []
    deployed = [w for w in all_wfs if _is_deployed(w)]
    deployed_summary = [
        {
            "id": w.get("id"),
            "name": w.get("name"),
            "version": w.get("deployed_version") or w.get("version"),
            "deployed_at": w.get("deployed_at") or w.get("published_at"),
            "deployed_by": w.get("deployed_by") or w.get("published_by"),
            "status": w.get("status"),
        }
        for w in deployed[:25]  # cap payload
    ]

    # 2) Active schedules ------------------------------------------------------
    sched_store = app_state.get("schedule_store")
    active_schedules: list[dict] = []
    if sched_store:
        try:
            for s in sched_store.list_all() or []:
                if s.get("enabled"):
                    active_schedules.append(
                        {
                            "id": s.get("id"),
                            "workflow_id": s.get("workflow_id"),
                            "cron": s.get("cron") or s.get("cron_expression"),
                            "next_run": s.get("next_run"),
                            "last_run": s.get("last_run"),
                        }
                    )
        except Exception:
            pass

    # 3) Run stats — last 24h --------------------------------------------------
    exec_store = app_state.get("execution_store")
    run_stats = {"total": 0, "success": 0, "error": 0, "running": 0}
    recent_runs: list[dict] = []
    if exec_store:
        try:
            stats = exec_store.get_stats(24) or {}
            run_stats = {
                "total": stats.get("total", 0),
                "success": stats.get("success", 0),
                "error": stats.get("error", 0),
                "running": stats.get("running", 0),
            }
            # Sample of recent runs (no logs, no params)
            for r in (exec_store.list_all(20) or [])[:10]:
                recent_runs.append(
                    {
                        "id": r.get("id"),
                        "workflow_id": r.get("workflow_id"),
                        "workflow_name": r.get("workflow_name"),
                        "status": r.get("status"),
                        "started_at": r.get("started_at"),
                        "duration_ms": r.get("duration_ms"),
                    }
                )
        except Exception:
            pass

    # 4) Open alerts -----------------------------------------------------------
    alert_store = app_state.get("alert_store")
    open_alerts: list[dict] = []
    alert_count = 0
    if alert_store:
        try:
            logs = []
            if hasattr(alert_store, "list_recent_logs"):
                logs = alert_store.list_recent_logs(50) or []
            elif hasattr(alert_store, "list_logs"):
                logs = alert_store.list_logs(50) or []
            for a in logs:
                # treat anything not explicitly resolved as open
                if a.get("resolved"):
                    continue
                alert_count += 1
                if len(open_alerts) < 10:
                    open_alerts.append(
                        {
                            "id": a.get("id"),
                            "rule_id": a.get("rule_id"),
                            "workflow_id": a.get("workflow_id"),
                            "severity": a.get("severity", "info"),
                            "message": a.get("message"),
                            "triggered_at": a.get("triggered_at") or a.get("created_at"),
                        }
                    )
        except Exception:
            pass

    # 5) Health score ----------------------------------------------------------
    total = run_stats["total"] or 0
    success_rate = (run_stats["success"] / total * 100.0) if total else 100.0
    if alert_count > 0:
        success_rate = max(0.0, success_rate - alert_count * 2.0)
    health = "healthy" if success_rate >= 95 else ("degraded" if success_rate >= 80 else "unhealthy")

    # Audit the read so admins can see who is peeking at PROD from DEV.
    # Best-effort — never let audit failures break the API.
    try:
        audit = app_state.get("audit_logger")
        if audit and user is not None:
            audit.log(
                user_id=getattr(user, "id", "unknown"),
                user_email=getattr(user, "email", "unknown"),
                action="view_prod_from_dev",
                resource_type="prod_glance",
                resource_id="summary",
                details={
                    "deployed_count": len(deployed),
                    "active_schedules": len(active_schedules),
                    "open_alerts": alert_count,
                    "viewer_role": getattr(user, "role", "unknown"),
                },
            )
    except Exception:
        pass

    return {
        "deployed_pipelines": {
            "count": len(deployed),
            "items": deployed_summary,
        },
        "active_schedules": {
            "count": len(active_schedules),
            "items": active_schedules[:10],
        },
        "runs_24h": run_stats,
        "recent_runs": recent_runs,
        "alerts": {
            "open": alert_count,
            "items": open_alerts,
        },
        "health": {
            "success_rate": round(success_rate, 1),
            "status": health,
        },
        "viewer": {
            "role": getattr(user, "role", "anonymous") if user else "anonymous",
            "read_only": True,
            "environment": "prod",
        },
    }


# ── D1: IR Replay (2026-05-26) ─────────────────────────────────────────
#
# Replays a historical execution using its stored workflow_snapshot.
# Differentiator move per the master vision — a capability that tools
# which don't capture a deterministic per-run IR snapshot can't offer.
# F-Pulse already stores the snapshot (and SHA, since D1 round 1)
# on every ExecutionRecord; this endpoint reconstitutes a Workflow
# from the snapshot and re-executes against today's data sources.
#
# Replay run is tagged with `metadata.replay_of = <original_id>` and
# `metadata.original_ir_sha = <sha>` so the audit trail stays joined.
# The diff endpoint compares two ExecutionRecords step-by-step and
# returns a structured summary that the UI can render side-by-side.


def _diff_executions(a: dict, b: dict) -> dict[str, Any]:
    """Step-by-step diff between two execution records.

    Returns:
      {
        "status_changed": bool,
        "ir_sha_match": bool,
        "duration_delta_ms": float,
        "rows_delta": int,        # total across all steps
        "steps": [
          {step_id, step_name, a_status, b_status, a_rows, b_rows, changed},
          ...
        ],
        "added_steps": [step_id, ...],   # only in b
        "removed_steps": [step_id, ...], # only in a
      }
    """
    a_logs = {sl.get("step_id"): sl for sl in (a.get("step_logs") or [])}
    b_logs = {sl.get("step_id"): sl for sl in (b.get("step_logs") or [])}
    all_ids = list(a_logs.keys()) + [sid for sid in b_logs if sid not in a_logs]

    steps_diff: list[dict[str, Any]] = []
    rows_delta = 0
    for sid in all_ids:
        al = a_logs.get(sid) or {}
        bl = b_logs.get(sid) or {}
        a_rows = int(al.get("rows_processed") or 0)
        b_rows = int(bl.get("rows_processed") or 0)
        rows_delta += (b_rows - a_rows)
        steps_diff.append({
            "step_id": sid,
            "step_name": bl.get("step_name") or al.get("step_name") or sid,
            "a_status": al.get("status"),
            "b_status": bl.get("status"),
            "a_rows": a_rows,
            "b_rows": b_rows,
            "a_duration_ms": al.get("duration_ms"),
            "b_duration_ms": bl.get("duration_ms"),
            "changed": (al.get("status") != bl.get("status")) or (a_rows != b_rows),
        })

    return {
        "status_changed": a.get("status") != b.get("status"),
        "ir_sha_match": (a.get("ir_sha") and a.get("ir_sha") == b.get("ir_sha")),
        "duration_delta_ms": (b.get("duration_ms") or 0) - (a.get("duration_ms") or 0),
        "rows_delta": rows_delta,
        "steps": steps_diff,
        "added_steps": [sid for sid in b_logs if sid not in a_logs],
        "removed_steps": [sid for sid in a_logs if sid not in b_logs],
    }


@router.post("/executions/{execution_id}/replay")
async def replay_execution(
    execution_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Replay a historical run by re-executing its stored IR snapshot.

    The new run is recorded as a fresh ExecutionRecord tagged with
    `metadata.replay_of = <original_id>` so the audit trail joins the
    two. Sources / connections are resolved against TODAY'S state —
    a connector that's been rotated will be exercised with the new
    credentials. To replay against historical data, point the IR's
    sources at fixed paths / snapshots before replaying.

    Returns:
        {
          "original_id": str,
          "replay_id": str,
          "ir_sha": str,                # of the snapshot we replayed
          "status": str,                # of the new run
          "diff": <diff payload>,       # comparison of new vs original
        }
    """
    store = get_execution_store()
    original = store.get(execution_id, workspace_id=workspace_id)
    if not original:
        raise HTTPException(404, "Execution not found")
    snapshot = original.workflow_snapshot
    if not snapshot:
        raise HTTPException(
            422,
            "This execution has no stored IR snapshot — replay is not available. "
            "Snapshots are written from 2026-05 forward; older runs predate the feature.",
        )

    # Reconstitute a Workflow from the snapshot. We import here to keep
    # monitor.py decoupled from the executor at module-load time
    # (executor imports duckdb + numpy and is heavy).
    from fpulse.ir.schema import Workflow
    from fpulse.engine.executor import WorkflowExecutor
    from fpulse.main import app_state
    from fpulse.monitoring.store import ExecutionRecord

    try:
        wf = Workflow(**snapshot)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            422,
            f"Stored IR snapshot is malformed and can't be reconstituted: {exc}",
        ) from exc

    # Build a fresh execution record. The validator on ExecutionRecord
    # auto-fills the new ir_sha from the new workflow_snapshot, but we
    # also stamp the original sha in metadata for the audit trail.
    replay_record = ExecutionRecord(
        workflow_id=wf.id,
        workflow_name=wf.name or original.workflow_name,
        project_id=getattr(wf, "project_id", "default"),
        workspace_id=workspace_id,
        steps_total=len(wf.steps),
        workflow_snapshot=wf.model_dump(mode="json"),
        triggered_by="replay",
        metadata={
            "replay_of": original.id,
            "original_ir_sha": original.ir_sha,
            "original_started_at": (
                original.started_at.isoformat() if original.started_at else None
            ),
        },
    )

    start = time.time()
    data_dir = app_state.get("data_dir", ".")
    executor = WorkflowExecutor(data_dir=data_dir, app_state=app_state)
    try:
        result = executor.run(wf)
        # Map the executor's result onto the record.
        replay_record.status = "success" if not result.errors else "error"
        replay_record.error_message = (
            "; ".join(result.errors)[:500] if result.errors else None
        )
        replay_record.step_logs = [
            # ExecutionRecord.step_logs accepts dicts; let pydantic
            # coerce them to StepLog at construction time.
            sl if isinstance(sl, dict) else sl.model_dump()
            for sl in (result.step_logs or [])
        ]
        replay_record.steps_completed = sum(
            1 for sl in replay_record.step_logs if sl.get("status") == "success"
        )
        replay_record.steps_failed = sum(
            1 for sl in replay_record.step_logs if sl.get("status") == "error"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("replay failed for %s: %s", execution_id, exc)
        replay_record.status = "error"
        replay_record.error_message = f"{type(exc).__name__}: {exc}"[:500]

    replay_record.completed_at = datetime.now(timezone.utc)
    replay_record.duration_ms = (time.time() - start) * 1000

    store.record(replay_record)

    diff = _diff_executions(
        original.model_dump(mode="json"),
        replay_record.model_dump(mode="json"),
    )
    return {
        "original_id": original.id,
        "replay_id": replay_record.id,
        "ir_sha": replay_record.ir_sha,
        "status": replay_record.status,
        "diff": diff,
    }


@router.get("/executions/{a_id}/diff/{b_id}")
async def diff_executions_endpoint(
    a_id: str,
    b_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Step-by-step diff between two arbitrary executions (not just
    a replay pair). Useful for "what changed between yesterday's run
    and today's" without re-executing anything."""
    store = get_execution_store()
    a = store.get(a_id, workspace_id=workspace_id)
    b = store.get(b_id, workspace_id=workspace_id)
    if not a:
        raise HTTPException(404, f"Execution not found: {a_id}")
    if not b:
        raise HTTPException(404, f"Execution not found: {b_id}")
    return {
        "a_id": a.id,
        "b_id": b.id,
        "a_workflow_id": a.workflow_id,
        "b_workflow_id": b.workflow_id,
        "a_started_at": a.started_at.isoformat() if a.started_at else None,
        "b_started_at": b.started_at.isoformat() if b.started_at else None,
        "diff": _diff_executions(a.model_dump(mode="json"), b.model_dump(mode="json")),
    }
