"""Regression: the v18 composite-index migration must skip tables that are
absent in this edition WITHOUT logging a scary warning.

`audit_log` is a Plus-only table — OSS writes audit events to the structured
logger, not a table (see fpulse/audit.py). Before this guard, a fresh OSS
install logged two alarming lines on every boot:

    F-Pulse schema v18: index idx_audit_user_time failed
        (no such table: main.audit_log) — underlying table may be missing a
        column from a prior migration path; continuing.

That warning is misleading (the table is intentionally absent, not corrupt) and
a bad first impression. The migration now checks table existence first and
skips absent-table indexes quietly, while still creating them where the table
exists (Plus) and still warning on GENUINE failures (table present, bad column).
"""
from __future__ import annotations

import logging
import sqlite3

from fpulse.storage.database import Database


def _oss_shaped_conn() -> sqlite3.Connection:
    """A connection shaped like a fresh OSS DB: the three time-series tables
    the migration indexes exist, but `audit_log` (Plus-only) does not."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE executions (workflow_id TEXT, workspace_id TEXT, started_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE alert_logs (workflow_id TEXT, workspace_id TEXT, triggered_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE lifecycle_events (workflow_id TEXT, workspace_id TEXT, timestamp TEXT)"
    )
    return conn


def _run_v18(conn: sqlite3.Connection) -> None:
    # The migration only touches `conn` + the module logger — build a bare
    # instance so we exercise the real method without the full init chain.
    db = Database.__new__(Database)
    db._migrate_v18_composite_indexes(conn)


def test_v18_skips_absent_audit_log_without_warning(caplog):
    conn = _oss_shaped_conn()
    with caplog.at_level(logging.DEBUG, logger="fpulse.storage.database"):
        _run_v18(conn)

    # No WARNING-or-worse about the absent table.
    scary = [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING
        and ("audit_log" in r.getMessage() or "no such table" in r.getMessage().lower())
    ]
    assert not scary, f"fresh-install migration logged scary warning(s): {scary}"

    idx = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    # Present-table indexes were created.
    assert "idx_executions_wf_started" in idx
    assert "idx_executions_ws_started" in idx
    assert "idx_alert_logs_wf_triggered" in idx
    assert "idx_lifecycle_wf_time" in idx
    # Absent-table (audit_log) indexes were skipped, not attempted-and-failed.
    assert "idx_audit_user_time" not in idx
    assert "idx_audit_action_time" not in idx


def test_v18_creates_audit_indexes_when_table_present(caplog):
    """When audit_log DOES exist (Plus), the indexes are still created."""
    conn = _oss_shaped_conn()
    conn.execute(
        "CREATE TABLE audit_log (user_id TEXT, action TEXT, timestamp TEXT)"
    )
    with caplog.at_level(logging.WARNING, logger="fpulse.storage.database"):
        _run_v18(conn)

    idx = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_audit_user_time" in idx
    assert "idx_audit_action_time" in idx
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_v18_is_rerun_safe(caplog):
    """Idempotent — running twice must not raise or warn."""
    conn = _oss_shaped_conn()
    _run_v18(conn)
    with caplog.at_level(logging.WARNING, logger="fpulse.storage.database"):
        _run_v18(conn)  # second run
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
