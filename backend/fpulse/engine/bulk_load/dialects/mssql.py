"""MS SQL Server bulk-load via BULK INSERT (CSV) or pyodbc fast_executemany.

Why MSSQL on the Gate 2 roadmap:
  * Common enterprise target (banking, insurance, retail back-office).
  * Native bulk path is `BULK INSERT FROM 'path' WITH (FORMAT='CSV', ...)`
    — but it requires the SQL Server process to read the file off
    its own filesystem, which doesn't work for remote installs.
  * Practical OSS path: `pyodbc` with `fast_executemany=True` — that
    batches per-row INSERTs into a single round-trip with TDS bulk
    semantics. 5-50× faster than naïve INSERT loops for typical row
    sizes; not as fast as native BULK INSERT but doesn't need
    server-side file access.

Pipeline (fast_executemany):
  1. Pull rows out of the DuckDB relation in batches of ~10k.
  2. `INSERT INTO target (col1, col2, ...) VALUES (?, ?, ...)` via
     `cursor.executemany(rows)`.
  3. Mode='merge' issues a temp table + MERGE statement keyed on
     `primary_key`.

Optional dependency: `pyodbc` + a Microsoft ODBC driver
(`ODBC Driver 18 for SQL Server` recommended). Listed under
`[project.optional-dependencies] mssql = [...]`.

Connection config keys (from F-Pulse `connections` store):
  host, port (default 1433), database, user, password,
  driver (default 'ODBC Driver 18 for SQL Server'),
  encrypt (default 'yes'), trust_server_certificate (default 'no').
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Iterable

from ..registry import register
from ..types import (
    BulkLoaderNotAvailable,
    BulkLoadRequest,
    BulkLoadResult,
)

logger = logging.getLogger(__name__)

_BATCH_SIZE_DEFAULT = 10_000


def _try_import_driver():
    try:
        import pyodbc  # type: ignore[import-not-found]
        return pyodbc
    except ImportError:
        return None


def _quote_ident(name: str) -> str:
    """MSSQL identifier: bracket-quoted; brackets escaped by doubling."""
    safe = str(name).replace("]", "]]")
    return f"[{safe}]"


def _qualified_table(schema_name: str, table: str) -> str:
    parts = table.split(".")
    if len(parts) == 1:
        return f"{_quote_ident(schema_name)}.{_quote_ident(parts[0])}"
    return ".".join(_quote_ident(p) for p in parts)


def _is_truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _build_connection_string(config: dict[str, Any]) -> str:
    """Build the ODBC connection string from F-Pulse credential config.

    Supports both auth modes:
      * SQL login   — UID/PWD from config['user'] / config['password'].
      * Windows / Active Directory integrated auth — Trusted_Connection.
        This is the default auth for most on-prem SQL Server installs,
        so it's selected automatically when no SQL login is provided
        (or explicitly via config['trusted_connection'] = true).
    """
    driver = config.get("driver", "ODBC Driver 18 for SQL Server")
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={config['host']},{int(config.get('port', 1433))}",
        f"DATABASE={config['database']}",
    ]
    # Decide auth: explicit trusted_connection flag wins; otherwise fall
    # back to Windows Auth when no SQL user was supplied.
    if "trusted_connection" in config:
        trusted = _is_truthy(config.get("trusted_connection"))
    else:
        trusted = not (config.get("user") or "").strip()
    if trusted:
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={config['user']}")
        parts.append(f"PWD={config['password']}")
    encrypt = config.get("encrypt", "yes")
    trust = config.get("trust_server_certificate", "no")
    parts.append(f"Encrypt={encrypt}")
    parts.append(f"TrustServerCertificate={trust}")
    return ";".join(parts)


def _iter_batches(rows: Iterable[tuple], batch_size: int) -> Iterable[list[tuple]]:
    """Yield batches of rows. Pure-Python — DuckDB's fetchall() returns a
    list, so we slice rather than re-iterate the cursor."""
    if isinstance(rows, list):
        for i in range(0, len(rows), batch_size):
            yield rows[i: i + batch_size]
        return
    batch: list[tuple] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


class MSSQLBulkLoader:
    """MSSQL fast_executemany bulk loader.

    Trades native BULK INSERT speed for remote-friendly transport. The
    `request.batch_size` knob controls the executemany chunk size; default
    10k rows fits comfortably in a single TDS message for typical row widths.
    """

    dialect = "mssql"
    method = "fast_executemany INSERT"

    def is_available(self) -> bool:
        return _try_import_driver() is not None

    def _connect(self, config: dict[str, Any]):
        driver = _try_import_driver()
        if driver is None:
            raise BulkLoaderNotAvailable(
                "mssql dialect requires pyodbc + an ODBC driver "
                "('ODBC Driver 18 for SQL Server' recommended)"
            )
        conn = driver.connect(_build_connection_string(config))
        # The whole point of this dialect — without this, executemany is
        # effectively a Python-side loop.
        try:
            conn.cursor().fast_executemany = True
        except AttributeError:
            # Older pyodbc — set on each cursor we open instead.
            pass
        return conn

    def _columns_and_rows(self, request: BulkLoadRequest) -> tuple[list[str], list[tuple]]:
        rel = request.relation
        if rel is None:
            raise ValueError("mssql: BulkLoadRequest.relation is required")
        cols = list(request.columns) if request.columns else list(rel.columns)
        rows = rel.fetchall()
        return cols, rows

    def _do_executemany(self, conn, target: str, cols: list[str], rows: list[tuple], batch_size: int) -> int:
        """Run executemany INSERT in batches. Returns rows_loaded."""
        if not rows:
            return 0
        cur = conn.cursor()
        try:
            try:
                cur.fast_executemany = True
            except AttributeError:
                pass
            placeholders = ", ".join(["?"] * len(cols))
            col_list = ", ".join(_quote_ident(c) for c in cols)
            sql = f"INSERT INTO {target} ({col_list}) VALUES ({placeholders})"
            total = 0
            for batch in _iter_batches(rows, batch_size):
                cur.executemany(sql, batch)
                total += len(batch)
            return total
        finally:
            cur.close()

    # ── Public load() ───────────────────────────────────────────────

    def load(self, request: BulkLoadRequest) -> BulkLoadResult:
        target = _qualified_table(request.schema_name or "dbo", request.table)
        batch_size = request.batch_size or _BATCH_SIZE_DEFAULT
        start = time.monotonic()

        cols, rows = self._columns_and_rows(request)
        conn = self._connect(request.config)
        try:
            if request.mode == "truncate":
                cur = conn.cursor()
                try:
                    cur.execute(f"TRUNCATE TABLE {target}")
                finally:
                    cur.close()

            if request.mode == "merge":
                if not request.primary_key:
                    raise ValueError("mssql: mode='merge' requires primary_key")
                # Stage to a temp table, then MERGE.
                stage_table = f"##fpulse_stage_{uuid.uuid4().hex[:8]}"  # global temp
                stage_q = stage_table  # global temps are referenced unqualified
                cur = conn.cursor()
                try:
                    # CREATE TABLE ... AS SELECT isn't supported in MSSQL; SELECT INTO is the equivalent.
                    cur.execute(f"SELECT TOP 0 * INTO {stage_q} FROM {target}")
                    rows_loaded = self._do_executemany(conn, stage_q, cols, rows, batch_size)
                    on_clause = " AND ".join(
                        f"T.{_quote_ident(k)} = S.{_quote_ident(k)}" for k in request.primary_key
                    )
                    set_clause = ", ".join(
                        f"T.{_quote_ident(c)} = S.{_quote_ident(c)}"
                        for c in cols if c not in request.primary_key
                    ) or "1=1"  # all-keys edge case: no non-key columns to update
                    insert_cols = ", ".join(_quote_ident(c) for c in cols)
                    insert_vals = ", ".join(f"S.{_quote_ident(c)}" for c in cols)
                    merge_sql = (
                        f"MERGE INTO {target} AS T USING {stage_q} AS S ON {on_clause}\n"
                        f"WHEN MATCHED THEN UPDATE SET {set_clause}\n"
                        f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals});"
                    )
                    cur.execute(merge_sql)
                finally:
                    try:
                        cur.execute(f"DROP TABLE IF EXISTS {stage_q}")
                    except Exception as exc:
                        logger.warning("mssql: failed to drop staging table: %s", exc)
                    cur.close()
            else:
                rows_loaded = self._do_executemany(conn, target, cols, rows, batch_size)

            conn.commit()
        finally:
            conn.close()

        elapsed = int((time.monotonic() - start) * 1000)
        return BulkLoadResult(
            rows_loaded=rows_loaded,
            duration_ms=elapsed,
            dialect=self.dialect,
            method=self.method,
            bytes_written=None,  # pyodbc executemany doesn't surface byte counts
        )


register(MSSQLBulkLoader())
