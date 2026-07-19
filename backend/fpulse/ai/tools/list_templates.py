"""
list_templates tool — read-only.

Lets the Copilot recommend a template when the user describes what they
want to build ("I need to sync Postgres to a warehouse", "show me a
data-quality starter", "what AI templates do you have"). Returns metadata
for both built-in templates (the curated 10 that ship with the gallery)
AND user-saved templates (from the user_templates SQLite table).

Per ai-boundary-contract.md §2 — only id / name / tagline / description /
category / complexity / tags / node_count are returned. Step-level
configuration (auth, queries, file paths) is NEVER sent to the LLM.

The agent typically responds with the template ``id`` so the user can
click through to the Templates page → "Use this template" to load it.
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
    source = (inputs.get("source") or "all").strip().lower()
    complexity = (inputs.get("complexity") or "").strip().lower()
    name_contains = (inputs.get("name_contains") or "").strip().lower()
    limit = max(1, min(int(inputs.get("limit") or 20), 50))

    items: list[dict[str, Any]] = []

    # ── Built-in templates (the curated 10) ─────────────────────────────
    if source in ("all", "builtin"):
        try:
            from fpulse.templates.catalog_meta import BUILTIN_TEMPLATES
            for t in BUILTIN_TEMPLATES:
                if complexity and t["complexity"] != complexity:
                    continue
                if name_contains:
                    haystack = (t["name"] + " " + t["tagline"] + " " + t["description"]).lower()
                    if name_contains not in haystack:
                        continue
                items.append({
                    "id": t["id"],
                    "name": t["name"],
                    "tagline": t["tagline"],
                    "description": t["description"],
                    "complexity": t["complexity"],
                    "category": t["category"],
                    "tags": t["tags"],
                    "node_count": t["node_count"],
                    "source": "builtin",
                })
        except Exception:
            # Catalog import failure is non-fatal — agent still gets user templates.
            pass

    # ── User-saved templates (from user_templates SQLite table) ─────────
    if source in ("all", "user"):
        try:
            from fpulse.main import app_state  # type: ignore
            db = app_state.get("db")
            if db is not None:
                cur = db.execute_with_retry(
                    """SELECT id, name, tagline, description, category, data,
                              created_at, updated_at
                         FROM user_templates
                        WHERE workspace_id = ?
                     ORDER BY updated_at DESC""",
                    [workspace_id],
                )
                rows = cur.fetchall() if cur else []
                import json as _json
                for r in rows:
                    (id_, name, tagline, description, category, data_json, created_at, updated_at) = r
                    body = {}
                    try:
                        body = _json.loads(data_json) if data_json else {}
                    except Exception:
                        body = {}
                    node_count = len(body.get("steps", []) or [])
                    derived_complexity = "complex" if node_count >= 4 else "simple"
                    if complexity and derived_complexity != complexity:
                        continue
                    if name_contains:
                        haystack = (name + " " + (tagline or "") + " " + (description or "")).lower()
                        if name_contains not in haystack:
                            continue
                    items.append({
                        "id": id_,
                        "name": name,
                        "tagline": tagline or "",
                        "description": description or "",
                        "complexity": derived_complexity,
                        "category": category or "Custom",
                        "tags": ["Yours"],
                        "node_count": node_count,
                        "source": "user",
                        "created_at": created_at,
                        "updated_at": updated_at,
                    })
        except Exception:
            # Best-effort — don't fail the whole tool because the table
            # doesn't exist on a never-restarted instance, etc.
            pass

    truncated = len(items) > limit
    items = items[:limit]

    return {
        "templates": items,
        "count": len(items),
        "truncated": truncated,
        "filters_applied": {
            "source": source,
            "complexity": complexity or None,
            "name_contains": name_contains or None,
        },
        "workspace_id": workspace_id,
    }


DEFINITION = ToolDefinition(
    name="list_templates",
    tier=ToolTier.READ,
    description=(
        "List pipeline templates the user can drop onto the canvas — both "
        "built-in starters and templates the user has saved themselves. "
        "Use this when the user asks 'what templates exist', 'show me a "
        "starter for X', 'do you have a template for Postgres → warehouse', "
        "'what's a good pattern for data quality / reverse ETL / fan-out / "
        "AI classification', or describes a use case they want a starting "
        "point for. Each result has an `id` you can recommend — the user "
        "loads it from the Templates page (Workflows → Templates). "
        "Filters: source ('builtin' / 'user' / 'all'), complexity ('simple' / "
        "'complex'), name_contains (substring match across name + tagline + "
        "description)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "enum": ["all", "builtin", "user"],
                "description": "Which library to search. 'builtin' = the curated 10 starters; 'user' = templates this workspace has saved; 'all' = both. Default 'all'.",
            },
            "complexity": {
                "type": "string",
                "enum": ["simple", "complex"],
                "description": "Filter by complexity. Simple = linear single-source pipelines; Complex = branching / multi-source / AI-augmented.",
            },
            "name_contains": {
                "type": "string",
                "description": "Case-insensitive substring matched against template name, tagline, and description. Use a keyword like 'postgres', 'warehouse', 'ai', 'webhook', 'quality'.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default 20, max 50).",
                "default": 20,
            },
            "workspace_id": {
                "type": "string",
                "description": "Workspace to scope user templates to. Optional — defaults to caller's workspace.",
            },
        },
    },
    output_schema={
        "templates": "list",
        "count": "int",
        "truncated": "bool",
        "filters_applied": "dict",
        "workspace_id": "str",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["template", "discovery", "read"],
)
