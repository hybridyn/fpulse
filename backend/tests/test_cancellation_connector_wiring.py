"""E3.1 wiring tests (2026-06-08).

Covers the connector-side cancel registration helper + the executor's
step-boundary cancel check, WITHOUT a live database. A fake connection
stands in for psycopg2/pyodbc. The actual interruption of a blocked
real query is [LIVE-SMOKE] (a pg_sleep(60) cancelled mid-flight) and
out of scope for unit tests.

Contracts pinned:
  * register_connection_cancel prefers conn.cancel(), falls back to
    conn.close()
  * cancelling the run fires the registered connection cancel
  * unregister removes it (no fire after normal completion)
  * no run_id / no token / no cancel-able method -> graceful no-op
  * the executor's step loop contains the boundary cancel check
"""
from __future__ import annotations

import pytest

from fpulse.engine.cancellation import (
    cancel_run,
    clear_token,
    create_token,
    register_connection_cancel,
    unregister_connection_cancel,
)


class _FakeConn:
    def __init__(self, has_cancel=True, has_close=True):
        self.cancelled = False
        self.closed = False
        if has_cancel:
            self.cancel = self._cancel
        if has_close:
            self.close = self._close

    def _cancel(self):
        self.cancelled = True

    def _close(self):
        self.closed = True


class TestRegisterConnectionCancel:
    def test_prefers_cancel_over_close(self):
        run_id = "e31-run-1"
        create_token(run_id)
        try:
            conn = _FakeConn(has_cancel=True, has_close=True)
            cb = register_connection_cancel(run_id, conn)
            assert cb is not None
            cancel_run(run_id)
            assert conn.cancelled is True
            assert conn.closed is False  # cancel preferred; close not used
        finally:
            clear_token(run_id)

    def test_falls_back_to_close(self):
        run_id = "e31-run-2"
        create_token(run_id)
        try:
            conn = _FakeConn(has_cancel=False, has_close=True)
            register_connection_cancel(run_id, conn)
            cancel_run(run_id)
            assert conn.closed is True
        finally:
            clear_token(run_id)

    def test_no_cancelable_method_returns_none(self):
        run_id = "e31-run-3"
        create_token(run_id)
        try:
            conn = _FakeConn(has_cancel=False, has_close=False)
            assert register_connection_cancel(run_id, conn) is None
        finally:
            clear_token(run_id)

    def test_no_run_id_is_noop(self):
        conn = _FakeConn()
        assert register_connection_cancel("", conn) is None
        assert register_connection_cancel(None, conn) is None

    def test_none_conn_is_noop(self):
        assert register_connection_cancel("r", None) is None

    def test_registers_even_without_preexisting_token(self):
        # Connector may register before the executor created the token;
        # get_or_create makes one.
        run_id = "e31-run-4"
        try:
            conn = _FakeConn()
            cb = register_connection_cancel(run_id, conn)
            assert cb is not None
            cancel_run(run_id)
            assert conn.cancelled is True
        finally:
            clear_token(run_id)


class TestUnregister:
    def test_unregister_prevents_fire(self):
        run_id = "e31-run-5"
        create_token(run_id)
        try:
            conn = _FakeConn()
            cb = register_connection_cancel(run_id, conn)
            unregister_connection_cancel(run_id, cb)
            cancel_run(run_id)
            assert conn.cancelled is False  # unregistered before cancel
        finally:
            clear_token(run_id)

    def test_unregister_noop_when_no_token(self):
        unregister_connection_cancel("never", lambda: None)  # no raise

    def test_unregister_none_callback_safe(self):
        unregister_connection_cancel("r", None)  # no raise


class TestExecutorBoundaryCheck:
    def test_executor_has_step_boundary_cancel_check(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / "fpulse" / "engine" / "executor.py").read_text(encoding="utf-8")
        assert "from fpulse.engine.cancellation import get_token" in src, (
            "E3.1 regression — executor must import get_token for the "
            "step-boundary cancel check"
        )
        assert 'run_result.status = "cancelled"' in src, (
            "E3.1 regression — executor must mark the run cancelled when "
            "the token is cancelled at a step boundary"
        )
