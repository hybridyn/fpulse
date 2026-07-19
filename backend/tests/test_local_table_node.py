"""Tests for the local_table_source + local_table_sink IR nodes (Y3 2026-05-23).

Each test sets up a real on-disk workspace, runs a sink in one of three
modes against a fresh DuckDB connection, then reads back via the source
to verify the on-disk state matches expectations.

Three sink modes covered: replace, append, merge.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


@pytest.fixture
def duck_conn():
    """Fresh in-memory DuckDB connection. Each test gets its own."""
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect()
    yield conn
    conn.close()


@pytest.fixture
def ctx_factory(tmp_path, duck_conn, datastore, schema_history_store):
    """Builds an ExecutionContext-shaped namespace per test.

    Z21 (2026-05-23) moved the sink to the `_input_step_ids` injection
    contract used by the real executor. ``ctx.input = ...`` assignments
    in this file's tests now need a ``_results`` dict + a
    ``get_inputs(step_ids)`` method to land in the same code path. We
    patch the ``input`` setattr through a small property so each test
    sees both the old assignment style (``ctx.input = rel``) AND the
    new injection contract — keeping the existing test bodies untouched
    while the production sink runs the same path it does in prod.
    """
    def _factory(input_rel=None):
        results: dict[str, object] = {}
        ns = SimpleNamespace(
            conn=duck_conn,
            app_state={
                "data_dir": str(tmp_path),
                "datastore": datastore,
                "schema_history_store": schema_history_store,
            },
            workspace_id="default",
            _results=results,
            run_id="run-test-001",
        )

        def _get_inputs(step_ids):
            return [results[sid] for sid in step_ids if sid in results]

        ns.get_inputs = _get_inputs

        # Old test bodies do ``ctx.input = rel``; bridge that to the
        # _results dict so the sink's last-resort fallback finds it.
        class _Proxy:
            def __get__(self, obj, objtype=None):
                vals = list(results.values())
                return vals[-1] if vals else None

            def __set__(self, obj, value):
                if value is None:
                    results.pop("_legacy_input", None)
                else:
                    results["_legacy_input"] = value

        # Apply the property at the type level on a fresh subclass so
        # the SimpleNamespace stays per-fixture.
        ProxyNS = type("CtxNS", (SimpleNamespace,), {"input": _Proxy()})
        proxied = ProxyNS(**ns.__dict__)
        proxied.get_inputs = _get_inputs
        if input_rel is not None:
            proxied.input = input_rel
        return proxied
    return _factory


def _run_sink(node_cls, params: dict, ctx) -> None:
    node = node_cls(params)
    node.execute(ctx)


def _read_back(ctx, schema: str, table: str):
    """Read a managed table directly through DuckDB so the test can
    assert on row contents independent of the source node."""
    glob = os.path.join(
        ctx.app_state["data_dir"], "tables", "default", schema, table, "part-*.parquet"
    ).replace("\\", "/")
    return ctx.conn.sql(f"SELECT * FROM read_parquet('{glob}') ORDER BY ALL").fetchall()


class TestLocalTableSink:
    def test_replace_mode_writes_part_zero(self, ctx_factory):
        from fpulse.nodes.local_table import LocalTableSinkNode
        ctx = ctx_factory()
        # Make a 2-row input relation.
        ctx.input = ctx.conn.sql("SELECT 1 AS id, 'alice' AS name UNION ALL SELECT 2, 'bob'")

        _run_sink(LocalTableSinkNode, {
            "schema_name": "default", "table_name": "customers", "mode": "replace",
        }, ctx)

        rows = _read_back(ctx, "default", "customers")
        assert sorted(rows) == [(1, "alice"), (2, "bob")]

    def test_replace_mode_overwrites_existing_parts(self, ctx_factory):
        from fpulse.nodes.local_table import LocalTableSinkNode
        ctx = ctx_factory()
        # First write
        ctx.input = ctx.conn.sql("SELECT 1 AS id, 'alice' AS name")
        _run_sink(LocalTableSinkNode, {
            "schema_name": "default", "table_name": "customers", "mode": "replace",
        }, ctx)
        # Replace
        ctx.input = ctx.conn.sql(
            "SELECT 10 AS id, 'newalice' AS name UNION ALL SELECT 20, 'newbob'"
        )
        _run_sink(LocalTableSinkNode, {
            "schema_name": "default", "table_name": "customers", "mode": "replace",
        }, ctx)
        rows = _read_back(ctx, "default", "customers")
        assert sorted(rows) == [(10, "newalice"), (20, "newbob")]
        # Only one part file.
        table_dir = os.path.join(
            ctx.app_state["data_dir"], "tables", "default", "default", "customers"
        )
        parts = [f for f in os.listdir(table_dir) if f.startswith("part-")]
        assert len(parts) == 1

    def test_append_mode_writes_additional_part_file(self, ctx_factory):
        from fpulse.nodes.local_table import LocalTableSinkNode
        ctx = ctx_factory()
        ctx.input = ctx.conn.sql("SELECT 1 AS id")
        _run_sink(LocalTableSinkNode, {
            "schema_name": "default", "table_name": "log", "mode": "replace",
        }, ctx)
        ctx.input = ctx.conn.sql("SELECT 2 AS id UNION ALL SELECT 3")
        _run_sink(LocalTableSinkNode, {
            "schema_name": "default", "table_name": "log", "mode": "append",
        }, ctx)

        table_dir = os.path.join(
            ctx.app_state["data_dir"], "tables", "default", "default", "log"
        )
        parts = sorted(f for f in os.listdir(table_dir) if f.startswith("part-"))
        assert len(parts) == 2  # part-000 + part-{timestamp}
        rows = _read_back(ctx, "default", "log")
        assert sorted(r[0] for r in rows) == [1, 2, 3]

    def test_merge_mode_upserts_on_key(self, ctx_factory):
        from fpulse.nodes.local_table import LocalTableSinkNode
        ctx = ctx_factory()
        # Seed: id 1, 2, 3
        ctx.input = ctx.conn.sql(
            "SELECT 1 AS id, 'alice' AS name "
            "UNION ALL SELECT 2, 'bob' "
            "UNION ALL SELECT 3, 'carol'"
        )
        _run_sink(LocalTableSinkNode, {
            "schema_name": "default", "table_name": "customers", "mode": "replace",
        }, ctx)
        # Merge: update id=2, insert id=4
        ctx.input = ctx.conn.sql(
            "SELECT 2 AS id, 'bob_updated' AS name "
            "UNION ALL SELECT 4, 'dave'"
        )
        _run_sink(LocalTableSinkNode, {
            "schema_name": "default", "table_name": "customers",
            "mode": "merge", "merge_on": ["id"],
        }, ctx)

        rows = _read_back(ctx, "default", "customers")
        assert sorted(rows) == sorted([
            (1, "alice"), (2, "bob_updated"), (3, "carol"), (4, "dave"),
        ])
        # Merge collapses back to a single part file.
        table_dir = os.path.join(
            ctx.app_state["data_dir"], "tables", "default", "default", "customers"
        )
        parts = [f for f in os.listdir(table_dir) if f.startswith("part-")]
        assert len(parts) == 1

    def test_merge_requires_merge_on(self, ctx_factory):
        from fpulse.nodes.local_table import LocalTableSinkNode
        ctx = ctx_factory(input_rel=None)
        ctx.input = ctx.conn.sql("SELECT 1 AS id")
        with pytest.raises(ValueError, match="merge mode requires"):
            _run_sink(LocalTableSinkNode, {
                "schema_name": "default", "table_name": "t", "mode": "merge",
            }, ctx)

    def test_unknown_mode_raises(self, ctx_factory):
        from fpulse.nodes.local_table import LocalTableSinkNode
        ctx = ctx_factory()
        ctx.input = ctx.conn.sql("SELECT 1 AS id")
        with pytest.raises(ValueError, match="unknown mode"):
            _run_sink(LocalTableSinkNode, {
                "schema_name": "default", "table_name": "t", "mode": "rewrite-everything",
            }, ctx)

    def test_table_metadata_refreshed_after_write(self, ctx_factory, datastore):
        from fpulse.nodes.local_table import LocalTableSinkNode
        ctx = ctx_factory()
        ctx.input = ctx.conn.sql("SELECT 1 AS id, 'alice' AS name")
        _run_sink(LocalTableSinkNode, {
            "schema_name": "default", "table_name": "customers", "mode": "replace",
        }, ctx)
        table = datastore.find_table_by_name("default", "default", "customers")
        assert table is not None
        assert table.row_count == 1
        assert table.column_count == 2
        assert table.part_count == 1
        cols = datastore.list_columns(table_id=table.id)
        assert sorted(c.name for c in cols) == ["id", "name"]


class TestLocalTableSource:
    def test_source_reads_written_rows(self, ctx_factory):
        from fpulse.nodes.local_table import LocalTableSinkNode, LocalTableSourceNode
        ctx = ctx_factory()
        ctx.input = ctx.conn.sql(
            "SELECT 1 AS id, 'a' AS v UNION ALL SELECT 2, 'b'"
        )
        _run_sink(LocalTableSinkNode, {
            "schema_name": "default", "table_name": "t1", "mode": "replace",
        }, ctx)
        # Source path:
        node = LocalTableSourceNode({"schema_name": "default", "table_name": "t1"})
        rel = node.execute(ctx)
        rows = rel.order("ALL").fetchall()
        assert rows == [(1, "a"), (2, "b")]

    def test_source_missing_table_raises(self, ctx_factory):
        from fpulse.nodes.local_table import LocalTableSourceNode
        ctx = ctx_factory()
        node = LocalTableSourceNode({"schema_name": "default", "table_name": "missing"})
        with pytest.raises(ValueError, match="no such managed table"):
            node.execute(ctx)
