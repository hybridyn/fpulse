"""Unit tests for connector-specific schema type normalization.

Covers Roadmap Item 3 (2026-05-27) — cross-dialect canonical type
vocabulary + compatibility predicate. These tests pin down the
behaviour the schema_policy engine depends on, so a change to the
dialect map ripples through here BEFORE it lands in production
drift evaluation.
"""

from __future__ import annotations

import pytest

from fpulse.intelligence.type_normalization import (
    CT_BINARY,
    CT_BOOL,
    CT_DATE,
    CT_DECIMAL,
    CT_FLOAT32,
    CT_FLOAT64,
    CT_INT8,
    CT_INT16,
    CT_INT32,
    CT_INT64,
    CT_JSON,
    CT_STRING,
    CT_TEXT,
    CT_TIMESTAMP,
    CT_TIMESTAMP_TZ,
    CT_UNKNOWN,
    canonicalize_type,
    types_compatible,
)


# ── canonicalize_type: per-dialect happy paths ──────────────────────


class TestCanonicalisePostgres:
    """PostgreSQL maps the lowest-common-denominator types directly."""

    def test_smallint(self):
        assert canonicalize_type("postgresql", "smallint") == CT_INT16

    def test_integer(self):
        assert canonicalize_type("postgresql", "integer") == CT_INT32

    def test_int_alias(self):
        assert canonicalize_type("postgresql", "int") == CT_INT32

    def test_bigint(self):
        assert canonicalize_type("postgresql", "bigint") == CT_INT64

    def test_int8_alias_for_bigint(self):
        assert canonicalize_type("postgresql", "int8") == CT_INT64

    def test_boolean(self):
        assert canonicalize_type("postgresql", "boolean") == CT_BOOL

    def test_real(self):
        assert canonicalize_type("postgresql", "real") == CT_FLOAT32

    def test_double_precision(self):
        assert canonicalize_type("postgresql", "double precision") == CT_FLOAT64

    def test_numeric_with_precision_scale(self):
        assert canonicalize_type("postgresql", "numeric(18,2)") == "decimal(18,2)"

    def test_decimal_alias(self):
        assert canonicalize_type("postgresql", "decimal(10,4)") == "decimal(10,4)"

    def test_varchar_with_length(self):
        assert canonicalize_type("postgresql", "varchar(255)") == "string(255)"

    def test_character_varying(self):
        assert canonicalize_type("postgresql", "character varying(50)") == "string(50)"

    def test_text(self):
        assert canonicalize_type("postgresql", "text") == CT_TEXT

    def test_date(self):
        assert canonicalize_type("postgresql", "date") == CT_DATE

    def test_timestamp_without_tz(self):
        assert canonicalize_type("postgresql", "timestamp") == CT_TIMESTAMP

    def test_timestamp_with_tz(self):
        assert canonicalize_type("postgresql", "timestamptz") == CT_TIMESTAMP_TZ

    def test_timestamp_with_time_zone_phrase(self):
        assert canonicalize_type(
            "postgresql", "timestamp with time zone"
        ) == CT_TIMESTAMP_TZ

    def test_bytea(self):
        assert canonicalize_type("postgresql", "bytea") == CT_BINARY

    def test_json(self):
        assert canonicalize_type("postgresql", "json") == CT_JSON

    def test_jsonb(self):
        assert canonicalize_type("postgresql", "jsonb") == CT_JSON


