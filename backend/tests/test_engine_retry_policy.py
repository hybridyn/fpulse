"""Pinned tests for E2 retry policy (2026-06-08).

Second milestone from docs/design/executor-maturity-1.2.md. Pure
helpers; no executor wire-in yet (that's E2.1). These tests pin the
decision logic the executor will consult.

Contracts pinned here:
  * Disabled policy returns True from should_retry (no-op - executor's
    existing per-step retry loop is unchanged)
  * Enabled policy short-circuits failures whose class isn't in retry_on
  * Enabled policy stops at max_attempts
  * None failure_class gets exactly ONE retry (conservative)
  * Backoff respects multiplier + cap
  * Schema rejects invalid values (max_attempts=0, bad failure-class names)
  * resolve_workflow_policy tolerates missing/null/bad workflow shapes
"""
from __future__ import annotations

import pytest

from fpulse.engine.failure_class import FailureClass
from fpulse.engine.retry_policy import (
    RetryPolicy,
    backoff_for,
    resolve_workflow_policy,
    should_retry,
)


# ── Schema validation ────────────────────────────────────────────────


class TestSchemaValidation:
    def test_defaults_match_safe_baseline(self):
        p = RetryPolicy()
        assert p.enabled is False
        assert p.max_attempts == 3
        assert p.initial_backoff_seconds == 2.0
        assert p.backoff_multiplier == 2.0
        assert set(p.retry_on) == {"transient", "dependency"}

    def test_max_attempts_must_be_positive(self):
        with pytest.raises(Exception):
            RetryPolicy(max_attempts=0)
        with pytest.raises(Exception):
            RetryPolicy(max_attempts=-1)

    def test_negative_backoff_rejected(self):
        with pytest.raises(Exception):
            RetryPolicy(initial_backoff_seconds=-1.0)
        with pytest.raises(Exception):
            RetryPolicy(backoff_max_seconds=-1.0)

    def test_multiplier_below_one_rejected(self):
        # A multiplier < 1 would SHRINK the delay - almost always a typo
        with pytest.raises(Exception):
            RetryPolicy(backoff_multiplier=0.5)

    def test_multiplier_exactly_one_is_fixed_delay(self):
        p = RetryPolicy(enabled=True, backoff_multiplier=1.0, initial_backoff_seconds=5.0)
        assert backoff_for(p, attempt=1) == 5.0
        assert backoff_for(p, attempt=5) == 5.0  # never grows

    def test_retry_on_with_unknown_class_rejected(self):
        # Typo protection - "transent" should fail loudly, not silently
        # disable retry for everything by being unreachable.
        with pytest.raises(Exception) as exc:
            RetryPolicy(retry_on=["transent"])
        assert "transent" in str(exc.value).lower()

    def test_retry_on_with_known_classes_accepted(self):
        for fc in FailureClass:
            RetryPolicy(retry_on=[fc.value])  # should not raise


# ── should_retry behaviour matrix ────────────────────────────────────


class TestShouldRetryWhenDisabled:
    """A disabled policy MUST return True for every input so the
    executor's existing per-step retry loop runs unchanged. Tests pin
    that no failure-class check is applied when policy.enabled=False."""

    def test_disabled_policy_returns_true_for_anything(self):
        p = RetryPolicy()  # enabled=False
        for fc in FailureClass:
            assert should_retry(p, fc.value, attempt=1) is True
        assert should_retry(p, None, attempt=1) is True
        # Even at high attempt counts (disabled policy doesn't enforce max)
        assert should_retry(p, "transient", attempt=100) is True


class TestShouldRetryWhenEnabled:
    def test_transient_in_default_retry_on(self):
        p = RetryPolicy(enabled=True)
        assert should_retry(p, "transient", attempt=1) is True
        assert should_retry(p, "transient", attempt=2) is True
        assert should_retry(p, "transient", attempt=3) is False  # >= max_attempts

    def test_data_quality_short_circuits(self):
        # data_quality NOT in default retry_on - retry won't change the data
        p = RetryPolicy(enabled=True)
        assert should_retry(p, "data_quality", attempt=1) is False

    def test_user_input_short_circuits(self):
        p = RetryPolicy(enabled=True)
        assert should_retry(p, "user_input", attempt=1) is False

    def test_fatal_short_circuits(self):
        p = RetryPolicy(enabled=True)
        assert should_retry(p, "fatal", attempt=1) is False

    def test_custom_retry_on_list(self):
        # Operator says "only retry transient; never dependency"
        p = RetryPolicy(enabled=True, retry_on=["transient"])
        assert should_retry(p, "transient", attempt=1) is True
        assert should_retry(p, "dependency", attempt=1) is False

    def test_max_attempts_stops_retry(self):
        p = RetryPolicy(enabled=True, max_attempts=2)
        assert should_retry(p, "transient", attempt=1) is True
        assert should_retry(p, "transient", attempt=2) is False

    def test_accepts_enum_value_too(self):
        # Callers may pass either string or FailureClass instance
        p = RetryPolicy(enabled=True)
        assert should_retry(p, FailureClass.TRANSIENT, attempt=1) is True
        assert should_retry(p, FailureClass.DATA_QUALITY, attempt=1) is False

    def test_none_failure_class_gets_one_retry(self):
        # Conservative: if classification failed entirely, give ONE
        # attempt then bail. Worst case: one wasted retry. Better than
        # blanket retry (which would mask real bugs) or blanket skip
        # (which would miss genuinely transient classification failures).
        p = RetryPolicy(enabled=True, max_attempts=5)
        assert should_retry(p, None, attempt=1) is True
        assert should_retry(p, None, attempt=2) is False


