"""Pinned tests for the cost / movement tracking surface (2026-06-07, P5).

Activates WAREHOUSE_WASTE detection. Records per-run cost events;
emits a finding when N consecutive events for the same source had
rows_read > 0 AND rows_written = 0 (we paid the read cost, nothing
flowed downstream).

Contracts pinned here:
  * Single event NEVER fires WAREHOUSE_WASTE (N-streak threshold)
  * A run with rows_written > 0 RESETS the streak
  * Re-recording the same (source, streak position) is idempotent
    (deterministic finding id)
  * summarise_by_source rolls events into per-source totals
  * Empty source_signature event is dropped (no anchor)
"""
from __future__ import annotations

import pytest

from fpulse.steward import (
    CostEvent,
    CostEventStore,
    CostFindingStore,
    FindingKind,
    FindingLevel,
    FindingSeverity,
    detect_cost_findings,
    record_cost_event,
    summarise_by_source,
)


def _event(*, source="src-1", rows_read=1000, rows_written=0,
            run_id="run-1", completed_at="2026-06-08T10:00:00+00:00",
            bytes_read=0, bytes_written=0,
            workflow_id="", workflow_name="", node_id="", node_label=""):
    return CostEvent(
        run_id=run_id, source_signature=source,
        workflow_id=workflow_id, workflow_name=workflow_name,
        node_id=node_id, node_label=node_label,
        rows_read=rows_read, rows_written=rows_written,
        bytes_read=bytes_read, bytes_written=bytes_written,
        completed_at=completed_at,
    )


# ── Event store ──────────────────────────────────────────────────────


class TestEventStore:
    def test_missing_returns_empty(self, tmp_path):
        store = CostEventStore(tmp_path / "ce.jsonl")
        assert store.all() == []

    def test_append_then_all(self, tmp_path):
        store = CostEventStore(tmp_path / "ce.jsonl")
        store.append(_event(run_id="r1"))
        store.append(_event(run_id="r2"))
        out = store.all()
        assert len(out) == 2
        assert out[0].run_id == "r1"

    def test_corrupt_lines_skipped(self, tmp_path):
        path = tmp_path / "ce.jsonl"
        path.write_text(
            '{"run_id": "ok", "source_signature": "s", "rows_read": 1, "rows_written": 0}\n'
            'this line is not JSON\n'
            '{"run_id": "ok2", "source_signature": "s", "rows_read": 1, "rows_written": 0}\n',
            encoding="utf-8",
        )
        out = CostEventStore(path).all()
        assert len(out) == 2  # bad line skipped, two good lines loaded


# ── WAREHOUSE_WASTE detector ────────────────────────────────────────


class TestWarehouseWasteDetector:
    def _stores(self, tmp_path):
        return (
            CostEventStore(tmp_path / "ce.jsonl"),
            CostFindingStore(tmp_path / "cf.jsonl"),
        )

    def test_single_zero_event_does_not_fire(self, tmp_path):
        es, fs = self._stores(tmp_path)
        out = record_cost_event(es, fs, _event())
        assert out == []

    def test_two_zero_events_do_not_fire(self, tmp_path):
        es, fs = self._stores(tmp_path)
        record_cost_event(es, fs, _event(run_id="r1"))
        out = record_cost_event(es, fs, _event(run_id="r2"))
        assert out == []

    def test_three_zero_events_fire(self, tmp_path):
        es, fs = self._stores(tmp_path)
        record_cost_event(es, fs, _event(run_id="r1"))
        record_cost_event(es, fs, _event(run_id="r2"))
        out = record_cost_event(es, fs, _event(run_id="r3"))
        assert len(out) == 1
        finding = out[0]
        assert finding.kind == FindingKind.WAREHOUSE_WASTE
        assert finding.level == FindingLevel.COST
        assert finding.severity == FindingSeverity.P2

    def test_non_zero_event_resets_streak(self, tmp_path):
        # Two zeros, then a productive run, then two more zeros — should NOT
        # fire because the streak got broken.
        es, fs = self._stores(tmp_path)
        record_cost_event(es, fs, _event(run_id="r1", rows_written=0))
        record_cost_event(es, fs, _event(run_id="r2", rows_written=0))
        record_cost_event(es, fs, _event(run_id="r3", rows_written=500))
        record_cost_event(es, fs, _event(run_id="r4", rows_written=0))
        out = record_cost_event(es, fs, _event(run_id="r5", rows_written=0))
        assert out == [], "productive run breaks the streak"

    def test_rows_read_zero_does_not_count_as_waste(self, tmp_path):
        # rows_read=0 AND rows_written=0 means "didn't run / no work". Only
        # counts as waste when we DID read but produced nothing.
        es, fs = self._stores(tmp_path)
        for run in ("r1", "r2", "r3"):
            record_cost_event(es, fs, _event(run_id=run, rows_read=0, rows_written=0))
        # Now record a real read with no write:
        out = record_cost_event(es, fs, _event(run_id="r4", rows_read=100, rows_written=0))
        assert out == []  # streak of 3 not yet met for read>0 events

    def test_different_sources_have_independent_streaks(self, tmp_path):
        es, fs = self._stores(tmp_path)
        # Three zero-output events on src-A → fires.
        for run in ("a1", "a2"):
            record_cost_event(es, fs, _event(source="src-A", run_id=run))
        a_out = record_cost_event(es, fs, _event(source="src-A", run_id="a3"))
        assert len(a_out) == 1 and "src-A" in a_out[0].body
        # Single event on src-B → no finding (different source).
        b_out = record_cost_event(es, fs, _event(source="src-B", run_id="b1"))
        assert b_out == []

    def test_event_without_any_anchor_is_dropped(self, tmp_path):
        es, fs = self._stores(tmp_path)
        # No source AND no sink AND no node_id → not recorded at all.
        out = record_cost_event(es, fs, CostEvent(
            run_id="r1", rows_read=10, rows_written=0,
        ))
        assert out == []
        assert es.all() == []


