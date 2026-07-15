"""Workspace + Membership pydantic models.

These mirror the SQLite tables defined in `fpulse/storage/database.py`
(see SCHEMA_VERSION=2 migration). The `data` JSON column on each table
stores the full Pydantic dump; indexed columns exist alongside it for
fast filter queries.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


# Workspace plan tags. Free workspaces are scoped by membership only;
# Plus workspaces additionally enforce per-environment role gates and
# license-bound seat limits. Both can coexist on the same install.
PLAN_FREE = "free"
PLAN_PLUS = "plus"
ALLOWED_PLANS = {PLAN_FREE, PLAN_PLUS}


# Per-workspace role hierarchy. Mirrors the instance-level RBAC roles
# in `fpulse/auth/models.py` so users see one consistent vocabulary
# across the product. The "instance role" on the User record only
# governs cross-workspace operations (creating new workspaces, etc);
# everything else is per-workspace via WorkspaceMember.role.
ROLE_VIEWER = "viewer"
ROLE_DEVELOPER = "developer"
ROLE_ADMIN = "admin"
ROLE_SUPER_ADMIN = "super_admin"
# Legacy constant kept for backward-compat imports; maps to ROLE_ADMIN.
ROLE_LEAD = ROLE_ADMIN
WORKSPACE_ROLES = (ROLE_VIEWER, ROLE_DEVELOPER, ROLE_ADMIN, ROLE_SUPER_ADMIN)


# Slug rule — url-safe lowercase, dashes only. Used in workspace
# switcher URLs and to namespace future per-workspace export files
# (e.g. `acme-corp.fpulsepkg`). Enforced at create/update time.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}$")


class Workspace(BaseModel):
    """A tenant boundary inside an F-Pulse install.

    Every project, pipeline, schedule, alert, connection, variable, and
    credential belongs to exactly one Workspace. Users can be members
    of multiple workspaces (with different roles in each) and switch
    between them via the top-nav switcher.

    Field-by-field corporate-policy notes:
      • `id` — opaque hex id, not derived from name; safe to expose.
      • `slug` — short url-safe alias; admins can rename without
        breaking persistent links by keeping the slug stable.
      • `plan` — `free` or `plus`. Plus is what unlocks the production
        environment, advanced RBAC, audit retention, etc. Free
        workspaces still get scoping and membership.
      • `is_personal` — true for the auto-created "Personal" workspace
        that every brand-new self-signed-up user gets. Used to filter
        these out of corporate workspace lists and to enable the
        "promote my personal stuff to a corporate workspace" flow.
      • `domain_allowlist` — list of email domains permitted to join
        via the request-access form. Empty = no domain restriction.
        Domains stored lowercased, no leading `@`. Match is suffix-
        based, so `corp.com` matches `alice@us.corp.com`.
      • `settings` — per-workspace overrides. The `audit_enabled`
        and `require_sso` keys live here so a corporate workspace
        can be hardened independently of the rest of the install.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    slug: str = ""
    plan: str = PLAN_FREE
    is_personal: bool = False
    owner_id: str = ""
    domain_allowlist: list[str] = Field(default_factory=list)
    settings: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("plan")
    @classmethod
    def _validate_plan(cls, v: str) -> str:
        if v not in ALLOWED_PLANS:
            raise ValueError(f"plan must be one of {sorted(ALLOWED_PLANS)}")
        return v

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        # Empty slug is allowed and gets auto-derived from the name on
        # save; we don't fail validation on empty so the create form
        # doesn't have to compute the slug client-side.
        if v == "":
            return v
        if not _SLUG_RE.match(v):
            raise ValueError(
                "slug must be lowercase letters/digits/dashes, 1-63 chars, "
                "start with a letter or digit"
            )
        return v

    @field_validator("domain_allowlist")
    @classmethod
    def _validate_domains(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for d in v or []:
            d2 = (d or "").strip().lower().lstrip("@")
            if not d2:
                continue
            # Very loose domain check — we trust the admin not to enter
            # garbage but reject obvious typos like "@company com" with
            # spaces or no dot at all. Real domains always contain a dot.
            if " " in d2 or "." not in d2:
                raise ValueError(f"invalid domain: {d!r}")
            cleaned.append(d2)
        return cleaned


class WorkspaceMember(BaseModel):
    """Join row between a User and a Workspace, plus a per-workspace role.

    Pending invites have `accepted_at=None`. The /workspaces/{id}/members
    endpoint returns both pending and accepted rows so the admin can see
    who's still in the queue.
    """

    workspace_id: str
    user_id: str
    role: str = ROLE_DEVELOPER
    invited_by: str = ""
    invited_at: datetime | None = None
    accepted_at: datetime | None = None

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        if v not in WORKSPACE_ROLES:
            raise ValueError(f"role must be one of {WORKSPACE_ROLES}")
        return v


# ── Request bodies ────────────────────────────────────────────────────────


class WorkspaceCreate(BaseModel):
    """Body for POST /api/workspaces.

    `owner_id` is intentionally NOT here — the API stamps it from the
    authenticated caller, just like projects. `plan` defaults to free
    because activating Plus requires a separate license activation
    step that runs against the existing Plus license manager.
    """
    name: str
    slug: str = ""
    domain_allowlist: list[str] = Field(default_factory=list)


class WorkspaceUpdate(BaseModel):
    """Body for PUT /api/workspaces/{id}. All fields optional."""
    name: str | None = None
    slug: str | None = None
    domain_allowlist: list[str] | None = None
    settings: dict | None = None


class MemberInvite(BaseModel):
    """Body for POST /api/workspaces/{id}/members.

    Either `user_id` (existing user already on this install) or `email`
    (anyone — looked up by email, must already exist). We don't accept
    "create user + invite" in one shot because account creation is its
    own audited action; the admin invites an existing account.
    """
    user_id: str = ""
    email: str = ""
    role: str = ROLE_DEVELOPER


class MemberRoleUpdate(BaseModel):
    """Body for PUT /api/workspaces/{id}/members/{user_id}."""
    role: str
