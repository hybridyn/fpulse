"""Shared FastAPI auth dependencies.

Why a separate module?
  Several routers (auth, backup, credentials, plus, ...) need the same role
  gates. Putting them in one place keeps the rules consistent and gives us
  a single audit hook for denied attempts.

Design notes
  - Endpoints that already work without auth in OSS local mode keep working:
    if no `Authorization` header is present we treat the caller as anonymous
    and only the *write* helpers fail. Read helpers degrade gracefully.
  - Every denied call is audit-logged with action="rbac_denied" so admins
    can spot probing attempts in the audit trail.
  - 4-role hierarchy (highest to lowest):
        super_admin  >  admin  >  developer  >  viewer
    Legacy roles "lead" and "member" are mapped to admin and developer
    respectively so existing data keeps working.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

# Role hierarchy — used by the role-rank check below.
# Legacy roles mapped: lead → admin (90), member → developer (50).
#
# 2026-05-30 (Track S P1): added workspace_admin / data_engineer /
# analyst so the test-fixture roles route correctly through
# require_min_rank. The test-suite anonymous_access_blocked.py covers
# data_engineer authoring workflows and analyst/viewer being blocked
# from mutations; without these entries data_engineer was treated as
# rank 0 and got rejected by writes that should allow it.
_ROLE_RANK = {
    "super_admin":      100,
    "admin":             90,
    "workspace_admin":   90,   # peer of admin, scoped to a workspace
    "lead":              90,   # legacy → admin
    "data_engineer":     70,   # full write authority on pipelines/creds
    "developer":         50,
    "member":            50,   # legacy → developer
    "analyst":           30,   # reads + some metadata; no cred/pipeline writes
    "viewer":            10,
}

ADMIN_ROLES = ("super_admin", "admin", "workspace_admin")


def _user_store():
    from fpulse.main import app_state
    return app_state["user_store"]


def _audit_denied(user, request: Request, required: str) -> None:
    """Best-effort audit log for a denied attempt. Never raises."""
    try:
        from fpulse.main import app_state
        audit = app_state.get("audit_logger")
        if audit:
            audit.log(
                user_id=getattr(user, "id", "anonymous") if user else "anonymous",
                user_email=getattr(user, "email", "anonymous") if user else "anonymous",
                action="rbac_denied",
                resource_type="api",
                resource_id=request.url.path,
                details={
                    "role": getattr(user, "role", None) if user else None,
                    "required": required,
                    "method": request.method,
                },
            )
    except Exception:
        pass


_SESSION_COOKIE = "fpulse_session"
_CSRF_COOKIE = "fpulse_csrf"


def _extract_token(request: Request) -> str:
    """Session token from ``Authorization: Bearer`` (CLI / service /
    programmatic / legacy browser) OR the HttpOnly ``fpulse_session`` cookie
    (the BFF browser flow). Dual-auth by design: both are accepted so moving
    the browser onto cookies never breaks non-browser clients."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        tok = auth[7:].strip()
        if tok:
            return tok
    try:
        return (request.cookies.get(_SESSION_COOKIE) or "").strip()
    except Exception:
        return ""


def csrf_protect(request: Request) -> None:
    """CSRF guard (double-submit cookie) for COOKIE-authenticated browser
    writes. Bearer-authenticated requests (CLI / service / programmatic) are
    exempt — they can't be driven cross-site. Only enforced for state-changing
    methods when the session actually came from the cookie, so it's a no-op
    for today's bearer-token frontend."""
    import hmac
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return
    if request.headers.get("Authorization", "").startswith("Bearer "):
        return
    if not request.cookies.get(_SESSION_COOKIE):
        return  # no cookie session → nothing to protect
    sent = request.headers.get("X-CSRF-Token", "")
    expected = request.cookies.get(_CSRF_COOKIE, "")
    if not (sent and expected and hmac.compare_digest(sent, expected)):
        raise HTTPException(403, "CSRF token missing or invalid")


def current_user_optional(request: Request):
    """Return the current user or None — never raises.

    Use for read endpoints that should work in anonymous local-dev mode but
    still want to attribute calls when a token is present.
    """
    token = _extract_token(request)
    if not token:
        return None
    try:
        return _user_store().get_user_for_session(token)
    except Exception:
        return None


def require_auth(request: Request):
    """Any valid logged-in user. Raises 401 otherwise."""
    token = _extract_token(request)
    if not token:
        _audit_denied(None, request, "any-authenticated")
        raise HTTPException(401, "Authentication required")
    user = _user_store().get_user_for_session(token)
    if not user:
        _audit_denied(None, request, "any-authenticated")
        raise HTTPException(401, "Invalid or expired session")
    if not getattr(user, "is_active", True):
        _audit_denied(user, request, "any-authenticated")
        raise HTTPException(403, "Account is deactivated")
    return user


