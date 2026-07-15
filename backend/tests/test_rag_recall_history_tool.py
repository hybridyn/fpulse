"""Tests for the recall_history agent tool."""

from __future__ import annotations

import asyncio

from fpulse.ai.tools import RECALL_HISTORY, ToolContext, ToolTier
from fpulse.ai.tools.base import ToolDefinition


def test_recall_history_definition_shape():
    assert isinstance(RECALL_HISTORY, ToolDefinition)
    assert RECALL_HISTORY.name == "recall_history"
    assert RECALL_HISTORY.tier == ToolTier.READ
    assert RECALL_HISTORY.requires_idempotency_key is False
    schema = RECALL_HISTORY.input_schema
    assert "query" in schema["properties"]
    assert schema.get("required") == ["query"]


def test_recall_history_empty_query_returns_empty():
    ctx = ToolContext(
        tenant_id="t1", user_id="u1", workspace_id="ws1", environment="dev",
    )
    result = asyncio.run(RECALL_HISTORY.handler({"query": ""}, ctx))
    assert result["chunks"] == []
    assert result["total"] == 0
    assert result["workspace_id"] == "ws1"


def test_recall_history_returns_empty_when_rag_not_in_app_state():
    """When app_state has no rag_embedder/rag_store, tool returns empty."""
    import fpulse.main as main_mod

    saved_emb = main_mod.app_state.pop("rag_embedder", None)
    saved_store = main_mod.app_state.pop("rag_store", None)
    try:
        ctx = ToolContext(
            tenant_id="t1", user_id="u1", workspace_id="ws1", environment="dev",
        )
        result = asyncio.run(RECALL_HISTORY.handler({"query": "anything"}, ctx))
        assert result["chunks"] == []
        assert result["total"] == 0
    finally:
        if saved_emb is not None:
            main_mod.app_state["rag_embedder"] = saved_emb
        if saved_store is not None:
            main_mod.app_state["rag_store"] = saved_store


def test_recall_history_calls_retrieve_when_wired(tmp_path):
    """When app_state has embedder + store, tool dispatches to retrieve."""
    import fpulse.main as main_mod
    from fpulse.ai.rag.store import VectorStore

    class FakeEmbedder:
        async def embed(self, text):
            return [1.0, 0.0]

        async def is_available(self):
            return True

    store = VectorStore(db_path=str(tmp_path / "rag.db"))
    store.upsert(
        workspace_id="ws1", kind="doc", content="hello", embedding=[1.0, 0.0],
    )

    saved_emb = main_mod.app_state.get("rag_embedder")
    saved_store = main_mod.app_state.get("rag_store")
    main_mod.app_state["rag_embedder"] = FakeEmbedder()
    main_mod.app_state["rag_store"] = store

    try:
        ctx = ToolContext(
            tenant_id="t1", user_id="u1", workspace_id="ws1", environment="dev",
        )
        result = asyncio.run(RECALL_HISTORY.handler({"query": "hi", "limit": 5}, ctx))
        assert result["total"] >= 1
        assert any("hello" in c["content"] for c in result["chunks"])
    finally:
        if saved_emb is None:
            main_mod.app_state.pop("rag_embedder", None)
        else:
            main_mod.app_state["rag_embedder"] = saved_emb
        if saved_store is None:
            main_mod.app_state.pop("rag_store", None)
        else:
            main_mod.app_state["rag_store"] = saved_store
        store.close()
