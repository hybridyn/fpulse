"""Connections CRUD API — reusable saved connections with parameterized reports."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from fpulse.connections.models import (
    Connection,
    ConnectionCreate,
    default_capabilities,
    ConnectionUpdate,
    ConnectionReport,
    ReportCreate,
    ReportUpdate,
    RunReportRequest,
    CONNECTION_TYPES,
    VALID_ENVIRONMENTS,
)
from fpulse.connections.tester import ConnectionTester
from fpulse.connections.catalog import get_catalog, registry_status
from fpulse.connections.credentials import resolve_credentials
from fpulse.auth.deps import (
    current_workspace_id,
    require_auth,
    require_admin,
)

router = APIRouter(prefix="/api/connections", tags=["connections"])


def _safe_workspace_id(request: Request) -> str:
    """Wrap current_workspace_id so dep failures surface as readable
    HTTP errors — same pattern as api/projects.py and api/workflows.py."""
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc

_tester = ConnectionTester()


def get_store():
    from fpulse.main import app_state
    return app_state["connection_store"]


def _invalidate_lineage(workspace_id: str) -> None:
    """Z25 (2026-05-23) — when a connection is created / updated / deleted
    the credential-usage map (which connections reference each credential)
    becomes stale. The connection-usage map only depends on workflow edits,
    so it is invalidated from versioning.py; here we only need credentials.

    Best-effort — never block the mutation if cache invalidation hiccups.
    """
    try:
        from fpulse.datastore.usage import invalidate_credential_usage
        invalidate_credential_usage(workspace_id)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# Project / credential validation helpers (audit G1 — 2026-05-22)
# ─────────────────────────────────────────────────────────────────────
#
# Previously create_connection / update_connection accepted whatever
# project_id / credential_id the caller posted, without verifying:
#   * the project actually exists
#   * the project lives in the caller's workspace
#   * the project is visible to the caller per ACL
#   * the credential exists
#   * the credential is in the caller's workspace
#
# The audit flagged this as "cross-project or cross-workspace references"
# / "broken connections that cannot test later". These helpers centralise
# the validation so create + update share the same rules.

def _validate_project_assignment(
    project_id: str | None,
    workspace_id: str,
    user,
) -> None:
    """Raise HTTPException(404) if project_id is not a real, visible project
    in this workspace. None / "" / "default" are short-circuit OK — they
    mean "global / unscoped".

    Uses the assert_project_access helper from the projects/acl module so
    workflow create + connection create share the same gate.
    """
    pid = (project_id or "").strip()
    if not pid or pid == "default":
        return
    from fpulse.projects.acl import assert_project_access
    assert_project_access(pid, workspace_id, user, action="attach_connection")


def _validate_environment(env: str | None) -> None:
    """Refuse a create / update whose environment value isn't in the
    canonical set (audit G4). None is OK — it means "unscoped / all
    envs" for legacy rows. Free-form strings used to be accepted
    silently, then the frontend's strict env filter hid the row from
    everyone because the string didn't match dev/prod/all.
    """
    if env is None or env == "":
        return
    if env not in VALID_ENVIRONMENTS:
        raise HTTPException(
            400,
            f"Invalid environment {env!r}. Must be one of: "
            f"{sorted(VALID_ENVIRONMENTS)} (or omit for unscoped).",
        )


def _validate_credential_assignment(
    credential_id: str | None,
    workspace_id: str,
) -> None:
    """Raise HTTPException(404) if credential_id doesn't belong to the
    caller's workspace. None / "" are short-circuit OK (no credential
    attached — connection.config is the source of truth)."""
    cid = (credential_id or "").strip()
    if not cid:
        return
    try:
        from fpulse.main import app_state
        cred_store = app_state.get("credential_store")
    except Exception:
        cred_store = None
    if cred_store is None:
        # No store wired — best-effort fail-open so a test env without
        # the store doesn't block every connection create. Production
        # always has the store.
        return
    cred = cred_store.get_raw(cid)
    if cred is None:
        raise HTTPException(404, "Credential not found")
    cred_ws = getattr(cred, "workspace_id", None) or "default"
    if cred_ws != workspace_id:
        # Cross-workspace — 404 to hide existence in another tenant.
        raise HTTPException(404, "Credential not found")


@router.get("/types")
async def list_connection_types(detail: bool = False):
    """Return all supported connection types.

    2026-05-22 (audit I3) — when ``detail=true``, returns a list of
    dicts with per-type readiness flags so the frontend can compute
    "certified" / "beta" from real backend capabilities instead of
    a hand-maintained allowlist that drifted. Each entry carries:

      * type           — the canonical string
      * tester         — true iff /api/connections/test-inline can
                         actually probe this type
      * catalog        — true iff /api/connections/{id}/catalog
                         returns a real registry provider
      * source_node    — true iff a StepType source for this type is
                         registered with the executor
      * sink_node      — true iff a sink StepType is registered
      * report         — true iff /api/connections/{id}/reports/.../run
                         knows how to execute against this type

    "certified" is then the AND of (tester, catalog OR source, sink)
    or a subset depending on connector class — leaving the policy to
    the frontend keeps it editable without a backend roll.

    The default (detail=false) preserves the old list-of-strings
    shape so existing clients keep working.
    """
    if not detail:
        return CONNECTION_TYPES

    # Collect per-dimension support from the real sources of truth.
    from fpulse.connections.tester import ConnectionTester as _Tester
    try:
        from fpulse.connections.catalog import _PROVIDERS as _CATALOG  # type: ignore
    except Exception:
        _CATALOG = {}
    # Source / sink node registration. The IR enum names follow the
    # ``<type>_source`` / ``<type>_sink`` convention for most
    # connectors, with a handful of generic-form exceptions.
    from fpulse.ir.schema import StepType
    enum_values = {st.value for st in StepType}

    tester_types = set(_Tester._TESTERS.keys())
    # Reports today: only the four DB connection types execute real
    # SQL queries through run_report. Everything else falls back to
    # the demo path (which after H3 only fires for no-credential
    # connections).
    # 2026-05-23 (T4 + U1/U2): oracle_fusion + oracle_bip are the canonical
    # Oracle REST report-runner targets. oracle_api stays as a legacy
    # alias. sap_s4hana / sap_successfactors join when the OData
    # report adapter lands; for now they're catalog+test only.
    report_types = {
        "postgresql", "mysql", "sqlite", "mssql",
        "rest_api", "oracle_api", "oracle_fusion", "oracle_bip",
    }

    items: list[dict] = []
    for t in CONNECTION_TYPES:
        source_node = f"{t}_source" in enum_values
        sink_node = f"{t}_sink" in enum_values
        items.append({
            "type": t,
            "tester": t in tester_types,
            "catalog": t in (_CATALOG or {}),
            "source_node": source_node,
            "sink_node": sink_node,
            "report": t in report_types,
        })
    return items


@router.get("/metadata")
async def connection_metadata():
    """Return connection type categories, storage types, and file formats
    for the Source/Destination config panel."""
    from fpulse.connections.models import CONNECTION_CATEGORIES, STORAGE_TYPES, FILE_FORMATS
    return {
        "types": CONNECTION_TYPES,
        "categories": CONNECTION_CATEGORIES,
        "storage_types": list(STORAGE_TYPES),
        "file_formats": FILE_FORMATS,
    }


# 2026-05-30 (P7): trailing-slash alias.
@router.get("", include_in_schema=False)
@router.get("/")
async def list_connections(
    type: str | None = None,
    project_id: str | None = None,
    scope: str | None = None,
    _user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List connections scoped to the caller's current workspace.

    2026-05-22 (audit F1): requires authenticated user. Previously
    any caller able to reach the API and provide a workspace header
    could enumerate connections — the audit flagged this as the
    top connections-page security gap. Workspace scope still applies.
    """
    store = get_store()
    return store.list_all(
        conn_type=type,
        project_id=project_id,
        scope=scope,
        workspace_id=workspace_id,
    )


# ── Z25 (2026-05-23) — Connection → Pipelines lineage ─────────────────────


@router.get("/usage")
async def get_connections_usage(
    _user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Bulk lineage map: which pipelines reference each connection.

    Returns ``{connection_id: [{workflow_id, name, role}, ...]}`` —
    same UsageRef shape the Storage page uses. Empty bucket means
    no pipeline references that connection. Cached for 30s; cache
    invalidates on workflow save (handled in ir/versioning.py).
    """
    from fpulse.datastore.usage import compute_connection_usage_cached
    return compute_connection_usage_cached(workspace_id)


@router.post("", include_in_schema=False)
@router.post("/")
async def create_connection(
    body: ConnectionCreate,
    request: Request,
    _user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create a new connection in the caller's current workspace.

    Connection names are auto-suffixed on collision within the same
    workspace, so saving "prod-snowflake" twice yields the second as
    "prod-snowflake (2)" rather than two indistinguishable rows.
    """
    # 2026-05-22: validate body.type against the canonical type registry
    # BEFORE persisting. The test-inline endpoint already did this; create
    # and update did not, so garbage types like "totally_fake_db" were
    # silently saved and only surfaced as runtime failures during pipeline
    # execution. Reject at the API boundary instead.
    if body.type not in CONNECTION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown connection type {body.type!r}. "
                f"Valid types: {sorted(CONNECTION_TYPES)[:10]}… "
                f"(see GET /api/connections/types for the full list)."
            ),
        )

    from fpulse.common.unique_name import ensure_unique_name
    store = get_store()
    # 2026-05-22 fix: ConnectionStore exposes `list_all`, not `list`. The
    # previous call hit AttributeError and the silent except below swallowed
    # it, so the promised "auto-suffix on collision" docstring quietly
    # never fired — two duplicates saved with identical names. The except
    # is intentionally narrow now (only catches AttributeError from older
    # store implementations during local development) so a genuine store
    # outage surfaces instead of being papered over.
    existing_names: set[str] = set()
    try:
        for c in store.list_all(workspace_id=workspace_id):
            n = getattr(c, "name", None) or (c.get("name") if isinstance(c, dict) else None)
            if n:
                existing_names.add(n)
    except AttributeError:
        # Legacy store without list_all — log a warning so the gap is visible
        # in operator logs rather than silently producing duplicate names.
        import logging
        logging.getLogger(__name__).warning(
            "ConnectionStore is missing list_all(); name-uniqueness check skipped. "
            "Upgrade your ConnectionStore implementation.",
        )
    unique_name = ensure_unique_name(body.name, existing_names)

    # ── Project + credential scope validation (audit G1) ─────────────
    # Refuse a create that points at a project the caller can't see or
    # a credential from another workspace. 404 on either failure so
    # cross-tenant ids can't be enumerated through this endpoint.
    from fpulse.auth.deps import current_user_optional
    user = current_user_optional(request)
    if user is not None:
        _validate_project_assignment(body.project_id, workspace_id, user)
    _validate_credential_assignment(body.credential_id, workspace_id)
    _validate_environment(getattr(body, "environment", None))

    # Capabilities resolution: caller can pass an explicit list, otherwise
    # we pick the type-appropriate default (notification connectors default
    # to write-only, everything else to ['read', 'write']).
    caps = body.capabilities if body.capabilities is not None else default_capabilities(body.type)
    connection = Connection(
        name=unique_name,
        type=body.type,
        config=body.config,
        credential_id=body.credential_id,
        project_id=body.project_id,
        workspace_id=workspace_id,
        description=body.description,
        tags=body.tags,
        # Carry the env scope set by the create form (Apr 21 2026 feature).
        # Defaults to None which the strict frontend filter then hides
        # — same behaviour as legacy untagged rows.
        environment=getattr(body, "environment", None),
        capabilities=caps,
    )
    created = store.create(connection)
    _invalidate_lineage(workspace_id)
    return created.model_dump(mode="json")


@router.get("/{connection_id}")
async def get_connection(
    connection_id: str,
    _user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Get a connection by ID — 404s across workspace boundary."""
    store = get_store()
    connection = store.get(connection_id, workspace_id=workspace_id)
    if not connection:
        raise HTTPException(404, "Connection not found")
    return connection.model_dump(mode="json")


@router.put("/{connection_id}")
async def update_connection(
    connection_id: str,
    body: ConnectionUpdate,
    request: Request,
    _user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Update a connection — refuses to cross workspace boundary.

    Credential-rotation hook: after a successful update, invalidate any
    pooled driver connections for this id so the next run gets a fresh
    connection with the updated credentials. Per Critical #5 / D-002.
    """
    # 2026-05-22: validate body.type if present. Updates are partial, so
    # only check when the caller actually supplied a type; absence means
    # "leave the existing type alone".
    if body.type is not None and body.type not in CONNECTION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown connection type {body.type!r}. "
                f"Valid types: {sorted(CONNECTION_TYPES)[:10]}… "
                f"(see GET /api/connections/types for the full list)."
            ),
        )

    # ── Project + credential scope validation on update (audit G1) ──
    # Same gate as create. If body re-points the connection at a
    # different project or credential, validate them. Body fields that
    # weren't set (Pydantic-unset) are not validated — we only check
    # what the client tried to change.
    from fpulse.auth.deps import current_user_optional
    user = current_user_optional(request)
    body_set = body.model_fields_set
    if "project_id" in body_set and user is not None:
        _validate_project_assignment(body.project_id, workspace_id, user)
    if "credential_id" in body_set:
        _validate_credential_assignment(body.credential_id, workspace_id)
    if "environment" in body_set:
        _validate_environment(body.environment)

    store = get_store()
    # 2026-05-22 (audit G2) — exclude_unset, not exclude_none. Previously
    # sending project_id: null silently dropped the field from the
    # update so a connection couldn't be moved from a project scope
    # back to global through this endpoint. exclude_unset preserves
    # explicit nulls and lets the store distinguish "field omitted"
    # (no change) from "field set to null" (clear it).
    updates = body.model_dump(exclude_unset=True)
    connection = store.update(connection_id, updates, workspace_id=workspace_id)
    if not connection:
        raise HTTPException(404, "Connection not found")
    # Invalidate pooled connections — best-effort, never block the update.
    try:
        from fpulse.main import app_state
        pool = app_state.get("connection_pool")
        if pool is not None:
            pool.invalidate_connection(connection_id)
    except Exception:
        pass
    _invalidate_lineage(workspace_id)
    return connection.model_dump(mode="json")


