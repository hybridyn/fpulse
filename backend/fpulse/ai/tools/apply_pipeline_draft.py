"""
apply_pipeline_draft tool — high-impact-write.

Saves a previously-drafted pipeline IR (from `draft_pipeline_from_intent`)
into the workflow store. This is the only way the agent can mutate the
workflow corpus, and it's gated by:
  - HIGH_IMPACT_WRITE tier → strict RBAC + dry-run by default
  - Idempotency key required
  - Confirmation card on the frontend (rendered for high-impact writes)
  - Audit trail via the standard agent trace
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier
from fpulse.ai.tools.draft_pipeline_from_intent import get_draft


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    draft_id = inputs.get("draft_id")
    name_override = inputs.get("name")
    idempotency_key = inputs.get("idempotency_key")

    if not draft_id:
        raise ValueError("draft_id is required (call draft_pipeline_from_intent first)")
    if not idempotency_key:
        raise ValueError("idempotency_key is required for high-impact-write tools")

    if ctx.dry_run:
        return {
            "workflow_id": "dry-run-id",
            "name": name_override or "[dry-run] Untitled",
            "step_count": 0,
            "saved": False,
            "message": "[dry-run] Draft would be saved to workflow store.",
        }

    if draft_id == "dry-run-draft":
        # Apply called with a dry-run draft id — bail out gracefully.
        return {
            "workflow_id": "",
            "name": "",
            "step_count": 0,
            "saved": False,
            "message": "Cannot apply a dry-run draft. Re-draft first.",
        }

    ir = get_draft(draft_id)
    if ir is None:
        raise ValueError(f"Draft {draft_id!r} not found (expired or already consumed)")

    # Resolve workflow store via app_state (matches every other write path).
    try:
        from fpulse.main import app_state  # type: ignore
        store = app_state.get("workflow_store")
    except Exception:
        store = None
    if store is None:
        raise RuntimeError("workflow_store unavailable — cannot persist draft")

    from fpulse.ir.schema import Workflow

    # Detect "modification of an existing pipeline" drafts produced by
    # modify_pipeline_step. When present, save over the existing workflow_id
    # rather than creating a new pipeline. The flag is private to the
    # draft store and stripped before validation.
    payload = dict(ir)
    target_existing_id = payload.pop("_modification_of", None)
    is_modification = bool(target_existing_id)

    if name_override:
        payload["name"] = name_override
    payload["workspace_id"] = ctx.workspace_id or "default"
    payload["owner_id"] = ctx.user_id or ""

    if is_modification:
        # Preserve the existing pipeline's id so the save() call updates
        # in place with a new version row rather than creating a fresh one.
        payload["id"] = target_existing_id
    else:
        # Fresh pipeline — drop any pre-existing id so the model generates one.
        payload.pop("id", None)

    try:
        wf = Workflow(**payload)
    except Exception as e:
        raise ValueError(f"Drafted IR didn't validate as a Workflow: {e}")

    change_summary = (
        f"Modified via Copilot from draft {draft_id[-8:]}"
        if is_modification else
        f"Created via Copilot from draft {draft_id[-8:]}"
    )
    version = store.save(wf, change_summary=change_summary, created_by=ctx.user_id or "agent")

    return {
        "workflow_id": wf.id,
        "name": wf.name,
        "step_count": len(wf.steps or []),
        "version": getattr(version, "version", 1),
        "saved": True,
        "modified": is_modification,
        "message": (
            f"Pipeline {wf.name!r} updated to version {getattr(version, 'version', 1)} "
            f"with {len(wf.steps or [])} step(s)."
            if is_modification else
            f"Pipeline {wf.name!r} created with {len(wf.steps or [])} step(s)."
        ),
    }


DEFINITION = ToolDefinition(
    name="apply_pipeline_draft",
    tier=ToolTier.HIGH_IMPACT_WRITE,
    description=(
        "Apply a draft pipeline IR (from draft_pipeline_from_intent) to the "
        "workflow store. Creates a real, saved pipeline the user can open in "
        "the Editor. HIGH-IMPACT WRITE — requires user confirmation; dry-run "
        "by default until the user has demonstrated comfort with the agent."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "draft_id": {
                "type": "string",
                "description": "Draft ID returned from draft_pipeline_from_intent.",
            },
            "name": {
                "type": "string",
                "description": "Optional override for the pipeline name.",
            },
            "idempotency_key": {
                "type": "string",
                "description": "Required. Format: {tier}.{user_id}.{action}.{target_id}.{semver}",
            },
        },
        "required": ["draft_id", "idempotency_key"],
    },
    output_schema={
        "workflow_id": "str",
        "name": "str",
        "step_count": "int",
        "version": "int",
        "saved": "bool",
        "modified": "bool",
        "message": "str",
    },
    handler=_handler,
    requires_idempotency_key=True,
    tags=["pipeline", "build", "apply"],
)
