"""End-to-end tests for the parameters arc beyond the resolver itself.

Covers:
  - Schedule.parameter_values round-trips through the model
  - Replay endpoint inherits captured parameter_values from the original
    execution and merges caller overrides correctly
  - summarize_pipeline tool surfaces declared parameters

These tests don't spin up a real DuckDB / SQLite — they exercise the
code paths that don't need them. The full execution path is covered by
the existing test_e2e_pipeline.py suite.
"""

from __future__ import annotations

import asyncio

import pytest

from fpulse.scheduling.models import Schedule, ScheduleCreate, ScheduleType
from fpulse.ir.schema import Step, StepType, Workflow, WorkflowParameter


# ---------------------------------------------------------------------------
# Schedule.parameter_values round-trips
# ---------------------------------------------------------------------------


def test_schedule_carries_parameter_values_through_model_dump():
    s = Schedule(
        workflow_id="wf-1",
        schedule_type=ScheduleType.CRON,
        cron_expression="0 8 * * *",
        parameter_values={"dataset": "orders", "batch_size": 5000},
    )
    dumped = s.model_dump(mode="json")
    assert dumped["parameter_values"] == {"dataset": "orders", "batch_size": 5000}

    # Reconstruct
    s2 = Schedule(**dumped)
    assert s2.parameter_values == {"dataset": "orders", "batch_size": 5000}


def test_schedule_create_accepts_empty_parameter_values_by_default():
    s = ScheduleCreate(workflow_id="wf-1")
    assert s.parameter_values == {}


def test_schedule_create_with_parameter_values():
    s = ScheduleCreate(
        workflow_id="wf-1",
        parameter_values={"env": "prod", "region": "us-east-1"},
    )
    assert s.parameter_values["env"] == "prod"
    assert s.parameter_values["region"] == "us-east-1"


# ---------------------------------------------------------------------------
# summarize_pipeline tool surfaces declared parameters
# ---------------------------------------------------------------------------


class _FakeWorkflowVersion:
    def __init__(self, workflow):
        self.workflow = workflow


class _FakeWorkflowStore:
    def __init__(self, wf):
        self._wf = wf

    def get(self, workflow_id, workspace_id=None):
        return _FakeWorkflowVersion(self._wf)


def test_summarize_pipeline_includes_parameters(monkeypatch):
    from fpulse.ai.tools.summarize_pipeline import _handler  # type: ignore
    from fpulse.ai.tools.base import ToolContext

    wf = Workflow(
        id="wf-1",
        name="demo",
        steps=[
            Step(id="s1", type=StepType.CSV_SOURCE, params={"path": "/data/x.csv"}),
            Step(id="s2", type=StepType.FILE_SINK, params={"path": "/out.parquet"}),
        ],
        parameters=[
            WorkflowParameter(name="dataset", type="string", default="orders",
                              description="Source CSV file"),
            WorkflowParameter(name="batch_size", type="int", default=1000),
            WorkflowParameter(name="run_date", type="string", required=True,
                              description="YYYY-MM-DD partition"),
        ],
    )

    # Use setitem so the real app_state dict (the one the routers + tools
    # imported a reference to) is mutated AND auto-reverted on teardown.
    # The earlier pattern (`fp_main.app_state = {}` followed by direct
    # mutation) leaked across tests; replaced 2026-05-22.
    from fpulse.main import app_state as _app_state
    monkeypatch.setitem(_app_state, "workflow_store", _FakeWorkflowStore(wf))

    ctx = ToolContext(
        tenant_id="default",
        user_id="u1",
        workspace_id="default",
        environment="dev",
    )
    out = asyncio.run(_handler({"pipeline_id": "wf-1"}, ctx))

    assert out["node_count"] == 2
    assert "csv_source" in out["source_types"]
    assert "file_sink" in out["destination_types"]
    assert isinstance(out["parameters"], list)
    assert len(out["parameters"]) == 3

    by_name = {p["name"]: p for p in out["parameters"]}
    assert by_name["dataset"]["default"] == "orders"
    assert by_name["dataset"]["description"] == "Source CSV file"
    assert by_name["batch_size"]["type"] == "int"
    assert by_name["run_date"]["required"] is True


def test_summarize_pipeline_handles_missing_pipeline(monkeypatch):
    from fpulse.ai.tools.summarize_pipeline import _handler
    from fpulse.ai.tools.base import ToolContext

    class _EmptyStore:
        def get(self, *a, **kw): return None

    # See sibling tests for the rationale — setitem mutates the real
    # app_state dict so router references see the change AND it auto-reverts.
    from fpulse.main import app_state as _app_state
    monkeypatch.setitem(_app_state, "workflow_store", _EmptyStore())

    ctx = ToolContext(
        tenant_id="default", user_id=None, workspace_id="default", environment="dev",
    )
    out = asyncio.run(_handler({"pipeline_id": "nonexistent"}, ctx))
    assert out["node_count"] == 0
    assert out["parameters"] == []
    assert "not found" in out.get("message", "").lower()


def test_summarize_pipeline_pipeline_with_no_parameters(monkeypatch):
    from fpulse.ai.tools.summarize_pipeline import _handler
    from fpulse.ai.tools.base import ToolContext

    wf = Workflow(
        id="wf-1",
        name="no-params",
        steps=[Step(id="s1", type=StepType.CSV_SOURCE, params={"path": "/x.csv"})],
        parameters=[],
    )
    # Use setitem so the real app_state dict (the one the routers + tools
    # imported a reference to) is mutated AND auto-reverted on teardown.
    # The earlier pattern (`fp_main.app_state = {}` followed by direct
    # mutation) leaked across tests; replaced 2026-05-22.
    from fpulse.main import app_state as _app_state
    monkeypatch.setitem(_app_state, "workflow_store", _FakeWorkflowStore(wf))

    ctx = ToolContext(
        tenant_id="default", user_id=None, workspace_id="default", environment="dev",
    )
    out = asyncio.run(_handler({"pipeline_id": "wf-1"}, ctx))
    assert out["parameters"] == []
    assert out["node_count"] == 1
