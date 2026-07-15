"""TOOL_ONLY_MODE — disable all LLM-using lanes, keep fast-lane only.

Use case: ultra-private operators who want zero LLM traffic (even local
Ollama) but still want the Copilot's deterministic tool dispatchers for
common operational questions.

When ``FPULSE_TOOL_ONLY_MODE=1`` is set:
  * The fast-lane (regex → tool → template) keeps working — no LLM.
  * The hybrid lane (1 tool + 1 LLM format) is blocked.
  * The single-shot lane (1 LLM call, no tools) is blocked.
  * The full agent loop is blocked.
  * Direct-action handlers (run_pipeline, test_connection, etc.) keep
    working — they're deterministic dispatches, not LLM calls.

When the fast-lane misses, the endpoint returns a friendly message
telling the user TOOL_ONLY_MODE is active and which deterministic
phrasings still work, instead of silently falling through to an LLM.

Toggleable via env var only (operator-level). The Settings UI surfaces
the current state read-only so users know what to expect.
"""

from __future__ import annotations

import os


_TRUE_VALUES = {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    """True when ``FPULSE_TOOL_ONLY_MODE`` is set to a truthy value.

    Re-read every call so operators can flip it without restarting
    (matters in dev — production should restart for full effect since
    some upstream caches may persist).
    """
    return os.environ.get("FPULSE_TOOL_ONLY_MODE", "").strip().lower() in _TRUE_VALUES


_UNAVAILABLE_MESSAGE = (
    "Tool-only mode is on, so I can only answer questions I can resolve "
    "with deterministic tools (no LLM). Try one of these phrasings:\n\n"
    "  • \"show my failures\" / \"what failed today\"\n"
    "  • \"list pipelines\" / \"list schedules\" / \"list alerts\"\n"
    "  • \"workspace overview\"\n"
    "  • \"check this connection\"\n"
    "  • \"run pipeline <name>\"\n\n"
    "To enable open-ended questions, unset ``FPULSE_TOOL_ONLY_MODE`` and "
    "restart the server."
)


def unavailable_response_text() -> str:
    """Plain-English explanation for when the fast-lane misses in
    TOOL_ONLY_MODE. Surfaced as the agent's reply so the user knows
    why the LLM-based lanes aren't responding."""
    return _UNAVAILABLE_MESSAGE
