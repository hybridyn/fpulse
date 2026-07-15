"""Lookback-window helper for incremental cursors (2026-06-08, B1 of
backfill-ux-1.2).

Late-arriving data is the classic problem with strict-cursor
incremental sync. Source updates a row at 09:59 (with clock skew on
the source). F-Pulse reads at 10:00 with cursor=10:00. The next
incremental read starts strictly after 10:00 and misses the 09:59
row.

This module ships a single pure function that the executor calls just
before issuing the incremental SELECT - it shifts the cursor back by
``lookback_seconds`` so the next read re-covers that window. The
dedupe store (sinks/dedupe_store.py) handles the re-read overlap so
downstream sees each row once.

Default lookback = 0 (strict cursor, current behaviour). Operators
opt into a lookback in the source node's Sync Mode panel.

# Why a separate module

Sync-mode declarations live in nodes/_sync_mode_decl.py (UI shape).
Cursor persistence lives in engine/sync_state_store.py. Neither is
the right home for the lookback math:
  - _sync_mode_decl.py is UI metadata; no logic
  - sync_state_store.py persists; doesn't compute the next read window

This module is the seam between those two: takes the persisted cursor
+ the configured lookback, returns the effective lower-bound the
SELECT should use.

# Cursor types

Watermarks come in three flavours across F-Pulse connectors:
  - **datetime** (ISO-8601 strings or float seconds)
  - **integer** (bigint identity columns, row counts)
  - **string** (opaque token from a source API)

Lookback only makes sense for the first two (numeric / time-based).
For string cursors the helper returns the cursor unchanged and logs
once that lookback was ignored.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Union

logger = logging.getLogger(__name__)

CursorValue = Union[str, int, float, None]


def apply_lookback(
    cursor: CursorValue,
    *,
    lookback_seconds: int = 0,
) -> CursorValue:
    """Return the effective lower-bound the next incremental read
    should use, given the persisted cursor + the configured lookback.

    Behaviour:
      * lookback_seconds <= 0     -> cursor returned unchanged
      * cursor is None / empty   -> None (first run; no incremental filter)
      * cursor is integer        -> cursor - lookback_seconds (floored at 0)
      * cursor is float          -> cursor - lookback_seconds (floored at 0)
      * cursor is ISO-8601 str   -> parsed, shifted back, re-formatted
      * cursor is opaque string  -> returned unchanged (log once)

    Examples::

        apply_lookback(None, lookback_seconds=3600)
          -> None  (first run; nothing to shift)

        apply_lookback(1717000000.0, lookback_seconds=3600)
          -> 1716996400.0

        apply_lookback("2026-06-08T10:00:00+00:00", lookback_seconds=86400)
          -> "2026-06-07T10:00:00+00:00"

        apply_lookback("opaque_token_xyz", lookback_seconds=300)
          -> "opaque_token_xyz"  (string, can't shift)
    """
    if lookback_seconds <= 0:
        return cursor
    if cursor is None or cursor == "":
        return None

    # Integer cursor (bigint identity / row count) - integer arithmetic
    if isinstance(cursor, int) and not isinstance(cursor, bool):
        return max(0, cursor - lookback_seconds)

    # Float cursor (unix epoch seconds)
    if isinstance(cursor, float):
        return max(0.0, cursor - lookback_seconds)

    # String cursor - try ISO-8601 parse; fall through if not parseable
    if isinstance(cursor, str):
        parsed = _try_parse_iso(cursor)
        if parsed is not None:
            shifted = parsed - timedelta(seconds=lookback_seconds)
            return _format_iso(shifted, original=cursor)
        # Opaque string token from a source API - lookback doesn't
        # apply. Log once at INFO and pass through unchanged.
        _warn_unshiftable_once(cursor)
        return cursor

    return cursor


# ── Internals ────────────────────────────────────────────────────────


_warned_for: set[str] = set()


def _warn_unshiftable_once(cursor: str) -> None:
    """Log once per process that a string cursor isn't shift-able.
    Avoids spamming the log on every incremental run."""
    sample = cursor[:32]
    if sample in _warned_for:
        return
    _warned_for.add(sample)
    logger.info(
        "Lookback configured but cursor is opaque string (sample=%r); "
        "lookback ignored. Use a datetime or integer cursor column to "
        "enable lookback for late-arriving data.",
        sample,
    )


def _try_parse_iso(value: str) -> datetime | None:
    """Tolerant ISO-8601 parser. Accepts trailing Z + missing tz."""
    if not value:
        return None
    s = value.strip()
    # Bare integer in a string ("1717000000") - treat as epoch seconds
    if s.isdigit():
        try:
            return datetime.fromtimestamp(int(s), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    # Tolerate trailing Z (Python < 3.11 fromisoformat rejected it)
    candidate = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _format_iso(dt: datetime, *, original: str) -> str:
    """Re-format a shifted datetime back to the original cursor's
    flavour: keep the trailing Z if the input had one; otherwise emit
    the explicit timezone offset."""
    if original.endswith("Z"):
        # Original was ZULU - emit Z too
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % (dt.microsecond // 1000)
    return dt.isoformat()
