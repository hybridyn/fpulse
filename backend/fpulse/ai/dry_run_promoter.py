"""
Dry-run-by-default for new write tools.

Per the round-3 reviewer: "Add write tools only after Step 1.5b governance
is complete" + "set dry-run as the default mode for all new write-capable
tools until they have passed a reliability threshold in internal testing."

Implementation:
  - First N successful invocations of a write tool by a given user are
    silently forced into dry_run mode regardless of the caller's intent.
  - After threshold (default 3 successful dry-runs), live mode unlocks
    for that (user, tool) pair.
  - Workspace policy `require_dry_run_first: false` exempts a tool from
    this gate (Plus track — read from workspace_ai_policy_store; OSS uses
    the global default).

Persisted in SQLite so the threshold tracks across process restarts.

Tunable via env:
  - ``FPULSE_AGENT_DRY_RUN_THRESHOLD`` default 3 (clamp 0-100; 0 disables the gate)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _threshold() -> int:
    raw = os.environ.get("FPULSE_AGENT_DRY_RUN_THRESHOLD", "").strip()
    if not raw:
        return 3
    try:
        v = int(raw)
    except ValueError:
        return 3
    return max(0, min(100, v))


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS ai_dry_run_counters (
    user_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT,
    PRIMARY KEY (user_id, tool_name)
);
"""


@dataclass
class DryRunPromoter:
    """Enforces dry-run-by-default for the first N successful invocations
    of a write-tier tool, per (user, tool) pair.

    Decisions:
      should_force_dry_run(user_id, tool_name) -> bool
        Returns True when the user has fewer than threshold successful runs
        of this tool. Agent loop sets ToolContext.dry_run=True accordingly.

      record_success(user_id, tool_name)
        Increment the counter. Called only on `outcome=success` from
        _execute_tool. Failures do NOT count toward the threshold —
        they neither help nor hurt the user's reliability score.
    """

    _db: Any = None

    def __post_init__(self) -> None:
        if self._db is not None:
            self._ensure_schema()

    def set_db(self, db) -> None:
        self._db = db
        if db is not None:
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            self._db.execute(_CREATE_SQL)
            self._db.commit()
        except Exception as exc:
            logger.warning("DryRunPromoter: schema init failed: %s", exc)

    def success_count(self, user_id: str, tool_name: str) -> int:
        if self._db is None or not user_id or not tool_name:
            return 0
        try:
            rows = self._db.fetchall(
                "SELECT success_count FROM ai_dry_run_counters "
                "WHERE user_id = ? AND tool_name = ? LIMIT 1",
                (user_id, tool_name),
            )
        except Exception:
            return 0
        return int(rows[0]["success_count"]) if rows else 0

    def should_force_dry_run(self, user_id: str | None, tool_name: str) -> bool:
        """Force dry_run iff the user has run this tool fewer than threshold
        times successfully.

        Anonymous users (no user_id): always force dry-run for write tools,
        regardless of the threshold setting. That matches the policy rule
        "anonymous_blocked_for_writes" — belt-and-suspenders posture. The
        anonymous check has to fire BEFORE the threshold-disabled short-
        circuit so threshold=0 (named-user opt-out) doesn't accidentally
        also let anonymous through.
        """
        if not user_id or user_id == "anonymous":
            return True
        threshold = _threshold()
        if threshold <= 0:
            return False
        return self.success_count(user_id, tool_name) < threshold

    def record_success(self, user_id: str | None, tool_name: str) -> None:
        """Bump the counter. Only call after a non-dry-run success."""
        if self._db is None:
            return
        if not user_id or user_id == "anonymous" or not tool_name:
            return
        try:
            self._db.execute(
                """
                INSERT INTO ai_dry_run_counters (user_id, tool_name, success_count, last_run_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(user_id, tool_name) DO UPDATE SET
                    success_count = success_count + 1,
                    last_run_at = excluded.last_run_at
                """,
                (user_id, tool_name, datetime.now(timezone.utc).isoformat()),
            )
            self._db.commit()
        except Exception as exc:
            logger.warning("DryRunPromoter.record_success failed: %s", exc)
