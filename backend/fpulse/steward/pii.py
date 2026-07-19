"""F-Pulse Steward - PII-leak detector (2026-06-08).

Activates the third of four governance-level FindingKinds:
PII_LEAK. Schema-name-based heuristic: when a column name matches a
curated PII pattern, surface it so operators see what's flowing
through their pipelines.

# Design choice: name-based, not value-based

A real PII detector that inspects column VALUES has two problems:
  1. It needs to read sample rows from every source - that's an
     outbound side effect we deliberately don't take (read-only
     Rule 1 still holds).
  2. False-positive risk is high when reading unstructured text
     (e.g. CSV with free-text columns containing emails by accident).

Name-based detection is conservative on both:
  - We never touch the data; we look at column names that already
    flow through Schema-snapshot recording.
  - Patterns are explicit and reviewable. Adding a pattern is a
    visible code change with a regression test.

The trade-off: a column called "user_data" might contain PII we don't
catch. The 1.2 PII Sampler (event-driven, opt-in, samples by user
consent only) closes that gap. For 1.1, schema-name catches the
high-volume cases (email / ssn / phone / address / dob).

# Pattern catalog

The catalog below is intentionally narrow - high-precision rather
than high-recall. Each pattern is paired with the PII class it
represents so the finding body can be specific ("3 email columns
detected" beats "PII columns detected").

# Lifecycle

Rides the existing schema-snapshot recording path. When
`record_snapshot()` in schema_drift.py succeeds, the API endpoint
also calls `record_pii_findings()` from this module with the same
snapshot. PII findings live in a separate journal so they survive
across rescans (same pattern as quality / cost / schema-drift).
"""
from __future__ import annotations

import hashlib
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from .models import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    FindingStatus,
    StewardFinding,
)
from .schema_drift import SchemaSnapshot


_FILE_LOCK = threading.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Catalog ─────────────────────────────────────────────────────────


# Each entry: (compiled regex on lower-cased column name, PII class label).
# Patterns are word-boundary-aware where helpful to avoid matching
# "email_subject" as an email column.
_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Email
    (re.compile(r"(?:^|_)(email|email_addr|email_address|e_mail)(?:$|_)"), "email"),
    # Social-security / national-ID
    (re.compile(r"(?:^|_)(ssn|social_security|national_id|nin|aadhaar|aadhar)(?:$|_)"), "national_id"),
    # Phone
    (re.compile(r"(?:^|_)(phone|phone_no|phone_number|mobile|mobile_no|cell|cellphone)(?:$|_)"), "phone"),
    # Postal address
    (re.compile(r"(?:^|_)(address|street|street_address|postal_code|postcode|zip|zipcode|zip_code)(?:$|_)"), "address"),
    # Date-of-birth / age
    (re.compile(r"(?:^|_)(dob|date_of_birth|birth_date|birthday|birth_dt)(?:$|_)"), "date_of_birth"),
    # Government IDs
    (re.compile(r"(?:^|_)(passport|passport_no|passport_number|drivers_license|driver_license|license_no)(?:$|_)"), "government_id"),
    # Financial
    (re.compile(r"(?:^|_)(credit_card|card_number|cc_number|cvv|cvc|iban|swift_code)(?:$|_)"), "financial"),
    # Auth / secrets accidentally stored in columns
    (re.compile(r"(?:^|_)(password|passwd|pwd|api_key|auth_token|secret_key|access_token)(?:$|_)"), "credential_in_column"),
    # Health
    (re.compile(r"(?:^|_)(medical_record|patient_id|diagnosis|prescription|insurance_id)(?:$|_)"), "health"),
]


# Severity rules:
#   credential_in_column -> always P1 (passwords / API keys in tables is severe)
#   national_id / financial / health -> P1 (high-sensitivity PII)
#   email / phone / address / dob / government_id -> P2 (PII but more common)
_HIGH_SENSITIVITY = {
    "credential_in_column", "national_id", "financial", "health",
}


# ── Checker ─────────────────────────────────────────────────────────


def check_columns_for_pii(column_names: list[str]) -> list[tuple[str, str]]:
    """Scan column names against the catalog. Returns a list of
    ``(column_name, pii_class)`` tuples for every match. One column can
    only match one pattern (first wins) to avoid double-counting an
    "email_address" column as both email AND address."""
    hits: list[tuple[str, str]] = []
    for col_raw in column_names:
        if not col_raw or not isinstance(col_raw, str):
            continue
        col = col_raw.lower()
        for pattern, label in _PII_PATTERNS:
            if pattern.search(col):
                hits.append((col_raw, label))
                break  # first match wins
    return hits


# ── Finding journal ─────────────────────────────────────────────────


class PIIFindingStore:
    """Append-only JSONL journal of every PII finding ever emitted.
    Same shape as the other event-driven finding stores. The scan
    path filters by suppression and re-surfaces still-open findings."""

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
            by_id[f.id] = f
        return [
            f for f in by_id.values()
            if f.evidence.get("source_signature") not in suppressed
            and f.status == FindingStatus.OPEN
        ]


# ── Emit + detect ───────────────────────────────────────────────────


def _signature(source_signature: str) -> str:
    raw = f"pii::{source_signature}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _finding_id(source_signature: str) -> str:
    return f"pii-{_signature(source_signature)[:12]}"


def _severity_for_hits(hits: list[tuple[str, str]]) -> FindingSeverity:
    """P1 if any high-sensitivity class is in the hit list (passwords,
    national_id, financial, health); otherwise P2."""
    classes = {h[1] for h in hits}
    if classes & _HIGH_SENSITIVITY:
        return FindingSeverity.P1
    return FindingSeverity.P2


def record_pii_findings(
    snapshot: SchemaSnapshot,
    finding_store: PIIFindingStore,
    *,
    workspace_id: str = "default",
) -> StewardFinding | None:
    """Check the snapshot's columns for PII names. If any hit, emit
    one PII_LEAK finding (deterministic id, idempotent on re-record).

    Returns the finding or None.
    """
    column_names = [c.name for c in snapshot.columns]
    hits = check_columns_for_pii(column_names)
    if not hits:
        return None

    by_class: dict[str, list[str]] = {}
    for col, cls in hits:
        by_class.setdefault(cls, []).append(col)

    severity = _severity_for_hits(hits)
    sig = _signature(snapshot.source_signature)
    fid = _finding_id(snapshot.source_signature)
    now = _iso_now()

    # Build a human body that names the PII classes explicitly.
    class_lines = []
    for cls in sorted(by_class.keys()):
        cols = sorted(by_class[cls])
        col_str = ", ".join(f"`{c}`" for c in cols[:5])
        if len(cols) > 5:
            col_str += f" (+{len(cols) - 5} more)"
        class_lines.append(f"- **{cls.replace('_', ' ')}**: {col_str}")

    body = (
        f"Schema for **{snapshot.source_label or snapshot.source_signature[:12]}** "
        f"contains **{len(hits)} column(s)** matching known PII naming patterns:\n\n"
        + "\n".join(class_lines)
        + "\n\nThis is a **name-based heuristic** - the detector flagged columns "
        f"by their *names*, not by inspecting any actual values (read-only Rule 1). "
        f"Verify by checking the source.\n\n"
        f"If these columns legitimately need to flow through this pipeline, dismiss "
        f"the finding - the signature stays suppressed for this source."
    )

    finding = StewardFinding(
        id=fid,
        workspace_id=workspace_id,
        kind=FindingKind.PII_LEAK,
        level=FindingLevel.GOVERNANCE,
        severity=severity,
        status=FindingStatus.OPEN,
        title=f"PII columns in {snapshot.source_label or snapshot.source_signature[:12]} ({len(hits)} found)",
        body=body,
        evidence={
            "source_signature": sig,
            "underlying_source_signature": snapshot.source_signature,
            "source_label": snapshot.source_label,
            "captured_at": snapshot.captured_at,
            "pii_hits": [{"column": c, "pii_class": cls} for c, cls in hits],
            "pii_classes_present": sorted(by_class.keys()),
            "total_pii_columns": len(hits),
        },
        proposed_actions=[
            {
                "label": "Dismiss (PII handling is intentional for this source)",
                "action": "suppress_finding",
                "params": {"finding_id": fid, "scope": "signature"},
            },
        ],
        first_seen=snapshot.captured_at,
        last_seen=snapshot.captured_at,
        occurrences=1,
        confidence="medium",  # name-based heuristic, not value-confirmed
        confidence_score=0.7,
        evidence_count=len(hits),
        baseline_window="schema_snapshot_name_match",
    )
    finding_store.append(finding)
    return finding


def detect_pii_findings(
    finding_store: PIIFindingStore,
    *,
    workspace_id: str = "default",
    suppressed_signatures: set[str] | None = None,
) -> list[StewardFinding]:
    """Read-side: surface open PII findings from the journal. Called
    by ``_run_scan``. Mirrors the read-side of every other event-driven
    detector."""
    return finding_store.open_findings(suppressed_signatures or set())
