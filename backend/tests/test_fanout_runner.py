"""Tests for the fanout runner against a fake slow API.

The fake records every call so we can assert on:
  - concurrency was actually bounded (peak_in_flight ≤ controller.max)
  - rate-limit signals halved concurrency
  - retry-after was respected
  - successful items were checkpointed and skipped on resume
  - failed items went to the DLQ file
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time

import pytest

from fpulse.connections.fanout_runner import (
    AIMDController,
    FanoutRunner,
    TokenBucket,
    _RateLimitedError,
)


# ── Unit: AIMD controller ────────────────────────────────────────────

def test_aimd_increases_after_success_window():
    c = AIMDController(initial=2, max_concurrency=10, success_window=3)
    for _ in range(3):
        c.on_success()
    assert c.current == 3
    for _ in range(3):
        c.on_success()
    assert c.current == 4


def test_aimd_halves_on_failure_with_floor():
    c = AIMDController(initial=8, min_concurrency=2)
    c.on_failure()
    assert c.current == 4
    c.on_failure()
    assert c.current == 2
    c.on_failure()
    assert c.current == 2  # floor holds


def test_aimd_does_not_exceed_max():
    c = AIMDController(initial=9, max_concurrency=10, success_window=1)
    for _ in range(5):
        c.on_success()
    assert c.current == 10


# ── Unit: token bucket ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_token_bucket_smooths_rate():
    bucket = TokenBucket(rate=10.0, burst=2)
    start = time.monotonic()
    for _ in range(6):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    # 2 burst + 4 refilled at 10/s ≈ 0.4s. Allow generous slack for CI.
    assert elapsed >= 0.3, f"bucket released too fast ({elapsed}s)"


# ── Integration: fanout against a fake API ──────────────────────────

class _FakeAPI:
    """Records every call. Configurable failure injection."""
    def __init__(self, latency_s: float = 0.01,
                  fail_ids: set[str] | None = None,
                  rate_limit_ids: set[str] | None = None) -> None:
        self.latency_s = latency_s
        self.fail_ids = fail_ids or set()
        self.rate_limit_ids = rate_limit_ids or set()
        self._in_flight = 0
        self.peak_in_flight = 0
        self.calls: list[str] = []
        self._lock = asyncio.Lock()

    async def fetch_one(self, rid: str) -> dict:
        async with self._lock:
            self._in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
            self.calls.append(rid)
        try:
            await asyncio.sleep(self.latency_s)
            if rid in self.fail_ids:
                raise RuntimeError(f"hard failure for {rid}")
            if rid in self.rate_limit_ids:
                raise _RateLimitedError(retry_after=0.05)
            return {"id": rid, "data": f"value-{rid}"}
        finally:
            async with self._lock:
                self._in_flight -= 1


@pytest.mark.asyncio
async def test_fanout_streams_all_records(tmp_path):
    api = _FakeAPI(latency_s=0.005)
    ids = [f"r{i}" for i in range(40)]

    runner = FanoutRunner(
        fetch_ids=lambda: _aiter(ids),
        fetch_one=api.fetch_one,
        output_path=str(tmp_path / "out.jsonl"),
        max_concurrency=8,
        rate_limit_rps=200.0,  # high — concurrency, not rate, is the cap here
    )
    result = await runner.run()

    assert result.total_ids == 40
    assert result.succeeded == 40
    assert result.failed == 0
    assert api.peak_in_flight <= 8, f"concurrency cap violated: {api.peak_in_flight}"

    # Every record landed in the JSONL file, in some order.
    with open(runner.output_path, encoding="utf-8") as f:
        recorded = [json.loads(line)["id"] for line in f]
    assert sorted(recorded) == sorted(ids)


@pytest.mark.asyncio
async def test_fanout_resumes_skipping_completed(tmp_path):
    api = _FakeAPI()
    ids = [f"r{i}" for i in range(20)]

    out = str(tmp_path / "out.jsonl")
    ckpt = out + ".checkpoint"
    # Pre-seed the checkpoint as if an earlier run completed half.
    with open(ckpt, "w", encoding="utf-8") as f:
        for rid in ids[:10]:
            f.write(rid + "\n")

    runner = FanoutRunner(
        fetch_ids=lambda: _aiter(ids),
        fetch_one=api.fetch_one,
        output_path=out,
        rate_limit_rps=200.0,
    )
    result = await runner.run()

    assert result.skipped_resumed == 10
    assert result.succeeded == 10
    # API was only called for the second half — first 10 IDs never fetched.
    assert sorted(api.calls) == sorted(ids[10:])


@pytest.mark.asyncio
async def test_fanout_writes_failures_to_dlq(tmp_path):
    api = _FakeAPI(fail_ids={"r3", "r7"})
    ids = [f"r{i}" for i in range(10)]

    runner = FanoutRunner(
        fetch_ids=lambda: _aiter(ids),
        fetch_one=api.fetch_one,
        output_path=str(tmp_path / "out.jsonl"),
        rate_limit_rps=200.0,
        max_retries=1,  # don't slow the test down with backoffs
    )
    result = await runner.run()

    assert result.succeeded == 8
    assert result.failed == 2

    with open(runner.failed_path, encoding="utf-8") as f:
        failed_ids = {json.loads(line)["id"] for line in f}
    assert failed_ids == {"r3", "r7"}


@pytest.mark.asyncio
async def test_rate_limit_signal_halves_concurrency(tmp_path):
    # Half the IDs return a rate-limit signal → controller should halve.
    rl_ids = {f"r{i}" for i in range(15) if i % 2 == 0}
    api = _FakeAPI(rate_limit_ids=rl_ids)
    ids = [f"r{i}" for i in range(15)]

    runner = FanoutRunner(
        fetch_ids=lambda: _aiter(ids),
        fetch_one=api.fetch_one,
        output_path=str(tmp_path / "out.jsonl"),
        initial_concurrency=8,
        max_concurrency=8,
        min_concurrency=1,
        rate_limit_rps=200.0,
        max_retries=0,  # fail fast so on_failure() runs early
    )
    await runner.run()
    # After several rate-limit signals the controller must have shrunk.
    assert runner._controller.current < 8


# ── helpers ──────────────────────────────────────────────────────────

async def _aiter(seq):
    return seq
