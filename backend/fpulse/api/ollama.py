"""
Ollama-side endpoints — status probe + model pull proxy.

Goal: let users go from "Ollama not configured" to "Ollama ready" without
ever leaving the F-Pulse browser tab. The frontend banner uses these to
replace the static `ollama pull llama3` shell instruction with a real button.

Endpoints:
  GET  /api/ai/ollama/status   probe localhost (or OLLAMA_URL) for running daemon + model list
  POST /api/ai/ollama/pull     stream Ollama's pull progress as NDJSON

Both endpoints are best-effort — if Ollama isn't running they degrade
cleanly. Status returns {running: false, models: []}; pull returns 502
with a JSON body the frontend can show in-line.
"""

from __future__ import annotations

import os
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from fpulse.auth.deps import current_user_optional

router = APIRouter(prefix="/api/ai/ollama", tags=["ai"])


# ---------------------------------------------------------------------------
# Tier-aware admin gate
#
# OSS (free): everyone can pull / delete Ollama models — local user, local
# machine, no gating.
#
# F-Pulse+: only admins can manage models. We detect "Plus is active" by
# looking for a license_manager in app_state. Plus middleware initializes
# it on startup; absent the Plus install, app_state has no entry → OSS
# behavior (open access). When Plus is loaded, anonymous + non-admin users
# get HTTP 403 with a clear message.
# ---------------------------------------------------------------------------


def _plus_is_active() -> bool:
    """True iff F-Pulse+ middleware is loaded for this process."""
    try:
        from fpulse.main import app_state
        return app_state.get("license_manager") is not None
    except Exception:
        return False


def _require_model_management(request: Request) -> None:
    """Gate Ollama model write operations.

    OSS: always allowed.
    Plus: requires admin or super_admin role. Anonymous and non-admin → 403.
    """
    if not _plus_is_active():
        return  # OSS — no gating

    user = current_user_optional(request)
    if user is None:
        raise HTTPException(
            status_code=403,
            detail="F-Pulse+ requires admin sign-in to manage Ollama models.",
        )
    role = (getattr(user, "role", "") or "").lower()
    if role not in ("super_admin", "admin"):
        raise HTTPException(
            status_code=403,
            detail=(
                "Only an admin can pull or remove Ollama models on F-Pulse+. "
                "Contact your workspace administrator."
            ),
        )


def _normalize_localhost(url: str) -> str:
    """Rewrite ``localhost`` host components to ``127.0.0.1``.

    Why this exists (2026-05-22): on Windows, ``localhost`` typically
    resolves to ``::1`` (IPv6) first. Ollama binds to ``127.0.0.1:11434``
    (IPv4) by default, so a v6 lookup gets connection-refused — and
    every Ollama management call (pull / status / delete) silently
    fails even though the Agent's own connection works (it uses the
    saved base_url which can happen to be either v4 or hostname).

    Normalizing at the resolver layer means we no longer care whether
    the saved config, env var, or default contains ``localhost`` vs
    ``127.0.0.1`` — both routes land on the IPv4 address that Ollama
    actually accepts.

    Only ``localhost`` is rewritten. Custom hostnames (e.g.
    ``ollama.internal``, ``ollama:11434`` in Docker) are left untouched
    so an operator's deliberate DNS routing isn't second-guessed.
    """
    if not url:
        return url
    # Both ``http://localhost`` and ``https://localhost``; the rewrite
    # is positional (right after the scheme) so we don't accidentally
    # replace ``localhost`` inside a path or query string.
    #
    # 2026-05-22: also catch IPv6 loopback ``[::1]`` and wildcard
    # ``0.0.0.0``. The IPv6 case bites Windows users whose saved
    # config got written by an autoprobe that picked ::1 before
    # Ollama bound to 127.0.0.1. The 0.0.0.0 case is a wildcard
    # bind address that's not a valid CLIENT-side host — connecting
    # to 0.0.0.0 has undefined behavior across OSes (Linux loops to
    # 127.0.0.1, Windows refuses), so rewriting to 127.0.0.1 is the
    # safe choice.
    bad_hosts = ("localhost", "[::1]", "0.0.0.0")
    for scheme in ("http://", "https://"):
        for bad in bad_hosts:
            prefix = scheme + bad
            if url.startswith(prefix):
                return scheme + "127.0.0.1" + url[len(prefix):]
    return url


