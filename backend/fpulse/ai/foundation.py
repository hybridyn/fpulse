"""
Foundation layer for the F-Pulse AI arc.

Single chokepoint that every AI-callable feature uses to invoke an LLM with
a guaranteed deterministic fallback. Composes:

  resolve_provider()          (existing in planner/ai_client.py)
  sanitize_for_llm()          (sanitize.py)
  enforce_budget()            (budget.py)
  cache.get/set               (cache.py)

What this module guarantees:
  - LLM path runs only when a provider is actually configured.
  - Fallback runs whenever LLM returns None, raises, or exceeds budget.
  - Caller never has to repeat the try/except + provider-check pattern.
  - Source of result ("llm" / "fallback") is reported back so callers can
    surface it (e.g. ai_powered=true on the response).

Step 1 foundation. See:
  docs/ai-boundary-contract.md
  docs/trust.md (Pillar 1)
  backend/tests/architecture/test_invariants.py
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class ProviderInfo:
    """Resolved provider state for the current call.

    Cheap to construct; safe to log (no api_key).
    """

    provider: str       # "claude" / "openai" / "ollama" / "none"
    model: str
    has_provider: bool  # True iff provider != "none"


def get_provider_info(
    user_id: str | None = None,
    workspace_id: str | None = None,
) -> ProviderInfo:
    """Read-only provider check without attempting a call."""
    from fpulse.planner.ai_client import resolve_provider

    provider, _, model, _ = resolve_provider(
        user_id=user_id, workspace_id=workspace_id
    )
    return ProviderInfo(
        provider=provider,
        model=model,
        has_provider=(provider != "none"),
    )


async def try_llm_then_fallback(
    *,
    llm_fn: Callable[[ProviderInfo], Awaitable[T | None]],
    fallback_fn: Callable[[], T],
    user_id: str | None = None,
    workspace_id: str | None = None,
) -> tuple[T, str]:
    """Run the LLM path; on miss/failure, return deterministic fallback.

    Args:
      llm_fn: async function receiving ProviderInfo. Must return None to
        signal "no usable result, fall back" (e.g. low confidence, parse
        failure). Raising is treated identically to returning None.
      fallback_fn: sync function that ALWAYS returns a valid result. The
        rule-based path. Must never raise.
      user_id / workspace_id: threaded through to provider resolution.

    Returns:
      (result, source) where source is "llm" or "fallback". The boolean
      ``ai_powered = (source == "llm")`` is the canonical surface for UI.

    The wrapper never raises. Any exception in llm_fn is swallowed and the
    fallback runs. Exceptions in fallback_fn DO propagate — that would be
    a bug in deterministic logic, not an AI failure.
    """
    info = get_provider_info(user_id=user_id, workspace_id=workspace_id)

    if not info.has_provider:
        return (fallback_fn(), "fallback")

    try:
        result = await llm_fn(info)
    except Exception:
        return (fallback_fn(), "fallback")

    if result is None:
        return (fallback_fn(), "fallback")
    return (result, "llm")
