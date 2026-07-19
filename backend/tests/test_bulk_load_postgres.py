"""Postgres dialect plugin tests — exercise the COPY/UPSERT generation
without needing a live Postgres. We inject a fake driver into the loader
so the SQL strings + COPY payload can be inspected.

These tests run on every host (no psycopg2 install required) because we
patch _try_import_driver to return a fake driver module.
"""

from __future__ import annotations

import io

import pytest

from fpulse.engine.bulk_load.types import BulkLoadRequest
import fpulse.engine.bulk_load.dialects.postgres as pg_mod


# ── Fakes ────────────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, sink: list[dict]):
        self._sink = sink

    def execute(self, sql: str, params=None):
        self._sink.append({"kind": "execute", "sql": sql, "params": params})

    def copy_expert(self, sql: str, file: io.StringIO):
        self._sink.append({
            "kind": "copy_expert",
            "sql": sql,
            "payload": file.getvalue(),
        })


class FakeConn:
    def __init__(self):
        self.events: list[dict] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self.events)

    def commit(self):
        self.committed = True
        self.events.append({"kind": "commit"})

    def rollback(self):
        self.rolled_back = True
        self.events.append({"kind": "rollback"})

    def close(self):
        self.closed = True


class FakeDriver:
    def __init__(self):
        self.last_kwargs: dict | None = None
        self.next_conn = FakeConn()

    def connect(self, **kwargs):
        self.last_kwargs = kwargs
        return self.next_conn


@pytest.fixture
def fake_driver(monkeypatch):
    """Replace _try_import_driver so PostgresBulkLoader uses our fake."""
    drv = FakeDriver()
    monkeypatch.setattr(pg_mod, "_try_import_driver", lambda: drv)
    return drv


class FakeRelation:
    def __init__(self, columns, rows):
        self.columns = columns
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


def _make_request(*, mode="append", primary_key=None, columns=None, rows=None):
    return BulkLoadRequest(
        conn_type="postgresql",
        config={"host": "h", "port": 5432, "database": "d", "user": "u", "password": "p"},
        table="customers",
        schema_name="public",
        mode=mode,
        primary_key=list(primary_key or []),
        relation=FakeRelation(columns or ["id", "name"], rows or [(1, "Alice"), (2, "Bob")]),
        duckdb_conn=object(),  # unused on this path
    )


# ── is_available + availability gating ───────────────────────────────


class TestAvailability:
    def test_is_available_true_with_fake_driver(self, fake_driver):
        plugin = pg_mod.PostgresBulkLoader()
        assert plugin.is_available() is True

    def test_load_raises_when_driver_missing(self, monkeypatch):
        monkeypatch.setattr(pg_mod, "_try_import_driver", lambda: None)
        plugin = pg_mod.PostgresBulkLoader()
        from fpulse.engine.bulk_load.types import BulkLoaderNotAvailable
        with pytest.raises(BulkLoaderNotAvailable):
            plugin.load(_make_request())


# ── Mode handlers — verify SQL shape + COPY payload ─────────────────


class TestAppend:
    def test_append_runs_one_copy(self, fake_driver):
        plugin = pg_mod.PostgresBulkLoader()
        result = plugin.load(_make_request(mode="append"))
        assert result.rows_loaded == 2
        events = fake_driver.next_conn.events
        copies = [e for e in events if e["kind"] == "copy_expert"]
        assert len(copies) == 1
        assert "COPY \"public\".\"customers\"" in copies[0]["sql"]
        assert "FORMAT CSV" in copies[0]["sql"]
        # Payload contains both rows in CSV form.
        assert "1,Alice" in copies[0]["payload"]
        assert "2,Bob" in copies[0]["payload"]
        assert fake_driver.next_conn.committed


class TestCreate:
    def test_create_drops_and_creates_table(self, fake_driver):
        plugin = pg_mod.PostgresBulkLoader()
        result = plugin.load(_make_request(mode="create"))
        assert result.rows_loaded == 2
        sqls = [e["sql"] for e in fake_driver.next_conn.events if e["kind"] == "execute"]
        assert any("DROP TABLE IF EXISTS" in s for s in sqls)
        assert any("CREATE TABLE" in s for s in sqls)
        # Should warn about text-only types.
        assert any("text" in w.lower() for w in result.warnings)


