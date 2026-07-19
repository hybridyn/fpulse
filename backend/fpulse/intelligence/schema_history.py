"""Schema-history store — append-only log of evolved managed-table shapes.

The store wraps the ``schema_history`` SQLite table (schema v28). Each
row is one *version* of a managed table's schema, captured AFTER a
sink applied a change under its configured ``schema_policy``.

Why a separate table instead of expanding ``storage_columns``:

  * ``storage_columns`` is a CURRENT-state cache, full-rewrite on
    every sink-metadata refresh. It can't show evolution.
  * The auditor needs "what columns did this table have on 2026-03-12?"
    answerable without replaying every git-equivalent diff — so each
    row stores the FULL column list, not just the change diff. Storage
    cost is ~hundreds of bytes per evolution, negligible at OSS scale.
  * Tests + the future Plus "schema time-travel" feature both want a
    single read interface; one store is one place to mock.

Reads are strictly chronological (``ORDER BY version``). Writes use
``MAX(version) + 1`` for the table — the unique index on
``(table_id, version)`` is the defence-in-depth backstop if two sink
writes race; the loser raises ``IntegrityError`` and the executor
retries the version assignment. That's identical to how
``storage_tables`` handles duplicate-logical-identity in the
DataStore, just one layer down.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id() -> str:
    return f"sh_{uuid.uuid4().hex[:12]}"


class SchemaHistoryStore:
    """CRUD over ``schema_history``. Thin SQLite wrapper, no domain logic.

    Domain logic — what constitutes a recordable change, which policy
    rejected it — lives in ``schema_policy.evaluate_policy``. This
    store just persists the *result* the sink decided to apply.
    """

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db):
        self._db = db

    def record(
        self,
        *,
        workspace_id: str,
        table_id: str,
        columns: list[dict[str, Any]],
        change_summary: dict[str, Any],
        applied_by_run_id: str | None = None,
        policy: str = "add_columns",
    ) -> dict[str, Any]:
        """Append a new version row for the given managed table.

        Returns the inserted row as a dict so callers can echo the
        version number back to the run log or the API response.

        version assignment: SELECT MAX + 1, then INSERT. The unique
        index on (table_id, version) gives us the race-condition
        backstop — under contention the loser raises IntegrityError
        and we retry with a bumped version. In practice, sink writes
        for the SAME table never race in OSS (one workspace, one
        worker pool, one writer per table) so the retry path is
        cold; the index is there for Plus + future multi-writer.
        """
        if not table_id:
            raise ValueError("schema_history.record: table_id is required")

        for attempt in range(3):
            next_version = self._next_version(table_id)
            row_id = _gen_id()
            recorded_at = _utc_now_iso()
            try:
                self._db.conn.execute(
                    "INSERT INTO schema_history "
                    "(id, workspace_id, table_id, version, recorded_at, "
                    " columns_json, change_summary, applied_by_run_id, policy) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row_id,
                        workspace_id or "default",
                        table_id,
                        next_version,
                        recorded_at,
                        json.dumps(columns, default=str),
                        json.dumps(change_summary, default=str),
                        applied_by_run_id or "",
                        policy or "add_columns",
                    ),
                )
                self._db.conn.commit()
                return {
                    "id": row_id,
                    "workspace_id": workspace_id or "default",
                    "table_id": table_id,
                    "version": next_version,
                    "recorded_at": recorded_at,
                    "columns_json": columns,
                    "change_summary": change_summary,
                    "applied_by_run_id": applied_by_run_id or "",
                    "policy": policy or "add_columns",
                }
            except Exception as exc:
                # Likely a race on the unique (table_id, version) index —
                # bump and retry. After three losses we surface the error
                # so the sink can decide whether to retry the whole write
                # (which is what the executor does for any other DB
                # IntegrityError on commit).
                logger.warning(
                    "schema_history.record retry %d for table %s: %s",
                    attempt + 1, table_id, exc,
                )
                if attempt == 2:
                    raise
        raise RuntimeError("schema_history.record: unreachable")

    def _next_version(self, table_id: str) -> int:
        row = self._db.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_history WHERE table_id = ?",
            (table_id,),
        ).fetchone()
        if row is None:
            return 1
        # sqlite3.Row supports both index and key access; tolerate both
        # shapes for the conftest fixtures that bypass row_factory.
        try:
            current = int(row["v"])
        except (KeyError, TypeError, IndexError):
            current = int(row[0]) if row else 0
        return current + 1

    def list_for_table(
        self,
        table_id: str,
        *,
        workspace_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return the table's history in chronological (version ASC) order.

        ``workspace_id`` is enforced when supplied — a caller from a
        different tenant gets an empty list, never another tenant's
        history. ``limit`` caps the result for the API; the chart in
        the UI is fine with 100 rows max.
        """
        if not table_id:
            return []

        where = "table_id = ?"
        params: list[Any] = [table_id]
        if workspace_id is not None:
            where += " AND workspace_id = ?"
            params.append(workspace_id)

        sql = (
            f"SELECT id, workspace_id, table_id, version, recorded_at, "
            f"columns_json, change_summary, applied_by_run_id, policy "
            f"FROM schema_history WHERE {where} ORDER BY version ASC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"

        rows = self._db.conn.execute(sql, tuple(params)).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(_row_to_dict(r))
        return out

    def latest(
        self, table_id: str, *, workspace_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the most recent version row or None if no history exists."""
        rows = self.list_for_table(table_id, workspace_id=workspace_id)
        return rows[-1] if rows else None


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Normalise a sqlite3.Row (or tuple) into the API dict shape."""
    def _g(key, idx):
        try:
            return row[key]
        except (KeyError, TypeError, IndexError):
            try:
                return row[idx]
            except (IndexError, TypeError):
                return None

    def _loads(val: Any, default: Any) -> Any:
        if val is None:
            return default
        if isinstance(val, (dict, list)):
            return val
        try:
            return json.loads(val)
        except (TypeError, ValueError):
            return default

    return {
        "id": _g("id", 0),
        "workspace_id": _g("workspace_id", 1),
        "table_id": _g("table_id", 2),
        "version": int(_g("version", 3) or 0),
        "recorded_at": _g("recorded_at", 4),
        "columns_json": _loads(_g("columns_json", 5), []),
        "change_summary": _loads(_g("change_summary", 6), {}),
        "applied_by_run_id": _g("applied_by_run_id", 7) or "",
        "policy": _g("policy", 8) or "add_columns",
    }


__all__ = ["SchemaHistoryStore"]
