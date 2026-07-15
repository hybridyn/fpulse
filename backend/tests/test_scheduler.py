"""Unit tests for PipelineScheduler — schedule evaluation, cron parsing."""

import pytest
import threading
from datetime import datetime, timezone, timedelta
from fpulse.scheduling.scheduler import PipelineScheduler
from fpulse.scheduling.models import ScheduleType


class TestSchedulerCronField:
    """Test _cron_field_matches static method."""

    def test_wildcard(self):
        assert PipelineScheduler._cron_field_matches(5, "*", 0, 59) is True

    def test_exact_match(self):
        assert PipelineScheduler._cron_field_matches(5, "5", 0, 59) is True
        assert PipelineScheduler._cron_field_matches(5, "10", 0, 59) is False

    def test_step(self):
        assert PipelineScheduler._cron_field_matches(0, "*/5", 0, 59) is True
        assert PipelineScheduler._cron_field_matches(15, "*/5", 0, 59) is True
        assert PipelineScheduler._cron_field_matches(7, "*/5", 0, 59) is False

    def test_comma_list(self):
        assert PipelineScheduler._cron_field_matches(3, "1,3,5", 0, 59) is True
        assert PipelineScheduler._cron_field_matches(4, "1,3,5", 0, 59) is False

    def test_range(self):
        assert PipelineScheduler._cron_field_matches(3, "1-5", 0, 59) is True
        assert PipelineScheduler._cron_field_matches(7, "1-5", 0, 59) is False

    def test_invalid_step(self):
        assert PipelineScheduler._cron_field_matches(5, "*/abc", 0, 59) is False

    def test_invalid_exact(self):
        assert PipelineScheduler._cron_field_matches(5, "abc", 0, 59) is False


class TestSchedulerIsDue:
    @pytest.fixture
    def scheduler(self):
        return PipelineScheduler(check_interval_seconds=30)

    def test_interval_never_run(self, scheduler):
        sched = {
            "schedule_type": "interval",
            "interval_minutes": 60,
            "last_run_at": None,
            "enabled": True,
        }
        now = datetime.now(timezone.utc)
        assert scheduler._is_due(sched, now) is True

    def test_interval_not_due(self, scheduler):
        now = datetime.now(timezone.utc)
        sched = {
            "schedule_type": "interval",
            "interval_minutes": 60,
            "last_run_at": (now - timedelta(minutes=30)).isoformat(),
            "enabled": True,
        }
        assert scheduler._is_due(sched, now) is False

    def test_interval_is_due(self, scheduler):
        now = datetime.now(timezone.utc)
        sched = {
            "schedule_type": "interval",
            "interval_minutes": 60,
            "last_run_at": (now - timedelta(minutes=65)).isoformat(),
            "enabled": True,
        }
        assert scheduler._is_due(sched, now) is True

    def test_daily_due(self, scheduler):
        now = datetime(2026, 4, 2, 9, 0, 0, tzinfo=timezone.utc)
        sched = {
            "schedule_type": "daily",
            "daily_time": "09:00",
            "last_run_at": None,
        }
        assert scheduler._is_due(sched, now) is True

    def test_daily_wrong_hour(self, scheduler):
        now = datetime(2026, 4, 2, 14, 0, 0, tzinfo=timezone.utc)
        sched = {
            "schedule_type": "daily",
            "daily_time": "09:00",
            "last_run_at": None,
        }
        assert scheduler._is_due(sched, now) is False

    def test_daily_already_ran_today(self, scheduler):
        now = datetime(2026, 4, 2, 9, 0, 0, tzinfo=timezone.utc)
        sched = {
            "schedule_type": "daily",
            "daily_time": "09:00",
            "last_run_at": datetime(2026, 4, 2, 9, 0, 0, tzinfo=timezone.utc),
        }
        assert scheduler._is_due(sched, now) is False

    def test_weekly_correct_day(self, scheduler):
        # 2026-04-02 is a Thursday (weekday=3)
        now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)
        sched = {
            "schedule_type": "weekly",
            "weekly_days": [3],
            "weekly_time": "10:00",
            "last_run_at": None,
        }
        assert scheduler._is_due(sched, now) is True

    def test_weekly_wrong_day(self, scheduler):
        now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)
        sched = {
            "schedule_type": "weekly",
            "weekly_days": [0, 1],  # Mon, Tue only
            "weekly_time": "10:00",
            "last_run_at": None,
        }
        assert scheduler._is_due(sched, now) is False

    def test_event_never_due(self, scheduler):
        now = datetime.now(timezone.utc)
        sched = {"schedule_type": "event"}
        assert scheduler._is_due(sched, now) is False

    def test_cron_matching(self, scheduler):
        now = datetime(2026, 4, 2, 10, 30, 0, tzinfo=timezone.utc)
        sched = {
            "schedule_type": "cron",
            "cron_expression": "30 10 * * *",
            "last_run_at": None,
        }
        assert scheduler._is_due(sched, now) is True

    def test_cron_not_matching(self, scheduler):
        now = datetime(2026, 4, 2, 10, 15, 0, tzinfo=timezone.utc)
        sched = {
            "schedule_type": "cron",
            "cron_expression": "30 10 * * *",
            "last_run_at": None,
        }
        assert scheduler._is_due(sched, now) is False

    def test_cron_empty_expression(self, scheduler):
        now = datetime.now(timezone.utc)
        sched = {
            "schedule_type": "cron",
            "cron_expression": "",
            "last_run_at": None,
        }
        assert scheduler._is_due(sched, now) is False


