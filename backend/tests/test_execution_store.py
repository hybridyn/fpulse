"""Unit tests for ExecutionStore."""

import pytest
from datetime import datetime, timezone, timedelta
from fpulse.monitoring.store import ExecutionStore, ExecutionRecord, StepLog


class TestExecutionStore:
    def test_record(self, execution_store):
        exe = ExecutionRecord(
            id="exe-001", workflow_id="wf-001", workflow_name="Test",
            status="success", steps_total=3, steps_completed=3,
        )
        result = execution_store.record(exe)
        assert result.id == "exe-001"

    def test_get(self, execution_store):
        exe = ExecutionRecord(id="exe-001", workflow_id="wf-001")
        execution_store.record(exe)
        found = execution_store.get("exe-001")
        assert found is not None
        assert found.workflow_id == "wf-001"

    def test_get_nonexistent(self, execution_store):
        assert execution_store.get("nope") is None

    def test_update(self, execution_store):
        exe = ExecutionRecord(id="exe-001", workflow_id="wf-001", status="running")
        execution_store.record(exe)
        updated = execution_store.update("exe-001", {"status": "success", "steps_completed": 5})
        assert updated is not None
        assert updated.status == "success"
        assert updated.steps_completed == 5

    def test_update_nonexistent(self, execution_store):
        assert execution_store.update("nope", {"status": "x"}) is None

    def test_list_all(self, execution_store):
        for i in range(5):
            execution_store.record(ExecutionRecord(id=f"exe-{i}", workflow_id="wf-001"))
        result = execution_store.list_all()
        assert len(result) == 5

    def test_list_all_reversed_order(self, execution_store):
        execution_store.record(ExecutionRecord(id="first", workflow_id="wf-001"))
        execution_store.record(ExecutionRecord(id="second", workflow_id="wf-001"))
        result = execution_store.list_all()
        assert result[0]["id"] == "second"

    def test_list_all_respects_limit(self, execution_store):
        for i in range(10):
            execution_store.record(ExecutionRecord(id=f"exe-{i}", workflow_id="wf-001"))
        result = execution_store.list_all(limit=3)
        assert len(result) == 3

    def test_list_by_workflow(self, execution_store):
        execution_store.record(ExecutionRecord(id="e1", workflow_id="wf-001"))
        execution_store.record(ExecutionRecord(id="e2", workflow_id="wf-002"))
        execution_store.record(ExecutionRecord(id="e3", workflow_id="wf-001"))
        result = execution_store.list_by_workflow("wf-001")
        assert len(result) == 2

    def test_list_by_project(self, execution_store):
        execution_store.record(ExecutionRecord(id="e1", workflow_id="wf-001", project_id="proj-a"))
        execution_store.record(ExecutionRecord(id="e2", workflow_id="wf-002", project_id="proj-b"))
        result = execution_store.list_by_project("proj-a")
        assert len(result) == 1


class TestExecutionStats:
    def test_stats_empty(self, execution_store):
        stats = execution_store.get_stats()
        assert stats["total"] == 0
        assert stats["success_rate"] == 0

    def test_stats_counts(self, execution_store):
        execution_store.record(ExecutionRecord(id="e1", workflow_id="wf", status="success", duration_ms=100))
        execution_store.record(ExecutionRecord(id="e2", workflow_id="wf", status="success", duration_ms=200))
        execution_store.record(ExecutionRecord(id="e3", workflow_id="wf", status="error"))
        stats = execution_store.get_stats()
        assert stats["total"] == 3
        assert stats["success"] == 2
        assert stats["failed"] == 1
        assert stats["success_rate"] == 66.7

    def test_stats_avg_duration(self, execution_store):
        execution_store.record(ExecutionRecord(id="e1", workflow_id="wf", status="success", duration_ms=100))
        execution_store.record(ExecutionRecord(id="e2", workflow_id="wf", status="success", duration_ms=300))
        stats = execution_store.get_stats()
        assert stats["avg_duration_ms"] == 200.0

    def test_stats_period_filtering(self, execution_store):
        """Old executions should not be counted in recent stats."""
        old = ExecutionRecord(id="old", workflow_id="wf", status="success")
        old.started_at = datetime.now(timezone.utc) - timedelta(hours=48)
        execution_store.record(old)
        execution_store.record(ExecutionRecord(id="new", workflow_id="wf", status="success"))
        stats = execution_store.get_stats(hours=24)
        assert stats["total"] == 1


class TestStepLog:
    def test_step_log_in_execution(self, execution_store):
        exe = ExecutionRecord(
            id="exe-001", workflow_id="wf-001",
            step_logs=[
                StepLog(step_id="s1", step_name="Load", status="success", rows_processed=100),
                StepLog(step_id="s2", step_name="Filter", status="error", error_message="Bad column"),
            ],
        )
        execution_store.record(exe)
        found = execution_store.get("exe-001")
        assert len(found.step_logs) == 2
        assert found.step_logs[0].rows_processed == 100
        assert found.step_logs[1].error_message == "Bad column"
