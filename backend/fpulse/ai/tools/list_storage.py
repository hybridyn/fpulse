"""list_storage — read-only. Workspace Storage inventory.

Single consolidated tool so the agent can answer questions like:
  * "What files do I have?"
  * "Is there an orders table?"
  * "Did the run produce any outputs?"

without bloating the tool registry with three separate list_* tools.
The response is shaped to be cheap to embed in a prompt (counts,
names, sizes) — for full row payloads the agent can still call the
REST APIs directly via the HTTP tool.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


def _summarize_files(objs: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for o in objs:
        # DataStore returns Pydantic StorageObject; tolerate dicts too.
        get = o.get if isinstance(o, dict) else (lambda k, default=None: getattr(o, k, default))
        if get("deleted_at"):
            continue
        out.append({
            "id": get("id", ""),
            "name": get("name", ""),
            "path": get("path", ""),
            "format": get("format") or "",
            "size_bytes": int(get("size_bytes") or 0),
            "project_id": get("project_id") or None,
            "tags": list(get("tags") or []),
        })
    return out


def _summarize_tables(tables: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tables:
        get = t.get if isinstance(t, dict) else (lambda k, default=None: getattr(t, k, default))
        out.append({
            "id": get("id", ""),
            "schema": get("schema_name", "default"),
            "name": get("name", ""),
            "rows": int(get("row_count") or 0),
            "columns": int(get("column_count") or 0),
            "size_bytes": int(get("size_bytes") or 0),
            "project_id": get("project_id") or None,
        })
    return out


def _summarize_outputs(objs: list[Any]) -> list[dict[str, Any]]:
    """Group outputs by (pipeline_id, run_id) for a compact summary."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for o in objs:
        get = o.get if isinstance(o, dict) else (lambda k, default=None: getattr(o, k, default))
        if get("deleted_at"):
            continue
        pid = str(get("pipeline_id") or "")
        rid = str(get("run_id") or "")
        key = (pid, rid)
        g = groups.setdefault(key, {
            "pipeline_id": pid, "run_id": rid,
            "object_count": 0, "size_bytes": 0,
        })
        g["object_count"] += 1
        g["size_bytes"] += int(get("size_bytes") or 0)
    return list(groups.values())


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Return a single rolled-up view of files / tables / outputs.

    Inputs (all optional):
      kind         — 'files' | 'tables' | 'outputs' | 'all' (default 'all')
      project_id   — filter to one project (None / omit = workspace-global view)
      include_system_docs — include bundled product documentation rows in files
                            (default False so the agent's reply doesn't drown
                            in 27 doc entries on a default install)
      limit        — cap per-kind list size (default 50)
    """
    workspace_id = ctx.workspace_id or ctx.tenant_id or "default"
    kind = (inputs.get("kind") or "all").lower()
    project_id = inputs.get("project_id")
    include_system = bool(inputs.get("include_system_docs", False))
    limit = int(inputs.get("limit") or 50)

    files: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    file_count = table_count = output_count = 0
    file_bytes = table_bytes = output_bytes = 0

    try:
        from fpulse.datastore.store import get_store
        from fpulse.datastore.models import OBJECT_KIND_FILE, OBJECT_KIND_OUTPUT
        from fpulse.api.reports import _DOC_CATALOG, _detect_tier
        store = get_store()
    except Exception as exc:
        return {
            "error": f"storage_unavailable: {type(exc).__name__}: {exc}",
            "files": [], "tables": [], "outputs": [],
            "workspace_id": workspace_id,
        }

    if kind in ("files", "all"):
        try:
            objs = store.list_objects(
                workspace_id,
                kind=OBJECT_KIND_FILE,
                project_id=project_id,
                include_deleted=False,
            )
            file_summaries = _summarize_files(objs)
            file_count = len(file_summaries)
            file_bytes = sum(f["size_bytes"] for f in file_summaries)
            files = file_summaries[:limit]
            # Optional: append bundled doc rows so the agent can answer
            # "do I have the quickstart guide?" even though docs live
            # outside the user-uploads tree.
            if include_system and project_id is None:
                is_plus = _detect_tier() == "plus"
                for d in _DOC_CATALOG:
                    if d.get("plus_only") and not is_plus:
                        continue
                    files.append({
                        "id": f"sys_doc:{d['path']}",
                        "name": d.get("title") or d["path"],
                        "path": f"_bundled/docs/{d['path']}",
                        "format": "md",
                        "size_bytes": 0,
                        "project_id": None,
                        "tags": ["system", "doc"],
                    })
                files = files[:limit]
        except Exception as exc:
            files = []
            file_count = 0
            file_bytes = 0
            return {
                "error": f"list_files_failed: {type(exc).__name__}: {exc}",
                "files": [], "tables": [], "outputs": [],
                "workspace_id": workspace_id,
            }

    if kind in ("tables", "all"):
        try:
            tbls = store.list_tables(workspace_id) if hasattr(store, "list_tables") else []
            table_summaries = _summarize_tables(tbls)
            table_count = len(table_summaries)
            table_bytes = sum(t["size_bytes"] for t in table_summaries)
            tables = table_summaries[:limit]
        except Exception:
            tables = []

    if kind in ("outputs", "all"):
        try:
            objs = store.list_objects(
                workspace_id,
                kind=OBJECT_KIND_OUTPUT,
                project_id=project_id,
                include_deleted=False,
            )
            output_groups = _summarize_outputs(objs)
            output_count = sum(g["object_count"] for g in output_groups)
            output_bytes = sum(g["size_bytes"] for g in output_groups)
            outputs = output_groups[:limit]
        except Exception:
            outputs = []

    return {
        "files": files,
        "tables": tables,
        "outputs": outputs,
        "totals": {
            "file_count": file_count,
            "file_bytes": file_bytes,
            "table_count": table_count,
            "table_bytes": table_bytes,
            "output_count": output_count,
            "output_bytes": output_bytes,
        },
        "workspace_id": workspace_id,
    }


DEFINITION = ToolDefinition(
    name="list_storage",
    tier=ToolTier.READ,
    description=(
        "List the workspace's Storage inventory — uploaded data files, managed "
        "Parquet tables, and pipeline-output artifacts. Returns counts, sizes, "
        "and a capped sample of each (default 50). Use when the user asks: "
        "\"what files do I have\", \"is there a customers table\", \"did the "
        "last run produce outputs\", or wants to discover what data is "
        "available before building a pipeline. Pass `kind` to narrow to one "
        "category, `project_id` to scope to a project, or `include_system_docs` "
        "to also surface the bundled product documentation rows."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["files", "tables", "outputs", "all"],
                "description": "Which inventory slice to return. Default 'all'.",
            },
            "project_id": {
                "type": "string",
                "description": "Optional — scope to one project. Omit for workspace-global view.",
            },
            "include_system_docs": {
                "type": "boolean",
                "description": "Append bundled doc rows (read-only system files). Default false.",
            },
            "limit": {
                "type": "integer",
                "description": "Cap per-kind list size. Default 50.",
            },
        },
    },
    output_schema={
        "files": "list",
        "tables": "list",
        "outputs": "list",
        "totals": "dict",
        "workspace_id": "str",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["storage", "read", "list"],
)
