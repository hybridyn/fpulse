"""
Tests for the AgentRunner tool-use loop.

Uses a deterministic FakeLLMClient so the loop exercises real branching
behavior (single tool call, multi-tool, iteration cap, error handling)
without any network calls or external dependencies.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from fpulse.ai.agent import (
    LOCAL_MAX_ITERATIONS,
    MAX_ITERATIONS,
    SYSTEM_PROMPT_TEMPLATE,
    _PROMPT_SIG_NAME,
    _resolve_max_iterations,
    AgentLLMClient,
    AgentRunner,
    LLMResponse,
    LLMToolUse,
)
from fpulse.ai.context import PageContext
from fpulse.ai.idempotency import default_store as default_idempotency_store
from fpulse.ai.idempotency import reset_default_store_for_tests as reset_idempotency_store
from fpulse.ai.prompt_signing import default_signer
from fpulse.ai.tools import (
    ToolRegistry,
    ToolTier,
    register_initial_tools,
)


# ---------------------------------------------------------------------------
# Fake LLM client — emits a script of canned LLMResponse values
# ---------------------------------------------------------------------------


@dataclass
class FakeLLMClient:
    """Returns successive LLMResponse values from `script`.

    On each `call()`, pops the next entry. Raises if script is exhausted.
    """

    script: list[LLMResponse]
    calls: list[dict] = field(default_factory=list)

    async def call(self, *, system, messages, tools):
        self.calls.append({
            "system": system,
            "messages": messages,
            "tools": tools,
        })
        if not self.script:
            raise AssertionError("FakeLLMClient script exhausted")
        return self.script.pop(0)


def _ctx() -> PageContext:
    return PageContext(
        page="pipelines.list",
        user_id="u-1",
        tenant_id="t-1",
        workspace_id="ws-1",
        environment="dev",
        visible_ids=("p-1", "p-2"),
        selected_ids=("p-1",),
        role="data_engineer",
    )


def _registry_read_only() -> ToolRegistry:
    reg = ToolRegistry()
    register_initial_tools(reg)
    return reg


# ---------------------------------------------------------------------------
# Single tool call → final answer
# ---------------------------------------------------------------------------


def test_agent_invokes_one_tool_then_returns_text():
    fake = FakeLLMClient(script=[
        # First call: model wants to call summarize_pipeline
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(id="tu-1", name="summarize_pipeline", input={"pipeline_id": "p-1"})],
            stop_reason="tool_use",
            tokens_in=100,
            tokens_out=20,
        ),
        # Second call: model is done, returns final text
        LLMResponse(
            text="Pipeline p-1 has 12 nodes, 2 sources, 1 destination.",
            tool_uses=[],
            stop_reason="end_turn",
            tokens_in=140,
            tokens_out=30,
        ),
    ])
    runner = AgentRunner(registry=_registry_read_only(), llm_client=fake)
    result = asyncio.run(runner.run(
        page_context=_ctx(),
        user_intent="Summarize pipeline p-1",
    ))
    assert result.outcome == "success"
    assert "12 nodes" in result.final_text
    assert len(result.steps) == 1
    assert result.steps[0].tool_name == "summarize_pipeline"
    assert result.steps[0].outcome == "success"
    assert result.steps[0].input_hash  # not empty
    assert result.steps[0].output_hash  # not empty
    assert result.total_tokens_in == 240
    assert result.total_tokens_out == 50


# ---------------------------------------------------------------------------
# No tool calls — model answers directly
# ---------------------------------------------------------------------------


def test_agent_returns_directly_when_no_tools_needed():
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="Hi! I can help you summarize pipelines, inspect connections, query metrics, or compose draft reports. What would you like?",
            tool_uses=[],
            stop_reason="end_turn",
            tokens_in=80,
            tokens_out=40,
        ),
    ])
    runner = AgentRunner(registry=_registry_read_only(), llm_client=fake)
    result = asyncio.run(runner.run(
        page_context=_ctx(),
        user_intent="Hello",
    ))
    assert result.outcome == "success"
    assert "summarize pipelines" in result.final_text
    assert result.steps == []


# ---------------------------------------------------------------------------
# Multi-tool sequence
# ---------------------------------------------------------------------------


def test_agent_handles_multi_step_sequence():
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(id="tu-1", name="summarize_pipeline", input={"pipeline_id": "p-1"})],
            stop_reason="tool_use",
        ),
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(id="tu-2", name="inspect_connections", input={"pipeline_id": "p-1"})],
            stop_reason="tool_use",
        ),
        LLMResponse(
            text="Pipeline p-1 looks healthy. 2 connections, both passing tests.",
            tool_uses=[],
            stop_reason="end_turn",
        ),
    ])
    runner = AgentRunner(registry=_registry_read_only(), llm_client=fake)
    result = asyncio.run(runner.run(
        page_context=_ctx(),
        user_intent="Check pipeline p-1 health",
    ))
    assert result.outcome == "success"
    assert len(result.steps) == 2
    assert [s.tool_name for s in result.steps] == ["summarize_pipeline", "inspect_connections"]


# ---------------------------------------------------------------------------
# Unknown / not-allowed tool
# ---------------------------------------------------------------------------


def test_agent_blocks_unknown_tool_with_policy_block_outcome():
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(id="tu-x", name="not_a_tool", input={})],
            stop_reason="tool_use",
        ),
        # Model recovers and returns text after seeing the error
        LLMResponse(
            text="I tried to use a tool that isn't available.",
            tool_uses=[],
            stop_reason="end_turn",
        ),
    ])
    runner = AgentRunner(registry=_registry_read_only(), llm_client=fake)
    result = asyncio.run(runner.run(
        page_context=_ctx(),
        user_intent="do something weird",
    ))
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.outcome == "policy_block"
    assert "not_a_tool" in step.tool_name
    assert "tool_not_in_allowed_tiers" in step.policy_rules_fired


# ---------------------------------------------------------------------------
# Tool handler raising → tool_failure outcome
# ---------------------------------------------------------------------------


def test_agent_records_tool_failure_when_handler_raises():
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="",
            # summarize_pipeline raises if pipeline_id is empty
            tool_uses=[LLMToolUse(id="tu-1", name="summarize_pipeline", input={"pipeline_id": ""})],
            stop_reason="tool_use",
        ),
        LLMResponse(
            text="The pipeline ID was missing.",
            tool_uses=[],
            stop_reason="end_turn",
        ),
    ])
    runner = AgentRunner(registry=_registry_read_only(), llm_client=fake)
    # Empty selected_ids + visible_ids so summarize_pipeline can't fall
    # back to ctx hints — see fpulse/ai/tools/summarize_pipeline.py lines
    # 36-41 where the fallback kicks in. Without this, the default
    # _ctx() provides selected_ids=("p-1",) and the handler silently
    # resolves "" → "p-1" instead of raising ValueError.
    page_ctx_no_selection = PageContext(
        page="pipelines.list",
        user_id="u-1",
        tenant_id="t-1",
        workspace_id="ws-1",
        environment="dev",
        visible_ids=(),
        selected_ids=(),
        role="data_engineer",
    )
    result = asyncio.run(runner.run(
        page_context=page_ctx_no_selection,
        user_intent="bad input",
    ))
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.outcome == "tool_failure"
    assert "ValueError" in step.decision_reason


# ---------------------------------------------------------------------------
# Iteration cap
# ---------------------------------------------------------------------------


def test_agent_stops_at_max_iterations():
    # Script forever asking for tool calls — should hit the cap
    long_script = [
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(id=f"tu-{i}", name="summarize_pipeline", input={"pipeline_id": f"p-{i}"})],
            stop_reason="tool_use",
        )
        for i in range(MAX_ITERATIONS + 5)
    ]
    fake = FakeLLMClient(script=long_script)
    runner = AgentRunner(registry=_registry_read_only(), llm_client=fake)
    result = asyncio.run(runner.run(
        page_context=_ctx(),
        user_intent="loop forever",
    ))
    # Should hit MAX_ITERATIONS and bail with timeout outcome
    assert result.outcome == "timeout"
    assert len(result.steps) == MAX_ITERATIONS


def test_resolve_max_iterations_is_provider_aware():
    """Local CPU providers get the tighter cap; cloud keeps the full loop.

    See agent.py: qwen2.5:7b on CPU (the 2026-05-19 tool-use floor) regularly
    runs past 240 s if allowed 6 steps; capping at 3 fails fast and lets the
    user retry on a faster provider or use a fast-lane phrase instead.
    """
    # Default — no provider hint → cloud default
    assert _resolve_max_iterations(None) == MAX_ITERATIONS
    assert _resolve_max_iterations("anthropic") == MAX_ITERATIONS
    assert _resolve_max_iterations("openai") == MAX_ITERATIONS
    assert _resolve_max_iterations("openrouter") == MAX_ITERATIONS
    # Local CPU providers — tighter cap
    assert _resolve_max_iterations("ollama") == LOCAL_MAX_ITERATIONS
    assert _resolve_max_iterations("OLLAMA") == LOCAL_MAX_ITERATIONS  # case-insensitive
    # Local cap must be strictly less than cloud cap, otherwise the whole
    # point of the resolver is gone.
    assert LOCAL_MAX_ITERATIONS < MAX_ITERATIONS


def test_resolve_max_iterations_env_override(monkeypatch):
    """Operator override via FPULSE_AGENT_MAX_ITERATIONS beats both defaults."""
    monkeypatch.setenv("FPULSE_AGENT_MAX_ITERATIONS", "2")
    assert _resolve_max_iterations("ollama") == 2
    assert _resolve_max_iterations("anthropic") == 2
    # Out-of-range values fall through to the provider-aware default
    monkeypatch.setenv("FPULSE_AGENT_MAX_ITERATIONS", "999")
    assert _resolve_max_iterations("ollama") == LOCAL_MAX_ITERATIONS
    monkeypatch.setenv("FPULSE_AGENT_MAX_ITERATIONS", "not_a_number")
    assert _resolve_max_iterations("anthropic") == MAX_ITERATIONS


# ---------------------------------------------------------------------------
# LLM exception → llm_failure outcome
# ---------------------------------------------------------------------------


@dataclass
class ExplodingLLMClient:
    async def call(self, *, system, messages, tools):
        raise RuntimeError("provider 503")


def test_agent_handles_llm_exception_with_llm_failure_outcome():
    runner = AgentRunner(registry=_registry_read_only(), llm_client=ExplodingLLMClient())
    result = asyncio.run(runner.run(
        page_context=_ctx(),
        user_intent="any intent",
    ))
    assert result.outcome == "llm_failure"
    assert len(result.steps) == 1
    assert result.steps[0].outcome == "llm_failure"


# ---------------------------------------------------------------------------
# Allowed tiers — write tools blocked when only READ allowed
# ---------------------------------------------------------------------------


def test_agent_with_read_only_tier_blocks_write_tools():
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="",
            # compose_report is SAFE_WRITE — should be filtered out
            tool_uses=[LLMToolUse(
                id="tu-1",
                name="compose_report",
                input={"template": "monthly", "idempotency_key": "k"},
            )],
            stop_reason="tool_use",
        ),
        LLMResponse(
            text="I tried compose_report but it's not allowed.",
            tool_uses=[],
            stop_reason="end_turn",
        ),
    ])
    runner = AgentRunner(registry=_registry_read_only(), llm_client=fake)
    # Default allowed_tiers = (READ,) so compose_report should be blocked
    result = asyncio.run(runner.run(
        page_context=_ctx(),
        user_intent="draft a report",
    ))
    step = result.steps[0]
    assert step.outcome == "policy_block"
    assert step.tool_name == "compose_report"


def test_agent_with_safe_write_tier_allows_compose_report():
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(
                id="tu-1",
                name="compose_report",
                input={"template": "monthly", "idempotency_key": "free.u-1.compose.r-1.v1"},
            )],
            stop_reason="tool_use",
        ),
        LLMResponse(
            text="Draft created.",
            tool_uses=[],
            stop_reason="end_turn",
        ),
    ])
    runner = AgentRunner(registry=_registry_read_only(), llm_client=fake)
    result = asyncio.run(runner.run(
        page_context=_ctx(),
        user_intent="draft a report",
        allowed_tiers=(ToolTier.READ, ToolTier.SAFE_WRITE),
    ))
    assert result.outcome == "success"
    assert result.steps[0].outcome == "success"
    assert result.steps[0].tool_name == "compose_report"


# ---------------------------------------------------------------------------
# Trace shape sanity
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Step 1.5b-2 governance integration tests
# ---------------------------------------------------------------------------


def test_rbac_blocks_viewer_attempting_safe_write():
    """Even if the request opts allow_safe_writes, viewer role can't reach SAFE_WRITE."""
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(
                id="tu-1",
                name="compose_report",
                input={"template": "monthly", "idempotency_key": "k"},
            )],
            stop_reason="tool_use",
        ),
        LLMResponse(text="Cannot do that.", tool_uses=[], stop_reason="end_turn"),
    ])
    runner = AgentRunner(registry=_registry_read_only(), llm_client=fake)
    # Pass viewer role even though the test fixture earlier used data_engineer
    ctx = PageContext(
        page="x", user_id="u-1", tenant_id="t-1", workspace_id="ws-1",
        environment="dev", role="viewer",
    )
    # Caller asks for SAFE_WRITE access — RBAC blocks because viewer can't write
    result = asyncio.run(runner.run(
        page_context=ctx,
        user_intent="x",
        allowed_tiers=(ToolTier.READ, ToolTier.SAFE_WRITE),
    ))
    step = result.steps[0]
    assert step.outcome == "policy_block"
    assert any("rbac:role_viewer_cannot_invoke_safe_write" in r for r in step.policy_rules_fired)


