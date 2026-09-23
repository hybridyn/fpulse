from __future__ import annotations

from fpulse.connections.catalog import get_catalog
from fpulse.connections import catalog_extensions


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


def test_airbyte_catalog_requires_base_url():
    catalog = get_catalog("airbyte", {})

    assert catalog.supported is False
    assert "base_url" in catalog.reason
    assert catalog.category == "integration_metadata"


def test_airbyte_catalog_lists_sources_and_connections(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_rpost(url, *, headers=None, json=None, **_kwargs):
        calls.append((url, json or {}))
        if url.endswith("/sources/list"):
            return _FakeResponse(200, {
                "sources": [
                    {"name": "Stripe production"},
                    {"name": "Salesforce sandbox"},
                ]
            })
        if url.endswith("/connections/list"):
            return _FakeResponse(200, {
                "connections": [
                    {"name": "Stripe to Snowflake"},
                ]
            })
        return _FakeResponse(404, {})

    monkeypatch.setattr(catalog_extensions, "_rpost", fake_rpost)

    catalog = get_catalog("airbyte", {
        "base_url": "http://airbyte.local:8001/",
        "workspace_id": "workspace-123",
        "api_key": "test-token",
    })

    assert catalog.supported is True
    assert catalog.category == "integration_metadata"
    assert catalog.kinds == ["connection", "source"]
    assert [item.name for item in catalog.items] == [
        "Stripe production",
        "Salesforce sandbox",
        "Stripe to Snowflake",
    ]
    assert [item.kind for item in catalog.items] == [
        "source",
        "source",
        "connection",
    ]
    assert calls == [
        ("http://airbyte.local:8001/api/v1/sources/list", {"workspaceId": "workspace-123"}),
        ("http://airbyte.local:8001/api/v1/connections/list", {"workspaceId": "workspace-123"}),
    ]


def test_airbyte_catalog_ignores_non_200_resource(monkeypatch):
    def fake_rpost(url, *, headers=None, json=None, **_kwargs):
        if url.endswith("/sources/list"):
            return _FakeResponse(500, {"message": "nope"})
        return _FakeResponse(200, {"connections": [{"name": "Only connection"}]})

    monkeypatch.setattr(catalog_extensions, "_rpost", fake_rpost)

    catalog = get_catalog("airbyte", {
        "base_url": "http://airbyte.local:8001",
        "workspace_id": "workspace-123",
    })

    assert catalog.supported is True
    assert [(item.kind, item.name) for item in catalog.items] == [
        ("connection", "Only connection"),
    ]


def test_airbyte_catalog_reports_request_exception(monkeypatch):
    def fake_rpost(*_args, **_kwargs):
        raise TimeoutError("boom")

    monkeypatch.setattr(catalog_extensions, "_rpost", fake_rpost)

    catalog = get_catalog("airbyte", {
        "base_url": "http://airbyte.local:8001",
        "workspace_id": "workspace-123",
    })

    assert catalog.supported is False
    assert "API request failed" in catalog.reason
