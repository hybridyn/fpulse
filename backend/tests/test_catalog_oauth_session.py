"""Tests for OAuthSession — the lazy-refresh auth wrapper.

We don't hit a real OAuth server. We patch `requests` at the module
level so every refresh POST and resource GET is observable. The
contract under test:

  1. Static token (no refresh_token) → never refreshes.
  2. Expired-with-buffer → refreshes BEFORE sending the request.
  3. 401 response → single retry with refreshed token.
  4. Successful refresh → writeback to credential store.
  5. Refresh failure on 401 → return original 401, don't loop.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from fpulse.connections import oauth_session as oauth_mod
from fpulse.connections.oauth_session import OAuthSession, REFRESH_BUFFER_S


def _mk_session(creds_in: dict, written_to: dict | None = None):
    """Wire an OAuthSession around a dict-backed credential store."""
    state = {"creds": dict(creds_in)}

    def _get():
        return dict(state["creds"])

    def _put(new):
        state["creds"] = dict(new)
        if written_to is not None:
            written_to.update(new)

    return OAuthSession(_get, _put), state


def test_static_token_never_refreshes():
    """No refresh_token in creds → no refresh attempted, even if expired."""
    creds = {"access_token": "abc", "expires_at": 0}  # expired but no refresh path
    sess, _ = _mk_session(creds)
    fake_resp = MagicMock(status_code=200)
    sess._session = MagicMock()
    sess._session.request.return_value = fake_resp
    # Patch out the refresh path entirely — if it's called, the test fails.
    with patch.object(sess, "_refresh", side_effect=AssertionError("must not refresh")):
        r = sess.get("https://api/x")
    assert r is fake_resp
    sess._session.request.assert_called_once()


def test_expired_within_buffer_triggers_refresh_before_request():
    """If expires_at is within REFRESH_BUFFER_S, refresh happens BEFORE the GET."""
    written: dict = {}
    creds = {
        "access_token": "old",
        "refresh_token": "rt-1",
        "client_id": "cid",
        "client_secret": "csec",
        "token_uri": "https://idp/token",
        "expires_at": time.time() + REFRESH_BUFFER_S - 10,  # within buffer → "expired"
    }
    sess, state = _mk_session(creds, written_to=written)
    # First call (refresh POST): returns new tokens.
    refresh_resp = MagicMock(status_code=200)
    refresh_resp.json.return_value = {
        "access_token": "new", "expires_in": 3600,
    }
    refresh_resp.raise_for_status = lambda: None
    # Resource call: returns 200 with the new bearer.
    resource_resp = MagicMock(status_code=200)
    sess._session = MagicMock()
    sess._session.request.return_value = resource_resp
    sess._requests = MagicMock()
    sess._requests.post.return_value = refresh_resp

    r = sess.get("https://api/x")

    assert r is resource_resp
    # Refresh POST happened first.
    sess._requests.post.assert_called_once()
    # Resource call carried the NEW token, not "old".
    sent_headers = sess._session.request.call_args.kwargs["headers"]
    assert sent_headers["Authorization"] == "Bearer new"
    # Writeback persisted the new token.
    assert written["access_token"] == "new"
    assert written["expires_at"] > time.time() + 3000


def test_unexpired_token_does_not_refresh():
    creds = {
        "access_token": "fresh",
        "refresh_token": "rt-1",
        "client_id": "cid",
        "token_uri": "https://idp/token",
        "expires_at": time.time() + 3600,  # well past the buffer
    }
    sess, _ = _mk_session(creds)
    sess._session = MagicMock()
    sess._session.request.return_value = MagicMock(status_code=200)
    sess._requests = MagicMock()

    sess.get("https://api/x")

    # Refresh should NOT be called because expiry is far future.
    sess._requests.post.assert_not_called()
    sent_headers = sess._session.request.call_args.kwargs["headers"]
    assert sent_headers["Authorization"] == "Bearer fresh"


def test_401_triggers_single_refresh_and_retry():
    """Server says token is bad even though we thought it was fresh.
    OAuthSession should refresh once and retry — exactly once."""
    creds = {
        "access_token": "stale",
        "refresh_token": "rt-1",
        "client_id": "cid",
        "token_uri": "https://idp/token",
        "expires_at": time.time() + 3600,  # we *think* it's fresh
    }
    sess, _ = _mk_session(creds)

    first = MagicMock(status_code=401)
    second = MagicMock(status_code=200)
    sess._session = MagicMock()
    sess._session.request.side_effect = [first, second]

    refresh_resp = MagicMock(status_code=200)
    refresh_resp.json.return_value = {"access_token": "new2", "expires_in": 3600}
    refresh_resp.raise_for_status = lambda: None
    sess._requests = MagicMock()
    sess._requests.post.return_value = refresh_resp

    r = sess.get("https://api/x")

    assert r is second
    assert sess._session.request.call_count == 2
    # Refresh attempted once; retry used the new token.
    sess._requests.post.assert_called_once()
    retry_headers = sess._session.request.call_args_list[1].kwargs["headers"]
    assert retry_headers["Authorization"] == "Bearer new2"


def test_401_with_failed_refresh_returns_original_401():
    """If the refresh attempt itself fails, we don't loop — return the 401."""
    creds = {
        "access_token": "stale", "refresh_token": "rt-1",
        "client_id": "cid", "token_uri": "https://idp/token",
        "expires_at": time.time() + 3600,
    }
    sess, _ = _mk_session(creds)

    first = MagicMock(status_code=401)
    sess._session = MagicMock()
    sess._session.request.return_value = first

    sess._requests = MagicMock()
    sess._requests.post.side_effect = RuntimeError("idp down")

    r = sess.get("https://api/x")

    assert r is first
    # Exactly one resource call, exactly one refresh attempt — no loop.
    assert sess._session.request.call_count == 1
    sess._requests.post.assert_called_once()


def test_refresh_rotates_refresh_token_when_idp_returns_one():
    """Some providers rotate the refresh_token on every refresh.
    We must persist the new one or the next refresh will fail."""
    written: dict = {}
    creds = {
        "access_token": "old", "refresh_token": "rt-1",
        "client_id": "cid", "token_uri": "https://idp/token",
        "expires_at": 0,  # forces refresh
    }
    sess, _ = _mk_session(creds, written_to=written)
    refresh_resp = MagicMock(status_code=200)
    refresh_resp.json.return_value = {
        "access_token": "new", "refresh_token": "rt-2", "expires_in": 3600,
    }
    refresh_resp.raise_for_status = lambda: None
    sess._session = MagicMock()
    sess._session.request.return_value = MagicMock(status_code=200)
    sess._requests = MagicMock()
    sess._requests.post.return_value = refresh_resp

    sess.get("https://api/x")

    assert written["refresh_token"] == "rt-2"
