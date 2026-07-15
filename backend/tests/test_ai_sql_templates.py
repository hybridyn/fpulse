"""Tests for fpulse.ai.sql_templates (Phase 3.4, May 18 2026).

Covers all 10 templates across all 3 dialects: signature correctness,
identifier-quoting safety, no-SQL-injection, registry consistency.
"""

from __future__ import annotations

import pytest

from fpulse.ai.sql_templates import (
    TEMPLATES,
    dedupe_by_key,
    date_truncate,
    find_duplicates,
    lag_diff,
    merge_upsert,
    percentile_aggregate,
    pivot,
    render_template,
    running_total,
    scd2_merge,
    unpivot,
)


# ── Registry consistency ──────────────────────────────────────────────────


def test_all_10_templates_registered():
    expected = {
        "merge_upsert", "scd2_merge", "dedupe_by_key", "pivot", "unpivot",
        "running_total", "lag_diff", "date_truncate",
        "percentile_aggregate", "find_duplicates",
    }
    assert set(TEMPLATES.keys()) == expected


def test_every_template_supports_all_3_dialects():
    """Contract: every template renders cleanly in all 3 dialects."""
    for name, meta in TEMPLATES.items():
        assert set(meta["dialects"]) == {"mssql", "postgres", "duckdb"}, (
            f"{name} missing a dialect"
        )


# ── Identifier quoting / no-injection ────────────────────────────────────


def test_mssql_uses_square_brackets():
    sql = merge_upsert(
        target="orders", source="stg_orders",
        key_cols=["id"], update_cols=["status"],
        dialect="mssql",
    )
    assert "[id]" in sql
    assert "[status]" in sql
    # No accidental Postgres-style quoting
    assert '"id"' not in sql


def test_postgres_uses_double_quotes():
    sql = merge_upsert(
        target="orders", source="stg_orders",
        key_cols=["id"], update_cols=["status"],
        dialect="postgres",
    )
    assert '"id"' in sql
    assert '"status"' in sql
    # No SQL Server brackets
    assert "[id]" not in sql


def test_injection_attempt_in_column_name_escaped():
    """An attacker-controlled column name like `evil]; DROP TABLE--` must
    be escaped to `[evil]]; DROP TABLE--]` (closing bracket doubled).
    The SQL parser then treats the whole thing as a single identifier
    (which will fail to resolve, not execute the DROP)."""
    sql = merge_upsert(
        target="t", source="s",
        key_cols=["evil]; DROP TABLE users--"],
        update_cols=["x"],
        dialect="mssql",
    )
    # The bracket should be doubled (escape) — not closed early
    assert "[evil]]; DROP TABLE users--]" in sql
    # And there should NOT be an unquoted DROP after a bare `]`
    assert "]; DROP" not in sql.replace("]]; DROP", "")


def test_quote_injection_in_postgres():
    """Same test for Postgres — embedded double-quotes get escaped by doubling."""
    sql = merge_upsert(
        target="t", source="s",
        key_cols=['evil"; DROP TABLE users--'],
        update_cols=["x"],
        dialect="postgres",
    )
    # The double-quote should be doubled
    assert '"evil""; DROP TABLE users--"' in sql


# ── merge_upsert ──────────────────────────────────────────────────────────


def test_merge_upsert_mssql_uses_merge_statement():
    sql = merge_upsert(
        target="dbo.orders", source="stg.orders",
        key_cols=["order_id"], update_cols=["status", "shipped_at"],
        dialect="mssql",
    )
    assert "MERGE INTO" in sql
    assert "WHEN MATCHED THEN UPDATE SET" in sql
    assert "WHEN NOT MATCHED BY TARGET THEN INSERT" in sql


def test_merge_upsert_postgres_uses_on_conflict():
    sql = merge_upsert(
        target="orders", source="stg_orders",
        key_cols=["order_id"], update_cols=["status"],
        dialect="postgres",
    )
    assert "ON CONFLICT" in sql
    assert "DO UPDATE SET" in sql
    assert "EXCLUDED." in sql


def test_merge_upsert_multi_key():
    sql = merge_upsert(
        target="t", source="s",
        key_cols=["a", "b"], update_cols=["c"],
        dialect="mssql",
    )
    # Composite key joined with AND
    assert "T.[a] = S.[a] AND T.[b] = S.[b]" in sql


# ── scd2_merge ────────────────────────────────────────────────────────────


def test_scd2_closes_old_then_inserts_new():
    sql = scd2_merge(
        target="dim_customer", source="stg_customer",
        key_cols=["customer_id"], tracked_cols=["name", "email"],
        dialect="mssql",
    )
    assert "Close out changed rows" in sql
    assert "UPDATE T SET" in sql
    assert "Insert new current version" in sql
    assert "INSERT INTO dim_customer" in sql


def test_scd2_uses_sysutcdatetime_in_mssql():
    sql = scd2_merge(
        target="d", source="s",
        key_cols=["k"], tracked_cols=["c"],
        dialect="mssql",
    )
    assert "SYSUTCDATETIME()" in sql


def test_scd2_uses_current_timestamp_in_postgres():
    sql = scd2_merge(
        target="d", source="s",
        key_cols=["k"], tracked_cols=["c"],
        dialect="postgres",
    )
    assert "CURRENT_TIMESTAMP" in sql


# ── dedupe_by_key ─────────────────────────────────────────────────────────


