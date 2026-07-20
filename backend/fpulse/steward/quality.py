"""F-Pulse Steward - native data-quality check recorder (2026-06-07).

Event-driven, same shape as schema_drift.py. An external runner
(F-Pulse executor, dbt test, Great Expectations checkpoint, Soda scan,
or a hand-rolled probe) evaluates an assertion against a dataset and
posts the result to ``POST /api/steward/quality-check``. We do NOT
evaluate the assertion - F-Pulse isn't a DQ runner; it's the place
those runners report into so failures land in the same surface as
duplicate / schema-drift / connector-health findings.

# Why event-driven (and not in-process evaluation)

Several reasons it's a much better fit than "Steward reads the data
itself":

  * **No outbound side effects on production sources.** Reading a
    Snowflake table to count nulls is not free; we don't get to do it
    silently on someone else's behalf.
  * **dbt / GX / Soda already exist.** Users running those should be
    able to pipe results into Steward without rewriting their checks.
  * **Same alert-fatigue guarantees.** A failed assertion becomes a
    standard StewardFinding, which means it picks up time-clamped
    escalation, de-dup, rebound detection, dismiss-with-reason for
    free.
  * **Read-only Rule 1 stays intact.** Steward observes assertion
    outcomes; it never executes them.

# Supported assertion types

Each posted assertion has a ``check`` discriminator that maps to the
right FindingKind:

| check name           | FindingKind            | Why                                       |
|----------------------|------------------------|-------------------------------------------|
| not_null             | NULL_SPIKE             | The specific case of null violations      |
| unique               | DUPLICATE_KEY_SPIKE    | "Unique column has duplicates"            |
| duplicate_key        | DUPLICATE_KEY_SPIKE    | Same kind, multi-column-key variant       |
| row_count_min        | VOLUME_ANOMALY         | "Fewer rows than expected"                |
| row_count_max        | VOLUME_ANOMALY         | "More rows than expected"                 |
| freshness            | FRESHNESS_MISS         | "Data hasn't updated within window"       |
| partition_missing    | PARTITION_MISSING      | "Expected partition not present"          |
| accepted_values      | QUALITY_CHECK_FAILED   | "Value outside allowed set"               |
| range                | QUALITY_CHECK_FAILED   | "Value outside numeric range"             |
| regex                | QUALITY_CHECK_FAILED   | "Value doesn't match pattern"             |
| referential_integrity| QUALITY_CHECK_FAILED   | "FK doesn't resolve to parent"            |
| custom               | QUALITY_CHECK_FAILED   | Anything else; caller controls the message|

Severity is derived from the check (integrity violations P1, others
P2 default; the caller can override).

# Storage

  * ``<workspace>/quality_findings.jsonl`` - append-only journal of
    every emitted finding. The scan path reads this and surfaces
    open findings (filtered by suppression) on every /findings call.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field, field_validator

from .models import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    FindingStatus,
    StewardFinding,
)


_FILE_LOCK = threading.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── DSL ──────────────────────────────────────────────────────────────


_INTEGRITY_CHECKS = {"not_null", "unique", "duplicate_key", "referential_integrity"}
_VALID_CHECKS = _INTEGRITY_CHECKS | {
    "row_count_min", "row_count_max", "freshness", "partition_missing",
    "accepted_values", "range", "regex", "custom",
}


class QualityAssertion(BaseModel):
    """One assertion the external runner evaluated. failed_count > 0
    means the assertion FAILED - that's what produces a finding."""

    check: str
    column: str = ""
    failed_count: int = 0
    total_rows: int = 0
    message: str = ""

    @field_validator("check")
    @classmethod
    def _valid_check(cls, v: str) -> str:
        if v not in _VALID_CHECKS:
            raise ValueError(
                f"check must be one of {sorted(_VALID_CHECKS)}, got {v!r}"
            )
        return v


class QualityCheckReport(BaseModel):
    """The full payload one runner sends on one source after one run."""

    source_signature: str
    source_label: str = ""
    run_id: str = ""
    assertions: list[QualityAssertion]


# ── Severity / kind mapping ──────────────────────────────────────────


_KIND_BY_CHECK: dict[str, FindingKind] = {
    "not_null":              FindingKind.NULL_SPIKE,
    "unique":                FindingKind.DUPLICATE_KEY_SPIKE,
    "duplicate_key":         FindingKind.DUPLICATE_KEY_SPIKE,
    "row_count_min":         FindingKind.VOLUME_ANOMALY,
    "row_count_max":         FindingKind.VOLUME_ANOMALY,
    "freshness":             FindingKind.FRESHNESS_MISS,
    "partition_missing":     FindingKind.PARTITION_MISSING,
    "accepted_values":       FindingKind.QUALITY_CHECK_FAILED,
    "range":                 FindingKind.QUALITY_CHECK_FAILED,
    "regex":                 FindingKind.QUALITY_CHECK_FAILED,
    "referential_integrity": FindingKind.QUALITY_CHECK_FAILED,
    "custom":                FindingKind.QUALITY_CHECK_FAILED,
}


def _severity_for_check(check: str, failed_count: int, total_rows: int) -> FindingSeverity:
    """Integrity checks (not_null / unique / duplicate_key / ref_int)
    are P1 when ANY row violated - those break downstream guarantees
    other code depends on. Non-integrity checks default to P2; if the
    failure rate is >50% of total_rows they escalate to P1 (something
    is structurally wrong)."""
    if check in _INTEGRITY_CHECKS:
        return FindingSeverity.P1 if failed_count > 0 else FindingSeverity.P3
    if total_rows > 0 and failed_count * 2 > total_rows:
        return FindingSeverity.P1
    return FindingSeverity.P2


# ── Finding journal ─────────────────────────────────────────────────


