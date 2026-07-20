"""Tests for RunManifest + FreshnessGate.

The freshness contract is what stops a 6-hour-cycle source from
being polled every 5 minutes. The gate reads the latest manifest
for a profile and decides — these tests lock down every branch:
no prior run, recent run, expired run, force override.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import pytest

from fpulse.extraction.freshness import (
    FreshnessBlocked,
    FreshnessDecision,
    FreshnessGate,
)
from fpulse.extraction.manifest import (
    RunManifest,
    schema_fingerprint_from_profile,
)


# ── Manifest serialisation + lookup ─────────────────────────────────

def test_manifest_round_trips(tmp_path):
    m = RunManifest(
        run_id="run-001", profile_name="test", started_at=1.0,
        completed_at=2.0, duration_s=1.0,
        row_counts={"extracted": 100, "failed": 2},
        schema_fingerprint="abc123", output_format="jsonl",
        output_path="/tmp/x.jsonl", failed_path="/tmp/x.jsonl.failed.jsonl",
        output_size_bytes=4096,
    )
    saved = m.save(str(tmp_path))
    with open(saved) as f:
        data = json.load(f)
    assert data["run_id"] == "run-001"
    assert data["row_counts"]["extracted"] == 100
    # Roundtrip through from_dict.
    restored = RunManifest.from_dict(data)
    assert restored.run_id == "run-001"
    assert restored.duration_s == 1.0


def test_latest_picks_most_recently_completed(tmp_path):
    """Multiple manifests for the same profile — latest() must return
    the one with the most-recent `completed_at`, not file mtime or
    lexicographic ordering."""
    for run_id, completed in [("run-old", 100.0), ("run-new", 200.0), ("run-mid", 150.0)]:
        RunManifest(
            run_id=run_id, profile_name="myprof",
            started_at=completed - 10, completed_at=completed, duration_s=10.0,
        ).save(str(tmp_path))
    latest = RunManifest.latest(str(tmp_path), "myprof")
    assert latest is not None
    assert latest.run_id == "run-new"


def test_latest_ignores_in_progress_runs(tmp_path):
    """A run with completed_at=None (still in progress / crashed) must
    not be returned as 'latest' — the freshness gate shouldn't gate on
    runs that never finished."""
    RunManifest(
        run_id="ongoing", profile_name="myprof",
        started_at=time.time(), completed_at=None, duration_s=None,
    ).save(str(tmp_path))
    assert RunManifest.latest(str(tmp_path), "myprof") is None


def test_latest_returns_none_for_unknown_profile(tmp_path):
    RunManifest(
        run_id="run-1", profile_name="other",
        started_at=1.0, completed_at=2.0, duration_s=1.0,
    ).save(str(tmp_path))
    assert RunManifest.latest(str(tmp_path), "myprof") is None


def test_latest_skips_corrupted_manifests(tmp_path):
    """A manifest file with bad JSON shouldn't break the lookup —
    ignore it and continue."""
    bad = tmp_path / "myprof__corrupt.manifest.json"
    bad.write_text("{bad json")
    # A valid manifest alongside.
    RunManifest(
        run_id="good", profile_name="myprof",
        started_at=1.0, completed_at=2.0, duration_s=1.0,
    ).save(str(tmp_path))
    latest = RunManifest.latest(str(tmp_path), "myprof")
    assert latest is not None and latest.run_id == "good"


# ── Schema fingerprint ──────────────────────────────────────────────

@dataclass
class _FakeSchema:
    field_paths: dict
    coercions: dict


@dataclass
class _FakeProfile:
    schema: _FakeSchema


def test_schema_fingerprint_changes_when_paths_change():
    a = _FakeProfile(schema=_FakeSchema(
        field_paths={"id": "id", "name": "computer_name"}, coercions={}))
    b = _FakeProfile(schema=_FakeSchema(
        field_paths={"id": "id", "name": "host_name"}, coercions={}))
    assert (schema_fingerprint_from_profile(a)
            != schema_fingerprint_from_profile(b))


def test_schema_fingerprint_changes_when_coercion_changes():
    a = _FakeProfile(schema=_FakeSchema(
        field_paths={"id": "id"}, coercions={"id": "int"}))
    b = _FakeProfile(schema=_FakeSchema(
        field_paths={"id": "id"}, coercions={"id": "str"}))
    assert (schema_fingerprint_from_profile(a)
            != schema_fingerprint_from_profile(b))


def test_schema_fingerprint_stable_for_same_inputs():
    a = _FakeProfile(schema=_FakeSchema(
        field_paths={"id": "id", "name": "n"},
        coercions={"id": "int"}))
    b = _FakeProfile(schema=_FakeSchema(
        field_paths={"name": "n", "id": "id"},  # different dict order
        coercions={"id": "int"}))
    # Order-independent — sorted keys produce stable hash.
    assert (schema_fingerprint_from_profile(a)
            == schema_fingerprint_from_profile(b))


# ── Freshness gate ──────────────────────────────────────────────────

@dataclass
class _GateProfile:
    name: str = "test_gate_profile"
    freshness_interval_seconds: int | None = None


def test_gate_allows_when_no_freshness_interval(tmp_path):
    gate = FreshnessGate(str(tmp_path))
    decision = gate.check(_GateProfile(freshness_interval_seconds=None))
    assert decision.allowed is True
    assert "no freshness_interval" in decision.reason.lower()


def test_gate_allows_when_no_prior_run(tmp_path):
    gate = FreshnessGate(str(tmp_path))
    decision = gate.check(_GateProfile(freshness_interval_seconds=3600))
    assert decision.allowed is True
    assert "no prior" in decision.reason.lower()


def test_gate_blocks_when_within_freshness_interval(tmp_path):
    """Last run completed 10 minutes ago, contract says 1 hour →
    blocked, with next_allowed_at set to last + interval."""
    last_completed = time.time() - 600  # 10 min ago
    RunManifest(
        run_id="recent", profile_name="myp",
        started_at=last_completed - 60, completed_at=last_completed,
        duration_s=60.0,
    ).save(str(tmp_path))
    gate = FreshnessGate(str(tmp_path))
    decision = gate.check(_GateProfile(name="myp", freshness_interval_seconds=3600))
    assert decision.allowed is False
    assert decision.last_completed_at == last_completed
    assert decision.next_allowed_at is not None
    assert decision.next_allowed_at > time.time()


def test_gate_allows_when_past_freshness_interval(tmp_path):
    last_completed = time.time() - 7200  # 2 hours ago
    RunManifest(
        run_id="old", profile_name="myp",
        started_at=last_completed - 60, completed_at=last_completed,
        duration_s=60.0,
    ).save(str(tmp_path))
    gate = FreshnessGate(str(tmp_path))
    decision = gate.check(_GateProfile(name="myp", freshness_interval_seconds=3600))
    assert decision.allowed is True


def test_gate_force_bypasses(tmp_path):
    """Force override always passes; decision records that it was
    forced so the audit log can flag it."""
    last_completed = time.time() - 60
    RunManifest(
        run_id="recent", profile_name="myp",
        started_at=last_completed - 10, completed_at=last_completed,
        duration_s=10.0,
    ).save(str(tmp_path))
    gate = FreshnessGate(str(tmp_path))
    decision = gate.check(_GateProfile(name="myp", freshness_interval_seconds=3600),
                            force=True)
    assert decision.allowed is True
    assert decision.forced is True
    assert "force" in decision.reason.lower()


def test_freshness_blocked_exception_carries_decision():
    decision = FreshnessDecision(
        allowed=False, reason="too soon",
        last_completed_at=100.0, next_allowed_at=200.0,
    )
    exc = FreshnessBlocked(decision)
    assert exc.decision is decision
    assert "too soon" in str(exc)
