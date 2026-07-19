"""
Unique-name helper — May 6 2026.

User-created entities (pipelines, connections, projects) should not
share names within a workspace. The pattern across F-Pulse stores is:

    1. caller sends a desired name (or accepts the default)
    2. we list existing names of the same kind in the same workspace
    3. ``ensure_unique_name`` returns either the original (if free) or
       a numbered variant: ``"My Pipeline"`` → ``"My Pipeline (2)"``

Same convention as Google Docs / Notion / Figma: append ``(N)`` rather
than rejecting the request. Less friction; users can rename later.

Why a shared helper instead of inline logic per endpoint:
  * One place to tune the suffix style if we change our mind.
  * Reusable from the bot's "create pipeline" flow without re-implementing.
  * Easy to test in isolation.
"""

from __future__ import annotations

import re
from typing import Iterable

# Strip a trailing ``(N)`` so we don't produce ``"foo (2) (2)"`` when
# the caller hands us a name that already has a suffix.
_SUFFIX_RE = re.compile(r"\s*\((\d+)\)\s*$")


def _strip_suffix(name: str) -> str:
    """Return the name with any trailing ``(N)`` removed."""
    return _SUFFIX_RE.sub("", name).rstrip()


def ensure_unique_name(desired: str, existing: Iterable[str]) -> str:
    """Return a name guaranteed unique within ``existing``.

    Args:
        desired: the name the user (or default) wants to use.
        existing: any iterable of names already taken in the same scope.

    Returns:
        ``desired`` if it isn't taken. Otherwise the lowest-numbered
        ``"<base> (N)"`` variant that is free, where ``<base>`` is
        ``desired`` with any existing ``(N)`` suffix stripped.
    """
    desired = (desired or "").strip()
    if not desired:
        # Empty / whitespace — fall back to a generic placeholder so the
        # downstream model never sees an empty name.
        desired = "Untitled"
    taken = set()
    for name in existing:
        if name:
            taken.add(name.strip())
    if desired not in taken:
        return desired
    base = _strip_suffix(desired) or desired
    n = 2
    while f"{base} ({n})" in taken:
        n += 1
        if n > 9999:  # paranoid bound
            return f"{base} ({n})"
    return f"{base} ({n})"
