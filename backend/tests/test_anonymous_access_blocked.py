"""
Regression test for A1-A5 — anonymous access and privilege escalation.

Before Week 1 Track S: all these tests FAIL (server returns 200 to
unauth and lets viewers POST).
After Week 1 Track S: all must pass.

This test file exists to catch any regression where a new router is
added without `Depends(require_auth)` or without role guards on its
mutations.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client, role_clients,
)


# ─────────────────────────────────────────────────────────────────────────
# Regression: every protected endpoint must 401 for anonymous callers
# ─────────────────────────────────────────────────────────────────────────

READS_MUST_BE_AUTHED = [
    "/api/workflows",
    "/api/projects",
    "/api/connections",
    "/api/schedules",
    "/api/alerts/rules",
    "/api/alerts/logs",
    "/api/monitor/executions",
    "/api/variables",
    "/api/credentials",
    "/api/plus/audit/events",
]

MUTATIONS_MUST_BE_AUTHED = [
    ("POST", "/api/workflows", {"name": "x", "steps": []}),
    ("POST", "/api/projects", {"id": "p1", "name": "p"}),
    ("POST", "/api/connections", {"name": "c", "type": "postgres"}),
    ("POST", "/api/schedules", {"workflow_id": "wf", "cron": "* * * * *"}),
    ("POST", "/api/alerts/rules", {"name": "a", "workflow_id": "wf"}),
    ("POST", "/api/variables", {"key": "K", "value": "v", "scope": "global"}),
    ("POST", "/api/credentials", {"name": "c", "type": "pg"}),
]


@pytest.mark.parametrize("path", READS_MUST_BE_AUTHED)
def test_anonymous_read_rejected(client, path):
    r = client.get(path)
    assert r.status_code in (401, 403), (
        f"SECURITY REGRESSION — anonymous GET {path} returned {r.status_code}. "
        f"Expected 401/403. This is A1-A4 regressing."
    )


@pytest.mark.parametrize("method,path,body", MUTATIONS_MUST_BE_AUTHED)
def test_anonymous_mutation_rejected(client, method, path, body):
    r = client.request(method, path, json=body)
    assert r.status_code in (401, 403), (
        f"SECURITY REGRESSION — anonymous {method} {path} returned {r.status_code}. "
        f"Expected 401/403. This is A1-A3 regressing."
    )


# ─────────────────────────────────────────────────────────────────────────
# Regression: viewer / analyst cannot mutate
# ─────────────────────────────────────────────────────────────────────────

VIEWER_CANNOT_POST = [
    ("/api/workflows", {"name": "x", "steps": []}),
    ("/api/variables", {"key": "K", "value": "v", "scope": "global"}),
    ("/api/alerts/rules", {"name": "a", "workflow_id": "wf"}),
    ("/api/schedules", {"workflow_id": "wf", "cron": "* * * * *"}),
    ("/api/credentials", {"name": "c", "type": "pg"}),
]


@pytest.mark.parametrize("path,body", VIEWER_CANNOT_POST)
def test_viewer_cannot_mutate(role_clients, path, body):
    viewer = role_clients.get("viewer")
    if viewer is None:
        pytest.skip("viewer role not provisioned")
    r = viewer.post(path, json=body)
    assert r.status_code in (401, 403), (
        f"PRIVILEGE ESCALATION — viewer POST {path} returned {r.status_code}. "
        f"Expected 401/403. This is A5 regressing."
    )


@pytest.mark.parametrize("path,body", VIEWER_CANNOT_POST)
def test_analyst_cannot_mutate(role_clients, path, body):
    analyst = role_clients.get("analyst")
    if analyst is None:
        pytest.skip("analyst role not provisioned")
    r = analyst.post(path, json=body)
    assert r.status_code in (401, 403), (
        f"PRIVILEGE ESCALATION — analyst POST {path} returned {r.status_code}. "
        f"Expected 401/403."
    )


# ─────────────────────────────────────────────────────────────────────────
# Regression: privileged execute + gateway surfaces reject anonymous callers
#
# These were previously reachable with no auth (only a workspace dep that
# falls back to "default" for tokenless callers), which let an anonymous
# caller mint execute-scoped API keys, publish arbitrary workflows as public
# endpoints, and run pipelines. Every entry here must 401/403 for anon.
# ─────────────────────────────────────────────────────────────────────────

PRIVILEGED_ANON_MUST_REJECT = [
    ("GET", "/api/gateway/keys", None),
    ("GET", "/api/gateway/endpoints", None),
    ("POST", "/api/gateway/keys", {"name": "k"}),
    ("POST", "/api/gateway/endpoints", {"workflow_id": "wf", "path": "p"}),
    ("POST", "/api/execute/workflow/ephemeral", {"workflow": {"id": "x", "steps": []}}),
    ("POST", "/api/execute/workflow/anon-probe", {}),
]


@pytest.mark.parametrize("method,path,body", PRIVILEGED_ANON_MUST_REJECT)
def test_anonymous_execute_and_gateway_rejected(client, method, path, body):
    r = client.request(method, path, json=body) if body is not None else client.request(method, path)
    assert r.status_code in (401, 403), (
        f"SECURITY REGRESSION — anonymous {method} {path} returned {r.status_code}. "
        f"Expected 401/403. Execute/gateway management must never be anonymous."
    )


# ─────────────────────────────────────────────────────────────────────────
# Regression: public endpoints remain public
# ─────────────────────────────────────────────────────────────────────────

PUBLIC_PATHS = ["/api/health", "/docs", "/openapi.json"]


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_public_endpoints_still_public(client, path):
    r = client.get(path)
    # 200 for health, 200/307 for docs, 200 for openapi
    assert r.status_code in (200, 307), (
        f"Public endpoint {path} now requires auth (status {r.status_code}) — "
        f"over-guard regression."
    )


# ─────────────────────────────────────────────────────────────────────────
# Regression: super_admin can still do everything
# ─────────────────────────────────────────────────────────────────────────

def test_super_admin_can_read_all(authed_client):
    for path in READS_MUST_BE_AUTHED:
        r = authed_client.get(path)
        # super_admin authed: no 401/403 ever
        assert r.status_code not in (401, 403), (
            f"AUTH REGRESSION — super_admin GET {path} returned {r.status_code}"
        )


def test_data_engineer_can_create_workflow(role_clients):
    de = role_clients.get("data_engineer")
    if de is None:
        pytest.skip("data_engineer not provisioned")
    r = de.post("/api/workflows", json={"name": "de-test", "steps": []})
    assert r.status_code not in (401, 403), (
        f"data_engineer should create workflows, got {r.status_code}"
    )
