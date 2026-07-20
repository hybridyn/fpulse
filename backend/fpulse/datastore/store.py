"""SQLite-backed metadata store for the workspace datastore.

Three tables, all created/migrated by ``storage/database.py`` schema v25:

  * ``storage_objects``  — one row per file (upload / output / trash)
  * ``storage_tables``   — one row per managed Parquet table
  * ``storage_columns``  — cached schema rows for tables + files

The store reads/writes JSON blobs through the existing ``Database``
helpers (``insert_json``, ``list_json``, etc.) so all the workspace-
filtering, backup, and connection-pool concerns the rest of F-Pulse
already implements apply for free.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from fpulse.datastore.models import (
    OBJECT_KIND_FILE,
    OBJECT_KIND_OUTPUT,
    StorageColumn,
    StorageObject,
    StorageTable,
)

logger = logging.getLogger(__name__)


def get_store() -> "DataStore":
    """Live DataStore from app_state. Mirrors fpulse.connections.store.get_store()."""
    from fpulse.main import app_state
    store = app_state.get("datastore")
    if store is None:
        raise RuntimeError(
            "app_state['datastore'] is not initialized. "
            "Check fpulse.main._populate_state — datastore is "
            "constructed alongside the connection store."
        )
    return store


class DataStore:
    """Metadata CRUD over storage_objects / storage_tables / storage_columns.

    The class is intentionally thin — it doesn't do filesystem I/O.
    That stays in the API layer and the IR nodes; this is just the
    metadata index. Keeping the two apart means tests can exercise
    the store with an in-memory SQLite and never touch a real disk.
    """

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db):
        self._db = db

    # ─── StorageObject CRUD ───────────────────────────────────────────────

    def save_object(self, obj: StorageObject) -> StorageObject:
        obj.updated_at = datetime.now(timezone.utc)
        data = obj.model_dump(mode="json")
        self._db.insert_json(
            "storage_objects", obj.id, data,
            workspace_id=obj.workspace_id,
            kind=obj.kind,
            project_id=obj.project_id or "",
            folder_id=obj.folder_id or "",  # Y15 (v26)
            pipeline_id=obj.pipeline_id or "",
            deleted_at=obj.deleted_at.isoformat() if obj.deleted_at else "",
        )
        return obj

    def get_object(self, object_id: str, workspace_id: str | None = None) -> StorageObject | None:
        data = self._db.get_json("storage_objects", object_id)
        if not data:
            return None
        if workspace_id and data.get("workspace_id") != workspace_id:
            return None
        return StorageObject(**data)

    def list_objects(
        self,
        workspace_id: str,
        *,
        kind: str | None = None,
        project_id: str | None = None,
        folder_id: str | None = None,
        include_deleted: bool = False,
    ) -> list[StorageObject]:
        """List objects in a workspace, optionally filtered by kind/project/folder.

        ``include_deleted=False`` (default) hides soft-deleted rows; the
        Files tab toggles this when the user clicks "Show deleted".
        ``folder_id`` filter is exact-match against the indexed column —
        pass an empty string to find files at the project root.
        """
        where = ["workspace_id = ?"]
        params: list[Any] = [workspace_id]
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if project_id is not None:
            where.append("project_id = ?")
            params.append(project_id or "")
        if folder_id is not None:
            where.append("folder_id = ?")
            params.append(folder_id or "")
        if not include_deleted:
            where.append("(deleted_at = '' OR deleted_at IS NULL)")
        rows = self._db.list_json(
            "storage_objects",
            where=" AND ".join(where),
            params=tuple(params),
            order_by="json_extract(data, '$.created_at') DESC",
        )
        return [StorageObject(**r) for r in rows]

    def soft_delete_object(self, object_id: str) -> bool:
        obj = self.get_object(object_id)
        if not obj:
            return False
        if obj.deleted_at:
            return True  # already soft-deleted; idempotent
        obj.deleted_at = datetime.now(timezone.utc)
        self.save_object(obj)
        return True

    def hard_delete_object(self, object_id: str) -> bool:
        # Cascade: drop cached schema columns first.
        self._db.conn.execute(
            "DELETE FROM storage_columns WHERE object_id = ?", (object_id,)
        )
        self._db.conn.commit()
        return self._db.delete_row("storage_objects", object_id)

    def list_outputs_grouped(self, workspace_id: str) -> list[dict[str, Any]]:
        """Pipeline outputs grouped by (pipeline_id, run_id) for the Outputs tab.

        Returns ``[{"pipeline_id", "run_id", "objects": [...]}]`` — one
        group per (pipeline, run). Empty list when there are no outputs.
        """
        rows = self.list_objects(workspace_id, kind=OBJECT_KIND_OUTPUT, include_deleted=False)
        groups: dict[tuple[str, str], list[StorageObject]] = {}
        for r in rows:
            key = (r.pipeline_id or "", r.run_id or "")
            groups.setdefault(key, []).append(r)
        out = []
        for (pid, rid), items in groups.items():
            out.append({
                "pipeline_id": pid,
                "run_id": rid,
                "size_bytes": sum(o.size_bytes for o in items),
                "object_count": len(items),
                "objects": [o.model_dump(mode="json") for o in items],
            })
        # Most-recent runs first (sort by max created_at in group).
        out.sort(
            key=lambda g: max(
                (o.get("created_at") or "") for o in g["objects"]
            ),
            reverse=True,
        )
        return out

    # ─── StorageTable CRUD ────────────────────────────────────────────────

    def save_table(self, table: StorageTable) -> StorageTable:
        """Persist a managed-table metadata row.

        2026-05-25 — switched from `insert_json` (which uses
        `INSERT OR REPLACE`) to an explicit `INSERT` so the unique
        constraint on `(workspace_id, schema_name, name)` actually
        raises ``sqlite3.IntegrityError`` on a duplicate logical
        identity. Previous behaviour silently replaced a different
        table id with the new row, which corrupted "Used by" tracking
        and any lineage that referenced the old id.

        Re-saving the SAME table id (e.g. metadata refresh after a sink
        write) still works via `INSERT OR REPLACE WHERE id = ?` — we
        only block dupes that come in with a NEW id but the same
        logical name.
        """
        import json
        table.updated_at = datetime.now(timezone.utc)
        data = table.model_dump(mode="json")
        # Detect duplicate-logical-identity (different id, same name)
        # before the SQL fires, so the error message is friendly. The
        # unique index is still the source of truth — this is just a
        # readable wrapper around the constraint.
        existing = self.find_table_by_name(
            table.workspace_id, table.schema_name, table.name,
        )
        if existing is not None and existing.id != table.id:
            import sqlite3
            raise sqlite3.IntegrityError(
                f"managed table '{table.schema_name}.{table.name}' already exists "
                f"in workspace '{table.workspace_id}' "
                f"(existing id={existing.id}; new id={table.id})"
            )
        # Re-save by id is fine: REPLACE on PK lets metadata refresh
        # (rows / columns / size_bytes after sink writes) update in
        # place without churn.
        self._db.insert_json(
            "storage_tables", table.id, data,
            workspace_id=table.workspace_id,
            schema_name=table.schema_name,
            name=table.name,
        )
        return table

    def get_table(self, table_id: str, workspace_id: str | None = None) -> StorageTable | None:
        data = self._db.get_json("storage_tables", table_id)
        if not data:
            return None
        if workspace_id and data.get("workspace_id") != workspace_id:
            return None
        return StorageTable(**data)

    def find_table_by_name(
        self, workspace_id: str, schema_name: str, table_name: str,
    ) -> StorageTable | None:
        rows = self._db.fetchall(
            "SELECT data FROM storage_tables "
            "WHERE workspace_id = ? AND schema_name = ? AND name = ?",
            (workspace_id, schema_name, table_name),
        )
        if not rows:
            return None
        import json
        return StorageTable(**json.loads(rows[0]["data"]))

    def list_tables(self, workspace_id: str) -> list[StorageTable]:
        rows = self._db.list_json(
            "storage_tables",
            where="workspace_id = ?",
            params=(workspace_id,),
            order_by="json_extract(data, '$.schema_name'), json_extract(data, '$.name')",
        )
        return [StorageTable(**r) for r in rows]

    def hard_delete_table(self, table_id: str) -> bool:
        # Cascade: drop the table's cached column rows first.
        self._db.conn.execute(
            "DELETE FROM storage_columns WHERE table_id = ?", (table_id,)
        )
        self._db.conn.commit()
        return self._db.delete_row("storage_tables", table_id)

    # ─── StorageColumn CRUD ───────────────────────────────────────────────

    def save_columns(
        self,
        columns: Iterable[StorageColumn],
        *,
        table_id: str | None = None,
        object_id: str | None = None,
    ) -> None:
        """Replace the cached column set for a table OR object.

        Pass ``table_id`` to refresh a managed-table's columns, or
        ``object_id`` to refresh a file's schema-infer cache. The
        existing rows for that owner are dropped first so this is
        always a full-rewrite, not an append.
        """
        if not (table_id or object_id) or (table_id and object_id):
            raise ValueError("save_columns: exactly one of table_id / object_id must be set")
        if table_id:
            self._db.conn.execute(
                "DELETE FROM storage_columns WHERE table_id = ?", (table_id,)
            )
        else:
            self._db.conn.execute(
                "DELETE FROM storage_columns WHERE object_id = ?", (object_id,)
            )
        self._db.conn.commit()
        for col in columns:
            # Force the FK so the caller can't accidentally desynchronise.
            col.table_id = table_id
            col.object_id = object_id
            data = col.model_dump(mode="json")
            self._db.insert_json(
                "storage_columns", col.id, data,
                workspace_id=col.workspace_id,
                table_id=table_id or "",
                object_id=object_id or "",
            )

    def list_columns(
        self, *, table_id: str | None = None, object_id: str | None = None,
    ) -> list[StorageColumn]:
        if table_id:
            rows = self._db.list_json(
                "storage_columns",
                where="table_id = ?",
                params=(table_id,),
                order_by="json_extract(data, '$.ordinal')",
            )
        elif object_id:
            rows = self._db.list_json(
                "storage_columns",
                where="object_id = ?",
                params=(object_id,),
                order_by="json_extract(data, '$.ordinal')",
            )
        else:
            return []
        return [StorageColumn(**r) for r in rows]

    # ─── Summary / counters ──────────────────────────────────────────────

    def workspace_summary(self, workspace_id: str) -> dict[str, int]:
        """Aggregate counts + byte totals for the GET /summary endpoint.

        Computed from metadata only — no filesystem walks. Files that
        exist on disk but aren't in storage_objects don't count; that's
        intentional, the reconciler should be back-filling them.
        """
        files = self.list_objects(workspace_id, kind=OBJECT_KIND_FILE, include_deleted=False)
        outputs = self.list_objects(workspace_id, kind=OBJECT_KIND_OUTPUT, include_deleted=False)
        all_rows = self.list_objects(workspace_id, include_deleted=True)
        trash = [r for r in all_rows if r.deleted_at]
        tables = self.list_tables(workspace_id)
        return {
            "workspace_id": workspace_id,
            "file_count": len(files),
            "file_size_bytes": sum(r.size_bytes for r in files),
            "output_count": len(outputs),
            "output_size_bytes": sum(r.size_bytes for r in outputs),
            "table_count": len(tables),
            "table_size_bytes": sum(t.size_bytes for t in tables),
            "trash_count": len(trash),
            "trash_size_bytes": sum(r.size_bytes for r in trash),
            "total_size_bytes": sum(r.size_bytes for r in files) + sum(r.size_bytes for r in outputs) + sum(t.size_bytes for t in tables) + sum(r.size_bytes for r in trash),
        }


__all__ = ["DataStore", "get_store"]
