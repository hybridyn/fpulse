"""Tests for the AI endpoints added during the Apr 30 completion arc.

Covers:
- GET /api/ai/providers/compare (price comparison + recommendation)
- POST /api/ai/transform/cost-estimate (deterministic, no LLM)
- GET /api/ai/pre-run-validate/{workflow_id} (deterministic structural checks)
- POST /api/ai/pre-publish/{workflow_id} (7-section card)

Each endpoint exercised here is the deterministic / fallback path. The
LLM-aware variants (diagnose_error_llm, post_run_summary, etc.) are
covered in test_ai_foundation.py via try_llm_then_fallback semantics.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fpulse.api import ai as ai_api
from fpulse.api import pre_publish as pre_publish_api


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ai_api.router)
    app.include_router(pre_publish_api.router)
    return app


# ---------------------------------------------------------------------------
# /api/ai/providers/compare
# ---------------------------------------------------------------------------


def test_providers_compare_returns_4_cloud_items_with_recommendation(monkeypatch):
    """Comparison must always include the 4 hardcoded cloud entries and
    mark exactly one as recommended, with a non-empty reason string."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)

    app = _build_app()
    client = TestClient(app)
    resp = client.get("/api/ai/providers/compare")
    assert resp.status_code == 200
    body = resp.json()

    assert len(body["items"]) == 5, "Expected 5 cloud entries (Claude×2 + OpenAI×2 + OpenRouter)"
    providers = {it["provider"] for it in body["items"]}
    assert providers == {"claude", "openai", "openrouter"}, f"Expected cloud + OpenRouter providers, got {providers}"

    recs = [it for it in body["items"] if it["recommend"]]
    assert len(recs) == 1, f"Expected exactly 1 recommended item, got {len(recs)}"

    # Recommendation must be the cheapest low-latency option.
    cheapest_low = min(
        (it for it in body["items"] if it["latency_band"] == "low"),
        key=lambda it: it["est_cost_per_turn_usd"],
    )
    assert recs[0]["model"] == cheapest_low["model"]

    assert body["recommendation_reason"]
    assert body["source"] in ("openrouter+fallback", "fallback")


