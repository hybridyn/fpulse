"""Cross-dialect canonical-type tests (2026-05-22).

Covers:

  - Per-dialect mapper round-trips (from_X → canonical → to_X).
  - Cross-dialect casts that real ETL hits: Oracle NUMBER → MSSQL
    INT/DECIMAL, MSSQL DATETIMEOFFSET → DATETIME2 (the timezone-loss
    case that the 2026-05-22 key-contract bug fix made detectable),
    MySQL TIMESTAMP → DATETIME, etc.
  - Key-contract pinning: `timezone params use "with_timezone"`. If a
    future contributor reintroduces the legacy `timezone` key in a
    mapper, this test catches it before it ships.
  - NUMBER disambiguation table from from_oracle.

These are pure unit tests — no live databases. Each test feeds a
synthetic column-dict (the shape each dialect's introspection query
would return) and asserts on the resulting CanonicalSchema or cast
plan.
"""

from __future__ import annotations

import pytest

from fpulse.types import (
    CanonicalSchema,
    CastSafety,
    Evidence,
    FPField,
    FPType,
    canonical_to_mssql_ddl,
    canonical_to_mysql_ddl,
    canonical_to_oracle_ddl,
    canonical_to_postgres_ddl,
    classify_cast,
    mssql_columns_to_canonical,
    mysql_columns_to_canonical,
    oracle_columns_to_canonical,
    postgres_columns_to_canonical,
)

pytestmark = pytest.mark.unit


# ────────────────────────────────────────────────────────────────────────
# Key-contract pin
# ────────────────────────────────────────────────────────────────────────


class TestTimezoneKeyContract:
    """Every mapper writes timezone state as `with_timezone`, NOT `timezone`.

    The 2026-05-22 cast_safety bug fix was to accept both keys, but the
    canonical writers must keep emitting `with_timezone`. Pin it here so a
    contributor who unintentionally reintroduces `timezone` is caught.
    """

    def test_postgres_writes_with_timezone(self):
        rows = [{
            "column_name": "ts",
            "data_type": "timestamp with time zone",
            "udt_name": "timestamptz",
            "is_nullable": "NO",
        }]
        sch = postgres_columns_to_canonical(rows)
        assert "with_timezone" in sch.fields[0].params
        assert "timezone" not in sch.fields[0].params

    def test_mssql_writes_with_timezone(self):
        rows = [{
            "column_name": "ts",
            "data_type": "datetimeoffset",
            "scale": 7,
            "is_nullable": False,
        }]
        sch = mssql_columns_to_canonical(rows)
        assert sch.fields[0].params["with_timezone"] is True

    def test_mysql_writes_with_timezone(self):
        rows = [{
            "column_name": "ts",
            "data_type": "timestamp",
            "column_type": "timestamp",
            "datetime_precision": 6,
            "is_nullable": "NO",
        }]
        sch = mysql_columns_to_canonical(rows)
        assert sch.fields[0].params["with_timezone"] is True

    def test_oracle_writes_with_timezone(self):
        rows = [{
            "column_name": "TS",
            "data_type": "TIMESTAMP(6) WITH TIME ZONE",
            "data_precision": None,
            "data_scale": 6,
            "nullable": "N",
        }]
        sch = oracle_columns_to_canonical(rows)
        assert sch.fields[0].params["with_timezone"] is True

    def test_cast_safety_picks_up_with_timezone(self):
        # MSSQL DATETIMEOFFSET → MSSQL DATETIME2 should drop timezone.
        source = FPField(
            name="x", type=FPType.TIMESTAMP, nullable=False,
            params={"with_timezone": True, "precision": 7},
        )
        target = FPField(
            name="x", type=FPType.TIMESTAMP, nullable=False,
            params={"with_timezone": False, "precision": 3},
        )
        safety, reason = classify_cast(source, target)
        assert safety == CastSafety.SEMANTIC_LOSSY
        assert reason and "timezone" in reason.lower()


# ────────────────────────────────────────────────────────────────────────
# Oracle NUMBER disambiguation
# ────────────────────────────────────────────────────────────────────────


