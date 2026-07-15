"""
Stage 0 — Memory observability endpoint.

GET /api/health/memory
  Returns an instrumentation-grade snapshot of the F-Pulse process so we can
  measure the "before" state honestly before any architectural changes land,
  and continue to observe the effect of each subsequent stage.

Fields:
  rss_mb, vms_mb          Resident / virtual memory (psutil)
  threads                  Live thread count (psutil)
  pid                      Process id
  uptime_seconds           Wall-clock since process started
  loaded_stores            Keys currently populated in app_state (reveals
                           which stores are actually materialised)
  warmup_status            pending | ok | failed | not_applicable
                           (populated once Stage 2 warmup task lands)
  versions                 python, fastapi, uvicorn, sqlite, platform
  db_files                 [{path, size_mb, wal_mb, shm_mb}] for every
                           SQLite file we can find under data_dir
  host                     cpu_count, total_memory_mb (context, not metric)

Query params:
  trace=true               Capture a tracemalloc top-10 snapshot by size.
                           Off by default — tracemalloc has non-trivial
                           overhead and must be explicitly enabled.

Notes (deliberately excluded per review):
  • open_files / handle count — not reliable/cheap on Windows, skipped
  • pipeline_count, queue_depth, worker utilisation — already exposed on
    /api/pool/status, not duplicated here

This endpoint is READ-ONLY. It allocates almost nothing beyond a psutil
sample and a few filesystem stat() calls. Safe to hit from a health-check
probe at sub-second intervals.
"""

from __future__ import annotations

import os
import sys
import sqlite3
import platform as _platform
import time
import tracemalloc
from pathlib import Path
from typing import Any

import psutil
from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/health", tags=["health"])


# Captured once at module import so uptime is honest even if app_state is
# not yet populated when the endpoint is first hit.
_PROCESS_START = time.time()

# tracemalloc is off by default — started lazily on first trace=true request
# so the cost is only paid when an operator explicitly asks for it.
_TRACEMALLOC_STARTED = False


def _rss_mb(proc: psutil.Process) -> float:
    try:
        return round(proc.memory_info().rss / (1024 * 1024), 1)
    except Exception:
        return 0.0


def _vms_mb(proc: psutil.Process) -> float:
    try:
        return round(proc.memory_info().vms / (1024 * 1024), 1)
    except Exception:
        return 0.0


def _versions() -> dict[str, str]:
    """Version pinning per reviewer guardrail — so behaviour is attributable
    to specific library versions, not just "Windows" or "Python 3.11"."""
    out: dict[str, str] = {
        "python": _platform.python_version(),
        "platform": f"{_platform.system()} {_platform.release()} ({_platform.machine()})",
        "sqlite": sqlite3.sqlite_version,
    }
    try:
        import fastapi  # lazy — only when endpoint is called
        out["fastapi"] = fastapi.__version__
    except Exception:
        out["fastapi"] = "unknown"
    try:
        import uvicorn  # lazy
        out["uvicorn"] = uvicorn.__version__
    except Exception:
        out["uvicorn"] = "unknown"
    return out


