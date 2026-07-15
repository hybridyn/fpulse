"""
fpulse.events — pub/sub event bus for pipeline state, logs, metrics.

## Why this package exists

The existing executor (engine/realtime.py) wires a single `on_event`
callback through every level of the call stack. That's point-to-point
coupling: the WebSocket streamer, the lineage indexer, the metrics
roll-up, the audit log, the alert engine all need to know about runs
— but the executor only knows about one of them at a time.

This package replaces that with a topic-based bus. The executor
publishes; everyone who cares subscribes. Adding a new consumer
(say, an OpenLineage emitter, or a Prometheus collector) becomes a
one-file change that doesn't touch the executor.

## OSS vs Plus

Same `EventBus` interface, two implementations:

- **InProcessEventBus** (OSS default): SQLite-backed durable log +
  in-memory asyncio queues for best-effort delivery. Zero external
  dependencies. Single binary keeps working.

- **NatsEventBus** (Plus, stubbed): NATS JetStream for durable
  events, NATS Core for best-effort. Cross-process, cross-host,
  replayable.

Business logic never imports either implementation directly — it
calls `get_event_bus()` from `.factory`, which reads `FPULSE_EVENT_BUS`
env (`"inprocess"` / `"nats"`) and hands back the right one.

## Durability classes

Not every event matters equally:

- **DURABLE**: state changes that *must* survive restart and *must*
  be re-deliverable to subscribers that come online later (run
  started/completed/failed, approval granted/denied). Goes to the
  SQLite event log; NATS JetStream in Plus.
- **BEST_EFFORT**: high-volume telemetry that can drop under load
  without breaking correctness (step progress, log lines, metric
  samples). Goes to an in-memory bounded queue; NATS Core in Plus.

Pick the wrong class and you either flood the disk (best-effort
written as durable) or lose a "step failed" event (durable written
as best-effort). The class lives on the event type itself — see
`types.py` — so callers can't get it wrong.

## What does NOT belong on the bus

- **Synchronous request/response** (e.g. "fetch this row from the
  cache"). The bus is one-way pub/sub. Use a normal function call.
- **Anything in the executor's critical path that *blocks the run*
  on delivery**. Publish is fire-and-forget; if you need a synchronous
  contract, it's not an event.
- **Large payloads** (>~16 KB per event). Put the blob in
  StepOutputStore / object storage, publish a reference.
- **AI Copilot suggestions**. Those are pre/post-flight features
  that go through their own request/response API — keeping AI out
  of the executor's event path is what makes the executor cheap to
  reason about.
"""

from .bus import (
    EventBus,
    Subscription,
    DurabilityClass,
    TopicPattern,
)
from .types import (
    Event,
    PipelineRunStarted,
    PipelineRunCompleted,
    PipelineRunFailed,
    PipelineRunCancelled,
    StepStarted,
    StepCompleted,
    StepFailed,
    StepSkipped,
    StepProgress,
    ApprovalRequested,
    ApprovalGranted,
    ApprovalDenied,
    AlertFired,
    SchemaDriftDetected,
)
from .factory import get_event_bus

__all__ = [
    "EventBus",
    "Subscription",
    "DurabilityClass",
    "TopicPattern",
    "Event",
    "PipelineRunStarted",
    "PipelineRunCompleted",
    "PipelineRunFailed",
    "PipelineRunCancelled",
    "StepStarted",
    "StepCompleted",
    "StepFailed",
    "StepSkipped",
    "StepProgress",
    "ApprovalRequested",
    "ApprovalGranted",
    "ApprovalDenied",
    "AlertFired",
    "SchemaDriftDetected",
    "get_event_bus",
]