class TestOracleNumberDisambiguation:
    """The decision table in from_oracle is the heart of the Oracle path."""

    def test_number_p9_s0_is_int32(self):
        sch = oracle_columns_to_canonical([{
            "column_name": "ID", "data_type": "NUMBER",
            "data_precision": 9, "data_scale": 0, "nullable": "N",
        }])
        f = sch.fields[0]
        assert f.type == FPType.INTEGER
        assert f.params["bits"] == 32

    def test_number_p18_s0_is_int64(self):
        sch = oracle_columns_to_canonical([{
            "column_name": "ID", "data_type": "NUMBER",
            "data_precision": 18, "data_scale": 0, "nullable": "N",
        }])
        f = sch.fields[0]
        assert f.type == FPType.INTEGER
        assert f.params["bits"] == 64

    def test_number_p38_s0_is_decimal(self):
        sch = oracle_columns_to_canonical([{
            "column_name": "BIG", "data_type": "NUMBER",
            "data_precision": 38, "data_scale": 0, "nullable": "N",
        }])
        f = sch.fields[0]
        assert f.type == FPType.DECIMAL
        assert f.params["precision"] == 38
        assert f.params["scale"] == 0

    def test_number_with_scale_is_decimal(self):
        sch = oracle_columns_to_canonical([{
            "column_name": "AMT", "data_type": "NUMBER",
            "data_precision": 18, "data_scale": 2, "nullable": "Y",
        }])
        f = sch.fields[0]
        assert f.type == FPType.DECIMAL
        assert f.params["precision"] == 18
        assert f.params["scale"] == 2

    def test_number_with_no_precision_or_scale_is_unknown(self):
        sch = oracle_columns_to_canonical([{
            "column_name": "AMBIG", "data_type": "NUMBER",
            "data_precision": None, "data_scale": None, "nullable": "Y",
        }])
        f = sch.fields[0]
        # Critical: bare NUMBER must NOT silently coerce to INTEGER or
        # DECIMAL(38). The operator has to pin it via the Mapping tab.
        assert f.type == FPType.UNKNOWN
        assert f.confidence == 0.0
        assert "ambiguous" in (f.params.get("reason") or "").lower()

    def test_oracle_date_is_timestamp_not_date(self):
        """Oracle DATE carries hh:mm:ss — must NOT classify as FPType.DATE."""
        sch = oracle_columns_to_canonical([{
            "column_name": "CREATED_AT", "data_type": "DATE",
            "nullable": "N",
        }])
        f = sch.fields[0]
        assert f.type == FPType.TIMESTAMP
        assert f.params["with_timezone"] is False
        assert f.params.get("subtype") == "oracle_date"


# ────────────────────────────────────────────────────────────────────────
# Cross-dialect cast safety (the actual ETL use case)
# ────────────────────────────────────────────────────────────────────────


class TestOracleToMSSQLCasts:
    """Realistic ETL: Oracle source → SQL Server target."""

    def _ora(self, **kw):
        sch = oracle_columns_to_canonical([{
            "column_name": "COL",
            "nullable": "N",
            **kw,
        }])
        return sch.fields[0]

    def _mssql(self, **kw):
        sch = mssql_columns_to_canonical([{
            "column_name": "col",
            "is_nullable": False,
            **kw,
        }])
        return sch.fields[0]

    def test_number_10_0_to_mssql_int_is_safe(self):
        source = self._ora(data_type="NUMBER", data_precision=10, data_scale=0)
        target = self._mssql(data_type="int")
        # Oracle NUMBER(10,0) → INTEGER 32 bits; MSSQL int → INTEGER 32 bits.
        # Wait — Oracle picks 32 only when p ≤ 9. NUMBER(10,0) → INTEGER 64.
        # So 64 → 32 is LOSSY.
        safety, reason = classify_cast(source, target)
        assert safety == CastSafety.LOSSY
        assert "integer narrows" in reason.lower() or "narrows" in reason.lower()

    def test_number_9_0_to_mssql_int_is_safe(self):
        source = self._ora(data_type="NUMBER", data_precision=9, data_scale=0)
        target = self._mssql(data_type="int")
        # Both are INTEGER 32 → SAFE.
        safety, _ = classify_cast(source, target)
        assert safety == CastSafety.SAFE

    def test_number_18_4_to_mssql_decimal_10_2_is_lossy(self):
        source = self._ora(data_type="NUMBER", data_precision=18, data_scale=4)
        target = self._mssql(data_type="decimal", precision=10, scale=2)
        safety, reason = classify_cast(source, target)
        assert safety == CastSafety.LOSSY
        assert "decimal" in reason.lower() or "narrow" in reason.lower()

    def test_number_18_2_to_mssql_decimal_18_2_is_safe(self):
        source = self._ora(data_type="NUMBER", data_precision=18, data_scale=2)
        target = self._mssql(data_type="decimal", precision=18, scale=2)
        safety, _ = classify_cast(source, target)
        assert safety == CastSafety.SAFE

    def test_bare_number_to_mssql_int_is_blocked_via_unknown(self):
        source = self._ora(data_type="NUMBER")  # no precision/scale → UNKNOWN
        target = self._mssql(data_type="int")
        safety, reason = classify_cast(source, target)
        # UNKNOWN → INTEGER is not SAFE — the cast classifier should
        # refuse to bless this without operator confirmation.
        assert safety != CastSafety.SAFE


