"""get_workspace_overview — read-only. Top-level dashboard counts."""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


def _safe_count(store_attr: str, workspace_id: str, app_state) -> int:
    try:
        store = app_state.get(store_attr)
        if store is None:
            return 0
        if store_attr == "alert_store":
            return len(store.list_rules(workspace_id=workspace_id))
        return len(store.list_all(workspace_id=workspace_id))
    except Exception:
        return 0


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    workspace_id = inputs.get("workspace_id") or ctx.workspace_id or ctx.tenant_id or "default"
    counts: dict[str, int] = {
        "pipelines": 0, "projects": 0, "schedules": 0,
        "alerts": 0, "connections": 0, "variables": 0, "credentials": 0,
    }
    try:
        from fpulse.main import app_state  # type: ignore
        counts["pipelines"]   = _safe_count("store",            workspace_id, app_state)
        counts["projects"]    = _safe_count("project_store",    workspace_id, app_state)
        counts["schedules"]   = _safe_count("schedule_store",   workspace_id, app_state)
        counts["alerts"]      = _safe_count("alert_store",      workspace_id, app_state)
        counts["connections"] = _safe_count("connection_store", workspace_id, app_state)
        counts["variables"]   = _safe_count("variable_store",   workspace_id, app_state)
        counts["credentials"] = _safe_count("credential_store", workspace_id, app_state)
    except Exception:
        pass
    return {
        "counts": counts,
        "workspace_id": workspace_id,
        "environment": ctx.environment,
    }


DEFINITION = ToolDefinition(
    name="get_workspace_overview",
    tier=ToolTier.READ,
    description=(
        "Get the top-level counts for the current workspace: pipelines, projects, "
        "schedules, alerts, connections, variables, credentials. Use when the user "
        "asks 'what's in this workspace?', 'give me an overview', 'how big is this "
        "F-Pulse install?', or wants a dashboard-style summary in chat."
    ),
    input_schema={
        "type": "object",
        "properties": {"workspace_id": {"type": "string"}},
    },
    output_schema={"counts": "dict", "workspace_id": "str", "environment": "str"},
    handler=_handler,
    requires_idempotency_key=False,
    tags=["overview", "read"],
)
