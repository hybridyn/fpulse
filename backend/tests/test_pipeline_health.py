"""Tests for the pipeline-health aggregator.

The aggregator is a pure function over execution-row dicts — no I/O,
no DB. These tests construct synthetic execution sequences and lock
down every derivation: success rate, duration percentiles, MTTR,
badge classification, and the running/never_run edge cases.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fpulse.monitoring.pipeline_health import (
    PipelineHealth,
    compute_all_health,
    compute_health,
    health_summary,
    _percentile,
)


def _row(*, status: str, started_at: datetime,
         duration_ms: float = 1000.0, error: str | None = None,
         completed_at: datetime | None = None) -> dict:
    """Mirror the shape ExecutionStore returns from list_by_workflow."""
    return {
        "workflow_id": "p1",
        "status": status,
        "started_at": started_at.isoformat(),
        "completed_at": (completed_at or started_at + timedelta(seconds=1)).isoformat(),
        "duration_ms": duration_ms,
        "error_message": error,
    }


# ── Empty / never-run state ────────────────────────────────────────

def test_no_executions_yields_never_run_badge():
    h = compute_health("p1", [])
    assert h.derive_badge() == "never_run"
    assert h.total_runs == 0
    assert h.success_rate is None


def test_only_running_executions_yields_running_badge():
    now = datetime.now(timezone.utc)
    h = compute_health("p1", [_row(status="running", started_at=now)])
    assert h.derive_badge() == "running"
    assert h.running_count == 1


# ── Healthy / degraded / unhealthy ─────────────────────────────────

def test_all_success_yields_healthy_badge():
    now = datetime.now(timezone.utc)
    rows = [_row(status="success", started_at=now - timedelta(minutes=i))
            for i in range(20)]
    h = compute_health("p1", rows)
    assert h.success_rate == 1.0
    assert h.derive_badge() == "healthy"


def test_high_failure_rate_yields_unhealthy_badge():
    now = datetime.now(timezone.utc)
    # 8 failures, 2 successes — 20% success rate, latest is failure.
    rows = [_row(status="error", started_at=now - timedelta(minutes=i))
            for i in range(8)]
    rows += [_row(status="success", started_at=now - timedelta(minutes=10 + i))
             for i in range(2)]
    h = compute_health("p1", rows)
    assert h.success_rate == 0.2
    assert h.last_run_status == "failed"
    assert h.derive_badge() == "unhealthy"


def test_mid_success_rate_yields_degraded_badge():
    now = datetime.now(timezone.utc)
    # 7 success, 3 fail — 70% success rate.
    rows = [_row(status="success", started_at=now - timedelta(minutes=i))
            for i in range(7)]
    rows += [_row(status="error", started_at=now - timedelta(minutes=10 + i))
             for i in range(3)]
    h = compute_health("p1", rows)
    assert h.success_rate == 0.7
    # Last run was success but rate is below the healthy threshold (0.95).
    assert h.derive_badge() == "degraded"


def test_recent_failure_overrides_high_historical_rate():
    """Newer failure = surface unhealthy even if historical rate is OK.

    A pipeline that succeeded 95 times last month then failed 5 times
    today should NOT show as healthy — most-recent status matters."""
    now = datetime.now(timezone.utc)
    # Most recent row is failure (rows arrive newest-first).
    rows = [_row(status="error", started_at=now - timedelta(minutes=i))
            for i in range(5)]
    rows += [_row(status="success", started_at=now - timedelta(minutes=10 + i))
             for i in range(5)]
    h = compute_health("p1", rows)
    # 5 fail / 5 success → 50% rate; last is fail. Below 0.5 trigger,
    # should be degraded NOT healthy because last_run_status='failed'.
    assert h.success_rate == 0.5
    assert h.last_run_status == "failed"
    assert h.derive_badge() in ("unhealthy", "degraded")
    assert h.derive_badge() != "healthy"


# ── Duration percentiles ───────────────────────────────────────────

def test_avg_and_p95_durations_computed_from_successes_only():
    """Failures don't have meaningful durations — exclude them from
    the latency stats so a 0-ms crash doesn't drag the avg down."""
    now = datetime.now(timezone.utc)
    rows = [
        _row(status="success", started_at=now, duration_ms=1000),  # 1.0s
        _row(status="success", started_at=now, duration_ms=2000),  # 2.0s
        _row(status="success", started_at=now, duration_ms=3000),  # 3.0s
        _row(status="success", started_at=now, duration_ms=4000),  # 4.0s
        _row(status="success", started_at=now, duration_ms=5000),  # 5.0s
        _row(status="error",   started_at=now, duration_ms=0),     # excluded
    ]
    h = compute_health("p1", rows)
    assert h.avg_duration_s == 3.0  # mean of 1..5
    # p95 of [1,2,3,4,5] interpolation = 4.8
    assert h.p95_duration_s is not None
    assert 4.5 < h.p95_duration_s < 5.0


