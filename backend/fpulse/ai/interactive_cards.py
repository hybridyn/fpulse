"""
Interactive card builders — May 5 2026.

The chat panel renders ``[CARD]{...}[/CARD]`` blocks as structured UI.
This module owns the JSON shapes for the new click-driven cards:

  - ``choices``       — "Pick one" with clickable rows. Used by clarify
                        and any place we need entity disambiguation.
  - ``next_actions``  — Chip row appended to a successful answer. Each
                        chip is a one-click follow-up (run / cancel /
                        explain / open-page).
  - ``confirm``       — Yes/No (or Sample/Live/Cancel) gate before any
                        write action. Replaces the older confirm_action
                        text-only render.

Every chip carries an ``action`` payload that travels to
``POST /api/ai/agent/action`` verbatim. The action endpoint dispatches
without going through the router or the LLM.

Action shapes (see also api/agent_action.py):
  {kind: "slot_fill",  intent_name, entity_kind, entity_id, entity_name}
  {kind: "fast_action", verb, entity_kind, entity_id, entity_name}
  {kind: "execute",    endpoint, method, query, body}
  {kind: "ask",        prompt}    # re-submits as a fresh router pass
  {kind: "navigate",   page, params}    # frontend opens a page
"""

from __future__ import annotations

import json
from typing import Any


def _emit(card: dict[str, Any]) -> str:
    """Render a card dict as the inline ``[CARD]{json}[/CARD]`` token."""
    return f"[CARD]{json.dumps(card)}[/CARD]"


# ─────────────────────────────────────────────────────────────────────
# choices — "pick one"
# ─────────────────────────────────────────────────────────────────────


def choices_card(
    *,
    title: str,
    subtitle: str | None,
    items: list[dict[str, Any]],
    intent_name: str,
    fallback: str | None = None,
) -> str:
    """Render an entity-pick card.

    ``items``: list of visible_items dicts (id, name, status, kind, meta).
    Each becomes a clickable row whose action slot-fills the pending intent.
    """
    rows: list[dict[str, Any]] = []
    for it in items[:10]:
        kind = it.get("kind") or "entity"
        name = it.get("name") or it.get("id") or "?"
        status = (it.get("status") or "").lower()
        meta = it.get("meta") or {}
        emoji = {
            "failed": "🔴", "error": "🔴",
            "success": "🟢", "published": "🟢",
            "running": "🟡", "queued": "🟡",
            "draft": "⚪", "archived": "⚫",
        }.get(status, "·")
        sub_bits: list[str] = []
        if status:
            sub_bits.append(status)
        last = meta.get("last_run") or meta.get("started_at")
        if last:
            sub_bits.append(f"last {last}")
        t = meta.get("type")
        if t:
            sub_bits.append(str(t))
        rows.append({
            "label": name,
            "emoji": emoji,
            "subtitle": " · ".join(sub_bits) if sub_bits else None,
            "action": {
                "kind": "slot_fill",
                "intent_name": intent_name,
                "entity_kind": kind,
                "entity_id": str(it.get("id") or ""),
                "entity_name": str(name),
            },
        })
    card = {
        "kind": "card",
        "type": "choices",
        "title": title,
    }
    if subtitle:
        card["subtitle"] = subtitle
    card["choices"] = rows
    if fallback:
        card["fallback"] = fallback
    return _emit(card)


# ─────────────────────────────────────────────────────────────────────
# next_actions — chip row appended to any answer
# ─────────────────────────────────────────────────────────────────────


def next_actions_card(
    *,
    actions: list[dict[str, Any]],
) -> str:
    """Append a chip row.

    Each ``action`` dict needs at least ``label`` + ``action`` (the action
    payload that will be POSTed when clicked). Optional ``style`` hints
    'primary' / 'danger' for visual emphasis.
    """
    chips: list[dict[str, Any]] = []
    for a in actions[:6]:
        if "label" not in a or "action" not in a:
            continue
        chip = {"label": a["label"], "action": a["action"]}
        if "style" in a:
            chip["style"] = a["style"]
        if "icon" in a:
            chip["icon"] = a["icon"]
        chips.append(chip)
    if not chips:
        return ""
    return _emit({"kind": "card", "type": "next_actions", "chips": chips})


def chips_for_pipeline(*, entity_id: str, entity_name: str, status: str = "") -> list[dict[str, Any]]:
    """Curated next-action chip set after the user has pinned a pipeline.
    Status drives the order and inclusion (no Retry chip on a 'success' run)."""
    base = [
        {
            "label": "Run it",
            "icon": "▶",
            "style": "primary",
            "action": {
                "kind": "fast_action", "verb": "run",
                "entity_kind": "pipeline", "entity_id": entity_id, "entity_name": entity_name,
            },
        },
        {
            "label": "Open executions",
            "icon": "📜",
            "action": {
                "kind": "navigate",
                "page": "executions",
                "params": {"workflow_id": entity_id},
            },
        },
    ]
    if status.lower() in ("failed", "error"):
        base.insert(1, {
            "label": "Why did it fail?",
            "icon": "❓",
            "action": {
                "kind": "fast_action", "verb": "diagnose",
                "entity_kind": "pipeline", "entity_id": entity_id, "entity_name": entity_name,
            },
        })
    return base


def chips_for_execution(*, entity_id: str, entity_name: str, status: str, workflow_id: str = "") -> list[dict[str, Any]]:
    chips: list[dict[str, Any]] = []
    if status.lower() in ("running", "queued"):
        chips.append({
            "label": "Cancel run",
            "icon": "⏹",
            "style": "danger",
            "action": {
                "kind": "fast_action", "verb": "cancel",
                "entity_kind": "execution", "entity_id": entity_id, "entity_name": entity_name,
            },
        })
    chips.append({
        "label": "Open in Executions",
        "icon": "📜",
        "action": {
            "kind": "navigate", "page": "executions",
            "params": {"execution_id": entity_id, "workflow_id": workflow_id} if workflow_id else {"execution_id": entity_id},
        },
    })
    return chips


def chips_for_connection(*, entity_id: str, entity_name: str) -> list[dict[str, Any]]:
    return [
        {
            "label": "Test it",
            "icon": "🧪",
            "style": "primary",
            "action": {
                "kind": "fast_action", "verb": "test",
                "entity_kind": "connection", "entity_id": entity_id, "entity_name": entity_name,
            },
        },
        {
            "label": "Open Connections",
            "icon": "🔌",
            "action": {"kind": "navigate", "page": "connections"},
        },
    ]


# ─────────────────────────────────────────────────────────────────────
# confirm — yes/no/cancel gate before write actions
# ─────────────────────────────────────────────────────────────────────


def confirm_card(
    *,
    title: str,
    summary: str,
    options: list[dict[str, Any]],
    tier: str = "safe_write",
    details: list[dict[str, str]] | None = None,
) -> str:
    """Render a confirmation card. ``options`` is a list of buttons,
    each with ``label`` + ``action`` + optional ``style`` (primary/danger)."""
    card: dict[str, Any] = {
        "kind": "card",
        "type": "confirm",
        "title": title,
        "summary": summary,
        "tier": tier,
        "options": options[:4],
    }
    if details:
        card["details"] = details
    return _emit(card)
