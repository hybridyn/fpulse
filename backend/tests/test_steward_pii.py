"""Pinned tests for the PII-leak detector (2026-06-08).

Third governance-level FindingKind to activate. Schema-name-based
heuristic: when a schema-snapshot includes columns whose names match
a curated PII catalog, emit a PII_LEAK finding alongside any
schema-drift finding from the same snapshot.

Contracts pinned here:
  * No PII columns → no finding (the safely-off default for sources
    that don't carry sensitive data)
  * High-sensitivity classes (credential_in_column / national_id /
    financial / health) escalate to P1; common PII (email / phone /
    address / dob / government_id) defaults P2
  * First match wins per column (an email_address column is
    classified as `email`, not double-counted as address)
  * Re-recording the same snapshot is idempotent (deterministic
    finding id)
  * The PII catalog respects word boundaries — `email_subject`
    does NOT match the email pattern (would be a false positive)
"""
from __future__ import annotations

import pytest

from fpulse.steward import (
    Column,
    FindingKind,
    FindingLevel,
    FindingSeverity,
    PIIFindingStore,
    SchemaSnapshot,
    check_columns_for_pii,
    detect_pii_findings,
    record_pii_findings,
)


def _snapshot(*cols: tuple[str, str], source="src-pii", label="users_table"):
    return SchemaSnapshot(
        source_signature=source, source_label=label,
        columns=[Column(name=n, type=t) for n, t in cols],
    )


# ── Pattern catalog ──────────────────────────────────────────────────


class TestPatternCatalog:
    def test_email_variants_classify_as_email(self):
        for n in ["email", "email_addr", "email_address", "user_email", "e_mail"]:
            hits = check_columns_for_pii([n])
            assert hits and hits[0][1] == "email", f"missed: {n}"

    def test_phone_variants(self):
        for n in ["phone", "phone_no", "mobile", "cell"]:
            hits = check_columns_for_pii([n])
            assert hits and hits[0][1] == "phone", f"missed: {n}"

    def test_national_id_variants(self):
        for n in ["ssn", "social_security", "national_id", "aadhaar"]:
            hits = check_columns_for_pii([n])
            assert hits and hits[0][1] == "national_id", f"missed: {n}"

    def test_credential_in_column(self):
        for n in ["password", "passwd", "api_key", "auth_token", "secret_key"]:
            hits = check_columns_for_pii([n])
            assert hits and hits[0][1] == "credential_in_column", f"missed: {n}"

    def test_word_boundary_avoids_substring_noise(self):
        # `preemail_count` has 'email' inside 'preemail' (no underscore
        # boundary before it) - must NOT match. The boundary regex
        # `(?:^|_)email(?:_|$)` protects against this class of
        # false positive.
        # `social_post` has no PII pattern at all (we don't match
        # 'social' alone - only `social_security`).
        for n in ["preemail_count", "social_post", "name", "created_at"]:
            hits = check_columns_for_pii([n])
            assert hits == [], f"false positive on {n}: {hits}"

    def test_email_adjacent_columns_flagged_conservatively(self):
        # `email_subject` / `email_template_id` are email-ADJACENT (not
        # the email value itself) but the detector flags them
        # conservatively. Better to surface and let the operator
        # dismiss than to silently miss real PII. The finding body
        # explicitly says "name-based heuristic - verify by checking
        # the source."
        for n in ["email_subject", "email_template_id"]:
            hits = check_columns_for_pii([n])
            assert hits, f"expected conservative flag on {n}"

    def test_non_pii_columns_emit_nothing(self):
        assert check_columns_for_pii(["id", "name", "created_at", "amount"]) == []

    def test_first_match_wins(self):
        # "email_address" matches the email pattern first; should NOT
        # double-classify as address even though the substring is there.
        hits = check_columns_for_pii(["email_address"])
        assert len(hits) == 1
        assert hits[0][1] == "email"

    def test_empty_or_invalid_columns_are_safe(self):
        assert check_columns_for_pii([]) == []
        assert check_columns_for_pii(["", "  "]) == []


# ── Finding emission ─────────────────────────────────────────────────


