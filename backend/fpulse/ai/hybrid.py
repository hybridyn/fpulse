"""
Hybrid (tool + LLM format) lane — May 17 2026.

Sits between the pure fast-lane (no LLM at all) and pure single-shot
(LLM but no tool data). Targets the middle ground that neither lane
covers cleanly:

  * Question has a reasoning marker ("why", "explain", "how come" …),
    so pure fast-lane returns a deterministic template that often
    doesn't directly answer the asked question.
  * Question needs FRESH data the LLM doesn't have in its prompt,
    so pure single-shot would either hallucinate or give a generic
    "I'd need to check" reply.

Solution: run ONE fast-lane tool to gather deterministic data, then
ONE short LLM call to phrase the answer in the user's own framing.
Total latency on local Ollama ~ 0.5 s (tool) + 3-8 s (LLM) = 4-9 s.
That's vs. 30-180 s for the full 6-step agent loop.

Trust invariant: the LLM never sees raw tool JSON — only the
deterministic rendered text from the fast-lane handler, with the
[CARD]…[/CARD] visual blocks stripped. The system prompt forbids
inventing facts and tells the model to answer using ONLY the data
above. Same anti-fabrication contract as single-shot.

Public surface:
  * ``should_use_hybrid(prompt) -> tuple[FastIntent, float, str] | None``
    — pure classifier. Combines reasoning-marker check with
    find_hybrid_candidate from fast_router.
  * ``run_hybrid(prompt, intent, fast_ctx, llm_client, page_context,
    app_state, on_token=None) -> HybridResult``
    — executes the fast-lane handler, strips card blocks, calls LLM
    once with the tight format prompt.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from fpulse.ai.context import PageContext
from fpulse.ai.fast_router import (
    FastIntent,
    find_hybrid_candidate,
    _has_llm_required_word,  # noqa: F401  (used to gate hybrid entry)
)
from fpulse.ai.session_context import build_session_block
from fpulse.ai.tools.base import ToolContext

logger = logging.getLogger(__name__)


# [CARD]{...}[/CARD] visual-card markers — stripped before sending the
# handler text to the LLM so the model doesn't try to copy / re-emit
# them (which it would do badly, breaking the frontend renderer).
_CARD_RE = re.compile(r"\[CARD\].*?\[/CARD\]", flags=re.DOTALL)


# Phrases that the deterministic fast-lane handlers emit when they have
# nothing meaningful to report. The hybrid lane treats these as "empty"
# because formatting them with an LLM doesn't help — the user asked
# "why did X fail?" and the single tool we picked said "I have no record
# of X". The caller should fall through to the full agent loop so a
# different tool (e.g. inspect_connections, get_installation_health)
# can find the real cause.
#
# Match is case-insensitive substring after [CARD] stripping. Keep this
# list short and aligned with phrasings used by fast_router handlers —
# every entry here is a deliberate trip-wire, not a guess.
_EMPTY_GENERIC_MARKERS: tuple[str, ...] = (
    "no record",
    "no records",
    "nothing to report",
    "no results",
    "no data yet",
    "i don't see any",
    "i didn't find any",
    "no matching",
    "no items",
    "no executions",
    "no failures",
    "no pipelines",
    "no schedules",
    "no alerts",
    "no connections",
    "0 results",
    "(empty)",
)


def is_empty_or_generic(handler_text: str) -> bool:
    """True when the fast-lane handler output is too thin to format.

    Strips card markers first, then lowercases and checks for any of the
    explicit empty-marker phrases. Also flags very-short outputs (< 24
    chars after stripping) — those almost always indicate "I had nothing
    to say" rather than a useful answer.

    The hybrid lane caller uses this to decide whether to spend the LLM
    format-pass budget. When True, the caller should fall through to the
    full agent loop so a different tool can take a swing at the question.
    """
    if not handler_text:
        return True
    stripped = _strip_card_blocks(handler_text).strip().lower()
    if not stripped:
        return True
    if len(stripped) < 24:
        return True
    return any(marker in stripped for marker in _EMPTY_GENERIC_MARKERS)


def should_use_hybrid(prompt: str) -> tuple[FastIntent, float, str] | None:
    """Classify whether this prompt is a fit for the hybrid lane.

    Returns ``(intent, confidence, reason)`` when ALL hold:
      * Prompt has a reasoning marker (_has_llm_required_word == True)
      * A fast-lane candidate exists at HYBRID_MIN_CONFIDENCE (0.5+),
        excluding intents that already serve_reasoning themselves
      * Prompt length is reasonable (≤ 280 chars — same heuristic as
        single-shot; longer = probably multi-part = full agent fits better)

    Returns None otherwise (caller falls through to single-shot / full agent).

    Pure function — safe to call without any I/O.
    """
    if not prompt:
        return None
    p = prompt.lower().strip()
    if len(p) > 280:
        return None
    if not _has_llm_required_word(p):
        # No reasoning marker → pure fast-lane would have caught a strong
        # match already, or single-shot isn't a good fit. Nothing for
        # hybrid to do.
        return None
    return find_hybrid_candidate(prompt)


# Tight format-pass prompt. Hard cap on length (3-5 sentences) so even
# slow local models finish in seconds. Same anti-fabrication clauses as
# the main agent system prompt.
_HYBRID_PROMPT_TAIL = (
    "\n\n## Hybrid format pass — strict rules\n"
    "You have been handed FRESH data fetched from F-Pulse just now.\n"
    "Answer the user's question using ONLY that data — do not invent\n"
    "pipeline names, numbers, statuses, or causes that are not\n"
    "literally present in the data block. If the data does not answer\n"
    "the question, say so in one sentence and suggest which page or\n"
    "follow-up question would help.\n\n"
    "Format constraints:\n"
    "- 3 to 5 sentences maximum.\n"
    "- No preamble, no apologies, no \"based on the data\" prefix.\n"
    "- Plain prose. Bullets are fine for ≥ 3 items.\n"
    "- Do NOT call tools. Do NOT request more information.\n"
    "- Do NOT emit [CARD] blocks — the frontend already has them.\n"
)


def _build_hybrid_system_prompt(
    page_context: PageContext,
    app_state: dict[str, Any] | None,
) -> str:
    """Reuses the shared session block (page state + edition boundary +
    trust contract) then appends the hybrid-specific format rules."""
    return build_session_block(page_context, app_state) + _HYBRID_PROMPT_TAIL


def _strip_card_blocks(text: str) -> str:
    """Remove [CARD]…[/CARD] visual markers, collapse leftover blank
    lines. Keeps the actual textual answer intact."""
    cleaned = _CARD_RE.sub("", text)
    # Collapse 3+ newlines down to 2 (preserves paragraph breaks).
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


@dataclass
class HybridResult:
    text: str
    intent_name: str
    confidence: float
    tool_ms: int      # time spent in the fast-lane handler
    llm_ms: int       # time spent in the LLM format pass
    elapsed_ms: int   # total wall-clock (tool_ms + llm_ms + small overhead)
    tokens_in: int
    tokens_out: int
    handler_text: str  # the raw fast-lane output (for the trace UI)
    # True when the single fast-lane tool returned empty/generic data
    # AND the user prompt had a reasoning marker. Caller should fall
    # through to the full agent loop so a different tool can find
    # the real cause (e.g. inspect_connections for a credential issue
    # behind a "why did this fail?" prompt that hit list_executions).
    escalate: bool = False
    escalate_reason: str = ""


async def run_hybrid(
    *,
    prompt: str,
    intent: FastIntent,
    fast_ctx: ToolContext,
    llm_client: Any,
    page_context: PageContext,
    app_state: dict[str, Any] | None,
    on_token=None,
) -> HybridResult:
    """Execute the hybrid lane: fast-lane handler → LLM format pass.

    Raises if the fast-lane handler raises (caller should fall through
    to single-shot or full agent on exception). The LLM call is wrapped
    so a provider error returns a graceful "handler_text + apology"
    result instead of bombing the whole run.
    """
    if intent.handler is None:
        raise ValueError(
            f"Hybrid lane can't run intent {intent.name!r} — no handler"
        )

    t0_total = time.perf_counter()

    # Phase 1: deterministic tool fetch via the fast-lane handler.
    t0_tool = time.perf_counter()
    handler_text = await intent.handler(prompt, fast_ctx)
    tool_ms = int((time.perf_counter() - t0_tool) * 1000)

    # Strip [CARD] blocks before showing the data to the LLM. The cards
    # are frontend-renderer markers and would confuse the format pass
    # (model would either copy them verbatim or hallucinate similar ones).
    data_for_llm = _strip_card_blocks(handler_text)

    # Empty / generic escalation — Risk A from the May 17 2026 review.
    # If the user asked a reasoning question ("why did X fail?") and the
    # one fast-lane tool we picked returned an empty list or a generic
    # "no record" reply, formatting that with an LLM doesn't answer the
    # question — it just dresses up the empty answer. Signal escalate=True
    # so the caller falls through to the full multi-tool agent loop,
    # which can try the OTHER tool that actually has the data (e.g.
    # inspect_connections finding an expired credential after the
    # list_executions handler said "no recent runs").
    if is_empty_or_generic(handler_text):
        elapsed_ms = int((time.perf_counter() - t0_total) * 1000)
        return HybridResult(
            text=handler_text,
            intent_name=intent.name,
            confidence=0.5,
            tool_ms=tool_ms,
            llm_ms=0,
            elapsed_ms=elapsed_ms,
            tokens_in=0,
            tokens_out=0,
            handler_text=handler_text,
            escalate=True,
            escalate_reason=(
                "fast-lane handler returned empty/generic — full agent "
                "loop may have a tool that knows the answer"
            ),
        )

    # Phase 2: LLM format pass.
    system = _build_hybrid_system_prompt(page_context, app_state)
    user_message = (
        f"User asked: {prompt}\n\n"
        f"## Fresh data fetched just now (from {intent.name})\n"
        f"{data_for_llm}\n\n"
        f"Answer the user's question using ONLY the data above."
    )
    messages = [{"role": "user", "content": user_message}]

    t0_llm = time.perf_counter()
    try:
        response = await llm_client.call(
            system=system,
            messages=messages,
            tools=[],
            on_token=on_token,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("hybrid LLM format pass failed: %s", exc)
        elapsed_ms = int((time.perf_counter() - t0_total) * 1000)
        # Graceful fallback — return the deterministic handler text. User
        # still gets the data, just without the reasoning narrative.
        return HybridResult(
            text=handler_text,
            intent_name=intent.name,
            confidence=0.8,
            tool_ms=tool_ms,
            llm_ms=0,
            elapsed_ms=elapsed_ms,
            tokens_in=0,
            tokens_out=0,
            handler_text=handler_text,
        )
    llm_ms = int((time.perf_counter() - t0_llm) * 1000)

    text = (
        getattr(response, "text", None)
        or getattr(response, "content", None)
        or ""
    )
    if not isinstance(text, str):
        text = str(text)
    text = text.strip()
    if not text:
        # LLM returned empty — fall back to handler text.
        text = handler_text

    tokens_in = int(getattr(response, "tokens_in", 0) or 0)
    tokens_out = int(getattr(response, "tokens_out", 0) or 0)

    elapsed_ms = int((time.perf_counter() - t0_total) * 1000)
    return HybridResult(
        text=text,
        intent_name=intent.name,
        confidence=1.0,
        tool_ms=tool_ms,
        llm_ms=llm_ms,
        elapsed_ms=elapsed_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        handler_text=handler_text,
    )
