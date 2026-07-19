"""Tests for fpulse.ai.schema_infer (Phase 3.3, May 18 2026).

Covers type detection, lattice promotion, nullability, string sizing,
int→bigint promotion, nested-object flattening, DDL rendering across
3 dialects, and the empty / malformed edge cases.
"""

from __future__ import annotations

from fpulse.ai.schema_infer import (
    Column,
    InferredSchema,
    infer_schema,
    render_ddl,
)


def _col(schema: InferredSchema, name: str) -> Column:
    """Helper — fetch the column by name, fail loudly if missing."""
    for c in schema.columns:
        if c.name == name:
            return c
    raise AssertionError(f"column {name!r} not in schema (have: {[c.name for c in schema.columns]})")


# ── Empty + edge cases ────────────────────────────────────────────────────


def test_infer_empty_samples_returns_empty_schema():
    s = infer_schema([])
    assert s.columns == []
    assert s.warnings  # records the empty-set warning


def test_infer_skips_non_dict_rows():
    s = infer_schema([{"a": 1}, "not a dict", {"a": 2}])
    assert s.row_count == 2  # only 2 dicts counted
    assert any("non-dict" in w for w in s.warnings)


def test_infer_handles_all_null_column():
    s = infer_schema([{"x": None}, {"x": None}])
    col = _col(s, "x")
    assert col.nullable is True
    # All-null falls back to NVARCHAR(255) — safest
    assert "NVARCHAR" in col.sql_type_mssql
    assert any("always null" in w for w in s.warnings)


# ── Type detection ────────────────────────────────────────────────────────


def test_int_column_promotes_to_bigint_when_large():
    s = infer_schema([{"v": 1}, {"v": 5_000_000_000}])  # > int32 max
    col = _col(s, "v")
    assert col.sql_type_mssql == "BIGINT"
    assert col.sql_type_postgres == "BIGINT"


def test_int_column_stays_int_when_small():
    s = infer_schema([{"v": 1}, {"v": 1000}])
    col = _col(s, "v")
    assert col.sql_type_mssql == "INT"
    assert col.sql_type_postgres == "INTEGER"


def test_float_column():
    s = infer_schema([{"price": 1.5}, {"price": 2.99}])
    col = _col(s, "price")
    assert col.json_type == "float"
    assert col.sql_type_mssql == "FLOAT"
    assert col.sql_type_postgres == "DOUBLE PRECISION"


def test_bool_column():
    s = infer_schema([{"active": True}, {"active": False}])
    col = _col(s, "active")
    assert col.json_type == "bool"
    assert col.sql_type_mssql == "BIT"
    assert col.sql_type_postgres == "BOOLEAN"


def test_bool_not_confused_with_int():
    """Python's bool is a subclass of int — must detect bool FIRST."""
    s = infer_schema([{"x": True}])
    col = _col(s, "x")
    assert col.json_type == "bool"  # not "int"


def test_date_vs_datetime_detection():
    s = infer_schema([{"d": "2026-05-18"}, {"d": "2026-05-19"}])
    col = _col(s, "d")
    assert col.json_type == "date"
    assert col.sql_type_mssql == "DATE"


def test_iso_datetime_detected():
    s = infer_schema([{"ts": "2026-05-18T10:00:00Z"}])
    col = _col(s, "ts")
    assert col.json_type == "datetime"
    assert col.sql_type_mssql == "DATETIME2"


def test_string_sized_by_max_length():
    s = infer_schema([{"name": "a"}, {"name": "x" * 100}])
    col = _col(s, "name")
    assert col.max_length == 100
    # 100 → next power of 2 = 128
    assert "NVARCHAR(128)" in col.sql_type_mssql
    assert "VARCHAR(128)" in col.sql_type_postgres


def test_string_over_4000_promotes_to_max():
    s = infer_schema([{"big": "x" * 5000}])
    col = _col(s, "big")
    assert "NVARCHAR(MAX)" in col.sql_type_mssql
    assert "TEXT" in col.sql_type_postgres


# ── Nullability ───────────────────────────────────────────────────────────


def test_column_missing_in_some_rows_marked_nullable():
    s = infer_schema([{"a": 1, "b": 2}, {"a": 1}])  # b missing in 2nd row
    assert _col(s, "b").nullable is True
    # a is present everywhere → NOT NULL
    assert _col(s, "a").nullable is False


def test_explicit_null_marks_column_nullable():
    s = infer_schema([{"x": 1}, {"x": None}])
    assert _col(s, "x").nullable is True


# ── Type promotion (lattice merge) ────────────────────────────────────────


def test_int_plus_float_promotes_to_float():
    s = infer_schema([{"v": 1}, {"v": 1.5}])
    col = _col(s, "v")
    assert col.json_type == "float"


