"""Phase 6 — BFF dual-auth token extraction + CSRF double-submit guard.

Dual-auth: bearer (CLI/service/programmatic) OR the HttpOnly session cookie
(browser). CSRF is enforced ONLY for cookie-authed state-changing requests;
bearer callers are exempt (not cross-site attackable).
"""
import pytest
from fastapi import HTTPException

from fpulse.auth import deps


class _Req:
    def __init__(self, method="POST", headers=None, cookies=None):
        self.method = method
        self.headers = headers or {}
        self.cookies = cookies or {}


def test_extract_token_bearer():
    assert deps._extract_token(_Req(headers={"Authorization": "Bearer abc123"})) == "abc123"


def test_extract_token_cookie():
    assert deps._extract_token(_Req(cookies={"fpulse_session": "cook123"})) == "cook123"


def test_extract_token_bearer_wins_over_cookie():
    r = _Req(headers={"Authorization": "Bearer hdr"}, cookies={"fpulse_session": "cook"})
    assert deps._extract_token(r) == "hdr"


def test_extract_token_none():
    assert deps._extract_token(_Req()) == ""


def test_csrf_bearer_is_exempt():
    deps.csrf_protect(_Req(headers={"Authorization": "Bearer x"}, cookies={"fpulse_session": "s"}))


def test_csrf_get_is_exempt():
    deps.csrf_protect(_Req(method="GET", cookies={"fpulse_session": "s", "fpulse_csrf": "t"}))


def test_csrf_no_cookie_session_is_exempt():
    deps.csrf_protect(_Req(method="POST"))  # anonymous / no cookie → nothing to protect


def test_csrf_cookie_mutation_missing_token_blocked():
    r = _Req(method="POST", cookies={"fpulse_session": "s", "fpulse_csrf": "t"})
    with pytest.raises(HTTPException) as ei:
        deps.csrf_protect(r)
    assert ei.value.status_code == 403


def test_csrf_cookie_mutation_wrong_token_blocked():
    r = _Req(method="POST", headers={"X-CSRF-Token": "WRONG"},
             cookies={"fpulse_session": "s", "fpulse_csrf": "t"})
    with pytest.raises(HTTPException):
        deps.csrf_protect(r)


def test_csrf_cookie_mutation_valid_token_ok():
    r = _Req(method="POST", headers={"X-CSRF-Token": "t"},
             cookies={"fpulse_session": "s", "fpulse_csrf": "t"})
    deps.csrf_protect(r)  # no raise