def _db_files(data_dir: str) -> list[dict[str, Any]]:
    """Stat every .db/.db-wal/.db-shm inside the data directory so we can see
    WAL growth over time (reviewer 3's operational signal).

    2026-05-28: each entry now also carries ``size_bytes`` (sum of
    main + wal + shm) in addition to the per-file ``*_mb`` rollups.
    The Dashboard's "DB size" tile sums ``f.size_bytes`` to render
    the headline number — previously it summed a missing field and
    always rendered "—" even on installs where the DB was healthy.
    Kept the MB fields for backwards compatibility with the
    /health/memory consumers + tests that pattern on `size_mb`.
    """
    files: dict[str, dict[str, Any]] = {}
    try:
        root = Path(data_dir)
        if not root.is_dir():
            return []
        for p in root.rglob("*.db*"):
            if not p.is_file():
                continue
            base = p.name
            if base.endswith("-wal"):
                key = base[:-4]
                kind = "wal"
            elif base.endswith("-shm"):
                key = base[:-4]
                kind = "shm"
            else:
                key = base
                kind = "main"
            entry = files.setdefault(key, {
                "path": "", "size_mb": 0.0, "wal_mb": 0.0, "shm_mb": 0.0,
                # size_bytes is the canonical byte total (main + wal + shm)
                # — what the dashboard frontend actually reads. Initialised
                # to 0 so a partial entry (e.g. WAL present, main missing)
                # still produces a numeric field rather than KeyError.
                "size_bytes": 0,
            })
            stat_bytes = p.stat().st_size
            size_mb = round(stat_bytes / (1024 * 1024), 2)
            if kind == "main":
                entry["path"] = str(p)
                entry["size_mb"] = size_mb
            elif kind == "wal":
                entry["wal_mb"] = size_mb
                if not entry["path"]:
                    entry["path"] = str(p.with_name(key))
            else:  # shm
                entry["shm_mb"] = size_mb
                if not entry["path"]:
                    entry["path"] = str(p.with_name(key))
            # Roll bytes from every kind into the shared total —
            # main + wal + shm all contribute to on-disk footprint.
            entry["size_bytes"] = entry.get("size_bytes", 0) + stat_bytes
    except Exception:
        pass
    return list(files.values())


def _loaded_stores(app_state: dict) -> list[str]:
    """Just the keys — revealing them is how we'll see Stage 2 feature-flag
    gating actually work (fewer keys present when a feature is disabled)."""
    try:
        return sorted(app_state.keys())
    except Exception:
        return []


def _warmup_status(app_state: dict) -> str:
    """Stage 2 sets app_state['warmup_status'] to pending|ok|failed.
    Reports not_applicable when the warmup task hasn't been scheduled
    (e.g. running standalone health checks before lifespan)."""
    return app_state.get("warmup_status", "not_applicable")


def _warmup_error(app_state: dict) -> str | None:
    """Populated only when warmup_status == 'failed' — gives the operator
    the exception type and message without needing to grep the logs."""
    return app_state.get("warmup_error")


def _flags_snapshot() -> dict[str, bool]:
    """Stage 2 — operator visibility on which optional features are
    actually active in this process. Empty dict on import errors so a
    misconfigured install never breaks the health endpoint."""
    try:
        from fpulse.feature_flags import snapshot
        return snapshot()
    except Exception:
        return {}


def _wal_stats(app_state: dict) -> dict[str, Any]:
    """Stage 3a — operator-visible SQLite WAL state.

    Returns the journal mode, db size in pages, current WAL pages
    (rises with writes, falls with auto-checkpoint), and the
    auto-checkpoint threshold. A WAL that grows monotonically across
    repeated calls signals a stuck reader or writer and is the kind
    of operational problem an operator wants to see early.

    Empty dict if the DB hasn't been instantiated yet (pre-lifespan
    or in tests) — health endpoint must never crash on missing state.
    """
    db = app_state.get("db")
    if db is None or not hasattr(db, "wal_stats"):
        return {}
    try:
        return db.wal_stats()
    except Exception as exc:
        return {"error": str(exc)}


def _pg_status(app_state: dict) -> dict[str, Any]:
    """Stage 3b — surface whether the PostgreSQL handle is configured
    and (when configured) its pool stats. Returns an explicit
    {'configured': false} so operators can see at a glance whether
    they're on the SQLite or Postgres path.

    Does NOT actually probe the DB on every health call (would be
    expensive). Use the dedicated /api/health/ready endpoint for the
    SELECT-1 ping when needed.
    """
    pg = app_state.get("pg")
    if pg is None:
        return {"configured": False}
    return {
        "configured": True,
        "initialised": getattr(pg, "_initialised", False),
        "url": _safe_pg_url(pg),
    }


def _safe_pg_url(pg: Any) -> str:
    try:
        from fpulse.storage.database_pg import _redact_url
        return _redact_url(getattr(pg, "db_url", ""))
    except Exception:
        return "<hidden>"


