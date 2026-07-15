"""
get_event_bus() — pick the transport based on environment.

This is the *only* file in the codebase that imports both
in_process and nats_bus. Every other consumer imports just the
interface from .bus, so swapping transports stays a one-place
change.

Selection rules:

  FPULSE_EVENT_BUS=inprocess  →  InProcessEventBus (default)
  FPULSE_EVENT_BUS=nats       →  NatsEventBus (Plus only; raises
                                  NotImplementedError on use until
                                  the stub is filled in)

Lazy singleton: the first call wires the bus, subsequent calls
return the same instance. Tests can override via _set_event_bus().
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from .bus import EventBus

log = logging.getLogger(__name__)

_BUS: EventBus | None = None
_BUS_LOCK = threading.Lock()


def get_event_bus() -> EventBus:
    """Return the process-wide EventBus, constructing on first call."""
    global _BUS
    if _BUS is not None:
        return _BUS
    with _BUS_LOCK:
        if _BUS is not None:  # double-checked locking
            return _BUS
        _BUS = _construct_from_env()
        _BUS.start()
    return _BUS


def _construct_from_env() -> EventBus:
    kind = os.environ.get("FPULSE_EVENT_BUS", "inprocess").lower()
    if kind == "inprocess":
        from .in_process import InProcessEventBus
        # Default to the same SQLite file the rest of the backend
        # uses, in a separate table — single DB keeps the OSS
        # install one-file. FPULSE_EVENT_DB overrides for tests
        # or when you want events on a different disk.
        db_path = os.environ.get("FPULSE_EVENT_DB") or _default_event_db()
        log.info("event-bus: in-process backend, db=%s", db_path)
        return InProcessEventBus(db_path=db_path)
    if kind == "nats":
        from .nats_bus import NatsEventBus
        servers = [s.strip() for s in os.environ.get(
            "FPULSE_NATS_SERVERS", "nats://localhost:4222",
        ).split(",") if s.strip()]
        log.info("event-bus: NATS backend, servers=%s", servers)
        return NatsEventBus(servers=servers)
    raise ValueError(
        f"FPULSE_EVENT_BUS={kind!r}; expected 'inprocess' or 'nats'."
    )


def _default_event_db() -> str:
    """Path to the OSS events DB. Co-located with the main fpulse.db
    so users running F-Pulse OSS don't end up with stray files in
    unexpected places."""
    data_dir = os.environ.get("FPULSE_DATA_DIR") or "."
    return str(Path(data_dir) / "fpulse_events.db")


def _set_event_bus(bus: EventBus | None) -> None:
    """Test hook. Replaces the singleton; pass None to reset."""
    global _BUS
    with _BUS_LOCK:
        if _BUS is not None and _BUS is not bus:
            _BUS.close()
        _BUS = bus
