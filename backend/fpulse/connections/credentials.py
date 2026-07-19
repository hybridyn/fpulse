"""Credential resolution — the boundary between catalog providers and
the secret store.

Providers don't read `connection.config` for tokens directly. They
go through `resolve_credentials(connection)` which:
  1. Tries Vault when `vault_key` is set on the connection.
  2. Falls back to `connection.config` for OSS deployments without
     the Plus Vault stack.

This way a single provider implementation works in both deployment
modes; tightening the Vault binding later is a one-place change.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _try_vault_get(vault_key: str) -> dict[str, Any] | None:
    """Best-effort Vault read. Returns None if the Vault stack isn't
    present in this build (OSS without Plus) or the key is missing."""
    try:
        from fpulse.main import app_state
        vault = app_state.get("vault")
        if vault is None:
            return None
        try:
            data = vault.get(vault_key)
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _try_vault_put(vault_key: str, data: dict[str, Any]) -> bool:
    try:
        from fpulse.main import app_state
        vault = app_state.get("vault")
        if vault is None:
            return False
        try:
            vault.put(vault_key, data)
            return True
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001
        return False


def resolve_credentials(connection: Any, override_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the merged credential dict for this connection.

    Precedence (last wins):
      1. Connection.config (non-secret defaults like host/port/database)
      2. Vault payload at connection.vault_key (when present)
      3. Linked credential via connection.credential_id (legacy path)
      4. Explicit override_config (for /test-inline calls)
    """
    out: dict[str, Any] = {}

    # 1. Connection config — non-secret metadata, always present.
    cfg = getattr(connection, "config", None) or {}
    if isinstance(cfg, dict):
        out.update(cfg)

    # 2. Vault — secrets, optional.
    vault_key = getattr(connection, "vault_key", None) or out.get("vault_key")
    if vault_key:
        v = _try_vault_get(vault_key)
        if v:
            out.update(v)

    # 3. Legacy credential link — pre-Vault path; still supported.
    cred_id = getattr(connection, "credential_id", None)
    if cred_id:
        try:
            from fpulse.main import app_state
            cred_store = app_state.get("credential_store")
            if cred_store:
                cred = cred_store.get_raw(cred_id)
                if cred and getattr(cred, "config", None):
                    out.update(cred.config)
                    # 2026-05-28 — mark the credential as used so the
                    # CredentialsPage "Last Used" column reflects actual
                    # pipeline activity, not just the date of the last
                    # manual Test click. Best-effort; a write failure
                    # here must never break credential resolution.
                    try:
                        conn_ws = getattr(connection, "workspace_id", None) or "default"
                        cred_store.mark_used(cred_id, workspace_id=conn_ws)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass

    # 4. Caller-supplied override (test-inline).
    if override_config:
        out.update(override_config)

    return out


def writeback_credentials(connection: Any, updated: dict[str, Any]) -> bool:
    """Persist refreshed OAuth tokens back where they came from.
    Returns True if the writeback succeeded. Never raises."""
    vault_key = getattr(connection, "vault_key", None) or (getattr(connection, "config", None) or {}).get("vault_key")
    if vault_key:
        return _try_vault_put(vault_key, updated)
    # No Vault → silently skip. The new token is still used in-memory
    # for this request; just won't survive a restart. OSS users can
    # opt into Vault by setting connection.vault_key.
    return False
