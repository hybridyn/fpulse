"""Sprint B — pgoutput decoder unit tests.

These tests exercise the decoder against synthetic byte streams that
match the PostgreSQL `pgoutput` wire format. No live Postgres needed —
the docker-compose-based integration test (`tests/integration/test_cdc_postgres.py`)
covers end-to-end semantics with a real replication slot.

The synthetic bytes here mirror what `psycopg2.extras.ReplicationCursor`
delivers as `msg.payload` for each message type.
"""

from __future__ import annotations

import struct

import pytest

from fpulse.connectors.pgoutput import (
    PgoutputDecoder,
    ensure_cdc_offsets_table,
    load_cdc_offset,
    save_cdc_offset,
)


# ── Byte builders ────────────────────────────────────────────────────


def _i32(v: int) -> bytes:
    return struct.pack(">i", v)


def _i16(v: int) -> bytes:
    return struct.pack(">h", v)


def _i64(v: int) -> bytes:
    return struct.pack(">q", v)


def _cstr(s: str) -> bytes:
    return s.encode("utf-8") + b"\x00"


def _begin(final_lsn: int = 1234, ts: int = 0, xid: int = 99) -> bytes:
    return b"B" + _i64(final_lsn) + _i64(ts) + _i32(xid)


def _commit(commit_lsn: int = 1234, end_lsn: int = 1235, ts: int = 0) -> bytes:
    return b"C" + bytes([0]) + _i64(commit_lsn) + _i64(end_lsn) + _i64(ts)


def _relation(
    relation_id: int,
    namespace: str,
    name: str,
    columns: list[tuple[str, int, int, bool]],
    replica_identity: bytes = b"d",
) -> bytes:
    """Each column tuple is (name, type_oid, type_modifier, is_key)."""
    out = b"R" + _i32(relation_id) + _cstr(namespace) + _cstr(name)
    out += replica_identity
    out += _i16(len(columns))
    for cname, type_oid, type_mod, is_key in columns:
        out += bytes([1 if is_key else 0]) + _cstr(cname) + _i32(type_oid) + _i32(type_mod)
    return out


def _tuple_data(values: list) -> bytes:
    """`values` items: str → text-tag, None → null-tag, b'...' → binary."""
    out = _i16(len(values))
    for v in values:
        if v is None:
            out += b"n"
        elif isinstance(v, str):
            data = v.encode("utf-8")
            out += b"t" + _i32(len(data)) + data
        elif isinstance(v, bytes):
            out += b"b" + _i32(len(v)) + v
        else:
            raise ValueError(f"unhandled test value type: {type(v)}")
    return out


def _insert(relation_id: int, values: list) -> bytes:
    return b"I" + _i32(relation_id) + b"N" + _tuple_data(values)


def _update(relation_id: int, after: list, before: list | None = None) -> bytes:
    out = b"U" + _i32(relation_id)
    if before is not None:
        out += b"K" + _tuple_data(before)
    out += b"N" + _tuple_data(after)
    return out


def _delete(relation_id: int, before: list) -> bytes:
    return b"D" + _i32(relation_id) + b"K" + _tuple_data(before)


# ── Decoder tests ────────────────────────────────────────────────────


class TestBeginCommit:
    def test_begin_decodes(self):
        decoder = PgoutputDecoder()
        ev = decoder.decode(_begin(final_lsn=42, xid=99), lsn=42)
        assert ev["op"] == "B"
        assert ev["final_lsn"] == 42
        assert ev["xid"] == 99

    def test_commit_decodes(self):
        decoder = PgoutputDecoder()
        ev = decoder.decode(_commit(commit_lsn=42, end_lsn=43), lsn=43)
        assert ev["op"] == "C"
        assert ev["commit_lsn"] == 42
        assert ev["end_lsn"] == 43


