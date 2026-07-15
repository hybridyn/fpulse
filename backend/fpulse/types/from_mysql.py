"""MySQL → CanonicalSchema.

Smaller delta than from_postgres / from_mssql / from_oracle — MySQL's
type system is close to ANSI for the most part. Two MySQL-specific
gotchas worth calling out:

  1. **TINYINT(1) is the conventional boolean.** Many ORMs use TINYINT(1)
     for booleans. We classify TINYINT with display_width==1 as BOOLEAN
     and any other TINYINT as INTEGER(bits=8). Operators who actually
     want a TINYINT (not a boolean) declare it as TINYINT(2)+ or use
     SMALLINT.

  2. **ENUM and SET are STRING with a subtype.** The enumerated value
     list rides in params so the Mapping tab can show "this field is
     constrained to {a, b, c}". SET goes to STRING with subtype="set"
     and a comma-joined value list (MySQL serializes SETs that way on
     the wire). The cast layer doesn't enforce the enumeration today;
     a future hardening pass can add a CHECK constraint on the
     target side.

Other mappings follow MySQL docs:

  - TINYINT/SMALLINT/MEDIUMINT/INT/BIGINT  → INTEGER(bits=8/16/24/32/64)
  - UNSIGNED variants                       → INTEGER(bits, unsigned=True)
  - DECIMAL(p, s) / NUMERIC(p, s)           → DECIMAL(precision, scale)
  - FLOAT / DOUBLE                          → FLOAT(bits=32 / 64)
  - CHAR(n) / VARCHAR(n)                    → STRING(length=n, fixed?)
  - TINYTEXT/TEXT/MEDIUMTEXT/LONGTEXT       → STRING (unbounded)
  - BINARY(n) / VARBINARY(n)                → BINARY(length=n)
  - TINYBLOB/BLOB/MEDIUMBLOB/LONGBLOB       → BINARY (unbounded)
  - BIT(n)                                  → BINARY(length=ceil(n/8), subtype="bit")
  - DATE                                    → DATE
  - DATETIME(p)                             → TIMESTAMP(precision=p, with_timezone=False)
  - TIMESTAMP(p)                            → TIMESTAMP(precision=p, with_timezone=True)
                                              (MySQL TIMESTAMP stores UTC; client
                                              sees session-tz conversion. Treat as
                                              tz-bearing so cross-dialect casts to
                                              tz-naive targets are flagged.)
  - TIME(p)                                 → TIME(precision=p, with_timezone=False)
  - YEAR                                    → INTEGER(bits=16, subtype="year")
  - JSON                                    → JSON
  - ENUM('a','b',...)                       → STRING(subtype="enum", choices=[...])
  - SET('a','b',...)                        → STRING(subtype="set", choices=[...])
  - GEOMETRY/POINT/POLYGON/...              → BINARY(subtype="<geom>")
"""

from __future__ import annotations

import re
from typing import Any

from fpulse.types.canonical import (
    CanonicalSchema,
    Evidence,
    FPField,
    FPType,
    Provenance,
)


# ── Public surface ──

def mysql_columns_to_canonical(rows: list[dict[str, Any]]) -> CanonicalSchema:
    """Convert ``INFORMATION_SCHEMA.COLUMNS``-shape rows → CanonicalSchema."""
    fields: list[FPField] = []
    for row in rows:
        name = row.get("column_name") or row.get("name") or row.get("COLUMN_NAME")
        if not name:
            continue
        # Normalize the row to lowercase keys so tests can pass either
        # convention (MySQL drivers return uppercase by default).
        norm = {k.lower(): v for k, v in row.items()}
        norm["column_name"] = name
        fp_type, params, native_raw = _mysql_to_fptype(norm)
        nullable = _read_nullable(norm)
        fields.append(FPField(
            name=name,
            type=fp_type,
            nullable=nullable,
            params=params,
            evidence=Evidence.ADVERTISED,
            confidence=1.0,
            provenance=[Provenance(
                source=f"MySQL {native_raw}",
                confidence=1.0,
                sample_size=0,
            )],
            native_raw=native_raw,
        ))
    return CanonicalSchema(fields=fields)


# Canonical introspection query. Returns more than the prior bare
# `name, type, nullable` shape: also precision/scale/length/datetime
# precision/column_type (which is the full type expression like
# "tinyint(1)" or "decimal(18,2) unsigned" — we parse it for the bits
# that INFORMATION_SCHEMA doesn't expose elsewhere).
CANONICAL_COLUMN_QUERY_MYSQL = """
    SELECT
        COLUMN_NAME            AS column_name,
        DATA_TYPE              AS data_type,
        COLUMN_TYPE            AS column_type,
        CHARACTER_MAXIMUM_LENGTH AS character_maximum_length,
        NUMERIC_PRECISION      AS numeric_precision,
        NUMERIC_SCALE          AS numeric_scale,
        DATETIME_PRECISION     AS datetime_precision,
        IS_NULLABLE            AS is_nullable,
        COLUMN_DEFAULT         AS column_default,
        COLUMN_KEY             AS column_key,
        EXTRA                  AS extra,
        ORDINAL_POSITION       AS ordinal_position
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
    ORDER BY ORDINAL_POSITION
""".strip()


# ── Internals ──

def _read_nullable(row: dict[str, Any]) -> bool:
    raw = row.get("is_nullable")
    if isinstance(raw, str):
        return raw.strip().upper() == "YES"
    if isinstance(raw, bool):
        return raw
    return True


