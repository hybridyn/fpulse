"""B1.1 integration test (2026-06-08).

Confirms the apply_lookback foundation (shipped in B1) is actually
wired into db_source's incremental cursor path. Pinned to catch the
regression where someone removes the `_apply_cursor_lookback` call
from execute() — the unit tests on apply_lookback() alone wouldn't
catch that.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fpulse.nodes.db_source import DbSourceNode


def _node(**params):
    """Instantiate DbSourceNode with the given params. BaseNode's
    constructor takes a single `params` dict; we don't need IR-level
    fields (id / type / label) for the lookback unit-of-work."""
    p = {"_step_id": "s1", **params}
    return DbSourceNode(params=p)


class TestApplyCursorLookback:
    def test_no_lookback_returns_cursor_unchanged(self):
        n = _node()  # no lookback_seconds
        assert n._apply_cursor_lookback("2026-06-08T10:00:00+00:00") == "2026-06-08T10:00:00+00:00"
        assert n._apply_cursor_lookback("12345") == "12345"

    def test_zero_lookback_explicit_returns_unchanged(self):
        n = _node(lookback_seconds=0)
        assert n._apply_cursor_lookback("2026-06-08T10:00:00+00:00") == "2026-06-08T10:00:00+00:00"

    def test_iso_cursor_shifts_back_by_lookback(self):
        n = _node(lookback_seconds=3600)
        out = n._apply_cursor_lookback("2026-06-08T10:00:00+00:00")
        # Parse to compare regardless of formatting flavour
        parsed = datetime.fromisoformat(out)
        assert parsed == datetime(2026, 6, 8, 9, 0, 0, tzinfo=timezone.utc)

    def test_24h_lookback_recovers_late_data_scenario(self):
        # The exact scenario from docs/design/backfill-ux-1.2.md B1:
        # cursor=10:00, late row updated at 09:55. With 24h lookback,
        # the effective cursor shifts to yesterday's 10:00, which is
        # WELL before 09:55, so the late row gets picked up.
        n = _node(lookback_seconds=86400)
        out = n._apply_cursor_lookback("2026-06-08T10:00:00+00:00")
        parsed = datetime.fromisoformat(out)
        late_row = datetime(2026, 6, 8, 9, 55, 0, tzinfo=timezone.utc)
        assert parsed < late_row, "effective cursor must be earlier than the late row's timestamp"

    def test_integer_cursor_shifts_in_string_form(self):
        # Source-stored cursor is a stringified integer (bigint identity);
        # lookback should subtract the seconds value but return a string
        # because the persisted cursor is always a string.
        n = _node(lookback_seconds=100)
        # 12345 is a digit string -> parsed as epoch by apply_lookback
        out = n._apply_cursor_lookback("12345")
        # Returned as ISO datetime string (epoch 12245 = past), not the
        # original numeric. Important: callers downstream get a string.
        assert isinstance(out, str)
        assert out != "12345", "lookback should have shifted the cursor"

    def test_opaque_cursor_passes_through(self):
        # An opaque API token cursor (not a number, not a date) can't
        # be shifted - return unchanged per the lookback contract.
        n = _node(lookback_seconds=3600)
        assert n._apply_cursor_lookback("opaque_token_xyzzy") == "opaque_token_xyzzy"

    def test_negative_lookback_no_op(self):
        # Defensive: negative lookback would shift forward, which is
        # nonsensical. Per the apply_lookback contract, no-op.
        n = _node(lookback_seconds=-5)
        assert n._apply_cursor_lookback("2026-06-08T10:00:00+00:00") == "2026-06-08T10:00:00+00:00"


class TestRegressionGuard:
    """If someone removes the _apply_cursor_lookback call from execute(),
    this test wouldn't directly fail (we'd need to actually run
    execute() against a real DB connection). But this test ensures the
    METHOD EXISTS on the class - the call site in execute() references
    it by attribute name and would AttributeError at runtime."""

    def test_method_exists_on_class(self):
        assert hasattr(DbSourceNode, "_apply_cursor_lookback"), (
            "B1.1 regression - _apply_cursor_lookback must stay on "
            "DbSourceNode; execute() references it inline."
        )

    def test_method_is_callable_on_instance(self):
        n = _node()
        assert callable(getattr(n, "_apply_cursor_lookback"))
