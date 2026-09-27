#!/usr/bin/env python
"""verify_contract - verify an Execution Contract against a run result.

A runnable proof of the "the data operation was verified" flagship, independent
of the executor/API wiring: it uses ``fpulse.contracts`` directly, so you can
demonstrate a signed verification receipt today.

Usage:
    python tools/verify_contract.py --demo
    python tools/verify_contract.py --contract c.json --run run.json \
        [--facts facts.json] [--resumed-from 59] [--out receipt.json]

``--demo`` runs the Oracle-timesheet backfill-resume scenario (a 120-window job
that failed at window 59, resumed, and is now verified). Exit code is 0 when the
contract verifies, 1 when it fails.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Make the backend importable when run from a source checkout.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BACKEND = os.path.join(_ROOT, "backend")
if os.path.isdir(_BACKEND) and _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fpulse.contracts import (  # noqa: E402
    ContractVerification,
    ExecutionContract,
    verify_chain,
    verify_run,
)
from fpulse.ir.schema import StepRunResult, WorkflowRunResult  # noqa: E402


def _demo_contract() -> ExecutionContract:
    return ExecutionContract(
        id="timesheet-monthly",
        name="Oracle timesheets -> SQL Server",
        input="Oracle / HR.timesheets",
        expected="~250,000 records",
        transformation="SQL transform v17",
        output="SQL Server / employee_timesheet",
        default_output_step="sink",
        guarantees=[
            {"type": "all_steps_succeeded", "description": "all 120 windows succeeded"},
            {"type": "row_count_variance", "expected_rows": 250000, "variance_pct": 0.05,
             "description": "row count within 5% of expected"},
            {"type": "no_duplicate_key", "key": "TM_REC_ID",
             "description": "no duplicate TM_REC_ID (exactly-once on resume)"},
            {"type": "checkpoint_enabled", "description": "checkpointed / resumable"},
            {"type": "destination_verified", "description": "destination transaction committed"},
        ],
    )


def _demo_run(rows: int = 250_314) -> WorkflowRunResult:
    return WorkflowRunResult(
        workflow_id="wf-timesheet", status="success",
        step_results={
            "sink": StepRunResult(
                step_id="sink", status="success", row_count=rows,
                columns=["TM_REC_ID", "hours", "project", "loaded_at"],
            ),
        },
    )


def _run_from_json(d: dict) -> WorkflowRunResult:
    steps = {sid: StepRunResult(**s) for sid, s in (d.get("step_results") or {}).items()}
    return WorkflowRunResult(
        workflow_id=d.get("workflow_id", "wf"),
        status=d.get("status", "success"),
        step_results=steps,
    )


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _verify_chain_cmd(a) -> int:
    receipts = [ContractVerification(**d) for d in _load(a.verify_chain)]
    contracts = None
    if a.contracts:
        raw = _load(a.contracts)
        items = raw.values() if isinstance(raw, dict) else raw
        contracts = {}
        for d in items:
            c = ExecutionContract(**d)
            contracts[c.id] = c
    result = verify_chain(receipts, contracts=contracts)
    mode = "linkage + recompute" if result.checked_hashes else "linkage only"
    if result.ok:
        print(f"✓ CHAIN OK - {result.count} receipt(s), {mode}")
        return 0
    print(f"✗ CHAIN BROKEN at index {result.broken_at} ({mode}): {result.reason}")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verify an Execution Contract against a run result.")
    ap.add_argument("--demo", action="store_true", help="run the built-in Oracle-timesheet-resume scenario")
    ap.add_argument("--contract", help="path to a contract JSON")
    ap.add_argument("--run", help="path to a WorkflowRunResult JSON")
    ap.add_argument("--facts", help="path to a facts JSON (duplicate_key_counts / checkpoint_enabled / ...)")
    ap.add_argument("--resumed-from", help="what the run resumed from (a run id, window index, unit)")
    ap.add_argument("--out", help="write the full receipt JSON here")
    ap.add_argument("--verify-chain", help="path to a JSON list of receipts to verify as a chain")
    ap.add_argument("--contracts", help="path to contracts (list or {id: contract}) for full hash recompute")
    a = ap.parse_args(argv)

    if a.verify_chain:
        return _verify_chain_cmd(a)

    if a.demo:
        contract, run = _demo_contract(), _demo_run()
        facts = {"duplicate_key_counts": {"TM_REC_ID": 0}, "checkpoint_enabled": True, "destination_verified": True}
        resumed_from: object = 59
        run_id = "run-8271"
    else:
        if not (a.contract and a.run):
            ap.error("--contract and --run are required unless --demo is given")
        contract = ExecutionContract(**_load(a.contract))
        run = _run_from_json(_load(a.run))
        facts = _load(a.facts) if a.facts else {}
        resumed_from = a.resumed_from
        run_id = None

    verification = verify_run(
        contract, run,
        run_id=run_id,
        resumed_from=resumed_from,
        checkpoint_enabled=facts.get("checkpoint_enabled"),
        destination_verified=facts.get("destination_verified"),
        duplicate_key_counts=facts.get("duplicate_key_counts"),
        source_age_seconds=facts.get("source_age_seconds"),
    )

    print(verification.render_markdown())
    print("\nreceipt:", verification.receipt_hash)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(verification.model_dump(mode="json"), f, indent=2)
        print("wrote", a.out)
    return 0 if verification.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
