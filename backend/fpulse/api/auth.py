"""Authentication API for VM deployment mode."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import traceback

from fastapi import APIRouter, Depends, HTTPException, Request, Response

logger = logging.getLogger(__name__)

from pydantic import BaseModel, Field

from fpulse.auth.deps import current_user_optional, require_admin, require_auth
from fpulse.auth.models import User, LoginRequest, RegisterRequest, InviteRequest
from fpulse.auth.password_policy import (
    MIN_LENGTH as PW_MIN_LENGTH,
    PasswordCheck,
    generate_strong_password,
    validate_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── Login rate-limit + account-lockout (2026-05-29) ──────────────────
#
# Threat: public OSS launches see automated credential-stuffing within
# hours. Without throttle/lockout, every leaked password list from
# someone else's breach is freely tested against /api/auth/login.
#
# Defense (in-process, no Redis dep):
#   * Per (email + client-ip) failed-attempt counter, sliding window.
#   * After LOGIN_SOFT_FAIL_THRESHOLD failures: progressive delay
#     starts (1s, then 2s, 4s, 8s up to 8s cap).
#   * After LOGIN_HARD_FAIL_THRESHOLD failures: 15-minute lockout —
#     login refuses even with correct credentials (returns 429).
#   * Success clears the counter for that (email, ip) pair.
#
# Limitations to be honest about:
#   * In-process state — restart resets counters. Acceptable for OSS
#     single-node; Plus distributed deployment will need a shared store.
#   * IPv4-only key — IPv6 attackers could rotate /64s. Future hardening.
#   * No CAPTCHA fallback — out of scope for v1.
#
# Tunables via env (defaults are reasonable):
#   FPULSE_LOGIN_SOFT_THRESHOLD    default 3  (delay-after-N-fails)
#   FPULSE_LOGIN_HARD_THRESHOLD    default 8  (lockout-after-N-fails)
#   FPULSE_LOGIN_LOCKOUT_SECONDS   default 900 (15 min)
#   FPULSE_LOGIN_WINDOW_SECONDS    default 900 (counter sliding window)

_LOGIN_SOFT_THRESHOLD = int(os.environ.get("FPULSE_LOGIN_SOFT_THRESHOLD", "3"))
_LOGIN_HARD_THRESHOLD = int(os.environ.get("FPULSE_LOGIN_HARD_THRESHOLD", "8"))
_LOGIN_LOCKOUT_SECONDS = int(os.environ.get("FPULSE_LOGIN_LOCKOUT_SECONDS", "900"))
_LOGIN_WINDOW_SECONDS = int(os.environ.get("FPULSE_LOGIN_WINDOW_SECONDS", "900"))
_LOGIN_MAX_DELAY_SECONDS = 8.0

_login_lock = threading.Lock()
# key = (email_lower, client_ip) -> {"count": N, "first_fail_ts": ts}
_login_fail_state: dict[tuple[str, str], dict] = {}


def _login_key(email: str, ip: str) -> tuple[str, str]:
    return ((email or "").strip().lower(), ip or "")


def _login_check_and_delay(email: str, ip: str) -> None:
    """Pre-login gate. Raises 429 if locked out; otherwise sleeps the
    progressive delay if past the soft threshold. No-ops on a clean key.

    Locks-out are eventually consistent: once the sliding window expires
    without further attempts, the counter is reaped lazily inside this
    function. So 'forgot my password and waited' just works without an
    explicit reset call.
    """
    key = _login_key(email, ip)
    now = time.time()
    with _login_lock:
        state = _login_fail_state.get(key)
        if state is None:
            return
        # Reap stale counters: if the most recent attempt is older than
        # the sliding window, the user has had their cool-down.
        if now - state.get("last_fail_ts", state["first_fail_ts"]) > _LOGIN_WINDOW_SECONDS:
            _login_fail_state.pop(key, None)
            return
        count = state["count"]
        # Hard lockout: refuse cleanly with 429 + Retry-After.
        if count >= _LOGIN_HARD_THRESHOLD:
            elapsed = now - state["first_fail_ts"]
            remaining = max(1, int(_LOGIN_LOCKOUT_SECONDS - elapsed))
            if remaining > 0:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "login_locked",
                        "message": (
                            f"Too many failed login attempts. Try again in "
                            f"{remaining // 60} minute(s)."
                        ),
                        "retry_after_seconds": remaining,
                    },
                    headers={"Retry-After": str(remaining)},
                )
            # Lockout window passed — reset on next attempt.
            _login_fail_state.pop(key, None)
            return
        # Soft-throttle: progressive delay starting at 1s. 2 ** (count - threshold).
        if count >= _LOGIN_SOFT_THRESHOLD:
            delay = min(
                _LOGIN_MAX_DELAY_SECONDS,
                2.0 ** (count - _LOGIN_SOFT_THRESHOLD),
            )
        else:
            delay = 0.0
    if delay > 0:
        # Sleep OUTSIDE the lock so other users aren't blocked.
        time.sleep(delay)


def _login_record_failure(email: str, ip: str) -> None:
    """Increment the failed-attempt counter for (email, ip)."""
    key = _login_key(email, ip)
    now = time.time()
    with _login_lock:
        state = _login_fail_state.get(key)
        if state is None:
            _login_fail_state[key] = {
                "count": 1,
                "first_fail_ts": now,
                "last_fail_ts": now,
            }
        else:
            state["count"] += 1
            state["last_fail_ts"] = now


def _login_record_success(email: str, ip: str) -> None:
    """Wipe the failed-attempt counter after a successful login."""
    key = _login_key(email, ip)
    with _login_lock:
        _login_fail_state.pop(key, None)


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email_change(store, user_id: str, new_email: str) -> str:
    """Validate an email being assigned to a user on the admin update path.

    `PUT /users/{id}` takes an untyped body and writes it straight to the
    user record, so without this guard an admin could set a malformed email
    or one already owned by another user. Since login is `get_user_by_email`,
    a duplicate makes login ambiguous. Returns the stripped email on success;
    raises 400 (bad format) / 409 (collision with a different user).
    """
    email = (new_email or "").strip()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "Invalid email address format.")
    existing = store.get_user_by_email(email)
    if existing is not None and getattr(existing, "id", None) != user_id:
        raise HTTPException(409, "That email is already in use by another user.")
    return email


def _enforce_password(password: str, *, email: str = "", name: str = "") -> None:
    """Run the password policy and raise 400 with a structured detail if it
    fails. Centralised so register / invite / reset / self-change all return
    the *same* error shape — the frontend lights up the same checklist no
    matter which endpoint rejected the password.
    """
    result = validate_password(password, email=email, name=name)
    if not result.ok:
        raise HTTPException(
            400,
            {
                "code": "weak_password",
                "message": "Password does not meet the strength policy.",
                "failures": result.failures,
                "suggestions": result.suggestions,
                "score": result.score,
                "label": result.label,
            },
        )


def _is_plus_active() -> bool:
    """Tiny helper — used by the request-access / reset endpoints to decide
    whether to log to the audit trail. Free tier has no audit, so guarding
    these calls keeps the code tidy.
    """
    from fpulse.main import app_state
    license_mgr = app_state.get("license_manager")
    return bool(license_mgr and license_mgr.is_plus)


def get_store():
    from fpulse.main import app_state
    return app_state["user_store"]


# Backwards-compatible alias — monitor.py imports _current_user from here.
# All real logic now lives in fpulse.auth.deps so role gates stay consistent.
def _current_user(request: Request):
    user = current_user_optional(request)
    if user is None:
        raise HTTPException(401, "Not authenticated")
    return user


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response):
    """Login with email/password.

    Session concurrency is controlled by admin settings:
      - unlimited: no limits (Free tier default)
      - single: 1 active session per user — new login kills old (Plus default)
      - capped: max N concurrent sessions
    """
    # Login is the one endpoint where a 500 is catastrophic — it locks
    # the user out of the entire product. Every sub-call below is
    # individually wrapped so a crash in (say) the audit logger or the
    # workspace lookup can never take down authentication itself. Only
    # the two calls that ARE authentication — password verify and
    # session create — can fail the request.
    # 2026-05-29: rate-limit + lockout gate. Runs BEFORE any DB lookup
    # so a brute-force attacker can't enumerate users via timing either.
    # The check is keyed on (email, client-ip) so a legit user on
    # another network isn't punished for an attacker's storm against
    # their account.
    _client_ip_for_throttle = request.client.host if request.client else ""
    _login_check_and_delay(body.email, _client_ip_for_throttle)

    try:
        store = get_store()
        user = store.get_user_by_email(body.email)
        if not user or not user.verify_password(body.password):
            # Record the failure BEFORE raising — drives the throttle
            # state machine for the next attempt from this key.
            _login_record_failure(body.email, _client_ip_for_throttle)
            raise HTTPException(401, "Invalid email or password")
        if not user.is_active:
            # Deactivated accounts also count as a failure — otherwise
            # an attacker can use the 403 vs 401 split as an oracle to
            # confirm which emails are real but deactivated. Treat the
            # same way.
            _login_record_failure(body.email, _client_ip_for_throttle)
            raise HTTPException(403, "Account is deactivated")
        # Successful credential check — wipe the failure counter for
        # this (email, ip) so the user's next session start is clean.
        _login_record_success(body.email, _client_ip_for_throttle)

        # 2026-06-03 (H1) — Transparent legacy hash migration. If the
        # user's password is still on the pre-bcrypt `salt:sha256`
        # format, rehash with the current bcrypt cost and persist. The
        # user sees no difference; subsequent logins use the bcrypt
        # path. After every legacy user logs in once, the auth store
        # contains only bcrypt hashes. Best-effort: a failure here
        # MUST NOT block the login (the credential check already
        # passed, the user is authenticated). Log and move on.
        if user.needs_rehash():
            try:
                store.set_password(user.id, User.hash_password(body.password))
            except Exception as _exc:
                import logging
                logging.getLogger(__name__).warning(
                    "Password rehash to bcrypt failed for user %s: %s. "
                    "User remains authenticated on the legacy hash; "
                    "next login will retry.",
                    user.id, _exc,
                )

        from fpulse.main import app_state
        license_mgr = app_state.get("license_manager")
        is_plus = bool(license_mgr and license_mgr.is_plus)

        # Read admin-configured session policy (best effort).
        session_mode = "unlimited"
        max_sessions = 1
        if is_plus:
            try:
                import json as _json
                db = app_state["db"]
                row = db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
                if row:
                    settings = _json.loads(row["data"])
                    session_mode = settings.get("session_mode", "single")
                    max_sessions = settings.get("max_concurrent_sessions", 1)
                else:
                    session_mode = "single"
            except Exception as exc:
                logger.warning("login: session-policy read failed: %s", exc)
                session_mode = "single"

        client_ip = request.client.host if request.client else ""

        session = store.create_session(
            user_id=user.id,
            ip_address=client_ip,
            session_mode=session_mode,
            max_sessions=max_sessions,
        )

        # Audit log — best effort, never blocks login.
        try:
            audit = app_state.get("audit_logger")
            if audit:
                audit.log(
                    user_id=user.id,
                    user_email=user.email,
                    action="login",
                    resource_type="session",
                    resource_id=session.token[:8],
                    ip_address=client_ip,
                    details={"session_mode": session_mode},
                )
        except Exception as exc:
            logger.warning("login: audit write failed (non-fatal): %s", exc)

        tier = license_mgr.tier if license_mgr else "free"

        # Workspace lookup — best effort. If this crashes the user can
        # still sign in; the frontend just won't have the workspace list
        # on first paint and will fetch it again on the next /me call.
        try:
            workspaces = _user_workspaces(user)
        except Exception as exc:
            logger.warning(
                "login: workspace lookup failed for %s (non-fatal): %s",
                user.email, exc,
            )
            workspaces = []

        # BFF dual-auth: also set the session as an HttpOnly cookie (so the
        # browser need not hold the token in JS) plus a readable CSRF token
        # for the double-submit guard. The token is STILL returned in the body
        # so CLI / programmatic / current bearer clients keep working.
        import secrets as _secrets
        from fpulse import runtime_config as _rc
        csrf_token = _secrets.token_urlsafe(32)
        _secure = _rc.IS_SERVER_MODE  # require HTTPS for the cookie when exposed
        response.set_cookie(
            "fpulse_session", session.token,
            httponly=True, secure=_secure, samesite="lax", path="/",
        )
        response.set_cookie(
            "fpulse_csrf", csrf_token,
            httponly=False, secure=_secure, samesite="lax", path="/",
        )
        return {
            "token": session.token,
            "csrf_token": csrf_token,
            "user": {
                "id": user.id,
                "email": user.email,
                "name": user.name,
                "role": user.role,
                "projects": user.projects,
                "environments": user.environments,
            },
            "tier": tier,
            "session_mode": session_mode,
            "workspaces": workspaces,
        }
    except HTTPException:
        raise
    except Exception as exc:
        # Anything unexpected — log the full traceback so the operator
        # can diagnose it from the backend console, and return a
        # structured 500 with the exception class name + message so the
        # LoginPage can show the real cause instead of a bare
        # "Internal Server Error".
        logger.error(
            "login: unexpected failure for %s: %s\n%s",
            body.email, exc, traceback.format_exc(),
        )
        raise HTTPException(
            500,
            {
                "code": "login_failed",
                "message": f"{type(exc).__name__}: {exc}",
            },
        )


def _read_signup_policy() -> dict:
    """Read the current signup policy from admin_settings.

    Returns a dict with:
      - allow_self_registration: whether /register is open to strangers
      - first_user_bootstrap: True iff the user table is empty — in that
        case the next /register call is allowed regardless of the flag
        and the created user is elevated to super_admin so the operator
        can bootstrap the instance without needing someone to invite them
    """
    from fpulse.main import app_state
    db = app_state.get("db")
    # Default = False. F-Pulse OSS is a single-operator install: the bootstrap
    # admin (seeded on first boot) is the only account, and self-registration
    # is OFF by default so that a network-exposed instance never lets strangers
    # create their own accounts. An operator who genuinely wants open signup
    # toggles it on in Admin → Security; the workspace-level `domain_allowlist`
    # still gates which users can be added to a corporate workspace regardless.
    allow = False
    if db:
        import json
        row = db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
        if row:
            try:
                allow = bool(json.loads(row["data"]).get("allow_self_registration", False))
            except Exception:
                allow = False
    # Bootstrap: if there are literally no users in the system, the FIRST
    # call to /register is always allowed — otherwise an operator who
    # just installed F-Pulse with `allow_self_registration=False` (the default)
    # would have no way to create the very first account. In practice OSS
    # auto-seeds the bootstrap admin on first boot, so the table is never empty
    # and this branch stays dormant — it's a safety net, not the normal path.
    user_store = app_state.get("user_store")
    is_bootstrap = False
    if user_store:
        try:
            is_bootstrap = len(user_store.list_users()) == 0
        except Exception:
            pass
    return {
        "allow_self_registration": allow,
        "first_user_bootstrap": is_bootstrap,
    }


@router.get("/signup-policy")
async def signup_policy():
    """Public endpoint the LoginPage calls to decide whether to show the
    "Sign up" link. Returns the same shape as `_read_signup_policy` minus
    anything sensitive.

    Deliberately unauthenticated — the login page can't hit it with a
    token since the user isn't signed in yet. The information leaked is
    limited to "does this instance accept new signups" which is already
    implicit from the login screen showing or hiding the signup link.
    """
    policy = _read_signup_policy()
    return {
        "allow_self_registration": policy["allow_self_registration"],
        "first_user_bootstrap": policy["first_user_bootstrap"],
    }


@router.post("/register")
async def register(body: RegisterRequest):
    """Register a new user.

    Gating rules (enforced in order):
      1. Email uniqueness — 409 if taken.
      2. Signup policy — if `allow_self_registration=False` AND this is
         not the first-user bootstrap, return 403. Admins create users
         via `/invite` in that mode instead.
      3. Seat limit — Plus only, unchanged.

    Role assignment:
      - If the user table is empty → `super_admin`. The first user on a
        fresh install is always the operator, so they own the instance
        end-to-end (including license activation).
      - Otherwise → `developer`. Admins promote manually via the Admin
        page if they want a second admin. We deliberately do NOT allow
        the register body to choose the role — that's a privilege
        escalation hole.
    """
    store = get_store()
    existing = store.get_user_by_email(body.email)
    if existing:
        raise HTTPException(409, "Email already registered")

    # Signup policy gate
    policy = _read_signup_policy()
    if not policy["allow_self_registration"] and not policy["first_user_bootstrap"]:
        raise HTTPException(
            403,
            "Self-registration is disabled on this server. "
            "Ask an administrator to invite you.",
        )

    # Strong-password gate. Runs BEFORE we touch the user store so a
    # weak-password attempt never creates a half-formed user record.
    # Pass email + name so the validator can reject "use my email as
    # my password" — the single most common bad pattern.
    _enforce_password(body.password, email=body.email, name=body.name or "")

    # Seat limit check (independent of signup policy — even an
    # invited user can't exceed the purchased seat count)
    from fpulse.main import app_state
    license_mgr = app_state.get("license_manager")
    if license_mgr and license_mgr.is_plus:
        current_users = len(store.list_users())
        if current_users >= license_mgr.seats:
            raise HTTPException(403, f"Seat limit reached ({license_mgr.seats}). Contact your admin.")

    # OSS role assignment. The FIRST user on a fresh install is the operator who
    # owns the box and must have full admin affordances (create projects, manage
    # connections, edit settings) — so they become super_admin. Every account
    # after that is a developer (an admin can promote them from the Admin page).
    # The request body can NEVER choose the role — that would be a
    # privilege-escalation hole.
    assigned_role = "super_admin" if policy["first_user_bootstrap"] else "developer"

    user = User(
        email=body.email,
        name=body.name or body.email.split("@")[0],
        password_hash=User.hash_password(body.password),
        role=assigned_role,
    )
    created = store.create_user(user)
    session = store.create_session(created.id)

    # Audit log
    audit = app_state.get("audit_logger")
    if audit:
        audit.log(
            user_id=created.id,
            user_email=created.email,
            action="register",
            resource_type="user",
            resource_id=created.id,
        )

    # Workspace bootstrap (schema v2).
    #
    # Self-registered users get ONLY a personal workspace — a private
    # sandbox where they are super_admin. They do NOT see the shared
    # Default workspace or any other user's data. An admin can later
    # invite them into a corporate/shared workspace via /invite or the
    # workspace members API.
    #
    # Exception: the very first user (super_admin bootstrap) also gets
    # Default workspace membership so they can administer the org-wide
    # workspace that admin-invited users will be added to.
    workspace_memberships: list[dict] = []
    try:
        ws_store = app_state.get("workspace_store")
        if ws_store:
            # First user (bootstrap) → also gets Default workspace
            if policy["first_user_bootstrap"]:
                try:
                    ws_store.add_member(
                        workspace_id="default",
                        user_id=created.id,
                        role="developer",
                        invited_by="system",
                        auto_accept=True,
                    )
                    workspace_memberships.append({
                        "workspace_id": "default",
                        "name": "Default",
                        "role": "developer",
                        "is_personal": False,
                    })
                except Exception:
                    pass

            # Personal workspace — every user gets one.
            try:
                personal_ws = ws_store.ensure_personal_workspace(
                    user_id=created.id,
                    user_email=created.email,
                    user_name=created.name,
                )
                workspace_memberships.append({
                    "workspace_id": personal_ws.id,
                    "name": personal_ws.name,
                    "role": "super_admin",
                    "is_personal": True,
                })
            except Exception:
                pass
    except Exception:
        pass

    return {
        "token": session.token,
        "user": {
            "id": created.id,
            "email": created.email,
            "name": created.name,
            "role": created.role,
        },
        "workspaces": workspace_memberships,
    }


def _user_workspaces(user) -> list[dict]:
    """Resolve the workspace memberships for a user, with names + per-workspace
    roles, suitable for display in the top-nav workspace switcher.

    Returns an empty list if the workspace store is not initialised
    (e.g. on a v1 install before the v2 migration has run) so the UI
    can degrade gracefully.
    """
    from fpulse.main import app_state
    ws_store = app_state.get("workspace_store")
    if not ws_store:
        return []

    # Self-heal: the seeded super_admin (id='admin') is created by
    # UserStore._ensure_admin() AFTER the v2 migration has already
    # finished enrolling the "existing users" snapshot, so on a fresh
    # install the admin shows up with zero workspace memberships even
    # though the Default workspace exists. Enrol them lazily on the
    # first /me or /login that goes through this path. Idempotent: if
    # they're already a member, is_member short-circuits and we skip.
    # Why only the seeded admin: any other user came through the
    # /register path which already creates a personal workspace and
    # adds Default membership, so they don't need this.
    # Self-heal: enrol the seeded super_admin in the Default workspace
    # if missing. The v2 schema migration only enrolled the snapshot of
    # users that existed BEFORE it ran, but the bootstrap admin
    # (id='admin') is created by UserStore._ensure_admin() AFTER
    # _init_schema, so on a fresh install the admin user appears with
    # zero workspace memberships even though the Default workspace
    # exists. We patch it lazily on the first /me or /login that goes
    # through this path. Idempotent: skipped once they're a member.
    # Why only the seeded admin: regular /register users hit the
    # personal-workspace creation path which already adds them to
    # Default; only the bootstrap path skips it.
    try:
        if (
            user.id == "admin"
            and ws_store.get("default")
            and not ws_store.is_member("default", user.id)
        ):
            ws_store.add_member(
                workspace_id="default",
                user_id=user.id,
                role="developer",
                invited_by="system",
                auto_accept=True,
            )
    except Exception:
        # Don't block login on a self-heal failure — the admin role
        # bypasses the workspace ACL anyway, so the user can still
        # work, they just won't see Default in the switcher this turn.
        pass

    out: list[dict] = []
    try:
        for ws in ws_store.list_for_user(user.id):
            role = ws_store.role_for(ws.id, user.id) or "viewer"
            out.append({
                "workspace_id": ws.id,
                "name": ws.name,
                "slug": ws.slug,
                "plan": ws.plan,
                "is_personal": ws.is_personal,
                "role": role,
            })
    except Exception:
        return []
    return out


@router.get("/me")
async def get_current_user(request: Request):
    """Get current user from session token, including workspace memberships
    so the frontend can populate the workspace switcher in one round-trip.
    """
    store = get_store()
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        raise HTTPException(401, "Not authenticated")
    user = store.get_user_for_session(token)
    if not user:
        raise HTTPException(401, "Invalid session")
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "projects": user.projects,
        "environments": user.environments,
        "workspaces": _user_workspaces(user),
    }


# ─── Self-service profile endpoints ─────────────────────────────────────────
# These let any logged-in user manage *their own* account without admin help.
# Distinct from /users/{id} (admin-only) which manages OTHER people's accounts.

class UpdateProfileRequest(BaseModel):
    """Whitelist of fields a user is allowed to change about themselves.

    Deliberately does NOT include `email`, `role`, `projects`, `environments`,
    or `is_active` — those are identity / authorization concerns and must
    remain admin-controlled to prevent privilege escalation.
    """
    name: str | None = Field(None, max_length=120)


@router.put("/me/profile")
async def update_my_profile(body: UpdateProfileRequest, user = Depends(require_auth)):
    """Update the *current user's* own profile fields.

    Why a separate endpoint instead of reusing `PUT /users/{id}`:
        `PUT /users/{id}` is gated by `require_admin` because it can change
        roles and project assignments — strictly an admin operation. But a
        normal developer should still be able to fix a typo in their own
        display name without filing a ticket. This endpoint enforces the
        narrow whitelist (currently just `name`) so self-edit can never
        become a privilege-escalation vector.
    """
    store = get_store()
    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name.strip()
    if not updates:
        return {"updated": False, "reason": "no_changes"}

    updated = store.update_user(user.id, updates)
    if not updated:
        raise HTTPException(404, "User not found")

    # Audit so admins can see who changed what about themselves.
    try:
        from fpulse.main import app_state
        audit = app_state.get("audit_logger")
        if audit:
            audit.log(
                user_id=user.id,
                user_email=user.email,
                action="self_profile_update",
                resource_type="user",
                resource_id=user.id,
                details={"fields": list(updates.keys())},
            )
    except Exception:
        pass

    return {
        "updated": True,
        "user": {
            "id": updated.id,
            "email": updated.email,
            "name": updated.name,
            "role": updated.role,
            "environments": updated.environments,
            "projects": updated.projects,
        },
    }


@router.get("/me/sessions")
async def list_my_sessions(request: Request, user = Depends(require_auth)):
    """List all active sessions for the *current user*.

    Lets the user audit their own login activity from the Account page —
    "where am I signed in right now?" — without exposing other users'
    sessions (which would require admin role via /api/plus/sessions/active).

    The current session is marked so the UI can highlight it and prevent
    self-revoke from inside the same browser tab (which would log you out
    immediately on the next request).
    """
    store = get_store()
    current_token = request.headers.get("Authorization", "").replace("Bearer ", "")

    sessions = []
    try:
        # The store may or may not expose list_sessions_for_user — fall back
        # to scanning all sessions if not. Either way we filter to the
        # caller's user_id so cross-user leakage is impossible.
        if hasattr(store, "list_sessions_for_user"):
            raw = store.list_sessions_for_user(user.id) or []
        elif hasattr(store, "list_sessions"):
            raw = [s for s in (store.list_sessions() or []) if getattr(s, "user_id", None) == user.id]
        else:
            raw = []

        for s in raw:
            token = getattr(s, "token", None) or (s.get("token") if isinstance(s, dict) else None)
            sessions.append({
                "id": (token or "")[:12],
                "created_at": str(getattr(s, "created_at", None) or (s.get("created_at") if isinstance(s, dict) else "")),
                "ip_address": getattr(s, "ip_address", None) or (s.get("ip_address") if isinstance(s, dict) else ""),
                "machine_id": getattr(s, "machine_id", None) or (s.get("machine_id") if isinstance(s, dict) else ""),
                "is_current": bool(token and current_token and token == current_token),
            })
    except Exception:
        # Soft-fail — never let the Account page error out because the
        # session store doesn't implement an enumeration helper.
        sessions = []

    return {"sessions": sessions, "count": len(sessions)}


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Logout — invalidate session (accepts bearer or cookie) + clear cookies."""
    store = get_store()
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.cookies.get("fpulse_session", "")
    if token:
        store.delete_session(token)
    response.delete_cookie("fpulse_session", path="/")
    response.delete_cookie("fpulse_csrf", path="/")
    return {"logged_out": True}


