"""
GlobalResourceGovernor — system-level admission control with tiered response.

Sprint 2 PR5 step 4 of the locked implementation order.

Every spawn path through ExecutionManager consults the governor via
`_admit()`. The governor samples system memory + CPU via psutil,
maps the reading to a tier (green/yellow/orange/red), applies
hysteresis so transitions don't flap, and raises `GovernorRejection`
when the spawn should not proceed.

Tiers (defaults, tunable via constructor):

  Tier    | Entry                              | Behavior
  --------|------------------------------------|-------------------------
  GREEN   | mem < 70% AND cpu < 85%            | Accept all spawns
  YELLOW  | 70% ≤ mem < 80% OR cpu ≥ 85%       | Accept queueable,
          |                                    | reject non-queueable
  ORANGE  | 80% ≤ mem < 90%                    | Same as YELLOW +
          |                                    | slow_signal() = True
  RED     | mem ≥ 90%                          | Reject everything

Hysteresis: once a tier is active, drop back a level only when memory
falls 5 percentage points below the tier's entry threshold (defaults:
yellow→green at 65, orange→yellow at 75, red→orange at 85). Prevents
flapping when memory bounces around a threshold.

"Queueable" maps naturally today:
  - pipeline       → queueable=True  (WorkerPool already queues)
  - subprocess     → queueable=False (no subprocess queue in PR5; step
                                      12-ish will add a DeferredQueue)
  - thread         → queueable=False
  - asyncio        → queueable=False
  - scheduled      → queueable=False

If psutil is unavailable, the governor returns GREEN unconditionally
and logs once at import — F-Pulse must not hard-fail when the optional
dependency is missing, but the operator should know enforcement is
best-effort (setrlimit on Linux subprocesses only).

"slow_signal()" is advisory — callers that want to reduce intensity
(lower DuckDB memory_limit, halve scheduler poll rate, etc.) check
it. Step 4 provides the signal; wiring it to the actual reducers is
per-caller work in later steps.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger("fpulse.global_governor")


try:
    import psutil as _psutil
    _HAS_PSUTIL = True
except ImportError:
    _psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False
    logger.warning(
        "psutil not installed — GlobalResourceGovernor will be a no-op. "
        "Memory/CPU admission control requires `pip install psutil`."
    )


class Tier(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"


# Ordering for comparing tier severity. Higher = worse.
_TIER_ORDER = {Tier.GREEN: 0, Tier.YELLOW: 1, Tier.ORANGE: 2, Tier.RED: 3}


@dataclass(frozen=True)
class GovernorSample:
    mem_pct: float
    cpu_pct: float
    tier: Tier
    sampled_at: datetime


class GovernorRejection(Exception):
    """Raised when admission is denied. Callers can catch this
    explicitly to distinguish capacity-rejection from other errors."""

    def __init__(self, tier: Tier, mem_pct: float, cpu_pct: float, reason: str):
        super().__init__(reason)
        self.tier = tier
        self.mem_pct = mem_pct
        self.cpu_pct = cpu_pct
        self.reason = reason


class GlobalResourceGovernor:
    """System-level admission gate. Thread-safe. Single instance per
    ExecutionManager."""

    def __init__(
        self,
        *,
        mem_defer_pct: float = 70.0,
        mem_slow_pct: float = 80.0,
        mem_red_pct: float = 90.0,
        cpu_defer_pct: float = 85.0,
        hysteresis_delta_pct: float = 5.0,
        sample_cache_s: float = 5.0,
    ):
        if not (0 < mem_defer_pct < mem_slow_pct < mem_red_pct <= 100):
            raise ValueError(
                "memory thresholds must satisfy 0 < defer < slow < red <= 100"
            )
        self._mem_defer = mem_defer_pct
        self._mem_slow = mem_slow_pct
        self._mem_red = mem_red_pct
        self._cpu_defer = cpu_defer_pct
        self._hyst = hysteresis_delta_pct
        self._cache_s = sample_cache_s

        self._lock = threading.Lock()
        self._cached: GovernorSample | None = None
        self._active_tier: Tier = Tier.GREEN

    # ── Sampling ───────────────────────────────────────────────────

    def sample(self, *, fresh: bool = False) -> GovernorSample:
        """Return the current sample. Cached for `sample_cache_s`
        seconds unless `fresh=True`."""
        now_s = time.time()
        with self._lock:
            if (
                not fresh
                and self._cached is not None
                and (now_s - self._cached.sampled_at.timestamp()) < self._cache_s
            ):
                return self._cached

        mem_pct, cpu_pct = self._read_psutil()

        with self._lock:
            tier = self._resolve_tier_locked(mem_pct, cpu_pct)
            self._active_tier = tier
            sample = GovernorSample(
                mem_pct=mem_pct,
                cpu_pct=cpu_pct,
                tier=tier,
                sampled_at=datetime.now(timezone.utc),
            )
            self._cached = sample
            return sample

    def _read_psutil(self) -> tuple[float, float]:
        if not _HAS_PSUTIL:
            return 0.0, 0.0
        try:
            mem_pct = float(_psutil.virtual_memory().percent)
            # interval=None returns the value since the previous call —
            # the first call after import returns 0. That's acceptable
            # for a best-effort signal; avoid blocking this hot path on
            # a sampling interval.
            cpu_pct = float(_psutil.cpu_percent(interval=None))
            return mem_pct, cpu_pct
        except Exception as exc:
            logger.warning("psutil read failed: %s", exc)
            return 0.0, 0.0

    def _resolve_tier_locked(self, mem_pct: float, cpu_pct: float) -> Tier:
        """Compute the tier, applying hysteresis against the
        previously-active tier. Must be called with self._lock held."""
        # Natural tier based on raw readings alone.
        if mem_pct >= self._mem_red:
            natural = Tier.RED
        elif mem_pct >= self._mem_slow:
            natural = Tier.ORANGE
        elif mem_pct >= self._mem_defer or cpu_pct >= self._cpu_defer:
            natural = Tier.YELLOW
        else:
            natural = Tier.GREEN

        # Hysteresis: if the natural tier is less severe than the
        # active tier, require memory to drop `hysteresis_delta_pct`
        # below the current tier's entry threshold before relaxing.
        active = self._active_tier
        if _TIER_ORDER[natural] >= _TIER_ORDER[active]:
            return natural

        entry_threshold = self._entry_threshold(active)
        if entry_threshold is None:
            return natural
        if mem_pct > (entry_threshold - self._hyst):
            # Still too close to the entry line; hold the active tier.
            return active
        return natural

    def _entry_threshold(self, tier: Tier) -> float | None:
        if tier == Tier.YELLOW:
            return self._mem_defer
        if tier == Tier.ORANGE:
            return self._mem_slow
        if tier == Tier.RED:
            return self._mem_red
        return None

    # ── Admission ──────────────────────────────────────────────────

    def check(self, kind: str, *, queueable: bool) -> Tier:
        """Evaluate admission for a spawn of the given kind.

        Returns the current tier if admission is allowed. Raises
        GovernorRejection if the spawn must not proceed.
        """
        if not _HAS_PSUTIL:
            # No enforcement possible; always accept, always green.
            return Tier.GREEN

        sample = self.sample()

        if sample.tier == Tier.RED:
            raise GovernorRejection(
                tier=sample.tier,
                mem_pct=sample.mem_pct,
                cpu_pct=sample.cpu_pct,
                reason=(
                    f"system memory at {sample.mem_pct:.1f}% (>= {self._mem_red:.0f}%) "
                    f"— rejecting spawn kind={kind!r}"
                ),
            )

        if sample.tier in (Tier.YELLOW, Tier.ORANGE) and not queueable:
            raise GovernorRejection(
                tier=sample.tier,
                mem_pct=sample.mem_pct,
                cpu_pct=sample.cpu_pct,
                reason=(
                    f"system under pressure (tier={sample.tier.value}, "
                    f"mem={sample.mem_pct:.1f}%, cpu={sample.cpu_pct:.1f}%) "
                    f"— non-queueable spawn kind={kind!r} rejected until pressure eases"
                ),
            )

        return sample.tier

    def slow_signal(self) -> bool:
        """True when running jobs should reduce intensity (orange or
        red). Uses the cached sample — call `sample(fresh=True)` first
        if a live reading is required."""
        with self._lock:
            if self._cached is None:
                return False
            return self._cached.tier in (Tier.ORANGE, Tier.RED)

    # ── Introspection (for /metrics + AdminPage) ──────────────────

    def snapshot(self) -> dict:
        """Serializable status snapshot for the admin UI."""
        with self._lock:
            s = self._cached
        return {
            "available": _HAS_PSUTIL,
            "active_tier": (s.tier.value if s else Tier.GREEN.value),
            "mem_pct": (s.mem_pct if s else None),
            "cpu_pct": (s.cpu_pct if s else None),
            "sampled_at": (s.sampled_at.isoformat() if s else None),
            "thresholds": {
                "mem_defer_pct": self._mem_defer,
                "mem_slow_pct": self._mem_slow,
                "mem_red_pct": self._mem_red,
                "cpu_defer_pct": self._cpu_defer,
                "hysteresis_delta_pct": self._hyst,
            },
        }
