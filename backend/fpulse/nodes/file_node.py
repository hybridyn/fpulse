"""
Universal File Source / Sink — one node per direction, format auto-detected.

Why a single node instead of CSV/JSON/Parquet/Excel/XML tiles?
  Most users just want "read this file". The format is already encoded in
  the filename. Asking them to pick the right tile out of five is friction.
  This node sniffs the extension, dispatches to the right reader/writer,
  and only surfaces format-specific options when relevant.

Format detection rules (extension → handler):
  .csv  .tsv                       → DuckDB read_csv_auto
  .json .ndjson .jsonl             → DuckDB read_json_auto
  .parquet .pq                     → DuckDB read_parquet
  .xlsx .xls                       → openpyxl → DuckDB
  .xml                             → ElementTree → DuckDB
  (anything else)                  → user must override format explicitly

Power users who want zero ambiguity can still use the dedicated CSV /
JSON / Parquet / Excel / XML tiles — those nodes remain registered.
The default palette just hides them in favour of this universal tile.
"""

from __future__ import annotations

import itertools
import json
import os
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb is referenced only in type annotations — node
# execute() return types and helper signatures. Runtime work goes
# through ctx.conn (owned by ExecutionContext).
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register


# ── Extension → format mapping ──

_EXT_FORMAT = {
    ".csv":     "csv",
    ".tsv":     "tsv",
    ".txt":     "csv",      # treat as CSV with user-provided delimiter
    ".json":    "json",
    ".ndjson":  "ndjson",
    ".jsonl":   "ndjson",
    ".parquet": "parquet",
    ".pq":      "parquet",
    ".xlsx":    "excel",
    ".xls":     "excel",
    ".xml":     "xml",
    # 2026-06-01: columnar-format readers — both go through pyarrow then
    # land in DuckDB as a relation via `conn.from_arrow()`. ORC has no
    # new dependency (pyarrow is already in requirements); Avro requires
    # the `fastavro` package (also added to requirements.txt).
    ".avro":    "avro",
    ".orc":     "orc",
}


def _detect_format(file_path: str, override: str = "") -> str:
    """Return one of csv/tsv/json/ndjson/parquet/excel/xml."""
    if override and override != "auto":
        return override
    ext = os.path.splitext(file_path)[1].lower()
    fmt = _EXT_FORMAT.get(ext)
    if not fmt:
        raise ValueError(
            f"File node: cannot detect format for '{file_path}' (extension '{ext}'). "
            f"Set the Format field explicitly. Supported: {sorted(set(_EXT_FORMAT.values()))}"
        )
    return fmt


def _resolve(file_path: str, data_dir: str) -> str:
    """Resolve a file_path for WRITES. Always joins relative paths with
    `data_dir` so sinks land deterministically — never redirects to a
    pre-existing file elsewhere. Read-path resolution uses
    `_resolve_read` below (which has a project-root fallback for
    sample-pack pipelines)."""
    if not file_path:
        raise ValueError("File node: file_path is required")
    if not os.path.isabs(file_path):
        file_path = os.path.join(data_dir, file_path)
    return file_path


def _resolve_read(file_path: str, data_dir: str) -> str:
    """Resolve a file_path for READS. Delegates to the shared helper so
    sample-pack project-relative paths (e.g.
    `samples/free-api-pipelines/data/orders.csv`) work without the
    data_dir-doubling workaround. See `_path_utils.py` for the full
    rationale (2026-05-26 fix)."""
    if not file_path:
        raise ValueError("File node: file_path is required")
    from fpulse.nodes._path_utils import resolve_input_path
    return resolve_input_path(file_path, data_dir)


# ── Source ──

DEV_SAMPLE_ROWS = 1000  # limit in dev mode; full_run bypasses

# Avro is read in bounded batches so peak Python-heap is one batch, not the
# whole file (see FileSourceNode._read_avro). Process-global counter gives each
# read a collision-free DuckDB temp-table name.
_AVRO_BATCH_ROWS = 50_000
_AVRO_TMP_SEQ = itertools.count()


