"""Template API — user-saved templates + workflow import/export.

PR 5 (May 17 2026): the built-in gallery surface (`GET /api/templates`,
`/categories`, `/{id}`, `/{id}/use`) was removed. The frontend reads its
built-in templates from `frontend/src/templates/catalog.ts` directly and
never consumed the backend gallery — keeping both was just a source of
"which one is canonical?" confusion. The CLI's pipeline export/import
commands and the TemplatesPage "My templates" library (workspace-scoped)
remain — those are the legitimately backend-owned surfaces.

If you need a backend-served built-in gallery later (e.g. to share
templates across workspaces), reintroduce it as a new router under a
different name, not here.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from fpulse.auth.deps import current_workspace_id
from fpulse.ir.schema import (
    Workflow,
    Step,
    StepConnection,
    StepType,
    NodePosition,
)

router = APIRouter(prefix="/api/templates", tags=["templates"])


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ImportRequest(BaseModel):
    """Import a pipeline definition from JSON."""
    name: str
    description: str = ""
    steps: list[dict]
    connections: list[dict]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _template_to_workflow(
    template: dict,
    custom_name: str | None = None,
    workspace_id: str = "default",
) -> Workflow:
    """Convert a gallery template dict into a proper Workflow IR.

    Step IDs are freshly generated so every instantiation is unique.
    Connections reference steps by index in the template, mapped to new IDs.
    The resulting workflow is stamped with ``workspace_id`` so it lands
    in the caller's tenant.
    """
    steps: list[Step] = []
    step_ids: list[str] = []

    for i, raw in enumerate(template["steps"]):
        sid = uuid.uuid4().hex[:8]
        step_ids.append(sid)

        # Resolve step type
        try:
            st = StepType(raw["type"])
        except ValueError:
            st = StepType.TRANSFORM

        pos = raw.get("position", {})
        steps.append(Step(
            id=sid,
            type=st,
            label=raw.get("label", ""),
            params=raw.get("params", {}),
            position=NodePosition(x=pos.get("x", i * 350), y=pos.get("y", 100)),
        ))

    connections: list[StepConnection] = []
    for conn in template.get("connections", []):
        from_idx = conn.get("from_step", 0)
        to_idx = conn.get("to_step", 1)
        if 0 <= from_idx < len(step_ids) and 0 <= to_idx < len(step_ids):
            connections.append(StepConnection(
                from_step=step_ids[from_idx],
                to_step=step_ids[to_idx],
                from_port=conn.get("from_port", "output"),
                to_port=conn.get("to_port", "input"),
            ))

    return Workflow(
        name=custom_name or template.get("name", "Untitled Pipeline"),
        description=template.get("description", ""),
        workspace_id=workspace_id or "default",
        steps=steps,
        connections=connections,
        metadata={
            "template_id": template.get("id"),
            "template_category": template.get("category"),
        },
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
#
# PR 5: the built-in gallery endpoints (`GET /`, `/categories`,
# `/{template_id}`, `/{template_id}/use`) were removed because nothing on
# the OSS frontend consumed them — the canonical built-in catalogue is
# `frontend/src/templates/catalog.ts`. Keeping the backend gallery alive
# meant two sources of truth diverged silently.


# ═══════════════════════════════════════════════════════════════════════
#  User templates (workspace-scoped library)
#
#  IMPORTANT: declared BEFORE the `/{template_id}` catch-all so FastAPI's
#  ordered route matching resolves `/api/templates/user[...]` here
#  instead of treating "user" as a built-in template id and 404'ing.
# ═══════════════════════════════════════════════════════════════════════

class UserTemplateCreate(BaseModel):
    name: str
    tagline: str = ""
    description: str = ""
    category: str = "Custom"
    steps: list[dict]
    connections: list[dict] = []


def _get_db():
    from fpulse.main import app_state
    return app_state["db"]


def _row_to_user_template(row: tuple) -> dict:
    """Map a user_templates row to the same shape the gallery API returns
    so the frontend can merge them transparently."""
    (id_, ws_id, name, tagline, description, category, data_json,
     created_by, created_at, updated_at) = row
    body = json.loads(data_json) if data_json else {}
    return {
        "id": id_,
        "name": name,
        "tagline": tagline,
        "description": description,
        "category": category,
        "source": "user",
        "created_by": created_by,
        "created_at": created_at,
        "updated_at": updated_at,
        "steps": body.get("steps", []),
        "connections": body.get("connections", []),
    }


@router.get("/user")
async def list_user_templates(
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List all user-saved templates in the caller's workspace."""
    db = _get_db()
    cur = db.execute_with_retry(
        """SELECT id, workspace_id, name, tagline, description, category,
                  data, created_by, created_at, updated_at
             FROM user_templates
            WHERE workspace_id = ?
         ORDER BY updated_at DESC""",
        [workspace_id],
    )
    rows = cur.fetchall() if cur else []
    return {"templates": [_row_to_user_template(r) for r in rows]}


