"""Sprint B integration test — requires a live Postgres with wal_level=logical.

Spin it up:
    docker compose -f docker-compose.test.yml up -d
    pytest backend/tests/integration/test_cdc_postgres.py -v
    docker compose -f docker-compose.test.yml down -v

The test intentionally has NO dependency on F-Pulse's storage layer or
the backend HTTP API — it goes straight from psycopg2 logical-replication
events into the pgoutput decoder, then asserts the row events are
correct. That keeps the surface area small and the failure modes obvious.

Skipped automatically when the test Postgres isn't reachable, so this
file is safe to leave on the default `pytest` invocation.

Exit gate (Sprint B): a schema change (ALTER TABLE ADD COLUMN) lands
in the stream, the next Insert reflects the new column, and
schema_version increments. ~30s end-to-end on a laptop.
"""

from __future__ import annotations

import os
import time

import pytest

PG_HOST = os.environ.get("FPULSE_TEST_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("FPULSE_TEST_PG_PORT", "5433"))
PG_USER = os.environ.get("FPULSE_TEST_PG_USER", "fpulse_test")
PG_PASS = os.environ.get("FPULSE_TEST_PG_PASS", "fpulse_test")
PG_DB = os.environ.get("FPULSE_TEST_PG_DB", "fpulse_test")
PG_SLOT = "fpulse_test_slot"
PG_PUB = "fpulse_test_pub"

psycopg2 = pytest.importorskip("psycopg2")
# `psycopg2.extras` is a separate submodule — importorskip on the base
# module doesn't pull it in, so the LogicalReplicationConnection lookup
# below would AttributeError at collection time. Import it explicitly.
psycopg2_extras = pytest.importorskip("psycopg2.extras")
LogicalReplicationConnection = psycopg2_extras.LogicalReplicationConnection  # type: ignore


def _can_connect() -> bool:
    try:
        c = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, dbname=PG_DB,
            user=PG_USER, password=PG_PASS, connect_timeout=2,
        )
        c.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _can_connect(),
    reason=(
        "Test Postgres not reachable on "
        f"{PG_HOST}:{PG_PORT}. Start it with "
        "`docker compose -f docker-compose.test.yml up -d` "
        "or set FPULSE_TEST_PG_* env vars."
    ),
)


def _connect():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASS,
    )


def _cleanup_slot(conn, slot: str) -> None:
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT pg_drop_replication_slot(slot_name) "
            "FROM pg_replication_slots WHERE slot_name = %s",
            (slot,),
        )
        conn.commit()
    finally:
        cur.close()


def _setup_publication_and_table(conn) -> None:
    cur = conn.cursor()
    try:
        cur.execute(f"DROP PUBLICATION IF EXISTS {PG_PUB}")
        cur.execute("DROP TABLE IF EXISTS users")
        cur.execute("""
            CREATE TABLE users (
                id    INTEGER PRIMARY KEY,
                email TEXT NOT NULL
            )
        """)
        cur.execute(f"CREATE PUBLICATION {PG_PUB} FOR TABLE users")
        conn.commit()
    finally:
        cur.close()


def _create_slot_if_missing() -> None:
    """Create the replication slot up front. Slots only capture WAL
    produced AFTER they exist, so a slot created mid-test misses any
    INSERT/ALTER that happened before it. The fixture must guarantee
    the slot is in place before any test mutates the table."""
    repl = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASS,
        connection_factory=LogicalReplicationConnection,
    )
    try:
        cur = repl.cursor()
        try:
            cur.create_replication_slot(PG_SLOT, output_plugin="pgoutput")
        except psycopg2.errors.DuplicateObject:
            pass
        finally:
            cur.close()
    finally:
        repl.close()


@pytest.fixture(scope="module")
def pg_setup():
    conn = _connect()
    _cleanup_slot(conn, PG_SLOT)
    _setup_publication_and_table(conn)
    _create_slot_if_missing()  # <-- slot before any DML
    yield conn
    _cleanup_slot(conn, PG_SLOT)
    cur = conn.cursor()
    cur.execute(f"DROP PUBLICATION IF EXISTS {PG_PUB}")
    cur.execute("DROP TABLE IF EXISTS users")
    conn.commit()
    cur.close()
    conn.close()


