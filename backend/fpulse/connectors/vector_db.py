"""
Vector DB source and sink — Pinecone, Weaviate, Qdrant, Chroma, pgvector.

Source: similarity-search a query vector (or text embedded on the fly) and
        return the top-K matches as rows with id, score, metadata.

Sink:   take upstream rows (with `id`, optional `text`/`vector`, optional
        metadata fields) and upsert them into the chosen vector store. If
        rows have `text` but no `vector`, an embedding model can be invoked
        (OpenAI, Cohere, or local sentence-transformers) to compute vectors.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for type annotations on _rows_to_relation
# and execute() returns.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register


_PROVIDERS = ["pinecone", "weaviate", "qdrant", "chroma", "pgvector"]


# ─────────────────────────── Embedding helpers ───────────────────────────

def _embed_texts(texts: list[str], provider: str, model: str, api_key: str) -> list[list[float]]:
    if not texts:
        return []
    if provider == "openai":
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError("openai package missing. pip install openai") from e
        client = OpenAI(api_key=api_key) if api_key else OpenAI()
        resp = client.embeddings.create(model=model or "text-embedding-3-small", input=texts)
        return [d.embedding for d in resp.data]
    if provider == "cohere":
        try:
            import cohere
        except ImportError as e:
            raise RuntimeError("cohere package missing. pip install cohere") from e
        co = cohere.Client(api_key)
        resp = co.embed(texts=texts, model=model or "embed-english-v3.0", input_type="search_document")
        return resp.embeddings
    if provider == "sentence_transformers":
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError("sentence-transformers missing. pip install sentence-transformers") from e
        m = SentenceTransformer(model or "all-MiniLM-L6-v2")
        return m.encode(texts).tolist()
    raise RuntimeError(f"Unknown embedding provider '{provider}'")


# ─────────────────────────── Provider adapters ───────────────────────────

def _query_pinecone(cfg, vector, top_k, namespace):
    try:
        from pinecone import Pinecone
    except ImportError as e:
        raise RuntimeError("pinecone-client missing. pip install pinecone-client") from e
    pc = Pinecone(api_key=cfg["api_key"])
    index = pc.Index(cfg["index"])
    result = index.query(vector=vector, top_k=top_k, namespace=namespace, include_metadata=True)
    return [
        {"id": m.id, "score": m.score, **(m.metadata or {})}
        for m in result.matches
    ]


def _upsert_pinecone(cfg, items, namespace):
    from pinecone import Pinecone
    pc = Pinecone(api_key=cfg["api_key"])
    index = pc.Index(cfg["index"])
    vectors = [{"id": str(it["id"]), "values": it["vector"], "metadata": it.get("metadata", {})} for it in items]
    index.upsert(vectors=vectors, namespace=namespace)
    return len(vectors)


def _query_weaviate(cfg, vector, top_k, class_name):
    try:
        import weaviate
    except ImportError as e:
        raise RuntimeError("weaviate-client missing. pip install weaviate-client") from e
    client = weaviate.Client(url=cfg["url"], auth_client_secret=weaviate.AuthApiKey(cfg["api_key"]) if cfg.get("api_key") else None)
    result = (
        client.query.get(class_name, ["_additional { id distance }"])
        .with_near_vector({"vector": vector}).with_limit(top_k).do()
    )
    objs = result.get("data", {}).get("Get", {}).get(class_name, [])
    return [{"id": o["_additional"]["id"], "score": 1 - o["_additional"].get("distance", 0)} for o in objs]


def _upsert_weaviate(cfg, items, class_name):
    import weaviate
    client = weaviate.Client(url=cfg["url"], auth_client_secret=weaviate.AuthApiKey(cfg["api_key"]) if cfg.get("api_key") else None)
    with client.batch as batch:
        for it in items:
            batch.add_data_object(
                data_object=it.get("metadata", {}),
                class_name=class_name,
                uuid=str(it["id"]),
                vector=it["vector"],
            )
    return len(items)


def _query_qdrant(cfg, vector, top_k, collection):
    try:
        from qdrant_client import QdrantClient
    except ImportError as e:
        raise RuntimeError("qdrant-client missing. pip install qdrant-client") from e
    client = QdrantClient(url=cfg.get("url"), api_key=cfg.get("api_key"))
    hits = client.search(collection_name=collection, query_vector=vector, limit=top_k)
    return [{"id": str(h.id), "score": h.score, **(h.payload or {})} for h in hits]


def _upsert_qdrant(cfg, items, collection):
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct
    client = QdrantClient(url=cfg.get("url"), api_key=cfg.get("api_key"))
    points = [PointStruct(id=it["id"], vector=it["vector"], payload=it.get("metadata", {})) for it in items]
    client.upsert(collection_name=collection, points=points)
    return len(points)


def _query_chroma(cfg, vector, top_k, collection):
    try:
        import chromadb
    except ImportError as e:
        raise RuntimeError("chromadb missing. pip install chromadb") from e
    client = chromadb.HttpClient(host=cfg.get("host", "localhost"), port=int(cfg.get("port", 8000)))
    coll = client.get_collection(collection)
    result = coll.query(query_embeddings=[vector], n_results=top_k)
    ids = result.get("ids", [[]])[0]
    distances = result.get("distances", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0] or [{}] * len(ids)
    return [{"id": i, "score": 1 - d, **(m or {})} for i, d, m in zip(ids, distances, metadatas)]


def _upsert_chroma(cfg, items, collection):
    import chromadb
    client = chromadb.HttpClient(host=cfg.get("host", "localhost"), port=int(cfg.get("port", 8000)))
    coll = client.get_or_create_collection(collection)
    coll.upsert(
        ids=[str(it["id"]) for it in items],
        embeddings=[it["vector"] for it in items],
        metadatas=[it.get("metadata", {}) for it in items],
    )
    return len(items)


def _query_pgvector(cfg, vector, top_k, table):
    try:
        import psycopg2
    except ImportError as e:
        raise RuntimeError("psycopg2 missing. pip install psycopg2-binary") from e
    conn = psycopg2.connect(
        host=cfg["host"], port=int(cfg.get("port", 5432)), dbname=cfg["database"],
        user=cfg["user"], password=cfg["password"],
    )
    try:
        cur = conn.cursor()
        vec_literal = "[" + ",".join(str(x) for x in vector) + "]"
        cur.execute(
            f"SELECT id, 1 - (embedding <=> %s::vector) AS score, metadata "
            f"FROM {table} ORDER BY embedding <=> %s::vector LIMIT %s",
            (vec_literal, vec_literal, top_k),
        )
        rows = cur.fetchall()
        return [{"id": r[0], "score": float(r[1]), **(r[2] or {})} for r in rows]
    finally:
        conn.close()


def _upsert_pgvector(cfg, items, table):
    import psycopg2
    conn = psycopg2.connect(
        host=cfg["host"], port=int(cfg.get("port", 5432)), dbname=cfg["database"],
        user=cfg["user"], password=cfg["password"],
    )
    try:
        cur = conn.cursor()
        for it in items:
            vec_literal = "[" + ",".join(str(x) for x in it["vector"]) + "]"
            cur.execute(
                f"INSERT INTO {table} (id, embedding, metadata) VALUES (%s, %s::vector, %s) "
                f"ON CONFLICT (id) DO UPDATE SET embedding = EXCLUDED.embedding, metadata = EXCLUDED.metadata",
                (str(it["id"]), vec_literal, json.dumps(it.get("metadata", {}))),
            )
        conn.commit()
        return len(items)
    finally:
        conn.close()


_QUERY_FNS = {
    "pinecone": _query_pinecone,
    "weaviate": _query_weaviate,
    "qdrant": _query_qdrant,
    "chroma": _query_chroma,
    "pgvector": _query_pgvector,
}

_UPSERT_FNS = {
    "pinecone": _upsert_pinecone,
    "weaviate": _upsert_weaviate,
    "qdrant": _upsert_qdrant,
    "chroma": _upsert_chroma,
    "pgvector": _upsert_pgvector,
}


def _rows_to_relation(conn: duckdb.DuckDBPyConnection, rows: list[dict]) -> duckdb.DuckDBPyRelation:
    if not rows:
        return conn.sql("SELECT NULL AS empty WHERE false")
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")
        return conn.sql(f"SELECT * FROM read_json_auto('{path}', format='newline_delimited')")
    except Exception:
        return conn.sql("SELECT NULL AS empty WHERE false")


# ─────────────────────────── Nodes ───────────────────────────

@register(StepType.VECTOR_SOURCE)
class VectorSourceNode(BaseNode):
    """Similarity-search a vector DB and return top-K matches as rows."""

    display_name = "Vector DB Source"
    category = "source"
    description = "Query Pinecone/Weaviate/Qdrant/Chroma/pgvector for nearest neighbors"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        provider = self.params.get("provider")
        if provider not in _QUERY_FNS:
            raise ValueError(f"Vector Source: provider must be one of {_PROVIDERS}")

        cfg = {k: v for k, v in self.params.items() if v not in (None, "") and not k.startswith("_")}
        top_k = int(self.params.get("top_k", 10))
        namespace_or_collection = (
            self.params.get("namespace")
            or self.params.get("collection")
            or self.params.get("class_name")
            or self.params.get("table")
        )

        # Either an explicit vector, or text → embed
        vector = self.params.get("query_vector")
        if isinstance(vector, str):
            try:
                vector = json.loads(vector)
            except Exception:
                vector = None
        if not vector:
            text = self.params.get("query_text")
            if not text:
                raise ValueError("Vector Source: either query_vector or query_text required")
            vector = _embed_texts(
                [text],
                provider=self.params.get("embedding_provider", "openai"),
                model=self.params.get("embedding_model", ""),
                api_key=self.params.get("embedding_api_key", ""),
            )[0]

        rows = _QUERY_FNS[provider](cfg, vector, top_k, namespace_or_collection)
        return _rows_to_relation(ctx.conn, rows)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"provider": "qdrant", "top_k": 10, "embedding_provider": "openai"}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "provider", "type": "string", "label": "Vector DB", "required": True,
             "options": [{"value": p, "label": p.title()} for p in _PROVIDERS]},
            {"name": "url", "type": "string", "label": "Endpoint URL"},
            {"name": "api_key", "type": "string", "label": "API Key", "secret": True},
            {"name": "index", "type": "string", "label": "Index (Pinecone)"},
            {"name": "collection", "type": "string", "label": "Collection (Qdrant/Chroma)"},
            {"name": "class_name", "type": "string", "label": "Class (Weaviate)"},
            {"name": "table", "type": "string", "label": "Table (pgvector)"},
            {"name": "namespace", "type": "string", "label": "Namespace"},
            {"name": "top_k", "type": "number", "label": "Top K", "default": 10},
            {"name": "query_text", "type": "string", "label": "Query text (auto-embed)"},
            {"name": "query_vector", "type": "string", "label": "Query vector (JSON array)"},
            {"name": "embedding_provider", "type": "string", "label": "Embedding provider",
             "options": [{"value": x, "label": x} for x in ["openai", "cohere", "sentence_transformers"]]},
            {"name": "embedding_model", "type": "string", "label": "Embedding model"},
            {"name": "embedding_api_key", "type": "string", "label": "Embedding API key", "secret": True},
        ]


@register(StepType.VECTOR_SINK)
class VectorSinkNode(BaseNode):
    """Upsert upstream rows into a vector DB. Auto-embeds text columns if needed."""

    display_name = "Vector DB Sink"
    category = "output"
    description = "Embed and upsert rows to Pinecone/Weaviate/Qdrant/Chroma/pgvector"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        upstream_ids = self.params.get("_input_step_ids", [])
        if not upstream_ids:
            raise ValueError("Vector Sink: requires an upstream node")
        rel = ctx.get_input(upstream_ids[0])
        if rel is None:
            raise ValueError("Vector Sink: upstream produced no relation")

        provider = self.params.get("provider")
        if provider not in _UPSERT_FNS:
            raise ValueError(f"Vector Sink: provider must be one of {_PROVIDERS}")

        cols = rel.columns
        rows = rel.fetchall()
        records = [dict(zip(cols, r)) for r in rows]

        id_col = self.params.get("id_column", "id")
        text_col = self.params.get("text_column", "text")
        vector_col = self.params.get("vector_column", "vector")

        # Build items list
        items: list[dict] = []
        texts_needing_embed: list[tuple[int, str]] = []
        for i, rec in enumerate(records):
            item_id = rec.get(id_col) or i
            vec = rec.get(vector_col)
            if isinstance(vec, str):
                try:
                    vec = json.loads(vec)
                except Exception:
                    vec = None
            metadata = {k: v for k, v in rec.items() if k not in (id_col, vector_col, text_col)}
            items.append({"id": item_id, "vector": vec, "metadata": metadata})
            if not vec:
                txt = rec.get(text_col, "")
                if txt:
                    texts_needing_embed.append((i, str(txt)))

        if texts_needing_embed:
            embedded = _embed_texts(
                [t for _, t in texts_needing_embed],
                provider=self.params.get("embedding_provider", "openai"),
                model=self.params.get("embedding_model", ""),
                api_key=self.params.get("embedding_api_key", ""),
            )
            for (idx, _), vec in zip(texts_needing_embed, embedded):
                items[idx]["vector"] = vec

        # Drop items without vectors
        items = [it for it in items if it.get("vector")]

        cfg = {k: v for k, v in self.params.items() if v not in (None, "") and not k.startswith("_")}
        target = (
            self.params.get("namespace")
            or self.params.get("collection")
            or self.params.get("class_name")
            or self.params.get("table")
            or ""
        )
        count = _UPSERT_FNS[provider](cfg, items, target)

        # Echo the upstream relation for downstream observability
        return rel

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"provider": "qdrant", "id_column": "id", "text_column": "text", "vector_column": "vector",
                "embedding_provider": "openai"}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "provider", "type": "string", "label": "Vector DB", "required": True,
             "options": [{"value": p, "label": p.title()} for p in _PROVIDERS]},
            {"name": "url", "type": "string", "label": "Endpoint URL"},
            {"name": "api_key", "type": "string", "label": "API Key", "secret": True},
            {"name": "index", "type": "string", "label": "Index"},
            {"name": "collection", "type": "string", "label": "Collection"},
            {"name": "class_name", "type": "string", "label": "Class"},
            {"name": "table", "type": "string", "label": "Table (pgvector)"},
            {"name": "namespace", "type": "string", "label": "Namespace"},
            {"name": "id_column", "type": "string", "label": "ID column", "default": "id"},
            {"name": "text_column", "type": "string", "label": "Text column", "default": "text"},
            {"name": "vector_column", "type": "string", "label": "Vector column", "default": "vector"},
            {"name": "embedding_provider", "type": "string", "label": "Embedder",
             "options": [{"value": x, "label": x} for x in ["openai", "cohere", "sentence_transformers"]]},
            {"name": "embedding_model", "type": "string", "label": "Embedding model"},
            {"name": "embedding_api_key", "type": "string", "label": "Embedding API key", "secret": True},
        ]
