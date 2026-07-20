"""Unit tests for the chat product-knowledge layer (Layer 2).

Chunking is pure (no I/O) — tested directly. Indexing + retrieval use
fake Embedder + VectorStore so we exercise the wiring without touching
Ollama or sqlite-vec.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fpulse.ai.product_knowledge import (
    PRODUCT_WORKSPACE_ID,
    chunk_markdown,
    format_product_context,
    index_product_knowledge,
    retrieve_product_facts,
)


# ── Fakes ────────────────────────────────────────────────────────────


class FakeEmbedder:
    """Returns a deterministic vector based on text content so two calls
    on the same text produce the same embedding (good enough for tests
    that just need 'embeddings exist')."""

    def __init__(self, available: bool = True, dim: int = 8):
        self._available = available
        self._dim = dim

    async def embed(self, text: str) -> list[float] | None:
        if not self._available:
            return None
        # Trivial deterministic hash → vector, normalised.
        h = abs(hash(text))
        vec = [((h >> (i * 4)) & 0xF) / 15.0 for i in range(self._dim)]
        return vec

    async def is_available(self) -> bool:
        return self._available


class FakeVectorStore:
    """In-memory upsert + search keyed by `kind` and `workspace_id`.

    Search returns documents whose embedding matches the query exactly
    (any dimension — we don't simulate cosine distance, just equality).
    Tests can inject scores by pre-loading results.
    """

    def __init__(self):
        self.docs: dict[str, dict[str, Any]] = {}
        self._search_results: list[dict[str, Any]] | None = None

    def upsert(
        self, *, doc_id, workspace_id, kind, content, embedding, metadata,
    ) -> str:
        self.docs[doc_id] = {
            "id": doc_id,
            "workspace_id": workspace_id,
            "kind": kind,
            "content": content,
            "embedding": list(embedding) if embedding else None,
            "metadata": metadata,
        }
        return doc_id

    def force_search_result(self, results: list[dict[str, Any]]) -> None:
        """Override the next search() call's results for retrieval tests."""
        self._search_results = results

    def search(
        self, *, query_embedding, workspace_id, kind, limit, min_score,
    ) -> list[dict[str, Any]]:
        if self._search_results is not None:
            out, self._search_results = self._search_results, None
            return out[:limit]
        # Naive fallback: return docs in the right partition with score=1.0.
        results = []
        for d in self.docs.values():
            if d["workspace_id"] != workspace_id:
                continue
            if kind and d["kind"] != kind:
                continue
            results.append({**d, "score": 1.0})
        return results[:limit]


# ── Chunking ─────────────────────────────────────────────────────────


class TestChunkMarkdown:
    def test_single_section(self):
        md = (
            "# Title\n\n"
            "Some intro text that is comfortably long enough to clear the "
            "minimum chunk-size threshold (around 80 characters) without "
            "any ambiguity at the edge case."
        )
        chunks = chunk_markdown("test.md", md)
        assert len(chunks) == 1
        assert chunks[0]["heading"] == "Title"
        assert "intro text" in chunks[0]["content"]

    def test_multiple_h2_sections(self):
        md = """# F-Pulse intro

Top-level intro paragraph that is comfortably above the minimum chunk threshold so it should be retained as its own chunk after the heading-split pass runs.

## Section A

Content for section A. This is several sentences of meaningful prose describing what section A talks about so it passes the minimum chunk size with margin to spare.

## Section B

Section B content. Several sentences of meaningful prose so the chunker keeps it as a separate chunk after the heading split logic runs and respects the size threshold.
"""
        chunks = chunk_markdown("intro.md", md)
        # Intro + 2 sections = 3 chunks
        assert len(chunks) == 3
        headings = [c["heading"] for c in chunks]
        assert "F-Pulse intro" in headings  # the level-1 title used as intro heading
        assert "Section A" in headings
        assert "Section B" in headings

    def test_short_chunks_dropped(self):
        md = (
            "# T\n\n"
            "## A\n\nOK\n\n"
            "## B\n\nThis section is plainly long enough to clear the 80-character "
            "minimum chunk-size threshold with comfortable margin on either side."
        )
        chunks = chunk_markdown("test.md", md)
        # Section "A" body is too short; only B + the long-enough intro (if any).
        headings = [c["heading"] for c in chunks]
        assert "B" in headings
        assert "A" not in headings

    def test_oversized_section_subsplits(self):
        big = "Sentence one. " * 200   # ~2800 chars, over the 1500 limit.
        md = f"# T\n\n## Big\n\n{big}"
        chunks = chunk_markdown("big.md", md)
        # Should produce 2+ chunks for the big section.
        big_chunks = [c for c in chunks if c["heading"] == "Big"]
        assert len(big_chunks) >= 2

    def test_chunk_includes_heading_prefix(self):
        md = "# T\n\n## Salient\n\n" + ("X" * 200)
        chunks = chunk_markdown("test.md", md)
        for c in chunks:
            if c["heading"] == "Salient":
                assert c["content"].startswith("## Salient")
                break
        else:
            pytest.fail("Salient chunk not produced")


# ── Indexing ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestIndexProductKnowledge:
    async def test_indexes_files_in_dir(self, tmp_path):
        # Build a tiny facts dir. Bodies are clearly above the 80-char
        # minimum chunk threshold so the size-filter doesn't drop them.
        (tmp_path / "01_a.md").write_text(
            "# Title A\n\nSection-A intro that is comfortably above the minimum "
            "chunk-size threshold so the chunker keeps it as its own chunk "
            "without any edge-case ambiguity.",
            encoding="utf-8",
        )
        (tmp_path / "02_b.md").write_text(
            "# Title B\n\n## Topic 1\n\nTopic 1 body that comfortably exceeds the "
            "minimum chunk character count threshold by a wide margin so this "
            "test is not sensitive to small wording tweaks.",
            encoding="utf-8",
        )
        embedder = FakeEmbedder()
        store = FakeVectorStore()
        counts = await index_product_knowledge(
            embedder=embedder, vector_store=store, facts_dir=tmp_path,
        )
        assert counts["files"] == 2
        assert counts["chunks"] >= 2
        # All docs land in the product partition with kind=product.
        for d in store.docs.values():
            assert d["workspace_id"] == PRODUCT_WORKSPACE_ID
            assert d["kind"] == "product"

    async def test_missing_dir_returns_zero(self, tmp_path):
        embedder = FakeEmbedder()
        store = FakeVectorStore()
        counts = await index_product_knowledge(
            embedder=embedder, vector_store=store,
            facts_dir=tmp_path / "does_not_exist",
        )
        assert counts == {"files": 0, "chunks": 0}

    async def test_disable_env_var(self, tmp_path, monkeypatch):
        (tmp_path / "01.md").write_text(
            "# T\n\nSome long enough content here to clear the minimum chunk size limit cleanly.",
            encoding="utf-8",
        )
        monkeypatch.setenv("FPULSE_DISABLE_PRODUCT_KNOWLEDGE", "1")
        embedder = FakeEmbedder()
        store = FakeVectorStore()
        counts = await index_product_knowledge(
            embedder=embedder, vector_store=store, facts_dir=tmp_path,
        )
        assert counts == {"files": 0, "chunks": 0}

    async def test_idempotent_reindex(self, tmp_path):
        (tmp_path / "01.md").write_text(
            "# T\n\nThis is content that exceeds the minimum chunk size of 100 characters easily for the test.",
            encoding="utf-8",
        )
        embedder = FakeEmbedder()
        store = FakeVectorStore()
        first = await index_product_knowledge(
            embedder=embedder, vector_store=store, facts_dir=tmp_path,
        )
        before = len(store.docs)
        second = await index_product_knowledge(
            embedder=embedder, vector_store=store, facts_dir=tmp_path,
        )
        # Same content → same doc_id → upsert replaces, count unchanged.
        assert len(store.docs) == before
        assert first["chunks"] == second["chunks"]


# ── Retrieval ────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestRetrieveProductFacts:
    async def test_retrieve_returns_chunks(self):
        embedder = FakeEmbedder()
        store = FakeVectorStore()
        store.force_search_result([{
            "kind": "product",
            "content": "## Salient\n\nF-Pulse uses qwen2.5:7b as the local CPU default.",
            "metadata": {"filename": "01.md", "heading": "Salient"},
            "score": 0.85,
        }])
        chunks = await retrieve_product_facts(
            query="what local model do you use",
            embedder=embedder,
            vector_store=store,
        )
        assert len(chunks) == 1
        assert "qwen2.5:7b" in chunks[0]["content"]
        assert chunks[0]["score"] == 0.85
        assert chunks[0]["metadata"]["heading"] == "Salient"

    async def test_disable_rag_blocks_product(self, monkeypatch):
        monkeypatch.setenv("FPULSE_DISABLE_RAG", "1")
        embedder = FakeEmbedder()
        store = FakeVectorStore()
        store.force_search_result([{"content": "X", "score": 1.0}])
        chunks = await retrieve_product_facts(
            query="anything", embedder=embedder, vector_store=store,
        )
        assert chunks == []

    async def test_disable_product_only(self, monkeypatch):
        monkeypatch.setenv("FPULSE_DISABLE_PRODUCT_KNOWLEDGE", "1")
        embedder = FakeEmbedder()
        store = FakeVectorStore()
        chunks = await retrieve_product_facts(
            query="anything", embedder=embedder, vector_store=store,
        )
        assert chunks == []

    async def test_embedder_unavailable_returns_empty(self):
        embedder = FakeEmbedder(available=False)
        store = FakeVectorStore()
        chunks = await retrieve_product_facts(
            query="anything", embedder=embedder, vector_store=store,
        )
        assert chunks == []

    async def test_format_context_renders_block(self):
        chunks = [
            {
                "kind": "product",
                "content": "## Editions\n\nOSS Free is Apache 2.0.",
                "metadata": {"filename": "03_editions.md", "heading": "Editions"},
                "score": 0.9,
            },
        ]
        block = format_product_context(chunks)
        assert "F-Pulse product knowledge" in block
        assert "03_editions.md" in block
        assert "Apache 2.0" in block

    async def test_format_empty_returns_empty(self):
        assert format_product_context([]) == ""


# ── Real curated docs exist + are non-empty ─────────────────────────


class TestCuratedDocs:
    def test_facts_dir_exists_and_has_files(self):
        from fpulse.ai.product_knowledge import _product_facts_dir
        d = _product_facts_dir()
        # In CI the docs dir should exist with the curated files.
        assert d.is_dir(), f"facts dir missing: {d}"
        md_files = list(d.glob("*.md"))
        assert len(md_files) >= 5, f"expected ≥5 fact files, found {len(md_files)}"

    def test_curated_files_chunk_cleanly(self):
        from fpulse.ai.product_knowledge import _product_facts_dir
        d = _product_facts_dir()
        for md in d.glob("*.md"):
            raw = md.read_text(encoding="utf-8")
            chunks = chunk_markdown(md.name, raw)
            assert chunks, f"{md.name} produced 0 chunks"
            for c in chunks:
                assert len(c["content"]) >= 100
                assert len(c["content"]) <= 3000
