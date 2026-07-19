"""ExecutionManager admin API — Sprint 2 PR5 step 5.

Exposes the manager's introspection surface for the admin UI and
`watchdog.ps1`:

  GET  /api/admin/execution/stats   Tier, caps, per-kind counts, pool status
  GET  /api/admin/execution/inspect Registry entries (optionally filtered)
  POST /api/admin/execution/reap    Run an orphan/leak sweep, return the report
  GET  /api/admin/execution/governor/snapshot  Raw governor reading

`/reap` is POST because it mutates state (kills orphans). All endpoints
require an admin session token.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Any

from fpulse.auth.deps import require_admin

# Admin-only: /reap mutates state and /inspect exposes run internals.
router = APIRouter(
    prefix="/api/admin/execution",
    tags=["execution-manager"],
    dependencies=[Depends(require_admin)],
)


def _get_manager():
    """Lazy import — keeps api/__init__.py free of runtime dependencies
    on engine internals. Raises 503 if the manager isn't initialized
    (e.g., during early warmup)."""
    from fpulse.main import app_state
    mgr = app_state.get("execution_manager")
    if mgr is None:
        raise HTTPException(503, "ExecutionManager not initialized")
    return mgr


@router.get("/stats")
async def get_stats() -> dict[str, Any]:
    """Return current ExecutionStats. Consumed by the admin overview
    card and by /metrics."""
    mgr = _get_manager()
    stats = mgr.stats()
    return {
        "tier": stats.tier,
        "by_kind": stats.by_kind,
        "caps": stats.caps,
        "pool": stats.pool_status.get("pool", {}),
        "counters": stats.pool_status.get("counters", {}),
    }


@router.get("/inspect")
async def inspect_registry(
    owner: str | None = Query(None, description="Filter by owner (pipeline_id, etc.)"),
) -> dict[str, Any]:
    """Return the registry entries, optionally filtered. Useful for
    debugging why a kind's count is non-zero."""
    mgr = _get_manager()
    records = mgr.inspect(owner=owner)
    return {
        "count": len(records),
        "records": [
            {
                "id": r.handle.id,
                "kind": r.handle.kind,
                "owner": r.handle.owner,
                "pid": r.handle.pid,
                "parent_pid": r.handle.parent_pid,
                "started_at": r.handle.started_at.isoformat() if r.handle.started_at else None,
                "status": r.status,
                "children": r.children,
                "memory_peak_mb": r.memory_peak_mb,
                "runtime_ms": r.runtime_ms,
                "attempts": r.attempts,
                "exit_reason": r.exit_reason,
                "underlying_id": r.handle.underlying_id,
            }
            for r in records
        ],
    }


@router.post("/reap")
async def trigger_reap() -> dict[str, Any]:
    """Walk the process tree, kill orphans, sweep leaks. Called every
    30 s by `watchdog.ps1`; also available in the admin UI for manual
    verification."""
    mgr = _get_manager()
    report = mgr.reap()
    return {
        "orphans_killed": report.orphans_killed,
        "leaks_swept": report.leaks_swept,
        "checked_at": report.checked_at.isoformat(),
    }


@router.get("/governor/snapshot")
async def governor_snapshot() -> dict[str, Any]:
    """Raw GlobalResourceGovernor reading. Includes active tier,
    sampled percentages, and the configured thresholds. The admin UI
    shows this on the Execution Manager card."""
    mgr = _get_manager()
    governor = getattr(mgr, "_governor", None)
    if governor is None:
        return {"available": False, "active_tier": "green"}
    return governor.snapshot()


@router.post("/{handle_id}/cancel")
async def cancel_handle(handle_id: str) -> dict[str, Any]:
    """Cancel a live task by its registry ID. Flagged as TBD in the
    original runbook; lands now to close the admin surface.

    Cancel semantics per kind:
      - pipeline: WorkerPool.cancel() — removes from queue if still
        queued, attempts to cancel the future if running.
      - subprocess: SubprocessRunner.cancel() — SIGTERM → 3 s grace
        → SIGKILL via the tree-teardown dance.
      - thread / scheduled: sets the cooperative stop_event. The fn
        must poll it to actually exit (Python can't forcibly kill
        threads without risking corrupted state).
      - asyncio: task.cancel() — raises CancelledError at next await.

    Response body:
      {ok: bool, reason: str, handle_id: str}

    Returns 404 when the handle ID isn't in the registry (already
    finished or never existed)."""
    mgr = _get_manager()
    ok, reason = mgr.cancel_by_id(handle_id)
    if not ok and reason == "handle not found in registry":
        raise HTTPException(404, reason)
    return {"ok": ok, "reason": reason, "handle_id": handle_id}


@router.get("/{handle_id}/logs")
async def tail_logs(
    handle_id: str,
    stream: str = Query("stdout", pattern="^(stdout|stderr)$"),
    tail: int = Query(200, ge=1, le=10_000),
) -> dict[str, Any]:
    """Return the last N lines of a subprocess's stdout or stderr log.

    Step 6: logs are disk-backed. This endpoint tails the file without
    loading the whole thing — the UI can poll while the process is
    still running."""
    import os

    mgr = _get_manager()
    records = mgr.inspect()
    record = next((r for r in records if r.handle.id == handle_id), None)
    if record is None:
        raise HTTPException(404, f"Handle {handle_id} not found")

    path = record.stdout_log_path if stream == "stdout" else record.stderr_log_path
    if not path or not os.path.isfile(path):
        return {
            "handle_id": handle_id,
            "stream": stream,
            "path": path,
            "exists": False,
            "lines": [],
        }

    # Cheap tail: read the whole file only if it's small; else seek
    # backwards in ~32 KB chunks until we have enough newlines.
    file_size = os.path.getsize(path)
    chunk = 32 * 1024
    lines: list[str] = []
    try:
        with open(path, "rb") as fh:
            if file_size <= chunk:
                data = fh.read()
            else:
                data = b""
                offset = file_size
                while offset > 0 and data.count(b"\n") <= tail:
                    step = min(chunk, offset)
                    offset -= step
                    fh.seek(offset)
                    data = fh.read(step) + data
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()[-tail:]
    except OSError as exc:
        import logging
        logging.getLogger(__name__).exception("Log read failed")
        raise HTTPException(500, "Log read failed") from exc

    return {
        "handle_id": handle_id,
        "stream": stream,
        "path": path,
        "exists": True,
        "total_bytes": file_size,
        "lines": lines,
    }
