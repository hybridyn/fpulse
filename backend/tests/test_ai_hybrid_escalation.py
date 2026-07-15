"""Tests for the hybrid-lane empty/generic escalation (Risk A fix).

The hybrid lane runs ONE fast-lane tool + ONE LLM format pass. When the
single tool returns empty/generic ("no record of X"), running the LLM
format pass just dresses up that empty answer. We instead set
``escalate=True`` so the caller falls through to the full multi-tool
agent loop, which can try a different tool.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from fpulse.ai.context import PageContext
from fpulse.ai.fast_router import FastIntent
from fpulse.ai.hybrid import (
    HybridResult,
    is_empty_or_generic,
    run_hybrid,
)
from fpulse.ai.tools.base import ToolContext


# ── is_empty_or_generic ───────────────────────────────────────────────────


def test_blank_is_empty():
    assert is_empty_or_generic("") is True
    assert is_empty_or_generic("   \n  ") is True


def test_short_handler_text_is_empty():
    # < 24 chars after stripping = "I had nothing to say"
    assert is_empty_or_generic("OK") is True
    assert is_empty_or_generic("No.") is True


def test_no_record_marker_triggers():
    assert is_empty_or_generic(
        "I have no record of any pipeline named 'sync'."
    ) is True


def test_no_failures_marker_triggers():
    assert is_empty_or_generic(
        "Your workspace shows no failures in the last 24 hours."
    ) is True


def test_zero_results_marker_triggers():
    assert is_empty_or_generic(
        "Search returned 0 results across all pipelines."
    ) is True


def test_real_answer_is_not_empty():
    text = (
        "Pipeline `nightly_etl` failed at 02:14 UTC because the source "
        "Postgres connection refused with 'authentication failed'. "
        "The credential expired Mar 12. Open Connections → nightly_pg → Reset."
    )
    assert is_empty_or_generic(text) is False


def test_card_markers_stripped_before_check():
    # Handler returned a card with real data — that's NOT empty.
    text = '[CARD]{"kind":"card","type":"table","rows":[{"a":1}]}[/CARD]\n\nHere are your top 3 failing pipelines this week with their error rates and last-success timestamps.'
    assert is_empty_or_generic(text) is False


def test_card_only_with_empty_prose_is_empty():
    # Just a card marker with nothing else — too thin to format.
    text = '[CARD]{"kind":"card"}[/CARD]'
    assert is_empty_or_generic(text) is True


# ── run_hybrid escalate path ──────────────────────────────────────────────


@dataclass
class _FakeLLM:
    """Stub LLM that fails the test if called. We use this to assert that
    the LLM is NOT invoked when the handler returns empty/generic data."""

    async def call(self, **kwargs):
        raise AssertionError("LLM should NOT be called on empty handler output")


async def _empty_handler(prompt: str, ctx: ToolContext) -> str:
    return "I have no record of recent executions for this pipeline."


async def _rich_handler(prompt: str, ctx: ToolContext) -> str:
    return (
        "Pipeline `nightly_etl` ran 3 times in the last 24h: 2 succeeded "
        "(durations 2.4s, 2.1s) and 1 failed at 02:14 UTC with the error "
        "'source connection refused'. The credential for the source "
        "Postgres expired Mar 12 — open Connections to reset."
    )


@dataclass
class _OkLLM:
    """Stub LLM that returns a canned text response."""

    async def call(self, **kwargs):
        return _LLMResp(text="LLM-formatted answer.", tokens_in=10, tokens_out=20)


@dataclass
class _LLMResp:
    text: str
    tokens_in: int
    tokens_out: int


def _page_ctx() -> PageContext:
    return PageContext(
        page="dashboard",
        user_id="u1",
        tenant_id="t1",
        workspace_id="w1",
        environment="dev",
    )


def _fast_ctx() -> ToolContext:
    return ToolContext(
        tenant_id="t1", user_id="u1", workspace_id="w1",
        environment="dev", dry_run=False,
    )


def test_run_hybrid_escalates_on_empty_handler():
    intent = FastIntent(
        name="list_recent_executions",
        triggers=("recent runs",),
        handler=_empty_handler,
        serves_reasoning=False,
    )
    result = asyncio.run(run_hybrid(
        prompt="why did the sync pipeline fail?",
        intent=intent,
        fast_ctx=_fast_ctx(),
        llm_client=_FakeLLM(),  # would raise if called
        page_context=_page_ctx(),
        app_state=None,
    ))
    assert result.escalate is True
    assert "empty/generic" in result.escalate_reason
    assert result.llm_ms == 0           # LLM was not invoked
    assert result.tokens_in == 0
    assert result.tokens_out == 0
    # Handler text is preserved so the caller can still surface it as
    # a fallback if it chooses not to escalate.
    assert "no record" in result.text


def test_run_hybrid_runs_llm_on_rich_handler():
    intent = FastIntent(
        name="list_recent_executions",
        triggers=("recent runs",),
        handler=_rich_handler,
        serves_reasoning=False,
    )
    result = asyncio.run(run_hybrid(
        prompt="why did the sync pipeline fail?",
        intent=intent,
        fast_ctx=_fast_ctx(),
        llm_client=_OkLLM(),
        page_context=_page_ctx(),
        app_state=None,
    ))
    assert result.escalate is False
    assert result.escalate_reason == ""
    assert result.llm_ms > 0 or result.text == "LLM-formatted answer."
    assert result.tokens_in == 10
    assert result.tokens_out == 20
