"""list_schedules — read-only. Active schedules with cron + last/next run."""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    workspace_id = inputs.get("workspace_id") or ctx.workspace_id or ctx.tenant_id or "default"
    only_enabled = bool(inputs.get("only_enabled", False))
    schedules: list[dict[str, Any]] = []
    try:
        from fpulse.main import app_state  # type: ignore
        store = app_state.get("schedule_store")
        if store is not None:
            for r in store.list_all(workspace_id=workspace_id):
                if only_enabled and not r.get("enabled", True):
                    continue
                schedules.append({
                    "id": r.get("id", ""),
                    "workflow_id": r.get("workflow_id", ""),
                    "workflow_name": r.get("workflow_name", ""),
                    "schedule_type": r.get("schedule_type", ""),
                    "cron": r.get("cron", ""),
                    "enabled": bool(r.get("enabled", True)),
                    "last_run_at": r.get("last_run_at", ""),
                    "next_run_at": r.get("next_run_at", ""),
                })
    except Exception:
        schedules = []
    return {
        "schedules": schedules,
        "total": len(schedules),
        "workspace_id": workspace_id,
    }


DEFINITION = ToolDefinition(
    name="list_schedules",
    tier=ToolTier.READ,
    description=(
        "List the schedules in the current workspace. Returns id, workflow_id, "
        "workflow_name, schedule_type (cron/interval/once), cron expression, enabled "
        "flag, last_run_at, next_run_at. Use when the user asks about scheduled jobs, "
        "automation, or wants to know what's running and when."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "only_enabled": {"type": "boolean", "description": "If true, exclude disabled schedules."},
            "workspace_id": {"type": "string"},
        },
    },
    output_schema={"schedules": "list", "total": "int", "workspace_id": "str"},
    handler=_handler,
    requires_idempotency_key=False,
    tags=["schedule", "read", "list"],
)
