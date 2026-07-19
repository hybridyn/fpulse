"""Storage usage scanner — "who uses this file/table?" (Y12 2026-05-23).

A storage object (file) or managed table is only as honest as the
ability to answer "what would break if I drop this?". Without a usage
view, the Storage page is a graveyard — files go in, no one knows
what's still relevant, and deletions silently corrupt pipelines.

This module walks workflow_versions and builds two indexes for a
workspace:

  files_by_id[storage_object_id]  → list[{workflow_id, name}]
  tables_by_id[storage_table_id]  → list[{workflow_id, name}]

Reference shapes detected:

  1. local_table_source / local_table_sink — params.schema_name +
     params.table_name → table reference.
  2. Generic source / destination with connector_type='local_table' —
     same shape as #1 (these dispatch through SOURCE_MAP/DEST_MAP).
  3. File-based source/sink with params.file_path — match against
     storage_object.path (both use forward-slash relative paths).
  4. Promote-to-table provenance — storage_tables.created_from_object_id
     captures the file that seeded a managed table. Counted as a
     usage edge ("this file is the source of this table") so users
     don't delete an upload that's downstream of a live table.

Cost: O(workflows × steps). For OSS single-tenant installs this is
a millisecond; for thousands of workflows we cache the bulk result
for 30 seconds keyed on the workspace's max(updated_at).
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


# Step types that mean "reads from a managed table"
_TABLE_SOURCE_TYPES = {"local_table_source"}
_TABLE_SINK_TYPES = {"local_table_sink"}
# Step types that route to a managed table via connector_type
_GENERIC_DISPATCH_TYPES = {"source", "destination"}

# Step types whose params.file_path may resolve to a storage_object
# Note: no `parquet_sink` enum exists — parquet writes flow through
# local_table_sink, s3_sink, or generic `destination` with file_format=parquet.
_FILE_PATH_TYPES = {
    "csv_source", "json_source", "parquet_source", "excel_source",
    "xml_source", "file_source",
    "csv_sink", "json_sink", "excel_sink", "file_sink",
    "source", "destination",  # generic — params.file_path used too
}


def _scan_workflow_for_refs(
    workflow: dict[str, Any],
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Walk one workflow's steps. Returns ``(table_refs, file_paths)``.

    table_refs is a list of ``(schema_name, table_name, role)`` triples
    where ``role`` ∈ ``{'source', 'sink', 'generic'}``. Z22 added the role
    so the Managed Tables row can pick which workflow is the WRITER
    (the data-prep pipeline that fills the table) vs the consumer.

    file_paths is a list of ``params.file_path`` strings (relative paths).
    """
    table_refs: list[tuple[str, str, str]] = []
    file_paths: list[str] = []

    steps = workflow.get("steps") or []
    if not isinstance(steps, list):
        return table_refs, file_paths

    for step in steps:
        if not isinstance(step, dict):
            continue
        stype = step.get("type")
        params = step.get("params") or {}
        if not isinstance(params, dict):
            continue

        # Cases 1 + 2 — managed-table references with role detection.
        role: str | None = None
        if stype in _TABLE_SOURCE_TYPES:
            role = "source"
        elif stype in _TABLE_SINK_TYPES:
            role = "sink"
        elif stype in _GENERIC_DISPATCH_TYPES:
            connector_type = (params.get("connector_type") or "").lower()
            if connector_type == "local_table":
                # Generic dispatch — the step type tells us read vs write.
                # `source` step + connector_type=local_table = source.
                # `destination` step + connector_type=local_table = sink.
                role = "source" if stype == "source" else "sink" if stype == "destination" else "generic"
        if role:
            schema = (params.get("schema_name") or "default").lower()
            tname = (params.get("table_name") or "").lower()
            if tname:
                table_refs.append((schema, tname, role))

        # Case 3 — file-path references.
        if stype in _FILE_PATH_TYPES:
            fp = params.get("file_path") or params.get("path")
            if isinstance(fp, str) and fp:
                # Normalise to forward slashes — both wire format and
                # storage_object.path use them.
                file_paths.append(fp.replace("\\", "/"))

    return table_refs, file_paths


