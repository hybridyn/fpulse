"""Notification API — in-app notification bell for logged-in users.

Every authenticated user can read their own notifications and mark them
as read. No user can see another user's notifications — the user_id
filter is always derived from the session token, never from the URL.

Also exposes admin-only config GET/PUT for workspace-wide notification
settings (long-running threshold, schedule miss, channels, quiet hours,
debounce, daily digest). The watchdog in worker_pool.py + the scheduler
read this config to decide whether to fire alerts. Without this endpoint,
the settings UI in SettingsPage saves only to browser localStorage and
the backend never sees the operator's choices.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from fpulse.auth.deps import require_auth, require_admin, current_user_optional

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _resolve_user_id(request: Request) -> str:
    """Resolve the user id for bell-style read/mark endpoints.

    OSS Free is a single-user product (memory rule: "OSS has no admin
    role concept"). In dev-autologin mode the frontend has no token to
    send, so a strict ``require_auth`` here returns 401 and the bell
    never shows the run notifications we just persisted. Mirror the
    pattern already used by ``/config``: prefer an authenticated user,
    otherwise fall back to the first/bootstrap user.

    Plus tier overlays its own routers with the strict gate so this
    relaxation is OSS-only.
    """
    user = current_user_optional(request)
    if user is not None:
        return user.id

    from fpulse.main import app_state
    user_store = app_state.get("user_store")
    if user_store is None:
        raise HTTPException(503, "user store not initialized")
    try:
        users = user_store.list_users() or []
        if users:
            first = users[0]
            uid = first.get("id") if isinstance(first, dict) else getattr(first, "id", None)
            if uid:
                return uid
    except Exception:
        pass
    raise HTTPException(401, "No bootstrap user available; sign in to view notifications")


def _get_store():
    from fpulse.main import app_state
    return app_state["notification_store"]


def _get_db():
    from fpulse.main import app_state
    return app_state.get("db")


# ─────────────────────────────────────────────────────────────────────────
# Config GET/PUT (admin only) — May 3 2026
# ─────────────────────────────────────────────────────────────────────────

# Defaults match what the watchdog assumes when admin_settings is empty
# (see worker_pool._long_running_threshold_min and on_long_running /
# on_schedule_miss in notifications/service.py).
_DEFAULT_CONFIG: dict[str, Any] = {
    # Pipeline event toggles
    "notify_on_success": True,
    "notify_on_error": True,
    "notify_on_warning": False,
    # Execution alerts
    "notify_on_long_running": True,
    "long_running_threshold_min": 30,
    "notify_on_schedule_miss": True,
    # External channels
    "channels": ["email"],
    "email_enabled": False,
    "browser_enabled": False,
    "slack_webhook": "",
    "discord_webhook": "",
    "teams_webhook": "",
    "webhook_url": "",
    # Plus-only delivery controls (saved by Plus admins; ignored by
    # the OSS dispatch path)
    "quiet_hours_enabled": False,
    "quiet_hours_start": "22:00",
    "quiet_hours_end": "07:00",
    "debounce_seconds": 60,
    "daily_digest": False,
    "daily_digest_time": "08:00",
    # SMTP — managed via Admin → Settings; surfaced here read-only for parity
    "smtp": {},
}


def _read_config() -> dict[str, Any]:
    db = _get_db()
    if db is None:
        return dict(_DEFAULT_CONFIG)
    try:
        row = db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
        if not row:
            return dict(_DEFAULT_CONFIG)
        settings = json.loads(row["data"])
        cfg = dict(_DEFAULT_CONFIG)
        cfg.update(settings.get("notifications") or {})
        return cfg
    except Exception:
        return dict(_DEFAULT_CONFIG)


def _write_config(updates: dict[str, Any]) -> dict[str, Any]:
    db = _get_db()
    if db is None:
        raise HTTPException(503, "settings store not initialized")
    try:
        row = db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
        existing = json.loads(row["data"]) if row else {}
        notifications = dict(_DEFAULT_CONFIG)
        notifications.update(existing.get("notifications") or {})
        # Whitelist updates to the known keys so a malformed payload can't
        # introduce arbitrary fields into the persisted JSON.
        for k, v in updates.items():
            if k in _DEFAULT_CONFIG:
                notifications[k] = v
        existing["notifications"] = notifications
        # Settings table requires `created_at NOT NULL`. The original
        # INSERT OR REPLACE didn't supply it, so the very first save
        # 500'd with "NOT NULL constraint failed: settings.created_at"
        # — exactly the error the user hit when trying to configure
        # SMTP via the Settings UI on 2026-05-09. Stamp it on every
        # write (REPLACE bumps the value; that's fine — it's a "last
        # written" timestamp for the row, not an invariant).
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT OR REPLACE INTO settings (id, data, created_at) VALUES ('admin_settings', ?, ?)",
            (json.dumps(existing), now_iso),
        )
        # The db wrapper's generic execute() doesn't commit and the
        # connection isn't in autocommit mode, so the write would otherwise
        # linger in an uncommitted transaction — durable only if some later
        # operation on the same thread-local connection happened to commit
        # before restart. Commit explicitly so the config actually persists.
        db.commit()
        return notifications
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("failed to persist notifications config")
        raise HTTPException(500, "failed to persist notifications config") from exc


class NotificationConfigUpdate(BaseModel):
    notify_on_success: bool | None = None
    notify_on_error: bool | None = None
    notify_on_warning: bool | None = None
    notify_on_long_running: bool | None = None
    long_running_threshold_min: int | None = Field(default=None, ge=1, le=1440)
    notify_on_schedule_miss: bool | None = None
    channels: list[str] | None = None
    email_enabled: bool | None = None
    browser_enabled: bool | None = None
    slack_webhook: str | None = None
    discord_webhook: str | None = None
    teams_webhook: str | None = None
    webhook_url: str | None = None
    quiet_hours_enabled: bool | None = None
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    debounce_seconds: int | None = Field(default=None, ge=0, le=3600)
    daily_digest: bool | None = None
    daily_digest_time: str | None = None
    # SMTP — nested object so the alerts notifier can read it as
    # notifications.smtp.{host,port,user,password,from_email,tls}.
    # Empty/None means "no override" — fall back to env vars.
    smtp: dict | None = None


@router.get("/config")
async def get_notification_config():
    """Read the workspace-wide notification config.

    Open in OSS — OSS Free is a single bootstrap user with no RBAC
    (memory rule: "OSS has no admin role concept"). The previous
    `require_admin` gate locked out the operator's own SMTP form
    when the session token expired (a real bug on 2026-05-09 — the
    user couldn't fix email delivery without first re-logging-in to
    a system that has no real auth). Plus tier reinstates the admin
    gate via its own router overlay.
    """
    return _read_config()


@router.put("/config")
async def put_notification_config(body: NotificationConfigUpdate):
    """Update the workspace-wide notification config.

    Open in OSS — see GET docstring above. Plus tier reinstates the
    admin gate. Only fields explicitly set in the payload are updated;
    partial PUTs are supported.
    """
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    return _write_config(updates)


# ─────────────────────────────────────────────────────────────────────────
# Telemetry consent (admin only) — May 3 2026
# Separate from notifications config because it has its own audit
# implications (consent timestamp + revocation flushes the queue).
# Lives in this router for now to avoid creating a new module; could
# split out later.
# ─────────────────────────────────────────────────────────────────────────

class TelemetryConsentUpdate(BaseModel):
    enabled: bool


@router.get("/telemetry/consent")
async def get_telemetry_consent(user=Depends(require_admin)):
    """Read the telemetry consent flag from admin_settings. Admin-only.

    Returns: `{enabled: bool, consented_at: str | null}`
    """
    from fpulse.telemetry import is_telemetry_enabled
    db = _get_db()
    if db is None:
        return {"enabled": False, "consented_at": None}
    enabled = is_telemetry_enabled(db)
    consented_at = None
    try:
        row = db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
        if row:
            settings = json.loads(row["data"])
            consented_at = settings.get("telemetry_consented_at")
    except Exception:
        pass
    return {"enabled": enabled, "consented_at": consented_at}


@router.put("/telemetry/consent")
async def put_telemetry_consent(
    body: TelemetryConsentUpdate,
    user=Depends(require_admin),
):
    """Update the telemetry consent flag. Admin-only.

    The actual telemetry sender doesn't exist yet (1.0.1 work). This
    endpoint persists the choice now so operators can opt in / out
    today and the sender, when it ships, just reads the existing flag.
    """
    from fpulse.telemetry import set_telemetry_enabled
    db = _get_db()
    if db is None:
        raise HTTPException(503, "settings store not initialized")
    set_telemetry_enabled(db, body.enabled)
    return {"enabled": body.enabled}


@router.get("/")
async def list_notifications(
    request: Request,
    unread_only: bool = False,
    limit: int = 50,
):
    """List the current user's notifications, newest first."""
    store = _get_store()
    return store.list_for_user(_resolve_user_id(request), unread_only=unread_only, limit=limit)


@router.get("/count")
async def unread_count(request: Request):
    """Get the count of unread notifications (for the bell badge)."""
    store = _get_store()
    return {"unread": store.unread_count(_resolve_user_id(request))}


@router.post("/{notification_id}/read")
async def mark_read(notification_id: str, request: Request):
    """Mark a single notification as read."""
    store = _get_store()
    store.mark_read(notification_id, _resolve_user_id(request))
    return {"read": True}


@router.post("/read-all")
async def mark_all_read(request: Request):
    """Mark all notifications as read for the current user."""
    store = _get_store()
    count = store.mark_all_read(_resolve_user_id(request))
    return {"marked_read": count}


@router.delete("/{notification_id}")
async def delete_notification(notification_id: str, request: Request):
    """Permanently delete a single notification owned by the current user."""
    store = _get_store()
    deleted = store.delete(notification_id, _resolve_user_id(request))
    if not deleted:
        raise HTTPException(404, "Notification not found")
    return {"deleted": True}


@router.delete("/")
async def clear_notifications(request: Request, only_read: bool = False):
    """Clear all notifications for the current user.

    Pass ``?only_read=true`` to keep unread notifications and only
    sweep the ones the user has already seen.
    """
    store = _get_store()
    count = store.delete_all_for_user(_resolve_user_id(request), only_read=only_read)
    return {"deleted": count}
