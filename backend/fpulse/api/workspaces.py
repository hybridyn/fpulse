"""Workspaces API.

Endpoints for the multi-tenant Workspace foundation introduced in
schema v2. Provides:

  • CRUD on workspaces (admins create / rename / delete)
  • Membership management (invite, remove, role change)
  • Lookup endpoints for the frontend workspace switcher
  • The "claim a personal user into a corporate workspace" admin
    action, which solves the laptop→corporate scenario without
    losing the user's existing personal projects

Corporate-policy guards baked in:
  • Every write goes through `require_admin` (instance-level admin)
    OR a per-workspace admin role check, whichever is appropriate.
  • The Default workspace is treated as un-deletable (the v2 migration
    target).
  • Email-domain allowlist is enforced at invite time so a corporate
    workspace can refuse outside accounts even if the install accepts
    open signup at the instance level.
  • Every workspace mutation writes an audit row (action="workspace_*").
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from fpulse.auth.deps import current_user_optional, require_admin, require_auth
from fpulse.workspaces.models import (
    MemberInvite,
    MemberRoleUpdate,
    Workspace,
    WorkspaceCreate,
    WorkspaceUpdate,
    PLAN_FREE,
    ROLE_ADMIN,
    ROLE_DEVELOPER,
    ROLE_SUPER_ADMIN,
    WORKSPACE_ROLES,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


def _stores():
    """Resolve the workspace + user stores from app_state.

    Done lazily inside each endpoint instead of at import time so that
    the test suite can swap stores in/out via app_state without needing
    to re-import the router.
    """
    from fpulse.main import app_state
    return app_state.get("workspace_store"), app_state.get("user_store"), app_state


def _audit(action: str, user, **details) -> None:
    """Best-effort audit log. Never raises — auditing must not break
    user-facing operations even if the audit subsystem is down.
    """
    try:
        from fpulse.main import app_state
        audit = app_state.get("audit_logger")
        if audit:
            audit.log(
                user_id=getattr(user, "id", "system"),
                user_email=getattr(user, "email", "system"),
                action=action,
                resource_type="workspace",
                resource_id=details.get("workspace_id", ""),
                details=details,
            )
    except Exception:
        pass


def _serialise(ws: Workspace, *, member_count: int | None = None) -> dict:
    """Wire format for a Workspace. Hides nothing sensitive — there's
    nothing in the model that needs masking — but normalises the
    datetime fields to ISO strings.
    """
    out = ws.model_dump(mode="json")
    if member_count is not None:
        out["member_count"] = member_count
    return out


def _can_admin_workspace(user, workspace_id: str) -> bool:
    """True if `user` is allowed to manage `workspace_id`.

    Two paths to admin power over a workspace:
      1. Instance-level super_admin/admin (the install owner)
      2. Per-workspace `admin` or `super_admin` membership row

    Either is sufficient. Pure read access (membership of any role)
    is checked separately via `_can_view_workspace`.
    """
    if not user:
        return False
    if user.role in ("super_admin", "admin"):
        return True
    ws_store, _, _ = _stores()
    if not ws_store:
        return False
    role = ws_store.role_for(workspace_id, user.id)
    return role in (ROLE_ADMIN, ROLE_SUPER_ADMIN)


def _can_view_workspace(user, workspace_id: str) -> bool:
    """True if `user` may *see* the workspace at all (any membership)."""
    if not user:
        return False
    if user.role in ("super_admin", "admin"):
        return True
    ws_store, _, _ = _stores()
    if not ws_store:
        return False
    return ws_store.is_member(workspace_id, user.id)


# ── List & lookup ────────────────────────────────────────────────────────


@router.get("/")
async def list_workspaces(request: Request, user = Depends(require_auth)):
    """List workspaces visible to the caller.

    Instance-level admins see every workspace on the install.
    Other users see only the workspaces they're members of.

    Used by the top-nav workspace switcher (frontend) to populate
    the dropdown. The response also includes a `member_count` so
    the dropdown can show "Acme Corp · 12 members".
    """
    ws_store, _, _ = _stores()
    if not ws_store:
        raise HTTPException(503, "Workspace store not initialised")

    if user.role in ("super_admin", "admin"):
        workspaces = ws_store.list_all()
    else:
        workspaces = ws_store.list_for_user(user.id)

    out = []
    for ws in workspaces:
        members = ws_store.list_members(ws.id)
        accepted = sum(1 for m in members if m.accepted_at is not None)
        out.append(_serialise(ws, member_count=accepted))
    return out


@router.get("/{workspace_id}")
async def get_workspace(workspace_id: str, user = Depends(require_auth)):
    """Single workspace by id.

    404 (not 403) for non-members so the existence of a private
    workspace can't be enumerated by ID guessing.
    """
    ws_store, _, _ = _stores()
    if not ws_store:
        raise HTTPException(503, "Workspace store not initialised")
    if not _can_view_workspace(user, workspace_id):
        raise HTTPException(404, "Workspace not found")
    ws = ws_store.get(workspace_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    members = ws_store.list_members(workspace_id)
    return _serialise(ws, member_count=sum(1 for m in members if m.accepted_at is not None))


# ── Create / update / delete ─────────────────────────────────────────────


@router.post("/")
async def create_workspace(body: WorkspaceCreate, user = Depends(require_admin)):
    """Create a new workspace. Instance-admin only.

    The caller becomes the workspace owner AND a super_admin member of
    the new workspace, so they can immediately invite others without
    needing a second permission grant.
    """
    ws_store, _, _ = _stores()
    if not ws_store:
        raise HTTPException(503, "Workspace store not initialised")

    ws = Workspace(
        name=body.name,
        slug=body.slug or "",
        plan=PLAN_FREE,
        is_personal=False,
        owner_id=user.id,
        domain_allowlist=body.domain_allowlist,
    )
    created = ws_store.create(ws)
    ws_store.add_member(
        workspace_id=created.id,
        user_id=user.id,
        role=ROLE_SUPER_ADMIN,
        invited_by=user.id,
        auto_accept=True,
    )
    _audit("workspace_create", user, workspace_id=created.id, name=created.name)
    return _serialise(created, member_count=1)


@router.put("/{workspace_id}")
async def update_workspace(
    workspace_id: str, body: WorkspaceUpdate, user = Depends(require_auth)
):
    """Rename a workspace, change its slug, edit the domain allowlist.

    Allowed for instance admins OR per-workspace admins. The Plus
    activation state of a workspace is managed via the existing
    license endpoints, not here, so `plan` cannot be changed via this
    route — preventing a per-workspace admin from self-promoting to
    Plus without going through the license activation flow.
    """
    if not _can_admin_workspace(user, workspace_id):
        raise HTTPException(403, "Admin permission required for this workspace")
    ws_store, _, _ = _stores()
    updates = body.model_dump(exclude_none=True)
    updated = ws_store.update(workspace_id, updates)
    if not updated:
        raise HTTPException(404, "Workspace not found")
    _audit("workspace_update", user, workspace_id=workspace_id, fields=list(updates.keys()))
    return _serialise(updated)


@router.delete("/{workspace_id}")
async def delete_workspace(workspace_id: str, user = Depends(require_admin)):
    """Delete a workspace and all its memberships. Instance-admin only.

    The Default workspace is un-deletable — it's the back-fill target
    for legacy data and removing it would orphan every existing project
    on a v1→v2 upgraded install.
    """
    if workspace_id == "default":
        raise HTTPException(400, "The Default workspace cannot be deleted")
    ws_store, _, _ = _stores()
    if not ws_store.delete(workspace_id):
        raise HTTPException(404, "Workspace not found")
    _audit("workspace_delete", user, workspace_id=workspace_id)
    return {"deleted": True}


# ── Membership ───────────────────────────────────────────────────────────


@router.get("/{workspace_id}/members")
async def list_members(workspace_id: str, user = Depends(require_auth)):
    """List members of a workspace, joined with user details.

    Visible to any member of the workspace (so a developer can see
    "who else is on this team"), not just admins. We don't expose
    sensitive fields — just id, email, name, role, invited/accepted
    timestamps.
    """
    if not _can_view_workspace(user, workspace_id):
        raise HTTPException(404, "Workspace not found")
    ws_store, user_store, _ = _stores()
    members = ws_store.list_members(workspace_id)
    out = []
    for m in members:
        u = user_store.get_user(m.user_id) if hasattr(user_store, "get_user") else None
        out.append({
            "user_id": m.user_id,
            "email": getattr(u, "email", ""),
            "name": getattr(u, "name", ""),
            "role": m.role,
            "invited_by": m.invited_by,
            "invited_at": m.invited_at.isoformat() if m.invited_at else None,
            "accepted_at": m.accepted_at.isoformat() if m.accepted_at else None,
            "is_pending": m.accepted_at is None,
        })
    return out


@router.post("/{workspace_id}/members")
async def invite_member(
    workspace_id: str, body: MemberInvite, user = Depends(require_auth)
):
    """Invite an existing user into the workspace.

    Either `user_id` or `email` may be supplied; we look up by id first
    (cheaper, exact) and fall back to email lookup. The invited user
    must already exist as an account on this install — we don't create
    new accounts via this endpoint, that's a separate audited action.

    Corporate policy: if the workspace has a `domain_allowlist` set,
    the invitee's email domain must match (suffix match). Empty
    allowlist = no domain restriction.

    The 'claim a personal user' flow is just `invite_member` against a
    corporate workspace with the personal user's id — there's no
    separate endpoint because the semantics are identical (add user X
    to workspace Y), and the personal user keeps their personal
    workspace alongside the new corporate one.
    """
    if not _can_admin_workspace(user, workspace_id):
        raise HTTPException(403, "Admin permission required for this workspace")
    if body.role not in WORKSPACE_ROLES:
        raise HTTPException(400, f"Invalid role; must be one of {WORKSPACE_ROLES}")
    ws_store, user_store, _ = _stores()
    ws = ws_store.get(workspace_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")

    target = None
    if body.user_id:
        target = user_store.get_user(body.user_id) if hasattr(user_store, "get_user") else None
    if not target and body.email:
        target = user_store.get_user_by_email(body.email)
    if not target:
        raise HTTPException(404, "User not found")

    # Domain allowlist enforcement.
    if ws.domain_allowlist:
        email_lower = (target.email or "").lower()
        if not any(email_lower.endswith("@" + d) or email_lower.endswith("." + d)
                   for d in ws.domain_allowlist):
            raise HTTPException(
                403,
                f"Email domain not permitted in this workspace. "
                f"Allowed domains: {', '.join(ws.domain_allowlist)}",
            )

    member = ws_store.add_member(
        workspace_id=workspace_id,
        user_id=target.id,
        role=body.role,
        invited_by=user.id,
        auto_accept=True,  # admin-initiated → no separate accept step
    )
    _audit(
        "workspace_member_added", user,
        workspace_id=workspace_id,
        target_user_id=target.id,
        target_email=target.email,
        role=body.role,
    )
    return {
        "user_id": member.user_id,
        "email": target.email,
        "role": member.role,
        "accepted_at": member.accepted_at.isoformat() if member.accepted_at else None,
    }


@router.put("/{workspace_id}/members/{user_id}")
async def update_member_role(
    workspace_id: str, user_id: str, body: MemberRoleUpdate, user = Depends(require_auth)
):
    """Change a member's per-workspace role. Admins only."""
    if not _can_admin_workspace(user, workspace_id):
        raise HTTPException(403, "Admin permission required for this workspace")
    if body.role not in WORKSPACE_ROLES:
        raise HTTPException(400, f"Invalid role; must be one of {WORKSPACE_ROLES}")
    ws_store, _, _ = _stores()
    if not ws_store.update_member_role(workspace_id, user_id, body.role):
        raise HTTPException(404, "Membership not found")
    _audit("workspace_member_role_change", user,
           workspace_id=workspace_id, target_user_id=user_id, role=body.role)
    return {"updated": True}


@router.delete("/{workspace_id}/members/{user_id}")
async def remove_member(
    workspace_id: str, user_id: str, user = Depends(require_auth)
):
    """Remove a member from a workspace. Admins only.

    Refuses to remove the workspace owner — that would orphan the
    workspace. To transfer ownership, the admin must first PUT
    `/workspaces/{id}` with a new owner_id, then remove the old one.
    """
    if not _can_admin_workspace(user, workspace_id):
        raise HTTPException(403, "Admin permission required for this workspace")
    ws_store, _, _ = _stores()
    ws = ws_store.get(workspace_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if user_id == ws.owner_id:
        raise HTTPException(
            400,
            "Cannot remove the workspace owner. Transfer ownership first.",
        )
    if not ws_store.remove_member(workspace_id, user_id):
        raise HTTPException(404, "Membership not found")
    _audit("workspace_member_removed", user,
           workspace_id=workspace_id, target_user_id=user_id)
    return {"removed": True}
