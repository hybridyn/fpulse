"""
JDBC dialect registry for warehouse connectors.

Each dialect is a tiny adapter: build a connection (or DuckDB attach string),
read a table or run a query, and return rows. Drivers are imported lazily so
F-Pulse never crashes if a vendor driver isn't installed — instead, the user
gets a clear "pip install <package>" message.

Dialects supported:
  snowflake, bigquery, redshift, databricks, mssql, oracle, mongodb, clickhouse
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class DialectInfo:
    name: str
    label: str
    driver_pkg: str
    install_hint: str
    reader: Callable[[dict[str, Any], str | None, str | None, int | None], tuple[list[str], list[tuple]]]
    writer: Callable[[dict[str, Any], str, list[str], list[tuple]], int] | None = None


_REGISTRY: dict[str, DialectInfo] = {}


def register_dialect(info: DialectInfo) -> None:
    _REGISTRY[info.name] = info


def get_dialect(name: str) -> DialectInfo:
    if name not in _REGISTRY:
        raise ValueError(f"Unknown JDBC dialect '{name}'. Available: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def list_dialects() -> list[dict]:
    return [{"name": d.name, "label": d.label, "install": d.install_hint} for d in _REGISTRY.values()]


def _import_or_raise(pkg: str, install_hint: str):
    try:
        return importlib.import_module(pkg)
    except ImportError as e:
        raise RuntimeError(
            f"Driver '{pkg}' is not installed. Install with: pip install {install_hint}"
        ) from e


# ─────────────────────────── Snowflake ───────────────────────────

def _snowflake_reader(cfg, query, table, limit):
    sf = _import_or_raise("snowflake.connector", "snowflake-connector-python")
    conn = sf.connect(
        user=cfg["user"], password=cfg["password"], account=cfg["account"],
        warehouse=cfg.get("warehouse"), database=cfg.get("database"), schema=cfg.get("schema"),
        role=cfg.get("role"),
    )
    try:
        cur = conn.cursor()
        sql = query or f"SELECT * FROM {table}"
        if limit:
            sql = f"SELECT * FROM ({sql}) LIMIT {int(limit)}"
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows
    finally:
        conn.close()

register_dialect(DialectInfo(
    "snowflake", "Snowflake", "snowflake.connector", "snowflake-connector-python", _snowflake_reader,
))


# ─────────────────────────── BigQuery ───────────────────────────

def _bigquery_reader(cfg, query, table, limit):
    bq = _import_or_raise("google.cloud.bigquery", "google-cloud-bigquery")
    client = bq.Client(project=cfg.get("project"))
    sql = query or f"SELECT * FROM `{cfg.get('project')}.{cfg.get('dataset')}.{table}`"
    if limit:
        sql = f"SELECT * FROM ({sql}) LIMIT {int(limit)}"
    job = client.query(sql)
    rows = list(job.result())
    cols = [f.name for f in job.schema] if job.schema else (list(rows[0].keys()) if rows else [])
    return cols, [tuple(r[c] for c in cols) for r in rows]

register_dialect(DialectInfo(
    "bigquery", "Google BigQuery", "google.cloud.bigquery", "google-cloud-bigquery", _bigquery_reader,
))


# ─────────────────────────── Redshift ───────────────────────────

def _redshift_reader(cfg, query, table, limit):
    rs = _import_or_raise("redshift_connector", "redshift_connector")
    conn = rs.connect(
        host=cfg["host"], database=cfg["database"], port=int(cfg.get("port", 5439)),
        user=cfg["user"], password=cfg["password"],
    )
    try:
        cur = conn.cursor()
        sql = query or f"SELECT * FROM {table}"
        if limit:
            sql = f"{sql} LIMIT {int(limit)}"
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows
    finally:
        conn.close()

register_dialect(DialectInfo(
    "redshift", "Amazon Redshift", "redshift_connector", "redshift_connector", _redshift_reader,
))


# ─────────────────────────── Databricks ───────────────────────────

def _databricks_reader(cfg, query, table, limit):
    db = _import_or_raise("databricks.sql", "databricks-sql-connector")
    conn = db.connect(
        server_hostname=cfg["host"], http_path=cfg["http_path"], access_token=cfg["access_token"],
    )
    try:
        cur = conn.cursor()
        sql = query or f"SELECT * FROM {table}"
        if limit:
            sql = f"{sql} LIMIT {int(limit)}"
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows
    finally:
        conn.close()

register_dialect(DialectInfo(
    "databricks", "Databricks SQL", "databricks.sql", "databricks-sql-connector", _databricks_reader,
))


# ─────────────────────────── MSSQL ───────────────────────────

def _mssql_reader(cfg, query, table, limit):
    pyodbc = _import_or_raise("pyodbc", "pyodbc")
    driver = cfg.get("driver", "ODBC Driver 18 for SQL Server")
    conn_str = (
        f"DRIVER={{{driver}}};SERVER={cfg['host']},{cfg.get('port', 1433)};"
        f"DATABASE={cfg['database']};UID={cfg['user']};PWD={cfg['password']};"
        f"TrustServerCertificate=yes;"
    )
    conn = pyodbc.connect(conn_str)
    # Decode SQL Server extended types (DATETIMEOFFSET / TIME) that pyodbc
    # can't handle natively — otherwise fetch aborts with "ODBC SQL type
    # -155 is not yet supported".
    from fpulse.connectors.odbc_runtime import (
        humanize_odbc_read_error, register_mssql_odbc_converters,
    )
    register_mssql_odbc_converters(conn)
    try:
        cur = conn.cursor()
        sql = query or f"SELECT * FROM {table}"
        if limit:
            sql = f"SELECT TOP {int(limit)} * FROM ({sql}) AS sub"
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        try:
            rows = cur.fetchall()
        except Exception as exc:  # turn unsupported-type errors into a fix
            hint = humanize_odbc_read_error(exc)
            if hint:
                raise ValueError(hint) from exc
            raise
        return cols, [tuple(r) for r in rows]
    finally:
        conn.close()

register_dialect(DialectInfo(
    "mssql", "Microsoft SQL Server", "pyodbc", "pyodbc", _mssql_reader,
))


# ─────────────────────────── Oracle ───────────────────────────

def _oracle_reader(cfg, query, table, limit):
    ox = _import_or_raise("oracledb", "oracledb")
    dsn = ox.makedsn(cfg["host"], int(cfg.get("port", 1521)), service_name=cfg.get("service_name"))
    conn = ox.connect(user=cfg["user"], password=cfg["password"], dsn=dsn)
    try:
        cur = conn.cursor()
        sql = query or f"SELECT * FROM {table}"
        if limit:
            sql = f"SELECT * FROM ({sql}) WHERE ROWNUM <= {int(limit)}"
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows
    finally:
        conn.close()

register_dialect(DialectInfo(
    "oracle", "Oracle", "oracledb", "oracledb", _oracle_reader,
))


# ─────────────────────────── MongoDB ───────────────────────────

def _mongodb_reader(cfg, query, table, limit):
    pm = _import_or_raise("pymongo", "pymongo")
    client = pm.MongoClient(cfg["uri"])
    try:
        db = client[cfg["database"]]
        coll = db[table or cfg.get("collection")]
        cursor = coll.find({}).limit(int(limit) if limit else 1000)
        docs = list(cursor)
        if not docs:
            return [], []
        cols: list[str] = []
        seen = set()
        for d in docs:
            for k in d.keys():
                if k not in seen:
                    cols.append(k)
                    seen.add(k)
        rows = [tuple(str(d.get(c)) if isinstance(d.get(c), (dict, list)) else d.get(c) for c in cols) for d in docs]
        return cols, rows
    finally:
        client.close()

register_dialect(DialectInfo(
    "mongodb", "MongoDB", "pymongo", "pymongo", _mongodb_reader,
))


# ─────────────────────────── ClickHouse ───────────────────────────

def _clickhouse_reader(cfg, query, table, limit):
    ch = _import_or_raise("clickhouse_connect", "clickhouse-connect")
    client = ch.get_client(
        host=cfg["host"], port=int(cfg.get("port", 8123)),
        username=cfg.get("user", "default"), password=cfg.get("password", ""),
        database=cfg.get("database", "default"),
    )
    sql = query or f"SELECT * FROM {table}"
    if limit:
        sql = f"{sql} LIMIT {int(limit)}"
    result = client.query(sql)
    return list(result.column_names), [tuple(r) for r in result.result_rows]

register_dialect(DialectInfo(
    "clickhouse", "ClickHouse", "clickhouse_connect", "clickhouse-connect", _clickhouse_reader,
))