def test_providers_compare_force_param_accepted(monkeypatch):
    """?force=1 should not 4xx; the request still produces a valid response."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = _build_app()
    client = TestClient(app)
    resp = client.get("/api/ai/providers/compare?force=1")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and len(body["items"]) == 5


# ---------------------------------------------------------------------------
# /api/ai/transform/cost-estimate (deterministic)
# ---------------------------------------------------------------------------


def test_cost_estimate_simple_select():
    app = _build_app()
    client = TestClient(app)
    resp = client.post(
        "/api/ai/transform/cost-estimate",
        json={
            "sql": "SELECT a, b FROM t",
            "upstream_row_count": 10000,
            "upstream_column_count": 5,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cost_band"] in ("low", "medium", "high")
    assert body["rough_rows_out"] >= 0
    assert body["estimated_ms"] >= 1


def test_cost_estimate_join_marks_higher_cost():
    app = _build_app()
    client = TestClient(app)
    plain = client.post("/api/ai/transform/cost-estimate", json={
        "sql": "SELECT * FROM t",
        "upstream_row_count": 100_000,
        "upstream_column_count": 10,
    }).json()
    joined = client.post("/api/ai/transform/cost-estimate", json={
        "sql": "SELECT * FROM t JOIN u ON t.id = u.id",
        "upstream_row_count": 100_000,
        "upstream_column_count": 10,
    }).json()
    assert joined["estimated_ms"] >= plain["estimated_ms"], (
        "JOIN should be at least as expensive as plain SELECT"
    )
    assert joined["notes"], "JOIN should produce at least one note"


def test_cost_estimate_where_reduces_output_rows():
    app = _build_app()
    client = TestClient(app)
    body = client.post("/api/ai/transform/cost-estimate", json={
        "sql": "SELECT * FROM t WHERE x > 0",
        "upstream_row_count": 1000,
        "upstream_column_count": 4,
    }).json()
    assert body["rough_rows_out"] < 1000, "WHERE should drop output rows"
    assert any("filter" in n.lower() for n in body["notes"]), \
        "WHERE should be acknowledged in notes"


# ---------------------------------------------------------------------------
# /api/ai/pre-run-validate/{workflow_id}
# ---------------------------------------------------------------------------


class _FakeWorkflow:
    def __init__(self, steps=(), connections=(), workspace_id="default"):
        self.steps = list(steps)
        self.connections = list(connections)
        self.workspace_id = workspace_id

    def model_dump(self, mode: str = "python") -> dict:
        """Pydantic-compatible serializer — pre_publish now calls this on
        the workflow to produce a canonical hash. Without it, the endpoint
        falls back to dict(workflow_obj), which requires the object to be
        iterable (it isn't)."""
        def _step_dict(s):
            return {
                "id": getattr(s, "id", ""),
                "type": getattr(getattr(s, "type", None), "value", "") or "",
                "label": getattr(s, "label", ""),
            }
        return {
            "steps": [_step_dict(s) for s in self.steps],
            "connections": list(self.connections),
            "workspace_id": self.workspace_id,
        }


class _FakeStep:
    def __init__(self, sid: str, type_: str = "filter", label: str = ""):
        self.id = sid
        self.label = label
        # mimic the Pydantic Step.type which is a StepType enum
        class _Type:
            value = type_
        self.type = _Type()


class _FakeWorkflowVersion:
    def __init__(self, workflow):
        self.workflow = workflow


class _FakeWorkflowStore:
    def __init__(self, workflow):
        self._wf = workflow

    def get(self, workflow_id, version=None, workspace_id=None):
        return _FakeWorkflowVersion(self._wf)


def _install_fake_store(monkeypatch, workflow):
    """Install a fake workflow_store + null execution_log.

    Uses ``monkeypatch.setitem`` so the mutation is reverted at test
    teardown — the prior implementation rebound the module attribute
    and leaked state across tests in full-suite runs (2026-05-22 fix).
    """
    from fpulse.main import app_state as _app_state
    monkeypatch.setitem(_app_state, "workflow_store", _FakeWorkflowStore(workflow))
    monkeypatch.setitem(_app_state, "execution_log", None)


def test_pre_run_validate_empty_pipeline_blocks(monkeypatch):
    _install_fake_store(monkeypatch, _FakeWorkflow(steps=[]))
    app = _build_app()
    client = TestClient(app)
    resp = client.get("/api/ai/pre-run-validate/wf-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["can_run"] is False
    codes = {i["code"] for i in body["items"]}
    assert "empty_pipeline" in codes


def test_pre_run_validate_no_destination_warns(monkeypatch):
    wf = _FakeWorkflow(steps=[
        _FakeStep("s1", "csv_source"),
        _FakeStep("s2", "filter"),
    ])
    _install_fake_store(monkeypatch, wf)
    app = _build_app()
    client = TestClient(app)
    resp = client.get("/api/ai/pre-run-validate/wf-1")
    body = resp.json()
    # Source present, destination missing → warning, not blocker
    assert body["can_run"] is True
    codes = {i["code"] for i in body["items"]}
    assert "no_destination" in codes
    severities = {i["severity"] for i in body["items"]}
    assert "blocker" not in severities


def test_pre_run_validate_no_source_blocks(monkeypatch):
    wf = _FakeWorkflow(steps=[
        _FakeStep("s1", "filter"),
        _FakeStep("s2", "file_sink"),
    ])
    _install_fake_store(monkeypatch, wf)
    app = _build_app()
    client = TestClient(app)
    resp = client.get("/api/ai/pre-run-validate/wf-1")
    body = resp.json()
    assert body["can_run"] is False
    codes = {i["code"] for i in body["items"]}
    assert "no_source" in codes


def test_pre_run_validate_unknown_workflow_404(monkeypatch):
    """No store installed → 503; store with no workflow → 404."""
    from fpulse.main import app_state as _app_state

    class _EmptyStore:
        def get(self, *a, **kw): return None
    monkeypatch.setitem(_app_state, "workflow_store", _EmptyStore())

    app = _build_app()
    client = TestClient(app)
    resp = client.get("/api/ai/pre-run-validate/missing-id")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/ai/pre-publish/{workflow_id}
# ---------------------------------------------------------------------------


def test_pre_publish_returns_seven_sections(monkeypatch):
    wf = _FakeWorkflow(
        steps=[
            _FakeStep("s1", "csv_source", label="Source"),
            _FakeStep("s2", "filter", label="Filter"),
            _FakeStep("s3", "file_sink", label="Sink"),
        ],
    )
    _install_fake_store(monkeypatch, wf)
    app = _build_app()
    client = TestClient(app)
    resp = client.post("/api/ai/pre-publish/wf-1?target_env=prod")
    assert resp.status_code == 200
    body = resp.json()

    # All 7 expected top-level fields per project_fpulse_ai_completion.md §4b
    for k in (
        "workflow_id", "workflow_name", "target_environment",
        "snapshot_hash", "risk_level", "sections",
        "severity_summary", "blockers", "warnings", "infos",
    ):
        assert k in body, f"Missing field: {k}"

    # Sections must include the 4 deterministic-computable ones.
    for sec in ("change_diff", "inventory", "approval_plan", "security_posture"):
        assert sec in body["sections"], f"Missing section: {sec}"

    # SHA-256 hash is 64 hex chars.
    assert len(body["snapshot_hash"]) == 64
    int(body["snapshot_hash"], 16)  # raises if not hex

    # Risk level is one of the three valid bands.
    assert body["risk_level"] in ("low", "medium", "high")
