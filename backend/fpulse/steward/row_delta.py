"""F-Pulse Steward — row-count integrity detector (2026-06-18).

Enforces the "safe optimization" rule from docs/abstraction-boundary.md:

  > Runtime optimization must never change the logical dataset. It may change
  > how data moves / is stored / is processed — never what the data MEANS.

A node whose contract is **1:1** (one input row → one output row: derived
column, rename, sort, typecast, window, schema-map, embed, generic column
transform) must emit exactly as many rows as it consumed. If it doesn't, rows
were silently dropped or duplicated — a bad expression, a join hidden inside
SQL, or an optimization that changed meaning. That is precisely the integrity
violation the contract forbids, so we surface it as a node-level finding.

# Conservative by design

This detector is OBSERVE-ONLY and deliberately under-reports rather than
risk a false alarm:

  * Only the unambiguously 1:1 step types in ``PRESERVING_STEP_TYPES`` are
    checked. Cardinality-changing nodes (filter, join, aggregate, dedup,
    union, pivot, sample, routers, validators, …) are never flagged — a row
    delta there is expected, not a defect.
  * Only single-input steps are checked. A multi-input node is a join/union
    by definition and is exempt.
  * A finding fires only when BOTH counts are known, the input was non-empty,
    and they actually differ.

# Event-driven, like schema-drift

Detection happens at run-ingest time (``record_row_deltas``, called from
``steward/ingest.py`` after a successful FULL run) and findings are appended to
a per-workspace journal. The scan path (``detect_row_deltas``) just re-surfaces
still-open findings, filtered by suppression — same shape as the schema-drift
detector.

Storage: ``<data_dir>/steward/<workspace>/row_delta_findings.jsonl`` (append-only).
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path

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


# Step types whose contract is strictly 1:1 (row count in == row count out).
# Kept intentionally small — every entry here must be a node that CANNOT
# legitimately change cardinality. When in doubt, leave it out (we'd rather
# miss a delta than cry wolf on a node that's allowed to reshape rows).
PRESERVING_STEP_TYPES: frozenset[str] = frozenset({
    "transform",        # compute new column values
    "derived_column",   # add a computed column
    "rename",           # project / alias columns
    "typecast",         # change column types
    "sort",             # reorder rows
    "window",           # rank/lag/running totals — adds columns, same rows
    "schema_mapper",    # source→target field mapping + coercion
    "embedder",         # text column → vector column
})


def _step_type_str(step: object) -> str:
    st = getattr(step, "type", None)
    if st is None:
        st = getattr(step, "step_type", None)
    if hasattr(st, "value"):
        st = st.value
    return str(st or "").lower()


# ── Finding journal (append-only) — mirrors SchemaDriftFindingStore ──


class RowDeltaFindingStore:
    """Append-only JSONL of every row-delta finding ever emitted.

    Suppression keys on ``evidence["source_signature"]`` (here a node
    signature ``<workflow_id>::<step_id>``) so dismissal flows through the
    exact same path the other detectors use."""

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
            by_id[f.id] = f  # later record wins
        return [
            f for f in by_id.values()
            if f.evidence.get("source_signature") not in suppressed
            and f.status == FindingStatus.OPEN
        ]


# ── Helpers ──────────────────────────────────────────────────────────


def _finding_id(workflow_id: str, step_id: str, run_id: str) -> str:
    """Deterministic per (step, run) so re-ingesting the same run is
    idempotent (no duplicate findings on a replay)."""
    h = hashlib.sha256(f"{workflow_id}::{step_id}::{run_id}".encode("utf-8")).hexdigest()[:16]
    return f"rowdelta-{h}"


def _render_body(label: str, step_type: str, rows_in: int, rows_out: int) -> str:
    delta = rows_out - rows_in
    verb = "dropped" if delta < 0 else "duplicated"
    return (
        f"Row count changed in a step that should preserve it.\n\n"
        f"**{label}** (`{step_type}`): **{rows_in:,} in → {rows_out:,} out** "
        f"({delta:+,}).\n\n"
        f"A `{step_type}` step is 1:1 — one input row maps to one output row. "
        f"A change of this size means rows were silently {verb}. Usual causes: "
        f"an expression that filters or fans out, a join hidden inside custom "
        f"SQL, or an optimization that altered meaning.\n\n"
        f"If this is intentional, dismiss the finding to accept the new "
        f"baseline; otherwise inspect the step's logic."
    )


# ── Recorder (run-ingest side) ───────────────────────────────────────


def record_row_deltas(
    finding_store: RowDeltaFindingStore,
    *,
    workflow: object,
    run_result: object,
    workspace_id: str = "default",
) -> list[StewardFinding]:
    """Inspect a finished FULL run; append a finding for every 1:1 step
    whose row count changed. Returns the findings emitted (possibly empty).

    Best-effort and side-effect-light: callers (steward/ingest.py) already
    run inside a swallow-all guard, but we also guard per-step here so one
    odd step never aborts the rest.
    """
    findings: list[StewardFinding] = []
    steps = list(getattr(workflow, "steps", []) or [])
    if not steps:
        return findings
    step_by_id = {getattr(s, "id", None): s for s in steps}
    results = getattr(run_result, "step_results", {}) or {}

    # Input map: to_step -> [from_step], from the workflow connections.
    inputs: dict[str, list[str]] = {}
    for conn in (getattr(workflow, "connections", []) or []):
        to_step = getattr(conn, "to_step", None)
        from_step = getattr(conn, "from_step", None)
        if to_step is not None and from_step is not None:
            inputs.setdefault(str(to_step), []).append(str(from_step))

    wf_id = str(getattr(workflow, "id", "") or "")
    run_id = str(getattr(run_result, "run_id", "") or "")

    def _rows(step_id: str) -> int | None:
        sr = results.get(step_id)
        if sr is None or getattr(sr, "status", "") != "success":
            return None
        try:
            return int(getattr(sr, "row_count", None))
        except (TypeError, ValueError):
            return None

    for step_id, step in step_by_id.items():
        try:
            if step_id is None:
                continue
            if _step_type_str(step) not in PRESERVING_STEP_TYPES:
                continue
            srcs = inputs.get(str(step_id), [])
            if len(srcs) != 1:
                continue  # multi-input = join/union semantics → exempt
            rows_in = _rows(srcs[0])
            rows_out = _rows(str(step_id))
            if rows_in is None or rows_out is None:
                continue
            if rows_in <= 0 or rows_in == rows_out:
                continue

            step_type = _step_type_str(step)
            label = (getattr(step, "label", "") or "") or str(step_id)
            sig = f"{wf_id}::{step_id}"
            # A drop is data loss (P2); a gain is unexpected fan-out (P3).
            severity = FindingSeverity.P2 if rows_out < rows_in else FindingSeverity.P3
            fid = _finding_id(wf_id, str(step_id), run_id)
            finding = StewardFinding(
                id=fid,
                workspace_id=workspace_id,
                kind=FindingKind.ROW_COUNT_DELTA,
                level=FindingLevel.NODE,
                severity=severity,
                status=FindingStatus.OPEN,
                title=f"Row count changed in {label} ({rows_in:,} → {rows_out:,})",
                body=_render_body(str(label), step_type, rows_in, rows_out),
                evidence={
                    "source_signature": sig,
                    "workflow_id": wf_id,
                    "step_id": str(step_id),
                    "step_type": step_type,
                    "step_label": str(label),
                    "rows_in": rows_in,
                    "rows_out": rows_out,
                    "delta": rows_out - rows_in,
                    "run_id": run_id,
                },
                proposed_actions=[
                    {
                        "label": "Dismiss (intentional — accept this row count)",
                        "action": "suppress_finding",
                        "params": {"finding_id": fid, "scope": "signature"},
                    },
                ],
                first_seen=_iso_now(),
                last_seen=_iso_now(),
                occurrences=1,
                confidence="high",
                confidence_score=1.0,
                evidence_count=1,
                baseline_window="single_run",
            )
            finding_store.append(finding)
            findings.append(finding)
        except Exception:  # noqa: BLE001 — per-step best-effort
            continue
    return findings


# ── Detector (read-side, plugs into _run_scan) ──────────────────────


def detect_row_deltas(
    finding_store: RowDeltaFindingStore,
    *,
    workspace_id: str = "default",
    suppressed_signatures: set[str] | None = None,
) -> list[StewardFinding]:
    """Return every open row-delta finding for the workspace, after
    suppression. Detection itself happens in ``record_row_deltas`` at
    run-ingest time."""
    return finding_store.open_findings(suppressed_signatures or set())


__all__ = [
    "PRESERVING_STEP_TYPES",
    "RowDeltaFindingStore",
    "record_row_deltas",
    "detect_row_deltas",
]
