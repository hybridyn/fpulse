"""Unit tests for ScheduleStore."""

import pytest
from fpulse.scheduling.models import Schedule, ScheduleType
from fpulse.scheduling.store import ScheduleStore


class TestScheduleStore:
    def test_create(self, schedule_store, sample_schedule):
        s = schedule_store.create(sample_schedule)
        assert s.id == "sched-001"
        assert s.name == "Daily Run"

    def test_get(self, schedule_store, sample_schedule):
        schedule_store.create(sample_schedule)
        s = schedule_store.get("sched-001")
        assert s is not None
        assert s.workflow_id == "test-wf-001"

    def test_get_nonexistent(self, schedule_store):
        assert schedule_store.get("nope") is None

    def test_list_all(self, schedule_store, sample_schedule):
        schedule_store.create(sample_schedule)
        s2 = Schedule(id="sched-002", workflow_id="wf-002", name="Hourly")
        schedule_store.create(s2)
        result = schedule_store.list_all()
        assert len(result) == 2

    def test_list_by_workflow(self, schedule_store, sample_schedule):
        schedule_store.create(sample_schedule)
        s2 = Schedule(id="sched-002", workflow_id="wf-002", name="Other")
        schedule_store.create(s2)
        result = schedule_store.list_by_workflow("test-wf-001")
        assert len(result) == 1
        assert result[0]["id"] == "sched-001"

    def test_list_by_project(self, schedule_store, sample_schedule):
        schedule_store.create(sample_schedule)
        result = schedule_store.list_by_project("default")
        assert len(result) == 1

    def test_update(self, schedule_store, sample_schedule):
        schedule_store.create(sample_schedule)
        updated = schedule_store.update("sched-001", {"name": "Updated Run"})
        assert updated is not None
        assert updated.name == "Updated Run"

    def test_update_nonexistent(self, schedule_store):
        assert schedule_store.update("nope", {"name": "X"}) is None

    def test_delete(self, schedule_store, sample_schedule):
        schedule_store.create(sample_schedule)
        assert schedule_store.delete("sched-001") is True
        assert schedule_store.get("sched-001") is None

    def test_delete_nonexistent(self, schedule_store):
        assert schedule_store.delete("nope") is False

    def test_record_run(self, schedule_store, sample_schedule):
        schedule_store.create(sample_schedule)
        schedule_store.record_run("sched-001", "success")
        s = schedule_store.get("sched-001")
        assert s.last_run_status == "success"
        assert s.run_count == 1
        assert s.last_run_at is not None

    def test_record_run_increments_count(self, schedule_store, sample_schedule):
        schedule_store.create(sample_schedule)
        schedule_store.record_run("sched-001", "success")
        schedule_store.record_run("sched-001", "error")
        s = schedule_store.get("sched-001")
        assert s.run_count == 2
        assert s.last_run_status == "error"