def test_policy_blocks_anonymous_write():
    """Default engine rule 'anonymous_blocked_for_writes' fires when user_id is empty."""
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(
                id="tu-1",
                name="compose_report",
                input={"template": "monthly"},
            )],
            stop_reason="tool_use",
        ),
        LLMResponse(text="ok", tool_uses=[], stop_reason="end_turn"),
    ])
    runner = AgentRunner(registry=_registry_read_only(), llm_client=fake)
    # Anonymous (no user_id), but role admin so RBAC passes; policy must still block
    ctx = PageContext(
        page="x", user_id="", tenant_id="t-1", workspace_id="ws-1",
        environment="dev", role="admin",
    )
    result = asyncio.run(runner.run(
        page_context=ctx,
        user_intent="x",
        allowed_tiers=(ToolTier.READ, ToolTier.SAFE_WRITE),
    ))
    step = result.steps[0]
    assert step.outcome == "policy_block"
    assert any("anonymous_blocked_for_writes" in r for r in step.policy_rules_fired)


def test_policy_blocks_prod_write_without_approval():
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(
                id="tu-1",
                name="compose_report",
                input={"template": "monthly"},
            )],
            stop_reason="tool_use",
        ),
        LLMResponse(text="blocked", tool_uses=[], stop_reason="end_turn"),
    ])
    runner = AgentRunner(registry=_registry_read_only(), llm_client=fake)
    ctx = PageContext(
        page="x", user_id="u-1", tenant_id="t-1", workspace_id="ws-1",
        environment="prod", role="admin",
    )
    result = asyncio.run(runner.run(
        page_context=ctx,
        user_intent="x",
        allowed_tiers=(ToolTier.READ, ToolTier.SAFE_WRITE),
        is_dry_run=False, has_approval=False,
    ))
    step = result.steps[0]
    assert step.outcome == "policy_block"
    assert any("no_prod_writes_without_approval" in r for r in step.policy_rules_fired)


