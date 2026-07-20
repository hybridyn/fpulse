"""Build an authenticated HTTP session from an AuthProfile + resolved config.

The engine doesn't care which auth flavor the source uses — it just
needs something with a `.get()` / `.request()` shape. This module
returns the right session type for the auth declared in the profile:

  - oauth2          → OAuthSession (lazy refresh + 401 retry + vault writeback)
  - api_token       → requests.Session with a static header
  - bearer          → requests.Session with Authorization: Bearer
  - basic           → requests.Session with HTTPBasicAuth
  - none            → plain requests.Session

For oauth2 we expect the config dict (already resolved through
credentials.resolve_credentials) to contain access_token / refresh_token /
client_id / token_uri / expires_at. The wrapper handles refresh on the
first 401 or on imminent expiry.
"""

from __future__ import annotations

import logging
from typing import Any

from fpulse.connections.oauth_session import OAuthSession
from fpulse.connections.runtime import resolve_verify_ssl
from fpulse.extraction.profile import AuthProfile

logger = logging.getLogger(__name__)


def build_session(auth: AuthProfile, config: dict[str, Any],
                   writeback=None):
    """Return a session-shaped object ready to make requests.

    For oauth2 mode, the credential dict is taken by reference: the
    OAuthSession reads it lazily and writes back through `writeback`
    (typically `lambda new: vault.put(connection.vault_key, new)`).
    For all other modes, auth is applied immediately and the session
    is just `requests.Session`.
    """
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError("requests not installed") from exc

    verify = resolve_verify_ssl(config)

    if auth.type == "oauth2":
        # Substrate handles refresh dance; the engine doesn't see it.
        # Pass dict-by-value snapshots so refresh writeback flows through
        # the configured callback, not into our local variable.
        creds_state = {"creds": dict(config)}

        def _get():
            return dict(creds_state["creds"])

        def _put(new):
            creds_state["creds"] = dict(new)
            if writeback is not None:
                try:
                    writeback(new)
                except Exception:  # noqa: BLE001 — never fail a request because writeback failed
                    logger.warning("OAuth writeback failed", exc_info=True)

        sess = OAuthSession(_get, _put)
        # Honour the per-connection TLS toggle for the underlying
        # requests.Session inside the wrapper too — covers self-signed
        # internal IdP token endpoints as well as resource calls.
        try:
            sess._session.verify = verify  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return sess

    sess = requests.Session()
    sess.verify = verify

    if auth.type == "api_token":
        token = config.get("api_token") or config.get("token") or ""
        sess.headers[auth.header] = f"{auth.prefix}{token}"
    elif auth.type == "bearer":
        token = config.get("access_token") or config.get("token") or ""
        sess.headers["Authorization"] = f"Bearer {token}"
    elif auth.type == "basic":
        user = config.get("user") or config.get("username") or ""
        password = config.get("password", "")
        sess.auth = (user, password)  # requests handles the encoding
    elif auth.type == "none":
        pass  # nothing to attach
    elif auth.type in ("iam", "service_account"):
        # These auth modes don't drive HTTP requests directly — they
        # produce signed boto3 / google clients. The engine for those
        # uses the SDK directly, not this session.
        raise ValueError(
            f"auth.type={auth.type} is SDK-driven, not HTTP-session-driven; "
            "use the SDK extraction path instead"
        )
    else:
        raise ValueError(f"Unsupported auth.type: {auth.type!r}")

    return sess
