"""dbt project importer — turn a compiled dbt ``manifest.json`` into an
F-Pulse pipeline, to onboard dbt shops without a rewrite.

MVP contract (deliberate): we read the **compiled** manifest — the artifact
dbt writes to ``target/manifest.json`` after ``dbt compile`` / ``dbt docs
generate`` — and never the raw ``models/*.sql``. dbt has already resolved
``{{ ref }}`` / ``{{ source }}`` / macros / ``is_incremental()`` into
``compiled_code`` plus an explicit ``depends_on`` DAG, so we only read
*structure* + *finished SQL*. That keeps the importer to the standard library
(``json`` is done by the caller) with **no new dependency** and sidesteps the
entire Jinja/adapter-dialect problem, which is the hard part of dbt.

Mapping to the F-Pulse IR (see ``fpulse.ir.schema``):

    dbt model node        → SQL Transform step (``params.expression`` = SQL)
    dbt source            → Source placeholder step (bind a connection later)
    ``ref()`` edge        → StepConnection(alias = upstream model name)
    ``source()`` edge     → StepConnection(alias = source table name)

The ``alias`` on each edge is what makes the SQL runnable: F-Pulse's SQL
Transform registers every incoming relation under its edge alias, so a model
whose compiled SQL says ``FROM stg_orders`` finds ``stg_orders`` because the
edge from that upstream model carries ``alias="stg_orders"``.

``manifest_to_pipeline`` returns the same **portable pipeline dict** that
``POST /api/workflows/import`` already accepts, plus an honest ``report`` of
caveats (unbound sources, incremental models, missing/raw SQL, DuckDB dialect
risk) so the import surface can warn rather than silently mislead.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

# dbt materializations we recognise; anything else is passed through as-is.
_KNOWN_MATERIALIZATIONS = {
    "table", "view", "incremental", "ephemeral", "materialized_view",
}


def _safe_id(unique_id: str) -> str:
    """dbt ``unique_id`` (``model.project.name``) → a stable, valid step id.

    Deterministic so the same manifest always yields the same ids (re-import
    is diffable) and so edges resolve without a side lookup table.
    """
    s = re.sub(r"[^A-Za-z0-9]+", "_", unique_id or "").strip("_")
    return s or "node"


def manifest_to_pipeline(
    manifest: dict[str, Any],
    *,
    name: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Convert a compiled dbt ``manifest.json`` (parsed) into
    ``(pipeline_dict, report)``.

    ``pipeline_dict`` is import-ready (the shape ``POST /workflows/import``
    consumes). ``report`` carries counts + human-readable warnings so the
    caller can be honest about what will and won't run unchanged.

    Raises ``ValueError`` if ``manifest`` is not a dict.
    """
    if not isinstance(manifest, dict):
        raise ValueError("dbt manifest must be a JSON object")

    nodes = manifest.get("nodes")
    sources = manifest.get("sources")
    nodes = nodes if isinstance(nodes, dict) else {}
    sources = sources if isinstance(sources, dict) else {}

    models: dict[str, dict] = {
        uid: n for uid, n in nodes.items()
        if isinstance(n, dict) and n.get("resource_type") == "model"
    }

    warnings: list[str] = []
    incremental_models: list[str] = []
    missing_sql: list[str] = []

    # Stable id per model + source (edges reference these).
    id_of: dict[str, str] = {}
    for uid in models:
        id_of[uid] = _safe_id(uid)
    for uid in sources:
        id_of[uid] = _safe_id(uid)

    # Only materialise source nodes that a model actually reads — an
    # unreferenced source would be a dangling node on the canvas.
    referenced_sources: set[str] = set()
    for node in models.values():
        for dep in (node.get("depends_on") or {}).get("nodes") or []:
            if dep in sources:
                referenced_sources.add(dep)

    steps: list[dict] = []
    connections: list[dict] = []

    # ── Source placeholder nodes ─────────────────────────────────────────
    for uid in sorted(referenced_sources):
        src = sources.get(uid) or {}
        sname = src.get("name") or uid
        label = ".".join(x for x in (src.get("source_name"), sname) if x) or sname
        steps.append({
            "id": id_of[uid],
            "type": "source",
            "label": label,
            "params": {
                # Placeholders the UI surfaces so the operator binds a real
                # F-Pulse connection before running. Prefixed to avoid
                # colliding with any real source-node param.
                "_dbt_kind": "source",
                "_needs_connection": True,
                "table": sname,
                "source_name": src.get("source_name", ""),
            },
            "position": {},  # filled by _layout
        })
    if referenced_sources:
        warnings.append(
            f"{len(referenced_sources)} dbt source(s) imported as placeholder "
            "nodes — bind each to a real F-Pulse connection before running."
        )

    # ── Model nodes + edges ──────────────────────────────────────────────
    raw_sql_seen = False
    for uid, node in models.items():
        compiled = node.get("compiled_code") or node.get("compiled_sql")
        raw = node.get("raw_code") or node.get("raw_sql")
        sql = compiled or raw or ""
        if not compiled and raw:
            raw_sql_seen = True
        if not sql:
            missing_sql.append(node.get("name", uid))

        mat = (
            (node.get("config") or {}).get("materialized")
            or node.get("materialized")
            or "view"
        )
        if mat == "incremental":
            incremental_models.append(node.get("name", uid))

        steps.append({
            "id": id_of[uid],
            "type": "transform",
            "label": node.get("name", uid),
            "params": {
                "expression": sql,
                "_dbt_kind": "model",
                "_dbt_materialized": mat,
                "_dbt_schema": node.get("schema", ""),
            },
            "position": {},
        })

        for dep in (node.get("depends_on") or {}).get("nodes") or []:
            if dep in models:
                connections.append({
                    "from_step": id_of[dep],
                    "to_step": id_of[uid],
                    "alias": models[dep].get("name") or _safe_id(dep),
                })
            elif dep in referenced_sources:
                connections.append({
                    "from_step": id_of[dep],
                    "to_step": id_of[uid],
                    "alias": sources[dep].get("name") or _safe_id(dep),
                })

    # ── Honest caveats ───────────────────────────────────────────────────
    if raw_sql_seen:
        warnings.append(
            "Some models had no compiled SQL, so their raw SQL was used — it "
            "may still contain unresolved {{ }} templating. Run `dbt compile` "
            "first for a clean import."
        )
    if incremental_models:
        warnings.append(
            f"{len(incremental_models)} incremental model(s) imported as full "
            "rebuilds — dbt's is_incremental()/merge semantics don't map to a "
            "single SELECT. Review before scheduling."
        )
    if missing_sql:
        warnings.append(
            f"{len(missing_sql)} model(s) had no SQL body and were imported empty."
        )
    if models:
        warnings.append(
            "Model SQL is warehouse-dialect (from `dbt compile`); F-Pulse runs "
            "SQL Transforms on DuckDB. Some functions may need dialect tweaks — "
            "every node is editable after import."
        )

    _layout(steps, connections)

    meta = manifest.get("metadata") or {}
    proj_name = meta.get("project_name") or ""
    pipeline_name = name or (
        f"{proj_name} (dbt import)" if proj_name else "dbt project import"
    )

    pipeline = {
        "name": pipeline_name,
        "description": (
            "Imported from dbt project"
            + (f" '{proj_name}'" if proj_name else "")
            + f" — {len(models)} model(s), {len(referenced_sources)} source(s)."
        ),
        "metadata": {
            "source": "dbt",
            "dbt_project": proj_name,
            "dbt_schema_version": meta.get("dbt_schema_version", ""),
        },
        "steps": steps,
        "connections": connections,
    }
    report = {
        "models": len(models),
        "sources": len(referenced_sources),
        "connections": len(connections),
        "incremental_models": incremental_models,
        "warnings": warnings,
    }
    return pipeline, report


def _layout(steps: list[dict], connections: list[dict]) -> None:
    """Assign a left→right layered position to each step (in place).

    Column = longest dependency path to the node (its depth); rows stack
    within a column. Cheap, deterministic, and gives a readable DAG instead
    of every node piled at (0, 0).
    """
    ids = [s["id"] for s in steps]
    id_set = set(ids)
    preds: dict[str, list[str]] = {i: [] for i in ids}
    for c in connections:
        if c["to_step"] in id_set and c["from_step"] in id_set:
            preds[c["to_step"]].append(c["from_step"])

    depth: dict[str, int] = {}

    def _depth(nid: str, seen: frozenset[str]) -> int:
        if nid in depth:
            return depth[nid]
        if nid in seen:  # cycle guard — a real dbt DAG is acyclic, but be safe
            return 0
        parents = preds.get(nid, [])
        d = 0 if not parents else 1 + max(
            _depth(p, seen | {nid}) for p in parents
        )
        depth[nid] = d
        return d

    for i in ids:
        _depth(i, frozenset())

    per_column: dict[int, int] = defaultdict(int)
    for s in steps:
        col = depth.get(s["id"], 0)
        row = per_column[col]
        per_column[col] += 1
        s["position"] = {"x": col * 280, "y": row * 140}
