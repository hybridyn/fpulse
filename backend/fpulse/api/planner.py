"""Planner API — convert intent to workflow IR via AI or rules."""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from fpulse.auth.deps import current_workspace_id
from fpulse.planner.rule_planner import RulePlanner
from fpulse.planner.templates import TEMPLATES, create_from_template
from fpulse.planner.ai_client import ai_generate_pipeline, ai_generate_text, is_ai_available
from fpulse.ir.schema import Workflow, Step, StepConnection, StepType, NodePosition

router = APIRouter(prefix="/api/planner", tags=["planner"])


def _safe_workspace_id(request: Request) -> str:
    """Wrap current_workspace_id so dep failures surface as readable
    HTTP errors — same pattern as api/workflows.py."""
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


class IntentRequest(BaseModel):
    intent: str


class ChatMessage(BaseModel):
    role: str = "user"
    content: str


def _ai_json_to_workflow(data: dict, workspace_id: str = "default") -> Workflow | None:
    """Convert AI-generated JSON to a proper Workflow IR.

    Stamps the provided ``workspace_id`` on the result so the new
    pipeline lands in the caller's tenant — the AI output has no
    concept of workspace, so the caller is authoritative here.
    """
    if not data or not data.get("steps"):
        return None

    steps: list[Step] = []
    connections: list[StepConnection] = []

    # Build steps with proper IDs
    step_ids: list[str] = []
    for i, raw_step in enumerate(data["steps"]):
        step_type = raw_step.get("type", "transform")
        # Validate step type
        try:
            st = StepType(step_type)
        except ValueError:
            st = StepType.TRANSFORM

        sid = uuid.uuid4().hex[:8]
        step_ids.append(sid)

        pos = raw_step.get("position", {})
        step = Step(
            id=sid,
            type=st,
            label=raw_step.get("label", step_type.replace("_", " ").title()),
            params=raw_step.get("params", {}),
            position=NodePosition(x=pos.get("x", i * 350), y=pos.get("y", 100)),
        )
        steps.append(step)

    # Build connections (AI returns index-based connections)
    for conn in data.get("connections", []):
        from_idx = conn.get("from_step", 0)
        to_idx = conn.get("to_step", 1)
        if 0 <= from_idx < len(step_ids) and 0 <= to_idx < len(step_ids):
            connections.append(StepConnection(
                from_step=step_ids[from_idx],
                to_step=step_ids[to_idx],
            ))

    return Workflow(
        name=data.get("name", "AI Pipeline"),
        description=data.get("description", ""),
        workspace_id=workspace_id or "default",
        steps=steps,
        connections=connections,
    )


