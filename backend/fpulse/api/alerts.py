"""Alert rules and notification API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from fpulse.auth.deps import current_workspace_id, require_auth, require_min_rank
from fpulse.alerts.models import AlertRule, AlertRuleCreate, AlertRuleUpdate, AlertLog

# 2026-05-30 (Track S P1): router-level require_auth; write endpoints
# (create/update/delete alert rules) gate at data_engineer+.
_AUTH = Depends(require_auth)
_AUTHOR = Depends(require_min_rank("developer"))

router = APIRouter(
    prefix="/api/alerts",
    tags=["alerts"],
    dependencies=[_AUTH],
)


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


def get_store():
    from fpulse.main import app_state
    return app_state["alert_store"]


def _resolve_workflow_in_workspace(workflow_id: str, workspace_id: str):
    """Return the workflow if it lives in this workspace, else None.

    2026-05-22: mirrors ``schedules.py:_workflow_in_workspace`` but
    returns the workflow object so the caller can derive ``project_id``
    server-side (matching the audit's recommendation in
    docs/PROJECT_PIPELINE_CONFIGURATION_VALIDATION.md finding A4).
    Previously alert create stamped only workspace_id and trusted the
    client-supplied workflow_id / project_id — a caller in workspace A
    could attach an alert to a workflow in workspace B.
    """
    try:
        from fpulse.main import app_state
        workflow_store = app_state["store"]
        v = workflow_store.get(workflow_id, workspace_id=workspace_id)
        return v.workflow if v else None
    except Exception:
        return None


def _project_in_workspace(project_id: str, workspace_id: str) -> bool:
    """Return True if project_id is in the caller's workspace.

    Used when an alert rule is attached to a project (not a single
    workflow). Defensive: missing store → False, conservative reject.
    """
    try:
        from fpulse.main import app_state
        project_store = app_state.get("project_store")
        if project_store is None:
            return True  # Best-effort: don't block alerts on a missing store
        proj = project_store.get(project_id)
        if proj is None:
            return False
        proj_ws = getattr(proj, "workspace_id", None) or "default"
        return proj_ws == workspace_id
    except Exception:
        return False


@router.get("/rules")
async def list_rules(
    workflow_id: str | None = None,
    project_id: str | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    store = get_store()
    if workflow_id:
        return store.list_rules_by_workflow(workflow_id, workspace_id=workspace_id)
    if project_id:
        return store.list_rules_by_project(project_id, workspace_id=workspace_id)
    return store.list_rules(workspace_id=workspace_id)


@router.post("/rules", dependencies=[_AUTHOR])
async def create_rule(
    body: AlertRuleCreate,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create an alert rule in the caller's workspace.

    Validates that any referenced workflow_id / project_id actually
    belongs to the caller's workspace (matches schedules.py). If a
    workflow_id is given, project_id is overridden with the workflow's
    own project so the alert can't be silently mis-attributed.
    """
    store = get_store()
    data = body.model_dump()
    # If conditions list is empty, populate from single condition field
    if not data.get("conditions"):
        data["conditions"] = [data["condition"]]
    # Keep condition field in sync (first condition)
    if data["conditions"]:
        data["condition"] = data["conditions"][0]

    # ── Ownership validation (2026-05-22, audit A4) ─────────────────────
    raw_workflow_id = (data.get("workflow_id") or "").strip()
    raw_project_id = (data.get("project_id") or "").strip()
    if raw_workflow_id:
        wf = _resolve_workflow_in_workspace(raw_workflow_id, workspace_id)
        if wf is None:
            raise HTTPException(404, f"Workflow '{raw_workflow_id}' not found in this workspace")
        # Derive project_id from the workflow — the client doesn't get
        # to attach an alert to a different project than the workflow's
        # own. If they explicitly passed a mismatching project_id, the
        # workflow wins (server is the source of truth).
        data["project_id"] = getattr(wf, "project_id", "default") or "default"
    elif raw_project_id:
        if not _project_in_workspace(raw_project_id, workspace_id):
            raise HTTPException(404, f"Project '{raw_project_id}' not found in this workspace")

    data["workspace_id"] = workspace_id
    rule = AlertRule(**data)
    created = store.create_rule(rule)
    return created.model_dump(mode="json")


