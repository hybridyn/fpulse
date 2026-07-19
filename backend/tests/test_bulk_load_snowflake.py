"""Snowflake dialect plugin tests — Sprint 1 / Gate 2.

We patch `_try_import_driver` so the plugin uses an in-memory fake
connection. No live Snowflake required. Tests verify the SQL shapes
and CSV staging payload are correct.
"""

from __future__ import annotations

import os

import pytest

from fpulse.engine.bulk_load.types import BulkLoadRequest
import fpulse.engine.bulk_load.dialects.snowflake as sf_mod


# ── Fakes ────────────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, sink: list[dict]):
        self._sink = sink
        self.closed = False

    def execute(self, sql: str):
        self._sink.append({"kind": "execute", "sql": sql})

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self):
        self.events: list[dict] = []
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return FakeCursor(self.events)

    def commit(self):
        self.events.append({"kind": "commit"})
        self.committed = True

    def rollback(self):
        self.events.append({"kind": "rollback"})
        self.rolled_back = True

    def close(self):
        pass


class FakeDriver:
    def __init__(self):
        self.last_kwargs: dict | None = None
        self.next_conn = FakeConn()

    def connect(self, **kwargs):
        self.last_kwargs = kwargs
        return self.next_conn


@pytest.fixture
def fake_driver(monkeypatch):
    drv = FakeDriver()
    monkeypatch.setattr(sf_mod, "_try_import_driver", lambda: drv)
    return drv


class FakeRelation:
    def __init__(self, columns, rows):
        self.columns = columns
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


def _make_request(*, mode="append", primary_key=None, columns=None, rows=None,
                  staging_dir=None, table="customers", schema_name="public"):
    return BulkLoadRequest(
        conn_type="snowflake",
        config={
            "account": "acct123",
            "user": "u",
            "password": "p",
            "warehouse": "WH",
            "database": "DB",
            "schema": "PUBLIC",
            "role": "DEV",
        },
        table=table,
        schema_name=schema_name,
        mode=mode,
        primary_key=list(primary_key or []),
        relation=FakeRelation(columns or ["id", "name"], rows or [(1, "Alice"), (2, "Bob")]),
        duckdb_conn=object(),
        staging_dir=staging_dir,
    )


def _execed_sqls(conn: FakeConn) -> list[str]:
    return [e["sql"] for e in conn.events if e["kind"] == "execute"]


# ── Availability ─────────────────────────────────────────────────────


class TestAvailability:
    def test_is_available_true_with_fake_driver(self, fake_driver):
        plugin = sf_mod.SnowflakeBulkLoader()
        assert plugin.is_available() is True

    def test_load_raises_when_driver_missing(self, monkeypatch):
        monkeypatch.setattr(sf_mod, "_try_import_driver", lambda: None)
        plugin = sf_mod.SnowflakeBulkLoader()
        from fpulse.engine.bulk_load.types import BulkLoaderNotAvailable
        with pytest.raises(BulkLoaderNotAvailable):
            plugin.load(_make_request())


# ── Connect kwargs ───────────────────────────────────────────────────


class TestConnect:
    def test_connect_passes_through_main_keys(self, fake_driver, tmp_path):
        plugin = sf_mod.SnowflakeBulkLoader()
        plugin.load(_make_request(staging_dir=str(tmp_path)))
        kw = fake_driver.last_kwargs
        assert kw["account"] == "acct123"
        assert kw["user"] == "u"
        assert kw["warehouse"] == "WH"
        assert kw["database"] == "DB"
        assert kw["schema"] == "PUBLIC"
        assert kw["role"] == "DEV"
        assert kw["password"] == "p"

    def test_connect_drops_empty_kwargs(self, fake_driver, tmp_path):
        plugin = sf_mod.SnowflakeBulkLoader()
        req = _make_request(staging_dir=str(tmp_path))
        # Strip warehouse + role; the driver should not see them at all.
        req.config = {
            "account": "a",
            "user": "u",
            "password": "p",
            "database": "DB",
            "schema": "PUBLIC",
        }
        plugin.load(req)
        kw = fake_driver.last_kwargs
        assert "warehouse" not in kw
        assert "role" not in kw


