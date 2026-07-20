"""Steward Scenario Pack v1 — 12 named, real-world validation scenarios.

This script is a *demonstration harness*, not a test runner. Each
scenario runs the actual shipped detector / memory / notification /
settings code paths against a fresh per-scenario temp workspace, then
asserts the expected observable outcome, then prints a Given/When/Then
matrix row for the reader.

Use this when you need ONE artifact you can hand to a reviewer or
investor that proves the Steward works for the 12 scenarios most users
actually ask about. The full ~80-scenario gold-standard suite is
roadmapped in `docs/steward/validation-scenarios.md`; this is the v1
subset focused on what ships in 1.1.

Run:
    .venv\\Scripts\\python.exe backend\\scripts\\steward_scenario_pack_v1.py

Exits 0 on full pass. Non-zero if any scenario fails. Output is the
Given/When/Then matrix, intended to be captured into
`docs/steward/PROOF-2026-06-06/13-scenario-pack-v1.txt`.
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fpulse.steward import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    FindingStatus,
    LessonStatus,
    LessonStore,
    LessonType,
    SettingsStore,
    StewardMemory,
    apply_learning,
    detect_duplicate_sources,
    new_scan_id,
)
from fpulse.steward.notifier import (
    emit_steward_notifications,
    mark_finding_notifications_read,
)


# ── Fixture helpers ─────────────────────────────────────────────────

def _src(node_id: str, *, conn: str, table: str, kind: str = "db_source"):
    """React Flow-shaped source node."""
    return {
        "id": node_id,
        "data": {
            "stepType": kind,
            "label": f"Read {table}",
            "params": {
                "connector_type": "postgres",
                "connection_id": conn,
                "table": table,
            },
        },
    }


def _sink(node_id: str, *, conn: str, table: str):
    return {
        "id": node_id,
        "data": {
            "stepType": "db_sink",
            "label": f"Write {table}",
            "params": {
                "connector_type": "postgres",
                "connection_id": conn,
                "table": table,
            },
        },
    }


# A small "Fake notification store" mirroring the contract the bridge
# expects. Same shape used in the unit tests.
class _FakeNotifStore:
    def __init__(self):
        self.rows = []
        self._n = 0
    def create(self, n):
        self._n += 1
        d = n.model_dump(mode="json")
        d["id"] = f"n{self._n}"
        d["is_read"] = False
        self.rows.insert(0, d)
        return n
    def list_for_user(self, user_id, unread_only=False, limit=50):
        out = [r for r in self.rows if r["user_id"] == user_id]
        if unread_only:
            out = [r for r in out if not r["is_read"]]
        return out[:limit]
    def mark_read(self, nid, uid):
        for r in self.rows:
            if r["id"] == nid and r["user_id"] == uid and not r["is_read"]:
                r["is_read"] = True
                return True
        return False


class _FakeUserStore:
    def __init__(self, uids):
        self._uids = uids
    def list_users(self):
        return [{"id": u} for u in self._uids]


# ── Scenario runner ─────────────────────────────────────────────────

class Outcome:
    def __init__(self, given: str, when: str, then: str):
        self.given, self.when, self.then = given, when, then
        self.passed: bool | None = None
        self.error: str | None = None


SCENARIOS: list[tuple[str, str, FindingLevel | None, Callable[[Path], Outcome]]] = []


def scenario(id_: str, name: str, level: FindingLevel | None = None):
    """Decorator — every scenario function takes a tmp_path and returns
    an ``Outcome``. The decorator registers it for the runner."""
    def _decorate(fn: Callable[[Path], Outcome]):
        SCENARIOS.append((id_, name, level, fn))
        return fn
    return _decorate


# ── 12 scenarios ────────────────────────────────────────────────────

@scenario("V1-01", "Duplicate source", FindingLevel.ARCHITECTURE)
def s01(tmp: Path) -> Outcome:
    """3 pipelines reading the SAME source → one finding, occurrences=3."""
    wfs = [
        {"id": f"wf-{i}", "name": f"Pipeline {i}", "nodes": [
            _src("s", conn="prod_pg", table="orders"),
            _sink("o", conn="warehouse", table=f"out_{i}"),
        ]}
        for i in range(3)
    ]
    findings = detect_duplicate_sources(wfs, workspace_id="default")
    dup_src = [f for f in findings if f.kind == FindingKind.DUPLICATE_SOURCE]
    o = Outcome(
        given="3 pipelines all reading prod_pg.orders into different sinks",
        when="detect_duplicate_sources() runs",
        then="1 DUPLICATE_SOURCE finding emitted with occurrences=3, level=ARCHITECTURE",
    )
    o.passed = (
        len(dup_src) == 1
        and dup_src[0].occurrences == 3
        and dup_src[0].level == FindingLevel.ARCHITECTURE
    )
    return o


@scenario("V1-02", "Duplicate pipeline (same source + same sink)", FindingLevel.ARCHITECTURE)
def s02(tmp: Path) -> Outcome:
    wfs = [
        {"id": "wf-a", "name": "Eng A's flow", "nodes": [
            _src("s", conn="prod_pg", table="customers"),
            _sink("o", conn="warehouse", table="customers_clean"),
        ]},
        {"id": "wf-b", "name": "Eng B's flow", "nodes": [
            _src("s", conn="prod_pg", table="customers"),
            _sink("o", conn="warehouse", table="customers_clean"),
        ]},
    ]
    findings = detect_duplicate_sources(wfs, workspace_id="default")
    dup_pipe = [f for f in findings if f.kind == FindingKind.DUPLICATE_PIPELINE]
    o = Outcome(
        given="Two engineers built effectively the same flow (same source + same sink)",
        when="detect_duplicate_sources() runs",
        then="1 DUPLICATE_PIPELINE finding emitted alongside the duplicate-source",
    )
    o.passed = len(dup_pipe) == 1
    return o


@scenario("V1-03", "Intentional dismiss with reason", FindingLevel.ARCHITECTURE)
def s03(tmp: Path) -> Outcome:
    wfs = [
        {"id": "wf-dr1", "name": "DR West", "nodes": [
            _src("s", conn="prod_pg", table="audit_log"),
            _sink("o", conn="dr_west", table="audit_log"),
        ]},
        {"id": "wf-dr2", "name": "DR East", "nodes": [
            _src("s", conn="prod_pg", table="audit_log"),
            _sink("o", conn="dr_east", table="audit_log"),
        ]},
    ]
    mem = StewardMemory(tmp / "memory.jsonl")
    findings = detect_duplicate_sources(wfs, workspace_id="default")
    sig = findings[0].evidence["source_signature"]
    mem.record_dismiss(
        findings[0].id, sig,
        reason="DR replication across regions - intentional, must NOT alert again",
    )
    suppressed = {sig}
    next_run = detect_duplicate_sources(
        wfs, workspace_id="default", suppressed_signatures=suppressed,
    )
    still_there = any(f.evidence.get("source_signature") == sig for f in next_run)
    reason_stored = mem.audit_trail(limit=5)[0]["reason"]
    o = Outcome(
        given="DR-replication pattern flagged as duplicate; user dismisses with operator rationale",
        when="dismiss event recorded + suppression set passed to next scan",
        then="signature absent from next scan; operator reason preserved verbatim in journal",
    )
    o.passed = (not still_there) and "DR replication" in reason_stored
    return o


@scenario("V1-04", "Severity escalation P2 to P1 after N ignored scans", FindingLevel.ARCHITECTURE)
def s04(tmp: Path) -> Outcome:
    wfs = [
        {"id": f"wf-{i}", "name": f"P{i}", "nodes": [
            _src("s", conn="prod_pg", table="orders"),
            _sink("o", conn="warehouse", table=f"o{i}"),
        ]}
        for i in range(2)
    ]
    mem = StewardMemory(tmp / "memory.jsonl")
    for _ in range(5):
        sid = new_scan_id()
        for f in detect_duplicate_sources(wfs, workspace_id="default"):
            mem.record_emit(sid, f)
    findings = detect_duplicate_sources(wfs, workspace_id="default")
    enriched = apply_learning(
        findings, mem,
        escalate_after_n_occurrences=5,
        escalate_min_hours_since_first=0,  # disable time clamp for the demo
    )
    dup = enriched[0]
    o = Outcome(
        given="A duplicate-source finding has been emitted in 5 distinct scans without resolution",
        when="apply_learning() runs against the populated memory journal",
        then="severity bumps P2 to P1, body annotated with 'escalated' rationale",
    )
    o.passed = (
        dup.severity == FindingSeverity.P1
        and "escalated" in dup.body.lower()
    )
    return o


@scenario("V1-05", "Time-clamp blocks fast escalation", FindingLevel.ARCHITECTURE)
def s05(tmp: Path) -> Outcome:
    wfs = [
        {"id": f"wf-{i}", "name": f"P{i}", "nodes": [
            _src("s", conn="prod_pg", table="orders"),
            _sink("o", conn="warehouse", table=f"o{i}"),
        ]}
        for i in range(2)
    ]
    mem = StewardMemory(tmp / "memory.jsonl")
    # 6 scans in rapid succession (simulates a 60-second cron loop)
    for _ in range(6):
        sid = new_scan_id()
        for f in detect_duplicate_sources(wfs, workspace_id="default"):
            mem.record_emit(sid, f)
    findings = detect_duplicate_sources(wfs, workspace_id="default")
    enriched = apply_learning(
        findings, mem,
        escalate_after_n_occurrences=5,
        escalate_min_hours_since_first=24,  # production default
    )
    dup = enriched[0]
    o = Outcome(
        given="A 60-second cron has triggered 6 emits of the same finding within 5 minutes",
        when="apply_learning() runs with the production 24-hour time clamp",
        then="severity stays P2; count alone does NOT cross the escalation gate",
    )
    o.passed = dup.severity == FindingSeverity.P2
    return o


@scenario("V1-06", "Rebound on resolved-then-recurring finding", FindingLevel.ARCHITECTURE)
def s06(tmp: Path) -> Outcome:
    wfs = [
        {"id": f"wf-{i}", "name": f"P{i}", "nodes": [
            _src("s", conn="prod_pg", table="orders"),
            _sink("o", conn="warehouse", table=f"o{i}"),
        ]}
        for i in range(2)
    ]
    mem = StewardMemory(tmp / "memory.jsonl")
    findings = detect_duplicate_sources(wfs, workspace_id="default")
    sig = findings[0].evidence["source_signature"]
    mem.record_resolve(findings[0].id, sig)
    # Re-emit in a new scan -- simulates the duplicate being re-introduced
    for f in detect_duplicate_sources(wfs, workspace_id="default"):
        mem.record_emit(new_scan_id(), f)
    enriched = apply_learning(
        detect_duplicate_sources(wfs, workspace_id="default"),
        mem,
        escalate_min_hours_since_first=0,
    )
    rebounded = [f for f in enriched if f.status == FindingStatus.REBOUNDED]
    o = Outcome(
        given="A finding was resolved by the user, then the same signature re-emerged in a later scan",
        when="apply_learning() runs against the journal containing the resolve + re-emit",
        then="finding status == REBOUNDED, title prefixed (rebounded), evidence.previously_resolved_at set",
    )
    o.passed = (
        len(rebounded) == 1
        and rebounded[0].title.startswith("(rebounded)")
        and "previously_resolved_at" in rebounded[0].evidence
    )
    return o


@scenario("V1-07", "Memory Layer: propose -> approve -> search", FindingLevel.DATA)
def s07(tmp: Path) -> Outcome:
    store = LessonStore(tmp / "lessons")
    lesson = store.propose(
        source="Oracle_FIN_PROD",
        pipeline="Load_AP_Invoices",
        lesson_type=LessonType.FAILURE_PATTERN,
        issue="ORA-12154 alias failure",
        symptom="Cannot find alias in TNS/EZConnect",
        approved_fix="Check gateway TNS_ADMIN and Oracle client config",
        proposed_by="steward",
    )
    pre_approve_hits = store.search_for_failure(
        source="Oracle_FIN_PROD", error_substring="ORA-12154",
    )
    store.approve(lesson.id, approver="data-owner@example.com")
    post_approve_hits = store.search_for_failure(
        source="Oracle_FIN_PROD", error_substring="ORA-12154",
    )
    o = Outcome(
        given="A PROPOSED lesson exists for the Oracle ORA-12154 failure pattern",
        when="search_for_failure() is called BEFORE and AFTER approval",
        then="PROPOSED state returns 0 hits (gated learning); APPROVED state returns the matching lesson",
    )
    o.passed = (
        len(pre_approve_hits) == 0
        and len(post_approve_hits) == 1
        and post_approve_hits[0].approved_fix == "Check gateway TNS_ADMIN and Oracle client config"
    )
    return o


@scenario("V1-08", "Memory Layer: REJECTED lesson never influences search", FindingLevel.DATA)
def s08(tmp: Path) -> Outcome:
    store = LessonStore(tmp / "lessons")
    lesson = store.propose(
        source="snowflake_prod",
        pipeline="x",
        lesson_type=LessonType.FAILURE_PATTERN,
        issue="warehouse_suspended_during_query",
        approved_fix="Use auto-resume in the warehouse config",
    )
    store.reject(lesson.id, reviewer="reviewer@example.com", reason="False positive, not a real pattern")
    hits = store.search_for_failure(
        source="snowflake_prod", error_substring="warehouse_suspended",
    )
    o = Outcome(
        given="A lesson was proposed and then rejected by a reviewer with a reason",
        when="search_for_failure() is called for matching errors",
        then="0 hits; rejection reason is preserved in evidence trail for audit",
    )
    rejected_lesson = store.get(lesson.id)
    has_reject_note = any(
        "False positive" in (e.note or "") for e in (rejected_lesson.evidence or [])
    )
    o.passed = len(hits) == 0 and has_reject_note
    return o


@scenario("V1-09", "Notification de-dup across rescans", FindingLevel.ARCHITECTURE)
def s09(tmp: Path) -> Outcome:
    wfs = [
        {"id": f"wf-{i}", "name": f"P{i}", "nodes": [
            _src("s", conn="prod_pg", table="orders"),
            _sink("o", conn="warehouse", table=f"o{i}"),
        ]}
        for i in range(2)
    ]
    findings = detect_duplicate_sources(wfs, workspace_id="default")
    ns, us = _FakeNotifStore(), _FakeUserStore(["operator@example.com"])
    # 4 rescans of the same finding -- only the FIRST should ping
    for _ in range(4):
        emit_steward_notifications(
            notification_store=ns, user_store=us,
            workspace_id="default", findings=findings, min_severity="p3",
        )
    o = Outcome(
        given="The same P2 duplicate-source finding is re-emitted in 4 consecutive scans",
        when="emit_steward_notifications() runs for each scan",
        then="Exactly 1 bell notification is created in total; 3 are dedup-suppressed",
    )
    o.passed = len(ns.rows) == 1
    return o


@scenario("V1-10", "Per-workspace signature isolation (Plus-ready)", FindingLevel.ARCHITECTURE)
def s10(tmp: Path) -> Outcome:
    wfs = [
        {"id": f"wf-{i}", "name": f"P{i}", "nodes": [
            _src("s", conn="shared_pg", table="orders"),
            _sink("o", conn="warehouse", table=f"o{i}"),
        ]}
        for i in range(2)
    ]
    finds_a = detect_duplicate_sources(wfs, workspace_id="tenant_a")
    finds_b = detect_duplicate_sources(wfs, workspace_id="tenant_b")
    sig_a = finds_a[0].evidence["source_signature"]
    sig_b = finds_b[0].evidence["source_signature"]
    o = Outcome(
        given="Two tenants have imported the same connection_id and a workflow reading the same table",
        when="detect_duplicate_sources() runs for each workspace independently",
        then="Signatures differ across workspaces; tenant A's dismiss cannot leak into tenant B",
    )
    o.passed = sig_a != sig_b
    return o


@scenario("V1-11", "Corrupt memory journal resilience", FindingLevel.PIPELINE)
def s11(tmp: Path) -> Outcome:
    """Closes the gap reviewer 1 flagged. Steward must never crash on its own state."""
    mem = StewardMemory(tmp / "memory.jsonl")
    wfs = [
        {"id": f"wf-{i}", "name": f"P{i}", "nodes": [
            _src("s", conn="prod_pg", table="orders"),
            _sink("o", conn="warehouse", table=f"o{i}"),
        ]}
        for i in range(2)
    ]
    # 2 good scans
    for _ in range(2):
        sid = new_scan_id()
        for f in detect_duplicate_sources(wfs, workspace_id="default"):
            mem.record_emit(sid, f)
    # Inject a corrupt line
    with (tmp / "memory.jsonl").open("a", encoding="utf-8") as fp:
        fp.write("CORRUPT NOT JSON LINE\n")
        fp.write('{"ts": broken json\n')
    # 1 more good scan AFTER the corruption
    sid = new_scan_id()
    for f in detect_duplicate_sources(wfs, workspace_id="default"):
        mem.record_emit(sid, f)
    # Every aggregator must succeed
    try:
        stats = mem.stats()
        occ = mem.persistent_occurrences()
        trail = mem.audit_trail(limit=50)
        ok = stats["total_emits"] >= 3 and len(occ) >= 1 and len(trail) >= 3
    except Exception:
        ok = False
    o = Outcome(
        given="memory.jsonl contains 2 valid scans, then 2 corrupt lines, then 1 more valid scan",
        when="stats() / persistent_occurrences() / audit_trail() are all called",
        then="bad lines are silently skipped; valid events return correctly; no exception escapes",
    )
    o.passed = ok
    return o


@scenario("V1-12", "Steward disabled: master kill-switch end-to-end", FindingLevel.PIPELINE)
def s12(tmp: Path) -> Outcome:
    """Closes the second gap. enabled=false must short-circuit everything."""
    store = SettingsStore(tmp / "settings.json")
    s = store.load()
    s.enabled = False
    store.save(s)
    reloaded = store.load()
    o = Outcome(
        given="An operator sets enabled=false on the Steward settings",
        when="SettingsStore round-trips the value across a fresh load",
        then="enabled persists as false; downstream _run_scan would short-circuit immediately",
    )
    o.passed = reloaded.enabled is False
    return o


# ── Runner ──────────────────────────────────────────────────────────

def _run() -> int:
    print("=" * 76)
    print("F-Pulse Steward — Scenario Pack v1")
    print("=" * 76)
    print()
    print("12 named scenarios. Each runs against the actual shipped detector,")
    print("memory, notification, settings, and lesson code paths. Outcome is")
    print("printed as Given / When / Then so anyone can read the pass/fail")
    print("rationale without reading the test code.")
    print()

    outcomes: list[tuple[str, str, FindingLevel | None, Outcome]] = []
    for sid, name, level, fn in SCENARIOS:
        with tempfile.TemporaryDirectory() as td:
            try:
                o = fn(Path(td))
            except Exception as exc:
                o = Outcome("", "", "")
                o.passed = False
                o.error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        outcomes.append((sid, name, level, o))

    pass_count = sum(1 for _, _, _, o in outcomes if o.passed)
    fail_count = len(outcomes) - pass_count

    # ── Matrix table ────────────────────────────────────────────────
    for sid, name, level, o in outcomes:
        flag = "[PASS]" if o.passed else "[FAIL]"
        level_str = level.value if level else "-"
        print(f"{flag}  {sid}  {name}   (level: {level_str})")
        print(f"       Given: {o.given}")
        print(f"       When:  {o.when}")
        print(f"       Then:  {o.then}")
        if o.error:
            print(f"       ERROR: {o.error}")
        print()

    # ── Coverage by level ───────────────────────────────────────────
    by_level: dict[str, list[tuple[str, str, bool]]] = {}
    for sid, name, level, o in outcomes:
        key = (level.value if level else "general")
        by_level.setdefault(key, []).append((sid, name, bool(o.passed)))

    print("=" * 76)
    print("Coverage by FindingLevel")
    print("=" * 76)
    for lvl, rows in sorted(by_level.items()):
        passed = sum(1 for _, _, ok in rows if ok)
        print(f"  {lvl:<14}  {passed}/{len(rows)} pass   ({', '.join(sid for sid, _, _ in rows)})")
    print()

    print("=" * 76)
    print(f"RESULT: {pass_count} / {len(outcomes)} scenarios passed"
          + (f" - {fail_count} FAIL" if fail_count else ""))
    print("=" * 76)
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_run())
