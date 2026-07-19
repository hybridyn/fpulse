"""Unit tests for the Steward foreseer VOLUME_ANOMALY detector
(2026-06-08). Pins the baseline-variance behaviour (Hard Rule 6): the
detector learns each source's normal volume and flags deviations, never
an absolute threshold.
"""
from __future__ import annotations

import pytest

from fpulse.steward.cost import CostEvent
from fpulse.steward.foreseer import (
    _mad,
    _median,
    _signature,
    detect_volume_anomalies,
    modified_zscore,
)
from fpulse.steward.models import FindingKind, FindingLevel


def _ev(sig: str, rows_read: int, day: int, **kw) -> CostEvent:
    """A cost event for `sig` reading `rows_read` rows on `day`."""
    return CostEvent(
        source_signature=sig,
        rows_read=rows_read,
        rows_written=kw.pop("rows_written", rows_read),
        run_id=kw.pop("run_id", f"run-{day:03d}"),
        pipeline_id=kw.pop("pipeline_id", "pl-1"),
        pipeline_name=kw.pop("pipeline_name", "Orders"),
        completed_at=f"2026-06-{day:02d}T00:00:00+00:00",
        **kw,
    )


# ── statistics helpers ───────────────────────────────────────────────


def test_median_odd_and_even():
    assert _median([3, 1, 2]) == 2
    assert _median([1, 2, 3, 4]) == 2.5
    assert _median([]) == 0.0


def test_mad_basic():
    xs = [1, 1, 2, 2, 4, 6, 9]
    med = _median(xs)  # 2
    # deviations |x-2| = [1,1,0,0,2,4,7] -> median 1
    assert _mad(xs, med) == 1


def test_modified_zscore_zero_when_no_spread():
    assert modified_zscore(100, [10, 10, 10, 10]) == 0.0


def test_modified_zscore_flags_outlier():
    history = [100, 102, 98, 101, 99, 100]
    z = modified_zscore(0, history)
    assert z < -3.5  # a drop to zero is a strong negative outlier


# ── detector: fires on real deviations ───────────────────────────────


def _stable_history(sig: str, value: int, n: int) -> list[CostEvent]:
    return [_ev(sig, value, day=d + 1) for d in range(n)]


def test_sharp_drop_is_flagged():
    sig = "src-orders"
    # 6 prior runs ~10k, then a drop to 0.
    events = [_ev(sig, v, day=i + 1) for i, v in enumerate([10000, 9800, 10200, 9900, 10100, 9950])]
    events.append(_ev(sig, 0, day=7))
    findings = detect_volume_anomalies(events)
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == FindingKind.VOLUME_ANOMALY
    assert f.level == FindingLevel.DATA
    assert f.evidence["direction"] == "drop"
    assert f.evidence["current_rows_read"] == 0
    assert f.evidence["baseline_median"] > 9000
    assert f.evidence["sample_size"] == 6


def test_spike_is_flagged():
    sig = "src-events"
    events = [_ev(sig, v, day=i + 1) for i, v in enumerate([500, 520, 480, 510, 495, 505])]
    events.append(_ev(sig, 50000, day=7))  # 100x spike
    findings = detect_volume_anomalies(events)
    assert len(findings) == 1
    assert findings[0].evidence["direction"] == "spike"
    assert findings[0].evidence["pct_change"] > 100


def test_perfectly_stable_baseline_then_change_fires():
    # MAD == 0 path: every prior run identical, then a material+relative move.
    sig = "src-flat"
    events = _stable_history(sig, 1000, 6)
    events.append(_ev(sig, 100, day=7))  # 90% drop from a dead-steady 1000
    findings = detect_volume_anomalies(events)
    assert len(findings) == 1
    assert findings[0].evidence["direction"] == "drop"


# ── detector: stays quiet when it should (Rule 6 + guards) ───────────


def test_usually_empty_source_not_flagged():
    # Rule 6: a source that is 0 most days is NOT flagged for another 0.
    sig = "src-error-queue"
    events = _stable_history(sig, 0, 6)
    events.append(_ev(sig, 0, day=7))
    assert detect_volume_anomalies(events) == []


def test_insufficient_history_not_flagged():
    sig = "src-new"
    # Only 4 prior runs + current = below the 5-run minimum.
    events = [_ev(sig, v, day=i + 1) for i, v in enumerate([10000, 9800, 10200, 9900])]
    events.append(_ev(sig, 0, day=6))
    assert detect_volume_anomalies(events) == []


def test_small_absolute_change_not_flagged():
    # Tiny table: 10 -> 25 is a big ratio but < material-row guard.
    sig = "src-tiny"
    events = [_ev(sig, v, day=i + 1) for i, v in enumerate([10, 11, 9, 10, 12, 10])]
    events.append(_ev(sig, 25, day=7))
    assert detect_volume_anomalies(events) == []


def test_small_relative_change_not_flagged():
    # Large table, small relative wobble: 100k -> 102k should not fire.
    sig = "src-big"
    events = [_ev(sig, v, day=i + 1) for i, v in enumerate(
        [100000, 100500, 99500, 100200, 99800, 100100])]
    events.append(_ev(sig, 102000, day=7))
    assert detect_volume_anomalies(events) == []


# ── suppression + idempotency ────────────────────────────────────────


def test_suppressed_signature_is_skipped():
    sig = "src-orders"
    events = [_ev(sig, v, day=i + 1) for i, v in enumerate([10000, 9800, 10200, 9900, 10100, 9950])]
    events.append(_ev(sig, 0, day=7))
    sig_hash = _signature("volume_anomaly", "default", sig)
    findings = detect_volume_anomalies(events, suppressed_signatures={sig_hash})
    assert findings == []


def test_deterministic_finding_id_is_idempotent():
    sig = "src-orders"
    events = [_ev(sig, v, day=i + 1) for i, v in enumerate([10000, 9800, 10200, 9900, 10100, 9950])]
    events.append(_ev(sig, 0, day=7))
    f1 = detect_volume_anomalies(events)[0]
    f2 = detect_volume_anomalies(list(events))[0]
    assert f1.id == f2.id
    assert f1.id.startswith("vol-")


def test_events_without_source_signature_are_ignored():
    events = [_ev("", 100, day=i + 1) for i in range(8)]
    assert detect_volume_anomalies(events) == []
