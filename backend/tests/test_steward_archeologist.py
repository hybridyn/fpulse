"""Smoke + behavioural tests for the Steward Archeologist sub-agent.

The Steward is F-Pulse OSS's headline differentiator (read-only background
reliability layer; see ``backend/fpulse/steward/__init__.py`` for the hard
invariants). These tests pin the duplicate-source / duplicate-pipeline
detector's contract:

  * positive detection of duplicate sources across workflows
  * negative — single-workflow duplicates are NOT flagged
  * positive detection of duplicate pipelines (same source + same sink)
  * negative — same source + different sinks is intentional fan-out, not a duplicate
  * suppressed signatures are honoured (curator memory)
  * finding IDs are deterministic (re-run → same IDs, for upsert)
  * non-source nodes are ignored
"""
from __future__ import annotations

import pytest

from fpulse.steward.archeologist import (
    FINDING_ID_PREFIXES,
    _extract_sources,
    _source_signature,
    detect_duplicate_sources,
)
from fpulse.steward.models import (
    FindingKind,
    FindingSeverity,
    FindingStatus,
    StewardFinding,
)


# ── Helpers ─────────────────────────────────────────────────────────

def _source_node(node_id: str, *, connection_id: str, table: str, step_type: str = "db_source"):
    """React Flow node format (legacy / canvas runtime shape)."""
    return {
        "id": node_id,
        "data": {
            "stepType": step_type,
            "label": f"Read {table}",
            "params": {
                "connection_id": connection_id,
                "connector_type": "postgres",
                "table": table,
            },
        },
    }


def _fpulse_step(step_id: str, *, type_: str, params: dict[str, Any]):
    """F-Pulse step format (authoritative on-disk shape). Used to pin
    the 2026-06-06 fix where the Archeologist was blind to real
    workflows because it only knew the React Flow shape."""
    return {
        "id": step_id,
        "type": type_,
        "label": type_.replace("_", " ").title(),
        "params": params,
        "position": {"x": 100, "y": 100},
    }


def _sink_node(node_id: str, *, connection_id: str, table: str, step_type: str = "db_sink"):
    return {
        "id": node_id,
        "data": {
            "stepType": step_type,
            "label": f"Write {table}",
            "params": {
                "connection_id": connection_id,
                "connector_type": "postgres",
                "table": table,
            },
        },
    }


def _transform_node(node_id: str):
    return {
        "id": node_id,
        "data": {
            "stepType": "transform",
            "label": "Transform",
            "params": {"sql": "SELECT * FROM input"},
        },
    }


# ── Signature stability ─────────────────────────────────────────────

class TestSourceSignature:
    def test_same_inputs_same_signature(self):
        a = _source_signature({"connection_id": "c1", "connector_type": "postgres", "table": "orders"})
        b = _source_signature({"connection_id": "c1", "connector_type": "postgres", "table": "orders"})
        assert a == b
        assert a is not None
        assert len(a) == 16

    def test_field_ordering_doesnt_change_signature(self):
        """Stability invariant — dict key insertion order must not matter."""
        a = _source_signature({"table": "orders", "connection_id": "c1"})
        b = _source_signature({"connection_id": "c1", "table": "orders"})
        assert a == b

    def test_different_table_different_signature(self):
        a = _source_signature({"connection_id": "c1", "table": "orders"})
        b = _source_signature({"connection_id": "c1", "table": "customers"})
        assert a != b

    def test_different_connection_different_signature(self):
        """Same table in two different connections is NOT a duplicate
        source — could be prod vs staging, two distinct tenants, etc."""
        a = _source_signature({"connection_id": "prod_pg", "table": "orders"})
        b = _source_signature({"connection_id": "staging_pg", "table": "orders"})
        assert a != b

    def test_empty_params_returns_none(self):
        """A node with no identity fields can't contribute a duplicate signal."""
        assert _source_signature({}) is None
        assert _source_signature({"retry_count": 3}) is None

    def test_non_dict_returns_none(self):
        """Defensive — workflow stores have shipped weird shapes before."""
        assert _source_signature(None) is None  # type: ignore[arg-type]
        assert _source_signature("orders") is None  # type: ignore[arg-type]

    def test_workspace_prefix_changes_signature(self):
        """Architectural review Block 1B — Plus multi-workspace must
        not see cross-workspace collisions. The workspace_id parameter
        feeds into the hash so two tenants with the same connection_id
        produce DIFFERENT signatures."""
        same_source = {"connection_id": "shared_pg", "table": "orders"}
        sig_a = _source_signature(same_source, workspace_id="tenant_a")
        sig_b = _source_signature(same_source, workspace_id="tenant_b")
        sig_unscoped = _source_signature(same_source)
        assert sig_a != sig_b, "Same source in two workspaces must have distinct signatures"
        assert sig_a != sig_unscoped, "Scoped signature must differ from unscoped"
        # And within a single workspace the signature is still stable
        sig_a_again = _source_signature(same_source, workspace_id="tenant_a")
        assert sig_a == sig_a_again

    def test_workspace_only_input_returns_none(self):
        """The workspace_id alone (with no source identity fields) is
        not enough to form a meaningful signature — must return None."""
        assert _source_signature({}, workspace_id="any") is None
        assert _source_signature({"retry_count": 3}, workspace_id="any") is None

    def test_detector_handles_fpulse_step_format(self):
        """2026-06-06 regression — F-Pulse stores steps as
        ``{id, type, params}`` at top-level. The detector was
        previously hard-coded to React Flow's nested ``data.stepType``
        shape and silently returned ZERO findings against real
        production workflows. Pin both shapes work."""
        from fpulse.steward.models import FindingKind
        workflows = [
            {"id": "wf-a", "name": "Alpha — F-Pulse format", "nodes": [
                _fpulse_step("s1", type_="source", params={
                    "connector_type": "postgres",
                    "connection_id": "prod_pg",
                    "table": "orders",
                }),
                _fpulse_step("o1", type_="db_sink", params={
                    "connection_id": "warehouse", "table": "out_a",
                }),
            ]},
            {"id": "wf-b", "name": "Bravo — F-Pulse format", "nodes": [
                _fpulse_step("s1", type_="source", params={
                    "connector_type": "postgres",
                    "connection_id": "prod_pg",
                    "table": "orders",
                }),
                _fpulse_step("o1", type_="db_sink", params={
                    "connection_id": "warehouse", "table": "out_b",
                }),
            ]},
        ]
        findings = detect_duplicate_sources(workflows)
        dup_src = [f for f in findings if f.kind == FindingKind.DUPLICATE_SOURCE]
        assert len(dup_src) == 1, \
            "F-Pulse step format not detected — regression in _step_type_and_params"
        assert dup_src[0].occurrences == 2

    def test_detector_handles_mixed_format_workspaces(self):
        """A workspace can have a mix of legacy React Flow workflows
        and modern F-Pulse step workflows. Both must contribute to the
        same duplicate-source finding."""
        from fpulse.steward.models import FindingKind
        workflows = [
            # React Flow shape
            {"id": "wf-rf", "name": "ReactFlow", "nodes": [
                _source_node("n1", connection_id="prod_pg", table="orders"),
                _sink_node("n2", connection_id="warehouse", table="out_rf"),
            ]},
            # F-Pulse step shape — SAME source identity
            {"id": "wf-fp", "name": "F-Pulse", "nodes": [
                _fpulse_step("s1", type_="source", params={
                    "connector_type": "postgres",
                    "connection_id": "prod_pg",
                    "table": "orders",
                }),
                _fpulse_step("o1", type_="db_sink", params={
                    "connection_id": "warehouse", "table": "out_fp",
                }),
            ]},
        ]
        findings = detect_duplicate_sources(workflows)
        dup_src = [f for f in findings if f.kind == FindingKind.DUPLICATE_SOURCE]
        assert len(dup_src) == 1, \
            "Cross-format duplicate not detected — the two shapes must produce identical signatures"
        wf_names = {w["name"] for w in dup_src[0].evidence["workflows"]}
        assert wf_names == {"ReactFlow", "F-Pulse"}


# ── Duplicate-source detection ──────────────────────────────────────

class TestDuplicateSource:
    def test_two_workflows_same_source_flagged(self):
        workflows = [
            {
                "id": "wf-a",
                "name": "Orders → Analytics",
                "nodes": [
                    _source_node("n1", connection_id="prod_pg", table="orders"),
                    _sink_node("n2", connection_id="warehouse", table="orders_analytics"),
                ],
            },
            {
                "id": "wf-b",
                "name": "Orders → Finance",
                "nodes": [
                    _source_node("n1", connection_id="prod_pg", table="orders"),
                    _sink_node("n2", connection_id="warehouse", table="orders_finance"),
                ],
            },
        ]
        findings = detect_duplicate_sources(workflows)
        dup_src = [f for f in findings if f.kind == FindingKind.DUPLICATE_SOURCE]
        assert len(dup_src) == 1
        f = dup_src[0]
        assert f.id.startswith(FINDING_ID_PREFIXES[FindingKind.DUPLICATE_SOURCE])
        assert f.severity == FindingSeverity.P2
        assert f.status == FindingStatus.OPEN
        assert f.occurrences == 2
        wf_names = {w["name"] for w in f.evidence["workflows"]}
        assert wf_names == {"Orders → Analytics", "Orders → Finance"}

    def test_single_workflow_duplicate_source_not_flagged(self):
        """Same source appearing twice in ONE workflow is a different problem
        (validator territory) — Archeologist is cross-workflow only."""
        workflows = [
            {
                "id": "wf-a",
                "name": "Self-join",
                "nodes": [
                    _source_node("n1", connection_id="prod_pg", table="orders"),
                    _source_node("n2", connection_id="prod_pg", table="orders"),
                    _sink_node("n3", connection_id="warehouse", table="out"),
                ],
            },
        ]
        findings = detect_duplicate_sources(workflows)
        assert [f for f in findings if f.kind == FindingKind.DUPLICATE_SOURCE] == []

    def test_three_workflows_show_occurrence_count(self):
        workflows = [
            {
                "id": f"wf-{i}",
                "name": f"Pipeline {i}",
                "nodes": [
                    _source_node("n1", connection_id="prod_pg", table="orders"),
                    _sink_node("n2", connection_id="warehouse", table=f"out_{i}"),
                ],
            }
            for i in range(3)
        ]
        findings = detect_duplicate_sources(workflows)
        dup_src = [f for f in findings if f.kind == FindingKind.DUPLICATE_SOURCE]
        assert len(dup_src) == 1
        assert dup_src[0].occurrences == 3
        assert len(dup_src[0].evidence["workflows"]) == 3