def compute_workspace_usage(workspace_id: str) -> dict[str, Any]:
    """Bulk usage map for the Storage page.

    Returns:
        {
          "files":  { object_id: [{workflow_id, name}], ... },
          "tables": { table_id:  [{workflow_id, name}], ... },
        }

    Files indexed by id; tables indexed by id. Empty dicts when there
    are no references — the frontend renders an em-dash in that case.
    """
    from fpulse.datastore.store import get_store as get_datastore
    from fpulse.main import app_state

    store = get_datastore()
    workflow_store = app_state.get("workflow_store")
    if workflow_store is None:
        logger.warning("usage: workflow_store missing from app_state")
        return {"files": {}, "tables": {}}

    # Index of disk-paths and schema.table → object/table ids, scoped to
    # this workspace, so the scan can map a workflow's file_path / table
    # reference back to a storage row in O(1).
    objects = store.list_objects(workspace_id, include_deleted=False)
    path_to_obj_id: dict[str, str] = {o.path: o.id for o in objects}

    tables = store.list_tables(workspace_id)
    table_to_id: dict[tuple[str, str], str] = {
        (t.schema_name.lower(), t.name.lower()): t.id for t in tables
    }

    files_usage: dict[str, list[dict[str, str]]] = {}
    tables_usage: dict[str, list[dict[str, str]]] = {}

    # Z43 (2026-05-23) — list_all() returns flattened summaries WITHOUT
    # `steps`, so the scanner here used to find no refs ever. Switched to
    # list_all_full which preserves the full workflow JSON. Falls back to
    # the old path if the store predates list_all_full (legacy / tests).
    try:
        if hasattr(workflow_store, "list_all_full"):
            workflows = workflow_store.list_all_full(workspace_id=workspace_id)
        else:
            workflows = workflow_store.list_all(workspace_id=workspace_id)
    except TypeError:
        # Older signature without workspace_id kwarg.
        workflows = (
            workflow_store.list_all_full()
            if hasattr(workflow_store, "list_all_full")
            else workflow_store.list_all()
        )

    for wf in workflows:
        # `wf` may be either the bare workflow dict OR a wrapper. Handle both.
        wf_data = wf.get("workflow") if isinstance(wf, dict) and "workflow" in wf else wf
        if not isinstance(wf_data, dict):
            continue
        wf_id = wf_data.get("id") or ""
        wf_name = wf_data.get("name") or "(unnamed pipeline)"

        table_refs, file_paths = _scan_workflow_for_refs(wf_data)

        # Collapse multiple refs to the same (workflow_id, table) into one
        # entry — but promote 'sink' if any ref in that workflow is a sink
        # (writer wins for "open data prep" purposes).
        per_table_role: dict[tuple[str, str], str] = {}
        for schema, tname, role in table_refs:
            key = (schema, tname)
            existing = per_table_role.get(key)
            if existing == "sink":
                continue  # sink role is sticky
            per_table_role[key] = role if existing is None or role == "sink" else existing

        for (schema, tname), role in per_table_role.items():
            tid = table_to_id.get((schema, tname))
            if not tid:
                continue
            tables_usage.setdefault(tid, []).append({
                "workflow_id": wf_id,
                "name": wf_name,
                "role": role,
            })

        for fp in file_paths:
            oid = path_to_obj_id.get(fp)
            if not oid:
                continue
            files_usage.setdefault(oid, []).append({
                "workflow_id": wf_id,
                "name": wf_name,
            })

    # Case 4 — promote-to-table provenance edge.
    # A file that seeded a managed table is "in use by" any pipeline
    # that reads that table. Surface the table's pipeline list under
    # the file's usage too so deleting the file warns about downstream
    # damage.
    for table in tables:
        if not table.created_from_object_id:
            continue
        downstream = tables_usage.get(table.id) or []
        if not downstream:
            continue
        bucket = files_usage.setdefault(table.created_from_object_id, [])
        for ref in downstream:
            # Avoid double-counting if the file is ALSO directly
            # referenced by the same pipeline.
            if not any(b.get("workflow_id") == ref["workflow_id"] for b in bucket):
                bucket.append({
                    **ref,
                    "via_table": f"{table.schema_name}.{table.name}",
                })

    return {"files": files_usage, "tables": tables_usage}


