"""Node-audit round 3 (2026-06-11): Join sides, Union modes, Window order,
Pivot row columns.

Pins:
  * Join `left_input_id` — edge order is layout, not semantics.
  * Union by_name — was a dead frontend option that silently ran
    UNION DISTINCT; now a true schema union (UNION ALL BY NAME).
  * Window order_by entries with inline direction ("amount DESC") — the
    old code quoted the whole token as an identifier (same bug as Sort).
  * Pivot group_by (Row Columns) — backend always supported it; pinned
    here now that the UI exposes it.
"""
from __future__ import annotations

import duckdb
import pytest

from fpulse.nodes.base import ExecutionContext
from fpulse.nodes.join import JoinNode
from fpulse.nodes.activities import UnionNode, WindowNode, PivotNode, UnpivotNode


def _ctx() -> ExecutionContext:
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def _seed(ctx: ExecutionContext, step_id: str, sql: str):
    rel = ctx.conn.sql(sql)
    ctx.set_result(step_id, rel)
    return rel


# ── Join: explicit sides ───────────────────────────────────────────────

def test_join_left_input_id_overrides_edge_order():
    ctx = _ctx()
    _seed(ctx, "orders", "SELECT * FROM (VALUES (1, 50.0)) AS t(customer_id, amount)")
    _seed(ctx, "customers", "SELECT * FROM (VALUES (1, 'Asha'), (2, 'Bo')) AS t(customer_id, name)")

    # Edges drawn customers-first; LEFT join must still keep ALL orders
    # rows when orders is pinned as the left side.
    node = JoinNode(params={
        "_input_step_ids": ["customers", "orders"],   # "wrong" edge order
        "left_input_id": "orders",
        "join_type": "LEFT",
        "key_mode": "same_key",
        "join_key": ["customer_id"],
    })
    rows = node.execute(ctx).fetchall()
    assert len(rows) == 1            # one orders row, enriched
    assert rows[0][0] == 1

    # Without the pin, legacy behavior: first edge (customers) is left →
    # LEFT join keeps both customers rows.
    legacy = JoinNode(params={
        "_input_step_ids": ["customers", "orders"],
        "join_type": "LEFT",
        "key_mode": "same_key",
        "join_key": ["customer_id"],
    })
    assert len(legacy.execute(ctx).fetchall()) == 2


# ── Union: three real modes ────────────────────────────────────────────

def test_union_modes_all_distinct_by_name():
    ctx = _ctx()
    _seed(ctx, "a", "SELECT * FROM (VALUES (1, 'x'), (2, 'y')) AS t(id, name)")
    _seed(ctx, "b", "SELECT * FROM (VALUES (2, 'y'), (3, 'z')) AS t(id, name)")

    base = {"_input_step_ids": ["a", "b"]}
    assert len(UnionNode(params={**base, "mode": "all"}).execute(ctx).fetchall()) == 4
    assert len(UnionNode(params={**base, "mode": "distinct"}).execute(ctx).fetchall()) == 3

    # by_name: schema union — columns matched by NAME, missing NULL-filled
    _seed(ctx, "c", "SELECT * FROM (VALUES (4, 'w', 'IN')) AS t(id, name, country)")
    rel = UnionNode(params={"_input_step_ids": ["a", "c"], "mode": "by_name"}).execute(ctx)
    assert sorted(rel.columns) == ["country", "id", "name"]
    rows = {r[0]: r for r in rel.fetchall()}
    assert len(rows) == 3
    # rows from `a` have NULL country
    cols = rel.columns
    country_idx = cols.index("country")
    assert rows[1][country_idx] is None
    assert rows[4][country_idx] == "IN"

    with pytest.raises(ValueError, match="invalid mode"):
        UnionNode(params={**base, "mode": "sideways"}).execute(ctx)


# ── Window: inline order direction ─────────────────────────────────────

