"""Cast + inference policy gate tests.

Locks the per-policy verdict matrix the runtime relies on to decide
whether a pipeline plan proceeds, warns, or fails outright.
"""

from __future__ import annotations

import pytest

from fpulse.types import (
    CanonicalSchema,
    CastPlanElement,
    CastPolicy,
    CastSafety,
    Evidence,
    FPField,
    FPType,
    InferencePolicy,
    gate_cast_plan,
    gate_inferred_schema,
)


def _plan_element(safety: CastSafety, name: str = "c") -> CastPlanElement:
    return CastPlanElement(
        source_column=name,
        target_column=name,
        target_native_type="INTEGER",
        safety=safety,
        reason=None if safety == CastSafety.SAFE else "narrowing",
    )


# ── CastPolicy verdict matrix ──

class TestCastPolicySafe:
    """SAFE policy: only SAFE casts allowed; everything else blocked."""

    def test_safe_passes(self):
        verdict = gate_cast_plan([_plan_element(CastSafety.SAFE)], CastPolicy.SAFE)
        assert len(verdict.allowed) == 1
        assert verdict.blocked == []

    def test_semantic_lossy_blocks(self):
        verdict = gate_cast_plan(
            [_plan_element(CastSafety.SEMANTIC_LOSSY)], CastPolicy.SAFE,
        )
        assert verdict.is_blocked

    def test_lossy_blocks(self):
        verdict = gate_cast_plan([_plan_element(CastSafety.LOSSY)], CastPolicy.SAFE)
        assert verdict.is_blocked

    def test_impossible_blocks(self):
        verdict = gate_cast_plan(
            [_plan_element(CastSafety.IMPOSSIBLE)], CastPolicy.SAFE,
        )
        assert verdict.is_blocked


class TestCastPolicyCoerce:
    """COERCE policy: SAFE allowed, SEMANTIC_LOSSY warned, LOSSY/IMPOSSIBLE blocked."""

    def test_safe_allowed(self):
        verdict = gate_cast_plan([_plan_element(CastSafety.SAFE)], CastPolicy.COERCE)
        assert verdict.allowed and not verdict.warnings

    def test_semantic_lossy_warns_but_allows(self):
        verdict = gate_cast_plan(
            [_plan_element(CastSafety.SEMANTIC_LOSSY)], CastPolicy.COERCE,
        )
        assert len(verdict.allowed) == 1
        assert len(verdict.warnings) == 1
        assert not verdict.is_blocked

    def test_lossy_blocks(self):
        verdict = gate_cast_plan(
            [_plan_element(CastSafety.LOSSY)], CastPolicy.COERCE,
        )
        assert verdict.is_blocked

    def test_impossible_blocks(self):
        verdict = gate_cast_plan(
            [_plan_element(CastSafety.IMPOSSIBLE)], CastPolicy.COERCE,
        )
        assert verdict.is_blocked


class TestCastPolicyTruncate:
    """TRUNCATE: only IMPOSSIBLE blocks; LOSSY allowed with warning."""

    def test_lossy_warned_but_allowed(self):
        verdict = gate_cast_plan(
            [_plan_element(CastSafety.LOSSY)], CastPolicy.TRUNCATE,
        )
        assert verdict.allowed and verdict.warnings
        assert not verdict.is_blocked

    def test_impossible_still_blocks(self):
        verdict = gate_cast_plan(
            [_plan_element(CastSafety.IMPOSSIBLE)], CastPolicy.TRUNCATE,
        )
        assert verdict.is_blocked


class TestCastPolicyStrict:
    """STRICT: any non-SAFE is blocked."""

    def test_only_safe_passes(self):
        verdict = gate_cast_plan([_plan_element(CastSafety.SAFE)], CastPolicy.STRICT)
        assert verdict.allowed

    def test_semantic_lossy_blocked_strict(self):
        verdict = gate_cast_plan(
            [_plan_element(CastSafety.SEMANTIC_LOSSY)], CastPolicy.STRICT,
        )
        assert verdict.is_blocked


