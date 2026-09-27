"""Tests for offline receipt-chain verification (fpulse.contracts.chain)."""

from __future__ import annotations

from datetime import datetime, timezone

from fpulse.contracts import (
    ExecutionContract,
    Guarantee,
    GuaranteeType,
    verify_chain,
    verify_contract,
)
from fpulse.ir.schema import StepRunResult, WorkflowRunResult


def _build_chain(length=3):
    contract = ExecutionContract(
        id="c", default_output_step="sink",
        guarantees=[Guarantee(type=GuaranteeType.NO_EMPTY_OUTPUT)],
    )
    run = WorkflowRunResult(
        workflow_id="wf", status="success",
        step_results={"sink": StepRunResult(step_id="sink", status="success", row_count=5)},
    )
    receipts = []
    prev = None
    for i in range(length):
        v = verify_contract(contract, run, run_id=f"run{i}",
                            now=datetime(2026, 1, 1, i, tzinfo=timezone.utc), prev_hash=prev)
        receipts.append(v)
        prev = v.receipt_hash
    return contract, receipts


def test_intact_chain_passes_linkage():
    _, receipts = _build_chain()
    r = verify_chain(receipts)
    assert r.ok and r.count == 3 and r.broken_at is None
    assert r.checked_hashes is False  # no contracts -> linkage only


def test_broken_link_detected():
    _, receipts = _build_chain()
    receipts[1].prev_hash = "deadbeef"
    r = verify_chain(receipts)
    assert not r.ok and r.broken_at == 1


def test_reorder_detected():
    _, receipts = _build_chain()
    receipts[1], receipts[2] = receipts[2], receipts[1]
    r = verify_chain(receipts)
    assert not r.ok and r.broken_at == 1


def test_genesis_prev_enforced():
    _, receipts = _build_chain()
    assert verify_chain(receipts, genesis_prev="x").broken_at == 0
    assert verify_chain(receipts, genesis_prev=None).ok


def test_recompute_passes_with_contracts():
    contract, receipts = _build_chain()
    r = verify_chain(receipts, contracts={"c": contract})
    assert r.ok and r.checked_hashes is True


def test_in_place_edit_detected_with_contracts():
    contract, receipts = _build_chain()
    # linkage still lines up (stored hashes unchanged) but the content no longer
    # hashes to the stored receipt_hash -> only recompute catches it.
    receipts[1].verdict = "failed"
    assert verify_chain(receipts).ok is True  # linkage-only misses it
    r = verify_chain(receipts, contracts={"c": contract})
    assert not r.ok and r.broken_at == 1 and "tampered" in r.reason


def test_missing_contract_in_recompute_mode():
    _, receipts = _build_chain()
    r = verify_chain(receipts, contracts={})
    assert not r.ok and r.broken_at == 0 and "no contract" in r.reason


def test_empty_chain_ok():
    assert verify_chain([]).ok
