"""API docs (/docs, /redoc) must be fully self-hosted — no external CDN.

Regression guard for the 2026-06-10 fix. FastAPI's stock ``/docs`` pulls
swagger-ui from ``cdn.jsdelivr.net``, which Kaspersky Web Anti-Virus (and
any air-gapped / firewalled host) returns 503 for — leaving a blank page
with ``SwaggerUIBundle is not defined``. We vendor the assets and serve
them same-origin via :mod:`fpulse.docs_static`. These tests assert the
rendered HTML references only same-origin URLs and that the vendored
bundles are actually served.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fpulse.docs_static import (
    MOUNT_PATH,
    assets_present,
    mount_self_hosted_docs,
)


def _client() -> TestClient:
    # A throwaway app with no lifespan — we only exercise the docs routing,
    # which keeps this test fast and independent of the full app/DB boot.
    app = FastAPI(title="F-Pulse")
    mount_self_hosted_docs(app)
    return TestClient(app)


def test_vendored_assets_present():
    assert assets_present(), "vendored swagger-ui-bundle.js is missing"


def test_swagger_html_is_same_origin():
    html = _client().get("/docs").text
    assert f"{MOUNT_PATH}/swagger-ui-bundle.js" in html
    assert f"{MOUNT_PATH}/swagger-ui.css" in html
    # The whole point of the fix: zero external network dependency.
    assert "cdn.jsdelivr.net" not in html
    assert "fastapi.tiangolo.com" not in html


def test_redoc_html_is_same_origin():
    html = _client().get("/redoc").text
    assert f"{MOUNT_PATH}/redoc.standalone.js" in html
    assert "cdn.jsdelivr.net" not in html
    # with_google_fonts=False -> no fonts.googleapis.com fetch.
    assert "fonts.googleapis.com" not in html


def test_vendored_bundles_are_served():
    client = _client()
    for name in (
        "swagger-ui-bundle.js",
        "swagger-ui.css",
        "redoc.standalone.js",
        "favicon.png",
    ):
        resp = client.get(f"{MOUNT_PATH}/{name}")
        assert resp.status_code == 200, f"{name} -> {resp.status_code}"


def test_docs_and_redoc_return_200():
    client = _client()
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
