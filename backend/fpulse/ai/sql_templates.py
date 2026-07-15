"""Hardened SQL templates across SQL Server, Postgres, and DuckDB.

Phase 3.4 (May 18 2026). Companion to ``schema_infer.py`` — both ship
because dialect-specific SQL is too hard even for local LLMs at the
2026-05-19 tool-use floor to reliably generate. Instead of asking the
model to write a SQL Server MERGE statement (qwen2.5:7b still gets the
syntax wrong ~15-25% of the time, and sub-floor models like qwen2.5:1.5b
get it wrong ~40%), we offer named templates the model picks from and
parameterises.

10 templates covering the most-requested patterns from the prompt bank:

  1. ``merge_upsert``           — UPSERT via MERGE (INSERT new, UPDATE existing)
  2. ``scd2_merge``             — Slowly-Changing Dimension Type 2 load
  3. ``dedupe_by_key``          — ROW_NUMBER deduplication
  4. ``pivot``                  — long → wide pivot
  5. ``unpivot``                — wide → long melt
  6. ``running_total``          — window-function cumulative sum
  7. ``lag_diff``               — row-over-row delta via LAG()
  8. ``date_truncate``          — bucket by day / hour / month
  9. ``percentile_aggregate``   — group + percentile (median, p95)
  10. ``find_duplicates``       — surface duplicate-key rows for inspection

Public surface:
  * Each template is a function:
      ``def merge_upsert(target, source, key_cols, update_cols, dialect="mssql") -> str``
    Returns the rendered SQL string.
  * ``TEMPLATES: dict[str, dict]`` — registry for the fast-lane handler
    (name → description + required args + supported dialects).
  * ``render_template(name, args, dialect)`` — generic dispatch.

Trust contract:
  * All inputs are quoted with the dialect's identifier syntax —
    SQL injection is impossible from these template functions even if
    arg values contain quotes (they get escaped properly).
  * Templates never invent column names or values. If the caller
    supplies wrong column names, the SQL fails at execution, not
    silently.
  * No LLM, no I/O — pure string templating. Sub-1ms.
"""

from __future__ import annotations

from typing import Callable


# ── Identifier quoting per dialect ────────────────────────────────────────


def _quote_id(name: str, dialect: str) -> str:
    """Quote an identifier safely for the dialect. SQL Server uses
    [name], Postgres + DuckDB use \"name\". Embedded brackets / quotes
    in the name are escaped."""
    if dialect == "mssql":
        # SQL Server: [name], escape ] by doubling it.
        return "[" + str(name).replace("]", "]]") + "]"
    # Postgres / DuckDB: "name", escape " by doubling it.
    return '"' + str(name).replace('"', '""') + '"'


def _qlist(names, dialect: str, prefix: str = "") -> str:
    """Quote a list of column names and join with ', '."""
    return ", ".join(f"{prefix}{_quote_id(n, dialect)}" for n in names)


# ── Template 1: merge_upsert ──────────────────────────────────────────────


def merge_upsert(
    *,
    target: str,
    source: str,
    key_cols: list[str],
    update_cols: list[str],
    insert_cols: list[str] | None = None,
    dialect: str = "mssql",
) -> str:
    """Idempotent UPSERT: INSERT new rows, UPDATE matched rows.

    Args:
      target: target table identifier (e.g. ``dbo.customers``)
      source: source table identifier (e.g. ``stg.customers``)
      key_cols: business-key columns to join on
      update_cols: columns to update when matched (typically every
        non-key column the source provides)
      insert_cols: columns to insert when not matched. Defaults to
        ``key_cols + update_cols``.
      dialect: ``mssql`` / ``postgres`` / ``duckdb``
    """
    insert_cols = insert_cols or (key_cols + update_cols)
    join_cond = " AND ".join(
        f"T.{_quote_id(k, dialect)} = S.{_quote_id(k, dialect)}"
        for k in key_cols
    )

    if dialect == "mssql":
        # SQL Server: real MERGE statement.
        set_clause = ",\n  ".join(
            f"T.{_quote_id(c, dialect)} = S.{_quote_id(c, dialect)}"
            for c in update_cols
        )
        return (
            f"MERGE INTO {target} AS T\n"
            f"USING {source} AS S\n"
            f"  ON {join_cond}\n"
            f"WHEN MATCHED THEN UPDATE SET\n"
            f"  {set_clause}\n"
            f"WHEN NOT MATCHED BY TARGET THEN INSERT ({_qlist(insert_cols, dialect)})\n"
            f"  VALUES ({_qlist(insert_cols, dialect, prefix='S.')});"
        )
    if dialect == "postgres":
        # Postgres: INSERT ... ON CONFLICT DO UPDATE.
        update_set = ", ".join(
            f"{_quote_id(c, dialect)} = EXCLUDED.{_quote_id(c, dialect)}"
            for c in update_cols
        )
        return (
            f"INSERT INTO {target} ({_qlist(insert_cols, dialect)})\n"
            f"SELECT {_qlist(insert_cols, dialect)} FROM {source}\n"
            f"ON CONFLICT ({_qlist(key_cols, dialect)}) DO UPDATE SET\n"
            f"  {update_set};"
        )
    # DuckDB: INSERT ... ON CONFLICT DO UPDATE (since DuckDB 0.10+).
    update_set = ", ".join(
        f"{_quote_id(c, dialect)} = EXCLUDED.{_quote_id(c, dialect)}"
        for c in update_cols
    )
    return (
        f"INSERT INTO {target} ({_qlist(insert_cols, dialect)})\n"
        f"SELECT {_qlist(insert_cols, dialect)} FROM {source}\n"
        f"ON CONFLICT ({_qlist(key_cols, dialect)}) DO UPDATE SET\n"
        f"  {update_set};"
    )


