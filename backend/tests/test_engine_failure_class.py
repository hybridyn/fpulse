"""Pinned tests for the failure-classification taxonomy (E1, 2026-06-08).

First milestone from docs/design/executor-maturity-1.2.md. The
retry-policy work (E2) depends on these classifications being
stable - the labels and resolution order pin the contract.

Contracts pinned here:
  * Each FailureClass value is reachable from at least one fixture
  * Resolution order: per-connector classifier → built-in exception
    type → message substring → UNKNOWN
  * `retry_advisable` returns True only for TRANSIENT + DEPENDENCY
  * `summarise_failure_classes` rolls up correctly including blanks
  * register_classifier hook composes with the built-in rules
"""
from __future__ import annotations

import pytest

from fpulse.engine.failure_class import (
    FailureClass,
    classify_error,
    register_classifier,
    retry_advisable,
    summarise_failure_classes,
)


# ── Message-substring rules ─────────────────────────────────────────


class TestMessageClassification:
    def test_timeout_is_transient(self):
        for msg in ["Connection timed out after 30s", "deadline exceeded",
                     "Operation timed out"]:
            assert classify_error(msg) == FailureClass.TRANSIENT, msg

    def test_rate_limit_is_transient(self):
        for msg in ["429 Too Many Requests", "Rate limit exceeded",
                     "Throttled by upstream API", "Quota exceeded"]:
            assert classify_error(msg) == FailureClass.TRANSIENT, msg

    def test_deadlock_is_transient(self):
        for msg in ["lock timeout exceeded", "deadlock detected",
                     "Lock wait timeout"]:
            assert classify_error(msg) == FailureClass.TRANSIENT, msg

    def test_auth_is_dependency(self):
        for msg in ["401 Unauthorized", "403 Forbidden", "Access denied",
                     "Invalid credentials", "Expired token", "expired key"]:
            assert classify_error(msg) == FailureClass.DEPENDENCY, msg

    def test_unreachable_is_dependency(self):
        for msg in ["Connection refused", "Could not connect to host",
                     "Name or service not known", "DNS lookup failed"]:
            assert classify_error(msg) == FailureClass.DEPENDENCY, msg

    def test_5xx_classifications(self):
        # 500 = transient (try again), 503/504 = dependency (external down)
        assert classify_error("500 Internal Server Error") == FailureClass.TRANSIENT
        assert classify_error("503 Service Unavailable") == FailureClass.DEPENDENCY
        assert classify_error("504 Gateway Timeout") == FailureClass.DEPENDENCY

    def test_null_violation_is_data_quality(self):
        for msg in ["null value in column \"customer_id\" violates not-null constraint",
                     "Cannot insert NULL into 'email'",
                     "not-null violation on order_id"]:
            assert classify_error(msg) == FailureClass.DATA_QUALITY, msg

    def test_unique_violation_is_data_quality(self):
        for msg in ["unique violation on orders_pkey",
                     "duplicate key value violates unique constraint",
                     "Row already exists in table"]:
            assert classify_error(msg) == FailureClass.DATA_QUALITY, msg

    def test_schema_mismatch_is_data_quality(self):
        for msg in ["column \"foo\" does not exist",
                     "unknown column 'bar'",
                     "schema mismatch on inserts"]:
            assert classify_error(msg) == FailureClass.DATA_QUALITY, msg

    def test_invalid_config_is_user_input(self):
        for msg in ["Invalid pipeline configuration",
                     "Missing required field: connection_id",
                     "No such file or directory: /tmp/missing.csv"]:
            assert classify_error(msg) == FailureClass.USER_INPUT, msg

    def test_oom_is_fatal(self):
        for msg in ["Out of memory", "MemoryError: cannot allocate 1GB",
                     "Cannot allocate memory"]:
            assert classify_error(msg) == FailureClass.FATAL, msg

    def test_disk_full_is_fatal(self):
        for msg in ["No space left on device", "ENOSPC",
                     "Disk full while writing checkpoint"]:
            assert classify_error(msg) == FailureClass.FATAL, msg

    def test_unrecognised_is_unknown(self):
        assert classify_error("everything is fine actually") == FailureClass.UNKNOWN
        assert classify_error("") == FailureClass.UNKNOWN
        assert classify_error(None) == FailureClass.UNKNOWN


# ── Exception-type rules ────────────────────────────────────────────