def _consume_events(replication_conn, max_events: int, timeout_s: float = 10.0) -> list[bytes]:
    """Drain up to `max_events` raw payloads from the slot. Bounded by
    timeout via non-blocking read_message() — `consume_stream()` is
    blocking and doesn't honor outer timeouts cleanly, so we don't use
    it here."""
    import select

    cur = replication_conn.cursor()
    try:
        cur.start_replication(
            slot_name=PG_SLOT, decode=False,
            options={"publication_names": PG_PUB, "proto_version": "1"},
        )
    except psycopg2.errors.UndefinedObject:
        cur.create_replication_slot(PG_SLOT, output_plugin="pgoutput")
        cur.start_replication(
            slot_name=PG_SLOT, decode=False,
            options={"publication_names": PG_PUB, "proto_version": "1"},
        )

    payloads: list[bytes] = []
    deadline = time.time() + timeout_s

    while len(payloads) < max_events and time.time() < deadline:
        msg = cur.read_message()
        if msg is None:
            # No message ready — wait briefly for the connection's read
            # fd to signal more data, capped so we still honor `deadline`.
            try:
                select.select([cur], [], [], 0.5)
            except (OSError, ValueError):
                # On Windows, `select` on a libpq cursor occasionally
                # raises if the connection is between states; just back
                # off and try again on the next loop iteration.
                time.sleep(0.1)
            continue
        raw = msg.payload
        if isinstance(raw, str):
            raw = raw.encode("utf-8", errors="replace")
        payloads.append(bytes(raw))
        try:
            cur.send_feedback(flush_lsn=msg.data_start)
        except Exception:
            pass

    try:
        cur.close()
    except Exception:
        pass
    return payloads


def test_insert_decodes_via_real_postgres(pg_setup):
    """Round-trip: Insert a row, drain events, decode, assert."""
    from fpulse.connectors.pgoutput import PgoutputDecoder

    cur = pg_setup.cursor()
    cur.execute("INSERT INTO users (id, email) VALUES (1, 'alice@test.com')")
    pg_setup.commit()
    cur.close()

    repl = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASS,
        connection_factory=LogicalReplicationConnection,
    )
    try:
        payloads = _consume_events(repl, max_events=4)
    finally:
        repl.close()

    decoder = PgoutputDecoder()
    events = [decoder.decode(p) for p in payloads]
    inserts = [e for e in events if e and e["op"] == "I"]
    assert inserts, f"No Insert decoded — payloads={payloads!r}"
    assert inserts[0]["after"]["id"] == "1"
    assert inserts[0]["after"]["email"] == "alice@test.com"


def test_schema_change_bumps_schema_version(pg_setup):
    """Sprint B exit gate: ALTER TABLE ADD COLUMN, then INSERT — the new
    Insert event must carry the updated column and a bumped
    schema_version."""
    from fpulse.connectors.pgoutput import PgoutputDecoder

    decoder = PgoutputDecoder()

    # Snapshot the current schema once via a Relation message.
    cur = pg_setup.cursor()
    cur.execute("INSERT INTO users (id, email) VALUES (10, 'pre@test.com')")
    pg_setup.commit()

    # Now add a column.
    cur.execute("ALTER TABLE users ADD COLUMN active BOOLEAN DEFAULT TRUE")
    cur.execute("INSERT INTO users (id, email, active) VALUES (11, 'post@test.com', TRUE)")
    pg_setup.commit()
    cur.close()

    repl = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASS,
        connection_factory=LogicalReplicationConnection,
    )
    try:
        payloads = _consume_events(repl, max_events=12)
    finally:
        repl.close()

    versions_seen = set()
    last_insert = None
    for p in payloads:
        ev = decoder.decode(p)
        if ev and ev["op"] in ("I", "R"):
            versions_seen.add(ev.get("schema_version", 0))
            if ev["op"] == "I":
                last_insert = ev

    assert last_insert is not None
    assert "active" in (last_insert["after"] or {}), \
        f"new column 'active' missing from Insert after schema change: {last_insert!r}"
    assert max(versions_seen) >= 2, \
        f"schema_version did not advance past 1; saw {sorted(versions_seen)}"
