"""
AuditConsumer — append-only JSONL of every durable event.

Provides the regulatory / compliance side of observability: every
state change the system makes is on disk in a flat file, easy to
ship to a SIEM or grep through for "who ran what when".

Subscribes only to DURABLE events — best-effort telemetry (step
progress, log lines) does not belong in the audit log. Filtering
by topic class lives at the consumer, not the bus, because the
NATS subject namespace doesn't encode durability.

## Format

One JSON object per line, exactly the wire format the bus uses
(types.serialize()). Stable, language-agnostic, replayable.

## Usage

    from fpulse.events import get_event_bus
    from fpulse.events.consumers import AuditConsumer

    audit = AuditConsumer(path="/var/log/fpulse/audit.jsonl")
    audit.install(get_event_bus())

## Rotation

This consumer does not rotate. Use logrotate, systemd-journald, or
the platform's standard rotation tooling. Append-only + line-
oriented means rotation is safe — partial reads at the rotate
boundary truncate cleanly to whole events.
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from typing import Optional

from ..bus import DurabilityClass, EventBus, Subscription
from ..types import Event, serialize


# Topic prefixes that map to DURABLE events. Mirrors the set built
# from types.py — kept here rather than introspected so the audit
# log is robust to bus types changing classification.
DURABLE_TOPIC_PREFIXES: tuple[str, ...] = (
    "fpulse.pipeline.run.",
    "fpulse.step.started",
    "fpulse.step.completed",
    "fpulse.step.failed",
    "fpulse.step.skipped",
    "fpulse.approval.",
    "fpulse.alert.",
)


class AuditConsumer:
    """Append-only JSONL audit log. Subscribes to fpulse.> and
    writes the DURABLE subset."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sub: Optional[Subscription] = None

    # ── Wiring ──

    def install(self, bus: EventBus) -> Subscription:
        self._sub = bus.subscribe("fpulse.>", self._handle)
        return self._sub

    def uninstall(self) -> None:
        if self._sub is not None:
            self._sub.cancel()
            self._sub = None

    # ── Event handling ──

    def _handle(self, event: Event) -> None:
        # Filter to durable by class (preferred) or topic prefix
        # fallback (in case the event class metadata is missing
        # over the wire — defensive).
        durability = getattr(event, "DURABILITY", None)
        if durability is DurabilityClass.DURABLE:
            self._append(event)
            return
        if durability is None and event.topic.startswith(DURABLE_TOPIC_PREFIXES):
            self._append(event)

    def _append(self, event: Event) -> None:
        line = serialize(event)
        with self._lock:
            # Open + append + close on each event. Slow but
            # crash-safe: at most one event in flight on power
            # loss. For higher throughput, buffer + fsync on a
            # timer; for the OSS-scale audit log this is fine.
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")

    # ── Read API ──

    def read_all(self) -> list[str]:
        """Read every line. Test/debug helper. Production code that
        needs replay should use the bus's stream(since=...) API
        against the durable transport instead."""
        with self._lock:
            if not self._path.exists():
                return []
            with open(self._path, "r", encoding="utf-8") as f:
                return [ln.rstrip("\n") for ln in f if ln.strip()]
