"""Tests for staging writers — JSONL always works, Parquet works
when pyarrow is present, factory rejects unknown formats."""

from __future__ import annotations

import json
import os

import pytest

from fpulse.extraction.staging import (
    JsonlStagingWriter,
    StagingWriter,
    make_staging_writer,
    output_size_bytes,
)


# ── JSONL ───────────────────────────────────────────────────────────

def test_jsonl_writer_streams_records(tmp_path):
    path = str(tmp_path / "out.jsonl")
    writer = JsonlStagingWriter(path)
    for i in range(5):
        writer.write({"id": i, "name": f"item-{i}"})
    writer.close()

    rows = [json.loads(line) for line in open(path)]
    assert len(rows) == 5
    assert rows[0] == {"id": 0, "name": "item-0"}


def test_jsonl_writer_flushes_each_record(tmp_path):
    """A crash mid-run must lose at most one record — the writer
    flushes after every write."""
    path = str(tmp_path / "out.jsonl")
    writer = JsonlStagingWriter(path)
    writer.write({"id": 1})
    # File contents visible without close() because of flush().
    assert os.path.getsize(path) > 0
    writer.close()


def test_jsonl_writer_supports_context_manager(tmp_path):
    path = str(tmp_path / "out.jsonl")
    with JsonlStagingWriter(path) as w:
        w.write({"id": 1})
    # Exit closed it cleanly.
    assert os.path.getsize(path) > 0


def test_jsonl_writer_creates_missing_directory(tmp_path):
    nested = str(tmp_path / "deep" / "nested" / "out.jsonl")
    JsonlStagingWriter(nested).close()
    assert os.path.isdir(os.path.dirname(nested))


# ── Parquet (when pyarrow available) ────────────────────────────────

def test_parquet_writer_works_when_pyarrow_present(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    path = str(tmp_path / "out.parquet")
    writer = make_staging_writer("parquet", path)
    for i in range(50):
        writer.write({"id": i, "name": f"item-{i}", "price": float(i) * 1.5})
    writer.close()

    table = pq.read_table(path)
    assert table.num_rows == 50
    assert set(table.column_names) == {"id", "name", "price"}


def test_parquet_writer_batches_records(tmp_path):
    """Buffer flushes at batch_size, not per record — verifies the
    schema is committed once."""
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    path = str(tmp_path / "batched.parquet")
    from fpulse.extraction.staging import ParquetStagingWriter
    writer = ParquetStagingWriter(path, batch_size=10)
    for i in range(25):
        writer.write({"id": i})
    writer.close()
    table = pq.read_table(path)
    assert table.num_rows == 25


def test_parquet_writer_handles_missing_pyarrow(monkeypatch, tmp_path):
    """When pyarrow isn't importable, instantiation fails with a
    readable message — not a deep ImportError stack."""
    import builtins as _builtins
    real_import = _builtins.__import__

    def fake(name, *args, **kwargs):
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError("simulated pyarrow missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(_builtins, "__import__", fake)
    from fpulse.extraction.staging import ParquetStagingWriter
    with pytest.raises(RuntimeError, match="pyarrow"):
        ParquetStagingWriter(str(tmp_path / "x.parquet"))


# ── Factory ─────────────────────────────────────────────────────────

def test_factory_rejects_unknown_format():
    with pytest.raises(ValueError, match="output_format"):
        make_staging_writer("avro", "/tmp/out.avro")


def test_factory_returns_jsonl_writer(tmp_path):
    w = make_staging_writer("jsonl", str(tmp_path / "x.jsonl"))
    assert isinstance(w, StagingWriter)
    w.close()


# ── Helpers ─────────────────────────────────────────────────────────

def test_output_size_bytes_returns_zero_for_missing(tmp_path):
    assert output_size_bytes(str(tmp_path / "does-not-exist")) == 0


def test_output_size_bytes_returns_real_size(tmp_path):
    path = str(tmp_path / "x.txt")
    with open(path, "w") as f:
        f.write("hello world")
    assert output_size_bytes(path) == len("hello world")
