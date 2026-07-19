"""Per-request tool gating for local LLMs.

Background (2026-05-22). The agent registers ~24 tools. Sending all 24
schemas to a CPU-class 7B model every turn is a real problem:

  - Each tool schema adds ~80 tokens to the prompt. 24 * 80 = ~1.9 K
    tokens of tool definitions before the user's question even starts.
  - Local 7B models pick the wrong tool more often as the candidate set
    grows. Empirically, dropping from 24 → 6 tools roughly halves the
    "model called list_executions when I asked about connections"
    failure mode.
  - The OS-default Ollama context (`num_ctx=2048`) silently truncated
    the system prompt + tool block (fixed in `ollama_client.py` on the
    same day); even after raising num_ctx to 8192, fewer tools means
    less head-of-prompt that the model has to attend to.

This module exposes ``select_tools(page, prompt, available_tools)`` that
returns a curated subset (default cap: 8 tools) by combining:

  1. **Floor tools** — always included regardless of page/prompt.
  2. **Page-based picks** — tools known to be relevant to the current
     UI surface (editor / executions / connections / etc).
  3. **Keyword-based boosts** — tools whose names match terms in the
     prompt ("failure", "schedule", "connection", ...).
  4. **Deterministic fill** — if we're under the cap, fill from a stable
     ordering so the candidate set is reproducible (good for caching).

Selection is ON for all providers by default (2026-06-18; was Ollama-only):
the caller (agent.py) gives Ollama a tight cap (8) for routing accuracy and
cloud a looser cap (14) — every tool schema is ~80 tokens re-sent each loop, so
trimming the long tail saves tokens/$ on cloud too while keeping broad coverage.
Override with ``FPULSE_TOOL_SELECTOR=off``.

Bypass entirely with ``FPULSE_DISABLE_TOOL_SELECTOR=1`` if you want
every call to see every tool (useful for debugging which tool a model
WOULD have picked given the choice).
"""

from __future__ import annotations

import os
from typing import Iterable, Sequence

from fpulse.ai.tools.base import ToolDefinition


# Tools that should ALWAYS be in the candidate set. The floor exists so
# the model never gets stuck without an "I need to find this" escape
# hatch. Keep this list small — every floor entry costs prompt tokens
# even when the page wouldn't otherwise surface it.
_FLOOR_TOOLS: tuple[str, ...] = (
    "workspace_overview",   # generic "what's in this workspace" question
    "recall_history",       # cross-cutting RAG retrieval
    "lookup_help_topic",    # in-app help atlas
)


# Page-specific tool packs. The page identifier comes from
# ``PageContext.page`` — see ``frontend/src/hooks/usePageContext.ts`` for
# the canonical strings. Use ``startswith`` matching so sub-routes
# (e.g. ``editor.canvas`` vs ``editor.diff``) share the editor pack.
_PAGE_TOOLS: dict[str, tuple[str, ...]] = {
    "editor": (
        "validate_pipeline",
        "summarize_pipeline",
        "explain_step",
        "modify_pipeline_step",
        "draft_pipeline_from_intent",
        "list_templates",
    ),
    "executions": (
        "list_executions",
        "get_running_executions",
        "query_metrics",
        "summarize_pipeline",
    ),
    "connections": (
        "inspect_connections",
        "list_catalog",
    ),
    "pipelines": (
        "list_pipelines",
        "list_projects",
        "summarize_pipeline",
        "list_schedules",
    ),
    "projects": (
        "list_projects",
        "list_pipelines",
    ),
    "schedules": (
        "list_schedules",
        "get_next_scheduled",
        "list_pipelines",
    ),
    "alerts": (
        "list_alerts",
        "draft_alert_rule",
        "list_executions",
    ),
    "templates": (
        "list_templates",
        "draft_pipeline_from_intent",
    ),
    "dashboard": (
        "get_installation_health",
        "list_steward_findings",
        "query_metrics",
        "list_pipelines",
        "list_executions",
    ),
    "insights": (
        "get_installation_health",
        "list_steward_findings",
        "query_metrics",
        "list_executions",
    ),
    "steward": (
        "list_steward_findings",
        "get_installation_health",
    ),
    "trust": (
        "get_installation_health",
        "list_steward_findings",
        "get_user_role",
    ),
}


