"""Workflow CRUD + versioning + lifecycle API."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from fpulse.ir.schema import Workflow, PipelineStatus
from fpulse.ir.validator import validate_workflow, validate_capabilities
from fpulse.ir.plan_diff import compute_plan_diff
from fpulse.auth.deps import current_workspace_id, require_auth, require_min_rank

# 2026-05-30 (Track S P1): router-level require_auth blocks anonymous
# callers on every route below. Write endpoints add require_min_rank
# at the route level so analyst/viewer get 403 on mutations while
# data_engineer+ still authors. Reads stay at require_auth so any
# logged-in role can list / inspect.
_AUTH = Depends(require_auth)
# 2026-05-30 (P7 follow-up): gate writes at developer rank (50) so any
# role at or above developer can author — data_engineer (70), admin
# (90), workspace_admin (90), super_admin (100). analyst (30) and
# viewer (10) are still blocked. The earlier "data_engineer" gate was
# overly restrictive: developer-tier collaborators couldn't write
# their own pipelines.
_AUTHOR = Depends(require_min_rank("developer"))

router = APIRouter(
    prefix="/api/workflows",
    tags=["workflows"],
    dependencies=[_AUTH],
)


def _safe_workspace_id(request: Request) -> str:
    """Wrap current_workspace_id so dep failures surface as readable
    HTTP errors — same pattern as api/projects.py."""
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


# In-memory store (injected at startup). These thin wrappers delegate to
# fpulse.state so a missing entry surfaces as a clear RuntimeError rather
# than a bare KeyError (2026-05-22 — see fpulse/state.py docstring).
def get_store():
    from fpulse.state import get_workflow_store
    return get_workflow_store()


def get_data_dir():
    from fpulse.main import app_state
    # data_dir is a string path, not a wired service — the helpers in
    # fpulse.state are reserved for services that get raise-on-missing
    # semantics. Bare read is fine here.
    return app_state["data_dir"]


def get_execution_store():
    from fpulse.state import get_execution_store as _impl
    return _impl()


def get_lifecycle_store():
    from fpulse.main import app_state
    return app_state["lifecycle_store"]


def _get_db():
    from fpulse.state import get_db
    return get_db()


class WorkflowCreate(BaseModel):
    """Body for POST /api/workflows.

    2026-05-22: extended with project_id / folder_id / parameters so
    pipeline creation is **atomic into a project**. Previously the
    frontend created with name+description, then immediately PUT the
    workflow with project_id — two round-trips and a window where a
    crash between them left the pipeline in the default project. The
    audit (PROJECT_PIPELINE_CONFIGURATION_VALIDATION.md B3) called
    this out.

    2026-05-30 (W4): also accepts `steps` + `connections` so a single
    POST can land a fully-formed workflow in one round-trip. Older
    e2e tests, the CLI `fpulse import`, and templated drafts use this
    shape. When omitted, an empty workflow is created exactly as
    before (canvas-first creation flow).

    All extras are optional so existing clients (programmatic creates,
    template imports) keep working unchanged.
    """
    name: str = "Untitled Pipeline"
    description: str = ""
    project_id: str | None = None
    folder_id: str | None = None
    # Use the same shape the Workflow model uses so callers can paste
    # a parameter list straight in without translation.
    parameters: list[dict] | None = None
    # W4 — full Workflow shape for one-shot creation.
    steps: list[dict] | None = None
    connections: list[dict] | None = None


class WorkflowUpdate(BaseModel):
    workflow: Workflow
    change_summary: str = ""


def _parse_workflow_update(raw: dict) -> WorkflowUpdate:
    """W1 (2026-05-30) — accept both update body shapes:

      1. ``{"workflow": {...}, "change_summary": "..."}``  (canonical)
      2. ``{...Workflow blob...}``                          (plain)

    The plain shape is the historical contract — older e2e tests, the
    CLI ``fpulse import``, and some templated workflow drafts all send
    just the Workflow blob with no wrapper. Forcing the wrapper now
    returned 422 to every one of those callers, which is too disruptive.

    Detection rule: if the body has a top-level ``steps`` key (every
    Workflow has it) AND no top-level ``workflow`` key, treat it as
    the plain shape. Otherwise validate as the wrapper.

    Raises HTTPException(400) on a body that fits neither shape — same
    HTTP semantics as Pydantic's 422 but with a friendlier message.
    """
    looks_plain = (
        isinstance(raw, dict)
        and "steps" in raw
        and "workflow" not in raw
    )
    if looks_plain:
        # Pull change_summary out if the plain body smuggled one alongside.
        summary = raw.pop("change_summary", "") if isinstance(raw, dict) else ""
        return WorkflowUpdate(workflow=Workflow(**raw), change_summary=summary)
    try:
        return WorkflowUpdate(**raw)
    except Exception as exc:
        raise HTTPException(
            400,
            {
                "code": "invalid_workflow_body",
                "message": (
                    "PUT /api/workflows/{id} accepts either "
                    "{workflow: {...}, change_summary: '...'} OR the "
                    "Workflow blob directly. Neither shape matched. "
                    f"Detail: {exc}"
                ),
            },
        ) from exc


# 2026-05-30 (P7): alias the trailing-slash form so requests to
# /api/workflows (no slash) don't 307-redirect — httpx TestClient
# defaults to follow_redirects=False, so the 307 was masking the
# auth gate that lives on the canonical "/" path. include_in_schema
# keeps OpenAPI clean.
@router.get("", include_in_schema=False)
@router.get("/")
async def list_workflows(
    request: Request,
    project_id: str | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List all workflows visible in the caller's current workspace.

    When ``project_id`` is provided, only workflows belonging to that
    project are returned — used by the Pipelines page when a project
    context is active.

    2026-05-22 (audit E1) — when ``project_id`` is supplied we ALSO
    enforce project ACL via ``assert_project_access``. Previously a
    caller could list pipelines from any workspace project they
    weren't a member of, just by passing that project_id. Workspace
    boundary already held, but project-level ACL was leaking.

    For the unfiltered case (``project_id is None``) we fall back to
    workspace scope + per-row ACL filtering so users only see rows
    in projects they can access.
    """
    from fpulse.projects.acl import assert_project_access, can_see_project
    from fpulse.auth.deps import current_user_optional

    user = current_user_optional(request)
    all_wfs = get_store().list_all(workspace_id=workspace_id)

    if project_id:
        # ACL gate on the explicit project — 404s if the user can't
        # see the project, hiding its existence.
        if user is not None:
            assert_project_access(project_id, workspace_id, user, action="list_workflows")
        return [w for w in all_wfs if w.get("project_id") == project_id]

    # Unfiltered list — drop rows whose project the caller can't see.
    # Anonymous / OSS-no-auth callers get every workspace row (legacy
    # behaviour preserved). When a user is present, apply per-row
    # filter against project ACL.
    if user is None:
        return all_wfs

    try:
        from fpulse.main import app_state
        project_store = app_state.get("project_store")
    except Exception:
        project_store = None

    if project_store is None:
        return all_wfs

    # Cache project lookups to keep this O(N + unique projects)
    project_cache: dict[str, dict | None] = {}

    def _can_see_row(row_project_id: str) -> bool:
        if not row_project_id or row_project_id == "default":
            return True
        if row_project_id not in project_cache:
            proj = project_store.get(row_project_id)
            project_cache[row_project_id] = (
                proj.model_dump(mode="json") if proj else None
            )
        pd = project_cache[row_project_id]
        return pd is not None and can_see_project(user, pd)

    return [w for w in all_wfs if _can_see_row(w.get("project_id", "default"))]


@router.post("", dependencies=[_AUTHOR], include_in_schema=False)
@router.post("/", dependencies=[_AUTHOR])
async def create_workflow(
    body: WorkflowCreate,
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create a new empty workflow in the caller's current workspace.

    Rejects the placeholder name "Untitled Pipeline" with 400 so the
    workflows list never silently collects anonymous rows (rule locked
    2026-05-09). Real names are still auto-suffixed for uniqueness.

    2026-05-22 — accepts project_id / folder_id / parameters atomically
    (audit B3) and gates them via project ACL (audit E1) when the
    caller is an authenticated user.
    """
    # Local import — current_user_optional is referenced via the
    # already-existing import block at the top of the module.
    from fpulse.auth.deps import current_user_optional
    raw_name = (body.name or "").strip()
    if not raw_name or raw_name.lower() == "untitled pipeline":
        raise HTTPException(
            status_code=400,
            detail=(
                "Pipeline name is required and cannot be 'Untitled Pipeline'. "
                "Give it a descriptive name before saving."
            ),
        )

    # ── Optional project + folder validation (2026-05-22, audit B3+E1) ─
    # If the caller supplies project_id / folder_id, both must live in
    # the caller's workspace AND be visible per project ACL. Reject
    # cross-tenant / cross-ACL assignment at create time rather than
    # letting the workflow land in default project and orphan the
    # user's intent.
    target_project_id = (body.project_id or "").strip() or "default"
    target_folder_id = (body.folder_id or None)
    if target_project_id and target_project_id != "default":
        from fpulse.projects.acl import assert_project_access
        try:
            user = current_user_optional(request)
            if user is not None:
                # 404 on workspace mismatch OR ACL miss — same shape
                # so a caller can't enumerate project ids they don't
                # have access to.
                assert_project_access(
                    target_project_id, workspace_id, user,
                    action="create_workflow",
                )
            else:
                # No auth context (legacy / programmatic) — fall back
                # to the workspace boundary check only.
                from fpulse.main import app_state
                project_store = app_state.get("project_store")
                if project_store is not None:
                    proj = project_store.get(target_project_id)
                    if proj is None:
                        raise HTTPException(404, f"Project '{target_project_id}' not found")
                    proj_ws = getattr(proj, "workspace_id", None) or "default"
                    if proj_ws != workspace_id:
                        raise HTTPException(404, f"Project '{target_project_id}' not found")
        except HTTPException:
            raise
        except Exception:
            # Store-missing path is best-effort — fall through and let
            # the workflow create complete in default project so a
            # store init order bug doesn't block every workflow create.
            pass
    if target_folder_id:
        try:
            from fpulse.main import app_state
            folder_store = app_state.get("folder_store")
            if folder_store is not None:
                folder = folder_store.get(target_folder_id, workspace_id=workspace_id)
                if folder is None:
                    raise HTTPException(404, f"Folder '{target_folder_id}' not found in this workspace")
                # Folder's project must match — can't park a folder
                # under project A but assign workflow into project B.
                folder_proj = getattr(folder, "project_id", None) or "default"
                if folder_proj != target_project_id:
                    raise HTTPException(
                        400,
                        f"Folder '{target_folder_id}' belongs to project "
                        f"'{folder_proj}', not '{target_project_id}'.",
                    )
        except HTTPException:
            raise
        except Exception:
            pass

    # Pydantic-validate any supplied parameter blob through the real
    # WorkflowParameter model so we never persist a malformed list.
    parameters = []
    if body.parameters:
        from fpulse.ir.schema import WorkflowParameter as _WP
        try:
            parameters = [_WP(**p) for p in body.parameters]
        except Exception as exc:
            raise HTTPException(400, f"Invalid parameters: {exc}")

    # W4 (2026-05-30) — one-shot creation. If the body supplied steps
    # / connections, validate them through the real models so the
    # persisted blob is well-formed. Otherwise create empty
    # (canvas-first flow).
    from fpulse.ir.schema import Step as _Step, StepConnection as _StepConn
    steps_in: list = []
    if body.steps:
        try:
            steps_in = [_Step(**s) for s in body.steps]
        except Exception as exc:
            raise HTTPException(400, f"Invalid steps[]: {exc}")
    connections_in: list = []
    if body.connections:
        try:
            connections_in = [_StepConn(**c) for c in body.connections]
        except Exception as exc:
            raise HTTPException(400, f"Invalid connections[]: {exc}")

    wf = Workflow(
        name=raw_name,
        description=body.description,
        workspace_id=workspace_id,
        project_id=target_project_id,
        folder_id=target_folder_id,
        parameters=parameters,
        steps=steps_in,
        connections=connections_in,
    )
    # store.save() handles dedupe (auto-suffix) and is the single
    # chokepoint that re-validates the no-placeholder rule.
    version = get_store().save(wf, change_summary="Initial creation")
    return {"id": wf.id, "version": version.version, "workflow": wf.model_dump(mode="json")}


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    version: int | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Get a workflow (latest or specific version) from the caller's
    current workspace. Returns 404 if the workflow lives in a
    different workspace — deliberately the same error as "does not
    exist" so the existence of tenant-other pipelines doesn't leak.
    """
    v = get_store().get(workflow_id, version, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")
    return {
        "version": v.version,
        "workflow": v.workflow.model_dump(mode="json"),
        "created_by": v.created_by,
        "created_at": v.created_at.isoformat(),
        "change_summary": v.change_summary,
    }


@router.put("/{workflow_id}", dependencies=[_AUTHOR])
async def update_workflow(
    workflow_id: str,
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
):
    # W1 (2026-05-30) — body parsed manually so we can accept either
    # the wrapper {workflow, change_summary} OR a plain Workflow blob.
    # See _parse_workflow_update docstring for rationale.
    body = _parse_workflow_update(await request.json())
    """Save a new version of a workflow. Refuses to let a caller
    reassign a workflow into a different workspace — the workspace
    of the workflow is derived from the existing row, not from the
    request body, so a compromised client can't smuggle pipelines
    across tenant boundaries by editing the blob.
    """
    # Load existing to confirm it belongs to this workspace AND to
    # pin the workspace_id on the new version.
    existing = get_store().get(workflow_id, workspace_id=workspace_id)
    if not existing:
        raise HTTPException(404, "Workflow not found")

    body.workflow.id = workflow_id
    body.workflow.workspace_id = existing.workflow.workspace_id or workspace_id

    # ── Lifecycle field protection (2026-05-22) ─────────────────────────
    # Generic update is the path the canvas Save / Toolbar Save / Save
    # Dialog all use. Until now it accepted the entire Workflow blob,
    # which let a client set status=published, approval_status=approved,
    # deployed_version=N, etc. directly — bypassing the /test, /publish,
    # /submit-for-review, /approve, /deploy endpoints that are supposed
    # to be the only mutation paths for lifecycle state.
    #
    # The 2026-05-22 audit (docs/PROJECT_PIPELINE_CONFIGURATION_VALIDATION.md
    # finding A1) called this out as the highest-impact governance gap.
    #
    # Policy: any lifecycle-state field on the body is silently
    # overwritten with the existing value. We do NOT 400 the request,
    # because clients (SaveDialog before its own fix lands, AI drafts,
    # template imports) routinely send a status field as part of the
    # full workflow blob. 400-ing every save until they're all fixed
    # would break the UI; overwriting is the lower-friction landing.
    # If a caller really needs to change lifecycle state, the dedicated
    # endpoints (/test, /publish, /submit-for-review, /approve,
    # /submit-for-deploy, /approve-deploy, /deploy, /rollback) are
    # still there and authoritative.
    _SERVER_OWNED_FIELDS = (
        "status",
        "published_at", "published_by",
        "deployed_version", "deployed_at", "deployed_by",
        "approved_by", "approved_at",
        "approval_status", "approval_stage",
        "test_results",
        "owner_id",
        "created_at",
    )
    for field in _SERVER_OWNED_FIELDS:
        if hasattr(existing.workflow, field):
            setattr(body.workflow, field, getattr(existing.workflow, field))

    errors = validate_workflow(body.workflow)
    # Capability check: source nodes must use a read-capable connection,
    # sink nodes a write-capable one. Lazy-fetches each unique conn id at
    # most once per save — see validate_capabilities for the cache rule.
    try:
        from fpulse.main import app_state
        conn_store = app_state.get("connection_store")
        if conn_store is not None:
            errors.extend(validate_capabilities(
                body.workflow,
                lambda cid: conn_store.get(cid, workspace_id=workspace_id),
            ))
    except Exception:
        # Capability check is best-effort — if the store is missing for
        # any reason we don't want to block saves on it.
        pass

    warnings = [e.dict() for e in errors if e.severity == "warning"]
    hard_errors = [e.dict() for e in errors if e.severity == "error"]

    version = get_store().save(body.workflow, change_summary=body.change_summary)
    return {
        "version": version.version,
        "workflow": version.workflow.model_dump(mode="json"),
        "warnings": warnings,
        "errors": hard_errors,
    }


@router.post("/{workflow_id}/move", dependencies=[_AUTHOR])
async def move_workflow(
    workflow_id: str,
    target_project_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Reassign a workflow to a different project.

    Cheap operation — just edits the project_id pointer; the workflow
    IR + version history stay intact. Used by the Pipelines table's
    "Move to project" action and (transitively) by the Project delete
    flow when the user asks to drain a project before deleting it.

    Validates that ``target_project_id`` exists in this workspace so
    we can't orphan a workflow into a non-existent project.
    """
    existing = get_store().get(workflow_id, workspace_id=workspace_id)
    if not existing:
        raise HTTPException(404, "Workflow not found")

    from fpulse.main import app_state
    proj_store = app_state.get("project_store")
    if proj_store is not None:
        proj = proj_store.get(target_project_id)
        if proj is None:
            raise HTTPException(404, f"Target project '{target_project_id}' does not exist")

    wf = existing.workflow
    wf.project_id = target_project_id
    version = get_store().save(wf, change_summary=f"Moved to project {target_project_id}")
    return {
        "moved": True,
        "workflow_id": workflow_id,
        "project_id": target_project_id,
        "version": version.version,
    }


@router.delete("/{workflow_id}", dependencies=[_AUTHOR])
async def delete_workflow(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Delete a workflow. Scoped to the caller's workspace — a
    workflow in a different workspace returns 404 (not 403) to
    avoid leaking its existence.
    """
    existing = get_store().get(workflow_id, workspace_id=workspace_id)
    if not existing:
        raise HTTPException(404, "Workflow not found")
    if not get_store().delete(workflow_id):
        raise HTTPException(404, "Workflow not found")
    # Purge the on-disk step cache so stale parquet files don't linger
    # in the data dir after the workflow is gone.
    try:
        from fpulse.engine.step_cache import StepCache
        StepCache(get_data_dir(), workflow_id).clear()
    except Exception:
        pass

    # Drop any Postgres logical replication slots the workflow's CDC
    # sources created — an orphaned slot pins WAL on the source server
    # until its disk fills.
    try:
        from fpulse.connectors.cdc import cleanup_workflow_cdc_slots
        cleanup_workflow_cdc_slots(existing.workflow)
    except Exception:
        pass

    # P0 Day 5 (2026-05-23) — audit destructive workflow action.
    try:
        from fpulse.audit import audit_action
        audit_action(
            action="workflow.delete",
            resource_type="workflow",
            resource_id=workflow_id,
            workspace_id=workspace_id,
            details={
                "name": getattr(existing.workflow, "name", None) or "(unnamed)",
                "status": getattr(existing.workflow, "status", None),
            },
        )
    except Exception:
        pass

    return {"deleted": True}


@router.get("/{workflow_id}/versions")
async def list_versions(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List all versions of a workflow in the caller's workspace."""
    existing = get_store().get(workflow_id, workspace_id=workspace_id)
    if not existing:
        raise HTTPException(404, "Workflow not found")
    return get_store().get_versions(workflow_id)


@router.get("/{workflow_id}/diff")
async def diff_versions(
    workflow_id: str,
    v1: int,
    v2: int,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Diff two versions of a workflow (scoped to caller's workspace)."""
    existing = get_store().get(workflow_id, workspace_id=workspace_id)
    if not existing:
        raise HTTPException(404, "Workflow not found")
    result = get_store().diff(workflow_id, v1, v2)
    if not result:
        raise HTTPException(404, "Version not found")
    return result


@router.get("/{workflow_id}/parameter-usage")
async def parameter_usage(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List which steps reference each declared workflow parameter.

    UI calls this before deleting a parameter so it can warn the user
    about every step that would break. Returns
    `{<param_name>: [<step_id>, ...]}` — empty dict means no parameter
    is referenced anywhere (safe to delete any of them).
    """
    existing = get_store().get(workflow_id, workspace_id=workspace_id)
    if not existing or not existing.workflow:
        raise HTTPException(404, "Workflow not found")
    from fpulse.engine.parameters import find_parameter_references
    return find_parameter_references(existing.workflow)


@router.post("/{workflow_id}/validate")
async def validate(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Validate the latest version of a workflow (scoped to caller's
    workspace)."""
    v = get_store().get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")
    errors = validate_workflow(v.workflow)
    return {
        "valid": len([e for e in errors if e.severity == "error"]) == 0,
        "errors": [e.dict() for e in errors],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Preflight (#14, V14 of the F-Pulse product vision)
#
# Runs validate_workflow() + the structural checks in fpulse.ir.preflight:
#   - empty-pipeline / orphaned-nodes
#   - transform-without-input
#   - sink-without-source
#   - unconnected-source (warning)
#
# Shape mirrors /validate but adds `warnings` (advisory, doesn't block Save)
# and stable `code` strings on every finding so the editor can branch by
# finding type. Designed to be called on Save and on Run, before any
# side-effect happens.
#
# The ephemeral variant (POST /api/workflows/preflight, body: {workflow: ...})
# lets the editor preflight an unsaved IR without touching the store.
# ─────────────────────────────────────────────────────────────────────────────


def _run_preflight(workflow: Workflow) -> dict:
    """Shared core for both preflight endpoints.

    Merges `validate_workflow()` findings (which use a free-form
    `message` per the legacy ValidationError shape) with
    `preflight_workflow()` findings (which carry stable codes).
    """
    from fpulse.ir.preflight import preflight_workflow

    legacy_errors = validate_workflow(workflow)
    structural = preflight_workflow(workflow)

    findings: list[dict] = []
    # Lift legacy ValidationError to the new shape so the response is
    # uniform across the two sources. Legacy findings get the generic
    # "validation" code; new code in callers should prefer the
    # PreflightCode constants from `fpulse.ir.preflight`.
    for e in legacy_errors:
        findings.append(
            {
                "step_id": e.step_id,
                "code": "validation",
                "severity": e.severity,
                "message": e.message,
            }
        )
    findings.extend(structural)

    errors = [f for f in findings if f["severity"] == "error"]
    warnings = [f for f in findings if f["severity"] == "warning"]
    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "count": len(findings),
    }


@router.post("/{workflow_id}/preflight")
async def preflight_saved(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Preflight a saved workflow before Save / Run.

    Returns {valid, errors[], warnings[], count}. Each finding has
    {step_id, code, severity, message} — branch the UI by `code`,
    show the message verbatim.
    """
    v = get_store().get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")
    return _run_preflight(v.workflow)


class PreflightRequest(BaseModel):
    """Body for the ephemeral preflight — the unsaved IR the editor
    is currently working on. Lets the user catch problems before the
    first Save without round-tripping through the store."""

    workflow: Workflow


@router.post("/preflight")
async def preflight_ephemeral(body: PreflightRequest):
    """Preflight an unsaved workflow IR (used by the editor)."""
    return _run_preflight(body.workflow)


# ─────────────────────────────────────────────────────────────────────────────
# Schema Propagation Loop (PR 1) — live column-schema lookup per step.
#
# The frontend ConfigPanel and Data Wrangler call this when the user opens a
# step's config so the column-name dropdowns always reflect what's *actually*
# flowing in after every upstream Rename / Typecast / Derived-column. Fixes
# the silent "broken pipeline" bugs where a step references a column name
# that no longer exists post-upstream-rename.
#
# Cost: runs the upstream subgraph with preview_limit=0 (DuckDB materialises
# the relation but not the rows). Sources may still need to open a file or
# probe a remote, so a 200ms call is realistic on a clean cache; subsequent
# calls hit the StepCache.
#
# Accepts both:
#   GET  /api/workflows/{wf_id}/step/{step_id}/schema    — uses stored IR
#   POST /api/workflows/{wf_id}/step/{step_id}/schema    — uses request body IR
# The POST form lets the editor evaluate an unsaved-but-edited workflow.
# ─────────────────────────────────────────────────────────────────────────────


class StepSchemaRequest(BaseModel):
    """Body for the POST form — the (possibly unsaved) workflow IR the
    frontend is currently editing. Lets the schema lookup reflect edits
    the user hasn't saved yet."""
    workflow: Workflow


def _run_schema_lookup(workflow: Workflow, step_id: str) -> dict:
    """Shared core for GET + POST schema endpoints."""
    from fpulse.engine.executor import WorkflowExecutor
    from fpulse.main import app_state as _app_state

    data_dir = _app_state.get("data_dir", ".")
    executor = WorkflowExecutor(data_dir=data_dir, app_state=_app_state)
    try:
        return executor.get_step_input_schema(workflow, step_id)
    except ValueError as exc:
        # Unknown step_id — surface as 404 so the UI can degrade gracefully.
        raise HTTPException(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        import traceback
        logger.warning(
            "step schema lookup failed (workflow=%s step=%s): %s\n%s",
            workflow.id, step_id, exc, traceback.format_exc(),
        )
        # Schema-lookup failures are non-fatal — the UI should still let
        # the user type a column name; it just loses autocomplete.
        return {
            "step_id": step_id,
            "is_source": False,
            "inputs": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


@router.get("/{workflow_id}/step/{step_id}/schema")
async def get_step_schema(
    workflow_id: str,
    step_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Return the column schema flowing INTO `step_id` for the saved IR.

    Use the POST form below if the user has unsaved edits on the canvas.
    """
    v = get_store().get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")
    return _run_schema_lookup(v.workflow, step_id)


@router.get("/{workflow_id}/step/{step_id}/expected-schema")
async def get_step_expected_output_schema(
    workflow_id: str,
    step_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Predict what columns `step_id` would EMIT, without running it.

    R5 (2026-05-30) — schema preview without execution. Walks the DAG
    in topological order and calls each node class's static
    ``expected_output_schema(input_schemas, params)`` hook. Nodes that
    return None (don't know without running) are reported with
    ``status: 'unknown'`` so the UI can show "schema computed on first
    run" instead of blank.

    Response shape:
        {
          step_id, status: 'ok' | 'unknown' | 'not_found',
          columns: [{name, type}, ...],
          message?: str,
          chain: [{step_id, status}, ...]  # upstream chain
        }
    """
    v = get_store().get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    workflow = v.workflow
    steps_by_id = {s.id: s for s in workflow.steps}
    if step_id not in steps_by_id:
        raise HTTPException(404, f"Step '{step_id}' not found in workflow")

    # Build upstream map from edges (source -> [target,...])
    upstream: dict[str, list[str]] = {s.id: [] for s in workflow.steps}
    for edge in getattr(workflow, "edges", []) or []:
        src = getattr(edge, "source", None)
        tgt = getattr(edge, "target", None)
        if src and tgt and tgt in upstream:
            upstream[tgt].append(src)

    from fpulse.nodes.registry import NodeRegistry

    # Memoised recursive walk — predict each upstream step before
    # the current one. Cycles fall through to 'unknown' via the
    # _visiting set.
    cache: dict[str, dict] = {}
    visiting: set[str] = set()

    def _predict(sid: str) -> dict:
        if sid in cache:
            return cache[sid]
        if sid in visiting:
            return {"step_id": sid, "status": "unknown",
                    "columns": [], "message": "cycle detected"}
        visiting.add(sid)
        step = steps_by_id.get(sid)
        if step is None:
            visiting.discard(sid)
            return {"step_id": sid, "status": "not_found",
                    "columns": [], "message": "step missing"}

        # Predict upstream first
        upstream_predictions = [_predict(u) for u in upstream.get(sid, [])]
        input_schemas = [p["columns"] for p in upstream_predictions]

        try:
            cls = NodeRegistry.get(step.type)
        except Exception:
            visiting.discard(sid)
            cache[sid] = {"step_id": sid, "status": "unknown",
                          "columns": [], "message": "node class not registered"}
            return cache[sid]

        params = step.params or {}
        predictor = getattr(cls, "expected_output_schema", None)
        result: dict = {"step_id": sid, "status": "unknown", "columns": [],
                        "message": "node doesn't implement expected_output_schema yet"}
        if callable(predictor):
            try:
                cols = predictor(input_schemas, params)
                if cols is None:
                    result["message"] = "schema deferred to runtime (e.g. dynamic remote response)"
                else:
                    result = {"step_id": sid, "status": "ok",
                              "columns": list(cols)}
            except Exception as exc:  # noqa: BLE001 — never fail the preview
                result = {"step_id": sid, "status": "unknown",
                          "columns": [], "message": f"prediction crashed: {exc}"}
        visiting.discard(sid)
        cache[sid] = result
        return result

    final = _predict(step_id)
    chain = [
        {"step_id": s.id, "status": _predict(s.id)["status"]}
        for s in workflow.steps if s.id in cache
    ]
    final["chain"] = chain
    return final


@router.post("/{workflow_id}/step/{step_id}/schema")
async def get_step_schema_for_unsaved(
    workflow_id: str,
    step_id: str,
    body: StepSchemaRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Same as the GET form, but evaluates the workflow IR the caller
    supplies in the body — so the editor can ask "what columns are
    available at step X if I save these unsaved edits?" without
    round-tripping a save first."""
    # Stamp workspace_id on the inbound workflow so it can't probe another
    # tenant's data by spoofing the path's workspace.
    wf = body.workflow.model_copy(update={"workspace_id": workspace_id})
    return _run_schema_lookup(wf, step_id)


class WorkflowPlanRequest(BaseModel):
    """Body for the plan endpoint — the proposed Workflow IR the user is
    about to save / submit. The current version is read from the DB,
    never trusted from the request."""
    workflow: Workflow


@router.post("/{workflow_id}/plan")
async def plan_workflow(
    workflow_id: str,
    body: WorkflowPlanRequest,
    against: str = "latest",
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Pre-save / pre-deploy plan: what changes if this workflow IR is
    saved (or submitted / deployed) on top of the chosen baseline.

    `against=latest` (default): diff against the latest saved version —
        used by the Save action to preview the next save.
    `against=deployed`: diff against the currently-deployed version —
        used by Submit-for-Review and Deploy so the approver sees the
        full delta they are about to ship to PROD.

    Memory budget per the production-readiness plan: ≤ 50 MB. Achieved by
    (a) loading only one baseline version row, (b) capping every diff
    list at 200 items, (c) capping baseline analysis at 20 recent runs,
    (d) running the validator inline (already bounded). Nothing is
    cached — each call is a clean read.
    """
    store = get_store()
    existing = store.get(workflow_id, workspace_id=workspace_id)
    if not existing:
        raise HTTPException(404, "Workflow not found")

    # Pin identity / workspace from the stored row, not the request body
    # — same anti-tenant-smuggling rule as update_workflow.
    body.workflow.id = workflow_id
    body.workflow.workspace_id = existing.workflow.workspace_id or workspace_id

    # Pick the baseline. `deployed` falls back to latest if nothing is
    # currently deployed (first-time-publish case) so the approver still
    # sees a useful diff.
    baseline_workflow = existing.workflow
    baseline_label = "latest"
    baseline_version = existing.version
    if against == "deployed":
        deployed_v = existing.workflow.deployed_version
        if deployed_v:
            dv = store.get(workflow_id, version=deployed_v, workspace_id=workspace_id)
            if dv:
                baseline_workflow = dv.workflow
                baseline_label = "deployed"
                baseline_version = dv.version

    diff = compute_plan_diff(baseline_workflow, body.workflow)

    # Validator on the proposed IR — same checks the save endpoint runs,
    # so the approver sees the exact errors that would block the save.
    errors = validate_workflow(body.workflow)
    try:
        from fpulse.main import app_state
        conn_store = app_state.get("connection_store")
        if conn_store is not None:
            errors.extend(validate_capabilities(
                body.workflow,
                lambda cid: conn_store.get(cid, workspace_id=workspace_id),
            ))
    except Exception:
        pass

    validator = {
        "errors":   [e.dict() for e in errors if e.severity == "error"],
        "warnings": [e.dict() for e in errors if e.severity == "warning"],
    }

    # Hash deltas — the approver sees the same short pill they'd see on
    # the Deployments page after the save lands.
    from fpulse.ir.versioning import compute_workflow_hash
    try:
        current_hash = compute_workflow_hash(baseline_workflow)
    except Exception:
        current_hash = ""
    try:
        proposed_hash = compute_workflow_hash(body.workflow)
    except Exception:
        proposed_hash = ""

    # Execution baseline — bounded scan of the last 20 runs. Iterates the
    # rows, doesn't accumulate the full step_logs payload.
    baseline = _execution_baseline(workflow_id, workspace_id)

    # Connection ref enrichment — turn raw IDs into {name, type} so the
    # approver doesn't squint at hex strings. Lookup is per-unique-id and
    # capped by diff size, so memory stays small.
    diff["connection_refs"] = _enrich_connection_refs(
        diff["connection_refs"], workspace_id,
    )

    return {
        "workflow_id": workflow_id,
        "baseline_kind": baseline_label,
        "baseline_version": baseline_version,
        "current_version": existing.version,
        "deployed_version": existing.workflow.deployed_version,
        "current_hash": current_hash,
        "proposed_hash": proposed_hash,
        "hash_changed": (current_hash != proposed_hash) if (current_hash and proposed_hash) else True,
        "diff": diff,
        "validator": validator,
        "baseline": _execution_baseline(workflow_id, workspace_id),
    }


def _execution_baseline(workflow_id: str, workspace_id: str) -> dict:
    """Last-20-runs summary for the proposed workflow. Single query,
    bounded slice, no per-step accumulation."""
    try:
        runs = get_execution_store().list_by_workflow(
            workflow_id, limit=20, workspace_id=workspace_id,
        )
    except Exception:
        return {"runs_analyzed": 0}
    if not runs:
        return {"runs_analyzed": 0}

    durations: list[float] = []
    rows_seen: list[int] = []
    last_status: str | None = None
    last_at: str | None = None
    for r in runs:
        d = r.get("duration_ms")
        if isinstance(d, (int, float)) and d > 0:
            durations.append(float(d))
        # rows_processed sums across step_logs — but step_logs is heavy.
        # Use the cheaper top-level field if it exists; otherwise skip
        # (some legacy rows won't carry it, and we'd rather show "—" than
        # walk every step_log).
        rp = r.get("rows_processed_total") or r.get("rows_processed")
        if isinstance(rp, int) and rp > 0:
            rows_seen.append(rp)
        if last_status is None:
            last_status = r.get("status")
            last_at = r.get("started_at")

    avg_duration = sum(durations) / len(durations) if durations else 0.0
    p95_duration = 0.0
    if durations:
        s = sorted(durations)
        # nearest-rank p95, no numpy
        idx = max(0, int(round(0.95 * len(s))) - 1)
        p95_duration = s[idx]
    avg_rows = sum(rows_seen) // len(rows_seen) if rows_seen else 0

    return {
        "runs_analyzed": len(runs),
        "avg_duration_ms": int(avg_duration),
        "p95_duration_ms": int(p95_duration),
        "avg_rows_processed": int(avg_rows),
        "last_run_status": last_status or "",
        "last_run_at": last_at or "",
    }


def _enrich_connection_refs(refs: dict, workspace_id: str) -> dict:
    """Replace bare connection IDs with {id, name, type} dicts so the
    Plan modal can show 'Salesforce - Prod' instead of '8c4f...'.
    Bounded: refs lists are already capped by compute_plan_diff."""
    try:
        from fpulse.main import app_state
        conn_store = app_state.get("connection_store")
    except Exception:
        conn_store = None
    if conn_store is None:
        return {
            "added":   [{"id": cid, "name": cid, "type": "?"} for cid in refs.get("added", [])],
            "removed": [{"id": cid, "name": cid, "type": "?"} for cid in refs.get("removed", [])],
        }

    cache: dict[str, dict] = {}
    def look(cid: str) -> dict:
        if cid in cache:
            return cache[cid]
        try:
            c = conn_store.get(cid, workspace_id=workspace_id)
        except Exception:
            c = None
        cache[cid] = {
            "id": cid,
            "name": getattr(c, "name", cid) if c else cid,
            "type": getattr(c, "type", "?") if c else "?",
        }
        return cache[cid]

    return {
        "added":   [look(cid) for cid in refs.get("added", [])],
        "removed": [look(cid) for cid in refs.get("removed", [])],
    }


# ---------------------------------------------------------------------------
# Lifecycle endpoints
# ---------------------------------------------------------------------------

@router.post("/{workflow_id}/test")
async def test_workflow(
    workflow_id: str,
    preview_limit: int = 50,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Run workflow in test mode — validate, execute with sample data,
    then record the test result. Does NOT change pipeline status.

    2026-05-22: previously this endpoint auto-flipped status to PUBLISHED
    on test success (and FAILED on test error). That mixed the test and
    publish semantics — users expect /test to prove readiness, not make
    a pipeline live. Publishing is now an explicit /publish call.

    The endpoint still:
      * stores ``test_results`` with the latest test outcome
      * writes ``tested`` / ``test_failed`` lifecycle events
      * sets a TESTING status during execution (so the UI can show
        "currently running test") and reverts it after.

    What it no longer does:
      * never sets status=PUBLISHED on success
      * never sets status=FAILED on error (FAILED was a runtime state
        and was getting confused with "deployment is broken")

    Scoped to the caller's workspace."""
    from fpulse.engine.executor import WorkflowExecutor
    from fpulse.monitoring.store import ExecutionRecord, StepLog

    store = get_store()
    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    wf = v.workflow
    if wf.status == PipelineStatus.ARCHIVED:
        raise HTTPException(400, "Cannot test an archived pipeline. Restore it first.")

    # 2026-05-22: remember the status the workflow was in before we
    # started the test so we can revert it once the test completes.
    # Earlier behaviour was to leave the pipeline in TESTING (or worse,
    # PUBLISHED) afterwards — that misrepresented the real lifecycle
    # state to schedulers and the Pipelines page.
    pre_test_status = wf.status
    store.update_status(workflow_id, PipelineStatus.TESTING)
    lifecycle_store = get_lifecycle_store()
    lifecycle_store.add_event(workflow_id, "testing", "Test run started", workspace_id=workspace_id)

    # Execute the workflow
    exe_store = get_execution_store()
    exe = ExecutionRecord(
        workflow_id=workflow_id,
        workflow_name=wf.name,
        project_id=getattr(wf, "project_id", "default"),
        workspace_id=workspace_id,
        steps_total=len(wf.steps),
        triggered_by="test",
        workflow_snapshot=wf.model_dump(mode="json"),
    )

    start = time.time()
    executor = WorkflowExecutor(data_dir=get_data_dir())

    try:
        from fpulse.security.execution_codes import mint_for_run
        result = executor.execute_workflow(wf, preview_limit=preview_limit, execution_code=mint_for_run(wf))

        step_logs = []
        for step in wf.steps:
            step_result = result.step_results.get(step.id)
            if step_result:
                step_logs.append(StepLog(
                    step_id=step.id,
                    step_name=step.label or step.id,
                    step_type=step.type.value if hasattr(step.type, "value") else str(step.type),
                    status=step_result.status,
                    rows_processed=step_result.row_count,
                    duration_ms=step_result.duration_ms,
                    error_message=step_result.error,
                ))

        duration = (time.time() - start) * 1000
        exe.status = result.status
        exe.completed_at = datetime.now(timezone.utc)
        exe.duration_ms = round(duration, 1)
        exe.steps_completed = len([s for s in step_logs if s.status == "success"])
        exe.steps_failed = len([s for s in step_logs if s.status == "error"])
        exe.step_logs = step_logs
        if result.status == "error":
            failed_steps = [s for s in step_logs if s.error_message]
            exe.error_message = failed_steps[0].error_message if failed_steps else "Unknown error"

        exe_store.record(exe)

        # Build test results summary
        test_summary = {
            "execution_id": exe.id,
            "status": result.status,
            "duration_ms": round(duration, 1),
            "steps_total": len(wf.steps),
            "steps_passed": exe.steps_completed,
            "steps_failed": exe.steps_failed,
            "tested_at": datetime.now(timezone.utc).isoformat(),
            "error": exe.error_message,
        }

        # 2026-05-22: record test outcome WITHOUT publishing. The status
        # reverts to whatever it was before the test (draft / published
        # / etc.) and the result is stored on test_results for /publish
        # and /deploy to consult later. Publishing is now an explicit
        # /publish call — the test no longer ships the pipeline live.
        store.update_status(
            workflow_id,
            pre_test_status,
            test_results=test_summary,
        )
        if result.status == "success":
            lifecycle_store.add_event(
                workflow_id, "tested",
                f"Test passed ({exe.steps_completed}/{len(wf.steps)} steps)",
                workspace_id=workspace_id,
            )
        else:
            lifecycle_store.add_event(
                workflow_id, "test_failed",
                f"Test failed: {exe.error_message}",
                workspace_id=workspace_id,
            )

        return {
            "test_results": test_summary,
            "status": result.status,
            "execution": result.model_dump(mode="json"),
        }
    except Exception as e:
        duration = (time.time() - start) * 1000
        exe.status = "error"
        exe.completed_at = datetime.now(timezone.utc)
        exe.duration_ms = round(duration, 1)
        exe.error_message = str(e)
        exe_store.record(exe)

        test_summary = {
            "execution_id": exe.id,
            "status": "error",
            "duration_ms": round(duration, 1),
            "steps_total": len(wf.steps),
            "steps_passed": 0,
            "steps_failed": 0,
            "tested_at": datetime.now(timezone.utc).isoformat(),
            "error": str(e),
        }
        # 2026-05-22: same pattern as the happy path — record the test
        # outcome on test_results but revert status to pre-test value.
        # Don't promote a temporary test crash to a global FAILED state.
        store.update_status(
            workflow_id,
            pre_test_status,
            test_results=test_summary,
        )
        lifecycle_store.add_event(
            workflow_id, "test_failed",
            f"Test error: {e}",
            workspace_id=workspace_id,
        )
        import logging
        logging.getLogger(__name__).exception("Test execution failed")
        raise HTTPException(500, "Test execution failed") from e


# ---------------------------------------------------------------------------
# Data Wrangler per-step preview (design-data-wrangler-node.md §6.1)
# ---------------------------------------------------------------------------

class DataWranglerPreviewRequest(BaseModel):
    """Body for POST /workflows/{wid}/nodes/{nid}/data-wrangler/preview.

    The frontend posts the user's currently-edited (unsaved) sub-step list
    so the server can render a per-step preview without forcing a save."""
    steps: list[dict[str, Any]] = Field(default_factory=list)
    sample_rows: int = 100


class EphemeralDataWranglerPreviewRequest(BaseModel):
    """Body for POST /workflows/ephemeral/data-wrangler/preview (Z18, 2026-05-23).

    Sibling of DataWranglerPreviewRequest for the case where the user is
    iterating on a Wrangler recipe BEFORE saving the workflow. The full
    workflow IR comes from the request body — no store lookup, no DB
    write. Mirrors the EphemeralStepRunBody pattern (Z10).

    The wrangler node's sub-steps come from the IR (the node's params
    carry them), so this body is just the workflow + which node to
    preview + sample size.
    """
    workflow: dict[str, Any] = Field(...)
    node_id: str
    sample_rows: int = 100
    # Optional: caller can override the wrangler's saved sub-steps with
    # the currently-edited list. Keeps parity with the persisted endpoint
    # which lets the frontend post unsaved edits.
    steps: list[dict[str, Any]] | None = None


def _run_wrangler_preview(
    *,
    workflow,  # Workflow IR (already parsed)
    node_id: str,
    steps_override: list[dict[str, Any]] | None,
    sample_rows: int,
) -> dict[str, Any]:
    """Shared per-step preview logic used by both the persisted and
    ephemeral endpoints.

    Returns the dict shape clients see — node_id + predecessor_id + the
    preview helper's payload (sample_data + schema_delta per step,
    compiled SQL, etc).
    """
    from fpulse.engine.executor import WorkflowExecutor
    from fpulse.ir.schema import StepType
    from fpulse.nodes.data_wrangler import DataWranglerNode

    wf = workflow

    # Locate the target wrangler node.
    target = next((s for s in wf.steps if s.id == node_id), None)
    if target is None:
        raise HTTPException(404, f"Node {node_id} not in workflow")
    if target.type != StepType.DATA_WRANGLER:
        raise HTTPException(400, f"Node {node_id} is not a Data Wrangler node")

    # Find the predecessor — the step whose connection feeds this node.
    upstream_ids = [c.from_step for c in wf.connections if c.to_step == node_id]
    if not upstream_ids:
        raise HTTPException(400, "Data Wrangler node has no upstream — connect a source first")
    predecessor_id = upstream_ids[0]

    # Slice the workflow: keep everything up to and including the predecessor,
    # drop the recipe node + everything downstream. We walk the connection
    # graph backwards from the predecessor to collect required ancestors.
    keep: set[str] = set()
    frontier = [predecessor_id]
    while frontier:
        sid = frontier.pop()
        if sid in keep:
            continue
        keep.add(sid)
        for c in wf.connections:
            if c.to_step == sid:
                frontier.append(c.from_step)

    sliced = wf.model_copy(deep=True)
    sliced.steps = [s for s in sliced.steps if s.id in keep]
    sliced.connections = [
        c for c in sliced.connections if c.from_step in keep and c.to_step in keep
    ]

    # Execute the sliced workflow to populate the predecessor's sample.
    executor = WorkflowExecutor(data_dir=get_data_dir())
    sample_rows_clamped = max(1, min(int(sample_rows or 100), 1000))
    try:
        from fpulse.security.execution_codes import mint_for_run
        result = executor.execute_workflow(sliced, preview_limit=sample_rows_clamped, execution_code=mint_for_run(sliced))
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("Upstream execution failed")
        raise HTTPException(500, "Upstream execution failed") from exc

    if result.status != "success":
        # Surface the first failing upstream error to the client.
        err = next(
            (sr.error for sr in result.step_results.values() if sr.error),
            "Upstream pipeline failed",
        )
        raise HTTPException(400, f"Upstream failure: {err}")

    pred_result = result.step_results.get(predecessor_id)
    if not pred_result or not pred_result.sample_data:
        raise HTTPException(400, "Predecessor produced no rows to preview")

    # Substep list — caller's edited override if present, otherwise the
    # saved params on the wrangler node.
    wrangler_substeps = (
        steps_override
        if steps_override is not None
        else (target.params.get("steps") if isinstance(target.params, dict) else None)
        or []
    )

    # Reconstruct the predecessor relation from its sample_data.
    import duckdb
    conn = duckdb.connect(":memory:")
    try:
        # DuckDB infers schema from the list-of-dicts. We use a registered
        # name distinct from the recipe's internal __recipe_input so the
        # preview helper's own register/unregister doesn't collide.
        try:
            import pandas as pd  # local import — pandas is already a dep
            df = pd.DataFrame(pred_result.sample_data)
            conn.register("__pred_input", df)
        except Exception:
            # Fallback if pandas missing — build a VALUES table.
            if not pred_result.sample_data:
                raise HTTPException(400, "Predecessor returned no rows")
            cols = list(pred_result.sample_data[0].keys())
            placeholders = ", ".join(["?"] * len(cols))
            conn.execute(
                f"CREATE TABLE __pred_input ({', '.join(f'{c} VARCHAR' for c in cols)})"
            )
            for row in pred_result.sample_data:
                conn.execute(
                    f"INSERT INTO __pred_input VALUES ({placeholders})",
                    [row.get(c) for c in cols],
                )

        pred_relation = conn.sql("SELECT * FROM __pred_input")
        preview = DataWranglerNode.preview_steps(
            conn=conn,
            source=pred_relation,
            steps=wrangler_substeps,
            sample_rows=sample_rows_clamped,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {
        "node_id": node_id,
        "predecessor_id": predecessor_id,
        **preview,
    }


# IMPORTANT — this route MUST be registered BEFORE the parameterised
# /{workflow_id}/nodes/{node_id}/data-wrangler/preview route below.
# FastAPI matches routes in registration order; without this ordering
# a request to /workflows/ephemeral/data-wrangler/preview would be
# eaten by the parameterised route with workflow_id="ephemeral" and
# 404 the store lookup. Same lesson as Z13 (execute_step_ephemeral).
@router.post("/ephemeral/data-wrangler/preview")
async def preview_data_wrangler_node_ephemeral(
    body: EphemeralDataWranglerPreviewRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Per-step Wrangler preview against an inline workflow IR — no save required.

    Lets the user see the output AFTER EACH SUB-STEP they apply, from
    the moment they add the first one, without ever clicking Save. The
    button stays as "Refresh preview" regardless of save state.
    """
    from fpulse.ir.schema import Workflow
    try:
        wf = Workflow(**body.workflow)
    except Exception as exc:
        raise HTTPException(400, {
            "code": "invalid_workflow_ir",
            "message": f"Could not parse workflow IR: {exc}",
        })
    # Stamp the caller's workspace — never trust body content.
    wf.workspace_id = workspace_id

    return _run_wrangler_preview(
        workflow=wf,
        node_id=body.node_id,
        steps_override=body.steps,
        sample_rows=body.sample_rows,
    )


@router.post("/{workflow_id}/nodes/{node_id}/data-wrangler/preview")
async def preview_data_wrangler_node(
    workflow_id: str,
    node_id: str,
    body: DataWranglerPreviewRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Per-step preview for a Data Wrangler node (saved workflow path).

    Runs the workflow upstream of `node_id`, captures the predecessor's
    sample, then applies the client-supplied sub-steps cumulatively. Returns
    row-count + schema delta after each enabled sub-step plus the compiled
    SQL the wrangler would run for real."""
    store = get_store()
    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    return _run_wrangler_preview(
        workflow=v.workflow,
        node_id=node_id,
        steps_override=body.steps,
        sample_rows=body.sample_rows,
    )


@router.post("/{workflow_id}/publish")
async def publish_workflow(
    workflow_id: str,
    published_by: str = "user",
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Mark workflow as published (scoped to caller's workspace).
    Only allowed if last test passed."""
    store = get_store()
    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    wf = v.workflow
    if wf.status == PipelineStatus.ARCHIVED:
        raise HTTPException(400, "Cannot publish an archived pipeline. Restore it first.")

    # Check that last test passed
    if wf.test_results is None:
        raise HTTPException(400, "Pipeline has not been tested yet. Run /test first.")
    if wf.test_results.get("status") != "success":
        raise HTTPException(
            400,
            f"Last test did not pass (status: {wf.test_results.get('status')}). "
            "Run /test again to get a passing result before publishing.",
        )

    store.update_status(workflow_id, PipelineStatus.PUBLISHED, published_by=published_by)
    lifecycle_store = get_lifecycle_store()
    lifecycle_store.add_event(workflow_id, "published", f"Published by {published_by}", workspace_id=workspace_id)

    # In-app bell notification — every workspace user sees that the
    # pipeline went live. Best-effort; never blocks the publish.
    try:
        from fpulse.notifications.run_events import emit_lifecycle_notification
        from fpulse.main import app_state as _ns
        emit_lifecycle_notification(
            notification_store=_ns.get("notification_store"),
            user_store=_ns.get("user_store"),
            workflow_id=workflow_id,
            workflow_name=wf.name,
            event="published",
            actor=published_by,
            workspace_id=workspace_id,
        )
    except Exception:
        pass

    return {
        "id": workflow_id,
        "status": "published",
        "published_at": wf.published_at.isoformat() if wf.published_at else datetime.now(timezone.utc).isoformat(),
        "published_by": published_by,
    }


@router.post("/{workflow_id}/revoke")
async def revoke_workflow(
    workflow_id: str,
    revoked_by: str = "user",
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Revoke a published pipeline back to draft.

    Inverse of /publish. Used when the user wants to pull a live
    pipeline back into editing without archiving it. Schedules and
    triggers stay attached but won't fire while the pipeline is in
    draft (the scheduler can still see it; the executor's draft
    gate is what actually halts firing — see the deploy contract).

    Refuses if the pipeline isn't currently published, so the user
    isn't surprised by a no-op revoke on an already-draft row.
    """
    store = get_store()
    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    wf = v.workflow
    if wf.status != PipelineStatus.PUBLISHED:
        raise HTTPException(
            400,
            f"Pipeline is not published (current status: {wf.status.value}). "
            "Only published pipelines can be revoked.",
        )

    store.update_status(workflow_id, PipelineStatus.DRAFT)
    lifecycle_store = get_lifecycle_store()
    lifecycle_store.add_event(
        workflow_id, "revoked", f"Revoked from published by {revoked_by}",
        workspace_id=workspace_id,
    )

    try:
        from fpulse.notifications.run_events import emit_lifecycle_notification
        from fpulse.main import app_state as _ns
        emit_lifecycle_notification(
            notification_store=_ns.get("notification_store"),
            user_store=_ns.get("user_store"),
            workflow_id=workflow_id,
            workflow_name=wf.name,
            event="revoked",
            actor=revoked_by,
            workspace_id=workspace_id,
        )
    except Exception:
        pass

    return {
        "id": workflow_id,
        "status": "draft",
        "revoked_by": revoked_by,
        "revoked_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/{workflow_id}/cancel")
async def cancel_workflow_run(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Cancel the workflow's currently-running execution(s).

    Resolution order:
      1. Look up live handles owned by this workflow in the
         ExecutionManager registry; cancel each via cancel_by_id.
      2. Mark every running execution row for this workflow as
         ``cancelled`` in the execution store so the UI gets a final
         state even if the run was synchronous (no registered handle).

    Idempotent — calling on a workflow with no live runs returns
    ``{"cancelled": 0}`` without erroring."""
    v = get_store().get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    cancelled_handles = 0
    cancel_errors: list[str] = []

    # Cancel registered live handles (best-effort — manager may not be up).
    try:
        from fpulse.main import app_state
        mgr = app_state.get("execution_manager") if isinstance(app_state, dict) else None
        if mgr is not None:
            records = mgr.inspect(owner=workflow_id)
            for record in records:
                try:
                    ok, _ = mgr.cancel_by_id(record.handle.id)
                    if ok:
                        cancelled_handles += 1
                except Exception as exc:
                    cancel_errors.append(f"{record.handle.id}: {exc}")
    except Exception as exc:
        cancel_errors.append(f"manager lookup: {exc}")

    # Soft-cancel any running rows in the execution store so the
    # frontend stops showing a stuck "Running" state.
    cancelled_rows = 0
    try:
        exe_store = get_execution_store()
        running = exe_store.list_by_workflow(workflow_id, workspace_id=workspace_id)
        for r in running:
            status = r.get("status") if isinstance(r, dict) else getattr(r, "status", None)
            if status not in ("running", "queued"):
                continue
            rid = r.get("id") if isinstance(r, dict) else getattr(r, "id", "")
            if not rid:
                continue
            exe_store.update(
                rid,
                {"status": "cancelled", "completed_at": datetime.now(timezone.utc)},
                workspace_id=workspace_id,
            )
            cancelled_rows += 1
    except Exception as exc:
        cancel_errors.append(f"store update: {exc}")

    return {
        "workflow_id": workflow_id,
        "cancelled_handles": cancelled_handles,
        "cancelled_rows": cancelled_rows,
        "errors": cancel_errors,
    }


@router.post("/{workflow_id}/submit-for-review")
async def submit_for_review(
    workflow_id: str,
    submitted_by: str = "user",
    snapshot_hash: str = "",
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Submit a pipeline for deployment review.

    DEV → PROD flow:
      1. Developer creates & tests pipeline in DEV
      2. Developer submits for review (this endpoint)
      3. Lead/Admin reviews and approves
      4. Admin deploys approved pipeline to PROD

    This prevents developers from deploying directly to PROD.

    ``snapshot_hash`` (optional): SHA-256 of the canonical IR computed by
    the AI pre-publish card on the developer's screen at click time.
    Stored in workflow metadata + lifecycle event so a reviewer can later
    prove "the IR I'm approving exactly matches the one that was reviewed."
    """
    store = get_store()
    lifecycle_store = get_lifecycle_store()

    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    wf = v.workflow

    # Must be tested before submitting
    if wf.test_results is None or wf.test_results.get("status") != "success":
        raise HTTPException(400, "Pipeline must pass testing before submission")

    # Update submission fields
    wf.submitted_for_review = True
    wf.submitted_by = submitted_by
    wf.submitted_at = datetime.now(timezone.utc)
    wf.approval_status = "pending"
    # PR11 — initialize the two-gate state machine. Submit-for-review
    # always lands at Gate 1 (sandbox approval); a Plus admin must
    # approve Gate 1 before sandbox runs become available.
    wf.approval_stage = "pending_sandbox_approval"

    # Persist the AI pre-publish snapshot hash on the workflow's metadata
    # so the trust contract closes: the reviewer can later compare the
    # hash they're about to approve against this captured value.
    if snapshot_hash:
        md = dict(getattr(wf, "metadata", {}) or {})
        md["pre_publish_snapshot_hash"] = snapshot_hash
        md["pre_publish_snapshot_captured_at"] = wf.submitted_at.isoformat()
        wf.metadata = md

    store.save(wf, change_summary=f"Submitted for review by {submitted_by}")
    lifecycle_store.add_event(
        workflow_id, "submitted",
        f"Submitted for PROD review by {submitted_by}",
        metadata={
            "submitted_by": submitted_by,
            "version": v.version + 1,
            "snapshot_hash": snapshot_hash or None,
        },
        workspace_id=workspace_id,
    )

    # Build rich card data for notifications
    import json as _json
    card_data = {
        "pipeline_id": workflow_id,
        "pipeline_name": wf.name,
        "description": wf.description or "",
        "project_id": wf.project_id,
        "version": v.version + 1,
        "step_count": len(wf.steps),
        "connection_count": len(wf.connections),
        "node_types": list(set(s.type for s in wf.steps)),
        "owner": wf.owner_name or wf.owner_id or submitted_by,
        "submitted_by": submitted_by,
        "submitted_at": wf.submitted_at.isoformat(),
        "test_status": (wf.test_results or {}).get("status", "unknown"),
        "has_schedule": False,
        "has_alerts": False,
        "environment": "DEV → PROD",
    }

    # Enrich with schedule/alert info
    try:
        from fpulse.main import app_state as _state
        sched_store = _state.get("schedule_store")
        if sched_store:
            schedules = sched_store.list_for_workflow(workflow_id)
            card_data["has_schedule"] = len(schedules) > 0
            if schedules:
                card_data["schedule_cron"] = schedules[0].get("cron_expression", "")
        alert_store = _state.get("alert_store")
        if alert_store:
            alerts = alert_store.list_for_workflow(workflow_id)
            card_data["has_alerts"] = len(alerts) > 0
            card_data["alert_count"] = len(alerts)
    except Exception:
        pass

    # Create in-app notification cards for approvers
    try:
        db = _get_db()
        # Find approvers from approval gate
        gate_cursor = db.execute_with_retry(
            """SELECT approvers FROM approval_gates
               WHERE ((scope = 'pipeline' AND scope_id = ?) OR
                      (scope = 'project' AND scope_id = ?) OR
                      (scope = 'global' AND scope_id = ''))
               AND enabled = 1 AND workspace_id = ?
               ORDER BY CASE scope WHEN 'pipeline' THEN 1 WHEN 'project' THEN 2 ELSE 3 END
               LIMIT 1""",
            [workflow_id, wf.project_id, workspace_id],
        )
        gate_rows = gate_cursor.fetchall() if gate_cursor else []
        approvers = []
        if gate_rows:
            approvers = _json.loads(gate_rows[0][0] or "[]")

        # Fallback: notify all admins/leads if no specific approvers configured
        if not approvers:
            admin_cursor = db.execute_with_retry(
                "SELECT user_id FROM workspace_members WHERE workspace_id = ? AND role IN ('admin', 'lead', 'super_admin')",
                [workspace_id],
            )
            admin_rows = admin_cursor.fetchall() if admin_cursor else []
            approvers = [r[0] for r in admin_rows] if admin_rows else ["admin"]

        _create_approval_notification(
            db=db,
            workflow_id=workflow_id,
            workflow_name=wf.name,
            recipients=approvers,
            sender_id=submitted_by,
            action="submitted",
            message=f'Pipeline "{wf.name}" submitted for PROD deployment review by {submitted_by}',
            card_data=card_data,
            workspace_id=workspace_id,
        )
    except Exception:
        pass  # Never block the workflow action

    # Legacy external notifier
    try:
        from fpulse.main import app_state as _state
        notifier = _state.get("approval_notifier")
        if notifier:
            notifier.on_submit_for_review(
                workflow_id=workflow_id,
                workflow_name=wf.name,
                submitted_by_email=submitted_by,
                submitted_by_name=submitted_by,
            )
    except Exception:
        pass

    return {
        "id": workflow_id,
        "approval_status": "pending",
        "submitted_by": submitted_by,
        "submitted_at": wf.submitted_at.isoformat(),
        "approvers_notified": len(approvers) if 'approvers' in dir() else 0,
    }


@router.post("/{workflow_id}/approve")
async def approve_pipeline(
    workflow_id: str,
    approved_by: str = "admin",
    notes: str = "",
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Approve a pipeline for PROD deployment (scoped to caller's
    workspace). Only leads and admins can approve. Once approved, an
    admin can deploy.
    """
    store = get_store()
    lifecycle_store = get_lifecycle_store()

    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    wf = v.workflow
    if wf.approval_status != "pending":
        raise HTTPException(400, f"Pipeline is not pending review (status: {wf.approval_status or 'none'})")

    wf.approval_status = "approved"
    wf.approved_by = approved_by
    wf.approved_at = datetime.now(timezone.utc)
    wf.approval_notes = notes
    # PR11 — Gate 1 transition. The existing /approve endpoint is the
    # sandbox-approval gate. Stage moves to "sandbox_ready" so the Prod
    # admin can now run the pipeline in sandbox before requesting deploy.
    wf.approval_stage = "sandbox_ready"
    wf.sandbox_approved_at = wf.approved_at
    wf.sandbox_approved_by = approved_by
    wf.sandbox_approval_notes = notes

    store.save(wf, change_summary=f"Approved by {approved_by}")
    lifecycle_store.add_event(
        workflow_id, "approved",
        f"Approved for PROD deployment by {approved_by}" + (f": {notes}" if notes else ""),
        metadata={"approved_by": approved_by, "notes": notes},
        workspace_id=workspace_id,
    )

    # Create in-app notification for the submitter
    try:
        db = _get_db()
        if wf.submitted_by:
            _create_approval_notification(
                db=db,
                workflow_id=workflow_id,
                workflow_name=wf.name,
                recipients=[wf.submitted_by],
                sender_id=approved_by,
                action="approved",
                message=f'Pipeline "{wf.name}" has been approved for PROD deployment by {approved_by}' + (f" — {notes}" if notes else ""),
                card_data={
                    "pipeline_id": workflow_id,
                    "pipeline_name": wf.name,
                    "project_id": wf.project_id,
                    "version": v.version + 1,
                    "approved_by": approved_by,
                    "approved_at": wf.approved_at.isoformat(),
                    "notes": notes,
                    "status": "approved",
                },
                workspace_id=workspace_id,
            )
    except Exception:
        pass

    # Legacy external notifier
    try:
        from fpulse.main import app_state as _state
        notifier = _state.get("approval_notifier")
        if notifier and wf.submitted_by:
            us = _state.get("user_store")
            submitter = us.get_user_by_email(wf.submitted_by) if us else None
            submitter_id = submitter.id if submitter else wf.submitted_by
            notifier.on_approved(
                workflow_id=workflow_id,
                workflow_name=wf.name,
                approved_by_email=approved_by,
                approved_by_name=approved_by,
                submitted_by_user_id=submitter_id,
                notes=notes,
            )
    except Exception:
        pass

    # PR10 step 6 — expedite cleanup of any sandbox runs attached to
    # this workflow's approval. The cleanup daemon picks them up on its
    # next 15-minute tick rather than waiting the full 24h TTL.
    try:
        _expedite_sandbox_cleanup_for_workflow(workflow_id)
    except Exception as e:
        logger.warning("Sandbox cleanup expedite failed for %s: %s", workflow_id, e)

    return {
        "id": workflow_id,
        "approval_status": "approved",
        "approved_by": approved_by,
        "approved_at": wf.approved_at.isoformat(),
    }


@router.post("/{workflow_id}/reject")
async def reject_pipeline(
    workflow_id: str,
    rejected_by: str = "admin",
    notes: str = "",
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Reject a pipeline submission with feedback (scoped to caller's
    workspace)."""
    store = get_store()
    lifecycle_store = get_lifecycle_store()

    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    wf = v.workflow
    if wf.approval_status != "pending":
        raise HTTPException(400, f"Pipeline is not pending review")

    wf.approval_status = "rejected"
    wf.approved_by = rejected_by
    wf.approval_notes = notes

    store.save(wf, change_summary=f"Rejected by {rejected_by}: {notes}")
    lifecycle_store.add_event(
        workflow_id, "rejected",
        f"Rejected by {rejected_by}: {notes}",
        metadata={"rejected_by": rejected_by, "notes": notes},
        workspace_id=workspace_id,
    )

    # Create in-app notification for the submitter
    try:
        db = _get_db()
        if wf.submitted_by:
            _create_approval_notification(
                db=db,
                workflow_id=workflow_id,
                workflow_name=wf.name,
                recipients=[wf.submitted_by],
                sender_id=rejected_by,
                action="rejected",
                message=f'Pipeline "{wf.name}" was rejected by {rejected_by}: {notes}',
                card_data={
                    "pipeline_id": workflow_id,
                    "pipeline_name": wf.name,
                    "project_id": wf.project_id,
                    "version": v.version + 1,
                    "rejected_by": rejected_by,
                    "notes": notes,
                    "status": "rejected",
                },
                workspace_id=workspace_id,
            )
    except Exception:
        pass

    # Legacy external notifier
    try:
        from fpulse.main import app_state as _state
        notifier = _state.get("approval_notifier")
        if notifier and wf.submitted_by:
            us = _state.get("user_store")
            submitter = us.get_user_by_email(wf.submitted_by) if us else None
            submitter_id = submitter.id if submitter else wf.submitted_by
            notifier.on_rejected(
                workflow_id=workflow_id,
                workflow_name=wf.name,
                rejected_by_email=rejected_by,
                rejected_by_name=rejected_by,
                submitted_by_user_id=submitter_id,
                notes=notes,
            )
    except Exception:
        pass

    # PR10 step 6 — expedite cleanup of any sandbox runs attached to this
    # rejected approval. No need to keep scratch data around for a deploy
    # that's been rejected.
    try:
        _expedite_sandbox_cleanup_for_workflow(workflow_id)
    except Exception as e:
        logger.warning("Sandbox cleanup expedite failed for %s: %s", workflow_id, e)

    return {
        "id": workflow_id,
        "approval_status": "rejected",
        "rejected_by": rejected_by,
        "notes": notes,
    }


# ─────────────────────────────────────────────────────────────────────────
# PR11 — Two-Gate Approval: Gate 2 endpoints
# ─────────────────────────────────────────────────────────────────────────

@router.post("/{workflow_id}/submit-for-deploy")
async def submit_for_deploy(
    workflow_id: str,
    submitted_by: str = "user",
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Prod admin clicks "Submit for Deploy" after running the pipeline
    in sandbox. Transitions the workflow from ``sandbox_ready`` to
    ``pending_deploy_approval`` (Gate 2 pending).

    Hard requirement (invariant from project_two_gate_approval_decisions):
    at least ONE successful sandbox run must exist for this workflow
    before this endpoint succeeds. Approver-evidence rule.
    """
    store = get_store()
    lifecycle_store = get_lifecycle_store()

    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")
    wf = v.workflow

    if wf.approval_stage != "sandbox_ready":
        raise HTTPException(
            400,
            f"Pipeline must be in 'sandbox_ready' stage to submit for deploy "
            f"(current: {wf.approval_stage or 'none'})",
        )

    # Enforce: at least one successful sandbox run must exist.
    sandbox_run_id = _latest_successful_sandbox_run(workflow_id)
    if not sandbox_run_id:
        raise HTTPException(
            400,
            "No successful sandbox run found for this workflow. Run in "
            "sandbox at least once and verify the output before submitting "
            "for deploy.",
        )

    wf.approval_stage = "pending_deploy_approval"
    wf.deploy_evidence_sandbox_run_id = sandbox_run_id
    store.save(wf, change_summary=f"Submitted for deploy by {submitted_by}")
    lifecycle_store.add_event(
        workflow_id, "submitted_for_deploy",
        f"Submitted for deploy by {submitted_by} (sandbox evidence: {sandbox_run_id})",
        metadata={
            "submitted_by": submitted_by,
            "sandbox_run_id": sandbox_run_id,
        },
        workspace_id=workspace_id,
    )

    # Notify approvers — Gate 2 with sandbox evidence in the email body.
    try:
        from fpulse.main import app_state as _state
        notifier = _state.get("approval_notifier")
        if notifier:
            notifier.on_submitted_for_deploy(
                workflow_id=workflow_id,
                workflow_name=wf.name,
                submitted_by=submitted_by,
                sandbox_run_id=sandbox_run_id,
                gate1_approver=wf.sandbox_approved_by or "(unknown)",
            )
    except Exception:
        pass

    return {
        "id": workflow_id,
        "approval_stage": wf.approval_stage,
        "sandbox_evidence_run_id": sandbox_run_id,
    }


@router.post("/{workflow_id}/approve-deploy")
async def approve_deploy(
    workflow_id: str,
    approved_by: str = "approver",
    notes: str = "",
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Gate 2 — final approval to deploy and activate.

    Transitions ``pending_deploy_approval`` → ``active``. Requires:
    1. Stage is ``pending_deploy_approval``
    2. A sandbox-evidence run id is recorded (proves Gate 1 + sandbox happened)
    3. If workspace ``enforce_two_person_approval`` is on, the approver
       must differ from ``sandbox_approved_by``.
    """
    store = get_store()
    lifecycle_store = get_lifecycle_store()

    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")
    wf = v.workflow

    if wf.approval_stage != "pending_deploy_approval":
        raise HTTPException(
            400,
            f"Pipeline is not pending deploy approval "
            f"(current stage: {wf.approval_stage or 'none'})",
        )
    if not wf.deploy_evidence_sandbox_run_id:
        raise HTTPException(
            400,
            "Missing sandbox evidence — re-submit for deploy after a "
            "successful sandbox run.",
        )

    # Two-person rule (workspace setting; default off).
    if _two_person_required(workspace_id) and wf.sandbox_approved_by == approved_by:
        raise HTTPException(
            403,
            "Two-person approval is enforced in this workspace — Gate 2 "
            "must be approved by a different admin than Gate 1 "
            f"(Gate 1 approved by {wf.sandbox_approved_by}).",
        )

    wf.approval_stage = "active"
    wf.deploy_approved_at = datetime.now(timezone.utc)
    wf.deploy_approved_by = approved_by
    wf.deploy_approval_notes = notes
    store.save(wf, change_summary=f"Deploy approved by {approved_by}")
    lifecycle_store.add_event(
        workflow_id, "deploy_approved",
        f"Deploy approved by {approved_by}" + (f": {notes}" if notes else ""),
        metadata={
            "approved_by": approved_by,
            "notes": notes,
            "sandbox_evidence_run_id": wf.deploy_evidence_sandbox_run_id,
            "gate1_approver": wf.sandbox_approved_by,
        },
        workspace_id=workspace_id,
    )

    # Expedite sandbox cleanup — evidence has been used.
    try:
        _expedite_sandbox_cleanup_for_workflow(workflow_id)
    except Exception as e:
        logger.warning("Sandbox cleanup expedite failed for %s: %s", workflow_id, e)

    return {
        "id": workflow_id,
        "approval_stage": "active",
        "deploy_approved_by": approved_by,
        "deploy_approved_at": wf.deploy_approved_at.isoformat(),
    }


def _latest_successful_sandbox_run(workflow_id: str) -> str | None:
    """Return the id of the most recent successful sandbox run for this
    workflow, or None if there isn't one. Used to enforce the "sandbox
    is mandatory before Gate 2" invariant.
    """
    try:
        from fpulse.main import app_state
        db = app_state.get("db")
        if db is None:
            return None
        with db.connect() as conn:
            cur = conn.execute(
                "SELECT id FROM sandbox_runs "
                "WHERE workflow_id = ? AND status = 'success' "
                "ORDER BY finished_at DESC LIMIT 1",
                (workflow_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _two_person_required(workspace_id: str) -> bool:
    """Read the workspace_settings JSON for the enforce_two_person_approval
    flag. Default is False (single approver allowed)."""
    try:
        import json as _json
        from fpulse.main import app_state
        db = app_state.get("db")
        if db is None:
            return False
        with db.connect() as conn:
            cur = conn.execute(
                "SELECT settings FROM workspace_settings WHERE workspace_id = ?",
                (workspace_id,),
            )
            row = cur.fetchone()
            if not row:
                return False
            settings = _json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
            return bool(settings.get("enforce_two_person_approval"))
    except Exception:
        return False


def _expedite_sandbox_cleanup_for_workflow(workflow_id: str) -> int:
    """When an approval is decided (approve OR reject), bump cleanup_at
    to NOW for any sandbox runs attached to it. The cleanup daemon's
    next tick (≤15 min) drops the scratch resources.

    Approval-id resolution: today there's a 1:1 between workflow and
    its current pending approval (no historical multi-approval queue),
    so we treat workflow_id as the approval_id key for the lookup.
    Returns the number of sandbox runs whose cleanup was expedited.
    The base build has no sandbox runtime, so this helper is a no-op
    that returns 0.
    """
    return 0


@router.get("/{workflow_id}/pre-deploy-check")
async def pre_deploy_check(
    workflow_id: str,
    version: int | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Pre-deploy checklist — validates a pipeline is safe to deploy to PROD.

    This is the admin's gate before clicking Deploy. Returns a list of
    checks with pass/fail/warning status:

      1. Structural validation (no broken edges, required params filled)
      2. Approval status (must be approved)
      3. Has been tested (at least 1 successful execution in history)
      4. No unconfigured connections (every source/sink has a connection)
      5. Version exists and is not already deployed (prevent no-op)

    The response always includes ``can_deploy: bool`` — the frontend
    uses this to enable/disable the Deploy button.
    """
    store = get_store()

    # Resolve version
    if version is not None:
        v = store.get(workflow_id, version, workspace_id=workspace_id)
    else:
        v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    wf = v.workflow
    checks = []
    can_deploy = True

    # 1) Structural validation
    errors = validate_workflow(wf)
    struct_errors = [e for e in errors if e.severity == "error"]
    if struct_errors:
        checks.append({
            "name": "structural_validation",
            "label": "Pipeline structure valid",
            "status": "fail",
            "detail": f"{len(struct_errors)} error(s): {'; '.join(e.message for e in struct_errors[:3])}",
        })
        can_deploy = False
    else:
        checks.append({
            "name": "structural_validation",
            "label": "Pipeline structure valid",
            "status": "pass",
            "detail": f"{len(wf.steps)} steps, {len(wf.connections)} connections",
        })

    # 2) Approval status
    approval = getattr(wf, "approval_status", None)
    if approval == "approved":
        checks.append({
            "name": "approval",
            "label": "Approved for deployment",
            "status": "pass",
            "detail": f"Approved by {getattr(wf, 'approved_by', 'unknown')}",
        })
    elif approval == "rejected":
        checks.append({
            "name": "approval",
            "label": "Approved for deployment",
            "status": "fail",
            "detail": "Pipeline was rejected — address feedback and re-submit",
        })
        can_deploy = False
    else:
        checks.append({
            "name": "approval",
            "label": "Approved for deployment",
            "status": "fail",
            "detail": "Pipeline has not been submitted for review / approved yet",
        })
        can_deploy = False

    # 3) Execution history — at least 1 successful run
    try:
        from fpulse.main import app_state
        exe_store = app_state["execution_store"]
        history = exe_store.list_by_workflow(workflow_id, workspace_id=workspace_id)
        success_count = sum(
            1 for e in history
            if (e.get("status") if isinstance(e, dict) else getattr(e, "status", None)) == "success"
        )
        if success_count > 0:
            checks.append({
                "name": "tested",
                "label": "Successfully tested",
                "status": "pass",
                "detail": f"{success_count} successful run(s) in history",
            })
        else:
            checks.append({
                "name": "tested",
                "label": "Successfully tested",
                "status": "warning",
                "detail": "No successful execution recorded — run the pipeline at least once in DEV",
            })
    except Exception:
        checks.append({
            "name": "tested",
            "label": "Successfully tested",
            "status": "warning",
            "detail": "Could not check execution history",
        })

    # 4) Connections configured — every source/sink needs a connection,
    #    AND we surface which connections are used so the admin can verify
    #    they're appropriate for PROD (not just DEV-only DSNs).
    unconfigured = []
    used_connections: list[dict] = []  # {node, connection_id, connection_name, type}
    conn_store = None
    try:
        from fpulse.main import app_state
        conn_store = app_state.get("connection_store")
    except Exception:
        pass

    for step in wf.steps:
        stype = step.type.value if hasattr(step.type, "value") else str(step.type)
        if "source" in stype.lower() or "sink" in stype.lower():
            conn_id = step.params.get("connection_id")
            file_path = step.params.get("file_path")
            if not conn_id and not file_path:
                unconfigured.append(step.label or step.id)
            elif conn_id:
                # Resolve connection name for display
                conn_name = conn_id
                if conn_store:
                    try:
                        conn = conn_store.get(conn_id, workspace_id=workspace_id)
                        if conn:
                            conn_name = conn.name
                    except Exception:
                        pass
                used_connections.append({
                    "node": step.label or step.id,
                    "connection_id": conn_id,
                    "connection_name": conn_name,
                    "type": stype,
                })

    if unconfigured:
        checks.append({
            "name": "connections",
            "label": "All sources/sinks configured",
            "status": "fail",
            "detail": f"Unconfigured nodes: {', '.join(unconfigured[:5])}",
        })
        can_deploy = False
    elif used_connections:
        conn_summary = ", ".join(
            f"{c['node']} → {c['connection_name']}" for c in used_connections[:4]
        )
        checks.append({
            "name": "connections",
            "label": "Connections need PROD mapping",
            "status": "warning",
            "detail": f"DEV connections used: {conn_summary}. Verify these are valid for PROD or remap in the deploy wizard.",
            "connections": used_connections,
        })
    else:
        checks.append({
            "name": "connections",
            "label": "All sources/sinks configured",
            "status": "pass",
            "detail": "All source/sink nodes use file paths (no connections to remap)",
        })

    # 5) Not already deployed at this version
    current_deployed = getattr(wf, "deployed_version", None)
    target_version = version or v.version
    if current_deployed == target_version:
        checks.append({
            "name": "version_change",
            "label": "New version to deploy",
            "status": "warning",
            "detail": f"Version {target_version} is already the deployed version",
        })
    else:
        checks.append({
            "name": "version_change",
            "label": "New version to deploy",
            "status": "pass",
            "detail": f"Will deploy v{target_version}" + (f" (currently v{current_deployed})" if current_deployed else " (first deployment)"),
        })

    return {
        "workflow_id": workflow_id,
        "version": target_version,
        "can_deploy": can_deploy,
        "checks": checks,
        "total_checks": len(checks),
        "passed": sum(1 for c in checks if c["status"] == "pass"),
        "failed": sum(1 for c in checks if c["status"] == "fail"),
        "warnings": sum(1 for c in checks if c["status"] == "warning"),
    }


@router.post("/{workflow_id}/deploy")
async def deploy_version(
    workflow_id: str,
    version: int | None = None,
    deployed_by: str = "admin",
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Deploy a specific version of a pipeline to PROD (scoped to
    caller's workspace).

    If version is None, deploys the latest PUBLISHED version.
    This pins the deployed_version so scheduled runs and triggers
    execute this exact version — not whatever is latest in DEV.
    """
    store = get_store()
    lifecycle_store = get_lifecycle_store()

    # Get the target version
    if version is not None:
        v = store.get(workflow_id, version, workspace_id=workspace_id)
        if not v:
            raise HTTPException(404, f"Version {version} not found")
    else:
        v = store.get(workflow_id, workspace_id=workspace_id)
        if not v:
            raise HTTPException(404, "Workflow not found")
        version = v.version

    wf = v.workflow

    # ── Enforce pre-deploy validation (server-side gate) ──
    errors = validate_workflow(wf)
    struct_errors = [e for e in errors if e.severity == "error"]
    if struct_errors:
        raise HTTPException(
            400,
            f"Deploy blocked: {len(struct_errors)} structural error(s) — "
            + "; ".join(e.message for e in struct_errors[:3]),
        )

    # ── Approval gate (positive check, 2026-05-22) ───────────────────────
    # Previously this only blocked the explicit "rejected" state, which
    # let a caller deploy a pipeline that had never been submitted for
    # review (approval_status == "" / None) or was still pending. The
    # audit (PROJECT_PIPELINE_CONFIGURATION_VALIDATION.md finding A3)
    # called this out as a privilege-escalation-shaped hole: the
    # /submit-for-review → /approve → /deploy chain was meant to be
    # the only path to PROD, but /deploy itself didn't enforce that
    # the chain had actually been walked.
    #
    # Positive gate: require approval_status == "approved" before we
    # touch the deployed_version pointer. The "final" state used by the
    # two-gate Plus flow is "final_approved" — both are accepted here so
    # the OSS single-gate and Plus two-gate flows both work.
    approval = getattr(wf, "approval_status", "") or ""
    if approval == "rejected":
        raise HTTPException(400, "Deploy blocked: pipeline was rejected — address feedback and re-submit")
    if approval not in ("approved", "final_approved"):
        if approval in ("pending", "pending_final"):
            raise HTTPException(
                400,
                "Deploy blocked: approval is still pending. Wait for "
                "the reviewer to /approve before deploying.",
            )
        raise HTTPException(
            400,
            "Deploy blocked: pipeline has not been approved for "
            "deployment. Call /submit-for-review and have a reviewer "
            "approve it before deploying.",
        )

    # Check connections configured (source/sink nodes)
    unconfigured = []
    for step in wf.steps:
        stype = step.type.value if hasattr(step.type, "value") else str(step.type)
        if "source" in stype.lower() or "sink" in stype.lower():
            has_conn = bool(step.params.get("connection_id") or step.params.get("file_path"))
            if not has_conn:
                unconfigured.append(step.label or step.id)
    if unconfigured:
        raise HTTPException(
            400,
            f"Deploy blocked: unconfigured source/sink nodes — {', '.join(unconfigured[:5])}",
        )

    # Check not already deployed at this version
    current_deployed = getattr(wf, "deployed_version", None)
    if current_deployed == version:
        raise HTTPException(400, f"Deploy blocked: version {version} is already the deployed version")

    # ── Signed-artifact capture (v15+) ──
    # Fetch the version row's stored content_hash and write it into the
    # deploy lifecycle event. Gives admins an immutable "this exact hash
    # went live at this time" record they can verify against the stored
    # hash on rollback or audit. Legacy rows (pre-v15) return "" and we
    # just record empty — no failure path.
    _match, deployed_hash, _recomp = store.verify_version_hash(
        workflow_id, version, workspace_id=workspace_id,
    )

    # Record deployment
    store.update_status(
        workflow_id, PipelineStatus.PUBLISHED,
        published_by=deployed_by,
        deployed_version=version,
    )
    lifecycle_store.add_event(
        workflow_id, "deployed",
        f"Version {version} deployed to PROD by {deployed_by}",
        metadata={
            "deployed_version": version,
            "deployed_by": deployed_by,
            "content_hash": deployed_hash,
        },
        workspace_id=workspace_id,
    )

    # ── Notify: pipeline deployed ──
    try:
        from fpulse.main import app_state as _app_state
        notifier = _app_state.get("approval_notifier")
        if notifier:
            submitter_id = getattr(wf, "submitted_by_user_id", "") or ""
            notifier.on_deployed(
                workflow_id=workflow_id,
                workflow_name=wf.name,
                deployed_by_email=deployed_by,
                deployed_by_name=deployed_by,
                submitted_by_user_id=submitter_id,
            )
    except Exception:
        pass

    return {
        "id": workflow_id,
        "name": wf.name,
        "deployed_version": version,
        "deployed_by": deployed_by,
        "deployed_at": datetime.now(timezone.utc).isoformat(),
        "total_versions": len(store.get_versions(workflow_id)),
        "content_hash": deployed_hash,
    }


@router.post("/{workflow_id}/rollback")
async def rollback_version(
    workflow_id: str,
    to_version: int,
    rolled_back_by: str = "admin",
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Rollback a deployed pipeline to a previous version.

    This is a PROD operation — it changes which version of the pipeline
    runs when triggered by schedule or manual execution. The current
    version is preserved in history (no data lost).
    """
    store = get_store()
    lifecycle_store = get_lifecycle_store()

    # Get current deployment info
    current = store.get(workflow_id, workspace_id=workspace_id)
    if not current:
        raise HTTPException(404, "Workflow not found")

    current_deployed = current.workflow.deployed_version or current.version

    # Get the target rollback version
    target = store.get(workflow_id, to_version, workspace_id=workspace_id)
    if not target:
        raise HTTPException(404, f"Version {to_version} not found for rollback")

    if to_version == current_deployed:
        raise HTTPException(400, f"Already running version {to_version}")

    # ── Signed-artifact verification (v15+) ──
    # Re-hash the stored workflow for this version and compare to the
    # content_hash column. Mismatch means the row was altered between
    # save and now — refuse the rollback rather than deploying tampered
    # or corrupted content. Legacy (pre-v15) rows with empty hash are
    # forgiven with a warning log.
    match, stored_hash, recomputed_hash = store.verify_version_hash(
        workflow_id, to_version, workspace_id=workspace_id,
    )
    if stored_hash == "":
        logger.warning(
            "rollback: workflow %s v%d has no stored content hash (pre-v15); proceeding without verification",
            workflow_id, to_version,
        )
    elif not match:
        logger.error(
            "rollback: HASH MISMATCH on workflow %s v%d — stored=%s recomputed=%s. Refusing rollback.",
            workflow_id, to_version, stored_hash, recomputed_hash,
        )
        raise HTTPException(
            409,
            f"Cannot rollback to v{to_version}: content hash mismatch. "
            f"This version's stored data differs from when it was saved — "
            f"the row may have been tampered with or corrupted. "
            f"Contact an admin before retrying.",
        )

    # Perform rollback — pin to the target version
    store.update_status(
        workflow_id, PipelineStatus.PUBLISHED,
        published_by=rolled_back_by,
        deployed_version=to_version,
        rollback_from=current_deployed,
    )
    lifecycle_store.add_event(
        workflow_id, "rollback",
        f"Rolled back from v{current_deployed} to v{to_version} by {rolled_back_by}",
        metadata={
            "from_version": current_deployed,
            "to_version": to_version,
            "rolled_back_by": rolled_back_by,
            # Record the verified hash on the audit event — admins can
            # cross-reference "was THIS exact content deployed" later.
            "content_hash": stored_hash,
        },
        workspace_id=workspace_id,
    )

    return {
        "id": workflow_id,
        "name": current.workflow.name,
        "rolled_back_from": current_deployed,
        "rolled_back_to": to_version,
        "rolled_back_by": rolled_back_by,
        "rolled_back_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": stored_hash,
    }


@router.post("/{workflow_id}/archive")
async def archive_workflow(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Archive the pipeline (scoped to caller's workspace)."""
    store = get_store()
    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    if v.workflow.status == PipelineStatus.ARCHIVED:
        raise HTTPException(400, "Pipeline is already archived.")

    previous_status = v.workflow.status.value
    store.update_status(workflow_id, PipelineStatus.ARCHIVED)
    lifecycle_store = get_lifecycle_store()
    lifecycle_store.add_event(
        workflow_id, "archived",
        f"Archived from {previous_status} status",
        metadata={"previous_status": previous_status},
        workspace_id=workspace_id,
    )

    return {"id": workflow_id, "status": "archived", "previous_status": previous_status}


@router.post("/{workflow_id}/restore")
async def restore_workflow(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Restore an archived pipeline back to draft (scoped to caller's
    workspace)."""
    store = get_store()
    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    if v.workflow.status != PipelineStatus.ARCHIVED:
        raise HTTPException(400, f"Pipeline is not archived (current status: {v.workflow.status.value}).")

    store.update_status(workflow_id, PipelineStatus.DRAFT)
    lifecycle_store = get_lifecycle_store()
    lifecycle_store.add_event(
        workflow_id, "restored", "Restored from archive to draft",
        workspace_id=workspace_id,
    )

    return {"id": workflow_id, "status": "draft"}


@router.get("/{workflow_id}/lifecycle")
async def get_lifecycle(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Get lifecycle history/events for a workflow (scoped to caller's
    workspace)."""
    store = get_store()
    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    lifecycle_store = get_lifecycle_store()
    events = lifecycle_store.get_events(workflow_id, workspace_id=workspace_id)

    return {
        "workflow_id": workflow_id,
        "current_status": v.workflow.status.value,
        "published_at": v.workflow.published_at.isoformat() if v.workflow.published_at else None,
        "published_by": v.workflow.published_by,
        "test_results": v.workflow.test_results,
        "events": events,
    }


# ---------------------------------------------------------------------------
# Pipeline Export / Import — share pipelines as portable JSON
# ---------------------------------------------------------------------------

class PipelineImportRequest(BaseModel):
    pipeline: dict[str, Any]
    project_id: str = "default"
    rename: str = ""
    # Connection remapping: { "old_connection_name": "new_credential_id" }
    # When importing from a different instance, credential IDs/names differ.
    # This map lets the admin remap step connection_id / credential_id params
    # during import so the pipeline works in the target environment.
    connection_map: dict[str, str] = Field(default_factory=dict)


def build_pipeline_export_payload(
    wf,
    workspace_id: str,
    include_schedules: bool = False,
    include_alerts: bool = False,
) -> dict[str, Any]:
    """Serialise a Workflow into the portable, governance-stripped export
    shape (``format_version: 2``). Shared by the single-pipeline export
    (``GET /workflows/{id}/export``) and the project-level export
    (``GET /projects/{id}/export``) so both stay byte-identical.

    Lifecycle / governance state (status, approval, deployed_version,
    published_*, owner_id, test_results) is intentionally excluded — it is
    install-local and would create confusion if it crossed instances.
    Connection *references* are kept by id so the importer can remap them.
    """
    metadata = dict(wf.metadata or {})
    execution_settings = metadata.get("execution_settings", {}) or {}
    pipeline: dict[str, Any] = {
        "id": wf.id,
        "name": wf.name,
        "description": wf.description,
        "folder_id": getattr(wf, "folder_id", None),
        "parameters": [p.model_dump(mode="json") for p in (wf.parameters or [])],
        "steps": [s.model_dump(mode="json") for s in wf.steps],
        "connections": [c.model_dump(mode="json") for c in wf.connections],
        "metadata": metadata,
        "execution_settings": execution_settings,
    }
    if include_schedules:
        try:
            from fpulse.main import app_state
            sched_store = app_state.get("schedule_store")
            if sched_store is not None:
                pipeline["schedules"] = sched_store.list_by_workflow(
                    wf.id, workspace_id=workspace_id,
                )
        except Exception:
            pipeline["schedules"] = []
    if include_alerts:
        try:
            from fpulse.main import app_state
            alert_store = app_state.get("alert_store")
            if alert_store is not None:
                pipeline["alerts"] = alert_store.list_rules_by_workflow(
                    wf.id, workspace_id=workspace_id,
                )
        except Exception:
            pipeline["alerts"] = []
    return pipeline


@router.get("/{workflow_id}/export")
async def export_pipeline(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
    include_schedules: bool = False,
    include_alerts: bool = False,
):
    """Export a pipeline as portable JSON (for sharing or backup).

    Scoped to the caller's workspace. Connection references are kept by
    id so the importer can re-map them via the connection_map field on
    /import.

    2026-05-22 (audit D1) — schema bumped to ``format_version: 2``
    with the following additions over v1:

      * ``parameters`` — the pipeline's declared input parameters. v1
        silently omitted this, so exported workflows lost their
        ${param.X} bindings on import.
      * ``folder_id`` — purely informational (the importer can target a
        different folder via the import request body), but useful
        context for the operator inspecting the JSON.
      * ``execution_settings`` — surfaced from metadata as a top-level
        key so toolchains can read max_runtime / overlap policy
        without having to dig into the metadata blob.
      * ``schedules`` (optional) — included only when
        ``include_schedules=true`` so the default export stays small.
      * ``alerts`` (optional) — same opt-in semantics as schedules.

    Lifecycle / governance state is intentionally NOT exported:
    status, approval_status, deployed_version, published_*, owner_id,
    test_results. Those are install-local and would create confusion
    if "Approved on instance A" landed in instance B's database.
    """
    store = get_store()
    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    wf = v.workflow
    pipeline = build_pipeline_export_payload(
        wf, workspace_id,
        include_schedules=include_schedules,
        include_alerts=include_alerts,
    )

    return {
        "fpulse_version": "1.0.0",
        # Bumped 2026-05-22 — parameters/folder/schedules/alerts contract.
        "format_version": 2,
        "export_type": "pipeline",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "pipeline": pipeline,
    }


@router.post("/import", dependencies=[_AUTHOR])
async def import_pipeline(
    body: PipelineImportRequest,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Import a pipeline from exported JSON into the caller's
    workspace.

    Creates a new workflow with fresh IDs. The original step IDs in the
    export are preserved to keep connection references valid within the
    imported pipeline. The imported pipeline is stamped with the
    caller's workspace_id — imports NEVER cross tenant boundaries.
    """
    store = get_store()
    pipeline = body.pipeline

    if not pipeline or "steps" not in pipeline:
        raise HTTPException(400, "Invalid pipeline JSON — missing 'steps'")

    name = body.rename or pipeline.get("name", "Imported Pipeline")

    # 2026-05-22 (audit D1): restore parameters + folder_id if the
    # export carries them (format_version >= 2). v1 exports silently
    # omitted these so the import would lose every ${param.X}
    # binding the user had declared.
    parameters_blob = pipeline.get("parameters", []) or []
    parsed_parameters = []
    if parameters_blob:
        from fpulse.ir.schema import WorkflowParameter as _WP
        for p in parameters_blob:
            try:
                parsed_parameters.append(_WP(**p))
            except Exception:
                # Skip malformed parameter rows but log so the operator
                # can fix the export source. We never fail the whole
                # import just because one parameter blob is broken.
                logger.warning("Skipping invalid parameter in import: %r", p)

    # folder_id on the export is informational. The import body's
    # explicit project_id wins — the export's folder may not even
    # exist in the target workspace. If a caller wants to land the
    # workflow in a specific folder, the /folders move endpoint is
    # the second step.
    wf = Workflow(
        name=name,
        description=pipeline.get("description", ""),
        project_id=body.project_id,
        workspace_id=workspace_id,
        metadata=pipeline.get("metadata", {}),
        parameters=parsed_parameters,
    )

    # Rebuild steps from exported data, applying connection remapping
    from fpulse.ir.schema import Step, StepConnection, NodePosition
    conn_map = body.connection_map or {}
    remapped_count = 0

    for s in pipeline.get("steps", []):
        params = dict(s.get("params", {}))

        # Remap connection references if a mapping was provided.
        # Steps can reference connections via `connection_id`, `credential_id`,
        # or `connection_name` in their params dict.
        if conn_map:
            for key in ("connection_id", "credential_id", "connection_name"):
                old_val = params.get(key, "")
                if old_val and old_val in conn_map:
                    params[key] = conn_map[old_val]
                    remapped_count += 1

        step = Step(
            id=s.get("id", uuid.uuid4().hex[:8]),
            type=s.get("type", "filter"),
            label=s.get("label", ""),
            params=params,
            position=NodePosition(**(s.get("position", {}))),
            risk=s.get("risk", "low"),
        )
        wf.steps.append(step)

    for c in pipeline.get("connections", []):
        conn = StepConnection(**c)
        wf.connections.append(conn)

    version = store.save(wf, change_summary=f"Imported: {name}")
    lifecycle_store = get_lifecycle_store()
    lifecycle_store.add_event(
        wf.id, "created", f"Imported pipeline: {name}",
        workspace_id=workspace_id,
    )

    return {
        "id": wf.id,
        "name": wf.name,
        "version": version.version,
        "steps_imported": len(wf.steps),
        "connections_imported": len(wf.connections),
        "connections_remapped": remapped_count,
    }


@router.post("/{workflow_id}/clone", dependencies=[_AUTHOR])
async def clone_pipeline(
    workflow_id: str,
    name: str = "",
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Clone an existing pipeline into a new one. Scoped to the
    caller's workspace — you can only clone pipelines you can see, and
    the clone lands in your workspace."""
    store = get_store()
    v = store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Workflow not found")

    wf = v.workflow
    clone = Workflow(
        name=name or f"{wf.name} (Copy)",
        description=wf.description,
        project_id=wf.project_id,
        workspace_id=workspace_id,
        steps=[s.model_copy() for s in wf.steps],
        connections=[c.model_copy() for c in wf.connections],
        metadata=wf.metadata,
    )

    version = store.save(clone, change_summary=f"Cloned from {wf.name}")
    lifecycle_store = get_lifecycle_store()
    lifecycle_store.add_event(
        clone.id, "created", f"Cloned from {wf.name} ({workflow_id})",
        workspace_id=workspace_id,
    )

    return {
        "id": clone.id,
        "name": clone.name,
        "version": version.version,
        "cloned_from": workflow_id,
    }


# ── Data-size awareness ───────────────────────────────────────────────
# Frontend calls this when a user picks a file or before clicking Run to
# show volume tier, warnings, and scale-up hints without executing anything.

class _SourceInfoRequest(BaseModel):
    file_path: str


@router.post("/source-info")
async def source_info(body: _SourceInfoRequest):
    """Return file size metadata and volume-tier classification.

    The frontend uses this to show an inline badge on the source node
    (green/amber/orange/red) before the user clicks Run.
    """
    import os as _os
    from fpulse.nodes.guardrails import file_size_info

    data_dir = get_data_dir()
    path = body.file_path
    if not _os.path.isabs(path):
        path = _os.path.join(data_dir, path)

    info = file_size_info(path)
    return info


# ═══════════════════════════════════════════════════════════════════════
# Approval Gate Management
# ═══════════════════════════════════════════════════════════════════════

class _ApprovalGateBody(BaseModel):
    scope: str = "pipeline"  # pipeline | project | global
    scope_id: str = ""
    enabled: bool = True
    min_approvals: int = 1
    approvers: list[str] = Field(default_factory=list)
    notify_channels: list[str] = Field(default_factory=lambda: ["in_app"])


@router.get("/approval-gates")
async def list_approval_gates(
    scope: str | None = None,
    scope_id: str | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List approval gates. Filter by scope (pipeline/project/global) and scope_id."""
    db = _get_db()
    sql = "SELECT * FROM approval_gates WHERE workspace_id = ?"
    params: list = [workspace_id]
    if scope:
        sql += " AND scope = ?"
        params.append(scope)
    if scope_id:
        sql += " AND scope_id = ?"
        params.append(scope_id)
    sql += " ORDER BY scope, scope_id"

    import json as _json
    cursor = db.execute_with_retry(sql, params)
    rows = cursor.fetchall() if cursor else []
    return [
        {
            "id": r[0], "scope": r[1], "scope_id": r[2],
            "enabled": bool(r[3]), "min_approvals": r[4],
            "approvers": _json.loads(r[5] or "[]"),
            "notify_channels": _json.loads(r[6] or "[]"),
            "created_by": r[7], "created_at": r[8], "updated_at": r[9],
            "workspace_id": r[10],
        }
        for r in rows
    ]


@router.post("/approval-gates")
async def create_approval_gate(
    body: _ApprovalGateBody,
    created_by: str = "admin",
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create or update an approval gate for a pipeline, project, or globally."""
    import json as _json
    db = _get_db()
    now = datetime.now(timezone.utc).isoformat()

    # Check if gate already exists for this scope
    existing_cursor = db.execute_with_retry(
        "SELECT id FROM approval_gates WHERE scope = ? AND scope_id = ? AND workspace_id = ?",
        [body.scope, body.scope_id, workspace_id],
    )
    existing = existing_cursor.fetchall() if existing_cursor else []
    if existing:
        # Update existing
        gate_id = existing[0][0]
        db.execute_with_retry(
            """UPDATE approval_gates SET enabled = ?, min_approvals = ?,
               approvers = ?, notify_channels = ?, updated_at = ?
               WHERE id = ?""",
            [int(body.enabled), body.min_approvals,
             _json.dumps(body.approvers), _json.dumps(body.notify_channels),
             now, gate_id],
        )
        return {"id": gate_id, "action": "updated"}

    gate_id = uuid.uuid4().hex[:12]
    db.execute_with_retry(
        """INSERT INTO approval_gates
           (id, scope, scope_id, enabled, min_approvals, approvers,
            notify_channels, created_by, created_at, updated_at, workspace_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [gate_id, body.scope, body.scope_id, int(body.enabled),
         body.min_approvals, _json.dumps(body.approvers),
         _json.dumps(body.notify_channels), created_by, now, now, workspace_id],
    )
    return {"id": gate_id, "action": "created"}


@router.delete("/approval-gates/{gate_id}")
async def delete_approval_gate(
    gate_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Delete an approval gate."""
    db = _get_db()
    db.execute_with_retry(
        "DELETE FROM approval_gates WHERE id = ? AND workspace_id = ?",
        [gate_id, workspace_id],
    )
    return {"deleted": gate_id}


@router.get("/approval-gates/resolve/{workflow_id}")
async def resolve_approval_gate(
    workflow_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Resolve the effective approval gate for a pipeline.
    Priority: pipeline-specific > project-level > global."""
    import json as _json
    db = _get_db()
    store = get_store()

    v = store.get(workflow_id, workspace_id=workspace_id)
    project_id = v.workflow.project_id if v else "default"

    # Try pipeline-specific first
    for scope, sid in [("pipeline", workflow_id), ("project", project_id), ("global", "")]:
        cursor = db.execute_with_retry(
            "SELECT * FROM approval_gates WHERE scope = ? AND scope_id = ? AND workspace_id = ? AND enabled = 1",
            [scope, sid, workspace_id],
        )
        rows = cursor.fetchall() if cursor else []
        if rows:
            r = rows[0]
            return {
                "gate_id": r[0], "scope": r[1], "scope_id": r[2],
                "enabled": True, "min_approvals": r[4],
                "approvers": _json.loads(r[5] or "[]"),
                "notify_channels": _json.loads(r[6] or "[]"),
                "resolved_from": scope,
            }

    return {"enabled": False, "approvers": [], "resolved_from": None}


# ═══════════════════════════════════════════════════════════════════════
# Approval Notifications (In-App Notification Cards)
# ═══════════════════════════════════════════════════════════════════════

@router.get("/approval-notifications")
async def list_notifications(
    unread_only: bool = False,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List approval notifications for the current workspace."""
    import json as _json
    db = _get_db()
    sql = "SELECT * FROM approval_notifications WHERE workspace_id = ?"
    params: list = [workspace_id]
    if unread_only:
        sql += " AND read = 0"
    sql += " ORDER BY created_at DESC LIMIT 100"

    cursor = db.execute_with_retry(sql, params)
    rows = cursor.fetchall() if cursor else []
    return [
        {
            "id": r[0], "workflow_id": r[1], "workflow_name": r[2],
            "recipient_id": r[3], "sender_id": r[4], "action": r[5],
            "message": r[6], "card_data": _json.loads(r[7] or "{}"),
            "read": bool(r[8]), "created_at": r[9], "workspace_id": r[10],
        }
        for r in rows
    ]


@router.post("/approval-notifications/{notification_id}/read")
async def mark_notification_read(notification_id: str):
    """Mark a notification as read."""
    db = _get_db()
    db.execute_with_retry(
        "UPDATE approval_notifications SET read = 1 WHERE id = ?",
        [notification_id],
    )
    return {"marked_read": notification_id}


@router.post("/approval-notifications/read-all")
async def mark_all_read(
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Mark all notifications as read for the workspace."""
    db = _get_db()
    db.execute_with_retry(
        "UPDATE approval_notifications SET read = 1 WHERE workspace_id = ? AND read = 0",
        [workspace_id],
    )
    return {"status": "all_read"}


def _create_approval_notification(
    db: Any,
    workflow_id: str,
    workflow_name: str,
    recipients: list[str],
    sender_id: str,
    action: str,
    message: str,
    card_data: dict,
    workspace_id: str = "default",
):
    """Internal helper — create notification cards for each recipient."""
    import json as _json
    now = datetime.now(timezone.utc).isoformat()
    for recipient in recipients:
        nid = uuid.uuid4().hex[:12]
        db.execute_with_retry(
            """INSERT INTO approval_notifications
               (id, workflow_id, workflow_name, recipient_id, sender_id,
                action, message, card_data, read, created_at, workspace_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            [nid, workflow_id, workflow_name, recipient, sender_id,
             action, message, _json.dumps(card_data), now, workspace_id],
        )
