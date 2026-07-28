"""Tests for the paste/upload OpenAPI path (no public URL required).

Covers the shared spec parser, the authoring API's spec resolver precedence
(spec > text > url), and the Copilot draft tool accepting openapi_text.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import fpulse.connectors.drafts as drafts_mod
from fpulse.ai.tools.base import ToolContext
from fpulse.api.connector_authoring import FromOpenApiRequest, _resolve_spec
from fpulse.connectors.ai_authoring import parse_spec_text
from fpulse.connectors.drafts import DraftConnectorStore

SPEC_JSON = (
    '{"openapi":"3.0.0","info":{"title":"FactoHR"},'
    '"paths":{"/employees":{"get":{"operationId":"listEmployees",'
    '"responses":{"200":{"description":"ok"}}}}}}'
)

SPEC_YAML = """\
openapi: 3.0.0
info:
  title: FactoHR
paths:
  /employees:
    get:
      operationId: listEmployees
      responses:
        '200':
          description: ok
"""


def _ctx() -> ToolContext:
    return ToolContext(tenant_id="t", user_id="u@example.com",
                       workspace_id="default", environment="dev")


@pytest.fixture
def draft_store(tmp_path, monkeypatch):
    store = DraftConnectorStore(tmp_path / "drafts")
    monkeypatch.setattr(drafts_mod, "_STORE", store)
    return store


# ── parse_spec_text ───────────────────────────────────────────────────

def test_parse_spec_text_json():
    spec = parse_spec_text(SPEC_JSON)
    assert isinstance(spec, dict) and "/employees" in spec["paths"]


def test_parse_spec_text_yaml():
    spec = parse_spec_text(SPEC_YAML)
    assert isinstance(spec, dict) and "/employees" in spec["paths"]


@pytest.mark.parametrize("bad", ["", "   ", "just a sentence, not a spec: ["])
def test_parse_spec_text_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_spec_text(bad)


def test_parse_spec_text_rejects_non_object():
    # Valid JSON but a list, not an OpenAPI object.
    with pytest.raises(ValueError):
        parse_spec_text("[1, 2, 3]")


def test_parse_spec_text_size_cap():
    huge = "x" * (2 * 1024 * 1024 + 10)
    with pytest.raises(ValueError):
        parse_spec_text(huge)


# ── _resolve_spec precedence ──────────────────────────────────────────

def test_resolve_spec_from_text_json():
    req = FromOpenApiRequest(connector_id="factohr", openapi_text=SPEC_JSON)
    spec = asyncio.run(_resolve_spec(req))
    assert "/employees" in spec["paths"]


def test_resolve_spec_from_text_yaml():
    req = FromOpenApiRequest(connector_id="factohr", openapi_text=SPEC_YAML)
    spec = asyncio.run(_resolve_spec(req))
    assert "/employees" in spec["paths"]


def test_resolve_spec_prefers_parsed_dict_over_text():
    parsed = {"paths": {"/from_dict": {"get": {}}}}
    req = FromOpenApiRequest(connector_id="c", openapi_spec=parsed, openapi_text=SPEC_JSON)
    spec = asyncio.run(_resolve_spec(req))
    assert "/from_dict" in spec["paths"]


def test_resolve_spec_none_provided_400():
    req = FromOpenApiRequest(connector_id="c")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_resolve_spec(req))
    assert ei.value.status_code == 400


def test_resolve_spec_bad_text_400():
    req = FromOpenApiRequest(connector_id="c", openapi_text="not a spec: [")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_resolve_spec(req))
    assert ei.value.status_code == 400


def test_resolve_spec_missing_paths_400():
    req = FromOpenApiRequest(connector_id="c", openapi_text='{"openapi":"3.0.0"}')
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_resolve_spec(req))
    assert ei.value.status_code == 400


# ── Copilot tool accepts openapi_text ─────────────────────────────────

def test_draft_tool_from_openapi_text(draft_store):
    from fpulse.ai.tools.draft_connector_from_openapi import _handler

    out = asyncio.run(_handler(
        {
            "connector_id": "factohr", "display_name": "FactoHR",
            "openapi_text": SPEC_YAML,
            "idempotency_key": "safe_write.u.draft_connector.factohr.1.0.0",
        },
        _ctx(),
    ))
    assert out["runnable"] is True
    assert out["stream_count"] >= 1
    assert draft_store.get(out["draft_id"]) is not None


def test_draft_tool_bad_text_raises(draft_store):
    from fpulse.ai.tools.draft_connector_from_openapi import _handler

    with pytest.raises(ValueError):
        asyncio.run(_handler(
            {
                "connector_id": "factohr",
                "openapi_text": "nonsense: [",
                "idempotency_key": "safe_write.u.draft_connector.factohr.1.0.0",
            },
            _ctx(),
        ))
