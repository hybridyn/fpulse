"""Pinned tests for the connector-health detector (2026-06-07).

The first CONNECTOR-level Steward signal. These pin error classification,
the streak-counter state machine, the time-clamped emission rule, the
credential-expiry path, and full API integration (recording outcomes
via POST and seeing findings emerge in GET /findings).

These guarantees MUST hold:
  * One flap (consecutive_failures=1) NEVER emits a finding.
  * A failure streak that just started (< 5 minutes old) is held back.
  * A success resets the streak to zero and clears first_failure_at.
  * The detector picks the right FindingKind from the error class.
  * Suppression silences ONE (connection, kind) tuple without taking
    down the rest.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fpulse.steward import (
    ConnectorHealthState,
    ConnectorHealthStore,
    FindingKind,
    FindingLevel,
    FindingSeverity,
    classify_error,
    detect_connector_health,
    record_test_outcome,
)


def _hours_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=n)).isoformat()


def _days_ahead(n: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=n)).isoformat()


def _hours_ahead(n: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=n)).isoformat()


# ── Error classifier ────────────────────────────────────────────────


class TestErrorClassifier:
    """Maps free-text test errors to the canonical class set
    {auth_error, rate_limit, timeout, unreachable, unknown}. Drives
    which FindingKind the detector emits."""

    def test_auth_keywords(self):
        for msg in [
            "401 Unauthorized",
            "permission denied",
            "Invalid credentials",
            "Authentication failed",
            "Forbidden",
            "invalid token",
        ]:
            assert classify_error(msg) == "auth_error", msg

    def test_rate_limit_keywords(self):
        for msg in [
            "429 Too Many Requests",
            "rate limit exceeded",
            "Throttling applied",
            "quota exceeded for the day",
        ]:
            assert classify_error(msg) == "rate_limit", msg

    def test_timeout_keywords(self):
        for msg in ["Connection timed out", "deadline exceeded", "request timeout"]:
            assert classify_error(msg) == "timeout", msg

    def test_unreachable_keywords(self):
        for msg in [
            "Connection refused",
            "Name or service not known",
            "Could not connect to host",
            "No route to host",
            "SSL handshake failed",
        ]:
            assert classify_error(msg) == "unreachable", msg

    def test_unknown_falls_through(self):
        assert classify_error("something completely random") == "unknown"
        assert classify_error("") == "unknown"
        assert classify_error(None) == "unknown"

    def test_auth_takes_precedence_over_rate_limit_on_4xx(self):
        # "401 Unauthorized" must classify as auth, not match an
        # accidental 4xx-rate-limit pattern.
        assert classify_error("401 Unauthorized — credentials rejected") == "auth_error"


# ── Recorder state machine ──────────────────────────────────────────


class TestRecorderStateMachine:
    """record_test_outcome implements the streak counter. These are
    the invariants the detector reads."""

    def test_first_failure_starts_streak_at_one(self, tmp_path):
        store = ConnectorHealthStore(tmp_path / "ch.json")
        state = record_test_outcome(store, connection_id="c1", ok=False,
                                     error_message="401 Unauthorized")
        assert state.consecutive_failures == 1
        assert state.last_status == "failing"
        assert state.last_error_class == "auth_error"
        assert state.first_failure_at is not None

    def test_consecutive_failures_increment_streak(self, tmp_path):
        store = ConnectorHealthStore(tmp_path / "ch.json")
        record_test_outcome(store, connection_id="c1", ok=False, error_message="x")
        record_test_outcome(store, connection_id="c1", ok=False, error_message="x")
        state = record_test_outcome(store, connection_id="c1", ok=False, error_message="x")
        assert state.consecutive_failures == 3

    def test_first_failure_at_stays_stable_across_failure_streak(self, tmp_path):
        # Same streak → first_failure_at is recorded ONCE and preserved
        # across subsequent failures. Critical for the time-clamp rule.
        store = ConnectorHealthStore(tmp_path / "ch.json")
        s1 = record_test_outcome(store, connection_id="c1", ok=False, error_message="x")
        s2 = record_test_outcome(store, connection_id="c1", ok=False, error_message="x")
        s3 = record_test_outcome(store, connection_id="c1", ok=False, error_message="x")
        assert s1.first_failure_at == s2.first_failure_at == s3.first_failure_at

    def test_success_resets_streak_and_clears_first_failure(self, tmp_path):
        store = ConnectorHealthStore(tmp_path / "ch.json")
        record_test_outcome(store, connection_id="c1", ok=False, error_message="x")
        record_test_outcome(store, connection_id="c1", ok=False, error_message="x")
        state = record_test_outcome(store, connection_id="c1", ok=True)
        assert state.consecutive_failures == 0
        assert state.first_failure_at is None
        assert state.last_status == "healthy"

    def test_failure_after_recovery_starts_fresh_streak(self, tmp_path):
        # Failure → success → failure must produce streak=1, not
        # streak=3. Critical for "we fixed it, but it broke again"
        # to NOT auto-escalate.
        store = ConnectorHealthStore(tmp_path / "ch.json")
        record_test_outcome(store, connection_id="c1", ok=False, error_message="x")
        record_test_outcome(store, connection_id="c1", ok=False, error_message="x")
        record_test_outcome(store, connection_id="c1", ok=True)
        state = record_test_outcome(store, connection_id="c1", ok=False, error_message="x")
        assert state.consecutive_failures == 1


# ── Detector ────────────────────────────────────────────────────────


class TestDetector:
    """The detector reads health-store state + connection metadata and
    emits StewardFindings when the time-clamp + streak conditions hold."""

    def _seed_state(self, store, **kwargs):
        defaults = dict(
            connection_id="c1",
            consecutive_failures=5,
            first_failure_at=_hours_ago(2),  # well past time-clamp
            last_check_at=_hours_ago(0),
            last_status="failing",
            last_error_class="auth_error",
            last_error_message="401 Unauthorized",
        )
        defaults.update(kwargs)
        store.upsert(ConnectorHealthState(**defaults))

    def _connection(self, **kwargs):
        d = {"id": "c1", "name": "Prod DB", "type": "postgres"}
        d.update(kwargs)
        return d

    def test_single_flap_is_not_flagged(self, tmp_path):
        store = ConnectorHealthStore(tmp_path / "ch.json")
        self._seed_state(store, consecutive_failures=1, first_failure_at=_hours_ago(1))
        findings = detect_connector_health([self._connection()], store)
        assert findings == [], "single flap must not produce a finding"

    def test_streak_under_5_minutes_is_held_back(self, tmp_path):
        # Time-clamp - even with streak=3, if it just started 30s ago,
        # no finding (a 30-second burst of failures is not actionable).
        store = ConnectorHealthStore(tmp_path / "ch.json")
        thirty_seconds_ago = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        self._seed_state(store, consecutive_failures=3,
                          first_failure_at=thirty_seconds_ago)
        findings = detect_connector_health([self._connection()], store)
        assert findings == [], "sub-5-minute streak must not emit"

    def test_sustained_auth_failure_emits_auth_finding(self, tmp_path):
        store = ConnectorHealthStore(tmp_path / "ch.json")
        self._seed_state(store, last_error_class="auth_error")
        findings = detect_connector_health([self._connection()], store)
        assert len(findings) == 1
        f = findings[0]
        assert f.kind == FindingKind.CONNECTOR_AUTH_FAILURE
        assert f.level == FindingLevel.CONNECTOR
        assert f.evidence["connection_id"] == "c1"
        assert f.evidence["consecutive_failures"] == 5

    def test_unreachable_emits_unreachable_finding(self, tmp_path):
        store = ConnectorHealthStore(tmp_path / "ch.json")
        self._seed_state(store, last_error_class="unreachable")
        findings = detect_connector_health([self._connection()], store)
        assert findings[0].kind == FindingKind.CONNECTOR_UNREACHABLE

    def test_rate_limit_emits_rate_limit_finding(self, tmp_path):
        store = ConnectorHealthStore(tmp_path / "ch.json")
        self._seed_state(store, last_error_class="rate_limit")
        findings = detect_connector_health([self._connection()], store)
        assert findings[0].kind == FindingKind.CONNECTOR_RATE_LIMIT

    def test_timeout_collapses_to_unreachable(self, tmp_path):
        # Timeout and unreachable are operationally similar - same
        # FindingKind so users don't have two near-identical alert
        # categories to triage.
        store = ConnectorHealthStore(tmp_path / "ch.json")
        self._seed_state(store, last_error_class="timeout")
        findings = detect_connector_health([self._connection()], store)
        assert findings[0].kind == FindingKind.CONNECTOR_UNREACHABLE

    def test_severity_scales_with_streak(self, tmp_path):
        store = ConnectorHealthStore(tmp_path / "ch.json")
        cases = [(2, FindingSeverity.P3), (4, FindingSeverity.P2), (10, FindingSeverity.P1)]
        for streak, expected in cases:
            self._seed_state(store, consecutive_failures=streak)
            findings = detect_connector_health([self._connection()], store)
            assert findings[0].severity == expected, f"streak={streak}"

    def test_healthy_connection_emits_nothing(self, tmp_path):
        store = ConnectorHealthStore(tmp_path / "ch.json")
        store.upsert(ConnectorHealthState(
            connection_id="c1", consecutive_failures=0, last_status="healthy",
        ))
        findings = detect_connector_health([self._connection()], store)
        assert findings == []

    def test_suppression_silences_one_kind_only(self, tmp_path):
        # User suppressed the auth-failure signature on c1. Steward
        # still emits OTHER kinds for c1 if they appear.
        store = ConnectorHealthStore(tmp_path / "ch.json")
        # Two states: c1 failing with auth_error, c2 failing with unreachable.
        self._seed_state(store, connection_id="c1", last_error_class="auth_error")
        self._seed_state(store, connection_id="c2", last_error_class="unreachable")
        # Pull the c1-auth signature so we can suppress it.
        first = detect_connector_health(
            [self._connection(id="c1"), self._connection(id="c2", name="Other")], store,
        )
        sigs = {f.evidence["connection_id"]: f.evidence["source_signature"] for f in first}
        assert "c1" in sigs and "c2" in sigs

        # Now suppress c1's auth signature only.
        second = detect_connector_health(
            [self._connection(id="c1"), self._connection(id="c2", name="Other")],
            store,
            suppressed_signatures={sigs["c1"]},
        )
        emitted_conns = {f.evidence["connection_id"] for f in second}
        assert "c1" not in emitted_conns, "suppressed signature must be silenced"
        assert "c2" in emitted_conns, "other connection must still emit"


# ── Credential expiry ───────────────────────────────────────────────


class TestCredentialExpiry:
    """CREDENTIAL_NEAR_EXPIRY is independent of failure-streak - it
    fires when the recorded expiry is within the warning window even
    if the connection is currently healthy."""

    def test_within_warning_window_emits(self, tmp_path):
        store = ConnectorHealthStore(tmp_path / "ch.json")
        store.upsert(ConnectorHealthState(
            connection_id="c1",
            consecutive_failures=0,
            last_status="healthy",
            credential_expires_at=_days_ahead(3),  # 3 days away
        ))
        findings = detect_connector_health(
            [{"id": "c1", "name": "Snowflake", "type": "snowflake"}], store,
        )
        assert len(findings) == 1
        assert findings[0].kind == FindingKind.CREDENTIAL_NEAR_EXPIRY

    def test_outside_warning_window_does_not_emit(self, tmp_path):
        store = ConnectorHealthStore(tmp_path / "ch.json")
        store.upsert(ConnectorHealthState(
            connection_id="c1",
            credential_expires_at=_days_ahead(30),  # well outside 7-day window
        ))
        findings = detect_connector_health(
            [{"id": "c1", "name": "x", "type": "x"}], store,
        )
        assert findings == []

    def test_imminent_expiry_emits_p1(self, tmp_path):
        # < 1 day = page-worthy. Use 12 hours so expires_dt is reliably
        # in the future (vs `now+0d` which races with detector's
        # `datetime.now()` call) but still classifies as "imminent".
        store = ConnectorHealthStore(tmp_path / "ch.json")
        store.upsert(ConnectorHealthState(
            connection_id="c1", credential_expires_at=_hours_ahead(12),
        ))
        findings = detect_connector_health(
            [{"id": "c1", "name": "x", "type": "x"}], store,
        )
        assert findings[0].severity == FindingSeverity.P1


# ── API integration ────────────────────────────────────────────────


class TestAPIIntegration:
    """End-to-end: POST /connector-health → GET /findings shows the
    finding emerging through the scan path."""

    def _make_client(self, tmp_path, monkeypatch, connections=None):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import fpulse.api.steward as steward_mod
        import fpulse.main as main_mod

        monkeypatch.setattr(main_mod, "app_state",
                            {"data_dir": str(tmp_path)}, raising=False)
        # Empty workflows - we're testing the connector-health path.
        monkeypatch.setattr(steward_mod, "_workflows_for_scan", lambda ws: [])

        # Stub the connection store to return the supplied list.
        class _StubStore:
            def __init__(self, conns):
                self._conns = conns

            def list_all(self, workspace_id=None):
                return self._conns

        import fpulse.api.connections as conn_mod
        monkeypatch.setattr(conn_mod, "get_store", lambda: _StubStore(connections or []))

        app = FastAPI()
        from fpulse.auth.deps import require_auth
        app.dependency_overrides[require_auth] = lambda: None
        app.include_router(steward_mod.router)
        return TestClient(app)

    def test_record_endpoint_persists_state(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        r = client.post("/api/steward/connector-health", json={
            "connection_id": "c1", "ok": False,
            "error_message": "401 Unauthorized",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["recorded"] is True
        assert body["state"]["consecutive_failures"] == 1
        assert body["state"]["last_error_class"] == "auth_error"

    def test_list_endpoint_returns_recorded_states(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        client.post("/api/steward/connector-health", json={
            "connection_id": "c1", "ok": False, "error_message": "401",
        })
        client.post("/api/steward/connector-health", json={
            "connection_id": "c2", "ok": True,
        })
        r = client.get("/api/steward/connector-health")
        assert r.json()["count"] == 2

    def test_record_validates_required_fields(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        r1 = client.post("/api/steward/connector-health", json={"ok": True})
        assert r1.status_code == 400
        r2 = client.post("/api/steward/connector-health", json={"connection_id": "c1"})
        assert r2.status_code == 400

    def test_findings_endpoint_surfaces_sustained_failure(self, tmp_path, monkeypatch):
        """Full path - seed failure state via the POST endpoint (with
        a forced first_failure_at in the past so the time-clamp passes),
        then GET /findings and confirm the connector-health finding
        emerges through the same scan path Archeologist uses."""
        connections = [{"id": "c1", "name": "Prod PG", "type": "postgres"}]
        client = self._make_client(tmp_path, monkeypatch, connections=connections)

        # Seed 5 consecutive failures via the recorder, then back-date
        # first_failure_at past the 5-minute clamp.
        for _ in range(5):
            client.post("/api/steward/connector-health", json={
                "connection_id": "c1", "ok": False,
                "error_message": "Connection refused on port 5432",
            })
        # Back-date the first_failure_at directly in the store - the
        # recorder always sets it to "now", but the time-clamp is
        # what we want to exercise here.
        from fpulse.steward import ConnectorHealthStore
        from pathlib import Path
        store = ConnectorHealthStore(Path(tmp_path) / "steward" / "default" / "connector_health.json")
        state = store.get("c1")
        state.first_failure_at = _hours_ago(1)
        store.upsert(state)

        body = client.get("/api/steward/findings").json()
        kinds = [f["kind"] for f in body["findings"]]
        assert "connector_unreachable" in kinds, f"expected connector_unreachable in {kinds}"
