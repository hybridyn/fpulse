"""Pinned tests for the native data-quality check engine (2026-06-07).

P4 of the reviewer audit. Event-driven recorder: external runners
(F-Pulse executor, dbt test, GX, Soda, custom probe) post assertion
results; failed assertions become findings flowing through the same
surface as schema-drift / connector-health / Archeologist.

Contracts pinned here:
  * not_null / unique / duplicate_key / referential_integrity are
    INTEGRITY checks - any failure is P1
  * Non-integrity checks default P2, escalate to P1 when >50% of rows
    failed (structural problem, not a one-off)
  * Each (source, check, column) gets its OWN finding signature so
    dismissing "this dataset always has known nulls in zip_code" only
    silences that one combo, not every null check on that source
  * Re-recording the same assertion produces the same finding id
    (deterministic, no spam)
  * Empty assertions list is fine (assertion runner ran nothing wrong)
"""
from __future__ import annotations

import pytest

from fpulse.steward import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    QualityAssertion,
    QualityCheckReport,
    QualityFindingStore,
    detect_quality_findings,
    record_quality_report,
)


def _report(*assertions: QualityAssertion, source="abc", label="orders") -> QualityCheckReport:
    return QualityCheckReport(
        source_signature=source, source_label=label,
        run_id="exec-test", assertions=list(assertions),
    )


# ── Schema validation ────────────────────────────────────────────────


class TestSchemaValidation:
    def test_rejects_unknown_check(self):
        with pytest.raises(Exception):
            QualityAssertion.model_validate({"check": "vibes_check"})

    def test_all_known_checks_accepted(self):
        for check in [
            "not_null", "unique", "duplicate_key", "referential_integrity",
            "row_count_min", "row_count_max", "freshness", "partition_missing",
            "accepted_values", "range", "regex", "custom",
        ]:
            QualityAssertion.model_validate({"check": check})


# ── Severity rules ──────────────────────────────────────────────────


class TestSeverityRules:
    """Integrity checks → P1 on any failure. Non-integrity → P2 default,
    P1 when failure rate > 50%."""

    def test_not_null_failure_is_p1(self, tmp_path):
        store = QualityFindingStore(tmp_path / "qf.jsonl")
        out = record_quality_report(store, _report(
            QualityAssertion(check="not_null", column="customer_id",
                              failed_count=5, total_rows=10000),
        ))
        assert len(out) == 1
        assert out[0].severity == FindingSeverity.P1
        assert out[0].kind == FindingKind.NULL_SPIKE

    def test_unique_failure_is_p1(self, tmp_path):
        store = QualityFindingStore(tmp_path / "qf.jsonl")
        out = record_quality_report(store, _report(
            QualityAssertion(check="unique", column="order_id",
                              failed_count=1, total_rows=10000),
        ))
        assert out[0].severity == FindingSeverity.P1
        assert out[0].kind == FindingKind.DUPLICATE_KEY_SPIKE

    def test_accepted_values_minor_is_p2(self, tmp_path):
        store = QualityFindingStore(tmp_path / "qf.jsonl")
        out = record_quality_report(store, _report(
            QualityAssertion(check="accepted_values", column="status",
                              failed_count=3, total_rows=10000),
        ))
        assert out[0].severity == FindingSeverity.P2
        assert out[0].kind == FindingKind.QUALITY_CHECK_FAILED

    def test_accepted_values_majority_is_p1(self, tmp_path):
        # >50% failure rate → structural problem, P1.
        store = QualityFindingStore(tmp_path / "qf.jsonl")
        out = record_quality_report(store, _report(
            QualityAssertion(check="accepted_values", column="status",
                              failed_count=6000, total_rows=10000),
        ))
        assert out[0].severity == FindingSeverity.P1


# ── Kind mapping ────────────────────────────────────────────────────


class TestKindMapping:
    def _emit_one(self, tmp_path, check, column="x"):
        store = QualityFindingStore(tmp_path / "qf.jsonl")
        out = record_quality_report(store, _report(
            QualityAssertion(check=check, column=column,
                              failed_count=1, total_rows=100),
        ))
        return out[0].kind

    def test_not_null_maps_to_null_spike(self, tmp_path):
        assert self._emit_one(tmp_path, "not_null") == FindingKind.NULL_SPIKE

    def test_unique_and_duplicate_key_map_to_dup_key_spike(self, tmp_path):
        assert self._emit_one(tmp_path, "unique") == FindingKind.DUPLICATE_KEY_SPIKE
        assert self._emit_one(tmp_path, "duplicate_key", column="y") == FindingKind.DUPLICATE_KEY_SPIKE

    def test_row_count_maps_to_volume_anomaly(self, tmp_path):
        assert self._emit_one(tmp_path, "row_count_min") == FindingKind.VOLUME_ANOMALY
        assert self._emit_one(tmp_path, "row_count_max", column="y") == FindingKind.VOLUME_ANOMALY

    def test_freshness_maps_to_freshness_miss(self, tmp_path):
        assert self._emit_one(tmp_path, "freshness") == FindingKind.FRESHNESS_MISS

    def test_partition_missing_maps(self, tmp_path):
        assert self._emit_one(tmp_path, "partition_missing") == FindingKind.PARTITION_MISSING

    def test_constraint_checks_map_to_quality_check_failed(self, tmp_path):
        for check in ("accepted_values", "range", "regex", "referential_integrity", "custom"):
            assert self._emit_one(tmp_path, check, column=f"col_{check}") == FindingKind.QUALITY_CHECK_FAILED


# ── Recorder behaviour ──────────────────────────────────────────────


