"""Postgres → CanonicalSchema.

Reads ``information_schema.columns`` rows (or shape-equivalent dicts)
and returns a ``CanonicalSchema`` the runtime can compare against the
locked snapshot, drive the Mapping tab UI, and feed the sink-side
``to_postgres`` writer.

Mapping rules — kept conservative on purpose, so an FPType assignment
is never wider than what the database actually guarantees:

  - smallint / integer / bigint        → INTEGER(bits=16/32/64)
  - smallserial / serial / bigserial   → INTEGER(bits=16/32/64)
  - numeric(p,s) / decimal(p,s)        → DECIMAL(precision, scale)
  - money                              → DECIMAL(19, 2)  (PG convention)
  - real / double precision            → FLOAT(bits=32 / 64)
  - varchar(n) / character varying(n)  → STRING(length=n)   (no n → unbounded)
  - character(n) / char(n)             → STRING(length=n, fixed=True)
  - text                               → STRING (unbounded)
  - uuid                               → STRING(length=36, subtype="uuid")
  - inet / cidr / macaddr / macaddr8   → STRING(subtype=…)  (network types)
  - boolean / bool                     → BOOLEAN
  - date                               → DATE
  - time / timetz                      → TIME(precision, with_timezone)
  - timestamp / timestamptz            → TIMESTAMP(precision, with_timezone)
  - bytea                              → BINARY
  - json / jsonb                       → JSON(subtype="jsonb"|"json")
  - ARRAY (udt_name like ``_int4``)    → LIST(element_type=<recursive>)
  - everything else                    → UNKNOWN with native_raw preserved
                                         so the operator sees the real type
                                         in the Mapping tab even when we
                                         can't make a confident decision

Every emitted ``FPField`` records ``evidence=ADVERTISED`` and a single
``Provenance`` entry pointing back at the Postgres type name — that
keeps the explainability chain intact through the rest of the
pipeline.
"""

from __future__ import annotations

from typing import Any

from fpulse.types.canonical import (
    CanonicalSchema,
    Evidence,
    FPField,
    FPType,
    Provenance,
)


# ── Public surface ──

def postgres_columns_to_canonical(rows: list[dict[str, Any]]) -> CanonicalSchema:
    """Convert ``information_schema.columns`` rows → ``CanonicalSchema``.

    Each row must carry the keys produced by ``CANONICAL_COLUMN_QUERY``
    below (or its driver-quoted equivalent): ``column_name``,
    ``data_type``, ``udt_name``, ``is_nullable``,
    ``character_maximum_length``, ``numeric_precision``,
    ``numeric_scale``, ``datetime_precision``. Missing keys are tolerated
    so callers can pass in partial dicts from tests.
    """
    fields: list[FPField] = []
    for row in rows:
        name = row.get("column_name") or row.get("name")
        if not name:
            continue
        fp_type, params, native_raw = _pg_to_fptype(row)
        nullable = _read_nullable(row)
        fields.append(FPField(
            name=name,
            type=fp_type,
            nullable=nullable,
            params=params,
            evidence=Evidence.ADVERTISED,
            confidence=1.0,
            provenance=[Provenance(
                source=f"Postgres {native_raw}",
                confidence=1.0,
                sample_size=0,
            )],
            native_raw=native_raw,
        ))
    return CanonicalSchema(fields=fields)


# Reuse-anywhere SQL the introspection function below runs.
# Kept as a module constant so tests can assert the column list and so
# the sink-side writer can compare against the same projection without
# duplicating the SELECT list.
CANONICAL_COLUMN_QUERY = """
    SELECT
        column_name,
        data_type,
        udt_name,
        is_nullable,
        character_maximum_length,
        numeric_precision,
        numeric_scale,
        datetime_precision,
        ordinal_position
    FROM information_schema.columns
    WHERE table_schema = %s AND table_name = %s
    ORDER BY ordinal_position
""".strip()


# ── Internals ──

def _read_nullable(row: dict[str, Any]) -> bool:
    raw = row.get("is_nullable")
    if isinstance(raw, str):
        return raw.strip().upper() != "NO"
    if isinstance(raw, bool):
        return raw
    # Default to nullable — safer to over-permit than to lock the writer
    # into a NOT NULL constraint the source might not actually have.
    return True


