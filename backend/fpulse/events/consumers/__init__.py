"""
fpulse.events.consumers — built-in subscribers that turn the bus
into useful observability without editing the executor.

Each consumer is a single file that subscribes at startup and
exposes a small read API (Prometheus text, JSONL file, etc.).
Adding a new observability path = adding a new consumer here +
calling its `install(bus)` function from main.py startup. No
executor changes. That's the whole point of the bus.

Today:
  - metrics.MetricsConsumer  — Prometheus-format event counters,
                                run/step duration histograms.
  - audit.AuditConsumer       — append-only JSONL of every durable
                                event, for compliance / replay.
"""

from .metrics import MetricsConsumer
from .audit import AuditConsumer

__all__ = ["MetricsConsumer", "AuditConsumer"]
