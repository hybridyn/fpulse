"""
Tests for the built-in event-bus consumers. Each consumer is
exercised through the public install() API — proving the
"add-one-file" claim: no executor edits, no bus internals
touched.

Uses InProcessEventBus for transport; the consumers don't care
which transport is wired.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from fpulse.events import (
    PipelineRunCompleted,
    PipelineRunFailed,
    PipelineRunStarted,
    StepCompleted,
    StepFailed,
    StepProgress,
)
from fpulse.events.consumers import AuditConsumer, MetricsConsumer
from fpulse.events.in_process import InProcessEventBus


# ── Helpers ─────────────────────────────────────────────────────


def _wait_until(predicate, timeout: float = 1.5) -> bool:
    """Spin-wait for the bus dispatcher thread to drain. The bus is
    async by design, so tests need an explicit barrier — no fixed
    sleep, just poll the side effect we care about."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ── MetricsConsumer ─────────────────────────────────────────────


def test_metrics_counts_events_by_topic():
    bus = InProcessEventBus()
    bus.start()
    metrics = MetricsConsumer()
    metrics.install(bus)
    try:
        bus.publish(PipelineRunStarted(run_id="r1", pipeline_id="p1"))
        bus.publish(StepCompleted(
            run_id="r1", step_id="s1", step_type="filter",
            duration_ms=10, row_count=100,
        ))
        bus.publish(StepCompleted(
            run_id="r1", step_id="s2", step_type="filter",
            duration_ms=20, row_count=80,
        ))

        assert _wait_until(
            lambda: metrics.snapshot()["events_by_topic"].get(
                "fpulse.step.completed"
            ) == 2
        )
        snap = metrics.snapshot()
        assert snap["events_by_topic"]["fpulse.pipeline.run.started"] == 1
        assert snap["events_by_topic"]["fpulse.step.completed"] == 2
    finally:
        bus.close()


def test_metrics_step_duration_histogram():
    bus = InProcessEventBus()
    bus.start()
    metrics = MetricsConsumer()
    metrics.install(bus)
    try:
        bus.publish(StepCompleted(
            run_id="r1", step_id="s1", step_type="filter",
            duration_ms=100, row_count=10,
        ))
        bus.publish(StepCompleted(
            run_id="r1", step_id="s2", step_type="filter",
            duration_ms=300, row_count=10,
        ))
        bus.publish(StepCompleted(
            run_id="r1", step_id="s3", step_type="transform",
            duration_ms=50, row_count=10,
        ))

        # Wait for the LAST event in publish order so all earlier
        # events are guaranteed dispatched too.
        assert _wait_until(
            lambda: metrics.snapshot()["step_duration_ms"].get(
                "transform", {}
            ).get("count") == 1
        )
        snap = metrics.snapshot()
        assert snap["step_duration_ms"]["filter"]["sum"] == 400
        assert snap["step_duration_ms"]["filter"]["count"] == 2
        assert snap["step_duration_ms"]["transform"]["sum"] == 50
        assert snap["step_duration_ms"]["transform"]["count"] == 1
    finally:
        bus.close()


def test_metrics_step_failures_counter():
    bus = InProcessEventBus()
    bus.start()
    metrics = MetricsConsumer()
    metrics.install(bus)
    try:
        bus.publish(StepFailed(
            run_id="r1", step_id="s1", step_type="db_source",
            duration_ms=100, error_class="OperationalError",
            error_message="connection refused",
        ))
        bus.publish(StepFailed(
            run_id="r1", step_id="s2", step_type="db_source",
            duration_ms=100, error_class="OperationalError",
            error_message="timeout",
        ))
        bus.publish(StepFailed(
            run_id="r1", step_id="s3", step_type="api_source",
            duration_ms=100, error_class="HTTPError",
            error_message="503",
        ))

        # Wait for the LAST event so all three have been processed.
        assert _wait_until(
            lambda: metrics.snapshot()["step_failures"].get("api_source") == 1
        )
        snap = metrics.snapshot()
        assert snap["step_failures"]["db_source"] == 2
        assert snap["step_failures"]["api_source"] == 1
    finally:
        bus.close()


