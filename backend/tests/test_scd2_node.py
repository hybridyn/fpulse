"""Golden-file tests for the SCD2Node — Sprint 1 / Gate 1.

Three-run scenario per the design doc test plan:
  1. Initial load        — every business key is brand-new
  2. No-change re-run    — same input; output identical (hash match)
  3. Tracked-column edit — one row's tracked column changes; expect
     the previous current version closed out + a new current version

Plus edge cases:
  * Multi-column business key
  * passthrough_columns carried but not hashed
  * Custom column names (effective_from/to/current/surrogate_key)
  * Business key in target but missing from incoming → kept untouched
  * Default param validation (missing business_key / tracked_columns)
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from fpulse.nodes.base import ExecutionContext
from fpulse.nodes.scd2 import SCD2Node, row_hash


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def ctx():
    conn = duckdb.connect(":memory:")
    return ExecutionContext(conn=conn)


def _register(ctx: ExecutionContext, step_id: str, sql: str):
    ctx.set_result(step_id, ctx.conn.sql(sql))


def _rows_as_dicts(rel) -> list[dict[str, Any]]:
    cols = list(rel.columns)
    return [dict(zip(cols, r)) for r in rel.fetchall()]


def _run_scd2(ctx, *, params, incoming_step="incoming", target_step=None):
    inputs = [incoming_step]
    if target_step:
        inputs.append(target_step)
    full_params = {**params, "_input_step_ids": inputs}
    return SCD2Node(params=full_params).execute(ctx)


# ── Hash helper ──────────────────────────────────────────────────────


class TestRowHash:
    def test_stable_across_calls(self):
        a = row_hash(["alice", 30, None])
        b = row_hash(["alice", 30, None])
        assert a == b

    def test_changes_on_value_change(self):
        a = row_hash(["alice", 30])
        b = row_hash(["alice", 31])
        assert a != b

    def test_none_distinct_from_empty_string(self):
        a = row_hash([None])
        b = row_hash([""])
        # Both hash to "" via the stability rule, so they ARE the same — a
        # documented choice. Encode that here so a future implementation
        # change is forced to update the test deliberately.
        assert a == b

    def test_bool_normalised(self):
        a = row_hash([True])
        b = row_hash([1])
        # Both serialize to "1" by design.
        assert a == b


# ── Initial load ─────────────────────────────────────────────────────


class TestInitialLoad:
    def test_every_incoming_row_becomes_a_current_version(self, ctx):
        _register(ctx, "incoming", """
            SELECT * FROM (VALUES
                (1, 'Alice', 'gold'),
                (2, 'Bob',   'silver'),
                (3, 'Carol', 'bronze')
            ) AS t(customer_id, name, tier)
        """)
        result = _run_scd2(ctx, params={
            "business_key": ["customer_id"],
            "tracked_columns": ["name", "tier"],
            "run_time": "2026-05-04T00:00:00+00:00",
        })
        rows = _rows_as_dicts(result)
        assert len(rows) == 3
        # Every row is current.
        assert all(r["is_current"] for r in rows)
        assert all(r["valid_from"] == "2026-05-04T00:00:00+00:00" for r in rows)
        assert all(r["valid_to"] == "9999-12-31" for r in rows)
        # Surrogate key is deterministic per (business_key, valid_from).
        assert len(set(r["scd_id"] for r in rows)) == 3
        # Business keys preserved.
        bk_set = {r["customer_id"] for r in rows}
        assert bk_set == {1, 2, 3}


# ── No-change re-run ─────────────────────────────────────────────────


class TestNoChange:
    def test_identical_input_produces_identical_state(self, ctx):
        # Build the "initial" state via run 1.
        _register(ctx, "incoming1", """
            SELECT * FROM (VALUES
                (1, 'Alice', 'gold'),
                (2, 'Bob',   'silver')
            ) AS t(customer_id, name, tier)
        """)
        run1 = _run_scd2(ctx, params={
            "business_key": ["customer_id"],
            "tracked_columns": ["name", "tier"],
            "run_time": "2026-05-04T00:00:00+00:00",
        }, incoming_step="incoming1")
        ctx.set_result("target1", run1)

        # Run 2 with the same input + run1's output as the current target.
        _register(ctx, "incoming2", """
            SELECT * FROM (VALUES
                (1, 'Alice', 'gold'),
                (2, 'Bob',   'silver')
            ) AS t(customer_id, name, tier)
        """)
        run2 = _run_scd2(ctx, params={
            "business_key": ["customer_id"],
            "tracked_columns": ["name", "tier"],
            "run_time": "2026-05-05T00:00:00+00:00",  # different run_time
        }, incoming_step="incoming2", target_step="target1")
        rows2 = _rows_as_dicts(run2)

        # Same row count — no new versions created.
        assert len(rows2) == 2
        # All still current.
        assert all(r["is_current"] for r in rows2)
        # valid_from must remain the original — NOT updated to run_time of run 2.
        assert all(r["valid_from"] == "2026-05-04T00:00:00+00:00" for r in rows2)


# ── Change detection ─────────────────────────────────────────────────


class TestChangeDetection:
    def test_tracked_change_creates_new_version_and_closes_old(self, ctx):
        # Initial load.
        _register(ctx, "incoming1", """
            SELECT * FROM (VALUES
                (1, 'Alice', 'gold')
            ) AS t(customer_id, name, tier)
        """)
        run1 = _run_scd2(ctx, params={
            "business_key": ["customer_id"],
            "tracked_columns": ["name", "tier"],
            "run_time": "2026-05-04T00:00:00+00:00",
        }, incoming_step="incoming1")
        ctx.set_result("target1", run1)

        # Run 2 — alice's tier changes to platinum.
        _register(ctx, "incoming2", """
            SELECT * FROM (VALUES
                (1, 'Alice', 'platinum')
            ) AS t(customer_id, name, tier)
        """)
        run2 = _run_scd2(ctx, params={
            "business_key": ["customer_id"],
            "tracked_columns": ["name", "tier"],
            "run_time": "2026-05-05T00:00:00+00:00",
        }, incoming_step="incoming2", target_step="target1")
        rows = _rows_as_dicts(run2)

        # Expect 2 rows: the closed-out gold version + the new platinum current.
        assert len(rows) == 2
        currents = [r for r in rows if r["is_current"]]
        closed = [r for r in rows if not r["is_current"]]
        assert len(currents) == 1
        assert len(closed) == 1

        cur = currents[0]
        assert cur["tier"] == "platinum"
        assert cur["valid_from"] == "2026-05-05T00:00:00+00:00"
        assert cur["valid_to"] == "9999-12-31"

        prev = closed[0]
        assert prev["tier"] == "gold"
        assert prev["valid_from"] == "2026-05-04T00:00:00+00:00"
        assert prev["valid_to"] == "2026-05-05T00:00:00+00:00"

    def test_unchanged_key_is_not_emitted_twice(self, ctx):
        _register(ctx, "incoming1", """
            SELECT * FROM (VALUES
                (1, 'Alice', 'gold'),
                (2, 'Bob',   'silver')
            ) AS t(customer_id, name, tier)
        """)
        run1 = _run_scd2(ctx, params={
            "business_key": ["customer_id"],
            "tracked_columns": ["name", "tier"],
            "run_time": "2026-05-04T00:00:00+00:00",
        }, incoming_step="incoming1")
        ctx.set_result("target1", run1)

        # Only Bob changes; Alice is unchanged.
        _register(ctx, "incoming2", """
            SELECT * FROM (VALUES
                (1, 'Alice', 'gold'),
                (2, 'Bob',   'platinum')
            ) AS t(customer_id, name, tier)
        """)
        run2 = _run_scd2(ctx, params={
            "business_key": ["customer_id"],
            "tracked_columns": ["name", "tier"],
            "run_time": "2026-05-05T00:00:00+00:00",
        }, incoming_step="incoming2", target_step="target1")
        rows = _rows_as_dicts(run2)

        # Alice: 1 unchanged current. Bob: 1 closed + 1 current = 2 rows.
        # Total 3 rows.
        assert len(rows) == 3
        alice = [r for r in rows if r["customer_id"] == 1]
        bob = [r for r in rows if r["customer_id"] == 2]
        assert len(alice) == 1
        assert alice[0]["tier"] == "gold"
        assert alice[0]["is_current"]
        assert len(bob) == 2
        bob_curr = [r for r in bob if r["is_current"]][0]
        bob_prev = [r for r in bob if not r["is_current"]][0]
        assert bob_curr["tier"] == "platinum"
        assert bob_prev["tier"] == "silver"


# ── Multi-column business key + passthrough ──────────────────────────


class TestMultiColumnBusinessKey:
    def test_compound_key(self, ctx):
        _register(ctx, "incoming", """
            SELECT * FROM (VALUES
                ('US', 1, 'gold'),
                ('US', 2, 'silver'),
                ('UK', 1, 'bronze')
            ) AS t(country, customer_id, tier)
        """)
        result = _run_scd2(ctx, params={
            "business_key": ["country", "customer_id"],
            "tracked_columns": ["tier"],
            "run_time": "2026-05-04T00:00:00+00:00",
        })
        rows = _rows_as_dicts(result)
        assert len(rows) == 3
        # Three distinct surrogate keys for three distinct compound keys.
        assert len(set(r["scd_id"] for r in rows)) == 3


class TestPassthrough:
    def test_passthrough_carried_but_not_hashed(self, ctx):
        # First run: load Alice with notes='hello'.
        _register(ctx, "incoming1", """
            SELECT * FROM (VALUES
                (1, 'Alice', 'gold', 'hello')
            ) AS t(customer_id, name, tier, notes)
        """)
        run1 = _run_scd2(ctx, params={
            "business_key": ["customer_id"],
            "tracked_columns": ["name", "tier"],
            "passthrough_columns": ["notes"],
            "run_time": "2026-05-04T00:00:00+00:00",
        }, incoming_step="incoming1")
        ctx.set_result("target1", run1)

        # Second run: notes changes to 'world' but tracked columns don't —
        # SCD2 must NOT create a new version.
        _register(ctx, "incoming2", """
            SELECT * FROM (VALUES
                (1, 'Alice', 'gold', 'world')
            ) AS t(customer_id, name, tier, notes)
        """)
        run2 = _run_scd2(ctx, params={
            "business_key": ["customer_id"],
            "tracked_columns": ["name", "tier"],
            "passthrough_columns": ["notes"],
            "run_time": "2026-05-05T00:00:00+00:00",
        }, incoming_step="incoming2", target_step="target1")
        rows = _rows_as_dicts(run2)
        assert len(rows) == 1
        assert rows[0]["is_current"]
        # Stored notes is the value from run 1 (we never re-emit because
        # nothing tracked changed). This is the documented trade-off:
        # passthrough is NOT change-detected; if customers want to track
        # it, they put it in tracked_columns.
        assert rows[0]["notes"] == "hello"


# ── Missing business key in incoming (preservation) ──────────────────


class TestKeyPreservation:
    def test_target_only_keys_kept_untouched(self, ctx):
        _register(ctx, "incoming1", """
            SELECT * FROM (VALUES
                (1, 'Alice', 'gold'),
                (2, 'Bob',   'silver')
            ) AS t(customer_id, name, tier)
        """)
        run1 = _run_scd2(ctx, params={
            "business_key": ["customer_id"],
            "tracked_columns": ["name", "tier"],
            "run_time": "2026-05-04T00:00:00+00:00",
        }, incoming_step="incoming1")
        ctx.set_result("target1", run1)

        # Run 2 only sees Alice.
        _register(ctx, "incoming2", """
            SELECT * FROM (VALUES
                (1, 'Alice', 'gold')
            ) AS t(customer_id, name, tier)
        """)
        run2 = _run_scd2(ctx, params={
            "business_key": ["customer_id"],
            "tracked_columns": ["name", "tier"],
            "run_time": "2026-05-05T00:00:00+00:00",
        }, incoming_step="incoming2", target_step="target1")
        rows = _rows_as_dicts(run2)

        # Bob must still be present and still current — soft-delete is NOT
        # the default behavior (operator can chain a downstream node for that).
        bob = [r for r in rows if r["customer_id"] == 2]
        assert len(bob) == 1
        assert bob[0]["is_current"]


# ── Custom column names ──────────────────────────────────────────────


class TestCustomColumnNames:
    def test_custom_columns_emitted(self, ctx):
        _register(ctx, "incoming", """
            SELECT * FROM (VALUES
                (1, 'Alice', 'gold')
            ) AS t(customer_id, name, tier)
        """)
        result = _run_scd2(ctx, params={
            "business_key": ["customer_id"],
            "tracked_columns": ["name", "tier"],
            "effective_from_column": "start_dt",
            "effective_to_column": "end_dt",
            "current_flag_column": "active",
            "surrogate_key_column": "dim_key",
            "null_high_water": "2999-12-31",
            "run_time": "2026-05-04T00:00:00+00:00",
        })
        cols = set(result.columns)
        assert "start_dt" in cols
        assert "end_dt" in cols
        assert "active" in cols
        assert "dim_key" in cols
        rows = _rows_as_dicts(result)
        assert rows[0]["end_dt"] == "2999-12-31"
        assert rows[0]["active"]


# ── Validation ───────────────────────────────────────────────────────


class TestValidation:
    def test_missing_business_key_raises(self, ctx):
        _register(ctx, "incoming", "SELECT 1 AS customer_id, 'x' AS name")
        with pytest.raises(ValueError, match="business_key"):
            _run_scd2(ctx, params={"tracked_columns": ["name"]})

    def test_missing_tracked_columns_raises(self, ctx):
        _register(ctx, "incoming", "SELECT 1 AS customer_id")
        with pytest.raises(ValueError, match="tracked_columns"):
            _run_scd2(ctx, params={"business_key": ["customer_id"]})

    def test_missing_required_column_in_incoming_raises(self, ctx):
        _register(ctx, "incoming", "SELECT 1 AS customer_id")
        with pytest.raises(ValueError, match="missing required columns"):
            _run_scd2(ctx, params={
                "business_key": ["customer_id"],
                "tracked_columns": ["tier"],   # not in incoming
            })

    def test_no_input_raises(self, ctx):
        with pytest.raises(ValueError, match="no input data"):
            SCD2Node(params={
                "business_key": ["customer_id"],
                "tracked_columns": ["name"],
                "_input_step_ids": [],
            }).execute(ctx)


# ── Registration ─────────────────────────────────────────────────────


class TestRegistration:
    def test_scd2_registered_in_node_registry(self):
        from fpulse.ir.schema import StepType
        from fpulse.nodes.registry import get_registry

        registry = get_registry()
        cls = registry.get(StepType.SCD2)
        assert cls is SCD2Node
        assert cls.display_name == "SCD Type 2"
        assert cls.category == "transform"

    def test_scd2_appears_in_all_types(self):
        from fpulse.nodes.registry import get_registry
        meta = get_registry().all_types()
        scd2_meta = next((m for m in meta if m["type"] == "scd2"), None)
        assert scd2_meta is not None
        assert scd2_meta["category"] == "transform"
        assert "business_key" in scd2_meta["default_params"]
