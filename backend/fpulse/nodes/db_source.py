"""
Database Source node — reads data from real databases via saved Connections.

Supports: PostgreSQL, MySQL, MSSQL, SQLite, or DuckDB in-memory SQL.
Uses saved Connections and Credentials from the connection store.
Falls back to DuckDB in-memory if no connection is specified.
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for the execute() return-type annotation.
# The node runs queries against real DBs (psycopg2/pymysql/etc.) and
# only passes the result back through ctx.conn — no direct duckdb use.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register


def _get_connection_config(connection_id: str) -> tuple[dict, str] | None:
    """Fetch connection config + type from app_state."""
    try:
        from fpulse.main import app_state
        conn_store = app_state.get("connection_store")
        cred_store = app_state.get("credential_store")
        if not conn_store:
            return None

        connection = conn_store.get(connection_id)
        if not connection:
            return None

        config = dict(connection.config or {})

        # Merge credential secrets
        if connection.credential_id and cred_store:
            cred = cred_store.get_raw(connection.credential_id)
            if cred and cred.config:
                config.update(cred.config)

        return config, connection.type
    except Exception:
        return None


# ── Per-table column introspection ──
#
# Used by the Mapping tab's "Import destination schema" button.
# Returns each column's name + DB type + nullability for a given
# (connection, schema, table) so the UI can render a dropdown of
# real destination columns with their data types.

def describe_table_columns(
    conn_type: str,
    config: dict,
    schema: str,
    table: str,
) -> list[dict]:
    """Introspect a table's columns. Returns list of
    ``{name: str, type: str, nullable: bool}``.

    Raises ValueError if the connection type isn't supported by this
    helper or if the underlying driver fails (caller surfaces the
    error to the UI as a toast).
    """
    if not table:
        raise ValueError("Table name is required")

    ct = (conn_type or "").lower().strip()

    if ct == "postgresql":
        import psycopg2  # type: ignore
        conn = psycopg2.connect(
            host=config.get("host"), port=config.get("port") or 5432,
            dbname=config.get("database"),
            user=config.get("user") or config.get("username"),
            password=config.get("password"),
            connect_timeout=10,
        )
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (schema or "public", table),
            )
            return [
                {"name": r[0], "type": r[1], "nullable": (r[2] == "YES")}
                for r in cur.fetchall()
            ]
        finally:
            conn.close()

    if ct == "mysql":
        import pymysql  # type: ignore
        conn = pymysql.connect(
            host=config.get("host"), port=int(config.get("port") or 3306),
            database=config.get("database"),
            user=config.get("user") or config.get("username"),
            password=config.get("password"),
            connect_timeout=10,
        )
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION",
                (schema or config.get("database"), table),
            )
            return [
                {"name": r[0], "type": r[1], "nullable": (r[2] == "YES")}
                for r in cur.fetchall()
            ]
        finally:
            conn.close()

    if ct == "mssql":
        import pyodbc  # type: ignore
        host = config.get("host")
        port = config.get("port") or 1433
        database = config.get("database")
        user = config.get("user") or config.get("username")
        password = config.get("password")
        conn_str = (
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={host},{port};DATABASE={database};"
            f"UID={user};PWD={password};Connection Timeout=10;"
        )
        conn = pyodbc.connect(conn_str)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? "
                "ORDER BY ORDINAL_POSITION",
                (schema or "dbo", table),
            )
            return [
                {"name": r[0], "type": r[1], "nullable": (r[2] == "YES")}
                for r in cur.fetchall()
            ]
        finally:
            conn.close()

    if ct == "sqlite":
        db_path = config.get("database") or config.get("file")
        if not db_path:
            raise ValueError("SQLite connection has no database path")
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            # PRAGMA returns: cid, name, type, notnull, dflt_value, pk
            cur.execute(f'PRAGMA table_info("{table}")')
            rows = cur.fetchall()
            return [
                {"name": r[1], "type": r[2] or "TEXT", "nullable": not bool(r[3])}
                for r in rows
            ]
        finally:
            conn.close()

    raise ValueError(
        f"Column introspection not supported for connection type '{conn_type}'. "
        f"Supported: postgresql, mysql, mssql, sqlite."
    )


# ── Canonical schema introspection (richer than describe_table_columns) ──
#
# Reads the same Postgres ``information_schema.columns`` view but pulls
# every parameter the canonical type system cares about (precision,
# scale, length, datetime precision, udt_name, ordinal_position) and
# returns a ``CanonicalSchema`` instead of a flat list of dicts.
#
# Wired into the runtime so DbSource emits both a DuckDB relation AND a
# CanonicalSchema sidecar that drives drift detection, the Mapping tab
# cast-safety glyph, and the sink-side ``to_postgres`` writer.

def describe_table_canonical(
    conn_type: str,
    config: dict,
    schema: str,
    table: str,
):
    """Introspect a table → ``CanonicalSchema``.

    Wired dialects (2026-05-22): postgresql, mssql, mysql, oracle,
    sqlite. Each branch imports its driver lazily so test code that
    never touches a given dialect doesn't need that driver installed.
    Unsupported dialects raise ``NotImplementedError`` with the list of
    supported types in the message.
    """
    if not table:
        raise ValueError("Table name is required")
    ct = (conn_type or "").lower().strip()

    if ct == "postgresql":
        import psycopg2  # type: ignore
        from fpulse.types import (
            CANONICAL_COLUMN_QUERY,
            postgres_columns_to_canonical,
        )
        conn = psycopg2.connect(
            host=config.get("host"), port=config.get("port") or 5432,
            dbname=config.get("database"),
            user=config.get("user") or config.get("username"),
            password=config.get("password"),
            connect_timeout=10,
        )
        try:
            cur = conn.cursor()
            cur.execute(CANONICAL_COLUMN_QUERY, (schema or "public", table))
            col_names = [d[0] for d in cur.description]
            rows = [dict(zip(col_names, r)) for r in cur.fetchall()]
            return postgres_columns_to_canonical(rows)
        finally:
            conn.close()

    if ct in {"mssql", "sqlserver", "mssqlserver"}:
        import pyodbc  # type: ignore
        from fpulse.types.from_mssql import (
            CANONICAL_COLUMN_QUERY_MSSQL,
            mssql_columns_to_canonical,
        )
        # Driver string is operator-controllable; default to "ODBC Driver 18".
        driver = config.get("odbc_driver") or "ODBC Driver 18 for SQL Server"
        cs = (
            f"DRIVER={{{driver}}};"
            f"SERVER={config.get('host')},{config.get('port') or 1433};"
            f"DATABASE={config.get('database')};"
            f"UID={config.get('user') or config.get('username')};"
            f"PWD={config.get('password')};"
            f"TrustServerCertificate=yes;"
        )
        conn = pyodbc.connect(cs, timeout=10)
        try:
            cur = conn.cursor()
            cur.execute(CANONICAL_COLUMN_QUERY_MSSQL, (schema or "dbo", table))
            col_names = [d[0] for d in cur.description]
            rows = [dict(zip(col_names, r)) for r in cur.fetchall()]
            return mssql_columns_to_canonical(rows)
        finally:
            conn.close()

    if ct == "mysql":
        import pymysql  # type: ignore
        from fpulse.types.from_mysql import (
            CANONICAL_COLUMN_QUERY_MYSQL,
            mysql_columns_to_canonical,
        )
        conn = pymysql.connect(
            host=config.get("host"), port=int(config.get("port") or 3306),
            database=config.get("database"),
            user=config.get("user") or config.get("username"),
            password=config.get("password"),
            connect_timeout=10,
        )
        try:
            cur = conn.cursor()
            cur.execute(
                CANONICAL_COLUMN_QUERY_MYSQL,
                (schema or config.get("database"), table),
            )
            col_names = [d[0] for d in cur.description]
            rows = [dict(zip(col_names, r)) for r in cur.fetchall()]
            return mysql_columns_to_canonical(rows)
        finally:
            conn.close()

    if ct == "oracle":
        # cx_Oracle / oracledb either works; prefer python-oracledb (thin mode).
        try:
            import oracledb  # type: ignore
            connect_kwargs = dict(
                user=config.get("user") or config.get("username"),
                password=config.get("password"),
                dsn=config.get("dsn") or f"{config.get('host')}:{config.get('port') or 1521}/{config.get('service_name') or config.get('database')}",
            )
        except ImportError:
            import cx_Oracle as oracledb  # type: ignore
            connect_kwargs = dict(
                user=config.get("user") or config.get("username"),
                password=config.get("password"),
                dsn=oracledb.makedsn(
                    config.get("host"),
                    config.get("port") or 1521,
                    service_name=config.get("service_name") or config.get("database"),
                ),
            )
        from fpulse.types.from_oracle import (
            CANONICAL_COLUMN_QUERY_ORACLE,
            oracle_columns_to_canonical,
        )
        conn = oracledb.connect(**connect_kwargs)
        try:
            cur = conn.cursor()
            # Oracle stores unquoted identifiers in upper case; normalize.
            owner = (schema or config.get("user") or "").upper()
            cur.execute(
                CANONICAL_COLUMN_QUERY_ORACLE,
                owner=owner, table_name=table.upper(),
            )
            col_names = [d[0].lower() for d in cur.description]
            rows = [dict(zip(col_names, r)) for r in cur.fetchall()]
            return oracle_columns_to_canonical(rows)
        finally:
            conn.close()

    if ct == "sqlite":
        # SQLite has a different shape — PRAGMA table_info returns
        # (cid, name, type, notnull, dflt_value, pk). We adapt to the
        # canonical query shape inline rather than building a dedicated
        # from_sqlite mapper (SQLite's type system is too loose to merit
        # one — STORAGE is typeless; declared types are advisory).
        import sqlite3
        from fpulse.types.canonical import (
            CanonicalSchema, Evidence, FPField, FPType, Provenance,
        )
        db_path = config.get("path") or config.get("database") or ":memory:"
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            cur = conn.cursor()
            # Use the parameterized identifier carefully; PRAGMA can't be
            # parameterized so we validate the identifier first.
            if not table.replace("_", "").isalnum():
                raise ValueError(f"Unsafe table identifier for SQLite PRAGMA: {table!r}")
            cur.execute(f'PRAGMA table_info("{table}")')
            fields = []
            for row in cur.fetchall():
                _cid, name, declared_type, notnull, _default, _pk = row
                fp_type, params, native = _sqlite_declared_to_fptype(
                    (declared_type or "").upper()
                )
                fields.append(FPField(
                    name=name,
                    type=fp_type,
                    nullable=(not bool(notnull)),
                    params=params,
                    evidence=Evidence.ADVERTISED,
                    confidence=0.9,  # SQLite types are advisory
                    provenance=[Provenance(
                        source=f"SQLite {native}",
                        confidence=0.9,
                        sample_size=0,
                    )],
                    native_raw=native,
                ))
            return CanonicalSchema(fields=fields)
        finally:
            conn.close()

    raise NotImplementedError(
        f"Canonical schema introspection not yet wired for '{conn_type}'. "
        f"Supported: postgresql, mssql, mysql, oracle, sqlite."
    )


def _sqlite_declared_to_fptype(declared: str):
    """Map a SQLite declared type to (FPType, params, native_raw).

    SQLite types are advisory — declared "VARCHAR(10)" can hold any
    string. We follow the same SQLite type-affinity rules the storage
    engine uses internally.
    """
    from fpulse.types.canonical import FPType
    import re as _re
    if not declared:
        return FPType.UNKNOWN, {}, ""
    upper = declared.upper().strip()
    # Numeric affinity for INT family.
    if "INT" in upper:
        return FPType.INTEGER, {"bits": 64}, declared
    if any(t in upper for t in ("CHAR", "CLOB", "TEXT")):
        # Pull length out of e.g. VARCHAR(255)
        m = _re.search(r"\((\d+)\)", upper)
        params = {"length": int(m.group(1))} if m else {}
        return FPType.STRING, params, declared
    if "BLOB" in upper or not upper:
        return FPType.BINARY, {}, declared
    if any(t in upper for t in ("REAL", "FLOA", "DOUB")):
        return FPType.FLOAT, {"bits": 64}, declared
    if "BOOL" in upper:
        return FPType.BOOLEAN, {}, declared
    if "DATE" in upper or "TIME" in upper:
        with_tz = "TZ" in upper or "TIME ZONE" in upper
        return FPType.TIMESTAMP, {"with_timezone": with_tz}, declared
    if "DECIMAL" in upper or "NUMERIC" in upper:
        m = _re.search(r"\((\d+),\s*(\d+)\)", upper)
        params = {"precision": int(m.group(1)), "scale": int(m.group(2))} if m else {}
        return FPType.DECIMAL, params, declared
    return FPType.UNKNOWN, {}, declared


DEV_SAMPLE_ROWS = 1000


@register(StepType.DB_SOURCE)
class DbSourceNode(BaseNode):
    """Database Source — read from any database via saved connection.

    Enterprise features:
      - Table mode: pick schema + table (auto-generates SELECT *)
      - Query mode: write arbitrary SQL
      - Incremental load: WHERE watermark_column > last_value
      - Dev sample limit (overridden by full_run)
      - Query timeout (prevents runaway queries)
    """
    display_name = "Database Source"
    category = "source"
    description = "Read data from a database — pick a table or write your own SQL query"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        connection_id = self.params.get("connection_id", "")
        source_mode = self.params.get("source_mode", "query")

        # Build the SQL query based on mode
        if source_mode == "table":
            query = self._build_table_query()
        else:
            query = self.params.get("query", "")

        if not query:
            raise ValueError("DB Source: no SQL query provided")

        # Incremental load — append watermark WHERE clause.
        #
        # 2026-05-30 (P2 incremental sync): when sync_mode == "incremental"
        # we auto-load `last_cursor` from the SyncStateStore if the
        # operator left `watermark_value` blank, and stamp the new cursor
        # at the end. The legacy manual-cursor path (operator typed a
        # value) still wins so backfills can override.
        sync_mode = (self.params.get("sync_mode") or "full_refresh").lower()
        watermark_col = self.params.get("watermark_column", "").strip()
        watermark_val = self.params.get("watermark_value", "").strip()

        if sync_mode == "incremental" and watermark_col and not watermark_val:
            stored = self._load_sync_cursor(ctx, watermark_col)
            if stored is not None:
                # B1.1 (2026-06-08, docs/design/backfill-ux-1.2.md) -
                # apply the lookback window. Default 0 = strict cursor
                # (current behaviour preserved). Non-zero shifts the
                # cursor BACKWARD so the next incremental SELECT
                # re-covers the last N seconds, catching rows that
                # arrived at the source after our cursor moved past.
                # Dedupe-store handles the resulting overlap so
                # downstream sees each row once. See
                # backend/fpulse/engine/lookback.py for the math +
                # cursor-type handling.
                watermark_val = self._apply_cursor_lookback(stored)

        if watermark_col and watermark_val:
            # Wrap original query and add incremental filter.
            # NOTE: watermark_val is single-quote-escaped to dodge the
            # trivial injection vector. Identifier-level injection
            # (watermark_col) is mitigated by quoting it as a SQL ident.
            safe_val = str(watermark_val).replace("'", "''")
            query = (
                f"SELECT * FROM ({query}) AS __incr "
                f'WHERE "{watermark_col}" > \'{safe_val}\''
            )

        # Resolve the connection's dialect BEFORE applying the sample
        # limit — different dialects need different keywords (MSSQL
        # uses TOP, every other dialect uses LIMIT). Without this, the
        # sample wrapper produced `... LIMIT N` and SQL Server rejected
        # it with `Incorrect syntax near 'LIMIT'`.
        if connection_id:
            result = _get_connection_config(connection_id)
            if not result:
                raise ValueError(f"DB Source: connection '{connection_id}' not found")
            config, conn_type = result
        else:
            config, conn_type = {}, "duckdb"

        # Dev sample limit — dialect-aware.
        if not ctx.full_run:
            limit = int(self.params.get("sample_rows", DEV_SAMPLE_ROWS))
            up = query.upper()
            already_capped = "LIMIT" in up or " TOP " in up or " TOP(" in up
            if limit > 0 and not already_capped:
                if conn_type in ("mssql", "sqlserver"):
                    query = f"SELECT TOP {limit} * FROM ({query}) AS __sample"
                else:
                    query = f"SELECT * FROM ({query}) AS __sample LIMIT {limit}"

        # If no connection specified, run against DuckDB in-memory.
        if not connection_id:
            return ctx.conn.sql(query)

        # Execute against real database and load into DuckDB. Pass ctx +
        # connection_id so the Postgres path can use the connection pool
        # (Critical #5 Phase 2). All other dialects still use the legacy
        # direct-connect path until later phases of the rollout.
        rows, columns = self._execute_real(
            conn_type, config, query,
            ctx=ctx, connection_id=connection_id,
        )

        if not rows:
            col_defs = ", ".join(f"NULL AS \"{c}\"" for c in columns) if columns else "NULL AS empty"
            return ctx.conn.sql(f"SELECT {col_defs} WHERE false")

        # Load rows into DuckDB as a relation. We name the columns
        # explicitly via the `AS __vals (col_a, col_b, …)` clause so we
        # don't rely on DuckDB's auto-naming, which has shifted between
        # versions (`column0` in old releases, `col0` in newer ones).
        # The previous two-step approach (auto-named CREATE TABLE +
        # `column{i} AS "real_name"` rename) broke on every install
        # whose DuckDB had moved to the `col0` convention — the rename
        # referenced columns that didn't exist, raising
        # `Binder Error: Referenced column "column0" not found`.
        quoted_cols = ", ".join(f'"{c}"' for c in columns)
        values_sql = self._rows_to_values(rows, columns)
        # Per-step temp-table name: the returned relation reads this table
        # lazily, so two DB-source nodes in one pipeline must not share it.
        db_src = ctx.scoped_name("__db_source")
        ctx.conn.execute(
            f"CREATE OR REPLACE TEMP TABLE {db_src} AS "
            f"SELECT * FROM (VALUES {values_sql}) AS __vals ({quoted_cols})"
        )
        relation = ctx.conn.sql(f"SELECT * FROM {db_src}")

        # 2026-05-30 (P2): persist the new watermark for incremental mode.
        # The next run reads it back at the top of execute() above.
        if sync_mode == "incremental" and watermark_col:
            self._save_sync_cursor(ctx, watermark_col, len(rows))

        return relation

    def _apply_cursor_lookback(self, stored_cursor: str) -> str:
        """B1.1 (2026-06-08) - apply the configured lookback window to
        the persisted cursor before it's used in the WHERE clause.

        Returns the original cursor when no lookback is configured (the
        default). When lookback_seconds > 0, returns a cursor shifted
        back by that many seconds so the next incremental SELECT
        re-covers late-arriving data.

        Lookback only applies to numeric / time-based cursors. Opaque
        string cursors pass through unchanged (see lookback.py)."""
        lookback = int(self.params.get("lookback_seconds") or 0)
        if lookback <= 0:
            return stored_cursor
        from fpulse.engine.lookback import apply_lookback
        shifted = apply_lookback(stored_cursor, lookback_seconds=lookback)
        if shifted is None:
            return stored_cursor  # defensive: preserve current behaviour
        return str(shifted)

    def _load_sync_cursor(self, ctx: "ExecutionContext", cursor_col: str) -> str | None:
        """Return the persisted watermark for this (workflow, step) or
        None if no prior incremental run has completed."""
        try:
            from fpulse.engine.sync_state_store import sync_state_store
            workflow_id = getattr(ctx, "workflow_id", None) or ""
            step_id = self.params.get("_step_id", "") or ""
            if not (workflow_id and step_id):
                return None
            state = sync_state_store.get(workflow_id, step_id)
            if state and state.cursor_column == cursor_col and state.last_cursor:
                return state.last_cursor
        except Exception:  # noqa: BLE001 — read errors fall back to manual cursor
            pass
        return None

    def _save_sync_cursor(self, ctx: "ExecutionContext", cursor_col: str,
                          rows_loaded: int) -> None:
        """After a successful incremental load, compute MAX(cursor_col)
        across what was just materialised and upsert into the state
        store. The fresh upper bound becomes the lower bound of the
        next run's WHERE clause."""
        try:
            from fpulse.engine.sync_state_store import sync_state_store, SyncState
            workflow_id = getattr(ctx, "workflow_id", None) or ""
            step_id = self.params.get("_step_id", "") or ""
            if not (workflow_id and step_id):
                return
            try:
                row = ctx.conn.sql(
                    f'SELECT MAX("{cursor_col}") FROM __db_source'
                ).fetchone()
                new_cursor = str(row[0]) if row and row[0] is not None else None
            except Exception:  # noqa: BLE001 — keep stale cursor on probe failure
                new_cursor = None
            if new_cursor is None:
                return
            sync_state_store.upsert(SyncState(
                workflow_id=workflow_id,
                step_id=step_id,
                cursor_column=cursor_col,
                last_cursor=new_cursor,
                rows_last_run=int(rows_loaded or 0),
            ))
        except Exception:  # noqa: BLE001 — never break the run on a cursor save
            pass

    def _build_table_query(self) -> str:
        """Build a SELECT query from table mode parameters."""
        table = self.params.get("table", "").strip()
        schema_name = self.params.get("schema", "").strip()
        columns = self.params.get("columns", "").strip()
        where = self.params.get("where", "").strip()
        order_by = self.params.get("order_by", "").strip()

        if not table:
            raise ValueError("DB Source (table mode): table name is required")

        full_table = f'"{schema_name}"."{table}"' if schema_name else f'"{table}"'
        col_list = columns if columns else "*"
        sql = f"SELECT {col_list} FROM {full_table}"
        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        return sql

    def _execute_real(self, conn_type: str, config: dict, query: str,
                      ctx: 'ExecutionContext | None' = None,
                      connection_id: str | None = None) -> tuple[list[tuple], list[str]]:
        """Execute query against a real database. Returns (rows, columns).

        ctx + connection_id enable connection pooling (Critical #5).
        Both are optional for backwards compat; legacy callers omit them
        and get the direct-connect path."""
        host = config.get("host")
        port = config.get("port")
        database = config.get("database")
        user = config.get("user") or config.get("username")
        password = config.get("password")
        schema = config.get("schema")

        if conn_type == "sqlite":
            return self._query_sqlite(config, query, ctx=ctx, connection_id=connection_id)
        elif conn_type == "postgresql":
            return self._query_postgresql(host, port, database, user, password, schema, query,
                                           ctx=ctx, connection_id=connection_id)
        elif conn_type == "mysql":
            return self._query_mysql(host, port, database, user, password, query,
                                     ctx=ctx, connection_id=connection_id)
        elif conn_type == "mssql":
            return self._query_mssql(host, port, database, user, password, query,
                                     ctx=ctx, connection_id=connection_id)
        else:
            raise ValueError(f"DB Source: unsupported connection type '{conn_type}'. "
                             f"Use postgresql, mysql, mssql, or sqlite.")

    def _query_sqlite(self, config: dict, query: str, ctx=None, connection_id=None) -> tuple[list[tuple], list[str]]:
        """SQLite query path. Pool semantics: SQLite connections are NOT
        thread-safe by default, but our executor is single-threaded per
        run (DuckDB-driven), so pooling within a single run is safe and
        amortises the file-open cost across steps. The file-open cost
        is small but adds up for workflows with many steps reading the
        same .db file."""
        db_path = config.get("database") or config.get("file")
        if not db_path or not os.path.isfile(db_path):
            raise ValueError(f"SQLite: database file not found: {db_path}")

        def factory(_ct: str, _c: dict):
            return sqlite3.connect(db_path)

        pool = (ctx.app_state.get("connection_pool")
                if ctx is not None and getattr(ctx, "app_state", None) else None)
        run_id = getattr(ctx, "run_id", None) if ctx is not None else None

        if pool is not None and run_id and connection_id:
            conn = pool.acquire(
                connection_id=connection_id, run_id=run_id, conn_type="sqlite",
                config={}, factory=factory,
            )
            try:
                cursor = conn.execute(query)
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                return rows, columns
            except Exception:
                try: pool.invalidate_connection(connection_id)
                except Exception: pass
                raise
        else:
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.execute(query)
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                return rows, columns
            finally:
                conn.close()

    def _query_postgresql(self, host, port, database, user, password, schema, query,
                          ctx=None, connection_id=None):
        """Postgres query path. Uses the connection pool when:
          - ctx and connection_id are provided
          - app_state has a 'connection_pool' entry (installed at startup)
          - ctx.run_id is set (always true under WorkflowExecutor)
        Otherwise falls back to a direct psycopg2.connect/close.

        The pool keeps the connection alive across steps in the same run,
        amortising the 50-150ms TCP+TLS+auth handshake. See
        DESIGN_CONNECTION_POOLING.md.
        """
        import psycopg2  # type: ignore
        opts = f"-c search_path={schema}" if schema else None

        def factory(_ct: str, _c: dict):
            return psycopg2.connect(
                host=host, port=port or 5432,
                dbname=database, user=user, password=password,
                connect_timeout=10,
                options=opts,
            )

        pool = (ctx.app_state.get("connection_pool")
                if ctx is not None and getattr(ctx, "app_state", None) else None)
        run_id = getattr(ctx, "run_id", None) if ctx is not None else None

        if pool is not None and run_id and connection_id:
            # Pool path — pool owns the lifetime; do NOT close.
            conn = pool.acquire(
                connection_id=connection_id, run_id=run_id, conn_type="postgresql",
                config={}, factory=factory,
            )
            try:
                cur = conn.cursor()
                # E3.1 (2026-06-08) — register psycopg2's thread-safe
                # conn.cancel() so cancel_run() interrupts this query
                # mid-flight. No-op when there's no run token. [LIVE-SMOKE]
                from fpulse.engine.cancellation import (
                    register_connection_cancel, unregister_connection_cancel,
                )
                _cancel_cb = register_connection_cancel(run_id, conn)
                try:
                    cur.execute(query)
                    columns = [desc[0] for desc in cur.description] if cur.description else []
                    rows = cur.fetchall()
                    return rows, columns
                finally:
                    unregister_connection_cancel(run_id, _cancel_cb)
            except Exception:
                # If the cached connection died (network blip, server
                # restart), drop the entry so the next acquire gets a
                # fresh one. Then re-raise — caller decides retry.
                try:
                    pool.invalidate_connection(connection_id)
                except Exception:  # noqa: BLE001
                    pass
                raise
        else:
            # Legacy direct-connect path.
            conn = factory("postgresql", {})
            # E3.1 (2026-06-08) — register native cancel so a cancelled
            # run interrupts this query mid-flight. No-op without a run
            # token. [LIVE-SMOKE]
            from fpulse.engine.cancellation import (
                register_connection_cancel, unregister_connection_cancel,
            )
            _cancel_cb = register_connection_cancel(run_id, conn)
            try:
                cur = conn.cursor()
                cur.execute(query)
                columns = [desc[0] for desc in cur.description] if cur.description else []
                rows = cur.fetchall()
                return rows, columns
            finally:
                unregister_connection_cancel(run_id, _cancel_cb)
                conn.close()

    def _query_mysql(self, host, port, database, user, password, query,
                     ctx=None, connection_id=None):
        """MySQL query path. Uses connection pool when available — same
        pattern as Postgres. See DESIGN_CONNECTION_POOLING.md."""
        import pymysql  # type: ignore

        def factory(_ct: str, _c: dict):
            return pymysql.connect(
                host=host, port=int(port or 3306),
                database=database, user=user, password=password,
                connect_timeout=10,
            )

        pool = (ctx.app_state.get("connection_pool")
                if ctx is not None and getattr(ctx, "app_state", None) else None)
        run_id = getattr(ctx, "run_id", None) if ctx is not None else None

        if pool is not None and run_id and connection_id:
            conn = pool.acquire(
                connection_id=connection_id, run_id=run_id, conn_type="mysql",
                config={}, factory=factory,
            )
            try:
                cur = conn.cursor()
                cur.execute(query)
                columns = [desc[0] for desc in cur.description] if cur.description else []
                rows = cur.fetchall()
                return rows, columns
            except Exception:
                try: pool.invalidate_connection(connection_id)
                except Exception: pass
                raise
        else:
            conn = factory("mysql", {})
            try:
                cur = conn.cursor()
                cur.execute(query)
                columns = [desc[0] for desc in cur.description] if cur.description else []
                rows = cur.fetchall()
                return rows, columns
            finally:
                conn.close()

    def _query_mssql(self, host, port, database, user, password, query,
                     ctx=None, connection_id=None):
        """MSSQL query path. Uses connection pool when available — same
        pattern as Postgres / MySQL. ODBC connection setup is the most
        expensive of the four dialects (driver init + auth handshake),
        so the pool benefit is largest here for multi-step workflows."""
        import pyodbc  # type: ignore
        conn_str = (
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={host},{port or 1433};"
            f"DATABASE={database};"
            f"UID={user};PWD={password};"
            f"Connection Timeout=10;"
        )

        def factory(_ct: str, _c: dict):
            c = pyodbc.connect(conn_str)
            # Decode SQL Server extended types (DATETIMEOFFSET / TIME) that
            # pyodbc can't handle natively — else fetch aborts with "ODBC SQL
            # type -155 is not yet supported". Applied here so both the pooled
            # and direct-connect paths below inherit it.
            try:
                from fpulse.connectors.odbc_runtime import register_mssql_odbc_converters
                register_mssql_odbc_converters(c)
            except Exception:  # noqa: BLE001 — never let setup break a read
                pass
            return c

        pool = (ctx.app_state.get("connection_pool")
                if ctx is not None and getattr(ctx, "app_state", None) else None)
        run_id = getattr(ctx, "run_id", None) if ctx is not None else None

        from fpulse.connectors.odbc_runtime import humanize_odbc_read_error

        if pool is not None and run_id and connection_id:
            conn = pool.acquire(
                connection_id=connection_id, run_id=run_id, conn_type="mssql",
                config={}, factory=factory,
            )
            try:
                cur = conn.cursor()
                cur.execute(query)
                columns = [desc[0] for desc in cur.description] if cur.description else []
                rows = cur.fetchall()
                return rows, columns
            except Exception as exc:
                # An unsupported-column-type read is a data shape problem, not
                # a broken connection — re-raise it as actionable guidance and
                # don't invalidate the pooled connection.
                hint = humanize_odbc_read_error(exc)
                if hint:
                    raise ValueError(hint) from exc
                try: pool.invalidate_connection(connection_id)
                except Exception: pass
                raise
        else:
            conn = factory("mssql", {})
            try:
                cur = conn.cursor()
                cur.execute(query)
                columns = [desc[0] for desc in cur.description] if cur.description else []
                try:
                    rows = cur.fetchall()
                except Exception as exc:
                    hint = humanize_odbc_read_error(exc)
                    if hint:
                        raise ValueError(hint) from exc
                    raise
                return rows, columns
            finally:
                conn.close()

    def _rows_to_values(self, rows: list[tuple], columns: list[str]) -> str:
        """Convert rows to a DuckDB VALUES clause."""
        if not rows:
            return "(NULL)" + ", (NULL)" * (len(columns) - 1)

        def format_val(v):
            if v is None:
                return "NULL"
            if isinstance(v, (int, float)):
                return str(v)
            if isinstance(v, bool):
                return "true" if v else "false"
            # String — escape single quotes
            return "'" + str(v).replace("'", "''") + "'"

        parts = []
        for row in rows:
            vals = ", ".join(format_val(v) for v in row)
            parts.append(f"({vals})")
        return ", ".join(parts)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "connection_id": "",
            "source_mode": "query",
            "query": "SELECT 1 AS id, 'hello' AS value",
            "table": "",
            "schema": "",
            "columns": "",
            "where": "",
            "order_by": "",
            # 2026-05-30 (P2): sync_mode is the first-class incremental
            # contract. "full_refresh" = re-read every row every run;
            # "incremental" = WHERE watermark_column > last_cursor and
            # auto-persist the new max via SyncStateStore; "cdc" = stub
            # for log-based replication (use the dedicated cdc_source
            # node — DB Source's cdc mode is a UI affordance only).
            "sync_mode": "full_refresh",
            "watermark_column": "",
            "watermark_value": "",
            "sample_rows": DEV_SAMPLE_ROWS,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "Connection",
             "tab": "Source",
             "description": "Select a saved connection. Leave empty for DuckDB in-memory."},
            {"name": "source_mode", "type": "select", "label": "Mode",
             "options": ["query", "table"], "default": "query", "tab": "Source",
             "description": "query = write SQL, table = pick a table."},
            # Query mode
            {"name": "query", "type": "sql", "label": "SQL Query", "tab": "Source",
             "show_when": {"source_mode": ["query"]},
             "description": "Full SQL query to execute."},
            # Table mode
            {"name": "schema", "type": "text", "label": "Schema", "tab": "Source",
             "placeholder": "public",
             "show_when": {"source_mode": ["table"]}},
            {"name": "table", "type": "text", "label": "Table", "tab": "Source",
             "placeholder": "orders",
             "show_when": {"source_mode": ["table"]}},
            {"name": "columns", "type": "text", "label": "Columns", "tab": "Source",
             "placeholder": "* (all) or col1, col2, col3",
             "show_when": {"source_mode": ["table"]}},
            {"name": "where", "type": "text", "label": "WHERE Filter", "tab": "Source",
             "placeholder": "status = 'active' AND created_at > '2025-01-01'",
             "show_when": {"source_mode": ["table"]}},
            {"name": "order_by", "type": "text", "label": "ORDER BY", "tab": "Source",
             "placeholder": "created_at DESC",
             "show_when": {"source_mode": ["table"]}},
            # Incremental
            {"name": "sync_mode", "type": "select", "label": "Sync Mode",
             "tab": "Incremental",
             "options": ["full_refresh", "incremental", "cdc"],
             "default": "full_refresh",
             "description": (
                 "full_refresh = re-read every row each run. "
                 "incremental = read only rows newer than the last cursor "
                 "(auto-tracked between runs in the sync_state table). "
                 "cdc = use the dedicated CDC Source node for log-based "
                 "replication; this option is informational here."
             )},
            {"name": "watermark_column", "type": "text", "label": "Cursor Column",
             "tab": "Incremental",
             "placeholder": "updated_at",
             "show_when": {"sync_mode": ["incremental"]},
             "description": (
                 "Column to track for incremental loads. Must be monotonic "
                 "(timestamp or auto-increment id). Only rows where this "
                 "column > the stored cursor are loaded."
             )},
            {"name": "watermark_value", "type": "text", "label": "Manual Cursor Override (optional)",
             "tab": "Incremental",
             "placeholder": "2025-01-01T00:00:00",
             "show_when": {"sync_mode": ["incremental"]},
             "description": (
                 "Leave blank for normal incremental runs — the engine "
                 "auto-loads the last cursor from sync_state. Set a value "
                 "here only to override the auto-cursor (e.g. backfill "
                 "from a specific date, or recover after manual cleanup)."
             )},
            # Settings
            {"name": "sample_rows", "type": "number", "label": "Dev Sample Limit",
             "default": DEV_SAMPLE_ROWS, "tab": "Settings",
             "description": "Max rows in dev mode (0 = no limit). Ignored in Full Run."},
        ]
