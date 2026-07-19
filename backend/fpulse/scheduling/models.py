"""Pipeline scheduling models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ScheduleType(str, Enum):
    CRON = "cron"
    INTERVAL = "interval"
    DAILY = "daily"
    WEEKLY = "weekly"
    EVENT = "event"


class Schedule(BaseModel):
    """A pipeline schedule definition."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    workflow_id: str
    project_id: str = "default"
    # Tenant boundary — a schedule always belongs to the workspace of
    # the workflow it triggers. The background scheduler loop iterates
    # every workspace (system-level), but every user-facing API call
    # filters by the caller's workspace_id. Legacy rows back-filled to
    # 'default' by v8.
    workspace_id: str = "default"
    name: str = ""
    schedule_type: ScheduleType = ScheduleType.CRON
    enabled: bool = True

    # Cron
    cron_expression: str = ""  # e.g., "0 */6 * * *"

    # Interval
    interval_minutes: int = 60

    # Daily
    daily_time: str = "00:00"  # HH:MM

    # Weekly
    weekly_days: list[int] = Field(default_factory=list)  # 0=Mon..6=Sun
    weekly_time: str = "00:00"

    # Event
    event_trigger: str = ""  # e.g., "file_uploaded", "pipeline_completed"
    event_source_id: str = ""

    # Timezone
    timezone: str = "UTC"

    # Pipeline parameter values — when the scheduler fires this row, the
    # values here are passed to the executor. System placeholders like
    # ${utcnow:%Y-%m-%d} are resolved at fire time, not at schedule-create
    # time, so a daily schedule actually gets today's date every run.
    parameter_values: dict[str, Any] = Field(default_factory=dict)

    # Tracking
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    next_run_at: datetime | None = None
    run_count: int = 0

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ScheduleCreate(BaseModel):
    workflow_id: str
    project_id: str = "default"
    name: str = ""
    schedule_type: ScheduleType = ScheduleType.CRON
    enabled: bool = True
    cron_expression: str = ""
    interval_minutes: int = 60
    daily_time: str = "00:00"
    weekly_days: list[int] = Field(default_factory=list)
    weekly_time: str = "00:00"
    event_trigger: str = ""
    event_source_id: str = ""
    timezone: str = "UTC"
    parameter_values: dict[str, Any] = Field(default_factory=dict)
    # 2026-05-26 — Required when the pipeline contains an append_risky
    # or external sink. Scheduled pipelines re-run on a cadence, so an
    # unsafe sink will multiply rows / re-fire side effects on every
    # tick. The API returns 400 with `code: unsafe_for_schedule` and
    # the offending sink list unless this flag is set. Mirrors the
    # `acknowledge_side_effects` guardrail on backfills.
    acknowledge_side_effects: bool = False


class ScheduleUpdate(BaseModel):
    name: str | None = None
    schedule_type: ScheduleType | None = None
    enabled: bool | None = None
    cron_expression: str | None = None
    interval_minutes: int | None = None
    daily_time: str | None = None
    weekly_days: list[int] | None = None
    weekly_time: str | None = None
    event_trigger: str | None = None
    event_source_id: str | None = None
    timezone: str | None = None
    parameter_values: dict[str, Any] | None = None
