# Changelog

All notable changes to F-Pulse OSS are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

API stability policy: see `docs/api.md`. The HTTP API surface under `/api/*`
follows additive-only evolution within a major version; breaking changes
require a major bump and a deprecation window of at least one minor.

---

## [Unreleased]

## [1.0.0] — 2026-07-20

### Fixed — 2026-07-08 — scheduled-run step-output capture

**Scheduled runs now capture per-step output samples.** Opening a scheduled
execution in Executions → Lineage and clicking a node showed *"No output
captured for step …"* even though the run succeeded, so the Output / Table /
Schema / JSON drawer was empty. Scheduled runs built the pipeline executor
without the run context the manual **Run** path passes — the step-output store
(via `app_state`) and a `run_id` bound to the execution record — so capture was
silently skipped, and even when written it was keyed under an id the drawer
never queried. Scheduled runs and **Run now** now capture step outputs
identically to manual runs. Code-only fix (no schema change), so existing data
is untouched; scheduled executions recorded *before* the fix stay empty (their
samples were never written) — re-run, or the next schedule fire, captures
normally.

### Fixed / Added — 2026-06-18 — SQL Server types, AI cost accuracy, row-count integrity

**SQL Server `DATETIMEOFFSET` / `TIME` columns now read correctly.**
Reading a table with a `DATETIMEOFFSET` (or `TIME`) column failed with
`ODBC SQL type -155 is not yet supported` because pyodbc can't decode those
extended types on its own. F-Pulse now registers ODBC output converters on
every MSSQL read (both the Database Source and the JDBC/warehouse source), so
those columns come back as native timezone-aware datetimes / times — the same
shape Postgres and MySQL already return.
- **Self-serve recovery (no upgrade needed):** if a future, still-unhandled
  ODBC type is hit, the error is now actionable — it names the column and tells
  you to switch the Source to **Query mode** and cast it, e.g.
  `CONVERT(varchar(max), [the_column]) AS [the_column]` — instead of a cryptic
  driver code.

**AI cost estimate is no longer overstated for cheap models.** The Copilot's
per-reply cost was priced on the provider tier alone, so e.g. `gpt-4o-mini`
was billed at the OpenAI GPT-4o rate (~16× too high). Cost now resolves the
per-model rate (tolerant of dated / namespaced ids like `gpt-4o-mini-2024-07-18`
or `openai/gpt-4o-mini`).

**Fewer tokens per Copilot turn.** Tool schemas are now narrowed per request on
cloud providers too (previously local-only), trimming the long tail of
irrelevant tools re-sent on every agent step. Env-overridable
(`FPULSE_TOOL_SELECTOR`).

**New Steward detector — row-count integrity (`ROW_COUNT_DELTA`).** After a
full run, a step whose contract is strictly 1:1 (derived column, rename, sort,
typecast, window, schema-map, embed, generic transform) that silently changed
its row count now raises a node-level finding. Observe-only; conservative
(cardinality-changing nodes like filter/join/aggregate are never flagged).
Enforces the "safe optimization" rule in `docs/abstraction-boundary.md`.

### Added / Changed — 2026-06-17 — AI key ↔ Credentials, and Needs Attention clarity

Three operator-facing changes, all driven by direct user feedback.

**AI provider key can now live in the central Credentials store.**
Previously an AI provider's API key was only enterable inline on
*Insights → AI Provider*, in a store separate from every other secret.
Now the key has one governed home:
- **New `AI Provider` credential category** on the Credentials page
  (alongside Database / Cloud / API / …). Picking it on a fresh
  credential pre-seeds the right fields (`provider`, `api_key`,
  `base_url`), so an AI key gets the same governance as any other
  secret — encryption at rest, expiry, vault source, audit, and
  "used by".
- **"Use a saved credential" option** on the AI Provider form. A
  segmented toggle switches the API-key field between *Enter key inline*
  and *Use a saved credential* (a picker over existing credentials,
  AI-Provider-tagged ones first). The key is resolved from the
  credential at request time and is **never copied** into the AI config.
- **One source of truth.** Choosing a credential clears the inline key
  server-side; typing a fresh inline key clears the credential
  reference. The config API still returns only `has_key` +
  `credential_id` — never the secret.
- Schema **v31 → v32**: additive `credential_id` column on both
  `user_ai_config` and `workspace_ai_config` (fresh-install + idempotent
  upgrade ALTER + self-heal). Resolver injected in `main.py` so the AI
  config store stays decoupled from the credential store. Works for both
  per-user (Free/OSS) and per-workspace (Plus) configs.

**Dashboard → Needs Attention is now current-state, timestamped, and
clearable.**
- **Current-state semantics** (earlier 2026-06-17 fix): a pipeline
  appears only when its **most recent** run failed. A pipeline that
  failed and then ran clean drops off automatically on the next refresh.
  This is deliberately distinct from the 24h "failures" KPI, which is a
  time-window count.
- **Timestamp on every row** — shows *when it last failed*
  (relative, e.g. "2h ago"; exact time on hover) and a `×N` consecutive-
  failure streak badge when it has failed more than once.
- **Clear (acknowledge).** Each row has a Clear (×) action, plus a
  "Clear all" footer. Clearing hides that failure; it **reappears** if
  the pipeline fails again (new run) and clears for good once it runs
  clean. Acknowledgements are per-browser (`localStorage`), self-prune
  when no longer current, and the dashboard headline reconciles with the
  card so the two never disagree.
- Rows still deep-link to the **actual failed run** (`#executions/<id>`:
  error + failed step + diagnosis), not a filtered list.

### Added / Changed — 2026-06-08 — external-review follow-ups

Acting on an external validation review. Most of the review's
recommendations were already shipped (schema-drift, connector-health,
threshold volume/freshness checks, connector-certification honesty,
lineage, no inflated concurrency claims); these three were the genuine
remaining gaps:

- **Steward `foreseer` — automatic volume-anomaly detection
  (`VOLUME_ANOMALY`, DATA level).** First cut of the v1.3 `foreseer`
  sub-agent. Unlike the existing `row_count_min/max` *threshold* checks,
  this learns each source's normal volume from history (median + MAD
  robust baseline, modified z-score) and flags drops/spikes against the
  source's **own** baseline — Hard Rule 6 (baseline variance, never
  absolute thresholds). Reuses the existing `CostEvent` log
  (`rows_read` per source), so no new ingestion surface; recomputed each
  scan. Valid-empty + material-change guards keep it quiet on sparse /
  tiny sources. New `backend/fpulse/steward/foreseer.py` +
  `tests/test_foreseer_volume_anomaly.py` (14 tests). Wired into the
  scan path; rendered with a dedicated baseline→current evidence chip in
  `StewardBadge`.
- **CI: parallel test execution (`pytest-xdist`).** The fast-gate suite
  ran ~12 min serially (past a typical CI window). `conftest.py` already
  isolates every test in its own data dir, so the suite is xdist-safe —
  verified (2846 passed / 0 failed under 14 workers, ~68 s). Added
  `pytest-xdist` to `requirements-dev.txt` and switched
  `ci.yml`'s `backend-pytest-fast-gate` to `-n auto` with a 20-min
  hang ceiling.
- **Frontend bundle split.** `vite.config.ts` had no `manualChunks`, so
  React + the ReactFlow canvas collapsed into one ~756 KB entry chunk.
  Split `vendor-react` / `vendor-flow` into long-cached chunks; the
  entry chunk dropped to ~377 KB and no chunk trips the size warning.

### Added — 2026-06-08 — 1.2 reliability foundations (lineage / executor / backfill)

Audited design-first pass + foundation implementation across the three
reviewer-flagged areas. Three design docs landed first
(`docs/design/{lineage,executor-maturity,backfill-ux}-1.2.md`), each
auditing what already exists vs the real gap before any code. Key audit
finding: most reviewer items were already implemented — the real gaps
were narrower and more specific than "shallow / immature / missing".

Every item below ships a **tested foundation**. Load-bearing
integrations that touch the executor hot loop or live DB connections
are explicitly deferred to focused `.1` sub-milestones (noted per item)
so they can land with live integration tests, not blind edits.

**Lineage**
- **L1** — runtime lineage events: `lineage_step_runs` table +
  `LineageStore.record_step_run / get_runtime_lineage /
  get_runs_for_workflow` + `GET /api/lineage/runs/{run_id}` +
  `GET /api/lineage/workflow/{id}/runs`.
- **L1.1** — `ExecutionContext.emit_lineage_step_run()` + executor
  success-path wire-in (best-effort; never fails a run).
- **L2** — OpenLineage 1.0-5 formatter (`to_openlineage_run_event`) +
  append-only JSONL exporter, env-var configurable namespace / producer
  / column-redaction.
- **L2.1** — `OpenLineageHTTPExporter`: POST RunEvents to Marquez /
  DataHub over stdlib urllib, bounded retry on 5xx / 429 / network
  errors, never raises (lineage is observational). *Deferred L2.2:
  executor auto-export-on-completion hook.*
- **L3** — output-to-consumer self-attestation: `lineage_consumers`
  table + `POST/GET/DELETE /api/lineage/consumers` + `_overview`.
  Honest protocol — registers only consumers that opt in; Snowflake
  query-log auto-discovery is Plus (L4).

**Executor maturity**
- **E1** — `FailureClass` taxonomy (transient / dependency /
  data_quality / user_input / fatal / unknown) + `classify_error()` +
  per-connector classifier hook + `retry_advisable()` + rollup helper.
- **E1.1** — `StepRunResult.failure_class` + executor stamps it on
  every failed step.
- **E2** — `RetryPolicy` model + `should_retry()` / `backoff_for()` /
  `resolve_workflow_policy()`. Disabled by default (current behaviour
  preserved).
- **E2.1** — executor's per-step retry loop consults the policy before
  scheduling a retry, short-circuiting failures whose class isn't
  retryable. Optional `Workflow.retry_policy` IR field.
- **E3** — cancellation foundation: `CancellationToken` (cancel fires
  registered driver-level cancel callbacks) + `RunCancelled` + per-run
  registry + `bind_stop_event()` bridge to the execution_manager's
  existing cooperative Event. *Deferred E3.1: executor step-boundary
  checks + per-connector driver `.cancel()` registration.*

**Backfill correctness**
- **B1** — `lookback_seconds` on incremental sync + `apply_lookback()`
  helper (int / float / ISO-8601 / opaque cursors) to catch
  late-arriving data.
- **B1.1** — `db_source` applies the lookback to the stored cursor
  before the incremental SELECT.
- **B2** — `merge` write mode + `merge_key` + `tombstone_column` fields
  on the warehouse sink. Audit finding: per-dialect MERGE SQL already
  existed in `bulk_load/dialects` (postgres ON CONFLICT, mssql /
  snowflake MERGE) keyed on `primary_key`; the only gap was the UX to
  select it. *Deferred B2.1: execute() wiring `merge_key →
  BulkLoadRequest.primary_key`.*
- **B3** — resume-from-window: `first_unfinished_window_index()` +
  `from_window` on the orchestrator + `POST
  /api/executions/backfill/{id}/resume` (auto-detects first
  non-successful window; refuses 409 on running / succeeded).
- **B4** — soft-delete propagation foundation: `tombstone_column` +
  `partition_tombstones()` / `extract_tombstone_keys()`. *Deferred
  B4.1: per-dialect DELETE codegen in the sink.*

**Frontend**
- `FailureClassBadge` on the run-detail step rows (ExecutionsPage),
  distinct palette from the existing ErrorTypeBadge, with per-class
  retry-policy hint tooltips.

All foundations are covered by dedicated unit + integration tests
(≈260 new tests across the session), with source-grep regression
guards pinning each executor / sink wire-in so the integration points
can't be silently removed.

### Added — 2026-06-06 (eighth pass) — UX polish, real-data proof, gap-closure pack

Final push covering everything between R5 and V1-Gaps. Five logically
distinct workstreams shipped in one swing because reviewer feedback
converged on them simultaneously.

**R5 — Scan-progress feedback** (per "difficult to see any progress"
user report):
- Re-scan button now shows a spinning icon + violet-tinted background
  + cursor:wait while running.
