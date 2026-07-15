"""For Each (Run Pipeline) — true per-item loop (2026-06-11).

Per-row loop: run a sub-pipeline once per input row,
injecting the row as the sub-pipeline's parameters. The sub-execution itself
is ExecutePipeline's well-tested machinery; these tests pin the LOOP logic —
iteration count, per-row param injection, the item param, error policy, the
safety cap, and passthrough — by monkeypatching the sub-runner.
"""
from __future__ import annotations

import duckdb
import pytest

from fpulse.nodes.base import ExecutionContext
from fpulse.nodes import flow_control
from fpulse.nodes.flow_control import ForEachPipelineNode


def _ctx() -> ExecutionContext:
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def _seed(ctx, sid, sql):
    rel = ctx.conn.sql(sql)
    ctx.set_result(sid, rel)
    return rel


class _OK:
    status = "success"
    error = ""


def _items(ctx):
    return _seed(ctx, "items",
                 "SELECT * FROM (VALUES (1,'a'),(2,'b'),(3,'c')) AS t(id, name)")


def test_runs_subpipeline_once_per_row_and_passes_through(monkeypatch):
    ctx = _ctx()
    _items(ctx)
    calls = []
    monkeypatch.setattr(flow_control, "_run_subpipeline",
                        lambda c, pid, ov: (calls.append((pid, dict(ov))), _OK())[1])
    node = ForEachPipelineNode(params={"_input_step_ids": ["items"], "pipeline_id": "child"})
    out = node.execute(ctx)

    assert len(calls) == 3
    # per-row columns injected by name + whole row under the item param
    assert calls[0][0] == "child"
    assert calls[0][1]["id"] == 1 and calls[0][1]["name"] == "a"
    assert calls[0][1]["item"] == {"id": 1, "name": "a"}
    # input passes through unchanged (ForEach is control flow)
    assert sorted(out.fetchall()) == [(1, "a"), (2, "b"), (3, "c")]


def test_static_params_merged_and_overridden_by_row(monkeypatch):
    ctx = _ctx()
    _seed(ctx, "items", "SELECT * FROM (VALUES (1)) AS t(id)")
    seen = {}
    monkeypatch.setattr(flow_control, "_run_subpipeline",
                        lambda c, pid, ov: (seen.update(ov), _OK())[1])
    node = ForEachPipelineNode(params={
        "_input_step_ids": ["items"], "pipeline_id": "child",
        "parameters": {"env": "prod", "id": 999},
    })
    node.execute(ctx)
    assert seen["env"] == "prod"      # static carried
    assert seen["id"] == 1            # row overrides static


def test_custom_item_param_name(monkeypatch):
    ctx = _ctx()
    _seed(ctx, "items", "SELECT * FROM (VALUES (5)) AS t(id)")
    seen = {}
    monkeypatch.setattr(flow_control, "_run_subpipeline",
                        lambda c, pid, ov: (seen.update(ov), _OK())[1])
    node = ForEachPipelineNode(params={
        "_input_step_ids": ["items"], "pipeline_id": "child", "item_param": "row",
    })
    node.execute(ctx)
    assert seen["row"] == {"id": 5}


def test_on_item_error_fail_stops(monkeypatch):
    ctx = _ctx()
    _items(ctx)

    def boom(c, pid, ov):
        if ov["id"] == 2:
            raise RuntimeError("child blew up")
        return _OK()

    monkeypatch.setattr(flow_control, "_run_subpipeline", boom)
    node = ForEachPipelineNode(params={"_input_step_ids": ["items"], "pipeline_id": "child"})
    with pytest.raises(ValueError, match="item 2/3 failed"):
        node.execute(ctx)


def test_on_item_error_continue_skips(monkeypatch):
    ctx = _ctx()
    _items(ctx)
    ran = []

    def boom(c, pid, ov):
        ran.append(ov["id"])
        if ov["id"] == 2:
            raise RuntimeError("child blew up")
        return _OK()

    monkeypatch.setattr(flow_control, "_run_subpipeline", boom)
    node = ForEachPipelineNode(params={
        "_input_step_ids": ["items"], "pipeline_id": "child", "on_item_error": "continue",
    })
    out = node.execute(ctx)
    assert ran == [1, 2, 3]                 # all attempted despite item 2 failing
    assert sorted(out.fetchall()) == [(1, "a"), (2, "b"), (3, "c")]


def test_child_error_status_respected(monkeypatch):
    ctx = _ctx()
    _seed(ctx, "items", "SELECT * FROM (VALUES (1)) AS t(id)")

    class _Err:
        status = "error"
        error = "bad config"

    monkeypatch.setattr(flow_control, "_run_subpipeline", lambda c, pid, ov: _Err())
    node = ForEachPipelineNode(params={"_input_step_ids": ["items"], "pipeline_id": "child"})
    with pytest.raises(ValueError, match="sub-pipeline failed"):
        node.execute(ctx)


def test_max_iterations_cap_refuses_oversized_input(monkeypatch):
    ctx = _ctx()
    _seed(ctx, "items", "SELECT * FROM range(5) AS t(id)")
    monkeypatch.setattr(flow_control, "_run_subpipeline", lambda c, pid, ov: _OK())
    node = ForEachPipelineNode(params={
        "_input_step_ids": ["items"], "pipeline_id": "child", "max_iterations": 2,
    })
    with pytest.raises(ValueError, match="max_iterations is 2"):
        node.execute(ctx)


def test_missing_pipeline_id_and_input():
    ctx = _ctx()
    _seed(ctx, "items", "SELECT 1 AS id")
    with pytest.raises(ValueError, match="pipeline_id"):
        ForEachPipelineNode(params={"_input_step_ids": ["items"]}).execute(ctx)
    with pytest.raises(ValueError, match="input"):
        ForEachPipelineNode(params={"_input_step_ids": [], "pipeline_id": "child"}).execute(ctx)
