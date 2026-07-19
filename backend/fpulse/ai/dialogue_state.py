"""
Dialogue state — Layer 2 of the F-Pulse Copilot context model.

Picks up where Layer 1 (page context, fresh per turn) ends. Tracks the
*case file* that grows across turns of one conversation: which entity
the user is acting on, which intent they started, and what slot is
still missing before that intent can fire.

Modeled on dialogue-state-tracking systems used in production support
bots (Kodee, Microsoft Copilot Studio slot-filling, etc.). v1 is
intentionally minimal — just two structured slots — and reviewer-
locked rules enforce determinism (no LLM decides whether a short reply
fills a slot; the accept-list does).

Public surface:
  * ``DialogueState`` — frozen dataclass mirrored on the wire.
  * ``parse_state(payload) -> DialogueState`` — request → typed.
  * ``serialize_state(state) -> dict`` — typed → response.
  * ``try_slot_fill(prompt, state, ctx) -> SlotFillResult | None`` —
    the deterministic short-reply matcher.
  * ``update_state(state, intent_name, target, pending_slot) ->
    DialogueState`` — pure state-transition function.
  * ``substitute_reference(prompt, state) -> str`` — replaces vague
    refs ("it", "this") with the active entity's name when set.

All functions are pure. Side-effect-free. Safe in tests with no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from fpulse.ai.tools.base import ToolContext


# ─────────────────────────────────────────────────────────────────────
# Wire types
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ActiveEntity:
    kind: str   # "pipeline" | "execution" | "connection"
    id: str
    name: str


@dataclass(frozen=True)
class ActiveIntent:
    """The intent the user started but hasn't finished. ``missing_slot``
    is the field the system is still waiting on — typically "entity"
    when the clarify layer fired with multiple candidates."""

    name: str           # e.g. "diagnose_failure", "run_pipeline", "cancel_run"
    missing_slot: str | None = None  # None when ready to fire


@dataclass(frozen=True)
class DialogueState:
    active_entity: ActiveEntity | None = None
    active_intent: ActiveIntent | None = None


EMPTY_STATE = DialogueState()


def parse_state(payload: dict[str, Any] | None) -> DialogueState:
    """Build a DialogueState from a request payload. Tolerant: any
    missing/malformed field falls back to None."""
    if not payload or not isinstance(payload, dict):
        return EMPTY_STATE
    ent_raw = payload.get("active_entity")
    int_raw = payload.get("active_intent")
    ent = None
    if isinstance(ent_raw, dict) and ent_raw.get("id") and ent_raw.get("kind"):
        ent = ActiveEntity(
            kind=str(ent_raw["kind"]),
            id=str(ent_raw["id"]),
            name=str(ent_raw.get("name") or ent_raw["id"]),
        )
    intent = None
    if isinstance(int_raw, dict) and int_raw.get("name"):
        intent = ActiveIntent(
            name=str(int_raw["name"]),
            missing_slot=int_raw.get("missing_slot") or None,
        )
    return DialogueState(active_entity=ent, active_intent=intent)


def serialize_state(state: DialogueState) -> dict[str, Any]:
    out: dict[str, Any] = {"active_entity": None, "active_intent": None}
    if state.active_entity is not None:
        out["active_entity"] = {
            "kind": state.active_entity.kind,
            "id": state.active_entity.id,
            "name": state.active_entity.name,
        }
    if state.active_intent is not None:
        out["active_intent"] = {
            "name": state.active_intent.name,
            "missing_slot": state.active_intent.missing_slot,
        }
    return out


# ─────────────────────────────────────────────────────────────────────
# Slot-fill matcher — deterministic, accept-list only
# ─────────────────────────────────────────────────────────────────────


# Reviewer-locked accept-list. Any reply NOT matching one of these
# patterns falls through to fresh routing — we never guess.
_AFFIRMATIVE = frozenset({
    "yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "do it",
    "go", "go ahead", "proceed", "confirm", "confirmed", "approve", "approved",
})

_ORDINAL_RE = re.compile(r"""^(?:the\s+)?(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|1|2|3|4|5)\b""", re.I)
_ORDINAL_TO_INDEX = {
    "first": 0, "1st": 0, "1": 0,
    "second": 1, "2nd": 1, "2": 1,
    "third": 2, "3rd": 2, "3": 2,
    "fourth": 3, "4th": 3, "4": 3,
    "fifth": 4, "5th": 4, "5": 4,
}


@dataclass
class SlotFillResult:
    """The slot-fill matcher's decision."""

    entity: ActiveEntity
    intent_name: str
    fired_intent_text: str       # the synthetic prompt to re-route through
    reason: str                  # telemetry tag — "ordinal" / "name" / "yes"


