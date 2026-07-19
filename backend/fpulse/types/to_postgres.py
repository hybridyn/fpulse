"""CanonicalSchema → Postgres.

Two surfaces, both driven by the same FPType → PG type mapping:

  - ``canonical_to_postgres_ddl(schema, table)`` — emit a CREATE TABLE
    statement that matches the canonical contract. Used when the
    target table doesn't exist yet (Auto-create-table on first write).

  - ``canonical_to_postgres_alter(old, new, table)`` — emit ALTER TABLE
    statements for additive drift (new nullable columns, widened
    types). Critical-severity changes are NOT auto-applied: the
    runtime surfaces them to the operator and waits for confirmation.

  - ``plan_cast(source, target)`` — per-column cast plan. Each entry
    carries the SQL expression the sink should emit when copying a
    source column into the target column, plus the cast safety so the
    sink can honor ``cast_policy=strict``.

The DDL emitter is the inverse of ``from_postgres``. Round-trip
property: every canonical schema we read out of a real Postgres table
should emit DDL we can read back into the same canonical schema.
``tests/test_to_postgres.py::TestRoundTrip`` locks this invariant.
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

def canonical_to_postgres_ddl(
    schema: CanonicalSchema,
    table: str,
    schema_name: str = "public",
    if_not_exists: bool = True,
) -> str:
    """Render a canonical schema as a Postgres CREATE TABLE.

    Each column is emitted with its parameterized type and nullability.
    Column order matches ``schema.fields`` order so any downstream
    reader that maps by position stays stable.
    """
    cols = [_column_clause(f) for f in schema.fields]
    body = ",\n    ".join(cols)
    head = "CREATE TABLE"
    if if_not_exists:
        head += " IF NOT EXISTS"
    qualified = _quote_identifier(table)
    if schema_name:
        qualified = f"{_quote_identifier(schema_name)}.{qualified}"
    return f"{head} {qualified} (\n    {body}\n)"


def canonical_to_postgres_alter(
    old: CanonicalSchema,
    new: CanonicalSchema,
    table: str,
    schema_name: str = "public",
) -> list[str]:
    """Emit ALTER TABLE statements for additive drift.

    Returns ONLY the SQL we'd auto-apply without operator confirmation:

      - new nullable columns                  → ``ADD COLUMN … NULL``
      - same-kind widening (e.g. varchar)     → ``ALTER COLUMN … TYPE …``

    Anything that could lose data (REMOVED, narrowing, NOT-NULL adds,
    type kind change) is omitted on purpose. The runtime surfaces those
    via the drift engine + asks the operator before touching prod.
    """
    out: list[str] = []
    qualified = (f"{_quote_identifier(schema_name)}.{_quote_identifier(table)}"
                 if schema_name else _quote_identifier(table))
    old_by_name = {f.name: f for f in old.fields}

    for f in new.fields:
        existing = old_by_name.get(f.name)

        # New nullable column → safe to add.
        if existing is None:
            if f.nullable:
                out.append(
                    f"ALTER TABLE {qualified} ADD COLUMN {_column_clause(f)}"
                )
            # Non-nullable new columns need a backfill plan — skip auto.
            continue

        # Same kind, classifier-SAFE, params widened → widen the column.
        if existing.type == f.type:
            safety, _ = classify_cast(existing, f)
            if safety == CastSafety.SAFE and _widened(existing, f):
                native = _fp_to_pg_native(f)
                out.append(
                    f"ALTER TABLE {qualified} ALTER COLUMN "
                    f"{_quote_identifier(f.name)} TYPE {native}"
                )

    return out


# ── Cast plan ──

def plan_cast(
    source: CanonicalSchema,
    target: CanonicalSchema,
) -> list[CastPlanElement]:
    """Build the per-column cast plan for source → target.

    Each entry tells the sink how to copy one source column into the
    matching target column: the SQL fragment ('CAST(col AS NUMERIC(18,2))',
    'col::text', etc.), the cast safety, and a human-readable reason
    when the cast isn't lossless. Columns present in target but not
    source are skipped (the sink can default them or fail per policy).
    """
    target_by_name = {f.name: f for f in target.fields}
    plan: list[CastPlanElement] = []
    for src in source.fields:
        tgt = target_by_name.get(src.name)
        if tgt is None:
            # Source has a column the target doesn't — caller decides
            # whether to drop or fail per cast_policy.
            continue
        safety, reason = classify_cast(src, tgt)
        plan.append(CastPlanElement(
            source_column=src.name,
            target_column=tgt.name,
            target_native_type=_fp_to_pg_native(tgt),
            safety=safety,
            reason=reason,
        ))
    return plan


# ── Internals: FPType → PG native ──

def _column_clause(f: FPField) -> str:
    native = _fp_to_pg_native(f)
    null = "NULL" if f.nullable else "NOT NULL"
    return f"{_quote_identifier(f.name)} {native} {null}"


def _fp_to_pg_native(f: FPField) -> str:
    """FPField → Postgres native type string.

    Mirror image of ``from_postgres._pg_to_fptype``. New types added on
    the read side must be added here too — the round-trip test
    enforces it.
    """
    t = f.type
    p = f.params or {}

    if t == FPType.INTEGER:
        bits = int(p.get("bits", 32))
        if bits <= 16:
            return "SMALLINT"
        if bits >= 64:
            return "BIGINT"
        return "INTEGER"

    if t == FPType.DECIMAL:
        if p.get("subtype") == "money":
            return "MONEY"
        precision = p.get("precision")
        scale = p.get("scale", 0)
        if precision is not None:
            return f"NUMERIC({int(precision)},{int(scale)})"
        return "NUMERIC"

    if t == FPType.FLOAT:
        bits = int(p.get("bits", 64))
        return "DOUBLE PRECISION" if bits >= 64 else "REAL"

    if t == FPType.STRING:
        subtype = p.get("subtype")
        if subtype == "uuid":
            return "UUID"
        if subtype in {"inet", "cidr", "macaddr", "macaddr8"}:
            return subtype.upper()
        length = p.get("length")
        fixed = p.get("fixed", False)
        if length is None:
            return "TEXT"
        return f"CHAR({int(length)})" if fixed else f"VARCHAR({int(length)})"

    if t == FPType.BOOLEAN:
        return "BOOLEAN"

    if t == FPType.DATE:
        return "DATE"

    if t == FPType.TIME:
        tz = bool(p.get("with_timezone"))
        precision = p.get("precision")
        base = "TIMETZ" if tz else "TIME"
        return f"{base}({int(precision)})" if precision is not None else base

    if t == FPType.TIMESTAMP:
        tz = bool(p.get("with_timezone"))
        precision = p.get("precision")
        base = "TIMESTAMPTZ" if tz else "TIMESTAMP"
        return f"{base}({int(precision)})" if precision is not None else base

    if t == FPType.BINARY:
        return "BYTEA"

    if t == FPType.JSON:
        return "JSONB" if p.get("subtype") == "jsonb" else "JSON"

    if t == FPType.LIST:
        element = p.get("element_type")
        if isinstance(element, FPField):
            inner = _fp_to_pg_native(element)
            return f"{inner}[]"
        return "TEXT[]"  # honest fallback

    # STRUCT / MAP / UNKNOWN — fall back to JSONB so the row still
    # round-trips. Real STRUCT support on PG goes through composite
    # types, which we'll add when we wire writeable composite types.
    return "JSONB"


def _widened(old: FPField, new: FPField) -> bool:
    """Same-kind widening detection used by the ALTER emitter."""
    if old.type != new.type:
        return False
    if old.type == FPType.DECIMAL:
        op, os_ = int(old.params.get("precision", 38)), int(old.params.get("scale", 0))
        np_, ns = int(new.params.get("precision", 38)), int(new.params.get("scale", 0))
        return np_ > op or ns > os_
    if old.type == FPType.STRING:
        ol = old.params.get("length")
        nl = new.params.get("length")
        if ol is None:
            return False
        if nl is None:
            return True  # bounded → unbounded
        return int(nl) > int(ol)
    if old.type == FPType.INTEGER:
        ob = int(old.params.get("bits", 32))
        nb = int(new.params.get("bits", 32))
        return nb > ob
    return False


def _quote_identifier(ident: str) -> str:
    """Conservative double-quoted PG identifier."""
    safe = ident.replace('"', '""')
    return f'"{safe}"'
