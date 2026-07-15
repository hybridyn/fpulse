"""Backfill API — chunked re-execution of a pipeline over a date range.

Three endpoints:

  POST /api/executions/backfill
      Create + dispatch a new backfill. Body matches BackfillCreate.
      Returns 400 when the pipeline contains an append_risky or external
      sink and the caller didn't pass acknowledge_side_effects=true.

  GET  /api/executions/backfill
      List all parent backfills in the caller's workspace.

  GET  /api/executions/backfill/{id}
      Return one parent backfill + its window children.

  POST /api/executions/backfill/{id}/cancel
      Mark a backfill cancelled. In-flight windows finish naturally; the
      orchestrator polls between windows and bails on the next loop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from fpulse.auth.deps import current_workspace_id
from fpulse.backfills.models import (
    BackfillCreate, BackfillRun, BackfillStatus, OnFailure, WindowSize,
)
from fpulse.backfills.idempotency import find_unsafe_sinks
from fpulse.backfills.windows import generate_windows
from fpulse.backfills.store import get_backfill_store
from fpulse.backfills.orchestrator import (
    first_unfinished_window_index,
    run_backfill_async,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/executions/backfill", tags=["backfill"])


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, "workspace resolve failed") from exc


def _get_workflow_store():
    from fpulse.main import app_state
    return app_state.get("store")


def _get_execution_store():
    from fpulse.main import app_state
    return app_state.get("execution_store")


def _get_data_dir() -> str:
    from fpulse.main import app_state
    return app_state.get("data_dir", ".")


def _get_app_state() -> dict:
    from fpulse.main import app_state
    return app_state


def _serialize(row: BackfillRun) -> dict[str, Any]:
    d = row.model_dump(mode="json")
    # Drop the redundant repeat of cursor-bound fields on the wire when
    # they're the default empty.
    return d


@router.post("")
async def create_backfill(
    body: BackfillCreate,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create + dispatch a backfill.

    Steps:
      1. Resolve the workflow in the caller's workspace.
      2. Refuse with 400 if any sink is append_risky / external and the
         caller didn't acknowledge the side effects.
      3. Enumerate windows from start_date → end_date.
      4. Insert parent + child rows in backfill_runs.
      5. Spawn the orchestrator thread.
      6. Return the parent's id immediately.
    """
    wf_store = _get_workflow_store()
    if wf_store is None:
        raise HTTPException(500, "workflow store not initialised")
    versioned = wf_store.get(body.pipeline_id, workspace_id=workspace_id)
    if not versioned:
        raise HTTPException(404, f"pipeline {body.pipeline_id} not found")
    workflow = versioned.workflow

    # Idempotency guardrail. Backfills run a pipeline N times — a sink
    # that duplicates rows on append or fires a real-world side effect
    # is almost always wrong here. Block unless the caller explicitly
    # acknowledges.
    unsafe = find_unsafe_sinks(workflow.steps)
    if unsafe and not body.acknowledge_side_effects:
        raise HTTPException(
            400,
            {
                "code": "unsafe_for_backfill",
                "message": (
                    "Pipeline contains sinks that are not safe to re-run on "
                    "every window. Pass acknowledge_side_effects=true to "
                    "proceed at your own risk."
                ),
                "unsafe_sinks": unsafe,
            },
        )

    # 2026-05-26 — Cursor-usage preflight. If no source step references
    # the cursor parameter(s), every window reprocesses the same full
    # dataset — backfill silently no-ops. Block with HTTP 400 unless
    # the caller explicitly acknowledges via
    # ``acknowledge_no_cursor_usage=true``. See backfills/preflight.py
    # for the static IR scan logic.
    from fpulse.backfills.preflight import check_cursor_param_usage
    effective_cursor_names = (
        list(body.cursor_param_names or [])
        or ["window_start", "window_end"]
    )
    cursor_violation = check_cursor_param_usage(workflow, effective_cursor_names)
    if cursor_violation and not body.acknowledge_no_cursor_usage:
        raise HTTPException(400, cursor_violation)

    # Enumerate windows up-front so we can stamp total_windows on the
    # parent and surface obvious mistakes (zero windows, malformed dates)
    # at create time rather than from the orchestrator thread.
    try:
        windows = generate_windows(
            body.start_date, body.end_date,
            body.window_size, window_size_hours=body.window_size_hours,
        )
    except ValueError as exc:
        raise HTTPException(400, f"window enumeration failed: {exc}")
    if not windows:
        raise HTTPException(400, "no windows produced for the given range")

    store = get_backfill_store()

    # Parent row — carries the user-facing config and aggregate status.
    parent = BackfillRun(
        pipeline_id=body.pipeline_id,
        parent_backfill_id="",
        window_start=windows[0].start.isoformat(),
        window_end=windows[-1].end.isoformat(),
        params_template=dict(body.parameter_values or {}),
        status=BackfillStatus.PENDING,
        window_size=body.window_size,
        window_size_hours=body.window_size_hours,
        cursor_param_names=list(body.cursor_param_names or []) or ["window_start", "window_end"],
        concurrency=max(1, int(body.concurrency or 1)),
        on_failure=body.on_failure,
        acknowledge_side_effects=body.acknowledge_side_effects,
        workspace_id=workspace_id,
        project_id=getattr(workflow, "project_id", "default"),
        total_windows=len(windows),
    )
    store.create_parent(parent)

    children = [
        BackfillRun(
            pipeline_id=body.pipeline_id,
            parent_backfill_id=parent.id,
            window_start=w.start.isoformat(),
            window_end=w.end.isoformat(),
            params_template={},
            status=BackfillStatus.PENDING,
            workspace_id=workspace_id,
            project_id=getattr(workflow, "project_id", "default"),
            # Inherit cursor config from parent so a child read alone has
            # enough state to render correctly (the UI may fetch children
            # without their parent).
            cursor_param_names=list(parent.cursor_param_names),
        )
        for w in windows
    ]
    store.create_children(parent.id, children)

    # Dispatch the orchestrator. Use the existing WorkflowExecutor with
    # the same app_state the regular /api/execute/workflow path uses so
    # connections, pool, checkpoint store all resolve identically.
    from fpulse.engine.executor import WorkflowExecutor
    executor = WorkflowExecutor(data_dir=_get_data_dir(), app_state=_get_app_state())
    run_backfill_async(
        parent.id,
        store=store,
        executor=executor,
        workflow=workflow,
        exe_store=_get_execution_store(),
        extra_params=dict(body.parameter_values or {}),
    )

    # Audit — a backfill creates + dispatches N pipeline runs (a mutating,
    # resource-consuming execution action), so it must leave a trail.
    try:
        from fpulse.audit import audit_action
        audit_action(
            action="backfill.create",
            resource_type="pipeline",
            resource_id=body.pipeline_id,
            workspace_id=workspace_id,
            details={
                "backfill_id": parent.id,
                "total_windows": len(windows),
                "window_start": parent.window_start,
                "window_end": parent.window_end,
                "acknowledge_side_effects": bool(body.acknowledge_side_effects),
            },
        )
    except Exception:
        pass

    return {
        "backfill_id": parent.id,
        "pipeline_id": body.pipeline_id,
        "total_windows": len(windows),
        "status": parent.status.value,
        "window_start": parent.window_start,
        "window_end": parent.window_end,
    }


