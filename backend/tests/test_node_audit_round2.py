"""Node-audit round 2 (2026-06-11): Sort, Sample, Deduplicate, Flatten/Explode.

Pins the fixes for four latent bugs:
  * Sort quoted whole "col DIR" tokens as identifiers — every sort saved
    through the structured UI failed at runtime.
  * Sample ignored `fraction` entirely (dead param) and had no
    rows-vs-percent mode, no seed.
  * Deduplicate keep_last was DISTINCT ON — an arbitrary row, order_by
    silently ignored.
  * Flatten/Explode read inputs from ctx._results (every executed node)
    instead of the wired edges, and had no outer-explode/index options.
"""
from __future__ import annotations

import duckdb
import pytest

from fpulse.nodes.base import ExecutionContext
from fpulse.nodes.activities import SortNode, SampleNode
from fpulse.nodes.deduplicate import DeduplicateNode
from fpulse.nodes.advanced_transforms import FlattenExplodeNode


def _ctx() -> ExecutionContext:
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def _seed(ctx: ExecutionContext, step_id: str, sql: str):
    rel = ctx.conn.sql(sql)
    ctx.set_result(step_id, rel)
    return rel


# ── Sort ────────────────────────────────────────────────────────────────

def _sales(ctx):
    return _seed(ctx, "src", (
        "SELECT * FROM (VALUES "
        "(100, 'beta'), (300, 'alpha'), (100, 'alpha'), (NULL, 'zeta')"
        ") AS t(amount, name)"
    ))


def test_sort_parses_direction_tokens_from_structured_ui():
    """THE regression: UI serialises entries as 'amount DESC' strings."""
    ctx = _ctx()
    _sales(ctx)
    node = SortNode(params={"_input_step_ids": ["src"], "sort_by": ["amount DESC", "name ASC"]})
    rows = node.execute(ctx).fetchall()
    assert rows[0] == (300, "alpha")
    assert rows[1] == (100, "alpha")
    assert rows[2] == (100, "beta")


def test_sort_structured_dicts_and_nulls_placement():
    ctx = _ctx()
    _sales(ctx)
    node = SortNode(params={
        "_input_step_ids": ["src"],
        "sort_by": [{"column": "amount", "direction": "DESC", "nulls": "FIRST"}],
    })
    rows = node.execute(ctx).fetchall()
    assert rows[0][0] is None          # NULLS FIRST honoured
    assert rows[1][0] == 300

    node2 = SortNode(params={"_input_step_ids": ["src"], "sort_by": ["amount DESC NULLS LAST"]})
    rows2 = node2.execute(ctx).fetchall()
    assert rows2[-1][0] is None        # NULLS LAST honoured


def test_sort_rejects_duplicates_bad_direction_and_unknown_column():
    ctx = _ctx()
    _sales(ctx)
    with pytest.raises(ValueError, match="more than once"):
        SortNode(params={"_input_step_ids": ["src"], "sort_by": ["amount DESC", "amount ASC"]}).execute(ctx)
    with pytest.raises(ValueError, match="invalid sort entry"):
        SortNode(params={"_input_step_ids": ["src"], "sort_by": ["amount DES"]}).execute(ctx)
    with pytest.raises(ValueError, match="ammount"):
        SortNode(params={"_input_step_ids": ["src"], "sort_by": ["ammount DESC"]}).execute(ctx)


# ── Sample ──────────────────────────────────────────────────────────────

def _ten_rows(ctx):
    return _seed(ctx, "src", "SELECT * FROM range(10) AS t(n)")


def test_sample_rows_first_is_deterministic_prefix():
    ctx = _ctx()
    _ten_rows(ctx)
    node = SampleNode(params={"_input_step_ids": ["src"], "mode": "rows", "count": 3})
    assert node.execute(ctx).fetchall() == [(0,), (1,), (2,)]


def test_sample_legacy_fraction_now_honoured_as_percent():
    """fraction used to be a dead param — the engine sampled count=100."""
    ctx = _ctx()
    _ten_rows(ctx)
    node = SampleNode(params={"_input_step_ids": ["src"], "fraction": 0.5, "method": "first"})
    assert len(node.execute(ctx).fetchall()) == 5


def test_sample_count_wins_when_both_set_without_mode():
    ctx = _ctx()
    _ten_rows(ctx)
    node = SampleNode(params={"_input_step_ids": ["src"], "count": 2, "fraction": 0.9})
    assert len(node.execute(ctx).fetchall()) == 2