# ── Tiny TTL cache so the bulk endpoint doesn't re-scan on every render ──

_CACHE_TTL_SECONDS = 30
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def compute_workspace_usage_cached(workspace_id: str) -> dict[str, Any]:
    """Cached wrapper around compute_workspace_usage.

    Cache is invalidated on TTL or via ``invalidate(workspace_id)`` —
    the latter is called from the workflow save endpoint so a fresh
    save immediately reflects the new usage map.
    """
    now = time.time()
    hit = _cache.get(workspace_id)
    if hit and (now - hit[0]) < _CACHE_TTL_SECONDS:
        return hit[1]
    result = compute_workspace_usage(workspace_id)
    _cache[workspace_id] = (now, result)
    return result


def invalidate(workspace_id: str) -> None:
    _cache.pop(workspace_id, None)


def invalidate_all() -> None:
    _cache.clear()


# ─────────────────────────────────────────────────────────────────────────
# Z25 (2026-05-23) — Connection / Credential lineage
# ─────────────────────────────────────────────────────────────────────────
#
# Mirrors the storage usage scanner one level UP the dependency graph:
#
#   Credential ──used by──▶ Connection ──used by──▶ Pipeline
#
# Backed by two new endpoints — GET /api/connections/usage and
# GET /api/credentials/usage — which feed "Used by N pipelines" pills
# on the Connections page and "Used by N connections" pills on the
# Credentials page. Same UsageRef shape the Storage page uses so the
# frontend popover component is identical.


def _scan_workflow_for_connection_refs(workflow: dict[str, Any]) -> dict[str, str]:
    """Return ``{connection_id: role}`` for one workflow.

    Role is the step's category — 'source', 'destination', 'activity',
    or 'cloud-storage' — so the popover can show context (whether this
    pipeline READS from the connection or WRITES to it). When multiple
    steps in the same workflow reference the same connection, the role
    collapses: 'destination' wins over 'source' (writes are louder than
    reads when we ask "what would break if I delete this?").
    """
    out: dict[str, str] = {}
    steps = workflow.get("steps") or []
    if not isinstance(steps, list):
        return out

    for step in steps:
        if not isinstance(step, dict):
            continue
        params = step.get("params") or {}
        if not isinstance(params, dict):
            continue
        # Standard location.
        cid = params.get("connection_id")
        # Flow-control nodes use source_connection_id (per backend/fpulse/
        # nodes/flow_control.py:1251). Treat it the same.
        if not cid:
            cid = params.get("source_connection_id")
        if not isinstance(cid, str) or not cid:
            continue

        stype = (step.get("type") or "").lower()
        if stype == "destination" or stype.endswith("_sink"):
            role = "destination"
        elif stype == "source" or stype.endswith("_source"):
            role = "source"
        else:
            role = "activity"

        # Destination is sticky — once a workflow writes to the
        # connection, that's the role we surface.
        existing = out.get(cid)
        if existing == "destination":
            continue
        out[cid] = role if (existing is None or role == "destination") else existing
    return out


