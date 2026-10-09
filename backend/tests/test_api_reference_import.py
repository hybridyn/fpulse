import asyncio
import json
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from fpulse.api import api_explorer as explorer


@pytest.fixture
def download(monkeypatch):
    connection = MagicMock()
    response = connection.getresponse.return_value
    response.status = 200
    response.getheader.return_value = None
    monkeypatch.setattr(explorer.http.client, 'HTTPConnection', lambda *args, **kwargs: connection)
    monkeypatch.setattr(explorer.socket, 'socket', MagicMock())
    monkeypatch.setattr(explorer.ssl, 'create_default_context', MagicMock())
    target = MagicMock(return_value=(explorer.urlsplit(explorer.REFERENCE_URLS['stripe']), (2, 1, 6, '', ('1.1.1.1', 443))))
    monkeypatch.setattr(explorer, '_target', target)
    return connection, response, target


def test_large_json_and_more_than_200_endpoints(download):
    connection, response, target = download
    raw = json.dumps({'openapi': '3.0.0', 'description': 'x' * (3 * 1024 * 1024),
                      'servers': [{'url': 'https://api.stripe.com'}],
                      'paths': {f'/v1/{i}': {'get': {}} for i in range(594)}}).encode()
    response.read1.side_effect = [raw, b'']
    result = explorer.load_reference('stripe')
    assert len(result['operations']) == 594 and not result['limit_reached']
    assert result['bytes'] == len(raw)
    assert result['base_url'] == 'https://api.stripe.com'
    target.assert_called_once_with(explorer.REFERENCE_URLS['stripe'])
    assert 'Authorization' not in connection.request.call_args.kwargs['headers']
    connection.close.assert_called_once()


@pytest.mark.parametrize('status', [301, 404, 500])
def test_no_redirect_or_error_body_parsing(download, status):
    connection, response, _ = download
    response.status = status
    with pytest.raises(ValueError, match=f'HTTP {status}'):
        explorer.load_reference('stripe')
    response.read1.assert_not_called()
    connection.close.assert_called_once()


def test_declared_and_streamed_size_limits(download, monkeypatch):
    _, response, _ = download
    monkeypatch.setattr(explorer, 'SPEC_LIMIT', 10)
    response.getheader.return_value = '11'
    with pytest.raises(ValueError, match='limit'):
        explorer.load_reference('stripe')
    response.getheader.return_value = None
    response.read1.side_effect = [b'x' * 11]
    with pytest.raises(ValueError, match='limit'):
        explorer.load_reference('stripe')


def test_allowlist_and_authenticated_route():
    with pytest.raises(ValidationError):
        explorer.ReferenceRequest(reference='http://127.0.0.1')
    app = FastAPI()
    app.include_router(explorer.router)
    with TestClient(app) as client:
        assert client.post('/explorer/reference', json={'reference': 'stripe'}).status_code in (401, 403)


def test_network_failure_has_actionable_safe_error(monkeypatch):
    def fail(reference):
        raise TimeoutError('private diagnostic')
    monkeypatch.setattr(explorer, 'load_reference', fail)
    with pytest.raises(HTTPException) as error:
        asyncio.run(explorer.import_reference(explorer.ReferenceRequest(reference='stripe')))
    assert error.value.status_code == 502
    assert 'private diagnostic' not in error.value.detail


def test_operation_limit_is_visible():
    result = explorer.discover_spec({'openapi': '3.0.0', 'paths': {f'/{i}': {'get': {}} for i in range(5001)}})
    assert len(result['operations']) == 5000 and result['limit_reached']
