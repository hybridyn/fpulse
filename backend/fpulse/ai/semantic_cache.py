"""
Semantic-ish LLM response cache for deterministic prompts.

Bifrost-inspired pattern: cache the full LLM response for a SHA-256 hash of
(system_prompt + user_intent + tool schema digest), tenant-scoped, TTL'd.
A cache hit serves the exact prior response with zero token + latency cost.

Scope discipline:
  - ONLY caches when temperature is implicitly low (we don't know temp from
    the LLMResponse, but our agent always uses low temp for tool-use).
  - Tenant key prefix is non-negotiable. A cache hit MUST come from the same
    workspace + user_role context that produced it. Keeps multi-tenant
    isolation intact.
  - Tool calls are NOT cached. If the LLM returned tool_uses, the cache is
    bypassed because tool execution is side-effectful and runs need fresh
    audit trails.
  - The cache is in-process LRU + TTL — survives nothing except the current
    Python process. Designed for "same user asks same thing twice in a
    debugging session" not "global content delivery."

Wire-up:
  Replace direct `await llm_client.call(...)` in AgentRunner with
  `cached_call(llm_client, ...)`. Backwards-compatible — when the cache
  doesn't hit, behavior is identical to a direct call.

Disable via FPULSE_DISABLE_SEMANTIC_CACHE=1 if anything looks weird.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from fpulse.ai.agent import LLMResponse


# ---------------------------------------------------------------------------
# Cache parameters — env-tunable. Defaults sized for "single-user dev box."
# ---------------------------------------------------------------------------

_DEFAULT_TTL_S = 30 * 60   # 30 minutes
_DEFAULT_MAX_ENTRIES = 256


def _ttl_seconds() -> int:
    raw = os.environ.get("FPULSE_SEMANTIC_CACHE_TTL_S", "").strip()
    if not raw:
        return _DEFAULT_TTL_S
    try:
        v = int(raw)
        return max(0, min(v, 24 * 3600))
    except ValueError:
        return _DEFAULT_TTL_S


def _max_entries() -> int:
    raw = os.environ.get("FPULSE_SEMANTIC_CACHE_MAX", "").strip()
    if not raw:
        return _DEFAULT_MAX_ENTRIES
    try:
        v = int(raw)
        return max(8, min(v, 4096))
    except ValueError:
        return _DEFAULT_MAX_ENTRIES


def _is_disabled() -> bool:
    return os.environ.get("FPULSE_DISABLE_SEMANTIC_CACHE", "").strip().lower() in (
        "1", "true", "yes",
    )


# ---------------------------------------------------------------------------
# In-process LRU + TTL store. Module-level so the same Python process shares
# entries across endpoint requests.
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    response: LLMResponse
    inserted_at: float
    hits: int = 0


_store: "OrderedDict[str, _CacheEntry]" = OrderedDict()


@dataclass
class CacheStats:
    """Returned by ``stats()``. Useful for the /trust audit panel."""
    entries: int
    hits: int
    misses: int
    inserts: int
    evictions: int


_metrics = {"hits": 0, "misses": 0, "inserts": 0, "evictions": 0}


def stats() -> CacheStats:
    return CacheStats(
        entries=len(_store),
        hits=_metrics["hits"],
        misses=_metrics["misses"],
        inserts=_metrics["inserts"],
        evictions=_metrics["evictions"],
    )


def clear() -> None:
    _store.clear()
    for k in list(_metrics):
        _metrics[k] = 0


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _digest_tools(tools: list[dict[str, Any]]) -> str:
    """Stable hash of the tool schemas — avoids cache hits when the
    available tool set has changed (which would change semantics)."""
    if not tools:
        return "no-tools"
    canon = sorted(
        ((t.get("name", ""), t.get("description", "")[:80]) for t in tools),
        key=lambda x: x[0],
    )
    blob = json.dumps(canon, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _last_user_message(messages: list[dict[str, Any]]) -> str:
    """Pull the last user message text. Earlier turns are left out of the
    cache key — caching multi-turn conversations would explode the keyspace
    and mostly miss anyway."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = []
            for block in c:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return "\n".join(parts)
    return ""


def _build_key(
    *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tenant_id: str,
    user_role: str,
) -> str:
    user_text = _last_user_message(messages)
    components = (
        f"v1|t={tenant_id}|r={user_role}|s={hashlib.sha256(system.encode()).hexdigest()[:16]}|"
        f"u={hashlib.sha256(user_text.encode('utf-8', errors='replace')).hexdigest()[:24]}|"
        f"tools={_digest_tools(tools)}"
    )
    return components


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def cached_call(
    llm_call: Callable[..., Any],
    *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tenant_id: str = "default",
    user_role: str = "viewer",
    on_token: Callable[[str], None] | None = None,
) -> LLMResponse:
    """Wrap an LLM call with semantic caching.

    `llm_call` must be the bound `llm_client.call` method (we inject the
    same kwargs the agent loop passes through). When `on_token` is supplied
    we ALWAYS bypass the cache — streaming has its own UX contract that
    the cache would violate by replaying a complete response in one shot.
    """
    # Bypass the cache when:
    #   - it's globally disabled, OR
    #   - the caller is streaming tokens (cache would replay one shot,
    #     violating the streaming UX contract).
    if _is_disabled() or on_token is not None:
        if on_token is not None:
            return await llm_call(
                system=system, messages=messages, tools=tools, on_token=on_token,
            )
        return await llm_call(system=system, messages=messages, tools=tools)

    key = _build_key(
        system=system, messages=messages, tools=tools,
        tenant_id=tenant_id, user_role=user_role,
    )
    now = time.monotonic()
    ttl = _ttl_seconds()

    # Lookup — also evicts stale entries lazily.
    entry = _store.get(key)
    if entry is not None:
        if (now - entry.inserted_at) < ttl:
            # Move to MRU position (LRU-style).
            _store.move_to_end(key)
            entry.hits += 1
            _metrics["hits"] += 1
            # Return a copy with the same content; the agent loop appends
            # to messages so we don't want it mutating the cached object.
            return LLMResponse(
                text=entry.response.text,
                tool_uses=list(entry.response.tool_uses),
                stop_reason=entry.response.stop_reason,
                tokens_in=0,  # cache hit — no provider tokens spent
                tokens_out=0,
            )
        # Expired — drop it.
        _store.pop(key, None)
        _metrics["evictions"] += 1

    _metrics["misses"] += 1
    response = await llm_call(system=system, messages=messages, tools=tools)

    # Don't cache responses that triggered tool calls — tool execution is
    # side-effectful and we want fresh audit trails on every run.
    if not response.tool_uses:
        _store[key] = _CacheEntry(response=response, inserted_at=now)
        _metrics["inserts"] += 1
        # Enforce LRU max — pop the oldest until under cap.
        cap = _max_entries()
        while len(_store) > cap:
            _store.popitem(last=False)
            _metrics["evictions"] += 1

    return response
