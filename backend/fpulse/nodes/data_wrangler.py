"""Data Wrangler node — stepwise visible transform.

A single canvas node that hosts an ordered list of small transformation
sub-steps with per-step preview. Collapses what would otherwise be a long
linear chain of canvas nodes (Rename -> Cast -> Filter -> Derive -> Group)
into one tile.

See docs/design-data-wrangler-node.md for the full design.

Execution model:
  - Edit-time preview (frontend calls /data-wrangler/preview):
      Materialize step-by-step against a capped sample (default 100 rows).
      Returns row-count delta + schema delta per sub-step.
  - Run-time (executor calls .execute()):
      Compile the entire enabled-step list to ONE SQL statement using CTEs
      and submit once to DuckDB. The optimizer fuses the layered subselects;
      no temp views are materialized.

Sub-step DSL (v1: 6 ops):
  - filter:    drop rows by predicate
  - select:    keep only N columns
  - rename:    rename columns
  - cast:      change column types
  - derive:    add computed column(s)
  - group_by:  aggregate
  - sort:      ORDER BY one or more columns (P2-B, 2026-05-18)
  - dedupe:    drop duplicates by key (keep_first / keep_last) (P2-B)
  - sample:    LIMIT N (first) or USING SAMPLE N ROWS (random) (P2-B)
  - flatten:   expand STRUCT column into top-level fields (P2-B)
"""

from __future__ import annotations

import re
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.filter_node import _rules_to_condition
from fpulse.nodes.registry import register


# ─────────────────────────────────────────────────────────────────────────────
# Identifier quoting + type validation
# ─────────────────────────────────────────────────────────────────────────────

def _q(name: str) -> str:
    """Double-quote a SQL identifier, escaping embedded quotes."""
    if name is None:
        return '""'
    s = str(name).replace('"', '""')
    return f'"{s}"'


# Cast-target allowlist. DECIMAL(p,s) is validated via pattern; everything
# else must be one of these literal type names. Keeps cast SQL safe even
# though the value flows directly into the generated query.
_ALLOWED_CAST_TYPES = {
    "INTEGER", "BIGINT", "DOUBLE", "REAL", "VARCHAR",
    "BOOLEAN", "DATE", "TIMESTAMP", "TIME", "BLOB",
}
_DECIMAL_TYPE_PATTERN = re.compile(r"^DECIMAL\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\)$", re.IGNORECASE)


def _validate_cast_type(to_type: str) -> str:
    """Return a safe, normalized cast type or raise ValueError."""
    if not to_type:
        raise ValueError("cast: to_type is required")
    t = to_type.strip().upper()
    if t in _ALLOWED_CAST_TYPES:
        return t
    if _DECIMAL_TYPE_PATTERN.match(t):
        return t
    raise ValueError(
        f"cast: unsupported to_type {to_type!r}. "
        f"Allowed: {sorted(_ALLOWED_CAST_TYPES)} or DECIMAL(p,s)."
    )


_ALLOWED_AGG_FUNCS = {"SUM", "COUNT", "AVG", "MIN", "MAX", "COUNT_DISTINCT"}


def _validate_agg_func(func: str) -> str:
    f = (func or "").strip().upper()
    if f not in _ALLOWED_AGG_FUNCS:
        raise ValueError(
            f"group_by: unsupported aggregation func {func!r}. "
            f"Allowed: {sorted(_ALLOWED_AGG_FUNCS)}."
        )
    return f


# ─────────────────────────────────────────────────────────────────────────────
# Per-op compilers — each takes the inner SQL fragment (already a SELECT)
# and returns a new SELECT that wraps it as a subquery.
# ─────────────────────────────────────────────────────────────────────────────

def _compile_filter(prev_sql: str, config: dict, alias: str) -> str:
    mode = config.get("mode", "rules")
    if mode == "expression":
        condition = (config.get("expression") or "TRUE").strip()
    else:
        rules = config.get("rules") or []
        combinator = config.get("combinator", "AND")
        condition = _rules_to_condition(rules, combinator)
    if not condition or condition.upper() == "TRUE":
        return f"SELECT * FROM ({prev_sql}) AS {alias}"
    return f"SELECT * FROM ({prev_sql}) AS {alias} WHERE {condition}"