# ── EMPTY_OUTPUT (node-level) detector ──────────────────────────────


class TestEmptyOutputDetector:
    """First NODE-level Steward signal. Same streak pattern as
    warehouse_waste but anchored on (workflow_id, node_id) so a
    specific filter/join/transform that keeps producing nothing
    gets its own finding distinct from the source-level one."""

    def _stores(self, tmp_path):
        return (
            CostEventStore(tmp_path / "ce.jsonl"),
            CostFindingStore(tmp_path / "cf.jsonl"),
        )

    def _node_event(self, **kw):
        defaults = dict(
            source="src-x", run_id="r",
            rows_read=100, rows_written=0,
            workflow_id="wf-1", workflow_name="Test pipeline",
            node_id="n-filter", node_label="Filter active records",
        )
        defaults.update(kw)
        return _event(**defaults)

    def test_three_zero_node_runs_emit_empty_output(self, tmp_path):
        es, fs = self._stores(tmp_path)
        record_cost_event(es, fs, self._node_event(run_id="r1"))
        record_cost_event(es, fs, self._node_event(run_id="r2"))
        out = record_cost_event(es, fs, self._node_event(run_id="r3"))
        # Both warehouse_waste AND empty_output fire since both anchors
        # have the same streak shape.
        kinds = {f.kind for f in out}
        assert FindingKind.EMPTY_OUTPUT in kinds
        empty_out = next(f for f in out if f.kind == FindingKind.EMPTY_OUTPUT)
        assert empty_out.level == FindingLevel.NODE

    def test_node_event_without_source_still_fires_empty_output(self, tmp_path):
        # A node-only event (no source signature) still detects empty
        # output on the node anchor.
        es, fs = self._stores(tmp_path)
        for run in ("r1", "r2"):
            record_cost_event(es, fs, self._node_event(run_id=run, source=""))
        out = record_cost_event(es, fs, self._node_event(run_id="r3", source=""))
        kinds = {f.kind for f in out}
        assert FindingKind.EMPTY_OUTPUT in kinds
        assert FindingKind.WAREHOUSE_WASTE not in kinds, \
            "no source signature ⇒ warehouse_waste should not fire"

    def test_different_nodes_have_independent_streaks(self, tmp_path):
        es, fs = self._stores(tmp_path)
        for run in ("a1", "a2"):
            record_cost_event(es, fs, self._node_event(run_id=run, node_id="n-A"))
        out_a = record_cost_event(es, fs, self._node_event(run_id="a3", node_id="n-A"))
        assert any(f.kind == FindingKind.EMPTY_OUTPUT for f in out_a)
        out_b = record_cost_event(es, fs, self._node_event(run_id="b1", node_id="n-B"))
        assert not any(f.kind == FindingKind.EMPTY_OUTPUT for f in out_b)

    def test_productive_node_run_resets_streak(self, tmp_path):
        es, fs = self._stores(tmp_path)
        record_cost_event(es, fs, self._node_event(run_id="r1"))
        record_cost_event(es, fs, self._node_event(run_id="r2"))
        record_cost_event(es, fs, self._node_event(run_id="r3", rows_written=10))
        record_cost_event(es, fs, self._node_event(run_id="r4"))
        out = record_cost_event(es, fs, self._node_event(run_id="r5"))
        assert not any(f.kind == FindingKind.EMPTY_OUTPUT for f in out)


# ── Rollup ──────────────────────────────────────────────────────────


class TestSummariseBySource:
    def test_aggregates_rows_across_runs(self):
        events = [
            _event(source="src-A", run_id="r1", rows_read=100, rows_written=90),
            _event(source="src-A", run_id="r2", rows_read=200, rows_written=180),
            _event(source="src-B", run_id="r3", rows_read=50, rows_written=50),
        ]
        out = summarise_by_source(events)
        assert out["src-A"]["rows_read"] == 300
        assert out["src-A"]["rows_written"] == 270
        assert out["src-A"]["run_count"] == 2
        assert out["src-B"]["rows_read"] == 50
        assert out["src-B"]["run_count"] == 1

    def test_tracks_first_and_last_seen(self):
        events = [
            _event(source="src-X", run_id="r1",
                    completed_at="2026-06-07T10:00:00+00:00"),
            _event(source="src-X", run_id="r2",
                    completed_at="2026-06-07T11:00:00+00:00"),
            _event(source="src-X", run_id="r3",
                    completed_at="2026-06-07T09:00:00+00:00"),
        ]
        out = summarise_by_source(events)
        assert out["src-X"]["first_seen"] == "2026-06-07T09:00:00+00:00"
        assert out["src-X"]["last_seen"]  == "2026-06-07T11:00:00+00:00"


