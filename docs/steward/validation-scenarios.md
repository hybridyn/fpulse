# Steward — Validation Scenario Matrix

Structured per the converged feedback from four independent reviewers
(Pack v1 = Reviewer 1's 12-item core; matrix format = Reviewer 2;
80-test gold standard = Reviewer 3 long-term; FindingLevel grouping =
Reviewer 4).

## Status legend

| Tag             | Meaning                                                        |
|-----------------|----------------------------------------------------------------|
| **PACK-V1**     | In the 12-item Scenario Pack — all passing in 1.1              |
| **UNIT-TESTED** | Covered by `backend/tests/test_steward_archeologist.py`        |
| **CONTRACT**    | `FindingKind` enum value exists; detector ships in 1.2 / 1.3 / 1.4 |
| **GAP**         | Worth implementing as a test before the next release           |

---

## Pack v1 — the 12 scenarios that ship in 1.1

Run: `.venv\Scripts\python.exe backend\scripts\steward_scenario_pack_v1.py`
Latest run: **12/12 passed** (preserved at
`docs/steward/PROOF-2026-06-06/13-scenario-pack-v1.txt`).

| ID | Level | Scenario | Status |
|---|---|---|---|
| V1-01 | architecture | Duplicate source detected across 3 pipelines | **PACK-V1 / UNIT-TESTED** |
| V1-02 | architecture | Duplicate pipeline detected when source + sink both match | **PACK-V1 / UNIT-TESTED** |
| V1-03 | architecture | Intentional duplicate dismissed with reason; reason preserved verbatim | **PACK-V1 / UNIT-TESTED** |
| V1-04 | architecture | Severity escalation P2 → P1 after 5 ignored scans | **PACK-V1 / UNIT-TESTED** |
| V1-05 | architecture | Time-clamp blocks fast escalation (60-second cron does not race to P1) | **PACK-V1 / UNIT-TESTED** |
| V1-06 | architecture | Rebound state on resolved-then-recurring finding | **PACK-V1 / UNIT-TESTED** |
| V1-07 | data | Memory Layer: propose → approve → search returns the approved lesson | **PACK-V1 / UNIT-TESTED** |
| V1-08 | data | Memory Layer: REJECTED lesson never influences search | **PACK-V1 / UNIT-TESTED** |
| V1-09 | architecture | Notification de-dup: 4 rescans produce 1 notification, not 4 | **PACK-V1 / UNIT-TESTED** |
| V1-10 | architecture | Per-workspace signature isolation (Plus-ready, no cross-tenant leak) | **PACK-V1 / UNIT-TESTED** |
| V1-11 | pipeline | Corrupt memory journal — bad lines skipped, no exception escapes | **PACK-V1 / UNIT-TESTED** |
| V1-12 | pipeline | `enabled=false` master kill-switch persists across reload | **PACK-V1 / UNIT-TESTED** |

---

## Additional scenarios in unit tests (not in Pack v1 demo)

These are covered by the 67-test suite but didn't make Pack v1 to keep
the demo focused on the most-asked behaviours:

