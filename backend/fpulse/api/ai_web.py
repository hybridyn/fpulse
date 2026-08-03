"""Admin API for the Copilot's web-access toggle.

Reads/writes the web-access block of ``admin_settings`` so an operator can turn
the Copilot's web tools (``web_fetch`` / ``web_search``) on or off from
Settings -> AI, and set a search provider + key, without touching env vars or
restarting. The agent re-registers tools per request, so changes are live.

Security: admin-gated. The API key is never returned in cleartext — GET reports
only whether one is set (``has_key``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from fpulse.ai.web import (
    SETTING_API_KEY,
    SETTING_ENABLED,
    SETTING_ENDPOINT,
    SETTING_PROVIDER,
    read_admin_web_settings,
    web_access_enabled,
)
from fpulse.ai.web.search import SUPPORTED_PROVIDERS
from fpulse.auth.deps import require_min_rank

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai/web-access", tags=["ai-web-access"])


def _get_db():
    from fpulse.main import app_state
    return app_state.get("db")


class WebAccessUpdate(BaseModel):
    enabled: bool | None = None
    provider: str | None = None      # "" clears; "brave" | "tavily"
    api_key: str | None = None       # "" clears; omitted = leave unchanged
    endpoint: str | None = None


def _current() -> dict[str, Any]:
    s = read_admin_web_settings()
    key = str(s.get(SETTING_API_KEY) or "")
    return {
        # `enabled` reflects the effective state (env OR setting) so the UI
        # shows ON even when an env var forced it.
        "enabled": web_access_enabled(),
        "setting_enabled": bool(s.get(SETTING_ENABLED, False)),
        "provider": str(s.get(SETTING_PROVIDER) or ""),
        "has_key": bool(key),
        "endpoint": str(s.get(SETTING_ENDPOINT) or ""),
        "supported_providers": list(SUPPORTED_PROVIDERS),
    }


@router.get("", dependencies=[Depends(require_min_rank("admin"))])
async def get_web_access() -> dict[str, Any]:
    """Current web-access config. Never returns the raw key (has_key only)."""
    return _current()


@router.put("", dependencies=[Depends(require_min_rank("admin"))])
async def set_web_access(body: WebAccessUpdate) -> dict[str, Any]:
    """Update the web-access config in admin_settings. Live on next Copilot turn."""
    db = _get_db()
    if db is None:
        raise HTTPException(503, "settings store not initialized")

    if body.provider is not None:
        p = body.provider.strip().lower()
        if p and p not in SUPPORTED_PROVIDERS:
            raise HTTPException(
                400, f"provider must be one of {', '.join(SUPPORTED_PROVIDERS)} (or empty)"
            )

    try:
        row = db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
        existing = json.loads(row["data"]) if row and row["data"] else {}
        if not isinstance(existing, dict):
            existing = {}

        if body.enabled is not None:
            existing[SETTING_ENABLED] = bool(body.enabled)
        if body.provider is not None:
            existing[SETTING_PROVIDER] = body.provider.strip().lower()
        if body.endpoint is not None:
            existing[SETTING_ENDPOINT] = body.endpoint.strip()
        # api_key: only overwrite when the field is present. Empty string
        # clears it; omitting it leaves the stored key untouched.
        if body.api_key is not None:
            existing[SETTING_API_KEY] = body.api_key.strip()

        now_iso = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT OR REPLACE INTO settings (id, data, created_at) VALUES ('admin_settings', ?, ?)",
            (json.dumps(existing), now_iso),
        )
        # The db wrapper's generic execute() does NOT commit, and the
        # connection runs in the default (deferred) transaction mode — so
        # without this the INSERT sits in an uncommitted transaction on the
        # request thread's connection. The same-request read-back in
        # _current() sees it (read-your-own-writes), which is exactly why
        # this looked like it worked; but the change was invisible to other
        # worker threads and rolled back on restart. Commit makes it durable
        # and visible to register_initial_tools()'s per-request reads.
        db.commit()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to persist web-access config")
        raise HTTPException(500, "failed to persist web-access config") from exc

    return _current()
