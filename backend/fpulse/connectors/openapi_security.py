"""Operation-level OpenAPI security, without guessing credentials or OAuth grants."""
from __future__ import annotations

import base64
import re
from http.cookies import SimpleCookie
from typing import Any


def normalize_security(spec: dict, requirements: Any) -> tuple[dict, list[dict]]:
    schemes = (spec.get("components") or {}).get("securitySchemes", {})
    if not schemes:
        schemes = spec.get("securityDefinitions") or {}
    if not isinstance(schemes, dict):
        raise ValueError("OpenAPI security schemes must be an object")
    if not isinstance(requirements, list):
        raise ValueError("OpenAPI security must be an array")
    alternatives, params = [], {}
    for requirement in requirements or [{}]:
        if not isinstance(requirement, dict):
            raise ValueError("OpenAPI security alternatives must be objects")
        bindings = []
        for name, scopes in requirement.items():
            if not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes):
                raise ValueError("OpenAPI security scopes must be arrays of strings")
            scheme = schemes.get(name)
            binding: dict = {"name": name, "scopes": scopes, "type": "unsupported"}
            fields = []
            if not isinstance(scheme, dict) or "$ref" in scheme:
                binding["reason"] = "Missing or unresolved security scheme"
            elif scheme.get("type") == "http" and scheme.get("scheme", "").lower() == "bearer":
                binding["type"] = "bearer"
                fields = ["token"]
            elif scheme.get("type") == "basic" or (
                scheme.get("type") == "http" and scheme.get("scheme", "").lower() == "basic"
            ):
                binding["type"] = "basic"
                fields = ["username", "password"]
            elif scheme.get("type") == "apiKey" and scheme.get("in") in ("header", "query", "cookie"):
                key = scheme.get("name")
                if not isinstance(key, str) or not key or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
                    binding["reason"] = "Invalid API key name"
                else:
                    binding.update(type="api_key", location=scheme["in"], key_name=key)
                    fields = ["key"]
            else:
                binding["reason"] = (
                    "OAuth requires a saved-connection grant flow; not enabled for generated connectors yet"
                    if scheme.get("type") in ("oauth2", "openIdConnect")
                    else "Authentication method is not supported by generated connectors"
                )
            binding["fields"] = {}
            for field in fields:
                # Hex encoding is stable and collision-free, even for punctuation in scheme names.
                param = f"auth_{name.encode().hex()}_{field}"
                binding["fields"][field] = param
                params[param] = {"name": param, "label": f"{name}: {field}",
                                 "secret": True, "required": False}
            bindings.append(binding)
        alternatives.append({"schemes": bindings,
                             "supported": all(b["type"] != "unsupported" for b in bindings)})
    return {"type": "openapi", "alternatives": alternatives}, list(params.values())


def build_security(auth: dict, params: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Select one OR alternative and satisfy every AND scheme before any HTTP call."""
    alternatives = auth.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        raise ValueError("Invalid OpenAPI security definition")
    selected = params.get("auth_alternative")
    if selected in (None, "") and len(alternatives) == 1:
        selected = 0
    if isinstance(selected, bool) or not str(selected).isdigit() or int(selected) >= len(alternatives):
        raise ValueError("Select an authentication alternative for this operation")
    alternative = alternatives[int(selected)]
    headers: dict[str, str] = {}
    query: dict[str, str] = {}
    cookies = SimpleCookie()
    for binding in alternative["schemes"]:
        kind = binding["type"]
        if kind not in ("bearer", "basic", "api_key"):
            raise ValueError("Selected authentication method is not supported")
        values = {}
        required = {"bearer": ("token",), "basic": ("username", "password"), "api_key": ("key",)}[kind]
        for field in required:
            param = binding.get("fields", {}).get(field)
            value = params.get(param, "" if field == "password" else None) if param else None
            if not isinstance(value, str) or (not value and field != "password") or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError("Required authentication field is missing or invalid")
            values[field] = value
        header, value = None, None
        if kind == "bearer":
            header, value = "Authorization", f"Bearer {values['token']}"
        elif kind == "basic":
            if ":" in values["username"]:
                raise ValueError("Basic authentication username cannot contain a colon")
            token = base64.b64encode(f"{values['username']}:{values['password']}".encode()).decode()
            header, value = "Authorization", f"Basic {token}"
        else:
            key = binding["key_name"]
            location = binding["location"]
            if location == "header":
                header, value = key, values["key"]
            elif location == "query":
                if key in query:
                    raise ValueError("Conflicting authentication query parameters")
                query[key] = values["key"]
            elif location == "cookie":
                if key in cookies:
                    raise ValueError("Conflicting authentication cookies")
                cookies[key] = values["key"]
            else:
                raise ValueError("Unsupported API key location")
        if header:
            if header.lower() in {h.lower() for h in headers}:
                raise ValueError("Conflicting authentication headers")
            headers[header] = value
    if cookies:
        if "cookie" in {h.lower() for h in headers}:
            raise ValueError("Conflicting authentication cookie header")
        headers["Cookie"] = cookies.output(header="", sep=";").strip()
    return headers, query