def test_metrics_render_prometheus_format():
    bus = InProcessEventBus()
    bus.start()
    metrics = MetricsConsumer()
    metrics.install(bus)
    try:
        bus.publish(PipelineRunCompleted(
            run_id="r1", pipeline_id="p1", duration_ms=1500,
            rows_processed=100, step_count=3,
        ))
        bus.publish(StepCompleted(
            run_id="r1", step_id="s1", step_type="filter",
            duration_ms=200, row_count=100,
        ))
        bus.publish(StepFailed(
            run_id="r1", step_id="s2", step_type="api_source",
            duration_ms=300, error_class="HTTPError", error_message="500",
        ))

        # Wait for the LAST event published (StepFailed) so all
        # three are guaranteed dispatched.
        assert _wait_until(
            lambda: metrics.snapshot()["events_by_topic"].get(
                "fpulse.step.failed"
            ) == 1
        )

        text = metrics.render()

        # Counter lines.
        assert "# TYPE fpulse_events_total counter" in text
        assert 'fpulse_events_total{topic="fpulse.step.completed"} 1' in text
        assert 'fpulse_events_total{topic="fpulse.step.failed"} 1' in text
        # Step duration summary.
        assert 'fpulse_step_duration_seconds_sum{step_type="filter"} 0.200000' in text
        assert 'fpulse_step_duration_seconds_count{step_type="filter"} 1' in text
        # Failure counter.
        assert 'fpulse_step_failures_total{step_type="api_source"} 1' in text
        # Pipeline run summary.
        assert "fpulse_pipeline_run_duration_seconds_sum 1.500000" in text
        assert "fpulse_pipeline_run_duration_seconds_count 1" in text
    finally:
        bus.close()


# ── AuditConsumer ───────────────────────────────────────────────


def test_audit_writes_durable_events_only(tmp_path: Path):
    log_path = tmp_path / "audit.jsonl"
    bus = InProcessEventBus()
    bus.start()
    audit = AuditConsumer(path=log_path)
    audit.install(bus)
    try:
        # Two DURABLE.
        bus.publish(PipelineRunStarted(run_id="r1", pipeline_id="p1"))
        bus.publish(StepCompleted(
            run_id="r1", step_id="s1", step_type="filter",
            duration_ms=5, row_count=10,
        ))
        # One BEST_EFFORT — must NOT appear in audit.
        bus.publish(StepProgress(run_id="r1", step_id="s1", rows_so_far=5))

        # Wait until the two durables have landed.
        def lines_ready() -> bool:
            return log_path.exists() and len(audit.read_all()) >= 2

        assert _wait_until(lines_ready)

        # Brief grace period to confirm the best-effort event did NOT
        # sneak in late.
        time.sleep(0.15)
        lines = audit.read_all()
        assert len(lines) == 2

        # Each line is a parseable JSON object with the event's
        # topic + type intact.
        parsed = [json.loads(ln) for ln in lines]
        topics = {p["_topic"] for p in parsed}
        assert topics == {"fpulse.pipeline.run.started", "fpulse.step.completed"}
        # The progress event is conspicuously absent.
        assert all(p["_topic"] != "fpulse.step.progress" for p in parsed)
    finally:
        bus.close()


def test_audit_appends_idempotently_across_restarts(tmp_path: Path):
    """Audit consumer doesn't truncate — restarting and publishing
    more events appends to the existing log."""
    log_path = tmp_path / "audit.jsonl"

    bus1 = InProcessEventBus()
    bus1.start()
    AuditConsumer(path=log_path).install(bus1)
    bus1.publish(PipelineRunStarted(run_id="r1", pipeline_id="p1"))
    assert _wait_until(lambda: log_path.exists() and len(log_path.read_text().splitlines()) >= 1)
    bus1.close()

    bus2 = InProcessEventBus()
    bus2.start()
    AuditConsumer(path=log_path).install(bus2)
    bus2.publish(PipelineRunFailed(
        run_id="r2", pipeline_id="p1", duration_ms=10,
        failed_step_id="s1", error_class="X", error_message="y",
    ))
    assert _wait_until(lambda: len(log_path.read_text().splitlines()) >= 2)
    bus2.close()

    assert len(log_path.read_text().splitlines()) == 2


def test_audit_filename_is_created_recursively(tmp_path: Path):
    """`mkdir(parents=True)` happens in __init__ so callers can
    point at a path inside a non-existent dir tree."""
    nested = tmp_path / "deep" / "nested" / "audit.jsonl"
    AuditConsumer(path=nested)
    assert nested.parent.is_dir()


# ── Wire-up sanity ──────────────────────────────────────────────


def test_consumers_install_alongside_each_other(tmp_path: Path):
    """Two independent consumers, one bus, one event — both observe."""
    log_path = tmp_path / "audit.jsonl"
    bus = InProcessEventBus()
    bus.start()
    metrics = MetricsConsumer()
    audit = AuditConsumer(path=log_path)
    metrics.install(bus)
    audit.install(bus)
    try:
        bus.publish(StepCompleted(
            run_id="r1", step_id="s1", step_type="filter",
            duration_ms=10, row_count=100,
        ))
        assert _wait_until(
            lambda: metrics.snapshot()["events_by_topic"].get(
                "fpulse.step.completed"
            ) == 1
            and log_path.exists()
            and len(audit.read_all()) == 1
        )
    finally:
        bus.close()


