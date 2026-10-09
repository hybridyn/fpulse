import asyncio
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fpulse.api import api_explorer as explorer


@pytest.fixture
def server(monkeypatch):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.command, self.path, dict(self.headers)))
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', 'http://169.254.169.254/metadata')
                self.end_headers()
                return
            self.send_response(401 if self.path == '/denied' else 200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Set-Cookie', 'secret=CANARY')
            self.end_headers()
            payload = b'x' * (explorer.LIMIT + 20) if self.path == '/large' else json.dumps({'records': [{'id': 1, 'name': 'Row'}], 'access_token': 'CANARY', 'echo': self.headers.get('Authorization', '')}).encode()
            self.wfile.write(payload)
        do_POST = do_GET
        def log_message(self, *args):
            pass
    monkeypatch.setenv('FPULSE_API_SOURCE_ALLOW_PRIVATE', '1')
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{http.server_port}', received
    http.shutdown(); http.server_close(); thread.join(timeout=5)


@pytest.mark.parametrize('kind', ['none', 'bearer', 'basic', 'api_key', 'api_key_query'])
def test_request_auth_and_redaction(server, kind):
    url, received = server
    result = explorer.inspect_request(explorer.InspectRequest(url=url, auth_type=kind, token='CANARY', username='user', password='CANARY', key_name='X-Key'))
    assert result['status'] == 200
    assert result['data']['records'][0]['id'] == 1
    assert 'CANARY' not in json.dumps(result)
    assert 'Set-Cookie' not in result['headers']
    _, path, headers = received[0]
    if kind == 'bearer': assert headers['Authorization'] == 'Bearer CANARY'
    if kind == 'basic': assert headers['Authorization'].startswith('Basic ')
    if kind == 'api_key': assert headers['X-Key'] == 'CANARY'
    if kind == 'api_key_query': assert 'X-Key=CANARY' in path


def test_write_requires_confirmation_and_never_retries(server):
    url, received = server
    with pytest.raises(ValueError, match='Confirm'):
        explorer.inspect_request(explorer.InspectRequest(url=url, method='POST'))
    assert received == []
    explorer.inspect_request(explorer.InspectRequest(url=url, method='POST', confirm_write=True))
    assert len(received) == 1


def test_redirect_errors_and_response_cap(server):
    url, received = server
    assert explorer.inspect_request(explorer.InspectRequest(url=url + '/redirect'))['status'] == 302
    assert len(received) == 1
    assert explorer.inspect_request(explorer.InspectRequest(url=url + '/denied'))['status'] == 401
    large = explorer.inspect_request(explorer.InspectRequest(url=url + '/large'))
    assert large['truncated'] and large['bytes'] == explorer.LIMIT


def test_private_and_metadata_targets_blocked(monkeypatch):
    monkeypatch.delenv('FPULSE_API_SOURCE_ALLOW_PRIVATE', raising=False)
    with pytest.raises(ValueError, match='blocked'):
        explorer._target('http://127.0.0.1/')
    monkeypatch.setenv('FPULSE_API_SOURCE_ALLOW_PRIVATE', '1')
    with pytest.raises(ValueError, match='blocked'):
        explorer._target('http://169.254.169.254/')


@pytest.mark.parametrize('headers', [{'Host': 'internal'}, {'Authorization': 'secret'}, {'X-Test': 'bad\r\nheader'}])
def test_header_injection_blocked(headers):
    with pytest.raises(ValueError):
        explorer._request_parts(explorer.InspectRequest(url='https://example.com', headers=headers))


def test_discovery_does_not_generate_connectors():
    spec = {'openapi': '3.0.0', 'servers': [{'url': 'https://example.com'}], 'paths': {'/records/{id}': {'get': {}, 'delete': {'security': [{'Key': []}]}}}}
    result = asyncio.run(explorer.discover(explorer.DiscoverRequest(text=json.dumps(spec))))
    assert [o['method'] for o in result['operations']] == ['GET', 'DELETE']
    assert 'manifest' not in result


def test_network_endpoints_require_authentication():
    app = FastAPI()
    app.include_router(explorer.router)
    with TestClient(app) as client:
        assert client.post('/explorer/request', json={'url': 'https://example.com'}).status_code in (401, 403)


def test_dns_is_resolved_once_and_connected_to_validated_address(server, monkeypatch):
    url, received = server
    original = socket.getaddrinfo
    calls = []
    def resolve(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    explorer.inspect_request(explorer.InspectRequest(url=url))
    assert len(calls) == 1
