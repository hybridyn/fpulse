"""Window enumeration for backfills.

Given a start date, end date, and granularity, return the list of
[start, end) intervals the backfill should iterate over. Boundaries are
closed-open: window N's end equals window N+1's start so a daily
backfill of 2026-01-01 → 2026-01-03 produces three windows
[Jan 1, Jan 2), [Jan 2, Jan 3), [Jan 3, Jan 4). Inclusive end-of-range
is honoured by including the day the user named.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import WindowSize


@dataclass(frozen=True)
class WindowBounds:
    start: datetime  # UTC, inclusive
    end: datetime    # UTC, exclusive


def _parse(value: str) -> datetime:
    """Parse an ISO date or ISO timestamp; tolerate the trailing Z."""
    if not value:
        raise ValueError("empty date")
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"could not parse date {value!r}: {exc}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _add_months(dt: datetime, months: int) -> datetime:
    """Add N calendar months to dt, clamping to the end of month if needed."""
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    # Calendar end-of-month clamp — Feb won't have day 31.
    day = dt.day
    while day > 28:
        try:
            return dt.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1
    return dt.replace(year=year, month=month, day=day)


def generate_windows(
    start_date: str,
    end_date: str,
    window_size: WindowSize,
    *,
    window_size_hours: int = 24,
) -> list[WindowBounds]:
    """Return the list of [start, end) windows covering [start_date, end_date].

    The end_date is treated INCLUSIVELY: a daily backfill of
    2026-01-01 → 2026-01-03 produces three windows
    (2026-01-01..02, 2026-01-02..03, 2026-01-03..04).

    Args:
      start_date: ISO date or timestamp
      end_date: ISO date or timestamp (inclusive)
      window_size: granularity
      window_size_hours: only honoured when ``window_size == CUSTOM``;
        must be a positive integer.

    Raises:
      ValueError: if dates are malformed or end_date < start_date or
        a CUSTOM size <= 0.
    """
    start = _parse(start_date)
    end = _parse(end_date)
    if end < start:
        raise ValueError("end_date must be >= start_date")

    # Make the end-of-range inclusive. When the user passes a bare date
    # ("2026-01-01") we treat it as "include the whole day" — bump the
    # exclusive end-of-range to the next midnight. That makes
    # "2026-01-01 → 2026-01-01" produce one daily window OR 24 hourly
    # windows depending on granularity. When the user passes an ISO
    # timestamp ("2026-01-01T15:00"), preserve their time component but
    # still round up to the next granularity boundary so the named
    # instant is included.
    had_time_component = (
        "T" in start_date or "T" in end_date
        or any(ch in (start_date + end_date) for ch in ":")
    )
    if window_size == WindowSize.DAILY:
        end_inclusive = end.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    elif window_size == WindowSize.HOURLY:
        if had_time_component:
            end_inclusive = end.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            # Bare date → cover the whole day with 24 hourly windows.
            end_inclusive = end.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    elif window_size == WindowSize.WEEKLY:
        end_inclusive = end.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    elif window_size == WindowSize.MONTHLY:
        end_inclusive = end.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    else:  # CUSTOM
        if window_size_hours <= 0:
            raise ValueError("window_size_hours must be > 0 when window_size=custom")
        if had_time_component:
            end_inclusive = end + timedelta(hours=1)
        else:
            # Bare date with custom hours → cover the named day.
            end_inclusive = end.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    # Normalise start to the granularity's natural boundary so windows
    # line up tidily — Jan 1 daily starts at midnight even if the input
    # had a time-of-day. Custom/hourly preserve the input minute=0.
    if window_size == WindowSize.DAILY:
        cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
    elif window_size == WindowSize.HOURLY:
        cursor = start.replace(minute=0, second=0, microsecond=0)
    elif window_size == WindowSize.WEEKLY:
        cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
    elif window_size == WindowSize.MONTHLY:
        cursor = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # CUSTOM
        cursor = start.replace(minute=0, second=0, microsecond=0)

    windows: list[WindowBounds] = []
    safety = 0
    while cursor < end_inclusive:
        if window_size == WindowSize.DAILY:
            nxt = cursor + timedelta(days=1)
        elif window_size == WindowSize.HOURLY:
            nxt = cursor + timedelta(hours=1)
        elif window_size == WindowSize.WEEKLY:
            nxt = cursor + timedelta(days=7)
        elif window_size == WindowSize.MONTHLY:
            nxt = _add_months(cursor, 1)
        else:  # CUSTOM
            nxt = cursor + timedelta(hours=window_size_hours)
        windows.append(WindowBounds(start=cursor, end=nxt))
        cursor = nxt
        safety += 1
        if safety > 100_000:
            raise ValueError(
                "window count exceeds safety cap (100k) — pick a coarser window_size"
            )
    return windows
