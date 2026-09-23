"""Tests for the telemetry sender.

Hard contracts under test:
  1. Sender is no-op when consent is off.
  2. Payload conforms exactly to TELEMETRY_PAYLOAD_SCHEMA — no extra fields.
  3. Stack trace sanitizer drops user paths and env-var values.
  4. Queue drains via fake http client; failures are retried up to limit.
  5. revoke_and_flush() empties the queue without sending.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from fpulse.telemetry import (
    TELEMETRY_PAYLOAD_SCHEMA,
    build_event,
    get_queue,
    revoke_and_flush,
    send_event,
)
from fpulse.telemetry import sender as sender_mod


# ── Fakes ─────────────────────────────────────────────────────────────


class FakeDB:
    """In-memory stand-in for the SQLite settings store."""

    def __init__(self, telemetry_enabled: bool = True, install_id: str | None = None):
        self._settings: dict[str, Any] = {"telemetry_enabled": telemetry_enabled}
        if install_id:
            self._settings["telemetry_installation_id"] = install_id

    def fetchone(self, sql: str, *params):
        if "settings" in sql:
            return {"data": json.dumps(self._settings)}
        return None

    def execute(self, sql: str, params=()):
        # Detect INSERT OR REPLACE on settings → update our dict.
        if "settings" in sql and params:
            self._settings = json.loads(params[0])

    def commit(self):
        # No-op: this fake mutates its dict synchronously in execute().
        # Present so it mirrors the real Database interface — writers now
        # call db.commit() to actually flush the SQLite transaction.
        pass


class FakeHttpClient:
    """Captures POSTs. Configurable status code per call."""

    def __init__(self, status_codes: list[int] | None = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._codes = list(status_codes or [200])

    async def post(self, url: str, *, json: dict[str, Any], timeout: float):
        self.calls.append((url, json))
        code = self._codes.pop(0) if self._codes else 200

        class _Resp:
            status_code = code

        return _Resp()


@pytest.fixture(autouse=True)
def reset_queue():
    sender_mod._reset_for_tests()
    yield
    sender_mod._reset_for_tests()


# ── Consent gate ─────────────────────────────────────────────────────


def test_send_event_noop_when_consent_off():
    db = FakeDB(telemetry_enabled=False)

    async def run():
        return await send_event("startup", db=db, fpulse_version="1.0.0")

    sent = asyncio.run(run())
    assert sent is False
    assert get_queue().size() == 0


# ── Payload shape ────────────────────────────────────────────────────


def test_build_event_startup_has_required_fields_only():
    db = FakeDB()
    payload = build_event("startup", db=db, fpulse_version="1.0.0")

    # Every key in payload is in the schema's allowed set.
    expected_required = {"event_type", "fpulse_version", "python_version",
                         "os_family", "feature_flags", "installation_id"}
    assert expected_required.issubset(payload.keys())

    # No leakage — startup events have no exception fields.
    assert "exception_type" not in payload
    assert "stack_trace" not in payload

    # Match the documented schema's allowed key set.
    schema_fields = set(TELEMETRY_PAYLOAD_SCHEMA["fields"].keys())
    for key in payload.keys():
        assert key in schema_fields, f"unexpected key in payload: {key!r}"


def test_build_event_crash_includes_sanitized_traceback():
    db = FakeDB()
    raw_tb = (
        'Traceback (most recent call last):\n'
        '  File "/home/alice/secret/script.py", line 12, in <module>\n'
        '    something()\n'
        '  File "/usr/local/lib/python3.11/fpulse/api/agent.py", line 100, in run\n'
        '    raise RuntimeError("boom — FPULSE_API_KEY=sk_live_real")\n'
        'RuntimeError: boom\n'
    )
    payload = build_event(
        "crash",
        db=db,
        fpulse_version="1.0.0",
        exception_type="RuntimeError",
        stack_trace=raw_tb,
    )
    tb = payload["stack_trace"]
    # User home path scrubbed.
    assert "/home/alice" not in tb
    # Env-var value scrubbed.
    assert "sk_live_real" not in tb
    # fpulse frame retained.
    assert "fpulse/api/agent.py" in tb


def test_build_event_rejects_unknown_event_type():
    db = FakeDB()
    with pytest.raises(ValueError):
        build_event("login", db=db, fpulse_version="1.0.0")


def test_validate_rejects_extra_fields_after_construction():
    # Direct call to _validate_payload_shape via build_event with a tampered
    # payload would require monkeypatching; instead assert the helper
    # rejects an extra key.
    from fpulse.telemetry.sender import _validate_payload_shape
    bad = {
        "event_type": "startup",
        "fpulse_version": "1.0.0",
        "python_version": "3.11.7",
        "os_family": "Linux",
        "feature_flags": {},
        "installation_id": "abc",
        "user_email": "leak@example.com",  # forbidden
    }
    with pytest.raises(ValueError):
        _validate_payload_shape(bad)


# ── Installation ID ──────────────────────────────────────────────────


def test_installation_id_is_stable_across_calls():
    db = FakeDB()
    a = sender_mod.get_installation_id(db)
    b = sender_mod.get_installation_id(db)
    assert a == b
    assert len(a) >= 16


# ── Queue drain ──────────────────────────────────────────────────────


def test_queue_drain_sends_events():
    db = FakeDB()
    http = FakeHttpClient(status_codes=[200])

    async def run():
        await send_event("startup", db=db, fpulse_version="1.0.0")
        sent = await get_queue().drain_once(http)
        return sent

    sent = asyncio.run(run())
    assert sent == 1
    assert len(http.calls) == 1
    url, payload = http.calls[0]
    assert "telemetry" in url
    assert payload["event_type"] == "startup"


def test_queue_retries_on_failure_then_succeeds():
    db = FakeDB()
    # First send fails (500), second send is the retry — we need to drain twice.
    http = FakeHttpClient(status_codes=[500, 200])

    async def run():
        await send_event("startup", db=db, fpulse_version="1.0.0")
        first = await get_queue().drain_once(http)
        second = await get_queue().drain_once(http)
        return first, second

    first, second = asyncio.run(run())
    assert first == 0
    assert second == 1
    assert len(http.calls) == 2


# ── Revocation ───────────────────────────────────────────────────────


def test_revoke_and_flush_drops_queue_without_sending():
    db = FakeDB()
    http = FakeHttpClient(status_codes=[200])

    async def run():
        await send_event("startup", db=db, fpulse_version="1.0.0")
        assert get_queue().size() == 1
        await revoke_and_flush(db)
        assert get_queue().size() == 0
        sent = await get_queue().drain_once(http)
        return sent

    sent = asyncio.run(run())
    assert sent == 0
    assert len(http.calls) == 0
