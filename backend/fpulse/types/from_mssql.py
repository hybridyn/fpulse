"""SQL Server → CanonicalSchema.

Reads ``sys.columns``-shape rows (see ``CANONICAL_COLUMN_QUERY_MSSQL``)
and returns a ``CanonicalSchema`` the runtime can compare against the
locked snapshot, drive the Mapping tab UI, and feed the sink-side
``to_mssql`` writer.

Mapping rules — conservative, matching the SQL Server type system:

  - tinyint / smallint / int / bigint     → INTEGER(bits=8/16/32/64)
  - decimal(p,s) / numeric(p,s)           → DECIMAL(precision, scale)
  - money / smallmoney                    → DECIMAL(19,4) / DECIMAL(10,4)
  - real / float                          → FLOAT(bits=32 / 53)
  - varchar(n) / nvarchar(n)              → STRING(length=n, unicode?)
  - varchar(MAX) / nvarchar(MAX)          → STRING (unbounded, unicode?)
  - char(n) / nchar(n)                    → STRING(length=n, fixed, unicode?)
  - text / ntext                          → STRING (unbounded)
  - bit                                   → BOOLEAN
  - date                                  → DATE
  - time(p)                               → TIME(precision, with_timezone=False)
  - datetime / datetime2(p)               → TIMESTAMP(precision, with_timezone=False)
  - smalldatetime                         → TIMESTAMP(precision=0)
  - datetimeoffset(p)                     → TIMESTAMP(precision, with_timezone=True)
  - binary(n) / varbinary(n)              → BINARY(length=n)
  - varbinary(MAX) / image                → BINARY (unbounded)
  - uniqueidentifier                      → STRING(length=36, subtype="uuid")
  - xml                                   → STRING(subtype="xml")
  - rowversion / timestamp                → BINARY(length=8, subtype="rowversion")
  - sql_variant                           → UNKNOWN (intentionally — sql_variant
                                            is column-of-anything; the operator
                                            must pin a type via the Mapping tab)
  - everything else                       → UNKNOWN with native_raw preserved

Every emitted ``FPField`` records ``evidence=ADVERTISED``. The
``with_timezone`` param key matches what ``cast_safety.py`` reads (see
the 2026-05-22 fix that aligned the key contract).
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

def mssql_columns_to_canonical(rows: list[dict[str, Any]]) -> CanonicalSchema:
    """Convert ``sys.columns``-shape rows → ``CanonicalSchema``.

    Each row carries the keys produced by ``CANONICAL_COLUMN_QUERY_MSSQL``:
    ``column_name``, ``data_type``, ``max_length``, ``precision``,
    ``scale``, ``is_nullable``, ``is_identity``, ``is_computed``,
    ``collation_name``, ``default_definition``, ``column_id``. Missing
    keys are tolerated so callers can pass partial dicts from tests.
    """
    fields: list[FPField] = []
    for row in rows:
        name = row.get("column_name") or row.get("name")
        if not name:
            continue
        fp_type, params, native_raw = _mssql_to_fptype(row)
        nullable = _read_nullable(row)
        # Per-column DBA-facing extras (identity / computed / collation /
        # default) ride in `params["extra"]` so the Mapping tab can show
        # them without polluting the FPType comparison key.
        extra = _extra_metadata(row)
        if extra:
            params = {**params, "extra": extra}
        fields.append(FPField(
            name=name,
            type=fp_type,
            nullable=nullable,
            params=params,
            evidence=Evidence.ADVERTISED,
            confidence=1.0,
            provenance=[Provenance(
                source=f"SQL Server {native_raw}",
                confidence=1.0,
                sample_size=0,
            )],
            native_raw=native_raw,
        ))
    return CanonicalSchema(fields=fields)


# Canonical introspection query. Uses sys.* views (not INFORMATION_SCHEMA)
# because INFORMATION_SCHEMA in SQL Server omits ``is_identity``,
# ``is_computed``, and treats varchar(MAX) inconsistently. Bind params:
#   :0 → schema name (e.g. "dbo")
#   :1 → table name
CANONICAL_COLUMN_QUERY_MSSQL = """
    SELECT
        c.name              AS column_name,
        ty.name             AS data_type,
        c.max_length        AS max_length,
        c.precision         AS precision,
        c.scale             AS scale,
        c.is_nullable       AS is_nullable,
        c.is_identity       AS is_identity,
        c.is_computed       AS is_computed,
        c.collation_name    AS collation_name,
        dc.definition       AS default_definition,
        c.column_id         AS column_id
    FROM sys.columns c
    JOIN sys.tables t  ON c.object_id = t.object_id
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    JOIN sys.types ty  ON c.user_type_id = ty.user_type_id
    LEFT JOIN sys.default_constraints dc
        ON c.default_object_id = dc.object_id
    WHERE s.name = ? AND t.name = ?
    ORDER BY c.column_id