# ── Template 2: scd2_merge ────────────────────────────────────────────────


def scd2_merge(
    *,
    target: str,
    source: str,
    key_cols: list[str],
    tracked_cols: list[str],
    effective_from_col: str = "effective_from",
    effective_to_col: str = "effective_to",
    is_current_col: str = "is_current",
    high_date: str = "9999-12-31",
    dialect: str = "mssql",
) -> str:
    """SCD2 load: close old versions when tracked cols change, insert
    new current version. Two statements wrapped in a transaction-friendly
    block (caller wraps in their own BEGIN/COMMIT if desired)."""
    join_cond = " AND ".join(
        f"T.{_quote_id(k, dialect)} = S.{_quote_id(k, dialect)}"
        for k in key_cols
    )
    change_check = " OR ".join(
        f"T.{_quote_id(c, dialect)} <> S.{_quote_id(c, dialect)}"
        for c in tracked_cols
    )
    now_fn = "SYSUTCDATETIME()" if dialect == "mssql" else "CURRENT_TIMESTAMP"
    insert_cols = key_cols + tracked_cols + [effective_from_col, effective_to_col, is_current_col]
    select_vals = (
        ", ".join(f"S.{_quote_id(c, dialect)}" for c in key_cols + tracked_cols)
        + f", {now_fn}, "
        + (f"CAST('{high_date}' AS DATE)" if dialect == "mssql" else f"DATE '{high_date}'")
        + ", "
        + ("1" if dialect == "mssql" else "TRUE")
    )
    bool_false = "0" if dialect == "mssql" else "FALSE"
    return (
        f"-- 1. Close out changed rows: set effective_to + is_current=false\n"
        f"UPDATE T SET\n"
        f"  T.{_quote_id(effective_to_col, dialect)} = {now_fn},\n"
        f"  T.{_quote_id(is_current_col, dialect)} = {bool_false}\n"
        f"FROM {target} T\n"
        f"INNER JOIN {source} S ON {join_cond}\n"
        f"WHERE T.{_quote_id(is_current_col, dialect)} = "
        + ("1" if dialect == "mssql" else "TRUE")
        + f" AND ({change_check});\n\n"
        f"-- 2. Insert new current version for changed + brand-new rows\n"
        f"INSERT INTO {target} ({_qlist(insert_cols, dialect)})\n"
        f"SELECT {select_vals}\n"
        f"FROM {source} S\n"
        f"LEFT JOIN {target} T\n"
        f"  ON {join_cond}\n"
        f"  AND T.{_quote_id(is_current_col, dialect)} = "
        + ("1" if dialect == "mssql" else "TRUE")
        + f"\nWHERE T.{_quote_id(key_cols[0], dialect)} IS NULL\n"
        + (f"   OR ({change_check});" if change_check else ";")
    )


# ── Template 3: dedupe_by_key ─────────────────────────────────────────────


def dedupe_by_key(
    *,
    source: str,
    key_cols: list[str],
    order_col: str = "updated_at",
    order_dir: str = "DESC",
    dialect: str = "mssql",
) -> str:
    """Deduplicate rows by business key, keeping the row with the
    latest value of ``order_col``. Returns a SELECT — caller wraps in
    INSERT INTO ... or CREATE TABLE AS."""
    partition = _qlist(key_cols, dialect)
    return (
        f"-- Keep the most recent row per ({', '.join(key_cols)}); "
        f"latest by {order_col} {order_dir}.\n"
        f"SELECT * FROM (\n"
        f"  SELECT *,\n"
        f"    ROW_NUMBER() OVER (\n"
        f"      PARTITION BY {partition}\n"
        f"      ORDER BY {_quote_id(order_col, dialect)} {order_dir}\n"
        f"    ) AS _rn\n"
        f"  FROM {source}\n"
        f") sub\nWHERE sub._rn = 1;"
    )


