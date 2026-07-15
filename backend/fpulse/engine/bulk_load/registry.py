"""Dialect → plugin registry.

Plugins register themselves at import time via `@register(...)`. The
runner does `get(conn_type)` which returns the plugin instance OR None
when no plugin is available.

The registry is module-global. Tests may need to monkeypatch it; use
`_REGISTRY.clear()` and re-register a fake. Don't expose `_REGISTRY` as
public API — the official surface is `register` / `get` / `available_dialects`.
"""

from __future__ import annotations

import logging
from typing import Callable, TypeVar

from .types import BulkLoaderProtocol

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, BulkLoaderProtocol] = {}

T = TypeVar("T", bound=BulkLoaderProtocol)


def register(plugin: T) -> T:
    """Register a plugin. Idempotent — re-registering with the same dialect
    overwrites the prior entry (useful in tests; never happens in prod)."""
    if not isinstance(plugin, BulkLoaderProtocol):
        raise TypeError(
            f"bulk_load.register: object does not satisfy BulkLoaderProtocol "
            f"(missing dialect/method/is_available/load): {type(plugin).__name__}"
        )
    if plugin.dialect in _REGISTRY:
        logger.debug(
            "bulk_load.register: replacing existing plugin for dialect=%s",
            plugin.dialect,
        )
    _REGISTRY[plugin.dialect] = plugin
    return plugin


def get(conn_type: str) -> BulkLoaderProtocol | None:
    """Return the plugin registered for `conn_type`, or None.

    Does NOT call `is_available()` — callers can decide whether to skip
    the unavailable case or raise BulkLoaderNotAvailable.
    """
    return _REGISTRY.get(conn_type)


def available_dialects() -> list[str]:
    """Sorted list of registered dialects whose drivers are actually
    importable on this host. Useful for the eval harness Gate-1 check."""
    out: list[str] = []
    for dialect, plugin in _REGISTRY.items():
        try:
            if plugin.is_available():
                out.append(dialect)
        except Exception:  # noqa: BLE001 — never let a buggy plugin crash discovery
            logger.exception(
                "bulk_load.available_dialects: %s.is_available() raised",
                dialect,
            )
    return sorted(out)


def _clear_for_tests() -> None:
    """Test-only escape hatch. Don't call from production code."""
    _REGISTRY.clear()
