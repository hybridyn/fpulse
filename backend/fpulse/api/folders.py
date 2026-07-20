"""Folders API — nested grouping of pipelines inside a project.

Cascade-delete semantics: removing a folder removes every descendant
folder AND every workflow whose `folder_id` lives anywhere in that
subtree. Pipelines at the project root (folder_id=None) are not
affected by a folder delete.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from fpulse.auth.deps import require_auth, current_workspace_id
from fpulse.folders.models import Folder, FolderCreate, FolderUpdate

router = APIRouter(prefix="/api/folders", tags=["folders"])


def _store():
    from fpulse.main import app_state
    return app_state["folder_store"]


def _project_store():
    from fpulse.main import app_state
    return app_state["project_store"]


def _workflow_store():
    from fpulse.main import app_state
    return app_state["store"]


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


def _would_create_cycle(folder_id: str, candidate_parent: str | None, store) -> bool:
    """Return True if assigning candidate_parent to folder_id would create
    a cycle (parent chain eventually loops back to folder_id)."""
    if candidate_parent is None or candidate_parent == "":
        return False
    if candidate_parent == folder_id:
        return True
    visited: set[str] = set()
    cursor: str | None = candidate_parent
    while cursor:
        if cursor in visited:
            return True
        visited.add(cursor)
        if cursor == folder_id:
            return True
        parent = store.get(cursor)
        if not parent:
            return False
        cursor = parent.parent_folder_id or None
    return False


@router.get("")
async def list_folders(
    project_id: str = Query(..., description="Project to list folders for"),
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List all folders within a project (flat list — frontend builds the tree)."""
    folders = _store().list_for_project(project_id, workspace_id)
    return [f.model_dump(mode="json") for f in folders]


