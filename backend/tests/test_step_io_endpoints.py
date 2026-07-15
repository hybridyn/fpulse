"""Step-IO API endpoints — coverage for the historical execution replay surface.

Exercises the four endpoints added in Phase 2 by driving them through their
real implementations (no FastAPI mock layer):
  - GET /api/execute/execution/{id}/step/{step_id}/output
  - GET /api/execute/execution/{id}/step/{step_id}/input
  - GET /api/execute/execution/{id}/edges
  - GET /api/execute/execution/{id}/step/{step_id}/output/export

Tests monkeypatch `fpulse.main.app_state` so endpoint helpers resolve to
test stores. Each endpoint is awaited directly with explicit args, which
bypasses FastAPI's Depends machinery — the auth/workspace contract is
exercised by ExecutionStore's own workspace_id filter.
"""

from __future__ import annotations

import csv as _csv
import io as _io
import json as _json

import pytest
from fastapi import HTTPException

from fpulse.engine.step_output_store import StepOutputStore
from fpulse.monitoring.store import ExecutionStore, ExecutionRecord


@pytest.fixture
def stores(_fpulse_test_db, monkeypatch):
    """Per-test stores wired into the real app_state via setitem.

    Order-of-import bug (2026-05-22 fix): the prior implementation called
    ``monkeypatch.setattr("fpulse.main.app_state", fake_state)`` which
    rebound the *module attribute* to a brand-new dict. But the API router
    modules (``fpulse.api.execution`` etc.) have already done
    ``from fpulse.main import app_state`` at their own import time — they
    hold a reference to the ORIGINAL dict. The rebind doesn't reach them,
    so ``get_step_output_store()`` resolved against the un-patched real
    dict and raised ``KeyError: 'step_output_store'`` in full-suite runs.
    The same test passed when run in isolation because the imports were
    cold and the patched module attribute was the one any later
    ``from fpulse.main import app_state`` would see.

    Fix: mutate the real dict via ``setitem`` so every holder of the
    reference sees the test stores. ``monkeypatch.setitem`` auto-reverts
    on teardown — no manual cleanup needed.
    """
    from fpulse.main import app_state  # bind to the real dict

    step_output_store = StepOutputStore(db=_fpulse_test_db)
    execution_store = ExecutionStore(db=_fpulse_test_db)

    monkeypatch.setitem(app_state, "step_output_store", step_output_store)
    monkeypatch.setitem(app_state, "execution_store", execution_store)
    monkeypatch.setitem(app_state, "store", None)
    monkeypatch.setitem(app_state, "data_dir", ".")

    yield {
        "step_output": step_output_store,
        "execution": execution_store,
    }


def _seed_execution(stores, *, execution_id="exec-1", workspace_id="default", connections=None):
    """Insert an ExecutionRecord whose workflow_snapshot carries the given DAG edges."""
    snapshot = {
        "id": "wf-1",
        "name": "Test Pipeline",
        "steps": [],
        "connections": connections or [],
    }
    stores["execution"].record(ExecutionRecord(
        id=execution_id,
        workflow_id="wf-1",
        workspace_id=workspace_id,
        workflow_snapshot=snapshot,
    ))


def _seed_step(stores, *, execution_id, step_id, **kw):
    stores["step_output"].record(
        execution_id=execution_id,
        step_id=step_id,
        **kw,
    )


