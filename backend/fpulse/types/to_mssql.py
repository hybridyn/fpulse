"""CanonicalSchema → SQL Server.

DDL + cast-plan emission for a SQL Server target. Mirrors ``to_postgres``;
the cross-dialect cast tests in ``tests/test_canonical_cross_dialect.py``
exercise round-trip (Postgres → canonical → SQL Server) and edge-case
(Oracle NUMBER(p,s) → SQL Server INT/DECIMAL with cast safety) flows.

Surfaces:

  - ``canonical_to_mssql_ddl(schema, table, schema_name="dbo")`` — emit
    a ``CREATE TABLE`` matching the canonical contract.
  - ``canonical_to_mssql_alter(old, new, table, schema_name="dbo")`` —
    emit ``ALTER TABLE … ADD`` statements for additive drift. Critical
    changes (drop column / narrow type / non-null without default) are
    returned as separate "blocked" entries so the runtime can surface
    them to the operator before applying.
  - ``plan_cast(source, target)`` — re-exported from cast_safety so the
    Mapping tab and sink writer share one source of truth.
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

def canonical_to_mssql_ddl(
    schema: CanonicalSchema,
    table: str,
    schema_name: str = "dbo",
    if_not_exists: bool = True,
) -> str:
    """Render a canonical schema as a SQL Server ``CREATE TABLE``."""
    cols = [_field_to_ddl(f) for f in schema.fields]
    body = ",\n  ".join(cols)
    fq = _quote_ident(schema_name) + "." + _quote_ident(table)
    if if_not_exists:
        # SQL Server has no IF NOT EXISTS on CREATE TABLE; the canonical
        # pattern is an OBJECT_ID check.
        return (
            f"IF OBJECT_ID(N'{schema_name}.{table}', N'U') IS NULL\n"
            f"BEGIN\n"
            f"  CREATE TABLE {fq} (\n  {body}\n  );\n"
            f"END;"
        )
    return f"CREATE TABLE {fq} (\n  {body}\n);"


def canonical_to_mssql_alter(
    old: CanonicalSchema,
    new: CanonicalSchema,
    table: str,
    schema_name: str = "dbo",
) -> "AlterPlan":
    """Diff two canonical schemas and emit SQL Server ALTER statements.

    Additive drift (new nullable column, widened type) is safe. Critical
    drift (drop column, narrow type, non-null without default) lands in
    ``blocked`` so the runtime can require operator confirmation.
    """
    old_by_name = {f.name: f for f in old.fields}
    new_by_name = {f.name: f for f in new.fields}
    fq = _quote_ident(schema_name) + "." + _quote_ident(table)
    safe: list[str] = []
    blocked: list[tuple[str, str]] = []

    # Added columns.
    for name, field in new_by_name.items():
        if name in old_by_name:
            continue
        col_ddl = _field_to_ddl(field)
        if field.nullable:
            safe.append(f"ALTER TABLE {fq} ADD {col_ddl};")
        else:
            blocked.append((
                f"add NOT NULL column {name!r}",
                "Adding a NOT NULL column without a default fails on existing rows. "
                "Either make it nullable or add a default expression.",
            ))

    # Dropped columns — never auto-apply.
    for name in old_by_name:
        if name not in new_by_name:
            blocked.append((
                f"drop column {name!r}",
                "Column drops are irreversible. Confirm explicitly.",
            ))

    # Modified columns.
    for name, new_field in new_by_name.items():
        if name not in old_by_name:
            continue
        old_field = old_by_name[name]
        safety, reason = classify_cast(old_field, new_field)
        if safety == CastSafety.SAFE:
            continue
        # Even "safe-ish" widening (e.g. NVARCHAR(50) → NVARCHAR(100)) is
        # surfaced explicitly. The operator gets to see the plan.
        if safety == CastSafety.SEMANTIC_LOSSY or safety == CastSafety.LOSSY:
            blocked.append((
                f"change type of {name!r}: "
                f"{old_field.native_raw or old_field.type.value} → "
                f"{new_field.native_raw or new_field.type.value}",
                reason or "lossy cast",
            ))

    return AlterPlan(safe=safe, blocked=blocked)


def plan_cast(source: FPField, target: FPField) -> CastPlanElement:
    """Return a cast plan element for source → target.

    Wraps ``cast_safety.classify_cast`` with a SQL-Server-flavored
    expression so the sink writer can emit ``CAST(x AS DECIMAL(18,2))``
    when the canonical layer says the cast is safe.
    """
    safety, reason = classify_cast(source, target)
    expr = _cast_expression(source, target)
    return CastPlanElement(
        source=source,
        target=target,
        safety=safety,
        expression=expr,
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
        if bits <= 8:
            return "TINYINT"
        if bits <= 16:
            return "SMALLINT"
        if bits <= 32:
            return "INT"
        return "BIGINT"
    if t == FPType.DECIMAL:
        precision = int(p.get("precision") or 18)
        scale = int(p.get("scale") or 0)
        return f"DECIMAL({precision},{scale})"
    if t == FPType.FLOAT:
        bits = int(p.get("bits") or 64)
        return "REAL" if bits <= 32 else "FLOAT"
    if t == FPType.BOOLEAN:
        return "BIT"
    if t == FPType.STRING:
        unicode = bool(p.get("unicode"))
        fixed = bool(p.get("fixed"))
        if p.get("subtype") == "uuid":
            return "UNIQUEIDENTIFIER"
        if p.get("subtype") == "xml":
            return "XML"
        if p.get("unbounded"):
            return "NVARCHAR(MAX)" if unicode else "VARCHAR(MAX)"
        n = int(p.get("length") or 255)
        if fixed:
            return f"NCHAR({n})" if unicode else f"CHAR({n})"
        return f"NVARCHAR({n})" if unicode else f"VARCHAR({n})"
    if t == FPType.DATE:
        return "DATE"
    if t == FPType.TIME:
        precision = p.get("precision")
        return f"TIME({precision})" if precision is not None else "TIME"
    if t == FPType.TIMESTAMP:
        precision = p.get("precision")
        with_tz = bool(p.get("with_timezone"))
        if with_tz:
            return f"DATETIMEOFFSET({precision})" if precision is not None else "DATETIMEOFFSET"
        return f"DATETIME2({precision})" if precision is not None else "DATETIME2"
    if t == FPType.BINARY:
        if p.get("subtype") == "rowversion":
            return "ROWVERSION"
        if p.get("unbounded"):
            return "VARBINARY(MAX)"
        n = int(p.get("length") or 8000)
        fixed = bool(p.get("fixed"))
        return f"BINARY({n})" if fixed else f"VARBINARY({n})"
    if t == FPType.JSON:
        # SQL Server stores JSON as NVARCHAR(MAX) by convention — there's
        # a CHECK ISJSON() constraint we could emit, but that's a runtime
        # decision left to the operator (some pipelines accept malformed
        # JSON and rely on downstream cleanup).
        return "NVARCHAR(MAX)"
    if t == FPType.LIST or t == FPType.STRUCT or t == FPType.MAP:
        # SQL Server has no native list/struct/map. Serialize as JSON.
        return "NVARCHAR(MAX)"
    # UNKNOWN → preserve as NVARCHAR(MAX) and rely on the Mapping tab
    # to flag for operator pinning.
    return "NVARCHAR(MAX)"


def _cast_expression(source: FPField, target: FPField) -> str:
    """SQL Server CAST expression for the source column → target type."""
    target_sql = _type_to_sql(target)
    return f"CAST({_quote_ident(source.name)} AS {target_sql})"


def _quote_ident(name: str) -> str:
    """Quote a SQL Server identifier with square brackets."""
    # Replace ] with ]] per SQL Server escaping rules.
    safe = name.replace("]", "]]")
    return f"[{safe}]"


@dataclass(frozen=True)
class AlterPlan:
    """Outcome of an alter diff.

    Attributes:
        safe: list of ALTER statements that can be auto-applied.
        blocked: list of (description, reason) pairs the runtime must
            surface to the operator for confirmation.
    """
    safe: list[str]
    blocked: list[tuple[str, str]]
