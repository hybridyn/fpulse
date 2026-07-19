"""Run -> Steward ingestion (steward/ingest.py).

Pins that a finished FULL run records the data the previously-DARK detectors
need: per-node CostEvents (volume-anomaly / node-empty-output) and per-source
SchemaSnapshots (schema-drift), best-effort and never raising.

Duck-typed run objects (SimpleNamespace) — the recorder reads via getattr/dict
so we don't couple the test to the full IR models.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fpulse.steward.ingest import record_run


def _result(status="success", row_count=100, columns=None, duration_ms=5):
    return SimpleNamespace(
        status=status, row_count=row_count, duration_ms=duration_ms,
        columns=columns if columns is not None else [],
    )


def _wf(steps):
    return SimpleNamespace(id="wf1", name="My Pipeline", workspace_id="default", steps=steps)


def _cost_events(tmp: Path) -> list[dict]:
    p = tmp / "steward" / "default" / "cost_events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _drift_findings(tmp: Path) -> list[dict]:
    p = tmp / "steward" / "default" / "schema_drift_findings.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_records_cost_event_and_schema_snapshot(tmp_path):
    step = SimpleNamespace(id="src1", type="source", label="Orders",
                           params={"connection_id": "c1", "table": "orders"})
    rr = SimpleNamespace(status="success", run_id="r1", step_results={
        "src1": _result(columns=[{"name": "id", "type": "INTEGER"}, {"name": "amt", "type": "DOUBLE"}]),
    })
    n = record_run({"data_dir": str(tmp_path)}, _wf([step]), rr)
    assert n == 1
    events = _cost_events(tmp_path)
    assert len(events) == 1
    ev = events[0]
    assert ev["node_id"] == "src1" and ev["rows_read"] == 100 and ev["rows_written"] == 100
    assert ev["source_signature"]  # sources carry a stable signature
    # schema snapshot written for the source
    assert (tmp_path / "steward" / "default" / "schemas").exists()


def test_skips_non_success_run(tmp_path):
    step = SimpleNamespace(id="src1", type="source", label="Orders", params={})
    rr = SimpleNamespace(status="error", run_id="r1", step_results={"src1": _result()})
    assert record_run({"data_dir": str(tmp_path)}, _wf([step]), rr) == 0
    assert _cost_events(tmp_path) == []


def test_schema_change_across_runs_emits_drift(tmp_path):
    step = SimpleNamespace(id="src1", type="source", label="Orders",
                           params={"connection_id": "c1", "table": "orders"})
    app = {"data_dir": str(tmp_path)}
    # run 1 — baseline (no drift on first snapshot)
    record_run(app, _wf([step]), SimpleNamespace(status="success", run_id="r1", step_results={
        "src1": _result(columns=[{"name": "id", "type": "INTEGER"}, {"name": "amt", "type": "DOUBLE"}]),
    }))
    assert _drift_findings(tmp_path) == []
    # run 2 — amt type changed → drift finding
    record_run(app, _wf([step]), SimpleNamespace(status="success", run_id="r2", step_results={
        "src1": _result(columns=[{"name": "id", "type": "INTEGER"}, {"name": "amt", "type": "VARCHAR"}]),
    }))
    findings = _drift_findings(tmp_path)
    assert len(findings) == 1
    assert findings[0]["kind"] == "schema_drift"


def test_no_snapshot_when_columns_unknown(tmp_path):
    """An empty-columns source must NOT write a snapshot (would manufacture a
    false 'everything dropped' drift on the next populated run)."""
    step = SimpleNamespace(id="src1", type="source", label="Orders", params={"table": "t"})
    record_run({"data_dir": str(tmp_path)}, _wf([step]),
               SimpleNamespace(status="success", run_id="r1", step_results={"src1": _result(columns=[])}))
    schemas = tmp_path / "steward" / "default" / "schemas"
    assert not schemas.exists() or not any(schemas.iterdir())


def test_never_raises_on_bad_input(tmp_path):
    assert record_run(None, None, None) == 0
    assert record_run({"data_dir": str(tmp_path)}, _wf([]), SimpleNamespace(status="success", run_id="r", step_results={})) == 0


def _pii_findings(tmp: Path) -> list[dict]:
    p = tmp / "steward" / "default" / "pii_findings.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_pii_columns_flagged_from_source_snapshot(tmp_path):
    step = SimpleNamespace(id="src1", type="source", label="Customers",
                           params={"connection_id": "c1", "table": "customers"})
    rr = SimpleNamespace(status="success", run_id="r1", step_results={
        "src1": _result(columns=[
            {"name": "id", "type": "INTEGER"},
            {"name": "email", "type": "VARCHAR"},
            {"name": "ssn", "type": "VARCHAR"},
        ]),
    })
    record_run({"data_dir": str(tmp_path)}, _wf([step]), rr)
    findings = _pii_findings(tmp_path)
    assert len(findings) == 1
    assert findings[0]["kind"] == "pii_leak"


def test_transform_node_records_output_rows(tmp_path):
    """Non-source nodes record rows_written (feeds node-level EMPTY_OUTPUT)."""
    src = SimpleNamespace(id="s", type="source", label="S", params={})
    flt = SimpleNamespace(id="f", type="filter", label="Filter", params={})
    rr = SimpleNamespace(status="success", run_id="r1", step_results={
        "s": _result(row_count=100, columns=[{"name": "a", "type": "INT"}]),
        "f": _result(row_count=0),  # filter dropped everything
    })
    n = record_run({"data_dir": str(tmp_path)}, _wf([src, flt]), rr)
    assert n == 2
    evs = {e["node_id"]: e for e in _cost_events(tmp_path)}
    assert evs["f"]["rows_written"] == 0          # empty-output signal present
    assert evs["f"]["source_signature"] == ""     # non-source → no source sig
