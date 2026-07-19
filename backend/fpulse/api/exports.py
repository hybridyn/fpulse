"""PROD export API — download runs, usage, failures, and connections as CSV.

These endpoints return CSV downloads for operators who need
to share incident reports, usage summaries, or audit evidence with stakeholders
who don't have F-Pulse access.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from fpulse.auth.deps import current_workspace_id

router = APIRouter(prefix="/api/exports", tags=["exports"])


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


def _get(store_name: str):
    from fpulse.main import app_state
    return app_state[store_name]


def _csv_response(filename: str, rows: list[dict], columns: list[str]) -> StreamingResponse:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: _stringify(row.get(c)) for c in columns})
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _stringify(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        import json
        return json.dumps(v, default=str)
    return str(v)


def _within_window(ts_str: str | None, since: datetime | None) -> bool:
    if since is None or not ts_str:
        return True
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        return ts >= since
    except Exception:
        return True


def _resolve_since(days: int | None) -> datetime | None:
    if days is None or days <= 0:
        return None
    return datetime.utcnow() - timedelta(days=days)


@router.get("/runs.csv")
async def export_runs(
    days: int = Query(30, ge=0, le=365),
    project_id: str | None = None,
    workflow_id: str | None = None,
    status: str | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Export pipeline execution history as CSV (scoped to caller's
    workspace)."""
    store = _get("execution_store")
    runs = store.list_all(5000, workspace_id=workspace_id)
    since = _resolve_since(days)

    filtered = []
    for r in runs:
        if project_id and r.get("project_id") != project_id:
            continue
        if workflow_id and r.get("workflow_id") != workflow_id:
            continue
        if status and r.get("status") != status:
            continue
        if not _within_window(r.get("started_at"), since):
            continue
        filtered.append(r)

    columns = [
        "id", "workflow_id", "workflow_name", "project_id",
        "status", "started_at", "finished_at", "duration_ms",
        "triggered_by", "environment", "step_count", "error",
    ]
    return _csv_response(
        f"fpulse-runs-{datetime.utcnow().strftime('%Y%m%d')}.csv",
        filtered,
        columns,
    )


@router.get("/failures.csv")
async def export_failures(
    days: int = Query(30, ge=0, le=365),
    project_id: str | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Export failed pipeline runs with error details (scoped to
    caller's workspace)."""
    store = _get("execution_store")
    runs = store.list_all(5000, workspace_id=workspace_id)
    since = _resolve_since(days)

    failed = []
    for r in runs:
        if r.get("status") != "error":
            continue
        if project_id and r.get("project_id") != project_id:
            continue
        if not _within_window(r.get("started_at"), since):
            continue
        failed.append(r)

    columns = [
        "id", "workflow_id", "workflow_name", "project_id",
        "started_at", "finished_at", "duration_ms",
        "error", "failed_step", "triggered_by",
    ]
    return _csv_response(
        f"fpulse-failures-{datetime.utcnow().strftime('%Y%m%d')}.csv",
        failed,
        columns,
    )


@router.get("/usage.csv")
async def export_usage(
    days: int = Query(30, ge=0, le=365),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Export usage rollup: executions per pipeline, success rate, avg
    duration (scoped to caller's workspace)."""
    store = _get("execution_store")
    runs = store.list_all(10000, workspace_id=workspace_id)
    since = _resolve_since(days)

    agg: dict[str, dict] = {}
    for r in runs:
        if not _within_window(r.get("started_at"), since):
            continue
        wf = r.get("workflow_id") or "unknown"
        row = agg.setdefault(wf, {
            "workflow_id": wf,
            "workflow_name": r.get("workflow_name", ""),
            "project_id": r.get("project_id", ""),
            "total_runs": 0,
            "success_runs": 0,
            "failed_runs": 0,
            "total_duration_ms": 0,
            "first_run_at": r.get("started_at"),
            "last_run_at": r.get("started_at"),
        })
        row["total_runs"] += 1
        if r.get("status") == "success":
            row["success_runs"] += 1
        elif r.get("status") == "error":
            row["failed_runs"] += 1
        dur = r.get("duration_ms") or 0
        try:
            row["total_duration_ms"] += int(dur)
        except (TypeError, ValueError):
            pass
        started = r.get("started_at")
        if started:
            if not row["first_run_at"] or started < row["first_run_at"]:
                row["first_run_at"] = started
            if not row["last_run_at"] or started > row["last_run_at"]:
                row["last_run_at"] = started

    rows = []
    for row in agg.values():
        total = row["total_runs"] or 1
        row["success_rate_pct"] = round((row["success_runs"] / total) * 100, 2)
        row["avg_duration_ms"] = round(row["total_duration_ms"] / total)
        rows.append(row)

    rows.sort(key=lambda r: r["total_runs"], reverse=True)

    columns = [
        "workflow_id", "workflow_name", "project_id",
        "total_runs", "success_runs", "failed_runs",
        "success_rate_pct", "avg_duration_ms",
        "first_run_at", "last_run_at",
    ]
    return _csv_response(
        f"fpulse-usage-{datetime.utcnow().strftime('%Y%m%d')}.csv",
        rows,
        columns,
    )


@router.get("/connections.csv")
async def export_connections(
    project_id: str | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Export configured connections — credentials are redacted,
    scoped to the caller's workspace."""
    try:
        store = _get("connection_store")
    except KeyError:
        raise HTTPException(500, "Connection store not initialized")

    conns = (
        store.list_all(workspace_id=workspace_id)
        if hasattr(store, "list_all") else []
    )
    if project_id:
        conns = [c for c in conns if (c.get("project_id") == project_id or c.get("scope") == "global")]

    safe_rows = []
    for c in conns:
        safe_rows.append({
            "id": c.get("id"),
            "name": c.get("name"),
            "type": c.get("type"),
            "scope": c.get("scope"),
            "project_id": c.get("project_id"),
            "host": c.get("host") or (c.get("config", {}) or {}).get("host", ""),
            "database": (c.get("config", {}) or {}).get("database", ""),
            "created_at": c.get("created_at"),
            "last_tested_at": c.get("last_tested_at"),
            "test_status": c.get("test_status"),
        })

    columns = [
        "id", "name", "type", "scope", "project_id",
        "host", "database", "created_at", "last_tested_at", "test_status",
    ]
    return _csv_response(
        f"fpulse-connections-{datetime.utcnow().strftime('%Y%m%d')}.csv",
        safe_rows,
        columns,
    )
