"""Tests for the readable-SQL preview compiler (fpulse.compile.sql_preview).

Proves the SQL-native chain compiles faithfully, and — critically — that nodes
which are NOT SQL-expressible become explicit boundaries rather than fabricated
SQL (the honesty contract of a "preview").
"""

from __future__ import annotations

from fpulse.compile.sql_preview import compile_workflow_to_sql


def _wf(steps, conns, name="t"):
    return {"name": name, "steps": steps, "connections": conns}


def test_linear_source_filter_aggregate_compiles_faithfully():
    wf = _wf(
        steps=[
            {"id": "src", "type": "db_source",
             "params": {"query": "SELECT id, region, amount FROM sales"}},
            {"id": "flt", "type": "filter",
             "params": {"mode": "expression", "condition": "amount > 100"}},
            {"id": "agg", "type": "aggregate",
             "params": {"group_by": ["region"],
                        "functions": [{"column": "amount", "function": "SUM", "alias": "total"}]}},
            {"id": "out", "type": "destination", "params": {}},
        ],
        conns=[
            {"from_step": "src", "to_step": "flt"},
            {"from_step": "flt", "to_step": "agg"},
            {"from_step": "agg", "to_step": "out"},
        ],
    )
    r = compile_workflow_to_sql(wf)
    assert r.fully_compiled, r.boundary_steps
    s = r.sql
    assert "WITH" in s
    assert "SELECT id, region, amount FROM sales" in s
    assert "WHERE amount > 100" in s
    assert 'SUM("amount") AS "total"' in s
    assert 'GROUP BY "region"' in s
    assert s.rstrip().endswith(";")
    assert len(r.compiled_steps) == 4  # src, flt, agg, + sink


def test_api_source_becomes_a_boundary_not_fake_sql():
    wf = _wf(
        steps=[
            {"id": "api", "type": "api_source", "params": {}},
            {"id": "flt", "type": "filter",
             "params": {"mode": "expression", "condition": "x > 1"}},
        ],
        conns=[{"from_step": "api", "to_step": "flt"}],
    )
    r = compile_workflow_to_sql(wf)
    assert not r.fully_compiled
    boundary_ids = {b["id"] for b in r.boundary_steps}
    assert "api" in boundary_ids   # non-SQL source
    assert "flt" in boundary_ids   # downstream inherits the boundary
    assert "[boundary]" in r.sql


def test_rules_mode_filter_is_a_boundary_but_source_still_compiles():
    wf = _wf(
        steps=[
            {"id": "src", "type": "db_source", "params": {"query": "SELECT * FROM t"}},
            {"id": "flt", "type": "filter",
             "params": {"mode": "rules", "rules": [{"col": "a", "op": ">", "val": 1}]}},
        ],
        conns=[{"from_step": "src", "to_step": "flt"}],
    )
    r = compile_workflow_to_sql(wf)
    assert {"flt"} <= {b["id"] for b in r.boundary_steps}
    assert "SELECT * FROM t" in r.sql


def test_join_with_keys_compiles_faithfully():
    wf = _wf(
        steps=[
            {"id": "a", "type": "db_source", "params": {"query": "SELECT * FROM a"}},
            {"id": "b", "type": "db_source", "params": {"query": "SELECT * FROM b"}},
            {"id": "j", "type": "join",
             "params": {"join_type": "LEFT", "key_mode": "same_key", "join_key": ["id"]}},
        ],
        conns=[{"from_step": "a", "to_step": "j"}, {"from_step": "b", "to_step": "j"}],
    )
    r = compile_workflow_to_sql(wf)
    assert r.fully_compiled, r.boundary_steps
    assert "LEFT JOIN" in r.sql
    assert 's_a."id" = s_b."id"' in r.sql


def test_join_without_keys_is_a_boundary():
    wf = _wf(
        steps=[
            {"id": "a", "type": "db_source", "params": {"query": "SELECT * FROM a"}},
            {"id": "b", "type": "db_source", "params": {"query": "SELECT * FROM b"}},
            {"id": "j", "type": "join", "params": {}},  # no join_key -> can't build ON
        ],
        conns=[{"from_step": "a", "to_step": "j"}, {"from_step": "b", "to_step": "j"}],
    )
    r = compile_workflow_to_sql(wf)
    assert "j" in {b["id"] for b in r.boundary_steps}


