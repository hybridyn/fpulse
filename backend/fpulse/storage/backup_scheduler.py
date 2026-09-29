"""Backup scheduler — persistent settings + status surface.

Z26 (2026-05-23). The `api/backup.py` router references this module's
``BackupScheduler.get_settings()`` / ``save_settings()`` / ``get_status()``
helpers; before today the module didn't exist and those endpoints would
500 on import. This file lands the minimum required to make the
Settings → Backup UI work in OSS:

  * settings persisted at ``<data_dir>/backup_settings.json``
  * ``get_status()`` walks the local backups/ directory to surface the
    latest snapshot (mtime + size) plus the next-scheduled timestamp
    computed from the saved schedule

The scheduled-triggering daemon is ``BackupSchedulerDaemon`` (below). When
started from the app lifespan it wakes every 60s and, when the saved schedule
is enabled and a due slot has passed without a backup since, runs
``fpulse.storage.backup.backup_database()`` and prunes to the configured
retention count. OSS users get:

  * scheduled backups (hourly / daily@HH:MM / weekly, UTC) via the daemon
  * manual "Backup now" via POST /api/backup/create
  * startup-time snapshot via fpulse.storage.backup.backup_database()

The daemon fires a due slot at most once (it checks the newest backup's
timestamp) and catches up a slot missed while the app was down once, on the
next tick after start — it does not stack multiple missed slots.

Why a JSON file (vs. SQLite row)? Backup config is one row per install,
not one row per workspace, and it must be readable when the database is
being restored / corrupt / missing. A small flat file keeps the recovery
story honest.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# Default schedule — disabled by default. Users must opt in.
_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "frequency": "daily",     # hourly | daily | weekly
    "daily_time": "02:00",    # HH:MM, interpreted as UTC
    "weekly_day": 0,          # 0=Monday … 6=Sunday
    "retention_count": 5,
    "provider": {
        # OSS default: local backups under <data_dir>/backups/. The
        # `backup_dir` field is consumed by storage/providers.py's local
        # provider; empty string means "default location".
        "provider": "local",
        "backup_dir": "",
    },
}


def _settings_path() -> str:
    """Resolve where the settings JSON lives. We pull data_dir from
    app_state when available (production path); fall back to ``data/``
    under cwd for very early-startup callers."""
    try:
        from fpulse.main import app_state  # type: ignore
        data_dir = app_state.get("data_dir") or "data"
    except Exception:
        data_dir = "data"
    return os.path.join(data_dir, "backup_settings.json")


class BackupScheduler:
    """Static facade — no instance state. The settings file is the
    source of truth; everything reads/writes through this class."""

    @staticmethod
    def get_settings() -> dict[str, Any]:
        """Load persisted settings; fall back to defaults if missing or
        unparsable. Never raises — a corrupt file shouldn't take the API
        down. The caller is free to mutate the returned dict."""
        path = _settings_path()
        if not os.path.isfile(path):
            return dict(_DEFAULTS)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            # Backfill any missing keys so callers can rely on the full
            # shape. New fields added later default to _DEFAULTS values.
            merged = dict(_DEFAULTS)
            merged.update(loaded if isinstance(loaded, dict) else {})
            # Provider needs the same shallow-merge treatment.
            prov = dict(_DEFAULTS["provider"])
            prov.update(merged.get("provider") or {})
            merged["provider"] = prov
            return merged
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("backup settings: failed to load %s (%s) — using defaults", path, exc)
            return dict(_DEFAULTS)

    @staticmethod
    def save_settings(settings: dict[str, Any]) -> None:
        """Persist settings to disk. Creates the data dir if needed.
        Atomically writes via a temp file + rename so a crash mid-write
        doesn't leave a half-written JSON file behind."""
        path = _settings_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError as exc:
            logger.warning("backup settings: cannot create dir for %s (%s)", path, exc)
            return
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(settings, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("backup settings: write failed (%s)", exc)
            # Best-effort cleanup of the tmp file.
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    @staticmethod
    def get_status() -> dict[str, Any]:
        """Combined status surface for the UI. Includes:

          - the current schedule
          - the most recent local backup (mtime + size_bytes)
          - the next scheduled run (computed from frequency + clock)
          - the count of backups currently retained
        """
        settings = BackupScheduler.get_settings()
        backups_dir = _resolve_backups_dir(settings)
        latest, count = _scan_local_backups(backups_dir)
        return {
            "settings": settings,
            "backups_dir": backups_dir,
            "latest_backup": latest,
            "backup_count": count,
            # scheduler_active reflects whether the backup daemon loop is
            # actually running in THIS process; next_backup_at is the real
            # next slot only when it is — so the UI never advertises a run
            # nothing will honor.
            "scheduler_active": _DAEMON_RUNNING,
            "next_backup_at": (
                _compute_next_run(settings)
                if (_DAEMON_RUNNING and settings.get("enabled")) else None
            ),
        }


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _resolve_backups_dir(settings: dict[str, Any]) -> str:
    """Resolve the absolute path where local backups land. If the
    user configured an explicit backup_dir under provider, honor it;
    otherwise default to <data_dir>/backups/."""
    prov = settings.get("provider") or {}
    explicit = (prov.get("backup_dir") or "").strip()
    if explicit:
        return os.path.abspath(explicit)
    try:
        from fpulse.main import app_state  # type: ignore
        data_dir = app_state.get("data_dir") or "data"
    except Exception:
        data_dir = "data"
    return os.path.abspath(os.path.join(data_dir, "backups"))


def _scan_local_backups(backups_dir: str) -> tuple[dict[str, Any] | None, int]:
    """Return (latest backup metadata, total count). The latest
    metadata is None when no backups exist yet."""
    if not os.path.isdir(backups_dir):
        return None, 0
    entries: list[tuple[float, str, int]] = []
    try:
        for fname in os.listdir(backups_dir):
            full = os.path.join(backups_dir, fname)
            if not os.path.isfile(full):
                continue
            # Filter to actual backup snapshots (skip stray companion
            # files like .db-wal). The convention in storage/backup.py
            # is <prefix>_<timestamp>.db.
            if not fname.endswith(".db"):
                continue
            try:
                stat = os.stat(full)
            except OSError:
                continue
            entries.append((stat.st_mtime, fname, stat.st_size))
    except OSError:
        return None, 0
    if not entries:
        return None, 0
    entries.sort(key=lambda t: t[0], reverse=True)
    mtime, fname, size = entries[0]
    return (
        {
            "name": fname,
            "size_bytes": size,
            "created_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        },
        len(entries),
    )


def _compute_next_run(settings: dict[str, Any]) -> str | None:
    """Compute the next scheduled run time as ISO UTC. Returns None if
    scheduling is disabled — the UI then renders "manual only"."""
    if not settings.get("enabled"):
        return None
    freq = settings.get("frequency") or "daily"
    now = datetime.now(timezone.utc)
    if freq == "hourly":
        # Next top of the hour from now.
        nxt = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        return nxt.isoformat()
    # daily / weekly share a target time-of-day.
    daily = (settings.get("daily_time") or "02:00").strip()
    try:
        hh, mm = [int(p) for p in daily.split(":", 1)]
        hh = max(0, min(23, hh))
        mm = max(0, min(59, mm))
    except (ValueError, TypeError):
        hh, mm = 2, 0
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if freq == "weekly":
        target_day = settings.get("weekly_day", 0)
        try:
            target_day = max(0, min(6, int(target_day)))
        except (ValueError, TypeError):
            target_day = 0
        # Days ahead until target weekday (Monday=0 .. Sunday=6).
        delta_days = (target_day - candidate.weekday()) % 7
        candidate = candidate + timedelta(days=delta_days)
        if candidate <= now:
            candidate = candidate + timedelta(days=7)
        return candidate.isoformat()
    # daily — bump to tomorrow if today's slot has passed.
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate.isoformat()


# ─────────────────────────────────────────────────────────────────────
# Scheduled-backup daemon
# ─────────────────────────────────────────────────────────────────────

# Set by BackupSchedulerDaemon.start()/stop() so get_status() can report
# whether scheduled backups are actually being driven in this process.
_DAEMON_RUNNING = False


def _parse_hhmm(value: Any, default: tuple[int, int] = (2, 0)) -> tuple[int, int]:
    """Parse "HH:MM" (UTC) into (hour, minute), clamped; garbage → default."""
    try:
        hh, mm = (int(p) for p in str(value).split(":", 1))
        return max(0, min(23, hh)), max(0, min(59, mm))
    except (ValueError, TypeError):
        return default


def _due_now(settings: dict[str, Any], latest_at: datetime | None, now: datetime) -> bool:
    """True when the current schedule slot has arrived and no backup has been
    taken since it. Fires a slot at most once; catches a slot missed while the
    app was down once (on the next tick), but never stacks missed slots."""
    freq = settings.get("frequency") or "daily"
    if freq == "hourly":
        slot = now.replace(minute=0, second=0, microsecond=0)
    elif freq == "weekly":
        try:
            target_day = max(0, min(6, int(settings.get("weekly_day", 0))))
        except (ValueError, TypeError):
            target_day = 0
        if now.weekday() != target_day:
            return False
        hh, mm = _parse_hhmm(settings.get("daily_time", "02:00"))
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    else:  # daily
        hh, mm = _parse_hhmm(settings.get("daily_time", "02:00"))
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    if now < slot:
        return False
    return latest_at is None or latest_at < slot


def _resolve_db_path() -> str | None:
    """The SQLite path to back up — ``data_dir/fpulse.db`` (Database's default)."""
    try:
        from fpulse.main import app_state  # type: ignore
        data_dir = (app_state.get("data_dir") if isinstance(app_state, dict) else None) or "data"
        return os.path.join(data_dir, "fpulse.db")
    except Exception:
        return None


class BackupSchedulerDaemon:
    """Background thread that runs the saved backup schedule.

    Wakes every ``interval_seconds`` (default 60). On each tick, if the saved
    schedule is enabled and a slot is due, it runs ``backup_database`` and
    prunes to the configured retention count. Best-effort throughout: a tick
    failure is logged and the loop continues, so a transient error never
    silences the schedule permanently.
    """

    def __init__(self, interval_seconds: int = 60):
        self._interval = max(5, int(interval_seconds))
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="fpulse-backup-scheduler", daemon=True,
        )
        self._thread.start()
        global _DAEMON_RUNNING
        _DAEMON_RUNNING = True
        logger.info("Backup scheduler daemon started (interval=%ds)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=5)
        self._thread = None
        global _DAEMON_RUNNING
        _DAEMON_RUNNING = False
        logger.info("Backup scheduler daemon stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        # Tick immediately on start, then every interval.
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 — a tick must never kill the loop
                logger.warning("Backup scheduler tick failed (non-fatal): %s", exc)
            self._stop.wait(self._interval)

    def _tick(self) -> None:
        settings = BackupScheduler.get_settings()
        if not settings.get("enabled"):
            return
        backups_dir = _resolve_backups_dir(settings)
        latest, _count = _scan_local_backups(backups_dir)
        latest_at: datetime | None = None
        if latest and latest.get("created_at"):
            try:
                latest_at = datetime.fromisoformat(str(latest["created_at"]).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                latest_at = None
        now = datetime.now(timezone.utc)
        if not _due_now(settings, latest_at, now):
            return

        db_path = _resolve_db_path()
        if not db_path:
            logger.warning("Backup scheduler: could not resolve the database path — skipping tick")
            return

        from fpulse.storage.backup import backup_database, _prune_backups
        path = backup_database(db_path)
        if not path:
            logger.warning("Backup scheduler: scheduled backup produced no file (db missing?)")
            return
        logger.info("Backup scheduler: created scheduled backup %s", path)

        # Honor the user's configured retention_count (backup_database already
        # pruned to the global default; this enforces the per-install setting).
        try:
            keep = int(settings.get("retention_count", 5))
            base = os.path.splitext(os.path.basename(db_path))[0]
            _prune_backups(os.path.dirname(path), base, keep)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Backup scheduler: retention prune skipped: %s", exc)


__all__ = ["BackupScheduler", "BackupSchedulerDaemon"]
