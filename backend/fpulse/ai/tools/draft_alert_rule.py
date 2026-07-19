"""
draft_alert_rule tool — safe-write (draft only).

Composes an alert-rule configuration from a natural-language description,
returns a draft that the user can review and save via the Alerts page.
The agent does NOT save the rule directly — keeps the safe-write contract.
"""

from __future__ import annotations

import secrets
from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    workflow_id = inputs.get("workflow_id") or ""
    severity = (inputs.get("severity") or "warning").lower()
    description = (inputs.get("description") or "").strip()
    condition = (inputs.get("condition") or "").strip()
    channels = inputs.get("channels") or ["in-app"]
    idempotency_key = inputs.get("idempotency_key")

    if not description:
        raise ValueError("description is required")
    if severity not in ("info", "warning", "error", "critical"):
        severity = "warning"
    if not idempotency_key:
        raise ValueError("idempotency_key is required for safe-write tools")

    if ctx.dry_run:
        return {
            "draft_id": "dry-run-alert",
            "workflow_id": workflow_id,
            "severity": severity,
            "condition": condition or "(none)",
            "rule": {},
            "message": "[dry-run] No alert rule persisted.",
        }

    # Stub a rule scaffold — real wiring is done by the user via Alerts UI.
    # Defaults are conservative: notify in-app on FAILURE, fire once per
    # 24h to avoid alert fatigue.
    rule = {
        "name": (description[:60] + ("…" if len(description) > 60 else "")) or "Suggested alert",
        "workflow_id": workflow_id or None,
        "severity": severity,
        "condition": condition or "on_failure",
        "channels": channels,
        "cooldown_seconds": 86400,
        "enabled": False,  # draft → user must enable
        "draft_origin": "copilot",
    }

    return {
        "draft_id": "alert-draft-" + secrets.token_urlsafe(6),
        "workflow_id": workflow_id,
        "severity": severity,
        "condition": rule["condition"],
        "rule": rule,
        "message": (
            f"Drafted a {severity!r} alert. Open Alerts → Add Rule, paste the "
            f"draft, review/edit, and click Save to enable."
        ),
    }


DEFINITION = ToolDefinition(
    name="draft_alert_rule",
    tier=ToolTier.SAFE_WRITE,
    description=(
        "Draft an alert-rule scaffold from a natural-language description. "
        "Returns a non-persisted draft. The user must review + save the rule "
        "via the Alerts page; nothing fires until they do. Use for 'alert me "
        "when pipeline X fails twice in an hour' style asks."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "What should trigger the alert (NL).",
            },
            "workflow_id": {
                "type": "string",
                "description": "Optional pipeline this rule attaches to.",
            },
            "severity": {
                "type": "string",
                "enum": ["info", "warning", "error", "critical"],
                "default": "warning",
            },
            "condition": {
                "type": "string",
                "description": "Optional structured condition (e.g. 'on_failure', 'duration > 60s').",
            },
            "channels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Notification channels (in-app, email, slack, teams, webhook).",
            },
            "idempotency_key": {
                "type": "string",
                "description": "Required. Format: {tier}.{user_id}.{action}.{target_id}.{semver}",
            },
        },
        "required": ["description", "idempotency_key"],
    },
    output_schema={
        "draft_id": "str",
        "workflow_id": "str",
        "severity": "str",
        "condition": "str",
        "rule": "dict",
        "message": "str",
    },
    handler=_handler,
    requires_idempotency_key=True,
    tags=["alert", "draft"],
)
