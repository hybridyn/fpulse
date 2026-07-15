"""Tests for the per-row idempotency keys + checkpoint store used by
F-Pulse OSS external sinks.

Covers two layers in isolation:
  1. ``IdempotencyDedupeStore`` — SQLite-backed seen-marker index.
  2. ``compute_row_hash`` / ``should_skip`` — the per-row policy helpers
     every external sink plugs into.

The sink classes themselves are tested elsewhere; this file is the
unit-test floor for the dedup primitives so a future PR can refactor
either layer in confidence the contract still holds.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

import pytest

# Make `from fpulse...` importable when running pytest from the repo root
# without a package install. The backend tests/conftest.py already does
# this, but we duplicate the guard so this file can be run in isolation
# (e.g. `pytest backend/tests/test_sink_idempotency.py`).
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fpulse.sinks.dedupe_store import IdempotencyDedupeStore
from fpulse.sinks.idempotency_helper import compute_row_hash, should_skip
from fpulse.storage.database import Database


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def db():
    """Per-test SQLite DB with the sink_idempotency table created."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        d = Database(path)
        yield d
        try:
            d.close()
        except Exception:
            pass
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.fixture
def store(db):
    """Fresh ``IdempotencyDedupeStore`` bound to the per-test DB."""
    return IdempotencyDedupeStore(db=db)


# ─── compute_row_hash ────────────────────────────────────────────────


class TestComputeRowHash:
    def test_empty_expression_returns_empty(self):
        assert compute_row_hash({"a": 1}, "") == ""
        assert compute_row_hash({}, "") == ""

    def test_static_expression_is_hashed(self):
        h = compute_row_hash({}, "fixed-key")
        # SHA-256 of "fixed-key"
        assert len(h) == 64
        assert h == compute_row_hash({}, "fixed-key")  # deterministic

    def test_substitution(self):
        h1 = compute_row_hash({"user_id": 42}, "{user_id}")
        h2 = compute_row_hash({"user_id": 43}, "{user_id}")
        assert h1 != h2
        assert h1 == compute_row_hash({"user_id": 42}, "{user_id}")

    def test_int_string_collision(self):
        """`1` and `"1"` should hash identically — same semantic id."""
        h_int = compute_row_hash({"id": 1}, "{id}")
        h_str = compute_row_hash({"id": "1"}, "{id}")
        assert h_int == h_str

    def test_none_becomes_empty(self):
        """None substitutes as "" rather than "None"."""
        h_none = compute_row_hash({"id": None}, "{id}")
        h_missing = compute_row_hash({}, "{id}")
        h_empty = compute_row_hash({"id": ""}, "{id}")
        assert h_none == h_missing == h_empty

    def test_composite_key(self):
        h1 = compute_row_hash(
            {"user_id": 1, "event": "open"}, "{user_id}|{event}"
        )
        h2 = compute_row_hash(
            {"user_id": 1, "event": "click"}, "{user_id}|{event}"
        )
        h3 = compute_row_hash(
            {"user_id": 2, "event": "open"}, "{user_id}|{event}"
        )
        assert len({h1, h2, h3}) == 3

    def test_value_containing_placeholder_text_isnt_resubstituted(self):
        """If a column's value contains "{other_col}", it must NOT be
        re-substituted by a second pass. Single-pass regex render."""
        import hashlib

        h = compute_row_hash(
            {"a": "{b}", "b": "VALUE_B"},
            "{a}",
        )
        # Render of "{a}" with a="{b}" produces the literal string "{b}".
        # If a second pass had run, the result would be "VALUE_B".
        expected_literal = hashlib.sha256(b"{b}").hexdigest()
        bad_if_second_pass = hashlib.sha256(b"VALUE_B").hexdigest()
        assert h == expected_literal
        assert h != bad_if_second_pass

    def test_punctuation_only_template(self):
        """No substitutions → expression is hashed literally."""
        h = compute_row_hash({"x": 1}, "static|punctuation|only")
        assert h == compute_row_hash({}, "static|punctuation|only")


# ─── should_skip ─────────────────────────────────────────────────────


class TestShouldSkip:
    def test_empty_expression_returns_no_skip_no_hash(self, store):
        skip, h = should_skip("p", "s", {"a": 1}, "", store)
        assert (skip, h) == (False, "")

    def test_unrecorded_row_is_not_skipped(self, store):
        skip, h = should_skip("p", "s", {"id": 1}, "{id}", store)
        assert skip is False
        assert len(h) == 64

    def test_recorded_row_is_skipped(self, store):
        skip1, h1 = should_skip("p", "s", {"id": 1}, "{id}", store)
        assert skip1 is False
        store.record("p", "s", h1)
        skip2, h2 = should_skip("p", "s", {"id": 1}, "{id}", store)
        assert skip2 is True
        assert h2 == h1

    def test_missing_store_returns_no_skip(self):
        skip, h = should_skip("p", "s", {"id": 1}, "{id}", None)
        assert skip is False
        assert len(h) == 64

    def test_seen_exception_falls_through_safely(self, monkeypatch, store):
        """If the store crashes on lookup, we MUST NOT block the sink —
        duplicates are tolerable; missed sends are not."""

        def boom(*_a, **_kw):
            raise RuntimeError("storage glitch")

        monkeypatch.setattr(store, "seen", boom)
        skip, h = should_skip("p", "s", {"id": 1}, "{id}", store)
        assert skip is False
        assert len(h) == 64


