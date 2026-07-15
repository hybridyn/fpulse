"""Unit tests for the chat fast-lane router.

Pattern-matching tests are pure (no I/O, no tool calls). Renderer tests
patch the tool registry so we can assert the render output without
needing a live workspace.
"""

from __future__ import annotations

from typing import Any

import pytest

from fpulse.ai.fast_router import (
    FastIntent,
    FastLaneResult,
    run_fast_lane,
    try_match,
)
from fpulse.ai.tools.base import ToolContext


# ── Pattern matching ─────────────────────────────────────────────────


class TestMatchOverview:
    @pytest.mark.parametrize("prompt", [
        "give me a quick overview of my workspace",
        "give me an overview",
        "summarize my workspace",
        "summarise my workspace",
        "what's in this workspace",
        "whats in this workspace",
        "Show me the dashboard",
        "OVERVIEW",
    ])
    def test_overview_phrasings_match(self, prompt):
        intent = try_match(prompt)
        assert intent is not None
        assert intent.name == "overview"


class TestMatchListPipelines:
    @pytest.mark.parametrize("prompt", [
        "list my pipelines",
        "What pipelines are available?",
        "show me pipelines",
        "show pipelines",
        "what pipelines do i have",
        "my pipelines",
        "pipeline list please",
    ])
    def test_pipeline_phrasings_match(self, prompt):
        intent = try_match(prompt)
        assert intent is not None
        assert intent.name == "list_pipelines"

    def test_failed_excludes_list_pipelines(self):
        # "list pipelines that failed" must route to failed_executions.
        intent = try_match("list pipelines that failed")
        assert intent is not None
        assert intent.name == "failed_executions"

    def test_templates_excludes_list_pipelines(self):
        # Caught in production May 4 2026: "Can we check the existing
        # sample templates for the pipelines?" was matching list_pipelines
        # with name_filter='the' and returning "no pipelines match 'the'".
        # The word 'templates' must drop the prompt to the templates intent.
        for prompt in (
            "Can we check the existing sample templates for the pipelines?",
            "show me the pipeline templates",
            "what starter templates are there",
            "list the example pipelines",
        ):
            intent = try_match(prompt)
            assert intent is not None, f"no match for {prompt!r}"
            assert intent.name == "list_templates", (
                f"{prompt!r} matched {intent.name!r}, expected list_templates"
            )


class TestFilterStopWords:
    """Stop-words must never become a name_filter on list_pipelines.

    Caught in prod (May 4 2026): "list pipelines for the customer" was
    extracting filter='the' and reporting "no pipelines match 'the'".
    """

    def test_stop_word_after_preposition_yields_empty_filter(self):
        from fpulse.ai.fast_router import _extract_filter_hint

        for prompt, preps in [
            ("list pipelines for the customer", ["about", "for", "named", "matching"]),
            ("show pipelines about my project", ["about", "for", "named", "matching"]),
            ("pipelines about this workspace", ["about", "for", "named", "matching"]),
            ("show pipelines for some failures", ["about", "for", "named", "matching"]),
        ]:
            # First word after the preposition is a stop word; the helper
            # tries the SECOND word, which is also generic — so it returns
            # the second word OR empty depending on the case.
            result = _extract_filter_hint(prompt, preps)
            # The key contract: stop-words alone never come back.
            assert result not in {"the", "a", "an", "my", "this", "some",
                                  "pipeline", "pipelines"}, \
                f"got disallowed filter {result!r} from {prompt!r}"

    def test_real_filter_after_stop_word_still_extracts(self):
        from fpulse.ai.fast_router import _extract_filter_hint
        # "pipelines about the customer" → 'the' is stop-word, fall through
        # to 'customer' (the second word, valid name).
        result = _extract_filter_hint(
            "pipelines about the customer", ["about", "for", "named", "matching"]
        )
        assert result == "customer"

    def test_short_word_filter_rejected(self):
        """Single- or two-character names are almost never real — reject."""
        from fpulse.ai.fast_router import _extract_filter_hint
        result = _extract_filter_hint(
            "pipelines for x", ["about", "for", "named", "matching"]
        )
        assert result == ""


