"""
GET /api/v1/activity — unified causal-lineage / audit timeline.

Joins three event streams into a single chronological feed so reviewers
can answer "who did what, in what order" without bouncing between three
separate UIs:

    - audit_log         (every authenticated action)
    - agent_traces      (every Copilot run with tool I/O hashes)
    - execution_logs    (every pipeline run with snapshot + parameter_values)

Cycode/Wiz-inspired: the goal is causal tracing — let the buyer follow a
chain like "User triggered agent → agent called list_pipelines → user ran
pipeline X with params Y → it failed at step Z." All three pieces already
exist in the OSS data model; this endpoint just stitches them.

Filters: workspace_id is automatic (workspace-scoped). Optional `since`
(ISO timestamp), `user_id`, `workflow_id`, `kinds` (comma-separated subset
of [audit, agent, execution]).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request

from fpulse.auth.deps import current_workspace_id

router = APIRouter(prefix="/api/v1/activity", tags=["activity"])


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _normalize_ts(v: Any) -> datetime | None:
    """Coerce a datetime / ISO string / None to a tz-aware datetime."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


@router.get("")
def get_activity(
    request: Request,
    since: str | None = None,
    until: str | None = None,
    user_id: str | None = None,
    workflow_id: str | None = None,
    kinds: str = "audit,agent,execution",
    limit: int = 100,
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Unified chronological activity feed for the calling workspace.

    Each event has a stable shape:
        {
          kind: "audit" | "agent" | "execution",
          timestamp: ISO 8601,
          actor: user_id or "system",
          subject: workflow_id / agent run_id / audit resource_id,
          summary: short human-readable line,
          severity: "info" | "warning" | "error",
          details: {...kind-specific fields...}
        }
    """
    limit = max(1, min(int(limit or 100), 500))
    since_dt = _parse_iso(since)
    until_dt = _parse_iso(until)
    if since_dt is None:
        # Default window — last 7 days. Avoids a giant scan on a busy workspace.
        since_dt = datetime.now(timezone.utc) - timedelta(days=7)

    wanted_kinds = {k.strip().lower() for k in (kinds or "").split(",") if k.strip()}
    if not wanted_kinds:
        wanted_kinds = {"audit", "agent", "execution"}

    events: list[dict[str, Any]] = []

    try:
        from fpulse.main import app_state  # type: ignore
    except Exception:
        app_state = None  # type: ignore

    # ── Pipeline executions ────────────────────────────────────────────
    if "execution" in wanted_kinds and app_state:
        log_store = app_state.get("execution_log")
        if log_store is not None:
            try:
                rows = log_store.list_executions(
                    workflow_id=workflow_id, limit=limit, workspace_id=workspace_id,
                )
                for r in rows or []:
                    ts = _normalize_ts(r.get("started_at") or r.get("created_at"))
                    if ts is None or ts < since_dt:
                        continue
                    if until_dt and ts > until_dt:
                        continue
                    status = r.get("status", "unknown")
                    sev = "error" if status == "error" else ("warning" if status == "cancelled" else "info")
                    events.append({
                        "kind": "execution",
                        "timestamp": ts.isoformat(),
                        "actor": r.get("triggered_by", "system"),
                        "subject": r.get("workflow_id") or r.get("workflow_name"),
                        "summary": (
                            f"Pipeline {r.get('workflow_name') or r.get('workflow_id')!r} "
                            f"{status} in {r.get('duration_ms') or 0}ms"
                        ),
                        "severity": sev,
                        "details": {
                            "execution_id": r.get("execution_id") or r.get("id"),
                            "duration_ms": r.get("duration_ms"),
                            "rows_processed": r.get("total_rows_processed"),
                            "steps_failed": r.get("failed_steps"),
                            "error_summary": (r.get("error_summary") or "")[:200] or None,
                        },
                    })
            except Exception:
                # Best-effort — partial activity feed is better than 500.
                pass

    # ── Agent traces ───────────────────────────────────────────────────
    if "agent" in wanted_kinds and app_state:
        trace_store = app_state.get("trace_store")
        if trace_store is not None:
            try:
                rows = trace_store.list_recent(
                    user_id=user_id, workspace_id=workspace_id,
                    limit=limit,
                )
                for r in rows or []:
                    ts = _normalize_ts(r.get("created_at"))
                    if ts is None or ts < since_dt:
                        continue
                    if until_dt and ts > until_dt:
                        continue
                    outcome = r.get("outcome", "success")
                    sev = (
                        "error" if outcome in ("llm_failure", "tool_failure") else
                        "warning" if outcome in ("policy_block", "timeout") else
                        "info"
                    )
                    events.append({
                        "kind": "agent",
                        "timestamp": ts.isoformat(),
                        "actor": r.get("user_id") or "anonymous",
                        "subject": r.get("run_id"),
                        "summary": (
                            f"Agent run {r.get('run_id', '')[:8]}: "
                            f"{r.get('iterations', 0)} iter, "
                            f"{r.get('step_count', 0)} tool calls, outcome={outcome}"
                        ),
                        "severity": sev,
                        "details": {
                            "page": r.get("page"),
                            "elapsed_ms": r.get("elapsed_ms"),
                            "tokens_in": int(r.get("total_tokens_in", 0)),
                            "tokens_out": int(r.get("total_tokens_out", 0)),
                            "tokens": int(r.get("total_tokens_in", 0)) + int(r.get("total_tokens_out", 0)),
                            "model": r.get("model") or None,
                            "provider": r.get("provider") or None,
                            "user_intent": (r.get("user_intent") or "")[:200] or None,
                        },
                    })
            except Exception:
                pass

    # ── Audit log ──────────────────────────────────────────────────────
    if "audit" in wanted_kinds and app_state:
        # We don't have a dedicated audit_log "store" object; the table is
        # written to directly. Use the database handle to read.
        try:
            db = app_state.get("db")
            if db is not None:
                conditions = ["workspace_id = ?", "created_at >= ?"]
                params: list[Any] = [workspace_id, since_dt.isoformat()]
                if until_dt:
                    conditions.append("created_at <= ?")
                    params.append(until_dt.isoformat())
                if user_id:
                    conditions.append("user_id = ?")
                    params.append(user_id)
                if workflow_id:
                    conditions.append("(metadata LIKE ? OR resource_id = ?)")
                    params.append(f"%{workflow_id}%")
                    params.append(workflow_id)
                where = " AND ".join(conditions)
                sql = (
                    f"SELECT user_id, action, resource_type, resource_id, "
                    f"created_at, metadata "
                    f"FROM audit_log WHERE {where} "
                    f"ORDER BY created_at DESC LIMIT ?"
                )
                params.append(limit)
                rows = db.fetchall(sql, tuple(params))
                for r in rows or []:
                    ts = _normalize_ts(r.get("created_at") if isinstance(r, dict) else r["created_at"])
                    if ts is None or ts < since_dt:
                        continue
                    rd = dict(r)
                    action = rd.get("action", "")
                    sev = "warning" if "delete" in action or "reject" in action else "info"
                    events.append({
                        "kind": "audit",
                        "timestamp": ts.isoformat(),
                        "actor": rd.get("user_id") or "system",
                        "subject": rd.get("resource_id") or rd.get("resource_type"),
                        "summary": f"{action} on {rd.get('resource_type', '?')}",
                        "severity": sev,
                        "details": {
                            "resource_type": rd.get("resource_type"),
                            "metadata": (rd.get("metadata") or "")[:300] if isinstance(rd.get("metadata"), str) else None,
                        },
                    })
        except Exception:
            pass

    # Sort newest-first; trim to limit.
    events.sort(key=lambda e: e["timestamp"], reverse=True)
    events = events[:limit]

    counts = {"audit": 0, "agent": 0, "execution": 0}
    severity_counts = {"info": 0, "warning": 0, "error": 0}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        severity_counts[e["severity"]] = severity_counts.get(e["severity"], 0) + 1

    return {
        "events": events,
        "count": len(events),
        "kind_counts": counts,
        "severity_counts": severity_counts,
        "filter": {
            "since": since_dt.isoformat(),
            "until": until_dt.isoformat() if until_dt else None,
            "user_id": user_id,
            "workflow_id": workflow_id,
            "kinds": sorted(wanted_kinds),
        },
    }
