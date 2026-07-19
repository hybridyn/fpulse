"""Pinned tests for the schema-drift detector (2026-06-07).

First data-level Steward detector. Event-driven: detection happens at
POST /schema-snapshot time (not at scan time), but findings persist in
the journal and re-surface on every scan until dismissed.

These contracts MUST hold:
  * First snapshot of a source NEVER emits drift (it's the baseline)
  * Identical re-record NEVER emits drift (idempotent)
  * Add-only change emits ONE finding at P3
  * Drop OR type-change anywhere in the diff escalates the WHOLE
    finding to P1 (worst-case wins)
  * Dismissal silences the signature; the next drift on the SAME
    source produces a new finding (different captured_at → different id)
  * Findings persist across scans via the JSONL journal
"""
from __future__ import annotations

from pathlib import Path

import pytest

from fpulse.steward import (
    Column,
    FindingKind,
    FindingLevel,
    FindingSeverity,
    SchemaChange,
    SchemaDriftFindingStore,
    SchemaSnapshot,
    SchemaSnapshotStore,
    detect_schema_drift,
    diff_schemas,
    record_snapshot,
)


def _cols(*spec: tuple[str, str]) -> list[Column]:
    return [Column(name=n, type=t) for n, t in spec]


# ── Diff function ────────────────────────────────────────────────────


class TestDiffSchemas:
    def test_no_change_returns_empty(self):
        a = _cols(("id", "int"), ("name", "text"))
        b = _cols(("id", "int"), ("name", "text"))
        assert diff_schemas(a, b) == []

    def test_added_column(self):
        a = _cols(("id", "int"))
        b = _cols(("id", "int"), ("email", "text"))
        changes = diff_schemas(a, b)
        assert len(changes) == 1
        assert changes[0].kind == "added"
        assert changes[0].column_name == "email"
        assert changes[0].new_type == "text"

    def test_dropped_column(self):
        a = _cols(("id", "int"), ("legacy", "text"))
        b = _cols(("id", "int"))
        changes = diff_schemas(a, b)
        assert len(changes) == 1
        assert changes[0].kind == "dropped"
        assert changes[0].column_name == "legacy"

    def test_type_changed(self):
        a = _cols(("amount", "int"))
        b = _cols(("amount", "decimal"))
        changes = diff_schemas(a, b)
        assert len(changes) == 1
        assert changes[0].kind == "type_changed"
        assert changes[0].old_type == "int"
        assert changes[0].new_type == "decimal"

    def test_case_only_type_change_is_not_drift(self):
        # 'TEXT' and 'text' across drivers - same logical type, just
        # casing. Don't generate noise for this.
        a = _cols(("name", "text"))
        b = _cols(("name", "TEXT"))
        assert diff_schemas(a, b) == []

    def test_mixed_diff_returns_all_changes(self):
        a = _cols(("id", "int"), ("legacy", "text"), ("amount", "int"))
        b = _cols(("id", "int"), ("amount", "decimal"), ("email", "text"))
        changes = diff_schemas(a, b)
        kinds = {(c.kind, c.column_name) for c in changes}
        assert kinds == {
            ("added", "email"),
            ("dropped", "legacy"),
            ("type_changed", "amount"),
        }

    def test_diff_is_order_insensitive(self):
        a = _cols(("a", "int"), ("b", "int"))
        b = _cols(("b", "int"), ("a", "int"))
        assert diff_schemas(a, b) == []


# ── Snapshot store ───────────────────────────────────────────────────


class TestSnapshotStore:
    def test_get_missing_returns_none(self, tmp_path):
        store = SchemaSnapshotStore(tmp_path)
        assert store.get("nope") is None

    def test_upsert_then_get_roundtrip(self, tmp_path):
        store = SchemaSnapshotStore(tmp_path)
        snap = SchemaSnapshot(
            source_signature="abc",
            source_label="orders.csv",
            columns=_cols(("id", "int")),
        )
        store.upsert(snap)
        back = store.get("abc")
        assert back is not None
        assert back.source_signature == "abc"
        assert back.columns[0].name == "id"

    def test_upsert_replaces_previous(self, tmp_path):
        store = SchemaSnapshotStore(tmp_path)
        store.upsert(SchemaSnapshot(source_signature="abc", columns=_cols(("a", "int"))))
        store.upsert(SchemaSnapshot(source_signature="abc", columns=_cols(("b", "text"))))
        back = store.get("abc")
        assert back.columns[0].name == "b"

    def test_all_returns_every_signature(self, tmp_path):
        store = SchemaSnapshotStore(tmp_path)
        store.upsert(SchemaSnapshot(source_signature="a", columns=[]))
        store.upsert(SchemaSnapshot(source_signature="b", columns=[]))
        assert {s.source_signature for s in store.all()} == {"a", "b"}

    def test_corrupt_file_does_not_break_listing(self, tmp_path):
        # Hand-write a bad file alongside good ones - listing should
        # skip the bad one not crash.
        store = SchemaSnapshotStore(tmp_path)
        store.upsert(SchemaSnapshot(source_signature="ok", columns=[]))
        (tmp_path / "bad.json").write_text("{not valid json", encoding="utf-8")
        assert len(store.all()) == 1


