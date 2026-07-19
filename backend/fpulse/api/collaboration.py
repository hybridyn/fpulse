"""Collaboration API — comments, threads, @mentions on pipelines."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from fpulse.auth.deps import current_workspace_id

router = APIRouter(prefix="/api/collaboration", tags=["collaboration"])


def _get_store(request: Request):
    # Stage 2: feature flag guard. Gated by FPULSE_ENABLE_COLLABORATION.
    from fpulse.feature_flags import require
    from fpulse.main import app_state
    require("collaboration")
    return app_state["collaboration_store"]


class CommentRequest(BaseModel):
    body: str
    step_id: str = ""
    parent_id: str = ""


class UpdateCommentRequest(BaseModel):
    body: str


@router.get("/{workflow_id}/comments")
async def list_comments(
    workflow_id: str,
    step_id: str | None = Query(None),
    include_resolved: bool = Query(True),
    request: Request = None,
):
    """List all comments for a workflow (optionally filtered by node)."""
    store = _get_store(request)
    return store.list_comments(workflow_id, step_id=step_id, include_resolved=include_resolved)


@router.get("/{workflow_id}/stats")
async def comment_stats(workflow_id: str, request: Request):
    """Get comment counts and unresolved threads for a workflow."""
    store = _get_store(request)
    return store.get_stats(workflow_id)


@router.post("/{workflow_id}/comments")
async def add_comment(
    workflow_id: str,
    body: CommentRequest,
    request: Request,
):
    """Add a comment to a workflow or node."""
    store = _get_store(request)
    user_id = getattr(request.state, "user_id", "anonymous")
    user_name = getattr(request.state, "username", user_id)
    comment = store.add_comment(
        workflow_id=workflow_id, author_id=user_id, body=body.body,
        step_id=body.step_id, parent_id=body.parent_id, author_name=user_name,
    )
    # Send notifications for @mentions
    if comment and comment.get("mentions"):
        _notify_mentions(request, workflow_id, comment)
    return comment


@router.put("/comments/{comment_id}")
async def update_comment(
    comment_id: str,
    body: UpdateCommentRequest,
    request: Request,
):
    """Update a comment (author only)."""
    store = _get_store(request)
    user_id = getattr(request.state, "user_id", "anonymous")
    result = store.update_comment(comment_id, body.body, user_id)
    if not result:
        raise HTTPException(403, "Cannot edit this comment")
    return result


@router.post("/comments/{comment_id}/resolve")
async def resolve_comment(comment_id: str, request: Request):
    """Mark a comment thread as resolved."""
    store = _get_store(request)
    user_id = getattr(request.state, "user_id", "anonymous")
    return store.resolve_comment(comment_id, user_id)


@router.post("/comments/{comment_id}/unresolve")
async def unresolve_comment(comment_id: str, request: Request):
    """Reopen a resolved comment thread."""
    store = _get_store(request)
    return store.unresolve_comment(comment_id)


@router.delete("/comments/{comment_id}")
async def delete_comment(comment_id: str, request: Request):
    """Delete a comment and its replies."""
    store = _get_store(request)
    user_id = getattr(request.state, "user_id", "anonymous")
    store.delete_comment(comment_id, user_id)
    return {"status": "deleted"}


@router.get("/mentions/me")
async def my_mentions(request: Request):
    """Get all comments that @mention the current user."""
    store = _get_store(request)
    user_id = getattr(request.state, "user_id", "anonymous")
    return store.get_mentions(user_id)


def _notify_mentions(request: Request, workflow_id: str, comment: dict):
    """Send notifications for @mentions in a comment."""
    try:
        from fpulse.main import app_state
        notification_store = app_state.get("notification_store")
        if not notification_store:
            return
        for mentioned_user in comment.get("mentions", []):
            notification_store.create(
                user_id=mentioned_user,
                title=f"@{comment.get('author_name', 'Someone')} mentioned you",
                body=comment["body"][:200],
                type="mention",
                link=f"#editor?workflow={workflow_id}&comment={comment['id']}",
            )
    except Exception:
        pass  # Non-critical — don't fail the comment creation
