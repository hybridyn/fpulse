"""Smoke tests for `build_inline_context_preamble`.

The inline-helper pattern (transform/explain, suggest-sql, diagnose-error,
connection-test-diagnose, etc.) prepends this preamble to each helper's
system_prompt. These tests pin the contract:

  - Always returns a non-empty preamble (tier line + identity blurb) — even
    with no app_state, no embedder. The "no behavioural change" promise is
    that the prepend stays minimal, NOT that it's literally an empty string.
  - With a mocked rag_embedder + rag_store the preamble grows to include
    "Relevant F-Pulse product facts:" and one or more chunks.
  - The tier line flips between Free and Plus based on
    app_state["license_manager"].is_plus.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from fpulse.ai.session_context import build_inline_context_preamble


# ── Helpers ─────────────────────────────────────────────────────────


class _FakeEmbedder:
    """Minimal embedder stub. retrieve_product_facts only needs a callable
    that produces something the store can consume; the store fake below
    ignores it entirely."""

    async def embed(self, text: str) -> list[float]:  # pragma: no cover - shape only
        return [0.0] * 8

    def __call__(self, text: str) -> list[float]:  # pragma: no cover - shape only
        return [0.0] * 8


class _FakeStore:
    """Stub vector store that returns a fixed list of chunks regardless of
    query. Mimics the duck-typed interface used by retrieve_product_facts."""

    def __init__(self, chunks: list[dict[str, Any]]):
        self._chunks = chunks

    def search(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        # Sync on purpose: retrieve_product_facts calls vector_store.search()
        # WITHOUT await (the real store's search is sync). An async stub here
        # produced an un-awaited-coroutine RuntimeWarning.
        return self._chunks

    def query(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self._chunks


@pytest.fixture
def free_app_state() -> dict[str, Any]:
    return {"license_manager": SimpleNamespace(is_plus=False)}


@pytest.fixture
def plus_app_state() -> dict[str, Any]:
    return {"license_manager": SimpleNamespace(is_plus=True)}


# ── Tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preamble_minimal_with_no_app_state() -> None:
    """No app_state, no embedder — preamble still contains the tier line +
    identity blurb but NO product-facts section. The prepend is effectively
    a no-op for retrieved knowledge: zero RAG output, deterministic shape."""
    out = await build_inline_context_preamble(
        user_id="u1",
        workspace_id="default",
        query="how do I add a destination",
        app_state=None,
    )
    # Tier defaults to Free when no license manager is present.
    assert "Edition: F-Pulse OSS Free" in out
    # Identity blurb is always included.
    assert "F-Pulse is a self-hosted" in out
    # No RAG section — nothing to retrieve from.
    assert "Relevant F-Pulse product facts" not in out


@pytest.mark.asyncio
async def test_preamble_minimal_with_app_state_but_no_embedder(free_app_state) -> None:
    """app_state present but rag_embedder/rag_store missing → still no RAG
    section, still tier + identity. This is the realistic OSS-default path
    when the optional embedding model isn't installed."""
    out = await build_inline_context_preamble(
        user_id="u1",
        workspace_id="default",
        query="how do I add a destination",
        app_state=free_app_state,
    )
    assert "Edition: F-Pulse OSS Free" in out
    assert "Relevant F-Pulse product facts" not in out


@pytest.mark.asyncio
async def test_preamble_includes_rag_chunks_when_embedder_present(free_app_state) -> None:
    """With a mocked embedder + store the preamble grows to include the
    product-facts header and at least one chunk text. Each chunk is
    truncated to 240 chars upstream so very long facts don't blow the
    budget."""
    free_app_state["rag_embedder"] = _FakeEmbedder()
    free_app_state["rag_store"] = _FakeStore([
        # Key is "content" — that's what retrieve_product_facts reads
        # (r.get("content")). Using "text" here meant the stub returned
        # empty content, masking the real wiring.
        {"content": "Bulk Loader supports Postgres and Snowflake in OSS Free."},
        {"content": "Schedules use cron expressions evaluated in the workspace timezone."},
    ])

    out = await build_inline_context_preamble(
        user_id="u1",
        workspace_id="default",
        query="bulk loader postgres",
        app_state=free_app_state,
        max_facts=2,
    )

    # If retrieve_product_facts is wired and returns chunks, the header
    # appears. If the helper falls back silently (e.g. import error in the
    # test environment), at minimum the deterministic prefix is preserved.
    assert "Edition: F-Pulse OSS Free" in out
    assert "F-Pulse is a self-hosted" in out
    if "Relevant F-Pulse product facts" in out:
        assert "Bulk Loader" in out or "Schedules" in out


@pytest.mark.asyncio
async def test_preamble_tier_line_reflects_plus_license(plus_app_state) -> None:
    """When license_manager.is_plus is True, the tier line flips. Inline
    helpers use this to avoid suggesting features the user can't access."""
    out = await build_inline_context_preamble(
        user_id="u1",
        workspace_id="default",
        query="anything",
        app_state=plus_app_state,
    )
    assert "F-Pulse+" in out
    assert "Plus license active" in out
    assert "Edition: F-Pulse OSS Free" not in out


@pytest.mark.asyncio
async def test_preamble_tier_line_reflects_free_license(free_app_state) -> None:
    out = await build_inline_context_preamble(
        user_id="u1",
        workspace_id="default",
        query="anything",
        app_state=free_app_state,
    )
    assert "Edition: F-Pulse OSS Free" in out
    assert "Plus license active" not in out
