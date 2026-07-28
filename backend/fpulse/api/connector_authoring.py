"""Connector authoring API — Sprint C.

Endpoints:
  POST /api/connectors/author/from-openapi  — generate from OpenAPI spec
  POST /api/connectors/author/from-samples  — generate from sample payloads

Both return the generated manifest v2 + a validation report so the user can
see what's clean and what's flagged. The manifest is NOT saved to disk —
the caller decides whether to persist it. Saving is a separate endpoint
(future; out of scope for the launch demo).
"""

from __future__ import annotations

import logging
from typing import Any

import anyio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from fpulse.auth.deps import require_auth, require_min_rank
from fpulse.connectors.ai_authoring import (
    fetch_openapi_spec,
    generate_and_validate,
    parse_spec_text,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/connectors/author", tags=["connector-authoring"])


# ── Request / response schemas ────────────────────────────────────────


class FromOpenApiRequest(BaseModel):
    connector_id: str = Field(..., min_length=1, max_length=64)
    display_name: str | None = None
    category: str = "saas"
    # Provide exactly one of these three (precedence: spec > text > url):
    openapi_url: str | None = None     # a public URL the server fetches
    openapi_spec: dict | None = None   # an already-parsed spec dict
    openapi_text: str | None = None    # raw JSON/YAML pasted or uploaded


async def _resolve_spec(req: "FromOpenApiRequest") -> dict:
    """Turn a FromOpenApiRequest into a parsed spec dict.

    Precedence: an already-parsed ``openapi_spec`` wins; then pasted/uploaded
    ``openapi_text`` (parsed server-side so JSON *and* YAML work without a
    frontend YAML dep); then ``openapi_url`` (SSRF-hardened fetch). This is the
    single choke point both authoring endpoints share, so the paste/upload path
    and the URL path behave identically. Raises HTTPException(400) on any
    bad input so the caller can return it verbatim.
    """
    if req.openapi_spec is not None:
        spec = req.openapi_spec
    elif req.openapi_text:
        try:
            spec = parse_spec_text(req.openapi_text)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    elif req.openapi_url:
        try:
            spec = await anyio.to_thread.run_sync(fetch_openapi_spec, req.openapi_url)
        except Exception as exc:  # noqa: BLE001 — SSRF/parse/network all → 400
            logger.warning("fetch_openapi_spec failed: %s", exc)
            raise HTTPException(400, "failed to fetch spec") from exc
    else:
        raise HTTPException(400, "provide openapi_spec, openapi_text, or openapi_url")

    if not isinstance(spec, dict) or not spec.get("paths"):
        raise HTTPException(400, "invalid OpenAPI spec — missing 'paths'")
    return spec


class FromSamplesRequest(BaseModel):
    connector_id: str = Field(..., min_length=1, max_length=64)
    display_name: str | None = None
    category: str = "saas"
    base_url: str = ""
    stream_name: str | None = None
    samples: list[dict] = Field(..., min_length=1, max_length=5)


class AuthorResponse(BaseModel):
    manifest: dict
    validation: dict
    mode: str


# ── Endpoints ─────────────────────────────────────────────────────────


@router.post("/from-openapi", response_model=AuthorResponse)
async def author_from_openapi(req: FromOpenApiRequest) -> AuthorResponse:
    """Generate a v2 manifest from an OpenAPI 3.x spec.

    Provide `openapi_spec` (parsed dict), `openapi_text` (raw JSON/YAML you
    pasted or uploaded), or `openapi_url` (we fetch and parse). Precedence
    when more than one is present: spec > text > url.
    """
    spec = await _resolve_spec(req)

    try:
        result = generate_and_validate(
            spec, req.connector_id,
            mode="openapi",
            display_name=req.display_name,
            category=req.category,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("openapi → manifest generation failed")
        raise HTTPException(500, "generation failed") from exc

    return AuthorResponse(**result)


@router.post("/from-samples", response_model=AuthorResponse)
async def author_from_samples(req: FromSamplesRequest) -> AuthorResponse:
    """Generate a v2 manifest from 1-5 sample API responses."""
    try:
        result = generate_and_validate(
            req.samples, req.connector_id,
            mode="samples",
            display_name=req.display_name,
            category=req.category,
            base_url=req.base_url,
            stream_name=req.stream_name,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("samples → manifest generation failed")
        raise HTTPException(500, "generation failed") from exc

    return AuthorResponse(**result)


class RuntimeManifestResponse(BaseModel):
    manifest: dict
    mode: str = "runtime_v1"
    note: str = ""


@router.post("/from-openapi-runtime", response_model=RuntimeManifestResponse)
async def author_from_openapi_runtime(req: FromOpenApiRequest) -> RuntimeManifestResponse:
    """Generate a RUNTIME (v1) manifest from an OpenAPI 3 / Swagger 2 spec.

    ``/from-openapi`` emits the v2 *cert* spec (docs/certification); the
    runtime SaaS Connector node only LOADS v1 manifests
    (``rest_framework.load_manifests`` skips ``*.v2.json``), so a v2 draft
    isn't actually runnable. THIS emits the v1 manifest the node loads — save
    the returned JSON as ``fpulse/connectors/manifests/<id>.json`` and restart
    to use it. Returned for review (tier defaults to ``generated``): auth
    params, each stream's ``data_path``, and pagination usually need a human
    pass + a live test before shipping.

    Accepts `openapi_spec`, `openapi_text` (pasted/uploaded JSON or YAML), or
    `openapi_url` — same precedence as ``/from-openapi``.
    """
    spec = await _resolve_spec(req)

    try:
        from fpulse.connectors.openapi_import import manifest_from_openapi
        manifest = manifest_from_openapi(spec, connector_id=req.connector_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("openapi → runtime manifest generation failed")
        raise HTTPException(500, "generation failed") from exc

    n = len(manifest.get("streams", []))
    note = (
        f"Draft with {n} GET stream(s). Review the auth params + each stream's "
        f"data_path/pagination, then Save it as a Beta connector to use it in the "
        "SaaS Connector node (no restart needed)."
    )
    return RuntimeManifestResponse(manifest=manifest, note=note)


# ── Save / manage user-added connectors (admin/lead) ──────────────────
#
# Turns connector authoring into a real user privilege: an admin or lead saves
# a v1 runtime manifest and it persists in the writable user store + loads
# immediately as a Beta tile — no filesystem access, no restart. Everyone can
# then USE it; only admin/lead can add or remove.


class SaveManifestRequest(BaseModel):
    manifest: dict


@router.post("/save", dependencies=[Depends(require_min_rank("admin"))])
async def save_connector(req: SaveManifestRequest) -> dict[str, Any]:
    """Persist a v1 runtime manifest as a user-added **Beta** connector."""
    from fpulse.connectors import rest_framework as rf
    try:
        manifest = rf.save_user_manifest(req.manifest)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("save_user_manifest failed")
        raise HTTPException(500, "failed to save connector") from exc
    return {
        "saved": True,
        "id": manifest.id,
        "name": manifest.name,
        "tier": manifest.tier,
        "streams": len(manifest.streams),
    }


@router.get("/saved", dependencies=[Depends(require_auth)])
async def list_saved_connectors() -> dict[str, Any]:
    """List user-added (deletable) connectors. Readable by any signed-in user."""
    from fpulse.connectors import rest_framework as rf
    out = []
    for cid in sorted(rf.user_manifest_ids()):
        m = rf.get_manifest(cid)
        if m is not None:
            out.append({"id": m.id, "name": m.name, "tier": m.tier,
                        "category": m.category, "streams": len(m.streams)})
    return {"connectors": out, "count": len(out)}


@router.delete("/saved/{connector_id}", dependencies=[Depends(require_min_rank("admin"))])
async def delete_saved_connector(connector_id: str) -> dict[str, Any]:
    """Remove a user-added connector. 404 if it isn't a user-added one."""
    from fpulse.connectors import rest_framework as rf
    if not rf.delete_user_manifest(connector_id):
        raise HTTPException(404, "no such user-added connector")
    return {"deleted": True, "id": connector_id}