@router.post("")
async def create_folder(
    body: FolderCreate,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create a folder at the root of a project.

    F-Pulse OSS uses 1-level folders by design: a project contains
    folders, a folder contains pipelines. The `parent_folder_id` column
    is kept on the data model for forward-compat (a future tier may
    allow nesting) but the API rejects non-null values today.
    """
    project = _project_store().get(body.project_id)
    if not project:
        raise HTTPException(404, f"Project not found: {body.project_id}")
    if project.workspace_id != workspace_id:
        raise HTTPException(403, "Project belongs to a different workspace")

    if body.parent_folder_id:
        raise HTTPException(
            400,
            "Sub-folders are not supported in F-Pulse OSS — folders are one level deep.",
        )

    folder = Folder(
        name=body.name,
        description=body.description,
        project_id=body.project_id,
        parent_folder_id=None,
        workspace_id=workspace_id,
        color=body.color,
        icon=body.icon,
    )
    _store().create(folder)
    return folder.model_dump(mode="json")


@router.get("/{folder_id}")
async def get_folder(
    folder_id: str,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    folder = _store().get(folder_id)
    if not folder or folder.workspace_id != workspace_id:
        raise HTTPException(404, f"Folder not found: {folder_id}")
    return folder.model_dump(mode="json")


@router.patch("/{folder_id}")
async def update_folder(
    folder_id: str,
    body: FolderUpdate,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    folder = _store().get(folder_id)
    if not folder or folder.workspace_id != workspace_id:
        raise HTTPException(404, f"Folder not found: {folder_id}")

    updates = body.model_dump(exclude_unset=True)
    if "parent_folder_id" in updates and updates["parent_folder_id"]:
        # 1-level OSS rule — folders cannot be moved under another folder.
        # Allowed: PATCH with parent_folder_id=null (no-op move to root).
        raise HTTPException(
            400,
            "Sub-folders are not supported in F-Pulse OSS — folders are one level deep.",
        )

    updated = _store().update(folder_id, updates)
    return updated.model_dump(mode="json")


def _compute_folder_delete_impact(folder_id: str, workspace_id: str):
    """Compute the impact of deleting a folder without doing it.

    Returns (folder, doomed_folder_ids, affected_workflow_ids) or raises
    404 if the folder doesn't exist or isn't in this workspace. Used by
    both the dry-run preview endpoint and the actual delete so the
    counting logic is identical between preview and execute.
    """
    folder = _store().get(folder_id)
    if not folder or folder.workspace_id != workspace_id:
        raise HTTPException(404, f"Folder not found: {folder_id}")

    descendants = _store().descendants(folder_id)
    doomed_folder_ids = {folder_id} | {d.id for d in descendants}

    wf_store = _workflow_store()
    affected_workflow_ids: list[str] = []
    for wf_row in wf_store.list_all(workspace_id=workspace_id):
        wf_id = wf_row.get("id")
        if not wf_id:
            continue
        if wf_row.get("folder_id") in doomed_folder_ids:
            affected_workflow_ids.append(wf_id)

    return folder, doomed_folder_ids, affected_workflow_ids, descendants


@router.get("/{folder_id}/delete-preview")
async def preview_folder_delete(
    folder_id: str,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Dry-run: show what a delete of this folder would do.

    2026-05-22 (audit E3): the previous delete endpoint was a cascade
    that silently nuked every pipeline under the folder. This preview
    lets the UI render "Deleting this folder will also delete 7
    pipelines: …" so the user can choose between Move-to-root vs
    Cascade-delete before the destructive op fires.

    Returns folder names + pipeline ids so the UI can show them in
    the confirmation modal. Does not mutate anything.
    """
    folder, doomed_folder_ids, affected_workflow_ids, descendants = (
        _compute_folder_delete_impact(folder_id, workspace_id)
    )
    return {
        "folder_id": folder_id,
        "folder_name": folder.name,
        "descendant_folder_ids": [d.id for d in descendants],
        "descendant_folder_names": [d.name for d in descendants],
        "doomed_folder_count": len(doomed_folder_ids),
        "affected_workflow_count": len(affected_workflow_ids),
        "affected_workflow_ids": affected_workflow_ids,
    }


@router.delete("/{folder_id}")
async def delete_folder(
    folder_id: str,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
    force: bool = False,
    move_to_root: bool = False,
):
    """Delete a folder.

    2026-05-22 (audit E3) — destructive cascade behaviour is now an
    explicit opt-in. Three modes:

      * ``move_to_root=true`` (recommended default): reparent every
        pipeline whose folder is being deleted to project-root
        (folder_id = None), then delete the folder + descendants.
        Pipelines are preserved.
      * ``force=true``: original cascade — folder + descendants +
        pipelines all deleted. Use only when you really mean it.
      * Neither flag set + the folder contains pipelines: 409. The
        client must explicitly pick a mode. This avoids the silent
        data-loss the audit flagged.

    Empty folders (no pipelines anywhere in the subtree) delete
    without any flag — there's nothing to lose.
    """
    _folder, doomed_folder_ids, affected_workflow_ids, descendants = (
        _compute_folder_delete_impact(folder_id, workspace_id)
    )

    wf_store = _workflow_store()

    if affected_workflow_ids and not (force or move_to_root):
        raise HTTPException(
            409,
            {
                "message": (
                    "Folder contains pipelines — choose move_to_root=true "
                    "(reparent them to project root) or force=true (delete "
                    "them with the folder)."
                ),
                "affected_workflow_count": len(affected_workflow_ids),
                "affected_workflow_ids": affected_workflow_ids,
                "descendant_folder_count": len(descendants),
            },
        )

    moved_count = 0
    deleted_workflows = 0
    if move_to_root and affected_workflow_ids:
        # Reparent every affected workflow to project root.
        for wf_id in affected_workflow_ids:
            try:
                wf_v = wf_store.get(wf_id, workspace_id=workspace_id)
                if wf_v is None:
                    continue
                wf_v.workflow.folder_id = None
                wf_store.save(
                    wf_v.workflow,
                    change_summary="Reparented to project root on folder delete",
                )
                moved_count += 1
            except Exception:
                # Best-effort — keep going on individual failures
                # rather than aborting the whole operation. The folder
                # delete that follows is safe because the workflow
                # rows still exist (they just stayed in their old
                # folder); the user can retry.
                continue
    elif force and affected_workflow_ids:
        for wf_id in affected_workflow_ids:
            if wf_store.delete(wf_id):
                deleted_workflows += 1

    for fid in doomed_folder_ids:
        _store().delete(fid)

    return {
        "deleted_folder_id": folder_id,
        "deleted_descendant_count": len(descendants),
        "deleted_workflow_count": deleted_workflows,
        "moved_to_root_workflow_count": moved_count,
        "mode": "force" if force else ("move_to_root" if move_to_root else "empty"),
    }


class MoveWorkflowsRequest(BaseModel):
    workflow_ids: list[str]
    folder_id: str | None = None


@router.post("/move-workflows")
async def move_workflows(
    body: MoveWorkflowsRequest,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Move a batch of workflows to a folder (or to project root if
    folder_id is None). All workflows must already belong to the same
    project as the target folder."""
    target_project_id: str | None = None
    if body.folder_id:
        folder = _store().get(body.folder_id)
        if not folder or folder.workspace_id != workspace_id:
            raise HTTPException(404, f"Folder not found: {body.folder_id}")
        target_project_id = folder.project_id

    wf_store = _workflow_store()
    moved = 0
    for wf_id in body.workflow_ids:
        wf_version = wf_store.get(wf_id, workspace_id=workspace_id)
        if not wf_version:
            continue
        if target_project_id and wf_version.workflow.project_id != target_project_id:
            raise HTTPException(
                400,
                f"Workflow {wf_id} belongs to a different project than target folder",
            )
        wf_version.workflow.folder_id = body.folder_id
        wf_store.save(wf_version.workflow, change_summary=f"Moved to folder {body.folder_id or 'root'}")
        moved += 1
    return {"moved": moved}
