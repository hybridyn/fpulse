"""E2.1 integration test (2026-06-08).

Confirms RetryPolicy (shipped in E2) is consulted by the executor's
per-step retry loop. We don't run a real workflow here (heavy fixture
+ load-bearing); we pin three contracts:

  1. Workflow IR accepts an optional retry_policy dict
  2. resolve_workflow_policy() handles a Workflow with a dict policy
  3. The executor source contains the consult call site
     (regression-guard: if someone removes the 5-line block, this test
     fails loudly)

Combined with the E2 unit tests (32 cases covering should_retry +
backoff_for + resolve_workflow_policy across the FailureClass matrix),
these are sufficient to assert "the policy is wired, the policy works,
the wiring stays."
"""
from __future__ import annotations

import pytest

from fpulse.engine.retry_policy import (
    RetryPolicy,
    resolve_workflow_policy,
    should_retry,
)
from fpulse.ir.schema import Workflow


class TestWorkflowIRAcceptsPolicy:
    def test_default_workflow_has_no_policy(self):
        wf = Workflow(name="x")
        assert wf.retry_policy is None

    def test_workflow_accepts_dict_policy(self):
        wf = Workflow(name="x", retry_policy={
            "enabled": True,
            "max_attempts": 5,
            "retry_on": ["transient", "dependency"],
        })
        assert wf.retry_policy is not None
        assert wf.retry_policy["enabled"] is True
        assert wf.retry_policy["max_attempts"] == 5

    def test_workflow_serialises_policy(self):
        wf = Workflow(name="x", retry_policy={"enabled": True, "max_attempts": 7})
        dumped = wf.model_dump()
        assert dumped["retry_policy"]["enabled"] is True
        assert dumped["retry_policy"]["max_attempts"] == 7

    def test_workflow_json_round_trip(self):
        # End-to-end: declared in IR, serialised to JSON, parsed back.
        # This is the path the workflow store + frontend will take.
        wf = Workflow(name="x", retry_policy={
            "enabled": True,
            "retry_on": ["transient"],
        })
        import json
        parsed = json.loads(wf.model_dump_json())
        wf2 = Workflow.model_validate(parsed)
        assert wf2.retry_policy["enabled"] is True
        assert wf2.retry_policy["retry_on"] == ["transient"]


class TestPolicyResolutionFromIR:
    """resolve_workflow_policy reads the IR's retry_policy and produces
    a typed RetryPolicy the executor can consult."""

    def test_no_policy_returns_disabled_default(self):
        wf = Workflow(name="x")
        policy = resolve_workflow_policy(wf)
        assert isinstance(policy, RetryPolicy)
        assert policy.enabled is False

    def test_enabled_policy_resolves_correctly(self):
        wf = Workflow(name="x", retry_policy={
            "enabled": True,
            "max_attempts": 4,
            "retry_on": ["transient"],
        })
        policy = resolve_workflow_policy(wf)
        assert policy.enabled is True
        assert policy.max_attempts == 4
        assert policy.retry_on == ["transient"]

    def test_bad_policy_dict_returns_disabled_default(self):
        # Workflow with a malformed policy doesn't crash the executor
        wf = Workflow(name="x", retry_policy={"max_attempts": -99})
        policy = resolve_workflow_policy(wf)
        assert policy.enabled is False


class TestEndToEndDecision:
    """Compose the IR → resolver → decision helpers as the executor does."""

    def test_enabled_policy_short_circuits_data_quality(self):
        wf = Workflow(name="x", retry_policy={
            "enabled": True,
            "retry_on": ["transient", "dependency"],
        })
        policy = resolve_workflow_policy(wf)
        # data_quality not in retry_on → don't retry
        assert should_retry(policy, "data_quality", attempt=1) is False

    def test_enabled_policy_retries_transient(self):
        wf = Workflow(name="x", retry_policy={
            "enabled": True,
            "max_attempts": 3,
        })
        policy = resolve_workflow_policy(wf)
        assert should_retry(policy, "transient", attempt=1) is True
        assert should_retry(policy, "transient", attempt=3) is False  # >= max

    def test_disabled_policy_defers_to_executor_loop(self):
        wf = Workflow(name="x")  # no policy declared
        policy = resolve_workflow_policy(wf)
        # Disabled → returns True for everything; executor's existing
        # per-step retry loop decides as it always has
        assert should_retry(policy, "data_quality", attempt=1) is True
        assert should_retry(policy, "fatal", attempt=1) is True


class TestExecutorWireInRegressionGuard:
    """If someone removes the policy consult block from executor.py's
    except handler, this test fails loudly. The block is a 5-line
    addition at a single well-defined site (see retry_policy.py
    module docstring for the exact location)."""

    def test_executor_imports_retry_policy_helpers(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[1]
            / "fpulse" / "engine" / "executor.py"
        ).read_text(encoding="utf-8")
        # The integration imports these symbols lazily inside the
        # except handler so the module-load surface stays small.
        assert "resolve_workflow_policy" in src, (
            "E2.1 regression - executor must call resolve_workflow_policy"
        )
        assert "should_retry" in src, (
            "E2.1 regression - executor must call should_retry"
        )

    def test_executor_consults_before_retry_budget(self):
        # Pin the ORDER: the policy consult must happen BEFORE the
        # `attempt >= attempts_total` check, otherwise the policy
        # can't short-circuit retries that the per-step config
        # would otherwise schedule.
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[1]
            / "fpulse" / "engine" / "executor.py"
        ).read_text(encoding="utf-8")
        # Find the offsets of the two markers within _execute_step
        policy_pos = src.find("resolve_workflow_policy")
        attempts_pos = src.find("if attempt >= attempts_total")
        assert policy_pos > 0 and attempts_pos > 0, "markers missing"
        assert policy_pos < attempts_pos, (
            "E2.1 regression - policy consult must precede the "
            "attempts_total check so it can short-circuit retries "
            "the per-step config would otherwise schedule."
        )
