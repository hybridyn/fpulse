"""Pipeline run → in-app notification emitter.

Called from the manual-run path (api/execution.py) and the scheduled-run
path (scheduling/scheduler.py) after a workflow completes. Persists one
Notification per user in the workspace so the bell badge + Notifications
page reflect every run outcome — without requiring SMTP, alert rules, or
any other external setup.

Why this exists separately from `alerts/notifier.py`:
  * Alerts are user-configured (rules + channels + email/Slack targets).
  * In-app run notifications are unconditional ambient signal — every
    user always gets the bell badge for runs in their workspace.

Best-effort: failures here never bubble back into the run path.
"""

from __future__ import annotations

import logging
from typing import Iterable

from fpulse.notifications.models import Notification

logger = logging.getLogger("fpulse.notifications.run_events")


def _user_ids_for_workspace(user_store, workspace_id: str) -> Iterable[str]:
    """Resolve which users should see this run in their bell.

    OSS Free has a single bootstrap user → list_users() returns one row
    and we notify them. Plus tier overlays a workspace-aware list.
    """
    try:
        users = user_store.list_users() or []
    except Exception:
        return []
    return [u["id"] for u in users if isinstance(u, dict) and u.get("id")]


def emit_run_notification(
    *,
    notification_store,
    user_store,
    workflow_id: str,
    workflow_name: str,
    execution_id: str,
    status: str,
    workspace_id: str = "default",
    triggered_by: str = "manual",
    error_message: str = "",
    failed_step: str = "",
    duration_ms: int | None = None,
) -> None:
    """Persist a Notification per user describing a pipeline run outcome.

    The notification's link_type / link_id are set so clicking the row
    on the Notifications page deep-links into the Execution Summary
    panel on the Steps tab (see frontend/src/lib/notificationHref.ts).

    Suppresses the call entirely on intermediate states so we don't
    spam the bell — only terminal outcomes (success / error / failed)
    write a row.
    """
    s = (status or "").lower()
    if s not in ("success", "error", "failed"):
        return

    if notification_store is None or user_store is None:
        return

    if s == "success":
        ntype = "run_succeeded"
        title = f"{workflow_name or 'Pipeline'} succeeded"
        if duration_ms:
            message = f"Completed in {duration_ms / 1000:.1f}s ({triggered_by})."
        else:
            message = f"Completed via {triggered_by} run."
    else:
        ntype = "run_failed"
        title = f"{workflow_name or 'Pipeline'} failed"
        bits: list[str] = []
        if failed_step:
            bits.append(f"step '{failed_step}'")
        if error_message:
            short = (error_message or "").strip().splitlines()[0][:160]
            if short:
                bits.append(short)
        message = " — ".join(bits) if bits else f"Failed during {triggered_by} run."

    user_ids = list(_user_ids_for_workspace(user_store, workspace_id))
    if not user_ids:
        logger.debug("emit_run_notification: no users to notify for ws=%s", workspace_id)
        return

    metadata = {
        "workflow_id": workflow_id,
        "workflow_name": workflow_name,
        "execution_id": execution_id,
        "status": s,
        "triggered_by": triggered_by,
        "workspace_id": workspace_id,
    }
    if duration_ms is not None:
        metadata["duration_ms"] = duration_ms
    if failed_step:
        metadata["failed_step"] = failed_step

    for uid in user_ids:
        try:
            notification_store.create(Notification(
                user_id=uid,
                type=ntype,
                title=title,
                message=message,
                link_type="executions",
                link_id=execution_id,
                metadata=metadata,
            ))
        except Exception as exc:
            logger.warning("emit_run_notification persist failed for %s: %s", uid, exc)


def emit_lifecycle_notification(
    *,
    notification_store,
    user_store,
    workflow_id: str,
    workflow_name: str,
    event: str,                # 'published' | 'revoked'
    actor: str = "user",
    workspace_id: str = "default",
) -> None:
    """Persist a notification when a pipeline's lifecycle state changes.

    Fires for user-initiated transitions (Publish, Revoke) — these are
    low-volume, deliberate actions that warrant a row in the bell so a
    second user in the workspace sees what just happened. Run-start /
    run-completion live in ``emit_run_notification`` since they have
    very different volume characteristics.
    """
    if notification_store is None or user_store is None:
        return

    e = (event or "").lower()
    if e == "published":
        ntype = "pipeline_published"
        title = f"{workflow_name or 'Pipeline'} published"
        message = f"{actor} published this pipeline. It can now be scheduled."
    elif e == "revoked":
        ntype = "pipeline_revoked"
        title = f"{workflow_name or 'Pipeline'} revoked"
        message = f"{actor} revoked this pipeline back to draft. Schedules will not fire."
    else:
        return

    user_ids = list(_user_ids_for_workspace(user_store, workspace_id))
    if not user_ids:
        return

    metadata = {
        "workflow_id": workflow_id,
        "workflow_name": workflow_name,
        "event": e,
        "actor": actor,
        "workspace_id": workspace_id,
    }
    for uid in user_ids:
        try:
            notification_store.create(Notification(
                user_id=uid,
                type=ntype,
                title=title,
                message=message,
                link_type="workflow",
                link_id=workflow_id,
                metadata=metadata,
            ))
        except Exception as exc:
            logger.warning("emit_lifecycle_notification persist failed for %s: %s", uid, exc)