# ── Record + emit ────────────────────────────────────────────────────


class TestRecordSnapshot:
    """The single entry point that does diff + persist + emit."""

    def _stores(self, tmp_path):
        return (
            SchemaSnapshotStore(tmp_path / "schemas"),
            SchemaDriftFindingStore(tmp_path / "drift.jsonl"),
        )

    def test_first_snapshot_emits_no_finding(self, tmp_path):
        ss, fs = self._stores(tmp_path)
        _saved, changes, finding = record_snapshot(
            ss, fs,
            SchemaSnapshot(source_signature="abc",
                            source_label="orders",
                            columns=_cols(("id", "int"))),
        )
        assert changes == []
        assert finding is None

    def test_idempotent_resnapshot_emits_no_finding(self, tmp_path):
        ss, fs = self._stores(tmp_path)
        snap = SchemaSnapshot(source_signature="abc",
                               columns=_cols(("id", "int")))
        record_snapshot(ss, fs, snap)
        _saved, changes, finding = record_snapshot(ss, fs, snap)
        assert changes == []
        assert finding is None

    def test_add_only_change_emits_p3(self, tmp_path):
        ss, fs = self._stores(tmp_path)
        record_snapshot(ss, fs, SchemaSnapshot(
            source_signature="abc", columns=_cols(("id", "int")),
        ))
        _saved, changes, finding = record_snapshot(ss, fs, SchemaSnapshot(
            source_signature="abc", columns=_cols(("id", "int"), ("email", "text")),
        ))
        assert finding is not None
        assert finding.severity == FindingSeverity.P3
        assert finding.kind == FindingKind.SCHEMA_DRIFT
        assert finding.level == FindingLevel.DATA
        assert "email" in finding.evidence["added"]

    def test_drop_emits_p1(self, tmp_path):
        ss, fs = self._stores(tmp_path)
        record_snapshot(ss, fs, SchemaSnapshot(
            source_signature="abc", columns=_cols(("id", "int"), ("legacy", "text")),
        ))
        _saved, changes, finding = record_snapshot(ss, fs, SchemaSnapshot(
            source_signature="abc", columns=_cols(("id", "int")),
        ))
        assert finding.severity == FindingSeverity.P1
        assert "legacy" in finding.evidence["dropped"]

    def test_type_change_emits_p1(self, tmp_path):
        ss, fs = self._stores(tmp_path)
        record_snapshot(ss, fs, SchemaSnapshot(
            source_signature="abc", columns=_cols(("amount", "int")),
        ))
        _saved, changes, finding = record_snapshot(ss, fs, SchemaSnapshot(
            source_signature="abc", columns=_cols(("amount", "decimal")),
        ))
        assert finding.severity == FindingSeverity.P1
        assert "amount" in finding.evidence["type_changed"]

    def test_worst_case_wins_when_mixed(self, tmp_path):
        # ADD (P3) bundled with TYPE_CHANGE (P1) must escalate to P1.
        # We don't want operators thinking "oh, it's just additions"
        # while a type change quietly breaks downstream casts.
        ss, fs = self._stores(tmp_path)
        record_snapshot(ss, fs, SchemaSnapshot(
            source_signature="abc", columns=_cols(("id", "int"), ("amount", "int")),
        ))
        _saved, changes, finding = record_snapshot(ss, fs, SchemaSnapshot(
            source_signature="abc",
            columns=_cols(("id", "int"), ("amount", "decimal"), ("email", "text")),
        ))
        assert finding.severity == FindingSeverity.P1
        assert "email" in finding.evidence["added"]
        assert "amount" in finding.evidence["type_changed"]

    def test_finding_persists_in_journal(self, tmp_path):
        ss, fs = self._stores(tmp_path)
        record_snapshot(ss, fs, SchemaSnapshot(
            source_signature="abc", columns=_cols(("id", "int")),
        ))
        record_snapshot(ss, fs, SchemaSnapshot(
            source_signature="abc", columns=_cols(("id", "int"), ("x", "text")),
        ))
        all_findings = fs.all()
        assert len(all_findings) == 1
        assert all_findings[0].kind == FindingKind.SCHEMA_DRIFT


# ── Detector (read-side) ─────────────────────────────────────────────


