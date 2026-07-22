"""Connector-draft review API — the human gate for Copilot-drafted connectors.

The AI Copilot proposes a connector via the `draft_connector_from_*` tools; it
lands here as an inert PROPOSED draft. Only an admin can approve it — which, for
a runnable (OpenAPI-derived) draft, activates it as a live Beta connector via
`rest_framework.save_user_manifest`. Mirrors the Steward Memory-Layer
`POST /api/steward/lessons/{id}/approve` gate.

Credentials are never involved here: a draft holds auth *templates* only; the
API key is added later on the Connection.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from fpulse.auth.deps import require_auth, require_min_rank
from fpulse.connectors.drafts import DraftConnector, DraftStatus, default_draft_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/connectors/drafts", tags=["connector-drafts"])


def _approver(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if user is not None:
        email = getattr(user, "email", None)
        if email:
            return str(email)
    return "admin"


def _summary(d: DraftConnector) -> dict[str, Any]:
    return {
        "id": d.id,
        "connector_id": d.connector_id,
        "display_name": d.display_name,
        "mode": d.mode,
        "runnable": d.runnable,
        "status": d.status.value,
        "stream_count": len(d.manifest.get("streams") or []),
        "summary": d.summary,
        "source": d.source,
        "proposed_by": d.proposed_by,
        "created_at": d.created_at,
        "activated_connector_id": d.activated_connector_id,
    }


class RejectRequest(BaseModel):
    reason: str = ""


@router.get("", dependencies=[Depends(require_auth)])
async def list_drafts(status: str | None = None) -> dict[str, Any]:
    """List drafted connectors (any signed-in user can review; only admins act)."""
    store = default_draft_store()
    st: DraftStatus | None = None
    if status:
        try:
            st = DraftStatus(status)
        except ValueError:
            raise HTTPException(400, f"invalid status: {status!r}")
    drafts = store.list_all(status=st)
    return {"drafts": [_summary(d) for d in drafts], "count": len(drafts)}


@router.get("/{draft_id}", dependencies=[Depends(require_auth)])
async def get_draft(draft_id: str) -> dict[str, Any]:
    store = default_draft_store()
    draft = store.get(draft_id)
    if draft is None:
        raise HTTPException(404, "no such draft")
    return draft.model_dump(mode="json")


@router.post("/{draft_id}/approve", dependencies=[Depends(require_min_rank("admin"))])
async def approve_draft(draft_id: str, request: Request) -> dict[str, Any]:
    """Approve a PROPOSED draft. Runnable drafts activate as a live Beta connector."""
    store = default_draft_store()
    try:
        result = store.approve(draft_id, _approver(request))
    except ValueError as exc:
        # Activation of a runnable draft failed (malformed manifest / id clash) —
        # the draft stays PROPOSED. Surface the reason.
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("connector draft approve failed")
        raise HTTPException(500, "failed to approve draft") from exc
    if result is None:
        raise HTTPException(409, "draft not found or not in PROPOSED state")
    draft, activation = result
    return {"id": draft.id, "status": draft.status.value, "connector_id": draft.connector_id, **activation}


@router.post("/{draft_id}/reject", dependencies=[Depends(require_min_rank("admin"))])
async def reject_draft(draft_id: str, request: Request, body: RejectRequest | None = None) -> dict[str, Any]:
    store = default_draft_store()
    reason = (body.reason if body else "") or ""
    draft = store.reject(draft_id, _approver(request), reason)
    if draft is None:
        raise HTTPException(409, "draft not found or not in PROPOSED state")
    return {"id": draft.id, "status": draft.status.value}
