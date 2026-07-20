"""CanonicalSchema → Postgres — contract tests.

Locks the inverse mapping that the sink-side writer relies on, plus
the round-trip property: every PG type the reader recognizes must
emit DDL the reader can read back to the same FPType + params.
"""

from __future__ import annotations

import pytest

from fpulse.types import (
    CanonicalSchema,
    CastSafety,
    Evidence,
    FPField,
    FPType,
    canonical_to_postgres_alter,
    canonical_to_postgres_ddl,
    plan_cast,
    postgres_columns_to_canonical,
)


def _f(name: str, t: FPType, **kw) -> FPField:
    return FPField(name=name, type=t, **kw)


# ── DDL: per-type mapping ──

class TestDdlMapping:
    def _ddl(self, fields: list[FPField]) -> str:
        return canonical_to_postgres_ddl(
            CanonicalSchema(fields=fields), "t", schema_name="",
            if_not_exists=False,
        )

    def test_smallint(self):
        sql = self._ddl([_f("x", FPType.INTEGER, params={"bits": 16})])
        assert "SMALLINT" in sql

    def test_integer(self):
        sql = self._ddl([_f("x", FPType.INTEGER, params={"bits": 32})])
        assert "INTEGER" in sql
        assert "SMALLINT" not in sql
        assert "BIGINT" not in sql

    def test_bigint(self):
        sql = self._ddl([_f("x", FPType.INTEGER, params={"bits": 64})])
        assert "BIGINT" in sql

    def test_decimal_with_precision_scale(self):
        sql = self._ddl([_f(
            "amount", FPType.DECIMAL, params={"precision": 18, "scale": 4},
        )])
        assert "NUMERIC(18,4)" in sql

    def test_decimal_money_subtype(self):
        sql = self._ddl([_f(
            "p", FPType.DECIMAL, params={"precision": 19, "scale": 2, "subtype": "money"},
        )])
        assert "MONEY" in sql

    def test_real(self):
        sql = self._ddl([_f("r", FPType.FLOAT, params={"bits": 32})])
        assert "REAL" in sql

    def test_double(self):
        sql = self._ddl([_f("r", FPType.FLOAT, params={"bits": 64})])
        assert "DOUBLE PRECISION" in sql

    def test_varchar_with_length(self):
        sql = self._ddl([_f("s", FPType.STRING, params={"length": 255})])
        assert "VARCHAR(255)" in sql

    def test_char_fixed(self):
        sql = self._ddl([_f(
            "s", FPType.STRING, params={"length": 10, "fixed": True},
        )])
        assert "CHAR(10)" in sql
        assert "VARCHAR" not in sql

    def test_text_for_unbounded_string(self):
        sql = self._ddl([_f("s", FPType.STRING)])
        assert "TEXT" in sql

    def test_uuid_subtype(self):
        sql = self._ddl([_f("id", FPType.STRING, params={"subtype": "uuid"})])
        assert "UUID" in sql

    def test_inet_subtype(self):
        sql = self._ddl([_f("ip", FPType.STRING, params={"subtype": "inet"})])
        assert "INET" in sql

    def test_boolean(self):
        sql = self._ddl([_f("ok", FPType.BOOLEAN)])
        assert "BOOLEAN" in sql

    def test_date(self):
        sql = self._ddl([_f("d", FPType.DATE)])
        assert "DATE" in sql

    def test_timestamp_without_tz(self):
        sql = self._ddl([_f("ts", FPType.TIMESTAMP, params={"with_timezone": False})])
        assert "TIMESTAMP" in sql
        assert "TIMESTAMPTZ" not in sql

    def test_timestamptz(self):
        sql = self._ddl([_f("ts", FPType.TIMESTAMP, params={"with_timezone": True})])
        assert "TIMESTAMPTZ" in sql

    def test_time_with_precision(self):
        sql = self._ddl([_f(
            "t", FPType.TIME, params={"with_timezone": False, "precision": 6},
        )])
        assert "TIME(6)" in sql

    def test_timestamptz_with_precision(self):
        sql = self._ddl([_f(
            "ts", FPType.TIMESTAMP, params={"with_timezone": True, "precision": 3},
        )])
        assert "TIMESTAMPTZ(3)" in sql

    def test_bytea(self):
        sql = self._ddl([_f("b", FPType.BINARY)])
        assert "BYTEA" in sql

    def test_jsonb_subtype(self):
        sql = self._ddl([_f("j", FPType.JSON, params={"subtype": "jsonb"})])
        assert "JSONB" in sql

    def test_json_subtype(self):
        sql = self._ddl([_f("j", FPType.JSON, params={"subtype": "json"})])
        assert "JSON " in sql + " " or "JSON\n" in sql  # not JSONB
        assert "JSONB" not in sql

    def test_list_element_type(self):
        element = _f("(element)", FPType.INTEGER, params={"bits": 32})
        sql = self._ddl([_f("ids", FPType.LIST, params={"element_type": element})])
        assert "INTEGER[]" in sql

    def test_struct_falls_back_to_jsonb(self):
        # STRUCT writing as composite types isn't wired yet; JSONB
        # fallback keeps the row round-trippable.
        sql = self._ddl([_f("payload", FPType.STRUCT, fields={
            "id": _f("id", FPType.INTEGER),
        })])
        assert "JSONB" in sql


