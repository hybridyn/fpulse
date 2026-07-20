"""
list_steward_findings tool — read.

Surfaces the F-Pulse Steward's current advisory findings to the Copilot so it
can answer "what does the Steward flag", "any duplicate sources", "governance
issues", "is anything risky in my workspace" without the user opening the
Insights page.

HONESTY SCOPE (2026-06-16 wiring audit). The Steward ships nine detectors but
only FOUR are fed by the running product today, so only those ever produce
findings here:
  • Archeologist   — DUPLICATE_SOURCE / DUPLICATE_PIPELINE (from workflow defs)
  • Rules engine   — USER_DEFINED       (from workflow defs + user YAML rules)
  • Governance     — ENV_CROSSING / UNAPPROVED_DESTINATION / PII_LEAK
                     (from workflow defs + the workspace governance policy)
  • Connector health — CONNECTOR_AUTH_FAILURE / RATE_LIMIT / UNREACHABLE /
                     CREDENTIAL_NEAR_EXPIRY (from real connection /test outcomes)

The schema-drift, data-quality, cost, and volume-anomaly detectors are
event-driven and NOTHING in the product records their input yet, so they are
silent. This tool returns the ACTUAL finding list (never fabricated) — but the
description deliberately does NOT claim drift/quality/cost monitoring, and the
output carries a `coverage` note so the model says "nothing recorded" rather
than "verified clean" for the un-fed detectors.

Read-only: runs the same deterministic scan the Insights page uses, with
``record=False`` so a Copilot query never inflates persistent occurrence
counters or fans out notifications.
"""

from __future__ import annotations

from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


_DEFAULT_LIMIT = 25
_MAX_LIMIT = 50
_SEV_RANK = {"p1": 3, "p2": 2, "p3": 1}


def _empty_payload(workspace_id: str, reason: str) -> dict[str, Any]:
    """Honest stub when the scan can't run — an empty list with the reason,
    never a misleading 'clean workspace'."""
    return {
        "workspace_id": workspace_id,
        "count": 0,
        "returned": 0,
        "by_level": {},
        "findings": [],
        "coverage": reason,
    }


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    workspace_id = (
        inputs.get("workspace_id")
        or ctx.workspace_id
        or ctx.tenant_id
        or "default"
    )
    status = (inputs.get("status") or "open").strip().lower()
    level = (inputs.get("level") or "").strip().lower()
    min_severity = (inputs.get("min_severity") or "").strip().lower()

    raw_limit = inputs.get("limit", _DEFAULT_LIMIT)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    limit = max(1, min(limit, _MAX_LIMIT))

    try:
        # _run_scan is the same aggregator the /api/steward/findings endpoint
        # uses. record=False keeps this a pure read (no journaling / bell fan-out).
        from fpulse.api.steward import _run_scan

        findings, _settings = _run_scan(workspace_id, record=False)
    except Exception as exc:  # noqa: BLE001 — a scan bug must not break Copilot
        return _empty_payload(
            workspace_id, f"Could not run the Steward scan: {type(exc).__name__}."
        )

    if status and status != "all":
        findings = [f for f in findings if f.status.value == status]
    if level:
        findings = [f for f in findings if f.level.value == level]
    if min_severity in _SEV_RANK:
        findings = [
            f for f in findings
            if _SEV_RANK.get(f.severity.value, 1) >= _SEV_RANK[min_severity]
        ]

    # Most urgent first: severity desc, then recurrence count desc.
    findings.sort(
        key=lambda f: (_SEV_RANK.get(f.severity.value, 1), int(f.occurrences)),
        reverse=True,
    )

    by_level: dict[str, int] = {}
    for f in findings:
        by_level[f.level.value] = by_level.get(f.level.value, 0) + 1

    rows: list[dict[str, Any]] = []
    for f in findings[:limit]:
        action_labels = [
            str(a.get("label"))
            for a in (f.proposed_actions or [])
            if isinstance(a, dict) and a.get("label")
        ][:4]
        rows.append({
            "id": f.id,
            "kind": f.kind.value,
            "level": f.level.value,
            "severity": f.severity.value,
            "status": f.status.value,
            "title": (f.title or "")[:120],
            "summary": (f.body or "")[:240],
            "confidence": f.confidence,
            "occurrences": int(f.occurrences),
            "suggested_actions": action_labels,
        })

    return {
        "workspace_id": workspace_id,
        "count": len(findings),
        "returned": len(rows),
        "by_level": by_level,
        "findings": rows,
        # Honesty note so the model never reads an empty list as "verified
        # clean" for detectors the product doesn't feed yet.
        "coverage": (
            "Live detectors: duplicate sources/pipelines, user rules, governance "
            "(env-crossing, unapproved destination, PII columns), connector health. "
            "Schema-drift, data-quality, cost, and volume-anomaly detectors are not "
            "fed by execution yet — their absence here means 'nothing recorded', "
            "not 'verified clean'."
        ),
    }


DEFINITION = ToolDefinition(
    name="list_steward_findings",
    tier=ToolTier.READ,
    description=(
        "List the F-Pulse Steward's current advisory findings for the workspace. "
        "The Steward is read-only — it never changes anything; it flags issues for "
        "the user to act on. Today it reports: duplicate data sources / duplicate "
        "pipelines, governance violations (a dev credential used in prod, writes to "
        "an unapproved destination, PII-suggestive columns flowing out), connector "
        "health (auth failures, rate limits, unreachable sources, credentials near "
        "expiry — derived from real connection tests), and matches from user-defined "
        "rules. Use for 'what does the Steward flag', 'any duplicate sources', "
        "'governance / compliance issues', 'is anything risky here', 'review my "
        "workspace'. An empty result means nothing is flagged among those detectors "
        "— not a full data-quality/cost audit (those detectors aren't fed yet). "
        "Pair with get_installation_health for run/inventory health."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "workspace_id": {
                "type": "string",
                "description": "Optional workspace id. Defaults to the caller's workspace.",
            },
            "status": {
                "type": "string",
                "enum": ["open", "all", "acknowledged", "dismissed", "resolved", "rebounded"],
                "description": "Filter by lifecycle status. Default 'open' (active alerts).",
            },
            "level": {
                "type": "string",
                "enum": [
                    "pipeline", "node", "connector", "data",
                    "architecture", "governance", "cost",
                ],
                "description": "Optional observability-level filter (e.g. 'governance', 'connector').",
            },
            "min_severity": {
                "type": "string",
                "enum": ["p1", "p2", "p3"],
                "description": "Only return findings at this severity or higher (p1 highest).",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_LIMIT,
                "description": f"Max findings to return (1-{_MAX_LIMIT}, default {_DEFAULT_LIMIT}).",
            },
        },
    },
    output_schema={
        "workspace_id": "str",
        "count": "int",
        "returned": "int",
        "by_level": "dict",
        "findings": "list",
        "coverage": "str",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["steward", "findings", "governance", "advisory", "read"],
)
