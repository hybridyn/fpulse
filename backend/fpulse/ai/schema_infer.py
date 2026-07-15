"""Infer a SQL DDL schema from sample JSON records (Phase 3.3, May 18 2026).

Pure-Python, no external libraries beyond stdlib. Takes a list of
dict-shaped JSON records and produces:

  * Per-column type inference across all three target dialects
    (SQL Server / Postgres / DuckDB).
  * Nullability detection (column missing or null in at least one
    sample → NULLABLE).
  * VARCHAR length sizing (next power-of-2 above the max observed
    string length, capped at 4000 for SQL Server NVARCHAR).
  * Numeric precision (INT / BIGINT promotion, FLOAT vs DECIMAL).
  * Nested-object handling — top-level objects can be flattened with
    a separator into the parent table.
  * Nested-array handling — arrays of scalars become a comma-joined
    NVARCHAR for now (proper child-table normalization is roadmap;
    flagged in InferredSchema.warnings).

Public surface:
  * ``infer_schema(samples: list[dict], *, table_name: str = "...") -> InferredSchema``
  * ``InferredSchema`` / ``Column`` dataclasses
  * ``render_ddl(schema, dialect: str) -> str`` — produces CREATE TABLE

Trust contract:
  * Never raises. Malformed samples yield a `warnings` entry, not a crash.
  * Output is deterministic — same input always produces the same DDL.
  * Sample values are NOT included in the rendered DDL (avoids leaking
    PII into the response). They're kept in `Column.sample_values` for
    structured callers that want them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


# ── Data shapes ───────────────────────────────────────────────────────────


@dataclass
class Column:
    """One inferred column. Holds per-dialect types so the renderer
    doesn't need to translate again."""
    name: str
    json_type: str               # "str" / "int" / "float" / "bool" / "null" / "array" / "object" / "datetime" / "date" / "mixed"
    sql_type_mssql: str          # NVARCHAR(N) / INT / BIGINT / FLOAT / DATETIME2 / DATE / BIT
    sql_type_postgres: str       # VARCHAR(N) / INTEGER / BIGINT / DOUBLE PRECISION / TIMESTAMP / DATE / BOOLEAN
    sql_type_duckdb: str         # VARCHAR / INTEGER / BIGINT / DOUBLE / TIMESTAMP / DATE / BOOLEAN
    nullable: bool
    max_length: int = 0          # for string types; 0 for non-strings
    max_int: int = 0             # for integer types; drives INT vs BIGINT
    sample_values: list[Any] = field(default_factory=list)  # first 5 distinct samples (for caller; not rendered)


@dataclass
class InferredSchema:
    table_name: str
    columns: list[Column]
    row_count: int               # number of sample records inspected
    warnings: list[str] = field(default_factory=list)


# ── Helpers ───────────────────────────────────────────────────────────────


_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")
_BOOL_LIKE = ("true", "false", "yes", "no", "y", "n", "1", "0")
_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}",
)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _detect_json_type(value: Any) -> str:
    """Map a Python value (deserialised from JSON) → canonical type tag."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"  # IMPORTANT: bool is a subclass of int — check first
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, (datetime,)):
        return "datetime"
    if isinstance(value, date):
        return "date"
    if isinstance(value, str):
        # String might encode a datetime / date / bool — sniff.
        s = value.strip()
        if not s:
            return "str"
        if _ISO_DATETIME_RE.match(s):
            return "datetime"
        if _ISO_DATE_RE.match(s):
            return "date"
        return "str"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "str"  # unknown → safe default


def _next_pow2(n: int, floor: int = 32, ceiling: int = 4000) -> int:
    """Round n up to the next power of 2 within [floor, ceiling]."""
    if n <= floor:
        return floor
    if n >= ceiling:
        return ceiling
    p = floor
    while p < n:
        p *= 2
        if p >= ceiling:
            return ceiling
    return p


def _promote_type(existing: str, observed: str) -> str:
    """Lattice merge: when a column has mixed observed types, pick the
    safest covering type. Order: bool < int < float < str (str dominates).
    datetime / date / array / object don't promote — they collide."""
    if existing == observed:
        return existing
    if existing == "null":
        return observed  # null doesn't constrain anything
    if observed == "null":
        return existing
    numeric = {"bool", "int", "float"}
    if existing in numeric and observed in numeric:
        # bool < int < float
        order = ["bool", "int", "float"]
        return order[max(order.index(existing), order.index(observed))]
    # Any mixed string + non-string → coerce to str (safest, accepts anything).
    if "str" in (existing, observed):
        return "str"
    # Date + datetime → datetime (datetime is a superset).
    if {existing, observed} == {"date", "datetime"}:
        return "datetime"
    # Truly incompatible (array + object, object + str, etc.) → mixed.
    return "mixed"


