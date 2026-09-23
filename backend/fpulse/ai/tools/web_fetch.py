"""web_fetch tool — read-only, OPT-IN.

Fetches a PUBLIC URL and returns the page text, so the Copilot can read a
vendor's API docs or pull an OpenAPI/JSON document the user names. Registered
only when ``FPULSE_AI_WEB_ACCESS=1`` (see ``fpulse.ai.web``); F-Pulse OSS ships
with no web access for the agent.

Guardrails:
  - **SSRF-hardened.** The URL and every redirect go through the shared SSRF
    check; private/loopback/metadata hosts are blocked unless the operator sets
    ``FPULSE_AI_WEB_ALLOW_PRIVATE=1``.
  - **Size-capped** (1 MB) so a huge page can't blow up context or memory.
  - **Read-only.** GET only; no request body, no state change.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    url = (inputs.get("url") or "").strip()
    if not url:
        raise ValueError("url is required")

    import anyio

    from fpulse.ai.web.fetch import fetch_url_text
    from fpulse.security.ssrf import SsrfBlockedError

    try:
        result = await anyio.to_thread.run_sync(fetch_url_text, url)
    except SsrfBlockedError as exc:
        return {
            "ok": False,
            "url": url,
            "final_url": url,
            "status": 0,
            "content_type": "",
            "text": "",
            "bytes": 0,
            "truncated": False,
            "error": f"blocked: {exc}",
        }
    except Exception as exc:  # noqa: BLE001 — network errors surface as a clean result
        return {
            "ok": False,
            "url": url,
            "final_url": url,
            "status": 0,
            "content_type": "",
            "text": "",
            "bytes": 0,
            "truncated": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "ok": 200 <= result["status"] < 400,
        "url": result["url"],
        "final_url": result["final_url"],
        "status": result["status"],
        "content_type": result["content_type"],
        "text": result["text"],
        "bytes": result["bytes"],
        "truncated": result["truncated"],
        "error": "",
    }


DEFINITION = ToolDefinition(
    name="web_fetch",
    tier=ToolTier.READ,
    description=(
        "Fetch a PUBLIC web URL and return its text — use it to read a vendor's "
        "API documentation page or pull an OpenAPI/JSON document the user points "
        "you to. Only available when the operator has enabled Copilot web access. "
        "Private/loopback/internal hosts are blocked. GET only; response is capped "
        "at 1 MB. After fetching a spec you can pass its text to "
        "draft_connector_from_openapi."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The public http(s) URL to fetch.",
            },
        },
        "required": ["url"],
    },
    output_schema={
        "ok": "bool",
        "url": "str",
        "final_url": "str",
        "status": "int",
        "content_type": "str",
        "text": "str",
        "bytes": "int",
        "truncated": "bool",
        "error": "str",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["web", "fetch", "read"],
)
