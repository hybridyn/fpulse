"""
RAG retrieval — top-k cosine similarity search with sanitization.

Returns chunks ready for prompt injection: sanitized, size-capped,
workspace-scoped. Never exposes raw embeddings or internal IDs to the LLM.
"""

from __future__ import annotations

import os
from typing import Any, Sequence

from fpulse.ai.rag.embedder import Embedder
from fpulse.ai.rag.store import VectorStore
from fpulse.ai.sanitize import sanitize_for_llm

_MAX_TOTAL_CHARS = 2048
_DEFAULT_MIN_SCORE = 0.3

VALID_KINDS = {"failure", "pipeline", "doc", "catalog", "product"}


async def retrieve(
    *,
    query: str,
    workspace_id: str,
    embedder: Embedder,
    vector_store: VectorStore,
    kind: str | None = None,
    limit: int = 5,
    min_score: float | None = None,
) -> list[dict[str, Any]]:
    """Retrieve top-k relevant chunks for a query within a workspace.

    Returns list of {kind, content, metadata, score} dicts, sanitized
    and size-capped at 2KB total. Empty list when RAG is disabled or
    no matches found.
    """
    if os.environ.get("FPULSE_DISABLE_RAG", "").strip().lower() in ("1", "true", "yes"):
        return []

    if kind and kind not in VALID_KINDS:
        kind = None

    query_embedding = await embedder.embed(query)
    if query_embedding is None:
        return []

    threshold = min_score if min_score is not None else _DEFAULT_MIN_SCORE
    raw_results = vector_store.search(
        query_embedding=query_embedding,
        workspace_id=workspace_id,
        kind=kind,
        limit=limit,
        min_score=threshold,
    )

    chunks: list[dict[str, Any]] = []
    total_chars = 0
    for result in raw_results:
        content = result.get("content", "")
        sanitized = sanitize_for_llm(content, max_chars=min(500, _MAX_TOTAL_CHARS - total_chars))
        text = str(sanitized.payload)

        if total_chars + len(text) > _MAX_TOTAL_CHARS:
            break

        chunks.append({
            "kind": result.get("kind", ""),
            "content": text,
            "metadata": result.get("metadata", {}),
            "score": result.get("score", 0.0),
        })
        total_chars += len(text)

    return chunks


def format_context_block(chunks: list[dict[str, Any]]) -> str:
    """Format retrieved chunks into a context block for the system prompt.

    Returns empty string when no chunks are provided.
    """
    if not chunks:
        return ""

    lines = ["--- Retrieved Context (from workspace history & docs) ---"]
    for i, chunk in enumerate(chunks, 1):
        kind = chunk.get("kind", "unknown")
        score = chunk.get("score", 0.0)
        content = chunk.get("content", "")
        lines.append(f"[{i}] ({kind}, relevance={score:.2f}): {content}")
    lines.append("--- End Retrieved Context ---")
    return "\n".join(lines)
