"""Redshift bulk-load via S3 staging + COPY FROM.

Why Redshift:
  * Redshift's recommended bulk-load path is COPY from S3 (or other
    cloud storage). Row-by-row INSERT is 100-1000× slower at >10k rows.
  * The COPY command supports Parquet directly with column projection +
    type inference, no manual schema mapping.

Pipeline:
  1. Write rows to a local Parquet under `<staging_dir>/<uuid>.parquet`.
  2. Upload to `s3://<staging_bucket>/<staging_prefix>/<uuid>.parquet`
     using boto3.
  3. `COPY <target> FROM 's3://...' IAM_ROLE '<arn>' FORMAT AS PARQUET`
     OR `CREDENTIALS 'aws_access_key_id=...;aws_secret_access_key=...'`
     for non-IAM-role environments.
  4. Mode='merge' loads into a staging table, then runs a transactional
     DELETE + INSERT (Redshift has no native MERGE pre-RA3.14).
  5. `DELETE FROM s3://<staging_bucket>/<key>` after success.

Optional dependencies: `redshift-connector` (or `psycopg2-binary` —
Redshift speaks the Postgres wire protocol) + `boto3` for the S3
upload + `pyarrow` for the Parquet writer.

Connection config keys (from F-Pulse `connections` store):
  host, port (default 5439), database, user, password,
  iam_role_arn (preferred), aws_access_key_id, aws_secret_access_key,
  region, staging_bucket (required), staging_prefix (default 'fpulse_bulk/').
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
import uuid
from typing import Any, Optional

from ..registry import register
from ..types import (
    BulkLoaderNotAvailable,
    BulkLoadRequest,
    BulkLoadResult,
)

logger = logging.getLogger(__name__)


def _try_import_drivers():
    """Need a Redshift wire driver, boto3, and pyarrow. Returns
    (db_driver_module, boto3_module) or (None, None)."""
    try:
        import boto3  # type: ignore[import-not-found]
        import pyarrow  # noqa: F401
    except ImportError:
        return None, None
    # Prefer redshift-connector; fall back to psycopg2 (Redshift speaks PG wire).
    try:
        import redshift_connector  # type: ignore[import-not-found]
        return redshift_connector, boto3
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore[import-not-found]
        return psycopg2, boto3
    except ImportError:
        return None, None


def _quote_ident(name: str) -> str:
    """Redshift identifier: double-quoted, internal quotes doubled."""
    return '"' + str(name).replace('"', '""') + '"'


def _qualified_table(schema_name: str, table: str) -> str:
    parts = table.split(".")
    if len(parts) == 1:
        return f"{_quote_ident(schema_name)}.{_quote_ident(parts[0])}"
    return ".".join(_quote_ident(p) for p in parts)


class RedshiftBulkLoader:
    """Redshift COPY-from-S3 bulk loader."""

    dialect = "redshift"
    method = "COPY FROM s3 (Parquet)"

    def is_available(self) -> bool:
        db, s3 = _try_import_drivers()
        return db is not None and s3 is not None

    def _connect(self, config: dict[str, Any]):
        db_driver, _ = _try_import_drivers()
        if db_driver is None:
            raise BulkLoaderNotAvailable(
                "redshift dialect requires redshift-connector (or psycopg2) + boto3 + pyarrow"
            )
        # The two drivers have similar connect() signatures.
        connect_kwargs = {
            "host": config["host"],
            "port": int(config.get("port", 5439)),
            "database": config["database"],
            "user": config["user"],
            "password": config["password"],
        }
        return db_driver.connect(**connect_kwargs)

    def _s3_client(self, config: dict[str, Any]):
        _, boto3 = _try_import_drivers()
        if boto3 is None:
            raise BulkLoaderNotAvailable("redshift dialect requires boto3")
        s3_kwargs: dict[str, Any] = {}
        if config.get("region"):
            s3_kwargs["region_name"] = config["region"]
        if config.get("aws_access_key_id") and config.get("aws_secret_access_key"):
            s3_kwargs["aws_access_key_id"] = config["aws_access_key_id"]
            s3_kwargs["aws_secret_access_key"] = config["aws_secret_access_key"]
        return boto3.client("s3", **s3_kwargs)

    def _write_parquet(self, request: BulkLoadRequest, staging_dir: str) -> tuple[str, int, int]:
        """Write rows to a Parquet file. Returns (path, row_count, bytes)."""
        rel = request.relation
        if rel is None:
            raise ValueError("redshift: BulkLoadRequest.relation is required")
        path = os.path.join(staging_dir, f"{uuid.uuid4().hex}.parquet")
        rel.write_parquet(path, compression=request.compression or "zstd")
        try:
            rc = rel.count("*").fetchone()[0]
        except Exception:
            rc = 0
        size = os.path.getsize(path) if os.path.exists(path) else 0
        return path, int(rc), int(size)

    def _build_copy_sql(self, target: str, s3_url: str, config: dict[str, Any]) -> str:
        """Compose the COPY statement, choosing IAM-role or key-based auth."""
        if config.get("iam_role_arn"):
            auth = f"IAM_ROLE '{config['iam_role_arn']}'"
        elif config.get("aws_access_key_id") and config.get("aws_secret_access_key"):
            # Inline creds — last-resort fallback. Operators should prefer IAM roles.
            auth = (
                f"CREDENTIALS 'aws_access_key_id={config['aws_access_key_id']};"
                f"aws_secret_access_key={config['aws_secret_access_key']}'"
            )
        else:
            raise ValueError(
                "redshift: COPY needs auth — set 'iam_role_arn' (recommended) or aws_access_key_id+aws_secret_access_key"
            )
        return (
            f"COPY {target} FROM '{s3_url}' {auth} FORMAT AS PARQUET"
        )

    # ── Public load() ───────────────────────────────────────────────

    def load(self, request: BulkLoadRequest) -> BulkLoadResult:
        config = request.config
        bucket = config.get("staging_bucket")
        if not bucket:
            raise ValueError("redshift: connection config requires 'staging_bucket'")
        prefix = config.get("staging_prefix", "fpulse_bulk/").rstrip("/") + "/"

        staging_dir = request.staging_dir or tempfile.gettempdir()
        os.makedirs(staging_dir, exist_ok=True)

        target = _qualified_table(request.schema_name or "public", request.table)
        start = time.monotonic()

        parquet_path, row_count, bytes_written = self._write_parquet(request, staging_dir)
        s3_key = f"{prefix}{os.path.basename(parquet_path)}"
        s3_url = f"s3://{bucket}/{s3_key}"

        s3 = self._s3_client(config)
        s3.upload_file(parquet_path, bucket, s3_key)

        conn = self._connect(config)
        try:
            cur = conn.cursor()
            try:
                if request.mode == "truncate":
                    cur.execute(f"TRUNCATE TABLE {target}")

                if request.mode == "merge":
                    if not request.primary_key:
                        raise ValueError("redshift: mode='merge' requires primary_key")
                    # Stage table → DELETE existing matches → INSERT all from staging.
                    stage = f"{request.table}__fpulse_stage_{uuid.uuid4().hex[:8]}"
                    stage_q = _qualified_table(request.schema_name or "public", stage)
                    try:
                        cur.execute(f"CREATE TEMP TABLE {stage_q} (LIKE {target})")
                        cur.execute(self._build_copy_sql(stage_q, s3_url, config))
                        on_clause = " AND ".join(
                            f"{target}.{_quote_ident(k)} = {stage_q}.{_quote_ident(k)}"
                            for k in request.primary_key
                        )
                        cur.execute(f"DELETE FROM {target} USING {stage_q} WHERE {on_clause}")
                        cur.execute(f"INSERT INTO {target} SELECT * FROM {stage_q}")
                    finally:
                        try:
                            cur.execute(f"DROP TABLE IF EXISTS {stage_q}")
                        except Exception as exc:
                            logger.warning("redshift: failed to drop stage %s: %s", stage_q, exc)
                else:
                    cur.execute(self._build_copy_sql(target, s3_url, config))

                conn.commit()
            finally:
                cur.close()
        finally:
            conn.close()

        # Cleanup S3 staging file + local Parquet. Best-effort.
        try:
            s3.delete_object(Bucket=bucket, Key=s3_key)
        except Exception as exc:
            logger.warning("redshift: failed to remove staging object %s: %s", s3_url, exc)
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
            staged_files=[s3_url],
        )


register(RedshiftBulkLoader())
