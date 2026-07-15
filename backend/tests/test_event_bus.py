"""
Smoke tests for fpulse.events — proves the InProcess implementation
actually does what the interface promises.

Not exhaustive. Covers the four shapes that matter most:
  - Sync subscriber receives a published event
  - Durable events survive in SQLite and replay via cursor
  - Best-effort events drop oldest under overflow
  - Topic wildcards (`*`, `>`) route correctly
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from fpulse.events import (
    DurabilityClass,
    PipelineRunStarted,
    StepCompleted,
    StepProgress,
    get_event_bus,
)
from fpulse.events.factory import _set_event_bus
from fpulse.events.in_process import InProcessEventBus, _topic_matches


# ── Topic matching ──────────────────────────────────────────────


def test_topic_matches_exact():
    assert _topic_matches("fpulse.pipeline.run.started", "fpulse.pipeline.run.started")
    assert not _topic_matches("fpulse.pipeline.run.started", "fpulse.pipeline.run.failed")


def test_topic_matches_star():
    assert _topic_matches("fpulse.pipeline.run.*", "fpulse.pipeline.run.started")
    assert _topic_matches("fpulse.pipeline.run.*", "fpulse.pipeline.run.failed")
    # `*` does not cross segment boundaries
    assert not _topic_matches("fpulse.pipeline.*", "fpulse.pipeline.run.started")


def test_topic_matches_gt():
    assert _topic_matches("fpulse.pipeline.>", "fpulse.pipeline.run.started")
    assert _topic_matches("fpulse.pipeline.>", "fpulse.pipeline.step.completed")
    assert _topic_matches("fpulse.pipeline.>", "fpulse.pipeline.run")
    assert not _topic_matches("fpulse.pipeline.>", "fpulse.alert.fired")


# ── Sync callback delivery ──────────────────────────────────────


def test_sync_subscriber_receives_published_event():
    bus = InProcessEventBus()
    bus.start()
    try:
        received: list = []
        bus.subscribe("fpulse.pipeline.run.started", received.append)

        bus.publish(PipelineRunStarted(
            run_id="r1", pipeline_id="p1",
            pipeline_version="v1", triggered_by="user:42",
            project_id="proj1",
        ))

        # Dispatcher runs on a background thread; give it a moment.
        deadline = time.monotonic() + 1.5
        while not received and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(received) == 1
        assert received[0].run_id == "r1"
    finally:
        bus.close()


def test_durable_event_persists_and_replays():
    """Durable events survive close+reopen and replay via cursor."""
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        db = os.path.join(tmpdir, "events.db")

        # First bus instance: publish.
        bus1 = InProcessEventBus(db_path=db)
        bus1.start()
        bus1.publish(StepCompleted(
            run_id="r1", step_id="s1", step_type="csv_source",
            duration_ms=50, row_count=100,
        ))
        # Give dispatcher a beat.
        time.sleep(0.2)
        bus1.close()

        # Second bus instance: replay from cursor=0.
        bus2 = InProcessEventBus(db_path=db)
        bus2.start()
        try:
            replayed = bus2._replay("fpulse.>", since="0")
            assert len(replayed) == 1
            assert replayed[0].run_id == "r1"
            assert replayed[0].row_count == 100
        finally:
            bus2.close()


def test_best_effort_overflow_drops_oldest():
    """Per-subscription queue has bounded capacity; new events evict old."""
    from fpulse.events.in_process import _BEST_EFFORT_QUEUE_MAX

    async def runner():
        bus = InProcessEventBus()
        bus.start()
        try:
            it = bus.stream("fpulse.step.progress").__aiter__()

            # Publish more than capacity. All BEST_EFFORT.
            burst = _BEST_EFFORT_QUEUE_MAX + 50
            for i in range(burst):
                bus.publish(StepProgress(
                    run_id="r1", step_id="s1", rows_so_far=i,
                ))

            # Wait briefly for dispatch.
            await asyncio.sleep(0.3)

            # Pull what made it through.
            seen = []
            try:
                while True:
                    seen.append(await asyncio.wait_for(it.__anext__(), timeout=0.1))
            except (StopAsyncIteration, asyncio.TimeoutError):
                pass

            # We dropped some, kept the bounded amount.
            assert len(seen) <= _BEST_EFFORT_QUEUE_MAX
            # And the survivors are the *most recent* — overflow
            # dropped the oldest.
            assert seen[-1].rows_so_far == burst - 1
        finally:
            bus.close()

    asyncio.run(runner())


def test_subscriber_exception_does_not_kill_dispatch():
    """One bad handler must not silence the others."""
    bus = InProcessEventBus()
    bus.start()
    try:
        good_seen: list = []

        def bad_handler(_ev):
            raise RuntimeError("bad subscriber")

        bus.subscribe("fpulse.>", bad_handler)
        bus.subscribe("fpulse.>", good_seen.append)

        bus.publish(PipelineRunStarted(run_id="r1", pipeline_id="p1"))

        deadline = time.monotonic() + 1.5
        while not good_seen and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(good_seen) == 1
    finally:
        bus.close()


def test_factory_defaults_to_inprocess(monkeypatch):
    monkeypatch.delenv("FPULSE_EVENT_BUS", raising=False)
    monkeypatch.setenv("FPULSE_EVENT_DB", ":memory:")
    _set_event_bus(None)  # clear any prior singleton
    try:
        bus = get_event_bus()
        assert isinstance(bus, InProcessEventBus)
    finally:
        _set_event_bus(None)


# NatsEventBus end-to-end behaviour is covered in
# test_event_bus_nats.py (uses mocked nats.connect). The factory's
# `FPULSE_EVENT_BUS=nats` routing is tested there too, alongside
# the mock fixtures.
