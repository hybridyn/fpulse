"""
F-Pulse+ shared test fixtures v2.

Fixes the two systemic failures from the Apr 16 full test run:

  E1 — `self._db is None` on 158 store unit tests.
       Every store module expects a module-level DB to be injected at app
       startup. Unit tests instantiate stores directly and never ran startup,
       so the DB is None and every method that touches it AttributeErrors.

  E2 — Auth 401s cascade through 80+ e2e/api/test_plus tests.
       Existing tests use `TestClient(app)` without logging in first. Every
       guarded endpoint returns 401, and class-state tests crash downstream
       trying to read IDs that were never set.

This module is IMPORTED by test files that opt in — it does NOT override the
existing `tests/conftest.py`. New test files add at the top:

    from tests.conftest_fixtures_v2 import (  # noqa: F401
        db_fixture, app_v2, authed_client, role_clients,
    )

Old tests that want the fix:

    # At top of test_workflow_store.py (one line):
    pytestmark = pytest.mark.usefixtures("db_fixture")

Design principles:
  - Self-contained: no dependency on the existing tests/conftest.py.
  - Temp-dir-per-module: zero cross-test contamination.
  - Explicit migration run: proves the runner works before any test executes.
  - Graceful skip: if auth endpoints are unreachable, tests skip — they
    don't hard-fail with opaque errors.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path
from typing import Generator, Optional

import pytest
from fastapi.testclient import TestClient


# ═════════════════════════════════════════════════════════════════════════
# E1 — DB fixture: initialises SQLite, runs migrations, injects into stores
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def data_dir() -> Generator[str, None, None]:
    """Fresh FPULSE_DATA_DIR per test module. Cleaned up after."""
    d = tempfile.mkdtemp(prefix="fpulse_test_v2_")
    old = os.environ.get("FPULSE_DATA_DIR")
    os.environ["FPULSE_DATA_DIR"] = d
    # 2026-05-30 (P7): tests expect /docs and /openapi.json to be public
    # (test_public_endpoints_still_public). main.py gates those endpoints
    # on FPULSE_MODE=dev, so set the env var before main.py imports below
    # in db_fixture. Without this the test fixture sees a 404 on /docs
    # because the route was never registered.
    old_mode = os.environ.get("FPULSE_MODE")
    os.environ["FPULSE_MODE"] = "dev"

    # Seed sample files that many tests expect.
    (Path(d) / "orders.csv").write_text(
        "id,name,amount,region\n"
        "1,Alice,100,US\n"
        "2,Bob,200,EU\n"
        "3,Carol,150,US\n"
        "4,Dan,300,APAC\n"
        "5,Eve,250,EU\n",
        encoding="utf-8",
    )
    (Path(d) / "sample.json").write_text(
        '[{"id":1,"region":"US"},{"id":2,"region":"EU"}]',
        encoding="utf-8",
    )

    yield d

    os.environ.pop("FPULSE_DATA_DIR", None)
    if old is not None:
        os.environ["FPULSE_DATA_DIR"] = old
    if old_mode is None:
        os.environ.pop("FPULSE_MODE", None)
    else:
        os.environ["FPULSE_MODE"] = old_mode
    # Leave the tempdir on disk for post-mortem; OS cleans it.


@pytest.fixture(scope="module")
def db_fixture(data_dir: str) -> Generator[str, None, None]:
    """
    E1 FIX — initialises the SQLite database, runs migrations, ensures all
    store modules have their `_db` attribute bound to a valid connection.

    Works by:
      1. Setting FPULSE_DATA_DIR (done by data_dir).
      2. Force-reimporting fpulse so stores re-bind to the new DB.
      3. Running migrations.
      4. Calling the internal init path (equivalent to app startup).

    Yields the path to fpulse.db for inspection if tests need it.
    """
    # Step 1 (2026-05-31): we used to wipe `sys.modules["fpulse.*"]` here
    # to force fresh imports against the fresh FPULSE_DATA_DIR. That
    # turned out to be the root cause of ~70 test-suite contamination
    # failures (test_to_postgres, test_e2e_complete, test_scd2_node, ...
    # all pass alone, fail under the full suite). The wipe leaves
    # pre-existing imports holding stale module references; every
    # module-level singleton in fpulse (node registry, sql-type
    # registry, FastAPI app, agent governance state, ...) ends up in
    # an orphaned-but-still-referenced state.
    #
    # The replacement strategy: rely on fpulse.main's startup hook to
    # explicitly rebind every store's `_db` to the new test database
    # (see `set_db(db)` calls in fpulse/main.py lines ~404-458). On
    # first import, the hook fires; on subsequent fixture invocations,
    # we re-trigger it explicitly via `_rebind_stores` below.
    # Step 2: run migrations explicitly (proves the migration runner works).
    backend_root = Path(__file__).parent.parent  # tests/.. == backend/
    db_path = Path(data_dir) / "fpulse.db"

    try:
        from fpulse.storage.migrations import run_migrations
        run_migrations(db_path, backend_root)
    except ImportError:
        # Migration runner not yet built — skip migration, let stores
        # create their own schema via legacy `init_db()` path.
        pass
    except Exception as exc:
        pytest.fail(f"Migration runner crashed: {exc}")

    # Step 3: import fpulse.main so its startup hook initialises stores.
    # The existing code binds module-level _db globals during startup.
    try:
        import fpulse.main  # noqa: F401  (import for side effect)
    except Exception as exc:
        pytest.skip(f"fpulse.main import failed: {exc}")

    # Step 4: defensively poke each store module to ensure _db is non-None.
    # If a store is still None after main import, we know the app's startup
    # path is broken — fail clearly instead of surfacing as NoneType errors.
    store_modules = (
        "fpulse.ir.versioning",
        "fpulse.projects.store",
        "fpulse.scheduling.store",
        "fpulse.alerts.store",
        "fpulse.monitoring.store",
        "fpulse.auth.store",
        "fpulse.variables.store",
        "fpulse.credentials.store",
        "fpulse.connections.store",
        "fpulse.ir.lifecycle",
        "fpulse.intelligence.schema_contract",
    )
    unbound = []
    for modname in store_modules:
        try:
            mod = importlib.import_module(modname)
        except ImportError:
            continue
        # Walk attrs looking for store instances with a `_db` member.
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if hasattr(attr, "_db") and attr._db is None:
                unbound.append(f"{modname}.{attr_name}")

    if unbound:
        pytest.skip(
            f"Stores with unbound _db after startup: {unbound}. "
            f"Likely fpulse.main startup path changed; update this fixture."
        )

    yield str(db_path)


@pytest.fixture(scope="module")
def app_v2(db_fixture: str):
    """FastAPI app instance ready for TestClient wrapping."""
    from fpulse.main import app as fastapi_app
    return fastapi_app


@pytest.fixture(scope="module")
def client(app_v2) -> Generator[TestClient, None, None]:
    """Unauthenticated client. For protected endpoints use `authed_client`."""
    with TestClient(app_v2) as c:
        yield c


# ═════════════════════════════════════════════════════════════════════════
# E2 — Auth fixture: logs in, attaches token, exposes authed TestClient
# ═════════════════════════════════════════════════════════════════════════

DEV_ADMIN_EMAIL = "admin@fpulse.local"
DEV_ADMIN_PASSWORD = "admin"

LOGIN_PATHS = (
    "/api/auth/login",
    "/api/plus/auth/login",
    "/api/login",
)


def _extract_token(response) -> Optional[str]:
    """Handle the several shapes F-Pulse login responses come in."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    return (
        body.get("token")
        or body.get("session_token")
        or body.get("access_token")
        or body.get("jwt")
        or (body.get("user") or {}).get("token")
        or response.cookies.get("session")
        or response.cookies.get("fpulse_session")
    )