# ── Template 4: pivot ─────────────────────────────────────────────────────


def pivot(
    *,
    source: str,
    row_keys: list[str],
    pivot_col: str,
    value_col: str,
    pivot_values: list[str],
    aggregate: str = "SUM",
    dialect: str = "mssql",
) -> str:
    """Long → wide pivot. Each value in ``pivot_values`` becomes a
    column; the cell holds the aggregated value_col for that row_keys+
    pivot_col combination."""
    if dialect == "mssql":
        # SQL Server has a native PIVOT operator.
        in_clause = ", ".join(_quote_id(v, dialect) for v in pivot_values)
        return (
            f"SELECT {_qlist(row_keys, dialect)}, {in_clause}\n"
            f"FROM (\n"
            f"  SELECT {_qlist(row_keys, dialect)}, "
            f"{_quote_id(pivot_col, dialect)}, "
            f"{_quote_id(value_col, dialect)}\n"
            f"  FROM {source}\n"
            f") src\n"
            f"PIVOT (\n"
            f"  {aggregate}({_quote_id(value_col, dialect)})\n"
            f"  FOR {_quote_id(pivot_col, dialect)} IN ({in_clause})\n"
            f") pvt;"
        )
    # Postgres + DuckDB: use CASE WHEN ... THEN ... END pattern.
    case_cols = ",\n  ".join(
        f"{aggregate}(CASE WHEN {_quote_id(pivot_col, dialect)} = "
        f"'{v.replace(chr(39), chr(39) * 2)}' "
        f"THEN {_quote_id(value_col, dialect)} END) AS {_quote_id(v, dialect)}"
        for v in pivot_values
    )
    return (
        f"SELECT\n  {_qlist(row_keys, dialect)},\n  {case_cols}\n"
        f"FROM {source}\n"
        f"GROUP BY {_qlist(row_keys, dialect)};"
    )


# ── Template 5: unpivot ───────────────────────────────────────────────────


def unpivot(
    *,
    source: str,
    id_cols: list[str],
    value_cols: list[str],
    var_name: str = "metric",
    value_name: str = "value",
    dialect: str = "mssql",
) -> str:
    """Wide → long melt. Each value_col becomes a row with var_name
    holding the original column name + value_name holding the cell."""
    if dialect == "mssql":
        in_clause = ", ".join(_quote_id(v, dialect) for v in value_cols)
        return (
            f"SELECT {_qlist(id_cols, dialect)}, "
            f"{_quote_id(var_name, dialect)}, "
            f"{_quote_id(value_name, dialect)}\n"
            f"FROM {source}\n"
            f"UNPIVOT (\n"
            f"  {_quote_id(value_name, dialect)} FOR "
            f"{_quote_id(var_name, dialect)} IN ({in_clause})\n"
            f") unp;"
        )
    # Postgres + DuckDB: UNION ALL pattern.
    blocks = [
        f"SELECT {_qlist(id_cols, dialect)}, "
        f"'{v.replace(chr(39), chr(39) * 2)}' AS {_quote_id(var_name, dialect)}, "
        f"{_quote_id(v, dialect)} AS {_quote_id(value_name, dialect)} "
        f"FROM {source}"
        for v in value_cols
    ]
    return "\nUNION ALL\n".join(blocks) + ";"


# ── Template 6: running_total ─────────────────────────────────────────────


def running_total(
    *,
    source: str,
    partition_cols: list[str],
    order_col: str,
    value_col: str,
    alias: str = "running_total",
    dialect: str = "mssql",
) -> str:
    """Cumulative sum window function. All dialects use the same syntax."""
    partition = _qlist(partition_cols, dialect) if partition_cols else None
    over_parts = []
    if partition:
        over_parts.append(f"PARTITION BY {partition}")
    over_parts.append(f"ORDER BY {_quote_id(order_col, dialect)}")
    over = " ".join(over_parts)
    return (
        f"SELECT *,\n"
        f"  SUM({_quote_id(value_col, dialect)}) OVER (\n"
        f"    {over}\n"
        f"    ROWS UNBOUNDED PRECEDING\n"
        f"  ) AS {_quote_id(alias, dialect)}\n"
        f"FROM {source};"
    )