- Persistent "Last scanned at 14:23:08 · 3 findings · auto every 60s"
  footer line under the header so users always know how fresh the
  current view is.
- Ephemeral green "✓ Re-scan complete in 47 ms — 3 findings" flash
  appears for 4s after manual re-scan.

**R6 — Live visual proof + real-workspace detector fix**:
- Found and fixed a CRITICAL bug: detector was hard-coded for React
  Flow node format (`node.data.stepType`), but F-Pulse actually stores
  workflows in step format (`step.type` top-level). Real production
  workflows were producing ZERO findings. New `_step_type_and_params()`
  helper handles both shapes.
- 2 regression tests pin the fix
  (`test_detector_handles_fpulse_step_format`,
  `test_detector_handles_mixed_format_workspaces`).
- New `backend/scripts/steward_dryrun_live_data.py` runs the shipped
  detector against the user's REAL workflow database; on the bundled
  sample-DB it detected 3 actual duplicate-source findings between
  5 of their 18 existing pipelines (Aggregation Report ↔ Simple ETL;
  Sales Pipeline ↔ Siva; First Pipeline (copy) ↔ Siva).
- HTML render at `docs/steward/PROOF-2026-06-06/08-live-findings-render.html`
  + Memory-tab verification render at `10-memory-tab-rendered.html`
  (Vite-serveable at `http://localhost:5174/steward-proof.html`).
- New `steward_memory_verify.py` exercises every Memory-tab data
  source against shipped code paths.

**R7 — Dismiss-weighted escalation + reason sanitizer + visible
confidence + density polish** (per 4-reviewer convergent feedback):
- Critical alert-fatigue fix: `persistent_occurrences()` now walks
  the journal in append order and RESETS the per-signature scan set
  on dismiss. A previously-dismissed pattern that re-emerges no longer
  inherits its old N-scan count and immediately escalates to P1.
  Pinned by `test_dismiss_resets_persistent_occurrence_counter`.
- New `_sanitize_reason()` strips 5 secret patterns from dismiss
  reasons before journal write: AWS keys (AKIA/ASIA), Bearer tokens,
  `password=…` / `secret=…` k/v pairs, `user:password@host` URI
  credentials, private-IP ranges. 4 tests pin both redaction AND
  round-trip integrity of normal text.
- Confidence / level chips rendered on every Finding card (high =
  green, medium = amber, low = slate). Tooltip surfaces the
  `confidence_score`, `evidence_count`, and `baseline_window` fields.
- Findings card density tightened (py-4 → py-2.5), footer redesigned
  with shield icon + violet accent ("Read-only. Steward never modifies
  pipelines."), active tab state beefier (border-b-2 → border-b-[3px]
  + violet tint).
- StewardBadge.tsx settings UI: added the missing
  `escalate_min_hours_since_first` control (was 7 in UI vs 8 in
  backend — now matches).

