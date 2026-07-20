"""Pydantic models for the workspace datastore.

Three types of metadata row:

  * ``StorageObject`` — one row per file the user (or pipeline) put in
    the datastore. Files in uploads/, outputs/, and trash/ all map to
    StorageObject rows. The ``kind`` field distinguishes them, the
    ``deleted_at`` field marks soft-deleted entries.

  * ``StorageTable`` — one row per managed Parquet table. A table is a
    folder of one or more ``part-*.parquet`` files plus this metadata
    row giving it a name (``schema.table``) and tracking row/size stats.

  * ``StorageColumn`` — one row per column in a managed table OR cached
    schema-infer result for a file. Cheap to recompute, so we re-infer
    when the underlying file's mtime is newer than the row.

Backend field (``local`` / ``s3`` / ``azure_blob`` / …) is on every
row from day one so a future Plus add-on can layer remote object
backends without a schema migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


# Object kinds — explicit constants so consumers don't pass arbitrary strings.
OBJECT_KIND_FILE = "file"       # raw uploaded file (uploads/)
OBJECT_KIND_OUTPUT = "output"   # pipeline-generated artifact (outputs/)


# Sink modes for local_table_sink — mirrored on the frontend dropdown.
SINK_MODE_REPLACE = "replace"
SINK_MODE_APPEND = "append"
SINK_MODE_MERGE = "merge"


_FILE_FORMATS = {
    "csv", "tsv", "txt", "json", "ndjson", "jsonl",
    "parquet", "pq", "xlsx", "xls", "xml",
}


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class StorageObject(BaseModel):
    """A file in the datastore (upload OR pipeline output OR trash).

    Path is always relative to the data_dir root so the metadata row
    survives a data_dir move (e.g. running the same install from a
    different working directory). Bytes live on disk; this row is the
    index for everything queryable.
    """
    id: str = Field(default_factory=lambda: _gen_id("obj"))
    workspace_id: str = "default"
    kind: Literal["file", "output"] = OBJECT_KIND_FILE
    name: str  # display name (the original filename without dedup suffix)
    path: str  # relative to data_dir; e.g. "uploads/default/sales-20260523.csv"
    backend: str = "local"  # 'local' | 's3' | 'azure_blob' (Plus)
    format: str | None = None  # csv / json / parquet / excel / xml / etc
    size_bytes: int = 0
    row_count: int | None = None   # populated by preview/schema endpoints when known
    column_count: int | None = None
    sha256: str | None = None      # filled by the hashing helper on demand
    project_id: str | None = None  # None = workspace-global; set = project-scoped
    # 2026-05-23 (Y15): folder under the chosen project. OSS folders are
    # 1-level deep (see backend/fpulse/api/folders.py). None when:
    #   - project_id is None (Global has no folders), OR
    #   - the file is at the project root.
    folder_id: str | None = None
    pipeline_id: str | None = None  # set on output kind
    run_id: str | None = None       # set on output kind
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # When set, the file is in trash/ rather than its kind-specific dir.
    # Cleanup endpoint hard-deletes rows whose deleted_at < cutoff.
    deleted_at: datetime | None = None


class StorageTable(BaseModel):
    """A managed Parquet-backed table in the datastore.

    Identified by ``(workspace_id, schema, name)`` — the three together
    are unique. The bytes live under
    ``tables/{workspace_id}/{schema}/{name}/part-*.parquet``.

    Created via the "promote file to managed table" flow OR by the
    ``local_table_sink`` node writing fresh rows.
    """
    id: str = Field(default_factory=lambda: _gen_id("tbl"))
    workspace_id: str = "default"
    schema_name: str = "default"  # 'schema' is reserved by Pydantic v2 (model_dump warning)
    name: str
    format: str = "parquet"
    path: str  # relative to data_dir; e.g. "tables/default/default/customers"
    row_count: int = 0
    column_count: int = 0
    size_bytes: int = 0
    part_count: int = 0  # number of part-*.parquet files on disk
    backend: str = "local"
    created_from_object_id: str | None = None  # set when promoted from a file
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    # Z33 (2026-05-23) — Pipeline-Data-Prep provenance fields, populated
    # by LocalTableSinkNode when the table is produced by a Storage Z1
    # "Clean & Promote" pipeline (file → Wrangler → managed table).
    #
    #   prep_recipe           — the Wrangler step list (the recipe). A
    #                           list of {op, params} dicts; empty/None
    #                           when the table was written by a non-prep
    #                           pipeline.
    #   prep_source_object_id — the file row id the pipeline read from.
    #                           Lets the file detail surface "Prepared
    #                           as schema.name" without a workflow scan.
    #   prep_workflow_id      — the pipeline that produced this table.
    #                           The Storage page's "Edit recipe → re-run"
    #                           button opens this workflow in the Editor.
    #
    # All three default to None so tables produced outside the Z1 flow
    # (manual promotes, ad-hoc sink writes) keep the lighter shape.
    prep_recipe: list[dict[str, Any]] | None = None
    prep_source_object_id: str | None = None
    prep_workflow_id: str | None = None
    # N3 (2026-05-26) — Declarative tests on managed tables. Each entry:
    #   {test_type: 'not_null' | 'unique' | 'accepted_values' | 'relationships',
    #    columns: ['col_name', ...],
    #    accepted_values?: [val1, val2, ...],      # required for accepted_values
    #    to_table?: 'schema.name', to_column?: 'col'}  # required for relationships
    # The list is empty by default; legacy tables keep this shape silently.
    # Tests run via POST /api/storage/tables/{id}/tests/run, which compiles
    # each rule to a DuckDB SQL probe against the table's Parquet parts and
    # returns pass/fail with a row-count violation summary.
    tests: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StorageColumn(BaseModel):
    """Cached column metadata for a managed table or schema-inferred file.

    Either ``table_id`` OR ``object_id`` is set, never both. The DataStore
    enforces this on insert.
    """
    id: str = Field(default_factory=lambda: _gen_id("col"))
    workspace_id: str = "default"
    table_id: str | None = None
    object_id: str | None = None
    name: str
    type: str  # DuckDB-flavoured type label (e.g. VARCHAR, BIGINT, DOUBLE, TIMESTAMP)
    nullable: bool = True
    ordinal: int = 0
    sample: Any = None  # one sample value for the preview panel (may be None)


# ── Lightweight request/response models for the API ───────────────────────


class PromoteToTableRequest(BaseModel):
    """``POST /api/storage/promote-to-table`` body."""
    object_id: str
    schema_name: str = "default"
    table_name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    # When set, only these source columns are included — and renamed to
    # the value. ``{"Customer ID": "customer_id"}``. Empty dict = take
    # the source columns verbatim.
    column_map: dict[str, str] = Field(default_factory=dict)


class CleanupRequest(BaseModel):
    """``POST /api/storage/cleanup`` body."""
    # 'trash' = purge soft-deleted files older than `older_than_days`.
    # 'outputs' = purge pipeline outputs older than `older_than_days`.
    # 'all' = both (still respects the age cutoff).
    kind: Literal["trash", "outputs", "all"] = "trash"
    older_than_days: int = 30
    dry_run: bool = True


class StorageSummary(BaseModel):
    """``GET /api/storage/summary`` response.

    All counts are workspace-scoped — the caller's workspace is read
    from the request auth dep, not the URL.
    """
    workspace_id: str = "default"
    file_count: int = 0
    file_size_bytes: int = 0
    output_count: int = 0
    output_size_bytes: int = 0
    table_count: int = 0
    table_size_bytes: int = 0
    trash_count: int = 0
    trash_size_bytes: int = 0
    total_size_bytes: int = 0


__all__ = [
    "CleanupRequest",
    "OBJECT_KIND_FILE",
    "OBJECT_KIND_OUTPUT",
    "PromoteToTableRequest",
    "SINK_MODE_APPEND",
    "SINK_MODE_MERGE",
    "SINK_MODE_REPLACE",
    "StorageColumn",
    "StorageObject",
    "StorageSummary",
    "StorageTable",
]
