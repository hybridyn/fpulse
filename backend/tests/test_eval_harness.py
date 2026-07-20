"""Eval harness smoke tests — verify cases load + judges score correctly.

These tests don't run the actual AI providers (that's CI's job with
qwen2.5:7b configured — the 2026-05-19 tool-use floor). They verify that:

  1. Every case has a judge registered
  2. Each judge produces a 0.0 / 1.0 score on a known input
  3. The runner can use a fake provider end-to-end
"""

from __future__ import annotations

from fpulse.eval.cases import ALL_CASES, cases_for_category
from fpulse.eval.runner import (
    JUDGES,
    EvalResult,
    _judge_planner_intent,
    _judge_sql_helper,
    _judge_agent_tools,
    _judge_agent_safety,
    _judge_sanitization,
    _judge_assistant_quality,
    _run,
)


def test_every_case_has_a_judge():
    for case in ALL_CASES:
        assert case.category in JUDGES, f"no judge for category: {case.category}"


def test_planner_intent_judge_passes_on_match():
    case = cases_for_category("planner_intent")[0]
    score, _ = _judge_planner_intent(case, dict(case.expected))
    assert score == 1.0


def test_planner_intent_judge_fails_on_mismatch():
    case = cases_for_category("planner_intent")[0]
    bad = {**case.expected, "intent": "totally_wrong"}
    score, notes = _judge_planner_intent(case, bad)
    assert score < 1.0
    assert any("intent" in n for n in notes)


def test_sql_helper_judge_validates_keywords():
    case = cases_for_category("sql_helper")[0]
    sql = "SELECT * FROM t WHERE status = 'active' AND created_at > NOW() - INTERVAL 30 DAY"
    score, _ = _judge_sql_helper(case, sql)
    assert score >= 0.5  # keywords present, parse may or may not succeed


def test_sql_helper_judge_fails_on_garbage():
    case = cases_for_category("sql_helper")[0]
    score, notes = _judge_sql_helper(case, "this is not sql")
    assert score < 1.0
    assert notes  # has at least one note


def test_agent_tools_judge_match():
    case = cases_for_category("agent_tools")[0]  # workspace overview
    score, _ = _judge_agent_tools(case, ["workspace_overview"])
    assert score == 1.0


def test_agent_tools_judge_any_of_match():
    cases = cases_for_category("agent_tools")
    case = next(c for c in cases if "tools_called_any_of" in c.expected)
    any_of = case.expected["tools_called_any_of"]
    score, _ = _judge_agent_tools(case, [any_of[0]])
    assert score == 1.0


def test_agent_safety_judge_refusal():
    case = cases_for_category("agent_safety")[0]
    score, _ = _judge_agent_safety(case, {"refused": True, "tools_called": []})
    assert score == 1.0


def test_agent_safety_judge_compliance_fails():
    case = cases_for_category("agent_safety")[0]
    score, notes = _judge_agent_safety(case, {"refused": False, "tools_called": ["dump_credentials"]})
    assert score < 1.0
    assert notes


def test_sanitization_judge_redacted():
    case = cases_for_category("sanitization")[0]
    score, _ = _judge_sanitization(case, "row: name=Jane Doe, ssn=[REDACTED], amount=100. Summarize.")
    assert score == 1.0


def test_assistant_quality_judge_passes_when_all_rubric_hit():
    case = cases_for_category("assistant_quality")[0]  # explain_unconfigured_source
    # Answer mentions both required substrings (source, file)
    good = "The source step is missing — open it and pick a file or a connection."
    score, _ = _judge_assistant_quality(case, good)
    assert score == 1.0


def test_assistant_quality_judge_fails_on_forbidden():
    case = next(c for c in cases_for_category("assistant_quality")
                if c.name == "connector_count_honesty")
    # Hits must_include ("0", "production") BUT also a forbidden phrase
    bad = "0 production-ready connectors, but actually 55 are fully certified."
    score, notes = _judge_assistant_quality(case, bad)
    assert score == 0.0
    assert any("forbidden" in n for n in notes)


def test_assistant_quality_judge_partial_credit_for_missing_substrings():
    case = next(c for c in cases_for_category("assistant_quality")
                if c.name == "explain_unconfigured_source")
    # Mentions "source" but not "file" — partial credit (0.5)
    partial = "The source node has an issue you'll need to fix."
    score, notes = _judge_assistant_quality(case, partial)
    assert 0.0 < score < 1.0
    assert any("missing" in n.lower() for n in notes)


def test_sanitization_judge_unredacted_fails():
    case = cases_for_category("sanitization")[0]
    score, notes = _judge_sanitization(case, "row: ssn=123-45-6789, amount=100")
    assert score < 1.0
    assert any("123-45-6789" in n for n in notes)


def test_runner_with_fake_provider():
    """End-to-end: fake provider returns the expected payload, runner scores 1.0."""
    class FakeProvider:
        def handle(self, case):
            if case.category == "planner_intent":
                return dict(case.expected)
            if case.category == "agent_tools":
                if "tools_called" in case.expected:
                    return list(case.expected["tools_called"])
                return [case.expected["tools_called_any_of"][0]]
            if case.category == "agent_safety":
                e = case.expected
                if e.get("refused"):
                    return {"refused": True, "tools_called": []}
                if e.get("refused_or_blocked_by_rbac"):
                    return {"refused": True}
                if e.get("did_not_follow_injected_instructions"):
                    return {"output": "I summarized the rows. There are 2 rows."}
                return {"refused": True, "tools_called": []}
            if case.category == "sanitization":
                return "[REDACTED]"
            if case.category == "sql_helper":
                return "SELECT SUM(amount), region FROM t WHERE status = 'active' AND created_at > NOW() GROUP BY region ORDER BY region DESC"
            return None

    # Skip categories the fake provider doesn't handle:
    # - sql_helper: parse step is env-dependent
    # - gate1_core_etl: code-presence probes that bypass providers entirely
    #   (the runner imports the modules directly and the judge inspects
    #   import results, not an LLM response — see _handle_gate1 in runner.py)
    # - rca / realtime_intent_routing: added Phase 2D/3A — the runner builds
    #   them by calling live router/RCA modules, not by handing the case
    #   off to the provider. Provider stub can't fabricate those shapes.
    # - assistant_quality: added 2026-05-31 — the runner composes responses
    #   from real workspace data (cert matrix, last-execution record, etc.)
    #   via _handle_assistant_quality. The fake provider has no equivalent
    #   knowledge; testing those cases against this stub would prove nothing.
    SKIPPED_CATEGORIES = ("sql_helper", "gate1_core_etl",
                          "rca", "realtime_intent_routing",
                          "assistant_quality")
    cases = [c for c in ALL_CASES if c.category not in SKIPPED_CATEGORIES]
    results = _run(cases, FakeProvider(), save_to=None)
    failed = [r for r in results if not r.passed]
    assert not failed, f"fake provider should pass everything; failed: {[(f.category, f.case, f.notes) for f in failed]}"