class TestCanonicaliseMSSQL:
    def test_tinyint(self):
        assert canonicalize_type("mssql", "tinyint") == CT_INT8

    def test_smallint(self):
        assert canonicalize_type("mssql", "smallint") == CT_INT16

    def test_int(self):
        assert canonicalize_type("mssql", "int") == CT_INT32

    def test_bigint(self):
        assert canonicalize_type("mssql", "bigint") == CT_INT64

    def test_bit_is_bool(self):
        assert canonicalize_type("mssql", "bit") == CT_BOOL

    def test_real_is_float32(self):
        assert canonicalize_type("mssql", "real") == CT_FLOAT32

    def test_float_default_is_float64(self):
        assert canonicalize_type("mssql", "float") == CT_FLOAT64

    def test_float_n_24_is_float32(self):
        assert canonicalize_type("mssql", "float(24)") == CT_FLOAT32

    def test_float_n_53_is_float64(self):
        assert canonicalize_type("mssql", "float(53)") == CT_FLOAT64

    def test_decimal_with_precision_scale(self):
        assert canonicalize_type("mssql", "decimal(18,2)") == "decimal(18,2)"

    def test_money_maps_to_decimal(self):
        assert canonicalize_type("mssql", "money") == "decimal(38,9)"

    def test_nvarchar_with_length(self):
        assert canonicalize_type("mssql", "nvarchar(255)") == "string(255)"

    def test_text_unbounded(self):
        assert canonicalize_type("mssql", "text") == CT_TEXT

    def test_date(self):
        assert canonicalize_type("mssql", "date") == CT_DATE

    def test_datetime2(self):
        assert canonicalize_type("mssql", "datetime2") == CT_TIMESTAMP

    def test_datetimeoffset_is_tz(self):
        assert canonicalize_type("mssql", "datetimeoffset") == CT_TIMESTAMP_TZ

    def test_varbinary_with_length(self):
        assert canonicalize_type("mssql", "varbinary(64)") == "binary(64)"

    def test_uniqueidentifier_becomes_string36(self):
        assert canonicalize_type("mssql", "uniqueidentifier") == "string(36)"


class TestCanonicaliseOracle:
    def test_number_no_args_is_decimal_default(self):
        assert canonicalize_type("oracle", "number") == "decimal(38,9)"

    def test_number_with_precision_scale(self):
        assert canonicalize_type("oracle", "number(18,2)") == "decimal(18,2)"

    def test_number_small_precision_becomes_int32(self):
        # NUMBER(9) — fits in int32.
        assert canonicalize_type("oracle", "number(9)") == CT_INT32

    def test_number_precision_18_becomes_int64(self):
        assert canonicalize_type("oracle", "number(18)") == CT_INT64

    def test_number_precision_2_becomes_int8(self):
        assert canonicalize_type("oracle", "number(2)") == CT_INT8

    def test_varchar2_with_length(self):
        assert canonicalize_type("oracle", "varchar2(255)") == "string(255)"

    def test_nvarchar2_with_length(self):
        assert canonicalize_type("oracle", "nvarchar2(100)") == "string(100)"

    def test_clob_is_text(self):
        assert canonicalize_type("oracle", "clob") == CT_TEXT

    def test_date_is_timestamp_not_date(self):
        # Oracle DATE carries a time component — it's a timestamp.
        assert canonicalize_type("oracle", "date") == CT_TIMESTAMP

    def test_timestamp(self):
        assert canonicalize_type("oracle", "timestamp") == CT_TIMESTAMP

    def test_timestamp_with_time_zone(self):
        assert canonicalize_type(
            "oracle", "timestamp with time zone"
        ) == CT_TIMESTAMP_TZ

    def test_binary_float(self):
        assert canonicalize_type("oracle", "binary_float") == CT_FLOAT32

    def test_binary_double(self):
        assert canonicalize_type("oracle", "binary_double") == CT_FLOAT64

    def test_blob(self):
        assert canonicalize_type("oracle", "blob") == CT_BINARY


class TestCanonicaliseMySQL:
    def test_tinyint(self):
        assert canonicalize_type("mysql", "tinyint") == CT_INT8

    def test_tinyint_1_is_bool(self):
        # MySQL/MariaDB convention.
        assert canonicalize_type("mysql", "tinyint(1)") == CT_BOOL

    def test_smallint(self):
        assert canonicalize_type("mysql", "smallint") == CT_INT16

    def test_int(self):
        assert canonicalize_type("mysql", "int") == CT_INT32

    def test_bigint(self):
        assert canonicalize_type("mysql", "bigint") == CT_INT64

    def test_float_is_float32(self):
        assert canonicalize_type("mysql", "float") == CT_FLOAT32

    def test_double_is_float64(self):
        assert canonicalize_type("mysql", "double") == CT_FLOAT64

    def test_decimal(self):
        assert canonicalize_type("mysql", "decimal(18,2)") == "decimal(18,2)"

    def test_varchar(self):
        assert canonicalize_type("mysql", "varchar(255)") == "string(255)"

    def test_longtext(self):
        assert canonicalize_type("mysql", "longtext") == CT_TEXT

    def test_date(self):
        assert canonicalize_type("mysql", "date") == CT_DATE

    def test_datetime(self):
        assert canonicalize_type("mysql", "datetime") == CT_TIMESTAMP

    def test_timestamp_is_tz_aware(self):
        # MySQL TIMESTAMP is stored UTC, converted on read.
        assert canonicalize_type("mysql", "timestamp") == CT_TIMESTAMP_TZ

    def test_blob(self):
        assert canonicalize_type("mysql", "blob") == CT_BINARY


class TestCanonicaliseDuckDB:
    def test_integer(self):
        assert canonicalize_type("duckdb", "integer") == CT_INT32

    def test_bigint(self):
        assert canonicalize_type("duckdb", "bigint") == CT_INT64

    def test_decimal(self):
        assert canonicalize_type("duckdb", "decimal(18,2)") == "decimal(18,2)"

    def test_varchar_bare_is_string_unbounded(self):
        assert canonicalize_type("duckdb", "varchar") == CT_STRING

    def test_varchar_with_length(self):
        assert canonicalize_type("duckdb", "varchar(100)") == "string(100)"

    def test_double(self):
        assert canonicalize_type("duckdb", "double") == CT_FLOAT64

    def test_boolean(self):
        assert canonicalize_type("duckdb", "boolean") == CT_BOOL

    def test_date(self):
        assert canonicalize_type("duckdb", "date") == CT_DATE

    def test_timestamp(self):
        assert canonicalize_type("duckdb", "timestamp") == CT_TIMESTAMP

    def test_timestamptz(self):
        assert canonicalize_type("duckdb", "timestamptz") == CT_TIMESTAMP_TZ

    def test_blob(self):
        assert canonicalize_type("duckdb", "blob") == CT_BINARY


class TestCanonicaliseSnowflake:
    def test_number_with_precision(self):
        assert canonicalize_type("snowflake", "number(18,2)") == "decimal(18,2)"

    def test_int(self):
        assert canonicalize_type("snowflake", "int") == CT_INT32

    def test_bigint(self):
        assert canonicalize_type("snowflake", "bigint") == CT_INT64

    def test_float_is_float64(self):
        assert canonicalize_type("snowflake", "float") == CT_FLOAT64

    def test_varchar(self):
        assert canonicalize_type("snowflake", "varchar(255)") == "string(255)"

    def test_timestamp_ntz(self):
        assert canonicalize_type("snowflake", "timestamp_ntz") == CT_TIMESTAMP

    def test_timestamp_ltz_is_tz(self):
        assert canonicalize_type("snowflake", "timestamp_ltz") == CT_TIMESTAMP_TZ

    def test_variant_is_json(self):
        assert canonicalize_type("snowflake", "variant") == CT_JSON