def _pg_to_fptype(row: dict[str, Any]) -> tuple[FPType, dict[str, Any], str]:
    """Per-row classifier. Returns ``(FPType, params, native_raw)``."""
    data_type = (row.get("data_type") or "").lower().strip()
    udt = (row.get("udt_name") or "").lower().strip()
    native = _format_native(row)

    # Integers — bits set from the udt_name (int2/int4/int8) so serial
    # and smallserial flow through the same path.
    if data_type in {"smallint", "integer", "bigint"} or udt in {
        "int2", "int4", "int8", "smallserial", "serial", "bigserial",
    }:
        bits = _integer_bits(data_type, udt)
        return FPType.INTEGER, {"bits": bits}, native

    # Decimal / numeric.
    if data_type in {"numeric", "decimal"} or udt == "numeric":
        params: dict[str, Any] = {}
        p, s = row.get("numeric_precision"), row.get("numeric_scale")
        if p is not None:
            params["precision"] = int(p)
        if s is not None:
            params["scale"] = int(s)
        return FPType.DECIMAL, params, native

    if data_type == "money" or udt == "money":
        # PG money is locale-dependent at the wire but always p=19,s=2 logically.
        return FPType.DECIMAL, {"precision": 19, "scale": 2, "subtype": "money"}, native

    # Floats.
    if data_type == "real" or udt == "float4":
        return FPType.FLOAT, {"bits": 32}, native
    if data_type == "double precision" or udt == "float8":
        return FPType.FLOAT, {"bits": 64}, native

    # Strings — branch on length + fixedness.
    if data_type in {"character varying", "varchar"} or udt in {"varchar"}:
        params = {}
        n = row.get("character_maximum_length")
        if n is not None:
            params["length"] = int(n)
        return FPType.STRING, params, native
    if data_type in {"character", "char"} or udt in {"bpchar"}:
        params = {"fixed": True}
        n = row.get("character_maximum_length")
        if n is not None:
            params["length"] = int(n)
        return FPType.STRING, params, native
    if data_type == "text" or udt == "text":
        return FPType.STRING, {}, native
    if udt == "uuid" or data_type == "uuid":
        return FPType.STRING, {"length": 36, "subtype": "uuid"}, native
    if udt in {"inet", "cidr", "macaddr", "macaddr8"}:
        return FPType.STRING, {"subtype": udt}, native

    # Booleans.
    if data_type in {"boolean", "bool"} or udt == "bool":
        return FPType.BOOLEAN, {}, native

    # Temporals.
    if data_type == "date" or udt == "date":
        return FPType.DATE, {}, native
    if data_type in {"time without time zone", "time"} or udt == "time":
        params = {"with_timezone": False}
        if (p := row.get("datetime_precision")) is not None:
            params["precision"] = int(p)
        return FPType.TIME, params, native
    if data_type in {"time with time zone", "timetz"} or udt == "timetz":
        params = {"with_timezone": True}
        if (p := row.get("datetime_precision")) is not None:
            params["precision"] = int(p)
        return FPType.TIME, params, native
    if data_type in {"timestamp without time zone", "timestamp"} or udt == "timestamp":
        params = {"with_timezone": False}
        if (p := row.get("datetime_precision")) is not None:
            params["precision"] = int(p)
        return FPType.TIMESTAMP, params, native
    if data_type in {"timestamp with time zone", "timestamptz"} or udt == "timestamptz":
        params = {"with_timezone": True}
        if (p := row.get("datetime_precision")) is not None:
            params["precision"] = int(p)
        return FPType.TIMESTAMP, params, native

    # Binary.
    if data_type == "bytea" or udt == "bytea":
        return FPType.BINARY, {}, native

    # JSON.
    if data_type == "json" or udt == "json":
        return FPType.JSON, {"subtype": "json"}, native
    if data_type == "jsonb" or udt == "jsonb":
        return FPType.JSON, {"subtype": "jsonb"}, native

    # Arrays — PG advertises the array as data_type="ARRAY" with
    # udt_name like ``_int4``. Strip the leading underscore and recurse
    # with a synthetic row carrying the element's udt as data_type.
    if data_type == "array" and udt.startswith("_"):
        element_udt = udt[1:]
        element_type, element_params, element_native = _pg_to_fptype(
            {"data_type": element_udt, "udt_name": element_udt},
        )
        element = FPField(
            name="(element)",
            type=element_type,
            nullable=True,
            params=element_params,
            evidence=Evidence.ADVERTISED,
            native_raw=element_native,
        )
        return FPType.LIST, {"element_type": element}, native

    # Unknown — preserve the native type so the operator sees what we
    # actually got back, even if we won't bind it to an FPType today.
    return FPType.UNKNOWN, {}, native


def _integer_bits(data_type: str, udt: str) -> int:
    if data_type == "smallint" or udt in {"int2", "smallserial"}:
        return 16
    if data_type == "bigint" or udt in {"int8", "bigserial"}:
        return 64
    return 32  # integer / int4 / serial


def _format_native(row: dict[str, Any]) -> str:
    """Reconstruct a human-readable native type string for ``native_raw``.

    Mirrors how a DBA would describe the column: ``NUMERIC(18,2)``,
    ``VARCHAR(255)``, ``TIMESTAMPTZ(3)``, ``INTEGER[]``. Used in the
    Mapping tab tooltip + the provenance source label so operators
    always know what the source actually advertised.
    """
    data_type = (row.get("data_type") or "").upper().strip()
    udt = (row.get("udt_name") or "").lower().strip()

    # Numeric with precision/scale.
    p, s = row.get("numeric_precision"), row.get("numeric_scale")
    if data_type in {"NUMERIC", "DECIMAL"} and p is not None:
        return f"{data_type}({p},{s or 0})"

    # Strings with length.
    n = row.get("character_maximum_length")
    if data_type in {"CHARACTER VARYING", "VARCHAR"} and n is not None:
        return f"VARCHAR({n})"
    if data_type in {"CHARACTER", "CHAR"} and n is not None:
        return f"CHAR({n})"

    # Timestamps / times with precision + timezone.
    dp = row.get("datetime_precision")
    if data_type == "TIMESTAMP WITH TIME ZONE":
        return f"TIMESTAMPTZ({dp})" if dp is not None else "TIMESTAMPTZ"
    if data_type == "TIMESTAMP WITHOUT TIME ZONE":
        return f"TIMESTAMP({dp})" if dp is not None else "TIMESTAMP"
    if data_type == "TIME WITH TIME ZONE":
        return f"TIMETZ({dp})" if dp is not None else "TIMETZ"
    if data_type == "TIME WITHOUT TIME ZONE":
        return f"TIME({dp})" if dp is not None else "TIME"

    # Arrays.
    if data_type == "ARRAY" and udt.startswith("_"):
        return f"{udt[1:].upper()}[]"

    return data_type or udt.upper()
