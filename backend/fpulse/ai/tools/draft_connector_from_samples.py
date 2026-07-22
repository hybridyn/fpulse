"""draft_connector_from_samples tool — safe-write (draft only).

For vendors with no OpenAPI spec: the user pastes 1-5 sample API responses and
the deterministic engine (`ai_authoring.generate_and_validate` in 'samples'
mode) infers the response schema into a connector manifest draft.

This produces a *schema* draft (v2) — it captures the shape of the data but not
the endpoint path or auth, which samples alone can't reveal. So the draft is
`runnable=False`: approving it does NOT auto-activate a live connector; instead
it directs the admin to finish it (base URL + endpoint + auth) in the
Author-Connector UI. Same guardrails as the OpenAPI tool: no secrets, no
activation without an explicit admin step.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    connector_id = (inputs.get("connector_id") or "").strip()
    display_name = (inputs.get("display_name") or "").strip()
    base_url = (inputs.get("base_url") or "").strip()
    stream_name = (inputs.get("stream_name") or "").strip() or None
    category = (inputs.get("category") or "saas").strip() or "saas"
    samples = inputs.get("samples")
    idempotency_key = inputs.get("idempotency_key")

    if not connector_id:
        raise ValueError("connector_id is required (letters, digits, underscore)")
    if not idempotency_key:
        raise ValueError("idempotency_key is required for safe-write tools")
    if not isinstance(samples, list) or not samples:
        raise ValueError("samples must be a non-empty list of 1-5 example response objects")
    if len(samples) > 5:
        raise ValueError("provide at most 5 sample responses")
    if not all(isinstance(s, dict) for s in samples):
        raise ValueError("each sample must be a JSON object")

    if ctx.dry_run:
        return {
            "draft_id": "dry-run-draft",
            "connector_id": connector_id,
            "runnable": False,
            "summary": "[dry-run] No schema inferred.",
            "next_step": "[dry-run]",
        }

    from fpulse.connectors.ai_authoring import generate_and_validate

    try:
        result = generate_and_validate(
            samples,
            connector_id,
            mode="samples",
            display_name=display_name or None,
            category=category,
            base_url=base_url,
            stream_name=stream_name,
        )
    except ValueError as exc:
        raise ValueError(f"Could not infer a schema from those samples: {exc}")

    manifest = result.get("manifest") or {}

    from fpulse.connectors.drafts import default_draft_store

    store = default_draft_store()
    draft = store.propose(
        connector_id=connector_id,
        mode="samples_schema",
        manifest=manifest,
        runnable=False,
        display_name=display_name or connector_id,
        category=category,
        validation=result.get("validation") or {},
        summary=(
            f"Inferred a response schema for '{display_name or connector_id}' from "
            f"{len(samples)} sample(s). This is a starting point, not a runnable connector yet."
        ),
        source=f"samples:{len(samples)}",
        proposed_by=(ctx.user_id or "copilot"),
        workspace_id=ctx.workspace_id or "default",
    )

    return {
        "draft_id": draft.id,
        "connector_id": connector_id,
        "runnable": False,
        "summary": draft.summary,
        "next_step": (
            "Tell the user this captured the DATA SHAPE but still needs the base URL, "
            "endpoint path, and auth. After an admin approves the draft, they finish it in "
            "Insights → Author Connector and Save. No credentials are stored in the draft."
        ),
    }


DEFINITION = ToolDefinition(
    name="draft_connector_from_samples",
    tier=ToolTier.SAFE_WRITE,
    description=(
        "Draft a connector's response schema from 1-5 sample API responses, for vendors "
        "with no OpenAPI spec. Returns a draft_id. This captures the DATA SHAPE only — not "
        "the endpoint or auth — so it is a starting point a human finishes; it does NOT go "
        "live and stores NO credentials. Prefer draft_connector_from_openapi when a spec/URL "
        "is available."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "connector_id": {
                "type": "string",
                "description": "Short id for the connector (letters, digits, underscore).",
            },
            "display_name": {"type": "string", "description": "Optional human-friendly name."},
            "base_url": {"type": "string", "description": "Optional API base URL if the user knows it."},
            "stream_name": {"type": "string", "description": "Optional name for the inferred stream/entity."},
            "category": {"type": "string", "description": "Connector category (default 'saas')."},
            "samples": {
                "type": "array",
                "description": "1-5 example JSON response objects from the API.",
                "items": {"type": "object"},
                "minItems": 1,
                "maxItems": 5,
            },
            "idempotency_key": {
                "type": "string",
                "description": "Required. Format: {tier}.{user_id}.{action}.{target_id}.{semver}",
            },
        },
        "required": ["connector_id", "samples", "idempotency_key"],
    },
    output_schema={
        "draft_id": "str",
        "connector_id": "str",
        "runnable": "bool",
        "summary": "str",
        "next_step": "str",
    },
    handler=_handler,
    requires_idempotency_key=True,
    tags=["connector", "build", "draft", "samples"],
)
