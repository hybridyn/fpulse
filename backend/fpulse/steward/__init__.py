"""F-Pulse Steward — the background reliability + learning layer.

The Steward is F-Pulse's defining differentiator vs other OSS orchestrators.
It watches the pipeline definitions and the audit log and produces findings
that help users build safer, cheaper, less duplicative data pipelines —
without ever modifying production behaviour on its own.

Sub-agents (each a separate module — building out as v1.1 → v2.0):

  * archeologist.py — duplicate-source / duplicate-pipeline detection (v1.1)
  * autopsy.py      — failure root-cause analysis with memory (v1.2, planned)
  * foreseer.py     — volumetric + structural anomaly detection (v1.3, planned)
  * curator.py      — memory distillation into EPULSE_RUNBOOK.md (v1.4, planned)
  * optimizer.py    — cost + performance recommendations (v2.0, planned)

# Hard rules (architectural invariants — do NOT relax)

1. **Read-only.** The Steward NEVER mutates workflows, connections, schedules,
   credentials, or any other persisted state on its own. It produces findings.
   The user (or another agent with explicit user approval) applies them.
2. **Out-of-band.** The Steward must not block pipeline execution. It runs
   parallel to the executor, consuming the audit log + workflow store
   snapshots.
3. **Deterministic core, LLM-narration shell.** Each sub-agent's detection
   logic is plain code (graph/statistics/pattern-matching). The LLM is only
   used to phrase the finding in natural language — and never gates
   correctness. The Steward must remain useful with the LLM disabled.
4. **Explicit provenance.** Every finding carries the IDs of the inputs it
   inspected so a reviewer can trace it back to source.
5. **OSS-first.** The Steward ships in F-Pulse OSS. Plus tier adds
   cross-workspace correlation, shared memory, RBAC-aware approvals — never
   gates the core detection capability.
6. **Historical Baseline Variance, not absolute thresholds.** Volume,
   null-rate, freshness, and similar quantitative alerts MUST compare
   against an observed per-signature baseline — never an absolute
   number. A node that returns 0 rows 90% of the time is not flagged
   on the 91st zero day. A node that returns 10k +/- 500 rows daily
   IS flagged when it returns 0. Added 2026-06-05 (architectural
   review) to prevent the "valid empty table" fallacy that mutes
   monitoring channels.
7. **Intentional-change suppression.** Schema / topology mutations
   co-occurring across N+ entities within a maintenance window are
   rolled into a single baseline-update card, not N separate
   findings. Added 2026-06-05 to prevent "schema drift fatigue" when
   a planned migration touches many tables at once.
"""

from .models import (
    StewardFinding,
    FindingSeverity,
    FindingKind,
    FindingLevel,
    FindingStatus,
    KIND_TO_LEVEL,
    level_for_kind,
)
from .archeologist import detect_duplicate_sources
from .governance import (
    GovernancePolicy,
    GovernancePolicyStore,
    detect_governance,
)
from .connector_health import (
    ConnectorHealthState,
    ConnectorHealthStore,
    classify_error,
    detect_connector_health,
    record_test_outcome,
)
from .cost import (
    CostEvent,
    CostEventStore,
    CostFindingStore,
    detect_cost_findings,
    record_cost_event,
    summarise_by_source,
)
# 2026-06-08 — foreseer: automatic volumetric anomaly detection
# (VOLUME_ANOMALY) using historical baseline variance (Hard Rule 6).
# Reads the existing CostEvent history; no new ingestion surface.
from .foreseer import (
    detect_volume_anomalies,
    modified_zscore,
)
from .memory import StewardMemory, new_scan_id, apply_learning, sanitize_user_note
from .pii import (
    PIIFindingStore,
    check_columns_for_pii,
    detect_pii_findings,
    record_pii_findings,
)
from .quality import (
    QualityAssertion,
    QualityCheckReport,
    QualityFindingStore,
    detect_quality_findings,
    record_quality_report,
)
from .schema_drift import (
    Column,
    SchemaChange,
    SchemaDriftFindingStore,
    SchemaSnapshot,
    SchemaSnapshotStore,
    detect_schema_drift,
    diff_schemas,
    record_snapshot,
)
from .row_delta import (
    PRESERVING_STEP_TYPES,
    RowDeltaFindingStore,
    detect_row_deltas,
    record_row_deltas,
)
from .node_cardinality import (
    NodeCardinalityFindingStore,
    detect_node_cardinality,
    record_node_cardinality,
)
from .storage_intel import detect_orphaned_tables
from .rules import (
    NodeMatch,
    RuleLoadError,
    UserRule,
    WorkflowMatch,
    evaluate_rules,
    load_rules,
)
from .settings import StewardSettings, SettingsStore
# 2026-06-05 — F-Pulse Memory Layer. Distinct from memory.py (which is
# the operational event journal). Lessons are the durable, human-approved
# institutional knowledge surface. See docs/steward/memory-layer.md.
from .lessons import (
    EvidenceRef,
    LessonConfidence,
    LessonStatus,
    LessonStore,
    LessonType,
    MemoryLesson,
)

__all__ = [
    "StewardFinding",
    "FindingSeverity",
    "FindingKind",
    "FindingLevel",
    "FindingStatus",
    "KIND_TO_LEVEL",
    "level_for_kind",
    "detect_duplicate_sources",
    "StewardMemory",
    "new_scan_id",
    "apply_learning",
    "sanitize_user_note",
    "StewardSettings",
    "SettingsStore",
    "NodeMatch",
    "WorkflowMatch",
    "UserRule",
    "RuleLoadError",
    "load_rules",
    "evaluate_rules",
    "ConnectorHealthState",
    "ConnectorHealthStore",
    "classify_error",
    "detect_connector_health",
    "record_test_outcome",
    "Column",
    "SchemaChange",
    "SchemaSnapshot",
    "SchemaSnapshotStore",
    "SchemaDriftFindingStore",
    "diff_schemas",
    "record_snapshot",
    "detect_schema_drift",
    "PRESERVING_STEP_TYPES",
    "RowDeltaFindingStore",
    "detect_row_deltas",
    "record_row_deltas",
    "NodeCardinalityFindingStore",
    "detect_node_cardinality",
    "record_node_cardinality",
    "detect_orphaned_tables",
    "QualityAssertion",
    "QualityCheckReport",
    "QualityFindingStore",
    "record_quality_report",
    "detect_quality_findings",
    "GovernancePolicy",
    "GovernancePolicyStore",
    "detect_governance",
    "CostEvent",
    "CostEventStore",
    "CostFindingStore",
    "record_cost_event",
    "detect_cost_findings",
    "summarise_by_source",
    "detect_volume_anomalies",
    "modified_zscore",
    "PIIFindingStore",
    "check_columns_for_pii",
    "record_pii_findings",
    "detect_pii_findings",
    "EvidenceRef",
    "LessonConfidence",
    "LessonStatus",
    "LessonStore",
    "LessonType",
    "MemoryLesson",
]
