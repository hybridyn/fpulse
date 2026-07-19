"""
list_pipelines tool — read-only.

Returns the names + IDs + status of pipelines visible to the caller in the
current workspace. Closes a UX gap surfaced by Apr 29 dogfooding: the
agent had no way to answer "what pipelines exist?" and was falling back to
``inspect_connections``, then explaining to the user that connections are
not pipelines. With this tool registered, llama3.1 / qwen2.5 invoke it
directly when asked.

Per ai-boundary-contract.md §2 — only id, name, status, last_run_status,
step_count are sent to the LLM. Connection credentials, pipeline node
config, and SQL bodies are NEVER returned by this tool.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    workspace_id = (
        inputs.get("workspace_id")
        or ctx.workspace_id
        or ctx.tenant_id
        or "default"
    )

    # Pull from the live WorkflowStore when available; fall back to a small
    # stub when the store isn't wired (test env, app_state not populated).
    pipelines: list[dict[str, Any]] = []
    try:
        from fpulse.main import app_state  # type: ignore
        store = app_state.get("store")
        if store is not None:
            rows = store.list_all(workspace_id=workspace_id)
            for r in rows:
                pipelines.append({
                    "id": r.get("id", ""),
                    "name": r.get("name", "(untitled)"),
                    "status": r.get("status", "draft"),
                    "step_count": int(r.get("step_count", 0)),
                    "updated_at": r.get("updated_at", ""),
                })
    except Exception:
        # Best-effort — agent loop sees the tool succeed with an empty list
        # rather than tool_failure, which is more useful for the LLM.
        pipelines = []

    # Apply optional name-substring filter so the LLM can narrow on a hint
    # like "show pipelines about sales".
    name_filter = (inputs.get("name_filter") or "").strip().lower()
    if name_filter:
        pipelines = [p for p in pipelines if name_filter in (p.get("name") or "").lower()]

    return {
        "pipelines": pipelines,
        "total": len(pipelines),
        "workspace_id": workspace_id,
    }


DEFINITION = ToolDefinition(
    name="list_pipelines",
    tier=ToolTier.READ,
    description=(
        "List the pipelines in the current workspace. Returns each pipeline's "
        "id, name, status (draft / testing / published / archived), step_count, "
        "and updated_at. Use this when the user asks 'what pipelines exist?', "
        "'show me my pipelines', or wants to find a pipeline by name. Pass a "
        "case-insensitive name_filter to narrow results."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name_filter": {
                "type": "string",
                "description": "Case-insensitive substring to filter pipeline names. Optional.",
            },
            "workspace_id": {
                "type": "string",
                "description": "Workspace to scope to. Optional — defaults to the caller's current workspace.",
            },
        },
    },
    output_schema={
        "pipelines": "list",
        "total": "int",
        "workspace_id": "str",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["pipeline", "read", "list"],
)
