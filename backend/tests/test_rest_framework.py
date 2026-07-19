"""Tests for the v1 REST manifest runtime (rest_framework.py).

Focused on the 2026-05-23 T1 upgrade: manifest-level ``default_query`` /
``default_headers`` merging and the ``pagination.type="url"`` follow.
Before T1 those fields existed on shipped manifests (sap_odata,
servicenow, netsuite, dynamics365, twilio, ms_teams) but were silently
dropped by the runtime. These tests pin the merge contract so a future
refactor can't quietly re-break them.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

import pytest

from fpulse.connectors import rest_framework as rf


# ── Helpers ───────────────────────────────────────────────────────────────


class _FakeHTTPDriver:
    """Captures every HTTP request so tests can assert URL / headers /
    method / body / pagination.

    Signature widened 2026-06-01 to mirror the framework's `_http_request`
    upgrade (verb + body support). Tests that only care about URL and
    headers keep working unchanged because method/body default to GET/None.
    """

    def __init__(self, responses: list[Any]):
        # Each entry is either a dict (body) or a tuple (body, headers).
        self.responses = list(responses)
        # `calls` is the back-compat tuple-shape `(url, headers)` so
        # existing tests written before the 2026-06-01 verb-upgrade keep
        # working with their `url, headers = driver.calls[i]` unpack.
        self.calls: list[tuple[str, dict[str, str]]] = []
        # `requests` carries the full request record (method/body too)
        # for new tests that need to assert verb / payload semantics.
        self.requests: list[dict[str, Any]] = []

    def __call__(self, url: str, headers: dict[str, str],
                 method: str = "GET", body: Any = None,
                 body_text: str | None = None):
        self.calls.append((url, dict(headers)))
        self.requests.append({
            "url": url,
            "headers": dict(headers),
            "method": method,
            "body": body,
            "body_text": body_text,
        })
        if not self.responses:
            raise AssertionError(f"Unexpected extra request to {url}")
        item = self.responses.pop(0)
        if isinstance(item, tuple):
            body_resp, resp_headers = item
        else:
            body_resp, resp_headers = item, {}
        return body_resp, resp_headers


def _patch_http(monkeypatch, responses: list[Any]) -> _FakeHTTPDriver:
    driver = _FakeHTTPDriver(responses)
    # Patch the real boundary (`_http_request`) — the GET-only
    # `_http_get` alias still exists for back-compat with any external
    # caller, but the executor now goes through `_http_request`.
    monkeypatch.setattr(rf, "_http_request", driver)
    return driver


# ── default_query / default_headers (T1) ──────────────────────────────────


def test_default_query_is_merged_into_streams(monkeypatch):
    """sap_odata-style: $format=json + sap-client appended to every stream."""
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase",
        "name": "T",
        "base_url": "https://host",
        "auth": {"type": "none"},
        "default_query": {"$format": "json", "sap-client": "{client}"},
        "streams": [
            {"name": "s", "path": "/A_BP", "data_path": "d.results",
             "pagination": {"type": "none"}},
        ],
    })
    driver = _patch_http(monkeypatch, [{"d": {"results": [{"x": 1}]}}])
    rows = rf._execute_stream(manifest, manifest.streams[0], {"client": "100"})

    assert rows == [{"x": 1}]
    url, _headers = driver.calls[0]
    # Allow URL-encoded form (%24format) too — semantically equivalent per
    # RFC 3986. Some HTTP clients aggressively percent-encode reserved
    # chars; SAP OData accepts both forms.
    decoded = unquote(url)
    assert "$format=json" in decoded
    assert "sap-client=100" in decoded


def test_stream_query_overrides_default_query(monkeypatch):
    """Per-stream values win on conflict — defaults are fallback."""
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://host",
        "auth": {"type": "none"},
        "default_query": {"$format": "json", "sap-client": "100"},
        "streams": [
            {"name": "s", "path": "/A_BP",
             "query": {"sap-client": "200"},
             "data_path": "d.results",
             "pagination": {"type": "none"}},
        ],
    })
    driver = _patch_http(monkeypatch, [{"d": {"results": []}}])
    rf._execute_stream(manifest, manifest.streams[0], {})

    url, _ = driver.calls[0]
    assert "sap-client=200" in url
    assert "sap-client=100" not in url


def test_empty_interpolated_default_query_value_is_dropped(monkeypatch):
    """SAP rejects ``sap-client=`` (empty). Manifest declares the param
    but tenant didn't bind it — the runtime must drop the empty value
    rather than send a 400-causing query string."""
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://host",
        "auth": {"type": "none"},
        "default_query": {"sap-client": "{client}", "keep": "yes"},
        "streams": [{"name": "s", "path": "/x", "data_path": "",
                     "pagination": {"type": "none"}}],
    })
    driver = _patch_http(monkeypatch, [{}])
    rf._execute_stream(manifest, manifest.streams[0], {"client": ""})

    url, _ = driver.calls[0]
    assert "sap-client" not in url
    assert "keep=yes" in url


def test_default_headers_are_merged(monkeypatch):
    """netsuite-style: Prefer: transient on every stream."""
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://host",
        "auth": {"type": "bearer", "token_param": "access_token"},
        "default_headers": {"Prefer": "transient", "X-Tenant": "{tenant}"},
        "streams": [{"name": "s", "path": "/x", "data_path": "items",
                     "pagination": {"type": "none"}}],
    })
    driver = _patch_http(monkeypatch, [{"items": []}])
    rf._execute_stream(
        manifest, manifest.streams[0],
        {"access_token": "abc", "tenant": "acme"},
    )

    _url, headers = driver.calls[0]
    assert headers["Prefer"] == "transient"
    assert headers["X-Tenant"] == "acme"
    assert headers["Authorization"] == "Bearer abc"


def _oauth2_manifest():
    return rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://{tenant}.example.com",
        "auth": {"type": "oauth2", "token_url": "https://{tenant}.example.com/oauth/token"},
        "streams": [{"name": "s", "path": "/x", "pagination": {"type": "none"}}],
    })


def test_oauth2_dry_run_without_refresh_token_skips_gate(monkeypatch):
    """Regression (2026-06-16): `tools/test_connector.py --dry-run` builds an
    auth-header preview with NO credentials. _oauth2_refresh used to reach the
    SSRF gate on the templated token_url ('URL has no hostname') and crash
    EVERY oauth2 manifest's dry-run. With no refresh_token there's nothing to
    refresh, so it must return cleanly without touching the gate."""
    hits: list[str] = []
    monkeypatch.setattr(rf, "check_url", lambda url, **kw: hits.append(url))
    headers = rf._build_auth_headers(_oauth2_manifest(), {})
    assert headers["Authorization"] == "Bearer "
    assert hits == []  # gate never reached → no crash on the templated URL


def test_oauth2_with_refresh_token_still_hits_ssrf_gate(monkeypatch):
    """The dry-run guard must NOT weaken security: when a refresh_token IS
    present, the token_url still passes through check_url (which is outside the
    network try/except, so a block surfaces loudly)."""
    def _raise(url, **kw):
        raise RuntimeError(f"gate reached: {url}")
    monkeypatch.setattr(rf, "check_url", _raise)
    with pytest.raises(RuntimeError, match="gate reached"):
        rf._build_auth_headers(_oauth2_manifest(), {"refresh_token": "rt-123"})


def test_stream_headers_override_default_headers(monkeypatch):
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://host",
        "auth": {"type": "none"},
        "default_headers": {"X-Mode": "default"},
        "streams": [{"name": "s", "path": "/x", "data_path": "",
                     "headers": {"X-Mode": "stream"},
                     "pagination": {"type": "none"}}],
    })
    driver = _patch_http(monkeypatch, [{}])
    rf._execute_stream(manifest, manifest.streams[0], {})

    _url, headers = driver.calls[0]
    assert headers["X-Mode"] == "stream"


# ── pagination.type = "url" (T1) ──────────────────────────────────────────


def test_url_pagination_follows_absolute_next_link(monkeypatch):
    """OData v4 / Twilio style: response carries a full next URL."""
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://host",
        "auth": {"type": "none"},
        "streams": [{"name": "s", "path": "/x", "data_path": "value",
                     "pagination": {
                         "type": "url",
                         "next_url_path": "@odata.nextLink",
                         "max_pages": 5,
                     }}],
    })
    driver = _patch_http(monkeypatch, [
        {"value": [{"id": 1}],
         "@odata.nextLink": "https://host/x?$skiptoken=p2"},
        {"value": [{"id": 2}]},
    ])
    rows = rf._execute_stream(manifest, manifest.streams[0], {})

    assert rows == [{"id": 1}, {"id": 2}]
    assert driver.calls[0][0] == "https://host/x"
    assert driver.calls[1][0] == "https://host/x?$skiptoken=p2"


def test_url_pagination_joins_relative_next_link(monkeypatch):
    """sap_odata v2: ``d.__next`` may be a relative path."""
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://host/sap",
        "auth": {"type": "none"},
        "streams": [{"name": "s", "path": "/A_BP", "data_path": "d.results",
                     "pagination": {
                         "type": "url", "next_url_path": "d.__next",
                         "max_pages": 5,
                     }}],
    })
    driver = _patch_http(monkeypatch, [
        {"d": {"results": [{"id": 1}], "__next": "/sap/A_BP?$skip=200"}},
        {"d": {"results": [{"id": 2}]}},
    ])
    rows = rf._execute_stream(manifest, manifest.streams[0], {})

    assert len(rows) == 2
    assert driver.calls[1][0] == "https://host/sap/A_BP?$skip=200"


def test_url_pagination_stops_on_missing_next(monkeypatch):
    """No next URL → terminate without extra request."""
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://host",
        "auth": {"type": "none"},
        "streams": [{"name": "s", "path": "/x", "data_path": "value",
                     "pagination": {"type": "url",
                                    "next_url_path": "@odata.nextLink",
                                    "max_pages": 5}}],
    })
    driver = _patch_http(monkeypatch, [{"value": [{"id": 1}]}])
    rows = rf._execute_stream(manifest, manifest.streams[0], {})
    assert rows == [{"id": 1}]
    assert len(driver.calls) == 1


def test_url_pagination_misconfigured_terminates(monkeypatch):
    """type=url without next_url_path → return first page, no spin."""
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://host",
        "auth": {"type": "none"},
        "streams": [{"name": "s", "path": "/x", "data_path": "value",
                     "pagination": {"type": "url", "max_pages": 5}}],
    })
    driver = _patch_http(monkeypatch, [{"value": [{"id": 1}]}])
    rows = rf._execute_stream(manifest, manifest.streams[0], {})
    assert rows == [{"id": 1}]
    assert len(driver.calls) == 1


# ── 2026-06-01: verb + body + pagination-alias upgrade ────────────────────


def test_post_stream_forwards_method_and_json_body(monkeypatch):
    """POST stream with a JSON body — framework must thread both through.

    Pre-upgrade this silently sent a GET with no body, so the request
    never reached the vendor with the right shape (and most POST-only
    endpoints returned 404 or 405).
    """
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://api",
        "auth": {"type": "none"},
        "streams": [{
            "name": "submit",
            "path": "/v1/jobs",
            "method": "POST",
            "body": {"sql": "{sql}", "model": "{model}"},
            "data_path": "",
            "pagination": {"type": "none"},
        }],
    })
    driver = _patch_http(monkeypatch, [{"jobId": "abc"}])
    rf._execute_stream(manifest, manifest.streams[0],
                       {"sql": "SELECT 1", "model": "gpt-4"})
    req = driver.requests[0]
    assert req["method"] == "POST"
    assert req["body"] == {"sql": "SELECT 1", "model": "gpt-4"}
    assert req["body_text"] is None


def test_body_text_preserves_raw_payload(monkeypatch):
    """`body_text` is for ClickHouse-style raw SQL bodies (text/plain).

    Framework must not JSON-encode it and must not override the
    Content-Type the manifest already set.
    """
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://ch",
        "auth": {"type": "none"},
        "streams": [{
            "name": "q", "path": "/",
            "method": "POST",
            "body_text": "SELECT {n}",
            "headers": {"Content-Type": "text/plain"},
            "data_path": "",
            "pagination": {"type": "none"},
        }],
    })
    driver = _patch_http(monkeypatch, [{"ok": True}])
    rf._execute_stream(manifest, manifest.streams[0], {"n": "42"})
    req = driver.requests[0]
    assert req["method"] == "POST"
    assert req["body"] is None
    assert req["body_text"] == "SELECT 42"


def test_nested_body_deep_interpolation(monkeypatch):
    """`{prompt}` nested inside a JSON array must still substitute.

    The original (shallow) interpolator left these intact, so OpenAI
    chat_completions sent the literal string `{prompt}` to the API.
    """
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://api",
        "auth": {"type": "none"},
        "streams": [{
            "name": "chat", "path": "/v1/chat",
            "method": "POST",
            "body": {
                "model": "{model}",
                "messages": [{"role": "user", "content": "{prompt}"}],
            },
            "data_path": "",
            "pagination": {"type": "none"},
        }],
    })
    driver = _patch_http(monkeypatch, [{"id": "x"}])
    rf._execute_stream(manifest, manifest.streams[0],
                       {"model": "gpt-4", "prompt": "hello"})
    req = driver.requests[0]
    assert req["body"]["messages"][0]["content"] == "hello"
    assert req["body"]["model"] == "gpt-4"


def test_pagination_alias_page_token_routes_to_cursor(monkeypatch):
    """Microsoft Graph / Google Cloud style $skiptoken/nextPageToken.

    Manifests author this as `page_token` with `next_field` + `param`.
    Framework must normalize to its canonical `cursor` resolver — same
    semantics, different vocabulary.
    """
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://graph",
        "auth": {"type": "none"},
        "streams": [{
            "name": "items", "path": "/items",
            "data_path": "value",
            "pagination": {
                "type": "page_token",
                "param": "$skiptoken",
                "next_field": "@odata.nextLink",
                "max_pages": 3,
            },
        }],
    })
    driver = _patch_http(monkeypatch, [
        {"value": [{"id": 1}], "@odata.nextLink": "tok2"},
        {"value": [{"id": 2}], "@odata.nextLink": "tok3"},
        {"value": [{"id": 3}]},
    ])
    rows = rf._execute_stream(manifest, manifest.streams[0], {})
    assert [r["id"] for r in rows] == [1, 2, 3]
    # Page 2 must carry $skiptoken=tok2 in the query string.
    assert "%24skiptoken=tok2" in driver.calls[1][0] \
        or "$skiptoken=tok2" in driver.calls[1][0]


def test_pagination_alias_offset_routes_to_offset_limit(monkeypatch):
    """`offset` alias: param/count_param → offset_param/limit_param."""
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://api",
        "auth": {"type": "none"},
        "streams": [{
            "name": "rows", "path": "/r",
            "data_path": "",  # full payload is the list
            "pagination": {
                "type": "offset",
                "param": "skip",
                "count_param": "top",
                "page_size": 2,
                "max_pages": 3,
            },
        }],
    })
    driver = _patch_http(monkeypatch, [
        [{"id": 1}, {"id": 2}],
        [{"id": 3}, {"id": 4}],
        [{"id": 5}],
    ])
    rows = rf._execute_stream(manifest, manifest.streams[0], {})
    assert [r["id"] for r in rows] == [1, 2, 3, 4, 5]
    # Page 2 carries skip=2&top=2.
    assert "skip=2" in driver.calls[1][0]
    assert "top=2" in driver.calls[1][0]


def test_pagination_alias_page_routes_to_page_number(monkeypatch):
    """`page` alias: param/count_param → page_param/page_size_param."""
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://api",
        "auth": {"type": "none"},
        "streams": [{
            "name": "rows", "path": "/r",
            "data_path": "",
            "pagination": {
                "type": "page",
                "param": "page_num",
                "count_param": "size",
                "page_size": 2,
                "max_pages": 3,
            },
        }],
    })
    driver = _patch_http(monkeypatch, [
        [{"id": 1}, {"id": 2}],
        [{"id": 3}, {"id": 4}],
        [],
    ])
    rows = rf._execute_stream(manifest, manifest.streams[0], {})
    assert [r["id"] for r in rows] == [1, 2, 3, 4]
    # Page 2 carries page_num=2&size=2.
    assert "page_num=2" in driver.calls[1][0]
    assert "size=2" in driver.calls[1][0]


def test_default_method_is_get_when_unspecified(monkeypatch):
    """Manifests that omit `method` keep working as GET (back-compat)."""
    manifest = rf.RestConnectorManifest.from_dict({
        "id": "tcase", "name": "T", "base_url": "https://api",
        "auth": {"type": "none"},
        "streams": [{"name": "s", "path": "/x",
                     "data_path": "", "pagination": {"type": "none"}}],
    })
    driver = _patch_http(monkeypatch, [[{"id": 1}]])
    rf._execute_stream(manifest, manifest.streams[0], {})
    assert driver.requests[0]["method"] == "GET"
    assert driver.requests[0]["body"] is None


# ── Shipped manifests use these fields (regression pin) ───────────────────


def test_sap_odata_manifest_loads_with_default_query():
    """Reload the actual sap_odata manifest and confirm default_query
    survived parsing — this is the regression that prompted T1."""
    rf._MANIFEST_CACHE.clear()
    manifests = rf.load_manifests(force=True)
    sap = manifests.get("sap_odata")
    assert sap is not None
    assert sap.default_query.get("$format") == "json"
    assert sap.default_headers.get("Accept") == "application/json"


def test_dynamics365_manifest_uses_url_pagination():
    rf._MANIFEST_CACHE.clear()
    manifests = rf.load_manifests(force=True)
    dyn = manifests.get("dynamics365")
    assert dyn is not None
    assert dyn.streams, "expected at least one stream"
    page = dyn.streams[0].get("pagination") or {}
    # Either url (post-T1 manifest authors) or cursor with next_url_path
    # (pre-T1 manifest authors) — both must paginate correctly under T1.
    assert page.get("type") in {"url", "cursor"}
    assert page.get("next_url_path") == "@odata.nextLink"
