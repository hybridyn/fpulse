"""
Generic Source / Destination nodes.

These two nodes are thin delegating wrappers that pick a concrete source or
sink at runtime based on ``params["connector_type"]``.  The actual execution
is handed off to the existing registered node classes, so this module does
not duplicate any I/O logic.

Why a single "Source" / "Destination" palette entry?
----------------------------------------------------
A generic Source / Destination keeps the canvas small: connector type is a
property of one palette entry rather than a separate component per backend.
F-Pulse keeps the underlying typed nodes (CSV_SOURCE, DB_SOURCE, S3_SINK,
...) — this module just routes from the generic palette entry to the right
implementation.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for type annotations on _delegate and
# execute() returns. This module just routes to other registered nodes.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import NodeRegistry, register


# ── connector_type → concrete StepType ──

SOURCE_MAP: dict[str, StepType] = {
    "csv":         StepType.CSV_SOURCE,
    "json":        StepType.JSON_SOURCE,
    "parquet":     StepType.PARQUET_SOURCE,
    "excel":       StepType.EXCEL_SOURCE,
    "xml":         StepType.XML_SOURCE,
    "database":    StepType.DB_SOURCE,
    "rest_api":    StepType.API_SOURCE,
    "s3":          StepType.S3_SOURCE,
    "azure_blob":  StepType.AZURE_BLOB_SOURCE,
    "gcs":         StepType.GCS_SOURCE,
    "sharepoint":  StepType.SHAREPOINT_SOURCE,
    "onedrive":    StepType.ONEDRIVE_SOURCE,
    "kafka":       StepType.KAFKA_SOURCE,
    "ftp":         StepType.FTP_SOURCE,
    # SFTP (SSH File Transfer) routes to the same node; it reads connector_type
    # to default the protocol to sftp (paramiko). FTP/FTPS use ftplib.
    "sftp":        StepType.FTP_SOURCE,
    "gsheet":      StepType.GSHEET_SOURCE,
    "delta":       StepType.DELTA_SOURCE,
    # 2026-05-22 — generic Microsoft Graph reader. Routes the
    # `source` node to MicrosoftGraphSourceNode when
    # connector_type='microsoft_graph'.
    "microsoft_graph": StepType.MS_GRAPH_SOURCE,
    # 2026-05-23 (Y3) — managed local Parquet table. The Source node
    # accepts `connector_type='local_table'` and routes to the
    # LocalTableSourceNode which reads {DATA_DIR}/tables/{ws}/{schema}/
    # {name}/part-*.parquet by name.
    "local_table": StepType.LOCAL_TABLE_SOURCE,
}

DEST_MAP: dict[str, StepType] = {
    "csv":         StepType.CSV_SINK,
    "json":        StepType.JSON_SINK,
    "parquet":     StepType.FILE_SINK,     # file_sink picks Parquet writer by extension
    "excel":       StepType.EXCEL_SINK,
    "database":    StepType.DB_SINK,
    "s3":          StepType.S3_SINK,
    "azure_blob":  StepType.AZURE_BLOB_SINK,
    "gcs":         StepType.GCS_SINK,
    "sharepoint":  StepType.SHAREPOINT_SINK,
    "onedrive":    StepType.ONEDRIVE_SINK,
    "kafka":       StepType.KAFKA_SINK,
    # FTP/FTPS/SFTP upload — both route to FtpSinkNode, which infers the
    # protocol from connector_type (sftp) / the protocol param.
    "ftp":         StepType.FTP_SINK,
    "sftp":        StepType.FTP_SINK,
    "rest_api":    StepType.API_SINK,
    "webhook":     StepType.API_SINK,      # webhook = POST to URL, reuses API sink
    "email":       StepType.EMAIL_SINK,
    "delta":       StepType.DELTA_SINK,
    "warehouse":   StepType.WAREHOUSE_SINK,
    # 2026-05-23 (Y3) — managed local Parquet table sink.
    "local_table": StepType.LOCAL_TABLE_SINK,
}


def _delegate(target_type: StepType, params: dict[str, Any], ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
    """Instantiate the concrete node class and run it with the same params."""
    node_cls = NodeRegistry.get(target_type)
    node = node_cls(params)
    return node.execute(ctx)


@register(StepType.SOURCE)
class GenericSourceNode(BaseNode):
    """Generic source — routes to a concrete *_SOURCE node by connector_type."""
    display_name = "Source"
    category = "source"
    description = "Read data from a file, database, API, or storage location"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        connector = (self.params.get("connector_type") or "").strip().lower()
        if not connector:
            raise ValueError(
                "Source: no connector_type selected. Open the node config and pick "
                "a data source (CSV, Database, REST API, S3, ...)."
            )
        target = SOURCE_MAP.get(connector)
        if target is None:
            raise ValueError(
                f"Source: unknown connector_type '{connector}'. "
                f"Supported: {', '.join(sorted(SOURCE_MAP.keys()))}."
            )
        return _delegate(target, self.params, ctx)

    @staticmethod
    def default_params() -> dict[str, Any]:
        # Default to CSV so a freshly-dropped Source node immediately
        # shows the file-upload box. The connector picker remains visible
        # so users can switch to Database / API / S3 / etc. at any time.
        return {"connector_type": "csv"}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {
                "name": "connector_type",
                "type": "select",
                "label": "Connector Type",
                "required": True,
                "options": list(SOURCE_MAP.keys()),
            },
        ]

    @staticmethod
    def connector_schemas() -> dict[str, list]:
        """Per-connector param schemas of the delegated concrete source nodes.

        The generic Source has a single static ``param_schema`` (just
        ``connector_type``) because the REAL fields are mode-dependent —
        ``file_path`` for csv, ``query`` + ``connection_id`` for database, etc.
        This exposes the full per-connector field set so the frontend /
        validator / AI / docs see the true contract per ``connector_type``
        instead of relying on hand-maintained special cases. Surfaced on
        ``/api/node-types``.
        """
        out: dict[str, list] = {}
        for ct, stype in SOURCE_MAP.items():
            try:
                out[ct] = NodeRegistry.get(stype).param_schema()
            except Exception:  # noqa: BLE001 — best-effort; a bad node skips
                out[ct] = []
        return out


@register(StepType.DESTINATION)
class GenericDestinationNode(BaseNode):
    """Generic destination — routes to a concrete *_SINK node by connector_type."""
    display_name = "Destination"
    category = "sink"
    description = "Write data to a file, database, or storage location"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        connector = (self.params.get("connector_type") or "").strip().lower()
        if not connector:
            raise ValueError(
                "Destination: no connector_type selected. Open the node config and pick "
                "a target (CSV, Database, S3, Kafka, REST API, ...)."
            )
        target = DEST_MAP.get(connector)
        if target is None:
            raise ValueError(
                f"Destination: unknown connector_type '{connector}'. "
                f"Supported: {', '.join(sorted(DEST_MAP.keys()))}."
            )
        return _delegate(target, self.params, ctx)

    @staticmethod
    def default_params() -> dict[str, Any]:
        # Default to CSV so a freshly-dropped Destination node has a
        # working sub-config visible. Users can pick Database / S3 / etc.
        # afterward if they need a different target.
        return {"connector_type": "csv"}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {
                "name": "connector_type",
                "type": "select",
                "label": "Connector Type",
                "required": True,
                "options": list(DEST_MAP.keys()),
            },
        ]

    @staticmethod
    def connector_schemas() -> dict[str, list]:
        """Per-connector param schemas of the delegated concrete sink nodes —
        the real fields each connector_type needs (file_path, table/connection,
        topic, ...) which the single static param_schema can't express. Surfaced
        on /api/node-types so validation / AI / docs see the true contract."""
        out: dict[str, list] = {}
        for ct, stype in DEST_MAP.items():
            try:
                out[ct] = NodeRegistry.get(stype).param_schema()
            except Exception:  # noqa: BLE001
                out[ct] = []
        return out
