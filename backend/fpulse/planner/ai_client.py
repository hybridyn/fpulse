"""
AI Client — Multi-provider LLM client for F-Pulse.

Supports: Claude (Anthropic), OpenAI, Ollama (local).
Auto-detects from environment variables, falls back to rule planner.

Local Ollama auto-detection: when no provider env vars are set, this module
probes ``http://localhost:11434`` once per 5 minutes. If reachable with at
least one model installed, ``resolve_provider`` returns ``("ollama", ...)``
implicitly — no env var or restart needed. This is the OSS-launch-default
posture per ``project_fpulse_ollama_first.md``.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# Local Ollama auto-detect cache
#
# Probed lazily on first resolve_provider() call when no provider env vars
# are set. Sync httpx with a short timeout — at most a 1.5s block on cold
# cache, hits are free. Re-probed every 5 min so a freshly-started Ollama
# is picked up without restarting F-Pulse.
# ---------------------------------------------------------------------------

_OLLAMA_AUTOPROBE_TTL_S = 300.0
_OLLAMA_AUTOPROBE_TIMEOUT_S = 1.5
# 2026-05-22: explicit IPv4 instead of `localhost`. On Windows, `localhost`
# resolves to ``::1`` (IPv6) first in many configurations, and Ollama binds
# to ``127.0.0.1`` (IPv4) by default — so the v6 lookup silently failed the
# autoprobe even when Ollama was up. The pull / status management endpoints
# in api/ollama.py landed the same fix on the same day.
_OLLAMA_AUTOPROBE_URL = "http://127.0.0.1:11434"

_ollama_autoprobe_cache: dict[str, Any] = {
    "checked_at": 0.0,
    "result": None,  # (url, model_name) tuple or None
}


def _autoprobe_local_ollama() -> tuple[str, str] | None:
    """Probe localhost:11434 for a running Ollama daemon with >=1 model.

    Returns ``(url, model_name)`` if reachable + has models, else ``None``.
    Cached for 5 minutes so repeated calls don't pay the timeout cost.

    Disabled entirely when ``FPULSE_DISABLE_OLLAMA_AUTOPROBE=1`` (or any
    truthy value) is set. Useful for users who don't want F-Pulse to
    silently re-attach to a running Ollama daemon they're using for
    something else, or who deliberately picked a cloud provider and don't
    want Ollama in the resolver chain at all.
    """
    if os.environ.get("FPULSE_DISABLE_OLLAMA_AUTOPROBE", "").strip().lower() in ("1", "true", "yes"):
        return None

    now = time.monotonic()
    if now - _ollama_autoprobe_cache["checked_at"] < _OLLAMA_AUTOPROBE_TTL_S:
        return _ollama_autoprobe_cache["result"]

    # Prefixes for tool-trained Ollama families (Llama 3.1+, Mistral Nemo,
    # Qwen 2.5+, Firefunction v2, Command-R+). When multiple models are
    # installed, prefer one of these so the agent gets full tool-use rather
    # than falling back to text-only on a phi3 / mistral classic / etc.
    _TOOL_CAPABLE_PREFIXES = (
        "llama3.1", "llama3.2", "llama3.3",
        "qwen2.5", "mistral-nemo", "firefunction", "command-r",
    )

    def _name_is_tool_capable(name: str) -> bool:
        head = (name or "").split(":", 1)[0].lower()
        return any(head == p or head.startswith(p) for p in _TOOL_CAPABLE_PREFIXES)

    result: tuple[str, str] | None = None
    try:
        with httpx.Client(timeout=_OLLAMA_AUTOPROBE_TIMEOUT_S) as client:
            resp = client.get(f"{_OLLAMA_AUTOPROBE_URL}/api/tags")
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("models") or []
            if models:
                # Prefer the first tool-capable model; fall back to the first
                # model if none qualify (text-only mode is still better than
                # nothing, and the frontend warns about it).
                names = [m.get("name", "") for m in models if m.get("name")]
                pick = next((n for n in names if _name_is_tool_capable(n)), names[0] if names else "")
                if pick:
                    result = (_OLLAMA_AUTOPROBE_URL, pick)
    except Exception:
        result = None

    _ollama_autoprobe_cache["checked_at"] = now
    _ollama_autoprobe_cache["result"] = result
    return result


def invalidate_ollama_autoprobe() -> None:
    """Force the next ``resolve_provider()`` call to re-probe Ollama.

    Called by the agent endpoint after a model pull completes so the new
    model is picked up immediately instead of waiting up to 5 min for the
    cache TTL. Also useful as a generic "AI provider config changed" hook.
    """
    _ollama_autoprobe_cache["checked_at"] = 0.0
    _ollama_autoprobe_cache["result"] = None


# Backwards-compat alias used by tests written against the earlier name.
reset_ollama_autoprobe_for_tests = invalidate_ollama_autoprobe


def resolve_provider(
    user_id: str | None = None,
    workspace_id: str | None = None,
) -> tuple[str, str, str, str]:
    """Resolve the active AI provider. Returns (provider, api_key, model, base_url).

    Resolution order:
      1. ``workspace_ai_config`` / ``user_ai_config`` via AIConfigStore
         (only consulted when ``user_id`` or ``workspace_id`` is provided —
         endpoints that have request context pass these in).
      2. Env vars: ANTHROPIC_API_KEY / OPENAI_API_KEY / OLLAMA_URL.
      3. ``("none", "", "", "")`` — caller falls back to stub mode.

    The 4th element ``base_url`` is used for Ollama / Azure / custom
    OpenAI-compatible endpoints. For Claude / OpenAI direct it is "".
    """
    if user_id or workspace_id:
        try:
            from fpulse.main import app_state
            store = app_state.get("ai_config_store")
            if store is not None:
                cfg = store.resolve_active_config(
                    user_id=user_id, workspace_id=workspace_id
                )
                if cfg and cfg.get("provider"):
                    return (
                        (cfg.get("provider") or "").lower(),
                        cfg.get("api_key", ""),
                        cfg.get("model", ""),
                        cfg.get("base_url", ""),
                    )
        except Exception:
            # Store lookup is best-effort — any failure degrades to env vars
            # rather than breaking every AI-using endpoint.
            pass

    # Env-var fallback — legacy behaviour, unchanged.
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        model = os.environ.get("AI_MODEL", "claude-sonnet-4-20250514")
        return ("claude", key, model, "")

    key = os.environ.get("OPENAI_API_KEY", "")
    if key:
        model = os.environ.get("AI_MODEL", "gpt-4o")
        return ("openai", key, model, "")

    # OpenRouter — unified gateway to 100+ models. Single key, namespaced
    # model ids (e.g. "openai/gpt-4o-mini", "anthropic/claude-sonnet-4").
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        model = os.environ.get("AI_MODEL", "openai/gpt-4o-mini")
        return ("openrouter", key, model, "")

    ollama_url = os.environ.get("OLLAMA_URL", "")
    if ollama_url:
        model = os.environ.get("AI_MODEL", "llama3")
        return ("ollama", "", model, ollama_url)

    # Last resort: local Ollama auto-detect. Cached probe; cheap on hit.
    auto = _autoprobe_local_ollama()
    if auto is not None:
        url, model = auto
        # Allow the user to override the auto-picked model via AI_MODEL even
        # in auto-detect mode.
        chosen_model = os.environ.get("AI_MODEL", model)
        return ("ollama", "", chosen_model, url)

    return ("none", "", "", "")


def policy_route(
    prompt_kind: str,
    *,
    user_id: str | None = None,
    workspace_id: str | None = None,
) -> tuple[str, str, str, str]:
    """Route to a local provider when prompt_kind warrants it.

    Returns the same 4-tuple shape as ``resolve_provider``. Behind the
    ``FPULSE_ENABLE_POLICY_ROUTE=1`` env var so it stays opt-in.

    When OFF this is a pass-through to ``resolve_provider``. When ON and
    ``prompt_kind`` is in {'code', 'tool_result', 'sensitive'}, we prefer a
    local provider (Ollama) so data samples don't silently leave the box for
    a cloud LLM. If no local provider is available, we fall back to the
    user's configured provider.

    Default: ON when the server is exposed (SECURITY_MODE=server) — an
    exposed deployment should keep sensitive data local by default — and
    OFF (opt-in via ``FPULSE_ENABLE_POLICY_ROUTE=1``) on a local box.
    """
    _flag = os.environ.get("FPULSE_ENABLE_POLICY_ROUTE", "").strip().lower()
    from fpulse import runtime_config
    _enabled = _flag in ("1", "true", "yes") or (_flag == "" and runtime_config.IS_SERVER_MODE)
    if not _enabled:
        return resolve_provider(user_id=user_id, workspace_id=workspace_id)

    sensitive_kinds = {"code", "tool_result", "sensitive"}
    if prompt_kind not in sensitive_kinds:
        return resolve_provider(user_id=user_id, workspace_id=workspace_id)

    # Try local Ollama first
    ollama_url = os.environ.get("OLLAMA_URL", "")
    if ollama_url:
        model = os.environ.get("AI_MODEL", "llama3")
        return ("ollama", "", model, ollama_url)

    auto = _autoprobe_local_ollama()
    if auto is not None:
        url, model = auto
        chosen_model = os.environ.get("AI_MODEL", model)
        return ("ollama", "", chosen_model, url)

    # No local provider — fall back to user's config
    return resolve_provider(user_id=user_id, workspace_id=workspace_id)


def _get_provider() -> tuple[str, str, str]:
    """Legacy 3-tuple shim for call sites that don't thread user/workspace
    context. Same resolution order via ``resolve_provider`` but collapses
    the Ollama case back to the old (provider, url, model) shape.
    """
    provider, api_key, model, base_url = resolve_provider()
    if provider == "ollama":
        return (provider, base_url, model)
    return (provider, api_key, model)


# Providers that speak OpenAI's Chat Completions wire format. Listing a default
# base here lets a user pick any of them with just provider + api_key + model —
# no per-provider client, no hardcoded allowlist. This is what makes F-Pulse
# honor "use whatever model the user chooses" for the long tail of providers.
# An explicit base_url from config always wins over these (custom / self-hosted
# endpoints — vLLM, LM Studio, llama.cpp, LocalAI, or a gateway).
OPENAI_COMPATIBLE_BASES = {
    "deepseek": "https://api.deepseek.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "together": "https://api.together.xyz/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "xai": "https://api.x.ai/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "perplexity": "https://api.perplexity.ai",
    "nvidia": "https://integrate.api.nvidia.com/v1",
}


def openai_compatible_base(provider: str, base_url: str | None) -> str:
    """Resolve the API root for an OpenAI-compatible provider.

    An explicit ``base_url`` (a custom or self-hosted OpenAI-compatible server)
    always wins. Failing that, fall back to the known public default for a
    named cloud provider. Returns "" when neither is available (caller
    surfaces a clear error rather than silently doing nothing).
    """
    b = (base_url or "").strip()
    if b:
        return b
    return OPENAI_COMPATIBLE_BASES.get((provider or "").lower().strip(), "")


def _chat_completions_url(base: str) -> str:
    """Normalize an API root to a chat/completions URL. Accepts a bare root
    (``https://host/v1``) or a full endpoint already ending in
    ``/chat/completions`` and returns the full endpoint either way."""
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


SYSTEM_PROMPT = """You are F-Pulse Pipeline Architect, an AI that converts natural language into data pipeline definitions.

You MUST respond with valid JSON only. No markdown, no explanation, no code fences.

The JSON must have this exact structure:
{
  "name": "Pipeline Name",
  "description": "Brief description",
  "steps": [
    {
      "type": "<step_type>",
      "label": "Human-readable label",
      "params": { ... },
      "position": {"x": <number>, "y": 100}
    }
  ],
  "connections": [
    {"from_step": 0, "to_step": 1}
  ],
  "explanation": "Brief explanation of what this pipeline does"
}

IMPORTANT: In "connections", use the array INDEX (0-based) of the step, not the step id.

Available step types and their params:

SOURCES:
- csv_source: {"file_path": "filename.csv", "delimiter": ",", "header": true}
- db_source: {"query": "SELECT * FROM table_name"}
- api_source: {"url": "https://...", "method": "GET", "headers": {}}

ROW TRANSFORMS:
- filter: {"condition": "SQL WHERE clause, e.g. amount > 100"}
- transform: {"expression": "SELECT *, expression AS new_col FROM source_table"}
- deduplicate: {"key": ["col1", "col2"], "strategy": "keep_first"}
- sort: {"columns": ["col1"], "ascending": [true]}
- rename: {"mappings": {"old_name": "new_name"}}
- typecast: {"casts": {"column_name": "INTEGER"}}
- derived_column: {"columns": [{"name": "new_col", "expression": "col1 + col2"}]}

SET TRANSFORMS:
- aggregate: {"group_by": ["col"], "functions": [{"column": "amount", "function": "SUM", "alias": "total"}]}
- join: {"join_type": "inner", "join_key": "id"}
- lookup: {"lookup_key": "id", "return_columns": ["name"]}
- union: {"union_type": "all"}
- pivot: {"index_column": "date", "pivot_column": "category", "value_column": "amount", "agg_function": "SUM"}
- unpivot: {"id_columns": ["id"], "value_columns": ["col1", "col2"], "var_name": "variable", "val_name": "value"}
- window: {"function": "ROW_NUMBER", "partition_by": ["category"], "order_by": ["date"], "alias": "row_num"}

QUALITY:
- sample: {"method": "first", "count": 100}
- validate: {"rules": [{"column": "email", "rule": "not_null"}]}
- conditional_split: {"branches": [{"name": "high_value", "condition": "amount > 1000"}]}

OUTPUTS:
- output: {"format": "parquet", "file_path": "output.parquet"}
- db_sink: {"table_name": "target_table", "write_mode": "append", "connection_string": ""}

Layout rules:
- Position steps left-to-right: first step x=0, then x=350, x=700, x=1050, etc.
- All steps y=100
- Connect steps sequentially unless the pipeline has branches/joins.

If the user's request is unclear or not about data pipelines, respond with:
{"name": "", "steps": [], "connections": [], "explanation": "I can help you build data pipelines. Try: 'Load sales.csv, filter amount > 100, aggregate by category, output to parquet'"}
"""


def _audit_ai_call(
    *,
    user_id: str | None,
    workspace_id: str | None,
    provider: str,
    model: str,
    source: str,
    latency_ms: int,
    tokens_in: int,
    tokens_out: int,
    success: bool,
    error: str | None = None,
) -> None:
    """Write one row to audit_log per AI call. Best-effort — never raises.

    Memory-conscious by design: writes a small dict straight to SQLite via
    the shared audit logger (no buffering, no in-memory queue). If the
    logger isn't available (tests, early boot) we silently skip — an AI
    call shouldn't fail because telemetry failed.
    """
    try:
        from fpulse.main import app_state
        audit = app_state.get("audit_logger")
        if audit is None:
            return
        audit.log(
            user_id=user_id or "anonymous",
            user_email=user_id or "anonymous",  # plainer helpers don't have email
            action="ai_call" if success else "ai_call_failed",
            resource_type="ai",
            resource_id=provider,
            details={
                "provider": provider,
                "model": model,
                "source": source,
                "latency_ms": latency_ms,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "workspace_id": workspace_id or "",
                "success": success,
                "error": error[:500] if error else None,
            },
        )
    except Exception:
        # Audit is best-effort — never break the AI path because telemetry
        # wrote slowly or the logger wasn't ready.
        pass


async def ai_generate_pipeline(
    messages: list[dict[str, str]],
    *,
    user_id: str | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any] | None:
    """Call LLM to generate a pipeline IR from chat messages.

    ``user_id`` / ``workspace_id`` are optional. When present, the AI
    config store is consulted first (per-user on Free, workspace-wide
    on Plus). When absent, env vars are used — preserving the legacy
    zero-context call path.

    Every call — success OR failure — is written to audit_log with
    (provider, model, latency_ms, tokens_in, tokens_out, success). The
    audit row is a single dict, small, no buffering — memory cost is a
    few hundred bytes per call, released as soon as SQLite commits.

    Returns parsed pipeline dict or None if AI is not available.
    """
    provider, api_key, model, base_url = resolve_provider(
        user_id=user_id, workspace_id=workspace_id
    )

    if provider == "none":
        return None

    t0 = time.monotonic()
    result: dict | None = None
    usage: dict = {"input": 0, "output": 0}
    err: str | None = None
    try:
        if provider == "claude":
            result, usage = await _call_claude(api_key, model, messages)
        elif provider == "openai":
            result, usage = await _call_openai(api_key, model, messages)
        elif provider == "openrouter":
            # OpenRouter speaks OpenAI's chat-completions shape — same
            # request/response handling, different base URL and headers.
            result, usage = await _call_openrouter(api_key, model, messages)
        elif provider == "ollama":
            # Ollama: base_url can live in api_key (legacy env-var path)
            # or base_url (new store-resolved path). Accept either.
            result, usage = await _call_ollama(base_url or api_key, model, messages)
        else:
            # Any OTHER provider the user configured — DeepSeek, Groq, Mistral,
            # Moonshot/Kimi, Together, xAI, or a custom OpenAI-compatible
            # endpoint. Honor the choice via the OpenAI wire format instead of
            # silently dropping it (the old behaviour handled only the four
            # named providers above).
            api_base = openai_compatible_base(provider, base_url)
            if api_base:
                result, usage = await _call_openai_compatible(api_base, api_key, model, messages)
            else:
                err = (
                    f"provider {provider!r} is not OpenAI-compatible by default; "
                    "set a base_url to point at its OpenAI-compatible endpoint"
                )
                print(f"[F-Pulse AI] {err}")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"[F-Pulse AI] {provider} error: {e}")

    latency_ms = int((time.monotonic() - t0) * 1000)
    _audit_ai_call(
        user_id=user_id,
        workspace_id=workspace_id,
        provider=provider,
        model=model,
        source="planner.ai_generate_pipeline",
        latency_ms=latency_ms,
        tokens_in=int(usage.get("input", 0)),
        tokens_out=int(usage.get("output", 0)),
        success=result is not None,
        error=err,
    )
    return result


async def _call_claude(api_key: str, model: str, messages: list[dict]) -> tuple[dict | None, dict]:
    """Call Anthropic Claude API. Returns (parsed, {input, output}).

    Claude responses carry `usage.input_tokens` / `usage.output_tokens`
    which we surface so `_audit_ai_call` can record real token counts
    instead of zeroes.
    """
    user_messages = [{"role": m["role"], "content": m["content"]} for m in messages]

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 2048,
                "system": SYSTEM_PROMPT,
                "messages": user_messages,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"]
        usage_raw = data.get("usage") or {}
        usage = {"input": usage_raw.get("input_tokens", 0), "output": usage_raw.get("output_tokens", 0)}
        return _parse_json_response(text), usage


async def _call_openai(api_key: str, model: str, messages: list[dict]) -> tuple[dict | None, dict]:
    """Call OpenAI API. Returns (parsed, {input, output})."""
    oai_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in messages:
        oai_messages.append({"role": m["role"], "content": m["content"]})

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": oai_messages,
                "temperature": 0.3,
                "max_tokens": 2048,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage_raw = data.get("usage") or {}
        usage = {"input": usage_raw.get("prompt_tokens", 0), "output": usage_raw.get("completion_tokens", 0)}
        return _parse_json_response(text), usage


async def _call_openrouter(api_key: str, model: str, messages: list[dict]) -> tuple[dict | None, dict]:
    """Call OpenRouter (OpenAI-compatible Chat Completions). Returns (parsed, usage).

    Same shape as `_call_openai` — OpenRouter normalizes responses to
    OpenAI's chat.completions schema. Differences are the base URL and the
    attribution headers (HTTP-Referer + X-Title) used for OpenRouter's
    leaderboards.
    """
    oai_messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in messages:
        oai_messages.append({"role": m["role"], "content": m["content"]})

    referer = os.environ.get("FPULSE_OPENROUTER_REFERER", "https://hybridyn.example/fpulse")
    title = os.environ.get("FPULSE_OPENROUTER_TITLE", "F-Pulse")

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": referer,
                "X-Title": title,
            },
            json={
                "model": model or "openai/gpt-4o-mini",
                "messages": oai_messages,
                "temperature": 0.3,
                "max_tokens": 2048,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage_raw = data.get("usage") or {}
        usage = {"input": usage_raw.get("prompt_tokens", 0), "output": usage_raw.get("completion_tokens", 0)}
        return _parse_json_response(text), usage


async def _call_ollama(base_url: str, model: str, messages: list[dict]) -> tuple[dict | None, dict]:
    """Call Ollama local API. Returns (parsed, {input, output}).

    Ollama exposes `prompt_eval_count` (input) and `eval_count` (output)
    on non-streaming responses. When absent (older Ollama builds) we fall
    back to zero — still records a row, just without token detail.
    """
    ollama_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in messages:
        ollama_messages.append({"role": m["role"], "content": m["content"]})

    url = base_url.rstrip("/") + "/api/chat"

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            url,
            json={
                "model": model,
                "messages": ollama_messages,
                "stream": False,
                "options": {"temperature": 0.3},
                "keep_alive": "24h",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["message"]["content"]
        usage = {"input": data.get("prompt_eval_count", 0), "output": data.get("eval_count", 0)}
        return _parse_json_response(text), usage


async def _call_openai_compatible(base_url: str, api_key: str, model: str, messages: list[dict]) -> tuple[dict | None, dict]:
    """Call any OpenAI-compatible Chat Completions endpoint (pipeline JSON).

    Same wire format as ``_call_openai`` but the base URL + model are whatever
    the user configured — DeepSeek, Groq, Mistral, Moonshot/Kimi, Together,
    xAI, or a self-hosted server (vLLM / LM Studio / llama.cpp / LocalAI).
    ``api_key`` is optional: local servers often need none.
    """
    oai_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in messages:
        oai_messages.append({"role": m["role"], "content": m["content"]})

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            _chat_completions_url(base_url),
            headers=headers,
            json={
                "model": model,
                "messages": oai_messages,
                "temperature": 0.3,
                "max_tokens": 2048,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage_raw = data.get("usage") or {}
        usage = {"input": usage_raw.get("prompt_tokens", 0), "output": usage_raw.get("completion_tokens", 0)}
        return _parse_json_response(text), usage


def _parse_json_response(text: str) -> dict | None:
    """Extract and parse JSON from LLM response."""
    text = text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (fences)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
    return None


async def ai_generate_json(
    messages: list[dict[str, str]],
    *,
    system_prompt: str,
    source_label: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any] | None:
    """Generic LLM JSON-completion against the resolved provider.

    Used by AI features that want structured output but don't fit the
    pipeline-specific system prompt of `ai_generate_pipeline`. Honors the
    same audit + provider-resolution path so every LLM call is traceable.

    Returns parsed dict, or None when no provider is configured / call
    fails / response was unparseable. Callers MUST treat None as the
    "use deterministic fallback" signal.
    """
    provider, api_key, model, base_url = resolve_provider(
        user_id=user_id, workspace_id=workspace_id
    )
    if provider == "none":
        return None

    t0 = time.monotonic()
    result: dict | None = None
    usage: dict = {"input": 0, "output": 0}
    err: str | None = None
    try:
        if provider == "claude":
            result, usage = await _call_text_claude(api_key, model, system_prompt, messages)
        elif provider == "openai":
            result, usage = await _call_text_openai(api_key, model, system_prompt, messages)
        elif provider == "openrouter":
            result, usage = await _call_text_openrouter(api_key, model, system_prompt, messages)
        elif provider == "ollama":
            result, usage = await _call_text_ollama(base_url or api_key, model, system_prompt, messages)
        else:
            api_base = openai_compatible_base(provider, base_url)
            if api_base:
                result, usage = await _call_text_openai_compatible(api_base, api_key, model, system_prompt, messages)
            else:
                err = f"provider {provider!r} needs an OpenAI-compatible base_url"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    latency_ms = int((time.monotonic() - t0) * 1000)
    _audit_ai_call(
        user_id=user_id,
        workspace_id=workspace_id,
        provider=provider,
        model=model,
        source=source_label,
        latency_ms=latency_ms,
        tokens_in=int(usage.get("input", 0)),
        tokens_out=int(usage.get("output", 0)),
        success=result is not None,
        error=err,
    )
    return result


async def _call_text_claude(api_key, model, system_prompt, messages):
    user_messages = [{"role": m["role"], "content": m["content"]} for m in messages]
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "system": system_prompt,
                "messages": user_messages,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"]
        usage_raw = data.get("usage") or {}
        return _parse_json_response(text), {
            "input": usage_raw.get("input_tokens", 0),
            "output": usage_raw.get("output_tokens", 0),
        }


async def _call_text_openai(api_key, model, system_prompt, messages):
    oai_messages = [{"role": "system", "content": system_prompt}]
    for m in messages:
        oai_messages.append({"role": m["role"], "content": m["content"]})
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": oai_messages,
                "temperature": 0.2,
                "max_tokens": 1024,
                "store": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage_raw = data.get("usage") or {}
        return _parse_json_response(text), {
            "input": usage_raw.get("prompt_tokens", 0),
            "output": usage_raw.get("completion_tokens", 0),
        }


async def _call_text_openrouter(api_key, model, system_prompt, messages):
    """Generic JSON-text call via OpenRouter. Same shape as _call_text_openai."""
    oai_messages = [{"role": "system", "content": system_prompt}]
    for m in messages:
        oai_messages.append({"role": m["role"], "content": m["content"]})
    referer = os.environ.get("FPULSE_OPENROUTER_REFERER", "https://hybridyn.example/fpulse")
    title = os.environ.get("FPULSE_OPENROUTER_TITLE", "F-Pulse")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": referer,
                "X-Title": title,
            },
            json={
                "model": model or "openai/gpt-4o-mini",
                "messages": oai_messages,
                "temperature": 0.2,
                "max_tokens": 1024,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage_raw = data.get("usage") or {}
        return _parse_json_response(text), {
            "input": usage_raw.get("prompt_tokens", 0),
            "output": usage_raw.get("completion_tokens", 0),
        }


async def _call_text_ollama(base_url, model, system_prompt, messages):
    ollama_messages = [{"role": "system", "content": system_prompt}]
    for m in messages:
        ollama_messages.append({"role": m["role"], "content": m["content"]})
    url = base_url.rstrip("/") + "/api/chat"
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            url,
            json={
                "model": model,
                "messages": ollama_messages,
                "stream": False,
                "options": {"temperature": 0.2},
                "keep_alive": "24h",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["message"]["content"]
        return _parse_json_response(text), {
            "input": data.get("prompt_eval_count", 0),
            "output": data.get("eval_count", 0),
        }


async def _call_text_openai_compatible(base_url, api_key, model, system_prompt, messages):
    """Generic JSON-text call via any OpenAI-compatible endpoint. Mirrors
    ``_call_text_openai`` with a configurable base + optional api_key."""
    oai_messages = [{"role": "system", "content": system_prompt}]
    for m in messages:
        oai_messages.append({"role": m["role"], "content": m["content"]})
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            _chat_completions_url(base_url),
            headers=headers,
            json={
                "model": model,
                "messages": oai_messages,
                "temperature": 0.2,
                "max_tokens": 1024,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage_raw = data.get("usage") or {}
        return _parse_json_response(text), {
            "input": usage_raw.get("prompt_tokens", 0),
            "output": usage_raw.get("completion_tokens", 0),
        }


def is_ai_available() -> bool:
    """Check if any AI provider is configured."""
    provider, _, _ = _get_provider()
    return provider != "none"


async def ai_generate_text(
    messages: list[dict[str, str]],
    *,
    system_prompt: str,
    source_label: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    max_tokens: int = 1024,
) -> str | None:
    """Generic LLM plain-text completion against the resolved provider.

    Unlike `ai_generate_json`, this returns the raw assistant text — used
    by the Assistant chat surface (canvas Q&A) where the model is
    expected to reply in natural language, not JSON. Honors the same
    audit + provider-resolution path so every LLM call is traceable.

    Returns the reply string, or None when no provider is configured /
    the call fails. Callers MUST treat None as the "use deterministic
    fallback" signal.
    """
    provider, api_key, model, base_url = resolve_provider(
        user_id=user_id, workspace_id=workspace_id
    )
    if provider == "none":
        return None

    t0 = time.monotonic()
    text: str | None = None
    usage: dict = {"input": 0, "output": 0}
    err: str | None = None
    try:
        if provider == "claude":
            text, usage = await _call_plain_claude(api_key, model, system_prompt, messages, max_tokens)
        elif provider == "openai":
            text, usage = await _call_plain_openai(api_key, model, system_prompt, messages, max_tokens)
        elif provider == "openrouter":
            text, usage = await _call_plain_openrouter(api_key, model, system_prompt, messages, max_tokens)
        elif provider == "ollama":
            text, usage = await _call_plain_ollama(base_url or api_key, model, system_prompt, messages, max_tokens)
        else:
            api_base = openai_compatible_base(provider, base_url)
            if api_base:
                text, usage = await _call_plain_openai_compatible(api_base, api_key, model, system_prompt, messages, max_tokens)
            else:
                err = f"provider {provider!r} needs an OpenAI-compatible base_url"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    latency_ms = int((time.monotonic() - t0) * 1000)
    _audit_ai_call(
        user_id=user_id,
        workspace_id=workspace_id,
        provider=provider,
        model=model,
        source=source_label,
        latency_ms=latency_ms,
        tokens_in=int(usage.get("input", 0)),
        tokens_out=int(usage.get("output", 0)),
        success=text is not None,
        error=err,
    )
    return text


# Plain-text variants of the provider calls. They mirror `_call_text_*`
# but DO NOT pass the response through `_parse_json_response` — the
# Assistant Q&A surface wants prose, not parsed JSON.

async def _call_plain_claude(api_key, model, system_prompt, messages, max_tokens):
    user_messages = [{"role": m["role"], "content": m["content"]} for m in messages]
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": user_messages,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"]
        usage_raw = data.get("usage") or {}
        return text, {
            "input": usage_raw.get("input_tokens", 0),
            "output": usage_raw.get("output_tokens", 0),
        }


async def _call_plain_openai(api_key, model, system_prompt, messages, max_tokens):
    oai_messages = [{"role": "system", "content": system_prompt}]
    for m in messages:
        oai_messages.append({"role": m["role"], "content": m["content"]})
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": oai_messages,
                "temperature": 0.4,
                "max_tokens": max_tokens,
                "store": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage_raw = data.get("usage") or {}
        return text, {
            "input": usage_raw.get("prompt_tokens", 0),
            "output": usage_raw.get("completion_tokens", 0),
        }


async def _call_plain_openrouter(api_key, model, system_prompt, messages, max_tokens):
    oai_messages = [{"role": "system", "content": system_prompt}]
    for m in messages:
        oai_messages.append({"role": m["role"], "content": m["content"]})
    referer = os.environ.get("FPULSE_OPENROUTER_REFERER", "https://hybridyn.example/fpulse")
    title = os.environ.get("FPULSE_OPENROUTER_TITLE", "F-Pulse")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": referer,
                "X-Title": title,
            },
            json={
                "model": model or "openai/gpt-4o-mini",
                "messages": oai_messages,
                "temperature": 0.4,
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage_raw = data.get("usage") or {}
        return text, {
            "input": usage_raw.get("prompt_tokens", 0),
            "output": usage_raw.get("completion_tokens", 0),
        }


async def _call_plain_ollama(base_url, model, system_prompt, messages, max_tokens):
    ollama_messages = [{"role": "system", "content": system_prompt}]
    for m in messages:
        ollama_messages.append({"role": m["role"], "content": m["content"]})
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": ollama_messages,
                "stream": False,
                "options": {"temperature": 0.4, "num_predict": max_tokens},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data.get("message", {}).get("content") or ""
        return text, {
            "input": data.get("prompt_eval_count", 0),
            "output": data.get("eval_count", 0),
        }


async def _call_plain_openai_compatible(base_url, api_key, model, system_prompt, messages, max_tokens):
    """Generic plain-text call via any OpenAI-compatible endpoint. Mirrors
    ``_call_plain_openai`` with a configurable base + optional api_key."""
    oai_messages = [{"role": "system", "content": system_prompt}]
    for m in messages:
        oai_messages.append({"role": m["role"], "content": m["content"]})
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            _chat_completions_url(base_url),
            headers=headers,
            json={
                "model": model,
                "messages": oai_messages,
                "temperature": 0.4,
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage_raw = data.get("usage") or {}
        return text, {
            "input": usage_raw.get("prompt_tokens", 0),
            "output": usage_raw.get("completion_tokens", 0),
        }
