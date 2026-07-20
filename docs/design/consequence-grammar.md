# DESIGN — Consequence Grammar (F-Pulse OSS)

> **Status:** Draft (2026-05-19). Locked as the foundation artifact for the "reasoning data workspace" North Star. Implementation gated on post-v1.0.
>
> **Scope:** OSS. Plus may extend the grammar with governance-specific categories later (reserved namespace, section 10).
>
> **Companion memory:**
> - `project_fpulse_north_star_reasoning_workspace_2026-05-19.md`
> - `project_fpulse_v1_demo_prerun_impact_card_2026-05-19.md`
> - `project_fpulse_local_llm_floor_revision_2026-05-19.md`

---

## 1. Purpose

Define the typed contract between three layers of the F-Pulse reasoning system:

1. **Deterministic Engine (Tier 1)** — analyzes pipeline graph, schema, row stats, execution telemetry. Emits typed `Warning`, `Recommendation`, `Consequence`, `Intent` objects.
2. **Local LLM Explainer (Tier 2)** — consumes typed objects, produces human-readable explanation strings. Never the source of truth.
3. **UI Surface (Tier 3)** — renders typed objects + explanations as cards, badges, panels, scores.

Without a shared grammar, prompts become the de facto contract — and the system drifts. The grammar is also the eval-harness scoring surface (precision/recall by `rule_id`).

## 2. Goals & Non-Goals

**Goals:**
- A complete v1 catalog of `Warning`, `Recommendation`, `Intent`, `Consequence` types
- Strict Pydantic schemas (validatable by FastAPI + UI TypeScript codegen)
- A uniform severity / confidence / evidence model across all types
- Stable deterministic identifiers so the eval harness can score per type and LLM explanations can be cached
- Extension points so Plus can add governance categories without breaking OSS

**Non-Goals:**
- The analyzer engine code itself (separate `DESIGN_CONSEQUENCE_ENGINE.md`, post-v1.0)
- LLM prompt templates (separate, post-grammar)
- UI rendering specs (separate, post-engine)
- An exhaustive list of every conceivable warning — the v1 catalog is a believable, demoable subset; new types are PR-able later

## 3. Top-Level Schema

```python
from typing import Literal, Optional
from pydantic import BaseModel

Severity = Literal["info", "low", "medium", "high", "critical"]
Confidence = Literal["heuristic", "estimated", "measured"]
Kind = Literal["warning", "recommendation", "consequence", "intent"]


class Evidence(BaseModel):
    """What the engine observed that led to this finding."""
    rule_id: str                              # which engine rule fired (stable identifier)
    source_step_id: str
    column: Optional[str] = None
    sample_value: Optional[str] = None
    metric_name: Optional[str] = None         # e.g., "null_pct", "unique_pct", "row_count"
    metric_value: Optional[float] = None


class ReasoningArtifact(BaseModel):
    """Parent of Warning / Recommendation / Consequence / Intent."""
    id: str                                   # deterministic hash (see section 9)
    kind: Kind
    type: str                                 # the catalog entry (e.g., "join_explosion")
    severity: Severity
    confidence: Confidence
    evidence: list[Evidence] = []
    explanation: Optional[str] = None         # LLM-filled; None = use deterministic template
    workflow_id: str
    run_id: Optional[str] = None              # None for canvas-edit-time findings; set for run-time
    suggested_remediation: Optional[str] = None  # foreign key into Recommendation catalog by type
```

## 4. Warning Catalog (v1)

Warnings are problems the engine detected. Each has a deterministic detection rule, a typed payload, and a recommended remediation type (foreign key into section 5).

### 4.1 Pipeline Topology

| Type | Severity floor | Detection rule | Recommends |
|---|---|---|---|
| `dead_branch` | low | Subgraph with no path to any sink | (none — delete or wire) |
| `cyclic_dependency` | high | Cycle detected in workflow DAG | (none — manual fix) |
| `disconnected_node` | medium | Node not connected to any source | (none — delete or wire) |
| `unused_output` | info | Sink output never referenced downstream | (none — delete or use) |

