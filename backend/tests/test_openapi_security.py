import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from fpulse.connectors.openapi_import import manifest_from_openapi
from fpulse.connectors.openapi_security import build_security, normalize_security
from fpulse.connectors import rest_framework as rf


SCHEMES = {
    "bearer": {"type": "http", "scheme": "bearer"},
    "basic": {"type": "http", "scheme": "basic"},
    "header": {"type": "apiKey", "in": "header", "name": "X-Key"},
    "query": {"type": "apiKey", "in": "query", "name": "api_key"},
    "cookie": {"type": "apiKey", "in": "cookie", "name": "sessionid"},
    "oauth": {"type": "oauth2", "flows": {"clientCredentials": {"tokenUrl": "https://example.com/token"}}},
}


def definition(requirements):
    return normalize_security({"components": {"securitySchemes": SCHEMES}}, requirements)


def values(auth, index=0):
    return {param: "canary" for scheme in auth["alternatives"][index]["schemes"] for param in scheme["fields"].values()}


def test_operation_override_inheritance_and_unused_declarations():
    spec = {"components": {"securitySchemes": SCHEMES}, "security": [{"basic": []}],
            "paths": {"/inherit": {"get": {}}, "/public": {"get": {"security": []}},
                      "/key": {"get": {"security": [{"header": []}]}}}}
    manifest = manifest_from_openapi(spec)
    inherit, public, key = manifest["streams"]
    assert inherit["auth"]["alternatives"][0]["schemes"][0]["name"] == "basic"
    assert build_security(public["auth"], {}) == ({}, {})
    assert key["auth"]["alternatives"][0]["schemes"][0]["name"] == "header"
    assert not any("bearer" in p["label"] for p in manifest["params"])


@pytest.mark.parametrize("kind,expected", [
    ("bearer", ({"Authorization": "Bearer canary"}, {})),
    ("basic", ({"Authorization": "Basic Y2FuYXJ5OmNhbmFyeQ=="}, {})),
    ("header", ({"X-Key": "canary"}, {})),
    ("query", ({}, {"api_key": "canary"})),
    ("cookie", ({"Cookie": "sessionid=canary"}, {})),
])
def test_static_auth(kind, expected):
    auth, _ = definition([{kind: []}])
    assert build_security(auth, values(auth)) == expected


def test_and_and_explicit_or_including_anonymous():
    auth, _ = definition([{"header": [], "query": [], "cookie": []}, {}])
    with pytest.raises(ValueError, match="Select"):
        build_security(auth, values(auth))
    assert build_security(auth, {**values(auth), "auth_alternative": 0}) == (
        {"X-Key": "canary", "Cookie": "sessionid=canary"}, {"api_key": "canary"})
    assert build_security(auth, {**values(auth), "auth_alternative": 1}) == ({}, {})


@pytest.mark.parametrize("selection", [-1, True, "bogus", 99, 1.5])
def test_invalid_selection(selection):
    auth, _ = definition([{"header": []}])
    with pytest.raises(ValueError):
        build_security(auth, {**values(auth), "auth_alternative": selection})


@pytest.mark.parametrize("requirement", [{"missing": []}, {"oauth": ["read"]}])
def test_unsupported_fails_closed(requirement):
    auth, _ = definition([requirement])
    assert not auth["alternatives"][0]["supported"]
    with pytest.raises(ValueError, match="not supported"):
        build_security(auth, {})


def test_missing_credentials_and_header_injection_are_redacted():
    auth, _ = definition([{"header": []}])
    for value in (None, "", "secret\r\nInjected: canary"):
        with pytest.raises(ValueError) as error:
            build_security(auth, {next(iter(values(auth))): value})
        assert "canary" not in str(error.value)


def test_conflicting_and_headers_fail_closed():
    auth, _ = definition([{"basic": [], "bearer": []}])
    with pytest.raises(ValueError, match="Conflicting"):
        build_security(auth, values(auth))


def test_basic_allows_empty_password_for_api_key_username():
    auth, _ = definition([{"basic": []}])
    fields = auth["alternatives"][0]["schemes"][0]["fields"]
    assert build_security(auth, {fields["username"]: "canary"}) == ({"Authorization": "Basic Y2FuYXJ5Og=="}, {})


def test_runtime_uses_operation_auth_and_redacts_http_errors(monkeypatch):
    auth, _ = definition([{"query": []}])
    stream = {"name": "things", "path": "/things", "auth": auth}
    manifest = rf.RestConnectorManifest(id="test", name="test", base_url="https://example.com", auth={"type": "none"})
    requests = []
    def request(url, headers, **kwargs):
        requests.append((url, headers))
        raise urllib.error.HTTPError(url, 401, "canary", {}, None)
    monkeypatch.setattr(rf, "_http_request", request)
    with pytest.raises(RuntimeError) as error:
        rf._execute_stream(manifest, stream, values(auth))
    assert requests[0][0] == "https://example.com/things?api_key=canary"
    assert str(error.value) == "API request failed (HTTP 401)"


def test_cross_origin_redirect_blocked_before_forwarding():
    request = urllib.request.Request("https://example.com", headers={"X-Key": "canary"})
    with pytest.raises(ValueError, match="Cross-origin"):
        rf._SsrfGuardedRedirectHandler().redirect_request(request, None, 302, "", {}, "https://other.example/path")


def test_cross_origin_pagination_blocked(monkeypatch):
    calls = []
    def request(url, headers, **kwargs):
        calls.append(url)
        return {"items": [{}], "next": "https://other.example/leak"}, {}
    monkeypatch.setattr(rf, "_http_request", request)
    manifest = rf.RestConnectorManifest(id="test", name="test", base_url="https://example.com")
    with pytest.raises(ValueError, match="Cross-origin"):
        rf._execute_stream(manifest, {"path": "/", "data_path": "items", "pagination": {"type": "url", "next_url_path": "next"}}, {})
    assert len(calls) == 1


def test_static_credentials_arrive_at_real_http_server(monkeypatch):
    captured = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            captured.append((self.path, dict(self.headers)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'[{"ok":true}]')

        def log_message(self, *args):
            pass

    monkeypatch.setenv(rf.API_SOURCE_ALLOW_PRIVATE_ENV, "1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        manifest = rf.RestConnectorManifest(id="mock", name="Mock", base_url=f"http://127.0.0.1:{server.server_port}")
        for kind in SCHEMES.keys() - {"oauth"}:
            auth, _ = definition([{kind: []}])
            assert rf._execute_stream(manifest, {"path": "/rows", "auth": auth}, values(auth)) == [{"ok": True}]
            path, headers = captured[-1]
            expected_headers, expected_query = build_security(auth, values(auth))
            for key, value in expected_headers.items():
                assert headers[key] == value
            assert urllib.parse.parse_qs(urllib.parse.urlsplit(path).query) == {k: [v] for k, v in expected_query.items()}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
