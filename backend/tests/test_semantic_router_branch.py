"""Semantic Router opt-in branching (B4, 2026-06-15).

When `route_outputs` is on, the router tags each row with `_split_output` =
its matched label, so the executor routes rows to one output port per label
(+ the default). Off by default → single output (existing behavior). Uses the
deterministic `hash` provider so the test is offline + stable.
"""
from __future__ import annotations

import duckdb

from fpulse.nodes.base import ExecutionContext
from fpulse.nodes.ai import SemanticRouterNode


def _ctx() -> ExecutionContext:
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def _seed(ctx, sid, sql):
    rel = ctx.conn.sql(sql)
    ctx.set_result(sid, rel)
    return rel


def _node(route_outputs: bool) -> SemanticRouterNode:
    return SemanticRouterNode(params={
        "_input_step_ids": ["src"],
        "text_column": "msg",
        "labels": [
            {"name": "billing", "examples": ["invoice", "payment", "refund"]},
            {"name": "support", "examples": ["error", "bug", "broken"]},
        ],
        "provider": "hash",
        "default_label": "other",
        "route_outputs": route_outputs,
    })


def test_route_outputs_emits_split_output():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT * FROM (VALUES ('my invoice is wrong'),('the app crashed')) AS t(msg)")
    out = _node(True).execute(ctx)
    assert "_split_output" in out.columns
    so = out.columns.index("_split_output")
    allowed = {"billing", "support", "other"}
    assert all(r[so] in allowed for r in out.fetchall())


def test_no_route_outputs_is_single_output():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT * FROM (VALUES ('hello')) AS t(msg)")
    out = _node(False).execute(ctx)
    assert "_split_output" not in out.columns
    # still adds the label + score columns
    assert "__route" in out.columns
    assert "__route_score" in out.columns
