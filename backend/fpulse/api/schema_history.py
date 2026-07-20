"""Schema History API — audit + pre-run drift preview (2026-05-27).

Two endpoints, one router. Both are workspace-scoped and read-only:

  * ``GET /api/schema/history/{table_id}`` — chronological list of
    schema versions for a managed table. Returns ``[]`` if the table
    has never evolved under policy (a freshly promoted table sees its
    first row on the next sink write under a non-strict policy).

  * ``GET /api/schema/preview-drift/{workflow_id}`` — runs each
    managed-table sink in the workflow against its current upstream
    schema (cheap: source preview only, no sink write) and returns
    what each sink's ``schema_policy`` will DO for the next run.
    Drives the "Schema drift detected" panel in PreRunBanner so
    operators can review before triggering Live.

Both endpoints stay deliberately small — the policy decision logic
lives in ``intelligence/schema_policy.py``, the history persistence in
``intelligence/schema_history.py``. This file is the thin HTTP veneer.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from fpulse.auth.deps import current_workspace_id
from fpulse.intelligence.schema_policy import (
    DEFAULT_POLICY,
    SchemaPolicy,
    evaluate_policy,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/schema", tags=["schema"])


def _safe_workspace_id(request: Request) -> str:
    """Resolve workspace_id from auth deps, surfacing failures as 500.

    Mirrors the pattern from api/intelligence.py and api/contracts.py
    so the schema endpoints behave the same way under the same auth
    misconfigurations.
    """
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


def _get_history_store():
    from fpulse.main import app_state
    return app_state.get("schema_history_store")


def _get_datastore():
    from fpulse.main import app_state
    return app_state.get("datastore")


def _get_workflow_store():
    from fpulse.main import app_state
    return app_state.get("store")


def _get_data_dir() -> str:
    from fpulse.main import app_state
    return app_state.get("data_dir") or "."


# ── Response models ────────────────────────────────────────────────────


class SchemaVersionEntry(BaseModel):
    """One row of a managed table's schema history."""
    id: str
    version: int
    recorded_at: str
    columns: list[dict[str, Any]] = Field(default_factory=list)
    change_summary: dict[str, Any] = Field(default_factory=dict)
    applied_by_run_id: str = ""
    policy: str = "add_columns"


class SchemaHistoryResponse(BaseModel):
    table_id: str
    workspace_id: str
    versions: list[SchemaVersionEntry]
    current_version: int  # the latest version recorded; 0 if empty


class DriftPreviewSink(BaseModel):
    """One sink's pre-run drift summary."""
    step_id: str
    step_label: str
    sink_type: str               # local_table_sink | warehouse_sink | ...
    table_display_name: str
    policy: str
    ok: bool                     # False ⇒ this run will fail at the sink
    has_drift: bool
    severity: str = "info"
    rejection_reason: str | None = None
    # ``changes`` is shaped to drive the UI's "this run will: add column X,
    # widen Y from INT to BIGINT" rendering without the frontend having to
    # interpret the policy rules itself.
    changes: list[dict[str, Any]] = Field(default_factory=list)


class DriftPreviewResponse(BaseModel):
    workflow_id: str
    has_any_drift: bool
    has_any_rejection: bool
    sinks: list[DriftPreviewSink]


# ── GET /api/schema/history/{table_id} ─────────────────────────────────