class TestCanonicaliseBigQuery:
    def test_int64(self):
        assert canonicalize_type("bigquery", "int64") == CT_INT64

    def test_float64(self):
        assert canonicalize_type("bigquery", "float64") == CT_FLOAT64

    def test_numeric_default(self):
        assert canonicalize_type("bigquery", "numeric") == "decimal(38,9)"

    def test_string_with_length(self):
        assert canonicalize_type("bigquery", "string(100)") == "string(100)"

    def test_bytes(self):
        assert canonicalize_type("bigquery", "bytes") == CT_BINARY

    def test_date(self):
        assert canonicalize_type("bigquery", "date") == CT_DATE

    def test_datetime_is_naive_timestamp(self):
        assert canonicalize_type("bigquery", "datetime") == CT_TIMESTAMP

    def test_timestamp_is_tz(self):
        assert canonicalize_type("bigquery", "timestamp") == CT_TIMESTAMP_TZ


# ── canonicalize_type: parens parsing ────────────────────────────────


class TestParensParsing:
    def test_whitespace_inside_parens(self):
        assert canonicalize_type("postgresql", "numeric( 18 , 2 )") == "decimal(18,2)"

    def test_case_insensitive_base(self):
        assert canonicalize_type("postgresql", "VARCHAR(50)") == "string(50)"

    def test_mixed_case_dialect(self):
        assert canonicalize_type("PostgreSQL", "integer") == CT_INT32

    def test_precision_only_decimal(self):
        # NUMERIC(10) — precision but no scale; scale defaults to 0.
        assert canonicalize_type("postgresql", "numeric(10)") == "decimal(10,0)"

    def test_string_no_length(self):
        assert canonicalize_type("duckdb", "varchar") == CT_STRING

    def test_garbage_args_falls_back_to_default_decimal(self):
        # "decimal(abc)" — args don't parse; default to decimal(38,9).
        assert canonicalize_type("postgresql", "decimal(abc)") == "decimal(38,9)"


# ── canonicalize_type: dialect aliasing ─────────────────────────────


class TestDialectAliasing:
    def test_postgres_aliases_postgresql(self):
        assert canonicalize_type("postgres", "integer") == CT_INT32

    def test_pg_aliases_postgresql(self):
        assert canonicalize_type("pg", "bigint") == CT_INT64

    def test_redshift_aliases_postgresql(self):
        assert canonicalize_type("redshift", "varchar(50)") == "string(50)"

    def test_mssql_aliases_sqlserver(self):
        assert canonicalize_type("mssql", "int") == CT_INT32

    def test_sqlserver_aliases_sqlserver(self):
        assert canonicalize_type("sqlserver", "int") == CT_INT32

    def test_synapse_aliases_sqlserver(self):
        assert canonicalize_type("synapse", "nvarchar(100)") == "string(100)"

    def test_mariadb_aliases_mysql(self):
        assert canonicalize_type("mariadb", "tinyint(1)") == CT_BOOL

    def test_bq_aliases_bigquery(self):
        assert canonicalize_type("bq", "int64") == CT_INT64


# ── canonicalize_type: fallbacks ─────────────────────────────────────


class TestCanonicaliseFallbacks:
    def test_unknown_dialect_returns_unknown(self):
        assert canonicalize_type("frobnitz", "integer") == CT_UNKNOWN

    def test_empty_dialect_returns_unknown(self):
        assert canonicalize_type("", "integer") == CT_UNKNOWN

    def test_unknown_type_returns_unknown(self):
        assert canonicalize_type("postgresql", "frobnitz") == CT_UNKNOWN

    def test_empty_raw_type_returns_unknown(self):
        assert canonicalize_type("postgresql", "") == CT_UNKNOWN

    def test_none_raw_type_returns_unknown(self):
        assert canonicalize_type("postgresql", None) == CT_UNKNOWN  # type: ignore[arg-type]

    def test_none_dialect_returns_unknown(self):
        assert canonicalize_type(None, "integer") == CT_UNKNOWN  # type: ignore[arg-type]


# ── types_compatible: same-canonical (trivial) ──────────────────────


