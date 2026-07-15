"""Tests for OAuth flow primitives + PKCE + health registry.

Covers:
  - PKCE pair generation (RFC 7636 spec compliance)
  - client_credentials flow against an in-process token endpoint
  - authorization_code exchange (with and without PKCE)
  - device_code request + polling lifecycle (pending → success)
  - AuthHealth status derivation across all 5 states
  - Registry success/failure recording
  - OAuthSession + registry integration: refresh publishes events
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest

from fpulse.connections.oauth_flows import (
    DeviceCodePending,
    DeviceCodeSlowDown,
    authorization_code_exchange,
    client_credentials,
    device_code_poll,
    device_code_request,
    pkce_pair,
)
from fpulse.connections.oauth_health import (
    AuthHealth,
    AuthHealthRegistry,
    EXPIRING_SOON_S,
)
from fpulse.connections.oauth_session import OAuthSession


# ── PKCE ────────────────────────────────────────────────────────────

def test_pkce_pair_produces_valid_challenge():
    verifier, challenge = pkce_pair()
    # RFC 7636: verifier 43-128 chars unreserved.
    assert 43 <= len(verifier) <= 128
    # Challenge is base64url(SHA256(verifier)) without padding.
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    assert challenge == expected
    assert "=" not in challenge  # padding stripped


def test_pkce_pair_uniqueness():
    """Each call must produce a fresh verifier — no collision risk."""
    pairs = {pkce_pair()[0] for _ in range(100)}
    assert len(pairs) == 100


# ── Mock token endpoint ────────────────────────────────────────────

class _MockTokenHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode()
        params = {k: v[0] for k, v in parse_qs(raw).items()}
        self.server.last_request = params  # type: ignore[attr-defined]
        handler = self.server.handler  # type: ignore[attr-defined]
        code, body = handler(params, self.path)
        return self._json(code, body)


@pytest.fixture
def token_server():
    server = HTTPServer(("127.0.0.1", 0), _MockTokenHandler)
    server.handler = lambda params, path: (200, {})  # type: ignore[attr-defined]
    server.last_request = None  # type: ignore[attr-defined]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield server, base
    server.shutdown()


# ── client_credentials ──────────────────────────────────────────────

def test_client_credentials_flow_returns_credential_dict(token_server):
    server, base = token_server
    server.handler = lambda params, path: (200, {  # type: ignore[attr-defined]
        "access_token": "cc-token",
        "expires_in": 3600,
        "scope": "read:assets",
    })
    creds = client_credentials(
        f"{base}/oauth/token", "client-id", "client-secret",
        scope="read:assets",
    )
    assert creds["access_token"] == "cc-token"
    assert creds["scope"] == "read:assets"
    # expires_at translates from expires_in.
    assert abs(creds["expires_at"] - (time.time() + 3600)) < 5

    # Verify the right grant type went on the wire.
    assert server.last_request["grant_type"] == "client_credentials"  # type: ignore[attr-defined]
    assert server.last_request["client_id"] == "client-id"  # type: ignore[attr-defined]


def test_client_credentials_passes_audience_when_provided(token_server):
    server, base = token_server
    server.handler = lambda params, path: (200, {"access_token": "x", "expires_in": 60})  # type: ignore[attr-defined]
    client_credentials(f"{base}/oauth/token", "cid", "csec",
                         audience="https://api.example.com")
    assert server.last_request["audience"] == "https://api.example.com"  # type: ignore[attr-defined]


# ── authorization_code (incl. PKCE) ─────────────────────────────────

def test_authorization_code_with_pkce_includes_verifier(token_server):
    server, base = token_server
    server.handler = lambda params, path: (200, {  # type: ignore[attr-defined]
        "access_token": "ac-token",
        "refresh_token": "rt-001",
        "expires_in": 1800,
    })
    verifier, _ = pkce_pair()
    creds = authorization_code_exchange(
        f"{base}/oauth/token",
        code="auth-code-from-redirect",
        redirect_uri="https://app.example.com/callback",
        client_id="public-app",
        code_verifier=verifier,
    )
    assert creds["access_token"] == "ac-token"
    assert creds["refresh_token"] == "rt-001"
    assert server.last_request["grant_type"] == "authorization_code"  # type: ignore[attr-defined]
    assert server.last_request["code_verifier"] == verifier  # type: ignore[attr-defined]
    # Public client → no client_secret expected.
    assert "client_secret" not in server.last_request  # type: ignore[attr-defined]


def test_authorization_code_confidential_includes_secret(token_server):
    server, base = token_server
    server.handler = lambda params, path: (200, {"access_token": "x", "expires_in": 60})  # type: ignore[attr-defined]
    authorization_code_exchange(
        f"{base}/oauth/token",
        code="abc", redirect_uri="https://x/cb",
        client_id="cid", client_secret="csec",
    )
    assert server.last_request["client_secret"] == "csec"  # type: ignore[attr-defined]


# ── device_code (request + poll lifecycle) ──────────────────────────

def test_device_code_request_returns_user_visible_info(token_server):
    server, base = token_server
    server.handler = lambda params, path: (200, {  # type: ignore[attr-defined]
        "device_code": "dc-abc",
        "user_code": "ABCD-1234",
        "verification_uri": "https://example.com/device",
        "interval": 5,
        "expires_in": 600,
    })
    body = device_code_request(f"{base}/device/code", client_id="cid",
                                  scope="profile")
    assert body["user_code"] == "ABCD-1234"
    assert body["device_code"] == "dc-abc"


def test_device_code_poll_pending_raises_typed_exception(token_server):
    server, base = token_server

    def handler(params, path):
        return (400, {"error": "authorization_pending",
                       "error_description": "user has not completed auth"})
    server.handler = handler  # type: ignore[attr-defined]
    with pytest.raises(DeviceCodePending):
        device_code_poll(f"{base}/oauth/token",
                          device_code="dc-abc", client_id="cid")


def test_device_code_poll_slow_down_raises_typed_exception(token_server):
    server, base = token_server
    server.handler = lambda p, _path: (400, {"error": "slow_down"})  # type: ignore[attr-defined]
    with pytest.raises(DeviceCodeSlowDown):
        device_code_poll(f"{base}/oauth/token",
                          device_code="dc", client_id="cid")


def test_device_code_poll_success_returns_credentials(token_server):
    server, base = token_server
    server.handler = lambda p, _path: (200, {  # type: ignore[attr-defined]
        "access_token": "device-tok", "refresh_token": "rt", "expires_in": 3600,
    })
    creds = device_code_poll(f"{base}/oauth/token",
                                device_code="dc", client_id="cid")
    assert creds["access_token"] == "device-tok"
    assert creds["refresh_token"] == "rt"


# ── AuthHealth status derivation ────────────────────────────────────

def test_health_status_unknown_when_no_data():
    h = AuthHealth(connection_id="c1")
    assert h.derive_status() == "unknown"


def test_health_status_healthy_when_token_fresh():
    h = AuthHealth(connection_id="c1",
                     last_refresh_at=time.time() - 60,
                     expires_at=time.time() + 3600)
    assert h.derive_status() == "healthy"


def test_health_status_expiring_soon_when_under_buffer():
    h = AuthHealth(connection_id="c1",
                     last_refresh_at=time.time() - 60,
                     expires_at=time.time() + EXPIRING_SOON_S - 10)
    assert h.derive_status() == "expiring_soon"


def test_health_status_stale_when_expiry_passed():
    h = AuthHealth(connection_id="c1",
                     last_refresh_at=time.time() - 1000,
                     expires_at=time.time() - 100)
    assert h.derive_status() == "stale"


def test_health_status_failed_when_recent_failure_overrides():
    """A failed refresh AFTER the last success means the cached
    expires_at is not trustworthy — status must be 'failed'."""
    h = AuthHealth(connection_id="c1",
                     last_refresh_at=time.time() - 1000,
                     last_failure_at=time.time() - 10,
                     last_failure_reason="connection refused",
                     expires_at=time.time() + 3600)
    assert h.derive_status() == "failed"


def test_health_recovers_to_healthy_after_successful_refresh():
    """Failure in the past, success since → status is healthy again."""
    h = AuthHealth(connection_id="c1",
                     last_failure_at=time.time() - 1000,
                     last_refresh_at=time.time() - 10,
                     expires_at=time.time() + 3600)
    assert h.derive_status() == "healthy"


# ── Registry ────────────────────────────────────────────────────────

def test_registry_records_success_with_expiry_and_scopes():
    reg = AuthHealthRegistry()
    h = reg.record_refresh_success(
        "conn-1", flow="refresh_token",
        expires_at=time.time() + 3600, scopes=["read", "write"],
    )
    assert h.refresh_count == 1
    assert h.scopes == ["read", "write"]
    assert h.derive_status() == "healthy"


def test_registry_increments_counts_across_calls():
    reg = AuthHealthRegistry()
    for _ in range(3):
        reg.record_refresh_success("conn-1", expires_at=time.time() + 100)
    reg.record_refresh_failure("conn-1", reason="boom")
    h = reg.get("conn-1")
    assert h.refresh_count == 3
    assert h.failure_count == 1


def test_registry_caps_failure_reason_length():
    reg = AuthHealthRegistry()
    reg.record_refresh_failure("conn-1", reason="x" * 1000)
    assert len(reg.get("conn-1").last_failure_reason) <= 300


def test_registry_to_dict_includes_derived_fields():
    reg = AuthHealthRegistry()
    reg.record_refresh_success("conn-1", expires_at=time.time() + 1800)
    d = reg.get("conn-1").to_dict()
    assert d["status"] == "healthy"
    assert d["time_to_expiry"] is not None
    assert d["time_to_expiry"] > 0


# ── OAuthSession + registry integration ─────────────────────────────

def test_session_publishes_success_to_registry(token_server):
    server, base = token_server
    server.handler = lambda p, _path: (200, {  # type: ignore[attr-defined]
        "access_token": "fresh", "expires_in": 3600,
    })
    reg = AuthHealthRegistry()
    state = {"creds": {
        "access_token": "old", "refresh_token": "rt",
        "client_id": "cid", "client_secret": "csec",
        "token_uri": f"{base}/oauth/token",
        "expires_at": 0,
    }}
    sess = OAuthSession(
        get_credentials=lambda: dict(state["creds"]),
        put_credentials=lambda new: state.update({"creds": new}),
        connection_id="conn-7",
        registry=reg,
    )
    sess._refresh(state["creds"])
    h = reg.get("conn-7")
    assert h is not None
    assert h.refresh_count == 1
    assert h.derive_status() == "healthy"


def test_session_publishes_failure_to_registry(token_server):
    server, base = token_server
    server.handler = lambda p, _path: (500, {"error": "server_error"})  # type: ignore[attr-defined]
    reg = AuthHealthRegistry()
    state = {"creds": {
        "access_token": "old", "refresh_token": "rt",
        "client_id": "cid", "client_secret": "csec",
        "token_uri": f"{base}/oauth/token",
        "expires_at": 0,
    }}
    sess = OAuthSession(
        get_credentials=lambda: dict(state["creds"]),
        put_credentials=lambda new: None,
        connection_id="conn-8",
        registry=reg,
    )
    with pytest.raises(Exception):
        sess._refresh(state["creds"])
    h = reg.get("conn-8")
    assert h is not None
    assert h.failure_count == 1
    assert h.derive_status() == "failed"


def test_session_supports_client_credentials_flow(token_server):
    """When the credential dict declares flow='client_credentials',
    the session uses the client_credentials grant type — no
    refresh_token needed."""
    server, base = token_server
    server.handler = lambda p, _path: (200, {  # type: ignore[attr-defined]
        "access_token": "cc-fresh", "expires_in": 3600,
    })
    state = {"creds": {
        "access_token": "old", "flow": "client_credentials",
        "client_id": "cid", "client_secret": "csec",
        "token_uri": f"{base}/oauth/token",
        "expires_at": 0,
    }}
    sess = OAuthSession(
        get_credentials=lambda: dict(state["creds"]),
        put_credentials=lambda new: state.update({"creds": new}),
    )
    new = sess._refresh(state["creds"])
    assert new["access_token"] == "cc-fresh"
    # Verify the wire format used the correct grant_type.
    assert server.last_request["grant_type"] == "client_credentials"  # type: ignore[attr-defined]