@router.get("/users")
async def list_users(_user = Depends(require_admin)):
    """List all users — admin only."""
    store = get_store()
    return store.list_users()


@router.post("/invite")
async def invite_user(body: InviteRequest, _user = Depends(require_admin)):
    """Invite a new user (admin creates account with strong temp password) — admin only.

    The admin specifies which workspace to add the user to (defaults to
    "default"). The invited user gets:
      1. Membership in the specified workspace with the chosen role.
      2. A personal workspace (their private sandbox).
    This mirrors the self-registration flow but scoped to admin-assigned
    resources instead of a blank slate.
    """
    store = get_store()
    existing = store.get_user_by_email(body.email)
    if existing:
        raise HTTPException(400, "Email already registered")

    # Seat limit check
    from fpulse.main import app_state
    license_mgr = app_state.get("license_manager")
    if license_mgr and license_mgr.is_plus:
        current_users = len(store.list_users())
        if current_users >= license_mgr.seats:
            raise HTTPException(403, f"Seat limit reached ({license_mgr.seats}).")

    temp_password = generate_strong_password(20)
    user = User(
        email=body.email,
        name=body.name or body.email.split("@")[0],
        password_hash=User.hash_password(temp_password),
        role=body.role,
        projects=body.projects,
    )
    created = store.create_user(user)

    # Workspace bootstrap for invited user
    workspace_memberships: list[dict] = []
    try:
        ws_store = app_state.get("workspace_store")
        if ws_store:
            # 1. Add to the admin-specified workspace
            try:
                ws_store.add_member(
                    workspace_id=body.workspace_id,
                    user_id=created.id,
                    role=body.role,
                    invited_by=_user.id if hasattr(_user, "id") else "admin",
                    auto_accept=True,
                )
                workspace_memberships.append({
                    "workspace_id": body.workspace_id,
                    "role": body.role,
                    "is_personal": False,
                })
            except Exception:
                pass

            # 2. Personal workspace
            try:
                personal_ws = ws_store.ensure_personal_workspace(
                    user_id=created.id,
                    user_email=created.email,
                    user_name=created.name,
                )
                workspace_memberships.append({
                    "workspace_id": personal_ws.id,
                    "name": personal_ws.name,
                    "role": "super_admin",
                    "is_personal": True,
                })
            except Exception:
                pass
    except Exception:
        pass

    # Audit log
    audit = app_state.get("audit_logger")
    if audit:
        audit.log(
            user_id=_user.id if hasattr(_user, "id") else "admin",
            user_email=_user.email if hasattr(_user, "email") else "admin",
            action="invite",
            resource_type="user",
            resource_id=created.id,
            details={
                "invited_email": created.email,
                "role": body.role,
                "workspace_id": body.workspace_id,
            },
        )

    return {
        "user_id": created.id,
        "email": created.email,
        "temp_password": temp_password,
        "message": f"User created. Temporary password: {temp_password}",
        "workspaces": workspace_memberships,
    }


