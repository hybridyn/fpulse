"""SQLite-backed folder store."""

from __future__ import annotations

from datetime import datetime, timezone

from .models import Folder


class FolderStore:
    """Folder store backed by SQLite."""

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db):
        self._db = db

    def _save(self, folder: Folder):
        data = folder.model_dump(mode="json")
        self._db.insert_json(
            "folders", folder.id, data,
            name=folder.name,
            project_id=folder.project_id,
            parent_folder_id=folder.parent_folder_id or "",
            workspace_id=folder.workspace_id or "default",
            created_at=folder.created_at.isoformat(),
            updated_at=folder.updated_at.isoformat(),
        )

    def _load(self, data: dict) -> Folder:
        if "workspace_id" not in data or not data.get("workspace_id"):
            data = {**data, "workspace_id": "default"}
        return Folder(**data)

    def create(self, folder: Folder) -> Folder:
        self._save(folder)
        return folder

    def get(self, folder_id: str) -> Folder | None:
        data = self._db.get_json("folders", folder_id)
        if data is None:
            return None
        return self._load(data)

    def list_for_project(
        self,
        project_id: str,
        workspace_id: str | None = None,
    ) -> list[Folder]:
        if workspace_id is None:
            items = self._db.list_json(
                "folders",
                where="project_id = ?",
                params=(project_id,),
                order_by="name ASC",
            )
        else:
            items = self._db.list_json(
                "folders",
                where="project_id = ? AND workspace_id = ?",
                params=(project_id, workspace_id),
                order_by="name ASC",
            )
        return [self._load(it) for it in items]

    def list_all(self, workspace_id: str | None = None) -> list[Folder]:
        if workspace_id is None:
            items = self._db.list_json("folders", order_by="name ASC")
        else:
            items = self._db.list_json(
                "folders",
                where="workspace_id = ?",
                params=(workspace_id,),
                order_by="name ASC",
            )
        return [self._load(it) for it in items]

    def update(self, folder_id: str, updates: dict) -> Folder | None:
        folder = self.get(folder_id)
        if not folder:
            return None
        for key, value in updates.items():
            if value is not None and hasattr(folder, key):
                setattr(folder, key, value)
        folder.updated_at = datetime.now(timezone.utc)
        self._save(folder)
        return folder

    def delete(self, folder_id: str) -> bool:
        return self._db.delete_row("folders", folder_id)

    def descendants(self, folder_id: str) -> list[Folder]:
        """Return every folder transitively nested under `folder_id`,
        excluding the folder itself. Used by cascade-delete."""
        target = self.get(folder_id)
        if not target:
            return []
        all_in_project = self.list_for_project(target.project_id, target.workspace_id)
        by_parent: dict[str | None, list[Folder]] = {}
        for f in all_in_project:
            by_parent.setdefault(f.parent_folder_id, []).append(f)

        out: list[Folder] = []
        stack: list[str] = [folder_id]
        while stack:
            current = stack.pop()
            for child in by_parent.get(current, []):
                out.append(child)
                stack.append(child.id)
        return out

    def count(self) -> int:
        return self._db.count("folders")
