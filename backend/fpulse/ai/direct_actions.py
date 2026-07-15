"""
Direct-action layer — May 5 2026.

Imperative requests like "test this connection", "run this pipeline",
"cancel this run" route here instead of the LLM agent loop. The handler:

  1. Resolves the target entity from ``ctx`` (selected_ids → single
     visible_items match → quoted name in prompt).
  2. For READ-only actions (e.g. test connection), executes the
     in-process service directly — returns the real result.
  3. For WRITE actions (run / cancel), emits a structured preview the
     user must explicitly confirm before anything mutates.

Every direct action runs WITHOUT an LLM in the path. Latency target:
test_connection ~50ms-3s (depends on real protocol round-trip);
run/cancel preview ~5ms (pure local rendering).

Public surface:
  * ``direct_test_connection(prompt, ctx) -> str``
  * ``direct_run_pipeline(prompt, ctx) -> str``
  * ``direct_cancel_execution(prompt, ctx) -> str``
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fpulse.ai.tools.base import ToolContext

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Target resolution
# ─────────────────────────────────────────────────────────────────────


_QUOTED_NAME_RE = re.compile(r"""['"`]([^'"`]+)['"`]""")


def _extract_quoted_name(prompt: str) -> str | None:
    m = _QUOTED_NAME_RE.search(prompt)
    return m.group(1).strip() if m else None


def resolve_target(ctx: ToolContext, kind: str, prompt: str) -> dict[str, Any] | None:
    """Pick the entity the user is referring to.

    Resolution order (most-specific first):
      1. ``ctx.selected_ids[0]`` matched against ``ctx.visible_items``
         filtered by ``kind`` — explicit user click on the page.
      2. If exactly one ``visible_item`` of this kind is on screen, use it.
      3. A quoted name in the prompt that matches a visible_item by name
         (case-insensitive substring).

    Returns the matched item dict, or None if the target is ambiguous /
    unknown. Callers must render a friendly disambiguation message in
    the None case.
    """
    matching_kind = [it for it in ctx.visible_items if (it.get("kind") or "") == kind]

    if ctx.selected_ids:
        sel = ctx.selected_ids[0]
        for it in matching_kind:
            if it.get("id") == sel:
                return it

    if len(matching_kind) == 1:
        return matching_kind[0]

    name = _extract_quoted_name(prompt)
    if name and matching_kind:
        lower = name.lower()
        for it in matching_kind:
            if (it.get("name") or "").lower() == lower:
                return it
        for it in matching_kind:
            if lower in (it.get("name") or "").lower():
                return it

    return None


def _disambiguate_message(kind: str, items: list[dict[str, Any]], intent_name: str = "") -> str:
    """Render a clickable choices card so the user picks a target with
    one click instead of typing a quoted name."""
    label = {"connection": "connection", "pipeline": "pipeline",
             "execution": "execution"}.get(kind, kind)
    if not items:
        # Empty page — give a navigate chip to the right page.
        nav_page = {"pipeline": "workflows", "execution": "executions",
                    "connection": "connections"}.get(kind, "dashboard")
        from fpulse.ai.interactive_cards import next_actions_card
        chips = next_actions_card(actions=[
            {
                "label": f"Open {nav_page.title()}",
                "icon": "📂",
                "style": "primary",
                "action": {"kind": "navigate", "page": nav_page},
            },
        ])
        intro = (
            f"I can't see any {label}s on this page to act on. "
            f"Open the **{nav_page.title()}** page first, or quote a name "
            f"like `\"daily-ingest\"`."
        )
        return f"{intro}\n\n{chips}"
    # Multiple candidates — emit a choices card. Inferred intent (if any)
    # drives the slot-fill action so click → resolve → execute in one hop.
    from fpulse.ai.interactive_cards import choices_card
    intent_for_action = intent_name or {
        "pipeline": "run_pipeline",
        "execution": "cancel_run",
        "connection": "test_connection",
    }.get(kind, "view_pipeline")
    return choices_card(
        title=f"Which {label}?",
        subtitle=f"I see {len(items)} on this page",
        items=items,
        intent_name=intent_for_action,
        fallback="Or click an item on the page first.",
    )


# ─────────────────────────────────────────────────────────────────────
# test_connection — read-only, no confirmation needed
# ─────────────────────────────────────────────────────────────────────


