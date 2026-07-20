"""
Router test harness — May 5 2026.

A deterministic, no-LLM, pure-Python test of every important routing
decision. Each ``CASE`` declares a prompt, the expected routing path,
and (for fast-lane / slot-fill cases) the expected intent name.

GET /api/admin/router-tests
  → {"summary": {...}, "results": [{prompt, expected, actual, pass}, ...]}

This replaces "I tested in the dock" with a CI-style truth table that
runs in milliseconds. No tokens, no manual QA.

Phase 1 scope (May 2026): the 30-ish cases below are the
non-negotiable contract. Any change that turns a green case red
must be reverted or fixed before merge.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from fpulse.auth.deps import require_admin

router = APIRouter(dependencies=[Depends(require_admin)])


# Path tags we assert against. Keep this set tight.
P_FAST = "fast-lane"
P_CLARIFY = "clarify"
P_SLOT = "slot-fill"
P_REF = "ref-sub"
P_HYBRID = "hybrid"  # tool fetch + LLM format pass (May 17 2026)
P_SINGLE = "single-shot"
P_AGENT = "agent"
P_AUTO_PIN = "auto-pin"


# Visible-items templates. Used by the test harness to simulate the
# ``ctx.visible_items`` snapshot the frontend would send.
_PIPELINES_3 = [
    {"id": "p1", "name": "Daily ingest", "kind": "pipeline", "status": "failed",  "meta": {"steps": 5, "last_run": "2h ago"}},
    {"id": "p2", "name": "Stripe sync",  "kind": "pipeline", "status": "success", "meta": {"steps": 3}},
    {"id": "p3", "name": "Nightly ETL",  "kind": "pipeline", "status": "failed",  "meta": {"steps": 7}},
]
_PIPELINE_1 = [
    {"id": "p1", "name": "Aggregation Report", "kind": "pipeline", "status": "draft", "meta": {"steps": 4}},
]
_PIPELINES_0: list[dict[str, Any]] = []
_CONNECTIONS_2 = [
    {"id": "c1", "name": "prod-snowflake", "kind": "connection", "meta": {"type": "snowflake"}},
    {"id": "c2", "name": "staging-pg",     "kind": "connection", "meta": {"type": "postgresql"}},
]
_EXECUTIONS_3 = [
    {"id": "e1", "name": "Daily ingest", "kind": "execution", "status": "failed",  "meta": {"workflow_id": "p1", "duration_ms": 1200}},
    {"id": "e2", "name": "Stripe sync",  "kind": "execution", "status": "success", "meta": {"duration_ms": 900}},
    {"id": "e3", "name": "Daily ingest", "kind": "execution", "status": "running", "meta": {"workflow_id": "p1"}},
]


# ─────────────────────────────────────────────────────────────────────
# Test cases — Phase 1 contract
# ─────────────────────────────────────────────────────────────────────


CASES: list[dict[str, Any]] = [
    # ─── Greetings ───────────────────────────────────────────────────
    {"prompt": "hi", "expected_path": P_FAST, "expected_intent": "greet"},
    {"prompt": "hello there", "expected_path": P_FAST, "expected_intent": "greet"},

    # ─── Help / FAQ — the user's actual broken prompt + siblings ─────
    {"prompt": "help", "expected_path": P_FAST, "expected_intent": "help"},
    {"prompt": "what can you do?", "expected_path": P_FAST, "expected_intent": "help"},
    # User-reported May 5: this fell to agent loop. MUST pass.
    {"prompt": "What can you help me with on this page?", "expected_path": P_FAST, "expected_intent": "help"},
    {"prompt": "what can you help me with", "expected_path": P_FAST, "expected_intent": "help"},
    {"prompt": "how can you help me", "expected_path": P_FAST, "expected_intent": "help"},
    {"prompt": "what can you help with", "expected_path": P_FAST, "expected_intent": "help"},

    # ─── Product info ────────────────────────────────────────────────
    {"prompt": "what is f-pulse?", "expected_path": P_FAST, "expected_intent": "what_is_fpulse"},
    {"prompt": "tell me about fpulse", "expected_path": P_FAST, "expected_intent": "what_is_fpulse"},

    # ─── List intents ────────────────────────────────────────────────
    {"prompt": "list pipelines", "expected_path": P_FAST, "expected_intent": "list_pipelines", "items": _PIPELINES_3},
    {"prompt": "show me my pipelines", "expected_path": P_FAST, "expected_intent": "list_pipelines", "items": _PIPELINES_3},
    {"prompt": "give me a quick overview of my workspace", "expected_path": P_FAST, "expected_intent": "overview"},
    {"prompt": "list connections", "expected_path": P_FAST, "expected_intent": "list_connections", "items": _CONNECTIONS_2},

    # ─── Failures (token overlap) ────────────────────────────────────
    {"prompt": "what failed today?", "expected_path": P_FAST, "expected_intent": "failed_executions"},
    {"prompt": "Show me recent pipeline failures.", "expected_path": P_FAST, "expected_intent": "failed_executions"},
    {"prompt": "any errors today", "expected_path": P_FAST, "expected_intent": "failed_executions"},
    {"prompt": "which pipelines have failed", "expected_path": P_FAST, "expected_intent": "failed_executions"},

    # ─── Running ─────────────────────────────────────────────────────
    {"prompt": "what's running now", "expected_path": P_FAST, "expected_intent": "running_now"},
    {"prompt": "currently running pipelines", "expected_path": P_FAST, "expected_intent": "running_now"},

    # ─── Direct actions on visible entities ──────────────────────────
    {"prompt": "run this pipeline", "expected_path": P_FAST, "expected_intent": "direct.run_pipeline", "items": _PIPELINES_3},
    {"prompt": "test this connection", "expected_path": P_FAST, "expected_intent": "direct.test_connection", "items": _CONNECTIONS_2},
    # Trigger ordering bug — "cancel this run" matched run_pipeline (May 5 audit).
    {"prompt": "cancel this run", "expected_path": P_FAST, "expected_intent": "direct.cancel_execution", "items": _EXECUTIONS_3},
    {"prompt": "stop this run", "expected_path": P_FAST, "expected_intent": "direct.cancel_execution", "items": _EXECUTIONS_3},

    # ─── Verb + quoted name (post-substitution form) ─────────────────
    {"prompt": 'run "Daily ingest"', "expected_path": P_FAST, "expected_intent": "direct.run_pipeline", "items": _PIPELINES_3},
    {"prompt": 'test "prod-snowflake"', "expected_path": P_FAST, "expected_intent": "direct.test_connection", "items": _CONNECTIONS_2},
    {"prompt": 'cancel "Daily ingest"', "expected_path": P_FAST, "expected_intent": "direct.cancel_execution", "items": _EXECUTIONS_3},

    # ─── Clarify (multiple candidates) ───────────────────────────────
    {"prompt": "why did my last pipeline fail?", "expected_path": P_CLARIFY, "items": _PIPELINES_3},

    # ─── Auto-pin (exactly 1 candidate) — was failing in user's workspace
    {"prompt": "why did my last pipeline fail?", "expected_path": P_AUTO_PIN, "items": _PIPELINE_1},
    {"prompt": "explain this pipeline", "expected_path": P_AUTO_PIN, "items": _PIPELINE_1},

    # ─── Multi-step → must NOT fast-lane ─────────────────────────────
    {"prompt": "first list failed pipelines then retry the most recent one", "expected_path": P_AGENT},
    {"prompt": "list failed pipelines and then run the first one", "expected_path": P_AGENT},

    # ─── New FAQ intents (Category B from audit) ─────────────────────
    {"prompt": "how to add my pipiline", "expected_path": P_FAST},  # typo of "pipeline"
    {"prompt": "walk thro the connection flow", "expected_path": P_FAST},  # typo of "through"
    {"prompt": "How can we store credentials?", "expected_path": P_FAST},
    {"prompt": "What is new?", "expected_path": P_FAST},

    # ─── Build-me-a-pipeline → agent (correct) ───────────────────────
    {"prompt": "build me a pipeline that ingests CSV, joins on id, writes to postgres", "expected_path": P_AGENT},

    # ─── Upgrade / conversion (user-reported confusion) ──────────────
    # Was mis-firing on `what_tier` and returning a tier-description
    # instead of conversion guidance. Locked here so it can't regress.
    {"prompt": "How can we convert from free to plus?", "expected_path": P_FAST, "expected_intent": "upgrade_to_plus"},
    {"prompt": "how to upgrade to plus", "expected_path": P_FAST, "expected_intent": "upgrade_to_plus"},
    {"prompt": "switch to plus", "expected_path": P_FAST, "expected_intent": "upgrade_to_plus"},
    {"prompt": "how do i get plus", "expected_path": P_FAST, "expected_intent": "upgrade_to_plus"},

    # ─── About F-Pulse + maker ──────────────
    {"prompt": "what is hybridyn", "expected_path": P_FAST, "expected_intent": "about_hybridyn"},
    {"prompt": "tell me about hybridyn", "expected_path": P_FAST, "expected_intent": "about_hybridyn"},
    {"prompt": "who created this", "expected_path": P_FAST, "expected_intent": "who_created"},
    {"prompt": "who built fpulse", "expected_path": P_FAST, "expected_intent": "who_created"},
    {"prompt": "who is the founder", "expected_path": P_FAST, "expected_intent": "who_created"},

    # ─── Compute / resources (May 6 2026 user-reported) ──────────────
    {"prompt": "What is the compute size available?", "expected_path": P_FAST, "expected_intent": "help.compute_usage"},
    {"prompt": "What is the overall compute usage?", "expected_path": P_FAST, "expected_intent": "help.compute_usage"},
    {"prompt": "how much memory does it have", "expected_path": P_FAST, "expected_intent": "help.compute_usage"},
    {"prompt": "available resources", "expected_path": P_FAST, "expected_intent": "help.compute_usage"},
    {"prompt": "cpu load", "expected_path": P_FAST, "expected_intent": "help.compute_usage"},
    {"prompt": "memory available", "expected_path": P_FAST, "expected_intent": "help.compute_usage"},
    {"prompt": "what hardware are we running on", "expected_path": P_FAST, "expected_intent": "help.compute_usage"},
    {"prompt": "disk space", "expected_path": P_FAST, "expected_intent": "help.compute_usage"},
    {"prompt": "compute capacity", "expected_path": P_FAST, "expected_intent": "help.compute_usage"},
    {"prompt": "worker pool status", "expected_path": P_FAST, "expected_intent": "help.compute_usage"},

    # ─── Operational FAQ paraphrase coverage (May 6 2026) ────────────
    # 5–8 paraphrases per intent so the harness catches future regressions
    # without you having to type them in the dock first.

    # help.failure_handling
    {"prompt": "how are failures handled", "expected_path": P_FAST, "expected_intent": "help.failure_handling"},
    {"prompt": "how do retries work", "expected_path": P_FAST, "expected_intent": "help.failure_handling"},
    {"prompt": "what happens on failure", "expected_path": P_FAST, "expected_intent": "help.failure_handling"},
    {"prompt": "retry policy", "expected_path": P_FAST, "expected_intent": "help.failure_handling"},
    {"prompt": "what is dlq", "expected_path": P_FAST, "expected_intent": "help.failure_handling"},
    {"prompt": "dead letter queue", "expected_path": P_FAST, "expected_intent": "help.failure_handling"},

    # help.lineage
    {"prompt": "how does lineage work", "expected_path": P_FAST, "expected_intent": "help.lineage"},
    {"prompt": "what is lineage", "expected_path": P_FAST, "expected_intent": "help.lineage"},
    {"prompt": "data lineage", "expected_path": P_FAST, "expected_intent": "help.lineage"},
    {"prompt": "column lineage", "expected_path": P_FAST, "expected_intent": "help.lineage"},
    {"prompt": "data provenance", "expected_path": P_FAST, "expected_intent": "help.lineage"},

    # help.connection_health
    {"prompt": "how is connection health detected", "expected_path": P_FAST, "expected_intent": "help.connection_health"},
    {"prompt": "how does connection test work", "expected_path": P_FAST, "expected_intent": "help.connection_health"},
    {"prompt": "what does connection test do", "expected_path": P_FAST, "expected_intent": "help.connection_health"},
    {"prompt": "connection refused error", "expected_path": P_FAST, "expected_intent": "help.connection_health"},

    # help.credential_expiry
    {"prompt": "credential expiry", "expected_path": P_FAST, "expected_intent": "help.credential_expiry"},
    {"prompt": "password rotation", "expected_path": P_FAST, "expected_intent": "help.credential_expiry"},
    {"prompt": "how to rotate credentials", "expected_path": P_FAST, "expected_intent": "help.credential_expiry"},
    {"prompt": "when do credentials expire", "expected_path": P_FAST, "expected_intent": "help.credential_expiry"},

    # help.node_catalog
    {"prompt": "what nodes are available", "expected_path": P_FAST, "expected_intent": "help.node_catalog"},
    {"prompt": "list of nodes", "expected_path": P_FAST, "expected_intent": "help.node_catalog"},
    {"prompt": "what does each node do", "expected_path": P_FAST, "expected_intent": "help.node_catalog"},
    {"prompt": "supported nodes", "expected_path": P_FAST, "expected_intent": "help.node_catalog"},

    # help.scheduling_internals
    {"prompt": "how does scheduling work", "expected_path": P_FAST, "expected_intent": "help.scheduling_internals"},
    {"prompt": "scheduler internals", "expected_path": P_FAST, "expected_intent": "help.scheduling_internals"},
    {"prompt": "how does the scheduler work", "expected_path": P_FAST, "expected_intent": "help.scheduling_internals"},
    {"prompt": "how do scheduled pipelines run", "expected_path": P_FAST, "expected_intent": "help.scheduling_internals"},

    # help.scheduling_howto (how-to is a separate intent from internals)
    {"prompt": "how to schedule a pipeline", "expected_path": P_FAST, "expected_intent": "help.scheduling_howto"},
    {"prompt": "schedule a pipeline", "expected_path": P_FAST, "expected_intent": "help.scheduling_howto"},
    {"prompt": "automate a pipeline", "expected_path": P_FAST, "expected_intent": "help.scheduling_howto"},
    {"prompt": "cron a pipeline", "expected_path": P_FAST, "expected_intent": "help.scheduling_howto"},

    # help.projects
    {"prompt": "what are projects", "expected_path": P_FAST, "expected_intent": "help.projects"},
    {"prompt": "how do projects work", "expected_path": P_FAST, "expected_intent": "help.projects"},
    {"prompt": "project organization", "expected_path": P_FAST, "expected_intent": "help.projects"},
    {"prompt": "explain projects", "expected_path": P_FAST, "expected_intent": "help.projects"},

    # help.versions
    {"prompt": "pipeline versions", "expected_path": P_FAST, "expected_intent": "help.versions"},
    {"prompt": "version history", "expected_path": P_FAST, "expected_intent": "help.versions"},
    {"prompt": "how to rollback", "expected_path": P_FAST, "expected_intent": "help.versions"},
    {"prompt": "revert pipeline", "expected_path": P_FAST, "expected_intent": "help.versions"},

    # help.dryrun
    {"prompt": "what is dry run", "expected_path": P_FAST, "expected_intent": "help.dryrun"},
    {"prompt": "what is sample mode", "expected_path": P_FAST, "expected_intent": "help.dryrun"},
    {"prompt": "safety modes", "expected_path": P_FAST, "expected_intent": "help.dryrun"},
    {"prompt": "validate only", "expected_path": P_FAST, "expected_intent": "help.dryrun"},

    # help.permissions
    {"prompt": "how does rbac work", "expected_path": P_FAST, "expected_intent": "help.permissions"},
    {"prompt": "permission model", "expected_path": P_FAST, "expected_intent": "help.permissions"},
    {"prompt": "what roles exist", "expected_path": P_FAST, "expected_intent": "help.permissions"},

    # help.deploy_approval
    {"prompt": "how does deploy work", "expected_path": P_FAST, "expected_intent": "help.deploy_approval"},
    {"prompt": "promote to prod", "expected_path": P_FAST, "expected_intent": "help.deploy_approval"},
    {"prompt": "approval workflow", "expected_path": P_FAST, "expected_intent": "help.deploy_approval"},
    {"prompt": "two-gate approval", "expected_path": P_FAST, "expected_intent": "help.deploy_approval"},

    # help.connections_howto
    {"prompt": "how to add a connection", "expected_path": P_FAST, "expected_intent": "help.connections_howto"},
    {"prompt": "create a new connection", "expected_path": P_FAST, "expected_intent": "help.connections_howto"},
    {"prompt": "set up a connection", "expected_path": P_FAST, "expected_intent": "help.connections_howto"},

    # help.credentials_howto
    {"prompt": "how to store credentials", "expected_path": P_FAST, "expected_intent": "help.credentials_howto"},
    {"prompt": "where do credentials go", "expected_path": P_FAST, "expected_intent": "help.credentials_howto"},

    # help.first_pipeline
    {"prompt": "how to build a pipeline", "expected_path": P_FAST, "expected_intent": "help.first_pipeline"},
    {"prompt": "build my first pipeline", "expected_path": P_FAST, "expected_intent": "help.first_pipeline"},
    {"prompt": "create my first pipeline", "expected_path": P_FAST, "expected_intent": "help.first_pipeline"},

    # help.shortcuts
    {"prompt": "keyboard shortcuts", "expected_path": P_FAST, "expected_intent": "help.shortcuts"},
    {"prompt": "hotkeys", "expected_path": P_FAST, "expected_intent": "help.shortcuts"},

    # help.oss_vs_plus
    {"prompt": "what's the difference between oss and plus", "expected_path": P_FAST, "expected_intent": "help.oss_vs_plus"},
    {"prompt": "features in plus", "expected_path": P_FAST, "expected_intent": "help.oss_vs_plus"},
    {"prompt": "what does plus add", "expected_path": P_FAST, "expected_intent": "help.oss_vs_plus"},

    # help.whats_new
    {"prompt": "what's new", "expected_path": P_FAST, "expected_intent": "help.whats_new"},
    {"prompt": "release notes", "expected_path": P_FAST, "expected_intent": "help.whats_new"},
    {"prompt": "recent changes", "expected_path": P_FAST, "expected_intent": "help.whats_new"},

    # help.walkthrough
    {"prompt": "walk me through the app", "expected_path": P_FAST, "expected_intent": "help.walkthrough"},
    {"prompt": "give me a tour", "expected_path": P_FAST, "expected_intent": "help.walkthrough"},
    {"prompt": "show me around", "expected_path": P_FAST, "expected_intent": "help.walkthrough"},

    # what_is_fpulse paraphrases
    {"prompt": "what does this do", "expected_path": P_FAST, "expected_intent": "what_is_fpulse"},
    {"prompt": "what does this product do", "expected_path": P_FAST, "expected_intent": "what_is_fpulse"},
    {"prompt": "explain f-pulse", "expected_path": P_FAST, "expected_intent": "what_is_fpulse"},
    {"prompt": "what is this tool", "expected_path": P_FAST, "expected_intent": "what_is_fpulse"},

    # about_hybridyn paraphrases
    {"prompt": "what does hybridyn do", "expected_path": P_FAST, "expected_intent": "about_hybridyn"},
    {"prompt": "the company behind this", "expected_path": P_FAST, "expected_intent": "about_hybridyn"},

    # who_created paraphrases
    {"prompt": "who built this", "expected_path": P_FAST, "expected_intent": "who_created"},
    {"prompt": "who developed this", "expected_path": P_FAST, "expected_intent": "who_created"},
    {"prompt": "founder", "expected_path": P_FAST, "expected_intent": "who_created"},

    # what_tier paraphrases
    {"prompt": "what tier am i on", "expected_path": P_FAST, "expected_intent": "what_tier"},
    {"prompt": "which edition", "expected_path": P_FAST, "expected_intent": "what_tier"},
    {"prompt": "am i on plus", "expected_path": P_FAST, "expected_intent": "what_tier"},

    # overview paraphrases
    {"prompt": "give me an overview", "expected_path": P_FAST, "expected_intent": "overview"},
    {"prompt": "workspace summary", "expected_path": P_FAST, "expected_intent": "overview"},
    {"prompt": "summarize my workspace", "expected_path": P_FAST, "expected_intent": "overview"},

    # failed_executions paraphrases
    {"prompt": "what broke", "expected_path": P_FAST, "expected_intent": "failed_executions"},
    {"prompt": "any errors", "expected_path": P_FAST, "expected_intent": "failed_executions"},
    {"prompt": "failed runs", "expected_path": P_FAST, "expected_intent": "failed_executions"},

    # running_now paraphrases
    {"prompt": "currently running", "expected_path": P_FAST, "expected_intent": "running_now"},
    {"prompt": "active runs", "expected_path": P_FAST, "expected_intent": "running_now"},

    # list_executions paraphrases
    {"prompt": "show executions", "expected_path": P_FAST, "expected_intent": "list_executions"},
    {"prompt": "recent runs", "expected_path": P_FAST, "expected_intent": "list_executions"},
    {"prompt": "execution history", "expected_path": P_FAST, "expected_intent": "list_executions"},

    # navigate paraphrases
    {"prompt": "go to pipelines", "expected_path": P_FAST, "expected_intent": "navigate"},
    {"prompt": "open executions", "expected_path": P_FAST, "expected_intent": "navigate"},
    {"prompt": "take me to dashboard", "expected_path": P_FAST, "expected_intent": "navigate"},
    {"prompt": "open connections", "expected_path": P_FAST, "expected_intent": "navigate"},

    # ─── Alerts / email on failure (May 6 2026 user-reported) ────────
    {"prompt": "How can we trigger a mail through pipeline?", "expected_path": P_FAST, "expected_intent": "help.alerts_howto"},
    {"prompt": "If pipeline fail, I need a automated trigger should be sending the mail", "expected_path": P_FAST, "expected_intent": "help.alerts_howto"},
    {"prompt": "alert on failure", "expected_path": P_FAST, "expected_intent": "help.alerts_howto"},
    {"prompt": "send email when pipeline fails", "expected_path": P_FAST, "expected_intent": "help.alerts_howto"},
    {"prompt": "how to set up alert", "expected_path": P_FAST, "expected_intent": "help.alerts_howto"},
    {"prompt": "slack notification on failure", "expected_path": P_FAST, "expected_intent": "help.alerts_howto"},
]


# ─────────────────────────────────────────────────────────────────────
# Test runner
# ─────────────────────────────────────────────────────────────────────


def _classify(prompt: str, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run a prompt through the full Phase-1 routing decision and return
    the chosen path + intent. Mirrors api/agent.py order exactly."""
    from fpulse.ai.fast_router import try_match_scored, MIN_CONFIDENCE
    from fpulse.ai.clarify import needs_clarification
    from fpulse.ai.single_shot import should_use_single_shot
    from fpulse.ai.tools.base import ToolContext

    item_list = items if items is not None else _PIPELINES_3
    # Pick a sensible page based on dominant kind so empty-page cases also work.
    page = "pipelines.list"
    if item_list and any(it.get("kind") == "execution" for it in item_list):
        page = "executions.list"
    elif item_list and any(it.get("kind") == "connection" for it in item_list):
        page = "connections"
    ctx = ToolContext(
        tenant_id="default", user_id="test", workspace_id="default",
        environment="dev", page=page,
        visible_items=tuple(item_list),
    )

    # Step 1 — multi-step gate (Phase 1, fix #5)
    try:
        from fpulse.ai.multi_step import is_multi_step
        if is_multi_step(prompt):
            return {"path": P_AGENT, "intent": None, "reason": "multi_step"}
    except Exception:
        pass

    # Step 2 — fast-lane (post-typo-normalization is applied inside fast_router)
    scored = try_match_scored(prompt)
    if scored is not None:
        intent, conf, reason = scored
        if conf >= MIN_CONFIDENCE:
            return {"path": P_FAST, "intent": intent.name, "reason": reason}

    # Step 3 — clarify or auto-pin
    clar_kind = needs_clarification(prompt, ctx)
    if clar_kind is not None:
        # Reasoning + vague ref + ≥2 candidates of that kind = clarify card
        same = [it for it in ctx.visible_items if (it.get("kind") or "") == clar_kind]
        if len(same) >= 2:
            return {"path": P_CLARIFY, "intent": clar_kind, "reason": "clarify"}
        if len(same) == 1:
            return {"path": P_AUTO_PIN, "intent": clar_kind, "reason": "auto_pin_1"}

    # Step 4 — hybrid lane (tool + LLM format pass). Sits BEFORE
    # single-shot because hybrid serves the same "reasoning prompt"
    # bucket but adds a deterministic data fetch — strictly better
    # answer when a fast-lane intent matches. (Added May 17 2026.)
    try:
        from fpulse.ai.hybrid import should_use_hybrid
        hybrid_match = should_use_hybrid(prompt)
        if hybrid_match is not None:
            intent, conf, reason = hybrid_match
            return {"path": P_HYBRID, "intent": intent.name, "reason": reason}
    except Exception:
        pass

    # Step 5 — single-shot
    if should_use_single_shot(prompt):
        return {"path": P_SINGLE, "intent": None, "reason": "single_shot"}

    # Step 6 — agent loop
    return {"path": P_AGENT, "intent": None, "reason": "fallthrough"}


def _check(case: dict[str, Any]) -> dict[str, Any]:
    actual = _classify(case["prompt"], case.get("items"))
    exp_path = case["expected_path"]
    exp_intent = case.get("expected_intent")
    path_ok = actual["path"] == exp_path
    intent_ok = (exp_intent is None) or (actual["intent"] == exp_intent)
    return {
        "prompt": case["prompt"],
        "expected": f"{exp_path}" + (f":{exp_intent}" if exp_intent else ""),
        "actual": f"{actual['path']}" + (f":{actual['intent']}" if actual.get("intent") else ""),
        "reason": actual.get("reason", ""),
        "pass": path_ok and intent_ok,
    }


@router.get("/admin/router-tests")
async def router_tests() -> dict[str, Any]:
    results = [_check(c) for c in CASES]
    passed = sum(1 for r in results if r["pass"])
    failed = [r for r in results if not r["pass"]]
    return {
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": len(failed),
            "pass_rate": f"{passed}/{len(results)}",
        },
        "failures": failed,
        "all": results,
    }