def _compile_select(prev_sql: str, config: dict, alias: str) -> str:
    cols = config.get("columns") or []
    cols = [c for c in cols if c and str(c).strip()]
    if not cols:
        return f"SELECT * FROM ({prev_sql}) AS {alias}"
    projection = ", ".join(_q(c) for c in cols)
    return f"SELECT {projection} FROM ({prev_sql}) AS {alias}"


def _compile_rename(prev_sql: str, config: dict, alias: str) -> str:
    rename_map = config.get("rename_map") or {}
    pairs = [(k, v) for k, v in rename_map.items() if k and v and k != v]
    if not pairs:
        return f"SELECT * FROM ({prev_sql}) AS {alias}"
    # DuckDB supports SELECT * RENAME (old AS new, ...)
    renames = ", ".join(f"{_q(old)} AS {_q(new)}" for old, new in pairs)
    return f"SELECT * RENAME ({renames}) FROM ({prev_sql}) AS {alias}"


def _compile_cast(prev_sql: str, config: dict, alias: str) -> str:
    casts = config.get("casts") or []
    valid = []
    for c in casts:
        col = (c.get("column") or "").strip()
        to_type = (c.get("to_type") or "").strip()
        if not col or not to_type:
            continue
        normalized = _validate_cast_type(to_type)
        valid.append((col, normalized))
    if not valid:
        return f"SELECT * FROM ({prev_sql}) AS {alias}"
    # DuckDB: SELECT * REPLACE (CAST(col AS type) AS col, ...)
    replaces = ", ".join(
        f"CAST({_q(col)} AS {ttype}) AS {_q(col)}" for col, ttype in valid
    )
    return f"SELECT * REPLACE ({replaces}) FROM ({prev_sql}) AS {alias}"


def _compile_derive(prev_sql: str, config: dict, alias: str) -> str:
    derived = config.get("derived") or []
    extras = []
    for d in derived:
        name = (d.get("name") or "").strip()
        expr = (d.get("expression") or "").strip()
        if not name or not expr:
            continue
        extras.append(f"({expr}) AS {_q(name)}")
    if not extras:
        return f"SELECT * FROM ({prev_sql}) AS {alias}"
    return f"SELECT *, {', '.join(extras)} FROM ({prev_sql}) AS {alias}"


def _compile_group_by(prev_sql: str, config: dict, alias: str) -> str:
    keys = [k for k in (config.get("keys") or []) if k and str(k).strip()]
    aggs = config.get("aggregations") or []
    projection_parts: list[str] = []
    for k in keys:
        projection_parts.append(_q(k))
    for a in aggs:
        func = _validate_agg_func(a.get("func", "COUNT"))
        col = (a.get("column") or "*").strip()
        out_alias = (a.get("alias") or "").strip() or f"{func.lower()}_{col}"
        if col == "*":
            col_sql = "*"
        else:
            col_sql = _q(col)
        if func == "COUNT_DISTINCT":
            projection_parts.append(f"COUNT(DISTINCT {col_sql}) AS {_q(out_alias)}")
        else:
            projection_parts.append(f"{func}({col_sql}) AS {_q(out_alias)}")
    if not projection_parts:
        return f"SELECT * FROM ({prev_sql}) AS {alias}"
    projection = ", ".join(projection_parts)
    group_clause = ""
    if keys:
        group_clause = " GROUP BY " + ", ".join(_q(k) for k in keys)
    return f"SELECT {projection} FROM ({prev_sql}) AS {alias}{group_clause}"


# ─────────────────────────────────────────────────────────────────────────────
# P2-B (2026-05-18) — four new sub-step compilers so the Wrangler can
# absorb sort / dedupe / sample / flatten without users chaining
# standalone canvas nodes for them.
# ─────────────────────────────────────────────────────────────────────────────

def _compile_sort(prev_sql: str, config: dict, alias: str) -> str:
    sort_by = [s for s in (config.get("sort_by") or []) if s and str(s).strip()]
    direction = str(config.get("direction") or "ASC").upper()
    if direction not in ("ASC", "DESC"):
        direction = "ASC"
    if not sort_by:
        return f"SELECT * FROM ({prev_sql}) AS {alias}"
    order_clause = ", ".join(f"{_q(c)} {direction}" for c in sort_by)
    return f"SELECT * FROM ({prev_sql}) AS {alias} ORDER BY {order_clause}"


