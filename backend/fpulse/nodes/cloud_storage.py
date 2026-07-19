"""
Cloud object-storage nodes — Azure Data Lake Gen2, Azure Blob, Google Cloud Storage.

These are kept *separate* from `s3_source`/`s3_sink` because each cloud has
genuinely different semantics:

  * S3 / MinIO / R2 / Wasabi / B2 — flat namespace, AWS SigV4, `s3://` URI
  * ADLS Gen2                     — hierarchical namespace + POSIX ACLs,
                                    Azure AD / Shared Key / SAS auth,
                                    `abfss://container@account.dfs.core.windows.net/path` URI
  * Azure Blob                    — flat namespace, same Azure auth options,
                                    `az://container/path` or
                                    `wasbs://container@account.blob.core.windows.net/path` URI
  * Google Cloud Storage          — flat namespace, service account JSON or
                                    HMAC keys, `gs://bucket/object` URI

Implementation strategy
-----------------------
Use DuckDB native extensions (`azure`, `httpfs`) so we don't drag in heavy
SDKs.  Each node creates a scoped DuckDB SECRET, then runs `read_*` /
`COPY ... TO` against the cloud URI.  Falls back to a clear error if the
extension cannot be loaded.

All six nodes share `_CloudStorageBase` so file-format detection, format
options, and the empty-relation handling live in one place.
"""

from __future__ import annotations

import os
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for type annotations on helpers and
# execute() returns. Runtime work uses the `conn` argument that
# subclasses are handed (sourced from ctx.conn upstream).
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register


# ─────────────────────────────────────────────────────────
#  Shared base
# ─────────────────────────────────────────────────────────

class _CloudStorageBase(BaseNode):
    """Common helpers for cloud-storage source & sink nodes."""

    # Subclasses set these
    cloud_kind: str = ""           # "azure" or "gcs" — DuckDB extension name
    secret_type: str = ""          # "azure" or "gcs" — DuckDB SECRET TYPE
    uri_scheme: str = ""           # e.g. "abfss" / "az" / "gs"
    connection_kinds: tuple = ()   # accepted connection types from connections store

    # ── DuckDB extension loader ─────────────────────────
    def _ensure_extension(self, conn: duckdb.DuckDBPyConnection) -> None:
        ext = self.cloud_kind
        if not ext:
            return
        try:
            conn.execute(f"INSTALL {ext}")
            conn.execute(f"LOAD {ext}")
        except Exception as exc:
            raise RuntimeError(
                f"{self.display_name}: cannot load DuckDB '{ext}' extension. "
                f"Run `INSTALL {ext}; LOAD {ext};` manually or check network access. "
                f"Original error: {exc}"
            ) from exc

    # ── Secret creation (subclass overrides) ────────────
    def _create_secret(self, conn: duckdb.DuckDBPyConnection, config: dict) -> None:
        """Build a DuckDB SECRET for this cloud from connection/inline params."""
        raise NotImplementedError

    # ── Connection helpers ──────────────────────────────
    def _resolve_credentials(self) -> dict:
        """
        Merge inline params with credentials loaded from a saved connection.
        Inline params win over connection values so users can override.
        """
        config: dict[str, Any] = dict(self.params)
        connection_id = self.params.get("connection_id") or ""
        if connection_id:
            from fpulse.nodes.db_source import _get_connection_config
            result = _get_connection_config(connection_id)
            if result:
                conn_config, conn_kind = result
                if self.connection_kinds and conn_kind not in self.connection_kinds:
                    raise ValueError(
                        f"{self.display_name}: connection kind '{conn_kind}' is not "
                        f"compatible. Expected one of: {', '.join(self.connection_kinds)}."
                    )
                # connection values fill in only what wasn't set inline
                for k, v in conn_config.items():
                    if not config.get(k):
                        config[k] = v
        return config

    # ── File-format detection ───────────────────────────
    @staticmethod
    def _detect_format(path: str, explicit: str = "auto") -> str:
        if explicit and explicit != "auto":
            return explicit
        ext = os.path.splitext(path)[-1].lower()
        return {
            ".parquet": "parquet", ".pq": "parquet",
            ".json": "json", ".ndjson": "json",
            ".csv": "csv", ".tsv": "csv",
        }.get(ext, "csv")

    @staticmethod
    def _read_uri(conn: duckdb.DuckDBPyConnection, uri: str, fmt: str) -> duckdb.DuckDBPyRelation:
        if fmt == "parquet":
            return conn.sql(f"SELECT * FROM read_parquet('{uri}')")
        if fmt == "json":
            return conn.sql(f"SELECT * FROM read_json_auto('{uri}')")
        delim = "\\t" if uri.lower().endswith(".tsv") else ","
        return conn.sql(
            f"SELECT * FROM read_csv_auto('{uri}', delim='{delim}', header=true)"
        )

    @staticmethod
    def _write_uri(conn: duckdb.DuckDBPyConnection, rel: duckdb.DuckDBPyRelation,
                    uri: str, fmt: str) -> None:
        conn.register("__cloud_sink_rel", rel)
        if fmt == "parquet":
            conn.sql(f"COPY __cloud_sink_rel TO '{uri}' (FORMAT PARQUET)")
        elif fmt == "json":
            conn.sql(f"COPY __cloud_sink_rel TO '{uri}' (FORMAT JSON)")
        else:
            conn.sql(f"COPY __cloud_sink_rel TO '{uri}' (FORMAT CSV, HEADER)")


