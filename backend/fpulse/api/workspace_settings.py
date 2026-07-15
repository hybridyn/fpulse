"""Workspace settings API.

Per-workspace JSON blob of admin-tunable knobs. First user is
``enforce_two_person_approval`` (PR11) — when true, the Gate 2 deploy
approval must come from a different admin than Gate 1. Defaults to
false (single approver allowed).

Endpoints:

* ``GET  /api/plus/workspace-settings`` — read (any authenticated user)
* ``PUT  /api/plus/workspace-settings`` — write (admin only)

The single-row-per-workspace + free-form JSON shape lets us add new
settings without further migrations — pure additive evolution.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from fpulse.auth.deps import current_workspace_id, require_admin, require_auth


logger = logging.getLogger("fpulse.workspace_settings")
router = APIRouter(prefix="/api/plus/workspace-settings", tags=["workspace-settings"])


def _get_db():
    from fpulse.main import app_state
    db = app_state.get("db")
    if db is None:
        raise HTTPException(503, "Database not initialized")
    return db


# Default values for known settings — applied when no row exists yet.
DEFAULTS: dict[str, Any] = {
    "enforce_two_person_approval": False,
}


def _read_settings(workspace_id: str) -> dict[str, Any]:
    db = _get_db()
    row = db.fetchone(
        "SELECT settings FROM workspace_settings WHERE workspace_id = ?",
        (workspace_id,),
    )
    if not row:
        return dict(DEFAULTS)
    try:
        raw = row.get("settings")
        parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    # Merge over defaults so newly-introduced keys read sensibly.
    return {**DEFAULTS, **(parsed if isinstance(parsed, dict) else {})}


class WorkspaceSettingsResponse(BaseModel):
    workspace_id: str
    settings: dict[str, Any]


class UpdateWorkspaceSettingsBody(BaseModel):
    # Free-form patch object — only keys present here are updated;
    # missing keys keep their existing values.
    patch: dict[str, Any] = Field(default_factory=dict)


@router.get("", response_model=WorkspaceSettingsResponse)
def get_workspace_settings(
    _user=Depends(require_auth),
    workspace_id: str = Depends(current_workspace_id),
):
    """Read the workspace's settings dict (with defaults filled in)."""
    return WorkspaceSettingsResponse(
        workspace_id=workspace_id,
        settings=_read_settings(workspace_id),
    )


@router.put("", response_model=WorkspaceSettingsResponse)
def update_workspace_settings(
    body: UpdateWorkspaceSettingsBody = Body(...),
    user=Depends(require_admin),
    workspace_id: str = Depends(current_workspace_id),
):
    """Patch the workspace settings. Admin-only.

    Pass only the keys you want to change; existing keys are preserved.
    Audit-logged via the standard audit_logger.
    """
    db = _get_db()
    updated_by = getattr(user, "email", None) or getattr(user, "id", None) or "admin"
    now = datetime.now(timezone.utc).isoformat()

    current = _read_settings(workspace_id)
    merged = {**current, **(body.patch or {})}

    db.execute(
        """
        INSERT INTO workspace_settings (workspace_id, settings, updated_at, updated_by)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(workspace_id) DO UPDATE SET
            settings = excluded.settings,
            updated_at = excluded.updated_at,
            updated_by = excluded.updated_by
        """,
        (workspace_id, json.dumps(merged), now, updated_by),
    )
    db.commit()

    # Audit
    try:
        from fpulse.main import app_state
        audit_logger = app_state.get("audit_logger")
        if audit_logger:
            audit_logger.log(
                user_id=getattr(user, "id", "") or updated_by,
                user_email=getattr(user, "email", "") or updated_by,
                action="workspace_settings.changed",
                resource_type="workspace_settings",
                resource_id=workspace_id,
                details={"patch_keys": sorted(list((body.patch or {}).keys()))},
            )
    except Exception:
        pass

    return WorkspaceSettingsResponse(workspace_id=workspace_id, settings=merged)
