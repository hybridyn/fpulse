"""Credentials CRUD API — secure connection storage."""

from __future__ import annotations

import socket
import ssl
import time
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Request

from fpulse.auth.deps import current_workspace_id, require_auth, require_role
from fpulse.credentials.models import Credential, CredentialCreate, CredentialUpdate


def _safe_workspace_id(request: Request) -> str:
    """Wrap current_workspace_id so dep failures surface as readable
    HTTP errors — same pattern as api/projects.py, api/workflows.py,
    api/connections.py."""
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc

# Credentials hold passwords and API keys. Reads return masked values, but
# writes (create/update/delete) and connection-tests are gated to lead+ roles
# so a viewer or guest can't probe internal services or rewrite secrets.
router = APIRouter(prefix="/api/credentials", tags=["credentials"])

# Anyone authenticated can list/read masked secrets
_read = Depends(require_auth)
# Only lead/admin can mutate or run connection tests
_write = Depends(require_role("super_admin", "admin"))


def get_store():
    from fpulse.main import app_state
    return app_state["credential_store"]


def _derive_username(cred_dict: dict) -> str | None:
    """Pull a display-friendly username out of the masked config so the
    credentials list can show it without the user drilling into the
    detail view. Tries common keys in order."""
    cfg = cred_dict.get("config") or {}
    if not isinstance(cfg, dict):
        return None
    for key in ("username", "user", "user_id", "client_id", "account", "access_key_id", "api_key"):
        v = cfg.get(key)
        if v and not str(v).startswith("***"):
            return str(v)[:64]
    return None


def _enrich_list_item(cred_dict: dict, current_user) -> dict:
    """Augment a masked credential dict with list-view metadata:
    username (from config) + created-by display name (if not already
    set). Idempotent — existing values pass through."""
    if cred_dict.get("username") is None:
        cred_dict["username"] = _derive_username(cred_dict)
    if cred_dict.get("created_by") is None and current_user is not None:
        cred_dict["created_by"] = getattr(current_user, "email", None) or getattr(current_user, "name", None)
    return cred_dict


# ── Z25 (2026-05-23) — Credential → Connections lineage ──────────────────