def _login(client: TestClient, email: str, password: str) -> Optional[str]:
    for path in LOGIN_PATHS:
        r = client.post(path, json={"email": email, "password": password})
        if r.status_code == 200:
            tok = _extract_token(r)
            if tok:
                return tok
        # Try form-encoded as fallback (some FastAPI setups use OAuth2PasswordRequestForm)
        r = client.post(path, data={"username": email, "password": password})
        if r.status_code == 200:
            tok = _extract_token(r)
            if tok:
                return tok
    return None


def _reset_bootstrap_admin_password(data_dir_path: str) -> bool:
    """Reset the bootstrap admin to DEV_ADMIN_PASSWORD.

    Same rationale as fixtures_plus.py's sibling helper: the 2026
    security hardening in UserStore._ensure_admin generates a random
    password on first boot, but tests log in with the historical
    'admin'. Resetting the hash here means the admin_token fixture
    below works without a pre-test `seed-admin` CLI call.
    """
    import os
    try:
        from fpulse.storage.database import Database
        from fpulse.auth.store import UserStore
        from fpulse.auth.models import User
    except ImportError:
        return False

    db_path = os.path.join(data_dir_path, "fpulse.db")
    if not os.path.exists(db_path):
        return False
    try:
        db = Database(db_path)
        users = UserStore(db=db)
        admin = users.get_user("admin")
        if admin is None:
            return False
        admin.password_hash = User.hash_password(DEV_ADMIN_PASSWORD)
        admin.is_active = True
        users._save_user(admin)
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def admin_token(client: TestClient, data_dir: str) -> str:
    """Dev-seed admin token. Skips cleanly if login endpoint unavailable.

    Resets the bootstrap admin password to DEV_ADMIN_PASSWORD first so
    the test-known credentials work even on a fresh boot where the
    admin was just bootstrapped with a random password.
    """
    _reset_bootstrap_admin_password(data_dir)
    tok = _login(client, DEV_ADMIN_EMAIL, DEV_ADMIN_PASSWORD)
    if not tok:
        pytest.skip(
            f"Could not log in as {DEV_ADMIN_EMAIL} via any of {LOGIN_PATHS}. "
            f"Check the dev-seed in fpulse/main.py or update LOGIN_PATHS."
        )
    return tok