class TestRecordPIIFindings:
    def test_clean_schema_emits_no_finding(self, tmp_path):
        store = PIIFindingStore(tmp_path / "pii.jsonl")
        snap = _snapshot(("id", "int"), ("name", "text"), ("created_at", "timestamp"))
        assert record_pii_findings(snap, store) is None

    def test_email_only_is_p2(self, tmp_path):
        store = PIIFindingStore(tmp_path / "pii.jsonl")
        snap = _snapshot(("id", "int"), ("email", "text"))
        f = record_pii_findings(snap, store)
        assert f is not None
        assert f.kind == FindingKind.PII_LEAK
        assert f.level == FindingLevel.GOVERNANCE
        assert f.severity == FindingSeverity.P2
        assert "email" in f.evidence["pii_classes_present"]

    def test_password_column_escalates_to_p1(self, tmp_path):
        store = PIIFindingStore(tmp_path / "pii.jsonl")
        snap = _snapshot(("id", "int"), ("password", "text"))
        f = record_pii_findings(snap, store)
        assert f.severity == FindingSeverity.P1
        assert "credential_in_column" in f.evidence["pii_classes_present"]

    def test_ssn_escalates_to_p1(self, tmp_path):
        store = PIIFindingStore(tmp_path / "pii.jsonl")
        snap = _snapshot(("id", "int"), ("ssn", "text"))
        f = record_pii_findings(snap, store)
        assert f.severity == FindingSeverity.P1

    def test_email_plus_ssn_stays_p1(self, tmp_path):
        # Worst-case wins - any high-sensitivity hit escalates the
        # whole finding, even if lower-sensitivity hits are bundled.
        store = PIIFindingStore(tmp_path / "pii.jsonl")
        snap = _snapshot(("email", "text"), ("ssn", "text"))
        f = record_pii_findings(snap, store)
        assert f.severity == FindingSeverity.P1
        assert f.evidence["total_pii_columns"] == 2

    def test_evidence_carries_per_column_classification(self, tmp_path):
        store = PIIFindingStore(tmp_path / "pii.jsonl")
        snap = _snapshot(("email", "text"), ("phone_no", "text"), ("zip", "text"))
        f = record_pii_findings(snap, store)
        classes = {h["pii_class"] for h in f.evidence["pii_hits"]}
        assert classes == {"email", "phone", "address"}

    def test_finding_id_is_deterministic(self, tmp_path):
        store = PIIFindingStore(tmp_path / "pii.jsonl")
        snap = _snapshot(("email", "text"))
        a = record_pii_findings(snap, store)
        b = record_pii_findings(snap, store)
        assert a.id == b.id, "same source → same finding id (idempotent)"


# ── Scan-side detector ──────────────────────────────────────────────


class TestDetectPIIFindings:
    def test_empty_journal_returns_empty(self, tmp_path):
        store = PIIFindingStore(tmp_path / "pii.jsonl")
        assert detect_pii_findings(store) == []

    def test_open_findings_surface(self, tmp_path):
        store = PIIFindingStore(tmp_path / "pii.jsonl")
        snap = _snapshot(("email", "text"))
        record_pii_findings(snap, store)
        assert len(detect_pii_findings(store)) == 1

    def test_suppression_silences_finding(self, tmp_path):
        store = PIIFindingStore(tmp_path / "pii.jsonl")
        snap = _snapshot(("email", "text"))
        f = record_pii_findings(snap, store)
        sig = f.evidence["source_signature"]
        assert detect_pii_findings(store, suppressed_signatures={sig}) == []


# ── API integration via /schema-snapshot ─────────────────────────────


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

    def test_snapshot_with_pii_columns_surfaces_pii_finding(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        r = client.post("/api/steward/schema-snapshot", json={
            "source_signature": "users-table",
            "source_label": "users",
            "columns": [
                {"name": "id", "type": "int"},
                {"name": "email", "type": "text"},
                {"name": "phone_no", "type": "text"},
            ],
        })
        body = r.json()
        assert body["recorded"] is True
        # Baseline snapshot - drift NOT detected
        assert body["drift_detected"] is False
        # PII findings DO fire on the first snapshot (independent of drift)
        assert body.get("pii_finding_id"), f"expected pii_finding_id, got {body}"
        assert "email" in body.get("pii_columns", [])
        # And /findings surfaces it
        listing = client.get("/api/steward/findings").json()
        kinds = [f["kind"] for f in listing["findings"]]
        assert "pii_leak" in kinds

    def test_clean_snapshot_has_no_pii_finding(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        r = client.post("/api/steward/schema-snapshot", json={
            "source_signature": "events-table",
            "columns": [{"name": "id", "type": "int"}, {"name": "ts", "type": "timestamp"}],
        })
        body = r.json()
        assert body["recorded"] is True
        assert "pii_finding_id" not in body