def _compile_dedupe(prev_sql: str, config: dict, alias: str) -> str:
    key = [k for k in (config.get("key") or []) if k and str(k).strip()]
    strategy = str(config.get("strategy") or "keep_first").lower()
    if not key:
        return f"SELECT * FROM ({prev_sql}) AS {alias}"
    key_cols = ", ".join(_q(k) for k in key)
    # ROW_NUMBER() OVER (PARTITION BY ...) keeps either first (1) or
    # last (count). Stable across strategies, no DuckDB-specific syntax.
    row_dir = "ASC" if strategy == "keep_first" else "DESC"
    return (
        f"SELECT * EXCLUDE (__dedup_rn) FROM ("
        f"SELECT *, ROW_NUMBER() OVER (PARTITION BY {key_cols} ORDER BY 1 {row_dir}) AS __dedup_rn "
        f"FROM ({prev_sql}) AS {alias}"
        f") WHERE __dedup_rn = 1"
    )


def _compile_sample(prev_sql: str, config: dict, alias: str) -> str:
    method = str(config.get("method") or "first").lower()
    try:
        count = int(config.get("count") or 100)
    except (TypeError, ValueError):
        count = 100
    count = max(1, count)
    if method == "random":
        return f"SELECT * FROM ({prev_sql}) AS {alias} USING SAMPLE {count} ROWS"
    # 'first'
    return f"SELECT * FROM ({prev_sql}) AS {alias} LIMIT {count}"


def _compile_flatten(
    prev_sql: str,
    config: dict,
    alias: str,
    *,
    conn: Optional["duckdb.DuckDBPyConnection"] = None,
) -> str:
    """Compile a `flatten` sub-step.

    Without prefix: cheap one-liner using DuckDB STRUCT expansion
    `(col).*` — single SQL statement, optimizer fuses it.

    With prefix: needs to enumerate the struct's actual field names
    so each can be explicitly aliased `field` → `{prefix}field`.
    Requires a connection to introspect the struct via a zero-row
    probe `SELECT (col).* FROM (prev) LIMIT 0`. When no conn is
    available (frontend smoke tests, dry-run compilation), falls
    back to the un-prefixed expansion — the prefix is documented
    as runtime-resolved per P2-B; the wrangler executor passes a
    conn so production runs always honor the prefix.
    """
    column = str(config.get("column") or "").strip()
    prefix = str(config.get("prefix") or "")
    keep_original = bool(config.get("keep_original"))
    if not column:
        return f"SELECT * FROM ({prev_sql}) AS {alias}"

    expansion = f"({_q(column)}).*"
    # Prefix path: enumerate the struct's fields against a real
    # connection and build explicit `(col).field AS {prefix}field`
    # projections. Falls back to the un-prefixed `(col).*` form when
    # the introspection fails (struct type unknown / not a struct /
    # no conn). The downstream FlattenExplodeNode also has post-
    # process fallbacks for non-STRUCT columns.
    if prefix and conn is not None:
        try:
            probe = conn.sql(f"SELECT {expansion} FROM ({prev_sql}) LIMIT 0")
            field_names = list(probe.columns)
            if field_names:
                aliased = ", ".join(
                    f"{_q(column)}.{_q(f)} AS {_q(prefix + f)}"
                    for f in field_names
                )
                if keep_original:
                    return f"SELECT *, {aliased} FROM ({prev_sql}) AS {alias}"
                return f"SELECT * EXCLUDE ({_q(column)}), {aliased} FROM ({prev_sql}) AS {alias}"
        except Exception:
            # Fall through to the un-prefixed path. Better to flatten
            # without prefix than to fail the whole pipeline; the
            # subsequent step will see un-prefixed fields and the user
            # can rename them with a downstream Rename sub-step.
            pass
    if keep_original:
        return f"SELECT *, {expansion} FROM ({prev_sql}) AS {alias}"
    return f"SELECT * EXCLUDE ({_q(column)}), {expansion} FROM ({prev_sql}) AS {alias}"


# ─────────────────────────────────────────────────────────────────────────────
# B3 (2026-06-15) — three cleaning sub-steps so the Wrangler covers the
# common "tidy the data" ops without leaving the node: fill nulls, replace
# values, split a column by a delimiter.
# ─────────────────────────────────────────────────────────────────────────────

