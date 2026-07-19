"""Forgot-password SMTP path — exercise both delivery modes.

Covers the four-cell matrix described in the endpoint docstring:

                                SMTP configured | SMTP missing
    -----------------------------------------------------------
    Registered email            token = null    | token in body
                                 email sent     | no email send
    -----------------------------------------------------------
    Unknown email (anti-enum)   token = null    | token = null
                                 NO email send  | no email send

The SMTP path patches ``smtplib.SMTP`` so no real socket is opened. We
assert that ``sendmail`` was called with the right recipient, subject
and body containing the reset URL — and that the API response shape
matches the contract the frontend gates on (presence/absence of
``reset_token``).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# psutil isn't part of the test environment in some local installs; stubbing
# it before importing fpulse.api lets these tests run without that optional
# dep being present. The only psutil reference resolved at import time is
# the type-annotation in health_memory.py, which is deferred by the file's
# ``from __future__ import annotations`` — a bare ModuleType is enough.
if "psutil" not in sys.modules:
    sys.modules["psutil"] = ModuleType("psutil")

from fpulse.api.auth import ForgotPasswordRequest, forgot_password  # noqa: E402
from fpulse.auth.models import User  # noqa: E402
from fpulse.auth.store import UserStore  # noqa: E402


def _fake_request(*, host: str = "fpulse.example", scheme: str = "https",
                  client_ip: str = "10.0.0.1", origin: str | None = None) -> SimpleNamespace:
    """Build a minimal Request stand-in.

    The endpoint reads four things off ``request``:
      - ``request.client.host`` for the audit IP
      - ``request.url.scheme`` (fallback path in ``_request_origin``)
      - ``request.headers.get(...)`` for Origin / Host
      - ``request.base_url`` for the primary origin fallback

    We give it exactly that surface — no need for an ASGI scope.
    """
    headers = {"host": host}
    if origin:
        headers["origin"] = origin
    return SimpleNamespace(
        client=SimpleNamespace(host=client_ip),
        url=SimpleNamespace(scheme=scheme),
        headers=headers,
        base_url=f"{scheme}://{host}/",
    )


def _write_smtp_settings(db, *, host: str = "smtp.example.com",
                         port: int = 587, user: str = "bot@example.com",
                         password: str = "hunter2",
                         from_email: str = "noreply@fpulse.example",
                         tls: bool = True) -> None:
    """Persist a ``notifications.smtp`` block into the admin_settings row
    so ``NotificationService._load_smtp_config`` picks it up the same
    way it would in production."""
    payload = {"notifications": {"smtp": {
        "host": host, "port": port, "user": user, "password": password,
        "from_email": from_email, "tls": tls,
    }}}
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO settings (id, data, created_at, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET data = excluded.data, "
        "updated_at = excluded.updated_at",
        ("admin_settings", json.dumps(payload), now, now),
    )


@pytest.fixture
def app_state_wired(_fpulse_test_db, monkeypatch):
    """Wire the per-test DB + a UserStore into ``app_state``.

    ``forgot_password`` resolves the user store via ``get_store()`` which
    reads ``app_state["user_store"]``, and ``_read_auth_queue`` reads
    ``app_state["db"]`` directly. Mutating the real dict via setitem so
    the ``from fpulse.main import app_state`` references in auth.py see
    the test wiring — same pattern as test_step_io_endpoints.py.

    Env-var SMTP_* keys are wiped because ``_load_smtp_config`` falls back
    to them when the DB row is empty — a dev shell with SMTP_HOST exported
    would otherwise flip the "no SMTP" tests into the SMTP path.
    """
    from fpulse.main import app_state
    for env_key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER",
                    "SMTP_PASS", "SMTP_FROM", "SMTP_TLS",
                    "FPULSE_PUBLIC_URL"):
        monkeypatch.delenv(env_key, raising=False)
    user_store = UserStore(db=_fpulse_test_db)
    monkeypatch.setitem(app_state, "db", _fpulse_test_db)
    monkeypatch.setitem(app_state, "user_store", user_store)
    yield {"db": _fpulse_test_db, "user_store": user_store}


@pytest.fixture
def registered_user(app_state_wired):
    user = User(
        id="u-alice",
        email="alice@example.com",
        name="Alice",
        password_hash=User.hash_password("old-password-1!"),
    )
    app_state_wired["user_store"].create_user(user)
    return user


# ─────────────────────────────────────────────────────────────────────────
# SMTP configured branch
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_known_email_with_smtp_returns_null_token_and_sends_email(
    app_state_wired, registered_user
):
    _write_smtp_settings(app_state_wired["db"])

    with patch("fpulse.alerts.notifier.smtplib.SMTP") as mock_smtp:
        # Context-manager protocol: NotificationService uses ``with smtplib.SMTP(...) as server``.
        server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = server

        resp = await forgot_password(
            ForgotPasswordRequest(email=registered_user.email),
            _fake_request(host="fpulse.example", scheme="https"),
        )

        assert resp["queued"] is True
        assert resp["reset_token"] is None, (
            "SMTP path must NOT echo the live token — exposing it is exactly "
            "the no-SMTP account-takeover hole this change closes."
        )
        assert resp["expires_at"] is None
        assert resp["ttl_seconds"] is None
        assert "email" in resp["message"].lower()

    # smtplib.SMTP(host, port, timeout=...) — args[0]=host, args[1]=port.
    assert mock_smtp.call_count == 1
    smtp_args, smtp_kwargs = mock_smtp.call_args
    assert smtp_args[0] == "smtp.example.com"
    assert smtp_args[1] == 587

    # TLS path: starttls + login + sendmail
    assert server.starttls.called
    assert server.login.called
    assert server.sendmail.called

    from_email, to_list, payload = server.sendmail.call_args[0]
    assert from_email == "noreply@fpulse.example"
    assert to_list == ["alice@example.com"]
    assert "Subject: Reset your F-Pulse password" in payload
    # The reset URL is the load-bearing payload — assert the live token
    # made it into the body so a real user clicking the link reaches
    # the reset form with their token prefilled.
    assert "https://fpulse.example/?reset_token=" in payload


@pytest.mark.asyncio
async def test_known_email_with_smtp_uses_origin_header_when_present(
    app_state_wired, registered_user
):
    """If the browser sent an ``Origin`` header that differs from ``Host``
    (split frontend/backend deploys), the email should embed that origin
    — otherwise the link points at the API host and 404s."""
    _write_smtp_settings(app_state_wired["db"])

    with patch("fpulse.alerts.notifier.smtplib.SMTP") as mock_smtp:
        server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = server

        await forgot_password(
            ForgotPasswordRequest(email=registered_user.email),
            _fake_request(
                host="api.fpulse.example",
                origin="https://app.fpulse.example",
            ),
        )

    payload = server.sendmail.call_args[0][2]
    assert "https://app.fpulse.example/?reset_token=" in payload


# ─────────────────────────────────────────────────────────────────────────
# No-SMTP fallback branch
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_known_email_without_smtp_omits_token_by_default(
    app_state_wired, registered_user
):
    """Secure default: with no SMTP, multiple users, no inline opt-in, and a
    non-loopback caller, the reset token is NOT echoed in the response — it
    would be a public, unauthenticated account-takeover token (and its mere
    presence would leak that the email is registered). The request is still
    queued so an admin can fulfil it from the Auth Queue."""
    with patch("fpulse.alerts.notifier.smtplib.SMTP") as mock_smtp:
        resp = await forgot_password(
            ForgotPasswordRequest(email=registered_user.email),
            _fake_request(client_ip="10.0.0.1"),
        )

        assert resp["queued"] is True
        assert resp["reset_token"] is None
        assert resp["expires_at"] is None
        assert resp["ttl_seconds"] is None
        assert "generated" in resp["message"].lower()
    assert mock_smtp.called is False, (
        "No SMTP configured → endpoint must not even attempt a connection."
    )


@pytest.mark.asyncio
async def test_inline_token_opt_in_returns_token(
    app_state_wired, registered_user, monkeypatch
):
    """Opt-in path: FPULSE_FORGOT_TOKEN_INLINE=1 from a loopback client
    restores the local single-binary self-serve UX (token echoed in body)."""
    monkeypatch.setenv("FPULSE_FORGOT_TOKEN_INLINE", "1")
    with patch("fpulse.alerts.notifier.smtplib.SMTP"):
        resp = await forgot_password(
            ForgotPasswordRequest(email=registered_user.email),
            _fake_request(client_ip="127.0.0.1"),
        )

        assert resp["queued"] is True
        assert resp["reset_token"] is not None
        assert len(resp["reset_token"]) >= 32
        assert resp["expires_at"] is not None
        assert resp["ttl_seconds"] == 3600


# ─────────────────────────────────────────────────────────────────────────
# Anti-enumeration: unknown email returns the same shape in both branches
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_email_with_smtp_does_not_send(app_state_wired):
    _write_smtp_settings(app_state_wired["db"])

    with patch("fpulse.alerts.notifier.smtplib.SMTP") as mock_smtp:
        resp = await forgot_password(
            ForgotPasswordRequest(email="ghost@example.com"),
            _fake_request(),
        )

    assert resp["queued"] is True
    assert resp["reset_token"] is None
    # SMTP-configured message regardless of match, so the response shape
    # for unknown emails is indistinguishable from the known-email case
    # above — that's the anti-enumeration guarantee.
    assert "email" in resp["message"].lower()
    assert mock_smtp.called is False, (
        "Unknown email → MUST NOT send (would email a non-customer and "
        "leak that the address is not on file via bounce / delivery)."
    )


@pytest.mark.asyncio
async def test_unknown_email_without_smtp_returns_null_token(app_state_wired):
    with patch("fpulse.alerts.notifier.smtplib.SMTP") as mock_smtp:
        resp = await forgot_password(
            ForgotPasswordRequest(email="ghost@example.com"),
            _fake_request(),
        )

    assert resp["queued"] is True
    assert resp["reset_token"] is None
    assert resp["expires_at"] is None
    assert resp["ttl_seconds"] is None
    assert mock_smtp.called is False


# ─────────────────────────────────────────────────────────────────────────
# Resilience: SMTP relay down must NOT downgrade to inline-token mode
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_smtp_send_failure_still_returns_null_token(
    app_state_wired, registered_user
):
    """If the relay is misconfigured / unreachable, we accept the
    delivery loss and keep the token out of the response. A "fall back
    to inline token on failure" path would let an attacker DoS the
    SMTP relay (or trip a transient error) to re-open the takeover
    hole this change exists to close."""
    _write_smtp_settings(app_state_wired["db"])

    with patch("fpulse.alerts.notifier.smtplib.SMTP", side_effect=OSError("relay down")):
        resp = await forgot_password(
            ForgotPasswordRequest(email=registered_user.email),
            _fake_request(),
        )

    assert resp["reset_token"] is None
    assert "email" in resp["message"].lower()
