"""Operational health — one alertable surface for the signals an operator pages on.

`/api/health` (liveness) and `/api/health/ready` (readiness) answer "is the app
up?". This answers the different question "is it healthy enough to leave running
unattended?": disk headroom, backup freshness, run-queue pressure, scheduler
liveness, and the recent failure rate.

Design:
  * Every signal is computed **defensively** — any failure yields status
    "unknown", never a 500. A monitoring scraper must be able to poll this
    forever without the endpoint itself becoming an incident.
  * Each signal is scored ok / warn / critical against a threshold that an env
    var can override, so operators tune it to their host without a code change.
  * The top-level `status` is the worst of the signals; "unknown" never raises
    it (a missing backup or absent psutil is informational, not an alarm).

Authenticated: detailed operational topology should not be anonymous (see
docs/security-deployment.md §8). Single-node OSS scope — these are host +
in-process signals, not a distributed control plane.
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends

from fpulse.auth.deps import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ops", tags=["ops"])

# "unknown" deliberately ranks with "ok" so an uncomputable signal never pages.
_RANK = {"ok": 0, "unknown": 0, "warn": 1, "critical": 2}


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, "") or default)
    except (TypeError, ValueError):
        return default


def _sig(status: str, value: Any, detail: str, **extra: Any) -> dict:
    return {"status": status, "value": value, "detail": detail, **extra}


def _disk(data_dir: str) -> dict:
    warn_pct = _f("FPULSE_OPS_DISK_WARN_PCT", 15.0)
    crit_pct = _f("FPULSE_OPS_DISK_CRIT_PCT", 5.0)
    try:
        u = shutil.disk_usage(data_dir)
        free_pct = round(u.free / u.total * 100, 1) if u.total else 0.0
        free_gb = round(u.free / (1024 ** 3), 2)
        status = "critical" if free_pct <= crit_pct else "warn" if free_pct <= warn_pct else "ok"
        return _sig(status, free_pct, f"{free_gb} GB free ({free_pct}%) on the data volume",
                    free_gb=free_gb, warn_below_pct=warn_pct, crit_below_pct=crit_pct)
    except Exception as exc:  # noqa: BLE001
        return _sig("unknown", None, f"disk usage unavailable: {exc}")


def _backup() -> dict:
    warn_h = _f("FPULSE_OPS_BACKUP_WARN_HOURS", 48.0)
    crit_h = _f("FPULSE_OPS_BACKUP_CRIT_HOURS", 168.0)  # 7 days
    try:
        from fpulse.storage.backup_scheduler import BackupScheduler
        latest = (BackupScheduler.get_status() or {}).get("latest_backup")
        if not latest or not latest.get("created_at"):
            # A fresh install has none, and OSS backups are manual — informational.
            return _sig("unknown", None,
                        "no backup found yet (OSS backups are manual — see docs/product_facts/13_backup_recovery.md)")
        created = datetime.fromisoformat(str(latest["created_at"]).replace("Z", "+00:00"))
        age_h = round((datetime.now(timezone.utc) - created).total_seconds() / 3600, 1)
        status = "critical" if age_h >= crit_h else "warn" if age_h >= warn_h else "ok"
        return _sig(status, age_h, f"newest backup is {age_h}h old",
                    warn_after_hours=warn_h, crit_after_hours=crit_h)
    except Exception as exc:  # noqa: BLE001
        return _sig("unknown", None, f"backup status unavailable: {exc}")


def _queue(app_state) -> dict:
    warn_pct = _f("FPULSE_OPS_QUEUE_WARN_PCT", 70.0)
    crit_pct = _f("FPULSE_OPS_QUEUE_CRIT_PCT", 90.0)
    try:
        pool = app_state.get("worker_pool")
        if pool is None:
            return _sig("unknown", None, "worker pool not initialized")
        depth = int(pool._queue.depth())
        cap = int(getattr(pool._governor, "max_queue_depth", 0) or 0) or 1000
        pct = round(depth / cap * 100, 1) if cap else 0.0
        status = "critical" if pct >= crit_pct else "warn" if pct >= warn_pct else "ok"
        return _sig(status, depth, f"{depth}/{cap} jobs queued ({pct}%)", capacity=cap, pct=pct)
    except Exception as exc:  # noqa: BLE001
        return _sig("unknown", None, f"queue depth unavailable: {exc}")


def _scheduler(app_state) -> dict:
    try:
        sched = app_state.get("scheduler")
        if sched is None:
            return _sig("unknown", None, "scheduler not initialized")
        active = len(getattr(sched, "active_jobs", {}) or {})
        if not bool(getattr(sched, "is_running", False)):
            return _sig("critical", False,
                        "scheduler loop is NOT running — scheduled pipelines will not fire", active_jobs=active)
        return _sig("ok", True, f"scheduler running ({active} active job(s))", active_jobs=active)
    except Exception as exc:  # noqa: BLE001
        return _sig("unknown", None, f"scheduler status unavailable: {exc}")


def _failures(app_state) -> dict:
    warn_rate = _f("FPULSE_OPS_FAIL_WARN_PCT", 25.0)
    crit_rate = _f("FPULSE_OPS_FAIL_CRIT_PCT", 50.0)
    window_h = int(_f("FPULSE_OPS_FAIL_WINDOW_HOURS", 24.0))
    try:
        store = app_state.get("execution_store")
        if store is None:
            return _sig("unknown", None, "execution store not initialized")
        stats = store.get_stats(hours=window_h) or {}
        failed = int(stats.get("failed", 0) or 0)
        success = int(stats.get("success", 0) or 0)
        terminal = failed + success
        if terminal == 0:
            return _sig("ok", 0.0, f"no completed runs in the last {window_h}h")
        rate = round(failed / terminal * 100, 1)
        status = "critical" if rate >= crit_rate else "warn" if rate >= warn_rate else "ok"
        return _sig(status, rate, f"{failed}/{terminal} runs failed in the last {window_h}h ({rate}%)",
                    failed=failed, terminal=terminal, window_hours=window_h)
    except Exception as exc:  # noqa: BLE001
        return _sig("unknown", None, f"failure rate unavailable: {exc}")


@router.get("/health", dependencies=[Depends(require_auth)])
async def ops_health() -> dict:
    """Operator health: disk / backup / queue / scheduler / failed-runs.

    Poll it (authenticated) and alert on any signal whose ``status`` is
    ``warn`` or ``critical``; the top-level ``status`` is the worst of them.
    """
    from fpulse.main import app_state
    data_dir = (app_state.get("data_dir") if isinstance(app_state, dict) else None) or "data"

    signals = {
        "disk": _disk(data_dir),
        "backup": _backup(),
        "queue": _queue(app_state),
        "scheduler": _scheduler(app_state),
        "failed_runs": _failures(app_state),
    }
    overall = "ok"
    for s in signals.values():
        if _RANK.get(s.get("status", "unknown"), 0) > _RANK.get(overall, 0):
            overall = s["status"]

    return {
        "status": overall,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "signals": signals,
        "note": ("Single-node OSS operational health. 'unknown' means a signal "
                 "could not be computed (e.g. psutil or a backup is absent), not a failure. "
                 "Thresholds are overridable via FPULSE_OPS_* env vars."),
    }
