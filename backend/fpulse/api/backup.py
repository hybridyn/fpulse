"""Backup and restore API — data portability for F-Pulse.

Local backup (SQLite file copy) and optional cloud destinations
(S3, Azure Blob, GCS, MinIO).

Tenant model
------------
A backup is a snapshot of the *entire* SQLite database — it legitimately
contains data for every workspace on the install. That makes it an
**instance-level** operation, not a workspace-level one. Every endpoint
in this router therefore requires ``require_admin`` (super_admin or
admin), not a workspace membership check, so that:

  - A workspace admin (non-instance-admin) cannot exfiltrate rows from
    tenants they don't belong to via ``/export`` or ``/list``.
  - A restore cannot overwrite another tenant's state based only on
    workspace-level credentials.

If we ever add per-workspace export (a scoped JSON dump), it will live
on a separate route like ``/api/backup/workspace/{id}/export`` and use
``_safe_workspace_id`` — not this router.
"""

from __future__ import annotations

import os
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Any

import sqlite3

from fpulse.auth.deps import require_admin

# All backup write/destructive operations are admin-only.
# Listing/exporting still requires no special role since the data is already
# accessible to authenticated users via the regular CRUD APIs.
router = APIRouter(prefix="/api/backup", tags=["backup"])


class BackupResponse(BaseModel):
    status: str
    message: str
    backup_key: str = ""
    size_bytes: int = 0


class RestoreRequest(BaseModel):
    backup_key: str


class StorageConfigRequest(BaseModel):
    provider: str = "local"  # local | s3 | azure_blob | gcs | minio
    bucket: str = ""
    container: str = ""
    prefix: str = "fpulse-backups/"
    region: str = "us-east-1"
    endpoint_url: str = ""
    access_key: str = ""
    secret_key: str = ""
    connection_string: str = ""
    account_name: str = ""
    account_key: str = ""
    credentials_path: str = ""
    backup_dir: str = ""


def _get_db():
    from fpulse.main import app_state
    return app_state["db"]


def _get_provider(config: dict | None = None):
    from fpulse.storage.providers import create_provider
    if config:
        return create_provider(config)
    # Default: local provider
    from fpulse.main import app_state
    data_dir = app_state.get("data_dir", "data")
    return create_provider({"provider": "local", "backup_dir": os.path.join(data_dir, "backups")})


# ── Backup endpoints ──

@router.post("/create", response_model=BackupResponse)
async def create_backup(
    config: StorageConfigRequest | None = None,
    _user = Depends(require_admin),
):
    """Create a backup of the F-Pulse database. Admin only."""
    db = _get_db()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_key = f"fpulse_backup_{timestamp}.db"

    provider_config = config.model_dump() if config else {"provider": "local"}
    provider = _get_provider(provider_config)

    # Create a temp backup file
    from fpulse.main import app_state
    data_dir = app_state.get("data_dir", "data")
    temp_path = os.path.join(data_dir, f".backup_temp_{timestamp}.db")

    try:
        db.backup_to(temp_path)
        size = os.path.getsize(temp_path)

        # Upload to provider
        success = provider.upload(temp_path, backup_key)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to upload backup")

        return BackupResponse(
            status="ok",
            message=f"Backup created: {backup_key}",
            backup_key=backup_key,
            size_bytes=size,
        )
    finally:
        # Clean up temp file
        if os.path.isfile(temp_path):
            os.remove(temp_path)


@router.get("/list")
async def list_backups(
    provider: str = "local",
    _user = Depends(require_admin),
):
    """List available backups. Admin only — a backup name/timestamp
    is itself a piece of leaked activity metadata for other tenants,
    so we don't expose the list to non-admins."""
    storage = _get_provider({"provider": provider})
    return storage.list_backups()


@router.post("/restore")
async def restore_backup(
    request: RestoreRequest,
    config: StorageConfigRequest | None = None,
    _user = Depends(require_admin),
):
    """Restore from a backup. WARNING: This replaces all current data. Admin only."""
    db = _get_db()

    provider_config = config.model_dump() if config else {"provider": "local"}
    provider = _get_provider(provider_config)

    from fpulse.main import app_state
    data_dir = app_state.get("data_dir", "data")
    temp_path = os.path.join(data_dir, f".restore_temp.db")

    try:
        success = provider.download(request.backup_key, temp_path)
        if not success:
            raise HTTPException(status_code=404, detail=f"Backup not found: {request.backup_key}")

        # Import from the backup
        restore_conn = sqlite3.connect(temp_path)
        restore_conn.row_factory = sqlite3.Row
        try:
            # Read all tables from backup
            tables = [row[0] for row in restore_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '__%'"
            ).fetchall()]

            for table in tables:
                cursor = restore_conn.execute(f"SELECT * FROM {table}")
                cols = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                if not rows or not cols:
                    continue

                # Clear existing data in target table
                try:
                    db.execute(f"DELETE FROM {table}")
                except Exception:
                    pass

                # Insert backup data
                placeholders = ", ".join(["?"] * len(cols))
                col_names = ", ".join(cols)
                for row in rows:
                    row_dict = dict(row)
                    values = [row_dict.get(c) for c in cols]
                    db.execute(
                        f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})",
                        values,
                    )
            db.commit()
        finally:
            restore_conn.close()

        return {
            "status": "ok",
            "message": f"Restored from: {request.backup_key}",
            "tables_restored": len(tables),
        }
    finally:
        if os.path.isfile(temp_path):
            os.remove(temp_path)


