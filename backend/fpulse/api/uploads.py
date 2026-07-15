"""Dataset upload API — upload a local file once, reuse it in File nodes.

Stores under ``<data_dir>/uploads/<workspace_id>/`` so every workspace
has its own namespace and the File node's existing path resolution
(relative to ``data_dir``) keeps working — the node receives a path
like ``uploads/default/sales.csv`` and reads it normally.

Size cap defaults to 100 MB; override with ``FPULSE_UPLOAD_MAX_MB``.
Extensions are whitelisted to the formats the File node can parse.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from fpulse.auth.deps import current_workspace_id

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

_ALLOWED_EXTS = {
    ".csv", ".tsv", ".txt",
    ".json", ".ndjson", ".jsonl",
    ".parquet", ".pq",
    ".xlsx", ".xls",
    ".xml",
}

_MAX_MB = int(os.environ.get("FPULSE_UPLOAD_MAX_MB", "100"))
_MAX_BYTES = _MAX_MB * 1024 * 1024

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _get_data_dir() -> str:
    from fpulse.main import app_state
    return app_state["data_dir"]


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception:
        return "default"


def _sanitize(name: str) -> str:
    base = os.path.basename(name or "").strip()
    base = _SAFE_NAME.sub("_", base)
    return base or "upload.bin"


def _safe_delete_under_ws(rel_path: str, workspace_id: str) -> bool:
    """Delete an upload iff it lives under this workspace's upload dir.

    Returns True if the file was removed, False if it didn't exist or
    failed silently (caller treats this as best-effort cleanup, not a
    hard requirement). Path-traversal protected: any rel_path that
    resolves outside ``<data_dir>/uploads/<workspace_id>/`` is rejected.
    """
    if not rel_path:
        return False
    data_dir = _get_data_dir()
    ws_dir = os.path.normpath(os.path.join(data_dir, "uploads", workspace_id))
    abs_path = os.path.normpath(os.path.join(data_dir, rel_path))
    if not abs_path.startswith(ws_dir + os.sep):
        return False
    if not os.path.isfile(abs_path):
        return False
    try:
        os.remove(abs_path)
        return True
    except OSError:
        return False


@router.post("/file")
async def upload_file(
    file: UploadFile = File(...),
    workspace_id: str = Depends(_safe_workspace_id),
    replaces: str | None = None,
):
    """Upload a local dataset; returns the relative path the File node should use.

    When ``replaces`` is provided (the previous ``file_path`` from the
    same File node), the old file is deleted *after* the new upload
    succeeds — so a failed upload never loses the previous one. This
    keeps ``data/uploads/<ws>/`` from accumulating orphans when a user
    swaps a node's source file.
    """
    original = _sanitize(file.filename or "upload.bin")
    ext = os.path.splitext(original)[1].lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(
            400,
            f"Unsupported file extension '{ext}'. Allowed: {sorted(_ALLOWED_EXTS)}",
        )

    data_dir = _get_data_dir()
    ws_dir = os.path.join(data_dir, "uploads", workspace_id)
    os.makedirs(ws_dir, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stem, _ = os.path.splitext(original)
    stored_name = f"{stem}-{stamp}{ext}"
    abs_path = os.path.join(ws_dir, stored_name)

    size = 0
    with open(abs_path, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_BYTES:
                out.close()
                try:
                    os.remove(abs_path)
                except OSError:
                    pass
                raise HTTPException(
                    413,
                    f"File exceeds {_MAX_MB} MB limit (set FPULSE_UPLOAD_MAX_MB to raise).",
                )
            out.write(chunk)

    rel_path = os.path.join("uploads", workspace_id, stored_name).replace("\\", "/")

    # Best-effort cleanup of the previous file the node was pointing at.
    # Only runs after the new file is fully and successfully written so a
    # failed upload can never strand the caller without their old data.
    replaced = False
    if replaces and replaces != rel_path:
        replaced = _safe_delete_under_ws(replaces, workspace_id)

    return {
        "status": "ok",
        "file_path": rel_path,
        "original_name": original,
        "size_bytes": size,
        "replaced_previous": replaced,
    }


@router.get("/list")
async def list_uploads(workspace_id: str = Depends(_safe_workspace_id)):
    """List previously uploaded files for this workspace."""
    data_dir = _get_data_dir()
    ws_dir = os.path.join(data_dir, "uploads", workspace_id)
    if not os.path.isdir(ws_dir):
        return {"files": []}

    files = []
    for name in sorted(os.listdir(ws_dir)):
        abs_path = os.path.join(ws_dir, name)
        if not os.path.isfile(abs_path):
            continue
        stat = os.stat(abs_path)
        files.append({
            "name": name,
            "file_path": f"uploads/{workspace_id}/{name}",
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    return {"files": files}


@router.delete("/file")
async def delete_upload(
    file_path: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Delete a previously uploaded file. ``file_path`` is the relative path returned by upload."""
    if _safe_delete_under_ws(file_path, workspace_id):
        from fpulse.audit import audit_action
        audit_action(
            action="upload.file.delete",
            resource_type="file",
            resource_id=file_path,
            workspace_id=workspace_id,
        )
        return {"status": "ok"}
    # Distinguish "not yours" from "doesn't exist" for the caller — this
    # endpoint is explicit (vs the best-effort cleanup inside upload),
    # so the caller deserves a meaningful error.
    data_dir = _get_data_dir()
    ws_dir = os.path.normpath(os.path.join(data_dir, "uploads", workspace_id))
    abs_path = os.path.normpath(os.path.join(data_dir, file_path))
    if not abs_path.startswith(ws_dir + os.sep):
        raise HTTPException(400, "file_path is outside this workspace's upload directory")
    raise HTTPException(404, "File not found")
