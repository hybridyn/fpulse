"""Typed contracts for everything the Steward emits.

These are the JSON payloads the API serves and the UI renders. Each
sub-agent (archeologist, autopsy, foreseer, ...) produces
``StewardFinding`` records with sub-agent-specific ``kind``.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class FindingSeverity(str, Enum):
    """Per the alerting tier model — P1 pages, P2 in-app + notification
    channels, P3 daily digest. The Steward is conservative by default:
    almost everything is P2 or P3. P1 is reserved for "data loss imminent"
    or "production blocker" — most findings don't qualify."""

    P1 = "p1"
    P2 = "p2"
    P3 = "p3"


class FindingLevel(str, Enum):
    """The observability layer a finding lives at.

    Added 2026-06-05 per architectural review (multiple independent
    reviewers converged on multi-level observability). Every finding
    declares its level so the UI can group / filter by layer and the
    suppression rules can target the right boundary.

    The taxonomy mirrors industry data-observability practice (Monte
    Carlo's 5 pillars, DataHub's quality dimensions) plus an explicit
    ARCHITECTURE level for structural-design findings that do not fit
    cleanly into "this connector is broken" (CONNECTOR) or "this is
    burning money" (COST). Duplicate extraction, redundant transfer,
    and "same source extracted into N warehouses" are architecture
    decisions with cost + connector consequences — flagging them
    architecturally is more actionable than dropping them into either
    of those buckets.
    """

    PIPELINE     = "pipeline"     # End-to-end run health, SLA, partial output
    NODE         = "node"         # Step-level transforms, join explosion, empty out
    CONNECTOR    = "connector"    # Auth, rate-limit, network, source unreachable
    DATA         = "data"         # Schema drift, null spikes, freshness, partition
    ARCHITECTURE = "architecture" # Duplicate extraction, redundant transfer, structural waste
    GOVERNANCE   = "governance"   # PII movement, credential sprawl, env crossing
    COST         = "cost"         # Cost drift, runaway compute, warehouse waste


