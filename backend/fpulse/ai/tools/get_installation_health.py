"""
get_installation_health tool — read.

Wraps InventoryCollector to surface the same "installation health score" +
inventory totals + top-failing-pipelines roster the Reports page renders.
The Copilot uses this to answer punch-list / "what needs my attention" /
"audit my install" prompts in one tool call instead of fanning out across
list_executions + inspect_connections + list_schedules + list_alerts.

OSS-locked to tier="free": _compute_health then skips Plus-only checks
(admin-roster, approval-gate count) that would otherwise always fire and
mislead the reader.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


# Cap output to keep the JSON well under the agent loop's 4000-byte
# tool-result clamp. The caps are conservative — a typical OSS install
# has 0-3 issues and 0-5 failing pipelines, so clamping mostly matters
# for installs that actually need triage.
_MAX_ISSUES = 10
_MAX_TOP_FAILING = 5
_DEFAULT_TOP_FAILING = 5


# Keys we forward from report.totals. Stable subset — keeps the payload
# small and well-typed (every totals value is an int by construction).
_TOTAL_KEYS = (
    "projects",
    "pipelines",
    "pipelines_deployed",
    "pipelines_in_prod",
    "connections",
    "connections_inline_creds",
    "schedules",
    "schedules_enabled",
    "alerts",
    "alerts_enabled",
)


def _empty_payload(workspace_id: str, environment: str, reason: str) -> dict[str, Any]:
    """Stub payload used when the collector fails or app_state is unavailable.

    Score is 0 and the failure reason is the only listed issue so the
    LLM can tell the user we couldn't compute health rather than claiming
    a clean install. Better honest empty than a misleading 100/100.
    """
    return {
        "score": 0,
        "issue_count": 1,
        "issues": [reason],
        "totals": {k: 0 for k in _TOTAL_KEYS},
        "top_failing_pipelines": [],
        "recent_failures_24h": 0,
        "success_rate_pct_24h": 0.0,
        "workspace_id": workspace_id,
        "environment": environment,
        "tier": "free",
    }


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    workspace_id = (
        inputs.get("workspace_id")
        or ctx.workspace_id
        or ctx.tenant_id
        or "default"
    )
    # env_filter on the collector accepts "all" / "dev" / "prod" — map the
    # caller's environment unless they pin one explicitly. "all" by default
    # so totals reflect the whole install when answering "what needs my
    # attention" globally.
    env_input = (inputs.get("environment") or "all").lower()
    if env_input not in ("all", "dev", "prod"):
        env_input = "all"

    raw_top_n = inputs.get("top_n_failures", _DEFAULT_TOP_FAILING)
    try:
        top_n = int(raw_top_n)
    except (TypeError, ValueError):
        top_n = _DEFAULT_TOP_FAILING
    top_n = max(0, min(top_n, _MAX_TOP_FAILING))

    try:
        from fpulse.main import app_state  # type: ignore
    except Exception:
        return _empty_payload(workspace_id, env_input, "Backend state unavailable.")

    try:
        from fpulse.reports.inventory import InventoryCollector

        collector = InventoryCollector(
            app_state,
            caller=None,
            scope="admin",
            workspace_id=workspace_id,
            tier="free",
            env_filter=env_input,
        )
        report = collector.collect()
    except Exception as exc:  # noqa: BLE001 — never let a collector bug break Copilot
        return _empty_payload(
            workspace_id,
            env_input,
            f"Could not compute installation health: {type(exc).__name__}.",
        )

    health = report.health or {}
    issues_raw = health.get("issues") or []
    issues = [str(i) for i in issues_raw][:_MAX_ISSUES]
    score = health.get("score")
    if not isinstance(score, int):
        score = max(0, 100 - 10 * len(issues))

    totals_src = report.totals or {}
    totals = {k: int(totals_src.get(k, 0)) for k in _TOTAL_KEYS}

    audit = report.operational_audit
    top_failing: list[dict[str, Any]] = []
    # Prefer the 30-day failure-analysis rollup (counts + last error) when
    # populated; fall back to the 24h recent_failures roster otherwise.
    fa = report.failure_analysis
    if fa and fa.top_failing:
        for row in fa.top_failing[:top_n]:
            top_failing.append({
                "pipeline_name": row.pipeline_name,
                "failure_count": int(row.failure_count),
                "last_failure_at": row.last_failure_at,
                "last_error": row.last_error[:160],
            })
    elif audit and audit.recent_failures:
        for f in audit.recent_failures[:top_n]:
            top_failing.append({
                "pipeline_name": f.workflow_name,
                "failure_count": 1,
                "last_failure_at": f.failed_at,
                "last_error": (f.error or "")[:160],
            })

    return {
        "score": int(score),
        "issue_count": len(issues),
        "issues": issues,
        "totals": totals,
        "top_failing_pipelines": top_failing,
        "recent_failures_24h": int(audit.failed_executions) if audit else 0,
        "success_rate_pct_24h": float(audit.success_rate_pct) if audit else 0.0,
        "workspace_id": workspace_id,
        "environment": env_input,
        "tier": "free",
    }


DEFINITION = ToolDefinition(
    name="get_installation_health",
    tier=ToolTier.READ,
    description=(
        "Get the installation health score (0-100) plus a prioritised punch "
        "list of issues for the current workspace: connections still holding "
        "inline credentials, pipelines published-but-never-deployed, top "
        "failing pipelines from the last 30 days, 24h success rate, and "
        "headline inventory totals. Use for 'what needs my attention', "
        "'audit my install', 'health check', 'punch list', 'what should I "
        "fix first', 'which connections are risky'. One call returns the "
        "whole picture — prefer this over chaining list_executions + "
        "inspect_connections + list_schedules separately."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "workspace_id": {
                "type": "string",
                "description": "Optional workspace UUID. Defaults to the caller's workspace.",
            },
            "environment": {
                "type": "string",
                "enum": ["all", "dev", "prod"],
                "description": "Scope the report to a single environment. Default 'all'.",
            },
            "top_n_failures": {
                "type": "integer",
                "minimum": 0,
                "maximum": _MAX_TOP_FAILING,
                "description": (
                    f"How many top-failing pipelines to include "
                    f"(0-{_MAX_TOP_FAILING}, default {_DEFAULT_TOP_FAILING})."
                ),
            },
        },
    },
    output_schema={
        "score": "int",
        "issue_count": "int",
        "issues": "list",
        "totals": "dict",
        "top_failing_pipelines": "list",
        "recent_failures_24h": "int",
        "success_rate_pct_24h": "float",
        "workspace_id": "str",
        "environment": "str",
        "tier": "str",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["health", "audit", "punch_list", "read"],
)