# ─────────────────────────────────────────────────────────
#  Azure Data Lake Gen2
# ─────────────────────────────────────────────────────────

class _AdlsGen2Base(_CloudStorageBase):
    cloud_kind = "azure"
    secret_type = "azure"
    uri_scheme = "abfss"
    connection_kinds = ("azure_datalake", "adls_gen2", "azure")

    def _create_secret(self, conn: duckdb.DuckDBPyConnection, config: dict) -> None:
        """
        Builds an ADLS Gen2 secret from one of three auth styles:
          1. connection_string  — easiest, copy from Azure portal
          2. account_name + account_key
          3. account_name + sas_token
        """
        connection_string = (config.get("connection_string") or "").strip()
        account_name = (config.get("account_name") or "").strip()
        account_key = (config.get("account_key") or "").strip()
        sas_token = (config.get("sas_token") or "").strip()

        if connection_string:
            conn.execute(
                f"CREATE OR REPLACE SECRET fpulse_adls (TYPE AZURE, "
                f"CONNECTION_STRING '{connection_string}')"
            )
        elif account_name and account_key:
            conn.execute(
                f"CREATE OR REPLACE SECRET fpulse_adls (TYPE AZURE, "
                f"PROVIDER ACCESS_TOKEN, ACCOUNT_NAME '{account_name}', "
                f"ACCOUNT_KEY '{account_key}')"
            )
        elif account_name and sas_token:
            conn.execute(
                f"CREATE OR REPLACE SECRET fpulse_adls (TYPE AZURE, "
                f"PROVIDER SAS, ACCOUNT_NAME '{account_name}', "
                f"SAS_TOKEN '{sas_token}')"
            )
        else:
            raise ValueError(
                f"{self.display_name}: provide either a connection string, "
                f"or account_name + account_key, or account_name + sas_token."
            )

    @staticmethod
    def _build_uri(account: str, container: str, path: str) -> str:
        path = (path or "").lstrip("/")
        if not account or not container:
            raise ValueError(
                "Azure Data Lake Gen2: account_name, container, and path are required."
            )
        return f"abfss://{container}@{account}.dfs.core.windows.net/{path}"


@register(StepType.ADLS_GEN2_SOURCE)
class AdlsGen2SourceNode(_AdlsGen2Base):
    display_name = "Azure Data Lake Gen2 Source"
    category = "source"
    description = "Read files from Azure Data Lake Storage Gen2"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        config = self._resolve_credentials()
        path = (config.get("path") or "").strip()
        container = (config.get("container") or config.get("file_system") or "").strip()
        account = (config.get("account_name") or "").strip()
        fmt = self._detect_format(path, config.get("format", "auto"))

        self._ensure_extension(ctx.conn)
        self._create_secret(ctx.conn, config)
        uri = self._build_uri(account, container, path)
        return self._read_uri(ctx.conn, uri, fmt)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "connection_id": "",
            "account_name": "", "container": "", "path": "",
            "connection_string": "", "account_key": "", "sas_token": "",
            "format": "auto",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "Connection",
             "description": "Saved Azure Data Lake Gen2 connection. Or fill below."},
            {"name": "account_name", "type": "text", "label": "Storage Account",
             "placeholder": "mystorageacct", "required": True},
            {"name": "container", "type": "text", "label": "Container / Filesystem",
             "placeholder": "raw", "required": True},
            {"name": "path", "type": "text", "label": "Path / Glob",
             "placeholder": "year=2026/month=04/*.parquet", "required": True},
            {"name": "connection_string", "type": "password", "label": "Connection String (optional)",
             "description": "If set, used for auth. Otherwise account key or SAS."},
            {"name": "account_key", "type": "password", "label": "Account Key"},
            {"name": "sas_token", "type": "password", "label": "SAS Token"},
            {"name": "format", "type": "select", "label": "File Format",
             "options": ["auto", "parquet", "csv", "json"], "default": "auto"},
        ]


