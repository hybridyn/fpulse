"""
get_running_executions tool — read.

Returns the set of executions that are currently in-flight in the workspace.
The Copilot uses this for "what's running right now?" / "is anything stuck?".
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        from fpulse.main import app_state  # type: ignore
        log_store = app_state.get("execution_log")
    except Exception:
        log_store = None
    if log_store is None:
        return {"running": [], "count": 0, "message": "execution_log not available"}

    rows = log_store.list_executions(
        workflow_id=None,
        status="running",
        limit=50,
        workspace_id=ctx.workspace_id,
    )

    # Default to caller's env. "all" via input opts out for cross-env asks.
    env_filter = (inputs.get("environment") or ctx.environment or "").strip().lower()
    if env_filter == "all":
        env_filter = ""

    items = []
    for r in rows:
        row_env = (r.get("environment") or ctx.environment or "dev").strip().lower()
        if env_filter and row_env != env_filter:
            continue
        items.append({
            "execution_id": r.get("execution_id") or r.get("id"),
            "workflow_id": r.get("workflow_id"),
            "workflow_name": r.get("workflow_name"),
            "environment": row_env,
            "started_at": r.get("started_at"),
            "completed_steps": r.get("completed_steps") or 0,
            "total_steps": r.get("total_steps") or 0,
            "rows_processed": r.get("total_rows_processed") or 0,
            "triggered_by": r.get("triggered_by"),
        })

    return {
        "running": items,
        "count": len(items),
        "message": (
            f"{len(items)} execution(s) currently running in this workspace."
            if items else
            "No pipelines are currently running."
        ),
    }


DEFINITION = ToolDefinition(
    name="get_running_executions",
    tier=ToolTier.READ,
    description=(
        "List pipeline executions that are CURRENTLY RUNNING in this workspace. "
        "Use for 'what's running right now', 'is anything stuck', 'cancel a "
        "long-running job' style asks. Returns workflow_id + name + progress."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "environment": {
                "type": "string",
                "enum": ["dev", "prod", "all"],
                "description": "Filter by environment. Defaults to caller's current env.",
            },
        },
    },
    output_schema={
        "running": "list",
        "count": "int",
        "message": "str",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["execution", "running", "monitor"],
)