class TestDetectSchemaDrift:
    def test_no_journal_returns_empty(self, tmp_path):
        fs = SchemaDriftFindingStore(tmp_path / "drift.jsonl")
        assert detect_schema_drift(fs) == []

    def test_returns_open_findings_from_journal(self, tmp_path):
        ss = SchemaSnapshotStore(tmp_path / "schemas")
        fs = SchemaDriftFindingStore(tmp_path / "drift.jsonl")
        record_snapshot(ss, fs, SchemaSnapshot(source_signature="abc",
                                                 columns=_cols(("id", "int"))))
        record_snapshot(ss, fs, SchemaSnapshot(source_signature="abc",
                                                 columns=_cols(("id", "int"), ("x", "text"))))
        found = detect_schema_drift(fs)
        assert len(found) == 1
        assert found[0].kind == FindingKind.SCHEMA_DRIFT

    def test_suppression_silences_finding(self, tmp_path):
        ss = SchemaSnapshotStore(tmp_path / "schemas")
        fs = SchemaDriftFindingStore(tmp_path / "drift.jsonl")
        record_snapshot(ss, fs, SchemaSnapshot(source_signature="abc",
                                                 columns=_cols(("id", "int"))))
        record_snapshot(ss, fs, SchemaSnapshot(source_signature="abc",
                                                 columns=_cols(("id", "int"), ("x", "text"))))
        f = detect_schema_drift(fs)[0]
        sig = f.evidence["source_signature"]
        suppressed = detect_schema_drift(fs, suppressed_signatures={sig})
        assert suppressed == []


# ── API integration ─────────────────────────────────────────────────


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

    def test_first_snapshot_records_no_drift(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        r = client.post("/api/steward/schema-snapshot", json={
            "source_signature": "abc", "source_label": "orders",
            "columns": [{"name": "id", "type": "int"}],
        })
        assert r.status_code == 200
        body = r.json()
        assert body["recorded"] is True
        assert body["drift_detected"] is False

    def test_second_snapshot_with_drift_returns_finding_id(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        # Baseline.
        client.post("/api/steward/schema-snapshot", json={
            "source_signature": "abc",
            "columns": [{"name": "id", "type": "int"}],
        })
        # Drift.
        r = client.post("/api/steward/schema-snapshot", json={
            "source_signature": "abc",
            "columns": [{"name": "id", "type": "int"}, {"name": "email", "type": "text"}],
        })
        body = r.json()
        assert body["drift_detected"] is True
        assert body["finding_id"]
        assert len(body["changes"]) == 1
        assert body["changes"][0]["kind"] == "added"

    def test_findings_endpoint_surfaces_drift(self, tmp_path, monkeypatch):
        """End-to-end: record drift via POST → GET /findings shows it."""
        client = self._make_client(tmp_path, monkeypatch)
        client.post("/api/steward/schema-snapshot", json={
            "source_signature": "abc",
            "columns": [{"name": "amount", "type": "int"}],
        })
        client.post("/api/steward/schema-snapshot", json={
            "source_signature": "abc",
            "columns": [{"name": "amount", "type": "decimal"}],
        })
        body = client.get("/api/steward/findings").json()
        kinds = [f["kind"] for f in body["findings"]]
        assert "schema_drift" in kinds

    def test_validates_required_fields(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        # Missing source_signature
        r1 = client.post("/api/steward/schema-snapshot", json={"columns": []})
        assert r1.status_code == 400
        # Missing columns
        r2 = client.post("/api/steward/schema-snapshot", json={"source_signature": "x"})
        assert r2.status_code == 400

    def test_list_snapshots_endpoint(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        client.post("/api/steward/schema-snapshot", json={
            "source_signature": "a", "columns": [{"name": "x", "type": "int"}],
        })
        client.post("/api/steward/schema-snapshot", json={
            "source_signature": "b", "columns": [{"name": "y", "type": "text"}],
        })
        body = client.get("/api/steward/schema-snapshots").json()
        assert body["count"] == 2

    def test_dismiss_silences_subsequent_scans(self, tmp_path, monkeypatch):
        """Dismiss the drift finding → next scan no longer surfaces it."""
        client = self._make_client(tmp_path, monkeypatch)
        client.post("/api/steward/schema-snapshot", json={
            "source_signature": "abc", "columns": [{"name": "id", "type": "int"}],
        })
        r = client.post("/api/steward/schema-snapshot", json={
            "source_signature": "abc", "columns": [{"name": "id", "type": "int"},
                                                     {"name": "x", "type": "text"}],
        })
        finding_id = r.json()["finding_id"]

        # Dismiss it.
        d = client.post(f"/api/steward/findings/{finding_id}/dismiss",
                          json={"reason": "Intentional - new email column requested by analytics"})
        assert d.status_code == 200

        # Re-scan: drift finding is gone.
        body = client.get("/api/steward/findings").json()
        kinds = [f["kind"] for f in body["findings"]]
        assert "schema_drift" not in kinds
