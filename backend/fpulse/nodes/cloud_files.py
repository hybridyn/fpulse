"""
SaaS document-storage connectors — SharePoint, OneDrive, Google Drive, Dropbox, Box.

Why each gets its own node (not "one universal connector"):
  Every provider has a different OAuth flow, SDK, path semantics, and rate
  limit story. We share the *download → temp file → delegate to FileSourceNode*
  pipeline so format detection (CSV/JSON/Parquet/Excel/XML) is identical
  across all providers — but the auth and item-resolution code stays per-provider.

Auth Model:
  Each node references a connection_id from the connection store. The
  connection holds the provider's auth payload — for example:
    sharepoint:  tenant_id, client_id, client_secret, site_id, drive_id
    onedrive:    tenant_id, client_id, client_secret  (or refresh_token)
    gdrive:      service_account_json  OR  oauth refresh_token
    dropbox:     access_token  (or refresh_token + app key/secret)
    box:         access_token  (or jwt config)

  The middleware-style auth fetch is unified through `_get_token()` which
  exchanges credentials for a short-lived bearer token, cached in-process.

Operations:
  Source: list a folder OR fetch a specific file → download → read with FileSourceNode
  Sink:   write upstream relation to temp file → upload via provider API

Limits / Notes:
  • These connectors use stdlib `urllib` so they ship with no extra deps.
  • For very large transfers consider chunked uploads (TODO: SharePoint Large File Upload session).
  • Rate limits are surfaced as RuntimeErrors with the provider's response code.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for type annotations on execute() returns
# and helper signatures.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register


# ── Shared connection helper ──

def _get_connection(connection_id: str) -> dict | None:
    """Pull a connection's merged config (config + credential) from app_state."""
    if not connection_id:
        return None
    try:
        from fpulse.main import app_state
        conn_store = app_state.get("connection_store")
        cred_store = app_state.get("credential_store")
        if not conn_store:
            return None
        connection = conn_store.get(connection_id)
        if not connection:
            return None
        config = dict(connection.config or {})
        if connection.credential_id and cred_store:
            cred = cred_store.get_raw(connection.credential_id)
            if cred and cred.config:
                config.update(cred.config)
        return config
    except Exception:
        return None