class TestSchedulerStuckGuard:
    """Liveness-based overlap guard (2026-06-16). The watchdog must NOT release
    a run-lock just because a timer elapsed — that let short-interval schedules
    start an overlapping second run mid-flight. It releases only when the run
    thread is actually gone, or past a generous hard cap."""

    @pytest.fixture
    def scheduler(self):
        return PipelineScheduler(check_interval_seconds=30)

    def test_honest_long_run_keeps_lock(self, scheduler):
        stop = threading.Event()
        t = threading.Thread(target=stop.wait, daemon=True)
        t.start()
        try:
            sid = "sched-long"
            # 30 min in — well past the 5-min grace — but the thread is alive.
            scheduler._running_jobs[sid] = datetime.now(timezone.utc) - timedelta(minutes=30)
            scheduler._run_threads[sid] = t
            assert scheduler._stuck_schedule_ids(datetime.now(timezone.utc)) == []
        finally:
            stop.set()
            t.join(timeout=2)

    def test_dead_run_lock_is_released(self, scheduler):
        t = threading.Thread(target=lambda: None, daemon=True)
        t.start()
        t.join(timeout=2)  # thread has finished → dead
        sid = "sched-dead"
        scheduler._running_jobs[sid] = datetime.now(timezone.utc) - timedelta(minutes=30)
        scheduler._run_threads[sid] = t
        assert scheduler._stuck_schedule_ids(datetime.now(timezone.utc)) == [sid]

    def test_within_grace_never_flagged(self, scheduler):
        t = threading.Thread(target=lambda: None, daemon=True)
        t.start()
        t.join(timeout=2)  # dead, but the lock is only 10s old
        sid = "sched-fresh"
        scheduler._running_jobs[sid] = datetime.now(timezone.utc) - timedelta(seconds=10)
        scheduler._run_threads[sid] = t
        assert scheduler._stuck_schedule_ids(datetime.now(timezone.utc)) == []

    def test_hard_cap_releases_even_when_alive(self, scheduler):
        stop = threading.Event()
        t = threading.Thread(target=stop.wait, daemon=True)
        t.start()
        try:
            scheduler.STUCK_JOB_HARD_CAP_S = 60  # shrink for the test
            sid = "sched-hung"
            scheduler._running_jobs[sid] = datetime.now(timezone.utc) - timedelta(minutes=10)
            scheduler._run_threads[sid] = t
            assert scheduler._stuck_schedule_ids(datetime.now(timezone.utc)) == [sid]
        finally:
            stop.set()
            t.join(timeout=2)

    def test_missing_handle_evicted_after_grace(self, scheduler):
        sid = "sched-nohandle"
        scheduler._running_jobs[sid] = datetime.now(timezone.utc) - timedelta(minutes=30)
        # no _run_threads entry → can't prove liveness → evict (leaked lock)
        assert scheduler._stuck_schedule_ids(datetime.now(timezone.utc)) == [sid]