### 4.2 Schema

| Type | Severity floor | Detection rule | Recommends |
|---|---|---|---|
| `schema_break_downstream` | high | Upstream column rename/drop breaks ≥1 downstream node | `update_downstream_refs` |
| `type_narrowing` | medium | VARCHAR → INTEGER with rows that may not cast | `cast_before_join` |
| `missing_column` | high | Downstream node references column not in upstream schema | (manual) |
| `column_alias_collision` | low | Two upstream nodes produce same column name with different types | `rename_column` |

### 4.3 Join / Cardinality

| Type | Severity floor | Detection rule | Recommends |
|---|---|---|---|
| `join_explosion` | high | Left and right both non-unique on join key → estimated rows > 10× max(left, right) | `reorder_join`, `add_dedupe` |
| `cartesian_join` | critical | Join with no key (cross product) | `add_join_key` |
| `non_unique_left_key` | medium | Inner/left join with non-unique left side (silent duplication) | `add_dedupe` |
| `fanout_explosion` | high | Single-row input fans to >100× via array explode / unnest | `partition_output` |

### 4.4 Data Quality

| Type | Severity floor | Detection rule | Recommends |
|---|---|---|---|
| `null_propagation` | medium | Null rate in column > 10% at any step | `add_null_filter` |
| `duplicate_risk` | medium | Dedupe-suggestive column not used as primary key downstream | `add_dedupe` |
| `outlier_spike` | low | Column value distribution shifts >3σ from prior run | (review) |
| `invalid_range` | medium | Numeric column has values outside declared range constraint | `add_range_filter` |
| `regex_validation_fail` | medium | String column has rows not matching declared regex | `add_regex_filter` |

### 4.5 Performance

| Type | Severity floor | Detection rule | Recommends |
|---|---|---|---|
| `expensive_scan` | medium | Full-table scan where indexed filter is available | `pushdown_filter` |
| `missing_pushdown` | low | Filter could be moved before join/aggregate | `pushdown_filter` |
| `memory_pressure` | high | Estimated intermediate result > worker memory budget | `add_materialization`, `partition_output` |
| `retry_loop_risk` | medium | Side-effect node with retry > 1 and no idempotency key | `add_idempotency_key` |
| `unnecessary_materialization` | low | Step result materialized but used only once | `remove_materialization` |

### 4.6 Operational

| Type | Severity floor | Detection rule | Recommends |
|---|---|---|---|
| `huge_payload` | medium | API request/response > 10MB | `partition_output` |
| `slow_api` | low | API step P95 > 30s in last 10 runs | (review SLA) |
| `inconsistent_snapshot` | high | Source read mid-update without versioning/snapshot | `add_snapshot_read` |

## 5. Recommendation Catalog (v1)

Recommendations are suggested fixes the engine produces, tied to one or more warnings.

| Type | Triggered by | Action description |
|---|---|---|
| `pushdown_filter` | `expensive_scan`, `missing_pushdown` | Move filter step before join/aggregate |
| `add_materialization` | `memory_pressure` | Cache step result to bound peak memory |
| `remove_materialization` | `unnecessary_materialization` | Drop unused materialize step |
| `reorder_join` | `join_explosion` | Join in different order to reduce intermediate size |
| `cast_before_join` | `type_narrowing` | Insert explicit cast step before join |
| `add_dedupe` | `duplicate_risk`, `non_unique_left_key`, `join_explosion` | Insert dedupe before join |
| `add_idempotency_key` | `retry_loop_risk` | Wire idempotency key to side-effect node |
| `partition_output` | `huge_payload`, `fanout_explosion`, `memory_pressure` | Batch large output into multiple writes |
| `add_null_filter` | `null_propagation` | Drop or fill nulls explicitly upstream |
| `add_range_filter` | `invalid_range` | Insert numeric range guard upstream |
| `add_regex_filter` | `regex_validation_fail` | Insert string-pattern guard upstream |
| `add_join_key` | `cartesian_join` | Specify a join condition |
| `add_snapshot_read` | `inconsistent_snapshot` | Wrap source read in a versioned snapshot |
| `rename_column` | `column_alias_collision` | Alias one of the conflicting columns |
| `update_downstream_refs` | `schema_break_downstream` | Update downstream nodes to use the new column name |

