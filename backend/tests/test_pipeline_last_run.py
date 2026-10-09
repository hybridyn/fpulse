"""Pipeline Last Run must agree with canonical execution history."""
from datetime import datetime, timedelta, timezone

from fpulse.ir.schema import Workflow
from fpulse.monitoring.store import ExecutionRecord


def test_last_run_reads_history_without_optional_logger(workflow_store, execution_store):
    workflow_store.save(Workflow(id="pipeline", name="History source"))
    start = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)
    execution_store.record(ExecutionRecord(
        id="first", workflow_id="pipeline", status="success", started_at=start))
    execution_store.record(ExecutionRecord(
        id="latest", workflow_id="pipeline", status="error", triggered_by="schedule",
        started_at=start + timedelta(minutes=5)))
    row = workflow_store.list_all(workspace_id="default")[0]
    latest = execution_store.list_by_workflow("pipeline", workspace_id="default")[0]
    assert datetime.fromisoformat(row["last_run"]) == datetime.fromisoformat(latest["started_at"])
    assert row["last_run_status"] == "error"


def test_last_run_is_workspace_scoped_even_for_admin_listing(workflow_store, execution_store):
    workflow_store.save(Workflow(id="pipeline", name="Scoped history", workspace_id="default"))
    start = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)
    execution_store.record(ExecutionRecord(
        workflow_id="pipeline", workspace_id="default", status="success", started_at=start))
    execution_store.record(ExecutionRecord(
        workflow_id="pipeline", workspace_id="other", status="error",
        started_at=start + timedelta(days=1)))
    for rows in (workflow_store.list_all(), workflow_store.list_all(workspace_id="default")):
        assert rows[0]["last_run"] == start.isoformat()
        assert rows[0]["last_run_status"] == "success"


def test_no_retained_history_does_not_use_modified_time(workflow_store):
    workflow_store.save(Workflow(id="pipeline", name="No retained history"))
    row = workflow_store.list_all(workspace_id="default")[0]
    assert row["last_run"] == ""
    assert row["last_run_status"] == ""


def test_latest_run_uses_actual_time_and_stable_tie_break(workflow_store, execution_store):
    workflow_store.save(Workflow(id="pipeline", name="Timezone history"))
    for run_id, at, status in (
        ("a", "2026-09-18T14:00:00+05:30", "success"),
        ("b", "2026-09-18T09:00:00+00:00", "error"),
        ("c", "2026-09-18T09:00:00+00:00", "running"),
    ):
        execution_store.record(ExecutionRecord(
            id=run_id, workflow_id="pipeline", status=status, started_at=datetime.fromisoformat(at)))
    row = workflow_store.list_all(workspace_id="default")[0]
    assert row["last_run"] == "2026-09-18T09:00:00+00:00"
    assert row["last_run_status"] == "running"


def test_busy_pipeline_does_not_hide_older_run_of_another(workflow_store, execution_store):
    workflow_store.save(Workflow(id="quiet", name="Quiet pipeline"))
    start = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)
    execution_store.record(ExecutionRecord(workflow_id="quiet", started_at=start, status="success"))
    for index in range(205):
        execution_store.record(ExecutionRecord(
            workflow_id="busy", started_at=start + timedelta(seconds=index + 1)))
    assert workflow_store.list_all(workspace_id="default")[0]["last_run"] == start.isoformat()
