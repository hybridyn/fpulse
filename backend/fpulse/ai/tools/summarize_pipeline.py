"""
summarize_pipeline tool — read-only.

Returns counts + connector type histograms + alert presence + last run status
+ declared parameters for a given pipeline. Sample-data and credentials never
sent to LLM (per docs/ai-boundary-contract.md §2).

Falls back to a sample shape when no workflow store is wired (test mode).
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


_SOURCE_PREFIXES = ("csv_source", "db_source", "api_source", "json_source",
                    "parquet_source", "excel_source", "xml_source", "s3_source",
                    "adls_gen2_source")
_SINK_PREFIXES = ("file_sink", "db_sink", "csv_sink", "json_sink", "excel_sink",
                  "s3_sink", "kafka_sink", "api_sink", "delta_sink",
                  "warehouse_sink", "adls_gen2_sink", "output")


def _step_type(s) -> str:
    t = getattr(s, "type", None) or getattr(s, "step_type", None) or "unknown"
    return getattr(t, "value", t) if t else "unknown"


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pipeline_id = inputs.get("pipeline_id", "")
    # Default to the pipeline the user has open / selected when they ask
    # about "this pipeline" without naming an ID. selected_ids takes priority
    # (explicit user click) over visible_ids (whatever's on screen).
    if not pipeline_id and ctx.selected_ids:
        pipeline_id = ctx.selected_ids[0]
    if not pipeline_id and ctx.visible_ids and len(ctx.visible_ids) == 1:
        pipeline_id = ctx.visible_ids[0]
    if not pipeline_id:
        raise ValueError("pipeline_id is required (no pipeline selected on the current page)")

    # Resolve workflow store via app_state. Falls through to a stub when
    # the store isn't wired (unit tests / pre-app-state environments).
    try:
        from fpulse.main import app_state  # type: ignore
        store = app_state.get("workflow_store")
    except Exception:
        store = None

    if store is None:
        return {
            "node_count": 0,
            "source_types": [],
            "destination_types": [],
            "alerts_configured": False,
            "last_run_status": "unknown",
            "parameters": [],
            "message": "Workflow store unavailable; live data not loaded.",
        }

    workspace_id = ctx.workspace_id or "default"
    try:
        wv = store.get(pipeline_id, workspace_id=workspace_id)
    except TypeError:
        # Older store signatures may not accept workspace_id kwarg.
        wv = store.get(pipeline_id)
    if wv is None or wv.workflow is None:
        return {
            "node_count": 0,
            "source_types": [],
            "destination_types": [],
            "alerts_configured": False,
            "last_run_status": "unknown",
            "parameters": [],
            "message": f"Pipeline {pipeline_id!r} not found in this workspace.",
        }

    wf = wv.workflow
    steps = list(getattr(wf, "steps", []) or [])

    # Bucket source / destination types into deduplicated lists.
    source_types: list[str] = []
    destination_types: list[str] = []
    seen_src: set[str] = set()
    seen_dst: set[str] = set()
    for s in steps:
        t = _step_type(s)
        if t in _SOURCE_PREFIXES and t not in seen_src:
            source_types.append(t)
            seen_src.add(t)
        elif t in _SINK_PREFIXES and t not in seen_dst:
            destination_types.append(t)
            seen_dst.add(t)

    md = getattr(wf, "metadata", None) or {}
    alerts_configured = bool(md.get("alert_rule_ids") or md.get("alerts"))

    last_run_status = "unknown"
    try:
        last_run_status = (
            wf.test_results.get("status")
            if getattr(wf, "test_results", None)
            else "unknown"
        ) or "unknown"
    except Exception:
        last_run_status = "unknown"

    # Surface declared pipeline parameters so the Copilot can answer
    # "what parameters does this pipeline take?" / "what's the default
    # for batch_size?" without a second tool round-trip. Conservative
    # field set — no internal fields, just the user-visible declaration.
    parameters_summary: list[dict[str, Any]] = []
    for p in (getattr(wf, "parameters", []) or []):
        parameters_summary.append({
            "name": getattr(p, "name", ""),
            "type": getattr(p, "type", "string"),
            "default": getattr(p, "default", None),
            "description": getattr(p, "description", ""),
            "required": bool(getattr(p, "required", False)),
        })

    return {
        "node_count": len(steps),
        "source_types": source_types,
        "destination_types": destination_types,
        "alerts_configured": alerts_configured,
        "last_run_status": last_run_status,
        "parameters": parameters_summary,
    }


DEFINITION = ToolDefinition(
    name="summarize_pipeline",
    tier=ToolTier.READ,
    description=(
        "Get a high-level summary of a pipeline by ID — node count, source and "
        "destination connector types, whether alerts are configured, the last "
        "run status, and the declared parameters (name / type / default / "
        "description / required). Use this when the user asks about a specific "
        "pipeline and you need an overview, or 'what parameters does X take'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pipeline_id": {
                "type": "string",
                "description": "The pipeline UUID or slug to summarize.",
            },
        },
        "required": ["pipeline_id"],
    },
    output_schema={
        "node_count": "int",
        "source_types": "list",
        "destination_types": "list",
        "alerts_configured": "bool",
        "last_run_status": "str",
        "parameters": "list",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["pipeline", "read"],
)