class TestCompatibleSame:
    def test_same_type_same_dialect(self):
        assert types_compatible(
            "duckdb", "integer", "duckdb", "integer"
        ) is True

    def test_pg_numeric_to_duckdb_decimal_same_precision(self):
        # The headline drift fix — these canonicalise identically.
        assert types_compatible(
            "duckdb", "decimal(18,2)", "postgresql", "numeric(18,2)"
        ) is True

    def test_mssql_decimal_to_postgresql_numeric(self):
        assert types_compatible(
            "postgresql", "numeric(10,4)", "mssql", "decimal(10,4)"
        ) is True

    def test_oracle_varchar2_to_mssql_nvarchar(self):
        assert types_compatible(
            "mssql", "nvarchar(255)", "oracle", "varchar2(255)"
        ) is True


# ── types_compatible: numeric widening ──────────────────────────────


class TestCompatibleNumeric:
    def test_int32_to_int64_widens(self):
        assert types_compatible(
            "postgresql", "bigint", "postgresql", "integer"
        ) is True

    def test_int64_to_int32_narrows(self):
        assert types_compatible(
            "postgresql", "integer", "postgresql", "bigint"
        ) is False

    def test_int16_to_int64_widens(self):
        assert types_compatible(
            "mysql", "bigint", "mysql", "smallint"
        ) is True

    def test_decimal_widens(self):
        assert types_compatible(
            "duckdb", "decimal(18,2)", "postgresql", "numeric(10,2)"
        ) is True

    def test_decimal_narrows_precision(self):
        assert types_compatible(
            "duckdb", "decimal(10,2)", "postgresql", "numeric(18,2)"
        ) is False

    def test_decimal_narrows_scale(self):
        assert types_compatible(
            "duckdb", "decimal(18,0)", "postgresql", "numeric(18,2)"
        ) is False

    def test_int_to_decimal_is_compatible(self):
        # int32 fits in decimal(18,2) — the canonical "incoming int,
        # destination decimal" warehouse pattern.
        assert types_compatible(
            "duckdb", "decimal(18,2)", "postgresql", "integer"
        ) is True

    def test_int_to_bare_decimal_is_compatible(self):
        # destination decimal(38,9) holds any int.
        assert types_compatible(
            "snowflake", "number", "postgresql", "bigint"
        ) is True

    def test_int_to_float64_is_compatible(self):
        assert types_compatible(
            "postgresql", "double precision", "postgresql", "bigint"
        ) is True

    def test_decimal_to_float64_is_compatible(self):
        assert types_compatible(
            "postgresql", "double precision",
            "postgresql", "numeric(18,2)",
        ) is True

    def test_float32_to_float64_widens(self):
        assert types_compatible(
            "postgresql", "double precision", "postgresql", "real"
        ) is True

    def test_float64_to_float32_narrows(self):
        assert types_compatible(
            "postgresql", "real", "postgresql", "double precision"
        ) is False


# ── types_compatible: string family ─────────────────────────────────


class TestCompatibleString:
    def test_string_to_text_always_ok(self):
        assert types_compatible(
            "postgresql", "text", "postgresql", "varchar(50)"
        ) is True

    def test_string_widens_length(self):
        assert types_compatible(
            "mssql", "nvarchar(255)", "postgresql", "varchar(50)"
        ) is True

    def test_string_narrows_length(self):
        assert types_compatible(
            "mssql", "nvarchar(50)", "postgresql", "varchar(255)"
        ) is False

    def test_bare_string_holds_anything(self):
        # DuckDB VARCHAR is unbounded — holds any sized string.
        assert types_compatible(
            "duckdb", "varchar", "postgresql", "varchar(500)"
        ) is True

    def test_sized_string_does_not_hold_bare(self):
        assert types_compatible(
            "postgresql", "varchar(50)", "duckdb", "varchar"
        ) is False

    def test_text_to_string_narrows(self):
        # text is unbounded — a bounded string cannot hold it.
        assert types_compatible(
            "postgresql", "varchar(255)", "postgresql", "text"
        ) is False