@router.get("/history/{table_id}", response_model=SchemaHistoryResponse)
async def get_schema_history(
    table_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Return the chronological schema-evolution history for a managed table.

    Cross-workspace lookups return 404 exactly like a missing record —
    the existence of another tenant's table-id is itself non-public.

    Empty history (table has never evolved under policy) returns a 200
    with ``versions: []``. That's the normal case for tables that are
    only ever replaced wholesale or that have only had one writer
    since being promoted.
    """
    datastore = _get_datastore()
    if datastore is None:
        raise HTTPException(503, "datastore unavailable")

    # Workspace enforcement — fetching another tenant's managed table
    # by guessed id must 404, not leak existence. The datastore's
    # get_table returns None when workspace_id mismatches.
    table = datastore.get_table(table_id, workspace_id=workspace_id)
    if table is None:
        raise HTTPException(404, "managed table not found")

    store = _get_history_store()
    if store is None:
        # Mis-wired install — surface as 503 rather than empty list so
        # the operator notices schema_history_store didn't initialise.
        raise HTTPException(503, "schema_history_store unavailable")

    rows = store.list_for_table(table_id, workspace_id=workspace_id)

    versions = [
        SchemaVersionEntry(
            id=r["id"],
            version=r["version"],
            recorded_at=r["recorded_at"],
            columns=r["columns_json"] or [],
            change_summary=r["change_summary"] or {},
            applied_by_run_id=r["applied_by_run_id"] or "",
            policy=r["policy"] or DEFAULT_POLICY.value,
        )
        for r in rows
    ]
    return SchemaHistoryResponse(
        table_id=table_id,
        workspace_id=workspace_id,
        versions=versions,
        current_version=versions[-1].version if versions else 0,
    )


# ── GET /api/schema/preview-drift/{workflow_id} ────────────────────────


@router.get("/preview-drift/{workflow_id}", response_model=DriftPreviewResponse)
async def preview_drift(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """For every managed-table sink in this workflow, what would the
    next run do to the destination's schema?

    Strategy:
      * Walk the workflow's steps. For each sink that targets a managed
        table (currently ``local_table_sink``) we peek at the upstream
        step's already-known output schema. Two sources of truth:
          - The contract store, if a contract exists for the upstream step
          - The workflow-validation cache (``execute_step(preview_limit=1)``)
            as a fallback when no contract is pinned
      * Compare against the destination's current columns
      * Run ``evaluate_policy`` with the sink's configured ``schema_policy``
      * Return the decision verbatim

    The endpoint never *executes* the sink — it's a dry inspection.
    Worst-case cost is a sub-second source preview per sink.
    """
    wf_store = _get_workflow_store()
    if wf_store is None:
        raise HTTPException(503, "workflow store unavailable")
    v = wf_store.get(workflow_id, workspace_id=workspace_id)
    if v is None:
        raise HTTPException(404, "Workflow not found")
    wf = v.workflow

    datastore = _get_datastore()
    sinks: list[DriftPreviewSink] = []

    # Build a step_id → step map for upstream lookups.
    step_by_id = {s.id: s for s in wf.steps}

    # We may need to execute upstream steps to discover incoming columns —
    # done lazily and at preview_limit=1 so this stays cheap.
    executor = None

    for step in wf.steps:
        step_type_raw = step.type.value if hasattr(step.type, "value") else str(step.type)
        if step_type_raw not in ("local_table_sink", "warehouse_sink"):
            continue

        params = dict(step.params or {})
        policy_value = (
            params.get("schema_policy")
            or (DEFAULT_POLICY.value if params.get("auto_evolve", True) else SchemaPolicy.STRICT.value)
        )

        # ── Resolve existing destination schema ─────────────────────
        existing_cols: list[dict[str, Any]] = []
        display_name = ""
        if step_type_raw == "local_table_sink" and datastore is not None:
            schema_name = params.get("schema_name") or "default"
            table_name = params.get("table_name") or ""
            display_name = f"{schema_name}.{table_name}"
            if table_name:
                tbl = datastore.find_table_by_name(workspace_id, schema_name, table_name)
                if tbl is not None:
                    existing_cols = [
                        {"name": c.name, "type": c.type, "nullable": bool(c.nullable)}
                        for c in datastore.list_columns(table_id=tbl.id)
                    ]
        elif step_type_raw == "warehouse_sink":
            display_name = f"{params.get('schema', 'public')}.{params.get('table', '')}"
            # No managed table — existing schema would require executing
            # the connection. Skip the comparison for warehouse sinks in
            # OSS v1; the policy still surfaces via the UI dropdown.

        # ── Resolve incoming columns from the upstream step ─────────
        incoming_cols: list[dict[str, Any]] = []
        upstream_ids = list(params.get("_input_step_ids") or [])
        if not upstream_ids:
            # Fall back to connection graph: find any step that connects
            # INTO this sink. Connection has from_step / to_step.
            upstream_ids = [
                c.from_step for c in wf.connections if c.to_step == step.id
            ]
        if upstream_ids and step_by_id.get(upstream_ids[0]) is not None:
            try:
                if executor is None:
                    from fpulse.engine.executor import WorkflowExecutor
                    executor = WorkflowExecutor(data_dir=_get_data_dir())
                result = executor.execute_step(wf, upstream_ids[0], preview_limit=1)
                if result.status == "success":
                    for col_info in result.schema_info or []:
                        incoming_cols.append({
                            "name": col_info.get("name", ""),
                            "type": col_info.get("type", "VARCHAR"),
                            "nullable": col_info.get("nullable", True),
                        })
            except Exception as exc:
                logger.warning(
                    "drift-preview: upstream step %s execution failed: %s",
                    upstream_ids[0], exc,
                )

        # Skip sinks where we couldn't determine incoming schema —
        # better to omit than emit a false "all green" line.
        if not incoming_cols:
            continue

        decision = evaluate_policy(existing_cols, incoming_cols, policy_value)
        sinks.append(DriftPreviewSink(
            step_id=step.id,
            step_label=step.label or step.id,
            sink_type=step_type_raw,
            table_display_name=display_name,
            policy=decision.policy.value,
            ok=decision.ok,
            has_drift=decision.has_drift,
            severity=decision.severity,
            rejection_reason=decision.rejection_reason,
            changes=[c.to_dict() for c in decision.changes],
        ))

    return DriftPreviewResponse(
        workflow_id=workflow_id,
        has_any_drift=any(s.has_drift for s in sinks),
        has_any_rejection=any(not s.ok for s in sinks),
        sinks=sinks,
    )


__all__ = ["router"]
