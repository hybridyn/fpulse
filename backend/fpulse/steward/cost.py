"""F-Pulse Steward - cost / movement tracking (2026-06-07, P5).

Activates the WAREHOUSE_WASTE FindingKind (cost-level) via the same
event-driven pattern as quality / schema-drift / connector-health.

External runners (the F-Pulse executor, a CI job, a sidecar
instrumenting another framework) post per-run cost events. F-Pulse
stores them and emits findings when patterns cross thresholds.

# What ships today (1.1.x)

  * Event recording: POST /api/steward/cost-event
  * Per-source rollup: GET /api/steward/cost-summary
  * **WAREHOUSE_WASTE detector**: a source that has been read N times
    in a row producing zero output rows. "We're paying the read cost
    but nothing's flowing downstream — turn it off or fix the filter
    upstream."

# What's deliberately deferred to a focused later session

  * COST_DRIFT: requires statistical baseline machinery (the same
    "historical baseline variance" infrastructure Rule 6 expects).
    Coming with the 1.3 Cost Steward module.
  * COST_RECOMMENDATION: optimizer-class output. 2.0.
  * REDUNDANT_TRANSFER: same shape as Archeologist's duplicate-source
    finding, just cost-flavored. Adding a third detector for the same
    pattern would create noisy double-fires; defer to 1.3 when Cost
    Steward owns this whole layer.

# Storage

  * <workspace>/cost_events.jsonl           - append-only event log
  * <workspace>/cost_findings.jsonl         - finding journal (same
                                              shape as quality/schema-
                                              drift journals)

Both are JSONL so dead-simple to inspect with grep / jq.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .models import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    FindingStatus,
    StewardFinding,
)


_FILE_LOCK = threading.Lock()

# Threshold for WAREHOUSE_WASTE: how many consecutive zero-output runs
# from the same source before we surface a finding. 3 is conservative -
# a single empty-day on a daily pipeline is normal; 3 in a row strongly
# suggests upstream filter broke or the schedule is wrong.
_WAREHOUSE_WASTE_STREAK = 3


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Event shape ──────────────────────────────────────────────────────


class CostEvent(BaseModel):
    """One cost / movement event from one pipeline run.

    Either source_signature or sink_signature (or both) should be set
    for the cost rollup; if both are empty AND no node_id is set, the
    event is dropped (no anchor to record against).

    2026-06-07 — node_id / node_label / workflow_id / workflow_name
    added so node-level empty-output detection can share the same
    recording surface as pipeline-level warehouse-waste. When node_id
    is set, the same event also feeds the EMPTY_OUTPUT detector
    anchored on (workflow_id, node_id)."""

    run_id: str = ""
    pipeline_id: str = ""
    pipeline_name: str = ""
    source_signature: str = ""
    sink_signature: str = ""
    workflow_id: str = ""       # for node-level empty_output anchor
    workflow_name: str = ""
    node_id: str = ""           # for node-level empty_output anchor
    node_label: str = ""
    rows_read: int = 0
    rows_written: int = 0
    bytes_read: int = 0       # 0 = unknown / not reported
    bytes_written: int = 0    # 0 = unknown / not reported
    duration_ms: int = 0
    started_at: str = ""
    completed_at: str = ""
    recorded_at: str = Field(default_factory=_iso_now)


# ── Event store ──────────────────────────────────────────────────────


class CostEventStore:
    """Append-only JSONL of every cost event for the workspace. Used
    for the rollup endpoint + WAREHOUSE_WASTE detection."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: CostEvent) -> None:
        with _FILE_LOCK:
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(event.model_dump_json() + "\n")

    def all(self) -> list[CostEvent]:
        if not self.path.exists():
            return []
        out: list[CostEvent] = []
        try:
            with self.path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(CostEvent.model_validate_json(line))
                    except Exception:
                        continue
        except Exception:
            return []
        return out


# ── Finding journal ──────────────────────────────────────────────────


class CostFindingStore:
    """Same pattern as QualityFindingStore / SchemaDriftFindingStore -
    append-only finding journal, scan path filters by suppression."""

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


# ── Rollup ───────────────────────────────────────────────────────────


