"""
Generic JDBC source/sink — one node, 8 warehouses.

Resolves dialect at execute() time and delegates to the dialect adapter.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for type annotations on helper + execute()
# signatures. Runtime data flow goes through ctx.conn.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register
from fpulse.connectors.jdbc_dialects import get_dialect, list_dialects


def _get_connection_config(connection_id: str) -> tuple[dict, str] | None:
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
        if connection.credential_id and cred_store:
            cred = cred_store.get_raw(connection.credential_id)
            if cred and cred.config:
                config.update(cred.config)
        return config, connection.type
    except Exception:
        return None


def _rows_to_relation(conn: duckdb.DuckDBPyConnection, columns: list[str], rows: list[tuple]) -> duckdb.DuckDBPyRelation:
    if not columns:
        return conn.sql("SELECT NULL AS empty WHERE false")
    if not rows:
        col_sql = ", ".join(f"NULL AS \"{c}\"" for c in columns)
        return conn.sql(f"SELECT {col_sql} WHERE false")
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                obj = {c: (str(v) if hasattr(v, "isoformat") else v) for c, v in zip(columns, r)}
                f.write(json.dumps(obj, default=str) + "\n")
        return conn.sql(f"SELECT * FROM read_json_auto('{path}', format='newline_delimited')")
    except Exception:
        # Fallback to in-memory VALUES
        def fmt(v):
            if v is None:
                return "NULL"
            if isinstance(v, (int, float)):
                return str(v)
            return "'" + str(v).replace("'", "''") + "'"
        values_sql = ", ".join(
            "(" + ", ".join(fmt(v) for v in r) + ")" for r in rows
        )
        _qcols = ", ".join(f'"{c}"' for c in columns)
        conn.execute(f"CREATE OR REPLACE TEMP TABLE __jdbc_tmp AS SELECT * FROM (VALUES {values_sql}) AS __vals ({_qcols})")
        return conn.sql("SELECT * FROM __jdbc_tmp")


@register(StepType.JDBC_SOURCE)
class JdbcSourceNode(BaseNode):
    """Generic warehouse source — pick a dialect, run a query or read a table."""

    display_name = "Warehouse Source (JDBC)"
    category = "source"
    description = "Read from Snowflake, BigQuery, Redshift, Databricks, MSSQL, Oracle, MongoDB, ClickHouse"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        dialect_name = self.params.get("dialect")
        connection_id = self.params.get("connection_id")
        cfg: dict[str, Any] = {}

        if connection_id:
            resolved = _get_connection_config(connection_id)
            if resolved:
                cfg, conn_type = resolved
                if not dialect_name:
                    dialect_name = conn_type

        # Inline overrides
        for k in ("host", "port", "user", "password", "database", "schema", "warehouse",
                  "account", "role", "project", "dataset", "http_path", "access_token",
                  "service_name", "uri", "collection", "driver"):
            if self.params.get(k) not in (None, ""):
                cfg[k] = self.params[k]

        if not dialect_name:
            raise ValueError("JDBC Source: dialect is required (or pick a connection)")

        dialect = get_dialect(dialect_name)
        query = self.params.get("query") or ""
        table = self.params.get("table") or ""
        limit = self.params.get("limit")
        if not query and not table:
            raise ValueError("JDBC Source: provide either a SQL query or a table name")

        cols, rows = dialect.reader(cfg, query or None, table or None, int(limit) if limit else None)
        return _rows_to_relation(ctx.conn, cols, rows)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"dialect": "", "query": "", "table": "", "limit": 1000}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "dialect", "type": "string", "label": "Warehouse", "required": True,
             "options": [{"value": d["name"], "label": d["label"]} for d in list_dialects()]},
            {"name": "connection_id", "type": "string", "label": "Connection"},
            {"name": "query", "type": "string", "label": "SQL Query"},
            {"name": "table", "type": "string", "label": "Table (if no query)"},
            {"name": "limit", "type": "number", "label": "Row limit", "default": 1000},
        ]


@register(StepType.JDBC_SINK)
class JdbcSinkNode(BaseNode):
    """Generic warehouse sink — writes upstream rows via INSERT statements."""

    display_name = "Warehouse Sink (JDBC)"
    category = "output"
    description = "Write rows to Snowflake/BigQuery/Redshift/Databricks/MSSQL/Oracle/ClickHouse"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        upstream_ids = self.params.get("_input_step_ids", [])
        if not upstream_ids:
            raise ValueError("JDBC Sink: requires an upstream node")
        rel = ctx.get_input(upstream_ids[0])
        if rel is None:
            raise ValueError("JDBC Sink: upstream produced no relation")

        cols = rel.columns
        rows = rel.fetchall()

        dialect_name = self.params.get("dialect")
        target_table = self.params.get("table")
        if not dialect_name or not target_table:
            raise ValueError("JDBC Sink: dialect and table are required")

        cfg: dict[str, Any] = {}
        connection_id = self.params.get("connection_id")
        if connection_id:
            resolved = _get_connection_config(connection_id)
            if resolved:
                cfg, conn_type = resolved
        for k in ("host", "port", "user", "password", "database", "schema", "warehouse",
                  "account", "role", "project", "dataset", "http_path", "access_token",
                  "service_name", "uri", "driver"):
            if self.params.get(k) not in (None, ""):
                cfg[k] = self.params[k]

        # Default writer: per-dialect basic INSERT (kept simple — production users
        # should swap in COPY or bulk-load paths via dialect.writer hooks).
        inserted = _basic_insert(dialect_name, cfg, target_table, cols, rows)

        # Pass through for downstream observability
        return rel

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"dialect": "", "table": ""}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "dialect", "type": "string", "label": "Warehouse", "required": True,
             "options": [{"value": d["name"], "label": d["label"]} for d in list_dialects()]},
            {"name": "connection_id", "type": "string", "label": "Connection"},
            {"name": "table", "type": "string", "label": "Target table", "required": True},
        ]


def _safe_table(name: str) -> str:
    """Defence-in-depth identifier guard for the naive-insert path.

    The table name comes from connection config (operator-controlled), but
    we still reject anything that isn't a plain, optionally schema-qualified
    SQL identifier so a crafted name can't break out of the INSERT or trip a
    reserved-word edge. Validation-only (no quoting) so each dialect's
    existing case-folding behaviour is preserved.
    """
    import re as _re
    if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*", str(name or "")):
        raise ValueError(f"Refusing unsafe / invalid table identifier: {name!r}")
    return name


def _basic_insert(dialect_name: str, cfg: dict, table: str, cols: list[str], rows: list[tuple]) -> int:
    """Naive parameterized INSERT path. Each dialect's native bulk-load is preferred for big writes."""
    if not rows:
        return 0
    placeholders = ", ".join(["?"] * len(cols))
    col_list = ", ".join(f'"{c}"' for c in cols)
    sql = f'INSERT INTO {_safe_table(table)} ({col_list}) VALUES ({placeholders})'

    if dialect_name == "snowflake":
        import snowflake.connector as sf
        conn = sf.connect(
            user=cfg["user"], password=cfg["password"], account=cfg["account"],
            warehouse=cfg.get("warehouse"), database=cfg.get("database"), schema=cfg.get("schema"),
        )
        try:
            cur = conn.cursor()
            cur.executemany(sql.replace("?", "%s"), rows)
            conn.commit()
            return cur.rowcount or len(rows)
        finally:
            conn.close()

    if dialect_name == "redshift":
        import redshift_connector
        conn = redshift_connector.connect(
            host=cfg["host"], database=cfg["database"], user=cfg["user"], password=cfg["password"],
        )
        try:
            cur = conn.cursor()
            cur.executemany(sql.replace("?", "%s"), rows)
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    if dialect_name == "mssql":
        import pyodbc
        driver = cfg.get("driver", "ODBC Driver 18 for SQL Server")
        conn_str = (
            f"DRIVER={{{driver}}};SERVER={cfg['host']},{cfg.get('port', 1433)};"
            f"DATABASE={cfg['database']};UID={cfg['user']};PWD={cfg['password']};"
            f"TrustServerCertificate=yes;"
        )
        conn = pyodbc.connect(conn_str)
        try:
            cur = conn.cursor()
            cur.executemany(sql, rows)
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    if dialect_name == "clickhouse":
        import clickhouse_connect
        client = clickhouse_connect.get_client(
            host=cfg["host"], port=int(cfg.get("port", 8123)),
            username=cfg.get("user", "default"), password=cfg.get("password", ""),
            database=cfg.get("database", "default"),
        )
        client.insert(table, rows, column_names=cols)
        return len(rows)

    if dialect_name == "bigquery":
        from google.cloud import bigquery
        client = bigquery.Client(project=cfg.get("project"))
        table_ref = f"{cfg.get('project')}.{cfg.get('dataset')}.{_safe_table(table)}"
        json_rows = [dict(zip(cols, r)) for r in rows]
        errors = client.insert_rows_json(table_ref, json_rows)
        if errors:
            raise RuntimeError(f"BigQuery insert errors: {errors}")
        return len(rows)

    if dialect_name == "databricks":
        from databricks import sql as dbsql
        conn = dbsql.connect(
            server_hostname=cfg["host"], http_path=cfg["http_path"], access_token=cfg["access_token"],
        )
        try:
            cur = conn.cursor()
            cur.executemany(sql, rows)
            return len(rows)
        finally:
            conn.close()

    if dialect_name == "oracle":
        import oracledb
        dsn = oracledb.makedsn(cfg["host"], int(cfg.get("port", 1521)), service_name=cfg.get("service_name"))
        conn = oracledb.connect(user=cfg["user"], password=cfg["password"], dsn=dsn)
        try:
            cur = conn.cursor()
            placeholders = ", ".join(f":{i+1}" for i in range(len(cols)))
            ora_sql = f'INSERT INTO {_safe_table(table)} ({col_list}) VALUES ({placeholders})'
            cur.executemany(ora_sql, rows)
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    if dialect_name == "mongodb":
        import pymongo
        client = pymongo.MongoClient(cfg["uri"])
        try:
            db = client[cfg["database"]]
            coll = db[table]
            docs = [dict(zip(cols, r)) for r in rows]
            coll.insert_many(docs)
            return len(docs)
        finally:
            client.close()

    raise RuntimeError(f"No writer implemented for dialect '{dialect_name}'")
