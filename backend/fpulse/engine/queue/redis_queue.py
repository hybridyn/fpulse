"""
RedisQueue — JobQueue implementation backed by Redis lists.

Stage 5 Phase 2. Replaces InProcQueue in the fpulse-worker container
and (when ``FPULSE_REDIS_URL`` is set) in the fpulse-api container
so the API enqueues to the same Redis instance the worker consumes.

Queue layout
------------
Five Redis lists, one per priority level:
    fpulse:queue:p1  (highest priority — drained first)
    fpulse:queue:p2
    fpulse:queue:p3
    fpulse:queue:p4
    fpulse:queue:p5  (lowest priority — drained last)

Producers ``LPUSH`` (enqueue at the head), consumers ``RPOP`` (take
from the tail). This gives FIFO-within-priority without needing a
sort step. Across priorities, dequeue() does a best-effort scan:
try P1 first, fall through to P2, ..., P5.

For multi-worker deployments (``docker compose --scale fpulse-worker=N``),
the workers share the same Redis lists. ``RPOP`` is atomic — no
two workers will pop the same job. Phase 2 uses the simple non-blocking
RPOP; a future optimisation can switch to ``BRPOP`` with a multi-key
timeout for push-based wakeups.

Cancel path
-----------
``cancel(job_id)`` is O(N) per priority list — we ``LRANGE`` each
list, find the descriptor with the matching id, and ``LREM`` it.
At the queue depths we target (hundreds, not millions), this is
fine. If it becomes a bottleneck, we'd maintain a separate
``fpulse:queue:index`` hash mapping id → priority.

Why sync Redis, not aioredis
-----------------------------
JobQueue is a sync protocol (see ``engine/queue/__init__.py``
docstring). RedisQueue uses the sync redis client to match. The
performance cost is minimal: queue ops are single round-trips to
a local Redis, and the API threads submitting jobs are blocked
during the enqueue anyway.

The worker daemon in ``fpulse/worker.py`` is also sync — it polls
in a loop with a short sleep. When push-based wakeups matter, we
switch to ``BRPOP`` (still sync) before reaching for asyncio.

Deps
----
The ``redis>=5.0`` package is an optional dependency — pin in
``pyproject.toml`` under ``[project.optional-dependencies] queue``.
Import is lazy so single-binary OSS installs that never set
``FPULSE_REDIS_URL`` don't need redis installed.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .descriptor import JobDescriptor

if TYPE_CHECKING:
    import redis
    from fpulse.engine.worker_pool import QueuedJob

logger = logging.getLogger(__name__)

# Per-priority list keys. Ordered P1 → P5 so consumers can iterate
# and drain higher priorities first. Kept as a module constant so the
# API side, worker daemon, and ops tooling all agree on the scheme.
PRIORITY_KEYS = [f"fpulse:queue:p{p}" for p in range(1, 6)]


class RedisQueue:
    """JobQueue backed by five Redis lists (one per priority).

    Lazy connection — the actual Redis client isn't created until the
    first queue op, so OSS installs that import this module without
    setting ``FPULSE_REDIS_URL`` won't pay the redis-py import cost.
    """

    def __init__(self, url: str, namespace: str = "fpulse") -> None:
        self._url = url
        self._namespace = namespace
        self._client: "redis.Redis | None" = None
        self._closed = False

    def _get_client(self) -> "redis.Redis":
        """Lazy-create the Redis client. Raises with a helpful message
        if the redis package isn't installed."""
        if self._closed:
            raise RuntimeError("RedisQueue has been closed")
        if self._client is None:
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError(
                    "RedisQueue requires the 'redis' package. "
                    "Install with: pip install -e '.[queue]'"
                ) from exc
            # decode_responses=True so we work with str, not bytes —
            # keeps the descriptor round-trip straightforward. The cost
            # is an extra decode per op, negligible at our throughput.
            self._client = redis.Redis.from_url(
                self._url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=10,
            )
            try:
                self._client.ping()
            except Exception as exc:
                # Translate connection failure to a clearer error so
                # operators see "can't reach Redis at <url>" not the
                # raw socket error.
                raise RuntimeError(
                    f"RedisQueue: could not connect to {_redact(self._url)}: {exc}"
                ) from exc
            logger.info("RedisQueue connected to %s", _redact(self._url))
        return self._client

    def _key(self, priority: int) -> str:
        """Return the Redis list key for a given priority level.

        Priority is clamped to [1, 5] — any out-of-range value is
        normalised to the nearest valid level. Matches WorkerPool's
        clamp behaviour in submit().
        """
        p = max(1, min(5, int(priority)))
        return f"{self._namespace}:queue:p{p}"

    # ── JobQueue protocol ──

    def enqueue(self, job: "QueuedJob") -> None:
        desc = JobDescriptor.from_queued_job(job)
        key = self._key(job.priority)
        # LPUSH at the head; consumers RPOP from the tail = FIFO
        # within this priority list.
        self._get_client().lpush(key, desc.to_json())

    def dequeue(self) -> "QueuedJob | None":
        """Pop the highest-priority job. Non-blocking.

        Iterates P1 → P5 and returns the first non-empty list's tail.
        Atomic at the per-list level (RPOP), so two workers never get
        the same job even if both check P1 simultaneously.
        """
        client = self._get_client()
        for key in PRIORITY_KEYS:
            raw = client.rpop(key)
            if raw is None:
                continue
            try:
                desc = JobDescriptor.from_json(raw)
            except Exception as exc:
                # Malformed descriptor — log, drop, continue. Losing a
                # single corrupt job is better than halting the worker.
                logger.error(
                    "RedisQueue: dropping malformed descriptor from %s: %s",
                    key, exc,
                )
                continue
            return desc.to_queued_job()
        return None

    def depth(self) -> int:
        """Total queue depth across all priority lists.

        Uses a pipeline for a single round-trip. Racy — an enqueue
        between LLEN calls will show slightly stale counts — but the
        admin page tolerates that. For strict accuracy the caller can
        wrap depth() in its own lock; today nobody needs that.
        """
        client = self._get_client()
        pipe = client.pipeline()
        for key in PRIORITY_KEYS:
            pipe.llen(key)
        return sum(pipe.execute())

    def cancel(self, job_id: str) -> bool:
        """Remove a queued job by id. Returns True if found and removed.

        O(N) worst case per priority list — scan, find, LREM. At queue
        depths < 1k, this is a handful of milliseconds. If it matters,
        maintain a side-index (not Phase 2's concern).
        """
        client = self._get_client()
        for key in PRIORITY_KEYS:
            items = client.lrange(key, 0, -1)
            for item in items:
                try:
                    desc = JobDescriptor.from_json(item)
                except Exception:
                    continue
                if desc.id == job_id:
                    # LREM with count=0 removes all matching occurrences;
                    # id is unique so this is effectively count=1, but
                    # count=0 is defensive against accidental duplicates.
                    removed = client.lrem(key, 0, item)
                    return removed > 0
        return False

    def snapshot(self) -> "list[QueuedJob]":
        """Return every queued job in priority-then-FIFO order.

        Reads each priority list LRANGE 0 -1 (full list). Redis
        returns LPUSH order (newest first); we reverse to get
        oldest-first (FIFO). Priority is honoured by the outer loop
        P1 → P5.

        Used by the admin Execution Pool page. Callers should treat
        the result as read-only.
        """
        client = self._get_client()
        out: list["QueuedJob"] = []
        for key in PRIORITY_KEYS:
            # Redis lists: index 0 = head (LPUSH inserts here), last
            # index = tail (RPOP pops here). FIFO reading order is
            # tail→head, i.e. indexes reversed.
            items = client.lrange(key, 0, -1)
            for raw in reversed(items):
                try:
                    desc = JobDescriptor.from_json(raw)
                except Exception as exc:
                    logger.debug("snapshot: skipping malformed row: %s", exc)
                    continue
                out.append(desc.to_queued_job())
        return out

    def close(self) -> None:
        """Close the underlying Redis connection pool.

        Safe to call multiple times. Subsequent ops will raise
        RuntimeError — matches InProcQueue's post-close behaviour.
        """
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            try:
                self._client.close()
            except Exception as exc:
                logger.debug("RedisQueue close failed (non-fatal): %s", exc)
            self._client = None


def _redact(url: str) -> str:
    """Mask the password in a Redis URL for logging."""
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(url)
        if p.password:
            netloc = f"{p.username or ''}:***@{p.hostname}"
            if p.port:
                netloc += f":{p.port}"
            return urlunparse(p._replace(netloc=netloc))
    except Exception:
        pass
    return url
