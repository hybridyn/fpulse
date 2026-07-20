"""Pinned tests for the cancellation foundation (E3, 2026-06-08).

Third executor-maturity milestone from docs/design/executor-maturity-1.2.md.
Foundation only - the executor step-boundary checks + per-connector
driver.cancel() registration are E3.1, deferred.

Contracts pinned:
  * Token lifecycle: not cancelled -> cancel() -> is_cancelled
  * raise_if_cancelled raises RunCancelled only after cancel
  * Registered callbacks fire exactly once on cancel
  * cancel() is idempotent (callbacks fire once even if called twice)
  * A callback that raises doesn't block the others (best-effort)
  * Late registration (after cancel) fires immediately
  * unregister removes a callback before it can fire
  * bind_stop_event bridges both directions
  * Registry create/get/clear/cancel_run + leak detection
  * Thread-safety: concurrent cancel + check doesn't corrupt state
"""
from __future__ import annotations

import threading

import pytest

from fpulse.engine.cancellation import (
    CancellationToken,
    RunCancelled,
    active_token_count,
    cancel_run,
    clear_token,
    create_token,
    get_or_create_token,
    get_token,
)


# ── Token lifecycle ─────────────────────────────────────────────────


class TestTokenLifecycle:
    def test_starts_not_cancelled(self):
        tok = CancellationToken("run-1")
        assert tok.is_cancelled is False

    def test_cancel_flips_flag(self):
        tok = CancellationToken("run-1")
        tok.cancel()
        assert tok.is_cancelled is True

    def test_raise_if_cancelled_noop_when_live(self):
        tok = CancellationToken("run-1")
        tok.raise_if_cancelled()  # should NOT raise

    def test_raise_if_cancelled_raises_after_cancel(self):
        tok = CancellationToken("run-1")
        tok.cancel()
        with pytest.raises(RunCancelled) as exc:
            tok.raise_if_cancelled()
        assert exc.value.run_id == "run-1"

    def test_run_cancelled_carries_run_id(self):
        e = RunCancelled("run-xyz")
        assert e.run_id == "run-xyz"
        assert "run-xyz" in str(e)


# ── Driver cancel callbacks ─────────────────────────────────────────


class TestCallbacks:
    def test_callback_fires_on_cancel(self):
        tok = CancellationToken("run-1")
        fired = []
        tok.register_cancel_callback(lambda: fired.append("driver-cancel"))
        assert fired == []
        tok.cancel()
        assert fired == ["driver-cancel"]

    def test_multiple_callbacks_all_fire(self):
        tok = CancellationToken("run-1")
        fired = []
        for i in range(3):
            tok.register_cancel_callback(lambda i=i: fired.append(i))
        tok.cancel()
        assert sorted(fired) == [0, 1, 2]

    def test_cancel_idempotent_callbacks_fire_once(self):
        tok = CancellationToken("run-1")
        fired = []
        tok.register_cancel_callback(lambda: fired.append("x"))
        tok.cancel()
        tok.cancel()  # second call must NOT re-fire
        assert fired == ["x"]

    def test_raising_callback_does_not_block_others(self):
        tok = CancellationToken("run-1")
        fired = []

        def _boom():
            raise RuntimeError("driver cancel failed")

        tok.register_cancel_callback(_boom)
        tok.register_cancel_callback(lambda: fired.append("survived"))
        tok.cancel()  # must NOT raise
        # The good callback still fired despite the bad one raising
        assert fired == ["survived"]

    def test_late_registration_fires_immediately(self):
        # Race: cancel arrives between connection-open and callback-register.
        # The callback must fire immediately on registration.
        tok = CancellationToken("run-1")
        tok.cancel()
        fired = []
        tok.register_cancel_callback(lambda: fired.append("late"))
        assert fired == ["late"]

    def test_unregister_prevents_firing(self):
        tok = CancellationToken("run-1")
        fired = []

        def _cb():
            fired.append("should-not-fire")

        tok.register_cancel_callback(_cb)
        tok.unregister_cancel_callback(_cb)
        tok.cancel()
        assert fired == []

    def test_unregister_unknown_callback_is_safe(self):
        tok = CancellationToken("run-1")
        tok.unregister_cancel_callback(lambda: None)  # never registered - no raise


# ── Stop-event bridge ───────────────────────────────────────────────


