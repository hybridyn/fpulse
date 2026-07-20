"""Marketplace API — community pipeline template sharing, ratings, discovery."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from fpulse.auth.deps import current_workspace_id

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])


def _get_store(request: Request):
    # Stage 2: explicit feature-flag guard. If FPULSE_ENABLE_MARKETPLACE=0,
    # the store was never instantiated in lifespan; require() raises
    # FeatureDisabledError → 503 with a clear reason instead of a confusing
    # KeyError → 500.
    from fpulse.feature_flags import require
    from fpulse.main import app_state
    require("marketplace")
    return app_state["marketplace_store"]


class PublishRequest(BaseModel):
    name: str
    description: str = ""
    category: str = "community"
    tags: list[str] = []
    difficulty: str = "intermediate"
    icon: str = "📦"
    steps: list[dict] = []
    connections: list[dict] = []


class PublishFromWorkflowRequest(BaseModel):
    workflow_id: str
    category: str = "community"
    tags: list[str] = []
    difficulty: str = "intermediate"
    icon: str = "📦"


class RateRequest(BaseModel):
    rating: int
    review: str = ""


@router.get("")
async def list_marketplace(
    category: str | None = Query(None),
    search: str | None = Query(None),
    sort: str = Query("downloads", pattern="^(downloads|rating|newest|name)$"),
    limit: int = Query(100, ge=1, le=500),
    request: Request = None,
):
    """Browse marketplace templates."""
    store = _get_store(request)
    return store.list_all(category=category, search=search, sort=sort, limit=limit)


@router.get("/mine")
async def my_templates(request: Request, workspace_id: str = Depends(current_workspace_id)):
    """List templates published by the current user."""
    store = _get_store(request)
    user_id = getattr(request.state, "user_id", "anonymous")
    return store.get_by_author(user_id)


@router.get("/{template_id}")
async def get_template(template_id: str, request: Request):
    """Get a marketplace template with full details."""
    store = _get_store(request)
    tpl = store.get(template_id)
    if not tpl:
        raise HTTPException(404, "Template not found")
    return tpl


@router.post("")
async def publish_template(
    body: PublishRequest,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
):
    """Publish a new template to the marketplace."""
    store = _get_store(request)
    user_id = getattr(request.state, "user_id", "anonymous")
    user_name = getattr(request.state, "username", user_id)
    return store.publish(
        name=body.name, description=body.description, category=body.category,
        steps=body.steps, connections=body.connections,
        author_id=user_id, author_name=user_name,
        workspace_id=workspace_id, tags=body.tags,
        difficulty=body.difficulty, icon=body.icon,
    )


@router.post("/from-workflow")
async def publish_from_workflow(
    body: PublishFromWorkflowRequest,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
):
    """Publish an existing workflow as a marketplace template."""
    store = _get_store(request)
    from fpulse.main import app_state
    wf_store = app_state["store"]
    wf_version = wf_store.get(body.workflow_id, workspace_id=workspace_id)
    if not wf_version:
        raise HTTPException(404, f"Workflow not found: {body.workflow_id}")

    user_id = getattr(request.state, "user_id", "anonymous")
    user_name = getattr(request.state, "username", user_id)
    return store.publish_from_workflow(
        wf_version.workflow,
        author_id=user_id, author_name=user_name,
        category=body.category, tags=body.tags,
        difficulty=body.difficulty, icon=body.icon,
    )


@router.post("/{template_id}/download")
async def download_template(template_id: str, request: Request):
    """Download / use a marketplace template (increments counter)."""
    store = _get_store(request)
    tpl = store.get(template_id)
    if not tpl:
        raise HTTPException(404, "Template not found")
    store.increment_downloads(template_id)
    return tpl


@router.post("/{template_id}/rate")
async def rate_template(
    template_id: str,
    body: RateRequest,
    request: Request,
):
    """Rate a marketplace template (1-5 stars)."""
    store = _get_store(request)
    user_id = getattr(request.state, "user_id", "anonymous")
    return store.rate(template_id, user_id, body.rating, body.review)


@router.get("/{template_id}/ratings")
async def get_ratings(template_id: str, request: Request):
    """Get all ratings for a template."""
    store = _get_store(request)
    return store.get_ratings(template_id)


@router.delete("/{template_id}")
async def delete_template(template_id: str, request: Request):
    """Delete a marketplace template."""
    store = _get_store(request)
    user_id = getattr(request.state, "user_id", "anonymous")
    store.delete(template_id, user_id)
    return {"status": "deleted"}
