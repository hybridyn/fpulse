"""Ephemeral-run endpoint coverage — `POST /api/execute/workflow/ephemeral`.

The endpoint lets the canvas Run / Sample buttons execute an unsaved
workflow IR without first persisting a row to the Pipelines list. The
contract:
  - Malformed IR → 400, code=invalid_workflow_ir
  - Zero-step IR → 400, code=validation_failed (the canvas gate)
  - safety_mode=validate_only → 200, status=validated, no execution
  - safety_mode=dry_run + unknown connection_id → 200 with
    connection_issues populated, still no execution

These four cases cover the validation gate that the user-facing toast
relies on. Full happy-path execution is exercised by the existing
``test_executor`` suite (the ephemeral endpoint delegates to the same
``WorkflowExecutor``).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException


@pytest.fixture
def _conn_store_stub(monkeypatch):
    """Stub the connection store with a get() that returns None for any id.

    Used by dry_run to detect missing connections. Returning None for every
    lookup exercises the "connection not found" branch deterministically.
    """
    class _Stub:
        def get(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(
        "fpulse.connections.store.get_store",
        lambda: _Stub(),
    )
    return _Stub()


def _make_body(**overrides):
    """Build a minimal valid EphemeralRunBody payload."""
    from fpulse.api.execution import EphemeralRunBody

    default = {
        "workflow": {
            "id": "wf_eph_test",
            "name": "Ephemeral Test",
            "steps": [
                {
                    "id": "src1",
                    "type": "api_source",
                    "label": "Smoke",
                    "params": {"url": "https://jsonplaceholder.typicode.com/posts"},
                    "position": {"x": 100, "y": 100},
                    "risk": "low",
                },
            ],
            "connections": [],
            "parameters": [],
        },
        "preview_limit": 10,
        "full_run": False,
        "safety_mode": "validate_only",
        "environment": "dev",
        "parameter_values": {},
    }
    default.update(overrides)
    return EphemeralRunBody(**default)


class TestEphemeralRunValidation:
    """The validation gate that 'X steps valid' badge relies on."""

    @pytest.mark.asyncio
    async def test_malformed_ir_returns_400_invalid_workflow_ir(self):
        """A workflow dict that can't deserialise as Workflow → 400 with a
        descriptive code so the frontend can show a useful toast."""
        from fpulse.api.execution import run_workflow_ephemeral, EphemeralRunBody

        body = EphemeralRunBody(
            workflow={"id": "x", "steps": "this should be a list"},
            safety_mode="validate_only",
        )

        with pytest.raises(HTTPException) as exc_info:
            await run_workflow_ephemeral(body, workspace_id="default")

        assert exc_info.value.status_code == 400
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail.get("code") == "invalid_workflow_ir"

    @pytest.mark.asyncio
    async def test_zero_step_ir_returns_400_validation_failed(self):
        """An IR with no steps fails the canvas-side ‘X steps valid’ rule.
        The endpoint must reject it before any execution attempt."""
        from fpulse.api.execution import run_workflow_ephemeral

        body = _make_body(workflow={
            "id": "wf_empty",
            "name": "Empty",
            "steps": [],
            "connections": [],
            "parameters": [],
        })

        with pytest.raises(HTTPException) as exc_info:
            await run_workflow_ephemeral(body, workspace_id="default")

        assert exc_info.value.status_code == 400
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail.get("code") == "validation_failed"
        assert any(
            "no steps" in e.get("message", "").lower()
            for e in detail.get("errors", [])
        )

    @pytest.mark.asyncio
    async def test_validate_only_short_circuits_with_no_execution(self):
        """safety_mode=validate_only must return status=validated without
        executing anything. ExecutionRecord is NOT created in this branch —
        the test relies on that by not wiring an execution store."""
        from fpulse.api.execution import run_workflow_ephemeral

        body = _make_body(safety_mode="validate_only")
        result = await run_workflow_ephemeral(body, workspace_id="default")

        assert result["status"] == "validated"
        assert result["safety_mode"] == "validate_only"
        assert result["ephemeral"] is True
        assert result["step_results"] == {}
        assert result["validator"]["errors"] == []

    @pytest.mark.asyncio
    async def test_dry_run_reports_missing_connections(self, _conn_store_stub):
        """safety_mode=dry_run must look up each unique connection_id in
        the caller's workspace and surface any missing ones. Stub store
        returns None for every id, so the test deterministically gets one
        connection_issue entry per unique referenced connection_id."""
        from fpulse.api.execution import run_workflow_ephemeral

        body = _make_body(
            workflow={
                "id": "wf_dry",
                "name": "Dry Run Missing Conn",
                "steps": [
                    {
                        "id": "src1",
                        "type": "api_source",
                        "label": "Source",
                        "params": {"url": "https://jsonplaceholder.typicode.com/posts"},
                        "position": {"x": 100, "y": 100},
                        "risk": "low",
                    },
                    {
                        "id": "snk1",
                        "type": "warehouse_sink",
                        "label": "Sink",
                        "params": {
                            "connection_id": "does_not_exist",
                            "table": "t",
                            "mode": "create",
                            "_input_step_ids": ["src1"],
                        },
                        "position": {"x": 400, "y": 100},
                        "risk": "low",
                    },
                ],
                "connections": [
                    {"from_step": "src1", "to_step": "snk1",
                     "from_port": "output", "to_port": "input"},
                ],
                "parameters": [],
            },
            safety_mode="dry_run",
        )
        result = await run_workflow_ephemeral(body, workspace_id="default")

        assert result["safety_mode"] == "dry_run"
        assert result["ephemeral"] is True
        assert result["status"] == "invalid"
        issues = result["connection_issues"]
        assert len(issues) == 1
        assert issues[0]["connection_id"] == "does_not_exist"
        assert issues[0]["step_id"] == "snk1"
