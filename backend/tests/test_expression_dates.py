"""Expression-engine date helpers (2026-06-11).

Luxon-style date math used in {{ }} templates. Pins the additions that
made calendar-aware expressions work: startOf/endOf period boundaries and
month/year arithmetic in plus/minus (previously {months}/{years} were
silently ignored). Uses fixed datetimes (not $now) for determinism.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fpulse.expression.resolver import DateHelper, resolve_expressions, ExpressionError


def _d(y, m, d, hh=12, mm=0):
    return DateHelper(datetime(y, m, d, hh, mm, tzinfo=timezone.utc))


def test_start_end_of_month_year():
    d = _d(2026, 6, 8, 14, 30)
    assert d.startOf("month").toFormat("yyyy-MM-dd") == "2026-06-01"
    assert d.endOf("month").toFormat("yyyy-MM-dd") == "2026-06-30"
    assert d.startOf("year").toFormat("yyyy-MM-dd") == "2026-01-01"
    assert d.endOf("year").toFormat("yyyy-MM-dd") == "2026-12-31"


def test_start_end_of_quarter_week():
    d = _d(2026, 5, 20)                       # Q2
    assert d.startOf("quarter").toFormat("yyyy-MM-dd") == "2026-04-01"
    assert d.endOf("quarter").toFormat("yyyy-MM-dd") == "2026-06-30"
    assert d.startOf("week").dt.weekday() == 0   # Monday
    assert d.endOf("week").dt.weekday() == 6     # Sunday


def test_calendar_month_year_arithmetic_with_clamp():
    end_mar = _d(2026, 3, 31)
    assert end_mar.minus({"months": 1}).toFormat("yyyy-MM-dd") == "2026-02-28"  # clamp to Feb
    assert end_mar.plus({"months": 1}).toFormat("yyyy-MM-dd") == "2026-04-30"   # clamp to Apr
    assert end_mar.minus({"years": 1}).toFormat("yyyy-MM-dd") == "2025-03-31"
    # fixed-length parts still work and combine
    assert end_mar.minus({"days": 7}).toFormat("yyyy-MM-dd") == "2026-03-24"


def test_unknown_unit_raises():
    with pytest.raises(ExpressionError, match="startOf"):
        _d(2026, 1, 1).startOf("fortnight")
    with pytest.raises(ExpressionError, match="endOf"):
        _d(2026, 1, 1).endOf("decade")


def test_via_resolver_screenshot_shape():
    """The reported expression intent resolves end-to-end. Both quoted
    ({'days': 7}) and unquoted ({ days: 7 }, see test below) keys work."""
    out = resolve_expressions(
        {
            "from": "{{ $now.startOf('month').toFormat('yyyy-MM-dd') }}",
            "to": "{{ $now.endOf('month').toFormat('yyyy-MM-dd') }}",
            "last7": "{{ $now.minus({'days': 7}).toFormat('yyyy-MM-dd') }}",
        },
        ctx_results={}, node_labels={}, vars_={},
    )
    assert out["from"].endswith("-01")             # first of month
    assert len(out["to"]) == 10 and out["to"][:4].isdigit()
    assert len(out["last7"]) == 10


def test_unquoted_object_keys_are_supported():
    """A2 (2026-06-15): unquoted-key parity — `{ days: 7 }` (unquoted identifier key)
    now resolves the same as `{'days': 7}` (the safe evaluator treats a bare
    Name key as the string key, JS-style)."""
    out = resolve_expressions(
        {
            "unquoted": "{{ $now.minus({ days: 7 }).toFormat('yyyy-MM-dd') }}",
            "quoted": "{{ $now.minus({'days': 7}).toFormat('yyyy-MM-dd') }}",
        },
        ctx_results={}, node_labels={}, vars_={},
    )
    assert out["unquoted"] == out["quoted"]
    assert len(out["unquoted"]) == 10


def test_unquoted_multi_key_object():
    """Multiple unquoted keys + mixed quoted/unquoted both work."""
    out = resolve_expressions(
        {"x": "{{ $now.startOf('day').plus({ days: 1, hours: 2 }).toFormat('yyyy-MM-dd HH') }}"},
        ctx_results={}, node_labels={}, vars_={},
    )
    assert out["x"].endswith(" 02")