def _ollama_url(request: Request | None = None) -> str:
    """Resolve Ollama's base URL with the correct precedence.

    Resolution order (high → low):
      1. ``app_state["ai_config_store"]`` — the user's or workspace's
         saved base_url for provider=ollama.
      2. ``OLLAMA_URL`` env var — operator override.
      3. ``http://127.0.0.1:11434`` — Windows-safe IPv4 default.

    Whatever URL wins is passed through ``_normalize_localhost`` so any
    legacy ``http://localhost:11434`` stored from earlier autoprobes is
    rewritten to ``http://127.0.0.1:11434`` before the management
    endpoints call out. See the helper docstring for the IPv6 rationale.
    """
    resolved: str | None = None

    # 1. Saved AI config
    if request is not None:
        try:
            from fpulse.main import app_state
            store = app_state.get("ai_config_store")
            user = current_user_optional(request)
            user_id = getattr(user, "id", None) if user else None
            ws_header = request.headers.get("X-Workspace-Id") or "default"
            if store is not None:
                cfg = store.resolve_active_config(
                    user_id=user_id, workspace_id=ws_header,
                )
                if cfg and (cfg.get("provider") or "").lower() == "ollama":
                    base = (cfg.get("base_url") or "").strip()
                    if base:
                        resolved = base
        except Exception:
            # AI config lookup is best-effort — fall through to env / default
            # rather than failing the management endpoint on a side path.
            pass

    # 2. Env override
    if resolved is None:
        env_url = (os.environ.get("OLLAMA_URL") or "").strip()
        if env_url:
            resolved = env_url

    # 3. IPv4 default
    if resolved is None:
        resolved = "http://127.0.0.1:11434"

    return _normalize_localhost(resolved).rstrip("/")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@router.get("/status")
async def ollama_status(request: Request) -> dict:
    """Probe Ollama at the resolved URL. Cheap (~10 ms when up, 2s timeout when not).

    Reads the caller's saved AI config to pick up a custom base_url if set;
    otherwise falls back to env / 127.0.0.1.
    """
    url = _ollama_url(request)
    # 2026-05-22: log the resolved URL so /pull-vs-/status mismatches are
    # diagnosable side-by-side in the server log.
    import logging as _logging
    _log = _logging.getLogger(__name__)
    _log.info("ollama_status: probing %s", url)
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        _log.warning(
            "ollama_status: probe failed at %s — %s: %s",
            url, type(e).__name__, str(e)[:200],
        )
        return {
            "running": False,
            "models": [],
            "url": url,
            "error": f"{type(e).__name__}",
        }

    models = []
    for m in data.get("models") or []:
        # Ollama returns rich metadata per model; surface only what the UI needs
        models.append({
            "name": m.get("name", ""),
            "size": m.get("size", 0),
            "modified_at": m.get("modified_at", ""),
        })
    return {
        "running": True,
        "models": models,
        "url": url,
    }


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------


class PullRequest(BaseModel):
    """Body for POST /api/ai/ollama/pull."""

    model: str = Field(..., description="Ollama model name, e.g. 'llama3' or 'llama3:8b'.")


