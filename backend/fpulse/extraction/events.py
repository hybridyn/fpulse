"""Observability bus for extraction runs.

Replaces the bare `on_event` callback with a proper pub-sub channel
so multiple consumers (logger, state aggregator, SSE endpoint, UI
panel) can subscribe to the same event stream without coupling to
each other.

Design choices:
  - Per-run state is aggregated as events arrive — the API doesn't
    need to scan the whole event history to render "47% complete,
    current concurrency 9, 12 in DLQ"; it just reads the snapshot.
  - Events are kept in a bounded ring buffer per run so the UI can
    page back through recent activity without unbounded memory.
  - The bus is process-local — multi-worker deployments use the bus
    plus a side-channel (Redis pub-sub or similar) to fan out across
    workers. Phase 5 if/when needed.

Event types are an enum-string for cheap filtering:
    started · list_phase_start · list_phase_done ·
    enrichment_phase_start · progress · concurrency_changed ·
    rate_limited · auth_refreshed · item_failed · checkpoint ·
    enrichment_phase_done · completed · failed
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ── Event shape ──────────────────────────────────────────────────────

@dataclass
class ExtractionEvent:
    run_id: str
    profile: str
    kind: str
    ts: float
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "profile": self.profile,
            "kind": self.kind, "ts": self.ts, "payload": self.payload,
        }


# ── Per-run aggregated state ────────────────────────────────────────

@dataclass
class RunState:
    """Live snapshot of one extraction run, updated as events arrive.

    The UI reads this via `EventBus.snapshot(run_id)` rather than
    replaying events. ETA estimation is naive (linear extrapolation
    from last progress event) — good enough for "is this still
    making progress?" but not for forecasting.
    """
    run_id: str
    profile: str
    started_at: float = 0.0
    completed_at: float | None = None
    phase: str = "starting"
    listed: int = 0
    extracted: int = 0
    failed: int = 0
    skipped_resumed: int = 0
    concurrency: int = 0
    rate_limited_count: int = 0
    auth_refreshed_count: int = 0
    last_event_at: float = 0.0
    error: str | None = None

    def progress_fraction(self) -> float | None:
        if self.listed == 0:
            return None
        return min(1.0, (self.extracted + self.failed) / self.listed)

    def eta_seconds(self) -> float | None:
        """Naive linear ETA. Returns None when there's not enough data."""
        if self.completed_at:
            return 0.0
        done = self.extracted + self.failed
        if done == 0 or self.listed == 0:
            return None
        elapsed = (self.last_event_at or time.time()) - self.started_at
        if elapsed <= 0:
            return None
        rate = done / elapsed
        if rate <= 0:
            return None
        remaining = self.listed - done
        return remaining / rate

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "profile": self.profile,
            "started_at": self.started_at, "completed_at": self.completed_at,
            "phase": self.phase,
            "listed": self.listed, "extracted": self.extracted,
            "failed": self.failed, "skipped_resumed": self.skipped_resumed,
            "concurrency": self.concurrency,
            "rate_limited_count": self.rate_limited_count,
            "auth_refreshed_count": self.auth_refreshed_count,
            "last_event_at": self.last_event_at,
            "error": self.error,
            "progress": self.progress_fraction(),
            "eta_seconds": self.eta_seconds(),
        }


# ── The bus ──────────────────────────────────────────────────────────

