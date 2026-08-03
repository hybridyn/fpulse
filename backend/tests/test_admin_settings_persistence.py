"""Durability of admin_settings writes (regression for 2026-07-28).

Every writer of the singleton ``admin_settings`` row goes through the db
wrapper's generic ``execute()`` — which does NOT commit, on a connection that
runs in the default (deferred) transaction mode. So an ``INSERT OR REPLACE``
sat in an uncommitted transaction on the request thread's connection: the
same-request read-back saw it (read-your-own-writes), which made it *look*
like it worked, but the change was invisible to other worker threads and was
rolled back on process restart.

The Copilot web-access toggle was the reported symptom: ``PUT
/api/ai/web-access {provider:''}`` returned the new value from ``_current()``,
yet after a uvicorn restart the DB still held the old provider — and because
``register_initial_tools()`` reads ``web_access_enabled()`` per request, the
"live toggle, no restart" behaviour silently reverted.

These tests exercise the real handlers/helpers and then re-read through a
*different connection* — a second ``Database`` on the same file (a restart) and
a separate worker thread (a concurrent request) — because only a fresh
connection exposes the missing commit. Reading back on the writing thread's own
connection would pass even against the buggy code, which is exactly the trap
the original manual repro fell into.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from fpulse.storage.database import Database


@pytest.fixture(autouse=True)
def _clear_web_env(monkeypatch):
    """Force the DB path to be the only source of truth for these tests.

    ``web_access_enabled`` / ``get_search_config`` also consult env vars; clear
    them so an assertion can't pass because of an env fallback.
    """
    for var in (
        "FPULSE_AI_WEB_ACCESS",
        "FPULSE_WEB_SEARCH_PROVIDER",
        "FPULSE_WEB_SEARCH_API_KEY",
        "FPULSE_WEB_SEARCH_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)


def _restart(db: Database) -> Database:
    """Close ``db`` and return a fresh Database on the same file.

    This is a faithful "the uvicorn process restarted" simulation: closing
    the old handle releases its locks and rolls back any *uncommitted*
    transaction, so a write that was never committed is simply gone — the
    new handle reads the last durably-committed state, exactly like a new
    process would.
    """
    path = db.db_path
    db.close()
    return Database(path)


def _run_in_fresh_thread(fn):
    """Run ``fn`` on a new thread and return its result.

    The db manager hands each thread its own connection, so this reads
    admin_settings through a connection other than the writer's — the
    only way to observe whether the write was actually committed.
    """
    box: dict[str, object] = {}

    def _target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    t = threading.Thread(target=_target)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["value"]


# ── Web-access toggle (the reported bug) ──────────────────────────────

def test_web_access_write_survives_restart(tmp_path, monkeypatch):
    """PUT web-access, then read it back through a *restarted* process —
    a fresh Database on the same file after the first one is closed."""
    from fpulse.main import app_state
    from fpulse.api.ai_web import WebAccessUpdate, set_web_access
    import fpulse.ai.web as web

    db1 = Database(str(tmp_path / "fpulse.db"))
    monkeypatch.setitem(app_state, "db", db1)

    asyncio.run(set_web_access(WebAccessUpdate(
        enabled=True,
        provider="brave",
        api_key="k-secret-123",
        endpoint="https://search.example.internal",
    )))

    # Restart: close db1 (rolls back an uncommitted write), reopen. If the
    # handler didn't commit, everything below reads the pre-write state.
    db2 = _restart(db1)
    monkeypatch.setitem(app_state, "db", db2)
    try:
        settings = web.read_admin_web_settings()
        assert settings.get(web.SETTING_ENABLED) is True
        assert settings.get(web.SETTING_PROVIDER) == "brave"
        assert settings.get(web.SETTING_API_KEY) == "k-secret-123"
        assert settings.get(web.SETTING_ENDPOINT) == "https://search.example.internal"

        # And through the public resolvers the agent actually calls.
        assert web.web_access_enabled() is True
        provider, api_key, endpoint = web.get_search_config()
        assert (provider, api_key, endpoint) == (
            "brave", "k-secret-123", "https://search.example.internal",
        )
    finally:
        db2.close()


def test_web_access_write_visible_to_other_worker_thread(tmp_path, monkeypatch):
    """The change must be visible to a subsequent request served by a
    different worker thread WITHOUT a restart — this is what
    register_initial_tools() relies on for the live toggle."""
    from fpulse.main import app_state
    from fpulse.api.ai_web import WebAccessUpdate, set_web_access
    import fpulse.ai.web as web

    db = Database(str(tmp_path / "fpulse.db"))
    monkeypatch.setitem(app_state, "db", db)
    try:
        asyncio.run(set_web_access(WebAccessUpdate(enabled=True, provider="tavily")))

        # Read on a different thread → different thread-local connection. An
        # uncommitted write on the writer's connection is invisible here.
        enabled = _run_in_fresh_thread(web.web_access_enabled)
        provider = _run_in_fresh_thread(lambda: web.get_search_config()[0])
        assert enabled is True
        assert provider == "tavily"
    finally:
        db.close()


def test_web_access_clear_provider_survives_restart(tmp_path, monkeypatch):
    """Clearing the provider (provider='') must also persist — this is the
    exact payload from the 2026-07-28 repro (PUT {provider:''})."""
    from fpulse.main import app_state
    from fpulse.api.ai_web import WebAccessUpdate, set_web_access
    import fpulse.ai.web as web

    db1 = Database(str(tmp_path / "fpulse.db"))
    monkeypatch.setitem(app_state, "db", db1)

    # Start with a provider set...
    asyncio.run(set_web_access(WebAccessUpdate(provider="brave", api_key="k1")))
    # ...then clear it.
    asyncio.run(set_web_access(WebAccessUpdate(provider="")))

    db2 = _restart(db1)
    monkeypatch.setitem(app_state, "db", db2)
    try:
        assert web.read_admin_web_settings().get(web.SETTING_PROVIDER) == ""
        assert web.get_search_config()[0] == ""
    finally:
        db2.close()


# ── Other admin_settings writers with the same latent defect ──────────

def test_telemetry_consent_write_survives_restart(tmp_path):
    """set_telemetry_enabled writes admin_settings via the same execute()
    path AND (before the fix) omitted the NOT NULL created_at column, so the
    INSERT aborted and was swallowed. It must persist across a restart."""
    from fpulse.telemetry.consent import is_telemetry_enabled, set_telemetry_enabled

    db1 = Database(str(tmp_path / "fpulse.db"))
    set_telemetry_enabled(db1, True)

    db2 = _restart(db1)
    try:
        assert is_telemetry_enabled(db2) is True
    finally:
        db2.close()


def test_installation_id_is_stable_across_restart(tmp_path):
    """get_installation_id persists a random id on first call; a restart must
    read the SAME id back (else the receiver can't dedupe crash reports)."""
    from fpulse.telemetry.sender import get_installation_id

    db1 = Database(str(tmp_path / "fpulse.db"))
    iid1 = get_installation_id(db1)
    assert iid1 and len(iid1) >= 16

    db2 = _restart(db1)
    try:
        iid2 = get_installation_id(db2)
        assert iid2 == iid1
    finally:
        db2.close()


# ── The notifications config writer (also went through execute()) ─────

def test_notifications_config_write_survives_restart(tmp_path, monkeypatch):
    from fpulse.main import app_state
    from fpulse.api.notifications import _read_config, _write_config

    db1 = Database(str(tmp_path / "fpulse.db"))
    monkeypatch.setitem(app_state, "db", db1)
    _write_config({"long_running_threshold_min": 7, "notify_on_warning": True})

    db2 = _restart(db1)
    monkeypatch.setitem(app_state, "db", db2)
    try:
        cfg = _read_config()
        assert cfg["long_running_threshold_min"] == 7
        assert cfg["notify_on_warning"] is True
    finally:
        db2.close()