@register(StepType.FILE_SOURCE)
class FileSourceNode(BaseNode):
    """Read any supported file format. Format detected from extension.

    Enterprise features:
      - Connection-based file access (S3, SFTP, etc.) — resolves path via connection
      - Multi-file glob patterns (data/*.csv)
      - Encoding selection (utf-8, latin-1, cp1252, etc.)
      - Compression (auto-detected: .gz, .bz2, .zst, .xz)
      - Dev sample limit (DEV_SAMPLE_ROWS) — bypassed in full_run mode
      - File not found → clear error with path
    """
    display_name = "File"
    category = "source"
    description = "Read a file — format detected automatically from the file extension"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        raw_path = self.params.get("file_path") or self.params.get("path") or ""
        connection_id = self.params.get("connection_id", "")

        # Connection-based path resolution
        if connection_id:
            raw_path = self._resolve_via_connection(ctx, connection_id, raw_path)

        # Glob patterns are passed straight through (DuckDB handles them)
        if "*" not in raw_path:
            file_path = _resolve_read(raw_path, ctx.data_dir)
            if not os.path.isfile(file_path):
                raise ValueError(f"File: not found: {file_path}")
        else:
            file_path = _resolve_read(raw_path, ctx.data_dir)

        fmt = _detect_format(file_path, self.params.get("format", "auto"))
        encoding = self.params.get("encoding", "utf-8")

        rel = self._read_by_format(ctx, file_path, fmt, encoding)

        # Dev sample limit
        if not ctx.full_run:
            limit = int(self.params.get("sample_rows", DEV_SAMPLE_ROWS))
            if limit > 0:
                file_src_sample = ctx.register_scoped("__file_src_sample", rel)
                rel = ctx.conn.sql(f"SELECT * FROM {file_src_sample} LIMIT {limit}")

        return rel

    def _read_by_format(self, ctx: ExecutionContext, file_path: str,
                        fmt: str, encoding: str) -> duckdb.DuckDBPyRelation:
        """Dispatch to the right reader based on format."""
        if fmt in ("csv", "tsv"):
            delimiter = "\t" if fmt == "tsv" else self.params.get("delimiter", ",")
            null_str = self.params.get("null_string", "")
            quote_char = self.params.get("quote_char", '"')
            kwargs: dict[str, Any] = {
                "delimiter": delimiter,
                "header": self.params.get("header", True),
            }
            if null_str:
                kwargs["nullstr"] = null_str
            if quote_char and quote_char != '"':
                kwargs["quote"] = quote_char
            return ctx.conn.read_csv(file_path, **kwargs)
        if fmt == "json":
            return ctx.conn.read_json(file_path, format="array")
        if fmt == "ndjson":
            return ctx.conn.read_json(file_path, format="newline_delimited")
        if fmt == "parquet":
            return ctx.conn.read_parquet(file_path)
        if fmt == "excel":
            return self._read_excel(ctx.conn, file_path)
        if fmt == "xml":
            return self._read_xml(ctx.conn, file_path)
        if fmt == "orc":
            return self._read_orc(ctx.conn, file_path)
        if fmt == "avro":
            return self._read_avro(ctx.conn, file_path)
        raise ValueError(f"File: unsupported format '{fmt}'")

    def _read_orc(self, conn, file_path: str) -> "duckdb.DuckDBPyRelation":
        """Read an Apache ORC file via pyarrow → DuckDB relation.

        ORC is a columnar format common in Hadoop / Hive / Spark
        outputs. pyarrow ships native ORC support; we read the whole
        file into an Arrow Table and let DuckDB take it as a relation
        — same end-state as `read_parquet` so all downstream nodes
        see uniform DuckDB semantics.

        Windows note: pyarrow.orc's C++ backend requires an IANA tz
        database. Linux/Mac have one at `/usr/share/zoneinfo`; on
        Windows we point TZDIR at the `tzdata` PyPI package's bundled
        copy. Without this the first ORC read errors with
        "IANA time zone database is unavailable".

        Raises a clear, actionable error if pyarrow.orc / tzdata can't
        be loaded.
        """
        # Windows tz-database shim — set TZDIR before the first ORC
        # call. Idempotent: respects TZDIR if the operator already set
        # it (e.g. to a system zoneinfo path in a container).
        if not os.environ.get("TZDIR"):
            try:
                import tzdata  # type: ignore  # noqa: WPS433
                os.environ["TZDIR"] = os.path.join(
                    os.path.dirname(tzdata.__file__), "zoneinfo"
                )
            except ImportError:
                # tzdata is in requirements.txt; if it's missing the
                # operator skipped the install. Fall through and let
                # pyarrow's own error surface — the message below
                # tells them how to fix it.
                pass
        try:
            import pyarrow.orc as _orc  # noqa: WPS433
        except ImportError as exc:  # pragma: no cover - shipping pyarrow
            raise ValueError(
                "ORC reader: pyarrow.orc is not available. "
                "Install / upgrade pyarrow: `pip install -U pyarrow`."
            ) from exc
        try:
            table = _orc.read_table(file_path)
        except Exception as exc:
            msg = str(exc)
            if "time zone" in msg.lower() or "tzdir" in msg.lower():
                raise ValueError(
                    "ORC reader: IANA tz database is required by "
                    "pyarrow.orc but isn't available. Install the "
                    "`tzdata` package (`pip install tzdata`) or set "
                    "TZDIR to a system zoneinfo directory. Both are "
                    "listed in backend/requirements.txt."
                ) from exc
            raise
        return conn.from_arrow(table)

    def _read_avro(self, conn, file_path: str) -> "duckdb.DuckDBPyRelation":
        """Read an Apache Avro file via fastavro → Arrow → DuckDB, in bounded
        batches.

        Avro is row-oriented + schema-embedded. `fastavro.reader` yields records
        lazily; we accumulate them in fixed-size batches (`_AVRO_BATCH_ROWS`),
        convert each batch to a pyarrow Table, and append it into a DuckDB temp
        table. Peak Python-heap is therefore one batch — not the whole file —
        and the temp table is governed by the executor's DuckDB ``memory_limit``
        + spill-to-disk, so a multi-GB Avro file no longer materialises entirely
        in RAM (the previous ``list(reader)`` did, outside that cap).

        The Arrow schema is pinned from the first batch so an optional field that
        is all-null in a later batch doesn't drift its inferred type and break
        the append. (A first batch of 50k rows is a representative sample; the
        rare all-null-in-first-batch case is the one residual edge.)

        Raises a clear error pointing at the install fix if `fastavro`
        isn't on PATH.
        """
        try:
            import fastavro  # noqa: WPS433
        except ImportError as exc:
            raise ValueError(
                "Avro reader: `fastavro` package is not installed. "
                "Install it: `pip install fastavro>=1.9`. "
                "It's also listed in backend/requirements.txt for fresh installs."
            ) from exc
        try:
            import pyarrow as pa
        except ImportError as exc:  # pragma: no cover - pyarrow is core
            raise ValueError("pyarrow is required for Avro reading") from exc

        tmp = f"__avro_{next(_AVRO_TMP_SEQ)}"
        batch: list[dict] = []
        state = {"schema": None, "created": False}

        def _flush() -> None:
            if not batch:
                return
            if state["schema"] is None:
                table = pa.Table.from_pylist(batch)
                state["schema"] = table.schema
            else:
                table = pa.Table.from_pylist(batch, schema=state["schema"])
            conn.register("__avro_batch", table)
            try:
                if not state["created"]:
                    conn.execute(f'CREATE TEMP TABLE "{tmp}" AS SELECT * FROM __avro_batch')
                    state["created"] = True
                else:
                    conn.execute(f'INSERT INTO "{tmp}" SELECT * FROM __avro_batch')
            finally:
                conn.unregister("__avro_batch")
            batch.clear()

        with open(file_path, "rb") as f:
            for record in fastavro.reader(f):
                batch.append(record)
                if len(batch) >= _AVRO_BATCH_ROWS:
                    _flush()
            _flush()

        if not state["created"]:
            # Empty file — return a zero-row, zero-column relation so
            # downstream nodes get a valid (if empty) cursor.
            return conn.sql("SELECT * FROM (VALUES (NULL)) AS t(col) WHERE 1=0")

        return conn.sql(f'SELECT * FROM "{tmp}"')

    def _resolve_via_connection(self, ctx: ExecutionContext,
                                connection_id: str, path: str) -> str:
        """Resolve file path using a connection (S3, SFTP, local share, etc.)."""
        store = ctx.app_state.get("connection_store")
        if not store:
            return path

        conn_cfg = store.get(connection_id)
        if not conn_cfg:
            raise ValueError(f"File: connection '{connection_id}' not found")

        config = conn_cfg.get("config", conn_cfg)
        conn_type = conn_cfg.get("type", "")

        # S3-style: s3://bucket/prefix + path
        if conn_type in ("s3", "minio"):
            bucket = config.get("bucket", "")
            prefix = config.get("prefix", "").strip("/")
            endpoint = config.get("endpoint", "")
            key = config.get("access_key") or config.get("key", "")
            secret = config.get("secret_key") or config.get("secret", "")
            # Configure DuckDB S3 credentials
            if key and secret:
                ctx.conn.sql(f"SET s3_access_key_id='{key}'")
                ctx.conn.sql(f"SET s3_secret_access_key='{secret}'")
            if endpoint:
                ctx.conn.sql(f"SET s3_endpoint='{endpoint}'")
                ctx.conn.sql("SET s3_url_style='path'")
            s3_path = f"s3://{bucket}/{prefix}/{path}" if prefix else f"s3://{bucket}/{path}"
            return s3_path

        # Local/network share: base_path + path
        base_path = config.get("base_path") or config.get("root") or config.get("path", "")
        if base_path:
            return os.path.join(base_path, path)

        return path

    # ── Excel via openpyxl (delegated to ExcelSourceNode helper if available) ──
    def _read_excel(self, conn, file_path: str) -> duckdb.DuckDBPyRelation:
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            raise ValueError(
                "Excel format requires openpyxl: pip install openpyxl"
            )
        from fpulse.nodes.sources import ExcelSourceNode  # local import avoids cycle
        sheet = self.params.get("sheet_name", "")
        return ExcelSourceNode._read_with_openpyxl(
            conn, file_path, sheet,
            int(self.params.get("header_row", 1)),
            int(self.params.get("skip_rows", 0)),
        )

    # ── XML via ElementTree (delegated to XmlSourceNode if present) ──
    def _read_xml(self, conn, file_path: str) -> duckdb.DuckDBPyRelation:
        from fpulse.nodes.sources import XmlSourceNode
        node = XmlSourceNode({
            "file_path": file_path,
            "row_xpath": self.params.get("row_xpath", "//record"),
            "namespaces": self.params.get("namespaces", ""),
        })
        # XmlSourceNode expects an absolute path already-resolved
        return node.execute(type("X", (), {"conn": conn, "data_dir": ""})())

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "file_path": "",
            "connection_id": "",
            "format": "auto",
            "encoding": "utf-8",
            "delimiter": ",",
            "header": True,
            "null_string": "",
            "quote_char": '"',
            "sample_rows": DEV_SAMPLE_ROWS,
            "sheet_name": "",
            "header_row": 1,
            "skip_rows": 0,
            "row_xpath": "//record",
            "namespaces": "",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            # Connection tab
            {"name": "connection_id", "type": "connection_picker", "label": "Connection",
             "tab": "Source",
             "description": "Optional: S3 or local/network share. Leave empty for local files. (For SFTP/FTP, use the FTP / SFTP Source node.)"},
            {"name": "file_path", "type": "file", "label": "File Path", "required": True,
             "tab": "Source",
             "placeholder": "data/input.csv  (or .json, .parquet, .xlsx, .xml, .orc, .avro)",
             "description": "Relative to connection root (if set) or data directory. Glob patterns supported (*.csv)."},
            {"name": "format", "type": "select", "label": "Format Override",
             "options": ["auto", "csv", "tsv", "json", "ndjson", "parquet", "excel", "xml", "orc", "avro"],
             "default": "auto", "tab": "Source",
             "description": "Leave on 'auto' unless the extension is missing or wrong."},
            {"name": "encoding", "type": "select", "label": "Encoding",
             "options": ["utf-8", "utf-8-sig", "latin-1", "cp1252", "iso-8859-1", "ascii", "utf-16"],
             "default": "utf-8", "tab": "Source",
             "description": "File text encoding."},
            {"name": "sample_rows", "type": "number", "label": "Dev Sample Limit",
             "default": DEV_SAMPLE_ROWS, "tab": "Source",
             "description": "Max rows in dev mode (0 = no limit). Ignored in Full Run."},
            # CSV/TSV options
            {"name": "delimiter", "type": "select", "label": "Delimiter",
             "options": [",", ";", "\\t", "|"], "default": ",", "tab": "CSV Options",
             "show_when": {"format": ["auto", "csv", "tsv"]}},
            {"name": "header", "type": "boolean", "label": "Has Header Row", "default": True,
             "tab": "CSV Options",
             "show_when": {"format": ["auto", "csv", "tsv"]}},
            {"name": "null_string", "type": "text", "label": "NULL String",
             "placeholder": "NA, null, N/A", "tab": "CSV Options",
             "show_when": {"format": ["auto", "csv", "tsv"]},
             "description": "String values to interpret as NULL."},
            {"name": "quote_char", "type": "text", "label": "Quote Character",
             "default": '"', "tab": "CSV Options",
             "show_when": {"format": ["auto", "csv", "tsv"]}},
            # Excel options
            {"name": "sheet_name", "type": "text", "label": "Sheet Name",
             "placeholder": "Sheet1 — leave blank for first sheet",
             "tab": "Excel Options",
             "show_when": {"format": ["auto", "excel"]}},
            {"name": "header_row", "type": "number", "label": "Header Row", "default": 1,
             "tab": "Excel Options",
             "show_when": {"format": ["auto", "excel"]}},
            {"name": "skip_rows", "type": "number", "label": "Skip Rows", "default": 0,
             "tab": "Excel Options",
             "show_when": {"format": ["auto", "excel"]}},
            # XML options
            {"name": "row_xpath", "type": "text", "label": "Row XPath",
             "default": "//record", "tab": "XML Options",
             "show_when": {"format": ["auto", "xml"]}},
            {"name": "namespaces", "type": "text", "label": "Namespaces JSON",
             "placeholder": '{"ns": "http://example.com/ns"}',
             "tab": "XML Options",
             "show_when": {"format": ["auto", "xml"]}},
        ]


