"""Per-execution resource sampler.

Wraps a pipeline run with a background thread that samples ``psutil`` at
1 Hz, capturing peak RSS memory and total CPU seconds. Falls back to a
no-op when psutil is unavailable so the executor still works (resource
fields just come back as 0).

Usage:
    with ResourceMonitor() as rm:
        result = run_pipeline(...)
    print(rm.peak_memory_mb, rm.cpu_seconds)

Cheap by design — sampling is process-wide (not per-step). Per-step
attribution would require subprocess isolation, which is on the F-Pulse+
roadmap (PR5+) and out of scope for OSS.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
except Exception:
    _HAS_PSUTIL = False


@dataclass
class ResourceSample:
    """Aggregated metrics captured during a single pipeline run."""
    peak_memory_mb: float = 0.0
    cpu_seconds: float = 0.0
    sample_count: int = 0
    duration_s: float = 0.0


class ResourceMonitor:
    """Context manager that samples process resources during its lifetime."""

    def __init__(self, sample_interval_s: float = 1.0):
        self._interval = sample_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = 0.0
        self._cpu_start = 0.0
        self.sample = ResourceSample()
        self._proc = None
        if _HAS_PSUTIL:
            try:
                self._proc = psutil.Process(os.getpid())
            except Exception:
                self._proc = None

    @property
    def peak_memory_mb(self) -> float:
        return self.sample.peak_memory_mb

    @property
    def cpu_seconds(self) -> float:
        return self.sample.cpu_seconds

    def __enter__(self) -> "ResourceMonitor":
        self._t0 = time.monotonic()
        if self._proc is None:
            return self
        try:
            ct = self._proc.cpu_times()
            self._cpu_start = float(ct.user) + float(ct.system)
        except Exception:
            self._cpu_start = 0.0
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="fpulse-resmon")
        self._thread.start()
        return self

    def _loop(self) -> None:
        if self._proc is None:
            return
        peak = 0.0
        count = 0
        while not self._stop.is_set():
            try:
                rss = self._proc.memory_info().rss
                mb = rss / (1024 * 1024)
                if mb > peak:
                    peak = mb
                count += 1
            except Exception:
                pass
            self._stop.wait(self._interval)
        self.sample.peak_memory_mb = peak
        self.sample.sample_count = count

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.sample.duration_s = time.monotonic() - self._t0
        if self._proc is not None:
            try:
                ct = self._proc.cpu_times()
                cpu_end = float(ct.user) + float(ct.system)
                self.sample.cpu_seconds = max(0.0, cpu_end - self._cpu_start)
            except Exception:
                pass
        # Surface a single peak read in case the loop never sampled (very
        # short runs). At least we still get a memory snapshot.
        if self.sample.peak_memory_mb == 0.0 and self._proc is not None:
            try:
                self.sample.peak_memory_mb = self._proc.memory_info().rss / (1024 * 1024)
            except Exception:
                pass