class TestGetStepOutput:
    @pytest.mark.asyncio
    async def test_returns_captured_record(self, stores):
        from fpulse.api.execution import get_step_output

        _seed_execution(stores)
        _seed_step(
            stores, execution_id="exec-1", step_id="s1",
            step_index=0, step_type="csv_source", label="Load",
            row_count=5,
            sample_rows=[{"id": 1}, {"id": 2}],
            schema=[{"name": "id", "dtype": "INTEGER"}],
        )

        out = await get_step_output("exec-1", "s1", workspace_id="default")
        assert out["step_id"] == "s1"
        assert out["row_count"] == 5
        assert out["sample_rows"] == [{"id": 1}, {"id": 2}]
        assert out["label"] == "Load"

    @pytest.mark.asyncio
    async def test_unknown_execution_404(self, stores):
        from fpulse.api.execution import get_step_output

        with pytest.raises(HTTPException) as exc:
            await get_step_output("nope", "s1", workspace_id="default")
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_unknown_step_404(self, stores):
        from fpulse.api.execution import get_step_output

        _seed_execution(stores)
        with pytest.raises(HTTPException) as exc:
            await get_step_output("exec-1", "no-such-step", workspace_id="default")
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_workspace_scoping(self, stores):
        from fpulse.api.execution import get_step_output

        _seed_execution(stores, workspace_id="ws-a")
        _seed_step(stores, execution_id="exec-1", step_id="s1", row_count=1)

        # Same execution_id, wrong workspace → must look like 404 not data leak.
        with pytest.raises(HTTPException) as exc:
            await get_step_output("exec-1", "s1", workspace_id="ws-b")
        assert exc.value.status_code == 404


class TestGetStepInput:
    @pytest.mark.asyncio
    async def test_single_parent_returns_upstream(self, stores):
        from fpulse.api.execution import get_step_input

        _seed_execution(stores, connections=[
            {"from_step": "s1", "to_step": "s2"},
        ])
        _seed_step(
            stores, execution_id="exec-1", step_id="s1",
            label="Source", row_count=3,
            sample_rows=[{"id": 1}, {"id": 2}, {"id": 3}],
            schema=[{"name": "id", "dtype": "INTEGER"}],
        )

        out = await get_step_input("exec-1", "s2", workspace_id="default")
        assert len(out["inputs"]) == 1
        assert out["inputs"][0]["source_step_id"] == "s1"
        assert out["inputs"][0]["label"] == "Source"
        assert out["inputs"][0]["row_count"] == 3
        assert out["inputs"][0]["sample_rows"] == [{"id": 1}, {"id": 2}, {"id": 3}]
        assert out["inputs"][0]["missing"] is False

    @pytest.mark.asyncio
    async def test_multi_parent_join_returns_all_upstreams(self, stores):
        from fpulse.api.execution import get_step_input

        _seed_execution(stores, connections=[
            {"from_step": "left", "to_step": "join"},
            {"from_step": "right", "to_step": "join"},
        ])
        _seed_step(stores, execution_id="exec-1", step_id="left",
                   label="Left", row_count=10, sample_rows=[{"k": 1}])
        _seed_step(stores, execution_id="exec-1", step_id="right",
                   label="Right", row_count=20, sample_rows=[{"k": 2}])

        out = await get_step_input("exec-1", "join", workspace_id="default")
        ids = {inp["source_step_id"] for inp in out["inputs"]}
        assert ids == {"left", "right"}

    @pytest.mark.asyncio
    async def test_root_step_returns_empty_inputs(self, stores):
        from fpulse.api.execution import get_step_input

        _seed_execution(stores, connections=[
            {"from_step": "s1", "to_step": "s2"},
        ])
        # s1 is root — no upstreams point to it.
        out = await get_step_input("exec-1", "s1", workspace_id="default")
        assert out["inputs"] == []

    @pytest.mark.asyncio
    async def test_upstream_capture_missing_marks_missing(self, stores):
        from fpulse.api.execution import get_step_input

        _seed_execution(stores, connections=[
            {"from_step": "s1", "to_step": "s2"},
        ])
        # Connection exists but s1 was never captured (e.g. capture failed).
        out = await get_step_input("exec-1", "s2", workspace_id="default")
        assert len(out["inputs"]) == 1
        assert out["inputs"][0]["missing"] is True
        assert out["inputs"][0]["source_step_id"] == "s1"


