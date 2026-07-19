# F-Pulse concepts and terminology

## Pipeline

A pipeline is a directed graph of nodes that reads from sources, transforms data, and writes to sinks. Pipelines are persisted as versioned IR (intermediate representation) with a SHA-256 content hash on every save. Re-running a pipeline executes the same logic; restoring an old version replays the exact graph that ran.

A pipeline has a `status` lifecycle: `draft` → `testing` → `published` → `archived`. The frontend's Pipelines page lists all pipelines in the active workspace.

## Project

A project is a collection of pipelines sharing a logical purpose. Each pipeline belongs to exactly one project. Projects are workspace-scoped; you can move a pipeline between projects but not between workspaces.

## Workspace

A workspace is the top-level container. A single F-Pulse install can have multiple workspaces (Plus only — OSS Free has a single "default" workspace). All resources (pipelines, projects, schedules, alerts, connections, credentials, executions) are workspace-scoped.

## Environment: DEV vs PROD

Per `edition-matrix.md` line 60-61, environments split as follows:

**DEV** — OSS + Plus. The iterative sandbox. Pipelines run with `DEV_SAMPLE_ROWS` (50 by default) on source nodes for fast feedback. Most write operations are permitted.

**PROD environment + DEV→PROD promotion** — **F-Pulse+ only**. The PROD execution path with full-dataset runs and the two-gate approval workflow (Gate 1: developer requests; Gate 2: approver confirms after Sandbox run) is gated behind the Plus license.

**For OSS Free users**: DEV is the only environment in the legal contract. The frontend env-stripe may still let you toggle to a "PROD" view, but the supported, contracted execution path for OSS is DEV. If you need a production environment with approval gates, sandbox dry-run, and audit-log retention, that's F-Pulse+.

## Schedule

A schedule triggers a pipeline at a recurring time. Types: `cron` (full cron expression), `daily` (at HH:MM), `hourly`, `interval` (every N minutes), `once`. Schedules are workspace + environment scoped — a schedule fires in DEV or PROD, not both.

## Alert

An alert rule fires when a condition is met (`ON_FAILURE`, `ON_LONG_RUNNING`, `ON_SCHEDULE_MISS`). Channel: email, Discord, webhook, in-app notification. Alert logs persist for 30 days (or longer with F-Pulse+ retention policy).

## Connection

A connection is a reusable handle to an external system: database (Postgres / MySQL / etc.), REST API, cloud storage bucket, vector DB, or message queue. Connections store endpoint config + a reference to a credential. Pipelines reference connections by ID.

## Credential

A credential stores authentication material (password, API key, OAuth token, certificate). Credentials are encrypted at rest with **Fernet (AES-128-CBC + HMAC-SHA256)**. The master encryption key file lives at `~/.fpulse/secret.key` (or `$FPULSE_DATA_DIR/secret.key` if `FPULSE_DATA_DIR` is set). On POSIX it's chmod 600 and F-Pulse refuses to start if the file is world-readable.

Encryption is **always-on for both Free and Plus** (changed May 4 2026 — previous OSS versions stored credentials in plaintext on disk; upgrades re-encrypt on next save). Plus adds an additional path — the **Vault-Ref** pattern — where credentials live in an external vault (HashiCorp Vault, AWS Secrets Manager, etc.) and F-Pulse stores only a reference. See `07_credentials.md` for the operator-level guide.

The agent's `inspect_connections` tool returns connection metadata + key NAMES only — never key values.

## Variable

A workspace variable is a named scalar (string, int, bool, json) that pipelines reference via `$vars.NAME`. Variables have scope: `global` (workspace-wide) or `pipeline` (single pipeline).

## Execution

An execution is one run of a pipeline. Captured per row: status, started_at, completed_at, duration_ms, total_rows_processed, peak_memory_mb, cpu_seconds, parameter_values, workflow_snapshot (the IR that ran), exit_reason. Executions are immutable once written — they are historical / audit data.

The Executions page lists recent runs with filters by status, pipeline, environment.

## Run vs execution vs trigger

These terms overlap. Precise meaning:

- **Trigger** — what caused the run to start: `manual` (user clicked Run), `schedule` (a Schedule fired), `event` (an upstream pipeline emitted a complete event).
- **Run** / **execution** — used interchangeably. One invocation of a pipeline.
- **Run ID** — a UUID assigned at run start. Used as the join key in `pipeline_checkpoints` and for the Resume-from-step feature.

## Sandbox (Plus)

A Sandbox is a scratch namespace used in F-Pulse+ for the DEV → PROD promotion flow. Before promoting, F-Pulse runs the pipeline in Sandbox mode: every destination is rewritten to point at the scratch namespace, and source nodes get a row-limit hint. Lets reviewers verify behaviour against real upstream data without writing to production sinks.

## Two RBAC systems — easy to confuse

Per `edition-matrix.md` line 122-127, F-Pulse has TWO distinct RBAC systems. They serve different purposes; both can be active in Plus.

### 1. Agent-tool RBAC — OSS + Plus, always on

Gates **what the AI Copilot can call**. Four roles × two environments × three tool tiers (read / safe_write / high_impact_write). Even in OSS Free, the agent endpoint checks this matrix before invoking any tool. This is what shows up in error messages like "your role has no allowed tiers in env=prod".

Roles (agent-RBAC):
- `viewer` — read tools only, both envs
- `developer` — read + safe_write in DEV; read in PROD
- `admin` — all tiers in DEV; read + safe_write in PROD
- `super_admin` — all tiers in both envs

### 2. Workspace RBAC — F-Pulse+ only

Five-tier hierarchy that gates **who can edit what** in the workspace. Per-environment permissions, approval rights, seat limits.

Roles (workspace-RBAC, Plus only):
- **Super Admin** — install-wide
- **Workspace Admin** — owns the workspace, billing
- **Data Engineer** — write in DEV; PROD writes need approval
- **Analyst** — read everywhere; can approve PROD changes
- **Viewer** — read-only

OSS Free has a flat single-user model for this layer — no workspace-RBAC roles. The single operator is effectively the workspace admin. The agent-tool RBAC still applies; the agent treats the OSS operator as admin in DEV with read-only defaults in any PROD context.
