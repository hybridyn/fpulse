"""
Connection pool — per-run driver-connection cache.

Critical #5 from the node audit. See `DESIGN_CONNECTION_POOLING.md` for
the full design rationale.

This is **Phase 1 — skeleton only**. The class is not yet wired into
`db_source._get_connection_config()` or `WorkflowExecutor`. That's
Phase 2-3 work in a follow-up session, kept separate so this skeleton
can land without risking the existing single-connection-per-step path
that's working today.

Behaviour summary
-----------------
- Pool is keyed by ``(connection_id, run_id)`` — a connection borrowed
  by run A cannot be reused by run B.
- Per-``connection_id`` cap (default 5, env-overridable via
  ``FPULSE_CONNECTION_POOL_SIZE``). Block-or-fail behaviour is
  conservative: if the cap is hit and no connection is free, the
  factory is still called, but a warning is logged. Callers can
  inspect ``stats()`` to detect saturation.
- ``release_run()`` is the load-bearing safety net — it closes EVERY
  connection borrowed by that run, even if some are still "in use"
  (the run is over; a leaked driver handle is worse than a closed one).
- ``invalidate_connection()`` is the credential-rotation hook — drops
  every cached entry for a given ``connection_id`` so the next
  ``acquire()`` creates a fresh driver connection with the new
  credentials.
- Sync only — F-Pulse's executor is sync (DuckDB-driven). When the
  engine goes async this class needs a redesign.

The class is dialect-agnostic; the caller passes a ``factory`` callable
that builds the actual driver connection (psycopg2 / pymysql / etc.).
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Default per-`connection_id` cap. Override at instance creation time
# via env var or constructor arg. The number is conservative — five
# concurrent steps per connection_id covers a typical solo-dev workflow
# with parallel branches without exhausting downstream DB connection
# limits (a Postgres `max_connections=100` accommodates 20+ such
# pipelines running concurrently).
DEFAULT_MAX_PER_CONNECTION = 5


def _read_env_cap() -> int:
    raw = (os.environ.get("FPULSE_CONNECTION_POOL_SIZE") or "").strip()
    if not raw:
        return DEFAULT_MAX_PER_CONNECTION
    try:
        n = int(raw)
        return max(1, n)
    except (TypeError, ValueError):
        logger.warning(
            "FPULSE_CONNECTION_POOL_SIZE=%r is not an int — using default %d",
            raw, DEFAULT_MAX_PER_CONNECTION,
        )
        return DEFAULT_MAX_PER_CONNECTION


@dataclass
class _Entry:
    """One pooled driver connection."""
    driver: Any
    connection_id: str
    run_id: str
    in_use: bool = False
    # Optional close hook. Most drivers expose `.close()` directly, but
    # some (httpx clients, for example) need a different method. Caller
    # can pass a custom closer to `acquire()` if needed; default is
    # `obj.close()`.
    closer: Callable[[Any], None] | None = None


@dataclass
class PoolStats:
    """Snapshot of pool state at a point in time. Useful for tests
    and for the (deferred) Pool-page observability surface."""
    total_entries: int
    by_connection: dict[str, int] = field(default_factory=dict)
    by_run: dict[str, int] = field(default_factory=dict)


class ConnectionPool:
    """Per-run connection cache. Thread-safe.

    Caller pattern:
        pool = ConnectionPool()  # one instance, lives in app_state
        # In a step's execute():
        conn = pool.acquire(
            connection_id=cfg["connection_id"],
            run_id=ctx.run_id,
            conn_type="postgresql",
            config=cfg,
            factory=lambda ct, c: psycopg2.connect(
                host=c["host"], port=c["port"], dbname=c["database"],
                user=c["user"], password=c["password"],
            ),
        )
        # ...use conn (DO NOT call conn.close())...
        # When the workflow finishes (success / failure / timeout),
        # WorkflowExecutor calls pool.release_run(run_id) and the pool
        # closes every connection it borrowed.
    """

    def __init__(self, max_per_connection: int | None = None):
        self._cache: dict[tuple[str, str], list[_Entry]] = {}
        self._max = max_per_connection if max_per_connection is not None else _read_env_cap()
        self._lock = threading.RLock()

    # ── Public API ────────────────────────────────────────────────────

    def acquire(
        self,
        *,
        connection_id: str,
        run_id: str,
        conn_type: str,
        config: dict[str, Any],
        factory: Callable[[str, dict[str, Any]], Any],
        closer: Callable[[Any], None] | None = None,
    ) -> Any:
        """Borrow a driver connection.

        If a free entry exists for ``(connection_id, run_id)``, return
        its driver. Otherwise call ``factory(conn_type, config)`` to
        build a new one and cache it.

        Returns the raw driver object (psycopg2 connection, pymysql
        connection, etc.). The pool retains ownership; do NOT call
        ``.close()`` on it. The pool will close it when ``release_run``
        is called for ``run_id``.
        """
        if not connection_id or not run_id:
            # Pool requires both keys to function. Without a run_id we
            # can't safely scope the lifetime. Fall through to letting
            # the caller handle it directly — they'll build + close
            # their own connection.
            return factory(conn_type, config)

        key = (connection_id, run_id)
        with self._lock:
            entries = self._cache.setdefault(key, [])

            # Reuse a free entry from this run if one exists.
            for e in entries:
                if not e.in_use:
                    e.in_use = True
                    return e.driver

            # Cap check — count entries for this connection_id across
            # ALL runs (so we don't blow past `max_connections` on the
            # downstream DB even if multiple runs are concurrent).
            cross_run_count = sum(
                len(v) for k, v in self._cache.items() if k[0] == connection_id
            )
            if cross_run_count >= self._max:
                logger.warning(
                    "ConnectionPool: cap reached for connection_id=%r (%d entries across runs). "
                    "Creating an over-cap connection — consider raising FPULSE_CONNECTION_POOL_SIZE.",
                    connection_id, cross_run_count,
                )

            driver = factory(conn_type, config)
            entries.append(_Entry(
                driver=driver, connection_id=connection_id,
                run_id=run_id, in_use=True, closer=closer,
            ))
            return driver

    def release(self, *, connection_id: str, run_id: str, driver: Any) -> None:
        """Return a borrowed connection to the pool without closing it.

        Optional — most callers won't use this. The typical pattern is
        to acquire once per step and rely on ``release_run`` to do the
        cleanup at run end. ``release()`` exists for the rare case
        where a step explicitly wants to mark a connection as
        no-longer-in-use mid-run (e.g., it just ran something that
        might leave the connection in a bad state and wants the next
        step to get a fresh one).
        """
        key = (connection_id, run_id)
        with self._lock:
            entries = self._cache.get(key, [])
            for e in entries:
                if e.driver is driver:
                    e.in_use = False
                    return

    def release_run(self, run_id: str) -> int:
        """Close every connection borrowed by this run.

        Called from ``WorkflowExecutor.execute_workflow`` finally-block.
        Safe to call multiple times — second call is a no-op.

        Returns the number of connections closed (for logging / tests).
        """
        if not run_id:
            return 0
        closed = 0
        with self._lock:
            doomed_keys = [k for k in self._cache if k[1] == run_id]
            for k in doomed_keys:
                for e in self._cache.pop(k, []):
                    closed += self._close_entry(e)
        if closed:
            logger.debug("ConnectionPool: released %d connections for run_id=%s", closed, run_id)
        return closed

    def invalidate_connection(self, connection_id: str) -> int:
        """Drop every cached entry for ``connection_id`` across every
        run. Called when a credential rotates so the next ``acquire``
        creates a fresh driver connection with the updated credentials.

        In-use entries are still closed — the next caller of that run
        will get a brand-new connection on its next ``acquire`` and
        never see the old one again. This is correct for credential
        rotation: any in-flight query against the now-stale credential
        is going to fail anyway.

        Returns the number of connections closed.
        """
        if not connection_id:
            return 0
        closed = 0
        with self._lock:
            doomed_keys = [k for k in self._cache if k[0] == connection_id]
            for k in doomed_keys:
                for e in self._cache.pop(k, []):
                    closed += self._close_entry(e)
        if closed:
            logger.info(
                "ConnectionPool: invalidated %d connections for connection_id=%s "
                "(credential rotation or explicit purge).",
                closed, connection_id,
            )
        return closed

    def stats(self) -> PoolStats:
        """Snapshot of current pool state. Read-only; safe under
        contention thanks to the lock."""
        with self._lock:
            by_conn: dict[str, int] = {}
            by_run: dict[str, int] = {}
            total = 0
            for (cid, rid), entries in self._cache.items():
                n = len(entries)
                total += n
                by_conn[cid] = by_conn.get(cid, 0) + n
                by_run[rid] = by_run.get(rid, 0) + n
            return PoolStats(total_entries=total, by_connection=by_conn, by_run=by_run)

    def close_all(self) -> int:
        """Close every connection in the pool. Used by tests + at app
        shutdown. Returns the number of connections closed."""
        closed = 0
        with self._lock:
            for entries in self._cache.values():
                for e in entries:
                    closed += self._close_entry(e)
            self._cache.clear()
        return closed

    # ── Internal ──────────────────────────────────────────────────────

    @staticmethod
    def _close_entry(entry: _Entry) -> int:
        """Close one driver. Returns 1 on close, 0 on failure (logged).
        Never raises — the pool's lifecycle hooks must not break the
        caller's finally-block."""
        try:
            if entry.closer is not None:
                entry.closer(entry.driver)
            elif hasattr(entry.driver, "close"):
                entry.driver.close()
            return 1
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            logger.warning(
                "ConnectionPool: failed to close driver for connection_id=%s run_id=%s: %s",
                entry.connection_id, entry.run_id, exc,
            )
            return 0
