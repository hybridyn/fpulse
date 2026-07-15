"""Auto-backup for the SQLite database on startup.

Creates a timestamped copy of the DB file in a ``backups/`` directory next
to it, then prunes old snapshots beyond the retention count.

The backup uses SQLite's own VACUUM INTO (3.27+) which creates a
consistent, WAL-merged copy even while the main connection is open. On
older SQLite we fall back to shutil.copy2 of the main file only (the WAL
may not be merged, but it's better than nothing).

This module is called early in the FastAPI startup event — before the
scheduler starts and before any writes can happen.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone

from fpulse import runtime_config

logger = logging.getLogger(__name__)


def backup_database(db_path: str) -> str | None:
    """Create a backup of ``db_path`` and prune old backups.

    Returns the backup file path on success, ``None`` on failure.
    Never raises — a failed backup must not block startup.
    """
    if not os.path.isfile(db_path):
        logger.debug("backup: db file %s does not exist yet — skip", db_path)
        return None

    backup_dir = os.path.join(os.path.dirname(db_path), "backups")
    try:
        os.makedirs(backup_dir, exist_ok=True)
    except OSError as exc:
        logger.warning("backup: cannot create %s (%s) — skipping backup", backup_dir, exc)
        return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = os.path.splitext(os.path.basename(db_path))[0]
    target = os.path.join(backup_dir, f"{base}_{ts}.db")

    try:
        # Prefer VACUUM INTO — it creates a clean, WAL-merged copy
        conn = sqlite3.connect(db_path)
        conn.execute(f"VACUUM INTO '{target}'")
        conn.close()
        logger.info("backup: created %s (VACUUM INTO)", target)
    except Exception as exc:
        # VACUUM INTO not supported or failed — fall back to file copy
        logger.debug("backup: VACUUM INTO failed (%s), falling back to shutil.copy2", exc)
        try:
            shutil.copy2(db_path, target)
            # Also copy WAL if present
            wal = db_path + "-wal"
            if os.path.isfile(wal):
                shutil.copy2(wal, target + "-wal")
            logger.info("backup: created %s (file copy)", target)
        except OSError as copy_exc:
            logger.warning("backup: failed to create backup (%s) — skipping", copy_exc)
            return None

    _prune_backups(backup_dir, base, runtime_config.BACKUP_RETENTION_COUNT)
    return target


def _prune_backups(backup_dir: str, prefix: str, keep: int) -> int:
    """Delete oldest backup files beyond ``keep`` count. Returns count deleted."""
    if keep <= 0:
        return 0

    try:
        files = sorted(
            (
                f for f in os.listdir(backup_dir)
                if f.startswith(prefix) and f.endswith(".db")
            ),
            key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)),
            reverse=True,
        )
    except OSError:
        return 0

    to_delete = files[keep:]
    deleted = 0
    for fname in to_delete:
        try:
            path = os.path.join(backup_dir, fname)
            os.remove(path)
            # Also remove companion WAL if present
            wal = path + "-wal"
            if os.path.isfile(wal):
                os.remove(wal)
            deleted += 1
        except OSError:
            pass

    if deleted:
        logger.info("backup: pruned %d old backup(s), keeping %d", deleted, keep)
    return deleted
