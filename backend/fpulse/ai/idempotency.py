"""
Idempotency keys + dedup store for agent write tools.

Per locked format (`project_fpulse_ai_step0_locks.md` §2):
  {tier}.{user_id}.{action}.{target_id}.{semver}

For agent-driven writes the LLM doesn't know `target_id` ahead of time —
we synthesize it as the SHA-256 (truncated) of the canonical-JSON inputs.
That makes a re-emitted identical call return the cached result instead
of executing twice (true idempotency, not just dedup — per round-3 reviewer
"Idempotency at registry level — auto-generate keys from request hash").

Default TTL 24h matches the locks doc. Memory-bounded by max_entries with
oldest-expiry eviction when over cap.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TTL_SECONDS = 24 * 3600
DEFAULT_VERSION = "v1"


def generate_key(
    *,
    tier: str,
    user_id: str | None,
    action: str,
    payload: dict[str, Any],
    version: str = DEFAULT_VERSION,
) -> str:
    """Produce `{tier}.{user_id}.{action}.{payload_hash}.{semver}`.

    Anonymous users get user_id="anonymous" so the format stays well-formed.
    Payload is canonicalised (sorted keys, default=str) before hashing so
    reorderings of the same logical input collide.
    """
    uid = user_id or "anonymous"
    canonical = json.dumps(payload, sort_keys=True, default=str)
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{tier}.{uid}.{action}.{payload_hash}.{version}"


@dataclass
class _Slot:
    result: Any
    expires_at: float


@dataclass
class IdempotencyStore:
    """Per-process idempotency cache. Bounded + TTL'd.

    Not thread-safe — F-Pulse's async I/O model means single-threaded per
    worker. If that ever changes wrap operations in a lock.

    For multi-worker deployments (Plus tier with worker pool) this should
    be backed by Redis instead — same shape, distributed. That swap-in is
    a Tier B item; today we ship the in-process default.
    """

    max_entries: int = 4096
    _store: dict[str, _Slot] = field(default_factory=dict)

    def _now(self) -> float:
        return time.monotonic()

    def get(self, key: str) -> tuple[bool, Any]:
        """Return (hit, cached_result). Hit=False on miss or expiration."""
        slot = self._store.get(key)
        if slot is None:
            return (False, None)
        if slot.expires_at <= self._now():
            del self._store[key]
            return (False, None)
        return (True, slot.result)

    def put(
        self,
        key: str,
        result: Any,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")
        if len(self._store) >= self.max_entries and key not in self._store:
            # Evict the soonest-to-expire entry to make room.
            oldest = min(self._store, key=lambda k: self._store[k].expires_at)
            del self._store[oldest]
        self._store[key] = _Slot(result=result, expires_at=self._now() + ttl_seconds)

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: str) -> bool:
        hit, _ = self.get(key)
        return hit


# Per-process default. Tests construct their own.
_DEFAULT_STORE: IdempotencyStore | None = None


def default_store() -> IdempotencyStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = IdempotencyStore()
    return _DEFAULT_STORE


def reset_default_store_for_tests() -> None:
    global _DEFAULT_STORE
    _DEFAULT_STORE = None