@register(StepType.ADLS_GEN2_SINK)
class AdlsGen2SinkNode(_AdlsGen2Base):
    display_name = "Azure Data Lake Gen2 Sink"
    category = "output"
    description = "Write files to Azure Data Lake Storage Gen2"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Azure Data Lake Gen2 Sink: no upstream input")
        rel = inputs[0]

        config = self._resolve_credentials()
        path = (config.get("path") or "").strip()
        container = (config.get("container") or config.get("file_system") or "").strip()
        account = (config.get("account_name") or "").strip()
        fmt = self._detect_format(path, config.get("format", "parquet"))

        self._ensure_extension(ctx.conn)
        self._create_secret(ctx.conn, config)
        uri = self._build_uri(account, container, path)
        self._write_uri(ctx.conn, rel, uri, fmt)
        return rel

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "connection_id": "",
            "account_name": "", "container": "", "path": "",
            "connection_string": "", "account_key": "", "sas_token": "",
            "format": "parquet",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return AdlsGen2SourceNode.param_schema() + [
            # Override format default for sinks
        ]


# ─────────────────────────────────────────────────────────
#  Azure Blob Storage
# ─────────────────────────────────────────────────────────

class _AzureBlobBase(_CloudStorageBase):
    cloud_kind = "azure"
    secret_type = "azure"
    uri_scheme = "az"
    connection_kinds = ("azure_blob", "azure")

    def _create_secret(self, conn: duckdb.DuckDBPyConnection, config: dict) -> None:
        # Same auth model as ADLS Gen2 — share the secret builder.
        return _AdlsGen2Base._create_secret(self, conn, config)

    @staticmethod
    def _build_uri(account: str, container: str, path: str) -> str:
        path = (path or "").lstrip("/")
        if not account or not container:
            raise ValueError(
                "Azure Blob: account_name, container, and path are required."
            )
        return f"az://{container}/{path}"


@register(StepType.AZURE_BLOB_SOURCE)
class AzureBlobSourceNode(_AzureBlobBase):
    display_name = "Azure Blob Source"
    category = "source"
    description = "Read files from Azure Blob Storage"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        config = self._resolve_credentials()
        path = (config.get("path") or "").strip()
        container = (config.get("container") or "").strip()
        account = (config.get("account_name") or "").strip()
        fmt = self._detect_format(path, config.get("format", "auto"))

        self._ensure_extension(ctx.conn)
        self._create_secret(ctx.conn, config)
        uri = self._build_uri(account, container, path)
        return self._read_uri(ctx.conn, uri, fmt)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "connection_id": "",
            "account_name": "", "container": "", "path": "",
            "connection_string": "", "account_key": "", "sas_token": "",
            "format": "auto",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "Connection",
             "description": "Saved Azure Blob connection. Or fill below."},
            {"name": "account_name", "type": "text", "label": "Storage Account",
             "placeholder": "mystorageacct", "required": True},
            {"name": "container", "type": "text", "label": "Container",
             "placeholder": "data", "required": True},
            {"name": "path", "type": "text", "label": "Blob Path / Glob",
             "placeholder": "exports/2026/*.csv", "required": True},
            {"name": "connection_string", "type": "password", "label": "Connection String (optional)"},
            {"name": "account_key", "type": "password", "label": "Account Key"},
            {"name": "sas_token", "type": "password", "label": "SAS Token"},
            {"name": "format", "type": "select", "label": "File Format",
             "options": ["auto", "parquet", "csv", "json"], "default": "auto"},
        ]


@register(StepType.AZURE_BLOB_SINK)
class AzureBlobSinkNode(_AzureBlobBase):
    display_name = "Azure Blob Sink"
    category = "output"
    description = "Write files to Azure Blob Storage"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Azure Blob Sink: no upstream input")
        rel = inputs[0]

        config = self._resolve_credentials()
        path = (config.get("path") or "").strip()
        container = (config.get("container") or "").strip()
        account = (config.get("account_name") or "").strip()
        fmt = self._detect_format(path, config.get("format", "parquet"))

        self._ensure_extension(ctx.conn)
        self._create_secret(ctx.conn, config)
        uri = self._build_uri(account, container, path)
        self._write_uri(ctx.conn, rel, uri, fmt)
        return rel

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "connection_id": "",
            "account_name": "", "container": "", "path": "",
            "connection_string": "", "account_key": "", "sas_token": "",
            "format": "parquet",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return AzureBlobSourceNode.param_schema()


