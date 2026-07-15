"""Per-connection OAuth health metrics.

Reviewer 1 explicitly asked for an "auth health status" surface so
operators can see at a glance which connections are happy, which are
about to expire, and which have failed recent refreshes:

    {
      "auth_status": "healthy",
      "expires_in": 3200,
      "last_refresh": ...,
      "last_failure": ...
    }

This module is the storage + classification layer. The session
wrapper publishes events here on every refresh / failure; the API
endpoint reads the registry to render badges in the operator UI.

The registry is process-local in OSS. Multi-worker deployments would
need a side channel (Redis hash keyed by connection_id) — that's a
Plus-tier concern, not in scope here.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


HealthStatus = Literal["healthy", "expiring_soon", "stale", "failed", "unknown"]

# Threshold for "expiring soon" — UI surfaces a warning chip below this.
EXPIRING_SOON_S = 5 * 60


@dataclass
class AuthHealth:
    """Live snapshot of one connection's OAuth state.

    Updated by OAuthSession on refresh attempts (success or failure)
    and on every authenticated request. The `status` field is
    derived from the other fields, not stored — see `derive_status`.
    """
    connection_id: str
    flow: str = "refresh_token"      # "refresh_token" | "client_credentials" | "authorization_code" | "device_code"
    last_refresh_at: float | None = None
    last_failure_at: float | None = None
    last_failure_reason: str | None = None
    refresh_count: int = 0
    failure_count: int = 0
    expires_at: float | None = None
    scopes: list[str] = field(default_factory=list)

    def time_to_expiry(self) -> float | None:
        if self.expires_at is None:
            return None
        return self.expires_at - time.time()

    def derive_status(self) -> HealthStatus:
        """Derive a single-word status from the metrics. The
        priority order matters — recent failures override expiry
        info because a failed refresh means we can't trust the
        expires_at we last wrote down."""
        ttl = self.time_to_expiry()
        # Recent failure with no successful refresh after it = failed.
        if (self.last_failure_at and
                (self.last_refresh_at is None or
                 self.last_refresh_at < self.last_failure_at)):
            return "failed"
        if ttl is None and self.last_refresh_at is None:
            return "unknown"
        if ttl is not None and ttl < 0:
            return "stale"
        if ttl is not None and ttl < EXPIRING_SOON_S:
            return "expiring_soon"
        return "healthy"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["time_to_expiry"] = self.time_to_expiry()
        d["status"] = self.derive_status()
        return d


class AuthHealthRegistry:
    """Process-local map of connection_id → AuthHealth.

    Thread-safe (the OAuthSession may be driven from multiple async
    workers in the extraction engine). Subscribers can be added for
    fan-out to a future pubsub bus when this needs to go cross-worker.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, AuthHealth] = {}

    # ── Mutations ────────────────────────────────────────────────────

    def record_refresh_success(self, connection_id: str, *,
                                  flow: str = "refresh_token",
                                  expires_at: float | None = None,
                                  scopes: list[str] | None = None) -> AuthHealth:
        with self._lock:
            h = self._states.setdefault(connection_id,
                                          AuthHealth(connection_id=connection_id, flow=flow))
            h.flow = flow
            h.last_refresh_at = time.time()
            h.refresh_count += 1
            if expires_at is not None:
                h.expires_at = expires_at
            if scopes is not None:
                h.scopes = list(scopes)
            return h

    def record_refresh_failure(self, connection_id: str, *, reason: str,
                                  flow: str = "refresh_token") -> AuthHealth:
        with self._lock:
            h = self._states.setdefault(connection_id,
                                          AuthHealth(connection_id=connection_id, flow=flow))
            h.flow = flow
            h.last_failure_at = time.time()
            h.last_failure_reason = reason[:300]  # cap the stored string
            h.failure_count += 1
            return h

    def reset(self, connection_id: str) -> None:
        with self._lock:
            self._states.pop(connection_id, None)

    # ── Reads ────────────────────────────────────────────────────────

    def get(self, connection_id: str) -> AuthHealth | None:
        with self._lock:
            return self._states.get(connection_id)

    def list_all(self) -> list[AuthHealth]:
        with self._lock:
            return list(self._states.values())


# ── Module singleton ────────────────────────────────────────────────

_REGISTRY: AuthHealthRegistry | None = None


def get_registry() -> AuthHealthRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = AuthHealthRegistry()
    return _REGISTRY


def reset_registry_for_tests() -> None:
    global _REGISTRY
    _REGISTRY = None
