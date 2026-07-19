"""Unit tests for the launch security hardening (2026-05-29).

Covers three defenses added during the pre-launch security audit:

  1. SSRF allowlist on the OpenAPI fetcher
     (``fpulse.connectors.ai_authoring._ssrf_check_url``)
  2. Login rate-limit + account-lockout
     (``fpulse.api.auth._login_check_and_delay`` /
     ``_login_record_failure`` / ``_login_record_success``)
  3. AI endpoint per-user rate-limit
     (``fpulse.ai.rate_limit.check_ai_rate_limit`` /
     ``get_ai_rate_usage``)

These were validated by hand-rolled smoke scripts during the audit;
this file is the CI-grade regression guard.
"""

from __future__ import annotations

import importlib
import os
import time

import pytest
from fastapi import HTTPException


# ── 1. SSRF guard ─────────────────────────────────────────────────────


class TestSsrfGuard:
    """Every blocked URL in this class is a real exfiltration / probe
    vector we've seen in SSRF write-ups. If any of them START PASSING,
    the SSRF guard regressed and an attacker can poke internal services
    via the Author Connector feature.
    """

    @pytest.fixture(autouse=True)
    def _ensure_default_policy(self, monkeypatch):
        """The 'allow private' override is OFF by default. Make sure
        any other test that flipped it doesn't bleed in."""
        monkeypatch.delenv("FPULSE_OPENAPI_FETCH_ALLOW_PRIVATE", raising=False)

    def _check(self, url: str):
        from fpulse.connectors.ai_authoring import _ssrf_check_url
        return _ssrf_check_url(url)

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.169.254/computeMetadata/v1/",
        "http://[fe80::1]/",                 # link-local IPv6
    ])
    def test_blocks_cloud_metadata_endpoints(self, url):
        from fpulse.connectors.ai_authoring import SsrfBlockedError
        with pytest.raises(SsrfBlockedError):
            self._check(url)

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8001/api/health",
        "http://127.1.2.3/",
        "http://[::1]/",                     # IPv6 loopback
        "http://localhost/x",
    ])
    def test_blocks_loopback(self, url):
        from fpulse.connectors.ai_authoring import SsrfBlockedError
        with pytest.raises(SsrfBlockedError):
            self._check(url)

    @pytest.mark.parametrize("url", [
        "http://10.0.0.1/",
        "http://10.255.255.255/",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://192.168.1.1/",
        "http://192.168.255.254/",
    ])
    def test_blocks_rfc1918_private(self, url):
        from fpulse.connectors.ai_authoring import SsrfBlockedError
        with pytest.raises(SsrfBlockedError):
            self._check(url)

    @pytest.mark.parametrize("url,reason", [
        ("file:///etc/passwd",          "file://"),
        ("gopher://x.example.com/",     "gopher://"),
        ("ftp://example.com/",          "ftp://"),
        ("javascript:alert(1)",         "javascript:"),
        ("ldap://example.com/",         "ldap://"),
    ])
    def test_blocks_non_http_schemes(self, url, reason):
        from fpulse.connectors.ai_authoring import SsrfBlockedError
        with pytest.raises(SsrfBlockedError):
            self._check(url)

    def test_blocks_embedded_credentials(self):
        from fpulse.connectors.ai_authoring import SsrfBlockedError
        with pytest.raises(SsrfBlockedError):
            self._check("http://attacker:p@example.com/")

    def test_blocks_oversize_url(self):
        from fpulse.connectors.ai_authoring import SsrfBlockedError
        with pytest.raises(SsrfBlockedError):
            self._check("http://example.com/" + "A" * 3000)

    def test_allows_public_https(self):
        # Real public host that resolves. Don't actually fetch — just
        # confirm the policy check passes.
        scheme, host, port = self._check("https://api.github.com/")
        assert scheme == "https"
        assert host == "api.github.com"
        assert port == 443

    def test_override_allows_private_when_env_set(self, monkeypatch):
        """On-prem operators with an internal API catalog can opt in to
        private-range fetching. The override still keeps multicast /
        reserved blocked — see _ssrf_check_url body."""
        monkeypatch.setenv("FPULSE_OPENAPI_FETCH_ALLOW_PRIVATE", "1")
        # Loopback now passes (used to raise).
        scheme, host, _ = self._check("http://127.0.0.1:8080/spec.json")
        assert scheme == "http"
        assert host == "127.0.0.1"


# ── 2. Login rate-limit + lockout ─────────────────────────────────────


class TestLoginRateLimit:
    """Brute-force defense. Three behaviours under test:

      * First N attempts are free (no delay, no lockout)
      * Attempts N+1 .. M-1 incur exponential delay
      * Attempt M triggers hard lockout with HTTP 429 + Retry-After
      * Success wipes the counter
      * Per-(email, IP) isolation: attacker on IP-A doesn't lock out
        the legit user on IP-B
    """

    @pytest.fixture(autouse=True)
    def _reset_state(self):
        """Reset the in-process counter between tests so state from
        one test doesn't leak into another."""
        from fpulse.api import auth as auth_mod
        auth_mod._login_fail_state.clear()
        yield
        auth_mod._login_fail_state.clear()

    def test_first_attempts_are_free(self):
        from fpulse.api.auth import _login_check_and_delay
        # 3 attempts at the soft threshold — no delay yet.
        for _ in range(3):
            t0 = time.time()
            _login_check_and_delay("user@example.com", "1.2.3.4")
            assert time.time() - t0 < 0.05

    def test_progressive_delay_after_soft_threshold(self):
        from fpulse.api.auth import (
            _login_check_and_delay,
            _login_record_failure,
        )
        email, ip = "user@example.com", "1.2.3.4"
        # Burn through the 3 free attempts.
        for _ in range(3):
            _login_check_and_delay(email, ip)
            _login_record_failure(email, ip)
        # Attempt #4 should delay ~1s (2 ** (3 - 3) = 1).
        t0 = time.time()
        _login_check_and_delay(email, ip)
        delay = time.time() - t0
        assert delay >= 0.9, f"expected >=1s delay at attempt 4, got {delay:.2f}s"

    def test_hard_lockout_after_threshold(self):
        from fpulse.api.auth import (
            _login_check_and_delay,
            _login_record_failure,
        )
        email, ip = "attacker@example.com", "203.0.113.99"
        # Drive the counter past the hard threshold.
        for _ in range(8):
            try:
                _login_check_and_delay(email, ip)
            except HTTPException:
                pass  # delay-induced HTTPException is the prior fail state
            _login_record_failure(email, ip)
        # Next attempt MUST be locked out.
        with pytest.raises(HTTPException) as excinfo:
            _login_check_and_delay(email, ip)
        assert excinfo.value.status_code == 429
        # Retry-After header must be set so the client can back off.
        assert excinfo.value.headers.get("Retry-After")
        assert int(excinfo.value.headers["Retry-After"]) > 0
        # Detail must carry the structured machine code.
        assert excinfo.value.detail.get("code") == "login_locked"

    def test_cross_ip_isolation(self):
        from fpulse.api.auth import (
            _login_check_and_delay,
            _login_record_failure,
        )
        # Attacker storms 10 fails from IP-A.
        for _ in range(10):
            try:
                _login_check_and_delay("victim@example.com", "203.0.113.99")
            except HTTPException:
                pass
            _login_record_failure("victim@example.com", "203.0.113.99")
        # Legit user on IP-B with the same email should NOT be throttled.
        t0 = time.time()
        _login_check_and_delay("victim@example.com", "198.51.100.5")
        assert time.time() - t0 < 0.05, (
            "legit user from different IP was punished for attacker's storm"
        )

    def test_success_resets_counter(self):
        from fpulse.api.auth import (
            _login_check_and_delay,
            _login_record_failure,
            _login_record_success,
        )
        email, ip = "user@example.com", "1.2.3.4"
        # Two fails — under soft threshold but counter is non-zero.
        _login_check_and_delay(email, ip); _login_record_failure(email, ip)
        _login_check_and_delay(email, ip); _login_record_failure(email, ip)
        # Successful login wipes the counter.
        _login_record_success(email, ip)
        # Now ALL future attempts start clean.
        for _ in range(3):
            t0 = time.time()
            _login_check_and_delay(email, ip)
            assert time.time() - t0 < 0.05


