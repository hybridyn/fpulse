"""SQLite-backed workspace + membership store.

Sits next to `fpulse/projects/store.py` so the layering is parallel.
The schema lives in `fpulse/storage/database.py` (TABLES const,
SCHEMA_VERSION=2).

Design notes:
  • All write methods go through `_save_workspace` so the JSON blob
    and the indexed columns stay in sync. Never UPDATE one without
    the other.
  • The store does NOT enforce RBAC — that's the API layer's job.
    The store will happily delete a workspace if you ask it to;
    the API gates the call behind `require_admin`.
  • Reads are tolerant: a malformed JSON blob does NOT crash the
    listing call, it just gets logged and skipped. This protects
    against partial writes from a previous crashed process.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone

from .models import (
    Workspace,
    WorkspaceMember,
    PLAN_FREE,
    ROLE_DEVELOPER,
    ROLE_SUPER_ADMIN,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(name: str) -> str:
    """Derive a url-safe slug from a workspace name. Best-effort: an
    admin can override the result via the `slug` field.
    """
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:63] or "workspace"


class WorkspaceStore:
    """SQLite-backed CRUD for workspaces and memberships."""

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db) -> None:
        """Late-bind a database connection. Called from main.py once the
        database is initialised. Mirrors how ProjectStore handles it.
        """
        self._db = db

    # ── Workspace CRUD ──────────────────────────────────────────────────

    def _save_workspace(self, ws: Workspace) -> Workspace:
        """Persist a Workspace to both the JSON blob and the indexed columns."""
        # Auto-derive a slug if the caller didn't supply one. We do it
        # here (not in the model) so passing the model around in tests
        # without going through the store doesn't trip the validator.
        if not ws.slug:
            base = _slugify(ws.name)
            slug = base
            n = 1
            while self._slug_exists(slug, exclude_id=ws.id):
                n += 1
                slug = f"{base}-{n}"
            ws.slug = slug

        data = ws.model_dump(mode="json")
        self._db.execute(
            """INSERT INTO workspaces
               (id, name, slug, plan, is_personal, owner_id, domain_allowlist, settings, data, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name = excluded.name,
                   slug = excluded.slug,
                   plan = excluded.plan,
                   is_personal = excluded.is_personal,
                   owner_id = excluded.owner_id,
                   domain_allowlist = excluded.domain_allowlist,
                   settings = excluded.settings,
                   data = excluded.data,
                   updated_at = excluded.updated_at""",
            (
                ws.id,
                ws.name,
                ws.slug,
                ws.plan,
                1 if ws.is_personal else 0,
                ws.owner_id or "",
                json.dumps(ws.domain_allowlist or []),
                json.dumps(ws.settings or {}),
                json.dumps(data),
                ws.created_at.isoformat() if hasattr(ws.created_at, "isoformat") else str(ws.created_at),
                ws.updated_at.isoformat() if hasattr(ws.updated_at, "isoformat") else str(ws.updated_at),
            ),
        )
        self._db.conn.commit()
        return ws

    def _slug_exists(self, slug: str, *, exclude_id: str | None = None) -> bool:
        if exclude_id:
            row = self._db.fetchone(
                "SELECT id FROM workspaces WHERE slug = ? AND id != ?",
                (slug, exclude_id),
            )
        else:
            row = self._db.fetchone(
                "SELECT id FROM workspaces WHERE slug = ?",
                (slug,),
            )
        return row is not None

    def create(self, ws: Workspace) -> Workspace:
        return self._save_workspace(ws)

    def get(self, workspace_id: str) -> Workspace | None:
        row = self._db.fetchone(
            "SELECT data FROM workspaces WHERE id = ?",
            (workspace_id,),
        )
        if not row:
            return None
        try:
            return Workspace(**json.loads(row["data"]))
        except Exception as exc:
            logger.warning("F-Pulse: malformed workspace row %s: %s", workspace_id, exc)
            return None

    def get_by_slug(self, slug: str) -> Workspace | None:
        row = self._db.fetchone(
            "SELECT data FROM workspaces WHERE slug = ?",
            (slug,),
        )
        if not row:
            return None
        try:
            return Workspace(**json.loads(row["data"]))
        except Exception:
            return None

    def list_all(self) -> list[Workspace]:
        """Every workspace on this install. Admin-tier callers only —
        the API layer enforces this; the store doesn't.
        """
        rows = self._db.fetchall("SELECT data FROM workspaces ORDER BY name ASC")
        out: list[Workspace] = []
        for r in rows:
            try:
                out.append(Workspace(**json.loads(r["data"])))
            except Exception as exc:
                logger.warning("F-Pulse: malformed workspace row skipped: %s", exc)
        return out

    def list_for_user(self, user_id: str) -> list[Workspace]:
        """Workspaces this user is a member of (accepted memberships only).

        Powers the workspace switcher dropdown — a user only ever sees
        the workspaces they actually belong to.
        """
        rows = self._db.fetchall(
            """SELECT w.data FROM workspaces w
               JOIN workspace_members m ON m.workspace_id = w.id
               WHERE m.user_id = ? AND m.accepted_at IS NOT NULL
               ORDER BY w.name ASC""",
            (user_id,),
        )
        out: list[Workspace] = []
        for r in rows:
            try:
                out.append(Workspace(**json.loads(r["data"])))
            except Exception:
                continue
        return out

    def update(self, workspace_id: str, updates: dict) -> Workspace | None:
        ws = self.get(workspace_id)
        if not ws:
            return None
        for k, v in updates.items():
            if v is not None and hasattr(ws, k):
                setattr(ws, k, v)
        ws.updated_at = datetime.now(timezone.utc)
        return self._save_workspace(ws)

    def delete(self, workspace_id: str) -> bool:
        """Delete a workspace AND all its memberships.

        Refuses to delete the Default workspace — that's the back-fill
        target for legacy data, and dropping it would orphan every
        existing project on a v1→v2 upgraded install.
        """
        if workspace_id == "default":
            return False
        cur = self._db.execute(
            "DELETE FROM workspaces WHERE id = ?", (workspace_id,)
        )
        # Memberships are removed via FK ON DELETE CASCADE.
        self._db.conn.commit()
        return cur.rowcount > 0

    # ── Membership CRUD ─────────────────────────────────────────────────

    def add_member(
        self,
        workspace_id: str,
        user_id: str,
        role: str = ROLE_DEVELOPER,
        invited_by: str = "",
        auto_accept: bool = True,
    ) -> WorkspaceMember:
        """Insert a membership row. `auto_accept=True` (default) means the
        member is immediately active; passing False creates a pending
        invite that the user accepts later via /me/workspaces/accept.
        """
        now = _now_iso()
        accepted = now if auto_accept else None
        self._db.execute(
            """INSERT OR REPLACE INTO workspace_members
               (workspace_id, user_id, role, invited_by, invited_at, accepted_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (workspace_id, user_id, role, invited_by, now, accepted),
        )
        self._db.conn.commit()
        return WorkspaceMember(
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
            invited_by=invited_by,
            invited_at=datetime.fromisoformat(now),
            accepted_at=datetime.fromisoformat(accepted) if accepted else None,
        )

    def remove_member(self, workspace_id: str, user_id: str) -> bool:
        """Remove a user from a workspace. Returns False if they weren't
        a member to begin with (idempotent on the API side).
        """
        cur = self._db.execute(
            "DELETE FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, user_id),
        )
        self._db.conn.commit()
        return cur.rowcount > 0

    def update_member_role(
        self, workspace_id: str, user_id: str, role: str
    ) -> bool:
        cur = self._db.execute(
            "UPDATE workspace_members SET role = ? WHERE workspace_id = ? AND user_id = ?",
            (role, workspace_id, user_id),
        )
        self._db.conn.commit()
        return cur.rowcount > 0

    def get_member(
        self, workspace_id: str, user_id: str
    ) -> WorkspaceMember | None:
        row = self._db.fetchone(
            """SELECT workspace_id, user_id, role, invited_by, invited_at, accepted_at
               FROM workspace_members
               WHERE workspace_id = ? AND user_id = ?""",
            (workspace_id, user_id),
        )
        if not row:
            return None
        return WorkspaceMember(
            workspace_id=row["workspace_id"],
            user_id=row["user_id"],
            role=row["role"],
            invited_by=row["invited_by"] or "",
            invited_at=datetime.fromisoformat(row["invited_at"]) if row["invited_at"] else None,
            accepted_at=datetime.fromisoformat(row["accepted_at"]) if row["accepted_at"] else None,
        )

    def list_members(self, workspace_id: str) -> list[WorkspaceMember]:
        """All memberships in a workspace, including pending invites.

        The API layer is expected to JOIN this with the user table to
        produce a display row (email, name, role) — keeping the join
        out of the store means tests don't need to fixture user rows.
        """
        rows = self._db.fetchall(
            """SELECT workspace_id, user_id, role, invited_by, invited_at, accepted_at
               FROM workspace_members
               WHERE workspace_id = ?
               ORDER BY accepted_at IS NULL DESC, role DESC""",
            (workspace_id,),
        )
        out: list[WorkspaceMember] = []
        for r in rows:
            try:
                out.append(WorkspaceMember(
                    workspace_id=r["workspace_id"],
                    user_id=r["user_id"],
                    role=r["role"],
                    invited_by=r["invited_by"] or "",
                    invited_at=datetime.fromisoformat(r["invited_at"]) if r["invited_at"] else None,
                    accepted_at=datetime.fromisoformat(r["accepted_at"]) if r["accepted_at"] else None,
                ))
            except Exception:
                continue
        return out

    def is_member(self, workspace_id: str, user_id: str) -> bool:
        """Quick gate used by the API middleware: does this user have
        ANY membership row (accepted or pending) in this workspace?
        """
        row = self._db.fetchone(
            "SELECT 1 FROM workspace_members WHERE workspace_id = ? AND user_id = ? AND accepted_at IS NOT NULL",
            (workspace_id, user_id),
        )
        return row is not None

    def role_for(self, workspace_id: str, user_id: str) -> str | None:
        """Per-workspace role for a user, or None if not a member.

        Returns None for a *pending* invite, not the role — the user
        can't act in the workspace until they accept.
        """
        row = self._db.fetchone(
            """SELECT role FROM workspace_members
               WHERE workspace_id = ? AND user_id = ? AND accepted_at IS NOT NULL""",
            (workspace_id, user_id),
        )
        if not row:
            return None
        return row["role"]

    # ── Personal-workspace helpers ──────────────────────────────────────

    def ensure_personal_workspace(self, user_id: str, user_email: str, user_name: str) -> Workspace:
        """Create the auto "Personal" workspace for a user if they don't
        already have one. Called from the register endpoint so every
        new self-signed-up user immediately has a place to put their
        projects without an admin needing to act.

        The personal workspace is named after the user's display name
        (or email local-part as a fallback) and marked is_personal=True
        so the corporate workspaces list can filter it out.
        """
        # If the user already owns a personal workspace, return it.
        rows = self._db.fetchall(
            "SELECT data FROM workspaces WHERE owner_id = ? AND is_personal = 1",
            (user_id,),
        )
        if rows:
            try:
                return Workspace(**json.loads(rows[0]["data"]))
            except Exception:
                pass

        display = (user_name or user_email.split("@", 1)[0]).strip()
        ws = Workspace(
            name=f"{display}'s Personal",
            slug="",  # auto-derived
            plan=PLAN_FREE,
            is_personal=True,
            owner_id=user_id,
        )
        ws = self._save_workspace(ws)
        # Owner is automatically a super_admin of their personal workspace
        # so they have full control over their own stuff.
        self.add_member(
            workspace_id=ws.id,
            user_id=user_id,
            role=ROLE_SUPER_ADMIN,
            invited_by="system",
            auto_accept=True,
        )
        return ws
