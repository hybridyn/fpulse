"""F-Pulse OSS is a single-operator install: self-service registration is OFF
by default and an operator opts in via Admin -> Security.

Two things had to change to make that true, and both are pinned here:
  1. `_read_signup_policy()` must DEFAULT to allow_self_registration=False.
  2. The v2->v3 migration must NOT force-enable signup (it historically flipped
     False -> True "without admin consent" — reversed for single-operator).
"""
from __future__ import annotations

import json
import sqlite3

import fpulse.api.auth as auth
from fpulse.main import app_state
from fpulse.storage.database import Database


class _NoSettingsDB:
    """A fresh install: no admin_settings row yet."""

    def fetchone(self, *a, **k):
        return None


class _ExplicitEnableDB:
    """An operator who explicitly turned self-registration ON."""

    def fetchone(self, *a, **k):
        return {"data": json.dumps({"allow_self_registration": True})}


class _OneUserStore:
    """Non-empty user table -> NOT first-user bootstrap."""

    def list_users(self):
        return [object()]


def test_signup_policy_defaults_off(monkeypatch):
    monkeypatch.setitem(app_state, "db", _NoSettingsDB())
    monkeypatch.setitem(app_state, "user_store", _OneUserStore())
    policy = auth._read_signup_policy()
    assert policy["allow_self_registration"] is False
    assert policy["first_user_bootstrap"] is False


def test_signup_policy_honors_explicit_enable(monkeypatch):
    monkeypatch.setitem(app_state, "db", _ExplicitEnableDB())
    monkeypatch.setitem(app_state, "user_store", _OneUserStore())
    assert auth._read_signup_policy()["allow_self_registration"] is True


def test_v3_migration_no_longer_force_enables_signup():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE settings (id TEXT PRIMARY KEY, data TEXT)")
    conn.execute(
        "INSERT INTO settings (id, data) VALUES ('admin_settings', ?)",
        (json.dumps({"allow_self_registration": False}),),
    )
    conn.commit()

    Database.__new__(Database)._migrate_v3_signup_default(conn)

    # The migration must LEAVE the value alone (was False -> stays False).
    row = conn.execute(
        "SELECT data FROM settings WHERE id = 'admin_settings'"
    ).fetchone()
    assert json.loads(row[0])["allow_self_registration"] is False

    # ...but still record the idempotency marker so it never re-runs.
    marker = conn.execute(
        "SELECT value FROM _meta WHERE key = 'signup_default_v3'"
    ).fetchone()
    assert marker is not None and marker[0] == "1"