# ─────────────────────────────────────────────────────────
#  Google Cloud Storage
# ─────────────────────────────────────────────────────────

class _GcsBase(_CloudStorageBase):
    cloud_kind = "httpfs"  # GCS uses httpfs + S3-compat HMAC keys
    secret_type = "gcs"
    uri_scheme = "gs"
    connection_kinds = ("gcs", "google_cloud_storage")

    def _create_secret(self, conn: duckdb.DuckDBPyConnection, config: dict) -> None:
        """
        DuckDB GCS secret uses HMAC interop keys (KEY_ID + SECRET).
        Generate them in GCP Console → Cloud Storage → Settings → Interoperability.
        """
        key_id = (config.get("hmac_key_id") or config.get("access_key") or "").strip()
        secret = (config.get("hmac_secret") or config.get("secret_key") or "").strip()

        if not key_id or not secret:
            raise ValueError(
                "Google Cloud Storage: hmac_key_id and hmac_secret are required. "
                "Generate HMAC interop keys in GCP Console → Cloud Storage → Settings → Interoperability."
            )

        conn.execute(
            f"CREATE OR REPLACE SECRET fpulse_gcs (TYPE GCS, "
            f"KEY_ID '{key_id}', SECRET '{secret}')"
        )

    @staticmethod
    def _build_uri(bucket: str, path: str) -> str:
        path = (path or "").lstrip("/")
        if not bucket:
            raise ValueError("Google Cloud Storage: bucket and path are required.")
        return f"gs://{bucket}/{path}"


@register(StepType.GCS_SOURCE)
class GcsSourceNode(_GcsBase):
    display_name = "Google Cloud Storage Source"
    category = "source"
    description = "Read files from Google Cloud Storage"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        config = self._resolve_credentials()
        bucket = (config.get("bucket") or "").strip()
        path = (config.get("path") or "").strip()
        fmt = self._detect_format(path, config.get("format", "auto"))

        self._ensure_extension(ctx.conn)
        self._create_secret(ctx.conn, config)
        uri = self._build_uri(bucket, path)
        return self._read_uri(ctx.conn, uri, fmt)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "connection_id": "",
            "bucket": "", "path": "",
            "hmac_key_id": "", "hmac_secret": "",
            "format": "auto",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "Connection",
             "description": "Saved GCS connection. Or fill HMAC keys below."},
            {"name": "bucket", "type": "text", "label": "Bucket",
             "placeholder": "my-gcs-bucket", "required": True},
            {"name": "path", "type": "text", "label": "Object Path / Glob",
             "placeholder": "exports/*.parquet", "required": True},
            {"name": "hmac_key_id", "type": "text", "label": "HMAC Key ID",
             "description": "GCP Console → Cloud Storage → Settings → Interoperability"},
            {"name": "hmac_secret", "type": "password", "label": "HMAC Secret"},
            {"name": "format", "type": "select", "label": "File Format",
             "options": ["auto", "parquet", "csv", "json"], "default": "auto"},
        ]


@register(StepType.GCS_SINK)
class GcsSinkNode(_GcsBase):
    display_name = "Google Cloud Storage Sink"
    category = "output"
    description = "Write files to Google Cloud Storage"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        inputs = ctx.get_inputs(self.params.get("_input_step_ids", []))
        if not inputs:
            raise ValueError("Google Cloud Storage Sink: no upstream input")
        rel = inputs[0]

        config = self._resolve_credentials()
        bucket = (config.get("bucket") or "").strip()
        path = (config.get("path") or "").strip()
        fmt = self._detect_format(path, config.get("format", "parquet"))

        self._ensure_extension(ctx.conn)
        self._create_secret(ctx.conn, config)
        uri = self._build_uri(bucket, path)
        self._write_uri(ctx.conn, rel, uri, fmt)
        return rel

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "connection_id": "",
            "bucket": "", "path": "",
            "hmac_key_id": "", "hmac_secret": "",
            "format": "parquet",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return GcsSourceNode.param_schema()
