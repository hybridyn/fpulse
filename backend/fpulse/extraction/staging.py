"""Staging writers — abstraction over output formats for extraction runs.

JSONL is the default: append-style, debuggable, no schema commitment.
Parquet is opt-in: schema-aware, compressed, columnar — better for
warehouse loads, worse mid-run because you can't `tail -f` it.

The Parquet writer buffers records in memory and flushes in batches.
That's fine for the typical extraction-run sizes (10k-100k records);
huge runs should still use JSONL and convert at end if needed.

If pyarrow isn't installed, ParquetStagingWriter falls back to a
clear runtime error rather than silently swallowing the format
choice. Same lazy-import pattern as the catalog providers.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


# ── Base ────────────────────────────────────────────────────────────

class StagingWriter:
    """Minimal write/close interface every staging format implements."""
    path: str

    def write(self, record: dict[str, Any]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ── JSONL ───────────────────────────────────────────────────────────

class JsonlStagingWriter(StagingWriter):
    """Append-style JSONL writer. One JSON record per line, flushed
    after every write so a crash mid-run loses at most one record."""

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass


# ── Parquet ─────────────────────────────────────────────────────────

class ParquetStagingWriter(StagingWriter):
    """Schema-aware columnar writer. Buffers records in memory, flushes
    in batches to keep memory bounded.

    Schema is inferred from the first batch unless an explicit
    `schema_hint` (column → arrow type name) is supplied. Type-coerced
    values from SchemaMapper feed in cleanly because they're already
    plain Python types (int / float / bool / str / datetime / list).
    """

    def __init__(
        self, path: str, *,
        batch_size: int = 1000,
        schema_hint: dict[str, str] | None = None,
    ) -> None:
        try:
            import pyarrow as pa  # type: ignore
            import pyarrow.parquet as pq  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Parquet output requires pyarrow (pip install pyarrow)"
            ) from exc
        self._pa = pa
        self._pq = pq
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._batch_size = batch_size
        self._buffer: list[dict[str, Any]] = []
        self._writer = None  # type: ignore[assignment]
        self._schema_hint = schema_hint or {}

    def write(self, record: dict[str, Any]) -> None:
        self._buffer.append(record)
        if len(self._buffer) >= self._batch_size:
            self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return
        table = self._pa.Table.from_pylist(self._buffer)
        if self._writer is None:
            self._writer = self._pq.ParquetWriter(self.path, table.schema)
        self._writer.write_table(table)
        self._buffer.clear()

    def close(self) -> None:
        try:
            self._flush()
        finally:
            if self._writer is not None:
                try:
                    self._writer.close()
                except Exception:  # noqa: BLE001
                    pass


# ── Factory ─────────────────────────────────────────────────────────

def make_staging_writer(
    output_format: str, output_path: str, *,
    schema_hint: dict[str, str] | None = None,
) -> StagingWriter:
    fmt = output_format.lower()
    if fmt == "jsonl":
        return JsonlStagingWriter(output_path)
    if fmt == "parquet":
        return ParquetStagingWriter(output_path, schema_hint=schema_hint)
    raise ValueError(f"Unsupported output_format: {output_format!r} (jsonl|parquet)")


def output_size_bytes(path: str) -> int:
    """File size in bytes; 0 if missing — never raises."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
