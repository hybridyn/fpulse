from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fpulse.engine.history_output import historical_output
from fpulse.engine.step_output_store import StepOutputStore
from fpulse.monitoring.store import ExecutionRecord, ExecutionStore, StepLog


@pytest.fixture
def output_store(_fpulse_test_db):
    return StepOutputStore(_fpulse_test_db)


def execution(rows=None, days=0):
    return ExecutionRecord(id='history-run', workflow_id='workflow', status='success',
        started_at=datetime.now(timezone.utc) - timedelta(days=days),
        workflow_snapshot={'steps': [{'id': 'step', 'type': 'filter'}]},
        step_logs=[StepLog(step_id='step', rows_processed=2, output_preview=rows)])


def test_actual_log_sample_is_recovered_without_querying_sources(output_store):
    result = historical_output(execution([{'value': 7}]), 'step', output_store)
    assert result['sample_rows'] == [{'value': 7}]
    assert result['capture_source'] == 'step_log'
    assert result['schema'][0]['dtype'] == 'unknown'
    assert result['availability'] == 'available'


def test_missing_and_expired_are_distinct(output_store):
    assert historical_output(execution(), 'step', output_store)['availability'] == 'not_captured'
    expired = historical_output(execution([{'value': 'CANARY'}], days=31), 'step', output_store)
    assert expired['availability'] == 'expired'
    assert expired['sample_rows'] == []
    assert historical_output(execution(), 'unknown', output_store) is None


def test_pruned_capture_does_not_fall_back_to_log_copy(output_store):
    output_store.record('history-run', 'step', row_count=2, sample_rows=[{'value': 1}])
    output_store.prune_samples(ttl_days=-1)
    result = historical_output(execution([{'value': 'CANARY'}]), 'step', output_store)
    assert result['availability'] == 'expired'
    assert result['sample_rows'] == []


def test_zero_rows_is_captured_empty_not_missing(output_store):
    output_store.record('history-run', 'step', row_count=0, sample_rows=[])
    assert historical_output(execution(), 'step', output_store)['availability'] == 'empty'


@pytest.mark.asyncio
async def test_unknown_input_step_is_not_treated_as_root(monkeypatch, output_store):
    from fpulse.api import execution as api
    from fastapi import HTTPException
    history = Mock()
    record = execution()
    history.get.return_value = record
    monkeypatch.setattr(api, 'get_execution_store', lambda: history)
    monkeypatch.setattr(api, 'get_step_output_store', lambda: output_store)
    with pytest.raises(HTTPException) as error:
        await api.get_step_input('history-run', 'unknown', workspace_id='tenant')
    assert error.value.status_code == 404
    result = await api.get_step_input('history-run', 'step', workspace_id='tenant')
    assert result['inputs'] == []
    record.workflow_snapshot['connections'] = [{'from_step': 'missing', 'to_step': 'step'}]
    result = await api.get_step_input('history-run', 'step', workspace_id='tenant')
    assert result['inputs'][0]['row_count'] is None
    assert result['inputs'][0]['availability'] == 'not_captured'


@pytest.mark.asyncio
async def test_expired_export_is_refused_and_workspace_checked(monkeypatch, output_store):
    from fpulse.api import execution as api
    from fastapi import HTTPException
    history = Mock()
    history.get.return_value = execution([{'secret_row': 'CANARY'}], days=31)
    monkeypatch.setattr(api, 'get_execution_store', lambda: history)
    monkeypatch.setattr(api, 'get_step_output_store', lambda: output_store)
    with pytest.raises(HTTPException) as error:
        await api.export_step_output('history-run', 'step', workspace_id='tenant')
    assert error.value.status_code == 410
    history.get.assert_called_with('history-run', workspace_id='tenant')
    history.get.return_value = None
    with pytest.raises(HTTPException) as error:
        await api.get_step_output('history-run', 'step', workspace_id='other')
    assert error.value.status_code == 404


def test_scheduled_execution_captures_rows_under_history_id(
    monkeypatch, output_store, _fpulse_test_db, sample_csv_file, temp_data_dir,
):
    from fpulse import main
    from fpulse.ir.schema import Workflow, Step, StepType, StepConnection
    from fpulse.scheduling.scheduler import PipelineScheduler
    workflow = Workflow(id='disposable-history', steps=[
        Step(id='source', type=StepType.CSV_SOURCE, params={'file_path': 'orders.csv'}),
        Step(id='filter', type=StepType.FILTER, params={'condition': "status = 'active'"}),
    ], connections=[StepConnection(from_step='source', to_step='filter')])
    workflows = Mock()
    workflows.get.return_value = SimpleNamespace(workflow=workflow)
    history = ExecutionStore(_fpulse_test_db)
    monkeypatch.setattr(main, 'app_state', {
        'store': workflows, 'execution_store': history,
        'step_output_store': output_store, 'data_dir': temp_data_dir,
    })
    PipelineScheduler()._run_pipeline('disposable-schedule', workflow.id, 'Disposable')
    records = history.list_by_workflow(workflow.id, workspace_id='default')
    assert len(records) == 1
    record = history.get(records[0]['id'], workspace_id='default')
    assert record.status == 'success', record.error_message
    source = historical_output(record, 'source', output_store)
    filtered = historical_output(record, 'filter', output_store)
    assert source['row_count'] == 5
    assert filtered['row_count'] == 3
    assert len(filtered['sample_rows']) == 3
    assert all(row['status'] == 'active' for row in filtered['sample_rows'])
    assert filtered['capture_source'] == 'step_output'
