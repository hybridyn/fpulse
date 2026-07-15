"""Cancellation foundation (2026-06-08, E3 of executor-maturity-1.2).

# The gap this closes

The execution_manager already supports COOPERATIVE cancel via a
``threading.Event`` per run thread (``cancel_by_id`` sets the event;
the worker checks ``stop_event.is_set()`` at loop boundaries). That
works for pure-Python loops.

It does NOT work for a thread blocked inside a long-running driver
call - e.g. a 30-minute ``SELECT`` on Snowflake. The thread is parked
in C waiting on the socket; it can't check the Event until the query
returns. Setting the flag does nothing until then.

The fix (per docs/design/executor-maturity-1.2.md gap 2): let each
connector register a NATIVE cancel callback - ``psycopg2`` connection
``.cancel()``, ``pyodbc`` connection ``.close()``, Snowflake's
``query_cancel`` - that this layer fires when the run is cancelled.
That actually interrupts the blocked driver call.

# What ships here (foundation)

  * ``RunCancelled`` exception
  * ``CancellationToken`` - per-run cancel state + a registry of
    driver-level cancel callbacks. ``cancel()`` flips the flag AND
    fires every registered callback. ``raise_if_cancelled()`` is the
    cooperative check for step boundaries.
  * A module-level registry keyed by ``run_id`` so the executor (which
    holds the run_id) and connectors (which hold the live connection)
    can find the same token without threading it through every call.
  * ``bind_stop_event()`` - optional bridge so a token stays in sync
    with the execution_manager's existing ``threading.Event``; cancel
    on either surface flips both.

# What's deferred (E3.1, per-connector, focused sessions)

  * The executor calling ``token.raise_if_cancelled()`` at each step
    boundary (load-bearing executor edit)
  * Each connector adapter registering its driver ``.cancel()`` /
    ``.close()`` callback when it opens a connection (postgres /
    mssql / snowflake / bigquery)
  * A ``RunStatus.CANCELLED`` distinct from ``FAILED`` end-to-end
  * The test pin: spawn a ``pg_sleep(60)``, cancel after 2s, assert
    termination within 5s

Those touch live connections + the executor's hot loop and each
needs its own focused session + integration test.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)


class RunCancelled(Exception):
    """Raised by ``CancellationToken.raise_if_cancelled()`` when a run
    has been cancelled. The executor catches this and maps it to a
    CANCELLED (not FAILED) status - retry never applies to a
    deliberate cancel."""

    def __init__(self, run_id: str = "", message: str = ""):
        self.run_id = run_id
        super().__init__(message or f"run {run_id or '<unknown>'} was cancelled")


class CancellationToken:
    """Per-run cancellation state + driver-level cancel callbacks.

    Thread-safe: ``cancel()`` is called from a DIFFERENT thread (the
    API request handling the user's "Stop" click) than the one
    executing the run. All state mutations take the lock.
    """

    def __init__(self, run_id: str = ""):
        self.run_id = run_id
        self._cancelled = False
        self._lock = threading.RLock()
        self._callbacks: list[Callable[[], None]] = []
        # Optional bridge to the execution_manager's cooperative Event.
        self._stop_event: threading.Event | None = None

    # ── State ────────────────────────────────────────────────────────

    @property
    def is_cancelled(self) -> bool:
        with self._lock:
            if self._cancelled:
                return True
            # Stay in sync if a bound Event was set elsewhere.
            if self._stop_event is not None and self._stop_event.is_set():
                self._cancelled = True
                return True
            return False

    def raise_if_cancelled(self) -> None:
        """Cooperative check for step boundaries. Raises RunCancelled
        when the run has been cancelled; no-op otherwise."""
        if self.is_cancelled:
            raise RunCancelled(self.run_id)

    # ── Cancel ───────────────────────────────────────────────────────

    def cancel(self) -> None:
        """Flip the flag, sync the bound Event, and fire every
        registered driver-level cancel callback. Idempotent - calling
        twice fires callbacks only once.

        A callback that raises is logged and swallowed: one connector's
        cancel failure must not prevent the others from firing."""
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            if self._stop_event is not None:
                self._stop_event.set()
            callbacks = list(self._callbacks)
        # Fire callbacks OUTSIDE the lock - a driver .cancel() may block
        # briefly, and we don't want to hold the lock (which is.cancelled
        # readers also take) for the duration.
        for cb in callbacks:
            try:
                cb()
            except Exception as exc:  # noqa: BLE001 - best-effort cancel
                logger.warning(
                    "cancellation callback for run %s raised: %s",
                    self.run_id, exc,
                )

    # ── Driver callbacks ─────────────────────────────────────────────

    def register_cancel_callback(self, fn: Callable[[], None]) -> None:
        """Register a native driver-level cancel hook. Connectors call
        this when they open a connection, passing e.g.
        ``conn.cancel`` (psycopg2) or ``conn.close`` (pyodbc).

        If the token is ALREADY cancelled when a callback registers, it
        fires immediately - covers the race where cancel arrives between
        connection open and callback registration."""
        fire_now = False
        with self._lock:
            if self._cancelled:
                fire_now = True
            else:
                self._callbacks.append(fn)
        if fire_now:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "late cancel callback for run %s raised: %s",
                    self.run_id, exc,
                )

    def unregister_cancel_callback(self, fn: Callable[[], None]) -> None:
        """Remove a previously-registered callback. Connectors call
        this when they CLOSE a connection normally, so a later cancel
        doesn't try to cancel a dead connection."""
        with self._lock:
            try:
                self._callbacks.remove(fn)
            except ValueError:
                pass  # already gone - fine

    def bind_stop_event(self, event: threading.Event) -> None:
        """Bridge this token to the execution_manager's existing
        cooperative ``threading.Event``. After binding, cancel on
        EITHER surface flips both: token.cancel() sets the event, and
        is_cancelled reflects the event being set elsewhere."""
        with self._lock:
            self._stop_event = event
            # If either side is already tripped, converge immediately.
            if self._cancelled:
                event.set()
            elif event.is_set():
                self._cancelled = True


# ── Module-level registry ────────────────────────────────────────────
# Keyed by run_id so the executor (holds run_id) and connectors (hold
# the live connection) reach the same token without threading it
# through every function signature.

_registry: dict[str, CancellationToken] = {}
_registry_lock = threading.RLock()


def create_token(run_id: str) -> CancellationToken:
    """Create (or return the existing) token for a run. Idempotent:
    a second call for the same run_id returns the first token so
    connectors that register before the executor creates don't get a
    detached token."""
    with _registry_lock:
        tok = _registry.get(run_id)
        if tok is None:
            tok = CancellationToken(run_id)
            _registry[run_id] = tok
        return tok


def get_token(run_id: str) -> CancellationToken | None:
    """Return the token for a run, or None if none exists."""
    with _registry_lock:
        return _registry.get(run_id)


def get_or_create_token(run_id: str) -> CancellationToken:
    """Connectors use this: they may register a cancel callback before
    the executor has created the token. Returns the existing token or
    creates one."""
    return create_token(run_id)


def cancel_run(run_id: str) -> bool:
    """Cancel a run by id. Returns True if a token existed (and was
    cancelled), False if no token was registered for that run."""
    with _registry_lock:
        tok = _registry.get(run_id)
    if tok is None:
        return False
    tok.cancel()
    return True


def clear_token(run_id: str) -> None:
    """Remove a run's token from the registry. The executor calls this
    when a run finishes (success / failure / cancel) so the registry
    doesn't leak tokens for completed runs."""
    with _registry_lock:
        _registry.pop(run_id, None)


def active_token_count() -> int:
    """Number of tokens currently in the registry. Test/diagnostics
    helper - a non-zero count after all runs finish indicates a
    clear_token leak."""
    with _registry_lock:
        return len(_registry)


# ── Connector helper (E3.1) ──────────────────────────────────────────


def register_connection_cancel(run_id: str | None, conn) -> Callable[[], None] | None:
    """Register a database connection's native cancel hook on the run's
    cancellation token, so cancelling the run interrupts a query blocked
    inside the driver.

    Connectors call this right after opening a connection:

        cb = register_connection_cancel(ctx.run_id, conn)
        try:
            ... run the (possibly long) query ...
        finally:
            unregister_connection_cancel(ctx.run_id, cb)

    Picks the best native interrupt the driver exposes:
      * ``conn.cancel()``  - psycopg2 / snowflake (interrupts the
        in-flight statement, leaves the connection usable)
      * ``conn.close()``   - pyodbc / others (closes the socket, which
        unblocks the parked thread)

    Returns the registered callback (pass it back to
    ``unregister_connection_cancel``), or None when there's no run_id /
    no token / the connection exposes neither method. Never raises.
    """
    if not run_id or conn is None:
        return None
    cancel_fn = getattr(conn, "cancel", None)
    if not callable(cancel_fn):
        close_fn = getattr(conn, "close", None)
        if not callable(close_fn):
            return None
        cancel_fn = close_fn
    try:
        tok = get_or_create_token(run_id)
        tok.register_cancel_callback(cancel_fn)
        return cancel_fn
    except Exception:  # noqa: BLE001 - cancel plumbing must never break a query
        return None


def unregister_connection_cancel(run_id: str | None, callback) -> None:
    """Remove a previously-registered connection cancel callback (call
    in the connector's ``finally`` after the query completes normally,
    so a later cancel doesn't poke a closed connection). Never raises."""
    if not run_id or callback is None:
        return
    try:
        tok = get_token(run_id)
        if tok is not None:
            tok.unregister_cancel_callback(callback)
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "RunCancelled",
    "CancellationToken",
    "create_token",
    "get_token",
    "get_or_create_token",
    "cancel_run",
    "clear_token",
    "active_token_count",
    "register_connection_cancel",
    "unregister_connection_cancel",
]
