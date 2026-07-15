"""SQLite-backed schedule store."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .models import Schedule


class ScheduleStore:
    """Schedule store backed by SQLite."""

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db):
        self._db = db

    def _save(self, schedule: Schedule):
        data = schedule.model_dump(mode="json")
        self._db.insert_json(
            "schedules", schedule.id, data,
            workflow_id=schedule.workflow_id,
            project_id=schedule.project_id,
            workspace_id=schedule.workspace_id or "default",
            enabled=1 if schedule.enabled else 0,
            created_at=schedule.created_at.isoformat(),
            updated_at=schedule.updated_at.isoformat(),
        )

    def create(self, schedule: Schedule) -> Schedule:
        self._save(schedule)
        return schedule

    def get(self, schedule_id: str, workspace_id: str | None = None) -> Schedule | None:
        """Get a schedule — returns None across workspace boundary."""
        data = self._db.get_json("schedules", schedule_id)
        if data is None:
            return None
        if workspace_id is not None:
            sched_ws = data.get("workspace_id") or "default"
            if sched_ws != workspace_id:
                return None
        return Schedule(**data)

    def list_all(self, workspace_id: str | None = None) -> list[dict]:
        if workspace_id is not None:
            return self._db.list_json(
                "schedules", "workspace_id = ?", (workspace_id,)
            )
        return self._db.list_json("schedules")

    def list_by_workflow(
        self,
        workflow_id: str,
        workspace_id: str | None = None,
    ) -> list[dict]:
        if workspace_id is not None:
            return self._db.list_json(
                "schedules",
                "workflow_id = ? AND workspace_id = ?",
                (workflow_id, workspace_id),
            )
        return self._db.list_json("schedules", "workflow_id = ?", (workflow_id,))

    def list_by_project(
        self,
        project_id: str,
        workspace_id: str | None = None,
    ) -> list[dict]:
        if workspace_id is not None:
            return self._db.list_json(
                "schedules",
                "project_id = ? AND workspace_id = ?",
                (project_id, workspace_id),
            )
        return self._db.list_json("schedules", "project_id = ?", (project_id,))

    def update(
        self,
        schedule_id: str,
        updates: dict,
        workspace_id: str | None = None,
    ) -> Schedule | None:
        schedule = self.get(schedule_id, workspace_id=workspace_id)
        if not schedule:
            return None
        for key, value in updates.items():
            # Schedule workspace membership is immutable via update body
            # — same rule as connections/credentials.
            if key == "workspace_id":
                continue
            if value is not None and hasattr(schedule, key):
                setattr(schedule, key, value)
        schedule.updated_at = datetime.now(timezone.utc)
        self._save(schedule)
        return schedule

    def delete(self, schedule_id: str, workspace_id: str | None = None) -> bool:
        if workspace_id is not None:
            if not self.get(schedule_id, workspace_id=workspace_id):
                return False
        return self._db.delete_row("schedules", schedule_id)

    def record_run(self, schedule_id: str, status: str) -> None:
        """System-level — called by the scheduler loop, unscoped by design."""
        schedule = self.get(schedule_id)
        if schedule:
            schedule.last_run_at = datetime.now(timezone.utc)
            schedule.last_run_status = status
            schedule.run_count += 1
            self._save(schedule)