def summarise_by_source(events: list[CostEvent]) -> dict[str, dict[str, Any]]:
    """Per-source aggregation: total rows read/written, total bytes,
    run count, last_seen. Used by GET /api/steward/cost-summary."""
    agg: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "rows_read": 0, "rows_written": 0,
        "bytes_read": 0, "bytes_written": 0,
        "run_count": 0, "first_seen": "", "last_seen": "",
    })
    for ev in events:
        key = ev.source_signature or ev.sink_signature
        if not key:
            continue
        a = agg[key]
        a["rows_read"]      += ev.rows_read
        a["rows_written"]   += ev.rows_written
        a["bytes_read"]     += ev.bytes_read
        a["bytes_written"]  += ev.bytes_written
        a["run_count"]      += 1
        when = ev.completed_at or ev.recorded_at
        if when:
            if not a["first_seen"] or when < a["first_seen"]:
                a["first_seen"] = when
            if when > a["last_seen"]:
                a["last_seen"] = when
    return dict(agg)


# ── Detector: WAREHOUSE_WASTE (consecutive zero-output runs) ─────────


def _signature(*parts: str) -> str:
    raw = "::".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def record_cost_event(
    event_store: CostEventStore,
    finding_store: CostFindingStore,
    event: CostEvent,
    *,
    workspace_id: str = "default",
    streak_threshold: int = _WAREHOUSE_WASTE_STREAK,
) -> list[StewardFinding]:
    """Persist the event. Then check TWO streak conditions:

      1. WAREHOUSE_WASTE (source level) - last N events for the same
         source_signature all had rows_read > 0 AND rows_written = 0.
         Fires when an entire pipeline-run is wasted reads.

      2. EMPTY_OUTPUT (node level) - last N events for the same
         (workflow_id, node_id) all had rows_written = 0. Fires when
         a specific NODE in a pipeline keeps producing nothing.
         Useful for catching a broken filter/join that returns empty
         even when the upstream source has data.

    Returns a list of findings emitted (0, 1, or 2). Both detectors
    use deterministic ids so re-recording is idempotent.
    """
    # Need SOME anchor to record the event.
    has_anchor = bool(
        event.source_signature or event.sink_signature or event.node_id
    )
    if not has_anchor:
        return []
    event_store.append(event)

    all_events = event_store.all()
    emitted: list[StewardFinding] = []

    # ── WAREHOUSE_WASTE (source-level) ───────────────────────────────
    if event.source_signature:
        source_events = [
            e for e in all_events if e.source_signature == event.source_signature
        ]
        if len(source_events) >= streak_threshold:
            source_events.sort(key=lambda e: e.completed_at or e.recorded_at, reverse=True)
            last_n = source_events[:streak_threshold]
            if all(e.rows_written == 0 and e.rows_read > 0 for e in last_n):
                emitted.append(_build_warehouse_waste(
                    last_n, event, streak_threshold, workspace_id, finding_store,
                ))

    # ── EMPTY_OUTPUT (node-level) ────────────────────────────────────
    if event.node_id and event.workflow_id:
        node_events = [
            e for e in all_events
            if e.node_id == event.node_id and e.workflow_id == event.workflow_id
        ]
        if len(node_events) >= streak_threshold:
            node_events.sort(key=lambda e: e.completed_at or e.recorded_at, reverse=True)
            last_n = node_events[:streak_threshold]
            if all(e.rows_written == 0 for e in last_n):
                emitted.append(_build_empty_output(
                    last_n, event, streak_threshold, workspace_id, finding_store,
                ))

    return emitted


