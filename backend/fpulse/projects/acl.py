"""Shared project-access ACL helper.

2026-05-22 (audit E1) — until now the project ACL logic
(``_can_see`` in ``api/projects.py``) was only consulted by the
projects endpoints. The workflow / schedule / alert APIs scoped by
workspace but did NOT re-check project ACL, so a workspace member
who wasn't on a project's members list could still call
``GET /workflows?project_id=...`` and pull the project's pipelines.

This module exposes:

  * ``can_see_project(user, project_dict)`` — pure predicate, same
    rules as ``api/projects.py:_can_see``. Lifted here so other API
    modules don't have to import from a sibling api module.
  * ``assert_project_access(project_id, workspace_id, user, action)``
    — load the project, enforce workspace boundary + ACL, return the
    project on success. Raises ``HTTPException(404)`` on any failure
    (404, not 403, so project ids can't be enumerated by guessing).
    The ``action`` argument is informational today — reserved for a
    future per-action gate (read / write / delete).

Workspace-write paths (workflow create/update, schedule create,
alert create) should call this BEFORE store mutation so the
mutation is atomic with respect to the access decision.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


_ADMIN_ROLES = {"super_admin", "admin"}


def can_see_project(user: Any, project_dict: dict) -> bool:
    """Pure ACL predicate. Same rules as ``api/projects.py:_can_see``.

    Returns True iff ``user`` can see the project described by
    ``project_dict``. Admin role bypasses the ACL.

    The predicate is intentionally permissive on legacy rows (no
    members + no owner_id) so existing single-tenant installs keep
    working until an admin actually starts scoping access.
    """
    role = (getattr(user, "role", "") or "").lower()
    if role in _ADMIN_ROLES:
        return True

    user_id = getattr(user, "id", "") or ""
    user_projects = getattr(user, "projects", None) or []

    pid = project_dict.get("id")
    owner_id = project_dict.get("owner_id", "") or ""
    members = project_dict.get("members", []) or []

    if not user_projects:
        # No restriction set on the user — only bar them if the
        # project has an explicit member list that doesn't include
        # them. Owner always sees their own project.
        if members or owner_id:
            return user_id == owner_id or user_id in members
        return True

    if pid in user_projects:
        return True
    if user_id == owner_id:
        return True
    if user_id in members:
        return True
    return False


def assert_project_access(
    project_id: str,
    workspace_id: str,
    user: Any,
    action: str = "read",
) -> dict:
    """Load and gate a project. Returns the project dict on success.

    Raises ``HTTPException(404)`` if any of the following hold:
      * project doesn't exist
      * project is in a different workspace
      * user can't see the project (per ``can_see_project``)

    All three failure modes return 404 (NOT 403) so the existence of
    inaccessible projects doesn't leak — same shape as
    ``api/projects.py`` get_project / list_project_pipelines.

    Parameters
    ----------
    project_id : str
        The project id from the request path / body.
    workspace_id : str
        Caller's workspace id (from the X-Workspace-Id header dep).
    user : Any
        Authenticated user object — needs ``id``, ``role``, optionally
        ``projects`` allow-list.
    action : str
        Informational, reserved for future per-action policy. Today
        every action that calls this helper gets the same check.
    """
    # The "default" project sentinel exists on every install and is
    # intentionally accessible to every authenticated user — that's
    # how the new-pipeline flow works for users who haven't been
    # assigned a project. Short-circuit so we don't 404 it.
    if project_id == "default" or not project_id:
        return {"id": project_id or "default", "workspace_id": workspace_id, "name": "Default"}

    try:
        from fpulse.main import app_state
        store = app_state.get("project_store")
    except Exception:
        store = None
    if store is None:
        # No project store — return a synthetic dict so callers don't
        # have to special-case. Best-effort: this only fires during
        # tests / broken init.
        return {"id": project_id, "workspace_id": workspace_id}

    project = store.get(project_id)
    if project is None:
        raise HTTPException(404, "Project not found")

    proj_dict = project.model_dump(mode="json") if hasattr(project, "model_dump") else dict(project)
    proj_ws = proj_dict.get("workspace_id", "default")
    if proj_ws != workspace_id:
        # Cross-workspace — same 404 as "not found" so tenant-other
        # project ids don't leak.
        raise HTTPException(404, "Project not found")

    if not can_see_project(user, proj_dict):
        raise HTTPException(404, "Project not found")

    return proj_dict


__all__ = [
    "assert_project_access",
    "can_see_project",
]
