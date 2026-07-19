"""Tests for the 2026-06-02 OSS-local hardening middleware.

Covers:
  * Bind resolution honours FPULSE_BIND_HOST + FPULSE_ALLOW_LAN
  * /api/health/bind-info reports the right loopback / warning state
  * LocalOriginGuardMiddleware: Host header allowlist (primary defense)
  * LocalOriginGuardMiddleware: Origin/Referer pinning (secondary)
  * assert_dev_auth_local_only raises 403 for non-loopback callers
    even if no real dev-bypass is currently wired (regression-pins
    the contract so any future bypass MUST call the guard)
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI, Request, HTTPException
from fastapi.testclient import TestClient

from fpulse.api.local_hardening import (
    LocalOriginGuardMiddleware,
    _backend_bound_loopback_only,
    assert_dev_auth_local_only,
    bind_info,
    router as local_router,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_bind_env(monkeypatch):
    """Strip bind-env vars before each test so default = loopback."""
    for var in ("FPULSE_BIND_HOST", "FPULSE_ALLOW_LAN", "FPULSE_RESOLVED_BIND_HOST"):
        monkeypatch.delenv(var, raising=False)
    yield


def _make_app() -> FastAPI:
    """Build a minimal FastAPI app that exercises the middleware."""
    app = FastAPI()
    app.add_middleware(LocalOriginGuardMiddleware)
    app.include_router(local_router, prefix="/api")

    @app.post("/api/echo")
    async def echo():
        return {"ok": True}

    @app.get("/api/echo")
    async def echo_get():
        return {"ok": True}

    return app


# ── Bind resolution ───────────────────────────────────────────────────────


def test_default_resolves_loopback_only():
    """No env set → backend treated as loopback-bound."""
    assert _backend_bound_loopback_only() is True


def test_allow_lan_flips_loopback_off(monkeypatch):
    monkeypatch.setenv("FPULSE_ALLOW_LAN", "1")
    assert _backend_bound_loopback_only() is False


def test_explicit_bind_host_overrides_allow_lan(monkeypatch):
    # FPULSE_BIND_HOST is the explicit override; even with ALLOW_LAN set,
    # naming a loopback host wins.
    monkeypatch.setenv("FPULSE_ALLOW_LAN", "1")
    monkeypatch.setenv("FPULSE_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("FPULSE_RESOLVED_BIND_HOST", "127.0.0.1")
    assert _backend_bound_loopback_only() is True


def test_resolved_bind_host_is_authoritative(monkeypatch):
    """Once the launcher writes FPULSE_RESOLVED_BIND_HOST, middleware reads that."""
    monkeypatch.setenv("FPULSE_RESOLVED_BIND_HOST", "0.0.0.0")
    assert _backend_bound_loopback_only() is False


# ── /api/health/bind-info ────────────────────────────────────────────────


def test_bind_info_default_safe():
    info = bind_info()
    assert info["loopback_only"] is True
    assert info["warning"] is None
    assert info["allow_lan_flag"] is False


def test_bind_info_warns_on_lan_binding(monkeypatch):
    monkeypatch.setenv("FPULSE_ALLOW_LAN", "1")
    info = bind_info()
    assert info["loopback_only"] is False
    assert info["allow_lan_flag"] is True
    assert info["warning"] is not None
    assert "127.0.0.1" in info["warning"]  # tells the user how to fix it


# ── Host header allowlist (primary DNS-rebinding defense) ────────────────


def test_host_localhost_allowed():
    client = TestClient(_make_app())
    r = client.post("/api/echo", headers={"Host": "localhost:8001"})
    assert r.status_code == 200


def test_host_127_allowed():
    client = TestClient(_make_app())
    r = client.post("/api/echo", headers={"Host": "127.0.0.1:8001"})
    assert r.status_code == 200


def test_host_attacker_domain_blocked():
    """DNS-rebinding simulation: attacker.com resolved to 127.0.0.1
    by short-TTL DNS trickery → browser sends Host: attacker.com."""
    client = TestClient(_make_app())
    r = client.post("/api/echo", headers={"Host": "attacker.example.com"})
    assert r.status_code == 403
    assert r.json()["error"] == "non_loopback_host_blocked"


def test_host_check_skipped_on_lan_binding(monkeypatch):
    """LAN-bind install gets normal CORS handling; this guard goes away."""
    monkeypatch.setenv("FPULSE_RESOLVED_BIND_HOST", "0.0.0.0")
    client = TestClient(_make_app())
    r = client.post("/api/echo", headers={"Host": "anything.example.com"})
    assert r.status_code == 200


def test_health_endpoints_bypass_host_check():
    """Uptime probes must not be blocked even from external monitoring."""
    client = TestClient(_make_app())
    r = client.get("/api/health/bind-info", headers={"Host": "monitor.example.com"})
    assert r.status_code == 200


# ── Origin / Referer pinning (secondary defense) ─────────────────────────


def test_origin_loopback_allowed():
    client = TestClient(_make_app())
    r = client.post(
        "/api/echo",
        headers={"Host": "127.0.0.1:8001", "Origin": "http://localhost:5173"},
    )
    assert r.status_code == 200


def test_origin_non_loopback_blocked():
    client = TestClient(_make_app())
    r = client.post(
        "/api/echo",
        headers={"Host": "127.0.0.1:8001", "Origin": "https://attacker.example.com"},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "cross_origin_blocked"


def test_no_origin_allowed_for_curl():
    """curl / server-to-self calls without Origin must keep working."""
    client = TestClient(_make_app())
    r = client.post("/api/echo", headers={"Host": "127.0.0.1:8001"})
    assert r.status_code == 200


def test_referer_non_loopback_blocked():
    client = TestClient(_make_app())
    r = client.post(
        "/api/echo",
        headers={"Host": "127.0.0.1:8001", "Referer": "https://attacker.example.com/x"},
    )
    assert r.status_code == 403


# ── Dev-auth bypass guard ────────────────────────────────────────────────
#
# 2026-06-02: there is currently NO dev-auth bypass wired into the
# codebase. This guard is preventive — any future bypass (env-var
# toggle, debug header, etc.) MUST call assert_dev_auth_local_only()
# before honouring the bypass. These tests pin that contract so a
# regression that adds a bypass without the guard is caught
# immediately.


def test_dev_auth_guard_allows_loopback_caller():
    """Localhost caller is allowed through — guard returns silently."""
    request = _fake_request(client_host="127.0.0.1")
    assert_dev_auth_local_only(request)  # no raise


def test_dev_auth_guard_blocks_lan_caller():
    """Non-loopback caller is rejected with 403 even if the future
    bypass code path tried to engage."""
    request = _fake_request(client_host="10.0.0.5")
    with pytest.raises(HTTPException) as exc:
        assert_dev_auth_local_only(request)
    assert exc.value.status_code == 403


# ── Helpers ──────────────────────────────────────────────────────────────


def _fake_request(client_host: str) -> Request:
    """Build a minimal Request with a controllable client.host."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "client": (client_host, 12345),
        "query_string": b"",
    }
    return Request(scope)
