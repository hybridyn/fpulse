"""SQLite-backed project store."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .models import Project


class ProjectStore:
    """Project store backed by SQLite."""

    def __init__(self, db=None):
        self._db = db
        if db:
            self._ensure_default_project()

    def set_db(self, db):
        """Set database after construction (for late binding)."""
        self._db = db
        self._ensure_default_project()

    def _ensure_default_project(self):
        """Create the Default project on a fresh install only.

        Once any project exists in the database we treat the workspace
        as "user-managed" and stop force-creating Default. Otherwise a
        user who deleted Default (because they renamed everything into
        their own projects) would see it reappear on every restart.
        """
        existing_default = self._db.fetchone("SELECT id FROM projects WHERE id = ?", ("default",))
        if existing_default:
            return
        # No "default" row — only create it if the projects table is
        # truly empty (fresh install). On an established install the
        # absence of "default" is a deliberate user action; respect it.
        any_row = self._db.fetchone("SELECT id FROM projects LIMIT 1")
        if any_row:
            return
        default = Project(
            id="default",
            name="Default",
            description="Default project for ungrouped pipelines",
            owner="system",
            color="#94a3b8",
            icon="inbox",
            workspace_id="default",
        )
        self._save(default)

    def _save(self, project: Project):
        # workspace_id lives in BOTH the JSON blob (so model round-trips work)
        # and the indexed `workspace_id` column (so list_for_workspace can use
        # an index instead of scanning every row's JSON). The two must stay
        # in sync — only `_save` writes to the table, so this is the single
        # place that enforces it.
        data = project.model_dump(mode="json")
        self._db.insert_json(
            "projects", project.id, data,
            name=project.name,
            project_id="",
            workspace_id=project.workspace_id or "default",
            created_at=project.created_at.isoformat(),
            updated_at=project.updated_at.isoformat(),
        )

    def _load(self, data: dict) -> Project:
        # Tolerate legacy rows from before schema v2 that don't carry a
        # `workspace_id` field in the JSON blob — they live in `default`
        # by virtue of the migration's column back-fill.
        if "workspace_id" not in data or not data.get("workspace_id"):
            data = {**data, "workspace_id": "default"}
        return Project(**data)

    def create(self, project: Project) -> Project:
        # Store-layer uniqueness (May 6 2026) — auto-suffix the name if
        # another project in the same workspace already uses it.
        try:
            from fpulse.common.unique_name import ensure_unique_name
            ws_id = getattr(project, "workspace_id", None) or "default"
            existing_names: set[str] = set()
            for p in self.list_all(workspace_id=ws_id):
                n = p.get("name") if isinstance(p, dict) else getattr(p, "name", None)
                if n:
                    existing_names.add(n)
            if project.name:
                project.name = ensure_unique_name(project.name, existing_names)
        except Exception:  # noqa: BLE001
            pass
        self._save(project)
        return project

    def get(self, project_id: str) -> Project | None:
        data = self._db.get_json("projects", project_id)
        if data is None:
            return None
        return self._load(data)

    def list_all(self, workspace_id: str | None = None) -> list[dict]:
        """List projects, optionally scoped to a single workspace.

        Passing `workspace_id=None` returns every project on the install
        (admin tooling, audit, migration scripts). Normal API callers
        always pass the current workspace id from the request — see
        `current_workspace_id` dependency.
        """
        if workspace_id is None:
            items = self._db.list_json("projects", order_by="name ASC")
        else:
            items = self._db.list_json(
                "projects",
                where="workspace_id = ?",
                params=(workspace_id,),
                order_by="name ASC",
            )
        # Same legacy back-fill as `_load` — old JSON blobs may not have
        # workspace_id even though the column does. Ensures the wire format
        # the frontend sees is always consistent.
        for it in items:
            if "workspace_id" not in it or not it.get("workspace_id"):
                it["workspace_id"] = "default"
        return items

    def update(self, project_id: str, updates: dict) -> Project | None:
        project = self.get(project_id)
        if not project:
            return None
        for key, value in updates.items():
            if value is not None and hasattr(project, key):
                setattr(project, key, value)
        project.updated_at = datetime.now(timezone.utc)
        self._save(project)
        return project

    def delete(self, project_id: str) -> bool:
        """Delete a project. The Default project IS deletable — but the
        API layer must drain its pipelines/connections/credentials first
        (otherwise those rows fall back to a non-existent project_id).

        The store no longer auto-recreates Default at boot once the user
        has any other project, so deletion sticks across restarts.
        """
        return self._db.delete_row("projects", project_id)

    def count(self) -> int:
        return self._db.count("projects")
