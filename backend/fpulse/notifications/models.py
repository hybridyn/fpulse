"""Notification models — in-app + email notifications for approval workflow."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class Notification(BaseModel):
    """An in-app notification delivered to a specific user.

    Notifications are created by system events (approval workflow, pipeline
    failures, admin actions) and displayed in the frontend notification bell.
    Email delivery is a side-effect that happens at creation time when SMTP
    is configured — the in-app notification is the primary record.
    """
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    user_id: str                  # recipient
    type: str = "info"            # info | approval_request | approved | rejected | deployed | system
    title: str = ""
    message: str = ""
    # Link context — the frontend can navigate to the relevant page
    link_type: str = ""           # workflow | project | admin | approvals
    link_id: str = ""             # e.g. workflow_id
    # Metadata for rich rendering
    metadata: dict[str, Any] = Field(default_factory=dict)
    is_read: bool = False
    email_sent: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