def test_sample_random_seed_is_reproducible():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT * FROM range(1000) AS t(n)")
    p = {"_input_step_ids": ["src"], "mode": "rows", "count": 10, "method": "random", "seed": 42}
    first = SampleNode(params=p).execute(ctx).fetchall()
    second = SampleNode(params=p).execute(ctx).fetchall()
    assert first == second
    assert len(first) == 10


def test_sample_validation_errors():
    ctx = _ctx()
    _ten_rows(ctx)
    with pytest.raises(ValueError, match="between 0 and 100"):
        SampleNode(params={"_input_step_ids": ["src"], "mode": "percent", "percent": 150}).execute(ctx)
    with pytest.raises(ValueError, match="greater than 0"):
        SampleNode(params={"_input_step_ids": ["src"], "mode": "rows", "count": -5}).execute(ctx)
    with pytest.raises(ValueError, match="seed"):
        SampleNode(params={"_input_step_ids": ["src"], "method": "random", "seed": "abc"}).execute(ctx)


# ── Deduplicate ─────────────────────────────────────────────────────────

def _versions(ctx):
    return _seed(ctx, "src", (
        "SELECT * FROM (VALUES "
        "(1, DATE '2026-01-01', 'v1'), "
        "(1, DATE '2026-03-01', 'v3'), "
        "(1, DATE '2026-02-01', 'v2'), "
        "(2, DATE '2026-01-15', 'only')"
        ") AS t(id, created_at, tag)"
    ))


def test_dedup_keep_first_respects_order():
    ctx = _ctx()
    _versions(ctx)
    node = DeduplicateNode(params={
        "_input_step_ids": ["src"], "key": ["id"],
        "strategy": "keep_first", "order_by": "created_at DESC",
    })
    rows = {r[0]: r[2] for r in node.execute(ctx).fetchall()}
    assert rows == {1: "v3", 2: "only"}   # latest survives


def test_dedup_keep_last_actually_keeps_last_by_order():
    """Old impl: DISTINCT ON — arbitrary row, order_by ignored."""
    ctx = _ctx()
    _versions(ctx)
    node = DeduplicateNode(params={
        "_input_step_ids": ["src"], "key": ["id"],
        "strategy": "keep_last", "order_by": "created_at DESC",
    })
    rows = {r[0]: r[2] for r in node.execute(ctx).fetchall()}
    assert rows == {1: "v1", 2: "only"}   # last of DESC = earliest


def test_dedup_emit_duplicates_tags_unique_and_duplicate():
    """Dual-output mode: every row tagged unique/duplicate via _split_output."""
    ctx = _ctx()
    _versions(ctx)   # id=1 has 3 rows, id=2 has 1
    node = DeduplicateNode(params={
        "_input_step_ids": ["src"], "key": ["id"],
        "strategy": "keep_first", "order_by": "created_at DESC",
        "emit_duplicates": True,
    })
    rel = node.execute(ctx)
    assert "_split_output" in rel.columns
    assert "__rn" not in rel.columns
    tag = rel.columns.index("_split_output")
    rows = rel.fetchall()
    uniques = [r for r in rows if r[tag] == "unique"]
    dupes = [r for r in rows if r[tag] == "duplicate"]
    assert len(uniques) == 2      # one survivor per key (id 1, id 2)
    assert len(dupes) == 2        # the two extra id=1 rows
    # the surviving id=1 row is the latest (keep_first by created_at DESC)
    tagcol = rel.columns
    id_i, tag_i = tagcol.index("id"), tag
    latest = [r for r in uniques if r[id_i] == 1][0]
    assert str(latest[tagcol.index("tag")]) == "v3"


def test_dedup_validates_keys_order_and_strategy():
    ctx = _ctx()
    _versions(ctx)
    with pytest.raises(ValueError, match="orderid"):
        DeduplicateNode(params={"_input_step_ids": ["src"], "key": ["orderid"]}).execute(ctx)
    with pytest.raises(ValueError, match="invalid order-by"):
        DeduplicateNode(params={
            "_input_step_ids": ["src"], "key": ["id"], "order_by": "created_at DES",
        }).execute(ctx)
    with pytest.raises(ValueError, match="invalid strategy"):
        DeduplicateNode(params={
            "_input_step_ids": ["src"], "key": ["id"], "strategy": "keep_random",
        }).execute(ctx)


# ── Flatten / Explode ──────────────────────────────────────────────────

def _orders_items(ctx):
    return _seed(ctx, "src", (
        "SELECT * FROM (VALUES "
        "(1, ['A', 'B']), (2, []), (3, NULL)"
        ") AS t(order_id, items)"
    ))