| Scenario | Test | Level |
|---|---|---|
| Signature stable across dict-insertion-order changes | `test_field_ordering_doesnt_change_signature` | architecture |
| Same source in different connections → distinct signatures | `test_different_connection_different_signature` | architecture |
| Same source twice in ONE workflow → NOT flagged | `test_single_workflow_duplicate_source_not_flagged` | architecture |
| Fan-out (same source, different sinks) → flagged as duplicate_source, NOT duplicate_pipeline | `test_same_source_different_sink_not_pipeline_dup` | architecture |
| Finding IDs are deterministic across runs | `test_finding_id_stable_across_runs` | pipeline |
| F-Pulse step format (real production shape) detected | `test_detector_handles_fpulse_step_format` | architecture |
| Mixed React-Flow + F-Pulse formats in same workspace | `test_detector_handles_mixed_format_workspaces` | architecture |
| Severity escalation honours the count threshold | `test_severity_escalates_after_threshold` | architecture |
| Under-threshold counts do NOT escalate | `test_no_escalation_under_threshold` | architecture |
| Dismiss resets the persistent occurrence counter (alert-fatigue prevention) | `test_dismiss_resets_persistent_occurrence_counter` | architecture |
| Dismiss-reason AWS key sanitization | `test_dismiss_reason_sanitizer_strips_aws_keys` | governance |
| Dismiss-reason `password=secret` sanitization | `test_dismiss_reason_sanitizer_strips_password_kv` | governance |
| Dismiss-reason URI-creds + private-IP sanitization | `test_dismiss_reason_sanitizer_strips_uri_credentials` | governance |
| Dismiss-reason normal text round-trips verbatim | `test_dismiss_reason_sanitizer_passes_normal_text` | governance |
| Confidence richness on every finding | `test_finding_carries_confidence_richness` | pipeline |
| 8-state finding lifecycle exists | `test_expanded_finding_status_values_exist` | pipeline |
| KIND_TO_LEVEL mapping is complete | `test_kind_level_mapping_is_complete` | pipeline |
| Lesson YAML round-trip is human-readable | `test_yaml_render_is_human_readable` | data |
| Lesson revalidation bumps occurrence + may promote confidence | `test_revalidate_bumps_count_and_resets_clock` | data |
| STALE lesson revives on revalidate | `test_stale_lesson_revives_on_revalidate` | data |
| Workspace prefix changes signature (multi-tenant safety) | `test_workspace_prefix_changes_signature` | architecture |
| Notification bridge: escalation triggers new notification | `test_escalation_triggers_new_notification` | architecture |
| Notification bridge: rebound triggers new notification | `test_rebound_triggers_new_notification` | architecture |
| Notification bridge: below-min-severity does not notify | `test_below_min_severity_does_not_notify` | architecture |
| Notification bridge: silent when notification store missing | `test_silent_when_store_missing` | pipeline |

---

## Contract-shipped, detector pending (1.2 onwards)

Reviewer 3 / 4's "gold standard" scenarios. The `FindingKind` enum
values exist in 1.1 so the UI, memory, notification, and suppression
layers all understand them — but the detector that emits them ships
later.

| Scenario | Kind | Module | Ships |
|---|---|---|---|
| Pipeline runs over its SLA window | `sla_breach` | Sentinel | 1.2 |
| Pipeline succeeded but sink wrote 0 rows | `partial_output` | Sentinel | 1.2 |
| Repeated retry loop pattern | `retry_storm` | Sentinel | 1.2 |
| Pipeline normally non-empty writes 0 rows (with baseline) | `empty_output` | Sentinel | 1.2 |
| Pipeline often returns 0 rows on quiet days — NO alert (valid-empty guard, Rule 6) | `empty_output` | Sentinel | 1.2 |
| Connector auth expired / token rotation due | `connector_auth_failure`, `credential_near_expiry` | Sentinel | 1.2 |
| Repeated HTTP 429 / rate limiting | `connector_rate_limit` | Sentinel | 1.2 |
| Source host unreachable | `connector_unreachable` | Sentinel | 1.2 |
| Schema drift: column added / removed / typechanged | `schema_drift` | Foreseer | 1.3 |
| Planned migration: 50 tables in 60s window → ONE bundled card (Rule 7) | `schema_drift` | Foreseer | 1.3 |
| Null fraction spike on key column | `null_spike` | Foreseer | 1.3 |
| Volume anomaly: today's load deviates from its learned baseline (median/MAD) | `volume_anomaly` | Foreseer | **shipped 1.2.x** |
| Daily partition not received within freshness window | `freshness_miss` | Foreseer | 1.3 |
| Partition expected by downstream is missing | `partition_missing` | Foreseer | 1.3 |
| Join explodes 10k → 5M rows | `join_explosion` | Foreseer | 1.3 |
| Join collapses 10k → 10 rows | `join_collapse` | Foreseer | 1.3 |
| Filter drops 100% of input | `filter_dropped_all` | Foreseer | 1.3 |
| Cast failure spike on a column | `cast_failure` | Foreseer | 1.3 |
| Cost drift — today's load is 1766% above 30-day baseline | `cost_drift` | Cost Steward | 1.3 |
| Cross-cloud egress detected | `redundant_transfer` | Architecture Steward | 1.3 |
| Warehouse stayed warm overnight, no jobs ran | `warehouse_waste` | Cost Steward | 1.3 |
| PII column flows to unmanaged destination | `pii_leak` | Governor | 1.4 |
| Same service account used across N projects | `credential_sprawl` | Governor | 1.4 |
| DEV connection used in a PROD-bound workflow | `env_crossing` | Governor | 1.4 |
| Data written to a non-approved sink | `unapproved_destination` | Governor | 1.4 |
| Pipeline failed → past matching incident's approved_fix surfaced automatically | `failure_rca` | Incident Analyst | 1.2 |
| Upstream issue cascades to N downstream tables | `lineage_cascade` | Advisor | 2.0 |
| Optimizer recommends cost-saving config change | `cost_recommendation` | Optimizer | 2.0 |

