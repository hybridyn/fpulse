"""Read historical samples without querying current sources or destinations."""

from datetime import datetime, timedelta, timezone

from fpulse.engine.step_output_store import SAMPLE_TTL_DAYS, StepOutputStore, schema_from_sample


def historical_output(execution, step_id, store, *, now=None):
    now = now or datetime.now(timezone.utc)
    snapshot = execution.workflow_snapshot or {}
    step = next((s for s in snapshot.get('steps', []) if s.get('id') == step_id), None)
    log = next((s for s in execution.step_logs if s.step_id == step_id), None)
    record = store.get_step(execution.id, step_id)
    if record is None and step is None and log is None:
        return None
    if record is None:
        rows = log.output_preview if log else None
        capped, _, _, truncated = StepOutputStore._apply_caps(rows or [])
        names = list(dict.fromkeys(key for row in capped for key in row))
        record = {
            'execution_id': execution.id, 'step_id': step_id,
            'step_type': (step or {}).get('type', log.step_type if log else ''),
            'label': (step or {}).get('label') or (log.step_name if log else step_id),
            'status': log.status if log else 'unknown',
            'row_count': log.rows_processed if log else None,
            'sample_rows': capped, 'sample_truncated': truncated,
            'sample_pruned': False,
            'schema': schema_from_sample(capped, [{'name': key, 'type': 'unknown'} for key in names]),
            'captured_at': execution.started_at.isoformat(),
            'capture_source': 'step_log' if rows is not None else 'none',
            'missing': rows is None or (not rows and log.rows_processed > 0),
        }
    else:
        record = {**record, 'capture_source': 'step_output', 'missing': False}

    try:
        captured = datetime.fromisoformat(record['captured_at'].replace('Z', '+00:00'))
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        expired = captured < now - timedelta(days=SAMPLE_TTL_DAYS)
    except (ValueError, TypeError, KeyError):
        expired = True  # Unknown sample age must not bypass retention.
    if record.get('sample_pruned') or expired:
        record.update(sample_rows=[], sample_pruned=True, availability='expired')
    elif record['missing']:
        record['availability'] = 'not_captured'
    elif not record.get('sample_rows') and record.get('row_count', 0):
        record.update(missing=True, availability='not_captured')
    else:
        record['availability'] = 'available' if record.get('sample_rows') else 'empty'
    return record
