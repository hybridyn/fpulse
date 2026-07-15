"""Per-user AI endpoint rate limiter.

The AI agent + AI pipeline endpoints (`/api/ai/agent`,
`/api/ai/agent/stream`, etc.) make outbound LLM calls that cost real
money (OpenAI / Anthropic / OpenRouter charge per token, Ollama is
free but CPU-bound). Without per-user throttling, a single user — or
a single leaked session token — can:

  * drain the operator's LLM budget in minutes (cost abuse)
  * saturate the Ollama queue and DoS other workspace users
  * trigger LLM-vendor abuse flags that get the org's API key
    rate-limited or banned

Defense: sliding-window counter keyed on `(user_id_or_ip)`. When a
window's quota is exhausted, subsequent requests fail with HTTP 429
+ Retry-After so the frontend can show a friendly cool-down. No
external store required — single-node OSS deployments need no Redis.

Tunables via env:
  FPULSE_AI_RATE_MAX_PER_HOUR    default 60   (per-user calls per hour)
  FPULSE_AI_RATE_WINDOW_SECONDS  default 3600 (sliding window length)
  FPULSE_AI_RATE_ENABLE          default 1    (set to 0 to disable)
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque

from fastapi import HTTPException

# ── Tunables ──────────────────────────────────────────────────────────

_MAX_PER_WINDOW = int(os.environ.get("FPULSE_AI_RATE_MAX_PER_HOUR", "60"))
_WINDOW_SECONDS = int(os.environ.get("FPULSE_AI_RATE_WINDOW_SECONDS", "3600"))
_RATE_LIMIT_ENABLED = os.environ.get(
    "FPULSE_AI_RATE_ENABLE", "1"
).strip().lower() in ("1", "true", "yes", "on")


# ── State ─────────────────────────────────────────────────────────────
#
# Per-key timestamp deque. Each entry is a unix-seconds float. On every
# request we drop entries older than now - WINDOW and check the length.
# This is O(N) over the window length per request, but N is bounded by
# the quota so the cost is trivial in absolute terms.

_state_lock = threading.Lock()
_state: dict[str, deque] = {}


def _key(user_id: str | None, fallback_ip: str | None) -> str:
    """Build the rate-limit key. Prefer user_id; fall back to IP."""
    if user_id:
        return f"user:{user_id}"
    return f"ip:{fallback_ip or 'unknown'}"


def check_ai_rate_limit(
    *,
    user_id: str | None,
    fallback_ip: str | None = None,
    cost: int = 1,
) -> None:
    """Enforce the AI per-user rate limit.

    Call this at the top of any AI-cost endpoint. If the user is over
    quota, raises HTTP 429 with a Retry-After header. Otherwise records
    the request and returns.

    Args:
        user_id: authenticated user id (preferred key).
        fallback_ip: client IP for anonymous callers.
        cost: how many "calls" this request counts as. Streaming
            endpoints may want to charge 2-3 to reflect their
            longer LLM session. Default 1.

    The check is a no-op when ``FPULSE_AI_RATE_ENABLE=0``.
    """
    if not _RATE_LIMIT_ENABLED:
        return

    key = _key(user_id, fallback_ip)
    now = time.time()
    cutoff = now - _WINDOW_SECONDS

    with _state_lock:
        dq = _state.get(key)
        if dq is None:
            dq = deque()
            _state[key] = dq
        # Drop entries that have aged out of the window.
        while dq and dq[0] < cutoff:
            dq.popleft()

        if len(dq) + cost > _MAX_PER_WINDOW:
            # Over quota — figure out when the oldest in-window entry
            # will age out, that's when capacity returns.
            oldest = dq[0] if dq else now
            retry_after = max(1, int(_WINDOW_SECONDS - (now - oldest)))
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "ai_rate_limit_exceeded",
                    "message": (
                        f"AI request quota exceeded ({_MAX_PER_WINDOW} per "
                        f"{_WINDOW_SECONDS // 60} minutes). Try again in "
                        f"{retry_after // 60} minute(s)."
                    ),
                    "retry_after_seconds": retry_after,
                    "quota": _MAX_PER_WINDOW,
                    "window_seconds": _WINDOW_SECONDS,
                },
                headers={"Retry-After": str(retry_after)},
            )

        # Record this request.
        for _ in range(cost):
            dq.append(now)


def get_ai_rate_usage(
    *,
    user_id: str | None,
    fallback_ip: str | None = None,
) -> dict:
    """Inspect current usage for a key. Used by the agent budget endpoint.

    Returns:
        {used: int, quota: int, remaining: int, window_seconds: int}
    """
    key = _key(user_id, fallback_ip)
    now = time.time()
    cutoff = now - _WINDOW_SECONDS
    with _state_lock:
        dq = _state.get(key, deque())
        while dq and dq[0] < cutoff:
            dq.popleft()
        used = len(dq)
    return {
        "used": used,
        "quota": _MAX_PER_WINDOW,
        "remaining": max(0, _MAX_PER_WINDOW - used),
        "window_seconds": _WINDOW_SECONDS,
        "enabled": _RATE_LIMIT_ENABLED,
    }