def _sql_literal(v: Any) -> str:
    """Render a config value as a SQL literal. ``None`` or the explicit string
    ``"NULL"`` → SQL NULL; numeric-looking → numeric; everything else → a
    single-quote-escaped string literal (so "" becomes an empty string)."""
    if v is None:
        return "NULL"
    s = str(v)
    if s.upper() == "NULL":
        return "NULL"
    try:
        float(s)
        return s
    except ValueError:
        return "'" + s.replace("'", "''") + "'"


def _compile_fill_nulls(prev_sql: str, config: dict, alias: str) -> str:
    fills = config.get("fills") or []
    valid = []
    for f in fills:
        col = (f.get("column") or "").strip()
        if not col:
            continue
        valid.append((col, _sql_literal(f.get("value"))))
    if not valid:
        return f"SELECT * FROM ({prev_sql}) AS {alias}"
    replaces = ", ".join(f"COALESCE({_q(col)}, {lit}) AS {_q(col)}" for col, lit in valid)
    return f"SELECT * REPLACE ({replaces}) FROM ({prev_sql}) AS {alias}"


def _compile_replace_values(prev_sql: str, config: dict, alias: str) -> str:
    repls = config.get("replacements") or []
    by_col: dict[str, list[tuple[str, str]]] = {}
    for r in repls:
        col = (r.get("column") or "").strip()
        if not col:
            continue
        by_col.setdefault(col, []).append((_sql_literal(r.get("find")), _sql_literal(r.get("replace"))))
    if not by_col:
        return f"SELECT * FROM ({prev_sql}) AS {alias}"
    replaces = []
    for col, pairs in by_col.items():
        expr = _q(col)
        for find_lit, repl_lit in pairs:
            # exact-value match; nests so multiple replacements on one column chain
            expr = f"CASE WHEN {_q(col)} = {find_lit} THEN {repl_lit} ELSE {expr} END"
        replaces.append(f"{expr} AS {_q(col)}")
    return f"SELECT * REPLACE ({', '.join(replaces)}) FROM ({prev_sql}) AS {alias}"


def _compile_split_column(prev_sql: str, config: dict, alias: str) -> str:
    col = (config.get("column") or "").strip()
    delim = config.get("delimiter")
    delim = "," if delim in (None, "") else str(delim)
    into = [c for c in (config.get("into") or []) if c and str(c).strip()]
    if not col or not into:
        return f"SELECT * FROM ({prev_sql}) AS {alias}"
    delim_lit = "'" + delim.replace("'", "''") + "'"
    parts = ", ".join(
        f"split_part({_q(col)}, {delim_lit}, {i + 1}) AS {_q(name)}"
        for i, name in enumerate(into)
    )
    return f"SELECT *, {parts} FROM ({prev_sql}) AS {alias}"


_COMPILERS = {
    "filter":   _compile_filter,
    "select":   _compile_select,
    "rename":   _compile_rename,
    "cast":     _compile_cast,
    "derive":   _compile_derive,
    "group_by": _compile_group_by,
    "sort":     _compile_sort,
    "dedupe":   _compile_dedupe,
    "sample":   _compile_sample,
    "flatten":  _compile_flatten,
    "fill_nulls":     _compile_fill_nulls,
    "replace_values": _compile_replace_values,
    "split_column":   _compile_split_column,
}


# ─────────────────────────────────────────────────────────────────────────────
# Top-level compile
# ─────────────────────────────────────────────────────────────────────────────

