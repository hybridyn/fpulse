"""HTTP surface for the F-Pulse Steward.

Endpoints:
  GET  /api/steward/findings        — list current findings (default: open)
  POST /api/steward/scan            — force-rescan now
  POST /api/steward/findings/{id}/dismiss — user marks intentional
  POST /api/steward/findings/{id}/resolve — user took action that fixes it
  GET  /api/steward/settings        — current per-workspace settings
  PUT  /api/steward/settings        — update settings (partial body OK)
  GET  /api/steward/memory          — durable learning log (audit trail)
  GET  /api/steward/memory/stats    — aggregate counters for the Memory tab

The Steward is **read-only** at the workflow / connection layer — it
never mutates pipeline definitions. Dismissal + resolution write to its
own suppression store, not to user-managed objects.

OSS-default: the full Steward ships in OSS. F-Pulse+ Plus adds
cross-workspace correlation + shared memory + RBAC-aware approval
chains — but the deterministic finding-detection capability is OSS.
That positioning is the whole reason this module exists; see
``docs/steward/overview.md``.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from fpulse.auth.deps import current_workspace_id, require_auth
from fpulse.steward import (
    StewardFinding,
    FindingKind,
    FindingStatus,
    SettingsStore,
    StewardMemory,
    StewardSettings,
    apply_learning,
    Column,
    ConnectorHealthStore,
    CostEvent,
    CostEventStore,
    CostFindingStore,
    GovernancePolicy,
    GovernancePolicyStore,
    PIIFindingStore,
    QualityAssertion,
    QualityCheckReport,
    QualityFindingStore,
    NodeCardinalityFindingStore,
    RowDeltaFindingStore,
    SchemaDriftFindingStore,
    SchemaSnapshot,
    SchemaSnapshotStore,
    detect_connector_health,
    detect_cost_findings,
    detect_duplicate_sources,
    detect_governance,
    detect_node_cardinality,
    detect_orphaned_tables,
    detect_pii_findings,
    detect_quality_findings,
    detect_row_deltas,
    detect_schema_drift,
    detect_volume_anomalies,
    evaluate_rules,
    load_rules,
    new_scan_id,
    record_cost_event,
    record_pii_findings,
    record_quality_report,
    record_snapshot,
    record_test_outcome,
    sanitize_user_note,
    summarise_by_source,
)
# 2026-06-05 — F-Pulse Memory Layer (durable lessons surface). See
# docs/steward/memory-layer.md for the user-facing description and
# backend/fpulse/steward/lessons.py for the storage contract.
from fpulse.steward.lessons import (
    EvidenceRef,
    LessonStatus,
    LessonStore,
    LessonType,
    MemoryLesson,
)
# 2026-06-05 — Steward → notification bell bridge. Imports lazily inside
# the helper so we don't form a hard module-load dependency on the
# notifications package (the Steward must remain useful if the
# notifications package is ever stripped down for embedded builds).
from fpulse.steward.notifier import (
    emit_steward_notifications,
    mark_finding_notifications_read,
)


router = APIRouter(prefix="/api/steward", tags=["steward"])


# ── Suppression store ───────────────────────────────────────────────
# Per-workspace persistent record of finding signatures the user has
# explicitly marked as "intentional, don't flag again."
_FILE_LOCK = threading.Lock()


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except Exception:
        return "default"


def _steward_dir() -> Path:
    from fpulse.main import app_state
    base = Path(app_state["data_dir"]) / "steward"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _workspace_dir(workspace_id: str) -> Path:
    ws_dir = _steward_dir() / workspace_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    return ws_dir


def _suppressions_path(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / "suppressions.json"


def _memory_path(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / "memory.jsonl"


def _settings_path(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / "settings.json"


def _rules_dir(workspace_id: str) -> Path:
    """Per-workspace user-rules directory. One YAML file per rule. Admins
    edit these on disk (or, in Plus, via the in-app authoring UI which
    writes here). The directory is auto-created on first access so an
    OSS user dropping in their first rule doesn't need to mkdir first."""
    rules_dir = _workspace_dir(workspace_id) / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    return rules_dir


def _connector_health_path(workspace_id: str) -> Path:
    """Per-workspace connector-health state file. Single JSON keyed by
    connection_id - tracks failure streak / first-failure timestamp /
    classified error class beyond what the Connection table itself
    persists."""
    return _workspace_dir(workspace_id) / "connector_health.json"


def _get_connector_health_store(workspace_id: str) -> ConnectorHealthStore:
    return ConnectorHealthStore(_connector_health_path(workspace_id))


def _schemas_dir(workspace_id: str) -> Path:
    """Per-workspace schema-snapshot directory. One file per source
    signature - holds the LATEST snapshot, used as the baseline for
    the next drift comparison."""
    d = _workspace_dir(workspace_id) / "schemas"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_schema_snapshot_store(workspace_id: str) -> SchemaSnapshotStore:
    return SchemaSnapshotStore(_schemas_dir(workspace_id))


def _schema_drift_findings_path(workspace_id: str) -> Path:
    """Per-workspace journal of every schema-drift finding ever emitted.
    Append-only JSONL - the scan reads from here to re-surface open
    drift findings on every scan even though the diff event itself
    only happened once."""
    return _workspace_dir(workspace_id) / "schema_drift_findings.jsonl"


def _get_schema_drift_finding_store(workspace_id: str) -> SchemaDriftFindingStore:
    return SchemaDriftFindingStore(_schema_drift_findings_path(workspace_id))


def _get_row_delta_finding_store(workspace_id: str) -> RowDeltaFindingStore:
    """Per-workspace journal of row-count integrity findings — same
    append-only pattern as schema drift; written at run-ingest time."""
    return RowDeltaFindingStore(_workspace_dir(workspace_id) / "row_delta_findings.jsonl")


def _get_node_cardinality_finding_store(workspace_id: str) -> NodeCardinalityFindingStore:
    """Per-workspace journal of node cardinality anomalies (join explosion /
    collapse, dedupe over-removal, filter-dropped-all). Run-fed."""
    return NodeCardinalityFindingStore(
        _workspace_dir(workspace_id) / "node_cardinality_findings.jsonl"
    )


