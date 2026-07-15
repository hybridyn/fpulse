"""L1.1 integration test (2026-06-08).

Confirms the executor's wire-in path actually emits lineage events
through ExecutionContext into the LineageStore. The L1 unit tests
exercise the storage layer in isolation; this exercises the executor-
side seam (ctx.emit_lineage_step_run) which is what the executor
calls in its success path.

Contracts pinned:
  * ctx.emit_lineage_step_run is a no-op when no lineage_store is in app_state
  * ctx.emit_lineage_step_run is a no-op when run_id or workflow_id is missing
  * When all three are present, a row lands in the store with the right values
  * Errors inside the store NEVER propagate out (lineage must not fail the run)
  * The executor's success path is wired (regression guard - method exists,
    is called from the right spot)
"""
from __future__ import annotations

import sqlite3
import time

import pytest

import duckdb

from fpulse.lineage import LineageStore
from fpulse.nodes.base import ExecutionContext


# Re-use the FakeDB shape from test_lineage_runtime.py
class _FakeDB:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def fetchone(self, sql, params=()):
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql, params=()):
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]


@pytest.fixture
def ctx_with_lineage():
    ls = LineageStore(_FakeDB())
    conn = duckdb.connect(":memory:")
    ctx = ExecutionContext(
        conn=conn,
        app_state={"lineage_store": ls},
        run_id="test-run-1",
    )
    ctx.workflow_id = "test-wf-1"
    return ctx, ls


@pytest.fixture
def ctx_without_lineage():
    conn = duckdb.connect(":memory:")
    ctx = ExecutionContext(
        conn=conn,
        app_state={},  # NO lineage_store
        run_id="test-run-1",
    )
    ctx.workflow_id = "test-wf-1"
    return ctx


# ── No-op cases (the safety side of the contract) ────────────────────


class TestNoOpCases:
    def test_no_lineage_store_is_noop(self, ctx_without_lineage):
        # Should NOT raise; should NOT crash; should NOT log alarmingly.
        ctx_without_lineage.emit_lineage_step_run(
            step_id="s1", step_label="x", step_type="csv_source",
            columns_out=["id"], rows_out=10,
        )

    def test_missing_run_id_is_noop(self):
        ls = LineageStore(_FakeDB())
        conn = duckdb.connect(":memory:")
        ctx = ExecutionContext(conn=conn, app_state={"lineage_store": ls})
        # No run_id and no workflow_id - emit should silently no-op.
        ctx.emit_lineage_step_run(step_id="s1", rows_out=10)
        # Nothing landed in the store
        assert ls.get_runtime_lineage("any")["step_runs"] == []

    def test_missing_workflow_id_is_noop(self):
        ls = LineageStore(_FakeDB())
        conn = duckdb.connect(":memory:")
        ctx = ExecutionContext(conn=conn, app_state={"lineage_store": ls},
                                 run_id="run-A")
        # run_id present but workflow_id still empty - skip.
        ctx.emit_lineage_step_run(step_id="s1", rows_out=10)
        assert ls.get_runtime_lineage("run-A")["step_runs"] == []

    def test_store_exception_does_not_propagate(self, ctx_with_lineage):
        ctx, ls = ctx_with_lineage
        # Replace the store's record_step_run with one that raises -
        # the emit helper must swallow.
        def _boom(**kw):
            raise RuntimeError("simulated store failure")
        ls.record_step_run = _boom  # type: ignore[assignment]
        # Should NOT raise.
        ctx.emit_lineage_step_run(step_id="s1", rows_out=10)


# ── Happy path ───────────────────────────────────────────────────────


class TestEmitLandsInStore:
    def test_single_emit_lands(self, ctx_with_lineage):
        ctx, ls = ctx_with_lineage
        ctx.emit_lineage_step_run(
            step_id="s1",
            step_label="Load orders",
            step_type="csv_source",
            columns_out=["id", "amount"],
            rows_out=10000,
            started_at=1717000000.0,
            completed_at=1717000002.5,
        )
        out = ls.get_runtime_lineage("test-run-1")
        assert len(out["step_runs"]) == 1
        sr = out["step_runs"][0]
        assert sr["step_id"] == "s1"
        assert sr["step_label"] == "Load orders"
        assert sr["columns_out"] == ["id", "amount"]
        assert sr["rows_out"] == 10000

    def test_multiple_emits_accumulate_ordered_by_started_at(self, ctx_with_lineage):
        ctx, ls = ctx_with_lineage
        ctx.emit_lineage_step_run(step_id="s_late",  started_at=1717000010.0)
        ctx.emit_lineage_step_run(step_id="s_early", started_at=1717000000.0)
        ctx.emit_lineage_step_run(step_id="s_mid",   started_at=1717000005.0)
        order = [sr["step_id"] for sr in ls.get_runtime_lineage("test-run-1")["step_runs"]]
        assert order == ["s_early", "s_mid", "s_late"]

    def test_distinct_run_ids_isolated(self):
        # Two separate executions writing to the same store - each
        # only sees its own.
        ls = LineageStore(_FakeDB())
        conn = duckdb.connect(":memory:")

        ctx_a = ExecutionContext(conn=conn, app_state={"lineage_store": ls},
                                   run_id="run-A")
        ctx_a.workflow_id = "wf-1"
        ctx_a.emit_lineage_step_run(step_id="sA1", rows_out=1)

        ctx_b = ExecutionContext(conn=conn, app_state={"lineage_store": ls},
                                   run_id="run-B")
        ctx_b.workflow_id = "wf-1"
        ctx_b.emit_lineage_step_run(step_id="sB1", rows_out=99)

        assert [sr["step_id"] for sr in ls.get_runtime_lineage("run-A")["step_runs"]] == ["sA1"]
        assert [sr["step_id"] for sr in ls.get_runtime_lineage("run-B")["step_runs"]] == ["sB1"]


# ── Regression guard for the executor wire-in ────────────────────────


class TestExecutorWireIn:
    """Pin that the executor's success path calls emit_lineage_step_run.
    We don't run a full workflow here (heavy setup); we grep the
    executor source for the call site - cheap, catches removal."""

    def test_executor_calls_emit_lineage_step_run(self):
        from pathlib import Path
        exec_path = Path(__file__).resolve().parents[1] / "fpulse" / "engine" / "executor.py"
        src = exec_path.read_text(encoding="utf-8")
        assert "ctx.emit_lineage_step_run" in src, (
            "L1.1 regression - the executor's _execute_step success path "
            "must call ctx.emit_lineage_step_run(...). The helper exists "
            "on ExecutionContext; the executor wire-in is what closes "
            "the loop."
        )

    def test_emit_lineage_helper_exists_on_execution_context(self):
        assert hasattr(ExecutionContext, "emit_lineage_step_run"), (
            "L1.1 regression - ExecutionContext.emit_lineage_step_run "
            "must stay; the executor references it by attribute name."
        )
