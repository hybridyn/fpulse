"""
RAG (Retrieval-Augmented Generation) layer for the F-Pulse Copilot.

Augments the agent's answers with workspace-scoped retrieval over:
  - execution logs (failures in last 30d)
  - pipeline definitions (workflow IR)
  - catalog entries (connectors, step-types)
  - docs/*.md files

All retrieval is workspace-scoped via tenant isolation.
Embeddings run locally via Ollama nomic-embed-text; retrieved chunks
are sent in-prompt to whichever LLM provider is configured.

Disable entirely via FPULSE_DISABLE_RAG=1.
"""

from fpulse.ai.rag.embedder import Embedder
from fpulse.ai.rag.store import VectorStore
from fpulse.ai.rag.indexer import RAGIndexer
from fpulse.ai.rag.retrieve import retrieve

__all__ = ["Embedder", "VectorStore", "RAGIndexer", "retrieve"]