@router.get("/usage")
async def get_credentials_usage(
    _user = _read,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Bulk lineage map: which saved connections reference each credential.

    Returns ``{credential_id: [{connection_id, name, type}, ...]}`` —
    paired with the connections-usage endpoint so the user can answer
    "if I delete this credential, what breaks?" in two hops:
      credential → connections (this endpoint)
      connections → pipelines (connections/usage)

    Cached for 30s.
    """
    from fpulse.datastore.usage import compute_credential_usage_cached
    return compute_credential_usage_cached(workspace_id)


# 2026-05-30 (P7): trailing-slash alias.
@router.get("", include_in_schema=False)
@router.get("/")
async def list_credentials(
    request: Request,
    _user = _read,
    type: str | None = None,
    project_id: str | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List credentials scoped to the caller's current workspace (secrets masked).

    Enriches each row with `username` (derived from config) so the UI
    can render a tabular list without a per-row detail fetch.
    """
    store = get_store()
    rows = store.list_all(
        cred_type=type,
        project_id=project_id,
        workspace_id=workspace_id,
    )
    # `rows` may be list[dict] (legacy) or list[Credential] (newer store).
    out = []
    current_user = getattr(request.state, "user", None)
    for row in rows or []:
        d = row if isinstance(row, dict) else row.model_dump(mode="json")
        out.append(_enrich_list_item(d, current_user))
    return out


@router.post("", include_in_schema=False)
@router.post("/")
async def create_credential(
    body: CredentialCreate,
    request: Request,
    _user = _write,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create a new credential in the caller's current workspace. Lead/admin only."""
    store = get_store()
    current_user = getattr(request.state, "user", None)
    creator = (
        getattr(current_user, "email", None) or
        getattr(current_user, "name", None)
    ) if current_user else None
    credential = Credential(
        name=body.name,
        type=body.type,
        config=body.config,
        project_id=body.project_id,
        workspace_id=workspace_id,
        environment=body.environment,
        expires_at=body.expires_at,
        description=body.description,
        source=body.source or "local",
        vault_reference=body.vault_reference,
        created_by=creator,
        updated_by=creator,
    )
    created = store.create(credential)
    result = created.model_dump(mode="json")
    result["config"] = store._mask_config(created.config)
    result["username"] = _derive_username(result)
    return result


@router.get("/{credential_id}")
async def get_credential(
    credential_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Get a credential by ID (secrets masked) — 404s across workspace boundary."""
    store = get_store()
    credential = store.get(credential_id, workspace_id=workspace_id)
    if not credential:
        raise HTTPException(404, "Credential not found")
    result = credential.model_dump(mode="json")
    result["config"] = store._mask_config(credential.config)
    return result


@router.put("/{credential_id}")
async def update_credential(
    credential_id: str,
    body: CredentialUpdate,
    _user = _write,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Update a credential. Lead/admin only — refuses to cross workspace boundary."""
    store = get_store()
    updates = body.model_dump(exclude_none=True)
    credential = store.update(credential_id, updates, workspace_id=workspace_id)
    if not credential:
        raise HTTPException(404, "Credential not found")
    result = credential.model_dump(mode="json")
    result["config"] = store._mask_config(credential.config)
    return result


@router.post("/{credential_id}/move")
async def move_credential(
    credential_id: str,
    target_project_id: str | None = None,
    _user = _write,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Reassign a credential to a different project (or to global).

    Empty / null ``target_project_id`` makes the credential global.
    Validates that a non-empty target project exists.
    """
    store = get_store()
    target = (target_project_id or "").strip()
    if target:
        from fpulse.main import app_state
        proj_store = app_state.get("project_store")
        if proj_store is not None and proj_store.get(target) is None:
            raise HTTPException(404, f"Target project '{target}' does not exist")
    updated = store.update(
        credential_id,
        {"project_id": target or None},
        workspace_id=workspace_id,
    )
    if not updated:
        raise HTTPException(404, "Credential not found")
    return {"moved": True, "credential_id": credential_id, "project_id": target or None}


@router.delete("/{credential_id}")
async def delete_credential(
    credential_id: str,
    _user = _write,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Delete a credential. Lead/admin only — scoped to workspace."""
    store = get_store()
    if not store.delete(credential_id, workspace_id=workspace_id):
        raise HTTPException(404, "Credential not found")
    # Z25 (2026-05-23) — best-effort lineage-cache invalidation. The
    # credential-usage map is rebuilt from current connections so a stale
    # entry won't survive a refresh anyway, but clearing it keeps the next
    # /api/credentials/usage call honest without waiting for TTL.
    try:
        from fpulse.datastore.usage import invalidate_credential_usage
        invalidate_credential_usage(workspace_id)
    except Exception:
        pass
    return {"deleted": True}


@router.post("/{credential_id}/test")
async def test_credential(
    credential_id: str,
    _user = _write,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """**DEPRECATED — May 9 2026.** Use ``POST /api/connections/{id}/test``
    instead.

    Credentials are pure secret material; testing connectivity only makes
    sense at the Connection level, which carries the host + port + protocol
    alongside the credential reference. The Connection-level test resolves
    the credential and probes the target as a single deterministic check.

    This endpoint is retained for backwards compatibility with any external
    automation or older clients. It still attempts a TCP / HTTP probe using
    whatever endpoint hint happens to be stored in ``credential.config``,
    but the F-Pulse UI no longer surfaces the affordance: pure-secret
    credential types (``custom``, OAuth tokens, plain API-key bags) had no
    target to probe and would receive a misleading "ok" response.

    Lead/admin only — connection probes against internal hosts could be
    used by a low-trust user to map the network, so we gate the same as
    writes. Scoped to the caller's workspace: a credential in another
    tenant 404s.
    """
    store = get_store()
    credential = store.get(credential_id, workspace_id=workspace_id)
    if not credential:
        raise HTTPException(404, "Credential not found")

    # Use unmasked config for the actual test — also workspace-scoped
    raw_cred = store.get_raw(credential_id, workspace_id=workspace_id)
    if not raw_cred:
        raise HTTPException(404, "Credential not found")

    store.mark_used(credential_id, workspace_id=workspace_id)

    try:
        result = _test_credential_connectivity(raw_cred)
    except Exception as exc:
        result = {"status": "error", "message": f"Credential test error: {exc}"}

    return result


# ── Credential connectivity helpers ──

# Default ports by credential type
_DEFAULT_PORTS: dict[str, int] = {
    "postgresql": 5432,
    "mysql": 3306,
    "mssql": 1433,
    "oracle": 1521,
    "mongodb": 27017,
    "redis": 6379,
    "s3": 443,
    "gcs": 443,
    "azure_blob": 443,
    "kafka": 9092,
    "ftp": 21,
    "snowflake": 443,
    "bigquery": 443,
    "redshift": 5439,
    "databricks": 443,
}

# Types that should be tested via HTTP.
# 2026-05-23 (T4 + U1/U2 + V1/V2): expanded for Oracle / SAP families.
_HTTP_TYPES = {
    "rest_api", "graphql",
    "oracle_api", "oracle_fusion", "oracle_bip",
    "sap_s4hana", "sap_successfactors",
}


def _test_credential_connectivity(credential: Credential) -> dict:
    """Test connectivity for a credential based on its type and config."""
    cred_type = credential.type.lower()
    config = credential.config or {}

    # Custom — can't auto-test
    if cred_type == "custom":
        return {
            "status": "ok",
            "message": "Custom credentials cannot be auto-tested.",
            "latency_ms": 0,
        }

    # SQLite — file-based, no network
    if cred_type == "sqlite":
        return {
            "status": "ok",
            "message": f"SQLite credential '{credential.name}' validated (file-based)",
            "latency_ms": 0,
        }

    # HTTP-based types
    if cred_type in _HTTP_TYPES:
        url = config.get("base_url") or config.get("url") or config.get("endpoint")
        if not url:
            return {"status": "error", "message": "No URL configured in credential config"}
        return _http_test(url, credential.name)

    # S3 — endpoint or default AWS
    if cred_type == "s3":
        endpoint = config.get("endpoint") or config.get("endpoint_url")
        if endpoint:
            host, port = _parse_url_host_port(endpoint, 443)
        else:
            region = config.get("region", "us-east-1")
            host, port = f"s3.{region}.amazonaws.com", 443
        return _tcp_test(host, port, credential.name)

    # GCS
    if cred_type == "gcs":
        endpoint = config.get("endpoint")
        if endpoint:
            host, port = _parse_url_host_port(endpoint, 443)
        else:
            host, port = "storage.googleapis.com", 443
        return _tcp_test(host, port, credential.name)

    # Azure Blob
    if cred_type == "azure_blob":
        account = config.get("account_name") or config.get("account")
        if account:
            host, port = f"{account}.blob.core.windows.net", 443
        else:
            endpoint = config.get("endpoint")
            if endpoint:
                host, port = _parse_url_host_port(endpoint, 443)
            else:
                return {"status": "error", "message": "No account or endpoint in credential config"}
        return _tcp_test(host, port, credential.name)

    # Snowflake
    if cred_type == "snowflake":
        account = config.get("account", "")
        if account:
            host = account if "." in account and "snowflake" in account else f"{account}.snowflakecomputing.com"
        else:
            host = config.get("host")
        if not host:
            return {"status": "error", "message": "No account or host in credential config"}
        return _tcp_test(host, 443, credential.name)

    # BigQuery
    if cred_type == "bigquery":
        return _tcp_test("bigquery.googleapis.com", 443, credential.name)

    # Databricks
    if cred_type == "databricks":
        workspace = config.get("host") or config.get("workspace_url") or config.get("endpoint")
        if not workspace:
            return {"status": "error", "message": "No host/workspace_url in credential config"}
        host, port = _parse_url_host_port(workspace, 443)
        return _tcp_test(host, port, credential.name)

    # Kafka — broker string
    if cred_type == "kafka":
        brokers = config.get("brokers") or config.get("bootstrap_servers") or config.get("host")
        if not brokers:
            return {"status": "error", "message": "No broker address in credential config"}
        first = str(brokers).split(",")[0].strip()
        if ":" in first:
            parts = first.rsplit(":", 1)
            host, port = parts[0], int(parts[1])
        else:
            host, port = first, 9092
        return _tcp_test(host, port, credential.name)

    # Default: extract host/port from config
    host = config.get("host") or config.get("server")
    if not host:
        return {"status": "error", "message": f"No host configured for {cred_type} credential"}
    port = int(config.get("port", _DEFAULT_PORTS.get(cred_type, 0)))
    if port == 0:
        return {"status": "error", "message": f"No port configured for {cred_type} credential"}
    return _tcp_test(host, port, credential.name)


def _tcp_test(host: str, port: int, name: str) -> dict:
    """Test TCP connectivity to host:port with 5s timeout."""
    start = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=5)
        latency_ms = int((time.time() - start) * 1000)
        sock.close()
        return {
            "status": "ok",
            "message": f"TCP connection to {host}:{port} succeeded for '{name}'",
            "latency_ms": max(latency_ms, 1),
        }
    except socket.timeout:
        return {"status": "error", "message": f"Connection timed out reaching {host}:{port} (5s timeout)"}
    except OSError as exc:
        return {"status": "error", "message": f"Connection failed to {host}:{port}: {exc}"}


def _http_test(url: str, name: str) -> dict:
    """Test HTTP/HTTPS connectivity to a URL with 5s timeout."""
    start = time.time()
    try:
        req = urllib.request.Request(url, method="HEAD")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        resp = urllib.request.urlopen(req, timeout=5, context=ctx)
        latency_ms = int((time.time() - start) * 1000)
        return {
            "status": "ok",
            "message": f"HTTP {resp.status} from {url} for '{name}'",
            "latency_ms": max(latency_ms, 1),
        }
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.time() - start) * 1000)
        if exc.code in (401, 403, 405):
            return {
                "status": "ok",
                "message": f"Server reachable (HTTP {exc.code}) at {url} — auth required",
                "latency_ms": max(latency_ms, 1),
            }
        return {"status": "error", "message": f"HTTP error {exc.code} from {url}: {exc.reason}"}
    except urllib.error.URLError as exc:
        return {"status": "error", "message": f"Cannot reach {url}: {exc.reason}"}
    except socket.timeout:
        return {"status": "error", "message": f"HTTP request timed out for {url} (5s timeout)"}
    except Exception as exc:
        return {"status": "error", "message": f"HTTP test failed for {url}: {exc}"}


def _parse_url_host_port(url: str, default_port: int) -> tuple[str, int]:
    """Extract host and port from a URL string."""
    clean = url
    for prefix in ("https://", "http://"):
        if clean.lower().startswith(prefix):
            clean = clean[len(prefix):]
            break
    clean = clean.split("/")[0]
    if ":" in clean:
        parts = clean.rsplit(":", 1)
        try:
            return (parts[0], int(parts[1]))
        except ValueError:
            return (parts[0], default_port)
    return (clean, default_port)
