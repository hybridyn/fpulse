"""Project models for pipeline grouping."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class Project(BaseModel):
    """A project groups related pipelines together.

    Ownership model:
      - `owner_id` → the user.id of whoever created the project. Set by the
        API on create from the authenticated caller — NOT from the request
        body (preventing a caller from claiming someone else's identity).
      - `owner` → human-readable display name (email or name). Kept for
        backwards compatibility with the UI columns; may drift from
        `owner_id` if the owner renames themselves.
      - `members` → list of user.ids explicitly granted access to this
        project. Used as the inverse index for `user.projects` — either
        side being set grants access. Admins can always see every project
        regardless of membership.
    """
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    description: str = ""
    owner: str = "admin"
    owner_id: str = ""  # user.id of creator; empty only for pre-ACL legacy projects
    members: list[str] = Field(default_factory=list)
    # Schema v2: every project belongs to exactly one workspace. The
    # `default` workspace is the back-fill target for legacy single-tenant
    # rows so existing installs keep working without a data migration.
    # Self-signed-up users land their new projects in their personal
    # workspace; admins create projects in whichever workspace the
    # request was made against (X-Workspace-Id header).
    workspace_id: str = "default"
    # Project tree — None means a root-level project. Stored as a flat
    # parent pointer; the API exposes the full tree via /projects/tree.
    # Cycle prevention is enforced in the API layer on update/move.
    parent_id: str | None = None
    color: str = "#6366f1"
    icon: str = "folder"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Approval workflow
    approval_status: str = ""  # "" | "pending" | "approved" | "rejected"
    approval_notes: str = ""
    approved_by: str | None = None
    approved_at: datetime | None = None
    submitted_by: str | None = None
    # 2026-05-22: server-side archive lifecycle (audit C1).
    #   * archived_at / archived_by — when + who hit Archive.
    #     None on active projects.
    #   * status — "active" | "archived". Listed-by-default excludes
    #     archived rows; include_archived=true on /projects opt-in.
    # Previously archive was a frontend localStorage flag — that meant
    # archived-state was per-browser, unauditable, and easy to bypass
    # by another user opening the same project. Now it's authoritative.
    status: str = "active"
    archived_at: datetime | None = None
    archived_by: str | None = None


class ProjectCreate(BaseModel):
    """Create-project request body.

    `owner` / `owner_id` are intentionally NOT accepted here — the API
    stamps both fields from the authenticated session, so a caller can't
    spoof ownership by posting someone else's id.

    `metadata` is a free-form dict for project governance fields:
      cost_center, sponsor, department, priority, tags, etc.
    """
    name: str
    description: str = ""
    color: str = "#6366f1"
    icon: str = "folder"
    parent_id: str | None = None
    members: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    owner: str | None = None
    color: str | None = None
    icon: str | None = None
    parent_id: str | None = None
    members: list[str] | None = None
    metadata: dict[str, Any] | None = None
