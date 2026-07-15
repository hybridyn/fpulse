"""
Model Context Protocol (MCP) endpoint — JSON-RPC dispatcher.

Exposes F-Pulse's agent tool registry to MCP-compatible clients (Claude
Desktop, Cursor, Cline, Continue, Zed) so they can drive F-Pulse without
a custom integration. The same RBAC + tool-tier matrix from the internal
agent loop applies — external clients can't escalate.

OSS scope guardrails:
  - Only READ-tier tools are exposed by default. Write tools require an
    explicit `MCP_ALLOW_WRITE_TOOLS=1` env var because external clients
    can't render the confirmation cards we use internally.
  - Auth: optional Bearer token (mirrors the rest of the OSS API). When
    present, request runs as that user; absent, runs as anonymous in
    workspace 'default' — same as the rest of OSS local-dev.
  - JSON-RPC 2.0 over a single POST endpoint (most common MCP transport).
    SSE / stdio transports are upstream concerns — clients can wrap this.

Reference: https://modelcontextprotocol.io/specification
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Request

from fpulse.ai.tools import default_registry, register_initial_tools
from fpulse.ai.tools.base import ToolContext, ToolTier
from fpulse.ai.normalize import normalize_tool_output, SchemaError
from fpulse.ai.rbac import authorize_tool_call
from fpulse.ai.sanitize import sanitize_for_llm
from fpulse.auth.deps import current_user_optional, current_workspace_id

logger = logging.getLogger("fpulse.api.mcp")

router = APIRouter(prefix="/api/v1/mcp", tags=["mcp"])

# MCP spec version this server implements. Clients use this to feature-detect.
_MCP_PROTOCOL_VERSION = "2024-11-05"

_SERVER_INFO = {
    "name": "fpulse-mcp",
    "version": "1.0.0",
    "title": "F-Pulse Pipeline Agent",
    "description": (
        "F-Pulse exposes its data-pipeline read tools (list_pipelines, "
        "summarize_pipeline, query_metrics, etc.) over MCP so external AI "
        "clients can answer 'what's in my pipeline / what failed / what's "
        "running' without a custom integration."
    ),
}


def _allow_write_tools() -> bool:
    """Write tools off by default. Toggle via env var for power users."""
    return os.environ.get("MCP_ALLOW_WRITE_TOOLS", "").strip().lower() in ("1", "true", "yes")


def _allowed_tiers() -> tuple[ToolTier, ...]:
    if _allow_write_tools():
        return (ToolTier.READ, ToolTier.SAFE_WRITE)
    return (ToolTier.READ,)


def _ok(rpc_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _err(rpc_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": err}


# JSON-RPC 2.0 standard error codes
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


@router.post("")
@router.post("/")
async def mcp_dispatch(request: Request) -> dict[str, Any]:
    """Single JSON-RPC POST endpoint. Dispatches by `method`.

    Supported methods:
      - initialize         — handshake; returns server capabilities
      - notifications/initialized — client confirms init complete (no response body)
      - tools/list         — list F-Pulse read tools in MCP format
      - tools/call         — execute one tool with the supplied arguments
      - ping               — liveness check
    """
    try:
        body = await request.json()
    except Exception:
        return _err(None, _PARSE_ERROR, "Parse error: body is not valid JSON")

    if not isinstance(body, dict):
        return _err(None, _INVALID_REQUEST, "Request must be a JSON object")

    rpc_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}

    if body.get("jsonrpc") != "2.0":
        return _err(rpc_id, _INVALID_REQUEST, "Only JSON-RPC 2.0 is supported")

    register_initial_tools()  # idempotent

    # ── Method dispatch ─────────────────────────────────────────────────
    if method == "initialize":
        # Handshake. Return server info + capabilities so the client knows
        # what operations are supported.
        return _ok(rpc_id, {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": _SERVER_INFO,
        })

    if method == "ping":
        return _ok(rpc_id, {})

    if method == "notifications/initialized":
        # Client telling us init is done. No response body required;
        # we still return a 200 so the HTTP layer is happy.
        return {}

    if method == "tools/list":
        return _ok(rpc_id, {
            "tools": _list_tools_for_mcp(),
        })

    if method == "tools/call":
        if not isinstance(params, dict):
            return _err(rpc_id, _INVALID_PARAMS, "params must be an object")
        return await _call_tool_for_mcp(request, rpc_id, params)

    return _err(rpc_id, _METHOD_NOT_FOUND, f"Unknown method: {method!r}")


def _list_tools_for_mcp() -> list[dict[str, Any]]:
    """Render the registry in the shape MCP clients expect.

    MCP requires `inputSchema` (camelCase) — different from the internal
    `input_schema` (snake_case) we use elsewhere.
    """
    registry = default_registry()
    out = []
    allowed = set(_allowed_tiers())
    for tool in registry.filter_by_tiers(tuple(allowed)):
        out.append({
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema or {"type": "object", "properties": {}},
        })
    return out


async def _call_tool_for_mcp(
    request: Request,
    rpc_id: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Execute one tool. Mirrors the internal AgentRunner._execute_tool path
    but without the LLM loop wrapper — MCP clients drive the loop themselves.
    """
    name = params.get("name") or ""
    arguments = params.get("arguments") or {}
    if not isinstance(name, str) or not name:
        return _err(rpc_id, _INVALID_PARAMS, "params.name is required")
    if not isinstance(arguments, dict):
        return _err(rpc_id, _INVALID_PARAMS, "params.arguments must be an object")

    registry = default_registry()
    try:
        tool = registry.get(name)
    except Exception:
        return _err(
            rpc_id,
            _INVALID_PARAMS,
            f"Tool {name!r} not registered (or not exposed via MCP)",
        )

    if tool.tier not in _allowed_tiers():
        return _err(
            rpc_id,
            _INVALID_PARAMS,
            f"Tool {name!r} is tier={tool.tier.value!r} which is not exposed via MCP. "
            "Set MCP_ALLOW_WRITE_TOOLS=1 to enable safe-write tools.",
        )

    # RBAC. Optional auth — when no user, treat as the implicit local dev
    # caller in workspace 'default' (matches every other OSS endpoint).
    user = current_user_optional(request)
    user_id = getattr(user, "id", None) if user else None
    user_role = getattr(user, "role", "viewer") if user else "viewer"
    workspace_id = current_workspace_id(request)
    environment = (request.query_params.get("environment") or "dev").lower()
    if environment not in ("dev", "prod"):
        environment = "dev"

    if not authorize_tool_call(
        tool_tier=tool.tier,
        user_role=user_role,
        environment=environment,
    ):
        return _err(
            rpc_id,
            _INVALID_REQUEST,
            f"role={user_role!r} cannot invoke tier={tool.tier.value!r} in env={environment!r}",
        )

    ctx = ToolContext(
        tenant_id=workspace_id,
        user_id=user_id,
        workspace_id=workspace_id,
        environment=environment,
        dry_run=False,
    )

    try:
        sanitized_input = sanitize_for_llm(arguments)
        raw_result = await tool.handler(sanitized_input.payload, ctx)
    except Exception as e:
        return _err(
            rpc_id,
            _INTERNAL_ERROR,
            f"Tool {name!r} failed: {type(e).__name__}: {str(e)[:200]}",
        )

    # Schema validate then sanitize the output (PII redaction etc.) before
    # returning. MCP wraps the response in a `content` array of typed blocks;
    # we always emit one text block with the JSON-stringified result.
    try:
        normalized = normalize_tool_output(name, raw_result)
    except SchemaError as e:
        return _err(rpc_id, _INTERNAL_ERROR, f"Tool {name!r} output failed schema: {e}")

    sanitized_output = sanitize_for_llm(normalized)

    import json as _json
    text_block = _json.dumps(sanitized_output.payload, default=str, indent=2)[:8000]

    return _ok(rpc_id, {
        "content": [{"type": "text", "text": text_block}],
        # Useful debugging info; clients can ignore.
        "_meta": {
            "tool_tier": tool.tier.value,
            "redactions_applied": sanitized_output.redactions,
        },
    })


@router.get("/discovery")
def mcp_discovery() -> dict[str, Any]:
    """Public discovery endpoint — describes how to connect to this MCP server.

    No auth required. Lets external tooling (Claude Desktop config wizards,
    Cursor's MCP picker, etc.) probe the URL and fetch connection details
    without having to read our docs.
    """
    return {
        "protocolVersion": _MCP_PROTOCOL_VERSION,
        "serverInfo": _SERVER_INFO,
        "endpoint": "/api/v1/mcp",
        "transport": "http+jsonrpc",
        "auth": "bearer-optional",
        "tool_tiers_exposed": [t.value for t in _allowed_tiers()],
        "documentation": "https://docs.fpulse.io/mcp",
    }