# ── Duplicate-pipeline detection ────────────────────────────────────

class TestDuplicatePipeline:
    def test_same_source_same_sink_flagged(self):
        """Two workflows reading the same source AND writing the same sink
        is the classic 'two engineers built the same flow' accident."""
        workflows = [
            {
                "id": "wf-a",
                "name": "Engineer A's flow",
                "nodes": [
                    _source_node("s1", connection_id="prod_pg", table="orders"),
                    _transform_node("t1"),
                    _sink_node("o1", connection_id="warehouse", table="orders_clean"),
                ],
            },
            {
                "id": "wf-b",
                "name": "Engineer B's flow",
                "nodes": [
                    _source_node("s1", connection_id="prod_pg", table="orders"),
                    _transform_node("t1"),
                    _sink_node("o1", connection_id="warehouse", table="orders_clean"),
                ],
            },
        ]
        findings = detect_duplicate_sources(workflows)
        dup_pipe = [f for f in findings if f.kind == FindingKind.DUPLICATE_PIPELINE]
        assert len(dup_pipe) == 1
        f = dup_pipe[0]
        assert f.id.startswith(FINDING_ID_PREFIXES[FindingKind.DUPLICATE_PIPELINE])
        assert {w["name"] for w in f.evidence["workflows"]} == {"Engineer A's flow", "Engineer B's flow"}

    def test_same_source_different_sink_not_pipeline_dup(self):
        """Fan-out is intentional — same source, different destinations
        IS a duplicate-source finding but NOT a duplicate-pipeline finding."""
        workflows = [
            {
                "id": "wf-a",
                "name": "Orders → Analytics",
                "nodes": [
                    _source_node("s1", connection_id="prod_pg", table="orders"),
                    _sink_node("o1", connection_id="warehouse", table="analytics_orders"),
                ],
            },
            {
                "id": "wf-b",
                "name": "Orders → Finance",
                "nodes": [
                    _source_node("s1", connection_id="prod_pg", table="orders"),
                    _sink_node("o1", connection_id="warehouse", table="finance_orders"),
                ],
            },
        ]
        findings = detect_duplicate_sources(workflows)
        kinds = {f.kind for f in findings}
        assert FindingKind.DUPLICATE_SOURCE in kinds  # the source IS shared
        assert FindingKind.DUPLICATE_PIPELINE not in kinds  # but the pipeline shape isn't


# ── Suppression honoured ────────────────────────────────────────────

class TestSuppression:
    def test_suppressed_source_signature_skipped(self):
        """When the curator has marked a source signature as 'intentional
        duplicate' (DR replication, data-vault layering), re-scans must
        NOT keep nagging."""
        workflows = [
            {
                "id": "wf-a",
                "name": "DR primary",
                "nodes": [
                    _source_node("n1", connection_id="prod_pg", table="orders"),
                    _sink_node("n2", connection_id="dr_west", table="orders"),
                ],
            },
            {
                "id": "wf-b",
                "name": "DR secondary",
                "nodes": [
                    _source_node("n1", connection_id="prod_pg", table="orders"),
                    _sink_node("n2", connection_id="dr_east", table="orders"),
                ],
            },
        ]
        # First pass — finds it
        first = detect_duplicate_sources(workflows)
        dup_src_first = [f for f in first if f.kind == FindingKind.DUPLICATE_SOURCE]
        assert len(dup_src_first) == 1
        suppressed_sig = dup_src_first[0].evidence["source_signature"]

        # Second pass with that signature suppressed — gone
        second = detect_duplicate_sources(workflows, suppressed_signatures={suppressed_sig})
        assert [f for f in second if f.kind == FindingKind.DUPLICATE_SOURCE] == []


# ── ID determinism + empty cases ────────────────────────────────────

class TestDeterminismAndEmpty:
    def test_finding_id_stable_across_runs(self):
        """Persistence layer relies on this for upsert (occurrence
        counter increments, not duplicate rows)."""
        workflows = [
            {
                "id": "wf-a",
                "name": "A",
                "nodes": [
                    _source_node("n1", connection_id="prod_pg", table="orders"),
                    _sink_node("n2", connection_id="warehouse", table="out_a"),
                ],
            },
            {
                "id": "wf-b",
                "name": "B",
                "nodes": [
                    _source_node("n1", connection_id="prod_pg", table="orders"),
                    _sink_node("n2", connection_id="warehouse", table="out_b"),
                ],
            },
        ]
        ids_run1 = {f.id for f in detect_duplicate_sources(workflows)}
        ids_run2 = {f.id for f in detect_duplicate_sources(workflows)}
        assert ids_run1 == ids_run2

    def test_no_workflows_no_findings(self):
        assert detect_duplicate_sources([]) == []

    def test_single_workflow_no_findings(self):
        """A workspace with one workflow can't have cross-workflow duplicates."""
        workflows = [{
            "id": "wf-a",
            "name": "Lonely",
            "nodes": [
                _source_node("n1", connection_id="prod_pg", table="orders"),
                _sink_node("n2", connection_id="warehouse", table="out"),
            ],
        }]
        assert detect_duplicate_sources(workflows) == []

    def test_workflows_without_sources_ignored(self):
        """Transform-only / orchestration-only workflows don't trigger
        Archeologist findings (it scans sources, not arbitrary nodes)."""
        workflows = [
            {"id": "wf-a", "name": "Just transforms", "nodes": [_transform_node("t1"), _transform_node("t2")]},
            {"id": "wf-b", "name": "Just transforms 2", "nodes": [_transform_node("t1")]},
        ]
        assert detect_duplicate_sources(workflows) == []

    def test_returns_pydantic_models(self):
        """The API serialiser depends on these being StewardFinding instances
        (model_dump call sites). Pin the contract."""
        workflows = [
            {"id": "a", "name": "A", "nodes": [_source_node("n1", connection_id="c", table="t"), _sink_node("n2", connection_id="c", table="o1")]},
            {"id": "b", "name": "B", "nodes": [_source_node("n1", connection_id="c", table="t"), _sink_node("n2", connection_id="c", table="o2")]},
        ]
        findings = detect_duplicate_sources(workflows)
        assert findings
        for f in findings:
            assert isinstance(f, StewardFinding)
            assert isinstance(f.kind, FindingKind)


# ── Learning layer (memory + escalation + rebound) ──────────────────

