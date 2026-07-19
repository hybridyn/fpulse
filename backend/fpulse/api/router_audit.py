"""
Router audit endpoint — May 5 2026.

Read-only diagnostic: takes every historical prompt in the trace store
plus a curated reference set, runs each one through the current router
(fast-lane → clarify → single-shot → agent), and returns a table of
``(prompt, current_path, would_match, missing_triggers)``.

This endpoint exists to STOP guessing about router coverage. Before
writing more fixes, we look at what the system actually does on real
input. No LLM is invoked — every classification is pure-Python.

GET /api/admin/router-audit?include_reference=true
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from fpulse.auth.deps import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])


# Reference prompts — categories we explicitly want covered. Each one
# tagged with the path we believe SHOULD handle it.
_REFERENCE_PROMPTS: list[dict[str, str]] = [
    # Greetings / help / FAQ — should be fast-lane STATIC
    {"prompt": "hi", "expected": "fast-lane.greet"},
    {"prompt": "what can you do?", "expected": "fast-lane.help"},
    {"prompt": "what is f-pulse?", "expected": "fast-lane.what_is_fpulse"},

    # List intents — fast-lane DYNAMIC, served from page or tool
    {"prompt": "list pipelines", "expected": "fast-lane.list_pipelines"},
    {"prompt": "show me my pipelines", "expected": "fast-lane.list_pipelines"},
    {"prompt": "give me a quick overview of my workspace", "expected": "fast-lane.overview"},
    {"prompt": "list connections", "expected": "fast-lane.list_connections"},
    {"prompt": "show schedules", "expected": "fast-lane.list_schedules"},

    # Failure / running — token-overlap edge cases
    {"prompt": "what failed today?", "expected": "fast-lane.failed_executions"},
    {"prompt": "show me recent pipeline failures.", "expected": "fast-lane.failed_executions"},
    {"prompt": "any errors today", "expected": "fast-lane.failed_executions"},
    {"prompt": "which pipelines have failed", "expected": "fast-lane.failed_executions"},
    {"prompt": "what's running now", "expected": "fast-lane.running_now"},
    {"prompt": "currently running pipelines", "expected": "fast-lane.running_now"},

    # Direct actions — should match direct.* with on-page entity
    {"prompt": "run this pipeline", "expected": "fast-lane.direct.run_pipeline"},
    {"prompt": "test this connection", "expected": "fast-lane.direct.test_connection"},
    {"prompt": "cancel this run", "expected": "fast-lane.direct.cancel_execution"},

    # Vague reasoning — should hit clarify (multiple candidates) or
    # empty-clarify (no candidates) — NEITHER should hit agent loop
    {"prompt": "why did my last pipeline fail?", "expected": "clarify"},
    {"prompt": "why did this run fail", "expected": "clarify"},
    {"prompt": "why is this run slower than usual?", "expected": "clarify_or_empty"},
    {"prompt": "explain this pipeline", "expected": "clarify"},

    # Single-shot reasoning — known entity, no tools needed
    {"prompt": "explain why pipelines time out", "expected": "single-shot"},
    {"prompt": "what does the transform node do?", "expected": "single-shot"},
    {"prompt": "how does scheduling work?", "expected": "single-shot"},

    # Multi-step / open-ended — fall to agent (rare, intentional)
    {"prompt": "build me a pipeline that ingests CSV, joins on id, writes to postgres",
     "expected": "agent"},
    {"prompt": "first list failed pipelines then retry the most recent one",
     "expected": "agent"},

    # Ordinal slot-fill replies — only meaningful with pending_intent
    {"prompt": "first", "expected": "slot-fill (needs pending_intent)"},
    {"prompt": "the second one", "expected": "slot-fill (needs pending_intent)"},
    {"prompt": "yes", "expected": "slot-fill (needs pending_intent)"},

    # Reference substitution — only with active_entity
    {"prompt": "run it", "expected": "ref-sub.run_pipeline (needs active_entity)"},
    {"prompt": "what about it", "expected": "ref-sub or fast-lane"},
]


def _classify_prompt(prompt: str, has_visible_pipelines: bool = False, has_visible_executions: bool = False) -> dict[str, Any]:
    """Run a prompt through the router classifier (NO execution, NO LLM)."""
    from fpulse.ai.fast_router import try_match_scored, MIN_CONFIDENCE
    from fpulse.ai.clarify import needs_clarification
    from fpulse.ai.single_shot import should_use_single_shot
    from fpulse.ai.tools.base import ToolContext
    from fpulse.ai.dialogue_state import try_slot_fill, parse_state, ActiveIntent, DialogueState

    # Simulated context: a user on Workflows page with mixed pipelines
    items: list[dict[str, Any]] = []
    if has_visible_pipelines:
        items = [
            {"id": "p1", "name": "Daily ingest", "kind": "pipeline", "status": "failed"},
            {"id": "p2", "name": "Stripe sync",  "kind": "pipeline", "status": "success"},
            {"id": "p3", "name": "Nightly ETL",  "kind": "pipeline", "status": "failed"},
        ]
    if has_visible_executions:
        items = [
            {"id": "e1", "name": "Daily ingest", "kind": "execution", "status": "failed"},
            {"id": "e2", "name": "Stripe sync",  "kind": "execution", "status": "success"},
        ]
    ctx = ToolContext(
        tenant_id="default", user_id="audit", workspace_id="default",
        environment="dev", page="pipelines.list" if has_visible_pipelines else "executions.list" if has_visible_executions else "unknown",
        visible_items=tuple(items),
    )

    out: dict[str, Any] = {"prompt": prompt}

    # 1. Slot-fill probe with synthetic pending_intent
    pending_state = DialogueState(active_intent=ActiveIntent(name="diagnose_failure", missing_slot="entity"))
    sf = try_slot_fill(prompt, pending_state, ctx)
    out["slot_fill"] = f"would resolve to {sf.entity.name} (reason={sf.reason})" if sf else "no"

    # 2. Fast-lane match
    scored = try_match_scored(prompt)
    if scored is not None:
        intent, conf, reason = scored
        out["fast_lane"] = f"{intent.name} (conf={conf:.2f}, {reason})"
    else:
        out["fast_lane"] = f"NO MATCH (below {MIN_CONFIDENCE})"

    # 3. Clarify probe
    clar_kind = needs_clarification(prompt, ctx)
    out["clarify"] = clar_kind or "no"

    # 4. Single-shot probe
    out["single_shot"] = "yes" if should_use_single_shot(prompt) else "no"

    # 4b. Hybrid probe (May 17 2026) — tool fetch + LLM format pass
    from fpulse.ai.hybrid import should_use_hybrid
    hybrid_match = should_use_hybrid(prompt)
    out["hybrid"] = (
        f"{hybrid_match[0].name} (conf={hybrid_match[1]:.2f})"
        if hybrid_match is not None else "no"
    )

    # 5. Final routing decision (mirrors api/agent.py order). Hybrid sits
    # before single-shot — see comments in api/agent.py.
    if scored is not None:
        out["chosen_path"] = f"FAST-LANE: {scored[0].name}"
    elif clar_kind is not None:
        out["chosen_path"] = "CLARIFY"
    elif hybrid_match is not None:
        out["chosen_path"] = f"HYBRID: {hybrid_match[0].name}"
    elif should_use_single_shot(prompt):
        out["chosen_path"] = "SINGLE-SHOT (LLM)"
    else:
        out["chosen_path"] = "FULL AGENT LOOP (slow)"

    return out


@router.get("/admin/router-audit")
async def router_audit(request: Request, include_reference: bool = True) -> dict[str, Any]:
    """Run every historical + reference prompt through the router and
    return classification results. Read-only. Does not call any LLM."""
    rows: list[dict[str, Any]] = []

    # ── 1. Historical prompts from trace store ───────────────────────
    historical: list[str] = []
    try:
        from fpulse.main import app_state  # type: ignore
        ts = app_state.get("trace_store") if app_state else None
        if ts is not None and getattr(ts, "_db", None) is not None:
            cur = ts._db.execute(
                "SELECT user_intent, outcome, iterations, elapsed_ms, created_at "
                "FROM agent_traces ORDER BY created_at DESC LIMIT 100"
            )
            for row in cur.fetchall():
                ui = row[0] if not isinstance(row, dict) else row.get("user_intent")
                if ui:
                    historical.append(str(ui))
    except Exception as exc:  # noqa: BLE001
        logger.warning("router-audit: trace store read failed: %s", exc)

    # Dedupe while preserving order.
    seen: set[str] = set()
    unique_historical: list[str] = []
    for p in historical:
        if p in seen:
            continue
        seen.add(p)
        unique_historical.append(p)

    for prompt in unique_historical[:50]:
        rows.append({"source": "history", **_classify_prompt(prompt, has_visible_pipelines=True)})

    # ── 2. Reference prompts ─────────────────────────────────────────
    if include_reference:
        for ref in _REFERENCE_PROMPTS:
            cls = _classify_prompt(ref["prompt"], has_visible_pipelines=True)
            cls["expected"] = ref["expected"]
            cls["source"] = "reference"
            rows.append(cls)

    # ── 3. Summary ────────────────────────────────────────────────────
    summary = {"total": len(rows)}
    counts: dict[str, int] = {}
    for r in rows:
        path = r.get("chosen_path", "?").split(":")[0]
        counts[path] = counts.get(path, 0) + 1
    summary["by_path"] = counts

    return {"summary": summary, "rows": rows}
