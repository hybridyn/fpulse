"""web_search tool — read-only, OPT-IN.

Searches the web via a configured provider so the Copilot can discover a
vendor's API/docs pages, then read the best hit with ``web_fetch``. Registered
only when ``FPULSE_AI_WEB_ACCESS=1``; the provider + key are set separately
(see ``fpulse.ai.web.search``). With no provider configured the tool returns a
clear "not configured" result instead of pretending to search.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = (inputs.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    count = int(inputs.get("count") or 5)

    import anyio

    from fpulse.ai.web.search import WebSearchNotConfigured, search_web

    def _run() -> list[dict]:
        return search_web(query, count=count)

    try:
        results = await anyio.to_thread.run_sync(_run)
    except WebSearchNotConfigured as exc:
        return {"ok": False, "configured": False, "query": query,
                "results": [], "count": 0, "error": str(exc)}
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 — provider/network errors → clean result
        return {"ok": False, "configured": True, "query": query,
                "results": [], "count": 0, "error": f"{type(exc).__name__}: {exc}"}

    return {"ok": True, "configured": True, "query": query,
            "results": results, "count": len(results), "error": ""}


DEFINITION = ToolDefinition(
    name="web_search",
    tier=ToolTier.READ,
    description=(
        "Search the web for pages matching a query and return titles + URLs + "
        "snippets — use it to find a vendor's API or developer-docs page, then read "
        "the best result with web_fetch. Only available when the operator has enabled "
        "Copilot web access AND configured a search provider; if the provider isn't "
        "configured this returns configured=false with instructions, so tell the user "
        "to paste the spec/docs URL instead. Note: many vendors (e.g. FactoHR) don't "
        "publish a public API spec, so a search may find no usable spec."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query, e.g. 'Acme HR REST API OpenAPI spec'.",
            },
            "count": {
                "type": "integer",
                "description": "Max results to return (default 5, max 10).",
                "default": 5,
            },
        },
        "required": ["query"],
    },
    output_schema={
        "ok": "bool",
        "configured": "bool",
        "query": "str",
        "results": "list",
        "count": "int",
        "error": "str",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["web", "search", "read"],
)
