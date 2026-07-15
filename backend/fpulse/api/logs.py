"""
Execution Logs API — query, compare, and analyze pipeline execution history.

Provides detailed execution logs, event streams, execution comparison,
and aggregate statistics for pipeline monitoring and debugging. Every
endpoint is scoped to the caller's workspace — cross-tenant lookups
surface as 404 exactly like a genuinely missing record, so one tenant
cannot probe for the existence of another tenant's executions.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from fpulse.auth.deps import current_workspace_id

router = APIRouter(prefix="/api/logs", tags=["logs"])


def _safe_workspace_id(request: Request) -> str:
    """Wrap current_workspace_id so dep failures surface as readable
    HTTP errors — same pattern as planner/templates/exports."""
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


def _get_log_store():
    """Get the ExecutionLogger from app state."""
    from fpulse.main import app_state
    log_store = app_state.get("execution_log_store")
    if not log_store:
        raise HTTPException(503, "Execution log store not initialized")
    return log_store


# ── List executions ──

@router.get("/executions")
async def list_executions(
    workflow_id: str | None = None,
    status: str | None = None,
    triggered_by: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List executions visible to the caller's workspace.

    Query params:
        workflow_id: Filter by workflow ID.
        status: Filter by status (success, error, cancelled).
        triggered_by: Filter by trigger type (manual, schedule, webhook, websocket).
        limit: Max results (default 100, max 1000).
        offset: Skip first N results for pagination.
    """
    store = _get_log_store()
    return store.list_executions(
        workflow_id=workflow_id,
        status=status,
        triggered_by=triggered_by,
        limit=limit,
        offset=offset,
        workspace_id=workspace_id,
    )


# ── Get execution detail ──

@router.get("/executions/{execution_id}")
async def get_execution_log(
    execution_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Get detailed execution log including all events and result data.

    Returns the complete execution record with parsed step events and
    full result payload — scoped to the caller's workspace so cross-
    tenant access looks identical to a missing record.
    """
    store = _get_log_store()
    log = store.get_execution_log(execution_id, workspace_id=workspace_id)
    if not log:
        raise HTTPException(404, f"Execution log not found: {execution_id}")
    return log


# ── Get execution events ──

@router.get("/executions/{execution_id}/events")
async def get_execution_events(
    execution_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Get the event stream for a specific execution.

    Returns the ordered list of real-time events (workflow_started,
    step_started, step_completed, step_error, workflow_completed)
    with their timestamps and data. Scoped to the caller's workspace.
    """
    store = _get_log_store()
    events = store.get_execution_events(execution_id, workspace_id=workspace_id)
    if not events:
        # Check if execution exists in THIS workspace but has no events
        log = store.get_execution_log(execution_id, workspace_id=workspace_id)
        if not log:
            raise HTTPException(404, f"Execution not found: {execution_id}")
        return []
    return events


# ── Compare executions ──

class CompareRequest(BaseModel):
    execution_id_1: str
    execution_id_2: str


@router.post("/executions/compare")
async def compare_executions(
    req: CompareRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Compare two executions side-by-side.

    Returns per-step diffs in timing, row counts, status, and errors.
    Both executions must belong to the caller's workspace — otherwise
    the compare is refused with 404, so the endpoint cannot be used
    as a cross-tenant existence oracle.
    """
    store = _get_log_store()
    result = store.compare_executions(
        req.execution_id_1,
        req.execution_id_2,
        workspace_id=workspace_id,
    )
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


# ── Execution stats ──

@router.get("/stats/{workflow_id}")
async def get_execution_stats(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Get aggregate execution statistics for a workflow.

    Stats are computed only from rows belonging to the caller's
    workspace, so a caller cannot infer cross-tenant activity even
    by guessing a workflow id.

    Returns:
        - Total executions, success/error/cancelled counts
        - Success rate percentage
        - Duration stats: avg, min, max, p50, p95
        - Total and average rows processed
        - Top 5 most common errors
        - Recent trend (last 10 executions)
    """
    store = _get_log_store()
    return store.get_execution_stats(workflow_id, workspace_id=workspace_id)


# ── Recent executions ──

@router.get("/recent")
async def recent_executions(
    limit: int = Query(default=20, ge=1, le=100),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Get recent executions across all workflows in the caller's
    workspace, ordered by start time descending.
    """
    store = _get_log_store()
    return store.list_recent(limit=limit, workspace_id=workspace_id)