# ── Executor ↔ bus wiring ───────────────────────────────────────


def test_executor_publishes_typed_events_when_bus_attached():
    """When given an event_bus, RealtimeExecutor's _emit() also fires
    typed events to the bus alongside the legacy on_event callback.
    Verifies the mapping table in realtime._publish_to_bus directly,
    without standing up a full DuckDB workflow."""
    from fpulse.engine.realtime import RealtimeExecutor

    bus = InProcessEventBus()
    bus.start()
    metrics = MetricsConsumer()
    metrics.install(bus)
    try:
        legacy_seen: list[dict] = []
        execu = RealtimeExecutor(
            data_dir=".",
            on_event=legacy_seen.append,
            event_bus=bus,
        )

        # Exercise every legacy event name → typed event branch.
        execu._emit("workflow_started", {
            "execution_id": "run1", "workflow_id": "wf1",
            "workflow_name": "demo", "total_steps": 2,
            "step_ids": ["s1", "s2"], "started_at": "t",
        })
        execu._emit("step_started", {
            "execution_id": "run1", "step_id": "s1", "step_type": "csv_source",
            "label": "load", "step_index": 0, "total_steps": 2,
        })
        execu._emit("step_completed", {
            "execution_id": "run1", "step_id": "s1", "step_type": "csv_source",
            "label": "load", "status": "success",
            "row_count": 42, "columns": ["a", "b"],
            "duration_ms": 12, "progress_pct": 50,
            "step_index": 0, "total_steps": 2, "memory_delta_mb": 1,
        })
        execu._emit("step_error", {
            "execution_id": "run1", "step_id": "s2", "step_type": "transform",
            "label": "tx", "error": "boom", "traceback": "...",
            "duration_ms": 7, "step_index": 1, "total_steps": 2,
        })
        execu._emit("workflow_completed", {
            "execution_id": "run1", "workflow_id": "wf1", "status": "error",
            "total_duration_ms": 100, "total_rows": 42,
            "step_results": {}, "total_steps": 2,
            "error": "boom",
        })

        # Legacy callback still fires for every emit (back-compat).
        assert len(legacy_seen) == 5

        # Wait for the LAST event to land on the metrics consumer.
        assert _wait_until(
            lambda: metrics.snapshot()["events_by_topic"].get(
                "fpulse.pipeline.run.failed"
            ) == 1
        )
        snap = metrics.snapshot()
        # All five typed events arrived under their canonical topics.
        topics = snap["events_by_topic"]
        assert topics.get("fpulse.pipeline.run.started") == 1
        assert topics.get("fpulse.step.started") == 1
        assert topics.get("fpulse.step.completed") == 1
        assert topics.get("fpulse.step.failed") == 1
        assert topics.get("fpulse.pipeline.run.failed") == 1
        # The csv_source duration was captured in the histogram.
        assert snap["step_duration_ms"]["csv_source"]["sum"] == 12
        # The failure was counted per step_type.
        assert snap["step_failures"]["transform"] == 1
    finally:
        bus.close()


def test_executor_without_bus_is_unchanged_legacy_behaviour():
    """The bus parameter is optional. Callers passing only on_event
    see exactly today's behaviour — no surprise publishes, no errors."""
    from fpulse.engine.realtime import RealtimeExecutor

    legacy_seen: list[dict] = []
    execu = RealtimeExecutor(data_dir=".", on_event=legacy_seen.append)
    # event_bus defaults to None — should be a no-op on the bus side.
    execu._emit("workflow_started", {
        "execution_id": "r", "workflow_id": "w", "workflow_name": "n",
        "total_steps": 0, "step_ids": [], "started_at": "t",
    })
    assert len(legacy_seen) == 1
    assert execu.event_bus is None


def test_executor_bus_publish_failure_does_not_break_emit():
    """A failing bus.publish() must never block the run. _emit swallows
    bus errors after firing on_event — the legacy contract is
    sacrosanct."""
    from fpulse.engine.realtime import RealtimeExecutor

    class BrokenBus:
        def publish(self, _ev):
            raise RuntimeError("bus is down")

    legacy_seen: list[dict] = []
    execu = RealtimeExecutor(
        data_dir=".", on_event=legacy_seen.append, event_bus=BrokenBus(),
    )
    execu._emit("workflow_started", {
        "execution_id": "r", "workflow_id": "w", "workflow_name": "n",
        "total_steps": 0, "step_ids": [], "started_at": "t",
    })
    # Legacy still got the event; bus error swallowed.
    assert len(legacy_seen) == 1
