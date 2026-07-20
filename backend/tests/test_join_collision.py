"""Join duplicate-column collision handling (2026-06-15 node-audit).

The default projection used to be `SELECT *`, which emits two columns of the
same name whenever both inputs share one — and ALWAYS for the same_key join
keys. DuckDB keeps both, leaving downstream references ambiguous. The node now
builds an explicit, collision-safe projection:
  * same_key join keys collapse to one COALESCE(left, right) AS key column,
  * shared non-key columns keep the left copy and suffix the right one,
  * non-colliding joins are byte-identical to the old `SELECT *`.
"""
from __future__ import annotations

import duckdb

from fpulse.nodes.base import ExecutionContext
from fpulse.nodes.join import JoinNode


def _ctx() -> ExecutionContext:
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def _seed(ctx: ExecutionContext, step_id: str, sql: str):
    rel = ctx.conn.sql(sql)
    ctx.set_result(step_id, rel)
    return rel


def test_same_key_collapses_join_key_no_dup_column():
    ctx = _ctx()
    _seed(ctx, "orders", "SELECT * FROM (VALUES (1, 50.0)) AS t(customer_id, amount)")
    _seed(ctx, "customers", "SELECT * FROM (VALUES (1, 'Asha')) AS t(customer_id, name)")
    node = JoinNode(params={
        "_input_step_ids": ["orders", "customers"],
        "left_input_id": "orders",
        "join_type": "INNER",
        "key_mode": "same_key",
        "join_key": ["customer_id"],
    })
    out = node.execute(ctx)
    # single customer_id column (not two), plus amount + name
    assert out.columns == ["customer_id", "amount", "name"]
    assert out.fetchall() == [(1, 50.0, "Asha")]


def test_shared_non_key_column_is_suffixed():
    ctx = _ctx()
    # both sides carry a "name" column that is NOT the join key
    _seed(ctx, "orders", "SELECT * FROM (VALUES (1, 'order-A')) AS t(customer_id, name)")
    _seed(ctx, "customers", "SELECT * FROM (VALUES (1, 'Asha')) AS t(customer_id, name)")
    node = JoinNode(params={
        "_input_step_ids": ["orders", "customers"],
        "left_input_id": "orders",
        "join_type": "INNER",
        "key_mode": "same_key",
        "join_key": ["customer_id"],
    })
    out = node.execute(ctx)
    assert out.columns == ["customer_id", "name", "name_right"]
    assert out.fetchall() == [(1, "order-A", "Asha")]


def test_full_join_coalesces_key_for_right_only_rows():
    ctx = _ctx()
    _seed(ctx, "left", "SELECT * FROM (VALUES (1, 'L1')) AS t(id, lval)")
    _seed(ctx, "right", "SELECT * FROM (VALUES (2, 'R2')) AS t(id, rval)")
    node = JoinNode(params={
        "_input_step_ids": ["left", "right"],
        "left_input_id": "left",
        "join_type": "FULL",
        "key_mode": "same_key",
        "join_key": ["id"],
    })
    rows = sorted(node.execute(ctx).fetchall(), key=lambda r: r[0])
    # the right-only row keeps id=2 via COALESCE (would be NULL if we just
    # dropped the right key and kept left's)
    assert rows == [(1, "L1", None), (2, None, "R2")]


def test_custom_suffix_applies():
    ctx = _ctx()
    _seed(ctx, "a", "SELECT * FROM (VALUES (1, 'x')) AS t(id, status)")
    _seed(ctx, "b", "SELECT * FROM (VALUES (1, 'y')) AS t(id, status)")
    node = JoinNode(params={
        "_input_step_ids": ["a", "b"],
        "left_input_id": "a",
        "join_type": "INNER",
        "key_mode": "same_key",
        "join_key": ["id"],
        "dup_column_suffix": "_b",
    })
    out = node.execute(ctx)
    assert out.columns == ["id", "status", "status_b"]


def test_non_colliding_join_keeps_all_columns_in_order():
    ctx = _ctx()
    _seed(ctx, "orders", "SELECT * FROM (VALUES (1, 50.0)) AS t(customer_id, amount)")
    _seed(ctx, "customers", "SELECT * FROM (VALUES (1, 'Asha')) AS t(customer_id, full_name)")
    node = JoinNode(params={
        "_input_step_ids": ["orders", "customers"],
        "left_input_id": "orders",
        "join_type": "LEFT",
        "key_mode": "same_key",
        "join_key": ["customer_id"],
    })
    out = node.execute(ctx)
    assert out.columns == ["customer_id", "amount", "full_name"]