# ─── Password policy + reset endpoints ─────────────────────────────────────

@router.get("/password-policy")
async def password_policy_endpoint():
    """Public endpoint — exposes the current password rules so the frontend
    strength meter and the "Generate" button stay in sync with the server.

    No auth required: leaking "passwords must be 12 chars" tells an attacker
    nothing they couldn't infer by trying a 4-character password and reading
    the 400 response. Keeping it public means the LoginPage can call it
    *before* the user is signed in, which is exactly when they need it.
    """
    return {
        "min_length": PW_MIN_LENGTH,
        "require_lower": True,
        "require_upper": True,
        "require_digit": True,
        "require_symbol": True,
        "block_common": True,
        "block_email_in_password": True,
        "rules": [
            f"At least {PW_MIN_LENGTH} characters",
            "At least one lowercase letter (a-z)",
            "At least one uppercase letter (A-Z)",
            "At least one digit (0-9)",
            "At least one symbol (!@#$%^&* etc.)",
            "Not on the common-password blocklist",
            "Does not contain your email or name",
        ],
    }


class CheckPasswordRequest(BaseModel):
    """Throwaway request body for live strength checks from the UI."""
    password: str
    email: str = ""
    name: str = ""


@router.post("/check-password")
async def check_password(body: CheckPasswordRequest):
    """Run the password through the validator and return the structured
    result without creating or changing anything. Powers the live meter
    on the Register / Account / Reset forms.

    Public endpoint: same reasoning as /password-policy. We never log the
    candidate password — the response is computed and discarded.
    """
    result = validate_password(body.password, email=body.email, name=body.name)
    return {
        "ok": result.ok,
        "score": result.score,
        "label": result.label,
        "failures": result.failures,
        "suggestions": result.suggestions,
    }


