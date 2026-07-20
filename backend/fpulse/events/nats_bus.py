"""
NatsEventBus — Plus-tier transport, distributed pub/sub over NATS.

Same public surface as InProcessEventBus. Selected by
``FPULSE_EVENT_BUS=nats`` via factory.get_event_bus.

## Mapping to NATS primitives

  Durability         NATS feature
  ─────────────────  ─────────────────────────────────────────────
  DURABLE            JetStream stream "fpulse-evt" + ack on publish
  BEST_EFFORT        NATS Core (fire-and-forget pub/sub)

Both publish to the same subject — `event.topic`. Durable events
are additionally written to JetStream so subscribers can replay
from a cursor. Live subscribers via `subscribe()` listen on Core
and pay nothing for replay they don't want; `stream(since=...)`
subscribers attach to JetStream and get durable replay.

## Sync ↔ async bridging

The interface is sync (`publish()` returns when the caller is
done). nats-py is async-only. The bus owns a dedicated asyncio
loop in a background thread; sync calls schedule coroutines onto
that loop via `asyncio.run_coroutine_threadsafe` and either
block on the result (durable publish, subscribe setup) or fire
and forget (best-effort publish).

`stream()` returns an async iterator. The bus delivers messages
into a queue bound to the *caller's* loop using
`loop.call_soon_threadsafe`, so async iteration in the caller's
loop just works without cross-loop awaits.

## Install

``pip install "nats-py>=2.6"`` — not pulled in by the OSS
requirements file. The import is lazy so OSS users who never
flip ``FPULSE_EVENT_BUS=nats`` don't pay for the dep.

## Stream configuration

First time the bus starts, it ensures a JetStream stream with
subjects=["fpulse.>"], retention=limits, max_age=7d. Tune via
``FPULSE_NATS_STREAM`` and ``FPULSE_NATS_MAX_AGE_SECONDS`` env.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import uuid
from typing import AsyncIterator, Callable

from .bus import DurabilityClass, EventBus, Subscription, TopicPattern
from .types import Event, deserialize, serialize


log = logging.getLogger(__name__)

# Default JetStream stream retention. 7 days of durable events is
# enough for typical replay use cases (lineage rebuild, audit
# review, late-arriving consumer catch-up). Override via
# FPULSE_NATS_MAX_AGE_SECONDS at construction time.
_DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 3600

# How long we wait for the JetStream ack on a durable publish.
# 10 s is a generous WAN budget; under normal load this returns
# in single-digit ms. Raise it if the cluster's far away.
_DURABLE_PUBLISH_TIMEOUT = 10.0

# Setup operations (connect, ensure stream, attach subscription)
# get the same timeout — they should be much faster, but a slow
# cluster on first connection can take seconds.
_SETUP_TIMEOUT = 10.0


class _NatsSubscription(Subscription):
    """Handle returned from subscribe()/stream(). Owns the
    underlying NATS subscription so cancel() can unsubscribe."""

    def __init__(self, sub_id: str, bus: "NatsEventBus"):
        self.sub_id = sub_id
        self._bus = bus
        self._nats_sub: object | None = None  # nats.aio.subscription.Subscription
        self._cancelled = False
        # For stream() subscriptions: the caller-loop queue we
        # signal on cancel so the async iterator can exit cleanly.
        self._caller_queue: asyncio.Queue | None = None
        self._caller_loop: asyncio.AbstractEventLoop | None = None

    def cancel(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        self._bus._remove_subscription(self.sub_id)

    @property
    def cancelled(self) -> bool:
        return self._cancelled


class NatsEventBus(EventBus):
    """Distributed pub/sub over NATS + JetStream. See module docstring."""

    def __init__(
        self,
        servers: list[str] | None = None,
        stream_name: str = "fpulse-evt",
        durable_subject_filter: str = "fpulse.>",
        max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
    ):
        self._servers = servers or ["nats://localhost:4222"]
        self._stream_name = stream_name
        self._durable_subject_filter = durable_subject_filter
        self._max_age_seconds = max_age_seconds

        # Filled in by start().
        self._nc = None  # nats.aio.client.Client
        self._js = None  # nats.js.JetStreamContext
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

        self._subs: dict[str, _NatsSubscription] = {}
        self._subs_lock = threading.Lock()
        self._started = False
        self._stopped = threading.Event()

    # ── Lifecycle ──

    def start(self) -> None:
        if self._started:
            return
        try:
            import nats  # noqa: F401 — import-time check, real import in coroutine
        except ImportError as e:
            raise RuntimeError(
                "FPULSE_EVENT_BUS=nats requires `nats-py`. "
                "Install with `pip install \"nats-py>=2.6\"`."
            ) from e

        # Spin up a dedicated event loop in a background thread.
        # Every NATS coroutine in this bus runs on THIS loop —
        # caller threads schedule onto it via run_coroutine_threadsafe.
        ready = threading.Event()

        def _run_loop() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            ready.set()
            self._loop.run_forever()

        self._loop_thread = threading.Thread(
            target=_run_loop, name="fpulse-events-nats-loop", daemon=True,
        )
        self._loop_thread.start()
        ready.wait()

        # Connect + ensure the JetStream stream exists. Block here
        # so callers know start() succeeded synchronously.
        fut = asyncio.run_coroutine_threadsafe(self._connect_and_provision(), self._loop)
        fut.result(timeout=_SETUP_TIMEOUT)
        self._started = True

    async def _connect_and_provision(self) -> None:
        import nats
        from nats.js.api import RetentionPolicy, StreamConfig
        from nats.js.errors import NotFoundError

        self._nc = await nats.connect(servers=self._servers)
        self._js = self._nc.jetstream()
        # Ensure the durable stream. Treat "already exists" as success.
        try:
            await self._js.stream_info(self._stream_name)
        except NotFoundError:
            await self._js.add_stream(StreamConfig(
                name=self._stream_name,
                subjects=[self._durable_subject_filter],
                # nats-py expresses max_age in nanoseconds.
                max_age=self._max_age_seconds * 1_000_000_000,
                retention=RetentionPolicy.LIMITS,
            ))

    def close(self) -> None:
        if not self._started or self._stopped.is_set():
            return
        self._stopped.set()
        # Drain NATS, unsubscribe everything, then stop the loop.
        if self._loop is not None and self._loop.is_running():
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(
                    self._disconnect(), self._loop,
                ).result(timeout=_SETUP_TIMEOUT)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread is not None and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=2.0)

    async def _disconnect(self) -> None:
        # Cancel each subscription's underlying NATS sub before we
        # close the client — drain otherwise complains about live
        # consumers.
        with self._subs_lock:
            subs = list(self._subs.values())
        for sub in subs:
            if sub._nats_sub is not None:
                with contextlib.suppress(Exception):
                    await sub._nats_sub.unsubscribe()
            # Signal any waiting stream() iterator to exit.
            if sub._caller_queue is not None and sub._caller_loop is not None:
                with contextlib.suppress(RuntimeError):
                    sub._caller_loop.call_soon_threadsafe(
                        sub._caller_queue.put_nowait, None,
                    )
        if self._nc is not None:
            with contextlib.suppress(Exception):
                await self._nc.drain()

    # ── Publishing ──

    def publish(self, event: Event) -> None:
        if not self._started:
            self.start()
        assert self._loop is not None  # for type checkers
        if event.DURABILITY is DurabilityClass.DURABLE:
            fut = asyncio.run_coroutine_threadsafe(
                self._publish_durable(event), self._loop,
            )
            seq = fut.result(timeout=_DURABLE_PUBLISH_TIMEOUT)
            event.cursor = str(seq)
        else:
            # Fire-and-forget. Don't await the schedule, don't
            # block the caller. Errors are logged inside the coro.
            asyncio.run_coroutine_threadsafe(
                self._publish_core(event), self._loop,
            )

    async def _publish_durable(self, event: Event) -> int:
        """Publish via JetStream (durable) AND Core (live fan-out).

        JS write provides the durability + cursor; Core publish
        ensures live `subscribe()` consumers receive the event
        without paying for JS replay machinery.
        """
        payload = serialize(event).encode("utf-8")
        ack = await self._js.publish(event.topic, payload)
        # Best-effort live fan-out; if Core publish fails, the
        # durable write already succeeded — caller's contract is
        # satisfied. Log + swallow.
        try:
            await self._nc.publish(event.topic, payload)
        except Exception:
            log.exception("core fan-out publish failed (topic=%s)", event.topic)
        return ack.seq

    async def _publish_core(self, event: Event) -> None:
        payload = serialize(event).encode("utf-8")
        try:
            await self._nc.publish(event.topic, payload)
        except Exception:
            # BEST_EFFORT contract: never raise back to the caller.
            log.exception("best-effort publish failed (topic=%s)", event.topic)

    # ── Subscribing ──

    def subscribe(
        self,
        topic_pattern: TopicPattern,
        handler: Callable[[Event], None],
    ) -> Subscription:
        if not self._started:
            self.start()
        assert self._loop is not None

        sub_id = str(uuid.uuid4())
        sub = _NatsSubscription(sub_id, self)

        async def _attach() -> None:
            async def _cb(msg) -> None:
                try:
                    event = deserialize(msg.data.decode("utf-8"))
                except Exception:
                    log.exception("dropped malformed NATS message on %s", msg.subject)
                    return
                # Handlers may be slow / blocking. Run on the
                # loop's default executor so the NATS dispatcher
                # task stays free.
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, handler, event,
                    )
                except Exception:
                    log.exception("sync handler raised (sub=%s)", sub_id)

            sub._nats_sub = await self._nc.subscribe(topic_pattern, cb=_cb)

        fut = asyncio.run_coroutine_threadsafe(_attach(), self._loop)
        fut.result(timeout=_SETUP_TIMEOUT)

        with self._subs_lock:
            self._subs[sub_id] = sub
        return sub

    def stream(
        self,
        topic_pattern: TopicPattern,
        *,
        since: str | None = None,
    ) -> AsyncIterator[Event]:
        if not self._started:
            self.start()
        assert self._loop is not None
        caller_loop = asyncio.get_running_loop()
        caller_q: asyncio.Queue = asyncio.Queue()

        sub_id = str(uuid.uuid4())
        sub = _NatsSubscription(sub_id, self)
        sub._caller_queue = caller_q
        sub._caller_loop = caller_loop

        async def _attach() -> None:
            from nats.js.api import ConsumerConfig, DeliverPolicy

            if since is not None:
                try:
                    opt_start_seq = int(since) + 1
                except ValueError:
                    opt_start_seq = None
                if opt_start_seq is not None:
                    config = ConsumerConfig(
                        deliver_policy=DeliverPolicy.BY_START_SEQUENCE,
                        opt_start_seq=opt_start_seq,
                    )
                else:
                    config = ConsumerConfig(deliver_policy=DeliverPolicy.NEW)
            else:
                config = ConsumerConfig(deliver_policy=DeliverPolicy.NEW)

            async def _cb(msg) -> None:
                try:
                    event = deserialize(msg.data.decode("utf-8"))
                    if msg.metadata is not None:
                        event.cursor = str(msg.metadata.sequence.stream)
                    # Hop into the caller's loop. asyncio.Queue is
                    # not thread-safe, but call_soon_threadsafe is.
                    caller_loop.call_soon_threadsafe(caller_q.put_nowait, event)
                    await msg.ack()
                except Exception:
                    log.exception("stream dispatch failed on %s", msg.subject)
                    with contextlib.suppress(Exception):
                        await msg.nak()

            sub._nats_sub = await self._js.subscribe(
                topic_pattern, cb=_cb, config=config,
            )

        attach_fut = asyncio.run_coroutine_threadsafe(_attach(), self._loop)

        async def _iter() -> AsyncIterator[Event]:
            try:
                # Wait for the JS subscription to actually be live
                # before we start awaiting messages. Done as an
                # awaitable in the caller's loop so we don't block
                # the event loop on .result().
                await asyncio.get_running_loop().run_in_executor(
                    None, attach_fut.result, _SETUP_TIMEOUT,
                )
                with self._subs_lock:
                    self._subs[sub_id] = sub
                while not sub.cancelled:
                    event = await caller_q.get()
                    if event is None:  # close sentinel
                        return
                    yield event
            finally:
                sub.cancel()

        return _iter()

    # ── Internal ──

    def _remove_subscription(self, sub_id: str) -> None:
        with self._subs_lock:
            sub = self._subs.pop(sub_id, None)
        if sub is None:
            return
        # Tear down the NATS subscription on the bus loop.
        if sub._nats_sub is not None and self._loop is not None and self._loop.is_running():
            async def _unsub() -> None:
                with contextlib.suppress(Exception):
                    await sub._nats_sub.unsubscribe()
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(_unsub(), self._loop)
        # Wake any pending stream() iterator.
        if sub._caller_queue is not None and sub._caller_loop is not None:
            with contextlib.suppress(RuntimeError):
                sub._caller_loop.call_soon_threadsafe(
                    sub._caller_queue.put_nowait, None,
                )
