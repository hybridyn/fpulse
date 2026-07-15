"""Storage API — workspace-scoped file / table / output endpoints.

Twelve endpoints under ``/api/storage`` powering the Storage page and
the ``local_table_source`` / ``local_table_sink`` IR nodes:

    GET    /api/storage/summary
    GET    /api/storage/files
    POST   /api/storage/upload
    GET    /api/storage/preview/{object_id}
    GET    /api/storage/schema/{object_id}
    POST   /api/storage/folders          (logical tag, not a fs op)
    POST   /api/storage/move             (trash <-> uploads recovery)
    DELETE /api/storage/file/{object_id} (soft delete to trash/)
    POST   /api/storage/promote-to-table
    GET    /api/storage/tables
    GET    /api/storage/outputs
    POST   /api/storage/cleanup          (hard-delete from trash + old outputs)

Auth: every endpoint reads the caller's workspace via
``current_workspace_id`` and scopes every query/operation to that
workspace. Path traversal is centrally guarded by
``fpulse.datastore.paths.safe_join_under``.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from fpulse.auth.deps import current_workspace_id, require_auth
from fpulse.datastore.models import (
    CleanupRequest,
    OBJECT_KIND_FILE,
    OBJECT_KIND_OUTPUT,
    PromoteToTableRequest,
    StorageColumn,
    StorageObject,
    StorageTable,
)
from fpulse.datastore.paths import (
    format_from_filename,
    safe_filename,
    safe_join_under,
    safe_schema_or_table_name,
    workspace_paths,
)
from fpulse.datastore.reader import infer_schema, preview_file, _json_safe
from fpulse.datastore.store import get_store as get_datastore
from fpulse.datastore.usage import compute_workspace_usage_cached

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/storage", tags=["storage"])


# ── Shared helpers ────────────────────────────────────────────────────────


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception:
        return "default"


def _data_dir() -> str:
    from fpulse.main import app_state
    return app_state["data_dir"]


def _abs_path_for(obj: StorageObject) -> str:
    """Resolve a StorageObject.path to an absolute filesystem path.

    Always routes through safe_join_under so a corrupt index row that
    encodes a traversal can't trick us into opening /etc/passwd.
    """
    data_dir = _data_dir()
    return safe_join_under(data_dir, obj.path)


# ── Ad-hoc read-only SQL over managed tables (the Storage "query" surface) ──
#
# Lets a user run a SELECT across their workspace's managed Parquet tables —
# the "query my datasets like a database" capability. SAFETY (two layers):
#   1. Statement guard: a single statement, must start with SELECT/WITH, and a
#      whole-word blocklist rejects DDL/DML + every file-reading table function.
#   2. DuckDB sandbox (the REAL guarantee): every managed table is MATERIALISED
#      into an ephemeral in-memory DuckDB first, THEN `enable_external_access`
#      is turned OFF and the configuration is LOCKED before the user's query
#      runs — so even a guard bypass cannot read a file off disk, write
#      anything, ATTACH a DB, or INSTALL an extension. Only the workspace's own
#      tables are visible. Bounded by a result-row cap + a total-materialise cap.
import re as _re

_QUERY_MAX_ROWS = 5000
_QUERY_MAX_MATERIALIZE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB across all tables

_QUERY_FORBIDDEN = _re.compile(
    r"(?is)\b("
    r"insert|update|delete|drop|alter|create|attach|detach|copy|install|load|"
    r"pragma|export|import|truncate|vacuum|checkpoint|"
    r"read_csv|read_parquet|read_json|read_ndjson|read_text|read_blob|"
    r"parquet_scan|csv_scan|delta_scan|iceberg_scan|sniff_csv|glob"
    r")\b"
)


def _strip_sql_comments(sql: str) -> str:
    sql = _re.sub(r"/\*.*?\*/", " ", sql, flags=_re.S)
    sql = _re.sub(r"--[^\n]*", " ", sql)
    return sql.strip()


class TableQueryRequest(BaseModel):
    """``POST /api/storage/query`` body."""
    sql: str
    limit: int = 200


@router.post("/query")
def query_managed_tables(
    body: TableQueryRequest,
    _user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Run a read-only SELECT across this workspace's managed tables.

    Reference tables by name (e.g. ``SELECT region, SUM(revenue) FROM sales
    GROUP BY region``) — every workspace schema is on the search path so bare
    names resolve; qualify with QUOTES when disambiguating or when the schema
    is a reserved word (``"default"."sales"``). Read-only + sandboxed (see the
    module comment above). Returns ``{columns, rows, row_count, limit,
    truncated, tables_available}``.
    """
    import duckdb

    raw = (body.sql or "").strip()
    cleaned = _strip_sql_comments(raw).rstrip(";").strip()
    if not cleaned:
        raise HTTPException(400, "sql is required")
    if ";" in cleaned:
        raise HTTPException(400, "Only a single read-only statement is allowed (no ';').")
    if not _re.match(r"(?is)^\s*(select|with)\b", cleaned):
        raise HTTPException(400, "Only read-only SELECT / WITH queries are allowed.")
    if _QUERY_FORBIDDEN.search(cleaned):
        raise HTTPException(
            400,
            "Query rejected: it uses a disallowed keyword or file-reading function. "
            "Only SELECT over managed tables is permitted.",
        )

    limit = max(1, min(int(body.limit or 200), _QUERY_MAX_ROWS))
    store = get_datastore()
    data_dir = _data_dir()
    paths = workspace_paths(data_dir, workspace_id)
    tables = store.list_tables(workspace_id) or []

    conn = duckdb.connect()
    try:
        available: list[str] = []
        schemas_seen: set[str] = set()
        total_bytes = 0
        for t in tables:
            total_bytes += int(getattr(t, "size_bytes", 0) or 0)
            if total_bytes > _QUERY_MAX_MATERIALIZE_BYTES:
                raise HTTPException(
                    413,
                    "Workspace managed tables exceed the 2 GB ad-hoc-query cap. "
                    "Use a pipeline with a Managed Table Source for larger scans.",
                )
            schema = safe_schema_or_table_name(getattr(t, "schema_name", None) or "default")
            name = safe_schema_or_table_name(t.name)
            table_dir = paths.table_dir(schema, name)
            fq = f'"{schema}"."{name}"'
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            schemas_seen.add(schema)
            has_parts = os.path.isdir(table_dir) and any(
                f.startswith("part-") and f.endswith(".parquet")
                for f in os.listdir(table_dir)
            )
            if has_parts:
                glob = os.path.join(table_dir, "part-*.parquet")
                conn.execute(
                    f"CREATE TABLE {fq} AS SELECT * FROM read_parquet(?, union_by_name=true)",
                    [glob],
                )
            else:
                cols = store.list_columns(table_id=t.id)
                if cols:
                    coldefs = ", ".join(f'"{c.name}" {c.type}' for c in cols)
                    conn.execute(f"CREATE TABLE {fq} ({coldefs})")
                else:
                    conn.execute(f"CREATE TABLE {fq} (empty INTEGER)")
            available.append(f"{schema}.{name}")

        # Put every workspace schema on the search path so bare table names
        # resolve — done BEFORE the config lock below.
        if schemas_seen:
            conn.execute("SET search_path='" + ",".join(sorted(schemas_seen)) + "'")

        # ── Lock the sandbox BEFORE the user's query runs: no file I/O, no
        #    config changes, no extension loads from here on. This is the real
        #    guarantee that a read-only query cannot escape to the filesystem.
        conn.execute("SET enable_external_access=false")
        conn.execute("SET lock_configuration=true")

        rel = conn.sql(f"SELECT * FROM (\n{cleaned}\n) AS _fpulse_q LIMIT {limit}")
        columns = [{"name": c, "type": str(tp)} for c, tp in zip(rel.columns, rel.types)]
        rows = [
            {col: _json_safe(v) for col, v in zip(rel.columns, r)}
            for r in rel.fetchall()
        ]
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "limit": limit,
            "truncated": len(rows) >= limit,
            "tables_available": available,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Query failed: {exc}")
    finally:
        conn.close()