class TestMatchFailures:
    @pytest.mark.parametrize("prompt", [
        "what failed today",
        "show me failures",
        "list failures",
        "what broke",
        "any errors?",
        "recent failures",
        "failed runs",
    ])
    def test_failure_phrasings_match(self, prompt):
        intent = try_match(prompt)
        assert intent is not None
        assert intent.name == "failed_executions"


class TestMatchRunning:
    @pytest.mark.parametrize("prompt", [
        "what's running now",
        "running now",
        "currently running",
        "in flight",
        "active executions",
    ])
    def test_running_phrasings_match(self, prompt):
        intent = try_match(prompt)
        assert intent is not None
        assert intent.name == "running_now"


class TestMatchProjectsSchedulesAlerts:
    def test_list_projects(self):
        assert try_match("list my projects").name == "list_projects"
        assert try_match("show projects").name == "list_projects"

    def test_list_schedules(self):
        assert try_match("what schedules do i have").name == "list_schedules"
        assert try_match("show me my scheduled jobs").name == "list_schedules"

    def test_list_alerts(self):
        assert try_match("list alerts").name == "list_alerts"
        assert try_match("show me alert rules").name == "list_alerts"

    def test_list_connections(self):
        assert try_match("show my connections").name == "list_connections"
        assert try_match("what connections do i have").name == "list_connections"


class TestMatchCatalog:
    @pytest.mark.parametrize("prompt", [
        "what node types are supported",
        "list catalog",
        "what connectors are available",
        "supported nodes",
    ])
    def test_catalog_phrasings(self, prompt):
        intent = try_match(prompt)
        assert intent is not None
        # 2026-06-15: the stale static help.node_catalog was deleted; the
        # dynamic `catalog` intent (live registry) is the single source now.
        assert intent.name == "catalog"

    @pytest.mark.parametrize("prompt", [
        # The exact user-reported hijack: a catalog phrasing + a concrete goal.
        "What are the nodes available? I need lookup from a sql server table",
        "I need to lookup from a sql server table",
        "what nodes do i use to write to postgres",
        "load from s3 — which node types",
    ])
    def test_catalog_does_not_hijack_a_concrete_goal(self, prompt):
        """A prompt that states a concrete data goal must fall through to the
        agent loop (grounded answer + draft), not get a generic node dump —
        neither via a fast intent NOR via the atlas node/connector topic."""
        from fpulse.ai.fast_router import _try_atlas_match
        assert try_match(prompt) is None
        assert _try_atlas_match(prompt) is None


class TestMatchHelpAndGreet:
    def test_help(self):
        assert try_match("help").name == "help"
        assert try_match("what can you do").name == "help"
        assert try_match("what can i ask").name == "help"

    def test_greet(self):
        assert try_match("hi").name == "greet"
        assert try_match("hello").name == "greet"
        assert try_match("hey").name == "greet"
        assert try_match("hi there").name == "greet"
        # Greeting embedded in a longer prompt should NOT short-circuit —
        # the longer prompt is the question, not the greeting.
        assert try_match("hi can you list my pipelines").name == "list_pipelines"


class TestMatchProductInfo:
    @pytest.mark.parametrize("prompt", [
        "what is f-pulse",
        "what is fpulse",
        "what is this product",
        "what is the product we use",
        "tell me about f-pulse",
        "what does f-pulse do",
        "what does this do",
        "explain f-pulse",
    ])
    def test_what_is_fpulse(self, prompt):
        intent = try_match(prompt)
        assert intent is not None
        assert intent.name == "what_is_fpulse"

    def test_what_is_fpulse_does_not_hijack_pipeline_question(self):
        # "what does this pipeline do" is in the excludes — must NOT route
        # to what_is_fpulse. Should fall through to the LLM (it's a
        # reasoning question about a specific pipeline).
        intent = try_match("what does this pipeline do")
        assert intent is None or intent.name != "what_is_fpulse"

    @pytest.mark.parametrize("prompt", [
        "what tier am i on",
        "which tier",
        "am i on plus",
        "what edition",
        "is this plus",
        "do i have plus",
    ])
    def test_what_tier(self, prompt):
        intent = try_match(prompt)
        assert intent is not None
        assert intent.name == "what_tier"

    @pytest.mark.parametrize("prompt", [
        "what's my role",
        "what is my role",
        "who am i",
        "my permissions",
        "what permissions do i have",
        "what env am i on",
        "what workspace am i in",
    ])
    def test_my_role(self, prompt):
        intent = try_match(prompt)
        assert intent is not None
        assert intent.name == "my_role"


