"""Tests for fpulse.ai.rag.retrieve — top-k retrieval + sanitization."""

from __future__ import annotations

import asyncio

from fpulse.ai.rag.retrieve import retrieve, format_context_block
from fpulse.ai.rag.store import VectorStore


class FakeEmbedder:
    def __init__(self, vec=None):
        self._vec = vec or [1.0, 0.0]

    async def embed(self, text: str):
        return self._vec

    async def is_available(self) -> bool:
        return True


class BrokenEmbedder:
    async def embed(self, text):
        return None


def test_retrieve_returns_chunks_above_threshold(tmp_path):
    store = VectorStore(db_path=str(tmp_path / "rag.db"))
    store.upsert(workspace_id="ws1", kind="doc", content="hello world", embedding=[1.0, 0.0])
    store.upsert(workspace_id="ws1", kind="doc", content="goodbye", embedding=[-1.0, 0.0])

    chunks = asyncio.run(retrieve(
        query="hi",
        workspace_id="ws1",
        embedder=FakeEmbedder([1.0, 0.0]),
        vector_store=store,
        limit=5,
    ))
    assert len(chunks) == 1
    assert chunks[0]["content"] == "hello world"
    assert "score" in chunks[0]
    store.close()


def test_retrieve_returns_empty_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FPULSE_DISABLE_RAG", "1")
    store = VectorStore(db_path=str(tmp_path / "rag.db"))
    store.upsert(workspace_id="ws1", kind="doc", content="x", embedding=[1.0, 0.0])

    chunks = asyncio.run(retrieve(
        query="x",
        workspace_id="ws1",
        embedder=FakeEmbedder(),
        vector_store=store,
    ))
    assert chunks == []
    store.close()


def test_retrieve_returns_empty_when_embedding_fails(tmp_path):
    store = VectorStore(db_path=str(tmp_path / "rag.db"))
    chunks = asyncio.run(retrieve(
        query="x",
        workspace_id="ws1",
        embedder=BrokenEmbedder(),
        vector_store=store,
    ))
    assert chunks == []
    store.close()


def test_retrieve_respects_kind_filter(tmp_path):
    store = VectorStore(db_path=str(tmp_path / "rag.db"))
    store.upsert(workspace_id="ws1", kind="failure", content="err1", embedding=[1.0, 0.0])
    store.upsert(workspace_id="ws1", kind="doc", content="doc1", embedding=[1.0, 0.0])

    chunks = asyncio.run(retrieve(
        query="x",
        workspace_id="ws1",
        embedder=FakeEmbedder([1.0, 0.0]),
        vector_store=store,
        kind="failure",
    ))
    assert len(chunks) == 1
    assert chunks[0]["kind"] == "failure"
    store.close()


def test_retrieve_size_capped(tmp_path):
    store = VectorStore(db_path=str(tmp_path / "rag.db"))
    big = "x" * 1000
    for _ in range(10):
        store.upsert(workspace_id="ws1", kind="doc", content=big, embedding=[1.0, 0.0])

    chunks = asyncio.run(retrieve(
        query="x",
        workspace_id="ws1",
        embedder=FakeEmbedder([1.0, 0.0]),
        vector_store=store,
        limit=10,
    ))
    total_chars = sum(len(c["content"]) for c in chunks)
    assert total_chars <= 2048
    store.close()


def test_format_context_block_empty():
    assert format_context_block([]) == ""


def test_format_context_block_includes_kind_and_score():
    block = format_context_block([
        {"kind": "failure", "score": 0.85, "content": "pipeline X failed", "metadata": {}},
        {"kind": "doc", "score": 0.72, "content": "how to deploy", "metadata": {}},
    ])
    assert "Retrieved Context" in block
    assert "failure" in block
    assert "0.85" in block
    assert "pipeline X failed" in block
    assert "End Retrieved Context" in block
