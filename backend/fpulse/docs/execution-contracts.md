# Execution Contracts

An **Execution Contract** turns *"the pipeline succeeded"* into **"the data operation was verified."** You declare what a run must be true for — expected volume, no duplicate key, schema, checkpointing, destination — *before* it runs; afterward, F-Pulse checks each guarantee against the actual run and emits a signed, hash-chained **receipt**.

Verification is **deterministic** (never an LLM) and **fail-closed**: a guarantee whose evidence is missing is reported as *not proven*, never silently passed.

> **Status:** the verification library and CLI ship today (`fpulse.contracts`). Automatic firing on every run + the `/api/execution-contracts` endpoint are being wired in; until then, verify from the CLI or in your own code.

## Author a contract

A contract declares the operation and a list of typed guarantees. As JSON (`examples/execution-contracts/timesheet-monthly.json`):

```json
{
  "id": "timesheet-monthly",
  "name": "Oracle timesheets -> SQL Server",
  "input": "Oracle / HR.timesheets",
  "expected": "~250,000 records",
  "output": "SQL Server / employee_timesheet",
  "default_output_step": "sink",
  "guarantees": [
    {"type": "all_steps_succeeded"},
    {"type": "row_count_variance", "expected_rows": 250000, "variance_pct": 0.05},
    {"type": "no_duplicate_key", "key": "TM_REC_ID"},
    {"type": "schema_contains", "columns": ["TM_REC_ID", "loaded_at"]},
    {"type": "checkpoint_enabled"},
    {"type": "destination_verified"}
  ]
}
```

### Guarantee types

| Type | Checks | Source |
|---|---|---|
| `all_steps_succeeded` | workflow succeeded, no errored step | run result |
| `row_count_variance` | output rows within `expected_rows` ± `variance_pct` | run result |
| `row_count_min` / `row_count_max` | output rows ≥ `min_rows` / ≤ `max_rows` | run result |
| `no_empty_output` | output rows > 0 | run result |
| `schema_contains` | output columns include `columns` | run result |
| `no_duplicate_key` | 0 duplicates on `key` | fact: `duplicate_key_counts` |
| `checkpoint_enabled` | the run was checkpointed / resumable | fact: `checkpoint_enabled` |
| `destination_verified` | the destination commit was confirmed | fact: `destination_verified` |
| `freshness_max_age` | source age ≤ `max_age_seconds` | fact: `source_age_seconds` |

"Source: run result" guarantees are read from the `WorkflowRunResult`. "Source: fact" guarantees come from evidence the engine computes out-of-band (a dedup probe, the run mode, the sink's commit) and are **fail-closed** if not supplied.

## Verify a run

```python
from fpulse.contracts import verify_run

verification = verify_run(
    contract, run,                       # ExecutionContract + WorkflowRunResult
    run_id="run-8271",
    resumed_from=59,                     # a run id / window / unit (records + implies checkpoint_enabled)
    duplicate_key_counts={"TM_REC_ID": 0},
    destination_verified=True,
)
print(verification.render_markdown())
```

```
### Execution Contract ✓ VERIFIED
- run: run-8271   - resumed_from: 59
- ✓ all steps succeeded
- ✓ row count within 5% of expected (250314 within [237500, 262500])
- ✓ no duplicate TM_REC_ID
- ✓ checkpointed / resumable
- ✓ destination transaction committed
receipt: 372c5d827d062c6c...
```

## The receipt

A `ContractVerification` carries the `verdict` (`verified` / `failed`), each clause's result, `verified_at`, `resumed_from`, and a `receipt_hash` — a SHA-256 over the canonical `(contract, run, verdict, clauses, verified_at, prev_hash, resumed_from)`. Pass `prev_hash` to link receipts into a **chain**. In OSS the chain is tamper-evident; F-Pulse+ signs each receipt with Ed25519 on the same shape.

## Audit a chain of receipts

Given an ordered list of receipts, confirm the record is intact — **offline, with no access to the running system**:

```python
from fpulse.contracts import verify_chain

result = verify_chain(receipts, contracts={c.id: c})
# result.ok, result.count, result.broken_at, result.reason
```

- **Linkage (always):** each `prev_hash` matches the previous `receipt_hash` → catches a reordered, inserted, or deleted receipt.
- **Recompute (with `contracts`):** each `receipt_hash` is recomputed and checked → catches an in-place edit of a receipt's contents. Without the contracts, linkage only (an unsigned chain, like audit export, catches structural tampering but not a silent content edit).

## CLI

```bash
# run the built-in Oracle-timesheet backfill-resume demo
python tools/verify_contract.py --demo

# verify a real run against a contract, write the receipt
python tools/verify_contract.py --contract c.json --run run.json --facts facts.json --out receipt.json

# audit a chain of receipts (add --contracts for full recompute)
python tools/verify_contract.py --verify-chain receipts.json --contracts contracts.json
```

Exit code is `0` when it verifies, `1` when it fails — so it drops straight into CI or a post-run gate.

## Where it's headed

Once the run-path wiring lands, a contract attached to a workflow fires `verify_run` automatically at the end of each run (and each backfill window), stores the receipt, and exposes it at `/api/execution-contracts`, with a per-unit "Data Run Graph" view. The library documented here is the deterministic core all of that builds on.
