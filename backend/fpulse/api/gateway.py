"""API Gateway — manage API keys, published endpoints, and invoke published pipelines."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from fpulse.auth.deps import current_workspace_id, require_auth, require_min_rank

router = APIRouter(tags=["gateway"])


def _get_store(request: Request):
    from fpulse.main import app_state
    return app_state["gateway_store"]


# ── API Key Management ────────────────────────────────────────────────

class CreateKeyRequest(BaseModel):
    name: str
    scopes: list[str] = ["read", "execute"]
    rate_limit_rpm: int = 60
    ip_allowlist: list[str] = []
    expires_days: int = 0


@router.get("/api/gateway/keys")
async def list_keys(request: Request, workspace_id: str = Depends(current_workspace_id),
                    _user=Depends(require_auth)):
    store = _get_store(request)
    return store.list_api_keys(workspace_id)


@router.post("/api/gateway/keys")
async def create_key(body: CreateKeyRequest, request: Request,
                     workspace_id: str = Depends(current_workspace_id),
                     _user=Depends(require_min_rank("data_engineer"))):
    store = _get_store(request)
    user_id = getattr(_user, "id", "anonymous")
    return store.create_api_key(
        name=body.name, workspace_id=workspace_id, created_by=user_id,
        scopes=body.scopes, rate_limit_rpm=body.rate_limit_rpm,
        ip_allowlist=body.ip_allowlist, expires_days=body.expires_days,
    )


@router.delete("/api/gateway/keys/{key_id}")
async def revoke_key(key_id: str, request: Request,
                     _user=Depends(require_min_rank("data_engineer"))):
    store = _get_store(request)
    store.revoke_api_key(key_id)
    return {"status": "revoked"}


# ── Published Endpoints Management ────────────────────────────────────

class PublishEndpointRequest(BaseModel):
    workflow_id: str
    path: str
    name: str = ""
    description: str = ""
    method: str = "POST"
    require_api_key: bool = True
    rate_limit_rpm: int = 30
    timeout_seconds: int = 300
    input_schema: dict = {}


class UpdateEndpointRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    rate_limit_rpm: int | None = None
    timeout_seconds: int | None = None
    is_active: int | None = None
    require_api_key: bool | None = None


@router.get("/api/gateway/endpoints")
async def list_endpoints(request: Request, workspace_id: str = Depends(current_workspace_id),
                         _user=Depends(require_auth)):
    store = _get_store(request)
    return store.list_endpoints(workspace_id)


@router.post("/api/gateway/endpoints")
async def publish_endpoint(body: PublishEndpointRequest, request: Request,
                           workspace_id: str = Depends(current_workspace_id),
                           _user=Depends(require_min_rank("data_engineer"))):
    store = _get_store(request)
    return store.publish_endpoint(
        workflow_id=body.workflow_id, path=body.path, name=body.name,
        description=body.description, method=body.method, workspace_id=workspace_id,
        require_api_key=body.require_api_key, rate_limit_rpm=body.rate_limit_rpm,
        timeout_seconds=body.timeout_seconds, input_schema=body.input_schema,
    )


@router.put("/api/gateway/endpoints/{endpoint_id}")
async def update_endpoint(endpoint_id: str, body: UpdateEndpointRequest, request: Request,
                          _user=Depends(require_min_rank("data_engineer"))):
    store = _get_store(request)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    result = store.update_endpoint(endpoint_id, **updates)
    if not result:
        raise HTTPException(404, "Endpoint not found")
    return result


@router.delete("/api/gateway/endpoints/{endpoint_id}")
async def delete_endpoint(endpoint_id: str, request: Request,
                          _user=Depends(require_min_rank("data_engineer"))):
    store = _get_store(request)
    store.delete_endpoint(endpoint_id)
    return {"status": "deleted"}


# ── Public endpoint invocation ────────────────────────────────────────

@router.api_route(
    "/api/published/{path:path}",
    methods=["GET", "POST", "PUT"],
    # Dynamic catch-all gateway for user-published pipelines — the path is
    # arbitrary per user, so it can't be meaningfully documented, and the
    # multi-method registration produced a duplicate OpenAPI operation id.
    # Keep it out of the schema (it still serves requests normally).
    include_in_schema=False,
)
async def invoke_published_endpoint(path: str, request: Request):
    """Invoke a published pipeline endpoint.

    Validates API key (via X-API-Key header or ?api_key query param),
    checks rate limits, then executes the associated pipeline.
    """
    store = _get_store(request)
    endpoint = store.get_endpoint_by_path(path)
    if not endpoint:
        raise HTTPException(404, f"No published endpoint at: /{path}")

    if not endpoint.get("is_active"):
        raise HTTPException(503, "Endpoint is currently disabled")

    # API key validation
    key_info = None
    if endpoint.get("require_api_key"):
        from fpulse import runtime_config
        header_key = request.headers.get("X-API-Key")
        # Server mode: accept the key ONLY via the header — never a query
        # string, which leaks into access logs, proxies, and browser history.
        # Local mode keeps ?api_key= for convenience.
        if runtime_config.IS_SERVER_MODE:
            api_key = header_key
            hint = "Pass it in the X-API-Key header."
        else:
            api_key = header_key or request.query_params.get("api_key")
            hint = "Pass via X-API-Key header or ?api_key param."
        if not api_key:
            raise HTTPException(401, f"API key required. {hint}")
        key_info = store.validate_api_key(api_key, required_scope="execute")
        if not key_info:
            raise HTTPException(403, "Invalid or expired API key")

        # Rate limiting
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        rpm = min(endpoint.get("rate_limit_rpm", 30), key_info.get("rate_limit_rpm", 60))
        if not store.check_rate_limit(key_hash, rpm, endpoint["id"]):
            raise HTTPException(429, f"Rate limit exceeded ({rpm} requests/minute)")

    # Record the call
    store.record_call(endpoint["id"])

    # Execute the pipeline
    try:
        body = await request.json() if request.method in ("POST", "PUT") else {}
    except Exception:
        body = {}

    from fpulse.main import app_state
    wf_store = app_state["store"]
    wf_version = wf_store.get(endpoint["workflow_id"])
    if not wf_version:
        raise HTTPException(404, "Associated workflow not found")

    # Tenant scope: the published endpoint is workspace-bound. Confirm the
    # workflow it points at actually lives in that workspace, so a stale or
    # tampered endpoint row can't invoke another tenant's pipeline by id
    # alone. No-op when either side lacks a workspace_id (legacy single-
    # workspace installs).
    endpoint_ws = endpoint.get("workspace_id")
    workflow_ws = getattr(wf_version.workflow, "workspace_id", None)
    if endpoint_ws and workflow_ws and endpoint_ws != workflow_ws:
        raise HTTPException(404, "Associated workflow not found")

    # Run via the execution engine
    from fpulse.engine.executor import WorkflowExecutor
    executor = WorkflowExecutor(app_state=app_state)
    result = executor.execute(wf_version.workflow, params=body)

    # Audit every gateway invocation. This is an unauthenticated-by-user,
    # API-key-authed external entry point that executes a pipeline, so it
    # must leave a trail (who/which key, which endpoint, from where, outcome).
    try:
        audit_logger = app_state.get("audit_logger")
        if audit_logger:
            ki = key_info or {}
            client_ip = getattr(getattr(request, "client", None), "host", None)
            audit_logger.log(
                user_id=str(ki.get("id") or "api-key"),
                user_email=str(ki.get("name") or "gateway"),
                action="gateway.invoke",
                resource_type="published_endpoint",
                resource_id=path,
                details={
                    "workflow_id": endpoint["workflow_id"],
                    "workspace_id": endpoint.get("workspace_id"),
                    "method": request.method,
                    "client_ip": client_ip,
                    "outcome": "error" if result.get("error") else "success",
                },
            )
    except Exception:
        pass

    return {
        "status": "success" if not result.get("error") else "error",
        "endpoint": f"/{path}",
        "workflow_id": endpoint["workflow_id"],
        "result": result,
    }
