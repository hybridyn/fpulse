"""Canonical per-node contract metadata.

This is the **backend half** of the node contract. The frontend's
``frontend/src/utils/nodeArity.ts`` carries the same shape; both files
are kept hand-in-sync until the Phase 2 frontend refactor pulls all
node metadata from the backend at startup.

What lives here:

  * ``INPUT_CONTRACTS`` — per-step-type input cardinality (required,
    optional, variadic). The executor uses this to validate the DAG;
    ``/api/node-types`` exposes it so the UI can draw the right number
    of input handles.
  * ``SIDE_EFFECT_CLASS`` — classify the node's external effects as
    ``passthrough`` / ``transforming`` / ``terminal`` / pure (absent).
    Lets the run-replay viewer and impact-card decide whether replay
    is safe.

What does NOT live here:

  * Required *param* keys per node — those live on the node class
    (``param_schema`` static method) and in the backend ``validator.py``.
  * The deprecation registry — see ``fpulse.ir.migrations``.

When you add a new node class, update this file in lockstep with
``nodeArity.ts`` and the node-conformance test will pin the contract.
"""

from __future__ import annotations

from typing import TypedDict


# ──────────────────────────────────────────────────────────────────────
# Input contract per step type
# ──────────────────────────────────────────────────────────────────────


class InputContract(TypedDict):
    """Formal input cardinality. Mirrors the frontend ``InputContract``
    interface in ``nodeArity.ts``."""

    required: int
    optional: int
    variadic: bool


# Nodes that genuinely consume 2+ inputs.
MULTI_INPUT_NODES: frozenset[str] = frozenset({
    "join", "union", "scd2", "lookup",
})

# Source-like nodes that take no upstream.
NO_INPUT_NODES: frozenset[str] = frozenset({
    "source",
    "csv_source", "json_source", "excel_source", "xml_source",
    "parquet_source", "db_source", "api_source", "s3_source", "kafka_source",
    "ftp_source", "gsheet_source", "delta_source", "file_source",
    "sharepoint_source", "onedrive_source", "gdrive_source", "dropbox_source",
    "box_source", "azure_blob_source", "adls_gen2_source", "gcs_source",
    "webhook_trigger", "http_request",
    # 2026-05-22 — Microsoft Graph (generic) source. Same arity as
    # any other API-backed source: no upstream, produces rows.
    "microsoft_graph_source",
    # 2026-05-23 (Y3) — managed local table source. Reads from
    # tables/{ws}/{schema}/{name}/part-*.parquet by name; no upstream.
    "local_table_source",
})

# Explicit overrides for nodes that don't fit the "1 in, 1 out" default.
_CONTRACTS: dict[str, InputContract] = {
    # Set-combiners
    "join":   {"required": 2, "optional": 0, "variadic": False},
    "lookup": {"required": 2, "optional": 0, "variadic": False},
    "union":  {"required": 2, "optional": 0, "variadic": True},
    "scd2":   {"required": 1, "optional": 1, "variadic": False},
    # Control-flow that can run with or without data
    "append_variable": {"required": 0, "optional": 1, "variadic": False},
    "filter_array":    {"required": 0, "optional": 1, "variadic": False},
    "validation":      {"required": 0, "optional": 1, "variadic": False},
    "fail":            {"required": 0, "optional": 1, "variadic": False},
    # Action nodes — input is optional
    "copy_data":        {"required": 0, "optional": 1, "variadic": False},
    "file_system":      {"required": 0, "optional": 1, "variadic": False},
    "execute_sql_task": {"required": 0, "optional": 1, "variadic": False},
    "http_request":     {"required": 0, "optional": 1, "variadic": False},
    # Lookup activity: connection/query mode is self-contained (no
    # upstream); upstream mode consumes an optional input. It must NOT default
    # to 1 required input, or the canvas/validator falsely demands an edge.
    "lookup_activity":  {"required": 0, "optional": 1, "variadic": False},
    # SQL transform: 1 required primary input (registered as `source_table`
    # / `input`) + unbounded variadic extras (each a named DuckDB table).
    # Mirrors the frontend contract (frontend/src/utils/nodeArity.ts) and the
    # executor's multi-input handling in fpulse/nodes/transform.py — without
    # this entry the default contract treated Transform as single-input,
    # contradicting the canvas which already allows multiple incoming edges.
    "transform":        {"required": 1, "optional": 0, "variadic": True},
}

_SOURCE_CONTRACT: InputContract = {"required": 0, "optional": 0, "variadic": False}
_TRANSFORM_CONTRACT: InputContract = {"required": 1, "optional": 0, "variadic": False}


def contract_for(step_type: str) -> InputContract:
    """Return the input contract for a step type.

    Defaults to source contract (0 in) for known source types, transform
    contract (1 in) otherwise. The override map handles set-combiners,
    control-flow, and any other multi-input or optional-input cases.
    """
    if step_type in _CONTRACTS:
        return _CONTRACTS[step_type]
    if step_type in NO_INPUT_NODES:
        return _SOURCE_CONTRACT
    return _TRANSFORM_CONTRACT