# Prompt-keyword → tool boost. Keys are lowercase substrings. When ANY
# substring matches the user's prompt, the listed tools become candidates
# regardless of the current page. Order within a list matters — earlier
# entries win if the cap is tight.
_KEYWORD_TOOLS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    # Failures / errors / debugging
    (("fail", "error", "broke", "crash", "timeout"),
     ("list_executions", "recall_history", "get_installation_health")),
    # Connection-related
    (("connection", "credential", "auth"),
     ("inspect_connections", "list_catalog")),
    # Schedules
    (("schedule", "cron", "run at", "next run"),
     ("list_schedules", "get_next_scheduled")),
    # Permissions / role
    (("role", "permission", "rbac", "who can"),
     ("get_user_role",)),
    # Reports / output
    (("report", "summary", "summarise", "summarize"),
     ("compose_report", "summarize_pipeline")),
    # Health / audit / punch-list
    (("health", "audit", "punch list", "what should i fix", "what needs"),
     ("get_installation_health", "list_steward_findings")),
    # Steward advisories — duplicates, governance, compliance, risk review
    (("steward", "duplicate", "governance", "compliance", "risk", "redundant", "flag"),
     ("list_steward_findings", "get_installation_health")),
    # Build / draft / create
    (("create", "build", "draft", "make a pipeline"),
     ("draft_pipeline_from_intent", "apply_pipeline_draft")),
)


def _page_bucket(page: str | None) -> str | None:
    """Map a page string like ``editor.canvas`` to a bucket key (``editor``)."""
    if not page:
        return None
    head = page.split(".", 1)[0].strip().lower()
    return head or None


def _is_enabled_for_provider(provider_hint: str | None) -> bool:
    """Selection defaults: ON for every provider.

    2026-06-18 — extended from Ollama-only to all providers. Cloud models
    handle 24 schemas *correctly*, but every schema is ~80 tokens re-sent
    on every loop iteration, so all-26-tools-every-turn was a real token
    (and $) tax for cloud users too. Cloud gets a looser cap than Ollama
    (see select_tools) so the broader candidate set is preserved while the
    long tail of irrelevant tools is trimmed. Fully env-overridable:
    set FPULSE_TOOL_SELECTOR=off (or FPULSE_DISABLE_TOOL_SELECTOR=1) to
    restore every-tool-every-call.
    """
    override = os.environ.get("FPULSE_TOOL_SELECTOR", "").strip().lower()
    if override in {"1", "true", "yes", "on", "always"}:
        return True
    if override in {"0", "false", "no", "off", "never"}:
        return False
    # Default: on for all providers.
    return True


def select_tools(
    *,
    available_tools: Sequence[ToolDefinition],
    page: str | None,
    prompt: str,
    provider_hint: str | None = None,
    max_tools: int = 8,
) -> list[ToolDefinition]:
    """Return a curated list of tools for this request.

    ``available_tools`` is the result of ``registry.filter_by_tiers(...)``
    — i.e. RBAC has already happened. This function only narrows the set
    further by relevance to the page + prompt.

    Returns ``available_tools`` unchanged when:
      - selection is disabled via ``FPULSE_TOOL_SELECTOR=off`` or
        ``FPULSE_DISABLE_TOOL_SELECTOR=1``,
      - or the available set is already at/below ``max_tools``.
    """
    if os.environ.get("FPULSE_DISABLE_TOOL_SELECTOR", "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return list(available_tools)

    if not _is_enabled_for_provider(provider_hint):
        return list(available_tools)

    if len(available_tools) <= max_tools:
        return list(available_tools)

    by_name: dict[str, ToolDefinition] = {t.name: t for t in available_tools}
    picked: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name in seen or name not in by_name:
            return
        if len(picked) >= max_tools:
            return
        picked.append(name)
        seen.add(name)

    # 1. Floor — always present
    for name in _FLOOR_TOOLS:
        _add(name)

    # 2. Page bucket
    bucket = _page_bucket(page)
    if bucket and bucket in _PAGE_TOOLS:
        for name in _PAGE_TOOLS[bucket]:
            _add(name)

    # 3. Keyword boosts
    p = (prompt or "").lower()
    for triggers, boosted in _KEYWORD_TOOLS:
        if any(t in p for t in triggers):
            for name in boosted:
                _add(name)

    # 4. Deterministic fill — by-name alphabetical order. Stable for
    # cache keys; doesn't accidentally pick destructive tools first
    # because the registry already filtered by tier.
    for name in sorted(by_name.keys()):
        if len(picked) >= max_tools:
            break
        _add(name)

    return [by_name[n] for n in picked if n in by_name]


__all__ = ["select_tools"]