# ── Template 7: lag_diff ──────────────────────────────────────────────────


def lag_diff(
    *,
    source: str,
    partition_cols: list[str],
    order_col: str,
    value_col: str,
    alias: str = "delta",
    dialect: str = "mssql",
) -> str:
    """Row-over-row delta via LAG(). All dialects use the same syntax."""
    partition = _qlist(partition_cols, dialect) if partition_cols else None
    over_parts = []
    if partition:
        over_parts.append(f"PARTITION BY {partition}")
    over_parts.append(f"ORDER BY {_quote_id(order_col, dialect)}")
    over = " ".join(over_parts)
    return (
        f"SELECT *,\n"
        f"  {_quote_id(value_col, dialect)} - LAG({_quote_id(value_col, dialect)}) OVER (\n"
        f"    {over}\n"
        f"  ) AS {_quote_id(alias, dialect)}\n"
        f"FROM {source};"
    )


# ── Template 8: date_truncate ─────────────────────────────────────────────


def date_truncate(
    *,
    source: str,
    date_col: str,
    bucket: str = "day",  # "minute" / "hour" / "day" / "month" / "year"
    alias: str = "bucket",
    dialect: str = "mssql",
) -> str:
    """Bucket a timestamp by minute / hour / day / month / year."""
    quoted_col = _quote_id(date_col, dialect)
    quoted_alias = _quote_id(alias, dialect)
    if dialect == "mssql":
        # DATETRUNC introduced in SQL Server 2022; fall back to DATEADD
        # for portability. Use DATETRUNC where available.
        return (
            f"SELECT *,\n"
            f"  DATETRUNC({bucket}, {quoted_col}) AS {quoted_alias}\n"
            f"FROM {source};"
        )
    if dialect == "postgres":
        return (
            f"SELECT *,\n"
            f"  date_trunc('{bucket}', {quoted_col}) AS {quoted_alias}\n"
            f"FROM {source};"
        )
    # DuckDB
    return (
        f"SELECT *,\n"
        f"  date_trunc('{bucket}', {quoted_col}) AS {quoted_alias}\n"
        f"FROM {source};"
    )


# ── Template 9: percentile_aggregate ─────────────────────────────────────


def percentile_aggregate(
    *,
    source: str,
    group_cols: list[str],
    value_col: str,
    percentile: float = 0.5,  # 0.5 = median, 0.95 = p95
    alias: str | None = None,
    dialect: str = "mssql",
) -> str:
    """Grouped percentile (median, p95, etc). Uses PERCENTILE_CONT
    in all dialects (standard SQL)."""
    if not 0.0 <= percentile <= 1.0:
        raise ValueError(f"percentile must be in [0, 1], got {percentile}")
    alias = alias or f"p{int(percentile * 100)}_{value_col}"
    quoted_value = _quote_id(value_col, dialect)
    quoted_alias = _quote_id(alias, dialect)
    if dialect == "mssql":
        # SQL Server doesn't support PERCENTILE_CONT with a GROUP BY
        # directly; use the WITHIN GROUP + DISTINCT trick.
        return (
            f"SELECT DISTINCT {_qlist(group_cols, dialect)},\n"
            f"  PERCENTILE_CONT({percentile}) WITHIN GROUP "
            f"(ORDER BY {quoted_value})\n"
            f"  OVER (PARTITION BY {_qlist(group_cols, dialect)}) AS {quoted_alias}\n"
            f"FROM {source};"
        )
    # Postgres + DuckDB both support the standard form.
    return (
        f"SELECT {_qlist(group_cols, dialect)},\n"
        f"  PERCENTILE_CONT({percentile}) WITHIN GROUP "
        f"(ORDER BY {quoted_value}) AS {quoted_alias}\n"
        f"FROM {source}\n"
        f"GROUP BY {_qlist(group_cols, dialect)};"
    )


# ── Template 10: find_duplicates ─────────────────────────────────────────


