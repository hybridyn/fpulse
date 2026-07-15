"""Connector-specific schema type normalization (Roadmap Item 3, 2026-05-27).

WHY THIS MODULE EXISTS
======================

The schema-evolution policy engine in ``schema_policy.py`` compares an
existing destination column's type string against the incoming column's
type string using a lightly normalised string equality (``_norm``).
That works fine WITHIN one dialect — DuckDB ``VARCHAR`` is the same as
DuckDB ``TEXT`` and we map them through ``_TYPE_ALIASES``.

It falls over the moment ``existing`` and ``incoming`` come from
DIFFERENT dialects, which is the common warehouse-loader case:

  * Oracle source emits ``NUMBER(18,2)`` / ``VARCHAR2(255)`` / ``DATE``
    (where Oracle ``DATE`` actually carries a time component)
  * SQL Server source emits ``DECIMAL(18,2)`` / ``NVARCHAR(255)`` / ``DATETIME2``
  * PostgreSQL emits ``NUMERIC(18,2)`` / ``VARCHAR(255)`` / ``TIMESTAMP``
  * MySQL emits ``DECIMAL(18,2)`` / ``VARCHAR(255)`` / ``DATETIME``
  * DuckDB destination stores ``DECIMAL(18,2)`` / ``VARCHAR`` / ``TIMESTAMP``

Under pure string equality, a PostgreSQL ``NUMERIC(18,2)`` arriving at a
DuckDB destination already holding ``DECIMAL(18,2)`` looks like type
drift and trips the policy — even though the values are bit-identical
and the warehouse would accept them without coercion.

The fix is a CANONICAL TYPE VOCABULARY that every dialect maps into,
plus a compatibility predicate that knows widening rules in the
canonical space. The policy engine compares CANONICAL forms; drift is
only reported when the canonical forms actually differ in a meaningful
way.

This module is pure (no DB, no I/O, no module-level mutable state) so
the policy engine, the API drift-preview endpoint, and the unit tests
can call it without spinning anything up.

CANONICAL VOCABULARY
====================

  * ``int8``, ``int16``, ``int32``, ``int64``
  * ``float32``, ``float64``
  * ``decimal(p,s)`` — precision + scale; default ``decimal(38,9)``
  * ``string``, ``string(n)`` — varchar with optional length
  * ``text`` — unbounded
  * ``bool``
  * ``date``, ``timestamp``, ``timestamp_tz``
  * ``binary``, ``binary(n)``
  * ``json``
  * ``array<T>``, ``struct<...>``
  * ``unknown`` — last-resort fallback so callers never crash

The strings above are PUBLIC API — the UI may eventually surface them
in a "translated type" tooltip column on the Mapping tab. Keep them
lowercase, hyphen-free, and decorated with simple ``(n)`` or ``(p,s)``
parens. No spaces inside parens.
"""

from __future__ import annotations

import re
from typing import Callable

# ── Canonical type constants ──────────────────────────────────────────
#
# Constants rather than a string Enum because the canonical strings
# travel through JSON (drift events, API responses) and string
# comparison in callers should not need ``.value``.

CT_INT8 = "int8"
CT_INT16 = "int16"
CT_INT32 = "int32"
CT_INT64 = "int64"
CT_FLOAT32 = "float32"
CT_FLOAT64 = "float64"
CT_DECIMAL = "decimal"          # parameterised: decimal(p,s)
CT_STRING = "string"            # parameterised: string(n) or bare string
CT_TEXT = "text"                # unbounded
CT_BOOL = "bool"
CT_DATE = "date"
CT_TIMESTAMP = "timestamp"
CT_TIMESTAMP_TZ = "timestamp_tz"
CT_BINARY = "binary"            # parameterised: binary(n) or bare binary
CT_JSON = "json"
CT_ARRAY = "array"              # parameterised: array<T>
CT_STRUCT = "struct"            # parameterised: struct<...>
CT_UNKNOWN = "unknown"

# Integer ranking — used by ``types_compatible`` to allow widening only.
# A larger rank can hold the values of a smaller rank.
_INT_RANK: dict[str, int] = {
    CT_INT8: 1,
    CT_INT16: 2,
    CT_INT32: 3,
    CT_INT64: 4,
}

