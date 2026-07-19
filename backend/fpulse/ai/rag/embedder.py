"""
Thin wrapper around Ollama /api/embeddings for local embedding generation.

Default model: nomic-embed-text (768-dim, fits sqlite-vec).
Override via FPULSE_EMBEDDING_MODEL env var.

Falls through to None when no embedding provider is reachable,
which the caller treats as "RAG disabled for this request."
"""

from __future__ import annotations

import os
from typing import Sequence

import httpx

DEFAULT_MODEL = "nomic-embed-text"
# 2026-05-22: IPv4 default — see api/ollama.py:_ollama_url for the Windows
# `localhost`→IPv6 issue this avoids.
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
_TIMEOUT_S = 30.0


class Embedder:
    """Generate embeddings via Ollama's /api/embeddings endpoint."""

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = model or os.environ.get("FPULSE_EMBEDDING_MODEL", DEFAULT_MODEL)
        self.base_url = (
            base_url
            or os.environ.get("OLLAMA_URL", "").rstrip("/")
            or DEFAULT_OLLAMA_URL
        )
        self._url = f"{self.base_url}/api/embeddings"

    async def embed(self, text: str) -> list[float] | None:
        """Return embedding vector for a single text, or None on failure."""
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                resp = await client.post(
                    self._url,
                    json={"model": self.model, "prompt": text},
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("embedding")
        except Exception:
            return None

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float] | None]:
        """Embed multiple texts sequentially. Returns list aligned with input."""
        results: list[list[float] | None] = []
        for text in texts:
            results.append(await self.embed(text))
        return results

    async def is_available(self) -> bool:
        """Check if the embedding endpoint is reachable and the model exists."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    self._url,
                    json={"model": self.model, "prompt": "test"},
                )
                return resp.status_code == 200
        except Exception:
            return False
