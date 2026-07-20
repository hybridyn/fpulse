"""OAuth2 refresh-token wrapper for catalog providers.

Lazy refresh at request time, 5-minute expiry buffer, single retry on
401. Tokens are read from and written back to the credential store
(Vault when available, Connection.config as a graceful fallback in
OSS deployments without the Plus Vault stack).

The provider doesn't see refresh logic — it gets a `requests.Session`-
shaped object and just calls `.get(...)` / `.post(...)`. Auth is
attached automatically.

A connection's OAuth state lives under these keys (either in the
secret store keyed by `vault_key`, or directly in `Connection.config`
when Vault is absent):
    - access_token            (required)
    - refresh_token           (required for refresh; absent → static token)
    - token_uri               (where to POST refresh)
    - client_id, client_secret
    - expires_at              (unix seconds; absent → assume expired)
    - scope                   (optional, only used on refresh)

Non-secret OAuth metadata (token_uri, scopes, auth_type) MAY live in
Connection.config without going through Vault — only the refresh
token / client_secret / access_token are secrets.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from fpulse.connections.oauth_health import AuthHealthRegistry, get_registry

logger = logging.getLogger(__name__)

REFRESH_BUFFER_S = 5 * 60  # refresh if token expires within 5 minutes


class OAuthSession:
    """Thin wrapper around requests.Session with auto-refresh.

    Args:
      get_credentials: callable returning the current credential dict
        (e.g. `lambda: vault.get(connection.vault_key)`).
      put_credentials: callable accepting the updated credential dict
        and persisting it back. Used after a successful refresh.
      connection_id: optional, when provided the session publishes
        success/failure events to the AuthHealthRegistry so the
        operator UI can render a per-connection status badge.
      registry: injected for tests; defaults to the module singleton.
    """

    def __init__(
        self,
        get_credentials: Callable[[], dict[str, Any]],
        put_credentials: Callable[[dict[str, Any]], None],
        *,
        connection_id: str | None = None,
        registry: AuthHealthRegistry | None = None,
    ) -> None:
        try:
            import requests  # type: ignore
        except ImportError as exc:
            raise RuntimeError("requests not installed") from exc
        self._requests = requests
        self._session = requests.Session()
        self._get_creds = get_credentials
        self._put_creds = put_credentials
        self._connection_id = connection_id
        self._registry = registry or get_registry()

    # ── Token lifecycle ──

    def _is_expired(self, creds: dict[str, Any]) -> bool:
        exp = creds.get("expires_at")
        if not exp:
            return True  # unknown expiry → treat as expired, force refresh
        try:
            return time.time() + REFRESH_BUFFER_S >= float(exp)
        except (TypeError, ValueError):
            return True

    def _refresh(self, creds: dict[str, Any]) -> dict[str, Any]:
        """Refresh tokens via whichever flow the credentials declare.

        Supported flows:
          - refresh_token (default — when `refresh_token` is present)
          - client_credentials (when `flow="client_credentials"` or
            no refresh_token but client_id/client_secret are set)

        Publishes to the health registry on both success and failure
        so the operator UI sees rotation events in real time.
        """
        token_uri = creds.get("token_uri")
        client_id = creds.get("client_id")
        client_secret = creds.get("client_secret")
        flow = creds.get("flow") or (
            "refresh_token" if creds.get("refresh_token") else "client_credentials"
        )

        if not (token_uri and client_id):
            err = "OAuth refresh requires token_uri and client_id"
            self._publish_failure(flow, err)
            raise RuntimeError(err)

        if flow == "refresh_token":
            refresh_token = creds.get("refresh_token")
            if not refresh_token:
                err = "refresh_token grant requires refresh_token in credentials"
                self._publish_failure(flow, err)
                raise RuntimeError(err)
            data = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            }
        elif flow == "client_credentials":
            data = {
                "grant_type": "client_credentials",
                "client_id": client_id,
            }
        else:
            err = f"unsupported flow: {flow!r}"
            self._publish_failure(flow, err)
            raise RuntimeError(err)

        if client_secret:
            data["client_secret"] = client_secret
        if creds.get("scope"):
            data["scope"] = creds["scope"]
        if creds.get("audience"):
            data["audience"] = creds["audience"]  # Auth0-family

        try:
            r = self._requests.post(token_uri, data=data, timeout=10)
            r.raise_for_status()
            body = r.json()
        except Exception as exc:  # noqa: BLE001
            self._publish_failure(flow, f"{type(exc).__name__}: {exc}")
            raise

        new_creds = dict(creds)
        new_creds["access_token"] = body["access_token"]
        # Some providers rotate refresh_token; respect that.
        if body.get("refresh_token"):
            new_creds["refresh_token"] = body["refresh_token"]
        if body.get("expires_in"):
            new_creds["expires_at"] = time.time() + int(body["expires_in"])
        elif body.get("expires_at"):
            new_creds["expires_at"] = body["expires_at"]
        if body.get("scope"):
            new_creds["scope"] = body["scope"]

        # Persist back so the next call uses the fresh token without
        # repeating the dance.
        try:
            self._put_creds(new_creds)
        except Exception as exc:  # noqa: BLE001 — never fail a request because we couldn't persist
            logger.warning("Failed to persist refreshed OAuth token: %s", exc)

        # Publish success to the health registry.
        self._publish_success(flow, new_creds)
        return new_creds

    # ── Health publishing ────────────────────────────────────────────

    def _publish_success(self, flow: str, creds: dict[str, Any]) -> None:
        if not self._connection_id:
            return
        try:
            scopes = creds.get("scope", "")
            scope_list = scopes.split() if isinstance(scopes, str) and scopes else []
            self._registry.record_refresh_success(
                self._connection_id,
                flow=flow,
                expires_at=creds.get("expires_at"),
                scopes=scope_list,
            )
        except Exception:  # noqa: BLE001 — health is best-effort
            logger.exception("auth-health publish (success) failed")

    def _publish_failure(self, flow: str, reason: str) -> None:
        if not self._connection_id:
            return
        try:
            self._registry.record_refresh_failure(
                self._connection_id, reason=reason, flow=flow,
            )
        except Exception:  # noqa: BLE001
            logger.exception("auth-health publish (failure) failed")

    def _ensure_fresh(self) -> dict[str, Any]:
        creds = self._get_creds()
        if not creds.get("refresh_token"):
            return creds  # static token — nothing to refresh
        if self._is_expired(creds):
            creds = self._refresh(creds)
        return creds

    # ── HTTP shim ──

    def request(self, method: str, url: str, **kwargs: Any):
        creds = self._ensure_fresh()
        headers = dict(kwargs.pop("headers", None) or {})
        if creds.get("access_token"):
            headers.setdefault("Authorization", f"Bearer {creds['access_token']}")
        kwargs.setdefault("timeout", 10)
        r = self._session.request(method, url, headers=headers, **kwargs)
        # If we still got 401, force a single refresh + retry — covers
        # the case where the server rotated keys early or our cached
        # expiry was wrong.
        if r.status_code == 401 and creds.get("refresh_token"):
            try:
                creds = self._refresh(creds)
            except Exception:  # noqa: BLE001
                return r
            headers["Authorization"] = f"Bearer {creds['access_token']}"
            r = self._session.request(method, url, headers=headers, **kwargs)
        return r

    def get(self, url: str, **kwargs: Any):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass
