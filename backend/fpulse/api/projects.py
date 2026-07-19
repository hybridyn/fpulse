"""Projects CRUD API — auth-gated with role + ACL enforcement.

Access model (introduced Apr 8 2026):

    create / delete / update  → admin+ only (super_admin, admin)
    list / get / list pipelines → any logged-in user, filtered by ACL

ACL rules for non-admin users:
    A user can SEE a project iff at least one of:
      1. `user.projects` is empty (legacy "no restriction" default), OR
      2. the project.id is in `user.projects`, OR
      3. the user.id is in `project.members`, OR
      4. the user.id equals `project.owner_id`

    Admins (role super_admin / admin) bypass the ACL entirely — they can
    see every project in the instance. This matches the "admin operates,
    developer works" governance model: admins own projects, developers
    are granted access to specific ones.

Historical note: this file used to be zero-auth. Every endpoint was open
to unauthenticated callers, and `user.projects` was stored on the user
model but read by nobody. That made per-user project scoping a
paper-only feature. This rewrite closes that gap without breaking the
common case — if no admin ever sets `user.projects`, the previous
all-access behaviour is preserved by rule (1).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from fpulse.auth.deps import require_auth, require_admin, current_workspace_id
from fpulse.projects.models import Project, ProjectCreate, ProjectUpdate

router = APIRouter(prefix="/api/projects", tags=["projects"])


def get_store():
    from fpulse.main import app_state
    return app_state["project_store"]


def get_workflow_store():
    from fpulse.main import app_state
    return app_state["store"]


# ── ACL helper ───────────────────────────────────────────────────────────────

# Roles that bypass the per-project ACL. Kept as a set so adding `lead` or
# similar in future is a one-line change.
_ADMIN_ROLES = {"super_admin", "admin"}


def _can_see(user, project_dict: dict) -> bool:
    """Return True if `user` is allowed to see `project_dict`.

    `project_dict` is the raw dict returned by project_store.list_all() /
    .get(), not a Project instance, so we use .get() with defaults to stay
    tolerant of legacy rows missing the new `owner_id` / `members` fields.
    """
    # Admins see everything — no ACL applied.
    if user.role in _ADMIN_ROLES:
        return True

    # If the user has no project allow-list AND is not an explicit member
    # anywhere, fall back to "no restriction" so existing free-tier setups
    # keep working until an admin actually starts scoping access.
    user_projects = getattr(user, "projects", None) or []

    pid = project_dict.get("id")
    owner_id = project_dict.get("owner_id", "")
    members = project_dict.get("members", []) or []

    if not user_projects:
        # No restriction set on the user — only bar them if the project
        # has an explicit member list that doesn't include them. Owner
        # always sees their own project.
        if members or owner_id:
            return user.id == owner_id or user.id in members
        return True

    # User has an explicit allow-list → must match the project.
    if pid in user_projects:
        return True
    if user.id == owner_id:
        return True
    if user.id in members:
        return True
    return False


# ── Endpoints ────────────────────────────────────────────────────────────────


def _safe_workspace_id(request: Request) -> str:
    """Wrap current_workspace_id so dep failures surface as readable
    HTTP errors instead of FastAPI's opaque 500 body."""
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


# 2026-05-30 (P7): trailing-slash alias.
@router.get("", include_in_schema=False)
@router.get("/")
async def list_projects(
    user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
    include_archived: bool = False,
):
    """List projects visible to the caller in the current workspace.

    Two-stage filter:
      1. Workspace scope — only projects belonging to the workspace
         resolved from the X-Workspace-Id header (or the user's first
         membership) are considered. This is the multi-tenant cut.
      2. Project ACL — within that workspace, developers still only
         see projects they're a member/owner of or are listed in their
         `user.projects` allow-list. Admins see everything in the
         workspace.

    The two filters are independent. A user can be a member of three
    workspaces and only see five projects across all of them; switching
    workspaces changes which five.

    2026-05-22: archived projects (`status == "archived"`) are
    excluded by default. Pass `include_archived=true` to see them —
    the ProjectsPage uses this for the Archived tab. Was previously
    a frontend localStorage flag (audit C1).
    """
    store = get_store()
    wf_store = get_workflow_store()
    try:
        projects = store.list_all(workspace_id=workspace_id)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("list_all failed")
        raise HTTPException(500, "list_all failed") from exc

    if not include_archived:
        projects = [p for p in projects if (p.get("status") or "active") != "archived"]

    visible = [p for p in projects if _can_see(user, p)]

    # Enrich with pipeline counts (only for visible projects — no leak of
    # project ids the user can't access). Scope to the current workspace
    # so the count agrees with /projects/tree; previously this used a
    # global list_all() which made the Projects-page "Total Pipelines"
    # stat over-count by including other workspaces.
    all_workflows = wf_store.list_all(workspace_id=workspace_id)
    for p in visible:
        pid = p["id"]
        p["pipeline_count"] = len([
            w for w in all_workflows
            if w.get("project_id", "default") == pid
        ])

    # 2026-05-25 — per-project Storage rollup (files + tables + outputs).
    # Computed once over the workspace and joined in, so the project list
    # endpoint stays O(projects + storage_objects) rather than O(projects ×
    # storage_objects). Best-effort: any error leaves the field absent.
    try:
        from fpulse.datastore.store import get_store as _get_datastore
        from fpulse.datastore.models import OBJECT_KIND_FILE, OBJECT_KIND_OUTPUT
        ds = _get_datastore()
        all_files = ds.list_objects(workspace_id, kind=OBJECT_KIND_FILE, include_deleted=False)
        all_outputs = ds.list_objects(workspace_id, kind=OBJECT_KIND_OUTPUT, include_deleted=False)
        all_tables = ds.list_tables(workspace_id) if hasattr(ds, "list_tables") else []
        # Index by project_id once.
        from collections import defaultdict
        files_by_project: dict[str, list] = defaultdict(list)
        outputs_by_project: dict[str, list] = defaultdict(list)
        tables_by_project: dict[str, list] = defaultdict(list)
        for f in all_files:
            files_by_project[getattr(f, "project_id", None) or ""].append(f)
        for o in all_outputs:
            outputs_by_project[getattr(o, "project_id", None) or ""].append(o)
        for t in all_tables:
            tables_by_project[getattr(t, "project_id", None) or ""].append(t)
        for p in visible:
            pid = p["id"]
            fs = files_by_project.get(pid, [])
            os_ = outputs_by_project.get(pid, [])
            ts = tables_by_project.get(pid, [])
            p["storage"] = {
                "file_count": len(fs),
                "file_bytes": sum(int(getattr(x, "size_bytes", 0) or 0) for x in fs),
                "table_count": len(ts),
                "table_bytes": sum(int(getattr(x, "size_bytes", 0) or 0) for x in ts),
                "output_count": len(os_),
                "output_bytes": sum(int(getattr(x, "size_bytes", 0) or 0) for x in os_),
            }
    except Exception:
        import logging
        logging.getLogger(__name__).exception("list_projects storage rollup failed")
    return visible


@router.post("", include_in_schema=False)
@router.post("/")
async def create_project(
    body: ProjectCreate,
    user = Depends(require_admin),
    workspace_id: str = Depends(current_workspace_id),
):
    """Create a new project in the current workspace. Admin-only.

    The creator becomes the owner (both `owner_id` and human-readable
    `owner`). The project lands in the workspace resolved from the
    X-Workspace-Id header — admins who manage multiple workspaces must
    switch first (via the header / future UI switcher) before creating.

    Admins who create projects can immediately see them; to share with
    developers the admin must either add the project to the developer's
    `user.projects`, or add the developer's id to the project's
    `members`. Workspace membership is a separate, additional gate.
    """
    from fpulse.common.unique_name import ensure_unique_name
    store = get_store()
    # Auto-suffix duplicate project names within the same workspace.
    existing_names: set[str] = set()
    try:
        for p in store.list_all(workspace_id=workspace_id):
            n = p.get("name") if isinstance(p, dict) else getattr(p, "name", None)
            if n:
                existing_names.add(n)
    except Exception:  # noqa: BLE001
        existing_names = set()
    unique_name = ensure_unique_name(body.name, existing_names)
    if body.parent_id:
        parent = store.get(body.parent_id)
        if not parent:
            raise HTTPException(404, f"Parent project not found: {body.parent_id}")
        if parent.workspace_id != workspace_id:
            raise HTTPException(400, "Parent project belongs to a different workspace")
    project = Project(
        name=unique_name,
        description=body.description,
        owner=user.name or user.email,
        owner_id=user.id,
        workspace_id=workspace_id,
        parent_id=body.parent_id,
        color=body.color,
        icon=body.icon,
        members=body.members,
        metadata=body.metadata,
    )
    created = store.create(project)
    return created.model_dump(mode="json")


@router.get("/tree")
async def project_tree(
    user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Return the project tree visible to the caller as a list of root
    nodes with nested `children`. ACL is applied per-node; projects the
    caller can't see are pruned but their ACL-visible descendants are
    re-parented to the nearest visible ancestor so the tree stays
    connected."""
    store = get_store()
    wf_store = get_workflow_store()
    raw = store.list_all(workspace_id=workspace_id)

    all_workflows = wf_store.list_all(workspace_id=workspace_id)
    counts: dict[str, int] = {}
    for w in all_workflows:
        pid = w.get("project_id", "default")
        counts[pid] = counts.get(pid, 0) + 1

    by_id: dict[str, dict] = {p["id"]: p for p in raw}
    visible_ids = {p["id"] for p in raw if _can_see(user, p)}

    def nearest_visible_parent(pid: str) -> str | None:
        cursor = by_id.get(pid, {}).get("parent_id") or None
        seen: set[str] = set()
        while cursor:
            if cursor in seen:
                return None
            seen.add(cursor)
            if cursor in visible_ids:
                return cursor
            cursor = by_id.get(cursor, {}).get("parent_id") or None
        return None

    nodes: dict[str, dict] = {}
    for p in raw:
        if p["id"] not in visible_ids:
            continue
        p = dict(p)
        p["pipeline_count"] = counts.get(p["id"], 0)
        p["children"] = []
        nodes[p["id"]] = p

    roots: list[dict] = []
    for pid, node in nodes.items():
        parent = node.get("parent_id") or None
        effective_parent = parent if parent in nodes else nearest_visible_parent(pid)
        if effective_parent and effective_parent in nodes:
            nodes[effective_parent]["children"].append(node)
        else:
            roots.append(node)

    return roots


@router.get("/pending-approvals")
async def list_pending_projects(
    user=Depends(require_admin),
    workspace_id: str = Depends(current_workspace_id),
):
    """List all projects pending approval. Admin-only.

    ⚠️ ORDER MATTERS — this route MUST stay above `/{project_id}` so
    FastAPI matches "pending-approvals" as a static path instead of as
    a project_id (which would 404 because no such project exists). This
    is the canonical fix for the 404 reported 2026-05-26.
    """
    store = get_store()
    all_projects = store.list_all()
    pending = []
    for p in all_projects:
        p_dict = p if isinstance(p, dict) else p.model_dump(mode="json")
        if p_dict.get("approval_status") == "pending":
            ws = p_dict.get("workspace_id", "default")
            if ws == workspace_id or user.role in _ADMIN_ROLES:
                pending.append(p_dict)
    return pending


@router.get("/{project_id}")
async def get_project(
    project_id: str,
    user = Depends(require_auth),
    workspace_id: str = Depends(current_workspace_id),
):
    """Get a project by ID. 404 is returned for ALL access denials —
    "does not exist", "wrong workspace", and "ACL says no" — so that
    unprivileged users cannot enumerate project ids by probing for 403s
    or for the difference between "this project is in another workspace
    you don't belong to" and "this project doesn't exist at all"."""
    store = get_store()
    project = store.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    project_dict = project.model_dump(mode="json")
    # Workspace scope check — instance admins bypass it the same way
    # they bypass the project ACL below.
    proj_ws = project_dict.get("workspace_id") or "default"
    if proj_ws != workspace_id and user.role not in _ADMIN_ROLES:
        raise HTTPException(404, "Project not found")
    if not _can_see(user, project_dict):
        raise HTTPException(404, "Project not found")
    # 2026-05-25 — attach a lightweight Storage rollup so the Projects
    # page can show "N files, M tables" per project without an extra
    # round-trip. Best-effort: any failure leaves the field absent
    # rather than 500'ing the project detail call.
    try:
        from fpulse.datastore.store import get_store as _get_datastore
        from fpulse.datastore.models import OBJECT_KIND_FILE, OBJECT_KIND_OUTPUT
        ds = _get_datastore()
        files = ds.list_objects(workspace_id, kind=OBJECT_KIND_FILE, project_id=project_id, include_deleted=False)
        outputs = ds.list_objects(workspace_id, kind=OBJECT_KIND_OUTPUT, project_id=project_id, include_deleted=False)
        tables = []
        if hasattr(ds, "list_tables"):
            tables = [t for t in ds.list_tables(workspace_id) if (getattr(t, "project_id", None) or None) == project_id]
        project_dict["storage"] = {
            "file_count": len(files),
            "file_bytes": sum(int(getattr(f, "size_bytes", 0) or 0) for f in files),
            "table_count": len(tables),
            "table_bytes": sum(int(getattr(t, "size_bytes", 0) or 0) for t in tables),
            "output_count": len(outputs),
            "output_bytes": sum(int(getattr(o, "size_bytes", 0) or 0) for o in outputs),
        }
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "project storage rollup failed for %s", project_id,
        )
    return project_dict


@router.put("/{project_id}")
async def update_project(project_id: str, body: ProjectUpdate, user = Depends(require_admin)):
    """Update a project. Admin-only.

    We also allow this endpoint to mutate `members` via ProjectUpdate,
    so admins can grant/revoke developer access without having to edit
    the user record. See the user edit dialog for the inverse flow.

    `parent_id` may be set to move the project to a new parent. Cycle
    prevention walks the parent chain; rejecting cycles is mandatory or
    the tree-walk endpoints would loop.
    """
    store = get_store()
    updates = body.model_dump(exclude_unset=True)
    if "parent_id" in updates:
        new_parent = updates["parent_id"]
        if new_parent == project_id:
            raise HTTPException(400, "A project cannot be its own parent")
        if new_parent:
            visited: set[str] = set()
            cursor: str | None = new_parent
            while cursor:
                if cursor in visited or cursor == project_id:
                    raise HTTPException(400, "Move would create a project cycle")
                visited.add(cursor)
                parent_obj = store.get(cursor)
                if not parent_obj:
                    raise HTTPException(404, f"Parent project not found: {cursor}")
                cursor = parent_obj.parent_id or None
    project = store.update(project_id, updates)
    if not project:
        raise HTTPException(404, "Project not found")
    return project.model_dump(mode="json")


@router.delete("/{project_id}")
async def delete_project(project_id: str, _user = Depends(require_admin)):
    """Delete a project.

    The built-in ``default`` project is protected and cannot be deleted:
    new resources (alerts, schedules, workflows) fall back to
    ``project_id="default"`` when none is specified, so removing it
    cascades into 404s the moment anything is created without an
    explicit project. (2026-06-08: this guard was added after the
    restored e2e suite caught that deleting default silently broke
    alert/schedule creation.)

    Otherwise refuses if the project still contains pipelines,
    connections, or credentials — those have to be moved out first via
    the per-resource /move endpoints so nothing ends up orphaned to a
    non-existent project_id. The error response includes the counts so
    the UI can say "Move 13 pipelines first" instead of a generic
    failure.
    """
    store = get_store()
    if store.get(project_id) is None:
        raise HTTPException(404, "Project not found")

    # Protect the default project — deleting it cascades into broken
    # alert/schedule/workflow creation (everything that defaults to
    # project_id="default").
    if project_id == "default":
        raise HTTPException(
            409,
            "The default project cannot be deleted — new resources fall "
            "back to it when no project is specified.",
        )

    # Empty-check across all three child resource types.
    from fpulse.main import app_state
    wf_store = app_state.get("store")
    conn_store = app_state.get("connection_store")
    cred_store = app_state.get("credential_store")

    def _count_in(store_obj, attr_list: str) -> int:
        if store_obj is None:
            return 0
        try:
            rows = getattr(store_obj, attr_list)() or []
        except Exception:
            return 0
        return sum(
            1 for r in rows
            if (r.get("project_id") if isinstance(r, dict) else getattr(r, "project_id", None)) == project_id
        )

    pipeline_count = _count_in(wf_store, "list_all")
    connection_count = _count_in(conn_store, "list_all")
    credential_count = _count_in(cred_store, "list_all")

    if pipeline_count or connection_count or credential_count:
        raise HTTPException(409, {
            "message": "Project is not empty. Move its contents to another project first.",
            "pipelines": pipeline_count,
            "connections": connection_count,
            "credentials": credential_count,
        })

    if not store.delete(project_id):
        raise HTTPException(400, "Cannot delete this project")
    return {"deleted": True}


# ── Project archive / restore (2026-05-22) ─────────────────────────────
#
# Server-side replacement for the localStorage-only archive flag that
# used to live in ProjectsPage. The new lifecycle is auditable, shared
# across browsers / users, and survives reinstalls:
#
#   active   ──/archive──▶   archived   ──/restore──▶   active
#
# Archived projects:
#   - hidden from /projects?include_archived=false (default)
#   - listed by /projects?include_archived=true (Archived tab)
#   - cannot be selected as a workflow's project_id on create
#     (the workflow create validator will need an `active`-only check
#     in a follow-up; today the archived project still exists in the
#     DB so we keep that behaviour for now)
#
# Audit fields: archived_at, archived_by. Set on archive, cleared on
# restore. The audit doc (PROJECT_PIPELINE_CONFIGURATION_VALIDATION.md
# C1) called for retention policy + a server-side delete job — those
# are deliberately out of scope here; OSS keeps archived projects
# until an admin /delete's them. Plus is the right home for the
# retention scheduler if/when a customer asks.

