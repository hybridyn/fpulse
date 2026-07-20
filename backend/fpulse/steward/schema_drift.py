"""F-Pulse Steward - schema drift detector (2026-06-07).

The first DATA-level Steward detector and the first EVENT-DRIVEN one
(everything else - duplicates, connector health, user rules - derives
findings from current state on every scan; schema drift detects the
diff between two snapshots and emits a finding the moment a change
lands, regardless of scan timing).

# Why event-driven

Schema drift only matters at the moment a NEW shape appears. A scan
that runs N hours after the change can't see the change anymore - by
then the new shape IS the state. So the architecture is:

  1. Pipeline runs (or external recording) ships the **current** schema
     to ``POST /api/steward/schema-snapshot``.
  2. The recorder compares against the previous snapshot for that
     same source signature.
  3. If non-empty diff → append a StewardFinding to the per-workspace
     schema_drift_findings.jsonl.
  4. ``_run_scan`` reads that jsonl + the current suppression set and
     surfaces still-open drift findings.

# Three change classes

  * **ADDED**         - new column appeared. Usually safe (additive
                        change), surfaced at P3 so the team notices
                        without being paged.
  * **DROPPED**       - column gone. Almost always breaks downstream
                        consumers. P1.
  * **TYPE_CHANGED**  - same column name, different type. Casts in
                        downstream pipelines are likely to fail. P1.

# Storage

Per source:
  ``<data_dir>/steward/<workspace>/schemas/<source_signature>.json``

A single JSON file - the LATEST known snapshot for that source.
History isn't needed for drift detection (we only diff against the
previous), but the journal of past *findings* IS persisted because
that's what users dismiss / resolve.

Per workspace:
  ``<data_dir>/steward/<workspace>/schema_drift_findings.jsonl``

Append-only - one StewardFinding JSON per line. The scan path reads
this back and presents the findings to the API.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

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


# ── Data shapes ──────────────────────────────────────────────────────


class Column(BaseModel):
    """One column in a schema snapshot. Type is whatever the source
    reports - we don't normalise across dialects because a Postgres
    'timestamp without time zone' that becomes a 'string' downstream
    IS the kind of drift we want to surface."""

    name: str
    type: str = ""


class SchemaSnapshot(BaseModel):
    """A point-in-time schema for one source object. ``source_signature``
    matches the same key the Archeologist uses (so dedup-by-signature
    works across detectors)."""

    source_signature: str
    source_label: str = ""
    columns: list[Column]
    captured_at: str = Field(default_factory=_iso_now)
    run_id: str = ""


class SchemaChange(BaseModel):
    """One entry in a diff result."""

    kind: str  # added | dropped | type_changed
    column_name: str
    old_type: str = ""
    new_type: str = ""


# ── Diff ─────────────────────────────────────────────────────────────


def diff_schemas(old: list[Column], new: list[Column]) -> list[SchemaChange]:
    """Compute the change set between two schema snapshots.

    Order-insensitive (columns compared by name). Type comparison is
    string-equality after lowercasing - same-named columns whose type
    string changed casing only are NOT treated as drift, because that
    would generate noise on cosmetic differences across drivers.
    """
    old_by_name: dict[str, Column] = {c.name: c for c in old}
    new_by_name: dict[str, Column] = {c.name: c for c in new}
    changes: list[SchemaChange] = []

    for name, new_col in sorted(new_by_name.items()):
        old_col = old_by_name.get(name)
        if old_col is None:
            changes.append(SchemaChange(
                kind="added", column_name=name,
                old_type="", new_type=new_col.type,
            ))
        else:
            if (old_col.type or "").lower() != (new_col.type or "").lower():
                changes.append(SchemaChange(
                    kind="type_changed", column_name=name,
                    old_type=old_col.type, new_type=new_col.type,
                ))

    for name, old_col in sorted(old_by_name.items()):
        if name not in new_by_name:
            changes.append(SchemaChange(
                kind="dropped", column_name=name,
                old_type=old_col.type, new_type="",
            ))

    return changes


# ── Snapshot store (latest-only, per source) ────────────────────────


class SchemaSnapshotStore:
    """File-per-source store at ``<workspace>/schemas/<sig>.json``.

    Holds the LATEST snapshot only - history isn't needed for drift
    detection. The finding journal preserves the history of what
    actually changed."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, source_signature: str) -> Path:
        # Belt-and-braces: signatures should already be safe hex
        # strings from Archeologist, but hash anyway in case a caller
        # supplies a path-unfriendly value.
        safe = hashlib.sha256(source_signature.encode("utf-8")).hexdigest()[:32]
        return self.base_dir / f"{safe}.json"

    def get(self, source_signature: str) -> SchemaSnapshot | None:
        path = self._path(source_signature)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fp:
                return SchemaSnapshot.model_validate(json.load(fp))
        except Exception:
            return None

    def upsert(self, snapshot: SchemaSnapshot) -> None:
        path = self._path(snapshot.source_signature)
        tmp = path.with_suffix(".json.tmp")
        with _FILE_LOCK:
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(snapshot.model_dump(mode="json"), fp, indent=2)
            tmp.replace(path)

    def all(self) -> list[SchemaSnapshot]:
        out: list[SchemaSnapshot] = []
        for path in self.base_dir.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as fp:
                    out.append(SchemaSnapshot.model_validate(json.load(fp)))
            except Exception:
                continue
        return sorted(out, key=lambda s: s.source_signature)


# ── Finding journal (append-only) ───────────────────────────────────


