"""Unit tests for the bulk-load runner foundation — Sprint 1 / Gate 1.

Covers:
  * Registry: register / get / available_dialects / Protocol type-check.
  * Runner: validation, BulkLoaderNotAvailable, dispatch, default-fill of
    duration / dialect / method on the result.
  * Postgres plugin SQL helpers: identifier quoting, qualified table,
    is_available() honesty, register-on-import.

The Postgres plugin's actual COPY path is exercised in
test_bulk_load_postgres.py with a fake connection — no live DB required.
"""

from __future__ import annotations

import pytest

from fpulse.engine.bulk_load import (
    BulkLoaderNotAvailable,
    BulkLoaderProtocol,
    BulkLoadRequest,
    BulkLoadResult,
    available_dialects,
    bulk_load,
    get,
)
from fpulse.engine.bulk_load.registry import _clear_for_tests, register
from fpulse.engine.bulk_load.dialects.postgres import (
    _quote_ident,
    _qualified_table,
    PostgresBulkLoader,
)
from fpulse.engine.bulk_load.dialects.snowflake import SnowflakeBulkLoader


class FakeRelation:
    """Minimal stand-in for a DuckDBPyRelation for tests that don't want
    a live duckdb dependency for every assertion."""

    def __init__(self, columns: list[str], rows: list[tuple]):
        self.columns = columns
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Stand-in for a duckdb conn — only fetchall() is exercised by the
    runner-level tests; plugin-level tests use a richer fake."""


class FakePlugin:
    """Test plugin that records calls and lets the test choose its
    is_available() result."""

    dialect = "test"
    method = "FAKE LOAD"

    def __init__(self, available: bool = True):
        self._available = available
        self.calls: list[BulkLoadRequest] = []

    def is_available(self) -> bool:
        return self._available

    def load(self, request: BulkLoadRequest) -> BulkLoadResult:
        self.calls.append(request)
        return BulkLoadResult(
            rows_loaded=len(request.relation.fetchall()),
            duration_ms=0,           # let runner backfill
            dialect="",              # let runner backfill
            method="",               # let runner backfill
        )


# ── Auto-cleanup of registry between tests ───────────────────────────


@pytest.fixture(autouse=True)
def _restore_registry():
    """Make sure each test starts/ends with the production plugin set
    only — no test pollution into another test's registry view.

    Python caches imports, so `import dialects` after `_clear_for_tests()`
    is a no-op and would NOT re-run the @register call. We re-register
    each production plugin explicitly instead, mirroring what plugin
    modules do at import time.
    """
    yield
    _clear_for_tests()
    # Re-register every production plugin. Update this list when adding a
    # new dialect (BigQuery, Redshift, etc.).
    register(PostgresBulkLoader())
    register(SnowflakeBulkLoader())
    after = set(get_all_dialects())
    assert "postgresql" in after and "snowflake" in after, (
        f"Registry restoration failed; expected postgres+snowflake in {after}"
    )


def get_all_dialects() -> list[str]:
    """Helper: list every registered dialect, regardless of is_available()."""
    from fpulse.engine.bulk_load import registry as _r
    return list(_r._REGISTRY.keys())


# ── Registry ─────────────────────────────────────────────────────────


class TestRegistry:
    def test_postgres_plugin_registered_on_import(self):
        plugin = get("postgresql")
        assert plugin is not None
        assert plugin.dialect == "postgresql"
        assert plugin.method == "COPY FROM STDIN"

    def test_postgres_plugin_satisfies_protocol(self):
        plugin = get("postgresql")
        assert isinstance(plugin, BulkLoaderProtocol)

    def test_get_returns_none_for_unknown_dialect(self):
        assert get("never-heard-of-it") is None

    def test_register_rejects_non_protocol_object(self):
        class NotAPlugin: pass
        with pytest.raises(TypeError):
            register(NotAPlugin())  # type: ignore[arg-type]

    def test_available_dialects_excludes_unavailable(self):
        register(FakePlugin(available=False))
        avail = available_dialects()
        assert "test" not in avail

    def test_available_dialects_includes_available(self):
        register(FakePlugin(available=True))
        avail = available_dialects()
        assert "test" in avail


# ── Runner validation ────────────────────────────────────────────────


class TestRunnerValidation:
    def test_no_relation_raises(self):
        req = BulkLoadRequest(
            conn_type="postgresql",
            config={"host": "localhost"},
            table="t",
            duckdb_conn=FakeConn(),
        )
        with pytest.raises(ValueError, match="relation is required"):
            bulk_load(req)

    def test_no_duckdb_conn_raises(self):
        req = BulkLoadRequest(
            conn_type="postgresql",
            config={"host": "localhost"},
            table="t",
            relation=FakeRelation(["a"], [(1,)]),
        )
        with pytest.raises(ValueError, match="duckdb_conn is required"):
            bulk_load(req)

    def test_no_table_raises(self):
        req = BulkLoadRequest(
            conn_type="postgresql",
            config={"host": "localhost"},
            table="",
            relation=FakeRelation(["a"], [(1,)]),
            duckdb_conn=FakeConn(),
        )
        with pytest.raises(ValueError, match="table is required"):
            bulk_load(req)

    def test_merge_without_pk_raises(self):
        req = BulkLoadRequest(
            conn_type="postgresql",
            config={"host": "localhost"},
            table="t",
            mode="merge",
            relation=FakeRelation(["a"], [(1,)]),
            duckdb_conn=FakeConn(),
        )
        with pytest.raises(ValueError, match="primary_key"):
            bulk_load(req)


# ── Runner dispatch ──────────────────────────────────────────────────


class TestRunnerDispatch:
    def test_unknown_dialect_raises_not_available(self):
        req = BulkLoadRequest(
            conn_type="not-real",
            config={},
            table="t",
            relation=FakeRelation(["a"], [(1,)]),
            duckdb_conn=FakeConn(),
        )
        with pytest.raises(BulkLoaderNotAvailable):
            bulk_load(req)

    def test_plugin_unavailable_raises_not_available(self):
        register(FakePlugin(available=False))
        req = BulkLoadRequest(
            conn_type="test",
            config={},
            table="t",
            relation=FakeRelation(["a"], [(1,)]),
            duckdb_conn=FakeConn(),
        )
        with pytest.raises(BulkLoaderNotAvailable):
            bulk_load(req)

    def test_runner_backfills_duration_dialect_method(self):
        plugin = FakePlugin(available=True)
        register(plugin)
        req = BulkLoadRequest(
            conn_type="test",
            config={},
            table="t",
            relation=FakeRelation(["a"], [(1,), (2,), (3,)]),
            duckdb_conn=FakeConn(),
        )
        result = bulk_load(req)
        assert result.rows_loaded == 3
        assert result.dialect == "test"          # backfilled
        assert result.method == "FAKE LOAD"      # backfilled
        assert result.duration_ms >= 0           # backfilled (real wall clock)
        assert plugin.calls == [req]


# ── Postgres plugin SQL helpers ──────────────────────────────────────


class TestPostgresHelpers:
    def test_quote_ident_basic(self):
        assert _quote_ident("customers") == '"customers"'
        assert _quote_ident("My Table") == '"My Table"'

    def test_quote_ident_doubles_internal_quote(self):
        # Defeats SQL injection via name like:  bad"; DROP TABLE x;--
        assert _quote_ident('bad"name') == '"bad""name"'

    def test_qualified_table_uses_schema_when_no_dot(self):
        assert _qualified_table("public", "customers") == '"public"."customers"'

    def test_qualified_table_keeps_explicit_schema(self):
        assert _qualified_table("public", "analytics.customers") == '"analytics"."customers"'

    def test_postgres_is_available_consistent(self):
        plugin = PostgresBulkLoader()
        # Calling it twice must give the same answer (no flakiness from
        # caching). is_available() must NOT raise on a host without psycopg2.
        a = plugin.is_available()
        b = plugin.is_available()
        assert a == b
        assert isinstance(a, bool)
