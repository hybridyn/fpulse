"""Pre-publish review card — POST /api/ai/pre-publish/{workflow_id}.

Step 4 of the F-Pulse AI completion arc. Produces the 7-section preview
shown to the user immediately before they submit a pipeline for review or
deploy. Sections (per `project_fpulse_ai_completion.md` 4b):

  1. change_diff       — diff against the last successful publish
  2. inventory         — connections / nodes / alerts summary (counts + names)
  3. approval_plan     — which approvals are required for this env
  4. security_posture  — secrets safe / connections present / alerts wired
  5. severity_summary  — blocker / warning / info aggregation
  6. snapshot_hash     — SHA-256 over the canonical workflow IR
  7. risk_level        — low / medium / high derived from severities

The endpoint is fully deterministic — no LLM required. The card content
is what feeds into both the inline UI and the durable approval-snapshot
artifact (the SHA-256 commits the user's intent at click time, so a
reviewer can later prove "this is exactly what was reviewed").
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from fpulse.auth.deps import current_workspace_id

router = APIRouter(prefix="/api/ai", tags=["ai"])


class SeverityItem(BaseModel):
    severity: str  # "blocker" | "warning" | "info"
    section: str   # which of the 7 sections raised it
    message: str


class PrePublishResponse(BaseModel):
    workflow_id: str
    workflow_name: str
    target_environment: str
    snapshot_hash: str
    risk_level: str  # "low" | "medium" | "high"
    sections: dict[str, Any]
    severity_summary: dict[str, int]   # {"blocker": N, "warning": N, "info": N}
    blockers: list[SeverityItem]
    warnings: list[SeverityItem]
    infos: list[SeverityItem]


def _get_workflow_store():
    """Resolve the WorkflowStore via app_state, mirroring api/workflows.py."""
    try:
        from fpulse.main import app_state  # type: ignore
        return app_state.get("workflow_store")
    except Exception:
        return None


def _canonical_workflow_dict(workflow_obj) -> dict[str, Any]:
    """Strip volatile fields so the hash is stable across saves."""
    d = workflow_obj.model_dump(mode="json") if hasattr(workflow_obj, "model_dump") else dict(workflow_obj)
    for k in ("created_at", "updated_at", "deployed_at", "submitted_at", "approved_at", "published_at"):
        d.pop(k, None)
    return d


def _compute_hash(workflow_obj) -> str:
    canonical = _canonical_workflow_dict(workflow_obj)
    encoded = json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _step_type(s) -> str:
    t = getattr(s, "type", None) or getattr(s, "step_type", None) or "unknown"
    # type may be StepType enum — coerce to string for JSON
    return getattr(t, "value", t) if t else "unknown"


def _step_label(s) -> str:
    return getattr(s, "label", "") or getattr(s, "name", "") or getattr(s, "id", "")


def _build_inventory(workflow) -> dict[str, Any]:
    steps = getattr(workflow, "steps", []) or []
    connections = getattr(workflow, "connections", []) or []
    by_type: dict[str, int] = {}
    for s in steps:
        st = _step_type(s)
        by_type[st] = by_type.get(st, 0) + 1
    return {
        "node_count": len(steps),
        "connection_count": len(connections),
        "nodes_by_type": by_type,
        "node_names": [_step_label(s) for s in steps][:50],
    }


def _build_change_diff(workflow_store, workflow) -> dict[str, Any]:
    """Compare the latest version's IR with the most recent deployed one."""
    try:
        latest = workflow_store.get(workflow.id)
        latest_ir = latest.workflow if latest else None
        deployed_v = getattr(workflow, "deployed_version", None)
        if not deployed_v:
            return {
                "has_baseline": False,
                "summary": "No previous deployment to compare against — this is a first-time publish.",
                "added_steps": [],
                "removed_steps": [],
                "modified_steps": [],
            }
        baseline_v = workflow_store.get(workflow.id, version=deployed_v)
        baseline = baseline_v.workflow if baseline_v else None
        if baseline is None or latest_ir is None:
            return {"has_baseline": False, "summary": "Baseline unavailable.", "added_steps": [], "removed_steps": [], "modified_steps": []}
        baseline_ids = {getattr(s, "id", ""): s for s in (baseline.steps or [])}
        latest_ids = {getattr(s, "id", ""): s for s in (latest_ir.steps or [])}
        added = [sid for sid in latest_ids if sid not in baseline_ids]
        removed = [sid for sid in baseline_ids if sid not in latest_ids]
        modified: list[str] = []
        for sid, s in latest_ids.items():
            b = baseline_ids.get(sid)
            if b is None:
                continue
            try:
                a_dump = s.model_dump(mode="json") if hasattr(s, "model_dump") else dict(s)
                b_dump = b.model_dump(mode="json") if hasattr(b, "model_dump") else dict(b)
            except Exception:
                continue
            if a_dump != b_dump:
                modified.append(sid)
        return {
            "has_baseline": True,
            "baseline_version": deployed_v,
            "added_steps": added[:30],
            "removed_steps": removed[:30],
            "modified_steps": modified[:30],
            "summary": f"+{len(added)} added, -{len(removed)} removed, ~{len(modified)} modified",
        }
    except Exception as e:
        return {"has_baseline": False, "summary": f"Diff unavailable ({type(e).__name__}).", "added_steps": [], "removed_steps": [], "modified_steps": []}


def _build_security_posture(workflow) -> dict[str, Any]:
    """Look for surface-level security smells: hard-coded credentials,
    missing connection refs, alert coverage."""
    secrets_safe = True
    connections_present = True
    alerts_wired = False
    issues: list[str] = []

    steps = getattr(workflow, "steps", []) or []
    if not steps:
        connections_present = False
        issues.append("Pipeline has zero steps.")

    KEYS = ("password", "api_key", "secret", "token", "private_key")
    for s in steps:
        params = getattr(s, "params", {}) or {}
        for k, v in params.items():
            if not isinstance(v, str):
                continue
            kl = k.lower()
            if any(kl == kk or kl.endswith("_" + kk) for kk in KEYS):
                # Looks like a credential field. Safe forms: empty / starts with $ (env ref) / starts with vault://
                if v and not v.startswith(("$", "vault://", "{{")):
                    secrets_safe = False
                    issues.append(f"Step '{_step_label(s)}' has inline credential field {k!r}.")

    # Alerts wired? — workflow-level alerts list is not in the IR; check metadata.
    md = getattr(workflow, "metadata", {}) or {}
    if md.get("alert_rule_ids") or md.get("alerts"):
        alerts_wired = True

    return {
        "secrets_safe": secrets_safe,
        "connections_present": connections_present,
        "alerts_wired": alerts_wired,
        "issues": issues[:20],
    }


def _build_approval_plan(workflow, target_environment: str) -> dict[str, Any]:
    """Who needs to approve this for the chosen environment?"""
    if target_environment == "prod":
        return {
            "required": True,
            "gates": ["sandbox_run", "deploy_review"],
            "approvers": ["admin", "super_admin"],
            "summary": "PROD deploy needs Gate 1 (sandbox run + approval) and Gate 2 (deploy approval).",
        }
    return {
        "required": False,
        "gates": [],
        "approvers": [],
        "summary": "DEV publish is self-service — no approval required.",
    }


def _classify(security: dict, change: dict, inventory: dict) -> tuple[list[SeverityItem], list[SeverityItem], list[SeverityItem]]:
    blockers: list[SeverityItem] = []
    warnings: list[SeverityItem] = []
    infos: list[SeverityItem] = []

    if not inventory.get("node_count"):
        blockers.append(SeverityItem(severity="blocker", section="inventory", message="Pipeline has no steps."))
    if not security.get("secrets_safe", True):
        blockers.append(SeverityItem(severity="blocker", section="security_posture", message="Hard-coded credentials detected."))
    if not security.get("alerts_wired", False):
        warnings.append(SeverityItem(severity="warning", section="security_posture", message="No alert rules configured for this pipeline."))
    if change.get("has_baseline") and len(change.get("modified_steps", [])) > 5:
        warnings.append(SeverityItem(severity="warning", section="change_diff", message=f"{len(change['modified_steps'])} steps modified since last publish — review carefully."))
    if change.get("has_baseline") is False:
        infos.append(SeverityItem(severity="info", section="change_diff", message="No prior baseline — first-time publish."))
    return blockers, warnings, infos


def _risk_level(blockers, warnings) -> str:
    if blockers:
        return "high"
    if len(warnings) >= 2:
        return "medium"
    if warnings:
        return "low"
    return "low"


@router.post("/pre-publish/{workflow_id}", response_model=PrePublishResponse)
def pre_publish(workflow_id: str, request: Request, target_env: str = "prod") -> PrePublishResponse:
    """Build the 7-section pre-publish review card.

    Deterministic — no LLM call. Caller can show the result inline AND
    persist `snapshot_hash` as the approval artifact (committing the
    exact IR the user is about to publish).
    """
    target_env = target_env.lower()
    if target_env not in ("dev", "prod"):
        target_env = "prod"

    store = _get_workflow_store()
    if store is None:
        raise HTTPException(status_code=503, detail="Workflow store unavailable")

    wv = store.get(workflow_id)
    if wv is None or wv.workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} not found")
    workflow = wv.workflow

    # Workspace tenant guard.
    workspace_id = current_workspace_id(request)
    if getattr(workflow, "workspace_id", "default") not in (workspace_id, "default"):
        raise HTTPException(status_code=404, detail="Workflow not found in this workspace")

    inventory = _build_inventory(workflow)
    change_diff = _build_change_diff(store, workflow)
    security = _build_security_posture(workflow)
    approval = _build_approval_plan(workflow, target_env)
    snapshot_hash = _compute_hash(workflow)

    blockers, warnings, infos = _classify(security, change_diff, inventory)
    severity_summary = {
        "blocker": len(blockers),
        "warning": len(warnings),
        "info": len(infos),
    }
    risk = _risk_level(blockers, warnings)

    return PrePublishResponse(
        workflow_id=workflow_id,
        workflow_name=getattr(workflow, "name", workflow_id),
        target_environment=target_env,
        snapshot_hash=snapshot_hash,
        risk_level=risk,
        sections={
            "change_diff": change_diff,
            "inventory": inventory,
            "approval_plan": approval,
            "security_posture": security,
        },
        severity_summary=severity_summary,
        blockers=blockers,
        warnings=warnings,
        infos=infos,
    )
