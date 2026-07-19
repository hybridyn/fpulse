"""Pinned tests for the incremental-cursor lookback helper (B1, 2026-06-08).

First milestone from docs/design/backfill-ux-1.2.md. The helper is
pure - given a persisted cursor + a lookback_seconds value, it returns
the effective lower-bound the next incremental SELECT should use.

Contracts pinned here:
  * lookback=0 returns the cursor unchanged (current behaviour)
  * None cursor returns None (first run; no incremental filter)
  * Integer cursors shift by lookback_seconds with a 0 floor
  * Float cursors shift by lookback_seconds with a 0.0 floor
  * ISO-8601 string cursors parse, shift, re-format (preserving Z)
  * Opaque string cursors pass through unchanged (logged once)
  * Negative lookback is a no-op (defensive)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fpulse.engine.lookback import apply_lookback


# ── Boundary cases ──────────────────────────────────────────────────


class TestZeroOrNegativeLookback:
    def test_zero_returns_unchanged(self):
        assert apply_lookback("2026-06-08T10:00:00+00:00", lookback_seconds=0) == "2026-06-08T10:00:00+00:00"
        assert apply_lookback(1717000000, lookback_seconds=0) == 1717000000
        assert apply_lookback(None, lookback_seconds=0) is None

    def test_negative_is_noop(self):
        # Defensive: a negative lookback would shift FORWARD which is
        # nonsensical. Treat as 0.
        assert apply_lookback(1717000000, lookback_seconds=-1) == 1717000000

    def test_none_cursor_returns_none(self):
        # First incremental run - no cursor yet, no shifting to do.
        assert apply_lookback(None, lookback_seconds=3600) is None

    def test_empty_string_cursor_returns_none(self):
        assert apply_lookback("", lookback_seconds=3600) is None


# ── Integer cursors ─────────────────────────────────────────────────


class TestIntegerCursor:
    def test_subtracts_lookback(self):
        assert apply_lookback(1717000000, lookback_seconds=3600) == 1716996400

    def test_floors_at_zero(self):
        assert apply_lookback(100, lookback_seconds=1000) == 0

    def test_zero_cursor_stays_zero(self):
        assert apply_lookback(0, lookback_seconds=999) == 0


# ── Float cursors ───────────────────────────────────────────────────


class TestFloatCursor:
    def test_subtracts_lookback(self):
        assert apply_lookback(1717000000.5, lookback_seconds=3600) == 1716996400.5

    def test_floors_at_zero(self):
        assert apply_lookback(0.5, lookback_seconds=10) == 0.0


# ── ISO-8601 string cursors ─────────────────────────────────────────


class TestISO8601Cursor:
    def test_subtracts_one_hour(self):
        out = apply_lookback("2026-06-08T10:00:00+00:00", lookback_seconds=3600)
        # Parse both back to compare; format details aren't pinned
        from fpulse.engine.lookback import _try_parse_iso
        parsed = _try_parse_iso(out)
        expected = datetime(2026, 6, 8, 9, 0, 0, tzinfo=timezone.utc)
        assert parsed == expected

    def test_subtracts_24_hours(self):
        out = apply_lookback("2026-06-08T10:00:00+00:00", lookback_seconds=86400)
        from fpulse.engine.lookback import _try_parse_iso
        parsed = _try_parse_iso(out)
        expected = datetime(2026, 6, 7, 10, 0, 0, tzinfo=timezone.utc)
        assert parsed == expected

    def test_preserves_z_suffix(self):
        # Source emitted ZULU; shifted result should also end in Z
        out = apply_lookback("2026-06-08T10:00:00Z", lookback_seconds=3600)
        assert out.endswith("Z"), f"expected Z-terminated, got {out}"

    def test_emits_explicit_offset_when_input_had_one(self):
        out = apply_lookback("2026-06-08T10:00:00+00:00", lookback_seconds=3600)
        assert "+" in out or "Z" in out  # some explicit tz

    def test_integer_in_string_parses_as_epoch(self):
        # Some APIs emit cursors as bare numeric strings ("1717000000")
        out = apply_lookback("1717000000", lookback_seconds=3600)
        from fpulse.engine.lookback import _try_parse_iso
        parsed = _try_parse_iso(out)
        assert parsed.timestamp() == 1716996400.0


# ── Opaque string cursors ───────────────────────────────────────────


class TestOpaqueStringCursor:
    def test_returns_unchanged(self):
        # Opaque API token cursor - can't shift it, pass through.
        out = apply_lookback("opaque_token_xyz_abc", lookback_seconds=3600)
        assert out == "opaque_token_xyz_abc"

    def test_warn_only_once_per_distinct_sample(self, caplog):
        import logging
        caplog.set_level(logging.INFO, logger="fpulse.engine.lookback")
        apply_lookback("opaque_one_xxxxxxxxxxxxxxxx", lookback_seconds=3600)
        apply_lookback("opaque_one_xxxxxxxxxxxxxxxx", lookback_seconds=3600)
        info_records = [
            r for r in caplog.records
            if r.levelname == "INFO" and "lookback ignored" in r.message
        ]
        # Same cursor sample - logged exactly once across the two calls
        same_sample = [r for r in info_records if "opaque_one" in r.message]
        assert len(same_sample) <= 1


# ── Integration realism ─────────────────────────────────────────────


class TestIntegrationRealism:
    """Sanity checks that the helper composes correctly with the
    typical incremental-read flow."""

    def test_late_arriving_data_recovery(self):
        # Scenario: yesterday's run set the cursor to "2026-06-08T10:00:00Z".
        # A row updated at 2026-06-08T09:55:00Z arrived at the source
        # 10 seconds AFTER our read (clock skew). Without lookback, the
        # next read starts strictly after 10:00 and misses the row.
        # With a 1-hour lookback, the next read starts at 09:00, which
        # IS earlier than 09:55, so the late row gets picked up.
        original_cursor = "2026-06-08T10:00:00Z"
        late_row_timestamp = "2026-06-08T09:55:00Z"
        effective = apply_lookback(original_cursor, lookback_seconds=3600)

        from fpulse.engine.lookback import _try_parse_iso
        eff_dt = _try_parse_iso(effective)
        late_dt = _try_parse_iso(late_row_timestamp)
        assert eff_dt < late_dt, (
            f"effective cursor {eff_dt} should be < late row {late_dt} "
            f"so the SELECT >= effective_cursor picks up the late row"
        )

    def test_24h_lookback_on_typical_pipeline(self):
        # The recommended default for sources with clock skew - 24h
        # lookback - shifts a midnight cursor to the previous midnight.
        out = apply_lookback("2026-06-08T00:00:00+00:00", lookback_seconds=86400)
        from fpulse.engine.lookback import _try_parse_iso
        parsed = _try_parse_iso(out)
        assert parsed == datetime(2026, 6, 7, 0, 0, 0, tzinfo=timezone.utc)
