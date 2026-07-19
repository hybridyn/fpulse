"""
In-process implementation of :class:`fpulse.engine.queue.JobQueue`.

Stage 5 Phase 1 default. Same priority semantics as the old
``WorkerPool._queue`` list — lower priority number wins, ties break by
oldest ``queued_at``. Lives in-memory, lost on process restart.

Durability is a separate concern handled by
``WorkerPool._persistent_queue`` (SQLite-backed). That layer is
orthogonal: InProcQueue is "what's waiting right now", PersistentQueue
is "what to replay on boot". Phase 2's RedisQueue will replace BOTH —
Redis is both live and persistent.

Threading model: a single ``threading.Lock`` guards the list. That's
coarser than the per-operation fine-grained locks you might see in a
high-throughput queue library, but this queue is inside a single
Python process with GIL-protected list ops anyway — the lock primarily
keeps the "check depth → pop oldest" pattern atomic.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fpulse.engine.worker_pool import QueuedJob


class InProcQueue:
    """Thread-safe priority queue backed by a sorted list.

    Sorted-list-with-insert is O(N log N) per enqueue (due to the sort),
    but WorkerPool queues rarely exceed a few hundred jobs in practice
    — the P5 throughput ceiling is measured in thousands per hour, not
    per second. A heap would be faster but makes cancel-by-id awkward.
    The sorted-list choice matches the existing WorkerPool behaviour
    exactly so Phase 1 is a pure extraction, not a semantic change.
    """

    def __init__(self) -> None:
        self._jobs: list["QueuedJob"] = []
        self._lock = threading.Lock()
        self._closed = False

    def enqueue(self, job: "QueuedJob") -> None:
        if self._closed:
            raise RuntimeError("enqueue on closed InProcQueue")
        with self._lock:
            self._jobs.append(job)
            # Sort on every enqueue to maintain priority order. Stable
            # sort preserves insertion order within equal priorities,
            # which combined with ``queued_at`` as secondary key gives
            # deterministic FIFO-within-priority behaviour.
            self._jobs.sort(key=lambda j: (j.priority, j.queued_at))

    def dequeue(self) -> "QueuedJob | None":
        if self._closed:
            return None
        with self._lock:
            if not self._jobs:
                return None
            return self._jobs.pop(0)

    def depth(self) -> int:
        with self._lock:
            return len(self._jobs)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            for i, job in enumerate(self._jobs):
                if job.id == job_id:
                    self._jobs.pop(i)
                    return True
            return False

    def snapshot(self) -> "list[QueuedJob]":
        with self._lock:
            # Shallow copy of the list; callers get the same QueuedJob
            # objects (not copies of THOSE), which matches the old
            # WorkerPool behaviour — admin page reads attributes but
            # doesn't mutate jobs.
            return list(self._jobs)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._jobs.clear()
