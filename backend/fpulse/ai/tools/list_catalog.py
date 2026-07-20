"""
list_catalog tool — read-only.

Lets the Copilot answer "what connectors do you support?" / "which streaming
sinks are available?" / "is Snowflake on Plus or OSS?" without making the
agent reach into private internals — it pulls from the same public
`/api/v1/catalog` registry that external integrations use.

Filter args narrow the catalog so the agent can return small, focused
results (~20 connectors max) rather than dumping the whole catalog.
Deliberately no hardcoded total here — the count drifts and the live
registry is the only honest source. See docs/product_facts/08_connectors.md
for the shipped breakdown.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


def _detect_install_tier() -> str:
    """Resolve whether this F-Pulse instance is running as OSS-only or Plus.

    Plus injects a LicenseManager into app_state at startup; OSS never does.
    Absence of the license manager → free tier. Source of truth is
    ``app_state['license_manager']`` per backend/fpulse/api/auth.py.
    """
    try:
        from fpulse.main import app_state  # type: ignore
        license_mgr = app_state.get("license_manager")
        if license_mgr is not None and getattr(license_mgr, "is_plus", False):
            return "plus"
    except Exception:
        pass
    return "free"


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    category = (inputs.get("category") or "").strip().lower()
    capability = (inputs.get("capability") or "").strip().lower()
    tier = (inputs.get("tier") or "").strip().lower()
    name_contains = (inputs.get("name_contains") or "").strip().lower()
    limit = max(1, min(int(inputs.get("limit") or 20), 50))

    # Inline the public catalog logic so this tool works offline (no HTTP
    # call required). The catalog endpoint reads from the same constants.
    from fpulse.api.catalog import (
        _CONNECTOR_CATEGORIES,
        _CONNECTOR_CAPABILITIES,
        _CONNECTOR_VERSIONS,
        _DEFAULT_CAPS,
        _PLUS_ONLY_CONNECTORS,
    )
    from fpulse.connections.models import CONNECTION_TYPES

    # Tier-aware default filtering: on a Free OSS install, hide Plus-only
    # connectors UNLESS the user explicitly asked to see them via tier="plus"
    # (e.g. "what connectors are in F-Pulse Plus"). This prevents the agent
    # from leaking enterprise upsell content into routine OSS responses
    # (per EDITION_MATRIX boundary rule — Plus features stay invisible to
    # OSS users by default; explicit asks still get an honest answer).
    install_tier = _detect_install_tier()
    suppress_plus = install_tier == "free" and tier != "plus"

    items: list[dict[str, Any]] = []
    plus_hidden_count = 0
    for c in CONNECTION_TYPES:
        ver_meta = _CONNECTOR_VERSIONS.get(c, {})
        version = ver_meta.get("version", "v1")
        capabilities = _CONNECTOR_CAPABILITIES.get(c, _DEFAULT_CAPS)
        connector_tier = "plus" if c in _PLUS_ONLY_CONNECTORS else "oss"
        connector_category = _CONNECTOR_CATEGORIES.get(c, "other")

        if category and connector_category != category:
            continue
        if capability and capability not in capabilities:
            continue
        if tier and connector_tier != tier:
            continue
        if name_contains and name_contains not in c.lower():
            continue
        if suppress_plus and connector_tier == "plus":
            plus_hidden_count += 1
            continue

        items.append({
            "id": f"{c}_{version}",
            "name": c,
            "category": connector_category,
            "tier": connector_tier,
            "capabilities": capabilities,
            "version": version,
            "deprecated": bool(ver_meta.get("deprecated", False)),
        })

    truncated = len(items) > limit
    items = items[:limit]

    return {
        "connectors": items,
        "count": len(items),
        "truncated": truncated,
        "install_tier": install_tier,
        "plus_hidden_count": plus_hidden_count,
        "filters_applied": {
            "category": category or None,
            "capability": capability or None,
            "tier": tier or None,
            "name_contains": name_contains or None,
        },
    }


DEFINITION = ToolDefinition(
    name="list_catalog",
    tier=ToolTier.READ,
    description=(
        "List connectors from the public F-Pulse catalog with optional filters. "
        "Use this when the user asks 'what connectors / sources / sinks does "
        "F-Pulse support', 'do you support Snowflake / SAP / Kafka', 'which "
        "streaming destinations are available', 'is Salesforce on OSS or Plus'. "
        "Filters compose: category + capability + tier + name_contains."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Filter by category: relational | warehouse | nosql | streaming | api | saas | object_storage | filesystem | collaboration | other.",
            },
            "capability": {
                "type": "string",
                "enum": ["read", "write", "stream"],
                "description": "Filter to connectors with a specific capability.",
            },
            "tier": {
                "type": "string",
                "enum": ["oss", "plus"],
                "description": "Filter by tier — 'oss' (free) or 'plus' (Plus-only).",
            },
            "name_contains": {
                "type": "string",
                "description": "Substring match on connector name (case-insensitive).",
            },
            "limit": {
                "type": "integer",
                "description": "Max items to return (default 20, max 50).",
                "default": 20,
            },
        },
    },
    output_schema={
        "connectors": "list",
        "count": "int",
        "truncated": "bool",
        "install_tier": "str",
        "plus_hidden_count": "int",
        "filters_applied": "dict",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["catalog", "discovery", "read"],
)