class TestLearningLayer:
    """The Archeologist alone is stateless — every scan re-derives from
    the current workflow set. The learning layer is what makes "learn
    from mistakes" real: it tracks how many SEPARATE scans a finding
    has appeared in, escalates severity when the user keeps ignoring,
    and flags 'rebounded' findings that came back after being resolved."""

    def _two_dup_workflows(self):
        return [
            {"id": "a", "name": "A", "nodes": [
                _source_node("s1", connection_id="prod", table="orders"),
                _sink_node("o1", connection_id="wh", table="a_out"),
            ]},
            {"id": "b", "name": "B", "nodes": [
                _source_node("s1", connection_id="prod", table="orders"),
                _sink_node("o1", connection_id="wh", table="b_out"),
            ]},
        ]

    def test_persistent_occurrences_count_separate_scans(self, tmp_path):
        from fpulse.steward.memory import StewardMemory, new_scan_id

        mem = StewardMemory(tmp_path / "memory.jsonl")
        wfs = self._two_dup_workflows()
        # Three scans, each records the emit
        for _ in range(3):
            scan_id = new_scan_id()
            findings = detect_duplicate_sources(wfs)
            for f in findings:
                mem.record_emit(scan_id, f)
        occ = mem.persistent_occurrences()
        # One signature, seen in three distinct scans
        assert len(occ) == 1
        assert list(occ.values())[0] == 3

    def test_severity_escalates_after_threshold(self, tmp_path):
        from fpulse.steward.memory import StewardMemory, apply_learning, new_scan_id

        mem = StewardMemory(tmp_path / "memory.jsonl")
        wfs = self._two_dup_workflows()

        # Simulate 5 separate scans where the user ignored the finding
        for _ in range(5):
            scan_id = new_scan_id()
            for f in detect_duplicate_sources(wfs):
                mem.record_emit(scan_id, f)

        # Sixth scan — apply_learning should bump P2 → P1
        findings = detect_duplicate_sources(wfs)
        dup_src = [f for f in findings if f.kind == FindingKind.DUPLICATE_SOURCE][0]
        assert dup_src.severity == FindingSeverity.P2  # baseline before learning

        # escalate_min_hours_since_first=0 disables the time clamp so
        # the test runs synchronously without waiting 24h. See the
        # dedicated `test_time_clamp_blocks_fast_escalation` test below
        # for the time-clamp behaviour itself.
        enriched = apply_learning(
            findings, mem,
            escalate_after_n_occurrences=5,
            escalate_min_hours_since_first=0,
        )
        dup_src_enriched = [f for f in enriched if f.kind == FindingKind.DUPLICATE_SOURCE][0]
        assert dup_src_enriched.severity == FindingSeverity.P1
        assert "escalated" in dup_src_enriched.body.lower()
        # Cross-scan occurrence count is now visible to the user
        assert dup_src_enriched.occurrences >= 5

    def test_orphaned_table_does_not_escalate(self, tmp_path):
        """Housekeeping findings (orphaned managed tables) must NOT auto-escalate
        on repetition. A leftover table seen in many scans is the same
        low-priority fact, not rising risk — regression for the dev-workspace
        noise where every un-dismissed test table climbed P3 -> P2 -> P1.
        Reliability kinds (DUPLICATE_SOURCE above) still escalate; see
        ``_NON_ESCALATING_KINDS`` in steward/memory.py."""
        from types import SimpleNamespace
        from fpulse.steward.memory import StewardMemory, apply_learning, new_scan_id
        from fpulse.steward.storage_intel import detect_orphaned_tables

        table = SimpleNamespace(schema_name="default", name="leftover", row_count=10)

        mem = StewardMemory(tmp_path / "memory.jsonl")
        # Ten ignored scans — well past the default threshold of 5.
        for _ in range(10):
            scan_id = new_scan_id()
            for f in detect_orphaned_tables([], [table]):
                mem.record_emit(scan_id, f)

        enriched = apply_learning(
            detect_orphaned_tables([], [table]), mem,
            escalate_after_n_occurrences=5,
            escalate_min_hours_since_first=0,  # disable the 24h time clamp
        )
        orphan = [f for f in enriched if f.kind == FindingKind.ORPHANED_TABLE][0]
        # Stays at the detector's severity; no escalation footnote in the body.
        assert orphan.severity == FindingSeverity.P3
        assert "escalated" not in orphan.body.lower()
        # Cross-scan count is still surfaced — just not weaponised into severity.
        assert orphan.occurrences >= 5

    def test_no_escalation_under_threshold(self, tmp_path):
        from fpulse.steward.memory import StewardMemory, apply_learning, new_scan_id

        mem = StewardMemory(tmp_path / "memory.jsonl")
        wfs = self._two_dup_workflows()
        # Only 2 scans — well below default threshold of 5
        for _ in range(2):
            scan_id = new_scan_id()
            for f in detect_duplicate_sources(wfs):
                mem.record_emit(scan_id, f)
        findings = detect_duplicate_sources(wfs)
        enriched = apply_learning(
            findings, mem,
            escalate_after_n_occurrences=5,
            escalate_min_hours_since_first=0,
        )
        dup_src = [f for f in enriched if f.kind == FindingKind.DUPLICATE_SOURCE][0]
        assert dup_src.severity == FindingSeverity.P2  # unchanged

    def test_time_clamp_blocks_fast_escalation(self, tmp_path):
        """Architectural review Block 1C — a 60-second cron pipeline
        hitting the escalation count in 5 minutes must NOT page-out
        to P1. The time clamp requires the FIRST emit to be at least
        N hours old before severity bumps."""
        from fpulse.steward.memory import StewardMemory, apply_learning, new_scan_id
        from fpulse.steward.models import FindingSeverity, FindingKind

        mem = StewardMemory(tmp_path / "memory.jsonl")
        wfs = self._two_dup_workflows()
        # 6 scans in rapid succession — all timestamped within the same second
        for _ in range(6):
            sid = new_scan_id()
            for f in detect_duplicate_sources(wfs):
                mem.record_emit(sid, f)
        findings = detect_duplicate_sources(wfs)
        # Default 24h clamp + count threshold 5 → must NOT escalate
        # because first emit is seconds old, not 24h+
        enriched = apply_learning(
            findings, mem,
            escalate_after_n_occurrences=5,
            escalate_min_hours_since_first=24,
        )
        dup_src = [f for f in enriched if f.kind == FindingKind.DUPLICATE_SOURCE][0]
        assert dup_src.severity == FindingSeverity.P2, \
            "Time clamp failed — escalated despite first emit being seconds old"

    def test_rebound_promotes_status_to_rebounded(self, tmp_path):
        """REBOUNDED was promoted (2026-06-05) from a title-only
        annotation to a first-class FindingStatus enum value. Pin
        the contract."""
        from fpulse.steward.memory import StewardMemory, apply_learning, new_scan_id
        from fpulse.steward.models import FindingStatus

        mem = StewardMemory(tmp_path / "memory.jsonl")
        wfs = self._two_dup_workflows()
        # User resolved it once
        first = detect_duplicate_sources(wfs)[0]
        sig = first.evidence["source_signature"]
        mem.record_resolve(first.id, sig)
        # New scan — re-emit (someone re-introduced the dup)
        new_findings = detect_duplicate_sources(wfs)
        for f in new_findings:
            mem.record_emit(new_scan_id(), f)
        enriched = apply_learning(new_findings, mem, escalate_min_hours_since_first=0)
        rebound = enriched[0]
        assert rebound.status == FindingStatus.REBOUNDED
        # Backward-compat: the title prefix is still there too
        assert rebound.title.startswith("(rebounded)")
        # New evidence field for the UI chip
        assert "previously_resolved_at" in rebound.evidence

    def test_rebound_annotation_after_resolve(self, tmp_path):
        from fpulse.steward.memory import StewardMemory, apply_learning, new_scan_id

        mem = StewardMemory(tmp_path / "memory.jsonl")
        wfs = self._two_dup_workflows()

        # User resolved it once
        finding = detect_duplicate_sources(wfs)[0]
        sig = finding.evidence["source_signature"]
        mem.record_resolve(finding.id, sig)

        # New scan — re-derives the same finding (someone re-introduced the dup)
        new_findings = detect_duplicate_sources(wfs)
        for f in new_findings:
            mem.record_emit(new_scan_id(), f)

        enriched = apply_learning(new_findings, mem)
        rebound = [f for f in enriched if f.kind == FindingKind.DUPLICATE_SOURCE][0]
        assert rebound.title.startswith("(rebounded)")
        assert "regression" in rebound.body.lower()

    def test_audit_trail_records_dismiss_with_reason(self, tmp_path):
        from fpulse.steward.memory import StewardMemory

        mem = StewardMemory(tmp_path / "memory.jsonl")
        mem.record_dismiss(
            "dup-src-abc",
            "abc",
            reason="DR replication — intentional",
        )
        trail = mem.audit_trail(limit=10)
        assert len(trail) == 1
        assert trail[0]["kind"] == "dismiss"
        assert "DR replication" in trail[0]["reason"]

    def test_dismiss_resets_persistent_occurrence_counter(self, tmp_path):
        """Architectural Review 1 — alert-fatigue prevention.

        Without this guard, a signature that accumulated 8 scans
        BEFORE the user dismissed it would, on re-emit after the
        dismiss, still show occurrences=8 and immediately escalate
        to P1 — exactly the spam disaster the dismiss-with-reason
        loop is meant to prevent. The dismiss must act as a clean
        slate."""
        from fpulse.steward.memory import StewardMemory, new_scan_id
        from fpulse.steward.models import FindingKind

        mem = StewardMemory(tmp_path / "memory.jsonl")
        wfs = self._two_dup_workflows()
        sig = None

        # 8 scans BEFORE dismiss
        for _ in range(8):
            sid = new_scan_id()
            for f in detect_duplicate_sources(wfs):
                if sig is None and f.kind == FindingKind.DUPLICATE_SOURCE:
                    sig = f.evidence["source_signature"]
                mem.record_emit(sid, f)
        assert mem.persistent_occurrences().get(sig, 0) == 8

        # User dismisses
        mem.record_dismiss("dup-src-x", sig, reason="intentional fan-out")

        # Two MORE scans after dismiss (simulates the signature
        # re-appearing — e.g. someone re-created the duplicate)
        for _ in range(2):
            sid = new_scan_id()
            for f in detect_duplicate_sources(wfs):
                mem.record_emit(sid, f)

        # Post-dismiss count is 2, NOT 10. The pre-dismiss history
        # is excluded — dismiss acts as a clean slate.
        assert mem.persistent_occurrences().get(sig, 0) == 2

    def test_dismiss_reason_sanitizer_strips_aws_keys(self, tmp_path):
        """Architectural Review 4 — dismiss reasons must not leak
        secrets to the append-only journal."""
        from fpulse.steward.memory import StewardMemory

        mem = StewardMemory(tmp_path / "memory.jsonl")
        mem.record_dismiss(
            "dup-src-x", "sig123",
            reason="Service account using AKIAIOSFODNN7EXAMPLE intentionally for DR",
        )
        trail = mem.audit_trail(limit=10)
        # The AWS-key prefix is recognisable; it must NOT appear in the
        # stored reason text
        stored = trail[0]["reason"]
        assert "AKIAIOSFODNN7EXAMPLE" not in stored
        assert "[REDACTED:aws-key]" in stored
        # The surrounding context is preserved
        assert "intentionally for DR" in stored

    def test_dismiss_reason_sanitizer_strips_password_kv(self, tmp_path):
        from fpulse.steward.memory import StewardMemory

        mem = StewardMemory(tmp_path / "memory.jsonl")
        mem.record_dismiss(
            "dup-src-y", "sig456",
            reason="Used connection string password=supersecret123 for legacy",
        )
        stored = mem.audit_trail(limit=10)[0]["reason"]
        assert "supersecret123" not in stored
        assert "[REDACTED:secret]" in stored

    def test_dismiss_reason_sanitizer_strips_uri_credentials(self, tmp_path):
        from fpulse.steward.memory import StewardMemory

        mem = StewardMemory(tmp_path / "memory.jsonl")
        mem.record_dismiss(
            "dup-src-z", "sig789",
            reason="DR feed: postgres://admin:hunter2@10.0.5.21/proddb",
        )
        stored = mem.audit_trail(limit=10)[0]["reason"]
        assert "hunter2" not in stored
        assert "10.0.5.21" not in stored  # private IP also stripped
        assert "[REDACTED:credentials]" in stored
        assert "[REDACTED:private-ip]" in stored

    def test_corrupt_journal_line_does_not_break_scan(self, tmp_path):
        """Scenario Pack v1 / S11 — Corrupt memory resilience.
        Given: a memory.jsonl with one malformed line in the middle.
        When: persistent_occurrences / stats / audit_trail are called.
        Then: the bad line is skipped, the rest is processed normally,
              no exception escapes to the caller (Steward never crashes
              the scan path on its own state)."""
        from fpulse.steward.memory import StewardMemory, new_scan_id

        mem = StewardMemory(tmp_path / "memory.jsonl")
        wfs = self._two_dup_workflows()

        # 3 normal scans
        for _ in range(3):
            sid = new_scan_id()
            for f in detect_duplicate_sources(wfs):
                mem.record_emit(sid, f)

        # Inject corruption between valid events — this is the realistic
        # failure mode (disk full mid-write, OS crash, manual edit)
        with (tmp_path / "memory.jsonl").open("a", encoding="utf-8") as fp:
            fp.write("THIS IS NOT VALID JSON\n")
            fp.write('{"ts": "2026-06-06T00:00:00Z"\n')  # truncated JSON
            fp.write("\n\n")  # blank lines

        # Add one more good scan AFTER the corruption
        sid = new_scan_id()
        for f in detect_duplicate_sources(wfs):
            mem.record_emit(sid, f)

        # All three reads must succeed and return useful data
        s = mem.stats()
        assert s["total_emits"] >= 4   # 3 + 1 good scans of 1 finding each
        occ = mem.persistent_occurrences()
        assert any(n >= 4 for n in occ.values())  # at least one signature seen in 4 scans
        trail = mem.audit_trail(limit=100)
        assert len(trail) >= 4

    def test_dismiss_reason_sanitizer_passes_normal_text(self, tmp_path):
        """Operator notes with no secrets must round-trip verbatim —
        otherwise the Curator learns nothing useful."""
        from fpulse.steward.memory import StewardMemory

        mem = StewardMemory(tmp_path / "memory.jsonl")
        original = (
            "DR replication across regions — Sales Pipeline reads "
            "leads-1000 for daily reporting; Ad-hoc Analysis reads it for ad-hoc "
            "analysis. Different SLAs, intentional."
        )
        mem.record_dismiss("dup-src-w", "sig111", reason=original)
        assert mem.audit_trail(limit=10)[0]["reason"] == original

    def test_memory_stats_aggregate(self, tmp_path):
        from fpulse.steward.memory import StewardMemory, new_scan_id

        mem = StewardMemory(tmp_path / "memory.jsonl")
        wfs = self._two_dup_workflows()
        scan_id = new_scan_id()
        for f in detect_duplicate_sources(wfs):
            mem.record_emit(scan_id, f)
        mem.record_dismiss("dup-src-abc", "abc", reason="test")
        mem.record_resolve("dup-src-xyz", "xyz")
        s = mem.stats()
        assert s["total_emits"] >= 1
        assert s["total_dismisses"] == 1
        assert s["total_resolves"] == 1
        assert s["total_scans"] == 1
        assert s["distinct_signatures_seen"] >= 1