@router.get("/generate-password")
async def generate_password_endpoint(length: int = 20):
    """Return a freshly-generated strong password. Public — same threat
    model as a password manager's generator. The frontend "Suggest a
    strong password" button calls this so the generator stays consistent
    with the validator (one source of truth instead of two implementations).
    """
    if length < PW_MIN_LENGTH:
        length = PW_MIN_LENGTH
    if length > 128:  # sanity cap so a curl bomb can't allocate megabytes
        length = 128
    return {"password": generate_strong_password(length), "length": length}


class ChangePasswordRequest(BaseModel):
    """Self-serve change-password body. Requires the current password so a
    stolen session cookie can't silently rotate credentials behind the
    legitimate user's back.
    """
    current_password: str
    new_password: str


@router.post("/me/password")
async def change_my_password(
    body: ChangePasswordRequest,
    request: Request,
    user = Depends(require_auth),
):
    """Self-serve password change for the currently logged-in user.

    Re-verifies the current password before accepting the new one — even
    though the caller already passed `require_auth`, the session token
    alone is NOT proof that the human at the keyboard is the account
    owner (token theft, shared computer, forgotten logout). Asking for
    the current password closes that window.

    Runs the new password through `_enforce_password` so the policy is
    identical to what /register and /invite use.
    """
    store = get_store()
    fresh = store.get_user(user.id) if hasattr(store, "get_user") else None
    if not fresh:
        raise HTTPException(404, "User not found")
    if not fresh.verify_password(body.current_password):
        raise HTTPException(401, "Current password is incorrect")

    _enforce_password(body.new_password, email=fresh.email, name=fresh.name)

    # Hash + persist via the dedicated set_password path.
    # (2026-05-27: was previously update_user(password_hash=...) which
    # the store silently dropped — see store.set_password docstring.)
    updated = store.set_password(fresh.id, User.hash_password(body.new_password))
    if not updated:
        raise HTTPException(500, "Failed to update password")

    # Session hygiene: a password rotation must kill any token a thief
    # may hold. Keep the session that performed the change so the user
    # isn't logged out of the tab they're standing in. Best-effort —
    # never blocks the rotation itself.
    try:
        current_token = request.headers.get("Authorization", "").replace("Bearer ", "")
        store.revoke_other_sessions(fresh.id, keep_token=current_token)
    except Exception as exc:
        logger.warning(
            "Could not revoke other sessions after password change for %s: %s",
            fresh.id, exc,
        )

    # Bootstrap-password file cleanup (2026-05-22). When the bootstrap
    # admin rotates their password, the one-time INITIAL_ADMIN_PASSWORD.txt
    # on disk no longer corresponds to a valid credential AND would still
    # leak the original password to anyone with data-dir read access.
    # Delete it best-effort — never block the rotation on filesystem issues.
    if fresh.email == "admin@fpulse.local":
        try:
            import os
            from fpulse.main import app_state
            data_dir = app_state.get("data_dir")
            if data_dir:
                password_file = os.path.join(data_dir, "INITIAL_ADMIN_PASSWORD.txt")
                if os.path.exists(password_file):
                    os.remove(password_file)
                    logger.info(
                        "Removed bootstrap-password file at %s after admin rotation.",
                        password_file,
                    )
        except Exception as exc:
            logger.warning(
                "Bootstrap-password file cleanup failed (rotation still succeeded): %s",
                exc,
            )

    # Audit so admins can see voluntary password rotations.
    if _is_plus_active():
        try:
            from fpulse.main import app_state
            audit = app_state.get("audit_logger")
            if audit:
                audit.log(
                    user_id=fresh.id,
                    user_email=fresh.email,
                    action="self_password_change",
                    resource_type="user",
                    resource_id=fresh.id,
                )
        except Exception:
            pass

    return {"changed": True}


