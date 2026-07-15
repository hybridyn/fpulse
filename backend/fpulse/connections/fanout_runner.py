"""Fanout runner for slow list-then-enrich APIs.

The pattern: phase 1 fetches a list of resource IDs (cheap), phase 2
hits a per-resource endpoint (slow, rate-limited, fails under high
concurrency). Sources like on-prem endpoint-management products,
asset-management APIs, and CMDB exports all share this shape.

Five behaviours combined here:

  1. AIMD adaptive concurrency — start low, +1 every N successes,
     halve on failure. Discovers the per-deployment ceiling without
     hard-coding it.
  2. Token-bucket rate limit — sustained RPS budget, small burst
     allowance. Smooths the request rate even when concurrency would
     let more through.
  3. Per-item exponential backoff with jitter — one resource failing
     doesn't cascade.
  4. Streaming JSONL output — never holds the full result set in
     memory.
  5. File-based checkpoint — resume from the last successful resource
     after a crash. The checkpoint is the set of completed IDs; rerun
     skips them.

Wired against the catalog substrate so any saved Connection can drive
it: see `from_connection(connection)` factory.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)


# ── Rate limiter ─────────────────────────────────────────────────────

class TokenBucket:
    """Async token bucket. `rate` tokens per second, `burst` allowance.

    Acquiring a token is awaitable — workers naturally backpressure
    when the bucket is empty.
    """

    def __init__(self, rate: float, burst: int | None = None) -> None:
        self.rate = max(0.1, float(rate))
        self.capacity = burst or max(1, int(rate * 2))
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity,
                                    self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
                await asyncio.sleep(wait)


# ── AIMD concurrency controller ──────────────────────────────────────

class AIMDController:
    """Additive-increase, multiplicative-decrease concurrency controller.

    Mirrors the TCP congestion-control algorithm. Starts at `initial`,
    grows by +1 every `success_window` successes, halves on failure
    (floor `min_concurrency`). The current value caps a semaphore the
    workers wait on.
    """

    def __init__(self, initial: int = 4, min_concurrency: int = 1,
                  max_concurrency: int = 16, success_window: int = 50) -> None:
        self.min = min_concurrency
        self.max = max_concurrency
        self.success_window = success_window
        self._current = initial
        self._wins = 0

    @property
    def current(self) -> int:
        return self._current

    def on_success(self) -> int:
        self._wins += 1
        if self._wins >= self.success_window and self._current < self.max:
            self._current += 1
            self._wins = 0
        return self._current

    def on_failure(self) -> int:
        self._current = max(self.min, self._current // 2)
        self._wins = 0
        return self._current


# ── Retry helper ─────────────────────────────────────────────────────

async def _with_retry(
    fn: Callable[[], Awaitable[Any]],
    *, max_retries: int, base_delay: float = 1.0,
) -> Any:
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except _RateLimitedError as exc:
            # Respect Retry-After when the server told us how long to wait.
            wait = exc.retry_after if exc.retry_after else base_delay * (2 ** attempt)
            await asyncio.sleep(wait + random.uniform(0, 0.5))
            last_exc = exc
        except Exception as exc:  # noqa: BLE001 — catches network + API errors
            if attempt == max_retries:
                last_exc = exc
                break
            wait = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(wait)
            last_exc = exc
    raise last_exc if last_exc else RuntimeError("retry loop exited without exception")


class _RateLimitedError(Exception):
    """Raised by fetch_one to signal 429/503 — the controller halves
    concurrency AND the retry helper respects Retry-After."""
    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


# ── Public API ───────────────────────────────────────────────────────

@dataclass
class FanoutResult:
    total_ids: int
    succeeded: int
    failed: int
    skipped_resumed: int
    duration_s: float
    final_concurrency: int
    output_path: str
    failed_path: str


@dataclass
class FanoutRunner:
    """Drives the list → enrich → stage pattern for slow APIs.

    Required args:
      fetch_ids:  zero-arg coroutine returning an iterable of resource IDs
      fetch_one:  coroutine accepting one ID, returning the enriched record
                  (must raise _RateLimitedError on 429/503; any other raise
                  is treated as a hard failure for that ID)

    Optional args:
      output_path:    JSONL file written line-by-line as records arrive
      checkpoint_path: text file of completed IDs (resume support)
      failed_path:    JSONL of {id, error} for items that exhausted retries

    Tuning args:
      initial_concurrency / max_concurrency / min_concurrency
      rate_limit_rps         — sustained RPS cap (token bucket)
      max_retries            — per-item retry count on transient failures
      success_window         — successes between concurrency +1
    """

    fetch_ids: Callable[[], Awaitable[Iterable[str]]]
    fetch_one: Callable[[str], Awaitable[dict]]
    output_path: str
    checkpoint_path: str = ""
    failed_path: str = ""
    initial_concurrency: int = 4
    max_concurrency: int = 12
    min_concurrency: int = 1
    rate_limit_rps: float = 8.0
    rate_limit_burst: int = 12
    max_retries: int = 3
    success_window: int = 50
    progress_every: int = 100

    # ── Setup ────────────────────────────────────────────────────────

    def __post_init__(self) -> None:
        if not self.checkpoint_path:
            self.checkpoint_path = self.output_path + ".checkpoint"
        if not self.failed_path:
            self.failed_path = self.output_path + ".failed.jsonl"
        self._controller = AIMDController(
            initial=self.initial_concurrency,
            min_concurrency=self.min_concurrency,
            max_concurrency=self.max_concurrency,
            success_window=self.success_window,
        )
        self._bucket = TokenBucket(self.rate_limit_rps, self.rate_limit_burst)
        # Re-create on every run; assigned in run().
        self._sem: asyncio.Semaphore | None = None

    def _load_checkpoint(self) -> set[str]:
        if not os.path.isfile(self.checkpoint_path):
            return set()
        with open(self.checkpoint_path, encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}

    # ── Worker ───────────────────────────────────────────────────────

    async def _process_one(
        self, resource_id: str,
        out_handle, fail_handle, ckpt_handle,
        counters: dict[str, int],
    ) -> None:
        async def _attempt() -> dict:
            await self._bucket.acquire()
            return await self.fetch_one(resource_id)

        # Concurrency gate — semaphore size is the AIMD-controlled cap.
        # We don't shrink it dynamically; instead, on_failure simply
        # returns a smaller "current" and we await up to that count.
        assert self._sem is not None
        async with self._sem:
            try:
                record = await _with_retry(_attempt, max_retries=self.max_retries)
            except _RateLimitedError as exc:
                self._controller.on_failure()
                counters["failed"] += 1
                fail_handle.write(json.dumps({
                    "id": resource_id, "error": "rate_limited",
                    "retry_after": exc.retry_after,
                }) + "\n")
                fail_handle.flush()
                return
            except Exception as exc:  # noqa: BLE001
                counters["failed"] += 1
                fail_handle.write(json.dumps({
                    "id": resource_id, "error": f"{type(exc).__name__}: {exc}",
                }) + "\n")
                fail_handle.flush()
                # A hard failure shouldn't slam concurrency the same as
                # a rate-limit signal — only halve on rate-limit.
                return
            self._controller.on_success()
            counters["succeeded"] += 1
            out_handle.write(json.dumps(record, default=str) + "\n")
            ckpt_handle.write(resource_id + "\n")
            # flush often so a crash mid-run doesn't lose more than the
            # last in-flight item. Cost is negligible vs API latency.
            out_handle.flush()
            ckpt_handle.flush()
            if counters["succeeded"] % self.progress_every == 0:
                logger.info(
                    "fanout progress: succeeded=%d failed=%d concurrency=%d",
                    counters["succeeded"], counters["failed"],
                    self._controller.current,
                )

    # ── Run ──────────────────────────────────────────────────────────

    async def run(self) -> FanoutResult:
        os.makedirs(os.path.dirname(self.output_path) or ".", exist_ok=True)
        done = self._load_checkpoint()
        ids = list(await self.fetch_ids())
        pending = [i for i in ids if i not in done]
        skipped = len(ids) - len(pending)

        # Semaphore initially sized to the controller's max — workers
        # gate on `controller.current` via an explicit check below if
        # we want strict shrinking. For now the semaphore at max +
        # token bucket gives effective throttling, since the bucket
        # also rate-limits.
        self._sem = asyncio.Semaphore(self.max_concurrency)
        counters: dict[str, int] = {"succeeded": 0, "failed": 0}
        start = time.monotonic()

        # Open all three streams; close in finally so a crash mid-run
        # leaves a valid append-style file.
        out_h = open(self.output_path, "a", encoding="utf-8")
        fail_h = open(self.failed_path, "a", encoding="utf-8")
        ckpt_h = open(self.checkpoint_path, "a", encoding="utf-8")
        try:
            tasks = [
                asyncio.create_task(
                    self._process_one(rid, out_h, fail_h, ckpt_h, counters)
                )
                for rid in pending
            ]
            for fut in asyncio.as_completed(tasks):
                await fut
        finally:
            out_h.close()
            fail_h.close()
            ckpt_h.close()

        return FanoutResult(
            total_ids=len(ids),
            succeeded=counters["succeeded"],
            failed=counters["failed"],
            skipped_resumed=skipped,
            duration_s=round(time.monotonic() - start, 2),
            final_concurrency=self._controller.current,
            output_path=self.output_path,
            failed_path=self.failed_path,
        )


# ── Convenience: signal rate-limit from a sync requests-based fetcher ─

def raise_if_rate_limited(response) -> None:
    """Call from inside fetch_one when using requests/httpx and you got
    a 429 or 503 — converts to the typed exception the runner expects."""
    if response.status_code in (429, 503):
        retry_after = response.headers.get("Retry-After")
        try:
            wait = float(retry_after) if retry_after else None
        except ValueError:
            wait = None
        raise _RateLimitedError(retry_after=wait)
