"""Retry policy that consults FailureClass (2026-06-08, E2 of executor-maturity-1.2).

E1 shipped the FailureClass taxonomy: every failed step gets stamped
with a classification ("is this retryable in principle?"). E1.1 wired
the executor to populate it on StepRunResult.failure_class.

This module ships the next layer: a Pydantic model expressing
"retry this kind of failure up to N attempts with backoff B" plus
two pure helpers (`should_retry()` + `backoff_for()`) that the
executor's retry loop can consult without depending on the existing
per-step retry implementation.

# Design intent

The executor today already has per-step retry config
(`_settings.max_retries / retry_strategy / retry_delay_ms / on_error`).
That covers the operator's per-node "this node is flaky, retry 3x"
use case.

This module covers a different question: **"given that retry IS
configured, should THIS particular failure be retried?"**

Example:
  * Per-step retry says "max_retries=3"
  * The first attempt fails with `failure_class=data_quality`
    (null in a NOT-NULL column)
  * Without this module, the executor blindly retries 3x - wasting
    time, because the data won't change between attempts
  * With this module, the executor checks `policy.should_retry(
      "data_quality", attempt=1)` -> False -> skip remaining attempts,
    surface the failure immediately

The policy is intentionally separate from the per-step retry config
so:
  * Workflows that don't set a policy keep current behaviour exactly
    (the helpers default to "retry everything", same as today)
  * The integration point in the executor is a single new check that
    can be wrapped in a try/except - never breaks existing tests if
    something goes wrong

# Integration point (E2.1, deferred)

In `executor.py:_execute_step` inside the `except Exception as e:`
block at ~line 1037:

    last_error = e
    + # E2.1 - consult retry_policy before the per-step retry loop
    + #         decides to schedule another attempt.
    + from fpulse.engine.failure_class import classify_error as _fc
    + from fpulse.engine.retry_policy import resolve_workflow_policy
    + policy = resolve_workflow_policy(workflow)
    + fclass = _fc(e)
    + if not policy.should_retry(fclass.value, attempt=attempt):
    +     break  # short-circuit to the existing error-return path
    if attempt >= attempts_total:
        break

That's ~5 lines of additive change at a single well-defined site.
Tests for the executor wire-in will mirror the L1.1 / E1.1 / B1.1
shape: a regression-guard test that greps the executor source for
the call site + an integration test asserting the policy actually
short-circuits when failure_class is excluded.
"""
from __future__ import annotations

from typing import Iterable

from pydantic import BaseModel, Field, field_validator

from .failure_class import FailureClass, retry_advisable


# ── Defaults the model uses when fields are omitted ─────────────────


# "Retry everything" - matches current executor behaviour. Setting
# `enabled=False` (the default) short-circuits the helpers so any
# pre-policy code path keeps working unchanged.
_DEFAULT_RETRY_ON: list[str] = [
    FailureClass.TRANSIENT.value,
    FailureClass.DEPENDENCY.value,
]


# ── Model ────────────────────────────────────────────────────────────


