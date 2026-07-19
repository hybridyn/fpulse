"""AI provider configuration endpoints.

Two tiers served by one router:
  * Free/OSS — any authenticated user configures their own provider
    via /api/ai/config/me. Stored per-user, encrypted at rest.
  * Plus     — admin configures a workspace-wide provider via
    /api/ai/config/workspace. Non-admins inherit unless the admin
    sets allow_user_override = True.

Secrets never leave the server as plaintext on GET — responses carry
``has_key: bool`` instead of the key itself. PUTs accept ``api_key``
with three meanings:
  * absent / None → keep existing key (user toggling fields)
  * ""            → clear the key
  * any string    → replace the key (encrypted on write)

/test is a dry-run — it validates connectivity to the configured
provider using the posted key (or the stored key if omitted) WITHOUT
persisting anything. This is the UX that the Settings "Test" button
hangs off.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from fpulse.auth.deps import (
    require_auth,
    require_admin,
    current_workspace_id,
)

router = APIRouter(prefix="/api/ai/config", tags=["ai-config"])


# ── Pydantic bodies ─────────────────────────────────────────────────────

# Provider literals kept as plain strings so the frontend can extend
# without touching the backend. Unknown providers are rejected at
# /test time, not on save — saving a typo shouldn't block the user
# from fixing it.

class UserConfigUpdate(BaseModel):
    enabled: bool = False
    provider: str = ""
    model: str = ""
    base_url: str = ""
    # None means "keep existing"; "" means "clear"; string replaces.
    api_key: str | None = None
    # v32: import the key from a `credentials` store row instead of an
    # inline key. Same tri-state as api_key (None=keep / ""=clear /
    # id=use). A non-empty reference clears the inline key server-side.
    credential_id: str | None = None


class WorkspaceConfigUpdate(BaseModel):
    enabled: bool = False
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str | None = None
    credential_id: str | None = None
    allow_user_override: bool = False
    monthly_budget_usd: float = 0.0


class TestRequest(BaseModel):
    """Probe a provider without persisting. If any field is omitted,
    fall back to the caller's stored config.
    """
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str | None = None
    scope: str = "user"  # "user" or "workspace"


# ── Helpers ─────────────────────────────────────────────────────────────

def _store():
    from fpulse.main import app_state
    store = app_state.get("ai_config_store")
    if store is None:
        raise HTTPException(503, "AI config store is not available")
    return store


def _audit(user, action: str, details: dict) -> None:
    """Write one row to the audit log. Best-effort — never raises."""
    try:
        from fpulse.main import app_state
        audit = app_state.get("audit_logger")
        if audit:
            audit.log(
                user_id=getattr(user, "id", "anonymous"),
                user_email=getattr(user, "email", "anonymous"),
                action=action,
                resource_type="ai_config",
                resource_id=details.get("resource_id", ""),
                details=details,
            )
    except Exception:
        pass


def _is_plus() -> bool:
    try:
        from fpulse.main import app_state
        lm = app_state.get("license_manager")
        return bool(lm and lm.is_plus)
    except Exception:
        return False


# ── User endpoints (Free/OSS tier) ──────────────────────────────────────

@router.get("/me")
async def get_my_config(
    request: Request,
    user=Depends(require_auth),
):
    """Current user's AI provider config. Never returns the key — just
    ``has_key: bool``. Also returns the tier + whether the admin has
    disabled per-user overrides, so the frontend can disable the form
    instead of silently letting the user save a row that will never
    be consulted.
    """
    ws_id = current_workspace_id(request)
    user_cfg = _store().get_user_config(user.id)

    # Read the workspace row (admin-configured) — strip the ciphertext.
    ws_cfg = _store().get_workspace_config(ws_id)
    override_allowed = True
    if _is_plus() and ws_cfg.get("enabled"):
        override_allowed = bool(ws_cfg.get("allow_user_override"))

    return {
        "user": user_cfg,
        "workspace_id": ws_id,
        "workspace_enabled": bool(ws_cfg.get("enabled")),
        "workspace_allows_override": override_allowed,
        "is_plus": _is_plus(),
    }


@router.put("/me")
async def update_my_config(
    body: UserConfigUpdate,
    request: Request,
    user=Depends(require_auth),
):
    """Save the current user's AI config.

    Refuses the write with 403 if Plus is active AND the admin has
    disabled per-user override — otherwise the UI would accept a save
    that is silently ignored at request time, which is worse than a
    clear error.
    """
    ws_id = current_workspace_id(request)
    if _is_plus():
        ws_cfg = _store().get_workspace_config(ws_id)
        if ws_cfg.get("enabled") and not ws_cfg.get("allow_user_override"):
            raise HTTPException(
                403,
                "Workspace admin has disabled per-user AI configuration. "
                "Use the workspace default or ask your admin to enable "
                "'Allow users to override'.",
            )

    updated = _store().upsert_user_config(
        user_id=user.id,
        workspace_id=ws_id,
        enabled=body.enabled,
        provider=body.provider,
        model=body.model,
        base_url=body.base_url,
        api_key=body.api_key,
        credential_id=body.credential_id,
    )
    _audit(
        user,
        "ai_config_user_updated",
        {
            "resource_id": user.id,
            "enabled": body.enabled,
            "provider": body.provider,
            "model": body.model,
            "workspace_id": ws_id,
            "key_changed": body.api_key is not None,
            "credential_ref": bool(body.credential_id),
        },
    )
    return {"user": updated}


@router.delete("/me")
async def delete_my_config(
    request: Request,
    user=Depends(require_auth),
):
    """Revert to workspace / env / stub defaults."""
    deleted = _store().delete_user_config(user.id)
    _audit(user, "ai_config_user_deleted", {"resource_id": user.id, "deleted": deleted})
    return {"deleted": deleted}


# ── Workspace endpoints (Plus tier, admin only) ─────────────────────────

@router.get("/workspace")
async def get_workspace_config(
    request: Request,
    user=Depends(require_admin),
):
    """Workspace-wide AI config. Admin-only.

    Always returns a row-shape (with has_key=False for unconfigured)
    so the admin form can bind cleanly.
    """
    ws_id = current_workspace_id(request)
    return {
        "workspace_id": ws_id,
        "workspace": _store().get_workspace_config(ws_id),
        "is_plus": _is_plus(),
    }


@router.put("/workspace")
async def update_workspace_config(
    body: WorkspaceConfigUpdate,
    request: Request,
    user=Depends(require_admin),
):
    """Save workspace-wide AI config. Admin-only.

    Requires an active Plus license — the endpoint is exposed on Free
    too so the UI can show a clear "upgrade to Plus" explanation, but
    the write itself is blocked.
    """
    if not _is_plus():
        raise HTTPException(
            402,  # Payment Required — signals upgrade needed
            "Workspace-wide AI configuration is a Plus feature. Users "
            "on the Free tier configure their own provider under "
            "Account → AI Provider.",
        )

    ws_id = current_workspace_id(request)
    updated = _store().upsert_workspace_config(
        workspace_id=ws_id,
        enabled=body.enabled,
        provider=body.provider,
        model=body.model,
        base_url=body.base_url,
        api_key=body.api_key,
        allow_user_override=body.allow_user_override,
        monthly_budget_usd=body.monthly_budget_usd,
        configured_by=user.id,
        credential_id=body.credential_id,
    )
    _audit(
        user,
        "ai_config_workspace_updated",
        {
            "resource_id": ws_id,
            "enabled": body.enabled,
            "provider": body.provider,
            "model": body.model,
            "allow_user_override": body.allow_user_override,
            "monthly_budget_usd": body.monthly_budget_usd,
            "key_changed": body.api_key is not None,
            "credential_ref": bool(body.credential_id),
        },
    )
    return {"workspace": updated}


@router.delete("/workspace")
async def delete_workspace_config(
    request: Request,
    user=Depends(require_admin),
):
    ws_id = current_workspace_id(request)
    deleted = _store().delete_workspace_config(ws_id)
    _audit(
        user,
        "ai_config_workspace_deleted",
        {"resource_id": ws_id, "deleted": deleted},
    )
    return {"deleted": deleted}


# ── Connection test (both tiers) ────────────────────────────────────────

@router.post("/test")
async def test_config(
    body: TestRequest,
    request: Request,
    user=Depends(require_auth),
):
    """Probe a provider without persisting the config.

    Uses the posted values first; for any field left blank, falls back
    to the caller's stored config (user scope) or the workspace config
    (workspace scope). This lets the UI pre-populate a "Test with
    saved settings" shortcut AND a free-form one.
    """
    store = _store()
    ws_id = current_workspace_id(request)

    # Resolve the effective probe config.
    provider = body.provider
    model = body.model
    base_url = body.base_url
    api_key = body.api_key

    if not provider or api_key is None:
        if body.scope == "workspace":
            if not _is_plus():
                raise HTTPException(402, "Workspace scope requires Plus")
            # Admins only for workspace probe — same gate as the real save.
            if user.role not in ("super_admin", "admin"):
                raise HTTPException(403, "Admin required for workspace test")
            resolved = store.resolve_active_config(
                user_id=None, workspace_id=ws_id
            )
        else:
            resolved = store.resolve_active_config(
                user_id=user.id, workspace_id=ws_id
            )
        if resolved:
            provider = provider or resolved.get("provider", "")
            model = model or resolved.get("model", "")
            base_url = base_url or resolved.get("base_url", "")
            if api_key is None:
                api_key = resolved.get("api_key", "")

    if not provider:
        raise HTTPException(400, "Provider is required")

    # SSRF: the probe POSTs api_key to base_url. A non-admin must not be able
    # to aim it at a private/metadata host and read the response.
    _probe_ssrf_guard(base_url, user)

    started = time.monotonic()
    try:
        ok, detail = await _probe_provider(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key or "",
        )
    except Exception as exc:
        return {
            "ok": False,
            "provider": provider,
            "model": model,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "detail": f"probe error: {exc}",
        }

    return {
        "ok": ok,
        "provider": provider,
        "model": model,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "detail": detail,
    }


# ── Provider probes ─────────────────────────────────────────────────────
# Kept inline rather than reaching into planner.ai_client so the test
# endpoint stays independent of any in-flight prompt / tool changes
# over there. A provider probe is a 1-shot minimal call; we don't want
# the pipeline-generation system prompt leaking into it.

def _probe_ssrf_guard(base_url: str, user) -> None:
    """SSRF egress guard for the /test probe (which POSTs the api_key to the
    caller-supplied base_url). Loopback (local services like Ollama) is allowed
    for everyone; any other private / link-local (cloud metadata) / reserved
    target requires admin, so a non-admin can't turn the probe into an
    internal-network reach. See security.ssrf.is_internal_host."""
    from fpulse.security.ssrf import is_internal_host
    if base_url and is_internal_host(base_url):
        if getattr(user, "role", None) not in ("super_admin", "admin"):
            raise HTTPException(
                403, "Testing a private/internal host is restricted to admins."
            )


async def _probe_provider(
    *, provider: str, model: str, base_url: str, api_key: str
) -> tuple[bool, str]:
    p = provider.lower().strip()
    if p == "claude" or p == "anthropic":
        return await _probe_claude(api_key=api_key, model=model or "claude-haiku-4-5-20251001")
    if p == "openai":
        return await _probe_openai(api_key=api_key, model=model or "gpt-4o-mini")
    if p == "ollama":
        return await _probe_ollama(base_url=base_url or "http://127.0.0.1:11434", model=model or "llama3")
    if p == "azure":
        return await _probe_azure(api_key=api_key, base_url=base_url, model=model)
    if p in ("gemini", "google"):
        return await _probe_gemini(api_key=api_key, model=model or "gemini-1.5-flash")
    if p == "groq":
        return await _probe_openai_compatible(
            api_key=api_key,
            base_url=base_url or "https://api.groq.com/openai/v1",
            model=model or "llama-3.1-8b-instant",
        )
    if p == "deepseek":
        return await _probe_openai_compatible(
            api_key=api_key,
            base_url=base_url or "https://api.deepseek.com",
            model=model or "deepseek-chat",
        )
    if p == "mistral":
        return await _probe_openai_compatible(
            api_key=api_key,
            base_url=base_url or "https://api.mistral.ai/v1",
            model=model or "mistral-small-latest",
        )
    if p == "openrouter":
        return await _probe_openrouter(
            api_key=api_key, model=model or "openai/gpt-4o-mini",
        )
    if p == "custom":
        if not base_url:
            return False, "custom provider requires base_url"
        return await _probe_openai_compatible(
            api_key=api_key, base_url=base_url, model=model or "custom"
        )
    return False, f"unknown provider: {provider}"


async def _probe_claude(*, api_key: str, model: str) -> tuple[bool, str]:
    if not api_key:
        return False, "api_key required"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
    if resp.status_code == 200:
        return True, f"model ok: {model}"
    return False, f"{resp.status_code} {resp.text[:200]}"


async def _probe_openai(*, api_key: str, model: str) -> tuple[bool, str]:
    if not api_key:
        return False, "api_key required"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
            },
        )
    if resp.status_code == 200:
        return True, f"model ok: {model}"
    return False, f"{resp.status_code} {resp.text[:200]}"


async def _probe_ollama(*, base_url: str, model: str) -> tuple[bool, str]:
    """Ollama is the local / zero-key path — we only check the server
    is reachable and the model is pulled.
    """
    url = base_url.rstrip("/") + "/api/tags"
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            resp = await client.get(url)
        except Exception as exc:
            return False, f"ollama unreachable at {base_url}: {exc}"
    if resp.status_code != 200:
        return False, f"ollama {resp.status_code}"
    data = resp.json()
    names = {m.get("name", "").split(":")[0] for m in data.get("models", [])}
    if model and model.split(":")[0] not in names and names:
        return False, f"model {model} not pulled. Available: {sorted(names)[:5]}"
    return True, f"ollama reachable; models: {len(names)}"


async def _probe_azure(*, api_key: str, base_url: str, model: str) -> tuple[bool, str]:
    if not api_key:
        return False, "api_key required"
    if not base_url:
        return False, "azure requires base_url (deployment endpoint)"
    # Azure deployments vary; we just confirm the endpoint responds.
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                base_url.rstrip("/"),
                headers={"api-key": api_key},
            )
        except Exception as exc:
            return False, f"unreachable: {exc}"
    # 401/403 still means the endpoint resolved — we just don't have
    # the right path. Any 2xx or 4xx means "server responded", which
    # is all this smoke-probe validates.
    if 200 <= resp.status_code < 500:
        return True, f"endpoint reachable ({resp.status_code})"
    return False, f"{resp.status_code}"


async def _probe_gemini(*, api_key: str, model: str) -> tuple[bool, str]:
    if not api_key:
        return False, "api_key required"
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={api_key}"
    )
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url,
            json={
                "contents": [{"parts": [{"text": "ping"}]}],
                "generationConfig": {"maxOutputTokens": 8},
            },
        )
    if resp.status_code == 200:
        return True, f"model ok: {model}"
    return False, f"{resp.status_code} {resp.text[:200]}"


async def _probe_openai_compatible(
    *, api_key: str, base_url: str, model: str
) -> tuple[bool, str]:
    """Covers Groq, DeepSeek, Mistral, and any custom OpenAI-schema
    endpoint. Identical call shape to ``_probe_openai`` with a
    configurable base URL.
    """
    if not api_key:
        return False, "api_key required"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
            },
        )
    if resp.status_code == 200:
        return True, f"model ok: {model}"
    return False, f"{resp.status_code} {resp.text[:200]}"


async def _probe_openrouter(*, api_key: str, model: str) -> tuple[bool, str]:
    """Mirrors the production OpenRouter call shape (HTTP-Referer +
    X-Title attribution headers) so a green test connection guarantees
    the agent path will work. A bare /chat/completions probe without
    these headers can succeed while production calls fail OpenRouter's
    leaderboards / rate-limit attribution.
    """
    if not api_key:
        return False, "api_key required"
    referer = os.environ.get("FPULSE_OPENROUTER_REFERER", "https://hybridyn.example/fpulse")
    title = os.environ.get("FPULSE_OPENROUTER_TITLE", "F-Pulse")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": referer,
                "X-Title": title,
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
            },
        )
    if resp.status_code == 200:
        return True, f"model ok: {model}"
    if resp.status_code == 401:
        return False, "401 invalid API key (check sk-or-… prefix)"
    if resp.status_code == 402:
        return False, "402 insufficient credits — free models still need a non-zero balance for fee waiver"
    if resp.status_code == 404:
        return False, f"404 model not found: {model} — check the id (org/model[:tag])"
    if resp.status_code == 429:
        # 429 can come from OpenRouter's account daily cap OR from the
        # upstream provider's own rate limit. Try to disambiguate using
        # the response body (OpenRouter wraps upstream errors with
        # `metadata.raw` set, daily-cap errors say "rate limit").
        body_lower = resp.text.lower()
        if ":free" in model.lower() or "free" in body_lower:
            hint = (
                f"Free model {model} is rate-limited right now. "
                "Two common causes: (1) OpenRouter's free-tier daily cap on your account "
                "(~50/day without credit, ~1000/day with $10+ on file at openrouter.ai/credits); "
                "(2) the upstream provider's per-minute limit on this specific model. "
                "Quickest workaround: pick a free model from a different upstream "
                "(e.g. deepseek/deepseek-chat-v3:free, qwen/qwen-2.5-72b-instruct:free, "
                "meta-llama/llama-3.3-70b-instruct:free) — they don't share quotas."
            )
        else:
            hint = (
                "Rate limited. Wait a minute and retry, or top up OpenRouter credit "
                "to raise your daily quota."
            )
        return False, f"429 {hint}"
    if resp.status_code >= 500:
        return False, f"{resp.status_code} OpenRouter or upstream temporarily unavailable — retry"
    return False, f"{resp.status_code} {resp.text[:200]}"
