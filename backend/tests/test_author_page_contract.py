"""Author page contract: review and save the same runtime definition."""
import asyncio
import json

import pytest
from fastapi import HTTPException

from fpulse.api import connector_authoring as api
from fpulse.connectors import rest_framework as rf

SPEC = {
    "openapi": "3.0.0", "info": {"title": "Disposable API", "version": "1"},
    "servers": [{"url": "https://example.com"}],
    "components": {"securitySchemes": {"Key": {"type": "apiKey", "in": "header", "name": "X-Key"}}},
    "security": [{"Key": []}],
    "paths": {"/records": {"get": {"operationId": "records", "responses": {"200": {"description": "OK"}}}}},
}


@pytest.mark.parametrize("source", ["openapi_spec", "openapi_text", "openapi_url"])
def test_generate_review_save_reload(source, tmp_path, monkeypatch):
    fetches = []
    def fetch(url):
        fetches.append(url)
        return SPEC
    monkeypatch.setattr(api, "fetch_openapi_spec", fetch)
    monkeypatch.setattr(rf, "_MANIFEST_DIR", str(tmp_path / "packaged"))
    monkeypatch.setattr(rf, "_USER_MANIFEST_DIR", str(tmp_path / "user"))
    monkeypatch.setattr(rf, "_MANIFEST_CACHE", {})
    monkeypatch.setattr(rf, "_USER_MANIFEST_IDS", set())
    value = SPEC if source == "openapi_spec" else json.dumps(SPEC) if source == "openapi_text" else "https://example.com/spec"
    result = asyncio.run(api.author_from_openapi(api.FromOpenApiRequest(
        connector_id="disposable_author", display_name="Reviewed API", **{source: value})))
    reviewed = result.runtime_manifest
    assert reviewed["name"] == "Reviewed API"
    assert reviewed["streams"][0]["auth"]["alternatives"][0]["supported"]
    saved = asyncio.run(api.save_connector(api.SaveManifestRequest(manifest=reviewed)))
    assert saved["saved"]
    reloaded = rf.load_manifests(force=True)["disposable_author"]
    assert reloaded.streams == reviewed["streams"]
    assert reloaded.params == reviewed["params"]
    assert reloaded.tier == "beta"
    assert len(fetches) == (1 if source == "openapi_url" else 0)


def test_yaml_source_and_invalid_source():
    valid = "openapi: 3.0.0\ninfo:\n  title: Test\npaths:\n  /records:\n    get: {}\n"
    resolved = asyncio.run(api._resolve_spec(api.FromOpenApiRequest(connector_id="test", openapi_text=valid)))
    assert "/records" in resolved["paths"]
    with pytest.raises(HTTPException) as error:
        asyncio.run(api._resolve_spec(api.FromOpenApiRequest(connector_id="test", openapi_text="[1,2]")))
    assert error.value.status_code == 400


def test_text_size_limit():
    from fpulse.connectors.ai_authoring import parse_spec_text
    with pytest.raises(ValueError, match="exceeds"):
        parse_spec_text("x" * (2 * 1024 * 1024 + 1))
