"""Schema Contracts API — create, validate, and detect drift in step schemas."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from fpulse.auth.deps import current_workspace_id

router = APIRouter(prefix="/api/contracts", tags=["contracts"])


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


def get_contract_store():
    from fpulse.main import app_state
    return app_state["contract_store"]


def get_workflow_store():
    from fpulse.main import app_state
    return app_state["store"]


def get_data_dir():
    from fpulse.main import app_state
    return app_state["data_dir"]


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ColumnSpec(BaseModel):
    name: str
    type: str = "VARCHAR"
    nullable: bool = True
    constraints: dict[str, Any] | None = None


class ContractCreate(BaseModel):
    workflow_id: str
    step_id: str
    columns: list[ColumnSpec]
    description: str = ""


class SchemaInput(BaseModel):
    """Actual schema to validate against a contract."""
    columns: list[dict[str, Any]] = Field(
        ...,
        description="List of column dicts with keys: name, type, nullable",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/")
async def create_contract(
    body: ContractCreate,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create a schema contract for a step (scoped to the caller's
    workspace). The parent workflow must belong to the same workspace —
    cross-workspace contract creation returns 404 on the workflow
    lookup, deliberately the same response as "does not exist"."""
    store = get_contract_store()

    # Verify the workflow and step exist within this workspace
    wf_store = get_workflow_store()
    v = wf_store.get(body.workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    step_ids = {s.id for s in v.workflow.steps}
    if body.step_id not in step_ids:
        raise HTTPException(404, f"Step '{body.step_id}' not found in workflow")

    contract = store.create_contract(
        workflow_id=body.workflow_id,
        step_id=body.step_id,
        columns=[c.model_dump() for c in body.columns],
        description=body.description,
        workspace_id=workspace_id,
    )

    return contract.model_dump(mode="json")


@router.post("/validate/{contract_id}")
async def validate_contract(
    contract_id: str,
    body: SchemaInput,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Validate an actual schema against a contract.

    Pass the current output schema of the step and the system will check
    for breaking changes, warnings, and informational drifts. Scoped to
    the caller's workspace.
    """
    store = get_contract_store()
    contract = store.get_contract(contract_id, workspace_id=workspace_id)
    if not contract:
        raise HTTPException(404, "Contract not found")

    validation = store.validate_against(
        contract_id, body.columns, workspace_id=workspace_id,
    )
    return validation.model_dump(mode="json")


@router.get("/drift/{contract_id}")
async def check_drift(
    contract_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Check for schema drift using the last execution's output schema
    (scoped to caller's workspace).

    Runs the workflow step to get current output schema, then compares
    against the contract. For a lighter check, use POST /validate
    with a known schema instead.
    """
    store = get_contract_store()
    contract = store.get_contract(contract_id, workspace_id=workspace_id)
    if not contract:
        raise HTTPException(404, "Contract not found")

    # Try to get actual schema from last execution
    wf_store = get_workflow_store()
    v = wf_store.get(contract.workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    # Execute the step to get current schema
    from fpulse.engine.executor import WorkflowExecutor
    executor = WorkflowExecutor(data_dir=get_data_dir())

    try:
        result = executor.execute_step(v.workflow, contract.step_id, preview_limit=1)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("Failed to execute step for drift check")
        raise HTTPException(500, "Failed to execute step for drift check") from e

    if result.status == "error":
        raise HTTPException(
            500,
            f"Step execution failed during drift check: {result.error}",
        )

    # Build actual schema from result
    actual_schema = []
    for col_info in result.schema_info:
        actual_schema.append({
            "name": col_info.get("name", ""),
            "type": col_info.get("type", "VARCHAR"),
            "nullable": col_info.get("nullable", True),
        })

    drifts = store.detect_drift(
        contract_id, actual_schema, workspace_id=workspace_id,
    )

    return {
        "contract_id": contract_id,
        "step_id": contract.step_id,
        "drifts": [d.model_dump(mode="json") for d in drifts],
        "has_breaking": any(d.severity == "breaking" for d in drifts),
        "actual_schema": actual_schema,
    }


@router.post("/auto-create/{workflow_id}")
async def auto_create_contracts(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Auto-create schema contracts from the last successful run
    (scoped to caller's workspace).

    Executes the entire workflow and captures each step's output schema
    as a contract. Existing contracts for the same step are left intact.
    """
    wf_store = get_workflow_store()
    v = wf_store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    wf = v.workflow
    contract_store = get_contract_store()

    # Execute the workflow to get schemas
    from fpulse.engine.executor import WorkflowExecutor
    executor = WorkflowExecutor(data_dir=get_data_dir())

    try:
        from fpulse.security.execution_codes import mint_for_run
        run_result = executor.execute_workflow(wf, preview_limit=1, execution_code=mint_for_run(wf))
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("Workflow execution failed")
        raise HTTPException(500, "Workflow execution failed") from e

    if run_result.status == "error":
        # Still create contracts for steps that succeeded
        pass

    created = []
    skipped = []

    for step in wf.steps:
        step_result = run_result.step_results.get(step.id)
        if not step_result or step_result.status != "success":
            skipped.append({
                "step_id": step.id,
                "reason": "Step did not succeed" if step_result else "No result",
            })
            continue

        if not step_result.schema_info:
            skipped.append({
                "step_id": step.id,
                "reason": "No schema info available",
            })
            continue

        # Check if contract already exists for this step
        existing = contract_store.list_contracts_for_step(
            workflow_id, step.id, workspace_id=workspace_id,
        )
        if existing:
            skipped.append({
                "step_id": step.id,
                "reason": f"Contract already exists (id: {existing[0].id})",
            })
            continue

        # Build schema from step result
        actual_schema = []
        for col_info in step_result.schema_info:
            actual_schema.append({
                "name": col_info.get("name", ""),
                "type": col_info.get("type", "VARCHAR"),
                "nullable": col_info.get("nullable", True),
            })

        contract = contract_store.auto_create_from_schema(
            workflow_id=workflow_id,
            step_id=step.id,
            actual_schema=actual_schema,
            description=f"Auto-created from {step.label or step.id} ({step.type.value})",
            workspace_id=workspace_id,
        )
        created.append(contract.model_dump(mode="json"))

    return {
        "workflow_id": workflow_id,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "contracts": created,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# Catch-all routes (MUST be last to avoid shadowing specific routes)
# ---------------------------------------------------------------------------

@router.get("/{workflow_id}")
async def list_contracts(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List all schema contracts for a workflow (scoped to caller's
    workspace)."""
    store = get_contract_store()
    contracts = store.list_contracts(workflow_id, workspace_id=workspace_id)
    return [c.model_dump(mode="json") for c in contracts]


@router.get("/{workflow_id}/{contract_id}")
async def get_contract(
    workflow_id: str,
    contract_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Get a specific schema contract (scoped to caller's workspace)."""
    store = get_contract_store()
    contract = store.get_contract(contract_id, workspace_id=workspace_id)
    if not contract or contract.workflow_id != workflow_id:
        raise HTTPException(404, "Contract not found")
    return contract.model_dump(mode="json")


@router.delete("/{workflow_id}/{contract_id}")
async def delete_contract(
    workflow_id: str,
    contract_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Delete a schema contract (scoped to caller's workspace)."""
    store = get_contract_store()
    contract = store.get_contract(contract_id, workspace_id=workspace_id)
    if not contract or contract.workflow_id != workflow_id:
        raise HTTPException(404, "Contract not found")
    store.delete_contract(contract_id, workspace_id=workspace_id)
    return {"deleted": True}
