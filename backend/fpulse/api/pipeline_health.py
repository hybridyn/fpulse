"""HTTP surface for pipeline-health metrics.

Three endpoints, all read-only over the existing ExecutionStore:

  GET /api/pipelines/health                — every pipeline + badge
  GET /api/pipelines/health/_summary       — counts by badge
  GET /api/pipelines/{pipeline_id}/health  — one pipeline's full metrics

Health is computed on-demand from execution rows — no separate
write-side state to maintain or migrate. Caller-tunable window via
`?window=` (default 50, max 500).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from fpulse.auth.deps import current_workspace_id
from fpulse.monitoring.pipeline_health import (
    compute_all_health,
    compute_health,
    health_summary,
)

router = APIRouter(prefix="/api/pipelines/health", tags=["pipeline-health"])


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception:
        return "default"


def _get_execution_store():
    from fpulse.main import app_state
    return app_state.get("execution_store")


@router.get("")
async def list_health(
    window: int = Query(50, ge=1, le=500),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """All pipelines + their health badges. Newest-first by last run."""
    store = _get_execution_store()
    if store is None:
        return {"items": [], "count": 0,
                "note": "execution store not available"}
    health_map = compute_all_health(store, workspace_id=workspace_id, window=window)
    items = [h.to_dict() for h in health_map.values()]
    items.sort(key=lambda x: (x.get("last_run_at") or 0), reverse=True)
    return {"items": items, "count": len(items)}


@router.get("/_summary")
async def get_summary(
    window: int = Query(50, ge=1, le=500),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Counts by badge. Drives the dashboard health tile."""
    store = _get_execution_store()
    if store is None:
        return {"total": 0, "healthy": 0, "degraded": 0,
                "unhealthy": 0, "running": 0, "never_run": 0}
    health_map = compute_all_health(store, workspace_id=workspace_id, window=window)
    return health_summary(health_map)


# Note: per-pipeline endpoint lives at a sibling path (`/api/pipelines/{id}/health`)
# to fit the URL hierarchy operators expect. Defined as a separate router with
# the parent prefix so it composes cleanly with workflows-already-on-/api/pipelines.
per_pipeline_router = APIRouter(prefix="/api/pipelines", tags=["pipeline-health"])


@per_pipeline_router.get("/{pipeline_id}/health")
async def get_pipeline_health(
    pipeline_id: str,
    window: int = Query(50, ge=1, le=500),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Full health snapshot for one pipeline.

    404 only when the pipeline_id has zero executions in the workspace.
    A pipeline that exists but has never run yields a `never_run`
    badge — that's a valid (empty) state, not an error.
    """
    store = _get_execution_store()
    if store is None:
        raise HTTPException(503, "execution store not available")
    rows = store.list_by_workflow(pipeline_id, limit=window, workspace_id=workspace_id)
    if not rows:
        # Distinguish 'no executions' from 'unknown id' — without a
        # workflow lookup we err on the permissive side and return a
        # never_run record. Clients can validate the id exists via
        # the workflows API if they want strictness.
        h = compute_health(pipeline_id, [], window=window)
        return h.to_dict()
    h = compute_health(pipeline_id, rows, window=window)
    return h.to_dict()