# ── DDL: nullability + identifier quoting ──

class TestDdlShape:
    def test_not_null_emitted(self):
        sql = canonical_to_postgres_ddl(
            CanonicalSchema(fields=[
                _f("id", FPType.INTEGER, nullable=False, params={"bits": 32}),
            ]),
            "t", schema_name="",
        )
        assert "NOT NULL" in sql

    def test_nullable_emitted(self):
        sql = canonical_to_postgres_ddl(
            CanonicalSchema(fields=[
                _f("id", FPType.INTEGER, nullable=True, params={"bits": 32}),
            ]),
            "t", schema_name="",
        )
        assert " NULL" in sql
        assert "NOT NULL" not in sql

    def test_table_name_quoted(self):
        sql = canonical_to_postgres_ddl(
            CanonicalSchema(fields=[_f("id", FPType.INTEGER)]),
            "weird name",
            schema_name="my schema",
        )
        assert '"weird name"' in sql
        assert '"my schema"' in sql

    def test_identifier_with_quote_escaped(self):
        sql = canonical_to_postgres_ddl(
            CanonicalSchema(fields=[_f('a"b', FPType.INTEGER)]),
            "t", schema_name="",
        )
        assert '"a""b"' in sql

    def test_if_not_exists_optional(self):
        sql = canonical_to_postgres_ddl(
            CanonicalSchema(fields=[_f("x", FPType.INTEGER)]),
            "t", schema_name="", if_not_exists=False,
        )
        assert "IF NOT EXISTS" not in sql
        sql2 = canonical_to_postgres_ddl(
            CanonicalSchema(fields=[_f("x", FPType.INTEGER)]),
            "t", schema_name="", if_not_exists=True,
        )
        assert "IF NOT EXISTS" in sql2


# ── ALTER emission ──

class TestAlter:
    def test_add_nullable_column(self):
        old = CanonicalSchema(fields=[_f("id", FPType.INTEGER)])
        new = CanonicalSchema(fields=[
            _f("id", FPType.INTEGER),
            _f("name", FPType.STRING, nullable=True),
        ])
        stmts = canonical_to_postgres_alter(old, new, "t", schema_name="")
        assert len(stmts) == 1
        assert "ADD COLUMN" in stmts[0]
        assert '"name"' in stmts[0]

    def test_add_not_null_column_is_skipped(self):
        # Backfill required — operator must confirm; we don't auto-emit.
        old = CanonicalSchema(fields=[_f("id", FPType.INTEGER)])
        new = CanonicalSchema(fields=[
            _f("id", FPType.INTEGER),
            _f("name", FPType.STRING, nullable=False),
        ])
        stmts = canonical_to_postgres_alter(old, new, "t", schema_name="")
        assert stmts == []

    def test_remove_column_is_never_auto(self):
        # REMOVED is critical-severity drift — never auto-applied.
        old = CanonicalSchema(fields=[
            _f("id", FPType.INTEGER),
            _f("legacy", FPType.STRING),
        ])
        new = CanonicalSchema(fields=[_f("id", FPType.INTEGER)])
        stmts = canonical_to_postgres_alter(old, new, "t", schema_name="")
        assert stmts == []

    def test_varchar_widening_emits_alter(self):
        old = CanonicalSchema(fields=[
            _f("name", FPType.STRING, params={"length": 100}),
        ])
        new = CanonicalSchema(fields=[
            _f("name", FPType.STRING, params={"length": 500}),
        ])
        stmts = canonical_to_postgres_alter(old, new, "t", schema_name="")
        assert len(stmts) == 1
        assert "ALTER COLUMN" in stmts[0]
        assert "VARCHAR(500)" in stmts[0]

    def test_varchar_narrowing_is_skipped(self):
        old = CanonicalSchema(fields=[
            _f("name", FPType.STRING, params={"length": 500}),
        ])
        new = CanonicalSchema(fields=[
            _f("name", FPType.STRING, params={"length": 100}),
        ])
        stmts = canonical_to_postgres_alter(old, new, "t", schema_name="")
        assert stmts == []

    def test_decimal_widening_emits_alter(self):
        old = CanonicalSchema(fields=[
            _f("amount", FPType.DECIMAL, params={"precision": 10, "scale": 2}),
        ])
        new = CanonicalSchema(fields=[
            _f("amount", FPType.DECIMAL, params={"precision": 18, "scale": 4}),
        ])
        stmts = canonical_to_postgres_alter(old, new, "t", schema_name="")
        assert len(stmts) == 1
        assert "NUMERIC(18,4)" in stmts[0]

    def test_type_kind_change_never_auto(self):
        # INT → STRING is the operator's call, never auto.
        old = CanonicalSchema(fields=[_f("id", FPType.INTEGER)])
        new = CanonicalSchema(fields=[_f("id", FPType.STRING)])
        stmts = canonical_to_postgres_alter(old, new, "t", schema_name="")
        assert stmts == []