@router.post("/rescan")
def rescan_workspace_storage(
    _user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Force a filesystem→index rescan for the caller's workspace.

    The boot reconciler is sentinel-gated (it runs once per install), so files
    or pipeline outputs written directly to disk between boots don't appear on
    the Storage page until a rescan. This indexes any such files on demand.
    Add-only + idempotent — it never deletes an existing index row.
    """
    from fpulse.datastore.reconcile import reconcile_workspace

    store = get_datastore()
    data_dir = _data_dir()
    try:
        summary = reconcile_workspace(store, data_dir, workspace_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Rescan failed: {exc}")
    return {
        "workspace_id": workspace_id,
        "uploads_indexed": summary.get("uploads_indexed", 0),
        "outputs_indexed": summary.get("outputs_indexed", 0),
        "total_indexed": summary.get("uploads_indexed", 0) + summary.get("outputs_indexed", 0),
    }


_ALLOWED_UPLOAD_EXTS = {
    ".csv", ".tsv", ".txt",
    ".json", ".ndjson", ".jsonl",
    ".parquet", ".pq",
    ".xlsx", ".xls",
    ".xml",
}

_MAX_UPLOAD_MB = int(os.environ.get("FPULSE_UPLOAD_MAX_MB", "100"))
_MAX_UPLOAD_BYTES = _MAX_UPLOAD_MB * 1024 * 1024


# ── 0. Location + backend posture (Z4, 2026-05-23) ────────────────────────


@router.post("/_repair_schema")
def repair_storage_schema():
    """Idempotent schema repair for the storage_objects table.

    Some installs ended up with `_meta.schema_version` stamped past 26
    but the actual `folder_id` ALTER TABLE never ran (CREATE TABLE IF
    NOT EXISTS is a no-op on existing tables, so on dev DBs that
    predate v26 the column was never added). Every `save_object` then
    raised OperationalError silently. This endpoint adds any missing
    columns the current code expects.

    Returns the resulting column list so callers can confirm. Safe to
    call repeatedly — uses ADD COLUMN with an existence pre-check.
    """
    from fpulse.main import app_state
    db = app_state.get("db")
    if db is None:
        raise HTTPException(503, "database not initialized")
    conn = db.conn
    cols = conn.execute("PRAGMA table_info(storage_objects)").fetchall()
    existing = {c[1] for c in cols}
    added: list[str] = []
    expected = [
        ("folder_id", "TEXT DEFAULT ''"),
    ]
    for col, ddl in expected:
        if col not in existing:
            try:
                conn.execute(
                    f"ALTER TABLE storage_objects ADD COLUMN {col} {ddl}"
                )
                added.append(col)
            except Exception as exc:
                # Tolerate the duplicate-column race if two callers
                # run repair in parallel; surface anything else.
                if "duplicate column" not in str(exc).lower():
                    raise HTTPException(
                        500, f"failed adding {col}: {type(exc).__name__}: {exc}",
                    )
    conn.commit()
    cols = conn.execute("PRAGMA table_info(storage_objects)").fetchall()
    return {
        "columns": [c[1] for c in cols],
        "added": added,
    }


@router.get("/location")
def get_storage_location(workspace_id: str = Depends(_safe_workspace_id)):
    """Where storage lives + what backends are available on this install.

    Used by Settings → Operator Config → Storage location panel to show
    the user (a) the active data_dir, (b) free-disk + total-disk on that
    mount, (c) the canonical sub-tree layout, (d) which storage
    backends are wired in, and (e) any saved override that's pending
    a restart (Z27 — user-defined storage location).

    OSS reports `local` enabled and `s3` / `azure_blob` / `gcs` disabled
    with ``requires: 'plus'`` — the backend field is on every metadata
    row but only the local dispatch is implemented today (storage.md
    "Plus differences").
    """
    from fpulse.storage.storage_settings import load_override
    data_dir = _data_dir()
    abs_dir = os.path.abspath(data_dir)
    override = load_override()
    pending_dir = (override or {}).get("data_dir") if override else None
    pending_restart = bool(pending_dir and os.path.abspath(pending_dir) != abs_dir)
    info: dict[str, Any] = {
        "workspace_id": workspace_id,
        "active_backend": "local",
        "env_var": "FPULSE_DATA_DIR",
        "data_dir": abs_dir,
        # Z27: when the user saved a new path through the UI but hasn't
        # restarted yet, surface it as `pending_data_dir` so the UI
        # can render a "restart required" banner with the target path.
        "pending_data_dir": pending_dir if pending_restart else None,
        "pending_restart": pending_restart,
        "override_set_at": (override or {}).get("set_at") if override else None,
        "is_default": data_dir.rstrip("/\\").endswith("data"),
        "subtree": [
            {"name": "uploads/", "purpose": "raw uploaded files"},
            {"name": "outputs/", "purpose": "pipeline-generated artifacts"},
            {"name": "tables/", "purpose": "managed Parquet tables (schema.name)"},
            {"name": "trash/", "purpose": "soft-deleted files"},
            {"name": "checkpoints/", "purpose": "execution artifacts (system)"},
            {"name": "step_io/", "purpose": "execution artifacts (system)"},
        ],
        "backends": [
            {
                "id": "local",
                "label": "Local filesystem",
                "enabled": True,
                "requires": None,
                "description": "Files live on the same disk as the F-Pulse process. Set FPULSE_DATA_DIR to relocate.",
            },
            {
                "id": "s3",
                "label": "Amazon S3",
                "enabled": False,
                "requires": "plus",
                "description": "Stream uploads + Parquet parts to S3. Per-workspace bucket + prefix + region.",
            },
            {
                "id": "azure_blob",
                "label": "Azure Blob Storage",
                "enabled": False,
                "requires": "plus",
                "description": "Same as S3 against an Azure Storage account + container.",
            },
            {
                "id": "gcs",
                "label": "Google Cloud Storage",
                "enabled": False,
                "requires": "plus",
                "description": "GCS bucket-backed storage with the same workspace-scoped path scheme.",
            },
        ],
    }
    try:
        usage = shutil.disk_usage(abs_dir)
        info["disk"] = {
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "used_bytes": usage.used,
        }
    except OSError:
        info["disk"] = None
    return info


# Z27 (2026-05-23) — user-defined storage location.
#
# OSS users can now relocate the data directory from the Settings UI
# instead of editing FPULSE_DATA_DIR + restarting from a shell. The
# write path:
#
#   1. POST /location/test  validates a candidate path (existence,
#                           write probe, free-space report) without
#                           committing.
#   2. PUT  /location       persists the override to storage_settings.json
#                           (lives at <data_dir>/storage_settings.json
#                           so backup-and-restore keeps it).
#   3. (operator restart)   the boot path reads the override and uses
#                           it as the new data_dir.
#   4. DELETE /location     clears the override, reverting to the env
#                           var / default on next boot.
#
# Cloud backends (S3 / Azure Blob / GCS) are gated to Plus per the
# OSS/Plus split (feedback_oss_resilience_table_stakes.md): OSS owns
# the local file system; Plus owns the operational layer including
# remote storage backends.


class StorageLocationUpdate(BaseModel):
    """Payload for PUT /location."""
    data_dir: str
    active_backend: str = "local"


class StorageLocationProbe(BaseModel):
    """Payload for POST /location/test."""
    data_dir: str


@router.post("/location/test")
def test_storage_location(body: StorageLocationProbe, _ws: str = Depends(_safe_workspace_id)):
    """Validate a candidate data_dir without committing. UI calls this
    when the user clicks "Validate" to surface permission / disk-space
    issues before they Save."""
    from fpulse.storage.storage_settings import validate_path
    return validate_path(body.data_dir)


@router.put("/location")
def update_storage_location(body: StorageLocationUpdate, _ws: str = Depends(_safe_workspace_id)):
    """Persist a new data_dir override. The change takes effect on
    the next F-Pulse restart — we intentionally don't hot-swap the
    live data_dir while file handles + scheduled writes are open.

    Refuses to save a path that fails the same validation as
    /location/test (so the UI's "Validate" → "Save" flow can't be
    bypassed by clients posting an unvalidated path).
    """
    from fpulse.storage.storage_settings import save_override, validate_path
    probe = validate_path(body.data_dir)
    if not probe.get("ok"):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Path validation failed",
                "issues": probe.get("issues", []),
                "writable": probe.get("writable", False),
            },
        )
    backend = (body.active_backend or "local").strip().lower()
    if backend != "local":
        # Plus-only backends route through a different (Plus) endpoint.
        # Reject silently-passed non-local values so an OSS install
        # can't end up with a saved override no boot path can honor.
        raise HTTPException(
            status_code=400,
            detail=(
                f"Backend {backend!r} is not available in this edition. "
                f"OSS supports 'local' only — cloud backends (S3 / Azure Blob / GCS) ship with F-Pulse+."
            ),
        )
    record = save_override(probe["path"], active_backend=backend)
    return {
        "saved": True,
        "pending_data_dir": record["data_dir"],
        "active_backend": record["active_backend"],
        "pending_restart": True,
        "set_at": record["set_at"],
        "message": "Saved. Restart F-Pulse to apply the new location.",
    }


@router.delete("/location")
def clear_storage_location(_ws: str = Depends(_safe_workspace_id)):
    """Remove the persisted override; F-Pulse will use the env var /
    default on next boot. Useful when the operator wants to undo a
    saved location before restarting."""
    from fpulse.storage.storage_settings import clear_override
    clear_override()
    return {"cleared": True, "pending_restart": False}


# ── 1. Summary ────────────────────────────────────────────────────────────


@router.get("/summary")
def get_summary(workspace_id: str = Depends(_safe_workspace_id)):
    """Aggregate counts + bytes for the Storage page header.

    Read-only; counts come from the metadata index, not a fs walk.
    Anything on disk but not in storage_objects is invisible here
    (the reconciler should be back-filling it).
    """
    return get_datastore().workspace_summary(workspace_id)


# ── 1b. Usage (Y12 2026-05-23) ────────────────────────────────────────────


@router.get("/usage")
def get_usage(workspace_id: str = Depends(_safe_workspace_id)):
    """Bulk usage map for the Storage list views.

    Returns:
        ``{"files": {object_id: [{workflow_id, name}, ...]},
           "tables": {table_id: [{workflow_id, name}, ...]}}``

    Cached for 30 seconds so a page-load doesn't re-scan every workflow.
    The cache is invalidated on workflow save so freshness matters more
    than wall-clock TTL for the user's own edits.
    """
    return compute_workspace_usage_cached(workspace_id)


@router.get("/usage/file/{object_id}")
def get_usage_for_file(
    object_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Drill-down: pipelines that reference one file."""
    obj = get_datastore().get_object(object_id, workspace_id=workspace_id)
    if not obj:
        raise HTTPException(404, "object not found")
    full = compute_workspace_usage_cached(workspace_id)
    return {"object_id": object_id, "pipelines": full["files"].get(object_id, [])}


@router.get("/usage/table/{table_id}")
def get_usage_for_table(
    table_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Drill-down: pipelines that reference one managed table."""
    table = get_datastore().get_table(table_id, workspace_id=workspace_id)
    if not table:
        raise HTTPException(404, "table not found")
    full = compute_workspace_usage_cached(workspace_id)
    return {"table_id": table_id, "pipelines": full["tables"].get(table_id, [])}


@router.get("/tables/{table_id}/provenance")
def get_table_provenance(
    table_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """One-shot lineage answer for a managed table.

    2026-05-30 (P4): combines the four lineage questions the cert
    benchmark called out, so the UI doesn't need to chain four
    separate calls:

      * source_file        — the uploaded file this table was promoted
                              from, if any (created_from_object_id).
      * source_recipe      — the Wrangler recipe that produced it, if
                              the table came from a Storage Z1 "Clean &
                              Promote" pipeline.
      * source_workflow    — the pipeline that writes to this table.
      * last_run           — the most recent execution of that pipeline.
      * consumers          — pipelines that read FROM this table (the
                              "what breaks if I delete this?" answer).

    Every field is best-effort; missing data shows up as null/[]. The
    endpoint never 500s on a partially-populated table.
    """
    table = get_datastore().get_table(table_id, workspace_id=workspace_id)
    if not table:
        raise HTTPException(404, "table not found")

    # ── Source file (if promoted from an upload) ──────────────────────
    source_file = None
    src_obj_id = (
        getattr(table, "prep_source_object_id", None)
        or getattr(table, "created_from_object_id", None)
    )
    if src_obj_id:
        try:
            src = get_datastore().get_object(src_obj_id, workspace_id=workspace_id)
            if src:
                source_file = {
                    "id": src.id,
                    "name": src.name,
                    "kind": getattr(src, "kind", "file"),
                }
        except Exception:  # noqa: BLE001
            pass

    # ── Source workflow + last run ────────────────────────────────────
    source_workflow_id = getattr(table, "prep_workflow_id", None)
    source_workflow = None
    last_run = None
    if source_workflow_id:
        # Get the workflow's display name without failing on lookup error.
        try:
            from fpulse.main import app_state
            wf_store = app_state.get("store")
            if wf_store:
                wf_version = wf_store.get(source_workflow_id, workspace_id=workspace_id)
                if wf_version:
                    source_workflow = {
                        "id": source_workflow_id,
                        "name": getattr(wf_version.workflow, "name", source_workflow_id),
                    }
        except Exception:  # noqa: BLE001
            source_workflow = {"id": source_workflow_id, "name": source_workflow_id}

        # Latest execution of that workflow.
        try:
            from fpulse.main import app_state
            ex_store = app_state.get("execution_store")
            if ex_store:
                runs = ex_store.list_by_workflow(
                    source_workflow_id, limit=1, workspace_id=workspace_id,
                )
                if runs:
                    r = runs[0]
                    last_run = {
                        "id": r.get("id"),
                        "status": r.get("status"),
                        "started_at": r.get("started_at"),
                        "completed_at": r.get("completed_at"),
                        "rows_written": r.get("rows_written") or r.get("rows_out"),
                    }
        except Exception:  # noqa: BLE001
            pass

    # ── Consumer pipelines (the "what breaks if I delete this?" set) ──
    try:
        full = compute_workspace_usage_cached(workspace_id)
        consumers = full["tables"].get(table_id, [])
    except Exception:  # noqa: BLE001
        consumers = []

    # ── Recipe (preserved from Wrangler prep) ─────────────────────────
    source_recipe = None
    recipe = getattr(table, "prep_recipe", None)
    if isinstance(recipe, list) and recipe:
        source_recipe = recipe

    return {
        "table_id": table_id,
        "table_name": f"{table.schema_name}.{table.name}" if getattr(table, "schema_name", None) else table.name,
        "source_file": source_file,
        "source_workflow": source_workflow,
        "source_recipe": source_recipe,
        "last_run": last_run,
        "consumers": consumers,
        "consumer_count": len(consumers),
    }


# ── 2. List files ─────────────────────────────────────────────────────────


@router.get("/files")
def list_files(
    workspace_id: str = Depends(_safe_workspace_id),
    project_id: str | None = None,
    folder_id: str | None = None,
    include_deleted: bool = False,
    include_system_docs: bool = False,
):
    """List uploaded files (kind=file) for the workspace.

    Pass ``include_deleted=true`` to see soft-deleted rows; the
    "Show deleted" toggle on the Files tab uses this. ``folder_id``
    (Y15 2026-05-23) narrows to one folder under a project — pass an
    empty string to find files at the project root.

    Pass ``include_system_docs=true`` to additionally surface the
    bundled product documentation (the same set Help → Documentation
    serves) as read-only system rows in the same listing — Storage
    becomes the unified discovery surface; the Documentation viewer
    stays the canonical reader. System rows are virtual: there is no
    metadata table entry, and they are computed from the in-process
    `_DOC_CATALOG`. Frontend distinguishes them via the ``system`` tag.
    """
    objs = get_datastore().list_objects(
        workspace_id,
        kind=OBJECT_KIND_FILE,
        project_id=project_id,
        folder_id=folder_id,
        include_deleted=include_deleted,
    )
    rows = [o.model_dump(mode="json") for o in objs]

    if include_system_docs and project_id is None and not include_deleted:
        # Only inject system docs into the workspace-global, non-trash
        # view. They are bundled with the install, not project-scoped,
        # and they don't belong in trash rollups.
        try:
            from fpulse.api.reports import _DOC_CATALOG, _detect_tier
            is_plus = _detect_tier() == "plus"
            now = datetime.now(timezone.utc).isoformat()
            for d in _DOC_CATALOG:
                # OSS has no admin/non-admin distinction (single bootstrap
                # user), so admin_only flag is treated as visible. Plus
                # docs hidden on Free per the same rule the catalog uses.
                if d.get("plus_only") and not is_plus:
                    continue
                doc_path = d["path"]
                rows.append({
                    "id": f"sys_doc:{doc_path}",
                    "workspace_id": workspace_id,
                    "kind": OBJECT_KIND_FILE,
                    "name": d.get("title") or os.path.basename(doc_path),
                    "path": f"_bundled/docs/{doc_path}",
                    "backend": "system",
                    "format": "md",
                    "size_bytes": 0,
                    "row_count": None,
                    "column_count": None,
                    "sha256": None,
                    "project_id": None,
                    "folder_id": None,
                    "pipeline_id": None,
                    "run_id": None,
                    "tags": ["system", "doc"],
                    "description": d.get("summary", ""),
                    "created_at": now,
                    "updated_at": now,
                    "deleted_at": None,
                })
        except Exception:
            logger.exception("failed to inject system doc rows")

    return {"objects": rows, "count": len(rows)}


# ── 3. Upload ─────────────────────────────────────────────────────────────


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    workspace_id: str = Depends(_safe_workspace_id),
    project_id: str | None = None,
    folder_id: str | None = None,
    description: str = "",
):
    """Upload a file into the workspace's uploads/ root and index it.

    Body is multipart/form-data with a single ``file`` field. Query
    params:

      * ``project_id`` — scopes the upload to a project; omit for a
        workspace-global upload.
      * ``folder_id`` (Y15) — when a project is set, scopes the file
        to a folder under it. Ignored when project_id is omitted.
      * ``description`` — optional free-text note shown in the Files
        list.

    On success returns the full StorageObject row. The legacy
    /api/uploads/file endpoint is preserved for back-compat — both
    paths write to the same disk layout but only this one creates a
    metadata row.
    """
    original_name = safe_filename(file.filename or "upload.bin")
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in _ALLOWED_UPLOAD_EXTS:
        raise HTTPException(
            400,
            f"Unsupported extension {ext!r}. Allowed: {sorted(_ALLOWED_UPLOAD_EXTS)}",
        )

    paths = workspace_paths(_data_dir(), workspace_id).ensure()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stem = os.path.splitext(original_name)[0]
    stored = f"{stem}-{stamp}{ext}"
    abs_path = paths.upload_abs(stored)

    size = 0
    try:
        with open(abs_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    out.close()
                    try:
                        os.remove(abs_path)
                    except OSError:
                        pass
                    raise HTTPException(
                        413,
                        f"File exceeds {_MAX_UPLOAD_MB} MB limit. "
                        "Override with FPULSE_UPLOAD_MAX_MB.",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(500, f"upload write failed: {exc}")

    store = get_datastore()
    obj = StorageObject(
        workspace_id=workspace_id,
        kind=OBJECT_KIND_FILE,
        name=original_name,
        path=paths.relative_to_data_dir(abs_path),
        format=format_from_filename(original_name),
        size_bytes=size,
        project_id=project_id or None,
        # Y15: folder_id only meaningful when project_id is set; silently
        # ignored otherwise so a Global upload can't get a stray folder ref.
        folder_id=(folder_id or None) if project_id else None,
        description=description,
    )
    # 2026-05-28 — populate row_count + column_count on upload so the
    # Storage page's Files table shows real numbers immediately, instead
    # of dashes until the user clicks Preview for the first time.
    # Best-effort: on a parse failure the counts stay None and the upload
    # still succeeds (counts are nice-to-have, not load-bearing).
    try:
        from fpulse.datastore.reader import compute_file_stats
        rc, cc = compute_file_stats(abs_path, obj.format)
        obj.row_count = rc
        obj.column_count = cc
    except Exception as _exc:
        logger.debug("upload: file-stats best-effort skipped (%s)", _exc)
    store.save_object(obj)
    return obj.model_dump(mode="json")


# ── 3b. Replace bytes of an existing file (Y10 2026-05-23) ────────────────


@router.post("/file/{object_id}/replace")
async def replace_file(
    object_id: str,
    file: UploadFile = File(...),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """In-place replace the bytes of an existing storage_object.

    The object_id stays the same — so pipelines referencing it pick up
    the new bytes on next run. Old bytes are removed only AFTER the
    new upload writes successfully (no half-state if disk fills).

    Extension must match the original — swapping a .csv for a .parquet
    would break every downstream pipeline silently. Use a fresh upload
    instead for format changes.
    """
    store = get_datastore()
    obj = store.get_object(object_id, workspace_id=workspace_id)
    if not obj:
        raise HTTPException(404, "object not found")
    if obj.deleted_at:
        raise HTTPException(400, "cannot replace a soft-deleted file; restore it first")

    new_name = safe_filename(file.filename or obj.name)
    old_ext = os.path.splitext(obj.name)[1].lower()
    new_ext = os.path.splitext(new_name)[1].lower()
    if new_ext != old_ext:
        raise HTTPException(
            400,
            f"extension mismatch: original is {old_ext!r}, upload is {new_ext!r}. "
            f"Use a fresh upload for a different format.",
        )
    if new_ext not in _ALLOWED_UPLOAD_EXTS:
        raise HTTPException(400, f"unsupported extension {new_ext!r}")

    paths = workspace_paths(_data_dir(), workspace_id).ensure()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stem = os.path.splitext(new_name)[0]
    stored = f"{stem}-{stamp}{new_ext}"
    new_abs = paths.upload_abs(stored)

    size = 0
    try:
        with open(new_abs, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    out.close()
                    try:
                        os.remove(new_abs)
                    except OSError:
                        pass
                    raise HTTPException(
                        413,
                        f"File exceeds {_MAX_UPLOAD_MB} MB limit.",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(500, f"replace write failed: {exc}")

    # Delete OLD bytes only after the new file is fully on disk.
    old_abs = _abs_path_for(obj)
    if old_abs != new_abs and os.path.isfile(old_abs):
        try:
            os.remove(old_abs)
        except OSError as exc:
            logger.warning("replace: could not remove old bytes %s: %s", old_abs, exc)

    # Update metadata in place. Same object_id → downstream pipelines
    # keep pointing at the same row, but see the new bytes/size/timestamp.
    obj.name = new_name
    obj.path = paths.relative_to_data_dir(new_abs)
    obj.size_bytes = size
    obj.sha256 = None  # let next sha256 read recompute
    # 2026-05-28 — recompute counts inline so the Files table stays
    # accurate after an in-place replace. Same best-effort pattern as
    # the upload path: a parse failure clears the counts to None
    # rather than failing the replace.
    try:
        from fpulse.datastore.reader import compute_file_stats
        rc, cc = compute_file_stats(new_abs, obj.format)
        obj.row_count = rc
        obj.column_count = cc
    except Exception as _exc:
        logger.debug("replace: file-stats best-effort skipped (%s)", _exc)
        obj.row_count = None
        obj.column_count = None
    obj.updated_at = datetime.now(timezone.utc)
    store.save_object(obj)
    # Drop the cached schema-infer columns since the file changed.
    store._db.conn.execute(
        "DELETE FROM storage_columns WHERE object_id = ?", (obj.id,)
    )
    store._db.conn.commit()
    return obj.model_dump(mode="json")


# ── 4. Preview ────────────────────────────────────────────────────────────


@router.get("/preview/{object_id}")
def preview(
    object_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
    limit: int = 100,
    offset: int = 0,
):
    """Return a bounded sample of an uploaded file or pipeline output.

    Response is one of two shapes (``kind`` discriminator):

      * ``kind="rows"``     — tabular preview with columns + rows.
      * ``kind="document"`` — JSON tree fallback (Y8 2026-05-23) for
        non-records JSON. Includes ``is_pipeline_definition: true``
        when the shape matches an F-Pulse workflow export so the
        drawer can offer "Open in Editor".
    """
    obj = get_datastore().get_object(object_id, workspace_id=workspace_id)
    if not obj:
        raise HTTPException(404, "object not found")
    fmt = obj.format or format_from_filename(obj.name)
    if not fmt:
        raise HTTPException(400, f"preview: unknown format for {obj.name!r}")
    try:
        return preview_file(_abs_path_for(obj), fmt, limit=limit, offset=offset)
    except FileNotFoundError:
        raise HTTPException(404, "file not found on disk (was it deleted manually?)")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("preview failed for %s", object_id)
        raise HTTPException(500, f"preview failed: {exc}")


# ── 4b. Raw download ──────────────────────────────────────────────────────


@router.get("/raw/{object_id}")
def raw_download(
    object_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Stream the unmodified bytes of a stored file.

    Used by the "Open in Editor" affordance (Y8 2026-05-23) so the
    Toolbar import handler can load a workflow JSON that landed in
    Storage by mistake. Also handy for "download" / "share" UX —
    same handler, no extra surface needed.

    Workspace-scoped via the auth dep + the metadata row lookup, so
    a caller can't fish for an arbitrary path on disk.
    """
    obj = get_datastore().get_object(object_id, workspace_id=workspace_id)
    if not obj:
        raise HTTPException(404, "object not found")
    abs_path = _abs_path_for(obj)
    if not os.path.isfile(abs_path):
        raise HTTPException(404, "file not found on disk")
    return FileResponse(
        abs_path,
        media_type="application/octet-stream",
        filename=obj.name,
    )


# ── 5. Schema ─────────────────────────────────────────────────────────────


@router.get("/schema/{object_id}")
def get_schema(
    object_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Infer + cache the schema for an object.

    Cache is invalidated when the file's mtime moves past the cached
    row's updated_at. Cheap enough to recompute on miss; the cache
    is mostly to avoid hitting DuckDB on every render of the schema
    panel.
    """
    store = get_datastore()
    obj = store.get_object(object_id, workspace_id=workspace_id)
    if not obj:
        raise HTTPException(404, "object not found")

    abs_path = _abs_path_for(obj)
    if not os.path.isfile(abs_path):
        raise HTTPException(404, "file not found on disk")

    # Check cache freshness.
    file_mtime = datetime.fromtimestamp(os.path.getmtime(abs_path), tz=timezone.utc)
    cached = store.list_columns(object_id=object_id)
    if cached and obj.updated_at >= file_mtime:
        return {"columns": [c.model_dump(mode="json") for c in cached]}

    fmt = obj.format or format_from_filename(obj.name)
    if not fmt:
        raise HTTPException(400, f"schema: unknown format for {obj.name!r}")
    try:
        rows = infer_schema(abs_path, fmt)
    except Exception as exc:  # noqa: BLE001
        logger.exception("schema infer failed for %s", object_id)
        raise HTTPException(500, f"schema infer failed: {exc}")

    columns = [
        StorageColumn(
            workspace_id=workspace_id,
            object_id=object_id,
            name=r["name"],
            type=r["type"],
            nullable=r["nullable"],
            ordinal=r["ordinal"],
            sample=r.get("sample"),
        )
        for r in rows
    ]
    store.save_columns(columns, object_id=object_id)
    # Bump updated_at so subsequent hits use the cache until the file
    # is overwritten.
    obj.column_count = len(columns)
    obj.updated_at = datetime.now(timezone.utc)
    store.save_object(obj)
    return {"columns": [c.model_dump(mode="json") for c in columns]}


# ── 6. Logical folder (tag) ───────────────────────────────────────────────


@router.post("/folders")
def create_folder(
    body: dict[str, Any],
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create a logical folder by tagging existing objects.

    Storage in OSS v1.0 doesn't model folders as filesystem dirs —
    that would add a layer of nesting users have to navigate without
    really matching how they think about uploads. Instead a "folder"
    is a tag we apply to a set of object IDs. The Files tab can
    filter by tag to give the user the folder-like grouping UX.

    Body: ``{"name": "Q1 invoices", "object_ids": ["obj_..."]}``
    """
    name = (body.get("name") or "").strip()
    object_ids = body.get("object_ids") or []
    if not name:
        raise HTTPException(400, "name is required")
    if not isinstance(object_ids, list):
        raise HTTPException(400, "object_ids must be an array")

    store = get_datastore()
    tag = f"folder:{name.lower().replace(' ', '_')}"
    updated = 0
    for oid in object_ids:
        obj = store.get_object(oid, workspace_id=workspace_id)
        if not obj:
            continue
        if tag not in obj.tags:
            obj.tags.append(tag)
            store.save_object(obj)
            updated += 1
    return {"folder_tag": tag, "name": name, "updated": updated}


# ── 7. Move (trash <-> uploads recovery) ──────────────────────────────────


@router.post("/move")
def move_object(
    body: dict[str, Any],
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Move an object between trash and live state.

    Body: ``{"object_id": "...", "to": "trash" | "uploads"}``.
    "trash" soft-deletes (moves bytes into trash/{ws}/ + sets deleted_at);
    "uploads" restores (moves bytes back into uploads/{ws}/ + clears
    deleted_at). The path on the object row is updated to match.
    """
    object_id = body.get("object_id")
    target = body.get("to")
    if not object_id or target not in ("trash", "uploads"):
        raise HTTPException(400, "object_id and to ('trash' | 'uploads') required")

    store = get_datastore()
    obj = store.get_object(object_id, workspace_id=workspace_id)
    if not obj:
        raise HTTPException(404, "object not found")

    paths = workspace_paths(_data_dir(), workspace_id).ensure()
    src_abs = _abs_path_for(obj)
    if not os.path.isfile(src_abs):
        # File is gone — still allow the metadata flip so the UI can
        # finish the requested state change.
        logger.warning("move: source missing on disk: %s", src_abs)

    base = os.path.basename(src_abs) if src_abs else safe_filename(obj.name)
    if target == "trash":
        dest_abs = paths.trash_abs(base)
        if os.path.isfile(src_abs):
            shutil.move(src_abs, dest_abs)
        obj.path = paths.relative_to_data_dir(dest_abs)
        obj.deleted_at = datetime.now(timezone.utc)
    else:
        dest_abs = paths.upload_abs(base)
        if os.path.isfile(src_abs):
            shutil.move(src_abs, dest_abs)
        obj.path = paths.relative_to_data_dir(dest_abs)
        obj.deleted_at = None
    store.save_object(obj)
    return obj.model_dump(mode="json")


# ── 8. Soft delete ────────────────────────────────────────────────────────


@router.delete("/file/{object_id}")
def soft_delete(
    object_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Soft-delete a file — move bytes to trash/, set deleted_at.

    Hard delete happens via the cleanup endpoint or after the
    operator-configurable retention window passes.
    """
    store = get_datastore()
    obj = store.get_object(object_id, workspace_id=workspace_id)
    if not obj:
        raise HTTPException(404, "object not found")
    if obj.deleted_at:
        return obj.model_dump(mode="json")  # idempotent

    paths = workspace_paths(_data_dir(), workspace_id).ensure()
    src_abs = _abs_path_for(obj)
    base = safe_filename(os.path.basename(src_abs) or obj.name)
    dest_abs = paths.trash_abs(base)
    if os.path.isfile(src_abs):
        try:
            shutil.move(src_abs, dest_abs)
            obj.path = paths.relative_to_data_dir(dest_abs)
        except OSError as exc:
            raise HTTPException(500, f"trash move failed: {exc}")
    obj.deleted_at = datetime.now(timezone.utc)
    store.save_object(obj)

    # P0 Day 5 (2026-05-23) — audit destructive action.
    from fpulse.audit import audit_action
    audit_action(
        action="storage.file.delete",
        resource_type="file",
        resource_id=obj.id,
        workspace_id=workspace_id,
        details={"name": obj.name, "path": obj.path, "size_bytes": obj.size_bytes},
    )

    return obj.model_dump(mode="json")


# ── 9. Promote-to-table ───────────────────────────────────────────────────


@router.post("/promote-to-table")
def promote_to_table(
    body: PromoteToTableRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Promote a file to a managed Parquet table under tables/{ws}/.

    Idempotent on (workspace_id, schema_name, name): re-running with
    the same target replaces the existing part-000 file. Use sink
    nodes for append/merge semantics.
    """
    import duckdb

    store = get_datastore()
    obj = store.get_object(body.object_id, workspace_id=workspace_id)
    if not obj:
        raise HTTPException(404, "source object not found")
    if obj.deleted_at:
        raise HTTPException(400, "cannot promote a soft-deleted file")

    schema_name = safe_schema_or_table_name(body.schema_name or "default")
    table_name = safe_schema_or_table_name(body.table_name)
    if not table_name:
        raise HTTPException(400, "table_name is required")

    paths = workspace_paths(_data_dir(), workspace_id).ensure()
    table_dir = paths.table_dir(schema_name, table_name)
    os.makedirs(table_dir, exist_ok=True)
    parquet_path = os.path.join(table_dir, "part-000.parquet")
    src_abs = _abs_path_for(obj)
    fmt = obj.format or format_from_filename(obj.name)
    if not fmt:
        raise HTTPException(400, f"unknown format for source object {obj.name!r}")

    conn = duckdb.connect()
    try:
        from fpulse.datastore.reader import _open_relation  # type: ignore[attr-defined]
        rel = _open_relation(conn, src_abs, fmt)
        # Apply column rename if provided.
        if body.column_map:
            select_clauses = []
            for src_col, dest_col in body.column_map.items():
                if not src_col or not dest_col:
                    continue
                select_clauses.append(f'"{src_col}" AS "{dest_col}"')
            if select_clauses:
                rel = rel.project(", ".join(select_clauses))
        # Materialise to Parquet.
        rel.write_parquet(parquet_path)
        # Column metadata comes from the (projected) source relation; row count
        # from the written file. Use conn.execute (NOT conn.sql) with positional
        # params — DuckDB 1.5+ rejects a positional params list on .sql().
        columns = list(zip(rel.columns, rel.types))
        row_count = conn.execute(
            "SELECT COUNT(*) FROM read_parquet(?)", [parquet_path]
        ).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        logger.exception("promote-to-table failed for %s", body.object_id)
        raise HTTPException(500, f"promote failed: {exc}")
    finally:
        conn.close()

    size_bytes = os.path.getsize(parquet_path)
    rel_dir = paths.relative_to_data_dir(table_dir)

    existing = store.find_table_by_name(workspace_id, schema_name, table_name)
    if existing:
        existing.row_count = int(row_count)
        existing.column_count = len(columns)
        existing.size_bytes = size_bytes
        existing.part_count = 1  # promote overwrites part-000
        existing.description = body.description or existing.description
        existing.tags = body.tags or existing.tags
        existing.created_from_object_id = body.object_id
        existing.path = rel_dir
        store.save_table(existing)
        table = existing
    else:
        table = StorageTable(
            workspace_id=workspace_id,
            schema_name=schema_name,
            name=table_name,
            path=rel_dir,
            row_count=int(row_count),
            column_count=len(columns),
            size_bytes=size_bytes,
            part_count=1,
            created_from_object_id=body.object_id,
            description=body.description,
            tags=body.tags,
        )
        store.save_table(table)

    # Refresh cached column metadata for the new table.
    cols = [
        StorageColumn(
            workspace_id=workspace_id,
            table_id=table.id,
            name=name,
            type=str(typ),
            nullable=True,
            ordinal=idx,
        )
        for idx, (name, typ) in enumerate(columns)
    ]
    store.save_columns(cols, table_id=table.id)
    return table.model_dump(mode="json")


# ── 9b. Scaffold a cleanup pipeline (Z1, 2026-05-23) ──────────────────────


class ScaffoldCleanupRequest(BaseModel):
    """``POST /api/storage/scaffold-cleanup`` body.

    Returns a workflow JSON (NOT persisted) ready for the Editor's
    pending-import handoff. The user fills in the Data Wrangler steps,
    saves from the Editor — that's the persistence point.
    """
    object_id: str
    target_schema: str = "default"
    target_table: str | None = None


@router.post("/scaffold-cleanup")
def scaffold_cleanup_for_file(
    body: ScaffoldCleanupRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Emit a 3-node cleanup pipeline JSON for a file in Storage.

    Shape: ``source (csv/json) → data_wrangler (empty) → local_table_sink``.

    The response is NOT a persisted workflow — it's a pipeline blob the
    frontend stashes in ``sessionStorage['fpulse_pending_import']`` then
    navigates to ``#editor``, where the existing import handler picks
    it up and renders it on the canvas (same handoff Y8 uses).
    """
    from fpulse.datastore.scaffold import build_file_cleanup_workflow

    store = get_datastore()
    obj = store.get_object(body.object_id, workspace_id=workspace_id)
    if not obj:
        raise HTTPException(404, "source object not found")
    if obj.deleted_at:
        raise HTTPException(400, "cannot scaffold from a soft-deleted file")

    target_schema = safe_schema_or_table_name(body.target_schema or "default") or "default"
    target_table = (
        safe_schema_or_table_name(body.target_table) if body.target_table else None
    )

    workflow = build_file_cleanup_workflow(
        workspace_id=workspace_id,
        file_name=obj.name,
        file_path=obj.path,
        file_format=obj.format or format_from_filename(obj.name),
        target_schema=target_schema,
        target_table=target_table,
        project_id=obj.project_id,
        # Z33 (2026-05-23) — carry the source file's id through the
        # workflow metadata so the resulting local_table_sink can stamp
        # `prep_source_object_id` on its StorageTable. The Files tab
        # then back-links "Prepared as schema.name" without a workflow
        # scan.
        source_object_id=obj.id,
    )
    return {"workflow": workflow}


# ── 10. List tables ───────────────────────────────────────────────────────


@router.get("/tables")
def list_tables(workspace_id: str = Depends(_safe_workspace_id)):
    tables = get_datastore().list_tables(workspace_id)
    return {
        "tables": [t.model_dump(mode="json") for t in tables],
        "count": len(tables),
    }


@router.get("/tables/{table_id}/columns")
def list_table_columns(
    table_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    store = get_datastore()
    table = store.get_table(table_id, workspace_id=workspace_id)
    if not table:
        raise HTTPException(404, "table not found")
    cols = store.list_columns(table_id=table_id)
    return {"columns": [c.model_dump(mode="json") for c in cols]}


@router.get("/tables/{table_id}/preview")
def preview_table(
    table_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
    limit: int = 100,
    offset: int = 0,
):
    """Return a bounded row preview for a managed Parquet table."""
    import duckdb
    import json

    store = get_datastore()
    table = store.get_table(table_id, workspace_id=workspace_id)
    if not table:
        raise HTTPException(404, "table not found")
    abs_dir = safe_join_under(_data_dir(), table.path)
    if not os.path.isdir(abs_dir):
        raise HTTPException(404, "table directory not found on disk")
    parquet_glob = os.path.join(abs_dir, "*.parquet")
    limit = max(1, min(int(limit or 100), 1000))
    offset = max(0, int(offset or 0))

    conn = duckdb.connect()
    try:
        rel = conn.read_parquet(parquet_glob)
        sample = rel.limit(limit, offset)
        columns = [
            {"name": c, "type": str(t)}
            for c, t in zip(sample.columns, sample.types)
        ]
        rows = [
            json.loads(json.dumps(dict(zip(sample.columns, row)), default=str))
            for row in sample.fetchall()
        ]
        return {
            "kind": "rows",
            "columns": columns,
            "rows": rows,
            "row_count": table.row_count,
            "limit": limit,
            "offset": offset,
            "format": "parquet",
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("table preview failed for %s", table_id)
        raise HTTPException(500, f"table preview failed: {exc}")
    finally:
        conn.close()


class UpdateTableMetadataRequest(BaseModel):
    """``PATCH /api/storage/tables/{id}`` body — Z22 + Z23, 2026-05-23.

    Editable fields:
      - description / tags          — pure metadata
      - schema_name / table_name    — rename (Z23). Moves the directory
        on disk and updates the index row. Does NOT auto-rewrite
        consumer pipelines — the response includes ``stale_consumers``
        so the frontend can warn the user; they update affected
        pipelines themselves via the usage popover.
    """
    description: str | None = None
    tags: list[str] | None = None
    schema_name: str | None = None
    table_name: str | None = None


@router.patch("/tables/{table_id}")
def update_table_metadata(
    table_id: str,
    body: UpdateTableMetadataRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Edit metadata and/or rename a managed table.

    Idempotent. Returns the full updated row. When schema/name change:
      - validates the new identifiers (lowercase + alnum/underscore)
      - rejects 409 if (workspace, new_schema, new_name) already exists
      - moves bytes on disk (tables/{ws}/{old}/{old} → tables/{ws}/{new}/{new})
      - updates the index row's schema_name, name, path

    The response carries ``stale_consumers``: a list of pipelines that
    still reference the OLD name. Empty when nothing references the
    table OR the rename didn't fire.
    """
    store = get_datastore()
    table = store.get_table(table_id, workspace_id=workspace_id)
    if not table:
        raise HTTPException(404, "table not found")

    # ── Z23: rename branch ──────────────────────────────────────────────
    stale_consumers: list[dict[str, Any]] = []
    rename_fired = False
    if body.schema_name is not None or body.table_name is not None:
        new_schema = safe_schema_or_table_name(
            body.schema_name if body.schema_name is not None else table.schema_name
        ) or "default"
        new_name = safe_schema_or_table_name(
            body.table_name if body.table_name is not None else table.name
        )
        if not new_name:
            raise HTTPException(400, "table_name cannot be empty after normalisation")
        old_schema, old_name = table.schema_name, table.name
        if (new_schema, new_name) != (old_schema, old_name):
            # Collision check — another row in the same workspace already
            # owns this (schema, name) tuple.
            existing = store.find_table_by_name(workspace_id, new_schema, new_name)
            if existing and existing.id != table.id:
                raise HTTPException(
                    409,
                    f"A managed table named {new_schema}.{new_name} already exists in this workspace.",
                )
            # Move bytes on disk. The directory may not exist if the
            # table was created but never written to — that's a no-op.
            paths = workspace_paths(_data_dir(), workspace_id).ensure()
            old_dir = safe_join_under(_data_dir(), table.path)
            new_dir = paths.table_dir(new_schema, new_name)
            if os.path.isdir(old_dir):
                # Ensure the destination's parent exists, but the
                # destination itself must NOT exist (collision check
                # above should have caught it).
                os.makedirs(os.path.dirname(new_dir), exist_ok=True)
                if os.path.isdir(new_dir):
                    # Race: someone created it between the check and now.
                    raise HTTPException(
                        409,
                        f"Destination path {new_dir!r} already exists on disk.",
                    )
                try:
                    shutil.move(old_dir, new_dir)
                except OSError as exc:
                    logger.exception("rename failed moving %s → %s", old_dir, new_dir)
                    raise HTTPException(500, f"rename failed: {exc}")
            # Update index row.
            table.schema_name = new_schema
            table.name = new_name
            table.path = paths.relative_to_data_dir(new_dir)
            rename_fired = True
            # Collect stale consumers — pipelines that reference the OLD
            # name and will fail on next run unless updated. Cheap query
            # against the existing usage scanner (cache invalidates
            # below so consumers see the fresh state).
            full = compute_workspace_usage_cached(workspace_id)
            stale_consumers = list(full.get("tables", {}).get(table.id, []))

    # ── Metadata branch (description + tags) ────────────────────────────
    changed = rename_fired
    if body.description is not None and body.description != table.description:
        table.description = body.description
        changed = True
    if body.tags is not None:
        # Normalise: strip whitespace, drop empties, dedupe (preserve order).
        seen: set[str] = set()
        normalised: list[str] = []
        for t in body.tags:
            if not isinstance(t, str):
                continue
            t2 = t.strip()
            if not t2 or t2 in seen:
                continue
            seen.add(t2)
            normalised.append(t2)
        if normalised != (table.tags or []):
            table.tags = normalised
            changed = True

    if changed:
        table.updated_at = datetime.now(timezone.utc)
        store.save_table(table)
        # Renames change the (schema, name) key the usage scanner indexes
        # by, so invalidate the cache. Without this, the popover would
        # keep showing pipelines indexed against the old name until the
        # 30s TTL expires.
        if rename_fired:
            try:
                from fpulse.datastore.usage import invalidate as _invalidate_usage
                _invalidate_usage(workspace_id)
            except Exception:
                pass
            # P0 Day 5 — audit rename. Description/tags edits aren't
            # destructive and don't warrant a row in the audit log;
            # rename moves bytes on disk + breaks downstream consumers.
            from fpulse.audit import audit_action
            audit_action(
                action="storage.table.rename",
                resource_type="table",
                resource_id=table.id,
                workspace_id=workspace_id,
                details={
                    "from": f"{old_schema}.{old_name}",
                    "to": f"{table.schema_name}.{table.name}",
                    "stale_consumer_count": len(stale_consumers),
                },
            )

    payload = table.model_dump(mode="json")
    payload["stale_consumers"] = stale_consumers
    return payload


# ── N3 (2026-05-26): Declarative tests on managed tables ────────────
#
# dbt-style assertions per column / per row. The model stores the rule
# list; this endpoint compiles each rule to a DuckDB probe at run time
# and returns pass/fail + violation count. Backend doesn't validate
# rules at write time beyond shape (test_type required, columns array
# non-empty); the run endpoint surfaces a friendly error per failing
# rule rather than refusing to evaluate.


class TableTest(BaseModel):
    """One declarative assertion on a managed table.

    Mirrors the shape stored on StorageTable.tests; this Pydantic class
    is only used for the PUT body validation so the API rejects
    obviously-malformed rules at the boundary.

    2026-05-30 (P6): added `freshness`, `row_count_anomaly` test types
    + `severity` so contracts can warn vs fail. Pre-existing tests get
    severity='fail' by default (matches the historic "test fails =
    pipeline run fails" behavior).
    """
    test_type: str  # "not_null"|"unique"|"accepted_values"|"relationships"|"freshness"|"row_count_anomaly"
    columns: list[str] = []  # not required for freshness / row_count_anomaly
    accepted_values: list[Any] | None = None
    to_table: str | None = None   # for relationships: "schema.name"
    to_column: str | None = None  # for relationships: target column
    # P6 — extended fields
    severity: str = "fail"        # "fail" (stop the pipeline) | "warn" (log only)
    max_age_minutes: int | None = None   # for freshness: alert if updated_at older than this
    expected_min_rows: int | None = None # for row_count_anomaly: min row count
    expected_max_rows: int | None = None # for row_count_anomaly: max row count
    drift_pct: float | None = None       # for row_count_anomaly: percent change vs last_known


class PutTableTestsRequest(BaseModel):
    tests: list[TableTest]


@router.get("/tables/{table_id}/tests")
def list_table_tests(
    table_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Return the declarative tests configured for a managed table."""
    store = get_datastore()
    table = store.get_table(table_id, workspace_id=workspace_id)
    if not table:
        raise HTTPException(404, "table not found")
    return {"tests": list(table.tests or [])}


@router.put("/tables/{table_id}/tests")
def put_table_tests(
    table_id: str,
    body: PutTableTestsRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Replace the test list for a managed table.

    Validates rule shape:
      - test_type ∈ {not_null, unique, accepted_values, relationships}
      - columns list non-empty
      - accepted_values required (and non-empty) for accepted_values
      - to_table + to_column required for relationships
    """
    valid_types = {
        "not_null", "unique", "accepted_values", "relationships",
        "freshness", "row_count_anomaly",
    }
    valid_severities = {"fail", "warn"}
    for i, t in enumerate(body.tests):
        if t.test_type not in valid_types:
            raise HTTPException(400, f"tests[{i}]: unknown test_type '{t.test_type}'")
        if t.severity not in valid_severities:
            raise HTTPException(
                400, f"tests[{i}]: severity must be 'fail' or 'warn'"
            )
        # Most tests need columns; freshness + row_count_anomaly are
        # whole-table assertions that don't.
        if t.test_type not in ("freshness", "row_count_anomaly") and not t.columns:
            raise HTTPException(400, f"tests[{i}]: columns must be non-empty")
        if t.test_type == "accepted_values" and not t.accepted_values:
            raise HTTPException(
                400, f"tests[{i}]: accepted_values must be non-empty for accepted_values tests"
            )
        if t.test_type == "relationships" and not (t.to_table and t.to_column):
            raise HTTPException(
                400,
                f"tests[{i}]: relationships requires to_table + to_column",
            )
        if t.test_type == "freshness" and not t.max_age_minutes:
            raise HTTPException(
                400, f"tests[{i}]: freshness requires max_age_minutes > 0"
            )
        if t.test_type == "row_count_anomaly" and not any([
            t.expected_min_rows, t.expected_max_rows, t.drift_pct
        ]):
            raise HTTPException(
                400, f"tests[{i}]: row_count_anomaly needs at least one of expected_min_rows / expected_max_rows / drift_pct"
            )

    store = get_datastore()
    table = store.get_table(table_id, workspace_id=workspace_id)
    if not table:
        raise HTTPException(404, "table not found")

    serialised = [t.model_dump(mode="json", exclude_none=True) for t in body.tests]
    store.update_table(
        table_id=table_id,
        workspace_id=workspace_id,
        updates={"tests": serialised, "updated_at": datetime.now(timezone.utc)},
    )
    return {"tests": serialised, "count": len(serialised)}


def _quote_lit(v: Any) -> str:
    """Render a Python value as a SQL literal for DuckDB. Conservative —
    refuses anything weird with a TypeError so the run handler can
    convert it to a 400 rather than a SQL injection."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    raise TypeError(f"unsupported accepted_values element type: {type(v).__name__}")


@router.post("/tables/{table_id}/tests/run")
def run_table_tests(
    table_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Execute the configured tests against the table's Parquet parts.

    Each rule becomes one DuckDB query; results aggregate to a
    per-rule pass/fail + violation count. Doesn't write anything; safe
    to call repeatedly.

    Response:
        {
          "ran_at": "<iso>",
          "table_id": "...",
          "passed": int,
          "failed": int,
          "results": [
            {test_type, columns, ok: bool, violations: int, message?: str},
            ...
          ]
        }
    """
    import duckdb

    store = get_datastore()
    table = store.get_table(table_id, workspace_id=workspace_id)
    if not table:
        raise HTTPException(404, "table not found")
    tests = list(table.tests or [])
    if not tests:
        return {
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "table_id": table_id,
            "passed": 0,
            "failed": 0,
            "results": [],
        }

    abs_dir = safe_join_under(_data_dir(), table.path)
    if not os.path.isdir(abs_dir):
        raise HTTPException(404, "table directory not found on disk")
    parquet_glob = os.path.join(abs_dir, "*.parquet").replace("'", "''")

    conn = duckdb.connect()
    results: list[dict[str, Any]] = []
    passed = 0
    failed = 0
    try:
        for t in tests:
            tt = (t.get("test_type") or "").strip()
            cols = t.get("columns") or []
            entry: dict[str, Any] = {"test_type": tt, "columns": list(cols)}
            try:
                if tt == "not_null":
                    if not cols:
                        raise ValueError("columns required")
                    where = " OR ".join(f'"{c}" IS NULL' for c in cols)
                    sql = f"SELECT COUNT(*) FROM read_parquet('{parquet_glob}') WHERE {where}"
                elif tt == "unique":
                    if not cols:
                        raise ValueError("columns required")
                    select_cols = ", ".join(f'"{c}"' for c in cols)
                    sql = (
                        f"SELECT COALESCE(SUM(cnt - 1), 0) FROM ("
                        f"SELECT COUNT(*) AS cnt FROM read_parquet('{parquet_glob}') "
                        f"GROUP BY {select_cols} HAVING COUNT(*) > 1)"
                    )
                elif tt == "accepted_values":
                    if not cols or len(cols) != 1:
                        raise ValueError("accepted_values expects exactly one column")
                    vals = t.get("accepted_values") or []
                    if not vals:
                        raise ValueError("accepted_values list cannot be empty")
                    rendered = ", ".join(_quote_lit(v) for v in vals)
                    sql = (
                        f"SELECT COUNT(*) FROM read_parquet('{parquet_glob}') "
                        f'WHERE "{cols[0]}" NOT IN ({rendered}) '
                        f'AND "{cols[0]}" IS NOT NULL'
                    )
                elif tt == "relationships":
                    if not cols or len(cols) != 1:
                        raise ValueError("relationships expects exactly one column")
                    to_tbl = t.get("to_table")
                    to_col = t.get("to_column")
                    if not (to_tbl and to_col):
                        raise ValueError("relationships requires to_table + to_column")
                    schema_target, _, name_target = (to_tbl or "").partition(".")
                    if not schema_target or not name_target:
                        raise ValueError("to_table must be 'schema.name'")
                    target = store.find_table_by_name(
                        workspace_id, schema_target, name_target
                    )
                    if not target:
                        raise ValueError(f"target table {to_tbl} not found")
                    target_glob = os.path.join(
                        safe_join_under(_data_dir(), target.path), "*.parquet"
                    ).replace("'", "''")
                    sql = (
                        f'SELECT COUNT(*) FROM read_parquet(\'{parquet_glob}\') a '
                        f'WHERE a."{cols[0]}" IS NOT NULL '
                        f'AND a."{cols[0]}" NOT IN ('
                        f'SELECT "{to_col}" FROM read_parquet(\'{target_glob}\'))'
                    )
                elif tt == "freshness":
                    # 2026-05-30 (P6): table's updated_at vs now. No SQL
                    # probe needed — we read the field off the StorageTable
                    # row directly. Violations = 1 when stale, 0 otherwise.
                    max_age = int(t.get("max_age_minutes") or 0)
                    if max_age <= 0:
                        raise ValueError("freshness requires max_age_minutes > 0")
                    upd = getattr(table, "updated_at", None)
                    age_minutes = -1
                    if upd is not None:
                        try:
                            if isinstance(upd, str):
                                upd_dt = datetime.fromisoformat(upd.replace("Z", "+00:00"))
                            else:
                                upd_dt = upd
                            if upd_dt.tzinfo is None:
                                upd_dt = upd_dt.replace(tzinfo=timezone.utc)
                            age_minutes = int(
                                (datetime.now(timezone.utc) - upd_dt).total_seconds() / 60
                            )
                        except Exception:  # noqa: BLE001
                            age_minutes = -1
                    if age_minutes < 0:
                        # Unknown freshness — treat as a fail with explanation.
                        entry["ok"] = False
                        entry["violations"] = 1
                        entry["message"] = "could not parse table.updated_at"
                    else:
                        is_stale = age_minutes > max_age
                        entry["ok"] = not is_stale
                        entry["violations"] = 1 if is_stale else 0
                        entry["age_minutes"] = age_minutes
                        entry["threshold_minutes"] = max_age
                    # Skip the generic violation count post-processing.
                    entry["severity"] = t.get("severity", "fail")
                    if entry["ok"]:
                        passed += 1
                    else:
                        failed += 1
                    results.append(entry)
                    continue
                elif tt == "row_count_anomaly":
                    # 2026-05-30 (P6): hard min/max bounds + optional
                    # drift_pct vs the prior known row_count (stored on
                    # the StorageTable row).
                    min_r = t.get("expected_min_rows")
                    max_r = t.get("expected_max_rows")
                    drift = t.get("drift_pct")
                    prior = int(getattr(table, "row_count", 0) or 0)
                    actual_row = conn.execute(
                        f"SELECT COUNT(*) FROM read_parquet('{parquet_glob}')"
                    ).fetchone()
                    actual = int(actual_row[0] if actual_row else 0)
                    problems: list[str] = []
                    if min_r is not None and actual < int(min_r):
                        problems.append(f"{actual} rows < expected_min_rows={min_r}")
                    if max_r is not None and actual > int(max_r):
                        problems.append(f"{actual} rows > expected_max_rows={max_r}")
                    if drift is not None and prior > 0:
                        pct = abs(actual - prior) / prior * 100.0
                        if pct > float(drift):
                            problems.append(
                                f"row count shifted {pct:.1f}% vs prior {prior} (threshold {drift}%)"
                            )
                    is_anomaly = bool(problems)
                    entry["ok"] = not is_anomaly
                    entry["violations"] = len(problems)
                    entry["actual_rows"] = actual
                    entry["prior_rows"] = prior
                    if problems:
                        entry["message"] = "; ".join(problems)
                    entry["severity"] = t.get("severity", "fail")
                    if entry["ok"]:
                        passed += 1
                    else:
                        failed += 1
                    results.append(entry)
                    continue
                else:
                    raise ValueError(f"unknown test_type '{tt}'")

                row = conn.execute(sql).fetchone()
                violations = int(row[0] if row else 0)
                ok = violations == 0
                entry["ok"] = ok
                entry["violations"] = violations
                # 2026-05-30 (P6): severity surfaces fail vs warn so the
                # caller can choose to block the pipeline or just log.
                entry["severity"] = t.get("severity", "fail")
                if ok:
                    passed += 1
                else:
                    failed += 1
            except Exception as exc:  # noqa: BLE001
                entry["ok"] = False
                entry["violations"] = -1
                entry["message"] = f"{type(exc).__name__}: {exc}"[:200]
                failed += 1
            results.append(entry)
    finally:
        conn.close()

    return {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "table_id": table_id,
        "passed": passed,
        "failed": failed,
        "results": results,
    }


@router.delete("/tables/{table_id}")
def drop_table(
    table_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Drop a managed table — RECOVERABLE.

    2026-07: instead of destroying the Parquet bytes, the table's part-*
    directory is MOVED to ``trash/{ws}/dropped-tables/`` so an accidental
    drop can be recovered (the cleanup endpoint purges trashed tables older
    than the retention window). The metadata row is still removed, so the
    table name frees up immediately for re-creation. Previously this was an
    irreversible ``rmtree`` — no undo, no parity with the per-file trash.
    """
    store = get_datastore()
    table = store.get_table(table_id, workspace_id=workspace_id)
    if not table:
        raise HTTPException(404, "table not found")
    abs_dir = safe_join_under(_data_dir(), table.path)

    recoverable_at: str | None = None
    if os.path.isdir(abs_dir):
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            rel_dest = os.path.join(
                "trash", workspace_id, "dropped-tables",
                f"{table.schema_name}.{table.name}-{table_id}-{ts}",
            )
            dest = safe_join_under(_data_dir(), rel_dest)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(abs_dir, dest)
            recoverable_at = rel_dest
        except OSError as exc:
            raise HTTPException(500, f"drop failed (filesystem): {exc}")
    store.hard_delete_table(table_id)

    # Audit. Table drop = high-impact, log everything (incl. where it went).
    from fpulse.audit import audit_action
    audit_action(
        action="storage.table.drop",
        resource_type="table",
        resource_id=table_id,
        workspace_id=workspace_id,
        details={
            "schema_name": table.schema_name,
            "name": table.name,
            "row_count": table.row_count,
            "size_bytes": table.size_bytes,
            "recoverable_at": recoverable_at,
        },
    )

    return {
        "status": "ok",
        "id": table_id,
        "recoverable": recoverable_at is not None,
        "recoverable_at": recoverable_at,
    }


# ── 11. List outputs (grouped) ────────────────────────────────────────────


@router.get("/outputs")
def list_outputs(workspace_id: str = Depends(_safe_workspace_id)):
    groups = get_datastore().list_outputs_grouped(workspace_id)
    # Enrich each run-group with the human pipeline NAME — the groups
    # carry only pipeline_id (a workflow id). Best-effort + cached: a
    # deleted or ad-hoc pipeline keeps a null name and the UI falls back
    # to "Ad-hoc run". Same resolver the table-provenance endpoint uses.
    try:
        from fpulse.main import app_state
        wf_store = app_state.get("store")
    except Exception:  # noqa: BLE001
        wf_store = None
    name_cache: dict[str, str] = {}

    def _pipeline_name(pid: str) -> str | None:
        if not pid:
            return None
        if pid in name_cache:
            return name_cache[pid] or None
        resolved = ""
        if wf_store is not None:
            try:
                version = wf_store.get(pid, workspace_id=workspace_id)
                if version is not None:
                    resolved = getattr(getattr(version, "workflow", None), "name", "") or ""
            except Exception:  # noqa: BLE001
                resolved = ""
        name_cache[pid] = resolved
        return resolved or None

    for g in groups:
        g["pipeline_name"] = _pipeline_name(g.get("pipeline_id") or "")
    return {"groups": groups, "count": len(groups)}


@router.delete("/outputs/{object_id}")
def delete_output(
    object_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Delete one pipeline output artifact permanently."""
    store = get_datastore()
    obj = store.get_object(object_id, workspace_id=workspace_id)
    if not obj:
        raise HTTPException(404, "output not found")
    if obj.kind != OBJECT_KIND_OUTPUT:
        raise HTTPException(400, "object is not a pipeline output")
    abs_path = _abs_path_for(obj)
    if os.path.isfile(abs_path):
        try:
            os.remove(abs_path)
        except OSError as exc:
            raise HTTPException(500, f"delete failed (filesystem): {exc}")
    store.hard_delete_object(object_id)
    return {"status": "ok", "id": object_id}


# ── 12. Cleanup ───────────────────────────────────────────────────────────


@router.post("/cleanup")
def cleanup(
    body: CleanupRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Purge old trash and/or outputs.

    Dry-run by default so the UI can show "X files (Y MB) will be
    deleted" before the operator confirms. The actual purge runs
    when ``dry_run=False`` is posted.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(body.older_than_days)))
    store = get_datastore()

    targets: list[StorageObject] = []
    all_objs = store.list_objects(workspace_id, include_deleted=True)

    for obj in all_objs:
        if body.kind in ("trash", "all") and obj.deleted_at and obj.deleted_at < cutoff:
            targets.append(obj)
            continue
        if body.kind in ("outputs", "all") and obj.kind == OBJECT_KIND_OUTPUT:
            if obj.created_at < cutoff and not obj.deleted_at:
                targets.append(obj)

    purged: list[dict[str, Any]] = []
    for obj in targets:
        purged.append({
            "id": obj.id,
            "name": obj.name,
            "size_bytes": obj.size_bytes,
            "kind": obj.kind,
        })
        if body.dry_run:
            continue
        abs_path = safe_join_under(_data_dir(), obj.path)
        try:
            if os.path.isfile(abs_path):
                os.remove(abs_path)
        except OSError as exc:
            logger.warning("cleanup: could not remove %s: %s", abs_path, exc)
        store.hard_delete_object(obj.id)

    # 2026-07: also purge dropped managed-table directories (moved to
    # trash/{ws}/dropped-tables/ by DELETE /tables/{id}). They have no
    # metadata row, so sweep them off disk by directory mtime.
    dropped_tables_purged: list[dict[str, Any]] = []
    if body.kind in ("trash", "all"):
        dropped_root = safe_join_under(
            _data_dir(), os.path.join("trash", workspace_id, "dropped-tables")
        )
        if os.path.isdir(dropped_root):
            for entry in os.scandir(dropped_root):
                if not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    continue
                if mtime >= cutoff:
                    continue
                size = 0
                for root, _dirs, files in os.walk(entry.path):
                    for fn in files:
                        try:
                            size += os.path.getsize(os.path.join(root, fn))
                        except OSError:
                            pass
                dropped_tables_purged.append({"name": entry.name, "size_bytes": size})
                if not body.dry_run:
                    try:
                        shutil.rmtree(entry.path)
                    except OSError as exc:
                        logger.warning(
                            "cleanup: could not remove dropped table %s: %s", entry.path, exc,
                        )

    # Audit. Skip on dry-run (no state changed). On a real purge, one row
    # summarising the operation + a list of victims.
    if not body.dry_run and (purged or dropped_tables_purged):
        from fpulse.audit import audit_action
        audit_action(
            action="storage.cleanup",
            resource_type="storage_objects",
            resource_id=f"cleanup-{body.kind}",
            workspace_id=workspace_id,
            details={
                "kind": body.kind,
                "older_than_days": body.older_than_days,
                "purge_count": len(purged),
                "purge_bytes": sum(p["size_bytes"] for p in purged),
                "purged_ids": [p["id"] for p in purged[:50]],
                "dropped_tables_purged": len(dropped_tables_purged),
            },
        )

    return {
        "dry_run": body.dry_run,
        "purge_count": len(purged),
        "purge_bytes": sum(p["size_bytes"] for p in purged),
        "purged": purged,
        "dropped_tables_purge_count": len(dropped_tables_purged),
        "dropped_tables_purge_bytes": sum(d["size_bytes"] for d in dropped_tables_purged),
    }


__all__ = ["router"]
