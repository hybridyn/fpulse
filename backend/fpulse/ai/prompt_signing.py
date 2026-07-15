"""
HMAC-sign system prompts at startup; verify on every agent call.

Threat model: an in-process attacker (malicious dependency, runtime
patcher) mutates the agent's system prompt to bypass safety guidance.
The signer registers the canonical prompt(s) at import time, hashes them
with an HMAC key, and the agent loop verifies before sending to the LLM.

Defense scope — explicit:
  - Catches accidental mutation (linter rewrite, monkey-patch in tests).
  - Catches naive in-process tampering (a dep patches SYSTEM_PROMPT_TEMPLATE).
  - Does NOT defend against a fully-compromised process — an attacker
    that can patch the prompt can also patch the verifier. The defense
    is shallow but cheap; it raises the bar for attack tooling and
    surfaces accidents loudly.

Key resolution:
  1. `FPULSE_AI_PROMPT_SIGNING_KEY` env var (hex or raw — we accept either)
  2. Random 32 bytes per process if env var unset

Production deployments should set the env var so signatures are stable
across restarts (useful for trace replay verification).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field


def _resolve_key() -> bytes:
    """Read signing key from env or generate a per-process one.

    Accepts env value as either:
      - hex string ('a1b2c3...') — decoded
      - any other string — UTF-8 encoded bytes
    Empty / missing → 32 fresh random bytes.
    """
    raw = os.environ.get("FPULSE_AI_PROMPT_SIGNING_KEY", "")
    if not raw:
        return secrets.token_bytes(32)
    try:
        return bytes.fromhex(raw)
    except ValueError:
        return raw.encode("utf-8")


@dataclass
class PromptSigner:
    """HMAC-SHA256 signer for prompt templates.

    Construct with `PromptSigner.with_key()`; register prompts via
    `sign(name, prompt)`; verify before each LLM call via `verify(name, prompt)`.

    Re-signing the same name overwrites the prior signature — useful when
    a prompt is intentionally updated at runtime (Plus workspace policy
    customisation, future).
    """

    _key: bytes
    _signatures: dict[str, str] = field(default_factory=dict)

    @classmethod
    def with_key(cls, key: bytes | None = None) -> "PromptSigner":
        return cls(_key=key if key is not None else _resolve_key())

    def sign(self, name: str, prompt: str) -> str:
        """Compute + store the HMAC for `prompt`. Returns the hex digest."""
        if not name:
            raise ValueError("name must not be empty")
        sig = hmac.new(self._key, prompt.encode("utf-8"), hashlib.sha256).hexdigest()
        self._signatures[name] = sig
        return sig

    def verify(self, name: str, prompt: str) -> bool:
        """Constant-time compare against the registered signature."""
        expected = self._signatures.get(name)
        if expected is None:
            return False
        actual = hmac.new(self._key, prompt.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, actual)

    def has(self, name: str) -> bool:
        return name in self._signatures

    def __len__(self) -> int:
        return len(self._signatures)


class PromptTamperError(RuntimeError):
    """Raised by the agent loop when a verification check fails.

    Surfaces as `outcome=tool_failure` in the trace with decision_reason
    'prompt_signature_mismatch:{name}' so the failure is loud + auditable.
    """


# Per-process default — module init is import-time light, just allocates a
# dataclass + reads one env var. Heavy work (signing the actual prompts)
# happens in the agent module on its own import.
_DEFAULT_SIGNER: PromptSigner | None = None


def default_signer() -> PromptSigner:
    global _DEFAULT_SIGNER
    if _DEFAULT_SIGNER is None:
        _DEFAULT_SIGNER = PromptSigner.with_key()
    return _DEFAULT_SIGNER


def reset_default_signer_for_tests() -> None:
    global _DEFAULT_SIGNER
    _DEFAULT_SIGNER = None