def _tracemalloc_top(n: int = 10) -> list[dict[str, Any]]:
    """Top N allocations by size. Starts tracemalloc lazily on first call."""
    global _TRACEMALLOC_STARTED
    if not _TRACEMALLOC_STARTED:
        tracemalloc.start()
        _TRACEMALLOC_STARTED = True
        # First snapshot is empty — the user should hit the endpoint twice.
        return [{"note": "tracemalloc just started — hit endpoint again to get a real snapshot"}]

    try:
        snap = tracemalloc.take_snapshot()
        stats = snap.statistics("lineno")[:n]
        return [
            {
                "source": str(s.traceback),
                "size_mb": round(s.size / (1024 * 1024), 3),
                "count": s.count,
            }
            for s in stats
        ]
    except Exception as exc:
        return [{"error": str(exc)}]


@router.get("/memory")
async def memory_snapshot(request: Request, trace: bool = False) -> dict:
    """Instrumentation snapshot — the Stage 0 baseline source of truth.

    Hit this endpoint:
      • Right after startup  → idle RSS baseline
      • After one pipeline   → per-pipeline marginal cost
      • After N pipelines    → concurrent scaling behaviour
      • After idle hold      → leak detection (RSS drifting up with no work)

    Query:
      /api/health/memory            — plain snapshot
      /api/health/memory?trace=true — adds tracemalloc top-10 allocations

    Returns JSON. Stable field names — safe to script against.
    """
    proc = psutil.Process()

    # app_state is populated in main.py; we read it defensively in case this
    # endpoint is hit before lifespan startup completes.
    app_state: dict = getattr(request.app.state, "fpulse_state", None) or {}
    if not app_state:
        # Fallback for pre-Stage-1 code where app_state is still a module global.
        try:
            from fpulse.main import app_state as _module_app_state  # noqa
            app_state = _module_app_state
        except Exception:
            app_state = {}

    data_dir = app_state.get("data_dir") or os.environ.get("FPULSE_DATA_DIR", "")

    payload: dict[str, Any] = {
        "rss_mb": _rss_mb(proc),
        "vms_mb": _vms_mb(proc),
        "threads": proc.num_threads() if hasattr(proc, "num_threads") else 0,
        "pid": proc.pid,
        "uptime_seconds": round(time.time() - _PROCESS_START, 1),
        "loaded_stores": _loaded_stores(app_state),
        "warmup_status": _warmup_status(app_state),
        # Stage 2.5: which warmup path ran ("heavy" pre-imports duckdb +
        # registry; "light" defers them to first use). Set when warmup
        # task runs; absent before that.
        "warmup_mode": app_state.get("warmup_mode"),
        # Stage 2: operator-visible feature-flag snapshot. Disabled
        # features should also be visibly absent from loaded_stores.
        "flags": _flags_snapshot(),
        # Stage 3a: SQLite WAL state. Operators watch wal_pages over
        # time — monotonic growth = stuck reader / writer, drops to
        # ~0 after each auto-checkpoint.
        "wal": _wal_stats(app_state),
        # Stage 3b: PostgreSQL configuration state. {"configured":false}
        # on OSS; populated when FPULSE_DB_URL is set and the `pg` extra
        # is installed.
        "pg": _pg_status(app_state),
        "versions": _versions(),
        "db_files": _db_files(data_dir) if data_dir else [],
        "host": {
            "cpu_count": psutil.cpu_count(logical=True) or 0,
            "total_memory_mb": round(psutil.virtual_memory().total / (1024 * 1024)),
            "available_memory_mb": round(psutil.virtual_memory().available / (1024 * 1024)),
        },
    }

    # Stage 2: surface warmup_error ONLY when warmup actually failed,
    # so a healthy snapshot stays uncluttered.
    err = _warmup_error(app_state)
    if err is not None:
        payload["warmup_error"] = err

    if trace:
        payload["tracemalloc_top10"] = _tracemalloc_top(10)

    return payload