---

## Gap closure (all 8 closed 2026-06-06)

Every gap on the original list now has a passing test. Status updated
after the V1-Gaps pass.

| Gap | Test | Status |
|---|---|---|
| G1 — Source with `connector_type` but no object-identity → graceful skip | `test_source_without_identity_returns_none` | **CLOSED** + detector tightened (`object_identity_fields` group required) |
| G2 — Already-P1 finding stays P1 on further escalations (no over-bump) | `test_p1_does_not_double_escalate` | **CLOSED** — `apply_learning` guard already correct; "escalated" appears at most once in body |
| G3 — Rebounded finding re-resolved → cleanly rebounds with LATEST resolve timestamp | `test_re_resolve_clears_then_rebounds_cleanly` | **CLOSED** — `previously_resolved_at` always tracks the latest resolve |
| G4 — Lesson auto-aged to STALE after `validity_days` | `test_lesson_auto_ages_to_stale` | **CLOSED** — `age_to_stale()` transitions on backdated `last_validated`; second sweep is no-op |
| G5 — Cross-source lesson search (source=`""`) returns all approved lessons | `test_lesson_search_with_no_source_filter` | **CLOSED** — 3 sources / 3 matches when source filter empty |
| G6 — `notify_on_finding=false` produces zero notifications regardless of severity | `test_notify_disabled_produces_no_bell_rows` | **CLOSED** — even with P1 + min_severity=p1, toggle off = 0 rows |
| G7 — Workspace A dismiss does NOT suppress Workspace B's finding | `test_dismiss_does_not_leak_across_workspaces` | **CLOSED** — workspace-prefixed signatures + per-workspace suppression files; cross-tenant safety verified |
| G8 — Steward router error path returns valid JSON, never HTML | `test_steward_api_error_returns_json_not_html` | **CLOSED** — TestClient verifies 404 + 400 both return `application/json` with `detail` field |

Total test count: **75 unit tests** + **12 named scenarios** = all green.

---

## How to use this matrix

- **For "does the Steward work right now?"** → run Pack v1 and read the
  passes. 12 named scenarios cover the most-asked behaviours.
- **For "is this safe to leave running unattended?"** → check the
  governance + corrupt-resilience rows. They are all in the test suite.
- **For "what's coming next?"** → read the Contract section. Every row
  there has a `FindingKind` value already wired into the UI / memory /
  notification layers — only the detector ships later.
- **For "what should we test before the next release?"** → take the
  Known Gaps list. Each is a one-day test addition with high
  payoff.