@router.post("/users/{user_id}/reset-password")
async def admin_reset_password(user_id: str, _user = Depends(require_admin)):
    """Admin-only password reset.

    Generates a new strong temp password, stores its hash, and returns
    the cleartext value to the admin ONCE in the response body. The
    admin is responsible for delivering it to the affected user
    out-of-band (chat / phone / in person). We never email it because
    F-Pulse OSS has no SMTP requirement — the admin reset flow is the
    universal fallback that works on every install regardless of
    network configuration.

    Why admin-only and not self-serve via email: this is the chosen
    forgot-password mechanism for the no-SMTP path. A self-serve email
    reset can be added later as a *second* mode behind the same UX,
    using a `pending_resets` table and a token email — but until SMTP
    is configured, this is the only safe way to recover an account.
    """
    store = get_store()
    target = store.get_user(user_id) if hasattr(store, "get_user") else None
    if not target:
        raise HTTPException(404, "User not found")

    new_password = generate_strong_password(20)
    # Dedicated set_password path — update_user silently strips
    # password_hash for security (see store.set_password docstring).
    updated = store.set_password(target.id, User.hash_password(new_password))
    if not updated:
        raise HTTPException(500, "Failed to reset password")

    # Force-logout everywhere: an admin reset usually means the account
    # is compromised or the owner lost control — no existing token
    # should survive the rotation.
    try:
        store.revoke_all_sessions(target.id)
    except Exception as exc:
        logger.warning(
            "Could not revoke sessions after admin reset for %s: %s",
            target.id, exc,
        )

    # Audit — admin password resets are sensitive enough that we always
    # want them in the trail, even on free tier where audit is a no-op.
    try:
        from fpulse.main import app_state
        audit = app_state.get("audit_logger")
        if audit:
            audit.log(
                user_id=_user.id,
                user_email=_user.email,
                action="admin_password_reset",
                resource_type="user",
                resource_id=target.id,
                details={"target_email": target.email},
            )
    except Exception:
        pass

    return {
        "user_id": target.id,
        "email": target.email,
        "temp_password": new_password,
        "message": (
            "Password reset. Share this temporary password with the user "
            "out-of-band (chat / phone). It will not be shown again."
        ),
    }