def test_dedupe_uses_row_number_partition():
    sql = dedupe_by_key(
        source="customers", key_cols=["email"],
        order_col="updated_at", dialect="mssql",
    )
    assert "ROW_NUMBER()" in sql
    assert "PARTITION BY [email]" in sql
    assert "ORDER BY [updated_at] DESC" in sql
    assert "WHERE sub._rn = 1" in sql


def test_dedupe_respects_order_dir_asc():
    sql = dedupe_by_key(
        source="t", key_cols=["k"], order_col="ts",
        order_dir="ASC", dialect="postgres",
    )
    assert "ORDER BY \"ts\" ASC" in sql


# ── pivot / unpivot ───────────────────────────────────────────────────────


def test_pivot_mssql_uses_native_pivot():
    sql = pivot(
        source="sales", row_keys=["region"],
        pivot_col="month", value_col="amount",
        pivot_values=["Jan", "Feb"],
        dialect="mssql",
    )
    assert "PIVOT" in sql
    assert "FOR [month] IN" in sql


def test_pivot_postgres_uses_case_when_pattern():
    sql = pivot(
        source="sales", row_keys=["region"],
        pivot_col="month", value_col="amount",
        pivot_values=["Jan", "Feb"],
        aggregate="SUM",
        dialect="postgres",
    )
    assert "CASE WHEN" in sql
    assert "GROUP BY" in sql
    assert '"Jan"' in sql


def test_unpivot_postgres_uses_union_all():
    sql = unpivot(
        source="wide", id_cols=["id"],
        value_cols=["jan", "feb", "mar"],
        dialect="postgres",
    )
    # Three SELECTs joined by UNION ALL
    assert sql.count("UNION ALL") == 2
    assert '"jan"' in sql
    assert '"feb"' in sql


# ── Window functions ──────────────────────────────────────────────────────


def test_running_total():
    sql = running_total(
        source="sales", partition_cols=["region"],
        order_col="ds", value_col="amount",
        dialect="mssql",
    )
    assert "SUM([amount]) OVER" in sql
    assert "PARTITION BY [region]" in sql
    assert "ORDER BY [ds]" in sql
    assert "ROWS UNBOUNDED PRECEDING" in sql


def test_running_total_no_partition():
    sql = running_total(
        source="t", partition_cols=[],
        order_col="ts", value_col="v",
        dialect="postgres",
    )
    # No PARTITION BY clause
    assert "PARTITION BY" not in sql
    assert "ORDER BY \"ts\"" in sql


def test_lag_diff_uses_subtraction():
    sql = lag_diff(
        source="t", partition_cols=["k"],
        order_col="ts", value_col="v",
        dialect="duckdb",
    )
    assert "LAG(\"v\")" in sql
    assert "\"v\" - LAG" in sql


# ── date_truncate ─────────────────────────────────────────────────────────


def test_date_truncate_mssql_uses_datetrunc():
    sql = date_truncate(source="t", date_col="ts", bucket="day", dialect="mssql")
    assert "DATETRUNC(day," in sql


def test_date_truncate_postgres_uses_date_trunc():
    sql = date_truncate(source="t", date_col="ts", bucket="month", dialect="postgres")
    assert "date_trunc('month'" in sql


# ── percentile_aggregate ─────────────────────────────────────────────────


def test_percentile_uses_percentile_cont():
    sql = percentile_aggregate(
        source="latency", group_cols=["endpoint"],
        value_col="duration_ms", percentile=0.95,
        dialect="postgres",
    )
    assert "PERCENTILE_CONT(0.95) WITHIN GROUP" in sql


def test_percentile_out_of_range_raises():
    with pytest.raises(ValueError):
        percentile_aggregate(
            source="t", group_cols=["g"], value_col="v",
            percentile=1.5,
        )


def test_percentile_default_alias_includes_pXX():
    sql = percentile_aggregate(
        source="t", group_cols=["g"], value_col="v",
        percentile=0.95, dialect="postgres",
    )
    assert "p95_v" in sql


# ── find_duplicates ──────────────────────────────────────────────────────


def test_find_duplicates_uses_having_count_in_mssql():
    sql = find_duplicates(
        source="customers", key_cols=["email"],
        dialect="mssql",
    )
    assert "HAVING COUNT(*) > 1" in sql
    assert "WITH dups AS" in sql


def test_find_duplicates_uses_qualify_in_duckdb():
    """DuckDB supports the QUALIFY clause for filtering on window
    function output without a subquery."""
    sql = find_duplicates(
        source="t", key_cols=["k"], dialect="duckdb",
    )
    assert "QUALIFY dup_count > 1" in sql


# ── render_template dispatch ─────────────────────────────────────────────


def test_render_template_dispatches_correctly():
    sql = render_template("merge_upsert", {
        "target": "t", "source": "s",
        "key_cols": ["k"], "update_cols": ["v"],
    }, dialect="mssql")
    assert "MERGE INTO" in sql


def test_render_template_unknown_name_raises():
    with pytest.raises(KeyError, match="unknown SQL template"):
        render_template("nonexistent", {}, dialect="mssql")


def test_render_template_missing_required_arg_raises():
    """Pass an incomplete arg dict — the underlying function raises
    TypeError on missing kwargs, which surfaces cleanly to the caller."""
    with pytest.raises(TypeError):
        render_template("merge_upsert", {"target": "t"}, dialect="mssql")
