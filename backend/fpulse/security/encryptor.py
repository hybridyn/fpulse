"""
Encryptor — Fernet-backed encryption for credentials + AI provider API keys.

Replaces the previous Plus-gated encryption: from May 4 2026, every
install (Free OR Plus) encrypts credential blobs and provider API keys
at rest. The trust posture, COMPLIANCE.md, and product knowledge can
honestly say "encrypted at rest" without contradiction with the code.

## What this is

Standard `cryptography.fernet.Fernet`:
  * Cipher:  AES-128-CBC for confidentiality
  * MAC:     HMAC-SHA256 for integrity (authenticated encryption)
  * Format:  URL-safe base64 over `version | timestamp | iv | ciphertext | hmac`
  * Key:     32 bytes (256 bits), URL-safe base64 encoded

Fernet is the recommended high-level interface in the `cryptography`
library — it's hard to misuse, it's not subject to the AEAD nonce-reuse
foot-guns of raw GCM, and the format is versioned so we can rotate
without breaking old ciphertexts.

## Master key

One 32-byte symmetric key per install, stored at `~/.fpulse/secret.key`
(or `$FPULSE_DATA_DIR/secret.key` if `FPULSE_DATA_DIR` is set). The
file is created on first startup if missing; subsequent starts re-read
it. POSIX permissions are verified — F-Pulse refuses to start if the
key file is world-readable on a POSIX filesystem (fail-closed, no
fallback).

## Migration from plaintext

Two legacy formats existed on installs predating May 4 2026:
  * Credentials store: raw dicts, no marker (`{"password": "abc123"}`)
  * AI config store: `PLAIN:<plaintext>` sentinel

Both are tolerated on READ. The very next WRITE re-encrypts. Operators
running an admin migration script (see `docs/admin/encrypt_existing.md`)
can force re-encryption of every stored credential without an edit.

## Sentinel format

Encrypted values are stored with a versioned prefix:
  * Single value:  `ENC:v1:<fernet_token>`
  * Whole config:  the JSON-serialised dict where every sensitive value
                   is `ENC:v1:...`. Non-sensitive fields stay plaintext
                   (host, port, database name) so an operator looking
                   at the SQLite blob can still recognise the row.

## Sensitive field names

The credential `encrypt_config` only encrypts fields whose key matches
this allowlist. Adding a new sensitive field name here makes it
encrypted on the next save without a migration:

    password, secret, key, token, private_key, api_key, apikey,
    client_secret, passphrase, access_token, refresh_token, sasl_password

Database `host`, `port`, `database`, `user`/`username` stay plaintext —
they're not secrets and operators need to read them in DB tooling for
incident response.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


_ENC_PREFIX = "ENC:v1:"
_PLAIN_PREFIX = "PLAIN:"   # legacy AI config

_SENSITIVE_FIELD_NAMES = frozenset({
    "password", "secret", "key", "token", "private_key",
    "api_key", "apikey", "client_secret", "passphrase",
    "access_token", "refresh_token", "sasl_password",
    "private_key_pem", "service_account_json",
})


# ─────────────────────────────────────────────────────────────────────
# Master key management
# ─────────────────────────────────────────────────────────────────────


def master_key_path() -> Path:
    """Resolve the path of the symmetric master key file.

    Order:
      1. `FPULSE_MASTER_KEY_FILE` env var (operator override)
      2. `<FPULSE_DATA_DIR>/secret.key`
      3. `~/.fpulse/secret.key`
    """
    explicit = os.environ.get("FPULSE_MASTER_KEY_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    data_dir = os.environ.get("FPULSE_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir).expanduser() / "secret.key"
    return Path.home() / ".fpulse" / "secret.key"


def _verify_perms(path: Path) -> None:
    """Refuse world-readable key files on POSIX. No-op on Windows where
    NTFS ACLs are the responsibility of the operator's deployment."""
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise RuntimeError(
            f"Cannot stat master key file {path}: {exc}"
        ) from exc
    # World/group readable bits set?
    if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError(
            f"Master key file {path} is group/world readable "
            f"(mode={oct(mode & 0o777)}). Refuse to start. "
            f"Run: chmod 600 {path}"
        )


