"""
RAG indexer — builds the vector corpus from live F-Pulse data.

Sources indexed:
  1. execution_logs with status=failed/error in last 30 days
  2. workflow definitions (pipeline IR as plain-English summaries)
  3. catalog entries (connectors, step-types)
  4. docs/*.md files

Runs on-demand via POST /api/ai/rag/reindex and on a daily scheduled job
(03:00 UTC). Each run is workspace-scoped: the caller provides the
workspace_id and only that workspace's data is (re-)indexed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from fpulse.ai.rag.embedder import Embedder
from fpulse.ai.rag.store import VectorStore
from fpulse.ai.sanitize import sanitize_for_llm

logger = logging.getLogger("fpulse.ai.rag.indexer")

_FAILURE_LOOKBACK_DAYS = 30
_MAX_CONTENT_CHARS = 1500


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class RAGIndexer:
    """Iterates F-Pulse data sources and writes embeddings into VectorStore."""

    def __init__(
        self,
        *,
        embedder: Embedder,
        vector_store: VectorStore,
    ) -> None:
        self.embedder = embedder
        self.store = vector_store

    async def index_workspace(
        self,
        workspace_id: str,
        *,
        app_state: dict[str, Any] | None = None,
        docs_dir: str | None = None,
    ) -> dict[str, int]:
        """Index all sources for a workspace. Returns {source: count} indexed."""
        counts: dict[str, int] = {}

        if not await self.embedder.is_available():
            logger.warning("Embedding provider not available; skipping RAG indexing")
            return counts

        t0 = time.monotonic()

        counts["failures"] = await self._index_failures(workspace_id, app_state)
        counts["pipelines"] = await self._index_pipelines(workspace_id, app_state)
        counts["catalog"] = await self._index_catalog(workspace_id, app_state)
        counts["docs"] = await self._index_docs(workspace_id, docs_dir)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        total = sum(counts.values())
        logger.info(
            "RAG indexing complete for workspace=%s: %d docs in %dms %s",
            workspace_id, total, elapsed_ms, counts,
        )
        return counts

    async def _index_failures(
        self, workspace_id: str, app_state: dict | None
    ) -> int:
        """Index failed execution logs from the last 30 days."""
        if not app_state:
            return 0
        exec_store = app_state.get("execution_store")
        if exec_store is None:
            return 0

        count = 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=_FAILURE_LOOKBACK_DAYS)
        try:
            rows = exec_store.list_all(limit=500, workspace_id=workspace_id)
            for r in rows:
                status = (r.get("status") or "").lower()
                if status not in ("failed", "error"):
                    continue
                started = r.get("started_at", "")
                if started and started < cutoff.isoformat():
                    continue

                content = self._build_failure_content(r)
                sanitized = sanitize_for_llm(content, max_chars=_MAX_CONTENT_CHARS)
                text = str(sanitized.payload)

                embedding = await self.embedder.embed(text)
                if embedding is None:
                    continue

                doc_id = f"failure:{workspace_id}:{r.get('id', '')}"
                self.store.upsert(
                    doc_id=doc_id,
                    workspace_id=workspace_id,
                    kind="failure",
                    content=text,
                    embedding=embedding,
                    metadata={
                        "execution_id": r.get("id", ""),
                        "pipeline_name": r.get("workflow_name", ""),
                        "status": status,
                        # env metadata is optional — present only on Plus where
                        # DEV/PROD are real environments. OSS rows omit it
                        # (treated as 'dev' at retrieval time).
                        "environment": (r.get("environment") or "dev").lower(),
                        "started_at": started,
                        "error_snippet": (r.get("error") or "")[:200],
                    },
                )
                count += 1
        except Exception as e:
            logger.warning("Failed to index execution failures: %s", e)
        return count

    def _build_failure_content(self, row: dict) -> str:
        parts = [
            f"Pipeline '{row.get('workflow_name', 'unknown')}' failed.",
            f"Status: {row.get('status', '')}",
            f"Started: {row.get('started_at', '')}",
            f"Duration: {row.get('duration_ms', 0)}ms",
        ]
        error = row.get("error", "")
        if error:
            parts.append(f"Error: {error[:500]}")
        trigger = row.get("trigger", "")
        if trigger:
            parts.append(f"Trigger: {trigger}")
        return "\n".join(parts)

    async def _index_pipelines(
        self, workspace_id: str, app_state: dict | None
    ) -> int:
        """Index pipeline definitions as plain-text summaries."""
        if not app_state:
            return 0
        store = app_state.get("store")
        if store is None:
            return 0

        count = 0
        try:
            workflows = store.list_workflows(workspace_id=workspace_id) or []
            for wf in workflows:
                content = self._build_pipeline_content(wf)
                sanitized = sanitize_for_llm(content, max_chars=_MAX_CONTENT_CHARS)
                text = str(sanitized.payload)

                embedding = await self.embedder.embed(text)
                if embedding is None:
                    continue

                wf_id = wf.get("id", "")
                doc_id = f"pipeline:{workspace_id}:{wf_id}"
                self.store.upsert(
                    doc_id=doc_id,
                    workspace_id=workspace_id,
                    kind="pipeline",
                    content=text,
                    embedding=embedding,
                    metadata={
                        "pipeline_id": wf_id,
                        "pipeline_name": wf.get("name", ""),
                    },
                )
                count += 1
        except Exception as e:
            logger.warning("Failed to index pipelines: %s", e)
        return count

    def _build_pipeline_content(self, wf: dict) -> str:
        parts = [
            f"Pipeline: {wf.get('name', 'untitled')}",
            f"Description: {wf.get('description', 'No description')}",
        ]
        steps = wf.get("steps") or wf.get("nodes") or []
        if steps:
            step_labels = [s.get("label") or s.get("type", "?") for s in steps[:20]]
            parts.append(f"Steps ({len(steps)}): {', '.join(step_labels)}")
        return "\n".join(parts)

    async def _index_catalog(
        self, workspace_id: str, app_state: dict | None
    ) -> int:
        """Index catalog connectors and step-types."""
        count = 0
        try:
            from fpulse.catalog import get_catalog
            catalog = get_catalog()

            for entry in catalog.get("connectors", []):
                text = (
                    f"Connector: {entry.get('name', '')}\n"
                    f"Category: {entry.get('category', '')}\n"
                    f"Description: {entry.get('description', '')}"
                )
                embedding = await self.embedder.embed(text)
                if embedding is None:
                    continue
                doc_id = f"catalog:connector:{entry.get('id', entry.get('name', ''))}"
                self.store.upsert(
                    doc_id=doc_id,
                    workspace_id=workspace_id,
                    kind="catalog",
                    content=text,
                    embedding=embedding,
                    metadata={"catalog_type": "connector", "name": entry.get("name", "")},
                )
                count += 1

            for entry in catalog.get("step_types", []):
                text = (
                    f"Step type: {entry.get('name', '')}\n"
                    f"Category: {entry.get('category', '')}\n"
                    f"Description: {entry.get('description', '')}"
                )
                embedding = await self.embedder.embed(text)
                if embedding is None:
                    continue
                doc_id = f"catalog:step:{entry.get('id', entry.get('name', ''))}"
                self.store.upsert(
                    doc_id=doc_id,
                    workspace_id=workspace_id,
                    kind="catalog",
                    content=text,
                    embedding=embedding,
                    metadata={"catalog_type": "step_type", "name": entry.get("name", "")},
                )
                count += 1
        except Exception as e:
            logger.warning("Failed to index catalog: %s", e)
        return count

    async def _index_docs(self, workspace_id: str, docs_dir: str | None) -> int:
        """Index markdown files from docs/."""
        if not docs_dir or not os.path.isdir(docs_dir):
            return 0

        count = 0
        try:
            for fname in os.listdir(docs_dir):
                if not fname.endswith(".md"):
                    continue
                fpath = os.path.join(docs_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        raw = f.read(_MAX_CONTENT_CHARS * 2)
                except Exception:
                    continue

                sanitized = sanitize_for_llm(raw, max_chars=_MAX_CONTENT_CHARS)
                text = str(sanitized.payload)

                embedding = await self.embedder.embed(text)
                if embedding is None:
                    continue

                doc_id = f"doc:{workspace_id}:{_content_hash(fname)}"
                self.store.upsert(
                    doc_id=doc_id,
                    workspace_id=workspace_id,
                    kind="doc",
                    content=text,
                    embedding=embedding,
                    metadata={"filename": fname},
                )
                count += 1
        except Exception as e:
            logger.warning("Failed to index docs: %s", e)
        return count
