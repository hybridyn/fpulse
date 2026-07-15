"""Unit tests for the 2026-05-22 AI Assist plumbing additions.

Covers:
  - tool_selector.select_tools (page bucket, keyword boosts, cap, provider gate)
  - PageContext.to_extra_context_block (render + truncate)
  - PageContext.to_conversation_block (window + summary + truncation)
  - AgentRequest.mode validation

These are unit tests — they don't run the agent loop or hit an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

pytestmark = pytest.mark.unit


# ────────────────────────────────────────────────────────────────────────
# tool_selector
# ────────────────────────────────────────────────────────────────────────


@dataclass
class _StubTool:
    """Minimal stand-in for ToolDefinition for selector tests."""
    name: str


def _stub_tools(names: list[str]) -> list[_StubTool]:
    return [_StubTool(name=n) for n in names]


# A realistic catalogue mirroring the ~24 tools the agent registers today.
ALL_TOOL_NAMES = [
    "apply_pipeline_draft", "compose_report", "draft_alert_rule",
    "draft_pipeline_from_intent", "explain_step", "get_installation_health",
    "get_next_scheduled", "get_running_executions", "get_user_role",
    "inspect_connections", "list_alerts", "list_catalog", "list_executions",
    "list_pipelines", "list_projects", "list_schedules", "list_templates",
    "lookup_help_topic", "modify_pipeline_step", "query_metrics",
    "recall_history", "summarize_pipeline", "validate_pipeline",
    "workspace_overview",
]


def test_tool_selector_no_op_when_disabled(monkeypatch):
    monkeypatch.setenv("FPULSE_DISABLE_TOOL_SELECTOR", "1")
    from fpulse.ai.tool_selector import select_tools
    tools = _stub_tools(ALL_TOOL_NAMES)
    picked = select_tools(
        available_tools=tools, page="editor.canvas",
        prompt="what is wrong", provider_hint="ollama",
    )
    assert {t.name for t in picked} == set(ALL_TOOL_NAMES)


def test_tool_selector_on_for_cloud_with_looser_cap(monkeypatch):
    # 2026-06-18: scoping is now ON for cloud too, with a LOOSER cap than Ollama
    # (agent.py passes max_tools=14 for cloud vs 8 for Ollama) — every tool
    # schema is re-sent each loop, so trimming the long tail saves tokens on
    # cloud too while keeping broad coverage. Was previously off-for-cloud.
    monkeypatch.delenv("FPULSE_DISABLE_TOOL_SELECTOR", raising=False)
    monkeypatch.delenv("FPULSE_TOOL_SELECTOR", raising=False)
    from fpulse.ai.tool_selector import select_tools
    tools = _stub_tools(ALL_TOOL_NAMES)
    picked = select_tools(
        available_tools=tools, page="editor.canvas",
        prompt="any prompt", provider_hint="anthropic", max_tools=14,
    )
    names = {t.name for t in picked}
    assert len(picked) <= 14                       # cloud cap
    assert len(picked) < len(ALL_TOOL_NAMES)       # narrowed, not full passthrough
    for floor in ("workspace_overview", "recall_history", "lookup_help_topic"):
        assert floor in names                      # floor tools always survive


def test_tool_selector_env_off_restores_full_set(monkeypatch):
    # The documented escape hatch: FPULSE_TOOL_SELECTOR=off sends every tool.
    monkeypatch.delenv("FPULSE_DISABLE_TOOL_SELECTOR", raising=False)
    monkeypatch.setenv("FPULSE_TOOL_SELECTOR", "off")
    from fpulse.ai.tool_selector import select_tools
    tools = _stub_tools(ALL_TOOL_NAMES)
    picked = select_tools(
        available_tools=tools, page="editor.canvas",
        prompt="any prompt", provider_hint="anthropic", max_tools=8,
    )
    assert {t.name for t in picked} == set(ALL_TOOL_NAMES)


def test_tool_selector_on_for_ollama_by_default(monkeypatch):
    monkeypatch.delenv("FPULSE_DISABLE_TOOL_SELECTOR", raising=False)
    monkeypatch.delenv("FPULSE_TOOL_SELECTOR", raising=False)
    from fpulse.ai.tool_selector import select_tools
    tools = _stub_tools(ALL_TOOL_NAMES)
    picked = select_tools(
        available_tools=tools, page="editor.canvas",
        prompt="any prompt", provider_hint="ollama",
    )
    # Ollama: narrowed.
    assert len(picked) <= 8
    assert len(picked) < len(ALL_TOOL_NAMES)


def test_tool_selector_floor_tools_always_included(monkeypatch):
    monkeypatch.delenv("FPULSE_DISABLE_TOOL_SELECTOR", raising=False)
    from fpulse.ai.tool_selector import select_tools
    tools = _stub_tools(ALL_TOOL_NAMES)
    picked = select_tools(
        available_tools=tools, page="editor.canvas",
        prompt="random unrelated question", provider_hint="ollama",
    )
    names = {t.name for t in picked}
    # Floor tools: workspace_overview, recall_history, lookup_help_topic.
    for floor in ("workspace_overview", "recall_history", "lookup_help_topic"):
        assert floor in names, f"floor tool {floor} should always be selected"


def test_tool_selector_editor_bucket_brings_validate_and_explain(monkeypatch):
    monkeypatch.delenv("FPULSE_DISABLE_TOOL_SELECTOR", raising=False)
    from fpulse.ai.tool_selector import select_tools
    tools = _stub_tools(ALL_TOOL_NAMES)
    picked = select_tools(
        available_tools=tools, page="editor.canvas",
        prompt="what is wrong with this pipeline",
        provider_hint="ollama",
    )
    names = {t.name for t in picked}
    # Editor bucket should bring at least one of validate / summarize / explain.
    editor_tools = {"validate_pipeline", "summarize_pipeline", "explain_step"}
    assert names & editor_tools, "editor page should surface validate/summarize/explain"


def test_tool_selector_keyword_failure_boost(monkeypatch):
    monkeypatch.delenv("FPULSE_DISABLE_TOOL_SELECTOR", raising=False)
    from fpulse.ai.tool_selector import select_tools
    tools = _stub_tools(ALL_TOOL_NAMES)
    picked = select_tools(
        available_tools=tools, page="dashboard",
        prompt="why did the last run fail",
        provider_hint="ollama",
    )
    names = {t.name for t in picked}
    # "fail" keyword should pull list_executions in.
    assert "list_executions" in names


def test_tool_selector_keyword_connection_boost(monkeypatch):
    monkeypatch.delenv("FPULSE_DISABLE_TOOL_SELECTOR", raising=False)
    from fpulse.ai.tool_selector import select_tools
    tools = _stub_tools(ALL_TOOL_NAMES)
    picked = select_tools(
        available_tools=tools, page="dashboard",
        prompt="how do I configure a database connection",
        provider_hint="ollama",
    )
    names = {t.name for t in picked}
    assert "inspect_connections" in names


def test_tool_selector_cap_respected(monkeypatch):
    monkeypatch.delenv("FPULSE_DISABLE_TOOL_SELECTOR", raising=False)
    from fpulse.ai.tool_selector import select_tools
    tools = _stub_tools(ALL_TOOL_NAMES)
    picked = select_tools(
        available_tools=tools, page="editor.canvas",
        prompt="failure error connection schedule role audit",  # triggers many boosts
        provider_hint="ollama",
        max_tools=6,
    )
    assert len(picked) <= 6


def test_tool_selector_passes_through_when_under_cap(monkeypatch):
    monkeypatch.delenv("FPULSE_DISABLE_TOOL_SELECTOR", raising=False)
    from fpulse.ai.tool_selector import select_tools
    tools = _stub_tools(["workspace_overview", "list_pipelines", "recall_history"])
    picked = select_tools(
        available_tools=tools, page="editor.canvas",
        prompt="anything", provider_hint="ollama", max_tools=8,
    )
    # Available set is smaller than the cap — no narrowing needed.
    assert {t.name for t in picked} == {"workspace_overview", "list_pipelines", "recall_history"}


# ────────────────────────────────────────────────────────────────────────
# PageContext.to_extra_context_block
# ────────────────────────────────────────────────────────────────────────


def _ctx(**kw):
    from fpulse.ai.context import PageContext
    defaults = dict(
        page="editor.canvas",
        user_id="u1",
        tenant_id="default",
    )
    defaults.update(kw)
    return PageContext(**defaults)


def test_extra_context_block_empty_returns_empty_string():
    ctx = _ctx(extra_context={})
    assert ctx.to_extra_context_block() == ""


def test_extra_context_block_renders_keys_alphabetically():
    ctx = _ctx(extra_context={
        "zeta": {"a": 1},
        "alpha": {"b": 2},
    })
    block = ctx.to_extra_context_block()
    # alpha header must appear BEFORE zeta header (alphabetical ordering).
    assert block.index("### alpha") < block.index("### zeta")


def test_extra_context_block_respects_max_chars():
    big_payload = {"big": {"text": "x" * 5000}}
    ctx = _ctx(extra_context=big_payload)
    block = ctx.to_extra_context_block(max_chars=500)
    assert len(block) <= 600  # cap + tail marker tolerance
    assert "truncated" in block.lower()


def test_extra_context_block_handles_unjsonable_payloads():
    class _Weird:
        pass
    ctx = _ctx(extra_context={"weird": _Weird()})
    # Should not raise; the fallback is repr().
    block = ctx.to_extra_context_block()
    assert "weird" in block


# ────────────────────────────────────────────────────────────────────────
# PageContext.to_conversation_block
# ────────────────────────────────────────────────────────────────────────


def test_conversation_block_empty():
    ctx = _ctx()
    assert ctx.to_conversation_block() == ""


def test_conversation_block_renders_recent_turns():
    ctx = _ctx(recent_turns=(
        {"role": "user", "content": "what failed yesterday?"},
        {"role": "assistant", "content": "two pipelines: sales_etl and weekly_report"},
        {"role": "user", "content": "show me sales_etl"},
    ))
    block = ctx.to_conversation_block()
    assert "Conversation so far" in block
    assert "what failed yesterday?" in block
    assert "sales_etl" in block
    assert "**User:**" in block
    assert "**Assistant:**" in block


def test_conversation_block_windows_long_history():
    turns = tuple(
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg-{i}"}
        for i in range(40)
    )
    ctx = _ctx(recent_turns=turns)
    block = ctx.to_conversation_block(max_turns=5)
    # Should keep only the last 5
    assert "msg-39" in block
    assert "msg-38" in block
    assert "msg-35" in block
    assert "msg-30" not in block
    assert "msg-0" not in block


def test_conversation_block_truncates_long_turns():
    long_content = "x" * 5000
    ctx = _ctx(recent_turns=(
        {"role": "user", "content": long_content},
    ))
    block = ctx.to_conversation_block(max_chars_per_turn=200)
    # Should appear truncated with the ellipsis marker.
    assert "…" in block
    # The full 5000-char message should NOT survive verbatim.
    assert long_content not in block


def test_conversation_block_caps_summary():
    summary = "S" * 5000
    ctx = _ctx(conversation_summary=summary)
    block = ctx.to_conversation_block(max_summary_chars=300)
    assert len(block) < 1000
    assert "…" in block


def test_conversation_block_skips_invalid_turns():
    ctx = _ctx(recent_turns=(
        {"role": "", "content": "no role"},
        {"role": "user", "content": ""},
        {"role": "user", "content": "valid"},
    ))
    block = ctx.to_conversation_block()
    assert "valid" in block
    assert "no role" not in block


# ────────────────────────────────────────────────────────────────────────
# AgentRequest.mode
# ────────────────────────────────────────────────────────────────────────


def test_agent_request_mode_defaults_to_standard():
    from fpulse.api.agent import AgentRequest, PageContextRequest
    req = AgentRequest(
        user_intent="hi",
        page_context=PageContextRequest(page="editor.canvas"),
    )
    assert req.mode == "standard"


def test_agent_request_mode_accepts_deep():
    from fpulse.api.agent import AgentRequest, PageContextRequest
    req = AgentRequest(
        user_intent="reason about this",
        page_context=PageContextRequest(page="editor.canvas"),
        mode="deep",
    )
    assert req.mode == "deep"


def test_agent_request_conversation_accepts_turns_and_summary():
    from fpulse.api.agent import AgentRequest, ConversationContext, ConversationTurn, PageContextRequest
    convo = ConversationContext(
        recent_turns=[
            ConversationTurn(role="user", content="hi"),
            ConversationTurn(role="assistant", content="hello"),
        ],
        summary="user said hi",
    )
    req = AgentRequest(
        user_intent="hi",
        page_context=PageContextRequest(page="editor.canvas"),
        conversation=convo,
    )
    assert len(req.conversation.recent_turns) == 2
    assert req.conversation.summary == "user said hi"


def test_agent_request_extra_context_accepted_on_page_context():
    from fpulse.api.agent import AgentRequest, PageContextRequest
    req = AgentRequest(
        user_intent="explain",
        page_context=PageContextRequest(
            page="editor.canvas",
            extra_context={"workflow": {"id": "wf-1", "step_count": 3}},
        ),
    )
    assert req.page_context.extra_context["workflow"]["step_count"] == 3
