"""
inspect_connections tool — read-only.

Returns connection metadata + last test status. Credentials, hosts, ports,
database names NEVER returned (per ai-boundary-contract.md §2).
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pipeline_id = inputs.get("pipeline_id", "")
    # Default to the user's currently-selected pipeline so the LLM can
    # answer "what connections does this pipeline use?" without naming an ID.
    if not pipeline_id and ctx.selected_ids:
        pipeline_id = ctx.selected_ids[0]

    try:
        from fpulse.main import app_state

        store = app_state.get("connection_store")
        if not store:
            return {"connections": [], "total": 0, "_error": "Connection store unavailable."}
        rows = store.list_all(workspace_id=ctx.workspace_id)
    except Exception as exc:
        return {"connections": [], "total": 0, "_error": str(exc)}

    connections: list[dict[str, Any]] = []
    for conn in rows:
        data = conn.model_dump(mode="json") if hasattr(conn, "model_dump") else dict(conn)
        ok = data.get("last_test_ok")
        if ok is True:
            last_test = "passed"
        elif ok is False:
            last_test = "failed"
        else:
            last_test = "never"
        connections.append({
            "id": data.get("id"),
            "name": data.get("name"),
            "connector_type": data.get("type"),
            "project_id": data.get("project_id"),
            "environment": data.get("environment"),
            "capabilities": data.get("capabilities") or [],
            "last_test": last_test,
            "last_test_ok": ok,
            "last_test_at": data.get("last_test_at"),
            "last_test_error": (data.get("last_test_error") or "")[:180],
        })
    return {
        "connections": connections,
        "total": len(connections),
    }


DEFINITION = ToolDefinition(
    name="inspect_connections",
    tier=ToolTier.READ,
    description=(
        "List the connections used by a pipeline (or visible in the current "
        "workspace if no pipeline_id given). Returns connector type and last "
        "connectivity test result. Never returns credentials or hostnames."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pipeline_id": {
                "type": "string",
                "description": "Optional pipeline UUID to scope to. Omit for workspace-wide.",
            },
        },
    },
    output_schema={
        "connections": "list",
        "total": "int",
        "_error": "str?",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["connection", "read"],
)