class TestSchedulerStartStop:
    """Scheduler start/stop now routes through ExecutionManager
    (Apr 22 2026 migration — PR5 step 9 follow-up). Tests initialize
    a real manager + worker pool so the spawn_scheduled call path is
    exercised end-to-end."""

    @pytest.fixture
    def manager(self):
        from fpulse.engine.execution_manager import ExecutionManager
        from fpulse.engine.worker_pool import WorkerPool
        ExecutionManager._reset_for_tests()
        pool = WorkerPool(max_workers=1)

        # Governor that never rejects — we're testing scheduler
        # lifecycle, not admission control.
        from fpulse.engine.global_governor import GlobalResourceGovernor
        gov = GlobalResourceGovernor(
            mem_defer_pct=99.5, mem_slow_pct=99.7, mem_red_pct=99.9,
            cpu_defer_pct=99.5, sample_cache_s=0,
        )
        mgr = ExecutionManager.initialize(
            worker_pool=pool, governor=gov, register_atexit=False,
        )
        pool.start()  # after manager init so the pool's watchdog registers via spawn_thread
        try:
            yield mgr
        finally:
            mgr.shutdown(timeout_s=2)
            ExecutionManager._reset_for_tests()

    def test_start_stop(self, manager):
        scheduler = PipelineScheduler(check_interval_seconds=1)
        assert scheduler.is_running is False
        scheduler.start()
        assert scheduler.is_running is True
        scheduler.stop()
        assert scheduler.is_running is False

    def test_active_jobs_empty(self):
        scheduler = PipelineScheduler()
        assert scheduler.active_jobs == {}

    def test_double_start_no_crash(self, manager):
        scheduler = PipelineScheduler(check_interval_seconds=1)
        scheduler.start()
        scheduler.start()  # Should log warning, not crash
        scheduler.stop()

    def test_scheduler_registered_in_manager_stats(self, manager):
        """Post-migration verification: the scheduler shows up in
        `mgr.stats().by_kind["scheduled"]` while running. Confirms
        the spawn_scheduled call path works end-to-end."""
        scheduler = PipelineScheduler(check_interval_seconds=5)
        assert manager.stats().by_kind.get("scheduled", 0) == 0
        scheduler.start()
        try:
            assert manager.stats().by_kind["scheduled"] == 1
            # And the handle owner is "scheduler" (set in spawn_scheduled call).
            owners = [r.handle.owner for r in manager.inspect()]
            assert "scheduler" in owners
        finally:
            scheduler.stop()

    def test_manager_shutdown_stops_scheduler(self, manager):
        """If the operator shuts down the manager before calling
        scheduler.stop(), the scheduler's loop still exits (via the
        stop_event set by manager.shutdown's distributed cancel)."""
        scheduler = PipelineScheduler(check_interval_seconds=5)
        scheduler.start()
        handle = scheduler._handle
        assert handle is not None

        # Manager shutdown cancels every scheduled primitive.
        manager.shutdown(timeout_s=3)

        # After shutdown, the handle's registry entry is gone.
        assert not [r for r in manager.inspect() if r.handle.id == handle.id]
        # scheduler.stop() tolerates the stale handle.
        scheduler.stop()
        assert scheduler.is_running is False
