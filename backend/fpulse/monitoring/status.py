"""Execution status normalization.

2026-05-22 (audit J1) — the dashboard audit caught the
status-naming drift between surfaces:

  * ``ExecutionStore.get_stats()`` counts only ``status == "error"``
    as a failure
  * ``/api/monitor/failed`` likewise only looks for ``"error"``
  * the frontend Dashboard sometimes checks both ``"error"`` and
    ``"failed"``
  * pipeline-health treats ``"timeout"`` and ``"cancelled"`` as
    failures
  * different writers across the codebase use ``"failed"``,
    ``"error"``, ``"timeout"``, ``"cancelled"``, ``"canceled"``,
    ``"skipped"`` interchangeably

This module is the canonical bridge. Every count / filter that
reasons about "did the run succeed" goes through ``normalize_status``
so the answer is the same regardless of which writer originally
wrote the row.

Categories:

  * ``success``   — the run completed without error
  * ``failed``    — the run completed in a non-success state
                    (error, failed, timeout)
  * ``running``   — the run is currently executing
  * ``queued``    — the run is scheduled / waiting for a worker
  * ``cancelled`` — the run was halted by user action (cancel /
                    cancelled / canceled / aborted)
  * ``skipped``   — the run was intentionally skipped (deactivated
                    upstream, schedule overlap policy, etc.)
  * ``unknown``   — unrecognised status; logged at WARNING by the
                    callers so we can catch new writers.

The canonical category strings live here too so callers don't have
to spell them right repeatedly.
"""

from __future__ import annotations

from typing import Final


# ── Canonical category names ────────────────────────────────────────
STATUS_SUCCESS:   Final[str] = "success"
STATUS_FAILED:    Final[str] = "failed"
STATUS_RUNNING:   Final[str] = "running"
STATUS_QUEUED:    Final[str] = "queued"
STATUS_CANCELLED: Final[str] = "cancelled"
STATUS_SKIPPED:   Final[str] = "skipped"
STATUS_UNKNOWN:   Final[str] = "unknown"

ALL_CATEGORIES: Final[tuple[str, ...]] = (
    STATUS_SUCCESS, STATUS_FAILED, STATUS_RUNNING,
    STATUS_QUEUED, STATUS_CANCELLED, STATUS_SKIPPED,
    STATUS_UNKNOWN,
)


# Raw values written by various code paths, mapped to canonical
# categories. Add new aliases here rather than in callers.
_RAW_TO_CATEGORY: Final[dict[str, str]] = {
    # success
    "success":   STATUS_SUCCESS,
    "ok":        STATUS_SUCCESS,
    "completed": STATUS_SUCCESS,
    "passed":    STATUS_SUCCESS,
    # failure (audit J3): treat error, failed, timeout as failures.
    # Operators reading the dashboard care about "did anything not
    # finish cleanly" — splitting timeout out separately just hides
    # incidents inside an "other" bucket.
    "error":          STATUS_FAILED,
    "failed":         STATUS_FAILED,
    "failure":        STATUS_FAILED,
    "timeout":        STATUS_FAILED,
    "timed_out":      STATUS_FAILED,
    # running
    "running":    STATUS_RUNNING,
    "in_progress": STATUS_RUNNING,
    "executing":  STATUS_RUNNING,
    # queued
    "queued":     STATUS_QUEUED,
    "pending":    STATUS_QUEUED,
    "scheduled":  STATUS_QUEUED,
    "waiting":    STATUS_QUEUED,
    # cancelled — both spellings exist in the codebase
    "cancelled":  STATUS_CANCELLED,
    "canceled":   STATUS_CANCELLED,
    "aborted":    STATUS_CANCELLED,
    "stopped":    STATUS_CANCELLED,
    # skipped — pipeline runs blocked by upstream deactivation,
    # overlap-policy=skip, etc.
    "skipped":    STATUS_SKIPPED,
    "deactivated": STATUS_SKIPPED,
    "shadowed":   STATUS_SKIPPED,
}


def normalize_status(raw: str | None) -> str:
    """Return the canonical category for a raw status string.

    Unknown / empty / None values map to ``STATUS_UNKNOWN`` so
    callers always get a string and never accidentally count a
    ``None`` row as a failure.

    Case-insensitive; leading/trailing whitespace is stripped.
    """
    if not raw:
        return STATUS_UNKNOWN
    key = str(raw).strip().lower()
    return _RAW_TO_CATEGORY.get(key, STATUS_UNKNOWN)


def is_failed(raw: str | None) -> bool:
    """Convenience predicate. True iff the row counts as a failure."""
    return normalize_status(raw) == STATUS_FAILED


def is_success(raw: str | None) -> bool:
    """Convenience predicate. True iff the row counts as a success."""
    return normalize_status(raw) == STATUS_SUCCESS


def is_terminal(raw: str | None) -> bool:
    """True iff the run has finished (success / failed / cancelled /
    skipped). Used by counters that exclude in-flight rows from
    success-rate denominators.
    """
    return normalize_status(raw) in (
        STATUS_SUCCESS, STATUS_FAILED, STATUS_CANCELLED, STATUS_SKIPPED,
    )


__all__ = [
    "ALL_CATEGORIES",
    "STATUS_CANCELLED",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_SKIPPED",
    "STATUS_SUCCESS",
    "STATUS_UNKNOWN",
    "is_failed",
    "is_success",
    "is_terminal",
    "normalize_status",
]
