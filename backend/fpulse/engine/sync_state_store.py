"""
SyncStateStore — per-(workflow, source-step) cursor watermark for
incremental ingestion.

Why this exists
---------------
Pre-2026-05-30, F-Pulse's `db_source` node supported "incremental load"
via two hand-typed params: `watermark_column` and `watermark_value`.
The user had to remember to bump `watermark_value` after each run, or
the next run would re-load every row. That's not a real incremental
contract — it's the manual-bookkeeping version.

This store closes the gap. With `sync_mode=incremental` set on a
source node:

  1. At the start of each run, the node loads `last_cursor` from
     this table (keyed by workflow_id + step_id). If the row exists,
     the stored value silently overrides a blank `watermark_value`
     param. If the param IS set explicitly (e.g. a manual override
     for a backfill), the param wins.
  2. After the upstream rows materialise, the node computes
     `MAX(cursor_column)` and upserts back into the table along with
     `last_run_at` + `rows_last_run`.
  3. Reset State (delete the row) sends the next run back to the
     beginning — full refresh semantics until a new watermark lands.

What this is NOT
----------------
  * NOT a CDC replacement. Log-based CDC (Postgres pgoutput, MySQL
    binlog, etc.) goes through the dedicated `cdc_source` node which
    has its own slot+publication state. This store covers the much
    more common "WHERE updated_at > ?" cursor pattern.
  * NOT a generic checkpoint store. That's `pipeline_checkpoints`
    (per-run-id step outcomes for resume). This store is per-source
    cursor only.
  * NOT thread-safe across processes — relies on the underlying
    `Database` (SQLite WAL + per-thread connection) for that.

Failure mode
------------
Best-effort: a write/read error is logged but never raises. A failed
write just means the next run sees a stale cursor and re-loads some
rows — that's degraded but correct (upsert/merge sinks dedupe). A
failed read means the source falls back to whatever `watermark_value`
the user set, exactly the legacy behaviour.

Schema
------
See `_migrate_v31_sync_state` in fpulse/storage/database.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class SyncState(BaseModel):
    """One row of `sync_state` — the latest cursor watermark for a
    single (workflow, step) pair running in incremental mode."""

    workflow_id: str
    step_id: str
    cursor_column: str
    last_cursor: Optional[str] = None
    last_run_at: Optional[str] = None     # ISO 8601 UTC
    rows_last_run: int = 0


class SyncStateStore:
    """SQLite-backed per-source cursor store.

    Injected at startup the same way other stores are wired
    (see fpulse.main app_state). Best-effort logged + swallowed so a
    cursor read/write failure never breaks a pipeline run.
    """

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db) -> None:
        self._db = db

    # ── Reads ────────────────────────────────────────────────────────

    def get(self, workflow_id: str, step_id: str) -> Optional[SyncState]:
        """Return the stored cursor state for this (workflow, step), or
        None if no incremental run has completed yet."""
        if self._db is None:
            logger.debug("SyncStateStore.get: no db wired; returning None")
            return None
        try:
            # Database.fetchone returns a dict, not a tuple — index by
            # column name so the call matches the rest of the codebase.
            row = self._db.fetchone(
                "SELECT workflow_id, step_id, cursor_column, last_cursor, "
                "last_run_at, rows_last_run FROM sync_state "
                "WHERE workflow_id = ? AND step_id = ?",
                (workflow_id, step_id),
            )
        except Exception as exc:  # noqa: BLE001 — never fail the run on cursor read
            logger.warning(
                "SyncStateStore.get failed for workflow=%s step=%s: %s",
                workflow_id, step_id, exc,
            )
            return None

        if not row:
            return None
        return SyncState(
            workflow_id=row["workflow_id"],
            step_id=row["step_id"],
            cursor_column=row["cursor_column"],
            last_cursor=row["last_cursor"],
            last_run_at=row["last_run_at"],
            rows_last_run=int(row.get("rows_last_run") or 0),
        )

    # ── Writes ───────────────────────────────────────────────────────

    def upsert(self, state: SyncState) -> None:
        """Insert-or-replace the cursor watermark for this (workflow, step)."""
        if self._db is None:
            logger.debug("SyncStateStore.upsert: no db wired; skipping")
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._db.execute(
                """
                INSERT OR REPLACE INTO sync_state
                (workflow_id, step_id, cursor_column, last_cursor,
                 last_run_at, rows_last_run, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.workflow_id, state.step_id, state.cursor_column,
                    state.last_cursor, state.last_run_at or now,
                    state.rows_last_run, now,
                ),
            )
            self._db.commit()
        except Exception as exc:  # noqa: BLE001 — never fail the run on cursor write
            logger.warning(
                "SyncStateStore.upsert failed for workflow=%s step=%s: %s",
                state.workflow_id, state.step_id, exc,
            )

    def reset(self, workflow_id: str, step_id: str) -> None:
        """Delete the cursor for this (workflow, step) so the next run
        behaves like a full refresh again. Surfaced as the UI's
        "Reset State" action."""
        if self._db is None:
            logger.debug("SyncStateStore.reset: no db wired; skipping")
            return
        try:
            self._db.execute(
                "DELETE FROM sync_state WHERE workflow_id = ? AND step_id = ?",
                (workflow_id, step_id),
            )
            self._db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SyncStateStore.reset failed for workflow=%s step=%s: %s",
                workflow_id, step_id, exc,
            )

    def list_for_workflow(self, workflow_id: str) -> list[SyncState]:
        """Enumerate every incremental cursor for a workflow — used by
        the per-pipeline lineage / observability panels."""
        if self._db is None:
            return []
        try:
            rows = self._db.fetchall(
                "SELECT workflow_id, step_id, cursor_column, last_cursor, "
                "last_run_at, rows_last_run FROM sync_state "
                "WHERE workflow_id = ? ORDER BY step_id",
                (workflow_id,),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SyncStateStore.list_for_workflow failed for %s: %s",
                workflow_id, exc,
            )
            return []
        # Database.fetchall returns list[dict] — index by column name.
        return [
            SyncState(
                workflow_id=r["workflow_id"], step_id=r["step_id"],
                cursor_column=r["cursor_column"],
                last_cursor=r["last_cursor"],
                last_run_at=r["last_run_at"],
                rows_last_run=int(r.get("rows_last_run") or 0),
            )
            for r in rows
        ]


# Module-level singleton — wired at startup in fpulse.main the same
# way checkpoint_store and other stores are. Importing this module
# does NOT touch the DB; the binding happens via set_db().
sync_state_store = SyncStateStore()
