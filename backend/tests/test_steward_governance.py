"""Pinned tests for governance-level Steward detectors (2026-06-07).

env_crossing + unapproved_destination — state-derived (like Archeologist),
runs on the workspace's workflow snapshot at every scan.

Contracts:
  * Empty policy → no findings (the no-op default that lets governance
    ship safely-off until an admin configures it)
  * env_crossing fires only when ≥2 distinct env tags appear in one
    workflow's connection references
  * unapproved_destination fires only when approved_destinations is
    non-empty AND a sink references a connection outside the allowlist
  * Per-(workflow, kind) signature so dismiss silences ONE
    crossing/unapproved-write without taking down the whole detector
"""
from __future__ import annotations

import pytest

from fpulse.steward import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    GovernancePolicy,
    GovernancePolicyStore,
    detect_governance,
)


def _wf(wid, name, nodes):
    return {"id": wid, "name": name, "nodes": nodes}


def _src(connection_id, table="x"):
    return {"id": "n1", "type": "db_source",
            "params": {"connection_id": connection_id, "table": table}}


def _sink(connection_id, table="x"):
    return {"id": "n2", "type": "db_sink",
            "params": {"connection_id": connection_id, "table": table}}


# ── Empty policy ────────────────────────────────────────────────────


class TestEmptyPolicy:
    def test_empty_policy_emits_nothing(self):
        wfs = [_wf("w1", "x", [_src("c1"), _sink("c2")])]
        out = detect_governance(wfs, GovernancePolicy())
        assert out == []


# ── env_crossing ────────────────────────────────────────────────────


class TestEnvCrossing:
    def test_single_env_does_not_fire(self):
        wfs = [_wf("w1", "x", [_src("c1"), _sink("c2")])]
        policy = GovernancePolicy(env_tags={"c1": "prod", "c2": "prod"})
        out = detect_governance(wfs, policy)
        assert out == []

    def test_two_envs_in_one_workflow_fires(self):
        wfs = [_wf("w1", "Mixed pipeline", [_src("c1"), _sink("c2")])]
        policy = GovernancePolicy(env_tags={"c1": "dev", "c2": "prod"})
        out = detect_governance(wfs, policy)
        assert len(out) == 1
        f = out[0]
        assert f.kind == FindingKind.ENV_CROSSING
        assert f.level == FindingLevel.GOVERNANCE
        assert f.severity == FindingSeverity.P1
        assert set(f.evidence["envs"]) == {"dev", "prod"}

    def test_separate_workflows_distinct_envs_dont_cross(self):
        # Each workflow is single-env; the crossing rule is PER WORKFLOW.
        wfs = [
            _wf("w1", "Dev-only", [_src("c1"), _sink("c2")]),
            _wf("w2", "Prod-only", [_src("c3"), _sink("c4")]),
        ]
        policy = GovernancePolicy(env_tags={
            "c1": "dev", "c2": "dev", "c3": "prod", "c4": "prod",
        })
        assert detect_governance(wfs, policy) == []

    def test_unknown_connections_are_ignored(self):
        # A connection with no env tag is just unclassified; doesn't
        # trigger crossing.
        wfs = [_wf("w1", "x", [_src("c1"), _sink("c_untagged")])]
        policy = GovernancePolicy(env_tags={"c1": "prod"})
        assert detect_governance(wfs, policy) == []

    def test_suppression_silences(self):
        wfs = [_wf("w1", "x", [_src("c1"), _sink("c2")])]
        policy = GovernancePolicy(env_tags={"c1": "dev", "c2": "prod"})
        first = detect_governance(wfs, policy)
        sig = first[0].evidence["source_signature"]
        assert detect_governance(wfs, policy, suppressed_signatures={sig}) == []


# ── unapproved_destination ──────────────────────────────────────────


