"""Execution Pool API — Spark-style worker pool monitoring.

Exposes real-time pool status, worker utilization, queue depth,
and execution history for the admin Execution Pool page.

Note (2026-05-21): /pool/history now reads from the persistent
ExecutionStore (SQLite), not the in-memory WorkerPool._history. The
in-memory list was being wiped on every backend restart, which is the
opposite of what users expect from a "Run History" tab. Overview and
status endpoints still read from the live pool — they describe the
present (workers, queue), not the past (runs that finished).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from fpulse.auth.deps import current_workspace_id

router = APIRouter(prefix="/api/pool", tags=["execution-pool"])


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception:
        return "default"


def _get_pool():
    from fpulse.main import app_state
    pool = app_state.get("worker_pool")
    if pool is None:
        raise HTTPException(503, "Worker pool not initialized")
    return pool


@router.get("/status")
async def pool_status():
    """Return full worker pool status — workers, queue, counters, throughput.

    This is the primary endpoint for the Execution Pool admin page.
    Auto-refreshes every 2 seconds on the frontend.
    """
    pool = _get_pool()
    return pool.get_status()


# In-process cache for the pool→executions join (Pass 2, May 10 2026).
# Completed runs are immutable; once we've fetched a run's metadata
# (peak_memory_mb, cpu_seconds, error_message) we never need to re-query
# the executions store. Bounded at the same 500 limit as pool._history
# so the cache size tracks the data set it joins against.
_RUN_META_CACHE: dict[str, dict] = {}
_RUN_META_CACHE_MAX = 500


def _enrich_history_row(row: dict) -> dict:
    """Look up execution-store metadata for a pool history row and
    merge in the per-run resource fields. Cached after first lookup;
    safe to call on every poll because completed runs never change.
    """
    run_id = row.get("id")
    if not run_id:
        return row
    cached = _RUN_META_CACHE.get(run_id)
    if cached is not None:
        return {**row, **cached}
    extra: dict = {}
    try:
        from fpulse.main import app_state as _as
        store = _as.get("execution_store")
        if store is None:
            return row
        # ExecutionStore exposes get(id) for a stored execution; we
        # only need .metadata + .error_message off the result.
        exe = None
        try:
            exe = store.get(run_id)
        except Exception:
            exe = None
        if exe is not None:
            md = getattr(exe, "metadata", None) or {}
            if isinstance(md, dict):
                if "peak_memory_mb" in md:
                    extra["peak_memory_mb"] = md.get("peak_memory_mb")
                if "cpu_seconds" in md:
                    extra["cpu_seconds"] = md.get("cpu_seconds")
            err = getattr(exe, "error_message", None)
            # Pool history already carries `error` (set by worker_pool on
            # exception), but the executions store has the cleaned
            # error_message produced by the executor — prefer that when
            # present.
            if err and not row.get("error"):
                extra["error"] = err
    except Exception:
        pass
    # Cache even an empty result so we don't hammer the store on rows
    # that have no joinable execution. Evict an arbitrary entry when
    # the cap is hit (FIFO via dict iteration).
    if len(_RUN_META_CACHE) >= _RUN_META_CACHE_MAX:
        try:
            _RUN_META_CACHE.pop(next(iter(_RUN_META_CACHE)))
        except StopIteration:
            pass
    _RUN_META_CACHE[run_id] = extra
    return {**row, **extra}


PRIORITY_LABELS = {1: "P1", 2: "P2", 3: "P3", 4: "P4", 5: "P5"}


def _execution_to_history_row(exe) -> dict:
    """Project an ExecutionRecord into the dict shape the Pool > Run
    History frontend expects. Fields the frontend reads:

      id, workflow_id, workflow_name, status, environment,
      priority, priority_label, triggered_by, worker_id,
      queued_at, started_at, completed_at,
      duration_ms, wait_ms, rows_processed, steps,
      peak_memory_mb, cpu_seconds, error

    Backward-compat: matches the keys the previous pool._history
    output used so the frontend column reads keep working unchanged.
    """
    metadata = getattr(exe, "metadata", None) or {}
    if not isinstance(metadata, dict):
        metadata = {}

    started = getattr(exe, "started_at", None)
    completed = getattr(exe, "completed_at", None)
    started_iso = started.isoformat() if started else ""
    completed_iso = completed.isoformat() if completed else ""

    # Priority isn't on the ExecutionRecord today — fall back to P3
    # (Normal) so the column has a value. Metadata can override it
    # when set by the scheduler.
    priority = int(metadata.get("priority", 3) or 3)

    # Resource metrics live in TWO places depending on the execution path:
    #   subprocess_runner — top-level ExecutionRecord.memory_peak_mb +
    #                        runtime_ms (used when isolation is enabled).
    #   RealtimeExecutor  — metadata["peak_memory_mb"] + metadata["cpu_seconds"]
    #                        (the default in-process path the OSS UI hits).
    # We check top-level first, then metadata. Both are populated by the
    # ResourceMonitor context manager wrapped around the executor in
    # every execution path (api/execution.py for manual runs,
    # scheduling/scheduler.py for scheduled runs). When ResourceMonitor
    # has nothing to report (no psutil installed, or pre-2026-06-02
    # records where the scheduler didn't wrap), both fields stay None
    # and the UI shows "—" instead of a misleading derived value.
    # 2026-06-02 removed the `runtime_ms → cpu_seconds` fallback —
    # wall-clock duration ≠ CPU-seconds (different concept across cores)
    # and conflating them displayed wrong numbers.
    peak_mem = getattr(exe, "memory_peak_mb", None)
    if peak_mem is None:
        peak_mem = metadata.get("peak_memory_mb")
    cpu_sec = metadata.get("cpu_seconds")

    # Step logs are stored as a list on the record; surface the count
    # for the Steps column.
    step_logs = getattr(exe, "step_logs", None) or []
    rows_processed = sum(int(s.rows_processed or 0) for s in step_logs)
    steps_total = int(getattr(exe, "steps_total", 0) or len(step_logs))

    return {
        "id": exe.id,
        "workflow_id": exe.workflow_id,
        "workflow_name": getattr(exe, "workflow_name", "") or "",
        "priority": priority,
        "priority_label": PRIORITY_LABELS.get(priority, ""),
        "environment": metadata.get("environment", "dev"),
        "status": exe.status,
        # Queued isn't tracked — point at started so the column has a value.
        "queued_at": started_iso,
        "started_at": started_iso,
        "completed_at": completed_iso,
        "duration_ms": float(getattr(exe, "duration_ms", 0) or 0),
        "wait_ms": 0,
        "worker_id": metadata.get("worker_id", "") or "",
        "triggered_by": getattr(exe, "triggered_by", "manual") or "manual",
        "error": getattr(exe, "error_message", None) or "",
        "rows_processed": rows_processed,
        "steps": steps_total,
        "peak_memory_mb": peak_mem,
        "cpu_seconds": cpu_sec,
    }


@router.get("/history")
async def pool_history(limit: int = 100, workspace_id: str = Depends(_safe_workspace_id)):
    """Return recent execution history from the persistent execution
    store (NOT the in-memory pool._history, which was wiped on every
    backend restart).

    Scope: workspace-bound, matching every other history surface in
    the app (Executions page, Activity page). For pre-restart runs
    still in SQLite this is the only path that surfaces them.

    The peak_memory_mb + cpu_seconds columns come straight off the
    persisted ExecutionRecord — no in-process cache layer needed.
    """
    from fpulse.main import app_state
    exe_store = app_state.get("execution_store")
    if exe_store is None:
        raise HTTPException(503, "Execution store not initialized")

    # ExecutionStore.list_all returns raw dicts (JSON blobs). Rehydrate
    # via ExecutionRecord so the same projector handles every field
    # consistently with the persisted shape.
    from fpulse.monitoring.store import ExecutionRecord
    raw = exe_store.list_all(limit=limit, workspace_id=workspace_id)
    out = []
    for r in raw:
        try:
            exe = ExecutionRecord(**r)
            out.append(_execution_to_history_row(exe))
        except Exception:
            # Skip a malformed row rather than 500ing the whole page.
            continue
    return out


@router.get("/connections")
async def connection_pool_stats():
    """Return connection-pool stats — total cached driver connections,
    breakdown by connection_id and run_id.

    The pool is a per-run cache (Critical #5 / Phase 2-5) that amortises
    DB connection setup across steps. This endpoint surfaces its live
    state so operators can see whether the pool is helping (entries =
    long-lived runs reusing connections) or saturated (cap warnings in
    logs). When the pool isn't installed, returns a sentinel response
    rather than 503 — the page can still render its 'Connection pool
    not installed' card.
    """
    from fpulse.main import app_state
    pool = app_state.get("connection_pool") if isinstance(app_state, dict) else None
    if pool is None:
        return {
            "installed": False,
            "total_entries": 0,
            "by_connection": {},
            "by_run": {},
            "max_per_connection": 0,
        }
    s = pool.stats()
    return {
        "installed": True,
        "total_entries": s.total_entries,
        "by_connection": s.by_connection,
        "by_run": s.by_run,
        "max_per_connection": pool._max,
    }


@router.post("/cancel/{job_id}")
async def cancel_job(job_id: str):
    """Cancel a queued or running job by ID."""
    pool = _get_pool()
    cancelled = pool.cancel(job_id)
    if not cancelled:
        raise HTTPException(404, "Job not found or already completed")
    return {"cancelled": True, "job_id": job_id}


@router.get("/config")
async def pool_config():
    """Return current pool configuration — workers, memory, threads,
    governor tier, and spill-disk health."""
    from fpulse import runtime_config
    from fpulse.main import app_state
    import multiprocessing

    pool = _get_pool()

    try:
        import psutil
        mem = psutil.virtual_memory()
        cpu_pct = psutil.cpu_percent(interval=0)
        total_ram_gb = round(mem.total / 1073741824, 1)
        used_ram_gb = round(mem.used / 1073741824, 1)
        avail_ram_gb = round(mem.available / 1073741824, 1)
        try:
            io_wait_pct = float(getattr(psutil.cpu_times_percent(interval=0), "iowait", 0.0))
        except Exception:
            io_wait_pct = 0.0
    except ImportError:
        cpu_pct = 0
        total_ram_gb = 0
        used_ram_gb = 0
        avail_ram_gb = 0
        io_wait_pct = 0.0

    governor_block = None
    governor = app_state.get("global_governor") if isinstance(app_state, dict) else None
    if governor is not None:
        try:
            sample = governor.sample()
            governor_block = {
                "tier": getattr(sample.tier, "value", str(sample.tier)),
                "mem_pct": round(sample.mem_pct, 1),
                "cpu_pct": round(sample.cpu_pct, 1),
                "sampled_at": sample.sampled_at.isoformat(),
                "explanation": _governor_explanation(
                    getattr(sample.tier, "value", str(sample.tier)),
                    sample.mem_pct,
                    sample.cpu_pct,
                ),
            }
        except Exception as exc:
            governor_block = {"tier": "unknown", "error": str(exc)}

    spill_dir = runtime_config.DUCKDB_TEMP_DIRECTORY
    disk_type = _detect_disk_type(spill_dir)

    return {
        "max_workers": pool.max_workers,
        "cpu_cores": multiprocessing.cpu_count() or 4,
        "cpu_percent": cpu_pct,
        "io_wait_percent": round(io_wait_pct, 1),
        "duckdb_memory_limit": runtime_config.DUCKDB_MEMORY_LIMIT,
        "duckdb_threads": runtime_config.DUCKDB_THREADS,
        "duckdb_temp_dir": spill_dir,
        "mode": runtime_config.MODE,
        "ram": {
            "total_gb": total_ram_gb,
            "used_gb": used_ram_gb,
            "available_gb": avail_ram_gb,
        },
        "max_memory_per_worker": runtime_config.DUCKDB_MEMORY_LIMIT,
        "theoretical_max_ram": f"{pool.max_workers} workers x {runtime_config.DUCKDB_MEMORY_LIMIT} = {pool.max_workers * 4} GB worst case",
        "governor": governor_block,
        "spill": {
            "directory": spill_dir,
            "disk_type": disk_type,
            "io_wait_percent": round(io_wait_pct, 1),
            "io_wait_status": (
                "healthy" if io_wait_pct < 10
                else "elevated" if io_wait_pct < 25
                else "saturated"
            ),
        },
    }


def _governor_explanation(tier: str, mem_pct: float, cpu_pct: float) -> str:
    """Return a one-line human explanation of the governor tier."""
    t = (tier or "").lower()
    if t == "green":
        return f"Healthy — accepting all jobs (memory {mem_pct:.0f}%, CPU {cpu_pct:.0f}%)."
    if t == "yellow":
        if cpu_pct >= 85:
            return f"Throttling — CPU at {cpu_pct:.0f}%. New jobs queued; non-queueable spawns rejected."
        return f"Throttling — memory at {mem_pct:.0f}%. New jobs queued; non-queueable spawns rejected."
    if t == "orange":
        return f"High pressure — memory at {mem_pct:.0f}%. Reducers active; rejecting non-queueable spawns."
    if t == "red":
        return f"Critical — memory at {mem_pct:.0f}%. Rejecting all spawns until pressure relieves."
    return "Status unknown."


def _detect_disk_type(path: str) -> str:
    """Best-effort SSD/HDD detection. Returns 'ssd', 'hdd', or 'unknown'."""
    import os
    if not os.path.exists(path):
        return "unknown"
    try:
        import platform
        system = platform.system().lower()
        if system == "linux":
            stat = os.stat(path)
            major = os.major(stat.st_dev)
            for blk in os.listdir("/sys/block"):
                try:
                    with open(f"/sys/block/{blk}/dev") as f:
                        bm = f.read().strip().split(":")
                        if int(bm[0]) == major:
                            with open(f"/sys/block/{blk}/queue/rotational") as r:
                                rotational = r.read().strip()
                            return "hdd" if rotational == "1" else "ssd"
                except Exception:
                    continue
            return "unknown"
        return "unknown"
    except Exception:
        return "unknown"
