"""Read helpers for the Storage API — preview rows and infer schema.

Both endpoints open an ephemeral DuckDB connection per request, read
the file using DuckDB's auto-detecting readers, fetch a bounded
sample, and close. This keeps preview cheap (no hold on the
executor's pool), schema-honest (DuckDB's type inference is what the
pipeline will see), and easy to test (no shared state).

Format dispatch:

    csv / tsv          → read_csv_auto
    json / ndjson      → read_json_auto, FALLBACK to JSON document tree
    parquet            → read_parquet
    excel              → openpyxl → pandas → DuckDB.from_df
    xml                → XmlSourceNode delegation

2026-05-23 (Y8): JSON document fallback.
DuckDB's ``read_json_auto`` only handles records-shaped JSON (top-level
array OR newline-delimited objects). Any other valid JSON (configs,
package.json, OpenAPI specs, F-Pulse pipeline exports) crashes with
"Malformed JSON". The fallback path here parses the file with
``json.loads`` and returns a ``kind="document"`` payload — the preview
drawer renders it as a JSON tree instead of a row table. We also
detect F-Pulse pipeline shape (steps array + name) so the drawer can
offer "Open in Editor" as a recovery action when a user accidentally
drops a workflow file in Storage.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


# Defensive caps. Preview shouldn't be able to OOM the server even if
# a user asks for "all rows" via a malicious request.
_MAX_PREVIEW_ROWS = 1000
_MAX_PREVIEW_BYTES = 5 * 1024 * 1024


def _open_relation(conn, abs_path: str, fmt: str):
    """Open the right DuckDB relation for the file format.

    Returns a ``duckdb.DuckDBPyRelation``. Callers wrap with LIMIT /
    OFFSET / DESCRIBE as needed.
    """
    # Normalize extension aliases to the canonical format names below.
    # Stored formats are sometimes the raw file extension ("xlsx", "pq")
    # rather than the canonical name ("excel", "parquet") — e.g. pipeline
    # outputs keep their extension, which made an .xlsx preview fail with
    # "unsupported format 'xlsx'" even though Excel reading is supported.
    fmt = (fmt or "").lower()
    fmt = {
        "xlsx": "excel", "xls": "excel", "xlsm": "excel",
        "pq": "parquet",
        "jsonl": "json", "ndjson": "json",
    }.get(fmt, fmt)
    if fmt in ("csv", "tsv"):
        delimiter = "\t" if fmt == "tsv" else ","
        return conn.read_csv(abs_path, delimiter=delimiter, header=True)
    if fmt == "json":
        # Try array first; fall back to NDJSON on parse failure.
        try:
            return conn.read_json(abs_path, format="array")
        except Exception:
            return conn.read_json(abs_path, format="newline_delimited")
    if fmt == "parquet":
        return conn.read_parquet(abs_path)
    if fmt == "excel":
        return _read_excel(conn, abs_path)
    if fmt == "xml":
        return _read_xml(conn, abs_path)
    raise ValueError(f"preview: unsupported format {fmt!r}")


def _read_excel(conn, abs_path: str):
    """Excel preview via openpyxl + pandas → DuckDB."""
    try:
        import openpyxl  # noqa: F401
        import pandas as pd
    except ImportError:
        raise ValueError("Excel preview requires openpyxl + pandas (pip install openpyxl pandas)")
    df = pd.read_excel(abs_path, sheet_name=0, header=0)
    return conn.from_df(df)


def _read_xml(conn, abs_path: str):
    """XML preview via the existing XmlSourceNode."""
    from fpulse.nodes.sources import XmlSourceNode
    node = XmlSourceNode({
        "file_path": abs_path,
        "row_xpath": "//record",
        "namespaces": "",
    })
    return node.execute(type("X", (), {"conn": conn, "data_dir": ""})())


def preview_file(
    abs_path: str,
    fmt: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Return ``{kind, ...}`` for a file.

    Two shapes:
      * ``kind="rows"``     — DuckDB parsed the file as tabular records.
        Returns ``{columns, rows, row_count, limit, offset, format}``.
      * ``kind="document"`` — JSON file that isn't records-shaped, OR
        the records reader threw. Returns
        ``{document, format, document_kind, is_pipeline_definition}``
        so the drawer can render a tree view + (when relevant)
        offer "Open in Editor" for F-Pulse pipeline JSONs.

    Total row count is included on the rows path when cheap (Parquet
    stores it in the footer; CSV would need a full scan, so omitted).
    """
    import duckdb

    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"preview: file not found: {abs_path}")

    limit = max(1, min(int(limit or 100), _MAX_PREVIEW_ROWS))
    offset = max(0, int(offset or 0))

    # JSON gets a defensive parse path. If the file isn't records-shaped
    # (top-level object instead of array), DuckDB raises "Malformed JSON"
    # — but the file is perfectly valid JSON, just not tabular. Detect
    # before DuckDB sees it so the drawer can render a tree instead of
    # a misleading error.
    if fmt == "json":
        peek = _peek_json_shape(abs_path)
        if peek == "object":
            # Object root → render as document, skip DuckDB entirely.
            return _json_document_preview(abs_path, fmt)

    conn = duckdb.connect()
    try:
        try:
            rel = _open_relation(conn, abs_path, fmt)
            sample = rel.limit(limit, offset)
            columns = [
                {"name": c, "type": str(t)}
                for c, t in zip(sample.columns, sample.types)
            ]
            rows = [
                {col: _json_safe(v) for col, v in zip(sample.columns, row)}
                for row in sample.fetchall()
            ]
            total = _maybe_count_rows(conn, abs_path, fmt)
            return {
                "kind": "rows",
                "columns": columns,
                "rows": rows,
                "row_count": total,
                "limit": limit,
                "offset": offset,
                "format": fmt,
            }
        except Exception as exc:
            # DuckDB couldn't parse it as records. For JSON we fall back
            # to a document preview; for everything else we re-raise
            # since the error is meaningful (corrupt CSV / bad Parquet
            # footer / etc).
            if fmt != "json":
                raise
            logger.info("preview: records path failed for %s (%s); falling back to document",
                        abs_path, exc)
            return _json_document_preview(abs_path, fmt)
    finally:
        conn.close()


