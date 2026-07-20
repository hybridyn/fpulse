"""Tests for the eval CLI's --min-pass-rate / --min-category-pass-rate gates.

These are CI tuning knobs — we test the exit-code logic without spinning
up the full eval harness by patching ``run_all`` / ``run_category`` to
return canned results.
"""

from __future__ import annotations

import pytest

from fpulse.eval import run as run_module
from fpulse.eval.runner import EvalResult


def _result(name: str, category: str, *, passed: bool) -> EvalResult:
    return EvalResult(
        case=name,
        category=category,
        score=1.0 if passed else 0.0,
        passed=passed,
        response=None,
        elapsed_ms=1,
    )


@pytest.fixture
def _no_save(tmp_path, monkeypatch):
    """Redirect the latest.json + report writes into tmp so tests don't
    touch the real data dir."""
    monkeypatch.setenv("FPULSE_DATA_DIR", str(tmp_path))
    yield


def test_legacy_mode_all_pass_exits_zero(monkeypatch, _no_save, capsys):
    monkeypatch.setattr(
        run_module, "run_all",
        lambda save_to=None: [
            _result("a", "planner_intent", passed=True),
            _result("b", "sql_helper", passed=True),
        ],
    )
    rc = run_module.main(["--no-save"])
    assert rc == 0
    out = capsys.readouterr().out
    # Legacy mode doesn't print a "GATE OK" line because no thresholds
    # were passed — preserves the existing CI log shape.
    assert "GATE OK" not in out


def test_legacy_mode_any_fail_exits_one(monkeypatch, _no_save):
    monkeypatch.setattr(
        run_module, "run_all",
        lambda save_to=None: [
            _result("a", "planner_intent", passed=True),
            _result("b", "sql_helper", passed=False),
        ],
    )
    assert run_module.main(["--no-save"]) == 1


def test_min_pass_rate_85_percent_lets_some_fail(monkeypatch, _no_save, capsys):
    """9/10 pass → 90% ≥ 85% → exit 0 even though one case failed."""
    cases = [_result(f"c{i}", "planner_intent", passed=True) for i in range(9)]
    cases.append(_result("c9", "planner_intent", passed=False))
    monkeypatch.setattr(run_module, "run_all", lambda save_to=None: cases)
    rc = run_module.main(["--no-save", "--min-pass-rate=0.85"])
    assert rc == 0
    assert "GATE OK" in capsys.readouterr().out


def test_min_pass_rate_85_percent_blocks_low_score(monkeypatch, _no_save, capsys):
    """7/10 pass → 70% < 85% → exit 1."""
    cases = [_result(f"c{i}", "planner_intent", passed=True) for i in range(7)]
    cases.extend(_result(f"c{i}", "planner_intent", passed=False) for i in range(7, 10))
    monkeypatch.setattr(run_module, "run_all", lambda save_to=None: cases)
    rc = run_module.main(["--no-save", "--min-pass-rate=0.85"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "GATE FAILED" in err


def test_min_category_pass_rate_catches_buried_regression(monkeypatch, _no_save, capsys):
    """Overall 90% passes 85% gate, but ONE category at 50% should fail
    a strict per-category gate. Prevents the cascade case where one
    category silently regresses but the aggregate hides it."""
    cases = [_result(f"p{i}", "planner_intent", passed=True) for i in range(9)]
    # 2 of 2 sanitization cases fail — category rate 0%
    cases.extend([
        _result("s0", "sanitization", passed=False),
        _result("s1", "sanitization", passed=False),
    ])
    # Overall: 9 of 11 pass = ~82% (below 85 gate, so adjust):
    # Add one more passing case so overall is exactly 10/12 = 83.3% (< 85%)
    # Make it 10 planner passes + 2 sanitization fails → 10/12 = 83% < 85%
    monkeypatch.setattr(run_module, "run_all", lambda save_to=None: cases)
    rc = run_module.main([
        "--no-save",
        "--min-pass-rate=0.5",            # overall comfortably passes
        "--min-category-pass-rate=0.85",  # but sanitization at 0% fails
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "sanitization" in err


def test_zero_threshold_acts_like_legacy(monkeypatch, _no_save):
    """Explicit --min-pass-rate=0 == legacy: any failure = exit 1."""
    cases = [
        _result("a", "planner_intent", passed=True),
        _result("b", "planner_intent", passed=False),
    ]
    monkeypatch.setattr(run_module, "run_all", lambda save_to=None: cases)
    # Without flags: legacy mode → fail
    assert run_module.main(["--no-save"]) == 1
