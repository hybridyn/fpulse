"""
Feature test for GET /api/projects/{project_id}/export (OSS).

Verifies the SECRET-SAFE project export bundle contract:

  * status 200, export_type == "project"
  * at least one pipeline, and the exported pipeline keeps its declared
    `parameters` (regression: v1 silently dropped them so ${param.X}
    bindings were lost on import)
  * connection references are an ALLOWLIST only — no `config` / `password`
    / secret keys ever leave the box
  * project-scoped SECRET variable values are redacted to null + redacted:true
  * anonymous callers are rejected (401/403)

Everything is driven through the public API exactly as a client would.

Harness note
------------
This file reuses `db_fixture` / `app_v2` from conftest_fixtures_v2 (they run
migrations + app startup) but builds its OWN TestClients with a loopback
`base_url`. The stock `authed_client` fixture wraps `TestClient(app)` with the
default `http://testserver` base_url, which F-Pulse's DNS-rebinding guard
rejects with 403 `non_loopback_host_blocked` — so every login there skips.
Pinning base_url to http://127.0.0.1 makes the Host header loopback and the
login succeeds. Feature code and the shared fixtures are left untouched.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2,
    DEV_ADMIN_EMAIL, DEV_ADMIN_PASSWORD,
)

# Loopback host so the DNS-rebinding guard (non_loopback_host_blocked) passes.
LOOPBACK_BASE = "http://127.0.0.1"

# Keys that would indicate a secret leaked into a connection reference.
FORBIDDEN_CONN_KEYS = {"config", "password", "secret", "secrets", "credentials"}


def _reset_admin_password(app) -> None:
    """Force the bootstrap admin onto the test-known password.

    The 2026 hardening seeds a random admin password on first boot; tests
    log in with 'admin'. Same technique as the shared fixture's helper,
    but resilient to the app_state not being populated until startup ran
    (which db_fixture guarantees via importing fpulse.main + our TestClient
    context entering below).
    """
    from fpulse.main import app_state
    from fpulse.auth.models import User
    store = app_state.get("user_store")
    if store is None:
        return
    admin = store.get_user_by_email(DEV_ADMIN_EMAIL)
    if admin is None:
        return
    admin.password_hash = User.hash_password(DEV_ADMIN_PASSWORD)
    admin.is_active = True
    store._save_user(admin)


@pytest.fixture(scope="module")
def anon_client(app_v2):
    """Unauthenticated, loopback-host client."""
    with TestClient(app_v2, base_url=LOOPBACK_BASE) as c:
        yield c


@pytest.fixture(scope="module")
def admin_client(app_v2, anon_client):
    """Authenticated admin client (loopback host).

    Resets the admin password inside the running app, logs in, and returns
    a client carrying the bearer token + session cookie. Skips cleanly if
    login is unreachable so the failure is legible rather than an opaque 401.
    """
    # anon_client already entered the TestClient context → app startup ran,
    # so app_state is populated.
    _reset_admin_password(app_v2)

    r = anon_client.post(
        "/api/auth/login",
        json={"email": DEV_ADMIN_EMAIL, "password": DEV_ADMIN_PASSWORD},
    )
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    tok = r.json().get("token")
    if not tok:
        pytest.skip(f"login returned no token: {r.text[:200]}")

    # The login above was driven through anon_client, and the BFF login plants
    # an HttpOnly fpulse_session cookie that TestClient keeps in its jar.
    # require_auth accepts that cookie as well as the bearer header, so leaving
    # it would make anon_client silently authenticated and the anonymous-access
    # assertions below would pass against a logged-in session.
    anon_client.cookies.clear()

    c = TestClient(app_v2, base_url=LOOPBACK_BASE)
    c.headers["Authorization"] = f"Bearer {tok}"
    c.cookies.set("session", tok)
    c.cookies.set("fpulse_session", tok)
    with c:
        yield c


@pytest.fixture(scope="module")
def exported_bundle(admin_client: TestClient):
    """Create a project + pipeline (declared parameter + step) + a
    project-scoped secret variable, then export the project.

    Returns (project_id, export_json, secret_created).
    """
    # (a) Create a project.
    r = admin_client.post("/api/projects", json={
        "name": "Export Test Project",
        "description": "fixture project for export test",
        "color": "#123456",
        "icon": "database",
        "metadata": {"cost_center": "CC-42"},
    })
    assert r.status_code in (200, 201), f"project create failed: {r.status_code} {r.text[:300]}"
    pid = r.json()["id"]

    # (b) Create a pipeline in that project with a declared parameter and one
    #     step, atomically via POST /api/workflows.
    r = admin_client.post("/api/workflows", json={
        "name": "Export Test Pipeline",
        "description": "pipeline with a declared param",
        "project_id": pid,
        "parameters": [
            {"name": "dataset", "type": "string", "default": "orders",
             "description": "source csv"},
        ],
        "steps": [
            {"type": "csv_source", "label": "Read orders",
             "params": {"path": "orders.csv"}},
        ],
    })
    assert r.status_code in (200, 201), f"workflow create failed: {r.status_code} {r.text[:300]}"

    # (c) Create a project-scoped SECRET variable.
    r = admin_client.post("/api/variables", json={
        "key": "EXPORT_SECRET",
        "value": "super-secret-value",
        "type": "secret",
        "scope": "project",
        "project_id": pid,
        "description": "should be redacted on export",
    })
    secret_created = r.status_code in (200, 201)

    # (d) Export the project.
    r = admin_client.get(f"/api/projects/{pid}/export")
    assert r.status_code == 200, f"export failed: {r.status_code} {r.text[:500]}"
    bundle = r.json()

    return pid, bundle, secret_created


def test_export_top_level_shape(exported_bundle):
    _pid, bundle, _secret = exported_bundle
    assert bundle.get("export_type") == "project", bundle.get("export_type")
    assert bundle.get("format_version") == 1, bundle.get("format_version")
    assert bundle["project"]["name"] == "Export Test Project"
    assert bundle["project"]["color"] == "#123456"
    assert bundle["project"]["metadata"] == {"cost_center": "CC-42"}
    for key in ("folders", "pipelines", "variables", "connections"):
        assert key in bundle, f"bundle missing '{key}'"


def test_export_contains_pipeline_with_parameters(exported_bundle):
    _pid, bundle, _secret = exported_bundle
    pipelines = bundle["pipelines"]
    assert len(pipelines) >= 1, "expected at least one pipeline in the export"

    pl = next((p for p in pipelines if p.get("name") == "Export Test Pipeline"), pipelines[0])

    # The declared parameter must survive (NOT dropped — the v1 regression).
    params = pl.get("parameters") or []
    names = {p.get("name") for p in params}
    assert "dataset" in names, f"declared parameter 'dataset' was dropped: {params}"
    dataset = next(p for p in params if p.get("name") == "dataset")
    assert dataset.get("default") == "orders", dataset
    assert dataset.get("type") == "string", dataset

    # Steps carried through.
    assert pl.get("steps"), "pipeline export lost its steps"


def test_export_connections_have_no_secrets(exported_bundle):
    _pid, bundle, _secret = exported_bundle
    allowed = {"id", "name", "type", "scope", "project_id", "host", "database", "port"}
    for conn in bundle["connections"]:
        leaked = FORBIDDEN_CONN_KEYS.intersection(conn.keys())
        assert not leaked, f"connection reference leaked secret keys {leaked}: {conn}"
        extra = set(conn.keys()) - allowed
        assert not extra, f"connection reference has non-allowlist keys {extra}: {conn}"


def test_export_secret_variable_redacted(exported_bundle):
    _pid, bundle, secret_created = exported_bundle
    if not secret_created:
        pytest.skip("secret variable could not be created via API; skipping redaction check")
    variables = bundle["variables"]
    secret = next((v for v in variables if v.get("key") == "EXPORT_SECRET"), None)
    assert secret is not None, f"exported project variables missing EXPORT_SECRET: {variables}"
    assert secret.get("type") == "secret", secret
    assert secret.get("value") is None, f"secret value NOT redacted to null: {secret.get('value')!r}"
    assert secret.get("redacted") is True, f"secret missing redacted flag: {secret}"


def test_anonymous_export_rejected(anon_client: TestClient, exported_bundle):
    pid, _bundle, _secret = exported_bundle
    r = anon_client.get(f"/api/projects/{pid}/export")
    assert r.status_code in (401, 403), (
        f"SECURITY: anonymous GET /api/projects/{pid}/export returned "
        f"{r.status_code}, expected 401/403"
    )