def _build_warehouse_waste(
    last_n: list[CostEvent], event: CostEvent, streak_threshold: int,
    workspace_id: str, finding_store: CostFindingStore,
) -> StewardFinding:
    sig = _signature("warehouse_waste", workspace_id, event.source_signature)
    fid = f"cost-ww-{sig[:12]}"
    now = _iso_now()
    finding = StewardFinding(
        id=fid,
        workspace_id=workspace_id,
        kind=FindingKind.WAREHOUSE_WASTE,
        level=FindingLevel.COST,
        severity=FindingSeverity.P2,
        status=FindingStatus.OPEN,
        title=f"Source read but produced nothing for {streak_threshold} consecutive runs",
        body=(
            f"Source `{event.source_signature}` has been read **{streak_threshold} "
            f"times in a row** producing **zero output rows downstream** (rows_read "
            f"> 0, rows_written = 0 each time).\n\n"
            f"That's wasted work — you're paying the read cost but nothing's "
            f"flowing through. Likely causes:\n"
            f"- An upstream filter dropped all rows (changed input shape)\n"
            f"- The downstream transform errored silently (check run logs)\n"
            f"- The pipeline is on a schedule that no longer matches when data arrives\n\n"
            f"Most recent runs:\n"
            + "\n".join(f"- run `{e.run_id or '?'}` at {e.completed_at or '?'}: "
                       f"read {e.rows_read}, wrote {e.rows_written}"
                       for e in last_n)
            + "\n\nDismiss if this source is expected to be sparse (e.g. error queue)."
        ),
        evidence={
            "source_signature": sig,
            "underlying_source_signature": event.source_signature,
            "streak_length": streak_threshold,
            "recent_run_ids": [e.run_id for e in last_n],
            "total_rows_read_in_streak": sum(e.rows_read for e in last_n),
            "total_bytes_read_in_streak": sum(e.bytes_read for e in last_n),
        },
        proposed_actions=[
            {
                "label": "Dismiss (intentional - sparse source)",
                "action": "suppress_finding",
                "params": {"finding_id": fid, "scope": "signature"},
            },
        ],
        first_seen=last_n[-1].completed_at or last_n[-1].recorded_at or now,
        last_seen=now,
        occurrences=streak_threshold,
        confidence="high",
        confidence_score=1.0,
        evidence_count=streak_threshold,
        baseline_window=f"last_{streak_threshold}_runs",
    )
    finding_store.append(finding)
    return finding


def _build_empty_output(
    last_n: list[CostEvent], event: CostEvent, streak_threshold: int,
    workspace_id: str, finding_store: CostFindingStore,
) -> StewardFinding:
    """2026-06-07 — first NODE-level Steward signal. Activates
    FindingKind.EMPTY_OUTPUT. Same streak pattern as warehouse_waste
    but anchored to (workflow_id, node_id) — catches a specific
    broken transform / filter that keeps producing zero rows even
    when upstream sources have data."""
    sig = _signature("empty_output", workspace_id, event.workflow_id, event.node_id)
    fid = f"node-eo-{sig[:12]}"
    now = _iso_now()
    node_label = event.node_label or event.node_id
    wf_label = event.workflow_name or event.workflow_id
    finding = StewardFinding(
        id=fid,
        workspace_id=workspace_id,
        kind=FindingKind.EMPTY_OUTPUT,
        level=FindingLevel.NODE,
        severity=FindingSeverity.P2,
        status=FindingStatus.OPEN,
        title=f"Node `{node_label}` in `{wf_label}` produced 0 rows {streak_threshold} times in a row",
        body=(
            f"Node `{node_label}` in pipeline `{wf_label}` has been observed "
            f"**{streak_threshold} consecutive times** producing **zero output "
            f"rows**.\n\n"
            f"If this node is a filter/join/transform, the most likely causes are:\n"
            f"- A predicate dropping every row (input shape changed)\n"
            f"- A join condition that no longer matches (column rename upstream)\n"
            f"- A cast or coerce silently dropping invalid rows\n\n"
            f"Most recent runs of this node:\n"
            + "\n".join(f"- run `{e.run_id or '?'}` at {e.completed_at or '?'}: "
                       f"rows_out = 0"
                       for e in last_n)
            + "\n\nDismiss if this node is expected to be empty (e.g. rare event filter)."
        ),
        evidence={
            "source_signature": sig,  # per-(workflow, node) signature for suppression
            "workflow_id": event.workflow_id,
            "workflow_name": event.workflow_name,
            "node_id": event.node_id,
            "node_label": event.node_label,
            "streak_length": streak_threshold,
            "recent_run_ids": [e.run_id for e in last_n],
        },
        proposed_actions=[
            {
                "label": "Dismiss (intentional - rare-event filter)",
                "action": "suppress_finding",
                "params": {"finding_id": fid, "scope": "signature"},
            },
        ],
        first_seen=last_n[-1].completed_at or last_n[-1].recorded_at or now,
        last_seen=now,
        occurrences=streak_threshold,
        confidence="high",
        confidence_score=1.0,
        evidence_count=streak_threshold,
        baseline_window=f"last_{streak_threshold}_node_runs",
    )
    finding_store.append(finding)
    return finding


def detect_cost_findings(
    finding_store: CostFindingStore,
    *,
    workspace_id: str = "default",
    suppressed_signatures: set[str] | None = None,
) -> list[StewardFinding]:
    """Surface open cost findings from the journal. Called by
    _run_scan. Mirrors the read-side of every other event-driven
    detector."""
    return finding_store.open_findings(suppressed_signatures or set())
