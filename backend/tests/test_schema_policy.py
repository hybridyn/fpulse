"""Unit tests for ``intelligence.schema_policy.evaluate_policy``.

The evaluator is the single decision point every sink calls before
issuing ALTER DDL. These tests pin the 4-policy × 4-change-kind grid
the module docstring promises — one regression here ripples through
every sink that's wired to the new path.

Each test is shape-only: no DB, no DuckDB. ``evaluate_policy`` takes
column-dict lists in, returns a ``PolicyDecision`` out. That's the
contract; everything else is implementation detail.
"""

from __future__ import annotations

import pytest

from fpulse.intelligence.schema_policy import (
    DEFAULT_POLICY,
    SchemaPolicy,
    SchemaDriftError,
    evaluate_policy,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _col(name: str, type_: str = "VARCHAR", nullable: bool = True) -> dict:
    return {"name": name, "type": type_, "nullable": nullable}


CUSTOMERS_V1 = [_col("id", "INTEGER", False), _col("name", "VARCHAR")]
CUSTOMERS_V2_ADD = [*CUSTOMERS_V1, _col("email", "VARCHAR")]
CUSTOMERS_V2_DROP = [_col("id", "INTEGER", False)]              # name dropped
CUSTOMERS_V2_WIDEN = [_col("id", "BIGINT", False), _col("name", "VARCHAR")]
CUSTOMERS_V2_NARROW = [_col("id", "SMALLINT", False), _col("name", "VARCHAR")]


# ── First-write short-circuit ──────────────────────────────────────────


def test_first_write_no_existing_schema_accepts_any_policy():
    """No existing schema → policy short-circuits to ok regardless."""
    for policy in SchemaPolicy:
        decision = evaluate_policy([], CUSTOMERS_V1, policy)
        assert decision.ok is True
        assert decision.has_drift is False
        assert decision.changes == []


def test_default_policy_is_add_columns():
    """``None`` policy resolves to the documented default."""
    decision = evaluate_policy(CUSTOMERS_V1, CUSTOMERS_V2_ADD, None)
    assert decision.policy is DEFAULT_POLICY
    assert decision.policy is SchemaPolicy.ADD_COLUMNS


# ── strict ──────────────────────────────────────────────────────────────


def test_strict_rejects_any_change():
    """Even a pure additive change fails under strict — that's the point."""
    decision = evaluate_policy(CUSTOMERS_V1, CUSTOMERS_V2_ADD, SchemaPolicy.STRICT)
    assert decision.ok is False
    assert decision.has_drift is True
    assert "strict" in (decision.rejection_reason or "").lower()
    with pytest.raises(SchemaDriftError):
        decision.raise_if_rejected()


def test_strict_passes_no_change():
    """Identical schemas → ok under strict (nothing to refuse)."""
    decision = evaluate_policy(CUSTOMERS_V1, CUSTOMERS_V1, SchemaPolicy.STRICT)
    assert decision.ok is True
    assert decision.has_drift is False


# ── add_columns ─────────────────────────────────────────────────────────


def test_add_columns_accepts_pure_add():
    decision = evaluate_policy(CUSTOMERS_V1, CUSTOMERS_V2_ADD, SchemaPolicy.ADD_COLUMNS)
    assert decision.ok is True
    assert len(decision.adds) == 1
    assert decision.adds[0].column == "email"
    assert decision.adds[0].policy_action == "apply_add"


def test_add_columns_rejects_widening():
    decision = evaluate_policy(CUSTOMERS_V1, CUSTOMERS_V2_WIDEN, SchemaPolicy.ADD_COLUMNS)
    # Even safe widening fails under add_columns — that's compatible's job.
    assert decision.ok is False
    assert any(c.kind == "type_changed" for c in decision.changes)


def test_add_columns_rejects_drop():
    decision = evaluate_policy(CUSTOMERS_V1, CUSTOMERS_V2_DROP, SchemaPolicy.ADD_COLUMNS)
    assert decision.ok is False
    assert any(c.kind == "dropped" and c.column == "name" for c in decision.changes)


# ── compatible ──────────────────────────────────────────────────────────


def test_compatible_accepts_add_and_widening():
    """Widening (INT → BIGINT) + new column both apply."""
    incoming = [*CUSTOMERS_V2_WIDEN, _col("email", "VARCHAR")]
    decision = evaluate_policy(CUSTOMERS_V1, incoming, SchemaPolicy.COMPATIBLE)
    assert decision.ok is True
    assert any(c.policy_action == "apply_widen" for c in decision.changes)
    assert any(c.policy_action == "apply_add" for c in decision.changes)


def test_compatible_rejects_narrowing():
    """BIGINT → SMALLINT narrows; compatible refuses."""
    decision = evaluate_policy(
        [_col("id", "BIGINT", False)],
        [_col("id", "SMALLINT", False)],
        SchemaPolicy.COMPATIBLE,
    )
    assert decision.ok is False
    assert "narrowing" in (decision.rejection_reason or "").lower()


def test_compatible_rejects_drop():
    decision = evaluate_policy(CUSTOMERS_V1, CUSTOMERS_V2_DROP, SchemaPolicy.COMPATIBLE)
    assert decision.ok is False


# ── allow_all_with_warning ──────────────────────────────────────────────


def test_allow_all_applies_drop_and_emits_warning_severity():
    decision = evaluate_policy(
        CUSTOMERS_V1, CUSTOMERS_V2_DROP, SchemaPolicy.ALLOW_ALL_WITH_WARNING,
    )
    assert decision.ok is True
    assert decision.severity == "warning"
    assert any(c.policy_action == "apply_force" for c in decision.changes)


def test_allow_all_applies_narrowing_with_warning():
    decision = evaluate_policy(
        [_col("id", "BIGINT", False)],
        [_col("id", "SMALLINT", False)],
        SchemaPolicy.ALLOW_ALL_WITH_WARNING,
    )
    assert decision.ok is True
    # Narrowing isn't a strict widening, so it falls into apply_force.
    assert any(c.policy_action == "apply_force" for c in decision.changes)
    assert decision.severity in ("warning", "critical")


# ── String-form policy + alias resolution ──────────────────────────────


def test_string_policy_arg_is_accepted():
    """The IR carries policies as plain strings — handle both forms."""
    decision = evaluate_policy(CUSTOMERS_V1, CUSTOMERS_V2_ADD, "add_columns")
    assert decision.policy is SchemaPolicy.ADD_COLUMNS
    assert decision.ok is True


def test_unknown_policy_string_falls_back_to_default():
    """A typo'd policy in saved workflow JSON must NOT crash a run."""
    decision = evaluate_policy(CUSTOMERS_V1, CUSTOMERS_V2_ADD, "nonsense_value")
    assert decision.policy is DEFAULT_POLICY


def test_aliased_types_dont_trigger_drift():
    """VARCHAR vs STRING vs TEXT are aliases; not drift."""
    existing = [_col("name", "VARCHAR")]
    incoming = [_col("name", "STRING")]
    decision = evaluate_policy(existing, incoming, SchemaPolicy.STRICT)
    assert decision.ok is True
    assert decision.has_drift is False


# ── Decision summary shape (used by event payload + history) ────────────


def test_summary_contains_adds_drops_and_type_changes():
    incoming = [
        *CUSTOMERS_V2_WIDEN,             # widens id INT → BIGINT
        _col("email", "VARCHAR"),        # adds email
    ]
    # Original has an extra column 'archived' that the new schema drops.
    existing = [*CUSTOMERS_V1, _col("archived", "BOOLEAN")]
    decision = evaluate_policy(existing, incoming, SchemaPolicy.ALLOW_ALL_WITH_WARNING)
    s = decision.to_summary()
    assert "email" in s["added"]
    assert "archived" in s["dropped"]
    assert any(t["column"] == "id" for t in s["type_changed"])
    assert s["policy"] == SchemaPolicy.ALLOW_ALL_WITH_WARNING.value


def test_raise_if_rejected_is_no_op_when_ok():
    decision = evaluate_policy(CUSTOMERS_V1, CUSTOMERS_V2_ADD, SchemaPolicy.ADD_COLUMNS)
    # Must not raise.
    decision.raise_if_rejected()
