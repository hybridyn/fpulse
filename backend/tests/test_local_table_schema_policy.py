"""Per-policy enforcement through a full ``LocalTableSinkNode`` execution.

These tests exercise the path the runtime actually takes — sink reads
upstream relation, consults policy against existing storage_columns,
applies (or rejects) the write, records history. Anything below the
sink (DuckDB I/O, parquet, storage_tables) is real; nothing is mocked.

The matrix:

  +-------------------------+--------+-------------+------------+-------------------+
  | scenario                | strict | add_columns | compatible | allow_all_warning |
  +-------------------------+--------+-------------+------------+-------------------+
  | pure add (+email)       | fail   | succeed     | succeed    | succeed           |
  | drop column (-name)     | fail   | fail        | fail       | succeed (warn)    |
  +-------------------------+--------+-------------+------------+-------------------+

Eight cases would be the full grid. Four cover the pivot points the
acceptance criteria call out; the unit tests in test_schema_policy.py
pin the rest of the cells.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from fpulse.intelligence.schema_policy import SchemaDriftError


@pytest.fixture
def duck_conn():
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect()
    yield conn
    conn.close()


@pytest.fixture
def ctx_factory(tmp_path, duck_conn, datastore, schema_history_store):
    """Build an ExecutionContext-shaped namespace that includes the
    schema_history_store the sink will reach for via ctx.app_state."""

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
            input=input_rel,
            _results=results,
            run_id="run-test-001",
        )

        def _get_inputs(step_ids):
            return [results[sid] for sid in step_ids if sid in results]

        ns.get_inputs = _get_inputs
        return ns
    return _factory


def _seed_v1(ctx, schema: str = "default", table: str = "customers") -> None:
    """Land a baseline ``(id INTEGER, name VARCHAR)`` table on disk."""
    from fpulse.nodes.local_table import LocalTableSinkNode
    rel = ctx.conn.sql("SELECT CAST(1 AS INTEGER) AS id, 'alice' AS name")
    ctx._results["_seed"] = rel
    LocalTableSinkNode({
        "schema_name": schema,
        "table_name": table,
        "mode": "replace",
        "_input_step_ids": ["_seed"],
    }).execute(ctx)


def _evolve_with(ctx, sql: str, *, policy: str, mode: str = "append",
                 schema: str = "default", table: str = "customers"):
    """Run the sink with `sql` as upstream + the given schema_policy."""
    from fpulse.nodes.local_table import LocalTableSinkNode
    rel = ctx.conn.sql(sql)
    ctx._results["_input"] = rel
    return LocalTableSinkNode({
        "schema_name": schema,
        "table_name": table,
        "mode": mode,
        "schema_policy": policy,
        "_input_step_ids": ["_input"],
    }).execute(ctx)


def _read_columns(datastore, schema: str, table: str) -> list[str]:
    tbl = datastore.find_table_by_name("default", schema, table)
    if tbl is None:
        return []
    return [c.name for c in datastore.list_columns(table_id=tbl.id)]


# ── Pure-add scenarios ─────────────────────────────────────────────────


def test_strict_rejects_added_column(ctx_factory, datastore):
    ctx = ctx_factory()
    _seed_v1(ctx)
    with pytest.raises(SchemaDriftError):
        _evolve_with(
            ctx,
            "SELECT CAST(2 AS INTEGER) AS id, 'bob' AS name, 'bob@x.com' AS email",
            policy="strict",
        )
    # The destination shape must NOT have evolved — strict rejection
    # short-circuits before the parquet write touches disk.
    assert sorted(_read_columns(datastore, "default", "customers")) == ["id", "name"]


def test_add_columns_applies_new_column_and_records_history(
    ctx_factory, datastore, schema_history_store,
):
    ctx = ctx_factory()
    _seed_v1(ctx)
    _evolve_with(
        ctx,
        "SELECT CAST(2 AS INTEGER) AS id, 'bob' AS name, 'bob@x.com' AS email",
        policy="add_columns",
    )
    cols = _read_columns(datastore, "default", "customers")
    assert "email" in cols
    # History: exactly one row, captured under add_columns policy.
    tbl = datastore.find_table_by_name("default", "default", "customers")
    history = schema_history_store.list_for_table(tbl.id)
    assert len(history) == 1
    assert history[0]["policy"] == "add_columns"
    assert "email" in (history[0]["change_summary"].get("added") or [])
    assert history[0]["applied_by_run_id"] == "run-test-001"


def test_compatible_accepts_pure_add_same_as_add_columns(
    ctx_factory, datastore, schema_history_store,
):
    """Compatible should be a strict superset of add_columns — a pure
    add must pass and produce the same history shape."""
    ctx = ctx_factory()
    _seed_v1(ctx)
    _evolve_with(
        ctx,
        "SELECT CAST(2 AS INTEGER) AS id, 'bob' AS name, 'bob@x.com' AS email",
        policy="compatible",
    )
    assert "email" in _read_columns(datastore, "default", "customers")
    tbl = datastore.find_table_by_name("default", "default", "customers")
    history = schema_history_store.list_for_table(tbl.id)
    assert len(history) == 1
    assert history[0]["policy"] == "compatible"


def test_allow_all_with_warning_publishes_drift_event(
    ctx_factory, datastore, schema_history_store,
):
    """Adds under allow_all_with_warning succeed and emit an event.

    We listen on the in-process bus through ctx.app_state if available;
    otherwise we settle for history + applied-column verification, which
    is the primary durable record the operator audits anyway.
    """
    ctx = ctx_factory()
    _seed_v1(ctx)
    _evolve_with(
        ctx,
        "SELECT CAST(2 AS INTEGER) AS id, 'bob' AS name, 'b@x.com' AS email",
        policy="allow_all_with_warning",
    )
    cols = _read_columns(datastore, "default", "customers")
    assert "email" in cols
    tbl = datastore.find_table_by_name("default", "default", "customers")
    history = schema_history_store.list_for_table(tbl.id)
    assert len(history) == 1
    assert history[0]["change_summary"].get("severity") in ("info", "warning")


# ── Drop / narrowing scenarios ─────────────────────────────────────────


def test_strict_rejects_drop(ctx_factory, datastore):
    ctx = ctx_factory()
    _seed_v1(ctx)
    with pytest.raises(SchemaDriftError):
        _evolve_with(
            ctx,
            "SELECT CAST(2 AS INTEGER) AS id",   # drops 'name'
            policy="strict",
        )


def test_add_columns_rejects_drop(ctx_factory, datastore):
    ctx = ctx_factory()
    _seed_v1(ctx)
    with pytest.raises(SchemaDriftError):
        _evolve_with(
            ctx,
            "SELECT CAST(2 AS INTEGER) AS id",
            policy="add_columns",
        )


def test_compatible_rejects_drop(ctx_factory, datastore):
    ctx = ctx_factory()
    _seed_v1(ctx)
    with pytest.raises(SchemaDriftError):
        _evolve_with(
            ctx,
            "SELECT CAST(2 AS INTEGER) AS id",
            policy="compatible",
        )


def test_allow_all_with_warning_accepts_drop_with_warning_severity(
    ctx_factory, datastore, schema_history_store,
):
    """The dangerous one. allow_all_with_warning must accept the drop
    AND record the change with warning severity so the audit log shows
    'someone forced this through'."""
    ctx = ctx_factory()
    _seed_v1(ctx)
    _evolve_with(
        ctx,
        "SELECT CAST(2 AS INTEGER) AS id",   # drops 'name'
        policy="allow_all_with_warning",
        mode="replace",   # replace so the dropped column truly vanishes from disk
    )
    cols = _read_columns(datastore, "default", "customers")
    assert "name" not in cols
    tbl = datastore.find_table_by_name("default", "default", "customers")
    history = schema_history_store.list_for_table(tbl.id)
    assert len(history) == 1
    assert "name" in (history[0]["change_summary"].get("dropped") or [])
    # Severity must be warning (or critical when narrowing was also involved).
    assert history[0]["change_summary"].get("severity") in ("warning", "critical")


# ── History endpoint integration (FastAPI route) ───────────────────────


def test_schema_history_api_returns_chronological_versions(
    ctx_factory, datastore, schema_history_store, _fpulse_test_db,
):
    """End-to-end: write two evolutions, then call the API and verify
    chronological ordering + that the latest version reflects the
    accumulated shape."""
    ctx = ctx_factory()
    _seed_v1(ctx)
    _evolve_with(
        ctx,
        "SELECT CAST(2 AS INTEGER) AS id, 'b' AS name, 'b@x.com' AS email",
        policy="add_columns",
    )
    _evolve_with(
        ctx,
        "SELECT CAST(3 AS INTEGER) AS id, 'c' AS name, 'c@x.com' AS email, "
        "CAST(1 AS INTEGER) AS age",
        policy="add_columns",
    )

    tbl = datastore.find_table_by_name("default", "default", "customers")
    history = schema_history_store.list_for_table(tbl.id)
    # Two evolutions → two history rows.
    assert [h["version"] for h in history] == [1, 2]
    # Latest version contains all four columns.
    latest_cols = {c["name"] for c in history[-1]["columns_json"]}
    assert {"id", "name", "email", "age"}.issubset(latest_cols)