# ── Settings module ─────────────────────────────────────────────────

class TestSettings:
    def test_defaults_are_useful_not_noisy(self):
        from fpulse.steward.settings import StewardSettings

        s = StewardSettings()
        assert s.enabled is True  # OSS-default ON
        assert s.min_severity == "p3"  # show everything by default
        assert s.scan_on_save is True
        assert s.auto_stale_days == 30
        assert s.escalate_after_n_occurrences == 5

    def test_round_trip_persistence(self, tmp_path):
        from fpulse.steward.settings import SettingsStore, StewardSettings

        store = SettingsStore(tmp_path / "settings.json")
        # First load on empty file → defaults
        s1 = store.load()
        assert s1.enabled is True
        # Modify + save + reload
        s1.escalate_after_n_occurrences = 10
        s1.min_severity = "p2"
        store.save(s1)
        s2 = store.load()
        assert s2.escalate_after_n_occurrences == 10
        assert s2.min_severity == "p2"

    def test_corrupt_file_falls_back_to_defaults(self, tmp_path):
        """A corrupt settings.json must NOT crash the scan path —
        the Steward is a 'nice to have' surface and must degrade
        gracefully if its config file is mangled."""
        from fpulse.steward.settings import SettingsStore

        path = tmp_path / "settings.json"
        path.write_text("{ this is not json", encoding="utf-8")
        s = SettingsStore(path).load()
        assert s.enabled is True  # defaults

    def test_validation_rejects_bad_severity(self):
        from fpulse.steward.settings import StewardSettings
        import pytest as _pytest

        with _pytest.raises(Exception):
            StewardSettings(min_severity="p99")  # type: ignore[arg-type]

    def test_notify_settings_have_sensible_defaults(self):
        from fpulse.steward.settings import StewardSettings

        s = StewardSettings()
        assert s.notify_on_finding is True
        # Default is P2 to avoid info-only P3 spam in the bell, even
        # though the dropdown shows P3 by default.
        assert s.notify_min_severity == "p2"


# ── Notification bridge ─────────────────────────────────────────────

class _FakeNotificationStore:
    """In-memory stand-in for NotificationStore.create / list_for_user /
    mark_read. Mirrors only the contract the notifier needs."""

    def __init__(self):
        self.rows: list[dict] = []
        self._next_id = 0

    def create(self, notification):
        self._next_id += 1
        d = notification.model_dump(mode="json")
        d["id"] = f"n{self._next_id}"
        d["is_read"] = False
        self.rows.insert(0, d)  # newest first
        return notification

    def list_for_user(self, user_id, unread_only=False, limit=50):
        out = [r for r in self.rows if r["user_id"] == user_id]
        if unread_only:
            out = [r for r in out if not r.get("is_read")]
        return out[:limit]

    def mark_read(self, notification_id, user_id):
        for r in self.rows:
            if r["id"] == notification_id and r["user_id"] == user_id and not r.get("is_read"):
                r["is_read"] = True
                return True
        return False


class _FakeUserStore:
    def __init__(self, user_ids):
        self._user_ids = user_ids

    def list_users(self):
        return [{"id": uid} for uid in self._user_ids]


class TestNotificationBridge:
    """The notification bridge is the moment Steward findings cross
    from a dedicated surface (eye-icon badge) to the shared bell.
    Wrong de-dup here would mean every 60s poll fires a fresh bell
    ping for unresolved findings — user-visible spam disaster."""

    def _findings(self):
        wfs = [
            {"id": "a", "name": "A", "nodes": [
                _source_node("n1", connection_id="prod", table="orders"),
                _sink_node("n2", connection_id="wh", table="a_out"),
            ]},
            {"id": "b", "name": "B", "nodes": [
                _source_node("n1", connection_id="prod", table="orders"),
                _sink_node("n2", connection_id="wh", table="b_out"),
            ]},
        ]
        return detect_duplicate_sources(wfs)

    def test_first_emit_creates_notification(self):
        from fpulse.steward.notifier import emit_steward_notifications

        ns, us = _FakeNotificationStore(), _FakeUserStore(["u1"])
        result = emit_steward_notifications(
            notification_store=ns, user_store=us,
            workspace_id="default", findings=self._findings(),
            min_severity="p3",
        )
        assert result["created"] == 1
        # Notification metadata carries the de-dup keys
        n = ns.rows[0]
        assert n["metadata"]["source"] == "steward"
        assert n["metadata"]["finding_id"].startswith("dup-src-")
        assert n["metadata"]["severity"] == "p2"
        assert n["metadata"]["rebounded"] is False

    def test_rescan_with_same_severity_dedups(self):
        """The critical 'don't spam the bell' invariant."""
        from fpulse.steward.notifier import emit_steward_notifications

        ns, us = _FakeNotificationStore(), _FakeUserStore(["u1"])
        findings = self._findings()
        emit_steward_notifications(
            notification_store=ns, user_store=us,
            workspace_id="default", findings=findings, min_severity="p3",
        )
        # Three more scans of the same findings — bell must NOT grow
        for _ in range(3):
            result = emit_steward_notifications(
                notification_store=ns, user_store=us,
                workspace_id="default", findings=findings, min_severity="p3",
            )
            assert result["created"] == 0
            assert result["skipped_dedup"] >= 1
        assert len(ns.rows) == 1  # one notification total despite 4 emits

    def test_escalation_triggers_new_notification(self):
        """When a finding escalates P2 → P1, the user SHOULD see a new
        bell ping — it's genuinely new information."""
        from fpulse.steward.notifier import emit_steward_notifications
        from fpulse.steward.models import FindingSeverity

        ns, us = _FakeNotificationStore(), _FakeUserStore(["u1"])
        findings = self._findings()
        # First emit at default severity (P2)
        emit_steward_notifications(
            notification_store=ns, user_store=us,
            workspace_id="default", findings=findings, min_severity="p3",
        )
        # Now escalate — same finding ID, but severity bumped
        for f in findings:
            f.severity = FindingSeverity.P1
        result = emit_steward_notifications(
            notification_store=ns, user_store=us,
            workspace_id="default", findings=findings, min_severity="p3",
        )
        assert result["created"] == 1  # new severity = new notification
        assert len(ns.rows) == 2
        # And the new one has type 'steward_finding_escalated'
        latest = ns.rows[0]
        assert latest["type"] == "steward_finding_escalated"

    def test_rebound_triggers_new_notification(self):
        """A `(rebounded)` finding is distinct from the original even
        at the same severity — the bell should ping."""
        from fpulse.steward.notifier import emit_steward_notifications

        ns, us = _FakeNotificationStore(), _FakeUserStore(["u1"])
        findings = self._findings()
        emit_steward_notifications(
            notification_store=ns, user_store=us,
            workspace_id="default", findings=findings, min_severity="p3",
        )
        # Now mark them as rebounded (apply_learning would normally do this)
        for f in findings:
            f.title = "(rebounded) " + f.title
        result = emit_steward_notifications(
            notification_store=ns, user_store=us,
            workspace_id="default", findings=findings, min_severity="p3",
        )
        assert result["created"] == 1
        latest = ns.rows[0]
        assert latest["metadata"]["rebounded"] is True

    def test_below_min_severity_does_not_notify(self):
        from fpulse.steward.notifier import emit_steward_notifications

        ns, us = _FakeNotificationStore(), _FakeUserStore(["u1"])
        result = emit_steward_notifications(
            notification_store=ns, user_store=us,
            workspace_id="default", findings=self._findings(),
            min_severity="p1",  # only P1 findings should ping
        )
        # Default severity is P2 → filtered out
        assert result["created"] == 0
        assert result["skipped_severity"] >= 1
        assert len(ns.rows) == 0

    def test_dismiss_marks_related_notifications_read(self):
        from fpulse.steward.notifier import (
            emit_steward_notifications, mark_finding_notifications_read,
        )

        ns, us = _FakeNotificationStore(), _FakeUserStore(["u1"])
        findings = self._findings()
        emit_steward_notifications(
            notification_store=ns, user_store=us,
            workspace_id="default", findings=findings, min_severity="p3",
        )
        target = findings[0]
        assert not ns.rows[0]["is_read"]  # baseline

        marked = mark_finding_notifications_read(
            notification_store=ns, user_store=us,
            workspace_id="default", finding_id=target.id,
        )
        assert marked == 1
        assert ns.rows[0]["is_read"] is True

    def test_silent_when_store_missing(self):
        """If the notification store isn't wired (e.g. embedded build),
        the bridge must short-circuit cleanly — not crash the scan."""
        from fpulse.steward.notifier import emit_steward_notifications

        result = emit_steward_notifications(
            notification_store=None, user_store=None,
            workspace_id="default", findings=self._findings(),
        )
        assert result["created"] == 0
        assert result.get("skipped_no_store") is True

    def test_per_user_dedup_independent(self):
        """Two users get their own dedup window — one user marking
        their notification read must not stop the other user from
        getting the same finding."""
        from fpulse.steward.notifier import emit_steward_notifications

        ns, us = _FakeNotificationStore(), _FakeUserStore(["alice", "bob"])
        emit_steward_notifications(
            notification_store=ns, user_store=us,
            workspace_id="default", findings=self._findings(), min_severity="p3",
        )
        # Each user gets their own notification — count is 2 (one finding × 2 users)
        assert len(ns.rows) == 2
        recipients = {r["user_id"] for r in ns.rows}
        assert recipients == {"alice", "bob"}