# ── Reasoning / disambiguation gate ──────────────────────────────────


class TestReasoningGate:
    @pytest.mark.parametrize("prompt", [
        "why did pipeline X fail",
        "explain how pipelines work",
        "compare pipeline A and pipeline B",
        "should i use snowflake or bigquery",
        "diagnose this failure",
        "what's wrong with my pipeline",
        "how come the schedule isn't running",
        "help me understand this",
        "walk me through the failure",
        "recommend a pipeline structure",
    ])
    def test_reasoning_words_block_fast_lane(self, prompt):
        # All of these have keywords that would otherwise match an intent
        # (pipeline, fail, schedule, etc.) but the reasoning words mean
        # the user wants the LLM. Must NOT route to fast lane.
        intent = try_match(prompt)
        assert intent is None, f"Should fall through to LLM: {prompt!r}"

    @pytest.mark.parametrize("prompt", [
        "which pipelines failed in the last 24 hours and why?",
        "what failed today and why",
        "which pipelines failed and why",
        "failed in the last hour — why?",
    ])
    def test_reasoning_bypass_for_failed_executions(self, prompt):
        # Added May 17 2026 — reasoning-gate bypass.
        # failed_executions.serves_reasoning=True because its handler
        # already emits per-row error_message + a "why did the latest one
        # fail" chip. So prompts that combine "why" with a failure-list
        # query should hit fast-lane, NOT the 6-step LLM loop that the
        # user reported taking 203s on local Ollama.
        intent = try_match(prompt)
        assert intent is not None, f"Should hit fast-lane: {prompt!r}"
        assert intent.name == "failed_executions", (
            f"Expected failed_executions for {prompt!r}, got {intent.name}"
        )
        assert intent.serves_reasoning, (
            "failed_executions must keep serves_reasoning=True or the bypass breaks"
        )


