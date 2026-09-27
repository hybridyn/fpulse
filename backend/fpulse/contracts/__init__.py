"""Execution Contracts — verify that a data operation met its declared promise.

Public API::

    from fpulse.contracts import (
        ExecutionContract, Guarantee, GuaranteeType, verify_contract,
    )

See :mod:`fpulse.contracts.schema` for the model and design invariants.
"""

from __future__ import annotations

from .schema import (
    ClauseResult,
    ContractVerification,
    ExecutionContract,
    Guarantee,
    GuaranteeType,
)
from .service import verify_run
from .verify import verify_contract

__all__ = [
    "ExecutionContract",
    "Guarantee",
    "GuaranteeType",
    "ClauseResult",
    "ContractVerification",
    "verify_contract",
    "verify_run",
]