def infer_schema(abs_path: str, fmt: str) -> list[dict[str, Any]]:
    """Return per-column schema (name + type + nullable + sample).

    On a non-records JSON the column list is empty — the document
    structure is exposed by the preview endpoint, not the schema one.
    """
    import duckdb

    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"schema: file not found: {abs_path}")

    if fmt == "json" and _peek_json_shape(abs_path) == "object":
        return []

    conn = duckdb.connect()
    try:
        rel = _open_relation(conn, abs_path, fmt)
        sample = rel.limit(1)
        sample_row = sample.fetchone()
        out: list[dict[str, Any]] = []
        for idx, (col, typ) in enumerate(zip(rel.columns, rel.types)):
            sample_val = (
                _json_safe(sample_row[idx]) if sample_row is not None else None
            )
            out.append({
                "name": col,
                "type": str(typ),
                "nullable": True,  # DuckDB infer treats everything as nullable
                "ordinal": idx,
                "sample": sample_val,
            })
        return out
    except Exception:
        if fmt == "json":
            # Same fallback as preview — non-records JSON has no columns.
            return []
        raise
    finally:
        conn.close()


def _peek_json_shape(abs_path: str) -> str:
    """Return ``'array'`` | ``'object'`` | ``'unknown'`` by sniffing the
    first non-whitespace byte. Cheap O(1) check that lets us route a
    pipeline JSON (object root) away from DuckDB before it errors.
    """
    try:
        with open(abs_path, "rb") as f:
            chunk = f.read(64)
        for byte in chunk:
            ch = chr(byte)
            if ch in (" ", "\t", "\n", "\r"):
                continue
            if ch == "[":
                return "array"
            if ch == "{":
                return "object"
            # First non-space char is a letter / digit → NDJSON or raw value;
            # let DuckDB take the records path.
            return "unknown"
        return "unknown"
    except OSError:
        return "unknown"


def _json_document_preview(abs_path: str, fmt: str) -> dict[str, Any]:
    """Parse a non-records JSON file and return a document-style preview.

    Caps the read at ``_MAX_PREVIEW_BYTES`` because a 50MB OpenAPI spec
    deserves a "too large to preview" message, not a 500. The returned
    document is recursively truncated so the wire payload stays small
    even on deep / wide objects.
    """
    size = os.path.getsize(abs_path)
    if size > _MAX_PREVIEW_BYTES:
        return {
            "kind": "document",
            "format": fmt,
            "document": None,
            "document_kind": "too_large",
            "size_bytes": size,
            "is_pipeline_definition": False,
            "message": (
                f"File is {size:,} bytes — too large to preview as a "
                f"JSON document. Use a tool like `jq` or open it locally."
            ),
        }

    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except json.JSONDecodeError as exc:
        return {
            "kind": "document",
            "format": fmt,
            "document": None,
            "document_kind": "invalid",
            "size_bytes": size,
            "is_pipeline_definition": False,
            "message": f"Invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})",
        }
    except OSError as exc:
        raise RuntimeError(f"could not read {abs_path}: {exc}")

    return {
        "kind": "document",
        "format": fmt,
        "document": _truncate_for_wire(doc),
        "document_kind": "object" if isinstance(doc, dict) else "array",
        "size_bytes": size,
        # Pipeline detection — see _looks_like_pipeline for the heuristic.
        "is_pipeline_definition": _looks_like_pipeline(doc),
    }


def _looks_like_pipeline(doc: Any) -> bool:
    """Heuristic for "this is an F-Pulse pipeline export, not a data file".

    Triggered when the document is an object with:
      * ``steps`` — a non-empty list of dicts, each with ``type`` and ``params``
      * AND at least one of ``name`` (string) / ``connection_definitions`` /
        ``connections`` (list)

    The combination is narrow enough that data files (configs,
    package.json, OpenAPI specs) won't match, but loose enough to catch
    both the editor-exported shape and the sample-pipeline shape.
    """
    if not isinstance(doc, dict):
        return False
    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        return False
    if not all(
        isinstance(s, dict) and "type" in s and "params" in s
        for s in steps
    ):
        return False
    has_name = isinstance(doc.get("name"), str)
    has_conn_defs = isinstance(
        doc.get("connection_definitions") or doc.get("connections"), list,
    )
    return has_name or has_conn_defs