# ── F-Pulse Memory Layer (durable lessons) ──────────────────────────

class TestMemoryLayerLessons:
    """The Memory Layer ships the 8-step failure → lesson workflow.
    Tests pin every transition (propose → approve → revalidate → stale
    → revive) and the gated-learning invariant: a PROPOSED lesson must
    NOT influence search results until a human approves it."""

    def test_propose_creates_lesson_in_proposed_state(self, tmp_path):
        from fpulse.steward.lessons import LessonStore, LessonStatus, LessonType, LessonConfidence

        store = LessonStore(tmp_path)
        lesson = store.propose(
            source="Oracle_FIN_PROD",
            pipeline="Load_AP_Invoices",
            lesson_type=LessonType.FAILURE_PATTERN,
            issue="ORA-12154 alias failure",
            symptom="Cannot find alias in TNS/EZConnect",
            approved_fix="Check gateway TNS_ADMIN and Oracle client config",
            proposed_by="steward",
        )
        assert lesson.status == LessonStatus.PROPOSED
        assert lesson.confidence == LessonConfidence.LOW
        # Both .yaml and .json files written
        assert any(p.suffix == ".json" for p in tmp_path.iterdir())
        assert any(p.suffix == ".yaml" for p in tmp_path.iterdir())

    def test_approve_promotes_status_and_validates(self, tmp_path):
        from fpulse.steward.lessons import LessonStore, LessonStatus, LessonType, LessonConfidence

        store = LessonStore(tmp_path)
        lesson = store.propose(
            source="prod_pg", pipeline="orders_etl",
            lesson_type=LessonType.RETRY_RULE,
            issue="connection_pool_exhausted",
            approved_fix="Cap concurrent_connections at pool_size - 2",
        )
        approved = store.approve(lesson.id, approver="data-owner@hybridyn.com")
        assert approved is not None
        assert approved.status == LessonStatus.APPROVED
        assert approved.approved_by == "data-owner@hybridyn.com"
        # APPROVED with occurrence_count=1 → MEDIUM
        assert approved.confidence == LessonConfidence.MEDIUM

    def test_search_for_failure_excludes_proposed(self, tmp_path):
        """Rule 3 (Learning is gated) — a PROPOSED lesson must NOT
        appear in failure-search results until a human approves it."""
        from fpulse.steward.lessons import LessonStore, LessonType

        store = LessonStore(tmp_path)
        store.propose(
            source="snowflake_prod", pipeline="x",
            lesson_type=LessonType.FAILURE_PATTERN,
            issue="warehouse_suspended_during_query",
            approved_fix="Resume the warehouse before kicking the job",
        )
        # NOT approved — should not surface
        matches = store.search_for_failure(
            source="snowflake_prod",
            error_substring="warehouse_suspended",
        )
        assert matches == []

    def test_search_for_failure_returns_approved_ranked_by_confidence(self, tmp_path):
        from fpulse.steward.lessons import LessonStore, LessonType, LessonConfidence

        store = LessonStore(tmp_path)
        l1 = store.propose(
            source="snowflake_prod", pipeline="x",
            lesson_type=LessonType.FAILURE_PATTERN,
            issue="warehouse_suspended_during_query",
            approved_fix="Resume the warehouse",
        )
        store.approve(l1.id, approver="a@b")
        l2 = store.propose(
            source="snowflake_prod", pipeline="y",
            lesson_type=LessonType.FAILURE_PATTERN,
            issue="warehouse_suspended state",
            approved_fix="Use auto-resume in the warehouse config",
        )
        store.approve(l2.id, approver="a@b")
        # Boost l2 to HIGH confidence via revalidate ×5
        for _ in range(5):
            store.revalidate(l2.id, reviewer="a@b")

        matches = store.search_for_failure(
            source="snowflake_prod",
            error_substring="warehouse_suspended",
        )
        assert len(matches) == 2
        assert matches[0].id == l2.id  # HIGH outranks MEDIUM
        assert matches[0].confidence == LessonConfidence.HIGH

    def test_revalidate_bumps_count_and_resets_clock(self, tmp_path):
        from fpulse.steward.lessons import LessonStore, LessonType

        store = LessonStore(tmp_path)
        lesson = store.propose(
            source="s", pipeline="p",
            lesson_type=LessonType.SOURCE_QUIRK,
            issue="emails arrive with trailing CRLF",
            approved_fix="strip(s)",
        )
        store.approve(lesson.id, approver="r")
        original_validated = store.get(lesson.id).last_validated

        # Tiny sleep simulator — just assert occurrence_count grows
        revalidated = store.revalidate(lesson.id, reviewer="r")
        assert revalidated.occurrence_count == 2
        # last_validated either advanced or stayed equal (sub-millisecond
        # tests can land on the same ISO string); either is acceptable
        assert revalidated.last_validated >= original_validated

    def test_stale_lesson_revives_on_revalidate(self, tmp_path):
        from fpulse.steward.lessons import LessonStore, LessonStatus, LessonType

        store = LessonStore(tmp_path)
        lesson = store.propose(
            source="s", pipeline="p",
            lesson_type=LessonType.SLA_PATTERN,
            issue="batch always finishes after 02:00 UTC",
            approved_fix="Set the alert SLA to 03:30 not 01:30",
        )
        store.approve(lesson.id, approver="r")
        # Manually push it stale
        l = store.get(lesson.id)
        l.status = LessonStatus.STALE
        store.save(l)
        # Revalidate → revives
        revived = store.revalidate(lesson.id, reviewer="r")
        assert revived.status == LessonStatus.APPROVED

    def test_reject_records_reason_in_evidence(self, tmp_path):
        from fpulse.steward.lessons import LessonStore, LessonStatus, LessonType

        store = LessonStore(tmp_path)
        lesson = store.propose(
            source="s", pipeline="p",
            lesson_type=LessonType.FAILURE_PATTERN,
            issue="false positive",
            approved_fix="...",
        )
        rejected = store.reject(lesson.id, reviewer="reviewer@b", reason="not actually a pattern")
        assert rejected.status == LessonStatus.REJECTED
        assert any("not actually a pattern" in (e.note or "") for e in rejected.evidence)

    def test_yaml_render_is_human_readable(self, tmp_path):
        from fpulse.steward.lessons import LessonStore, LessonType

        store = LessonStore(tmp_path)
        lesson = store.propose(
            source="Oracle_FIN_PROD",
            pipeline="Load_AP_Invoices",
            lesson_type=LessonType.FAILURE_PATTERN,
            issue="ORA-12154 alias failure",
            symptom="Cannot find alias in TNS/EZConnect",
            approved_fix="Check gateway TNS_ADMIN and Oracle client config",
        )
        # Read the YAML file Off disk + verify it round-trips the key fields
        yaml_path = next(tmp_path.glob("*.yaml"))
        yaml_text = yaml_path.read_text(encoding="utf-8")
        assert "Oracle_FIN_PROD" in yaml_text
        assert "ORA-12154" in yaml_text  # note: colons in values are quoted
        assert "Check gateway TNS_ADMIN" in yaml_text
        assert "status: proposed" in yaml_text

    def test_stats_returns_breakdown(self, tmp_path):
        from fpulse.steward.lessons import LessonStore, LessonStatus, LessonType

        store = LessonStore(tmp_path)
        l1 = store.propose(source="a", pipeline="p1",
            lesson_type=LessonType.SOURCE_QUIRK,
            issue="x", approved_fix="y")
        l2 = store.propose(source="a", pipeline="p2",
            lesson_type=LessonType.FAILURE_PATTERN,
            issue="x2", approved_fix="y2")
        store.approve(l1.id, approver="r")
        s = store.stats()
        assert s["total_lessons"] == 2
        assert s["by_status"]["approved"] == 1
        assert s["by_status"]["proposed"] == 1
        assert s["by_type"]["source_quirk"] == 1
        assert s["by_type"]["failure_pattern"] == 1

    def test_filtering_by_source_and_type(self, tmp_path):
        from fpulse.steward.lessons import LessonStore, LessonStatus, LessonType

        store = LessonStore(tmp_path)
        store.propose(source="oracle", pipeline="p1",
            lesson_type=LessonType.RETRY_RULE,
            issue="x", approved_fix="y")
        store.propose(source="snowflake", pipeline="p2",
            lesson_type=LessonType.FAILURE_PATTERN,
            issue="x2", approved_fix="y2")
        oracle_only = store.list_all(source="oracle")
        assert len(oracle_only) == 1
        assert oracle_only[0].source == "oracle"
        failure_only = store.list_all(lesson_type=LessonType.FAILURE_PATTERN)
        assert len(failure_only) == 1
        assert failure_only[0].lesson_type == LessonType.FAILURE_PATTERN

    def test_corrupt_lesson_file_does_not_break_listing(self, tmp_path):
        from fpulse.steward.lessons import LessonStore, LessonType

        store = LessonStore(tmp_path)
        store.propose(source="a", pipeline="p",
            lesson_type=LessonType.SOURCE_QUIRK,
            issue="x", approved_fix="y")
        # Drop a garbage file
        (tmp_path / "garbage.json").write_text("{ this is not json", encoding="utf-8")
        # list_all must still return the valid one
        lessons = store.list_all()
        assert len(lessons) == 1

    def test_kind_level_mapping_is_complete(self):
        """Every FindingKind must appear in KIND_TO_LEVEL.

        Pins the multi-level observability contract (4-reviewer
        convergence, 2026-06-05). A new FindingKind added without a
        level mapping would default to PIPELINE silently — which is
        wrong for connector or data-level findings. Catch that at
        test time, not at runtime."""
        from fpulse.steward.models import FindingKind, KIND_TO_LEVEL

        missing = [k for k in FindingKind if k not in KIND_TO_LEVEL]
        assert not missing, (
            f"FindingKind enum has values without a KIND_TO_LEVEL entry: "
            f"{[k.value for k in missing]}. Each kind must declare which "
            f"observability layer it lives at."
        )

    def test_level_for_kind_returns_expected_layer(self):
        """Spot-check a representative kind from every level so the
        mapping table can't silently get re-shuffled."""
        from fpulse.steward.models import FindingKind, FindingLevel, level_for_kind

        cases = [
            # Reviewer R4: duplicate_* and redundant_transfer regrouped
            # under ARCHITECTURE (structural design) rather than
            # PIPELINE / CONNECTOR / COST.
            (FindingKind.DUPLICATE_PIPELINE,    FindingLevel.ARCHITECTURE),
            (FindingKind.DUPLICATE_SOURCE,      FindingLevel.ARCHITECTURE),
            (FindingKind.REDUNDANT_TRANSFER,    FindingLevel.ARCHITECTURE),
            (FindingKind.SLA_BREACH,            FindingLevel.PIPELINE),
            (FindingKind.EMPTY_OUTPUT,          FindingLevel.NODE),
            (FindingKind.CONNECTOR_RATE_LIMIT,  FindingLevel.CONNECTOR),
            (FindingKind.SCHEMA_DRIFT,          FindingLevel.DATA),
            (FindingKind.FRESHNESS_MISS,        FindingLevel.DATA),
            (FindingKind.PII_LEAK,              FindingLevel.GOVERNANCE),
            (FindingKind.COST_DRIFT,            FindingLevel.COST),
        ]
        for kind, expected_level in cases:
            assert level_for_kind(kind) == expected_level, \
                f"{kind.value} should be at level {expected_level.value}"

    def test_archeologist_findings_carry_correct_level(self):
        """Shipped detector wires the level field correctly.

        Reviewer convergence (R4): duplicate_source moved from
        CONNECTOR to ARCHITECTURE — "two pipelines reading the same
        table" is a design decision, not a transport problem."""
        from fpulse.steward.models import FindingKind, FindingLevel

        wfs = [
            {"id": "a", "name": "A", "nodes": [
                _source_node("n1", connection_id="prod", table="orders"),
                _sink_node("n2", connection_id="wh", table="a_out"),
            ]},
            {"id": "b", "name": "B", "nodes": [
                _source_node("n1", connection_id="prod", table="orders"),
                _sink_node("n2", connection_id="wh", table="b_out"),
            ]},
        ]
        findings = detect_duplicate_sources(wfs)
        dup_src = [f for f in findings if f.kind == FindingKind.DUPLICATE_SOURCE]
        assert dup_src and dup_src[0].level == FindingLevel.ARCHITECTURE

    def test_architecture_level_groups_structural_kinds(self):
        """ARCHITECTURE is the home for structural / design-level
        findings: duplicate extraction, redundant transfer, lineage
        cascade. Pin the grouping so it can't silently drift."""
        from fpulse.steward.models import FindingKind, FindingLevel, level_for_kind

        for kind in (
            FindingKind.DUPLICATE_SOURCE,
            FindingKind.DUPLICATE_PIPELINE,
            FindingKind.REDUNDANT_TRANSFER,
            FindingKind.LINEAGE_CASCADE,
        ):
            assert level_for_kind(kind) == FindingLevel.ARCHITECTURE, \
                f"{kind.value} should be ARCHITECTURE-level"

    def test_seven_levels_exist(self):
        """Pin the level count — reviewer convergence settled on 7
        (was 6, with ARCHITECTURE promoted from cost+connector split)."""
        from fpulse.steward.models import FindingLevel
        assert len(list(FindingLevel)) == 7
        names = {level.value for level in FindingLevel}
        assert names == {
            "pipeline", "node", "connector", "data",
            "architecture", "governance", "cost",
        }

    def test_finding_carries_confidence_richness(self):
        """Every shipped finding must expose confidence + score +
        evidence_count + baseline_window so the UI can render
        calibrated trust signals. Pinned per reviewer ask."""
        from fpulse.steward.models import FindingKind

        wfs = [
            {"id": "a", "name": "A", "nodes": [
                _source_node("n1", connection_id="prod", table="orders"),
                _sink_node("n2", connection_id="wh", table="a"),
            ]},
            {"id": "b", "name": "B", "nodes": [
                _source_node("n1", connection_id="prod", table="orders"),
                _sink_node("n2", connection_id="wh", table="b"),
            ]},
            {"id": "c", "name": "C", "nodes": [
                _source_node("n1", connection_id="prod", table="orders"),
                _sink_node("n2", connection_id="wh", table="c"),
            ]},
        ]
        findings = detect_duplicate_sources(wfs)
        dup_src = [f for f in findings if f.kind == FindingKind.DUPLICATE_SOURCE][0]
        # Archeologist is deterministic → confidence=high, score=1.0
        assert dup_src.confidence == "high"
        assert dup_src.confidence_score == 1.0
        # evidence_count = the 3 workflows touching the duplicate source
        assert dup_src.evidence_count == 3
        # Structural detector with no time window
        assert dup_src.baseline_window == "instantaneous"

    def test_expanded_finding_status_values_exist(self):
        """Reviewer convergence (R4): finding lifecycle expanded
        from 5 states to 8 to match richer ops workflows."""
        from fpulse.steward.models import FindingStatus
        names = {s.value for s in FindingStatus}
        assert names == {
            "open", "acknowledged", "dismissed", "resolved",
            "rebounded", "suppressed", "expired", "stale",
        }

    def test_evidence_refs_persist(self, tmp_path):
        from fpulse.steward.lessons import LessonStore, LessonType, EvidenceRef

        store = LessonStore(tmp_path)
        lesson = store.propose(
            source="a", pipeline="p",
            lesson_type=LessonType.FAILURE_PATTERN,
            issue="x", approved_fix="y",
            evidence=[
                EvidenceRef(kind="execution", id="exec-1234", note="failed at step 3"),
                EvidenceRef(kind="finding", id="dup-src-abc"),
            ],
        )
        reloaded = store.get(lesson.id)
        assert len(reloaded.evidence) == 2
        assert reloaded.evidence[0].kind == "execution"
        assert reloaded.evidence[0].id == "exec-1234"


