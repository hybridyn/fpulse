"""
POST /api/ai/agent — invoke the agent loop with a page context + user intent.

Step 1.5a-api. Synchronous (non-streaming) endpoint. SSE streaming is a
follow-up item.

Wiring:
  - Auth: optional (consistent with other AI endpoints in api/ai.py).
    Anonymous local-dev mode resolves to "default" tenant + user_id="anonymous".
  - Provider: routed via fpulse.planner.ai_client.resolve_provider.
    No provider → graceful 503-style response (HTTP 200 with no_provider
    flag) so the frontend can show a "configure AI provider" CTA without
    treating it as a hard error.
  - Tool tier: defaults to read-only (per Step 1.5a posture). The request
    body MAY widen via `allow_safe_writes=true` once Step 1.5b governance
    enforces RBAC on top.
  - Tools: pulled from the per-process default registry, populated at
    startup via fpulse.ai.tools.register_initial_tools().
  - Cost surfacing: response includes token_count + cost_usd estimate so
    the UI can render the inline "~N tokens · ~$X" indicator (Tier A
    cost visibility, pulled into Tier A per round-4 reviewer).

The endpoint instance can be unit-tested via app_state injection:
  app_state["agent_runner_factory"] — callable returning an AgentRunner.
  Default factory builds AnthropicAgentClient + default registry.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _log_router_decision(
    *,
    prompt: str,
    page: str | None,
    chosen_path: str,
    intent: str | None,
    confidence: float = 0.0,
    latency_ms: int = 0,
    served_from_page: bool = False,
    reason: str | None = None,
    workspace_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Fire-and-forget router-decision log. Never raises — telemetry
    must never break a real request."""
    try:
        from fpulse.api.router_telemetry import log_decision
        log_decision(
            prompt=prompt, page=page, chosen_path=chosen_path,
            intent=intent, confidence=confidence,
            latency_ms=latency_ms, served_from_page=served_from_page,
            reason=reason, workspace_id=workspace_id, user_id=user_id,
        )
    except Exception:  # noqa: BLE001
        pass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from fpulse.ai.agent import AgentLLMClient, AgentRunner
from fpulse.ai.anthropic_client import AnthropicAgentClient
from fpulse.ai.context import PageContext
from fpulse.ai.foundation import get_provider_info
from fpulse.ai.ollama_client import OllamaAgentClient
from fpulse.ai.openai_client import OpenAIAgentClient
from fpulse.ai.openrouter_client import OpenRouterAgentClient
from fpulse.ai.rbac import allowed_tiers_for
from fpulse.ai.tools import (
    ToolTier,
    default_registry,
    register_initial_tools,
)
from fpulse.auth.deps import current_user_optional, current_workspace_id