def _mysql_to_fptype(row: dict[str, Any]) -> tuple[FPType, dict[str, Any], str]:
    """Per-row classifier. Returns ``(FPType, params, native_raw)``."""
    data_type = (row.get("data_type") or "").lower().strip()
    column_type = (row.get("column_type") or "").lower().strip()
    native = column_type.upper() or data_type.upper()

    unsigned = "unsigned" in column_type

    # Integers (with the TINYINT(1) → BOOLEAN special case).
    if data_type == "tinyint":
        # MySQL's display width hides in column_type as `tinyint(1)`.
        m = re.search(r"tinyint\((\d+)\)", column_type)
        if m and int(m.group(1)) == 1:
            return FPType.BOOLEAN, {}, native
        params: dict[str, Any] = {"bits": 8}
        if unsigned:
            params["unsigned"] = True
        return FPType.INTEGER, params, native
    if data_type in {"smallint", "mediumint", "int", "integer", "bigint"}:
        bits = {"smallint": 16, "mediumint": 24, "int": 32, "integer": 32, "bigint": 64}[data_type]
        params = {"bits": bits}
        if unsigned:
            params["unsigned"] = True
        return FPType.INTEGER, params, native

    # Booleans aren't a real type in MySQL — synonym for tinyint(1). Handled above.
    if data_type == "bool" or data_type == "boolean":
        return FPType.BOOLEAN, {}, native

    # Decimals.
    if data_type in {"decimal", "numeric"}:
        params = {}
        p, s = row.get("numeric_precision"), row.get("numeric_scale")
        if p is not None:
            params["precision"] = int(p)
        if s is not None:
            params["scale"] = int(s)
        if unsigned:
            params["unsigned"] = True
        return FPType.DECIMAL, params, native

    # Floats.
    if data_type == "float":
        return FPType.FLOAT, {"bits": 32}, native
    if data_type in {"double", "double precision", "real"}:
        return FPType.FLOAT, {"bits": 64}, native

    # Bit.
    if data_type == "bit":
        # column_type is like `bit(3)` → 3 bits → 1 byte (ceil(3/8))
        m = re.search(r"bit\((\d+)\)", column_type)
        n_bits = int(m.group(1)) if m else 1
        n_bytes = (n_bits + 7) // 8
        return FPType.BINARY, {"length": n_bytes, "subtype": "bit", "bit_length": n_bits}, native

    # Strings.
    if data_type in {"varchar", "char"}:
        params = {}
        if data_type == "char":
            params["fixed"] = True
        n = row.get("character_maximum_length")
        if n is not None:
            params["length"] = int(n)
        return FPType.STRING, params, native

    if data_type in {"tinytext", "text", "mediumtext", "longtext"}:
        return FPType.STRING, {"unbounded": True, "subtype": data_type}, native

    # ENUM / SET — choices live in column_type as `enum('a','b','c')`.
    if data_type == "enum":
        choices = _parse_enum_choices(column_type)
        return FPType.STRING, {"subtype": "enum", "choices": choices}, native
    if data_type == "set":
        choices = _parse_enum_choices(column_type)
        return FPType.STRING, {"subtype": "set", "choices": choices}, native

    # JSON.
    if data_type == "json":
        return FPType.JSON, {}, native

    # Temporals.
    if data_type == "date":
        return FPType.DATE, {}, native

    if data_type == "year":
        return FPType.INTEGER, {"bits": 16, "subtype": "year"}, native

    if data_type == "time":
        params = {"with_timezone": False}
        if (p := row.get("datetime_precision")) is not None:
            params["precision"] = int(p)
        return FPType.TIME, params, native

    if data_type == "datetime":
        params = {"with_timezone": False}
        if (p := row.get("datetime_precision")) is not None:
            params["precision"] = int(p)
        return FPType.TIMESTAMP, params, native

    if data_type == "timestamp":
        # MySQL TIMESTAMP stores in UTC and converts on read using the
        # session timezone. Functionally tz-bearing for the canonical
        # contract — flag with_timezone=True so a tz-naive sink is
        # surfaced as SEMANTIC_LOSSY by cast_safety.
        params = {"with_timezone": True}
        if (p := row.get("datetime_precision")) is not None:
            params["precision"] = int(p)
        return FPType.TIMESTAMP, params, native

    # Binaries.
    if data_type in {"binary", "varbinary"}:
        params = {}
        if data_type == "binary":
            params["fixed"] = True
        n = row.get("character_maximum_length")
        if n is not None:
            params["length"] = int(n)
        return FPType.BINARY, params, native

    if data_type in {"tinyblob", "blob", "mediumblob", "longblob"}:
        return FPType.BINARY, {"unbounded": True, "subtype": data_type}, native

    # Spatial types — keep them as BINARY with a subtype so the operator
    # sees what they actually got.
    if data_type in {
        "geometry", "point", "linestring", "polygon",
        "multipoint", "multilinestring", "multipolygon", "geometrycollection",
    }:
        return FPType.BINARY, {"subtype": data_type}, native

    # Unknown — keep native_raw.
    return FPType.UNKNOWN, {}, native


def _parse_enum_choices(column_type: str) -> list[str]:
    """Parse the choice list out of ``enum('a','b','c')`` / ``set('a','b')``.

    The list is the source of truth for the canonical layer; the Mapping
    tab uses it to render a dropdown for downstream targets.
    """
    m = re.search(r"\((.+)\)$", column_type)
    if not m:
        return []
    inner = m.group(1)
    # MySQL escapes single quotes as ''. Replace them, then split on the
    # quote+comma+quote boundary.
    inner = inner.strip()
    if inner.startswith("'") and inner.endswith("'"):
        inner = inner[1:-1]
    # Now we have: a','b','c
    parts = re.split(r"'\s*,\s*'", inner)
    return [p.replace("''", "'") for p in parts if p]
