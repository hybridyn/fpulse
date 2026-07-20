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

The actual scheduled triggering daemon is intentionally NOT wired in
this file yet. OSS users get:

  * manual "Backup now" via existing POST /api/backup/create
  * startup-time snapshot via fpulse.storage.backup.backup_database()
  * configurable schedule that's stored and surfaced to the UI

A future change can register a background thread that wakes up every
60s, checks ``next_backup_at``, and calls the create endpoint when due.
That's an operational concern — the settings surface ships first.

Why a JSON file (vs. SQLite row)? Backup config is one row per install,
not one row per workspace, and it must be readable when the database is
being restored / corrupt / missing. A small flat file keeps the recovery
story honest.
"""

from __future__ import annotations

import json
import logging
import os
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
            "next_backup_at": _compute_next_run(settings),
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


__all__ = ["BackupScheduler"]
