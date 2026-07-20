"""
Product knowledge — Layer 2 of the chat knowledge architecture (May 4 2026).

What this module does:
  * Loads curated product fact files from `docs/product_facts/*.md`.
  * Splits each file into topic-sized chunks (~500-1500 chars, split at
    `##` headings).
  * Embeds chunks via the existing `Embedder` and stores them in the
    existing `VectorStore` under `kind="product"`.
  * Provides `retrieve_product_facts()` that returns the top-k chunks
    most relevant to a chat prompt.

Why this layer exists:
  * Layer 1 (`session_context.py`) injects WHO/WHERE/WHAT-tier in every
    turn. That's a fixed ~600 tokens.
  * Layer 2 retrieves PRODUCT KNOWLEDGE on demand — node types, edition
    boundaries, troubleshooting, FAQ. Only the relevant chunk is
    injected, so the prompt stays small while the LLM gets accurate
    answers for any specific question about F-Pulse.
  * Layer 3 (the existing tools registry) handles LIVE workspace state.

Together the three layers give the local CPU model enough context to
answer F-Pulse questions correctly without fine-tuning.

Indexing happens once at app startup (see `index_product_knowledge()`)
and is idempotent — re-indexing replaces existing chunks via the
`upsert(doc_id=...)` shape that VectorStore already supports.

The product index is workspace-agnostic — the same product facts apply
to every workspace, so we use the sentinel workspace id "_product" and
the retriever queries against it explicitly.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

from fpulse.ai.rag.embedder import Embedder
from fpulse.ai.rag.store import VectorStore
from fpulse.ai.sanitize import sanitize_for_llm

logger = logging.getLogger(__name__)

# Sentinel workspace id under which all product facts are stored. Lets
# `retrieve_product_facts` query a single namespace regardless of the
# caller's workspace, while still using the existing VectorStore schema
# unchanged.
PRODUCT_WORKSPACE_ID = "_product"

# Chunks above this size are sub-split at sentence boundaries. Below this
# size they're kept whole. Tuned for nomic-embed-text's 512-token soft
# limit and the recommended local-LLM floor (qwen2.5:7b et al.) which
# handles ~300-token retrieval blocks well.
_MAX_CHUNK_CHARS = 1500
# Lowered from 100 to 80 on May 4 2026: a 100-char floor was dropping
# legitimate short FAQ-style sections (e.g. "Is F-Pulse free? Yes …").
# 80 is still well above one-line fragments while keeping real content.
_MIN_CHUNK_CHARS = 80
_MAX_CHUNK_TOTAL_CHARS = 2500     # hard upper bound after sub-splitting


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _product_facts_dir() -> Path:
    """Resolve docs/product_facts/ regardless of cwd. Looks up via the
    package — the repo layout fixes this relative path."""
    # backend/fpulse/ai/product_knowledge.py → repo root is 4 parents up.
    here = Path(__file__).resolve()
    repo_root = here.parents[3]
    return repo_root / "docs" / "product_facts"


# ─────────────────────────────────────────────────────────────────────
# Chunking
# ─────────────────────────────────────────────────────────────────────


def _split_by_headings(markdown: str) -> list[tuple[str, str]]:
    """Split a markdown doc into (heading, body) pairs at level-2 (`##`)
    headings. The first chunk before any `##` is given the heading from
    the file's level-1 (`#`) title, or empty if there isn't one."""
    pairs: list[tuple[str, str]] = []
    title_match = re.match(r"^\s*#\s+(.+)$", markdown, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else ""

    parts = re.split(r"(?m)^##\s+(.+)$", markdown)
    if not parts:
        return pairs

    # parts[0] is everything before the first `##`. Treat as the
    # introduction with the doc's title as heading.
    intro = parts[0].strip()
    if intro:
        # Strip the level-1 title line itself so it doesn't appear twice.
        intro = re.sub(r"^\s*#\s+.+$", "", intro, count=1, flags=re.MULTILINE).strip()
        if intro:
            pairs.append((title or "Overview", intro))

    # Subsequent parts come in pairs: heading, body.
    for i in range(1, len(parts), 2):
        if i + 1 >= len(parts):
            break
        heading = parts[i].strip()
        body = parts[i + 1].strip()
        if body:
            pairs.append((heading, body))
    return pairs


def _sub_split_long(body: str) -> list[str]:
    """Break an oversized body into sentence-boundary subchunks under
    `_MAX_CHUNK_CHARS`. Conservative — prefers fewer larger splits."""
    if len(body) <= _MAX_CHUNK_CHARS:
        return [body]
    # Split at paragraph boundaries first.
    paragraphs = re.split(r"\n\s*\n", body)
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        # If a single paragraph is itself too big, slice on sentence ends.
        if len(para) > _MAX_CHUNK_CHARS:
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for sent in sentences:
                if not sent:
                    continue
                if len(buf) + len(sent) + 1 > _MAX_CHUNK_CHARS and buf:
                    chunks.append(buf.strip())
                    buf = sent
                else:
                    buf = (buf + " " + sent).strip() if buf else sent
            continue
        if len(buf) + len(para) + 2 > _MAX_CHUNK_CHARS and buf:
            chunks.append(buf.strip())
            buf = para
        else:
            buf = (buf + "\n\n" + para).strip() if buf else para
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


def chunk_markdown(filename: str, markdown: str) -> list[dict[str, Any]]:
    """Turn one markdown file into a list of chunk dicts with metadata.

    Each chunk: {filename, heading, content}. Heading prefixes the
    content so the embedder + retriever both see the topic label,
    improving recall on terse queries like "scd2 node".
    """
    out: list[dict[str, Any]] = []
    pairs = _split_by_headings(markdown)
    for heading, body in pairs:
        sub = _sub_split_long(body)
        for piece in sub:
            piece = piece.strip()
            if len(piece) < _MIN_CHUNK_CHARS:
                continue
            full = f"## {heading}\n\n{piece}" if heading else piece
            full = full[:_MAX_CHUNK_TOTAL_CHARS]
            out.append({
                "filename": filename,
                "heading": heading,
                "content": full,
            })
    return out


# ─────────────────────────────────────────────────────────────────────
# Indexing
# ─────────────────────────────────────────────────────────────────────


def _user_doc_entries(facts_path: Path) -> list[tuple[Path, str]]:
    """User-facing docs under ``docs/`` to index alongside the curated
    ``product_facts`` so the Copilot can answer FROM the real documentation,
    not just the hand-curated fact snippets.

    Returns ``(path, label)`` pairs. Deliberately EXCLUDES internal /
    contributor / dated material — design LLDs, security audits, roadmaps,
    ``PROOF-*`` validation snapshots, contributor build guides, internal
    contracts — that would pollute user-facing answers. Opt out entirely
    with ``FPULSE_PRODUCT_KNOWLEDGE_FULL_DOCS=0``.
    """
    import re
    if os.environ.get("FPULSE_PRODUCT_KNOWLEDGE_FULL_DOCS", "").strip().lower() in ("0", "false", "no", "off"):
        return []
    docs_root = facts_path.parent
    if not docs_root.is_dir():
        return []
    exclude = re.compile(
        r"(^|/)(product_facts|design|security|roadmap|extend|releases)/"
        r"|proof|audit|threat-model|ai-boundary-contract|ai-ops-contract"
        r"|dev-guide|(^|/)testing\.md$|eval-harness|connector-authoring|sprint-",
        re.IGNORECASE,
    )
    out: list[tuple[Path, str]] = []
    for md in sorted(docs_root.rglob("*.md")):
        rel = md.relative_to(docs_root).as_posix()
        if rel.startswith("product_facts/") or exclude.search(rel):
            continue
        out.append((md, rel))
    return out


async def index_product_knowledge(
    *,
    embedder: Embedder,
    vector_store: VectorStore,
    facts_dir: Path | str | None = None,
) -> dict[str, int]:
    """Load + chunk + embed every `*.md` under `docs/product_facts/`.

    Returns `{"files": N, "chunks": M}`. Idempotent — re-running replaces
    chunks via VectorStore.upsert with deterministic doc_ids.

    Best-effort: failures on individual files are logged + skipped, never
    fatal. Chat continues to work without product knowledge if indexing
    fails entirely (Layer 1 + tools still in play).
    """
    # Only fold in the wider user-facing docs when indexing the REAL default
    # docs tree. When a caller passes an explicit facts_dir (tests / fixtures),
    # index ONLY that directory — otherwise we'd re-scan its parent and pull in
    # unrelated or duplicate files.
    _use_full_docs = facts_dir is None
    if facts_dir is None:
        facts_dir = _product_facts_dir()
    facts_path = Path(facts_dir)

    if not facts_path.is_dir():
        logger.warning(
            "product_knowledge: facts dir %s not found; Layer 2 disabled",
            facts_path,
        )
        return {"files": 0, "chunks": 0}

    if os.environ.get("FPULSE_DISABLE_PRODUCT_KNOWLEDGE", "").strip().lower() in ("1", "true", "yes"):
        logger.info("product_knowledge: disabled via env var")
        return {"files": 0, "chunks": 0}

    files = 0
    chunks_indexed = 0
    # Curated product facts (stable label = bare filename, so their vector
    # ids never change) + user-facing docs across the rest of docs/ (labelled
    # by relative path so citations + doc_ids stay unique).
    entries: list[tuple[Path, str]] = [
        (md, md.name) for md in sorted(facts_path.glob("*.md"))
    ]
    if _use_full_docs:
        entries.extend(_user_doc_entries(facts_path))
    for md_path, label in entries:
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                raw = f.read()
        except Exception as exc:  # noqa: BLE001
            logger.warning("product_knowledge: read %s failed: %s", label, exc)
            continue

        try:
            chunks = chunk_markdown(label, raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("product_knowledge: chunk %s failed: %s", label, exc)
            continue

        if not chunks:
            continue
        files += 1

        for ch in chunks:
            content = ch["content"]
            sanitized = sanitize_for_llm(content, max_chars=_MAX_CHUNK_TOTAL_CHARS)
            text = str(sanitized.payload)

            embedding = await embedder.embed(text)
            if embedding is None:
                logger.debug(
                    "product_knowledge: embed returned None for %s/%s",
                    md_path.name, ch["heading"],
                )
                continue

            doc_id = (
                f"product:{_content_hash(label + '|' + ch['heading'] + '|' + text[:200])}"
            )
            try:
                vector_store.upsert(
                    doc_id=doc_id,
                    workspace_id=PRODUCT_WORKSPACE_ID,
                    kind="product",
                    content=text,
                    embedding=embedding,
                    metadata={
                        "filename": ch["filename"],
                        "heading": ch["heading"],
                    },
                )
                chunks_indexed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "product_knowledge: upsert %s/%s failed: %s",
                    md_path.name, ch["heading"], exc,
                )

    logger.info(
        "product_knowledge: indexed %d chunks from %d files",
        chunks_indexed, files,
    )
    return {"files": files, "chunks": chunks_indexed}


# ─────────────────────────────────────────────────────────────────────
# Retrieval
# ─────────────────────────────────────────────────────────────────────


async def retrieve_product_facts(
    *,
    query: str,
    embedder: Embedder,
    vector_store: VectorStore,
    limit: int = 3,
    min_score: float = 0.35,
) -> list[dict[str, Any]]:
    """Top-k product knowledge chunks for the user's prompt.

    Returns sanitized chunks ready for system-prompt injection. Empty
    list when product knowledge is disabled, the embedder is down, or
    nothing scores above `min_score`.

    The min_score is intentionally a touch higher than workspace RAG's
    default (0.30): we'd rather inject NOTHING than inject a barely-
    relevant chunk that wastes context on a small CPU model.
    """
    if os.environ.get("FPULSE_DISABLE_PRODUCT_KNOWLEDGE", "").strip().lower() in ("1", "true", "yes"):
        return []
    if os.environ.get("FPULSE_DISABLE_RAG", "").strip().lower() in ("1", "true", "yes"):
        # Honour the umbrella RAG-disable flag — if RAG is off, product
        # knowledge is off too. Layer 1 + fast lane still cover the
        # common ground.
        return []

    query_embedding = await embedder.embed(query)
    if query_embedding is None:
        return []

    try:
        raw = vector_store.search(
            query_embedding=query_embedding,
            workspace_id=PRODUCT_WORKSPACE_ID,
            kind="product",
            limit=limit,
            min_score=min_score,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("product_knowledge: search failed: %s", exc)
        return []

    chunks: list[dict[str, Any]] = []
    total_chars = 0
    char_budget = 1800   # leave room for Layer 1 + workspace RAG inside the budget
    for r in raw:
        content = r.get("content", "")
        sanitized = sanitize_for_llm(
            content, max_chars=min(700, char_budget - total_chars),
        )
        text = str(sanitized.payload)
        if total_chars + len(text) > char_budget:
            break
        chunks.append({
            "kind": "product",
            "content": text,
            "metadata": r.get("metadata", {}),
            "score": r.get("score", 0.0),
        })
        total_chars += len(text)
    return chunks


def format_product_context(chunks: list[dict[str, Any]]) -> str:
    """Render retrieved product chunks as a system-prompt block.

    Distinct from the workspace-RAG block so the LLM can tell when
    facts come from the curated product knowledge base vs. the user's
    own workspace history.
    """
    if not chunks:
        return ""
    lines = ["--- F-Pulse product knowledge (curated) ---"]
    for i, ch in enumerate(chunks, 1):
        meta = ch.get("metadata", {}) or {}
        loc = meta.get("filename", "") or "facts"
        head = meta.get("heading", "") or ""
        score = ch.get("score", 0.0)
        label = f"{loc}#{head}" if head else loc
        lines.append(f"[{i}] ({label}, relevance={score:.2f}):")
        lines.append(ch.get("content", ""))
        lines.append("")
    lines.append("--- End product knowledge ---")
    return "\n".join(lines).strip()