@router.get("")
async def list_backfills(
    workspace_id: str = Depends(_safe_workspace_id),
    pipeline_id: str | None = None,
):
    """List parent backfills, newest first. Optional pipeline_id filter."""
    store = get_backfill_store()
    rows = store.list_parents(workspace_id=workspace_id, pipeline_id=pipeline_id)
    return [_serialize(r) for r in rows]


@router.get("/{backfill_id}")
async def get_backfill(
    backfill_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Return one parent backfill + its window children."""
    store = get_backfill_store()
    parent = store.get(backfill_id, workspace_id=workspace_id)
    if parent is None:
        raise HTTPException(404, f"backfill {backfill_id} not found")
    if parent.parent_backfill_id:
        # Caller passed a child id by mistake — return 404 to keep the
        # endpoint contract tight. The child can still be fetched via
        # the parent's `windows` list below.
        raise HTTPException(404, f"backfill {backfill_id} is a window, not a parent")
    children = store.list_children(parent.id)
    return {
        "backfill": _serialize(parent),
        "windows": [_serialize(c) for c in children],
    }


@router.post("/preflight")
async def preflight_backfill(
    body: BackfillCreate,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Dry-run safety + cursor + window checks WITHOUT creating a backfill.

    2026-05-30 (P5): the Backfill modal needs to surface unsafe-sink and
    cursor-usage warnings BEFORE the user clicks Launch — getting back
    400 after submit forces them to re-fill the form. This endpoint
    runs the same checks the create path does and returns a structured
    verdict so the modal can render warnings inline.

    Response shape (HTTP 200 in all valid-input cases):
        {
          "ok": <bool>,                       # safe to proceed without ack flags
          "total_windows": <int>,
          "first_window": "<iso>",
          "last_window": "<iso>",
          "unsafe_sinks": [{step_id, type, reason}, ...],  # empty if all safe
          "cursor_violation": {...} | null,                # see preflight.py
          "warnings": ["..."],                             # human-readable
          "recommendations": ["..."]                       # e.g. "switch to merge mode"
        }
    """
    wf_store = _get_workflow_store()
    if wf_store is None:
        raise HTTPException(500, "workflow store not initialised")
    versioned = wf_store.get(body.pipeline_id, workspace_id=workspace_id)
    if not versioned:
        raise HTTPException(404, f"pipeline {body.pipeline_id} not found")
    workflow = versioned.workflow

    # Same idempotency / cursor scans the create path runs.
    unsafe = find_unsafe_sinks(workflow.steps)
    from fpulse.backfills.preflight import check_cursor_param_usage
    effective_cursor_names = (
        list(body.cursor_param_names or [])
        or ["window_start", "window_end"]
    )
    cursor_violation = check_cursor_param_usage(workflow, effective_cursor_names)

    # Window enumeration — surfaces zero-windows / bad-range up front.
    try:
        windows = generate_windows(
            body.start_date, body.end_date,
            body.window_size, window_size_hours=body.window_size_hours,
        )
    except ValueError as exc:
        raise HTTPException(400, f"window enumeration failed: {exc}")

    warnings: list[str] = []
    recommendations: list[str] = []
    if unsafe:
        warnings.append(
            f"Pipeline has {len(unsafe)} sink(s) flagged unsafe for re-run."
        )
        # Per-class recommendation — append vs external get different remedies.
        kinds = {s.get("idempotency_class") for s in unsafe}
        if "append_risky" in kinds:
            recommendations.append(
                "Switch append-mode sinks to merge or upsert so windows don't duplicate rows on overlap or retry."
            )
        if "external" in kinds:
            recommendations.append(
                "External-side-effect sinks (email/webhook/slack/api) will fire once per window. Set an idempotency_key on each to skip already-sent rows."
            )
    if cursor_violation:
        warnings.append(
            "No source step references the cursor parameters — every window "
            "would re-read the full source."
        )
        recommendations.append(
            "Reference $params.window_start and $params.window_end in the source's WHERE clause so each window reads only its slice."
        )
    if not windows:
        warnings.append("Range produces zero windows.")
    elif len(windows) > 1000:
        warnings.append(
            f"This backfill will run {len(windows)} windows — long-running. "
            "Consider a larger window_size."
        )

    ok = not unsafe and not cursor_violation and len(windows) > 0

    return {
        "ok": ok,
        "total_windows": len(windows),
        "first_window": windows[0].start.isoformat() if windows else None,
        "last_window": windows[-1].end.isoformat() if windows else None,
        "unsafe_sinks": unsafe,
        "cursor_violation": cursor_violation,
        "warnings": warnings,
        "recommendations": recommendations,
    }


@router.post("/{backfill_id}/cancel")
async def cancel_backfill(
    backfill_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Mark a backfill cancelled. In-flight windows finish; future windows
    are skipped by the orchestrator's polling loop."""
    store = get_backfill_store()
    parent = store.get(backfill_id, workspace_id=workspace_id)
    if parent is None:
        raise HTTPException(404, f"backfill {backfill_id} not found")
    ok = store.cancel(backfill_id, workspace_id=workspace_id)
    return {"cancelled": ok, "backfill_id": backfill_id}


@router.post("/{backfill_id}/resume")
async def resume_backfill(
    backfill_id: str,
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """B3 (2026-06-08, docs/design/backfill-ux-1.2.md) - resume a
    failed / cancelled / partial backfill from the first window that
    didn't complete successfully.

    Body (optional):
      {
        "from_window": 17   // explicit override; if omitted, auto-detected
                            // as the index of the first non-SUCCESS child
      }

    Behaviour:
      * Looks up the parent; 404 if not found
      * Refuses to resume a parent in SUCCESS or RUNNING state (no
        action needed / already running)
      * Calls first_unfinished_window_index() when from_window is omitted
      * Spawns the orchestrator with from_window so successful windows
        in the prefix are NOT re-attempted
      * Windows that previously failed in the resume range get a fresh
        attempt with their existing IDs (the executor updates the
        backfill_runs row in place)
    """
    store = get_backfill_store()
    parent = store.get(backfill_id, workspace_id=workspace_id)
    if parent is None:
        raise HTTPException(404, f"backfill {backfill_id} not found")

    if parent.status == BackfillStatus.RUNNING:
        raise HTTPException(
            409,
            f"backfill {backfill_id} is currently running; cancel it before resuming",
        )
    if parent.status == BackfillStatus.SUCCESS:
        raise HTTPException(
            409,
            f"backfill {backfill_id} already succeeded; nothing to resume",
        )

    # Body is optional - operator may want auto-detection
    try:
        body = await request.json()
    except Exception:
        body = {}
    from_window_raw = (body or {}).get("from_window")
    if from_window_raw is None:
        from_window = first_unfinished_window_index(backfill_id, store=store)
    else:
        try:
            from_window = max(0, int(from_window_raw))
        except (TypeError, ValueError):
            raise HTTPException(400, "from_window must be a non-negative integer")

    # Re-load the workflow + executor so the orchestrator has a fresh
    # set of dependencies. Mirrors the create-backfill path.
    wf_store = _get_workflow_store()
    if wf_store is None:
        raise HTTPException(500, "workflow store not initialised")
    versioned = wf_store.get(parent.pipeline_id, workspace_id=workspace_id)
    if not versioned:
        raise HTTPException(
            404,
            f"pipeline {parent.pipeline_id} no longer exists; cannot resume backfill",
        )
    workflow = versioned.workflow

    from fpulse.engine.executor import WorkflowExecutor
    executor = WorkflowExecutor(data_dir=_get_data_dir(), app_state=_get_app_state())
    run_backfill_async(
        backfill_id,
        store=store,
        executor=executor,
        workflow=workflow,
        exe_store=_get_execution_store(),
        from_window=from_window,
    )
    return {
        "resumed": True,
        "backfill_id": backfill_id,
        "from_window": from_window,
        "skipped_windows": from_window,
    }
