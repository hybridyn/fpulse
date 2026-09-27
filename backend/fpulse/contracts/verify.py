"""Deterministic post-run verification of an :class:`ExecutionContract`.

``verify_contract(contract, run, facts=...)`` checks every guarantee against the
actual :class:`~fpulse.ir.schema.WorkflowRunResult` (plus an out-of-band
``facts`` dict for evidence the result object doesn't carry: duplicate counts,
checkpoint state, destination confirmation, source freshness) and returns a
hash-receipt :class:`ContractVerification`.

Everything here is pure and deterministic — no I/O, no LLM — so a receipt is
reproducible: same inputs (including ``now``) -> same ``receipt_hash``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from fpulse.ir.schema import StepRunResult, WorkflowRunResult

from .schema import (
    ClauseResult,
    ContractVerification,
    ExecutionContract,
    Guarantee,
    GuaranteeType,
)


def _terminal_step(run: WorkflowRunResult) -> StepRunResult | None:
    if not run.step_results:
        return None
    return list(run.step_results.values())[-1]


def _resolve_output(
    run: WorkflowRunResult, guarantee: Guarantee, contract: ExecutionContract
) -> StepRunResult | None:
    sid = guarantee.output_step or contract.default_output_step
    if sid and sid in run.step_results:
        return run.step_results[sid]
    return _terminal_step(run)


def _check(
    g: Guarantee, run: WorkflowRunResult, contract: ExecutionContract, facts: dict[str, Any]
) -> ClauseResult:
    t = g.type

    def result(passed: bool, detail: str, expected: Any = None, observed: Any = None) -> ClauseResult:
        return ClauseResult(
            type=t, passed=passed, description=g.description,
            detail=detail, expected=expected, observed=observed,
        )

    if t == GuaranteeType.ALL_STEPS_SUCCEEDED:
        failed = [s.step_id for s in run.step_results.values() if s.status == "error"]
        ok = run.status == "success" and not failed
        return result(
            ok,
            "all steps succeeded" if ok else f"failed: {failed or run.status}",
            "success", run.status,
        )

    # Evidence-backed guarantees (values from `facts`, fail-closed if absent).
    if t == GuaranteeType.NO_DUPLICATE_KEY:
        counts = (facts.get("duplicate_key_counts") or {}) if facts else {}
        dup = counts.get(g.key) if g.key else facts.get("duplicate_count") if facts else None
        if dup is None:
            return result(False, f"required fact 'duplicate_key_counts[{g.key}]' not provided")
        ok = int(dup) == 0
        return result(ok, "no duplicate keys" if ok else f"{dup} duplicate(s) on '{g.key}'", 0, int(dup))

    if t == GuaranteeType.CHECKPOINT_ENABLED:
        val = facts.get("checkpoint_enabled") if facts else None
        if val is None:
            return result(False, "required fact 'checkpoint_enabled' not provided")
        return result(bool(val), "checkpointed / resumable" if val else "run was not checkpointed", True, bool(val))

    if t == GuaranteeType.DESTINATION_VERIFIED:
        val = facts.get("destination_verified") if facts else None
        if val is None:
            return result(False, "required fact 'destination_verified' not provided")
        return result(bool(val), "destination commit confirmed" if val else "destination not confirmed", True, bool(val))

    if t == GuaranteeType.FRESHNESS_MAX_AGE:
        if g.max_age_seconds is None:
            return result(False, "guarantee misconfigured (max_age_seconds)")
        age = facts.get("source_age_seconds") if facts else None
        if age is None:
            return result(False, "required fact 'source_age_seconds' not provided")
        ok = float(age) <= g.max_age_seconds
        return result(ok, f"source age {age}s <= {g.max_age_seconds}s" if ok else f"stale: {age}s > {g.max_age_seconds}s", g.max_age_seconds, age)

    # Output-shaped guarantees.
    out = _resolve_output(run, g, contract)
    if out is None:
        return result(False, "no output step available to measure")

    if t == GuaranteeType.NO_EMPTY_OUTPUT:
        ok = out.row_count > 0
        return result(ok, f"{out.row_count} rows" if ok else "output is empty", ">0", out.row_count)

    if t == GuaranteeType.ROW_COUNT_MIN:
        if g.min_rows is None:
            return result(False, "guarantee misconfigured (min_rows)")
        ok = out.row_count >= g.min_rows
        return result(ok, f"{out.row_count} >= {g.min_rows}" if ok else f"{out.row_count} < {g.min_rows}", g.min_rows, out.row_count)

    if t == GuaranteeType.ROW_COUNT_MAX:
        if g.max_rows is None:
            return result(False, "guarantee misconfigured (max_rows)")
        ok = out.row_count <= g.max_rows
        return result(ok, f"{out.row_count} <= {g.max_rows}" if ok else f"{out.row_count} > {g.max_rows}", g.max_rows, out.row_count)

    if t == GuaranteeType.ROW_COUNT_VARIANCE:
        if g.expected_rows is None or g.variance_pct is None:
            return result(False, "guarantee misconfigured (expected_rows/variance_pct)")
        lo = g.expected_rows * (1 - g.variance_pct)
        hi = g.expected_rows * (1 + g.variance_pct)
        ok = lo <= out.row_count <= hi
        band = f"{g.expected_rows} +/-{g.variance_pct * 100:.0f}%"
        return result(
            ok,
            f"{out.row_count} within [{lo:.0f}, {hi:.0f}]" if ok else f"{out.row_count} outside [{lo:.0f}, {hi:.0f}]",
            band, out.row_count,
        )

    if t == GuaranteeType.SCHEMA_CONTAINS:
        if not g.columns:
            return result(False, "guarantee misconfigured (columns)")
        have = set(out.columns or [])
        missing = [c for c in g.columns if c not in have]
        ok = not missing
        return result(ok, "schema compatible" if ok else f"missing columns: {missing}", g.columns, sorted(have))

    return result(False, f"unknown guarantee type: {t}")


def _receipt_hash(payload: dict[str, Any]) -> str:
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def verify_contract(
    contract: ExecutionContract,
    run: WorkflowRunResult,
    *,
    run_id: str | None = None,
    facts: dict[str, Any] | None = None,
    now: datetime | None = None,
    prev_hash: str | None = None,
    resumed_from: str | int | None = None,
) -> ContractVerification:
    """Verify ``contract`` against ``run`` and return a hash-receipt verdict.

    ``facts`` supplies evidence not present on the result object
    (``duplicate_key_counts``, ``checkpoint_enabled``, ``destination_verified``,
    ``source_age_seconds``). ``now`` is injectable for deterministic receipts.
    An empty contract (no guarantees) is ``failed`` — a promise with nothing to
    prove is not a verified operation.
    """
    facts = facts or {}
    clauses = [_check(g, run, contract, facts) for g in contract.guarantees]
    verdict = "verified" if clauses and all(c.passed for c in clauses) else "failed"
    ts = (now or datetime.now(timezone.utc)).isoformat()
    rid = run_id or run.workflow_id

    payload = {
        "contract": contract.model_dump(mode="json"),
        "run_id": rid,
        "verdict": verdict,
        "clauses": [c.model_dump(mode="json") for c in clauses],
        "verified_at": ts,
        "prev_hash": prev_hash,
        "resumed_from": resumed_from,
    }
    receipt = _receipt_hash(payload)

    return ContractVerification(
        contract_id=contract.id,
        run_id=rid,
        verdict=verdict,
        clauses=clauses,
        verified_at=ts,
        receipt_hash=receipt,
        prev_hash=prev_hash,
        resumed_from=resumed_from,
    )