def compile_wrangle(
    steps: list[dict],
    input_table: str,
    *,
    conn: Optional["duckdb.DuckDBPyConnection"] = None,
) -> str:
    """Compile an ordered list of sub-steps to a single SELECT statement.

    Disabled and unknown-op sub-steps are skipped. Empty (or all-disabled)
    recipes return ``SELECT * FROM <input_table>``.

    The output is one SQL statement — no temp views, no CREATE statements.
    The DuckDB optimizer fuses the layered subselects.

    `conn` is optional: pass it when you need runtime field introspection
    (currently only `flatten` with a prefix uses it — see _compile_flatten).
    When `conn` is omitted, the prefix on flatten is silently dropped and
    fields are exposed un-prefixed (graceful degradation; the production
    wrangler executor always passes a conn).
    """
    if not input_table or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", input_table):
        raise ValueError(f"compile_wrangle: invalid input_table identifier {input_table!r}")
    sql = f"SELECT * FROM {input_table}"
    idx = 0
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        if step.get("enabled") is False:
            continue
        op = step.get("op")
        compiler = _COMPILERS.get(op)
        if compiler is None:
            continue
        config = step.get("config") or {}
        alias = f"_w{idx}"
        # Flatten compiler is the only op that needs conn — pass via
        # kwargs so signatures stay flexible for future op additions.
        if op == "flatten":
            sql = compiler(sql, config, alias, conn=conn)
        else:
            sql = compiler(sql, config, alias)
        idx += 1
    return sql


def _locate_failing_step(
    conn: "duckdb.DuckDBPyConnection", steps: list[dict], input_table: str
) -> Optional[tuple[int, str, str, str]]:
    """Re-run the recipe one sub-step at a time to find the FIRST that fails.

    Used to turn a single opaque whole-node error into a precise
    ``sub-step N ('label') failed — <reason>`` message. Each step is probed
    with ``LIMIT 0`` (binds + type-checks the SQL without scanning data), so
    this catches the common config/SQL errors (bad column, bad expression,
    bad cast) cheaply. Returns ``(step_number, label, op, error)`` for the
    first failing step (1-based, matching the UI position), or None when no
    single step binds-fails (e.g. a pure runtime/value error on full data).
    """
    running = f"SELECT * FROM {input_table}"
    idx = 0
    for i, step in enumerate(steps or []):
        if not isinstance(step, dict) or step.get("enabled") is False:
            continue
        op = step.get("op")
        compiler = _COMPILERS.get(op)
        if compiler is None:
            continue
        config = step.get("config") or {}
        alias = f"_w{idx}"
        try:
            running = (
                compiler(running, config, alias, conn=conn)
                if op == "flatten" else compiler(running, config, alias)
            )
            conn.sql(f"SELECT * FROM ({running}) AS _probe LIMIT 0").fetchall()
        except Exception as exc:  # noqa: BLE001 — locating, not handling
            return (i + 1, str(step.get("label") or op), str(op), str(exc))
        idx += 1
    return None


def list_step_ops() -> list[str]:
    """Public introspection — used by tests and the frontend's add-step menu."""
    return list(_COMPILERS.keys())


# Backwards-compatible alias for the few seconds it took to rename — keeps
# any in-flight imports working without breakage. Safe to remove next cycle.
compile_recipe = compile_wrangle


# ─────────────────────────────────────────────────────────────────────────────
# DataWranglerNode
# ─────────────────────────────────────────────────────────────────────────────