**V1 Scenario Pack** (per Reviewer 1's 12-item validation pack):
- New `backend/scripts/steward_scenario_pack_v1.py` — 12 named
  Given/When/Then scenarios covering everything that ships in 1.1.
- Latest run: **12 / 12 passed** (artifact at
  `docs/steward/PROOF-2026-06-06/13-scenario-pack-v1.txt`).
- Coverage by level: architecture 8/8, data 2/2, pipeline 2/2.

**V1-Gaps — all 8 known gaps closed**:
- G1 source-without-identity: `_source_signature` tightened to
  require at least one object-identity field (`table`/`file_path`/
  `query`/`url`/`object`/`endpoint`). `connector_type` alone no
  longer hashes.
- G2 P1-no-double-escalate (test + body annotation count guard).
- G3 re-resolved REBOUND tracks LATEST resolve timestamp.
- G4 lesson auto-ages to STALE past `validity_days`.
- G5 lesson search with empty source filter searches all approved.
- G6 `notify_on_finding=false` end-to-end zero notifications.
- G7 cross-workspace dismiss isolation (Plus-tier safety).
- G8 router error path returns JSON, never HTML (FastAPI TestClient
  pinning).

**UI polish — outer-line + flowing-gradient border** (per "outer line
to show the difference" feedback):
- Dropdown panel wrapper now has a 3px-padded animated gradient
  border (violet → indigo → fuchsia → indigo → violet, 8s ease-in-out
  infinite cycle). Triple-layer shadow stack (ambient lift + violet
  glow + crisp white rim) lifts the panel off the cream page bg.

**Downstream sync** (Help page + Reports inventory + doc catalog):
- HelpPage.tsx "How to configure" updated with the 8th setting
  (`escalate_min_hours_since_first`) + "8 settings (6 core + 2
  notification)" total.
- New "Safety guarantees you can rely on" Help section covering
  read-only, no-alert-fatigue, time-clamped escalation,
  dismiss-reason sanitization, gated learning, per-workspace
  isolation, corrupt-journal resilience.
- Inventory `steward_summary` extended with `by_level`, `by_status`,
  `by_confidence` rollups (matches the post-R4 model).
- Doc catalog entry for `steward/validation-scenarios.md`.

**Test totals**: **75 unit tests + 12 named scenarios** — all green.
Test run wall time: 1.44s + ~1s.

---

### Added — 2026-06-05 (seventh pass) — 7th level + confidence richness + 8-state lifecycle + factual fixes

Direct response to 8 independent inventory reviews. Three real
product additions + factual corrections + soft-claim cleanup.

**Real product additions (multi-reviewer agreement)**:

- **7th observability level: `ARCHITECTURE`** (Reviews 3, 6). Structural
  / design-level findings — duplicate extraction, redundant transfer,
  lineage cascade — moved out of CONNECTOR / COST into a dedicated
  level. "Two pipelines reading the same table" is a design decision,
  not a transport problem; flagging it architecturally is more
  actionable. `duplicate_source`, `duplicate_pipeline`,
  `redundant_transfer`, `lineage_cascade` re-mapped to
  `FindingLevel.ARCHITECTURE`. Pinned by
  `test_architecture_level_groups_structural_kinds`.

- **Confidence richness on every finding** (Reviews 3, 6). Four new
  fields on `StewardFinding`: `confidence` ("low"/"medium"/"high"),
  `confidence_score` (0.0-1.0), `evidence_count`, `baseline_window`
  (e.g. "30_days", "instantaneous"). Without these the Steward sounds
  equally certain about a deterministic duplicate check (1.0
  certainty) and a 4-day-history statistical anomaly (unknown
  calibration). Pinned by `test_finding_carries_confidence_richness`.

- **8-state finding lifecycle** (Reviews 3, 6). Was 5 states
  (open/dismissed/resolved/rebounded/stale). Added: `acknowledged`
  ("I saw it, didn't act yet"), `suppressed` (silenced for a
  maintenance window), `expired` (auto-aged beyond stale + grace).
  Pinned by `test_expanded_finding_status_values_exist`.

**Factual fixes** (Reviews 1, 3, 5, 6, 8):

- Standardized **30 finding kinds** in all docs (was inconsistent
  between "26" and "30" in different sections).
- Standardized **7 levels** (was inconsistent between "6" and "7").
- Standardized **8 settings** — added `escalate_min_hours_since_first`
  control to `StewardBadge.tsx` Settings tab so the UI count matches
  the backend count.

**Soft-claim cleanup** (Reviews 1, 4, 8):

- README "Why F-Pulse" bullet rewritten to clearly separate "active
  in 1.1" from "contract-ready for 1.2-2.0". No more implicit claim
  that schema drift / empty output / etc are detected today.
- `overview.md` opener rewritten: "In 1.1 today, it detects duplicate
  sources and duplicate pipelines [...]. Its multi-level contract is
  ready for future specialist modules covering [...] which land
  progressively from 1.2 onwards."
- `memory-layer.md` 8-step flow framing made honest: search API today,
  auto-invocation in 1.2 (Incident Analyst).
- `vs-airbyte.md` + `vs-talend.md` competitor rows softened on the
  Memory Layer claim.

**Implementation guidance for Rules 6 + 7** (Reviews 2, 7):

Added required-implementation sections to `architecture.md`:

- Rule 6: future detectors MUST use seasonality-aware categorical
  windows (day_of_week + hour_of_day), not naive rolling averages.
  `confidence_score < 0.5` if bucket has < 12 samples. The
  `baseline_window` field on emitted findings MUST name the bucket
  strategy used.
- Rule 7: schema-drift detector MUST use a sliding time-clamped
  accumulator (default 60s window) to bundle mutations across N+
  tables in a maintenance window into ONE finding. Prevents the
  "50 alerts in 30 seconds" disaster during planned migrations.

**Tests**: 4 new tests (59 total, all green):
- `test_architecture_level_groups_structural_kinds`
- `test_seven_levels_exist`
- `test_finding_carries_confidence_richness`
- `test_expanded_finding_status_values_exist`

---

### Added — 2026-06-05 (sixth pass) — Multi-level observability contract

In direct response to 4-reviewer convergence on Steward scope
expansion. The Steward must observe at multiple levels, not just
duplicate-detection. The CONTRACT for that lands in 1.1 even though
not every detector ships in 1.1 — the UI / notification bridge /
memory / suppression layers must NOT need re-shaping when Sentinel
(1.2), Foreseer (1.3), Governor (1.4) ship their respective detectors.

**Taxonomy expansion** (`backend/fpulse/steward/models.py`):

- New **`FindingLevel`** enum: `PIPELINE / NODE / CONNECTOR / DATA /
  GOVERNANCE / COST`. Mirrors industry observability practice (Monte
  Carlo 5 pillars, DataHub quality dimensions).
- New `level` field on `StewardFinding` — every finding now declares
  its observability layer so the UI can group/filter, and suppression
  rules can target a specific boundary.
- New **`KIND_TO_LEVEL`** mapping table + `level_for_kind()` helper.
- **`FindingKind` enum expanded from 6 to 26 values** covering the
  full multi-level taxonomy:
  - Pipeline level (4): `DUPLICATE_PIPELINE`, `SLA_BREACH`,
    `PARTIAL_OUTPUT`, `RETRY_STORM`
  - Node level (5): `EMPTY_OUTPUT`, `JOIN_EXPLOSION`, `JOIN_COLLAPSE`,
    `FILTER_DROPPED_ALL`, `CAST_FAILURE`
  - Connector level (5): `DUPLICATE_SOURCE`, `CONNECTOR_AUTH_FAILURE`,
    `CONNECTOR_RATE_LIMIT`, `CONNECTOR_UNREACHABLE`,
    `CREDENTIAL_NEAR_EXPIRY`
  - Data level (6): `SCHEMA_DRIFT`, `NULL_SPIKE`,
    `DUPLICATE_KEY_SPIKE`, `VOLUME_ANOMALY`, `FRESHNESS_MISS`,
    `PARTITION_MISSING`
  - Governance level (4): `PII_LEAK`, `CREDENTIAL_SPRAWL`,
    `ENV_CROSSING`, `UNAPPROVED_DESTINATION`
  - Cost level (4): `COST_DRIFT`, `REDUNDANT_TRANSFER`,
    `WAREHOUSE_WASTE`, `COST_RECOMMENDATION`
  - Cross-cutting (2): `FAILURE_RCA`, `LINEAGE_CASCADE`

**Honest scope**: 1.1 ships `DUPLICATE_SOURCE` and
`DUPLICATE_PIPELINE` detectors only. The other 24 kinds are
CONTRACT-ONLY in 1.1 — the enum value exists so future modules slot
in cleanly, but no detector emits them yet. Per-kind ship release
documented in the model docstring + roadmap.

**Two new hard architectural rules** (Rules 6 + 7 — addressing the
critical traps Review 4 flagged):

- **Rule 6: Historical Baseline Variance, not absolute thresholds.**
  Volume / null-rate / freshness alerts MUST compare against an
  observed per-signature baseline. Prevents the "valid empty table"
  fallacy where a daily-disputes pipeline that returns 0 rows on
  quiet days pages out every quiet day.
- **Rule 7: Intentional-change suppression.** Schema/topology
  mutations co-occurring across N+ entities within a maintenance
  window are rolled into a single baseline-update card, not N
  separate findings. Prevents "schema drift fatigue" when a planned
  migration touches many tables at once.

Both rules pinned in `backend/fpulse/steward/__init__.py` (the
package-docstring source of truth) and in `docs/steward/architecture.md`
§3 ("The seven hard rules" — was five).

**Tests**: 3 new tests pinning the contract. 55 total, all green:
- `test_kind_level_mapping_is_complete` — every enum value has a
  level mapping (catches "added a new kind, forgot to map it")
- `test_level_for_kind_returns_expected_layer` — spot-checks one
  kind per level
- `test_archeologist_findings_carry_correct_level` — shipped
  detector correctly wires the level field

**Doc updates**:
- `overview.md` opening rewritten with the "pipeline structure, node
  behaviour, connector health, and output quality" framing + a new
  "six observability levels" table.
- `architecture.md` §3 expanded from five rules to seven.
- Polished one-liner updated to reflect the broader scope.

---

### Added — 2026-06-05 (fifth pass) — Steward refinements per architectural review

Addressing 4 sequential review blocks. Three real engineering fixes
+ doc copy + 4-module roadmap expansion.

**Engineering fixes (R1)**:

- **Time-clamped severity escalation** (Review 1C — bug). A 60-second
  cron pipeline hitting the 5-emit threshold in 5 minutes would have
  paged-out to P1. New setting `escalate_min_hours_since_first`
  (default 24h, range 0-720) requires the FIRST emit to be at least
  N hours old before severity bumps. Combined with the count
  threshold, an issue has to persist across at least one operator
  workday before escalation. Pinned by
  `test_time_clamp_blocks_fast_escalation`.

- **`REBOUNDED` promoted to a first-class `FindingStatus` enum**
  (Review 3.4). Previously only a title prefix `(rebounded)`. Now
  an explicit state alongside `open / dismissed / resolved / stale`.
  `apply_learning()` sets `status = REBOUNDED` and stamps
  `evidence.previously_resolved_at` so the UI can render a regression
  chip with the prior-resolve timestamp. Title prefix kept for
  backward-compat with existing UI label code. Pinned by
  `test_rebound_promotes_status_to_rebounded`.

- **Workspace-prefixed signatures** (Review 1B). Source signatures
  now optionally include `workspace_id` as the first hash component.
  Defends Plus multi-workspace deployments against the case where
  two tenants import the same `connection_id` — they will produce
  different signatures and never cross-pollinate findings or
  notifications. Default `None` preserves single-workspace behaviour
  for OSS Free. Pinned by `test_workspace_prefix_changes_signature`.

- 52 tests, all green. 4 new tests added for the above.

**Doc copy refinements (R2)**:

- `overview.md` opener rewritten with sharper boundary language:
  "advisor, not actor — recommends, annotates, and escalates; never
  mutates pipeline state; never runs fixes; never changes execution
  policy on its own."
- New "Finding lifecycle (the incident states)" section documenting
  the 5 states explicitly.
- New "Noise control" section explaining the three guards that
  prevent alert fatigue (dedup across scans, time-clamped escalation,
  separate notification min-severity).
- Honest "Note on automation level today" callout: the Memory Layer
  search API is shipped in 1.1, but automatic pipeline-fails ->
  search-lessons -> surface-fix is **Incident Analyst** in 1.2 (per
  Review 4 — was implied to be working in 1.1, corrected).
- New polished one-liner at the end of overview.md.

**Roadmap expansion — 4 new specialist modules** (Review 2):

The architecture roadmap now lists 15 specialist modules across 5
release planes. Additions:

- **Cost Steward (1.3)** — cost-drift detection ("today's load was
  1766% above 30-day average") + warehouse-warm waste.
- **Architecture Steward (1.3)** — cross-warehouse storage
  duplication ("same Oracle source extracted twice into separate
  lakehouses, est. 1.4 TB waste").
- **Knowledge Steward (1.4)** — learn SUCCESSFUL patterns and
  recommend them (incremental watermark + partition pushdown +
  batch_size=5000).
- **Governor (1.4)** — PII detection + credential sprawl +
  cross-project service-account use.

Plus three internal renames for clarity:
- "Sub-agents" -> "specialist modules" throughout (Review 2 + Review 3
  both flagged the autonomous-LLM-agent confusion).
- Renamed "Autopsy" -> **"Incident Analyst"** in the roadmap (clearer
  what it actually does).
- Added **"Sentinel"** (live pipeline health, 1.2) and **"Advisor"**
  (top-level UI presenter, 2.0).

---

### Added — 2026-06-05 (fourth pass) — F-Pulse Memory Layer + doc rewrite

In response to a senior architectural review (block 1: "make memory a
product feature"; block 2: section-by-section critique with
mitigations; block 3: honest validation; block 4: proposed
architecture draft). Three workstreams:

**M1 — F-Pulse Memory Layer (the new feature)**:

- New module `backend/fpulse/steward/lessons.py` with `MemoryLesson`,
  `LessonStore`, `LessonType` (10 categories), `LessonStatus`
  (PROPOSED / APPROVED / REJECTED / STALE), `LessonConfidence` (LOW /
  MEDIUM / HIGH), `EvidenceRef`.
- Per-workspace storage at `<data_dir>/steward/<ws>/lessons/` with
  TWO files per lesson — `<id>.yaml` (human-reviewable, hand-editable,
  PR-friendly) and `<id>.json` (machine-read by the API). Both written
  together inside a file lock; they cannot drift.
- The 8-step failure → lesson workflow: read error → search lessons
  by source + error substring → check source quirks → compare schema
  drift → surface highest-confidence match → recommend `approved_fix`
  verbatim → ask operator approval → on resolution, revalidate
  (occurrence_count++, may promote confidence LOW→MEDIUM→HIGH).
- **Gated learning** (architectural Rule 3): PROPOSED lessons are
  inert. They do NOT appear in `search_for_failure()` results until
  a human calls `/approve`. Pinned by
  `test_search_for_failure_excludes_proposed`.
- Auto-staling: APPROVED lessons untouched for `validity_days` (default
  180) transition to STALE; `revalidate()` revives them.
- 9 new HTTP endpoints on the existing Steward router: `GET /lessons`,
  `GET /lessons/stats`, `GET /lessons/{id}`, `POST /lessons` (propose),
  `POST /lessons/{id}/approve`, `POST /lessons/{id}/reject`,
  `POST /lessons/{id}/revalidate`, `POST /lessons/search`,
  `DELETE /lessons/{id}`. Steward router now exposes 17 endpoints
  total.
- 12 new tests under `TestMemoryLayerLessons`. Suite total: 48,
  all green.

**M2 — Doc rewrite (per reviewer concerns)**:

- New `docs/steward/memory-layer.md` — user-facing description of
  the Memory Layer with the YAML example, lifecycle diagram, 8-step
  flow, comparison to generic AI memory.
- New `docs/steward/positioning.md` — 60-second pitch, three
  differentiators, OSS-vs-Plus horizontal split, TL;DR for buyers.
  Moved here from `architecture.md` to remove the audience mismatch
  the reviewer flagged.
- `docs/steward/architecture.md` REWRITTEN to address every concern:
  - Replaced Unicode box-drawing + em-dashes with ASCII (reviewer
    flagged encoding-display risk on Windows consoles).
  - Softened the absolute "every other open-source orchestrator..."
    claim to "no other OSS orchestrator addresses them together as a
    first-class in-product layer."
  - Renamed "sub-agents" to "specialist modules" throughout (reviewer:
    "sub-agent" suggests autonomous LLM agents — these are
    deterministic analyzers).
  - New Appendix A explicitly listing every reviewer concern with
    its mitigation (signature collision, JSONL/SQLite drift, async
    dispatch, mock-dependency tests, IO bottleneck, perf staleness).
  - Verified OSS vs Plus claims against `docs/editions.md`.
- `docs/steward/overview.md` linked to all three companion docs.

**M3 — Top-level callouts**:

- `README.md` — Memory Layer added to the "Why F-Pulse" bullet as
  part of the Steward pitch, with explicit link to memory-layer.md.
- `docs/editions.md` — Memory Layer listed as a distinct OSS feature
  alongside the Steward.
- `docs/vs-airbyte.md` + `docs/vs-talend.md` — new row in side-by-side
  table: "Durable team-knowledge surface."
- `docs/customer-faq.md` — Q9 expanded with the Memory Layer privacy
  story (typed lessons, hand-editable YAML, gated learning).
- `docs/api.md` — new "F-Pulse Memory Layer (Lessons)" section with
  all 9 lesson endpoints + sample bodies + enum values.
- `backend/fpulse/api/reports.py` doc catalog gained 2 entries
  (`steward/memory-layer.md`, `steward/positioning.md`) so the
  in-app Documentation tab surfaces them under Core Concepts.

---

### Added — 2026-06-05 (third pass) — Steward → notification bell bridge

Third Steward pass: route findings into the existing in-app
notification system so the bell pings on new + escalated findings,
not just the Steward eye icon.

**New module** `backend/fpulse/steward/notifier.py`:
- `emit_steward_notifications(...)` — at-most-one notification per
  (user, finding_id, severity, rebound-state) tuple. Re-scans of
  unchanged findings produce ZERO new notifications.
- `mark_finding_notifications_read(...)` — called from
  dismiss/resolve handlers so the bell badge clears when the user
  acknowledges a finding (no stale unread count for triaged issues).
- All errors silently logged + swallowed — a notification persistence
  failure must not break the scan response.

**API wiring**:
- `_run_scan()` calls the notifier after recording emits.
- `POST /findings/{id}/dismiss` and `/resolve` now also mark related
  notifications read and return `notifications_marked_read` in the
  response body.

**Two new settings** (`StewardSettings`):
- `notify_on_finding: bool = True` — master toggle for bell integration.
- `notify_min_severity: "p2"` (default) — bell-only threshold,
  separate from `min_severity` which controls the eye-icon dropdown.
  Default P2 means info-only P3 findings stay in the badge without
  spamming the bell.

**Notification types** (new):
- `steward_finding` — new finding at or above `notify_min_severity`.
  Renders with the violet eye icon on the Notifications page.
- `steward_finding_escalated` — finding bumped to P1 via the
  learning layer. Renders with a red warning triangle.

**Deep-linking**:
- `notificationHref.ts` — clicking a `link_type: "steward"`
  notification navigates to `#dashboard` and dispatches
  `fpulse:steward-open` so the StewardBadge auto-opens its dropdown
  on the Findings tab. User lands in context, not on a generic page.

**UI** (`StewardBadge.tsx`):
- Settings tab gained a "Notification bell" section with the two new
  toggles. The min-severity dropdown is disabled when the master
  toggle is off (no surprise behaviour when notifications are paused).
- Listens for `fpulse:steward-open` and auto-opens itself + refreshes.

**Tests**: 8 new tests under `TestNotificationBridge` (36 total in
`test_steward_archeologist.py`; all green). Pins the critical de-dup
invariants:
- First emit creates one notification per user.
- Re-scans at same severity create ZERO new notifications.
- Severity escalation (P2 → P1) creates a NEW notification (severity
  changed = new event).
- Rebound annotation creates a NEW notification (rebound-state
  changed = new event).
- Per-user dedup is independent (one user marking read doesn't stop
  another user from getting the same finding).
- `min_severity` filter actually filters (P2 default skips P3).
- Dismiss marks related notifications read.
- Missing notification store short-circuits cleanly (embedded build).

---

### Added — 2026-06-05 (later) — Steward: learning layer + settings + Help docs

Second pass on the Steward. The 09:00 ship landed *detection*; this
pass adds *learning* — the actual "learn from mistakes" loop the user
asked for proof of. Plus settings + Help-page coverage + a Memory
tab showing the audit trail.

**Learning layer** (`backend/fpulse/steward/memory.py`):
- Append-only JSONL journal at `<data_dir>/steward/<ws>/memory.jsonl`
  records every emit, dismiss, and resolve.
- **Persistent occurrence counter** — counts the distinct *scans* a
  signature has appeared in, not the per-scan workflow count. Re-running
  the same scan twice in 30 s doesn't inflate it.
- **Severity escalation** — when persistent occurrences cross the
  `escalate_after_n_occurrences` threshold (default 5), the next scan
  bumps severity one step (P3→P2→P1) and adds an explanation line to
  the finding body. What the user keeps ignoring gets louder.
- **Rebound detection** — if a signature was resolved historically and
  re-appears, the new finding is prefixed `(rebounded)` and the body
  shows when it was last resolved. Catches "teammate re-introduced the
  dup" regressions.
- **Dismiss-with-reason** — UI prompts for optional free-text reason
  on dismiss; reason is logged in memory so the future Curator can
  mine the patterns of why findings get dismissed.

**Per-workspace settings** (`backend/fpulse/steward/settings.py`):
- `enabled` (master kill-switch, default true), `min_severity`
  (default p3), `scan_on_save` (default true), `auto_stale_days`
  (default 30), `escalate_after_n_occurrences` (default 5).
- Persisted to `<data_dir>/steward/<ws>/settings.json`. Hand-editable.
- Corrupt settings file falls back to defaults rather than crashing
  the scan path (pinned by `test_corrupt_file_falls_back_to_defaults`).

**HTTP API additions**: `GET /api/steward/settings`,
`PUT /api/steward/settings`, `GET /api/steward/memory`,
`GET /api/steward/memory/stats`. Dismiss now accepts optional
`{"reason": "..."}` body.

**UI**: `StewardBadge` gained a 3-tab strip — Findings / Memory /
Settings. Memory tab surfaces the persistent occurrence count card
(signatures shown in red once they cross the escalation threshold)
plus a live event stream. Settings tab has toggles + numeric inputs
that PUT to the API on change. Dismiss button now prompts for an
optional intent reason.

**`scan_on_save` plumbing**: `SaveDialog` dispatches
`fpulse:steward-refresh` after every workflow save; the Badge listens
and re-polls immediately, so a duplicate created right now appears
without waiting for the 60 s timer.

**Help page**: new "F-Pulse Steward" category in How-to with 7 guides
— What is the Steward, what it detects, **how it learns**, how to use
Findings, how to configure, how to see the proof in Memory, future
sub-agents roadmap.

**Tests**: 10 new tests under `TestLearningLayer` + `TestSettings`
(27 total in `test_steward_archeologist.py`; all green). Pins
persistent-occurrence counting, escalation threshold behaviour,
rebound annotation, dismiss-with-reason audit trail, settings
round-trip + corrupt-file fallback.

**Validation script**: `backend/scripts/validate_steward.py` exercises
the full API end-to-end against 9 fixture workflows (including a
known-good DR replication pattern that gets dismissed-with-reason and
a layered raw→staging chain). Output preserved at
`docs/steward/validation-output.txt` and a sample journal at
`docs/steward/sample-memory.jsonl` as launch evidence.

---

### Added — 2026-06-05 — F-Pulse Steward (Archeologist sub-agent)

Headline addition for 1.1. The Steward is the OSS-tier reliability +
learning layer that watches your workflow set and flags reliability
concerns — without ever mutating user data on its own. Full design
notes in [`docs/steward/overview.md`](docs/steward/overview.md);
the five hard architectural rules are pinned in
`backend/fpulse/steward/__init__.py`.

This first release lands the **Archeologist** sub-agent:

- **Duplicate-source detection** — two or more pipelines reading the
  same logical source (same `connection_id` + same object name) into
  different destinations. Surfaces as a `duplicate_source` finding
  with a `Consolidate via Managed Table` proposed action.
- **Duplicate-pipeline detection** — two or more pipelines with the
  same source signature set AND the same sink signature set. Catches
  the "two engineers built the same flow" accident. Surfaces as a
  `duplicate_pipeline` finding with a `Compare transforms` action.
- **Lineage-based, not name-matching** — the detector avoids the
  false-positive trap where layered `raw → staging → cleansed`
  pipelines look like duplicates.
- **Suppression that sticks** — `Dismiss (intentional)` writes to a
  per-workspace suppression file (`<data_dir>/steward/<ws>/suppressions.json`)
  so DR replications, data-vault layering, and other intentional
  duplicates stop nagging on re-scan.

Backend:
- `backend/fpulse/steward/{__init__,models,archeologist}.py` — pure-code
  detection. No LLM dependency. Deterministic SHA-256[:16] signatures
  so finding IDs are stable across runs (upsert-friendly).
- `backend/fpulse/api/steward.py` — `GET /api/steward/findings`,
  `POST /api/steward/scan`, `POST /api/steward/findings/{id}/dismiss`,
  `POST /api/steward/findings/{id}/resolve`. Auth-gated.
- `backend/tests/test_steward_archeologist.py` — 17 tests covering
  positive + negative detection, suppression, deterministic IDs,
  edge cases. All green.

Frontend:
- `frontend/src/components/StewardBadge.tsx` — header surface paired
  with the notification bell. Count badge, dropdown panel listing each
  finding with Dismiss + Resolve actions, 60 s visibility-gated poll.
- `frontend/src/components/Sidebar.tsx` — mounts `<StewardBadge>` to
  the LEFT of the bell.

Docs:
- `docs/steward/overview.md` — what it is, why it's in OSS, hard rules,
  HTTP API reference, roadmap.
- `docs/editions.md` — Steward listed under "Features included" with
  the OSS-first note.
- `docs/roadmap/oss-1-1.md` — Archeologist promoted to the headline
  entry for 1.1, future sub-agents (Autopsy, Foreseer, Curator,
  Optimizer) listed with target releases.

Sub-agent roadmap (future releases — none are in this build):
Autopsy (failure RCA — 1.2), Foreseer (volume + drift anomaly — 1.3),
Curator (`EPULSE_RUNBOOK.md` distillation — 1.4), Optimizer (cost +
perf — 2.0).

### Roadmap notes (post-1.0)

Items intentionally deferred from 1.0 to a future 1.1 release are
tracked in [docs/roadmap/oss-1-1.md](docs/roadmap/oss-1-1.md).
Currently on that list:

- **OSS desktop application** — Tauri or pip-install/Streamlit pattern; full evaluation deferred (4-reviewer consensus said correctness work first, packaging second)
- **Plus license enforcement implementation** — design specced in `docs/design/plus-license-model.md`; ~10 weeks of work pending sales/legal sign-off
- **Verified-tier connector candidates** — tier system shipped; per-connector live-smoke CI work is the 1.1 path to first Verified rows
- **Connector framework SDK extraction** — turn the REST manifest framework into a stable contributor surface
- **AI-Native pipeline construction** — wire the existing AI building blocks into a single "describe-pipeline-in-English" flow
- **Streaming connectors** — Kafka / Event Hub / Kinesis / Pulsar / MQTT need a push-shaped adapter beyond the current pull/HTTP framework

None of the above are promised for 1.1 — promotion to "doing now"
depends on actual operator + sales signal after launch.

---

## [1.0.0] — 2026-06-03 — launch sprint additions

### Added — 2026-06-03 final pre-launch hardening + repo standardisation

Pre-tag sweep covering security audit findings, canvas UX rebind,
threat-model publication, and repo-hygiene baseline.

**Security audit + fixes** (`docs/security/audit-2026-06-03.md`,
`docs/threat-model.md`)

- **H1 — password hashing migrated to bcrypt cost 12.**
  `auth/models.py` previously used single-round salted SHA-256, which
  was crackable at GPU speed if the auth-store file ever leaked. Now
  uses `bcrypt.hashpw(..., gensalt(rounds=12))` (~250 ms/hash,
  ~years to brute-force). `verify_password` accepts BOTH the new
  bcrypt format AND the legacy `<salt>:<sha256>` format; on a
  successful legacy verify the login path (`api/auth.py`) re-hashes
  with bcrypt in place. **No forced password resets.** Both branches
  use `hmac.compare_digest` for timing safety.
- **H2 — SSRF guard now covers every user-supplied URL fetch.**
  Extracted the existing comprehensive `_ssrf_check_url` from
  `connectors/ai_authoring.py` to a shared module
  `backend/fpulse/security/ssrf.py`. Wired into both `urlopen` sites
  in `nodes/activities.py` (initial fetch + pagination loop). Refuses
  cloud-metadata (`169.254/16`), loopback, RFC1918, link-local,
  multicast, reserved targets, plus non-http schemes
  (`file://`, `gopher://`, etc.) and URLs with embedded credentials.
  Operators with internal API catalogs can opt in per-feature via
  `FPULSE_API_SOURCE_ALLOW_PRIVATE=1` (separate from the OpenAPI
  flag, so policies can differ per feature).
- **L1 — UI label corrected.** Settings → About / Security Posture
  said "Credentials encrypted at rest (PBKDF2)" — actual
  implementation is Fernet (AES-128-CBC + HMAC-SHA256). Relabeled
  honestly.
- **Threat model published** at `docs/threat-model.md`. STRIDE-style
  per-asset model + trust boundaries + explicit out-of-scope list +
  verification commands so auditors can `curl` claims rather than
  trust a PDF.
- **CodeQL added to CI** (`.github/workflows/codeql.yml`). Python +
  TypeScript with the `security-extended` query pack. Complements
  existing Bandit + pip-audit + npm audit + gitleaks.

**Canvas UX rebind (n8n-style separation of selection from opening)**

- **Strict single-click vs double-click semantics.** Single-click on
  a node selects (highlight only) without opening the config modal.
  Double-click opens the config modal, scrolls it to the top, and
  focuses the first editable field. Right-click → "Open Settings"
  and "Fix configuration" (error nodes) also dispatch the open event.
- **F2 keyboard shortcut now actually bound** to rename. The kbd
  hint in the context menu had been there since launch; the binding
  itself never existed.
- **Transform node admitted to multi-input.** Backend always
  registered every ancestor's relation as a named DuckDB table
  (`fpulse/nodes/transform.py`); the canvas arity guard was the only
  thing blocking. Users can now wire multiple sources directly into a
  Transform and `JOIN` across them by sanitized node-label. First
  input remains `source_table` for backward-compat.
- **Canvas node clarity pass.** Per-category left-edge accent stripe
  (blue/emerald/orange/amber/violet/purple), wider cards (150→180px),
  larger icons (28→32px), larger handles (12→14px), stronger
  selected state (3px solid border), WCAG-AA-pass subtitle contrast
  (slate-400 → slate-500). Brand icons (Postgres, Salesforce, Slack,
  etc. — ~70 connectors) replace gradient glyphs where
  `params.connector_type` is set.
- **Edge live state.** Edges turn amber + accelerate animation when
  the source node is running; thicken proportionally to row count
  on success (2-5.5px log scale); render dashed slate when skipped;
  turn red on failure.
- **Density toggle now affects nodes** in addition to edges.
  `clean` = title only · `metrics` = + subtitle · `verbose` = + param
  preview. Tooltips rewritten to reflect dual scope.

**About page + Login page + Trust page corrections**

- **Settings → About card** redesigned. Real brand mark
  (`/fpulse-logo-mark.png`) replaces the generic lightning bolt SVG.
  Tagline updated from "AI-Native Data Pipeline Builder" to
  "Single-binary, local-first data pipeline engine" (matches
  readme.md positioning). Stat tiles use honest counts:
  40 node types, 33 connectors, 27 templates, Apache 2.0.
- **Login page tagline** updated for the same reason — first
  impression now matches readme positioning.
- **Trust page connector count** explainer added. The "Total"
  headline is now labelled "Catalog (all)" with an inline paragraph
  that reconciles it against the readme's "33 visible default"
  figure (the difference is v1 legacy + hidden manifests).
- **Trust page Eval Pass Rate** now includes a context paragraph
  framing the `48/339 (14%)` number honestly: 339 is the full
  future-coverage battery, 48 are v1.0-shipped surfaces, the rest
  are pending coverage tracked in the reliability sprint.

**Page audit fixes**

- **Connections tab counter consistency.** `All N` filter label now
  uses the same scope-filtered base as `Global` / `Project`, so
  counts sum correctly and match the TOTAL stat tile.
- **Insights "Author Connector" tab** label shortened to "Author" —
  prevented the two-line wrap at common viewport widths.
- **Pool → Configuration page** clarifies that values come from
  environment variables (read-only at runtime), adds per-row
  copy-as-`export FPULSE_X=value` buttons, fixes capacity-note copy
  ("Tune the values above" implied an editor that doesn't exist),
  and links to the verified `docs/scaling.md#the-4-vertical-scaling-knobs`
  anchor.
- **Help → Shortcuts page** reflects the new node bindings (Click /
  Double-click / F2 / Right-click) and the page width is constrained
  to a 5xl (1024px) reading column so the right-aligned kbd chips
  don't sit at the far edge of the viewport.

**Repo standardisation**

- `.editorconfig` added at root (kills the LF/CRLF warning churn).
- `.github/CODEOWNERS` added (reviewer auto-routing with explicit
  rules for security-sensitive paths).
- `.github/FUNDING.yml` added (Sponsor button points at the
  F-Pulse+ commercial track).
- `backend/test_import.py` (scratch debug file) removed.
- Operator scripts moved out of `backend/` to keep the package
  surface clean: `enrol_admin.py` and `inspect_users.py` →
  `scripts/admin/`.
- Root meta-doc files renamed to UPPER_CASE convention
  (README.md, SECURITY.md, CHANGELOG.md, CODE_OF_CONDUCT.md,
  CONTRIBUTING.md, PRIVACY.md, TRADEMARK.md, CLA.md, RELEASING.md).
  GitHub auto-detects these case-insensitively so external links to
  the old lowercase URLs continue to work.

### Added — 2026-06-02 hardening + launcher + tier system

Final pre-launch sweep. Three workstreams shipped end-to-end with test
coverage: localhost-hardening for OSS local mode, a one-command
launcher (`fpulse open`), and a 5-tier connector classification
exposed via the cert matrix. 4-reviewer convergent feedback was
applied to each.

**Local-first hardening (`backend/fpulse/api/local_hardening.py`)**

- **Loopback default.** Backend binds to `127.0.0.1` by default
  across every OSS launcher path (`fpulse serve`, `fpulse open`,
  `start.bat`, `start.ps1`, `install_service.py` for Windows
  schtasks / macOS launchd / Linux systemd, `install-windows-service.ps1`
  NSSM, `watchdog.ps1`). LAN binding requires explicit opt-in
  via `FPULSE_BIND_HOST` env or `FPULSE_ALLOW_LAN=1` convenience
  flag. Container deployments (`Dockerfile`, `railway.toml`, CI)
  keep `0.0.0.0` since the container is the boundary.
- **DNS-rebinding defense (two-layer).** Primary: `Host` header
  allowlist refuses any non-loopback `Host` value when the backend
  is loopback-bound. Secondary: `Origin` / `Referer` pinning catches
  cross-origin XHRs even when `Host` looks valid. Both engage only
  in loopback mode; LAN/Plus deployments use the existing CORS
  middleware unchanged.
- **`/api/health/bind-info` endpoint** + **`BindWarningBanner.tsx`**
  React component that surfaces a sticky red banner when the backend
  is actually LAN-bound. Session-dismissible only — returns on every
  page reload so operators can't silently silence a real security
  signal.
- **`assert_dev_auth_local_only()`** preventive guard. No dev-auth
  bypass exists in the codebase today; the guard is there so any
  future bypass MUST call it. Regression-pinned via tests so
  accidentally adding a bypass without the guard would fail CI.
- **`POST /api/system/shutdown`** — loopback-only graceful shutdown
  endpoint. Used by future "Stop server" UI; auto-`beforeunload`
  wiring intentionally skipped (would kill on browser refresh).

**One-command local launcher (`fpulse open`)**

- **`fpulse open`** — new top-level CLI verb. Alias for
  `fpulse serve --open`. Picks a free port (default 8001, falls
  back through 8010), starts the backend, opens the default
  browser. URL also printed for copy-paste fallback.
- **Headless detection** — `is_headless()` checks `SSH_CONNECTION`,
  `WSL_DISTRO_NAME`, `/.dockerenv`, Linux-without-DISPLAY. In any
  of those, skips browser auto-launch cleanly and prints the URL
  prominently so the operator can paste it into a browser on the
  host machine.
- **`--no-open` escape hatch** — keeps the port-fallback + URL
  printing but skips the browser launch attempt. For CI runs +
  environments that have their own browser-launch flow.
- **Deliberately skipped: tokenized launch URL.** The loopback bind
  + `Host` allowlist + `Origin` pinning already provide the defense
  tokens were meant to add. A URL-embedded token would leak via
  browser history / copy-paste. If a token is added later, it goes
  in a header, not the URL.

**Connector tier system (`backend/fpulse/api/cert_matrix.py`)**

- **5 user-facing tiers**: Production / Verified / Beta /
  Experimental / Hidden. Naming mirrors the Airbyte +n8n industry
  vocabulary. Computed per manifest from existing depth-score +
  validation-status + v1-capability signals, with optional manifest-
  declared `tier` field that can opt **down** (never up).
- **`by_tier` aggregate** in the `/api/connectors/cert-matrix`
  response alongside the legacy `by_label`. New optional
  `?include_hidden=true` query for admin/debug retrieval of
  Hidden rows.
- **10 manifests marked Hidden** (`airtable`, `facebook_ads`,
  `google_ads`, `google_analytics`, `linkedin_ads`, `mailchimp`,
  `monday`, `pipedrive`, `shopify`, `zoho_crm`) — consumer-marketing
  / SMB-CRM territory out of enterprise-data-engineering scope.
  Files stay on disk so slugs are reserved and direct links don't
  404. Back-compat: 3 pre-existing manifests with `"hidden": true`
  boolean flag (which was previously a no-op) now correctly resolve
  to Hidden tier.
- **`ConnectionsPage.tsx`** picker also hides `shopify` via the
  existing `roadmap` filter, since the connection-type picker
  doesn't read from the cert matrix yet (1.1 frontend wiring).

**REST framework upgrade (`backend/fpulse/connectors/rest_framework.py`)**

- **Method + body support.** `_http_request()` now honors
  `stream.method` (GET/POST/PUT/PATCH/DELETE) and sends
  `stream.body` (dict → JSON-encoded) / `stream.body_text` (raw).
  Previously the framework silently sent GET against every endpoint
  and dropped the body — making POST-shaped streams like
  `openai/chat_completions`, `snowflake/statements`,
  `mongodb/find` non-functional even though the JSON looked correct.
- **Pagination aliases.** Manifest-author-friendly names
  (`page_token`, `offset`, `page`) now normalize to the framework's
  canonical types (`cursor`, `offset_limit`, `page_number`) so 67
  streams across ~16 pre-existing manifests stop falling through
  to single-page reads.
- **Deep template substitution.** `_deep_interpolate()` walks
  nested dicts/lists so `{prompt}` inside an OpenAI-style
  `messages: [{...}]` array actually gets substituted (the previous
  shallow `_interpolate_dict` left nested strings untouched).
- **Back-compat shim.** `_http_get()` kept as a thin alias so
  external callers don't break.

**Avro + ORC file readers (`backend/fpulse/nodes/file_node.py`)**

- `_read_avro()` via `fastavro` → pyarrow → DuckDB relation.
- `_read_orc()` via `pyarrow.orc` → DuckDB relation. Auto-points
  `TZDIR` at the `tzdata` PyPI wheel on Windows where there's no
  system zoneinfo, avoiding the `IANA time zone database is
  unavailable` runtime error.
- New core deps: `fastavro>=1.9`, `tzdata>=2024.1`.
- Format dropdowns in File node + 5 Cloud Files sources updated
  to include `orc` + `avro`.

**Driver extras + install docs**

- **32 optional-dependency extras** declared in `pyproject.toml`:
  `postgres`, `mysql`, `mssql`, `oracle`, `db2`, `hana`, `teradata`,
  `snowflake`, `bigquery`, `databricks`, `clickhouse`, `mongodb`,
  `redis`, `cassandra`, `neo4j`, `elasticsearch`, `trino`, `aws`,
  `azure`, `google`, `kafka`, `sftp`, `delta`, `soap`,
  `vector-pinecone`, `vector-weaviate`, `vector-qdrant`,
  `vector-chroma`, `vector-embeddings`, `all-databases-no-os-deps`,
  `all`. Every `fpulse[X]` hint in the codebase's error messages
  now resolves to a real extra.
- **`docs/install/database-drivers.md`** — per-database install
  command + OS-level driver requirements (Microsoft ODBC Driver,
  Oracle Instant Client, IBM DSDriver), troubleshooting table.

**Smoke tester (`tools/test_connector.py`)**

- `--list` enumerates every manifest the framework can load
- `--dry-run` resolves method / URL / headers / body without
  calling out (`Authorization` header masked in output)
- `--live-batch` orchestrator for CI: iterates a YAML allow-list
  (`backend/fpulse/connectors/ci/live_smoke.yml`), skips cleanly
  when required secrets are missing (forks), writes
  `last_smoke_status.json` for cert-matrix auto-demotion.
- **`.github/workflows/connector-smoke.yml`** — dry-runs every
  manifest on PRs touching connector code; live-smoke job runs
  only when secrets are present.

**Documentation**

- `docs/install/security-hardening.md` — full local-hardening guide
- `docs/install/database-drivers.md` — DB driver install matrix
- `docs/design/plus-license-model.md` — named-user seat model,
  staged implementation plan (~3 sprints to first customer, Stage 2
  features when warranted), reviewer-driven design corrections
- `docs/roadmap/oss-1-1.md` — 7 deferred items with honest
  reasoning per item; explicitly "deferred from 1.0, not promised
  for 1.1"
- `docs/connectors.md` — full tier-vocab table, live breakdown
  table, list of the 10 hidden connectors
- `readme.md` — `fpulse open` is now the headline launch UX;
  loopback default + opt-in env vars documented
- `docs/quickstart.md` + `docs/product_facts/16_how_to.md` —
  `fpulse open` referenced in install instructions

**Test coverage added**

- `backend/tests/test_local_hardening.py` (17 tests) — bind
  resolution, Host allowlist, Origin pinning, dev-auth guard
- `backend/tests/test_cert_matrix_tiers.py` (16 tests) — tier
  computation rules, opt-down, back-compat hidden flag, by_tier
  aggregate, hidden_total
- `backend/tests/test_launcher.py` (13 tests) — port fallback,
  headless detection, browser-open failure handling, shutdown
  endpoint registration
- `backend/tests/test_rest_framework.py` (+7 tests, now 18 total)
  — method/body/aliases/deep-interpolation contract pinned
- **Total: 64 tests passing** for this batch; 1 Linux-only test
  skipped on the Windows dev machine.

### Added — 2026-05-30 launch-prep + roadmap sweep

The pre-public-launch backlog cleared in one focused session. Net: 44 files staged, 4 new modules, 39 modified. Each item below has a concrete contract verified in code.

- **Incremental sync state** (schema v31). New `sync_state` table + `engine/sync_state_store.py` + `api/sync_state.py` (GET/DELETE per-step cursor endpoints). `db_source.execute()` auto-loads `last_cursor` at the top of each run when `sync_mode=incremental` and auto-saves `MAX(cursor_column)` from the result. Manual `watermark_value` still wins (override for backfill). `api_source` participates via `{cursor}` placeholder substitution in URL/path/headers + `cursor_response_field` for auto-save. UI surfaces a "Last cursor" display + "Reset State" button under DbSourceConfig.
- **Connector certification matrix — 4 new capability flags + curated honesty**. `oauth_refresh`, `rate_limit`, `schema_drift`, `backfill_safety` now detected per manifest. `known_gaps[]` (manifest-declared + auto-inferred for v2 manifests) renders as a ⚠ chip on each cert row. UI: CertChips component shows 4 new ticks + tooltip.
- **One-shot lineage** (`GET /api/storage/tables/{id}/provenance`). Source file → source workflow → source recipe → last run → consumer pipelines, all in one call. StoragePreviewDrawer renders a unified lineage card.
- **Backfill preflight** (`POST /api/executions/backfill/preflight`). Returns server-authoritative warnings + recommendations (cursor-usage check, sink-safety, window-count enumeration) without committing. BackfillModal calls it on every dates/window change (debounced 350ms) and renders the result inline.
- **Data contracts extended**. `TableTest` gains `severity` (fail/warn), `freshness` (max_age_minutes against `table.updated_at`), `row_count_anomaly` (min/max bounds + drift_pct vs prior row count). StoragePreviewDrawer test editor surfaces the new options.
- **Schema preview** (`GET /api/workflows/{id}/step/{step_id}/expected-schema`). Walks the DAG and calls each registered node's static `expected_output_schema(input_schemas, params)` hook. Returns `status: ok|unknown` per step. Implemented for filter (passthrough), sort (passthrough), derived_column (appends). Other nodes return `null` → UI shows "schema computed on first run."
- **Multi-row formula** (`derived_column`). Each `columns` entry now accepts an optional `window: {partition_by, order_by}` which auto-wraps the expression in `OVER (PARTITION BY … ORDER BY …)`. Enables LAG / LEAD / running-totals without the full Window node.
- **Edge port semantics**. Executor stamps `_input_step_ports = [(from_step, from_port, to_port), …]` onto every downstream step's params, so branching nodes can self-filter on the port they got. Foundation for if/switch true/false routing.
- **Macros** (publishable workflows). `metadata.published_as_node=true` on a workflow surfaces it in `/api/node-types` as a virtual `execute_pipeline:<wf_id>` entry with `base_type: execute_pipeline`. Each `WorkflowParameter` becomes a render-time parameter in the macro tile. PipelinesPage kebab gets a "Publish as macro" toggle.
- **Side-effect dry-run**. New `ctx.preview_mode` flag on `ExecutionContext`. Executor-level short-circuit using `SIDE_EFFECT_CLASS`: passthrough sinks return the input relation unchanged; transforming/terminal nodes emit a one-row "preview_mode" marker. Single edit covers all 30+ side-effect nodes. `send_email` also has a node-level preview message.
- **code_script sandbox hardening**. AST-based import allowlist (re, json, math, statistics, datetime, decimal, itertools, functools, collections, csv, io, pandas, numpy, duckdb) layered on top of the existing string-block list. Catches `from socket import *`, `import urllib.request`, obfuscated module names. SyntaxError surfaces up-front instead of inside the worker thread.
- **Data profile depth**. `data_profile` extended from 11 → 16 metrics: adds `mean_value`, `median_value`, `stddev_value` (numerics via TRY_CAST) + `avg_length`, `max_length` (strings).
- **Security route protection**. 5 unprotected routers locked (workflows, schedules, variables, alerts, monitor) with router-level `require_auth` + per-route `require_min_rank("developer")`. Role rank map extended with `workspace_admin` (90), `data_engineer` (70), `analyst` (30) so test-fixture roles route correctly. Trailing-slash aliases (`""` + `"/"`) on the 6 root routes so httpx-default-no-follow-redirects clients don't 404. `/api/plus/audit/events` OSS-stub returns 401 (anonymous) / 402 (authed-needs-Plus). `tests/conftest_fixtures_v2.py` `_ensure_user` now creates test users directly via UserStore so the `role` field is honoured (the `/api/auth/register` endpoint deliberately strips it).
- **Workflow PUT contract back-compat**. The endpoint now accepts both `{workflow, change_summary}` and a plain `Workflow` blob. POST `/api/workflows/` also accepts `steps[]` + `connections[]` so one-shot creation works.

### Schema migration

- **v31 `sync_state` table** (idempotent additive). Per-(workflow, step) cursor watermark for incremental sources. Pre-existing pipelines unaffected — table fills as operators flip a source's `sync_mode` to `incremental`. See `fpulse/engine/sync_state_store.py` for the read/write API.

### Bugs caught + fixed during validation

- `SyncStateStore.get()` and `list_for_workflow()` were indexing `Database.fetchone/fetchall` results by integer; the wrapper returns dict. Would have crashed on the first incremental run. Now uses named keys.
- Test fixture `_ensure_user` was creating viewer/analyst users via `/api/auth/register`, which deliberately ignores the `role` field as anti-escalation. Result: every test user was created as `developer`, silently bypassing RBAC tests. Now goes through UserStore directly when the API can't honour the role.

### Security

Pre-public-launch hardening per the launch security audit. Each item has
a concrete threat model + verified test:

- **SSRF allowlist on the OpenAPI fetcher** (`backend/fpulse/connectors/ai_authoring.py`).
  The Author Connector feature fetches user-supplied URLs. Without
  validation an attacker could point at `169.254.169.254/...` or
  `localhost:5432` to exfiltrate cloud-metadata creds or probe internal
  services. Defense: scheme allowlist (http/https only), DNS resolution
  + reject loopback / link-local / private / multicast / reserved
  ranges, per-redirect-hop re-validation (defeats DNS-rebinding), 2 MB
  body cap, no embedded credentials. Override for on-prem catalogs:
  `FPULSE_OPENAPI_FETCH_ALLOW_PRIVATE=1`. **9 attack URLs tested, 8
  blocked, 1 public URL passes.**
- **Login rate-limit + account-lockout** (`backend/fpulse/api/auth.py`).
  Public OSS launches see automated credential-stuffing within hours.
  Defense: in-process sliding-window counter keyed on `(email,
  client-ip)`. 3 free attempts, then exponential delay (1s → 8s),
  then hard 15-minute lockout at 8 failures (HTTP 429 + Retry-After).
  Success wipes the counter. Cross-IP isolation — a legit user on
  another network isn't punished for an attacker's storm. Tunable via
  `FPULSE_LOGIN_SOFT_THRESHOLD` / `FPULSE_LOGIN_HARD_THRESHOLD` /
  `FPULSE_LOGIN_LOCKOUT_SECONDS` env vars.
- **AI endpoint per-user rate-limit** (`backend/fpulse/ai/rate_limit.py`).
  Without throttling, a single leaked session token can drain the
  operator's LLM budget in minutes. Defense: sliding-window per-user
  counter, default 60 calls/hour. Streaming endpoint charges 2× to
  reflect longer LLM sessions. Wired into `/api/ai/agent` and
  `/api/ai/agent/stream`. Tunable via `FPULSE_AI_RATE_MAX_PER_HOUR` /
  `FPULSE_AI_RATE_WINDOW_SECONDS`; disable in tests with
  `FPULSE_AI_RATE_ENABLE=0`.
- **`.github/dependabot.yml`** — weekly Python + npm + GitHub-Actions
  updates, grouped minor/patch to reduce review load, major bumps
  surfaced separately. Closes the "we didn't catch the `psutil` /
  `pandas` packaging gaps" credibility cost.
- **`privacy.md` at repo root** — plain-language privacy policy
  covering: no-telemetry guarantee, what data is stored locally and
  where, outbound connection list (only when operator-configured),
  retention policies, access control, data export / deletion
  primitives. Cross-references `docs/ai-boundary-contract.md` for AI
  data flow and `security.md` for vulnerability reporting.
- **Backup-restore round-trip tests** (`backend/tests/test_backup_restore.py`).
  Backups are useless if they can't be restored. Three tests: full
  round-trip preserves every row, workspace isolation survives the
  restore (regression guard against schema-collapse bugs), no
  plaintext credential markers reach the snapshot file (defense in
  depth on the encryption layer). All 3 pass.

### Added

#### Extensibility framework — make the OSS connector model first-class

- **`Insights → Author Connector` — common starting points gallery.** Six
  curated public OpenAPI specs (Stripe / GitHub / Slack / Twilio /
  DigitalOcean / Plaid) shown as one-click cards in the Basics step of
  the OpenAPI authoring mode. Click → connector_id, display_name, and
  OpenAPI URL pre-filled, ready to Continue → Generate. Eliminates the
  "I want to try this but don't have a URL handy" friction. Source
  list lives at the top of `frontend/src/components/pages/ConnectorAuthorPage.tsx`
  for easy extension.
- **`Insights → Gallery` — community gallery page.** New sibling tab to
  Author Connector. Six curated starting-point cards (each one a one-click
  link to the Author page pre-filled with the OpenAPI URL) plus a "How
  this works today" banner pointing to GitHub Discussions for the live
  community list. Three contribution paths surfaced: Build your own /
  Share yours / Request a connector.
- **Connector picker — "Don't see your tool?" footer.** A banner now
  appears below the connector grid in EVERY category with two CTAs:
  "Build your own (90s)" → Author Connector, and "Suggest a connector"
  → pre-filled GitHub issue. The natural disappointment point after
  scrolling 30-odd cards looking for a system that isn't there.
- **Connector picker — provenance scaffold.** The legend above the grid
  now shows three provenance tiers (F-Pulse / Community / Yours)
  alongside Certified/Beta. Per-card pills activate automatically when
  manifest discovery starts tagging non-first-party connectors — no
  further frontend work needed at that point.
- **`docs/extend/build-a-connector.md`.** 30-minute end-to-end tutorial
  for the four authoring paths (OpenAPI, samples, hand-authored,
  derive-from-upstream). Links into the Help-page docs catalog.
- **`docs/extend/build-a-node.md`.** Two-tier guide for custom nodes —
  the 5-minute Python-transform path and the 30-minute first-class
  node-type path.
- **`docs/extend/derive-from-talend.md` + `talend-derivation-roadmap.md`.**
  Process and prioritised port list for deriving connectors from
  compatibly-licensed upstream OSS sources. Both surfaced in the Help
  catalog under a new "Extending F-Pulse" category.
- **`docs/vs-talend.md`.** Side-by-side comparison page for evaluators.
  Honest about where each tool wins; positioned as a reference doc
  for those who came looking for the comparison.
- **GitHub issue templates** for connector requests, node requests, and
  contributions — pre-fills the info reviewers need so first-touch
  triage doesn't have to ask the same five questions every time.
- **`NOTICE` — Apache 2.0 derivations section.** Scaffold for attributing
  upstream OSS sources when a connector or node is derived. Required
  by Apache 2.0 §4(b); makes future derivations safe and consistent.
- **`benchmarks/talend-comparison/` reproducible benchmark scaffold.**
  Deterministic data generator + F-Pulse-side pipeline + spec for
  building the equivalent job on a competing JVM tool to compare wall-
  clock and peak memory head-to-head.

#### Operator UX — startup, alerts, and dashboard

- **Alert email lineage now renders the actual DAG.** Scheduled-run
  alert emails (and manual-run alerts) used to flatten every pipeline
  into a linear `A → B → C` chain regardless of branches or joins.
  Now the renderer computes per-step rank from the workflow's
  connections, groups by rank into columns, and stacks within a
  column. Pipelines with parallel branches and joins render with
  parallel branches and joins. Falls back to the linear layout when
  no `workflow_connections` is supplied (legacy callers stay quiet).
- **Scheduled runs now populate per-step lineage in the Executions UI.**
  Schedule-fired executions previously rendered "No lineage data" in
  the Lineage tab because `step_logs` weren't being written. The
  scheduler now builds and attaches `StepLog` entries the same way
  the manual-run path does — Lineage tab works for scheduled runs.
- **Dashboard System tile.** "DB size" and "Uptime" used to render
  "—" even on a healthy install because of a backend-frontend
  contract mismatch (`uptime_sec` vs `uptime_seconds`, missing
  `db_files` from the summary endpoint). Fixed.
- **Dashboard Workspace Overview — empty workspace looks empty.**
  Fresh OSS installs used to land with 3 auto-seeded demo connections
  pointing at non-existent endpoints (`db.example.com`, etc.),
  inflating the Connections count. Demo seeding is now gated behind
  `FPULSE_SEED_DEMO_DATA` (default off). See `.env.example`.

#### Backend hardening

- **`pipeline_checkpoints` table self-heals on every boot** — installs
  whose schema_version was stamped past 23 without the migration body
  actually running (partial-failure recovery) silently failed every
  step persistence with "no such table: pipeline_checkpoints". The
  v23 migration is now also called from the boot-time self-healing
  block, alongside v27.
- **`/api/plus/license` OSS stub.** Returns the canonical "no license"
  payload directly instead of 404-ing on every Dashboard / Sidebar /
  Account mount. Frontend behaviour unchanged; backend log noise
  reduced from ~12 lines per page load to 0.
- **`workflow_versions.list_all()` defensive fallback.** The "Last Run"
  column JOIN against `execution_logs` now degrades gracefully to
  NULL when the table doesn't exist (test environments + admin tools
  that skip the lifespan boot). Production paths unchanged.

### Changed

- **README headline + value-prop** — rewritten to lead with positive
  statements of what F-Pulse *is* (DuckDB engine, built-in scheduler,
  operational layer) rather than what it isn't. Per the
  no-competitor-product-names policy.
- **`docs/connectors.md`** — reframed as a three-tier model: first-party
  catalog (37 manifests) + open framework (build any in ~90 seconds)
  + community contributions. Removes the "what's missing" framing.

### Fixed

- **`start.bat` and `start.ps1` — pip step no longer hangs invisibly.**
  Pre-2026-05 the launcher ran `pip install -r requirements.txt
  --quiet 2>nul` on every start; pip would silently re-resolve
  against PyPI for 10-30s even when nothing changed, and when it
  ACTUALLY hung (network blip, lock contention, heavy wheel build)
  `--quiet 2>nul` swallowed the only signal. Now probes for the
  canonical heavy deps (`fastapi`, `uvicorn`, `psutil`, `duckdb`,
  `pydantic`, `pandas`, `pyarrow`) with one `python -c` call. If
  they all import, pip is skipped entirely. Otherwise pip runs
  WITHOUT `--quiet 2>nul` so progress + errors are visible.
- **`start.bat` and `start.ps1` — port collision detection up front.**
  Pre-flight check on ports 5174 (frontend) and 8001 (backend)
  before spawning anything. If either is held, prints the holding
  PID + the exact `taskkill` / `Stop-Process` command and exits.
  No more silent "two frontends, no backend reachable" state.
- **`start.bat` and `start.ps1` — backend window no longer crashes
  silently.** Removed `--reload` from the spawned uvicorn invocation
  because the reloader's watcher/worker process pair doesn't survive
  `start "title"`'s detached console on Windows. Operators editing
  backend code can re-run start.bat after the edit, or run uvicorn
  manually in a foreground terminal with `--reload` when actively
  iterating.
- **Vite `strictPort: true`.** Vite no longer drifts from 5174 to
  5175 when an orphan holds the original port — it now exits with
  EADDRINUSE and the launcher's port-collision pre-check tells the
  user exactly which orphan to kill. Single canonical URL:
  http://localhost:5174.
- **`requirements.txt` + `pyproject.toml`.** Added `pandas>=2.0.0`,
  `pyarrow>=14.0.0`, and `psutil>=5.9.0` (psutil was previously
  only in pyproject; pandas + pyarrow were imported by the
  heavy-mode warmup but never declared). Fresh installs no longer
  print "Warmup failed: No module named 'pandas'" at boot.
- **"[object Object]" error toasts.** FastAPI dict-bodied
  HTTPException details no longer stringify as `[object Object]`
  in the frontend toast. New `_humanizeApiError(detail)` helper
  handles string / array / object-with-message / object-with-detail
  / object-with-error shapes.
- **Auto-save no longer clobbers published pipelines.** The pipeline
  auto-saver skips writes when the workflow's status is `published`
  — manual edits + Save take an explicit republish path. Prevents
  test runs from silently overwriting a vetted production version.
- **Table overflow swept across all list pages.** Long names no
  longer push the trailing actions column off-screen. Affected:
  Pipelines, Connections, Credentials, Variables, Schedules,
  Executions, Trash.
- **Dashboard "Success Rate" is scheduled-only.** Manual test runs
  the user fires while iterating on a pipeline no longer drag the
  operational-health KPI down. Falls back to all-runs success_rate
  when the backend hasn't shipped the scheduled sub-dict (older
  OSS installs).

### Removed

- **Demo connection seeding by default.** The 3 auto-seeded demo
  connections (Orders DB / Oracle Fusion / Snowflake DW) pointing
  at non-existent endpoints are no longer created on fresh installs.
  Set `FPULSE_SEED_DEMO_DATA=1` to opt back in for demo deployments
  or screenshot harnesses. Existing installs keep their seeded rows
  — the gate only fires when the table is literally empty.

---

## [1.0.0] — 2026-06-01

First public release of F-Pulse OSS — single-binary visual data pipeline
engine with embedded AI assistance, 40 node types across 6 categories,
45 first-party connector manifests, DuckDB execution, and an optional
local LLM via Ollama.

### Tested with

This release was built and tested against the following matrix. Running
outside this matrix is supported on a best-effort basis but not gated by
CI. To upgrade an external component independently of F-Pulse, edit the
corresponding tag in `docker-compose.yml` (or your `.env`) and run
`docker compose pull <service>` — see `docs/deployment.md`.

| Component         | Version(s)                  | Notes                                       |
|-------------------|-----------------------------|---------------------------------------------|
| Python            | 3.11.7, 3.12.1              | 3.12 is the recommended runtime             |
| Node.js           | 20.10 LTS                   | Frontend build only; not required at runtime|
| Docker Engine     | 25.0+                       | Compose v2 (`docker compose`, not `-compose`)|
| DuckDB            | 1.1.3                       | Bundled via `duckdb` pip wheel              |
| Ollama            | 0.5.7                       | Optional, enables local-LLM agent surface   |
| PostgreSQL        | n/a (OSS uses SQLite)       | F-Pulse+ tested against 16.4-alpine         |
| Recommended models| `qwen2.5:7b` (CPU laptop)   | Local tool-use floor per 2026-05-19 revision; ~6 GB RAM, 30–60 s/turn |
|                   | `llama3.1:8b` / `phi-4` (CPU)| Equally-supported floor alternatives        |
|                   | `qwen2.5:14b` (12GB+ GPU)   | Faster tool-use latency on a discrete GPU   |

Pinned image tags live in `docker-compose.yml` and can be overridden via
`.env`:

```env
FPULSE_IMAGE_TAG=1.0.0
OLLAMA_IMAGE_TAG=0.5.7
```

### Added

#### Insights → Activity — operator visibility (2026-05-19)

- **Token breakdown on the page header tile** — Tokens KPI now shows
  total as the headline plus an `Input X · Output Y` footer. Older
  agent rows (pre-split) back-fill into input so totals reconcile.
- **MODEL column in the events table** — every agent row records the
  provider and model name (`qwen2.5:1.5b`, `claude-sonnet-4-6`, etc.)
  captured at run-start via `resolve_provider()`. Trace schema gains
  `model` and `provider` columns via idempotent `ALTER TABLE` so
  existing installs upgrade without a separate migration step. Older
  rows that predate the column render `—`.
- **Per-run TOKENS column** — total + `in X · out Y` for each event,
  matching the KPI tile's visual language. Failed runs that never
  consumed tokens show `—` rather than a misleading `0`.
- **Search input** — case-insensitive match across actor, summary,
  model, and user_intent. Updates the visible-count toolbar live.
- **CSV export** — emits exactly what's currently filtered, with
  columns `kind, actor, model, provider, tokens_in, tokens_out,
  tokens_total, summary, timestamp, severity`. File name carries an
  ISO-timestamp suffix.
- **Per-workspace AI cost-rate table** at `/api/v1/ai/cost-rates`
  (GET/PUT/DELETE). Resolution order is model → provider → fallback.
  Ollama short-circuits to $0 even if a user blanks the row. Replaces
  the previous hardcoded `(tokens / 1000) * 0.0006` blended estimate
  (flagged in `docs/PAGE_BY_PAGE_AUDIT.md` "Cost estimate honesty",
  now resolved).
- **Settings → AI Pricing editor** — full per-workspace rate-table
  CRUD (providers, per-model overrides, default pricing). Save
  broadcasts `fpulse-settings-changed` so the Activity tile
  re-fetches and updates without a page reload.
  - **Cached-input column** alongside Input and Output — forward-
    compatible field for prompt-cache hit pricing. The OSS trace
    store doesn't separate cached tokens yet, so it currently
    contributes $0 to real costs, but the Cost Simulator models the
    savings and the data shape is ready for when provider clients
    report cached counts.
  - **Sticky "unsaved changes" banner** with Discard + Save actions
    appears at the bottom of the section when the draft differs
    from the persisted state.
  - **Per-model search + provider grouping** — entries grouped by
    inferred provider (Anthropic / OpenAI / Ollama / Other), with
    a search input for dense lists.
  - **Numeric validation** — min 0, max 1000, step 0.01; invalid
    cells render red.
  - **Info tooltips** on key concepts (cached input, default
    pricing, per-model overrides, local inference).
  - **"Default pricing"** label replaces the prior raw "Fallback
    (unknown provider/model)" wording.
  - **Cost simulator** below the rate editor — compact 4-input
    calculator (single model/provider picker · requests/day · input
    tokens · output tokens) yields one headline number: estimated
    monthly cost (with the day rate as a subtitle). Uses the
    current DRAFT rates so ops can compare providers before
    saving. Intentionally minimal: not auto-filled from historical
    traces — manual entry only, projection vs. replay are kept
    separate.
- **Est. Cost tile honesty** — shows `$0.00 · local — no per-token
  cost` only when every classified run was Ollama (positive
  evidence required); otherwise shows the per-model rate-table sum
  with `per-model rates · edit in Settings`. Legacy null-provider
  rows resolve via the fallback rate, not the misleading "free"
  label.
- **Documentation** — `docs/dashboard-metrics.md` gains an
  "Insights → Activity page" section with the resolution order,
  seed rate table, and editor location.

#### Bug fixes

- `/api/plus/workspace-settings` GET/PUT — replaced broken
  `db.connect()` calls (the `Database` class has no such method)
  with direct `db.fetchone()` / `db.execute()` per the actual API.
  Endpoint had been returning 500 on every call.

#### Pipeline engine — Sprint 1 (Gate 1)

- **Schema migration v23** introduces the `pipeline_checkpoints` table —
  a per-`(run_id, step_id)` index of step outcomes. The executor records
  success / failure / skipped on every step so a failed run can be
  resumed from the first non-success step instead of from scratch.
  Best-effort: a checkpoint write failure NEVER fails the run.
- **Bulk-load runner** (`fpulse.engine.bulk_load`) — per-dialect plugin
  pattern. Replaces the previous row-by-row INSERT path with native
  bulk-load paths for production-scale writes. Two dialects ship in
  this release; six more are designed for a follow-up.
- **Postgres bulk-load dialect** — `COPY FROM STDIN` via
  `psycopg2-binary` (preferred) or `psycopg` v3. All four modes
  supported: create / append / truncate / **merge** (idempotent
  UPSERT via `INSERT … ON CONFLICT DO UPDATE`).
- **Snowflake bulk-load dialect** — `PUT` to user stage + `COPY INTO`
  via the `snowflake-connector-python` driver. Same four modes; merge
  uses Snowflake-native `MERGE INTO`.
- **SCD Type 2 node** — slowly-changing-dimension Type 2 with
  hash-based change detection. Tracks historical versions per business
  key; emits the full new dimension state for the downstream sink.
  Configurable column names (`effective_from`, `effective_to`,
  `current_flag`, `surrogate_key`, `null_high_water`).
- **DataProfileNode** with `include_columns` / `exclude_columns`
  filters and a proper `param_schema()` so the UI renders rich field
  metadata.

#### Visual builder

- Visual pipeline builder with 37 node types and a React Flow canvas
  with snap-to-grid, minimap, and auto-fit-view editor preferences
  (live-applied without reload — see
  `frontend/src/hooks/useEditorPreferences.ts`).
- Pre-run banner with last-run summary, cost/duration estimate, and a
  4-mode safety picker (Live / Sample / Dry-run / Validate-only).

#### AI Copilot — chat architecture overhaul

- **Fast-lane router** — rule-based classifier that answers the 14
  most-common questions ("list pipelines", "give me an overview",
  "what failed today", "what is f-pulse", "what's my role", etc.) in
  sub-1 second without hitting the LLM. Falls through to the agent
  loop for genuinely novel prompts. Reasoning gate ("why", "explain",
  "compare", "diagnose") always routes to the LLM.
- **Layer 1 — session context block** — every chat turn carries a
  structured Markdown block describing who the user is, role,
  environment, edition (Free/Plus), workspace counts, and what they
  can or cannot do. Always-on, ~400-600 tokens; never dropped under
  budget pressure. Lets the LLM say "SSO is F-Pulse+ only" instead of
  hallucinating capabilities the operator's edition doesn't have.
- **Layer 2 — product knowledge RAG** — 15 curated `docs/product_facts/`
  files (overview, node types, editions, concepts, troubleshooting,
  FAQ, credentials, connectors, notifications, AI Copilot, RBAC,
  templates, backup, audit, scaling) chunked + embedded at startup.
  Top-3 retrieval per turn injects accurate F-Pulse-specific knowledge
  into the system prompt. Admin reindex via
  `POST /api/ai/product-knowledge/reindex`.
- **Provider-aware wall-clock** — 300 s for local Ollama (qwen2.5:3b
  on CPU is slow per turn), 120 s for cloud providers. Override via
  `FPULSE_AGENT_WALL_CLOCK_S=600` (clamped 10-600).
- **Stop button** — red button replaces Send while a turn is in
  flight. AbortController wired through the streaming fetch; partial
  text is preserved with a `_Stopped by user._` marker so users can
  bail out of slow CPU runs without waiting for the wall-clock cap.
- **Elapsed-seconds counter** + actionable reassurance hint after 30 s
  on local providers ("for instant answers next time, try `list
  pipelines`, `overview`, `failures today`").
- **First-launch qwen2.5:3b banner** — surfaces when Ollama is
  configured but no model is installed, or when the active model is
  too large for CPU (`llama3.1:8b` etc.). One-click pulls the
  recommended `qwen2.5:3b` and auto-refreshes the provider cache.
- **`get_installation_health` agent tool** — single READ tool that
  wraps `InventoryCollector` to return the installation health score
  (0-100), the prioritised punch list of issues (inline credentials,
  undeployed pipelines, etc.), top failing pipelines, 24 h success
  rate, and headline inventory totals. Replaces ad-hoc fan-out across
  `inspect_connections` + `list_executions` + `list_schedules` for
  "what needs my attention" / "audit my install" prompts. Tier-aware:
  on Free the issue text drops the Plus-only Vault migration endpoint
  reference. Brings the registry to 20 tools (15 READ + 4 SAFE_WRITE
  + 1 HIGH_IMPACT_WRITE).

#### Connector certification — Gate 3

- **Certification matrix API** — `GET /api/connectors/cert-matrix`
  returns one row per manifest with depth score (0-5), category,
  vendor, validation status. Public endpoint; renders into the
  Connector Cert Matrix page in the frontend.
- **Connector manifest schema v2** (F0.1) with depth-score rubric and
  a `python -m fpulse.connectors.certify` CLI for validation +
  v1→v2 migration.

#### Trust artifacts — Gate 4

- **Trust posture API** — `GET /api/trust/posture` returns the live
  sovereignty + security baseline + supported-models policy as
  structured JSON. Stable shape so compliance scrapers can rely on it.
- **`GET /api/trust/eval-summary`** surfaces the most recent eval
  harness output for the trust page.
- **`GET /api/trust/supported-models`** returns the supported-models
  policy as JSON; mirrors `docs/supported-models.md`.
- **`docs/supported-models.md`** — authoritative supported-models policy
  with hardware tiers, cloud opt-in language, deprecated recommendations.
- **`docs/compliance.md`** — compliance one-pager with verifiable
  artifact links per claim.
- **Live posture section** on the existing Trust page renders the
  posture API response with KPI tiles + verifiable artifact links.

#### Operations

- **Encryption is now always-on (Free + Plus).** Master key file at
  `~/.fpulse/secret.key` (or `$FPULSE_DATA_DIR/secret.key`); chmod 600
  on first run; fail-closed on world-readable POSIX permissions. See
  Security section for cipher details.
- **Notification watchdog** (long-running and schedule-miss detectors)
  with configurable methods (email, webhook, Discord) per workspace.
- **Telemetry consent UI** with opt-in default OFF; backend persists to
  `admin_settings.telemetry_enabled`.
- **Eval harness self-validating Gate-1 case** — confirms SCD2 node,
  DataProfileNode, checkpoint store, and bulk-load runner are all
  wired before the harness reports green.
- **Deployment runbook** at `docs/deployment.md` covering install,
  upgrade (F-Pulse / Ollama runtime / Ollama models), backup, and
  disaster recovery.

### Changed

- **Local LLM tool-use floor raised to ~7B (2026-05-19).** The previous
  `qwen2.5:1.5b` / `:3b` CPU picks were demoted from the recommended set
  after they failed to drive the agent's tool-use loop reliably — small
  Qwen 2.5 models advertise tool schemas but silently return greetings
  or empty responses instead of calling tools. The reliable floor is
  now `qwen2.5:7b` (~6 GB RAM at Q4_K_M, 30–60 s per agent turn on CPU);
  `llama3.1:8b` and `phi-4` are equally-supported alternatives. The
  Settings → AI Provider page banner detects sub-floor models and offers
  a one-click upgrade. Trust-page tier table, `/api/trust/supported-models`,
  `docs/supported-models.md`, `docker-compose.yml` defaults, and the
  product-knowledge atlas were all updated to match. Historical
  references to `qwen2.5:1.5b` / `:3b` in earlier CHANGELOG entries
  are intentional and unchanged — they document what was recommended
  at the time of those entries.
- **Credential encryption — security correction.** Pre-1.0 OSS Free
  installs stored credentials and AI provider API keys in plaintext on
  disk despite the marketing claim of "encrypted at rest". From 1.0
  onward, the encryptor is always wired (Free + Plus). Existing
  installs upgrading to 1.0 keep working — legacy plaintext rows are
  tolerated on read; the next save re-encrypts. See
  `docs/compliance.md` for the cryptography table.
- **Frontend Page type extended** with `'cert-matrix'` so the new
  Connector Cert Matrix page is reachable via `#cert-matrix`.
- **Settings page is fully wired**: every visible OSS control is either
  (a) bound to a real consumer, (b) read-only env-var display with the
  variable name surfaced, or (c) Plus-gated and hidden in OSS.
- **Help documentation filtered** to OSS-relevant guides; Plus-only
  content is hidden behind `plus_only` catalog flags (defence-in-depth
  404 in the content endpoint).
- **Help → Documentation tab now resolves cross-doc `.md` links inside
  the viewer.** Previously, clicking `[Trust posture](trust.md)` from
  inside `ai.md` triggered an SPA-route navigation to `/help/trust.md`
  and 404'd. The viewer now intercepts relative `.md` links, resolves
  them against the currently-selected doc's path (handles `./`, `../`,
  subdirectories, preserves `#anchor`), looks them up in the doc
  catalog, and either swaps the selection in-place or surfaces a
  banner explaining the target isn't part of the in-app set. Four
  developer docs that were referenced from `readme.md` but missing
  from the catalog (`api.md`, `dev-guide.md`, `execution-architecture.md`,
  `testing.md`) were added; changelog.md is now also reachable via the
  catalog (uses a `repo_root` flag to read from outside `docs/`).
- **Reports page redaction notice is tier-aware.** On OSS Free the
  notice now reads "Secrets are flagged as `[INLINE — MIGRATE]`"
  without mentioning Vault references — Vault is a Plus-only feature
  and would never appear in an OSS report. Plus retains the original
  mixed phrasing.
- **Reports scoped to a single Project or Pipeline now narrow the
  whole report, not just the projects/workflows section.** Schedules,
  alerts, connections, approval gates, and the operational/failure/
  duration analyses are filtered to the surviving workflow IDs so a
  pipeline-scope download describes that pipeline alone — not the
  whole workspace. Users section is emptied for pipeline scope.
- **Pre-launch documentation refresh.** Stale `v0.5.0` headers updated
  to `v1.0.0` in `api.md` / `dev-guide.md` / `testing.md`. Tier
  breakdown in `docs/product_facts/10_ai_copilot.md` reconciled with
  the registry (15 READ + 4 SAFE_WRITE + 1 HIGH_IMPACT_WRITE). Stale
  tool-count references (17 / 19) bumped to 20 in the agent system
  prompt and several product-facts files. Broken `[Scheduling]
  (scheduling.md)` link removed from `quickstart.md` (file did not
  exist on disk); `[node reference](nodes.md)` cross-references
  rephrased to point at the Help → Nodes tab since `nodes.md` is
  intentionally excluded from the doc catalog.
- **Help → Documentation header anchors now scroll.** Markdown headers
  (`<h1>`–`<h4>`) are emitted with GitHub-style slug ids
  (`6-submitting-a-project-for-approval`, `table-of-contents`, etc.)
  and a `scroll-mt-16` offset to clear the sticky page header. The
  article-level click handler intercepts `#anchor` links so TOC entries
  scroll without setting `window.location.hash` (which the SPA router
  would otherwise interpret as a top-level navigation). Cross-doc
  `target.md#section` links stash the anchor and scroll after the
  next doc has rendered.
- **In-catalog documentation audit (May 9 2026).** The Help →
  Documentation tab was reviewed for OSS appropriateness:
    - `user-guides/projects.md` rewritten for the OSS audience.
    - `ai.md` Governance section reframed.
    - `trust.md` adjusted so OSS commitments are clearly identified.
    - `execution-architecture.md` withdrawn from the doc catalog;
      `architecture.md` covers the OSS execution model and is what
      the README now points at.
    - `security-deployment.md` re-added to the doc catalog. Covers TLS
      termination, reverse-proxy snippets, the security-relevant env
      vars F-Pulse OSS reads, master-key file-permission enforcement,
      `X-Forwarded-Proto` header forwarding for HSTS, `FPULSE_CSP`
      overrides for iframe embedding, and rate-limiting guidance.
- **Pricing and connector counts framed honestly**: 45 OSS connectors
  (8 dialects + 37 SaaS manifests), 18 prod-grade per the depth-5 rubric.

### Security

- **Credentials and AI provider API keys are encrypted at rest** with
  Fernet (AES-128-CBC + HMAC-SHA256). Key file at
  `~/.fpulse/secret.key` (or `$FPULSE_DATA_DIR/secret.key`); 32-byte
  symmetric, chmod 600 on first run, fail-closed on world-readable
  POSIX permissions. See `backend/fpulse/security/encryptor.py`.
- **AI agent enforces RBAC + policy gates + idempotency caching +
  prompt signing** on every tool call.
- **Per-user and per-workspace daily token wallets** (env-tunable) with
  forced dry-run on first 3 successful invocations of any write tool.
- **No outbound traffic in default config.** Cloud LLM providers are
  off-by-default opt-in; telemetry is off-by-default opt-in. See
  `GET /api/trust/posture` for live verification.

### Known limitations

- F-Pulse+ Postgres adapter (Stage 3b) ships in a follow-up release;
  OSS continues on SQLite.
- Bulk-load dialects beyond Postgres + Snowflake (BigQuery, Redshift,
  Databricks, MSSQL, Oracle, MongoDB, ClickHouse) are designed and
  ship in a follow-up.
- Connector marketplace and Python Transform node remain Plus-only per
  `edition-matrix.md`.
- Frontend `npm run build` may emit pre-existing TypeScript strict-mode
  errors; the build still produces a runnable bundle and `vite dev`
  serves correctly. Strict-build cleanup tracked for 1.0.1.

---

[1.0.0]: https://github.com/hybridyn/fpulse/releases/tag/v1.0.0
