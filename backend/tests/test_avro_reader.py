"""Avro reader is bounded-batch streaming, not whole-file `list(reader)`.

Proves the multi-batch append path produces the same rows as a single read
(no records lost or duplicated across batch boundaries) and that an empty file
yields an empty relation. See file_node.FileSourceNode._read_avro.
"""
import duckdb
import fastavro
import pytest

from fpulse.nodes import file_node
from fpulse.nodes.file_node import FileSourceNode

_SCHEMA = fastavro.parse_schema({
    "type": "record",
    "name": "Rec",
    "fields": [
        {"name": "id", "type": "int"},
        {"name": "name", "type": ["null", "string"]},
    ],
})


def _write_avro(path, records):
    with open(path, "wb") as f:
        fastavro.writer(f, _SCHEMA, records)
    return str(path)


def test_avro_multibatch_reads_every_row(tmp_path, monkeypatch):
    # Force several flushes: 7 records at batch size 3 → batches of 3, 3, 1.
    monkeypatch.setattr(file_node, "_AVRO_BATCH_ROWS", 3)
    records = [{"id": i, "name": f"n{i}"} for i in range(7)]
    path = _write_avro(tmp_path / "data.avro", records)

    conn = duckdb.connect(":memory:")
    rel = FileSourceNode({})._read_avro(conn, path)
    rows = sorted(rel.fetchall())

    assert len(rows) == 7  # nothing dropped or duplicated at batch boundaries
    assert [r[0] for r in rows] == list(range(7))
    assert [r[1] for r in rows] == [f"n{i}" for i in range(7)]


def test_avro_single_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(file_node, "_AVRO_BATCH_ROWS", 50_000)
    path = _write_avro(tmp_path / "one.avro", [{"id": 1, "name": "a"}])
    conn = duckdb.connect(":memory:")
    rel = FileSourceNode({})._read_avro(conn, path)
    assert rel.fetchall() == [(1, "a")]


def test_avro_empty_file_yields_empty_relation(tmp_path):
    path = _write_avro(tmp_path / "empty.avro", [])
    conn = duckdb.connect(":memory:")
    rel = FileSourceNode({})._read_avro(conn, path)
    assert rel.fetchall() == []


def test_avro_concurrent_reads_dont_collide(tmp_path):
    # Two reads on the same connection must use distinct temp tables.
    p1 = _write_avro(tmp_path / "a.avro", [{"id": 1, "name": "a"}])
    p2 = _write_avro(tmp_path / "b.avro", [{"id": 2, "name": "b"}])
    conn = duckdb.connect(":memory:")
    node = FileSourceNode({})
    r1 = node._read_avro(conn, p1)
    r2 = node._read_avro(conn, p2)
    # Materialise r1 AFTER r2 exists — proves r2 didn't clobber r1's table.
    assert r2.fetchall() == [(2, "b")]
    assert r1.fetchall() == [(1, "a")]