def load_or_create_master_key() -> bytes:
    """Read the master key, generating one on first run.

    Returns the 32-byte URL-safe-base64 key Fernet expects. Creates the
    parent directory with mode 700 if missing. The key file itself is
    chmod 600 on POSIX.
    """
    path = master_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            # Best-effort — some filesystems (FAT32, ACL-only) reject chmod.
            pass

    if path.is_file():
        _verify_perms(path)
        key = path.read_bytes().strip()
        # Sanity: Fernet keys are 44 chars (32 bytes base64). Fail loud if
        # someone wrote a different file at this path.
        if len(key) < 32:
            raise RuntimeError(
                f"Master key file {path} is too short to be a Fernet key. "
                f"Either restore a backup or delete the file to regenerate."
            )
        return key

    # First run — generate and persist.
    key = Fernet.generate_key()
    path.write_bytes(key)
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    logger.info(
        "F-Pulse: created new master key at %s (mode 600). "
        "Back this file up — without it, encrypted credentials are unrecoverable.",
        path,
    )
    return key


# ─────────────────────────────────────────────────────────────────────
# Encryptor
# ─────────────────────────────────────────────────────────────────────


class Encryptor:
    """Fernet-based credential + API-key encryptor.

    Construct via `Encryptor.from_master_key()` — the loader handles
    file location, permission check, and first-run generation.
    """

    def __init__(self, key: bytes) -> None:
        try:
            self._fernet = Fernet(key)
        except (TypeError, ValueError, base64.binascii.Error) as exc:
            raise RuntimeError(
                f"Invalid Fernet key: {exc}. Master key file may be "
                f"corrupt; restore from backup."
            ) from exc

    # ── Construction helpers ────────────────────────────────────────

    @classmethod
    def from_master_key(cls) -> "Encryptor":
        """Load (or create) the master key and return an Encryptor."""
        return cls(load_or_create_master_key())

    # ── Single-value API ────────────────────────────────────────────

    def encrypt_value(self, plaintext: str) -> str:
        """Return `ENC:v1:<token>`. Empty input returns empty."""
        if not plaintext:
            return ""
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return f"{_ENC_PREFIX}{token}"

    def decrypt_value(self, ciphertext: str) -> str:
        """Reverse of encrypt_value. Tolerates legacy formats:
          * `ENC:v1:<token>`  → decrypt
          * `PLAIN:<value>`   → return value (legacy AI config sentinel)
          * empty             → empty
          * other             → return as-is (treat as plaintext for
                                 backward compatibility with very old
                                 OSS installs that had no encryption)
        """
        if not ciphertext:
            return ""
        if ciphertext.startswith(_ENC_PREFIX):
            try:
                return self._fernet.decrypt(
                    ciphertext[len(_ENC_PREFIX):].encode("ascii")
                ).decode("utf-8")
            except InvalidToken as exc:
                raise RuntimeError(
                    "Decryption failed — master key may have changed since "
                    "this credential was saved. Restore the original "
                    "secret.key or re-enter the credential."
                ) from exc
        if ciphertext.startswith(_PLAIN_PREFIX):
            return ciphertext[len(_PLAIN_PREFIX):]
        # Untyped plaintext — only happens on installs that pre-date this
        # encryptor entirely. Return as-is; the next save will encrypt.
        return ciphertext

    # ── Config-dict API ─────────────────────────────────────────────

    def encrypt_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of `config` with sensitive fields encrypted in
        place. Non-sensitive fields (host, port, database, user) are
        passed through unchanged so operators can identify rows in
        DB tooling without decrypting."""
        if not isinstance(config, dict):
            return config
        out: dict[str, Any] = {}
        for k, v in config.items():
            if self._is_sensitive_field(k) and isinstance(v, str) and v:
                out[k] = self.encrypt_value(v)
            elif isinstance(v, dict):
                out[k] = self.encrypt_config(v)
            else:
                out[k] = v
        return out

    def decrypt_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Reverse of encrypt_config. Tolerant of mixed plaintext +
        ciphertext rows produced during the migration window."""
        if not isinstance(config, dict):
            return config
        out: dict[str, Any] = {}
        for k, v in config.items():
            if self._is_sensitive_field(k) and isinstance(v, str) and v:
                out[k] = self.decrypt_value(v)
            elif isinstance(v, dict):
                out[k] = self.decrypt_config(v)
            else:
                out[k] = v
        return out

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _is_sensitive_field(name: str) -> bool:
        if not isinstance(name, str):
            return False
        return name.lower() in _SENSITIVE_FIELD_NAMES


# ─────────────────────────────────────────────────────────────────────
# Module-level convenience for tests + scripts
# ─────────────────────────────────────────────────────────────────────


def fresh_encryptor_for_tests() -> Encryptor:
    """Create an Encryptor with an ephemeral key (not persisted). Useful
    in unit tests that need encryption without touching the user's home dir."""
    return Encryptor(secrets.token_bytes(32) and Fernet.generate_key())
