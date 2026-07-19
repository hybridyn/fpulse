"""OAuth2 flow primitives.

The existing `oauth_session.py` only supports the refresh_token grant
(reading a stored refresh_token, getting a fresh access_token). That
covers ~80% of the SaaS connector workload but is missing four
critical capabilities Reviewer 1 flagged:

  - PKCE (authorization_code with code_verifier/code_challenge)
  - authorization_code (server-side exchange)
  - client_credentials (service-to-service, no user)
  - device_code (input-constrained devices; e.g. headless CLIs)

This module provides each flow as a standalone function returning a
credential dict in the same shape as oauth_session expects, so the
session wrapper consumes any of them transparently.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any


# ── PKCE ────────────────────────────────────────────────────────────

def pkce_pair() -> tuple[str, str]:
    """Generate a (verifier, challenge) tuple per RFC 7636.

    Verifier is 43-128 chars URL-safe; challenge is the SHA-256 of the
    verifier, base64url-encoded without padding. Caller stores the
    verifier alongside the in-flight authorization request and sends
    the challenge with the redirect.
    """
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


# ── Helpers ─────────────────────────────────────────────────────────

def _post(token_uri: str, data: dict[str, Any], *, timeout: int = 10) -> dict[str, Any]:
    """POST x-www-form-urlencoded; raise on HTTP error; return JSON."""
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError("requests not installed") from exc
    r = requests.post(token_uri, data=data, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _absorb_token(body: dict[str, Any]) -> dict[str, Any]:
    """Translate a token-endpoint response into the credential dict
    shape the oauth_session expects (access_token / refresh_token /
    expires_at / scope)."""
    out: dict[str, Any] = {"access_token": body["access_token"]}
    if body.get("refresh_token"):
        out["refresh_token"] = body["refresh_token"]
    if body.get("expires_in"):
        out["expires_at"] = time.time() + int(body["expires_in"])
    elif body.get("expires_at"):
        out["expires_at"] = body["expires_at"]
    if body.get("scope"):
        out["scope"] = body["scope"]
    return out


# ── Flows ───────────────────────────────────────────────────────────

def client_credentials(
    token_uri: str, client_id: str, client_secret: str,
    *, scope: str | None = None, audience: str | None = None,
) -> dict[str, Any]:
    """RFC 6749 §4.4 — service-to-service auth, no user involvement.

    Used for: headless connectors (Falcon, internal APIs), CI/CD
    integrations, anything where the caller IS the principal.
    """
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if scope:
        data["scope"] = scope
    if audience:
        data["audience"] = audience  # Auth0 / some identity vendors
    return _absorb_token(_post(token_uri, data))


def authorization_request(
    authorize_uri: str, *, client_id: str, redirect_uri: str,
    scope: str | None = None, use_pkce: bool = True,
) -> dict[str, str]:
    """Build the user-redirect URL for RFC 6749 §4.1 step 1.

    Always includes a ``state`` nonce (the CSRF binding RFC 6749 §10.12
    requires) and, by default, a PKCE challenge. The caller stores
    ``state`` (and ``code_verifier`` when PKCE) against the in-flight
    request, and on callback MUST pass them through
    :func:`validate_callback_state` / :func:`authorization_code_exchange`.

    Returns ``{"url", "state"}`` plus ``"code_verifier"`` when PKCE.
    """
    from urllib.parse import urlencode

    state = secrets.token_urlsafe(32)
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if scope:
        params["scope"] = scope
    out: dict[str, str] = {"state": state}
    if use_pkce:
        verifier, challenge = pkce_pair()
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
        out["code_verifier"] = verifier
    sep = "&" if "?" in authorize_uri else "?"
    out["url"] = f"{authorize_uri}{sep}{urlencode(params)}"
    return out


class OAuthStateMismatch(RuntimeError):
    """Callback ``state`` didn't match the stored value — likely a CSRF
    attempt (an authorization response we never initiated)."""


def validate_callback_state(expected_state: str, received_state: str | None) -> None:
    """Constant-time check that a callback's ``state`` matches the value
    issued by :func:`authorization_request`.

    Raise :class:`OAuthStateMismatch` on any mismatch or absence — a code
    arriving with the wrong/missing state must never be exchanged.
    """
    import hmac

    if (
        not expected_state
        or not received_state
        or not hmac.compare_digest(expected_state, received_state)
    ):
        raise OAuthStateMismatch(
            "OAuth callback state mismatch — this authorization response was "
            "not initiated by this F-Pulse instance. Code exchange refused."
        )


def authorization_code_exchange(
    token_uri: str, *, code: str, redirect_uri: str,
    client_id: str, client_secret: str | None = None,
    code_verifier: str | None = None,
    expected_state: str | None = None, received_state: str | None = None,
) -> dict[str, Any]:
    """RFC 6749 §4.1 — exchange auth code for tokens.

    With `code_verifier` set, this is the PKCE variant (recommended
    for public clients per RFC 7636). `client_secret` is optional
    when PKCE is used (public client) but required for confidential
    clients.

    When ``expected_state`` is provided, the callback's ``received_state``
    is verified first (CSRF binding) — a mismatch refuses the exchange.
    Callers wiring a real browser callback should always pass both.
    """
    if expected_state is not None:
        validate_callback_state(expected_state, received_state)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    if code_verifier:
        data["code_verifier"] = code_verifier
    return _absorb_token(_post(token_uri, data))


def device_code_request(
    device_code_uri: str, *, client_id: str, scope: str | None = None,
) -> dict[str, Any]:
    """RFC 8628 §3.1 — start a device-authorization flow.

    Returns the device_code (used to poll), user_code (shown to the
    human), verification_uri (where the human authenticates), and
    `interval` / `expires_in` from the issuer.
    """
    data = {"client_id": client_id}
    if scope:
        data["scope"] = scope
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError("requests not installed") from exc
    r = requests.post(device_code_uri, data=data, timeout=10)
    r.raise_for_status()
    return r.json()


class DeviceCodePending(RuntimeError):
    """Issuer says authorization is still pending — caller polls again."""


class DeviceCodeSlowDown(RuntimeError):
    """Issuer asked us to back off — caller increases poll interval."""


def device_code_poll(
    token_uri: str, *, device_code: str, client_id: str,
    client_secret: str | None = None,
) -> dict[str, Any]:
    """RFC 8628 §3.4 — poll for the user completing authorization.

    Translates the standard error responses into typed exceptions so
    a polling loop is easy to write:
        - `authorization_pending` → DeviceCodePending
        - `slow_down`             → DeviceCodeSlowDown
        - anything else           → bubble up
    On success returns the credential dict ready for storage.
    """
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError("requests not installed") from exc
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    r = requests.post(token_uri, data=data, timeout=10)
    if r.status_code == 200:
        return _absorb_token(r.json())
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    err = body.get("error", "")
    if err == "authorization_pending":
        raise DeviceCodePending(body.get("error_description", err))
    if err == "slow_down":
        raise DeviceCodeSlowDown(body.get("error_description", err))
    r.raise_for_status()
    raise RuntimeError(f"unexpected device_code response: {body or r.text}")