# Float ranking — float32 → float64 is widening; the reverse narrows.
_FLOAT_RANK: dict[str, int] = {
    CT_FLOAT32: 1,
    CT_FLOAT64: 2,
}


# ── Dialect aliasing ──────────────────────────────────────────────────
#
# Connector code in the wild spells the same engine multiple ways
# (``postgres`` vs ``postgresql``, ``mssql`` vs ``sqlserver``,
# ``mariadb`` vs ``mysql``). Normalise everything to the canonical
# spelling used by the per-dialect maps below.

_DIALECT_ALIASES: dict[str, str] = {
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "pg": "postgresql",
    "redshift": "postgresql",          # wire-compatible enough for type names
    "cockroachdb": "postgresql",
    "mssql": "sqlserver",
    "sqlserver": "sqlserver",
    "synapse": "sqlserver",
    "mysql": "mysql",
    "mariadb": "mysql",
    "oracle": "oracle",
    "oracle_db": "oracle",
    "duckdb": "duckdb",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "bq": "bigquery",
}


def _normalise_dialect(dialect: str | None) -> str:
    """Lowercase + alias a dialect name. Empty / unknown → ''."""
    if not dialect:
        return ""
    return _DIALECT_ALIASES.get(dialect.lower().strip(), dialect.lower().strip())


# ── Paren parsing ─────────────────────────────────────────────────────
#
# Most dialect type strings carry width / precision in parens, e.g.
# ``NUMERIC(18,2)``, ``VARCHAR2(255)``, ``DECIMAL(10, 2)``. Tolerate
# whitespace inside the parens and case-fold the base.

_PARENS_RE = re.compile(r"^\s*([A-Za-z0-9_ ]+?)\s*\(([^)]*)\)\s*$")


def _split_type(raw: str) -> tuple[str, list[str]]:
    """Split ``NUMERIC(18,2)`` → ('numeric', ['18', '2'])."""
    if not raw:
        return "", []
    m = _PARENS_RE.match(raw)
    if not m:
        return raw.strip().lower(), []
    base = m.group(1).strip().lower()
    args_raw = m.group(2).strip()
    if not args_raw:
        return base, []
    args = [a.strip() for a in args_raw.split(",") if a.strip()]
    return base, args


def _format_decimal(args: list[str], default_p: int = 38, default_s: int = 9) -> str:
    """Format a canonical decimal(p,s) string with sensible defaults."""
    if len(args) >= 2:
        try:
            p = int(args[0])
            s = int(args[1])
            return f"{CT_DECIMAL}({p},{s})"
        except ValueError:
            pass
    if len(args) == 1:
        try:
            p = int(args[0])
            return f"{CT_DECIMAL}({p},0)"
        except ValueError:
            pass
    return f"{CT_DECIMAL}({default_p},{default_s})"


def _format_string(args: list[str]) -> str:
    """Format string(n) or bare string when no length info."""
    if args:
        try:
            n = int(args[0])
            return f"{CT_STRING}({n})"
        except ValueError:
            pass
    return CT_STRING


def _format_binary(args: list[str]) -> str:
    if args:
        try:
            n = int(args[0])
            return f"{CT_BINARY}({n})"
        except ValueError:
            pass
    return CT_BINARY


# ── Per-dialect canonicalisers ────────────────────────────────────────
#
# Each function takes the (already lowercased) base + args list and
# returns a canonical type string. Unknown bases → CT_UNKNOWN.
#
# A function-per-dialect lets us encode dialect quirks like Oracle's
# DATE-is-actually-datetime and SQL Server's NVARCHAR-vs-VARCHAR
# distinction without an explosion of giant dict literals.