@router.delete("/{backup_key}")
async def delete_backup(
    backup_key: str,
    provider: str = "local",
    _user = Depends(require_admin),
):
    """Delete a specific backup. Admin only."""
    storage = _get_provider({"provider": provider})
    success = storage.delete(backup_key)
    if not success:
        raise HTTPException(status_code=404, detail=f"Backup not found: {backup_key}")
    return {"status": "ok", "message": f"Deleted: {backup_key}"}


# ── Export/Import (JSON format for cross-platform portability) ──

@router.get("/export")
async def export_json(_user = Depends(require_admin)):
    """Export entire database as JSON. Admin only — this dump spans
    every workspace on the install, so it's the single most sensitive
    endpoint in the product. Under no circumstances should a
    non-instance-admin be able to call it."""
    db = _get_db()
    return db.export_all()


@router.post("/import")
async def import_json(
    data: dict[str, Any],
    _user = Depends(require_admin),
):
    """Import data from a JSON export. Merges with existing data. Admin only."""
    db = _get_db()
    db.import_all(data)
    tables = data.get("_tables", {})
    return {
        "status": "ok",
        "message": f"Imported {len(tables)} tables",
        "tables": list(tables.keys()),
    }


# ── Storage provider test ──

@router.post("/test-provider")
async def test_storage_provider(
    config: StorageConfigRequest,
    _user = Depends(require_admin),
):
    """Test connectivity to a storage provider. Admin only — the
    request body carries provider credentials (access keys, service
    account blobs, etc.), so we don't want a non-admin probing the
    endpoint and getting free credential validation."""
    provider = _get_provider(config.model_dump())
    return provider.test_connection()


# ── Backup schedule & settings ──

class BackupScheduleRequest(BaseModel):
    enabled: bool = False
    frequency: str = "daily"        # hourly | daily | weekly
    daily_time: str = "02:00"       # HH:MM UTC
    weekly_day: int = 0             # 0=Monday
    retention_count: int = 5
    provider: StorageConfigRequest | None = None


@router.get("/settings")
async def get_backup_settings(_user = Depends(require_admin)):
    """Get current backup schedule and provider config. Admin only."""
    from fpulse.storage.backup_scheduler import BackupScheduler
    return BackupScheduler.get_settings()


@router.put("/settings")
async def update_backup_settings(
    body: BackupScheduleRequest,
    _user = Depends(require_admin),
):
    """Update backup schedule and provider config. Admin only."""
    from fpulse.storage.backup_scheduler import BackupScheduler

    settings = BackupScheduler.get_settings()
    settings["enabled"] = body.enabled
    settings["frequency"] = body.frequency
    settings["daily_time"] = body.daily_time
    settings["weekly_day"] = body.weekly_day
    settings["retention_count"] = body.retention_count
    if body.provider:
        settings["provider"] = body.provider.model_dump()
    BackupScheduler.save_settings(settings)
    return {"status": "ok", "settings": settings}


@router.get("/status")
async def get_backup_status(_user = Depends(require_admin)):
    """Get backup health: last backup time, next scheduled, status."""
    from fpulse.main import app_state
    scheduler = app_state.get("backup_scheduler")
    if scheduler:
        return scheduler.get_status()
    # Fallback if scheduler not wired
    from fpulse.storage.backup_scheduler import BackupScheduler
    return BackupScheduler.get_settings()


@router.post("/{backup_key}/preview")
async def preview_backup(
    backup_key: str,
    config: StorageConfigRequest | None = None,
    _user = Depends(require_admin),
):
    """Preview a backup before restoring — shows tables, row counts,
    and size without modifying any data. Admin only."""
    provider_config = config.model_dump() if config else {"provider": "local"}
    provider = _get_provider(provider_config)

    from fpulse.main import app_state
    data_dir = app_state.get("data_dir", "data")
    temp_path = os.path.join(data_dir, ".preview_temp.db")

    try:
        success = provider.download(backup_key, temp_path)
        if not success:
            raise HTTPException(status_code=404, detail=f"Backup not found: {backup_key}")

        conn = sqlite3.connect(temp_path)
        conn.row_factory = sqlite3.Row
        try:
            tables_raw = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '__%'"
            ).fetchall()

            tables = []
            total_rows = 0
            for row in tables_raw:
                name = row[0]
                count = conn.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()[0]
                total_rows += count
                tables.append({"name": name, "row_count": count})

            file_size = os.path.getsize(temp_path)

            return {
                "backup_key": backup_key,
                "file_size_bytes": file_size,
                "table_count": len(tables),
                "total_rows": total_rows,
                "tables": sorted(tables, key=lambda t: t["row_count"], reverse=True),
            }
        finally:
            conn.close()
    finally:
        if os.path.isfile(temp_path):
            os.remove(temp_path)
