"""Credential models — secure storage for connection secrets."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class Credential(BaseModel):
    """A stored credential for database/cloud/API connections."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    type: str  # postgresql, mysql, s3, kafka, rest_api, etc.
    config: dict[str, Any] = Field(default_factory=dict)  # type-specific fields
    project_id: str = ""  # empty = global within workspace
    # Tenant boundary — credentials NEVER cross workspaces, even when
    # project_id is "" ("global"). "Global within workspace" is the
    # correct read: a DB password that's visible to every project in
    # one workspace must stay invisible to every project in another
    # workspace. Legacy rows back-filled to 'default' by v7.
    workspace_id: str = "default"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_used: datetime | None = None
    # ── Metadata surfaced in the Credentials list (Apr 18) ──
    # `created_by` / `updated_by` — audit trail of who touched the record.
    # `environment` — 'dev' | 'prod' | 'all' (defaults to 'all' for legacy
    #                  records that pre-date this column).
    # `expires_at` — rotation target; UI badges expiry countdown.
    # `description` — free-text note (visible as a chip in the list).
    # `source` — storage backend: 'local' | 'builtin_vault' | 'azure_kv' |
    #            'aws_sm' | 'hashi_vault' | 'gcp_sm'. Non-local values
    #            require a configured Vault Provider (see Admin → Vaults).
    # `vault_reference` — path/identifier within the external vault.
    created_by: str | None = None
    updated_by: str | None = None
    environment: str | None = None  # 'dev' | 'prod' | 'all'
    expires_at: datetime | None = None
    description: str | None = None
    source: str | None = None          # 'local' (default) or vault provider
    vault_reference: str | None = None


class CredentialCreate(BaseModel):
    name: str
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    project_id: str = ""
    # Optional metadata — frontend may or may not send these; backend
    # stores whatever arrives and ignores the rest.
    environment: str | None = None
    expires_at: datetime | None = None
    description: str | None = None
    source: str | None = None
    vault_reference: str | None = None


class CredentialUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    config: dict[str, Any] | None = None
    project_id: str | None = None
    environment: str | None = None
    expires_at: datetime | None = None
    description: str | None = None
    source: str | None = None
    vault_reference: str | None = None