def compute_connection_usage(workspace_id: str) -> dict[str, list[dict[str, Any]]]:
    """{connection_id: [{workflow_id, name, role}, ...]} for the workspace.

    Role ∈ {source, destination, activity}. Empty bucket when no
    pipeline references the connection (frontend renders an em-dash).
    """
    from fpulse.main import app_state

    workflow_store = app_state.get("workflow_store")
    if workflow_store is None:
        logger.warning("connection usage: workflow_store missing from app_state")
        return {}

    # Z43 (2026-05-23) — list_all() drops the `steps` array which the
    # scanner below needs to find connection_id references. Use the
    # full variant; fall back to list_all() only for stores that haven't
    # implemented list_all_full yet.
    try:
        if hasattr(workflow_store, "list_all_full"):
            workflows = workflow_store.list_all_full(workspace_id=workspace_id)
        else:
            workflows = workflow_store.list_all(workspace_id=workspace_id)
    except TypeError:
        workflows = (
            workflow_store.list_all_full()
            if hasattr(workflow_store, "list_all_full")
            else workflow_store.list_all()
        )

    out: dict[str, list[dict[str, Any]]] = {}
    for wf in workflows:
        wf_data = wf.get("workflow") if isinstance(wf, dict) and "workflow" in wf else wf
        if not isinstance(wf_data, dict):
            continue
        wf_id = wf_data.get("id") or ""
        wf_name = wf_data.get("name") or "(unnamed pipeline)"

        for cid, role in _scan_workflow_for_connection_refs(wf_data).items():
            out.setdefault(cid, []).append({
                "workflow_id": wf_id,
                "name": wf_name,
                "role": role,
            })
    return out


def compute_credential_usage(workspace_id: str) -> dict[str, list[dict[str, Any]]]:
    """{credential_id: [{connection_id, name, type}, ...]} for the workspace.

    Walks the connection store (not the workflow store) — credential
    references live on the Connection row, not in pipeline IRs.
    """
    from fpulse.connections.store import get_store as _conn_store

    try:
        connections = _conn_store().list_all(workspace_id=workspace_id)
    except TypeError:
        connections = _conn_store().list_all()

    out: dict[str, list[dict[str, Any]]] = {}
    for c in connections:
        if not isinstance(c, dict):
            # Some store impls return objects, not dicts. Best-effort
            # attribute access; skip silently on shape mismatch.
            cid = getattr(c, "credential_id", None)
            conn_id = getattr(c, "id", None)
            name = getattr(c, "name", None)
            ctype = getattr(c, "type", None)
        else:
            cid = c.get("credential_id")
            conn_id = c.get("id")
            name = c.get("name")
            ctype = c.get("type")
        if not isinstance(cid, str) or not cid:
            continue
        if not conn_id:
            continue
        out.setdefault(cid, []).append({
            "connection_id": conn_id,
            "name": name or "(unnamed connection)",
            "type": ctype or "unknown",
        })
    return out


# Same TTL cache pattern as compute_workspace_usage_cached. Two separate
# caches keyed by (kind, workspace_id) so a connection update doesn't
# invalidate the credential map.
_conn_usage_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cred_usage_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def compute_connection_usage_cached(workspace_id: str) -> dict[str, Any]:
    now = time.time()
    hit = _conn_usage_cache.get(workspace_id)
    if hit and (now - hit[0]) < _CACHE_TTL_SECONDS:
        return hit[1]
    result = compute_connection_usage(workspace_id)
    _conn_usage_cache[workspace_id] = (now, result)
    return result


def compute_credential_usage_cached(workspace_id: str) -> dict[str, Any]:
    now = time.time()
    hit = _cred_usage_cache.get(workspace_id)
    if hit and (now - hit[0]) < _CACHE_TTL_SECONDS:
        return hit[1]
    result = compute_credential_usage(workspace_id)
    _cred_usage_cache[workspace_id] = (now, result)
    return result


def invalidate_connection_usage(workspace_id: str) -> None:
    _conn_usage_cache.pop(workspace_id, None)


def invalidate_credential_usage(workspace_id: str) -> None:
    _cred_usage_cache.pop(workspace_id, None)


__all__ = [
    "compute_workspace_usage",
    "compute_workspace_usage_cached",
    "compute_connection_usage",
    "compute_connection_usage_cached",
    "compute_credential_usage",
    "compute_credential_usage_cached",
    "invalidate",
    "invalidate_all",
    "invalidate_connection_usage",
    "invalidate_credential_usage",
]