class FindingKind(str, Enum):
    """The catalogue of finding types the Steward can produce.

    Each kind maps to exactly one ``FindingLevel`` (see
    ``KIND_TO_LEVEL`` below). The taxonomy is intentionally fine-grained
    so the UI can render specific labels + icons rather than a generic
    "anomaly" pill — users learn what each kind means and what action
    to take, which is the whole point of "advisor, not actor".

    NOT all of these kinds have a shipping detector yet. The contract
    is set now so future specialist modules slot in without re-shaping
    the storage, UI, notification, or memory layers. See `architecture.md`
    §"Roadmap" for the per-module ship plan.

    Active detectors today:
      DUPLICATE_SOURCE, DUPLICATE_PIPELINE (architecture);
      CONNECTOR_AUTH_FAILURE / CONNECTOR_RATE_LIMIT / CONNECTOR_UNREACHABLE /
      CREDENTIAL_NEAR_EXPIRY (connector); SCHEMA_DRIFT + the quality engine
      (NULL_SPIKE / DUPLICATE_KEY_SPIKE / VOLUME_ANOMALY / FRESHNESS_MISS /
      PARTITION_MISSING / QUALITY_CHECK_FAILED) (data); EMPTY_OUTPUT (node);
      WAREHOUSE_WASTE (cost); ENV_CROSSING / UNAPPROVED_DESTINATION / PII_LEAK
      (governance); USER_DEFINED (rules engine).
    Contract-only (detector lands in the indicated release):
      the remaining pipeline / structural kinds — see KIND_RELEASE below.
    """

    # ── Pipeline level ────────────────────────────────────────────
    DUPLICATE_PIPELINE  = "duplicate_pipeline"   # 1.1 — Archeologist (shipped)
    SLA_BREACH          = "sla_breach"           # 1.2 — Sentinel
    PARTIAL_OUTPUT      = "partial_output"       # 1.2 — Sentinel
    RETRY_STORM         = "retry_storm"          # 1.2 — Sentinel

    # ── Node level ────────────────────────────────────────────────
    EMPTY_OUTPUT        = "empty_output"         # active — node empty-output detector
    # 2026-06-18 — node cardinality anomalies, run-fed (see steward/node_cardinality.py).
    JOIN_EXPLOSION      = "join_explosion"       # active — join output >> inputs (near-cartesian)
    JOIN_COLLAPSE       = "join_collapse"        # active — join output << inputs (key mismatch)
    FILTER_DROPPED_ALL  = "filter_dropped_all"   # active — filter removed every row
    DEDUPE_COLLAPSE     = "dedupe_collapse"      # active — dedupe removed almost everything
    CAST_FAILURE        = "cast_failure"         # 1.3 — Foreseer
    # 2026-06-18 — row-count integrity. A step whose contract is 1:1
    # (derived_column, rename, sort, …) changed its row count, which means
    # rows were silently dropped or duplicated. Enforces the "safe
    # optimization" rule in docs/abstraction-boundary.md.
    ROW_COUNT_DELTA     = "row_count_delta"      # active — row-delta integrity detector

    # ── Connector level ───────────────────────────────────────────
    DUPLICATE_SOURCE         = "duplicate_source"          # 1.1 — Archeologist (shipped)
    CONNECTOR_AUTH_FAILURE   = "connector_auth_failure"    # active — Connector-health
    CONNECTOR_RATE_LIMIT     = "connector_rate_limit"      # active — Connector-health
    CONNECTOR_UNREACHABLE    = "connector_unreachable"     # active — Connector-health
    CREDENTIAL_NEAR_EXPIRY   = "credential_near_expiry"    # active — Connector-health

    # ── Data level ────────────────────────────────────────────────
    SCHEMA_DRIFT        = "schema_drift"         # active — Schema-drift detector
    NULL_SPIKE          = "null_spike"           # active — Quality engine
    DUPLICATE_KEY_SPIKE = "duplicate_key_spike"  # active — Quality engine
    VOLUME_ANOMALY      = "volume_anomaly"       # active — Foreseer (baseline variance)
    FRESHNESS_MISS      = "freshness_miss"       # active — Quality engine
    PARTITION_MISSING   = "partition_missing"    # active — Quality engine
    # 2026-06-07 — generic data-quality check failure for constraint-
    # style assertions (accepted_values, range, regex, custom). The
    # specific kinds above (NULL_SPIKE etc) capture the named cases;
    # this kind catches everything else without forcing the caller to
    # squeeze a regex violation into NULL_SPIKE just to emit a finding.
    QUALITY_CHECK_FAILED = "quality_check_failed"  # 1.1.x — Quality engine

    # ── Governance level ──────────────────────────────────────────
    PII_LEAK              = "pii_leak"               # active — Governance (schema-based)
    CREDENTIAL_SPRAWL     = "credential_sprawl"      # contract-only (1.4 — Governor)
    ENV_CROSSING          = "env_crossing"           # active — Governance (dev cred in prod, etc)
    UNAPPROVED_DESTINATION = "unapproved_destination"  # active — Governance

    # ── Cost level ────────────────────────────────────────────────
    COST_DRIFT          = "cost_drift"           # 1.3 — Cost Steward
    REDUNDANT_TRANSFER  = "redundant_transfer"   # 1.3 — Architecture Steward
    # 2026-06-18 — storage intelligence: a managed table no pipeline reads
    # or writes (leftover from a deleted/edited pipeline). Architecture-level.
    ORPHANED_TABLE      = "orphaned_table"       # active — storage-intel detector
    WAREHOUSE_WASTE     = "warehouse_waste"      # active — cost warehouse-waste
    COST_RECOMMENDATION = "cost_recommendation"  # 2.0 — Optimizer

    # ── Cross-cutting ─────────────────────────────────────────────
    FAILURE_RCA         = "failure_rca"          # 1.2 — Incident Analyst
    LINEAGE_CASCADE     = "lineage_cascade"      # 2.0 — Advisor

    # ── User-defined rules ────────────────────────────────────────
    # Findings emitted by the YAML-rule engine (backend/fpulse/steward/
    # rules.py). The rule author picks the level via the rule's `level:`
    # field — we set it directly on the StewardFinding so user rules
    # can live at ANY of the 7 levels without 7 separate kind enums.
    # The rule id is preserved in evidence["rule_id"] so the UI can
    # group user-rule findings by which rule produced them.
    # Added 2026-06-07 for the Plus custom-logic foundation; OSS gets
    # the engine + filesystem discovery, Plus adds authoring UI.
    USER_DEFINED        = "user_defined"         # 1.1 — Rules engine