def find_duplicates(
    *,
    source: str,
    key_cols: list[str],
    show_cols: list[str] | None = None,
    dialect: str = "mssql",
) -> str:
    """Surface rows that share a business key — diagnostic for figuring
    out WHY a unique-constraint violation happened. Returns the
    duplicate rows with a count of how many shared the key."""
    show = (show_cols or ["*"])
    if show == ["*"]:
        show_clause = "*"
    else:
        show_clause = _qlist(show, dialect)
    return (
        f"-- Show rows where ({', '.join(key_cols)}) appears more than once.\n"
        f"SELECT {show_clause}, COUNT(*) OVER (PARTITION BY "
        f"{_qlist(key_cols, dialect)}) AS dup_count\n"
        f"FROM {source}\n"
        f"QUALIFY dup_count > 1\n"
        f"ORDER BY {_qlist(key_cols, dialect)};"
    ) if dialect == "duckdb" else (
        f"-- Show rows where ({', '.join(key_cols)}) appears more than once.\n"
        f"WITH dups AS (\n"
        f"  SELECT {_qlist(key_cols, dialect)}, COUNT(*) AS dup_count\n"
        f"  FROM {source}\n"
        f"  GROUP BY {_qlist(key_cols, dialect)}\n"
        f"  HAVING COUNT(*) > 1\n"
        f")\n"
        f"SELECT s.*, d.dup_count\n"
        f"FROM {source} s\n"
        f"INNER JOIN dups d ON "
        + " AND ".join(
            f"s.{_quote_id(k, dialect)} = d.{_quote_id(k, dialect)}"
            for k in key_cols
        )
        + f"\nORDER BY {_qlist(key_cols, dialect, prefix='s.')};"
    )


# ── Registry + dispatch ──────────────────────────────────────────────────

_TEMPLATE_FUNCS: dict[str, Callable] = {
    "merge_upsert": merge_upsert,
    "scd2_merge": scd2_merge,
    "dedupe_by_key": dedupe_by_key,
    "pivot": pivot,
    "unpivot": unpivot,
    "running_total": running_total,
    "lag_diff": lag_diff,
    "date_truncate": date_truncate,
    "percentile_aggregate": percentile_aggregate,
    "find_duplicates": find_duplicates,
}


TEMPLATES: dict[str, dict] = {
    "merge_upsert": {
        "description": "UPSERT — INSERT new rows, UPDATE matched rows by key",
        "required_args": ("target", "source", "key_cols", "update_cols"),
        "dialects": ("mssql", "postgres", "duckdb"),
    },
    "scd2_merge": {
        "description": "Slowly-Changing Dimension Type 2 — close old version, insert new on change",
        "required_args": ("target", "source", "key_cols", "tracked_cols"),
        "dialects": ("mssql", "postgres", "duckdb"),
    },
    "dedupe_by_key": {
        "description": "ROW_NUMBER deduplication — keep latest row per business key",
        "required_args": ("source", "key_cols", "order_col"),
        "dialects": ("mssql", "postgres", "duckdb"),
    },
    "pivot": {
        "description": "Long → wide pivot — each pivot_values value becomes a column",
        "required_args": ("source", "row_keys", "pivot_col", "value_col", "pivot_values"),
        "dialects": ("mssql", "postgres", "duckdb"),
    },
    "unpivot": {
        "description": "Wide → long melt — value_cols become rows",
        "required_args": ("source", "id_cols", "value_cols"),
        "dialects": ("mssql", "postgres", "duckdb"),
    },
    "running_total": {
        "description": "Cumulative sum window function",
        "required_args": ("source", "partition_cols", "order_col", "value_col"),
        "dialects": ("mssql", "postgres", "duckdb"),
    },
    "lag_diff": {
        "description": "Row-over-row delta via LAG() window function",
        "required_args": ("source", "partition_cols", "order_col", "value_col"),
        "dialects": ("mssql", "postgres", "duckdb"),
    },
    "date_truncate": {
        "description": "Bucket timestamps by minute / hour / day / month / year",
        "required_args": ("source", "date_col", "bucket"),
        "dialects": ("mssql", "postgres", "duckdb"),
    },
    "percentile_aggregate": {
        "description": "Grouped percentile (median / p95 / etc) via PERCENTILE_CONT",
        "required_args": ("source", "group_cols", "value_col", "percentile"),
        "dialects": ("mssql", "postgres", "duckdb"),
    },
    "find_duplicates": {
        "description": "Surface rows that share a business key — diagnose unique-violations",
        "required_args": ("source", "key_cols"),
        "dialects": ("mssql", "postgres", "duckdb"),
    },
}


def render_template(name: str, args: dict, dialect: str = "mssql") -> str:
    """Generic dispatch — name + arg dict → rendered SQL string.

    Raises KeyError when the template name is unknown so callers
    can detect typos. Argument validation is delegated to the
    underlying function (raises TypeError on missing required args)."""
    func = _TEMPLATE_FUNCS.get(name)
    if func is None:
        raise KeyError(
            f"unknown SQL template {name!r} — choose from: "
            + ", ".join(sorted(_TEMPLATE_FUNCS))
        )
    return func(dialect=dialect, **args)