class TestExceptionTypeClassification:
    def test_memory_error_is_fatal(self):
        assert classify_error(MemoryError("OOM")) == FailureClass.FATAL

    def test_value_error_is_user_input(self):
        assert classify_error(ValueError("bad input")) == FailureClass.USER_INPUT

    def test_type_error_is_user_input(self):
        assert classify_error(TypeError("expected int")) == FailureClass.USER_INPUT

    def test_key_error_is_user_input(self):
        assert classify_error(KeyError("missing_field")) == FailureClass.USER_INPUT

    def test_file_not_found_is_user_input(self):
        assert classify_error(FileNotFoundError("not here")) == FailureClass.USER_INPUT

    def test_connection_refused_is_dependency(self):
        assert classify_error(ConnectionRefusedError("nope")) == FailureClass.DEPENDENCY

    def test_timeout_error_is_transient(self):
        assert classify_error(TimeoutError("slow")) == FailureClass.TRANSIENT

    def test_unknown_exception_type_falls_through_to_message(self):
        # Custom exception with no built-in type rule - falls to message regex
        class MyOddError(Exception):
            pass
        assert classify_error(MyOddError("connection refused by peer")) == FailureClass.DEPENDENCY
        assert classify_error(MyOddError("unrelated text")) == FailureClass.UNKNOWN


# ── Resolution order ─────────────────────────────────────────────────


class TestResolutionOrder:
    def test_per_connector_classifier_wins_over_builtin(self):
        # Built-in says ValueError → USER_INPUT. Per-connector says
        # this specific kind of ValueError is data quality.
        def _override(exc):
            if "schema check" in str(exc).lower():
                return FailureClass.DATA_QUALITY
            return None
        register_classifier("ValueError", _override)
        try:
            assert classify_error(ValueError("schema check failed")) == FailureClass.DATA_QUALITY
            # Non-matching message falls through to the built-in rule
            assert classify_error(ValueError("bad input")) == FailureClass.USER_INPUT
        finally:
            # Restore (don't leak registration into other tests)
            from fpulse.engine import failure_class as fc_mod
            fc_mod._CONNECTOR_CLASSIFIERS.pop("ValueError", None)

    def test_returning_none_from_override_falls_through(self):
        def _no_op(exc):
            return None
        register_classifier("FileNotFoundError", _no_op)
        try:
            # Should still get the built-in USER_INPUT result
            assert classify_error(FileNotFoundError("x")) == FailureClass.USER_INPUT
        finally:
            from fpulse.engine import failure_class as fc_mod
            fc_mod._CONNECTOR_CLASSIFIERS.pop("FileNotFoundError", None)

    def test_explicit_exception_type_arg_overrides_inferred(self):
        # Caller passes the type name explicitly (e.g. when the
        # error was deserialised from JSON and the exception class
        # isn't available locally).
        assert classify_error("some message", exception_type="MemoryError") == FailureClass.FATAL


# ── Retry advisability ──────────────────────────────────────────────


class TestRetryAdvisable:
    def test_transient_advises_retry(self):
        assert retry_advisable(FailureClass.TRANSIENT) is True

    def test_dependency_advises_retry(self):
        assert retry_advisable(FailureClass.DEPENDENCY) is True

    def test_data_quality_does_not(self):
        assert retry_advisable(FailureClass.DATA_QUALITY) is False

    def test_user_input_does_not(self):
        assert retry_advisable(FailureClass.USER_INPUT) is False

    def test_fatal_does_not(self):
        assert retry_advisable(FailureClass.FATAL) is False

    def test_unknown_does_not(self):
        # Conservative default - don't retry what we don't understand
        assert retry_advisable(FailureClass.UNKNOWN) is False

    def test_accepts_string(self):
        # Persisted column is a string; retry_advisable handles that.
        assert retry_advisable("transient") is True
        assert retry_advisable("fatal") is False


# ── Rollup ──────────────────────────────────────────────────────────


class TestSummarise:
    def test_rolls_up_counts(self):
        data = ["transient", "transient", "dependency", "fatal", "transient"]
        out = summarise_failure_classes(data)
        assert out["transient"] == 3
        assert out["dependency"] == 1
        assert out["fatal"] == 1
        assert out["data_quality"] == 0

    def test_empty_list_returns_all_zeros(self):
        out = summarise_failure_classes([])
        assert all(v == 0 for v in out.values())
        # All six categories present
        assert set(out.keys()) == {fc.value for fc in FailureClass}

    def test_blank_classified_as_unknown(self):
        out = summarise_failure_classes(["", "transient", "", None])  # type: ignore
        assert out["unknown"] == 3
        assert out["transient"] == 1


# ── Reachability ─────────────────────────────────────────────────────


class TestReachability:
    """Every FailureClass must be reachable from at least one fixture,
    otherwise the classifier silently can't surface it."""

    def test_all_classes_have_a_route(self):
        # Match-fail message routes each class is supposed to hit:
        cases = {
            FailureClass.TRANSIENT:    "Connection timed out after 30s",
            FailureClass.DEPENDENCY:   "401 Unauthorized",
            FailureClass.DATA_QUALITY: "unique violation",
            FailureClass.USER_INPUT:   "Missing required field",
            FailureClass.FATAL:        "Out of memory",
            FailureClass.UNKNOWN:      "nothing matches this string at all",
        }
        for expected, msg in cases.items():
            assert classify_error(msg) == expected, f"{expected.value} unreachable via msg={msg!r}"