""".strip()


# ── Internals ──

def _read_nullable(row: dict[str, Any]) -> bool:
    raw = row.get("is_nullable")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().upper() not in {"NO", "0", "FALSE"}
    return True


def _extra_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """Pull SQL-Server-specific column attributes out of the row.

    These don't influence FPType but ARE shown in the Mapping tab so the
    operator sees identity / computed / collation / default-constraint
    information at a glance.
    """
    extra: dict[str, Any] = {}
    if row.get("is_identity"):
        extra["identity"] = True
    if row.get("is_computed"):
        extra["computed"] = True
    coll = row.get("collation_name")
    if coll:
        extra["collation"] = str(coll)
    default = row.get("default_definition")
    if default:
        extra["default"] = str(default)
    return extra


def _mssql_to_fptype(row: dict[str, Any]) -> tuple[FPType, dict[str, Any], str]:
    """Per-row classifier. Returns ``(FPType, params, native_raw)``."""
    data_type = (row.get("data_type") or "").lower().strip()
    native = _format_native(row)

    # Integers — width by name. tinyint is unsigned 8-bit in SQL Server.
    if data_type == "tinyint":
        return FPType.INTEGER, {"bits": 8, "unsigned": True}, native
    if data_type == "smallint":
        return FPType.INTEGER, {"bits": 16}, native
    if data_type == "int":
        return FPType.INTEGER, {"bits": 32}, native
    if data_type == "bigint":
        return FPType.INTEGER, {"bits": 64}, native

    # Decimal / numeric.
    if data_type in {"decimal", "numeric"}:
        params: dict[str, Any] = {}
        p, s = row.get("precision"), row.get("scale")
        if p is not None:
            params["precision"] = int(p)
        if s is not None:
            params["scale"] = int(s)
        return FPType.DECIMAL, params, native

    # money / smallmoney — fixed precision per SQL Server docs.
    if data_type == "money":
        return FPType.DECIMAL, {"precision": 19, "scale": 4, "subtype": "money"}, native
    if data_type == "smallmoney":
        return FPType.DECIMAL, {"precision": 10, "scale": 4, "subtype": "money"}, native

    # Floats. SQL Server's float(n) maps to FLOAT(53) when n>24, FLOAT(24) (= real) otherwise.
    if data_type == "real":
        return FPType.FLOAT, {"bits": 32}, native
    if data_type == "float":
        # `precision` here is mantissa bits in sys.columns (24 or 53).
        bits = 64
        p = row.get("precision")
        if p is not None and int(p) <= 24:
            bits = 32
        return FPType.FLOAT, {"bits": bits}, native

    # Strings. SQL Server reports max_length in BYTES — for nvarchar that's
    # 2× the character count, and -1 for MAX. Convert to character length.
    if data_type in {"varchar", "nvarchar", "char", "nchar"}:
        unicode = data_type.startswith("n")
        fixed = data_type in {"char", "nchar"}
        params = {}
        if unicode:
            params["unicode"] = True
        if fixed:
            params["fixed"] = True
        max_length = row.get("max_length")
        if max_length is None or int(max_length) == -1:
            # -1 == MAX
            params["unbounded"] = True
        else:
            char_len = int(max_length) // 2 if unicode else int(max_length)
            params["length"] = char_len
        return FPType.STRING, params, native

    if data_type in {"text", "ntext"}:
        params = {"unbounded": True}
        if data_type == "ntext":
            params["unicode"] = True
        return FPType.STRING, params, native

    if data_type == "uniqueidentifier":
        return FPType.STRING, {"length": 36, "subtype": "uuid"}, native

    if data_type == "xml":
        return FPType.STRING, {"subtype": "xml"}, native

    # Booleans. SQL Server's `bit` is the conventional boolean.
    if data_type == "bit":
        return FPType.BOOLEAN, {}, native

    # Temporals.
    if data_type == "date":
        return FPType.DATE, {}, native

    if data_type == "time":
        params = {"with_timezone": False}
        if (p := row.get("scale")) is not None:
            # sys.columns.scale carries fractional-second precision for time/datetime2.
            params["precision"] = int(p)
        return FPType.TIME, params, native

    if data_type in {"datetime", "smalldatetime"}:
        # smalldatetime has minute resolution; datetime has ~3ms.
        precision = 0 if data_type == "smalldatetime" else 3
        return FPType.TIMESTAMP, {"with_timezone": False, "precision": precision}, native

    if data_type == "datetime2":
        params = {"with_timezone": False}
        if (p := row.get("scale")) is not None:
            params["precision"] = int(p)
        return FPType.TIMESTAMP, params, native

    if data_type == "datetimeoffset":
        params = {"with_timezone": True}
        if (p := row.get("scale")) is not None:
            params["precision"] = int(p)
        return FPType.TIMESTAMP, params, native

    # Binaries.
    if data_type in {"binary", "varbinary"}:
        params = {}
        if data_type == "binary":
            params["fixed"] = True
        max_length = row.get("max_length")
        if max_length is None or int(max_length) == -1:
            params["unbounded"] = True
        else:
            params["length"] = int(max_length)
        return FPType.BINARY, params, native

    if data_type == "image":
        return FPType.BINARY, {"unbounded": True, "legacy": True}, native

    # rowversion / timestamp (SQL Server "timestamp" is NOT a date type —
    # it's an 8-byte row-version. Always 8 bytes, always unique within a DB.
    if data_type in {"rowversion", "timestamp"}:
        return FPType.BINARY, {"length": 8, "subtype": "rowversion"}, native

    # sql_variant is column-of-anything — we deliberately classify as UNKNOWN
    # so the Mapping tab can require the operator to pin a concrete type.
    if data_type == "sql_variant":
        return FPType.UNKNOWN, {"reason": "sql_variant: type varies per row"}, native

    # geography / geometry / hierarchyid / CLR types — keep as UNKNOWN with
    # native_raw so the operator sees what they actually got.
    return FPType.UNKNOWN, {}, native


def _format_native(row: dict[str, Any]) -> str:
    """Reconstruct the human-readable native type string for ``native_raw``.

    Mirrors how a DBA would describe the column: ``NVARCHAR(255)``,
    ``DECIMAL(18,2)``, ``DATETIME2(3)``, ``DATETIMEOFFSET(7)``, etc.
    """
    data_type = (row.get("data_type") or "").upper().strip()
    if not data_type:
        return ""

    p, s = row.get("precision"), row.get("scale")
    if data_type in {"DECIMAL", "NUMERIC"} and p is not None:
        return f"{data_type}({p},{s or 0})"

    max_length = row.get("max_length")
    if data_type in {"VARCHAR", "NVARCHAR", "CHAR", "NCHAR"}:
        if max_length is None or int(max_length) == -1:
            return f"{data_type}(MAX)"
        char_len = int(max_length) // 2 if data_type.startswith("N") else int(max_length)
        return f"{data_type}({char_len})"

    if data_type in {"BINARY", "VARBINARY"}:
        if max_length is None or int(max_length) == -1:
            return f"{data_type}(MAX)"
        return f"{data_type}({max_length})"

    if data_type in {"TIME", "DATETIME2", "DATETIMEOFFSET"} and s is not None:
        return f"{data_type}({s})"

    if data_type == "FLOAT" and p is not None:
        return f"FLOAT({p})"

    return data_type