class TestCastPolicyLearn:
    """LEARN: everything proceeds; non-SAFE goes into warnings."""

    def test_impossible_warned_not_blocked(self):
        verdict = gate_cast_plan(
            [_plan_element(CastSafety.IMPOSSIBLE)], CastPolicy.LEARN,
        )
        assert verdict.allowed
        assert verdict.warnings
        assert not verdict.is_blocked

    def test_safe_no_warning(self):
        verdict = gate_cast_plan([_plan_element(CastSafety.SAFE)], CastPolicy.LEARN)
        assert verdict.allowed
        assert not verdict.warnings


# ── Mixed-plan aggregation ──

class TestPlanAggregation:
    def test_partial_block_separates_allowed_and_blocked(self):
        plan = [
            _plan_element(CastSafety.SAFE, "a"),
            _plan_element(CastSafety.LOSSY, "b"),
            _plan_element(CastSafety.SAFE, "c"),
        ]
        verdict = gate_cast_plan(plan, CastPolicy.SAFE)
        assert [e.source_column for e in verdict.allowed] == ["a", "c"]
        assert [e.source_column for e in verdict.blocked] == ["b"]

    def test_truncate_lets_lossy_through_but_warns(self):
        plan = [
            _plan_element(CastSafety.SAFE, "a"),
            _plan_element(CastSafety.LOSSY, "b"),
        ]
        verdict = gate_cast_plan(plan, CastPolicy.TRUNCATE)
        assert len(verdict.allowed) == 2
        assert {e.source_column for e in verdict.warnings} == {"b"}


# ── InferencePolicy ──

def _f(name: str, t: FPType, evidence: Evidence = Evidence.INFERRED) -> FPField:
    return FPField(name=name, type=t, evidence=evidence)


class TestInferencePolicy:
    def test_auto_lets_unknowns_pass(self):
        schema = CanonicalSchema(fields=[
            _f("a", FPType.STRING),
            _f("b", FPType.UNKNOWN),
        ])
        verdict = gate_inferred_schema(schema, InferencePolicy.AUTO)
        assert verdict.ok
        assert verdict.blocked == []

    def test_strict_blocks_on_any_unknown(self):
        schema = CanonicalSchema(fields=[
            _f("a", FPType.STRING),
            _f("b", FPType.UNKNOWN),
        ])
        verdict = gate_inferred_schema(schema, InferencePolicy.STRICT)
        assert not verdict.ok
        assert verdict.blocked == ["b"]

    def test_strict_passes_when_all_resolved(self):
        schema = CanonicalSchema(fields=[
            _f("a", FPType.STRING),
            _f("b", FPType.INTEGER),
        ])
        verdict = gate_inferred_schema(schema, InferencePolicy.STRICT)
        assert verdict.ok

    def test_coerce_rewrites_unknown_to_string(self):
        schema = CanonicalSchema(fields=[
            _f("a", FPType.STRING),
            _f("b", FPType.UNKNOWN),
        ])
        verdict = gate_inferred_schema(schema, InferencePolicy.COERCE)
        assert verdict.ok
        assert verdict.coerced == ["b"]
        b = schema.by_name("b")
        assert b is not None
        assert b.type == FPType.STRING
        assert b.evidence == Evidence.COERCED

    def test_manual_blocks_non_manual_evidence(self):
        schema = CanonicalSchema(fields=[
            _f("a", FPType.STRING, evidence=Evidence.MANUAL),
            _f("b", FPType.STRING, evidence=Evidence.ADVERTISED),
            _f("c", FPType.STRING, evidence=Evidence.INFERRED),
        ])
        verdict = gate_inferred_schema(schema, InferencePolicy.MANUAL)
        assert not verdict.ok
        # b + c are not MANUAL.
        assert set(verdict.blocked) == {"b", "c"}

    def test_learn_lets_unknowns_through(self):
        schema = CanonicalSchema(fields=[
            _f("b", FPType.UNKNOWN),
        ])
        verdict = gate_inferred_schema(schema, InferencePolicy.LEARN)
        assert verdict.ok