def test_policy_allows_prod_write_with_approval():
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(
                id="tu-1",
                name="compose_report",
                input={"template": "monthly"},
            )],
            stop_reason="tool_use",
        ),
        LLMResponse(text="ok", tool_uses=[], stop_reason="end_turn"),
    ])
    runner = AgentRunner(registry=_registry_read_only(), llm_client=fake)
    ctx = PageContext(
        page="x", user_id="u-1", tenant_id="t-1", workspace_id="ws-1",
        environment="prod", role="admin",
    )
    reset_idempotency_store()
    result = asyncio.run(runner.run(
        page_context=ctx,
        user_intent="x",
        allowed_tiers=(ToolTier.READ, ToolTier.SAFE_WRITE),
        has_approval=True,
    ))
    step = result.steps[0]
    assert step.outcome == "success", step.decision_reason


def test_idempotency_cache_hit_returns_prior_result():
    """Same write inputs → second call hits the cache."""
    reset_idempotency_store()
    fake = FakeLLMClient(script=[
        # Call 1
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(id="tu-1", name="compose_report", input={"template": "monthly"})],
            stop_reason="tool_use",
        ),
        LLMResponse(text="done first", tool_uses=[], stop_reason="end_turn"),
        # Call 2 with IDENTICAL input — should be cache hit
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(id="tu-2", name="compose_report", input={"template": "monthly"})],
            stop_reason="tool_use",
        ),
        LLMResponse(text="done second", tool_uses=[], stop_reason="end_turn"),
    ])
    runner = AgentRunner(registry=_registry_read_only(), llm_client=fake)
    ctx = PageContext(
        page="x", user_id="u-1", tenant_id="t-1", workspace_id="ws-1",
        environment="dev", role="developer",
    )
    r1 = asyncio.run(runner.run(
        page_context=ctx, user_intent="x",
        allowed_tiers=(ToolTier.READ, ToolTier.SAFE_WRITE),
    ))
    r2 = asyncio.run(runner.run(
        page_context=ctx, user_intent="x",
        allowed_tiers=(ToolTier.READ, ToolTier.SAFE_WRITE),
    ))
    assert r1.steps[0].outcome == "success"
    assert r2.steps[0].outcome == "success"
    # Cache hit recorded in decision_reason
    assert "idempotent_cache_hit" in r2.steps[0].decision_reason
    # Output hash matches across the two runs (same cached result)
    assert r1.steps[0].output_hash == r2.steps[0].output_hash


