"""
Router coverage audit — May 6 2026.

Runs a structured 200-prompt sample (the user-advisory dataset, grouped
by behavioral category) through the classifier and reports which path
each prompt takes.

Purpose: replace the "should I train a model?" debate with an empirical
table. Anything that lands on `agent` (the slow path) is a gap in
trigger coverage — fix in code, no training needed.

GET /api/admin/router-coverage
  → {by_category: {A_help: [...], B_list: [...], ...},
     summary: {by_path: {fast-lane: N, clarify: M, agent: K, ...}}}
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from fpulse.auth.deps import require_admin

router = APIRouter(dependencies=[Depends(require_admin)])


# Categories mirror the user's advisory grouping. Each prompt classified
# without simulated visible_items so we see baseline router coverage —
# entity-resolving prompts (e.g. "run daily ingest") naturally fall to
# clarify/agent in this view, and that's correct.
PROMPT_SET: dict[str, list[str]] = {
    "A_help": [
        "What can you help me with?",
        "What can you do here?",
        "What can you help me with on this page?",
        "What actions are available?",
        "How do I use this page?",
        "What should I do next?",
        "Show me what I can do",
        "Guide me through this",
        "Help me get started",
        "Explain this page",
        "What options do I have?",
        "What features are here?",
        "What can I do with pipelines?",
        "What can I do with connections?",
        "Walk me through this app",
        "Walk through the workflow",
        "Show available actions",
        "How do I begin?",
        "What is this page for?",
        "What can I manage here?",
        "wat can u help me with",
        "help me wit this page",
        "wat can i do here",
    ],
    "B_listing": [
        "Show pipelines",
        "List pipelines",
        "Show all pipelines",
        "What pipelines do I have?",
        "Show active pipelines",
        "Show failed pipelines",
        "Show recent pipelines",
        "List connections",
        "Show connections",
        "Show executions",
        "Show recent executions",
        "Show failed executions",
        "Show running pipelines",
        "What is currently running?",
        "Show latest runs",
        "Show last 5 executions",
        "Show pipeline status",
        "Show connection status",
        "give pipelines",
        "pipelines list",
        "show me pipelines",
        "what pipelines exist",
    ],
    "C_run": [
        "Run pipeline",
        "Run daily ingest",
        "Start daily ingest pipeline",
        "Execute pipeline",
        "Trigger pipeline",
        "Run this pipeline",
        "Start this",
        "Run the first pipeline",
        "Run pipeline number 1",
        "Execute the selected pipeline",
        "Run the current pipeline",
        "Start job",
        "Trigger job",
        "Kick off pipeline",
        "Run now",
        "Run immediately",
        'run "Daily ingest"',
        'start "ETL pipeline"',
        "rn pipeline",
        "run pipline",
        "execte pipeline",
    ],
    "D_cancel": [
        "Cancel run",
        "Stop pipeline",
        "Stop execution",
        "Cancel this run",
        "Stop this job",
        "Abort execution",
        "Kill run",
        "Cancel current execution",
        "Stop running pipeline",
        "Cancel pipeline job",
        "Stop now",
        "Abort this",
    ],
    "E_failures": [
        "Why did this fail?",
        "Why did pipeline fail?",
        "Show failures",
        "Show failed runs",
        "What went wrong?",
        "Explain failure",
        "Show error logs",
        "What is the issue?",
        "Why is this failing?",
        "Debug this pipeline",
        "Show failure reason",
        "What caused this failure?",
        "Show recent failures",
        "Why is this run slow?",
        "Why is this taking long?",
        "Why is this stuck?",
        "Investigate this run",
    ],
    "F_connections": [
        "Test connection",
        "Test database connection",
        "Check connection",
        "Verify connection",
        "Validate connection",
        "Is connection working?",
        "Check if connection is valid",
        "Test this connection",
        "Run connection test",
        "Check credentials",
        "Verify credentials",
        "Add connection",
        "Create connection",
        "Update connection",
        "Store credentials",
        "How to store credentials?",
    ],
    "G_navigation": [
        "Go to pipelines",
        "Open pipelines",
        "Show pipelines page",
        "Navigate to executions",
        "Open connections page",
        "Take me to dashboard",
        "Go back",
        "Open this pipeline",
        "View details",
        "Show details",
        "Open logs",
        "Show logs",
    ],
    "H_clarify": [
        "Run pipeline",
        "Run this",
        "Run it",
        "Start this",
        "Use this one",
        "Execute that",
        "Run the second one",
        "Run first pipeline",
        "Use first",
        "Pick second",
        "Choose this pipeline",
    ],
    "I_followup_state": [
        "first",
        "second",
        "the first one",
        "the second one",
        "yes",
        "no",
        "do it",
        "go ahead",
        "run it",
        "cancel it",
        "what about it?",
        "explain that",
        "why that one?",
        "show more details",
    ],
    "J_multistep": [
        "List failed pipelines then rerun latest",
        "Show failures and fix them",
        "Find failed pipeline and retry",
        "Check connection and run pipeline",
        "Show pipelines then execute first",
        "Investigate failure and suggest fix",
        "Debug pipeline and rerun",
        "Analyze performance and optimize",
    ],
    "K_edge": [
        "test",
        "hello",
        "hi",
        "?",
        "do something",
        "help",
        "ok",
        "run",
        "pipeline",
        "error",
        "something is wrong",
    ],
}


def _classify(prompt: str) -> dict[str, Any]:
    """Same classifier the test harness uses, with a synthetic
    multi-pipeline context so clarify cases can resolve."""
    from fpulse.ai.fast_router import try_match_scored, MIN_CONFIDENCE
    from fpulse.ai.clarify import needs_clarification
    from fpulse.ai.single_shot import should_use_single_shot
    from fpulse.ai.tools.base import ToolContext

    # Realistic context: 3 pipelines visible (so clarify cases work).
    items = [
        {"id": "p1", "name": "Daily ingest",  "kind": "pipeline", "status": "failed"},
        {"id": "p2", "name": "ETL pipeline",  "kind": "pipeline", "status": "success"},
        {"id": "p3", "name": "Stripe sync",   "kind": "pipeline", "status": "running"},
    ]
    ctx = ToolContext(
        tenant_id="default", user_id="audit", workspace_id="default",
        environment="dev", page="pipelines.list",
        visible_items=tuple(items),
    )

    try:
        from fpulse.ai.multi_step import is_multi_step
        if is_multi_step(prompt):
            return {"path": "agent", "intent": "(multi-step)", "reason": "multi_step"}
    except Exception:
        pass

    scored = try_match_scored(prompt)
    if scored is not None:
        intent, conf, reason = scored
        if conf >= MIN_CONFIDENCE:
            return {"path": "fast-lane", "intent": intent.name, "reason": f"{reason} ({conf:.2f})"}

    clar_kind = needs_clarification(prompt, ctx)
    if clar_kind is not None:
        same = [it for it in ctx.visible_items if (it.get("kind") or "") == clar_kind]
        if len(same) >= 2:
            return {"path": "clarify", "intent": clar_kind, "reason": f"{len(same)} candidates"}
        if len(same) == 1:
            return {"path": "auto-pin", "intent": clar_kind, "reason": "1 candidate"}

    # Hybrid lane sits BEFORE single-shot — see api/agent.py and
    # fpulse/ai/hybrid.py (added May 17 2026).
    try:
        from fpulse.ai.hybrid import should_use_hybrid
        hybrid_match = should_use_hybrid(prompt)
        if hybrid_match is not None:
            intent, conf, reason = hybrid_match
            return {"path": "hybrid", "intent": intent.name, "reason": f"{reason} ({conf:.2f})"}
    except Exception:
        pass

    if should_use_single_shot(prompt):
        return {"path": "single-shot", "intent": "(LLM)", "reason": "reasoning"}

    return {"path": "agent", "intent": "(LLM loop)", "reason": "fallthrough"}


@router.get("/admin/router-coverage")
async def router_coverage() -> dict[str, Any]:
    by_category: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    total_per_category: dict[str, dict[str, int]] = {}
    total = 0

    for cat, prompts in PROMPT_SET.items():
        rows: list[dict[str, Any]] = []
        cat_counts: dict[str, int] = {}
        for p in prompts:
            r = _classify(p)
            rows.append({
                "prompt": p,
                "path": r["path"],
                "intent": r["intent"],
                "reason": r["reason"],
            })
            counts[r["path"]] = counts.get(r["path"], 0) + 1
            cat_counts[r["path"]] = cat_counts.get(r["path"], 0) + 1
            total += 1
        by_category[cat] = rows
        total_per_category[cat] = cat_counts

    return {
        "summary": {
            "total": total,
            "by_path": counts,
            "by_category": total_per_category,
        },
        "by_category": by_category,
    }
