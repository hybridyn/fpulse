"""Offline verification of a sequence of Execution Contract receipts.

The auditability half of "prove it": hand an auditor an ordered list of
:class:`~fpulse.contracts.schema.ContractVerification` receipts and confirm the
record is intact and untampered — with no access to the running system.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from pydantic import BaseModel

from .schema import ContractVerification, ExecutionContract
from .verify import _receipt_hash, _receipt_payload


class ChainResult(BaseModel):
    """Outcome of :func:`verify_chain`."""

    ok: bool
    count: int
    checked_hashes: bool = False  # True when contracts were supplied for full recompute
    broken_at: int | None = None  # index of the first bad receipt
    reason: str = ""


def verify_chain(
    receipts: Sequence[ContractVerification],
    *,
    contracts: Mapping[str, ExecutionContract] | None = None,
    genesis_prev: str | None = None,
) -> ChainResult:
    """Verify an ordered list of receipts.

    **Linkage (always):** ``receipts[0].prev_hash == genesis_prev`` and each
    ``receipts[i].prev_hash == receipts[i-1].receipt_hash`` — detects a removed,
    reordered, or inserted receipt.

    **Recompute (when ``contracts`` is given** — a map of ``contract_id ->
    ExecutionContract``): each ``receipt_hash`` is recomputed from its inputs
    and checked against the stored value — detects an in-place edit of a
    receipt's contents. Without the contracts, only linkage is checked (the same
    unsigned-chain honesty as audit export: reorder/insert/delete is caught,
    a silent in-place edit is not). F-Pulse+ Ed25519 signatures would close that
    gap on top of the same receipts.
    """
    n = len(receipts)
    do_recompute = contracts is not None
    prev = genesis_prev

    for i, r in enumerate(receipts):
        if r.prev_hash != prev:
            return ChainResult(
                ok=False, count=n, checked_hashes=do_recompute, broken_at=i,
                reason=f"broken link at index {i}: prev_hash {r.prev_hash!r} != expected {prev!r}",
            )
        if do_recompute:
            contract = contracts.get(r.contract_id)
            if contract is None:
                return ChainResult(
                    ok=False, count=n, checked_hashes=True, broken_at=i,
                    reason=f"no contract supplied for id {r.contract_id!r} at index {i}",
                )
            recomputed = _receipt_hash(
                _receipt_payload(contract, r.run_id, r.verdict, r.clauses,
                                 r.verified_at, r.prev_hash, r.resumed_from, r.provenance)
            )
            if recomputed != r.receipt_hash:
                return ChainResult(
                    ok=False, count=n, checked_hashes=True, broken_at=i,
                    reason=f"tampered receipt at index {i}: recomputed hash does not match stored",
                )
        prev = r.receipt_hash

    return ChainResult(ok=True, count=n, checked_hashes=do_recompute)
