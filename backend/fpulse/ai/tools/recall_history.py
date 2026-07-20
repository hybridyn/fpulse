"""recall_history — read-only RAG retrieval over workspace history & docs.

Returns relevant chunks from execution failures, pipeline definitions,
catalog entries, and docs. Use when the user asks open-ended history
questions like "what failed last week" or "what does pipeline X do" or
"which connector should I use for Snowflake."

Workspace-scoped via the existing tenant isolation. Embeddings via local
Ollama nomic-embed-text. Disable entirely with FPULSE_DISABLE_RAG=1.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    workspace_id = ctx.workspace_id or ctx.tenant_id or "default"
    query = (inputs.get("query") or "").strip()
    kind = inputs.get("kind") or None
    limit = max(1, min(int(inputs.get("limit") or 5), 10))

    # Default failure-kind retrieval to caller's environment so DEV users
    # don't see PROD failure context (and vice versa). Pipeline / catalog /
    # doc kinds are env-agnostic and never filtered. Pass environment="all"
    # to opt out for cross-env asks.
    env_filter = (inputs.get("environment") or ctx.environment or "").strip().lower()
    if env_filter == "all":
        env_filter = ""

    if not query:
        return {"chunks": [], "total": 0, "workspace_id": workspace_id, "environment_filter": env_filter or "all"}

    chunks: list[dict[str, Any]] = []
    try:
        from fpulse.main import app_state  # type: ignore
        embedder = app_state.get("rag_embedder")
        store = app_state.get("rag_store")
        if embedder is not None and store is not None:
            from fpulse.ai.rag.retrieve import retrieve
            raw_chunks = await retrieve(
                query=query,
                workspace_id=workspace_id,
                embedder=embedder,
                vector_store=store,
                kind=kind,
                # Fetch a few extra so post-filter still has room to return `limit`
                limit=limit * 2 if env_filter else limit,
            )
            # Post-filter failure chunks by env metadata.
            for c in raw_chunks:
                if env_filter and c.get("kind") == "failure":
                    chunk_env = ((c.get("metadata") or {}).get("environment") or "dev").lower()
                    if chunk_env != env_filter:
                        continue
                chunks.append(c)
                if len(chunks) >= limit:
                    break
    except Exception:
        chunks = []

    return {
        "chunks": chunks,
        "total": len(chunks),
        "workspace_id": workspace_id,
        "environment_filter": env_filter or "all",
    }


DEFINITION = ToolDefinition(
    name="recall_history",
    tier=ToolTier.READ,
    description=(
        "Search workspace history and docs for relevant context: failed "
        "executions (last 30d), pipeline definitions, catalog (connectors, "
        "step types), and product docs. Returns top-k chunks with relevance "
        "scores. Use for open-ended questions like 'what failed last week', "
        "'what does this pipeline do', or 'which connector should I use'. "
        "Filter by kind: 'failure', 'pipeline', 'doc', or 'catalog'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The natural-language question or search phrase.",
            },
            "kind": {
                "type": "string",
                "enum": ["failure", "pipeline", "doc", "catalog"],
                "description": "Optional: restrict to one source kind.",
            },
            "limit": {
                "type": "integer",
                "description": "Max chunks to return (1-10, default 5).",
            },
            "environment": {
                "type": "string",
                "enum": ["dev", "prod", "all"],
                "description": "Filter failure-kind chunks by env. Defaults to caller's env.",
            },
        },
        "required": ["query"],
    },
    output_schema={
        "chunks": "list",
        "total": "int",
        "workspace_id": "str",
        "environment_filter": "str",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["rag", "read", "search"],
)