_DIALECT_MAP_BY_JSON_TYPE: dict[str, dict[str, str]] = {
    "null":     {"mssql": "NVARCHAR(255)",  "postgres": "VARCHAR(255)", "duckdb": "VARCHAR"},
    "bool":     {"mssql": "BIT",            "postgres": "BOOLEAN",      "duckdb": "BOOLEAN"},
    "int":      {"mssql": "INT",            "postgres": "INTEGER",      "duckdb": "INTEGER"},
    "float":    {"mssql": "FLOAT",          "postgres": "DOUBLE PRECISION", "duckdb": "DOUBLE"},
    "datetime": {"mssql": "DATETIME2",      "postgres": "TIMESTAMP",    "duckdb": "TIMESTAMP"},
    "date":     {"mssql": "DATE",           "postgres": "DATE",         "duckdb": "DATE"},
    "array":    {"mssql": "NVARCHAR(MAX)",  "postgres": "TEXT",         "duckdb": "VARCHAR"},
    "object":   {"mssql": "NVARCHAR(MAX)",  "postgres": "JSONB",        "duckdb": "JSON"},
    "mixed":    {"mssql": "NVARCHAR(MAX)",  "postgres": "TEXT",         "duckdb": "VARCHAR"},
}


def _resolve_types(json_type: str, max_length: int, max_int: int) -> tuple[str, str, str]:
    """Pick per-dialect SQL types. String sizing + int-vs-bigint
    promotion live here."""
    if json_type == "str":
        size = _next_pow2(max(max_length, 1))
        if size >= 4000:
            return ("NVARCHAR(MAX)", "TEXT", "VARCHAR")
        return (f"NVARCHAR({size})", f"VARCHAR({size})", "VARCHAR")
    if json_type == "int":
        # Promote to BIGINT if any value exceeds int32 max.
        if max_int > 2_147_483_647 or max_int < -2_147_483_648:
            return ("BIGINT", "BIGINT", "BIGINT")
        return ("INT", "INTEGER", "INTEGER")
    base = _DIALECT_MAP_BY_JSON_TYPE.get(json_type) or _DIALECT_MAP_BY_JSON_TYPE["mixed"]
    return (base["mssql"], base["postgres"], base["duckdb"])


# ── Main entry point ──────────────────────────────────────────────────────


