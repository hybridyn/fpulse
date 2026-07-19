"""Steward → notification bridge.

When the Steward emits a finding the user really should know about, this
module writes a row to the in-app notification system so the
notification bell + email channel pick it up. The eye-icon Steward
badge is the dedicated surface for the full findings view, but the
notification bell is where users *go to find out what changed* — so
P1 escalations and rebounded findings belong there too.

# De-dup rule (the important bit)

Re-running the scan must NOT spam the bell. The Steward re-derives
findings on every `/api/steward/findings` call, so naively writing
"new finding!" on every re-emit would put one notification in the bell
per poll interval per finding.

The rule we enforce:

    For each (user, finding_id) pair, at most ONE notification per
    (severity, rebounded-flag) combination.

That means:
  * First time a P2 duplicate-source appears → 1 notification
  * Re-scans of the same P2 finding → 0 new notifications (dedup hit)
  * Same finding escalates to P1 → 1 new notification (severity changed)
  * Re-scans at P1 → 0 new notifications
  * User resolves + same signature comes back → 1 new "(rebounded)"
    notification (rebounded-flag changed)

We implement this by scanning the user's recent notifications for
matching metadata before writing. SQL-clean — no extra schema, no
extra table, and the de-dup window is naturally bounded by the
notification listing window.

# Why not just write at the API layer

Two reasons:
  1. The same de-dup logic should fire whether the scan was triggered
     by the periodic poll, the manual `/scan` endpoint, or the
     `scan_on_save` event from the frontend. Centralising here is the
     only place all three paths converge.
  2. Future sub-agents (Autopsy, Foreseer) will emit other finding
     kinds. A single bridge module keeps the notification policy in
     one place; each sub-agent just produces `StewardFinding` objects.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from fpulse.notifications.models import Notification
from fpulse.steward.models import FindingKind, FindingSeverity, StewardFinding


logger = logging.getLogger("fpulse.steward.notifier")


_KIND_TITLE_PREFIX: dict[FindingKind, str] = {
    FindingKind.DUPLICATE_SOURCE: "Duplicate source",
    FindingKind.DUPLICATE_PIPELINE: "Duplicate pipeline",
    FindingKind.FAILURE_RCA: "Failure analysis",
    FindingKind.VOLUME_ANOMALY: "Volume anomaly",
    FindingKind.SCHEMA_DRIFT: "Schema drift",
    FindingKind.COST_RECOMMENDATION: "Cost recommendation",
}


def _user_ids_for_workspace(user_store, workspace_id: str) -> Iterable[str]:
    """Mirror the same helper from notifications/run_events.py so the
    Steward notifier follows the exact recipient resolution rules as
    pipeline run notifications. OSS Free → single bootstrap user."""
    try:
        users = user_store.list_users() or []
    except Exception:  # noqa: BLE001 — never break the scan path
        return []
    return [u["id"] for u in users if isinstance(u, dict) and u.get("id")]


def _finding_summary(f: StewardFinding) -> tuple[str, str]:
    """Produce (title, message) suitable for the notification bell.

    We deliberately do NOT reuse the finding's full body — it contains
    multi-paragraph guidance + escalation footnotes that would render
    awkwardly in a one-line bell row. The notification is the
    *attention-grabber*; the Steward dropdown is the place to read the
    full finding."""
    kind_label = _KIND_TITLE_PREFIX.get(f.kind, f.kind.value)
    title = f"{kind_label} — {f.title}"
    workflows = f.evidence.get("workflows") or []
    if workflows:
        names = ", ".join(w.get("name", "") for w in workflows[:3])
        if len(workflows) > 3:
            names += f", +{len(workflows) - 3} more"
        message = f"Affects: {names}."
    else:
        message = "Open the Steward panel for details."
    if f.severity == FindingSeverity.P1:
        message = "[P1] " + message
    return title, message


def _is_rebounded(finding: StewardFinding) -> bool:
    """Mirror the convention in steward/memory.py::apply_learning — a
    rebounded finding has its title prefixed `(rebounded)`. Cheaper
    than re-deriving the rebound condition from memory state."""
    return finding.title.startswith("(rebounded)")


def _existing_notification_matches(
    notification_store,
    user_id: str,
    finding_id: str,
    severity: str,
    rebounded: bool,
) -> bool:
    """De-dup query — has this user already received a notification for
    this exact (finding, severity, rebound-state) tuple?

    We only inspect the latest ~50 notifications. That's intentional:
    if the user has cleared/marked-read 50+ notifications since the
    last emit of this finding, re-surfacing it is the correct
    behaviour (it's old enough they probably forgot).
    """
    try:
        recent = notification_store.list_for_user(user_id, unread_only=False, limit=50)
    except Exception:  # noqa: BLE001
        return False  # On query failure, fail-safe by ALLOWING the create
    for n in recent:
        meta = n.get("metadata") or {}
        if (
            meta.get("source") == "steward"
            and meta.get("finding_id") == finding_id
            and meta.get("severity") == severity
            and bool(meta.get("rebounded")) == rebounded
        ):
            return True
    return False


def emit_steward_notifications(
    *,
    notification_store,
    user_store,
    workspace_id: str,
    findings: list[StewardFinding],
    min_severity: str = "p3",
) -> dict[str, Any]:
    """Write at-most-one notification per (user, finding, severity,
    rebound-state) tuple for every NEW or NEWLY-ESCALATED finding in
    the batch.

    Returns a small summary the caller can log or surface:
    ``{"created": N, "skipped_dedup": M, "skipped_severity": K}``.

    The Steward is a 'nice to have' surface — every error path here
    swallows the exception (with logging) rather than letting a
    notification persistence failure break the scan response.
    """
    if notification_store is None or user_store is None:
        return {"created": 0, "skipped_dedup": 0, "skipped_severity": 0, "skipped_no_store": True}

    sev_rank = {"p1": 3, "p2": 2, "p3": 1}
    min_rank = sev_rank.get(min_severity, 1)

    user_ids = list(_user_ids_for_workspace(user_store, workspace_id))
    if not user_ids:
        return {"created": 0, "skipped_dedup": 0, "skipped_severity": 0, "skipped_no_users": True}

    created = 0
    skipped_dedup = 0
    skipped_severity = 0

    for f in findings:
        if sev_rank.get(f.severity.value, 1) < min_rank:
            skipped_severity += 1
            continue

        rebounded = _is_rebounded(f)
        title, message = _finding_summary(f)
        metadata = {
            "source": "steward",
            "finding_id": f.id,
            "finding_kind": f.kind.value,
            "severity": f.severity.value,
            "rebounded": rebounded,
            "workspace_id": workspace_id,
            "occurrences": f.occurrences,
        }

        ntype = "steward_finding_escalated" if f.severity == FindingSeverity.P1 else "steward_finding"

        for uid in user_ids:
            if _existing_notification_matches(
                notification_store, uid, f.id, f.severity.value, rebounded
            ):
                skipped_dedup += 1
                continue
            try:
                notification_store.create(Notification(
                    user_id=uid,
                    type=ntype,
                    title=title,
                    message=message,
                    # The link is the dashboard for now — clicking the
                    # notification can't auto-open the Steward dropdown
                    # (it's not a routed page). When the dropdown gains
                    # a deep-link URL, swap link_type / link_id.
                    link_type="steward",
                    link_id=f.id,
                    metadata=metadata,
                ))
                created += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("steward notification persist failed for user=%s finding=%s: %s",
                               uid, f.id, exc)

    return {
        "created": created,
        "skipped_dedup": skipped_dedup,
        "skipped_severity": skipped_severity,
    }


def mark_finding_notifications_read(
    *,
    notification_store,
    user_store,
    workspace_id: str,
    finding_id: str,
) -> int:
    """When the user dismisses or resolves a finding, mark every related
    notification as read. Otherwise the bell badge keeps a stale unread
    count for an issue the user has already addressed.

    Returns the count of notifications marked.
    """
    if notification_store is None or user_store is None:
        return 0
    marked = 0
    for uid in _user_ids_for_workspace(user_store, workspace_id):
        try:
            recent = notification_store.list_for_user(uid, unread_only=True, limit=200)
        except Exception:  # noqa: BLE001
            continue
        for n in recent:
            meta = n.get("metadata") or {}
            if meta.get("source") != "steward":
                continue
            if meta.get("finding_id") != finding_id:
                continue
            try:
                if notification_store.mark_read(n.get("id"), uid):
                    marked += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("steward mark_read failed for %s: %s", n.get("id"), exc)
    return marked
