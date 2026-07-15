"""Tests for the Stage 5 Phase 1 queue abstraction.

Covers the ``JobQueue`` protocol contract plus the ``InProcQueue``
default implementation. Verifies priority ordering, thread safety,
cancel-by-id, snapshot immutability, and close semantics.

Does NOT test WorkerPool integration end-to-end — that's what
test_worker_pool.py (future / existing) covers. These tests pin the
queue's behavioural contract so Phase 2's RedisQueue can be validated
against the same assertions.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from fpulse.engine.queue import JobQueue, InProcQueue
from fpulse.engine.worker_pool import QueuedJob


def _mk_job(priority: int = 3, workflow_id: str = "wf", queued_offset_s: int = 0) -> QueuedJob:
    """Build a minimal QueuedJob for queue testing.

    ``queued_offset_s`` shifts queued_at backward so multiple jobs can
    be created with deterministic FIFO-within-priority ordering.
    """
    return QueuedJob(
        workflow_id=workflow_id,
        workflow_name=f"test-{workflow_id}",
        priority=priority,
        queued_at=datetime.now(timezone.utc) - timedelta(seconds=queued_offset_s),
    )


# ══════════════════════════════════════════════════════════════════════
# Protocol conformance
# ══════════════════════════════════════════════════════════════════════

class TestProtocolConformance:
    """InProcQueue must satisfy the JobQueue protocol at runtime."""

    def test_inproc_queue_is_job_queue(self):
        q = InProcQueue()
        assert isinstance(q, JobQueue)

    def test_has_all_required_methods(self):
        q = InProcQueue()
        for method_name in ("enqueue", "dequeue", "depth", "cancel", "snapshot", "close"):
            assert hasattr(q, method_name), f"missing {method_name}"
            assert callable(getattr(q, method_name))


# ══════════════════════════════════════════════════════════════════════
# Basic operations
# ══════════════════════════════════════════════════════════════════════

class TestBasicOps:
    def test_new_queue_is_empty(self):
        q = InProcQueue()
        assert q.depth() == 0
        assert q.dequeue() is None
        assert q.snapshot() == []

    def test_enqueue_increments_depth(self):
        q = InProcQueue()
        q.enqueue(_mk_job())
        assert q.depth() == 1
        q.enqueue(_mk_job())
        assert q.depth() == 2

    def test_dequeue_decrements_depth(self):
        q = InProcQueue()
        q.enqueue(_mk_job())
        q.enqueue(_mk_job())
        q.dequeue()
        assert q.depth() == 1
        q.dequeue()
        assert q.depth() == 0

    def test_dequeue_empty_returns_none(self):
        q = InProcQueue()
        assert q.dequeue() is None


# ══════════════════════════════════════════════════════════════════════
# Priority + FIFO ordering
# ══════════════════════════════════════════════════════════════════════

class TestPriorityOrdering:
    """Lower priority number = higher priority. Within same priority,
    older queued_at drains first (FIFO). This matches the pre-Phase-1
    WorkerPool behaviour exactly — zero-behavior-change contract."""

    def test_lower_priority_dequeues_first(self):
        q = InProcQueue()
        low_pri = _mk_job(priority=5, workflow_id="low")
        high_pri = _mk_job(priority=1, workflow_id="high")
        q.enqueue(low_pri)
        q.enqueue(high_pri)
        first = q.dequeue()
        assert first.workflow_id == "high", "P1 must drain before P5"
        second = q.dequeue()
        assert second.workflow_id == "low"

    def test_fifo_within_same_priority(self):
        q = InProcQueue()
        old = _mk_job(priority=3, workflow_id="old", queued_offset_s=10)
        new = _mk_job(priority=3, workflow_id="new", queued_offset_s=0)
        # Insert in reverse chronological order
        q.enqueue(new)
        q.enqueue(old)
        first = q.dequeue()
        assert first.workflow_id == "old", "older queued_at must drain first within priority"

    def test_interleaved_priorities_drain_correctly(self):
        q = InProcQueue()
        p3_old = _mk_job(priority=3, workflow_id="p3-old", queued_offset_s=5)
        p1 = _mk_job(priority=1, workflow_id="p1", queued_offset_s=0)
        p3_new = _mk_job(priority=3, workflow_id="p3-new", queued_offset_s=0)
        q.enqueue(p3_old)
        q.enqueue(p1)
        q.enqueue(p3_new)
        drain_order = [q.dequeue().workflow_id for _ in range(3)]
        assert drain_order == ["p1", "p3-old", "p3-new"], (
            f"Expected P1, then P3-old, then P3-new. Got {drain_order}."
        )


# ══════════════════════════════════════════════════════════════════════
# Cancel
# ══════════════════════════════════════════════════════════════════════

class TestCancel:
    def test_cancel_queued_job_by_id(self):
        q = InProcQueue()
        job = _mk_job()
        q.enqueue(job)
        assert q.cancel(job.id) is True
        assert q.depth() == 0

    def test_cancel_unknown_id_returns_false(self):
        q = InProcQueue()
        assert q.cancel("nonexistent") is False

    def test_cancel_does_not_affect_other_jobs(self):
        q = InProcQueue()
        keep = _mk_job(workflow_id="keep")
        remove = _mk_job(workflow_id="remove")
        q.enqueue(keep)
        q.enqueue(remove)
        q.cancel(remove.id)
        assert q.depth() == 1
        assert q.dequeue().workflow_id == "keep"


# ══════════════════════════════════════════════════════════════════════
# Snapshot semantics
# ══════════════════════════════════════════════════════════════════════

class TestSnapshot:
    def test_snapshot_returns_copy_not_reference(self):
        q = InProcQueue()
        q.enqueue(_mk_job())
        snap = q.snapshot()
        # Mutation of the snapshot must not change live queue
        snap.clear()
        assert q.depth() == 1, "snapshot must be a copy"

    def test_snapshot_preserves_priority_order(self):
        q = InProcQueue()
        q.enqueue(_mk_job(priority=3, workflow_id="a"))
        q.enqueue(_mk_job(priority=1, workflow_id="b"))
        q.enqueue(_mk_job(priority=5, workflow_id="c"))
        snap = q.snapshot()
        assert [j.workflow_id for j in snap] == ["b", "a", "c"]

    def test_snapshot_empty_queue(self):
        q = InProcQueue()
        assert q.snapshot() == []


# ══════════════════════════════════════════════════════════════════════
# Close semantics
# ══════════════════════════════════════════════════════════════════════

class TestClose:
    def test_close_clears_queue(self):
        q = InProcQueue()
        q.enqueue(_mk_job())
        q.close()
        assert q.depth() == 0

    def test_enqueue_after_close_raises(self):
        q = InProcQueue()
        q.close()
        with pytest.raises(RuntimeError):
            q.enqueue(_mk_job())

    def test_dequeue_after_close_returns_none(self):
        q = InProcQueue()
        q.close()
        assert q.dequeue() is None

    def test_close_is_idempotent(self):
        q = InProcQueue()
        q.close()
        q.close()  # second call must not raise


# ══════════════════════════════════════════════════════════════════════
# Thread safety
# ══════════════════════════════════════════════════════════════════════

class TestThreadSafety:
    """Concurrent enqueue from multiple threads must not lose jobs or
    corrupt the sorted-list invariant. WorkerPool uses its own outer
    lock but InProcQueue is also called from the admin-page get_status
    path without that lock held by the caller."""

    def test_concurrent_enqueue_preserves_count(self):
        q = InProcQueue()
        jobs_per_thread = 50
        thread_count = 8
        expected = jobs_per_thread * thread_count

        def worker():
            for _ in range(jobs_per_thread):
                q.enqueue(_mk_job())

        threads = [threading.Thread(target=worker) for _ in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert q.depth() == expected, f"lost jobs: expected {expected}, got {q.depth()}"

    def test_concurrent_enqueue_dequeue_no_duplicates(self):
        """A job enqueued once must be dequeued exactly once, even
        under concurrent dequeue pressure."""
        q = InProcQueue()
        jobs = [_mk_job(workflow_id=f"wf-{i}") for i in range(100)]
        for j in jobs:
            q.enqueue(j)

        seen: list[str] = []
        seen_lock = threading.Lock()

        def drain():
            while True:
                job = q.dequeue()
                if job is None:
                    return
                with seen_lock:
                    seen.append(job.id)

        threads = [threading.Thread(target=drain) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(seen) == 100, f"expected 100 drained, got {len(seen)}"
        assert len(set(seen)) == 100, "duplicates detected"


# ══════════════════════════════════════════════════════════════════════
# WorkerPool integration — the behavior-preservation contract
# ══════════════════════════════════════════════════════════════════════

class TestWorkerPoolIntegration:
    """WorkerPool with the default (unspecified) job_queue must behave
    identically to pre-Phase-1. The InProcQueue dependency must be
    injected cleanly; tests can swap in a mock for isolation."""

    def test_pool_defaults_to_inproc_queue(self):
        from fpulse.engine.worker_pool import WorkerPool
        pool = WorkerPool(max_workers=2)
        try:
            assert isinstance(pool._queue, InProcQueue)
            assert pool._queue.depth() == 0
        finally:
            pool.stop()

    def test_pool_accepts_custom_queue(self):
        """Dependency injection path — Phase 2 will pass RedisQueue via
        this constructor arg. Phase 1 test: a custom InProcQueue works."""
        from fpulse.engine.worker_pool import WorkerPool
        custom = InProcQueue()
        pool = WorkerPool(max_workers=2, job_queue=custom)
        try:
            assert pool._queue is custom
        finally:
            pool.stop()

    def test_stop_closes_queue(self):
        from fpulse.engine.worker_pool import WorkerPool
        pool = WorkerPool(max_workers=2)
        q = pool._queue
        pool.stop()
        # Verify the queue was closed by enqueueing — should raise.
        with pytest.raises(RuntimeError):
            q.enqueue(_mk_job())

    def test_get_status_depth_matches_queue(self):
        """The queue_depth field in get_status() must read via the
        protocol (depth()), not via len() on a list — regression guard
        for the refactor."""
        from fpulse.engine.worker_pool import WorkerPool, Priority
        pool = WorkerPool(max_workers=1)
        pool.start()
        try:
            # Busy-lock the single worker so subsequent submits queue
            done_event = threading.Event()
            blocker_ran = threading.Event()

            def blocking_fn():
                blocker_ran.set()
                done_event.wait(timeout=5)
                return {"status": "success"}

            pool.submit(
                workflow_id="busy",
                workflow_name="busy",
                fn=blocking_fn,
                priority=Priority.P3_NORMAL,
            )
            assert blocker_ran.wait(timeout=2), "blocker job never ran"

            # Now these should queue
            pool.submit(
                workflow_id="q1",
                workflow_name="q1",
                fn=lambda: {"status": "success"},
                priority=Priority.P3_NORMAL,
            )
            pool.submit(
                workflow_id="q2",
                workflow_name="q2",
                fn=lambda: {"status": "success"},
                priority=Priority.P3_NORMAL,
            )

            status = pool.get_status()
            assert status["pool"]["queue_depth"] == 2
            assert len(status["queue"]) == 2

            done_event.set()
            # Give the pool a tick to drain
            time.sleep(0.5)
        finally:
            pool.stop()