class TestTruncate:
    def test_truncate_runs_truncate_then_copy(self, fake_driver):
        plugin = pg_mod.PostgresBulkLoader()
        result = plugin.load(_make_request(mode="truncate"))
        assert result.rows_loaded == 2
        events = fake_driver.next_conn.events
        execs = [e for e in events if e["kind"] == "execute"]
        # Order matters: TRUNCATE must precede the COPY.
        truncate_idx = next(i for i, e in enumerate(events) if e["kind"] == "execute" and "TRUNCATE" in e["sql"])
        copy_idx = next(i for i, e in enumerate(events) if e["kind"] == "copy_expert")
        assert truncate_idx < copy_idx
        assert any("TRUNCATE TABLE" in s for s in [e["sql"] for e in execs])


class TestMerge:
    def test_merge_uses_temp_table_and_on_conflict(self, fake_driver):
        plugin = pg_mod.PostgresBulkLoader()
        result = plugin.load(_make_request(mode="merge", primary_key=["id"]))
        assert result.rows_loaded == 2
        events = fake_driver.next_conn.events
        sqls = [e["sql"] for e in events if e["kind"] == "execute"]
        # Must create a TEMP staging table.
        assert any("CREATE TEMP TABLE" in s and "_fpulse_bulk_stage" in s for s in sqls)
        # Must do INSERT … ON CONFLICT … DO UPDATE SET …
        assert any("ON CONFLICT" in s and "DO UPDATE SET" in s for s in sqls)
        # The COPY must target the staging table, not the real table.
        copies = [e for e in events if e["kind"] == "copy_expert"]
        assert len(copies) == 1
        assert "_fpulse_bulk_stage" in copies[0]["sql"]
        assert "\"public\".\"customers\"" not in copies[0]["sql"]

    def test_merge_with_pk_only_columns_uses_do_nothing(self, fake_driver):
        plugin = pg_mod.PostgresBulkLoader()
        # Table is just (id) — id is the PK, no other columns to UPDATE.
        req = _make_request(
            mode="merge", primary_key=["id"],
            columns=["id"], rows=[(1,), (2,)],
        )
        plugin.load(req)
        sqls = [e["sql"] for e in fake_driver.next_conn.events if e["kind"] == "execute"]
        assert any("ON CONFLICT" in s and "DO NOTHING" in s for s in sqls)


# ── Connection wiring ────────────────────────────────────────────────


class TestConnect:
    def test_connect_passes_through_config(self, fake_driver):
        plugin = pg_mod.PostgresBulkLoader()
        plugin.load(_make_request())
        kw = fake_driver.last_kwargs
        assert kw["host"] == "h"
        assert kw["port"] == 5432
        assert kw["dbname"] == "d"
        assert kw["user"] == "u"
        assert kw["password"] == "p"

    def test_connect_falls_back_to_dbname_alias(self, fake_driver):
        plugin = pg_mod.PostgresBulkLoader()
        req = _make_request()
        # Some connection rows store 'dbname' instead of 'database'.
        req.config = {"host": "h", "port": 5432, "dbname": "d", "username": "u"}
        plugin.load(req)
        kw = fake_driver.last_kwargs
        assert kw["dbname"] == "d"
        assert kw["user"] == "u"


# ── Rollback on error ────────────────────────────────────────────────


class TestRollback:
    def test_failed_copy_rolls_back(self, monkeypatch, fake_driver):
        plugin = pg_mod.PostgresBulkLoader()

        # Make copy_expert blow up.
        original_cursor = fake_driver.next_conn.cursor

        def boom_cursor():
            cur = original_cursor()
            def boom(sql, file):
                raise RuntimeError("simulated copy failure")
            cur.copy_expert = boom
            return cur

        monkeypatch.setattr(fake_driver.next_conn, "cursor", boom_cursor)

        with pytest.raises(RuntimeError, match="simulated copy failure"):
            plugin.load(_make_request(mode="append"))
        assert fake_driver.next_conn.rolled_back
        assert not fake_driver.next_conn.committed