# ── PUT + COPY pipeline ──────────────────────────────────────────────


class TestPutCopyPipeline:
    def test_append_runs_create_stage_put_copy_remove(self, fake_driver, tmp_path):
        plugin = sf_mod.SnowflakeBulkLoader()
        result = plugin.load(_make_request(mode="append", staging_dir=str(tmp_path)))
        assert result.rows_loaded == 2
        sqls = _execed_sqls(fake_driver.next_conn)
        # Stage create
        assert any("CREATE STAGE IF NOT EXISTS" in s and "fpulse_bulk" in s for s in sqls)
        # PUT
        assert any(s.startswith("PUT 'file://") and "@~/fpulse_bulk" in s for s in sqls)
        # COPY INTO target
        assert any("COPY INTO " in s and '"public"."customers"' in s for s in sqls)
        # Remove from stage
        assert any(s.startswith("REMOVE @~/fpulse_bulk/") for s in sqls)
        assert fake_driver.next_conn.committed

    def test_create_drops_then_creates_then_copies(self, fake_driver, tmp_path):
        plugin = sf_mod.SnowflakeBulkLoader()
        result = plugin.load(_make_request(mode="create", staging_dir=str(tmp_path)))
        sqls = _execed_sqls(fake_driver.next_conn)
        assert any("DROP TABLE IF EXISTS" in s for s in sqls)
        assert any("CREATE TABLE" in s and "VARCHAR" in s for s in sqls)
        assert any("COPY INTO" in s for s in sqls)
        # Should warn about VARCHAR-only types.
        assert any("VARCHAR" in w for w in result.warnings)

    def test_truncate_runs_truncate_before_copy(self, fake_driver, tmp_path):
        plugin = sf_mod.SnowflakeBulkLoader()
        plugin.load(_make_request(mode="truncate", staging_dir=str(tmp_path)))
        events = fake_driver.next_conn.events
        truncate_idx = next(
            i for i, e in enumerate(events)
            if e["kind"] == "execute" and "TRUNCATE TABLE" in e["sql"]
        )
        copy_idx = next(
            i for i, e in enumerate(events)
            if e["kind"] == "execute" and "COPY INTO" in e["sql"] and "FROM @~/fpulse_bulk" in e["sql"]
        )
        assert truncate_idx < copy_idx


# ── MERGE mode ───────────────────────────────────────────────────────


class TestMerge:
    def test_merge_uses_temp_table_and_native_merge(self, fake_driver, tmp_path):
        plugin = sf_mod.SnowflakeBulkLoader()
        plugin.load(_make_request(
            mode="merge", primary_key=["id"], staging_dir=str(tmp_path),
        ))
        sqls = _execed_sqls(fake_driver.next_conn)
        # Temp table created.
        assert any("CREATE TEMPORARY TABLE" in s and "FPULSE_BULK_STAGE" in s for s in sqls)
        # COPY INTO targets the staging table (not the real table).
        copy_sqls = [s for s in sqls if "COPY INTO" in s and "FROM @~/fpulse_bulk" in s]
        assert len(copy_sqls) == 1
        assert "FPULSE_BULK_STAGE" in copy_sqls[0]
        assert '"public"."customers"' not in copy_sqls[0]
        # MERGE INTO target.
        merge_sqls = [s for s in sqls if "MERGE INTO" in s]
        assert len(merge_sqls) == 1
        assert "WHEN MATCHED THEN UPDATE SET" in merge_sqls[0]
        assert "WHEN NOT MATCHED THEN INSERT" in merge_sqls[0]

    def test_merge_pk_only_columns_skips_update(self, fake_driver, tmp_path):
        plugin = sf_mod.SnowflakeBulkLoader()
        plugin.load(_make_request(
            mode="merge", primary_key=["id"], columns=["id"], rows=[(1,), (2,)],
            staging_dir=str(tmp_path),
        ))
        sqls = _execed_sqls(fake_driver.next_conn)
        merge = next(s for s in sqls if "MERGE INTO" in s)
        assert "WHEN MATCHED THEN UPDATE SET" not in merge
        assert "WHEN NOT MATCHED THEN INSERT" in merge


