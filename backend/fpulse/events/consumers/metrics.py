"""
MetricsConsumer — count events by topic, track durations, export
Prometheus text format.

Why this lives in events/consumers and not in engine/: the goal of
the EventBus is that adding a new observability path = adding one
file here that subscribes. The executor knows nothing about
Prometheus, doesn't import the metrics module, doesn't grow a new
`on_metric` callback. If you delete this file tomorrow, the
executor still runs.

## Exposed metrics

  fpulse_events_total{topic="..."}              counter
  fpulse_pipeline_run_duration_seconds          histogram (sum/count)
  fpulse_step_duration_seconds{step_type="..."} histogram (sum/count)
  fpulse_step_failures_total{step_type="..."}   counter

The histograms are simple sum/count (computed mean) — full bucket
distributions can be added later with `prometheus_client` if the
team brings that dep in. The point of the sketch is the pattern:
*observability lives downstream of the bus*.

## Usage

    from fpulse.events import get_event_bus
    from fpulse.events.consumers import MetricsConsumer

    metrics = MetricsConsumer()
    metrics.install(get_event_bus())

    # In an HTTP route:
    return Response(metrics.render(), media_type="text/plain")

The consumer is thread-safe (it's invoked from the bus dispatcher
thread, but render() can be called from a FastAPI worker thread).
"""

from __future__ import annotations

import collections
import threading
from typing import Optional

from ..bus import EventBus, Subscription
from ..types import (
    Event,
    PipelineRunCompleted,
    PipelineRunFailed,
    StepCompleted,
    StepFailed,
)


class MetricsConsumer:
    """In-memory metrics collector. Subscribes to the bus on install()."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Counter: events received, keyed by topic.
        self._events_by_topic: dict[str, int] = collections.defaultdict(int)
        # Histogram-ish: sum + count per step_type for duration.
        self._step_duration_sum_ms: dict[str, int] = collections.defaultdict(int)
        self._step_duration_count: dict[str, int] = collections.defaultdict(int)
        # Per-step-type failure counter.
        self._step_failures: dict[str, int] = collections.defaultdict(int)
        # Pipeline run duration.
        self._run_duration_sum_ms: int = 0
        self._run_duration_count: int = 0
        # Subscription handle so install() can be undone in tests.
        self._sub: Optional[Subscription] = None

    # ── Wiring ──

    def install(self, bus: EventBus) -> Subscription:
        """Subscribe to every fpulse event. Returns the subscription so
        callers can hold / cancel it; we also keep it on self for
        convenience."""
        self._sub = bus.subscribe("fpulse.>", self._handle)
        return self._sub

    def uninstall(self) -> None:
        if self._sub is not None:
            self._sub.cancel()
            self._sub = None

    # ── Event handling ──

    def _handle(self, event: Event) -> None:
        with self._lock:
            self._events_by_topic[event.topic] += 1
            if isinstance(event, StepCompleted):
                self._step_duration_sum_ms[event.step_type] += event.duration_ms
                self._step_duration_count[event.step_type] += 1
            elif isinstance(event, StepFailed):
                self._step_failures[event.step_type] += 1
            elif isinstance(event, (PipelineRunCompleted, PipelineRunFailed)):
                self._run_duration_sum_ms += event.duration_ms
                self._run_duration_count += 1

    # ── Read API ──

    def snapshot(self) -> dict:
        """Return a JSON-serializable snapshot of all metrics. Useful
        for tests and for the `/metrics.json` debug endpoint."""
        with self._lock:
            return {
                "events_by_topic": dict(self._events_by_topic),
                "step_duration_ms": {
                    step_type: {
                        "sum": self._step_duration_sum_ms[step_type],
                        "count": self._step_duration_count[step_type],
                    }
                    for step_type in self._step_duration_count
                },
                "step_failures": dict(self._step_failures),
                "run_duration_ms": {
                    "sum": self._run_duration_sum_ms,
                    "count": self._run_duration_count,
                },
            }

    def render(self) -> str:
        """Prometheus text exposition format. Mounted at /metrics."""
        with self._lock:
            lines: list[str] = []
            lines.append("# HELP fpulse_events_total Total events observed by topic.")
            lines.append("# TYPE fpulse_events_total counter")
            for topic, n in sorted(self._events_by_topic.items()):
                lines.append(f'fpulse_events_total{{topic="{topic}"}} {n}')

            lines.append("# HELP fpulse_step_duration_seconds Step execution duration.")
            lines.append("# TYPE fpulse_step_duration_seconds summary")
            for step_type in sorted(self._step_duration_count):
                sum_s = self._step_duration_sum_ms[step_type] / 1000.0
                count = self._step_duration_count[step_type]
                lines.append(
                    f'fpulse_step_duration_seconds_sum{{step_type="{step_type}"}} {sum_s:.6f}'
                )
                lines.append(
                    f'fpulse_step_duration_seconds_count{{step_type="{step_type}"}} {count}'
                )

            lines.append("# HELP fpulse_step_failures_total Failed steps by type.")
            lines.append("# TYPE fpulse_step_failures_total counter")
            for step_type, n in sorted(self._step_failures.items()):
                lines.append(f'fpulse_step_failures_total{{step_type="{step_type}"}} {n}')

            lines.append("# HELP fpulse_pipeline_run_duration_seconds Pipeline run duration.")
            lines.append("# TYPE fpulse_pipeline_run_duration_seconds summary")
            sum_s = self._run_duration_sum_ms / 1000.0
            lines.append(f"fpulse_pipeline_run_duration_seconds_sum {sum_s:.6f}")
            lines.append(
                f"fpulse_pipeline_run_duration_seconds_count {self._run_duration_count}"
            )

            return "\n".join(lines) + "\n"
