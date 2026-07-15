"""
compose_report tool — safe-write (draft-only).

Composes a report from a template and provided summary data. Returns a
draft_id; the actual send/schedule is a separate high-impact-write tool that
ships in Step 4.

Per round-3 reviewer guidance: "Add write tools only after Step 1.5b
governance is complete." compose_report is the lone safe-write tool in the
initial registry because it's truly draft-only — no external delivery, no
scheduled job, no state mutation beyond a draft row in the reports store.
"""

from __future__ import annotations

import secrets
from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    template = inputs.get("template", "")
    title = inputs.get("title", "Untitled Report")
    summary = inputs.get("summary", "")
    sections = inputs.get("sections", [])
    idempotency_key = inputs.get("idempotency_key")

    if not template:
        raise ValueError("template is required")
    if not idempotency_key:
        raise ValueError("idempotency_key is required for safe-write tools")

    # In dry-run mode return a stable mock draft_id so the agent can continue
    # planning without committing anything.
    if ctx.dry_run:
        return {
            "title": title,
            "summary": summary or "[dry-run] no summary persisted",
            "sections": sections,
            "draft_id": "dry-run-draft",
        }

    # Step 1.5a stub: return a fresh draft_id. Step 6 wires real report store.
    draft_id = secrets.token_urlsafe(8)
    return {
        "title": title,
        "summary": summary,
        "sections": sections,
        "draft_id": draft_id,
    }


DEFINITION = ToolDefinition(
    name="compose_report",
    tier=ToolTier.SAFE_WRITE,
    description=(
        "Compose a draft report from a template + summary content. Returns a "
        "draft_id — the report is NOT sent or scheduled by this tool; the "
        "user must explicitly send/schedule the draft via a separate action."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "template": {"type": "string", "description": "Template name (e.g. 'monthly-metrics')."},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "sections": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Section objects: {heading, body}.",
            },
            "idempotency_key": {
                "type": "string",
                "description": "Required. Format: {tier}.{user_id}.{action}.{target_id}.{semver}",
            },
        },
        "required": ["template", "idempotency_key"],
    },
    output_schema={
        "title": "str",
        "summary": "str",
        "sections": "list",
        "draft_id": "str",
    },
    handler=_handler,
    requires_idempotency_key=True,
    tags=["report", "draft"],
)
