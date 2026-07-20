"""list_projects — read-only. Workspace projects + counts."""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    workspace_id = inputs.get("workspace_id") or ctx.workspace_id or ctx.tenant_id or "default"
    projects: list[dict[str, Any]] = []
    try:
        from fpulse.main import app_state  # type: ignore
        store = app_state.get("project_store")
        if store is not None:
            for r in store.list_all(workspace_id=workspace_id):
                projects.append({
                    "id": r.get("id", ""),
                    "name": r.get("name", "(untitled)"),
                    "description": (r.get("description") or "")[:200],
                    "pipeline_count": int(r.get("workflow_count", r.get("pipeline_count", 0)) or 0),
                    "created_at": r.get("created_at", ""),
                })
    except Exception:
        projects = []
    return {"projects": projects, "total": len(projects), "workspace_id": workspace_id}


DEFINITION = ToolDefinition(
    name="list_projects",
    tier=ToolTier.READ,
    description=(
        "List the projects (folders / organisational groups) in the current workspace. "
        "Returns id, name, description (truncated), pipeline_count, created_at. Use when "
        "the user asks about projects, organisational structure, or wants to know how "
        "their pipelines are grouped."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "workspace_id": {"type": "string", "description": "Optional override; defaults to caller's workspace."},
        },
    },
    output_schema={"projects": "list", "total": "int", "workspace_id": "str"},
    handler=_handler,
    requires_idempotency_key=False,
    tags=["project", "read", "list"],
)
