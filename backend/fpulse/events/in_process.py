"""
InProcessEventBus — the OSS default transport.

Durable events → SQLite append-only table.
Best-effort events → in-memory bounded queues.
Single process, zero external dependencies. Lives next to the
backend database; no extra service to install.

Threading model:
  - publish() is callable from any thread.
  - A single dispatcher thread drains the publish queue, persists
    durable events to SQLite, then fans out to subscriptions.
  - Sync subscribers are invoked inline on the dispatcher thread
    (handlers must be fast / non-blocking).
  - Async stream() consumers get their own asyncio.Queue and run
    in the caller's loop; the dispatcher thread feeds them via
    loop.call_soon_threadsafe.

Capacity:
  - Publish queue: unbounded (durable writes don't block the
    executor; if SQLite is slow, we'd rather buffer than drop).
  - Per-subscription best-effort queue: 1024 events. Overflow
    drops the *oldest* (keep recent telemetry over stale).
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
import queue
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Callable

from .bus import DurabilityClass, EventBus, Subscription, TopicPattern
from .types import Event, deserialize, serialize


log = logging.getLogger(__name__)

# Per-subscription bounded queue for best-effort delivery.
_BEST_EFFORT_QUEUE_MAX = 1024


def _topic_matches(pattern: TopicPattern, topic: str) -> bool:
    """NATS-style topic match.

    * matches one segment
    > matches one or more trailing segments
    """
    if pattern == topic:
        return True
    if pattern.endswith(".>"):
        prefix = pattern[:-2]
        return topic == prefix or topic.startswith(prefix + ".")
    if "*" in pattern:
        # Convert to fnmatch-friendly: NATS `*` ~ fnmatch `*` but
        # NATS `*` doesn't cross segment boundaries. Build a regex
        # via fnmatch.translate AFTER segment-splitting.
        p_parts = pattern.split(".")
        t_parts = topic.split(".")
        if len(p_parts) != len(t_parts):
            return False
        return all(
            fnmatch.fnmatchcase(t, p) and "." not in t
            for p, t in zip(p_parts, t_parts)
        )
    return False


class _InProcSubscription(Subscription):
    """Subscription handle for sync callbacks or async streams."""

    def __init__(
        self,
        sub_id: str,
        pattern: TopicPattern,
        bus: "InProcessEventBus",
        handler: Callable[[Event], None] | None = None,
        async_queue: asyncio.Queue | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        self.sub_id = sub_id
        self.pattern = pattern
        self._bus = bus
        self.handler = handler
        self.async_queue = async_queue
        self.loop = loop
        self._cancelled = False

    def cancel(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        self._bus._remove_subscription(self.sub_id)

    @property
    def cancelled(self) -> bool:
        return self._cancelled


class InProcessEventBus(EventBus):
    """Single-process pub/sub. The OSS-tier default.

    Args:
      db_path: Path to the SQLite file that stores durable events.
        If ``None`` (default), durable events go to an in-memory
        SQLite database — useful for tests but loses durability
        guarantees across process restarts.
    """

    def __init__(self, db_path: str | Path | None = None):
        self._db_path = str(db_path) if db_path else ":memory:"
        self._subs: dict[str, _InProcSubscription] = {}
        self._subs_lock = threading.Lock()
        self._publish_q: queue.Queue[Event | None] = queue.Queue()
        self._dispatcher: threading.Thread | None = None
        self._dispatcher_lock = threading.Lock()
        self._stopped = threading.Event()
        self._started = False
        # One long-lived connection per bus instance. Required for
        # ":memory:" (which is connection-scoped) and avoids
        # per-write open/close overhead on file-backed DBs. We use
        # check_same_thread=False + an explicit lock so publish()
        # (any thread) and _replay() (caller's loop thread) can
        # share the connection safely.
        self._conn: sqlite3.Connection | None = None
        self._db_lock = threading.Lock()

    # ── Lifecycle ──

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._open_db()
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            name="fpulse-events-dispatcher",
            daemon=True,
        )
        self._dispatcher.start()

    def close(self) -> None:
        if not self._started or self._stopped.is_set():
            return
        self._stopped.set()
        self._publish_q.put(None)  # sentinel
        if self._dispatcher and self._dispatcher.is_alive():
            self._dispatcher.join(timeout=2.0)
        # Release the SQLite file handle so the OS can clean up
        # the tempfile (Windows otherwise holds the lock until
        # the conn is GC'd, which breaks tempdir cleanup).
        with self._db_lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def _open_db(self) -> None:
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fpulse_event_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fpulse_event_log_topic "
            "ON fpulse_event_log(topic, id)"
        )
        self._conn.commit()

    # ── Publishing ──

    def publish(self, event: Event) -> None:
        if not self._started:
            self.start()
        self._ensure_dispatcher()
        # Durable: synchronous write so the caller gets a hard
        # ack before we even consider dispatch. Mirrors NATS
        # JetStream's publish-with-ack semantics in Plus.
        if event.DURABILITY is DurabilityClass.DURABLE:
            cursor = self._write_durable(event)
            event.cursor = cursor
        # Both classes go through the same dispatcher — the only
        # difference is the durable write above.
        self._publish_q.put(event)

    def _write_durable(self, event: Event) -> str:
        line = serialize(event)
        with self._db_lock:
            assert self._conn is not None, "bus not started"
            cur = self._conn.execute(
                "INSERT INTO fpulse_event_log (topic, occurred_at, payload) "
                "VALUES (?, ?, ?)",
                (event.topic, event.occurred_at, line),
            )
            self._conn.commit()
            return str(cur.lastrowid)

    # ── Subscribing ──

    def subscribe(
        self,
        topic_pattern: TopicPattern,
        handler: Callable[[Event], None],
    ) -> Subscription:
        if not self._started:
            self.start()
        sub_id = str(uuid.uuid4())
        sub = _InProcSubscription(
            sub_id=sub_id, pattern=topic_pattern, bus=self, handler=handler,
        )
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
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        sub_id = str(uuid.uuid4())
        sub = _InProcSubscription(
            sub_id=sub_id,
            pattern=topic_pattern,
            bus=self,
            async_queue=q,
            loop=loop,
        )
        with self._subs_lock:
            self._subs[sub_id] = sub

        async def _iter() -> AsyncIterator[Event]:
            try:
                # Replay durable events from the cursor first.
                if since is not None:
                    for ev in self._replay(topic_pattern, since):
                        yield ev
                # Then tail live.
                while not sub.cancelled:
                    ev = await q.get()
                    if ev is None:  # close sentinel
                        return
                    yield ev
            finally:
                sub.cancel()

        return _iter()

    def _replay(self, pattern: TopicPattern, since: str) -> list[Event]:
        """Pull DURABLE events from the log strictly after `since`."""
        try:
            since_id = int(since)
        except ValueError:
            return []
        with self._db_lock:
            assert self._conn is not None, "bus not started"
            rows = self._conn.execute(
                "SELECT id, payload FROM fpulse_event_log WHERE id > ? ORDER BY id ASC",
                (since_id,),
            ).fetchall()
        out: list[Event] = []
        for rowid, payload in rows:
            ev = deserialize(payload)
            if _topic_matches(pattern, ev.topic):
                ev.cursor = str(rowid)
                out.append(ev)
        return out

    def _remove_subscription(self, sub_id: str) -> None:
        with self._subs_lock:
            sub = self._subs.pop(sub_id, None)
        # Wake any async iterator waiting on the queue.
        if sub is not None and sub.async_queue is not None and sub.loop is not None:
            with contextlib.suppress(RuntimeError):
                sub.loop.call_soon_threadsafe(sub.async_queue.put_nowait, None)

    # ── Dispatcher ──

    def _ensure_dispatcher(self) -> None:
        """Self-heal: restart the dispatcher thread if it died.

        A dead dispatcher otherwise turns publish() into a silent black
        hole — events queue forever and no consumer (audit, metrics,
        websocket streams) ever sees them, while the app looks healthy.
        """
        d = self._dispatcher
        if self._stopped.is_set() or (d is not None and d.is_alive()):
            return
        with self._dispatcher_lock:
            d = self._dispatcher
            if self._stopped.is_set() or (d is not None and d.is_alive()):
                return
            log.error("events dispatcher thread died — restarting")
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop,
                name="fpulse-events-dispatcher",
                daemon=True,
            )
            self._dispatcher.start()

    def _dispatch_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                event = self._publish_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:  # sentinel
                break
            try:
                self._fanout(event)
            except Exception:
                # A single bad event must not kill the dispatcher — that
                # would silently disconnect every consumer.
                log.exception(
                    "event dispatch failed (topic=%s)",
                    getattr(event, "topic", "?"),
                )

    def _fanout(self, event: Event) -> None:
        with self._subs_lock:
            subs = list(self._subs.values())
        for sub in subs:
            if not _topic_matches(sub.pattern, event.topic):
                continue
            if sub.handler is not None:
                self._call_handler(sub, event)
            elif sub.async_queue is not None and sub.loop is not None:
                self._enqueue_async(sub, event)

    def _call_handler(self, sub: _InProcSubscription, event: Event) -> None:
        try:
            sub.handler(event)  # type: ignore[misc]
        except Exception:
            # One bad subscriber must not poison the others.
            log.exception(
                "event handler raised (sub=%s topic=%s)",
                sub.sub_id, event.topic,
            )

    def _enqueue_async(self, sub: _InProcSubscription, event: Event) -> None:
        # Best-effort: if the consumer is slow, drop oldest.
        q = sub.async_queue
        loop = sub.loop
        if q is None or loop is None:
            return

        def _put() -> None:
            if q.qsize() >= _BEST_EFFORT_QUEUE_MAX and event.DURABILITY is DurabilityClass.BEST_EFFORT:
                try:
                    q.get_nowait()  # drop oldest
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(event)

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_put)
