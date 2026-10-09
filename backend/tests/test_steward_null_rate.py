"""Tests for the Steward null-rate (NULL_RATE_ANOMALY) detector."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fpulse.steward import (
    NullRateSample,
    NullRateSampleStore,
    detect_null_rate_anomalies,
    samples_from_report,
)
from fpulse.steward.models import FindingKind


def _s(rate, i, src="src", col="email"):
    return NullRateSample(
        source_signature=src, column=col, null_rate=rate, total_rows=1000,
        run_id=f"r{i}", at=datetime(2026, 1, 1, i, tzinfo=timezone.utc).isoformat(),
    )


def _series(rates, **kw):
    return [_s(r, i, **kw) for i, r in enumerate(rates)]


def test_spike_detected():
    samples = _series([0.01, 0.012, 0.009, 0.011, 0.01, 0.013, 0.12])
    findings = detect_null_rate_anomalies(samples)
    assert len(findings) == 1
    assert findings[0].kind == FindingKind.NULL_RATE_ANOMALY
    assert findings[0].evidence["column"] == "email"
    assert findings[0].evidence["current_null_rate"] == 0.12


def test_improvement_not_flagged():
    samples = _series([0.10, 0.11, 0.09, 0.10, 0.10, 0.11, 0.01])
    assert detect_null_rate_anomalies(samples) == []


def test_small_absolute_move_not_flagged():
    samples = _series([0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.025])  # +1.5pp < 2pp floor
    assert detect_null_rate_anomalies(samples) == []


def test_insufficient_history_not_flagged():
    samples = _series([0.01, 0.01, 0.01, 0.5])  # 3 baseline < min_history 5
    assert detect_null_rate_anomalies(samples) == []


def test_suppressed_signature_silenced():
    samples = _series([0.01, 0.012, 0.009, 0.011, 0.01, 0.013, 0.12])
    once = detect_null_rate_anomalies(samples)
    assert len(once) == 1
    sig = once[0].evidence["source_signature"]
    assert detect_null_rate_anomalies(samples, suppressed_signatures={sig}) == []


def test_deterministic_ids():
    samples = _series([0.01, 0.012, 0.009, 0.011, 0.01, 0.013, 0.12])
    a = detect_null_rate_anomalies(samples)
    b = detect_null_rate_anomalies(samples)
    assert [f.id for f in a] == [f.id for f in b]


def test_samples_from_report_extracts_only_not_null():
    report = SimpleNamespace(source_signature="s", run_id="r1", assertions=[
        SimpleNamespace(check="not_null", column="email", failed_count=50, total_rows=1000),
        SimpleNamespace(check="not_null", column="", failed_count=1, total_rows=10),    # no column
        SimpleNamespace(check="unique", column="id", failed_count=0, total_rows=10),    # not not_null
        SimpleNamespace(check="not_null", column="x", failed_count=0, total_rows=0),     # no rows
    ])
    out = samples_from_report(report)
    assert len(out) == 1
    assert out[0].column == "email"
    assert abs(out[0].null_rate - 0.05) < 1e-9


def test_store_roundtrip(tmp_path):
    store = NullRateSampleStore(tmp_path / "nr.jsonl")
    store.append_many(_series([0.01, 0.02]))
    got = store.all()
    assert len(got) == 2 and got[0].column == "email"
