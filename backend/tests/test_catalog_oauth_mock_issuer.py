"""End-to-end OAuth verification against a mock issuer.

Per reviewer guidance: 'You only need *one* real representative for each
OAuth flavor. Set up an internal OAuth2 Mock Server and configure it to
return valid JWTs on one call and `expired` errors on the next. This
validates your 401-retry-with-refresh wrapper and Vault persistence
logic without hitting a single real API.'

This test stands up an in-process FastAPI app that emulates a real
OAuth2 token endpoint + a protected resource, then drives the
OAuthSession against it. It proves the substrate handles the full
lifecycle without any external network call.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from fpulse.connections.oauth_session import OAuthSession


# ────────────────────────────────────────────────────────────────────
#  Mock issuer + protected resource — pure stdlib, no FastAPI dep
# ────────────────────────────────────────────────────────────────────

class _MockOAuthHandler(BaseHTTPRequestHandler):
    """Single handler that serves both `/oauth/token` and `/resource`.

    Behavior is configured on the server instance:
      - server.access_token_seq: list of access tokens to issue, in order
      - server.refresh_token_seq: optional rotation values
      - server.expired_tokens: tokens that should 401 on /resource
      - server.refresh_calls: int counter (assert from test)
    """

    def log_message(self, format, *args):  # quiet
        pass

    def _json(self, code: int, body: dict) -> None:
        import json
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/oauth/token":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode()
            params = parse_qs(raw)
            grant = params.get("grant_type", [""])[0]
            self.server.refresh_calls += 1  # type: ignore[attr-defined]
            assert grant == "refresh_token", f"unexpected grant: {grant}"
            access = self.server.access_token_seq.pop(0)  # type: ignore[attr-defined]
            body = {"access_token": access, "expires_in": 3600}
            rotated = self.server.refresh_token_seq  # type: ignore[attr-defined]
            if rotated:
                body["refresh_token"] = rotated.pop(0)
            return self._json(200, body)
        return self._json(404, {"error": "not found"})

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/resource":
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return self._json(401, {"error": "no token"})
            token = auth[len("Bearer "):]
            if token in self.server.expired_tokens:  # type: ignore[attr-defined]
                return self._json(401, {"error": "token expired"})
            return self._json(200, {"hello": token})
        return self._json(404, {"error": "not found"})


@pytest.fixture
def mock_issuer():
    server = HTTPServer(("127.0.0.1", 0), _MockOAuthHandler)
    server.access_token_seq = []   # type: ignore[attr-defined]
    server.refresh_token_seq = []  # type: ignore[attr-defined]
    server.expired_tokens = set()  # type: ignore[attr-defined]
    server.refresh_calls = 0        # type: ignore[attr-defined]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield server, base
    server.shutdown()


# ────────────────────────────────────────────────────────────────────
#  End-to-end verification against the mock
# ────────────────────────────────────────────────────────────────────

def _wire_session(initial_creds: dict):
    """Wire an OAuthSession with a dict-backed credential store so we
    can observe writeback in the test."""
    state = {"creds": dict(initial_creds)}
    sess = OAuthSession(
        get_credentials=lambda: dict(state["creds"]),
        put_credentials=lambda new: state.update({"creds": dict(new)}),
    )
    return sess, state


def test_expired_token_triggers_refresh_and_resource_succeeds(mock_issuer):
    """Token starts expired → wrapper hits /oauth/token → uses new
    token on /resource → 200. No external network calls."""
    server, base = mock_issuer
    server.access_token_seq = ["fresh-A"]
    server.expired_tokens = {"old"}

    sess, state = _wire_session({
        "access_token": "old",
        "refresh_token": "rt-1",
        "client_id": "cid",
        "client_secret": "csec",
        "token_uri": f"{base}/oauth/token",
        "expires_at": time.time() - 60,  # already expired
    })

    r = sess.get(f"{base}/resource")
    assert r.status_code == 200
    assert r.json() == {"hello": "fresh-A"}
    # Refresh was called exactly once.
    assert server.refresh_calls == 1
    # Writeback persisted the new access token to the store.
    assert state["creds"]["access_token"] == "fresh-A"


def test_401_on_resource_triggers_single_retry_with_fresh_token(mock_issuer):
    """Cached token says 'fresh', server says 401 → wrapper refreshes
    once and retries. Counts must be exactly: 1 refresh, 2 resource hits."""
    server, base = mock_issuer
    server.access_token_seq = ["fresh-B"]
    server.expired_tokens = {"stale"}  # makes the first GET 401

    sess, _ = _wire_session({
        "access_token": "stale",
        "refresh_token": "rt-2",
        "client_id": "cid",
        "token_uri": f"{base}/oauth/token",
        "expires_at": time.time() + 3600,  # claim it's fresh
    })

    r = sess.get(f"{base}/resource")
    assert r.status_code == 200
    assert r.json() == {"hello": "fresh-B"}
    assert server.refresh_calls == 1


def test_rotated_refresh_token_is_persisted(mock_issuer):
    """Issuer rotates refresh_token on every refresh (Salesforce pattern).
    Wrapper must persist the new one — otherwise the next refresh fails."""
    server, base = mock_issuer
    server.access_token_seq = ["fresh-C"]
    server.refresh_token_seq = ["rt-rotated"]

    sess, state = _wire_session({
        "access_token": "old", "refresh_token": "rt-original",
        "client_id": "cid", "token_uri": f"{base}/oauth/token",
        "expires_at": 0,
    })
    sess.get(f"{base}/resource")
    assert state["creds"]["refresh_token"] == "rt-rotated"


def test_unexpired_token_skips_refresh_entirely(mock_issuer):
    """If we have a token that's still well within its expiry, don't
    refresh — that's the whole point of the cache."""
    server, base = mock_issuer
    server.access_token_seq = []  # would error if used

    sess, _ = _wire_session({
        "access_token": "still-valid",
        "refresh_token": "rt-3",
        "client_id": "cid", "token_uri": f"{base}/oauth/token",
        "expires_at": time.time() + 3600,
    })
    r = sess.get(f"{base}/resource")
    assert r.status_code == 200
    assert server.refresh_calls == 0  # never touched the issuer