class QualityFindingStore:
    """Append-only JSONL of every quality finding ever emitted. Same
    pattern as SchemaDriftFindingStore - findings persist across scans
    because the underlying state (the assertion result) is a point-in-
    time event that wouldn't otherwise be re-derivable."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, finding: StewardFinding) -> None:
        with _FILE_LOCK:
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(finding.model_dump_json() + "\n")

    def all(self) -> list[StewardFinding]:
        if not self.path.exists():
            return []
        out: list[StewardFinding] = []
        try:
            with self.path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(StewardFinding.model_validate_json(line))
                    except Exception:
                        continue
        except Exception:
            return []
        return out

    def open_findings(self, suppressed_signatures: set[str] | None = None) -> list[StewardFinding]:
        suppressed = suppressed_signatures or set()
        by_id: dict[str, StewardFinding] = {}
        for f in self.all():
            by_id[f.id] = f  # later wins (re-record updates)
        return [
            f for f in by_id.values()
            if f.evidence.get("source_signature") not in suppressed
            and f.status == FindingStatus.OPEN
        ]


# ── Recorder (single entry point) ───────────────────────────────────


def _finding_id(source_signature: str, check: str, column: str) -> str:
    """Deterministic per (source, check, column) - re-running the same
    assertion against the same source produces the same finding id so
    repeat failures collapse rather than spam."""
    raw = f"qc::{source_signature}::{check}::{column}"
    return f"qc-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _signature(source_signature: str, check: str, column: str) -> str:
    raw = f"qc::{source_signature}::{check}::{column}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _render_title(check: str, column: str, source_label: str,
                   failed_count: int, total_rows: int) -> str:
    label = source_label or "source"
    where = f"`{column}`" if column else label
    if check == "not_null":
        return f"Null values in {where} ({failed_count} of {total_rows})"
    if check in ("unique", "duplicate_key"):
        return f"Duplicate values in {where} ({failed_count} of {total_rows})"
    if check == "row_count_min":
        return f"Row count below minimum: {failed_count} rows in {label}"
    if check == "row_count_max":
        return f"Row count above maximum: {failed_count} rows in {label}"
    if check == "freshness":
        return f"Freshness miss on {label}"
    if check == "partition_missing":
        return f"Expected partition missing in {label}"
    if check == "accepted_values":
        return f"Out-of-set values in {where} ({failed_count} of {total_rows})"
    if check == "range":
        return f"Out-of-range values in {where} ({failed_count} of {total_rows})"
    if check == "regex":
        return f"Regex mismatch in {where} ({failed_count} of {total_rows})"
    if check == "referential_integrity":
        return f"Referential integrity broken on {where} ({failed_count} orphans)"
    return f"Quality check failed: {check} on {where}"


def record_quality_report(
    store: QualityFindingStore,
    report: QualityCheckReport,
    *,
    workspace_id: str = "default",
) -> list[StewardFinding]:
    """Persist any failed assertions as StewardFindings. Returns the
    list of findings emitted (empty if every assertion passed)."""
    out: list[StewardFinding] = []
    now = _iso_now()
    for assertion in report.assertions:
        if assertion.failed_count <= 0:
            continue
        kind = _KIND_BY_CHECK.get(assertion.check, FindingKind.QUALITY_CHECK_FAILED)
        sig = _signature(report.source_signature, assertion.check, assertion.column)
        fid = _finding_id(report.source_signature, assertion.check, assertion.column)
        severity = _severity_for_check(assertion.check, assertion.failed_count, assertion.total_rows)
        title = _render_title(assertion.check, assertion.column,
                                report.source_label or report.source_signature[:12],
                                assertion.failed_count, assertion.total_rows)
        body_lines = [f"**Check:** `{assertion.check}`"]
        if assertion.column:
            body_lines.append(f"**Column:** `{assertion.column}`")
        body_lines.append(f"**Failed rows:** {assertion.failed_count}"
                          + (f" of {assertion.total_rows}" if assertion.total_rows else ""))
        if assertion.message:
            body_lines.append("")
            body_lines.append(assertion.message)
        body_lines.append("")
        body_lines.append(
            "If this failure is expected (e.g. legacy dataset with known holes), "
            "dismiss the finding — the signature will stay suppressed for future runs."
        )
        finding = StewardFinding(
            id=fid,
            workspace_id=workspace_id,
            kind=kind,
            level=FindingLevel.DATA,
            severity=severity,
            status=FindingStatus.OPEN,
            title=title,
            body="\n".join(body_lines),
            evidence={
                "source_signature": sig,  # NOTE: signature is per (source, check, column)
                "underlying_source_signature": report.source_signature,
                "source_label": report.source_label,
                "check": assertion.check,
                "column": assertion.column,
                "failed_count": assertion.failed_count,
                "total_rows": assertion.total_rows,
                "run_id": report.run_id,
                "message": assertion.message,
            },
            proposed_actions=[
                {
                    "label": "Dismiss (acceptable failure for this dataset)",
                    "action": "suppress_finding",
                    "params": {"finding_id": fid, "scope": "signature"},
                },
            ],
            first_seen=now,
            last_seen=now,
            occurrences=1,
            confidence="high",
            confidence_score=1.0,
            evidence_count=assertion.failed_count,
            baseline_window="single_assertion",
        )
        store.append(finding)
        out.append(finding)
    return out


def detect_quality_findings(
    store: QualityFindingStore,
    *,
    workspace_id: str = "default",
    suppressed_signatures: set[str] | None = None,
) -> list[StewardFinding]:
    """Surface open quality findings from the journal. Called by
    _run_scan; mirrors detect_schema_drift's read-side."""
    return store.open_findings(suppressed_signatures or set())