class TestRecorder:
    def test_passing_assertions_emit_no_findings(self, tmp_path):
        store = QualityFindingStore(tmp_path / "qf.jsonl")
        out = record_quality_report(store, _report(
            QualityAssertion(check="not_null", column="id",
                              failed_count=0, total_rows=10000),
            QualityAssertion(check="unique", column="id",
                              failed_count=0, total_rows=10000),
        ))
        assert out == []

    def test_only_failing_assertions_emit_findings(self, tmp_path):
        store = QualityFindingStore(tmp_path / "qf.jsonl")
        out = record_quality_report(store, _report(
            QualityAssertion(check="not_null", column="id",
                              failed_count=0, total_rows=10000),       # pass
            QualityAssertion(check="not_null", column="email",
                              failed_count=12, total_rows=10000),      # fail
        ))
        assert len(out) == 1
        assert out[0].evidence["column"] == "email"

    def test_finding_id_is_deterministic(self, tmp_path):
        store = QualityFindingStore(tmp_path / "qf.jsonl")
        a = record_quality_report(store, _report(
            QualityAssertion(check="not_null", column="email",
                              failed_count=5, total_rows=100),
        ))
        b = record_quality_report(store, _report(
            QualityAssertion(check="not_null", column="email",
                              failed_count=99, total_rows=100),  # different count, same id
        ))
        assert a[0].id == b[0].id, "same (source, check, column) must produce same id"

    def test_distinct_columns_get_distinct_signatures(self, tmp_path):
        store = QualityFindingStore(tmp_path / "qf.jsonl")
        out = record_quality_report(store, _report(
            QualityAssertion(check="not_null", column="email",
                              failed_count=1, total_rows=100),
            QualityAssertion(check="not_null", column="phone",
                              failed_count=1, total_rows=100),
        ))
        sigs = {f.evidence["source_signature"] for f in out}
        assert len(sigs) == 2, "different columns must get different signatures"

    def test_finding_carries_evidence_back(self, tmp_path):
        store = QualityFindingStore(tmp_path / "qf.jsonl")
        out = record_quality_report(store, _report(
            QualityAssertion(check="unique", column="order_id",
                              failed_count=3, total_rows=500,
                              message="Found 3 duplicates in last sync"),
        ))
        ev = out[0].evidence
        assert ev["check"] == "unique"
        assert ev["column"] == "order_id"
        assert ev["failed_count"] == 3
        assert ev["total_rows"] == 500
        assert "Found 3 duplicates" in ev["message"]


# ── Detector / suppression ──────────────────────────────────────────


class TestDetector:
    def test_empty_journal_returns_empty(self, tmp_path):
        store = QualityFindingStore(tmp_path / "qf.jsonl")
        assert detect_quality_findings(store) == []

    def test_journal_findings_surface(self, tmp_path):
        store = QualityFindingStore(tmp_path / "qf.jsonl")
        record_quality_report(store, _report(
            QualityAssertion(check="not_null", column="x",
                              failed_count=5, total_rows=100),
        ))
        assert len(detect_quality_findings(store)) == 1

    def test_suppression_silences_finding(self, tmp_path):
        store = QualityFindingStore(tmp_path / "qf.jsonl")
        emitted = record_quality_report(store, _report(
            QualityAssertion(check="not_null", column="x",
                              failed_count=5, total_rows=100),
        ))
        sig = emitted[0].evidence["source_signature"]
        assert detect_quality_findings(store, suppressed_signatures={sig}) == []


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

    def test_post_with_failures_returns_finding_ids(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        r = client.post("/api/steward/quality-check", json={
            "source_signature": "abc",
            "source_label": "orders",
            "run_id": "exec-1",
            "assertions": [
                {"check": "not_null", "column": "id",
                  "failed_count": 0, "total_rows": 1000},   # passes
                {"check": "not_null", "column": "email",
                  "failed_count": 12, "total_rows": 1000},  # fails
                {"check": "unique", "column": "id",
                  "failed_count": 2, "total_rows": 1000},   # fails
            ],
        })
        assert r.status_code == 200
        body = r.json()
        assert body["recorded"] is True
        assert body["assertions_total"] == 3
        assert body["findings_emitted"] == 2
        assert len(body["finding_ids"]) == 2

    def test_passing_post_emits_no_findings(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        r = client.post("/api/steward/quality-check", json={
            "source_signature": "abc",
            "assertions": [
                {"check": "not_null", "column": "id",
                  "failed_count": 0, "total_rows": 1000},
            ],
        })
        assert r.json()["findings_emitted"] == 0

    def test_invalid_check_rejected(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        r = client.post("/api/steward/quality-check", json={
            "source_signature": "abc",
            "assertions": [{"check": "vibes", "failed_count": 1}],
        })
        assert r.status_code == 400

    def test_findings_surface_via_findings_endpoint(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        client.post("/api/steward/quality-check", json={
            "source_signature": "abc", "source_label": "orders",
            "assertions": [{"check": "not_null", "column": "id",
                             "failed_count": 5, "total_rows": 100}],
        })
        body = client.get("/api/steward/findings").json()
        kinds = [f["kind"] for f in body["findings"]]
        assert "null_spike" in kinds

    def test_dismiss_silences_quality_finding(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        r = client.post("/api/steward/quality-check", json={
            "source_signature": "abc",
            "assertions": [{"check": "not_null", "column": "id",
                             "failed_count": 5, "total_rows": 100}],
        })
        finding_id = r.json()["finding_ids"][0]
        client.post(f"/api/steward/findings/{finding_id}/dismiss",
                     json={"reason": "Known issue with legacy id column"})
        body = client.get("/api/steward/findings").json()
        kinds = [f["kind"] for f in body["findings"]]
        assert "null_spike" not in kinds
