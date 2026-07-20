"""SQLite-backed variable store."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .models import Variable


class VariableStore:
    """Variable store with global/project scoping, backed by SQLite."""

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db):
        self._db = db

    def _save(self, variable: Variable):
        data = variable.model_dump(mode="json")
        self._db.insert_json(
            "variables", variable.id, data,
            key=variable.key,
            scope=variable.scope,
            project_id=variable.project_id,
            workspace_id=variable.workspace_id or "default",
            created_at=variable.created_at.isoformat(),
            updated_at=variable.updated_at.isoformat(),
        )

    def create(self, variable: Variable) -> Variable:
        self._save(variable)
        return variable

    def get(self, variable_id: str, workspace_id: str | None = None) -> Variable | None:
        data = self._db.get_json("variables", variable_id)
        if data is None:
            return None
        if workspace_id is not None:
            if (data.get("workspace_id") or "default") != workspace_id:
                return None
        return Variable(**data)

    def list_all(
        self,
        scope: str | None = None,
        project_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[dict]:
        items = self._db.list_json("variables")
        result = []
        for v in items:
            if workspace_id is not None:
                if (v.get("workspace_id") or "default") != workspace_id:
                    continue
            if scope and v.get("scope") != scope:
                continue
            if project_id and v.get("scope") == "project" and v.get("project_id") != project_id:
                continue
            # Mask secrets
            if v.get("type") == "secret":
                val = v.get("value", "")
                if len(val) > 4:
                    v["value"] = val[:2] + "***" + val[-2:]
                else:
                    v["value"] = "***"
            result.append(v)
        return sorted(result, key=lambda x: x.get("key", ""))

    def update(
        self,
        variable_id: str,
        updates: dict,
        workspace_id: str | None = None,
    ) -> Variable | None:
        variable = self.get(variable_id, workspace_id=workspace_id)
        if not variable:
            return None
        for key, value in updates.items():
            if key == "workspace_id":
                continue
            if value is not None and hasattr(variable, key):
                setattr(variable, key, value)
        variable.updated_at = datetime.now(timezone.utc)
        self._save(variable)
        return variable

    def delete(self, variable_id: str, workspace_id: str | None = None) -> bool:
        if workspace_id is not None:
            if not self.get(variable_id, workspace_id=workspace_id):
                return False
        return self._db.delete_row("variables", variable_id)

    def resolve(
        self,
        key: str,
        project_id: str | None = None,
        workspace_id: str | None = None,
    ) -> str | None:
        """Resolve a variable value — project scope first, then global fallback.

        When ``workspace_id`` is provided, both the project-scoped and
        the global fallback lookups are restricted to that workspace.
        A variable named DB_URL in workspace A CANNOT be resolved by a
        pipeline running in workspace B, even if B has no DB_URL of
        its own — the resolver returns None and the caller treats
        that as a missing variable.
        """
        if project_id:
            if workspace_id is not None:
                row = self._db.fetchone(
                    "SELECT data FROM variables WHERE key = ? AND scope = 'project' AND project_id = ? AND workspace_id = ?",
                    (key, project_id, workspace_id),
                )
            else:
                row = self._db.fetchone(
                    "SELECT data FROM variables WHERE key = ? AND scope = 'project' AND project_id = ?",
                    (key, project_id),
                )
            if row:
                data = json.loads(row["data"])
                return data.get("value")
        if workspace_id is not None:
            row = self._db.fetchone(
                "SELECT data FROM variables WHERE key = ? AND scope = 'global' AND workspace_id = ?",
                (key, workspace_id),
            )
        else:
            row = self._db.fetchone(
                "SELECT data FROM variables WHERE key = ? AND scope = 'global'",
                (key,),
            )
        if row:
            data = json.loads(row["data"])
            return data.get("value")
        return None

    def count(self) -> int:
        return self._db.count("variables")