# ─── Forgot-password + request-access (works without SMTP) ─────────────────
#
# Both flows queue an *admin action* instead of emailing the user, so
# they work on a freshly installed F-Pulse with no email configuration.
# The queue lives in the existing `settings` table under id='auth_queue'
# (rather than a brand-new table) so we don't need a schema migration —
# the cost is one extra JSON parse on read, which is negligible at the
# scale this queue ever reaches (single-digit pending items in practice).

_AUTH_QUEUE_ID = "auth_queue"


def _read_auth_queue() -> dict:
    """Load the auth queue from the settings table.

    Returns a dict with three arrays:
        forgot_password: [{email, requested_at, ip}, ...]
        access_requests: [{email, name, reason, requested_at, ip}, ...]
        reset_tokens:    [{token, user_id, email, created_at, expires_at,
                           used, used_at, ip}, ...]

    Always returns a well-formed dict — missing rows / malformed JSON
    fall back to empty lists so callers never need to None-check.
    """
    from fpulse.main import app_state
    db = app_state.get("db")
    if not db:
        return {"forgot_password": [], "access_requests": [], "reset_tokens": []}
    row = db.fetchone("SELECT data FROM settings WHERE id = ?", (_AUTH_QUEUE_ID,))
    if not row:
        return {"forgot_password": [], "access_requests": [], "reset_tokens": []}
    import json
    try:
        data = json.loads(row["data"])
        data.setdefault("forgot_password", [])
        data.setdefault("access_requests", [])
        data.setdefault("reset_tokens", [])
        return data
    except Exception:
        return {"forgot_password": [], "access_requests": [], "reset_tokens": []}