class TestStopEventBridge:
    def test_token_cancel_sets_bound_event(self):
        tok = CancellationToken("run-1")
        ev = threading.Event()
        tok.bind_stop_event(ev)
        tok.cancel()
        assert ev.is_set()

    def test_bound_event_set_elsewhere_reflects_in_token(self):
        tok = CancellationToken("run-1")
        ev = threading.Event()
        tok.bind_stop_event(ev)
        ev.set()  # execution_manager cancelled via the Event directly
        assert tok.is_cancelled is True

    def test_binding_already_cancelled_token_sets_event(self):
        tok = CancellationToken("run-1")
        tok.cancel()
        ev = threading.Event()
        tok.bind_stop_event(ev)
        assert ev.is_set()

    def test_binding_already_set_event_cancels_token(self):
        tok = CancellationToken("run-1")
        ev = threading.Event()
        ev.set()
        tok.bind_stop_event(ev)
        assert tok.is_cancelled is True


# ── Registry ────────────────────────────────────────────────────────


class TestRegistry:
    def _cleanup(self, *run_ids):
        for r in run_ids:
            clear_token(r)

    def test_create_then_get(self):
        tok = create_token("reg-run-1")
        try:
            assert get_token("reg-run-1") is tok
        finally:
            self._cleanup("reg-run-1")

    def test_create_is_idempotent(self):
        a = create_token("reg-run-2")
        b = create_token("reg-run-2")
        try:
            assert a is b, "second create must return the same token"
        finally:
            self._cleanup("reg-run-2")

    def test_get_missing_returns_none(self):
        assert get_token("never-created-xyz") is None

    def test_get_or_create_creates(self):
        tok = get_or_create_token("reg-run-3")
        try:
            assert get_token("reg-run-3") is tok
        finally:
            self._cleanup("reg-run-3")

    def test_cancel_run_cancels_existing(self):
        tok = create_token("reg-run-4")
        try:
            assert cancel_run("reg-run-4") is True
            assert tok.is_cancelled is True
        finally:
            self._cleanup("reg-run-4")

    def test_cancel_run_missing_returns_false(self):
        assert cancel_run("never-created-abc") is False

    def test_clear_token_removes(self):
        create_token("reg-run-5")
        clear_token("reg-run-5")
        assert get_token("reg-run-5") is None

    def test_clear_missing_is_safe(self):
        clear_token("never-created-def")  # no raise

    def test_active_token_count_tracks(self):
        before = active_token_count()
        create_token("reg-run-6")
        create_token("reg-run-7")
        try:
            assert active_token_count() >= before + 2
        finally:
            self._cleanup("reg-run-6", "reg-run-7")

    def test_connector_registers_before_executor_creates(self):
        # Connector opens connection + registers a cancel callback via
        # get_or_create_token BEFORE the executor's create_token. They
        # must share the same token so the executor's later cancel
        # fires the connector's callback.
        fired = []
        connector_tok = get_or_create_token("reg-run-8")
        connector_tok.register_cancel_callback(lambda: fired.append("driver"))
        try:
            # Executor "creates" the same run's token later - gets the same one
            executor_tok = create_token("reg-run-8")
            assert executor_tok is connector_tok
            cancel_run("reg-run-8")
            assert fired == ["driver"]
        finally:
            self._cleanup("reg-run-8")


# ── Thread safety ───────────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_cancel_and_check(self):
        # One thread cancels; many threads poll is_cancelled. No
        # corruption, no missed cancel, callback fires exactly once.
        tok = CancellationToken("run-concurrent")
        fire_count = []
        lock = threading.Lock()

        def _cb():
            with lock:
                fire_count.append(1)

        tok.register_cancel_callback(_cb)

        stop = threading.Event()
        results = []

        def _poller():
            while not stop.is_set():
                results.append(tok.is_cancelled)

        pollers = [threading.Thread(target=_poller) for _ in range(8)]
        for p in pollers:
            p.start()

        # Cancel from several threads at once - idempotency must hold
        cancellers = [threading.Thread(target=tok.cancel) for _ in range(4)]
        for c in cancellers:
            c.start()
        for c in cancellers:
            c.join()

        stop.set()
        for p in pollers:
            p.join()

        assert tok.is_cancelled is True
        # Callback fired exactly once despite 4 concurrent cancels
        assert sum(fire_count) == 1
