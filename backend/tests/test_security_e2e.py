"""Direct FastAPI-client proof of the OSS hardening — real routes + the CSRF
middleware, not just helpers. Server mode is simulated by monkeypatching
runtime_config.IS_SERVER_MODE (the code reads it dynamically per request).

All assertions are rejection-based (401/403 before the handler), so they don't
mutate the app's data dir.
"""
import pytest
from fastapi.testclient import TestClient

from fpulse import runtime_config
from fpulse.main import app


@pytest.fixture
def client():
    # base_url host must be loopback ('localhost') to pass the
    # LocalOriginGuard middleware (which rejects non-loopback Host headers in
    # loopback mode, before auth runs).
    with TestClient(app, base_url="http://localhost") as c:
        yield c


def _is_csrf_403(r) -> bool:
    if r.status_code != 403:
        return False
    try:
        return "csrf" in (r.json().get("detail", "") or "").lower()
    except Exception:
        return False


# ── CSRF double-submit middleware ──

def test_cookie_post_without_csrf_token_is_blocked(client):
    r = client.post("/api/executions/backfill",
                    json={"pipeline_id": "p", "start_date": "2026-01-01", "end_date": "2026-01-02"},
                    cookies={"fpulse_session": "sess"})
    assert _is_csrf_403(r), f"expected CSRF 403, got {r.status_code}"


def test_cookie_post_with_matching_csrf_passes_the_guard(client):
    r = client.post("/api/executions/backfill",
                    json={"pipeline_id": "p", "start_date": "2026-01-01", "end_date": "2026-01-02"},
                    headers={"X-CSRF-Token": "tok"},
                    cookies={"fpulse_session": "sess", "fpulse_csrf": "tok"})
    assert not _is_csrf_403(r)  # guard passed; route may 401/validate, not CSRF-403


def test_bearer_post_is_csrf_exempt(client):
    r = client.post("/api/executions/backfill",
                    json={"pipeline_id": "p", "start_date": "2026-01-01", "end_date": "2026-01-02"},
                    headers={"Authorization": "Bearer invalid"},
                    cookies={"fpulse_session": "sess"})
    assert not _is_csrf_403(r)  # bearer is never CSRF-checked


# ── Server-mode anonymous blocks ──

def test_anonymous_upload_rejected_in_server_mode(client, monkeypatch):
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", True)
    r = client.post("/api/uploads/file", files={"file": ("t.csv", b"a\n1\n", "text/csv")})
    assert r.status_code == 401, f"expected 401, got {r.status_code}"


def test_anonymous_backfill_rejected_in_server_mode(client, monkeypatch):
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", True)
    r = client.post("/api/executions/backfill",
                    json={"pipeline_id": "p", "start_date": "2026-01-01", "end_date": "2026-01-02"})
    assert r.status_code == 401, f"expected 401, got {r.status_code}"


def test_anonymous_ai_execution_rejected_in_server_mode(client, monkeypatch):
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", True)
    r = client.post("/api/ai/agent/action", json={
        "action": {"kind": "fast_action", "verb": "run", "entity_id": "p1", "entity_name": "Pipe"},
        "page_context": {"page": "pipelines", "environment": "dev",
                          "selected_ids": [], "visible_ids": [], "visible_items": []},
        "dialogue_state": {},
    })
    assert r.status_code == 401, f"expected 401, got {r.status_code}"
