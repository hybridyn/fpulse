"""
modify_pipeline_step tool — safe-write (draft only).

Takes an existing pipeline + a single edit op (insert / delete / reconfigure
/ rename / move) and produces a draft IR with the change applied. Nothing
is saved — `apply_pipeline_draft` (HIGH_IMPACT_WRITE) is the explicit commit
step, gated by RBAC + dry-run + confirmation card.

This is the "modify what I already have" companion to draft_pipeline_from_intent.
draft_pipeline_from_intent builds a NEW pipeline from natural language;
modify_pipeline_step EDITS an existing one with a precise op.

Trust contract:
  - Existing pipeline is fetched read-only; the modification happens on
    a deepcopy of the IR.
  - The draft is held in-process via the same _DRAFT_STORE used by
    draft_pipeline_from_intent. apply_pipeline_draft picks it up.
  - The draft is tagged with `_modification_of: pipeline_id` so
    apply_pipeline_draft saves over the existing pipeline rather than
    creating a new one.
  - Defaults to ctx.selected_ids[0] when pipeline_id is omitted, so the
    user can say "add a Filter after the CSV source" while looking at a
    pipeline in the Editor without naming it.
"""

from __future__ import annotations

import copy
from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier
from fpulse.ai.tools.draft_pipeline_from_intent import store_draft

# Allowed ops. Each enforces a small, predictable change so the
# confirmation diff stays legible. Larger refactors should fall back to
# draft_pipeline_from_intent (rebuild) rather than chaining many ops.
ALLOWED_OPS = ("insert", "delete", "reconfigure", "rename", "move")


def _load_pipeline_ir(pipeline_id: str, workspace_id: str) -> dict[str, Any] | None:
    """Fetch the existing pipeline as a plain dict, or None if missing."""
    try:
        from fpulse.main import app_state  # type: ignore
        store = app_state.get("workflow_store")
    except Exception:
        return None
    if store is None:
        return None
    try:
        wv = store.get(pipeline_id, workspace_id=workspace_id)
    except TypeError:
        wv = store.get(pipeline_id)
    if wv is None or wv.workflow is None:
        return None
    wf = wv.workflow
    # model_dump() if pydantic v2, dict() if v1.
    try:
        return wf.model_dump()
    except AttributeError:
        return wf.dict()


