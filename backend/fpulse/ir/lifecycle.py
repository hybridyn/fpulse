"""SQLite-backed lifecycle event store for pipeline status tracking."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Telemetry counters retained so the metrics endpoint can read them
# uniformly. In the OSS build they only ever record sqlite_ok /
# sqlite_failed; the pg_* keys stay at zero.
_DUAL_WRITE_STATS: dict[str, int] = {
    "sqlite_ok": 0,
    "sqlite_failed": 0,
    "pg_ok": 0,
    "pg_failed": 0,
    "pg_skipped_no_loop": 0,
}

_SHADOW_READ_STATS: dict[str, int] = {
    "match": 0,
    "mismatch": 0,
    "pg_failed": 0,
    "pg_skipped_no_loop": 0,
    "pg_skipped_disabled": 0,
}


def get_dual_write_stats() -> dict[str, int]:
    return dict(_DUAL_WRITE_STATS)


def get_shadow_read_stats() -> dict[str, int]:
    return dict(_SHADOW_READ_STATS)


class LifecycleEvent(BaseModel):
    """A single lifecycle transition event.

    Workspace binding is inherited from the parent workflow at write
    time — once written, a lifecycle event is an immutable audit
    record and cannot be moved between workspaces.
    """
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    workflow_id: str
    workspace_id: str = "default"
    event: str  # "testing", "published", "failed", "archived", "restored"
    message: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class LifecycleStore:
    """Lifecycle event store backed by SQLite."""

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db):
        self._db = db

    def add_event(
        self,
        workflow_id: str,
        event: str,
        message: str = "",
        metadata: dict[str, Any] | None = None,
        workspace_id: str = "default",
    ) -> LifecycleEvent:
        evt = LifecycleEvent(
            workflow_id=workflow_id,
            workspace_id=workspace_id or "default",
            event=event,
            message=message,
            metadata=metadata or {},
        )
        data = evt.model_dump(mode="json")

        try:
            self._db.insert_json(
                "lifecycle_events", evt.id, data,
                workflow_id=workflow_id,
                workspace_id=workspace_id or "default",
                event=event,
                timestamp=evt.timestamp.isoformat(),
            )
            _DUAL_WRITE_STATS["sqlite_ok"] += 1
        except Exception as exc:
            _DUAL_WRITE_STATS["sqlite_failed"] += 1
            logger.error(
                "LifecycleStore: SQLite write failed for event=%s workflow=%s: %s",
                event, workflow_id, exc,
            )
            raise

        return evt

    def get_events(
        self, workflow_id: str, workspace_id: str | None = None
    ) -> list[dict]:
        if workspace_id is not None:
            return self._db.list_json(
                "lifecycle_events",
                "workflow_id = ? AND workspace_id = ?",
                (workflow_id, workspace_id),
                order_by="timestamp DESC",
            )
        return self._db.list_json(
            "lifecycle_events", "workflow_id = ?", (workflow_id,),
            order_by="timestamp DESC",
        )

    def get_latest_event(
        self, workflow_id: str, workspace_id: str | None = None
    ) -> LifecycleEvent | None:
        if workspace_id is not None:
            rows = self._db.fetchall(
                "SELECT data FROM lifecycle_events WHERE workflow_id = ? AND workspace_id = ? ORDER BY timestamp DESC LIMIT 1",
                (workflow_id, workspace_id),
            )
        else:
            rows = self._db.fetchall(
                "SELECT data FROM lifecycle_events WHERE workflow_id = ? ORDER BY timestamp DESC LIMIT 1",
                (workflow_id,),
            )
        if not rows:
            return None
        return LifecycleEvent(**json.loads(rows[0]["data"]))

    def delete_events(
        self, workflow_id: str, workspace_id: str | None = None
    ) -> bool:
        if workspace_id is not None:
            cursor = self._db.execute(
                "DELETE FROM lifecycle_events WHERE workflow_id = ? AND workspace_id = ?",
                (workflow_id, workspace_id),
            )
        else:
            cursor = self._db.execute(
                "DELETE FROM lifecycle_events WHERE workflow_id = ?",
                (workflow_id,),
            )
        self._db.commit()
        return cursor.rowcount > 0
