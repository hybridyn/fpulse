"""CanonicalSchema → Oracle.

Inverse of ``from_oracle`` for DDL emission + cast planning. Note:
writing INTO Oracle is less common in F-Pulse pipelines than reading
FROM Oracle (typical use case is Oracle as a legacy source, with
the target being Postgres / Snowflake / SQL Server / Parquet). This
module covers the round-trip case + the rarer "extract to Oracle"
flow without trying to handle the most exotic Oracle features.

Surfaces:
  - ``canonical_to_oracle_ddl(schema, owner, table)``
  - ``canonical_to_oracle_alter(old, new, owner, table)``
  - ``plan_cast(source, target)``
"""

from __future__ import annotations

from dataclasses import dataclass

from fpulse.types.canonical import CanonicalSchema, FPField, FPType
from fpulse.types.cast_safety import (
    CastPlanElement,
    CastSafety,
    classify_cast,
)


# ── DDL emission ──

def canonical_to_oracle_ddl(
    schema: CanonicalSchema,
    table: str,
    owner: str | None = None,
) -> str:
    """Render a canonical schema as an Oracle CREATE TABLE.

    Oracle has no IF NOT EXISTS on CREATE TABLE. The runtime must check
    ``all_tables`` first (or wrap in a PL/SQL exception block) before
    issuing this DDL. We emit the plain CREATE here.
    """
    cols = [_field_to_ddl(f) for f in schema.fields]
    body = ",\n  ".join(cols)
    fq = (owner + "." if owner else "") + _quote_ident(table)
    return f"CREATE TABLE {fq} (\n  {body}\n)"


def canonical_to_oracle_alter(
    old: CanonicalSchema,
    new: CanonicalSchema,
    table: str,
    owner: str | None = None,
) -> "AlterPlan":
    """Diff two canonical schemas and emit Oracle ALTER statements."""
    old_by_name = {f.name: f for f in old.fields}
    new_by_name = {f.name: f for f in new.fields}
    fq = (owner + "." if owner else "") + _quote_ident(table)
    safe: list[str] = []
    blocked: list[tuple[str, str]] = []

    for name, field in new_by_name.items():
        if name in old_by_name:
            continue
        col_ddl = _field_to_ddl(field)
        if field.nullable:
            safe.append(f"ALTER TABLE {fq} ADD ({col_ddl})")
        else:
            blocked.append((
                f"add NOT NULL column {name!r}",
                "Oracle requires a DEFAULT for NOT NULL adds on a non-empty table.",
            ))

    for name in old_by_name:
        if name not in new_by_name:
            blocked.append((
                f"drop column {name!r}",
                "Column drops are irreversible. Confirm explicitly.",
            ))

    for name, new_field in new_by_name.items():
        if name not in old_by_name:
            continue
        old_field = old_by_name[name]
        safety, reason = classify_cast(old_field, new_field)
        if safety in (CastSafety.LOSSY, CastSafety.SEMANTIC_LOSSY):
            blocked.append((
                f"change type of {name!r}: "
                f"{old_field.native_raw or old_field.type.value} → "
                f"{new_field.native_raw or new_field.type.value}",
                reason or "lossy cast",
            ))

    return AlterPlan(safe=safe, blocked=blocked)


def plan_cast(source: FPField, target: FPField) -> CastPlanElement:
    safety, reason = classify_cast(source, target)
    return CastPlanElement(
        source=source,
        target=target,
        safety=safety,
        expression=_cast_expression(source, target),
        reason=reason or "",
    )


# ── Per-FPType DDL fragment ──

def _field_to_ddl(field: FPField) -> str:
    type_sql = _type_to_sql(field)
    null_sql = "" if field.nullable else " NOT NULL"
    return f"{_quote_ident(field.name)} {type_sql}{null_sql}"