# ── 3. AI endpoint rate-limit ─────────────────────────────────────────


class TestAiRateLimit:
    """Per-user sliding-window quota on AI endpoints. LLM calls cost
    real money — without this a single leaked session can drain the
    operator's budget in minutes.
    """

    @pytest.fixture
    def small_quota(self, monkeypatch):
        """Tight quota + short window so tests stay fast."""
        monkeypatch.setenv("FPULSE_AI_RATE_MAX_PER_HOUR", "5")
        monkeypatch.setenv("FPULSE_AI_RATE_WINDOW_SECONDS", "60")
        monkeypatch.setenv("FPULSE_AI_RATE_ENABLE", "1")
        # Force re-read of module-level constants.
        from fpulse.ai import rate_limit
        importlib.reload(rate_limit)
        return rate_limit

    def test_allows_up_to_quota(self, small_quota):
        rl = small_quota
        for i in range(5):
            rl.check_ai_rate_limit(user_id="user-a")
        usage = rl.get_ai_rate_usage(user_id="user-a")
        assert usage["used"] == 5
        assert usage["remaining"] == 0

    def test_blocks_at_quota_exhaustion(self, small_quota):
        rl = small_quota
        for _ in range(5):
            rl.check_ai_rate_limit(user_id="user-b")
        with pytest.raises(HTTPException) as excinfo:
            rl.check_ai_rate_limit(user_id="user-b")
        assert excinfo.value.status_code == 429
        assert excinfo.value.detail["code"] == "ai_rate_limit_exceeded"
        assert int(excinfo.value.headers["Retry-After"]) > 0

    def test_cross_user_isolation(self, small_quota):
        rl = small_quota
        # User-c exhausts their quota.
        for _ in range(5):
            rl.check_ai_rate_limit(user_id="user-c")
        # User-d should not be affected.
        rl.check_ai_rate_limit(user_id="user-d")
        u_d = rl.get_ai_rate_usage(user_id="user-d")
        assert u_d["used"] == 1
        assert u_d["remaining"] == 4

    def test_cost_parameter_charges_extra(self, small_quota):
        """Streaming endpoints charge 2x because their LLM session
        runs more iterations than a synchronous call."""
        rl = small_quota
        rl.check_ai_rate_limit(user_id="user-e", cost=3)
        usage = rl.get_ai_rate_usage(user_id="user-e")
        assert usage["used"] == 3

    def test_disable_flag_is_a_no_op(self, monkeypatch):
        """Test environments that hammer the AI endpoint can set
        FPULSE_AI_RATE_ENABLE=0 to skip the check entirely."""
        monkeypatch.setenv("FPULSE_AI_RATE_MAX_PER_HOUR", "1")
        monkeypatch.setenv("FPULSE_AI_RATE_ENABLE", "0")
        from fpulse.ai import rate_limit
        importlib.reload(rate_limit)
        # 50 calls with quota=1 would normally lock at call #2;
        # disabled mode just returns.
        for _ in range(50):
            rate_limit.check_ai_rate_limit(user_id="test")

    def test_ip_fallback_when_no_user(self, small_quota):
        """Anonymous endpoints (rare on F-Pulse but possible) key
        on client IP instead of user_id."""
        rl = small_quota
        for _ in range(5):
            rl.check_ai_rate_limit(user_id=None, fallback_ip="203.0.113.5")
        with pytest.raises(HTTPException):
            rl.check_ai_rate_limit(user_id=None, fallback_ip="203.0.113.5")
        # Different IP, clean slate.
        rl.check_ai_rate_limit(user_id=None, fallback_ip="203.0.113.6")
