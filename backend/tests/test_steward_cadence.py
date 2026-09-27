"""Tests for the Steward cadence (CADENCE_MISS) detector."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fpulse.steward import detect_cadence_misses
from fpulse.steward.models import FindingKind

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ev(sig, dt, pid="p1"):
    iso = dt.isoformat()
    return SimpleNamespace(
        source_signature=sig, completed_at=iso, recorded_at=iso,
        pipeline_id=pid, pipeline_name="Daily load", run_id="run-" + iso,
    )


def _daily(sig, n, start=BASE):
    return [_ev(sig, start + timedelta(days=i)) for i in range(n)]


def test_overdue_daily_source_is_flagged():
    events = _daily("s", 8)                 # 8 daily runs -> 7 intervals of 1d
    now = BASE + timedelta(days=7) + timedelta(days=3)  # 3 days after the last run
    findings = detect_cadence_misses(events, now=now)
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == FindingKind.CADENCE_MISS
    assert f.evidence["overdue_ratio"] >= 3.0
    assert f.evidence["sample_size"] == 7


def test_on_schedule_source_not_flagged():
    events = _daily("s", 8)
    now = BASE + timedelta(days=7, hours=6)  # only 6h after the last run
    assert detect_cadence_misses(events, now=now) == []


def test_slightly_late_not_flagged():
    events = _daily("s", 8)
    now = BASE + timedelta(days=7) + timedelta(hours=25)  # ~1.04x cadence
    assert detect_cadence_misses(events, now=now) == []


def test_insufficient_history_not_flagged():
    events = _daily("s", 4)                  # only 3 intervals (< min_history=5)
    now = BASE + timedelta(days=3) + timedelta(days=10)
    assert detect_cadence_misses(events, now=now) == []


def test_irregular_cadence_not_flagged():
    # gaps: 1h, 40d, 1h, 40d, 1h, 40d -> MAD/median huge, no real cadence
    times = [BASE]
    for delta in [timedelta(hours=1), timedelta(days=40)] * 3:
        times.append(times[-1] + delta)
    events = [_ev("s", t) for t in times]
    now = times[-1] + timedelta(days=200)
    assert detect_cadence_misses(events, now=now) == []


def test_suppressed_signature_silenced():
    events = _daily("s", 8)
    now = BASE + timedelta(days=10)
    once = detect_cadence_misses(events, now=now)
    assert len(once) == 1
    sig_hash = once[0].evidence["source_signature"]
    assert detect_cadence_misses(events, now=now, suppressed_signatures={sig_hash}) == []


def test_deterministic_ids():
    events = _daily("s", 8)
    now = BASE + timedelta(days=10)
    a = detect_cadence_misses(events, now=now)
    b = detect_cadence_misses(events, now=now)
    assert [f.id for f in a] == [f.id for f in b]
