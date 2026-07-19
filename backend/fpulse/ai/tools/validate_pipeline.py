"""
validate_pipeline tool — read-only.

Runs the IR validator against a saved pipeline and returns the structured
list of errors. Lets the agent answer "what's wrong with this pipeline?" /
"is it ready to publish?" without the LLM having to inspect every node and
guess. Mirrors the validator the executor short-circuits on for safety_mode
== 'dry_run' / 'validate_only', so the agent's answer matches the actual
publish gate.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pipeline_id = inputs.get("pipeline_id", "")
    # Default to the user's selection / single-visible pipeline so the LLM
    # doesn't need to thread an explicit ID when the user says "validate
    # this one". Mirrors summarize_pipeline's resolution rule.
    if not pipeline_id and ctx.selected_ids:
        pipeline_id = ctx.selected_ids[0]
    if not pipeline_id and ctx.visible_ids and len(ctx.visible_ids) == 1:
        pipeline_id = ctx.visible_ids[0]
    if not pipeline_id:
        raise ValueError("pipeline_id is required (no pipeline selected on the current page)")

    try:
        from fpulse.main import app_state  # type: ignore
        store = app_state.get("workflow_store")
    except Exception:
        store = None

    if store is None:
        return {
            "pipeline_id": pipeline_id,
            "valid": False,
            "error_count": 0,
            "errors": [],
            "message": "Workflow store unavailable; cannot validate.",
        }

    workspace_id = ctx.workspace_id or "default"
    try:
        wv = store.get(pipeline_id, workspace_id=workspace_id)
    except TypeError:
        wv = store.get(pipeline_id)
    if wv is None or wv.workflow is None:
        return {
            "pipeline_id": pipeline_id,
            "valid": False,
            "error_count": 0,
            "errors": [],
            "message": f"Pipeline {pipeline_id!r} not found in this workspace.",
        }

    # Reuse the canonical IR validator — same one the executor invokes
    # under safety_mode=dry_run/validate_only. Keeping a single source of
    # truth means the agent's answer can never diverge from what Publish
    # or Run would report.
    try:
        from fpulse.ir.validator import validate_workflow as _validate
        raw_errors = _validate(wv.workflow) or []
    except Exception as exc:
        return {
            "pipeline_id": pipeline_id,
            "valid": False,
            "error_count": 1,
            "errors": [{"step_id": "", "severity": "error", "message": f"Validator crashed: {exc}"}],
        }

    errors_out: list[dict[str, Any]] = []
    for e in raw_errors:
        errors_out.append({
            "step_id": getattr(e, "step_id", "") or "",
            "severity": getattr(e, "severity", "error") or "error",
            "message": getattr(e, "message", str(e)),
        })

    return {
        "pipeline_id": pipeline_id,
        "pipeline_name": getattr(wv.workflow, "name", "") or "",
        "valid": len(errors_out) == 0,
        "error_count": len(errors_out),
        "errors": errors_out,
    }


DEFINITION = ToolDefinition(
    name="validate_pipeline",
    tier=ToolTier.READ,
    description=(
        "Run the F-Pulse IR validator against a saved pipeline and return "
        "the structured list of errors (empty list = valid). Use this when "
        "the user asks 'what's wrong with this pipeline?', 'are there any "
        "issues?', 'is it ready to publish?', 'why won't it publish?', or "
        "any variant. The result is authoritative — it's the same validator "
        "Publish + Run gate on, so your answer matches what those buttons "
        "would report."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pipeline_id": {
                "type": "string",
                "description": "Pipeline UUID. Optional when the user has a single pipeline selected/open — the tool will fall back to ctx.selected_ids / ctx.visible_ids.",
            },
        },
        "required": [],
    },
    output_schema={
        "pipeline_id": "str",
        "pipeline_name": "str",
        "valid": "bool",
        "error_count": "int",
        "errors": "list",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["pipeline", "read", "validation"],
)
