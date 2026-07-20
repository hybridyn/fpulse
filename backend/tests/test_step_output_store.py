"""StepOutputStore — capture + retention contract tests.

Locks the OSS caps (100 rows / 1 MB / 30-day TTL on samples) and the
counts-and-schema-retained-indefinitely behavior. The execution-replay
viewer's correctness depends on these invariants holding under arbitrary
captured payloads.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from fpulse.engine.step_output_store import (
    MAX_SAMPLE_BYTES,
    MAX_SAMPLE_ROWS,
    SAMPLE_TTL_DAYS,
    StepOutputStore,
)


@pytest.fixture
def store(_fpulse_test_db):
    return StepOutputStore(db=_fpulse_test_db)


class TestRecordAndRead:
    def test_record_then_get_round_trip(self, store):
        store.record(
            execution_id="exec-1",
            step_id="s1",
            step_index=0,
            step_type="csv_source",
            label="Load CSV",
            row_count=5,
            sample_rows=[{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}],
            schema=[
                {"name": "id", "dtype": "INTEGER", "null_count": 0, "distinct_count": 2},
                {"name": "name", "dtype": "VARCHAR", "null_count": 0, "distinct_count": 2},
            ],
        )

        got = store.get_step("exec-1", "s1")
        assert got is not None
        assert got["execution_id"] == "exec-1"
        assert got["step_id"] == "s1"
        assert got["row_count"] == 5
        assert got["sample_rows"] == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
        assert got["schema"] == [
            {"name": "id", "dtype": "INTEGER", "null_count": 0, "distinct_count": 2},
            {"name": "name", "dtype": "VARCHAR", "null_count": 0, "distinct_count": 2},
        ]
        assert got["sample_truncated"] is False
        assert got["sample_pruned"] is False

    def test_get_step_missing_returns_none(self, store):
        assert store.get_step("nope", "nope") is None

    def test_list_for_execution_ordered_by_step_index(self, store):
        for i, sid in enumerate(["s3", "s1", "s2"]):
            store.record(
                execution_id="exec-2",
                step_id=sid,
                step_index={"s1": 0, "s2": 1, "s3": 2}[sid],
                row_count=i,
            )

        rows = store.list_for_execution("exec-2")
        assert [r["step_id"] for r in rows] == ["s1", "s2", "s3"]

    def test_list_for_execution_scoped(self, store):
        store.record(execution_id="exec-A", step_id="s1", row_count=1)
        store.record(execution_id="exec-B", step_id="s1", row_count=2)

        a_rows = store.list_for_execution("exec-A")
        b_rows = store.list_for_execution("exec-B")
        assert len(a_rows) == 1 and a_rows[0]["row_count"] == 1
        assert len(b_rows) == 1 and b_rows[0]["row_count"] == 2

    def test_delete_for_execution(self, store):
        store.record(execution_id="exec-3", step_id="s1", row_count=1)
        store.record(execution_id="exec-3", step_id="s2", row_count=2)
        store.record(execution_id="exec-other", step_id="s1", row_count=99)

        deleted = store.delete_for_execution("exec-3")
        assert deleted == 2
        assert store.list_for_execution("exec-3") == []
        assert len(store.list_for_execution("exec-other")) == 1


class TestCaps:
    def test_row_cap_truncates_oversized_samples(self, store):
        oversized = [{"i": i} for i in range(MAX_SAMPLE_ROWS + 50)]
        store.record(
            execution_id="exec-cap",
            step_id="s1",
            row_count=len(oversized),
            sample_rows=oversized,
        )

        got = store.get_step("exec-cap", "s1")
        assert len(got["sample_rows"]) == MAX_SAMPLE_ROWS
        assert got["sample_truncated"] is True
        assert got["row_count"] == MAX_SAMPLE_ROWS + 50  # real count preserved

    def test_byte_cap_walks_sample_down(self, store):
        # Each row ~10 KB. 200 rows would be ~2 MB without caps; the
        # row cap clamps to 100 first (~1 MB), and the byte cap walks
        # further if needed.
        big_row = {"payload": "x" * 10_000}
        rows = [big_row] * 200
        store.record(
            execution_id="exec-bytes",
            step_id="s1",
            row_count=len(rows),
            sample_rows=rows,
        )

        got = store.get_step("exec-bytes", "s1")
        assert got["sample_bytes"] <= MAX_SAMPLE_BYTES
        assert got["sample_truncated"] is True
        assert got["row_count"] == 200

    def test_single_oversized_row_drops_sample_keeps_counts(self, store):
        # One row that alone exceeds the byte cap → sample drops to [],
        # but row_count + schema are preserved so the UI still shows
        # "1 row, schema X, sample unavailable (too large)."
        monster = {"payload": "x" * (MAX_SAMPLE_BYTES + 1024)}
        store.record(
            execution_id="exec-monster",
            step_id="s1",
            row_count=1,
            sample_rows=[monster],
            schema=[{"name": "payload", "dtype": "VARCHAR"}],
        )

        got = store.get_step("exec-monster", "s1")
        assert got["sample_rows"] == []
        assert got["sample_truncated"] is True
        assert got["row_count"] == 1
        assert got["schema"] == [{"name": "payload", "dtype": "VARCHAR"}]

    def test_small_sample_under_cap_not_marked_truncated(self, store):
        store.record(
            execution_id="exec-small",
            step_id="s1",
            row_count=3,
            sample_rows=[{"i": 1}, {"i": 2}, {"i": 3}],
        )
        got = store.get_step("exec-small", "s1")
        assert got["sample_truncated"] is False


class TestPruning:
    def test_prune_drops_old_samples_keeps_counts_and_schema(self, store, _fpulse_test_db):
        # Insert a recent + an old row by hand-stamping captured_at.
        store.record(
            execution_id="exec-recent",
            step_id="s1",
            row_count=2,
            sample_rows=[{"i": 1}, {"i": 2}],
            schema=[{"name": "i", "dtype": "INTEGER"}],
        )
        store.record(
            execution_id="exec-old",
            step_id="s1",
            row_count=99,
            sample_rows=[{"i": 1}],
            schema=[{"name": "i", "dtype": "INTEGER"}],
        )
        # Backdate the "old" row past the TTL window.
        old_ts = (datetime.now(timezone.utc) - timedelta(days=SAMPLE_TTL_DAYS + 5)).isoformat()
        _fpulse_test_db.execute(
            "UPDATE step_outputs SET captured_at = ? WHERE execution_id = ?",
            (old_ts, "exec-old"),
        )
        _fpulse_test_db.commit()

        pruned = store.prune_samples()
        assert pruned == 1

        old = store.get_step("exec-old", "s1")
        assert old["sample_rows"] == []
        assert old["sample_pruned"] is True
        # Counts + schema preserved indefinitely:
        assert old["row_count"] == 99
        assert old["schema"] == [{"name": "i", "dtype": "INTEGER"}]

        recent = store.get_step("exec-recent", "s1")
        assert recent["sample_pruned"] is False
        assert recent["sample_rows"] == [{"i": 1}, {"i": 2}]

    def test_prune_is_idempotent(self, store, _fpulse_test_db):
        store.record(execution_id="exec-old", step_id="s1", row_count=1, sample_rows=[{"i": 1}])
        old_ts = (datetime.now(timezone.utc) - timedelta(days=SAMPLE_TTL_DAYS + 5)).isoformat()
        _fpulse_test_db.execute(
            "UPDATE step_outputs SET captured_at = ? WHERE execution_id = ?",
            (old_ts, "exec-old"),
        )
        _fpulse_test_db.commit()

        first = store.prune_samples()
        second = store.prune_samples()
        assert first == 1
        assert second == 0


class TestSerialization:
    def test_none_values_round_trip(self, store):
        store.record(
            execution_id="exec-null",
            step_id="s1",
            row_count=1,
            sample_rows=[{"a": None, "b": 1}],
        )
        got = store.get_step("exec-null", "s1")
        assert got["sample_rows"] == [{"a": None, "b": 1}]

    def test_non_json_values_fall_back_to_str(self, store):
        # datetime is not JSON-native; default=str must catch it.
        ts = datetime(2026, 5, 20, 12, 30, tzinfo=timezone.utc)
        store.record(
            execution_id="exec-dt",
            step_id="s1",
            row_count=1,
            sample_rows=[{"when": ts}],
        )
        got = store.get_step("exec-dt", "s1")
        assert isinstance(got["sample_rows"][0]["when"], str)
        assert "2026-05-20" in got["sample_rows"][0]["when"]

    def test_empty_sample_and_schema(self, store):
        store.record(execution_id="exec-empty", step_id="s1", row_count=0)
        got = store.get_step("exec-empty", "s1")
        assert got["sample_rows"] == []
        assert got["schema"] == []
        assert got["sample_truncated"] is False