# ── Backoff math ────────────────────────────────────────────────────


class TestBackoff:
    def test_first_attempt_returns_initial(self):
        p = RetryPolicy(enabled=True, initial_backoff_seconds=2.0,
                          backoff_multiplier=2.0)
        assert backoff_for(p, attempt=1) == 2.0

    def test_exponential_growth(self):
        p = RetryPolicy(enabled=True, initial_backoff_seconds=1.0,
                          backoff_multiplier=2.0, backoff_max_seconds=999.0)
        assert backoff_for(p, attempt=1) == 1.0   # 1 * 2^0
        assert backoff_for(p, attempt=2) == 2.0   # 1 * 2^1
        assert backoff_for(p, attempt=3) == 4.0   # 1 * 2^2
        assert backoff_for(p, attempt=4) == 8.0

    def test_linear_growth(self):
        # multiplier=1 + initial=5 => fixed 5s delay every retry
        p = RetryPolicy(enabled=True, initial_backoff_seconds=5.0,
                          backoff_multiplier=1.0)
        assert backoff_for(p, attempt=1) == 5.0
        assert backoff_for(p, attempt=10) == 5.0

    def test_cap_enforced(self):
        # Without a cap, attempt=10 with multiplier=2 would be huge
        p = RetryPolicy(enabled=True, initial_backoff_seconds=1.0,
                          backoff_multiplier=2.0, backoff_max_seconds=60.0)
        assert backoff_for(p, attempt=20) == 60.0  # capped, not 1*2^19

    def test_zero_attempt_is_zero_delay(self):
        # Edge case: attempt=0 shouldn't happen in real callers but
        # the helper handles it defensively
        p = RetryPolicy(enabled=True)
        assert backoff_for(p, attempt=0) == 0.0


# ── Resolver ─────────────────────────────────────────────────────────


class TestResolveWorkflowPolicy:
    def test_none_workflow_returns_disabled_default(self):
        p = resolve_workflow_policy(None)
        assert isinstance(p, RetryPolicy)
        assert p.enabled is False

    def test_workflow_without_field_returns_default(self):
        class _Stub:
            pass
        p = resolve_workflow_policy(_Stub())
        assert p.enabled is False

    def test_workflow_with_dict_policy_parses(self):
        class _Stub:
            retry_policy = {"enabled": True, "max_attempts": 5,
                              "retry_on": ["transient"]}
        p = resolve_workflow_policy(_Stub())
        assert p.enabled is True
        assert p.max_attempts == 5
        assert p.retry_on == ["transient"]

    def test_workflow_with_pydantic_policy_passes_through(self):
        class _Stub:
            retry_policy = RetryPolicy(enabled=True, max_attempts=7)
        p = resolve_workflow_policy(_Stub())
        assert p.enabled is True
        assert p.max_attempts == 7

    def test_bad_policy_dict_returns_default_not_raises(self):
        # Robustness: a malformed policy in the IR should fall back to
        # "no policy" rather than crash the executor.
        class _Stub:
            retry_policy = {"max_attempts": -99}  # invalid
        p = resolve_workflow_policy(_Stub())
        assert p.enabled is False


# ── Composition with the failure_class taxonomy ──────────────────────


class TestComposition:
    """The whole point is that should_retry consults the same string
    values failure_class produces. Smoke test the round-trip."""

    @pytest.mark.parametrize("classifier_output,expected_retry", [
        ("transient",     True),
        ("dependency",    True),
        ("data_quality",  False),
        ("user_input",    False),
        ("fatal",         False),
        ("unknown",       False),  # not in default retry_on
    ])
    def test_default_policy_matches_classifier_strings(self, classifier_output, expected_retry):
        p = RetryPolicy(enabled=True)
        assert should_retry(p, classifier_output, attempt=1) is expected_retry
