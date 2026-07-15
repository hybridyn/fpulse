"""
draft_pipeline_from_intent tool — safe-write (draft only).

Turns a natural-language description into a draft Workflow IR using the
existing `ai_generate_pipeline` planner. Returns the IR + a draft_id so the
agent can describe the proposed pipeline to the user; an explicit follow-up
`apply_pipeline_draft` call (high-impact-write) actually saves it.

This is the keystone of "end-to-end pipeline building" via Copilot:
  user: "Load orders.csv, filter status=active, write to a Parquet file"
  agent: calls draft_pipeline_from_intent → returns IR with 3 steps
  agent: shows summary + diff to user via ConfirmationCard
  user clicks Apply → agent calls apply_pipeline_draft → saved + opened in Editor

Trust contract:
- Draft is held in-process for the duration of the agent run; nothing is
  persisted until the apply step.
- The planner uses the user's configured cloud provider (Anthropic/OpenAI),
  honoring the same wallet caps + audit trail as every other LLM call.
"""

from __future__ import annotations

import secrets
from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier

# Module-level draft store — keyed by draft_id, valued by the IR dict.
# Bounded TTL via simple FIFO eviction; the agent is expected to consume
# the draft within the same run. Cross-run drafts are not supported.
_DRAFT_STORE: dict[str, dict[str, Any]] = {}
_DRAFT_MAX = 64


def _evict_if_full() -> None:
    while len(_DRAFT_STORE) > _DRAFT_MAX:
        # Drop the oldest insertion. dict preserves insertion order.
        oldest = next(iter(_DRAFT_STORE))
        _DRAFT_STORE.pop(oldest, None)


def get_draft(draft_id: str) -> dict[str, Any] | None:
    """Read a stored draft. Used by apply_pipeline_draft."""
    return _DRAFT_STORE.get(draft_id)


def store_draft(ir: dict[str, Any]) -> str:
    """Stash an IR draft, return its id."""
    _evict_if_full()
    draft_id = "wf-draft-" + secrets.token_urlsafe(8)
    _DRAFT_STORE[draft_id] = ir
    return draft_id


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    intent = (inputs.get("intent") or "").strip()
    project_id = inputs.get("project_id") or "default"
    name = (inputs.get("name") or "").strip()
    idempotency_key = inputs.get("idempotency_key")

    if not intent:
        raise ValueError("intent is required (natural-language description)")
    if not idempotency_key:
        raise ValueError("idempotency_key is required for safe-write tools")

    # Dry-run returns a stable mock draft so the agent can continue planning.
    if ctx.dry_run:
        return {
            "draft_id": "dry-run-draft",
            "name": name or "[dry-run] Untitled",
            "step_count": 0,
            "ir": {"steps": [], "connections": []},
            "summary": "[dry-run] No real planner call made.",
            "ai_powered": False,
        }

    # Use the existing planner to turn intent → IR.
    from fpulse.planner.ai_client import ai_generate_pipeline

    messages = [{"role": "user", "content": intent}]
    try:
        ir = await ai_generate_pipeline(
            messages,
            user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
        )
    except Exception as e:
        raise RuntimeError(f"Planner failed: {type(e).__name__}: {e}")

    if ir is None:
        # No provider OR planner couldn't parse a valid IR — fall back to a
        # minimal scaffold so the agent has something to talk about.
        ir = {
            "name": name or "New pipeline",
            "description": intent[:200],
            "steps": [],
            "connections": [],
        }
        summary = (
            "No AI provider configured or planner could not produce a valid IR. "
            "A blank pipeline scaffold was created — you can build it manually "
            "in the Editor or configure a provider in Insights → AI Provider."
        )
        ai_powered = False
    else:
        steps = ir.get("steps") or []
        ir.setdefault("name", name or ir.get("name") or "New pipeline")
        ir.setdefault("project_id", project_id)
        summary = (
            f"Generated a {len(steps)}-step pipeline from your description. "
            f"Inspect the steps below; call apply_pipeline_draft with this "
            f"draft_id to save it as a new pipeline in project {project_id!r}."
        )
        ai_powered = True

    draft_id = store_draft(ir)
    return {
        "draft_id": draft_id,
        "name": ir.get("name", "New pipeline"),
        "step_count": len(ir.get("steps") or []),
        "ir": ir,
        "summary": summary,
        "ai_powered": ai_powered,
    }


DEFINITION = ToolDefinition(
    name="draft_pipeline_from_intent",
    tier=ToolTier.SAFE_WRITE,
    description=(
        "Turn a natural-language pipeline description into a draft Workflow IR. "
        "Returns a draft_id and the proposed steps + connections. NOTHING IS "
        "SAVED — the user must explicitly call apply_pipeline_draft to commit. "
        "Use this for 'help me build X' / 'create a pipeline that does Y' asks."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": "Natural-language description of the pipeline to build.",
            },
            "name": {
                "type": "string",
                "description": "Optional name for the new pipeline.",
            },
            "project_id": {
                "type": "string",
                "description": "Project to drop the new pipeline under. Defaults to 'default'.",
            },
            "idempotency_key": {
                "type": "string",
                "description": "Required. Format: {tier}.{user_id}.{action}.{target_id}.{semver}",
            },
        },
        "required": ["intent", "idempotency_key"],
    },
    output_schema={
        "draft_id": "str",
        "name": "str",
        "step_count": "int",
        "ir": "dict",
        "summary": "str",
        "ai_powered": "bool",
    },
    handler=_handler,
    requires_idempotency_key=True,
    tags=["pipeline", "build", "draft"],
)
