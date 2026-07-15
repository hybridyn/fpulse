"""F-Pulse Memory Layer — durable, gated lessons.

This module is *distinct* from ``steward/memory.py``:

  * ``memory.py``    = the **event journal** — append-only JSONL log of
                       every emit / dismiss / resolve. Used by the
                       learning layer to compute persistent occurrence
                       counts, severity escalation, and rebound
                       detection. Operational, high-volume, deletable.

  * ``lessons.py``   = the **Memory Layer** — durable, human-approved
                       lessons distilled from operator decisions. One
                       lesson per file. Low-volume, schema-stable,
                       repo-friendly. The actual institutional
                       knowledge that survives team turnover.

# Why a separate store

The event journal answers "what has the Steward seen recently?". The
Memory Layer answers a different question: "what has this team
**learned** that future runs should benefit from?". Those are two
different storage shapes:

  * Events: append-mostly, large, ephemeral, no human curation.
  * Lessons: append-rarely, small, durable, human-approved.

Conflating them — storing lessons in the same JSONL as raw events —
would mean a lesson the team confirmed two years ago could be lost in
a journal-rotation. Per-lesson YAML files make backup, code-review,
and migration trivially safe.

# The 10 lesson categories (from the architecture review)

    source_quirk           — a non-obvious detail about a source system
    schema_drift           — observed shape changes worth remembering
    failure_pattern        — known failure signature + the fix that works
    transformation_rule    — "always do X when reading Y"
    retry_rule             — when retry helps vs when it doesn't
    cost_anomaly           — patterns that drove unexpected cost
    duplicate_warning      — intentional duplicates (DR, data-vault, etc.)
    sla_pattern            — observed timing / volume behaviour
    user_fix               — fix the user applied + why it worked
    security_finding       — PII / credential / access pattern of note

# The 8-step failure → lesson workflow

When a pipeline fails:

  1. Steward reads the current error.
  2. Searches existing lessons by source + error class.
  3. Checks source memory (matching `source_quirk` / `failure_pattern`).
  4. Compares against recent schema_drift entries.
  5. Surfaces a suggested root cause from the highest-confidence lesson.
  6. Recommends the lesson's `approved_fix`.
  7. Asks user approval BEFORE any pipeline change.
  8. On resolution, stores a validated lesson update (occurrence_count++,
     last_validated = now, confidence = boosted).

This module implements the storage + retrieval primitives that the
workflow runs on top of.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


_FILE_LOCK = threading.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_filename(name: str) -> str:
    """Sanitize a lesson identifier into a filesystem-safe name. Keeps
    the file map browsable + grep-friendly. Falls back to the raw uuid
    if the name has no safe chars."""
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-")
    return cleaned[:80] or uuid.uuid4().hex[:12]


class LessonType(str, Enum):
    """Categories from the architecture review (block 1). One per
    Memory-Layer field; the UI groups lessons by this value."""

    SOURCE_QUIRK = "source_quirk"
    SCHEMA_DRIFT = "schema_drift"
    FAILURE_PATTERN = "failure_pattern"
    TRANSFORMATION_RULE = "transformation_rule"
    RETRY_RULE = "retry_rule"
    COST_ANOMALY = "cost_anomaly"
    DUPLICATE_WARNING = "duplicate_warning"
    SLA_PATTERN = "sla_pattern"
    USER_FIX = "user_fix"
    SECURITY_FINDING = "security_finding"


class LessonStatus(str, Enum):
    """Lifecycle.

    PROPOSED    — Steward or user suggested a lesson; not yet approved.
                  Steward will NOT use a PROPOSED lesson to influence
                  future reasoning (Rule 3: Learning is gated).
    APPROVED    — A human reviewer has confirmed the lesson. Steward
                  uses it for matching + recommendations.
    REJECTED    — The reviewer marked the proposal incorrect. Kept on
                  disk for audit + to suppress re-propose loops.
    STALE       — Auto-aged after `validity_days` without re-validation
                  (default 180). Hidden from default queries; still
                  retrievable for historical context.
    """

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    STALE = "stale"


class LessonConfidence(str, Enum):
    """Confidence tracks how much the team trusts this lesson.

    LOW    — observed once, not yet validated by a second occurrence
    MEDIUM — re-validated 2-4 times OR approved by one reviewer
    HIGH   — re-validated 5+ times OR approved by multiple reviewers
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceRef(BaseModel):
    """Pointer back to the source data — required by architectural
    Rule 4 (explicit provenance). Every lesson must be re-derivable
    from the cited evidence."""

    kind: Literal["finding", "execution", "journal_event", "manual"]
    id: str
    note: str = ""


