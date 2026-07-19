# What's in this build

F-Pulse OSS Free is a single-binary, single-user data pipeline platform that runs end-to-end on your machine. This page lists what ships in this build.

## Install profile

| | **F-Pulse OSS** (Apache 2.0) |
|---|---|
| **Who it's for** | Solo builders, prototypers, evaluators |
| **Install** | Single binary, SQLite, runs on a laptop |
| **License** | Apache 2.0 — fork it, ship it, sell it (subject to the trademark policy) |
| **Support** | Community (GitHub Issues) |
| **Updates** | Public releases |

## Features included

Everything below ships in this build, unrestricted.

- **Visual canvas builder** with all 40 node types
- **33 first-party connectors visible by default** — 4 database dialects (PostgreSQL / MySQL / MS SQL Server / SQLite), 2 bulk-load dialects (Postgres `COPY FROM STDIN`, Snowflake `PUT` + `COPY INTO`), 27 SaaS REST manifests (19 Beta + 8 Experimental tier). 10 additional manifests ship Hidden — present on disk but suppressed from pickers (consumer-marketing / SMB-CRM / ads — out of enterprise-data-engineering scope). Full breakdown per tier at `/api/connectors/cert-matrix` and [`docs/connectors.md`](connectors.md).
- **DuckDB execution engine** with full vertical scaling on a tuned host
- **Local AI assistant** via Ollama — unrestricted, no per-call limits
- **Cloud AI assistant** via bring-your-own-key — unrestricted, no proxying through us
- **20 pipeline templates** (Medallion ETL, CDC, Data Quality, etc.)
- **Cron scheduling** with retry and backfill
- **Pipeline export / import** as zip bundles
- **WebSocket execution logs** with per-step preview
- **Encrypted credentials store** (Fernet — AES-128-CBC + HMAC-SHA256, master key file mode 0600)
- **REST API** + CLI
- **Email notifications** + Slack, Discord, and generic webhook destinations
- **Long-running pipeline alerts** + schedule-miss detection
- **Browser desktop notifications**
- **Pool admin page** (read-only governor banner, spill check, hardware presets)
- **Per-step metrics** + downloadable run diagnostics
- **Plugin SDK** + connector SDK
- **AI evaluation harness** for testing model quality on your own corpus
- **F-Pulse Steward** — read-only background reliability layer that flags duplicate sources and duplicate pipelines across your workspace. Specialist modules land progressively: Archeologist (1.1, this build), Incident Analyst (1.2), Foreseer (1.3), Curator (1.4), Optimizer (2.0). **OSS-first**, not a paywalled feature. See [`steward/overview.md`](steward/overview.md).
- **F-Pulse Memory Layer** — durable, gated lesson store, separate from the operational journal and from dismiss-suppression. Lessons are created via explicit `POST /api/steward/lessons` and stay `PROPOSED` until a human approves them; only `APPROVED` lessons influence Steward reasoning. Ten lesson categories (source_quirk, failure_pattern, retry_rule, cost_anomaly, …), YAML on disk for hand-review, deterministic search by source + error substring via the lesson-search API. Auto-invocation on pipeline failure lands with Incident Analyst in 1.2. See [`steward/memory-layer.md`](steward/memory-layer.md). Ships in OSS.
- **Steward user-defined rules (YAML)** — admins write additional detectors as YAML files under `<data_dir>/steward/<workspace>/rules/`. No code, no plugins, no fork. Matches become regular findings with the same alert-fatigue guarantees (time-clamp, rebound, de-dup) as built-in detectors. Per-workspace storage; broken rules surface as load errors via `GET /api/steward/rules` so admins see WHY a rule isn't taking effect rather than silent skip. In-app authoring UI + SQL escape hatch + cross-workspace rule sharing ship in Plus. See [`steward/custom-rules.md`](steward/custom-rules.md). Ships in OSS.
- **Steward connector-health detector** — first connector-level Steward signal. Tracks per-connection test outcomes (auto-recorded from the existing Test Connection button + an external `POST /api/steward/connector-health` for CI / monitoring tools). Sustained-failure streaks emit one of `connector_auth_failure`, `connector_unreachable`, or `connector_rate_limit` with severity scaling by streak length (P3 at 2-3 fails, P2 at 4-9, P1 at 10+). Time-clamped: a single flap or sub-5-minute streak never escalates. Also fires `credential_near_expiry` when recorded credential expiry is within 7 days. Per-(connection, kind) suppression — dismissing "intentional rate-limit on this source" doesn't silence auth alerts on the same connection. See [`steward/connector-health.md`](steward/connector-health.md). Ships in OSS.
- **Steward schema-drift detector** — first data-level Steward signal and first event-driven detector (drift detection happens at `POST /api/steward/schema-snapshot` time, not at scan time; findings then persist in a per-workspace JSONL journal so subsequent scans re-surface them until dismissed). Three change classes: `added` (P3, additive — surfaced for awareness), `dropped` (P1 — almost always breaks downstream), `type_changed` (P1 — downstream casts likely fail). Worst-case wins: any drop or type-change in a mixed diff escalates the whole finding to P1. First snapshot for a source establishes the baseline (never a finding). Same dismiss/resolve flow as every other finding kind; resolving with a `fix_note` becomes a `PROPOSED` lesson via the resolve→lesson loop. See [`steward/schema-drift.md`](steward/schema-drift.md). Ships in OSS.
- **Steward native data-quality checks** — event-driven surface for assertion-result reporting. External runners (F-Pulse executor, dbt test, Great Expectations checkpoint, Soda scan, custom probes) post failed assertions to `POST /api/steward/quality-check`; F-Pulse does NOT evaluate the assertions itself (read-only Rule 1) — it's the place results land. 12 supported check types — integrity (`not_null`, `unique`, `duplicate_key`, `referential_integrity`) escalate to P1 on any failure; non-integrity (`accepted_values`, `range`, `regex`, `freshness`, `row_count_*`, `partition_missing`, `custom`) default P2, escalate to P1 when >50% of rows failed. Findings map to the right FindingKind automatically (`null_spike`, `duplicate_key_spike`, `volume_anomaly`, `freshness_miss`, `partition_missing`, or generic `quality_check_failed`). Per-(source, check, column) suppression — dismissing "this dataset has known nulls in zip_code" only silences that combo. See [`steward/quality-checks.md`](steward/quality-checks.md). Ships in OSS.
- **Steward governance detectors (env_crossing + unapproved_destination)** — first governance-level Steward signals. Per-workspace policy at `governance.json` (managed via `GET`/`PUT /api/steward/governance`) maps connection IDs to environment tags and defines a sink allowlist. `env_crossing` (P1) fires when one workflow references connections tagged with multiple envs (dev/prod mixing — almost always a mistake). `unapproved_destination` (P2) fires when a sink references a connection not on the allowlist. Both ship as state-derived detectors that run at every scan against the same workflow snapshot Archeologist uses. Empty policy = no findings (safely-off default). PII-leak and credential-sprawl detectors deferred to a focused later session (need a curated regex catalog). See [`steward/governance.md`](steward/governance.md). Ships in OSS.
- **Steward cost / movement tracking + WAREHOUSE_WASTE + EMPTY_OUTPUT detectors** — first cost-level AND node-level Steward signals. External runners (F-Pulse executor, CI jobs, framework sidecars) post per-run cost events via `POST /api/steward/cost-event` (rows read/written, bytes, duration; optionally `workflow_id` + `node_id` for per-node observations). Two detectors activate from the same surface: `WAREHOUSE_WASTE` (P2, cost-level) fires when a source has been read 3+ times in a row producing zero output rows downstream; `EMPTY_OUTPUT` (P2, node-level) fires when a specific (workflow, node) tuple has produced zero rows 3 times in a row — catches a broken filter/join/transform even when upstream sources have data. A productive run resets the streak. Per-source aggregation rollup available via `GET /api/steward/cost-summary`. `cost_drift` and `cost_recommendation` deferred to the 1.3 Cost Steward + 2.0 Optimizer modules (need real baseline machinery). See [`steward/cost-tracking.md`](steward/cost-tracking.md). Ships in OSS.
- **Workspaces + role-based access control (RBAC)** — multi-user with per-workspace roles (`super_admin` / `workspace_admin` / `data_engineer` / `analyst` / `viewer`), uncapped seats on Free. Team-scale features (cross-workspace correlation, SSO, RBAC-aware approval chains) are Plus.

## Default storage and runtime

- Persistence: SQLite database at `$FPULSE_DATA_DIR/fpulse.db`
- Execution engine: in-process DuckDB
- Worker model: priority-aware in-process worker pool with five priority lanes
- Default concurrency: `FPULSE_MAX_CONCURRENT_RUNS` (defaults to 4)

## When this build is the right fit

This build runs as a single instance (one node). It's the right fit when:

- You are building or prototyping pipelines on your laptop or a single server.
- Your workloads sit comfortably below a single-node ceiling (see [scaling](scaling.md)).
- You don't need cross-workspace correlation, SSO, or RBAC-aware approval chains (the Plus team layer).
- Local-only execution and BYO-key cloud AI are sufficient for your privacy needs.

## Looking for team features?

F-Pulse+ is a paid extension for teams; see [hybridyn.com/f-pulse](https://hybridyn.com/f-pulse) for details.

## Pricing

This build is free under Apache 2.0. For commercial offerings, see [hybridyn.com/pricing](https://hybridyn.com/pricing).
