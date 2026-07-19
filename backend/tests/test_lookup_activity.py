"""Lookup *activity* node — 2026-06-11.

Distinct from the Lookup *transformation* (test_lookup_node.py). This node
fetches reference row(s) into $vars for downstream control flow. The tests
pin two things:

  1. Capture semantics — firstRow / rows / count / isEmpty, order_by row
     selection, first-row-only vs multi-row, on_empty fail/empty.
  2. THE consumption path — a value captured into ctx.vars resolves through
     the same expression engine the executor uses per-step, so a downstream
     step's {{ $vars.<name>.firstRow.<col> }} sees the looked-up value.
"""
from __future__ import annotations

import duckdb
import pytest

from fpulse.nodes.base import ExecutionContext
from fpulse.nodes.control_extras import LookupActivityNode
from fpulse.expression.resolver import resolve_expressions


def _ctx() -> ExecutionContext:
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def _seed(ctx: ExecutionContext, step_id: str, sql: str):
    rel = ctx.conn.sql(sql)
    ctx.set_result(step_id, rel)
    return rel


def _watermark_src(ctx):
    return _seed(ctx, "src", (
        "SELECT * FROM (VALUES "
        "(1, TIMESTAMP '2026-01-01 00:00:00'), "
        "(2, TIMESTAMP '2026-03-01 00:00:00'), "
        "(3, TIMESTAMP '2026-02-01 00:00:00')"
        ") AS t(id, updated_at)"
    ))


def test_first_row_only_with_order_by_picks_the_watermark():
    ctx = _ctx()
    _watermark_src(ctx)
    node = LookupActivityNode(params={
        "_input_step_ids": ["src"], "_step_id": "lk",
        "output_var": "wm", "first_row_only": True, "order_by": "updated_at DESC",
    })
    rel = node.execute(ctx)

    # Captured into ctx.vars in the firstRow/count/isEmpty shape
    cap = ctx.vars["wm"]
    assert cap["count"] == 1
    assert cap["isEmpty"] is False
    assert cap["firstRow"]["id"] == 2          # newest row (2026-03)
    assert str(cap["firstRow"]["updated_at"]).startswith("2026-03-01")
    # Relation also returns the row (so $('node').first() works)
    assert len(rel.fetchall()) == 1


def test_consumption_path_downstream_expression_resolves_captured_value():
    """The whole point: a captured var resolves through the SAME engine the
    executor runs per-step (executor.py passes vars_=ctx.vars)."""
    ctx = _ctx()
    _watermark_src(ctx)
    LookupActivityNode(params={
        "_input_step_ids": ["src"], "_step_id": "lk",
        "output_var": "wm", "first_row_only": True, "order_by": "updated_at DESC",
    }).execute(ctx)

    # Simulate a downstream step whose param references the captured var.
    downstream_params = {
        "condition": "created_at > '{{ $vars.wm.firstRow.updated_at }}'",
        "count_gate": "{{ $vars.wm.count }}",
        "is_empty": "{{ $vars.wm.isEmpty }}",
    }
    resolved = resolve_expressions(
        downstream_params, ctx_results=ctx.results_as_rows(),
        node_labels={}, vars_=ctx.vars,
    )
    assert "2026-03-01" in resolved["condition"]
    assert resolved["count_gate"] == 1          # whole-string expr → typed int
    assert resolved["is_empty"] is False


def test_node_ref_consumption_also_works():
    """$('Lookup (Activity)').first().col works because the node returns rows."""
    ctx = _ctx()
    _watermark_src(ctx)
    rel = LookupActivityNode(params={
        "_input_step_ids": ["src"], "_step_id": "lk",
        "output_var": "wm", "first_row_only": True, "order_by": "updated_at DESC",
    }).execute(ctx)
    ctx.set_result("lk", rel)
    resolved = resolve_expressions(
        {"v": "{{ $('Lookup (Activity)').first().id }}"},
        ctx_results=ctx.results_as_rows(),
        node_labels={"lk": "Lookup (Activity)"},
        vars_=ctx.vars,
    )
    assert resolved["v"] == 2


def test_multi_row_capture_respects_max_rows():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT * FROM range(100) AS t(n)")
    node = LookupActivityNode(params={
        "_input_step_ids": ["src"], "_step_id": "lk",
        "output_var": "ids", "first_row_only": False, "max_rows": 10,
    })
    node.execute(ctx)
    cap = ctx.vars["ids"]
    assert cap["count"] == 10
    assert len(cap["rows"]) == 10