def test_deduplicate_compiles_to_qualify_row_number():
    wf = _wf(
        steps=[
            {"id": "src", "type": "db_source", "params": {"query": "SELECT * FROM u"}},
            {"id": "dd", "type": "deduplicate",
             "params": {"key": ["email"], "strategy": "keep_last", "order_by": "created_at DESC"}},
        ],
        conns=[{"from_step": "src", "to_step": "dd"}],
    )
    r = compile_workflow_to_sql(wf)
    assert r.fully_compiled, r.boundary_steps
    assert 'QUALIFY ROW_NUMBER() OVER (PARTITION BY "email"' in r.sql
    assert 'ORDER BY "created_at" ASC' in r.sql  # keep_last reverses DESC


def test_rename_and_typecast_compile():
    wf = _wf(
        steps=[
            {"id": "src", "type": "db_source", "params": {"query": "SELECT * FROM t"}},
            {"id": "rn", "type": "rename", "params": {"mappings": {"old": "new", "a": "b"}}},
            {"id": "tc", "type": "typecast", "params": {"casts": {"amt": "DOUBLE"}}},
        ],
        conns=[{"from_step": "src", "to_step": "rn"}, {"from_step": "rn", "to_step": "tc"}],
    )
    r = compile_workflow_to_sql(wf)
    assert r.fully_compiled, r.boundary_steps
    assert '"old" AS "new"' in r.sql
    assert 'CAST("amt" AS DOUBLE) AS "amt"' in r.sql
    assert "REPLACE" in r.sql


def test_union_compiles_with_mode():
    wf = _wf(
        steps=[
            {"id": "a", "type": "db_source", "params": {"query": "SELECT * FROM a"}},
            {"id": "b", "type": "db_source", "params": {"query": "SELECT * FROM b"}},
            {"id": "u", "type": "union", "params": {"mode": "by_name"}},
        ],
        conns=[{"from_step": "a", "to_step": "u"}, {"from_step": "b", "to_step": "u"}],
    )
    r = compile_workflow_to_sql(wf)
    assert r.fully_compiled, r.boundary_steps
    assert "UNION ALL BY NAME" in r.sql
    assert "SELECT * FROM s_a" in r.sql and "SELECT * FROM s_b" in r.sql


def test_window_compiles_multiple_functions():
    wf = _wf(
        steps=[
            {"id": "src", "type": "db_source", "params": {"query": "SELECT * FROM t"}},
            {"id": "w", "type": "window",
             "params": {"partition_by": ["region"], "order_by": ["amount DESC"],
                        "window_functions": [
                            {"function": "RANK", "alias": "rnk"},
                            {"function": "SUM", "column": "amount", "alias": "running"},
                        ]}},
        ],
        conns=[{"from_step": "src", "to_step": "w"}],
    )
    r = compile_workflow_to_sql(wf)
    assert r.fully_compiled, r.boundary_steps
    assert 'RANK() OVER (PARTITION BY "region" ORDER BY "amount" DESC) AS "rnk"' in r.sql
    assert 'SUM("amount") OVER (PARTITION BY "region" ORDER BY "amount" DESC) AS "running"' in r.sql


def test_derived_column_row_local_and_windowed():
    wf = _wf(
        steps=[
            {"id": "src", "type": "db_source",
             "params": {"query": "SELECT qty, price, amount, cust, ts FROM t"}},
            {"id": "dc", "type": "derived_column", "params": {"columns": [
                {"name": "total", "expression": "qty * price"},
                {"name": "running", "expression": "SUM(amount)",
                 "window": {"partition_by": ["cust"], "order_by": ["ts"]}},
            ]}},
        ],
        conns=[{"from_step": "src", "to_step": "dc"}],
    )
    r = compile_workflow_to_sql(wf)
    assert r.fully_compiled, r.boundary_steps
    assert 'qty * price AS "total"' in r.sql
    assert 'SUM(amount) OVER (PARTITION BY "cust" ORDER BY "ts") AS "running"' in r.sql