def _write_auth_queue(queue: dict) -> None:
    """Persist the auth queue. Idempotent — overwrites the row each time."""
    from fpulse.main import app_state
    db = app_state.get("db")
    if not db:
        return
    import json
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO settings (id, data, created_at, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at""",
        (_AUTH_QUEUE_ID, json.dumps(queue), now_iso, now_iso),
    )


class ForgotPasswordRequest(BaseModel):
    email: str


#
# Reset token TTL — how long a forgot-password token stays valid before
# the user has to re-request. One hour is the common-case sweet spot:
# long enough that a user who walks away from their desk to check email
# can still come back and reset, short enough that a leaked token has
# a narrow attack window. Tokens are also single-use and are purged
# when a new reset is requested for the same user.
_RESET_TOKEN_TTL_SECONDS = 3600


def _smtp_is_configured() -> bool:
    """Cheap check used by the forgot-password flow to decide whether
    to email the reset link (and null the in-body token) or to keep the
    OSS no-SMTP fallback of returning the token in the response.

    Re-reads via ``NotificationService._load_smtp_config`` so the same
    Admin -> Settings row that drives alert delivery drives this gate too
    — no second source of truth, no restart needed when the admin
    configures SMTP for the first time.
    """
    try:
        from fpulse.alerts.notifier import NotificationService
        cfg = NotificationService._load_smtp_config()
        return bool(cfg.get("host"))
    except Exception:
        return False


def _send_reset_email(to: str, token: str, ttl_seconds: int, origin: str) -> bool:
    """Send the forgot-password reset link via SMTP.

    Returns True if the email was dispatched, False otherwise (SMTP
    not configured, send failed, etc.). Never raises — the caller
    treats failure as "token stays null in the response" so a flaky
    SMTP relay can't downgrade the anti-enumeration shape.
    """
    try:
        from fpulse.alerts.notifier import NotificationService
        notifier = NotificationService()
        if not notifier.smtp_host:
            return False
        url = f"{origin}/?reset_token={token}"
        ttl_minutes = max(1, ttl_seconds // 60)
        body = (
            "Hi,\n\n"
            "We received a request to reset the password on your F-Pulse account.\n\n"
            "Click the link below to set a new password:\n\n"
            f"  {url}\n\n"
            f"This link expires in {ttl_minutes} minutes and can only be used once.\n"
            "If you didn't request a reset, you can safely ignore this email.\n\n"
            "-- F-Pulse OSS"
        )
        notifier.send_simple_email(to, "Reset your F-Pulse password", body)
        return True
    except Exception as exc:
        logger.warning("Failed to send password reset email to %s: %s", to, exc)
        return False


def _request_origin(request: Request) -> str:
    """Best-effort recovery of the public-facing origin (scheme + host)
    so the reset link in the outgoing email is clickable.

    Preference order:
      1. ``FPULSE_PUBLIC_URL`` env var — explicit override for deployments
         where the API host differs from the user-facing host (reverse
         proxy, split frontend/backend).
      2. The request's ``Origin`` header — set by browsers when the
         frontend POSTs to /api.
      3. ``request.base_url`` — fine for single-binary OSS where the
         API and the SPA share one origin.
    """
    import os as _os
    override = _os.environ.get("FPULSE_PUBLIC_URL")
    if override:
        return override.rstrip("/")
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")
    try:
        return str(request.base_url).rstrip("/")
    except Exception:
        host = request.headers.get("host", "localhost")
        scheme = request.url.scheme if getattr(request, "url", None) else "http"
        return f"{scheme}://{host}"


def _prune_expired_reset_tokens(queue: dict) -> dict:
    """Drop expired / used reset tokens from the queue.

    Called opportunistically inside every forgot-password flow (read AND
    write) so the settings row never grows unbounded. We don't need a
    background job for this — the queue is single-digit entries in
    practice and the pruning cost is negligible.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    alive = []
    for t in queue.get("reset_tokens", []):
        if t.get("used"):
            continue
        try:
            expires = datetime.fromisoformat(t.get("expires_at", ""))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= now:
                continue
        except Exception:
            continue
        alive.append(t)
    queue["reset_tokens"] = alive
    return queue


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, request: Request):
    """Public endpoint the LoginPage calls when a user clicks "Forgot password".

    Behaviour is *deliberately uniform* whether or not the email exists in
    the user table — we always return 200 with the same message shape.
    Leaking "this email is registered" lets an attacker enumerate
    accounts via the forgot-password form.

    When the email DOES match a real user we do two things:

      1. Generate a single-use self-serve reset token (1 hour TTL) and
         stash it in the auth queue. The token is returned to the caller
         inside the response body (`reset_token` field) on this F-Pulse
         instance because F-Pulse OSS has no SMTP requirement — the
         user can either copy the link directly from the success screen,
         OR an admin can pick it up from the Admin → Auth Queue page
         and pass it along out-of-band.

      2. Still append a "forgot_password" entry to the admin queue so
         admins on multi-user instances see the request even if the
         user never interacted with the self-serve screen.

    Anti-enumeration shape: when the email does NOT match a real user,
    we return the same `queued: True` response with NO token and NO
    admin-queue entry. The caller cannot distinguish the two cases from
    the response alone.
    """
    import secrets
    from datetime import datetime, timedelta, timezone

    store = get_store()
    user = store.get_user_by_email(body.email)
    reset_token: str | None = None
    expires_at_iso: str | None = None
    smtp_configured = _smtp_is_configured()
    email_sent = False

    if user:
        queue = _read_auth_queue()
        _prune_expired_reset_tokens(queue)

        # Dedupe the admin queue entry (refresh-loop protection).
        existing_emails = {e.get("email") for e in queue["forgot_password"]}
        if body.email not in existing_emails:
            queue["forgot_password"].append({
                "email": body.email,
                "user_id": user.id,
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "ip": request.client.host if request.client else "",
            })

        # Invalidate any previous unused tokens for this user — a new
        # request supersedes an older one.
        queue["reset_tokens"] = [
            t for t in queue["reset_tokens"]
            if t.get("user_id") != user.id
        ]

        # Mint a new token. 32 URL-safe bytes → 43-char token, which
        # is 256 bits of entropy, well past any reasonable brute-force
        # threshold for an hour-long window.
        reset_token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=_RESET_TOKEN_TTL_SECONDS)
        expires_at_iso = expires.isoformat()
        queue["reset_tokens"].append({
            "token": reset_token,
            "user_id": user.id,
            "email": user.email,
            "created_at": now.isoformat(),
            "expires_at": expires_at_iso,
            "used": False,
            "used_at": None,
            "ip": request.client.host if request.client else "",
        })

        _write_auth_queue(queue)

        # If SMTP is configured on this instance, deliver the link by
        # email and DON'T echo the token in the API response — that
        # closes the no-SMTP account-takeover hole where any caller
        # who knew a registered email could curl the API and read the
        # live reset token. When SMTP isn't configured we keep the
        # existing OSS fallback (token in body) so a single-binary
        # local install still works without infrastructure.
        if smtp_configured:
            email_sent = _send_reset_email(
                to=user.email,
                token=reset_token,
                ttl_seconds=_RESET_TOKEN_TTL_SECONDS,
                origin=_request_origin(request),
            )

        # Audit on Plus so admins see password reset activity in the
        # trail even before the user completes the flow.
        if _is_plus_active():
            try:
                from fpulse.main import app_state
                audit = app_state.get("audit_logger")
                if audit:
                    audit.log(
                        user_id=user.id,
                        user_email=user.email,
                        action="forgot_password_requested",
                        resource_type="user",
                        resource_id=user.id,
                        details={"ip": request.client.host if request.client else ""},
                    )
            except Exception:
                pass

    # Uniform response shape. The message text is driven by *SMTP
    # configuration*, not by whether the email matched, so an attacker
    # cannot distinguish known/unknown accounts by reading the response:
    #
    #   SMTP configured   -> message says "emailed", token = null in body
    #                        regardless of whether the user existed.
    #   SMTP not configured -> message says "generated", token in body
    #                          only when the user existed.
    #
    # ``email_sent`` is internal-only — we don't expose it to the
    # caller. If SMTP is configured but the send itself failed (relay
    # down, etc.) we still return token=null so a broken relay can't
    # downgrade the security posture; the user can retry once SMTP is
    # healthy, or an admin can read the token from the auth queue.
    # Only echo the reset token in the API response under safe conditions —
    # otherwise any caller who submits a *registered* email receives a live
    # account-takeover token from a public, unauthenticated endpoint (and
    # the token's presence also leaks that the email exists, undermining the
    # anti-enumeration guarantee). Safe to inline when:
    #   (a) single-user instance — you can only reset your own account, or
    #   (b) explicitly opted in from a loopback client
    #       (FPULSE_FORGOT_TOKEN_INLINE=1) for local single-binary dev.
    # Default multi-user posture: token is NOT returned; an admin reads it
    # from the Auth Queue, or the instance configures SMTP for emailed links.
    import os as _os
    _ip = request.client.host if request.client else ""
    _loopback = _ip in {"127.0.0.1", "::1", "localhost"}
    _inline_flag = _os.environ.get("FPULSE_FORGOT_TOKEN_INLINE", "").strip().lower() in {"1", "true", "yes", "on"}
    try:
        _single_user = len(store.list_users()) <= 1
    except Exception:
        _single_user = False
    _inline_ok = _single_user or (_inline_flag and _loopback)
    token_in_body = reset_token if (reset_token and not smtp_configured and _inline_ok) else None
    if smtp_configured:
        message = (
            "If that email is registered, a reset link has been emailed to you. "
            "Check your inbox (and the spam folder) for a message from F-Pulse."
        )
    else:
        message = (
            "If that email is registered, a reset link has been generated. "
            "You can use it below to set a new password, or ask your "
            "administrator to share it with you out-of-band."
        )
    return {
        "queued": True,
        "message": message,
        "reset_token": token_in_body,
        "expires_at": expires_at_iso if token_in_body else None,
        "ttl_seconds": _RESET_TOKEN_TTL_SECONDS if token_in_body else None,
    }


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@router.get("/reset-password/verify/{token}")
async def verify_reset_token(token: str):
    """Public — check whether a reset token is valid before showing the
    "choose new password" form.

    Returns 404 for missing / expired / used tokens with the same body
    shape so the frontend can show a clean "link expired" message
    without the component having to disambiguate error codes.
    """
    from datetime import datetime, timezone

    queue = _read_auth_queue()
    for t in queue.get("reset_tokens", []):
        if t.get("token") != token:
            continue
        if t.get("used"):
            raise HTTPException(404, "Reset link has already been used")
        try:
            expires = datetime.fromisoformat(t.get("expires_at", ""))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                raise HTTPException(404, "Reset link has expired")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(404, "Reset link is invalid")

        return {
            "valid": True,
            "email": t.get("email", ""),
            "expires_at": t.get("expires_at"),
        }

    raise HTTPException(404, "Reset link is invalid")


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, request: Request):
    """Public — complete a forgot-password flow by consuming a valid
    reset token and setting a new password.

    Steps, all enforced:
      1. Token exists, not used, not expired.
      2. New password passes the same strength policy as /register.
      3. Token is marked `used=True` BEFORE the password is written,
         so a concurrent duplicate submission can't consume the same
         token twice.
      4. Any other active sessions for the user are NOT auto-killed
         here — that's the caller's responsibility (the UI prompts
         the user to log in again after reset, which creates a fresh
         session and, if session_mode=single, replaces any old one).
    """
    from datetime import datetime, timezone

    queue = _read_auth_queue()
    match = None
    for t in queue.get("reset_tokens", []):
        if t.get("token") == body.token:
            match = t
            break

    if not match:
        raise HTTPException(404, "Reset link is invalid")
    if match.get("used"):
        raise HTTPException(404, "Reset link has already been used")
    try:
        expires = datetime.fromisoformat(match.get("expires_at", ""))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            raise HTTPException(404, "Reset link has expired")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(404, "Reset link is invalid")

    store = get_store()
    target = store.get_user(match.get("user_id")) if hasattr(store, "get_user") else None
    if not target:
        # User was deleted between requesting the reset and consuming
        # it. Invalidate the token and bail with a generic error.
        match["used"] = True
        _write_auth_queue(queue)
        raise HTTPException(404, "Reset link is invalid")

    # Strength gate — same policy as register / invite / self-change.
    _enforce_password(body.new_password, email=target.email, name=target.name)

    # Mark used FIRST so a duplicate submission loses the race.
    match["used"] = True
    match["used_at"] = datetime.now(timezone.utc).isoformat()
    _write_auth_queue(queue)

    # Dedicated set_password path — update_user silently strips
    # password_hash for security (see store.set_password docstring).
    updated = store.set_password(
        target.id, User.hash_password(body.new_password),
    )
    if not updated:
        raise HTTPException(500, "Failed to update password")

    # The forgot-password flow implies the caller may not control their
    # old sessions (lost device, shared machine) — revoke them all so
    # the new password is the only way in.
    try:
        store.revoke_all_sessions(target.id)
    except Exception as exc:
        logger.warning(
            "Could not revoke sessions after password reset for %s: %s",
            target.id, exc,
        )

    # Clean up — drop the satisfied entry from the admin forgot_password
    # queue (if present) so the admin page doesn't keep showing a
    # request that has already been fulfilled by self-serve.
    queue = _read_auth_queue()
    queue["forgot_password"] = [
        e for e in queue["forgot_password"] if e.get("email") != target.email
    ]
    # Re-mark the token as used in the re-read queue because we read a
    # fresh copy after _enforce_password, and drop it fully.
    queue["reset_tokens"] = [
        t for t in queue["reset_tokens"] if t.get("token") != body.token
    ]
    _write_auth_queue(queue)

    if _is_plus_active():
        try:
            from fpulse.main import app_state
            audit = app_state.get("audit_logger")
            if audit:
                audit.log(
                    user_id=target.id,
                    user_email=target.email,
                    action="self_password_reset",
                    resource_type="user",
                    resource_id=target.id,
                    details={"ip": request.client.host if request.client else ""},
                )
        except Exception:
            pass

    return {
        "reset": True,
        "email": target.email,
        "message": "Password has been reset. You can now sign in with your new password.",
    }