def test_filter_selects_subset():
    ctx = _ctx()
    _seed(ctx, "src", (
        "SELECT * FROM (VALUES (1,'active'),(2,'inactive'),(3,'active')) AS t(id, status)"
    ))
    node = LookupActivityNode(params={
        "_input_step_ids": ["src"], "_step_id": "lk",
        "output_var": "act", "first_row_only": False, "filter": "status = 'active'",
    })
    node.execute(ctx)
    assert ctx.vars["act"]["count"] == 2


def test_on_empty_fail_vs_empty():
    ctx = _ctx()
    _seed(ctx, "src", "SELECT * FROM (VALUES (1,'x')) AS t(id, name) WHERE false")

    with pytest.raises(ValueError, match="returned 0 rows"):
        LookupActivityNode(params={
            "_input_step_ids": ["src"], "_step_id": "lk",
            "output_var": "e", "on_empty": "fail",
        }).execute(ctx)

    LookupActivityNode(params={
        "_input_step_ids": ["src"], "_step_id": "lk2",
        "output_var": "e", "on_empty": "empty",
    }).execute(ctx)
    cap = ctx.vars["e"]
    assert cap["count"] == 0 and cap["isEmpty"] is True and cap["firstRow"] == {}


def test_missing_input_raises_clearly():
    ctx = _ctx()
    with pytest.raises(ValueError, match="needs one input"):
        LookupActivityNode(params={"_input_step_ids": [], "output_var": "x"}).execute(ctx)


# ── #15: self-contained connection + query mode ─────────────────────

def test_connection_mode_fetches_without_upstream(monkeypatch):
    """With a connection + query the activity reads its OWN data — no upstream
    wiring — a self-contained Lookup activity. The driver matrix is stubbed."""
    ctx = _ctx()
    seen = {}

    def fake_run(cid, sql, timeout=60):
        seen["cid"] = cid
        seen["sql"] = sql
        return ([{"watermark": "2026-03-01"}], -1)

    monkeypatch.setattr("fpulse.nodes.control_extras._run_connection_sql", fake_run)
    LookupActivityNode(params={
        "_step_id": "lk", "output_var": "wm",
        "source_mode": "connection", "connection_id": "prod-pg",
        "query": "SELECT MAX(updated_at) AS watermark FROM orders",
        "first_row_only": True,
    }).execute(ctx)  # NOTE: no _input_step_ids — self-contained

    assert seen["cid"] == "prod-pg"
    cap = ctx.vars["wm"]
    assert cap["count"] == 1
    assert cap["firstRow"]["watermark"] == "2026-03-01"
    assert cap["value"] == cap["rows"]          # `value` alias


def test_connection_mode_requires_query(monkeypatch):
    ctx = _ctx()
    monkeypatch.setattr("fpulse.nodes.control_extras._run_connection_sql", lambda *a, **k: ([], 0))
    with pytest.raises(ValueError, match="needs a 'query'"):
        LookupActivityNode(params={
            "_step_id": "lk", "output_var": "x",
            "source_mode": "connection", "connection_id": "c",
        }).execute(ctx)


def test_upstream_mode_still_default(monkeypatch):
    """No connection → reads the wired upstream relation (back-compat)."""
    ctx = _ctx()
    # _run_connection_sql must NOT be called in upstream mode.
    monkeypatch.setattr(
        "fpulse.nodes.control_extras._run_connection_sql",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call connection")),
    )
    _seed(ctx, "src", "SELECT 'A' AS tag")
    LookupActivityNode(params={
        "_input_step_ids": ["src"], "_step_id": "lk", "output_var": "v",
    }).execute(ctx)
    assert ctx.vars["v"]["firstRow"]["tag"] == "A"


def test_two_lookups_in_one_run_dont_clobber_each_other():
    """Per-step temp tables keep each returned relation stable."""
    ctx = _ctx()
    _seed(ctx, "a", "SELECT 'A' AS tag")
    _seed(ctx, "b", "SELECT 'B' AS tag")
    rel_a = LookupActivityNode(params={
        "_input_step_ids": ["a"], "_step_id": "la", "output_var": "va",
    }).execute(ctx)
    rel_b = LookupActivityNode(params={
        "_input_step_ids": ["b"], "_step_id": "lb", "output_var": "vb",
    }).execute(ctx)
    # rel_a must still read 'A' after rel_b ran (no shared-view clobber)
    assert rel_a.fetchall()[0][0] == "A"
    assert rel_b.fetchall()[0][0] == "B"
