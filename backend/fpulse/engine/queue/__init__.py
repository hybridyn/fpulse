"""
Stage 5 Phase 1 — Queue abstraction (2026-04-20 late session).

Defines the ``JobQueue`` protocol that WorkerPool uses to buffer queued
jobs, and re-exports the ``InProcQueue`` default implementation.

Why this exists
---------------
Today's WorkerPool (engine/worker_pool.py) holds its priority queue as a
plain ``list[QueuedJob]`` inside the class. That's fine for single-process
F-Pulse, but Stage 5 Phase 2 will add an out-of-process worker container
that pulls jobs from a shared Redis list. Rather than ship two divergent
code paths, Phase 1 extracts the queue operations behind a protocol so
both the in-process and Redis paths look identical to WorkerPool.

Phase 1 deliverable (THIS module): protocol + InProcQueue + zero
behavior change. Existing tests keep passing. The admin Execution Pool
page sees the same metrics as before.

Phase 2 deliverable (future): RedisQueue implementing the same protocol,
plus a separate ``fpulse/worker.py`` daemon that calls ``dequeue`` in a
loop.

Design choices
--------------
* Sync protocol (not async). Reason: WorkerPool today is synchronous —
  FastAPI's sync endpoint handlers call ``pool.submit()`` directly, and
  ``ThreadPoolExecutor`` runs the job function in a worker thread. Making
  JobQueue async would force an asyncio refactor of the entire pool, which
  is exactly the "no behavior change" constraint we're avoiding.
  Phase 2's RedisQueue will wrap its async redis client in a sync shim
  (``asyncio.run`` in the submit path) or the worker daemon will use a
  dedicated asyncio event loop. TBD when we get there.

* ``QueuedJob`` stays as-is. InProcQueue keeps live Python references
  (``_fn`` callable, ``_kwargs`` dict). Serialisation is deferred to
  Phase 2 where RedisQueue will need a ``JobDescriptor`` separate from
  ``QueuedJob``.

* Thread safety is the implementation's responsibility. WorkerPool's
  existing lock DOES wrap enqueue/dequeue today; InProcQueue has its own
  lock so callers don't have to know.

* ``cancel`` returns bool. True if a queued job was removed; False if
  the job wasn't in the queue (already dispatched, already cancelled,
  or unknown id). Cancellation of a RUNNING job is WorkerPool's concern,
  not the queue's.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .inproc import InProcQueue

if TYPE_CHECKING:
    from fpulse.engine.worker_pool import QueuedJob


@runtime_checkable
class JobQueue(Protocol):
    """Queue of :class:`QueuedJob` ordered by priority + queued_at.

    Implementations must be thread-safe — WorkerPool calls these methods
    from both the submitting thread (HTTP request handler) and the worker
    completion callback thread (``_on_job_complete``).

    The queue MUST be priority-aware: dequeue returns the highest-priority
    (lowest numeric) job first, breaking ties by oldest queued_at.
    """

    def enqueue(self, job: "QueuedJob") -> None:
        """Add a job to the queue. No return value; assume success or raise."""
        ...

    def dequeue(self) -> "QueuedJob | None":
        """Remove and return the highest-priority job, or None if empty.

        Non-blocking — callers who want to wait spin with sleep between
        calls, or use a blocking variant added in Phase 2. Phase 1
        WorkerPool dispatches from completion callbacks, which is
        inherently reactive — no blocking dequeue needed.
        """
        ...

    def depth(self) -> int:
        """Number of jobs currently queued."""
        ...

    def cancel(self, job_id: str) -> bool:
        """Remove a queued job by id. Returns True if removed, False if
        the job wasn't in the queue. Cancelling a RUNNING job is the
        caller's responsibility — the queue only manages queued work."""
        ...

    def snapshot(self) -> "list[QueuedJob]":
        """Return a copy of the current queue in priority order.

        Used by ``WorkerPool.get_status()`` to render the admin page.
        Returns a COPY — mutations to the result must not affect the
        live queue."""
        ...

    def close(self) -> None:
        """Release any resources (Redis connection, file handle, …).

        InProcQueue's close is a no-op; RedisQueue (future) will close
        the connection pool. Phase 1 callers may skip calling this but
        Phase 2 callers should.
        """
        ...


def build_job_queue(redis_url: str | None = None) -> JobQueue:
    """Factory: return the right JobQueue for the current environment.

    Stage 5 Phase 2. Callers (WorkerPool constructor, worker.py
    entrypoint) pass the resolved ``FPULSE_REDIS_URL`` value. When it's
    set, we build a RedisQueue; when it's unset or empty, InProcQueue
    keeps the OSS single-binary install working unchanged.

    Lazy import of the Redis backend keeps OSS installs from paying
    the redis-py import cost (and from needing the dependency at all).
    """
    if redis_url:
        from .redis_queue import RedisQueue
        return RedisQueue(redis_url)
    return InProcQueue()


__all__ = ["JobQueue", "InProcQueue", "build_job_queue"]