# ─── IdempotencyDedupeStore ──────────────────────────────────────────


class TestDedupeStore:
    def test_round_trip(self, store):
        assert store.seen("p", "s", "abc") is False
        store.record("p", "s", "abc")
        assert store.seen("p", "s", "abc") is True

    def test_no_db_is_no_op(self):
        s = IdempotencyDedupeStore(db=None)
        assert s.seen("p", "s", "abc") is False
        # Should not raise:
        s.record("p", "s", "abc")
        assert s.seen("p", "s", "abc") is False  # Still no db, still False

    def test_different_pipeline_is_independent(self, store):
        store.record("pipeline_a", "s", "abc")
        assert store.seen("pipeline_a", "s", "abc") is True
        assert store.seen("pipeline_b", "s", "abc") is False

    def test_different_sink_step_is_independent(self, store):
        store.record("p", "email_step", "abc")
        assert store.seen("p", "email_step", "abc") is True
        assert store.seen("p", "webhook_step", "abc") is False

    def test_replace_overwrites_ttl(self, store):
        """Second record() for the same key must overwrite the TTL —
        no duplicate rows even though we're using INSERT OR REPLACE."""
        store.record("p", "s", "abc", ttl_seconds=60)
        store.record("p", "s", "abc", ttl_seconds=120)
        # Count rows for this key — must be exactly 1.
        row = store._db.fetchone(
            "SELECT COUNT(*) AS n FROM sink_idempotency "
            "WHERE pipeline_id=? AND sink_step_id=? AND key_hash=?",
            ("p", "s", "abc"),
        )
        assert row["n"] == 1

    def test_ttl_expiry_unseens_row(self, store):
        """A record with ttl_seconds=0 expires immediately → not seen."""
        # ttl_seconds=0 means expires_at == now → already past on lookup.
        # The store treats `<= now` as past, so a 0 TTL is "stale on insert".
        # Use a tiny but positive TTL and sleep to be deterministic across
        # systems where clock granularity matters.
        store.record("p", "s", "abc", ttl_seconds=1)
        # Right after record: still seen.
        assert store.seen("p", "s", "abc") is True
        time.sleep(1.1)
        # Past expires_at: unseen.
        assert store.seen("p", "s", "abc") is False

    def test_no_ttl_is_permanent(self, store):
        """ttl_seconds=0 with our impl writes NULL expires_at → permanently seen."""
        store.record("p", "s", "abc", ttl_seconds=0)
        assert store.seen("p", "s", "abc") is True

    def test_empty_inputs_are_safe(self, store):
        """Empty pipeline_id / sink_step_id / hash never store or match."""
        store.record("", "s", "abc")
        store.record("p", "", "abc")
        store.record("p", "s", "")
        assert store.seen("", "s", "abc") is False
        assert store.seen("p", "", "abc") is False
        assert store.seen("p", "s", "") is False

    def test_stats_empty_pipeline(self, store):
        s = store.stats("nonexistent_pipeline")
        assert s == {"total": 0, "last_24h": 0, "oldest_age_seconds": 0}

    def test_stats_after_records(self, store):
        for i in range(3):
            store.record("p", "s", f"hash{i}")
        s = store.stats("p")
        assert s["total"] == 3
        assert s["last_24h"] == 3
        assert s["oldest_age_seconds"] >= 0
        # Different pipeline gets independent counts.
        assert store.stats("other")["total"] == 0

    def test_seen_swallows_storage_error(self, store, monkeypatch):
        """A broken DB read returns False rather than raising."""

        def boom(*_a, **_kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr(store._db, "fetchone", boom)
        # Must not raise:
        assert store.seen("p", "s", "abc") is False

    def test_record_swallows_storage_error(self, store, monkeypatch):
        """A broken DB write logs and returns rather than raising."""

        def boom(*_a, **_kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr(store._db, "execute", boom)
        # Must not raise:
        store.record("p", "s", "abc")


# ─── Integration: helper + store end-to-end ──────────────────────────


class TestEndToEnd:
    def test_two_runs_same_row_skips_second(self, store):
        """The full sink-side pattern: should_skip → record on miss →
        next call to should_skip returns skip=True."""
        row = {"user_id": 42, "event": "welcome"}
        key = "{user_id}|{event}"

        # Run 1
        skip, h = should_skip("p", "email", row, key, store)
        assert skip is False
        store.record("p", "email", h)

        # Run 2
        skip, h2 = should_skip("p", "email", row, key, store)
        assert skip is True
        assert h2 == h

    def test_different_row_after_record_fires(self, store):
        """Recording one row must NOT skip a different row with the same template."""
        key = "{user_id}"

        skip1, h1 = should_skip("p", "email", {"user_id": 1}, key, store)
        store.record("p", "email", h1)

        skip2, h2 = should_skip("p", "email", {"user_id": 2}, key, store)
        assert skip2 is False
        assert h1 != h2
