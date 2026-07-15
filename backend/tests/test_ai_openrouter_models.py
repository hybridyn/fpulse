"""Tests for GET /api/ai/openrouter/models — model browser endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fpulse.api import ai as ai_api


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ai_api.router)
    return app


def _reset_caches():
    ai_api._OR_MODELS_CACHE["data"] = None
    ai_api._OR_MODELS_CACHE["fetched_at"] = 0.0


def _mock_openrouter_response(models: list[dict]):
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value={"data": models})

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=fake_resp)
    return mock_client


SAMPLE_MODELS = [
    {
        "id": "openai/gpt-4o-mini",
        "name": "GPT-4o mini",
        "context_length": 128000,
        "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
        "supported_parameters": ["tools", "tool_choice"],
    },
    {
        "id": "anthropic/claude-sonnet-4",
        "name": "Claude Sonnet 4",
        "context_length": 200000,
        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        "supported_parameters": ["tools"],
    },
    {
        "id": "deepseek/deepseek-chat-v3:free",
        "name": "DeepSeek V3 (Free)",
        "context_length": 64000,
        "pricing": {"prompt": "0", "completion": "0"},
        "supported_parameters": [],
    },
    {
        "id": "meta-llama/llama-3.1-8b-instruct",
        "name": "Llama 3.1 8B",
        "context_length": 32000,
        "pricing": {"prompt": "0.00000005", "completion": "0.0000001"},
    },
]


def test_openrouter_models_returns_full_catalog():
    _reset_caches()
    mock_client = _mock_openrouter_response(SAMPLE_MODELS)

    with patch("httpx.AsyncClient", return_value=mock_client):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/ai/openrouter/models")

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "openrouter"
    assert body["total"] == 4
    ids = {item["id"] for item in body["items"]}
    assert "openai/gpt-4o-mini" in ids
    assert "deepseek/deepseek-chat-v3:free" in ids


def test_openrouter_models_marks_free_models():
    _reset_caches()
    mock_client = _mock_openrouter_response(SAMPLE_MODELS)

    with patch("httpx.AsyncClient", return_value=mock_client):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/ai/openrouter/models")

    body = resp.json()
    free_ids = {it["id"] for it in body["items"] if it["is_free"]}
    assert "deepseek/deepseek-chat-v3:free" in free_ids
    assert "openai/gpt-4o-mini" not in free_ids


def test_openrouter_models_free_only_filter():
    _reset_caches()
    mock_client = _mock_openrouter_response(SAMPLE_MODELS)

    with patch("httpx.AsyncClient", return_value=mock_client):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/ai/openrouter/models?free_only=true")

    body = resp.json()
    assert all(it["is_free"] for it in body["items"])
    assert body["total"] >= 1


def test_openrouter_models_tools_only_filter():
    _reset_caches()
    mock_client = _mock_openrouter_response(SAMPLE_MODELS)

    with patch("httpx.AsyncClient", return_value=mock_client):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/ai/openrouter/models?tools_only=true")

    body = resp.json()
    # Both openai/* and anthropic/* declare tools support; meta-llama-3.1
    # also matches via the prefix heuristic
    assert all(it["supports_tools"] for it in body["items"])
    ids = {it["id"] for it in body["items"]}
    assert "openai/gpt-4o-mini" in ids
    assert "anthropic/claude-sonnet-4" in ids


def test_openrouter_models_sorts_free_first_then_by_cost():
    _reset_caches()
    mock_client = _mock_openrouter_response(SAMPLE_MODELS)

    with patch("httpx.AsyncClient", return_value=mock_client):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/ai/openrouter/models")

    body = resp.json()
    items = body["items"]
    # Free always first
    assert items[0]["is_free"] is True
    # Among non-free, ascending est cost
    paid = [it for it in items if not it["is_free"]]
    costs = [it["est_cost_per_turn_usd"] for it in paid]
    assert costs == sorted(costs)


def test_openrouter_models_returns_unavailable_on_network_error():
    _reset_caches()
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("connection refused"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/ai/openrouter/models")

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "unavailable"
    assert body["total"] == 0


def test_openrouter_models_uses_cache_within_ttl():
    """Second call within TTL should not hit httpx again."""
    _reset_caches()
    mock_client = _mock_openrouter_response(SAMPLE_MODELS)

    with patch("httpx.AsyncClient", return_value=mock_client):
        app = _build_app()
        client = TestClient(app)
        client.get("/api/ai/openrouter/models")
        client.get("/api/ai/openrouter/models")

    # Only one network call despite two requests
    assert mock_client.get.call_count == 1


def test_openrouter_models_force_bypasses_cache():
    _reset_caches()
    mock_client = _mock_openrouter_response(SAMPLE_MODELS)

    with patch("httpx.AsyncClient", return_value=mock_client):
        app = _build_app()
        client = TestClient(app)
        client.get("/api/ai/openrouter/models")
        client.get("/api/ai/openrouter/models?force=true")

    assert mock_client.get.call_count == 2