# Single source of truth for "what level does each finding kind live at?".
# UI groups by FindingLevel, suppression rules target it, and reviewers
# can scan the table to verify nothing slipped into the wrong layer.
KIND_TO_LEVEL: dict[FindingKind, FindingLevel] = {
    # Pipeline
    FindingKind.DUPLICATE_PIPELINE:    FindingLevel.ARCHITECTURE,
    FindingKind.SLA_BREACH:            FindingLevel.PIPELINE,
    FindingKind.PARTIAL_OUTPUT:        FindingLevel.PIPELINE,
    FindingKind.RETRY_STORM:           FindingLevel.PIPELINE,
    # Node
    FindingKind.EMPTY_OUTPUT:          FindingLevel.NODE,
    FindingKind.JOIN_EXPLOSION:        FindingLevel.NODE,
    FindingKind.JOIN_COLLAPSE:         FindingLevel.NODE,
    FindingKind.FILTER_DROPPED_ALL:    FindingLevel.NODE,
    FindingKind.DEDUPE_COLLAPSE:       FindingLevel.NODE,
    FindingKind.CAST_FAILURE:          FindingLevel.NODE,
    FindingKind.ROW_COUNT_DELTA:       FindingLevel.NODE,
    # Connector — pure transport-layer failures only. Structural
    # duplicate-source mapping moved to ARCHITECTURE (2026-06-05)
    # per reviewer feedback: "same source ingested twice" is a
    # design decision, not a transport problem.
    FindingKind.CONNECTOR_AUTH_FAILURE:     FindingLevel.CONNECTOR,
    FindingKind.CONNECTOR_RATE_LIMIT:       FindingLevel.CONNECTOR,
    FindingKind.CONNECTOR_UNREACHABLE:      FindingLevel.CONNECTOR,
    FindingKind.CREDENTIAL_NEAR_EXPIRY:     FindingLevel.CONNECTOR,
    # Data
    FindingKind.SCHEMA_DRIFT:          FindingLevel.DATA,
    FindingKind.NULL_SPIKE:            FindingLevel.DATA,
    FindingKind.DUPLICATE_KEY_SPIKE:   FindingLevel.DATA,
    FindingKind.VOLUME_ANOMALY:        FindingLevel.DATA,
    FindingKind.FRESHNESS_MISS:        FindingLevel.DATA,
    FindingKind.PARTITION_MISSING:     FindingLevel.DATA,
    FindingKind.QUALITY_CHECK_FAILED:  FindingLevel.DATA,
    # Architecture — structural / design-level findings (2026-06-05).
    # Reviewer convergence: duplicate extraction, redundant transfer,
    # repeated source copies are not "connector broke" or "cost too
    # high" — they are architecture decisions. Flagging them
    # architecturally is more actionable than dropping them in
    # CONNECTOR or COST.
    FindingKind.DUPLICATE_SOURCE:      FindingLevel.ARCHITECTURE,
    FindingKind.REDUNDANT_TRANSFER:    FindingLevel.ARCHITECTURE,
    FindingKind.ORPHANED_TABLE:        FindingLevel.ARCHITECTURE,
    FindingKind.LINEAGE_CASCADE:       FindingLevel.ARCHITECTURE,
    # Governance
    FindingKind.PII_LEAK:              FindingLevel.GOVERNANCE,
    FindingKind.CREDENTIAL_SPRAWL:     FindingLevel.GOVERNANCE,
    FindingKind.ENV_CROSSING:          FindingLevel.GOVERNANCE,
    FindingKind.UNAPPROVED_DESTINATION: FindingLevel.GOVERNANCE,
    # Cost — pure spend/waste findings only.
    FindingKind.COST_DRIFT:            FindingLevel.COST,
    FindingKind.WAREHOUSE_WASTE:       FindingLevel.COST,
    FindingKind.COST_RECOMMENDATION:   FindingLevel.COST,
    # Cross-cutting
    FindingKind.FAILURE_RCA:           FindingLevel.PIPELINE,
    # User-defined — default to PIPELINE for KIND_TO_LEVEL lookups,
    # but the rules engine ALWAYS overrides this on the emitted finding
    # using the rule's declared `level:` field. The mapping here is
    # just so generic level_for_kind() never raises.
    FindingKind.USER_DEFINED:          FindingLevel.PIPELINE,
}


def level_for_kind(kind: FindingKind) -> FindingLevel:
    """Map a kind to its observability level. Falls back to PIPELINE
    for any kind not in the table — safer than raising, since the
    Steward must never crash the scan path on an unknown enum."""
    return KIND_TO_LEVEL.get(kind, FindingLevel.PIPELINE)