def _short_reply(prompt: str) -> bool:
    """A reply is 'short' if it has ≤ 4 words. Anything longer is
    treated as a fresh query, even if it starts with an affirmative."""
    return 0 < len(prompt.strip().split()) <= 4


def _candidates_for(state: DialogueState, ctx: ToolContext) -> list[dict[str, Any]]:
    """Items in `ctx.visible_items` matching the kind we're waiting on."""
    if state.active_intent is None:
        return []
    kind = _intent_to_kind(state.active_intent.name)
    if kind is None:
        return []
    return [it for it in ctx.visible_items if (it.get("kind") or "") == kind]


def _intent_to_kind(intent_name: str) -> str | None:
    table = {
        "diagnose_failure": "pipeline",
        "run_pipeline": "pipeline",
        "view_pipeline": "pipeline",
        "cancel_run": "execution",
        "diagnose_run": "execution",
        "test_connection": "connection",
    }
    # Default per kind suffix in name: "*_pipeline" / "*_run" / "*_connection".
    if intent_name in table:
        return table[intent_name]
    if intent_name.endswith("_pipeline"):
        return "pipeline"
    if intent_name.endswith("_run") or intent_name.endswith("_execution"):
        return "execution"
    if intent_name.endswith("_connection"):
        return "connection"
    return None


def try_slot_fill(
    prompt: str,
    state: DialogueState,
    ctx: ToolContext,
) -> SlotFillResult | None:
    """Deterministic slot-fill matcher.

    Fires only when ALL conditions hold:
      * ``state.active_intent`` exists and has a missing slot.
      * Prompt is a short reply (≤ 4 words).
      * Reply matches the accept-list:
        - exact name of a candidate (case-insensitive)
        - ordinal ("first", "1", "the second")
        - affirmative ("yes") AND exactly one candidate exists

    Anything else returns None — caller treats as a fresh query.
    """
    if state.active_intent is None or state.active_intent.missing_slot != "entity":
        return None
    if not prompt or not _short_reply(prompt):
        return None

    p = prompt.strip().rstrip(".?!").lower()
    candidates = _candidates_for(state, ctx)
    if not candidates:
        return None

    # 1. Exact / substring name match (case-insensitive).
    for it in candidates:
        nm = (it.get("name") or "").lower()
        if nm and (p == nm or nm in p):
            return _build_result(it, state, "name")

    # 2. Ordinal — "first" / "1" / "the third".
    m = _ORDINAL_RE.match(p)
    if m:
        idx = _ORDINAL_TO_INDEX.get(m.group(1).lower())
        if idx is not None and idx < len(candidates):
            return _build_result(candidates[idx], state, "ordinal")

    # 3. Affirmative — only when there's exactly one candidate (no
    #    ambiguity to resolve).
    if p in _AFFIRMATIVE and len(candidates) == 1:
        return _build_result(candidates[0], state, "yes")

    return None


def _build_result(item: dict[str, Any], state: DialogueState, reason: str) -> SlotFillResult:
    kind = (item.get("kind") or _intent_to_kind(state.active_intent.name)  # type: ignore[union-attr]
            or "entity")
    name = item.get("name") or item.get("id") or "?"
    entity = ActiveEntity(kind=kind, id=str(item.get("id") or ""), name=str(name))
    intent_name = state.active_intent.name  # type: ignore[union-attr]
    fired = _resynthesize_prompt(intent_name, name)
    return SlotFillResult(
        entity=entity,
        intent_name=intent_name,
        fired_intent_text=fired,
        reason=reason,
    )


