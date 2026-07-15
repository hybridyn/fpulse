"""Bulk-load entry point.

`bulk_load(request)` looks up the plugin for `request.conn_type`, validates
the request, and dispatches. Failures are normalized into
`BulkLoaderNotAvailable` (no plugin / driver missing) vs raw exceptions
(plugin-side failure: connection error, table doesn't exist, etc.) so the
caller can decide whether to fall back to the row-by-row INSERT path or
surface the error.

This module also ensures all dialect plugins are imported. Plugin modules
register themselves on import; importing the dialects package here is the
seam that wires them into the registry.
"""

from __future__ import annotations

import logging
import time

from . import dialects  # noqa: F401 — import side-effect registers plugins
from .registry import get
from .types import (
    BulkLoaderNotAvailable,
    BulkLoadRequest,
    BulkLoadResult,
)

logger = logging.getLogger(__name__)


def bulk_load(request: BulkLoadRequest) -> BulkLoadResult:
    """Run a bulk-load request through the dialect plugin for
    `request.conn_type`.

    Raises:
      * `BulkLoaderNotAvailable` — no plugin registered, OR plugin
        reports its driver is not importable. Caller MAY fall back to
        row-by-row INSERT.
      * Any other exception — plugin-side failure (auth, network,
        permission, schema mismatch). Caller should NOT silently fall
        back; surface to the operator.
    """
    if request.relation is None:
        raise ValueError("bulk_load: request.relation is required")
    if request.duckdb_conn is None:
        raise ValueError("bulk_load: request.duckdb_conn is required")
    if not request.table:
        raise ValueError("bulk_load: request.table is required")
    if request.mode == "merge" and not request.primary_key:
        raise ValueError(
            "bulk_load: mode='merge' requires request.primary_key"
        )

    plugin = get(request.conn_type)
    if plugin is None:
        raise BulkLoaderNotAvailable(
            f"No bulk-load plugin registered for conn_type='{request.conn_type}'. "
            f"Caller can fall back to row-by-row INSERT."
        )

    try:
        if not plugin.is_available():
            raise BulkLoaderNotAvailable(
                f"Bulk-load plugin for '{request.conn_type}' is registered but "
                f"its driver is not importable. Install the optional dependency "
                f"or fall back to row-by-row INSERT."
            )
    except BulkLoaderNotAvailable:
        raise
    except Exception as exc:  # noqa: BLE001
        # is_available() should not raise but be defensive.
        raise BulkLoaderNotAvailable(
            f"Bulk-load plugin for '{request.conn_type}' is_available() raised: {exc}"
        ) from exc

    t0 = time.perf_counter()
    result = plugin.load(request)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    # Defensive: plugins should set duration_ms themselves but if they forget,
    # fill it in from our wall clock.
    if result.duration_ms <= 0:
        result.duration_ms = elapsed_ms
    if not result.dialect:
        result.dialect = plugin.dialect
    if not result.method:
        result.method = plugin.method
    logger.info(
        "bulk_load: %s rows=%d table=%s dialect=%s method=%s duration_ms=%d",
        "OK", result.rows_loaded, request.table,
        result.dialect, result.method, result.duration_ms,
    )
    return result