class SchemaDriftFindingStore:
    """Append-only JSONL of every drift finding ever emitted.

    Scan reads this back so drift findings persist across scans even
    though the underlying state (the latest snapshot) doesn't carry
    diff information by itself.

    Each line is a StewardFinding JSON. Dismiss flow writes to the
    standard suppression store, which the scan layer filters against
    when reading from here."""

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
        """All findings still open after suppression. Dedup by finding
        id - if the same drift was re-recorded (e.g. user replayed a
        run), we return the most recent record."""
        suppressed = suppressed_signatures or set()
        by_id: dict[str, StewardFinding] = {}
        for f in self.all():
            by_id[f.id] = f  # later wins
        return [
            f for f in by_id.values()
            if f.evidence.get("source_signature") not in suppressed
            and f.status == FindingStatus.OPEN
        ]


# ── Drift severity rules ────────────────────────────────────────────


def _severity_for_changes(changes: list[SchemaChange]) -> FindingSeverity:
    """Worst-case wins: a single drop or type_change escalates the
    whole finding to P1 regardless of how many low-sev additions it's
    bundled with."""
    if any(c.kind in ("dropped", "type_changed") for c in changes):
        return FindingSeverity.P1
    return FindingSeverity.P3


def _finding_id(source_signature: str, captured_at: str) -> str:
    """Deterministic per (source, capture-time) - re-recording the
    SAME snapshot at the same timestamp is idempotent."""
    h = hashlib.sha256(f"{source_signature}::{captured_at}".encode("utf-8")).hexdigest()[:16]
    return f"sdrift-{h}"


def _render_body(source_label: str, changes: list[SchemaChange]) -> str:
    by_kind: dict[str, list[SchemaChange]] = {"dropped": [], "type_changed": [], "added": []}
    for c in changes:
        by_kind.setdefault(c.kind, []).append(c)
    lines = [f"Schema drift detected on **{source_label or 'source'}**.", ""]
    if by_kind["dropped"]:
        lines.append("**Dropped columns** (likely breaks downstream consumers):")
        for c in by_kind["dropped"]:
            lines.append(f"- `{c.column_name}` (was `{c.old_type}`)")
        lines.append("")
    if by_kind["type_changed"]:
        lines.append("**Type changes** (casts in downstream pipelines may fail):")
        for c in by_kind["type_changed"]:
            lines.append(f"- `{c.column_name}`: `{c.old_type}` -> `{c.new_type}`")
        lines.append("")
    if by_kind["added"]:
        lines.append("**Added columns** (usually safe, surfaced for awareness):")
        for c in by_kind["added"]:
            lines.append(f"- `{c.column_name}` (`{c.new_type}`)")
        lines.append("")
    lines.append(
        "If this change was intentional, dismiss the finding - the new shape "
        "becomes the baseline for future drift detection."
    )
    return "\n".join(lines)


# ── Recorder (single entry point) ───────────────────────────────────


def record_snapshot(
    snapshot_store: SchemaSnapshotStore,
    finding_store: SchemaDriftFindingStore,
    snapshot: SchemaSnapshot,
    *,
    workspace_id: str = "default",
) -> tuple[SchemaSnapshot, list[SchemaChange], StewardFinding | None]:
    """Persist a new snapshot. Diff against the previous; if non-empty,
    append a StewardFinding to the journal.

    Returns ``(snapshot, changes, finding or None)`` so callers can
    surface the immediate outcome to the user."""
    previous = snapshot_store.get(snapshot.source_signature)
    snapshot_store.upsert(snapshot)
    if previous is None:
        # First snapshot for this source - establishes the baseline,
        # no drift to flag.
        return snapshot, [], None
    changes = diff_schemas(previous.columns, snapshot.columns)
    if not changes:
        return snapshot, [], None
    severity = _severity_for_changes(changes)
    finding = StewardFinding(
        id=_finding_id(snapshot.source_signature, snapshot.captured_at),
        workspace_id=workspace_id,
        kind=FindingKind.SCHEMA_DRIFT,
        level=FindingLevel.DATA,
        severity=severity,
        status=FindingStatus.OPEN,
        title=f"Schema drift: {snapshot.source_label or snapshot.source_signature[:12]}",
        body=_render_body(snapshot.source_label, changes),
        evidence={
            "source_signature": snapshot.source_signature,
            "source_label": snapshot.source_label,
            "captured_at": snapshot.captured_at,
            "previous_captured_at": previous.captured_at,
            "run_id": snapshot.run_id,
            "changes": [c.model_dump() for c in changes],
            "added": [c.column_name for c in changes if c.kind == "added"],
            "dropped": [c.column_name for c in changes if c.kind == "dropped"],
            "type_changed": [c.column_name for c in changes if c.kind == "type_changed"],
        },
        proposed_actions=[
            {
                "label": "Dismiss (intentional schema change - accept new baseline)",
                "action": "suppress_finding",
                "params": {"finding_id": _finding_id(snapshot.source_signature, snapshot.captured_at),
                            "scope": "signature"},
            },
        ],
        first_seen=snapshot.captured_at,
        last_seen=snapshot.captured_at,
        occurrences=1,
        confidence="high",
        confidence_score=1.0,
        evidence_count=len(changes),
        baseline_window="previous_snapshot",
    )
    finding_store.append(finding)
    return snapshot, changes, finding


# ── Detector (read-side, plugs into _run_scan) ──────────────────────


def detect_schema_drift(
    finding_store: SchemaDriftFindingStore,
    *,
    workspace_id: str = "default",
    suppressed_signatures: set[str] | None = None,
) -> list[StewardFinding]:
    """Return every open drift finding for the workspace, after
    suppression. This is the read-side that ``_run_scan`` calls -
    detection itself happens inside ``record_snapshot`` at the moment
    a new snapshot is recorded."""
    return finding_store.open_findings(suppressed_signatures or set())