@router.post("/{connection_id}/move")
async def move_connection(
    connection_id: str,
    target_project_id: str | None = None,
    _user = Depends(require_admin),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Reassign a connection to a different project (or to global).

    Pass ``target_project_id`` empty / null to make the connection
    global (visible to every project in the workspace). Validates the
    target project exists when provided.
    """
    store = get_store()
    target = (target_project_id or "").strip()
    if target:
        from fpulse.main import app_state
        proj_store = app_state.get("project_store")
        if proj_store is not None and proj_store.get(target) is None:
            raise HTTPException(404, f"Target project '{target}' does not exist")
    # Use the existing update path so workspace-scoping + audit hooks
    # all behave the same as a normal edit.
    updated = store.update(
        connection_id,
        {"project_id": target or None},
        workspace_id=workspace_id,
    )
    if not updated:
        raise HTTPException(404, "Connection not found")
    return {"moved": True, "connection_id": connection_id, "project_id": target or None}


@router.delete("/{connection_id}")
async def delete_connection(
    connection_id: str,
    _user = Depends(require_admin),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Delete a connection and its reports — scoped to workspace.

    Also invalidates any pooled driver connections for this id so they
    don't outlive the deletion.
    """
    store = get_store()
    if not store.delete(connection_id, workspace_id=workspace_id):
        raise HTTPException(404, "Connection not found")
    # Invalidate pooled connections — best-effort.
    try:
        from fpulse.main import app_state
        pool = app_state.get("connection_pool")
        if pool is not None:
            pool.invalidate_connection(connection_id)
    except Exception:
        pass
    _invalidate_lineage(workspace_id)
    return {"deleted": True}


@router.post("/{connection_id}/test")
async def test_connection(
    connection_id: str,
    _user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Test a saved connection — performs real protocol-level connectivity check."""
    store = get_store()
    connection = store.get(connection_id, workspace_id=workspace_id)
    if not connection:
        raise HTTPException(404, "Connection not found")

    # Merge connection config with credential config
    config = dict(connection.config)
    if connection.credential_id:
        try:
            from fpulse.main import app_state
            cred_store = app_state.get("credential_store")
            if cred_store:
                # Pass the workspace so a connection can't deref a credential
                # from another tenant (defense-in-depth — the connection is
                # already workspace-scoped above).
                cred = cred_store.get_raw(
                    connection.credential_id, workspace_id=workspace_id
                )
                if cred and cred.config:
                    config.update(cred.config)
        except Exception:
            pass

    result = _tester.test_connection(connection.type, config)

    # Audit — a connection test decrypts + uses a credential against an
    # external system (credential use).
    try:
        from fpulse.audit import audit_action, actor_for
        audit_action(
            action="connection.test",
            resource_type="connection",
            resource_id=connection_id,
            actor=actor_for(_user),
            workspace_id=workspace_id,
            details={
                "type": connection.type,
                "used_credential": bool(connection.credential_id),
                "ok": bool(result.get("success")),
            },
        )
    except Exception:
        pass

    # Persist the outcome on the connection so the list view's
    # HEALTHY / STALE / FAILING stat cards and the per-row "last test"
    # column reflect reality. Keep the error string short — the full
    # pyodbc dump goes back to the caller in `result.error`.
    from datetime import datetime, timezone
    error_summary = (result.get("error") or result.get("message") or "")[:300] if not result.get("success") else ""
    try:
        store.update(
            connection_id,
            {
                "last_test_at": datetime.now(timezone.utc),
                "last_test_ok": bool(result.get("success")),
                "last_test_error": error_summary,
            },
            workspace_id=workspace_id,
        )
    except Exception:
        # Persisting the test record is best-effort — never block the
        # actual test result on a storage hiccup.
        pass

    # 2026-06-07 — also stamp Steward's connector-health sidecar so the
    # connector-health detector picks up the streak / error-class on
    # the next scan. Wrapped in best-effort try/except: a Steward
    # outage MUST NOT break the user's connection-test workflow.
    try:
        from fpulse.steward import ConnectorHealthStore, record_test_outcome
        from fpulse.main import app_state
        from pathlib import Path
        steward_dir = Path(app_state["data_dir"]) / "steward" / workspace_id
        steward_dir.mkdir(parents=True, exist_ok=True)
        record_test_outcome(
            ConnectorHealthStore(steward_dir / "connector_health.json"),
            connection_id=connection_id,
            ok=bool(result.get("success")),
            error_message=error_summary,
        )
    except Exception:
        pass

    return result


@router.get("/_catalog/registry")
async def get_catalog_registry():
    """Audit endpoint — exposes which connector types have real catalog
    providers, which are intentional no-catalog (write-only), and which
    are planned placeholders. Counts are derived from the registry, not
    hand-edited, so the numbers in docs/release notes can be cross-checked
    against this endpoint."""
    return registry_status()


@router.get("/{connection_id}/columns")
async def get_connection_table_columns(
    connection_id: str,
    table: str,
    schema: str = "",
    _user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Per-table column introspection used by the Mapping tab.

    Returns the list of columns + DB types for one table on the named
    connection — separate from the broader /catalog endpoint which
    only enumerates table-level objects. Supports the same RDBMS set
    as the Database Sink writer (postgresql, mysql, mssql, sqlite);
    other connection types respond 400 so the UI can fall back to
    free-text input.
    """
    store = get_store()
    connection = store.get(connection_id, workspace_id=workspace_id)
    if not connection:
        raise HTTPException(404, "Connection not found")

    # 2026-05-22 (audit I2): build config from the already-loaded,
    # workspace-validated connection object. The previous
    # `_get_connection_config(connection_id)` did its own
    # workspace-agnostic lookup, which technically bypassed the
    # workspace boundary already enforced two lines above. Use the
    # workspace-scoped credential resolver instead.
    from fpulse.nodes.db_source import describe_table_columns
    from fpulse.connections.credentials import resolve_credentials
    config = resolve_credentials(connection)
    conn_type = connection.type
    if not config:
        raise HTTPException(404, "Connection config not resolvable")
    try:
        cols = describe_table_columns(conn_type, config, schema, table)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Schema introspection failed: {e}")
    return {"columns": cols, "schema": schema, "table": table}


@router.get("/{connection_id}/catalog")
async def get_connection_catalog(
    connection_id: str,
    _user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Browse the catalog (tables / views / procedures / synonyms /
    collections / topics / buckets / …) for a saved connection.

    Returns a uniform `Catalog` shape regardless of connector type:
    `{supported, reason, items: [{name, kind, parent, metadata}], parents, kinds}`.
    Frontend uses `parents`/`kinds` for grouping. When `supported=false`,
    the free-text input remains the user's path.
    """
    store = get_store()
    connection = store.get(connection_id, workspace_id=workspace_id)
    if not connection:
        raise HTTPException(404, "Connection not found")

    # Substrate credential resolver — Vault first when bound, falls back
    # to the legacy credential_id link and connection.config in that
    # order. Per architecture decision: refresh tokens / client secrets
    # belong in Vault, not connection.config.
    config = resolve_credentials(connection)
    catalog = get_catalog(connection.type, config)
    return catalog.model_dump(mode="json")


# Z32 (2026-05-23) — POST /api/connections/{id}/scaffold-cleanup removed
# along with its frontend wand. The on-row "Pipeline Data Prep" wand built
# a 3-node draft pipeline (source → wrangler → local_table_sink) from a
# stream pick. User flagged it as unhelpful — discovery happens better
# through the Editor's source palette + the connection's catalog
# endpoint. The shared helper `build_connection_cleanup_workflow` was
# the only consumer of this route; both removed together.


class InlineTestRequest(BaseModel):
    """Test a connection config before saving it."""
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    credential_id: str | None = None


@router.post("/test-inline")
async def test_connection_inline(
    body: InlineTestRequest,
    _user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Test connection settings without saving — use before creating a connection.

    2026-05-22 (audit F2) — previously this endpoint had no auth and
    no workspace dep, AND it would merge whatever credential_id was
    supplied without checking the credential belonged to the caller.
    That made it usable as:
      * an open server-side network probe (the server connects from
        the F-Pulse host wherever the body says)
      * a credential-id oracle (guess an id, see if connect-attempt
        timing reveals existence)
      * a credential-config leak vector (merge bypasses workspace
        boundary)
    Now: requires auth, requires workspace dep, and any supplied
    credential_id must belong to that workspace.
    """
    if body.type not in CONNECTION_TYPES:
        raise HTTPException(400, f"Unknown connection type: {body.type}")

    config = dict(body.config)

    # Credential validation (audit F2). Reject silently-unknown
    # credential ids with a 404 instead of falling through to a
    # configless test — that would mask the real problem (typo /
    # cross-workspace id) as a generic connection failure.
    if body.credential_id:
        try:
            from fpulse.main import app_state
            cred_store = app_state.get("credential_store")
            if cred_store is None:
                raise HTTPException(500, "Credential store unavailable")
            cred = cred_store.get_raw(body.credential_id)
            if cred is None:
                # Same 404 shape used elsewhere so a caller can't
                # enumerate credential ids by guessing.
                raise HTTPException(404, "Credential not found")
            cred_ws = getattr(cred, "workspace_id", None) or "default"
            if cred_ws != workspace_id:
                # Cross-workspace credential — 404 to avoid leaking
                # existence in another tenant.
                raise HTTPException(404, "Credential not found")
            if cred.config:
                config.update(cred.config)
        except HTTPException:
            raise
        except Exception:
            # Any other store error → fail closed. Better to refuse
            # the test than to silently fall back to a misleading
            # configless probe.
            raise HTTPException(500, "Credential resolution failed")

    result = _tester.test_connection(body.type, config)
    return result


# ── Reports ──


@router.get("/{connection_id}/reports")
async def list_reports(
    connection_id: str,
    _user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List all reports for a connection — scoped via parent connection workspace."""
    store = get_store()
    if not store.get(connection_id, workspace_id=workspace_id):
        raise HTTPException(404, "Connection not found")
    return store.list_reports(connection_id)


@router.post("/{connection_id}/reports")
async def create_report(
    connection_id: str,
    body: ReportCreate,
    _user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create a report for a connection — scoped via parent connection workspace."""
    store = get_store()
    if not store.get(connection_id, workspace_id=workspace_id):
        raise HTTPException(404, "Connection not found")
    report = ConnectionReport(
        connection_id=connection_id,
        name=body.name,
        description=body.description,
        query_template=body.query_template,
        parameters=body.parameters,
    )
    created = store.create_report(report)
    return created.model_dump(mode="json")


@router.put("/{connection_id}/reports/{report_id}")
async def update_report(
    connection_id: str,
    report_id: str,
    body: ReportUpdate,
    _user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Update a report — scoped via parent connection workspace."""
    store = get_store()
    if not store.get(connection_id, workspace_id=workspace_id):
        raise HTTPException(404, "Report not found")
    report = store.get_report(report_id)
    if not report or report.connection_id != connection_id:
        raise HTTPException(404, "Report not found")
    updates = body.model_dump(exclude_none=True)
    updated = store.update_report(report_id, updates)
    if not updated:
        raise HTTPException(404, "Report not found")
    return updated.model_dump(mode="json")


@router.delete("/{connection_id}/reports/{report_id}")
async def delete_report(
    connection_id: str,
    report_id: str,
    _user = Depends(require_admin),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Delete a report — scoped via parent connection workspace."""
    store = get_store()
    if not store.get(connection_id, workspace_id=workspace_id):
        raise HTTPException(404, "Report not found")
    report = store.get_report(report_id)
    if not report or report.connection_id != connection_id:
        raise HTTPException(404, "Report not found")
    if not store.delete_report(report_id):
        raise HTTPException(404, "Report not found")
    return {"deleted": True}


@router.post("/{connection_id}/reports/{report_id}/run")
async def run_report(
    connection_id: str,
    report_id: str,
    body: RunReportRequest | None = None,
    _user = Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Run a report — scoped via parent connection workspace."""
    store = get_store()
    if not store.get(connection_id, workspace_id=workspace_id):
        raise HTTPException(404, "Connection not found")
    params = body.params if body else {}
    result = store.run_report(connection_id, report_id, params)
    if result.status == "error":
        raise HTTPException(400, result.error)
    return result.model_dump(mode="json")
