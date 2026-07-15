"""Tests for fpulse.ai.rag.embedder — Ollama /api/embeddings wrapper."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from fpulse.ai.rag.embedder import Embedder, DEFAULT_MODEL, DEFAULT_OLLAMA_URL


def test_embedder_defaults(monkeypatch):
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("FPULSE_EMBEDDING_MODEL", raising=False)
    e = Embedder()
    assert e.model == DEFAULT_MODEL
    assert e.base_url == DEFAULT_OLLAMA_URL
    assert e._url == f"{DEFAULT_OLLAMA_URL}/api/embeddings"


def test_embedder_custom_config():
    e = Embedder(model="custom-model", base_url="http://example:9999")
    assert e.model == "custom-model"
    assert e.base_url == "http://example:9999"


def test_embedder_strips_trailing_slash_from_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434/")
    e = Embedder()
    assert not e.base_url.endswith("/")


def _build_mock_client(*, embedding=None, raise_exc=None):
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(
        return_value={"embedding": embedding} if embedding is not None else {}
    )

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    if raise_exc:
        mock_client.post = AsyncMock(side_effect=raise_exc)
    else:
        mock_client.post = AsyncMock(return_value=fake_resp)
    return mock_client


def test_embed_returns_vector_on_success():
    mock_client = _build_mock_client(embedding=[0.1, 0.2, 0.3])
    with patch("fpulse.ai.rag.embedder.httpx.AsyncClient", return_value=mock_client):
        e = Embedder()
        vec = asyncio.run(e.embed("hello"))
        assert vec == [0.1, 0.2, 0.3]


def test_embed_returns_none_on_error():
    mock_client = _build_mock_client(raise_exc=Exception("connection refused"))
    with patch("fpulse.ai.rag.embedder.httpx.AsyncClient", return_value=mock_client):
        e = Embedder()
        vec = asyncio.run(e.embed("hello"))
        assert vec is None


def test_embed_batch():
    mock_client = _build_mock_client(embedding=[0.5, 0.5])
    with patch("fpulse.ai.rag.embedder.httpx.AsyncClient", return_value=mock_client):
        e = Embedder()
        results = asyncio.run(e.embed_batch(["a", "b", "c"]))
        assert len(results) == 3
        assert all(r == [0.5, 0.5] for r in results)