# ── Sink ──

@register(StepType.FILE_SINK)
class FileSinkNode(BaseNode):
    """Write any supported file format. Writer chosen by extension."""
    display_name = "File"
    category = "destination"
    description = "Write a file — format chosen automatically from the file extension"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        upstream = self.params.get("_input_step_ids") or []
        if not upstream:
            raise ValueError("File Sink: needs an upstream node")

        rel = ctx.get_input(upstream[0])
        if rel is None:
            raise ValueError(f"File Sink: upstream '{upstream[0]}' has no result")

        raw_path = self.params.get("file_path") or self.params.get("path") or ""
        file_path = _resolve(raw_path, ctx.data_dir)
        os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)

        fmt = _detect_format(file_path, self.params.get("format", "auto"))
        # Excel/XML get special treatment because DuckDB COPY TO doesn't ship a writer.
        if fmt == "csv":
            delim = self.params.get("delimiter", ",")
            header = "true" if self.params.get("header", True) else "false"
            ctx.conn.execute(
                f"COPY ({rel.sql_query()}) TO '{file_path}' "
                f"(FORMAT CSV, DELIMITER '{delim}', HEADER {header})"
            )
        elif fmt == "tsv":
            ctx.conn.execute(
                f"COPY ({rel.sql_query()}) TO '{file_path}' "
                f"(FORMAT CSV, DELIMITER E'\\t', HEADER true)"
            )
        elif fmt == "parquet":
            compression = self.params.get("compression", "snappy")
            ctx.conn.execute(
                f"COPY ({rel.sql_query()}) TO '{file_path}' "
                f"(FORMAT PARQUET, COMPRESSION '{compression}')"
            )
        elif fmt in ("json", "ndjson"):
            array_mode = "ARRAY true" if fmt == "json" else "ARRAY false"
            ctx.conn.execute(
                f"COPY ({rel.sql_query()}) TO '{file_path}' (FORMAT JSON, {array_mode})"
            )
        elif fmt == "excel":
            self._write_excel(rel, file_path)
        elif fmt == "xml":
            self._write_xml(rel, file_path)
        else:
            raise ValueError(f"File Sink: unsupported format '{fmt}'")

        # Pass-through so downstream nodes (audit, notify) can chain
        return rel

    def _write_excel(self, rel: duckdb.DuckDBPyRelation, file_path: str) -> None:
        try:
            import openpyxl
        except ImportError:
            raise ValueError("Excel sink requires openpyxl: pip install openpyxl")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = self.params.get("sheet_name") or "Sheet1"
        cols = rel.columns
        ws.append(cols)
        for row in rel.fetchall():
            ws.append(list(row))
        wb.save(file_path)

    def _write_xml(self, rel: duckdb.DuckDBPyRelation, file_path: str) -> None:
        import xml.etree.ElementTree as ET
        root_tag = self.params.get("root_tag", "data")
        row_tag = self.params.get("row_tag", "record")
        root = ET.Element(root_tag)
        cols = rel.columns
        for row in rel.fetchall():
            r = ET.SubElement(root, row_tag)
            for col, val in zip(cols, row):
                el = ET.SubElement(r, col)
                el.text = "" if val is None else str(val)
        ET.ElementTree(root).write(file_path, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "file_path": "",
            "format": "auto",
            "delimiter": ",",
            "header": True,
            "compression": "snappy",
            "sheet_name": "",
            "root_tag": "data",
            "row_tag": "record",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "file_path", "type": "text", "label": "Output File", "required": True,
             "placeholder": "out/result.parquet  (or .csv, .json, .xlsx, .xml)",
             "description": "Writer is chosen from the extension."},
            {"name": "format", "type": "select", "label": "Format Override",
             "options": ["auto", "csv", "tsv", "json", "ndjson", "parquet", "excel", "xml", "orc", "avro"],
             "default": "auto"},
            {"name": "delimiter", "type": "select", "label": "Delimiter (CSV only)",
             "options": [",", ";", "\\t", "|"], "default": ",",
             "show_when": {"format": ["auto", "csv"]}},
            {"name": "header", "type": "boolean", "label": "Write Header (CSV only)", "default": True,
             "show_when": {"format": ["auto", "csv", "tsv"]}},
            {"name": "compression", "type": "select", "label": "Compression (Parquet only)",
             "options": ["snappy", "zstd", "gzip", "uncompressed"], "default": "snappy",
             "show_when": {"format": ["auto", "parquet"]}},
            {"name": "sheet_name", "type": "text", "label": "Sheet Name (Excel only)",
             "default": "Sheet1", "show_when": {"format": ["auto", "excel"]}},
            {"name": "root_tag", "type": "text", "label": "Root Tag (XML only)",
             "default": "data", "show_when": {"format": ["auto", "xml"]}},
            {"name": "row_tag", "type": "text", "label": "Row Tag (XML only)",
             "default": "record", "show_when": {"format": ["auto", "xml"]}},
        ]
