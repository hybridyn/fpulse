"""Transform registers ONLY its directly-connected inputs — not every ancestor.

Node-contract hardening (2026-06-10): the Transform node previously
auto-registered every executed ancestor's output as a named DuckDB table,
so SQL could silently reference a grandparent node that was not wired into
the Transform (a hidden cross-node dependency). It now registers only the
inputs reachable via the edges the user actually drew (`_input_step_ids`):
the first as `source_table` / `input`, and each input by its sanitized
node label.
"""
from __future__ import annotations

import duckdb
import pytest

from fpulse.nodes.base import ExecutionContext
from fpulse.nodes.transform import TransformNode


def _ctx() -> ExecutionContext:
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def _seed(ctx: ExecutionContext, step_id: str, sql: str):
    rel = ctx.conn.sql(sql)
    ctx.set_result(step_id, rel)
    return rel


def test_source_table_is_first_direct_input():
    ctx = _ctx()
    _seed(ctx, "src1", "SELECT 1 AS id, 'a' AS name")
    node = TransformNode(params={
        "_input_step_ids": ["src1"],
        "expression": "SELECT id, name FROM source_table",
    })
    assert node.execute(ctx).fetchall() == [(1, "a")]


def test_grandparent_not_referenceable():
    """A node that is executed but NOT a direct input must not be registered."""
    ctx = _ctx()
    _seed(ctx, "grandparent", "SELECT 99 AS secret")   # ran, but NOT wired in
    _seed(ctx, "src1", "SELECT 1 AS id")
    node = TransformNode(params={
        "_input_step_ids": ["src1"],
        "_node_labels": {"grandparent": "Grandparent", "src1": "Source"},
        "expression": "SELECT * FROM grandparent",       # references a non-input
    })
    # Under the old all-ancestors registration this succeeded; now it must
    # fail because `grandparent` is not a direct input of this Transform.
    with pytest.raises(Exception):
        node.execute(ctx)


def test_direct_inputs_registered_by_label():
    """Each directly-connected input is referenceable by its sanitized label."""
    ctx = _ctx()
    _seed(ctx, "a", "SELECT 1 AS k, 10 AS lv")
    _seed(ctx, "b", "SELECT 1 AS k, 'x' AS dept")
    node = TransformNode(params={
        "_input_step_ids": ["a", "b"],
        "_node_labels": {"a": "Left Table", "b": "Right Table"},
        "expression": (
            "SELECT l.k, l.lv, r.dept FROM left_table l "
            "JOIN right_table r ON l.k = r.k"
        ),
    })
    assert node.execute(ctx).fetchall() == [(1, 10, "x")]


def test_input_referenceable_by_edge_alias():
    """A per-edge alias lets SQL read a user-named table instead of the label."""
    ctx = _ctx()
    _seed(ctx, "a", "SELECT 1 AS k, 10 AS lv")
    _seed(ctx, "b", "SELECT 1 AS k, 'x' AS dept")
    node = TransformNode(params={
        "_input_step_ids": ["a", "b"],
        "_node_labels": {"a": "Left Table", "b": "Right Table"},
        # User aliases — note the second is sanitized ("lookup table" -> lookup_table).
        "_input_step_aliases": {"a": "orders", "b": "lookup table"},
        "expression": (
            "SELECT o.k, o.lv, lt.dept FROM orders o "
            "JOIN lookup_table lt ON o.k = lt.k"
        ),
    })
    assert node.execute(ctx).fetchall() == [(1, 10, "x")]


def test_alias_is_additive_label_still_works():
    """The alias is registered IN ADDITION to the sanitized label."""
    ctx = _ctx()
    _seed(ctx, "a", "SELECT 5 AS n")
    node = TransformNode(params={
        "_input_step_ids": ["a"],
        "_node_labels": {"a": "Feed"},
        "_input_step_aliases": {"a": "orders"},
        # `orders` (alias) and `feed` (label) both resolve to the same relation.
        "expression": "SELECT orders.n + feed.n AS total FROM orders JOIN feed ON orders.n = feed.n",
    })
    assert node.execute(ctx).fetchall() == [(10,)]


def test_executor_stamps_edge_alias_onto_consumer():
    """_build_input_map records connection.alias as {from_step: alias} on the consumer."""
    from fpulse.ir.schema import Workflow, Step, StepConnection, StepType
    from fpulse.engine.executor import WorkflowExecutor

    src = Step(id="s1", type=StepType.TRANSFORM, label="Orders Feed")
    dst = Step(id="t1", type=StepType.TRANSFORM, label="Joiner")
    wf = Workflow(steps=[src, dst], connections=[
        StepConnection(from_step="s1", to_step="t1", alias="orders"),
    ])
    WorkflowExecutor._build_input_map(None, wf)  # method ignores self
    assert dst.params.get("_input_step_aliases") == {"s1": "orders"}

    # A connection with no alias leaves the consumer's params untouched.
    a = Step(id="a", type=StepType.TRANSFORM)
    b = Step(id="b", type=StepType.TRANSFORM)
    wf2 = Workflow(steps=[a, b], connections=[StepConnection(from_step="a", to_step="b")])
    WorkflowExecutor._build_input_map(None, wf2)
    assert "_input_step_aliases" not in b.params

    # The IR field itself defaults to None (additive, non-breaking).
    assert StepConnection(from_step="x", to_step="y").alias is None
