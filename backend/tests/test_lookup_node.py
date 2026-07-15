"""LookupNode contract — a real lookup, not a disguised LEFT JOIN.

2026-06-11 validation-audit follow-up. Pins the upgraded parameter set
(explicit reference input, split keys, no-match / multiple-match policy,
return-column projection) AND the legacy defaults so old pipelines that
only set `lookup_key` keep producing the same joins.
"""
from __future__ import annotations

import duckdb
import pytest

from fpulse.nodes.base import ExecutionContext
from fpulse.nodes.activities import LookupNode


def _ctx() -> ExecutionContext:
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def _seed(ctx: ExecutionContext, step_id: str, sql: str):
    rel = ctx.conn.sql(sql)
    ctx.set_result(step_id, rel)
    return rel


def _orders(ctx):
    return _seed(ctx, "orders", (
        "SELECT * FROM (VALUES "
        "(1, 101, 50.0), (2, 102, 75.0), (3, 999, 20.0)"
        ") AS t(order_id, customer_id, amount)"
    ))


def _customers(ctx):
    return _seed(ctx, "customers", (
        "SELECT * FROM (VALUES "
        "(101, 'Asha', 'IN'), (102, 'Bo', 'SE')"
        ") AS t(customer_id, customer_name, country)"
    ))


def test_legacy_defaults_left_join_appends_ref_columns_without_key_dup():
    """Old pipelines set only lookup_key; second connection is the reference."""
    ctx = _ctx()
    _orders(ctx)
    _customers(ctx)
    node = LookupNode(params={
        "_input_step_ids": ["orders", "customers"],
        "lookup_key": "customer_id",
    })
    rel = node.execute(ctx)
    # main cols + ref cols MINUS the key (no duplicate customer_id column)
    assert rel.columns == ["order_id", "customer_id", "amount", "customer_name", "country"]
    rows = sorted(rel.fetchall())
    assert rows == [
        (1, 101, 50.0, "Asha", "IN"),
        (2, 102, 75.0, "Bo", "SE"),
        (3, 999, 20.0, None, None),   # keep = LEFT JOIN keeps unmatched rows
    ]


def test_lookup_input_id_makes_connection_order_irrelevant():
    """Reference chosen by step id, even when wired as the FIRST connection."""
    ctx = _ctx()
    _orders(ctx)
    _customers(ctx)
    node = LookupNode(params={
        "_input_step_ids": ["customers", "orders"],   # reversed order
        "lookup_input_id": "customers",
        "lookup_key": "customer_id",
    })
    rel = node.execute(ctx)
    assert rel.columns == ["order_id", "customer_id", "amount", "customer_name", "country"]
    assert len(rel.fetchall()) == 3


def test_split_keys_match_differently_named_columns():
    ctx = _ctx()
    _seed(ctx, "orders", "SELECT * FROM (VALUES (1, 101)) AS t(order_id, cust)")
    _customers(ctx)
    node = LookupNode(params={
        "_input_step_ids": ["orders", "customers"],
        "main_key": "cust",
        "lookup_key": "customer_id",
    })
    rows = node.execute(ctx).fetchall()
    assert rows == [(1, 101, "Asha", "IN")]


def test_no_match_drop_is_inner_join():
    ctx = _ctx()
    _orders(ctx)
    _customers(ctx)
    node = LookupNode(params={
        "_input_step_ids": ["orders", "customers"],
        "lookup_key": "customer_id",
        "no_match": "drop",
    })
    rows = node.execute(ctx).fetchall()
    assert sorted(r[0] for r in rows) == [1, 2]   # order 3 (customer 999) dropped


def test_multiple_match_first_prevents_fanout():
    ctx = _ctx()
    _seed(ctx, "orders", "SELECT * FROM (VALUES (1, 101)) AS t(order_id, customer_id)")
    _seed(ctx, "customers", (
        "SELECT * FROM (VALUES (101, 'Asha'), (101, 'Asha-dup')) "
        "AS t(customer_id, customer_name)"
    ))
    fanout = LookupNode(params={
        "_input_step_ids": ["orders", "customers"],
        "lookup_key": "customer_id",
    })
    assert len(fanout.execute(ctx).fetchall()) == 2   # default 'all' fans out

    first = LookupNode(params={
        "_input_step_ids": ["orders", "customers"],
        "lookup_key": "customer_id",
        "multiple_match": "first",
    })
    assert len(first.execute(ctx).fetchall()) == 1


def test_return_columns_projection_and_unknown_column_error():
    ctx = _ctx()
    _orders(ctx)
    _customers(ctx)
    node = LookupNode(params={
        "_input_step_ids": ["orders", "customers"],
        "lookup_key": "customer_id",
        "return_columns": ["country"],
    })
    rel = node.execute(ctx)
    assert rel.columns == ["order_id", "customer_id", "amount", "country"]

    bad = LookupNode(params={
        "_input_step_ids": ["orders", "customers"],
        "lookup_key": "customer_id",
        "return_columns": ["segment"],
    })
    with pytest.raises(ValueError, match="segment"):
        bad.execute(ctx)


def test_colliding_return_column_gets_lookup_suffix():
    ctx = _ctx()
    _seed(ctx, "orders", "SELECT * FROM (VALUES (1, 101, 'web')) AS t(order_id, customer_id, source)")
    _seed(ctx, "customers", "SELECT * FROM (VALUES (101, 'crm')) AS t(customer_id, source)")
    node = LookupNode(params={
        "_input_step_ids": ["orders", "customers"],
        "lookup_key": "customer_id",
    })
    rel = node.execute(ctx)
    assert rel.columns == ["order_id", "customer_id", "source", "source_lookup"]
    assert rel.fetchall() == [(1, 101, "web", "crm")]


def test_missing_key_columns_raise_clear_errors():
    ctx = _ctx()
    _orders(ctx)
    _customers(ctx)
    with pytest.raises(ValueError, match="main-stream key"):
        LookupNode(params={
            "_input_step_ids": ["orders", "customers"],
            "main_key": "nope",
            "lookup_key": "customer_id",
        }).execute(ctx)
    with pytest.raises(ValueError, match="lookup-dataset key"):
        # main_key valid on its side; only the reference-side key is wrong
        LookupNode(params={
            "_input_step_ids": ["orders", "customers"],
            "main_key": "customer_id",
            "lookup_key": "nope",
        }).execute(ctx)