class TestGetExecutionEdges:
    @pytest.mark.asyncio
    async def test_edges_carry_row_counts_from_source(self, stores):
        from fpulse.api.execution import get_execution_edges

        _seed_execution(stores, connections=[
            {"from_step": "s1", "to_step": "s2"},
            {"from_step": "s2", "to_step": "s3"},
        ])
        _seed_step(stores, execution_id="exec-1", step_id="s1", row_count=100)
        _seed_step(stores, execution_id="exec-1", step_id="s2", row_count=42)
        _seed_step(stores, execution_id="exec-1", step_id="s3", row_count=42)

        out = await get_execution_edges("exec-1", workspace_id="default")
        edges = {(e["from_step"], e["to_step"]): e for e in out["edges"]}
        assert edges[("s1", "s2")]["row_count"] == 100
        assert edges[("s2", "s3")]["row_count"] == 42

    @pytest.mark.asyncio
    async def test_failed_source_step_reflects_status_on_edge(self, stores):
        from fpulse.api.execution import get_execution_edges

        _seed_execution(stores, connections=[
            {"from_step": "s1", "to_step": "s2"},
        ])
        _seed_step(stores, execution_id="exec-1", step_id="s1",
                   status="error", row_count=0)

        out = await get_execution_edges("exec-1", workspace_id="default")
        assert out["edges"][0]["from_status"] == "error"
        assert out["edges"][0]["row_count"] == 0

    @pytest.mark.asyncio
    async def test_no_connections_returns_empty_edges(self, stores):
        from fpulse.api.execution import get_execution_edges

        _seed_execution(stores, connections=[])
        out = await get_execution_edges("exec-1", workspace_id="default")
        assert out["edges"] == []


class TestExport:
    @pytest.mark.asyncio
    async def test_csv_export_has_header_and_rows(self, stores):
        from fpulse.api.execution import export_step_output

        _seed_execution(stores)
        _seed_step(
            stores, execution_id="exec-1", step_id="s1", label="Source",
            row_count=2,
            sample_rows=[{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}],
            schema=[
                {"name": "id", "dtype": "INTEGER"},
                {"name": "name", "dtype": "VARCHAR"},
            ],
        )

        resp = await export_step_output("exec-1", "s1", workspace_id="default", fmt="csv")
        assert resp.media_type == "text/csv"
        assert "attachment" in resp.headers["content-disposition"].lower()
        # Body is a streamable bytes/str payload
        body = resp.body.decode("utf-8") if isinstance(resp.body, (bytes, bytearray)) else resp.body
        reader = _csv.DictReader(_io.StringIO(body))
        rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["name"] == "Alice"
        assert rows[1]["id"] == "2"

    @pytest.mark.asyncio
    async def test_json_export_returns_array(self, stores):
        from fpulse.api.execution import export_step_output

        _seed_execution(stores)
        _seed_step(
            stores, execution_id="exec-1", step_id="s1", label="Source",
            row_count=2,
            sample_rows=[{"id": 1}, {"id": 2}],
        )

        resp = await export_step_output("exec-1", "s1", workspace_id="default", fmt="json")
        assert resp.media_type == "application/json"
        body = resp.body.decode("utf-8") if isinstance(resp.body, (bytes, bytearray)) else resp.body
        parsed = _json.loads(body)
        assert parsed == [{"id": 1}, {"id": 2}]

    @pytest.mark.asyncio
    async def test_unknown_format_400(self, stores):
        from fpulse.api.execution import export_step_output

        _seed_execution(stores)
        _seed_step(stores, execution_id="exec-1", step_id="s1", row_count=1, sample_rows=[{"x": 1}])

        with pytest.raises(HTTPException) as exc:
            await export_step_output("exec-1", "s1", workspace_id="default", fmt="xlsx")
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_filename_sanitized(self, stores):
        from fpulse.api.execution import export_step_output

        _seed_execution(stores, execution_id="exec-1abcdef")
        _seed_step(
            stores, execution_id="exec-1abcdef", step_id="s1",
            label="Risky / path \\ name with spaces",
            row_count=1, sample_rows=[{"x": 1}],
        )

        resp = await export_step_output("exec-1abcdef", "s1", workspace_id="default", fmt="json")
        disp = resp.headers["content-disposition"]
        # No slashes/backslashes in filename; spaces normalized.
        filename_part = disp.split("filename=")[1]
        assert "/" not in filename_part
        assert "\\" not in filename_part
        assert " " not in filename_part.strip('"')
