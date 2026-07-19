"""B2.1 wiring test (2026-06-08).

Confirms the warehouse sink's merge mode maps merge_key onto
BulkLoadRequest.primary_key and delegates to the bulk-load registry —
WITHOUT a live warehouse. A fake loader records the request it
receives. The actual MERGE execution against a real DB is [LIVE-SMOKE]
and out of scope for unit tests.

Contracts pinned:
  * merge_key param parsed into a clean column list (comma-split)
  * conn_type normalized to the registry's dialect key
  * merge mode with no merge_key raises a clear error
  * merge mode builds a BulkLoadRequest(mode='merge', primary_key=...)
    and calls the registered loader
  * unavailable loader raises a clear error
"""
from __future__ import annotations

import duckdb
import pytest

from fpulse.nodes.sinks import WarehouseSinkNode


def _node(**params):
    p = {"_step_id": "s1", **params}
    return WarehouseSinkNode(params=p)


def _ctx():
    from fpulse.nodes.base import ExecutionContext
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def _relation(ctx):
    ctx.conn.execute("CREATE TABLE __src AS SELECT 1 AS id, 'a' AS name")
    return ctx.conn.sql("SELECT * FROM __src")


# ── merge_key parsing ───────────────────────────────────────────────


class TestMergeKeyParsing:
    def test_single_key(self):
        assert _node(merge_key="id")._parse_merge_key() == ["id"]

    def test_composite_key_comma_split(self):
        assert _node(merge_key="customer_id, order_date")._parse_merge_key() == \
            ["customer_id", "order_date"]

    def test_empty(self):
        assert _node()._parse_merge_key() == []
        assert _node(merge_key="  ")._parse_merge_key() == []

    def test_strips_and_drops_blanks(self):
        assert _node(merge_key="a, , b ,")._parse_merge_key() == ["a", "b"]


class TestDialectNormalization:
    def test_postgres_alias(self):
        assert WarehouseSinkNode._normalize_dialect("postgres") == "postgresql"

    def test_sqlserver_alias(self):
        assert WarehouseSinkNode._normalize_dialect("sqlserver") == "mssql"

    def test_passthrough_unknown(self):
        assert WarehouseSinkNode._normalize_dialect("faketest") == "faketest"


# ── merge delegation ────────────────────────────────────────────────


class _FakeLoader:
    """Minimal bulk-load plugin that records the request it receives."""
    dialect = "faketest"
    method = "FAKE"

    def __init__(self):
        self.received = None

    def is_available(self):
        return True

    def load(self, request):
        self.received = request
        from fpulse.engine.bulk_load.types import BulkLoadResult
        return BulkLoadResult(rows_loaded=0, duration_ms=0,
                               dialect=self.dialect, method=self.method,
                               warnings=[])


class TestMergeDelegation:
    def _register_fake(self):
        from fpulse.engine.bulk_load import registry as reg
        fake = _FakeLoader()
        reg.register(fake)
        return fake, reg

    def test_merge_builds_request_with_primary_key(self):
        fake, reg = self._register_fake()
        try:
            ctx = _ctx()
            src = _relation(ctx)
            node = _node(merge_key="id", table="orders", schema="analytics")
            out = node._write_merge(ctx, src, "orders", "analytics",
                                     "faketest", {"host": "x"})
            assert fake.received is not None
            req = fake.received
            assert req.mode == "merge"
            assert req.primary_key == ["id"]
            assert req.table == "orders"
            assert req.schema_name == "analytics"
            assert req.conn_type == "faketest"
            # passthrough: returns the source relation
            assert out is src
        finally:
            reg._clear_for_tests()

    def test_composite_key_passed_through(self):
        fake, reg = self._register_fake()
        try:
            ctx = _ctx()
            src = _relation(ctx)
            node = _node(merge_key="customer_id, order_date")
            node._write_merge(ctx, src, "t", "public", "faketest", {})
            assert fake.received.primary_key == ["customer_id", "order_date"]
        finally:
            reg._clear_for_tests()

    def test_missing_merge_key_raises(self):
        ctx = _ctx()
        src = _relation(ctx)
        node = _node()  # no merge_key
        with pytest.raises(ValueError, match="merge mode requires Merge Key"):
            node._write_merge(ctx, src, "t", "public", "postgresql", {})

    def test_unavailable_loader_raises(self):
        # No loader registered for this dialect → clear error
        from fpulse.engine.bulk_load import registry as reg
        reg._clear_for_tests()
        ctx = _ctx()
        src = _relation(ctx)
        node = _node(merge_key="id")
        with pytest.raises(ValueError, match="needs an installed bulk loader"):
            node._write_merge(ctx, src, "t", "public", "no_such_dialect", {})
