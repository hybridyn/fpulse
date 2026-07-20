"""Tests for the X1/X2 adapter modules (2026-05-23).

The adapters wrap rest_framework with a leaner config-driven entry
point. These tests pin the v2/v4 split for OData and exercise each
pagination type through the REST adapter so a future refactor can't
quietly break a connector that drives through the adapter.
"""

from __future__ import annotations

from typing import Any

import pytest

from fpulse.connectors import rest_framework as rf
from fpulse.connectors.adapters import odata, rest as rest_adapter


# ── Shared HTTP capture ───────────────────────────────────────────────────


class _Driver:
    """Captures every HTTP request the framework dispatches.

    Signature widened 2026-06-02 to mirror the framework's
    `_http_request` upgrade (verb + body support). Existing tests
    that only unpack `(url, headers)` from `driver.calls[i]` keep
    working unchanged because the call shape is preserved; new
    method/body assertions can read `driver.requests[i]` instead.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        # Back-compat tuple shape — every assertion in this file
        # unpacks `url, headers = driver.calls[i]` and we don't want
        # to rewrite each one.
        self.calls = []
        # Full request record (method/body too) for new tests.
        self.requests = []

    def __call__(self, url, headers, method="GET", body=None, body_text=None):
        self.calls.append((url, dict(headers)))
        self.requests.append({
            "url": url,
            "headers": dict(headers),
            "method": method,
            "body": body,
            "body_text": body_text,
        })
        if not self.responses:
            raise AssertionError(f"Unexpected request to {url}")
        item = self.responses.pop(0)
        if isinstance(item, tuple):
            body, resp_headers = item
        else:
            body, resp_headers = item, {}
        return body, resp_headers


def _patch_http(monkeypatch, responses):
    driver = _Driver(responses)
    # 2026-06-02 framework upgrade: the executor now calls
    # `_http_request` instead of `_http_get`. Patching the new
    # boundary is what actually intercepts the call. `_http_get`
    # remains as a back-compat alias for any external caller, but
    # tests targeting the executor must patch the real path.
    monkeypatch.setattr(rf, "_http_request", driver)
    return driver


# ── OData adapter (X1) ───────────────────────────────────────────────────


def test_odata_v2_uses_d_results_and_d_next(monkeypatch):
    driver = _patch_http(monkeypatch, [
        {"d": {"results": [{"id": 1}], "__next": "https://host/svc/E?$skiptoken=2"}},
        {"d": {"results": [{"id": 2}]}},
    ])
    rows = odata.run_odata_stream(
        base_url="https://host/svc", entity_set="E", version="v2",
        auth=("basic", "u", "p"),
    )
    assert rows == [{"id": 1}, {"id": 2}]
    # default_query should set $format=json on the first call.
    assert "%24format=json" in driver.calls[0][0] or "$format=json" in driver.calls[0][0]
    # second call follows the absolute __next.
    assert driver.calls[1][0] == "https://host/svc/E?$skiptoken=2"


def test_odata_v4_uses_value_and_odata_nextlink(monkeypatch):
    driver = _patch_http(monkeypatch, [
        {"value": [{"id": 1}], "@odata.nextLink": "https://host/svc/E?$skiptoken=p2"},
        {"value": [{"id": 2}]},
    ])
    rows = odata.run_odata_stream(
        base_url="https://host/svc", entity_set="E", version="v4",
        auth=("bearer", "tok"),
    )
    assert rows == [{"id": 1}, {"id": 2}]
    # v4 should NOT inject $format=json.
    assert "$format=json" not in driver.calls[0][0]
    # bearer auth should populate the Authorization header.
    assert driver.calls[0][1].get("Authorization") == "Bearer tok"


def test_odata_v2_strips_empty_sap_client(monkeypatch):
    driver = _patch_http(monkeypatch, [{"d": {"results": []}}])
    odata.run_odata_stream(
        base_url="https://host/svc", entity_set="E", version="v2",
        auth=None, sap_client=None,
    )
    url, _ = driver.calls[0]
    assert "sap-client" not in url


def test_odata_top_and_filter_propagate(monkeypatch):
    driver = _patch_http(monkeypatch, [{"value": []}])
    odata.run_odata_stream(
        base_url="https://host/svc", entity_set="Users",
        version="v4", filter_query="active eq true",
        select_fields="id,name", top=50,
    )
    url, _ = driver.calls[0]
    assert "%24top=50" in url
    assert "%24select=id%2Cname" in url
    assert "active+eq+true" in url or "active%20eq%20true" in url


def test_odata_unknown_version_raises():
    with pytest.raises(ValueError, match="unknown OData version"):
        odata.run_odata_stream(
            base_url="https://host", entity_set="E", version="v3",
        )


# ── REST adapter (X2) ────────────────────────────────────────────────────


def test_rest_adapter_offset_limit_paginates(monkeypatch):
    driver = _patch_http(monkeypatch, [
        {"data": [{"i": 1}, {"i": 2}]},
        {"data": [{"i": 3}, {"i": 4}]},
        {"data": [{"i": 5}]},                # partial page → stop
    ])
    rows = rest_adapter.run_rest_stream(
        base_url="https://host", path="/x", data_path="data",
        pagination={
            "type": "offset_limit", "offset_param": "offset",
            "limit_param": "limit", "page_size": 2, "max_pages": 5,
        },
    )
    assert [r["i"] for r in rows] == [1, 2, 3, 4, 5]


def test_rest_adapter_url_pagination(monkeypatch):
    driver = _patch_http(monkeypatch, [
        {"items": [{"a": 1}], "next": "https://host/x?cursor=p2"},
        {"items": [{"a": 2}]},
    ])
    rows = rest_adapter.run_rest_stream(
        base_url="https://host", path="/x", data_path="items",
        pagination={"type": "url", "next_url_path": "next", "max_pages": 5},
    )
    assert [r["a"] for r in rows] == [1, 2]
    assert driver.calls[1][0] == "https://host/x?cursor=p2"


def test_rest_adapter_api_key_header(monkeypatch):
    driver = _patch_http(monkeypatch, [{"items": []}])
    rest_adapter.run_rest_stream(
        base_url="https://host", path="/x",
        auth=("api_key", "X-My-Key", "abc123"),
        data_path="items",
    )
    _url, headers = driver.calls[0]
    assert headers.get("X-My-Key") == "abc123"


def test_rest_adapter_none_pagination_single_page(monkeypatch):
    driver = _patch_http(monkeypatch, [{"data": [{"id": 1}, {"id": 2}]}])
    rows = rest_adapter.run_rest_stream(
        base_url="https://host", path="/x", data_path="data",
    )
    assert len(rows) == 2
    assert len(driver.calls) == 1


def test_rest_adapter_missing_base_url_raises():
    with pytest.raises(ValueError, match="base_url"):
        rest_adapter.run_rest_stream(base_url="", path="/x")


def test_rest_adapter_missing_path_raises():
    with pytest.raises(ValueError, match="path"):
        rest_adapter.run_rest_stream(base_url="https://host", path="")