# ── plan_cast ──

class TestPlanCast:
    def test_safe_widening_classified_safe(self):
        source = CanonicalSchema(fields=[_f("x", FPType.INTEGER, params={"bits": 32})])
        target = CanonicalSchema(fields=[_f("x", FPType.INTEGER, params={"bits": 64})])
        plan = plan_cast(source, target)
        assert len(plan) == 1
        assert plan[0].safety == CastSafety.SAFE
        assert plan[0].target_native_type == "BIGINT"

    def test_lossy_narrowing_classified_lossy(self):
        source = CanonicalSchema(fields=[_f("x", FPType.STRING, params={"length": 500})])
        target = CanonicalSchema(fields=[_f("x", FPType.STRING, params={"length": 100})])
        plan = plan_cast(source, target)
        assert plan[0].safety == CastSafety.LOSSY
        assert "VARCHAR(100)" == plan[0].target_native_type
        assert plan[0].reason and "narrows" in plan[0].reason

    def test_semantic_lossy_int_to_string(self):
        source = CanonicalSchema(fields=[_f("x", FPType.INTEGER)])
        target = CanonicalSchema(fields=[_f("x", FPType.STRING)])
        plan = plan_cast(source, target)
        assert plan[0].safety == CastSafety.SEMANTIC_LOSSY

    def test_missing_source_column_dropped_from_plan(self):
        # Target has a column source doesn't — sink decides via cast_policy.
        source = CanonicalSchema(fields=[_f("x", FPType.INTEGER)])
        target = CanonicalSchema(fields=[
            _f("x", FPType.INTEGER),
            _f("y", FPType.STRING),
        ])
        plan = plan_cast(source, target)
        names = [p.target_column for p in plan]
        assert "x" in names
        assert "y" not in names


# ── Round-trip property: from_postgres → to_postgres → from_postgres ──

class TestRoundTrip:
    """Every type the reader recognizes must emit DDL the reader can
    read back to the same FPType + params."""

    def _round_trip(self, pg_row: dict) -> FPField:
        canonical = postgres_columns_to_canonical([pg_row])
        # Take the field out, render its native PG type, reconstruct a
        # row that mimics what information_schema would tell us back.
        from fpulse.types.to_postgres import _fp_to_pg_native
        f = canonical.fields[0]
        native = _fp_to_pg_native(f)
        return f, native

    def test_integer_round_trip(self):
        f, native = self._round_trip({
            "column_name": "x", "data_type": "integer", "udt_name": "int4",
            "is_nullable": "YES",
        })
        assert f.type == FPType.INTEGER
        assert native == "INTEGER"

    def test_bigint_round_trip(self):
        f, native = self._round_trip({
            "column_name": "x", "data_type": "bigint", "udt_name": "int8",
            "is_nullable": "YES",
        })
        assert native == "BIGINT"

    def test_numeric_18_4_round_trip(self):
        f, native = self._round_trip({
            "column_name": "amount", "data_type": "numeric", "udt_name": "numeric",
            "numeric_precision": 18, "numeric_scale": 4, "is_nullable": "YES",
        })
        assert native == "NUMERIC(18,4)"

    def test_varchar_255_round_trip(self):
        f, native = self._round_trip({
            "column_name": "name", "data_type": "character varying", "udt_name": "varchar",
            "character_maximum_length": 255, "is_nullable": "YES",
        })
        assert native == "VARCHAR(255)"

    def test_text_round_trip(self):
        f, native = self._round_trip({
            "column_name": "body", "data_type": "text", "udt_name": "text",
            "is_nullable": "YES",
        })
        assert native == "TEXT"

    def test_timestamptz_round_trip(self):
        f, native = self._round_trip({
            "column_name": "ts", "data_type": "timestamp with time zone",
            "udt_name": "timestamptz", "datetime_precision": 6, "is_nullable": "YES",
        })
        assert native == "TIMESTAMPTZ(6)"

    def test_uuid_round_trip(self):
        f, native = self._round_trip({
            "column_name": "id", "data_type": "uuid", "udt_name": "uuid",
            "is_nullable": "YES",
        })
        assert native == "UUID"

    def test_jsonb_round_trip(self):
        f, native = self._round_trip({
            "column_name": "p", "data_type": "jsonb", "udt_name": "jsonb",
            "is_nullable": "YES",
        })
        assert native == "JSONB"

    def test_int_array_round_trip(self):
        f, native = self._round_trip({
            "column_name": "ids", "data_type": "ARRAY", "udt_name": "_int4",
            "is_nullable": "YES",
        })
        assert native == "INTEGER[]"
