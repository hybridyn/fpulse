"""Consistent execution elapsed time; step timings remain independent."""

from datetime import datetime, timezone


def execution_timing(record: dict) -> dict:
    result = dict(record)
    metadata = dict(result.get('metadata') or {})
    result['metadata'] = metadata
    if result.get('status') in ('running', 'queued', 'pending'):
        return result
    try:
        def parse(value):
            stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
            return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
        elapsed = (parse(result['completed_at']) - parse(result['started_at'])).total_seconds() * 1000
        if elapsed < 0:
            raise ValueError('Completion precedes start')
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        metadata.setdefault('duration_basis', 'legacy_reported')
        return result
    metadata.setdefault('original_reported_duration_ms', result.get('duration_ms'))
    metadata['duration_basis'] = 'recorded_start_to_finish'
    result['duration_ms'] = round(elapsed, 3)
    return result
