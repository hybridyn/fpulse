"""Publish-time documentation policy (admin-configurable).

The publish gate requires a pipeline to carry a ``business_purpose`` before
it can go live (see ``api/workflows.py`` ``publish_workflow``). This module
owns the one escape hatch: an operator can relax that requirement org-wide
from Settings -> Publishing, without a restart.

The switch is deliberately **instance-level, not per-pipeline** — a
per-pipeline "skip docs" checkbox would be dismissed on every pipeline and
the requirement would mean nothing. As a single admin policy (default ON),
"every published pipeline states its purpose" stays a real guarantee, while
an org whose culture doesn't want it can turn it off in one place.

Reads mirror the ``ai_web`` pattern: env var wins (headless / air-gap
bootstrap), then the admin setting, then the default (ON).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from fpulse.auth.deps import require_min_rank

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/publish-policy", tags=["publish-policy"])

# admin_settings key + env override.
SETTING_REQUIRE_PURPOSE = "require_pipeline_purpose"
REQUIRE_PURPOSE_ENV = "FPULSE_REQUIRE_PIPELINE_PURPOSE"


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _get_db():
    from fpulse.main import app_state
    return app_state.get("db")


def _read_admin_settings() -> dict:
    """The admin_settings blob ({} on any failure — defensive)."""
    try:
        db = _get_db()
        if not db:
            return {}
        row = db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
        if not row or not row["data"]:
            return {}
        data = json.loads(row["data"])
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def require_pipeline_purpose() -> bool:
    """True iff a ``business_purpose`` is required before publishing.

    Default **True** (feature on). Resolution order:
      1. ``FPULSE_REQUIRE_PIPELINE_PURPOSE`` env var, if set (pins the policy
         for a headless / air-gap deploy).
      2. The ``require_pipeline_purpose`` admin setting, if present.
      3. Default True.
    """
    env = os.environ.get(REQUIRE_PURPOSE_ENV, "")
    if env.strip() != "":
        return _truthy(env)
    val = _read_admin_settings().get(SETTING_REQUIRE_PURPOSE, True)
    if isinstance(val, bool):
        return val
    return _truthy(val)


class PublishPolicyUpdate(BaseModel):
    require_business_purpose: bool


def _current() -> dict[str, Any]:
    # `enabled` is the effective policy (env OR setting); `setting_enabled`
    # is just what's stored, so the UI can tell when an env var forced it.
    stored = _read_admin_settings().get(SETTING_REQUIRE_PURPOSE, True)
    return {
        "require_business_purpose": require_pipeline_purpose(),
        "setting_value": bool(stored) if isinstance(stored, bool) else _truthy(stored),
        "env_override": os.environ.get(REQUIRE_PURPOSE_ENV, "").strip() != "",
    }


@router.get("", dependencies=[Depends(require_min_rank("admin"))])
async def get_publish_policy() -> dict[str, Any]:
    """Current publish-documentation policy."""
    return _current()


@router.put("", dependencies=[Depends(require_min_rank("admin"))])
async def set_publish_policy(body: PublishPolicyUpdate) -> dict[str, Any]:
    """Enable/disable the required-business-purpose publish gate, org-wide."""
    db = _get_db()
    if db is None:
        raise HTTPException(503, "settings store not initialized")
    try:
        row = db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
        existing = json.loads(row["data"]) if row and row["data"] else {}
        if not isinstance(existing, dict):
            existing = {}
        existing[SETTING_REQUIRE_PURPOSE] = bool(body.require_business_purpose)
        now_iso = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT OR REPLACE INTO settings (id, data, created_at) VALUES ('admin_settings', ?, ?)",
            (json.dumps(existing), now_iso),
        )
        # Generic execute() doesn't commit and the connection is in deferred-
        # transaction mode; without this the write rolls back on restart (see
        # the same footgun fixed in ai_web.py).
        db.commit()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to persist publish policy")
        raise HTTPException(500, "failed to persist publish policy") from exc
    return _current()
