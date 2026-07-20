"""
Tests for SecurityHeadersMiddleware (2026-05-06).

Pins the headers vulnerability scanners (Nessus, ZAP, Qualys) check for.
If any of these assertions fail, a downstream Nessus run will regress.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fpulse.api.security_headers import SecurityHeadersMiddleware


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    return TestClient(app)


def test_baseline_headers_present(client):
    r = client.get("/ping")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in r.headers["permissions-policy"]
    assert r.headers["cross-origin-opener-policy"] == "same-origin"
    assert r.headers["x-xss-protection"] == "0"
    assert "default-src" in r.headers["content-security-policy"]
    assert r.headers["server"] == "fpulse"


def test_hsts_only_on_https(client):
    r = client.get("/ping")
    assert "strict-transport-security" not in r.headers


def test_hsts_on_forwarded_https(client):
    r = client.get("/ping", headers={"X-Forwarded-Proto": "https"})
    assert "max-age=" in r.headers["strict-transport-security"]
    assert "includeSubDomains" in r.headers["strict-transport-security"]


def test_x_powered_by_stripped():
    app = FastAPI()

    @app.middleware("http")
    async def add_powered_by(request, call_next):
        resp = await call_next(request)
        resp.headers["X-Powered-By"] = "Express/4.0"
        return resp

    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    r = TestClient(app).get("/ping")
    assert "x-powered-by" not in {k.lower() for k in r.headers.keys()}


def test_disable_via_env(monkeypatch):
    monkeypatch.setenv("FPULSE_DISABLE_SECURITY_HEADERS", "1")
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    r = TestClient(app).get("/ping")
    assert "x-frame-options" not in r.headers


def test_csp_override_env(monkeypatch):
    monkeypatch.setenv("FPULSE_CSP", "default-src 'none'")
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    r = TestClient(app).get("/ping")
    assert r.headers["content-security-policy"] == "default-src 'none'"
