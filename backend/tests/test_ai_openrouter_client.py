"""Tests for OpenRouterAgentClient.

Mocks httpx so we can verify:
  - Correct base URL and attribution headers
  - Tool-use payload shape mirrors OpenAI's function-calling format
  - tool_calls parse correctly into LLMToolUse blocks
  - resolve_provider must return 'openrouter' before this client is invoked
  - The compare endpoint includes the OpenRouter row
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fpulse.ai.openrouter_client import OpenRouterAgentClient
from fpulse.api import ai as ai_api


class _FakeAsyncClient:
    """Minimal httpx.AsyncClient stand-in that captures the POST args
    and returns a canned response. Used to assert payload shape without
    a real network call."""

    def __init__(self, response_payload: dict, captured: dict):
        self._payload = response_payload
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url: str, headers: dict, json: dict):  # type: ignore[no-untyped-def]
        self._captured["url"] = url
        self._captured["headers"] = headers
        self._captured["body"] = json
        return _FakeResponse(self._payload)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _patch_resolve(monkeypatch, provider: str = "openrouter", model: str = "openai/gpt-4o-mini"):
    """Make resolve_provider return openrouter so the client doesn't
    raise the wrong-provider RuntimeError."""
    def _stub(user_id=None, workspace_id=None):
        return (provider, "sk-or-test-fake", model, "")
    monkeypatch.setattr(
        "fpulse.planner.ai_client.resolve_provider",
        _stub,
    )


def test_openrouter_sends_attribution_headers_and_correct_url(monkeypatch):
    _patch_resolve(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(
        "fpulse.ai.openrouter_client.httpx.AsyncClient",
        lambda **kw: _FakeAsyncClient({
            "choices": [{"message": {"content": "hello", "tool_calls": []}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }, captured),
    )

    client = OpenRouterAgentClient(user_id="u1", workspace_id="default")
    out = asyncio.run(client.call(
        system="You are F-Pulse.",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
    ))

    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    # OpenRouter requires these for attribution / leaderboards
    assert "HTTP-Referer" in captured["headers"]
    assert "X-Title" in captured["headers"]
    assert out.text == "hello"
    assert out.tokens_in == 10
    assert out.tokens_out == 2
    assert out.tool_uses == []
    assert out.stop_reason == "end_turn"


def test_openrouter_sends_namespaced_model_id(monkeypatch):
    _patch_resolve(monkeypatch, model="anthropic/claude-sonnet-4")
    captured: dict = {}
    monkeypatch.setattr(
        "fpulse.ai.openrouter_client.httpx.AsyncClient",
        lambda **kw: _FakeAsyncClient({
            "choices": [{"message": {"content": "hi", "tool_calls": []}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }, captured),
    )
    client = OpenRouterAgentClient()
    asyncio.run(client.call(
        system="x", messages=[{"role": "user", "content": "y"}], tools=[],
    ))
    assert captured["body"]["model"] == "anthropic/claude-sonnet-4"


def test_openrouter_parses_tool_calls(monkeypatch):
    _patch_resolve(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(
        "fpulse.ai.openrouter_client.httpx.AsyncClient",
        lambda **kw: _FakeAsyncClient({
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "list_pipelines",
                                "arguments": '{"limit": 5}',
                            },
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 50, "completion_tokens": 8},
        }, captured),
    )
    client = OpenRouterAgentClient()
    out = asyncio.run(client.call(
        system="sys", messages=[{"role": "user", "content": "list them"}],
        tools=[{"name": "list_pipelines", "description": "x", "input_schema": {"type": "object"}}],
    ))
    assert out.stop_reason == "tool_use"
    assert len(out.tool_uses) == 1
    assert out.tool_uses[0].name == "list_pipelines"
    assert out.tool_uses[0].input == {"limit": 5}
    assert out.tool_uses[0].id == "call_abc"


def test_openrouter_raises_when_resolver_returns_wrong_provider(monkeypatch):
    """The endpoint factory MUST dispatch by provider before constructing
    OpenRouterAgentClient. If the factory misroutes us, fail loud."""
    _patch_resolve(monkeypatch, provider="openai")
    monkeypatch.setattr(
        "fpulse.ai.openrouter_client.httpx.AsyncClient",
        lambda **kw: _FakeAsyncClient({"choices": []}, {}),
    )
    client = OpenRouterAgentClient()
    with pytest.raises(RuntimeError, match="OpenRouterAgentClient invoked but"):
        asyncio.run(client.call(
            system="x", messages=[{"role": "user", "content": "y"}], tools=[],
        ))


def test_openrouter_raises_when_api_key_missing(monkeypatch):
    def _stub(user_id=None, workspace_id=None):
        return ("openrouter", "", "openai/gpt-4o-mini", "")
    monkeypatch.setattr("fpulse.planner.ai_client.resolve_provider", _stub)
    client = OpenRouterAgentClient()
    with pytest.raises(RuntimeError, match="api_key is empty"):
        asyncio.run(client.call(
            system="x", messages=[{"role": "user", "content": "y"}], tools=[],
        ))


# ---------------------------------------------------------------------------
# Compare endpoint includes the OpenRouter row
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ai_api.router)
    return app


def test_compare_endpoint_includes_openrouter_row(monkeypatch):
    """OpenRouter shows up alongside Claude/OpenAI in the price table."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    app = _build_app()
    client = TestClient(app)
    resp = client.get("/api/ai/providers/compare")
    assert resp.status_code == 200
    body = resp.json()

    providers = {it["provider"] for it in body["items"]}
    assert "openrouter" in providers, f"OpenRouter missing from compare; got {providers}"

    or_row = next(it for it in body["items"] if it["provider"] == "openrouter")
    assert or_row["model"].startswith("openai/") or "/" in or_row["model"], (
        "OpenRouter model id should be namespaced (e.g. openai/gpt-4o-mini)"
    )


def test_compare_endpoint_recommendation_still_one_winner(monkeypatch):
    """Adding OpenRouter must not break the single-winner invariant."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = _build_app()
    client = TestClient(app)
    body = client.get("/api/ai/providers/compare").json()
    recs = [it for it in body["items"] if it["recommend"]]
    assert len(recs) == 1