def _resynthesize_prompt(intent_name: str, name: str) -> str:
    """Build a canonical prompt for the now-resolved intent. The fast-
    lane router and direct-action layer recognise these phrasings."""
    quoted = f'"{name}"'
    table = {
        "diagnose_failure": f"why did {quoted} fail",
        "run_pipeline": f"run {quoted}",
        "view_pipeline": f"about {quoted}",
        "cancel_run": f"cancel {quoted}",
        "diagnose_run": f"explain {quoted}",
        "test_connection": f"test {quoted}",
    }
    return table.get(intent_name, f"about {quoted}")


# ─────────────────────────────────────────────────────────────────────
# Reference resolution — substitute "it" / "this" with active entity
# ─────────────────────────────────────────────────────────────────────


_VAGUE_REF_RE = re.compile(
    r"""\b(it|this(?:\s+(?:pipeline|workflow|run|execution|connection))?|"""
    r"""that(?:\s+(?:pipeline|workflow|run|execution|connection))?|"""
    r"""the\s+same(?:\s+one)?)\b""",
    re.I,
)


def substitute_reference(prompt: str, state: DialogueState) -> str:
    """If the prompt has a vague reference and an active entity is
    pinned, rewrite the reference inline. Otherwise return the prompt
    unchanged.

    Conservative: only substitutes when the entity name is a single
    proper noun-ish token. Multi-word names get quoted to keep the
    downstream matchers happy.
    """
    if state.active_entity is None:
        return prompt
    name = state.active_entity.name
    if not name:
        return prompt
    quoted = f'"{name}"'

    def _sub(_m: re.Match[str]) -> str:
        return quoted

    return _VAGUE_REF_RE.sub(_sub, prompt, count=1)


# ─────────────────────────────────────────────────────────────────────
# State updates — pure transition functions
# ─────────────────────────────────────────────────────────────────────


def with_pending_clarification(
    state: DialogueState, intent_name: str,
) -> DialogueState:
    """Record that we asked for clarification on ``intent_name``."""
    return replace(
        state,
        active_intent=ActiveIntent(name=intent_name, missing_slot="entity"),
    )


def with_resolved_entity(
    state: DialogueState, entity: ActiveEntity,
) -> DialogueState:
    """Pin the entity, mark intent slot filled (clears ``missing_slot``)."""
    intent = state.active_intent
    if intent is not None:
        intent = ActiveIntent(name=intent.name, missing_slot=None)
    return DialogueState(active_entity=entity, active_intent=intent)


def cleared() -> DialogueState:
    return EMPTY_STATE


# Map fast-lane intent_name → dialogue intent_name. Lets us promote a
# fast-lane match (which doesn't know about Layer 2) into a sticky
# active_intent the next turn can build on.
_FASTLANE_TO_INTENT = {
    "failed_executions": "diagnose_run",
    "running_now": "diagnose_run",
    "list_pipelines": "view_pipeline",
    "list_connections": "test_connection",
    "direct.run_pipeline": "run_pipeline",
    "direct.cancel_execution": "cancel_run",
    "direct.test_connection": "test_connection",
    "clarify": None,  # clarify already sets pending separately
}


def promote_intent(fastlane_intent_name: str) -> str | None:
    return _FASTLANE_TO_INTENT.get(fastlane_intent_name)


