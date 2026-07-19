"""run_backfill() — chunked re-execution loop.

The API layer creates the parent + child rows synchronously, then hands
them to ``run_backfill_async`` which spawns a background task that
iterates the windows. Each window:

  1. Binds ``${param.window_start}`` and ``${param.window_end}`` (plus
     any extra parameter_values the user passed) and dispatches a normal
     WorkflowExecutor.execute_workflow().
  2. Records the resulting status on the child row.
  3. Honours the parent's ``on_failure`` policy on failure.
  4. Polls the parent for a CANCELLED signal between windows.

Concurrency > 1 dispatches windows in batches through a ThreadPoolExecutor.
Sequential (concurrency = 1) is the default and the only mode the tests
exercise.

The orchestrator deliberately reuses ``WorkflowExecutor`` and
``ExecutionRecord`` rather than building a parallel execution path —
every window goes through the same idempotency / checkpoint / retry /
logging machinery as a regular Run-button invocation. That keeps
behaviour identical between "ran this pipeline once with X params" and
"ran this pipeline as part of a backfill with X params".
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone
from typing import Any

from .models import Backfill, BackfillRun, BackfillStatus, OnFailure
from .store import BackfillStore

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_window_params(
    parent: Backfill,
    child: BackfillRun,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the parameter_values dict for a single window.

    Cursor params get the window's start/end as ISO strings; any extra
    parameter_values the user passed at create time pass through
    unchanged. The child's ``cursor_param_names`` list determines which
    parameter names receive the bounds — defaults are
    ``window_start`` / ``window_end`` but a user can rename.
    """
    names = parent.cursor_param_names or ["window_start", "window_end"]
    start_name = names[0] if len(names) > 0 else "window_start"
    end_name = names[1] if len(names) > 1 else "window_end"
    out: dict[str, Any] = {}
    if isinstance(extra, dict):
        out.update(extra)
    # Cursor params win over extras if they collide — the whole point is
    # that the window's bounds bind these names.
    out[start_name] = child.window_start
    out[end_name] = child.window_end
    return out


def _run_one_window(
    parent: Backfill,
    child: BackfillRun,
    *,
    executor,
    workflow,
    exe_store,
    store: BackfillStore,
    extra_params: dict[str, Any],
) -> BackfillRun:
    """Execute one window and stamp the child row with the outcome."""
    bound = build_window_params(parent, child, extra_params)
    # Stamp the resolved params on the child so the UI can render exactly
    # what was used.
    store.update_status(child.id, BackfillStatus.RUNNING, params_template=bound)

    from fpulse.monitoring.store import ExecutionRecord  # local import — avoid cycle

    exe = ExecutionRecord(
        workflow_id=workflow.id,
        workflow_name=getattr(workflow, "name", "") or workflow.id,
        project_id=getattr(workflow, "project_id", "default"),
        workspace_id=getattr(workflow, "workspace_id", "default"),
        steps_total=len(workflow.steps),
        workflow_snapshot=workflow.model_dump(mode="json") if hasattr(workflow, "model_dump") else {},
        triggered_by="backfill",
    )
    try:
        md = dict(getattr(exe, "metadata", {}) or {})
        md["backfill_id"] = parent.id
        md["backfill_window_start"] = child.window_start
        md["backfill_window_end"] = child.window_end
        exe.metadata = md
    except Exception:  # noqa: BLE001
        pass

    start_ts = time.time()
    try:
        from fpulse.security.execution_codes import mint_for_run
        result = executor.execute_workflow(
            workflow,
            parameter_values=bound,
            run_id=exe.id,
            execution_code=mint_for_run(workflow, job_run_id=exe.id),
        )
        duration_ms = (time.time() - start_ts) * 1000
        exe.status = result.status
        exe.completed_at = datetime.now(timezone.utc)
        exe.duration_ms = round(duration_ms, 1)
        exe.steps_completed = sum(
            1 for sr in (result.step_results or {}).values() if sr.status == "success"
        )
        exe.steps_failed = sum(
            1 for sr in (result.step_results or {}).values() if sr.status == "error"
        )
        if exe_store is not None:
            try:
                exe_store.record(exe)
            except Exception:  # noqa: BLE001
                logger.warning("backfill: exe_store.record failed for window %s", child.id, exc_info=True)

        if result.status == "success":
            store.update_status(
                child.id, BackfillStatus.SUCCESS,
                execution_id=exe.id, completed=True,
            )
        else:
            first_err = next(
                (sr.error for sr in (result.step_results or {}).values() if sr.error),
                "",
            )
            store.update_status(
                child.id, BackfillStatus.FAILED,
                execution_id=exe.id, completed=True,
                error_message=first_err or "Pipeline returned non-success status",
            )
        return store.get(child.id) or child
    except Exception as exc:  # noqa: BLE001
        logger.exception("backfill: window execution crashed: %s", exc)
        store.update_status(
            child.id, BackfillStatus.FAILED,
            execution_id=exe.id, completed=True,
            error_message=str(exc),
        )
        if exe_store is not None:
            try:
                exe.status = "error"
                exe.completed_at = datetime.now(timezone.utc)
                exe.error_message = str(exc)
                exe_store.record(exe)
            except Exception:  # noqa: BLE001
                pass
        return store.get(child.id) or child


def first_unfinished_window_index(
    parent_id: str,
    *,
    store: BackfillStore,
) -> int:
    """B3 (2026-06-08, docs/design/backfill-ux-1.2.md) - locate the
    index of the first window that hasn't completed successfully.

    Used by the "resume from where it failed" path: rather than asking
    the operator to count windows manually, the API auto-detects which
    window to restart from. Returns 0 if all windows are pending OR if
    every window has succeeded (in which case there's nothing to resume).
    """
    children = store.list_children(parent_id)
    for i, child in enumerate(children):
        if child.status != BackfillStatus.SUCCESS:
            return i
    return 0  # all done OR no children


