"""
EventBus interface — what every transport implementation must satisfy.

Keeping this file dependency-free is deliberate: anything that
imports an event-bus transport (in_process, nats_bus) drags in
sqlite / asyncio / nats-py. Anything that imports *just the bus
interface* should stay light, so engine/executor.py can publish
events without paying that cost on import.
"""

from __future__ import annotations

import abc
import enum
from typing import AsyncIterator, Callable, Protocol


# Topic-pattern grammar mirrors NATS:
#   "fpulse.pipeline.run.started"     — exact match
#   "fpulse.pipeline.run.*"           — single-segment wildcard
#   "fpulse.pipeline.>"               — multi-segment wildcard
# Implementations must support all three. Dot-separated, lowercase,
# `>` is only valid as the last segment.
TopicPattern = str


class DurabilityClass(enum.Enum):
    """Two-tier QoS. Picks the storage backend at publish time.

    Set on the Event subclass (`DURABILITY = DurabilityClass.DURABLE`),
    not at the call site — wrong class is a bug we want to catch in
    code review, not at runtime."""

    # Persisted to disk before publish returns; subscribers that
    # connect later can replay. Use for state changes the system
    # must never silently lose: run lifecycle, approvals, alerts
    # that have already fired.
    DURABLE = "durable"

    # Fire-and-forget. Dropped on overflow / network blip. Use for
    # high-volume telemetry: step progress, log lines, metric
    # samples. Losing one is fine; losing them all means turn up
    # observability separately.
    BEST_EFFORT = "best_effort"


class Subscription(abc.ABC):
    """Handle returned by subscribe(). Holding it keeps the
    subscription alive; calling .cancel() detaches the handler."""

    @abc.abstractmethod
    def cancel(self) -> None:
        ...


class EventBus(abc.ABC):
    """Pub/sub bus. One interface, multiple transports.

    Publishers call publish(). Subscribers call subscribe() (sync
    callback) or stream() (async iterator). Both transports must
    implement these four methods identically — that's the whole
    point of the abstraction.

    Threading model:
      - publish() is safe to call from any thread, any time.
      - subscribe() handlers are invoked on a *background* worker
        thread/task owned by the bus. Handlers must not block the
        main event loop and must not raise (raised exceptions get
        logged and the handler stays subscribed).
      - stream() yields events to the caller's coroutine; the
        caller drives the iteration cadence, so it's the natural
        choice for back-pressure-aware consumers.

    Topic conventions: dot-separated, lowercase. F-Pulse uses the
    `fpulse.` prefix on everything it owns — see types.py.
    """

    # ── Publishing ──

    @abc.abstractmethod
    def publish(self, event: "Event") -> None:
        """Publish an event. Returns immediately.

        For DURABLE events: the call blocks just long enough to
        write to the durable log, then returns; subscriber dispatch
        is async. If the write fails, the call *raises* — callers
        in the executor MUST handle this (logging + retry of the
        publish itself, not the underlying business operation).

        For BEST_EFFORT events: enqueued and returned. Drops
        silently if the bus is overloaded — caller never sees it.
        """
        ...

    # ── Subscribing ──

    @abc.abstractmethod
    def subscribe(
        self,
        topic_pattern: TopicPattern,
        handler: Callable[["Event"], None],
    ) -> Subscription:
        """Register a synchronous handler for events matching the pattern.

        Handler is called from a bus-owned worker thread. Do not
        block the asyncio loop from inside it (e.g. don't call
        `loop.run_until_complete`). If the handler raises, the bus
        logs and continues — one bad subscriber does not affect
        the others.
        """
        ...

    @abc.abstractmethod
    def stream(
        self,
        topic_pattern: TopicPattern,
        *,
        since: str | None = None,
    ) -> AsyncIterator["Event"]:
        """Async iterator over matching events.

        `since`: opaque cursor returned by a previous Event's
        `cursor` attribute. Only meaningful for DURABLE events —
        BEST_EFFORT has no replay. Pass `None` for "from now on".

        Use this when you want explicit back-pressure: the bus
        won't deliver event N+1 until your `async for` loop is
        ready for it.
        """
        ...

    # ── Lifecycle ──

    @abc.abstractmethod
    def start(self) -> None:
        """Open transport connections, spin up worker threads. Safe
        to call multiple times — second call is a no-op."""
        ...

    @abc.abstractmethod
    def close(self) -> None:
        """Drain pending publishes, cancel subscriptions, release
        resources. Safe to call multiple times."""
        ...


class _EventLike(Protocol):
    """Structural type used internally by transports — avoids a
    circular import with types.py. Real events extend the `Event`
    dataclass in types.py."""

    topic: str
    cursor: str | None
    DURABILITY: DurabilityClass


# Forward declaration alias, so callers can `from .bus import Event`
# even though the concrete class lives in types.py.
Event = _EventLike  # type: ignore[assignment]
