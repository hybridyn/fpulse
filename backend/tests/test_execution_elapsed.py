from datetime import datetime, timedelta, timezone

import pytest

from fpulse.monitoring.store import ExecutionRecord
from fpulse.monitoring.timing import execution_timing


def test_history_detail_lists_and_average_use_same_elapsed_time(execution_store):
    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    execution = ExecutionRecord(workflow_id='timed', project_id='project', status='success',
        started_at=start, completed_at=start + timedelta(seconds=8), duration_ms=390)
    execution_store.record(execution)
    assert execution_store.get(execution.id).duration_ms == 8000
    for rows in [execution_store.list_all(), execution_store.list_by_workflow('timed'),
                 execution_store.list_by_project('project')]:
        assert rows[0]['duration_ms'] == 8000
        assert rows[0]['metadata']['original_reported_duration_ms'] == 390
    assert execution_store.get_stats()['avg_duration_ms'] == 8000


@pytest.mark.parametrize('status', ['success', 'error', 'cancelled'])
def test_timezone_offsets_and_terminal_outcomes(status):
    result = execution_timing({'status': status, 'started_at': '2026-09-18T10:00:00+05:30',
        'completed_at': '2026-09-18T04:30:02Z', 'duration_ms': 10})
    assert result['duration_ms'] == 2000
    assert execution_timing(result) == result


@pytest.mark.parametrize('completed', [None, 'bad-date', '2026-09-18T09:00:00Z'])
def test_missing_or_invalid_timestamps_do_not_invent_duration(completed):
    result = execution_timing({'status': 'success', 'started_at': '2026-09-18T10:00:00Z',
        'completed_at': completed, 'duration_ms': 390})
    assert result['duration_ms'] == 390
    assert result['metadata']['duration_basis'] == 'legacy_reported'


def test_running_record_is_not_completed_and_finalization_updates_elapsed(execution_store):
    start = datetime.now(timezone.utc) - timedelta(seconds=5)
    record = ExecutionRecord(workflow_id='timed', started_at=start)
    execution_store.record(record)
    assert record.completed_at is None
    record.status = 'success'
    record.completed_at = start + timedelta(seconds=5)
    record.duration_ms = 12
    execution_store.record(record)
    record.completed_at = start + timedelta(seconds=9)
    execution_store.record(record)
    assert execution_store.get(record.id).duration_ms == 9000
    assert execution_store.get(record.id).metadata['original_reported_duration_ms'] == 12