class TestRelationCaching:
    def test_relation_message_caches_schema(self):
        decoder = PgoutputDecoder()
        ev = decoder.decode(
            _relation(101, "public", "users", [
                ("id", 23, -1, True),
                ("email", 25, -1, False),
            ]),
            lsn=10,
        )
        assert ev["op"] == "R"
        assert ev["name"] == "users"
        assert ev["schema_version"] == 1
        assert [c["name"] for c in ev["columns"]] == ["id", "email"]
        # cached
        assert 101 in decoder.known_relations()

    def test_relation_redelivery_does_not_bump_schema_version(self):
        decoder = PgoutputDecoder()
        cols = [("id", 23, -1, True), ("email", 25, -1, False)]
        decoder.decode(_relation(101, "public", "users", cols))
        ev = decoder.decode(_relation(101, "public", "users", cols))
        assert ev["schema_version"] == 1  # unchanged

    def test_schema_drift_bumps_version(self):
        decoder = PgoutputDecoder()
        decoder.decode(_relation(101, "public", "users", [
            ("id", 23, -1, True),
        ]))
        # Add a column → drift.
        ev = decoder.decode(_relation(101, "public", "users", [
            ("id", 23, -1, True),
            ("email", 25, -1, False),
        ]))
        assert ev["schema_version"] == 2

        # Drop a column → drift again.
        ev = decoder.decode(_relation(101, "public", "users", [
            ("id", 23, -1, True),
        ]))
        assert ev["schema_version"] == 3


class TestRowEvents:
    @pytest.fixture
    def primed(self) -> PgoutputDecoder:
        decoder = PgoutputDecoder()
        decoder.decode(_relation(101, "public", "users", [
            ("id", 23, -1, True),
            ("email", 25, -1, False),
            ("active", 16, -1, False),
        ]))
        return decoder

    def test_insert_decodes_with_column_names(self, primed):
        ev = primed.decode(_insert(101, ["1", "alice@test.com", "t"]))
        assert ev["op"] == "I"
        assert ev["before"] is None
        assert ev["after"] == {"id": "1", "email": "alice@test.com", "active": "t"}
        assert ev["name"] == "users"
        assert ev["schema_version"] == 1

    def test_update_with_before_image(self, primed):
        ev = primed.decode(_update(101,
            after=["1", "alice@new.com", "t"],
            before=["1", "alice@old.com", "t"],
        ))
        assert ev["op"] == "U"
        assert ev["before"]["email"] == "alice@old.com"
        assert ev["after"]["email"] == "alice@new.com"

    def test_delete_carries_before_image(self, primed):
        ev = primed.decode(_delete(101, ["1", "alice@test.com", "t"]))
        assert ev["op"] == "D"
        assert ev["after"] is None
        assert ev["before"] == {"id": "1", "email": "alice@test.com", "active": "t"}

    def test_null_value_decodes_as_none(self, primed):
        ev = primed.decode(_insert(101, ["1", None, "t"]))
        assert ev["after"]["email"] is None

    def test_insert_without_relation_emits_warning(self):
        """Defensive: stream out of order."""
        decoder = PgoutputDecoder()
        ev = decoder.decode(_insert(999, ["1"]))
        assert ev["op"] == "I"
        assert ev.get("_warning")
        assert ev["name"] is None


class TestUnsupportedMessage:
    def test_unknown_message_returns_none(self):
        decoder = PgoutputDecoder()
        # 'O' = Origin, not handled.
        assert decoder.decode(b"O" + _i64(0) + b"\x00", lsn=1) is None


# ── LSN persistence tests ────────────────────────────────────────────


class TestCdcOffsets:
    @pytest.fixture
    def conn(self):
        import sqlite3
        c = sqlite3.connect(":memory:")
        ensure_cdc_offsets_table(c)
        yield c
        c.close()

    def test_table_created(self, conn):
        # Re-running ensure is a no-op.
        ensure_cdc_offsets_table(conn)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cdc_offsets'"
        ).fetchall()
        assert rows

    def test_save_then_load_roundtrip(self, conn):
        save_cdc_offset(
            conn,
            workflow_id="wf1",
            slot_name="fpulse_slot",
            last_lsn=12345,
            publication="fpulse_pub",
            event_count=100,
        )
        row = load_cdc_offset(conn, "wf1", "fpulse_slot")
        assert row is not None
        assert row["last_lsn"] == 12345
        assert row["publication"] == "fpulse_pub"
        assert row["event_count"] == 100

    def test_save_is_idempotent_upsert(self, conn):
        save_cdc_offset(conn, workflow_id="wf1", slot_name="s", last_lsn=10)
        save_cdc_offset(conn, workflow_id="wf1", slot_name="s", last_lsn=20)
        row = load_cdc_offset(conn, "wf1", "s")
        assert row["last_lsn"] == 20

    def test_load_returns_none_when_missing(self, conn):
        assert load_cdc_offset(conn, "missing", "missing") is None
