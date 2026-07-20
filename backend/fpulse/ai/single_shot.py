"""
Single-shot LLM mode — May 5 2026.

Sits between the fast-lane router (no LLM at all) and the full agent
loop (6 iterations × tool calls). Targets the middle ground:

  * Question NEEDS reasoning — fast-lane can't answer
  * Question does NOT need tools — page context + user prompt is enough

For these, ONE LLM call with no tools, no RAG, a tight ≤5-sentence
constraint runs in 3-8 s on local Ollama vs. 30-90 s for the full loop.

The classifier is intentionally conservative — when in doubt, fall
through to the full agent. Wrong-fast is much worse than slow-correct.

Public surface:
  * ``should_use_single_shot(prompt: str) -> bool`` — pure classifier.
  * ``run_single_shot(prompt, page_context, llm_client, app_state)
        -> SingleShotResult`` — builds prompt + calls LLM once.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from fpulse.ai.context import PageContext
from fpulse.ai.session_context import build_session_block

logger = logging.getLogger(__name__)


# Reasoning markers — when present the user wants explanation, not action.
_REASONING_MARKERS = (
    "why ", "why is", "why did", "why does", "why are", "why isn't", "why does",
    "explain", "what does", "what is the difference", "what is meant by",
    "how does", "how come", "what's the meaning",
    "tell me about this", "tell me about that",
    "what's happening", "whats happening",
    "what's the purpose", "what is the purpose",
    "interpret", "summarise this", "summarize this",
    "what should i", "what would happen",
    # Added May 17 2026 — natural dashboard phrasings that the fast-lane
    # doesn't catch (no exact-phrase trigger) but which are reasoning-y
    # and don't need the 6-step agent loop. These phrasings show up
    # frequently in real chat sessions and previously fell through to
    # the full loop on local Ollama (60-180 s).
    "what's going on", "whats going on", "what is going on",
    "anything concerning", "anything broken",
    "should i worry", "is anything wrong",
    "how are things", "how are we doing", "how's it going", "hows it going",
    "give me a rundown", "rundown of",
    "in plain english", "in plain words", "in simple terms",
)

# Imperative / action markers — these need direct_actions or full agent.
# Single-shot must NOT handle them (it can't actually do anything).
_IMPERATIVE_MARKERS = (
    "run ", "execute ", "trigger ",
    "delete ", "remove ", "drop ",
    "cancel ", "stop ", "kill ",
    "create ", "make ", "build ", "set up ", "setup ",
    "rename ", "duplicate ", "copy ",
    "schedule ", "deploy ", "publish ",
    "test connection", "test this connection",
)

# Multi-step markers — these need the full agent loop. Single-shot has
# one shot to answer, so chained instructions break.
_MULTISTEP_MARKERS = (
    "first ", "then ", "after that", "step by step", "step-by-step",
    "and then", " and also ", " followed by ",
    "for each", "foreach", "for every",
)


def should_use_single_shot(prompt: str) -> bool:
    """Decide whether this prompt is a good fit for single-shot.

    Returns True when:
      * Prompt contains a reasoning marker ("why", "explain", etc.)
      * AND no imperative-action marker
      * AND no multi-step marker
      * AND prompt length is reasonable (≤ 280 chars — tweet-length).
        Longer prompts are usually multi-part and the agent loop is
        a better fit.

    Pure function — safe to call without any I/O.
    """
    if not prompt:
        return False
    p = prompt.lower().strip()
    if len(p) > 280:
        return False
    if not any(m in p for m in _REASONING_MARKERS):
        return False
    if any(m in p for m in _IMPERATIVE_MARKERS):
        return False
    if any(m in p for m in _MULTISTEP_MARKERS):
        return False
    return True


# ─────────────────────────────────────────────────────────────────────
# System prompt — tight, no tools, page context inlined
# ─────────────────────────────────────────────────────────────────────


_SHOT_PROMPT_TAIL = (
    "\n\n## Response constraints\n"
    "- Answer in **5 sentences or fewer**.\n"
    "- Do **NOT** call tools or request more information; answer with what's "
    "in this prompt.\n"
    "- If you genuinely cannot answer from the context above, reply with one "
    "sentence saying so and suggest what page or action would help.\n"
    "- No preamble, no apologies, no \"as an AI\". Get to the point."
)


def _build_system_prompt(page_context: PageContext, app_state: dict[str, Any] | None) -> str:
    """Same Layer-1 session block the agent loop uses, plus a hard
    response-shape constraint. The shared block keeps the LLM grounded
    in page state + edition boundary; the tail caps verbosity."""
    layer1 = build_session_block(page_context, app_state)
    return layer1 + _SHOT_PROMPT_TAIL


@dataclass
class SingleShotResult:
    text: str
    elapsed_ms: int
    tokens_in: int
    tokens_out: int


async def run_single_shot(
    *,
    prompt: str,
    page_context: PageContext,
    llm_client: Any,
    app_state: dict[str, Any] | None,
    on_token=None,
) -> SingleShotResult:
    """Run the single-shot path. ONE LLM call, no tools, no RAG.

    ``llm_client`` must implement the same ``call(*, system, messages,
    tools, on_token)`` signature the agent loop uses (Ollama / Anthropic /
    OpenAI clients all match).
    """
    system = _build_system_prompt(page_context, app_state)
    messages = [{"role": "user", "content": prompt}]
    t0 = time.perf_counter()
    response = await llm_client.call(
        system=system,
        messages=messages,
        tools=[],
        on_token=on_token,
    )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    # The LLM clients return a small response object; extract text robustly
    # so we don't depend on a specific attribute name across providers.
    text = (
        getattr(response, "text", None)
        or getattr(response, "content", None)
        or ""
    )
    if not isinstance(text, str):
        text = str(text)
    tokens_in = int(getattr(response, "tokens_in", 0) or 0)
    tokens_out = int(getattr(response, "tokens_out", 0) or 0)

    return SingleShotResult(
        text=text.strip(),
        elapsed_ms=elapsed_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )
