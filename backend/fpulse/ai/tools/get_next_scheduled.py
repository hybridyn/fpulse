"""
get_next_scheduled tool — read.

Returns the schedules that will fire within the next ``window_minutes``.
The Copilot uses this for "what runs in the next hour?" / "any conflicts?".
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from fpulse.ai.tools.base import ToolContext, ToolDefinition, ToolTier


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    window_minutes = int(inputs.get("window_minutes") or 60)
    window_minutes = max(1, min(window_minutes, 24 * 60))

    try:
        from fpulse.main import app_state  # type: ignore
        sched_store = app_state.get("schedule_store")
    except Exception:
        sched_store = None
    if sched_store is None:
        return {
            "upcoming": [],
            "count": 0,
            "window_minutes": window_minutes,
            "message": "schedule_store not available",
        }

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(minutes=window_minutes)

    upcoming: list[dict[str, Any]] = []
    try:
        all_schedules = sched_store.list(workspace_id=ctx.workspace_id)
    except Exception:
        all_schedules = []
    for s in all_schedules:
        next_run = (
            s.get("next_run_at") if isinstance(s, dict) else getattr(s, "next_run_at", None)
        )
        if not next_run:
            continue
        # Normalize to datetime
        try:
            nr = next_run if isinstance(next_run, datetime) else datetime.fromisoformat(str(next_run).replace("Z", "+00:00"))
        except Exception:
            continue
        if nr.tzinfo is None:
            nr = nr.replace(tzinfo=timezone.utc)
        if not (now <= nr <= horizon):
            continue
        upcoming.append({
            "schedule_id": s.get("id") if isinstance(s, dict) else getattr(s, "id", ""),
            "workflow_id": s.get("workflow_id") if isinstance(s, dict) else getattr(s, "workflow_id", ""),
            "name": s.get("name") if isinstance(s, dict) else getattr(s, "name", ""),
            "cron": s.get("cron") if isinstance(s, dict) else getattr(s, "cron", ""),
            "next_run_at": nr.isoformat(),
            "minutes_from_now": int((nr - now).total_seconds() // 60),
            "enabled": (s.get("enabled", True) if isinstance(s, dict) else getattr(s, "enabled", True)),
        })

    upcoming.sort(key=lambda x: x["minutes_from_now"])

    return {
        "upcoming": upcoming,
        "count": len(upcoming),
        "window_minutes": window_minutes,
        "message": (
            f"{len(upcoming)} schedule(s) will fire in the next {window_minutes} minute(s)."
            if upcoming else
            f"No schedules are due in the next {window_minutes} minute(s)."
        ),
    }


DEFINITION = ToolDefinition(
    name="get_next_scheduled",
    tier=ToolTier.READ,
    description=(
        "List scheduled pipelines that will fire within a time window (default "
        "60 minutes). Use for 'what runs in the next hour', 'any schedule "
        "conflicts coming up', 'do I have anything overnight'. Returns each "
        "upcoming run's workflow + cron + minutes-until-fire."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "window_minutes": {
                "type": "integer",
                "description": "How far ahead to look (1 to 1440). Defaults to 60.",
                "default": 60,
            },
        },
    },
    output_schema={
        "upcoming": "list",
        "count": "int",
        "window_minutes": "int",
        "message": "str",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["schedule", "upcoming", "monitor"],
)
