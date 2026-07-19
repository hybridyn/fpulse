"""
Product knowledge admin API — May 4 2026.

Endpoints:
  GET  /api/ai/product-knowledge/status   — public: chunk counts + last
                                             reindex timestamp
  POST /api/ai/product-knowledge/reindex  — admin: re-runs the chunker +
                                             embedder against the live
                                             docs/product_facts/*.md set

Why this exists:
  Layer 2 of the chat knowledge architecture indexes curated product
  facts on app startup. Without an admin reindex hook, editing a fact
  file requires a docker restart for the change to take effect — fine
  for OSS quickstart but rough for an admin iterating on copy.

Status is open (no auth) because the counts + timestamp are useful
context for any user wondering "does the chat know about feature X
yet?". The counts contain no workspace data so there's no privacy
concern.

Reindex is admin-only because it triggers embedding work on the
backend (a few seconds of Ollama CPU time per reindex) and
operators don't want any user to be able to spike that.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from fpulse.auth.deps import require_admin

router = APIRouter(prefix="/api/ai/product-knowledge", tags=["ai"])
logger = logging.getLogger(__name__)


# In-process state. Updated on every reindex (manual or startup) so the
# status endpoint can surface the most recent counts. Lost on restart;
# the startup indexer fills it again within seconds.
_LAST_REINDEX: dict[str, Any] = {
    "ran_at": None,
    "files": 0,
    "chunks": 0,
    "duration_ms": 0,
    "trigger": None,   # "startup" | "admin" | None
    "error": None,
}


def _record(counts: dict[str, int], duration_ms: int, trigger: str, error: str | None = None) -> None:
    _LAST_REINDEX["ran_at"] = datetime.now(timezone.utc).isoformat()
    _LAST_REINDEX["files"] = int(counts.get("files", 0) or 0)
    _LAST_REINDEX["chunks"] = int(counts.get("chunks", 0) or 0)
    _LAST_REINDEX["duration_ms"] = int(duration_ms)
    _LAST_REINDEX["trigger"] = trigger
    _LAST_REINDEX["error"] = error


def record_startup_reindex(counts: dict[str, int], duration_ms: int) -> None:
    """Hook the main.py startup task can call to publish its result here.

    Public seam — main.py's `_index_product_knowledge_task` calls this
    after the indexer completes so the very first /status call after
    startup returns useful numbers.
    """
    _record(counts, duration_ms, trigger="startup", error=None)


def record_startup_failure(error: str) -> None:
    _record({"files": 0, "chunks": 0}, duration_ms=0, trigger="startup", error=error)


# ── GET status ───────────────────────────────────────────────────────


@router.get("/status")
def status() -> dict[str, Any]:
    """Snapshot of the in-process product-knowledge index state.

    Stable shape so the UI can render a small "indexed N chunks · M
    minutes ago" indicator without parsing free-form text.
    """
    return {
        **_LAST_REINDEX,
        "facts_dir_exists": _facts_dir_exists(),
    }


def _facts_dir_exists() -> bool:
    try:
        from fpulse.ai.product_knowledge import _product_facts_dir
        return _product_facts_dir().is_dir()
    except Exception:  # noqa: BLE001
        return False


# ── POST reindex ─────────────────────────────────────────────────────


@router.post("/reindex")
async def reindex(user=Depends(require_admin)) -> dict[str, Any]:
    """Re-run the product-knowledge indexer right now. Admin only.

    Returns the same shape as /status. Operators call this after editing
    `docs/product_facts/*.md` so the change takes effect without a
    backend restart. Idempotent — re-runs replace existing chunks via
    deterministic doc IDs.
    """
    import time

    try:
        from fpulse.main import app_state  # type: ignore
        embedder = app_state.get("rag_embedder")
        store = app_state.get("rag_store")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"app_state not available: {exc}",
        )

    if embedder is None or store is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "RAG embedder or vector store is not initialised on this "
                "backend. Check FPULSE_DISABLE_RAG and Ollama availability."
            ),
        )

    t0 = time.perf_counter()
    try:
        from fpulse.ai.product_knowledge import index_product_knowledge
        counts = await index_product_knowledge(
            embedder=embedder, vector_store=store,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        _record({"files": 0, "chunks": 0}, elapsed_ms, "admin", error=str(exc))
        logger.warning("product_knowledge reindex (admin) failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Reindex failed: {type(exc).__name__}: {str(exc)[:200]}",
        )

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    _record(counts, elapsed_ms, "admin", error=None)
    logger.info(
        "product_knowledge reindex by admin %s: %d chunks from %d files in %dms",
        getattr(user, "id", "?"), counts.get("chunks", 0), counts.get("files", 0), elapsed_ms,
    )
    return {**_LAST_REINDEX, "facts_dir_exists": _facts_dir_exists()}