def _canonical_oracle(base: str, args: list[str]) -> str:
    # Oracle has a small, idiosyncratic type set.
    if base in ("number", "numeric"):
        # NUMBER with no precision is unbounded — map to decimal(38,9).
        # NUMBER(p) with no scale is an integer with precision p.
        if len(args) == 1:
            try:
                p = int(args[0])
                if p <= 2:
                    return CT_INT8
                if p <= 4:
                    return CT_INT16
                if p <= 9:
                    return CT_INT32
                if p <= 18:
                    return CT_INT64
                return _format_decimal(args)
            except ValueError:
                return _format_decimal(args)
        return _format_decimal(args)
    if base in ("integer", "int", "smallint"):
        return CT_INT32 if base != "smallint" else CT_INT16
    if base in ("float", "binary_float"):
        return CT_FLOAT32
    if base in ("binary_double",):
        return CT_FLOAT64
    if base in ("char", "nchar", "varchar", "varchar2", "nvarchar2"):
        return _format_string(args)
    if base in ("clob", "nclob", "long"):
        return CT_TEXT
    if base == "date":
        # Oracle DATE includes a time component — really a timestamp.
        return CT_TIMESTAMP
    if base.startswith("timestamp"):
        # "timestamp", "timestamp with time zone", "timestamp with local time zone"
        if "time zone" in base or "tz" in base:
            return CT_TIMESTAMP_TZ
        return CT_TIMESTAMP
    if base in ("raw", "long raw", "blob", "bfile"):
        return _format_binary(args) if args else CT_BINARY
    if base == "boolean":
        return CT_BOOL
    if base in ("json",):
        return CT_JSON
    return CT_UNKNOWN


def _canonical_sqlserver(base: str, args: list[str]) -> str:
    if base == "tinyint":
        return CT_INT8
    if base == "smallint":
        return CT_INT16
    if base in ("int", "integer"):
        return CT_INT32
    if base == "bigint":
        return CT_INT64
    if base == "bit":
        return CT_BOOL
    if base in ("real",):
        return CT_FLOAT32
    if base in ("float",):
        # SQL Server FLOAT(n) — n<=24 is float32, else float64.
        if args:
            try:
                n = int(args[0])
                return CT_FLOAT32 if n <= 24 else CT_FLOAT64
            except ValueError:
                pass
        return CT_FLOAT64
    if base in ("decimal", "numeric", "money", "smallmoney"):
        return _format_decimal(args)
    if base in ("char", "varchar", "nchar", "nvarchar"):
        return _format_string(args)
    if base in ("text", "ntext", "xml"):
        return CT_TEXT
    if base == "date":
        return CT_DATE
    if base in ("datetime", "datetime2", "smalldatetime"):
        return CT_TIMESTAMP
    if base == "datetimeoffset":
        return CT_TIMESTAMP_TZ
    if base in ("binary", "varbinary", "image"):
        return _format_binary(args)
    if base in ("uniqueidentifier",):
        return _format_string([str(36)])
    if base in ("json",):
        return CT_JSON
    return CT_UNKNOWN


def _canonical_postgresql(base: str, args: list[str]) -> str:
    if base in ("smallint", "int2"):
        return CT_INT16
    if base in ("integer", "int", "int4"):
        return CT_INT32
    if base in ("bigint", "int8"):
        return CT_INT64
    if base in ("boolean", "bool"):
        return CT_BOOL
    if base == "real":
        return CT_FLOAT32
    if base in ("double precision", "float8"):
        return CT_FLOAT64
    if base in ("numeric", "decimal", "money"):
        return _format_decimal(args)
    if base in ("character", "char", "varchar", "character varying"):
        return _format_string(args)
    if base in ("text", "citext"):
        return CT_TEXT
    if base == "date":
        return CT_DATE
    if base in ("timestamp", "timestamp without time zone"):
        return CT_TIMESTAMP
    if base in ("timestamptz", "timestamp with time zone"):
        return CT_TIMESTAMP_TZ
    if base in ("bytea",):
        return CT_BINARY
    if base in ("json", "jsonb"):
        return CT_JSON
    if base in ("uuid",):
        return _format_string([str(36)])
    return CT_UNKNOWN


def _canonical_mysql(base: str, args: list[str]) -> str:
    if base == "tinyint":
        # MySQL TINYINT(1) is the bool convention.
        if args and args[0] == "1":
            return CT_BOOL
        return CT_INT8
    if base == "smallint":
        return CT_INT16
    if base in ("mediumint", "int", "integer"):
        return CT_INT32
    if base == "bigint":
        return CT_INT64
    if base in ("bool", "boolean"):
        return CT_BOOL
    if base == "float":
        return CT_FLOAT32
    if base in ("double", "double precision", "real"):
        return CT_FLOAT64
    if base in ("decimal", "numeric"):
        return _format_decimal(args)
    if base in ("char", "varchar"):
        return _format_string(args)
    if base in ("text", "tinytext", "mediumtext", "longtext"):
        return CT_TEXT
    if base == "date":
        return CT_DATE
    if base in ("datetime",):
        return CT_TIMESTAMP
    if base in ("timestamp",):
        # MySQL TIMESTAMP is stored in UTC and converted on read — TZ-aware.
        return CT_TIMESTAMP_TZ
    if base in ("binary", "varbinary"):
        return _format_binary(args)
    if base in ("blob", "tinyblob", "mediumblob", "longblob"):
        return CT_BINARY
    if base in ("json",):
        return CT_JSON
    if base in ("enum", "set"):
        return CT_STRING
    return CT_UNKNOWN


