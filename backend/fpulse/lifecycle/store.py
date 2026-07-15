"""LifecycleToggleStore — SQLite operations on lifecycle_toggle_requests.

Schema is defined in ``storage/database.py`` (v21 migration). This
module is the only place that touches the table.
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


_COLUMNS = (
    "id", "workflow_id", "workflow_version", "workspace_id",
    "action", "target_env", "requested_by", "requested_at", "reason",
    "status", "decided_by", "decided_at", "decision_notes",
)


@dataclass
class LifecycleToggleRequest:
    id: str
    workflow_id: str
    workflow_version: int
    workspace_id: str
    action: str             # 'activate' | 'deactivate'
    target_env: str         # 'prod' for now (DEV is direct toggle)
    requested_by: str
    requested_at: str       # ISO 8601
    reason: str = ""
    status: str = "pending"  # 'pending' | 'approved' | 'rejected'
    decided_by: str | None = None
    decided_at: str | None = None
    decision_notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "workflow_version": self.workflow_version,
            "workspace_id": self.workspace_id,
            "action": self.action,
            "target_env": self.target_env,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "reason": self.reason,
            "status": self.status,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "decision_notes": self.decision_notes,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_req(row) -> LifecycleToggleRequest:
    if isinstance(row, sqlite3.Row):
        d = dict(row)
    else:
        d = dict(zip(_COLUMNS, row))
    return LifecycleToggleRequest(
        id=d["id"],
        workflow_id=d["workflow_id"],
        workflow_version=d["workflow_version"],
        workspace_id=d.get("workspace_id") or "default",
        action=d["action"],
        target_env=d.get("target_env") or "prod",
        requested_by=d["requested_by"],
        requested_at=d["requested_at"],
        reason=d.get("reason") or "",
        status=d.get("status") or "pending",
        decided_by=d.get("decided_by"),
        decided_at=d.get("decided_at"),
        decision_notes=d.get("decision_notes"),
    )


class LifecycleToggleStore:
    """Thin SQLite repo for ``lifecycle_toggle_requests``."""

    def create(
        self,
        conn: sqlite3.Connection,
        *,
        workflow_id: str,
        workflow_version: int,
        workspace_id: str,
        action: str,
        target_env: str,
        requested_by: str,
        reason: str = "",
    ) -> LifecycleToggleRequest:
        if action not in ("activate", "deactivate"):
            raise ValueError(f"Invalid action: {action!r}")
        # Refuse a duplicate pending request for the same workflow + action.
        # Auditor-friendly: one open request at a time.
        existing = conn.execute(
            "SELECT id FROM lifecycle_toggle_requests "
            "WHERE workflow_id = ? AND action = ? AND status = 'pending'",
            (workflow_id, action),
        ).fetchone()
        if existing:
            raise ValueError(
                f"A pending {action} request already exists for this workflow "
                f"(id={existing[0]}). Wait for it to be decided or cancel it."
            )

        req_id = uuid.uuid4().hex
        requested_at = _now_iso()
        conn.execute(
            """
            INSERT INTO lifecycle_toggle_requests (
                id, workflow_id, workflow_version, workspace_id,
                action, target_env, requested_by, requested_at, reason, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                req_id, workflow_id, workflow_version, workspace_id,
                action, target_env, requested_by, requested_at, reason,
            ),
        )
        conn.commit()
        return LifecycleToggleRequest(
            id=req_id,
            workflow_id=workflow_id,
            workflow_version=workflow_version,
            workspace_id=workspace_id,
            action=action,
            target_env=target_env,
            requested_by=requested_by,
            requested_at=requested_at,
            reason=reason,
        )

    def get(self, conn: sqlite3.Connection, req_id: str) -> LifecycleToggleRequest | None:
        cur = conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM lifecycle_toggle_requests WHERE id = ?",
            (req_id,),
        )
        row = cur.fetchone()
        return _row_to_req(row) if row else None

    def list_pending(
        self, conn: sqlite3.Connection,
        *, workspace_id: str | None = None, limit: int = 100,
    ) -> list[LifecycleToggleRequest]:
        if workspace_id:
            cur = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM lifecycle_toggle_requests "
                f"WHERE status = 'pending' AND workspace_id = ? "
                f"ORDER BY requested_at DESC LIMIT ?",
                (workspace_id, limit),
            )
        else:
            cur = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM lifecycle_toggle_requests "
                f"WHERE status = 'pending' ORDER BY requested_at DESC LIMIT ?",
                (limit,),
            )
        return [_row_to_req(r) for r in cur.fetchall()]

    def decide(
        self,
        conn: sqlite3.Connection,
        req_id: str,
        *,
        decision: str,                    # 'approved' | 'rejected'
        decided_by: str,
        decision_notes: str = "",
    ) -> LifecycleToggleRequest:
        if decision not in ("approved", "rejected"):
            raise ValueError(f"Invalid decision: {decision!r}")
        existing = self.get(conn, req_id)
        if not existing:
            raise ValueError(f"Lifecycle request not found: {req_id}")
        if existing.status != "pending":
            raise ValueError(
                f"Request {req_id} is already {existing.status}; cannot re-decide"
            )
        conn.execute(
            """
            UPDATE lifecycle_toggle_requests
            SET status = ?, decided_by = ?, decided_at = ?, decision_notes = ?
            WHERE id = ?
            """,
            (decision, decided_by, _now_iso(), decision_notes, req_id),
        )
        conn.commit()
        return self.get(conn, req_id)  # Return fresh state.
