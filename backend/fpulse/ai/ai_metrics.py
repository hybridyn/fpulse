"""In-process AI loop metrics — counters + running averages.

Lightweight, process-local, no external deps. Surfaced by
``GET /api/metrics/ai`` so operators can see at a glance how the
Copilot is actually being used:

  * total agent requests today
  * fast-lane / hybrid / single-shot / full-loop breakdown
  * fallback hits (LLM unavailable → deterministic rule)
  * tool-loop hop average (helps decide when to bump cap)
  * average latency per lane (helps decide when to switch lanes)

The store resets at midnight UTC. For long-term retention, point
``FPULSE_DATA_DIR/ai_metrics/<date>.json`` at a daily exporter cron.
For Prometheus export, the existing ``/api/metrics`` endpoint can
attach an ``AIMetricsCollector`` in a follow-up (not in scope today).

Thread-safe via a single ``threading.Lock``; latency is microseconds
of contention overhead even under high concurrency. The counters are
incremented from inside the request path so they must be cheap.

Design constraints:
- No I/O on the hot path (no writes to disk, no DB).
- Read endpoint never blocks the write path beyond the lock duration.
- Reset happens lazily at read time when the date rolls over —
  keeps the data fresh without needing a background timer.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class _LaneStats:
    """Per-lane aggregate. ``latency_ms_total`` / ``count`` = average."""
    count: int = 0
    latency_ms_total: int = 0
    tool_hops_total: int = 0
    tokens_in_total: int = 0
    tokens_out_total: int = 0


_LANES: tuple[str, ...] = (
    "fast_lane",
    "hybrid",
    "single_shot",
    "agent_loop",
    "tool_only_mode_block",
    "no_provider_fallback",
)


@dataclass
class AIMetricsSnapshot:
    """Read-only view returned by ``get_snapshot``."""
    period_start_utc: str
    total_requests: int
    fallback_hits: int           # rule-based fallback ran (LLM unavail / failed)
    escalations: int             # hybrid lane → full agent loop
    per_lane: dict[str, dict[str, Any]]


class AIMetricsStore:
    """Thread-safe in-process counter store. One instance per process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._period_start = _today_utc_iso()
        self._total_requests = 0
        self._fallback_hits = 0
        self._escalations = 0
        self._per_lane: dict[str, _LaneStats] = defaultdict(_LaneStats)

    def record_request(
        self,
        *,
        lane: str,
        latency_ms: int,
        tool_hops: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """Record one finished agent request.

        ``lane`` should be one of ``_LANES`` (unknown values are accepted
        but flagged in the snapshot under their own bucket — useful for
        spotting new lane types added without updating this enum).
        """
        with self._lock:
            self._maybe_reset_locked()
            self._total_requests += 1
            stats = self._per_lane[lane]
            stats.count += 1
            stats.latency_ms_total += max(0, int(latency_ms))
            stats.tool_hops_total += max(0, int(tool_hops))
            stats.tokens_in_total += max(0, int(tokens_in))
            stats.tokens_out_total += max(0, int(tokens_out))

    def record_fallback(self) -> None:
        """LLM unavailable or failed → rule-based fallback ran."""
        with self._lock:
            self._maybe_reset_locked()
            self._fallback_hits += 1

    def record_escalation(self) -> None:
        """Hybrid lane handler returned empty/generic → full agent loop."""
        with self._lock:
            self._maybe_reset_locked()
            self._escalations += 1

    def get_snapshot(self) -> AIMetricsSnapshot:
        """Return a read-only snapshot. Safe to call from any thread."""
        with self._lock:
            self._maybe_reset_locked()
            per_lane: dict[str, dict[str, Any]] = {}
            for name in _LANES:
                stats = self._per_lane.get(name, _LaneStats())
                per_lane[name] = self._stats_to_dict(stats)
            # Surface unknown lanes too — operator visibility into drift.
            for name, stats in self._per_lane.items():
                if name not in _LANES:
                    per_lane[name] = self._stats_to_dict(stats)
            return AIMetricsSnapshot(
                period_start_utc=self._period_start,
                total_requests=self._total_requests,
                fallback_hits=self._fallback_hits,
                escalations=self._escalations,
                per_lane=per_lane,
            )

    def reset(self) -> None:
        """Manual reset — useful in tests; production uses lazy daily reset."""
        with self._lock:
            self._period_start = _today_utc_iso()
            self._total_requests = 0
            self._fallback_hits = 0
            self._escalations = 0
            self._per_lane.clear()

    # ─── internals ────────────────────────────────────────────────────────

    def _maybe_reset_locked(self) -> None:
        """If the UTC date has rolled over, reset counters. Caller holds lock."""
        today = _today_utc_iso()
        if today != self._period_start:
            self._period_start = today
            self._total_requests = 0
            self._fallback_hits = 0
            self._escalations = 0
            self._per_lane.clear()

    @staticmethod
    def _stats_to_dict(stats: _LaneStats) -> dict[str, Any]:
        count = max(stats.count, 1)  # avoid div-by-zero in averages
        return {
            "count": stats.count,
            "avg_latency_ms": round(stats.latency_ms_total / count, 1) if stats.count else 0.0,
            "avg_tool_hops": round(stats.tool_hops_total / count, 2) if stats.count else 0.0,
            "tokens_in_total": stats.tokens_in_total,
            "tokens_out_total": stats.tokens_out_total,
        }


def _today_utc_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# Single per-process store, lazily constructed.
_STORE: AIMetricsStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> AIMetricsStore:
    """Module-level accessor — constructs on first call."""
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = AIMetricsStore()
    return _STORE