def _canonical_duckdb(base: str, args: list[str]) -> str:
    if base in ("tinyint", "int1"):
        return CT_INT8
    if base in ("smallint", "int2", "short"):
        return CT_INT16
    if base in ("integer", "int", "int4", "signed"):
        return CT_INT32
    if base in ("bigint", "int8", "long"):
        return CT_INT64
    if base in ("hugeint",):
        return CT_INT64                       # closest cross-dialect canonical
    if base in ("boolean", "bool", "logical"):
        return CT_BOOL
    if base in ("real", "float4"):
        return CT_FLOAT32
    if base in ("double", "float8", "float"):
        return CT_FLOAT64
    if base in ("decimal", "numeric"):
        return _format_decimal(args)
    if base in ("varchar", "string", "char", "bpchar"):
        return _format_string(args)
    if base in ("text",):
        return CT_TEXT
    if base in ("date",):
        return CT_DATE
    if base in ("timestamp", "datetime"):
        return CT_TIMESTAMP
    if base in ("timestamptz", "timestamp with time zone"):
        return CT_TIMESTAMP_TZ
    if base in ("blob", "bytea", "binary", "varbinary"):
        return _format_binary(args) if args else CT_BINARY
    if base in ("json",):
        return CT_JSON
    if base in ("uuid",):
        return _format_string([str(36)])
    return CT_UNKNOWN


def _canonical_snowflake(base: str, args: list[str]) -> str:
    if base in ("number", "decimal", "numeric"):
        # Snowflake NUMBER w/o scale is integer-typed but stored as decimal.
        return _format_decimal(args)
    if base in ("int", "integer", "bigint", "smallint", "tinyint", "byteint"):
        # All map to NUMBER(38,0) underneath. Keep the explicit rank for
        # widening checks.
        if base == "tinyint" or base == "byteint":
            return CT_INT8
        if base == "smallint":
            return CT_INT16
        if base in ("int", "integer"):
            return CT_INT32
        return CT_INT64
    if base in ("float", "float4", "float8", "double", "double precision", "real"):
        # Snowflake's FLOAT family is all 64-bit.
        return CT_FLOAT64
    if base in ("boolean",):
        return CT_BOOL
    if base in ("varchar", "string", "text", "char", "character"):
        return _format_string(args) if args else CT_STRING
    if base in ("binary", "varbinary"):
        return _format_binary(args)
    if base == "date":
        return CT_DATE
    if base in ("datetime", "timestamp", "timestamp_ntz"):
        return CT_TIMESTAMP
    if base in ("timestamp_ltz", "timestamp_tz"):
        return CT_TIMESTAMP_TZ
    if base in ("variant", "object", "array"):
        return CT_JSON
    return CT_UNKNOWN


def _canonical_bigquery(base: str, args: list[str]) -> str:
    if base in ("int64", "integer", "int", "smallint", "bigint", "tinyint", "byteint"):
        return CT_INT64
    if base in ("float64", "float"):
        return CT_FLOAT64
    if base in ("numeric", "decimal"):
        # BQ NUMERIC is fixed precision 38, scale 9.
        return _format_decimal(args or ["38", "9"])
    if base in ("bignumeric",):
        return _format_decimal(args or ["76", "38"])
    if base in ("bool", "boolean"):
        return CT_BOOL
    if base in ("string",):
        return _format_string(args) if args else CT_STRING
    if base in ("bytes",):
        return _format_binary(args)
    if base == "date":
        return CT_DATE
    if base in ("datetime",):
        return CT_TIMESTAMP
    if base in ("timestamp",):
        return CT_TIMESTAMP_TZ
    if base in ("json",):
        return CT_JSON
    if base in ("geography", "interval"):
        return CT_STRING
    return CT_UNKNOWN


