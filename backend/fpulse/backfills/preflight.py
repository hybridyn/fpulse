"""Preflight checks that run before a backfill is dispatched.

These are correctness checks — not safety guardrails (those live in
`idempotency.py` + the unsafe-sink block in `api/backfills.py`).

`check_cursor_param_usage` catches the most common silent-failure mode
of backfills: the orchestrator faithfully passes `${param.window_start}`
and `${param.window_end}` to every windowed run, but if no source step
in the pipeline actually REFERENCES those params (e.g. in a `WHERE`
clause on a db_source), every window reprocesses the same full dataset.
The user sees N green "success" rows in the Backfills panel and walks
away thinking the historical re-process worked — when in fact the same
data was re-written N times.

The check is a static scan of the workflow IR. It does not run the
pipeline. False negatives are possible if the user references the
cursor param via an indirect path (e.g. via a child template); the
check warns rather than blocks unless the caller chose to enforce.
"""
from __future__ import annotations

from typing import Any

# Step types that READ data from outside the pipeline. These are the
# nodes most likely to need a date-range filter when a backfill is in
# play. Sinks and transforms are deliberately excluded — they're
# downstream of any cursor filter, so they don't need the reference
# themselves.
#
# Keep aligned with backend/fpulse/ir/schema.py StepType source variants.
SOURCE_STEP_TYPES: frozenset[str] = frozenset({
    "source",            # generic source
    "csv_source",
    "db_source",
    "api_source",
    "json_source",
    "parquet_source",
    "excel_source",
    "xml_source",
    "file_source",
    "gsheet_source",
    "delta_source",
    "ftp_source",
    "kafka_source",
    "s3_source",
    "azure_blob_source",
    "gcs_source",
    "sharepoint_source",
    "onedrive_source",
    "gdrive_source",
    "dropbox_source",
    "box_source",
    "local_table_source",
    "microsoft_graph_source",
    # JDBC / CDC / vector / SaaS variants that load_manifests registers
    "saas_source",
    "jdbc_source",
    "cdc_source",
    "openapi_source",
    "vector_source",
    "warehouse_source",
    "warehouse_source_jdbc",
})


def _references_param(value: Any, param_name: str) -> bool:
    """Recursively check if `value` (str / dict / list / scalar) contains
    a `${param.<param_name>}` reference anywhere inside it.

    Matches the substitution pattern used by
    ``fpulse.engine.parameters.resolve_workflow_parameters`` — must stay
    aligned with whatever that function understands. Today that's the
    Mustache-style ``${param.NAME}`` token.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return f"${{param.{param_name}}}" in value
    if isinstance(value, dict):
        return any(_references_param(v, param_name) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_references_param(v, param_name) for v in value)
    return False


def _step_type(step: Any) -> str:
    """Normalize step.type to a string (it can be an Enum or already a str)."""
    t = getattr(step, "type", None)
    if t is None:
        return ""
    return t.value if hasattr(t, "value") else str(t)


def check_cursor_param_usage(
    workflow: Any,
    cursor_param_names: list[str],
) -> dict | None:
    """Return None when the pipeline correctly references the cursor
    params, or a dict describing the violation otherwise.

    Violation shape:
        {
            "code": "no_source_uses_cursor_param",
            "message": <human-readable>,
            "cursor_param_names": [...],
            "sources_checked": [{"id": ..., "label": ..., "type": ...}, ...],
        }

    Caller should either:
      - Reject the backfill (HTTP 400) with this dict in the response.
      - Surface it as a warning when ``acknowledge_no_cursor_usage`` is
        set by the user.
    """
    if not cursor_param_names:
        # Backfill with no cursor params at all — nothing to validate.
        return None

    steps = getattr(workflow, "steps", None) or []
    sources = [s for s in steps if _step_type(s) in SOURCE_STEP_TYPES]

    if not sources:
        # No source steps means the pipeline isn't reading external
        # data — backfill cursor usage is moot. Don't warn.
        return None

    any_source_uses_cursor = False
    for s in sources:
        params = getattr(s, "params", None) or {}
        if any(_references_param(params, n) for n in cursor_param_names):
            any_source_uses_cursor = True
            break

    if any_source_uses_cursor:
        return None

    # No source uses any cursor param → meaningful backfill is impossible.
    sample_param = cursor_param_names[0]
    sources_summary = [
        {
            "id": getattr(s, "id", ""),
            "label": getattr(s, "label", None) or getattr(s, "id", ""),
            "type": _step_type(s),
        }
        for s in sources
    ]
    return {
        "code": "no_source_uses_cursor_param",
        "message": (
            f"None of this pipeline's {len(sources)} source step(s) reference "
            f"the backfill cursor parameter(s) {cursor_param_names!r}. "
            f"Without a parameter reference (e.g. "
            f"`WHERE created_at >= '${{param.{sample_param}}}'` on a db_source, "
            f"or `?since=${{param.{sample_param}}}` on an api_source), every "
            f"backfill window will reprocess the same full dataset — the "
            f"backfill will appear to succeed but the windows won't actually "
            f"partition the work. Either edit a source to reference the "
            f"cursor params, or pass acknowledge_no_cursor_usage=true if you "
            f"know what you're doing."
        ),
        "cursor_param_names": list(cursor_param_names),
        "sources_checked": sources_summary,
    }
