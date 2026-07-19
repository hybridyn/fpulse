"""
query_metrics tool — read-only.

Returns aggregated metrics (run counts, success rate, avg duration, etc.)
for a given scope and window. Returns numbers + scope label only — never
raw row data.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


_WINDOW_MAP = {
    "last_24h": "24h",
    "last_7d": "7d",
    "last_30d": "30d",
}


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    scope = inputs.get("scope", "workspace")
    keys = inputs.get("keys", ["runs", "success_rate"])
    window = inputs.get("window", "last_24h")
    if window not in _WINDOW_MAP:
        raise ValueError(f"Unsupported window {window!r}; use one of {list(_WINDOW_MAP)}")

    # Step 1.5a stub: well-shaped sample. Step 7 wires real metrics aggregator.
    sample = [
        {"key": k, "value": (42 if k == "runs" else 0.95 if k == "success_rate" else 0.0), "window": window, "scope": scope}
        for k in keys
    ]
    return {
        "metrics": sample,
        "window": window,
    }


DEFINITION = ToolDefinition(
    name="query_metrics",
    tier=ToolTier.READ,
    description=(
        "Query aggregated metrics for the workspace, a specific project, or a "
        "specific pipeline. Returns counts and ratios — never raw row data. "
        "Available keys: runs, success_rate, avg_duration_ms, p95_duration_ms, "
        "errors_total. Available windows: last_24h, last_7d, last_30d."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["workspace", "project", "pipeline"],
                "description": "What scope to aggregate over.",
            },
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Metric keys to fetch.",
            },
            "window": {
                "type": "string",
                "enum": ["last_24h", "last_7d", "last_30d"],
            },
        },
        "required": ["scope", "keys", "window"],
    },
    output_schema={
        "metrics": "list",
        "window": "str",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["metrics", "read"],
)
