"""Pinned tests for the OpenLineage formatter + JSONL exporter
(L2, 2026-06-08).

Conformance to the OpenLineage 1.0-5 RunEvent shape: every required
field present, eventType / eventTime / producer / schemaURL / run /
job all populated, inputs+outputs structured correctly.

These tests do NOT exercise an HTTP POST (deferred to L2.1). They
pin the formatter + the JSONL file exporter so the format is fixed
before any network code is added.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from fpulse.lineage import LineageStore
from fpulse.lineage.openlineage import (
    OPENLINEAGE_PRODUCER_DEFAULT,
    OPENLINEAGE_SCHEMA_URL,
    OpenLineageJSONLExporter,
    to_openlineage_run_event,
)


# Re-use the SQLite fake from earlier lineage tests
class _FakeDB:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def fetchone(self, sql, params=()):
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql, params=()):
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]


def _step_run(**overrides):
    base = {
        "workflow_id": "wf-1", "run_id": "run-A", "step_id": "s1",
        "step_label": "Load orders", "step_type": "csv_source",
        "columns_in": [], "columns_out": ["id", "amount"],
        "rows_in": 0, "rows_out": 10000,
        "started_at": 1717000000.0, "completed_at": 1717000002.5,
        "error": "",
    }
    base.update(overrides)
    return base


# ── Conformance: required RunEvent fields ───────────────────────────


class TestConformance:
    """The 1.0-5 spec requires: eventType, eventTime, producer,
    schemaURL, run (with runId), job (with namespace + name). Pin
    each one."""

    def test_event_has_all_required_top_level_fields(self):
        ev = to_openlineage_run_event(_step_run())
        for field in ("eventType", "eventTime", "producer", "schemaURL", "run", "job"):
            assert field in ev, f"missing required field {field}"

    def test_event_type_complete_by_default(self):
        ev = to_openlineage_run_event(_step_run())
        assert ev["eventType"] == "COMPLETE"

    def test_event_type_can_be_fail(self):
        ev = to_openlineage_run_event(_step_run(error="boom"), event_type="FAIL")
        assert ev["eventType"] == "FAIL"

    def test_event_time_is_iso_rfc3339(self):
        ev = to_openlineage_run_event(_step_run())
        # Sanity: parseable as ISO + has timezone offset
        from datetime import datetime
        parsed = datetime.fromisoformat(ev["eventTime"])
        assert parsed.tzinfo is not None, "eventTime must include timezone"

    def test_producer_uri_present(self):
        ev = to_openlineage_run_event(_step_run())
        assert ev["producer"] == OPENLINEAGE_PRODUCER_DEFAULT

    def test_schema_url_pinned_to_known_spec(self):
        ev = to_openlineage_run_event(_step_run())
        assert ev["schemaURL"] == OPENLINEAGE_SCHEMA_URL

    def test_run_has_runid(self):
        ev = to_openlineage_run_event(_step_run(run_id="my-run-id"))
        assert ev["run"]["runId"] == "my-run-id"

    def test_job_has_namespace_and_name(self):
        ev = to_openlineage_run_event(_step_run(workflow_id="wf-X", step_label="Load"))
        assert ev["job"]["namespace"] == "f-pulse"
        assert ev["job"]["name"] == "wf-X.Load"


# ── Inputs / outputs ────────────────────────────────────────────────


class TestInputsOutputs:
    def test_empty_columns_produce_empty_lists(self):
        ev = to_openlineage_run_event(_step_run(columns_in=[], columns_out=[]))
        assert ev["inputs"] == []
        assert ev["outputs"] == []

    def test_output_columns_produce_dataset_with_schema_facet(self):
        ev = to_openlineage_run_event(_step_run(
            columns_out=["id", "amount", "name"],
        ))
        assert len(ev["outputs"]) == 1
        out = ev["outputs"][0]
        assert out["namespace"] == "f-pulse"
        assert "name" in out
        # Schema facet present + lists each column
        schema = out["facets"]["schema"]
        names = [f["name"] for f in schema["fields"]]
        assert names == ["id", "amount", "name"]

    def test_input_columns_produce_input_dataset(self):
        ev = to_openlineage_run_event(_step_run(
            columns_in=["raw_id"], columns_out=["clean_id"],
        ))
        assert len(ev["inputs"]) == 1
        assert ev["inputs"][0]["facets"]["schema"]["fields"][0]["name"] == "raw_id"

    def test_redact_columns_strips_schema_facet(self, monkeypatch):
        monkeypatch.setenv("FPULSE_LINEAGE_REDACT_COLUMNS", "1")
        ev = to_openlineage_run_event(_step_run(columns_out=["ssn", "email"]))
        # Output dataset still emitted but column names redacted
        assert ev["outputs"][0].get("facets", {}) == {}


# ── Facets ──────────────────────────────────────────────────────────


class TestFacets:
    def test_source_code_facet_carries_step_type(self):
        ev = to_openlineage_run_event(_step_run(step_type="db_source"))
        src = ev["job"]["facets"]["sourceCode"]
        assert src["language"] == "fpulse-node"
        assert src["sourceCode"] == "db_source"

    def test_run_facet_carries_row_counts(self):
        ev = to_openlineage_run_event(_step_run(rows_in=5, rows_out=10000))
        stats = ev["run"]["facets"]["fpulse_runtime_stats"]
        assert stats["rows_in"] == 5
        assert stats["rows_out"] == 10000

    def test_error_message_facet_on_fail_event(self):
        ev = to_openlineage_run_event(
            _step_run(error="ConnectionRefusedError: db.example.com"),
            event_type="FAIL",
        )
        err = ev["run"]["facets"]["errorMessage"]
        assert "ConnectionRefusedError" in err["message"]


# ── Namespace / producer overrides ──────────────────────────────────


class TestEnvOverrides:
    def test_namespace_override(self, monkeypatch):
        monkeypatch.setenv("FPULSE_LINEAGE_NAMESPACE", "my-org")
        ev = to_openlineage_run_event(_step_run())
        assert ev["job"]["namespace"] == "my-org"
        assert ev["outputs"] == [] or ev["outputs"][0]["namespace"] == "my-org"

    def test_producer_override(self, monkeypatch):
        monkeypatch.setenv("FPULSE_LINEAGE_PRODUCER", "https://my-fpulse.example/v1")
        ev = to_openlineage_run_event(_step_run())
        assert ev["producer"] == "https://my-fpulse.example/v1"


# ── JSONL exporter ──────────────────────────────────────────────────


class TestJSONLExporter:
    def test_writes_one_line_per_event(self, tmp_path):
        path = tmp_path / "lineage.jsonl"
        exporter = OpenLineageJSONLExporter(path)
        exporter.write_event(to_openlineage_run_event(_step_run(step_id="a")))
        exporter.write_event(to_openlineage_run_event(_step_run(step_id="b")))
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 2

    def test_each_line_is_valid_json(self, tmp_path):
        path = tmp_path / "lineage.jsonl"
        exporter = OpenLineageJSONLExporter(path)
        exporter.write_event(to_openlineage_run_event(_step_run()))
        for ln in path.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                parsed = json.loads(ln)
                assert "eventType" in parsed

    def test_export_run_pulls_all_step_runs_from_store(self, tmp_path):
        store = LineageStore(_FakeDB())
        # Plant 3 step-runs for one run + 1 for another (must be excluded)
        for i, sid in enumerate(["s1", "s2", "s3"]):
            store.record_step_run(
                workflow_id="wf-1", run_id="run-A", step_id=sid,
                step_label=f"Step {sid}", step_type="db_source",
                columns_out=["id"], rows_out=100,
                started_at=float(i),
            )
        store.record_step_run(
            workflow_id="wf-1", run_id="run-OTHER", step_id="s_other",
            started_at=0.0,
        )

        path = tmp_path / "out.jsonl"
        exporter = OpenLineageJSONLExporter(path)
        n = exporter.export_run("run-A", store)
        assert n == 3
        lines = path.read_text(encoding="utf-8").splitlines()
        # All 3 events should carry runId="run-A"
        for ln in lines:
            ev = json.loads(ln)
            assert ev["run"]["runId"] == "run-A"

    def test_export_uses_fail_for_errored_steps(self, tmp_path):
        store = LineageStore(_FakeDB())
        store.record_step_run(
            workflow_id="wf-1", run_id="run-A", step_id="s_ok",
            started_at=1.0, completed_at=2.0,
        )
        store.record_step_run(
            workflow_id="wf-1", run_id="run-A", step_id="s_bad",
            started_at=3.0, completed_at=4.0,
            error="ConnectionError: x",
        )
        path = tmp_path / "out.jsonl"
        OpenLineageJSONLExporter(path).export_run("run-A", store)
        events = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        types = {ev["job"]["name"].rsplit(".", 1)[1]: ev["eventType"] for ev in events}
        assert types["s_ok"] == "COMPLETE"
        assert types["s_bad"] == "FAIL"