class FindingStatus(str, Enum):
    """Lifecycle states.

    ``open``      — active alert in the UI.
    ``dismissed`` — user explicitly marked it as intentional
                    (suppressed from this workspace for this exact
                    signature).
    ``resolved``  — the user took action that closes the finding
                    (e.g. deleted one of the duplicate pipelines).
    ``rebounded`` — promoted (2026-06-05, per architectural review) from
                    a body-annotation to a first-class state. Set when
                    a signature previously RESOLVED re-emerges in a
                    later scan. Carries the same severity as a fresh
                    finding plus a `previously_resolved_at` evidence
                    marker. UI renders with the rebound chip so
                    regressions are visually distinct from new issues.
    ``stale``     — the underlying signal is gone but the user hasn't
                    explicitly acknowledged it; auto-aged after 30 days.
    """

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"   # user saw + accepted; not yet acted on (2026-06-05)
    DISMISSED = "dismissed"
    RESOLVED = "resolved"
    REBOUNDED = "rebounded"
    SUPPRESSED = "suppressed"       # silenced for a window (deploy maintenance) (2026-06-05)
    EXPIRED = "expired"             # auto-aged beyond stale + grace period (2026-06-05)
    STALE = "stale"


class StewardFinding(BaseModel):
    """A single observation the Steward wants the user to know about.

    Always carries enough provenance that a reviewer can re-derive it
    from the audit log — never an opaque "the AI thinks so."
    """

    id: str
    workspace_id: str = "default"
    kind: FindingKind
    # 2026-06-05 — observability level (pipeline / node / connector /
    # data / governance / cost). Defaults from KIND_TO_LEVEL via
    # `level_for_kind()`; callers can override for cross-cutting cases
    # (e.g. FAILURE_RCA pinpointed to a specific node).
    level: FindingLevel = FindingLevel.PIPELINE
    severity: FindingSeverity
    status: FindingStatus = FindingStatus.OPEN

    # User-visible title (one sentence, ≤ 80 chars). The UI renders this
    # as the alert headline.
    title: str
    # User-visible body (markdown allowed). Contains the "what / why /
    # proposed action" block. Kept human-narratable.
    body: str

    # Concrete, machine-readable evidence. The shape varies per ``kind``
    # — the consumer narrows by switching on ``kind``. Examples:
    #   DUPLICATE_SOURCE → {"connection_id": "...", "object": "...",
    #                       "workflow_ids": [...]}
    #   FAILURE_RCA      → {"execution_id": "...", "error_class": "...",
    #                       "matched_past_incidents": [...]}
    evidence: dict[str, Any] = Field(default_factory=dict)

    # Suggested actions the UI surfaces as buttons. Each entry is
    # ``{label, action, params}`` — the executor dispatches by ``action``
    # name. The Steward only proposes; the user (or an approval gate)
    # confirms before anything happens.
    proposed_actions: list[dict[str, Any]] = Field(default_factory=list)

    # When the finding was first observed (UTC ISO 8601). Used for
    # auto-aging into ``stale`` status.
    first_seen: str
    # When the underlying signal was last confirmed (UTC ISO 8601).
    last_seen: str
    # Monotonic recurrence counter. A finding seen 17 times across runs
    # is more urgent than one seen once. Used for severity escalation
    # rules in the foreseer/optimizer sub-agents.
    occurrences: int = 1

    # ── Confidence + evidence richness (added 2026-06-05 per reviewer
    # convergence). Without these fields, the Steward sounds equally
    # certain about every finding — which is misleading when a
    # statistical-anomaly detector with 4 days of history says "this is
    # unusual" vs a deterministic duplicate-source check that knows
    # for a fact two pipelines read the same table. The Steward MUST
    # earn calibration trust before users act on findings without
    # double-checking.
    #
    # `confidence` is the user-facing pill (LOW / MEDIUM / HIGH).
    # `confidence_score` is the underlying 0.0-1.0 number (Wilson lower
    # bound, statistical p-value, or signature-match certainty — the
    # producing module is responsible for choosing the right metric).
    # `evidence_count` is the number of distinct observations that
    # back the finding (deduplicate-source = workflow count; volume
    # anomaly = baseline-sample size; failure RCA = matched past
    # incident count).
    # `baseline_window` is the time window the detector used to form
    # its expectation (e.g. "30_days", "100_runs", "instantaneous").
    # ────────────────────────────────────────────────────────────────
    confidence: str = "high"
    confidence_score: float = 1.0
    evidence_count: int = 1
    baseline_window: str = "instantaneous"
