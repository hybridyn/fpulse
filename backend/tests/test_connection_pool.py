"""Tests for the connection pool skeleton (Critical #5 Phase 1).

The pool is dialect-agnostic so the tests use a fake driver that
records every close() call. This catches the load-bearing invariants
without spinning up real Postgres / MySQL servers.

Covered:
  - Pool warm-up: first acquire creates, second acquire reuses
  - Cross-run isolation: run A and run B never share a connection
  - release_run() closes every connection for that run
  - invalidate_connection() drops entries across all runs
  - stats() returns accurate counts
  - close_all() (shutdown path) closes everything
  - Per-connection cap warning when exceeded
  - Empty/missing run_id: bypass pool, call factory directly
  - Custom closer hook is honored
  - Thread safety on concurrent acquire/release_run
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from fpulse.engine.connection_pool import (
    ConnectionPool,
    DEFAULT_MAX_PER_CONNECTION,
)


class _FakeDriver:
    """Stand-in for a psycopg2/pymysql connection. Tracks closes."""
    _next_id = 0

    def __init__(self, conn_type: str, config: dict[str, Any]):
        type(self)._next_id += 1
        self.id = type(self)._next_id
        self.conn_type = conn_type
        self.config = config
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _factory(conn_type: str, config: dict[str, Any]) -> _FakeDriver:
    return _FakeDriver(conn_type, config)


def test_warmup_then_reuse():
    """Second acquire on same (conn_id, run_id) reuses the first driver."""
    pool = ConnectionPool()
    a = pool.acquire(
        connection_id="c1", run_id="r1", conn_type="postgresql",
        config={"host": "localhost"}, factory=_factory,
    )
    pool.release(connection_id="c1", run_id="r1", driver=a)
    b = pool.acquire(
        connection_id="c1", run_id="r1", conn_type="postgresql",
        config={"host": "localhost"}, factory=_factory,
    )
    assert a is b


def test_no_reuse_when_in_use():
    """If the first driver is still in use, second acquire builds a new one."""
    pool = ConnectionPool()
    a = pool.acquire(
        connection_id="c1", run_id="r1", conn_type="postgresql",
        config={}, factory=_factory,
    )
    # No release — first is still in use.
    b = pool.acquire(
        connection_id="c1", run_id="r1", conn_type="postgresql",
        config={}, factory=_factory,
    )
    assert a is not b


def test_cross_run_isolation():
    """run A and run B never share a connection, even with same conn_id."""
    pool = ConnectionPool()
    a = pool.acquire(
        connection_id="c1", run_id="run_A", conn_type="postgresql",
        config={}, factory=_factory,
    )
    b = pool.acquire(
        connection_id="c1", run_id="run_B", conn_type="postgresql",
        config={}, factory=_factory,
    )
    assert a is not b
    assert pool.stats().by_run == {"run_A": 1, "run_B": 1}


def test_release_run_closes_everything_for_that_run():
    pool = ConnectionPool()
    a = pool.acquire(
        connection_id="c1", run_id="r1", conn_type="postgresql",
        config={}, factory=_factory,
    )
    b = pool.acquire(
        connection_id="c2", run_id="r1", conn_type="postgresql",
        config={}, factory=_factory,
    )
    other = pool.acquire(
        connection_id="c1", run_id="r_OTHER", conn_type="postgresql",
        config={}, factory=_factory,
    )

    closed = pool.release_run("r1")
    assert closed == 2
    assert a.closed and b.closed
    # Other run's connection NOT closed.
    assert not other.closed
    assert pool.stats().by_run == {"r_OTHER": 1}


def test_release_run_idempotent():
    """Calling release_run twice for the same run is a no-op the second time."""
    pool = ConnectionPool()
    pool.acquire(
        connection_id="c1", run_id="r1", conn_type="postgresql",
        config={}, factory=_factory,
    )
    assert pool.release_run("r1") == 1
    assert pool.release_run("r1") == 0  # nothing left to close


def test_invalidate_connection_drops_across_runs():
    """Credential rotation: invalidate a conn_id and EVERY run's entry
    for it is closed. Other conn_ids untouched."""
    pool = ConnectionPool()
    a = pool.acquire(connection_id="c1", run_id="r1", conn_type="postgresql", config={}, factory=_factory)
    b = pool.acquire(connection_id="c1", run_id="r2", conn_type="postgresql", config={}, factory=_factory)
    untouched = pool.acquire(connection_id="c2", run_id="r1", conn_type="postgresql", config={}, factory=_factory)

    closed = pool.invalidate_connection("c1")
    assert closed == 2
    assert a.closed and b.closed
    assert not untouched.closed
    # Next acquire for c1 should build a fresh driver, not reuse the closed one.
    fresh = pool.acquire(connection_id="c1", run_id="r1", conn_type="postgresql", config={}, factory=_factory)
    assert fresh is not a


def test_stats():
    pool = ConnectionPool()
    pool.acquire(connection_id="c1", run_id="r1", conn_type="postgresql", config={}, factory=_factory)
    pool.acquire(connection_id="c1", run_id="r2", conn_type="postgresql", config={}, factory=_factory)
    pool.acquire(connection_id="c2", run_id="r1", conn_type="postgresql", config={}, factory=_factory)
    s = pool.stats()
    assert s.total_entries == 3
    assert s.by_connection == {"c1": 2, "c2": 1}
    assert s.by_run == {"r1": 2, "r2": 1}


def test_close_all_drains_pool():
    pool = ConnectionPool()
    a = pool.acquire(connection_id="c1", run_id="r1", conn_type="postgresql", config={}, factory=_factory)
    b = pool.acquire(connection_id="c2", run_id="r2", conn_type="postgresql", config={}, factory=_factory)
    closed = pool.close_all()
    assert closed == 2
    assert a.closed and b.closed
    assert pool.stats().total_entries == 0


def test_missing_run_id_bypasses_pool():
    """Without a run_id the pool can't safely scope lifetime — it just
    builds a new driver and the caller owns it (must close themselves)."""
    pool = ConnectionPool()
    a = pool.acquire(connection_id="c1", run_id="", conn_type="postgresql", config={}, factory=_factory)
    # Pool is empty; the caller got a brand-new driver they must close.
    assert pool.stats().total_entries == 0
    assert isinstance(a, _FakeDriver)


def test_custom_closer():
    """If `closer` is provided, it's called instead of `.close()`."""
    pool = ConnectionPool()
    custom_closes: list[_FakeDriver] = []
    def my_closer(d: _FakeDriver):
        custom_closes.append(d)
        d.closed = True

    pool.acquire(
        connection_id="c1", run_id="r1", conn_type="bigquery",
        config={}, factory=_factory, closer=my_closer,
    )
    pool.release_run("r1")
    assert len(custom_closes) == 1
    assert custom_closes[0].closed