def _http_request(method: str, url: str, *, headers: dict | None = None,
                  body: bytes | None = None, timeout: int = 60) -> tuple[int, bytes, dict]:
    """Stdlib HTTP wrapper that returns (status, body, headers) and never raises on HTTPError."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b"", dict(e.headers or {})


def _delegate_to_file_source(ctx: ExecutionContext, local_path: str,
                              format_override: str = "auto") -> duckdb.DuckDBPyRelation:
    """Hand off a downloaded file to the universal File Source so format
    detection lives in exactly one place."""
    from fpulse.nodes.file_node import FileSourceNode
    node = FileSourceNode({"file_path": local_path, "format": format_override})
    return node.execute(ctx)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Microsoft Graph base — shared by SharePoint and OneDrive
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _GraphBase:
    """Shared Microsoft Graph helpers."""

    _token_cache: dict[str, tuple[str, float]] = {}

    @classmethod
    def _get_graph_token(cls, config: dict) -> str:
        """Client-credentials flow → bearer token, cached for the lifetime of its expiry."""
        tenant = config.get("tenant_id") or "common"
        client_id = config.get("client_id") or ""
        client_secret = config.get("client_secret") or ""
        if not client_id or not client_secret:
            raise ValueError("Microsoft Graph: client_id and client_secret required on the connection")

        cache_key = f"{tenant}:{client_id}"
        cached = cls._token_cache.get(cache_key)
        if cached and cached[1] > time.time() + 30:
            return cached[0]

        url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        body = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }).encode()
        status, payload, _ = _http_request("POST", url, body=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
        })
        if status >= 400:
            raise RuntimeError(f"Graph token error {status}: {payload[:300].decode('utf-8', 'ignore')}")
        data = json.loads(payload)
        token = data["access_token"]
        cls._token_cache[cache_key] = (token, time.time() + int(data.get("expires_in", 3600)))
        return token

    @staticmethod
    def _graph_get(path: str, token: str) -> bytes:
        url = f"https://graph.microsoft.com/v1.0{path}"
        status, payload, _ = _http_request("GET", url, headers={
            "Authorization": f"Bearer {token}",
        })
        if status >= 400:
            raise RuntimeError(f"Graph GET {path} → {status}: {payload[:300].decode('utf-8', 'ignore')}")
        return payload

    @staticmethod
    def _graph_put(path: str, token: str, body: bytes, content_type: str = "application/octet-stream") -> bytes:
        url = f"https://graph.microsoft.com/v1.0{path}"
        status, payload, _ = _http_request("PUT", url, body=body, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
        })
        if status >= 400:
            raise RuntimeError(f"Graph PUT {path} → {status}: {payload[:300].decode('utf-8', 'ignore')}")
        return payload


def _download_to_temp(content: bytes, suggested_name: str) -> str:
    """Write API response bytes to a temp file preserving the source extension."""
    suffix = os.path.splitext(suggested_name)[1] or ".bin"
    fd, tmp_path = tempfile.mkstemp(prefix="fpulse_dl_", suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return tmp_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SharePoint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@register(StepType.SHAREPOINT_SOURCE)
class SharePointSourceNode(_GraphBase, BaseNode):
    """Read a file from a SharePoint document library via Microsoft Graph."""
    display_name = "SharePoint"
    category = "source"
    description = "Read a file from a SharePoint document library"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        config = _get_connection(self.params.get("connection_id", "")) or {}
        site_id = self.params.get("site_id") or config.get("site_id") or ""
        drive_id = self.params.get("drive_id") or config.get("drive_id") or ""
        item_path = self.params.get("item_path", "").lstrip("/")
        if not site_id:
            raise ValueError("SharePoint: site_id required (on node or connection)")
        if not item_path:
            raise ValueError("SharePoint: item_path required (e.g. 'Reports/2026/sales.csv')")

        token = self._get_graph_token(config)
        # Resolve drive (default drive of the site if not specified)
        if not drive_id:
            drive_id = self._resolve_default_drive(site_id, token)

        # Download by drive item path
        encoded = urllib.parse.quote(item_path)
        content = self._graph_get(
            f"/drives/{drive_id}/root:/{encoded}:/content", token
        )
        tmp_path = _download_to_temp(content, item_path)
        try:
            return _delegate_to_file_source(ctx, tmp_path, self.params.get("format", "auto"))
        finally:
            try: os.unlink(tmp_path)
            except OSError: pass

    def _resolve_default_drive(self, site_id: str, token: str) -> str:
        payload = self._graph_get(f"/sites/{site_id}/drive", token)
        return json.loads(payload)["id"]

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"connection_id": "", "site_id": "", "drive_id": "", "item_path": "", "format": "auto"}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "SharePoint Connection",
             "connection_type": "sharepoint", "required": True,
             "description": "Tenant + client_id + client_secret. Manage in Connections page."},
            {"name": "site_id", "type": "text", "label": "Site ID",
             "placeholder": "contoso.sharepoint.com,abc123,def456",
             "description": "Graph site ID. Override the connection default if needed."},
            {"name": "drive_id", "type": "text", "label": "Drive ID (optional)",
             "description": "Leave blank to use the site's default Documents library."},
            {"name": "item_path", "type": "text", "label": "Item Path", "required": True,
             "placeholder": "Reports/2026/sales.csv"},
            {"name": "format", "type": "select", "label": "Format Override",
             "options": ["auto", "csv", "json", "parquet", "excel", "xml", "orc", "avro"], "default": "auto"},
            # X3 (2026-05-30) — sync_mode marker. SharePoint Graph
            # supports `lastModifiedDateTime gt {cursor}` via $filter
            # on `/drive/root/search(q='')`. Operator embeds the cursor
            # in item_path or via the Graph API extras; auto-substitution
            # is roadmap.
            *__import__("fpulse.nodes._sync_mode_decl",
                        fromlist=["sync_mode_marker_entries"]).sync_mode_marker_entries(
                "SharePoint Drive listings filter via Graph "
                "`$filter=lastModifiedDateTime gt {cursor}`. Embed "
                "{cursor} in your item_path glob or use a downstream "
                "Filter node on the file metadata. Auto-substitution "
                "lands per release.",
            ),
        ]


@register(StepType.SHAREPOINT_SINK)
class SharePointSinkNode(_GraphBase, BaseNode):
    """Upload upstream data to a SharePoint document library."""
    display_name = "SharePoint"
    category = "destination"
    description = "Upload data to a SharePoint site library"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        upstream = self.params.get("_input_step_ids") or []
        if not upstream:
            raise ValueError("SharePoint Sink: needs an upstream node")
        rel = ctx.get_input(upstream[0])
        if rel is None:
            raise ValueError(f"SharePoint Sink: upstream '{upstream[0]}' has no result")

        config = _get_connection(self.params.get("connection_id", "")) or {}
        site_id = self.params.get("site_id") or config.get("site_id")
        drive_id = self.params.get("drive_id") or config.get("drive_id") or ""
        item_path = self.params.get("item_path", "").lstrip("/")
        if not site_id or not item_path:
            raise ValueError("SharePoint Sink: site_id and item_path required")

        # Render the relation to a temp file via FileSinkNode (so format = extension)
        from fpulse.nodes.file_node import FileSinkNode
        suffix = os.path.splitext(item_path)[1] or ".csv"
        fd, tmp_path = tempfile.mkstemp(prefix="fpulse_up_", suffix=suffix)
        os.close(fd)
        sink = FileSinkNode({"file_path": tmp_path, "format": "auto",
                             "_input_step_ids": upstream})
        sink.execute(ctx)

        token = self._get_graph_token(config)
        if not drive_id:
            drive_id = json.loads(self._graph_get(f"/sites/{site_id}/drive", token))["id"]

        with open(tmp_path, "rb") as f:
            body = f.read()
        try: os.unlink(tmp_path)
        except OSError: pass

        encoded = urllib.parse.quote(item_path)
        self._graph_put(f"/drives/{drive_id}/root:/{encoded}:/content", token, body)
        return rel

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"connection_id": "", "site_id": "", "drive_id": "", "item_path": ""}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "SharePoint Connection",
             "connection_type": "sharepoint", "required": True},
            {"name": "site_id", "type": "text", "label": "Site ID"},
            {"name": "drive_id", "type": "text", "label": "Drive ID (optional)"},
            {"name": "item_path", "type": "text", "label": "Destination Path", "required": True,
             "placeholder": "Reports/2026/output.parquet"},
        ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OneDrive (same Graph API, /me/drive or /users/{id}/drive)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _OneDriveBase(_GraphBase):
    @staticmethod
    def _drive_root(config: dict, override_user: str = "") -> str:
        user = override_user or config.get("user_id") or ""
        return f"/users/{user}/drive" if user else "/me/drive"


@register(StepType.ONEDRIVE_SOURCE)
class OneDriveSourceNode(_OneDriveBase, BaseNode):
    display_name = "OneDrive"
    category = "source"
    description = "Read a file from OneDrive (personal or business)"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        config = _get_connection(self.params.get("connection_id", "")) or {}
        item_path = self.params.get("item_path", "").lstrip("/")
        if not item_path:
            raise ValueError("OneDrive: item_path required")
        token = self._get_graph_token(config)
        drive_root = self._drive_root(config, self.params.get("user_id", ""))
        encoded = urllib.parse.quote(item_path)
        content = self._graph_get(f"{drive_root}/root:/{encoded}:/content", token)
        tmp = _download_to_temp(content, item_path)
        try:
            return _delegate_to_file_source(ctx, tmp, self.params.get("format", "auto"))
        finally:
            try: os.unlink(tmp)
            except OSError: pass

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"connection_id": "", "user_id": "", "item_path": "", "format": "auto"}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "OneDrive Connection",
             "connection_type": "onedrive", "required": True},
            {"name": "user_id", "type": "text", "label": "User ID (optional)",
             "description": "Leave blank for /me/drive (delegated auth)."},
            {"name": "item_path", "type": "text", "label": "Item Path", "required": True,
             "placeholder": "Documents/data.xlsx"},
            {"name": "format", "type": "select", "label": "Format Override",
             "options": ["auto", "csv", "json", "parquet", "excel", "xml", "orc", "avro"], "default": "auto"},
        ]


@register(StepType.ONEDRIVE_SINK)
class OneDriveSinkNode(_OneDriveBase, BaseNode):
    display_name = "OneDrive"
    category = "destination"
    description = "Upload a file to OneDrive"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        upstream = self.params.get("_input_step_ids") or []
        if not upstream:
            raise ValueError("OneDrive Sink: needs an upstream node")
        rel = ctx.get_input(upstream[0])
        if rel is None:
            raise ValueError(f"OneDrive Sink: upstream '{upstream[0]}' has no result")

        config = _get_connection(self.params.get("connection_id", "")) or {}
        item_path = self.params.get("item_path", "").lstrip("/")
        if not item_path:
            raise ValueError("OneDrive Sink: item_path required")

        from fpulse.nodes.file_node import FileSinkNode
        suffix = os.path.splitext(item_path)[1] or ".csv"
        fd, tmp = tempfile.mkstemp(prefix="fpulse_up_", suffix=suffix)
        os.close(fd)
        FileSinkNode({"file_path": tmp, "format": "auto",
                      "_input_step_ids": upstream}).execute(ctx)

        with open(tmp, "rb") as f:
            body = f.read()
        try: os.unlink(tmp)
        except OSError: pass

        token = self._get_graph_token(config)
        drive_root = self._drive_root(config, self.params.get("user_id", ""))
        encoded = urllib.parse.quote(item_path)
        self._graph_put(f"{drive_root}/root:/{encoded}:/content", token, body)
        return rel

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"connection_id": "", "user_id": "", "item_path": ""}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "OneDrive Connection",
             "connection_type": "onedrive", "required": True},
            {"name": "user_id", "type": "text", "label": "User ID (optional)"},
            {"name": "item_path", "type": "text", "label": "Destination Path", "required": True,
             "placeholder": "Documents/output.parquet"},
        ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Google Drive (Drive API v3)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _GDriveBase:
    @staticmethod
    def _get_token(config: dict) -> str:
        """Use OAuth2 refresh token (simpler than full service account JWT signing)."""
        refresh_token = config.get("refresh_token") or ""
        client_id = config.get("client_id") or ""
        client_secret = config.get("client_secret") or ""
        if not refresh_token:
            raise ValueError(
                "Google Drive: refresh_token required on connection "
                "(or use a service account access_token directly via 'access_token' field)"
            )
        # Direct access_token short-circuit (good for tests)
        if config.get("access_token"):
            return config["access_token"]
        body = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        status, payload, _ = _http_request("POST",
            "https://oauth2.googleapis.com/token", body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status >= 400:
            raise RuntimeError(f"GDrive token error {status}: {payload[:300].decode('utf-8', 'ignore')}")
        return json.loads(payload)["access_token"]


@register(StepType.GDRIVE_SOURCE)
class GoogleDriveSourceNode(_GDriveBase, BaseNode):
    display_name = "Google Drive"
    category = "source"
    description = "Read a file from Google Drive by file ID"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        config = _get_connection(self.params.get("connection_id", "")) or {}
        file_id = self.params.get("file_id", "")
        file_name = self.params.get("file_name", "") or "download.bin"
        if not file_id:
            raise ValueError("Google Drive: file_id required")

        token = self._get_token(config)
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        status, payload, _ = _http_request("GET", url,
            headers={"Authorization": f"Bearer {token}"})
        if status >= 400:
            raise RuntimeError(f"GDrive GET {status}: {payload[:300].decode('utf-8', 'ignore')}")

        tmp = _download_to_temp(payload, file_name)
        try:
            return _delegate_to_file_source(ctx, tmp, self.params.get("format", "auto"))
        finally:
            try: os.unlink(tmp)
            except OSError: pass

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"connection_id": "", "file_id": "", "file_name": "", "format": "auto"}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "Google Drive Connection",
             "connection_type": "gdrive", "required": True},
            {"name": "file_id", "type": "text", "label": "File ID", "required": True,
             "description": "From the Google Drive share link or list response."},
            {"name": "file_name", "type": "text", "label": "File Name (for format detection)",
             "placeholder": "report.csv"},
            {"name": "format", "type": "select", "label": "Format Override",
             "options": ["auto", "csv", "json", "parquet", "excel", "xml", "orc", "avro"], "default": "auto"},
        ]


@register(StepType.GDRIVE_SINK)
class GoogleDriveSinkNode(_GDriveBase, BaseNode):
    display_name = "Google Drive"
    category = "destination"
    description = "Upload a file to Google Drive"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        upstream = self.params.get("_input_step_ids") or []
        if not upstream:
            raise ValueError("Google Drive Sink: needs an upstream node")
        rel = ctx.get_input(upstream[0])
        if rel is None:
            raise ValueError(f"Google Drive Sink: upstream '{upstream[0]}' has no result")

        config = _get_connection(self.params.get("connection_id", "")) or {}
        file_name = self.params.get("file_name", "")
        parent_id = self.params.get("parent_folder_id", "")
        if not file_name:
            raise ValueError("Google Drive Sink: file_name required")

        from fpulse.nodes.file_node import FileSinkNode
        suffix = os.path.splitext(file_name)[1] or ".csv"
        fd, tmp = tempfile.mkstemp(prefix="fpulse_up_", suffix=suffix)
        os.close(fd)
        FileSinkNode({"file_path": tmp, "format": "auto",
                      "_input_step_ids": upstream}).execute(ctx)
        with open(tmp, "rb") as f:
            body = f.read()
        try: os.unlink(tmp)
        except OSError: pass

        token = self._get_token(config)
        # Multipart upload (metadata + media in one request)
        boundary = "fpulse_boundary_a1b2c3"
        metadata = {"name": file_name}
        if parent_id:
            metadata["parents"] = [parent_id]
        meta_part = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
        ).encode()
        media_part = (
            f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n"
        ).encode() + body + f"\r\n--{boundary}--".encode()
        payload = meta_part + media_part

        url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
        status, resp, _ = _http_request("POST", url, body=payload, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        })
        if status >= 400:
            raise RuntimeError(f"GDrive upload {status}: {resp[:300].decode('utf-8', 'ignore')}")
        return rel

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"connection_id": "", "file_name": "", "parent_folder_id": ""}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "Google Drive Connection",
             "connection_type": "gdrive", "required": True},
            {"name": "file_name", "type": "text", "label": "File Name", "required": True,
             "placeholder": "output.parquet"},
            {"name": "parent_folder_id", "type": "text", "label": "Parent Folder ID (optional)"},
        ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dropbox (API v2 — content endpoint uses Dropbox-API-Arg header)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _DropboxBase:
    @staticmethod
    def _get_token(config: dict) -> str:
        if config.get("access_token"):
            return config["access_token"]
        # Refresh-token flow
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": config.get("refresh_token", ""),
            "client_id": config.get("client_id", ""),
            "client_secret": config.get("client_secret", ""),
        }).encode()
        status, payload, _ = _http_request("POST",
            "https://api.dropboxapi.com/oauth2/token", body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status >= 400:
            raise RuntimeError(f"Dropbox token error {status}")
        return json.loads(payload)["access_token"]


@register(StepType.DROPBOX_SOURCE)
class DropboxSourceNode(_DropboxBase, BaseNode):
    display_name = "Dropbox"
    category = "source"
    description = "Read a file from Dropbox"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        config = _get_connection(self.params.get("connection_id", "")) or {}
        path = self.params.get("path", "")
        if not path.startswith("/"):
            path = "/" + path
        if not path or path == "/":
            raise ValueError("Dropbox: path required (e.g. '/Reports/data.csv')")

        token = self._get_token(config)
        status, payload, _ = _http_request("POST",
            "https://content.dropboxapi.com/2/files/download",
            headers={
                "Authorization": f"Bearer {token}",
                "Dropbox-API-Arg": json.dumps({"path": path}),
            },
        )
        if status >= 400:
            raise RuntimeError(f"Dropbox download {status}: {payload[:300].decode('utf-8', 'ignore')}")
        tmp = _download_to_temp(payload, path)
        try:
            return _delegate_to_file_source(ctx, tmp, self.params.get("format", "auto"))
        finally:
            try: os.unlink(tmp)
            except OSError: pass

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"connection_id": "", "path": "", "format": "auto"}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "Dropbox Connection",
             "connection_type": "dropbox", "required": True},
            {"name": "path", "type": "text", "label": "Path", "required": True,
             "placeholder": "/Reports/data.csv"},
            {"name": "format", "type": "select", "label": "Format Override",
             "options": ["auto", "csv", "json", "parquet", "excel", "xml", "orc", "avro"], "default": "auto"},
        ]


@register(StepType.DROPBOX_SINK)
class DropboxSinkNode(_DropboxBase, BaseNode):
    display_name = "Dropbox"
    category = "destination"
    description = "Upload a file to Dropbox"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        upstream = self.params.get("_input_step_ids") or []
        if not upstream:
            raise ValueError("Dropbox Sink: needs an upstream node")
        rel = ctx.get_input(upstream[0])
        if rel is None:
            raise ValueError(f"Dropbox Sink: upstream '{upstream[0]}' has no result")

        config = _get_connection(self.params.get("connection_id", "")) or {}
        path = self.params.get("path", "")
        if not path.startswith("/"):
            path = "/" + path
        if not path or path == "/":
            raise ValueError("Dropbox Sink: path required")

        from fpulse.nodes.file_node import FileSinkNode
        suffix = os.path.splitext(path)[1] or ".csv"
        fd, tmp = tempfile.mkstemp(prefix="fpulse_up_", suffix=suffix)
        os.close(fd)
        FileSinkNode({"file_path": tmp, "format": "auto",
                      "_input_step_ids": upstream}).execute(ctx)
        with open(tmp, "rb") as f:
            body = f.read()
        try: os.unlink(tmp)
        except OSError: pass

        token = self._get_token(config)
        status, payload, _ = _http_request("POST",
            "https://content.dropboxapi.com/2/files/upload",
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Dropbox-API-Arg": json.dumps({"path": path, "mode": "overwrite", "autorename": False}),
                "Content-Type": "application/octet-stream",
            },
        )
        if status >= 400:
            raise RuntimeError(f"Dropbox upload {status}: {payload[:300].decode('utf-8', 'ignore')}")
        return rel

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"connection_id": "", "path": ""}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "Dropbox Connection",
             "connection_type": "dropbox", "required": True},
            {"name": "path", "type": "text", "label": "Destination Path", "required": True,
             "placeholder": "/Reports/output.parquet"},
        ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Box (API v2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _BoxBase:
    @staticmethod
    def _get_token(config: dict) -> str:
        if config.get("access_token"):
            return config["access_token"]
        # Refresh-token flow
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": config.get("refresh_token", ""),
            "client_id": config.get("client_id", ""),
            "client_secret": config.get("client_secret", ""),
        }).encode()
        status, payload, _ = _http_request("POST",
            "https://api.box.com/oauth2/token", body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status >= 400:
            raise RuntimeError(f"Box token error {status}")
        return json.loads(payload)["access_token"]


@register(StepType.BOX_SOURCE)
class BoxSourceNode(_BoxBase, BaseNode):
    display_name = "Box"
    category = "source"
    description = "Read a file from Box by file ID"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        config = _get_connection(self.params.get("connection_id", "")) or {}
        file_id = self.params.get("file_id", "")
        file_name = self.params.get("file_name", "") or "download.bin"
        if not file_id:
            raise ValueError("Box: file_id required")

        token = self._get_token(config)
        # Box returns 302 → presigned download URL. urllib follows redirects automatically.
        status, payload, _ = _http_request("GET",
            f"https://api.box.com/2.0/files/{file_id}/content",
            headers={"Authorization": f"Bearer {token}"},
        )
        if status >= 400:
            raise RuntimeError(f"Box download {status}: {payload[:300].decode('utf-8', 'ignore')}")
        tmp = _download_to_temp(payload, file_name)
        try:
            return _delegate_to_file_source(ctx, tmp, self.params.get("format", "auto"))
        finally:
            try: os.unlink(tmp)
            except OSError: pass

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"connection_id": "", "file_id": "", "file_name": "", "format": "auto"}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "Box Connection",
             "connection_type": "box", "required": True},
            {"name": "file_id", "type": "text", "label": "File ID", "required": True},
            {"name": "file_name", "type": "text", "label": "File Name (for format detection)",
             "placeholder": "report.csv"},
            {"name": "format", "type": "select", "label": "Format Override",
             "options": ["auto", "csv", "json", "parquet", "excel", "xml", "orc", "avro"], "default": "auto"},
        ]


@register(StepType.BOX_SINK)
class BoxSinkNode(_BoxBase, BaseNode):
    display_name = "Box"
    category = "destination"
    description = "Upload a file to Box"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        upstream = self.params.get("_input_step_ids") or []
        if not upstream:
            raise ValueError("Box Sink: needs an upstream node")
        rel = ctx.get_input(upstream[0])
        if rel is None:
            raise ValueError(f"Box Sink: upstream '{upstream[0]}' has no result")

        config = _get_connection(self.params.get("connection_id", "")) or {}
        file_name = self.params.get("file_name", "")
        parent_id = self.params.get("parent_folder_id", "0")  # 0 = root
        if not file_name:
            raise ValueError("Box Sink: file_name required")

        from fpulse.nodes.file_node import FileSinkNode
        suffix = os.path.splitext(file_name)[1] or ".csv"
        fd, tmp = tempfile.mkstemp(prefix="fpulse_up_", suffix=suffix)
        os.close(fd)
        FileSinkNode({"file_path": tmp, "format": "auto",
                      "_input_step_ids": upstream}).execute(ctx)
        with open(tmp, "rb") as f:
            body = f.read()
        try: os.unlink(tmp)
        except OSError: pass

        token = self._get_token(config)
        # Box multipart upload to /files/content
        boundary = "fpulse_box_boundary"
        attrs = json.dumps({"name": file_name, "parent": {"id": parent_id}})
        payload = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="attributes"\r\n\r\n'
            f"{attrs}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + body + f"\r\n--{boundary}--".encode()

        status, resp, _ = _http_request("POST",
            "https://upload.box.com/api/2.0/files/content",
            body=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        if status >= 400:
            raise RuntimeError(f"Box upload {status}: {resp[:300].decode('utf-8', 'ignore')}")
        return rel

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"connection_id": "", "file_name": "", "parent_folder_id": "0"}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "Box Connection",
             "connection_type": "box", "required": True},
            {"name": "file_name", "type": "text", "label": "File Name", "required": True,
             "placeholder": "output.parquet"},
            {"name": "parent_folder_id", "type": "text", "label": "Parent Folder ID",
             "default": "0", "description": "0 = root folder"},
        ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Microsoft Graph (generic source) — 2026-05-22
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# First-class Graph source that any /users, /groups, /sites,
# /drives, /teams, /planner, /me/messages etc. flow can reuse. The
# SharePoint / OneDrive nodes above stay for their file-flavored
# UX (download a specific drive item and parse it as CSV/JSON);
# this node is the general-purpose JSON-rows reader for arbitrary
# Graph resource collections.
#
# Reuses _GraphBase for token acquisition + caching so the
# client-credentials flow is identical across every Graph-backed
# node in F-Pulse.

@register(StepType.MS_GRAPH_SOURCE)
class MicrosoftGraphSourceNode(_GraphBase, BaseNode):
    """Read JSON rows from any Microsoft Graph resource collection.

    Configuration:
      * connection_id — saved ``microsoft_graph`` connection (tenant
        + client_id + secret + scope + base_url).
      * resource_path — e.g. ``/users``, ``/groups``, ``/sites``,
        ``/teams``, ``/me/messages``. Leading slash is added if
        missing.
      * query_params — OData query string fragments. Free-form
        string; appended to the URL after ``?``. Example:
        ``$top=100&$select=id,displayName,mail``.
      * data_path — JSON key the rows live under. Default ``"value"``
        (the canonical Graph collection wrapper). Set to ``""`` to
        treat the response as a single object → one row.
      * paginate — follow ``@odata.nextLink`` until exhausted. On by
        default; turn off for one-page samples.
      * max_pages — safety ceiling on pagination loops. Default 100;
        a typical tenant /users with $top=100 will be ~5–10 pages.

    Returns: DuckDB relation with one row per JSON object in the
    collection. Schema is inferred from the first row (pandas-style)
    so nested dict/array columns are preserved as DuckDB struct /
    list types.
    """

    display_name = "Microsoft Graph"
    category = "source"
    description = "Read from any Microsoft Graph resource — users, groups, sites, drives, teams, planner, mail, calendars, or a custom endpoint."

    _MAX_PAGES_FLOOR = 1
    _MAX_PAGES_CEIL = 10_000
    _DEFAULT_MAX_PAGES = 100

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        config = _get_connection(self.params.get("connection_id", "")) or {}
        if not config:
            raise ValueError(
                "Microsoft Graph: pick a saved connection. The connection holds "
                "tenant_id + client_id + client_secret + base_url."
            )

        # Resource path normalisation. Accepts "/users", "users",
        # or a fully-qualified Graph URL (in which case it's used
        # verbatim). The latter is what /custom-endpoint flows
        # supply when they need beta or sovereign-cloud URLs.
        raw_path = (self.params.get("resource_path") or "").strip()
        if not raw_path:
            raise ValueError("Microsoft Graph: resource_path is required (e.g. '/users').")
        base_url = (config.get("base_url") or "https://graph.microsoft.com/v1.0").rstrip("/")
        if raw_path.startswith("http://") or raw_path.startswith("https://"):
            url = raw_path
        else:
            if not raw_path.startswith("/"):
                raw_path = "/" + raw_path
            url = f"{base_url}{raw_path}"
        # Append query params (free-form OData string).
        query = (self.params.get("query_params") or "").strip().lstrip("?")
        if query:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{query}"

        data_path = self.params.get("data_path", "value")
        paginate = bool(self.params.get("paginate", True))
        max_pages = int(self.params.get("max_pages", self._DEFAULT_MAX_PAGES) or self._DEFAULT_MAX_PAGES)
        max_pages = max(self._MAX_PAGES_FLOOR, min(max_pages, self._MAX_PAGES_CEIL))

        token = self._get_graph_token(config)

        rows: list[dict] = []
        next_url: str | None = url
        page_count = 0
        while next_url and page_count < max_pages:
            page_count += 1
            status, payload, _ = _http_request("GET", next_url, headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            })
            if status >= 400:
                raise RuntimeError(
                    f"Microsoft Graph: {next_url} → HTTP {status}: "
                    f"{payload[:400].decode('utf-8', 'ignore')}"
                )
            body = json.loads(payload)
            # Extract rows for this page.
            if not data_path:
                # Single-object resource (e.g. /me, /organization).
                if isinstance(body, dict):
                    rows.append(body)
                next_url = None
                break
            chunk = body.get(data_path)
            if isinstance(chunk, list):
                # Filter out non-dict entries defensively — Graph
                # collections are always [obj] but a custom endpoint
                # could return strings/ints.
                rows.extend([r for r in chunk if isinstance(r, dict)])
            elif isinstance(chunk, dict):
                # Some endpoints return a singleton under value
                # (e.g. /me/drive). Treat as one row.
                rows.append(chunk)
            # Follow Graph's @odata.nextLink for pagination.
            next_url = body.get("@odata.nextLink") if paginate else None

        if not rows:
            # Empty result — return a zero-row relation with a
            # placeholder column so downstream `select *` doesn't
            # crash. Same shape ApiSourceNode uses for empty
            # responses.
            return ctx.conn.sql("SELECT NULL AS empty WHERE false")

        # Use pandas → DuckDB ingestion (audit-fix from the
        # ApiSourceNode rewrite). Native column naming + nested-type
        # support without the hand-built VALUES SQL footgun.
        try:
            import pandas as _pd  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Microsoft Graph source requires pandas. "
                "Install with: pip install pandas"
            ) from exc

        # Union of keys across all rows so columns missing from
        # individual rows still appear as nullable in the relation.
        all_keys: list[str] = []
        seen: set[str] = set()
        for r in rows:
            for k in r:
                if k not in seen:
                    all_keys.append(k)
                    seen.add(k)
        df = _pd.DataFrame([{k: r.get(k) for k in all_keys} for r in rows])
        return ctx.conn.from_df(df)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "connection_id": "",
            "resource_path": "/users",
            "query_params": "",
            "data_path": "value",
            "paginate": True,
            "max_pages": 100,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "Microsoft Graph Connection",
             "connection_type": "microsoft_graph", "required": True,
             "description": "Tenant + client_id + client_secret. Manage in the Connections page."},
            {"name": "resource_path", "type": "select", "label": "Resource",
             "required": True,
             "options": [
                 "/users", "/groups", "/sites", "/drives", "/teams",
                 "/me", "/organization",
                 "/planner/plans", "/planner/tasks",
                 "/me/messages", "/me/calendars", "/me/events",
                 "/directoryRoles", "/applications", "/servicePrincipals",
             ],
             "freeform": True,
             "default": "/users",
             "description": "Pick a preset or type a custom Graph endpoint (e.g. /sites/{id}/lists, /teams/{id}/channels)."},
            {"name": "query_params", "type": "text", "label": "OData Query Params",
             "placeholder": "$top=100&$select=id,displayName,mail",
             "description": "Free-form OData query string. $top / $filter / $select / $orderby / $expand / $count all supported by Graph."},
            {"name": "data_path", "type": "text", "label": "Data Path",
             "default": "value",
             "description": "JSON key holding the row collection. Default 'value' (Graph's collection wrapper). Set blank for single-object endpoints like /me or /organization."},
            {"name": "paginate", "type": "boolean", "label": "Follow @odata.nextLink",
             "default": True,
             "description": "Walk every page until exhausted. Disable for quick samples."},
            {"name": "max_pages", "type": "number", "label": "Max Pages",
             "default": 100,
             "description": "Safety cap on pagination loops. Increase for big tenants."},
        ]
