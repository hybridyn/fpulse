"""
Clarification layer — May 5 2026.

Fires BEFORE single-shot and BEFORE the full agent loop. Catches prompts
that contain a reasoning verb + a vague reference ("this", "my last",
"the failed one") with NO concrete target. Instead of burning 30-90s
on a guessing LLM call, asks the user "which one?" with an instant,
clickable list pulled straight from visible_items.

Modeled on Kodee's confirmation pattern: "I found the email plan. To
cancel it, I need your confirmation that this is the exact plan…" —
the bot asks first, then acts.

Latency target: <5ms. Pure rendering from on-screen state. No LLM, no
tool calls, no I/O.

Public surface:
  * ``needs_clarification(prompt, ctx) -> str | None`` — returns the
    missing kind ("pipeline" / "execution" / "connection") or None.
  * ``build_clarification(prompt, ctx, kind) -> str`` — markdown list
    with one-click follow-up prompts.
"""

from __future__ import annotations

import re
from typing import Any

from fpulse.ai.tools.base import ToolContext


# Reasoning verbs — same set the single-shot classifier checks.
_REASONING_VERBS = (
    "why ", "explain", "what does", "how does",
    "tell me about", "what's wrong", "whats wrong", "what is wrong",
    "diagnose", "investigate",
    "summarise", "summarize",
)

# Vague-reference markers — "this", "the", "my last" → user means a
# specific entity but didn't name it.
_VAGUE_REFS = (
    " this ", " this.", " this?", "this pipeline", "this run", "this connection",
    "this execution", "this workflow",
    " that ", " that.", " that?", "that pipeline", "that run",
    "my last", "the last", "my latest", "the latest",
    " the failed", " the broken", " the failing",
    "the recent", "my recent",
    "my pipeline", "my run", "my execution", "my connection",
)

# Map vague-reference fragments → the entity kind the user is asking about.
_REF_TO_KIND: tuple[tuple[tuple[str, ...], str], ...] = (
    (("pipeline", "workflow"), "pipeline"),
    (("run", "execution"), "execution"),
    (("connection", "data source"), "connection"),
)


_QUOTED_NAME_RE = re.compile(r"""['"`]([^'"`]+)['"`]""")


def _has_any(prompt_lower: str, needles: tuple[str, ...]) -> bool:
    return any(n in prompt_lower for n in needles)


def _infer_kind(prompt_lower: str, ctx: ToolContext) -> str | None:
    """Pick the entity kind the user is referring to.

    First the prompt itself ("this pipeline" → pipeline). Falls back to
    the kind dominant in ``visible_items`` so a prompt like "why did
    this fail?" on the Executions page resolves to ``execution``.
    """
    for tokens, kind in _REF_TO_KIND:
        if any(t in prompt_lower for t in tokens):
            return kind
    if ctx.visible_items:
        # Pick the most-frequent kind on screen.
        counts: dict[str, int] = {}
        for it in ctx.visible_items:
            k = it.get("kind") or ""
            if k:
                counts[k] = counts.get(k, 0) + 1
        if counts:
            return max(counts, key=lambda k: counts[k])
    return None


def needs_clarification(prompt: str, ctx: ToolContext) -> str | None:
    """Return the entity kind that needs disambiguation, or None.

    Fires when the prompt has a reasoning verb + vague reference + no
    concrete target. The CALLER decides what to do with the result based
    on candidate count (auto-pin if 1, clarify card if ≥2).

    Returns the inferred kind whenever the trigger conditions hold,
    *regardless of candidate count*. Phase-1 fix May 5 2026: the
    previous version returned None on count<2, which silently fell
    through to single-shot/agent and hid the auto-pin opportunity.
    """
    if not prompt:
        return None
    p = prompt.lower()
    if not _has_any(p, _REASONING_VERBS):
        return None
    if not _has_any(p, _VAGUE_REFS):
        return None
    if ctx.selected_ids:
        return None
    if _QUOTED_NAME_RE.search(prompt):
        return None
    return _infer_kind(p, ctx)


