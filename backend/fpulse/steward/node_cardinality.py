"""F-Pulse Steward — node cardinality anomaly detector (2026-06-18).

The companion to the row-count integrity detector (steward/row_delta.py).
Row-delta watches nodes that should be **1:1**; this watches the nodes that
are *allowed* to change cardinality but can do so *catastrophically* when
mis-configured:

  * join        — output >> inputs  → JOIN_EXPLOSION  (near-cartesian / bad key)
                  output << inputs  → JOIN_COLLAPSE   (key mismatch, almost nothing matched)
  * deduplicate — removed almost everything → DEDUPE_COLLAPSE (wrong dedupe key)
  * filter      — removed every row         → FILTER_DROPPED_ALL (wrong predicate)

# Conservative by design

Like row-delta, this is OBSERVE-ONLY and tuned to fire only on *egregious*
cases, not normal variation — a one-to-many join legitimately produces more
rows, and a filter legitimately drops some. Thresholds carry an absolute
floor so small datasets never trip them. When in doubt, we stay silent.

Event-driven / run-fed: recorded at run-ingest time (record_node_cardinality,
called from steward/ingest.py after a successful FULL run); the scan re-surfaces
still-open findings. Same shape as schema-drift and row-delta.

Storage: ``<data_dir>/steward/<workspace>/node_cardinality_findings.jsonl``.
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

# ── Thresholds (deliberately conservative — egregious cases only) ────
_JOIN_EXPLODE_RATIO = 10.0     # output >= 10x the larger input ...
_JOIN_EXPLODE_FLOOR = 50_000   # ... and at least this many rows
_JOIN_COLLAPSE_RATIO = 0.05    # output <= 5% of the smaller input ...
_COLLAPSE_FLOOR = 1_000        # ... and the input had at least this many rows
_DEDUPE_COLLAPSE_RATIO = 0.05  # output <= 5% of input (removed >= 95%)
_FILTER_FLOOR = 100            # filter dropped ALL of at least this many rows


# Tunable thresholds surfaced on the Coverage page (rung 1.5). Defaults mirror
# the constants above. An operator overrides per-kind via
# StewardSettings.detectors[<kind>].thresholds = {"ratio": X, "floor": Y} —
# e.g. "my joins legitimately explode 50x, don't warn under 100x".
THRESHOLD_SPEC: dict[str, list[dict]] = {
    FindingKind.JOIN_EXPLOSION.value: [
        {"key": "ratio", "label": "Explosion ratio (output / larger input)",
         "default": _JOIN_EXPLODE_RATIO, "min": 2, "max": 1000, "step": 1},
        {"key": "floor", "label": "Min output rows to flag",
         "default": _JOIN_EXPLODE_FLOOR, "min": 0, "max": 100000000, "step": 1000},
    ],
    FindingKind.JOIN_COLLAPSE.value: [
        {"key": "ratio", "label": "Kept ratio (output / smaller input)",
         "default": _JOIN_COLLAPSE_RATIO, "min": 0.001, "max": 0.9, "step": 0.01},
        {"key": "floor", "label": "Min input rows",
         "default": _COLLAPSE_FLOOR, "min": 0, "max": 100000000, "step": 100},
    ],
    FindingKind.DEDUPE_COLLAPSE.value: [
        {"key": "ratio", "label": "Kept ratio (output / input)",
         "default": _DEDUPE_COLLAPSE_RATIO, "min": 0.001, "max": 0.9, "step": 0.01},
        {"key": "floor", "label": "Min input rows",
         "default": _COLLAPSE_FLOOR, "min": 0, "max": 100000000, "step": 100},
    ],
    FindingKind.FILTER_DROPPED_ALL.value: [
        {"key": "floor", "label": "Min input rows",
         "default": _FILTER_FLOOR, "min": 0, "max": 100000000, "step": 50},
    ],
}


def _eff(thresholds: dict | None, kind_value: str, key: str) -> float:
    """Effective threshold: the operator's override if present + parseable,
    otherwise the built-in default from THRESHOLD_SPEC."""
    default = next(
        (s["default"] for s in THRESHOLD_SPEC.get(kind_value, []) if s["key"] == key),
        0,
    )
    ov = (thresholds or {}).get(kind_value) or {}
    try:
        v = ov.get(key)
        return float(v) if v is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _step_type_str(step: object) -> str:
    st = getattr(step, "type", None)
    if st is None:
        st = getattr(step, "step_type", None)
    if hasattr(st, "value"):
        st = st.value
    return str(st or "").lower()


class NodeCardinalityFindingStore:
    """Append-only JSONL journal — mirrors RowDeltaFindingStore. Suppression
    keys on ``evidence["source_signature"]`` (a node signature) so dismissal
    flows through the same path as every other detector."""

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


def _finding_id(workflow_id: str, step_id: str, run_id: str, kind: str) -> str:
    h = hashlib.sha256(f"{workflow_id}::{step_id}::{run_id}::{kind}".encode("utf-8")).hexdigest()[:16]
    return f"ncard-{h}"


def _mk_finding(
    *, kind: FindingKind, severity: FindingSeverity, workspace_id: str,
    wf_id: str, step_id: str, run_id: str, label: str, title: str,
    body: str, evidence: dict,
) -> StewardFinding:
    sig = f"{wf_id}::{step_id}"
    fid = _finding_id(wf_id, str(step_id), run_id, kind.value)
    return StewardFinding(
        id=fid,
        workspace_id=workspace_id,
        kind=kind,
        level=FindingLevel.NODE,
        severity=severity,
        status=FindingStatus.OPEN,
        title=title,
        body=body,
        evidence={"source_signature": sig, "workflow_id": wf_id,
                  "step_id": str(step_id), "step_label": label,
                  "run_id": run_id, **evidence},
        proposed_actions=[{
            "label": "Dismiss (expected for this step)",
            "action": "suppress_finding",
            "params": {"finding_id": fid, "scope": "signature"},
        }],
        first_seen=_iso_now(),
        last_seen=_iso_now(),
        occurrences=1,
        confidence="high",
        confidence_score=1.0,
        evidence_count=1,
        baseline_window="single_run",
    )


def record_node_cardinality(
    finding_store: NodeCardinalityFindingStore,
    *,
    workflow: object,
    run_result: object,
    workspace_id: str = "default",
    thresholds: dict | None = None,
) -> list[StewardFinding]:
    """Inspect a finished FULL run; flag egregious cardinality anomalies on
    join / deduplicate / filter nodes. Best-effort, per-step guarded.

    ``thresholds`` (rung 1.5): optional per-kind overrides, e.g.
    ``{"join_explosion": {"ratio": 100}}``. Absent keys fall back to the
    conservative built-in defaults."""
    findings: list[StewardFinding] = []
    steps = list(getattr(workflow, "steps", []) or [])
    if not steps:
        return findings

    # Effective thresholds (override or default) — resolved once per run.
    je_ratio = _eff(thresholds, "join_explosion", "ratio")
    je_floor = _eff(thresholds, "join_explosion", "floor")
    jc_ratio = _eff(thresholds, "join_collapse", "ratio")
    jc_floor = _eff(thresholds, "join_collapse", "floor")
    dd_ratio = _eff(thresholds, "dedupe_collapse", "ratio")
    dd_floor = _eff(thresholds, "dedupe_collapse", "floor")
    flt_floor = _eff(thresholds, "filter_dropped_all", "floor")
    step_by_id = {getattr(s, "id", None): s for s in steps}
    results = getattr(run_result, "step_results", {}) or {}

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
            stype = _step_type_str(step)
            if stype not in ("join", "deduplicate", "filter"):
                continue
            out = _rows(str(step_id))
            if out is None:
                continue
            label = (getattr(step, "label", "") or "") or str(step_id)
            srcs = inputs.get(str(step_id), [])
            in_counts = [c for c in (_rows(s) for s in srcs) if c is not None]
            if not in_counts:
                continue

            if stype == "join" and len(in_counts) >= 2:
                hi, lo = max(in_counts), min(in_counts)
                if hi > 0 and out >= hi * je_ratio and out >= je_floor:
                    findings.append(_mk_finding(
                        kind=FindingKind.JOIN_EXPLOSION, severity=FindingSeverity.P2,
                        workspace_id=workspace_id, wf_id=wf_id, step_id=str(step_id),
                        run_id=run_id, label=str(label),
                        title=f"Join row explosion in {label} ({hi:,} → {out:,})",
                        body=(f"The join **{label}** produced **{out:,}** rows from inputs of "
                              f"{hi:,} and {lo:,} — a {out / hi:.0f}x blow-up. This usually "
                              f"means a non-unique join key on both sides (an accidental "
                              f"many-to-many / near-cartesian join). Check the join keys and "
                              f"whether one side needs de-duplicating first."),
                        evidence={"rows_out": out, "rows_in_max": hi, "rows_in_min": lo,
                                  "anomaly": "explosion"},
                    ))
                elif lo >= jc_floor and out <= lo * jc_ratio:
                    findings.append(_mk_finding(
                        kind=FindingKind.JOIN_COLLAPSE, severity=FindingSeverity.P2,
                        workspace_id=workspace_id, wf_id=wf_id, step_id=str(step_id),
                        run_id=run_id, label=str(label),
                        title=f"Join dropped almost everything in {label} ({lo:,} → {out:,})",
                        body=(f"The join **{label}** kept only **{out:,}** of {lo:,} rows. An "
                              f"inner join that matches almost nothing usually means a key "
                              f"mismatch (type, casing, trimming, or the wrong column). Verify "
                              f"the join keys line up, or switch to a left join if non-matches "
                              f"should be kept."),
                        evidence={"rows_out": out, "rows_in_max": max(in_counts),
                                  "rows_in_min": lo, "anomaly": "collapse"},
                    ))
            elif stype == "deduplicate":
                src_rows = in_counts[0]
                if src_rows >= dd_floor and out <= src_rows * dd_ratio:
                    findings.append(_mk_finding(
                        kind=FindingKind.DEDUPE_COLLAPSE, severity=FindingSeverity.P2,
                        workspace_id=workspace_id, wf_id=wf_id, step_id=str(step_id),
                        run_id=run_id, label=str(label),
                        title=f"Deduplicate removed almost everything in {label} ({src_rows:,} → {out:,})",
                        body=(f"Deduplicate **{label}** kept only **{out:,}** of {src_rows:,} "
                              f"rows (removed {100 * (1 - out / src_rows):.0f}%). Removing this "
                              f"much usually means the dedupe key is too coarse — e.g. keying "
                              f"on a column that repeats across genuinely-distinct rows. Check "
                              f"the key columns."),
                        evidence={"rows_out": out, "rows_in": src_rows, "anomaly": "dedupe_collapse"},
                    ))
            elif stype == "filter":
                src_rows = in_counts[0]
                if src_rows >= flt_floor and out == 0:
                    findings.append(_mk_finding(
                        kind=FindingKind.FILTER_DROPPED_ALL, severity=FindingSeverity.P3,
                        workspace_id=workspace_id, wf_id=wf_id, step_id=str(step_id),
                        run_id=run_id, label=str(label),
                        title=f"Filter removed every row in {label} ({src_rows:,} → 0)",
                        body=(f"Filter **{label}** dropped all {src_rows:,} input rows. If an "
                              f"empty result is expected this run, dismiss this; otherwise the "
                              f"predicate is likely too strict or references the wrong column / "
                              f"value."),
                        evidence={"rows_out": 0, "rows_in": src_rows, "anomaly": "filter_dropped_all"},
                    ))
        except Exception:  # noqa: BLE001 — per-step best-effort
            continue

    for f in findings:
        finding_store.append(f)
    return findings


def detect_node_cardinality(
    finding_store: NodeCardinalityFindingStore,
    *,
    workspace_id: str = "default",
    suppressed_signatures: set[str] | None = None,
) -> list[StewardFinding]:
    """Return open cardinality findings, after suppression. Detection happens
    in record_node_cardinality at run-ingest time."""
    return finding_store.open_findings(suppressed_signatures or set())


__all__ = [
    "NodeCardinalityFindingStore",
    "record_node_cardinality",
    "detect_node_cardinality",
    "THRESHOLD_SPEC",
]
