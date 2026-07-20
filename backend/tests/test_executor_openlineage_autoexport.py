"""L2.2 test (2026-06-08): executor auto-export OpenLineage on run completion.

Tests the _maybe_export_openlineage hook in isolation (no full executor
run needed — that path is exercised by the broader executor tests). The
hook reads FPULSE_LINEAGE_OPENLINEAGE_URL + app_state['lineage_store']
and calls the HTTP exporter. We verify it fires only when configured and
never raises.

Contracts pinned:
  * No env var set -> no export attempt
  * Env set but no lineage_store -> no-op, no raise
  * No run_id -> no-op
  * Env set + store present -> export_run called with the run_id
  * Exporter raising is swallowed (run cleanup never breaks)
"""
from __future__ import annotations

import sqlite3

import pytest

from fpulse.engine.executor import WorkflowExecutor
from fpulse.lineage import LineageStore


class _FakeDB:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params); self._conn.commit(); return cur

    def fetchone(self, sql, params=()):
        r = self._conn.execute(sql, params).fetchone(); return dict(r) if r else None

    def fetchall(self, sql, params=()):
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]


def _executor(lineage_store=None):
    app_state = {}
    if lineage_store is not None:
        app_state["lineage_store"] = lineage_store
    return WorkflowExecutor(data_dir=".", app_state=app_state)


class TestAutoExportHook:
    def test_no_env_var_no_export(self, monkeypatch):
        monkeypatch.delenv("FPULSE_LINEAGE_OPENLINEAGE_URL", raising=False)
        store = LineageStore(_FakeDB())
        store.record_step_run(workflow_id="wf", run_id="r1", step_id="s1",
                              started_at=1.0)
        calls = []
        import fpulse.lineage.openlineage as ol
        monkeypatch.setattr(ol.OpenLineageHTTPExporter, "export_run",
                            lambda self, rid, st: calls.append(rid) or {"posted": 0, "failed": 0})
        _executor(store)._maybe_export_openlineage("r1")
        assert calls == []  # no env → never constructed/called

    def test_env_set_but_no_store_is_noop(self, monkeypatch):
        monkeypatch.setenv("FPULSE_LINEAGE_OPENLINEAGE_URL", "https://marquez/x")
        # No lineage_store in app_state
        _executor(None)._maybe_export_openlineage("r1")  # must not raise

    def test_no_run_id_is_noop(self, monkeypatch):
        monkeypatch.setenv("FPULSE_LINEAGE_OPENLINEAGE_URL", "https://marquez/x")
        store = LineageStore(_FakeDB())
        calls = []
        import fpulse.lineage.openlineage as ol
        monkeypatch.setattr(ol.OpenLineageHTTPExporter, "export_run",
                            lambda self, rid, st: calls.append(rid) or {"posted": 0, "failed": 0})
        _executor(store)._maybe_export_openlineage("")
        assert calls == []

    def test_env_and_store_triggers_export(self, monkeypatch):
        monkeypatch.setenv("FPULSE_LINEAGE_OPENLINEAGE_URL", "https://marquez/api/v1/lineage")
        store = LineageStore(_FakeDB())
        store.record_step_run(workflow_id="wf", run_id="r1", step_id="s1",
                              columns_out=["id"], rows_out=5, started_at=1.0)
        seen = {}
        import fpulse.lineage.openlineage as ol

        def _fake_export(self, run_id, lineage_store):
            seen["run_id"] = run_id
            seen["url"] = self.url
            return {"posted": 1, "failed": 0}

        monkeypatch.setattr(ol.OpenLineageHTTPExporter, "export_run", _fake_export)
        _executor(store)._maybe_export_openlineage("r1")
        assert seen["run_id"] == "r1"
        assert seen["url"] == "https://marquez/api/v1/lineage"

    def test_exporter_raising_is_swallowed(self, monkeypatch):
        monkeypatch.setenv("FPULSE_LINEAGE_OPENLINEAGE_URL", "https://marquez/x")
        store = LineageStore(_FakeDB())
        store.record_step_run(workflow_id="wf", run_id="r1", step_id="s1",
                              started_at=1.0)
        import fpulse.lineage.openlineage as ol

        def _boom(self, run_id, lineage_store):
            raise RuntimeError("network exploded")

        monkeypatch.setattr(ol.OpenLineageHTTPExporter, "export_run", _boom)
        # Must NOT raise — run cleanup must never break over lineage export
        _executor(store)._maybe_export_openlineage("r1")

    def test_hook_is_called_from_run_cleanup(self):
        # Regression guard: the run-completion path must invoke the hook.
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / "fpulse" / "engine" / "executor.py").read_text(encoding="utf-8")
        assert "_maybe_export_openlineage(run_id)" in src, (
            "L2.2 regression — run cleanup must call _maybe_export_openlineage"
        )
