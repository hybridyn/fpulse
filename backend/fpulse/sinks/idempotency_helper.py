"""
Per-row idempotency helpers shared by every external sink.

Why these live separately from ``dedupe_store.py``
──────────────────────────────────────────────────
The store is a *storage* concern (open the SQLite handle, write a row,
read a row). These helpers are the *policy* layer that every sink
plugs into:

  1. Render a per-row key from the sink-configured template
     (``{col_name}`` substitution against the row dict).
  2. Hash that key to a stable, fixed-width SHA-256 hex digest so the
     storage column doesn't have to accommodate arbitrary user text.
  3. Ask the store whether this hash has been seen for this pipeline +
     sink step.

Keeping the policy in its own module means:
  - Every sink class uses the *same* hashing rules, so a key that
    deduplicates an email sink would also deduplicate a webhook sink
    if both were configured with the same template. (Same hash → same
    semantic identity, regardless of which sink type fired.)
  - The hashing rules are unit-testable in isolation without spinning
    up the DB-backed store.
  - The sink classes stay thin — three method calls per row, no
    inline hash logic, no inline error handling.

Public surface
──────────────
    compute_row_hash(row, key_expression) → str  (sha256 hex)
    should_skip(pipeline_id, sink_step_id, row,
                key_expression, dedupe_store) → (skip: bool, hash: str)
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Matches ``{column_name}`` placeholders in the key template. Column
# names are restricted to identifier-like characters (letters, digits,
# underscores, plus dot and dash for namespaced columns). A column
# that doesn't match this regex can't be referenced from a key
# template — that's intentional: keys built from arbitrary punctuation
# would silently mis-substitute on common JSON column names.
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.\-]*)\}")


def compute_row_hash(row: dict, key_expression: str) -> str:
    """Render ``key_expression`` against ``row`` and SHA-256 the result.

    The expression supports ``{col_name}`` substitution against the
    row dict. Missing columns substitute as the empty string — this
    is intentional so a sink with a template like
    ``{user_id}|{event_type}`` produces a stable hash even when one
    column is NULL (a NULL becomes empty, which is still
    deterministic across runs).

    Stability guarantees:
      * Same row + same expression → same hash, across processes and
        Python versions (sha256 is stable; the rendered string is
        deterministic from the row dict because we render via the
        regex's findall order, not dict iteration order).
      * Different value in any substituted column → different hash.
      * Different expression → different hash even with same row.

    Returns "" when ``key_expression`` is empty — this lets the
    caller skip the hash + lookup entirely without checking the
    template up-front. An empty hash NEVER matches any stored row.

    Type coercion: values are stringified with ``str(value)`` rather
    than ``repr`` so int-vs-string variants of the same id collide
    correctly (``1`` and ``"1"`` both render as ``"1"``). NULL/None
    becomes ``""`` rather than ``"None"`` so it lines up with the
    "missing column → empty string" rule.
    """
    if not key_expression:
        return ""

    rendered = _render(row, key_expression)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def should_skip(
    pipeline_id: str,
    sink_step_id: str,
    row: dict,
    key_expression: str,
    dedupe_store,
) -> tuple[bool, str]:
    """Decide whether to skip a row's side effect; return (skip, hash).

    Behaviour matrix:
      * ``key_expression`` empty → returns ``(False, "")`` immediately.
        The caller fires the side effect; no dedup record is written.
        This is the back-compat path for sinks that don't set the key.
      * ``key_expression`` set but the dedup store is unwired → the
        store's ``seen()`` returns False defensively, so this returns
        ``(False, <hash>)``. The caller fires the side effect, then
        the matching ``record()`` is a no-op — the next run sees no
        marker and may duplicate. This is the load-bearing tradeoff:
        a missing dedup store must never block sends.
      * Row already seen (within TTL) → ``(True, <hash>)``. The caller
        skips the side effect.
      * Row not yet seen (or TTL expired) → ``(False, <hash>)``. The
        caller fires the side effect, then is expected to call
        ``dedupe_store.record(pipeline_id, sink_step_id, hash, ttl)``.

    The hash is returned even when ``skip=False`` so the caller doesn't
    have to recompute it before recording — the helper does the hash
    once and hands it back.
    """
    key_hash = compute_row_hash(row, key_expression)
    if not key_hash:
        return False, ""

    if dedupe_store is None:
        # Same safety-first stance as the store's own missing-db case.
        return False, key_hash

    try:
        if dedupe_store.seen(pipeline_id, sink_step_id, key_hash):
            return True, key_hash
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "should_skip: dedup store seen() raised; firing sink to be safe "
            "(pipeline=%s sink=%s): %s",
            pipeline_id, sink_step_id, exc,
        )
        return False, key_hash

    return False, key_hash


# ── Internal ──────────────────────────────────────────────────────────


def _render(row: dict, expression: str) -> str:
    """Substitute ``{col}`` placeholders in ``expression`` from ``row``.

    Uses a single regex pass rather than per-key ``str.replace`` so the
    output is well-defined when a value contains another column name's
    placeholder (a string from one column never gets re-substituted by
    a later pass).
    """
    def _sub(match: re.Match) -> str:
        key = match.group(1)
        value = row.get(key)
        if value is None:
            return ""
        return str(value)

    return _PLACEHOLDER_RE.sub(_sub, expression)
