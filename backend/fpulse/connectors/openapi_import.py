"""OpenAPI / Swagger -> F-Pulse REST connector manifest (DRAFT).

Turns an OpenAPI 3 or Swagger 2 spec into a draft manifest for the
manifest-driven REST framework (``rest_framework.py``), so the long tail of
REST APIs becomes a paste-a-spec operation instead of a hand-written
connector. This is the scaling lesson: a connector FACTORY, not hand-written
connectors.

The output is a DRAFT — the auth params, stream `data_path`, and pagination
usually need a human pass + a live test before it ships. Pure function, no
network: the caller fetches the spec (SSRF-guarded) and passes the parsed
dict here. The emitted auth blocks match ``_build_auth_headers`` exactly.
"""
from __future__ import annotations

import re
from typing import Any


def _slug(s: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", (s or "").strip().lower()).strip("_")
    return out or "api"


def _auth_from_spec(spec: dict) -> tuple[dict, list[dict]]:
    """Map the first declared security scheme to (auth_block, params) in the
    shape rest_framework._build_auth_headers understands. Defaults to bearer."""
    schemes = ((spec.get("components") or {}).get("securitySchemes")) or {}
    if not schemes:  # Swagger 2
        schemes = spec.get("securityDefinitions") or {}

    bearer = (
        {"type": "bearer", "token_param": "access_token",
         "header_name": "Authorization", "header_template": "Bearer {token}"},
        [{"name": "access_token", "label": "Access Token / API Key",
          "required": True, "secret": True}],
    )

    for sch in schemes.values():
        if not isinstance(sch, dict):
            continue
        t = (sch.get("type") or "").lower()
        scheme = (sch.get("scheme") or "").lower()
        if t == "http" and scheme == "bearer":
            return bearer
        if (t == "http" and scheme == "basic") or t == "basic":
            return (
                {"type": "basic", "username_param": "username", "password_param": "password"},
                [{"name": "username", "label": "Username", "required": True},
                 {"name": "password", "label": "Password", "required": True, "secret": True}],
            )
        if t == "apikey":
            loc = (sch.get("in") or "header").lower()
            name = sch.get("name") or "X-API-Key"
            if loc == "query":
                return (
                    {"type": "api_key", "key_param": "api_key", "query_param": name},
                    [{"name": "api_key", "label": f"API Key ({name})", "required": True, "secret": True}],
                )
            return (
                {"type": "api_key", "key_param": "api_key", "header_name": name, "header_template": "{key}"},
                [{"name": "api_key", "label": f"API Key ({name})", "required": True, "secret": True}],
            )
        if t == "oauth2":
            token_url = ""
            for fl in (sch.get("flows") or {}).values():
                if isinstance(fl, dict) and fl.get("tokenUrl"):
                    token_url = fl["tokenUrl"]
                    break
            token_url = token_url or sch.get("tokenUrl", "")
            return (
                {"type": "oauth2", "access_token_param": "access_token", "token_url": token_url,
                 "client_id_param": "client_id", "client_secret_param": "client_secret"},
                [{"name": "access_token", "label": "Access Token (or use client credentials below)", "required": False, "secret": True},
                 {"name": "client_id", "label": "Client ID", "required": False},
                 {"name": "client_secret", "label": "Client Secret", "required": False, "secret": True}],
            )
    return bearer


def _base_url(spec: dict) -> str:
    servers = spec.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict) and servers[0].get("url"):
        return str(servers[0]["url"]).rstrip("/")
    host = spec.get("host")
    if host:
        scheme = (spec.get("schemes") or ["https"])[0]
        return f"{scheme}://{host}{spec.get('basePath', '') or ''}".rstrip("/")
    return ""


def manifest_from_openapi(
    spec: Any, *, connector_id: str | None = None,
    base_url: str | None = None, max_streams: int = 50,
) -> dict:
    """Parse an OpenAPI 3 / Swagger 2 spec into a draft manifest dict.

    GET operations become read streams (the framework is source-focused).
    Raises ValueError on a non-object spec.
    """
    if not isinstance(spec, dict):
        raise ValueError("OpenAPI spec must be a JSON object")
    info = spec.get("info") or {}
    title = str(info.get("title") or "API")
    auth, params = _auth_from_spec(spec)

    streams: list[dict] = []
    seen: set[str] = set()
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        get = methods.get("get")
        if not isinstance(get, dict):
            continue
        base = get.get("operationId") or path.strip("/").replace("/", "_") or "root"
        name = _slug(base)
        if name in seen:
            name = _slug(f"{base}_{path}")
        if not name or name in seen:
            continue
        seen.add(name)
        streams.append({
            "name": name,
            "label": str(get.get("summary") or name)[:60],
            "path": path,
            "method": "GET",
            # data_path + pagination are left for the human pass — the spec
            # rarely states where the array lives or how it pages.
        })
        if len(streams) >= max_streams:
            break

    return {
        "id": _slug(connector_id or title),
        "name": title,
        "description": str(info.get("description") or "")[:200],
        "category": "saas",
        "tier": "generated",  # never auto-ships at Certified — needs review + test
        "base_url": (base_url or _base_url(spec)).rstrip("/"),
        "auth": auth,
        "params": params,
        "streams": streams,
    }
