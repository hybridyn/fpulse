"""Pinned tests for L1 runtime lineage events (2026-06-08).

First milestone from docs/design/lineage-1.2.md. Storage layer +
API surface for per-run lineage facts (columns in/out, rows in/out,
timing) - distinct from the design-time lineage graph that
build_from_workflow() generates.

Contracts pinned here:
  * record_step_run round-trips losslessly (insert + read by run_id)
  * Multiple step-runs for one run_id come back in started_at order
  * Distinct run_ids are isolated (a run_id query returns ONLY its rows)
  * get_runs_for_workflow returns distinct run_ids, most-recent first
  * delete_workflow_lineage cleans up the runtime table TOO (not just
    nodes + edges)
  * API endpoints return JSON matching the storage shape
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from fpulse.lineage import LineageStore


# ── Thin sqlite-dict adapter mirroring fpulse.storage.database.Database ──

class _FakeDB:
    """Minimal sqlite-backed DB matching the dict-row interface
    LineageStore uses (execute / fetchone / fetchall returning dicts)."""

    def __init__(self):
        # check_same_thread=False because FastAPI TestClient spawns
        # the request on a worker thread, distinct from the test
        # thread that creates the connection.
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def fetchone(self, sql: str, params: tuple | list = ()) -> dict | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple | list = ()) -> list[dict]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]


@pytest.fixture
def store():
    return LineageStore(_FakeDB())


# ── record + read round-trip ────────────────────────────────────────


class TestRecordStepRun:
    def test_record_then_get_by_run_id(self, store):
        store.record_step_run(
            workflow_id="wf-1", run_id="run-A", step_id="s1",
            step_label="Load orders", step_type="csv_source",
            columns_in=[], columns_out=["id", "amount"],
            rows_in=0, rows_out=10000,
            started_at=1717000000.0, completed_at=1717000002.5,
        )
        out = store.get_runtime_lineage("run-A")
        assert out["run_id"] == "run-A"
        assert len(out["step_runs"]) == 1
        sr = out["step_runs"][0]
        assert sr["step_id"] == "s1"
        assert sr["columns_out"] == ["id", "amount"]
        assert sr["rows_out"] == 10000
        assert sr["completed_at"] == 1717000002.5

    def test_multiple_steps_in_started_at_order(self, store):
        store.record_step_run(workflow_id="wf-1", run_id="run-B",
                                step_id="s_late", started_at=1717000010.0)
        store.record_step_run(workflow_id="wf-1", run_id="run-B",
                                step_id="s_early", started_at=1717000000.0)
        store.record_step_run(workflow_id="wf-1", run_id="run-B",
                                step_id="s_mid", started_at=1717000005.0)
        out = store.get_runtime_lineage("run-B")
        order = [sr["step_id"] for sr in out["step_runs"]]
        assert order == ["s_early", "s_mid", "s_late"]

    def test_distinct_run_ids_are_isolated(self, store):
        store.record_step_run(workflow_id="wf-1", run_id="run-X",
                                step_id="s1", started_at=time.time())
        store.record_step_run(workflow_id="wf-1", run_id="run-Y",
                                step_id="s2", started_at=time.time())
        assert len(store.get_runtime_lineage("run-X")["step_runs"]) == 1
        assert store.get_runtime_lineage("run-X")["step_runs"][0]["step_id"] == "s1"

    def test_empty_run_returns_empty_list_not_error(self, store):
        out = store.get_runtime_lineage("no-such-run")
        assert out["run_id"] == "no-such-run"
        assert out["step_runs"] == []

    def test_error_field_carries_through(self, store):
        store.record_step_run(
            workflow_id="wf-1", run_id="run-fail",
            step_id="s_broken",
            started_at=1717000000.0, completed_at=1717000001.0,
            error="ConnectionError: db.example.com unreachable",
        )
        sr = store.get_runtime_lineage("run-fail")["step_runs"][0]
        assert "ConnectionError" in sr["error"]

    def test_columns_in_out_default_to_empty_lists(self, store):
        # Many node types don't know their column shape; the table
        # must not require a non-empty list.
        store.record_step_run(workflow_id="wf-1", run_id="run-noschema",
                                step_id="s1", started_at=1717000000.0)
        sr = store.get_runtime_lineage("run-noschema")["step_runs"][0]
        assert sr["columns_in"] == []
        assert sr["columns_out"] == []


# ── per-workflow run listing ─────────────────────────────────────────


class TestRunListing:
    def test_returns_distinct_runs_most_recent_first(self, store):
        # Three runs on workflow A, one on workflow B.
        store.record_step_run(workflow_id="wf-A", run_id="run-1",
                                step_id="s1", started_at=1.0)
        store.record_step_run(workflow_id="wf-A", run_id="run-2",
                                step_id="s1", started_at=2.0)
        store.record_step_run(workflow_id="wf-A", run_id="run-3",
                                step_id="s1", started_at=3.0)
        store.record_step_run(workflow_id="wf-B", run_id="run-99",
                                step_id="s1", started_at=2.5)
        runs = store.get_runs_for_workflow("wf-A")
        assert runs == ["run-3", "run-2", "run-1"]
        # wf-B must NOT bleed through
        assert "run-99" not in runs

    def test_limit_caps_returned_count(self, store):
        for i in range(20):
            store.record_step_run(workflow_id="wf-x", run_id=f"r{i}",
                                    step_id="s1", started_at=float(i))
        assert len(store.get_runs_for_workflow("wf-x", limit=5)) == 5

    def test_empty_workflow_returns_empty(self, store):
        assert store.get_runs_for_workflow("nonexistent") == []


# ── Cleanup ──────────────────────────────────────────────────────────


class TestDeleteWorkflowLineage:
    def test_delete_removes_runtime_rows_too(self, store):
        # Plant design-time + runtime lineage for wf-A and wf-B.
        store.record_step(workflow_id="wf-A", step_id="s1",
                           step_label="x", step_type="csv_source",
                           columns=["id"])
        store.record_step_run(workflow_id="wf-A", run_id="run-1",
                                step_id="s1", started_at=time.time())
        store.record_step(workflow_id="wf-B", step_id="s1",
                           step_label="x", step_type="csv_source",
                           columns=["id"])
        store.record_step_run(workflow_id="wf-B", run_id="run-1",
                                step_id="s1", started_at=time.time())
        # Delete wf-A
        store.delete_workflow_lineage("wf-A")
        # wf-A is fully gone
        assert store.get_runs_for_workflow("wf-A") == []
        assert store.get_runtime_lineage("run-1")["step_runs"] != [], \
            "wait - this run_id was used by both wf-A and wf-B; only wf-A rows should be gone"
        # Only the wf-B run_id remains
        assert [sr["workflow_id"] for sr in
                  store.get_runtime_lineage("run-1")["step_runs"]] == ["wf-B"]
        # wf-B still intact
        assert store.get_runs_for_workflow("wf-B") == ["run-1"]


# ── API surface ─────────────────────────────────────────────────────


class TestAPIEndpoints:
    def _make_client(self, tmp_path, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import fpulse.api.lineage as lineage_mod
        import fpulse.main as main_mod
        # Plant a real LineageStore against an in-memory sqlite.
        ls = LineageStore(_FakeDB())
        monkeypatch.setattr(main_mod, "app_state",
                            {"data_dir": str(tmp_path), "lineage_store": ls},
                            raising=False)
        # Bypass the feature flag (the API helper guards on
        # FPULSE_ENABLE_LINEAGE; for tests we want it always on).
        monkeypatch.setattr(lineage_mod, "_get_lineage_store",
                            lambda req: ls)
        app = FastAPI()
        from fpulse.auth.deps import current_workspace_id
        app.dependency_overrides[current_workspace_id] = lambda: "default"
        app.include_router(lineage_mod.router)
        return TestClient(app), ls

    def test_get_runtime_lineage_endpoint(self, tmp_path, monkeypatch):
        client, ls = self._make_client(tmp_path, monkeypatch)
        ls.record_step_run(workflow_id="wf-1", run_id="run-api",
                            step_id="s1", step_label="Load",
                            columns_out=["id"], rows_out=42,
                            started_at=time.time())
        body = client.get("/api/lineage/runs/run-api").json()
        assert body["run_id"] == "run-api"
        assert len(body["step_runs"]) == 1
        assert body["step_runs"][0]["rows_out"] == 42

    def test_get_runtime_lineage_empty_run_is_200_not_404(self, tmp_path, monkeypatch):
        client, _ = self._make_client(tmp_path, monkeypatch)
        r = client.get("/api/lineage/runs/no-such-run")
        assert r.status_code == 200
        assert r.json()["step_runs"] == []

    def test_list_runs_for_workflow_endpoint(self, tmp_path, monkeypatch):
        client, ls = self._make_client(tmp_path, monkeypatch)
        ls.record_step_run(workflow_id="wf-1", run_id="r-old",
                            step_id="s1", started_at=1.0)
        ls.record_step_run(workflow_id="wf-1", run_id="r-new",
                            step_id="s1", started_at=99.0)
        body = client.get("/api/lineage/workflow/wf-1/runs").json()
        assert body["workflow_id"] == "wf-1"
        assert body["runs"][0] == "r-new"  # most recent first
