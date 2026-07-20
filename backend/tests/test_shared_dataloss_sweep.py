"""Silent-data-loss fixes reconciled from the F-Pulse+ monorepo.

The monorepo and this OSS repo share no git history, so bug fixes to shared
code don't flow automatically. This locks in two silent-data-loss fixes ported
by the 2026-07-19 sweep:

  1. Managed-table MERGE dropped ALL existing rows the moment the incoming
     batch had a single NULL in a key column (SQL ``NOT IN`` returns NULL, never
     TRUE, for every existing row). Now a NULL-safe anti-join
     (``NOT EXISTS`` + ``IS NOT DISTINCT FROM``).
  2. Incremental ``db_source`` advanced its sync cursor at read-time. If a later
     sink failed, the next run skipped every row past the advanced cursor and
     they were never loaded. The watermark is now buffered on the run context
     and committed by the executor ONLY when the whole run succeeds.
"""
from __future__ import annotations

import os

import duckdb
import pytest

from fpulse.nodes.base import ExecutionContext


# ── 1. NULL-safe managed-table merge ────────────────────────────────────────

def test_merge_keeps_existing_rows_when_incoming_batch_has_null_key(tmp_path):
    from fpulse.nodes.local_table import LocalTableSinkNode

    table_dir = str(tmp_path)
    conn = duckdb.connect(":memory:")

    # Existing managed table: two rows keyed by id 1 and 2.
    existing_part = os.path.join(table_dir, "part-000.parquet").replace("\\", "/")
    conn.execute(
        "COPY (SELECT * FROM (VALUES (1, 'a'), (2, 'b')) t(id, val)) "
        f"TO '{existing_part}' (FORMAT PARQUET)"
    )

    # Incoming batch overwrites id=1 and carries a row with a NULL key.
    conn.execute(
        "CREATE OR REPLACE TEMP VIEW _lt_sink_input AS "
        "SELECT * FROM (VALUES (1, 'A'), (NULL, 'new')) t(id, val)"
    )

    ctx = ExecutionContext(conn=conn, data_dir=table_dir)
    LocalTableSinkNode({})._mode_merge(ctx, table_dir, merge_on=["id"])

    glob = os.path.join(table_dir, "part-*.parquet").replace("\\", "/")
    rows = conn.execute(
        f"SELECT id, val FROM read_parquet('{glob}')"
    ).fetchall()

    # Pre-fix: the untouched id=2 row was silently dropped, leaving only
    # [(1, 'A'), (None, 'new')]. Fixed: id=2 survives, id=1 is overwritten,
    # and the NULL-key row is appended.
    assert (2, "b") in rows, f"existing row id=2 was dropped by merge: {rows}"
    assert (1, "A") in rows
    assert (None, "new") in rows


# ── 2. Deferred incremental-cursor commit ───────────────────────────────────

class _FakeDb:
    """Records upserts so we can assert whether/when the cursor was persisted."""

    def __init__(self):
        self.upserts: list = []

    def execute(self, sql, params):
        if "INSERT OR REPLACE INTO sync_state" in sql:
            self.upserts.append(params)

    def commit(self):
        pass


@pytest.fixture
def wired_store(monkeypatch):
    from fpulse.engine import sync_state_store as sss

    fake = _FakeDb()
    monkeypatch.setattr(sss.sync_state_store, "_db", fake)
    return fake


def _source_ctx(values):
    conn = duckdb.connect(":memory:")
    rows = ", ".join(f"({v})" for v in values)
    conn.execute(f"CREATE TABLE __db_source AS SELECT * FROM (VALUES {rows}) t(updated_at)")
    ctx = ExecutionContext(conn=conn, data_dir=".")
    ctx.workflow_id = "wf1"
    return ctx


def test_cursor_buffers_at_read_time_and_is_not_persisted(wired_store):
    from fpulse.nodes.db_source import DbSourceNode

    ctx = _source_ctx([10, 20, 30])
    DbSourceNode({"_step_id": "s1"})._save_sync_cursor(ctx, "updated_at", rows_loaded=3)

    # Buffered on the run context, NOT written through to the store yet —
    # otherwise the cursor would advance before the sink is known to succeed.
    assert wired_store.upserts == [], "cursor persisted at read-time (data-loss window)"
    assert len(ctx.pending_sync_cursors) == 1
    assert ctx.pending_sync_cursors[0].last_cursor == "30"


def test_failed_run_does_not_advance_cursor(wired_store):
    from fpulse.nodes.db_source import DbSourceNode

    ctx = _source_ctx([10, 20])
    DbSourceNode({"_step_id": "s1"})._save_sync_cursor(ctx, "updated_at", rows_loaded=2)

    # Run FAILED -> the executor never calls _commit_sync_cursors, so the
    # buffer is simply discarded and the persisted cursor never moves.
    assert wired_store.upserts == []
    assert len(ctx.pending_sync_cursors) == 1


def test_successful_run_commits_buffered_cursor(wired_store):
    from fpulse.nodes.db_source import DbSourceNode
    from fpulse.engine.executor import WorkflowExecutor

    ctx = _source_ctx([10, 20, 30])
    DbSourceNode({"_step_id": "s1"})._save_sync_cursor(ctx, "updated_at", rows_loaded=3)

    WorkflowExecutor()._commit_sync_cursors(ctx)

    # Persisted exactly once, and the buffer is cleared so a re-commit is a no-op.
    assert len(wired_store.upserts) == 1
    assert ctx.pending_sync_cursors == []
    WorkflowExecutor()._commit_sync_cursors(ctx)
    assert len(wired_store.upserts) == 1


def test_direct_ctx_without_buffer_upserts_immediately(wired_store):
    """A unit path that builds a ctx without the pending_sync_cursors buffer
    keeps the historical immediate-upsert behaviour (back-compat)."""
    from fpulse.nodes.db_source import DbSourceNode

    ctx = _source_ctx([10])
    delattr(ctx, "pending_sync_cursors")  # simulate an old-style context

    DbSourceNode({"_step_id": "s1"})._save_sync_cursor(ctx, "updated_at", rows_loaded=1)
    assert len(wired_store.upserts) == 1
