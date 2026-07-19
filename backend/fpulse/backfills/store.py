"""SQLite-backed backfill store.

Owns reads and writes against the ``backfill_runs`` table. The table
holds both parent rows (the backfill itself) and child rows (each window),
distinguished by ``parent_backfill_id`` ('' for parent, parent.id for
children).

Aggregate counters on parent rows are kept in sync by ``update_status``
— callers update child status; the store recomputes the parent's
succeeded/failed/skipped tallies and bumps parent.status to running /
success / partial / failed accordingly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Iterable

from .models import Backfill, BackfillRun, BackfillStatus

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BackfillStore:
    """Thin store on top of a fpulse.storage.database.Database handle.

    Best-effort writes — a store failure on a child-status update logs a
    warning but never raises, mirroring CheckpointStore's contract. The
    backfill orchestrator should still be able to make forward progress
    even if a single status write fails.
    """

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db):
        self._db = db

    # ── Writes ───────────────────────────────────────────────────────

    def create_parent(self, parent: Backfill) -> Backfill:
        """Insert a parent backfill row. Returns the parent unchanged."""
        if self._db is None:
            raise RuntimeError("BackfillStore.create_parent: no db wired")
        self._db.insert_json(
            "backfill_runs",
            parent.id,
            json.loads(parent.model_dump_json()),
            pipeline_id=parent.pipeline_id,
            parent_backfill_id="",
            workspace_id=parent.workspace_id or "default",
            status=parent.status.value if isinstance(parent.status, BackfillStatus) else str(parent.status),
            window_start=parent.window_start,
            window_end=parent.window_end,
            created_at=parent.created_at.isoformat(),
            updated_at=parent.updated_at.isoformat(),
        )
        return parent

    def create_children(self, parent_id: str, children: Iterable[BackfillRun]) -> int:
        """Bulk-insert child window rows. Returns count inserted."""
        if self._db is None:
            raise RuntimeError("BackfillStore.create_children: no db wired")
        count = 0
        for child in children:
            child.parent_backfill_id = parent_id
            self._db.insert_json(
                "backfill_runs",
                child.id,
                json.loads(child.model_dump_json()),
                pipeline_id=child.pipeline_id,
                parent_backfill_id=parent_id,
                workspace_id=child.workspace_id or "default",
                status=child.status.value if isinstance(child.status, BackfillStatus) else str(child.status),
                window_start=child.window_start,
                window_end=child.window_end,
                created_at=child.created_at.isoformat(),
                updated_at=child.updated_at.isoformat(),
            )
            count += 1
        return count

    def update_status(
        self,
        row_id: str,
        status: BackfillStatus | str,
        *,
        execution_id: str | None = None,
        error_message: str | None = None,
        completed: bool = False,
        params_template: dict | None = None,
    ) -> BackfillRun | None:
        """Patch a single row's status. Use ``completed=True`` to stamp completed_at."""
        if self._db is None:
            return None
        try:
            existing = self.get(row_id)
            if existing is None:
                return None
            existing.status = BackfillStatus(status) if not isinstance(status, BackfillStatus) else status
            if execution_id is not None:
                existing.execution_id = execution_id
            if error_message is not None:
                existing.error_message = error_message[:4000]
            if params_template is not None:
                existing.params_template = dict(params_template)
            if existing.started_at is None and existing.status in (BackfillStatus.RUNNING, BackfillStatus.SUCCESS, BackfillStatus.FAILED):
                existing.started_at = _now_iso()
            if completed:
                existing.completed_at = _now_iso()
            existing.updated_at = datetime.now(timezone.utc)
            self._db.insert_json(
                "backfill_runs",
                existing.id,
                json.loads(existing.model_dump_json()),
                pipeline_id=existing.pipeline_id,
                parent_backfill_id=existing.parent_backfill_id,
                workspace_id=existing.workspace_id or "default",
                status=existing.status.value,
                window_start=existing.window_start,
                window_end=existing.window_end,
                created_at=existing.created_at.isoformat(),
                updated_at=existing.updated_at.isoformat(),
            )
            # If this was a child row, recompute parent aggregates.
            if existing.parent_backfill_id:
                self._recompute_parent_aggregates(existing.parent_backfill_id)
            return existing
        except Exception as exc:  # noqa: BLE001
            logger.warning("BackfillStore.update_status(%s) failed: %s", row_id, exc)
            return None

    def _recompute_parent_aggregates(self, parent_id: str) -> None:
        """Walk children and roll their statuses up to the parent row."""
        children = self.list_children(parent_id)
        succeeded = sum(1 for c in children if c.status == BackfillStatus.SUCCESS)
        failed = sum(1 for c in children if c.status == BackfillStatus.FAILED)
        skipped = sum(1 for c in children if c.status == BackfillStatus.SKIPPED)
        running = sum(1 for c in children if c.status == BackfillStatus.RUNNING)
        total = len(children)
        parent = self.get(parent_id)
        if parent is None:
            return
        parent.total_windows = total
        parent.succeeded_windows = succeeded
        parent.failed_windows = failed
        parent.skipped_windows = skipped
        # Aggregate status — don't overwrite a terminal CANCELLED.
        if parent.status == BackfillStatus.CANCELLED:
            pass
        elif running > 0 or (succeeded + failed + skipped) < total:
            parent.status = BackfillStatus.RUNNING
        elif failed == 0 and succeeded > 0:
            parent.status = BackfillStatus.SUCCESS
        elif failed > 0 and succeeded > 0:
            parent.status = BackfillStatus.PARTIAL
        elif failed > 0:
            parent.status = BackfillStatus.FAILED
        if parent.status in (BackfillStatus.SUCCESS, BackfillStatus.FAILED, BackfillStatus.PARTIAL):
            if parent.completed_at is None:
                parent.completed_at = _now_iso()
        parent.updated_at = datetime.now(timezone.utc)
        try:
            self._db.insert_json(
                "backfill_runs",
                parent.id,
                json.loads(parent.model_dump_json()),
                pipeline_id=parent.pipeline_id,
                parent_backfill_id="",
                workspace_id=parent.workspace_id or "default",
                status=parent.status.value,
                window_start=parent.window_start,
                window_end=parent.window_end,
                created_at=parent.created_at.isoformat(),
                updated_at=parent.updated_at.isoformat(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("BackfillStore: parent rollup write failed for %s: %s", parent_id, exc)

    # ── Reads ────────────────────────────────────────────────────────

    def get(self, row_id: str, workspace_id: str | None = None) -> BackfillRun | None:
        """Fetch a single row (parent or child). Workspace-scoped."""
        if self._db is None:
            return None
        data = self._db.get_json("backfill_runs", row_id)
        if data is None:
            return None
        if workspace_id is not None and data.get("workspace_id", "default") != workspace_id:
            return None
        return BackfillRun(**data)

    def list_parents(self, workspace_id: str | None = None, pipeline_id: str | None = None) -> list[BackfillRun]:
        """List every parent backfill, newest first."""
        if self._db is None:
            return []
        where = "parent_backfill_id = ''"
        params: list = []
        if workspace_id is not None:
            where += " AND workspace_id = ?"
            params.append(workspace_id)
        if pipeline_id is not None:
            where += " AND pipeline_id = ?"
            params.append(pipeline_id)
        rows = self._db.list_json(
            "backfill_runs",
            where=where,
            params=tuple(params),
            order_by="created_at DESC",
        )
        return [BackfillRun(**r) for r in rows]

    def list_children(self, parent_id: str) -> list[BackfillRun]:
        """List every window row under a parent, oldest-first by window_start."""
        if self._db is None:
            return []
        rows = self._db.list_json(
            "backfill_runs",
            where="parent_backfill_id = ?",
            params=(parent_id,),
            order_by="window_start ASC",
        )
        return [BackfillRun(**r) for r in rows]

    def cancel(self, parent_id: str, workspace_id: str | None = None) -> bool:
        """Mark a backfill cancelled — does NOT stop in-flight executions.

        The orchestrator polls this status between window dispatches and
        bails out on the next loop iteration. In-progress windows finish
        naturally. Returns True if the row was updated.
        """
        parent = self.get(parent_id, workspace_id=workspace_id)
        if parent is None or parent.status in (
            BackfillStatus.SUCCESS, BackfillStatus.FAILED,
            BackfillStatus.PARTIAL, BackfillStatus.CANCELLED,
        ):
            return False
        parent.status = BackfillStatus.CANCELLED
        parent.completed_at = _now_iso()
        parent.updated_at = datetime.now(timezone.utc)
        self._db.insert_json(
            "backfill_runs",
            parent.id,
            json.loads(parent.model_dump_json()),
            pipeline_id=parent.pipeline_id,
            parent_backfill_id="",
            workspace_id=parent.workspace_id or "default",
            status=parent.status.value,
            window_start=parent.window_start,
            window_end=parent.window_end,
            created_at=parent.created_at.isoformat(),
            updated_at=parent.updated_at.isoformat(),
        )
        return True


# Module-level singleton — wired to db at app startup, mirrors the
# checkpoint_store pattern.
_backfill_store = BackfillStore()


def get_backfill_store() -> BackfillStore:
    return _backfill_store
