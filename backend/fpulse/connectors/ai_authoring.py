"""AI-authored connector generator — Sprint C.

Generates a manifest v2 skeleton from one of:

  1. **OpenAPI 3.x spec** — parses paths, identifies list/paginated endpoints,
     infers auth from the spec's security schemes, builds streams from JSON
     Schema response bodies.

  2. **Sample API responses** — given 1-3 example response payloads, infers
     a JSON Schema and a paginated stream skeleton.

The generator is deterministic — no LLM calls required for the MVP. The
output runs through the existing manifest_v2 validator so a freshly
generated connector either validates clean or produces concrete TODO
warnings the user can address.

Pipeline:
    user → POST /api/connectors/author/from-openapi {url|text, name}
         → openapi_to_manifest()
         → manifest_v2.validate_manifest()
         → returned with validation report

Reference: DESIGN_F01_MANIFEST_V2.md, manifest_v2.py.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from fpulse.connectors.manifest_v2 import (
    VALID_AUTH_TYPES,
    VALID_PAGINATION_STRATEGIES,
    validate_manifest,
)

logger = logging.getLogger(__name__)


# ── Public API ────────────────────────────────────────────────────────


def openapi_to_manifest(
    spec: dict,
    connector_id: str,
    *,
    display_name: str | None = None,
    category: str = "saas",
) -> dict:
    """Build a v2 manifest skeleton from an OpenAPI 3.x spec dict.

    The output is intentionally a *starter* — pagination strategy and
    incremental field are inferred when obvious and stubbed with TODOs
    when not. The user is expected to review the diff before saving.
    """
    info = spec.get("info") or {}
    if not display_name:
        display_name = str(info.get("title") or connector_id).strip()

    base_url = _extract_base_url(spec)
    homepage = _server_url_no_path(base_url)
    # Drop relative URLs (e.g. "/api/v3" from a Petstore-style spec) —
    # manifest homepage should be the absolute origin or empty.
    if homepage and not homepage.startswith(("http://", "https://")):
        homepage = ""
    # Vendor distinct from display_name when we have no other signal.
    # Default to empty rather than duplicating the title verbatim.
    vendor = (info.get("contact") or {}).get("name") or ""
    if not vendor and info.get("title") and info["title"].strip() != display_name:
        vendor = info["title"].strip()

    auth_block = _build_auth_block(spec)
    rate_limit_block = _default_rate_limit()
    streams = _streams_from_paths(spec)

    manifest: dict[str, Any] = {
        "version": 2,
        "connector": {
            "type": connector_id,
            "display_name": display_name,
            "category": category,
            "vendor": vendor,
            "homepage": homepage,
            "docs_url": info.get("contact", {}).get("url") or info.get("license", {}).get("url") or "",
            "oss": True,
        },
        "certification": {
            "depth_score": 1,        # AI-generated → starts at depth 1
            "status": "beta",
            "last_validated": datetime.now(timezone.utc).date().isoformat(),
            "owner": "community",
            "validator": "ai-authoring",
            "known_issues": [
                "Auto-generated from OpenAPI; review pagination + incremental fields before promoting beyond depth 1.",
                "Fixtures pending: depth ≥ 3 requires happy_path/empty/auth_error/rate_limit/schema_drift fixtures.",
            ],
        },
        "auth": auth_block,
        "rate_limit": rate_limit_block,
        "streams": streams,
        "fixtures": [],
    }
    return manifest


def samples_to_manifest(
    samples: list[dict],
    connector_id: str,
    *,
    base_url: str = "",
    display_name: str | None = None,
    category: str = "saas",
    stream_name: str | None = None,
) -> dict:
    """Build a v2 manifest skeleton from one or more sample response payloads.

    The first sample drives schema inference. Pagination is left as
    `none` with a TODO unless the caller specifies it later. Auth is left
    as a `custom` placeholder.
    """
    if not samples:
        raise ValueError("samples_to_manifest: provide at least one sample response")

    if not display_name:
        display_name = connector_id.replace("_", " ").title()

    primary_sample = samples[0]
    rows = _extract_rows_from_sample(primary_sample)
    inferred_schema = _infer_json_schema(rows[0] if rows else primary_sample)

    inferred_pk = _guess_primary_key(inferred_schema)
    inferred_incremental, incremental_format = _guess_incremental_field(inferred_schema)

    name = (stream_name or _guess_stream_name(primary_sample) or "items").strip()

    stream: dict[str, Any] = {
        "name": name,
        "primary_key": inferred_pk,
        "incremental_field": inferred_incremental,
        "incremental_format": incremental_format,
        "cursor_strategy": "page_token" if inferred_incremental else "full_refresh",
        "pagination": {
            "strategy": "none",
            "_note": "TODO: set strategy + cursor_field. Inspect API docs for next-page mechanism.",
        },
        "depends_on": [],
        "schema": inferred_schema,
    }

    return {
        "version": 2,
        "connector": {
            "type": connector_id,
            "display_name": display_name,
            "category": category,
            "vendor": display_name,
            "homepage": _server_url_no_path(base_url),
            "docs_url": "",
            "oss": True,
        },
        "certification": {
            "depth_score": 1,
            "status": "beta",
            "last_validated": datetime.now(timezone.utc).date().isoformat(),
            "owner": "community",
            "validator": "ai-authoring",
            "known_issues": [
                "Auto-generated from sample payloads; pagination + auth need manual review.",
                "Single-stream skeleton. Add additional streams for related resources.",
            ],
        },
        "auth": {
            "schemes": [
                {
                    "type": "custom",
                    "_note": "TODO: replace with api_key / oauth2 / basic / jwt_bearer per the API's auth model.",
                }
            ],
        },
        "rate_limit": _default_rate_limit(),
        "streams": [stream],
        "fixtures": [],
    }


def generate_and_validate(
    spec_or_samples: dict | list,
    connector_id: str,
    *,
    mode: str,
    display_name: str | None = None,
    category: str = "saas",
    base_url: str = "",
    stream_name: str | None = None,
) -> dict:
    """End-to-end: generate manifest + validate + return both.

    Returns a dict with keys:
      - `manifest`: the generated v2 manifest dict
      - `validation`: ValidationResult.to_dict() output
      - `mode`: 'openapi' or 'samples'
    """
    if mode == "openapi":
        if not isinstance(spec_or_samples, dict):
            raise ValueError("openapi mode expects a dict spec")
        manifest = openapi_to_manifest(
            spec_or_samples, connector_id,
            display_name=display_name, category=category,
        )
    elif mode == "samples":
        if not isinstance(spec_or_samples, list):
            raise ValueError("samples mode expects a list of payload dicts")
        manifest = samples_to_manifest(
            spec_or_samples, connector_id,
            base_url=base_url, display_name=display_name,
            category=category, stream_name=stream_name,
        )
    else:
        raise ValueError(f"unknown mode: {mode!r} (expected 'openapi' or 'samples')")

    validation = validate_manifest(manifest, connector_root=None)
    return {
        "manifest": manifest,
        "validation": validation.to_dict(),
        "mode": mode,
    }


# ── OpenAPI parsing helpers ───────────────────────────────────────────


def _extract_base_url(spec: dict) -> str:
    servers = spec.get("servers") or []
    if servers and isinstance(servers[0], dict):
        return str(servers[0].get("url") or "")
    return ""


def _server_url_no_path(url: str) -> str:
    """Strip path off server URL — homepage should be the bare origin."""
    m = re.match(r"^(https?://[^/]+)", url or "")
    return m.group(1) if m else url


def _build_auth_block(spec: dict) -> dict:
    """Map OpenAPI security schemes to manifest v2 auth schemes."""
    components = spec.get("components") or {}
    schemes = components.get("securitySchemes") or {}
    mapped: list[dict] = []

    for _name, scheme in schemes.items():
        if not isinstance(scheme, dict):
            continue
        s_type = (scheme.get("type") or "").lower()
        s_scheme = (scheme.get("scheme") or "").lower()

        if s_type == "http" and s_scheme == "bearer":
            mapped.append({
                "type": "jwt_bearer",
                "header": "Authorization",
                "value_template": "Bearer {token}",
                "docs": scheme.get("description", ""),
            })
        elif s_type == "http" and s_scheme == "basic":
            mapped.append({
                "type": "basic",
                "username_template": "{username}",
                "password_template": "{password}",
                "docs": scheme.get("description", ""),
            })
        elif s_type == "apikey":
            mapped.append({
                "type": "api_key",
                "in": (scheme.get("in") or "header").lower(),
                "name": scheme.get("name") or "X-API-Key",
                "value_template": "{api_key}",
                "docs": scheme.get("description", ""),
            })
        elif s_type == "oauth2":
            flows = scheme.get("flows") or {}
            primary_flow = next(iter(flows.values())) if flows else {}
            mapped.append({
                "type": "oauth2",
                "authorization_url": primary_flow.get("authorizationUrl", ""),
                "token_url": primary_flow.get("tokenUrl", ""),
                "scopes": list((primary_flow.get("scopes") or {}).keys()),
                "docs": scheme.get("description", ""),
            })

    if not mapped:
        # Most APIs need *some* auth — leave a clear TODO instead of silence.
        mapped.append({
            "type": "custom",
            "_note": "TODO: API has no securitySchemes declared in OpenAPI; configure auth manually.",
        })

    # Sanity: ensure all type values are valid per the manifest schema.
    for s in mapped:
        if s.get("type") not in VALID_AUTH_TYPES:
            s["type"] = "custom"

    return {"schemes": mapped}


def _default_rate_limit() -> dict:
    """Sensible defaults — most public APIs converge on this shape."""
    return {
        "default": {
            "requests_per_minute": 60,
            "daily_quota": None,
        },
        "retry": {
            "max_attempts": 5,
            "backoff": "exponential",
            "base_seconds": 2,
            "max_seconds": 60,
            "retry_on_status": [429, 500, 502, 503, 504],
        },
    }


def _streams_from_paths(spec: dict) -> list[dict]:
    """Identify list/paginated GET endpoints and emit a stream per resource.

    Heuristic: any `GET` whose response includes an array (top-level or
    nested under a common pagination wrapper) is a candidate stream.
    Multiple paths returning the same schema collapse into a single
    stream (e.g. /pet/findByStatus + /pet/findByTags both → 'pets').
    """
    paths = spec.get("paths") or {}
    # First pass: collect candidate (path, schema, op) tuples with the
    # schema fully resolved + cleaned. Second pass: dedupe by schema and
    # name them.
    candidates: list[tuple[str, dict, dict]] = []
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        get = methods.get("get")
        if not isinstance(get, dict):
            continue
        item_schema = _extract_array_item_schema(get, spec)
        if not item_schema:
            continue
        candidates.append((path, item_schema, get))

    if not candidates:
        return []

    # Group by content-equivalent schemas. Two streams with the same
    # `properties` keys + types are the same resource viewed differently
    # (e.g. /pet/findByStatus vs /pet/findByTags both return Pet).
    groups: dict[str, list[tuple[str, dict, dict]]] = {}
    for path, schema, get in candidates:
        sig = _schema_signature(schema)
        groups.setdefault(sig, []).append((path, schema, get))

    streams: list[dict] = []
    seen_names: set[str] = set()
    for _sig, items in groups.items():
        # Pick a representative — the one with the shortest path is
        # almost always the canonical resource endpoint (`/pet` over
        # `/pet/findByStatus`). When all have similar lengths, the first
        # candidate wins.
        items_sorted = sorted(items, key=lambda x: (len(x[0]), x[0]))
        path, schema, get = items_sorted[0]

        # Naming: try multiple sources in order, pick the first that
        # produces a clean noun.
        name = _stream_name_from_op(path, get) or _path_to_stream_name(path) or "items"
        # Plural-form preference (Stripe-style).
        name = _pluralize_simple(name)
        # Disambiguate if collision.
        base = name
        i = 2
        while name in seen_names:
            name = f"{base}_{i}"
            i += 1
        seen_names.add(name)

        pagination = _infer_pagination_from_parameters(get.get("parameters") or [])
        primary_key = _guess_primary_key(schema)
        incremental_field, incremental_format = _guess_incremental_field(schema)

        streams.append({
            "name": name,
            "primary_key": primary_key,
            "incremental_field": incremental_field,
            "incremental_format": incremental_format,
            "cursor_strategy": "page_token" if incremental_field else "full_refresh",
            "pagination": pagination,
            "depends_on": [],
            "schema": schema,
            "_paths": [p for p, _, _ in items_sorted],
        })

    return streams


def _schema_signature(schema: dict) -> str:
    """Stable signature over a schema's property names + types — used to
    group endpoints that return the same resource shape."""
    if not isinstance(schema, dict):
        return ""
    props = schema.get("properties") or {}
    parts = []
    for k in sorted(props.keys()):
        v = props.get(k)
        if isinstance(v, dict):
            t = v.get("type") or v.get("$ref") or ""
            parts.append(f"{k}:{t}")
        else:
            parts.append(k)
    return "|".join(parts)


# Verbs / qualifiers that pollute path-derived stream names. We drop
# them when picking the noun. `findByStatus`, `search`, `latest`,
# `recent` etc. are operations, not resources.
_PATH_VERB_TOKENS = {
    "findbystatus", "findbytags", "findby", "search", "list", "latest",
    "recent", "upload", "uploadimage", "login", "logout", "logoutuser",
    "loginuser", "createwithlist", "create", "delete", "update", "get",
}


def _stream_name_from_op(path: str, get_op: dict) -> str | None:
    """Try operationId first; if it looks like a verb-noun combo, prefer
    the noun. Falls back to None so the caller tries path heuristics."""
    op_id = get_op.get("operationId")
    if not op_id:
        return None
    snake = _camel_to_snake(op_id)
    # If operationId is `getPetById`, take the noun (`pet`); if it's
    # `listOrders`, take `orders`. We strip leading verbs.
    parts = snake.split("_")
    leading_verbs = {"get", "list", "find", "search", "fetch", "show", "read"}
    while parts and parts[0] in leading_verbs:
        parts.pop(0)
    # Drop trailing qualifiers like `_by_status`.
    if "by" in parts:
        parts = parts[: parts.index("by")]
    name = "_".join(parts).strip("_")
    if name and name not in _PATH_VERB_TOKENS:
        return name
    return None


def _camel_to_snake(s: str) -> str:
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def _pluralize_simple(word: str) -> str:
    """Tiny English pluralizer — covers the 95% case."""
    if not word or word.endswith(("s", "data", "info")):
        return word
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("ch", "sh", "x", "z")):
        return word + "es"
    return word + "s"


def _extract_array_item_schema(get_op: dict, spec: dict) -> dict | None:
    """Find the array item's JSON Schema in a GET 200 response.

    Walks through `$ref` chains and returns a fully-resolved, cleaned
    schema (no $refs left, no OpenAPI-only fields like `xml` / `example` /
    `discriminator`). Required for the manifest validator, which insists
    every property has an explicit `type`.
    """
    responses = get_op.get("responses") or {}
    success = responses.get("200") or responses.get("default") or {}
    content = (success.get("content") or {}).get("application/json") or {}
    schema = content.get("schema")
    if not isinstance(schema, dict):
        return None

    schema = _resolve_refs_deep(schema, spec)

    # Top-level array.
    if schema.get("type") == "array":
        items = _resolve_refs_deep(schema.get("items") or {}, spec)
        return _clean_schema(items) if items.get("type") == "object" else None

    # Common pagination wrapper: { data: [...], next_cursor: ... } or similar.
    if schema.get("type") == "object":
        properties = schema.get("properties") or {}
        for key in ("data", "results", "items", "records"):
            wrapped = properties.get(key)
            if not isinstance(wrapped, dict):
                continue
            wrapped = _resolve_refs_deep(wrapped, spec)
            if wrapped.get("type") == "array":
                items = _resolve_refs_deep(wrapped.get("items") or {}, spec)
                if items.get("type") == "object":
                    return _clean_schema(items)
    return None


# OpenAPI-specific fields that aren't part of JSON Schema draft-07 and
# break the manifest validator if left in. Strip them recursively.
_OPENAPI_NOISE_FIELDS = {"xml", "example", "examples", "discriminator", "externalDocs", "readOnly", "writeOnly"}

# Hard cap on $ref recursion depth to avoid infinite loops on circular
# references (which OpenAPI specs sometimes have).
_MAX_REF_DEPTH = 10


def _resolve_refs_deep(node: Any, spec: dict, _depth: int = 0) -> Any:
    """Walk a JSON Schema tree replacing every `$ref` with the resolved
    target. Bounded recursion. Returns the input unchanged if it isn't
    a dict / list. Adds an explicit `type: object` when the resolved
    object lacks one (so the manifest validator stops complaining)."""
    if _depth > _MAX_REF_DEPTH:
        return node
    if isinstance(node, list):
        return [_resolve_refs_deep(item, spec, _depth + 1) for item in node]
    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        target = _follow_ref(ref, spec)
        if target is not None:
            # Recursively resolve the target — its properties may have
            # their own $refs.
            resolved = _resolve_refs_deep(target, spec, _depth + 1)
            return resolved

    # Walk every value, recursing into nested dicts/lists.
    out: dict = {}
    for k, v in node.items():
        out[k] = _resolve_refs_deep(v, spec, _depth + 1)

    # Defensive: an object with `properties` but no explicit `type`
    # confuses the validator. Add it.
    if "properties" in out and "type" not in out and not ref:
        out["type"] = "object"

    return out


def _follow_ref(ref: str, spec: dict) -> dict | None:
    parts = ref.lstrip("#/").split("/")
    cur: Any = spec
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return None
    return cur if isinstance(cur, dict) else None


def _clean_schema(schema: dict) -> dict:
    """Strip OpenAPI-only fields and ensure every property has a `type`.

    The manifest validator's `_validate_schema` requires `type` on every
    property entry — without this pass the OpenAPI specs that use
    `enum`-only or `format`-only properties (no explicit `type`) all
    fail validation with errors like 'properties.x.type: required'.
    """
    if not isinstance(schema, dict):
        return schema

    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in _OPENAPI_NOISE_FIELDS:
            continue
        if isinstance(v, dict):
            v = _clean_schema(v)
        elif isinstance(v, list):
            v = [_clean_schema(item) if isinstance(item, dict) else item for item in v]
        out[k] = v

    # Add a default $schema if missing — manifest v2 expects draft-07.
    if "type" in out and out.get("type") == "object" and "$schema" not in out:
        out["$schema"] = "http://json-schema.org/draft-07/schema#"

    # Ensure every property has a type. If a property has only an `enum`,
    # infer `string`. If only `format`, infer `string`. If only `$ref`
    # (residual from circular ref), drop it (schema-wise it's a black
    # box, downstream fills it in).
    properties = out.get("properties")
    if isinstance(properties, dict):
        for prop_name, prop_def in list(properties.items()):
            if not isinstance(prop_def, dict):
                continue
            if "type" not in prop_def:
                if "enum" in prop_def or "format" in prop_def:
                    prop_def["type"] = "string"
                elif "properties" in prop_def:
                    prop_def["type"] = "object"
                elif "items" in prop_def:
                    prop_def["type"] = "array"
                else:
                    # Last resort — call it a string. Better than the
                    # validator crashing on missing `type`.
                    prop_def["type"] = "string"

    return out


def _resolve_ref(node: dict, spec: dict) -> dict:
    """Backwards-compat shim — kept for any external callers; new code
    should use `_resolve_refs_deep`."""
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if not ref:
        return node
    target = _follow_ref(ref, spec)
    return target if target is not None else node


def _path_to_stream_name(path: str) -> str:
    """`/v1/customers/{id}` → `customers`. `/api/orders` → `orders`."""
    segments = [s for s in path.split("/") if s and not s.startswith("{")]
    if not segments:
        return ""
    # Skip version prefixes.
    if re.match(r"^v\d+$|^api$", segments[0], re.IGNORECASE):
        segments = segments[1:]
    if not segments:
        return ""
    return segments[-1].replace("-", "_").lower()


def _infer_pagination_from_parameters(parameters: list) -> dict:
    """Look for common pagination query parameter names."""
    names = [
        (p.get("name") or "").lower()
        for p in parameters if isinstance(p, dict)
    ]

    # Cursor-based.
    cursor_params = ["cursor", "starting_after", "after", "next_token", "page_token"]
    for cp in cursor_params:
        if cp in names:
            return {
                "strategy": "cursor",
                "cursor_param": cp,
                "cursor_field": "next_cursor",  # TODO: confirm response field
                "page_size_param": next(
                    (n for n in ["limit", "per_page", "page_size", "count"] if n in names),
                    "limit",
                ),
                "page_size": 100,
                "_note": "cursor_field auto-set to 'next_cursor' — confirm against API docs.",
            }

    # Offset-based.
    if "offset" in names or "skip" in names:
        return {
            "strategy": "offset",
            "offset_param": "offset" if "offset" in names else "skip",
            "limit_param": next(
                (n for n in ["limit", "per_page", "page_size", "top"] if n in names),
                "limit",
            ),
            "page_size": 100,
        }

    # Page-number-based.
    if "page" in names:
        return {
            "strategy": "page_token",
            "page_param": "page",
            "page_size_param": next(
                (n for n in ["per_page", "limit", "page_size"] if n in names),
                "per_page",
            ),
            "page_size": 100,
        }

    return {
        "strategy": "none",
        "_note": "No paging params detected. Verify the API response is single-shot.",
    }


# ── JSON Schema inference (samples mode) ──────────────────────────────


def _infer_json_schema(sample: Any) -> dict:
    """Derive a minimal JSON Schema draft-07 object from a sample payload."""
    if not isinstance(sample, dict):
        # Defensive — a stream's items must be objects per manifest v2.
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"value": {"type": "string"}},
        }
    properties: dict[str, Any] = {}
    required: list[str] = []
    for k, v in sample.items():
        properties[k] = _python_value_to_schema(v)
        if v is not None:
            required.append(k)
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": required,
        "properties": properties,
    }


def _python_value_to_schema(v: Any) -> dict:
    if v is None:
        return {"type": ["string", "null"]}
    if isinstance(v, bool):
        return {"type": "boolean"}
    if isinstance(v, int):
        return {"type": "integer"}
    if isinstance(v, float):
        return {"type": "number"}
    if isinstance(v, str):
        if _looks_like_iso_datetime(v):
            return {"type": "string", "format": "date-time"}
        if _looks_like_email(v):
            return {"type": "string", "format": "email"}
        return {"type": "string"}
    if isinstance(v, list):
        if v:
            return {"type": "array", "items": _python_value_to_schema(v[0])}
        return {"type": "array", "items": {"type": "string"}}
    if isinstance(v, dict):
        return _infer_json_schema(v)
    return {"type": "string"}


def _looks_like_iso_datetime(s: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", s))


def _looks_like_email(s: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s))


def _extract_rows_from_sample(sample: Any) -> list[dict]:
    """Pull row-shaped objects from a sample payload (handles common wrappers)."""
    if isinstance(sample, list):
        return [r for r in sample if isinstance(r, dict)]
    if isinstance(sample, dict):
        for key in ("data", "results", "items", "records"):
            wrapped = sample.get(key)
            if isinstance(wrapped, list):
                return [r for r in wrapped if isinstance(r, dict)]
        return [sample]
    return []


def _guess_stream_name(sample: Any) -> str | None:
    """If the sample wraps rows under a key like 'data' or 'results', use the
    plural noun closest to it. Otherwise None."""
    if isinstance(sample, dict):
        for key in ("data", "results", "items", "records"):
            if key in sample and isinstance(sample[key], list):
                return key
    return None


def _guess_primary_key(schema: dict) -> list[str]:
    """Pick the first column that looks like an id."""
    properties = schema.get("properties") or {}
    for name in ("id", "uuid", "_id", "pk", "primary_id"):
        if name in properties:
            return [name]
    # Fallback: anything ending in '_id'.
    for name in properties:
        if name.endswith("_id"):
            return [name]
    return []


def _guess_incremental_field(schema: dict) -> tuple[str | None, str]:
    """Look for a timestamp-shaped column; return (name, format)."""
    properties = schema.get("properties") or {}
    candidates_iso = ("updated_at", "modified_at", "last_modified", "created_at", "timestamp")
    candidates_unix = ("created", "updated", "modified", "ts", "ts_ms")

    for name in candidates_iso:
        if name in properties:
            return name, "iso8601"
    for name in candidates_unix:
        prop = properties.get(name)
        if isinstance(prop, dict) and prop.get("type") == "integer":
            return name, "unix_seconds"
    return None, "iso8601"


# ── URL fetching (for from-openapi mode) ──────────────────────────────
#
# SSRF defense (2026-05-29):
#
# The Author Connector feature lets users paste an arbitrary OpenAPI URL,
# which the server then fetches. Without validation this is a classic
# server-side request forgery (SSRF) sink — an attacker could submit
# `http://169.254.169.254/latest/meta-data/iam/security-credentials/` to
# exfiltrate cloud-instance credentials, or `http://localhost:5432` to
# probe internal services, or `file:///etc/passwd` to read local files.
#
# Defenses below, in order:
#   1. Scheme allowlist (http / https only — no file://, gopher://, etc.)
#   2. Host required (no bare schemes, no userinfo trick)
#   3. DNS resolution + rejection of any address that resolves to:
#        loopback (127/8, ::1)
#        link-local (169.254/16) — incl. AWS / GCP / Azure metadata
#        private (10/8, 172.16/12, 192.168/16, fc00::/7)
#        multicast / reserved / unspecified
#   4. Resolved-IP fetch (Host header set to original hostname) so a DNS-
#      rebinding attack can't flip the address between check and fetch.
#   5. Redirect handling: each Location: hop is re-validated through the
#      same pipeline. Cap at 5 hops to prevent infinite-redirect DoS.
#   6. Response size cap (2 MB) — OpenAPI specs that are larger are
#      almost certainly attacks or mistakes.
#
# Override for development / on-prem with internal API catalogs:
#   FPULSE_OPENAPI_FETCH_ALLOW_PRIVATE=1   # allow private/loopback hosts
# Default off — only enable in trusted internal-network deployments.


# 2026-06-03 — SsrfBlockedError + _ssrf_check_url extracted to the
# shared module `fpulse.security.ssrf` so the same defence applies to
# every user-controlled URL fetch in F-Pulse (api_source, http_request,
# OpenAPI authoring, future webhook delivery, …) — not just this file.
# Re-exported here so existing imports keep working unchanged.
from fpulse.security.ssrf import (
    SsrfBlockedError,
    check_url as _ssrf_check_url_impl,
    OPENAPI_ALLOW_PRIVATE_ENV,
)


_MAX_OPENAPI_SPEC_BYTES = 2 * 1024 * 1024   # 2 MB
_MAX_OPENAPI_REDIRECTS = 5


def parse_spec_text(text: str) -> dict:
    """Parse an OpenAPI/Swagger spec from raw text (JSON first, then YAML).

    The shared parser behind both the URL-fetch path and the paste/upload
    path, so a spec typed or dropped into the Author page (or handed to the
    Copilot as text) is decoded exactly like one fetched from a URL. Size-
    capped identically to the fetch path so a huge paste can't exhaust memory.

    Raises ``ValueError`` if the text is empty, too large, or decodes to
    something that isn't a JSON/YAML object.
    """
    if not isinstance(text, str):
        raise ValueError("spec text must be a string")
    raw = text.strip()
    if not raw:
        raise ValueError("spec text is empty")
    # Cap on the raw character length — the same 2 MB ceiling the fetch path
    # enforces on the response body. Bytes ≥ chars for UTF-8, so measuring
    # the encoded length is the honest bound.
    if len(raw.encode("utf-8", errors="ignore")) > _MAX_OPENAPI_SPEC_BYTES:
        raise ValueError(
            f"Spec exceeds {_MAX_OPENAPI_SPEC_BYTES} bytes; refusing to parse"
        )

    # JSON first (cheaper, most common).
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        # YAML fallback if PyYAML is available; otherwise surface a clear error.
        try:
            import yaml
        except ImportError as e:
            raise ValueError(
                "Spec is not JSON and PyYAML is not installed. "
                "Paste a JSON spec or install pyyaml."
            ) from e
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError as e:  # noqa: BLE001 — user-facing parse error
            raise ValueError(f"Spec is not valid JSON or YAML: {e}") from e

    if not isinstance(loaded, dict):
        raise ValueError("OpenAPI spec must decode to an object (a JSON/YAML mapping)")
    return loaded


def _ssrf_check_url(url: str) -> tuple[str, str, int]:
    """Validate a URL against the OpenAPI-fetch SSRF policy.

    Thin wrapper around :func:`fpulse.security.ssrf.check_url` that
    pins the OpenAPI-specific escape env var
    (:data:`fpulse.security.ssrf.OPENAPI_ALLOW_PRIVATE_ENV`). Kept as a
    no-arg function so legacy callers don't have to thread the env-var
    name through every call site.
    """
    return _ssrf_check_url_impl(url, allow_private_env=OPENAPI_ALLOW_PRIVATE_ENV)


def fetch_openapi_spec(url: str, *, timeout: float = 10.0) -> dict:
    """Fetch an OpenAPI spec from a URL. Accepts JSON or YAML content.

    SSRF-hardened (2026-05-29) — the user-supplied URL is validated
    through ``_ssrf_check_url`` before each request and on every
    redirect hop. See the comment block above for the threat model
    and the ``FPULSE_OPENAPI_FETCH_ALLOW_PRIVATE`` override.
    """
    import urllib.request
    import urllib.error

    current_url = url
    for _hop in range(_MAX_OPENAPI_REDIRECTS):
        # Validate before every fetch — covers both the initial URL
        # and every redirect Location.
        _ssrf_check_url(current_url)

        req = urllib.request.Request(
            current_url,
            headers={
                # Identify ourselves so well-behaved upstreams can rate-
                # limit / log appropriately, and don't accept gzip — we
                # cap the body at 2 MB raw.
                "User-Agent": "F-Pulse/1.0 OpenAPI-fetcher",
                "Accept": "application/json, application/yaml, text/yaml, */*;q=0.1",
            },
        )
        # Use an opener that does NOT follow redirects automatically —
        # we need to revalidate each Location through the SSRF check.
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = resp.getcode()
                if status in (301, 302, 303, 307, 308):
                    next_url = resp.headers.get("Location")
                    if not next_url:
                        raise SsrfBlockedError(
                            "Redirect with no Location header"
                        )
                    # Relative Location: resolve against current_url.
                    from urllib.parse import urljoin
                    current_url = urljoin(current_url, next_url)
                    continue
                # Cap the read to defeat decompression-bomb / huge-body
                # attempts. Reading 1 byte over the limit is enough to
                # know the body exceeded the cap.
                body = resp.read(_MAX_OPENAPI_SPEC_BYTES + 1)
                if len(body) > _MAX_OPENAPI_SPEC_BYTES:
                    raise SsrfBlockedError(
                        f"Spec body exceeds {_MAX_OPENAPI_SPEC_BYTES} bytes; "
                        f"refusing to load"
                    )
                body_str = body.decode("utf-8", errors="replace")
                break
        except urllib.error.URLError as exc:
            # Surface network errors cleanly — the caller maps these to
            # an HTTPException 400.
            raise RuntimeError(f"Fetch failed: {exc}") from exc
    else:
        raise SsrfBlockedError(
            f"Too many redirects (>{_MAX_OPENAPI_REDIRECTS}); possible loop"
        )

    # Decode via the shared JSON-then-YAML parser (same path as pasted specs).
    return parse_spec_text(body_str)


class _NoRedirect(__import__("urllib.request", fromlist=["HTTPRedirectHandler"]).HTTPRedirectHandler):
    """urllib redirect handler that does NOT follow automatically.

    Used by ``fetch_openapi_spec`` so each redirect hop can be
    re-validated through ``_ssrf_check_url`` before the next fetch.
    Returning None from these hooks causes urllib to surface the 3xx
    response to the caller, which then dispatches the loop above.
    """

    def http_error_301(self, req, fp, code, msg, headers):  # noqa: D401
        # Return the response un-followed so the caller sees the 3xx
        # plus the Location header and can re-validate.
        return fp

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301