# Dispatch table — keyed on the normalised dialect.
_DIALECT_DISPATCH: dict[str, Callable[[str, list[str]], str]] = {
    "oracle": _canonical_oracle,
    "sqlserver": _canonical_sqlserver,
    "postgresql": _canonical_postgresql,
    "mysql": _canonical_mysql,
    "duckdb": _canonical_duckdb,
    "snowflake": _canonical_snowflake,
    "bigquery": _canonical_bigquery,
}


# ── Public API ────────────────────────────────────────────────────────


def canonicalize_type(dialect: str, raw_type: str) -> str:
    """Return a canonical type string for cross-dialect comparison.

    Args:
        dialect: connector type string — ``postgresql``, ``mssql``,
            ``oracle``, ``mysql``, ``duckdb``, ``snowflake``, ``bigquery``,
            or any alias listed in ``_DIALECT_ALIASES``.
        raw_type: the dialect's native type string, optionally with
            parens for precision / scale / length. Whitespace and case
            don't matter.

    Returns:
        A canonical string from the vocabulary in the module docstring.
        Unknown dialect → ``"unknown"``. Unknown type → ``"unknown"``.
        Never raises.
    """
    if raw_type is None or not str(raw_type).strip():
        return CT_UNKNOWN
    dialect_norm = _normalise_dialect(dialect)
    handler = _DIALECT_DISPATCH.get(dialect_norm)
    if handler is None:
        return CT_UNKNOWN
    base, args = _split_type(str(raw_type))
    if not base:
        return CT_UNKNOWN
    return handler(base, args)


def _parse_decimal_params(canonical: str) -> tuple[int, int] | None:
    """Extract (precision, scale) from a canonical ``decimal(p,s)`` string."""
    if not canonical.startswith(CT_DECIMAL + "("):
        return None
    inside = canonical[len(CT_DECIMAL) + 1 : -1]
    parts = inside.split(",")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _parse_string_length(canonical: str) -> int | None:
    """Extract ``n`` from a canonical ``string(n)`` string, or None for bare."""
    if canonical == CT_STRING:
        return None
    if not canonical.startswith(CT_STRING + "("):
        return None
    inside = canonical[len(CT_STRING) + 1 : -1]
    try:
        return int(inside)
    except ValueError:
        return None


def _is_int(canonical: str) -> bool:
    return canonical in _INT_RANK


def _is_float(canonical: str) -> bool:
    return canonical in _FLOAT_RANK


def _is_decimal(canonical: str) -> bool:
    return canonical == CT_DECIMAL or canonical.startswith(CT_DECIMAL + "(")


def _is_string(canonical: str) -> bool:
    return canonical == CT_STRING or canonical.startswith(CT_STRING + "(")


def _canonical_kind(canonical: str) -> str:
    """Strip parens, return the bare canonical kind: 'decimal', 'string', etc."""
    if "(" in canonical:
        return canonical.split("(", 1)[0]
    return canonical


