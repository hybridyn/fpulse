"""Set Variable node — repurposed to a real $vars setter (2026-06-15).

Previously this node appended COLUMNS (`SELECT *, expr AS name`), making it a
duplicate of Derived Column AND a misnomer (it never wrote $vars). It now
evaluates each expression once and stores the scalar on ctx.vars[name], read
downstream as {{ $vars.NAME }}. Input rows pass through UNCHANGED.
"""
from __future__ import annotations

import duckdb
import pytest

from fpulse.nodes.base import ExecutionContext
from fpulse.nodes.flow_control import SetVariableNode


def _ctx() -> ExecutionContext:
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def _seed(ctx: ExecutionContext, step_id: str, sql: str):
    rel = ctx.conn.sql(sql)
    ctx.set_result(step_id, rel)
    return rel


def test_constant_sets_vars_and_passes_input_through():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT * FROM (VALUES (1, 'a'), (2, 'b')) AS t(id, name)")
    node = SetVariableNode(params={
        "_input_step_ids": ["src"],
        "variables": [
            {"name": "env", "expression": "'prod'"},
            {"name": "batch_size", "expression": "100"},
        ],
    })
    out = node.execute(ctx)

    # vars captured on the context
    assert ctx.vars["env"] == "prod"
    assert ctx.vars["batch_size"] == 100

    # input is passed through UNCHANGED — no columns added
    rows = out.fetchall()
    assert len(rows) == 2
    assert out.columns == ["id", "name"]


def test_aggregate_expression_over_input():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT * FROM (VALUES (1, 10), (2, 30), (3, 20)) AS t(id, amount)")
    node = SetVariableNode(params={
        "_input_step_ids": ["src"],
        "variables": [{"name": "max_amount", "expression": "MAX(amount)"}],
    })
    node.execute(ctx)
    assert ctx.vars["max_amount"] == 30


def test_no_input_constant_only():
    ctx = _ctx()
    node = SetVariableNode(params={
        "_input_step_ids": [],
        "variables": [{"name": "greeting", "expression": "'hello'"}],
    })
    out = node.execute(ctx)
    assert ctx.vars["greeting"] == "hello"
    # empty pass-through relation, doesn't crash
    assert out.fetchall() == []


def test_bad_expression_raises_clear_error():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT 1 AS id")
    node = SetVariableNode(params={
        "_input_step_ids": ["src"],
        "variables": [{"name": "x", "expression": "no_such_col + 1"}],
    })
    with pytest.raises(ValueError, match="Set Variable 'x'"):
        node.execute(ctx)


def test_blank_entries_skipped():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT 1 AS id")
    node = SetVariableNode(params={
        "_input_step_ids": ["src"],
        "variables": [
            {"name": "", "expression": "'ignored'"},
            {"name": "kept", "expression": "'yes'"},
            {"name": "noexpr", "expression": ""},
        ],
    })
    node.execute(ctx)
    assert ctx.vars == {"kept": "yes"}
