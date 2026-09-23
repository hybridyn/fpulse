"""Telemetry consent — admin-settings flag + payload schema documentation.

This module deliberately does NOT implement the sender. We're shipping the
opt-in UI + persistence in 1.0.0 so operators choose their posture before
the actual sender goes live in 1.0.1. Until then, `is_telemetry_enabled`
is checked at every hypothetical send site but no network call happens.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("fpulse.telemetry")

# Payload schema documented for the /trust page. This is the EXACT shape
# that goes over the wire when telemetry is enabled. If we ever change it,
# bump the version and notify every opted-in operator.
TELEMETRY_PAYLOAD_SCHEMA = {
    "version": 1,
    "fields": {
        "event_type": "string — 'crash' | 'startup' | 'shutdown'",
        "fpulse_version": "string — e.g. '1.0.0'",
        "python_version": "string — e.g. '3.11.7'",
        "os_family": "string — 'Linux' | 'Darwin' | 'Windows' (no kernel version, no distro)",
        "feature_flags": "object — {marketplace: bool, lineage: bool, ...}",
        "exception_type": "string — fully-qualified type name, crash events only",
        "stack_trace": "string — sanitized: file paths within fpulse/* only, no /home/, no env vars",
        "installation_id": "string — random 16-byte UUID generated on first opt-in; can be regenerated",
    },
    "never_sent": [
        "row data, query results, intermediate datasets",
        "SQL text, configuration values, environment variables",
        "connection strings, credentials, API keys",
        "user file paths, user IDs, workspace names, project names",
        "agent prompts, agent responses, LLM provider details",
    ],
}


def is_telemetry_enabled(db: Any) -> bool:
    """Read the consent flag from admin_settings. Default False on missing/error."""
    try:
        row = db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
        if not row:
            return False
        settings = json.loads(row["data"])
        return bool(settings.get("telemetry_enabled", False))
    except Exception as exc:
        logger.debug("telemetry consent read failed (default off): %s", exc)
        return False


def set_telemetry_enabled(db: Any, enabled: bool) -> None:
    """Persist the consent flag. Idempotent."""
    try:
        row = db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
        settings = json.loads(row["data"]) if row else {}
        if bool(settings.get("telemetry_enabled", False)) == bool(enabled):
            return
        settings["telemetry_enabled"] = bool(enabled)
        if enabled and "telemetry_consented_at" not in settings:
            from datetime import datetime, timezone
            settings["telemetry_consented_at"] = datetime.now(timezone.utc).isoformat()
        if not enabled:
            settings.pop("telemetry_consented_at", None)
        # settings.created_at is NOT NULL with no default; INSERT OR REPLACE
        # deletes+reinserts the row, so a payload that omits created_at aborts
        # with "NOT NULL constraint failed: settings.created_at" (the same
        # failure notifications._write_config was already fixed for). Stamp it.
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT OR REPLACE INTO settings (id, data, created_at) VALUES ('admin_settings', ?, ?)",
            (json.dumps(settings), now_iso),
        )
        # Generic execute() doesn't commit and the connection isn't in
        # autocommit mode — commit so the consent flag survives restart.
        db.commit()
        logger.info("telemetry %s by admin", "enabled" if enabled else "disabled")
    except Exception as exc:
        logger.warning("telemetry consent write failed: %s", exc)
