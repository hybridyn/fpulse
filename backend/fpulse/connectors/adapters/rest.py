"""Generic REST engine — config-driven pagination.

The v1 manifest runtime (rest_framework) already implements every
pagination style the open-core connectors use:

  * ``none``         — single-page response
  * ``page_number``  — increment a `page=` param until empty
  * ``offset_limit`` — Twilio/ServiceNow style
  * ``cursor``       — opaque cursor token OR full next URL in body
  * ``url``          — explicit next-URL follow (OData v4, Twilio,
                       Oracle Fusion's hasMore)
  * ``link_header``  — RFC-5988 Link header (GitHub, Shopify)

This adapter exposes a single ``run_rest_stream()`` entry that builds
an ephemeral manifest and dispatches into the same engine. It exists
so future first-class REST connectors don't have to author a
manifest file when their shape is dynamic (one connector → many
runtime paths) — they just call this function with the right config.

Bearer + Basic + API-key auth are supported by passing the auth tuple
shape from the OData adapter (kept identical for consistency).

2026-05-23 X2: oracle_fusion's hasMore/items/limit/offset can be
expressed as ``pagination.type = "url"`` with ``next_url_path =
"links[?rel == 'next'].href|[0]"`` — the JSONPath-ish dot syntax of
rest_framework's _dot_get isn't quite that powerful, so for Fusion
the simplest engine variant remains ``offset_limit``; this adapter
wraps both forms so callers pick the right one per connector.
"""

from __future__ import annotations

from typing import Any, Sequence

from fpulse.connectors.adapters.odata import _auth_block, _params_for_auth
from fpulse.connectors.rest_framework import (
    RestConnectorManifest,
    _execute_stream,
)


def run_rest_stream(
    *,
    base_url: str,
    path: str,
    method: str = "GET",
    auth: Sequence[Any] | None = None,
    query: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    data_path: str = "",
    pagination: dict[str, Any] | None = None,
    max_pages: int = 50,
) -> list[dict[str, Any]]:
    """Execute a single REST stream and return the collected rows.

    ``pagination`` is the same dict shape rest_framework accepts:

      {"type": "url",         "next_url_path": "links[0].href", "max_pages": 100}
      {"type": "cursor",      "cursor_field": "nextCursor", "cursor_param": "cursor"}
      {"type": "offset_limit","offset_param": "offset","limit_param": "limit",
                              "page_size": 100, "max_pages": 50}
      {"type": "page_number", "page_param": "page", "page_size_param": "per_page",
                              "page_size": 100, "max_pages": 50}
      {"type": "link_header"}
      {"type": "none"}

    Defaults to ``type=none`` (single page) when not provided.

    ``data_path`` is a dot-path into the response body to find the
    row array — e.g. ``data`` for Stripe-style responses, ``items``
    for Oracle Fusion, ``value`` for OData v4. Leave empty when the
    response IS the row array.
    """
    if not base_url:
        raise ValueError("run_rest_stream: base_url is required.")
    if not path:
        raise ValueError("run_rest_stream: path is required.")

    pagination = dict(pagination or {"type": "none"})
    pagination.setdefault("max_pages", max_pages)

    auth_kind = (auth[0].lower() if auth else "none")
    if auth_kind == "api_key":
        # auth = ("api_key", header_name, key_value)
        header_name = auth[1] if len(auth) > 1 else "X-API-Key"
        key_value = auth[2] if len(auth) > 2 else ""
        auth_block = {
            "type": "api_key",
            "header_name": header_name,
            "key_param": "_rest_apikey",
        }
        auth_params = {"_rest_apikey": key_value}
    else:
        auth_block = _auth_block(auth)
        auth_params = _params_for_auth(auth)

    manifest = RestConnectorManifest.from_dict({
        "id": "_inline_rest",
        "name": "Inline REST",
        "base_url": base_url.rstrip("/"),
        "auth": auth_block,
        "default_headers": headers or {},
        "streams": [{
            "name": "inline",
            "path": path if path.startswith("/") else f"/{path}",
            "method": method or "GET",
            "query": query or {},
            "data_path": data_path,
            "pagination": pagination,
        }],
    })

    return _execute_stream(manifest, manifest.streams[0], auth_params)


__all__ = ["run_rest_stream"]