def test_explode_basic_drops_empty_rows_by_default():
    ctx = _ctx()
    _orders_items(ctx)
    node = FlattenExplodeNode(params={
        "_input_step_ids": ["src"], "mode": "explode", "column": "items",
    })
    rows = sorted(node.execute(ctx).fetchall())
    assert rows == [(1, "A"), (1, "B")]   # orders 2 and 3 vanish


def test_explode_keep_empty_preserves_rows_with_null_element():
    ctx = _ctx()
    _orders_items(ctx)
    node = FlattenExplodeNode(params={
        "_input_step_ids": ["src"], "mode": "explode", "column": "items",
        "keep_empty": True,
    })
    rows = sorted(node.execute(ctx).fetchall(), key=lambda r: (r[0], str(r[1])))
    assert (2, None) in rows and (3, None) in rows
    assert len(rows) == 4


def test_explode_add_index_emits_one_based_positions():
    ctx = _ctx()
    _orders_items(ctx)
    node = FlattenExplodeNode(params={
        "_input_step_ids": ["src"], "mode": "explode", "column": "items",
        "add_index": True,
    })
    rel = node.execute(ctx)
    assert "items_index" in rel.columns
    rows = sorted(rel.fetchall())
    assert rows == [(1, "A", 1), (1, "B", 2)]


def test_explode_dot_notation_nested_array():
    """Flatten/explode parity: explode a nested array via a dotted path."""
    ctx = _ctx()
    _seed(ctx, "src", (
        "SELECT * FROM (VALUES "
        "(1, {'items': ['a', 'b']}), (2, {'items': ['c']}) "
        ") AS t(order_id, data)"
    ))
    node = FlattenExplodeNode(params={
        "_input_step_ids": ["src"], "mode": "explode", "column": "data.items",
    })
    rel = node.execute(ctx)
    assert "items" in rel.columns         # leaf name becomes the output field
    assert "data" not in rel.columns      # the struct base is consumed
    assert sorted(rel.fetchall()) == [(1, "a"), (1, "b"), (2, "c")]


def test_explode_dot_notation_rejected_in_flatten_mode():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT 1 AS id, {'items': ['a']} AS data")
    with pytest.raises(ValueError, match="explode mode only"):
        FlattenExplodeNode(params={
            "_input_step_ids": ["src"], "mode": "flatten", "column": "data.items",
        }).execute(ctx)


def test_explode_type_guard_names_the_problem():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT 1 AS id, 5 AS scalar_col, {'a': 1} AS struct_col")
    with pytest.raises(ValueError, match="ARRAY/LIST"):
        FlattenExplodeNode(params={
            "_input_step_ids": ["src"], "mode": "explode", "column": "scalar_col",
        }).execute(ctx)
    with pytest.raises(ValueError, match="flatten"):
        FlattenExplodeNode(params={
            "_input_step_ids": ["src"], "mode": "explode", "column": "struct_col",
        }).execute(ctx)


# ── Derived Column: Add vs Replace ─────────────────────────────────────

def test_derived_column_replace_mode_and_collision_error():
    from fpulse.nodes.activities import DerivedColumnNode
    ctx = _ctx()
    _seed(ctx, "src", "SELECT 1 AS id, 100.0 AS salary")

    # collision without replace → clear error, not a duplicate column
    with pytest.raises(ValueError, match="already exists"):
        DerivedColumnNode(params={
            "_input_step_ids": ["src"],
            "columns": [{"name": "salary", "expression": "salary * 1.1"}],
        }).execute(ctx)

    # replace=true swaps the value in place, schema unchanged
    rel = DerivedColumnNode(params={
        "_input_step_ids": ["src"],
        "columns": [{"name": "salary", "expression": "salary * 1.1", "replace": True}],
    }).execute(ctx)
    assert rel.columns == ["id", "salary"]
    rows = rel.fetchall()
    assert rows[0][0] == 1
    assert abs(float(rows[0][1]) - 110.0) < 1e-9


def test_flatten_explode_uses_wired_inputs_not_all_results():
    """ctx holds an unrelated executed node; only the wired input counts."""
    ctx = _ctx()
    _seed(ctx, "unrelated", "SELECT 99 AS noise")
    _orders_items(ctx)
    node = FlattenExplodeNode(params={
        "_input_step_ids": ["src"], "mode": "explode", "column": "items",
    })
    rows = sorted(node.execute(ctx).fetchall())
    assert rows == [(1, "A"), (1, "B")]