@router.post("/user")
async def create_user_template(
    body: UserTemplateCreate,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Save a pipeline as a user template. Workspace + name is unique."""
    from datetime import datetime, timezone
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Template name is required")
    if len(body.steps) == 0:
        raise HTTPException(400, "Template must contain at least one step")

    db = _get_db()
    dup_cur = db.execute_with_retry(
        "SELECT id FROM user_templates WHERE workspace_id = ? AND lower(name) = lower(?)",
        [workspace_id, name],
    )
    if dup_cur and dup_cur.fetchone():
        raise HTTPException(409, f'A user template named "{name}" already exists')

    tid = "ut_" + uuid.uuid4().hex[:10]
    now = datetime.now(timezone.utc).isoformat()
    payload = {"steps": body.steps, "connections": body.connections}

    db.execute_with_retry(
        """INSERT INTO user_templates
             (id, workspace_id, name, tagline, description, category,
              data, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [tid, workspace_id, name, body.tagline, body.description, body.category,
         json.dumps(payload), "", now, now],
    )

    return {
        "id": tid,
        "name": name,
        "tagline": body.tagline,
        "description": body.description,
        "category": body.category,
        "source": "user",
        "created_at": now,
        "updated_at": now,
        "steps": body.steps,
        "connections": body.connections,
    }


@router.delete("/user/{template_id}")
async def delete_user_template(
    template_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Delete a user template. 404 if it doesn't exist OR belongs to a
    different workspace (don't leak existence across tenants)."""
    db = _get_db()
    cur = db.execute_with_retry(
        "SELECT id FROM user_templates WHERE id = ? AND workspace_id = ?",
        [template_id, workspace_id],
    )
    if not cur or not cur.fetchone():
        raise HTTPException(404, "User template not found")
    db.execute_with_retry(
        "DELETE FROM user_templates WHERE id = ? AND workspace_id = ?",
        [template_id, workspace_id],
    )
    return {"deleted": template_id}


# ═══════════════════════════════════════════════════════════════════════


@router.post("/import")
async def import_pipeline(
    body: ImportRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Import a pipeline from a JSON definition into the caller's
    workspace. Rejects the placeholder "Untitled Pipeline" name with 400
    (rule locked 2026-05-09 — store.save() also catches this defensively)."""
    raw_name = (body.name or "").strip()
    if not raw_name or raw_name.lower() == "untitled pipeline":
        raise HTTPException(
            status_code=400,
            detail=(
                "Pipeline name is required and cannot be 'Untitled Pipeline'. "
                "Give it a descriptive name before importing."
            ),
        )
    template_dict = {
        "id": f"import-{uuid.uuid4().hex[:8]}",
        "name": raw_name,
        "description": body.description,
        "steps": body.steps,
        "connections": body.connections,
    }

    workflow = _template_to_workflow(
        template_dict, custom_name=raw_name, workspace_id=workspace_id,
    )

    from fpulse.main import app_state
    store = app_state["store"]
    version = store.save(workflow, change_summary=f"Imported: {raw_name}")

    return {
        "id": workflow.id,
        "version": version.version,
        "workflow": workflow.model_dump(mode="json"),
    }


@router.post("/export/{workflow_id}")
async def export_workflow(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Export an existing workflow as a shareable JSON template
    (scoped to caller's workspace)."""
    from fpulse.main import app_state
    store = app_state["store"]

    wf_version = store.get(workflow_id, workspace_id=workspace_id)
    if not wf_version:
        raise HTTPException(status_code=404, detail=f"Workflow not found: {workflow_id}")

    wf = wf_version.workflow

    # Build a portable template (index-based connections instead of ID-based)
    step_id_to_idx = {s.id: i for i, s in enumerate(wf.steps)}

    export_steps = [
        {
            "type": s.type.value,
            "label": s.label,
            "params": s.params,
            "position": {"x": s.position.x, "y": s.position.y},
        }
        for s in wf.steps
    ]

    export_connections = []
    for c in wf.connections:
        from_idx = step_id_to_idx.get(c.from_step)
        to_idx = step_id_to_idx.get(c.to_step)
        if from_idx is not None and to_idx is not None:
            conn = {"from_step": from_idx, "to_step": to_idx}
            if c.from_port != "output":
                conn["from_port"] = c.from_port
            if c.to_port != "input":
                conn["to_port"] = c.to_port
            export_connections.append(conn)

    return {
        "name": wf.name,
        "description": wf.description,
        "steps": export_steps,
        "connections": export_connections,
        "exported_at": wf.updated_at.isoformat() if wf.updated_at else None,
        "version": wf_version.version,
    }
