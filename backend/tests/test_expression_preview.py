"""Live ``{{ }}`` expression preview endpoint (C4, 2026-06-15).

The endpoint reuses the runtime resolver (fpulse.expression), so these also
serve as a contract test that the in-editor preview matches execution.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fpulse.api import expressions_router
from fpulse.auth.deps import require_auth


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(expressions_router)

    class _User:
        id = "t"
        email = "t@fpulse.local"
        role = "super_admin"
        is_active = True

    app.dependency_overrides[require_auth] = lambda: _User()
    return TestClient(app)


def _preview(client: TestClient, **body) -> dict:
    r = client.post("/api/expression/preview", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_literal_passthrough(client):
    out = _preview(client, expression="hello")
    assert out["ok"] and out["result"] == "hello"


def test_json_field(client):
    out = _preview(client, expression="{{ $json.name }}", sample_row={"name": "Ada"})
    assert out["ok"] and out["result"] == "Ada"


def test_string_interpolation(client):
    out = _preview(client, expression="Hi {{ $json.name }}!", sample_row={"name": "Ada"})
    assert out["ok"] and out["result"] == "Hi Ada!"


def test_vars_preserves_type(client):
    out = _preview(client, expression="{{ $vars.threshold }}", vars={"threshold": 42})
    assert out["ok"] and out["result"] == "42" and out["value_type"] == "int"


def test_now_helper(client):
    out = _preview(client, expression="{{ $now.startOf('year') }}")
    assert out["ok"] and out["value_type"] == "datetime"


def test_node_ref(client):
    out = _preview(
        client,
        expression="{{ $('Source').first().id }}",
        node_samples={"Source": [{"id": 7}]},
    )
    assert out["ok"] and out["result"] == "7"


def test_item_index(client):
    out = _preview(client, expression="{{ $itemIndex }}", item_index=3)
    assert out["ok"] and out["result"] == "3"


def test_bad_expression_returns_error_not_500(client):
    out = _preview(client, expression="{{ $json.missing }}", sample_row={})
    assert out["ok"] is False and out["error"]


def test_syntax_error_returns_error(client):
    out = _preview(client, expression="{{ $json.name + }}", sample_row={"name": "x"})
    assert out["ok"] is False and out["error"]
