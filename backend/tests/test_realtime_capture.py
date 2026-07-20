"""RealtimeExecutor — step output capture wiring tests.

Verifies the executor records per-step outputs to StepOutputStore at the
right hook point, computes sample-derived schema stats, and never lets a
capture failure abort the run.
"""

from __future__ import annotations

import pytest

from fpulse.engine.realtime import RealtimeExecutor
from fpulse.engine.step_output_store import StepOutputStore
from fpulse.ir.schema import Step, StepType


@pytest.fixture
def step_output_store(_fpulse_test_db):
    return StepOutputStore(db=_fpulse_test_db)


class TestSchemaFromSample:
    def test_derives_null_and_distinct_counts(self):
        sample = [
            {"id": 1, "name": "Alice", "age": 30},
            {"id": 2, "name": "Bob", "age": None},
            {"id": 3, "name": "Alice", "age": 25},
        ]
        schema_info = [
            {"name": "id", "type": "INTEGER", "nullable": False},
            {"name": "name", "type": "VARCHAR", "nullable": True},
            {"name": "age", "type": "INTEGER", "nullable": True},
        ]
        out = RealtimeExecutor._schema_from_sample(sample, schema_info)

        by_name = {c["name"]: c for c in out}
        assert by_name["id"]["null_count"] == 0
        assert by_name["id"]["distinct_count"] == 3
        assert by_name["name"]["null_count"] == 0
        assert by_name["name"]["distinct_count"] == 2  # Alice appears twice
        assert by_name["age"]["null_count"] == 1
        assert by_name["age"]["distinct_count"] == 2

    def test_flags_stats_as_sample_derived(self):
        sample = [{"x": 1}, {"x": 2}]
        out = RealtimeExecutor._schema_from_sample(
            sample, [{"name": "x", "type": "INTEGER"}]
        )
        assert out[0]["from_sample"] is True
        assert out[0]["sample_size"] == 2

    def test_unhashable_values_leave_distinct_unknown(self):
        # Nested list/dict columns (from XML/JSON parse nodes) are unhashable.
        # We must not crash; distinct_count falls back to None.
        sample = [{"payload": [1, 2]}, {"payload": [3, 4]}]
        out = RealtimeExecutor._schema_from_sample(
            sample, [{"name": "payload", "type": "JSON"}]
        )
        assert out[0]["distinct_count"] is None
        assert out[0]["null_count"] == 0

    def test_empty_sample(self):
        out = RealtimeExecutor._schema_from_sample(
            [], [{"name": "x", "type": "INTEGER"}]
        )
        assert out[0]["null_count"] == 0
        assert out[0]["distinct_count"] == 0
        assert out[0]["sample_size"] == 0


class TestCaptureWiring:
    def test_records_to_store_on_success(self, step_output_store):
        executor = RealtimeExecutor(step_output_store=step_output_store)
        step = Step(id="s1", type=StepType.FILTER, label="Filter Active",
                    params={"condition": "status = 'active'"})

        executor._capture_step_output(
            execution_id="exec-test",
            step=step,
            step_index=0,
            step_type="filter",
            status="success",
            row_count=42,
            sample_data=[{"id": 1}, {"id": 2}],
            schema_info=[{"name": "id", "type": "INTEGER"}],
        )

        got = step_output_store.get_step("exec-test", "s1")
        assert got is not None
        assert got["status"] == "success"
        assert got["row_count"] == 42
        assert got["sample_rows"] == [{"id": 1}, {"id": 2}]
        assert got["label"] == "Filter Active"
        assert got["schema"][0]["name"] == "id"
        assert got["schema"][0]["from_sample"] is True

    def test_records_failed_step_with_error_status(self, step_output_store):
        executor = RealtimeExecutor(step_output_store=step_output_store)
        step = Step(id="s2", type=StepType.FILTER, label="Bad Filter",
                    params={"condition": "WHERE oops"})

        executor._capture_step_output(
            execution_id="exec-fail",
            step=step,
            step_index=1,
            step_type="filter",
            status="error",
            row_count=0,
            sample_data=[],
            schema_info=[],
        )

        got = step_output_store.get_step("exec-fail", "s2")
        assert got is not None
        assert got["status"] == "error"
        assert got["row_count"] == 0
        assert got["sample_rows"] == []

    def test_noop_when_store_not_wired(self):
        # No store → method must return cleanly without raising.
        executor = RealtimeExecutor(step_output_store=None)
        step = Step(id="s1", type=StepType.FILTER, label="Filter",
                    params={"condition": "x = 1"})
        executor._capture_step_output(
            execution_id="exec-noop",
            step=step,
            step_index=0,
            step_type="filter",
            status="success",
            row_count=1,
            sample_data=[{"x": 1}],
            schema_info=[{"name": "x", "type": "INTEGER"}],
        )

    def test_capture_failure_does_not_abort(self):
        """A broken store must not crash the executor — runs are the priority."""
        class BrokenStore:
            def record(self, **_kw):
                raise RuntimeError("disk full")

        executor = RealtimeExecutor(step_output_store=BrokenStore())
        step = Step(id="s1", type=StepType.FILTER, label="Filter",
                    params={"condition": "x = 1"})

        # Must NOT raise — capture is best-effort by contract.
        executor._capture_step_output(
            execution_id="exec-broken",
            step=step,
            step_index=0,
            step_type="filter",
            status="success",
            row_count=1,
            sample_data=[{"x": 1}],
            schema_info=[{"name": "x", "type": "INTEGER"}],
        )
