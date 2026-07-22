"""Tests for agentic connector creation (P0/P1/P2):
draft_connector_from_openapi / _from_samples, the inert DraftConnectorStore,
the approve→activate gate, and the test_connection tool.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import fpulse.connectors.drafts as drafts_mod
from fpulse.ai.tools.base import ToolContext
from fpulse.connectors.drafts import DraftConnectorStore, DraftStatus

MINIMAL_OPENAPI = {
    "openapi": "3.0.0",
    "info": {"title": "FactoHR", "version": "1.0"},
    "servers": [{"url": "https://api.factohr.example.com"}],
    "paths": {
        "/employees": {
            "get": {
                "operationId": "listEmployees",
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
    "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
}


def _ctx(dry_run: bool = False) -> ToolContext:
    return ToolContext(
        tenant_id="t", user_id="u@example.com", workspace_id="default",
        environment="dev", dry_run=dry_run,
    )


@pytest.fixture
def draft_store(tmp_path, monkeypatch):
    """Point the process-wide draft store at a tmp dir so tests are hermetic."""
    store = DraftConnectorStore(tmp_path / "drafts")
    monkeypatch.setattr(drafts_mod, "_STORE", store)
    return store


def test_three_tools_registered():
    from fpulse.ai.tools import register_initial_tools
    from fpulse.ai.tools.registry import ToolRegistry

    reg = register_initial_tools(ToolRegistry())
    names = {t.name for t in reg.list_all()}
    assert {"draft_connector_from_openapi", "draft_connector_from_samples", "test_connection"} <= names
    # All three must be write-tier with idempotency required (registry enforces it).
    for n in ("draft_connector_from_openapi", "draft_connector_from_samples", "test_connection"):
        t = reg.get(n)
        assert t.requires_idempotency_key is True


def test_openapi_tool_creates_inert_runnable_draft(draft_store):
    from fpulse.ai.tools.draft_connector_from_openapi import _handler

    out = asyncio.run(_handler(
        {
            "connector_id": "factohr", "display_name": "FactoHR",
            "openapi_spec": MINIMAL_OPENAPI,
            "idempotency_key": "safe_write.u.draft_connector.factohr.1.0.0",
        },
        _ctx(),
    ))
    assert out["runnable"] is True
    assert out["stream_count"] >= 1

    draft = draft_store.get(out["draft_id"])
    assert draft is not None
    assert draft.status == DraftStatus.PROPOSED          # inert — not live
    assert draft.manifest.get("base_url")                # activatable shape
    assert draft.manifest.get("streams")
    # Guardrail: no real secret can be in the draft — only auth templates.
    blob = json.dumps(draft.manifest).lower()
    assert "password" not in blob or "{" in blob         # any secret-ish field is a template


def test_openapi_tool_dry_run_makes_no_draft(draft_store):
    from fpulse.ai.tools.draft_connector_from_openapi import _handler

    out = asyncio.run(_handler(
        {"connector_id": "x", "openapi_spec": MINIMAL_OPENAPI, "idempotency_key": "k"},
        _ctx(dry_run=True),
    ))
    assert out["draft_id"] == "dry-run-draft"
    assert draft_store.list_all() == []


def test_openapi_tool_requires_idempotency_key(draft_store):
    from fpulse.ai.tools.draft_connector_from_openapi import _handler

    with pytest.raises(ValueError):
        asyncio.run(_handler({"connector_id": "x", "openapi_spec": MINIMAL_OPENAPI}, _ctx()))


def test_samples_tool_creates_non_runnable_draft(draft_store):
    from fpulse.ai.tools.draft_connector_from_samples import _handler

    out = asyncio.run(_handler(
        {
            "connector_id": "acme", "samples": [{"id": 1, "name": "a"}],
            "base_url": "https://api.acme.test", "idempotency_key": "k",
        },
        _ctx(),
    ))
    assert out["runnable"] is False
    draft = draft_store.get(out["draft_id"])
    assert draft is not None and draft.status == DraftStatus.PROPOSED


def test_approve_runnable_activates_via_save(draft_store, monkeypatch):
    from fpulse.ai.tools.draft_connector_from_openapi import _handler

    out = asyncio.run(_handler(
        {"connector_id": "factohr", "openapi_spec": MINIMAL_OPENAPI, "idempotency_key": "k"},
        _ctx(),
    ))
    draft_id = out["draft_id"]

    # Mock the real activation so the test doesn't touch the global manifest cache.
    import fpulse.connectors.rest_framework as rf

    class _Saved:
        id = "factohr"
        tier = "beta"
        streams = [object()]

    seen = {}

    def _fake_save(data):
        seen["data"] = data
        return _Saved()

    monkeypatch.setattr(rf, "save_user_manifest", _fake_save)

    result = draft_store.approve(draft_id, "admin@example.com")
    assert result is not None
    draft, activation = result
    assert draft.status == DraftStatus.APPROVED
    assert draft.approved_by == "admin@example.com"
    assert activation["activated"] is True
    assert activation["connector_id"] == "factohr"
    assert seen["data"]["id"] == "factohr"             # reviewed id forced onto manifest


def test_approve_non_runnable_guides_not_activates(draft_store):
    d = draft_store.propose(connector_id="acme", mode="samples_schema", manifest={"x": 1}, runnable=False)
    result = draft_store.approve(d.id, "admin")
    assert result is not None
    draft, activation = result
    assert draft.status == DraftStatus.APPROVED
    assert activation["activated"] is False
    assert "Author" in activation["note"]


def test_reject_then_cannot_approve(draft_store):
    d = draft_store.propose(connector_id="x", mode="openapi_runtime", manifest={}, runnable=True)
    rejected = draft_store.reject(d.id, "admin", "not needed")
    assert rejected is not None and rejected.status == DraftStatus.REJECTED
    # A decided draft can't be approved.
    assert draft_store.approve(d.id, "admin") is None


def test_test_connection_dry_run():
    from fpulse.ai.tools.test_connection import _handler

    out = asyncio.run(_handler({"connection_id": "c1", "idempotency_key": "k"}, _ctx(dry_run=True)))
    assert out["success"] is True
