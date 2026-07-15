"""Tests for the EventBus + RunState aggregator.

Locks down the contract that the API + UI rely on:
  - Events flow to subscribers in publish order
  - Per-run state aggregates correctly across the lifecycle
  - Progress fraction + ETA derive sensibly from observed events
  - Old completed runs are evicted when the bus fills up
  - Subscriber failures don't break publish

The API endpoint test is light — it's a thin shim over `bus.snapshot`
and `bus.history`, both of which are exercised here directly.
"""

from __future__ import annotations

import time

import pytest

from fpulse.extraction.events import (
    EventBus,
    ExtractionEvent,
    RunState,
    make_run_id,
)


def _evt(run_id: str, kind: str, **payload) -> ExtractionEvent:
    return ExtractionEvent(
        run_id=run_id, profile="test_profile",
        kind=kind, ts=time.time(), payload=payload,
    )


# ── Subscriptions ───────────────────────────────────────────────────

def test_subscriber_receives_published_events_in_order():
    bus = EventBus()
    received: list[str] = []
    bus.subscribe(lambda e: received.append(e.kind))
    rid = make_run_id()
    for k in ("started", "list_phase_start", "list_phase_done", "completed"):
        bus.publish(_evt(rid, k))
    assert received == ["started", "list_phase_start", "list_phase_done", "completed"]


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    received: list[str] = []
    unsub = bus.subscribe(lambda e: received.append(e.kind))
    bus.publish(_evt("r1", "started"))
    unsub()
    bus.publish(_evt("r1", "completed"))
    assert received == ["started"]


def test_failing_subscriber_does_not_break_publish():
    bus = EventBus()
    other_received: list[str] = []
    bus.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.subscribe(lambda e: other_received.append(e.kind))
    bus.publish(_evt("r1", "started"))
    # The failing handler logged an error; the other handler still ran.
    assert other_received == ["started"]


# ── State derivation ────────────────────────────────────────────────

def test_lifecycle_state_progresses_through_phases():
    bus = EventBus()
    rid = make_run_id()
    bus.publish(_evt(rid, "started"))
    snap = bus.snapshot(rid)
    assert snap is not None and snap["phase"] == "starting"

    bus.publish(_evt(rid, "list_phase_start"))
    assert bus.snapshot(rid)["phase"] == "list"

    bus.publish(_evt(rid, "list_phase_done", id_count=100))
    snap = bus.snapshot(rid)
    assert snap["phase"] == "list"  # not transitioned yet
    assert snap["listed"] == 100

    bus.publish(_evt(rid, "enrichment_phase_start", target_count=100,
                      initial_concurrency=4))
    snap = bus.snapshot(rid)
    assert snap["phase"] == "enrichment"
    assert snap["concurrency"] == 4

    bus.publish(_evt(rid, "progress", extracted=50, failed=2, concurrency=8))
    snap = bus.snapshot(rid)
    assert snap["extracted"] == 50
    assert snap["failed"] == 2
    assert snap["concurrency"] == 8
    # Progress fraction reflects real progress.
    assert snap["progress"] == pytest.approx(0.52, abs=0.01)

    bus.publish(_evt(rid, "enrichment_phase_done",
                      succeeded=98, failed=2, skipped_resumed=0,
                      final_concurrency=10))
    snap = bus.snapshot(rid)
    assert snap["extracted"] == 98
    assert snap["concurrency"] == 10

    bus.publish(_evt(rid, "completed"))
    snap = bus.snapshot(rid)
    assert snap["phase"] == "completed"
    assert snap["completed_at"] is not None


def test_failure_path_records_error():
    bus = EventBus()
    rid = make_run_id()
    bus.publish(_evt(rid, "started"))
    bus.publish(_evt(rid, "failed", error="ConnectionError: boom"))
    snap = bus.snapshot(rid)
    assert snap["phase"] == "failed"
    assert "boom" in snap["error"]
    assert snap["completed_at"] is not None


