"""Execution Contract — a declarative, verifiable promise about a data run.

Tier-1 flagship primitive (see ``docs/positioning-answer-engine.md``). It turns
"pipeline succeeded" into **"the data operation was verified"**: the author
declares the input, the expected shape and a set of *guarantees* BEFORE a run;
after the run, :func:`fpulse.contracts.verify.verify_contract` checks each
guarantee against the actual :class:`~fpulse.ir.schema.WorkflowRunResult` and
emits a tamper-evident :class:`ContractVerification` receipt.

Design invariants (deliberate):

* **Deterministic core.** Every guarantee is checked in code, never by an LLM.
  The AI layer may *propose* a contract; it never *decides* whether one passed.
* **Fail-closed.** A guarantee whose required evidence is missing is reported as
  ``passed=False`` with a clear reason — "not proven" is never silently "passed".
* **Receipt, not a log line.** The verdict is a canonical-hash receipt so it can
  be chained (``prev_hash``) and, in F-Pulse+, Ed25519-signed on top of the same
  shape (same OSS-detects-tamper / Plus-prevents-forgery split as audit export).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class GuaranteeType(str, Enum):
    """The deterministic checks an Execution Contract can assert."""

    ROW_COUNT_VARIANCE = "row_count_variance"   # output rows within expected ± variance_pct
    ROW_COUNT_MIN = "row_count_min"             # output rows >= min_rows
    ROW_COUNT_MAX = "row_count_max"             # output rows <= max_rows
    NO_EMPTY_OUTPUT = "no_empty_output"         # output rows > 0
    SCHEMA_CONTAINS = "schema_contains"         # output columns superset of `columns`
    NO_DUPLICATE_KEY = "no_duplicate_key"       # 0 duplicate values on `key` (from facts)
    ALL_STEPS_SUCCEEDED = "all_steps_succeeded" # workflow success + no errored step
    CHECKPOINT_ENABLED = "checkpoint_enabled"   # run was checkpointed/resumable (from facts)
    DESTINATION_VERIFIED = "destination_verified"  # destination commit confirmed (from facts)
    FRESHNESS_MAX_AGE = "freshness_max_age"     # source age <= max_age_seconds (from facts)


class Guarantee(BaseModel):
    """One clause of an Execution Contract.

    Only the fields relevant to ``type`` need to be set; the verifier reports a
    misconfigured clause as a (fail-closed) failure rather than raising.
    """

    type: GuaranteeType
    description: str | None = None
    # For row/schema checks: which step's output to inspect. Falls back to the
    # contract's ``default_output_step`` and then to the run's terminal step.
    output_step: str | None = None

    # row-count guarantees
    expected_rows: int | None = None
    variance_pct: float | None = None  # 0.05 == +/- 5%
    min_rows: int | None = None
    max_rows: int | None = None

    # schema guarantee
    columns: list[str] | None = None

    # evidence-backed guarantees (values come from the `facts` dict, not the
    # result object, because the engine computes them out-of-band)
    key: str | None = None            # no_duplicate_key
    max_age_seconds: int | None = None  # freshness_max_age


class ExecutionContract(BaseModel):
    """The declared promise for a data operation, authored before the run."""

    id: str
    name: str = ""
    input: str | None = None           # e.g. "Oracle / HR.timesheets"
    expected: str | None = None        # e.g. "~250,000 records"
    transformation: str | None = None  # e.g. "SQL transform v17"
    output: str | None = None          # e.g. "SQL Server / employee_timesheet"
    default_output_step: str | None = None
    guarantees: list[Guarantee] = Field(default_factory=list)


class ClauseResult(BaseModel):
    """The outcome of verifying one :class:`Guarantee`."""

    type: GuaranteeType
    passed: bool
    description: str | None = None
    detail: str = ""
    expected: Any | None = None
    observed: Any | None = None


class ContractVerification(BaseModel):
    """The receipt: a verified/failed verdict over all clauses of a contract."""

    contract_id: str
    run_id: str
    verdict: str  # "verified" | "failed"
    clauses: list[ClauseResult] = Field(default_factory=list)
    verified_at: str
    receipt_hash: str
    prev_hash: str | None = None
    # resume context (ties the Execution Contract to durable/resumable runs)
    resumed_from: str | int | None = None

    @property
    def verified(self) -> bool:
        return self.verdict == "verified"

    def render_markdown(self) -> str:
        """A human/enterprise-legible 'the data operation was verified' block."""
        icon = "✓" if self.verified else "✗"
        head = f"### Execution Contract {icon} {self.verdict.upper()}"
        lines = [head, "", f"- run: `{self.run_id}`  ", f"- verified_at: {self.verified_at}  "]
        if self.resumed_from is not None:
            lines.append(f"- resumed_from: {self.resumed_from}  ")
        lines.append("")
        for c in self.clauses:
            mark = "✓" if c.passed else "✗"
            label = c.description or c.type.value
            extra = f" ({c.detail})" if c.detail else ""
            lines.append(f"- {mark} {label}{extra}")
        lines.append("")
        lines.append(f"receipt: `{self.receipt_hash[:16]}...`")
        return "\n".join(lines)
