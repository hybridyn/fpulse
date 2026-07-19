"""Secure SSH/SFTP client construction for the FTP connector nodes.

Paramiko's ``AutoAddPolicy`` silently trusts whatever host key a server
presents on first contact, which leaves SFTP transfers open to
man-in-the-middle interception. This module builds an ``SSHClient`` that
*verifies* host keys instead:

* known hosts are loaded from the system file plus any operator-supplied
  ``known_hosts`` path (per-connection config or ``FPULSE_SSH_KNOWN_HOSTS``);
* a pinned fingerprint (``host_key_fingerprint`` / ``host_key`` in the
  connection config, or ``FPULSE_SSH_HOST_KEY_FINGERPRINT``) is enforced when
  provided — accepts SHA256 base64 or MD5 hex forms;
* unknown hosts are rejected by default (``RejectPolicy``);
* the legacy auto-add behaviour is still available, but only when explicitly
  opted into via ``auto_add_host_key`` in the config or
  ``FPULSE_SSH_AUTO_ADD_HOST_KEYS=1`` — the same secure-by-default / explicit
  opt-in pattern used for the Code Script node.

paramiko is an optional dependency, so it is imported lazily inside the
functions here (this module must import cleanly without it).
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

_TRUTHY = {"1", "true", "yes", "on"}


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in _TRUTHY


class _PinnedFingerprintPolicy:
    """Host-key policy that accepts only a pre-shared fingerprint.

    Duck-typed to paramiko's policy protocol (it implements
    ``missing_host_key``) so this module does not need to subclass
    ``paramiko.MissingHostKeyPolicy`` at import time.
    """

    def __init__(self, fingerprint: str) -> None:
        self._want = fingerprint.strip()

    def _matches(self, key: Any) -> bool:
        raw = key.asbytes()
        sha256_b64 = base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
        md5_hex = hashlib.md5(raw).hexdigest()
        want = self._want
        # SHA256 base64 form is case-sensitive; tolerate a 'SHA256:' prefix
        # and trailing '=' padding.
        w = want[7:] if want.lower().startswith("sha256:") else want
        if w.rstrip("=") == sha256_b64:
            return True
        # MD5 hex form is case-insensitive; tolerate a 'MD5:' prefix and the
        # conventional colon separators.
        w = want.lower()
        w = w[4:] if w.startswith("md5:") else w
        if w.replace(":", "") == md5_hex:
            return True
        return False

    def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
        import paramiko

        if not self._matches(key):
            raise paramiko.SSHException(
                f"Host key verification failed for {hostname}: the presented "
                "key does not match the pinned fingerprint."
            )
        # Fingerprint matched — trust it for the life of this connection.
        client.get_host_keys().add(hostname, key.get_name(), key)


def build_ssh_client(config: dict[str, Any] | None = None):
    """Return a ``paramiko.SSHClient`` with host-key verification configured.

    See the module docstring for the policy precedence. ``config`` is the
    merged node params + resolved connection config; unrelated keys are
    ignored.
    """
    import paramiko

    config = config or {}
    client = paramiko.SSHClient()

    # Load verified hosts so previously-trusted servers connect without prompts.
    try:
        client.load_system_host_keys()
    except Exception:  # noqa: BLE001 — absent/unreadable known_hosts is non-fatal
        pass
    known_hosts = config.get("known_hosts") or os.environ.get("FPULSE_SSH_KNOWN_HOSTS")
    if known_hosts and os.path.exists(str(known_hosts)):
        try:
            client.load_host_keys(str(known_hosts))
        except Exception:  # noqa: BLE001
            pass

    pinned = (
        config.get("host_key_fingerprint")
        or config.get("host_key")
        or os.environ.get("FPULSE_SSH_HOST_KEY_FINGERPRINT")
    )
    if pinned:
        client.set_missing_host_key_policy(_PinnedFingerprintPolicy(str(pinned)))
    elif _truthy(config.get("auto_add_host_key")) or _truthy(
        os.environ.get("FPULSE_SSH_AUTO_ADD_HOST_KEYS", "")
    ):
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    return client