def test_rate_limit_and_auth_refresh_counters_accumulate():
    bus = EventBus()
    rid = make_run_id()
    bus.publish(_evt(rid, "started"))
    for _ in range(3):
        bus.publish(_evt(rid, "rate_limited"))
    for _ in range(2):
        bus.publish(_evt(rid, "auth_refreshed"))
    snap = bus.snapshot(rid)
    assert snap["rate_limited_count"] == 3
    assert snap["auth_refreshed_count"] == 2


def test_progress_fraction_none_before_listed():
    """ETA + progress aren't meaningful until we know the total.
    Don't surface garbage early values to the UI."""
    bus = EventBus()
    rid = make_run_id()
    bus.publish(_evt(rid, "started"))
    snap = bus.snapshot(rid)
    assert snap["progress"] is None
    assert snap["eta_seconds"] is None


def test_eta_estimates_when_progress_observed():
    bus = EventBus()
    rid = make_run_id()
    # Stamp a started event 10 seconds ago, then half-progress now.
    started = ExtractionEvent(run_id=rid, profile="p", kind="started",
                                ts=time.time() - 10, payload={})
    bus.publish(started)
    bus.publish(_evt(rid, "enrichment_phase_start", target_count=100))
    bus.publish(_evt(rid, "progress", extracted=50, failed=0))
    snap = bus.snapshot(rid)
    # 50 done in ~10s → 5/s → ~10s remaining for the other 50.
    assert snap["eta_seconds"] is not None
    assert 5.0 <= snap["eta_seconds"] <= 30.0


# ── History ring buffer ─────────────────────────────────────────────

def test_history_keeps_last_n_events():
    bus = EventBus(history_size=10)
    rid = make_run_id()
    for i in range(15):
        bus.publish(_evt(rid, "progress", extracted=i))
    history = bus.history(rid)
    # Ring buffer truncated to history_size; we get the last 10.
    assert len(history) == 10
    extracted_values = [e["payload"]["extracted"] for e in history]
    assert extracted_values == list(range(5, 15))


def test_history_for_unknown_run_returns_empty():
    bus = EventBus()
    assert bus.history("never_existed") == []


# ── List runs ───────────────────────────────────────────────────────

def test_list_runs_returns_newest_first():
    bus = EventBus()
    older = make_run_id()
    newer = make_run_id()
    bus.publish(ExtractionEvent(run_id=older, profile="p", kind="started",
                                  ts=1.0, payload={}))
    bus.publish(ExtractionEvent(run_id=newer, profile="p", kind="started",
                                  ts=2.0, payload={}))
    runs = bus.list_runs()
    assert [r["run_id"] for r in runs] == [newer, older]


def test_list_runs_active_only_filter():
    bus = EventBus()
    active = make_run_id()
    done = make_run_id()
    bus.publish(_evt(active, "started"))
    bus.publish(_evt(done, "started"))
    bus.publish(_evt(done, "completed"))
    actives = bus.list_runs(active_only=True)
    assert len(actives) == 1
    assert actives[0]["run_id"] == active


# ── Eviction ────────────────────────────────────────────────────────

def test_completed_runs_evict_when_cap_exceeded():
    """Active runs are never evicted; old completed runs roll off."""
    bus = EventBus()
    bus._max_runs_in_memory = 5  # type: ignore[attr-defined]

    active_id = make_run_id()
    bus.publish(_evt(active_id, "started"))  # never completes

    completed_ids = []
    for i in range(10):
        rid = make_run_id()
        completed_ids.append(rid)
        bus.publish(ExtractionEvent(run_id=rid, profile="p", kind="started",
                                      ts=10.0 + i, payload={}))
        bus.publish(ExtractionEvent(run_id=rid, profile="p", kind="completed",
                                      ts=10.0 + i + 0.1, payload={}))

    # Active run still tracked, oldest completed runs gone.
    assert bus.snapshot(active_id) is not None
    assert bus.snapshot(completed_ids[0]) is None
    # The most recent completed run is preserved.
    assert bus.snapshot(completed_ids[-1]) is not None