def test_window_order_by_inline_direction_tokens():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT * FROM (VALUES ('A', 100), ('A', 300), ('A', 200)) AS t(grp, amount)")
    node = WindowNode(params={
        "_input_step_ids": ["src"],
        "partition_by": ["grp"],
        "order_by": ["amount DESC"],   # inline token — used to break as identifier
        "window_functions": [{"function": "ROW_NUMBER", "alias": "rn"}],
    })
    rel = node.execute(ctx)
    rows = sorted(rel.fetchall(), key=lambda r: r[2])
    assert rows[0][1] == 300           # rn=1 is the LARGEST amount (DESC honored)

    with pytest.raises(ValueError, match="invalid order-by"):
        WindowNode(params={
            "_input_step_ids": ["src"],
            "order_by": ["amount DES"],
            "window_functions": [{"function": "ROW_NUMBER", "alias": "rn"}],
        }).execute(ctx)


# ── Delete Data: dead files-mode fails loudly ──────────────────────────

def test_delete_data_files_mode_raises_instead_of_silent_noop():
    """The param schema advertised files mode but execute never implemented
    it — a retention pipeline 'deleting' files silently deleted nothing."""
    from fpulse.nodes.flow_control import DeleteDataNode
    ctx = _ctx()
    _seed(ctx, "src", "SELECT 1 AS id")
    with pytest.raises(ValueError, match="NO files would be deleted"):
        DeleteDataNode(params={
            "_input_step_ids": ["src"],
            "target_kind": "files",
            "target_path": "/data/incoming/",
        }).execute(ctx)

    # rows mode still works as the inverse filter it is
    _seed(ctx, "rows", "SELECT * FROM (VALUES (1), (2), (3)) AS t(id)")
    rel = DeleteDataNode(params={
        "_input_step_ids": ["rows"],
        "target_kind": "rows",
        "condition": "id = 2",
    }).execute(ctx)
    assert sorted(r[0] for r in rel.fetchall()) == [1, 3]


# ── Pivot: row columns ─────────────────────────────────────────────────

def test_unpivot_id_columns_restrict_carried_columns():
    ctx = _ctx()
    _seed(ctx, "src", (
        "SELECT * FROM (VALUES ('A', 'east', 1, 2), ('B', 'west', 3, 4)) "
        "AS t(product, region, q1, q2)"
    ))
    # keep only `product` as identifier (drop `region`)
    node = UnpivotNode(params={
        "_input_step_ids": ["src"],
        "columns": ["q1", "q2"], "id_columns": ["product"],
        "name_column": "quarter", "value_column": "sales",
    })
    rel = node.execute(ctx)
    assert sorted(rel.columns) == ["product", "quarter", "sales"]
    assert "region" not in rel.columns
    assert len(rel.fetchall()) == 4   # 2 products x 2 quarters


def test_unpivot_include_nulls_toggle():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT * FROM (VALUES (1, 10, NULL)) AS t(id, q1, q2)")
    drop = UnpivotNode(params={
        "_input_step_ids": ["src"], "columns": ["q1", "q2"],
        "name_column": "k", "value_column": "v",
    })
    assert len(drop.execute(ctx).fetchall()) == 1   # NULL q2 dropped

    keep = UnpivotNode(params={
        "_input_step_ids": ["src"], "columns": ["q1", "q2"],
        "name_column": "k", "value_column": "v", "include_nulls": True,
    })
    assert len(keep.execute(ctx).fetchall()) == 2   # NULL q2 kept


def test_unpivot_unknown_column_errors():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT 1 AS id, 2 AS q1")
    with pytest.raises(ValueError, match="not found"):
        UnpivotNode(params={
            "_input_step_ids": ["src"], "columns": ["q9"],
            "name_column": "k", "value_column": "v",
        }).execute(ctx)