def _format_candidate_label(it: dict[str, Any], kind: str) -> str:
    name = it.get("name") or it.get("id") or "?"
    status = it.get("status") or ""
    meta = it.get("meta") or {}
    bits = [f"**{name}**"]
    if status:
        emoji = {
            "failed": "🔴", "error": "🔴",
            "success": "🟢", "published": "🟢",
            "running": "🟡", "queued": "🟡",
            "draft": "⚪", "archived": "⚫",
        }.get(status.lower(), "")
        bits.insert(0, emoji or "·")
        bits.append(f"({status})")
    if kind == "execution":
        dur = meta.get("duration_ms")
        if isinstance(dur, (int, float)):
            bits.append(f"— {int(dur)} ms")
    elif kind == "pipeline":
        last = meta.get("last_run") or meta.get("last_run_at")
        if last:
            bits.append(f"— last run {last}")
    elif kind == "connection":
        t = meta.get("type")
        if t:
            bits.append(f"— {t}")
    return " ".join(bits).strip()


def _focus_for(prompt_lower: str, kind: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-rank candidates so the most likely target is first.

    Heuristics:
      * If the prompt mentions "fail" / "broken" / "error", surface
        items whose status is failed/error.
      * If the prompt mentions "running" / "in progress", surface those.
      * "last" / "latest" / "recent" — already in display order
        (frontend sends most-recent-first), so leave order intact.
    """
    if kind in ("execution", "pipeline"):
        if any(w in prompt_lower for w in ("fail", "broken", "error")):
            failed = [it for it in items if (it.get("status") or "").lower() in ("failed", "error")]
            if failed:
                rest = [it for it in items if it not in failed]
                return failed + rest
        if any(w in prompt_lower for w in ("running", "in progress", "in flight")):
            running = [it for it in items if (it.get("status") or "").lower() in ("running", "queued")]
            if running:
                rest = [it for it in items if it not in running]
                return running + rest
    return list(items)


def build_clarification(prompt: str, ctx: ToolContext, kind: str) -> str:
    """Render the clarification message as an interactive `choices` card.

    The card lists candidate entities; clicking one POSTs to
    `/api/ai/agent/action` with a slot_fill payload that pins the
    entity and fires the originally-blocked intent — no LLM, no router.

    Falls back to a "navigate" hint when zero candidates are visible.
    """
    from fpulse.ai.dialogue_state import infer_intent_from_prompt
    from fpulse.ai.interactive_cards import choices_card

    p = prompt.lower()
    same_kind = [it for it in ctx.visible_items if (it.get("kind") or "") == kind]
    same_kind = _focus_for(p, kind, same_kind)
    label_kind = {"pipeline": "pipeline", "execution": "run", "connection": "connection"}.get(kind, kind)
    intent_name = infer_intent_from_prompt(prompt)

    if not same_kind:
        # Zero candidates of the right kind on screen. Two cases:
        # 1) User is already on the target page — they're "here" but
        #    nothing's loaded yet (or the workspace genuinely has none).
        #    Don't tell them to navigate; offer next steps that keep the
        #    conversation moving.
        # 2) User is on a different page — give the navigate chip.
        from fpulse.ai.interactive_cards import next_actions_card
        nav_page = {"pipeline": "workflows", "execution": "executions",
                    "connection": "connections"}.get(kind, "dashboard")
        page = (ctx.page or "").lower()
        # Heuristic: are we already on the relevant page?
        on_target = (
            (kind == "pipeline"   and ("pipeline" in page or "workflow" in page)) or
            (kind == "execution"  and "execution" in page) or
            (kind == "connection" and "connection" in page)
        )
        if on_target:
            # Pivot: ask which related thing they want, instead of a
            # dead-end "go elsewhere" message.
            if kind == "execution":
                intro = (
                    "I'm on Executions with you, but I don't see any runs in "
                    "the current view. A few directions we can take:"
                )
                chips = next_actions_card(actions=[
                    {"label": "Which pipeline?", "icon": "→", "style": "primary",
                     "action": {"kind": "ask", "prompt": "list pipelines"}},
                    {"label": "Show all executions",
                     "action": {"kind": "ask", "prompt": "show executions"}},
                    {"label": "Show recent failures",
                     "action": {"kind": "ask", "prompt": "show me recent failures"}},
                    {"label": "Run a pipeline",
                     "action": {"kind": "ask", "prompt": "run a pipeline"}},
                ])
            elif kind == "pipeline":
                intro = (
                    "I'm on the Workflows page but I don't see any pipelines "
                    "in view. Want to:"
                )
                chips = next_actions_card(actions=[
                    {"label": "List all pipelines", "icon": "→", "style": "primary",
                     "action": {"kind": "ask", "prompt": "list pipelines"}},
                    {"label": "Create your first one",
                     "action": {"kind": "ask", "prompt": "how to build a pipeline"}},
                    {"label": "Open editor",
                     "action": {"kind": "navigate", "page": "editor"}},
                ])
            elif kind == "connection":
                intro = (
                    "I'm on the Connections page but nothing's in view yet. "
                    "Want to:"
                )
                chips = next_actions_card(actions=[
                    {"label": "List all connections", "icon": "→", "style": "primary",
                     "action": {"kind": "ask", "prompt": "list connections"}},
                    {"label": "Add a new connection",
                     "action": {"kind": "ask", "prompt": "how to add a connection"}},
                    {"label": "How to store credentials?",
                     "action": {"kind": "ask", "prompt": "how to store credentials"}},
                ])
            else:
                intro = (
                    f"I don't see any {label_kind}s in view right now. Want "
                    f"to dig into something specific?"
                )
                chips = next_actions_card(actions=[
                    {"label": "Workspace overview", "style": "primary",
                     "action": {"kind": "ask", "prompt": "give me an overview"}},
                ])
            return f"{intro}\n\n{chips}"
        # On a different page — navigate AND continue the conversation
        # on the new page. ``then_ask`` is a follow-up prompt the
        # frontend submits automatically after the page change settles.
        # Without it the bot would go silent post-navigation, leaving
        # the user to retype their original question.
        followup_by_kind = {
            "execution":  "which run are you asking about?",
            "pipeline":   "which pipeline are you asking about?",
            "connection": "which connection are you asking about?",
        }
        then_ask = followup_by_kind.get(kind, f"which {label_kind}?")
        intro = (
            f"I can't see any {label_kind}s from this page. Open "
            f"**{nav_page.title()}** and I'll ask you which one — "
            f"or quote a name like `\"daily-ingest\"`."
        )
        chips = next_actions_card(actions=[
            {
                "label": f"Open {nav_page.title()}",
                "icon": "📂",
                "style": "primary",
                "action": {
                    "kind": "navigate",
                    "page": nav_page,
                    "then_ask": then_ask,
                },
            },
        ])
        return f"{intro}\n\n{chips}"

    title = f"Which {label_kind}?"
    subtitle = f"I see {len(same_kind)} on this page"
    return choices_card(
        title=title,
        subtitle=subtitle,
        items=same_kind,
        intent_name=intent_name,
        fallback="Or click an item on the page first.",
    )


def _suggested_followup(prompt: str, kind: str, name: str) -> str:
    """Build the canonical follow-up prompt the router will recognise.

    Re-uses the user's verb if recognisable, else falls back to a
    template per kind. Always quotes the name so the resolver picks
    it up unambiguously.
    """
    p = prompt.lower()
    quoted = f'"{name}"'
    if "why" in p and ("fail" in p or "error" in p or "broken" in p):
        return f'why did {quoted} fail'
    if "explain" in p:
        return f'explain {quoted}'
    if "what's wrong" in p or "whats wrong" in p:
        return f'whats wrong with {quoted}'
    if kind == "execution":
        return f'show me {quoted}'
    if kind == "pipeline":
        return f'about {quoted}'
    return f'about {quoted}'
