"""Pool allocation API — PR14.

Three endpoints under ``/api/plus/pool/allocation``:

* ``GET``                 — read the current allocation (any authenticated user)
* ``PUT``                 — update the allocation (admin only)
* ``GET /queue-depth``    — live queue depth split by env (any authenticated user)

The allocation table is per-workspace; the X-Workspace-Id header
selects which one. Defaults to 60/20/20 (PROD/DEV/burst) when no row
exists yet.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from fpulse.auth.deps import current_workspace_id, require_admin, require_auth
from fpulse.pool import (
    DEFAULT_BURST_PCT,
    DEFAULT_DEV_PCT,
    DEFAULT_PROD_PCT,
    PoolAllocationStore,
)


logger = logging.getLogger("fpulse.pool.api")
router = APIRouter(prefix="/api/plus/pool/allocation", tags=["pool"])

_store = PoolAllocationStore()


def _get_db():
    from fpulse.main import app_state
    db = app_state.get("db")
    if db is None:
        raise HTTPException(503, "Database not initialized")
    return db


# ─────────────────────────────────────────────────────────────────────────
# Request / response models
# ─────────────────────────────────────────────────────────────────────────


class AllocationResponse(BaseModel):
    workspace_id: str
    prod_reserved_pct: int
    dev_reserved_pct: int
    burst_pct: int
    updated_at: str = ""
    updated_by: str | None = None
    # Computed at response time so the UI doesn't have to re-derive.
    total_workers: int | None = None
    slots: dict[str, int] | None = None


class UpdateAllocationBody(BaseModel):
    prod_reserved_pct: int = Field(..., ge=0, le=100)
    dev_reserved_pct: int = Field(..., ge=0, le=100)
    burst_pct: int = Field(..., ge=0, le=100)


# ─────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────


def _total_workers() -> int:
    """Read total worker capacity from the ExecutionManager (or fall back)."""
    try:
        from fpulse.engine.execution_manager import ExecutionManager
        mgr = ExecutionManager.get()
        # Prefer an explicit attribute if defined; fall back to a reasonable default.
        for attr in ("max_workers", "_max_workers", "capacity"):
            if hasattr(mgr, attr):
                v = getattr(mgr, attr)
                if isinstance(v, int) and v > 0:
                    return v
    except Exception:
        pass
    return 20  # Conservative default matching the start.ps1 banner


@router.get("", response_model=AllocationResponse)
def get_allocation(
    _user=Depends(require_auth),
    workspace_id: str = Depends(current_workspace_id),
):
    """Read the current pool allocation for this workspace.

    Available to any authenticated user — they need to see capacity to
    understand queue waits. Editing is admin-gated (PUT below).
    """
    db = _get_db()
    with db.connect() as conn:
        alloc = _store.get(conn, workspace_id)
    total = _total_workers()
    return AllocationResponse(
        **alloc.to_dict(),
        total_workers=total,
        slots=alloc.slots(total),
    )


@router.put("", response_model=AllocationResponse)
def update_allocation(
    body: UpdateAllocationBody = Body(...),
    user=Depends(require_admin),
    workspace_id: str = Depends(current_workspace_id),
):
    """Persist a new allocation. The DB CHECK constraint AND the store's
    pre-validation both enforce sum=100, so a bad request returns 400."""
    db = _get_db()
    updated_by = getattr(user, "email", None) or getattr(user, "id", None) or "admin"
    try:
        with db.connect() as conn:
            alloc = _store.upsert(
                conn,
                workspace_id=workspace_id,
                prod_reserved_pct=body.prod_reserved_pct,
                dev_reserved_pct=body.dev_reserved_pct,
                burst_pct=body.burst_pct,
                updated_by=updated_by,
            )
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Audit event so the change is visible in the audit log.
    try:
        from fpulse.main import app_state
        audit_logger = app_state.get("audit_logger")
        if audit_logger:
            audit_logger.log(
                user_id=getattr(user, "id", "") or updated_by,
                user_email=getattr(user, "email", "") or updated_by,
                action="pool.allocation.changed",
                resource_type="pool_allocation",
                resource_id=workspace_id,
                details={
                    "prod_reserved_pct": body.prod_reserved_pct,
                    "dev_reserved_pct": body.dev_reserved_pct,
                    "burst_pct": body.burst_pct,
                },
            )
    except Exception:
        pass

    total = _total_workers()
    return AllocationResponse(
        **alloc.to_dict(),
        total_workers=total,
        slots=alloc.slots(total),
    )


@router.get("/queue-depth")
def queue_depth(
    _user=Depends(require_auth),
    workspace_id: str = Depends(current_workspace_id),
):
    """Live queue depth split by env. Reads the WorkerPool's live
    ``environments.active_dev`` / ``active_prod`` counters that the
    pool already maintains for its dashboard endpoint. Burst busy is
    derived (busy beyond what the dev+prod reservations cover lands
    in burst). Returns zeros if the pool isn't available yet."""
    busy_prod = busy_dev = busy_burst = 0
    queued_total = 0
    try:
        from fpulse.main import app_state
        worker_pool = app_state.get("worker_pool")
        if worker_pool is not None and hasattr(worker_pool, "get_status"):
            status = worker_pool.get_status() or {}
            envs = status.get("environments") or {}
            busy_dev = int(envs.get("active_dev") or 0)
            busy_prod = int(envs.get("active_prod") or 0)
            queued_total = int((status.get("pool") or {}).get("queue_depth") or 0)
    except Exception:
        pass

    db = _get_db()
    with db.connect() as conn:
        alloc = _store.get(conn, workspace_id)
    total = _total_workers()
    slots = alloc.slots(total)

    # Burst spill-over: any DEV/PROD busy beyond their reservation lives
    # in burst. Cap at slot capacity so the UI never shows >100% on a lane.
    spill_dev = max(0, busy_dev - slots["dev"])
    spill_prod = max(0, busy_prod - slots["prod"])
    busy_burst = min(slots["burst"], spill_dev + spill_prod)
    busy_dev_in_reserved = busy_dev - spill_dev
    busy_prod_in_reserved = busy_prod - spill_prod

    return {
        "workspace_id": workspace_id,
        "total_workers": total,
        "slots": slots,
        "busy": {
            "prod": busy_prod_in_reserved,
            "dev": busy_dev_in_reserved,
            "burst": busy_burst,
        },
        "busy_total": {
            "prod": busy_prod,
            "dev": busy_dev,
        },
        "queued": queued_total,
        "as_of": "now",
    }
