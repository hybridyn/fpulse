"""Per-step output capture store.

Persists a sample of each step's output (rows + schema + counts) for
each execution, enabling the historical execution-replay viewer where
the user opens a past run, sees the pipeline graph with per-node
status, and clicks any node to inspect Schema / Table / JSON tabs of
the data that flowed through.

Capture is OUTPUTS ONLY. The UI derives a step's "input" view by
walking the DAG to the upstream step's output. For multi-parent steps
(joins, unions), the UI fetches each upstream output and presents them
as separate inputs.

OSS caps:
- Up to 100 rows per step sample (MAX_SAMPLE_ROWS)
- Up to 1 MB serialized JSON per step sample (MAX_SAMPLE_BYTES)
- Sample rows pruned after 30 days (SAMPLE_TTL_DAYS); row_count and
  schema retained indefinitely so historical lineage stays inspectable

Plus extends these via per-workspace settings (not in OSS).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fpulse.storage.database import Database


MAX_SAMPLE_ROWS = 100
MAX_SAMPLE_BYTES = 1024 * 1024
SAMPLE_TTL_DAYS = 30


def schema_from_sample(
    sample_data: list[dict[str, Any]],
    schema_info: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Augment schema_info with null/distinct counts derived from the sample.

    Stats are sample-only; callers should surface from_sample=True so the
    UI says "computed from N-row sample" rather than implying full-table
    truth. Used by both executors so the captured shape is identical.
    """
    sample_size = len(sample_data)
    out: list[dict[str, Any]] = []
    for info in schema_info:
        col = info.get("name", "")
        values = [r.get(col) for r in sample_data]
        null_count = sum(1 for v in values if v is None)
        try:
            distinct_count: int | None = len({v for v in values if v is not None})
        except TypeError:
            # Nested dict/list columns (XML/JSON parse output) are unhashable.
            distinct_count = None
        out.append({
            "name": col,
            "dtype": info.get("type", ""),
            "nullable": info.get("nullable", True),
            "null_count": null_count,
            "distinct_count": distinct_count,
            "from_sample": True,
            "sample_size": sample_size,
        })
    return out


class StepOutputStore:
    """Storage for per-step output snapshots, one row per (execution, step)."""

    TABLE_NAME = "step_outputs"

    def __init__(self, db: Database):
        self.db = db
        self._ensure_table()

    def _ensure_table(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS step_outputs (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                step_index INTEGER DEFAULT 0,
                step_type TEXT DEFAULT '',
                label TEXT DEFAULT '',
                status TEXT DEFAULT 'success',
                row_count INTEGER DEFAULT 0,
                sample_rows TEXT DEFAULT '[]',
                sample_bytes INTEGER DEFAULT 0,
                sample_truncated INTEGER DEFAULT 0,
                sample_pruned INTEGER DEFAULT 0,
                schema_json TEXT DEFAULT '[]',
                captured_at TEXT NOT NULL
            )
        """)

        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_step_outputs_execution
            ON step_outputs(execution_id)
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_step_outputs_step
            ON step_outputs(execution_id, step_id)
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_step_outputs_captured
            ON step_outputs(captured_at)
        """)
        self.db.commit()

    def record(
        self,
        execution_id: str,
        step_id: str,
        *,
        step_index: int = 0,
        step_type: str = "",
        label: str = "",
        status: str = "success",
        row_count: int = 0,
        sample_rows: list[dict[str, Any]] | None = None,
        schema: list[dict[str, Any]] | None = None,
    ) -> str:
        rows, sample_json, sample_bytes, truncated = self._apply_caps(sample_rows or [])
        schema_json = json.dumps(schema or [], default=str)

        record_id = uuid.uuid4().hex
        self.db.execute(
            """
            INSERT INTO step_outputs (
                id, execution_id, step_id, step_index, step_type, label,
                status, row_count, sample_rows, sample_bytes,
                sample_truncated, sample_pruned, schema_json, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                record_id,
                execution_id,
                step_id,
                step_index,
                step_type,
                label,
                status,
                row_count,
                sample_json,
                sample_bytes,
                1 if truncated else 0,
                schema_json,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.db.commit()
        return record_id

    def get_step(self, execution_id: str, step_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone(
            """
            SELECT * FROM step_outputs
            WHERE execution_id = ? AND step_id = ?
            ORDER BY captured_at DESC LIMIT 1
            """,
            (execution_id, step_id),
        )
        return self._row_to_dict(row) if row else None

    def list_for_execution(self, execution_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT * FROM step_outputs
            WHERE execution_id = ?
            ORDER BY step_index ASC, captured_at ASC
            """,
            (execution_id,),
        )
        return [self._row_to_dict(r) for r in rows]

    def delete_for_execution(self, execution_id: str) -> int:
        cursor = self.db.execute(
            "DELETE FROM step_outputs WHERE execution_id = ?",
            (execution_id,),
        )
        self.db.commit()
        return cursor.rowcount if cursor else 0

    def prune_samples(self, ttl_days: int = SAMPLE_TTL_DAYS) -> int:
        """Null out sample_rows on records older than ttl_days; keep counts + schema."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
        cursor = self.db.execute(
            """
            UPDATE step_outputs
            SET sample_rows = '[]',
                sample_bytes = 2,
                sample_pruned = 1
            WHERE captured_at < ? AND sample_pruned = 0
            """,
            (cutoff,),
        )
        self.db.commit()
        return cursor.rowcount if cursor else 0

    @staticmethod
    def _apply_caps(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, int, bool]:
        truncated = False

        if len(rows) > MAX_SAMPLE_ROWS:
            rows = rows[:MAX_SAMPLE_ROWS]
            truncated = True

        sample_json = json.dumps(rows, default=str)
        sample_bytes = len(sample_json.encode("utf-8"))

        while sample_bytes > MAX_SAMPLE_BYTES and len(rows) > 1:
            rows = rows[: max(1, len(rows) // 2)]
            sample_json = json.dumps(rows, default=str)
            sample_bytes = len(sample_json.encode("utf-8"))
            truncated = True

        if sample_bytes > MAX_SAMPLE_BYTES:
            sample_json = "[]"
            sample_bytes = len(sample_json.encode("utf-8"))
            truncated = True
            rows = []

        return rows, sample_json, sample_bytes, truncated

    @staticmethod
    def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        try:
            out["sample_rows"] = json.loads(out.get("sample_rows") or "[]")
        except (json.JSONDecodeError, TypeError):
            out["sample_rows"] = []
        try:
            out["schema"] = json.loads(out.get("schema_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            out["schema"] = []
        out.pop("schema_json", None)
        out["sample_truncated"] = bool(out.get("sample_truncated"))
        out["sample_pruned"] = bool(out.get("sample_pruned"))
        return out
