"""SQLite-backed store for AI provider configuration.

Two tables, same shape:
  * ``user_ai_config``     — per-user row (Free/OSS tier, AccountPage)
  * ``workspace_ai_config`` — per-workspace row (Plus tier, AdminPage)

Both store the API key encrypted via the credential encryptor (the shared
"ENC:..." format). Plaintext keys are only ever held in memory during a
single request — the DB row is always ciphertext.

Layering mirrors WorkspaceStore:
  * constructor takes optional ``db`` and ``encryptor`` (late-bind via
    set_db / set_encryptor from main.py)
  * all writes go through a single ``_upsert_*`` helper so the columns
    and the encryption step stay in sync
  * the store does NOT enforce RBAC — that's the API layer's job
  * reads are tolerant: a bad ciphertext returns the row with an empty
    api_key rather than crashing the whole endpoint
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Columns returned to API callers. The encrypted key is NEVER included —
# callers only see whether a key is configured (has_key bool).
_SAFE_COLUMNS = (
    "enabled",
    "provider",
    "model",
    "base_url",
    "has_key",
    # v32: the id (not the secret) of a `credentials` store row the key is
    # imported from. Safe to return — it's a reference, not the key.
    "credential_id",
    "created_at",
    "updated_at",
)


class AIConfigStore:
    """CRUD for user_ai_config and workspace_ai_config tables.

    Never returns the ciphertext or plaintext API key to HTTP callers —
    the router uses ``get_user_config()`` / ``get_workspace_config()`` for
    display (which return ``has_key: bool`` instead) and
    ``resolve_active_key()`` internally when it needs the plaintext for
    the LLM call.
    """

    def __init__(self, db=None, encryptor=None):
        self._db = db
        self._enc = encryptor
        # v32: optional callable injected from main.py that resolves a
        # `credentials` store row to its plaintext API key.
        # Signature: fn(credential_id: str, workspace_id: str | None) -> str | None.
        # Kept as an injected dependency (not a direct import) so the AI
        # config store stays decoupled from the credential store and
        # remains trivially testable.
        self._cred_resolver = None

    def set_db(self, db) -> None:
        self._db = db

    def set_encryptor(self, encryptor) -> None:
        self._enc = encryptor

    def set_credential_resolver(self, resolver) -> None:
        """Inject the credential→plaintext-key resolver (see __init__)."""
        self._cred_resolver = resolver

    # ── Helpers ─────────────────────────────────────────────────────────

    def _encrypt(self, plaintext: str) -> str:
        """Encrypt, tolerating a missing encryptor (tests / fallback)."""
        if not plaintext:
            return ""
        if self._enc is None:
            # Fallback: store as plaintext wrapped in a sentinel so we
            # can tell on read. This only happens in tests that forgot
            # to inject an encryptor; production always has one.
            return f"PLAIN:{plaintext}"
        return self._enc.encrypt_value(plaintext)

    def _decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ""
        if ciphertext.startswith("PLAIN:"):
            return ciphertext[6:]
        if self._enc is None:
            return ""
        try:
            return self._enc.decrypt_value(ciphertext)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("F-Pulse AI config: decrypt failed: %s", exc)
            return ""

    def _merge_secret_source(
        self, *, existing, api_key: str | None, credential_id: str | None
    ) -> tuple[str, str]:
        """Reconcile the inline key and the credential reference into the
        two column values to persist, enforcing a single source of truth.

        ``existing`` is the prior row (or None) projecting at least
        ``api_key_encrypted`` and ``credential_id``. Both ``api_key`` and
        ``credential_id`` are tri-state (None=keep / ""=clear / value=set).
        Returns ``(api_key_encrypted, credential_id)``.
        """
        prev_encrypted = (existing["api_key_encrypted"] if existing else "") or ""
        prev_cred = (existing["credential_id"] if existing else "") or ""

        # An explicitly-supplied secret this call wins and clears the other
        # source, so the secret has exactly one home:
        #   * a non-empty credential_id → use the credential, drop inline
        #   * else a non-empty api_key   → use inline, drop the reference
        if credential_id:
            return "", credential_id
        if api_key:
            return self._encrypt(api_key), ""

        # No new secret supplied — apply the tri-state per column against
        # the prior values (None = keep, "" = clear).
        encrypted = prev_encrypted if api_key is None else ""
        cred_ref = prev_cred if credential_id is None else ""
        # If a prior credential reference survives, keep the inline key
        # clear so a stale ciphertext can't shadow the reference.
        if cred_ref:
            encrypted = ""
        return encrypted, cred_ref

    @staticmethod
    def _row_to_safe_dict(row, *, extra: dict | None = None) -> dict:
        """Shape a DB row into the display dict (no ciphertext leaks)."""
        if row is None:
            return {}
        credential_id = ""
        try:
            credential_id = row["credential_id"] or ""
        except (IndexError, KeyError):
            # Older SELECT that didn't project the column — treat as none.
            credential_id = ""
        d: dict[str, Any] = {
            "enabled": bool(row["enabled"]),
            "provider": row["provider"] or "",
            "model": row["model"] or "",
            "base_url": row["base_url"] or "",
            # A key is "present" if either an inline key OR a credential
            # reference is configured. The form uses this to decide whether
            # to show "key on file".
            "has_key": bool(row["api_key_encrypted"]) or bool(credential_id),
            "credential_id": credential_id,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if extra:
            d.update(extra)
        return d

    # ── User AI config (Free tier) ──────────────────────────────────────

    def get_user_config(self, user_id: str) -> dict:
        """Return the user's AI config with ``has_key`` instead of the
        ciphertext. Returns an empty-but-shaped dict when no row exists
        so the frontend form can bind against stable keys.
        """
        row = self._db.fetchone(
            """SELECT enabled, provider, model, api_key_encrypted, base_url,
                      credential_id, workspace_id, created_at, updated_at
               FROM user_ai_config
               WHERE user_id = ?""",
            (user_id,),
        )
        if not row:
            return {
                "enabled": False,
                "provider": "",
                "model": "",
                "base_url": "",
                "has_key": False,
                "credential_id": "",
                "workspace_id": "",
                "created_at": None,
                "updated_at": None,
            }
        return self._row_to_safe_dict(row, extra={"workspace_id": row["workspace_id"]})

    def upsert_user_config(
        self,
        *,
        user_id: str,
        workspace_id: str,
        enabled: bool,
        provider: str,
        model: str,
        base_url: str,
        api_key: str | None,
        credential_id: str | None = None,
    ) -> dict:
        """Insert or update the per-user row.

        ``api_key=None`` means "keep the existing encrypted key" (user
        toggling enabled/provider without re-entering their key).
        ``api_key=""`` means "clear the key".

        ``credential_id`` follows the same tri-state: ``None`` keeps the
        existing reference, ``""`` clears it, any id imports the key from
        that credential. A non-empty reference clears the inline key (and
        vice-versa) so the secret has exactly one home.
        """
        now = _now_iso()
        existing = self._db.fetchone(
            """SELECT api_key_encrypted, credential_id
               FROM user_ai_config WHERE user_id = ?""",
            (user_id,),
        )
        encrypted, cred_ref = self._merge_secret_source(
            existing=existing, api_key=api_key, credential_id=credential_id
        )

        self._db.execute(
            """INSERT INTO user_ai_config
               (user_id, workspace_id, enabled, provider, model,
                api_key_encrypted, base_url, credential_id,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   workspace_id = excluded.workspace_id,
                   enabled = excluded.enabled,
                   provider = excluded.provider,
                   model = excluded.model,
                   api_key_encrypted = excluded.api_key_encrypted,
                   base_url = excluded.base_url,
                   credential_id = excluded.credential_id,
                   updated_at = excluded.updated_at""",
            (
                user_id,
                workspace_id or "default",
                1 if enabled else 0,
                provider or "",
                model or "",
                encrypted,
                base_url or "",
                cred_ref,
                now,
                now,
            ),
        )
        self._db.conn.commit()
        return self.get_user_config(user_id)

    def delete_user_config(self, user_id: str) -> bool:
        cur = self._db.execute(
            "DELETE FROM user_ai_config WHERE user_id = ?", (user_id,)
        )
        self._db.conn.commit()
        return cur.rowcount > 0

    # ── Workspace AI config (Plus tier) ─────────────────────────────────

    def get_workspace_config(self, workspace_id: str) -> dict:
        row = self._db.fetchone(
            """SELECT enabled, provider, model, api_key_encrypted, base_url,
                      credential_id, allow_user_override, monthly_budget_usd,
                      configured_by, created_at, updated_at
               FROM workspace_ai_config
               WHERE workspace_id = ?""",
            (workspace_id,),
        )
        if not row:
            return {
                "enabled": False,
                "provider": "",
                "model": "",
                "base_url": "",
                "has_key": False,
                "credential_id": "",
                "allow_user_override": False,
                "monthly_budget_usd": 0.0,
                "configured_by": "",
                "created_at": None,
                "updated_at": None,
            }
        return self._row_to_safe_dict(
            row,
            extra={
                "allow_user_override": bool(row["allow_user_override"]),
                "monthly_budget_usd": float(row["monthly_budget_usd"] or 0.0),
                "configured_by": row["configured_by"] or "",
            },
        )

    def upsert_workspace_config(
        self,
        *,
        workspace_id: str,
        enabled: bool,
        provider: str,
        model: str,
        base_url: str,
        api_key: str | None,
        allow_user_override: bool,
        monthly_budget_usd: float,
        configured_by: str,
        credential_id: str | None = None,
    ) -> dict:
        now = _now_iso()
        existing = self._db.fetchone(
            """SELECT api_key_encrypted, credential_id
               FROM workspace_ai_config WHERE workspace_id = ?""",
            (workspace_id,),
        )
        encrypted, cred_ref = self._merge_secret_source(
            existing=existing, api_key=api_key, credential_id=credential_id
        )

        self._db.execute(
            """INSERT INTO workspace_ai_config
               (workspace_id, enabled, provider, model, api_key_encrypted,
                base_url, credential_id, allow_user_override,
                monthly_budget_usd, configured_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(workspace_id) DO UPDATE SET
                   enabled = excluded.enabled,
                   provider = excluded.provider,
                   model = excluded.model,
                   api_key_encrypted = excluded.api_key_encrypted,
                   base_url = excluded.base_url,
                   credential_id = excluded.credential_id,
                   allow_user_override = excluded.allow_user_override,
                   monthly_budget_usd = excluded.monthly_budget_usd,
                   configured_by = excluded.configured_by,
                   updated_at = excluded.updated_at""",
            (
                workspace_id,
                1 if enabled else 0,
                provider or "",
                model or "",
                encrypted,
                base_url or "",
                cred_ref,
                1 if allow_user_override else 0,
                float(monthly_budget_usd or 0.0),
                configured_by or "",
                now,
                now,
            ),
        )
        self._db.conn.commit()
        return self.get_workspace_config(workspace_id)

    def delete_workspace_config(self, workspace_id: str) -> bool:
        cur = self._db.execute(
            "DELETE FROM workspace_ai_config WHERE workspace_id = ?",
            (workspace_id,),
        )
        self._db.conn.commit()
        return cur.rowcount > 0

    # ── Resolution (used by ai_client at request time) ──────────────────

    def resolve_active_config(
        self,
        *,
        user_id: str | None,
        workspace_id: str | None,
    ) -> dict | None:
        """Return the effective AI config with PLAINTEXT api_key, or None.

        Resolution order (first hit wins):
          1. workspace_ai_config[workspace_id] if enabled AND
             allow_user_override = 0 (workspace forces its own config)
          2. user_ai_config[user_id] if enabled
          3. workspace_ai_config[workspace_id] if enabled AND
             allow_user_override = 1 (workspace is the fallback)
          4. None — caller should try env vars next

        Returned dict shape:
          { provider, model, api_key, base_url, source } where source is
          "workspace" or "user" — useful for audit logging.
        """
        ws_row = None
        if workspace_id:
            ws_row = self._db.fetchone(
                """SELECT enabled, provider, model, api_key_encrypted,
                          base_url, credential_id, allow_user_override
                   FROM workspace_ai_config WHERE workspace_id = ?""",
                (workspace_id,),
            )

        # (1) workspace forces its config when override is off
        if ws_row and ws_row["enabled"] and not ws_row["allow_user_override"]:
            return self._shape_resolved(ws_row, source="workspace", workspace_id=workspace_id)

        # (2) user config
        if user_id:
            user_row = self._db.fetchone(
                """SELECT enabled, provider, model, api_key_encrypted,
                          base_url, credential_id
                   FROM user_ai_config WHERE user_id = ?""",
                (user_id,),
            )
            if user_row and user_row["enabled"]:
                return self._shape_resolved(user_row, source="user", workspace_id=workspace_id)

        # (3) workspace fallback when override is allowed
        if ws_row and ws_row["enabled"]:
            return self._shape_resolved(ws_row, source="workspace", workspace_id=workspace_id)

        return None

    def _shape_resolved(self, row, *, source: str, workspace_id: str | None = None) -> dict:
        # The key comes from the referenced credential when one is set,
        # otherwise from the inline encrypted column. provider/model/
        # base_url always come from the AI-config row itself.
        api_key = ""
        credential_id = ""
        try:
            credential_id = row["credential_id"] or ""
        except (IndexError, KeyError):
            credential_id = ""
        if credential_id and self._cred_resolver is not None:
            try:
                api_key = self._cred_resolver(credential_id, workspace_id) or ""
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "F-Pulse AI config: credential resolve failed for %s: %s",
                    credential_id, exc,
                )
                api_key = ""
        else:
            api_key = self._decrypt(row["api_key_encrypted"] or "")
        return {
            "provider": row["provider"] or "",
            "model": row["model"] or "",
            "api_key": api_key,
            "base_url": row["base_url"] or "",
            "source": source,
        }