@register(StepType.DATA_WRANGLER)
class DataWranglerNode(BaseNode):
    """Stepwise visible transform — one canvas node, N ordered sub-steps.

    Each sub-step is one of: filter, select, rename, cast, derive, group_by.
    See docs/design-data-wrangler-node.md for the sub-step JSON shape.
    """
    display_name = "Data Wrangler"
    category = "transform"
    description = (
        "Apply a sequence of small transformations in one tile — "
        "filter, rename, cast, derive, group — with per-step preview."
    )

    # Registered table name we wrap upstream input as. Kept in sync with the
    # compile_wrangle(input_table=...) argument below.
    _INPUT_TABLE = "__wrangler_input"

    def execute(self, ctx: ExecutionContext) -> "duckdb.DuckDBPyRelation":
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Data Wrangler node has no input data")
        source = inputs[0]
        steps = self.params.get("steps") or []

        # Register source FIRST so the flatten compiler can probe its
        # struct fields when a prefix is set. compile_wrangle's only
        # use of conn today is _compile_flatten's two-step prefix
        # resolution; other ops emit pure SQL with no conn use.
        #
        # Note (2026-05-19): we deliberately DO NOT unregister
        # __wrangler_input on the way out. ctx.conn.sql() returns a
        # lazy DuckDBPyRelation that resolves its source table on
        # .fetchall() / .count() — not at construction time. A previous
        # finally-block unregister produced "Table __wrangler_input
        # does not exist" the moment the caller tried to materialize
        # the result. The next DataWranglerNode invocation will simply
        # re-register the name with its own source via DuckDB's normal
        # rebind behaviour, so the leak is bounded to one entry.
        # Scope the input-table name per step. The returned relation is LAZY
        # over this name (see the note above about not unregistering it), so a
        # SECOND Data Wrangler downstream would otherwise re-register the shared
        # name with a relation that references itself → DuckDB "infinite
        # recursion detected". (The old "rebind is harmless" assumption below
        # only holds when the new source doesn't reference the same name.)
        input_table = ctx.register_scoped(self._INPUT_TABLE, source)
        sql = compile_wrangle(steps, input_table, conn=ctx.conn)
        # 2026-06-15 (debuggability): the recipe compiles to ONE fused query,
        # so a broken sub-step would otherwise surface as a single opaque
        # whole-node error. Eagerly bind-check the fused query (LIMIT 0 = bind
        # + type-check, no data scan, negligible cost). On failure, pin the
        # first failing sub-step so the error names it. (Pure runtime/value
        # errors on full data may still surface generically — use the node's
        # per-step preview, which runs each step on a sample.)
        try:
            ctx.conn.sql(f"SELECT * FROM ({sql}) AS _bind_check LIMIT 0").fetchall()
        except Exception as exc:  # noqa: BLE001
            loc = _locate_failing_step(ctx.conn, steps, input_table)
            if loc:
                n, label, op, err = loc
                raise ValueError(
                    f"Data Wrangler: sub-step {n} ('{label}', {op}) failed — {err}. "
                    f"Open the node and use the per-step preview to inspect it."
                ) from exc
            raise ValueError(f"Data Wrangler failed: {exc}") from exc
        return ctx.conn.sql(sql)

    # ── Edit-time preview ──
    #
    # Returns one entry per enabled sub-step (in order), each with the
    # cumulative row count + column schema after running steps[0..i] against
    # a capped sample. Used by the frontend DataWranglerConfig component.
    @classmethod
    def preview_steps(
        cls,
        conn: "duckdb.DuckDBPyConnection",
        source: "duckdb.DuckDBPyRelation",
        steps: list[dict],
        sample_rows: int = 100,
    ) -> dict[str, Any]:
        sample_rows = max(1, min(int(sample_rows or 100), 1000))

        # Cap upstream to the sample size in one go — this is the only place
        # we materialize a temp view; everything else is logical SQL.
        conn.register(cls._INPUT_TABLE, source)
        try:
            base = conn.sql(f"SELECT * FROM {cls._INPUT_TABLE} LIMIT {sample_rows}")
            conn.register("__wrangler_sample", base)
            try:
                # Build cumulative SQL after each enabled sub-step.
                per_step: list[dict[str, Any]] = []
                prev_columns: list[tuple[str, str]] = []
                base_cols = list(zip(base.columns, [str(t) for t in base.types]))

                # Fetch the first ~10 rows of input as sample_data for the UI.
                base_sample_rel = conn.sql(
                    "SELECT * FROM __wrangler_sample LIMIT 10"
                )
                base_rows = [
                    dict(zip(base.columns, r))
                    for r in base_sample_rel.fetchall()
                ]

                per_step.append({
                    "index": -1,
                    "label": "input",
                    "row_count": base.count("*").fetchone()[0],
                    "columns": [{"name": n, "type": t} for n, t in base_cols],
                    "schema_delta": {"added": [], "removed": [], "retyped": []},
                    "sample_data": base_rows,
                })
                prev_columns = base_cols

                idx = 0
                running_sql = "SELECT * FROM __wrangler_sample"
                for i, step in enumerate(steps or []):
                    if not isinstance(step, dict):
                        continue
                    if step.get("enabled") is False:
                        continue
                    op = step.get("op")
                    compiler = _COMPILERS.get(op)
                    if compiler is None:
                        continue
                    config = step.get("config") or {}
                    alias = f"_w{idx}"
                    # 2026-06-15 (debuggability): run each sub-step on the
                    # sample inside try/except. If one fails, mark THAT step
                    # with status=error + the message and STOP — so the user
                    # sees which step broke (and that the earlier ones were
                    # fine), instead of the whole preview blowing up.
                    try:
                        next_sql = (
                            compiler(running_sql, config, alias, conn=conn)
                            if op == "flatten" else compiler(running_sql, config, alias)
                        )
                        rel = conn.sql(next_sql)
                        cols = list(zip(rel.columns, [str(t) for t in rel.types]))
                        delta = _schema_delta(prev_columns, cols)
                        sample_rows_list = [
                            dict(zip(rel.columns, r))
                            for r in conn.sql(
                                f"SELECT * FROM ({next_sql}) AS _sample LIMIT 10"
                            ).fetchall()
                        ]
                        row_count = rel.count("*").fetchone()[0]
                    except Exception as exc:  # noqa: BLE001
                        per_step.append({
                            "index": i,
                            "op": op,
                            "label": step.get("label") or op,
                            "status": "error",
                            "error": str(exc),
                            "row_count": 0,
                            "columns": [],
                            "schema_delta": {"added": [], "removed": [], "retyped": []},
                            "sample_data": [],
                        })
                        break  # downstream steps build on this one — can't run
                    per_step.append({
                        "index": i,
                        "op": op,
                        "label": step.get("label") or op,
                        "status": "ok",
                        "row_count": row_count,
                        "columns": [{"name": n, "type": t} for n, t in cols],
                        "schema_delta": delta,
                        "sample_data": sample_rows_list,
                    })
                    running_sql = next_sql
                    prev_columns = cols
                    idx += 1

                # Compile final SQL against the real input table for display.
                # Pass conn so flatten-with-prefix shows the actual resolved
                # field projections instead of the un-prefixed fallback.
                generated_sql = compile_wrangle(steps, cls._INPUT_TABLE, conn=conn)
                return {
                    "sample_rows": sample_rows,
                    "steps": per_step,
                    "generated_sql": generated_sql,
                }
            finally:
                try:
                    conn.unregister("__wrangler_sample")
                except Exception:
                    pass
        finally:
            try:
                conn.unregister(cls._INPUT_TABLE)
            except Exception:
                pass

    @staticmethod
    def default_params() -> dict[str, Any]:
        # Start empty. The frontend's DataWranglerConfig renders a
        # StarterEmptyState when steps.length === 0, letting the user pick
        # which kind of step they actually want (filter / select / rename /
        # cast / derive / group_by) instead of silently committing to a
        # "Filter rows" step they didn't ask for. Consistent with the
        # 2026-05-09 no-silent-create rule for pipelines, applied here
        # to sub-steps.
        return {"steps": []}

    @staticmethod
    def param_schema() -> list[dict]:
        # The Data Wrangler node is configured by a dedicated
        # `DataWranglerConfig` component on the frontend; the schema-driven
        # fallback only sees a single opaque `steps` array. The ConfigPanel
        # routes DATA_WRANGLER to DataWranglerConfig.tsx via its hardcoded
        # UX table.
        return [
            {
                "name": "steps",
                "type": "wrangler_steps",
                "label": "Wrangler steps",
                "description": (
                    "Ordered list of sub-steps. Configured via the Data "
                    "Wrangler editor — drag to reorder, toggle to disable."
                ),
            },
        ]


# Backwards-compatible aliases for the previous name — safe to keep for one
# cycle and remove later. New code should import DataWranglerNode.
RecipeNode = DataWranglerNode


# ─────────────────────────────────────────────────────────────────────────────
# Schema delta helper
# ─────────────────────────────────────────────────────────────────────────────

def _schema_delta(
    prev: list[tuple[str, str]],
    curr: list[tuple[str, str]],
) -> dict[str, list]:
    """Compute column-level delta between two schemas.

    Returns dict with three keys:
      - added:   columns in curr not in prev (with type)
      - removed: column names in prev not in curr
      - retyped: columns in both whose type changed
    """
    prev_map = {n: t for n, t in prev}
    curr_map = {n: t for n, t in curr}
    added = [{"name": n, "type": t} for n, t in curr if n not in prev_map]
    removed = [n for n, _ in prev if n not in curr_map]
    retyped = [
        {"name": n, "from": prev_map[n], "to": curr_map[n]}
        for n in curr_map
        if n in prev_map and prev_map[n] != curr_map[n]
    ]
    return {"added": added, "removed": removed, "retyped": retyped}
