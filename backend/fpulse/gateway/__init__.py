"""
API Gateway — publish pipelines as REST endpoints with rate limiting and API keys.

Enables external consumers to trigger pipelines or query their results via
authenticated REST endpoints. Each published endpoint has its own API key,
rate limit, and optional IP allowlist.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class GatewayStore:
    """SQLite-backed API gateway for published pipeline endpoints."""

    def __init__(self, db):
        self._db = db
        self._ensure_tables()

    def _ensure_tables(self):
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                key_prefix TEXT NOT NULL,
                workspace_id TEXT DEFAULT 'default',
                created_by TEXT DEFAULT '',
                scopes TEXT DEFAULT '["read","execute"]',
                rate_limit_rpm INTEGER DEFAULT 60,
                ip_allowlist TEXT DEFAULT '[]',
                is_active INTEGER DEFAULT 1,
                last_used_at REAL DEFAULT 0,
                request_count INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                expires_at REAL DEFAULT 0
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS published_endpoints (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                method TEXT DEFAULT 'POST',
                name TEXT DEFAULT '',
                description TEXT DEFAULT '',
                workspace_id TEXT DEFAULT 'default',
                require_api_key INTEGER DEFAULT 1,
                rate_limit_rpm INTEGER DEFAULT 30,
                timeout_seconds INTEGER DEFAULT 300,
                input_schema TEXT DEFAULT '{}',
                is_active INTEGER DEFAULT 1,
                request_count INTEGER DEFAULT 0,
                last_called_at REAL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS rate_limit_log (
                key_hash TEXT NOT NULL,
                endpoint_id TEXT DEFAULT '',
                timestamp REAL NOT NULL
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_rate_limit_ts
            ON rate_limit_log(key_hash, timestamp)
        """)

    # ── API Keys ──────────────────────────────────────────────────────

    def create_api_key(
        self,
        name: str,
        workspace_id: str = "default",
        created_by: str = "",
        scopes: list[str] | None = None,
        rate_limit_rpm: int = 60,
        ip_allowlist: list[str] | None = None,
        expires_days: int = 0,
    ) -> dict:
        """Create a new API key. Returns the full key (shown once only)."""
        key_id = f"ak_{uuid.uuid4().hex[:8]}"
        raw_key = f"fpk_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        key_prefix = raw_key[:8]
        now = time.time()
        expires_at = now + (expires_days * 86400) if expires_days > 0 else 0

        self._db.execute(
            "INSERT INTO api_keys "
            "(id, name, key_hash, key_prefix, workspace_id, created_by, scopes, "
            "rate_limit_rpm, ip_allowlist, is_active, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (key_id, name, key_hash, key_prefix, workspace_id, created_by,
             json.dumps(scopes or ["read", "execute"]), rate_limit_rpm,
             json.dumps(ip_allowlist or []), now, expires_at),
        )
        return {"id": key_id, "name": name, "api_key": raw_key, "prefix": key_prefix,
                "scopes": scopes or ["read", "execute"], "rate_limit_rpm": rate_limit_rpm}

    def validate_api_key(self, raw_key: str, required_scope: str = "read") -> dict | None:
        """Validate an API key. Returns key metadata or None."""
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        row = self._db.fetchone(
            "SELECT * FROM api_keys WHERE key_hash=? AND is_active=1", (key_hash,),
        )
        if not row:
            return None
        # Check expiry
        if row["expires_at"] and row["expires_at"] < time.time():
            return None
        # Check scope
        scopes = json.loads(row.get("scopes", "[]")) if isinstance(row.get("scopes"), str) else row.get("scopes", [])
        if required_scope not in scopes:
            return None
        # Update usage
        self._db.execute(
            "UPDATE api_keys SET last_used_at=?, request_count=request_count+1 WHERE id=?",
            (time.time(), row["id"]),
        )
        return dict(row)

    def list_api_keys(self, workspace_id: str = "default") -> list[dict]:
        """List all API keys (masked)."""
        rows = self._db.fetchall(
            "SELECT id, name, key_prefix, scopes, rate_limit_rpm, is_active, "
            "request_count, last_used_at, created_at, expires_at "
            "FROM api_keys WHERE workspace_id=? ORDER BY created_at DESC",
            (workspace_id,),
        )
        result = []
        for r in rows:
            d = dict(r)
            d["scopes"] = json.loads(d["scopes"]) if isinstance(d.get("scopes"), str) else d.get("scopes", [])
            result.append(d)
        return result

    def revoke_api_key(self, key_id: str):
        self._db.execute("UPDATE api_keys SET is_active=0 WHERE id=?", (key_id,))

    def delete_api_key(self, key_id: str):
        self._db.execute("DELETE FROM api_keys WHERE id=?", (key_id,))

    # ── Rate Limiting ─────────────────────────────────────────────────

    def check_rate_limit(self, key_hash: str, rpm_limit: int, endpoint_id: str = "") -> bool:
        """Check if a request is within rate limits. Returns True if allowed."""
        cutoff = time.time() - 60
        # Clean old entries
        self._db.execute("DELETE FROM rate_limit_log WHERE timestamp < ?", (cutoff - 300,))

        row = self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM rate_limit_log WHERE key_hash=? AND timestamp > ?",
            (key_hash, cutoff),
        )
        count = row["cnt"] if row else 0
        if count >= rpm_limit:
            return False

        self._db.execute(
            "INSERT INTO rate_limit_log (key_hash, endpoint_id, timestamp) VALUES (?, ?, ?)",
            (key_hash, endpoint_id, time.time()),
        )
        return True

    # ── Published Endpoints ───────────────────────────────────────────

    def publish_endpoint(
        self,
        workflow_id: str,
        path: str,
        name: str = "",
        description: str = "",
        method: str = "POST",
        workspace_id: str = "default",
        require_api_key: bool = True,
        rate_limit_rpm: int = 30,
        timeout_seconds: int = 300,
        input_schema: dict | None = None,
    ) -> dict:
        """Publish a pipeline as a REST endpoint."""
        ep_id = f"ep_{uuid.uuid4().hex[:8]}"
        path = path.strip("/")
        now = time.time()
        self._db.execute(
            "INSERT INTO published_endpoints "
            "(id, workflow_id, path, method, name, description, workspace_id, "
            "require_api_key, rate_limit_rpm, timeout_seconds, input_schema, "
            "is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (ep_id, workflow_id, path, method.upper(), name, description,
             workspace_id, 1 if require_api_key else 0, rate_limit_rpm,
             timeout_seconds, json.dumps(input_schema or {}), now, now),
        )
        return self.get_endpoint(ep_id)

    def get_endpoint(self, endpoint_id: str) -> dict | None:
        row = self._db.fetchone("SELECT * FROM published_endpoints WHERE id=?", (endpoint_id,))
        return self._ep_to_dict(row) if row else None

    def get_endpoint_by_path(self, path: str) -> dict | None:
        path = path.strip("/")
        row = self._db.fetchone(
            "SELECT * FROM published_endpoints WHERE path=? AND is_active=1", (path,),
        )
        return self._ep_to_dict(row) if row else None

    def list_endpoints(self, workspace_id: str = "default") -> list[dict]:
        rows = self._db.fetchall(
            "SELECT * FROM published_endpoints WHERE workspace_id=? ORDER BY created_at DESC",
            (workspace_id,),
        )
        return [self._ep_to_dict(r) for r in rows]

    def update_endpoint(self, endpoint_id: str, **kwargs) -> dict | None:
        allowed = {"name", "description", "rate_limit_rpm", "timeout_seconds", "is_active", "require_api_key"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return self.get_endpoint(endpoint_id)
        set_clause = ", ".join(f"{k}=?" for k in updates)
        params = list(updates.values()) + [time.time(), endpoint_id]
        self._db.execute(
            f"UPDATE published_endpoints SET {set_clause}, updated_at=? WHERE id=?", tuple(params),
        )
        return self.get_endpoint(endpoint_id)

    def delete_endpoint(self, endpoint_id: str):
        self._db.execute("DELETE FROM published_endpoints WHERE id=?", (endpoint_id,))

    def record_call(self, endpoint_id: str):
        self._db.execute(
            "UPDATE published_endpoints SET request_count=request_count+1, last_called_at=? WHERE id=?",
            (time.time(), endpoint_id),
        )

    def _ep_to_dict(self, row) -> dict:
        d = dict(row)
        if "input_schema" in d and isinstance(d["input_schema"], str):
            try:
                d["input_schema"] = json.loads(d["input_schema"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d
