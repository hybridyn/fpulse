"""Alert and notification models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AlertChannel(str, Enum):
    EMAIL = "email"
    SLACK = "slack"
    TEAMS = "teams"
    AD_GROUP = "ad_group"
    WEBHOOK = "webhook"


class AlertCondition(str, Enum):
    ON_FAILURE = "on_failure"
    ON_SUCCESS = "on_success"
    ON_SLA_BREACH = "on_sla_breach"
    ON_LONG_RUNNING = "on_long_running"
    ON_RESOURCE_THRESHOLD = "on_resource_threshold"
    ON_ANY = "on_any"


class AlertRule(BaseModel):
    """An alert rule for pipeline notifications."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    workflow_id: str | None = None  # None = project-level default
    project_id: str = "default"
    # Tenant boundary — same "global within workspace" rule as
    # connections/credentials. A rule with workflow_id=None fires
    # across every workflow in ONE workspace only, never crosses
    # tenants. Legacy rows back-filled to 'default' by v9.
    workspace_id: str = "default"
    enabled: bool = True

    # What triggers the alert — supports multiple conditions
    condition: AlertCondition = AlertCondition.ON_FAILURE  # kept for backwards compat
    conditions: list[AlertCondition] = Field(default_factory=lambda: [AlertCondition.ON_FAILURE])
    condition_logic: str = "any"  # "any" or "all"
    long_running_threshold_minutes: int = 60
    # Resource thresholds (percent)
    cpu_threshold: int = 90
    memory_threshold: int = 85
    disk_threshold: int = 90

    # Where to send
    channel: AlertChannel = AlertChannel.EMAIL
    # Channel-specific config
    email_addresses: list[str] = Field(default_factory=list)
    slack_webhook_url: str = ""
    teams_webhook_url: str = ""
    ad_group_name: str = ""
    webhook_url: str = ""

    # Template
    custom_message: str = ""

    # Tracking
    last_triggered_at: datetime | None = None
    trigger_count: int = 0

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AlertRuleCreate(BaseModel):
    name: str = ""
    workflow_id: str | None = None
    project_id: str = "default"
    condition: AlertCondition = AlertCondition.ON_FAILURE  # single (backwards compat)
    conditions: list[AlertCondition] = Field(default_factory=list)  # multi-select
    condition_logic: str = "any"  # "any" or "all"
    long_running_threshold_minutes: int = 60
    channel: AlertChannel = AlertChannel.EMAIL
    email_addresses: list[str] = Field(default_factory=list)
    slack_webhook_url: str = ""
    teams_webhook_url: str = ""
    ad_group_name: str = ""
    webhook_url: str = ""
    custom_message: str = ""


class AlertRuleUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    condition: AlertCondition | None = None
    long_running_threshold_minutes: int | None = None
    channel: AlertChannel | None = None
    email_addresses: list[str] | None = None
    slack_webhook_url: str | None = None
    teams_webhook_url: str | None = None
    ad_group_name: str | None = None
    webhook_url: str | None = None
    custom_message: str | None = None


class AlertLog(BaseModel):
    """Record of a triggered alert."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    rule_id: str
    workflow_id: str
    # Inherited from the parent rule at log-write time. A log NEVER
    # changes workspaces after being written, even if the parent rule
    # is deleted — the log is an immutable audit record of what fired.
    workspace_id: str = "default"
    execution_id: str = ""
    channel: AlertChannel
    condition: AlertCondition
    status: str = "sent"  # sent | failed
    message: str = ""
    error: str | None = None
    triggered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