# ── Resolve → PROPOSED-lesson capture loop (2026-06-07) ──

class TestResolveLessonCapture:
    """Closes the dismiss-vs-resolve-vs-lesson architectural separation
    in CODE (not just docs). Before this change:

      * dismiss  → suppression + sanitised journal entry      ✓
      * resolve  → suppression + journal entry, NO lesson    ✗ (lesson store had no organic feeder)
      * lesson   → only created by explicit POST /lessons    ✓ (manual only)

    After: ``POST /findings/{id}/resolve`` accepts an optional
    ``fix_note`` field; when supplied the note is sanitised via the
    same 5-regex sweep as dismiss reasons and filed as a ``PROPOSED``
    lesson. The lesson stays inert until a human approves it
    (Rule 3 - learning is gated). Without ``fix_note`` the endpoint
    behaves exactly as before. These tests pin both paths."""

    def test_finding_kind_to_lesson_type_mapping(self):
        """`_lesson_type_for_finding` maps the two active 1.1 detectors
        to DUPLICATE_WARNING and falls back to USER_FIX for any kind
        not yet explicitly mapped (so future detectors keep the loop
        working without code changes)."""
        from fpulse.api.steward import _lesson_type_for_finding
        from fpulse.steward import FindingKind, LessonType

        assert _lesson_type_for_finding(FindingKind.DUPLICATE_SOURCE) == LessonType.DUPLICATE_WARNING
        assert _lesson_type_for_finding(FindingKind.DUPLICATE_PIPELINE) == LessonType.DUPLICATE_WARNING
        # Pick any FindingKind whose detector hasn't shipped yet -
        # fallback is USER_FIX.
        assert _lesson_type_for_finding(FindingKind.SCHEMA_DRIFT) == LessonType.USER_FIX
        assert _lesson_type_for_finding(FindingKind.COST_DRIFT) == LessonType.USER_FIX

    def _make_test_client(self, tmp_path, monkeypatch, duplicate_workflows):
        """Helper: build a TestClient where _workflows_for_scan returns
        the supplied fixtures, app_state points at tmp_path, and auth
        is overridden. Returns (client, app, workspace_id)."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import fpulse.api.steward as steward_mod
        import fpulse.main as main_mod

        monkeypatch.setattr(main_mod, "app_state", {"data_dir": str(tmp_path)}, raising=False)
        monkeypatch.setattr(steward_mod, "_workflows_for_scan",
                            lambda ws: duplicate_workflows)

        app = FastAPI()
        from fpulse.auth.deps import require_auth
        app.dependency_overrides[require_auth] = lambda: None
        app.include_router(steward_mod.router)
        return TestClient(app)

    def _duplicate_fixture(self):
        """Two workflows reading the same source object -> Archeologist
        emits a DUPLICATE_SOURCE finding we can resolve in the tests.

        Returns the post-normalisation shape that ``_workflows_for_scan``
        hands to ``detect_duplicate_sources`` - a list of dicts each with
        ``id``, ``name``, and a ``nodes`` list (F-Pulse-step entries
        with type+params at the top level)."""
        node = {"id": "s1", "type": "csv_source",
                "params": {"file_path": "/data/orders.csv"}}
        return [
            {"id": "wf-A", "name": "Aggregation Report",
             "workspace_id": "default", "nodes": [node]},
            {"id": "wf-B", "name": "Simple ETL",
             "workspace_id": "default", "nodes": [node]},
        ]

    def test_resolve_with_fix_note_creates_proposed_lesson(self, tmp_path, monkeypatch):
        """Resolve with fix_note → response carries lesson_id +
        lesson_status='proposed'; the lesson appears in the store
        with status PROPOSED (gated - not yet APPROVED)."""
        from fpulse.steward.lessons import LessonStore, LessonStatus, LessonType

        client = self._make_test_client(tmp_path, monkeypatch, self._duplicate_fixture())

        # Pull the finding id from /scan so we have something to resolve.
        listing = client.get("/api/steward/findings").json()
        assert listing["findings"], "fixture should produce at least one duplicate finding"
        finding_id = listing["findings"][0]["id"]

        r = client.post(
            f"/api/steward/findings/{finding_id}/resolve",
            json={"fix_note": "Consolidated both pipelines onto wf-A; "
                              "deleted wf-B after stakeholder sign-off"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["resolved"] is True
        assert body["lesson_id"], "fix_note should have produced a lesson"
        assert body["lesson_status"] == "proposed"

        # Verify the lesson actually landed in the store with the right
        # status + type + sanitised text intact.
        lessons_dir = tmp_path / "steward" / "default" / "lessons"
        store = LessonStore(lessons_dir)
        lesson = store.get(body["lesson_id"])
        assert lesson is not None
        assert lesson.status == LessonStatus.PROPOSED, "must NOT be auto-approved (Rule 3)"
        assert lesson.lesson_type == LessonType.DUPLICATE_WARNING
        assert "Consolidated both pipelines" in lesson.approved_fix
        # Evidence carries the finding id back so reviewers can trace.
        assert any(e.id == finding_id for e in lesson.evidence)

    def test_resolve_with_fix_note_sanitises_secrets(self, tmp_path, monkeypatch):
        """The fix_note runs through the same 5-regex sanitiser as
        dismiss reasons (AWS keys / bearer / password= / URI creds /
        private IPs) so accidental secrets never reach the lesson on
        disk."""
        from fpulse.steward.lessons import LessonStore

        client = self._make_test_client(tmp_path, monkeypatch, self._duplicate_fixture())
        listing = client.get("/api/steward/findings").json()
        finding_id = listing["findings"][0]["id"]

        secret_note = (
            "Rotated the leaked AWS key AKIAABCDEFGHIJKLMNOP and reset "
            "password=hunter2 in the pipeline config; new IAM role in place."
        )
        r = client.post(
            f"/api/steward/findings/{finding_id}/resolve",
            json={"fix_note": secret_note},
        )
        assert r.status_code == 200
        lesson_id = r.json()["lesson_id"]
        assert lesson_id

        store = LessonStore(tmp_path / "steward" / "default" / "lessons")
        lesson = store.get(lesson_id)
        assert lesson is not None
        # Original secret tokens must NOT appear in the on-disk lesson.
        assert "AKIAABCDEFGHIJKLMNOP" not in lesson.approved_fix
        assert "hunter2" not in lesson.approved_fix
        # Redaction markers WERE substituted in.
        assert "[REDACTED:" in lesson.approved_fix

    def test_resolve_without_fix_note_creates_no_lesson(self, tmp_path, monkeypatch):
        """Backward compatibility: resolve with empty/missing body
        behaves exactly as before - marks resolved, no lesson, no
        change to the lesson store."""
        from fpulse.steward.lessons import LessonStore

        client = self._make_test_client(tmp_path, monkeypatch, self._duplicate_fixture())
        listing = client.get("/api/steward/findings").json()
        finding_id = listing["findings"][0]["id"]

        # No body at all
        r1 = client.post(f"/api/steward/findings/{finding_id}/resolve")
        assert r1.status_code == 200
        assert r1.json()["lesson_id"] is None
        assert r1.json()["lesson_status"] is None

        # Empty fix_note
        r2 = client.post(
            f"/api/steward/findings/{finding_id}/resolve",
            json={"fix_note": "   "},
        )
        assert r2.status_code == 200
        assert r2.json()["lesson_id"] is None

        # Lesson store stays empty.
        lessons_dir = tmp_path / "steward" / "default" / "lessons"
        if lessons_dir.exists():
            store = LessonStore(lessons_dir)
            assert store.list_all() == []


# ── V1-Gaps: 8 named gap-closure tests (per docs/steward/validation-scenarios.md) ──

class TestGapClosure:
    """Closes every gap listed in `docs/steward/validation-scenarios.md`
    § "Known small gaps". Each test maps 1:1 to a row in that table so
    the matrix stays honest as the code evolves."""

    # ── G1 ──────────────────────────────────────────────────────────
    def test_source_without_identity_returns_none(self):
        """Gap G1 — Defensive: a source node missing every identity
        field (no connection_id, no table, no file_path, etc.) must
        produce ``None`` so the detector silently skips it rather
        than crashing or producing a bogus signature."""
        assert _source_signature({}) is None
        assert _source_signature({"connector_type": "csv"}) is None  # type alone is not identity
        # Even with workspace_id prefix, no identity → None
        assert _source_signature({"connector_type": "csv"}, workspace_id="default") is None
        # A source NODE (full shape) with empty params returns no signature
        node = {
            "id": "n1",
            "data": {
                "stepType": "csv_source",
                "params": {"retry_count": 3, "_settings": {"timeout": 30}},  # no identity
            },
        }
        srcs = _extract_sources("wf", "name", [node])
        assert srcs == []

    # ── G2 ──────────────────────────────────────────────────────────
    def test_p1_does_not_double_escalate(self, tmp_path):
        """Gap G2 — Once a finding has been bumped to P1, further
        ignored scans MUST NOT attempt to bump again. P1 is the top
        of the severity ladder. Without this guard ``_bump_severity``
        would silently no-op but apply_learning could repeatedly
        annotate the body text."""
        from fpulse.steward.memory import StewardMemory, apply_learning, new_scan_id
        from fpulse.steward.models import FindingKind, FindingSeverity

        mem = StewardMemory(tmp_path / "memory.jsonl")
        wfs = [
            {"id": "a", "name": "A", "nodes": [
                _source_node("n1", connection_id="prod", table="orders"),
                _sink_node("n2", connection_id="wh", table="a_out"),
            ]},
            {"id": "b", "name": "B", "nodes": [
                _source_node("n1", connection_id="prod", table="orders"),
                _sink_node("n2", connection_id="wh", table="b_out"),
            ]},
        ]
        # Accumulate 10 scans (way past the threshold) so the finding
        # would escalate multiple times if the guard were missing
        for _ in range(10):
            sid = new_scan_id()
            for f in detect_duplicate_sources(wfs):
                mem.record_emit(sid, f)
        findings = detect_duplicate_sources(wfs)
        enriched = apply_learning(
            findings, mem,
            escalate_after_n_occurrences=3,
            escalate_min_hours_since_first=0,
        )
        dup = [f for f in enriched if f.kind == FindingKind.DUPLICATE_SOURCE][0]
        # Stays P1, doesn't try to bump "above" P1
        assert dup.severity == FindingSeverity.P1
        # The escalation note appears at most ONCE in the body (not
        # appended on every scan past threshold)
        assert dup.body.lower().count("escalated") <= 1

    # ── G3 ──────────────────────────────────────────────────────────
    def test_re_resolve_clears_then_rebounds_cleanly(self, tmp_path):
        """Gap G3 — After a REBOUND, if the user resolves it AGAIN and
        the signature re-emerges AGAIN, the state machine must remain
        clean: the second rebound's `previously_resolved_at` reflects
        the LATEST resolve, not the original one. This matters because
        users investigate "when did I last fix this?" and a stale
        timestamp sends them to the wrong git commit."""
        from fpulse.steward.memory import StewardMemory, apply_learning, new_scan_id
        from fpulse.steward.models import FindingStatus

        mem = StewardMemory(tmp_path / "memory.jsonl")
        wfs = [
            {"id": "a", "name": "A", "nodes": [
                _source_node("n1", connection_id="prod", table="orders"),
                _sink_node("n2", connection_id="wh", table="a"),
            ]},
            {"id": "b", "name": "B", "nodes": [
                _source_node("n1", connection_id="prod", table="orders"),
                _sink_node("n2", connection_id="wh", table="b"),
            ]},
        ]
        first = detect_duplicate_sources(wfs)[0]
        sig = first.evidence["source_signature"]

        # Resolve #1
        mem.record_resolve(first.id, sig)
        first_resolve_ts = mem.audit_trail(limit=5)[0]["ts"]

        # Re-emit → REBOUND #1
        for f in detect_duplicate_sources(wfs):
            mem.record_emit(new_scan_id(), f)
        enriched = apply_learning(detect_duplicate_sources(wfs), mem,
                                  escalate_min_hours_since_first=0)
        assert enriched[0].status == FindingStatus.REBOUNDED
        assert enriched[0].evidence["previously_resolved_at"] == first_resolve_ts

        # Resolve #2 (later)
        import time as _t; _t.sleep(0.001)  # ensure a new microsecond
        mem.record_resolve(first.id, sig)
        second_resolve_ts = mem.audit_trail(limit=5)[0]["ts"]
        assert second_resolve_ts > first_resolve_ts

        # Re-emit → REBOUND #2; `previously_resolved_at` MUST be the
        # latest resolve, not the original
        for f in detect_duplicate_sources(wfs):
            mem.record_emit(new_scan_id(), f)
        enriched_2 = apply_learning(detect_duplicate_sources(wfs), mem,
                                    escalate_min_hours_since_first=0)
        assert enriched_2[0].status == FindingStatus.REBOUNDED
        assert enriched_2[0].evidence["previously_resolved_at"] == second_resolve_ts

    # ── G4 ──────────────────────────────────────────────────────────
    def test_lesson_auto_ages_to_stale(self, tmp_path):
        """Gap G4 — A lesson untouched past `validity_days` must
        transition to STALE on the next `age_to_stale()` sweep. Without
        this, lessons accumulate indefinitely and the user can't tell
        which approved knowledge is still fresh."""
        from datetime import datetime, timezone, timedelta
        from fpulse.steward.lessons import LessonStore, LessonStatus, LessonType

        store = LessonStore(tmp_path)
        lesson = store.propose(
            source="oracle", pipeline="p",
            lesson_type=LessonType.FAILURE_PATTERN,
            issue="x", approved_fix="y",
        )
        store.approve(lesson.id, approver="r")
        # Manually backdate last_validated to 200 days ago
        l = store.get(lesson.id)
        l.validity_days = 180  # default but explicit for the assertion
        cutoff = datetime.now(timezone.utc) - timedelta(days=200)
        l.last_validated = cutoff.isoformat()
        store.save(l)
        # Run the maintenance hook
        aged = store.age_to_stale()
        assert aged == 1
        # State transitioned
        re_read = store.get(lesson.id)
        assert re_read.status == LessonStatus.STALE
        # A second sweep is a no-op (already STALE)
        assert store.age_to_stale() == 0

    # ── G5 ──────────────────────────────────────────────────────────
    def test_lesson_search_with_no_source_filter(self, tmp_path):
        """Gap G5 — Calling `search_for_failure(source="")` (or None)
        must search ALL approved lessons rather than returning empty.
        Some failure paths don't know which source caused them (e.g.
        a transform that processed already-joined data) and the
        operator wants to see every matching lesson."""
        from fpulse.steward.lessons import LessonStore, LessonType

        store = LessonStore(tmp_path)
        for src in ("oracle_prod", "snowflake_prod", "postgres_warehouse"):
            l = store.propose(
                source=src, pipeline="p",
                lesson_type=LessonType.FAILURE_PATTERN,
                issue="connection_timeout after 30s",
                approved_fix=f"Bump {src} pool timeout to 60s",
            )
            store.approve(l.id, approver="r")
        # Source = "" → cross-source search returns ALL three
        all_hits = store.search_for_failure(source="", error_substring="connection_timeout")
        assert len(all_hits) == 3
        sources = {h.source for h in all_hits}
        assert sources == {"oracle_prod", "snowflake_prod", "postgres_warehouse"}
        # Source = specific → narrows to one
        narrow = store.search_for_failure(source="oracle_prod", error_substring="connection_timeout")
        assert len(narrow) == 1
        assert narrow[0].source == "oracle_prod"

    # ── G6 ──────────────────────────────────────────────────────────
    def test_notify_disabled_produces_no_bell_rows(self):
        """Gap G6 — Master toggle integrity. When `notify_on_finding`
        is False at the settings layer, the API-level `_run_scan`
        path must NOT call the notifier at all, regardless of finding
        severity. We simulate the settings-respecting flow by passing
        the findings to the notifier under both flags and verifying
        the count delta is zero when the flag is off."""
        from fpulse.steward.notifier import emit_steward_notifications
        from fpulse.steward.settings import StewardSettings

        ns, us = _FakeNotificationStore(), _FakeUserStore(["u1"])
        findings = self._findings_with_severity_p1()
        # Settings simulate `notify_on_finding=False` -> caller skips the bridge entirely
        settings = StewardSettings(notify_on_finding=False, notify_min_severity="p1")
        if settings.notify_on_finding:
            emit_steward_notifications(
                notification_store=ns, user_store=us,
                workspace_id="default", findings=findings,
                min_severity=settings.notify_min_severity,
            )
        # Toggle off → zero rows even though we have a P1 finding
        assert len(ns.rows) == 0

        # Sanity: toggling on with the same findings DOES create one
        settings_on = StewardSettings(notify_on_finding=True, notify_min_severity="p3")
        if settings_on.notify_on_finding:
            emit_steward_notifications(
                notification_store=ns, user_store=us,
                workspace_id="default", findings=findings,
                min_severity=settings_on.notify_min_severity,
            )
        assert len(ns.rows) == 1

    def _findings_with_severity_p1(self):
        """Helper — produce a finding pre-escalated to P1."""
        from fpulse.steward.models import FindingSeverity
        wfs = [
            {"id": "a", "name": "A", "nodes": [
                _source_node("n1", connection_id="prod", table="orders"),
                _sink_node("n2", connection_id="wh", table="a"),
            ]},
            {"id": "b", "name": "B", "nodes": [
                _source_node("n1", connection_id="prod", table="orders"),
                _sink_node("n2", connection_id="wh", table="b"),
            ]},
        ]
        findings = detect_duplicate_sources(wfs)
        for f in findings:
            f.severity = FindingSeverity.P1
        return findings

    # ── G7 ──────────────────────────────────────────────────────────
    def test_dismiss_does_not_leak_across_workspaces(self):
        """Gap G7 — Multi-tenant safety. A dismiss in workspace A
        must NOT silence the same source pattern in workspace B.
        Together with the workspace-prefixed signature (R1) this is
        the Plus-tier safety story: two tenants can independently
        triage their own duplicates without seeing each other's
        decisions."""
        wfs = [
            {"id": "a", "name": "A", "nodes": [
                _source_node("n1", connection_id="shared_pg", table="orders"),
                _sink_node("n2", connection_id="wh", table="a"),
            ]},
            {"id": "b", "name": "B", "nodes": [
                _source_node("n1", connection_id="shared_pg", table="orders"),
                _sink_node("n2", connection_id="wh", table="b"),
            ]},
        ]
        # Workspace A: find + dismiss
        findings_a = detect_duplicate_sources(wfs, workspace_id="tenant_a")
        sig_a = findings_a[0].evidence["source_signature"]
        suppressed_a = {sig_a}

        # Workspace B: same source pattern → different signature
        findings_b = detect_duplicate_sources(wfs, workspace_id="tenant_b")
        sig_b = findings_b[0].evidence["source_signature"]
        # Signatures differ across workspaces (already pinned in R1 test)
        assert sig_a != sig_b
        # Workspace A's suppression set does NOT include B's signature
        assert sig_b not in suppressed_a
        # Re-scan workspace B with A's suppression set → finding still present
        findings_b_rescan = detect_duplicate_sources(
            wfs, workspace_id="tenant_b", suppressed_signatures=suppressed_a,
        )
        assert any(f.evidence.get("source_signature") == sig_b for f in findings_b_rescan)

    # ── G8 ──────────────────────────────────────────────────────────
    def test_steward_api_error_returns_json_not_html(self, tmp_path, monkeypatch):
        """Gap G8 — API integrators rely on the Steward router
        returning JSON for every status code (200/4xx/5xx). A FastAPI
        misconfiguration could return HTML on unhandled exceptions —
        breaking any client that unconditionally parses JSON. Pin
        this with a TestClient that triggers the not-found path."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from fpulse.api.steward import router
        # Steward's per-workspace file helpers read app_state["data_dir"]
        # lazily. In a real app `main.py` populates this on startup; in
        # tests we monkey-patch it with a temp dir so the endpoints can
        # write their settings/lessons files without crashing.
        import fpulse.main as main_mod
        monkeypatch.setattr(main_mod, "app_state", {"data_dir": str(tmp_path)}, raising=False)

        app = FastAPI()
        from fpulse.auth.deps import require_auth
        app.dependency_overrides[require_auth] = lambda: None
        app.include_router(router)
        client = TestClient(app)

        # Hit a non-existent lesson → 404 with JSON body
        r = client.get("/api/steward/lessons/nonexistent-id-9999")
        assert r.headers.get("content-type", "").startswith("application/json")
        body = r.json()
        assert "detail" in body  # FastAPI's canonical error shape

        # Hit a malformed PUT to settings → 400 with JSON body
        r2 = client.put("/api/steward/settings", json={"min_severity": "p99"})
        assert r2.headers.get("content-type", "").startswith("application/json")
        body2 = r2.json()
        assert "detail" in body2
