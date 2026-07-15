"""Shared types for the bulk-load runner.

`BulkLoadRequest`/`BulkLoadResult` are the dialect-agnostic contract.
`BulkLoaderProtocol` is what each dialect plugin implements.

Kept in a separate module from `__init__.py` so dialect plugins can
import the contract without triggering the runner / registry import
chain (avoids circular imports when plugins register via decorators).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol, runtime_checkable

LoadMode = Literal["create", "append", "truncate", "merge"]


class BulkLoaderNotAvailable(RuntimeError):
    """Raised when no plugin is registered for the requested conn_type, or
    when a plugin's optional dependency (e.g. psycopg2) is not installed.

    Callers SHOULD catch this and fall back to the basic INSERT path so a
    missing optional driver never breaks pipelines that previously worked
    with the row-by-row loader.
    """


@dataclass
class BulkLoadRequest:
    """Inputs to a bulk-load call.

    Most fields map 1:1 onto the WarehouseSinkNode params. Two new ones:

      * `primary_key` — required for `mode='merge'`. Plugins that support
        it generate an idempotent UPSERT/MERGE statement so re-running a
        partially-completed pipeline does not double-insert.
      * `staging_dir` — local filesystem path used for Parquet/CSV staging
        (Snowflake, BigQuery, Redshift, Databricks, MSSQL all stage). The
        runner creates `<data_dir>/staging/<run_id>/` and passes it down.
    """

    conn_type: str                              # 'postgresql' | 'mysql' | ...
    config: dict[str, Any]                      # connection config (host/port/db/user/password)
    table: str                                  # bare table name OR schema.table
    schema_name: str = "public"                 # used when `table` doesn't already include a schema
    mode: LoadMode = "append"
    primary_key: list[str] = field(default_factory=list)
    relation: Any = None                        # DuckDBPyRelation; Any avoids hard duckdb dep at type-check time
    duckdb_conn: Any = None                     # DuckDBPyConnection
    columns: list[str] = field(default_factory=list)   # explicit column ordering; falls back to relation.columns
    staging_dir: Optional[str] = None
    batch_size: int = 100_000                   # rows per chunk on staging path; per-driver default if 0
    compression: str = "zstd"                   # parquet compression codec
    timeout_s: int = 600                        # hard ceiling on the total bulk-load call
    extra: dict[str, Any] = field(default_factory=dict)  # dialect-specific knobs


@dataclass
class BulkLoadResult:
    """Outputs of a bulk-load call.

    `bytes_written` is informational; not every dialect can report it (e.g.
    psycopg2 COPY does not return a byte count). When unknown, plugins
    should set it to None rather than 0 so the caller can tell the
    difference between "wrote 0 bytes" and "the driver does not say".
    """

    rows_loaded: int
    duration_ms: int
    dialect: str
    method: str                                 # "COPY FROM STDIN", "COPY INTO @stage", etc.
    bytes_written: Optional[int] = None
    staged_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class BulkLoaderProtocol(Protocol):
    """Contract every dialect plugin implements.

    Two-method shape so the registry can do a fast feature check
    (`is_available()`) without paying the import cost of an optional
    driver package on systems that aren't using that dialect.
    """

    dialect: str       # canonical conn_type, e.g. "postgresql"
    method: str        # short human label for the bulk method, e.g. "COPY FROM STDIN"

    def is_available(self) -> bool:
        """Return True if the optional driver is importable AND the plugin
        is otherwise ready to run. Called lazily; should not raise."""
        ...

    def load(self, request: BulkLoadRequest) -> BulkLoadResult:
        """Execute the bulk load. Raises on hard failure. Plugins must
        validate `request.relation` is non-None before reading from it
        (the runner enforces this but plugins should be defensive)."""
        ...
