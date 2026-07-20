"""SQLite-backed alert store.

Stage 3b dual-write (2026-04-20):
  add_log() dual-writes to Postgres when a PG handle is wired in via
  set_pg(). SQLite remains source of truth; PG is best-effort. Only
  the alert_logs table is dual-written — alert_rules is a mutable
  config surface (update_rule, trigger counters) and stays SQLite-only
  during Stage 3b.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .models import AlertRule, AlertLog

if TYPE_CHECKING:
    from fpulse.storage.database_pg import PostgresDatabase

logger = logging.getLogger(__name__)

# Stage 3b telemetry — exposed via /api/metrics alongside audit +
# lifecycle counters. Same 5-outcome shape so operators can line up
# all three stores in Grafana.
_DUAL_WRITE_STATS: dict[str, int] = {
    "sqlite_ok": 0,
    "sqlite_failed": 0,
    "pg_ok": 0,
    "pg_failed": 0,
    "pg_skipped_no_loop": 0,
}

# PR6 — shadow-read counter matching lifecycle + audit shapes.
_SHADOW_READ_STATS: dict[str, int] = {
    "match": 0,
    "mismatch": 0,
    "pg_failed": 0,
    "pg_skipped_no_loop": 0,
    "pg_skipped_disabled": 0,
}


def get_dual_write_stats() -> dict[str, int]:
    """Snapshot of alert_logs dual-write counters for the metrics endpoint."""
    return dict(_DUAL_WRITE_STATS)


def get_shadow_read_stats() -> dict[str, int]:
    """Snapshot of alert_logs shadow-read counters for the metrics endpoint."""
    return dict(_SHADOW_READ_STATS)


def _alert_log_key(row: dict | None) -> str | None:
    """Stable identity for shadow-read comparison. Uses `id`, falls
    back to (rule_id, triggered_at, workflow_id)."""
    if not row:
        return None
    rid = row.get("id")
    if rid:
        return str(rid)
    return (
        f"{row.get('rule_id', '')}|"
        f"{row.get('triggered_at', '')}|"
        f"{row.get('workflow_id', '')}"
    )


class AlertStore:
    """Alert rule and log store backed by SQLite.

    Stage 3b: optional PG dual-write for alert_logs via set_pg(). When
    set, add_log() fires an async background task to write the log row
    to PG. Rules stay SQLite-only."""

    def __init__(self, db=None):
        self._db = db
        # Stage 3b — Postgres handle for alert_logs dual-write.
        # None = SQLite-only.
        self._pg: "PostgresDatabase | None" = None
        # PR6 — shadow reads off by default. Operator toggles via
        # FPULSE_ALERT_LOGS_SHADOW_READS env var at startup.
        self._shadow_reads_enabled: bool = False

    def set_db(self, db):
        self._db = db

    def set_pg(self, pg: "PostgresDatabase | None") -> None:
        """Wire (or unwire) the Postgres handle for alert_logs dual-write.

        Called from main.py lifespan after pg.init_alert_log_schema().
        Setting to None disables dual-write."""
        self._pg = pg
        if pg is not None:
            logger.info("AlertStore: PG dual-write enabled (alert_logs only)")

    def set_shadow_reads(self, enabled: bool) -> None:
        """PR6: toggle shadow reads for alert_logs. When True and a PG
        handle is set, list_logs / list_logs_by_workflow fire a
        background task that re-queries PG and compares. Disabled by
        default."""
        self._shadow_reads_enabled = enabled
        logger.info(
            "AlertStore: shadow reads %s",
            "enabled" if enabled else "disabled",
        )

    def _shadow_reads_active(self) -> bool:
        return self._pg is not None and self._shadow_reads_enabled

    def _save_rule(self, rule: AlertRule):
        data = rule.model_dump(mode="json")
        self._db.insert_json(
            "alert_rules", rule.id, data,
            workflow_id=rule.workflow_id or "",
            project_id=rule.project_id,
            workspace_id=rule.workspace_id or "default",
            enabled=1 if rule.enabled else 0,
            created_at=rule.created_at.isoformat(),
            updated_at=rule.updated_at.isoformat(),
        )

    # ── Rules ──

    def create_rule(self, rule: AlertRule) -> AlertRule:
        self._save_rule(rule)
        return rule

    def get_rule(self, rule_id: str, workspace_id: str | None = None) -> AlertRule | None:
        data = self._db.get_json("alert_rules", rule_id)
        if data is None:
            return None
        if workspace_id is not None:
            if (data.get("workspace_id") or "default") != workspace_id:
                return None
        return AlertRule(**data)

    def list_rules(self, workspace_id: str | None = None) -> list[dict]:
        if workspace_id is not None:
            return self._db.list_json("alert_rules", "workspace_id = ?", (workspace_id,))
        return self._db.list_json("alert_rules")

    def list_rules_by_workflow(
        self, workflow_id: str, workspace_id: str | None = None
    ) -> list[dict]:
        if workspace_id is not None:
            return self._db.list_json(
                "alert_rules",
                "workflow_id = ? AND workspace_id = ?",
                (workflow_id, workspace_id),
            )
        return self._db.list_json("alert_rules", "workflow_id = ?", (workflow_id,))

    def list_rules_by_project(
        self, project_id: str, workspace_id: str | None = None
    ) -> list[dict]:
        if workspace_id is not None:
            return self._db.list_json(
                "alert_rules",
                "project_id = ? AND workspace_id = ?",
                (project_id, workspace_id),
            )
        return self._db.list_json("alert_rules", "project_id = ?", (project_id,))

    def update_rule(
        self, rule_id: str, updates: dict, workspace_id: str | None = None
    ) -> AlertRule | None:
        rule = self.get_rule(rule_id, workspace_id=workspace_id)
        if not rule:
            return None
        for key, value in updates.items():
            if key == "workspace_id":
                continue
            if value is not None and hasattr(rule, key):
                setattr(rule, key, value)
        rule.updated_at = datetime.now(timezone.utc)
        self._save_rule(rule)
        return rule

    def delete_rule(self, rule_id: str, workspace_id: str | None = None) -> bool:
        if workspace_id is not None:
            if not self.get_rule(rule_id, workspace_id=workspace_id):
                return False
        return self._db.delete_row("alert_rules", rule_id)

    # ── Logs ──

    def add_log(self, log: AlertLog) -> AlertLog:
        # Inherit workspace_id from parent rule (authoritative source)
        # so a log of a cross-tenant attempt still lands in the right
        # tenant's audit trail, not the caller's.
        parent = self._db.get_json("alert_rules", log.rule_id)
        if parent:
            log.workspace_id = parent.get("workspace_id") or "default"
        data = log.model_dump(mode="json")

        # ── 1) SQLite write (source of truth) ──
        try:
            self._db.insert_json(
                "alert_logs", log.id, data,
                rule_id=log.rule_id,
                workflow_id=log.workflow_id,
                workspace_id=log.workspace_id or "default",
                triggered_at=log.triggered_at.isoformat(),
            )
            _DUAL_WRITE_STATS["sqlite_ok"] += 1
        except Exception as exc:
            _DUAL_WRITE_STATS["sqlite_failed"] += 1
            logger.error(
                "AlertStore: SQLite write failed for rule=%s workflow=%s: %s",
                log.rule_id, log.workflow_id, exc,
            )
            raise

        # Update rule trigger info — unscoped because this is a
        # system-level write triggered by the execution engine.
        rule = self.get_rule(log.rule_id)
        if rule:
            rule.last_triggered_at = log.triggered_at
            rule.trigger_count += 1
            self._save_rule(rule)

        # ── 2) PG dual-write (best-effort, fire-and-forget) ──
        if self._pg is None:
            return log

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Sync-only caller (tests, CLI). SQLite already has the
            # row; reconcile --backfill can catch PG up later.
            _DUAL_WRITE_STATS["pg_skipped_no_loop"] += 1
            return log

        loop.create_task(
            self._pg_write(
                log.id, log.triggered_at.isoformat(), log.rule_id,
                log.workflow_id, log.workspace_id or "default", data,
            ),
            name=f"alert-log-pg-write-{log.id}",
        )
        return log

    async def _pg_write(
        self,
        entry_id: str,
        triggered_at: str,
        rule_id: str,
        workflow_id: str,
        workspace_id: str,
        data: dict,
    ) -> None:
        """Background coroutine writing one alert_log to PG.

        Matches the lifecycle dual-write shape: catches
        all exceptions, increments the failure counter, never raises."""
        if self._pg is None:
            return
        try:
            await self._pg.write_alert_log_event(
                entry_id=entry_id,
                triggered_at=triggered_at,
                rule_id=rule_id,
                workflow_id=workflow_id,
                workspace_id=workspace_id,
                data=data,
            )
            _DUAL_WRITE_STATS["pg_ok"] += 1
        except Exception as exc:
            _DUAL_WRITE_STATS["pg_failed"] += 1
            logger.debug(
                "AlertStore: PG dual-write failed for rule=%s workflow=%s: %s",
                rule_id, workflow_id, exc,
            )

    def list_logs(self, limit: int = 100, workspace_id: str | None = None) -> list[dict]:
        if workspace_id is not None:
            sqlite_result = self._db.list_json(
                "alert_logs", "workspace_id = ?", (workspace_id,),
                order_by=f"triggered_at DESC LIMIT {limit}",
            )
        else:
            sqlite_result = self._db.list_json(
                "alert_logs", order_by=f"triggered_at DESC LIMIT {limit}"
            )
        self._fire_shadow_read_list(sqlite_result, limit=limit, workspace_id=workspace_id)
        return sqlite_result

    def list_logs_by_workflow(
        self, workflow_id: str, limit: int = 50, workspace_id: str | None = None
    ) -> list[dict]:
        if workspace_id is not None:
            sqlite_result = self._db.list_json(
                "alert_logs",
                "workflow_id = ? AND workspace_id = ?",
                (workflow_id, workspace_id),
                order_by=f"triggered_at DESC LIMIT {limit}",
            )
        else:
            sqlite_result = self._db.list_json(
                "alert_logs", "workflow_id = ?", (workflow_id,),
                order_by=f"triggered_at DESC LIMIT {limit}",
            )
        self._fire_shadow_read_by_workflow(
            sqlite_result, workflow_id=workflow_id,
            limit=limit, workspace_id=workspace_id,
        )
        return sqlite_result

    # ── Shadow-read plumbing (PR6) ────────────────────────────────────

    def _fire_shadow_read_list(
        self,
        sqlite_result: list[dict],
        *,
        limit: int,
        workspace_id: str | None,
    ) -> None:
        if not self._shadow_reads_active():
            if self._pg is not None:
                _SHADOW_READ_STATS["pg_skipped_disabled"] += 1
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _SHADOW_READ_STATS["pg_skipped_no_loop"] += 1
            return
        loop.create_task(
            self._pg_shadow_read_list(
                sqlite_result, limit=limit, workspace_id=workspace_id,
            ),
            name="alert-shadow-read-list",
        )

    def _fire_shadow_read_by_workflow(
        self,
        sqlite_result: list[dict],
        *,
        workflow_id: str,
        limit: int,
        workspace_id: str | None,
    ) -> None:
        if not self._shadow_reads_active():
            if self._pg is not None:
                _SHADOW_READ_STATS["pg_skipped_disabled"] += 1
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _SHADOW_READ_STATS["pg_skipped_no_loop"] += 1
            return
        loop.create_task(
            self._pg_shadow_read_by_workflow(
                sqlite_result, workflow_id=workflow_id,
                limit=limit, workspace_id=workspace_id,
            ),
            name=f"alert-shadow-read-wf-{workflow_id}",
        )

    async def _pg_shadow_read_list(
        self,
        sqlite_result: list[dict],
        *,
        limit: int,
        workspace_id: str | None,
    ) -> None:
        if self._pg is None:
            return
        try:
            pg_result = await self._pg.read_alert_logs(
                limit=limit, workspace_id=workspace_id,
            )
        except Exception as exc:
            _SHADOW_READ_STATS["pg_failed"] += 1
            logger.debug("AlertStore shadow read (list) failed: %s", exc)
            return

        sqlite_ids = {_alert_log_key(r) for r in sqlite_result}
        pg_ids = {_alert_log_key(r) for r in pg_result}

        if sqlite_ids == pg_ids:
            _SHADOW_READ_STATS["match"] += 1
            return

        _SHADOW_READ_STATS["mismatch"] += 1
        only_sqlite = sqlite_ids - pg_ids
        only_pg = pg_ids - sqlite_ids
        logger.warning(
            "AlertStore shadow read MISMATCH (list) "
            "workspace=%s limit=%d sqlite=%d pg=%d "
            "only_sqlite=%d only_pg=%d sample_only_sqlite=%s sample_only_pg=%s",
            workspace_id or "*", limit,
            len(sqlite_result), len(pg_result),
            len(only_sqlite), len(only_pg),
            sorted(only_sqlite)[:3], sorted(only_pg)[:3],
        )

    async def _pg_shadow_read_by_workflow(
        self,
        sqlite_result: list[dict],
        *,
        workflow_id: str,
        limit: int,
        workspace_id: str | None,
    ) -> None:
        if self._pg is None:
            return
        try:
            pg_result = await self._pg.read_alert_logs_by_workflow(
                workflow_id=workflow_id,
                limit=limit,
                workspace_id=workspace_id,
            )
        except Exception as exc:
            _SHADOW_READ_STATS["pg_failed"] += 1
            logger.debug(
                "AlertStore shadow read (by_workflow=%s) failed: %s",
                workflow_id, exc,
            )
            return

        sqlite_ids = {_alert_log_key(r) for r in sqlite_result}
        pg_ids = {_alert_log_key(r) for r in pg_result}

        if sqlite_ids == pg_ids:
            _SHADOW_READ_STATS["match"] += 1
            return

        _SHADOW_READ_STATS["mismatch"] += 1
        only_sqlite = sqlite_ids - pg_ids
        only_pg = pg_ids - sqlite_ids
        logger.warning(
            "AlertStore shadow read MISMATCH (by_workflow) "
            "workflow=%s workspace=%s limit=%d sqlite=%d pg=%d "
            "only_sqlite=%d only_pg=%d sample_only_sqlite=%s sample_only_pg=%s",
            workflow_id, workspace_id or "*", limit,
            len(sqlite_result), len(pg_result),
            len(only_sqlite), len(only_pg),
            sorted(only_sqlite)[:3], sorted(only_pg)[:3],
        )
