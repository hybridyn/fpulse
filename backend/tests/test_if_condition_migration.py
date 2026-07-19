"""If Condition branch-port back-compat migration (2026-06-15 control-flow alignment).

`if_condition` became a true/false brancher (emits `_split_output`). Legacy
edges carry the schema-default `from_port='output'`, which the router would
leave unrouted — leaking both branches + the tag column. The migration remaps
those legacy edges onto the 'true' branch so old keep-matching-rows pipelines
behave exactly as before. Explicit branch ports are left untouched.
"""
from __future__ import annotations

from fpulse.ir.migrations import migrate_legacy_node_types


def _wf(connections):
    return {
        "id": "w1",
        "steps": [
            {"id": "if1", "type": "if_condition", "params": {"condition": "x > 0"}},
            {"id": "snk", "type": "destination", "params": {}},
        ],
        "connections": connections,
    }


def test_legacy_output_edge_remapped_to_true():
    wf = _wf([{"from_step": "if1", "to_step": "snk", "from_port": "output", "to_port": "input"}])
    out = migrate_legacy_node_types(wf)
    assert out["connections"][0]["from_port"] == "true"


def test_missing_from_port_treated_as_legacy():
    wf = _wf([{"from_step": "if1", "to_step": "snk", "to_port": "input"}])
    out = migrate_legacy_node_types(wf)
    assert out["connections"][0]["from_port"] == "true"


def test_explicit_branch_ports_left_alone():
    wf = _wf([
        {"from_step": "if1", "to_step": "a", "from_port": "true", "to_port": "input"},
        {"from_step": "if1", "to_step": "b", "from_port": "false", "to_port": "input"},
    ])
    out = migrate_legacy_node_types(wf)
    assert [c["from_port"] for c in out["connections"]] == ["true", "false"]


def test_non_if_edges_untouched():
    wf = {
        "id": "w2",
        "steps": [
            {"id": "src", "type": "source", "params": {}},
            {"id": "flt", "type": "filter", "params": {}},
        ],
        "connections": [{"from_step": "src", "to_step": "flt", "from_port": "output", "to_port": "input"}],
    }
    out = migrate_legacy_node_types(wf)
    assert out["connections"][0]["from_port"] == "output"
