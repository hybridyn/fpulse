"""Source inference + sink cast policies.

Two knobs the operator turns from the node config to control how
strict the runtime is about type-level decisions:

  - ``InferencePolicy`` (source side) — how aggressively to resolve
    types when the source doesn't advertise a schema (JSON / CSV /
    REST). Choices:

        AUTO     sample-based inference. Default. Unresolvable
                 columns fall back to ``FPType.UNKNOWN`` so the
                 sink-side gate can decide what to do.
        STRICT   fail at planning time if any column resolves to
                 ``FPType.UNKNOWN``. Used when the operator wants
                 an absolute contract.
        COERCE   force unresolvable columns to ``FPType.STRING``
                 instead of UNKNOWN. Pipelines keep flowing.
        MANUAL   require an operator-declared schema; reject any
                 INFERRED evidence. Strongest contract.
        LEARN    accumulate samples across runs to refine the
                 inferred type. Snapshots merge into the locked
                 schema over the configured window.

  - ``CastPolicy`` (sink side) — how to handle source→target
    mismatches the cast classifier flagged.

        SAFE     fail if any cast is not SAFE. Default for tier-1
                 governance.
        COERCE   permit SEMANTIC_LOSSY (e.g., timestamp→date)
                 with a warning; fail on LOSSY / IMPOSSIBLE.
        TRUNCATE permit LOSSY (e.g., varchar(500)→varchar(255));
                 fail only on IMPOSSIBLE. The "I know what I'm
                 doing" mode.
        STRICT   fail on anything not identity (kind + params
                 exactly equal).
        LEARN    record the mismatches but proceed using the
                 target schema. Used during initial pipeline
                 development.

The ``gate_cast_plan`` helper applies a policy to a plan from
``plan_cast`` and returns the verdict in a shape the sink can act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from fpulse.types.cast_safety import CastPlanElement, CastSafety


class InferencePolicy(Enum):
    AUTO = "auto"
    STRICT = "strict"
    COERCE = "coerce"
    MANUAL = "manual"
    LEARN = "learn"


class CastPolicy(Enum):
    SAFE = "safe"
    COERCE = "coerce"
    TRUNCATE = "truncate"
    STRICT = "strict"
    LEARN = "learn"


@dataclass
class CastPlanVerdict:
    """Return value from ``gate_cast_plan`` — what the sink should do.

    ``allowed`` is the list of elements the sink may write. ``blocked``
    is the elements the policy refuses to allow (the sink raises with
    these on the error). ``warnings`` is informational only — the sink
    proceeds but the explainability surface records them.
    """

    allowed: list[CastPlanElement] = field(default_factory=list)
    blocked: list[CastPlanElement] = field(default_factory=list)
    warnings: list[CastPlanElement] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked)


def gate_cast_plan(
    plan: list[CastPlanElement],
    policy: CastPolicy,
) -> CastPlanVerdict:
    """Filter a cast plan by policy. Pure function; deterministic."""
    verdict = CastPlanVerdict()
    for element in plan:
        safety = element.safety
        bucket = _classify(policy, safety)
        if bucket == "allow":
            verdict.allowed.append(element)
        elif bucket == "warn":
            verdict.allowed.append(element)
            verdict.warnings.append(element)
        else:  # "block"
            verdict.blocked.append(element)
    return verdict


def _classify(policy: CastPolicy, safety: CastSafety) -> str:
    """Per-safety verdict under a given policy. Returns 'allow' / 'warn' / 'block'."""
    if policy == CastPolicy.STRICT:
        return "allow" if safety == CastSafety.SAFE else "block"

    if policy == CastPolicy.SAFE:
        if safety == CastSafety.SAFE:
            return "allow"
        return "block"

    if policy == CastPolicy.COERCE:
        if safety == CastSafety.SAFE:
            return "allow"
        if safety == CastSafety.SEMANTIC_LOSSY:
            return "warn"
        return "block"  # LOSSY or IMPOSSIBLE

    if policy == CastPolicy.TRUNCATE:
        if safety == CastSafety.SAFE:
            return "allow"
        if safety in (CastSafety.SEMANTIC_LOSSY, CastSafety.LOSSY):
            return "warn"
        return "block"  # IMPOSSIBLE only

    if policy == CastPolicy.LEARN:
        # Permissive: everything proceeds; non-SAFE goes into warnings
        # so the audit log captures it. IMPOSSIBLE still warned (not blocked)
        # because LEARN mode is for development and the operator wants
        # everything to flow until they tighten the policy.
        if safety == CastSafety.SAFE:
            return "allow"
        return "warn"

    # Unknown policy — fail closed.
    return "block"


# ── Inference policy enforcement ──

@dataclass
class InferenceVerdict:
    """Source-side decision: does the inferred schema satisfy policy?

    ``ok`` is True when no UNKNOWN columns survived (under STRICT)
    or when COERCE handled them transparently. ``blocked`` lists the
    fields the policy refuses to forward; ``coerced`` lists those that
    were silently promoted to STRING by COERCE.
    """

    ok: bool
    blocked: list[str] = field(default_factory=list)
    coerced: list[str] = field(default_factory=list)


def gate_inferred_schema(
    schema,  # CanonicalSchema — typed as Any to keep this module import-light
    policy: InferencePolicy,
) -> InferenceVerdict:
    """Apply InferencePolicy to a source-side schema.

    AUTO + LEARN are permissive — UNKNOWNs pass through and let the
    sink policy decide.
    STRICT — any UNKNOWN field blocks the plan.
    COERCE — silently rewrites UNKNOWN fields to STRING.
    MANUAL — rejects any field whose evidence isn't MANUAL.
    """
    # Lazy import to avoid a cycle (canonical.py imports nothing from policy).
    from fpulse.types.canonical import Evidence, FPType

    if policy in (InferencePolicy.AUTO, InferencePolicy.LEARN):
        return InferenceVerdict(ok=True)

    if policy == InferencePolicy.STRICT:
        bad = [f.name for f in schema.fields if f.type == FPType.UNKNOWN]
        return InferenceVerdict(ok=not bad, blocked=bad)

    if policy == InferencePolicy.COERCE:
        coerced: list[str] = []
        for f in schema.fields:
            if f.type == FPType.UNKNOWN:
                f.type = FPType.STRING
                f.params = {}
                f.evidence = Evidence.COERCED
                coerced.append(f.name)
        return InferenceVerdict(ok=True, coerced=coerced)

    if policy == InferencePolicy.MANUAL:
        bad = [f.name for f in schema.fields if f.evidence != Evidence.MANUAL]
        return InferenceVerdict(ok=not bad, blocked=bad)

    return InferenceVerdict(ok=False)
