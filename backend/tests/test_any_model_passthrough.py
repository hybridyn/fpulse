"""Any-model passthrough — F-Pulse must honor whatever provider/model the user
picks, not just the named ones with dedicated handling.

Covers all four AI entry points:
  - planner.ai_client.ai_generate_pipeline  (pipeline JSON)
  - planner.ai_client.ai_generate_json      (generic JSON)
  - planner.ai_client.ai_generate_text      (plain text)
  - ai.openai_compatible_client.OpenAICompatibleAgentClient  (the Steward)

A provider outside {claude, openai, openrouter, ollama} must route through the
generic OpenAI-compatible path instead of being silently dropped (planner) or
hard-failing on the Anthropic client (Steward).
"""

from __future__ import annotations

import asyncio

from fpulse.planner import ai_client
from fpulse.planner.ai_client import _chat_completions_url, openai_compatible_base


# ── Pure endpoint resolution ─────────────────────────────────────────────

def test_openai_compatible_base_uses_known_default():
    assert openai_compatible_base("deepseek", "") == "https://api.deepseek.com/v1"
    assert openai_compatible_base("moonshot", None) == "https://api.moonshot.cn/v1"
    assert openai_compatible_base("kimi", "") == "https://api.moonshot.cn/v1"


def test_explicit_base_url_wins_over_default():
    assert openai_compatible_base("deepseek", "http://localhost:8000/v1") == (
        "http://localhost:8000/v1"
    )


def test_unknown_provider_without_base_is_empty():
    assert openai_compatible_base("some-random-vendor", "") == ""


def test_chat_completions_url_normalization():
    assert _chat_completions_url("https://api.deepseek.com/v1") == (
        "https://api.deepseek.com/v1/chat/completions"
    )
    assert _chat_completions_url("https://api.deepseek.com/v1/") == (
        "https://api.deepseek.com/v1/chat/completions"
    )
    full = "https://host/v1/chat/completions"
    assert _chat_completions_url(full) == full


# ── Planner routing (all three entry points) ─────────────────────────────

def test_pipeline_routes_unknown_provider_to_compatible(monkeypatch):
    captured: dict = {}

    async def fake_call(base_url, api_key, model, messages):
        captured.update(base=base_url, model=model)
        return ({"name": "P", "steps": [], "connections": [], "explanation": "ok"}, {"input": 1, "output": 2})

    monkeypatch.setattr(ai_client, "resolve_provider", lambda **kw: ("deepseek", "sk", "deepseek-chat", ""))
    monkeypatch.setattr(ai_client, "_call_openai_compatible", fake_call)

    result = asyncio.run(ai_client.ai_generate_pipeline([{"role": "user", "content": "load a csv"}]))
    assert result and result["name"] == "P"
    assert captured["base"] == "https://api.deepseek.com/v1"


def test_json_routes_unknown_provider_to_compatible(monkeypatch):
    captured: dict = {}

    async def fake_call(base_url, api_key, model, system_prompt, messages):
        captured.update(base=base_url, model=model)
        return ({"ok": True}, {"input": 1, "output": 1})

    monkeypatch.setattr(ai_client, "resolve_provider", lambda **kw: ("moonshot", "sk", "moonshot-v1-8k", ""))
    monkeypatch.setattr(ai_client, "_call_text_openai_compatible", fake_call)

    result = asyncio.run(ai_client.ai_generate_json(
        [{"role": "user", "content": "hi"}], system_prompt="sys", source_label="test.json"
    ))
    assert result == {"ok": True}
    assert captured["base"] == "https://api.moonshot.cn/v1"


def test_text_routes_unknown_provider_to_compatible(monkeypatch):
    captured: dict = {}

    async def fake_call(base_url, api_key, model, system_prompt, messages, max_tokens):
        captured.update(base=base_url, model=model)
        return ("hello", {"input": 1, "output": 1})

    # Local vLLM/LM Studio via explicit base_url + no key.
    monkeypatch.setattr(
        ai_client, "resolve_provider",
        lambda **kw: ("custom", "", "qwen2.5-coder", "http://localhost:8000/v1"),
    )
    monkeypatch.setattr(ai_client, "_call_plain_openai_compatible", fake_call)

    result = asyncio.run(ai_client.ai_generate_text(
        [{"role": "user", "content": "hi"}], system_prompt="sys", source_label="test.text"
    ))
    assert result == "hello"
    assert captured["base"] == "http://localhost:8000/v1"


def test_pipeline_unknown_provider_without_base_returns_none(monkeypatch):
    monkeypatch.setattr(ai_client, "resolve_provider", lambda **kw: ("mystery-vendor", "k", "m", ""))
    result = asyncio.run(ai_client.ai_generate_pipeline([{"role": "user", "content": "hi"}]))
    assert result is None


# ── Steward client ───────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    capture: dict = {}
    payload: dict = {}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeAsyncClient.capture = {"url": url, "headers": headers, "body": json}
        return _FakeResp(_FakeAsyncClient.payload)


def test_steward_compatible_client_hits_resolved_endpoint(monkeypatch):
    from fpulse.ai import openai_compatible_client as occ

    _FakeAsyncClient.payload = {
        "choices": [{"message": {"content": "hello", "tool_calls": []}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    monkeypatch.setattr(ai_client, "resolve_provider", lambda **kw: ("moonshot", "sk-kimi", "moonshot-v1-8k", ""))
    monkeypatch.setattr(occ.httpx, "AsyncClient", _FakeAsyncClient)

    client = occ.OpenAICompatibleAgentClient()
    resp = asyncio.run(client.call(system="sys", messages=[], tools=[]))

    assert resp.text == "hello"
    assert resp.tokens_in == 5 and resp.tokens_out == 3
    assert _FakeAsyncClient.capture["url"] == "https://api.moonshot.cn/v1/chat/completions"
    assert _FakeAsyncClient.capture["headers"].get("Authorization") == "Bearer sk-kimi"


def test_steward_compatible_client_omits_auth_for_local_server(monkeypatch):
    from fpulse.ai import openai_compatible_client as occ

    _FakeAsyncClient.payload = {
        "choices": [{"message": {"content": "ok", "tool_calls": []}, "finish_reason": "stop"}],
        "usage": {},
    }
    monkeypatch.setattr(
        ai_client, "resolve_provider",
        lambda **kw: ("custom", "", "qwen2.5-coder", "http://localhost:8000/v1"),
    )
    monkeypatch.setattr(occ.httpx, "AsyncClient", _FakeAsyncClient)

    client = occ.OpenAICompatibleAgentClient()
    resp = asyncio.run(client.call(system="sys", messages=[], tools=[]))

    assert resp.text == "ok"
    assert _FakeAsyncClient.capture["url"] == "http://localhost:8000/v1/chat/completions"
    assert "Authorization" not in _FakeAsyncClient.capture["headers"]