# ── Scan-side detector ──────────────────────────────────────────────


class TestScanSideDetect:
    def test_empty_journal_returns_empty(self, tmp_path):
        fs = CostFindingStore(tmp_path / "cf.jsonl")
        assert detect_cost_findings(fs) == []

    def test_suppression_silences_finding(self, tmp_path):
        es = CostEventStore(tmp_path / "ce.jsonl")
        fs = CostFindingStore(tmp_path / "cf.jsonl")
        for run in ("r1", "r2", "r3"):
            out = record_cost_event(es, fs, _event(run_id=run))
        assert len(out) >= 1
        sig = out[0].evidence["source_signature"]
        # After suppressing this one signature, the warehouse-waste finding
        # should be gone (other findings could still surface but we asserted
        # against the source-level one).
        remaining = detect_cost_findings(fs, suppressed_signatures={sig})
        assert all(f.evidence.get("source_signature") != sig for f in remaining)


# ── API integration ────────────────────────────────────────────────


class TestAPIIntegration:
    def _make_client(self, tmp_path, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import fpulse.api.steward as steward_mod
        import fpulse.main as main_mod
        monkeypatch.setattr(main_mod, "app_state",
                            {"data_dir": str(tmp_path)}, raising=False)
        monkeypatch.setattr(steward_mod, "_workflows_for_scan", lambda ws: [])
        app = FastAPI()
        from fpulse.auth.deps import require_auth
        app.dependency_overrides[require_auth] = lambda: None
        app.include_router(steward_mod.router)
        return TestClient(app)

    def test_post_event_records(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        r = client.post("/api/steward/cost-event", json={
            "source_signature": "src-test", "run_id": "r1",
            "rows_read": 100, "rows_written": 90,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["recorded"] is True
        assert body["finding_emitted"] is False

    def test_three_zero_runs_emit_finding_via_api(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        for run in ("r1", "r2"):
            client.post("/api/steward/cost-event", json={
                "source_signature": "src-test", "run_id": run,
                "rows_read": 100, "rows_written": 0,
            })
        r = client.post("/api/steward/cost-event", json={
            "source_signature": "src-test", "run_id": "r3",
            "rows_read": 100, "rows_written": 0,
        })
        body = r.json()
        assert body["finding_emitted"] is True
        assert body["findings_emitted"] >= 1
        assert body["finding_ids"]
        assert body["finding_id"]

    def test_empty_output_fires_via_api_with_node_id(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        for run in ("r1", "r2"):
            client.post("/api/steward/cost-event", json={
                "workflow_id": "wf-1", "workflow_name": "Test pipe",
                "node_id": "n-filter", "node_label": "Filter",
                "run_id": run, "rows_read": 100, "rows_written": 0,
            })
        r = client.post("/api/steward/cost-event", json={
            "workflow_id": "wf-1", "workflow_name": "Test pipe",
            "node_id": "n-filter", "node_label": "Filter",
            "run_id": "r3", "rows_read": 100, "rows_written": 0,
        })
        body = r.json()
        assert body["findings_emitted"] >= 1
        # Verify the EMPTY_OUTPUT one is among them via /findings.
        listing = client.get("/api/steward/findings").json()
        kinds = [f["kind"] for f in listing["findings"]]
        assert "empty_output" in kinds

    def test_summary_endpoint_returns_rollup(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        client.post("/api/steward/cost-event", json={
            "source_signature": "src-A", "rows_read": 100, "rows_written": 90,
        })
        client.post("/api/steward/cost-event", json={
            "source_signature": "src-A", "rows_read": 200, "rows_written": 180,
        })
        body = client.get("/api/steward/cost-summary").json()
        assert body["source_count"] == 1
        assert body["by_source"]["src-A"]["run_count"] == 2
        assert body["by_source"]["src-A"]["rows_read"] == 300

    def test_event_without_signature_rejected(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        r = client.post("/api/steward/cost-event", json={
            "rows_read": 100, "rows_written": 0,
        })
        assert r.status_code == 400

    def test_finding_surfaces_in_findings_endpoint(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        for run in ("r1", "r2", "r3"):
            client.post("/api/steward/cost-event", json={
                "source_signature": "src-test", "run_id": run,
                "rows_read": 100, "rows_written": 0,
            })
        body = client.get("/api/steward/findings").json()
        kinds = [f["kind"] for f in body["findings"]]
        assert "warehouse_waste" in kinds
