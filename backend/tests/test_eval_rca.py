"""Sprint E exit-gate test — 5 seeded failure scenarios produce useful answers.

The eval harness's `rca` category covers token expired, schema drift, rate
limit, network timeout, and warehouse lock. Each case must score 1.0 (both
diagnosis + suggestion keyword-match) via the deterministic fallback in
`diagnose_error` — the LLM path is a bonus, not a requirement.

This is the closing gate for Sprint E. Failures here mean the regex pattern
set in `embedded.py` is missing the scenario; add a pattern, retry.
"""

from __future__ import annotations

import pytest

from fpulse.eval.cases import RCA_CASES
from fpulse.eval.runner import _handle_rca, _judge_rca


@pytest.mark.parametrize("case", RCA_CASES, ids=[c.name for c in RCA_CASES])
def test_rca_scenario_passes(case):
    """Every seeded RCA scenario must score 1.0 against the keyword judge."""
    response = _handle_rca(case)
    score, notes = _judge_rca(case, response)
    assert score == 1.0, (
        f"RCA case {case.name!r} scored {score:.2f}\n"
        f"  diagnosis: {response.get('diagnosis')!r}\n"
        f"  suggestion: {response.get('suggestion')!r}\n"
        f"  notes: {notes}"
    )


def test_rca_response_shape():
    """All RCA responses must carry the standard diagnose-error shape."""
    response = _handle_rca(RCA_CASES[0])
    assert "diagnosis" in response
    assert "suggestion" in response
    assert "severity" in response
    assert response["severity"] in ("error", "warning", "info")
    assert "ai_powered" in response  # set by try_llm_then_fallback


def test_rca_ai_powered_flag_correct_when_no_provider(monkeypatch):
    """Without a provider, ai_powered should be False (we used the fallback)."""
    # Force resolve_provider to return 'none' so the LLM path is bypassed.
    from fpulse.planner import ai_client
    monkeypatch.setattr(
        ai_client, "resolve_provider",
        lambda **kwargs: ("none", None, "", None),
    )
    response = _handle_rca(RCA_CASES[0])
    assert response.get("ai_powered") is False