router = APIRouter(prefix="/api/ai", tags=["ai"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class VisibleItemRequest(BaseModel):
    id: str
    name: str | None = None
    status: str | None = None
    kind: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class PageContextRequest(BaseModel):
    page: str
    visible_ids: list[str] = Field(default_factory=list)
    selected_ids: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    environment: str = "dev"
    # Rich snapshot of on-screen entities. Lets the fast-lane router and
    # single-shot LLM mode answer page questions without a tool call to
    # discover what the user sees. Hard-capped to 50 server-side to bound
    # token cost regardless of frontend behavior.
    visible_items: list[VisibleItemRequest] = Field(default_factory=list)
    # Page-supplied richer context (2026-05-22). Open-shape dict for
    # anything a page wants its assistant to see — the editor can put
    # the current pipeline IR + validation errors here, an executions
    # page can put the last failed-run summary, etc. The block is
    # sanitized (PII / secrets stripped) and budgeted (~2.4 KB cap)
    # before reaching the LLM; large payloads should be summaries.
    # See PageContext.to_extra_context_block in fpulse/ai/context.py.
    extra_context: dict[str, Any] = Field(default_factory=dict)


class ConversationTurn(BaseModel):
    """One entry in the rolling conversation history sent on each request."""
    role: str  # "user" | "assistant"
    content: str


class ConversationContext(BaseModel):
    """Rolling conversation history (2026-05-22).

    The frontend chat store is the source of truth for conversational
    state. On each agent call, it serializes a bounded slice of the
    history into this model:

      - ``recent_turns`` — the last N (default 8-12) raw turns,
        included verbatim so the model sees exact phrasing.
      - ``summary`` — a free-form compressed summary of OLDER turns
        beyond the recent window. The frontend (or a background
        summarizer) is responsible for producing this; the backend
        does NOT re-summarize — it just renders whatever is sent.

    Server-side hard caps (defence-in-depth against runaway frontend):
      - up to 20 recent turns
      - up to 4000 chars per turn (further truncated by sanitize)
      - up to 1200 chars of summary
    """
    recent_turns: list[ConversationTurn] = Field(default_factory=list)
    summary: str = ""


class AgentRequest(BaseModel):
    user_intent: str
    page_context: PageContextRequest
    allow_safe_writes: bool = False
    max_tokens: int = 8000  # Free default per project_fpulse_ai_step0_locks §1
    # Layer 2 — dialogue state. Frontend persists this in sessionStorage
    # and sends it on every turn. Backend reads it before routing,
    # mutates it after, and returns the new value as `case_file` on
    # the response.
    dialogue_state: dict[str, Any] = Field(default_factory=dict)
    # Layer 3 — rolling conversation memory (2026-05-22). See the
    # ConversationContext docstring. Optional: omitting it gives the
    # same prior behaviour (no conversational continuity beyond the
    # single turn). Adding it makes the assistant feel less forgetful
    # without changing the agent loop.
    conversation: ConversationContext = Field(default_factory=ConversationContext)
    # Reasoning mode (2026-05-22). Routing hint, not a hard contract:
    #   - "quick"    — prefer fast-lane shortcuts; tighter timeouts.
    #                  Right for "list X" / "show overview" / similar.
    #   - "standard" — current default. Fast-lane attempted, agent loop
    #                  if fast-lane misses.
    #   - "deep"     — skip fast-lane entirely; widen extra_context
    #                  budget; raise max_iterations and wall-clock.
    #                  Use sparingly: on a CPU 7B, a single deep turn
    #                  can take 2-5 minutes. The frontend should surface
    #                  an explicit latency warning before sending this.
    mode: str = "standard"


class TraceStepResponse(BaseModel):
    step_id: str
    tool_name: str
    tool_tier: str
    input_hash: str
    output_hash: str
    timestamp: str
    latency_ms: int
    tokens_in: int
    tokens_out: int
    decision_reason: str
    redactions_applied: dict[str, int]
    outcome: str
    policy_rules_fired: list[str]


class CostInfo(BaseModel):
    tokens_in: int
    tokens_out: int
    estimated_usd: float
    provider: str
    model: str


class AgentResponse(BaseModel):
    run_id: str
    final_text: str
    outcome: str
    iterations: int
    elapsed_ms: int
    steps: list[TraceStepResponse]
    tool_results: list[dict[str, Any]]
    cost: CostInfo
    no_provider: bool = False
    # Fast-lane responses come from the rule-based pre-flight router rather
    # than the LLM agent loop. Sub-1 s typical, deterministic. Frontend
    # uses this to show an "Instant" badge and skip the "this is slow"
    # reassurance hint.
    instant: bool = False
    instant_intent: str = ""
    # Router confidence in [0, 1] for the matched intent. Only meaningful
    # when ``instant`` is True. The UI uses it to show "high-confidence"
    # vs. "best-guess" affordances.
    instant_confidence: float = 0.0
    # True when the answer was rendered from the page's `visible_items`
    # snapshot — no backend tool call was made. Lets the UI mark these
    # responses as zero-I/O for the trust posture story.
    served_from_page: bool = False
    # Layer 2 — updated dialogue state to persist client-side. Frontend
    # writes this back into sessionStorage and includes it on the next
    # request. Always present (may be the empty struct).
    case_file: dict[str, Any] = Field(default_factory=lambda: {
        "active_entity": None, "active_intent": None,
    })


# ---------------------------------------------------------------------------
# Cost estimate — rough $ per million tokens. Lives here, not in budget.py,
# because budgets are about counts; pricing is provider-specific config.
# ---------------------------------------------------------------------------

_PRICING_PER_MTOKENS: dict[str, tuple[float, float]] = {
    # provider: (input_$/Mtok, output_$/Mtok). Rough; refresh per release.
    "claude": (3.00, 15.00),    # Sonnet baseline
    "openai": (2.50, 10.00),    # GPT-4o baseline
    # OpenRouter cost varies wildly by underlying model — use the default
    # routed model (gpt-4o-mini) baseline + their ~5% markup. Real per-call
    # cost comes from the response's `usage.cost_usd` field when available.
    "openrouter": (0.16, 0.63),
    "ollama": (0.00, 0.00),     # local
    "none": (0.00, 0.00),
}


@router.get("/agent/budget")
def agent_budget(request: Request) -> dict[str, Any]:
    """Today's wallet usage for the caller's user + workspace.

    Returns counts + caps + percent_used so the UI can render an inline
    "12,400 / 100,000 tokens used today" indicator without the chat panel
    having to know the cap math.

    Anonymous (OSS local-dev) sees workspace-only data — there's no
    per-user row when user_id is null.
    """
    from fpulse.ai.wallet import (
        daily_user_cap,
        daily_workspace_cap,
        rate_per_minute,
    )
    user = current_user_optional(request)
    user_id = getattr(user, "id", None) if user else None
    workspace_id = current_workspace_id(request)

    try:
        from fpulse.main import app_state
        wallet = app_state.get("wallet_guard")
    except Exception:
        wallet = None

    user_row: dict[str, Any] = {}
    workspace_row: dict[str, Any] = {}
    if wallet is not None:
        if user_id:
            user_row = wallet.usage_for("user", user_id)
        if workspace_id:
            workspace_row = wallet.usage_for("workspace", workspace_id)

    user_total = int(user_row.get("tokens_in", 0) or 0) + int(user_row.get("tokens_out", 0) or 0)
    ws_total = int(workspace_row.get("tokens_in", 0) or 0) + int(workspace_row.get("tokens_out", 0) or 0)

    user_cap = daily_user_cap()
    ws_cap = daily_workspace_cap()

    return {
        "user": {
            "user_id": user_id,
            "tokens_used_today": user_total,
            "tokens_cap": user_cap,
            "percent_used": round((user_total / user_cap) * 100, 1) if user_cap else 0.0,
            "request_count_today": int(user_row.get("request_count", 0) or 0),
            "cost_usd_today": float(user_row.get("cost_usd", 0.0) or 0.0),
        },
        "workspace": {
            "workspace_id": workspace_id,
            "tokens_used_today": ws_total,
            "tokens_cap": ws_cap,
            "percent_used": round((ws_total / ws_cap) * 100, 1) if ws_cap else 0.0,
            "request_count_today": int(workspace_row.get("request_count", 0) or 0),
            "cost_usd_today": float(workspace_row.get("cost_usd", 0.0) or 0.0),
        },
        "rate_limit_per_minute": rate_per_minute(),
    }


def _estimate_cost_usd(
    provider: str,
    tokens_in: int,
    tokens_out: int,
    model: str | None = None,
) -> float:
    """USD estimate for one agent turn.

    2026-06-18 — now MODEL-aware. The old version keyed pricing on the
    provider alone, so e.g. gpt-4o-mini was billed at the OpenAI
    GPT-4o-tier rate (2.5/10.0 per Mtok) — ~16x its real 0.15/0.60 rate.
    We delegate to fpulse.ai.cost_rates, which carries per-model overrides
    (and tolerant matching for dated / namespaced ids like
    "gpt-4o-mini-2024-07-18" or "openai/gpt-4o-mini"). The legacy
    provider-only table stays as a last-resort fallback.
    """
    try:
        from fpulse.ai import cost_rates
        return round(
            cost_rates.compute_cost_usd(
                cost_rates.DEFAULT_RATES, provider, model, tokens_in, tokens_out
            ),
            6,
        )
    except Exception:  # noqa: BLE001 — pricing must never break a response
        in_per_m, out_per_m = _PRICING_PER_MTOKENS.get(provider, (0.0, 0.0))
        return round((tokens_in / 1_000_000) * in_per_m + (tokens_out / 1_000_000) * out_per_m, 6)


# ---------------------------------------------------------------------------
# AgentRunner construction (factory pattern for testability)
# ---------------------------------------------------------------------------


def _build_default_runner(*, user_id: str | None, workspace_id: str | None) -> AgentRunner:
    """Production AgentRunner: provider-aware client + default registry.

    Dispatches by the resolved provider so an Ollama-configured workspace
    gets `OllamaAgentClient`, an Anthropic-configured one gets
    `AnthropicAgentClient`, etc. OpenAI tool-use client lands in 1.5b-5;
    today its provider falls through to the Anthropic client which will
    raise — endpoint guards via provider check before reaching this.

    Safe to call multiple times — register_initial_tools is idempotent.
    """
    register_initial_tools()  # idempotent
    info = get_provider_info(user_id=user_id, workspace_id=workspace_id)
    client: AgentLLMClient
    if info.provider == "ollama":
        client = OllamaAgentClient(user_id=user_id, workspace_id=workspace_id)
    elif info.provider == "claude":
        client = AnthropicAgentClient(user_id=user_id, workspace_id=workspace_id)
    elif info.provider == "openai":
        client = OpenAIAgentClient(user_id=user_id, workspace_id=workspace_id)
    elif info.provider == "openrouter":
        # OpenRouter — single key, 100+ models. Tool-use depends on the
        # underlying model id (anthropic/claude-* + openai/gpt-* + most
        # llama-3.1+ variants all support it).
        client = OpenRouterAgentClient(user_id=user_id, workspace_id=workspace_id)
    else:
        # Unsupported provider for tool-use today (gemini / azure / custom).
        # Fall back to Anthropic so the resulting RuntimeError surfaces in the
        # trace as `llm_failure` rather than silently doing nothing.
        client = AnthropicAgentClient(user_id=user_id, workspace_id=workspace_id)
    return AgentRunner(
        registry=default_registry(),
        llm_client=client,
    )


def _resolve_runner(request: Request, user_id: str | None, workspace_id: str | None) -> AgentRunner:
    """Look up the runner factory in app_state (for tests) else use default.

    Tests inject:
        from fpulse.main import app_state
        app_state["agent_runner_factory"] = lambda **kw: AgentRunner(
            registry=test_registry,
            llm_client=FakeLLMClient(...),
        )
    """
    try:
        from fpulse.main import app_state
        factory = app_state.get("agent_runner_factory")
        if factory:
            return factory(user_id=user_id, workspace_id=workspace_id)
    except Exception:
        pass
    return _build_default_runner(user_id=user_id, workspace_id=workspace_id)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/agent", response_model=AgentResponse)
async def run_agent(req: AgentRequest, request: Request) -> AgentResponse:
    user = current_user_optional(request)
    user_id = getattr(user, "id", None) if user else None
    user_role = getattr(user, "role", "viewer") if user else "viewer"
    workspace_id = current_workspace_id(request)

    # 2026-05-29: per-user rate limit. LLM calls cost real money — a
    # single user or leaked session token can drain the operator's
    # budget in minutes without this. Default 60/hour per user;
    # configurable via FPULSE_AI_RATE_* env vars. Disable in test
    # environments with FPULSE_AI_RATE_ENABLE=0.
    from fpulse.ai.rate_limit import check_ai_rate_limit
    _client_ip = request.client.host if request.client else None
    check_ai_rate_limit(user_id=user_id, fallback_ip=_client_ip)

    # Provider check — graceful no-op response if no provider configured.
    info = get_provider_info(user_id=user_id, workspace_id=workspace_id)
    if not info.has_provider:
        return AgentResponse(
            run_id="no-provider",
            final_text=(
                "AI is not configured. Set ANTHROPIC_API_KEY / OPENAI_API_KEY / "
                "OLLAMA_URL in your environment, or configure a provider in "
                "Insights → AI Provider."
            ),
            outcome="success",
            iterations=0,
            elapsed_ms=0,
            steps=[],
            tool_results=[],
            cost=CostInfo(
                tokens_in=0,
                tokens_out=0,
                estimated_usd=0.0,
                provider="none",
                model="",
            ),
            no_provider=True,
        )

    # Step 1.5b-2: tiers come from RBAC matrix (role × env), not hard-coded.
    # `allow_safe_writes` is an opt-in cap — even if the role permits writes,
    # the request body must explicitly opt in or the agent gets read-only.
    role_tiers = set(allowed_tiers_for(user_role, req.page_context.environment))
    if req.allow_safe_writes:
        # User opts in to safe-write tools. High-impact still requires
        # additional confirmation (Step 1.5b-3 wires the confirmation gate).
        cap = {ToolTier.READ, ToolTier.SAFE_WRITE}
    else:
        cap = {ToolTier.READ}
    # ── AI safety mode header — Settings → General → AI Assistant.
    # When set, the user has explicitly asked the Copilot to be read-only
    # regardless of role / allow_safe_writes. Wins over the request body
    # (defense-in-depth — the toggle is a user-promised guarantee, not an
    # advisory). Sent as ``X-FPulse-AI-Safety: 1`` by frontend `_headers()`.
    ai_safety_header = request.headers.get("x-fpulse-ai-safety", "").strip()
    if ai_safety_header in ("1", "true", "yes"):
        cap = {ToolTier.READ}
    allowed_tiers: tuple[ToolTier, ...] = tuple(
        sorted(role_tiers & cap, key=lambda t: t.value)
    )
    if not allowed_tiers:
        # Role has no AI permission in this env. Surface a clear policy_block
        # rather than running an empty-toolset agent.
        from fpulse.ai.agent import AgentRunResult, TraceStep
        from datetime import datetime, timezone
        import uuid as _uuid
        denied = TraceStep(
            step_id=str(_uuid.uuid4()),
            tool_name="(no tools)",
            tool_tier="meta",
            input_hash="",
            output_hash="",
            timestamp=datetime.now(timezone.utc).isoformat(),
            latency_ms=0,
            tokens_in=0,
            tokens_out=0,
            decision_reason=f"role={user_role!r} has no allowed tiers in env={req.page_context.environment!r}",
            outcome="policy_block",
            policy_rules_fired=[f"rbac:role_{user_role}_no_access_in_{req.page_context.environment}"],
        )
        return AgentResponse(
            run_id=str(_uuid.uuid4()),
            final_text="Your role has no AI tool access in this environment.",
            outcome="policy_block",
            iterations=0,
            elapsed_ms=0,
            steps=[
                TraceStepResponse(
                    step_id=denied.step_id,
                    tool_name=denied.tool_name,
                    tool_tier=denied.tool_tier,
                    input_hash=denied.input_hash,
                    output_hash=denied.output_hash,
                    timestamp=denied.timestamp,
                    latency_ms=denied.latency_ms,
                    tokens_in=denied.tokens_in,
                    tokens_out=denied.tokens_out,
                    decision_reason=denied.decision_reason,
                    redactions_applied=denied.redactions_applied,
                    outcome=denied.outcome,
                    policy_rules_fired=denied.policy_rules_fired,
                ),
            ],
            tool_results=[],
            cost=CostInfo(tokens_in=0, tokens_out=0, estimated_usd=0.0, provider=info.provider, model=info.model),
        )

    # Sanitize page-supplied extra_context BEFORE it lands in the agent
    # (2026-05-22). The page is free to attach anything — pipeline IR,
    # selected node params, last-run summary — so we must strip secrets /
    # PII at the boundary like every other payload. Hard-cap depth to
    # prevent a runaway frontend from blowing prompt budget.
    _raw_extra = req.page_context.extra_context or {}
    if _raw_extra:
        from fpulse.ai.sanitize import sanitize_for_llm as _sanitize_extra
        _extra_clean = _sanitize_extra(_raw_extra).payload
    else:
        _extra_clean = {}

    # Conversation memory (2026-05-22). Server-side caps:
    #   - up to 20 recent turns (frontend usually sends 8-12)
    #   - sanitize each turn through the same boundary helper used for
    #     visible_items / extra_context, so a copy-pasted secret in a
    #     prior assistant turn doesn't leak back into the next prompt
    #   - reject empty roles silently (treat as malformed entry)
    _convo = req.conversation
    _clean_turns: list[dict[str, str]] = []
    if _convo and _convo.recent_turns:
        from fpulse.ai.sanitize import sanitize_for_llm as _sanitize_turn
        for t in _convo.recent_turns[-20:]:
            role = (t.role or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            content = (t.content or "").strip()
            if not content:
                continue
            # Sanitize content; the helper handles strings as well as dicts
            content_clean = _sanitize_turn({"text": content}).payload.get("text", content)
            _clean_turns.append({"role": role, "content": content_clean[:4000]})
    _convo_summary = (_convo.summary or "").strip() if _convo else ""

    page_ctx = PageContext(
        page=req.page_context.page,
        user_id=user_id or "anonymous",
        tenant_id=workspace_id,
        workspace_id=workspace_id,
        environment=req.page_context.environment,
        visible_ids=tuple(req.page_context.visible_ids),
        selected_ids=tuple(req.page_context.selected_ids),
        filters=req.page_context.filters,
        role=user_role,
        visible_items=tuple(
            it.model_dump(exclude_none=True)
            for it in req.page_context.visible_items[:50]
        ),
        extra_context=_extra_clean,
        recent_turns=tuple(_clean_turns),
        conversation_summary=_convo_summary,
    )

    # ── Layer 2: dialogue state ───────────────────────────────────────
    # Parse the incoming case_file. Slot-fill (deterministic short-reply
    # → pending intent) runs BEFORE fast-lane so "yes" / "first" /
    # "Daily ingest" don't get mistaken for novel queries. Reference
    # substitution rewrites "it" / "this" using the pinned active_entity.
    from fpulse.ai.dialogue_state import (
        infer_intent_from_prompt,
        parse_state,
        serialize_state,
        substitute_reference,
        try_slot_fill,
        with_pending_clarification,
        with_resolved_entity,
    )
    incoming_state = parse_state(req.dialogue_state)
    case_file_out = serialize_state(incoming_state)

    # Build fast_ctx once — slot-fill + clarify + fast-lane all use it.
    from fpulse.ai.fast_router import run_fast_lane
    from fpulse.ai.tools.base import ToolContext as _FastCtx
    fast_ctx = _FastCtx(
        tenant_id=workspace_id or "default",
        user_id=user_id,
        workspace_id=workspace_id,
        environment=req.page_context.environment,
        dry_run=False,
        selected_ids=tuple(req.page_context.selected_ids),
        visible_ids=tuple(req.page_context.visible_ids),
        page=req.page_context.page or "",
        visible_items=tuple(
            it.model_dump(exclude_none=True)
            for it in req.page_context.visible_items[:50]
        ),
    )

    effective_prompt = req.user_intent

    # ── Multi-step gate (Phase 1 final May 5 2026) ────────────────────
    # Chained imperatives ("first X then Y") MUST go to the agent loop —
    # fast-lane keyword match would otherwise pick a single intent and
    # silently drop the rest of the request.
    try:
        from fpulse.ai.multi_step import is_multi_step
        _multi_step = is_multi_step(req.user_intent)
    except Exception:  # noqa: BLE001
        _multi_step = False

    sf = try_slot_fill(req.user_intent, incoming_state, fast_ctx)
    if sf is not None:
        # Pin the resolved entity. Then dispatch directly to the right
        # handler — re-routing through fast-lane fails because the
        # synthesized prompts don't match its trigger list.
        case_file_out = serialize_state(with_resolved_entity(incoming_state, sf.entity))
        from fpulse.ai.direct_actions import (
            direct_cancel_execution,
            direct_run_pipeline,
            direct_test_connection,
        )
        from fpulse.ai.dialogue_state import (
            describe_entity as _describe_entity,
            diagnose_failure as _diagnose_failure_handler,
        )
        SLOT_DISPATCH = {
            "test_connection": direct_test_connection,
            "run_pipeline": direct_run_pipeline,
            "cancel_run": direct_cancel_execution,
            "view_pipeline": _describe_entity,
            "diagnose_run": _describe_entity,
            "diagnose_failure": _diagnose_failure_handler,
        }
        handler = SLOT_DISPATCH.get(sf.intent_name)
        if handler is not None:
            try:
                text = await handler(sf.fired_intent_text, fast_ctx)
            except Exception as exc:  # noqa: BLE001
                logger.warning("slot-fill dispatch %s failed: %s", sf.intent_name, exc)
                text = None
            if text:
                import uuid as _uuid
                return AgentResponse(
                    run_id=str(_uuid.uuid4()),
                    final_text=text,
                    outcome="success",
                    iterations=0,
                    elapsed_ms=0,
                    steps=[],
                    tool_results=[],
                    cost=CostInfo(
                        tokens_in=0, tokens_out=0, estimated_usd=0.0,
                        provider=info.provider, model=info.model,
                    ),
                    instant=True,
                    instant_intent=f"slotfill.{sf.intent_name}",
                    instant_confidence=0.95,
                    served_from_page=True,
                    case_file=case_file_out,
                )
        # No fast handler (e.g. diagnose_failure) — fall through with the
        # synthesized prompt + pinned entity so single-shot answers cleanly.
        effective_prompt = sf.fired_intent_text
    elif incoming_state.active_entity is not None:
        effective_prompt = substitute_reference(req.user_intent, incoming_state)
        # Post-substitution direct dispatch: when the user said "run it" /
        # "test it" / "cancel it" + we just rewrote it to a quoted name,
        # send straight to the direct handler — same as slot-fill above.
        if effective_prompt != req.user_intent:
            from fpulse.ai.direct_actions import (
                direct_cancel_execution,
                direct_run_pipeline,
                direct_test_connection,
            )
            head = effective_prompt.lower().strip().split()
            verb = head[0] if head else ""
            VERB_DISPATCH = {
                "run": ("run_pipeline", direct_run_pipeline),
                "execute": ("run_pipeline", direct_run_pipeline),
                "test": ("test_connection", direct_test_connection),
                "verify": ("test_connection", direct_test_connection),
                "cancel": ("cancel_run", direct_cancel_execution),
                "stop": ("cancel_run", direct_cancel_execution),
                "kill": ("cancel_run", direct_cancel_execution),
            }
            entry = VERB_DISPATCH.get(verb)
            if entry is not None:
                intent_label, handler = entry
                try:
                    text = await handler(effective_prompt, fast_ctx)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ref-sub dispatch %s failed: %s", verb, exc)
                    text = None
                if text:
                    import uuid as _uuid
                    return AgentResponse(
                        run_id=str(_uuid.uuid4()),
                        final_text=text,
                        outcome="success",
                        iterations=0,
                        elapsed_ms=0,
                        steps=[],
                        tool_results=[],
                        cost=CostInfo(
                            tokens_in=0, tokens_out=0, estimated_usd=0.0,
                            provider=info.provider, model=info.model,
                        ),
                        instant=True,
                        instant_intent=f"refsub.{intent_label}",
                        instant_confidence=0.9,
                        served_from_page=True,
                        case_file=case_file_out,
                    )

    # ── Fast-lane pre-flight (May 4 2026; deep-mode skip 2026-05-22) ──
    # Multi-step prompts skip fast-lane + clarify entirely; they need
    # the agent loop to plan + chain steps. ``mode='deep'`` also skips
    # fast-lane — the user has explicitly asked for full reasoning, so
    # we should not short-circuit even if a fast-lane intent matches.
    _is_deep_mode = (req.mode or "standard").strip().lower() == "deep"
    if ToolTier.READ in cap and not _multi_step and not _is_deep_mode:
        try:
            fast_result = await run_fast_lane(effective_prompt, fast_ctx)
        except Exception:  # noqa: BLE001
            fast_result = None
        if fast_result is not None:
            import uuid as _uuid
            _log_router_decision(
                prompt=req.user_intent, page=req.page_context.page,
                chosen_path="fast-lane", intent=fast_result.intent_name,
                confidence=fast_result.confidence, latency_ms=fast_result.elapsed_ms,
                served_from_page=fast_result.served_from_page,
                reason=fast_result.match_reason,
                workspace_id=workspace_id, user_id=user_id,
            )
            try:
                from fpulse.ai.ai_metrics import get_store
                get_store().record_request(
                    lane="fast_lane", latency_ms=fast_result.elapsed_ms,
                )
            except Exception:  # noqa: BLE001
                pass
            return AgentResponse(
                run_id=str(_uuid.uuid4()),
                final_text=fast_result.text,
                outcome="success",
                iterations=0,
                elapsed_ms=fast_result.elapsed_ms,
                steps=[],
                tool_results=[],
                cost=CostInfo(
                    tokens_in=0,
                    tokens_out=0,
                    estimated_usd=0.0,
                    provider=info.provider,
                    model=info.model,
                ),
                instant=True,
                instant_intent=fast_result.intent_name,
                instant_confidence=fast_result.confidence,
                served_from_page=fast_result.served_from_page,
                case_file=case_file_out,
            )

    # ── Clarification / auto-pin (May 5 2026) ─────────────────────────
    # Multi-step prompts go straight to the agent — no clarify, no auto-pin.
    try:
        from fpulse.ai.clarify import build_clarification, needs_clarification
        clar_kind = None if _multi_step else needs_clarification(effective_prompt, fast_ctx)
    except Exception:  # noqa: BLE001
        clar_kind = None
    if clar_kind is not None:
        import uuid as _uuid
        same_kind = [it for it in fast_ctx.visible_items if (it.get("kind") or "") == clar_kind]
        # AUTO-PIN: exactly 1 candidate of this kind on screen → resolve
        # silently and dispatch via SLOT_DISPATCH (same as a slot-fill).
        # Was the missing fix on the 1-pipeline workspace.
        if len(same_kind) == 1:
            from fpulse.ai.dialogue_state import (
                ActiveEntity,
                describe_entity as _describe_entity,
                diagnose_failure as _diagnose_failure_handler,
            )
            from fpulse.ai.direct_actions import (
                direct_cancel_execution,
                direct_run_pipeline,
                direct_test_connection,
            )
            target = same_kind[0]
            inferred = infer_intent_from_prompt(effective_prompt)
            entity = ActiveEntity(
                kind=str(target.get("kind") or clar_kind),
                id=str(target.get("id") or ""),
                name=str(target.get("name") or target.get("id") or "?"),
            )
            case_file_out = serialize_state(with_resolved_entity(incoming_state, entity))
            AUTO_PIN_DISPATCH = {
                "diagnose_failure": _diagnose_failure_handler,
                "view_pipeline": _describe_entity,
                "diagnose_run": _describe_entity,
                "test_connection": direct_test_connection,
                "run_pipeline": direct_run_pipeline,
                "cancel_run": direct_cancel_execution,
            }
            handler = AUTO_PIN_DISPATCH.get(inferred, _describe_entity)
            synth = f'about "{entity.name}"' if inferred == "view_pipeline" else \
                    f'why did "{entity.name}" fail' if inferred == "diagnose_failure" else \
                    f'about "{entity.name}"'
            try:
                text = await handler(synth, fast_ctx)
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto-pin dispatch %s failed: %s", inferred, exc)
                text = None
            if text:
                return AgentResponse(
                    run_id=str(_uuid.uuid4()),
                    final_text=text,
                    outcome="success",
                    iterations=0,
                    elapsed_ms=0,
                    steps=[],
                    tool_results=[],
                    cost=CostInfo(
                        tokens_in=0, tokens_out=0, estimated_usd=0.0,
                        provider=info.provider, model=info.model,
                    ),
                    instant=True,
                    instant_intent=f"autopin.{inferred}",
                    instant_confidence=0.92,
                    served_from_page=True,
                    case_file=case_file_out,
                )
        # Otherwise (≥2 candidates): build the clarify card.
        text = build_clarification(effective_prompt, fast_ctx, clar_kind)
        pending = with_pending_clarification(
            incoming_state, infer_intent_from_prompt(effective_prompt),
        )
        return AgentResponse(
            run_id=str(_uuid.uuid4()),
            final_text=text,
            outcome="success",
            iterations=0,
            elapsed_ms=0,
            steps=[],
            tool_results=[],
            cost=CostInfo(
                tokens_in=0,
                tokens_out=0,
                estimated_usd=0.0,
                provider=info.provider,
                model=info.model,
            ),
            instant=True,
            instant_intent="clarify",
            instant_confidence=0.95,
            served_from_page=True,
            case_file=serialize_state(pending),
        )

    runner = _resolve_runner(request, user_id=user_id, workspace_id=workspace_id)

    # ── TOOL_ONLY_MODE short-circuit ─────────────────────────────────
    # When the operator has set FPULSE_TOOL_ONLY_MODE=1, we never call
    # an LLM. Fast-lane already ran above and didn't match — return a
    # friendly explanation instead of falling through to the hybrid /
    # single-shot / full-agent paths.
    from fpulse.ai.tool_only_mode import is_enabled as _tool_only_enabled
    from fpulse.ai.tool_only_mode import unavailable_response_text as _tool_only_text
    if _tool_only_enabled():
        import uuid as _uuid
        _log_router_decision(
            prompt=req.user_intent, page=req.page_context.page,
            chosen_path="tool_only_mode_block", intent=None,
            confidence=0.0, latency_ms=0,
            reason="FPULSE_TOOL_ONLY_MODE=1; no fast-lane match",
            workspace_id=workspace_id, user_id=user_id,
        )
        return AgentResponse(
            run_id=str(_uuid.uuid4()),
            final_text=_tool_only_text(),
            outcome="success",
            iterations=0,
            elapsed_ms=0,
            steps=[],
            tool_results=[],
            cost=CostInfo(
                tokens_in=0, tokens_out=0, estimated_usd=0.0,
                provider="tool_only", model="",
            ),
            instant=True,
            instant_intent="tool_only_mode",
            instant_confidence=1.0,
            served_from_page=False,
            case_file=case_file_out,
        )

    # ── Hybrid lane (May 17 2026) ────────────────────────────────────
    # Same as the stream endpoint's hybrid block — reasoning prompt
    # PLUS a viable fast-lane data intent → one tool fetch + one LLM
    # format pass instead of the 6-step agent loop. See backend
    # fpulse/ai/hybrid.py for the trust contract.
    if info.provider != "none" and ToolTier.READ in cap and not _multi_step:
        from fpulse.ai.hybrid import run_hybrid, should_use_hybrid
        hybrid_match = should_use_hybrid(req.user_intent)
        if hybrid_match is not None:
            hybrid_intent, hybrid_conf, _ = hybrid_match
            try:
                from fpulse.main import app_state  # type: ignore
                _app_state = app_state
            except Exception:
                _app_state = None
            try:
                hyb = await run_hybrid(
                    prompt=req.user_intent,
                    intent=hybrid_intent,
                    fast_ctx=fast_ctx,
                    llm_client=runner.llm_client,
                    page_context=page_ctx,
                    app_state=_app_state,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("hybrid lane failed, falling through: %s", exc)
                hyb = None
            # Skip the early return when the hybrid lane explicitly asked
            # to escalate — fast-lane tool returned empty/generic, full
            # agent loop may have a different tool that can answer.
            if hyb is not None and hyb.text and not hyb.escalate:
                try:
                    from fpulse.ai.ai_metrics import get_store
                    get_store().record_request(
                        lane="hybrid", latency_ms=hyb.elapsed_ms,
                        tool_hops=1, tokens_in=hyb.tokens_in,
                        tokens_out=hyb.tokens_out,
                    )
                except Exception:  # noqa: BLE001
                    pass
                import uuid as _uuid
                return AgentResponse(
                    run_id=str(_uuid.uuid4()),
                    final_text=hyb.text,
                    outcome="success",
                    iterations=2,
                    elapsed_ms=hyb.elapsed_ms,
                    steps=[],
                    tool_results=[],
                    cost=CostInfo(
                        tokens_in=hyb.tokens_in,
                        tokens_out=hyb.tokens_out,
                        estimated_usd=_estimate_cost_usd(
                            info.provider, hyb.tokens_in, hyb.tokens_out,
                            model=info.model,
                        ),
                        provider=info.provider,
                        model=info.model,
                    ),
                    instant=False,
                    instant_intent=f"hybrid.{hybrid_intent.name}",
                    instant_confidence=hybrid_conf,
                    served_from_page=False,
                    case_file=case_file_out,
                )
            if hyb is not None and hyb.escalate:
                _log_router_decision(
                    prompt=req.user_intent, page=req.page_context.page,
                    chosen_path="hybrid_escalate", intent=f"hybrid.{hybrid_intent.name}",
                    confidence=hybrid_conf, latency_ms=hyb.elapsed_ms,
                    reason=hyb.escalate_reason,
                    workspace_id=workspace_id, user_id=user_id,
                )
                try:
                    from fpulse.ai.ai_metrics import get_store
                    get_store().record_escalation()
                except Exception:  # noqa: BLE001
                    pass

    # ── Single-shot path (May 5 2026) ─────────────────────────────────
    # For reasoning-only prompts ("why did X fail?", "explain this") that
    # the fast-lane can't answer, run ONE LLM call with no tools / RAG
    # instead of the 6-iteration agent loop. ~3-8 s on local Ollama vs.
    # 30-90 s for the full loop. Conservative classifier — falls through
    # to the agent loop on any ambiguity.
    if info.provider != "none":
        from fpulse.ai.single_shot import run_single_shot, should_use_single_shot
        if should_use_single_shot(req.user_intent):
            try:
                from fpulse.main import app_state  # type: ignore
                _app_state = app_state
            except Exception:
                _app_state = None
            try:
                shot = await run_single_shot(
                    prompt=req.user_intent,
                    page_context=page_ctx,
                    llm_client=runner.llm_client,
                    app_state=_app_state,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("single_shot failed, falling through: %s", exc)
                shot = None
            if shot is not None and shot.text:
                import uuid as _uuid
                return AgentResponse(
                    run_id=str(_uuid.uuid4()),
                    final_text=shot.text,
                    outcome="success",
                    iterations=1,
                    elapsed_ms=shot.elapsed_ms,
                    steps=[],
                    tool_results=[],
                    cost=CostInfo(
                        tokens_in=shot.tokens_in,
                        tokens_out=shot.tokens_out,
                        estimated_usd=_estimate_cost_usd(
                            info.provider, shot.tokens_in, shot.tokens_out,
                            model=info.model,
                        ),
                        provider=info.provider,
                        model=info.model,
                    ),
                    instant=False,
                    instant_intent="single_shot",
                    instant_confidence=0.0,
                    served_from_page=False,
                    case_file=case_file_out,
                )

    try:
        result = await runner.run(
            page_context=page_ctx,
            user_intent=effective_prompt,
            allowed_tiers=allowed_tiers,
            max_tokens=req.max_tokens,
            mode=req.mode,
        )
    except Exception as e:
        # Hard runner failure — surface as 500 so the frontend can retry
        # with backoff. Should be rare; AgentRunner itself catches most things.
        raise HTTPException(status_code=500, detail=f"Agent runner failed: {type(e).__name__}")

    # Record into the AI metrics store. Best-effort — never breaks
    # the request. Counts the actual tool hops (iterations minus the
    # final answer-only iteration) so operators can decide when to
    # bump MAX_ITERATIONS.
    try:
        from fpulse.ai.ai_metrics import get_store
        get_store().record_request(
            lane="agent_loop",
            latency_ms=result.elapsed_ms,
            tool_hops=max(0, result.iterations - 1),
            tokens_in=result.total_tokens_in,
            tokens_out=result.total_tokens_out,
        )
    except Exception:  # noqa: BLE001
        pass

    steps_response = [
        TraceStepResponse(
            step_id=s.step_id,
            tool_name=s.tool_name,
            tool_tier=s.tool_tier,
            input_hash=s.input_hash,
            output_hash=s.output_hash,
            timestamp=s.timestamp,
            latency_ms=s.latency_ms,
            tokens_in=s.tokens_in,
            tokens_out=s.tokens_out,
            decision_reason=s.decision_reason,
            redactions_applied=s.redactions_applied,
            outcome=s.outcome,
            policy_rules_fired=s.policy_rules_fired,
        )
        for s in result.steps
    ]

    cost = CostInfo(
        tokens_in=result.total_tokens_in,
        tokens_out=result.total_tokens_out,
        estimated_usd=_estimate_cost_usd(
            info.provider, result.total_tokens_in, result.total_tokens_out,
            model=info.model,
        ),
        provider=info.provider,
        model=info.model,
    )

    # Telemetry — agent-loop fallthrough is the slow path; logging
    # these is what surfaces "should have been a fast-lane" candidates.
    _log_router_decision(
        prompt=req.user_intent, page=req.page_context.page,
        chosen_path="agent", intent=None,
        confidence=0.0, latency_ms=result.elapsed_ms,
        served_from_page=False, reason="fallthrough",
        workspace_id=workspace_id, user_id=user_id,
    )

    return AgentResponse(
        run_id=result.run_id,
        final_text=result.final_text,
        outcome=result.outcome,
        iterations=result.iterations,
        elapsed_ms=result.elapsed_ms,
        steps=steps_response,
        tool_results=result.tool_results,
        cost=cost,
        case_file=case_file_out,
    )


# ---------------------------------------------------------------------------
# Streaming endpoint — SSE for incremental tool-step rendering
# ---------------------------------------------------------------------------


def _trace_step_to_dict(s: Any) -> dict[str, Any]:
    return {
        "step_id": s.step_id,
        "tool_name": s.tool_name,
        "tool_tier": s.tool_tier,
        "input_hash": s.input_hash,
        "output_hash": s.output_hash,
        "timestamp": s.timestamp,
        "latency_ms": s.latency_ms,
        "tokens_in": s.tokens_in,
        "tokens_out": s.tokens_out,
        "decision_reason": s.decision_reason,
        "redactions_applied": s.redactions_applied,
        "outcome": s.outcome,
        "policy_rules_fired": s.policy_rules_fired,
    }


@router.post("/agent/stream")
async def run_agent_stream(req: AgentRequest, request: Request) -> StreamingResponse:
    """Streaming variant of /agent — emits SSE events as the loop progresses.

    Event types emitted:
      run_start    — { run_id, page }
      iteration    — { iteration }
      tool_start   — { tool_name }
      step         — { step: TraceStep }     (one per tool execution)
      final        — { run_id, final_text, outcome, iterations, elapsed_ms,
                       steps, tool_results, cost, no_provider }
      error        — { error }                (terminal; runner crashed)

    Format: each event is one `data: <json>\\n\\n` SSE frame.

    The synchronous /agent endpoint stays available unchanged. Frontend
    picks the streaming path for the chat UX where incremental rendering
    is helpful.
    """
    user = current_user_optional(request)
    user_id = getattr(user, "id", None) if user else None
    user_role = getattr(user, "role", "viewer") if user else "viewer"
    workspace_id = current_workspace_id(request)

    # 2026-05-29: rate limit — same key as /agent. cost=2 because a
    # streaming session typically runs more iterations than a single
    # synchronous call, so it represents more LLM-token spend.
    from fpulse.ai.rate_limit import check_ai_rate_limit
    _client_ip = request.client.host if request.client else None
    check_ai_rate_limit(user_id=user_id, fallback_ip=_client_ip, cost=2)

    info = get_provider_info(user_id=user_id, workspace_id=workspace_id)

    async def _no_provider_stream():
        # Single-frame stream that mirrors the synchronous no_provider response.
        payload = {
            "event": "final",
            "run_id": "no-provider",
            "final_text": (
                "AI is not configured. Set ANTHROPIC_API_KEY / OPENAI_API_KEY / "
                "OLLAMA_URL in your environment, or configure a provider in "
                "Insights → AI Provider."
            ),
            "outcome": "success",
            "iterations": 0,
            "elapsed_ms": 0,
            "steps": [],
            "tool_results": [],
            "cost": {
                "tokens_in": 0,
                "tokens_out": 0,
                "estimated_usd": 0.0,
                "provider": "none",
                "model": "",
            },
            "no_provider": True,
        }
        yield f"data: {json.dumps(payload)}\n\n"

    if not info.has_provider:
        return StreamingResponse(_no_provider_stream(), media_type="text/event-stream")

    role_tiers = set(allowed_tiers_for(user_role, req.page_context.environment))
    cap = {ToolTier.READ, ToolTier.SAFE_WRITE} if req.allow_safe_writes else {ToolTier.READ}
    # AI safety mode header — see sync endpoint for full rationale.
    # Wins over allow_safe_writes / role tiers; forces read-only.
    ai_safety_header = request.headers.get("x-fpulse-ai-safety", "").strip()
    if ai_safety_header in ("1", "true", "yes"):
        cap = {ToolTier.READ}
    allowed_tiers: tuple[ToolTier, ...] = tuple(
        sorted(role_tiers & cap, key=lambda t: t.value)
    )

    if not allowed_tiers:
        from datetime import datetime, timezone
        import uuid as _uuid

        denied_step = {
            "step_id": str(_uuid.uuid4()),
            "tool_name": "(no tools)",
            "tool_tier": "meta",
            "input_hash": "",
            "output_hash": "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "latency_ms": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "decision_reason": (
                f"role={user_role!r} has no allowed tiers in env="
                f"{req.page_context.environment!r}"
            ),
            "redactions_applied": {},
            "outcome": "policy_block",
            "policy_rules_fired": [
                f"rbac:role_{user_role}_no_access_in_{req.page_context.environment}",
            ],
        }
        run_id = str(_uuid.uuid4())

        async def _denied_stream():
            payload = {
                "event": "final",
                "run_id": run_id,
                "final_text": "Your role has no AI tool access in this environment.",
                "outcome": "policy_block",
                "iterations": 0,
                "elapsed_ms": 0,
                "steps": [denied_step],
                "tool_results": [],
                "cost": {
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "estimated_usd": 0.0,
                    "provider": info.provider,
                    "model": info.model,
                },
                "no_provider": False,
            }
            yield f"data: {json.dumps(payload)}\n\n"

        return StreamingResponse(_denied_stream(), media_type="text/event-stream")

    # Sanitize page-supplied extra_context BEFORE it lands in the agent
    # (2026-05-22). The page is free to attach anything — pipeline IR,
    # selected node params, last-run summary — so we must strip secrets /
    # PII at the boundary like every other payload. Hard-cap depth to
    # prevent a runaway frontend from blowing prompt budget.
    _raw_extra = req.page_context.extra_context or {}
    if _raw_extra:
        from fpulse.ai.sanitize import sanitize_for_llm as _sanitize_extra
        _extra_clean = _sanitize_extra(_raw_extra).payload
    else:
        _extra_clean = {}

    # Conversation memory (2026-05-22). Server-side caps:
    #   - up to 20 recent turns (frontend usually sends 8-12)
    #   - sanitize each turn through the same boundary helper used for
    #     visible_items / extra_context, so a copy-pasted secret in a
    #     prior assistant turn doesn't leak back into the next prompt
    #   - reject empty roles silently (treat as malformed entry)
    _convo = req.conversation
    _clean_turns: list[dict[str, str]] = []
    if _convo and _convo.recent_turns:
        from fpulse.ai.sanitize import sanitize_for_llm as _sanitize_turn
        for t in _convo.recent_turns[-20:]:
            role = (t.role or "").strip().lower()
            if role not in ("user", "assistant"):
                continue
            content = (t.content or "").strip()
            if not content:
                continue
            # Sanitize content; the helper handles strings as well as dicts
            content_clean = _sanitize_turn({"text": content}).payload.get("text", content)
            _clean_turns.append({"role": role, "content": content_clean[:4000]})
    _convo_summary = (_convo.summary or "").strip() if _convo else ""

    page_ctx = PageContext(
        page=req.page_context.page,
        user_id=user_id or "anonymous",
        tenant_id=workspace_id,
        workspace_id=workspace_id,
        environment=req.page_context.environment,
        visible_ids=tuple(req.page_context.visible_ids),
        selected_ids=tuple(req.page_context.selected_ids),
        filters=req.page_context.filters,
        role=user_role,
        visible_items=tuple(
            it.model_dump(exclude_none=True)
            for it in req.page_context.visible_items[:50]
        ),
        extra_context=_extra_clean,
        recent_turns=tuple(_clean_turns),
        conversation_summary=_convo_summary,
    )

    # ── Layer 2 (stream) ──────────────────────────────────────────────
    # Same dialogue-state wedge as the sync endpoint. Slot-fill +
    # reference-substitution dispatch directly to handlers via SSE.
    from fpulse.ai.dialogue_state import (
        describe_entity as _describe_entity,
        diagnose_failure as _diagnose_failure_handler,
        infer_intent_from_prompt,
        parse_state,
        serialize_state,
        substitute_reference,
        try_slot_fill,
        with_pending_clarification,
        with_resolved_entity,
    )
    from fpulse.ai.fast_router import run_fast_lane
    from fpulse.ai.tools.base import ToolContext as _FastCtx
    fast_ctx = _FastCtx(
        tenant_id=workspace_id or "default",
        user_id=user_id,
        workspace_id=workspace_id,
        environment=req.page_context.environment,
        dry_run=False,
        selected_ids=tuple(req.page_context.selected_ids),
        visible_ids=tuple(req.page_context.visible_ids),
        page=req.page_context.page or "",
        visible_items=tuple(
            it.model_dump(exclude_none=True)
            for it in req.page_context.visible_items[:50]
        ),
    )
    incoming_state = parse_state(req.dialogue_state)
    case_file_out = serialize_state(incoming_state)
    effective_prompt = req.user_intent

    # Multi-step gate (mirror of sync path).
    try:
        from fpulse.ai.multi_step import is_multi_step
        _multi_step = is_multi_step(req.user_intent)
    except Exception:  # noqa: BLE001
        _multi_step = False

    def _instant_sse(text: str, intent_name: str, *, conf: float, case_file: dict[str, Any]):
        import uuid as _uuid
        run_id = str(_uuid.uuid4())

        async def _gen():
            start = {"event": "run_start", "run_id": run_id, "page": req.page_context.page}
            yield f"data: {json.dumps(start)}\n\n"
            final = {
                "event": "final",
                "run_id": run_id,
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
                "no_provider": False,
                "instant": True,
                "instant_intent": intent_name,
                "instant_confidence": conf,
                "served_from_page": True,
                "case_file": case_file,
            }
            yield f"data: {json.dumps(final)}\n\n"
        return StreamingResponse(_gen(), media_type="text/event-stream")

    # Slot-fill dispatch.
    sf = try_slot_fill(req.user_intent, incoming_state, fast_ctx)
    if sf is not None:
        case_file_out = serialize_state(with_resolved_entity(incoming_state, sf.entity))
        from fpulse.ai.direct_actions import (
            direct_cancel_execution,
            direct_run_pipeline,
            direct_test_connection,
        )
        SLOT_DISPATCH = {
            "test_connection": direct_test_connection,
            "run_pipeline": direct_run_pipeline,
            "cancel_run": direct_cancel_execution,
            "view_pipeline": _describe_entity,
            "diagnose_run": _describe_entity,
            "diagnose_failure": _diagnose_failure_handler,
        }
        h = SLOT_DISPATCH.get(sf.intent_name)
        if h is not None:
            try:
                text = await h(sf.fired_intent_text, fast_ctx)
            except Exception as exc:  # noqa: BLE001
                logger.warning("slot-fill stream dispatch %s failed: %s", sf.intent_name, exc)
                text = None
            if text:
                return _instant_sse(text, f"slotfill.{sf.intent_name}", conf=0.95, case_file=case_file_out)
        effective_prompt = sf.fired_intent_text
    elif incoming_state.active_entity is not None:
        effective_prompt = substitute_reference(req.user_intent, incoming_state)
        if effective_prompt != req.user_intent:
            from fpulse.ai.direct_actions import (
                direct_cancel_execution,
                direct_run_pipeline,
                direct_test_connection,
            )
            head = effective_prompt.lower().strip().split()
            verb = head[0] if head else ""
            VERB_DISPATCH = {
                "run": ("run_pipeline", direct_run_pipeline),
                "execute": ("run_pipeline", direct_run_pipeline),
                "test": ("test_connection", direct_test_connection),
                "verify": ("test_connection", direct_test_connection),
                "cancel": ("cancel_run", direct_cancel_execution),
                "stop": ("cancel_run", direct_cancel_execution),
                "kill": ("cancel_run", direct_cancel_execution),
            }
            entry = VERB_DISPATCH.get(verb)
            if entry is not None:
                intent_label, handler = entry
                try:
                    text = await handler(effective_prompt, fast_ctx)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ref-sub stream dispatch %s failed: %s", verb, exc)
                    text = None
                if text:
                    return _instant_sse(text, f"refsub.{intent_label}", conf=0.9, case_file=case_file_out)

    # ── Fast-lane pre-flight (May 4 2026; deep-mode skip 2026-05-22) ──
    # See sync endpoint comment above. Streams a single 'final' event so
    # the frontend renders the answer immediately without waiting for
    # iteration / tool_start / token events to arrive. Multi-step
    # prompts skip fast-lane + clarify and go straight to the agent.
    # ``mode='deep'`` also skips fast-lane (user opted into full reasoning).
    _is_deep_mode_stream = (req.mode or "standard").strip().lower() == "deep"
    if ToolTier.READ in cap and not _multi_step and not _is_deep_mode_stream:
        try:
            fast_result = await run_fast_lane(effective_prompt, fast_ctx)
        except Exception:  # noqa: BLE001
            fast_result = None
        if fast_result is not None:
            import uuid as _uuid
            run_id = str(_uuid.uuid4())
            text = fast_result.text
            elapsed_ms = fast_result.elapsed_ms
            intent_name = fast_result.intent_name
            confidence = fast_result.confidence
            served_from_page = fast_result.served_from_page

            async def _fast_stream():
                # Single-shot final event — fast lane runs synchronously
                # so by the time we yield this, the answer is fully built.
                start = {"event": "run_start", "run_id": run_id, "page": req.page_context.page}
                yield f"data: {json.dumps(start)}\n\n"
                final = {
                    "event": "final",
                    "run_id": run_id,
                    "final_text": text,
                    "outcome": "success",
                    "iterations": 0,
                    "elapsed_ms": elapsed_ms,
                    "steps": [],
                    "tool_results": [],
                    "cost": {
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "estimated_usd": 0.0,
                        "provider": info.provider,
                        "model": info.model,
                    },
                    "no_provider": False,
                    "instant": True,
                    "instant_intent": intent_name,
                    "instant_confidence": confidence,
                    "served_from_page": served_from_page,
                    "case_file": case_file_out,
                }
                yield f"data: {json.dumps(final)}\n\n"

            return StreamingResponse(_fast_stream(), media_type="text/event-stream")

    # ── Clarification / auto-pin layer (stream) ──────────────────────
    try:
        from fpulse.ai.clarify import build_clarification, needs_clarification
        _clar_kind = None if _multi_step else needs_clarification(effective_prompt, fast_ctx)
    except Exception:  # noqa: BLE001
        _clar_kind = None
    if _clar_kind is not None:
        same_kind = [it for it in fast_ctx.visible_items if (it.get("kind") or "") == _clar_kind]
        # AUTO-PIN: 1 candidate → dispatch silently like a slot-fill.
        if len(same_kind) == 1:
            from fpulse.ai.dialogue_state import ActiveEntity
            from fpulse.ai.direct_actions import (
                direct_cancel_execution,
                direct_run_pipeline,
                direct_test_connection,
            )
            target = same_kind[0]
            inferred = infer_intent_from_prompt(effective_prompt)
            entity = ActiveEntity(
                kind=str(target.get("kind") or _clar_kind),
                id=str(target.get("id") or ""),
                name=str(target.get("name") or target.get("id") or "?"),
            )
            case_file_out = serialize_state(with_resolved_entity(incoming_state, entity))
            AUTO_PIN_DISPATCH = {
                "diagnose_failure": _diagnose_failure_handler,
                "view_pipeline": _describe_entity,
                "diagnose_run": _describe_entity,
                "test_connection": direct_test_connection,
                "run_pipeline": direct_run_pipeline,
                "cancel_run": direct_cancel_execution,
            }
            handler = AUTO_PIN_DISPATCH.get(inferred, _describe_entity)
            synth = (f'why did "{entity.name}" fail' if inferred == "diagnose_failure"
                     else f'about "{entity.name}"')
            try:
                text = await handler(synth, fast_ctx)
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto-pin stream dispatch %s failed: %s", inferred, exc)
                text = None
            if text:
                return _instant_sse(text, f"autopin.{inferred}", conf=0.92, case_file=case_file_out)
        # Otherwise (≥2 candidates): clarify card.
        text = build_clarification(effective_prompt, fast_ctx, _clar_kind)
        pending = with_pending_clarification(
            incoming_state, infer_intent_from_prompt(effective_prompt),
        )
        return _instant_sse(text, "clarify", conf=0.95, case_file=serialize_state(pending))

    runner = _resolve_runner(request, user_id=user_id, workspace_id=workspace_id)

    # ── TOOL_ONLY_MODE short-circuit (stream variant) ─────────────────
    # Same intent as the sync endpoint — block every LLM-using lane
    # when FPULSE_TOOL_ONLY_MODE=1. Fast-lane already missed by the
    # time we get here.
    from fpulse.ai.tool_only_mode import is_enabled as _tool_only_enabled
    from fpulse.ai.tool_only_mode import unavailable_response_text as _tool_only_text
    if _tool_only_enabled():
        _log_router_decision(
            prompt=req.user_intent, page=req.page_context.page,
            chosen_path="tool_only_mode_block_stream", intent=None,
            confidence=0.0, latency_ms=0,
            reason="FPULSE_TOOL_ONLY_MODE=1; no fast-lane match",
            workspace_id=workspace_id, user_id=user_id,
        )
        return _instant_sse(
            _tool_only_text(), "tool_only_mode",
            conf=1.0, case_file=case_file_out,
        )

    # ── Hybrid lane (May 17 2026) ────────────────────────────────────
    # Sits between fast-lane (no LLM) and single-shot (LLM but no tool
    # data). For prompts with reasoning markers that ALSO map to a
    # fast-lane data intent — e.g. "Why are pipelines slow today?" —
    # runs ONE tool fetch + ONE LLM format pass instead of the 6-step
    # agent loop. Total ~4-9 s on local Ollama vs. 30-180 s.
    # Conservative — falls through to single-shot / full agent on any
    # ambiguity. Skipped when the read tier is unavailable (no point
    # fetching tool data we can't run).
    if info.provider != "none" and ToolTier.READ in cap and not _multi_step:
        from fpulse.ai.hybrid import run_hybrid, should_use_hybrid
        hybrid_match = should_use_hybrid(req.user_intent)
        if hybrid_match is not None:
            hybrid_intent, hybrid_conf, hybrid_reason = hybrid_match
            try:
                from fpulse.main import app_state  # type: ignore
                _app_state = app_state
            except Exception:
                _app_state = None

            import uuid as _uuid
            run_id = str(_uuid.uuid4())

            async def _hybrid_stream():
                start = {"event": "run_start", "run_id": run_id, "page": req.page_context.page}
                yield f"data: {json.dumps(start)}\n\n"
                # Step 1 — surfaces the deterministic tool fetch under
                # the friendly tool label the frontend's TOOL_STATUS_LABELS
                # map already knows ("Checking recent executions…" etc.).
                yield f"data: {json.dumps({'event': 'iteration', 'iteration': 1, 'max': 2})}\n\n"
                yield f"data: {json.dumps({'event': 'tool_start', 'tool_name': hybrid_intent.name})}\n\n"

                # Token deltas — drain from a queue while run_hybrid runs.
                token_q: asyncio.Queue = asyncio.Queue()

                def _on_token(delta: str) -> None:
                    try:
                        token_q.put_nowait(delta)
                    except Exception:
                        pass

                # The LLM format pass is Step 2; emit the iteration tick
                # right before run_hybrid kicks off the LLM call. We don't
                # know the precise instant the handler finishes (it's
                # awaited inside run_hybrid), so the UI may briefly show
                # "Checking…" before transitioning to the token stream —
                # acceptable; it's still honest about what's happening.
                async def _runner():
                    try:
                        return await run_hybrid(
                            prompt=req.user_intent,
                            intent=hybrid_intent,
                            fast_ctx=fast_ctx,
                            llm_client=runner.llm_client,
                            page_context=page_ctx,
                            app_state=_app_state,
                            on_token=_on_token,
                        )
                    finally:
                        await token_q.put(None)  # sentinel

                runner_task = asyncio.create_task(_runner())
                # Emit Step 2 right before we start draining tokens — the
                # very first token arrival means the LLM phase started.
                step2_emitted = False
                while True:
                    delta = await token_q.get()
                    if delta is None:
                        break
                    if not step2_emitted:
                        yield f"data: {json.dumps({'event': 'iteration', 'iteration': 2, 'max': 2})}\n\n"
                        step2_emitted = True
                    yield f"data: {json.dumps({'event': 'token', 'delta': delta})}\n\n"

                hybrid_result = await runner_task
                # Escalation — the single fast-lane tool returned empty/generic.
                # Append a one-line hint that we can dig deeper if asked, so the
                # user isn't left with just "no record of X" when their question
                # was "why did X fail?". Full mid-stream agent-loop escalation
                # is a follow-up; today we expose the limitation honestly.
                if hybrid_result is not None and hybrid_result.escalate:
                    _log_router_decision(
                        prompt=req.user_intent, page=req.page_context.page,
                        chosen_path="hybrid_escalate_stream",
                        intent=f"hybrid.{hybrid_intent.name}",
                        confidence=hybrid_conf,
                        latency_ms=hybrid_result.elapsed_ms,
                        reason=hybrid_result.escalate_reason,
                        workspace_id=workspace_id, user_id=user_id,
                    )
                    hint = (
                        "\n\n_I only checked one place. "
                        "If you'd like me to dig further, ask "
                        "\"check connections and recent failures too.\"_"
                    )
                    text_with_hint = (hybrid_result.text or "") + hint
                else:
                    text_with_hint = hybrid_result.text if hybrid_result else ""
                final = {
                    "event": "final",
                    "run_id": run_id,
                    "final_text": text_with_hint,
                    "outcome": "success" if hybrid_result and hybrid_result.text else "llm_failure",
                    "iterations": 2,
                    "elapsed_ms": hybrid_result.elapsed_ms if hybrid_result else 0,
                    "steps": [],
                    "tool_results": [],
                    "cost": {
                        "tokens_in": hybrid_result.tokens_in if hybrid_result else 0,
                        "tokens_out": hybrid_result.tokens_out if hybrid_result else 0,
                        "estimated_usd": _estimate_cost_usd(
                            info.provider,
                            hybrid_result.tokens_in if hybrid_result else 0,
                            hybrid_result.tokens_out if hybrid_result else 0,
                            model=info.model,
                        ),
                        "provider": info.provider,
                        "model": info.model,
                    },
                    "no_provider": False,
                    "instant": False,
                    "instant_intent": f"hybrid.{hybrid_intent.name}",
                    "instant_confidence": hybrid_conf,
                    "served_from_page": False,
                    "case_file": case_file_out,
                }
                yield f"data: {json.dumps(final)}\n\n"

            return StreamingResponse(_hybrid_stream(), media_type="text/event-stream")

    # ── Single-shot path on the stream endpoint ──────────────────────
    # Same classifier as the sync endpoint. Streams the LLM tokens as
    # they arrive so the user sees text appearing immediately even
    # though only ONE LLM call is made.
    if info.provider != "none":
        from fpulse.ai.single_shot import run_single_shot, should_use_single_shot
        if should_use_single_shot(req.user_intent):
            try:
                from fpulse.main import app_state  # type: ignore
                _app_state = app_state
            except Exception:
                _app_state = None

            import uuid as _uuid
            run_id = str(_uuid.uuid4())

            async def _shot_stream():
                start = {"event": "run_start", "run_id": run_id, "page": req.page_context.page}
                yield f"data: {json.dumps(start)}\n\n"
                # max=1 — single-shot only ever fires one iteration. The
                # frontend reads this to show "Thinking through step 1 of 1…"
                # instead of "step 1 of 6" which falsely implies more work
                # is coming. See _resolve_max_iterations in fpulse.ai.agent.
                yield f"data: {json.dumps({'event': 'iteration', 'iteration': 1, 'max': 1})}\n\n"

                # Token deltas are pushed onto a queue from the LLM client's
                # callback; the SSE generator drains them between awaits.
                token_q: asyncio.Queue = asyncio.Queue()

                def _on_token(delta: str) -> None:
                    try:
                        token_q.put_nowait(delta)
                    except Exception:
                        pass

                async def _runner():
                    try:
                        return await run_single_shot(
                            prompt=req.user_intent,
                            page_context=page_ctx,
                            llm_client=runner.llm_client,
                            app_state=_app_state,
                            on_token=_on_token,
                        )
                    finally:
                        await token_q.put(None)  # sentinel

                runner_task = asyncio.create_task(_runner())
                while True:
                    delta = await token_q.get()
                    if delta is None:
                        break
                    yield f"data: {json.dumps({'event': 'token', 'delta': delta})}\n\n"

                shot = await runner_task
                final = {
                    "event": "final",
                    "run_id": run_id,
                    "final_text": shot.text if shot else "",
                    "outcome": "success" if shot and shot.text else "llm_failure",
                    "iterations": 1,
                    "elapsed_ms": shot.elapsed_ms if shot else 0,
                    "steps": [],
                    "tool_results": [],
                    "cost": {
                        "tokens_in": shot.tokens_in if shot else 0,
                        "tokens_out": shot.tokens_out if shot else 0,
                        "estimated_usd": _estimate_cost_usd(
                            info.provider,
                            shot.tokens_in if shot else 0,
                            shot.tokens_out if shot else 0,
                            model=info.model,
                        ),
                        "provider": info.provider,
                        "model": info.model,
                    },
                    "no_provider": False,
                    "instant": False,
                    "instant_intent": "single_shot",
                    "instant_confidence": 0.0,
                    "served_from_page": False,
                }
                yield f"data: {json.dumps(final)}\n\n"

            return StreamingResponse(_shot_stream(), media_type="text/event-stream")

    queue: asyncio.Queue = asyncio.Queue()
    SENTINEL = object()

    def _on_event(event: dict[str, Any]) -> None:
        # Sync callback from inside AgentRunner — non-blocking enqueue.
        try:
            queue.put_nowait(event)
        except Exception:
            pass

    async def _runner_task():
        try:
            result = await runner.run(
                page_context=page_ctx,
                user_intent=req.user_intent,
                allowed_tiers=allowed_tiers,
                max_tokens=req.max_tokens,
                on_event=_on_event,
                mode=req.mode,
            )
            final_payload = {
                "event": "final",
                "run_id": result.run_id,
                "final_text": result.final_text,
                "outcome": result.outcome,
                "iterations": result.iterations,
                "elapsed_ms": result.elapsed_ms,
                "steps": [_trace_step_to_dict(s) for s in result.steps],
                "tool_results": result.tool_results,
                "cost": {
                    "tokens_in": result.total_tokens_in,
                    "tokens_out": result.total_tokens_out,
                    "estimated_usd": _estimate_cost_usd(
                        info.provider, result.total_tokens_in, result.total_tokens_out,
                        model=info.model,
                    ),
                    "provider": info.provider,
                    "model": info.model,
                },
                "no_provider": False,
            }
            queue.put_nowait(final_payload)
        except Exception as e:
            queue.put_nowait({
                "event": "error",
                "error": f"{type(e).__name__}: {str(e)[:200]}",
            })
        finally:
            queue.put_nowait(SENTINEL)

    task = asyncio.create_task(_runner_task())

    async def _event_stream():
        try:
            while True:
                # Heartbeat every 15s so proxies don't close the connection
                # mid-inference (Ollama on CPU can take 30+s for one call).
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is SENTINEL:
                    break
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Diagnostic endpoint — useful for the frontend to hide AI UI when no provider
# ---------------------------------------------------------------------------


@router.get("/agent/traces")
def list_agent_traces(request: Request, limit: int = 20) -> dict[str, Any]:
    """List the caller's recent agent runs (newest first).

    Anonymous (OSS local dev): scoped to workspace.
    Authenticated: scoped to user.
    """
    user = current_user_optional(request)
    user_id = getattr(user, "id", None) if user else None
    workspace_id = current_workspace_id(request)
    try:
        from fpulse.main import app_state
        store = app_state.get("trace_store")
    except Exception:
        store = None
    if store is None:
        return {"traces": [], "total": 0}
    rows = store.list_recent(
        user_id=user_id,
        workspace_id=workspace_id,
        limit=max(1, min(int(limit or 20), 200)),
    )
    return {"traces": rows, "total": len(rows)}


@router.get("/agent/trace/{run_id}")
def get_agent_trace(run_id: str, request: Request) -> dict[str, Any]:
    """Fetch a single agent trace by run_id.

    Authorization: only the caller who owns the trace can fetch.
    Workspace admins (and instance super_admins) can fetch any trace
    in their workspace. Anonymous OSS local-dev mode trusts the caller
    if the trace's user_id is null/empty.
    """
    try:
        from fpulse.main import app_state
        store = app_state.get("trace_store")
    except Exception:
        store = None
    if store is None:
        raise HTTPException(status_code=404, detail="Trace store not available")

    trace = store.get(run_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace not found")

    # Authorization gate. OSS local-dev with anonymous traces is permissive;
    # any authenticated context tightens to ownership / admin / workspace.
    user = current_user_optional(request)
    user_id = getattr(user, "id", None) if user else None
    user_role = (getattr(user, "role", "") if user else "").lower()
    workspace_id = current_workspace_id(request)
    raw_trace_user = trace.get("user_id") or ""
    # Treat both empty and the literal placeholder "anonymous" as no-owner.
    trace_user = "" if raw_trace_user in ("", "anonymous") else raw_trace_user
    trace_ws = trace.get("workspace_id") or "default"

    if user_id:
        is_owner = trace_user == user_id
        is_admin = user_role in ("super_admin", "admin")
        is_same_ws = trace_ws == workspace_id
        # Owner can always read; admins can read same-workspace traces;
        # any user can read anonymous traces in their workspace (OSS local-dev).
        if not (
            is_owner
            or (is_admin and is_same_ws)
            or (not trace_user and is_same_ws)
        ):
            raise HTTPException(status_code=403, detail="Not authorized to view this trace")
    else:
        # Anonymous caller: only allow when the trace itself is anonymous.
        if trace_user:
            raise HTTPException(status_code=403, detail="Sign in to view this trace")

    return trace


# ---------------------------------------------------------------------------
# Copilot-style contextual suggestions — what the user is most likely to ask
# given the page they're on. Drives the "Suggested prompts" panel inside the
# chat dock's empty state.
#
# Deliberately deterministic — no LLM call. Suggestions update on every page
# navigation; should be cheap and instant.
# ---------------------------------------------------------------------------


class AgentSuggestion(BaseModel):
    label: str       # short user-facing button text
    prompt: str      # the actual user_intent text we'll submit
    icon: str | None = None
    category: str = "general"  # "general" | "diagnose" | "build" | "monitor"


@router.get("/agent/suggestions")
def agent_suggestions(
    request: Request,
    page: str = "",
    selected_count: int = 0,
) -> dict[str, list[AgentSuggestion]]:
    """Per-page contextual quick prompts.

    The frontend's `useCurrentPageContext()` hook publishes a `page` key
    like 'pipelines.list' / 'executions.detail' / 'editor.canvas'. We
    return 4-6 suggestions tailored to that page; the chat panel renders
    them as one-click buttons below the empty-state copy.
    """
    register_initial_tools()  # idempotent — keeps the tool registry warm

    PER_PAGE: dict[str, list[AgentSuggestion]] = {
        "dashboard": [
            AgentSuggestion(
                label="What's running right now?",
                prompt="What pipelines are currently running and how far along are they?",
                category="monitor",
            ),
            AgentSuggestion(
                label="Workspace health summary",
                prompt="Give me a one-paragraph health summary for this workspace.",
                category="monitor",
            ),
            AgentSuggestion(
                label="What pipelines failed today?",
                prompt="Which pipelines failed in the last 24 hours and why?",
                category="diagnose",
            ),
            AgentSuggestion(
                label="Top 3 slowest pipelines",
                prompt="What are the 3 slowest pipelines this week?",
                category="monitor",
            ),
            AgentSuggestion(
                label="What runs in the next hour?",
                prompt="Which scheduled pipelines will fire in the next 60 minutes?",
                category="monitor",
            ),
            AgentSuggestion(
                label="Anomalies in recent runs",
                prompt="Are there any anomalies in recent pipeline runs vs the 7-day baseline?",
                category="diagnose",
            ),
        ],
        "pipelines.list": [
            AgentSuggestion(
                label="Build me a new pipeline",
                prompt="Build me a pipeline that: ",
                category="build",
            ),
            AgentSuggestion(
                label="List my active pipelines",
                prompt="Show me all active pipelines I have access to.",
                category="general",
            ),
            AgentSuggestion(
                label="Pipelines pending review",
                prompt="Which pipelines are pending approval right now?",
                category="general",
            ),
            AgentSuggestion(
                label="What's running right now?",
                prompt="Which pipelines are currently running?",
                category="monitor",
            ),
            AgentSuggestion(
                label="Recently modified",
                prompt="Which pipelines have been modified in the last 7 days?",
                category="general",
            ),
        ],
        "executions": [
            AgentSuggestion(
                label="Diagnose the latest failure",
                prompt="Diagnose the most recent failed execution and suggest a fix.",
                category="diagnose",
            ),
            AgentSuggestion(
                label="Why is this run slow?",
                prompt="Why is the most recent successful run slower than usual?",
                category="diagnose",
            ),
            AgentSuggestion(
                label="Summarize today's runs",
                prompt="Summarize today's pipeline executions in a single paragraph.",
                category="monitor",
            ),
            AgentSuggestion(
                label="Failure pattern analysis",
                prompt="Are there any patterns in recent failures (same step, same time, same connection)?",
                category="diagnose",
            ),
        ],
        "editor.canvas": [
            AgentSuggestion(
                label="Suggest the next node",
                prompt="Looking at the current canvas, what node should I add next?",
                category="build",
            ),
            AgentSuggestion(
                label="Explain this pipeline",
                prompt="Walk me through what this pipeline does step-by-step.",
                category="build",
            ),
            AgentSuggestion(
                label="Find missing pieces",
                prompt="What's missing from this pipeline before it can run safely in PROD?",
                category="build",
            ),
            AgentSuggestion(
                label="Optimization ideas",
                prompt="Suggest optimizations for this pipeline.",
                category="build",
            ),
        ],
        "connections": [
            AgentSuggestion(
                label="Which connections are broken?",
                prompt="Which connections have failed their last health check?",
                category="diagnose",
            ),
            AgentSuggestion(
                label="List connection types",
                prompt="What connection types do I currently have configured?",
                category="general",
            ),
            AgentSuggestion(
                label="Diagnose a connection failure",
                prompt="Help me diagnose why a connection is failing.",
                category="diagnose",
            ),
        ],
        "schedules": [
            AgentSuggestion(
                label="What runs in the next hour?",
                prompt="Which scheduled pipelines will trigger in the next hour?",
                category="monitor",
            ),
            AgentSuggestion(
                label="Schedule conflicts",
                prompt="Are any of my schedules likely to overlap?",
                category="diagnose",
            ),
        ],
        "alerts": [
            AgentSuggestion(
                label="Active alerts",
                prompt="What alerts are currently firing?",
                category="monitor",
            ),
            AgentSuggestion(
                label="Suggest alerts for this pipeline",
                prompt="Suggest alert rules I should add for the pipelines I have.",
                category="build",
            ),
        ],
        "reports": [
            AgentSuggestion(
                label="Summarize this week's runs",
                prompt="Give me a one-page summary of this week's pipeline runs.",
                category="monitor",
            ),
            AgentSuggestion(
                label="Failure narrative",
                prompt="Generate a narrative report for recent failures.",
                category="monitor",
            ),
        ],
        "help": [
            AgentSuggestion(
                label="How do I build my first pipeline?",
                prompt="Walk me through building my first pipeline step by step.",
                category="general",
            ),
            AgentSuggestion(
                label="What node types are available?",
                prompt="List the node types I can use, grouped by category.",
                category="general",
            ),
            AgentSuggestion(
                label="How do connections work?",
                prompt="Explain how connections work in F-Pulse and which connection types are supported.",
                category="general",
            ),
            AgentSuggestion(
                label="How do I schedule a pipeline?",
                prompt="How do I set up a schedule to run a pipeline automatically?",
                category="general",
            ),
            AgentSuggestion(
                label="Show keyboard shortcuts",
                prompt="What keyboard shortcuts are available in the editor?",
                category="general",
            ),
            AgentSuggestion(
                label="What's in F-Pulse OSS vs Plus?",
                prompt="What features are in F-Pulse OSS Free vs F-Pulse Plus?",
                category="general",
            ),
        ],
    }

    # Default: a small set that works on any page.
    DEFAULT = [
        AgentSuggestion(label="What can you do?", prompt="What can you help me with on this page?", category="general"),
        AgentSuggestion(label="Workspace overview", prompt="Give me a quick overview of my workspace.", category="monitor"),
        AgentSuggestion(label="Recent failures", prompt="Show me recent pipeline failures.", category="diagnose"),
    ]

    page_key = page.lower().strip()
    # Loose match — 'pipelines.list' OR 'pipelines' both hit the pipelines bucket.
    items = (
        PER_PAGE.get(page_key)
        or PER_PAGE.get(page_key.split(".", 1)[0])
        or DEFAULT
    )

    if selected_count > 0:
        # When the user has a selection, prepend a "selection-aware" prompt.
        items = [
            AgentSuggestion(
                label=f"Tell me about the {selected_count} selected item{'s' if selected_count != 1 else ''}",
                prompt=f"Summarize the {selected_count} item{'s' if selected_count != 1 else ''} I have selected on this page.",
                category="general",
            ),
            *items,
        ]

    return {"suggestions": items[:6]}


@router.post("/agent/refresh-provider")
def agent_refresh_provider(request: Request) -> dict[str, Any]:
    """Invalidate the Ollama autoprobe cache and re-resolve the provider.

    The frontend calls this immediately after a successful Ollama pull so
    the agent picks up the newly-installed model without waiting for the
    5-minute cache TTL. Returns the freshly-resolved provider info so the
    UI can update without a separate /agent/status call.
    """
    from fpulse.planner.ai_client import invalidate_ollama_autoprobe

    invalidate_ollama_autoprobe()
    user = current_user_optional(request)
    user_id = getattr(user, "id", None) if user else None
    workspace_id = current_workspace_id(request)
    info = get_provider_info(user_id=user_id, workspace_id=workspace_id)
    register_initial_tools()  # idempotent
    return {
        "available": info.has_provider,
        "provider": info.provider,
        "model": info.model,
        "tool_count": len(default_registry()),
    }


@router.get("/agent/draft/{draft_id}/preview")
def get_draft_preview(draft_id: str, request: Request) -> dict[str, Any]:
    """Return a structured diff of an AI-drafted pipeline change.

    Used by the Copilot ConfirmationCard so the user can see exactly what
    will change BEFORE clicking Confirm. The shape is suitable for direct
    consumption by the frontend's ``DiffPreview.tsx`` (Review #2 + #1
    follow-up — "AI suggests, humans confirm" needs a visible diff).

    Returns 404 when the draft has expired (in-process eviction at 64
    drafts deep) — the caller should re-issue the original request.
    """
    from fpulse.ai.draft_diff import compute_diff
    from fpulse.ai.tools.draft_pipeline_from_intent import get_draft
    from fpulse.ai.tools.modify_pipeline_step import _load_pipeline_ir

    draft = get_draft(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft expired or unknown")

    # Strip the private "_modification_of" tag before the diff — it's
    # metadata, not a real IR field, and would confuse the diff if a
    # future change started keying on its presence.
    payload = dict(draft)
    target_existing_id = payload.pop("_modification_of", None)

    before_ir: dict[str, Any] | None = None
    if target_existing_id:
        # Modification of an existing pipeline — fetch the current state
        # to diff against.
        workspace_id = current_workspace_id(request) or "default"
        before_ir = _load_pipeline_ir(target_existing_id, workspace_id)

    diff = compute_diff(before_ir=before_ir, after_ir=payload)
    return {
        "draft_id": draft_id,
        "target_pipeline_id": target_existing_id,
        "is_modification": bool(target_existing_id),
        "after_pipeline_name": payload.get("name") or "",
        "diff": diff.to_jsonable(),
    }


@router.get("/agent/status")
def agent_status(request: Request) -> dict[str, Any]:
    user = current_user_optional(request)
    user_id = getattr(user, "id", None) if user else None
    workspace_id = current_workspace_id(request)
    info = get_provider_info(user_id=user_id, workspace_id=workspace_id)
    register_initial_tools()  # idempotent — keeps tool_count honest
    # Surface the wall-clock cap so the UI can warn the user when their
    # CPU-Ollama setup needs a higher cap than the current default.
    # `max_iterations` reports the cap for the CURRENTLY-RESOLVED provider
    # (3 for local Ollama, 6 for cloud) — the SSE stream still emits the
    # per-run cap on each `iteration` event, this is just the steady-state.
    from fpulse.ai.agent import (
        WALL_CLOCK_TIMEOUT_SECONDS,
        MAX_ITERATIONS,
        LOCAL_MAX_ITERATIONS,
        _resolve_max_iterations,
    )
    from fpulse.ai.tool_only_mode import is_enabled as _tool_only_enabled
    return {
        "available": info.has_provider,
        "provider": info.provider,
        "model": info.model,
        "tool_count": len(default_registry()),
        "wall_clock_timeout_seconds": int(WALL_CLOCK_TIMEOUT_SECONDS),
        "max_iterations": int(_resolve_max_iterations(info.provider)),
        "max_iterations_cloud": int(MAX_ITERATIONS),
        "max_iterations_local": int(LOCAL_MAX_ITERATIONS),
        # TOOL_ONLY_MODE state — surfaced read-only so Settings can show
        # the operator's air-gapped posture without needing to inspect env.
        "tool_only_mode": _tool_only_enabled(),
    }