class TestBuildIntentNeverHijacked:
    """Regression — 2026-05-17 user report.

    A user on the Editor canvas clicked the 'CSV → filter → Parquet'
    template chip and the resulting prompt
    ``"Build a pipeline: Build a pipeline that reads sales.csv, filters
    rows where status=\"active\", and writes the result to a Parquet
    file."`` returned a list of 20 recent executions instead of drafting
    a pipeline.

    Root cause: ``list_executions`` had trigger ``"show pipeline status"``
    which token-overlapped {pipeline, status} with the prompt's
    "pipeline" + "status=active" content tokens, scoring 0.75 on Tier 5
    — above MIN_CONFIDENCE (0.6) — and firing the deterministic
    handler before the agent loop could choose
    ``draft_pipeline_from_intent``.

    Fix: ``list_executions`` (and ``list_pipelines``) excludes now
    include "build", "create", "draft", "make", etc. so build-intent
    prompts fall through to the agent loop.
    """

    _BUILD_PROMPTS = [
        # The literal user-reported case (double prefix from chip + template)
        "Build a pipeline: Build a pipeline that reads sales.csv, "
        "filters rows where status=\"active\", and writes the result to "
        "a Parquet file.",
        # Variants the chip / user might type
        "Build a pipeline that reads sales.csv and writes to Parquet",
        "Create a pipeline that filters rows where status=active",
        "Draft a new pipeline for daily Postgres sync",
        "Make a pipeline that aggregates sales by region",
        "Design a pipeline to copy data from S3 to Snowflake",
        "Construct a pipeline that joins customers and orders",
        "Scaffold a pipeline for CSV ingestion",
        "Generate a pipeline that reads JSON and writes CSV",
    ]

    @pytest.mark.parametrize("prompt", _BUILD_PROMPTS)
    def test_build_intents_fall_through_to_agent(self, prompt):
        """Build / create / draft prompts MUST NOT hit the operational
        fast lane. They need the agent loop's draft_pipeline_from_intent
        tool (SAFE_WRITE tier) which the fast lane can't substitute for."""
        intent = try_match(prompt)
        assert intent is None, (
            f"Build-intent prompt was hijacked by {intent.name!r} fast-lane "
            f"intent — must fall through to agent loop. Prompt: {prompt!r}"
        )

    @pytest.mark.parametrize("prompt", _BUILD_PROMPTS)
    @pytest.mark.asyncio
    async def test_build_intents_also_skip_atlas(self, prompt):
        """Atlas has ``howto.create_pipeline`` with alias ``"build a
        pipeline"`` — without the imperative guard in _try_atlas_match,
        the atlas would intercept build-intent prompts and return the
        how-to guide ("Three ways: from a template / in the Editor /
        from natural language") instead of letting the agent loop call
        ``draft_pipeline_from_intent`` to ACTUALLY draft the pipeline.

        The guard: imperative verb at start + no knowledge-question
        prefix → skip atlas. ``run_fast_lane`` is the end-to-end check
        because it runs both try_match_scored AND the atlas fallback."""
        from fpulse.ai.tools.base import ToolContext
        ctx = ToolContext(
            tenant_id="default", user_id="u-test", workspace_id="default",
            environment="dev", dry_run=False,
        )
        result = await run_fast_lane(prompt, ctx)
        assert result is None, (
            f"Build-intent prompt fell into the fast lane after the "
            f"operational excludes — likely the atlas matched a how-to "
            f"topic. Returned intent: {result.intent_name!r}. "
            f"Prompt: {prompt!r}"
        )


class TestEmptyAndJunk:
    def test_empty_returns_none(self):
        assert try_match("") is None
        assert try_match("   ") is None

    def test_unknown_intent_returns_none(self):
        # No keyword overlap with any registered intent.
        assert try_match("the weather is nice today") is None
        assert try_match("compute the eigenvalues of this matrix") is None


# ── Renderer tests with a stubbed tool registry ──────────────────────


@pytest.fixture
def fake_ctx():
    return ToolContext(
        tenant_id="default",
        user_id="u-test",
        workspace_id="default",
        environment="dev",
        dry_run=False,
    )


@pytest.fixture
def stub_tools(monkeypatch):
    """Replace `_call_tool` with a stub that returns canned outputs.
    Lets renderer tests run without a live store."""
    canned: dict[str, Any] = {}

    async def fake_call_tool(name, args, ctx):
        return canned.get(name, {"_error": f"no stub for {name}"})

    import fpulse.ai.fast_router as fr
    monkeypatch.setattr(fr, "_call_tool", fake_call_tool)
    return canned


