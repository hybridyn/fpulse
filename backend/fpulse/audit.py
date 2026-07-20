"""Audit log for destructive actions (P0 Day 5, 2026-05-23).

Centralized helper that every destructive endpoint calls before
mutating state. Records WHO did WHAT to WHICH resource, with enough
detail to reconstruct the action's intent for a security review.

Scope in OSS v1.0:
  * Logs to the standard structured logger with an ``AUDIT:`` prefix
    so log scrapers + journalctl filters can find them deterministically.
  * Permission checks live on each endpoint (workspace_id dep). OSS
    runs as a single bootstrap user so the check is trivially-always-
    allow, but the plumbing is here so Plus's RBAC layer can extend
    it without rewriting every callsite.

Plus override point: replace ``audit_action`` with a version that
inserts into a persistent ``audit_log`` table. The OSS implementation
intentionally does not create a new SQLite table — that's a v1.x
migration we don't need yet, and the logger output is searchable
enough for OSS operators today.

Action vocabulary — keep these strings stable; downstream tools key
on them:

  storage.file.delete         (soft-delete file → trash)
  storage.file.replace        (replace bytes in place)
  storage.table.drop          (drop managed table — hard delete)
  storage.table.rename        (rename schema/name)
  storage.cleanup             (purge trash + old outputs)
  workflow.delete             (delete a workflow row)
  workflow.archive            (archive a workflow)
  workflow.deploy             (deploy DEV → PROD)
  workflow.rollback           (rollback a deployment)
  connection.delete           (delete a saved connection)
  credential.delete           (delete a credential)

Each event carries:
  * action          — one of the strings above
  * resource_type   — file / table / workflow / connection / credential / pipeline_outputs
  * resource_id     — the row id, or a (schema, name) tuple for tables
  * actor           — bootstrap user id in OSS; real user id under Plus RBAC
  * workspace_id    — always set, scoped to the caller's workspace
  * details         — free-form dict, e.g. {old_name, new_name} for rename
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("fpulse.audit")


def audit_action(
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    actor: str = "system",
    workspace_id: str = "default",
    details: dict[str, Any] | None = None,
) -> None:
    """Record a destructive action.

    Best-effort: never raises. A logging failure must not block the
    action it was logging — the caller has already gone past its
    permission gate at this point, and refusing the action because we
    couldn't log it would be a worse outcome.

    OSS impl writes one structured INFO line:

        AUDIT: <action> resource=<type>:<id> actor=<actor>
               workspace=<ws> details=<json>

    Plus monkeypatches this function to ALSO persist to its audit_log
    table. The callsites don't need to know which mode they're in.
    """
    try:
        ts = datetime.now(timezone.utc).isoformat()
        details_str = json.dumps(details or {}, default=str, sort_keys=True)
        logger.info(
            "AUDIT: %s resource=%s:%s actor=%s workspace=%s ts=%s details=%s",
            action,
            resource_type,
            resource_id,
            actor,
            workspace_id,
            ts,
            details_str,
        )
    except Exception:
        # Never raise — the destructive action already happened.
        # A debug-level log of the failure is enough.
        logger.debug("audit_action: log emission failed", exc_info=True)


def actor_for(user: Any) -> str:
    """Resolve a request's user object to an audit actor id.

    Helper for endpoints that already have a ``user`` dep injected.
    Returns ``"system"`` if no user is available (CLI / service paths).
    """
    if user is None:
        return "system"
    return (
        getattr(user, "id", None)
        or getattr(user, "username", None)
        or getattr(user, "email", None)
        or "system"
    )


__all__ = ["audit_action", "actor_for"]
