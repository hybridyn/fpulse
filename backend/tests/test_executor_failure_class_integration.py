"""E1.1 integration test (2026-06-08).

Confirms FailureClass shipped in E1 is wired into the executor's
error path. We don't run the full executor here (it has heavy
dependencies); instead we pin two contracts:

  1. StepRunResult model accepts the new failure_class field
  2. The classifier the executor will call resolves each exception
     type to a stable failure_class string

These together cover the "executor sets the field, downstream reads
it as a string" contract that E2 (retry policy) will depend on.
"""
from __future__ import annotations

import pytest

from fpulse.engine.failure_class import FailureClass, classify_error
from fpulse.ir.schema import StepRunResult


class TestStepRunResultField:
    """Pin the model surface E1.1 added."""

    def test_failure_class_defaults_to_none(self):
        r = StepRunResult(step_id="s1")
        assert r.failure_class is None

    def test_failure_class_accepts_enum_value(self):
        # Persisted as plain string (FailureClass.value)
        for cls in FailureClass:
            r = StepRunResult(step_id="s1", status="error",
                                failure_class=cls.value)
            assert r.failure_class == cls.value

    def test_failure_class_serialises_in_model_dump(self):
        r = StepRunResult(step_id="s1", status="error",
                            failure_class="transient")
        dumped = r.model_dump()
        assert dumped["failure_class"] == "transient"

    def test_failure_class_present_in_json_serialization(self):
        # Frontend reads model_dump_json — pin that the new field
        # makes it through the JSON encoding path.
        r = StepRunResult(step_id="s1", status="error",
                            failure_class="dependency")
        import json
        parsed = json.loads(r.model_dump_json())
        assert parsed["failure_class"] == "dependency"


class TestClassifierMatchesExecutorContract:
    """The executor's wire-in calls classify_error(last_error) and
    stores .value on StepRunResult.failure_class. Pin that every
    plausible exception type the executor might see produces a
    string the StepRunResult validator accepts."""

    @pytest.mark.parametrize("exc,expected", [
        (MemoryError("OOM"),                    FailureClass.FATAL),
        (ConnectionRefusedError("nope"),         FailureClass.DEPENDENCY),
        (TimeoutError("slow"),                   FailureClass.TRANSIENT),
        (ValueError("bad input"),                FailureClass.USER_INPUT),
        (FileNotFoundError("not found"),         FailureClass.USER_INPUT),
        (Exception("connection timed out"),      FailureClass.TRANSIENT),
        (Exception("401 Unauthorized"),          FailureClass.DEPENDENCY),
        (Exception("unique violation"),          FailureClass.DATA_QUALITY),
        (Exception("totally unknown error"),     FailureClass.UNKNOWN),
    ])
    def test_classifier_maps_each_exception_to_string(self, exc, expected):
        fc = classify_error(exc)
        assert fc == expected
        # And StepRunResult accepts the resulting string value
        r = StepRunResult(step_id="s1", status="error",
                            failure_class=fc.value)
        assert r.failure_class == expected.value


class TestRegressionGuard:
    """If someone removes the failure_class field from StepRunResult,
    this fails loudly. The executor's error-path wire-in references
    the field by name and would silently drop the classification on
    every failed run."""

    def test_step_run_result_has_failure_class_attribute(self):
        # Build a default instance and assert the attribute exists
        # (not just .__dict__ - because Pydantic uses model_fields)
        r = StepRunResult(step_id="s1")
        assert hasattr(r, "failure_class")
        # Pydantic 2.11 deprecates model_fields on the instance — read it
        # from the model class.
        assert "failure_class" in type(r).model_fields, (
            "E1.1 regression - failure_class must stay declared on "
            "StepRunResult; executor.py references it by keyword arg."
        )
