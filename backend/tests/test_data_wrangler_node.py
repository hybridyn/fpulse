"""Tests for the Data Wrangler node (stepwise visible transform).

Covers:
  - compile_wrangle() output shape for each of the 6 sub-step ops
  - disabled / unknown / empty step handling
  - identifier quoting safety
  - cast type validation allowlist
  - end-to-end DataWranglerNode.execute() against a real DuckDB relation
  - preview_steps() schema-delta computation
"""

from __future__ import annotations

import duckdb
import pytest

from fpulse.nodes.base import ExecutionContext
from fpulse.nodes.data_wrangler import (
    DataWranglerNode,
    compile_wrangle,
    list_step_ops,
    _q,
    _schema_delta,
    _validate_cast_type,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def ctx():
    conn = duckdb.connect(":memory:")
    return ExecutionContext(conn=conn)


def _make_upstream(ctx: ExecutionContext, sql: str, step_id: str = "upstream_1"):
    rel = ctx.conn.sql(sql)
    ctx.set_result(step_id, rel)
    return rel


def _orders_sample(ctx: ExecutionContext):
    """A small orders-like dataset used across multiple tests."""
    return _make_upstream(ctx, """
        SELECT * FROM (VALUES
            ('C1', 'active',   '2026-01-15', '100.50'),
            ('C2', 'inactive', '2026-02-03', '50.00'),
            ('C1', 'active',   '2026-03-22', '275.00'),
            ('C3', 'active',   '2026-04-11', '0.00'),
            ('C2', 'active',   '2026-05-01', '999.99')
        ) AS t(cust_id, status, ord_dt, amount)
    """)


# ─────────────────────────────────────────────────────────────────────────────
# Compiler unit tests
# ─────────────────────────────────────────────────────────────────────────────

def test_compile_empty_wrangler_is_identity():
    sql = compile_wrangle([], "src")
    assert sql == "SELECT * FROM src"


def test_compile_all_disabled_is_identity():
    sql = compile_wrangle(
        [{"op": "filter", "enabled": False, "config": {"mode": "expression", "expression": "1=2"}}],
        "src",
    )
    assert sql == "SELECT * FROM src"


def test_compile_unknown_op_is_skipped():
    sql = compile_wrangle(
        [{"op": "nonsense", "enabled": True, "config": {}}],
        "src",
    )
    assert sql == "SELECT * FROM src"


def test_compile_filter_expression():
    sql = compile_wrangle(
        [{"op": "filter", "enabled": True,
          "config": {"mode": "expression", "expression": "amount > 100"}}],
        "src",
    )
    assert "WHERE amount > 100" in sql
    assert "FROM src" in sql


def test_compile_filter_rules():
    sql = compile_wrangle(
        [{"op": "filter", "enabled": True,
          "config": {"mode": "rules", "combinator": "AND",
                     "rules": [{"column": "status", "op": "=", "value": "active"}]}}],
        "src",
    )
    assert '"status"' in sql
    assert "WHERE" in sql


def test_compile_select_projection():
    sql = compile_wrangle(
        [{"op": "select", "enabled": True, "config": {"columns": ["a", "b"]}}],
        "src",
    )
    assert sql.startswith('SELECT "a", "b" FROM')


def test_compile_rename_uses_duckdb_rename_clause():
    sql = compile_wrangle(
        [{"op": "rename", "enabled": True,
          "config": {"rename_map": {"old_a": "new_a", "old_b": "new_b"}}}],
        "src",
    )
    assert "RENAME" in sql
    assert '"old_a" AS "new_a"' in sql
    assert '"old_b" AS "new_b"' in sql


def test_compile_cast_uses_duckdb_replace():
    sql = compile_wrangle(
        [{"op": "cast", "enabled": True,
          "config": {"casts": [{"column": "amount", "to_type": "DECIMAL(18,2)"}]}}],
        "src",
    )
    assert "REPLACE" in sql
    assert 'CAST("amount" AS DECIMAL(18,2))' in sql


def test_compile_derive_adds_columns():
    sql = compile_wrangle(
        [{"op": "derive", "enabled": True,
          "config": {"derived": [{"name": "domain", "expression": "split_part(email,'@',2)"}]}}],
        "src",
    )
    assert "AS \"domain\"" in sql
    assert "split_part(email,'@',2)" in sql


def test_compile_group_by_aggregates():
    sql = compile_wrangle(
        [{"op": "group_by", "enabled": True,
          "config": {"keys": ["cust_id"],
                     "aggregations": [
                         {"func": "SUM", "column": "amount", "alias": "total"},
                         {"func": "COUNT", "column": "*",     "alias": "n"},
                     ]}}],
        "src",
    )
    assert 'SUM("amount") AS "total"' in sql
    assert 'COUNT(*) AS "n"' in sql
    assert 'GROUP BY "cust_id"' in sql


def test_compile_group_by_count_distinct():
    sql = compile_wrangle(
        [{"op": "group_by", "enabled": True,
          "config": {"keys": ["cust_id"],
                     "aggregations": [{"func": "COUNT_DISTINCT", "column": "status",
                                        "alias": "n_statuses"}]}}],
        "src",
    )
    assert 'COUNT(DISTINCT "status") AS "n_statuses"' in sql


def test_compile_chain_all_six_ops_wraps_subqueries():
    """A chain of all 6 ops should produce a valid SQL string."""
    steps = [
        {"op": "filter",   "enabled": True,
         "config": {"mode": "expression", "expression": "TRUE"}},
        {"op": "select",   "enabled": True, "config": {"columns": ["a", "b", "c"]}},
        {"op": "rename",   "enabled": True, "config": {"rename_map": {"a": "x"}}},
        {"op": "cast",     "enabled": True,
         "config": {"casts": [{"column": "b", "to_type": "INTEGER"}]}},
        {"op": "derive",   "enabled": True,
         "config": {"derived": [{"name": "d", "expression": "b + 1"}]}},
        {"op": "group_by", "enabled": True,
         "config": {"keys": ["x"], "aggregations": [{"func": "SUM", "column": "b", "alias": "tot"}]}},
    ]
    sql = compile_wrangle(steps, "src")
    # Subquery aliases _w0.._w5 should appear (some get filtered out by TRUE-only filter)
    assert "_w" in sql
    assert "GROUP BY" in sql


def test_invalid_input_table_rejected():
    with pytest.raises(ValueError):
        compile_wrangle([], "src; DROP TABLE x")


# ─────────────────────────────────────────────────────────────────────────────
# Helper unit tests
# ─────────────────────────────────────────────────────────────────────────────

def test_quote_identifier_escapes_embedded_quotes():
    assert _q('col') == '"col"'
    assert _q('with"quote') == '"with""quote"'
    assert _q('with space') == '"with space"'


def test_validate_cast_type_allowlist():
    assert _validate_cast_type("integer") == "INTEGER"
    assert _validate_cast_type("DECIMAL(10,2)") == "DECIMAL(10,2)"
    assert _validate_cast_type("decimal(5,0)") == "DECIMAL(5,0)"
    with pytest.raises(ValueError):
        _validate_cast_type("DROP TABLE")
    with pytest.raises(ValueError):
        _validate_cast_type("")
    with pytest.raises(ValueError):
        _validate_cast_type("FANCYTYPE")


def test_schema_delta_added_removed_retyped():
    prev = [("id", "INTEGER"), ("name", "VARCHAR"), ("amount", "VARCHAR")]
    curr = [("id", "INTEGER"), ("amount", "DECIMAL(18,2)"), ("year", "INTEGER")]
    delta = _schema_delta(prev, curr)
    assert delta["added"] == [{"name": "year", "type": "INTEGER"}]
    assert delta["removed"] == ["name"]
    assert delta["retyped"] == [{"name": "amount", "from": "VARCHAR", "to": "DECIMAL(18,2)"}]


def test_list_step_ops_returns_all_supported_ops():
    """Expanded May 2026: sort / dedupe / sample / flatten added to the
    original six. Test asserts the full current set."""
    ops = list_step_ops()
    assert set(ops) == {
        "filter", "select", "rename", "cast", "derive", "group_by",
        "sort", "dedupe", "sample", "flatten",
        # B3 (2026-06-15) cleaning ops
        "fill_nulls", "replace_values", "split_column",
    }


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end execute() tests
# ─────────────────────────────────────────────────────────────────────────────

def test_execute_empty_wrangler_passes_through(ctx):
    _orders_sample(ctx)
    node = DataWranglerNode(params={"_input_step_ids": ["upstream_1"], "steps": []})
    result = node.execute(ctx)
    assert result.count("*").fetchone()[0] == 5


def test_execute_filter_then_select(ctx):
    _orders_sample(ctx)
    node = DataWranglerNode(params={
        "_input_step_ids": ["upstream_1"],
        "steps": [
            {"op": "filter", "enabled": True,
             "config": {"mode": "rules", "combinator": "AND",
                         "rules": [{"column": "status", "op": "=", "value": "active"}]}},
            {"op": "select", "enabled": True, "config": {"columns": ["cust_id", "amount"]}},
        ],
    })
    result = node.execute(ctx)
    rows = result.fetchall()
    assert len(rows) == 4  # 4 active rows
    assert result.columns == ["cust_id", "amount"]


def test_execute_rename_then_cast(ctx):
    _orders_sample(ctx)
    node = DataWranglerNode(params={
        "_input_step_ids": ["upstream_1"],
        "steps": [
            {"op": "rename", "enabled": True,
             "config": {"rename_map": {"cust_id": "customer_id", "ord_dt": "order_date"}}},
            {"op": "cast", "enabled": True,
             "config": {"casts": [
                 {"column": "amount", "to_type": "DECIMAL(18,2)"},
                 {"column": "order_date", "to_type": "DATE"},
             ]}},
        ],
    })
    result = node.execute(ctx)
    assert "customer_id" in result.columns
    assert "order_date" in result.columns
    type_map = dict(zip(result.columns, [str(t) for t in result.types]))
    assert "DECIMAL" in type_map["amount"]
    assert type_map["order_date"] == "DATE"


def test_execute_derive_then_group_by(ctx):
    _orders_sample(ctx)
    node = DataWranglerNode(params={
        "_input_step_ids": ["upstream_1"],
        "steps": [
            {"op": "cast", "enabled": True,
             "config": {"casts": [{"column": "amount", "to_type": "DOUBLE"}]}},
            {"op": "derive", "enabled": True,
             "config": {"derived": [{"name": "double_amount", "expression": "amount * 2"}]}},
            {"op": "group_by", "enabled": True,
             "config": {"keys": ["cust_id"],
                         "aggregations": [
                             {"func": "SUM", "column": "double_amount", "alias": "total_doubled"},
                             {"func": "COUNT", "column": "*",            "alias": "n_orders"},
                         ]}},
        ],
    })
    result = node.execute(ctx)
    rows = {r[0]: r for r in result.fetchall()}
    # C1: 100.50 + 275.00 = 375.50  -> doubled = 751.00, 2 orders
    assert rows["C1"][2] == 2
    assert abs(rows["C1"][1] - 751.0) < 0.01


def test_execute_disabled_step_skipped(ctx):
    _orders_sample(ctx)
    node = DataWranglerNode(params={
        "_input_step_ids": ["upstream_1"],
        "steps": [
            {"op": "filter", "enabled": False,
             "config": {"mode": "expression", "expression": "1=0"}},  # would drop all
            {"op": "select", "enabled": True, "config": {"columns": ["cust_id"]}},
        ],
    })
    result = node.execute(ctx)
    assert result.count("*").fetchone()[0] == 5  # filter skipped → all 5 kept


def test_execute_no_input_raises():
    conn = duckdb.connect(":memory:")
    ctx = ExecutionContext(conn=conn)
    node = DataWranglerNode(params={"steps": []})
    with pytest.raises(ValueError, match="no input"):
        node.execute(ctx)


def test_execute_keeps_input_table_registered_for_lazy_materialization(ctx):
    """The wrangler intentionally leaves __wrangler_input registered after
    execute() returns. ctx.conn.sql() returns a lazy DuckDBPyRelation that
    resolves its source table only when materialized (fetchall/count), so
    unregistering in a finally-block would break the returned relation.
    Subsequent DataWranglerNode invocations re-bind the name to their own
    source — the leak is bounded to one entry."""
    _orders_sample(ctx)
    node = DataWranglerNode(params={"_input_step_ids": ["upstream_1"], "steps": []})
    node.execute(ctx)
    # Still queryable — the registration survives execute() on purpose.
    rows = ctx.conn.sql("SELECT * FROM __wrangler_input").fetchall()
    assert len(rows) == 5


# ─────────────────────────────────────────────────────────────────────────────
# Preview tests
# ─────────────────────────────────────────────────────────────────────────────

def test_preview_returns_one_entry_per_enabled_step(ctx):
    src = _orders_sample(ctx)
    preview = DataWranglerNode.preview_steps(
        ctx.conn,
        src,
        steps=[
            {"op": "rename", "enabled": True,
             "config": {"rename_map": {"cust_id": "customer_id"}}},
            {"op": "filter", "enabled": False,
             "config": {"mode": "expression", "expression": "1=0"}},  # skipped
            {"op": "select", "enabled": True, "config": {"columns": ["customer_id", "amount"]}},
        ],
        sample_rows=50,
    )
    # one input entry + two enabled steps = 3 entries
    assert len(preview["steps"]) == 3
    assert preview["steps"][0]["label"] == "input"
    assert preview["steps"][1]["op"] == "rename"
    assert preview["steps"][2]["op"] == "select"


def test_preview_schema_delta_reports_renames_as_add_remove(ctx):
    src = _orders_sample(ctx)
    preview = DataWranglerNode.preview_steps(
        ctx.conn,
        src,
        steps=[{"op": "rename", "enabled": True,
                "config": {"rename_map": {"cust_id": "customer_id"}}}],
    )
    delta = preview["steps"][1]["schema_delta"]
    assert any(c["name"] == "customer_id" for c in delta["added"])
    assert "cust_id" in delta["removed"]


def test_preview_generated_sql_uses_real_input_table(ctx):
    src = _orders_sample(ctx)
    preview = DataWranglerNode.preview_steps(
        ctx.conn,
        src,
        steps=[{"op": "select", "enabled": True, "config": {"columns": ["cust_id"]}}],
    )
    # Generated SQL references the runtime input table, not the sample table
    assert "__wrangler_input" in preview["generated_sql"]
    assert "__wrangler_sample" not in preview["generated_sql"]


def test_preview_caps_sample_size(ctx):
    # Make a much larger source than sample_rows
    rows_sql = " UNION ALL ".join(f"SELECT {i} AS n" for i in range(0, 500))
    rel = ctx.conn.sql(rows_sql)
    preview = DataWranglerNode.preview_steps(
        ctx.conn,
        rel,
        steps=[{"op": "select", "enabled": True, "config": {"columns": ["n"]}}],
        sample_rows=25,
    )
    assert preview["steps"][0]["row_count"] == 25


def test_preview_returns_sample_rows_per_step(ctx):
    src = _orders_sample(ctx)
    preview = DataWranglerNode.preview_steps(
        ctx.conn,
        src,
        steps=[{"op": "select", "enabled": True, "config": {"columns": ["cust_id"]}}],
    )
    # Input entry has sample_data; so does the enabled select step.
    assert "sample_data" in preview["steps"][0]
    assert "sample_data" in preview["steps"][1]
    assert len(preview["steps"][0]["sample_data"]) > 0
    assert "cust_id" in preview["steps"][1]["sample_data"][0]


# ─────────────────────────────────────────────────────────────────────────────
# Registry sanity
# ─────────────────────────────────────────────────────────────────────────────

def test_data_wrangler_registered_in_node_registry():
    """Smoke test: get_registry() must include the DATA_WRANGLER step type."""
    from fpulse.ir.schema import StepType
    from fpulse.nodes.registry import get_registry

    reg = get_registry()
    types = {t["type"] for t in reg.all_types()}
    assert StepType.DATA_WRANGLER.value in types

    entry = next(t for t in reg.all_types() if t["type"] == StepType.DATA_WRANGLER.value)
    assert entry["label"] == "Data Wrangler"
    assert entry["category"] == "transform"
    assert "steps" in entry["default_params"]


# ─────────────────────────────────────────────────────────────────────────────
# B3 (2026-06-15) — cleaning sub-steps: fill_nulls / replace_values / split_column
# ─────────────────────────────────────────────────────────────────────────────

def _run_recipe(ctx, steps):
    """Compile a recipe over a tiny dirty table and return the result relation."""
    ctx.conn.execute(
        "CREATE OR REPLACE TEMP TABLE __wt AS SELECT * FROM (VALUES "
        "('a', 'N/A', 'x-1'), (NULL, 'keep', 'y-2')) AS t(name, status, code)"
    )
    return ctx.conn.sql(compile_wrangle(steps, "__wt", conn=ctx.conn))


def test_new_ops_registered():
    assert {"fill_nulls", "replace_values", "split_column"} <= set(list_step_ops())


def test_fill_nulls(ctx):
    rel = _run_recipe(ctx, [{"op": "fill_nulls", "enabled": True,
                             "config": {"fills": [{"column": "name", "value": "unknown"}]}}])
    names = sorted(r[rel.columns.index("name")] for r in rel.fetchall())
    assert names == ["a", "unknown"]


def test_replace_values_to_null(ctx):
    rel = _run_recipe(ctx, [{"op": "replace_values", "enabled": True,
                             "config": {"replacements": [{"column": "status", "find": "N/A", "replace": "NULL"}]}}])
    statuses = [r[rel.columns.index("status")] for r in rel.fetchall()]
    assert None in statuses and "keep" in statuses


def test_split_column(ctx):
    rel = _run_recipe(ctx, [{"op": "split_column", "enabled": True,
                             "config": {"column": "code", "delimiter": "-", "into": ["letter", "num"]}}])
    cols = rel.columns
    assert "letter" in cols and "num" in cols
    rows = {r[cols.index("code")]: r for r in rel.fetchall()}
    assert rows["x-1"][cols.index("letter")] == "x"
    assert rows["x-1"][cols.index("num")] == "1"


# ── Debuggability (2026-06-15): pin which sub-step failed ────────────────

def test_execute_pins_failing_substep(ctx):
    _make_upstream(ctx, "SELECT 1 AS a, 2 AS b")
    node = DataWranglerNode(params={
        "_input_step_ids": ["upstream_1"],
        "steps": [
            {"op": "derive", "enabled": True, "config": {"derived": [{"name": "c", "expression": "a + b"}]}},
            {"op": "derive", "enabled": True, "config": {"derived": [{"name": "d", "expression": "no_such_col + 1"}]}},
        ],
    })
    with pytest.raises(ValueError, match="sub-step 2"):
        node.execute(ctx)


def test_preview_marks_failing_step_and_stops(ctx):
    src = _make_upstream(ctx, "SELECT 1 AS a, 2 AS b")
    preview = DataWranglerNode.preview_steps(ctx.conn, src, steps=[
        {"op": "derive", "enabled": True, "config": {"derived": [{"name": "c", "expression": "a + b"}]}},
        {"op": "derive", "enabled": True, "config": {"derived": [{"name": "d", "expression": "no_such_col + 1"}]}},
        {"op": "select", "enabled": True, "config": {"columns": ["a"]}},  # must NOT run
    ])
    steps = preview["steps"]
    statuses = [s.get("status") for s in steps if s.get("index", -1) >= 0]
    assert statuses == ["ok", "error"]          # step3 never ran
    err = next(s for s in steps if s.get("status") == "error")
    assert err["index"] == 1 and err["error"]
