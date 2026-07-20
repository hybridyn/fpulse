"""
Detailed execution logging — structured, queryable, comparable.

Stores complete execution logs with all events, timing data, and
step-level details in SQLite for historical analysis.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fpulse.storage.database import Database


class ExecutionLogger:
    """Detailed structured logging for pipeline executions.

    Stores complete execution records including all real-time events,
    step timing, row counts, and error details. Supports comparison
    between executions and aggregate statistics.
    """

    TABLE_NAME = "execution_logs"

    def __init__(self, db: Database):
        self.db = db
        self._ensure_table()

    def _ensure_table(self):
        """Create the execution_logs table if it doesn't exist.

        Also performs an idempotent workspace_id migration for older
        installs so every row can be tenant-scoped. Legacy rows are
        back-filled from ``workflow_versions`` where possible and fall
        through to the sentinel 'default' workspace otherwise.
        """
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS execution_logs (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                workflow_name TEXT DEFAULT '',
                status TEXT DEFAULT 'running',
                started_at TEXT NOT NULL,
                completed_at TEXT,
                duration_ms REAL DEFAULT 0,
                total_steps INTEGER DEFAULT 0,
                completed_steps INTEGER DEFAULT 0,
                failed_steps INTEGER DEFAULT 0,
                total_rows_processed INTEGER DEFAULT 0,
                triggered_by TEXT DEFAULT 'manual',
                error_summary TEXT,
                step_events TEXT DEFAULT '[]',
                result_data TEXT DEFAULT '{}',
                workspace_id TEXT DEFAULT 'default',
                created_at TEXT NOT NULL
            )
        """)

        # Idempotent ALTER for older installs where the column is
        # missing — sqlite raises OperationalError if it already exists.
        import sqlite3
        try:
            self.db.execute(
                "ALTER TABLE execution_logs ADD COLUMN workspace_id TEXT DEFAULT 'default'"
            )
        except sqlite3.OperationalError:
            pass

        # Back-fill legacy rows from their parent workflow's workspace.
        # Safe to re-run on every startup — only touches still-default rows.
        try:
            self.db.execute("""
                UPDATE execution_logs
                SET workspace_id = COALESCE(
                    (SELECT wv.workspace_id FROM workflow_versions wv
                     WHERE wv.workflow_id = execution_logs.workflow_id
                     ORDER BY wv.version DESC LIMIT 1),
                    'default')
                WHERE workspace_id = 'default' OR workspace_id IS NULL
            """)
        except sqlite3.OperationalError:
            # workflow_versions may not exist in fresh installs — ignore.
            pass

        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_exelog_execution
            ON execution_logs(execution_id)
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_exelog_workflow
            ON execution_logs(workflow_id)
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_exelog_status
            ON execution_logs(status)
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_exelog_started
            ON execution_logs(started_at)
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_exelog_workspace
            ON execution_logs(workspace_id)
        """)
        self.db.commit()

    # ── Write ──

    def log_execution(
        self,
        execution_id: str,
        workflow_id: str,
        workflow_name: str = "",
        events: list[dict[str, Any]] | None = None,
        result: dict[str, Any] | None = None,
        triggered_by: str = "manual",
        workspace_id: str = "default",
    ) -> str:
        """Store a complete execution log with all events.

        Args:
            execution_id: Unique ID for this execution.
            workflow_id: The workflow that was executed.
            workflow_name: Human-readable workflow name.
            events: List of real-time events from RealtimeExecutor.
            result: The full WorkflowRunResult as dict.
            triggered_by: How the execution was triggered (manual/schedule/webhook/websocket).

        Returns:
            The log record ID.
        """
        events = events or []
        result = result or {}

        log_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()

        # Extract timing from events
        started_at = now
        completed_at = None
        duration_ms = 0.0
        status = result.get("status", "unknown")
        total_steps = 0
        completed_steps = 0
        failed_steps = 0
        total_rows = 0
        error_summary = None

        for event in events:
            etype = event.get("type", "")
            if etype == "workflow_started":
                started_at = event.get("started_at", now)
                total_steps = event.get("total_steps", 0)
            elif etype == "step_completed":
                completed_steps += 1
                total_rows += event.get("row_count", 0)
            elif etype == "step_error":
                failed_steps += 1
                if not error_summary:
                    step_label = event.get("label", event.get("step_id", ""))
                    error_summary = f"{step_label}: {event.get('error', 'Unknown error')}"
            elif etype == "workflow_completed":
                completed_at = event.get("timestamp")
                duration_ms = event.get("total_duration_ms", 0)
                status = event.get("status", status)

        # Fallback from result if events didn't have the data
        if not completed_at:
            completed_at = result.get("completed_at", now)
        if duration_ms == 0:
            duration_ms = result.get("duration_ms", 0)

        self.db.execute(
            """
            INSERT OR REPLACE INTO execution_logs
            (id, execution_id, workflow_id, workflow_name, status,
             started_at, completed_at, duration_ms,
             total_steps, completed_steps, failed_steps,
             total_rows_processed, triggered_by, error_summary,
             step_events, result_data, workspace_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log_id, execution_id, workflow_id, workflow_name, status,
                started_at, completed_at, duration_ms,
                total_steps, completed_steps, failed_steps,
                total_rows, triggered_by, error_summary,
                json.dumps(events, default=str),
                json.dumps(result, default=str),
                workspace_id or "default",
                now,
            ),
        )
        self.db.commit()
        return log_id

    # ── Read ──

    def get_execution_log(
        self,
        execution_id: str,
        workspace_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get detailed log for an execution by execution_id.

        When ``workspace_id`` is provided the lookup is scoped to the
        caller's tenant — a log that exists in a different workspace
        is indistinguishable from a missing log (returns ``None``),
        so callers should translate that into a 404 exactly like they
        would for a genuinely unknown execution id.
        """
        if workspace_id is not None:
            row = self.db.fetchone(
                "SELECT * FROM execution_logs WHERE execution_id = ? AND workspace_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (execution_id, workspace_id),
            )
            if not row:
                row = self.db.fetchone(
                    "SELECT * FROM execution_logs WHERE id = ? AND workspace_id = ?",
                    (execution_id, workspace_id),
                )
        else:
            row = self.db.fetchone(
                "SELECT * FROM execution_logs WHERE execution_id = ? ORDER BY created_at DESC LIMIT 1",
                (execution_id,),
            )
            if not row:
                # Also try by log id
                row = self.db.fetchone(
                    "SELECT * FROM execution_logs WHERE id = ?",
                    (execution_id,),
                )
        if not row:
            return None
        return self._row_to_dict(row)

    def list_executions(
        self,
        workflow_id: str | None = None,
        status: str | None = None,
        triggered_by: str | None = None,
        limit: int = 100,
        offset: int = 0,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List executions with filtering.

        Args:
            workflow_id: Filter by workflow.
            status: Filter by status (success/error/cancelled).
            triggered_by: Filter by trigger type.
            limit: Max results.
            offset: Skip first N results.

        Returns:
            List of execution log summaries (without full events/result).
        """
        conditions = []
        params: list = []

        if workspace_id is not None:
            conditions.append("workspace_id = ?")
            params.append(workspace_id)
        if workflow_id:
            conditions.append("workflow_id = ?")
            params.append(workflow_id)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if triggered_by:
            conditions.append("triggered_by = ?")
            params.append(triggered_by)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        sql = f"""
            SELECT id, execution_id, workflow_id, workflow_name, status,
                   started_at, completed_at, duration_ms,
                   total_steps, completed_steps, failed_steps,
                   total_rows_processed, triggered_by, error_summary, created_at
            FROM execution_logs
            {where}
            ORDER BY started_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        rows = self.db.fetchall(sql, tuple(params))
        return [dict(r) for r in rows]

    def get_execution_events(
        self,
        execution_id: str,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get the event stream for a specific execution.

        When ``workspace_id`` is set the lookup returns an empty list
        for executions belonging to other tenants, matching the
        "treat as missing" convention used elsewhere.
        """
        if workspace_id is not None:
            row = self.db.fetchone(
                "SELECT step_events FROM execution_logs "
                "WHERE execution_id = ? AND workspace_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (execution_id, workspace_id),
            )
            if not row:
                row = self.db.fetchone(
                    "SELECT step_events FROM execution_logs "
                    "WHERE id = ? AND workspace_id = ?",
                    (execution_id, workspace_id),
                )
        else:
            row = self.db.fetchone(
                "SELECT step_events FROM execution_logs WHERE execution_id = ? ORDER BY created_at DESC LIMIT 1",
                (execution_id,),
            )
            if not row:
                row = self.db.fetchone(
                    "SELECT step_events FROM execution_logs WHERE id = ?",
                    (execution_id,),
                )
        if not row:
            return []
        return json.loads(row.get("step_events", "[]"))

    def list_recent(
        self,
        limit: int = 20,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent executions across all workflows.

        ``workspace_id`` filters the result to a single tenant — the
        API layer passes the caller's workspace so operators never see
        activity from other tenants in the recent feed.
        """
        if workspace_id is not None:
            rows = self.db.fetchall(
                """
                SELECT id, execution_id, workflow_id, workflow_name, status,
                       started_at, completed_at, duration_ms,
                       total_steps, completed_steps, failed_steps,
                       total_rows_processed, triggered_by, error_summary, created_at
                FROM execution_logs
                WHERE workspace_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (workspace_id, limit),
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT id, execution_id, workflow_id, workflow_name, status,
                       started_at, completed_at, duration_ms,
                       total_steps, completed_steps, failed_steps,
                       total_rows_processed, triggered_by, error_summary, created_at
                FROM execution_logs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [dict(r) for r in rows]

    # ── Compare ──

    def compare_executions(
        self,
        exec_id_1: str,
        exec_id_2: str,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Compare two executions — diff in timing, row counts, errors.

        Returns a structured comparison with per-step diffs. When
        ``workspace_id`` is supplied, both executions must belong to
        that workspace or the compare is refused with the same
        "not found" shape a single-lookup would produce — preventing
        an attacker from using compare as a cross-tenant oracle.
        """
        log1 = self.get_execution_log(exec_id_1, workspace_id=workspace_id)
        log2 = self.get_execution_log(exec_id_2, workspace_id=workspace_id)

        if not log1 or not log2:
            missing = []
            if not log1:
                missing.append(exec_id_1)
            if not log2:
                missing.append(exec_id_2)
            return {"error": f"Execution(s) not found: {', '.join(missing)}"}

        events1 = log1.get("step_events", [])
        events2 = log2.get("step_events", [])

        # Build step-level maps
        steps1 = self._extract_step_data(events1)
        steps2 = self._extract_step_data(events2)

        all_step_ids = sorted(set(list(steps1.keys()) + list(steps2.keys())))

        step_diffs = []
        for step_id in all_step_ids:
            s1 = steps1.get(step_id, {})
            s2 = steps2.get(step_id, {})
            diff = {
                "step_id": step_id,
                "label": s1.get("label") or s2.get("label", step_id),
                "exec_1": {
                    "status": s1.get("status", "n/a"),
                    "duration_ms": s1.get("duration_ms", 0),
                    "row_count": s1.get("row_count", 0),
                    "error": s1.get("error"),
                },
                "exec_2": {
                    "status": s2.get("status", "n/a"),
                    "duration_ms": s2.get("duration_ms", 0),
                    "row_count": s2.get("row_count", 0),
                    "error": s2.get("error"),
                },
            }

            # Calculate deltas
            dur1 = s1.get("duration_ms", 0) or 0
            dur2 = s2.get("duration_ms", 0) or 0
            rows1 = s1.get("row_count", 0) or 0
            rows2 = s2.get("row_count", 0) or 0

            diff["duration_delta_ms"] = round(dur2 - dur1, 2)
            diff["row_count_delta"] = rows2 - rows1

            if dur1 > 0:
                diff["duration_change_pct"] = round(((dur2 - dur1) / dur1) * 100, 1)
            else:
                diff["duration_change_pct"] = 0

            step_diffs.append(diff)

        return {
            "exec_1": {
                "execution_id": log1.get("execution_id"),
                "workflow_name": log1.get("workflow_name"),
                "status": log1.get("status"),
                "duration_ms": log1.get("duration_ms"),
                "total_rows": log1.get("total_rows_processed"),
                "started_at": log1.get("started_at"),
                "triggered_by": log1.get("triggered_by"),
            },
            "exec_2": {
                "execution_id": log2.get("execution_id"),
                "workflow_name": log2.get("workflow_name"),
                "status": log2.get("status"),
                "duration_ms": log2.get("duration_ms"),
                "total_rows": log2.get("total_rows_processed"),
                "started_at": log2.get("started_at"),
                "triggered_by": log2.get("triggered_by"),
            },
            "summary": {
                "duration_delta_ms": round(
                    (log2.get("duration_ms") or 0) - (log1.get("duration_ms") or 0), 2
                ),
                "row_delta": (log2.get("total_rows_processed") or 0) - (log1.get("total_rows_processed") or 0),
                "status_changed": log1.get("status") != log2.get("status"),
            },
            "step_diffs": step_diffs,
        }

    # ── Stats ──

    def get_execution_stats(
        self,
        workflow_id: str,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate stats for a workflow: avg duration, success rate, common errors.

        Args:
            workflow_id: The workflow to aggregate stats for.
            workspace_id: Optional tenant boundary. When set, rows from
                other workspaces are excluded from the aggregation so
                a caller cannot infer activity in another tenant even
                by guessing a workflow id.

        Returns:
            Aggregate statistics dict.
        """
        if workspace_id is not None:
            rows = self.db.fetchall(
                """
                SELECT status, duration_ms, total_rows_processed,
                       error_summary, started_at, completed_steps, failed_steps, total_steps
                FROM execution_logs
                WHERE workflow_id = ? AND workspace_id = ?
                ORDER BY started_at DESC
                """,
                (workflow_id, workspace_id),
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT status, duration_ms, total_rows_processed,
                       error_summary, started_at, completed_steps, failed_steps, total_steps
                FROM execution_logs
                WHERE workflow_id = ?
                ORDER BY started_at DESC
                """,
                (workflow_id,),
            )

        if not rows:
            return {
                "workflow_id": workflow_id,
                "total_executions": 0,
                "success_count": 0,
                "error_count": 0,
                "cancelled_count": 0,
                "success_rate": 0,
                "avg_duration_ms": 0,
                "min_duration_ms": 0,
                "max_duration_ms": 0,
                "p50_duration_ms": 0,
                "p95_duration_ms": 0,
                "total_rows_processed": 0,
                "avg_rows_per_execution": 0,
                "common_errors": [],
                "recent_trend": [],
            }

        total = len(rows)
        success = [r for r in rows if r["status"] == "success"]
        errors = [r for r in rows if r["status"] == "error"]
        cancelled = [r for r in rows if r["status"] == "cancelled"]

        durations = sorted([r["duration_ms"] for r in rows if r["duration_ms"] and r["duration_ms"] > 0])
        total_rows = sum(r["total_rows_processed"] or 0 for r in rows)

        # Percentile calculation
        def percentile(values: list[float], pct: float) -> float:
            if not values:
                return 0
            idx = int(len(values) * pct / 100)
            idx = min(idx, len(values) - 1)
            return round(values[idx], 2)

        # Common errors (top 5)
        error_counts: dict[str, int] = {}
        for r in errors:
            err = r.get("error_summary") or "Unknown error"
            # Truncate for grouping
            key = err[:120]
            error_counts[key] = error_counts.get(key, 0) + 1

        common_errors = sorted(
            [{"error": k, "count": v} for k, v in error_counts.items()],
            key=lambda x: x["count"],
            reverse=True,
        )[:5]

        # Recent trend (last 10 executions)
        recent_trend = []
        for r in rows[:10]:
            recent_trend.append({
                "started_at": r["started_at"],
                "status": r["status"],
                "duration_ms": r["duration_ms"],
                "rows": r["total_rows_processed"],
            })

        return {
            "workflow_id": workflow_id,
            "total_executions": total,
            "success_count": len(success),
            "error_count": len(errors),
            "cancelled_count": len(cancelled),
            "success_rate": round(len(success) / total * 100, 1) if total > 0 else 0,
            "avg_duration_ms": round(sum(durations) / len(durations), 2) if durations else 0,
            "min_duration_ms": round(durations[0], 2) if durations else 0,
            "max_duration_ms": round(durations[-1], 2) if durations else 0,
            "p50_duration_ms": percentile(durations, 50),
            "p95_duration_ms": percentile(durations, 95),
            "total_rows_processed": total_rows,
            "avg_rows_per_execution": round(total_rows / total, 1) if total > 0 else 0,
            "common_errors": common_errors,
            "recent_trend": recent_trend,
        }

    # ── Helpers ──

    def _row_to_dict(self, row: dict) -> dict[str, Any]:
        """Convert a database row to a rich dict with parsed JSON fields."""
        result = dict(row)
        # Parse JSON fields
        if "step_events" in result and isinstance(result["step_events"], str):
            try:
                result["step_events"] = json.loads(result["step_events"])
            except (json.JSONDecodeError, TypeError):
                result["step_events"] = []
        if "result_data" in result and isinstance(result["result_data"], str):
            try:
                result["result_data"] = json.loads(result["result_data"])
            except (json.JSONDecodeError, TypeError):
                result["result_data"] = {}
        return result

    def _extract_step_data(self, events: list[dict]) -> dict[str, dict]:
        """Extract per-step data from an event list for comparison."""
        steps: dict[str, dict] = {}

        for event in events:
            etype = event.get("type", "")
            step_id = event.get("step_id")
            if not step_id:
                continue

            if step_id not in steps:
                steps[step_id] = {}

            if etype == "step_started":
                steps[step_id]["label"] = event.get("label", step_id)
                steps[step_id]["step_type"] = event.get("step_type")
            elif etype == "step_completed":
                steps[step_id]["status"] = "success"
                steps[step_id]["duration_ms"] = event.get("duration_ms", 0)
                steps[step_id]["row_count"] = event.get("row_count", 0)
            elif etype == "step_error":
                steps[step_id]["status"] = "error"
                steps[step_id]["duration_ms"] = event.get("duration_ms", 0)
                steps[step_id]["error"] = event.get("error")
                steps[step_id]["diagnosis"] = event.get("diagnosis")

        return steps
