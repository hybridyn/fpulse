"""OData engine — one adapter, two versions.

OData v2 (legacy SAP) and v4 (modern SAP / Microsoft) differ in three
places:

  * ``data_path``     — v2 is ``d.results``; v4 is ``value``.
  * ``next_url_path`` — v2 is ``d.__next``; v4 is ``@odata.nextLink``.
  * ``$format``       — v2 needs an explicit ``$format=json``; v4
                         defaults to JSON.

This adapter hides those differences behind a single ``run_odata_stream()``
entry point. Callers pass:

  base_url       — service root, e.g. ``https://host/sap/opu/odata/sap/SRVC``
  entity_set     — the OData entity-set name (path segment)
  version        — ``v2`` (default) or ``v4``
  auth           — ``("basic", username, password)`` |
                   ``("bearer", token)``               |
                   ``("none",)`` (default)
  filter_query   — optional ``$filter`` value
  select_fields  — optional ``$select`` value
  top            — optional ``$top`` value (server-side row limit)
  sap_client     — optional ``sap-client`` query parameter
  extra_headers  — dict of headers merged after auth

Returns a flat list of row dicts collected across pages.

The implementation delegates to rest_framework's ``_execute_stream`` so
the T1-era default_query / url-pagination plumbing applies for free.
The adapter constructs an ephemeral in-memory ``RestConnectorManifest``;
no JSON file is written.

2026-05-23 X1: this module is the substrate for sap_s4hana,
sap_successfactors, and any future OData-backed connector. Adding a
new one should require only an entry in the picker + a tester probe;
the read path is config-only.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from fpulse.connectors.rest_framework import (
    RestConnectorManifest,
    _execute_stream,
)


def _auth_block(auth: Sequence[Any] | None) -> dict[str, Any]:
    """Translate the friendly auth tuple to a manifest auth block."""
    if not auth:
        return {"type": "none"}
    kind = auth[0].lower()
    if kind == "basic":
        return {
            "type": "basic",
            "username_param": "_odata_user",
            "password_param": "_odata_pass",
        }
    if kind == "bearer":
        return {
            "type": "bearer",
            "token_param": "_odata_token",
            "header_name": "Authorization",
            "header_template": "Bearer {token}",
        }
    return {"type": "none"}


def _params_for_auth(auth: Sequence[Any] | None) -> dict[str, Any]:
    if not auth:
        return {}
    kind = auth[0].lower()
    if kind == "basic":
        # auth = ("basic", user, pass)
        return {"_odata_user": auth[1], "_odata_pass": auth[2]}
    if kind == "bearer":
        # auth = ("bearer", token)
        return {"_odata_token": auth[1]}
    return {}


def run_odata_stream(
    *,
    base_url: str,
    entity_set: str,
    version: str = "v2",
    auth: Sequence[Any] | None = None,
    filter_query: str | None = None,
    select_fields: str | None = None,
    top: int | None = None,
    sap_client: str | None = None,
    extra_headers: dict[str, str] | None = None,
    max_pages: int = 200,
) -> list[dict[str, Any]]:
    """Execute one OData entity-set read and return collected rows.

    See module docstring for the auth tuple shape and the v2/v4 split.
    Raises ``RuntimeError`` on HTTP failure (propagated from
    rest_framework's HTTPError wrapper).
    """
    if not base_url:
        raise ValueError("run_odata_stream: base_url is required.")
    if not entity_set:
        raise ValueError("run_odata_stream: entity_set is required.")

    version = (version or "v2").lower()
    if version not in ("v2", "v4"):
        raise ValueError(
            f"run_odata_stream: unknown OData version {version!r}; expected 'v2' or 'v4'."
        )

    # Version-specific response shape.
    if version == "v2":
        data_path = "d.results"
        next_url_path = "d.__next"
        default_query: dict[str, Any] = {"$format": "json"}
    else:
        data_path = "value"
        next_url_path = "@odata.nextLink"
        default_query = {}

    # Build optional query overrides.
    stream_query: dict[str, Any] = {}
    if filter_query:
        stream_query["$filter"] = filter_query
    if select_fields:
        stream_query["$select"] = select_fields
    if top is not None:
        stream_query["$top"] = str(int(top))

    # sap_client lives in default_query so it propagates if the manifest
    # ever multiplexes streams. Empty value → T1's empty-strip drops it.
    if sap_client:
        default_query["sap-client"] = sap_client

    # Compose ephemeral manifest.
    manifest = RestConnectorManifest.from_dict({
        "id": "_inline_odata",
        "name": "Inline OData",
        "base_url": base_url.rstrip("/"),
        "auth": _auth_block(auth),
        "default_query": default_query,
        "default_headers": {
            "Accept": "application/json",
            **(extra_headers or {}),
        },
        "streams": [{
            "name": entity_set,
            "path": f"/{entity_set.lstrip('/')}",
            "method": "GET",
            "query": stream_query,
            "data_path": data_path,
            "pagination": {
                "type": "url",
                "next_url_path": next_url_path,
                "max_pages": max_pages,
            },
        }],
    })

    params = _params_for_auth(auth)
    return _execute_stream(manifest, manifest.streams[0], params)


__all__ = ["run_odata_stream"]
