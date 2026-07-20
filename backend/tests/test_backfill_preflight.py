"""Unit tests for ``fpulse.backfills.preflight.check_cursor_param_usage``.

Pure-function tests — no FastAPI, no DB, no app_state. Each test builds
the smallest possible Workflow/Step pydantic object that exercises one
branch of the static-IR scan.

The check is the second guardrail (after find_unsafe_sinks) on the
backfill create path: if no source step references the cursor
parameter(s), every window would reprocess the same full dataset. The
helper returns ``None`` when at-least-one source references the cursor
and a violation dict otherwise; the API turns the violation into HTTP
400 unless the caller passes ``acknowledge_no_cursor_usage=true``.

Coverage notes:
  - The 8 happy/sad-path cases in the spec exercise the public function.
  - A second class drills into the recursive ``_references_param`` so a
    later refactor that changes string-matching semantics (e.g. moving
    away from ``${param.NAME}`` literal) trips a test.
"""

from __future__ import annotations

import pytest

from fpulse.backfills.preflight import (
    check_cursor_param_usage,
    _references_param,
)
from fpulse.ir.schema import Workflow, Step, StepType


# ─────────────────────────────────────────────────────────────────────
# check_cursor_param_usage — public surface
# ─────────────────────────────────────────────────────────────────────


def _wf(*steps: Step) -> Workflow:
    """Build a minimal Workflow holding the given steps."""
    return Workflow(id="wf-test", name="t", steps=list(steps))


class TestCheckCursorParamUsage:
    def test_no_source_steps_returns_none(self):
        """A pipeline with only transforms/sinks isn't reading external
        data — the cursor-usage check is moot, returns None (no warning)."""
        wf = _wf(
            Step(id="t1", type=StepType.FILTER,
                 params={"condition": "x > 0"}),
            Step(id="t2", type=StepType.OUTPUT,
                 params={"format": "csv", "path": "out.csv"}),
        )
        assert check_cursor_param_usage(wf, ["window_start", "window_end"]) is None

    def test_db_source_references_window_start_returns_none(self):
        wf = _wf(
            Step(id="s1", type=StepType.DB_SOURCE, params={
                "query": "SELECT * FROM o WHERE created_at >= '${param.window_start}'",
                "connection_id": "c1",
            }),
        )
        assert check_cursor_param_usage(wf, ["window_start", "window_end"]) is None

    def test_api_source_references_window_end_returns_none(self):
        wf = _wf(
            Step(id="s1", type=StepType.API_SOURCE, params={
                "url": "https://api.example.com/orders?to=${param.window_end}",
            }),
        )
        assert check_cursor_param_usage(wf, ["window_start", "window_end"]) is None

    def test_source_without_cursor_returns_violation(self):
        """The pipeline has a source but it doesn't reference any cursor
        param → backfill would silently re-process. Expect the canonical
        violation dict."""
        wf = _wf(
            Step(id="s1", type=StepType.DB_SOURCE, params={
                "query": "SELECT * FROM orders",
                "connection_id": "c1",
            }),
        )
        v = check_cursor_param_usage(wf, ["window_start", "window_end"])
        assert v is not None
        assert v["code"] == "no_source_uses_cursor_param"
        # Sources_checked must surface the offending source.
        assert isinstance(v["sources_checked"], list)
        assert len(v["sources_checked"]) == 1
        assert v["sources_checked"][0]["id"] == "s1"
        assert v["sources_checked"][0]["type"] == "db_source"
        # Cursor names echoed back so the caller can render them in the message.
        assert v["cursor_param_names"] == ["window_start", "window_end"]

    def test_multiple_sources_at_least_one_uses_cursor(self):
        """One source references the cursor, the other doesn't → at-least-one
        rule passes, returns None."""
        wf = _wf(
            Step(id="s1", type=StepType.DB_SOURCE, params={
                "query": "SELECT * FROM lookup",  # static, no cursor
                "connection_id": "c1",
            }),
            Step(id="s2", type=StepType.DB_SOURCE, params={
                "query": "SELECT * FROM events WHERE ts >= '${param.window_start}'",
                "connection_id": "c1",
            }),
        )
        assert check_cursor_param_usage(wf, ["window_start", "window_end"]) is None

    def test_custom_cursor_param_names_pass_through(self):
        wf = _wf(
            Step(id="s1", type=StepType.DB_SOURCE, params={
                "query": "SELECT * FROM o WHERE d >= '${param.from_date}'",
                "connection_id": "c1",
            }),
        )
        assert check_cursor_param_usage(wf, ["from_date", "to_date"]) is None

    def test_nested_dict_cursor_reference_detected(self):
        """The scan must recurse into nested dicts — e.g. a `where: {clause: …}`
        block tucked under params."""
        wf = _wf(
            Step(id="s1", type=StepType.DB_SOURCE, params={
                "query": "SELECT 1",  # query itself has no cursor
                "where": {
                    "clause": "ts >= '${param.window_start}' AND ts < '${param.window_end}'",
                },
            }),
        )
        assert check_cursor_param_usage(wf, ["window_start", "window_end"]) is None

    def test_empty_cursor_param_names_returns_none(self):
        """Defensive: with no cursor params to check, no check is possible."""
        wf = _wf(
            Step(id="s1", type=StepType.DB_SOURCE, params={
                "query": "SELECT * FROM orders",
                "connection_id": "c1",
            }),
        )
        assert check_cursor_param_usage(wf, []) is None


# ─────────────────────────────────────────────────────────────────────
# _references_param — the recursive scalar/dict/list walker
# ─────────────────────────────────────────────────────────────────────


class TestReferencesParamHelper:
    def test_string_match(self):
        assert _references_param("ts >= '${param.window_start}'", "window_start") is True

    def test_string_no_match(self):
        assert _references_param("ts >= '2026-01-01'", "window_start") is False

    def test_nested_dict_match(self):
        v = {"a": {"b": {"c": "x = ${param.foo}"}}}
        assert _references_param(v, "foo") is True

    def test_nested_dict_no_match(self):
        v = {"a": {"b": "hi"}, "c": [1, 2, 3]}
        assert _references_param(v, "missing") is False

    def test_list_of_strings_match(self):
        assert _references_param(
            ["select 1", "select * from t where x > ${param.cutoff}"],
            "cutoff",
        ) is True

    def test_list_no_match(self):
        assert _references_param(["a", "b", "c"], "anything") is False

    def test_none_returns_false(self):
        assert _references_param(None, "x") is False

    def test_int_returns_false(self):
        assert _references_param(42, "x") is False

    def test_float_returns_false(self):
        assert _references_param(3.14, "x") is False

    def test_bool_returns_false(self):
        assert _references_param(True, "x") is False

    def test_tuple_also_walked(self):
        """tuples take the same branch as lists in the implementation."""
        assert _references_param(("a", "x=${param.q}"), "q") is True

    def test_partial_name_no_match(self):
        """``${param.window}`` must NOT match cursor name ``window_start`` —
        the brace closer enforces the boundary."""
        assert _references_param("${param.window}", "window_start") is False
