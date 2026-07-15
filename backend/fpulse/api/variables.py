"""Variables CRUD API — global and project-scoped key-value pairs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from fpulse.auth.deps import current_workspace_id, require_auth, require_min_rank
from fpulse.variables.models import Variable, VariableCreate, VariableUpdate

# 2026-05-30 (Track S P1): same router-level + write-level guard pattern
# as workflows/schedules. Variables hold runtime values that downstream
# pipelines consume, so write authority gates at data_engineer+.
_AUTH = Depends(require_auth)
_AUTHOR = Depends(require_min_rank("developer"))

router = APIRouter(
    prefix="/api/variables",
    tags=["variables"],
    dependencies=[_AUTH],
)


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


def get_store():
    from fpulse.main import app_state
    return app_state["variable_store"]


# 2026-05-30 (P7): trailing-slash alias.
@router.get("", include_in_schema=False)
@router.get("/")
async def list_variables(
    scope: str | None = None,
    project_id: str | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List variables scoped to the caller's workspace."""
    store = get_store()
    return store.list_all(scope=scope, project_id=project_id, workspace_id=workspace_id)


@router.post("", dependencies=[_AUTHOR], include_in_schema=False)
@router.post("/", dependencies=[_AUTHOR])
async def create_variable(
    body: VariableCreate,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create a new variable in the caller's workspace."""
    store = get_store()
    variable = Variable(
        key=body.key,
        value=body.value,
        type=body.type,
        scope=body.scope,
        project_id=body.project_id,
        workspace_id=workspace_id,
        description=body.description,
    )
    created = store.create(variable)
    return created.model_dump(mode="json")


@router.get("/{variable_id}")
async def get_variable(
    variable_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Get a variable by ID — 404 across workspace boundary."""
    store = get_store()
    variable = store.get(variable_id, workspace_id=workspace_id)
    if not variable:
        raise HTTPException(404, "Variable not found")
    return variable.model_dump(mode="json")


@router.put("/{variable_id}", dependencies=[_AUTHOR])
async def update_variable(
    variable_id: str,
    body: VariableUpdate,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Update a variable — refuses to cross workspace boundary."""
    store = get_store()
    updates = body.model_dump(exclude_none=True)
    variable = store.update(variable_id, updates, workspace_id=workspace_id)
    if not variable:
        raise HTTPException(404, "Variable not found")
    return variable.model_dump(mode="json")


@router.delete("/{variable_id}", dependencies=[_AUTHOR])
async def delete_variable(
    variable_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Delete a variable — scoped to workspace."""
    store = get_store()
    if not store.delete(variable_id, workspace_id=workspace_id):
        raise HTTPException(404, "Variable not found")
    return {"deleted": True}


@router.post("/resolve")
async def resolve_variable(
    key: str,
    project_id: str | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Resolve a variable value — project scope first, then global fallback,
    both restricted to the caller's workspace."""
    store = get_store()
    value = store.resolve(key, project_id, workspace_id=workspace_id)
    if value is None:
        raise HTTPException(404, f"Variable '{key}' not found")
    return {"key": key, "value": value}