def test_pivot_group_by_keys_output_rows():
    ctx = _ctx()
    _seed(ctx, "src", (
        "SELECT * FROM (VALUES "
        "('Jan', 'HW', 100), ('Jan', 'SW', 200), ('Feb', 'HW', 150), ('Feb', 'SW', 250)"
        ") AS t(month, category, amount)"
    ))
    node = PivotNode(params={
        "_input_step_ids": ["src"],
        "pivot_column": "category",
        "value_column": "amount",
        "agg_function": "SUM",
        "group_by": ["month"],
    })
    rel = node.execute(ctx)
    assert sorted(rel.columns) == ["HW", "SW", "month"]
    rows = {r[rel.columns.index("month")]: r for r in rel.fetchall()}
    assert len(rows) == 2
    hw = rel.columns.index("HW")
    assert rows["Jan"][hw] == 100 and rows["Feb"][hw] == 150


def test_pivot_without_group_by_groups_by_remaining_columns():
    """Empty Row Columns must group by every remaining column EXPLICITLY.
    Regression (2026-06-16 in-app find): the old fallback emitted
    `GROUP BY ALL`, which DuckDB's PIVOT *statement* rejects ('syntax error
    at or near ALL'), so EVERY pivot with no Row Columns failed at runtime —
    even though the UI hint says 'Empty = group by every remaining column'."""
    ctx = _ctx()
    _seed(ctx, "src", (
        "SELECT * FROM (VALUES "
        "('Jan', 'HW', 100), ('Jan', 'SW', 200), ('Feb', 'HW', 150), ('Feb', 'SW', 250)"
        ") AS t(month, category, amount)"
    ))
    node = PivotNode(params={
        "_input_step_ids": ["src"],
        "pivot_column": "category",
        "value_column": "amount",
        "agg_function": "SUM",
        # no group_by → must implicitly group by `month` (the remaining column)
    })
    rel = node.execute(ctx)
    assert sorted(rel.columns) == ["HW", "SW", "month"]
    assert len(rel.fetchall()) == 2  # Jan, Feb


# ── Pivot: freeze (explicit values) + fill (A3, 2026-06-15) ─────────────

def test_pivot_freeze_pins_columns_even_when_value_absent():
    ctx = _ctx()
    # data only has HW; freeze should still produce an SW column
    _seed(ctx, "src", (
        "SELECT * FROM (VALUES ('Jan', 'HW', 100), ('Feb', 'HW', 150)) "
        "AS t(month, category, amount)"
    ))
    node = PivotNode(params={
        "_input_step_ids": ["src"],
        "pivot_column": "category",
        "value_column": "amount",
        "agg_function": "SUM",
        "group_by": ["month"],
        "pivot_values": "HW, SW",        # comma-separated freeze list
    })
    rel = node.execute(ctx)
    assert sorted(rel.columns) == ["HW", "SW", "month"]
    sw = rel.columns.index("SW")
    # SW present but NULL (no fill requested)
    assert all(r[sw] is None for r in rel.fetchall())


def test_pivot_fill_replaces_empty_cells():
    ctx = _ctx()
    _seed(ctx, "src", (
        "SELECT * FROM (VALUES ('Jan', 'HW', 100), ('Feb', 'SW', 250)) "
        "AS t(month, category, amount)"
    ))
    node = PivotNode(params={
        "_input_step_ids": ["src"],
        "pivot_column": "category",
        "value_column": "amount",
        "agg_function": "SUM",
        "group_by": ["month"],
        "pivot_values": "HW, SW",
        "fill_value": "0",               # numeric fill
    })
    rel = node.execute(ctx)
    cols = rel.columns
    rows = {r[cols.index("month")]: r for r in rel.fetchall()}
    hw, sw = cols.index("HW"), cols.index("SW")
    # Jan has HW only → SW filled with 0; Feb has SW only → HW filled with 0
    assert rows["Jan"][hw] == 100 and rows["Jan"][sw] == 0
    assert rows["Feb"][hw] == 0 and rows["Feb"][sw] == 250


def test_pivot_unknown_column_errors():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT * FROM (VALUES ('Jan', 100)) AS t(month, amount)")
    node = PivotNode(params={
        "_input_step_ids": ["src"],
        "pivot_column": "nope",
        "value_column": "amount",
    })
    import pytest
    with pytest.raises(ValueError, match="not found"):
        node.execute(ctx)