class TestRunFastLane:
    @pytest.mark.asyncio
    async def test_no_match_returns_none(self, fake_ctx):
        result = await run_fast_lane("compute eigenvalues", fake_ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_overview_renders_kpi_card(self, fake_ctx, stub_tools):
        stub_tools["get_workspace_overview"] = {
            "counts": {
                "pipelines": 5, "projects": 2, "schedules": 3,
                "alerts": 1, "connections": 4,
            },
            "workspace_id": "default",
            "environment": "dev",
        }
        result = await run_fast_lane("give me an overview", fake_ctx)
        assert result is not None
        assert result.intent_name == "overview"
        # KPI card embedded.
        assert "[CARD]" in result.text
        # Counts surfaced in the prose.
        assert "5 pipelines" in result.text
        assert "3 schedules" in result.text

    @pytest.mark.asyncio
    async def test_list_pipelines_renders_bullets(self, fake_ctx, stub_tools):
        stub_tools["list_pipelines"] = {
            "pipelines": [
                {"id": "p1", "name": "sales_etl", "status": "published", "step_count": 4},
                {"id": "p2", "name": "weekly_report", "status": "draft", "step_count": 2},
            ],
            "total": 2,
        }
        result = await run_fast_lane("list my pipelines", fake_ctx)
        assert result is not None
        assert result.intent_name == "list_pipelines"
        assert "sales_etl" in result.text
        assert "weekly_report" in result.text
        assert "2 pipelines" in result.text

    @pytest.mark.asyncio
    async def test_list_pipelines_empty(self, fake_ctx, stub_tools):
        stub_tools["list_pipelines"] = {"pipelines": [], "total": 0}
        result = await run_fast_lane("show me pipelines", fake_ctx)
        assert result is not None
        assert "no pipelines" in result.text.lower()

    @pytest.mark.asyncio
    async def test_failed_executions_clean_state(self, fake_ctx, stub_tools):
        stub_tools["list_executions"] = {"executions": []}
        result = await run_fast_lane("any failures today?", fake_ctx)
        assert result is not None
        assert result.intent_name == "failed_executions"
        assert "no failures" in result.text.lower() or "running clean" in result.text.lower()

    @pytest.mark.asyncio
    async def test_running_now(self, fake_ctx, stub_tools):
        stub_tools["get_running_executions"] = {"running": []}
        result = await run_fast_lane("what's running now", fake_ctx)
        assert result is not None
        assert result.intent_name == "running_now"

    @pytest.mark.asyncio
    async def test_help_static_text(self, fake_ctx):
        # Help renderer doesn't call any tool — works without stubs.
        # Body was rewritten May 2026 ("fastest at" instead of "instant
        # answers"); assert on the durable shape — non-empty static text
        # + a "next_actions" card with chips the user can click.
        result = await run_fast_lane("what can you do", fake_ctx)
        assert result is not None
        assert result.intent_name == "help"
        assert "fastest" in result.text.lower()
        assert "next_actions" in result.text

    @pytest.mark.asyncio
    async def test_greet_static_text(self, fake_ctx):
        result = await run_fast_lane("hi", fake_ctx)
        assert result is not None
        assert result.intent_name == "greet"

    @pytest.mark.asyncio
    async def test_tool_error_renders_friendly_message(self, fake_ctx, stub_tools):
        # _call_tool returns {"_error": ...} on failure — renderer must
        # handle this gracefully, not crash the request.
        stub_tools["list_pipelines"] = {"_error": "store unavailable"}
        result = await run_fast_lane("list pipelines", fake_ctx)
        assert result is not None
        assert "couldn't" in result.text.lower() or "could not" in result.text.lower()

    @pytest.mark.asyncio
    async def test_elapsed_ms_recorded(self, fake_ctx, stub_tools):
        stub_tools["list_pipelines"] = {"pipelines": [], "total": 0}
        result = await run_fast_lane("list pipelines", fake_ctx)
        assert result is not None
        assert result.elapsed_ms >= 0
        # Sub-second by construction.
        assert result.elapsed_ms < 5_000


# ── Sanity: count and uniqueness of intents ─────────────────────────


class TestRegistry:
    def test_intents_have_unique_names(self):
        from fpulse.ai.fast_router import _intents_for_tests
        names = [i.name for i in _intents_for_tests()]
        assert len(names) == len(set(names)), f"duplicate intent names: {names}"

    def test_intent_count_within_target(self):
        from fpulse.ai.fast_router import _intents_for_tests
        # Original target was 10-18; the registry grew during the Phase 2A-D
        # build-out (atlas + help.* namespace + lookup tools). Current
        # ceiling is 80 — well below the point where match latency starts
        # to matter (registry walk is O(n) but n*triggers stays sub-ms).
        # Floor stays at 8 so a regression that empties the registry trips
        # this test.
        n = len(_intents_for_tests())
        assert 8 <= n <= 80, f"intent count {n} outside design target"