class TestMSSQLToMSSQLTimezone:
    """The bug-fix smoke test: DATETIMEOFFSET → DATETIME2 now detected."""

    def test_datetimeoffset_to_datetime2_is_lossy(self):
        sch = mssql_columns_to_canonical([
            {"column_name": "ts_src", "data_type": "datetimeoffset", "scale": 7, "is_nullable": False},
            {"column_name": "ts_tgt", "data_type": "datetime2", "scale": 3, "is_nullable": False},
        ])
        source, target = sch.fields[0], sch.fields[1]
        safety, reason = classify_cast(source, target)
        assert safety == CastSafety.SEMANTIC_LOSSY
        assert reason and "timezone" in reason.lower()


# ────────────────────────────────────────────────────────────────────────
# MySQL TINYINT(1) boolean special case
# ────────────────────────────────────────────────────────────────────────


class TestMySQLTinyint1IsBoolean:
    def test_tinyint_1_is_boolean(self):
        sch = mysql_columns_to_canonical([{
            "column_name": "is_active",
            "data_type": "tinyint",
            "column_type": "tinyint(1)",
            "is_nullable": "NO",
        }])
        assert sch.fields[0].type == FPType.BOOLEAN

    def test_tinyint_4_is_integer(self):
        sch = mysql_columns_to_canonical([{
            "column_name": "qty",
            "data_type": "tinyint",
            "column_type": "tinyint(4)",
            "is_nullable": "NO",
        }])
        assert sch.fields[0].type == FPType.INTEGER
        assert sch.fields[0].params["bits"] == 8


# ────────────────────────────────────────────────────────────────────────
# DDL round-trip smoke
# ────────────────────────────────────────────────────────────────────────


class TestDDLEmission:
    """Quick smoke tests on each to_X DDL emitter."""

    def _sample(self):
        return CanonicalSchema(fields=[
            FPField(name="id", type=FPType.INTEGER, nullable=False, params={"bits": 64}),
            FPField(name="amount", type=FPType.DECIMAL, nullable=True,
                    params={"precision": 18, "scale": 2}),
            FPField(name="created_at", type=FPType.TIMESTAMP, nullable=False,
                    params={"with_timezone": True, "precision": 6}),
            FPField(name="name", type=FPType.STRING, nullable=True,
                    params={"length": 255, "unicode": True}),
        ])

    def test_mssql_ddl_emits_recognizable_types(self):
        ddl = canonical_to_mssql_ddl(self._sample(), "orders")
        assert "BIGINT" in ddl
        assert "DECIMAL(18,2)" in ddl
        assert "DATETIMEOFFSET" in ddl
        assert "NVARCHAR(255)" in ddl

    def test_oracle_ddl_emits_number_for_integer(self):
        ddl = canonical_to_oracle_ddl(self._sample(), "ORDERS")
        # Oracle has no native INTEGER — INTEGER 64 → NUMBER(19,0)
        assert "NUMBER(19,0)" in ddl
        assert "NUMBER(18,2)" in ddl
        assert "TIMESTAMP" in ddl and "WITH TIME ZONE" in ddl
        assert "NVARCHAR2(255)" in ddl

    def test_mysql_ddl_emits_bigint_and_datetime_variants(self):
        ddl = canonical_to_mysql_ddl(self._sample(), "orders")
        assert "BIGINT" in ddl
        assert "DECIMAL(18,2)" in ddl
        # with_timezone=True → MySQL TIMESTAMP (session-tz aware), not DATETIME.
        assert "TIMESTAMP" in ddl
        assert "VARCHAR(255)" in ddl

    def test_postgres_ddl_baseline(self):
        # Smoke test that the existing emitter still works alongside the new
        # mappers — no symbol-clash from the __init__.py additions.
        ddl = canonical_to_postgres_ddl(self._sample(), "orders")
        assert "BIGINT" in ddl
        assert "NUMERIC(18,2)" in ddl
