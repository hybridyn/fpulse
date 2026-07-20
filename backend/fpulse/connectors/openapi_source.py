"""
Generic OpenAPI / Swagger source.

Paste an OpenAPI spec URL (or inline JSON), pick a path + method, and the node
runs the request and returns the JSON response as rows. Auth and pagination
are configured per-call. This is the "any-API" escape hatch when no manifest
exists yet.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.parse
import urllib.request
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for the execute() return-type annotation.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register
from fpulse.connectors.rest_framework import (
    RestConnectorManifest,
    _execute_stream,
    _rows_to_relation,
)


def _fetch_spec(url: str) -> dict:
    if url.startswith("http"):
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = resp.read().decode("utf-8")
    else:
        with open(url, "r", encoding="utf-8") as f:
            data = f.read()
    if data.lstrip().startswith("{"):
        return json.loads(data)
    try:
        import yaml
        return yaml.safe_load(data)
    except ImportError as e:
        raise RuntimeError("OpenAPI spec is YAML — install PyYAML: pip install pyyaml") from e


def _discover_endpoints(spec: dict) -> list[dict]:
    """Flatten OpenAPI 3.x paths into a list of (method, path, operation_id, summary)."""
    out: list[dict] = []
    paths = spec.get("paths", {})
    for path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for method in ("get", "post", "put", "delete", "patch"):
            op = ops.get(method)
            if not op:
                continue
            out.append({
                "method": method.upper(),
                "path": path,
                "operation_id": op.get("operationId", f"{method}_{path}"),
                "summary": op.get("summary", ""),
            })
    return out


@register(StepType.OPENAPI_SOURCE)
class OpenApiSourceNode(BaseNode):
    """Drive any OpenAPI-described API. Paste a spec URL → pick endpoint → fetch."""

    display_name = "OpenAPI Source"
    category = "source"
    description = "Generic source for any OpenAPI/Swagger-described REST API"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        spec_url = self.params.get("spec_url")
        if not spec_url:
            raise ValueError("OpenAPI Source: spec_url is required")
        spec = _fetch_spec(spec_url)
        servers = spec.get("servers") or [{"url": ""}]
        base_url = self.params.get("base_url") or servers[0].get("url", "")

        method = (self.params.get("method") or "GET").upper()
        path = self.params.get("path")
        if not path:
            endpoints = _discover_endpoints(spec)
            if not endpoints:
                raise ValueError("OpenAPI spec has no usable endpoints")
            path = endpoints[0]["path"]
            method = endpoints[0]["method"]

        # Build a synthetic manifest so we reuse rest_framework's auth/pagination machinery.
        auth_type = self.params.get("auth_type") or "none"
        manifest = RestConnectorManifest(
            id="__openapi_inline__",
            name="OpenAPI",
            base_url=base_url,
            auth={
                "type": auth_type,
                "token_param": "token",
                "key_param": "api_key",
                "header_name": self.params.get("auth_header", "Authorization"),
                "header_template": self.params.get("auth_template", "Bearer {token}"),
            },
        )
        stream = {
            "name": "endpoint",
            "path": path,
            "method": method,
            "query": self._parse_kv(self.params.get("query") or ""),
            "headers": self._parse_kv(self.params.get("headers") or ""),
            "data_path": self.params.get("data_path", ""),
            "pagination": {"type": self.params.get("pagination", "none"),
                           "page_size": int(self.params.get("page_size", 100)),
                           "max_pages": int(self.params.get("max_pages", 20))},
        }
        env = {
            "token": self.params.get("token", ""),
            "api_key": self.params.get("api_key", ""),
        }
        rows = _execute_stream(manifest, stream, env)
        return _rows_to_relation(ctx.conn, rows)

    @staticmethod
    def _parse_kv(text: str) -> dict[str, str]:
        """Parse 'a=1, b=2' or 'a=1\\nb=2' into a dict."""
        if not text:
            return {}
        out: dict[str, str] = {}
        for chunk in text.replace("\n", ",").split(","):
            chunk = chunk.strip()
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"spec_url": "", "method": "GET", "pagination": "none", "page_size": 100, "max_pages": 20}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "spec_url", "type": "string", "label": "OpenAPI Spec URL", "required": True},
            {"name": "base_url", "type": "string", "label": "Base URL (override)"},
            {"name": "method", "type": "string", "label": "Method",
             "options": [{"value": m, "label": m} for m in ["GET", "POST", "PUT", "DELETE", "PATCH"]]},
            {"name": "path", "type": "string", "label": "Endpoint path", "required": True},
            {"name": "query", "type": "string", "label": "Query params (k=v, ...)"},
            {"name": "headers", "type": "string", "label": "Headers (k=v, ...)"},
            {"name": "data_path", "type": "string", "label": "Response data path (dot-notation)"},
            {"name": "auth_type", "type": "string", "label": "Auth",
             "options": [{"value": x, "label": x} for x in ["none", "bearer", "api_key", "basic"]]},
            {"name": "token", "type": "string", "label": "Token", "secret": True},
            {"name": "pagination", "type": "string", "label": "Pagination",
             "options": [{"value": x, "label": x} for x in ["none", "page_number", "offset_limit", "cursor", "link_header"]]},
            {"name": "max_pages", "type": "number", "label": "Max pages", "default": 20},
        ]
