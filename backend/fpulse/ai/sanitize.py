"""
Data minimization gateway — the single chokepoint for everything sent to an LLM.

Implements the contract in docs/ai-boundary-contract.md §3 (universal denylist),
§6 (data minimization), and the redaction rules in §2.

  sanitize_for_llm(payload, *, tool_name=None, allowed_fields=None,
                   workspace_pii_patterns=None, max_chars=None)

Order of operations (matches ai-boundary-contract.md §6):
  1. Field allowlist  — drop any field not in allowed_fields (when given)
  2. Field denylist   — drop any field whose name matches the universal denylist
  3. Size cap         — truncate to max_chars (rough proxy for token cap)
  4. PII redaction    — apply universal patterns + workspace-configured patterns

Returns SanitizeResult so callers can record redaction counts in the trace
(per replay-safe trace shape — counts only, never values).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Universal denylist — field names that NEVER reach the LLM
# ---------------------------------------------------------------------------

DENY_FIELD_PATTERN = re.compile(
    r"(?i)(password|secret|token|api_key|apikey|private_key|signing_secret|"
    r"client_secret|access_key|refresh_token|bearer|credential)"
)

# ---------------------------------------------------------------------------
# Universal redaction patterns
# ---------------------------------------------------------------------------

REDACTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Email
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # Aadhaar — 12 digits with optional spaces (must come before generic phone)
    ("aadhaar", re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")),
    # US SSN
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Credit card (rough — 13-19 digit run; will over-match, that's intentional)
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    # Phone (international + national variants)
    ("phone", re.compile(r"\b\+?\d{1,3}[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")),
    # IPv4
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    # API key heuristic — 32+ chars of base64/hex
    ("api_key", re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")),
]

# Conservative default cap. Real cap comes from budget.py; this is a safety
# net so a sanitize-only call still bounds output.
DEFAULT_MAX_CHARS = 32_000


@dataclass
class SanitizeResult:
    """Result of sanitize_for_llm.

    .payload is the cleaned value, safe to send to the LLM.
    .redactions records {category: count} for the trace — never raw values.
    .truncated is True if size cap was hit.
    .dropped_fields lists field names removed by allowlist/denylist.
    """

    payload: Any
    redactions: dict[str, int] = field(default_factory=dict)
    truncated: bool = False
    dropped_fields: list[str] = field(default_factory=list)


def _redact_string(text: str, extra_patterns: list[re.Pattern[str]]) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    out = text
    for category, pat in REDACTION_PATTERNS:
        out, n = pat.subn(f"[REDACTED:{category.upper()}]", out)
        if n:
            counts[category] = counts.get(category, 0) + n
    for pat in extra_patterns:
        out, n = pat.subn("[REDACTED:WORKSPACE]", out)
        if n:
            counts["workspace"] = counts.get("workspace", 0) + n
    return out, counts


def _walk(
    value: Any,
    extra_patterns: list[re.Pattern[str]],
    counts: dict[str, int],
    dropped: list[str],
    allowed_fields: set[str] | None,
    *,
    is_top_level: bool,
) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            # Allowlist: applies only at top level (so we don't strip nested
            # column-name keys etc.)
            if is_top_level and allowed_fields is not None and k not in allowed_fields:
                dropped.append(k)
                continue
            # Denylist: applies at every nesting level
            if DENY_FIELD_PATTERN.search(k):
                dropped.append(k)
                continue
            out[k] = _walk(v, extra_patterns, counts, dropped, allowed_fields, is_top_level=False)
        return out
    if isinstance(value, list):
        return [_walk(v, extra_patterns, counts, dropped, allowed_fields, is_top_level=False) for v in value]
    if isinstance(value, str):
        redacted, sub_counts = _redact_string(value, extra_patterns)
        for cat, n in sub_counts.items():
            counts[cat] = counts.get(cat, 0) + n
        return redacted
    return value


def sanitize_for_llm(
    payload: Any,
    *,
    tool_name: str | None = None,
    allowed_fields: set[str] | None = None,
    workspace_pii_patterns: list[str] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> SanitizeResult:
    """Sanitize payload for sending to an LLM. See module docstring for order.

    The ``tool_name`` argument is reserved for tool-registry-driven allowlist
    lookup (Step 1.5a). Today, callers pass ``allowed_fields`` directly.
    """
    extra_patterns = [re.compile(p) for p in (workspace_pii_patterns or [])]
    counts: dict[str, int] = {}
    dropped: list[str] = []

    cleaned = _walk(
        payload, extra_patterns, counts, dropped, allowed_fields,
        is_top_level=True,
    )

    truncated = False
    text = repr(cleaned) if not isinstance(cleaned, str) else cleaned
    if len(text) > max_chars:
        truncated = True
        if isinstance(cleaned, str):
            cleaned = cleaned[:max_chars] + f"\n[truncated, {len(text) - max_chars} more chars]"
        elif isinstance(cleaned, list):
            keep = max(1, len(cleaned) * max_chars // max(1, len(text)))
            removed = len(cleaned) - keep
            cleaned = cleaned[:keep] + ([f"[truncated, {removed} more items]"] if removed else [])
        # dicts: leave structure; the LLM-side budget enforcement will take over

    return SanitizeResult(
        payload=cleaned,
        redactions=counts,
        truncated=truncated,
        dropped_fields=dropped,
    )