async def diagnose_failure(prompt: str, ctx: ToolContext) -> str:
    """Deterministic failure-context renderer + next-action chips."""
    from fpulse.ai.direct_actions import resolve_target
    from fpulse.ai.interactive_cards import (
        chips_for_pipeline,
        chips_for_execution,
        next_actions_card,
    )

    target = resolve_target(ctx, "pipeline", prompt)
    target_kind = "pipeline"
    if target is None:
        target = resolve_target(ctx, "execution", prompt)
        target_kind = "execution"
    if target is None:
        return "I couldn't pin down which entity you mean — try clicking it on the page."
    name = target.get("name") or target.get("id")
    status = (target.get("status") or "unknown").lower()
    meta = target.get("meta") or {}
    last_run = meta.get("last_run") or meta.get("started_at") or meta.get("completed_at")
    pid = str(target.get("id") or "")
    workflow_id = str((meta.get("workflow_id") if isinstance(meta, dict) else "") or pid)

    if status not in ("failed", "error"):
        text = (
            f"**{name}** is currently `{status}`, not failed."
        )
        chips = chips_for_pipeline(entity_id=pid, entity_name=str(name), status=status) \
            if target_kind == "pipeline" \
            else chips_for_execution(entity_id=pid, entity_name=str(name), status=status, workflow_id=workflow_id)
        return f"{text}\n\n{next_actions_card(actions=chips)}"

    parts = [f"**{name}** is in a `{status}` state."]
    if last_run:
        parts.append(f"Last activity: **{last_run}**.")
    parts.append("")
    parts.append(
        "I can't read the underlying logs from this view — opening the executions "
        "page will show the step that failed and its error message."
    )
    chips = chips_for_pipeline(entity_id=pid, entity_name=str(name), status=status) \
        if target_kind == "pipeline" \
        else chips_for_execution(entity_id=pid, entity_name=str(name), status=status, workflow_id=workflow_id)
    return "\n".join(parts) + "\n\n" + next_actions_card(actions=chips)


async def describe_entity(prompt: str, ctx: ToolContext) -> str:
    """Render a quick info summary + next-action chips for the entity
    referenced in ``prompt``. No tool call, no LLM."""
    from fpulse.ai.direct_actions import resolve_target
    from fpulse.ai.interactive_cards import (
        chips_for_connection,
        chips_for_execution,
        chips_for_pipeline,
        next_actions_card,
    )
    found_kind: str | None = None
    target: dict[str, Any] | None = None
    for kind in ("pipeline", "execution", "connection"):
        target = resolve_target(ctx, kind, prompt)
        if target is not None:
            found_kind = kind
            break
    if target is None or found_kind is None:
        return "I couldn't pin down which entity you mean — try clicking it on the page."

    name = target.get("name") or target.get("id")
    status = target.get("status") or "—"
    meta = target.get("meta") or {}
    eid = str(target.get("id") or "")
    bits = [f"**{name}** · `{found_kind}` · status: `{status}`"]
    chips: list[dict[str, Any]] = []
    if found_kind == "pipeline":
        steps = meta.get("steps")
        last_run = meta.get("last_run") or meta.get("last_run_at")
        ver = meta.get("version")
        if steps is not None:
            bits.append(f"- Steps: **{int(steps)}**")
        if ver is not None:
            bits.append(f"- Version: v{ver}")
        if last_run:
            bits.append(f"- Last run: {last_run}")
        chips = chips_for_pipeline(entity_id=eid, entity_name=str(name), status=str(status))
    elif found_kind == "execution":
        wid = str(meta.get("workflow_id") or "")
        dur = meta.get("duration_ms")
        if wid:
            bits.append(f"- Workflow: `{wid}`")
        if isinstance(dur, (int, float)):
            bits.append(f"- Duration: {int(dur)} ms")
        chips = chips_for_execution(entity_id=eid, entity_name=str(name), status=str(status), workflow_id=wid)
    elif found_kind == "connection":
        t = meta.get("type")
        env = meta.get("environment")
        if t:
            bits.append(f"- Type: **{t}**")
        if env:
            bits.append(f"- Environment: {env}")
        chips = chips_for_connection(entity_id=eid, entity_name=str(name))
    return "\n".join(bits) + "\n\n" + next_actions_card(actions=chips)


def infer_intent_from_prompt(prompt: str) -> str:
    """Map a free-text prompt to the dialogue intent name we should
    pin when clarification fires. Conservative — defaults to a
    generic "view" intent if nothing matches so Turn 2 still threads
    the entity through."""
    p = prompt.lower()
    if "fail" in p or "error" in p or "broken" in p or "wrong" in p:
        return "diagnose_failure"
    if "run " in p or "execute" in p or "trigger" in p or "kick off" in p:
        return "run_pipeline"
    if "cancel" in p or "stop" in p or "kill" in p or "abort" in p:
        return "cancel_run"
    if "test " in p or "verify" in p or "ping" in p:
        return "test_connection"
    return "view_pipeline"
