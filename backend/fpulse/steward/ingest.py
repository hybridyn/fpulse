"""Best-effort run -> Steward ingestion (2026-06-17).

After a FULL pipeline run completes, record per-node CostEvents (row counts)
and per-source SchemaSnapshots so the Steward's volume-anomaly,
node-empty-output, and schema-drift detectors have REAL data instead of being
permanently empty (the "5 dark detectors" gap).

Hard rules honoured:
  * Out-of-band + fully best-effort — EVERY failure is swallowed so
    observability can never affect a data run (Steward rule #2).
  * Only FULL runs feed the stores. DEV sampled runs (full_run=False) and
    PROD-sandbox runs use capped/scratch row counts that would poison the
    volume baselines, so the executor must not call this for them.
  * Schema snapshots are recorded ONLY when columns are known — an empty
    snapshot would manufacture a false "everything dropped" drift on the next
    populated run.

Stores live under ``<data_dir>/steward/<workspace_id>/`` — the SAME layout the
``api/steward.py`` endpoints read, so the scan surfaces what we record here.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def _ws_dir(data_dir: str, workspace_id: str) -> Path:
    d = Path(data_dir) / "steward" / (workspace_id or "default")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_source(step_type: str) -> bool:
    t = (step_type or "").lower()
    return t == "source" or t.endswith("_source") or "source" in t


def _source_signature(step_type: str, params: dict, workflow_id: str, step_id: str) -> str:
    """Stable id for a source ACROSS runs (so drift + volume baselines line
    up). Built from the identifying params; falls back to (workflow, step)
    when the source is unparameterised."""
    p = params or {}
    parts = [
        str(p.get("connector_type") or step_type or ""),
        str(p.get("connection_id") or ""),
        str(p.get("schema") or ""),
        str(p.get("table") or ""),
        str(p.get("file_path") or p.get("path") or ""),
        str(p.get("query") or "")[:120],
    ]
    ident = "|".join(x for x in parts if x)
    if not ident:
        ident = f"{workflow_id}::{step_id}"
    return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]


def _columns(step_result: Any) -> list[dict]:
    # Prefer schema_info — it carries column TYPES (list of {name, type}).
    # `columns` is names-only (list[str]); reading it first left every
    # type="" and made schema-drift TYPE_CHANGED detection impossible. (2026-06-17)
    raw = (
        getattr(step_result, "schema_info", None)
        or getattr(step_result, "columns", None)
        or []
    )
    out: list[dict] = []
    for c in raw:
        if isinstance(c, dict):
            name = c.get("name") or c.get("column") or c.get("col")
            if name:
                out.append({"name": str(name), "type": str(c.get("type") or "")})
        elif isinstance(c, str):
            out.append({"name": c, "type": ""})
    return out


def _step_type_str(step: Any) -> str:
    st = getattr(step, "type", None)
    if st is None:
        st = getattr(step, "step_type", None)
    if hasattr(st, "value"):
        st = st.value
    return str(st or "")


def record_run(app_state: Any, workflow: Any, run_result: Any) -> int:
    """Record cost + schema observations for a finished FULL run.

    Returns the number of cost events recorded (0 on any failure). NEVER
    raises — the caller is on the run-completion path.
    """
    try:
        if app_state is None or workflow is None or run_result is None:
            return 0
        if getattr(run_result, "status", "") != "success":
            return 0  # only learn from clean runs; partial runs are noise
        data_dir = app_state.get("data_dir") if hasattr(app_state, "get") else None
        if not data_dir:
            return 0
        workspace_id = getattr(workflow, "workspace_id", None) or "default"
        ws = _ws_dir(data_dir, workspace_id)

        from fpulse.steward.cost import (
            CostEvent, CostEventStore, CostFindingStore, record_cost_event,
        )
        from fpulse.steward.schema_drift import (
            Column, SchemaDriftFindingStore, SchemaSnapshot,
            SchemaSnapshotStore, record_snapshot,
        )
        from fpulse.steward.pii import PIIFindingStore, record_pii_findings
        from fpulse.steward.row_delta import RowDeltaFindingStore, record_row_deltas
        from fpulse.steward.node_cardinality import (
            NodeCardinalityFindingStore, record_node_cardinality,
        )

        cost_events = CostEventStore(ws / "cost_events.jsonl")
        cost_findings = CostFindingStore(ws / "cost_findings.jsonl")
        snap_store = SchemaSnapshotStore(ws / "schemas")
        drift_findings = SchemaDriftFindingStore(ws / "schema_drift_findings.jsonl")
        pii_findings = PIIFindingStore(ws / "pii_findings.jsonl")
        row_delta_findings = RowDeltaFindingStore(ws / "row_delta_findings.jsonl")
        node_card_findings = NodeCardinalityFindingStore(ws / "node_cardinality_findings.jsonl")

        wf_id = str(getattr(workflow, "id", "") or "")
        wf_name = str(getattr(workflow, "name", "") or "")
        run_id = str(getattr(run_result, "run_id", "") or "")
        steps = {getattr(s, "id", None): s for s in (getattr(workflow, "steps", []) or [])}
        results = getattr(run_result, "step_results", {}) or {}

        recorded = 0
        for step_id, sr in results.items():
            try:
                if getattr(sr, "status", "") != "success":
                    continue
                step = steps.get(step_id)
                step_type = _step_type_str(step) if step else ""
                label = (getattr(step, "label", "") if step else "") or str(step_id)
                rows = int(getattr(sr, "row_count", 0) or 0)
                dur = int(getattr(sr, "duration_ms", 0) or 0)
                src = _is_source(step_type)
                params = (getattr(step, "params", None) or {}) if step else {}
                sig = _source_signature(step_type, params, wf_id, str(step_id)) if src else ""

                # rows_written = this node's OUTPUT row count (feeds the
                # node-level EMPTY_OUTPUT detector). For sources, rows_read
                # mirrors it (feeds the volume-anomaly baseline). Never set a
                # source's rows_written to 0 — that would mis-fire EMPTY_OUTPUT.
                ev = CostEvent(
                    run_id=run_id,
                    workflow_id=wf_id,
                    workflow_name=wf_name,
                    node_id=str(step_id),
                    node_label=str(label),
                    rows_written=rows,
                    rows_read=rows if src else 0,
                    source_signature=sig,
                    duration_ms=dur,
                )
                record_cost_event(cost_events, cost_findings, ev, workspace_id=workspace_id)
                recorded += 1

                if src:
                    cols = _columns(sr)
                    if cols:  # only snapshot when columns are known
                        snap = SchemaSnapshot(
                            source_signature=sig,
                            source_label=str(label),
                            columns=[Column(name=c["name"], type=c["type"]) for c in cols],
                            run_id=run_id,
                        )
                        record_snapshot(snap_store, drift_findings, snap, workspace_id=workspace_id)
                        # PII rides the same snapshot — flags PII-suggestive
                        # column NAMES (read-only heuristic, never values).
                        record_pii_findings(snap, pii_findings, workspace_id=workspace_id)
            except Exception:  # noqa: BLE001 — per-step best-effort
                continue

        # Row-count integrity (2026-06-18): flag any 1:1 step whose row
        # count changed — silent drop/duplication. Whole-run analysis (needs
        # the input map), so it runs once after the per-step loop. Best-effort.
        try:
            record_row_deltas(
                row_delta_findings,
                workflow=workflow,
                run_result=run_result,
                workspace_id=workspace_id,
            )
        except Exception:  # noqa: BLE001 — observability must never break a run
            pass

        # Node cardinality anomalies (2026-06-18): egregious join explosion /
        # collapse, dedupe over-removal, filter-dropped-all. Run-fed, same
        # whole-run analysis as row-delta. Best-effort.
        # rung 1.5 — pull any per-detector threshold overrides the operator set
        # on the Coverage page so detection at ingest uses *their* numbers.
        card_thresholds: dict[str, dict] = {}
        try:
            from fpulse.steward.settings import SettingsStore
            _scfg = SettingsStore(ws / "settings.json").load()
            for _k, _ov in (getattr(_scfg, "detectors", None) or {}).items():
                _t = getattr(_ov, "thresholds", None)
                if _t:
                    card_thresholds[_k] = dict(_t)
        except Exception:  # noqa: BLE001 — fall back to built-in defaults
            card_thresholds = {}
        try:
            record_node_cardinality(
                node_card_findings,
                workflow=workflow,
                run_result=run_result,
                workspace_id=workspace_id,
                thresholds=card_thresholds,
            )
        except Exception:  # noqa: BLE001 — observability must never break a run
            pass

        return recorded
    except Exception:  # noqa: BLE001 — observability must never break a run
        return 0
