"""
Handle / reference store for the agent loop.

Implements the "pass IDs not payloads" pattern from
docs/ai-boundary-contract.md §6 and project_fpulse_ai_operational_architecture.md
("Pass IDs/handles, not payloads").

Pattern:
  big_payload = {"rows": [...50KB...]}
  handle = handle_store.put(tenant_id, big_payload, ttl_seconds=600)
  # send {"sample_id": handle} to the agent instead of big_payload
  # later, a tool resolves it back:
  payload = handle_store.get(tenant_id, handle)

Tenant scoping is mandatory — get() returns None if the tenant_id does not
match the one used at put() time. Cross-tenant access is impossible by API
shape, not just convention.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

DEFAULT_TTL_SECONDS = 600  # 10 min — covers an interactive agent run


@dataclass
class _Slot:
    tenant_id: str
    value: Any
    expires_at: float


class HandleStore:
    """Per-process handle store. Tenant-scoped. TTL-bounded.

    Not for durable storage — process restart loses handles, and that's
    intended (a stuck agent can't resume against stale data).
    """

    def __init__(self, *, max_entries: int = 4096) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max_entries = max_entries
        self._store: dict[str, _Slot] = {}

    def _now(self) -> float:
        return time.monotonic()

    def _purge_expired(self) -> None:
        now = self._now()
        expired = [k for k, s in self._store.items() if s.expires_at <= now]
        for k in expired:
            del self._store[k]

    def put(
        self,
        tenant_id: str,
        value: Any,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> str:
        """Store value, return opaque handle id. Tenant-scoped.

        Raises ValueError on empty tenant_id or non-positive TTL.
        """
        if not tenant_id:
            raise ValueError("tenant_id must not be empty")
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")

        self._purge_expired()
        # Bound entries — drop oldest by expiration if over cap
        if len(self._store) >= self._max_entries:
            oldest_key = min(self._store, key=lambda k: self._store[k].expires_at)
            del self._store[oldest_key]

        handle = secrets.token_urlsafe(16)
        self._store[handle] = _Slot(
            tenant_id=tenant_id,
            value=value,
            expires_at=self._now() + ttl_seconds,
        )
        return handle

    def get(self, tenant_id: str, handle: str) -> Any | None:
        """Return value if handle exists, not expired, AND belongs to tenant.

        Tenant mismatch is silent — same return as missing handle. This
        prevents leakage of "this handle exists but you can't see it" via
        timing or error-message analysis.
        """
        slot = self._store.get(handle)
        if slot is None:
            return None
        if slot.expires_at <= self._now():
            del self._store[handle]
            return None
        if slot.tenant_id != tenant_id:
            return None
        return slot.value

    def delete(self, tenant_id: str, handle: str) -> bool:
        """Delete the handle. Returns True on success, False on miss / wrong tenant.

        Tenant mismatch returns False (and does NOT delete) for the same
        reason as get().
        """
        slot = self._store.get(handle)
        if slot is None:
            return False
        if slot.tenant_id != tenant_id:
            return False
        del self._store[handle]
        return True

    def __len__(self) -> int:
        return len(self._store)

    def stats(self) -> dict[str, int]:
        return {"entries": len(self._store), "max_entries": self._max_entries}