@router.put("/rules/by-workflow/{workflow_id}/default", dependencies=[_AUTHOR])
async def upsert_default_alert(
    workflow_id: str,
    body: AlertRuleCreate,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Idempotent upsert of the "default alert rule" for a workflow.

    2026-05-22 (audit D3): SaveDialog used to POST /alerts every save,
    piling up duplicate alert rules. This gives the dialog a single
    upsert call mirroring /schedules/by-workflow/.../default.

    Same semantics as the schedule version:
      * No rule for this workflow → create.
      * One or more rules → update the oldest in place.
      * Manually-added secondary rules are not touched.

    project_id is derived from the workflow server-side (matching the
    create_rule rules in audit A4) — the client doesn't get to pick.
    """
    wf = _resolve_workflow_in_workspace(workflow_id, workspace_id)
    if wf is None:
        raise HTTPException(404, "Workflow not found")

    store = get_store()
    existing = store.list_rules_by_workflow(workflow_id, workspace_id=workspace_id)

    data = body.model_dump()
    if not data.get("conditions"):
        data["conditions"] = [data["condition"]]
    if data["conditions"]:
        data["condition"] = data["conditions"][0]
    data["workspace_id"] = workspace_id
    data["workflow_id"] = workflow_id
    data["project_id"] = getattr(wf, "project_id", "default") or "default"

    if existing:
        try:
            target = sorted(
                existing,
                key=lambda r: (r.get("created_at") if isinstance(r, dict)
                               else getattr(r, "created_at", None)) or "",
            )[0]
        except Exception:
            target = existing[0]
        target_id = (target.get("id") if isinstance(target, dict)
                     else getattr(target, "id", None))
        if not target_id:
            raise HTTPException(500, "Could not resolve default alert rule id")
        # Drop the read-only fields from the upsert payload so we
        # don't try to overwrite ids / timestamps via the update path.
        updates = {k: v for k, v in data.items() if k not in ("id", "created_at")}
        updated = store.update_rule(target_id, updates, workspace_id=workspace_id)
        if not updated:
            raise HTTPException(404, "Alert rule not found")
        return updated.model_dump(mode="json")

    rule = AlertRule(**data)
    created = store.create_rule(rule)
    return created.model_dump(mode="json")


@router.get("/rules/{rule_id}")
async def get_rule(
    rule_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    store = get_store()
    rule = store.get_rule(rule_id, workspace_id=workspace_id)
    if not rule:
        raise HTTPException(404, "Alert rule not found")
    return rule.model_dump(mode="json")


@router.put("/rules/{rule_id}", dependencies=[_AUTHOR])
async def update_rule(
    rule_id: str,
    body: AlertRuleUpdate,
    workspace_id: str = Depends(_safe_workspace_id),
):
    store = get_store()
    updates = body.model_dump(exclude_none=True)

    # ── Ownership validation on update (2026-05-22) ─────────────────────
    # Re-pointing an existing alert at a different workflow / project
    # has to obey the same workspace boundary as create. Otherwise a
    # caller in workspace A could create a benign rule, then PUT it
    # over to point at workspace B's workflow.
    new_wf_id = (updates.get("workflow_id") or "").strip() if "workflow_id" in updates else ""
    new_proj_id = (updates.get("project_id") or "").strip() if "project_id" in updates else ""
    if new_wf_id:
        wf = _resolve_workflow_in_workspace(new_wf_id, workspace_id)
        if wf is None:
            raise HTTPException(404, f"Workflow '{new_wf_id}' not found in this workspace")
        # Workflow's own project_id wins, same as create.
        updates["project_id"] = getattr(wf, "project_id", "default") or "default"
    elif new_proj_id:
        if not _project_in_workspace(new_proj_id, workspace_id):
            raise HTTPException(404, f"Project '{new_proj_id}' not found in this workspace")

    rule = store.update_rule(rule_id, updates, workspace_id=workspace_id)
    if not rule:
        raise HTTPException(404, "Alert rule not found")
    return rule.model_dump(mode="json")


@router.delete("/rules/{rule_id}", dependencies=[_AUTHOR])
async def delete_rule(
    rule_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    store = get_store()
    if not store.delete_rule(rule_id, workspace_id=workspace_id):
        raise HTTPException(404, "Alert rule not found")
    return {"deleted": True}


@router.post("/rules/{rule_id}/test")
async def test_alert(
    rule_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Send a real test notification for an alert rule — workspace-scoped."""
    store = get_store()
    rule = store.get_rule(rule_id, workspace_id=workspace_id)
    if not rule:
        raise HTTPException(404, "Alert rule not found")

    # Send real notification
    from fpulse.alerts.notifier import NotificationService
    notifier = NotificationService()
    log = notifier.send(rule, {
        "workflow_name": "Test Pipeline",
        "workflow_id": rule.workflow_id or "test",
        "execution_id": "test-run",
        "status": "success",
        "duration_ms": 1250,
        "triggered_condition": "test",
    })

    store.add_log(log)
    return {
        "status": log.status,
        "message": f"Test alert {'sent' if log.status == 'sent' else 'failed'} via {rule.channel.value}",
        "error": log.error,
    }


@router.get("/logs")
async def list_logs(
    workflow_id: str | None = None,
    limit: int = 100,
    workspace_id: str = Depends(_safe_workspace_id),
):
    store = get_store()
    if workflow_id:
        return store.list_logs_by_workflow(workflow_id, limit, workspace_id=workspace_id)
    return store.list_logs(limit, workspace_id=workspace_id)
