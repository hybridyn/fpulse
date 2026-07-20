"""
Bulk-load runner — Sprint 1 / Gate 1.

Per-dialect plugin pattern that gives each certified database a native
bulk-load path (COPY FROM STDIN, COPY INTO Parquet stage, BULK INSERT,
direct-path INSERT, native Mongo bulk_write, etc.) instead of the basic
row-by-row INSERT that the WarehouseSinkNode does today.

Public API:

    from fpulse.engine.bulk_load import bulk_load, BulkLoadRequest

    result = bulk_load(BulkLoadRequest(
        conn_type="postgresql",
        config={...},                    # connection config from connections store
        table="public.customers",
        primary_key=["customer_id"],     # optional, enables MERGE on re-run
        mode="append",                   # 'create' | 'append' | 'truncate' | 'merge'
        duckdb_conn=ctx.conn,
        relation=source,                 # DuckDBPyRelation from upstream
    ))

The runner picks the dialect plugin via `registry.get(conn_type)`. If no
plugin is registered, the call raises `BulkLoaderNotAvailable` and the
caller is expected to fall back to the row-by-row path.

Why a separate package from `nodes/sinks.py`:

  * Clean dialect boundary — adding the next dialect (Snowflake, BigQuery,
    Redshift, Databricks, MSSQL, Oracle, Mongo, ClickHouse) is one new
    file under dialects/, no edits to existing dialects.
  * Testable in isolation — each plugin has its own unit + smoke tests
    that don't need the full sink-node executor stack.
  * Reusable — bulk-load is also useful from CLI tooling (CSV → DB
    one-shot loader for migrations) and from the planner's "rewrite this
    INSERT-loop into a bulk load" optimization.
"""

from .types import (
    BulkLoadRequest,
    BulkLoadResult,
    BulkLoaderProtocol,
    BulkLoaderNotAvailable,
    LoadMode,
)
from .registry import register, get, available_dialects
from .runner import bulk_load

__all__ = [
    "BulkLoadRequest",
    "BulkLoadResult",
    "BulkLoaderProtocol",
    "BulkLoaderNotAvailable",
    "LoadMode",
    "register",
    "get",
    "available_dialects",
    "bulk_load",
]
