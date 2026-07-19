"""Notification store — SQLite-backed persistence for in-app notifications."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fpulse.notifications.models import Notification

logger = logging.getLogger("fpulse.notifications")


class NotificationStore:
    """CRUD for in-app notifications, backed by SQLite."""

    def __init__(self, db):
        self._db = db
        self._ensure_table()

    def _ensure_table(self):
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                type TEXT DEFAULT 'info',
                is_read INTEGER DEFAULT 0,
                data JSON NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read)"
        )
        self._db.commit()

    def create(self, notification: Notification) -> Notification:
        """Persist a notification."""
        self._db.execute(
            "INSERT INTO notifications (id, user_id, type, is_read, data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                notification.id,
                notification.user_id,
                notification.type,
                0,
                json.dumps(notification.model_dump(mode="json")),
                notification.created_at.isoformat(),
            ),
        )
        self._db.commit()
        return notification

    def list_for_user(
        self,
        user_id: str,
        unread_only: bool = False,
        limit: int = 50,
    ) -> list[dict]:
        """List notifications for a user, newest first."""
        if unread_only:
            rows = self._db.fetchall(
                "SELECT data FROM notifications WHERE user_id = ? AND is_read = 0 "
                "ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            )
        else:
            rows = self._db.fetchall(
                "SELECT data FROM notifications WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            )
        return [json.loads(r["data"]) for r in rows]

    def unread_count(self, user_id: str) -> int:
        """Count unread notifications for a user."""
        row = self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM notifications WHERE user_id = ? AND is_read = 0",
            (user_id,),
        )
        return row["cnt"] if row else 0

    def mark_read(self, notification_id: str, user_id: str) -> bool:
        """Mark a single notification as read."""
        self._db.execute(
            "UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?",
            (notification_id, user_id),
        )
        # Also update the JSON blob
        row = self._db.fetchone(
            "SELECT data FROM notifications WHERE id = ?", (notification_id,)
        )
        if row:
            data = json.loads(row["data"])
            data["is_read"] = True
            self._db.execute(
                "UPDATE notifications SET data = ? WHERE id = ?",
                (json.dumps(data), notification_id),
            )
        self._db.commit()
        return True

    def mark_all_read(self, user_id: str) -> int:
        """Mark all notifications as read for a user. Returns count."""
        cursor = self._db.execute(
            "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0",
            (user_id,),
        )
        # Bulk update JSON blobs
        rows = self._db.fetchall(
            "SELECT id, data FROM notifications WHERE user_id = ? AND is_read = 1",
            (user_id,),
        )
        for r in rows:
            data = json.loads(r["data"])
            if not data.get("is_read"):
                data["is_read"] = True
                self._db.execute(
                    "UPDATE notifications SET data = ? WHERE id = ?",
                    (json.dumps(data), r["id"]),
                )
        self._db.commit()
        return cursor.rowcount if hasattr(cursor, "rowcount") else 0

    def delete_old(self, days: int = 30) -> int:
        """Delete notifications older than N days."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = self._db.execute(
            "DELETE FROM notifications WHERE created_at < ?", (cutoff,)
        )
        self._db.commit()
        return cursor.rowcount if hasattr(cursor, "rowcount") else 0

    def delete(self, notification_id: str, user_id: str) -> bool:
        """Delete a single notification owned by ``user_id``.

        Returns True when a row was actually removed. The user_id check
        prevents one user from deleting another user's notification by
        guessing an id.
        """
        cursor = self._db.execute(
            "DELETE FROM notifications WHERE id = ? AND user_id = ?",
            (notification_id, user_id),
        )
        self._db.commit()
        return bool(getattr(cursor, "rowcount", 0))

    def delete_all_for_user(self, user_id: str, only_read: bool = False) -> int:
        """Delete every notification for ``user_id``.

        ``only_read=True`` keeps unread notifications and clears the
        rest — useful for a "clear history but keep what I haven't
        looked at yet" UX.
        """
        if only_read:
            cursor = self._db.execute(
                "DELETE FROM notifications WHERE user_id = ? AND is_read = 1",
                (user_id,),
            )
        else:
            cursor = self._db.execute(
                "DELETE FROM notifications WHERE user_id = ?",
                (user_id,),
            )
        self._db.commit()
        return getattr(cursor, "rowcount", 0) or 0
