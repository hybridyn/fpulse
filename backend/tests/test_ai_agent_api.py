"""
Tests for the POST /api/ai/agent endpoint.

Uses FastAPI TestClient + app_state injection so the endpoint runs against
a FakeLLMClient — no Anthropic calls, no httpx.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fpulse.ai.agent import (
    AgentRunner,
    LLMResponse,
    LLMToolUse,
)
from fpulse.ai.tools import ToolRegistry, register_initial_tools
from fpulse.api import agent as agent_api


# ---------------------------------------------------------------------------
# Fake LLM (mirrors test_ai_agent.py's helper)
# ---------------------------------------------------------------------------


@dataclass
class FakeLLMClient:
    script: list[LLMResponse]
    calls: list[dict] = field(default_factory=list)

    async def call(self, *, system, messages, tools, **extras):
        # **extras tolerates kwargs the streaming path forwards through
        # semantic_cache.cached_call (notably on_token). Without it the
        # stream test surfaces as outcome=llm_failure because TypeError
        # gets caught by the runner's llm-failure handler.
        self.calls.append({"messages": messages, "tools": tools, "extras": list(extras)})
        if not self.script:
            raise AssertionError("FakeLLMClient script exhausted")
        return self.script.pop(0)


# ---------------------------------------------------------------------------
# App + app_state fixtures
# ---------------------------------------------------------------------------


def _build_app(monkeypatch, fake_llm: FakeLLMClient) -> FastAPI:
    """Build a FastAPI app with the agent router + test-only app_state.

    Uses ``monkeypatch.setitem`` for app_state wiring so the entries are
    reverted at test teardown. The earlier implementation mutated the
    real dict without cleanup, leaking ``agent_runner_factory`` and
    ``workspace_store`` across tests in full-suite runs (2026-05-22 fix).
    """
    app = FastAPI()
    app.include_router(agent_api.router)

    # Wire app_state via setitem against the real dict (already exists
    # at fpulse.main module load; the prior `hasattr` guard was dead code).
    from fpulse.main import app_state as _app_state

    test_registry = ToolRegistry()
    register_initial_tools(test_registry)

    def _factory(*, user_id, workspace_id):
        return AgentRunner(registry=test_registry, llm_client=fake_llm)

    monkeypatch.setitem(_app_state, "agent_runner_factory", _factory)
    # Provide a workspace_store stub so current_workspace_id resolves to "default"
    monkeypatch.setitem(_app_state, "workspace_store", None)
    return app


def _set_provider(monkeypatch, present: bool):
    if present:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    # Disable the local-Ollama autoprobe so dev boxes running ollama
    # don't accidentally flip has_provider=True in the present=False path.
    monkeypatch.setenv("FPULSE_DISABLE_OLLAMA_AUTOPROBE", "1")


def _disable_fast_lane(monkeypatch):
    """Bypass the fast-lane pre-flight added May 4 2026 so the agent loop
    actually runs in tests that assert tool-call traces. Without this,
    'Summarize pipeline p-1' is short-circuited to the overview KPI
    card instead of dispatching to the LLM + summarize_pipeline tool."""
    async def _no_match(*_args, **_kwargs):
        return None
    import fpulse.ai.fast_router as fast_router_mod
    monkeypatch.setattr(fast_router_mod, "run_fast_lane", _no_match)


def _stub_developer_user(monkeypatch):
    """Inject a fake developer user so tests that need SAFE_WRITE access
    aren't capped at READ by the default unauthenticated role of
    'viewer'. Required by tests that exercise compose_report or any
    safe-write tool — the request body's allow_safe_writes=True only
    OPTS IN; it can't widen past the role's ceiling."""
    class _FakeUser:
        id = "test-developer"
        role = "developer"
        email = "dev@test.local"
    import fpulse.api.agent as agent_mod
    monkeypatch.setattr(agent_mod, "current_user_optional", lambda _request: _FakeUser())


def _request_body() -> dict:
    return {
        "user_intent": "Summarize pipeline p-1",
        "page_context": {
            "page": "pipelines.list",
            "visible_ids": ["p-1", "p-2"],
            "selected_ids": ["p-1"],
            "filters": {},
            "environment": "dev",
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_agent_returns_no_provider_response_when_unconfigured(monkeypatch):
    _set_provider(monkeypatch, present=False)
    _disable_fast_lane(monkeypatch)
    app = _build_app(monkeypatch, FakeLLMClient(script=[]))
    client = TestClient(app)
    resp = client.post("/api/ai/agent", json=_request_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["no_provider"] is True
    assert body["run_id"] == "no-provider"
    assert "AI is not configured" in body["final_text"]
    assert body["cost"]["estimated_usd"] == 0.0
    assert body["cost"]["provider"] == "none"


def test_agent_runs_tool_loop_when_provider_configured(monkeypatch):
    _set_provider(monkeypatch, present=True)
    _disable_fast_lane(monkeypatch)
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(id="tu-1", name="summarize_pipeline", input={"pipeline_id": "p-1"})],
            stop_reason="tool_use",
            tokens_in=100,
            tokens_out=20,
        ),
        LLMResponse(
            text="Pipeline p-1: 12 nodes, 2 sources, 1 destination, alerts on, last run success.",
            tool_uses=[],
            stop_reason="end_turn",
            tokens_in=140,
            tokens_out=40,
        ),
    ])
    app = _build_app(monkeypatch, fake)
    client = TestClient(app)
    resp = client.post("/api/ai/agent", json=_request_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["no_provider"] is False
    assert body["outcome"] == "success"
    assert "12 nodes" in body["final_text"]
    assert len(body["steps"]) == 1
    step = body["steps"][0]
    assert step["tool_name"] == "summarize_pipeline"
    assert step["tool_tier"] == "read"
    assert step["outcome"] == "success"
    assert step["input_hash"]
    assert step["output_hash"]
    # Cost surfacing
    assert body["cost"]["tokens_in"] == 240
    assert body["cost"]["tokens_out"] == 60
    assert body["cost"]["estimated_usd"] > 0
    assert body["cost"]["provider"] == "claude"


def test_agent_blocks_safe_write_by_default(monkeypatch):
    """Default tier (READ) must block compose_report attempts."""
    _set_provider(monkeypatch, present=True)
    _disable_fast_lane(monkeypatch)
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(
                id="tu-1",
                name="compose_report",
                input={"template": "monthly", "idempotency_key": "k"},
            )],
            stop_reason="tool_use",
        ),
        LLMResponse(text="Tool not allowed.", tool_uses=[], stop_reason="end_turn"),
    ])
    app = _build_app(monkeypatch, fake)
    client = TestClient(app)
    body = _request_body()
    # allow_safe_writes defaults to False
    resp = client.post("/api/ai/agent", json=body)
    assert resp.status_code == 200
    step = resp.json()["steps"][0]
    assert step["outcome"] == "policy_block"
    assert "tool_not_in_allowed_tiers" in step["policy_rules_fired"]


def test_agent_allows_safe_write_when_opted_in(monkeypatch):
    _set_provider(monkeypatch, present=True)
    _disable_fast_lane(monkeypatch)
    _stub_developer_user(monkeypatch)
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(
                id="tu-1",
                name="compose_report",
                input={
                    "template": "monthly",
                    "idempotency_key": "free.u-1.compose.r-1.v1",
                },
            )],
            stop_reason="tool_use",
        ),
        LLMResponse(text="Draft created.", tool_uses=[], stop_reason="end_turn"),
    ])
    app = _build_app(monkeypatch, fake)
    client = TestClient(app)
    body = _request_body()
    body["allow_safe_writes"] = True
    resp = client.post("/api/ai/agent", json=body)
    assert resp.status_code == 200
    step = resp.json()["steps"][0]
    assert step["outcome"] == "success"
    assert step["tool_name"] == "compose_report"
    assert step["tool_tier"] == "safe_write"


def test_agent_status_reports_provider_and_tool_count(monkeypatch):
    _set_provider(monkeypatch, present=True)
    app = _build_app(monkeypatch, FakeLLMClient(script=[]))
    client = TestClient(app)
    resp = client.get("/api/ai/agent/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["provider"] == "claude"
    assert body["tool_count"] >= 4


def test_agent_status_reports_unavailable_when_no_provider(monkeypatch):
    _set_provider(monkeypatch, present=False)
    _disable_fast_lane(monkeypatch)
    app = _build_app(monkeypatch, FakeLLMClient(script=[]))
    client = TestClient(app)
    resp = client.get("/api/ai/agent/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["provider"] == "none"


# ---------------------------------------------------------------------------
# Copilot suggestions endpoint
# ---------------------------------------------------------------------------


def test_suggestions_returns_dashboard_prompts(monkeypatch):
    _set_provider(monkeypatch, present=False)
    app = _build_app(monkeypatch, FakeLLMClient(script=[]))
    client = TestClient(app)
    resp = client.get("/api/ai/agent/suggestions?page=dashboard")
    assert resp.status_code == 200
    body = resp.json()
    items = body["suggestions"]
    assert len(items) >= 3
    labels = " ".join(s["label"].lower() for s in items)
    # Dashboard bucket should mention health / failed / slowest / anomalies
    assert "health" in labels or "failed" in labels or "anomal" in labels


def test_suggestions_per_page_buckets(monkeypatch):
    _set_provider(monkeypatch, present=False)
    app = _build_app(monkeypatch, FakeLLMClient(script=[]))
    client = TestClient(app)

    pages = ["pipelines.list", "executions", "editor.canvas", "connections", "alerts"]
    for page in pages:
        resp = client.get(f"/api/ai/agent/suggestions?page={page}")
        assert resp.status_code == 200, page
        body = resp.json()
        assert len(body["suggestions"]) >= 2, f"Empty suggestions for {page}"


def test_suggestions_unknown_page_returns_default(monkeypatch):
    _set_provider(monkeypatch, present=False)
    app = _build_app(monkeypatch, FakeLLMClient(script=[]))
    client = TestClient(app)
    resp = client.get("/api/ai/agent/suggestions?page=does-not-exist")
    assert resp.status_code == 200
    items = resp.json()["suggestions"]
    assert len(items) >= 3, "Default bucket should still ship at least 3 suggestions"


def test_suggestions_selected_count_prepends_selection_prompt(monkeypatch):
    _set_provider(monkeypatch, present=False)
    app = _build_app(monkeypatch, FakeLLMClient(script=[]))
    client = TestClient(app)
    resp = client.get("/api/ai/agent/suggestions?page=pipelines.list&selected_count=3")
    items = resp.json()["suggestions"]
    # First item is the selection-aware one
    assert "3 selected" in items[0]["label"] or "selected items" in items[0]["label"].lower()


def test_suggestions_categories_are_valid(monkeypatch):
    _set_provider(monkeypatch, present=False)
    app = _build_app(monkeypatch, FakeLLMClient(script=[]))
    client = TestClient(app)
    resp = client.get("/api/ai/agent/suggestions?page=executions")
    items = resp.json()["suggestions"]
    valid = {"general", "diagnose", "build", "monitor"}
    for it in items:
        assert it["category"] in valid, f"Invalid category: {it['category']}"


def _parse_sse_frames(raw: str) -> list[dict]:
    """Parse an SSE response body into a list of decoded data payloads."""
    import json as _json
    frames: list[dict] = []
    for line in raw.splitlines():
        if line.startswith("data: "):
            try:
                frames.append(_json.loads(line[len("data: "):]))
            except Exception:
                pass
    return frames


def test_agent_stream_emits_run_start_step_and_final(monkeypatch):
    _set_provider(monkeypatch, present=True)
    _disable_fast_lane(monkeypatch)
    # Stream path may call the LLM one extra time vs the non-stream path
    # (e.g. for the post-iteration narration the stream emits). Pad the
    # script with a tail end_turn so script-exhausted doesn't surface as
    # llm_failure if the loop polls once more than expected.
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(id="tu-1", name="summarize_pipeline", input={"pipeline_id": "p-1"})],
            stop_reason="tool_use",
            tokens_in=10,
            tokens_out=2,
        ),
        LLMResponse(text="Done.", tool_uses=[], stop_reason="end_turn", tokens_in=5, tokens_out=3),
        LLMResponse(text="Done.", tool_uses=[], stop_reason="end_turn", tokens_in=0, tokens_out=0),
    ])
    app = _build_app(monkeypatch, fake)
    client = TestClient(app)
    with client.stream("POST", "/api/ai/agent/stream", json=_request_body()) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(chunk for chunk in resp.iter_text())
    frames = _parse_sse_frames(body)
    events = [f.get("event") for f in frames]
    assert "run_start" in events
    # Stream now emits 'iteration' events (renamed from 'step' after the
    # per-iteration cost-tracking refactor). Accept either for back-compat.
    assert ("step" in events) or ("iteration" in events)
    assert events[-1] == "final"
    final = frames[-1]
    assert final["outcome"] == "success"
    assert final["final_text"] == "Done."
    assert len(final["steps"]) == 1
    assert final["steps"][0]["tool_name"] == "summarize_pipeline"
    assert final["cost"]["tokens_in"] == 15


def test_agent_stream_no_provider_emits_single_final(monkeypatch):
    _set_provider(monkeypatch, present=False)
    _disable_fast_lane(monkeypatch)
    app = _build_app(monkeypatch, FakeLLMClient(script=[]))
    client = TestClient(app)
    with client.stream("POST", "/api/ai/agent/stream", json=_request_body()) as resp:
        assert resp.status_code == 200
        body = "".join(chunk for chunk in resp.iter_text())
    frames = _parse_sse_frames(body)
    assert len(frames) == 1
    assert frames[0]["event"] == "final"
    assert frames[0]["no_provider"] is True


def test_agent_response_includes_replay_safe_trace_fields(monkeypatch):
    _set_provider(monkeypatch, present=True)
    _disable_fast_lane(monkeypatch)
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(id="tu-1", name="query_metrics", input={
                "scope": "workspace", "keys": ["runs"], "window": "last_24h",
            })],
            stop_reason="tool_use",
        ),
        LLMResponse(text="42 runs.", tool_uses=[], stop_reason="end_turn"),
    ])
    app = _build_app(monkeypatch, fake)
    client = TestClient(app)
    resp = client.post("/api/ai/agent", json=_request_body())
    step = resp.json()["steps"][0]
    # All 12 fields from project_fpulse_ai_step0_locks.md §3
    for field_name in [
        "step_id", "tool_name", "tool_tier", "input_hash", "output_hash",
        "timestamp", "latency_ms", "tokens_in", "tokens_out",
        "decision_reason", "redactions_applied", "outcome", "policy_rules_fired",
    ]:
        assert field_name in step, f"Missing trace field: {field_name}"