## 6. Consequence Catalog (v1)

Consequences are *predicted effects* of running the pipeline as configured. Used by the **pre-run impact card** (v1.0 demo, see companion memory).

| Type | Payload (in addition to common ReasoningArtifact fields) |
|---|---|
| `row_delta` | `step_id`, `input_rows: int`, `estimated_output_rows: int`, `delta_pct: float` |
| `schema_delta` | `step_id`, `columns_added: list[str]`, `columns_removed: list[str]`, `columns_retyped: list[tuple[str, str, str]]` (column, from_type, to_type) |
| `null_delta` | `step_id`, `column`, `input_null_pct: float`, `estimated_output_null_pct: float` |
| `memory_delta` | `step_id`, `estimated_peak_mb: float` |
| `cost_delta` | `step_id`, `estimated_api_calls: int`, `estimated_compute_seconds: float` |
| `latency_delta` | `step_id`, `estimated_seconds: float` |

Confidence semantics: most consequences will be `estimated` (engine sampled or projected). `measured` requires a prior run with matching pipeline hash.

## 7. Intent Catalog (v1)

Intents are the user's stated or inferred goal. Used by Tier 2 (LLM intent parser) to route a natural-language prompt to a workflow template or guided builder.

| Type | Example prompt | Routes to |
|---|---|---|
| `clean_data` | "Clean this customer list" | Data wrangler with null/regex/dedupe defaults |
| `remove_duplicates` | "Dedupe by email" | Dedupe node configured with column hint |
| `enrich_records` | "Add Salesforce data to these accounts" | Connector + join builder |
| `aggregate_by_key` | "Sum revenue by region" | Aggregate node |
| `normalize_schema` | "Make the columns match the staging table" | Schema mapper |
| `optimize_pipeline` | "Make this faster" | Engine optimization analyzer (no direct edit) |
| `explain_failure` | "Why did this run fail?" | Diagnostic LLM call grounded in last run telemetry |
| `explain_step` | "What does this node do?" | Help atlas lookup + LLM rephrase |

Intent confidence is always `heuristic` or `estimated` — never `measured`.

## 8. Severity / Confidence / Evidence Model

**Severity** — fixed 5-level scale (`info`, `low`, `medium`, `high`, `critical`). Engine emits the floor severity; users can override per-rule via settings.

**Confidence:**
- `heuristic` — rule fires on shape alone (e.g., non-unique key detected → join explosion possible). No row data examined.
- `estimated` — engine sampled or did a partial scan to project the warning.
- `measured` — engine ran the step (or a prior run did) and has actual counts.

**UI rule:** only `severity ≥ medium` warnings surface in the pre-run impact card by default. `info` / `low` available in a "Details" drawer.

**Evidence** — every artifact MUST carry at least one `Evidence` object with a `rule_id`. The eval harness uses `rule_id` for precision / recall scoring. Without a `rule_id`, the artifact is unscored / unstable and must not ship.

## 9. Stable Identifiers & Caching

Every `ReasoningArtifact.id` is a deterministic hash of:

```
sha256(workflow_id + step_id + type + sorted(evidence.rule_id + evidence.metric_name))
```

This lets:
- The UI dedupe across re-renders
- The engine cache LLM-generated `explanation` strings per artifact `id`
- The eval harness diff artifact sets between runs

LLM-generated explanations are cached forever (artifact `id` is the cache key). If the user changes the pipeline, a new artifact `id` is produced and a fresh explanation is generated.

## 10. Extension Points