class MemoryLesson(BaseModel):
    """A single durable lesson the team has accumulated.

    Mirrors the YAML shape from the architecture review:

        source: Oracle_FIN_PROD
        pipeline: Load_AP_Invoices
        lesson_type: failure_pattern
        issue: ORA-12154 alias failure
        symptom: Cannot find alias in TNS/EZConnect
        approved_fix: Check gateway TNS_ADMIN and Oracle client config
        confidence: high
        last_validated: 2026-06-05
        approved_by: Data Owner
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    workspace_id: str = "default"

    # Provenance / scoping — what this lesson is *about*
    source: str = Field(
        default="",
        description=(
            "Logical source identifier this lesson concerns "
            "(e.g. 'Oracle_FIN_PROD', 'salesforce_prod', "
            "'postgres_warehouse'). Empty if cross-source."
        ),
    )
    pipeline: str = Field(
        default="",
        description=(
            "Pipeline name or ID this lesson concerns. Empty if "
            "source-wide and applies to any pipeline reading the source."
        ),
    )
    lesson_type: LessonType
    tags: list[str] = Field(default_factory=list)

    # The lesson itself
    issue: str = Field(
        description="Short title, e.g. 'ORA-12154 alias failure'.",
    )
    symptom: str = Field(
        default="",
        description=(
            "What the user / log surface actually shows when this "
            "happens. The 'how do I recognise this?' field."
        ),
    )
    root_cause: str = Field(
        default="",
        description="Best understanding of why it happens.",
    )
    approved_fix: str = Field(
        description=(
            "The fix the team has agreed on. Steward suggests THIS "
            "text verbatim to future operators encountering matching "
            "symptoms. Plain prose — short, imperative."
        ),
    )

    # Lifecycle + trust
    status: LessonStatus = LessonStatus.PROPOSED
    confidence: LessonConfidence = LessonConfidence.LOW
    proposed_by: str = Field(
        default="steward",
        description="Who proposed it ('steward' if auto, else user email).",
    )
    approved_by: str = Field(
        default="",
        description="Reviewer who approved (empty until status=APPROVED).",
    )

    # Auto-stale management
    validity_days: int = Field(
        default=180,
        ge=30,
        le=730,
        description=(
            "Auto-age after this many days without re-validation. "
            "Reviewers can re-validate to push the clock out."
        ),
    )

    # Audit metadata
    created_at: str = Field(default_factory=_iso_now)
    last_validated: str = Field(default_factory=_iso_now)
    occurrence_count: int = Field(
        default=1,
        ge=1,
        description=(
            "How many times this lesson has been re-confirmed by a "
            "matching incident. Bumps confidence as it grows."
        ),
    )

    # Provenance — Rule 4 requires every lesson cite the data it came from
    evidence: list[EvidenceRef] = Field(default_factory=list)

    def to_yaml(self) -> str:
        """Render as a human-readable YAML block. We hand-roll this
        rather than pull in PyYAML — keeps deps minimal and the output
        format is predictable for diff review."""
        d = self.model_dump(mode="json")
        lines: list[str] = []
        for key in (
            "id", "workspace_id", "source", "pipeline", "lesson_type",
            "status", "confidence", "issue", "symptom", "root_cause",
            "approved_fix", "proposed_by", "approved_by",
            "validity_days", "occurrence_count",
            "created_at", "last_validated",
        ):
            v = d.get(key)
            if v is None or v == "":
                continue
            if isinstance(v, str) and "\n" in v:
                lines.append(f"{key}: |")
                for ln in v.splitlines():
                    lines.append(f"  {ln}")
            else:
                # Quote strings with colons or leading dashes to keep parsing safe
                if isinstance(v, str) and (":" in v or v.startswith("-")):
                    lines.append(f"{key}: {json.dumps(v)}")
                else:
                    lines.append(f"{key}: {v}")
        if d.get("tags"):
            lines.append("tags:")
            for t in d["tags"]:
                lines.append(f"  - {t}")
        if d.get("evidence"):
            lines.append("evidence:")
            for e in d["evidence"]:
                lines.append(f"  - kind: {e['kind']}")
                lines.append(f"    id: {e['id']}")
                if e.get("note"):
                    lines.append(f"    note: {json.dumps(e['note'])}")
        return "\n".join(lines) + "\n"


class LessonStore:
    """File-per-lesson store at ``<data_dir>/steward/<ws>/lessons/``.

    Each lesson is stored as both ``<id>.yaml`` (the canonical
    human-readable form) AND ``<id>.json`` (the machine-readable form
    used by the API). The two files are written together inside the
    file lock — they are always consistent.

    Why two formats: the YAML is what reviewers diff in PRs; the JSON
    is what the API returns without parsing-overhead. Both are
    regenerated from the same Pydantic model so they can't drift.
    """

    def __init__(self, lessons_dir: Path):
        self.dir = lessons_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    # ── Write side ───────────────────────────────────────────────

    def save(self, lesson: MemoryLesson) -> MemoryLesson:
        """Upsert by lesson.id. Writes BOTH .yaml and .json atomically
        under the file lock."""
        # Auto-bump confidence based on occurrence_count
        lesson = self._recompute_confidence(lesson)
        base = self.dir / _safe_filename(lesson.id)
        yaml_path = base.with_suffix(".yaml")
        json_path = base.with_suffix(".json")
        with _FILE_LOCK:
            # Write the JSON first — it's atomically loadable and
            # what the API depends on. YAML is the human side.
            with json_path.open("w", encoding="utf-8") as fp:
                json.dump(lesson.model_dump(mode="json"), fp, indent=2)
            with yaml_path.open("w", encoding="utf-8") as fp:
                fp.write(lesson.to_yaml())
        return lesson

    def propose(
        self,
        *,
        source: str,
        pipeline: str,
        lesson_type: LessonType,
        issue: str,
        approved_fix: str,
        symptom: str = "",
        root_cause: str = "",
        proposed_by: str = "steward",
        evidence: list[EvidenceRef] | None = None,
        workspace_id: str = "default",
        tags: list[str] | None = None,
    ) -> MemoryLesson:
        """Create a new PROPOSED lesson. Steward calls this when it
        sees a pattern worth remembering; the user must then call
        ``approve()`` before the lesson influences future reasoning
        (architectural Rule 3: Learning is gated)."""
        lesson = MemoryLesson(
            workspace_id=workspace_id,
            source=source,
            pipeline=pipeline,
            lesson_type=lesson_type,
            issue=issue,
            symptom=symptom,
            root_cause=root_cause,
            approved_fix=approved_fix,
            status=LessonStatus.PROPOSED,
            confidence=LessonConfidence.LOW,
            proposed_by=proposed_by,
            evidence=evidence or [],
            tags=tags or [],
        )
        return self.save(lesson)

    def approve(self, lesson_id: str, approver: str) -> MemoryLesson | None:
        """Promote a PROPOSED lesson to APPROVED. Required before
        Steward will surface this lesson as a recommendation."""
        lesson = self.get(lesson_id)
        if lesson is None or lesson.status != LessonStatus.PROPOSED:
            return None
        lesson.status = LessonStatus.APPROVED
        lesson.approved_by = approver
        lesson.last_validated = _iso_now()
        return self.save(lesson)

    def reject(self, lesson_id: str, reviewer: str, reason: str = "") -> MemoryLesson | None:
        """Mark a proposal incorrect. Kept on disk so Steward doesn't
        re-propose the same pattern."""
        lesson = self.get(lesson_id)
        if lesson is None:
            return None
        lesson.status = LessonStatus.REJECTED
        lesson.approved_by = reviewer
        lesson.last_validated = _iso_now()
        # Stash the rejection reason in evidence so the audit trail is preserved
        lesson.evidence.append(EvidenceRef(kind="manual", id=reviewer, note=f"rejected: {reason}"))
        return self.save(lesson)

    def revalidate(self, lesson_id: str, reviewer: str) -> MemoryLesson | None:
        """Re-confirm an existing APPROVED lesson — bumps the
        occurrence counter, pushes the auto-stale clock, and may
        promote confidence."""
        lesson = self.get(lesson_id)
        if lesson is None or lesson.status not in (LessonStatus.APPROVED, LessonStatus.STALE):
            return None
        lesson.occurrence_count += 1
        lesson.last_validated = _iso_now()
        if lesson.status == LessonStatus.STALE:
            lesson.status = LessonStatus.APPROVED
        if not lesson.approved_by:
            lesson.approved_by = reviewer
        return self.save(lesson)

    def _recompute_confidence(self, lesson: MemoryLesson) -> MemoryLesson:
        """Derive confidence from occurrence_count + approval state."""
        if lesson.status == LessonStatus.REJECTED:
            lesson.confidence = LessonConfidence.LOW
        elif lesson.occurrence_count >= 5 and lesson.status == LessonStatus.APPROVED:
            lesson.confidence = LessonConfidence.HIGH
        elif lesson.occurrence_count >= 2 or lesson.status == LessonStatus.APPROVED:
            lesson.confidence = LessonConfidence.MEDIUM
        else:
            lesson.confidence = LessonConfidence.LOW
        return lesson

    def delete(self, lesson_id: str) -> bool:
        """Hard delete — used for rejected proposals the team wants to
        prune. APPROVED lessons should be marked STALE rather than
        deleted; the audit trail matters."""
        base = self.dir / _safe_filename(lesson_id)
        deleted = False
        with _FILE_LOCK:
            for suffix in (".json", ".yaml"):
                p = base.with_suffix(suffix)
                if p.is_file():
                    try:
                        p.unlink()
                        deleted = True
                    except OSError:
                        pass
        return deleted

    # ── Read side ────────────────────────────────────────────────

    def get(self, lesson_id: str) -> MemoryLesson | None:
        """Load a single lesson by ID. Returns None if not found or
        the file is corrupt (corruption never crashes the caller —
        Steward is a 'nice to have' surface)."""
        path = self.dir / (_safe_filename(lesson_id) + ".json")
        if not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
            return MemoryLesson.model_validate(data)
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def list_all(
        self,
        *,
        status: LessonStatus | None = None,
        lesson_type: LessonType | None = None,
        source: str | None = None,
        pipeline: str | None = None,
    ) -> list[MemoryLesson]:
        """List lessons matching the filter. Newest-first by
        last_validated. Filters are AND-combined."""
        out: list[MemoryLesson] = []
        for path in self.dir.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as fp:
                    data = json.load(fp)
                lesson = MemoryLesson.model_validate(data)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if status is not None and lesson.status != status:
                continue
            if lesson_type is not None and lesson.lesson_type != lesson_type:
                continue
            if source is not None and lesson.source != source:
                continue
            if pipeline is not None and lesson.pipeline != pipeline:
                continue
            out.append(lesson)
        out.sort(key=lambda L: L.last_validated, reverse=True)
        return out

    def search_for_failure(
        self,
        *,
        source: str = "",
        error_substring: str = "",
        max_results: int = 5,
    ) -> list[MemoryLesson]:
        """The actual failure-recovery query (step 2 of the 8-step
        workflow). Returns APPROVED lessons whose ``source`` matches
        AND whose ``issue`` or ``symptom`` contains the error
        substring, ranked by confidence + occurrence_count."""
        if not error_substring:
            return []
        needle = error_substring.lower()
        candidates: list[MemoryLesson] = []
        for lesson in self.list_all(status=LessonStatus.APPROVED):
            if source and lesson.source and lesson.source != source:
                # Skip cross-source matches if the caller specified a source
                continue
            haystack = (lesson.issue + " " + lesson.symptom + " " + lesson.root_cause).lower()
            if needle in haystack:
                candidates.append(lesson)
        # Rank: HIGH > MEDIUM > LOW, then by occurrence_count desc
        conf_rank = {
            LessonConfidence.HIGH: 3,
            LessonConfidence.MEDIUM: 2,
            LessonConfidence.LOW: 1,
        }
        candidates.sort(
            key=lambda L: (conf_rank.get(L.confidence, 0), L.occurrence_count),
            reverse=True,
        )
        return candidates[:max_results]

    def stats(self) -> dict[str, Any]:
        """At-a-glance counters for the UI Memory Layer card."""
        by_status: dict[str, int] = {s.value: 0 for s in LessonStatus}
        by_type: dict[str, int] = {}
        total = 0
        for path in self.dir.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as fp:
                    data = json.load(fp)
                lesson = MemoryLesson.model_validate(data)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            total += 1
            by_status[lesson.status.value] = by_status.get(lesson.status.value, 0) + 1
            by_type[lesson.lesson_type.value] = by_type.get(lesson.lesson_type.value, 0) + 1
        return {
            "total_lessons": total,
            "by_status": by_status,
            "by_type": by_type,
        }

    # ── Maintenance ──────────────────────────────────────────────

    def age_to_stale(self) -> int:
        """Walk every APPROVED lesson and transition to STALE any whose
        ``last_validated`` is older than ``validity_days`` ago. Returns
        the count of transitions. Called from a periodic maintenance
        hook (currently the API's /lessons GET — cheap, idempotent)."""
        from datetime import datetime, timedelta
        aged = 0
        for lesson in self.list_all(status=LessonStatus.APPROVED):
            try:
                last = datetime.fromisoformat(lesson.last_validated)
            except (ValueError, TypeError):
                continue
            cutoff = datetime.now(timezone.utc) - timedelta(days=lesson.validity_days)
            if last < cutoff:
                lesson.status = LessonStatus.STALE
                self.save(lesson)
                aged += 1
        return aged