def test_int_plus_string_promotes_to_string():
    s = infer_schema([{"v": 1}, {"v": "abc"}])
    col = _col(s, "v")
    assert col.json_type == "str"


def test_date_plus_datetime_promotes_to_datetime():
    s = infer_schema([{"d": "2026-05-18"}, {"d": "2026-05-19T10:00:00Z"}])
    col = _col(s, "d")
    assert col.json_type == "datetime"


def test_truly_incompatible_promotes_to_mixed():
    s = infer_schema([{"x": {"nested": 1}}, {"x": [1, 2, 3]}])
    col = _col(s, "x")
    # When flattening, the object case becomes nested cols; the array
    # makes the column "array". Mix of array + object types → mixed.
    # OR — under flattening, the object gets flattened away. Test
    # the no-flatten case explicitly:
    s = infer_schema(
        [{"x": {"nested": 1}}, {"x": [1, 2, 3]}],
        flatten_objects=False,
    )
    col = _col(s, "x")
    assert col.json_type == "mixed"


# ── Nested objects (flattening) ───────────────────────────────────────────


def test_nested_object_flattened_with_separator():
    s = infer_schema([{"id": 1, "addr": {"city": "Bangalore", "pin": "560001"}}])
    names = {c.name for c in s.columns}
    assert "id" in names
    assert "addr_city" in names
    assert "addr_pin" in names


def test_nested_object_not_flattened_when_disabled():
    s = infer_schema(
        [{"id": 1, "addr": {"city": "Bangalore"}}],
        flatten_objects=False,
    )
    names = {c.name for c in s.columns}
    assert "addr" in names
    assert "addr_city" not in names


# ── DDL rendering ─────────────────────────────────────────────────────────


def test_render_ddl_mssql_uses_square_brackets():
    s = infer_schema([{"id": 1, "name": "foo"}], table_name="users")
    ddl = render_ddl(s, dialect="mssql")
    assert "CREATE TABLE [users] (" in ddl
    assert "[id] INT" in ddl
    assert "[name] NVARCHAR" in ddl


def test_render_ddl_postgres_uses_double_quotes():
    s = infer_schema([{"id": 1, "name": "foo"}], table_name="users")
    ddl = render_ddl(s, dialect="postgres")
    assert 'CREATE TABLE "users" (' in ddl
    assert '"id" INTEGER' in ddl
    assert '"name" VARCHAR' in ddl


def test_render_ddl_duckdb():
    s = infer_schema([{"id": 1, "v": 1.5}], table_name="t")
    ddl = render_ddl(s, dialect="duckdb")
    assert 'CREATE TABLE "t" (' in ddl
    assert '"id" INTEGER' in ddl
    assert '"v" DOUBLE' in ddl


def test_render_ddl_nullability_in_output():
    s = infer_schema([{"a": 1, "b": 2}, {"a": 1}])  # b nullable
    ddl = render_ddl(s, dialect="mssql")
    assert "[a] INT NOT NULL" in ddl
    assert "[b] INT NULL" in ddl


def test_render_ddl_warnings_inlined_as_comments():
    """Schema warnings should render as SQL comments above the DDL —
    operator-friendly, never lost to /dev/null."""
    s = infer_schema([{"x": None}])  # always-null warning
    ddl = render_ddl(s, dialect="mssql")
    assert ddl.lstrip().startswith("-- Schema inference notes:")
    assert "always null" in ddl


def test_render_ddl_empty_schema():
    s = infer_schema([])
    ddl = render_ddl(s, dialect="mssql")
    assert "-- (empty schema" in ddl


# ── Sample values kept but NOT rendered ───────────────────────────────────


def test_sample_values_kept_internally_not_in_ddl():
    """The trust contract: sample values shouldn't leak into the DDL
    response (could contain PII). They're stored on Column.sample_values
    for downstream callers that want them."""
    s = infer_schema([{"email": "alice@example.com"}, {"email": "bob@example.com"}])
    col = _col(s, "email")
    # Samples are kept
    assert len(col.sample_values) > 0
    assert "alice@example.com" in col.sample_values
    # But they're NOT in the rendered DDL
    ddl = render_ddl(s, dialect="mssql")
    assert "alice@example.com" not in ddl
    assert "bob@example.com" not in ddl


def test_sample_values_deduped_and_capped():
    """At most 5 distinct sample values per column."""
    rows = [{"v": i % 3} for i in range(20)]  # only 3 distinct values
    s = infer_schema(rows)
    col = _col(s, "v")
    assert len(col.sample_values) <= 5
    assert set(col.sample_values) == {0, 1, 2}