# ──────────────────────────────────────────────────────────────────────
# Side-effect classification
# ──────────────────────────────────────────────────────────────────────
#
# Three classes:
#   passthrough  — input relation passes through unchanged; the side
#                  effect (write/send/publish) happens in parallel.
#   transforming — produces a NEW relation that reflects the side
#                  effect (http_request merges response, copy_data
#                  emits row count, get_metadata emits stats, etc.)
#   terminal     — emits a small descriptive relation that downstream
#                  can't meaningfully chain (send_email, slack_notify).
# Pure nodes (filter, aggregate, embedder, …) are NOT in this map.

SideEffectClass = str  # Literal["passthrough", "transforming", "terminal"]

SIDE_EFFECT_CLASS: dict[str, SideEffectClass] = {
    # Sinks — passthrough
    "csv_sink": "passthrough", "json_sink": "passthrough", "excel_sink": "passthrough",
    # NOTE: parquet writes are funnelled through `local_table_sink`,
    # `s3_sink`, or generic `destination` with file_format=parquet — there
    # is intentionally no PARQUET_SINK StepType (only PARQUET_SOURCE).
    "db_sink": "passthrough", "s3_sink": "passthrough",
    "kafka_sink": "passthrough", "api_sink": "passthrough", "email_sink": "passthrough",
    "delta_sink": "passthrough", "warehouse_sink": "passthrough", "file_sink": "passthrough",
    "ftp_sink": "passthrough",
    "sharepoint_sink": "passthrough", "onedrive_sink": "passthrough",
    "gdrive_sink": "passthrough", "dropbox_sink": "passthrough", "box_sink": "passthrough",
    "adls_gen2_sink": "passthrough", "azure_blob_sink": "passthrough", "gcs_sink": "passthrough",
    "webhook_sink": "passthrough",
    "output": "passthrough", "destination": "passthrough",
    # 2026-05-23 (Y3): managed local Parquet sink — passthrough of the
    # input relation while the bytes write to disk in parallel.
    "local_table_sink": "passthrough",
    # Transforming — output reflects the side effect
    "http_request": "transforming",
    "copy_data": "transforming",
    "delete_data": "transforming",
    "get_metadata": "transforming",
    "execute_sql_task": "transforming",
    "file_system": "transforming",
    "execute_pipeline": "transforming",
    # Terminal — no meaningful data continuation
    "send_email": "terminal",
    "slack_notify": "terminal",
    "fail": "terminal",
}


def side_effect_class_for(step_type: str) -> SideEffectClass | None:
    """Return the side-effect class or None if the node is pure."""
    return SIDE_EFFECT_CLASS.get(step_type)


# ──────────────────────────────────────────────────────────────────────
# Output-kind classification (2026-06-18)
# ──────────────────────────────────────────────────────────────────────
#
# What a node PRODUCES for downstream consumers. The side-effect class
# answers "does it touch the outside world?"; this answers "what comes out
# of its output port?" so the config UI's Data Out band reads truthfully
# instead of labelling every non-sink node as "produces rows".
#
#   dataset      — a table of rows (sources, pure transforms, joins, and the
#                  action nodes whose output reflects the result: http_request,
#                  copy_data, execute_sql_task, execute_pipeline, …).
#   variable     — writes a runtime variable; the input relation passes through.
#   report       — statistics / metadata, not a row dataset to transform on.
#   branch       — routes rows / flow to more than one named output.
#   side_effect  — writes externally (sinks) or sends a notification; no
#                  meaningful NEW dataset to continue.
#   terminal     — ends the run (fail).
#   control      — orchestrates execution (loops, waits); no dataset output.

OutputKind = str  # Literal["dataset","variable","report","branch","side_effect","terminal","control"]

OUTPUT_KIND: dict[str, OutputKind] = {
    # Variable producers
    "set_variable": "variable",
    "append_variable": "variable",
    "filter_array": "variable",
    "lookup_activity": "variable",
    # Report / metadata
    "data_profile": "report",
    "get_metadata": "report",
    # Branching / routing
    "if_condition": "branch",
    "switch_case": "branch",
    "conditional_split": "branch",
    # Terminal
    "fail": "terminal",
    # Control-flow orchestrators — no row dataset out
    "wait_delay": "control",
    "foreach_loop": "control",
    "foreach_pipeline": "control",
    "until_loop": "control",
    # Action notifications — external send, nothing meaningful continues
    "send_email": "side_effect",
    "slack_notify": "side_effect",
}


def output_kind_for(step_type: str) -> OutputKind:
    """Classify what a node produces downstream. Defaults: external writers
    (sinks, passthrough side-effect) are ``side_effect``; everything else
    yields a ``dataset``."""
    if step_type in OUTPUT_KIND:
        return OUTPUT_KIND[step_type]
    if SIDE_EFFECT_CLASS.get(step_type) == "passthrough":
        return "side_effect"
    return "dataset"


def has_side_effect(step_type: str) -> bool:
    """True iff the node touches the outside world (any class)."""
    return step_type in SIDE_EFFECT_CLASS


__all__ = [
    "InputContract",
    "MULTI_INPUT_NODES",
    "NO_INPUT_NODES",
    "SIDE_EFFECT_CLASS",
    "SideEffectClass",
    "OUTPUT_KIND",
    "OutputKind",
    "contract_for",
    "has_side_effect",
    "side_effect_class_for",
    "output_kind_for",
]