def _quality_findings_path(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / "quality_findings.jsonl"


def _get_quality_finding_store(workspace_id: str) -> QualityFindingStore:
    return QualityFindingStore(_quality_findings_path(workspace_id))


def _governance_policy_path(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / "governance.json"


def _get_governance_policy_store(workspace_id: str) -> GovernancePolicyStore:
    return GovernancePolicyStore(_governance_policy_path(workspace_id))


def _cost_events_path(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / "cost_events.jsonl"


def _cost_findings_path(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / "cost_findings.jsonl"


def _get_cost_event_store(workspace_id: str) -> CostEventStore:
    return CostEventStore(_cost_events_path(workspace_id))


def _get_cost_finding_store(workspace_id: str) -> CostFindingStore:
    return CostFindingStore(_cost_findings_path(workspace_id))


def _pii_findings_path(workspace_id: str) -> Path:
    return _workspace_dir(workspace_id) / "pii_findings.jsonl"


def _get_pii_finding_store(workspace_id: str) -> PIIFindingStore:
    return PIIFindingStore(_pii_findings_path(workspace_id))


def _lessons_dir(workspace_id: str) -> Path:
    """Per-workspace Memory-Layer directory. One YAML + one JSON file
    per lesson — see backend/fpulse/steward/lessons.py for the layout
    rationale."""
    d = _workspace_dir(workspace_id) / "lessons"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_lesson_store(workspace_id: str) -> LessonStore:
    return LessonStore(_lessons_dir(workspace_id))


def _get_memory(workspace_id: str) -> StewardMemory:
    return StewardMemory(_memory_path(workspace_id))


def _get_settings_store(workspace_id: str) -> SettingsStore:
    return SettingsStore(_settings_path(workspace_id))


def _load_suppressions(workspace_id: str) -> dict[str, Any]:
    path = _suppressions_path(workspace_id)
    if not path.is_file():
        return {"suppressed_signatures": [], "history": []}
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
        data.setdefault("suppressed_signatures", [])
        data.setdefault("history", [])
        return data
    except (OSError, json.JSONDecodeError):
        return {"suppressed_signatures": [], "history": []}


def _save_suppressions(workspace_id: str, data: dict[str, Any]) -> None:
    path = _suppressions_path(workspace_id)
    with _FILE_LOCK:
        with path.open("w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2)


def _workflows_for_scan(workspace_id: str) -> list[dict[str, Any]]:
    from fpulse.api.workflows import get_store
    store = get_store()
    rows = store.list_all(workspace_id=workspace_id) or []
    out: list[dict[str, Any]] = []
    for row in rows:
        if hasattr(row, "model_dump"):
            d = row.model_dump()
        elif isinstance(row, dict):
            d = row
        else:
            continue
        nodes = (
            d.get("nodes")
            or (d.get("graph") or {}).get("nodes")
            or d.get("steps")
            or []
        )
        out.append({
            "id": d.get("id") or "",
            "name": d.get("name") or "",
            "nodes": nodes,
        })
    return out


def _run_scan(workspace_id: str, *, record: bool = True) -> tuple[list[StewardFinding], StewardSettings]:
    """Single entry point for "run all Steward sub-agents and apply
    learned-from-history adjustments." Returns the post-learning
    findings + the settings used so callers can filter by min_severity
    consistently. When ``record`` is True (default), every emit is
    logged to the memory journal so persistent occurrence counts grow
    across scans."""
    settings = _get_settings_store(workspace_id).load()
    if not settings.enabled:
        return [], settings

    workflows = _workflows_for_scan(workspace_id)
    suppressions = _load_suppressions(workspace_id)
    suppressed = set(suppressions.get("suppressed_signatures") or [])
    findings = detect_duplicate_sources(
        workflows,
        workspace_id=workspace_id,
        suppressed_signatures=suppressed,
    )

    # 2026-06-07 — user-defined YAML rules run on the same workflow
    # snapshot, producing FindingKind.USER_DEFINED records. They flow
    # through the same downstream surface (apply_learning for escalation
    # + rebound, suppression filtering, notification de-dup, UI) so
    # admins get the same alert-fatigue guarantees on their own rules
    # as the built-in Archeologist findings.
    user_rules, _rule_errors = load_rules(_rules_dir(workspace_id))
    user_findings = evaluate_rules(workflows, user_rules, workspace_id=workspace_id)
    # Apply the same suppression set — admins can dismiss user-rule
    # findings exactly like built-in ones.
    user_findings = [
        f for f in user_findings
        if f.evidence.get("source_signature") not in suppressed
    ]
    findings.extend(user_findings)

    # 2026-06-07 — connector-health detector. First CONNECTOR-level
    # detector to ship; activates CONNECTOR_AUTH_FAILURE,
    # CONNECTOR_RATE_LIMIT, CONNECTOR_UNREACHABLE, and
    # CREDENTIAL_NEAR_EXPIRY. Reads from the health-state sidecar
    # populated by the test-connection endpoint (or external POSTs
    # to /api/steward/connector-health). Same suppression semantics
    # as everything else - dismiss-with-reason silences a single
    # (connection, kind) pair without taking down the whole detector.
    try:
        from fpulse.api.connections import get_store as _get_connection_store
        conn_store = _get_connection_store()
        connection_rows = conn_store.list_all(workspace_id=workspace_id) or []
        connections_for_health = []
        for row in connection_rows:
            if hasattr(row, "model_dump"):
                connections_for_health.append(row.model_dump())
            elif isinstance(row, dict):
                connections_for_health.append(row)
        health_findings = detect_connector_health(
            connections_for_health,
            _get_connector_health_store(workspace_id),
            workspace_id=workspace_id,
            suppressed_signatures=suppressed,
        )
        findings.extend(health_findings)
    except Exception:
        # Connector-health is additive; if its inputs aren't available
        # (e.g. connection store not initialised in a test harness), the
        # rest of the scan must still succeed.
        pass

    # 2026-06-07 — schema-drift detector. Event-driven: actual diff
    # detection happens at POST /schema-snapshot time; the scan path
    # just re-surfaces still-open drift findings from the journal,
    # filtered by suppression.
    try:
        drift_findings = detect_schema_drift(
            _get_schema_drift_finding_store(workspace_id),
            workspace_id=workspace_id,
            suppressed_signatures=suppressed,
        )
        findings.extend(drift_findings)
    except Exception:
        pass

    # 2026-06-18 — row-count integrity. Recorded at run-ingest time
    # (steward/ingest.py → record_row_deltas) when a 1:1 step's row count
    # changed; the scan re-surfaces still-open findings, filtered by
    # suppression. Same event-driven shape as schema drift.
    try:
        row_delta_findings = detect_row_deltas(
            _get_row_delta_finding_store(workspace_id),
            workspace_id=workspace_id,
            suppressed_signatures=suppressed,
        )
        findings.extend(row_delta_findings)
    except Exception:
        pass

    # 2026-06-18 — node cardinality anomalies (join explosion/collapse, dedupe
    # over-removal, filter-dropped-all). Recorded at run-ingest; scan
    # re-surfaces still-open findings, filtered by suppression.
    try:
        node_card_findings = detect_node_cardinality(
            _get_node_cardinality_finding_store(workspace_id),
            workspace_id=workspace_id,
            suppressed_signatures=suppressed,
        )
        findings.extend(node_card_findings)
    except Exception:
        pass

    # 2026-06-18 — storage intelligence: managed tables that no pipeline
    # reads or writes (leftovers). State-derived each scan from the live
    # table listing + the same workflow snapshot the other detectors use.
    try:
        from fpulse.main import app_state
        _ds = app_state.get("datastore")
        _tables = _ds.list_tables(workspace_id) if _ds is not None else []
        orphan_findings = detect_orphaned_tables(
            workflows, _tables,
            workspace_id=workspace_id,
            suppressed_signatures=suppressed,
        )
        findings.extend(orphan_findings)
    except Exception:
        pass

    # 2026-06-07 — native data-quality findings. Same event-driven
    # pattern: POST /quality-check writes assertion-failure findings
    # to the journal; the scan re-surfaces still-open ones.
    try:
        quality_findings = detect_quality_findings(
            _get_quality_finding_store(workspace_id),
            workspace_id=workspace_id,
            suppressed_signatures=suppressed,
        )
        findings.extend(quality_findings)
    except Exception:
        pass

    # 2026-06-07 — governance-level state-derived findings
    # (env_crossing, unapproved_destination). Reads the per-workspace
    # governance.json policy + the same workflow snapshot Archeologist
    # uses. No-op if no policy is configured.
    try:
        policy = _get_governance_policy_store(workspace_id).load()
        gov_findings = detect_governance(
            workflows, policy,
            workspace_id=workspace_id,
            suppressed_signatures=suppressed,
        )
        findings.extend(gov_findings)
    except Exception:
        pass

    # 2026-06-07 — cost-level findings (warehouse_waste today;
    # cost_drift / cost_recommendation deferred to 1.3 Cost Steward).
    # Event-driven recording happens at POST /cost-event; the scan
    # path re-surfaces open findings from the journal.
    try:
        cost_findings = detect_cost_findings(
            _get_cost_finding_store(workspace_id),
            workspace_id=workspace_id,
            suppressed_signatures=suppressed,
        )
        findings.extend(cost_findings)
    except Exception:
        pass

    # 2026-06-08 — foreseer VOLUME_ANOMALY detector. Pure statistical
    # pass over the same CostEvent history (rows_read per source over
    # runs): flags a source whose latest volume breaks from its OWN
    # learned median/MAD baseline (Hard Rule 6 — no absolute thresholds).
    # Recomputed each scan from the event log, so no separate journal.
    try:
        cost_events = _get_cost_event_store(workspace_id).all()
        volume_findings = detect_volume_anomalies(
            cost_events,
            workspace_id=workspace_id,
            suppressed_signatures=suppressed,
        )
        findings.extend(volume_findings)
    except Exception:
        pass

    # 2026-06-08 — PII-leak detector (third governance kind). Rides
    # the schema-snapshot recording path: when a snapshot lands with
    # PII-suggestive column names, a finding is emitted into the PII
    # journal. The scan path re-surfaces still-open findings here.
    try:
        pii_findings = detect_pii_findings(
            _get_pii_finding_store(workspace_id),
            workspace_id=workspace_id,
            suppressed_signatures=suppressed,
        )
        findings.extend(pii_findings)
    except Exception:
        pass

    # 2026-06-18 (Steward rung 1) — per-detector config from the Coverage
    # page. Drop findings whose detector the operator DISABLED, and apply any
    # per-detector SEVERITY override. Done before learning/min-severity/notify
    # so a disabled detector never escalates or pings the bell, and an
    # overridden severity flows through every downstream gate.
    _det_cfg = getattr(settings, "detectors", None) or {}
    if _det_cfg:
        from fpulse.steward.models import FindingSeverity as _Sev
        _kept = []
        for _f in findings:
            _kind = _f.kind.value if hasattr(_f.kind, "value") else str(_f.kind)
            _ov = _det_cfg.get(_kind)
            if _ov is not None:
                if not getattr(_ov, "enabled", True):
                    continue
                _ovsev = getattr(_ov, "severity", None)
                if _ovsev:
                    try:
                        _f.severity = _Sev(_ovsev)
                    except Exception:
                        pass
            _kept.append(_f)
        findings = _kept

    memory = _get_memory(workspace_id)
    findings = apply_learning(
        findings,
        memory,
        escalate_after_n_occurrences=settings.escalate_after_n_occurrences,
        escalate_min_hours_since_first=settings.escalate_min_hours_since_first,
    )

    # Min-severity filter
    sev_rank = {"p1": 3, "p2": 2, "p3": 1}
    min_rank = sev_rank.get(settings.min_severity, 1)
    findings = [f for f in findings if sev_rank.get(f.severity.value, 1) >= min_rank]

    if record:
        scan_id = new_scan_id()
        for f in findings:
            memory.record_emit(scan_id, f)
        # 2026-06-05 — fan out to the notification bell. The notifier's
        # de-dup logic (at-most-one per user+finding+severity+rebound)
        # means re-scans of unchanged findings don't spam the bell;
        # only NEW or NEWLY-ESCALATED ones cross the threshold.
        if settings.notify_on_finding and findings:
            try:
                from fpulse.main import app_state
                emit_steward_notifications(
                    notification_store=app_state.get("notification_store"),
                    user_store=app_state.get("user_store"),
                    workspace_id=workspace_id,
                    findings=findings,
                    min_severity=settings.notify_min_severity,
                )
            except Exception:
                # Never block the scan response on notification persistence
                pass

    return findings, settings


# ── Findings endpoints ──────────────────────────────────────────────

@router.get("/findings", dependencies=[Depends(require_auth)])
async def list_findings(
    request: Request,
    status: str = "open",
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    findings, _settings = _run_scan(workspace_id)
    if status and status != "all":
        findings = [f for f in findings if f.status.value == status]
    return {
        "workspace_id": workspace_id,
        "count": len(findings),
        "findings": [f.model_dump(mode="json") for f in findings],
    }


@router.post("/scan", dependencies=[Depends(require_auth)])
async def force_scan(
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    findings, _settings = _run_scan(workspace_id)
    return {
        "workspace_id": workspace_id,
        "scanned": True,
        "count": len(findings),
    }


@router.get("/coverage", dependencies=[Depends(require_auth)])
async def coverage(
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Proof-of-coverage: WHAT the Steward watches + live counts.

    The detector list is the real active-detector registry
    (steward/coverage.py — only detectors that actually run). The counts
    are computed from the current scan, so this page can never claim
    coverage the engine doesn't have.
    """
    from datetime import datetime, timezone
    from fpulse.steward.coverage import coverage_detectors

    findings, settings = _run_scan(workspace_id, record=False)
    open_findings = [f for f in findings if f.status.value == "open"]

    by_severity: dict[str, int] = {"p1": 0, "p2": 0, "p3": 0}
    by_level: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for f in open_findings:
        sev = f.severity.value if hasattr(f.severity, "value") else str(f.severity)
        by_severity[sev] = by_severity.get(sev, 0) + 1
        lvl = f.level.value if hasattr(f.level, "value") else str(f.level)
        by_level[lvl] = by_level.get(lvl, 0) + 1
        knd = f.kind.value if hasattr(f.kind, "value") else str(f.kind)
        by_kind[knd] = by_kind.get(knd, 0) + 1

    detectors = coverage_detectors()
    det_cfg = getattr(settings, "detectors", None) or {}
    from fpulse.steward.node_cardinality import THRESHOLD_SPEC
    for d in detectors:
        d["open"] = by_kind.get(d["kind"], 0)  # live open count per detector
        # Per-detector config (rung 1): is it enabled, and any severity override.
        ov = det_cfg.get(d["kind"])
        d["enabled"] = bool(getattr(ov, "enabled", True)) if ov is not None else True
        d["severity_override"] = getattr(ov, "severity", None) if ov is not None else None
        # Tunable thresholds (rung 1.5): schema + current value (override|default).
        spec = THRESHOLD_SPEC.get(d["kind"])
        if spec:
            ov_thr = (getattr(ov, "thresholds", None) or {}) if ov is not None else {}
            d["thresholds"] = [
                {**s, "value": ov_thr.get(s["key"], s["default"])} for s in spec
            ]

    return {
        "workspace_id": workspace_id,
        "enabled": bool(getattr(settings, "enabled", True)),
        "last_scan": datetime.now(timezone.utc).isoformat(),
        "detector_count": len(detectors),
        "open_total": len(open_findings),
        "by_severity": by_severity,
        "by_level": by_level,
        "detectors": detectors,
    }


@router.post("/findings/{finding_id}/dismiss", dependencies=[Depends(require_auth)])
async def dismiss_finding(
    finding_id: str,
    request: Request,
    body: dict[str, Any] | None = Body(default=None),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Mark a finding as intentional. Optional body ``{"reason": "..."}``
    is recorded in memory for the Curator (1.4) to mine."""
    # Re-derive findings without recording emits — we just need the
    # evidence to extract the signature. Recording would inflate the
    # persistent occurrence counter for a no-op dismiss action.
    findings, _settings = _run_scan(workspace_id, record=False)
    target = next((f for f in findings if f.id == finding_id), None)
    if target is None:
        raise HTTPException(404, f"Finding {finding_id} not found in current scan")

    suppress_key = (
        target.evidence.get("source_signature")
        or target.evidence.get("shape_signature")
    )
    if not suppress_key:
        raise HTTPException(400, f"Finding {finding_id} has no suppressable signature")

    reason = (body or {}).get("reason") if isinstance(body, dict) else None

    data = _load_suppressions(workspace_id)
    if suppress_key not in data["suppressed_signatures"]:
        data["suppressed_signatures"].append(suppress_key)
    data["history"].append({
        "finding_id": finding_id,
        "signature": suppress_key,
        "action": "dismissed",
        "ts": target.last_seen,
        "reason": reason or "",
    })
    _save_suppressions(workspace_id, data)

    _get_memory(workspace_id).record_dismiss(finding_id, suppress_key, reason)

    # 2026-06-05 — clear the bell badge for an issue the user has just
    # acknowledged. Otherwise the unread count keeps a stale +1 for a
    # finding that's already been triaged. Best-effort; swallow errors.
    notifications_marked = 0
    try:
        from fpulse.main import app_state
        notifications_marked = mark_finding_notifications_read(
            notification_store=app_state.get("notification_store"),
            user_store=app_state.get("user_store"),
            workspace_id=workspace_id,
            finding_id=finding_id,
        )
    except Exception:
        pass

    return {
        "dismissed": True,
        "finding_id": finding_id,
        "signature": suppress_key,
        "reason": reason or "",
        "notifications_marked_read": notifications_marked,
    }


_FINDING_KIND_TO_LESSON_TYPE: dict[FindingKind, LessonType] = {
    # 2026-06-07 — when a duplicate finding is resolved (via consolidation
    # OR by confirming it was intentional in some other system context),
    # the right lesson category is DUPLICATE_WARNING — that's the bucket
    # search() will surface next time someone wires up another consumer
    # of the same source.
    FindingKind.DUPLICATE_SOURCE: LessonType.DUPLICATE_WARNING,
    FindingKind.DUPLICATE_PIPELINE: LessonType.DUPLICATE_WARNING,
    # Every other finding kind falls through to USER_FIX (see
    # `_lesson_type_for_finding`), so the resolve->lesson loop works for any
    # kind without code changes; we refine this mapping as more specific
    # lesson categories make sense per detector.
}


def _lesson_type_for_finding(kind: FindingKind) -> LessonType:
    """Map a finding kind to the lesson category the Memory Layer
    should file the fix under. Falls back to USER_FIX for any kind
    not yet explicitly mapped - the resolve→lesson loop stays working
    even for future detectors whose mapping hasn't been decided yet."""
    return _FINDING_KIND_TO_LESSON_TYPE.get(kind, LessonType.USER_FIX)


@router.post("/findings/{finding_id}/resolve", dependencies=[Depends(require_auth)])
async def resolve_finding(
    finding_id: str,
    request: Request,
    body: dict[str, Any] | None = Body(default=None),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Mark a finding as resolved.

    Optional body ``{"fix_note": "..."}`` captures **what fixed it**.
    When supplied, the note is sanitised (AWS keys / bearer tokens /
    passwords / URI creds / private IPs all redacted) and filed as a
    ``PROPOSED`` lesson in the Memory Layer. The lesson stays inert
    until a human approves it (Rule 3: Learning is gated) - this is
    the organic feeder for the lesson store that 1.1 was missing.

    Without ``fix_note`` the endpoint behaves exactly as before: marks
    resolved, records to journal, clears notifications, no lesson.
    """
    findings, _settings = _run_scan(workspace_id, record=False)
    target = next((f for f in findings if f.id == finding_id), None)
    sig = None
    if target is not None:
        sig = (
            target.evidence.get("source_signature")
            or target.evidence.get("shape_signature")
        )
    data = _load_suppressions(workspace_id)
    data["history"].append({
        "finding_id": finding_id,
        "signature": sig or "",
        "action": "resolved",
        "ts": (target.last_seen if target else ""),
    })
    _save_suppressions(workspace_id, data)
    _get_memory(workspace_id).record_resolve(finding_id, sig)

    # 2026-06-07 — optional fix_note → PROPOSED lesson candidate.
    # Best-effort: lesson-store failures don't fail the resolve.
    lesson_id: str | None = None
    raw_fix_note = (body or {}).get("fix_note") if isinstance(body, dict) else None
    if raw_fix_note and target is not None:
        try:
            sanitized = sanitize_user_note(raw_fix_note).strip()
            if sanitized:
                # Best-effort user identity (same pattern as approve_lesson).
                proposer = "user"
                try:
                    user = getattr(request.state, "user", None)
                    if user is not None:
                        proposer = getattr(user, "email", "") or "user"
                except Exception:
                    pass
                # Pull source/pipeline labels from the finding evidence
                # for the lesson's source/pipeline fields (lesson search
                # uses these to match future failures back to the lesson).
                source_label = (
                    target.evidence.get("source_object")
                    or target.evidence.get("source_signature")
                    or ""
                )
                pipeline_names = target.evidence.get("workflows") or []
                pipeline_label = pipeline_names[0] if pipeline_names else ""

                lesson = _get_lesson_store(workspace_id).propose(
                    workspace_id=workspace_id,
                    source=str(source_label),
                    pipeline=str(pipeline_label),
                    lesson_type=_lesson_type_for_finding(target.kind),
                    issue=target.title,
                    # StewardFinding's prose explainer is `body`, not
                    # `description`. We pull a 1-line symptom from the
                    # first line so the lesson YAML stays readable.
                    symptom=(target.body or "").split("\n", 1)[0],
                    approved_fix=sanitized,
                    proposed_by=proposer,
                    evidence=[EvidenceRef(
                        kind="finding",
                        id=finding_id,
                        note=f"kind={target.kind.value}, severity={target.severity.value}",
                    )],
                )
                lesson_id = lesson.id
        except Exception:
            # Don't fail the resolve over a lesson-store hiccup; the
            # core action (mark resolved, journal, clear bell) already
            # succeeded above.
            lesson_id = None

    # Also clear the bell badge — same reason as dismiss above.
    notifications_marked = 0
    try:
        from fpulse.main import app_state
        notifications_marked = mark_finding_notifications_read(
            notification_store=app_state.get("notification_store"),
            user_store=app_state.get("user_store"),
            workspace_id=workspace_id,
            finding_id=finding_id,
        )
    except Exception:
        pass
    return {
        "resolved": True,
        "finding_id": finding_id,
        "signature": sig or "",
        "notifications_marked_read": notifications_marked,
        "lesson_id": lesson_id,
        "lesson_status": "proposed" if lesson_id else None,
    }


# ── User-defined rules endpoints ────────────────────────────────────
# OSS Free (rung 2, 2026-06-18): DECLARATIVE rules can be authored in-app
# (POST/DELETE below) and are stored as the exact same YAML files an
# operator could hand-edit or GitOps — the form is just one way to write
# them. The Plus escape hatch is SQL/expression rules over a read-only
# metadata view; those are NOT accepted by this endpoint.

@router.get("/rules", dependencies=[Depends(require_auth)])
async def list_user_rules(
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Return every user-rule loaded for this workspace + any parse
    errors. Surface the errors at the UI so admins see WHY a rule
    isn't taking effect (rather than the rule silently being
    skipped)."""
    rules, errors = load_rules(_rules_dir(workspace_id))
    return {
        "workspace_id": workspace_id,
        "rules_dir": str(_rules_dir(workspace_id)),
        "count": len(rules),
        "rules": [r.model_dump(mode="json") for r in rules],
        "errors": [e.model_dump() for e in errors],
    }


@router.post("/rules", dependencies=[Depends(require_auth)])
async def create_user_rule(
    request: Request,
    body: dict[str, Any] = Body(...),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Create or replace a DECLARATIVE user rule (rung 2 — OSS Free).

    The body is validated against the UserRule schema, then written as a
    YAML file (`<id>.yaml`) into the workspace rules dir — the same files
    an operator could hand-edit / GitOps. SQL/expression rules are a
    Plus-tier escape hatch and are not accepted here.
    """
    import yaml as _yaml
    from fpulse.steward.rules import UserRule
    try:
        rule = UserRule.model_validate(body)
    except Exception as e:
        raise HTTPException(400, f"Invalid rule: {e}")
    rdir = _rules_dir(workspace_id)
    rdir.mkdir(parents=True, exist_ok=True)
    try:
        with (rdir / f"{rule.id}.yaml").open("w", encoding="utf-8") as fp:
            _yaml.safe_dump(
                rule.model_dump(mode="json", exclude_none=True),
                fp, sort_keys=False, allow_unicode=True,
            )
    except OSError as e:
        raise HTTPException(500, f"Could not write rule: {e}")
    return {"workspace_id": workspace_id, "saved": rule.id,
            "rule": rule.model_dump(mode="json")}


@router.delete("/rules/{rule_id}", dependencies=[Depends(require_auth)])
async def delete_user_rule(
    request: Request,
    rule_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Delete a user-rule file. Guards the id against path traversal — a
    DELETE path segment is attacker-controlled, so only a bare rule id
    (same pattern create enforces) is accepted."""
    import re as _re
    if not _re.match(r"^[a-z0-9][a-z0-9_-]{0,62}$", rule_id or ""):
        raise HTTPException(400, "invalid rule id")
    rdir = _rules_dir(workspace_id)
    removed = False
    for ext in (".yaml", ".yml"):
        p = rdir / f"{rule_id}{ext}"
        if p.is_file():
            try:
                p.unlink()
                removed = True
            except OSError as e:
                raise HTTPException(500, f"Could not delete rule: {e}")
    if not removed:
        raise HTTPException(404, f"rule {rule_id!r} not found")
    return {"workspace_id": workspace_id, "deleted": rule_id}


# ── Connector-health endpoints ──────────────────────────────────────
# The built-in /api/connections/{id}/test endpoint calls record_test_outcome
# directly on success/failure, so users get health updates for free when
# they click Test. These endpoints exist for OUT-OF-PROCESS recording
# (CI runners, external monitoring tools) + visibility.

@router.get("/connector-health", dependencies=[Depends(require_auth)])
async def list_connector_health(
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Return every recorded connector-health state in this workspace.

    Used by the UI / external dashboards to render per-connection
    status without having to walk the connections list + correlate
    timestamps by hand."""
    states = _get_connector_health_store(workspace_id).all()
    return {
        "workspace_id": workspace_id,
        "count": len(states),
        "states": [s.model_dump(mode="json") for s in states],
    }


@router.post("/connector-health", dependencies=[Depends(require_auth)])
async def record_connector_health(
    request: Request,
    body: dict[str, Any] = Body(...),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Record a connector-health probe result from an external source.

    Body:
      {
        "connection_id": "conn-abc",
        "ok": true | false,
        "error_message": "...",          # required when ok=false
        "latency_ms": 250,                # optional
        "credential_expires_at": "..."    # optional ISO-8601
      }

    Same recorder the built-in test_connection endpoint calls. Lets
    a CI runner or external monitor push health updates without
    F-Pulse being the one running the probe."""
    connection_id = body.get("connection_id")
    if not connection_id:
        raise HTTPException(400, "connection_id is required")
    if "ok" not in body:
        raise HTTPException(400, "ok (bool) is required")
    new_state = record_test_outcome(
        _get_connector_health_store(workspace_id),
        connection_id=str(connection_id),
        ok=bool(body["ok"]),
        error_message=str(body.get("error_message") or ""),
        latency_ms=body.get("latency_ms"),
        credential_expires_at=body.get("credential_expires_at"),
    )
    return {"recorded": True, "state": new_state.model_dump(mode="json")}


# ── Schema-drift endpoints ──────────────────────────────────────────
# Event-driven detector: the POST endpoint records a current schema for
# a source; if it differs from the previous snapshot for that source,
# a SCHEMA_DRIFT finding is appended to the per-workspace journal and
# surfaces in /findings on the next scan.
#
# Three change classes (per docs/steward/schema-drift.md):
#   * ADDED         — new column appeared (P3)
#   * DROPPED       — column gone (P1)
#   * TYPE_CHANGED  — same name, different type (P1)
#
# Worst-case wins: any drop or type_change in the diff escalates the
# whole finding to P1 regardless of how many low-sev additions are
# bundled alongside.

@router.post("/schema-snapshot", dependencies=[Depends(require_auth)])
async def record_schema_snapshot(
    request: Request,
    body: dict[str, Any] = Body(...),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Record a current schema for a source. If it differs from the
    previous snapshot, a SCHEMA_DRIFT finding is emitted.

    Body:
      {
        "source_signature": "abc123...",        // required - same key as Archeologist
        "source_label":     "orders_csv",       // optional - human-readable
        "columns": [{"name": "id", "type": "int"}, ...],   // required
        "run_id":           "exec-9876"         // optional - links to a pipeline run
      }

    Returns:
      {
        "recorded":       true,
        "drift_detected": true | false,
        "changes":        [...]   // only when drift detected
        "finding_id":     "..."   // only when drift detected
      }
    """
    sig = body.get("source_signature")
    if not sig:
        raise HTTPException(400, "source_signature is required")
    raw_cols = body.get("columns")
    if not isinstance(raw_cols, list):
        raise HTTPException(400, "columns must be a list of {name, type}")
    try:
        columns = [Column.model_validate(c) for c in raw_cols]
    except Exception as e:
        raise HTTPException(400, f"invalid columns: {e}")

    snapshot = SchemaSnapshot(
        source_signature=str(sig),
        source_label=str(body.get("source_label") or ""),
        columns=columns,
        run_id=str(body.get("run_id") or ""),
    )
    _saved, changes, finding = record_snapshot(
        _get_schema_snapshot_store(workspace_id),
        _get_schema_drift_finding_store(workspace_id),
        snapshot,
        workspace_id=workspace_id,
    )
    out: dict[str, Any] = {
        "recorded": True,
        "drift_detected": bool(finding),
    }
    if finding is not None:
        out["finding_id"] = finding.id
        out["changes"] = [c.model_dump() for c in changes]

    # 2026-06-08 — also run the name-based PII detector against the
    # incoming snapshot. Emits a separate PII_LEAK finding (governance
    # level) when any column name matches a known PII pattern.
    # Best-effort: a PII-store hiccup must not fail the snapshot record.
    try:
        pii_finding = record_pii_findings(
            snapshot,
            _get_pii_finding_store(workspace_id),
            workspace_id=workspace_id,
        )
        if pii_finding is not None:
            out["pii_finding_id"] = pii_finding.id
            out["pii_columns"] = pii_finding.evidence.get("pii_classes_present", [])
    except Exception:
        pass

    return out


@router.get("/schema-snapshots", dependencies=[Depends(require_auth)])
async def list_schema_snapshots(
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Return every stored schema snapshot in this workspace (latest
    per source). Used by the UI / external tools to render the current
    schema baseline before pushing the next snapshot."""
    snaps = _get_schema_snapshot_store(workspace_id).all()
    return {
        "workspace_id": workspace_id,
        "count": len(snaps),
        "snapshots": [s.model_dump(mode="json") for s in snaps],
    }


# ── Data-quality check endpoint ─────────────────────────────────────
# Native check support. External runners (F-Pulse executor, dbt test,
# Great Expectations checkpoint, Soda scan) post assertion results
# here; failed assertions become findings flowing through the same
# surface as every other detector. F-Pulse does NOT evaluate the
# assertions — that's the runner's job.

@router.post("/quality-check", dependencies=[Depends(require_auth)])
async def record_quality_check(
    request: Request,
    body: dict[str, Any] = Body(...),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Record the result of one or more data-quality assertions.

    Body:
      {
        "source_signature": "...",            // required - same key Archeologist uses
        "source_label":     "orders",         // optional
        "run_id":           "exec-1234",      // optional
        "assertions": [
          {
            "check":        "not_null",       // see quality.py for supported checks
            "column":       "customer_id",    // optional, depends on check
            "failed_count": 5,                // 0 = passed; > 0 = failed → finding
            "total_rows":   10000,            // optional, used for severity scaling
            "message":      "Investigated 2026-06-07: bug in upstream loader"  // optional
          },
          ...
        ]
      }
    """
    sig = body.get("source_signature")
    if not sig:
        raise HTTPException(400, "source_signature is required")
    raw_asserts = body.get("assertions")
    if not isinstance(raw_asserts, list):
        raise HTTPException(400, "assertions must be a list")
    try:
        report = QualityCheckReport(
            source_signature=str(sig),
            source_label=str(body.get("source_label") or ""),
            run_id=str(body.get("run_id") or ""),
            assertions=[QualityAssertion.model_validate(a) for a in raw_asserts],
        )
    except Exception as e:
        raise HTTPException(400, f"invalid payload: {e}")
    emitted = record_quality_report(
        _get_quality_finding_store(workspace_id),
        report,
        workspace_id=workspace_id,
    )
    return {
        "recorded": True,
        "assertions_total": len(report.assertions),
        "findings_emitted": len(emitted),
        "finding_ids": [f.id for f in emitted],
    }


# ── Cost / movement tracking endpoints ──────────────────────────────
# Event-driven recording surface (P5 of the reviewer audit). External
# runners post per-pipeline-run cost events; F-Pulse stores them and
# emits WAREHOUSE_WASTE findings on consecutive zero-output runs.
# COST_DRIFT + COST_RECOMMENDATION + REDUNDANT_TRANSFER deferred to
# the 1.3 Cost Steward module (need real baseline machinery).

@router.post("/cost-event", dependencies=[Depends(require_auth)])
async def record_cost_event_endpoint(
    request: Request,
    body: dict[str, Any] = Body(...),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Record one per-run cost event. Body:
        {
          "run_id":              "exec-1234",        // optional but recommended
          "pipeline_id":         "pl-abc",
          "pipeline_name":       "Daily orders ETL",
          "source_signature":    "abc...",           // either source OR sink required
          "sink_signature":      "def...",
          "rows_read":           10000,
          "rows_written":        9876,
          "bytes_read":          1234567,            // 0 = unknown
          "bytes_written":       1234567,
          "duration_ms":         4523,
          "started_at":          "2026-06-07T10:00:00Z",
          "completed_at":        "2026-06-07T10:00:04Z"
        }

    A WAREHOUSE_WASTE finding fires when the last 3 events from the
    same source_signature all have rows_read > 0 AND rows_written = 0."""
    try:
        event = CostEvent.model_validate(body)
    except Exception as e:
        raise HTTPException(400, f"invalid cost event: {e}")
    # Need SOME anchor: source / sink / node-in-workflow. A node_id
    # alone (without workflow_id) wouldn't anchor either - the
    # node-level streak is keyed on the (workflow_id, node_id) pair.
    has_anchor = bool(
        event.source_signature or event.sink_signature
        or (event.node_id and event.workflow_id)
    )
    if not has_anchor:
        raise HTTPException(
            400,
            "event needs at least one anchor: source_signature, "
            "sink_signature, or (workflow_id + node_id)",
        )
    emitted = record_cost_event(
        _get_cost_event_store(workspace_id),
        _get_cost_finding_store(workspace_id),
        event,
        workspace_id=workspace_id,
    )
    return {
        "recorded": True,
        "findings_emitted": len(emitted),
        "finding_ids": [f.id for f in emitted],
        # Back-compat — earlier shipped shape was `finding_emitted` (bool)
        # + single `finding_id`. Keep those for any caller already
        # pinned to that shape; new callers should use the plural keys.
        "finding_emitted": len(emitted) > 0,
        "finding_id": emitted[0].id if emitted else None,
    }


@router.get("/cost-summary", dependencies=[Depends(require_auth)])
async def get_cost_summary(
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Per-source aggregation of all recorded cost events. Useful for
    a 'top 10 most expensive sources' view in the UI / external dashboards."""
    events = _get_cost_event_store(workspace_id).all()
    summary = summarise_by_source(events)
    return {
        "workspace_id": workspace_id,
        "event_count": len(events),
        "source_count": len(summary),
        "by_source": summary,
    }


# ── Governance policy endpoints ─────────────────────────────────────
# Per-workspace governance.json controls env_crossing + unapproved_
# destination detectors. Both default to OFF (empty maps) — admins
# opt in by setting env_tags / approved_destinations.

@router.get("/governance", dependencies=[Depends(require_auth)])
async def get_governance_policy(
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    policy = _get_governance_policy_store(workspace_id).load()
    return {"workspace_id": workspace_id, "policy": policy.model_dump()}


@router.put("/governance", dependencies=[Depends(require_auth)])
async def update_governance_policy(
    request: Request,
    body: dict[str, Any] = Body(...),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Replace the workspace governance policy. Body shape:
        {
          "env_tags":              {"conn-id": "dev", ...},
          "approved_destinations": ["conn-id-1", "conn-id-2"]
        }
    Empty maps disable the respective detector."""
    try:
        policy = GovernancePolicy.model_validate(body)
    except Exception as e:
        raise HTTPException(400, f"invalid governance policy: {e}")
    _get_governance_policy_store(workspace_id).save(policy)
    return {"saved": True, "policy": policy.model_dump()}


# ── Settings endpoints ──────────────────────────────────────────────

@router.get("/settings", dependencies=[Depends(require_auth)])
async def get_settings(
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    s = _get_settings_store(workspace_id).load()
    return {"workspace_id": workspace_id, "settings": s.model_dump()}


@router.put("/settings", dependencies=[Depends(require_auth)])
async def update_settings(
    request: Request,
    body: dict[str, Any] = Body(...),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Partial-update friendly: pass only the fields you want to change.
    Server merges them on top of the current persisted settings, then
    re-validates the merged object so a malformed PUT can't leave the
    file in an unparseable state."""
    store = _get_settings_store(workspace_id)
    current = store.load().model_dump()
    current.update({k: v for k, v in body.items() if k in StewardSettings.model_fields})
    try:
        merged = StewardSettings.model_validate(current)
    except Exception as e:
        raise HTTPException(400, f"Invalid settings: {e}")
    store.save(merged)
    return {"workspace_id": workspace_id, "settings": merged.model_dump()}


# ── Memory / audit-trail endpoints ──────────────────────────────────

@router.get("/memory", dependencies=[Depends(require_auth)])
async def get_memory(
    request: Request,
    limit: int = 100,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Recent events from the durable learning log. Newest first.
    Backs the 'Memory' tab in the Steward UI — gives the user real
    evidence that the Steward IS learning from their dismisses /
    resolves / repeat-emits over time."""
    mem = _get_memory(workspace_id)
    return {
        "workspace_id": workspace_id,
        "events": mem.audit_trail(limit=max(1, min(limit, 1000))),
        "persistent_occurrences": mem.persistent_occurrences(),
    }


@router.get("/memory/stats", dependencies=[Depends(require_auth)])
async def get_memory_stats(
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    return {"workspace_id": workspace_id, "stats": _get_memory(workspace_id).stats()}


# ── F-Pulse Memory Layer — durable lessons ──────────────────────────
#
# These endpoints expose the *Memory Layer*: human-approved lessons
# accumulated over time (source quirks, failure patterns, retry rules,
# etc.). Distinct from the /memory endpoints above, which expose the
# *operational event journal* used by the learning layer for
# escalation + rebound detection.
#
# See `docs/steward/memory-layer.md` for the user-facing description
# and `backend/fpulse/steward/lessons.py` for the storage contract.

@router.get("/lessons", dependencies=[Depends(require_auth)])
async def list_lessons(
    request: Request,
    status: str | None = None,
    lesson_type: str | None = None,
    source: str | None = None,
    pipeline: str | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """List lessons matching the filters. Newest first by
    last_validated. Also auto-ages any APPROVED lessons whose
    `validity_days` has elapsed — cheap + idempotent."""
    store = _get_lesson_store(workspace_id)
    aged = store.age_to_stale()  # opportunistic maintenance
    parsed_status = LessonStatus(status) if status else None
    parsed_type = LessonType(lesson_type) if lesson_type else None
    lessons = store.list_all(
        status=parsed_status,
        lesson_type=parsed_type,
        source=source,
        pipeline=pipeline,
    )
    return {
        "workspace_id": workspace_id,
        "count": len(lessons),
        "auto_aged_this_request": aged,
        "lessons": [L.model_dump(mode="json") for L in lessons],
    }


@router.get("/lessons/stats", dependencies=[Depends(require_auth)])
async def lesson_stats(
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    return {"workspace_id": workspace_id, "stats": _get_lesson_store(workspace_id).stats()}


@router.get("/lessons/{lesson_id}", dependencies=[Depends(require_auth)])
async def get_lesson(
    lesson_id: str,
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    lesson = _get_lesson_store(workspace_id).get(lesson_id)
    if lesson is None:
        raise HTTPException(404, f"Lesson {lesson_id} not found")
    return lesson.model_dump(mode="json")


@router.post("/lessons", dependencies=[Depends(require_auth)])
async def propose_lesson(
    request: Request,
    body: dict[str, Any] = Body(...),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Propose a new lesson. Status starts as PROPOSED — the lesson
    does NOT influence Steward reasoning until a reviewer approves it
    (Rule 3: Learning is gated)."""
    required = ("lesson_type", "issue", "approved_fix")
    missing = [k for k in required if not body.get(k)]
    if missing:
        raise HTTPException(400, f"Missing required fields: {missing}")
    try:
        lt = LessonType(body["lesson_type"])
    except ValueError:
        raise HTTPException(400, f"Invalid lesson_type: {body['lesson_type']!r}")
    evidence = [EvidenceRef.model_validate(e) for e in (body.get("evidence") or [])]
    lesson = _get_lesson_store(workspace_id).propose(
        workspace_id=workspace_id,
        source=body.get("source", ""),
        pipeline=body.get("pipeline", ""),
        lesson_type=lt,
        issue=body["issue"],
        symptom=body.get("symptom", ""),
        root_cause=body.get("root_cause", ""),
        approved_fix=body["approved_fix"],
        proposed_by=body.get("proposed_by", "user"),
        evidence=evidence,
        tags=body.get("tags") or [],
    )
    return lesson.model_dump(mode="json")


@router.post("/lessons/{lesson_id}/approve", dependencies=[Depends(require_auth)])
async def approve_lesson(
    lesson_id: str,
    request: Request,
    body: dict[str, Any] | None = Body(default=None),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Promote PROPOSED → APPROVED. Body: ``{"approver": "..."}``.
    Falls back to the requester's email if not supplied."""
    approver = ""
    if isinstance(body, dict):
        approver = body.get("approver", "") or ""
    if not approver:
        # Best-effort fallback to the authenticated user
        try:
            user = getattr(request.state, "user", None)
            approver = (getattr(user, "email", "") or "unknown") if user else "unknown"
        except Exception:
            approver = "unknown"
    lesson = _get_lesson_store(workspace_id).approve(lesson_id, approver)
    if lesson is None:
        raise HTTPException(404, f"Lesson {lesson_id} not found or not in PROPOSED state")
    return lesson.model_dump(mode="json")


@router.post("/lessons/{lesson_id}/reject", dependencies=[Depends(require_auth)])
async def reject_lesson(
    lesson_id: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Mark a proposal incorrect. Body: ``{"reviewer": "...",
    "reason": "..."}``. Reason is preserved in the lesson's evidence
    trail so the audit history shows why this didn't stick."""
    reviewer = body.get("reviewer") or "unknown"
    reason = body.get("reason") or ""
    lesson = _get_lesson_store(workspace_id).reject(lesson_id, reviewer, reason)
    if lesson is None:
        raise HTTPException(404, f"Lesson {lesson_id} not found")
    return lesson.model_dump(mode="json")


@router.post("/lessons/{lesson_id}/revalidate", dependencies=[Depends(require_auth)])
async def revalidate_lesson(
    lesson_id: str,
    request: Request,
    body: dict[str, Any] | None = Body(default=None),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Re-confirm an APPROVED (or STALE) lesson. Bumps occurrence_count,
    refreshes last_validated, may promote confidence LOW→MEDIUM→HIGH,
    and revives a STALE lesson back to APPROVED. The validity_days
    clock starts over."""
    reviewer = "unknown"
    if isinstance(body, dict) and body.get("reviewer"):
        reviewer = body["reviewer"]
    lesson = _get_lesson_store(workspace_id).revalidate(lesson_id, reviewer)
    if lesson is None:
        raise HTTPException(404, f"Lesson {lesson_id} not found or not in APPROVED/STALE state")
    return lesson.model_dump(mode="json")


@router.post("/lessons/search", dependencies=[Depends(require_auth)])
async def search_lessons_for_failure(
    request: Request,
    body: dict[str, Any] = Body(...),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Step 2 of the 8-step failure → lesson workflow. Given a source
    and an error substring, return matching APPROVED lessons ranked by
    confidence + occurrence_count. Used by the Autopsy sub-agent (1.2)
    and by the editor's failure-helper UI."""
    source = body.get("source", "") or ""
    error_substring = body.get("error", "") or body.get("error_substring", "") or ""
    max_results = int(body.get("max_results", 5))
    matches = _get_lesson_store(workspace_id).search_for_failure(
        source=source,
        error_substring=error_substring,
        max_results=max_results,
    )
    return {
        "workspace_id": workspace_id,
        "query": {"source": source, "error_substring": error_substring},
        "count": len(matches),
        "matches": [L.model_dump(mode="json") for L in matches],
    }


@router.delete("/lessons/{lesson_id}", dependencies=[Depends(require_auth)])
async def delete_lesson(
    lesson_id: str,
    request: Request,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """Hard-delete a lesson. Only suitable for rejected proposals the
    team wants to prune — APPROVED lessons should be marked STALE
    instead so the audit trail survives."""
    deleted = _get_lesson_store(workspace_id).delete(lesson_id)
    if not deleted:
        raise HTTPException(404, f"Lesson {lesson_id} not found")
    return {"deleted": True, "lesson_id": lesson_id}
