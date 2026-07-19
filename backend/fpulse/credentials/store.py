"""SQLite-backed credential store with always-on encryption (May 4 2026).

Sensitive config fields are encrypted at rest using Fernet (AES-128-CBC +
HMAC-SHA256) — the encryptor is wired into app_state at startup for both
Free and Plus. The previous Plus-gated path that left OSS Free credentials
in plaintext is gone.

Tolerates legacy plaintext rows on read so existing OSS installs upgrade
without manual migration: the next save re-encrypts each row.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from copy import deepcopy

from .models import Credential


class CredentialStore:
    """Credential store with secret masking, backed by SQLite.

    Sensitive fields (password, secret, api_key, token, …) are encrypted
    at rest via the always-on Fernet encryptor. Non-sensitive fields
    (host, port, database name) are kept plaintext so operators can
    identify rows in DB tooling without decrypting.
    """

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db):
        self._db = db

    def _get_encryptor(self):
        """Get the always-on encryptor from app_state.

        May 4 2026: removed the `license_mgr.is_plus` gate — credentials
        are now encrypted in OSS Free too, matching what EDITION_MATRIX,
        COMPLIANCE.md, and the trust posture endpoint say.

        Returns None only when FPULSE_DISABLE_ENCRYPTION=1 is set (a
        test-only escape hatch) or when app_state hasn't been populated
        yet (e.g. unit tests constructing the store directly). In both
        cases the store falls back to plaintext storage.
        """
        try:
            from fpulse.main import app_state
            return app_state.get("encryptor")
        except Exception:
            return None

    def _save(self, credential: Credential):
        data = credential.model_dump(mode="json")

        # F-Pulse: encrypt sensitive fields before saving
        encryptor = self._get_encryptor()
        if encryptor:
            data["config"] = encryptor.encrypt_config(data.get("config", {}))

        self._db.insert_json(
            "credentials", credential.id, data,
            name=credential.name,
            type=credential.type,
            project_id=credential.project_id,
            workspace_id=credential.workspace_id or "default",
            created_at=credential.created_at.isoformat(),
            updated_at=credential.updated_at.isoformat(),
        )

    def create(self, credential: Credential) -> Credential:
        self._save(credential)
        return credential

    def get(self, credential_id: str, workspace_id: str | None = None) -> Credential | None:
        """Get a credential — returns None if cross-workspace access is attempted."""
        data = self._db.get_json("credentials", credential_id)
        if data is None:
            return None
        if workspace_id is not None:
            cred_ws = data.get("workspace_id") or "default"
            if cred_ws != workspace_id:
                return None
        return Credential(**data)

    def list_all(
        self,
        cred_type: str | None = None,
        project_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[dict]:
        items = self._db.list_json("credentials")
        result = []
        for c in items:
            if cred_type and c.get("type") != cred_type:
                continue
            if project_id and c.get("project_id") and c.get("project_id") != project_id:
                continue
            if workspace_id is not None:
                cred_ws = c.get("workspace_id") or "default"
                if cred_ws != workspace_id:
                    continue
            # Mask sensitive config fields
            c["config"] = self._mask_config(c.get("config", {}))
            result.append(c)
        return sorted(result, key=lambda x: x.get("name", ""))

    def get_raw(
        self,
        credential_id: str,
        workspace_id: str | None = None,
    ) -> Credential | None:
        """Get credential with unmasked config (for engine use).

        F-Pulse: decrypts encrypted fields before returning.

        If `workspace_id` is provided, enforces the tenant boundary and
        returns None on cross-workspace access. Engine callsites that
        already have a Connection on hand should pass
        `connection.workspace_id` here so a compromised or misconfigured
        pipeline can't deref a credential from another tenant.
        """
        data = self._db.get_json("credentials", credential_id)
        if data is None:
            return None

        if workspace_id is not None:
            cred_ws = data.get("workspace_id") or "default"
            if cred_ws != workspace_id:
                return None

        # F-Pulse: decrypt sensitive fields
        encryptor = self._get_encryptor()
        if encryptor:
            data["config"] = encryptor.decrypt_config(data.get("config", {}))

        return Credential(**data)

    def update(
        self,
        credential_id: str,
        updates: dict,
        workspace_id: str | None = None,
    ) -> Credential | None:
        credential = self.get(credential_id, workspace_id=workspace_id)
        if not credential:
            return None
        for key, value in updates.items():
            # Refuse to cross workspace boundary via an update body —
            # workspace membership of a credential is immutable once set
            # (an admin moving secrets between tenants must delete and
            # recreate, which produces an audit trail).
            if key == "workspace_id":
                continue
            if value is not None and hasattr(credential, key):
                setattr(credential, key, value)
        credential.updated_at = datetime.now(timezone.utc)
        self._save(credential)
        return credential

    def delete(self, credential_id: str, workspace_id: str | None = None) -> bool:
        # Pre-check via get so cross-tenant delete is a no-op + 404
        # rather than silently removing the row from another workspace.
        if workspace_id is not None:
            if not self.get(credential_id, workspace_id=workspace_id):
                return False
        return self._db.delete_row("credentials", credential_id)

    def mark_used(self, credential_id: str, workspace_id: str | None = None) -> None:
        """Update last_used timestamp — scoped when workspace_id provided."""
        cred = self.get(credential_id, workspace_id=workspace_id)
        if cred:
            cred.last_used = datetime.now(timezone.utc)
            self._save(cred)

    def count(self) -> int:
        return self._db.count("credentials")

    @staticmethod
    def _mask_config(config: dict) -> dict:
        """Mask password/secret/key fields in config."""
        masked = {}
        sensitive_keys = {"password", "secret", "secret_access_key", "api_key", "token", "private_key"}
        for k, v in config.items():
            if isinstance(v, str) and k.lower() in sensitive_keys:
                masked[k] = "***" if len(v) <= 4 else v[:2] + "***"
            else:
                masked[k] = v
        return masked
