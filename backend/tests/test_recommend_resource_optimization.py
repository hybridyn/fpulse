"""Tests for the recommend_resource_optimization Copilot/MCP tool."""
from __future__ import annotations

import asyncio

from fpulse.monitoring.store import ExecutionRecord, StepLog
from fpulse.ai.tools.base import ToolContext, ToolTier
from fpulse.ai.tools.recommend_resource_optimization import _handler, DEFINITION


class _FakeStore:
    def __init__(self, rec, rows=None):
        self._rec = rec
        self._rows = rows or []

    def get(self, execution_id, workspace_id=None):
        return self._rec

    def list_by_workflow(self, workflow_id, limit=50, workspace_id=None):
        return self._rows


def _ctx() -> ToolContext:
    return ToolContext(tenant_id="t", user_id="u", workspace_id="default", environment="dev")


def _run(inputs):
    return asyncio.run(_handler(inputs, _ctx()))


def test_definition_is_read_tier_no_idempotency():
    assert DEFINITION.tier == ToolTier.READ
    assert DEFINITION.requires_idempotency_key is False
    assert DEFINITION.name == "recommend_resource_optimization"


def test_flags_bottleneck_memory_empty_and_cpu():
    import fpulse.main as fmain
    rec = ExecutionRecord(
        workflow_id="wf1", workflow_name="WF", status="success",
        duration_ms=10000, metadata={"peak_memory_mb": 500.0, "cpu_seconds": 9.5},
        step_logs=[
            StepLog(step_id="s1", step_name="extract", status="success", duration_ms=8000, rows_processed=5000),
            StepLog(step_id="s2", step_name="filter", status="success", duration_ms=200, rows_processed=0),
        ],
    )
    fmain.app_state["execution_store"] = _FakeStore(rec)
    out = _run({"execution_id": "e1"})
    assert out["analyzed"] is True
    areas = {r["area"] for r in out["recommendations"]}
    assert "bottleneck" in areas    # extract = 80% of the run
    assert "memory" in areas        # 500 MB near the ~512 MB default
    assert "empty_output" in areas  # filter produced 0 rows
    assert "cpu" in areas           # cpu 9.5s ~= wall 10s


def test_healthy_run_reports_no_issue():
    import fpulse.main as fmain
    rec = ExecutionRecord(
        workflow_id="wf2", workflow_name="OK", status="success",
        duration_ms=1000, metadata={"peak_memory_mb": 50.0, "cpu_seconds": 0.5},
        step_logs=[
            StepLog(step_id="a", step_name="a", status="success", duration_ms=300, rows_processed=100),
            StepLog(step_id="b", step_name="b", status="success", duration_ms=250, rows_processed=90),
        ],
    )
    fmain.app_state["execution_store"] = _FakeStore(rec)
    out = _run({"execution_id": "e2"})
    assert out["analyzed"] is True
    assert {r["area"] for r in out["recommendations"]} == {"none"}


def test_missing_inputs_returns_blank():
    out = _run({})
    assert out["analyzed"] is False
    assert out["recommendations"] == []


def test_no_run_found_returns_blank():
    import fpulse.main as fmain

    class _Empty:
        def get(self, *a, **k):
            return None

        def list_by_workflow(self, *a, **k):
            return []

    fmain.app_state["execution_store"] = _Empty()
    out = _run({"pipeline_id": "wf-none"})
    assert out["analyzed"] is False
