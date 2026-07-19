"""Branch-output routing — strictly-additive port filtering (2026-06-11).

Branch nodes (conditional_split) tag rows with `_split_output`. Until now
nothing consumed that tag, so wiring a node to a specific output handle did
not actually route. `ExecutionContext.route_relation` / `get_routed_inputs`
now filter an input down to its branch when (and only when) the input
carries `_split_output` AND the edge's `from_port` is a real branch name.

These tests pin BOTH halves of the contract:
  * routing works for branch-tagged inputs, and
  * every legacy shape (from_port="output", or no `_split_output`) is
    returned completely unchanged.
"""
from __future__ import annotations

import duckdb

from fpulse.nodes.base import ExecutionContext
from fpulse.nodes.flow_control import IfConditionNode


def _ctx() -> ExecutionContext:
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def _seed(ctx: ExecutionContext, sid: str, sql: str):
    rel = ctx.conn.sql(sql)
    ctx.set_result(sid, rel)
    return rel


# ── route_relation ──────────────────────────────────────────────────────

def test_route_relation_filters_branch_and_drops_tag():
    ctx = _ctx()
    rel = ctx.conn.sql("SELECT * FROM (VALUES (1,'a'),(2,'b'),(3,'a')) AS t(id, _split_output)")
    routed = ctx.route_relation(rel, "a")
    assert sorted(routed.fetchall()) == [(1,), (3,)]
    assert "_split_output" not in routed.columns


def test_route_relation_output_port_is_passthrough():
    ctx = _ctx()
    rel = ctx.conn.sql("SELECT * FROM (VALUES (1,'a'),(2,'b')) AS t(id, _split_output)")
    routed = ctx.route_relation(rel, "output")
    assert routed.fetchall() == rel.fetchall()
    assert "_split_output" in routed.columns       # legacy port: untouched


def test_route_relation_without_split_column_is_unchanged():
    ctx = _ctx()
    rel = ctx.conn.sql("SELECT * FROM (VALUES (1,'x')) AS t(id, name)")
    assert ctx.route_relation(rel, "true").fetchall() == [(1, "x")]


# ── get_routed_inputs ───────────────────────────────────────────────────

def test_get_routed_inputs_legacy_no_ports_unchanged():
    ctx = _ctx()
    _seed(ctx, "s1", "SELECT 1 AS id")
    assert ctx.get_routed_inputs(["s1"], None)[0].fetchall() == [(1,)]


def test_get_routed_inputs_routes_by_port():
    ctx = _ctx()
    _seed(ctx, "split", "SELECT * FROM (VALUES (1,'hi'),(2,'lo'),(3,'hi')) AS t(id, _split_output)")
    out = ctx.get_routed_inputs(["split"], [("split", "hi", "input")])
    assert sorted(out[0].fetchall()) == [(1,), (3,)]


# ── integration: a flow-control node downstream of a branch port ─────────

def test_if_condition_consumes_only_its_branch():
    ctx = _ctx()
    _seed(ctx, "split", "SELECT * FROM (VALUES (1,'hi'),(2,'lo'),(3,'hi')) AS t(id, _split_output)")
    node = IfConditionNode(params={
        "_input_step_ids": ["split"],
        "_input_step_ports": [("split", "hi", "input")],
        "condition": "1=1",
    })
    out = node.execute(ctx)
    # Only the 'hi' branch was consumed; If now re-tags every row true/false.
    ids = sorted(r[out.columns.index("id")] for r in out.fetchall())
    assert ids == [1, 3]


def test_if_condition_branches_true_false():
    """If Condition is now a true/false brancher (2026-06-15 control-flow alignment): it
    tags every row, and routing to the 'true' port reproduces the old filter
    (legacy edges are migrated output→true), while 'false' gets the rest."""
    ctx = _ctx()
    _seed(ctx, "src", "SELECT * FROM (VALUES (1),(2),(3)) AS t(id)")
    node = IfConditionNode(params={"_input_step_ids": ["src"], "condition": "id > 1"})
    rel = node.execute(ctx)
    assert "_split_output" in rel.columns
    assert sorted(ctx.route_relation(rel, "true").fetchall()) == [(2,), (3,)]
    assert sorted(ctx.route_relation(rel, "false").fetchall()) == [(1,)]