def test_prompt_tamper_halts_run():
    """Mutating SYSTEM_PROMPT_TEMPLATE between sign + verify halts the agent."""
    # Re-sign with a known key, then deliberately fail verification by
    # poking the signer's stored sig
    signer = default_signer()
    signer.sign(_PROMPT_SIG_NAME, "tampered")  # different content => verify of real template fails

    fake = FakeLLMClient(script=[])  # never invoked
    runner = AgentRunner(registry=_registry_read_only(), llm_client=fake)
    ctx = PageContext(
        page="x", user_id="u-1", tenant_id="t-1", workspace_id="ws-1",
        environment="dev", role="developer",
    )
    result = asyncio.run(runner.run(page_context=ctx, user_intent="x"))
    assert result.outcome == "tool_failure"
    assert len(result.steps) == 1
    assert result.steps[0].decision_reason == "prompt_signature_mismatch"
    assert "halted" in result.final_text.lower() or "integrity" in result.final_text.lower()

    # Restore signature so other tests don't fail
    signer.sign(_PROMPT_SIG_NAME, SYSTEM_PROMPT_TEMPLATE)


def test_trace_step_includes_replay_safe_fields():
    fake = FakeLLMClient(script=[
        LLMResponse(
            text="",
            tool_uses=[LLMToolUse(id="tu-1", name="summarize_pipeline", input={"pipeline_id": "p-1"})],
            stop_reason="tool_use",
        ),
        LLMResponse(text="Done.", tool_uses=[], stop_reason="end_turn"),
    ])
    runner = AgentRunner(registry=_registry_read_only(), llm_client=fake)
    result = asyncio.run(runner.run(page_context=_ctx(), user_intent="x"))
    step = result.steps[0]
    # Fields from project_fpulse_ai_step0_locks.md §3
    assert step.step_id
    assert step.tool_name
    assert step.tool_tier in {"read", "safe_write", "high_impact_write"}
    assert step.input_hash
    assert step.output_hash
    assert step.timestamp.endswith("+00:00")  # UTC ISO 8601
    assert isinstance(step.latency_ms, int)
    assert isinstance(step.tokens_in, int)
    assert isinstance(step.tokens_out, int)
    assert isinstance(step.decision_reason, str)
    assert isinstance(step.redactions_applied, dict)
    assert step.outcome in {"success", "llm_failure", "tool_failure", "policy_block", "timeout", "user_rejection"}
