"""F-Pulse OSS does NOT auto-create an admin by default — the operator creates
their own account on first launch (empty DB -> first_user_bootstrap -> the first
/register becomes super_admin). Auto-seeding admin@fpulse.local is OPT-IN via
`FPULSE_BOOTSTRAP_ADMIN=1`, for headless / Docker / CI deploys that can't do an
interactive first run. This pins both halves.

(The rest of the suite sets FPULSE_BOOTSTRAP_ADMIN=1 in conftest.py so it can
keep assuming the seeded admin exists; here we drive UserStore._ensure_admin
directly with the flag both cleared and set.)
"""
from __future__ import annotations

from fpulse.auth.store import UserStore
from fpulse.storage.database import Database


def _fresh_store(tmp_path, dbname):
    # A fresh on-disk DB runs all migrations (creates the users table), exactly
    # like a first boot. UserStore(db=...) calls _ensure_admin() in its ctor.
    return Database(str(tmp_path / dbname))


def test_no_admin_autoseed_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("FPULSE_BOOTSTRAP_ADMIN", raising=False)
    store = UserStore(db=_fresh_store(tmp_path, "default_off.db"))
    # Empty user table -> the frontend shows "create account" and the first
    # /register becomes super_admin. No pre-baked account, no password file.
    assert store.get_user("admin") is None
    assert len(store.list_users()) == 0


def test_admin_autoseed_when_opted_in(tmp_path, monkeypatch):
    monkeypatch.setenv("FPULSE_BOOTSTRAP_ADMIN", "1")
    store = UserStore(db=_fresh_store(tmp_path, "optin.db"))
    admin = store.get_user("admin")
    assert admin is not None
    assert admin.email == "admin@fpulse.local"
    assert admin.role == "admin"


def test_flag_values_other_than_1_do_not_seed(tmp_path, monkeypatch):
    # Only the exact string "1" opts in — a stray "0"/"true"/"" must NOT seed.
    for val in ("0", "true", "yes", ""):
        monkeypatch.setenv("FPULSE_BOOTSTRAP_ADMIN", val)
        store = UserStore(db=_fresh_store(tmp_path, f"val_{val or 'empty'}.db"))
        assert store.get_user("admin") is None, f"unexpected seed for value {val!r}"
