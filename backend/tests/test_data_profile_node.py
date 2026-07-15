"""Smoke tests for the DataProfileNode (May 3 2026 Sprint 1)."""

from __future__ import annotations

import duckdb
import pytest

from fpulse.nodes.base import ExecutionContext
from fpulse.nodes.quality import DataProfileNode


@pytest.fixture
def ctx():
    conn = duckdb.connect(":memory:")
    return ExecutionContext(conn=conn)


def _make_input(ctx: ExecutionContext, sql: str):
    """Helper: register a relation as 'upstream', return its step_id."""
    rel = ctx.conn.sql(sql)
    ctx.set_result("upstream_1", rel)
    return rel


def test_profile_basic_columns(ctx):
    _make_input(ctx, """
        SELECT * FROM (VALUES
            (1, 'alice', 10.5, NULL),
            (2, 'bob',   20.0, 'x'),
            (3, 'alice', 30.0, 'y'),
            (4, NULL,    40.0, 'x'),
            (5, 'carol', 50.0, NULL)
        ) AS t(id, name, amount, tag)
    """)
    node = DataProfileNode(params={"_input_step_ids": ["upstream_1"]})
    result = node.execute(ctx)
    rows = {r[0]: r for r in result.fetchall()}

    assert set(rows) == {"id", "name", "amount", "tag"}

    # id column: no nulls, all distinct
    id_row = rows["id"]
    assert id_row[3] == 0       # null_count
    assert id_row[4] == 0.0     # null_pct
    assert id_row[5] == 5       # distinct_count

    # name column: 1 null, 'alice' is the top value (appears twice)
    name_row = rows["name"]
    assert name_row[3] == 1     # 1 null
    assert name_row[4] == 20.0  # 20% null
    assert name_row[9] == "alice"  # top_value
    assert name_row[10] == 2    # top_value_count

    # tag column: 2 nulls, 'x' top with 2 occurrences
    tag_row = rows["tag"]
    assert tag_row[3] == 2
    assert tag_row[9] == "x"
    assert tag_row[10] == 2


def test_profile_empty_source(ctx):
    _make_input(ctx, "SELECT 1 AS a WHERE FALSE")
    node = DataProfileNode(params={"_input_step_ids": ["upstream_1"]})
    result = node.execute(ctx)
    rows = result.fetchall()
    # Empty source: empty profile is fine
    assert rows == []


def test_profile_min_max_for_numeric(ctx):
    _make_input(ctx, "SELECT * FROM (VALUES (1, 10), (2, 20), (3, 30)) AS t(id, val)")
    node = DataProfileNode(params={"_input_step_ids": ["upstream_1"]})
    result = node.execute(ctx)
    rows = {r[0]: r for r in result.fetchall()}

    assert rows["val"][7] == "10"   # min_value (cast to VARCHAR)
    assert rows["val"][8] == "30"   # max_value


def test_profile_top_value_disabled(ctx):
    _make_input(ctx, "SELECT * FROM (VALUES ('a'), ('a'), ('b')) AS t(c)")
    node = DataProfileNode(params={
        "_input_step_ids": ["upstream_1"],
        "include_top_value": False,
    })
    result = node.execute(ctx)
    rows = result.fetchall()
    assert len(rows) == 1
    assert rows[0][9] is None       # top_value
    assert rows[0][10] == 0         # top_value_count


def test_profile_no_input_raises(ctx):
    node = DataProfileNode(params={"_input_step_ids": []})
    with pytest.raises(ValueError, match="no input"):
        node.execute(ctx)


def test_profile_default_params():
    # 2026-06-15: a stale 2nd default_params/param_schema pair used to SHADOW
    # the real one (Python keeps the later definition), hiding
    # include/exclude_columns from the UI. C2 removed the stale pair and added
    # passthrough_data — these now assert the real, de-shadowed schema.
    assert DataProfileNode.default_params() == {
        "sample_rows": 0,
        "include_top_value": True,
        "include_columns": [],
        "exclude_columns": [],
        "passthrough_data": False,
    }


def test_profile_param_schema_shape():
    schema = DataProfileNode.param_schema()
    assert {p["name"] for p in schema} == {
        "sample_rows", "include_top_value",
        "include_columns", "exclude_columns", "passthrough_data",
    }