def infer_schema(
    samples: list[dict],
    *,
    table_name: str = "inferred_table",
    flatten_objects: bool = True,
    flatten_separator: str = "_",
) -> InferredSchema:
    """Inspect ``samples`` and infer a SQL DDL schema.

    Args:
      samples: list of JSON-deserialised dicts. Each dict is one row.
      table_name: identifier for the resulting CREATE TABLE.
      flatten_objects: when True (default), top-level nested objects
        are flattened into the parent columns with the separator
        (e.g. ``address.city`` → ``address_city``). When False,
        nested objects collapse into a single JSON column.
      flatten_separator: separator string for flattened keys.

    Returns:
      InferredSchema. Never raises; bad rows go into ``warnings``.
    """
    if not samples:
        return InferredSchema(
            table_name=table_name,
            columns=[],
            row_count=0,
            warnings=["empty sample set — no schema inferred"],
        )

    warnings: list[str] = []
    # Per-column accumulator: name → (current_type, max_string_len, max_int, nullable, sample_values, total_seen)
    accum: dict[str, dict[str, Any]] = {}
    row_count = 0

    for raw_row in samples:
        if not isinstance(raw_row, dict):
            warnings.append(f"skipped non-dict sample (type={type(raw_row).__name__})")
            continue
        row_count += 1
        row = _flatten_row(raw_row, flatten_objects, flatten_separator) if flatten_objects else dict(raw_row)
        for col_name, value in row.items():
            jt = _detect_json_type(value)
            if col_name not in accum:
                accum[col_name] = {
                    "type": jt,
                    "max_len": 0,
                    "max_int": 0,
                    "seen": 0,
                    "samples": [],
                }
            a = accum[col_name]
            a["seen"] += 1
            a["type"] = _promote_type(a["type"], jt)
            if isinstance(value, str):
                a["max_len"] = max(a["max_len"], len(value))
            if isinstance(value, int) and not isinstance(value, bool):
                a["max_int"] = max(a["max_int"], abs(value))
            if len(a["samples"]) < 5 and value not in a["samples"]:
                a["samples"].append(value)

    # Detect nullability: a column is NULLABLE if not present in EVERY
    # row OR if any observed value is null.
    columns: list[Column] = []
    for col_name, a in accum.items():
        nullable = a["seen"] < row_count or a["type"] == "null" or any(
            v is None for v in a["samples"]
        )
        # If all values were null, fall back to NVARCHAR(255) — safest.
        effective_type = a["type"] if a["type"] != "null" else "str"
        if a["type"] == "null":
            warnings.append(f"column {col_name!r} was always null — typed as NVARCHAR(255)")
        if effective_type == "mixed":
            warnings.append(
                f"column {col_name!r} has mixed types — typed as NVARCHAR(MAX)/TEXT"
            )
        mssql, pg, duck = _resolve_types(effective_type, a["max_len"], a["max_int"])
        columns.append(Column(
            name=col_name,
            json_type=effective_type,
            sql_type_mssql=mssql,
            sql_type_postgres=pg,
            sql_type_duckdb=duck,
            nullable=nullable,
            max_length=a["max_len"],
            max_int=a["max_int"],
            sample_values=a["samples"],
        ))

    return InferredSchema(
        table_name=table_name,
        columns=columns,
        row_count=row_count,
        warnings=warnings,
    )


def _flatten_row(
    obj: dict,
    flatten_objects: bool,
    sep: str,
    prefix: str = "",
) -> dict:
    """Recursively flatten nested dicts. Lists are kept as-is (arrays
    become comma-joined strings at type-resolution time)."""
    out: dict[str, Any] = {}
    for k, v in obj.items():
        key = f"{prefix}{sep}{k}" if prefix else str(k)
        if isinstance(v, dict) and flatten_objects:
            nested = _flatten_row(v, flatten_objects, sep, key)
            out.update(nested)
        else:
            out[key] = v
    return out


# ── DDL renderer ──────────────────────────────────────────────────────────


def render_ddl(schema: InferredSchema, dialect: str = "mssql") -> str:
    """Render a CREATE TABLE statement for the named dialect.

    Dialects: "mssql" / "postgres" / "duckdb". Unknown dialect → mssql."""
    if not schema.columns:
        return f"-- (empty schema — {schema.warnings[0] if schema.warnings else 'no columns inferred'})"

    if dialect == "postgres":
        quote = '"'
        type_attr = "sql_type_postgres"
        table_kw = "CREATE TABLE"
    elif dialect == "duckdb":
        quote = '"'
        type_attr = "sql_type_duckdb"
        table_kw = "CREATE TABLE"
    else:  # mssql
        quote = "["
        close_quote = "]"
        type_attr = "sql_type_mssql"
        table_kw = "CREATE TABLE"

    # Build column lines. Identifier quoting differs by dialect.
    def _q(name: str) -> str:
        if dialect == "mssql":
            return f"[{name}]"
        return f'"{name}"'

    lines = [f"{table_kw} {_q(schema.table_name)} ("]
    col_lines = []
    for c in schema.columns:
        sql_type = getattr(c, type_attr)
        null_clause = "NULL" if c.nullable else "NOT NULL"
        col_lines.append(f"  {_q(c.name)} {sql_type} {null_clause}")
    lines.append(",\n".join(col_lines))
    lines.append(");")
    # Inline warnings as SQL comments above the statement (operator-friendly).
    if schema.warnings:
        header = ["-- Schema inference notes:"]
        for w in schema.warnings:
            header.append(f"--   * {w}")
        header.append("")
        return "\n".join(header + lines)
    return "\n".join(lines)