def test_data_wrangler_reuses_its_own_compile_wrangle():
    """The Data Wrangler node compiles by REUSING compile_wrangle — its select/
    rename sub-ops appear in the preview (no duplicated logic here)."""
    wf = _wf(
        steps=[
            {"id": "src", "type": "db_source", "params": {"query": "SELECT a, b, c FROM t"}},
            {"id": "dw", "type": "data_wrangler", "params": {"steps": [
                {"op": "select", "config": {"columns": ["a", "b"]}},
                {"op": "rename", "config": {"rename_map": {"a": "x"}}},
            ]}},
        ],
        conns=[{"from_step": "src", "to_step": "dw"}],
    )
    r = compile_workflow_to_sql(wf)
    assert r.fully_compiled, r.boundary_steps
    assert "s_src" in r.sql        # recipe compiled against the upstream CTE
    assert "RENAME" in r.sql       # the rename sub-op's SQL (from compile_wrangle)


def test_transform_rebinds_source_table_to_the_upstream_cte():
    wf = _wf(
        steps=[
            {"id": "src", "type": "db_source", "params": {"query": "SELECT id, x FROM t"}},
            {"id": "tr", "type": "transform",
             "params": {"expression": "SELECT id, x * 2 AS dbl FROM source_table WHERE x > 0"}},
        ],
        conns=[{"from_step": "src", "to_step": "tr"}],
    )
    r = compile_workflow_to_sql(wf)
    assert r.fully_compiled, r.boundary_steps
    assert "FROM s_src WHERE x > 0" in r.sql  # source_table rebound to the CTE


def test_pivot_and_unpivot_emit_duckdb_native_syntax():
    r = compile_workflow_to_sql(_wf(
        steps=[
            {"id": "src", "type": "db_source", "params": {"query": "SELECT region, month, amt FROM s"}},
            {"id": "pv", "type": "pivot",
             "params": {"pivot_column": "month", "value_column": "amt",
                        "agg_function": "SUM", "group_by": ["region"]}},
        ],
        conns=[{"from_step": "src", "to_step": "pv"}],
    ))
    assert 'PIVOT s_src ON "month" USING SUM("amt") GROUP BY "region"' in r.sql

    r2 = compile_workflow_to_sql(_wf(
        steps=[
            {"id": "src", "type": "db_source", "params": {"query": "SELECT id, jan, feb FROM s"}},
            {"id": "up", "type": "unpivot",
             "params": {"columns": ["jan", "feb"], "id_columns": ["id"],
                        "name_column": "month", "value_column": "amt"}},
        ],
        conns=[{"from_step": "src", "to_step": "up"}],
    ))
    assert 'UNPIVOT ("amt" FOR "month" IN ("jan", "feb"))' in r2.sql


def test_compiled_sql_actually_runs_in_duckdb():
    """The strongest test: source (inline VALUES) -> filter -> pivot compiles to
    SQL that DuckDB actually executes and returns the expected rows."""
    import duckdb

    wf = _wf(
        steps=[
            {"id": "src", "type": "db_source", "params": {
                "query": "SELECT * FROM (VALUES ('east','jan',10),('east','feb',20),('west','jan',5)) "
                         "AS t(region, month, amt)"}},
            {"id": "flt", "type": "filter", "params": {"mode": "expression", "condition": "amt >= 5"}},
            {"id": "pv", "type": "pivot", "params": {
                "pivot_column": "month", "value_column": "amt", "agg_function": "SUM",
                "group_by": ["region"]}},
        ],
        conns=[{"from_step": "src", "to_step": "flt"}, {"from_step": "flt", "to_step": "pv"}],
    )
    r = compile_workflow_to_sql(wf)
    assert r.fully_compiled, r.boundary_steps

    con = duckdb.connect()
    try:
        rows = con.execute(r.sql).fetchall()
    finally:
        con.close()
    assert len(rows) == 2  # east + west, pivoted by month


def test_empty_pipeline_is_handled():
    r = compile_workflow_to_sql({"name": "empty", "steps": [], "connections": []})
    assert r.compiled_steps == []
    assert "no SQL-compilable" in r.sql
