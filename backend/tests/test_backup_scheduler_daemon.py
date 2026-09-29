"""Backup scheduler daemon — the wired scheduled-backup feature (was P0-3).

Covers the pure due-slot logic and an end-to-end tick that actually produces a
backup file, so the "schedule fires a backup" promise is tested, not asserted.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fpulse.storage import backup_scheduler as bs


# ── Pure due-slot logic ────────────────────────────────────────────────

def test_parse_hhmm():
    assert bs._parse_hhmm("02:30") == (2, 30)
    assert bs._parse_hhmm("99:99") == (23, 59)   # clamped
    assert bs._parse_hhmm("garbage") == (2, 0)   # default


def test_due_daily_after_slot_never_backed_up():
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    assert bs._due_now({"frequency": "daily", "daily_time": "02:00"}, None, now) is True


def test_not_due_daily_before_slot():
    now = datetime.now(timezone.utc).replace(hour=1, minute=0, second=0, microsecond=0)
    assert bs._due_now({"frequency": "daily", "daily_time": "02:00"}, None, now) is False


def test_not_due_daily_already_backed_up_since_slot():
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    latest = now.replace(hour=2, minute=5)  # a backup taken just after today's slot
    assert bs._due_now({"frequency": "daily", "daily_time": "02:00"}, latest, now) is False


def test_not_due_weekly_wrong_day():
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    wrong_day = (now.weekday() + 1) % 7
    settings = {"frequency": "weekly", "weekly_day": wrong_day, "daily_time": "02:00"}
    assert bs._due_now(settings, None, now) is False


def test_due_hourly_then_not_after_backup():
    now = datetime.now(timezone.utc).replace(minute=30, second=0, microsecond=0)
    assert bs._due_now({"frequency": "hourly"}, None, now) is True
    top_of_hour = now.replace(minute=0)
    assert bs._due_now({"frequency": "hourly"}, top_of_hour + timedelta(minutes=1), now) is False


# ── End-to-end: a due tick produces a backup, and only once per slot ────

def _seed_db(path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript("CREATE TABLE t (x INTEGER); INSERT INTO t VALUES (1);")
        conn.commit()
    finally:
        conn.close()


def test_daemon_tick_creates_backup_when_due_and_not_twice(tmp_path):
    import fpulse.main as fmain
    fmain.app_state["data_dir"] = str(tmp_path)   # settings/backups/db resolve here
    _seed_db(tmp_path / "fpulse.db")

    settings = dict(bs._DEFAULTS)
    settings.update(enabled=True, frequency="daily", daily_time="00:00", retention_count=5)
    bs.BackupScheduler.save_settings(settings)

    daemon = bs.BackupSchedulerDaemon(interval_seconds=5)
    daemon._tick()  # fire synchronously — a midnight slot is already past

    backups = list((tmp_path / "backups").glob("fpulse_*.db"))
    assert backups, "daemon did not create a scheduled backup for a due slot"

    # Same slot already served → a second tick must not create another.
    daemon._tick()
    assert len(list((tmp_path / "backups").glob("fpulse_*.db"))) == len(backups)


def test_daemon_tick_noop_when_disabled(tmp_path):
    import fpulse.main as fmain
    fmain.app_state["data_dir"] = str(tmp_path)
    _seed_db(tmp_path / "fpulse.db")
    settings = dict(bs._DEFAULTS)
    settings.update(enabled=False, frequency="daily", daily_time="00:00")
    bs.BackupScheduler.save_settings(settings)

    bs.BackupSchedulerDaemon()._tick()
    assert not (tmp_path / "backups").exists() or not list((tmp_path / "backups").glob("fpulse_*.db"))


def test_daemon_start_stop_sets_running_flag(tmp_path):
    import fpulse.main as fmain
    fmain.app_state["data_dir"] = str(tmp_path)  # no settings file → disabled → tick is a no-op
    d = bs.BackupSchedulerDaemon(interval_seconds=5)
    d.start()
    try:
        assert d.is_running is True
        assert bs._DAEMON_RUNNING is True
    finally:
        d.stop()
    assert bs._DAEMON_RUNNING is False
    assert d.is_running is False
