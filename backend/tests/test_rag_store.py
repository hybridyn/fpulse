"""Tests for fpulse.ai.rag.store — VectorStore CRUD + search fallback."""

from __future__ import annotations

import pytest

from fpulse.ai.rag.store import VectorStore, _bytes_to_float_list, _float_list_to_bytes


def test_float_bytes_round_trip():
    vec = [0.1, -0.5, 1.0, 0.0]
    raw = _float_list_to_bytes(vec)
    assert len(raw) == 4 * 4  # 4 floats × 4 bytes
    out = _bytes_to_float_list(raw)
    assert all(abs(a - b) < 1e-6 for a, b in zip(vec, out))


def test_upsert_and_count(tmp_path):
    store = VectorStore(db_path=str(tmp_path / "rag.db"))
    store.upsert(
        workspace_id="ws1",
        kind="failure",
        content="pipeline X failed",
        embedding=[0.1, 0.2, 0.3],
        metadata={"pipeline": "X"},
    )
    assert store.count() == 1
    assert store.count(workspace_id="ws1") == 1
    assert store.count(workspace_id="other") == 0
    store.close()


def test_upsert_replaces_same_id(tmp_path):
    store = VectorStore(db_path=str(tmp_path / "rag.db"))
    did = store.upsert(
        doc_id="fixed-id",
        workspace_id="ws1",
        kind="doc",
        content="v1",
        embedding=[1.0, 0.0],
    )
    assert did == "fixed-id"
    store.upsert(
        doc_id="fixed-id",
        workspace_id="ws1",
        kind="doc",
        content="v2",
        embedding=[1.0, 0.0],
    )
    assert store.count() == 1
    store.close()


def test_search_returns_top_k_workspace_scoped(tmp_path):
    store = VectorStore(db_path=str(tmp_path / "rag.db"))
    # ws1 has two docs, ws2 has one
    store.upsert(workspace_id="ws1", kind="doc", content="cat", embedding=[1.0, 0.0])
    store.upsert(workspace_id="ws1", kind="doc", content="dog", embedding=[0.0, 1.0])
    store.upsert(workspace_id="ws2", kind="doc", content="bird", embedding=[1.0, 0.0])

    # Query closer to "cat" — should return cat first, dog second, no ws2 results
    results = store.search(query_embedding=[1.0, 0.0], workspace_id="ws1", limit=5)
    assert len(results) == 2
    assert results[0]["content"] == "cat"
    assert results[0]["score"] > results[1]["score"]
    # Workspace isolation
    assert all("bird" != r["content"] for r in results)
    store.close()


def test_search_filter_by_kind(tmp_path):
    store = VectorStore(db_path=str(tmp_path / "rag.db"))
    store.upsert(workspace_id="ws1", kind="failure", content="err", embedding=[1.0, 0.0])
    store.upsert(workspace_id="ws1", kind="doc", content="doc1", embedding=[1.0, 0.0])

    results = store.search(query_embedding=[1.0, 0.0], workspace_id="ws1", kind="failure")
    assert len(results) == 1
    assert results[0]["kind"] == "failure"
    store.close()


def test_search_min_score_threshold(tmp_path):
    store = VectorStore(db_path=str(tmp_path / "rag.db"))
    store.upsert(workspace_id="ws1", kind="doc", content="a", embedding=[1.0, 0.0])
    store.upsert(workspace_id="ws1", kind="doc", content="b", embedding=[-1.0, 0.0])

    # Query [1, 0]: score=1 for "a", score=-1 for "b". min_score=0.5 keeps only "a".
    results = store.search(
        query_embedding=[1.0, 0.0], workspace_id="ws1", min_score=0.5,
    )
    assert len(results) == 1
    assert results[0]["content"] == "a"
    store.close()


def test_delete_by_kind(tmp_path):
    store = VectorStore(db_path=str(tmp_path / "rag.db"))
    store.upsert(workspace_id="ws1", kind="failure", content="a", embedding=[1.0, 0.0])
    store.upsert(workspace_id="ws1", kind="doc", content="b", embedding=[1.0, 0.0])
    deleted = store.delete_by_kind("ws1", "failure")
    assert deleted == 1
    assert store.count() == 1
    store.close()
