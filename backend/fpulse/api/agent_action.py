"""
POST /api/ai/agent/action — structured-action endpoint.

Sister of /api/ai/agent. Accepts a typed action object (the same payload
emitted by interactive cards: ``slot_fill`` / ``fast_action`` /
``execute`` / ``ask`` / ``navigate``) and dispatches WITHOUT going
through the router or the LLM.

Returns the same ``AgentResponse`` shape so the chat panel renders it
identically. The ``case_file`` is read from the request, mutated based
on the action, and returned.

Why a separate endpoint:
  * Bypasses router/clarify/single-shot — the user already chose.
  * Keeps the clicked action in trace logs as a distinct intent
    (``action.slot_fill.run_pipeline`` etc.) for analytics.
  * Lets us add per-action authz and rate limits later without
    touching the main agent flow.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from fpulse.ai.context import PageContext
from fpulse.ai.foundation import get_provider_info
from fpulse.ai.tools.base import ToolContext

logger = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────
# Wire types
# ─────────────────────────────────────────────────────────────────────


class ActionPayload(BaseModel):
    kind: str
    # slot_fill / fast_action
    intent_name: str | None = None
    verb: str | None = None
    entity_kind: str | None = None
    entity_id: str | None = None
    entity_name: str | None = None
    # execute
    endpoint: str | None = None
    method: str | None = None
    query: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)
    # ask
    prompt: str | None = None
    # navigate
    page: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    # Optional follow-up prompt the frontend submits after navigate
    # settles. Lets the bot continue the conversation on the new page.
    then_ask: str | None = None


class PageContextRequestLite(BaseModel):
    page: str = "unknown"
    visible_ids: list[str] = Field(default_factory=list)
    selected_ids: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    environment: str = "dev"
    visible_items: list[dict[str, Any]] = Field(default_factory=list)


class ActionRequest(BaseModel):
    action: ActionPayload
    page_context: PageContextRequestLite
    dialogue_state: dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# AI action gate (Phase 5) — execution actions require server-side
# permission, never chip/prompt trust. Read/draft actions are unaffected.
# ─────────────────────────────────────────────────────────────────────

_AI_EXECUTION_HANDLER_NAMES = {
    "direct_run_pipeline",
    "direct_cancel_execution",
    "direct_test_connection",
}
_AI_WRITE_ROLES = {"developer", "admin", "super_admin"}


def _ai_execution_denied(user) -> str | None:
    """Refusal message if the assistant may NOT run execution actions for
    this caller, else None. Read/draft actions never reach this gate."""
    from fpulse import runtime_config
    if not runtime_config.AI_ALLOW_EXECUTE:
        return (
            "The assistant is in read/draft mode, so it won't run, cancel, "
            "or test on your behalf. An operator can enable execution with "
            "FPULSE_AI_ALLOW_EXECUTE=1."
        )
    # When the server is exposed, require a real write role — the chip the
    # user clicked is not proof they're allowed to execute.
    if runtime_config.IS_SERVER_MODE and getattr(user, "role", None) not in _AI_WRITE_ROLES:
        return (
            "You don't have permission to run, cancel, or test pipelines from "
            "the assistant — a developer role or higher is required."
        )
    return None


async def _dispatch_ai_handler(handler, prompt, fast_ctx, *, user, workspace_id, verb):
    """Invoke an AI-proposed handler, gating + auditing execution actions."""
    if getattr(handler, "__name__", "") in _AI_EXECUTION_HANDLER_NAMES:
        denied = _ai_execution_denied(user)
        if denied is not None:
            return denied
        try:
            from fpulse.audit import audit_action, actor_for
            audit_action(
                action=f"ai.action.{verb}",
                resource_type="ai_action",
                resource_id=str(verb),
                actor=actor_for(user),
                workspace_id=workspace_id,
                details={"handler": getattr(handler, "__name__", "")},
            )
        except Exception:
            pass
    return await handler(prompt, fast_ctx)


# ─────────────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────────────


@router.post("/api/ai/agent/action")
async def agent_action(req: ActionRequest, request: Request) -> dict[str, Any]:
    """Dispatch a structured action. NO router, NO LLM."""
    from fpulse.auth.deps import current_user_optional, current_workspace_id
    from fpulse.ai.dialogue_state import (
        ActiveEntity,
        describe_entity,
        diagnose_failure,
        parse_state,
        serialize_state,
        with_resolved_entity,
    )
    from fpulse.ai.direct_actions import (
        direct_cancel_execution,
        direct_run_pipeline,
        direct_test_connection,
    )

    user = current_user_optional(request)
    user_id = getattr(user, "id", None) if user else None
    workspace_id = current_workspace_id(request) or "default"
    info = get_provider_info(user_id=user_id, workspace_id=workspace_id)

    fast_ctx = ToolContext(
        tenant_id=workspace_id, user_id=user_id, workspace_id=workspace_id,
        environment=req.page_context.environment, dry_run=False,
        selected_ids=tuple(req.page_context.selected_ids),
        visible_ids=tuple(req.page_context.visible_ids),
        page=req.page_context.page or "",
        visible_items=tuple(req.page_context.visible_items[:50]),
    )
    incoming = parse_state(req.dialogue_state)
    case_file_out = serialize_state(incoming)

    a = req.action
    text: str | None = None
    intent_label = f"action.{a.kind}"

    # ── slot_fill: user clicked an entity in a clarify card ──────────
    if a.kind == "slot_fill" and a.intent_name and a.entity_id and a.entity_name:
        new_state = with_resolved_entity(
            incoming,
            ActiveEntity(
                kind=a.entity_kind or "entity",
                id=a.entity_id,
                name=a.entity_name,
            ),
        )
        case_file_out = serialize_state(new_state)
        # Synth a quoted-name prompt for the downstream handler.
        synth = f'"{a.entity_name}"'
        SLOT = {
            "test_connection": (direct_test_connection, f"test {synth}"),
            "run_pipeline": (direct_run_pipeline, f"run {synth}"),
            "cancel_run": (direct_cancel_execution, f"cancel {synth}"),
            "view_pipeline": (describe_entity, f"about {synth}"),
            "diagnose_run": (describe_entity, f"about {synth}"),
            "diagnose_failure": (diagnose_failure, f"why did {synth} fail"),
        }
        entry = SLOT.get(a.intent_name)
        if entry is not None:
            handler, prompt = entry
            try:
                text = await _dispatch_ai_handler(
                    handler, prompt, fast_ctx,
                    user=user, workspace_id=workspace_id, verb=a.intent_name,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("action slot_fill %s failed: %s", a.intent_name, exc)
        intent_label = f"action.slot_fill.{a.intent_name}"

    # ── fast_action: a chip click after a successful answer ─────────
    elif a.kind == "fast_action" and a.verb and a.entity_id and a.entity_name:
        new_state = with_resolved_entity(
            incoming,
            ActiveEntity(
                kind=a.entity_kind or "entity",
                id=a.entity_id,
                name=a.entity_name,
            ),
        )
        case_file_out = serialize_state(new_state)
        synth = f'"{a.entity_name}"'
        VERB = {
            "run": (direct_run_pipeline, f"run {synth}"),
            "execute": (direct_run_pipeline, f"run {synth}"),
            "test": (direct_test_connection, f"test {synth}"),
            "verify": (direct_test_connection, f"test {synth}"),
            "cancel": (direct_cancel_execution, f"cancel {synth}"),
            "stop": (direct_cancel_execution, f"cancel {synth}"),
            "diagnose": (diagnose_failure, f"why did {synth} fail"),
            "describe": (describe_entity, f"about {synth}"),
        }
        entry = VERB.get(a.verb)
        if entry is not None:
            handler, prompt = entry
            try:
                text = await _dispatch_ai_handler(
                    handler, prompt, fast_ctx,
                    user=user, workspace_id=workspace_id, verb=a.verb,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("action fast_action %s failed: %s", a.verb, exc)
        intent_label = f"action.fast.{a.verb}"

    # ── execute: a Confirm button — actually fire the API call ──────
    elif a.kind == "execute" and a.endpoint:
        # Phase-1 implementation: don't proxy yet — return a clear
        # message saying what would happen. The frontend can do the
        # actual POST itself with the user's auth header. Keeps the
        # confirm flow safe and reviewable.
        text = (
            f"Confirmed. Frontend will fire `{a.method or 'POST'} {a.endpoint}`.\n"
            "_(Wire-through implementation is the next step — Phase 2.)_"
        )
        intent_label = "action.execute"

    # ── ask: re-submit as a fresh router pass (hidden from chat) ────
    elif a.kind == "ask" and a.prompt:
        # Caller-side: the frontend should issue a normal /agent/stream
        # call with `a.prompt`. This branch exists so the action
        # endpoint can return a hint when a chip just wants to seed
        # the next user turn. Most chips won't use this kind.
        text = f"Asked: {a.prompt}"
        intent_label = "action.ask"

    # ── navigate: handled entirely client-side; we just ack ─────────
    elif a.kind == "navigate" and a.page:
        text = f"Opening **{a.page}**…"
        intent_label = "action.navigate"

    if text is None:
        raise HTTPException(status_code=400, detail=f"Unsupported action payload: {a.kind}")

    return {
        "run_id": str(_uuid.uuid4()),
        "final_text": text,
        "outcome": "success",
        "iterations": 0,
        "elapsed_ms": 0,
        "steps": [],
        "tool_results": [],
        "cost": {
            "tokens_in": 0, "tokens_out": 0, "estimated_usd": 0.0,
            "provider": info.provider, "model": info.model,
        },
        "instant": True,
        "instant_intent": intent_label,
        "instant_confidence": 1.0,
        "served_from_page": True,
        "case_file": case_file_out,
    }