# Wire-size caps for the document tree. Keep modest so a 5MB
# OpenAPI spec doesn't bloat the preview payload.
_MAX_DOC_DEPTH = 6
_MAX_DOC_LIST_ITEMS = 50
_MAX_DOC_OBJECT_KEYS = 100
_MAX_DOC_STRING = 500


def _truncate_for_wire(value: Any, depth: int = 0) -> Any:
    """Recursively bound depth, list length, and string length so the
    JSON tree we send to the browser stays bounded."""
    if depth >= _MAX_DOC_DEPTH:
        if isinstance(value, dict):
            return {"…": f"<truncated, {len(value)} keys at depth>"}
        if isinstance(value, list):
            return [f"<truncated, {len(value)} items at depth>"]
        return value
    if isinstance(value, dict):
        keys = list(value.keys())
        out: dict[str, Any] = {}
        for k in keys[:_MAX_DOC_OBJECT_KEYS]:
            out[str(k)] = _truncate_for_wire(value[k], depth + 1)
        if len(keys) > _MAX_DOC_OBJECT_KEYS:
            out["…"] = f"<{len(keys) - _MAX_DOC_OBJECT_KEYS} more keys>"
        return out
    if isinstance(value, list):
        truncated = [
            _truncate_for_wire(v, depth + 1)
            for v in value[:_MAX_DOC_LIST_ITEMS]
        ]
        if len(value) > _MAX_DOC_LIST_ITEMS:
            truncated.append(f"<{len(value) - _MAX_DOC_LIST_ITEMS} more items>")
        return truncated
    if isinstance(value, str) and len(value) > _MAX_DOC_STRING:
        return value[:_MAX_DOC_STRING] + "…"
    return _json_safe(value)


def _maybe_count_rows(conn, abs_path: str, fmt: str) -> int | None:
    """Return the row count when DuckDB can compute it cheaply.

    Parquet stores the row count in its footer so the count is O(1).
    For other formats a count requires a full scan — too expensive
    for an interactive preview, so we return None.
    """
    if fmt != "parquet":
        return None
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM read_parquet(?)",
            [abs_path],
        ).fetchone()[0]
    except Exception:
        return None


def compute_file_stats(abs_path: str, fmt: str) -> tuple[int | None, int | None]:
    """One-shot row + column count for an uploaded file.

    Unlike ``_maybe_count_rows`` (which guards against the cost of a
    full scan inside the interactive preview), this is meant to run
    once at upload time so the StorageObject row has populated counts
    immediately — the Files table can show real numbers without the
    user having to click Preview first.

    Cost characterisation:
      * Parquet — O(1) (footer read).
      * CSV / JSON / Excel — O(N) full scan, but the file is hot on
        disk and DuckDB is fast (millions of rows/sec); acceptable
        for a one-time upload-side operation, even on the small
        single-node OSS host.

    Returns ``(row_count, column_count)``. Either side may be ``None``
    on a parse failure — caller persists the partial result rather
    than failing the upload (a file the user just put on the canvas
    must always be saved; counts are nice-to-have, not load-bearing).
    """
    import duckdb
    if not os.path.isfile(abs_path):
        return None, None
    fmt = (fmt or "").lower()
    conn = duckdb.connect()
    try:
        try:
            rel = _open_relation(conn, abs_path, fmt)
        except Exception:
            return None, None
        # Column count is always cheap — read straight from the relation
        # without forcing a full materialisation. ``rel.columns`` is the
        # list of column names DuckDB inferred from the source.
        col_count: int | None = None
        try:
            col_count = len(rel.columns)
        except Exception:
            col_count = None
        # Row count: footer-fast for Parquet, full-scan for everything
        # else. Best-effort; on a parse failure we return whatever we
        # have so the upload still records a partial result.
        row_count: int | None = None
        try:
            if fmt == "parquet":
                row_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM read_parquet(?)",
                    [abs_path],
                ).fetchone()[0]
            else:
                # Use the already-opened relation so the same reader
                # settings (delimiter detection, encoding, etc.) apply.
                row_count = rel.count("*").fetchone()[0]
        except Exception:
            row_count = None
        return row_count, col_count
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _json_safe(v):
    """Coerce DuckDB row values into JSON-serialisable form.

    Pandas / DuckDB return numpy scalars, datetimes, decimals — all of
    which trip the default JSON encoder. Cast to native Python where
    we can; fall back to str() for the rest. Bytes get base64'd so
    blob columns don't break the preview.
    """
    import base64
    import datetime as _dt
    import decimal

    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return base64.b64encode(bytes(v)).decode("ascii")
    if isinstance(v, list):
        return [_json_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    return str(v)


__all__ = ["infer_schema", "preview_file"]
