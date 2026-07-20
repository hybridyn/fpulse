"""
explain_step tool — read-only.

Returns the role + sanitized configuration of one step inside a saved
pipeline. Lets the agent answer "what does the Filter step do?" / "why
is the join keyed on customer_id?" / "what file is the Source reading?"
without leaking credentials or sample data.

Sanitization rules (per docs/ai-boundary-contract.md §2):
  - connection_id and connector_type are returned (they're discovery info,
    not secrets).
  - Anything matching a credential-like key name (password, secret, token,
    api_key, auth_*) is redacted to "***".
  - Free-form params (file_path, query, condition, expression) flow as-is
    so the agent can explain what the step does, but we cap each value
    length to keep token cost predictable.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


_CREDENTIAL_HINTS = (
    "password", "secret", "token", "api_key", "apikey",
    "auth_token", "access_key", "private_key", "client_secret",
    "connection_string",  # may embed user:pass
)
_MAX_VALUE_LEN = 600


def _redact_value(key: str, value: Any) -> Any:
    k = (key or "").lower()
    if any(hint in k for hint in _CREDENTIAL_HINTS):
        return "***"
    if isinstance(value, str) and len(value) > _MAX_VALUE_LEN:
        return value[:_MAX_VALUE_LEN] + f"… (truncated, {len(value)} chars total)"
    return value


def _step_type(s) -> str:
    t = getattr(s, "type", None) or getattr(s, "step_type", None) or "unknown"
    return getattr(t, "value", t) if t else "unknown"


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pipeline_id = inputs.get("pipeline_id", "")
    step_id = inputs.get("step_id", "")
    if not pipeline_id and ctx.selected_ids:
        pipeline_id = ctx.selected_ids[0]
    if not pipeline_id and ctx.visible_ids and len(ctx.visible_ids) == 1:
        pipeline_id = ctx.visible_ids[0]
    if not pipeline_id:
        raise ValueError("pipeline_id is required (no pipeline selected on the current page)")
    if not step_id:
        raise ValueError("step_id is required")

    try:
        from fpulse.main import app_state  # type: ignore
        store = app_state.get("workflow_store")
    except Exception:
        store = None

    if store is None:
        return {
            "pipeline_id": pipeline_id,
            "step_id": step_id,
            "found": False,
            "message": "Workflow store unavailable; cannot inspect.",
        }

    workspace_id = ctx.workspace_id or "default"
    try:
        wv = store.get(pipeline_id, workspace_id=workspace_id)
    except TypeError:
        wv = store.get(pipeline_id)
    if wv is None or wv.workflow is None:
        return {
            "pipeline_id": pipeline_id,
            "step_id": step_id,
            "found": False,
            "message": f"Pipeline {pipeline_id!r} not found in this workspace.",
        }

    wf = wv.workflow
    target = next((s for s in (getattr(wf, "steps", []) or []) if getattr(s, "id", "") == step_id), None)
    if target is None:
        return {
            "pipeline_id": pipeline_id,
            "step_id": step_id,
            "found": False,
            "message": f"Step {step_id!r} not in pipeline {pipeline_id!r}.",
        }

    raw_params = getattr(target, "params", {}) or {}
    sanitized_params: dict[str, Any] = {}
    for k, v in raw_params.items():
        sanitized_params[k] = _redact_value(k, v)

    # Upstream / downstream connectivity — useful context for "why is this
    # step here?" / "what feeds into it?" without a second tool call.
    upstream_ids: list[str] = []
    downstream_ids: list[str] = []
    for c in (getattr(wf, "connections", []) or []):
        if getattr(c, "to_step", None) == step_id:
            upstream_ids.append(getattr(c, "from_step", ""))
        if getattr(c, "from_step", None) == step_id:
            downstream_ids.append(getattr(c, "to_step", ""))

    return {
        "pipeline_id": pipeline_id,
        "step_id": step_id,
        "found": True,
        "step_type": _step_type(target),
        "label": getattr(target, "label", "") or "",
        "params": sanitized_params,
        "upstream_step_ids": upstream_ids,
        "downstream_step_ids": downstream_ids,
    }


DEFINITION = ToolDefinition(
    name="explain_step",
    tier=ToolTier.READ,
    description=(
        "Drill into a single step (node) inside a saved pipeline and return "
        "its type, label, sanitized configuration, and which steps feed into "
        "or out of it. Use this when the user asks about a specific step — "
        "'what does the Filter do', 'how is the Join configured', 'what file "
        "does the source read'. Credentials are redacted; large strings are "
        "truncated. Prefer this over summarize_pipeline when the user is "
        "asking about ONE node."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pipeline_id": {
                "type": "string",
                "description": "Pipeline UUID. Optional when a single pipeline is selected/open.",
            },
            "step_id": {
                "type": "string",
                "description": "ID of the step within the pipeline. Required.",
            },
        },
        "required": ["step_id"],
    },
    output_schema={
        "pipeline_id": "str",
        "step_id": "str",
        "found": "bool",
        "step_type": "str",
        "label": "str",
        "params": "dict",
        "upstream_step_ids": "list",
        "downstream_step_ids": "list",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["pipeline", "read", "drill-down"],
)
