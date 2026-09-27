"""Ergonomic entry point that the run path (executor / backfill / API) calls.

``verify_run()`` assembles the out-of-band ``facts`` dict from values the
engine already knows at the end of a run — the resume source, whether the run
was checkpointed, the sink's commit confirmation, a duplicate-key probe, source
freshness — and returns the :class:`ContractVerification` receipt.

Kept deliberately thin and dependency-free (imports only the pure verifier and
the result model) so it can be called from anywhere in the run path without
pulling in heavy modules. Persistence and HTTP live elsewhere; this only
produces the receipt.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fpulse.ir.schema import WorkflowRunResult

from .schema import ContractVerification, ExecutionContract
from .verify import verify_contract


def verify_run(
    contract: ExecutionContract,
    run: WorkflowRunResult,
    *,
    run_id: str | None = None,
    resumed_from: str | int | None = None,
    checkpoint_enabled: bool | None = None,
    destination_verified: bool | None = None,
    duplicate_key_counts: dict[str, int] | None = None,
    source_age_seconds: float | None = None,
    prev_hash: str | None = None,
    now: datetime | None = None,
) -> ContractVerification:
    """Verify ``contract`` against a finished ``run`` and return the receipt.

    Fact derivation rules (all fail-closed downstream if still absent):

    * ``checkpoint_enabled`` — used as given; if omitted but ``resumed_from`` is
      set, the run demonstrably resumed, so it is treated as ``True``.
    * ``destination_verified`` / ``duplicate_key_counts`` / ``source_age_seconds``
      — passed straight through to the verifier when supplied.

    ``resumed_from`` (a prior run id, a window index, a unit number) is recorded
    on the receipt, tying the Execution Contract to durable/resumable runs.
    """
    facts: dict[str, Any] = {}

    if checkpoint_enabled is not None:
        facts["checkpoint_enabled"] = bool(checkpoint_enabled)
    elif resumed_from is not None:
        facts["checkpoint_enabled"] = True

    if destination_verified is not None:
        facts["destination_verified"] = bool(destination_verified)
    if duplicate_key_counts is not None:
        facts["duplicate_key_counts"] = duplicate_key_counts
    if source_age_seconds is not None:
        facts["source_age_seconds"] = source_age_seconds

    return verify_contract(
        contract, run,
        run_id=run_id, facts=facts, now=now,
        prev_hash=prev_hash, resumed_from=resumed_from,
    )
