"""Workflow governance fields — Plus-tier approval/deploy/PROD-toggle state.

PR 3 — IR Governance Split.

These fields used to live inline on ``Workflow`` (see schema.py history at
lines 255-294 prior to this split). They have no meaning in F-Pulse OSS:

  * OSS is a single-user tool with no approval workflow.
  * OSS has no PROD/DEV environment separation (see feedback_oss_no_prod_chrome).
  * OSS has no sandbox-then-promote pipeline lifecycle.

Keeping the fields inline was leaking Plus complexity into the OSS IR — every
new Workflow had 14 defaulted-to-empty governance fields that exposed Plus
semantics anyone reading the schema would have to mentally filter out.

Moving forward:

  * Plus reads/writes Workflow.governance directly.
  * OSS leaves Workflow.governance = None.
  * The inline fields on Workflow are marked deprecated and will be removed
    in a future major version; for the v1.x line they remain populated for
    back-compat with any code that hasn't migrated to the sub-model yet.

If you're adding a NEW governance-shaped field, add it here, not to Workflow.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class WorkflowGovernance(BaseModel):
    """Plus-tier governance state for a workflow.

    Empty / None on every OSS workflow. The presence of a non-None
    ``governance`` field is the signal that this workflow is being managed
    under the F-Pulse+ approval lifecycle.
    """

    # ── Single-gate approval (legacy, kept for back-compat) ────────────
    submitted_for_review: bool = False
    submitted_by: str | None = None
    submitted_at: datetime | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    # "" | "pending" | "approved" | "rejected"
    approval_status: str = ""
    approval_notes: str = ""

    # ── Two-gate approval flow (PR11) ──────────────────────────────────
    # State machine values:
    #   ""                            initial / draft
    #   "pending_sandbox_approval"    Gate 1 — awaiting approver decision
    #   "sandbox_ready"               Gate 1 approved; Prod admin can run
    #                                 the workflow in PROD-Sandbox
    #   "pending_deploy_approval"     Gate 2 — awaiting approver with
    #                                 sandbox evidence attached
    #   "active"                      Live in PROD
    #   "rejected"                    Rejected at either gate; back to
    #                                 draft on next edit
    approval_stage: str = ""

    # Gate 1 — sandbox approval audit trail.
    sandbox_approved_at: datetime | None = None
    sandbox_approved_by: str | None = None
    sandbox_approval_notes: str = ""

    # Gate 2 — deploy approval audit trail. The successful sandbox_runs.id
    # used as the evidence at Gate 2 is recorded here so the audit trail
    # can prove which run the approver inspected before approving.
    deploy_approved_at: datetime | None = None
    deploy_approved_by: str | None = None
    deploy_approval_notes: str = ""
    deploy_evidence_sandbox_run_id: str | None = None

    # ── Pipeline activate/deactivate flags (PR12) ──────────────────────
    # DEV : direct toggle (Free + Plus, owner can flip without approval).
    # PROD: toggle is approval-gated via lifecycle_toggle_requests table.
    # Default both True so post-deploy pipelines are immediately live.
    #
    # OSS note: OSS has no PROD env, so ``is_active_prod`` is meaningless
    # in OSS land. We keep it here for Plus; OSS code paths should never
    # read this field.
    is_active_dev: bool = True
    is_active_prod: bool = True


def empty_governance() -> WorkflowGovernance:
    """Helper for tests and Plus migration code — returns a fresh
    all-default governance sub-model so callers don't have to remember
    every field's empty value."""
    return WorkflowGovernance()


def is_empty_governance(g: WorkflowGovernance | None) -> bool:
    """Return True when the governance sub-model carries no information
    beyond the field defaults — i.e. the workflow is effectively
    governance-free. OSS workflows always answer True. Plus uses this to
    skip writing a JSON sub-tree when nothing has been promoted."""
    if g is None:
        return True
    defaults = WorkflowGovernance()
    return g.model_dump() == defaults.model_dump()
