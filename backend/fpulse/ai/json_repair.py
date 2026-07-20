"""Tolerant JSON parser for local-model tool-call arguments.

Local models at the 2026-05-19 tool-use floor (qwen2.5:7b, llama3.1:8b,
phi-4) emit tool arguments that round-trip through ``json.loads`` most
of the time, but a 1-2% slice produces output that's *almost* JSON:
trailing commas inside an object, an unquoted Python ``True`` / ``None``,
a stray ``\\n`` inside a string, fences like ``` ```json …``` ``` wrapped
around the payload, or single quotes everywhere. Sub-floor models
(qwen2.5:1.5b/3b) fail far more aggressively — they're handled by the
banner + agent loop, not by this repairer.

``parse_tolerant`` is a strict-first, then-repair-and-retry helper:

    1. Try ``json.loads`` straight. If that works, return it (fast path).
    2. Otherwise run a series of conservative repairs (strip fences,
       drop trailing commas before ``}`` / ``]``, replace Python
       literals, escape lone control characters, swap single → double
       quotes in obvious cases).
    3. Re-parse. If repair succeeds, return it with ``repaired=True``.
    4. If repair fails, return ``({}, repaired=False, error="…")``.

The agent loop calls this instead of bare ``json.loads`` on every
provider's tool_call ``arguments`` payload so a single misplaced comma
doesn't turn into a silent ``{}`` (which the model then interprets as
"the user supplied no arguments" and asks them to repeat themselves).

This module is pure-Python, dependency-free, and safe to import at
module load. No I/O, no logging side-effects.

Threat model:
- We *only* operate on text the agent already accepted from the LLM.
- Repairs are syntactic, not semantic — we never invent keys or values.
- If repair fails, we return an empty dict and a reason string; the
  caller decides whether to retry the LLM, surface to the user, or
  proceed with empty args.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


# Fence patterns: ```json … ```  or  ``` … ```  (with optional language tag).
_FENCE_RE = re.compile(
    r"^```(?:json|JSON)?\s*(.*?)\s*```$",
    flags=re.DOTALL,
)

# Trailing comma before a closing brace/bracket: ``{"a": 1,}`` → ``{"a": 1}``.
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")

# Unescaped control characters inside what looks like a JSON string. We
# match raw \n / \r / \t that appear *between* quote characters and replace
# them with their escaped form. Conservative — we don't try to repair
# nested-string edge cases; just the common "newline inside a value" issue.
_CONTROL_IN_STRING_RE = re.compile(r'("(?:[^"\\]|\\.)*?)([\n\r\t])')


@dataclass(frozen=True)
class RepairResult:
    """Outcome of one ``parse_tolerant`` call."""
    value: Any
    repaired: bool
    error: str | None = None


def _strip_fence(text: str) -> str:
    """If the payload is wrapped in a markdown code fence, return the
    inner content. Otherwise return ``text`` unchanged."""
    s = text.strip()
    m = _FENCE_RE.match(s)
    if m:
        return m.group(1).strip()
    return s


def _drop_trailing_commas(text: str) -> str:
    """``{"a": 1,}`` → ``{"a": 1}``. Idempotent."""
    prev = None
    out = text
    while prev != out:
        prev = out
        out = _TRAILING_COMMA_RE.sub(r"\1", out)
    return out


def _replace_python_literals(text: str) -> str:
    """``True`` / ``False`` / ``None`` → ``true`` / ``false`` / ``null``.

    Word-boundary anchored so identifiers like ``None_id`` or ``Truthy``
    aren't rewritten. Strings containing the literal words are left
    alone too — we only touch tokens that sit outside quoted runs by
    operating on a tokeniser-aware regex.
    """
    # Quick rejection: if the text contains none of the three tokens at
    # all, skip the more expensive scan.
    if not any(tok in text for tok in ("True", "False", "None")):
        return text

    def _rewrite_outside_strings(s: str) -> str:
        out: list[str] = []
        i = 0
        n = len(s)
        in_string: str | None = None  # the opening quote character, or None
        while i < n:
            ch = s[i]
            # String handling — copy through until the matching close,
            # respecting escape sequences.
            if in_string:
                out.append(ch)
                if ch == "\\" and i + 1 < n:
                    out.append(s[i + 1])
                    i += 2
                    continue
                if ch == in_string:
                    in_string = None
                i += 1
                continue
            if ch in ('"', "'"):
                in_string = ch
                out.append(ch)
                i += 1
                continue
            # Outside any string — try to match one of the three literals
            # at a word boundary.
            for src, dst in (("True", "true"), ("False", "false"), ("None", "null")):
                end = i + len(src)
                if (
                    s[i:end] == src
                    and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] == "_"))
                    and (end == n or not (s[end].isalnum() or s[end] == "_"))
                ):
                    out.append(dst)
                    i = end
                    break
            else:
                out.append(ch)
                i += 1
        return "".join(out)

    return _rewrite_outside_strings(text)


def _escape_control_chars_in_strings(text: str) -> str:
    """Replace raw newline/CR/tab inside a JSON string with their escaped
    form. We loop because each substitution shortens the match window."""
    prev = None
    out = text
    while prev != out:
        prev = out
        out = _CONTROL_IN_STRING_RE.sub(
            lambda m: m.group(1) + {"\n": "\\n", "\r": "\\r", "\t": "\\t"}[m.group(2)],
            out,
        )
    return out


def _single_to_double_quotes(text: str) -> str:
    """Convert obvious single-quoted JSON to double-quoted.

    Conservative: we only convert when the entire payload uses single
    quotes consistently (no embedded ``"`` characters that would break).
    Tools that emit Python ``repr``-style dicts hit this path.
    """
    if '"' in text:
        return text  # mixed quoting — refuse to touch it
    if "'" not in text:
        return text
    # Swap '...' for "..." but leave escaped \' intact.
    return re.sub(r"(?<!\\)'", '"', text)


def _apply_repairs(text: str) -> str:
    """Run the repair pipeline. Each step is idempotent on already-valid
    JSON, so applying them in series doesn't break the strict-first
    path's payloads."""
    t = _strip_fence(text)
    t = _single_to_double_quotes(t)
    t = _replace_python_literals(t)
    t = _drop_trailing_commas(t)
    t = _escape_control_chars_in_strings(t)
    return t


def parse_tolerant(payload: Any) -> RepairResult:
    """Parse ``payload`` as JSON, repairing common small-model defects.

    Accepts ``str``, ``bytes``, or already-decoded ``dict`` / ``list``
    (passthrough — small-model providers sometimes hand us a real dict).
    Returns a ``RepairResult`` carrying the parsed value, whether any
    repair was needed, and (if parsing failed) the reason.

    Never raises. Callers can rely on ``result.value`` being usable
    (``{}`` if parsing failed entirely).
    """
    # Already parsed — passthrough.
    if isinstance(payload, (dict, list)):
        return RepairResult(value=payload, repaired=False)

    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8", errors="replace")
        except Exception:
            return RepairResult(value={}, repaired=False, error="bytes-decode-failed")

    if not isinstance(payload, str):
        return RepairResult(value={}, repaired=False, error=f"unsupported-type:{type(payload).__name__}")

    text = payload.strip()
    if not text:
        return RepairResult(value={}, repaired=False, error="empty")

    # Fast path: strict JSON parse.
    try:
        return RepairResult(value=json.loads(text), repaired=False)
    except json.JSONDecodeError:
        pass

    # Slow path: apply repairs, retry.
    repaired_text = _apply_repairs(text)
    try:
        return RepairResult(value=json.loads(repaired_text), repaired=True)
    except json.JSONDecodeError as exc:
        # Last-ditch attempt: trim everything past the matching outer brace.
        # Models sometimes append commentary after a valid object.
        trimmed = _trim_to_outer_object(repaired_text)
        if trimmed and trimmed != repaired_text:
            try:
                return RepairResult(value=json.loads(trimmed), repaired=True)
            except json.JSONDecodeError:
                pass
        return RepairResult(
            value={},
            repaired=False,
            error=f"json-decode: {str(exc)[:80]}",
        )


def _trim_to_outer_object(text: str) -> str | None:
    """If ``text`` starts with ``{`` or ``[``, return the substring up to
    and including the matching close bracket. Used to chop trailing
    chatter the model may have appended after the JSON payload.

    Returns ``None`` if no matching bracket is found.
    """
    if not text:
        return None
    if text[0] not in "{[":
        return None
    open_ch = text[0]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_string: str | None = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue
        if ch in ('"', "'"):
            in_string = ch
            i += 1
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[: i + 1]
        i += 1
    return None
