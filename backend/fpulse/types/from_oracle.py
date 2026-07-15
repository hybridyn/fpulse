"""Oracle → CanonicalSchema.

The hard problem in Oracle is ``NUMBER`` — it's a single storage type
that the schema author uses for everything from booleans to high-
precision decimals. Treating every ``NUMBER`` as one logical FPType
loses information; treating every ``NUMBER`` separately requires more
context than the column dictionary carries. The disambiguation table
below is the considered middle ground.

NUMBER mapping (the canonical decision table — keep this stable; it's
exercised in ``tests/test_canonical_cross_dialect.py``):

  NUMBER(p, 0), p ≤ 9            → INTEGER(bits=32)
  NUMBER(p, 0), 9 < p ≤ 18       → INTEGER(bits=64)
  NUMBER(p, 0), p > 18           → DECIMAL(precision=p, scale=0)
  NUMBER(p, s), s > 0            → DECIMAL(precision=p, scale=s)
  NUMBER with no p/s declared    → UNKNOWN with native_raw="NUMBER" so
                                   the Mapping tab REQUIRES operator
                                   confirmation before writing into a
                                   target INT or DECIMAL (writing a
                                   bare NUMBER into INT silently
                                   truncates; writing into DECIMAL(38)
                                   over-allocates; only the operator
                                   knows the schema author's intent).

Other types — straightforward:

  FLOAT(b), BINARY_FLOAT, BINARY_DOUBLE → FLOAT
  VARCHAR2(n), NVARCHAR2(n)             → STRING(length=n, unicode?)
  CHAR(n), NCHAR(n)                     → STRING(length=n, fixed, unicode?)
  CLOB, NCLOB                           → STRING(unbounded, unicode?)
  DATE                                  → TIMESTAMP(precision=0) **NOT** DATE.
                                          Oracle's DATE carries time too —
                                          using FPType.DATE would silently
                                          drop the time component.
  TIMESTAMP(p)                          → TIMESTAMP(precision=p, with_timezone=False)
  TIMESTAMP(p) WITH TIME ZONE           → TIMESTAMP(precision=p, with_timezone=True)
  TIMESTAMP(p) WITH LOCAL TIME ZONE     → TIMESTAMP(precision=p,
                                                    with_timezone=True,
                                                    subtype="local")
  RAW(n), LONG RAW                      → BINARY(length=n)
  BLOB                                  → BINARY(unbounded)
  ROWID, UROWID                         → STRING(subtype="rowid")
  XMLTYPE                               → STRING(subtype="xml")
  BOOLEAN (23ai+)                       → BOOLEAN
  everything else                       → UNKNOWN with native_raw

Bounds: Oracle character semantics matter. ``char_used`` returns ``C``
for character semantics and ``B`` for byte semantics. We report
``length`` in characters when ``C``, or the actual byte count when
``B`` (rare, but documented).
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

def oracle_columns_to_canonical(rows: list[dict[str, Any]]) -> CanonicalSchema:
    """Convert ``all_tab_columns``-shape rows → ``CanonicalSchema``.

    Each row carries the keys produced by ``CANONICAL_COLUMN_QUERY_ORACLE``:
    ``column_name``, ``data_type``, ``data_precision``, ``data_scale``,
    ``char_length``, ``char_used``, ``nullable``, ``data_default``,
    ``column_id``. Missing keys are tolerated for tests.
    """
    fields: list[FPField] = []
    for row in rows:
        name = row.get("column_name") or row.get("name")
        if not name:
            continue
        fp_type, params, native_raw = _oracle_to_fptype(row)
        nullable = _read_nullable(row)
        # Default expression goes in params["extra"]["default"] alongside
        # whatever the per-type classifier added.
        default = row.get("data_default")
        if default and isinstance(default, str) and default.strip():
            params = {**params, "extra": {**(params.get("extra") or {}), "default": default.strip()}}
        fields.append(FPField(
            name=name,
            type=fp_type,
            nullable=nullable,
            params=params,
            evidence=Evidence.ADVERTISED,
            confidence=_confidence_for(fp_type, row),
            provenance=[Provenance(
                source=f"Oracle {native_raw}",
                confidence=1.0,
                sample_size=0,
            )],
            native_raw=native_raw,
        ))
    return CanonicalSchema(fields=fields)


# Canonical introspection query. Owner is the schema name in Oracle.
# Bind: :owner, :table_name (uppercased — Oracle stores identifiers
# uppercased by default unless quoted at creation).
CANONICAL_COLUMN_QUERY_ORACLE = """
    SELECT
        column_name,
        data_type,
        data_precision,
        data_scale,
        char_length,
        char_used,
        nullable,
        data_default,
        column_id
    FROM all_tab_columns
    WHERE owner = :owner
      AND table_name = :table_name
    ORDER BY column_id
