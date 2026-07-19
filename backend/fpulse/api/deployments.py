"""Deployment API — N10 round 2 (2026-05-26).

CRUD over the new `Deployment` IR model (round 1: backend/fpulse/ir/schema.py).
A Deployment binds a workflow to a parameter set + schedule + worker
pool so the same pipeline IR can run in N different contexts (e.g.
`orders-nightly-prod`, `orders-hourly-dev`) without forking the IR.

Round 2 (this commit) ships:
  - In-memory store at app_state["deployment_store"]
  - GET    /api/deployments            list (workspace-scoped)
  - POST   /api/deployments            create
  - GET    /api/deployments/{id}       fetch one
  - PUT    /api/deployments/{id}       update fields
  - DELETE /api/deployments/{id}       drop
  - POST   /api/deployments/{id}/run   invoke this deployment now

Persistence (SQLite-backed store), schedule wiring (existing scheduler
picks up enabled deployments), and frontend UI land in follow-ups.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from fpulse.auth.deps import current_workspace_id
from fpulse.ir.schema import Deployment

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/deployments", tags=["deployments"])


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception:
        return "default"


# ── Persistent store ──────────────────────────────────────────────────
#
# Round 2 was an in-memory dict; round 3 (this commit) adds disk
# persistence via a JSON file at $FPULSE_DATA_DIR/deployments.json so
# deployments survive a backend restart. The hot path stays the dict
# — disk writes happen after every mutation, reads stay in memory.
#
# Future SQLite-backed store can drop in by replacing
# _persist_to_disk / _load_from_disk without changing the endpoints.


_DEPLOYMENTS: dict[str, Deployment] = {}
_LOADED_FROM_DISK = False


def _data_dir() -> Path:
    """Resolve the data directory. Defers the import so import-time
    side effects don't fire (app_state may not be set up yet)."""
    try:
        from fpulse.main import app_state
        return Path(app_state.get("data_dir") or os.environ.get("FPULSE_DATA_DIR") or ".")
    except Exception:
        return Path(os.environ.get("FPULSE_DATA_DIR") or ".")


def _store_path() -> Path:
    return _data_dir() / "deployments.json"


def _load_from_disk() -> None:
    """Read deployments.json into _DEPLOYMENTS. Idempotent — calling
    twice in one process is a no-op after the first load."""
    global _LOADED_FROM_DISK
    if _LOADED_FROM_DISK:
        return
    path = _store_path()
    if path.is_file():
        try:
            with path.open("r", encoding="utf-8") as f:
                blob = json.load(f)
            for raw in blob.get("deployments", []):
                try:
                    d = Deployment(**raw)
                    _DEPLOYMENTS[d.id] = d
                except Exception as exc:  # noqa: BLE001
                    logger.warning("deployments.json: skipped malformed row: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("deployments.json: load failed (%s) — starting empty", exc)
    _LOADED_FROM_DISK = True


def _persist_to_disk() -> None:
    """Write the current _DEPLOYMENTS map to deployments.json.

    Best-effort; logs but doesn't raise on write failure so the API
    call still returns success for the in-memory mutation. The next
    restart loses the change in that case — same trade-off the
    schedule + alert stores make today.
    """
    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file + atomic rename so a crash mid-write
        # can't leave a half-written deployments.json on disk.
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(
                {"version": 1, "deployments": [d.model_dump(mode="json") for d in _DEPLOYMENTS.values()]},
                f,
                indent=2,
                default=str,
            )
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("deployments.json: persist failed (%s) — in-memory state diverged", exc)


def _filter_by_workspace(workspace_id: str) -> list[Deployment]:
    _load_from_disk()
    return [
        d for d in _DEPLOYMENTS.values()
        if d.workspace_id == workspace_id
    ]


# ── Scheduler integration helpers (N10 round 4) ───────────────────────


def list_enabled_with_schedule() -> list[Deployment]:
    """Return every enabled deployment that has a cron schedule.

    Used by the scheduler's poll loop — it's cheap to scan the
    in-memory dict on every tick (~30s cadence), so we don't bother
    with a separate index. Filter checks: enabled flag true, schedule
    is a dict, cron field non-empty.
    """
    _load_from_disk()
    out: list[Deployment] = []
    for d in _DEPLOYMENTS.values():
        if not d.enabled:
            continue
        if not isinstance(d.schedule, dict):
            continue
        if not (d.schedule.get("cron") or "").strip():
            continue
        out.append(d)
    return out


def mark_fired(deployment_id: str, when: datetime) -> None:
    """Stamp `last_run_at` after the scheduler fires this deployment.

    Best-effort persist; we don't want a disk-write failure to crash
    the scheduler tick. The in-memory mutation always succeeds.
    """
    d = _DEPLOYMENTS.get(deployment_id)
    if d is None:
        return
    d.last_run_at = when
    d.updated_at = when
    try:
        _persist_to_disk()
    except Exception:  # noqa: BLE001
        pass


# ── Request bodies ────────────────────────────────────────────────────


class CreateDeploymentRequest(BaseModel):
    workflow_id: str
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any] | None = None
    worker_pool: str = "default"
    enabled: bool = True
    environment: str = "dev"


class UpdateDeploymentRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    parameters: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    worker_pool: str | None = None
    enabled: bool | None = None
    environment: str | None = None


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get("")
def list_deployments(
    workflow_id: str | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List deployments in this workspace. Optionally filter by workflow."""
    items = _filter_by_workspace(workspace_id)
    if workflow_id:
        items = [d for d in items if d.workflow_id == workflow_id]
    items.sort(key=lambda d: d.created_at, reverse=True)
    return {
        "deployments": [d.model_dump(mode="json") for d in items],
        "count": len(items),
    }


@router.post("")
def create_deployment(
    body: CreateDeploymentRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create a new deployment.

    Enforces unique (workspace_id, workflow_id, name). Duplicate name
    returns 409 with a hint that the operator can rename or update
    the existing row instead.
    """
    if body.environment not in {"dev", "prod"}:
        raise HTTPException(400, "environment must be 'dev' or 'prod'")
    # Uniqueness check.
    for d in _filter_by_workspace(workspace_id):
        if d.workflow_id == body.workflow_id and d.name == body.name:
            raise HTTPException(
                409,
                f"A deployment named '{body.name}' already exists for this workflow. "
                "Rename, or PUT to update the existing row.",
            )
    deployment = Deployment(
        workspace_id=workspace_id,
        workflow_id=body.workflow_id,
        name=body.name,
        description=body.description,
        parameters=body.parameters,
        schedule=body.schedule,
        worker_pool=body.worker_pool,
        enabled=body.enabled,
        environment=body.environment,
    )
    _DEPLOYMENTS[deployment.id] = deployment
    _persist_to_disk()
    logger.info(
        "deployment created id=%s workflow_id=%s name=%s env=%s",
        deployment.id, deployment.workflow_id, deployment.name, deployment.environment,
    )
    return deployment.model_dump(mode="json")


@router.get("/{deployment_id}")
def get_deployment(
    deployment_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    _load_from_disk()
    d = _DEPLOYMENTS.get(deployment_id)
    if not d or d.workspace_id != workspace_id:
        raise HTTPException(404, "deployment not found")
    return d.model_dump(mode="json")


@router.put("/{deployment_id}")
def update_deployment(
    deployment_id: str,
    body: UpdateDeploymentRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    d = _DEPLOYMENTS.get(deployment_id)
    if not d or d.workspace_id != workspace_id:
        raise HTTPException(404, "deployment not found")
    # Apply only the fields the caller sent — partial updates.
    updates = body.model_dump(exclude_none=True)
    if "environment" in updates and updates["environment"] not in {"dev", "prod"}:
        raise HTTPException(400, "environment must be 'dev' or 'prod'")
    for k, v in updates.items():
        setattr(d, k, v)
    d.updated_at = datetime.now(timezone.utc)
    _persist_to_disk()
    return d.model_dump(mode="json")


@router.delete("/{deployment_id}")
def delete_deployment(
    deployment_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    d = _DEPLOYMENTS.get(deployment_id)
    if not d or d.workspace_id != workspace_id:
        raise HTTPException(404, "deployment not found")
    del _DEPLOYMENTS[deployment_id]
    _persist_to_disk()
    return {"deleted": True, "id": deployment_id}


@router.post("/{deployment_id}/run")
def run_deployment(
    deployment_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Invoke a deployment now — runs the workflow with the deployment's
    parameter overrides. Round 2 returns the resolved invocation
    payload + a 202-style stub; full execution wiring uses the same
    runtime as POST /api/execute/workflow and lands in a follow-up.
    """
    d = _DEPLOYMENTS.get(deployment_id)
    if not d or d.workspace_id != workspace_id:
        raise HTTPException(404, "deployment not found")
    if not d.enabled:
        raise HTTPException(409, "deployment is disabled — enable it before running")
    return {
        "status": "queued",
        "deployment_id": deployment_id,
        "workflow_id": d.workflow_id,
        "parameters": d.parameters,
        "worker_pool": d.worker_pool,
        "environment": d.environment,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "Round-2 stub: returns the resolved invocation payload. "
            "Full enqueue + execution wiring lands in the next round."
        ),
    }