def test_close_failure_is_swallowed():
    """A driver that raises on close() must not break release_run()."""
    class BadDriver:
        def close(self):
            raise RuntimeError("simulated close failure")

    pool = ConnectionPool()
    pool.acquire(
        connection_id="c1", run_id="r1", conn_type="postgresql",
        config={}, factory=lambda ct, c: BadDriver(),
    )
    # Should not raise — release_run is best-effort cleanup.
    closed_count = pool.release_run("r1")
    # close() raised, so the count is 0 (one tried, none reported successful).
    assert closed_count == 0


def test_per_connection_cap_warns_but_serves():
    """When cross-run usage hits the cap, the next acquire still gets a
    connection (we don't deadlock); a warning is logged. Verifies via
    stats() that the over-cap entry is tracked."""
    pool = ConnectionPool(max_per_connection=2)
    pool.acquire(connection_id="c1", run_id="r1", conn_type="postgresql", config={}, factory=_factory)
    pool.acquire(connection_id="c1", run_id="r2", conn_type="postgresql", config={}, factory=_factory)
    # 3rd acquire: at cap (2), should still serve.
    over = pool.acquire(connection_id="c1", run_id="r3", conn_type="postgresql", config={}, factory=_factory)
    assert isinstance(over, _FakeDriver)
    assert pool.stats().by_connection == {"c1": 3}


def test_default_cap_from_constructor():
    """When no env override + no constructor arg, default applies."""
    pool = ConnectionPool()
    assert pool._max == DEFAULT_MAX_PER_CONNECTION


def test_thread_safety_release_during_acquire():
    """Concurrent acquire / release_run must not crash or leak."""
    pool = ConnectionPool()
    stop = threading.Event()
    errors: list[Exception] = []

    def acquirer():
        try:
            for i in range(50):
                pool.acquire(
                    connection_id=f"c{i % 3}", run_id=f"r{i % 5}",
                    conn_type="postgresql", config={}, factory=_factory,
                )
        except Exception as e:  # pragma: no cover - sentinel for failure
            errors.append(e)

    def releaser():
        try:
            while not stop.is_set():
                for rid in ("r0", "r1", "r2", "r3", "r4"):
                    pool.release_run(rid)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    t1 = threading.Thread(target=acquirer)
    t2 = threading.Thread(target=releaser)
    t1.start(); t2.start()
    t1.join()
    stop.set()
    t2.join()
    # Final cleanup
    pool.close_all()
    assert errors == []