class EventBus:
    """Process-local pub-sub for extraction events.

    Concurrency model: the engine publishes from whichever loop is
    running; subscribers are called synchronously inside `publish`.
    For long-running subscribers (writing to disk, pushing to a
    websocket), the subscriber should hand off to its own queue and
    return quickly — `publish` shouldn't be blocked by slow consumers.
    """

    def __init__(self, *, history_size: int = 500) -> None:
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[ExtractionEvent], None]] = []
        self._states: dict[str, RunState] = {}
        # Per-run ring buffers. Keyed by run_id; each holds the last
        # N events for that run. Old runs evict from the registry on
        # an LRU schedule via `_evict_if_needed`.
        self._history: dict[str, deque[ExtractionEvent]] = defaultdict(
            lambda: deque(maxlen=history_size)
        )
        self._history_size = history_size
        self._max_runs_in_memory = 100

    # ── Subscriptions ────────────────────────────────────────────────

    def subscribe(self, fn: Callable[[ExtractionEvent], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(fn)

        def _unsubscribe() -> None:
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)

        return _unsubscribe

    # ── Publishing ───────────────────────────────────────────────────

    def publish(self, event: ExtractionEvent) -> None:
        with self._lock:
            self._history[event.run_id].append(event)
            self._update_state(event)
            self._evict_if_needed()
            subs = list(self._subscribers)
        for fn in subs:
            try:
                fn(event)
            except Exception:  # noqa: BLE001 — never fail publish on slow subscriber
                logger.exception("event subscriber raised")

    # ── State derivation ─────────────────────────────────────────────

    def _update_state(self, event: ExtractionEvent) -> None:
        st = self._states.setdefault(
            event.run_id,
            RunState(run_id=event.run_id, profile=event.profile,
                     started_at=event.ts),
        )
        st.last_event_at = event.ts
        p = event.payload

        if event.kind == "started":
            st.started_at = event.ts
            st.phase = "starting"
        elif event.kind == "list_phase_start":
            st.phase = "list"
        elif event.kind == "list_phase_done":
            st.listed = int(p.get("id_count", st.listed))
        elif event.kind == "enrichment_phase_start":
            st.phase = "enrichment"
            st.concurrency = int(p.get("initial_concurrency", st.concurrency))
            if "target_count" in p:
                st.listed = int(p["target_count"])
        elif event.kind == "progress":
            st.extracted = int(p.get("extracted", st.extracted))
            st.failed = int(p.get("failed", st.failed))
            if "concurrency" in p:
                st.concurrency = int(p["concurrency"])
        elif event.kind == "concurrency_changed":
            st.concurrency = int(p.get("current", st.concurrency))
        elif event.kind == "rate_limited":
            st.rate_limited_count += 1
        elif event.kind == "auth_refreshed":
            st.auth_refreshed_count += 1
        elif event.kind == "item_failed":
            st.failed += 1
        elif event.kind == "enrichment_phase_done":
            st.extracted = int(p.get("succeeded", st.extracted))
            st.failed = int(p.get("failed", st.failed))
            st.skipped_resumed = int(p.get("skipped_resumed", st.skipped_resumed))
            st.concurrency = int(p.get("final_concurrency", st.concurrency))
        elif event.kind == "completed":
            st.completed_at = event.ts
            st.phase = "completed"
            st.extracted = int(p.get("extracted", st.extracted))
            st.failed = int(p.get("failed", st.failed))
        elif event.kind == "failed":
            st.completed_at = event.ts
            st.phase = "failed"
            st.error = p.get("error")

    def _evict_if_needed(self) -> None:
        """Drop old completed runs when we exceed the in-memory cap.
        Active (uncompleted) runs are never evicted."""
        if len(self._states) <= self._max_runs_in_memory:
            return
        completed = [
            (rid, st) for rid, st in self._states.items()
            if st.completed_at is not None
        ]
        completed.sort(key=lambda x: x[1].completed_at or 0.0)
        for rid, _ in completed[: len(self._states) - self._max_runs_in_memory]:
            self._states.pop(rid, None)
            self._history.pop(rid, None)

    # ── Read API ─────────────────────────────────────────────────────

    def snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            st = self._states.get(run_id)
            return st.to_dict() if st else None

    def list_runs(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            runs = list(self._states.values())
        if active_only:
            runs = [r for r in runs if r.completed_at is None]
        runs.sort(key=lambda r: r.started_at, reverse=True)
        return [r.to_dict() for r in runs]

    def history(self, run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            buf = list(self._history.get(run_id, []))
        return [e.to_dict() for e in buf[-limit:]]


# ── Module singleton ────────────────────────────────────────────────

_BUS: EventBus | None = None


def get_bus() -> EventBus:
    """Process-local event bus singleton.

    Tests should construct their own EventBus and pass it explicitly
    rather than rely on the singleton; this keeps test isolation
    clean. Production code uses get_bus() so subscribers wired at
    startup share the same channel.
    """
    global _BUS
    if _BUS is None:
        _BUS = EventBus()
    return _BUS


def reset_bus_for_tests() -> None:
    """Clear the singleton — call from test fixtures only."""
    global _BUS
    _BUS = None


# ── Helper used by the engine ───────────────────────────────────────

def make_run_id() -> str:
    return uuid.uuid4().hex[:12]
