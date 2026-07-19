"""Sync State API — surface the per-(workflow, step) cursor watermark.

Backs the `Last cursor` display + `Reset State` button on incremental
sources. The store + table are described in
``fpulse/engine/sync_state_store.py`` and the v31 migration.

Endpoints
---------
  GET    /api/sync-state/{workflow_id}/{step_id}
      Return the persisted cursor row, or ``{state: null}`` if no
      incremental run has completed yet for this step.

  DELETE /api/sync-state/{workflow_id}/{step_id}
      Drop the cursor row so the next run behaves like a full refresh
      again. Gated to data_engineer+ — analyst / viewer can read but
      not destructively reset.

  GET    /api/sync-state/{workflow_id}
      List every incremental cursor for a workflow. Used by the
      pipeline-level observability panel ("3 incremental sources in
      this pipeline, last sync at …").

All endpoints require auth at the router level. Reset is gated higher
because it changes downstream behaviour for every subsequent run.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from fpulse.auth.deps import require_auth, require_min_rank
from fpulse.engine.sync_state_store import sync_state_store

# Same dependency convention as the other launch-prep routers
# (workflows, schedules, variables, alerts, monitor) — see P1 docstring
# in auth/deps.py for the rationale.
_AUTH = Depends(require_auth)
_AUTHOR = Depends(require_min_rank("developer"))

router = APIRouter(
    prefix="/api/sync-state",
    tags=["sync-state"],
    dependencies=[_AUTH],
)


@router.get("/{workflow_id}")
def list_workflow_sync_state(workflow_id: str):
    """All incremental cursors for one workflow.

    Empty list when no source step in this workflow has produced an
    incremental cursor yet.
    """
    rows = sync_state_store.list_for_workflow(workflow_id)
    return {
        "workflow_id": workflow_id,
        "count": len(rows),
        "state": [r.model_dump(mode="json") for r in rows],
    }


@router.get("/{workflow_id}/{step_id}")
def get_step_sync_state(workflow_id: str, step_id: str):
    """One source step's cursor watermark.

    Returns ``{state: null}`` when no incremental run has completed
    yet — the UI uses this to show "No prior sync" instead of an
    error.
    """
    state = sync_state_store.get(workflow_id, step_id)
    return {
        "workflow_id": workflow_id,
        "step_id": step_id,
        "state": state.model_dump(mode="json") if state else None,
    }


@router.delete("/{workflow_id}/{step_id}", dependencies=[_AUTHOR])
def reset_step_sync_state(workflow_id: str, step_id: str):
    """Drop the cursor row.

    The next run for this step reads everything from the source again
    (full-refresh semantics) and then writes a fresh watermark. Useful
    after a manual cleanup, a backfill, or when the upstream schema
    changes in a way that broke the cursor column.

    Gated to data_engineer+ — analyst / viewer can't destructively
    reset sync state.
    """
    # Tolerate a missing row — DELETE is idempotent.
    sync_state_store.reset(workflow_id, step_id)
    return {"ok": True, "workflow_id": workflow_id, "step_id": step_id, "reset": True}
