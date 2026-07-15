"""
Unit tests for the product-knowledge admin API.

Tests the pure functions (record_*) and the endpoint handlers without
needing FastAPI's app instance. Exercises both happy-path and the
several failure modes the endpoints are explicitly defensive about.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import fpulse.api.product_knowledge as api_pk


# ── Pure status hooks ───────────────────────────────────────────────


class TestRecordHooks:
    def setup_method(self):
        # Reset module-level state before each test.
        api_pk._LAST_REINDEX.update({
            "ran_at": None,
            "files": 0,
            "chunks": 0,
            "duration_ms": 0,
            "trigger": None,
            "error": None,
        })

    def test_record_startup_reindex_publishes_state(self):
        api_pk.record_startup_reindex({"files": 3, "chunks": 27}, 4500)
        assert api_pk._LAST_REINDEX["files"] == 3
        assert api_pk._LAST_REINDEX["chunks"] == 27
        assert api_pk._LAST_REINDEX["duration_ms"] == 4500
        assert api_pk._LAST_REINDEX["trigger"] == "startup"
        assert api_pk._LAST_REINDEX["error"] is None
        # ISO timestamp recorded.
        assert api_pk._LAST_REINDEX["ran_at"] is not None
        from datetime import datetime
        datetime.fromisoformat(api_pk._LAST_REINDEX["ran_at"])

    def test_record_startup_failure_sets_error(self):
        api_pk.record_startup_failure("Ollama unreachable")
        assert api_pk._LAST_REINDEX["error"] == "Ollama unreachable"
        assert api_pk._LAST_REINDEX["files"] == 0
        assert api_pk._LAST_REINDEX["chunks"] == 0
        assert api_pk._LAST_REINDEX["trigger"] == "startup"

    def test_record_normalises_int_types(self):
        # `_record` must coerce Nones / strings to safe ints. Defensive
        # against a future caller passing None for chunks.
        api_pk._record(
            {"files": None, "chunks": "12"},  # type: ignore[arg-type]
            duration_ms=100,
            trigger="admin",
        )
        assert api_pk._LAST_REINDEX["files"] == 0
        assert api_pk._LAST_REINDEX["chunks"] == 12


# ── /status endpoint ─────────────────────────────────────────────────


class TestStatusEndpoint:
    def test_status_returns_current_state(self):
        api_pk._record({"files": 6, "chunks": 30}, 1200, "startup")
        out = api_pk.status()
        assert out["files"] == 6
        assert out["chunks"] == 30
        assert out["trigger"] == "startup"
        # Includes the facts_dir_exists meta probe.
        assert "facts_dir_exists" in out
        assert isinstance(out["facts_dir_exists"], bool)

    def test_status_safe_when_facts_dir_check_throws(self, monkeypatch):
        # If _product_facts_dir() raises, status should still return
        # successfully with facts_dir_exists=False.
        def _boom():
            raise RuntimeError("simulated")
        monkeypatch.setattr(
            "fpulse.ai.product_knowledge._product_facts_dir", _boom,
        )
        out = api_pk.status()
        assert out["facts_dir_exists"] is False


# ── /reindex endpoint ────────────────────────────────────────────────


@pytest.mark.asyncio
class TestReindexEndpoint:
    async def test_503_when_app_state_missing_embedder(self, monkeypatch):
        # Simulate app_state with no embedder/store.
        fake_app_state = {}
        # Patch the lazy import in the endpoint.
        with patch.dict("sys.modules"):
            import sys
            import types
            mod = types.ModuleType("fpulse.main")
            mod.app_state = fake_app_state  # type: ignore[attr-defined]
            sys.modules["fpulse.main"] = mod
            with pytest.raises(HTTPException) as ei:
                await api_pk.reindex(user=object())
        assert ei.value.status_code == 503
        assert "embedder" in ei.value.detail.lower() or "vector store" in ei.value.detail.lower()

    async def test_500_when_indexer_raises(self, monkeypatch):
        # app_state has both, but the indexer itself raises.
        class _FakeStore: pass
        class _FakeEmb: pass
        fake_app_state = {
            "rag_embedder": _FakeEmb(),
            "rag_store": _FakeStore(),
        }
        # 2026-05-31: wrap the sys.modules swap in patch.dict so it
        # auto-reverts. Without this, subsequent tests that import
        # fpulse.main get the fake module with `_FakeEmb` (no methods),
        # which broke test_rag_recall_history_tool::
        # test_recall_history_calls_retrieve_when_wired.
        import sys, types
        with patch.dict(sys.modules, {}, clear=False):
            mod = types.ModuleType("fpulse.main")
            mod.app_state = fake_app_state  # type: ignore[attr-defined]
            sys.modules["fpulse.main"] = mod

            async def _boom_index(*, embedder, vector_store, facts_dir=None):
                raise RuntimeError("simulated index failure")
            monkeypatch.setattr(
                "fpulse.ai.product_knowledge.index_product_knowledge", _boom_index,
            )
            with pytest.raises(HTTPException) as ei:
                await api_pk.reindex(user=object())
            assert ei.value.status_code == 500
            # Failure should be recorded in the status state.
            assert api_pk._LAST_REINDEX["error"] is not None
            assert api_pk._LAST_REINDEX["trigger"] == "admin"

    async def test_happy_path_records_state(self, monkeypatch):
        class _FakeStore: pass
        class _FakeEmb: pass
        fake_app_state = {
            "rag_embedder": _FakeEmb(),
            "rag_store": _FakeStore(),
        }
        # 2026-05-31: wrap the sys.modules swap in patch.dict — same
        # rationale as test_500_when_indexer_raises above.
        import sys, types
        with patch.dict(sys.modules, {}, clear=False):
            mod = types.ModuleType("fpulse.main")
            mod.app_state = fake_app_state  # type: ignore[attr-defined]
            sys.modules["fpulse.main"] = mod

            async def _ok_index(*, embedder, vector_store, facts_dir=None):
                return {"files": 6, "chunks": 30}
            monkeypatch.setattr(
                "fpulse.ai.product_knowledge.index_product_knowledge", _ok_index,
            )

            # Caller is a fake "admin" object — endpoint just logs user.id.
            class _Admin:
                id = "u-admin-test"
            out = await api_pk.reindex(user=_Admin())
            assert out["files"] == 6
            assert out["chunks"] == 30
            assert out["trigger"] == "admin"
            assert out["error"] is None
            assert out["duration_ms"] >= 0
            assert api_pk._LAST_REINDEX["files"] == 6
            assert api_pk._LAST_REINDEX["chunks"] == 30
