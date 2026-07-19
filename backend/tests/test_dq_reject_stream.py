"""Data Quality reject stream (2026-06-11) — multi-output A1.

`mode='reject'` tags every row pass/reject via _split_output and exposes two
output streams. Pins the node-level emit AND end-to-end routing: a DQ reject
node feeding two ordinary Filter nodes (plain get_inputs) — each receives only
its branch, tag stripped, via the central executor routing.
"""
from __future__ import annotations

import duckdb

from fpulse.ir.schema import Workflow, Step, StepType, StepConnection
from fpulse.engine.executor import WorkflowExecutor
from fpulse.nodes.base import ExecutionContext
from fpulse.nodes.quality import DataQualityNode


def _ctx() -> ExecutionContext:
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def test_reject_mode_tags_pass_and_reject():
    ctx = _ctx()
    rel = ctx.conn.sql("SELECT * FROM (VALUES (1,'a@x.com'),(2,NULL),(3,'c@x.com')) AS t(id, email)")
    ctx.set_result("src", rel)
    node = DataQualityNode(params={
        "_input_step_ids": ["src"], "_step_id": "dq", "mode": "reject",
        "rules": [{"op": "not_null", "column": "email"}],
    })
    out = node.execute(ctx)
    rows = {r[0]: r for r in out.fetchall()}
    tag_idx = out.columns.index("_split_output")
    assert rows[1][tag_idx] == "pass"
    assert rows[2][tag_idx] == "reject"     # null email
    assert rows[3][tag_idx] == "pass"


def test_reject_stream_routes_to_plain_downstream(sample_csv_file, temp_data_dir):
    executor = WorkflowExecutor(data_dir=temp_data_dir)
    wf = Workflow(
        id="test-dq-reject",
        steps=[
            Step(id="src", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
            Step(id="dq", type=StepType.DATA_QUALITY, params={
                "mode": "reject",
                "rules": [{"op": "eq", "column": "status", "value": "active"}],
            }),
            Step(id="good", type=StepType.FILTER, params={"condition": "1=1"}),
            Step(id="bad", type=StepType.FILTER, params={"condition": "1=1"}),
        ],
        connections=[
            StepConnection(from_step="src", to_step="dq"),
            StepConnection(from_step="dq", to_step="good", from_port="pass"),
            StepConnection(from_step="dq", to_step="bad", from_port="reject"),
        ],
    )
    result = executor.execute_workflow(wf)
    assert result.status == "success", result.step_results
    total = result.step_results["src"].row_count
    n_pass = result.step_results["good"].row_count
    n_rej = result.step_results["bad"].row_count
    assert n_pass == 3                      # active rows
    assert n_pass + n_rej == total          # exact partition
    # tag stripped from the routed downstream
    cols = result.step_results["good"].columns or []
    names = [c.get("name") if isinstance(c, dict) else c for c in cols]
    assert "_split_output" not in names
