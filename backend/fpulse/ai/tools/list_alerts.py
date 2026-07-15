"""list_alerts — read-only. Alert rules in the workspace."""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    workspace_id = inputs.get("workspace_id") or ctx.workspace_id or ctx.tenant_id or "default"
    rules: list[dict[str, Any]] = []
    try:
        from fpulse.main import app_state  # type: ignore
        store = app_state.get("alert_store")
        if store is not None:
            for r in store.list_rules(workspace_id=workspace_id):
                rules.append({
                    "id": r.get("id", ""),
                    "name": r.get("name", ""),
                    "workflow_id": r.get("workflow_id", ""),
                    "condition": r.get("condition", ""),
                    "channel": r.get("channel", ""),
                    "enabled": bool(r.get("enabled", True)),
                    "last_fired_at": r.get("last_fired_at", ""),
                })
    except Exception:
        rules = []
    return {
        "alerts": rules,
        "total": len(rules),
        "workspace_id": workspace_id,
    }


DEFINITION = ToolDefinition(
    name="list_alerts",
    tier=ToolTier.READ,
    description=(
        "List the alert rules in the current workspace. Returns id, name, "
        "workflow_id (which pipeline it watches), condition (e.g. ON_FAILURE), "
        "channel (email/slack/webhook), enabled flag, last_fired_at. Use when the "
        "user asks about monitoring, alerts, or notification coverage."
    ),
    input_schema={
        "type": "object",
        "properties": {"workspace_id": {"type": "string"}},
    },
    output_schema={"alerts": "list", "total": "int", "workspace_id": "str"},
    handler=_handler,
    requires_idempotency_key=False,
    tags=["alert", "read", "list"],
)