class RetryPolicy(BaseModel):
    """Workflow-level retry policy. Read from workflow IR (when
    present) or returned as a disabled default. Distinct from per-step
    retry settings - this policy short-circuits the per-step loop
    when the failure class isn't worth retrying."""

    enabled: bool = False
    max_attempts: int = 3
    initial_backoff_seconds: float = 2.0
    backoff_multiplier: float = 2.0
    backoff_max_seconds: float = 60.0
    # FailureClass enum values. A failure whose class IS in this list
    # is eligible for retry; anything else short-circuits to the
    # error-return path immediately.
    retry_on: list[str] = Field(default_factory=lambda: list(_DEFAULT_RETRY_ON))

    @field_validator("max_attempts")
    @classmethod
    def _max_attempts_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"max_attempts must be >= 1, got {v}")
        return v

    @field_validator("initial_backoff_seconds", "backoff_max_seconds")
    @classmethod
    def _backoff_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"backoff seconds must be >= 0, got {v}")
        return v

    @field_validator("backoff_multiplier")
    @classmethod
    def _multiplier_at_least_one(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError(
                f"backoff_multiplier must be >= 1.0 (use 1.0 for fixed delay), "
                f"got {v}"
            )
        return v

    @field_validator("retry_on")
    @classmethod
    def _retry_on_values_valid(cls, v: list[str]) -> list[str]:
        # Reject unknown FailureClass strings so a typo doesn't
        # silently disable retry for everything.
        valid = {fc.value for fc in FailureClass}
        for item in v:
            if item not in valid:
                raise ValueError(
                    f"retry_on contains unknown failure class {item!r}; "
                    f"valid: {sorted(valid)}"
                )
        return v


# ── Helpers ──────────────────────────────────────────────────────────


def should_retry(
    policy: RetryPolicy,
    failure_class: str | FailureClass | None,
    *,
    attempt: int,
) -> bool:
    """Should the executor schedule another attempt after a failure?

    Arguments:
      policy        - the workflow's RetryPolicy (disabled by default)
      failure_class - FailureClass enum value (or its string), or None
                      when the executor couldn't classify
      attempt       - 1-based attempt number that just failed
                      (1 = first attempt; 2 = first retry, etc.)

    Returns:
      True  - schedule another attempt (executor should sleep, then run)
      False - give up; the executor should propagate the failure

    Behaviour matrix:
      * policy.enabled == False  -> defer to caller (returns True, so
                                     the executor's existing per-step
                                     retry loop is unchanged)
      * attempt >= max_attempts  -> False
      * failure_class is None    -> conservative: True only if any
                                     retry budget remains; the executor
                                     will give it ONE retry then bail
      * failure_class in retry_on -> True (subject to budget)
      * failure_class NOT in retry_on -> False (short-circuit; retry
                                          won't change the outcome)
    """
    if not policy.enabled:
        # Pre-policy behaviour: defer to whatever called us. The
        # executor's per-step retry loop still drives the decision.
        return True
    if attempt >= policy.max_attempts:
        return False
    if failure_class is None:
        # Unclassified failure - give it ONE retry then bail. Better
        # than blanket retry; worse than knowing the class. The
        # classifier surfacing UNKNOWN already handles this case
        # (passes through "unknown" string), so this path is for
        # genuine None.
        return attempt < 2
    fc = failure_class.value if isinstance(failure_class, FailureClass) else str(failure_class)
    return fc in set(policy.retry_on)


def backoff_for(policy: RetryPolicy, *, attempt: int) -> float:
    """How long to sleep before the next attempt.

    `attempt` is 1-based and refers to the attempt that JUST failed.
    The returned delay is for the NEXT one (attempt+1).

    Capped at `policy.backoff_max_seconds` to prevent unbounded growth
    on long-running exponential backoff.
    """
    if attempt < 1:
        return 0.0
    delay = policy.initial_backoff_seconds * (policy.backoff_multiplier ** (attempt - 1))
    return min(delay, policy.backoff_max_seconds)


# ── Resolver (used by the executor wire-in, E2.1) ───────────────────


def resolve_workflow_policy(workflow) -> RetryPolicy:
    """Pull the RetryPolicy out of a Workflow IR if one is declared,
    otherwise return a disabled default.

    Reads from ``workflow.retry_policy`` if present (the IR field is
    optional). Used by the executor's retry loop (E2.1). Tolerant of
    test paths that pass plain dicts or workflow stubs without the
    field.
    """
    if workflow is None:
        return RetryPolicy()
    try:
        raw = getattr(workflow, "retry_policy", None)
    except Exception:
        return RetryPolicy()
    if raw is None:
        return RetryPolicy()
    try:
        if isinstance(raw, RetryPolicy):
            return raw
        if isinstance(raw, dict):
            return RetryPolicy.model_validate(raw)
        # Already a Pydantic instance with the right shape duck-typed?
        return RetryPolicy.model_validate(raw)
    except Exception:
        # Don't fail the run over a bad policy config - just behave
        # as if no policy were declared.
        return RetryPolicy()


__all__ = [
    "RetryPolicy",
    "should_retry",
    "backoff_for",
    "resolve_workflow_policy",
]
