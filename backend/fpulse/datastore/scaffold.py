"""Scaffold Data Prep flows backed by the pipeline runtime.

The Storage page and the Connections catalog both want a one-click path
from "dirty data" to "Data Wrangler ready to fill in". The runtime shape
is still workflow JSON because the executor already understands
Source → Data Wrangler → Sink, but the frontend presents different
surfaces:

* storage_file → one-time/manual file Data Prep that loads a managed table
* connection_stream → Pipeline Data Prep that runs on every execution

Three nodes, two connections, empty Wrangler steps. The user fills in
the prep recipe in the canvas; the source + sink are pre-wired.

Pure builders — no DB access, no filesystem I/O — so the units are
trivially testable. The API endpoints (storage.py / connections.py)
call these helpers, then return the dict for the frontend to stash in
``sessionStorage['fpulse_pending_import']`` before navigating to
``#editor`` (the same handoff Y8 uses for pipeline-JSON files).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

# Source detection — extension → (connector_type, params overlay).
#
# 2026-05-23 (post-validation polish): the table previously emitted
# per-format StepTypes directly (csv_source / json_source / parquet_source
# / excel_source / xml_source). Those types are in the frontend's
# `SOURCE_REMAP` legacy migration table (migrateLegacyNodes.ts) and get
# silently rewritten to the generic `source` step + `connector_type`
# param on Editor load. The scaffold now emits the **modern** shape
# directly — generic `source` with the connector_type set — so no
# migration runs on import, no rewrite toast, and the canvas form
# matches what users get when they drag a Source from the palette.
#
# All five formats still resolve to the same backend executor paths;
# the dispatch happens through `connector_type` instead of step type.
_FILE_FORMAT_TO_CONNECTOR: dict[str, tuple[str, dict[str, Any]]] = {
    "csv":     ("csv",     {"delimiter": ",",  "header": True}),
    "tsv":     ("csv",     {"delimiter": "\t", "header": True}),
    "txt":     ("csv",     {"delimiter": ",",  "header": True}),
    "json":    ("json",    {"format": "auto"}),
    "ndjson":  ("json",    {"format": "lines"}),
    "jsonl":   ("json",    {"format": "lines"}),
    "parquet": ("parquet", {}),
    "pq":      ("parquet", {}),
    "excel":   ("excel",   {}),
    "xlsx":    ("excel",   {}),
    "xls":     ("excel",   {}),
    "xml":     ("xml",     {}),
}


_SNAKE_RE = re.compile(r"[^a-z0-9]+")

# Real file extensions we know how to strip. Anything else after a dot
# is treated as part of the stem (e.g. `public.orders` → `public_orders`,
# a DB-qualified name, not a file with extension `.orders`).
_KNOWN_FILE_EXTS = frozenset(_FILE_FORMAT_TO_CONNECTOR.keys()) | {
    # Tolerate a few extras the format-map doesn't enumerate but which
    # would still be uploaded as data files.
    "tsv", "ndjson", "jsonl", "xls", "xml",
}


def suggest_table_name(filename_or_stream: str) -> str:
    """Turn ``Orders Q1 2026.csv`` → ``orders_q1_2026``.

    Lowercase, strip a real file extension (only if recognized — see
    ``_KNOWN_FILE_EXTS``), collapse non-alphanum runs to a single
    underscore, trim leading/trailing underscores.

    2026-05-25 — the previous implementation stripped EVERYTHING after
    the last dot, which broke DB-qualified names: ``public.orders``
    became ``public`` instead of ``public_orders``. Now we only strip
    when the suffix is a known data-file extension; otherwise the dot
    is treated as a name separator and snake-cased like any other
    non-alphanum character.
    """
    raw = filename_or_stream or ""
    stem = raw
    if "." in raw:
        head, _, tail = raw.rpartition(".")
        if tail.lower() in _KNOWN_FILE_EXTS:
            stem = head
    snake = _SNAKE_RE.sub("_", stem.lower()).strip("_")
    return snake or "untitled"


def suggest_schema_from_connection(connection_name: str) -> str:
    """Default schema name for connection-sourced Pipeline Data Prep.

    Reuses the connection's name, snake-cased. The user can override
    in the canvas before running — this is just a sensible starting
    point.

    2026-05-25 — fall back to the literal ``"default"`` schema name
    when the input is empty. The prior `... or "default"` chain never
    fired because `suggest_table_name("")` returns ``"untitled"``
    (truthy), so an empty connection name produced ``"untitled"``
    instead of the canonical ``"default"`` schema F-Pulse uses for
    workspace-global tables.
    """
    if not (connection_name or "").strip():
        return "default"
    return suggest_table_name(connection_name)


def _gen_step_id(prefix: str = "step") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _new_workflow_id() -> str:
    return uuid.uuid4().hex


def build_file_cleanup_workflow(
    *,
    workspace_id: str,
    file_name: str,
    file_path: str,
    file_format: str | None,
    target_schema: str = "default",
    target_table: str | None = None,
    project_id: str | None = None,
    source_object_id: str | None = None,
) -> dict[str, Any]:
    """Emit a workflow JSON: file source → Data Wrangler → Managed Table Sink.

    The Wrangler step starts with ``steps: []`` — the user fills in the
    cleanup recipe in the canvas. Sink mode defaults to ``replace`` so
    repeated runs from the same scaffold overwrite the prior part-000.

    ``file_path`` is the data-dir-relative path (e.g.
    ``uploads/default/orders.csv``) — the CSV/JSON source nodes resolve
    it against ``ctx.data_dir`` at runtime.

    Returns the dict shape expected by ``Toolbar.processImportedJson``
    (flat form, format_version 1) — the frontend wraps in an envelope
    if needed.
    """
    fmt = (file_format or "").lower()
    connector_type, source_overlay = _FILE_FORMAT_TO_CONNECTOR.get(
        fmt, ("csv", {"delimiter": ",", "header": True})
    )

    table_name = target_table or suggest_table_name(file_name)
    src_id = _gen_step_id("src")
    wrg_id = _gen_step_id("wrg")
    snk_id = _gen_step_id("snk")

    # Step positions — laid out left-to-right so the user lands on a
    # readable canvas, not a stack of overlapping nodes.
    return {
        "id": _new_workflow_id(),
        "name": f"{file_name} data prep",
        "description": (
            f"File Data Prep draft for {file_name}. "
            "Fill in the Data Wrangler steps to clean the rows, then save "
            "the result into a managed table."
        ),
        "workspace_id": workspace_id,
        "project_id": project_id,
        "parameters": [],
        "connection_definitions": [],
        "steps": [
            {
                # Generic Source step — same shape the Source palette
                # node produces. Format-specific behaviour is selected
                # via the `connector_type` param so the canvas opens
                # without triggering the legacy-source migration in
                # migrateLegacyNodes.ts (which would silently rewrite
                # csv_source/parquet_source/etc. and surface a remap
                # toast). See SOURCE_REMAP for the full legacy list.
                "id": src_id,
                "type": "source",
                "label": file_name,
                "position": {"x": 80, "y": 200},
                "params": {
                    "connector_type": connector_type,
                    "file_path": file_path,
                    **source_overlay,
                },
            },
            {
                "id": wrg_id,
                "type": "data_wrangler",
                "label": "Data Prep",
                "position": {"x": 420, "y": 200},
                "params": {
                    "steps": [],
                    "_input_step_ids": [src_id],
                },
            },
            {
                "id": snk_id,
                "type": "local_table_sink",
                "label": f"{target_schema}.{table_name}",
                "position": {"x": 760, "y": 200},
                "params": {
                    "schema_name": target_schema,
                    "table_name": table_name,
                    "mode": "replace",
                    "_input_step_ids": [wrg_id],
                },
            },
        ],
        "connections": [
            {"from_step": src_id, "to_step": wrg_id, "from_port": "output", "to_port": "input"},
            {"from_step": wrg_id, "to_step": snk_id, "from_port": "output", "to_port": "input"},
        ],
        # Tag this so the frontend can present one-time file Data Prep
        # rather than normal pipeline authoring chrome. Z33: also carry
        # the source file's object_id so the local_table_sink can
        # stamp it on the resulting managed table (back-link for the
        # Storage Files tab's "Prepared as schema.name" badge).
        "metadata": {
            "scaffolded_from": "storage_file",
            "source_object_name": file_name,
            "source_object_id": source_object_id,
        },
    }


# Z32 (2026-05-23) — `build_connection_cleanup_workflow` removed with
# the frontend wand that drove its API route. Only consumer was the
# Connections-page Pipeline Data Prep affordance which the user flagged
# as unhelpful. The file-side scaffolder (build_file_cleanup_workflow)
# is unaffected — that's the Storage Z1 path which remains.


__all__ = [
    "build_file_cleanup_workflow",
    "suggest_schema_from_connection",
    "suggest_table_name",
]