@router.post("/generate")
async def generate_plan(
    body: IntentRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Convert natural language intent to a workflow IR.

    Any pipeline auto-saved from a high-confidence plan is stamped
    with the caller's workspace so AI-generated pipelines never leak
    across tenant boundaries.
    """
    planner = RulePlanner()
    result = planner.plan(body.intent)

    # Auto-save if high confidence
    if result.workflow and result.confidence >= 0.5:
        result.workflow.workspace_id = workspace_id or "default"
        from fpulse.main import app_state
        store = app_state["store"]
        version = store.save(result.workflow, change_summary=f"Generated from intent: {body.intent}")
        return {
            **result.dict(),
            "saved": True,
            "version": version.version,
        }

    return {**result.dict(), "saved": False}


@router.get("/templates")
async def list_templates():
    """List available pipeline templates."""
    return TEMPLATES


@router.post("/templates/{template_key}")
async def use_template(
    template_key: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create a workflow from a template inside the caller's
    workspace."""
    if template_key not in TEMPLATES:
        return {"error": f"Unknown template: {template_key}"}

    workflow = create_from_template(template_key)
    if not workflow:
        return {"error": "Failed to create workflow from template"}

    workflow.workspace_id = workspace_id or "default"

    from fpulse.main import app_state
    store = app_state["store"]
    version = store.save(workflow, change_summary=f"Created from template: {template_key}")

    return {
        "id": workflow.id,
        "version": version.version,
        "workflow": workflow.model_dump(mode="json"),
    }


_QUESTION_PREFIX = ("what", "how", "why", "when", "where", "who", "is", "are", "does", "do", "can", "should")
_GREETING_RE = re.compile(r"^(hi|hello|hey|howdy|good\s+(morning|afternoon|evening))[\s!.,]*$", re.IGNORECASE)
_EXPLAIN_HINTS = ("explain", "describe this", "show me the sql", "show sql", "show me the code", "what does this")


def _looks_like_question_or_greeting(text: str) -> bool:
    """Cheap intent-shape check so the chat endpoint can refuse to
    plan a pipeline from non-create messages. The rule planner has
    no concept of conversation state, so without this it would
    return its "Could not determine data source" boilerplate for
    every non-create message — confusing for users asking questions
    about the canvas they already have."""
    t = (text or "").strip().lower()
    if not t:
        return True
    if _GREETING_RE.match(t):
        return True
    first = t.split()[0] if t.split() else ""
    if first in _QUESTION_PREFIX and "?" in t:
        return True
    if first in _QUESTION_PREFIX[:8]:  # what/how/why/when/where/who/is/are without requiring '?'
        return True
    if any(h in t for h in _EXPLAIN_HINTS):
        return True
    return False


@router.post("/chat")
async def chat(
    messages: list[ChatMessage],
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Chat endpoint — uses AI when available, falls back to rule
    planner. Any workflow auto-saved from the chat is stamped with
    the caller's workspace."""
    if not messages:
        return {"reply": "What kind of data pipeline would you like to build?", "workflow": None}

    last_msg = messages[-1].content

    # Refuse to plan a pipeline from clearly-non-create messages.
    # Frontend already classifies intent client-side and only routes
    # create/modify here, but we add the same guard server-side as
    # defence-in-depth — protects future callers (CLI, scripts) from
    # the "Could not determine data source" garbage when they send a
    # question. The frontend's client-side question handler renders
    # the actual canvas-aware answer; we just have to NOT confuse it
    # with a planner failure.
    if _looks_like_question_or_greeting(last_msg):
        return {
            "reply": (
                "I help build and modify pipelines from natural language. "
                "Ask me to build a pipeline (e.g. *\"Load orders.csv, "
                "deduplicate by order_id, write to Parquet\"*), or use "
                "the canvas's own tools to inspect the current pipeline."
            ),
            "workflow": None,
            "confidence": 0,
            "ai_powered": False,
            "intent": "question_or_greeting",
        }

    # Clarify-before-draft (May 17 2026 — Phase 2A): if the prompt is a
    # build-pipeline request that's missing key config (source auth,
    # sink connection, write mode, etc.), ask the user the missing
    # questions BEFORE running the planner. Without this, the planner
    # silently guesses every gap and the user gets back a draft they
    # have to fix in 5 places.
    #
    # Skipped on follow-up turns where the previous assistant message
    # already asked questions — at that point the user is answering,
    # not making a new request. Detected by looking for our marker
    # phrase in the most-recent assistant message.
    is_followup = (
        len(messages) >= 2
        and messages[-2].role == "assistant"
        and "A few quick questions before I draft it" in (messages[-2].content or "")
    )
    if not is_followup:
        try:
            from fpulse.ai.clarify_draft import (
                detect_missing_draft_fields,
                render_clarification_card,
            )
            cset = detect_missing_draft_fields(last_msg)
            if cset is not None:
                return {
                    "reply": render_clarification_card(cset),
                    "workflow": None,
                    "confidence": 0,
                    "ai_powered": False,
                    "intent": "clarify_first",
                    "clarification": {
                        "source_type": cset.source_type,
                        "sink_type": cset.sink_type,
                        "detected_intent": cset.detected_intent,
                        "question_count": len(cset.questions),
                        # Phase 3.1 (May 18 2026) — structured questions
                        # for the frontend ClarifyCard. When a question
                        # bank is provided the chat panel renders chips
                        # instead of plain markdown, and submission
                        # produces a parseable answer string.
                        "questions": [
                            {
                                "field": q.field,
                                "question": q.question,
                                "chips": list(q.chips),
                                "required": q.required,
                            }
                            for q in cset.questions
                        ],
                    },
                }
        except Exception:
            # Clarification engine failures must never block the chat —
            # fall through to the existing planner path.
            pass
    else:
        # Phase 2F (May 18 2026) — answer the clarification.
        # We're on a follow-up turn. Look back through the conversation
        # to find the ORIGINAL build-pipeline prompt (the user-turn that
        # triggered the clarification card) so we can re-detect source
        # / sink types and match against an enterprise template. The
        # last_msg now holds the user's ANSWERS, which we parse and
        # use to populate template placeholders.
        try:
            from fpulse.ai.clarify_draft import detect_missing_draft_fields
            from fpulse.ai.clarify_to_template import (
                match_template_from_intent_and_answers,
                parse_answers_freeform,
                populate_template,
            )
            from fpulse.main import app_state

            # Walk back: previous assistant turn is the clarification card
            # (messages[-2]); the user turn before that (messages[-3]) is
            # the original build-pipeline request.
            original_prompt = ""
            if len(messages) >= 3 and messages[-3].role == "user":
                original_prompt = messages[-3].content or ""

            if original_prompt:
                cset = detect_missing_draft_fields(original_prompt)
                if cset is not None:
                    parsed = parse_answers_freeform(last_msg, cset.questions)
                    template_key = match_template_from_intent_and_answers(
                        cset.source_type, cset.sink_type, parsed.values,
                    )
                    if template_key:
                        wf = populate_template(template_key, parsed.values)
                        if wf is not None and wf.steps:
                            wf.workspace_id = workspace_id or "default"
                            store = app_state["store"]
                            version = store.save(
                                wf,
                                change_summary=f"Template ({template_key}) populated from clarify answers",
                            )
                            step_names = " → ".join(s.label for s in wf.steps)
                            reply = (
                                f"Drafted **{wf.name}** from the "
                                f"`{template_key}` template.\n\n"
                                f"Steps: {step_names}\n\n"
                            )
                            if parsed.matched_fields:
                                reply += (
                                    f"_Filled in_: {', '.join(parsed.matched_fields)}.\n"
                                )
                            if parsed.unmatched_fields:
                                reply += (
                                    "_Still need to fill in (left as "
                                    f"`<your-...>` placeholders)_: "
                                    f"{', '.join(parsed.unmatched_fields)}. "
                                    "Open the step configs to set these.\n"
                                )
                            reply += (
                                "\nReview the canvas, fill in any remaining "
                                "placeholders, then click **Run All** to test."
                            )
                            return {
                                "reply": reply,
                                "workflow": wf.model_dump(mode="json"),
                                "confidence": 0.9,
                                "ai_powered": False,
                                "intent": "template_populated",
                                "template_key": template_key,
                                "matched_fields": list(parsed.matched_fields),
                                "unmatched_fields": list(parsed.unmatched_fields),
                            }
        except Exception:
            # Template-bridge failures fall through to the rule planner.
            pass

    # Try AI first if available
    if is_ai_available():
        chat_msgs = [{"role": m.role, "content": m.content} for m in messages]
        ai_result = await ai_generate_pipeline(chat_msgs)

        if ai_result and ai_result.get("steps"):
            workflow = _ai_json_to_workflow(ai_result, workspace_id=workspace_id)
            if workflow and workflow.steps:
                from fpulse.main import app_state
                store = app_state["store"]
                version = store.save(workflow, change_summary=f"AI Chat: {last_msg[:100]}")

                explanation = ai_result.get("explanation", "")
                step_names = " → ".join(s.label for s in workflow.steps)
                reply = f"I've created your pipeline: **{workflow.name}**\n\n"
                reply += f"Steps: {step_names}\n\n"
                if explanation:
                    reply += f"{explanation}\n\n"
                reply += "The pipeline is on your canvas. Click any node to configure it, or click **Run All** to execute."

                return {
                    "reply": reply,
                    "workflow": workflow.model_dump(mode="json"),
                    "confidence": 0.95,
                    "ai_powered": True,
                }
        elif ai_result and ai_result.get("explanation"):
            return {
                "reply": ai_result["explanation"],
                "workflow": None,
                "confidence": 0,
                "ai_powered": True,
            }

    # Fall back to rule planner
    planner = RulePlanner()
    result = planner.plan(last_msg)

    if result.workflow and result.confidence >= 0.5:
        result.workflow.workspace_id = workspace_id or "default"
        from fpulse.main import app_state
        store = app_state["store"]
        version = store.save(result.workflow, change_summary=f"Chat: {last_msg[:100]}")

        reply = f"I've created a pipeline: **{result.workflow.name}**\n\n"
        reply += f"Steps: {' → '.join(s.label for s in result.workflow.steps)}\n\n"
        reply += "The pipeline is on your canvas. Click any node to configure it, or click **Run All** to execute."

        return {
            "reply": reply,
            "workflow": result.workflow.model_dump(mode="json"),
            "confidence": result.confidence,
            "ai_powered": False,
        }
    elif result.needs_ai:
        return {
            "reply": f"I understood parts of your request but need more detail.\n\n{result.explanation}\n\nCould you be more specific about the data source and operations?",
            "workflow": None,
            "confidence": result.confidence,
            "ai_powered": False,
        }
    else:
        return {
            "reply": "I'm not sure what pipeline you need. Try something like:\n\n- *\"Load orders.csv, deduplicate by order_id, output to parquet\"*\n- *\"Read sales.csv, filter amount > 100, calculate daily revenue\"*",
            "workflow": None,
            "confidence": 0,
            "ai_powered": False,
        }


@router.get("/ai-status")
async def ai_status():
    """Check if AI provider is configured."""
    return {"ai_available": is_ai_available()}


# ── Canvas chat (Assistant on the editor page) ─────────────────────────────
#
# The Assistant panel sends the full chat history PLUS a snapshot of the
# editor's canvas (nodes/edges/parameters/validation/status). The LLM gets
# a system prompt that frames it as a data-pipeline assistant aware of the
# user's current canvas. The reply is plain natural language — no JSON
# parsing — so questions like "what does this do" / "what are the issues" /
# "can we build a pipeline from sql for each table" feel like a real chat.
#
# Returns `{ reply, ai_powered }`. When `ai_available()` is False the
# frontend falls back to its client-side smart handlers (which are
# canvas-aware too, just keyword-driven).

class CanvasNode(BaseModel):
    id: str
    type: str
    label: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class CanvasEdge(BaseModel):
    source: str
    target: str
    condition: str | None = None


class CanvasParameter(BaseModel):
    name: str
    type: str | None = None
    default: Any | None = None
    required: bool | None = None


class CanvasValidationIssue(BaseModel):
    level: str
    step_id: str | None = None
    message: str


class CanvasContext(BaseModel):
    workflow_id: str | None = None
    workflow_name: str = "(unsaved)"
    status: str = "draft"
    version: int = 0
    nodes: list[CanvasNode] = Field(default_factory=list)
    edges: list[CanvasEdge] = Field(default_factory=list)
    parameters: list[CanvasParameter] = Field(default_factory=list)
    issues: list[CanvasValidationIssue] = Field(default_factory=list)


class CanvasChatRequest(BaseModel):
    messages: list[ChatMessage]
    canvas: CanvasContext


def _canvas_snapshot_for_prompt(c: CanvasContext) -> str:
    """Render the canvas as a compact, LLM-readable block. We cap each
    list at 50 entries to bound token cost — pipelines with hundreds of
    nodes will get summarised."""
    lines: list[str] = []
    lines.append(f"PIPELINE: {c.workflow_name!r} (id={c.workflow_id or 'unsaved'}, status={c.status}, v{c.version})")
    if not c.nodes:
        lines.append("CANVAS: empty (no nodes yet)")
        return "\n".join(lines)
    lines.append(f"CANVAS: {len(c.nodes)} step(s), {len(c.edges)} connection(s)")
    lines.append("")
    lines.append("STEPS:")
    for n in c.nodes[:50]:
        # Params printed shallowly so the LLM sees the connector_type /
        # file_path / table / condition without exploding token cost.
        keys = ", ".join(f"{k}={v!r}" for k, v in list((n.params or {}).items())[:6])
        lines.append(f"  - {n.id} [{n.type}] {n.label or ''} {('('+keys+')') if keys else ''}".rstrip())
    if len(c.nodes) > 50:
        lines.append(f"  ... and {len(c.nodes) - 50} more steps")
    lines.append("")
    lines.append("CONNECTIONS:")
    for e in c.edges[:50]:
        cond = f" [{e.condition}]" if e.condition else ""
        lines.append(f"  - {e.source} -> {e.target}{cond}")
    if len(c.edges) > 50:
        lines.append(f"  ... and {len(c.edges) - 50} more connections")
    if c.parameters:
        lines.append("")
        lines.append("PARAMETERS:")
        for p in c.parameters[:30]:
            req = " [required]" if p.required else ""
            default = f" default={p.default!r}" if p.default is not None else ""
            lines.append(f"  - {p.name} : {p.type or 'any'}{req}{default}")
    if c.issues:
        lines.append("")
        lines.append("VALIDATION ISSUES:")
        for i in c.issues[:20]:
            sid = f" ({i.step_id[:8]})" if i.step_id else ""
            lines.append(f"  - [{i.level}]{sid} {i.message}")
    return "\n".join(lines)


CANVAS_CHAT_SYSTEM_PROMPT = """You are F-Pulse Assistant, an expert data-engineering copilot embedded in F-Pulse's pipeline editor. The user is looking at a pipeline canvas and may ask questions about it, request changes, or describe new pipelines they want built.

Rules:
- Answer the user's QUESTION directly using the canvas snapshot you're given. Do not pretend the canvas is empty if it has nodes; do not fabricate node names.
- Be concise. Markdown is fine. Use bullets for lists. Code fences for SQL/JSON. No preamble like "Sure! I'd be happy to..." — go straight to the answer.
- When the user asks to BUILD or MODIFY a pipeline, briefly describe the plan (sources -> transforms -> destinations) and tell them they can click **Generate** in the prompt card to commit it, or you can do it for them if they confirm. Do NOT invent JSON in the chat reply.
- When the user asks "what does this do" / "explain" / "describe" — walk through the steps using the actual labels and step types from the snapshot.
- When the user asks about issues / errors / validation — read the VALIDATION ISSUES section and report them. If empty, say so plainly.
- When the user asks about last-run status or schedules — say you don't have that in this context unless it's in the snapshot.
- Never claim to have created or modified a pipeline. The user controls the canvas; you only advise.
- If the user asks something unrelated to data pipelines (weather, jokes, world facts), politely redirect them back to the canvas."""


@router.post("/canvas-chat")
async def canvas_chat(
    body: CanvasChatRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Natural-language chat anchored to the editor's canvas state.

    Returns the LLM's plain-text reply. When no AI provider is
    configured, returns `ai_available=False` so the frontend can fall
    back to its client-side smart handlers.
    """
    if not body.messages:
        return {"reply": "What can I help you build or explain?", "ai_powered": False, "ai_available": False}

    if not is_ai_available():
        return {
            "reply": "",
            "ai_powered": False,
            "ai_available": False,
        }

    snapshot = _canvas_snapshot_for_prompt(body.canvas)
    system_prompt = f"{CANVAS_CHAT_SYSTEM_PROMPT}\n\n--- CURRENT CANVAS ---\n{snapshot}\n--- END CANVAS ---"

    chat_msgs = [{"role": m.role, "content": m.content} for m in body.messages]
    text = await ai_generate_text(
        chat_msgs,
        system_prompt=system_prompt,
        source_label="canvas-chat",
        workspace_id=workspace_id,
        max_tokens=1024,
    )
    if not text:
        return {
            "reply": "",
            "ai_powered": False,
            "ai_available": True,
            "error": "LLM call returned no text",
        }
    return {"reply": text.strip(), "ai_powered": True, "ai_available": True}
