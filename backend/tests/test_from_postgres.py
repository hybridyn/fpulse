"""Postgres → CanonicalSchema — contract tests.

Pure-function tests over ``postgres_columns_to_canonical``. No real DB
needed: the test fixtures pass in dicts shaped like the rows the
``information_schema.columns`` query returns, so the mapping logic is
exercised deterministically.

Locks the per-type mapping so the sink-side writer and the Mapping
tab can rely on the same FPType + params contract.
"""

from __future__ import annotations

import pytest

from fpulse.types import (
    CanonicalSchema,
    Evidence,
    FPType,
    diff_schemas,
    postgres_columns_to_canonical,
)


def _row(**kw) -> dict:
    """Build a row dict with the introspection-query column names."""
    defaults = {
        "column_name": "c",
        "data_type": "text",
        "udt_name": "text",
        "is_nullable": "YES",
        "character_maximum_length": None,
        "numeric_precision": None,
        "numeric_scale": None,
        "datetime_precision": None,
        "ordinal_position": 1,
    }
    defaults.update(kw)
    return defaults


# ── Integers ──

class TestIntegers:
    def test_smallint_is_int16(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="x", data_type="smallint", udt_name="int2"),
        ])
        assert schema.fields[0].type == FPType.INTEGER
        assert schema.fields[0].params["bits"] == 16

    def test_integer_is_int32(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="x", data_type="integer", udt_name="int4"),
        ])
        assert schema.fields[0].params["bits"] == 32

    def test_bigint_is_int64(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="x", data_type="bigint", udt_name="int8"),
        ])
        assert schema.fields[0].params["bits"] == 64

    def test_serial_maps_to_integer_kind(self):
        # PG advertises serial via udt_name even when data_type is "integer"
        schema = postgres_columns_to_canonical([
            _row(column_name="id", data_type="integer", udt_name="serial"),
        ])
        assert schema.fields[0].type == FPType.INTEGER
        assert schema.fields[0].params["bits"] == 32


# ── Decimal / Numeric ──

class TestDecimal:
    def test_numeric_with_precision_scale(self):
        schema = postgres_columns_to_canonical([
            _row(
                column_name="amount",
                data_type="numeric",
                udt_name="numeric",
                numeric_precision=18,
                numeric_scale=4,
            ),
        ])
        f = schema.fields[0]
        assert f.type == FPType.DECIMAL
        assert f.params["precision"] == 18
        assert f.params["scale"] == 4

    def test_money_locks_to_19_2(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="price", data_type="money", udt_name="money"),
        ])
        f = schema.fields[0]
        assert f.type == FPType.DECIMAL
        assert f.params["precision"] == 19
        assert f.params["scale"] == 2
        assert f.params["subtype"] == "money"


# ── Floats ──

class TestFloats:
    def test_real_is_float32(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="r", data_type="real", udt_name="float4"),
        ])
        assert schema.fields[0].type == FPType.FLOAT
        assert schema.fields[0].params["bits"] == 32

    def test_double_is_float64(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="r", data_type="double precision", udt_name="float8"),
        ])
        assert schema.fields[0].params["bits"] == 64


# ── Strings ──

class TestStrings:
    def test_varchar_with_length(self):
        schema = postgres_columns_to_canonical([
            _row(
                column_name="name",
                data_type="character varying",
                udt_name="varchar",
                character_maximum_length=255,
            ),
        ])
        f = schema.fields[0]
        assert f.type == FPType.STRING
        assert f.params["length"] == 255

    def test_varchar_no_length_is_unbounded(self):
        schema = postgres_columns_to_canonical([
            _row(
                column_name="bio",
                data_type="character varying",
                udt_name="varchar",
                character_maximum_length=None,
            ),
        ])
        assert "length" not in schema.fields[0].params

    def test_text_is_unbounded_string(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="body", data_type="text", udt_name="text"),
        ])
        f = schema.fields[0]
        assert f.type == FPType.STRING
        assert "length" not in f.params

    def test_char_marks_fixed(self):
        schema = postgres_columns_to_canonical([
            _row(
                column_name="code",
                data_type="character",
                udt_name="bpchar",
                character_maximum_length=10,
            ),
        ])
        f = schema.fields[0]
        assert f.params.get("fixed") is True
        assert f.params["length"] == 10

    def test_uuid_maps_to_string_with_subtype(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="id", data_type="uuid", udt_name="uuid"),
        ])
        f = schema.fields[0]
        assert f.type == FPType.STRING
        assert f.params["subtype"] == "uuid"
        assert f.params["length"] == 36

    def test_inet_is_string_subtype(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="ip", data_type="inet", udt_name="inet"),
        ])
        f = schema.fields[0]
        assert f.type == FPType.STRING
        assert f.params["subtype"] == "inet"


# ── Booleans ──

class TestBoolean:
    def test_bool(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="ok", data_type="boolean", udt_name="bool"),
        ])
        assert schema.fields[0].type == FPType.BOOLEAN


# ── Date / Time ──