def run_backfill_sync(
    parent_id: str,
    *,
    store: BackfillStore,
    executor,
    workflow,
    exe_store,
    extra_params: dict[str, Any] | None = None,
    from_window: int = 0,
) -> Backfill:
    """Run the full backfill on the calling thread.

    Used by tests (predictable) and by the API's background path
    (wrapped in a Thread so the HTTP response returns immediately).

    `from_window` (B3, 2026-06-08, docs/design/backfill-ux-1.2.md) -
    when > 0, skip the first N windows. Used by the resume-from-window
    path: a backfill that failed at window 17 can be resumed by calling
    with `from_window=17`, and windows 0..16 are left untouched (their
    existing SUCCESS / FAILED status carries through to the aggregate).
    Default 0 = full run, current behaviour preserved.
    """
    parent = store.get(parent_id)
    if parent is None:
        raise ValueError(f"backfill {parent_id} not found")

    extra = dict(extra_params or {})
    all_children = store.list_children(parent_id)
    if not all_children:
        # Nothing to do — mark success.
        store.update_status(parent_id, BackfillStatus.SUCCESS, completed=True)
        return store.get(parent_id) or parent

    # B3 - slice off the skipped prefix. We still count ALL windows in
    # total_windows so the aggregate progress remains accurate; only
    # the iteration scope is narrowed.
    if from_window < 0:
        from_window = 0
    if from_window > len(all_children):
        # Out-of-range resume request - treat as "everything already done"
        store.update_status(parent_id, BackfillStatus.SUCCESS, completed=True)
        return store.get(parent_id) or parent
    children = all_children[from_window:]
    if not children:
        # Resume request happened to land at the very end. Mark success.
        store.update_status(parent_id, BackfillStatus.SUCCESS, completed=True)
        return store.get(parent_id) or parent

    # Mark parent running.
    parent.status = BackfillStatus.RUNNING
    parent.started_at = _now_iso()
    parent.total_windows = len(all_children)
    store.update_status(parent_id, BackfillStatus.RUNNING)

    on_failure = parent.on_failure
    concurrency = max(1, int(parent.concurrency or 1))

    def _process(child: BackfillRun) -> BackfillRun:
        return _run_one_window(
            parent, child,
            executor=executor, workflow=workflow,
            exe_store=exe_store, store=store, extra_params=extra,
        )

    if concurrency <= 1:
        for child in children:
            # Cancellation check — fresh read of the parent.
            current = store.get(parent_id)
            if current and current.status == BackfillStatus.CANCELLED:
                logger.info("backfill %s: cancelled mid-flight; skipping remaining windows", parent_id)
                break
            outcome = _process(child)
            if outcome.status == BackfillStatus.FAILED:
                if on_failure == OnFailure.STOP:
                    logger.info("backfill %s: window %s failed; halting per on_failure=stop", parent_id, child.id)
                    break
                if on_failure == OnFailure.RETRY_ONCE:
                    logger.info("backfill %s: retrying window %s once", parent_id, child.id)
                    outcome = _process(child)
                    if outcome.status == BackfillStatus.FAILED:
                        # After the retry, fall through to on_failure rule
                        # — but RETRY_ONCE collapses to CONTINUE post-retry
                        # (don't STOP a backfill on a single window after
                        # we've already retried it).
                        pass
                # CONTINUE / post-retry → keep going.
    else:
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="backfill") as pool:
            futures: dict[Future, BackfillRun] = {}
            for child in children:
                current = store.get(parent_id)
                if current and current.status == BackfillStatus.CANCELLED:
                    break
                futures[pool.submit(_process, child)] = child
            for fut in list(futures.keys()):
                try:
                    outcome = fut.result()
                except Exception:  # noqa: BLE001 — already recorded by _process
                    continue
                if outcome.status == BackfillStatus.FAILED and on_failure == OnFailure.STOP:
                    # Don't cancel in-flight futures — they finish naturally — but
                    # mark the backfill so the polling loop above bails next window.
                    store.cancel(parent_id)

    # Final rollup — _recompute_parent_aggregates was invoked on every child
    # update, so the latest read already has the right aggregate state.
    return store.get(parent_id) or parent


def run_backfill_async(
    parent_id: str,
    *,
    store: BackfillStore,
    executor,
    workflow,
    exe_store,
    extra_params: dict[str, Any] | None = None,
    from_window: int = 0,
) -> threading.Thread:
    """Fire-and-forget wrapper — spawns a daemon thread to run the backfill.

    Returns the Thread so the API layer can hand it to a test that wants
    to ``join()`` and observe the final state. Production callers
    don't need to retain the handle.

    `from_window` (B3, 2026-06-08) - passed through to run_backfill_sync.
    Used by the /resume endpoint to skip windows already completed
    successfully before the previous run failed.
    """
    def _target():
        try:
            run_backfill_sync(
                parent_id,
                store=store, executor=executor, workflow=workflow,
                exe_store=exe_store, extra_params=extra_params,
                from_window=from_window,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("backfill %s: orchestrator crashed", parent_id)
            # Without this, the parent stays RUNNING forever and the user
            # has no signal the backfill died — the UI shows it in-flight
            # while nothing is happening.
            try:
                store.update_status(
                    parent_id,
                    BackfillStatus.FAILED,
                    error_message=f"Orchestrator crashed: {exc}",
                )
            except Exception:
                logger.exception(
                    "backfill %s: could not mark parent FAILED after crash",
                    parent_id,
                )
    t = threading.Thread(
        target=_target, name=f"backfill-{parent_id}", daemon=True,
    )
    t.start()
    return t
