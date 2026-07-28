"""draft_connector_from_openapi tool — safe-write (draft only).

Turns an OpenAPI 3.x / Swagger 2 spec (or a URL to one) into an INERT draft
connector using the existing deterministic engine
(`fpulse.connectors.openapi_import.manifest_from_openapi`). NOTHING goes live:
the draft is held in the DraftConnectorStore as PROPOSED; a human admin must
call `POST /api/connectors/drafts/{id}/approve` to activate it as a Beta
connector.

Guardrails (the reason a read-only observer can safely gain this write tool):
  - **No secrets through the LLM.** The generated manifest carries auth
    *templates* only (e.g. "Bearer {token}"). The real API key is added later,
    on the Connection, via the encrypted credential store — never here.
  - **No arbitrary web access.** A URL is fetched only via the SSRF-hardened
    `fetch_openapi_spec` (blocks private/loopback/metadata IPs, caps size,
    revalidates every redirect). The agent cannot reach internal services.
  - **No activation.** This tool only proposes; approval is a separate,
    admin-gated human action.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    connector_id = (inputs.get("connector_id") or "").strip()
    display_name = (inputs.get("display_name") or "").strip()
    openapi_url = (inputs.get("openapi_url") or "").strip()
    openapi_spec = inputs.get("openapi_spec")
    openapi_text = (inputs.get("openapi_text") or "").strip()
    category = (inputs.get("category") or "saas").strip() or "saas"
    idempotency_key = inputs.get("idempotency_key")

    if not connector_id:
        raise ValueError("connector_id is required (letters, digits, underscore)")
    if not idempotency_key:
        raise ValueError("idempotency_key is required for safe-write tools")
    if openapi_spec is None and not openapi_text and not openapi_url:
        raise ValueError(
            "provide openapi_spec (a parsed dict), openapi_text (pasted JSON/YAML), or openapi_url"
        )

    if ctx.dry_run:
        return {
            "draft_id": "dry-run-draft",
            "connector_id": connector_id,
            "runnable": True,
            "stream_count": 0,
            "summary": "[dry-run] No spec fetched or parsed.",
            "next_step": "[dry-run]",
        }

    # 1. Resolve the spec. Precedence: parsed dict > pasted text > fetched URL.
    #    Pasted text is parsed server-side (JSON then YAML) so a spec the user
    #    can't host publicly — the common case for gated vendor APIs — still
    #    works fully offline.
    spec = openapi_spec
    if spec is None and openapi_text:
        from fpulse.connectors.ai_authoring import parse_spec_text

        try:
            spec = parse_spec_text(openapi_text)
        except ValueError as exc:
            raise ValueError(f"Could not parse the pasted spec: {exc}")
    if spec is None:
        import anyio

        from fpulse.connectors.ai_authoring import fetch_openapi_spec

        try:
            spec = await anyio.to_thread.run_sync(fetch_openapi_spec, openapi_url)
        except Exception as exc:  # SSRF block / network / parse all surface here
            raise RuntimeError(
                f"Could not fetch the OpenAPI spec from that URL: {type(exc).__name__}: {exc}. "
                "The URL must be public (private/loopback/metadata hosts are blocked). "
                "Alternatively paste the spec directly as openapi_text (JSON or YAML)."
            )

    if not isinstance(spec, dict) or not spec.get("paths"):
        raise ValueError("that doesn't look like an OpenAPI spec — no 'paths' found")

    # 2. Generate a v1 RUNTIME manifest (the shape the SaaS Connector node runs
    #    and save_user_manifest activates).
    from fpulse.connectors.openapi_import import manifest_from_openapi

    try:
        manifest = manifest_from_openapi(spec, connector_id=connector_id)
    except ValueError as exc:
        raise ValueError(f"Could not build a connector from that spec: {exc}")

    manifest["id"] = connector_id
    if display_name:
        manifest["name"] = display_name

    streams = manifest.get("streams") or []

    # 3. Stash as an INERT draft.
    from fpulse.connectors.drafts import default_draft_store

    store = default_draft_store()
    draft = store.propose(
        connector_id=connector_id,
        mode="openapi_runtime",
        manifest=manifest,
        runnable=True,
        display_name=display_name or manifest.get("name", connector_id),
        category=category,
        summary=(
            f"Drafted connector '{manifest.get('name', connector_id)}' with "
            f"{len(streams)} endpoint(s) from an OpenAPI spec."
        ),
        source=f"openapi:{openapi_url or 'inline-spec'}",
        proposed_by=(ctx.user_id or "copilot"),
        workspace_id=ctx.workspace_id or "default",
    )

    return {
        "draft_id": draft.id,
        "connector_id": connector_id,
        "runnable": True,
        "stream_count": len(streams),
        "auth_note": "Auth is a template only (e.g. 'Bearer {token}') — no API key is stored in this draft.",
        "summary": draft.summary,
        "next_step": (
            "Show the user the drafted endpoints. NOTHING is live yet. To activate it, "
            "an admin approves the draft (POST /api/connectors/drafts/{draft_id}/approve), "
            "which saves it as a Beta connector; then they create a Connection from it and "
            "enter the API key there."
        ),
    }


DEFINITION = ToolDefinition(
    name="draft_connector_from_openapi",
    tier=ToolTier.SAFE_WRITE,
    description=(
        "Draft a new REST connector from an OpenAPI 3.x / Swagger 2 spec when the user "
        "wants to connect a system F-Pulse doesn't ship a connector for. Give the spec "
        "one of three ways: openapi_text (paste the JSON/YAML the user provided — the "
        "usual path for vendors like FactoHR that gate their spec behind a login and "
        "don't publish it), openapi_spec (an already-parsed dict), or openapi_url (a "
        "PUBLIC URL the server fetches). Returns a draft_id and the discovered "
        "endpoints. NOTHING GOES LIVE and NO CREDENTIALS ARE STORED — the manifest "
        "holds only auth templates. An admin must approve the draft (a separate human "
        "step) to activate it as a Beta connector; the API key is added later on the "
        "Connection. If the user has no spec at all, use draft_connector_from_samples "
        "with a few example API responses instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "connector_id": {
                "type": "string",
                "description": "Short id for the connector (letters, digits, underscore), e.g. 'factohr'.",
            },
            "display_name": {
                "type": "string",
                "description": "Optional human-friendly name, e.g. 'FactoHR'.",
            },
            "openapi_url": {
                "type": "string",
                "description": "PUBLIC URL to the OpenAPI/Swagger JSON or YAML. Fetched via an SSRF-hardened fetcher (private/loopback/metadata hosts blocked).",
            },
            "openapi_text": {
                "type": "string",
                "description": "Raw OpenAPI/Swagger spec as JSON or YAML text — paste what the user gave you when there's no public URL. Parsed server-side.",
            },
            "openapi_spec": {
                "type": "object",
                "description": "Already-parsed OpenAPI spec dict. Provide one of openapi_text / openapi_spec / openapi_url.",
            },
            "category": {
                "type": "string",
                "description": "Connector category (default 'saas').",
            },
            "idempotency_key": {
                "type": "string",
                "description": "Required. Format: {tier}.{user_id}.{action}.{target_id}.{semver}",
            },
        },
        "required": ["connector_id", "idempotency_key"],
    },
    output_schema={
        "draft_id": "str",
        "connector_id": "str",
        "runnable": "bool",
        "stream_count": "int",
        "auth_note": "str",
        "summary": "str",
        "next_step": "str",
    },
    handler=_handler,
    requires_idempotency_key=True,
    tags=["connector", "build", "draft", "openapi"],
)