**Plus extensions** (out of scope for OSS grammar, but reserved namespace):
- `governance.*` — policy violations, RBAC denials, audit-trail anomalies
- `compliance.*` — PII/PHI detection, retention violations
- `cost_governance.*` — workspace AI cost-rate threshold warnings, per-developer usage caps

OSS grammar MUST NOT reference these types directly. Plus can register additional types via a manifest. Discriminator: type strings prefixed with `governance.` / `compliance.` / `cost_governance.` are routed only when the Plus tier is detected at runtime.

**Custom rule extensions** (future OSS): workspace admins can register custom rules with a JSON DSL. Out of v1 scope but the grammar already supports it because `Evidence.rule_id` is a free-form string.

## 11. Eval Harness Integration

The existing eval harness (14 cases × 5 categories) must extend with three new categories that score the grammar:

- **`reasoning.warning_precision`** — given a labeled test pipeline, did the engine produce the expected warning types? `tp / (tp + fp)` per `type`.
- **`reasoning.warning_recall`** — did the engine miss expected warnings? `tp / (tp + fn)` per `type`.
- **`reasoning.explanation_faithfulness`** — does the LLM `explanation` correctly reference the `Evidence`? Scored by structured comparison, not free-text grading.

Test pipelines are hand-crafted to provoke specific warning types. Pipeline hash + expected artifact-id set = the ground-truth fixture.

## 12. Async & Structured Generation Pattern

Per `project_fpulse_local_llm_floor_revision_2026-05-19.md`:

- LLM `explanation` generation runs async; never block the executor, ingestion, or UI thread.
- All LLM calls use structured generation (Ollama `format: "json"`, or Instructor / Outlines on top).
- Pre-filter common engine outputs with deterministic explanation templates before invoking LLM. The LLM is the upgrade path, not the default path.
- LLM timeout (default 60s) falls back to deterministic template. UI never spinners forever on the model.

## 13. Open Questions

1. **Engine location:** in-process with the executor, or a separate analyzer service? (Decide when post-v1 implementation starts.)
2. **Explanation caching:** persistent (DB) or in-memory only? (Default: persistent keyed on artifact `id`.)
3. **Eval ground-truth source:** manually labeled test pipelines, or synthetic generation? (Default: manual for v1 catalog; revisit if catalog grows past ~50 types.)
4. **Multi-language explanations:** in-scope for OSS, or English-only? (Default English; revisit when first non-English deployment is real.)
5. **Severity overrides:** workspace-level, user-level, or per-pipeline? (Default workspace-level; user override is Plus.)

## 14. References

**Memory:**
- `project_fpulse_north_star_reasoning_workspace_2026-05-19.md` — the North Star this grammar serves
- `project_fpulse_v1_demo_prerun_impact_card_2026-05-19.md` — the v1.0 demo this grammar makes possible
- `project_fpulse_local_llm_floor_revision_2026-05-19.md` — Tier 2 model floor
- `project_fpulse_positioning_lock_2026-05-03.md` — "Governed data system with AI inside"
- `project_fpulse_local_only_lock_2026-05-03.md` — local-first principle
- `project_fpulse_oss_resilience_table_stakes_2026-05-10.md` — OSS gets table-stakes resilience

**Existing primitives this builds on:**
- Schema propagation
- Row counts (per-step)
- Mutation tracking
- Lineage graph
- Validation engine
- Checkpoints (Gate 3.5)
- DataProfileNode (shipped Batch 6)

**Adjacent design docs:**
- `DESIGN_EXECUTION_INTELLIGENCE.md` (empty placeholder in F-Pulse main repo — may converge here)
- `DESIGN_COLUMN_LEVEL_LINEAGE.md` (provides the column-level edges this grammar's evidence layer references)
- `DESIGN_DATA_PREVIEW_EVERYWHERE.md` (provides the row samples this grammar's Evidence.sample_value uses)

## 15. Changelog

- **2026-05-19** — Initial draft. Locked as foundation artifact for the reasoning workspace North Star.
