"""SQLite-backed user and session store."""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from .models import User, Session

logger = logging.getLogger(__name__)


def _session_ttl_days() -> float:
    """Session lifetime in days. 0 disables expiry.

    Default 7 — long enough that a daily user never re-authenticates
    mid-week, short enough that a forgotten or stolen token dies on its
    own instead of staying valid forever.
    """
    raw = os.environ.get("FPULSE_SESSION_TTL_DAYS", "7").strip()
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return 7.0


class UserStore:
    """User + session store backed by SQLite."""

    def __init__(self, db=None):
        self._db = db
        if db:
            self._ensure_admin()

    def set_db(self, db):
        self._db = db
        self._ensure_admin()

    def _ensure_admin(self):
        """Create the default admin account if it doesn't exist, and
        self-heal its role if a previous build seeded it with a non-admin
        role that prevents the operator from managing their own install.

        F-Pulse OSS is a single-user developer tool: the bootstrap account
        is the operator running their own install. They need to be able
        to create projects, manage connections, and edit settings — all of
        which require ``admin`` in the frontend permission matrix. Earlier
        builds seeded role=``developer`` which silently disabled "Create
        Project" and similar affordances on the user's own machine.

        Bootstrap password handling: we generate a random 24-char password
        on first boot and write it ONCE to ``INITIAL_ADMIN_PASSWORD.txt``
        next to the SQLite db, plus log it to stdout. The operator is
        expected to read it, sign in, change it, and delete the file. We
        never read the file back — losing it after rotation is the desired
        behaviour.

        The 5-tier workspace RBAC in F-Pulse+ is enforced server-side and
        is unaffected by this role; Plus deployments use named workspace
        members with their own role assignments.
        """
        row = self._db.fetchone("SELECT id FROM users WHERE id = ?", ("admin",))
        if not row:
            initial_password = secrets.token_urlsafe(18)  # ~24 chars
            admin = User(
                id="admin",
                email="admin@fpulse.local",
                name="Admin",
                password_hash=User.hash_password(initial_password),
                role="admin",
            )
            self._save_user(admin)
            self._write_initial_password(initial_password)
            return

        # Heal an existing seeded user whose role drifted to a non-admin
        # value (prior builds seeded 'developer' which disables Create
        # Project on the OSS UI). super_admin is left alone — that's a
        # deliberate elevation. environments allow-list also gets healed
        # so PROD toggles are visible.
        existing = self.get_user("admin")
        if existing:
            dirty = False
            if existing.role not in ("admin", "super_admin"):
                existing.role = "admin"
                dirty = True
            envs = list(existing.environments or [])
            if envs and "prod" not in envs:
                envs.append("prod")
                existing.environments = envs
                dirty = True
            if dirty:
                self._save_user(existing)

    def _write_initial_password(self, password: str) -> None:
        """Persist the bootstrap password to a file next to the db so a fresh
        operator can find it on first boot. Also log it loudly so a one-shot
        container start (where the file may be ephemeral) still surfaces it.

        We deliberately do NOT raise on failure — if the data dir is somehow
        not writable, we still want the admin user to exist and the password
        to be in the logs. Losing the file after the first read is the
        intended end state.
        """
        try:
            db_path = getattr(self._db, "db_path", None)
            data_dir = os.path.dirname(db_path) if db_path else os.getcwd()
            target = os.path.join(data_dir, "INITIAL_ADMIN_PASSWORD.txt")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(
                    "F-Pulse bootstrap developer credentials\n"
                    "=======================================\n"
                    f"  email:    admin@fpulse.local\n"
                    f"  password: {password}\n\n"
                    "Sign in, change this password from the Account page,\n"
                    "then DELETE this file. F-Pulse never reads it back.\n"
                )
            logger.warning(
                "F-Pulse: created bootstrap admin admin@fpulse.local. "
                "Initial password written to %s. Sign in, rotate it, delete the file.",
                target,
            )
        except OSError as exc:
            # File-system was read-only or similar — fall back to log-only.
            logger.warning(
                "F-Pulse: created bootstrap admin admin@fpulse.local. "
                "Could not write password file (%s). One-time password: %s "
                "— copy this NOW from the logs, it will not be shown again.",
                exc, password,
            )

    def _save_user(self, user: User):
        data = user.model_dump(mode="json")
        self._db.insert_json(
            "users", user.id, data,
            email=user.email,
            created_at=user.created_at.isoformat(),
        )

    def create_user(self, user: User) -> User:
        self._save_user(user)
        return user

    def get_user(self, user_id: str) -> User | None:
        data = self._db.get_json("users", user_id)
        if data is None:
            return None
        return User(**data)

    def get_user_by_email(self, email: str) -> User | None:
        row = self._db.fetchone("SELECT data FROM users WHERE email = ?", (email,))
        if row is None:
            return None
        data = json.loads(row["data"])
        return User(**data)

    def list_users(self) -> list[dict]:
        items = self._db.list_json("users")
        return [
            {
                "id": u.get("id"),
                "email": u.get("email"),
                "name": u.get("name"),
                "role": u.get("role"),
                "projects": u.get("projects", []),
                "environments": u.get("environments", []),
                "is_active": u.get("is_active", True),
                "last_login_at": u.get("last_login_at"),
                "created_at": u.get("created_at"),
            }
            for u in items
        ]

    def update_user(self, user_id: str, updates: dict) -> User | None:
        user = self.get_user(user_id)
        if not user:
            return None
        for key, value in updates.items():
            # password_hash is deliberately excluded from the generic
            # update path — callers must use set_password() so the
            # intent is explicit and auditable. Caught a real
            # security bug on 2026-05-27: three password-change
            # endpoints were passing password_hash to update_user
            # and the exclusion was silently dropping it, making
            # reset / change / admin-reset appear to succeed while
            # the password never actually changed.
            if value is not None and hasattr(user, key) and key != 'password_hash':
                setattr(user, key, value)
        self._save_user(user)
        return user

    def set_password(self, user_id: str, new_password_hash: str) -> User | None:
        """Set a user's password hash. Dedicated path (not via
        update_user) so the intent is explicit at every call site —
        the generic update path silently drops password_hash to
        prevent accidental privilege escalation through arbitrary
        update bodies.

        Callers must pass an ALREADY-HASHED password (via
        User.hash_password). Plaintext is never persisted. Returns
        the updated User on success, None if the user_id is unknown.
        """
        user = self.get_user(user_id)
        if not user:
            return None
        user.password_hash = new_password_hash
        self._save_user(user)
        return user

    def delete_user(self, user_id: str) -> bool:
        if user_id == "admin":
            return False
        return self._db.delete_row("users", user_id)

    # ── Sessions ──

    def create_session(
        self,
        user_id: str,
        machine_id: str = "",
        ip_address: str = "",
        session_mode: str = "unlimited",
        max_sessions: int = 1,
    ) -> Session:
        """Create a session with configurable concurrency control.

        Session modes (admin-configurable):
          - unlimited: no session limits (default for Free tier / dev)
          - single: 1 active session per user — new login kills old session
          - capped: max N concurrent sessions per user

        Why this matters:
          On a server, credential sharing is the #1 revenue leakage vector.
          If User A gives password to User B and both login simultaneously,
          'single' mode kicks User A out — creating friction that stops sharing.
        """
        # ── Enforce session mode ──
        if session_mode == "single":
            # Kill ALL existing sessions for this user
            self._db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            self._db.commit()
        elif session_mode == "capped" and max_sessions > 0:
            # Count existing sessions, remove oldest if over limit
            rows = self._db.fetchall(
                "SELECT token, created_at FROM sessions WHERE user_id = ? ORDER BY created_at ASC",
                (user_id,),
            )
            # Need to remove enough to make room for the new one
            excess = len(rows) - max_sessions + 1
            if excess > 0:
                for row in rows[:excess]:
                    self._db.execute("DELETE FROM sessions WHERE token = ?", (row["token"],))
                self._db.commit()

        ttl_days = _session_ttl_days()
        session = Session(
            user_id=user_id,
            machine_id=machine_id,
            ip_address=ip_address,
            expires_at=(
                datetime.now(timezone.utc) + timedelta(days=ttl_days)
                if ttl_days > 0 else None
            ),
        )
        data = session.model_dump(mode="json")
        self._db.execute(
            "INSERT OR REPLACE INTO sessions (token, user_id, data, created_at) VALUES (?, ?, ?, ?)",
            (session.token, user_id, json.dumps(data, default=str), session.created_at.isoformat()),
        )
        self._db.commit()
        # Update last login
        user = self.get_user(user_id)
        if user:
            user.last_login_at = datetime.now(timezone.utc)
            user.last_login_ip = ip_address
            user.last_login_machine = machine_id
            self._save_user(user)
        return session

    def count_active_sessions(self, user_id: str) -> int:
        """Count active sessions for a user."""
        row = self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM sessions WHERE user_id = ?", (user_id,),
        )
        return row["cnt"] if row else 0

    def get_active_sessions(self, user_id: str) -> list[dict]:
        """Get all active sessions for a user (for admin visibility)."""
        rows = self._db.fetchall(
            "SELECT data FROM sessions WHERE user_id = ?", (user_id,),
        )
        return [json.loads(r["data"]) for r in rows]

    def revoke_all_sessions(self, user_id: str) -> int:
        """Revoke all sessions for a user (admin force-logout)."""
        cursor = self._db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        self._db.commit()
        return cursor.rowcount

    def revoke_other_sessions(self, user_id: str, keep_token: str = "") -> int:
        """Revoke every session for a user except ``keep_token``.

        Called after a credential rotation so any stolen token dies with
        the old password, while the session that performed the change
        stays logged in. Pass "" to revoke everything.
        """
        if not keep_token:
            return self.revoke_all_sessions(user_id)
        cursor = self._db.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token != ?",
            (user_id, keep_token),
        )
        self._db.commit()
        return cursor.rowcount

    def get_session(self, token: str) -> Session | None:
        row = self._db.fetchone("SELECT data FROM sessions WHERE token = ?", (token,))
        if row is None:
            return None
        data = json.loads(row["data"])
        return Session(**data)

    def delete_session(self, token: str) -> bool:
        cursor = self._db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        self._db.commit()
        return cursor.rowcount > 0

    def get_user_for_session(self, token: str) -> User | None:
        session = self.get_session(token)
        if not session:
            return None
        if self._session_expired(session):
            # Delete eagerly so the table doesn't accumulate dead rows
            # and /me/sessions doesn't list tokens that can't be used.
            self.delete_session(token)
            return None
        return self.get_user(session.user_id)

    @staticmethod
    def _session_expired(session: Session) -> bool:
        ttl_days = _session_ttl_days()
        expires_at = session.expires_at
        if expires_at is None:
            # Legacy session created before TTLs existed — derive the
            # deadline from created_at so pre-upgrade tokens don't stay
            # valid forever.
            if ttl_days <= 0:
                return False
            created = session.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            expires_at = created + timedelta(days=ttl_days)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= expires_at
