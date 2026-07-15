"""Node security fixes (2026-06-15, #18 from the doc-audit appendix).

  * SQL-literal injection: branch labels / case values are user-supplied and
    were interpolated into SQL string literals unescaped — a single quote
    broke the query and was an injection vector. They are now escaped.
  * SSRF: Slack/Teams webhook POST now runs the same check_url guard as
    HTTP Request (blocks loopback / private / cloud-metadata hosts).
"""
from __future__ import annotations

import duckdb
import pytest

from fpulse.nodes.base import ExecutionContext
from fpulse.nodes.activities import ConditionalSplitNode
from fpulse.nodes.flow_control import SwitchCaseNode, SlackNotifyNode


def _ctx() -> ExecutionContext:
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def _seed(ctx, sid, sql):
    rel = ctx.conn.sql(sql)
    ctx.set_result(sid, rel)
    return rel


def test_conditional_split_escapes_quote_in_branch_name():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT * FROM (VALUES (1),(2)) AS t(id)")
    node = ConditionalSplitNode(params={
        "_input_step_ids": ["src"],
        # a single quote in the label would previously break the SQL
        "conditions": [{"name": "O'Brien", "condition": "id = 1"}],
        "default_output": "rest",
    })
    out = node.execute(ctx)
    so = out.columns.index("_split_output")
    labels = {r[so] for r in out.fetchall()}
    assert labels == {"O'Brien", "rest"}


def test_switch_case_escapes_quote_in_value():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT * FROM (VALUES (1,'O''Brien'),(2,'Smith')) AS t(id, name)")
    node = SwitchCaseNode(params={
        "_input_step_ids": ["src"],
        "column": "name",
        "active_case": "O'Brien",
    })
    rows = node.execute(ctx).fetchall()
    assert [r[0] for r in rows] == [1]  # matched the quoted value, no SQL break


def test_slack_notify_blocks_ssrf_metadata_host():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT 1 AS id")
    node = SlackNotifyNode(params={
        "_input_step_ids": ["src"],
        # cloud-metadata endpoint — must be blocked by the SSRF guard
        "webhook_url": "http://169.254.169.254/latest/meta-data/",
        "message": "hi",
    })
    with pytest.raises(ValueError, match="blocked"):
        node.execute(ctx)
