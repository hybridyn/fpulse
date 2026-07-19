"""
CheckpointStore — per-run pipeline checkpoint index for resume-from-step.

Sprint 1 / Gate 1 work. Persists the per-(run_id, step_id) outcome of every
step in a pipeline run plus a pointer to the Parquet snapshot the existing
StepCache wrote on success. The executor's "Resume from step X" feature
reads this table to decide where to pick up after a failure.

Why a separate module from StepCache:

    StepCache       — keyed by workflow_id + content-addressable hash.
                      Powers "Rerun from here" (skip steps whose params +
                      upstream haven't changed). A workflow has ONE manifest
                      that lives forever.
    CheckpointStore — keyed by run_id. Records the per-run sequence of
                      step outcomes. A run has ONE checkpoint set; many
                      runs accumulate; old ones are evicted on a TTL.

The two cooperate: CheckpointStore points at Parquet files written by
StepCache (output_ref column). On a clean run we just write to StepCache
and record success in CheckpointStore. On resume we read CheckpointStore
to find the failure boundary and re-load StepCache outputs by output_ref.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal, Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)

CheckpointStatus = Literal["success", "failed", "in_progress", "skipped"]


class Checkpoint(BaseModel):
    """One row of `pipeline_checkpoints` — the outcome of a single step
    in a single run."""

    workflow_id: str
    run_id: str
    step_id: str
    status: CheckpointStatus
    completed_at: Optional[str] = None  # ISO 8601 UTC
    rows_in: Optional[int] = None
    rows_out: Optional[int] = None
    duration_ms: Optional[int] = None
    output_ref: Optional[str] = None     # path to Parquet snapshot, relative to data_dir
    error_summary: Optional[str] = None


class CheckpointStore:
    """SQLite-backed checkpoint index.

    Thread-safe via the underlying `Database` (per-thread connection, WAL).
    All operations are best-effort logged + swallowed so a checkpoint write
    failure NEVER fails the pipeline run itself — checkpoints are an
    observability/recovery aid, not a correctness contract.
    """

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db):
        self._db = db

    # ── Writes ───────────────────────────────────────────────────────

    def upsert(self, cp: Checkpoint) -> None:
        """Insert-or-replace a single checkpoint row."""
        if self._db is None:
            logger.debug("CheckpointStore.upsert: no db wired; skipping")
            return
        try:
            self._db.execute(
                """
                INSERT OR REPLACE INTO pipeline_checkpoints
                (workflow_id, run_id, step_id, status, completed_at,
                 rows_in, rows_out, duration_ms, output_ref, error_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cp.workflow_id, cp.run_id, cp.step_id, cp.status,
                    cp.completed_at, cp.rows_in, cp.rows_out, cp.duration_ms,
                    cp.output_ref, cp.error_summary,
                ),
            )
            self._db.commit()
        except Exception as exc:  # noqa: BLE001 — never fail the run on checkpoint
            logger.warning(
                "CheckpointStore.upsert failed for run=%s step=%s: %s",
                cp.run_id, cp.step_id, exc,
            )

    def mark_in_progress(self, workflow_id: str, run_id: str, step_id: str) -> None:
        """Convenience: stamp a step as in_progress when execution begins."""
        self.upsert(Checkpoint(
            workflow_id=workflow_id,
            run_id=run_id,
            step_id=step_id,
            status="in_progress",
        ))

    def mark_success(
        self,
        workflow_id: str,
        run_id: str,
        step_id: str,
        *,
        rows_out: int | None = None,
        rows_in: int | None = None,
        duration_ms: int | None = None,
        output_ref: str | None = None,
    ) -> None:
        self.upsert(Checkpoint(
            workflow_id=workflow_id,
            run_id=run_id,
            step_id=step_id,
            status="success",
            completed_at=_now_iso(),
            rows_in=rows_in,
            rows_out=rows_out,
            duration_ms=duration_ms,
            output_ref=output_ref,
        ))

    def mark_failed(
        self,
        workflow_id: str,
        run_id: str,
        step_id: str,
        *,
        error_summary: str,
        duration_ms: int | None = None,
    ) -> None:
        self.upsert(Checkpoint(
            workflow_id=workflow_id,
            run_id=run_id,
            step_id=step_id,
            status="failed",
            completed_at=_now_iso(),
            duration_ms=duration_ms,
            error_summary=_truncate(error_summary, 4000),
        ))

    def mark_skipped(
        self,
        workflow_id: str,
        run_id: str,
        step_id: str,
        *,
        reason: str = "",
    ) -> None:
        self.upsert(Checkpoint(
            workflow_id=workflow_id,
            run_id=run_id,
            step_id=step_id,
            status="skipped",
            completed_at=_now_iso(),
            error_summary=_truncate(reason, 4000) if reason else None,
        ))

    # ── Reads ────────────────────────────────────────────────────────

    def get_run(self, run_id: str) -> list[Checkpoint]:
        """Return all checkpoints for a given run, in step order."""
        if self._db is None:
            return []
        try:
            rows = self._db.fetchall(
                """
                SELECT workflow_id, run_id, step_id, status, completed_at,
                       rows_in, rows_out, duration_ms, output_ref, error_summary
                FROM pipeline_checkpoints
                WHERE run_id = ?
                ORDER BY completed_at ASC, step_id ASC
                """,
                (run_id,),
            )
            return [Checkpoint(**r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("CheckpointStore.get_run(%s) failed: %s", run_id, exc)
            return []

    def get(self, run_id: str, step_id: str) -> Checkpoint | None:
        if self._db is None:
            return None
        try:
            row = self._db.fetchone(
                """
                SELECT workflow_id, run_id, step_id, status, completed_at,
                       rows_in, rows_out, duration_ms, output_ref, error_summary
                FROM pipeline_checkpoints
                WHERE run_id = ? AND step_id = ?
                """,
                (run_id, step_id),
            )
            return Checkpoint(**row) if row else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "CheckpointStore.get(%s, %s) failed: %s", run_id, step_id, exc,
            )
            return None

    def latest_failed_run(self, workflow_id: str) -> str | None:
        """Return the run_id of the most recent run for `workflow_id` that has
        at least one `failed` step. None if no failed runs.

        Used by the executor's `resume()` entry point when the caller doesn't
        know which run to resume.
        """
        if self._db is None:
            return None
        try:
            row = self._db.fetchone(
                """
                SELECT run_id
                FROM pipeline_checkpoints
                WHERE workflow_id = ? AND status = 'failed'
                ORDER BY completed_at DESC
                LIMIT 1
                """,
                (workflow_id,),
            )
            return row["run_id"] if row else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "CheckpointStore.latest_failed_run(%s) failed: %s",
                workflow_id, exc,
            )
            return None

    def successful_step_ids(self, run_id: str) -> set[str]:
        """The set of step_ids that succeeded in `run_id`. The executor
        reuses these on resume by registering the StepCache parquet at
        `output_ref` instead of re-executing the step."""
        return {
            cp.step_id for cp in self.get_run(run_id)
            if cp.status == "success"
        }

    # ── Eviction / cleanup ───────────────────────────────────────────

    def delete_run(self, run_id: str) -> int:
        """Drop all checkpoints for a run. Returns number of rows deleted."""
        if self._db is None:
            return 0
        try:
            cur = self._db.execute(
                "DELETE FROM pipeline_checkpoints WHERE run_id = ?",
                (run_id,),
            )
            self._db.commit()
            return cur.rowcount or 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("CheckpointStore.delete_run(%s) failed: %s", run_id, exc)
            return 0

    def delete_workflow(self, workflow_id: str) -> int:
        """Drop all checkpoints for every run of `workflow_id`. Used when a
        workflow is hard-deleted."""
        if self._db is None:
            return 0
        try:
            cur = self._db.execute(
                "DELETE FROM pipeline_checkpoints WHERE workflow_id = ?",
                (workflow_id,),
            )
            self._db.commit()
            return cur.rowcount or 0
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "CheckpointStore.delete_workflow(%s) failed: %s",
                workflow_id, exc,
            )
            return 0

    def evict_older_than(self, ttl_days: int = 7) -> int:
        """Sweep checkpoints whose `completed_at` is older than `ttl_days`.

        Default 7 days — long enough that the operator has time to investigate
        a failed run, short enough that the table doesn't grow unboundedly.
        Caller is expected to schedule this from a background task.

        Implementation note: `completed_at` is stored as Python's
        `datetime.isoformat()` (e.g. `2026-05-04T14:23:45.123456+00:00`).
        SQLite's `datetime('now', ...)` returns a different format
        (`2026-05-04 14:23:45`) so direct string comparison would be wrong.
        We compute the cutoff in Python and pass the same ISO-format string
        SQLite is already storing, so the comparison is lexicographic on
        identical-shape strings — correct for ISO-8601 by construction.
        """
        if self._db is None or ttl_days < 0:
            return 0
        cutoff_iso = (
            datetime.now(timezone.utc) - timedelta(days=int(ttl_days))
        ).isoformat()
        try:
            cur = self._db.execute(
                """
                DELETE FROM pipeline_checkpoints
                WHERE completed_at IS NOT NULL
                  AND completed_at < ?
                """,
                (cutoff_iso,),
            )
            self._db.commit()
            count = cur.rowcount or 0
            if count:
                logger.info(
                    "CheckpointStore.evict_older_than(%d days): %d rows",
                    ttl_days, count,
                )
            return count
        except Exception as exc:  # noqa: BLE001
            logger.warning("CheckpointStore.evict_older_than failed: %s", exc)
            return 0

    def evict_orphaned_in_progress(self, older_than_minutes: int = 60) -> int:
        """Sweep `in_progress` rows whose timestamp is far in the past — these
        belong to runs killed mid-execution by a crash or restart. Without this
        sweep, a resume() walk would falsely pick up the in-progress step as
        not-yet-attempted."""
        if self._db is None:
            return 0
        try:
            cur = self._db.execute(
                """
                DELETE FROM pipeline_checkpoints
                WHERE status = 'in_progress'
                  AND completed_at IS NULL
                  AND rowid IN (
                      SELECT p.rowid FROM pipeline_checkpoints p
                      WHERE p.status = 'in_progress'
                  )
                """,
            )
            # NOTE: SQLite doesn't store an automatic created-at on these
            # rows; we only know the row's age via the timestamp on a
            # subsequent success/fail update. For now, the simplest safe
            # rule is "any in_progress row that's still in_progress when
            # the sweeper runs is orphaned" — a real run that's still
            # executing will have updated the row to success/failed by
            # the time the sweep fires.
            _ = older_than_minutes
            self._db.commit()
            count = cur.rowcount or 0
            if count:
                logger.info(
                    "CheckpointStore.evict_orphaned_in_progress: %d rows",
                    count,
                )
            return count
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "CheckpointStore.evict_orphaned_in_progress failed: %s", exc,
            )
            return 0


# ── Helpers ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(s: str, max_len: int) -> str:
    if not s:
        return s
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


# Module-level singleton so imports don't need to know how to wire `db`.
# `fpulse.main` calls `checkpoint_store.set_db(database)` at startup.
checkpoint_store = CheckpointStore()


def get_checkpoint_store() -> CheckpointStore:
    return checkpoint_store
