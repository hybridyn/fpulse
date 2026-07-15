"""Data Lineage API — column-level lineage graph for pipeline visualization."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from fpulse.auth.deps import current_workspace_id

router = APIRouter(prefix="/api/lineage", tags=["lineage"])


def _get_lineage_store(request: Request):
    # Stage 2: feature flag guard. Gated by FPULSE_ENABLE_LINEAGE.
    from fpulse.feature_flags import require
    from fpulse.main import app_state
    require("lineage")
    return app_state["lineage_store"]


def _get_workflow_store(request: Request):
    # Workflow store is core (always-on) — no flag guard.
    from fpulse.main import app_state
    return app_state["store"]


# ── Consumer self-attestation endpoints (L3, 2026-06-08) ────────────
# Declared BEFORE the catch-all `/{workflow_id}` route below because
# FastAPI matches routes in declaration order - a GET /consumers
# would otherwise be matched by /{workflow_id} with
# workflow_id="consumers" and crash on the workflow-store lookup.
#
# Downstream consumers post themselves so F-Pulse can answer "if we
# change this output, what downstream breaks?". Honest protocol:
# this only sees consumers WHO ARE POLITE enough to register. The
# Plus tier (per docs/design/lineage-1.2.md L4) adds the Snowflake
# QUERY_HISTORY scraper for real auto-discovery.

@router.post("/consumers")
async def register_consumer(
    request: Request,
    body: dict[str, Any] = Body(...),
    workspace_id: str = Depends(current_workspace_id),
):
    """Register (or update) one downstream consumer of a F-Pulse output.

    Body:
      {
        "output_id":      "fpulse://workspace/<ws>/pipeline/<pl>/sink/<step>",
        "consumer_id":    "snowflake://prod-warehouse/analytics/orders_view",
        "consumer_type":  "snowflake_view" | "tableau_dashboard" |
                          "python_notebook" | "fpulse_pipeline" | "other",
        "last_read_at":   1717000000.0,           // optional, epoch seconds
        "attested_by":    "user@example.com",     // optional
        "notes":          "Read nightly by Analytics team"  // optional
      }

    Idempotent on (output_id, consumer_id, consumer_type) - re-attestation
    updates last_read_at without creating duplicates.
    """
    output_id = (body or {}).get("output_id")
    consumer_id = (body or {}).get("consumer_id")
    consumer_type = (body or {}).get("consumer_type")
    if not output_id or not consumer_id or not consumer_type:
        raise HTTPException(
            400,
            "output_id, consumer_id, and consumer_type are all required",
        )
    store = _get_lineage_store(request)
    rid = store.record_consumer(
        output_id=str(output_id),
        consumer_id=str(consumer_id),
        consumer_type=str(consumer_type),
        last_read_at=body.get("last_read_at"),
        attested_by=str(body.get("attested_by") or ""),
        notes=str(body.get("notes") or ""),
    )
    return {"recorded": True, "id": rid}


@router.get("/consumers")
async def list_consumers_for_output(
    request: Request,
    output_id: str,
    workspace_id: str = Depends(current_workspace_id),
):
    """Return every registered consumer for the given output_id,
    most-recently-attested first. Used by the UI "who reads this?"
    panel + by impact-analysis tools considering a schema change."""
    if not output_id:
        raise HTTPException(400, "output_id query parameter is required")
    store = _get_lineage_store(request)
    consumers = store.list_consumers(output_id)
    return {
        "output_id": output_id,
        "count": len(consumers),
        "consumers": consumers,
    }


@router.get("/consumers/_overview")
async def consumers_overview(
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
):
    """One row per output that has at least one registered consumer.
    Powers a "show me everything downstream knows about" view."""
    store = _get_lineage_store(request)
    rows = store.list_all_outputs_with_consumers()
    return {"count": len(rows), "outputs": rows}


@router.delete("/consumers")
async def deregister_consumer(
    request: Request,
    body: dict[str, Any] = Body(...),
    workspace_id: str = Depends(current_workspace_id),
):
    """Remove one consumer registration. Body shape mirrors POST -
    the (output_id, consumer_id, consumer_type) triple identifies the row."""
    output_id = (body or {}).get("output_id")
    consumer_id = (body or {}).get("consumer_id")
    consumer_type = (body or {}).get("consumer_type")
    if not output_id or not consumer_id or not consumer_type:
        raise HTTPException(
            400,
            "output_id, consumer_id, and consumer_type are all required",
        )
    store = _get_lineage_store(request)
    removed = store.delete_consumer(
        output_id=str(output_id),
        consumer_id=str(consumer_id),
        consumer_type=str(consumer_type),
    )
    return {"removed": removed}


@router.get("/{workflow_id}")
async def get_lineage_graph(
    workflow_id: str,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
):
    """Get the column-level lineage graph for a workflow (React Flow format)."""
    lineage_store = _get_lineage_store(request)
    graph = lineage_store.get_graph(workflow_id)
    if not graph["nodes"]:
        # Auto-build from workflow definition
        store = _get_workflow_store(request)
        wf_version = store.get(workflow_id, workspace_id=workspace_id)
        if not wf_version:
            raise HTTPException(404, f"Workflow not found: {workflow_id}")
        graph = lineage_store.build_from_workflow(wf_version.workflow)
    return graph


@router.post("/{workflow_id}/rebuild")
async def rebuild_lineage(
    workflow_id: str,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
):
    """Force-rebuild lineage graph from the current workflow definition."""
    lineage_store = _get_lineage_store(request)
    store = _get_workflow_store(request)
    wf_version = store.get(workflow_id, workspace_id=workspace_id)
    if not wf_version:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")
    graph = lineage_store.build_from_workflow(wf_version.workflow)
    return graph


# ── Runtime lineage endpoints (L1, 2026-06-08) ──────────────────────
# The /{workflow_id} GET above returns the DESIGN-TIME graph (inferred
# from workflow IR). These endpoints return RUNTIME facts: what
# actually ran on a specific run_id. Distinct from the audit log
# because lineage focuses on data shape (columns in / out, rows in /
# out) rather than success/failure events.

@router.get("/runs/{run_id}")
async def get_runtime_lineage(
    run_id: str,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
):
    """Step-level runtime lineage for one execution: per-step columns
    in/out, rows in/out, timing. Empty list if the run didn't emit
    lineage (e.g. nothing was wired to call record_step_run)."""
    lineage_store = _get_lineage_store(request)
    return lineage_store.get_runtime_lineage(run_id)


@router.get("/workflow/{workflow_id}/runs")
async def list_runs_with_lineage(
    workflow_id: str,
    request: Request,
    limit: int = 50,
    workspace_id: str = Depends(current_workspace_id),
):
    """List run_ids that have recorded runtime lineage for this
    workflow, most recent first. Powers a "pick a run to inspect"
    dropdown."""
    lineage_store = _get_lineage_store(request)
    return {
        "workflow_id": workflow_id,
        "limit": limit,
        "runs": lineage_store.get_runs_for_workflow(workflow_id, limit=limit),
    }


# (Consumer endpoints declared above the parametrized /{workflow_id}
# routes to avoid FastAPI's declaration-order route-matching collision.)


@router.get("/{workflow_id}/column/{column_name}")
async def get_column_lineage(
    workflow_id: str,
    column_name: str,
    request: Request,
):
    """Trace a single column through the entire pipeline."""
    lineage_store = _get_lineage_store(request)
    return lineage_store.get_column_lineage(workflow_id, column_name)


@router.delete("/{workflow_id}")
async def delete_lineage(
    workflow_id: str,
    request: Request,
):
    """Delete all lineage data for a workflow."""
    lineage_store = _get_lineage_store(request)
    lineage_store.delete_workflow_lineage(workflow_id)
    return {"status": "deleted", "workflow_id": workflow_id}