# ── CSV staging ──────────────────────────────────────────────────────


class TestCsvStaging:
    def test_csv_file_written_and_cleaned_up(self, fake_driver, tmp_path):
        plugin = sf_mod.SnowflakeBulkLoader()
        result = plugin.load(_make_request(staging_dir=str(tmp_path)))
        # staged_files lists the local file path for observability.
        assert len(result.staged_files) == 1
        # File is removed after the call (caller should not need to clean up).
        assert not os.path.exists(result.staged_files[0])

    def test_csv_handles_none_and_quotes(self, fake_driver, tmp_path):
        plugin = sf_mod.SnowflakeBulkLoader()
        plugin._materialize_rows = lambda r: (   # type: ignore[method-assign]
            ["id", "name"],
            [(1, None), (2, 'has "quote" inside'), (3, "comma, here")],
        )

        # Capture the staged file before it's removed.
        captured: list[str] = []

        original_put = plugin._put_file
        def capturing_put(cur, local_path):
            with open(local_path, "r", encoding="utf-8") as f:
                captured.append(f.read())
            return original_put(cur, local_path)
        plugin._put_file = capturing_put  # type: ignore[method-assign]

        plugin.load(_make_request(staging_dir=str(tmp_path)))
        assert len(captured) == 1
        body = captured[0]
        # Header row, then 3 data rows.
        lines = body.splitlines()
        assert lines[0] == "id,name"
        assert "1," in lines[1]                # None → empty
        assert '"has ""quote"" inside"' in lines[2]
        assert '"comma, here"' in lines[3]


# ── Identifier escapers ──────────────────────────────────────────────


class TestIdentEscaping:
    def test_quote_ident(self):
        assert sf_mod._quote_ident("customers") == '"customers"'
        assert sf_mod._quote_ident('bad"name') == '"bad""name"'

    def test_qualified_table_keeps_three_parts(self):
        # database.schema.table — pass through.
        out = sf_mod._qualified_table("public", "DB.SCHEMA.TBL")
        assert out == '"DB"."SCHEMA"."TBL"'

    def test_qualified_table_uses_schema_when_no_dot(self):
        assert sf_mod._qualified_table("PUBLIC", "customers") == '"PUBLIC"."customers"'


# ── Error path ───────────────────────────────────────────────────────


class TestErrorPath:
    def test_copy_failure_rolls_back_and_cleans_stage(self, monkeypatch, fake_driver, tmp_path):
        plugin = sf_mod.SnowflakeBulkLoader()

        original_copy = plugin._copy_into

        def boom_copy(cur, target_qual, staged_name, columns):
            # Only fail when targeting the real table; the merge path's
            # staging-table COPY should still succeed if any test wants it.
            if '"public"."customers"' in target_qual:
                raise RuntimeError("simulated COPY failure")
            return original_copy(cur, target_qual, staged_name, columns)

        plugin._copy_into = boom_copy  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="simulated COPY failure"):
            plugin.load(_make_request(mode="append", staging_dir=str(tmp_path)))

        assert fake_driver.next_conn.rolled_back
        # REMOVE @~/fpulse_bulk/... must still run even on failure so the
        # user stage doesn't accumulate orphan files.
        sqls = _execed_sqls(fake_driver.next_conn)
        assert any(s.startswith("REMOVE @~/fpulse_bulk/") for s in sqls)