@router.post("/pull")
async def ollama_pull(req: PullRequest, request: Request):
    """Stream Ollama's pull progress as NDJSON.

    Ollama's `/api/pull` returns a chunked stream of JSON objects, one per
    progress event. We proxy them through unchanged so the frontend sees
    the same {status, completed, total, digest} shape Ollama emits.

    Pull can take minutes for multi-GB models — this endpoint stays open
    until Ollama finishes or the client disconnects. No backend timeout.

    F-Pulse+ admin gate enforced before the proxy starts.
    """
    _require_model_management(request)
    url = _ollama_url(request)

    # Fast-fail if Ollama isn't reachable so the frontend can render a
    # useful error instead of waiting on a connect timeout mid-stream.
    #
    # 2026-05-22: bumped probe timeout 2s → 5s. Windows users were
    # hitting false 502s when Ollama had just started — the daemon's
    # first /api/tags response after boot can take 3-4s while it
    # enumerates the local model directory. 5s still feels instant
    # for a UI click and matches the embedder's availability probe
    # in ai/rag/embedder.py. We also log the failure server-side so
    # repeat 502s are diagnosable (the response body shows only the
    # exception class, not the message).
    import logging as _logging
    _log = _logging.getLogger(__name__)
    _log.info("ollama_pull: model=%r probing %s", req.model, url)
    try:
        async with httpx.AsyncClient(timeout=5.0) as probe:
            await probe.get(f"{url}/api/tags")
    except Exception as e:
        _log.warning(
            "ollama_pull: probe failed at %s — %s: %s",
            url, type(e).__name__, str(e)[:200],
        )
        return JSONResponse(
            status_code=502,
            content={
                "error": "ollama_unreachable",
                "message": (
                    "Ollama is not reachable at "
                    f"{url}. Install from https://ollama.com/download "
                    "and ensure the service is running. (If you just "
                    "started Ollama, give it a few seconds and try again.)"
                ),
                "exception": type(e).__name__,
                # 2026-05-22: surface the exception message text too so
                # the user can spot common causes ("Connection refused"
                # = Ollama not listening; "Name or service not known"
                # = DNS; "timed out" = firewall / wrong host).
                "exception_message": str(e)[:300],
                "resolved_url": url,
            },
        )

    async def stream() -> AsyncIterator[bytes]:
        # No top-level timeout — pulls can take a while.
        async with httpx.AsyncClient(timeout=None) as client:
            try:
                async with client.stream(
                    "POST",
                    f"{url}/api/pull",
                    json={"name": req.model, "stream": True},
                ) as r:
                    async for chunk in r.aiter_bytes():
                        yield chunk
            except httpx.HTTPError as e:
                # Emit one final NDJSON line so the frontend's stream parser
                # surfaces the error cleanly instead of just hanging.
                import json as _json
                yield (_json.dumps({
                    "status": "error",
                    "error": type(e).__name__,
                    "message": str(e)[:200],
                }) + "\n").encode("utf-8")

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@router.delete("/models/{model:path}")
async def ollama_delete(model: str, request: Request):
    """Delete a model from Ollama. Frees the disk space immediately.

    Path uses :path so model names containing ':' (e.g. 'llama3:8b') survive
    the URL parser without being split. Ollama itself accepts the qualified
    name in the request body.

    Returns:
      200 with {deleted: name} on success.
      403 on F-Pulse+ when caller is not an admin.
      404 if Ollama reports the model does not exist.
      502 if Ollama is unreachable.
    """
    _require_model_management(request)
    url = _ollama_url(request)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                "DELETE",
                f"{url}/api/delete",
                json={"name": model},
            )
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={
                "error": "ollama_unreachable",
                "message": f"Ollama is not reachable at {url}.",
                "exception": type(e).__name__,
            },
        )

    if resp.status_code == 404:
        return JSONResponse(
            status_code=404,
            content={"error": "model_not_found", "model": model},
        )
    if resp.status_code >= 400:
        # Surface Ollama's own error message verbatim — usually informative.
        body = resp.text[:500]
        return JSONResponse(
            status_code=502,
            content={"error": "ollama_error", "status": resp.status_code, "message": body},
        )
    return {"deleted": model}
