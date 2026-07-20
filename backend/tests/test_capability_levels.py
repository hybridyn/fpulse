"""Tests for the connector capability-level inspector (T2 2026-05-23).

The capability_levels module computes per-surface registry flags
(declared/form/testable/catalog/source_runtime/sink_runtime/manifest/
certified) by introspecting the live backend. These tests pin the
expected level for high-confidence reference connectors so a future
refactor can't quietly downgrade a type without flagging CI.
"""

from __future__ import annotations

from fpulse.connections import capability_levels as cl
from fpulse.connections.models import CONNECTION_TYPES


def test_all_levels_returns_one_row_per_declared_type():
    rows = cl.all_levels()
    assert len(rows) == len(CONNECTION_TYPES)
    names = {row["type"] for row in rows}
    assert names == set(CONNECTION_TYPES)


def test_every_row_carries_full_level_dict():
    expected_keys = {
        "declared", "form", "testable", "catalog",
        "source_runtime", "sink_runtime", "manifest", "certified",
    }
    for row in cl.all_levels():
        assert set(row["levels"].keys()) == expected_keys, row["type"]
        # declared is True by definition of being in CONNECTION_TYPES.
        assert row["levels"]["declared"] is True, row["type"]


def test_postgresql_is_production_grade():
    """postgresql is the canonical "everything wired" example. If this
    drops below production we've broken something foundational."""
    levels = cl.capability_levels("postgresql")
    assert levels["declared"]
    assert levels["form"]
    assert levels["testable"]
    assert levels["catalog"]
    assert levels["source_runtime"]
    assert levels["sink_runtime"]
    assert cl.maturity_label(levels) in {"production", "certified"}


def test_microsoft_graph_is_at_least_configurable():
    """Microsoft Graph shipped 2026-05-22 with form + tester + catalog +
    source runtime. Any drop signals a regression in the S-phase work."""
    levels = cl.capability_levels("microsoft_graph")
    assert levels["form"]
    assert levels["testable"]
    assert levels["catalog"]
    assert levels["source_runtime"]
    assert cl.maturity_label(levels) in {"production", "certified", "configurable"}


def test_maturity_label_picks_highest_truthful_level():
    """If runtime+form+testable are set, we shouldn't read out as merely
    form_only — the label rolls up to production."""
    levels = {
        "declared": True, "form": True, "testable": True, "catalog": True,
        "source_runtime": True, "sink_runtime": False,
        "manifest": False, "certified": False,
    }
    assert cl.maturity_label(levels) == "production"

    levels["source_runtime"] = False
    assert cl.maturity_label(levels) == "configurable"

    levels["testable"] = False
    assert cl.maturity_label(levels) == "form_only"

    levels["form"] = False
    levels["manifest"] = True
    assert cl.maturity_label(levels) == "manifest_only"


def test_certified_set_is_subset_of_form_types():
    """A connector can't be "certified" without a way to configure it.
    Catches mismatched curation between the two constants."""
    assert cl.CERTIFIED_TYPES.issubset(cl.FRONTEND_FORM_TYPES)


def test_no_form_type_is_missing_from_declared():
    """The frontend form mirror must be a subset of CONNECTION_TYPES —
    otherwise the picker offers a type the backend won't accept."""
    leaked = cl.FRONTEND_FORM_TYPES - set(CONNECTION_TYPES)
    assert not leaked, f"FRONTEND_FORM_TYPES has types not in CONNECTION_TYPES: {leaked}"