async def direct_test_connection(prompt: str, ctx: ToolContext) -> str:
    """Test a saved connection by ID, in-process. No LLM, no HTTP re-entry."""
    target = resolve_target(ctx, "connection", prompt)
    if target is None:
        items = [it for it in ctx.visible_items if (it.get("kind") or "") == "connection"]
        return _disambiguate_message("connection", items, intent_name="test_connection")

    conn_id = target.get("id")
    name = target.get("name") or conn_id

    try:
        from fpulse.connections.tester import ConnectionTester
        from fpulse.main import app_state  # type: ignore

        conn_store = app_state.get("connection_store") if app_state else None
        if conn_store is None:
            return f"Couldn't access connection store. Try the Connections page → Test."
        connection = conn_store.get(conn_id, workspace_id=ctx.workspace_id)
        if connection is None:
            return f"Connection `{conn_id}` not found in this workspace."

        config = dict(connection.config)
        if connection.credential_id:
            cred_store = app_state.get("credential_store")
            if cred_store is not None:
                cred = cred_store.get_raw(connection.credential_id)
                if cred and cred.config:
                    config.update(cred.config)

        tester = ConnectionTester()
        result = tester.test_connection(connection.type, config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("direct_test_connection failed for %s: %s", conn_id, exc)
        return f"Couldn't test **{name}**: {str(exc)[:200]}"

    ok = bool(result.get("success"))
    detail = result.get("message") or result.get("detail") or ""
    from fpulse.ai.interactive_cards import chips_for_connection, next_actions_card
    chips = next_actions_card(actions=chips_for_connection(entity_id=str(conn_id), entity_name=str(name)))
    head = f"✅ **{name}** is reachable. {detail[:240]}".strip() if ok \
           else f"❌ **{name}** failed connectivity check.\n\n{detail[:400]}".strip()
    return head + "\n\n" + chips


# ─────────────────────────────────────────────────────────────────────
# run_pipeline — write action, preview + explicit confirm
# ─────────────────────────────────────────────────────────────────────


_LIVE_KEYWORDS = ("live", "for real", "production", "prod ")
_SAMPLE_KEYWORDS = ("sample", "preview", "small", "limited")
_DRY_KEYWORDS = ("dry run", "dry-run", "validate", "validation only")


def _detect_safety_mode(prompt: str) -> str:
    p = prompt.lower()
    if any(k in p for k in _DRY_KEYWORDS):
        return "dry_run"
    if any(k in p for k in _LIVE_KEYWORDS):
        return "live"
    if any(k in p for k in _SAMPLE_KEYWORDS):
        return "sample"
    return "sample"  # safe default


async def direct_run_pipeline(prompt: str, ctx: ToolContext) -> str:
    """Build a confirmation preview for running a pipeline. Does NOT
    execute — the agent's frontend confirmation card is the gate. The
    user clicks Confirm, which calls the existing
    ``POST /api/execution/workflow/{id}`` endpoint.
    """
    target = resolve_target(ctx, "pipeline", prompt)
    if target is None:
        items = [it for it in ctx.visible_items if (it.get("kind") or "") == "pipeline"]
        return _disambiguate_message("pipeline", items, intent_name="run_pipeline")

    pipeline_id = target.get("id")
    name = target.get("name") or pipeline_id
    safety_mode = _detect_safety_mode(prompt)
    env = ctx.environment or "dev"

    from fpulse.ai.interactive_cards import confirm_card
    mode_hint = {
        "sample": "Sample mode runs against a small row sample — safe for iteration.",
        "dry_run": "Dry-run validates IR + connections without executing any step.",
        "live": "Live mode processes the full dataset and writes to all sinks.",
    }[safety_mode]
    options: list[dict[str, Any]] = [
        {
            "label": "Confirm (sample)",
            "style": "primary",
            "action": {
                "kind": "execute",
                "endpoint": f"/api/execution/workflow/{pipeline_id}",
                "method": "POST",
                "query": {"environment": env, "safety_mode": "sample", "full_run": False},
            },
        },
        {
            "label": "Live mode",
            "style": "danger",
            "action": {
                "kind": "execute",
                "endpoint": f"/api/execution/workflow/{pipeline_id}",
                "method": "POST",
                "query": {"environment": env, "safety_mode": "live", "full_run": True},
            },
        },
        {
            "label": "Cancel",
            "action": {"kind": "ask", "prompt": ""},
        },
    ]
    body = confirm_card(
        title=f"Run pipeline: {name}",
        summary=f"{mode_hint} About to run **{name}** in `{env.upper()}`.",
        tier="high_impact_write" if safety_mode == "live" else "safe_write",
        options=options,
        details=[
            {"label": "Pipeline", "value": str(name)},
            {"label": "Environment", "value": env.upper()},
            {"label": "Default mode", "value": safety_mode},
        ],
    )
    return body


# ─────────────────────────────────────────────────────────────────────
# cancel_execution — write action, preview + explicit confirm
# ─────────────────────────────────────────────────────────────────────


async def direct_cancel_execution(prompt: str, ctx: ToolContext) -> str:
    """Build a confirmation preview for cancelling a running execution.
    Cancellation is reversible only by re-running, so we never auto-fire."""
    target = resolve_target(ctx, "execution", prompt)
    if target is None:
        items = [it for it in ctx.visible_items if (it.get("kind") or "") == "execution"]
        running = [e for e in items if (e.get("status") or "").lower() in ("running", "queued")]
        if running:
            items = running
        return _disambiguate_message("execution", items, intent_name="cancel_run")

    status = (target.get("status") or "").lower()
    if status not in ("running", "queued"):
        name = target.get("name") or target.get("id")
        return f"**{name}** is `{status or 'unknown'}` — nothing to cancel."

    workflow_id = (target.get("meta") or {}).get("workflow_id") or target.get("id")
    name = target.get("name") or target.get("id")
    from fpulse.ai.interactive_cards import confirm_card
    options = [
        {
            "label": "Confirm cancel",
            "style": "danger",
            "action": {
                "kind": "execute",
                "endpoint": f"/api/info/cancel/{workflow_id}",
                "method": "POST",
            },
        },
        {"label": "Keep running", "action": {"kind": "ask", "prompt": ""}},
    ]
    return confirm_card(
        title=f"Cancel run: {name}",
        summary=(
            f"Cancel the in-flight run of **{name}**? Steps already started "
            f"may finish; subsequent steps won't begin. This is irreversible."
        ),
        tier="high_impact_write",
        options=options,
        details=[
            {"label": "Workflow", "value": str(name)},
            {"label": "Status", "value": str(status)},
        ],
    )