# ── types_compatible: temporal ──────────────────────────────────────


class TestCompatibleTemporal:
    def test_date_fits_in_timestamp(self):
        assert types_compatible(
            "postgresql", "timestamp", "postgresql", "date"
        ) is True

    def test_timestamp_to_date_narrows(self):
        assert types_compatible(
            "postgresql", "date", "postgresql", "timestamp"
        ) is False

    def test_timestamp_to_timestamp_tz_widens(self):
        assert types_compatible(
            "postgresql", "timestamptz", "postgresql", "timestamp"
        ) is True

    def test_timestamp_tz_to_timestamp_narrows(self):
        assert types_compatible(
            "postgresql", "timestamp", "postgresql", "timestamptz"
        ) is False

    def test_oracle_date_to_duckdb_timestamp(self):
        # Oracle DATE includes time → already canonicalises to timestamp.
        assert types_compatible(
            "duckdb", "timestamp", "oracle", "date"
        ) is True


# ── types_compatible: binary ────────────────────────────────────────


class TestCompatibleBinary:
    def test_binary_widens(self):
        assert types_compatible(
            "mssql", "varbinary(64)", "mssql", "varbinary(32)"
        ) is True

    def test_binary_narrows(self):
        assert types_compatible(
            "mssql", "varbinary(16)", "mssql", "varbinary(32)"
        ) is False

    def test_bare_binary_holds_sized(self):
        assert types_compatible(
            "postgresql", "bytea", "mssql", "varbinary(64)"
        ) is True


# ── types_compatible: incompatible families ─────────────────────────


class TestCompatibleIncompatible:
    def test_string_to_int_fails(self):
        assert types_compatible(
            "postgresql", "integer", "postgresql", "varchar(10)"
        ) is False

    def test_json_to_int_fails(self):
        assert types_compatible(
            "postgresql", "integer", "postgresql", "json"
        ) is False

    def test_bool_to_int_fails(self):
        # Not in the widening table — caller treats as drift even though
        # many engines coerce silently. Surface it.
        assert types_compatible(
            "postgresql", "integer", "postgresql", "boolean"
        ) is False

    def test_date_to_string_fails(self):
        assert types_compatible(
            "postgresql", "varchar(20)", "postgresql", "date"
        ) is False


# ── types_compatible: unknown-dialect fallback ──────────────────────


class TestCompatibleUnknownDialect:
    """When dialect isn't recognised, the function returns False so the
    caller falls back to string-equality (the pre-2026-05-27 behaviour).
    """

    def test_unknown_existing_dialect_returns_false(self):
        assert types_compatible(
            "frobnitz", "integer", "postgresql", "integer"
        ) is False

    def test_unknown_incoming_dialect_returns_false(self):
        assert types_compatible(
            "postgresql", "integer", "frobnitz", "integer"
        ) is False

    def test_both_unknown_returns_false(self):
        assert types_compatible(
            "frobnitz", "integer", "snorfnozzle", "integer"
        ) is False

    def test_empty_dialect_returns_false(self):
        assert types_compatible(
            "", "integer", "postgresql", "integer"
        ) is False

    def test_unknown_type_returns_false(self):
        # Even with known dialects, an unrecognised type → False so the
        # policy engine falls back to string-equality.
        assert types_compatible(
            "postgresql", "frobnitz", "postgresql", "integer"
        ) is False


# ── types_compatible: alias coverage ────────────────────────────────


class TestCompatibleWithAliases:
    def test_postgres_and_postgresql_equivalent(self):
        assert types_compatible(
            "postgres", "integer", "postgresql", "integer"
        ) is True

    def test_mssql_and_sqlserver_equivalent(self):
        assert types_compatible(
            "mssql", "int", "sqlserver", "int"
        ) is True

    def test_mariadb_and_mysql_equivalent(self):
        assert types_compatible(
            "mariadb", "bigint", "mysql", "bigint"
        ) is True
