"""Regression test for self-serve password change (2026-06-08).

The app-validation pass found that AccountPage was wired to the
Plus-only /api/plus/users/change-password path (404 in OSS), so every
self-serve password change silently failed. The fix points the UI at
POST /api/auth/me/password. This test pins that endpoint end-to-end so
the path can't regress silently again.

Loopback Host required by the DNS-rebinding middleware (see the e2e
fixtures) — TestClient base_url must be a loopback name.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


OLD = "OldPass!2026X"
NEW = "NewPass!2026Y"


@pytest.fixture()
def client():
    os.environ["FPULSE_DATA_DIR"] = tempfile.mkdtemp()
    from fpulse.main import app, app_state
    from fpulse.auth.models import User
    with TestClient(app, base_url="http://localhost") as c:
        admin = app_state["user_store"].get_user("admin")
        admin.password_hash = User.hash_password(OLD)
        app_state["user_store"]._save_user(admin)
        tok = c.post("/api/auth/login", json={
            "email": "admin@fpulse.local", "password": OLD,
        }).json()["token"]
        c.headers.update({"Authorization": f"Bearer {tok}"})
        yield c


class TestChangeMyPassword:
    def test_endpoint_exists_and_changes_password(self, client):
        # The route the UI now calls must exist + succeed (not 404).
        r = client.post("/api/auth/me/password", json={
            "current_password": OLD, "new_password": NEW,
        })
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"

    def test_new_password_works_old_fails(self, client):
        client.post("/api/auth/me/password", json={
            "current_password": OLD, "new_password": NEW,
        })
        # New password logs in
        good = client.post("/api/auth/login", json={
            "email": "admin@fpulse.local", "password": NEW,
        })
        assert good.status_code == 200
        # Old password rejected
        bad = client.post("/api/auth/login", json={
            "email": "admin@fpulse.local", "password": OLD,
        })
        assert bad.status_code == 401

    def test_wrong_current_password_rejected(self, client):
        r = client.post("/api/auth/me/password", json={
            "current_password": "WrongCurrent!9", "new_password": NEW,
        })
        assert r.status_code in (400, 401, 403)

    def test_plus_path_is_not_the_oss_route(self, client):
        # Guard against the regression: the Plus path must NOT be what
        # self-serve relies on (it 404s / is unavailable in OSS).
        r = client.post("/api/plus/users/change-password", json={
            "current_password": OLD, "new_password": NEW,
        })
        assert r.status_code != 200, (
            "The Plus change-password path responded 200 in OSS — if this "
            "ever becomes the wired path again, self-serve breaks on real "
            "OSS installs where Plus routes are absent."
        )


class TestRegressionGuard:
    def test_account_page_uses_correct_method(self):
        # The UI must call changeMyOwnPassword (-> /auth/me/password),
        # not the legacy changeMyPassword (-> /plus/users/change-password).
        from pathlib import Path
        ap = (Path(__file__).resolve().parents[1].parent
              / "frontend" / "src" / "components" / "pages" / "AccountPage.tsx")
        if ap.exists():
            src = ap.read_text(encoding="utf-8")
            assert "changeMyOwnPassword" in src, (
                "AccountPage must call api.changeMyOwnPassword (the "
                "/auth/me/password endpoint), not the Plus-only legacy method."
            )