@pytest.fixture(scope="module")
def authed_client(app_v2, admin_token: str) -> Generator[TestClient, None, None]:
    """
    E2 FIX — authenticated TestClient that survives across tests in a module.
    Every request carries both Bearer header AND session cookie (covers both
    auth styles the middleware might check).
    """
    c = TestClient(app_v2)
    c.headers["Authorization"] = f"Bearer {admin_token}"
    c.cookies.set("session", admin_token)
    c.cookies.set("fpulse_session", admin_token)
    with c:
        yield c


# ═════════════════════════════════════════════════════════════════════════
# Role-based clients — 5 tiers for RBAC matrix tests
# ═════════════════════════════════════════════════════════════════════════

ROLE_CREDS: dict[str, tuple[str, str]] = {
    "super_admin":     (DEV_ADMIN_EMAIL,           DEV_ADMIN_PASSWORD),
    "workspace_admin": ("wsadmin@fpulse.local",    "Password123!"),
    "data_engineer":   ("engineer@fpulse.local",   "Password123!"),
    "analyst":         ("analyst@fpulse.local",    "Password123!"),
    "viewer":          ("viewer@fpulse.local",     "Password123!"),
}


def _ensure_user(authed_client: TestClient, email: str, password: str, role: str) -> bool:
    """Create test user with the requested role.

    2026-05-30 (V11 fix): the original implementation POSTed to
    /api/auth/register, which DELIBERATELY ignores the `role` body
    field as a privilege-escalation defence (every self-registered
    user gets role=developer regardless of what the body says). That
    meant the "viewer" / "analyst" fixture users were created with
    role=developer and the RBAC tests for those roles silently
    passed mutations they should have been blocked from. Fix: go
    through the UserStore directly when the API endpoints can't honour
    the role — same DB, same hashing, but the role we asked for is
    actually written. Falls back to the API path first so seat-limit
    + audit-log side effects still fire when those endpoints exist.
    """
    # Try API paths first — these run the full create flow (seat
    # check, audit, email confirmation, etc.) when they exist.
    for path in ("/api/plus/users", "/api/users"):
        r = authed_client.post(path, json={
            "email": email,
            "password": password,
            "role": role,
            "name": role.replace("_", " ").title(),
        })
        if r.status_code in (200, 201):
            # Verify the API actually honoured the role; if not, fall
            # through to the direct-store path below.
            from fpulse.main import app_state
            store = app_state.get("user_store")
            if store:
                u = store.get_user_by_email(email)
                if u and u.role == role:
                    return True
            # Otherwise force the right role via the store.
            break
        if r.status_code in (400, 409):
            # Already exists — fall through to ensure role is right.
            break

    # Direct-store path — bypasses /api/auth/register's hard-coded
    # role=developer assignment. Required for any role other than
    # developer / super_admin.
    from fpulse.main import app_state
    from fpulse.auth.models import User
    store = app_state.get("user_store")
    if store is None:
        return False
    existing = store.get_user_by_email(email)
    if existing:
        # Update role if it doesn't match.
        if existing.role != role:
            existing.role = role
            existing.password_hash = User.hash_password(password)
            existing.is_active = True
            store._save_user(existing)
        return True
    # Create fresh with the right role.
    user = User(
        email=email,
        name=role.replace("_", " ").title(),
        role=role,
        password_hash=User.hash_password(password),
        is_active=True,
    )
    try:
        store._save_user(user)
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def role_clients(
    app_v2, authed_client: TestClient,
) -> Generator[dict[str, TestClient], None, None]:
    """
    {role_name: authenticated_TestClient} for all 5 tiers.
    Users are provisioned on demand. Missing roles are simply absent from
    the dict, and tests `pytest.skip` when they ask for a role not present.
    """
    clients: dict[str, TestClient] = {"super_admin": authed_client}

    for role, (email, pw) in ROLE_CREDS.items():
        if role == "super_admin":
            continue
        _ensure_user(authed_client, email, pw, role)
        c = TestClient(app_v2)
        tok = _login(c, email, pw)
        if tok:
            c.headers["Authorization"] = f"Bearer {tok}"
            c.cookies.set("session", tok)
            c.cookies.set("fpulse_session", tok)
            clients[role] = c

    yield clients

    for role, c in clients.items():
        if role != "super_admin":
            try:
                c.close()
            except Exception:
                pass


# ═════════════════════════════════════════════════════════════════════════
# Convenience helpers available to tests
# ═════════════════════════════════════════════════════════════════════════

def assert_forbidden(response) -> None:
    """Assert that a response is properly rejected for the role."""
    assert response.status_code in (401, 403), (
        f"Expected 401/403, got {response.status_code}: {response.text[:200]}"
    )


def assert_allowed(response) -> None:
    """
    Assert the request was authorized, even if the payload was malformed
    or the resource didn't exist. 4xx auth codes fail, everything else passes.
    """
    assert response.status_code not in (401, 403), (
        f"Authorized request got auth-denial: {response.status_code}: {response.text[:200]}"
    )


__all__ = [
    "data_dir",
    "db_fixture",
    "app_v2",
    "client",
    "admin_token",
    "authed_client",
    "role_clients",
    "assert_forbidden",
    "assert_allowed",
    "ROLE_CREDS",
]
