"""Tests for the local-network / air-gapped runtime helpers."""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from fpulse.connections.runtime import (
    ReachabilityResult,
    check_reachability,
    is_air_gapped,
    resolve_verify_ssl,
    runtime_status,
)


# ── verify_ssl precedence ────────────────────────────────────────────

def test_verify_defaults_true_with_no_overrides(monkeypatch):
    monkeypatch.delenv("FPULSE_VERIFY_SSL", raising=False)
    assert resolve_verify_ssl(None) is True
    assert resolve_verify_ssl({}) is True


def test_verify_per_connection_false_wins(monkeypatch):
    monkeypatch.setenv("FPULSE_VERIFY_SSL", "true")
    # Per-connection override is the strongest signal — internal
    # services with self-signed certs need this to opt out.
    assert resolve_verify_ssl({"verify_ssl": False}) is False


def test_verify_per_connection_true_wins_over_env_false(monkeypatch):
    """Test the inverse direction too: an operator may have globally
    disabled verify in dev but want to leave it on for one specific
    well-behaved connection."""
    monkeypatch.setenv("FPULSE_VERIFY_SSL", "false")
    assert resolve_verify_ssl({"verify_ssl": True}) is True


def test_verify_env_false_propagates_when_no_override(monkeypatch):
    monkeypatch.setenv("FPULSE_VERIFY_SSL", "false")
    assert resolve_verify_ssl(None) is False
    assert resolve_verify_ssl({}) is False


def test_verify_env_garbage_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("FPULSE_VERIFY_SSL", "yesplease")
    # Unparseable value → default (True) wins — fail-safe direction.
    assert resolve_verify_ssl(None) is True


# ── air-gapped flag ─────────────────────────────────────────────────

def test_air_gapped_default_false(monkeypatch):
    monkeypatch.delenv("FPULSE_AIR_GAPPED", raising=False)
    assert is_air_gapped() is False


def test_air_gapped_truthy_values(monkeypatch):
    for v in ("1", "true", "True", "yes", "on"):
        monkeypatch.setenv("FPULSE_AIR_GAPPED", v)
        assert is_air_gapped() is True, f"failed for {v!r}"


def test_air_gapped_falsy_values(monkeypatch):
    for v in ("0", "false", "no", "off"):
        monkeypatch.setenv("FPULSE_AIR_GAPPED", v)
        assert is_air_gapped() is False, f"failed for {v!r}"


def test_runtime_status_shape(monkeypatch):
    monkeypatch.setenv("FPULSE_AIR_GAPPED", "1")
    monkeypatch.setenv("FPULSE_VERIFY_SSL", "false")
    s = runtime_status()
    assert s == {"air_gapped": True, "verify_ssl_default": False}


# ── reachability probe ─────────────────────────────────────────────

@pytest.fixture
def listening_server():
    """A trivial HTTP server bound to localhost — enough that a TCP
    socket connect will succeed."""
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield server, base
    server.shutdown()


def test_reachability_succeeds_against_local_listener(listening_server):
    server, base = listening_server
    result = check_reachability(base, timeout_s=2.0)
    assert result.reachable is True
    assert result.detail == "tcp connect ok"
    assert result.latency_ms is not None and result.latency_ms >= 0


def test_reachability_fails_for_closed_port():
    # Bind a port and immediately release it so we know it's closed.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    result = check_reachability(f"http://127.0.0.1:{port}", timeout_s=1.0)
    assert result.reachable is False
    assert "127.0.0.1" in result.target


def test_reachability_fails_for_unresolvable_host():
    result = check_reachability("http://this-host-does-not-exist-xyz.invalid",
                                  timeout_s=2.0)
    assert result.reachable is False
    # DNS failure should be classified, not a generic timeout.
    assert "dns" in result.detail.lower() or "name" in result.detail.lower()


def test_reachability_handles_url_without_scheme():
    """check_reachability should accept bare host:port too — the
    engine's base_url is already a real URL but be tolerant."""
    result = check_reachability("127.0.0.1:1", timeout_s=0.5)
    # Won't connect (port 1 closed), but must not raise.
    assert isinstance(result, ReachabilityResult)
    assert result.target.startswith("127.0.0.1:")


def test_reachability_returns_in_bounded_time():
    """A 1-second timeout must not stretch into 10-second territory
    even on slow CI."""
    import time
    start = time.monotonic()
    result = check_reachability("http://192.0.2.1:80", timeout_s=1.0)  # TEST-NET-1
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"reachability check overran: {elapsed:.2f}s"
    # On most networks this lands as timeout; on some as routing error.
    # Both are acceptable — we just need it to be `reachable=False`.
    assert result.reachable is False