class TestTemporals:
    def test_date(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="d", data_type="date", udt_name="date"),
        ])
        assert schema.fields[0].type == FPType.DATE

    def test_timestamp_without_timezone(self):
        schema = postgres_columns_to_canonical([
            _row(
                column_name="ts",
                data_type="timestamp without time zone",
                udt_name="timestamp",
                datetime_precision=6,
            ),
        ])
        f = schema.fields[0]
        assert f.type == FPType.TIMESTAMP
        assert f.params["with_timezone"] is False
        assert f.params["precision"] == 6

    def test_timestamptz(self):
        schema = postgres_columns_to_canonical([
            _row(
                column_name="ts",
                data_type="timestamp with time zone",
                udt_name="timestamptz",
                datetime_precision=3,
            ),
        ])
        f = schema.fields[0]
        assert f.params["with_timezone"] is True
        assert f.params["precision"] == 3

    def test_time_without_tz(self):
        schema = postgres_columns_to_canonical([
            _row(
                column_name="t",
                data_type="time without time zone",
                udt_name="time",
                datetime_precision=6,
            ),
        ])
        f = schema.fields[0]
        assert f.type == FPType.TIME
        assert f.params["with_timezone"] is False


# ── Binary / JSON ──

class TestBinaryAndJson:
    def test_bytea_is_binary(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="b", data_type="bytea", udt_name="bytea"),
        ])
        assert schema.fields[0].type == FPType.BINARY

    def test_json_subtype(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="payload", data_type="json", udt_name="json"),
        ])
        f = schema.fields[0]
        assert f.type == FPType.JSON
        assert f.params["subtype"] == "json"

    def test_jsonb_subtype(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="payload", data_type="jsonb", udt_name="jsonb"),
        ])
        assert schema.fields[0].params["subtype"] == "jsonb"


# ── Arrays ──

class TestArrays:
    def test_int_array(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="ids", data_type="ARRAY", udt_name="_int4"),
        ])
        f = schema.fields[0]
        assert f.type == FPType.LIST
        element = f.params["element_type"]
        assert element.type == FPType.INTEGER
        assert element.params["bits"] == 32

    def test_text_array(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="tags", data_type="ARRAY", udt_name="_text"),
        ])
        element = schema.fields[0].params["element_type"]
        assert element.type == FPType.STRING


# ── Nullability + Evidence ──

class TestMetadata:
    def test_not_null_flag_respected(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="x", data_type="integer", udt_name="int4", is_nullable="NO"),
        ])
        assert schema.fields[0].nullable is False

    def test_evidence_is_advertised(self):
        schema = postgres_columns_to_canonical([
            _row(column_name="x", data_type="integer", udt_name="int4"),
        ])
        assert schema.fields[0].evidence == Evidence.ADVERTISED

    def test_provenance_records_pg_native_type(self):
        schema = postgres_columns_to_canonical([
            _row(
                column_name="amount",
                data_type="numeric",
                udt_name="numeric",
                numeric_precision=18,
                numeric_scale=2,
            ),
        ])
        prov = schema.fields[0].provenance
        assert len(prov) == 1
        assert "NUMERIC" in prov[0].source
        assert "(18,2)" in prov[0].source


# ── Unknown handling ──

class TestUnknown:
    def test_unmapped_type_preserves_native_raw(self):
        # tsvector is real but we don't bind it to an FPType today.
        schema = postgres_columns_to_canonical([
            _row(column_name="ts", data_type="tsvector", udt_name="tsvector"),
        ])
        f = schema.fields[0]
        assert f.type == FPType.UNKNOWN
        assert f.native_raw  # something descriptive, not empty


# ── Round-trip drift check ──

class TestRoundTrip:
    def test_same_schema_has_zero_drift(self):
        rows = [
            _row(column_name="id", data_type="integer", udt_name="int4", is_nullable="NO"),
            _row(
                column_name="amount", data_type="numeric", udt_name="numeric",
                numeric_precision=18, numeric_scale=2,
            ),
            _row(
                column_name="name", data_type="character varying", udt_name="varchar",
                character_maximum_length=255,
            ),
            _row(column_name="created_at", data_type="timestamp with time zone",
                 udt_name="timestamptz", datetime_precision=6),
        ]
        a = postgres_columns_to_canonical(rows)
        b = postgres_columns_to_canonical(rows)
        assert diff_schemas(a, b) == []

    def test_widening_pg_column_surfaces_as_widening(self):
        # Operator ALTERed varchar(100) → varchar(500) — should land
        # as PARAMS_WIDENED at INFO severity.
        before = postgres_columns_to_canonical([
            _row(
                column_name="bio", data_type="character varying", udt_name="varchar",
                character_maximum_length=100,
            ),
        ])
        after = postgres_columns_to_canonical([
            _row(
                column_name="bio", data_type="character varying", udt_name="varchar",
                character_maximum_length=500,
            ),
        ])
        diffs = diff_schemas(before, after)
        assert len(diffs) == 1
        assert diffs[0].path == "bio"
        # PARAMS_WIDENED with INFO severity — same engine as test_drift_engine.
        assert diffs[0].severity.value == "info"
        assert diffs[0].category.value == "params_widened"

    def test_narrowing_pg_column_surfaces_as_critical(self):
        before = postgres_columns_to_canonical([
            _row(
                column_name="amount", data_type="numeric", udt_name="numeric",
                numeric_precision=18, numeric_scale=4,
            ),
        ])
        after = postgres_columns_to_canonical([
            _row(
                column_name="amount", data_type="numeric", udt_name="numeric",
                numeric_precision=10, numeric_scale=2,
            ),
        ])
        diffs = diff_schemas(before, after)
        assert diffs[0].severity.value == "critical"
        assert diffs[0].category.value == "params_narrowed"
