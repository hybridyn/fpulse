"""
SQLite-vec backed vector store for RAG documents.

Each workspace gets its own logical partition via the workspace_id column.
Schema: rag_docs(id TEXT PK, workspace_id TEXT, kind TEXT, content TEXT,
                 embedding BLOB, metadata TEXT, indexed_at TEXT)

The embedding column stores float32 vectors as raw bytes (little-endian),
which sqlite-vec reads via vec_distance_cosine().

sqlite-vec is a single-file loadable extension — no server, no sidecar.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence


def _float_list_to_bytes(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _bytes_to_float_list(raw: bytes) -> list[float]:
    n = len(raw) // 4
    return list(struct.unpack(f"<{n}f", raw))


class VectorStore:
    """Workspace-scoped vector store backed by SQLite + sqlite-vec."""

    def __init__(self, db_path: str, *, dimensions: int = 768) -> None:
        self.db_path = db_path
        self.dimensions = dimensions
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._ensure_schema()
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._conn
        assert conn is not None
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rag_docs (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB,
                metadata TEXT DEFAULT '{}',
                indexed_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rag_docs_ws_kind
            ON rag_docs(workspace_id, kind)
        """)
        conn.commit()

    def upsert(
        self,
        *,
        doc_id: str | None = None,
        workspace_id: str,
        kind: str,
        content: str,
        embedding: Sequence[float] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Insert or replace a document. Returns the doc id."""
        conn = self._get_conn()
        did = doc_id or str(uuid.uuid4())
        emb_bytes = _float_list_to_bytes(embedding) if embedding else None
        meta_json = json.dumps(metadata or {})
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT OR REPLACE INTO rag_docs
                (id, workspace_id, kind, content, embedding, metadata, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (did, workspace_id, kind, content, emb_bytes, meta_json, now),
        )
        conn.commit()
        return did

    def search(
        self,
        *,
        query_embedding: Sequence[float],
        workspace_id: str,
        kind: str | None = None,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Cosine similarity search within a workspace.

        Returns list of {id, kind, content, metadata, score} sorted by
        descending similarity. Score is 1 - cosine_distance (higher = better).

        Falls back to brute-force Python cosine when sqlite-vec extension
        is not available (tests, minimal installs).
        """
        conn = self._get_conn()
        query_bytes = _float_list_to_bytes(query_embedding)

        # Try sqlite-vec first
        try:
            return self._search_vec(conn, query_bytes, workspace_id, kind, limit, min_score)
        except sqlite3.OperationalError:
            # sqlite-vec not loaded — fall back to Python brute-force
            return self._search_python(conn, query_embedding, workspace_id, kind, limit, min_score)

    def _search_vec(
        self,
        conn: sqlite3.Connection,
        query_bytes: bytes,
        workspace_id: str,
        kind: str | None,
        limit: int,
        min_score: float,
    ) -> list[dict[str, Any]]:
        """Search using sqlite-vec's vec_distance_cosine()."""
        if kind:
            rows = conn.execute(
                """
                SELECT id, kind, content, metadata,
                       (1.0 - vec_distance_cosine(embedding, ?)) AS score
                FROM rag_docs
                WHERE workspace_id = ? AND kind = ? AND embedding IS NOT NULL
                ORDER BY score DESC
                LIMIT ?
                """,
                (query_bytes, workspace_id, kind, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, kind, content, metadata,
                       (1.0 - vec_distance_cosine(embedding, ?)) AS score
                FROM rag_docs
                WHERE workspace_id = ? AND embedding IS NOT NULL
                ORDER BY score DESC
                LIMIT ?
                """,
                (query_bytes, workspace_id, limit),
            ).fetchall()

        results = []
        for row in rows:
            score = float(row[4])
            if score >= min_score:
                results.append({
                    "id": row[0],
                    "kind": row[1],
                    "content": row[2],
                    "metadata": json.loads(row[3] or "{}"),
                    "score": round(score, 4),
                })
        return results

    def _search_python(
        self,
        conn: sqlite3.Connection,
        query_vec: Sequence[float],
        workspace_id: str,
        kind: str | None,
        limit: int,
        min_score: float,
    ) -> list[dict[str, Any]]:
        """Brute-force cosine similarity in Python (fallback)."""
        if kind:
            rows = conn.execute(
                "SELECT id, kind, content, metadata, embedding FROM rag_docs "
                "WHERE workspace_id = ? AND kind = ? AND embedding IS NOT NULL",
                (workspace_id, kind),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, kind, content, metadata, embedding FROM rag_docs "
                "WHERE workspace_id = ? AND embedding IS NOT NULL",
                (workspace_id,),
            ).fetchall()

        scored = []
        for row in rows:
            doc_vec = _bytes_to_float_list(row[4])
            score = _cosine_sim(query_vec, doc_vec)
            if score >= min_score:
                scored.append({
                    "id": row[0],
                    "kind": row[1],
                    "content": row[2],
                    "metadata": json.loads(row[3] or "{}"),
                    "score": round(score, 4),
                })
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def count(self, workspace_id: str | None = None) -> int:
        conn = self._get_conn()
        if workspace_id:
            row = conn.execute(
                "SELECT COUNT(*) FROM rag_docs WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM rag_docs").fetchone()
        return row[0] if row else 0

    def delete_by_kind(self, workspace_id: str, kind: str) -> int:
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM rag_docs WHERE workspace_id = ? AND kind = ?",
            (workspace_id, kind),
        )
        conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


def _cosine_sim(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