class RequestAccessBody(BaseModel):
    """Public 'request access' form. Captures enough that an admin can
    decide whether to approve, but no more — we don't ask for a password
    here because the admin generates one on approval.
    """
    email: str
    name: str = ""
    reason: str = ""


@router.post("/request-access")
async def request_access(body: RequestAccessBody, request: Request):
    """Public endpoint — anyone can submit a request to be granted access.

    Visible on the LoginPage even when self-registration is OFF (which is
    the default), so the "blind admin" problem is solved: an admin who
    keeps signup closed still gets a queued notification when someone
    needs an account, instead of having to be told out-of-band.

    Same anti-enumeration stance as /forgot-password — we never tell the
    caller whether the email is already registered. The admin sees the
    duplicate when they review the queue.
    """
    from datetime import datetime, timezone
    queue = _read_auth_queue()
    existing_emails = {e.get("email") for e in queue["access_requests"]}
    if body.email and body.email not in existing_emails:
        queue["access_requests"].append({
            "email": body.email,
            "name": body.name,
            "reason": (body.reason or "").strip()[:500],  # cap to keep the queue small
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "ip": request.client.host if request.client else "",
        })
        _write_auth_queue(queue)
    return {
        "queued": True,
        "message": (
            "Request received. An administrator will review your request and "
            "create your account if approved."
        ),
    }


@router.get("/auth-queue")
async def get_auth_queue(_user = Depends(require_admin)):
    """Admin-only — return the full auth queue (forgot-password + access requests)
    so the Admin page can render the pending list and act on it.
    """
    return _read_auth_queue()


class DismissQueueItem(BaseModel):
    kind: str   # "forgot_password" | "access_requests"
    email: str


@router.post("/auth-queue/dismiss")
async def dismiss_queue_item(body: DismissQueueItem, _user = Depends(require_admin)):
    """Admin-only — drop one entry from the auth queue.

    Used after the admin has either fulfilled the request (created the
    user / reset the password) or decided it's spam. We match by email
    inside the named bucket so an attacker who knows the API can't
    blindly clear the whole queue.
    """
    if body.kind not in ("forgot_password", "access_requests"):
        raise HTTPException(400, "Invalid queue kind")
    queue = _read_auth_queue()
    bucket = queue.get(body.kind, [])
    queue[body.kind] = [e for e in bucket if e.get("email") != body.email]
    _write_auth_queue(queue)
    return {"dismissed": True, "remaining": len(queue[body.kind])}


@router.put("/users/{user_id}")
async def update_user(user_id: str, body: dict, _user = Depends(require_admin)):
    """Update a user's role, projects, active status, or email — admin only."""
    store = get_store()
    # Guard email changes: validate format + reject collisions before the
    # untyped body is written (login is by email, so a duplicate breaks it).
    if isinstance(body, dict) and body.get("email") is not None:
        body = {**body, "email": _validate_email_change(store, user_id, body["email"])}
    user = store.update_user(user_id, body)
    if not user:
        raise HTTPException(404, "User not found")
    return {"updated": True}


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, _user = Depends(require_admin)):
    """Delete a user (cannot delete admin) — admin only."""
    store = get_store()
    if not store.delete_user(user_id):
        raise HTTPException(400, "Cannot delete this user")
    return {"deleted": True}
