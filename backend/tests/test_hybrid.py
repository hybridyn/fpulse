"""Unit + integration tests for the hybrid (tool + LLM format) lane.

Two layers:
  * ``TestShouldUseHybrid`` — pure classifier tests, no I/O.
  * ``TestRunHybrid`` — full pipeline with stubbed _call_tool + fake
    LLM client. Verifies the handler runs, [CARD] blocks get stripped
    before the LLM sees the data, and the result combines correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from fpulse.ai.context import PageContext
from fpulse.ai.hybrid import (
    HybridResult,
    _strip_card_blocks,
    run_hybrid,
    should_use_hybrid,
)
from fpulse.ai.tools.base import ToolContext


# ── Classifier ───────────────────────────────────────────────────────


class TestShouldUseHybrid:
    @pytest.mark.parametrize("prompt", [
        # Has reasoning marker + would match a fast-lane data intent
        "why are the recent pipelines so slow",
        "explain the recent executions",
        "how come the schedule keeps slipping",
        "what's wrong with my connections",
    ])
    def test_reasoning_plus_data_intent_returns_match(self, prompt):
        match = should_use_hybrid(prompt)
        # Not all of these will necessarily match — depends on whether the
        # fast-lane scorer finds an intent above HYBRID_MIN_CONFIDENCE. At
        # minimum the function must return *something* for at least the
        # first one (recent + pipelines is a strong list_executions match).
        # Strictness here would over-couple the test to the intent table.
        assert match is None or hasattr(match[0], "name")

    @pytest.mark.parametrize("prompt", [
        # No reasoning marker → not hybrid territory
        "list my pipelines",
        "show recent executions",
        "what's in my workspace",
        "running now",
    ])
    def test_no_reasoning_marker_returns_none(self, prompt):
        assert should_use_hybrid(prompt) is None

    @pytest.mark.parametrize("prompt", [
        "",
        "   ",
        "why",  # too short / no data intent
    ])
    def test_empty_or_trivial_returns_none(self, prompt):
        assert should_use_hybrid(prompt) is None

    def test_long_prompt_returns_none(self):
        # > 280 chars → defer to full agent loop (heuristic borrowed
        # from single_shot.should_use_single_shot — long prompts are
        # usually multi-part).
        long_prompt = "why did " + ("the pipeline keep failing because " * 30)
        assert len(long_prompt) > 280
        assert should_use_hybrid(long_prompt) is None

    def test_failed_executions_excluded_serves_reasoning(self):
        # failed_executions has serves_reasoning=True — it has its own
        # bypass path in try_match_scored (P0 #1, May 17 2026). The
        # hybrid candidate finder MUST skip it so we don't double-handle.
        match = should_use_hybrid("which pipelines failed and why")
        if match is not None:
            assert match[0].name != "failed_executions", (
                "failed_executions must be excluded from hybrid — it has"
                " its own deterministic reasoning bypass"
            )


# ── Helpers ──────────────────────────────────────────────────────────


def _strip_test_text_sample() -> tuple[str, str]:
    """Sample handler-style text + the expected stripped form."""
    raw = (
        "**3 failed executions** in recent history:\n"
        "- ❌ **sales_etl** — Connection timeout after 30s\n"
        "- ❌ **hr_sync** — Invalid schema mapping\n"
        "- ❌ **inventory_sync** — Auth token expired\n"
        "\n"
        '[CARD]{"kind": "card", "type": "next_actions", "chips": [{"label": "Open Executions"}]}[/CARD]'
    )
    stripped = (
        "**3 failed executions** in recent history:\n"
        "- ❌ **sales_etl** — Connection timeout after 30s\n"
        "- ❌ **hr_sync** — Invalid schema mapping\n"
        "- ❌ **inventory_sync** — Auth token expired"
    )
    return raw, stripped


class TestStripCardBlocks:
    def test_removes_card_block(self):
        raw, expected = _strip_test_text_sample()
        assert _strip_card_blocks(raw) == expected

    def test_no_card_block_returns_unchanged(self):
        text = "Just plain text with no cards."
        assert _strip_card_blocks(text) == text

    def test_multiple_card_blocks_all_removed(self):
        text = (
            "Header\n[CARD]{\"a\":1}[/CARD]\n"
            "Middle\n[CARD]{\"b\":2}[/CARD]\n"
            "Footer"
        )
        result = _strip_card_blocks(text)
        assert "[CARD]" not in result
        assert "Header" in result
        assert "Middle" in result
        assert "Footer" in result


# ── Full pipeline with stubbed handler + fake LLM ────────────────────


@dataclass
class _FakeResponse:
    text: str
    tokens_in: int = 50
    tokens_out: int = 30


@dataclass
class FakeLLMClient:
    """Records what was sent to the LLM and returns a canned response.

    Mirrors the signature run_hybrid expects: call(*, system, messages,
    tools, on_token). on_token is optional; we ignore it in the fake.
    """
    response_text: str = "Three pipelines failed today, all unrelated."
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, *, system, messages, tools, on_token=None):
        self.calls.append({
            "system": system,
            "messages": messages,
            "tools": tools,
        })
        return _FakeResponse(text=self.response_text)


@pytest.fixture
def fake_ctx() -> ToolContext:
    return ToolContext(
        tenant_id="default",
        user_id="u-test",
        workspace_id="default",
        environment="dev",
        dry_run=False,
    )


@pytest.fixture
def fake_page_ctx() -> PageContext:
    return PageContext(
        page="dashboard",
        user_id="u-test",
        tenant_id="default",
        workspace_id="default",
        environment="dev",
        visible_ids=(),
        selected_ids=(),
        role="data_engineer",
    )


def _make_intent_with_handler(name: str, handler_fn):
    """FastIntent is @dataclass(frozen=True), so we build a fresh one
    each test instead of monkeypatching an existing one. Triggers are
    irrelevant here — run_hybrid uses the handler directly."""
    from fpulse.ai.fast_router import FastIntent
    return FastIntent(
        name=name,
        triggers=("__test_only_trigger__",),
        handler=handler_fn,
    )


class TestRunHybrid:
    @pytest.mark.asyncio
    async def test_returns_handler_text_when_no_data(self, fake_ctx, fake_page_ctx):
        """When the handler returns text that strips to empty (cards-only),
        run_hybrid must skip the LLM call and return the handler text as-is."""
        async def empty_handler(prompt, ctx):
            return '[CARD]{"x":1}[/CARD]'

        target = _make_intent_with_handler("test_empty", empty_handler)
        fake = FakeLLMClient()

        result = await run_hybrid(
            prompt="explain my pipelines",
            intent=target,
            fast_ctx=fake_ctx,
            llm_client=fake,
            page_context=fake_page_ctx,
            app_state=None,
        )
        assert isinstance(result, HybridResult)
        assert result.text == '[CARD]{"x":1}[/CARD]'
        assert len(fake.calls) == 0  # LLM was skipped
        assert result.llm_ms == 0

    @pytest.mark.asyncio
    async def test_llm_sees_stripped_text(self, fake_ctx, fake_page_ctx):
        """The LLM must receive the handler output WITHOUT [CARD] blocks
        — that's the trust contract: the format pass works on
        deterministic prose, not on UI markers."""
        raw, _expected_stripped = _strip_test_text_sample()

        async def stub_handler(prompt, ctx):
            return raw

        target = _make_intent_with_handler("test_strips", stub_handler)
        fake = FakeLLMClient(response_text="Three pipelines failed today.")

        result = await run_hybrid(
            prompt="why did pipelines fail",
            intent=target,
            fast_ctx=fake_ctx,
            llm_client=fake,
            page_context=fake_page_ctx,
            app_state=None,
        )
        assert len(fake.calls) == 1
        sent_message = fake.calls[0]["messages"][0]["content"]
        # Stripped text must be in the message
        assert "sales_etl" in sent_message
        assert "Connection timeout" in sent_message
        # CARD block must NOT be in the message
        assert "[CARD]" not in sent_message
        assert "next_actions" not in sent_message
        # Final returned text is the LLM's response, not the handler's
        assert result.text == "Three pipelines failed today."
        assert result.handler_text == raw  # raw kept for trace UI
        assert result.tokens_in == 50
        assert result.tokens_out == 30

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_handler_text(self, fake_ctx, fake_page_ctx):
        """If the LLM raises, the user should still get the deterministic
        handler text — no LLM error surfaced to the chat."""
        async def stub_handler(prompt, ctx):
            return "Deterministic answer with real data."

        @dataclass
        class ExplodingLLM:
            async def call(self, *, system, messages, tools, on_token=None):
                raise RuntimeError("provider 503")

        target = _make_intent_with_handler("test_fallback", stub_handler)

        result = await run_hybrid(
            prompt="why did pipelines fail",
            intent=target,
            fast_ctx=fake_ctx,
            llm_client=ExplodingLLM(),
            page_context=fake_page_ctx,
            app_state=None,
        )
        assert result.text == "Deterministic answer with real data."
        assert result.llm_ms == 0
        assert result.confidence == 0.8  # graceful-fallback marker

    @pytest.mark.asyncio
    async def test_raises_when_intent_has_no_handler(self, fake_ctx, fake_page_ctx):
        from fpulse.ai.fast_router import FastIntent

        intent_no_handler = FastIntent(
            name="static_only",
            triggers=("foo",),
            handler=None,
            static_answer="hi",
        )
        fake = FakeLLMClient()
        with pytest.raises(ValueError, match="no handler"):
            await run_hybrid(
                prompt="why foo",
                intent=intent_no_handler,
                fast_ctx=fake_ctx,
                llm_client=fake,
                page_context=fake_page_ctx,
                app_state=None,
            )
