"""Tombstone partition helper (2026-06-08, B4 of backfill-ux-1.2).

When an incremental source has a soft-delete column (e.g. ``is_deleted``
boolean or ``deleted_at`` timestamp), rows flagged as deleted need
DIFFERENT downstream handling than live rows:

  * live rows -> ordinary upsert / append
  * tombstoned rows -> propagate as DELETE (or mark-deleted) at the sink

This module ships the pure split helper that the sink wire-in (B4.1,
deferred to per-dialect focused sessions) will call. Keeping the
partition logic separate from the per-dialect SQL generation means
the same predicate works across postgres / mssql / snowflake.

# What ships here (foundation)

  * ``is_tombstoned(row, column)`` - per-row predicate handling the
    common shapes: bool, int 0/1, ISO timestamp string ("deleted_at
    is non-null"), None / empty
  * ``partition_tombstones(rows, column)`` - splits a batch into
    (live, deleted) lists
  * ``extract_tombstone_keys(rows, key_columns)`` - pulls just the
    natural-key fields from deleted rows so the sink can issue a
    DELETE without re-sending the full row

# What's deferred to B4.1 / B4.2 (per-dialect, focused sessions)

  * postgres dialect: MERGE ... WHEN MATCHED AND tombstoned THEN DELETE
  * mssql dialect: explicit DELETE + INSERT pattern (MERGE has known
    correctness bugs per Microsoft docs)
  * snowflake dialect: native MERGE with DELETE branch
  * append sink: pass-through with tombstone marker carried (no DELETE)
  * CSV / file sinks: documentation that they can't express deletes;
    UI warning

These all touch load-bearing per-dialect SQL gen + need their own
test pins.
"""
from __future__ import annotations

from typing import Any, Iterable


def is_tombstoned(row: dict[str, Any], column: str) -> bool:
    """Single-row predicate. Returns True if the row's tombstone column
    indicates the row has been (soft-)deleted at the source.

    Handles the common source-column shapes:
      * Boolean: True/False
      * Integer flag: 1/0 (postgres int columns + mssql BIT)
      * Timestamp: any non-empty value = "deleted at this time"
        (matches the deleted_at convention used by ActiveRecord /
        Rails-style soft deletes)
      * String 'true'/'false'/'1'/'0'/'t'/'f' (some sources serialise
        booleans to text)
      * Missing column / None / empty string -> not tombstoned
    """
    if not column:
        return False
    if column not in row:
        return False
    value = row[column]
    if value is None:
        return False
    # Booleans first (isinstance(True, int) is True, so check bool first)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if not v:
            return False
        if v in ("false", "f", "no", "n", "0"):
            return False
        # Anything else - including ISO timestamps - counts as
        # "the column has a value, so the row is tombstoned"
        return True
    # Other types (bytes / datetime) - presence = tombstoned
    return True


def partition_tombstones(
    rows: Iterable[dict[str, Any]],
    column: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ``rows`` into (live_rows, tombstoned_rows) based on the
    tombstone column. Both lists preserve input order.

    Returns the input as a single live list when ``column`` is empty
    (the common case - most sources have no tombstone column declared,
    and we don't want to scan every row when we'd know upfront the
    predicate would never fire).
    """
    if not column:
        # Fast path: no tombstone column declared; everything is live.
        return list(rows), []
    live: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    for row in rows:
        if is_tombstoned(row, column):
            deleted.append(row)
        else:
            live.append(row)
    return live, deleted


_DIALECT_PARAM = {
    # DB-API paramstyle per dialect driver.
    "postgres": "%s", "postgresql": "%s",
    "snowflake": "%s",
    "mssql": "?", "sqlserver": "?",
    "duckdb": "?",
    "bigquery": "?",
    "redshift": "%s",
}


def _quote_ident_for(dialect: str, name: str) -> str:
    """Dialect-aware identifier quoter. SQL Server uses [brackets];
    everyone else uses ANSI "double quotes". Doubles the closing
    delimiter to defeat injection / reserved-word edge cases."""
    d = (dialect or "").lower()
    n = str(name)
    if d in ("mssql", "sqlserver"):
        return "[" + n.replace("]", "]]") + "]"
    return '"' + n.replace('"', '""') + '"'


def build_delete_sql(
    dialect: str,
    table: str,
    key_columns: list[str],
    n_rows: int = 1,
) -> str:
    """B4.1 (2026-06-08) - build a parameterized DELETE for tombstone
    propagation. Returns the SQL string with placeholders; the caller
    binds the flattened key values (see ``flatten_delete_params``).

    * Single key column -> ``DELETE FROM t WHERE "k" IN (?, ?, ...)``
    * Composite key     -> ``DELETE FROM t WHERE ("a" = ? AND "b" = ?)
                            OR ("a" = ? AND "b" = ?) ...`` (the OR-of-ANDs
      form works on every dialect, unlike row-value IN which SQL Server
      rejects).

    Placeholder style is dialect-correct (``%s`` for psycopg2 /
    snowflake / redshift, ``?`` for pyodbc / duckdb). Identifier quoting
    is dialect-correct (brackets for mssql, double-quotes otherwise).

    Raises ValueError on empty key_columns or n_rows < 1. Pure string
    generation - no DB connection. The execute() wiring that runs this
    against a live connection is [LIVE-SMOKE]: verify against a real
    database before relying on delete propagation.
    """
    if not key_columns:
        raise ValueError("build_delete_sql requires at least one key column")
    if n_rows < 1:
        raise ValueError("build_delete_sql requires n_rows >= 1")
    d = (dialect or "").lower()
    ph = _DIALECT_PARAM.get(d)
    if ph is None:
        raise ValueError("build_delete_sql: unknown dialect " + repr(dialect))

    qtable = ".".join(_quote_ident_for(d, p) for p in str(table).split("."))

    if len(key_columns) == 1:
        col = _quote_ident_for(d, key_columns[0])
        placeholders = ", ".join([ph] * n_rows)
        return "DELETE FROM " + qtable + " WHERE " + col + " IN (" + placeholders + ")"

    # Composite key - OR-of-ANDs, portable across dialects.
    cols = [_quote_ident_for(d, c) for c in key_columns]
    one = "(" + " AND ".join(c + " = " + ph for c in cols) + ")"
    clause = " OR ".join([one] * n_rows)
    return "DELETE FROM " + qtable + " WHERE " + clause


def flatten_delete_params(
    keys: list[dict[str, Any]],
    key_columns: list[str],
) -> list[Any]:
    """Flatten the per-row key dicts into the positional parameter list
    matching the placeholders produced by ``build_delete_sql``. Order is
    row-major, column order following ``key_columns``."""
    out: list[Any] = []
    for row in keys:
        for c in key_columns:
            out.append(row.get(c))
    return out


def extract_tombstone_keys(
    rows: list[dict[str, Any]],
    key_columns: list[str],
) -> list[dict[str, Any]]:
    """Pull just the natural-key fields out of each tombstoned row so
    the sink can issue a DELETE ``WHERE`` clause without re-sending
    the row's full payload. Skips rows missing any key column (those
    are operator-config errors the sink wire-in will surface
    separately).

    ``key_columns`` is the same list the sink's ``merge_key`` (B2)
    uses for upserts - same identity, different operation.
    """
    if not key_columns:
        return []
    keys: list[dict[str, Any]] = []
    for row in rows:
        if all(c in row for c in key_columns):
            keys.append({c: row[c] for c in key_columns})
    return keys
