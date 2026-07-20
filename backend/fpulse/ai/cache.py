"""
L1 in-memory cache for AI-side derived summaries.

Per docs/performance.md §6 and ai-boundary-contract.md §7:
  - Bounded by entry count (LRU) AND wall-clock TTL
  - Strict tenant key prefix enforced — `{tenant_id}:{cache_type}:{key}`
  - Caches only safe derived summaries, NOT raw sensitive payloads
  - L2 (Redis) and L3 (SQLite) wrappers ship in Tier B; this is L1 only

Default TTLs (from project_fpulse_ai_step0_locks.md §4) are surfaced as
constants so callers don't hardcode magic numbers.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

# Default TTLs (seconds) per cache type — see performance.md §6.
TTL_SCHEMA_INTROSPECTION = 6 * 3600
TTL_PIPELINE_SUMMARY = 1 * 3600
TTL_METRICS_SUMMARY = 5 * 60
TTL_CONNECTOR_METADATA = 24 * 3600
TTL_CONNECTION_HEALTH = 15 * 60
TTL_LLM_RESPONSE = 30 * 60

# Key prefix format: {tenant_id}:{cache_type}:{key}
# Enforced via assert + regex check on every set. Cross-tenant key collision
# is a CI assertion failure (Rule 8).
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+:[A-Za-z0-9_-]+:.+")


class TenantKeyError(ValueError):
    """Raised when a cache key is missing the {tenant_id}:{cache_type}: prefix."""


@dataclass
class _Entry:
    value: Any
    expires_at: float


class L1Cache:
    """Bounded LRU + TTL cache. Per-process, per-worker.

    Not thread-safe — F-Pulse's async I/O model means single-threaded per
    worker; if that ever changes, wrap operations in a lock.

    Memory is bounded by `max_entries` (LRU eviction) AND by `max_bytes`
    if provided (best-effort; we measure repr() length). Default 5 MB cap
    matches the per-worker target in docs/performance.md.
    """

    def __init__(
        self,
        *,
        max_entries: int = 1024,
        max_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        if max_bytes < 1024:
            raise ValueError("max_bytes must be >= 1024")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._store: OrderedDict[str, _Entry] = OrderedDict()
        self._approx_bytes = 0

    def _now(self) -> float:
        return time.monotonic()

    def get(self, key: str) -> Any | None:
        """Return cached value or None on miss / expiration."""
        self._validate_key(key)
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._now():
            self._evict(key)
            return None
        # Move to end (LRU bookkeeping)
        self._store.move_to_end(key)
        return entry.value

    def set(self, key: str, value: Any, *, ttl_seconds: int) -> None:
        """Insert or update. Enforces tenant key prefix."""
        self._validate_key(key)
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")
        # Cost approximation
        size = len(repr(value))
        # Evict existing entry's bytes first (re-insert)
        if key in self._store:
            self._approx_bytes -= len(repr(self._store[key].value))
            del self._store[key]
        self._store[key] = _Entry(value=value, expires_at=self._now() + ttl_seconds)
        self._approx_bytes += size
        # Evict by bytes
        while self._approx_bytes > self._max_bytes and self._store:
            oldest_key, oldest_entry = self._store.popitem(last=False)
            self._approx_bytes -= len(repr(oldest_entry.value))
        # Evict by count
        while len(self._store) > self._max_entries:
            oldest_key, oldest_entry = self._store.popitem(last=False)
            self._approx_bytes -= len(repr(oldest_entry.value))

    def delete(self, key: str) -> None:
        self._validate_key(key)
        self._evict(key)

    def clear(self) -> None:
        self._store.clear()
        self._approx_bytes = 0

    def _evict(self, key: str) -> None:
        entry = self._store.pop(key, None)
        if entry is not None:
            self._approx_bytes -= len(repr(entry.value))

    def _validate_key(self, key: str) -> None:
        if not _KEY_PATTERN.match(key):
            raise TenantKeyError(
                f"Cache key must use '{{tenant_id}}:{{cache_type}}:{{key}}' format; got {key!r}"
            )

    # Diagnostics ----------------------------------------------------

    def __len__(self) -> int:
        return len(self._store)

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self._store),
            "approx_bytes": self._approx_bytes,
            "max_entries": self._max_entries,
            "max_bytes": self._max_bytes,
        }


def make_key(tenant_id: str, cache_type: str, key: str) -> str:
    """Helper for callers — produces a properly-prefixed key.

    Raises ValueError on inputs containing `:` to prevent injection.
    """
    for part_name, part in (("tenant_id", tenant_id), ("cache_type", cache_type)):
        if not part:
            raise ValueError(f"{part_name} must not be empty")
        if ":" in part:
            raise ValueError(f"{part_name} must not contain ':' — got {part!r}")
    return f"{tenant_id}:{cache_type}:{key}"
