"""BigQuery bulk-load via load_table_from_file (Parquet) or batch insert.

Why BigQuery on the Gate 2 roadmap:
  * High-demand among analytics-heavy buyers; second-most-asked after
    Snowflake among the regulated segment.
  * BigQuery's recommended bulk path is the load-job API with a
    Parquet/JSON source — not streaming `tabledata.insertAll` (which
    has per-row cost + insertion buffer that complicates re-runs).
  * Idempotent merge via `MERGE INTO target USING temp` — same shape
    as Snowflake.

Pipeline:
  1. Write rows to a local Parquet under `<staging_dir>/<uuid>.parquet`
     (zstd compression — ratio + decode speed beat snappy on most data).
  2. `client.load_table_from_file(...)` with `WriteDisposition` set per
     mode: WRITE_TRUNCATE for 'truncate', WRITE_APPEND for 'append',
     WRITE_EMPTY for 'create'.
  3. Mode='merge' loads into `<target>__fpulse_stage_<uuid>` then
     `MERGE INTO target USING staging` keyed on `primary_key`, then
     drops the staging table.

Optional dependency: `google-cloud-bigquery` + `pyarrow` (for the
Parquet writer). Listed under `[project.optional-dependencies]
bigquery = [...]`. Without these, `is_available()` returns False and
the runner raises `BulkLoaderNotAvailable`.

Auth: BigQuery client picks up service-account JSON via the standard
`GOOGLE_APPLICATION_CREDENTIALS` env var, OR via an explicit
`service_account_json` (string) passed in `request.config`. F-Pulse's
Credentials page stores this as a single secret value.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from typing import Any

from ..registry import register
from ..types import (
    BulkLoaderNotAvailable,
    BulkLoadRequest,
    BulkLoadResult,
)

logger = logging.getLogger(__name__)


def _try_import_driver():
    """Both google-cloud-bigquery AND pyarrow are required. Return the
    bigquery module on success; None if either is missing."""
    try:
        import pyarrow  # noqa: F401
        from google.cloud import bigquery  # type: ignore[import-not-found]
        return bigquery
    except ImportError:
        return None


def _quote_ident(name: str) -> str:
    """BigQuery identifier: backtick-quoted; backticks not allowed in idents."""
    safe = str(name).replace("`", "")
    return f"`{safe}`"


def _qualified_table(dataset: str, table: str) -> str:
    """Resolve `dataset.table` (or `project.dataset.table`)."""
    parts = table.split(".")
    if len(parts) == 1:
        return f"{_quote_ident(dataset)}.{_quote_ident(parts[0])}"
    return ".".join(_quote_ident(p) for p in parts)


class BigQueryBulkLoader:
    """BigQuery load-job bulk loader.

    Connection config keys (from F-Pulse `connections` store):
      project_id (required), dataset (required, used as schema_name fallback),
      service_account_json (str — optional, falls back to ADC), location
      (default 'US'), key_file_path (alt to inline JSON).
    """

    dialect = "bigquery"
    method = "load_table_from_file (Parquet)"

    def is_available(self) -> bool:
        return _try_import_driver() is not None

    def _connect(self, config: dict[str, Any]):
        driver = _try_import_driver()
        if driver is None:
            raise BulkLoaderNotAvailable(
                "bigquery dialect requires google-cloud-bigquery and pyarrow"
            )
        project = config.get("project_id") or config.get("project")
        if not project:
            raise ValueError("bigquery: connection config requires 'project_id'")

        # Auth resolution — explicit JSON > key file path > ADC.
        sa_json = config.get("service_account_json")
        key_path = config.get("key_file_path")
        if sa_json:
            try:
                from google.oauth2 import service_account  # type: ignore[import-not-found]
                info = json.loads(sa_json) if isinstance(sa_json, str) else sa_json
                creds = service_account.Credentials.from_service_account_info(info)
                return driver.Client(project=project, credentials=creds, location=config.get("location", "US"))
            except Exception as exc:
                raise BulkLoaderNotAvailable(f"bigquery: invalid service_account_json: {exc}")
        if key_path:
            return driver.Client.from_service_account_json(key_path, project=project)
        # Application Default Credentials (env var GOOGLE_APPLICATION_CREDENTIALS, or GCE metadata)
        return driver.Client(project=project, location=config.get("location", "US"))

    # ── Parquet staging ──────────────────────────────────────────────

    def _write_parquet(self, request: BulkLoadRequest, staging_dir: str) -> tuple[str, int]:
        """Write the DuckDB relation to a Parquet file via DuckDB's COPY.
        Returns (path, row_count). DuckDB's Parquet writer is faster than
        round-tripping through pandas and uses less memory."""
        rel = request.relation
        if rel is None:
            raise ValueError("bigquery: BulkLoadRequest.relation is required")
        path = os.path.join(staging_dir, f"{uuid.uuid4().hex}.parquet")
        # COPY (SELECT * FROM rel) TO 'path' (FORMAT 'parquet', COMPRESSION 'zstd')
        rel.write_parquet(path, compression=request.compression or "zstd")
        # row_count from the relation if available
        try:
            rc = rel.count("*").fetchone()[0]
        except Exception:
            rc = 0
        return path, int(rc)

    # ── Public load() ───────────────────────────────────────────────

    def load(self, request: BulkLoadRequest) -> BulkLoadResult:
        client = self._connect(request.config)
        driver = _try_import_driver()
        assert driver is not None  # _connect would have raised
        start = time.monotonic()

        dataset = request.schema_name or request.config.get("dataset") or "default"
        target = _qualified_table(dataset, request.table)
        staging_dir = request.staging_dir or tempfile.gettempdir()
        os.makedirs(staging_dir, exist_ok=True)

        parquet_path, row_count = self._write_parquet(request, staging_dir)
        bytes_written = os.path.getsize(parquet_path) if os.path.exists(parquet_path) else None

        # Map mode → WriteDisposition
        write_disposition = {
            "create": "WRITE_EMPTY",
            "append": "WRITE_APPEND",
            "truncate": "WRITE_TRUNCATE",
            "merge": "WRITE_APPEND",  # merge handled separately below
        }.get(request.mode, "WRITE_APPEND")

        if request.mode == "merge":
            if not request.primary_key:
                raise ValueError("bigquery: mode='merge' requires primary_key")
            # Stage to a temp table, then MERGE into target.
            stage_table = f"{request.table}__fpulse_stage_{uuid.uuid4().hex[:8]}"
            stage_qualified = _qualified_table(dataset, stage_table)
            try:
                self._load_to_table(client, driver, parquet_path, stage_qualified, "WRITE_TRUNCATE")
                self._merge(client, target, stage_qualified, request.primary_key)
            finally:
                # Drop the staging table even if MERGE failed; ignore drop errors.
                try:
                    client.query(f"DROP TABLE IF EXISTS {stage_qualified}").result()
                except Exception as exc:
                    logger.warning("bigquery: failed to drop staging table %s: %s", stage_qualified, exc)
        else:
            self._load_to_table(client, driver, parquet_path, target, write_disposition)

        # Cleanup the local Parquet file regardless of outcome above.
        try:
            os.remove(parquet_path)
        except OSError:
            pass

        elapsed = int((time.monotonic() - start) * 1000)
        return BulkLoadResult(
            rows_loaded=row_count,
            duration_ms=elapsed,
            dialect=self.dialect,
            method=self.method,
            bytes_written=bytes_written,
            staged_files=[parquet_path],
        )

    # ── Helpers ──────────────────────────────────────────────────────

    def _load_to_table(self, client, driver, parquet_path: str, target: str, write_disposition: str) -> None:
        """Run a single load job from local Parquet."""
        job_config = driver.LoadJobConfig(
            source_format=driver.SourceFormat.PARQUET,
            write_disposition=write_disposition,
        )
        with open(parquet_path, "rb") as f:
            job = client.load_table_from_file(f, target, job_config=job_config)
        # Block until the load completes; raises on error.
        job.result(timeout=None)

    def _merge(self, client, target: str, stage: str, primary_key: list[str]) -> None:
        """MERGE staging into target, idempotent on primary_key."""
        on_clause = " AND ".join(
            f"T.{_quote_ident(k)} = S.{_quote_ident(k)}" for k in primary_key
        )
        # Pull column names from the staging table to build the SET / INSERT clauses.
        # Cheaper than DESCRIBing target — staging is what we just wrote.
        rows = list(client.query(f"SELECT * FROM {stage} LIMIT 0").result())
        # rows is empty but the result has the schema:
        schema = client.get_table(stage.replace("`", "")).schema
        cols = [f.name for f in schema]
        set_clause = ", ".join(f"{_quote_ident(c)} = S.{_quote_ident(c)}" for c in cols if c not in primary_key)
        insert_cols = ", ".join(_quote_ident(c) for c in cols)
        insert_vals = ", ".join(f"S.{_quote_ident(c)}" for c in cols)
        sql = (
            f"MERGE INTO {target} T USING {stage} S ON {on_clause}\n"
            f"WHEN MATCHED THEN UPDATE SET {set_clause}\n"
            f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
        )
        client.query(sql).result(timeout=None)


# Register at module bottom — triggers on import via dialects/__init__.py
register(BigQueryBulkLoader())