def test_percentile_helper_basic():
    assert _percentile([], 50) is None
    assert _percentile([5], 50) == 5
    assert _percentile([1, 2, 3, 4, 5], 50) == 3
    # 95th percentile of 1..5 = 4.8 (linear interpolation)
    assert abs((_percentile([1, 2, 3, 4, 5], 95) or 0) - 4.8) < 0.01


# ── Last failure surfacing ─────────────────────────────────────────

def test_last_failure_reason_truncated_to_300_chars():
    """Pyodbc dumps and stack traces are huge — UI shouldn't have to
    truncate. The aggregator caps at 300 chars itself."""
    now = datetime.now(timezone.utc)
    long_msg = "x" * 1000
    h = compute_health("p1", [_row(status="error", started_at=now, error=long_msg)])
    assert h.last_failure_reason is not None
    assert len(h.last_failure_reason) <= 300


def test_last_failure_picks_most_recent_failure():
    """Multiple failures in window — last_failure_at and reason come
    from the chronologically most-recent failure, not the first one."""
    now = datetime.now(timezone.utc)
    rows = [
        _row(status="error", started_at=now,                          error="newest"),
        _row(status="error", started_at=now - timedelta(hours=1),     error="middle"),
        _row(status="error", started_at=now - timedelta(hours=2),     error="oldest"),
    ]
    h = compute_health("p1", rows)
    assert h.last_failure_reason == "newest"


# ── MTTR computation ───────────────────────────────────────────────

def test_mttr_none_when_no_failure_recovery_pair():
    """All successes → no failure to recover from → mttr is None."""
    now = datetime.now(timezone.utc)
    rows = [_row(status="success", started_at=now)]
    h = compute_health("p1", rows)
    assert h.mttr_s is None


def test_mttr_computed_when_failure_then_success():
    """Failure at T, success at T+30s → mttr = 30s."""
    base = datetime.now(timezone.utc)
    # Rows are stored newest-first.
    rows = [
        _row(status="success", started_at=base + timedelta(seconds=30),
              completed_at=base + timedelta(seconds=30)),
        _row(status="error",   started_at=base, completed_at=base),
    ]
    h = compute_health("p1", rows)
    assert h.mttr_s is not None
    assert 25 <= h.mttr_s <= 35


def test_mttr_averages_across_multiple_recoveries():
    """Multiple failure/recovery pairs in window → mean of the gaps."""
    base = datetime.now(timezone.utc)
    rows_oldest_first = [
        _row(status="error",   started_at=base + timedelta(seconds=0),  completed_at=base + timedelta(seconds=0)),
        _row(status="success", started_at=base + timedelta(seconds=10), completed_at=base + timedelta(seconds=10)),
        _row(status="error",   started_at=base + timedelta(seconds=20), completed_at=base + timedelta(seconds=20)),
        _row(status="success", started_at=base + timedelta(seconds=50), completed_at=base + timedelta(seconds=50)),
    ]
    # Reverse to mimic ExecutionStore's newest-first ordering.
    h = compute_health("p1", list(reversed(rows_oldest_first)))
    # First gap: 10s, second gap: 30s. Mean: 20s.
    assert h.mttr_s is not None
    assert 18 <= h.mttr_s <= 22


# ── Window / rolling behaviour ─────────────────────────────────────

def test_window_truncates_input_rows():
    now = datetime.now(timezone.utc)
    rows = [_row(status="success", started_at=now - timedelta(seconds=i))
            for i in range(200)]
    h = compute_health("p1", rows, window=50)
    assert h.total_runs == 50
    assert h.rolling_window == 50


# ── Bulk + summary ──────────────────────────────────────────────────

class _FakeStore:
    """Stub ExecutionStore exposing only what compute_all_health calls."""
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def list_recent(self, *, limit: int = 100, workspace_id=None) -> list[dict]:
        return self.rows[:limit]


def test_compute_all_groups_by_workflow_id():
    now = datetime.now(timezone.utc)
    rows = [
        {**_row(status="success", started_at=now), "workflow_id": "p1"},
        {**_row(status="error",   started_at=now), "workflow_id": "p2"},
        {**_row(status="success", started_at=now), "workflow_id": "p1"},
    ]
    health_map = compute_all_health(_FakeStore(rows))
    assert set(health_map.keys()) == {"p1", "p2"}
    assert health_map["p1"].success_count == 2
    assert health_map["p2"].failure_count == 1


def test_summary_counts_by_badge():
    health_map = {
        "p1": PipelineHealth(pipeline_id="p1"),  # never_run
        "p2": PipelineHealth(pipeline_id="p2", total_runs=5,
                              success_count=5, success_rate=1.0,
                              last_run_status="success"),  # healthy
        "p3": PipelineHealth(pipeline_id="p3", total_runs=10,
                              success_count=2, failure_count=8,
                              success_rate=0.2,
                              last_run_status="failed"),  # unhealthy
    }
    summary = health_summary(health_map)
    assert summary["total"] == 3
    assert summary["never_run"] == 1
    assert summary["healthy"] == 1
    assert summary["unhealthy"] == 1
