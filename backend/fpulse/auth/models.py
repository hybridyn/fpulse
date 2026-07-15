"""Authentication models for VM deployment mode.

2026-06-03 — H1 fix: password hashing migrated from single-round salted
SHA-256 to bcrypt (cost factor 12). The previous scheme prevented
rainbow-table attacks via the salt but left each guess at ~10 billion/sec
on a modern GPU, so a leaked auth-store file was crackable in
attacker-bounded time. bcrypt at cost 12 = ~250 ms per hash = ~4 guesses
per second per GPU core = years for a real password.

Backward compatibility: `verify_password` accepts BOTH formats so
existing installs don't force password resets:
  * `$2b$...` / `$2a$...`  → bcrypt verify (current)
  * `<hex>:<hex>`          → legacy salted-SHA256 verify
On a successful legacy verify the caller (api/auth.py login path)
calls `store.set_password(user.id, User.hash_password(password))` to
upgrade the hash in place. After every legacy user has logged in once,
the auth store contains only bcrypt hashes.

See `docs/security/audit-2026-06-03.md` finding H1 for the full
rationale and migration plan.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

import bcrypt
from pydantic import BaseModel, Field


# ── Password hashing helpers ────────────────────────────────────────
# Centralised here so unit tests and the legacy-rehash login path can
# reference them by name rather than reaching into User._.

# bcrypt cost factor. 12 = ~250 ms per hash on a 2024-era laptop CPU.
# Each unit increment doubles the work; OWASP currently recommends ≥ 12
# for interactive login. 14 starts to feel slow on cheap VPS hardware
# (~1s), which makes the login UX worse without buying meaningful
# brute-force resistance on top of the existing rate limit.
_BCRYPT_ROUNDS = 12


def _is_bcrypt_hash(stored: str) -> bool:
    """True if `stored` looks like a bcrypt hash. bcrypt hashes begin
    with `$2a$`, `$2b$`, or `$2y$` (variant prefixes for compatibility
    with old C libraries; Python's `bcrypt` produces `$2b$`). Used to
    route `verify_password` to the right branch."""
    return isinstance(stored, str) and stored.startswith(("$2a$", "$2b$", "$2y$"))


def _verify_legacy_sha256(password: str, stored: str) -> bool:
    """Verify a password against the pre-2026-06-03 `salt:sha256` hash
    format. Constant-time comparison via `hmac.compare_digest` so the
    legacy branch doesn't reintroduce a timing oracle the bcrypt path
    doesn't have."""
    if ":" not in stored:
        return False
    salt, stored_hash = stored.split(":", 1)
    check = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return hmac.compare_digest(check, stored_hash)


class User(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    email: str
    name: str = ""
    password_hash: str = ""
    role: str = "developer"  # super_admin | admin | developer | viewer
    projects: list[str] = Field(default_factory=list)  # project IDs user can access
    # Empty list = no restriction; role permission matrix decides what env the
    # user can reach. An admin can sandbox a specific account to DEV by setting
    # this to ["dev"] explicitly via PUT /api/auth/users/{id}.
    environments: list[str] = Field(default_factory=list)  # dev | prod
    # Per-project PROD permissions — granular env access control.
    # Format: { "project_id": ["can_view_prod", "can_run_prod", "can_deploy_prod", "can_manage_prod_connections"] }
    # Empty = use role defaults. Admin/Super Admin bypass this entirely.
    prod_permissions: dict[str, list[str]] = Field(default_factory=dict)
    is_active: bool = True
    last_login_at: datetime | None = None
    last_login_ip: str | None = None
    last_login_machine: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a plaintext password with bcrypt at cost _BCRYPT_ROUNDS.
        Returns the bcrypt string `$2b$...` (encoded ASCII). Empty
        passwords are rejected (callers should validate length before
        calling)."""
        if not isinstance(password, str) or not password:
            raise ValueError("Cannot hash empty password")
        return bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt(rounds=_BCRYPT_ROUNDS),
        ).decode("ascii")

    def verify_password(self, password: str) -> bool:
        """Verify a plaintext password against the stored hash. Accepts
        BOTH the current bcrypt format AND the pre-2026-06-03 legacy
        `salt:sha256` format so existing users don't need a reset.
        Callers that want to know whether the user is still on a legacy
        hash (so they can rehash in place) check
        :meth:`needs_rehash` after a successful verify."""
        if not self.password_hash or not isinstance(password, str):
            return False
        if _is_bcrypt_hash(self.password_hash):
            try:
                return bcrypt.checkpw(
                    password.encode("utf-8"),
                    self.password_hash.encode("ascii"),
                )
            except (ValueError, TypeError):
                # Malformed bcrypt hash — refuse rather than fall
                # through to legacy. Legitimate bcrypt hashes never
                # fail this way; if we see it, the hash is corrupted.
                return False
        # Legacy format — verify with the old hash function.
        return _verify_legacy_sha256(password, self.password_hash)

    def needs_rehash(self) -> bool:
        """True when the stored hash is NOT the current bcrypt format.
        The login flow calls this after a successful `verify_password`
        and, when True, rehashes the password with the current
        algorithm + cost factor and persists the new hash via
        `store.set_password()`. End result: after every legacy user
        has logged in once, the auth store contains only bcrypt hashes."""
        return bool(self.password_hash) and not _is_bcrypt_hash(self.password_hash)


class Session(BaseModel):
    token: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    user_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    # Anti-sharing: machine fingerprint + IP tracking
    machine_id: str = ""  # hostname + user-agent hash
    ip_address: str = ""
    is_active: bool = True


# ── Role Permission Matrix ──
# Defines what each role can do across DEV and PROD environments
ROLE_HIERARCHY = ["viewer", "developer", "admin", "super_admin"]

# Valid PROD permissions (per-project, per-user)
PROD_PERMISSIONS = [
    "can_view_prod",                # View production pipelines, runs, logs
    "can_run_prod",                 # Execute pipelines in production
    "can_deploy_prod",              # Deploy pipeline versions to production
    "can_manage_prod_connections",  # Edit production connections/credentials
]

ROLE_PERMISSIONS = {
    "super_admin": {
        "dev": ["create", "edit", "delete", "execute", "schedule", "manage_users", "manage_projects", "manage_system"],
        "prod": ["deploy", "rollback", "approve", "execute", "view", "manage_users", "manage_system", "manage_license"],
    },
    "admin": {
        "dev": ["create", "edit", "delete", "execute", "schedule", "manage_users", "manage_projects"],
        "prod": ["deploy", "rollback", "approve", "execute", "view", "manage_users"],
    },
    "developer": {
        "dev": ["create", "edit", "execute"],
        "prod": [],  # No default PROD access — must be explicitly granted per project
    },
    "member": {  # Legacy compat — maps to developer
        "dev": ["create", "edit", "execute"],
        "prod": [],
    },
    "lead": {  # Legacy compat — maps to admin
        "dev": ["create", "edit", "delete", "execute", "schedule", "manage_users", "manage_projects"],
        "prod": ["deploy", "rollback", "approve", "execute", "view", "manage_users"],
    },
    "viewer": {
        "dev": ["view"],
        "prod": [],  # No default PROD access — must be explicitly granted per project
    },
}


def has_permission(role: str, environment: str, action: str) -> bool:
    """Check if a role has permission for an action in an environment."""
    perms = ROLE_PERMISSIONS.get(role, {})
    env_perms = perms.get(environment, [])
    return action in env_perms


def has_prod_permission(user, project_id: str, permission: str) -> bool:
    """Check if a user has a specific PROD permission on a project.

    Admin/Super Admin always have full PROD access.
    Developers and Viewers need explicit per-project grants.

    Permission values:
      can_view_prod, can_run_prod, can_deploy_prod, can_manage_prod_connections
    """
    role = getattr(user, "role", "viewer")

    # Admin+ always have full PROD access
    if role in ("super_admin", "admin"):
        return True

    # Check per-project PROD permissions
    prod_perms = getattr(user, "prod_permissions", {}) or {}
    project_perms = prod_perms.get(project_id, [])

    # Also check wildcard "*" for all-projects grant
    all_perms = prod_perms.get("*", [])

    return permission in project_perms or permission in all_perms


def get_role_level(role: str) -> int:
    """Get numeric level for role comparison."""
    # Map legacy roles to their equivalent
    if role == "member":
        role = "developer"
    if role == "lead":
        role = "admin"
    return ROLE_HIERARCHY.index(role) if role in ROLE_HIERARCHY else 0


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str = ""


class InviteRequest(BaseModel):
    email: str
    name: str = ""
    role: str = "developer"
    projects: list[str] = Field(default_factory=list)
    workspace_id: str = "default"  # workspace to add the invited user to
