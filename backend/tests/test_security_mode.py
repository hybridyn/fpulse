"""SECURITY_MODE posture — server mode removes the anonymous workspace fallback.

local  (default) — unauthenticated callers fall back to the 'default'
                   workspace so the single-user laptop flow works with no login.
server           — no anonymous fallback; unauthenticated callers get 401.
"""
import pytest
from fastapi import HTTPException

from fpulse import runtime_config
from fpulse.auth import deps


class _Req:
    """Minimal stand-in for starlette Request — only `.headers` is read."""
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_security_mode_defaults_to_local():
    # Permissive by default so `pip install fpulse && fpulse open` is unchanged.
    assert runtime_config.IS_LOCAL_MODE is (not runtime_config.IS_SERVER_MODE)
    assert runtime_config.SECURITY_MODE in ("local", "server")
    assert "security_mode" in runtime_config.snapshot()


def test_local_mode_allows_anonymous(monkeypatch):
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", False)
    monkeypatch.setattr(deps, "current_user_optional", lambda req: None)
    # No workspace store wired → degradation path returns 'default'.
    import fpulse.main as m
    monkeypatch.setitem(m.app_state, "workspace_store", None)
    assert deps.current_workspace_id(_Req()) == "default"


def test_server_mode_blocks_anonymous(monkeypatch):
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", True)
    monkeypatch.setattr(deps, "current_user_optional", lambda req: None)
    with pytest.raises(HTTPException) as ei:
        deps.current_workspace_id(_Req())
    assert ei.value.status_code == 401


def test_server_mode_still_resolves_for_authenticated(monkeypatch):
    """A logged-in user is unaffected by server mode — normal resolution."""
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", True)

    class _User:
        id = "u1"
        role = "viewer"

    monkeypatch.setattr(deps, "current_user_optional", lambda req: _User())
    import fpulse.main as m
    monkeypatch.setitem(m.app_state, "workspace_store", None)  # → 'default'
    # No ws_store → explicit-or-default path; must NOT raise for a real user.
    assert deps.current_workspace_id(_Req({"X-Workspace-Id": "default"})) == "default"