def types_compatible(
    dialect_a: str,
    type_a: str,
    dialect_b: str,
    type_b: str,
) -> bool:
    """True if a column of (dialect_a, type_a) can safely hold values of (dialect_b, type_b).

    The compatibility predicate canonicalises both sides first, then
    applies widening rules in the canonical space:

      * Same canonical type → True
      * Wider numeric → True (``int32`` → ``int64``,
        ``decimal(10,2)`` → ``decimal(18,2)``; reverse → False)
      * ``int*`` → ``decimal(p,s)`` for s ≥ 0 → True
      * ``int*`` / ``decimal`` → ``float64`` → True (lossy but accepted
        by all warehouses we support)
      * ``string(n)`` → ``string(m)`` for m ≥ n → True; → ``text`` always True
      * ``date`` → ``timestamp`` → True; reverse → False
      * ``timestamp`` → ``timestamp_tz`` → True (assumes UTC); reverse → False
      * All others → False (caller treats as drift)

    "A holds B" means ``a`` is the EXISTING destination type and ``b``
    is the incoming source type. The destination is wider; the source
    is narrower (or equal). This matches how ``evaluate_policy`` is
    called: ``types_compatible(dest, src)``.

    Unknown dialect on EITHER side → False, so the caller falls back to
    the existing string-equality check. That preserves the pre-2026-05-27
    behaviour for dialects we haven't taught the table about.
    """
    dialect_a_n = _normalise_dialect(dialect_a)
    dialect_b_n = _normalise_dialect(dialect_b)
    if not dialect_a_n or not dialect_b_n:
        return False
    if dialect_a_n not in _DIALECT_DISPATCH or dialect_b_n not in _DIALECT_DISPATCH:
        return False

    ca = canonicalize_type(dialect_a, type_a)
    cb = canonicalize_type(dialect_b, type_b)

    if ca == CT_UNKNOWN or cb == CT_UNKNOWN:
        return False

    # Identical canonicalisation — trivially compatible.
    if ca == cb:
        return True

    kind_a = _canonical_kind(ca)
    kind_b = _canonical_kind(cb)

    # ── Numeric family ────────────────────────────────────────────
    # int → int widening
    if _is_int(ca) and _is_int(cb):
        return _INT_RANK[ca] >= _INT_RANK[cb]

    # int → decimal: holds any int as long as scale >= 0
    if _is_decimal(ca) and _is_int(cb):
        params = _parse_decimal_params(ca)
        if params is None:
            return True  # bare decimal == decimal(38,9) which holds any int
        p, s = params
        # An int needs (digits + scale) precision. We accept any p, s>=0
        # because callers want the "is this an unsafe change" signal,
        # not "did we pick the perfect precision".
        return s >= 0

    # decimal → decimal widening (precision >= and scale >=)
    if _is_decimal(ca) and _is_decimal(cb):
        pa = _parse_decimal_params(ca) or (38, 9)
        pb = _parse_decimal_params(cb) or (38, 9)
        return pa[0] >= pb[0] and pa[1] >= pb[1]

    # int or decimal → float64: widening to imprecise but warehouse-accepted
    if ca == CT_FLOAT64 and (_is_int(cb) or _is_decimal(cb)):
        return True

    # float widening
    if _is_float(ca) and _is_float(cb):
        return _FLOAT_RANK[ca] >= _FLOAT_RANK[cb]

    # int → float64 the other direction (cb already handled above)

    # ── String family ────────────────────────────────────────────
    # text holds any string
    if ca == CT_TEXT and (_is_string(cb) or cb == CT_TEXT):
        return True

    # string(m) holds string(n) when m >= n; bare string holds any string(n)
    if _is_string(ca) and _is_string(cb):
        la = _parse_string_length(ca)
        lb = _parse_string_length(cb)
        if la is None:
            return True            # bare string == unbounded varchar
        if lb is None:
            return False           # narrower destination, unbounded source
        return la >= lb

    # ── Temporal family ──────────────────────────────────────────
    # date → timestamp (destination is timestamp, holds a date)
    if ca == CT_TIMESTAMP and cb == CT_DATE:
        return True
    # timestamp → timestamp_tz (destination is TZ-aware, holds naive as UTC)
    if ca == CT_TIMESTAMP_TZ and cb == CT_TIMESTAMP:
        return True

    # ── Binary family ────────────────────────────────────────────
    if kind_a == CT_BINARY and kind_b == CT_BINARY:
        # bare binary is unbounded → holds any binary(n)
        if ca == CT_BINARY:
            return True
        if cb == CT_BINARY:
            return False
        # binary(m) holds binary(n) when m >= n
        try:
            la = int(ca[len(CT_BINARY) + 1 : -1])
            lb = int(cb[len(CT_BINARY) + 1 : -1])
            return la >= lb
        except ValueError:
            return False

    return False


__all__ = [
    # canonical constants
    "CT_INT8", "CT_INT16", "CT_INT32", "CT_INT64",
    "CT_FLOAT32", "CT_FLOAT64",
    "CT_DECIMAL", "CT_STRING", "CT_TEXT", "CT_BOOL",
    "CT_DATE", "CT_TIMESTAMP", "CT_TIMESTAMP_TZ",
    "CT_BINARY", "CT_JSON", "CT_ARRAY", "CT_STRUCT", "CT_UNKNOWN",
    # public functions
    "canonicalize_type",
    "types_compatible",
]