@router.post("/{project_id}/archive")
async def archive_project(
    project_id: str,
    user = Depends(require_admin),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Mark a project archived (server-side, auditable).

    Replaces the old localStorage-only archive. Idempotent — archiving
    an already-archived project just refreshes `archived_at`/`archived_by`.

    Project pipelines / connections / credentials are NOT modified;
    they remain queryable and runnable. Archive is a visibility +
    governance signal, not a data operation. To stop runs of pipelines
    under an archived project, the operator can deactivate each
    pipeline's schedule.
    """
    store = get_store()
    proj = store.get(project_id)
    if proj is None:
        raise HTTPException(404, "Project not found")
    # Cross-workspace guard — same 404 as "not found" so existence
    # of tenant-other projects doesn't leak.
    if (getattr(proj, "workspace_id", None) or "default") != workspace_id:
        raise HTTPException(404, "Project not found")

    archived_by = getattr(user, "email", None) or getattr(user, "id", None) or "admin"
    updates = {
        "status": "archived",
        "archived_at": datetime.now(timezone.utc),
        "archived_by": archived_by,
    }
    updated = store.update(project_id, updates)
    if updated is None:
        raise HTTPException(404, "Project not found")
    return {
        "archived": True,
        "project": updated.model_dump(mode="json"),
    }


@router.post("/{project_id}/restore")
async def restore_project(
    project_id: str,
    _user = Depends(require_admin),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Restore an archived project to active state.

    Idempotent — restoring an already-active project is a no-op return.
    """
    store = get_store()
    proj = store.get(project_id)
    if proj is None:
        raise HTTPException(404, "Project not found")
    if (getattr(proj, "workspace_id", None) or "default") != workspace_id:
        raise HTTPException(404, "Project not found")

    updates = {
        "status": "active",
        "archived_at": None,
        "archived_by": None,
    }
    updated = store.update(project_id, updates)
    if updated is None:
        raise HTTPException(404, "Project not found")
    return {
        "restored": True,
        "project": updated.model_dump(mode="json"),
    }


@router.get("/{project_id}/pipelines")
async def list_project_pipelines(
    project_id: str,
    user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List all pipelines in a project.

    ACL mirrors the single-project GET — if the caller can't see the
    project, we return 404 (not 403) so project ids can't be enumerated.

    2026-05-22 (audit notes on E1): also scope the workflow lookup to
    the caller's workspace. The previous call to ``wf_store.list_all()``
    without ``workspace_id`` traversed every workspace's workflows
    before filtering by ``project_id``, which is wasteful on multi-
    tenant installs and (more importantly) could surface pipelines
    from another workspace if a project_id happened to collide.
    """
    from fpulse.projects.acl import assert_project_access
    # Loads + checks workspace + checks ACL in one helper. Returns
    # 404 (not 403) on any failure so project ids can't be enumerated.
    assert_project_access(project_id, workspace_id, user, action="read_pipelines")
    wf_store = get_workflow_store()
    workspace_wf = wf_store.list_all(workspace_id=workspace_id)
    return [w for w in workspace_wf if w.get("project_id", "default") == project_id]


@router.get("/{project_id}/export")
async def export_project(
    project_id: str,
    user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
    include_schedules: bool = False,
    include_alerts: bool = False,
):
    """Export a whole project as a portable, SECRET-SAFE JSON bundle for
    import into another instance (e.g. F-Pulse+ via POST /projects/import).

    Bundle contents:
      * project metadata (name/description/color/icon/metadata),
      * folder structure (flat tree),
      * every pipeline in the project, each via the SAME governance-stripped
        contract as GET /workflows/{id}/export (steps, connections,
        parameters, metadata; lifecycle/approval/deploy state excluded),
      * project-scoped variables with secret VALUES redacted to null,
      * connection REFERENCES only — an allowlist of non-secret fields
        (id/name/type/host/database/port). Connection ``config`` secret
        values are NEVER emitted, so the bundle can safely leave the box.

    The importer remaps connection references to the target instance's own
    saved connections (via ``connection_map``), so credentials are supplied
    on the destination — never carried in the file.

    ACL mirrors GET /projects/{id}/pipelines: 404 (not 403) on any access
    failure so project ids can't be enumerated.
    """
    from fpulse.projects.acl import assert_project_access
    from fpulse.api.workflows import build_pipeline_export_payload
    from fpulse.main import app_state

    assert_project_access(project_id, workspace_id, user, action="export")

    store = get_store()
    project = store.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    p = project.model_dump(mode="json")

    # ── Pipelines (full IR via the shared export contract) ──────────────
    wf_store = get_workflow_store()
    workspace_wf = wf_store.list_all(workspace_id=workspace_id)
    pipelines: list[dict] = []
    for w in workspace_wf:
        if w.get("project_id", "default") != project_id:
            continue
        wid = w.get("id")
        if not wid:
            continue
        v = wf_store.get(wid, workspace_id=workspace_id)
        if v is None:
            continue
        pipelines.append(build_pipeline_export_payload(
            v.workflow, workspace_id,
            include_schedules=include_schedules,
            include_alerts=include_alerts,
        ))

    # ── Project-scoped variables (secret values redacted to null) ───────
    variables: list[dict] = []
    try:
        var_store = app_state.get("variable_store")
        if var_store is not None:
            for var in var_store.list_all(
                scope="project", project_id=project_id, workspace_id=workspace_id,
            ):
                vd = dict(var)
                if vd.get("type") == "secret":
                    vd["value"] = None
                    vd["redacted"] = True
                variables.append(vd)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("project export: variables failed")

    # ── Connection REFERENCES (non-secret allowlist, mirrors exports.py) ─
    connections: list[dict] = []
    try:
        conn_store = app_state.get("connection_store")
        if conn_store is not None and hasattr(conn_store, "list_all"):
            for c in conn_store.list_all(workspace_id=workspace_id):
                if c.get("project_id") != project_id and c.get("scope") != "global":
                    continue
                cfg = c.get("config", {}) or {}
                connections.append({
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "type": c.get("type"),
                    "scope": c.get("scope"),
                    "project_id": c.get("project_id"),
                    "host": c.get("host") or cfg.get("host", ""),
                    "database": cfg.get("database", ""),
                    "port": cfg.get("port"),
                })
    except Exception:
        import logging
        logging.getLogger(__name__).exception("project export: connections failed")

    # ── Folder structure (best-effort; OSS folders are a flat tree) ─────
    folders: list[dict] = []
    try:
        folder_store = app_state.get("folder_store")
        if folder_store is not None and hasattr(folder_store, "list_all"):
            for f in folder_store.list_all(workspace_id=workspace_id):
                fd = f if isinstance(f, dict) else f.model_dump(mode="json")
                if fd.get("project_id") == project_id:
                    folders.append({
                        "id": fd.get("id"),
                        "name": fd.get("name"),
                        "parent_id": fd.get("parent_id"),
                        "project_id": fd.get("project_id"),
                    })
    except Exception:
        import logging
        logging.getLogger(__name__).exception("project export: folders failed")

    return {
        "fpulse_version": "1.0.0",
        "format_version": 1,
        "export_type": "project",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "project": {
            "name": p.get("name"),
            "description": p.get("description", ""),
            "color": p.get("color"),
            "icon": p.get("icon"),
            "metadata": p.get("metadata", {}),
        },
        "folders": folders,
        "pipelines": pipelines,
        "variables": variables,
        "connections": connections,
    }


# ═══════════════════════════════════════════════════════════════════════
# Project Approval Workflow
# ═══════════════════════════════════════════════════════════════════════

def _get_db():
    from fpulse.main import app_state
    return app_state["db"]


@router.post("/{project_id}/submit-for-approval")
async def submit_project_for_approval(
    project_id: str,
    user=Depends(require_auth),
    workspace_id: str = Depends(current_workspace_id),
):
    """Submit a newly created project for admin approval.
    Project status changes to 'pending' and admins are notified."""
    import json as _json
    from datetime import datetime, timezone
    store = get_store()
    project = store.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    project.approval_status = "pending"
    project.submitted_by = user.id if hasattr(user, 'id') else str(user)
    store.update(project_id, {
        "approval_status": "pending",
        "submitted_by": project.submitted_by,
    })

    # Create notification cards for admins
    try:
        db = _get_db()
        admin_cursor = db.execute_with_retry(
            "SELECT user_id FROM workspace_members WHERE workspace_id = ? AND role IN ('admin', 'lead', 'super_admin')",
            [workspace_id],
        )
        admin_rows = admin_cursor.fetchall() if admin_cursor else []
        recipients = [r[0] for r in admin_rows] if admin_rows else ["admin"]

        now = datetime.now(timezone.utc).isoformat()
        card_data = {
            "type": "project_approval",
            "project_id": project_id,
            "project_name": project.name,
            "description": project.description or "",
            "owner": project.owner,
            "color": project.color,
            "icon": project.icon,
            "members": project.members,
            "submitted_by": project.submitted_by,
            "submitted_at": now,
            "metadata": project.metadata,
        }

        for recipient in recipients:
            import uuid as _uuid
            nid = _uuid.uuid4().hex[:12]
            db.execute_with_retry(
                """INSERT INTO approval_notifications
                   (id, workflow_id, workflow_name, recipient_id, sender_id,
                    action, message, card_data, read, created_at, workspace_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                [nid, project_id, project.name, recipient, project.submitted_by,
                 "project_submitted",
                 f'New project "{project.name}" submitted for approval by {project.submitted_by}',
                 _json.dumps(card_data), now, workspace_id],
            )
    except Exception:
        pass  # Never block the action

    return {
        "id": project_id,
        "approval_status": "pending",
        "submitted_by": project.submitted_by,
    }


@router.post("/{project_id}/approve")
async def approve_project(
    project_id: str,
    notes: str = "",
    user=Depends(require_admin),
    workspace_id: str = Depends(current_workspace_id),
):
    """Approve a pending project. Admin-only."""
    import json as _json
    from datetime import datetime, timezone
    store = get_store()
    project = store.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if project.approval_status != "pending":
        raise HTTPException(400, f"Project is not pending approval (status: {project.approval_status or 'none'})")

    now = datetime.now(timezone.utc)
    approver = user.id if hasattr(user, 'id') else str(user)
    store.update(project_id, {
        "approval_status": "approved",
        "approved_by": approver,
        "approved_at": now.isoformat(),
        "approval_notes": notes,
    })

    # Notify the submitter
    try:
        db = _get_db()
        if project.submitted_by:
            import uuid as _uuid
            nid = _uuid.uuid4().hex[:12]
            db.execute_with_retry(
                """INSERT INTO approval_notifications
                   (id, workflow_id, workflow_name, recipient_id, sender_id,
                    action, message, card_data, read, created_at, workspace_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                [nid, project_id, project.name, project.submitted_by, approver,
                 "project_approved",
                 f'Project "{project.name}" has been approved by {approver}' + (f": {notes}" if notes else ""),
                 _json.dumps({"project_id": project_id, "project_name": project.name, "status": "approved", "approved_by": approver, "notes": notes}),
                 now.isoformat(), workspace_id],
            )
    except Exception:
        pass

    return {"id": project_id, "approval_status": "approved", "approved_by": approver}


@router.post("/{project_id}/reject")
async def reject_project(
    project_id: str,
    notes: str = "",
    user=Depends(require_admin),
    workspace_id: str = Depends(current_workspace_id),
):
    """Reject a pending project with feedback. Admin-only."""
    import json as _json
    from datetime import datetime, timezone
    store = get_store()
    project = store.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if project.approval_status != "pending":
        raise HTTPException(400, "Project is not pending approval")

    rejector = user.id if hasattr(user, 'id') else str(user)
    store.update(project_id, {
        "approval_status": "rejected",
        "approval_notes": notes,
    })

    # Notify the submitter
    try:
        db = _get_db()
        if project.submitted_by:
            import uuid as _uuid
            nid = _uuid.uuid4().hex[:12]
            now = datetime.now(timezone.utc).isoformat()
            db.execute_with_retry(
                """INSERT INTO approval_notifications
                   (id, workflow_id, workflow_name, recipient_id, sender_id,
                    action, message, card_data, read, created_at, workspace_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                [nid, project_id, project.name, project.submitted_by, rejector,
                 "project_rejected",
                 f'Project "{project.name}" was rejected: {notes}',
                 _json.dumps({"project_id": project_id, "project_name": project.name, "status": "rejected", "rejected_by": rejector, "notes": notes}),
                 now, workspace_id],
            )
    except Exception:
        pass

    return {"id": project_id, "approval_status": "rejected", "notes": notes}