def require_role(*roles: str):
    """Build a dependency that requires one of the given roles."""
    allowed = set(roles)

    def _dep(request: Request):
        user = require_auth(request)
        if user.role not in allowed:
            _audit_denied(user, request, "|".join(sorted(allowed)))
            raise HTTPException(403, f"Role required: {', '.join(sorted(allowed))}")
        return user

    return _dep


def require_min_rank(min_role: str):
    """Build a dependency that requires at least the given role rank."""
    threshold = _ROLE_RANK.get(min_role, 0)

    def _dep(request: Request):
        user = require_auth(request)
        if _ROLE_RANK.get(user.role, 0) < threshold:
            _audit_denied(user, request, f">={min_role}")
            raise HTTPException(403, f"Requires role >= {min_role}")
        return user

    return _dep


# Convenience pre-built dependencies — import these into routers.
require_admin = require_role("super_admin", "admin")
# Legacy alias — lead mapped to admin rank, so require_lead ≡ require_admin.
require_lead = require_min_rank("admin")


# ── Workspace context ───────────────────────────────────────────────────────
#
# Stage 1 of the Workspace foundation: every scoped read/write resolves a
# *current workspace* from the request and routers filter their queries by
# it. There is no UI switcher yet — the frontend will start sending the
# `X-Workspace-Id` header from a value cached in localStorage, defaulting
# to `default` until a switcher is built.
#
# Resolution order (first hit wins):
#   1. `X-Workspace-Id` header — explicit override; we still verify the
#      caller is a member of that workspace (instance admins bypass).
#      A non-member trying to address someone else's workspace gets 403,
#      not silently downgraded — that would corrupt their next write.
#   2. The user's first accepted workspace membership (deterministic by
#      created_at). For self-signed-up users this is normally their
#      Personal workspace; for legacy users it's "default".
#   3. The literal string "default" — always exists thanks to the v2
#      migration's back-fill, so we never return None.
#
# Why a dep instead of just reading the header inline:
#   - One place to enforce the membership check, so a future router that
#     forgets to scope its query still gets the auth gate.
#   - One place to add per-workspace audit fields later (workspace_id on
#     every audit row, not just user_id).


def current_workspace_id(request: Request) -> str:
    """Resolve the workspace this request is acting on.

    Always returns a workspace id — never None — so callers can use the
    return value directly as a SQL filter without a None-guard.

    In OSS / free-tier local mode (no login), gracefully falls back to
    ``"default"`` so every page works without authentication.  When the
    user *is* logged in, the full membership check applies: 403 if they
    explicitly addressed a workspace they don't belong to.
    """
    # Try to get the authenticated user — but don't fail if none.
    # Free-tier / local-dev callers have no token, and that's fine:
    # they get the "default" workspace.
    user = current_user_optional(request)

    # SECURITY_MODE=server (self-hosted / LAN-exposed): no anonymous
    # workspace fallback. An unauthenticated caller must not silently land
    # in the 'default' workspace — require a real login. Local mode keeps
    # the single-user laptop convenience (falls through to the anonymous
    # branch below). Checked before the ws_store/degradation paths so the
    # gate holds regardless of install shape.
    if user is None:
        from fpulse import runtime_config
        if runtime_config.IS_SERVER_MODE:
            raise HTTPException(
                401,
                "Authentication required. This F-Pulse server runs with "
                "FPULSE_SECURITY_MODE=server; sign in to continue.",
            )

    explicit = request.headers.get("X-Workspace-Id", "").strip()

    from fpulse.main import app_state
    ws_store = app_state.get("workspace_store")

    # No workspace store on the install (legacy / test fixture without
    # the v2 migration applied) → degrade to "default" so the request
    # still works. Production installs always have one wired up in
    # main.py at boot.
    if not ws_store:
        return explicit or "default"

    # Anonymous caller (free tier, no login) — use explicit header or
    # default. No membership check needed for anonymous users.
    if user is None:
        return explicit or "default"

    if explicit:
        # Instance admins can address any workspace — they bypass the
        # per-workspace membership check the same way they bypass the
        # project ACL.
        if user.role in ADMIN_ROLES:
            return explicit
        if ws_store.is_member(explicit, user.id):
            return explicit
        _audit_denied(user, request, f"workspace_member:{explicit}")
        raise HTTPException(
            403,
            f"You are not a member of workspace {explicit!r}. "
            "Switch workspaces or ask an admin to invite you.",
        )

    # No explicit header — pick the user's first workspace. For
    # self-signed-up users this is normally Personal; for the seeded
    # admin / legacy users it's Default.
    try:
        memberships = ws_store.list_for_user(user.id)
        if memberships:
            return memberships[0].id
    except Exception:
        # Store error — fall through to the safe default below.
        pass
    return "default"
