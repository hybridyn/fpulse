"""Tests for the Execution Contract (fpulse.contracts).

Covers each guarantee type (pass / fail / misconfigured / missing-evidence
fail-closed), the overall verdict, deterministic + chained receipts, the
rendered attestation, and the flagship Oracle-timesheet resume scenario.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fpulse.contracts import (
    ExecutionContract,
    Guarantee,
    GuaranteeType,
    verify_contract,
    verify_run,
)
from fpulse.ir.schema import StepRunResult, WorkflowRunResult

FIXED = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _step(step_id, status="success", row_count=0, columns=None):
    return StepRunResult(step_id=step_id, status=status, row_count=row_count, columns=columns or [])


def _run(status="success", steps=None, wf="wf1"):
    return WorkflowRunResult(workflow_id=wf, status=status, step_results=steps or {})


def _contract(*guarantees, default_output_step="sink"):
    return ExecutionContract(id="c1", name="t", default_output_step=default_output_step, guarantees=list(guarantees))


# ── individual guarantees ────────────────────────────────────────────────

def test_all_steps_succeeded_pass_and_fail():
    ok_run = _run(steps={"a": _step("a"), "sink": _step("sink", row_count=5)})
    v = verify_contract(_contract(Guarantee(type=GuaranteeType.ALL_STEPS_SUCCEEDED)), ok_run)
    assert v.verified

    bad_run = _run(status="error", steps={"a": _step("a", status="error"), "sink": _step("sink")})
    v2 = verify_contract(_contract(Guarantee(type=GuaranteeType.ALL_STEPS_SUCCEEDED)), bad_run)
    assert not v2.verified
    assert "a" in v2.clauses[0].detail


def test_row_count_variance():
    run = _run(steps={"sink": _step("sink", row_count=248_000)})
    g = Guarantee(type=GuaranteeType.ROW_COUNT_VARIANCE, expected_rows=250_000, variance_pct=0.05)
    assert verify_contract(_contract(g), run).verified  # within +/-5%

    run_low = _run(steps={"sink": _step("sink", row_count=100_000)})
    assert not verify_contract(_contract(g), run_low).verified


def test_row_count_variance_misconfigured_is_failed():
    run = _run(steps={"sink": _step("sink", row_count=10)})
    g = Guarantee(type=GuaranteeType.ROW_COUNT_VARIANCE, expected_rows=10)  # no variance_pct
    v = verify_contract(_contract(g), run)
    assert not v.verified
    assert "misconfigured" in v.clauses[0].detail


def test_no_empty_output():
    assert verify_contract(_contract(Guarantee(type=GuaranteeType.NO_EMPTY_OUTPUT)),
                           _run(steps={"sink": _step("sink", row_count=1)})).verified
    assert not verify_contract(_contract(Guarantee(type=GuaranteeType.NO_EMPTY_OUTPUT)),
                               _run(steps={"sink": _step("sink", row_count=0)})).verified


def test_row_count_min_max():
    run = _run(steps={"sink": _step("sink", row_count=50)})
    assert verify_contract(_contract(Guarantee(type=GuaranteeType.ROW_COUNT_MIN, min_rows=10)), run).verified
    assert not verify_contract(_contract(Guarantee(type=GuaranteeType.ROW_COUNT_MIN, min_rows=99)), run).verified
    assert verify_contract(_contract(Guarantee(type=GuaranteeType.ROW_COUNT_MAX, max_rows=99)), run).verified
    assert not verify_contract(_contract(Guarantee(type=GuaranteeType.ROW_COUNT_MAX, max_rows=10)), run).verified


def test_schema_contains():
    run = _run(steps={"sink": _step("sink", row_count=5, columns=["id", "name", "loaded_at"])})
    ok = Guarantee(type=GuaranteeType.SCHEMA_CONTAINS, columns=["id", "name"])
    assert verify_contract(_contract(ok), run).verified
    bad = Guarantee(type=GuaranteeType.SCHEMA_CONTAINS, columns=["id", "missing_col"])
    v = verify_contract(_contract(bad), run)
    assert not v.verified
    assert "missing_col" in v.clauses[0].detail


def test_no_duplicate_key_pass_fail_and_missing_is_fail_closed():
    run = _run(steps={"sink": _step("sink", row_count=5)})
    g = Guarantee(type=GuaranteeType.NO_DUPLICATE_KEY, key="TM_REC_ID")

    assert verify_contract(_contract(g), run, facts={"duplicate_key_counts": {"TM_REC_ID": 0}}).verified
    assert not verify_contract(_contract(g), run, facts={"duplicate_key_counts": {"TM_REC_ID": 3}}).verified

    # missing evidence -> fail-closed, not silently passed
    v = verify_contract(_contract(g), run, facts={})
    assert not v.verified
    assert "not provided" in v.clauses[0].detail


def test_checkpoint_and_destination_facts():
    run = _run(steps={"sink": _step("sink", row_count=5)})
    cp = Guarantee(type=GuaranteeType.CHECKPOINT_ENABLED)
    dv = Guarantee(type=GuaranteeType.DESTINATION_VERIFIED)
    assert verify_contract(_contract(cp, dv), run,
                           facts={"checkpoint_enabled": True, "destination_verified": True}).verified
    assert not verify_contract(_contract(cp), run, facts={"checkpoint_enabled": False}).verified
    assert not verify_contract(_contract(dv), run, facts={}).verified  # fail-closed


def test_freshness():
    run = _run(steps={"sink": _step("sink", row_count=5)})
    g = Guarantee(type=GuaranteeType.FRESHNESS_MAX_AGE, max_age_seconds=3600)
    assert verify_contract(_contract(g), run, facts={"source_age_seconds": 120}).verified
    assert not verify_contract(_contract(g), run, facts={"source_age_seconds": 7200}).verified


def test_empty_contract_is_failed():
    # A promise with nothing to prove is not a verified operation.
    v = verify_contract(_contract(), _run(steps={"sink": _step("sink", row_count=5)}))
    assert v.verdict == "failed"
    assert v.clauses == []


# ── receipt ──────────────────────────────────────────────────────────────

def test_receipt_hash_is_deterministic():
    run = _run(steps={"sink": _step("sink", row_count=5)})
    g = Guarantee(type=GuaranteeType.NO_EMPTY_OUTPUT)
    a = verify_contract(_contract(g), run, now=FIXED)
    b = verify_contract(_contract(g), run, now=FIXED)
    assert a.receipt_hash == b.receipt_hash
    assert len(a.receipt_hash) == 64


def test_receipt_hash_changes_with_verdict():
    g = Guarantee(type=GuaranteeType.NO_EMPTY_OUTPUT)
    passed = verify_contract(_contract(g), _run(steps={"sink": _step("sink", row_count=5)}), now=FIXED)
    failed = verify_contract(_contract(g), _run(steps={"sink": _step("sink", row_count=0)}), now=FIXED)
    assert passed.receipt_hash != failed.receipt_hash


def test_receipt_chaining():
    run = _run(steps={"sink": _step("sink", row_count=5)})
    g = Guarantee(type=GuaranteeType.NO_EMPTY_OUTPUT)
    first = verify_contract(_contract(g), run, now=FIXED)
    second = verify_contract(_contract(g), run, now=FIXED, prev_hash=first.receipt_hash)
    assert second.prev_hash == first.receipt_hash
    # linking the previous hash changes this receipt's hash (tamper-evident chain)
    assert second.receipt_hash != first.receipt_hash


def test_render_markdown_reports_each_clause():
    run = _run(steps={"sink": _step("sink", row_count=5, columns=["id"])})
    v = verify_contract(
        _contract(
            Guarantee(type=GuaranteeType.NO_EMPTY_OUTPUT, description="rows present"),
            Guarantee(type=GuaranteeType.SCHEMA_CONTAINS, columns=["id"], description="schema ok"),
        ),
        run, now=FIXED, resumed_from=59,
    )
    md = v.render_markdown()
    assert "VERIFIED" in md
    assert "rows present" in md and "schema ok" in md
    assert "resumed_from: 59" in md
    assert v.receipt_hash[:16] in md


# ── verify_run() ergonomic wrapper ───────────────────────────────────────

def test_verify_run_resumed_from_implies_checkpoint():
    run = _run(steps={"sink": _step("sink", row_count=5)})
    c = _contract(Guarantee(type=GuaranteeType.CHECKPOINT_ENABLED))
    # resumed_from set, checkpoint_enabled omitted -> treated as True
    v = verify_run(c, run, run_id="r2", resumed_from="r1")
    assert v.verified
    assert v.run_id == "r2" and v.resumed_from == "r1"


def test_verify_run_explicit_checkpoint_false_overrides():
    run = _run(steps={"sink": _step("sink", row_count=5)})
    c = _contract(Guarantee(type=GuaranteeType.CHECKPOINT_ENABLED))
    v = verify_run(c, run, resumed_from="r1", checkpoint_enabled=False)
    assert not v.verified


def test_verify_run_missing_checkpoint_is_fail_closed():
    run = _run(steps={"sink": _step("sink", row_count=5)})
    c = _contract(Guarantee(type=GuaranteeType.CHECKPOINT_ENABLED))
    v = verify_run(c, run)  # nothing implies checkpoint
    assert not v.verified


def test_verify_run_passes_through_facts():
    run = _run(steps={"sink": _step("sink", row_count=5)})
    c = _contract(
        Guarantee(type=GuaranteeType.NO_DUPLICATE_KEY, key="k"),
        Guarantee(type=GuaranteeType.DESTINATION_VERIFIED),
        Guarantee(type=GuaranteeType.FRESHNESS_MAX_AGE, max_age_seconds=3600),
    )
    v = verify_run(
        c, run,
        duplicate_key_counts={"k": 0},
        destination_verified=True,
        source_age_seconds=60,
    )
    assert v.verified


# ── flagship scenario ────────────────────────────────────────────────────

def test_oracle_timesheet_resume_scenario():
    """A 120-window backfill that failed at window 59, resumed, and is now
    verified: expected rows +/-5%, no duplicate TM_REC_ID, checkpoint on,
    destination confirmed, all steps green. This is the reference demo."""
    run = _run(steps={
        "oracle_source": _step("oracle_source", row_count=250_314, columns=["TM_REC_ID", "hours", "project"]),
        "transform": _step("transform", row_count=250_314, columns=["TM_REC_ID", "hours", "project", "loaded_at"]),
        "sink": _step("sink", row_count=250_314, columns=["TM_REC_ID", "hours", "project", "loaded_at"]),
    })
    contract = ExecutionContract(
        id="timesheet-monthly",
        name="Oracle timesheets -> SQL Server",
        input="Oracle / HR.timesheets",
        expected="~250,000 records",
        transformation="SQL transform v17",
        output="SQL Server / employee_timesheet",
        default_output_step="sink",
        guarantees=[
            Guarantee(type=GuaranteeType.ALL_STEPS_SUCCEEDED, description="all 120 windows succeeded"),
            Guarantee(type=GuaranteeType.ROW_COUNT_VARIANCE, expected_rows=250_000, variance_pct=0.05,
                      description="row count within 5% of expected"),
            Guarantee(type=GuaranteeType.NO_DUPLICATE_KEY, key="TM_REC_ID",
                      description="no duplicate TM_REC_ID (exactly-once on resume)"),
            Guarantee(type=GuaranteeType.SCHEMA_CONTAINS, columns=["TM_REC_ID", "loaded_at"],
                      description="destination schema compatible"),
            Guarantee(type=GuaranteeType.CHECKPOINT_ENABLED, description="checkpointed / resumable"),
            Guarantee(type=GuaranteeType.DESTINATION_VERIFIED, description="destination transaction committed"),
        ],
    )
    v = verify_contract(
        contract, run, run_id="run-8271",
        facts={
            "duplicate_key_counts": {"TM_REC_ID": 0},
            "checkpoint_enabled": True,
            "destination_verified": True,
        },
        resumed_from=59,
    )
    assert v.verified
    assert v.run_id == "run-8271"
    assert v.resumed_from == 59
    assert all(c.passed for c in v.clauses)

    # If the same resume had double-written on window 59, the contract catches it:
    v_bad = verify_contract(
        contract, run, run_id="run-8271",
        facts={"duplicate_key_counts": {"TM_REC_ID": 4200}, "checkpoint_enabled": True, "destination_verified": True},
        resumed_from=59,
    )
    assert not v_bad.verified
    dup_clause = next(c for c in v_bad.clauses if c.type == GuaranteeType.NO_DUPLICATE_KEY)
    assert not dup_clause.passed