class TestUnapprovedDestination:
    def test_empty_approved_list_disables_detector(self):
        # Empty list = "no allowlist enforced", not "everything unapproved".
        wfs = [_wf("w1", "x", [_sink("anything")])]
        policy = GovernancePolicy(approved_destinations=[])
        assert detect_governance(wfs, policy) == []

    def test_approved_sink_does_not_fire(self):
        wfs = [_wf("w1", "x", [_sink("snowflake_prod")])]
        policy = GovernancePolicy(approved_destinations=["snowflake_prod"])
        assert detect_governance(wfs, policy) == []

    def test_unapproved_sink_fires(self):
        wfs = [_wf("w1", "Risky writer", [_sink("legacy_dw")])]
        policy = GovernancePolicy(approved_destinations=["snowflake_prod"])
        out = detect_governance(wfs, policy)
        assert len(out) == 1
        f = out[0]
        assert f.kind == FindingKind.UNAPPROVED_DESTINATION
        assert f.level == FindingLevel.GOVERNANCE
        assert f.severity == FindingSeverity.P2
        assert "legacy_dw" in f.evidence["unapproved_connections"]

    def test_source_node_not_treated_as_destination(self):
        # An UNAPPROVED source is NOT a finding for this detector —
        # only sinks (destinations).
        wfs = [_wf("w1", "x", [_src("legacy_db"), _sink("snowflake_prod")])]
        policy = GovernancePolicy(approved_destinations=["snowflake_prod"])
        assert detect_governance(wfs, policy) == []

    def test_multiple_unapproved_sinks_collapsed_into_one_finding(self):
        wfs = [_wf("w1", "Multi-sink", [_sink("dw_a"), _sink("dw_b")])]
        policy = GovernancePolicy(approved_destinations=["snowflake_prod"])
        out = detect_governance(wfs, policy)
        assert len(out) == 1
        assert set(out[0].evidence["unapproved_connections"]) == {"dw_a", "dw_b"}

    def test_suppression_silences(self):
        wfs = [_wf("w1", "x", [_sink("legacy_dw")])]
        policy = GovernancePolicy(approved_destinations=["snowflake_prod"])
        sig = detect_governance(wfs, policy)[0].evidence["source_signature"]
        assert detect_governance(wfs, policy, suppressed_signatures={sig}) == []


# ── Policy store I/O ────────────────────────────────────────────────


class TestPolicyStore:
    def test_missing_file_returns_empty_policy(self, tmp_path):
        store = GovernancePolicyStore(tmp_path / "governance.json")
        policy = store.load()
        assert policy.env_tags == {}
        assert policy.approved_destinations == []

    def test_save_then_load_roundtrip(self, tmp_path):
        store = GovernancePolicyStore(tmp_path / "governance.json")
        original = GovernancePolicy(
            env_tags={"c1": "dev", "c2": "prod"},
            approved_destinations=["snowflake_prod"],
        )
        store.save(original)
        back = store.load()
        assert back.env_tags == original.env_tags
        assert back.approved_destinations == original.approved_destinations

    def test_corrupt_file_returns_empty_policy_not_raises(self, tmp_path):
        path = tmp_path / "governance.json"
        path.write_text("{ not valid", encoding="utf-8")
        policy = GovernancePolicyStore(path).load()
        assert policy.env_tags == {}


# ── Both detectors firing on the same workflow ──────────────────────


class TestCombined:
    def test_both_findings_emit_independently(self):
        # Crossing prod+dev AND writing to an unapproved sink → 2 findings.
        wfs = [_wf("w1", "Bad", [_src("c1"), _sink("c2")])]
        policy = GovernancePolicy(
            env_tags={"c1": "dev", "c2": "prod"},
            approved_destinations=["c_only_approved"],
        )
        out = detect_governance(wfs, policy)
        kinds = {f.kind for f in out}
        assert FindingKind.ENV_CROSSING in kinds
        assert FindingKind.UNAPPROVED_DESTINATION in kinds


# ── API integration ────────────────────────────────────────────────


class TestAPIIntegration:
    def _make_client(self, tmp_path, monkeypatch, workflows=None):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import fpulse.api.steward as steward_mod
        import fpulse.main as main_mod
        monkeypatch.setattr(main_mod, "app_state",
                            {"data_dir": str(tmp_path)}, raising=False)
        monkeypatch.setattr(steward_mod, "_workflows_for_scan",
                            lambda ws: workflows or [])
        app = FastAPI()
        from fpulse.auth.deps import require_auth
        app.dependency_overrides[require_auth] = lambda: None
        app.include_router(steward_mod.router)
        return TestClient(app)

    def test_default_governance_policy_is_empty(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        body = client.get("/api/steward/governance").json()
        assert body["policy"]["env_tags"] == {}
        assert body["policy"]["approved_destinations"] == []

    def test_put_policy_then_get_returns_it(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        r = client.put("/api/steward/governance", json={
            "env_tags": {"c1": "prod"},
            "approved_destinations": ["sink_a"],
        })
        assert r.status_code == 200
        back = client.get("/api/steward/governance").json()
        assert back["policy"]["env_tags"] == {"c1": "prod"}
        assert back["policy"]["approved_destinations"] == ["sink_a"]

    def test_findings_endpoint_surfaces_env_crossing(self, tmp_path, monkeypatch):
        wfs = [_wf("w1", "Crosser", [_src("c1"), _sink("c2")])]
        client = self._make_client(tmp_path, monkeypatch, workflows=wfs)
        # Configure the policy that triggers detection.
        client.put("/api/steward/governance", json={
            "env_tags": {"c1": "dev", "c2": "prod"},
        })
        body = client.get("/api/steward/findings").json()
        kinds = [f["kind"] for f in body["findings"]]
        assert "env_crossing" in kinds
