"""Security primitives — encryptor, key management, audit hooks.

Public surface:
  * `Encryptor` — Fernet-backed AES-128-CBC + HMAC-SHA256 for credentials
    and AI provider API keys. Always wired in main.py, both Free and Plus.
  * `load_or_create_master_key()` — reads `~/.fpulse/secret.key`, generates
    one on first run, refuses to start on world-readable POSIX permissions.

This module replaces the previous behaviour where credential encryption
was Plus-gated and OSS stored secrets in plaintext (or `PLAIN:<value>`
sentinel for AI config). The new default: every install — Free or Plus —
encrypts credentials at rest. Plus adds Vault-Ref as an additional path.
"""

from .encryptor import (
    Encryptor,
    load_or_create_master_key,
    master_key_path,
)

__all__ = [
    "Encryptor",
    "load_or_create_master_key",
    "master_key_path",
]
