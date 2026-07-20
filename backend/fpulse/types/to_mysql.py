"""CanonicalSchema → MySQL.

DDL + cast-plan emission for a MySQL target. Mirrors the to_postgres /
to_mssql shape so the runtime can swap sinks behind one interface.
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

def canonical_to_mysql_ddl(
    schema: CanonicalSchema,
    table: str,
    if_not_exists: bool = True,
) -> str:
    cols = [_field_to_ddl(f) for f in schema.fields]
    body = ",\n  ".join(cols)
    ine = " IF NOT EXISTS" if if_not_exists else ""
    return f"CREATE TABLE{ine} {_quote_ident(table)} (\n  {body}\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"


def canonical_to_mysql_alter(
    old: CanonicalSchema,
    new: CanonicalSchema,
    table: str,
) -> "AlterPlan":
    old_by_name = {f.name: f for f in old.fields}
    new_by_name = {f.name: f for f in new.fields}
    fq = _quote_ident(table)
    safe: list[str] = []
    blocked: list[tuple[str, str]] = []

    for name, field in new_by_name.items():
        if name in old_by_name:
            continue
        col_ddl = _field_to_ddl(field)
        if field.nullable:
            safe.append(f"ALTER TABLE {fq} ADD COLUMN {col_ddl}")
        else:
            blocked.append((
                f"add NOT NULL column {name!r}",
                "MySQL requires a DEFAULT for NOT NULL adds on a non-empty table.",
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
    null_sql = "NULL" if field.nullable else "NOT NULL"
    return f"{_quote_ident(field.name)} {type_sql} {null_sql}"


def _type_to_sql(field: FPField) -> str:
    t = field.type
    p = field.params or {}
    if t == FPType.INTEGER:
        bits = int(p.get("bits") or 32)
        unsigned = " UNSIGNED" if p.get("unsigned") else ""
        if bits <= 8:
            return f"TINYINT{unsigned}"
        if bits <= 16:
            return f"SMALLINT{unsigned}"
        if bits <= 24:
            return f"MEDIUMINT{unsigned}"
        if bits <= 32:
            return f"INT{unsigned}"
        return f"BIGINT{unsigned}"
    if t == FPType.DECIMAL:
        precision = int(p.get("precision") or 18)
        scale = int(p.get("scale") or 0)
        return f"DECIMAL({precision},{scale})"
    if t == FPType.FLOAT:
        bits = int(p.get("bits") or 64)
        return "FLOAT" if bits <= 32 else "DOUBLE"
    if t == FPType.BOOLEAN:
        # Canonical MySQL convention for booleans.
        return "TINYINT(1)"
    if t == FPType.STRING:
        if p.get("subtype") == "uuid":
            return "CHAR(36)"
        if p.get("subtype") == "enum" and p.get("choices"):
            # Python 3.11 doesn't allow same-quote nesting inside an f-string
            # (PEP 701 landed in 3.12). Escape and quote outside the f-string
            # so this compiles on the project's minimum-supported runtime.
            escaped = [c.replace("'", "''") for c in p["choices"]]
            quoted = ",".join("'" + e + "'" for e in escaped)
            return f"ENUM({quoted})"
        if p.get("unbounded"):
            return "LONGTEXT"
        n = int(p.get("length") or 255)
        if p.get("fixed"):
            # CHAR has 255 max; longer → fall back to VARCHAR.
            if n > 255:
                return f"VARCHAR({n})"
            return f"CHAR({n})"
        if n > 65535:
            return "LONGTEXT"
        return f"VARCHAR({n})"
    if t == FPType.DATE:
        return "DATE"
    if t == FPType.TIME:
        precision = p.get("precision")
        return f"TIME({precision})" if precision is not None else "TIME"
    if t == FPType.TIMESTAMP:
        precision = p.get("precision")
        with_tz = bool(p.get("with_timezone"))
        # MySQL's TIMESTAMP type is tz-aware via session conversion;
        # DATETIME is tz-naive. Pick by the canonical with_timezone flag.
        if with_tz:
            return f"TIMESTAMP({precision})" if precision is not None else "TIMESTAMP"
        return f"DATETIME({precision})" if precision is not None else "DATETIME"
    if t == FPType.BINARY:
        if p.get("unbounded"):
            return "LONGBLOB"
        n = int(p.get("length") or 255)
        if p.get("fixed"):
            return f"BINARY({n})"
        if n > 65535:
            return "LONGBLOB"
        return f"VARBINARY({n})"
    if t == FPType.JSON:
        return "JSON"
    # LIST / STRUCT / MAP / UNKNOWN → JSON for portability.
    return "JSON"


def _cast_expression(source: FPField, target: FPField) -> str:
    target_sql = _type_to_sql(target)
    return f"CAST({_quote_ident(source.name)} AS {target_sql})"


def _quote_ident(name: str) -> str:
    """MySQL identifiers use backticks; embedded backticks doubled."""
    safe = name.replace("`", "``")
    return f"`{safe}`"


@dataclass(frozen=True)
class AlterPlan:
    safe: list[str]
    blocked: list[tuple[str, str]]