""".strip()


# ── Internals ──

def _read_nullable(row: dict[str, Any]) -> bool:
    raw = row.get("nullable")
    # Oracle returns 'Y' / 'N' for nullable.
    if isinstance(raw, str):
        return raw.strip().upper() == "Y"
    if isinstance(raw, bool):
        return raw
    return True


def _confidence_for(fp_type: FPType, row: dict[str, Any]) -> float:
    """Lower the confidence when we had to disambiguate.

    NUMBER without precision/scale → UNKNOWN with confidence 0.0; the
    operator must pin a target type. NUMBER(p,0) where p ≤ 9 → INTEGER
    with confidence 0.85 (the disambiguation rule is correct ~85% of
    the time empirically — there's always some schema author who picks
    NUMBER(9,0) for what should be NUMBER(12,0)).
    """
    data_type = (row.get("data_type") or "").upper().strip()
    if fp_type == FPType.UNKNOWN and data_type == "NUMBER":
        return 0.0
    if data_type == "NUMBER":
        return 0.85
    return 1.0


def _oracle_to_fptype(row: dict[str, Any]) -> tuple[FPType, dict[str, Any], str]:
    """Per-row classifier. Returns ``(FPType, params, native_raw)``."""
    data_type = (row.get("data_type") or "").upper().strip()
    native = _format_native(row)

    # NUMBER — the disambiguation table.
    if data_type == "NUMBER":
        p, s = row.get("data_precision"), row.get("data_scale")
        if p is None and s is None:
            # Bare NUMBER — operator must pin. Don't silently coerce.
            return FPType.UNKNOWN, {"reason": "NUMBER without precision/scale: ambiguous"}, native
        if p is None:
            # Scale present without precision is rare; treat as DECIMAL.
            return FPType.DECIMAL, {"scale": int(s) if s is not None else 0}, native
        precision = int(p)
        scale = int(s or 0)
        if scale == 0:
            if precision <= 9:
                return FPType.INTEGER, {"bits": 32}, native
            if precision <= 18:
                return FPType.INTEGER, {"bits": 64}, native
            return FPType.DECIMAL, {"precision": precision, "scale": 0}, native
        return FPType.DECIMAL, {"precision": precision, "scale": scale}, native

    # Floats.
    if data_type in {"FLOAT", "BINARY_FLOAT"}:
        return FPType.FLOAT, {"bits": 32}, native
    if data_type == "BINARY_DOUBLE":
        return FPType.FLOAT, {"bits": 64}, native

    # Strings.
    if data_type in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"}:
        unicode = data_type.startswith("N")
        fixed = data_type in {"CHAR", "NCHAR"}
        params: dict[str, Any] = {}
        if unicode:
            params["unicode"] = True
        if fixed:
            params["fixed"] = True
        char_length = row.get("char_length")
        char_used = (row.get("char_used") or "").strip().upper()
        if char_length is not None:
            params["length"] = int(char_length)
            if char_used == "B":
                params["length_semantics"] = "bytes"
            elif char_used == "C":
                params["length_semantics"] = "chars"
        return FPType.STRING, params, native

    if data_type in {"CLOB", "NCLOB"}:
        params = {"unbounded": True}
        if data_type == "NCLOB":
            params["unicode"] = True
        return FPType.STRING, params, native

    if data_type == "LONG":
        # LONG is deprecated but still in legacy schemas. Unbounded VARCHAR2.
        return FPType.STRING, {"unbounded": True, "legacy": True}, native

    if data_type == "ROWID":
        return FPType.STRING, {"subtype": "rowid"}, native
    if data_type == "UROWID":
        return FPType.STRING, {"subtype": "urowid"}, native

    if data_type == "XMLTYPE":
        return FPType.STRING, {"subtype": "xml"}, native

    # Booleans (Oracle 23ai+).
    if data_type == "BOOLEAN":
        return FPType.BOOLEAN, {}, native

    # Temporals. Oracle's DATE carries a time component — emit as TIMESTAMP
    # with precision 0, NOT as FPType.DATE. Surfacing as FPType.DATE would
    # silently drop hh:mm:ss on every read.
    if data_type == "DATE":
        return FPType.TIMESTAMP, {"with_timezone": False, "precision": 0, "subtype": "oracle_date"}, native

    if data_type.startswith("TIMESTAMP"):
        # data_type strings look like:
        #   TIMESTAMP(6)
        #   TIMESTAMP(6) WITH TIME ZONE
        #   TIMESTAMP(6) WITH LOCAL TIME ZONE
        upper = data_type.upper()
        with_tz = "WITH TIME ZONE" in upper or "WITH LOCAL TIME ZONE" in upper
        with_local = "WITH LOCAL TIME ZONE" in upper
        params = {"with_timezone": with_tz}
        if with_local:
            params["subtype"] = "local"
        # Precision is encoded in the type string; also exposed via scale
        # in some driver tables. Pull from scale when available.
        s = row.get("data_scale")
        if s is not None:
            params["precision"] = int(s)
        else:
            # Parse "(n)" out of the type string as a fallback.
            try:
                inside = upper.split("(", 1)[1].split(")", 1)[0]
                params["precision"] = int(inside)
            except (IndexError, ValueError):
                pass
        return FPType.TIMESTAMP, params, native

    if data_type == "INTERVAL YEAR TO MONTH":
        return FPType.STRING, {"subtype": "interval_y2m"}, native
    if data_type == "INTERVAL DAY TO SECOND":
        return FPType.STRING, {"subtype": "interval_d2s"}, native

    # Binaries.
    if data_type == "RAW":
        n = row.get("char_length") or row.get("data_length")
        params = {}
        if n is not None:
            params["length"] = int(n)
        return FPType.BINARY, params, native
    if data_type == "LONG RAW":
        return FPType.BINARY, {"unbounded": True, "legacy": True}, native
    if data_type == "BLOB":
        return FPType.BINARY, {"unbounded": True}, native
    if data_type == "BFILE":
        return FPType.BINARY, {"unbounded": True, "subtype": "bfile"}, native

    # JSON (23ai+).
    if data_type == "JSON":
        return FPType.JSON, {}, native

    # Anything else (USER_DEFINED, REF, opaque object types, ...) → UNKNOWN.
    return FPType.UNKNOWN, {}, native


def _format_native(row: dict[str, Any]) -> str:
    """Reconstruct the human-readable native type string."""
    data_type = (row.get("data_type") or "").upper().strip()
    if not data_type:
        return ""

    if data_type == "NUMBER":
        p, s = row.get("data_precision"), row.get("data_scale")
        if p is None and s is None:
            return "NUMBER"
        if s in (None, 0):
            return f"NUMBER({p})" if p is not None else "NUMBER"
        return f"NUMBER({p},{s})"

    if data_type in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW"}:
        n = row.get("char_length") or row.get("data_length")
        if n is not None:
            return f"{data_type}({n})"
        return data_type

    # TIMESTAMP precision already encoded in data_type for Oracle.
    return data_type
