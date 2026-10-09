from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fpulse.api import trust
from fpulse.planner import ai_client


def test_provider_tuple_and_scope_do_not_expose_secrets(monkeypatch):
    resolve = Mock(return_value=('openai', 'SECRET', 'test-model', 'https://private.example'))
    monkeypatch.setattr(ai_client, 'resolve_provider', resolve)
    result = trust._provider_status('user-one', 'workspace-one')
    resolve.assert_called_once_with(user_id='user-one', workspace_id='workspace-one')
    assert result['provider'] == 'openai'
    assert result['model'] == 'test-model'
    assert result['is_local'] is False
    assert result['status'] == 'configured'
    assert 'SECRET' not in str(result) and 'private.example' not in str(result)
    resolve.side_effect = RuntimeError('unavailable')
    assert trust._provider_status()['is_local'] is None
    assert trust._provider_status()['status'] == 'failed'


def test_unmeasured_security_controls_have_no_verification_timestamp():
    for row in trust._security_baseline():
        assert row['status'] == 'not_checked'
        assert row['checked_at'] is None
        assert row['evidence'] is None


def test_telemetry_read_distinguishes_false_and_failure(monkeypatch):
    from fpulse.main import app_state
    db = Mock()
    monkeypatch.setitem(app_state, 'db', db)
    db.fetchone.return_value = {'data': '{"telemetry_enabled": true}'}
    assert trust._read_telemetry_consent() is True
    db.fetchone.return_value = None
    assert trust._read_telemetry_consent() is False
    db.fetchone.side_effect = RuntimeError('offline')
    assert trust._read_telemetry_consent() is None


def test_diagnostics_requires_login_and_uses_account_scope(monkeypatch):
    app = FastAPI()
    app.include_router(trust.router)
    with TestClient(app) as client:
        assert client.get('/api/trust/diagnostics').status_code == 401
        app.dependency_overrides[trust.require_auth] = lambda: SimpleNamespace(id='signed-in-user')
        monkeypatch.setattr(trust, 'current_workspace_id', lambda request: 'workspace-one')
        resolve = Mock(return_value=('ollama', '', 'local-model', 'http://localhost'))
        monkeypatch.setattr(ai_client, 'resolve_provider', resolve)
        monkeypatch.setattr(trust, '_read_telemetry_consent', lambda: False)
        response = client.get('/api/trust/diagnostics')
        assert response.status_code == 200
        resolve.assert_called_once_with(user_id='signed-in-user', workspace_id='workspace-one')
        assert response.json()['security_baseline'][-1]['status'] == 'verified'