def _apply_op(ir: dict[str, Any], op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Apply one edit op to a copy of the IR; return the modified copy.

    Raises ValueError on invalid ops / unknown step IDs / arg shape.
    """
    new_ir = copy.deepcopy(ir)
    steps: list[dict[str, Any]] = list(new_ir.get("steps") or [])
    connections: list[dict[str, Any]] = list(new_ir.get("connections") or [])

    if op == "insert":
        # Insert a step after a named anchor step (or at the end).
        anchor_id = args.get("after_step_id")
        new_step = args.get("step")
        if not isinstance(new_step, dict):
            raise ValueError("insert: 'step' (dict) is required")
        if not new_step.get("type"):
            raise ValueError("insert: step.type is required")
        new_step.setdefault("id", f"s_new_{len(steps) + 1}")
        if anchor_id:
            idx = next((i for i, s in enumerate(steps) if s.get("id") == anchor_id), None)
            if idx is None:
                raise ValueError(f"insert: after_step_id {anchor_id!r} not found")
            steps.insert(idx + 1, new_step)
            # If the anchor had a downstream edge, splice the new step in.
            for c in connections:
                if c.get("from_step") == anchor_id:
                    # Repoint old edge to start from new step
                    c["from_step"] = new_step["id"]
            connections.append({"from_step": anchor_id, "to_step": new_step["id"]})
        else:
            steps.append(new_step)
            if steps[:-1]:
                last_existing = steps[-2]
                connections.append({"from_step": last_existing["id"], "to_step": new_step["id"]})

    elif op == "delete":
        target = args.get("step_id")
        if not target:
            raise ValueError("delete: step_id is required")
        if not any(s.get("id") == target for s in steps):
            raise ValueError(f"delete: step_id {target!r} not found")
        steps = [s for s in steps if s.get("id") != target]
        # Drop edges touching the deleted step. We do NOT auto-reconnect
        # parents to children — that's a larger semantic change the user
        # should make explicit. The agent should follow up if needed.
        connections = [
            c for c in connections
            if c.get("from_step") != target and c.get("to_step") != target
        ]

    elif op == "reconfigure":
        target = args.get("step_id")
        params = args.get("params")
        if not target:
            raise ValueError("reconfigure: step_id is required")
        if not isinstance(params, dict):
            raise ValueError("reconfigure: params (dict) is required")
        idx = next((i for i, s in enumerate(steps) if s.get("id") == target), None)
        if idx is None:
            raise ValueError(f"reconfigure: step_id {target!r} not found")
        merged = dict(steps[idx].get("params") or {})
        merged.update(params)
        steps[idx] = {**steps[idx], "params": merged}

    elif op == "rename":
        target = args.get("step_id")
        new_label = args.get("label")
        if not target or not new_label:
            raise ValueError("rename: step_id and label are required")
        idx = next((i for i, s in enumerate(steps) if s.get("id") == target), None)
        if idx is None:
            raise ValueError(f"rename: step_id {target!r} not found")
        steps[idx] = {**steps[idx], "label": new_label}

    elif op == "move":
        target = args.get("step_id")
        position = args.get("position")
        if not target or not isinstance(position, dict):
            raise ValueError("move: step_id and position (dict with x,y) are required")
        idx = next((i for i, s in enumerate(steps) if s.get("id") == target), None)
        if idx is None:
            raise ValueError(f"move: step_id {target!r} not found")
        steps[idx] = {**steps[idx], "position": position}

    else:
        raise ValueError(f"Unknown op: {op}. Allowed: {ALLOWED_OPS}")

    new_ir["steps"] = steps
    new_ir["connections"] = connections
    return new_ir


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pipeline_id = inputs.get("pipeline_id", "")
    if not pipeline_id and ctx.selected_ids:
        pipeline_id = ctx.selected_ids[0]
    op = (inputs.get("op") or "").strip().lower()
    args = inputs.get("args") or {}
    idempotency_key = inputs.get("idempotency_key")

    if not pipeline_id:
        raise ValueError(
            "pipeline_id is required (no pipeline selected on the current page)"
        )
    if op not in ALLOWED_OPS:
        raise ValueError(f"op must be one of {ALLOWED_OPS} (got {op!r})")
    if not idempotency_key:
        raise ValueError("idempotency_key is required for safe-write tools")

    # Dry-run path returns a stable mock so the agent can describe the
    # intended change without touching state.
    if ctx.dry_run:
        return {
            "draft_id": "dry-run-mod-draft",
            "pipeline_id": pipeline_id,
            "op": op,
            "step_count_before": 0,
            "step_count_after": 0,
            "summary": f"[dry-run] Would apply {op} to pipeline {pipeline_id!r}.",
        }

    workspace_id = ctx.workspace_id or ctx.tenant_id or "default"
    ir = _load_pipeline_ir(pipeline_id, workspace_id)
    if ir is None:
        raise ValueError(
            f"Pipeline {pipeline_id!r} not found in workspace {workspace_id!r}"
        )

    before_count = len(ir.get("steps") or [])
    new_ir = _apply_op(ir, op, args)
    after_count = len(new_ir.get("steps") or [])

    # Tag the draft so apply_pipeline_draft saves over the existing pipeline
    # rather than creating a new one. Stripped before persistence.
    new_ir["_modification_of"] = pipeline_id

    draft_id = store_draft(new_ir)

    summary = (
        f"Drafted '{op}' on pipeline {ir.get('name', pipeline_id)!r}. "
        f"Steps: {before_count} → {after_count}. "
        f"Call apply_pipeline_draft with this draft_id to commit."
    )

    return {
        "draft_id": draft_id,
        "pipeline_id": pipeline_id,
        "op": op,
        "step_count_before": before_count,
        "step_count_after": after_count,
        "summary": summary,
    }


DEFINITION = ToolDefinition(
    name="modify_pipeline_step",
    tier=ToolTier.SAFE_WRITE,
    description=(
        "Edit an EXISTING pipeline with a single op: insert | delete | "
        "reconfigure | rename | move. Returns a draft_id; nothing is saved "
        "until the user confirms via apply_pipeline_draft. "
        "Use this for 'add a Filter after CSV Source', 'remove the dedup "
        "step', 'change the join key on step X', 'rename step Y to Z'. "
        "Defaults pipeline_id to the user's currently-selected pipeline "
        "(ctx.selected_ids[0]) when omitted. For brand-new pipelines, "
        "use draft_pipeline_from_intent instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pipeline_id": {
                "type": "string",
                "description": "Existing pipeline UUID. Omit to default to currently-selected pipeline.",
            },
            "op": {
                "type": "string",
                "enum": ["insert", "delete", "reconfigure", "rename", "move"],
                "description": "Edit operation to perform.",
            },
            "args": {
                "type": "object",
                "description": (
                    "Op-specific args. "
                    "insert: {after_step_id?, step: {type, label?, params?}}. "
                    "delete: {step_id}. "
                    "reconfigure: {step_id, params: {...}}. "
                    "rename: {step_id, label}. "
                    "move: {step_id, position: {x, y}}."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": "Required. Format: {tier}.{user_id}.{action}.{target_id}.{semver}",
            },
        },
        "required": ["op", "args", "idempotency_key"],
    },
    output_schema={
        "draft_id": "str",
        "pipeline_id": "str",
        "op": "str",
        "step_count_before": "int",
        "step_count_after": "int",
        "summary": "str",
    },
    handler=_handler,
    requires_idempotency_key=True,
    tags=["pipeline", "edit", "modify", "draft"],
)