def _type_to_sql(field: FPField) -> str:
    t = field.type
    p = field.params or {}
    if t == FPType.INTEGER:
        bits = int(p.get("bits") or 32)
        if bits <= 16:
            return "NUMBER(5,0)"
        if bits <= 32:
            return "NUMBER(10,0)"
        return "NUMBER(19,0)"
    if t == FPType.DECIMAL:
        precision = int(p.get("precision") or 18)
        scale = int(p.get("scale") or 0)
        return f"NUMBER({precision},{scale})"
    if t == FPType.FLOAT:
        bits = int(p.get("bits") or 64)
        return "BINARY_FLOAT" if bits <= 32 else "BINARY_DOUBLE"
    if t == FPType.BOOLEAN:
        # Pre-23ai Oracle has no BOOLEAN — fall back to NUMBER(1,0). Use
        # NUMBER(1) so the operator sees the intent in introspection.
        return "NUMBER(1,0)"
    if t == FPType.STRING:
        unicode = bool(p.get("unicode"))
        if p.get("subtype") == "uuid":
            return "VARCHAR2(36)"
        if p.get("unbounded") or p.get("length") is None:
            return "NCLOB" if unicode else "CLOB"
        n = int(p.get("length") or 255)
        # VARCHAR2 has a 4000-byte (32767 with MAX_STRING_SIZE=EXTENDED)
        # limit. For larger declared lengths, fall back to CLOB to avoid
        # silent truncation at write time.
        if n > 4000:
            return "NCLOB" if unicode else "CLOB"
        return f"NVARCHAR2({n})" if unicode else f"VARCHAR2({n})"
    if t == FPType.DATE:
        # Use DATE (carries time) so a canonical DATE → Oracle DATE
        # round-trip doesn't lose anything. The note here matters: the
        # operator may have come from a SOURCE that distinguishes DATE
        # from DATETIME (Postgres, SQL Server) — they should know that
        # writing to Oracle "DATE" gets time information attached.
        return "DATE"
    if t == FPType.TIME:
        # Oracle has no pure TIME type. Synthesize via TIMESTAMP and let
        # the caller see this in the Mapping tab.
        return "TIMESTAMP(6)"
    if t == FPType.TIMESTAMP:
        precision = p.get("precision")
        with_tz = bool(p.get("with_timezone"))
        if with_tz:
            if p.get("subtype") == "local":
                return f"TIMESTAMP({precision}) WITH LOCAL TIME ZONE" if precision is not None else "TIMESTAMP WITH LOCAL TIME ZONE"
            return f"TIMESTAMP({precision}) WITH TIME ZONE" if precision is not None else "TIMESTAMP WITH TIME ZONE"
        return f"TIMESTAMP({precision})" if precision is not None else "TIMESTAMP"
    if t == FPType.BINARY:
        if p.get("subtype") == "bfile":
            return "BFILE"
        if p.get("unbounded") or p.get("length") is None:
            return "BLOB"
        n = int(p.get("length") or 2000)
        # RAW limit is 2000; bigger goes to BLOB to avoid truncation.
        if n > 2000:
            return "BLOB"
        return f"RAW({n})"
    if t == FPType.JSON:
        # 23ai has native JSON; older versions store as VARCHAR2/CLOB with
        # IS JSON constraint. Emit CLOB for portability; runtime can opt
        # into JSON via an alter when running on 23ai+.
        return "CLOB"
    # LIST / STRUCT / MAP / UNKNOWN → CLOB.
    return "CLOB"


def _cast_expression(source: FPField, target: FPField) -> str:
    target_sql = _type_to_sql(target)
    return f"CAST({_quote_ident(source.name)} AS {target_sql})"


def _quote_ident(name: str) -> str:
    """Oracle identifiers are uppercased by default. Quote to preserve
    mixed-case names that came from a case-sensitive source dialect.
    """
    safe = name.replace('"', '""')
    return f'"{safe}"'


@dataclass(frozen=True)
class AlterPlan:
    safe: list[str]
    blocked: list[tuple[str, str]]
