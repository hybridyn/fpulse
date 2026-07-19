"""Connector catalog — list browsable objects (tables, collections, topics,
buckets, indexes…) for any supported connection.

Used by the editor's Source/Sink config panel to replace free-text inputs
with browsable dropdowns. Every provider is defensive about driver imports
and slow/failing connections — the response always returns a uniform
shape, never raises.

Provider registration is by connection type. Each provider returns a
`Catalog` that the frontend groups by `parent` (schema/database/bucket)
and `kind` (table/view/procedure/synonym/collection/topic/index/object).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 8


# ── Provider capability metadata ──
#
# Every catalog provider declares its capabilities up front. These flags
# are part of the provider contract — anything that violates them is a
# bug, not a feature. The frontend reads `category` to group connectors
# in the UI; the cost flags (`billed_metadata`, `requires_compute`)
# document the choice of metadata path so future maintainers don't
# accidentally swap to a billed equivalent.

@dataclass(frozen=True)
class ProviderMeta:
    category: str                    # ui grouping: relational | warehouse | nosql | object_storage | streaming | search_cache | api | saas | collaboration | observability | vector | file_system | integration_metadata | notification | other
    auth: str = "basic"              # basic | iam | api_key | oauth2 | service_account | none
    pagination: str = "none"         # none | cursor | offset | link_header | page_token
    billed_metadata: bool = False    # provider contract: catalog must NOT use a billed metadata path
    requires_compute: bool = False   # provider contract: catalog must NOT spin up a warehouse / cluster
    tier: str = "tier1"              # tier1: no new deps | tier2: lightweight SDK | tier3: heavy/vendor SDK | tier4: documented no-op
    # Honest verification status — stamps how much real-world proof
    # this provider has. The frontend renders this as a badge next to
    # the connector. Never claim higher than evidence supports.
    #   sdk_validated       — code follows canonical API; not run live
    #   sandbox_verified    — verified against emulator / vendor docker (e.g. Azurite, gvenzl/oracle-xe, Cosmos DB Emulator)
    #   integration_verified — verified against a real instance (cloud or self-hosted)
    #   production_ready    — integration_verified + has docs + scopes documented + error UX checked
    #   n_a                  — verification doesn't apply (no-catalog by design)
    verification: str = "sdk_validated"
    # Capability flags — let generic engines drive providers without
    # connector-specific branching. Only `supports_catalog` is required
    # by this module; the rest are forward-looking flags for future
    # capabilities (CDC, streaming, pushdown, incremental sync).
    supports_catalog: bool = True
    supports_cdc: bool = False
    supports_streaming: bool = False
    supports_pushdown: bool = False
    supports_incremental: bool = False
    notes: str = ""


# ── Response shape ──

class CatalogItem(BaseModel):
    """One browsable object — schema-agnostic. `parent` groups items in
    the UI (database name, schema name, bucket, namespace, etc.). `kind`
    drives the icon and which config field receives the click."""
    name: str
    kind: str  # table | view | procedure | synonym | collection | topic | index | bucket | object | key | function
    parent: str = ""  # schema, database, bucket, namespace
    metadata: dict[str, Any] = Field(default_factory=dict)


class Catalog(BaseModel):
    supported: bool = True
    reason: str = ""  # populated when supported=False
    items: list[CatalogItem] = Field(default_factory=list)
    # Distinct values present, computed at build time so frontend doesn't
    # have to re-derive on every render.
    parents: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)
    # Provider-declared metadata, surfaced so the UI can group/sort/badge
    # connectors and so consumers know whether the catalog path is free,
    # what auth was used, etc.
    category: str = ""
    auth: str = ""
    tier: str = ""
    verification: str = ""
    capabilities: dict[str, bool] = Field(default_factory=dict)


def _meta_to_caps(meta: ProviderMeta | None) -> dict[str, bool]:
    if not meta:
        return {}
    return {
        "catalog": meta.supports_catalog,
        "cdc": meta.supports_cdc,
        "streaming": meta.supports_streaming,
        "pushdown": meta.supports_pushdown,
        "incremental": meta.supports_incremental,
    }


def _build(items: list[CatalogItem], meta: ProviderMeta | None = None) -> Catalog:
    """Finalise a Catalog from raw items — populate distinct parents/kinds."""
    parents = sorted({i.parent for i in items if i.parent})
    kinds = sorted({i.kind for i in items})
    return Catalog(
        items=items,
        parents=parents,
        kinds=kinds,
        category=meta.category if meta else "",
        auth=meta.auth if meta else "",
        tier=meta.tier if meta else "",
        verification=meta.verification if meta else "",
        capabilities=_meta_to_caps(meta),
    )


def _unsupported(reason: str, meta: ProviderMeta | None = None) -> Catalog:
    return Catalog(
        supported=False,
        reason=reason,
        category=meta.category if meta else "",
        tier=meta.tier if meta else "",
        verification=meta.verification if meta else "",
        capabilities=_meta_to_caps(meta),
    )


# ── Provider registry ──

CatalogFn = Callable[[dict[str, Any]], Catalog]
_PROVIDERS: dict[str, CatalogFn] = {}
_PROVIDER_META: dict[str, ProviderMeta] = {}
# Disjoint sets — the registry is auditable from outside the module;
# see `registry_status()`. Anything in CONNECTION_TYPES that isn't in
# one of these is unregistered.
_REAL_TYPES: set[str] = set()        # real protocol-level provider
_NO_CATALOG_TYPES: set[str] = set()  # intentional: write-only, no catalog
_PLANNED_TYPES: set[str] = set()     # registered placeholder, awaiting impl


def register(conn_type: str, meta: ProviderMeta) -> Callable[[CatalogFn], CatalogFn]:
    def deco(fn: CatalogFn) -> CatalogFn:
        _PROVIDERS[conn_type] = fn
        _PROVIDER_META[conn_type] = meta
        _REAL_TYPES.add(conn_type)
        # If this type was previously a placeholder (e.g. catalog_extensions
        # is imported AFTER the stub loop ran on a hot reload), promote it
        # cleanly out of the planned bucket so the disjoint-set invariant
        # in registry_status() holds.
        _PLANNED_TYPES.discard(conn_type)
        _NO_CATALOG_TYPES.discard(conn_type)
        return fn
    return deco


def get_catalog(conn_type: str, config: dict[str, Any]) -> Catalog:
    """Dispatch to the registered provider; never raises."""
    fn = _PROVIDERS.get(conn_type.lower())
    meta = _PROVIDER_META.get(conn_type.lower())
    if fn is None:
        return _unsupported(
            f"Catalog browsing is not yet implemented for '{conn_type}'. "
            "The free-text input remains available."
        )
    try:
        result = fn(config)
        # Stamp meta on the response if the provider didn't supply it.
        if meta and not result.category:
            result = result.model_copy(update={
                "category": meta.category,
                "auth": meta.auth,
                "tier": meta.tier,
                "verification": meta.verification,
                "capabilities": _meta_to_caps(meta),
            })
        return result
    except Exception as exc:  # noqa: BLE001 — boundary
        logger.exception("Catalog provider failed for %s", conn_type)
        return _unsupported(f"Catalog query failed: {exc}", meta)


# ── Helpers ──

def _resolve_user(config: dict[str, Any], default: str) -> str:
    return config.get("user") or config.get("username") or default


# ════════════════════════════════════════════════════════════════════
#                          SQL family
# ════════════════════════════════════════════════════════════════════

def _sql_collect(cursor, queries: list[tuple[str, str, str]]) -> list[CatalogItem]:
    """Run a list of (kind, sql, parent_col_or_empty) queries and merge.

    Each query must return rows where:
      - column 0 is the parent (schema/database) — empty string if none
      - column 1 is the item name
    `kind` is applied to every row from that query.
    """
    items: list[CatalogItem] = []
    for kind, sql, _ in queries:
        try:
            cursor.execute(sql)
            for row in cursor.fetchall():
                parent = (row[0] or "") if len(row) > 1 else ""
                name = row[1] if len(row) > 1 else row[0]
                if name:
                    items.append(CatalogItem(name=str(name), kind=kind, parent=str(parent)))
        except Exception as exc:  # noqa: BLE001 — best effort per kind
            logger.debug("SQL catalog query failed for kind=%s: %s", kind, exc)
    return items


# ── PostgreSQL family (postgresql, redshift, cockroachdb) ──

def _postgres_catalog(config: dict[str, Any]) -> Catalog:
    try:
        import psycopg2  # type: ignore
    except ImportError:
        return _unsupported("psycopg2 driver not installed (pip install psycopg2-binary)")

    conn = psycopg2.connect(
        host=config.get("host", "localhost"),
        port=int(config.get("port", 5432)),
        dbname=config.get("database", "postgres"),
        user=_resolve_user(config, "postgres"),
        password=config.get("password", ""),
        connect_timeout=DEFAULT_TIMEOUT,
    )
    try:
        cur = conn.cursor()
        items = _sql_collect(cur, [
            ("table",
             "SELECT table_schema, table_name FROM information_schema.tables "
             "WHERE table_type='BASE TABLE' AND table_schema NOT IN "
             "('pg_catalog','information_schema') ORDER BY 1,2", ""),
            ("view",
             "SELECT table_schema, table_name FROM information_schema.views "
             "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
             "ORDER BY 1,2", ""),
            ("procedure",
             "SELECT routine_schema, routine_name FROM information_schema.routines "
             "WHERE routine_schema NOT IN ('pg_catalog','information_schema') "
             "ORDER BY 1,2", ""),
        ])
        return _build(items)
    finally:
        conn.close()


_PG_META = ProviderMeta(category="relational", auth="basic", pagination="none", tier="tier1")


@register("postgresql", _PG_META)
def _pg(config): return _postgres_catalog(config)


@register("redshift", ProviderMeta(category="warehouse", auth="basic", tier="tier1"))
def _redshift(config):
    cfg = {**config, "port": int(config.get("port", 5439))}
    return _postgres_catalog(cfg)


@register("cockroachdb", _PG_META)
def _cockroach(config):
    cfg = {**config, "port": int(config.get("port", 26257))}
    return _postgres_catalog(cfg)


# ── MySQL family (mysql, mariadb) ──

def _mysql_catalog(config: dict[str, Any]) -> Catalog:
    try:
        import pymysql  # type: ignore
    except ImportError:
        return _unsupported("pymysql driver not installed (pip install pymysql)")

    conn = pymysql.connect(
        host=config.get("host", "localhost"),
        port=int(config.get("port", 3306)),
        database=config.get("database") or None,
        user=_resolve_user(config, "root"),
        password=config.get("password", ""),
        connect_timeout=DEFAULT_TIMEOUT,
    )
    try:
        cur = conn.cursor()
        items = _sql_collect(cur, [
            ("table",
             "SELECT table_schema, table_name FROM information_schema.tables "
             "WHERE table_type='BASE TABLE' AND table_schema NOT IN "
             "('mysql','information_schema','performance_schema','sys') "
             "ORDER BY 1,2", ""),
            ("view",
             "SELECT table_schema, table_name FROM information_schema.views "
             "WHERE table_schema NOT IN "
             "('mysql','information_schema','performance_schema','sys') "
             "ORDER BY 1,2", ""),
            ("procedure",
             "SELECT routine_schema, routine_name FROM information_schema.routines "
             "WHERE routine_type='PROCEDURE' AND routine_schema NOT IN "
             "('mysql','information_schema','performance_schema','sys') "
             "ORDER BY 1,2", ""),
        ])
        return _build(items)
    finally:
        conn.close()


_MYSQL_META = ProviderMeta(category="relational", auth="basic", tier="tier1")


@register("mysql", _MYSQL_META)
def _mysql(config): return _mysql_catalog(config)


@register("mariadb", _MYSQL_META)
def _mariadb(config): return _mysql_catalog(config)


# ── MSSQL family (mssql, synapse) ──

@register("mssql", ProviderMeta(
    category="relational", auth="basic", tier="tier1",
    verification="integration_verified",
    notes="verified live against SQL Server 2022 Express on 2026-05-08"))
def _mssql_catalog(config: dict[str, Any]) -> Catalog:
    try:
        import pyodbc  # type: ignore
    except ImportError:
        return _unsupported("pyodbc driver not installed")

    installed = [d for d in pyodbc.drivers() if "SQL Server" in d]
    preferred = [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
        "SQL Server Native Client 11.0",
        "SQL Server",
    ]
    driver = next((d for d in preferred if d in installed), None) or (installed[0] if installed else None)
    if not driver:
        return _unsupported("No SQL Server ODBC driver installed")

    raw_user = config.get("user") or config.get("username")
    password = config.get("password", "")
    windows_auth = bool(config.get("windows_auth")) or (not raw_user and not password)
    trust_cert = "yes" if (config.get("trust_server_certificate") or "18" in driver) else "no"
    encrypt = "yes" if config.get("encrypt") else ("optional" if "18" in driver else "no")
    auth = "Trusted_Connection=yes;" if windows_auth else f"UID={raw_user or 'sa'};PWD={password};"

    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={config.get('host','localhost')},{int(config.get('port',1433))};"
        f"DATABASE={config.get('database','master')};"
        f"{auth}Encrypt={encrypt};TrustServerCertificate={trust_cert};"
        f"Connection Timeout={DEFAULT_TIMEOUT};"
    )
    conn = pyodbc.connect(conn_str)
    try:
        cur = conn.cursor()
        items = _sql_collect(cur, [
            ("table",
             "SELECT table_schema, table_name FROM information_schema.tables "
             "WHERE table_type='BASE TABLE' ORDER BY 1,2", ""),
            ("view",
             "SELECT table_schema, table_name FROM information_schema.views "
             "ORDER BY 1,2", ""),
            ("procedure",
             "SELECT s.name, p.name FROM sys.procedures p "
             "JOIN sys.schemas s ON p.schema_id=s.schema_id "
             "ORDER BY 1,2", ""),
            ("synonym",
             "SELECT s.name, syn.name FROM sys.synonyms syn "
             "JOIN sys.schemas s ON syn.schema_id=s.schema_id "
             "ORDER BY 1,2", ""),
            ("function",
             "SELECT s.name, o.name FROM sys.objects o "
             "JOIN sys.schemas s ON o.schema_id=s.schema_id "
             "WHERE o.type IN ('FN','IF','TF') ORDER BY 1,2", ""),
        ])
        return _build(items)
    finally:
        conn.close()


@register("synapse", ProviderMeta(category="warehouse", auth="basic", tier="tier1"))
def _synapse(config): return _mssql_catalog(config)


# ── SQLite ──

@register("sqlite", ProviderMeta(category="relational", auth="none", tier="tier1"))
def _sqlite_catalog(config: dict[str, Any]) -> Catalog:
    import sqlite3
    path = config.get("database") or config.get("file") or config.get("path")
    if not path:
        return _unsupported("No database file path configured")
    conn = sqlite3.connect(path, timeout=DEFAULT_TIMEOUT)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        )
        items: list[CatalogItem] = []
        for kind, name in cur.fetchall():
            items.append(CatalogItem(name=str(name), kind=str(kind), parent="main"))
        return _build(items)
    finally:
        conn.close()


# ── DuckDB ──

@register("duckdb", ProviderMeta(category="warehouse", auth="none", tier="tier1"))
def _duckdb_catalog(config: dict[str, Any]) -> Catalog:
    try:
        import duckdb  # type: ignore
    except ImportError:
        return _unsupported("duckdb driver not installed (pip install duckdb)")
    path = config.get("database") or config.get("path") or ":memory:"
    conn = duckdb.connect(path)
    try:
        items: list[CatalogItem] = []
        rows = conn.execute(
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            "ORDER BY 1,2"
        ).fetchall()
        for schema, name, ttype in rows:
            kind = "view" if "VIEW" in str(ttype).upper() else "table"
            items.append(CatalogItem(name=str(name), kind=kind, parent=str(schema or "")))
        return _build(items)
    finally:
        conn.close()


# ── ClickHouse ──

@register("clickhouse", ProviderMeta(category="warehouse", auth="basic", tier="tier1"))
def _clickhouse_catalog(config: dict[str, Any]) -> Catalog:
    try:
        from clickhouse_driver import Client  # type: ignore
    except ImportError:
        return _unsupported("clickhouse-driver not installed (pip install clickhouse-driver)")
    client = Client(
        host=config.get("host", "localhost"),
        port=int(config.get("port", 9000)),
        user=_resolve_user(config, "default"),
        password=config.get("password", ""),
        database=config.get("database") or "default",
        connect_timeout=DEFAULT_TIMEOUT,
    )
    rows = client.execute(
        "SELECT database, name, engine FROM system.tables "
        "WHERE database NOT IN ('system','INFORMATION_SCHEMA','information_schema') "
        "ORDER BY 1,2"
    )
    items: list[CatalogItem] = []
    for db, name, engine in rows:
        kind = "view" if "View" in str(engine) else "table"
        items.append(CatalogItem(name=str(name), kind=kind, parent=str(db),
                                 metadata={"engine": str(engine)}))
    return _build(items)


# ════════════════════════════════════════════════════════════════════
#                          NoSQL
# ════════════════════════════════════════════════════════════════════

@register("mongodb", ProviderMeta(category="nosql", auth="basic", tier="tier1"))
def _mongodb_catalog(config: dict[str, Any]) -> Catalog:
    try:
        from pymongo import MongoClient  # type: ignore
    except ImportError:
        return _unsupported("pymongo driver not installed (pip install pymongo)")
    uri = config.get("uri") or config.get("connection_string")
    if uri:
        client = MongoClient(uri, serverSelectionTimeoutMS=DEFAULT_TIMEOUT * 1000)
    else:
        client = MongoClient(
            host=config.get("host", "localhost"),
            port=int(config.get("port", 27017)),
            username=_resolve_user(config, None) or None,
            password=config.get("password") or None,
            serverSelectionTimeoutMS=DEFAULT_TIMEOUT * 1000,
        )
    try:
        items: list[CatalogItem] = []
        for db_name in client.list_database_names():
            if db_name in ("admin", "local", "config"):
                continue
            try:
                for coll in client[db_name].list_collection_names():
                    items.append(CatalogItem(name=coll, kind="collection", parent=db_name))
            except Exception:  # noqa: BLE001
                continue
        return _build(items)
    finally:
        client.close()


# ════════════════════════════════════════════════════════════════════
#                        Search & Cache
# ════════════════════════════════════════════════════════════════════

def _es_catalog(config: dict[str, Any], default_port: int) -> Catalog:
    try:
        from elasticsearch import Elasticsearch  # type: ignore
    except ImportError:
        return _unsupported("elasticsearch client not installed (pip install elasticsearch)")
    host = config.get("host", "localhost")
    port = int(config.get("port", default_port))
    scheme = config.get("scheme", "http")
    user = config.get("user") or config.get("username")
    password = config.get("password")
    es_kwargs: dict[str, Any] = {"hosts": [f"{scheme}://{host}:{port}"], "request_timeout": DEFAULT_TIMEOUT}
    if user:
        es_kwargs["basic_auth"] = (user, password or "")
    es = Elasticsearch(**es_kwargs)
    try:
        items: list[CatalogItem] = []
        for idx in es.indices.get_alias(index="*").keys():
            if not str(idx).startswith("."):
                items.append(CatalogItem(name=str(idx), kind="index", parent=""))
        return _build(items)
    finally:
        try:
            es.close()
        except Exception:
            pass


_SEARCH_META = ProviderMeta(category="search_cache", auth="basic", tier="tier1")


@register("elasticsearch", _SEARCH_META)
def _es(config): return _es_catalog(config, 9200)


@register("opensearch", _SEARCH_META)
def _opensearch(config): return _es_catalog(config, 9200)


@register("redis", ProviderMeta(category="search_cache", auth="basic", tier="tier1",
                                 notes="catalog samples namespace prefixes via SCAN, not all keys"))
def _redis_catalog(config: dict[str, Any]) -> Catalog:
    try:
        import redis  # type: ignore
    except ImportError:
        return _unsupported("redis client not installed (pip install redis)")
    r = redis.Redis(
        host=config.get("host", "localhost"),
        port=int(config.get("port", 6379)),
        password=config.get("password") or None,
        db=int(config.get("db", 0)),
        socket_timeout=DEFAULT_TIMEOUT,
    )
    try:
        # Use SCAN with a sample limit — KEYS * on a big DB freezes the
        # server. We're showing browsable namespaces, not exhaustive keys.
        items: list[CatalogItem] = []
        seen_prefixes: set[str] = set()
        count = 0
        for key in r.scan_iter(count=200):
            count += 1
            if count > 500:
                break
            k = key.decode("utf-8", errors="replace") if isinstance(key, bytes) else str(key)
            # Group by colon-separated namespace, common Redis convention.
            prefix = k.split(":", 1)[0] if ":" in k else "(root)"
            if prefix not in seen_prefixes:
                seen_prefixes.add(prefix)
                items.append(CatalogItem(name=prefix, kind="key", parent="",
                                         metadata={"sample": True}))
        return _build(items)
    finally:
        try:
            r.close()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════
#                         Streaming
# ════════════════════════════════════════════════════════════════════

@register("kafka", ProviderMeta(category="streaming", auth="basic", tier="tier1"))
def _kafka_catalog(config: dict[str, Any]) -> Catalog:
    try:
        from kafka import KafkaAdminClient  # type: ignore
    except ImportError:
        return _unsupported("kafka-python not installed (pip install kafka-python)")
    bootstrap = (
        config.get("bootstrap_servers")
        or config.get("brokers")
        or f"{config.get('host', 'localhost')}:{int(config.get('port', 9092))}"
    )
    admin = KafkaAdminClient(
        bootstrap_servers=bootstrap,
        request_timeout_ms=DEFAULT_TIMEOUT * 1000,
    )
    try:
        items = [CatalogItem(name=t, kind="topic", parent="")
                 for t in sorted(admin.list_topics()) if not t.startswith("__")]
        return _build(items)
    finally:
        try:
            admin.close()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════
#                         Object Storage
# ════════════════════════════════════════════════════════════════════

def _s3_compat_catalog(config: dict[str, Any], default_endpoint: str | None = None) -> Catalog:
    try:
        import boto3  # type: ignore
        from botocore.config import Config as BotoConfig  # type: ignore
    except ImportError:
        return _unsupported("boto3 not installed (pip install boto3)")

    endpoint = config.get("endpoint_url") or config.get("endpoint") or default_endpoint
    region = config.get("region") or config.get("region_name") or "us-east-1"
    s3_kwargs: dict[str, Any] = {
        "config": BotoConfig(connect_timeout=DEFAULT_TIMEOUT, read_timeout=DEFAULT_TIMEOUT,
                              retries={"max_attempts": 1}),
        "region_name": region,
    }
    if endpoint:
        s3_kwargs["endpoint_url"] = endpoint
    if config.get("access_key_id") or config.get("aws_access_key_id"):
        s3_kwargs["aws_access_key_id"] = config.get("access_key_id") or config.get("aws_access_key_id")
        s3_kwargs["aws_secret_access_key"] = config.get("secret_access_key") or config.get("aws_secret_access_key")
        if config.get("session_token") or config.get("aws_session_token"):
            s3_kwargs["aws_session_token"] = config.get("session_token") or config.get("aws_session_token")

    s3 = boto3.client("s3", **s3_kwargs)
    items: list[CatalogItem] = []

    # If a specific bucket is configured, list objects/prefixes inside.
    bucket = config.get("bucket")
    if bucket:
        paginator = s3.get_paginator("list_objects_v2")
        prefix = config.get("prefix", "") or ""
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/", PaginationConfig={"MaxItems": 500}):
            for cp in page.get("CommonPrefixes", []) or []:
                items.append(CatalogItem(name=cp["Prefix"], kind="prefix", parent=bucket))
            for obj in page.get("Contents", []) or []:
                items.append(CatalogItem(name=obj["Key"], kind="object", parent=bucket,
                                         metadata={"size": obj.get("Size")}))
        return _build(items)

    # Otherwise list buckets at the account level.
    resp = s3.list_buckets()
    for b in resp.get("Buckets", []):
        items.append(CatalogItem(name=b["Name"], kind="bucket", parent=""))
    return _build(items)


_S3_META = ProviderMeta(category="object_storage", auth="iam", tier="tier1")


@register("s3", _S3_META)
def _s3(config): return _s3_compat_catalog(config)


@register("minio", ProviderMeta(category="object_storage", auth="basic", tier="tier1"))
def _minio(config): return _s3_compat_catalog(config, default_endpoint="http://localhost:9000")


# ════════════════════════════════════════════════════════════════════
#                            APIs
# ════════════════════════════════════════════════════════════════════

@register("rest_api", ProviderMeta(category="api", auth="api_key", tier="tier1"))
def _rest_api_catalog(config: dict[str, Any]) -> Catalog:
    """Pull endpoints from an OpenAPI spec when one is reachable;
    otherwise return unsupported with a hint."""
    try:
        import requests  # type: ignore
    except ImportError:
        return _unsupported("requests not installed")

    spec_url = (
        config.get("openapi_url")
        or config.get("spec_url")
        or _join_url(config.get("base_url", ""), "/openapi.json")
    )
    if not spec_url:
        return _unsupported("Set 'openapi_url' on the connection to enable endpoint browsing.")
    try:
        r = requests.get(spec_url, timeout=DEFAULT_TIMEOUT, headers=_rest_headers(config))
        if r.status_code != 200:
            return _unsupported(f"OpenAPI spec at {spec_url} returned HTTP {r.status_code}")
        spec = r.json()
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"OpenAPI spec fetch failed: {exc}")

    items: list[CatalogItem] = []
    paths = spec.get("paths", {}) if isinstance(spec, dict) else {}
    for path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for method in ("get", "post", "put", "patch", "delete"):
            if method in ops:
                tag = ""
                op = ops[method]
                if isinstance(op, dict) and op.get("tags"):
                    tag = str(op["tags"][0])
                items.append(CatalogItem(
                    name=f"{method.upper()} {path}",
                    kind="endpoint",
                    parent=tag,
                    metadata={"summary": (op.get("summary") if isinstance(op, dict) else "") or ""},
                ))
    return _build(items)


@register("oracle_api", ProviderMeta(category="api", auth="api_key", tier="tier1",
                                       notes="Legacy alias of oracle_fusion; reuses the generic REST catalog."))
def _oracle_api(config): return _rest_api_catalog(config)


# 2026-05-23 (U1): Oracle Fusion Cloud — product-specific catalog. Lists
# the canonical FBDI/SCM/HCM resource paths so users can pick an entity
# without spelunking the docs. Static set is a known-honest path: live
# Fusion exposes hundreds of resources but the noisy ones are admin
# meta and we'd rather ship a curated cookbook than a 600-entry dropdown.
_FUSION_FSCM_ENDPOINTS = [
    ("invoices", "Payable Invoices"),
    ("suppliers", "Suppliers"),
    ("purchaseOrders", "Purchase Orders"),
    ("salesOrders", "Sales Orders"),
    ("items", "Items / Inventory"),
    ("receivables", "Receivables"),
    ("journals", "GL Journals"),
    ("budgetaryControlBalances", "Budget Balances"),
]
_FUSION_HCM_ENDPOINTS = [
    ("workers", "Workers"),
    ("emps", "Employees"),
    ("jobs", "Jobs"),
    ("locations", "Locations"),
    ("departments", "Departments"),
    ("absences", "Absences"),
    ("compensationPlans", "Compensation Plans"),
]
_FUSION_CRM_ENDPOINTS = [
    ("accounts", "Accounts"),
    ("opportunities", "Opportunities"),
    ("contacts", "Contacts"),
    ("leads", "Leads"),
    ("activities", "Activities"),
]


@register("oracle_fusion", ProviderMeta(
    category="api", auth="basic", tier="tier1",
    notes="Canonical FBDI/FSCM/HCM/CRM resource list; live introspection deferred."))
def _oracle_fusion_catalog(config: dict[str, Any]) -> Catalog:
    family = (config.get("api_family") or "fscm").lower()
    table = {
        "fscm": ("fscmRestApi", _FUSION_FSCM_ENDPOINTS),
        "hcm":  ("hcmRestApi",  _FUSION_HCM_ENDPOINTS),
        "crm":  ("crmRestApi",  _FUSION_CRM_ENDPOINTS),
    }.get(family, ("fscmRestApi", _FUSION_FSCM_ENDPOINTS))
    parent, endpoints = table
    items = [
        CatalogItem(name=path, kind="resource", parent=parent,
                    metadata={"label": label, "api_family": family})
        for path, label in endpoints
    ]
    return _build(items)


@register("oracle_bip", ProviderMeta(
    category="api", auth="basic", tier="tier1",
    notes="BIP catalog browsing is deferred; lists canonical report folders only."))
def _oracle_bip_catalog(config: dict[str, Any]) -> Catalog:
    # BIP catalog browsing requires a SOAP/REST roundtrip per folder.
    # Ship a curated folder seed list; full live browse is U2's
    # post-1.0 follow-up.
    seeds = [
        ("/Shared Folders", "Shared report root"),
        ("/Users", "Personal report root"),
        ("/Custom", "Custom reports"),
    ]
    items = [
        CatalogItem(name=p, kind="folder", parent="",
                    metadata={"label": lbl})
        for p, lbl in seeds
    ]
    return _build(items)


# 2026-05-23 (V1/V2): SAP product families. The OData catalog provider
# already handles generic OData browsing; the SAP-specific entries
# below delegate to it so the user gets entity-set browsing for free
# once they connect.

@register("sap_s4hana", ProviderMeta(
    category="api", auth="basic", tier="tier1",
    notes="Delegates to the generic OData catalog ($metadata parse)."))
def _sap_s4hana_catalog(config: dict[str, Any]) -> Catalog:
    # Late import — odata catalog lives in catalog_extensions.
    fn = _PROVIDERS.get("odata")
    if not fn:
        return _unsupported("OData provider not registered yet.")
    return fn(config)


@register("sap_successfactors", ProviderMeta(
    category="api", auth="basic", tier="tier1",
    notes="Delegates to the generic OData catalog ($metadata parse)."))
def _sap_successfactors_catalog(config: dict[str, Any]) -> Catalog:
    fn = _PROVIDERS.get("odata")
    if not fn:
        return _unsupported("OData provider not registered yet.")
    return fn(config)


@register("graphql", ProviderMeta(category="api", auth="api_key", tier="tier1"))
def _graphql_catalog(config: dict[str, Any]) -> Catalog:
    try:
        import requests  # type: ignore
    except ImportError:
        return _unsupported("requests not installed")
    url = config.get("url") or config.get("endpoint") or config.get("base_url")
    if not url:
        return _unsupported("Set 'url' on the GraphQL connection to enable type browsing.")
    introspection = {
        "query": "{ __schema { types { name kind } queryType { name } mutationType { name } } }"
    }
    try:
        r = requests.post(url, json=introspection, timeout=DEFAULT_TIMEOUT, headers=_rest_headers(config))
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"GraphQL introspection failed: {exc}")
    types = (data.get("data", {}) or {}).get("__schema", {}).get("types", []) or []
    items: list[CatalogItem] = []
    for t in types:
        name = t.get("name") or ""
        if not name or name.startswith("__"):
            continue
        items.append(CatalogItem(name=name, kind=str(t.get("kind", "type")).lower(), parent=""))
    return _build(items)


def _rest_headers(config: dict[str, Any]) -> dict[str, str]:
    headers = dict(config.get("headers") or {})
    auth_type = (config.get("auth_type") or "").lower()
    if auth_type in ("bearer", "oauth2") and config.get("token"):
        headers["Authorization"] = f"Bearer {config['token']}"
    elif auth_type == "api_key" and config.get("api_key"):
        key_header = config.get("api_key_header") or "X-API-Key"
        headers[key_header] = config["api_key"]
    return headers


def _join_url(base: str, path: str) -> str:
    if not base:
        return ""
    return base.rstrip("/") + "/" + path.lstrip("/")


# ════════════════════════════════════════════════════════════════════
#                  Launch-tier warehouses (Tier 3)
# ════════════════════════════════════════════════════════════════════
# These use vendor SDKs that aren't core deps. Provider returns a clear
# "install fpulse[<extra>]" message when the SDK is absent. The chosen
# metadata path is **always** the cost-free / no-compute one — see
# ProviderMeta(billed_metadata=False, requires_compute=False) and the
# accompanying notes string. A future maintainer who replaces the
# query path must preserve those guarantees.

@register("bigquery", ProviderMeta(
    category="warehouse", auth="service_account", tier="tier3",
    billed_metadata=False, requires_compute=False,
    notes="uses list_datasets + list_tables — free metadata APIs, never INFORMATION_SCHEMA (billed)"))
def _bigquery_catalog(config: dict[str, Any]) -> Catalog:
    try:
        from google.cloud import bigquery  # type: ignore
        from google.oauth2 import service_account  # type: ignore
    except ImportError:
        return _unsupported("google-cloud-bigquery not installed (pip install fpulse[bigquery])")
    project = config.get("project_id") or config.get("project")
    if not project:
        return _unsupported("BigQuery requires 'project_id' on the connection")
    sa_json = config.get("service_account_json") or config.get("credentials_json")
    if sa_json:
        import json as _json
        info = _json.loads(sa_json) if isinstance(sa_json, str) else sa_json
        creds = service_account.Credentials.from_service_account_info(info)
        client = bigquery.Client(project=project, credentials=creds)
    else:
        client = bigquery.Client(project=project)  # ADC
    items: list[CatalogItem] = []
    for ds in client.list_datasets(max_results=200):
        try:
            for tbl in client.list_tables(ds.reference, max_results=500):
                kind = "view" if str(tbl.table_type).upper() == "VIEW" else "table"
                items.append(CatalogItem(name=tbl.table_id, kind=kind,
                                          parent=ds.dataset_id,
                                          metadata={"project": project}))
        except Exception as exc:  # noqa: BLE001
            logger.debug("BigQuery list_tables failed for %s: %s", ds.dataset_id, exc)
    return _build(items)


@register("snowflake", ProviderMeta(
    category="warehouse", auth="basic", tier="tier3",
    billed_metadata=False, requires_compute=False,
    notes="uses SHOW TERSE TABLES/VIEWS IN ACCOUNT — never needs a running warehouse"))
def _snowflake_catalog(config: dict[str, Any]) -> Catalog:
    try:
        import snowflake.connector  # type: ignore
    except ImportError:
        return _unsupported("snowflake-connector-python not installed (pip install fpulse[snowflake])")
    account = config.get("account")
    user = _resolve_user(config, "")
    password = config.get("password", "")
    if not (account and user):
        return _unsupported("Snowflake requires 'account' and 'user' on the connection")
    # Connect WITHOUT specifying a warehouse — SHOW commands don't need one,
    # and we explicitly avoid spinning up compute just to browse metadata.
    conn = snowflake.connector.connect(
        account=account, user=user, password=password,
        login_timeout=DEFAULT_TIMEOUT, network_timeout=DEFAULT_TIMEOUT,
    )
    try:
        cur = conn.cursor()
        items: list[CatalogItem] = []
        # SHOW TERSE returns: created_on, name, kind, database_name, schema_name
        cur.execute("SHOW TERSE TABLES IN ACCOUNT")
        for row in cur.fetchall():
            db, schema, name = row[3], row[4], row[1]
            items.append(CatalogItem(name=name, kind="table", parent=f"{db}.{schema}"))
        cur.execute("SHOW TERSE VIEWS IN ACCOUNT")
        for row in cur.fetchall():
            db, schema, name = row[3], row[4], row[1]
            items.append(CatalogItem(name=name, kind="view", parent=f"{db}.{schema}"))
        return _build(items)
    finally:
        conn.close()


@register("databricks", ProviderMeta(
    category="warehouse", auth="api_key", tier="tier3",
    billed_metadata=False, requires_compute=False,
    notes="Unity Catalog REST API — no SQL warehouse spin-up needed for catalog browse"))
def _databricks_catalog(config: dict[str, Any]) -> Catalog:
    try:
        import requests  # type: ignore
    except ImportError:
        return _unsupported("requests not installed")
    host = (config.get("host") or config.get("workspace_url") or "").strip()
    if host and not host.startswith("http"):
        host = "https://" + host
    host = host.rstrip("/")
    token = config.get("token") or config.get("personal_access_token")
    if not (host and token):
        return _unsupported("Databricks requires 'host' (workspace URL) and 'token' on the connection")
    headers = {"Authorization": f"Bearer {token}"}
    items: list[CatalogItem] = []
    cats = requests.get(f"{host}/api/2.1/unity-catalog/catalogs",
                         headers=headers, timeout=DEFAULT_TIMEOUT)
    if cats.status_code != 200:
        return _unsupported(f"Unity Catalog list returned HTTP {cats.status_code}")
    for cat in cats.json().get("catalogs", []) or []:
        cat_name = cat.get("name")
        if not cat_name:
            continue
        try:
            schemas = requests.get(f"{host}/api/2.1/unity-catalog/schemas",
                                    params={"catalog_name": cat_name},
                                    headers=headers, timeout=DEFAULT_TIMEOUT).json()
        except Exception:  # noqa: BLE001
            continue
        for sch in schemas.get("schemas", []) or []:
            sch_name = sch.get("name")
            if not sch_name:
                continue
            try:
                tables = requests.get(f"{host}/api/2.1/unity-catalog/tables",
                                       params={"catalog_name": cat_name, "schema_name": sch_name,
                                               "max_results": 500},
                                       headers=headers, timeout=DEFAULT_TIMEOUT).json()
            except Exception:  # noqa: BLE001
                continue
            for tbl in tables.get("tables", []) or []:
                kind = "view" if (tbl.get("table_type") or "").upper() == "VIEW" else "table"
                items.append(CatalogItem(name=tbl["name"], kind=kind,
                                          parent=f"{cat_name}.{sch_name}"))
    return _build(items)


# ════════════════════════════════════════════════════════════════════
#               Write-only / no-catalog connectors
# ════════════════════════════════════════════════════════════════════

_WRITE_ONLY_REASON = (
    "This connector is write-only — it has no browsable catalog. "
    "Use the connection as a sink target."
)
_NOTIFY_META = ProviderMeta(
    category="notification", auth="api_key", tier="tier1",
    verification="n_a", supports_catalog=False)
_TEAMS_META = ProviderMeta(
    category="collaboration", auth="oauth2", tier="tier3",
    verification="n_a", supports_catalog=False)

for _t in ("slack", "smtp", "sendgrid", "twilio", "pagerduty"):
    _PROVIDERS[_t] = lambda _cfg, _r=_WRITE_ONLY_REASON, _m=_NOTIFY_META: _unsupported(_r, _m)
    _PROVIDER_META[_t] = _NOTIFY_META
    _NO_CATALOG_TYPES.add(_t)
_PROVIDERS["teams"] = lambda _cfg, _r=_WRITE_ONLY_REASON, _m=_TEAMS_META: _unsupported(_r, _m)
_PROVIDER_META["teams"] = _TEAMS_META
_NO_CATALOG_TYPES.add("teams")


# ════════════════════════════════════════════════════════════════════
#       Integration-metadata connectors (relabeled, not normal sources)
# ════════════════════════════════════════════════════════════════════
# Per reviewer feedback: airbyte / fivetran / dbt are integration
# *platforms*, not data sources. Their "catalog" lists configured
# pipelines / models, not tables. Keeping them in CONNECTION_TYPES is
# fine, but they get a distinct category so the UI can group and
# label them correctly.

_INTEGRATION_REASON = (
    "Integration-platform browsing lists configured pipelines / models, "
    "not data tables. Implementation is planned — free-text input remains."
)
_INTEGRATION_META = ProviderMeta(
    category="integration_metadata", auth="api_key", tier="tier1",
    notes="lists configured connections / models, not underlying data objects",
)
for _t in ("airbyte", "fivetran", "dbt"):
    _PROVIDERS[_t] = lambda _cfg, _r=_INTEGRATION_REASON, _m=_INTEGRATION_META: _unsupported(_r, _m)
    _PROVIDER_META[_t] = _INTEGRATION_META
    _PLANNED_TYPES.add(_t)


# ════════════════════════════════════════════════════════════════════
#           Planned providers — registered, awaiting impl
# ════════════════════════════════════════════════════════════════════
# Each entry carries category metadata so the registry endpoint and
# the frontend's connector picker can group them correctly even though
# the provider itself returns "planned" today.

_STUB_CATALOG: dict[str, ProviderMeta] = {
    # Relational
    "oracle":     ProviderMeta(category="relational", auth="basic", tier="tier3"),
    "db2":        ProviderMeta(category="relational", auth="basic", tier="tier3"),
    "sap_hana":   ProviderMeta(category="relational", auth="basic", tier="tier3"),
    "teradata":   ProviderMeta(category="relational", auth="basic", tier="tier3"),
    "informix":   ProviderMeta(category="relational", auth="basic", tier="tier3"),
    # Warehouses
    "trino":      ProviderMeta(category="warehouse", auth="basic", tier="tier1"),
    "presto":     ProviderMeta(category="warehouse", auth="basic", tier="tier1"),
    "athena":     ProviderMeta(category="warehouse", auth="iam", tier="tier1",
                                billed_metadata=False, requires_compute=False,
                                notes="planned: Glue Data Catalog metadata path, never Athena query API"),
    # NoSQL
    "cassandra":  ProviderMeta(category="nosql", auth="basic", tier="tier2"),
    "couchbase":  ProviderMeta(category="nosql", auth="basic", tier="tier1"),
    "dynamodb":   ProviderMeta(category="nosql", auth="iam", tier="tier1"),
    "cosmosdb":   ProviderMeta(category="nosql", auth="api_key", tier="tier2"),
    "neo4j":      ProviderMeta(category="nosql", auth="basic", tier="tier2"),
    "arangodb":   ProviderMeta(category="nosql", auth="basic", tier="tier2"),
    "firebase":   ProviderMeta(category="nosql", auth="service_account", tier="tier2"),
    # Cloud storage
    "gcs":        ProviderMeta(category="object_storage", auth="service_account", tier="tier2"),
    "azure_blob": ProviderMeta(category="object_storage", auth="api_key", tier="tier2"),
    "adls_gen2":  ProviderMeta(category="object_storage", auth="api_key", tier="tier2"),
    # File systems
    "sftp":       ProviderMeta(category="file_system", auth="basic", tier="tier2"),
    "hdfs":       ProviderMeta(category="file_system", auth="basic", tier="tier1"),
    "local_file": ProviderMeta(category="file_system", auth="none", tier="tier1"),
    "ftp":        ProviderMeta(category="file_system", auth="basic", tier="tier1"),
    # Streaming
    "rabbitmq":   ProviderMeta(category="streaming", auth="basic", tier="tier1"),
    "pulsar":     ProviderMeta(category="streaming", auth="oauth2", tier="tier1"),
    "kinesis":    ProviderMeta(category="streaming", auth="iam", tier="tier1"),
    "eventhub":   ProviderMeta(category="streaming", auth="oauth2", tier="tier2"),
    "sqs":        ProviderMeta(category="streaming", auth="iam", tier="tier1"),
    "pubsub":     ProviderMeta(category="streaming", auth="service_account", tier="tier2"),
    "nats":       ProviderMeta(category="streaming", auth="basic", tier="tier2"),
    # APIs
    "odata":      ProviderMeta(category="api", auth="basic", tier="tier1"),
    "soap":       ProviderMeta(category="api", auth="basic", tier="tier2"),
    # SaaS (mostly OAuth2)
    "salesforce": ProviderMeta(category="saas", auth="oauth2", tier="tier1"),
    "hubspot":    ProviderMeta(category="saas", auth="oauth2", tier="tier1"),
    "zendesk":    ProviderMeta(category="saas", auth="oauth2", tier="tier1"),
    "jira":       ProviderMeta(category="saas", auth="oauth2", tier="tier1"),
    "servicenow": ProviderMeta(category="saas", auth="oauth2", tier="tier1"),
    "sap":        ProviderMeta(category="saas", auth="basic", tier="tier4"),
    "netsuite":   ProviderMeta(category="saas", auth="oauth2", tier="tier1"),
    "dynamics365": ProviderMeta(category="saas", auth="oauth2", tier="tier1"),
    "workday":    ProviderMeta(category="saas", auth="basic", tier="tier4"),
    # Collaboration
    "sharepoint": ProviderMeta(category="collaboration", auth="oauth2", tier="tier1"),
    "onedrive":   ProviderMeta(category="collaboration", auth="oauth2", tier="tier1"),
    "gdrive":     ProviderMeta(category="collaboration", auth="oauth2", tier="tier2"),
    "dropbox":    ProviderMeta(category="collaboration", auth="oauth2", tier="tier1"),
    "box":        ProviderMeta(category="collaboration", auth="oauth2", tier="tier1"),
    "gsheet":     ProviderMeta(category="collaboration", auth="oauth2", tier="tier2"),
    # Search / cache
    "memcached":  ProviderMeta(category="search_cache", auth="none", tier="tier4",
                                notes="memcached has no catalog primitive — likely permanent no-op"),
    "solr":       ProviderMeta(category="search_cache", auth="basic", tier="tier1"),
    # Observability
    "datadog":    ProviderMeta(category="observability", auth="api_key", tier="tier1"),
    "splunk":     ProviderMeta(category="observability", auth="basic", tier="tier1"),
    "newrelic":   ProviderMeta(category="observability", auth="api_key", tier="tier1"),
    "prometheus": ProviderMeta(category="observability", auth="basic", tier="tier1"),
    "grafana":    ProviderMeta(category="observability", auth="api_key", tier="tier1"),
    # Vector
    "pinecone":   ProviderMeta(category="vector", auth="api_key", tier="tier1"),
    "weaviate":   ProviderMeta(category="vector", auth="api_key", tier="tier1"),
    "qdrant":     ProviderMeta(category="vector", auth="api_key", tier="tier1"),
    "chroma":     ProviderMeta(category="vector", auth="none", tier="tier1"),
    "pgvector":   ProviderMeta(category="vector", auth="basic", tier="tier1",
                                notes="pgvector is PostgreSQL — planned to delegate to _postgres_catalog filtered by vector column"),
    "milvus":     ProviderMeta(category="vector", auth="basic", tier="tier1"),
    # Other
    "custom":     ProviderMeta(category="other", auth="none", tier="tier4",
                                notes="custom connectors have no schema by definition — permanent no-op"),
}

_PLANNED_REASON = (
    "Catalog browsing is planned for this connector. The free-text input "
    "remains available; use it to specify the object name manually."
)

for _t, _meta in _STUB_CATALOG.items():
    if _t in _PROVIDERS:  # already real or no-catalog
        continue
    _PROVIDERS[_t] = lambda _cfg, _r=_PLANNED_REASON, _m=_meta: _unsupported(_r, _m)
    _PROVIDER_META[_t] = _meta
    _PLANNED_TYPES.add(_t)


# Import extension providers — their @register decorators fire on import,
# populating _PROVIDERS / _PROVIDER_META / _REAL_TYPES alongside the
# builtins above. Kept at the bottom so the symbols register/ProviderMeta
# /etc. are already defined before catalog_extensions imports them.
# The import is intentionally side-effect-only.
from fpulse.connections import catalog_extensions  # noqa: E402, F401


def registry_status() -> dict[str, Any]:
    """Auditable snapshot of the catalog registry. Counts and lists are
    derived directly from the live registry sets — never hand-edited.
    Adds per-category and per-tier breakdowns so docs/release notes can
    cite numbers that match what the system actually reports.
    """
    by_category: dict[str, list[str]] = {}
    by_tier: dict[str, list[str]] = {}
    by_verification: dict[str, list[str]] = {}
    for t, meta in _PROVIDER_META.items():
        by_category.setdefault(meta.category or "uncategorised", []).append(t)
        by_tier.setdefault(meta.tier or "unknown", []).append(t)
        by_verification.setdefault(meta.verification or "unknown", []).append(t)
    for d in (by_category, by_tier, by_verification):
        for k in d:
            d[k].sort()
    return {
        "real": sorted(_REAL_TYPES),
        "no_catalog": sorted(_NO_CATALOG_TYPES),
        "planned": sorted(_PLANNED_TYPES),
        "by_category": by_category,
        "by_tier": by_tier,
        "by_verification": by_verification,
        "counts": {
            "real": len(_REAL_TYPES),
            "no_catalog": len(_NO_CATALOG_TYPES),
            "planned": len(_PLANNED_TYPES),
            "total_registered": len(_PROVIDERS),
            "by_category": {k: len(v) for k, v in by_category.items()},
            "by_tier": {k: len(v) for k, v in by_tier.items()},
            "by_verification": {k: len(v) for k, v in by_verification.items()},
        },
    }
