"""OpenAPI/Swagger -> connector manifest generator.

Pins that a pasted spec yields a draft manifest compatible with the
rest_framework runtime (id / base_url / auth shape / GET streams), for both
OpenAPI 3 and Swagger 2, with each supported security scheme mapping to the
exact auth block _build_auth_headers expects.
"""

from __future__ import annotations

from fpulse.connectors.openapi_import import manifest_from_openapi


def _openapi3(security_schemes):
    return {
        "openapi": "3.0.0",
        "info": {"title": "Acme API", "description": "Acme things"},
        "servers": [{"url": "https://api.acme.com/v1"}],
        "components": {"securitySchemes": security_schemes},
        "paths": {
            "/customers": {"get": {"operationId": "listCustomers", "summary": "List customers"}},
            "/orders": {"get": {"summary": "List orders"}},
            "/orders/{id}": {"post": {"summary": "create"}},  # non-GET → skipped
        },
    }


def test_openapi3_bearer_basic_shape():
    m = manifest_from_openapi(_openapi3({"BearerAuth": {"type": "http", "scheme": "bearer"}}))
    assert m["id"] == "acme_api"
    assert m["name"] == "Acme API"
    assert m["base_url"] == "https://api.acme.com/v1"
    assert m["auth"]["type"] == "bearer"
    assert m["auth"]["header_template"] == "Bearer {token}"
    assert any(p["name"] == "access_token" and p["secret"] for p in m["params"])
    names = {s["name"] for s in m["streams"]}
    assert names == {"listcustomers", "orders"}  # only GETs, slugged
    assert all(s["method"] == "GET" for s in m["streams"])
    assert m["tier"] == "generated"


def test_openapi3_apikey_header():
    m = manifest_from_openapi(_openapi3({"ApiKey": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}))
    assert m["auth"]["type"] == "api_key"
    assert m["auth"]["header_name"] == "X-Api-Key"


def test_openapi3_apikey_query():
    m = manifest_from_openapi(_openapi3({"ApiKey": {"type": "apiKey", "in": "query", "name": "apikey"}}))
    assert m["auth"]["type"] == "api_key"
    assert m["auth"]["query_param"] == "apikey"


def test_openapi3_oauth2_token_url():
    m = manifest_from_openapi(_openapi3({
        "OAuth": {"type": "oauth2", "flows": {"clientCredentials": {"tokenUrl": "https://api.acme.com/oauth/token"}}},
    }))
    assert m["auth"]["type"] == "oauth2"
    assert m["auth"]["token_url"] == "https://api.acme.com/oauth/token"


def test_swagger2_host_basepath_and_basic():
    spec = {
        "swagger": "2.0",
        "info": {"title": "Legacy API"},
        "host": "legacy.example.com",
        "basePath": "/api",
        "schemes": ["https"],
        "securityDefinitions": {"basic": {"type": "basic"}},
        "paths": {"/things": {"get": {"summary": "things"}}},
    }
    m = manifest_from_openapi(spec)
    assert m["base_url"] == "https://legacy.example.com/api"
    assert m["auth"]["type"] == "basic"
    assert {p["name"] for p in m["params"]} == {"username", "password"}
    assert [s["path"] for s in m["streams"]] == ["/things"]


def test_no_security_defaults_to_bearer():
    spec = {"openapi": "3.0.0", "info": {"title": "X"}, "servers": [{"url": "https://x.io"}], "paths": {}}
    m = manifest_from_openapi(spec)
    assert m["auth"]["type"] == "bearer"
    assert m["streams"] == []


def test_connector_id_override_and_bad_spec():
    m = manifest_from_openapi({"info": {"title": "Y"}, "paths": {}}, connector_id="My Custom ID")
    assert m["id"] == "my_custom_id"
    import pytest
    with pytest.raises(ValueError):
        manifest_from_openapi("not a dict")
