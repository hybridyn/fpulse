"""
SQLite persistence layer for F-Pulse.

Design: JSON-blob storage — each table has indexed columns for filtering
plus a `data` column with the full Pydantic model serialized as JSON.
Zero migration headaches when models evolve.

F-Pulse is a TOOL, not a service. All data lives on the user's machine.

Stage 3a (2026-04-19) — production SQLite tuning
─────────────────────────────────────────────────
WAL mode + busy_timeout were already on. Stage 3a adds the remaining
production pragmas, surfaces WAL stats for operator observability,
and tracks every per-thread connection so shutdown can drain them all
(previously close() only closed the calling thread's connection,
leaving worker / scheduler / backup-thread connections orphaned at
shutdown — visible as "database is locked" warnings on rapid restart).

Pragma rationale:
  • journal_mode=WAL          (already on) — concurrent reads + 1 writer
  • synchronous=NORMAL        new — recommended pairing with WAL.
                               FULL is the SQLite default but is
                               double-fsync overkill in WAL mode.
                               NORMAL is the documented WAL-safe value
                               and gives ~30% write throughput back.
  • busy_timeout=15000        (already on) — wait up to 15s on lock
  • foreign_keys=ON           (already on)
  • cache_size=-64000         new — 64 MB page cache (default is 2 MB).
                               Negative = KB. Our DB is small (<1 MB)
                               so this caches the entire DB.
  • temp_store=MEMORY         new — temp tables in RAM, not on disk
  • mmap_size=268435456       new — 256 MB read mmap window. Reads
                               that hit warm pages skip the syscall.

Reviewer 3 ops signal:
  WAL pages count is exposed via /api/health/memory. Operators can
  watch WAL grow (= writes piling up) and see the auto-checkpoint
  dropping it back. A WAL that grows monotonically signals a writer
  not committing or a reader holding a transaction open.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Schema version — bump when tables change
#
# v1 → v2 (Workspace foundation, Apr 8 2026):
#   * adds the `workspaces` and `workspace_members` tables
#   * back-fills a "Default" workspace and enrols every existing user
#     into it as a member, with the seeded `admin` user promoted to
#     workspace owner. This is the corporate-policy-friendly way to
#     turn an existing single-tenant install into the multi-tenant
#     model without losing data: every legacy row becomes "owned by
#     the Default workspace" and admins can later create new
#     workspaces and move things around.
#   * adds an indexed `workspace_id` column to `projects` so the
#     scoping query is fast (the JSON blob also stores it for
#     redundancy, but the column is what queries filter on).
#
# v2 → v3 (Signup default flip, Apr 9 2026):
#   * one-time flip of `allow_self_registration` from False → True on
#     installs that were created under the old default. We know the
#     value was never *explicitly* chosen by an admin because the
#     previous code path stamped it from a hard-coded default with no
#     "set by admin" marker — so flipping all existing False values is
#     safe. After the migration, the admin's POSTs to /plus/admin/
#     settings continue to be the source of truth.
#
# v3 → v4 (Self-heal projects.workspace_id, Apr 9 2026):
#   * the v2 migration was supposed to ALTER TABLE projects ADD COLUMN
#     workspace_id, but on at least one dev DB the version marker got
#     bumped to 2 without the column actually being present (likely the
#     ALTER raised before the marker was written, or v2 ran on a DB
#     where the projects table didn't yet exist and was later created
#     fresh from CREATE TABLE which doesn't include the column). v4 is
#     a self-healing pass that PRAGMA-checks for the column and adds it
#     if missing — same back-fill semantics as v2.
#
# v4 → v5 (Workflow workspace scoping, Apr 9 2026):
#   * adds a denormalised `workspace_id` column to `workflow_versions`
#     and back-fills every row to 'default'. The JSON blob inside
#     `data` already carries the Workflow.workspace_id field (also
#     back-filled), but the column lets `list_all(workspace_id=…)`
#     hit an index instead of scanning every latest-version row and
#     parsing JSON to filter. Uses the same PRAGMA-based self-heal
#     pattern as v4 so re-running is safe.
#
# v5 → v6 (Connection workspace scoping, Apr 9 2026):
#   * adds a denormalised `workspace_id` column to `connections` and
#     back-fills every row to 'default'. Important nuance: a
#     connection whose project_id is NULL ("Global") is still scoped
#     to a single workspace — "global" means "visible to every
#     project WITHIN this workspace", NOT "visible across workspaces".
#     The JSON blob carries Connection.workspace_id too, which the
#     migration back-fills by rewriting legacy blobs in place.
#
# v6 → v7 (Credentials workspace scoping, Apr 9 2026):
#   * adds a denormalised `workspace_id` column to `credentials` and
#     back-fills every row to 'default'. Secrets are the highest-
#     sensitivity tenant data on the platform — a credential whose
#     project_id is "" ("global") is still locked to a single
#     workspace. Same back-fill semantics as v6: PRAGMA self-heal,
#     in-place JSON blob rewrite, skip rows that already carry a
#     non-empty workspace_id so admin-set values survive re-runs.
#
# v7 → v8 (Schedules workspace scoping, Apr 9 2026):
#   * adds a denormalised `workspace_id` column to `schedules` and
#     back-fills every row to 'default'. User-facing API endpoints
#     filter by the caller's workspace_id; the background scheduler
#     loop keeps iterating every workspace because it runs as a
#     system-level process with no user context. Same PRAGMA self-
#     heal + in-place JSON blob rewrite as v6/v7.
#
# v8 → v9 (Alerts workspace scoping, Apr 9 2026):
#   * adds a denormalised `workspace_id` column to BOTH `alert_rules`
#     and `alert_logs`, back-filling to 'default'. Logs inherit the
#     workspace from their parent rule at write time and never change
#     after being written — an alert log is an immutable audit record.
#
# v9 → v10 (Executions workspace scoping, Apr 9 2026):
#   * adds a denormalised `workspace_id` column to `executions` and
#     back-fills to 'default'. Execution records are immutable audit
#     history — the workspace stamped at creation time is final.
#
# v10 → v11 (Variables workspace scoping, Apr 9 2026):
#   * adds a denormalised `workspace_id` column to `variables` and
#     back-fills to 'default'. Crucial nuance: scope='global' means
#     "global within workspace", not "global across tenants". The
#     resolve() method enforces the boundary on both project-scoped
#     and global lookups.
#
# v11 → v12 (Lifecycle events workspace scoping, Apr 9 2026):
#   * adds a denormalised `workspace_id` column to `lifecycle_events`
#     and back-fills. Events inherit workspace from parent workflow
#     at write time; they're immutable audit records afterwards.
#
# v12 → v13 (Schema contracts workspace scoping, Apr 9 2026):
#   * adds a denormalised `workspace_id` column to `schema_contracts`
#     and back-fills from the parent workflow via workflow_versions.
#     Contracts inherit workspace from the parent workflow at create
#     time and cannot be moved between workspaces afterwards.
#
# v13 → v14 (AI provider configuration, Apr 21 2026):
#   * adds two new tables:
#       - `user_ai_config` — per-user provider (Free/OSS tier)
#       - `workspace_ai_config` — workspace-wide provider (Plus tier)
#     Both store provider/model/base_url plus an encrypted api_key
#     (encrypted, "ENC:..." format). Resolution order at
#     request time: workspace_ai_config → user_ai_config → env vars
#     → stub mode. `allow_user_override` on the workspace row lets
#     Plus admins decide whether per-user overrides are honoured.
#     Tables are created purely additively via CREATE TABLE IF NOT
#     EXISTS — safe on fresh and upgrade paths alike.
#
# v14 → v15 (Signed artifacts — workflow content hash, Apr 22 2026):
#   * adds a `content_hash` column to `workflow_versions`. Every save
#     computes SHA-256 over the canonical JSON of the workflow body
#     (sort_keys=True) and stores the hex digest. Enterprise buyer
#     story: "prove this deployed version exactly matches what was
#     approved". Rollback verifies the stored hash re-matches what it
#     gets when re-hashing the stored blob; mismatch = 409 Conflict
#     (tamper or corruption detected). Legacy rows (pre-migration)
#     get empty hash and skip verification — avoids back-fill compute
#     on upgrade. Column is NULL-tolerant so the migration is cheap.
#     Memory cost: ~0 — one 32-byte hash per version, never buffered.
#
# v22 → v23 (Pipeline checkpoints — Sprint 1, May 4 2026):
#   * adds a `pipeline_checkpoints` table that records, per-(run_id, step_id),
#     the success/failure status, row counts, duration, and the path to the
#     Parquet snapshot written by the existing StepCache. This is the
#     persistent index the executor's "Resume from step X" feature reads to
#     pick up a failed run from the first non-success step instead of from
#     scratch. Independent of the StepCache manifest.json (which is keyed by
#     workflow_id + effective_hash for "Rerun from here") because resume is
#     keyed by run_id and cares about the actual sequence of step outcomes
#     for a specific run, not about content-addressable hash matches.
#     Idempotent: CREATE TABLE IF NOT EXISTS only — no data move needed on
#     upgrade because there is nothing to back-fill.
#
# v27 → v28 (Schema history for managed-table policy decisions, 2026-05-27):
#   * adds a `schema_history` table — one row per applied schema change
#     to a managed table. Driven by the new schema_policy enforcement
#     path in the local_table / db / warehouse sinks. Pure additive,
#     no back-fill: pre-existing tables get their first history row
#     on the next sink write under any non-strict policy.
#   * see fpulse/intelligence/schema_history.py for the store wrapper
#     and fpulse/intelligence/schema_policy.py for the enforcement
#     evaluator.
#
# v28 → v29 (Backfill runs, 2026-05-27):
#   * adds a `backfill_runs` table — chunked re-execution of a pipeline
#     over a historical date range. One parent row per backfill carries
#     the user-facing config (start/end/window_size/concurrency/
#     on_failure); one child row per time window carries the cursor
#     parameter binding + the execution_id of the dispatched run. Both
#     shapes share the table; parent_backfill_id = '' means parent.
#   * pure additive — pre-existing installs get no back-fill because
#     there is no prior backfill state to recover. See
#     fpulse/backfills/ for the orchestrator + store + idempotency
#     guardrail.
#
# v29 → v30 (Sink idempotency keys, 2026-05-27):
#   * adds the `sink_idempotency` table — per-(pipeline, sink_step, hash)
#     marker that an external sink (email/webhook/api/kafka/slack) has
#     already fired its side effect for a given row. Lets re-runs,
#     retries, and acknowledged backfills skip the duplicate sends that
#     the frontend's red idempotency badge warns about.
#   * pure additive — no pre-existing state to migrate. The table is
#     populated lazily the first time a user sets `idempotency_key` on
#     a sink and re-runs.
#   * TTL behaviour + skip semantics live in fpulse/sinks/dedupe_store.py;
#     the per-sink wiring lives in fpulse/sinks/idempotency_helper.py.
SCHEMA_VERSION = 32

# All table definitions
TABLES = """
-- Projects
-- NOTE: workspace_id column is added by the v2/v4 migrations rather
-- than declared here. CREATE TABLE IF NOT EXISTS only runs on fresh
-- installs, but the v4 self-heal migration also adds the index on
-- existing rows, so both paths converge on the same shape.
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_id TEXT,
    workspace_id TEXT DEFAULT 'default',
    data JSON NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_name ON projects(name);

-- Folders — nested grouping of pipelines inside a project.
-- Tree shape via parent_folder_id ('' = sits at the project root).
-- Fresh installs get this table directly; upgrades go through the v24
-- migration (which also adds the projects.parent_id column).
CREATE TABLE IF NOT EXISTS folders (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_id TEXT NOT NULL,
    parent_folder_id TEXT DEFAULT '',
    workspace_id TEXT DEFAULT 'default',
    data JSON NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_folders_project ON folders(project_id);
CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_folder_id);
CREATE INDEX IF NOT EXISTS idx_folders_workspace ON folders(workspace_id);

-- Workflow versions (one row per version)
-- NOTE: workspace_id is declared here for fresh installs; on existing
-- DBs the v5 migration adds it via ALTER TABLE. The index is created
-- in the migration (NOT here) because CREATE INDEX on a column that
-- doesn't yet exist on an existing table would fail at startup.
CREATE TABLE IF NOT EXISTS workflow_versions (
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    data JSON NOT NULL,
    created_by TEXT DEFAULT 'user',
    change_summary TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    workspace_id TEXT DEFAULT 'default',
    content_hash TEXT DEFAULT '',
    PRIMARY KEY (workflow_id, version)
);
CREATE INDEX IF NOT EXISTS idx_wv_workflow ON workflow_versions(workflow_id);

-- Schedules
-- Schedules
-- NOTE: workspace_id is added by the v8 migration on upgrades; the
-- column lives here for fresh installs. Scheduler background loop
-- iterates every workspace (system-level), but user-facing APIs
-- filter by caller's workspace_id.
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    project_id TEXT DEFAULT 'default',
    workspace_id TEXT DEFAULT 'default',
    enabled INTEGER DEFAULT 1,
    data JSON NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_workflow ON schedules(workflow_id);
CREATE INDEX IF NOT EXISTS idx_schedules_project ON schedules(project_id);

-- Alert rules
-- Alert rules
-- NOTE: workspace_id added by v9 migration on upgrade; declared
-- inline here for fresh installs.
CREATE TABLE IF NOT EXISTS alert_rules (
    id TEXT PRIMARY KEY,
    workflow_id TEXT,
    project_id TEXT DEFAULT 'default',
    workspace_id TEXT DEFAULT 'default',
    enabled INTEGER DEFAULT 1,
    data JSON NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_workflow ON alert_rules(workflow_id);
CREATE INDEX IF NOT EXISTS idx_alerts_project ON alert_rules(project_id);

-- Alert logs
CREATE TABLE IF NOT EXISTS alert_logs (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    workspace_id TEXT DEFAULT 'default',
    data JSON NOT NULL,
    triggered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_logs_rule ON alert_logs(rule_id);
CREATE INDEX IF NOT EXISTS idx_alert_logs_workflow ON alert_logs(workflow_id);

-- Executions
-- Executions
-- NOTE: workspace_id added by v10 migration on upgrades. Execution
-- records are immutable audit history — once written, workspace
-- never changes even if the parent workflow is moved/deleted.
-- v17 added the budget/actual columns (PR5 step 7). All NULL-tolerant
-- on existing rows; new writes populate whichever fields are
-- available (subprocess runs have memory_peak_mb, thread runs only
-- have runtime_ms, etc.).
CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    project_id TEXT DEFAULT 'default',
    workspace_id TEXT DEFAULT 'default',
    status TEXT DEFAULT 'running',
    data JSON NOT NULL,
    started_at TEXT NOT NULL,
    budget_memory_mb INTEGER,
    budget_runtime_s INTEGER,
    budget_max_attempts INTEGER,
    memory_peak_mb REAL,
    runtime_ms REAL,
    attempts INTEGER,
    exit_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_executions_workflow ON executions(workflow_id);
CREATE INDEX IF NOT EXISTS idx_executions_project ON executions(project_id);
CREATE INDEX IF NOT EXISTS idx_executions_started ON executions(started_at);
-- idx_executions_exit_reason is created by _migrate_v17_execution_budget
-- rather than here: on a pre-v17 upgrade the column doesn't exist yet
-- when executescript(TABLES) runs, which crashes the whole init. The
-- v17 migration ALTERs the column first, then creates the index.

-- Sandbox runs (PR10 — PROD deploy-preview environment)
-- An ephemeral execution tagged "sandbox" that reads PROD-class data via
-- real connections but writes only to a scratch namespace. Triggered by an
-- approver during the review of a pending deploy. Auto-purges 24h after
-- creation OR on approve/reject (whichever comes first). See
-- DESIGN_PROD_SANDBOX.md for the load-bearing invariants (I1-I10).
CREATE TABLE IF NOT EXISTS sandbox_runs (
    id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    execution_id TEXT,
    scratch_namespace TEXT NOT NULL,
    scratch_paths TEXT,                       -- JSON list of created paths/tables
    row_limit INTEGER NOT NULL DEFAULT 10000,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued|running|success|failed|cleaned
    triggered_by TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    finished_at TEXT,
    cleanup_at TEXT NOT NULL,
    cleaned_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_sandbox_runs_approval ON sandbox_runs(approval_id);
CREATE INDEX IF NOT EXISTS idx_sandbox_runs_cleanup ON sandbox_runs(cleanup_at) WHERE cleaned_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sandbox_runs_status ON sandbox_runs(status);

-- Lifecycle toggle requests (PR12 — Activate / Deactivate approval)
-- One row per pending request; decided rows kept for audit.
CREATE TABLE IF NOT EXISTS lifecycle_toggle_requests (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    action TEXT NOT NULL,
    target_env TEXT NOT NULL DEFAULT 'prod',
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    decided_by TEXT,
    decided_at TEXT,
    decision_notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_toggle_workflow ON lifecycle_toggle_requests(workflow_id);
CREATE INDEX IF NOT EXISTS idx_lifecycle_toggle_pending ON lifecycle_toggle_requests(status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_lifecycle_toggle_workspace ON lifecycle_toggle_requests(workspace_id);

-- Workspace settings (PR11 — enforce_two_person_approval, etc.)
CREATE TABLE IF NOT EXISTS workspace_settings (
    workspace_id TEXT PRIMARY KEY,
    settings JSON NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    updated_by TEXT
);

-- Pool allocations (PR14 — per-workspace logical pool split)
-- Three slices of total worker capacity that ExecutionManager enforces
-- at admit time. CHECK ensures the three percentages always sum to 100.
CREATE TABLE IF NOT EXISTS pool_allocations (
    workspace_id TEXT PRIMARY KEY,
    prod_reserved_pct INTEGER NOT NULL DEFAULT 60,
    dev_reserved_pct INTEGER NOT NULL DEFAULT 20,
    burst_pct INTEGER NOT NULL DEFAULT 20,
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    CHECK (prod_reserved_pct >= 0 AND prod_reserved_pct <= 100),
    CHECK (dev_reserved_pct  >= 0 AND dev_reserved_pct  <= 100),
    CHECK (burst_pct         >= 0 AND burst_pct         <= 100),
    CHECK (prod_reserved_pct + dev_reserved_pct + burst_pct = 100)
);

-- Users
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    data JSON NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- Sessions
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    data JSON NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- Variables
-- Variables
-- NOTE: workspace_id added by v11 migration on upgrades.
CREATE TABLE IF NOT EXISTS variables (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    scope TEXT DEFAULT 'global',
    project_id TEXT DEFAULT '',
    workspace_id TEXT DEFAULT 'default',
    data JSON NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_variables_key ON variables(key);
CREATE INDEX IF NOT EXISTS idx_variables_scope ON variables(scope);

-- Credentials
-- Credentials
-- NOTE: workspace_id is added to this table by the v7 migration on
-- upgrade installs; on a fresh install CREATE TABLE creates it directly
-- via the column below. Same pattern as connections/projects.
CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    project_id TEXT DEFAULT '',
    workspace_id TEXT DEFAULT 'default',
    data JSON NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credentials_type ON credentials(type);

-- Connections
-- NOTE: workspace_id is added to this table by the v6 migration on
-- existing DBs; declared here so fresh installs get it up front. The
-- index is created in the migration, not here, because CREATE INDEX
-- on a column that doesn't yet exist on an existing table fails at
-- startup (same lesson as workflow_versions in v5).
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    project_id TEXT,
    workspace_id TEXT DEFAULT 'default',
    data JSON NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_connections_type ON connections(type);
CREATE INDEX IF NOT EXISTS idx_connections_project ON connections(project_id);

-- Connection reports
CREATE TABLE IF NOT EXISTS connection_reports (
    id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    data JSON NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (connection_id) REFERENCES connections(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_conn_reports_conn ON connection_reports(connection_id);

-- Lifecycle events
-- Lifecycle events
-- NOTE: workspace_id added by v12 migration on upgrades.
CREATE TABLE IF NOT EXISTS lifecycle_events (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    workspace_id TEXT DEFAULT 'default',
    event TEXT NOT NULL,
    data JSON NOT NULL,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_workflow ON lifecycle_events(workflow_id);

-- Schema contracts
-- NOTE: workspace_id added by v13 migration on upgrades.
CREATE TABLE IF NOT EXISTS schema_contracts (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    workspace_id TEXT DEFAULT 'default',
    step_id TEXT NOT NULL,
    data JSON NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contracts_workflow ON schema_contracts(workflow_id);
CREATE INDEX IF NOT EXISTS idx_contracts_step ON schema_contracts(step_id);
-- NOTE: idx_contracts_workspace is created by the v13 migration, NOT here.
-- Keeping it in this top-level script would crash startup on any dev DB
-- whose schema_contracts table predates the workspace_id column: the
-- CREATE TABLE IF NOT EXISTS above is a no-op on an existing table (so
-- the column is never added here), and then a CREATE INDEX referencing
-- a missing column raises sqlite3.OperationalError before any migration
-- has had a chance to run. The v13 migration adds the column AND the
-- index idempotently via _add_workspace_id_column("schema_contracts", 13).

-- Settings (admin, system preferences)
-- 2026-05-27 (v27): added updated_at so writers can stamp last-touch time
-- without ALTER TABLE on every upgrading install. Fresh installs land here
-- directly; upgrading installs pass through _migrate_v27_settings_updated_at.
CREATE TABLE IF NOT EXISTS settings (
    id TEXT PRIMARY KEY,
    data JSON NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT ''
);

-- ── Workspace datastore (schema v25, 2026-05-23) ───────────────────────
-- Three tables that index the user-visible "Storage" surface backing
-- the Storage page. Bytes live on the filesystem under FPULSE_DATA_DIR;
-- these tables are the queryable metadata layer.
--
-- storage_objects: one row per file (upload | output | trash)
-- storage_tables:  one row per managed Parquet table (schema.name)
-- storage_columns: cached column rows for both tables AND objects
--
-- See fpulse/datastore/ for the module that drives these tables.
-- Migration v25 mirrors these CREATE-IF-NOT-EXISTS statements so an
-- upgrading install lands on the same shape as a fresh one.
CREATE TABLE IF NOT EXISTS storage_objects (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    kind TEXT NOT NULL DEFAULT 'file',     -- 'file' | 'output'
    project_id TEXT DEFAULT '',            -- '' = workspace-global
    folder_id TEXT DEFAULT '',             -- v26 (Y15): '' = at project root or global
    pipeline_id TEXT DEFAULT '',           -- set on kind='output'
    deleted_at TEXT DEFAULT '',            -- '' / NULL = live row; iso ts = soft-deleted
    data JSON NOT NULL                     -- full StorageObject Pydantic model
);
CREATE INDEX IF NOT EXISTS idx_storage_objects_ws ON storage_objects(workspace_id);
CREATE INDEX IF NOT EXISTS idx_storage_objects_kind ON storage_objects(workspace_id, kind);
CREATE INDEX IF NOT EXISTS idx_storage_objects_pipeline ON storage_objects(pipeline_id);
CREATE INDEX IF NOT EXISTS idx_storage_objects_deleted ON storage_objects(workspace_id, deleted_at);
-- NOTE: idx_storage_objects_folder is created by the v26 migration, NOT here.
-- Keeping it in this top-level script would crash startup on any dev DB
-- whose storage_objects table predates the folder_id column: the
-- CREATE TABLE IF NOT EXISTS above is a no-op on an existing table (so
-- the column is never added here), and then CREATE INDEX referencing
-- a missing column raises sqlite3.OperationalError before any migration
-- has had a chance to run. The v26 migration adds the column AND the
-- index idempotently. Same lesson as schema_contracts in v13.

CREATE TABLE IF NOT EXISTS storage_tables (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    schema_name TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL,
    data JSON NOT NULL                     -- full StorageTable Pydantic model
);
-- Composite unique index enforces (workspace, schema, name) identity.
-- Two tables with the same triple shouldn't coexist; the API + sink
-- node check this before insert but the DB constraint is the
-- defence-in-depth backstop.
CREATE UNIQUE INDEX IF NOT EXISTS uq_storage_tables_name
    ON storage_tables(workspace_id, schema_name, name);
CREATE INDEX IF NOT EXISTS idx_storage_tables_ws ON storage_tables(workspace_id);

CREATE TABLE IF NOT EXISTS storage_columns (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    table_id TEXT NOT NULL DEFAULT '',     -- '' when row describes a file
    object_id TEXT NOT NULL DEFAULT '',    -- '' when row describes a table
    data JSON NOT NULL                     -- full StorageColumn Pydantic model
);
CREATE INDEX IF NOT EXISTS idx_storage_columns_table ON storage_columns(table_id);
CREATE INDEX IF NOT EXISTS idx_storage_columns_object ON storage_columns(object_id);

-- Drift events (schema v16)
-- Per-occurrence record of detected divergence between declared state and
-- observed/recomputed state. One row per (workspace, item, kind) until
-- the operator resolves it; resolution either marks it fixed or accepts
-- the new state as baseline. SCAN writes new rows, never updates — the
-- timeline of detections is itself useful audit data.
CREATE TABLE IF NOT EXISTS drift_events (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    item_type TEXT NOT NULL,        -- workflow | schedule | connection | pool
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL,             -- hash_mismatch | schedule_orphan | ...
    severity TEXT NOT NULL DEFAULT 'warning',  -- info | warning | critical
    detected_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution TEXT,                -- fixed | accepted | dismissed
    details JSON NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_drift_workspace ON drift_events(workspace_id);
CREATE INDEX IF NOT EXISTS idx_drift_open ON drift_events(workspace_id, resolved_at);
CREATE INDEX IF NOT EXISTS idx_drift_item ON drift_events(item_type, item_id);

-- Schema history (schema v28, 2026-05-27)
-- One immutable row per applied schema change to a managed table. The
-- sink path consults schema_policy on every write; when the incoming
-- shape differs from the existing shape AND the policy accepts the
-- change, the sink appends a row here BEFORE the bytes land — so a
-- failed write leaves a "we were about to do X" audit trail rather
-- than an undocumented schema mutation.
--
-- version is a per-table monotonic counter: the sink reads MAX(version)
-- for the table and writes MAX+1. The columns_json blob is the FULL
-- column list at this version (not just the diff), so the API can
-- render any historical shape without replaying the diff chain.
CREATE TABLE IF NOT EXISTS schema_history (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    table_id TEXT NOT NULL,             -- storage_tables.id of the managed table
    version INTEGER NOT NULL,           -- per-table monotonic counter (1, 2, 3, ...)
    recorded_at TEXT NOT NULL,
    columns_json JSON NOT NULL,         -- full column list at this version
    change_summary JSON NOT NULL DEFAULT '{}',  -- adds/drops/widens + policy + severity
    applied_by_run_id TEXT DEFAULT '',  -- the run_id that produced this version
    policy TEXT NOT NULL DEFAULT 'add_columns'  -- the schema_policy that applied
);
CREATE INDEX IF NOT EXISTS idx_schema_history_table ON schema_history(table_id, version);
CREATE INDEX IF NOT EXISTS idx_schema_history_ws ON schema_history(workspace_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_schema_history_table_version
    ON schema_history(table_id, version);

-- Backfill runs (schema v29, 2026-05-27)
-- Chunked re-execution of a pipeline over a historical date range. The
-- table holds two row shapes: parent rows (parent_backfill_id = '') carry
-- the user-facing config and aggregate status; child rows
-- (parent_backfill_id = <parent.id>) carry one row per time window with
-- the cursor parameter binding and the execution_id of the dispatched
-- run. ``data`` is the full BackfillRun JSON blob; the indexed columns
-- exist so the API can list parents + drill into children without
-- scanning JSON.
CREATE TABLE IF NOT EXISTS backfill_runs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    pipeline_id TEXT NOT NULL,
    parent_backfill_id TEXT NOT NULL DEFAULT '',  -- '' = parent row
    status TEXT NOT NULL DEFAULT 'pending',       -- pending|running|success|failed|partial|cancelled|skipped
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    data JSON NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backfill_runs_ws ON backfill_runs(workspace_id);
CREATE INDEX IF NOT EXISTS idx_backfill_runs_pipeline ON backfill_runs(pipeline_id);
CREATE INDEX IF NOT EXISTS idx_backfill_runs_parent ON backfill_runs(parent_backfill_id);
CREATE INDEX IF NOT EXISTS idx_backfill_runs_status ON backfill_runs(status);

-- ── Workspaces (multi-tenant foundation, schema v2) ─────────────────────
-- A Workspace is the unit of isolation: projects, pipelines, members,
-- and (in Plus) the license activation all live inside one. Every
-- existing F-Pulse install gets a single "Default" workspace via the
-- v2 migration so the introduction of the entity is invisible to
-- single-tenant users — they don't have to know workspaces exist
-- unless they choose to create a second one.
--
-- Corporate-policy notes:
--   • plan column lets the same install run a free workspace next to
--     a Plus workspace (e.g. one shared instance, two business units).
--   • domain_allowlist is a JSON-encoded list of email domains that
--     are permitted to self-join via /request-access. Empty = no
--     domain restriction. This is the simplest way to satisfy
--     "only @company.com users can join" without standing up SSO.
--   • is_personal flags an auto-generated "personal" workspace owned
--     by a single user. Used to suppress these from corporate
--     workspace lists and to gate the export-to-corporate flow.
--   • All scoped queries MUST filter on workspace_id; the helper
--     `Database.workspace_filter()` exists to enforce that at the
--     storage layer rather than relying on every API author to
--     remember.
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT UNIQUE,                  -- url-safe short name; unique per install
    plan TEXT DEFAULT 'free',          -- 'free' | 'plus'
    is_personal INTEGER DEFAULT 0,     -- 1 = auto-created personal workspace
    owner_id TEXT,                     -- user.id of the workspace owner
    domain_allowlist JSON DEFAULT '[]',-- list of email domains permitted to join
    settings JSON DEFAULT '{}',        -- per-workspace overrides (audit, sso, etc.)
    data JSON NOT NULL,                -- full Workspace pydantic model
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workspaces_owner ON workspaces(owner_id);
CREATE INDEX IF NOT EXISTS idx_workspaces_plan ON workspaces(plan);

-- Workspace membership — many-to-many between users and workspaces.
-- A user can belong to multiple workspaces (personal + one or more
-- corporate workspaces) and have different roles in each. The role
-- column is the per-workspace RBAC role, distinct from the user's
-- "instance role" on the user record (which only governs cross-
-- workspace operations like creating new workspaces).
CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'developer',  -- viewer|developer|lead|admin|super_admin
    invited_by TEXT,                         -- user.id of the inviter
    invited_at TEXT,
    accepted_at TEXT,                        -- null = pending invite
    PRIMARY KEY (workspace_id, user_id),
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_wsm_user ON workspace_members(user_id);

-- Approval gate configuration — per-pipeline or per-project
CREATE TABLE IF NOT EXISTS approval_gates (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT 'pipeline',   -- 'pipeline' | 'project' | 'global'
    scope_id TEXT NOT NULL DEFAULT '',         -- pipeline_id, project_id, or '' for global
    enabled INTEGER NOT NULL DEFAULT 1,
    min_approvals INTEGER NOT NULL DEFAULT 1,
    approvers TEXT NOT NULL DEFAULT '[]',      -- JSON array of user IDs/emails
    notify_channels TEXT NOT NULL DEFAULT '[]', -- JSON array: ['email','slack','in_app']
    created_by TEXT,
    created_at TEXT,
    updated_at TEXT,
    workspace_id TEXT NOT NULL DEFAULT 'default'
);
CREATE INDEX IF NOT EXISTS idx_ag_scope ON approval_gates(scope, scope_id, workspace_id);

-- Approval notifications / cards inbox
CREATE TABLE IF NOT EXISTS approval_notifications (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    workflow_name TEXT NOT NULL,
    recipient_id TEXT NOT NULL,               -- user who should see it
    sender_id TEXT NOT NULL DEFAULT '',        -- who triggered the action
    action TEXT NOT NULL,                      -- 'submitted' | 'approved' | 'rejected' | 'changes_requested'
    message TEXT NOT NULL DEFAULT '',
    card_data TEXT NOT NULL DEFAULT '{}',      -- JSON: full pipeline metadata snapshot
    read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    workspace_id TEXT NOT NULL DEFAULT 'default'
);
CREATE INDEX IF NOT EXISTS idx_an_recipient ON approval_notifications(recipient_id, read);
CREATE INDEX IF NOT EXISTS idx_an_workflow ON approval_notifications(workflow_id);

-- AI provider config (per-user) — Free/OSS tier
-- Users configure their own LLM provider from AccountPage. API key is
-- encrypted ("ENC:..." format). When the workspace AI config has
-- allow_user_override = 0,
-- rows here are ignored in favour of the workspace-wide config.
CREATE TABLE IF NOT EXISTS user_ai_config (
    user_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    enabled INTEGER NOT NULL DEFAULT 0,
    provider TEXT DEFAULT '',
    model TEXT DEFAULT '',
    api_key_encrypted TEXT DEFAULT '',
    base_url TEXT DEFAULT '',
    -- v32: optional reference to a row in the `credentials` store. When
    -- set, the API key is resolved from that credential at request time
    -- and `api_key_encrypted` is left blank (single source of truth).
    credential_id TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_ai_config_ws ON user_ai_config(workspace_id);

-- AI provider config (workspace-wide) — Plus tier
-- Admin-configured from AdminPage. All non-admin users inherit this
-- unless `allow_user_override` = 1. `monthly_budget_usd` = 0 means no
-- cap; any positive value triggers soft alert at 80% and hard stop
-- at 100% (enforced at call-site, not here).
CREATE TABLE IF NOT EXISTS workspace_ai_config (
    workspace_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    provider TEXT DEFAULT '',
    model TEXT DEFAULT '',
    api_key_encrypted TEXT DEFAULT '',
    base_url TEXT DEFAULT '',
    -- v32: optional reference to a row in the `credentials` store (see
    -- user_ai_config.credential_id). Lets a workspace point its AI
    -- provider at a governed credential instead of an inline key.
    credential_id TEXT DEFAULT '',
    allow_user_override INTEGER NOT NULL DEFAULT 0,
    monthly_budget_usd REAL NOT NULL DEFAULT 0,
    configured_by TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- User templates — pipelines saved as reusable templates by users.
-- Workspace-scoped so OSS Free's single user gets a personal library
-- and Plus tier can later reuse the same table for shared workspace
-- templates. Stored as the canvas IR (steps + connections) plus the
-- gallery metadata (tagline / description / category) the TemplatesPage
-- needs to render the card. No FK to workflows: a user template is
-- a snapshot, decoupled from the source workflow's lifecycle.
CREATE TABLE IF NOT EXISTS user_templates (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL,
    tagline TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'Custom',
    data JSON NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_templates_ws ON user_templates(workspace_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_templates_ws_name ON user_templates(workspace_id, name);

-- Sink idempotency markers (v30, 2026-05-27)
-- One row per (pipeline_id, sink_step_id, key_hash) that an external
-- sink has already fired for. Lookups happen per row on every external
-- sink fire when the user has set `idempotency_key` on the sink, so the
-- compound index is what keeps the hot path cheap.
CREATE TABLE IF NOT EXISTS sink_idempotency (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_id TEXT NOT NULL,
    sink_step_id TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    UNIQUE(pipeline_id, sink_step_id, key_hash)
);
CREATE INDEX IF NOT EXISTS idx_sink_idempotency_lookup
    ON sink_idempotency(pipeline_id, sink_step_id, key_hash);
CREATE INDEX IF NOT EXISTS idx_sink_idempotency_expires
    ON sink_idempotency(expires_at);

-- ── Incremental sync state (schema v31, 2026-05-30) ─────────────────────
-- Per (workflow, step) cursor watermark for sources running in
-- sync_mode=incremental. The source node auto-loads `last_cursor` at
-- the start of each run (so the operator doesn't have to type the
-- watermark by hand) and writes back the max it observed at the end.
-- The Reset State action on the UI deletes the row so the next run
-- behaves like a full refresh again. See fpulse/engine/sync_state_store.py.
CREATE TABLE IF NOT EXISTS sync_state (
    workflow_id    TEXT NOT NULL,
    step_id        TEXT NOT NULL,
    cursor_column  TEXT NOT NULL,
    last_cursor    TEXT,                       -- string-coerced; type owned by the source's column
    last_run_at    TIMESTAMP,
    rows_last_run  INTEGER DEFAULT 0,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workflow_id, step_id)
);
CREATE INDEX IF NOT EXISTS idx_sync_state_workflow
    ON sync_state(workflow_id);

-- Metadata table for schema version tracking
CREATE TABLE IF NOT EXISTS _meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    """SQLite database manager for F-Pulse.

    Thread-safe. Each thread gets its own connection via thread-local
    storage. WAL mode + production pragmas applied per-connection.

    Stage 3a: every connection is also tracked in a regular set so
    close() can drain ALL threads' connections at shutdown, not just
    the caller's. (We can't use WeakSet because sqlite3.Connection
    objects don't expose __weakref__.) The cost — holding references
    to connections from threads that exit before close() — is
    negligible in practice because every F-Pulse thread is
    long-lived: worker_pool workers, scheduler, backup_scheduler,
    and uvicorn's reused request threads. Each connection is ~10 KB.
    """

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            data_dir = os.environ.get("FPULSE_DATA_DIR", os.path.join(os.getcwd(), "data"))
            db_path = os.path.join(data_dir, "fpulse.db")

        self.db_path = db_path
        self._local = threading.local()
        # Stage 3a: track every per-thread connection so close() can
        # drain them all at shutdown. Regular set (not WeakSet)
        # because sqlite3.Connection has no __weakref__ slot.
        self._all_conns: set[sqlite3.Connection] = set()
        self._all_conns_lock = threading.Lock()

        # Ensure directory exists
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        # Initialize schema
        self._init_schema()

        logger.info("F-Pulse database: %s (schema target=%d)", db_path, SCHEMA_VERSION)

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        """Apply the canonical production pragma set to a fresh connection.

        Must run after sqlite3.connect() and before any user query.
        Each pragma is wrapped so a SQLite build that doesn't recognise
        one (rare — these are core pragmas since 3.7) doesn't break
        startup. Order matters: journal_mode must come before
        synchronous so the latter is interpreted in WAL context.
        """
        # journal_mode and synchronous are the load-bearing pair; the
        # others are perf wins that we want but can survive without.
        critical = (
            ("journal_mode", "WAL"),
            ("synchronous", "NORMAL"),
            ("foreign_keys", "ON"),
            ("busy_timeout", "15000"),
        )
        for name, value in critical:
            try:
                conn.execute(f"PRAGMA {name}={value}")
            except sqlite3.DatabaseError as exc:
                logger.warning(
                    "F-Pulse: PRAGMA %s=%s failed (continuing): %s",
                    name, value, exc,
                )

        # Performance pragmas — every one of these is non-fatal.
        # cache_size: negative value means KB (so -64000 = 64 MB),
        # positive means pages (depends on page_size). Negative is
        # the portable form recommended by the SQLite docs.
        perf = (
            ("cache_size", "-64000"),    # 64 MB page cache
            ("temp_store", "MEMORY"),    # temp tables / indexes in RAM
            ("mmap_size", "268435456"),  # 256 MB read mmap window
        )
        for name, value in perf:
            try:
                conn.execute(f"PRAGMA {name}={value}")
            except sqlite3.DatabaseError as exc:
                logger.debug(
                    "F-Pulse: PRAGMA %s=%s skipped (non-critical): %s",
                    name, value, exc,
                )

    @property
    def conn(self) -> sqlite3.Connection:
        """Get thread-local SQLite connection. Creates + tunes on first use.

        `check_same_thread=False` allows the lifespan shutdown path to
        close connections opened by request-handling threads. Routine
        operations stay on their original thread (each thread has its
        own `self._local.conn`), so the only cross-thread access is
        `close()` during shutdown — safe because no other thread is
        using the connection at that point. Without this flag, the
        shutdown path emits 'SQLite objects created in a thread can
        only be used in that same thread' on every reload.
        """
        if not hasattr(self._local, "conn") or self._local.conn is None:
            new_conn = sqlite3.connect(self.db_path, check_same_thread=False)
            new_conn.row_factory = sqlite3.Row
            self._apply_pragmas(new_conn)
            self._local.conn = new_conn
            with self._all_conns_lock:
                self._all_conns.add(new_conn)
        return self._local.conn

    # ── Stage 3a: WAL observability + checkpoint control ──

    def wal_stats(self) -> dict[str, Any]:
        """Snapshot WAL state for /api/health/memory.

        Returns:
            journal_mode    str   "wal" expected; anything else means
                                  WAL didn't take effect on this DB
            page_size       int   bytes per page (4096 default)
            page_count      int   total pages allocated to the main DB
            wal_pages       int   pages currently in the WAL waiting
                                  for a checkpoint. Growing monotonically
                                  = writer + open reader holding the
                                  WAL pinned. Drops to ~0 after each
                                  passive auto-checkpoint.
            wal_autocheckpoint  int   page threshold that triggers
                                      auto-checkpoint (default 1000)
            db_size_bytes   int   page_size * page_count

        All read-only queries — safe to call from a health probe at
        sub-second intervals.
        """
        out: dict[str, Any] = {}
        try:
            c = self.conn
            out["journal_mode"] = c.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]
            page_size = c.execute("PRAGMA page_size").fetchone()[0]
            page_count = c.execute("PRAGMA page_count").fetchone()[0]
            out["page_size"] = page_size
            out["page_count"] = page_count
            out["db_size_bytes"] = page_size * page_count
            try:
                out["wal_autocheckpoint"] = c.execute(
                    "PRAGMA wal_autocheckpoint"
                ).fetchone()[0]
            except Exception:
                out["wal_autocheckpoint"] = None
            try:
                # PRAGMA wal_checkpoint returns (busy, log, checkpointed)
                # in PASSIVE mode — running the noop variant gives us
                # the current wal_pages without forcing a checkpoint.
                row = c.execute(
                    "PRAGMA wal_checkpoint(PASSIVE)"
                ).fetchone()
                if row:
                    out["wal_busy"] = row[0]
                    out["wal_pages"] = row[1]
                    out["wal_pages_checkpointed"] = row[2]
            except Exception:
                pass
        except Exception as exc:
            out["error"] = str(exc)
        return out

    def wal_checkpoint(self, mode: str = "PASSIVE") -> dict[str, Any]:
        """Force a WAL checkpoint. Operator-callable for ops investigations.

        Modes (per SQLite docs):
          PASSIVE  — non-blocking; checkpoints what it can without
                     waiting for readers / writer to finish
          FULL     — blocks until existing writer finishes, then
                     checkpoints all frames
          RESTART  — like FULL plus blocks until all readers drain,
                     then truncates / restarts the WAL
          TRUNCATE — like RESTART plus shrinks the WAL file to 0 bytes

        Returns the SQLite (busy, log, checkpointed) tuple.
        """
        mode = mode.upper()
        if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            return {"error": f"invalid mode: {mode}"}
        try:
            row = self.conn.execute(
                f"PRAGMA wal_checkpoint({mode})"
            ).fetchone()
            return {
                "mode": mode,
                "busy": row[0] if row else None,
                "wal_pages": row[1] if row else None,
                "checkpointed": row[2] if row else None,
            }
        except Exception as exc:
            return {"mode": mode, "error": str(exc)}

    def execute_with_retry(self, sql: str, params=(), max_retries: int = 3):
        """Execute SQL with automatic retry on 'database is locked'.

        SQLite PRAGMA busy_timeout handles most contention, but under heavy
        concurrent load (e.g. scheduler + backup + API write all hitting the
        WAL at once) the timeout can still expire. This wrapper retries with
        exponential backoff to avoid crashing the server.
        """
        import time
        for attempt in range(max_retries):
            try:
                return self.conn.execute(sql, params)
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    wait = (attempt + 1) * 2  # 2s, 4s, 6s
                    logger.warning(
                        "DB locked (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, max_retries, wait, sql[:80],
                    )
                    time.sleep(wait)
                else:
                    raise

    def _init_schema(self):
        """Create tables if they don't exist, then run any pending migrations.

        Migrations are tracked via the `_meta.schema_version` row. We read
        the previous version BEFORE running the CREATE-IF-NOT-EXISTS block
        so we can tell the difference between a brand-new install (no
        previous row → start at 0) and an upgrade (previous row exists).

        Migrations must be:
          • additive only — never DROP or RENAME a column / table
          • idempotent — safe to re-run if a previous attempt half-finished
          • back-fill data conservatively — never delete user content,
            never change ownership without writing an audit row

        See SCHEMA_VERSION constant for the changelog.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Stage 3a: tune the migration connection too so backfills and
        # ALTER TABLEs benefit from the larger cache and NORMAL sync.
        # Don't track this conn in _all_conns — it's local to this
        # method and closed in the finally block below.
        self._apply_pragmas(conn)
        try:
            # Read the previous version BEFORE applying CREATE TABLE — the
            # _meta row only exists if the install is older than the very
            # first schema, so a missing row means "fresh install, run all
            # migrations". A present row with version=N means "skip
            # migrations 1..N, run N+1..SCHEMA_VERSION".
            previous_version = 0
            try:
                row = conn.execute(
                    "SELECT value FROM _meta WHERE key = 'schema_version'"
                ).fetchone()
                if row and row["value"]:
                    previous_version = int(row["value"])
            except sqlite3.OperationalError:
                # _meta table doesn't exist yet — fresh install path.
                previous_version = 0

            conn.executescript(TABLES)

            # Run incremental migrations. Each migration is wrapped in its
            # own try so a single failing migration on a 5-step upgrade
            # doesn't roll back the four that already worked. We commit
            # after each successful step so a crash mid-upgrade leaves
            # the schema in a known state.
            if previous_version < 2:
                self._migrate_v2_workspaces(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "2"),
                )
                conn.commit()

            if previous_version < 3:
                self._migrate_v3_signup_default(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "3"),
                )
                conn.commit()

            if previous_version < 4:
                self._migrate_v4_heal_projects_workspace(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "4"),
                )
                conn.commit()

            if previous_version < 5:
                self._migrate_v5_workflow_workspace(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "5"),
                )
                conn.commit()

            if previous_version < 6:
                self._migrate_v6_connections_workspace(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "6"),
                )
                conn.commit()

            if previous_version < 7:
                self._migrate_v7_credentials_workspace(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "7"),
                )
                conn.commit()

            if previous_version < 8:
                self._migrate_v8_schedules_workspace(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "8"),
                )
                conn.commit()

            if previous_version < 9:
                self._migrate_v9_alerts_workspace(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "9"),
                )
                conn.commit()

            if previous_version < 10:
                self._migrate_v10_executions_workspace(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "10"),
                )
                conn.commit()

            if previous_version < 11:
                self._migrate_v11_variables_workspace(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "11"),
                )
                conn.commit()

            if previous_version < 12:
                self._migrate_v12_lifecycle_workspace(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "12"),
                )
                conn.commit()

            if previous_version < 13:
                self._migrate_v13_contracts_workspace(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "13"),
                )
                conn.commit()

            if previous_version < 14:
                self._migrate_v14_ai_config(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "14"),
                )
                conn.commit()

            if previous_version < 15:
                self._migrate_v15_content_hash(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "15"),
                )
                conn.commit()

            if previous_version < 16:
                self._migrate_v16_drift_events(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "16"),
                )
                conn.commit()

            if previous_version < 17:
                self._migrate_v17_execution_budget(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "17"),
                )
                conn.commit()

            if previous_version < 18:
                self._migrate_v18_composite_indexes(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "18"),
                )
                conn.commit()

            if previous_version < 19:
                self._migrate_v19_credential_vault(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "19"),
                )
                conn.commit()

            if previous_version < 20:
                self._migrate_v20_sandbox_runs(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "20"),
                )
                conn.commit()

            if previous_version < 21:
                self._migrate_v21_two_gate_approval(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "21"),
                )
                conn.commit()

            if previous_version < 22:
                self._migrate_v22_pool_allocations(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "22"),
                )
                conn.commit()

            if previous_version < 23:
                self._migrate_v23_pipeline_checkpoints(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "23"),
                )
                conn.commit()

            if previous_version < 24:
                self._migrate_v24_folders(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "24"),
                )
                conn.commit()

            if previous_version < 25:
                self._migrate_v25_datastore(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "25"),
                )
                conn.commit()

            if previous_version < 26:
                self._migrate_v26_storage_folder(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "26"),
                )
                conn.commit()

            if previous_version < 27:
                self._migrate_v27_settings_updated_at(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "27"),
                )
                conn.commit()

            if previous_version < 28:
                self._migrate_v28_schema_history(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "28"),
                )
                conn.commit()

            if previous_version < 29:
                self._migrate_v29_backfill_runs(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "29"),
                )
                conn.commit()

            if previous_version < 30:
                self._migrate_v30_sink_idempotency(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "30"),
                )
                conn.commit()

            if previous_version < 31:
                self._migrate_v31_sync_state(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "31"),
                )
                conn.commit()

            if previous_version < 32:
                self._migrate_v32_ai_credential_ref(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("schema_version", "32"),
                )
                conn.commit()

            # Self-healing additive column checks.
            # The version-gated migration above runs once. But there's
            # a sharp edge: if someone bumps SCHEMA_VERSION before
            # adding the migration body, the "Final version stamp"
            # below records the new version while no column change
            # ever happened — and subsequent boots see
            # `previous_version >= SCHEMA_VERSION` and skip the
            # migration permanently. The recovery would be manual
            # ALTERs on every customer's DB.
            #
            # The fix: run idempotent column-presence checks
            # unconditionally on every boot. Each check is cheap
            # (one PRAGMA + at most one ALTER) and the underlying
            # _migrate_v27_settings_updated_at is already a no-op
            # when the column exists. So this acts as a safety net,
            # not duplicate work.
            self._migrate_v27_settings_updated_at(conn)
            # v23 self-heal (2026-05-28): a real OSS install was seen
            # in the wild with schema_version stamped past 23 but the
            # `pipeline_checkpoints` table missing, producing one
            # "CheckpointStore.upsert failed: no such table" warning
            # per step per run. The migration body uses CREATE TABLE
            # IF NOT EXISTS for both the table and its two indexes,
            # so re-running it costs one bytecode-compiled DDL parse
            # and zero schema changes when the table is already
            # present. Cheap enough to run unconditionally as a net.
            try:
                self._migrate_v23_pipeline_checkpoints(conn)
            except sqlite3.OperationalError as exc:
                # If the install is so old it doesn't even have the
                # ambient context this migration assumes, log and
                # move on — checkpoint persistence is a "nice to
                # have" recovery feature, not a hard dependency for
                # pipeline execution. The next boot retries.
                logger.warning(
                    "F-Pulse: pipeline_checkpoints self-heal skipped: %s",
                    exc,
                )
            conn.commit()
            # v32 self-heal (2026-06-17): the credential_id column on the
            # two AI-config tables is additive + PRAGMA-guarded, so re-running
            # is a no-op once present. Cheap insurance against the
            # bumped-version-without-migration sharp edge described above.
            self._migrate_v32_ai_credential_ref(conn)
            conn.commit()

            # Final version stamp — covers the case where SCHEMA_VERSION
            # got bumped without adding a migration (no-op upgrade).
            conn.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            conn.commit()
        finally:
            conn.close()

    def _migrate_v2_workspaces(self, conn: sqlite3.Connection) -> None:
        """v1 → v2: introduce the Workspace foundation without losing data.

        Steps (all idempotent):
          1. If `workspaces` is empty, create a single 'Default' workspace
             with a deterministic id ('default') so existing JSON blobs
             that already store project_id='default' line up automatically.
          2. Add a `workspace_id` column to `projects` if it isn't already
             there (SQLite has no IF NOT EXISTS for ADD COLUMN, so we
             try-except on the duplicate-column error).
          3. Back-fill `workspace_id='default'` on every existing project
             row that doesn't have one yet.
          4. Enrol every existing user in the Default workspace as a
             member. The user with id='admin' (the seeded super_admin)
             becomes the workspace owner. Other users default to
             'developer' role inside the workspace; an admin can promote
             them later.

        Why this is corporate-policy-safe:
          • No data is moved, copied, or deleted — only tagged with a
            workspace_id. A rollback to v1 (drop the column, drop the
            new tables) would leave the install identical to before.
          • The Default workspace is marked is_personal=0 so it's
            visible in corporate workspace lists; an admin can rename
            it ("Acme Corp") without losing the back-fill semantics.
          • Audit: this function does NOT write audit rows because it
            runs at startup before the audit logger is wired up. The
            very first admin login after upgrade triggers a "schema
            v2 upgrade applied" audit row from the auth layer instead.
        """
        from datetime import datetime, timezone
        import json as _json

        now_iso = datetime.now(timezone.utc).isoformat()

        # Step 1: ensure the Default workspace exists.
        existing = conn.execute(
            "SELECT id FROM workspaces WHERE id = 'default'"
        ).fetchone()
        if not existing:
            default_data = {
                "id": "default",
                "name": "Default",
                "slug": "default",
                "plan": "free",
                "is_personal": 0,
                "owner_id": "admin",
                "domain_allowlist": [],
                "settings": {},
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            conn.execute(
                """INSERT INTO workspaces
                   (id, name, slug, plan, is_personal, owner_id, domain_allowlist, settings, data, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "default", "Default", "default", "free", 0, "admin",
                    "[]", "{}", _json.dumps(default_data), now_iso, now_iso,
                ),
            )
            logger.info("F-Pulse: schema v2 — created Default workspace")

        # Step 2: add workspace_id column to projects (idempotent).
        try:
            conn.execute(
                "ALTER TABLE projects ADD COLUMN workspace_id TEXT DEFAULT 'default'"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_projects_workspace ON projects(workspace_id)"
            )
            logger.info("F-Pulse: schema v2 — added projects.workspace_id column")
        except sqlite3.OperationalError as exc:
            # Column already exists from a partial previous run — fine.
            if "duplicate column" not in str(exc).lower():
                raise

        # Step 3: back-fill workspace_id on every project row that lacks one.
        conn.execute(
            "UPDATE projects SET workspace_id = 'default' WHERE workspace_id IS NULL OR workspace_id = ''"
        )

        # Step 4: enrol every existing user as a member of Default.
        users = conn.execute("SELECT id FROM users").fetchall()
        for u in users:
            uid = u["id"] if isinstance(u, sqlite3.Row) else u[0]
            role = "developer"
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO workspace_members
                       (workspace_id, user_id, role, invited_by, invited_at, accepted_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    ("default", uid, role, "system", now_iso, now_iso),
                )
            except sqlite3.OperationalError as exc:
                logger.warning("F-Pulse: schema v2 — could not enrol user %s: %s", uid, exc)
        logger.info("F-Pulse: schema v2 — enrolled %d existing users in Default workspace", len(users))

    def _migrate_v3_signup_default(self, conn: sqlite3.Connection) -> None:
        """v2 → v3: flip `allow_self_registration` to True on installs that
        still carry the old default of False.

        Why this is safe to do without admin consent:
          • Before this migration there was NO marker distinguishing
            "admin explicitly chose False" from "False is the default".
            Both paths wrote the same JSON. So either choice has the
            same provenance, and we have to pick one — the new default
            (True) is the deliberate product decision being rolled out.
          • The migration is idempotent: it only touches the row when
            the key is currently False AND the v3 marker hasn't been
            written. After the marker exists we never touch the value
            again, so a subsequent admin choice of False sticks.
          • If the admin really did want invite-only mode, they re-flip
            it once in Admin → Security → Self-service registration
            and the change persists across reboots.

        Idempotency marker: a row in `_meta` with key `signup_default_v3`.
        Present = migration already ran on this install.
        """
        import json as _json
        marker = conn.execute(
            "SELECT value FROM _meta WHERE key = 'signup_default_v3'"
        ).fetchone()
        if marker:
            return  # already applied

        row = conn.execute(
            "SELECT data FROM settings WHERE id = 'admin_settings'"
        ).fetchone()
        if row and row["data"]:
            try:
                data = _json.loads(row["data"])
                if data.get("allow_self_registration") is False:
                    data["allow_self_registration"] = True
                    conn.execute(
                        "UPDATE settings SET data = ? WHERE id = 'admin_settings'",
                        (_json.dumps(data),),
                    )
                    logger.info(
                        "F-Pulse: schema v3 — flipped allow_self_registration "
                        "False → True (was the old default)"
                    )
            except Exception as exc:
                logger.warning(
                    "F-Pulse: schema v3 — could not parse admin_settings, "
                    "leaving untouched: %s", exc
                )

        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
            ("signup_default_v3", "1"),
        )

    def _migrate_v5_workflow_workspace(self, conn: sqlite3.Connection) -> None:
        """v4 → v5: add workspace_id column to workflow_versions and
        back-fill every existing row.

        The column is denormalised — the Workflow.workspace_id field
        inside the JSON blob is the logical source of truth, but
        having an indexed column lets list_all(workspace_id=…) skip
        the json_extract step and use an index.

        Idempotent via PRAGMA — does nothing if the column is already
        present. Back-fill is safe because every pre-v5 row was
        created before multi-tenancy existed, so they all belong to
        'default' by definition.
        """
        cols = conn.execute("PRAGMA table_info(workflow_versions)").fetchall()
        col_names = {c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in cols}
        if "workspace_id" not in col_names:
            try:
                conn.execute(
                    "ALTER TABLE workflow_versions ADD COLUMN workspace_id TEXT DEFAULT 'default'"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_wv_workspace ON workflow_versions(workspace_id)"
                )
                logger.info(
                    "F-Pulse: schema v5 — added workflow_versions.workspace_id column"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        # Back-fill. Safe to re-run: only touches rows where the
        # column is NULL / empty, which a fresh ALTER gives us.
        conn.execute(
            "UPDATE workflow_versions SET workspace_id = 'default' WHERE workspace_id IS NULL OR workspace_id = ''"
        )

        # Patch the JSON blob too, so the Workflow model parses back
        # with the correct workspace_id. We only rewrite rows whose
        # blob doesn't already carry a workspace_id key so this is a
        # one-time back-fill, not a destructive overwrite.
        import json as _json
        rows = conn.execute(
            "SELECT workflow_id, version, data FROM workflow_versions"
        ).fetchall()
        for r in rows:
            try:
                blob = _json.loads(r["data"] if isinstance(r, sqlite3.Row) else r[2])
                wf = blob.get("workflow") or {}
                if wf.get("workspace_id"):
                    continue
                wf["workspace_id"] = "default"
                blob["workflow"] = wf
                conn.execute(
                    "UPDATE workflow_versions SET data = ? WHERE workflow_id = ? AND version = ?",
                    (_json.dumps(blob), r["workflow_id"], r["version"]),
                )
            except Exception as exc:
                logger.warning(
                    "F-Pulse: schema v5 — could not back-fill workflow blob %s/%s: %s",
                    r["workflow_id"] if isinstance(r, sqlite3.Row) else r[0],
                    r["version"] if isinstance(r, sqlite3.Row) else r[1],
                    exc,
                )

    def _migrate_v6_connections_workspace(self, conn: sqlite3.Connection) -> None:
        """v5 → v6: add workspace_id to `connections` and back-fill.

        Two writes:
          1. Indexed column on the `connections` table (for fast
             workspace-scoped listing).
          2. JSON blob back-fill so Connection.workspace_id round-trips
             through model parsing.

        Safety: idempotent via PRAGMA check; re-running is a no-op
        once the column exists. Back-fill only touches rows that
        don't already carry a workspace_id so an admin-placed value
        survives re-runs.
        """
        cols = conn.execute("PRAGMA table_info(connections)").fetchall()
        col_names = {c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in cols}
        if "workspace_id" not in col_names:
            try:
                conn.execute(
                    "ALTER TABLE connections ADD COLUMN workspace_id TEXT DEFAULT 'default'"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_connections_workspace ON connections(workspace_id)"
                )
                logger.info(
                    "F-Pulse: schema v6 — added connections.workspace_id column"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        conn.execute(
            "UPDATE connections SET workspace_id = 'default' WHERE workspace_id IS NULL OR workspace_id = ''"
        )

        # JSON blob back-fill — one-shot, don't overwrite admin-set
        # values. Unlike workflow_versions where the data wraps the
        # workflow one level down, connections.data IS the Connection
        # dict at the top level, so we patch directly.
        import json as _json
        rows = conn.execute("SELECT id, data FROM connections").fetchall()
        for r in rows:
            try:
                blob = _json.loads(r["data"] if isinstance(r, sqlite3.Row) else r[1])
                if blob.get("workspace_id"):
                    continue
                blob["workspace_id"] = "default"
                conn.execute(
                    "UPDATE connections SET data = ? WHERE id = ?",
                    (_json.dumps(blob), r["id"] if isinstance(r, sqlite3.Row) else r[0]),
                )
            except Exception as exc:
                logger.warning(
                    "F-Pulse: schema v6 — could not back-fill connection blob %s: %s",
                    r["id"] if isinstance(r, sqlite3.Row) else r[0],
                    exc,
                )

    def _add_workspace_id_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        version: int,
    ) -> None:
        """Generic single-table workspace_id migration.

        Adds an indexed `workspace_id TEXT DEFAULT 'default'` column,
        back-fills the column on existing rows, and rewrites JSON
        blobs that don't already carry the field. Idempotent via
        PRAGMA + `CREATE INDEX IF NOT EXISTS`.
        """
        import json as _json
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        col_names = {c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in cols}
        if "workspace_id" not in col_names:
            try:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN workspace_id TEXT DEFAULT 'default'"
                )
                logger.info(
                    "F-Pulse: schema v%d — added %s.workspace_id column",
                    version, table,
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_workspace ON {table}(workspace_id)"
        )
        conn.execute(
            f"UPDATE {table} SET workspace_id = 'default' WHERE workspace_id IS NULL OR workspace_id = ''"
        )
        rows = conn.execute(f"SELECT id, data FROM {table}").fetchall()
        for r in rows:
            try:
                blob = _json.loads(r["data"] if isinstance(r, sqlite3.Row) else r[1])
                if blob.get("workspace_id"):
                    continue
                blob["workspace_id"] = "default"
                conn.execute(
                    f"UPDATE {table} SET data = ? WHERE id = ?",
                    (_json.dumps(blob), r["id"] if isinstance(r, sqlite3.Row) else r[0]),
                )
            except Exception as exc:
                logger.warning(
                    "F-Pulse: schema v%d — could not back-fill %s blob %s: %s",
                    version, table,
                    r["id"] if isinstance(r, sqlite3.Row) else r[0],
                    exc,
                )

    def _migrate_v19_credential_vault(self, conn: sqlite3.Connection) -> None:
        """v18 → v19: extended credential metadata.

        Adds forward-compat fields and a vault_secrets table to support
        external credential references. Every change is strictly additive;
        existing code keeps working unchanged.

        Columns added:
          workflow_versions.engine         — "duckdb" default; reserved for future engines
          schema_contracts.format          — "json_schema" default; accepts "avro"|"protobuf"
          schema_contracts.compatibility   — "BACKWARD" default; Confluent-style compat modes
          approval_gates.preflight_status  — "pending"|"passed"|"failed"|"skipped"
          approval_gates.preflight_result  — JSON payload from preflight runner
          connections.credentials_ref      — external secret reference (NULL = legacy inline)

        New table:
          vault_secrets — external secret store for the credential-ref
          migration path (inline → reference) without breaking existing
          connection configs.
        """
        # ── executions engine marker ──
        cols_wv = conn.execute("PRAGMA table_info(workflow_versions)").fetchall()
        wv_names = {(c["name"] if isinstance(c, sqlite3.Row) else c[1]) for c in cols_wv}
        if "engine" not in wv_names:
            try:
                conn.execute(
                    "ALTER TABLE workflow_versions ADD COLUMN engine TEXT DEFAULT 'duckdb'"
                )
                logger.info("F-Pulse: schema v19 — workflow_versions.engine added")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        # ── schema contracts: format + compatibility ──
        cols_sc = conn.execute("PRAGMA table_info(schema_contracts)").fetchall()
        sc_names = {(c["name"] if isinstance(c, sqlite3.Row) else c[1]) for c in cols_sc}
        contract_additions = [
            ("format", "TEXT DEFAULT 'json_schema'"),
            ("compatibility", "TEXT DEFAULT 'BACKWARD'"),
        ]
        for name, decl in contract_additions:
            if name in sc_names:
                continue
            try:
                conn.execute(f"ALTER TABLE schema_contracts ADD COLUMN {name} {decl}")
                logger.info("F-Pulse: schema v19 — schema_contracts.%s added", name)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        # ── approval_gates: preflight fields ──
        cols_ag = conn.execute("PRAGMA table_info(approval_gates)").fetchall()
        ag_names = {(c["name"] if isinstance(c, sqlite3.Row) else c[1]) for c in cols_ag}
        preflight_additions = [
            ("preflight_status", "TEXT DEFAULT 'pending'"),
            ("preflight_result", "JSON"),
        ]
        for name, decl in preflight_additions:
            if name in ag_names:
                continue
            try:
                conn.execute(f"ALTER TABLE approval_gates ADD COLUMN {name} {decl}")
                logger.info("F-Pulse: schema v19 — approval_gates.%s added", name)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        # ── connections: credentials_ref (Vault pointer) ──
        cols_conn = conn.execute("PRAGMA table_info(connections)").fetchall()
        conn_names = {(c["name"] if isinstance(c, sqlite3.Row) else c[1]) for c in cols_conn}
        if "credentials_ref" not in conn_names:
            try:
                conn.execute(
                    "ALTER TABLE connections ADD COLUMN credentials_ref TEXT"
                )
                logger.info("F-Pulse: schema v19 — connections.credentials_ref added")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        # ── vault_secrets table (external credential store) ──
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS vault_secrets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                secret_type TEXT NOT NULL DEFAULT 'custom',
                encrypted_value TEXT NOT NULL,
                masked_value TEXT,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                created_by TEXT DEFAULT 'system',
                expires_at TEXT,
                rotation_count INTEGER NOT NULL DEFAULT 0,
                last_rotated_at TEXT,
                last_used_at TEXT,
                linked_connections JSON,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_vault_secrets_workspace ON vault_secrets(workspace_id);
            CREATE INDEX IF NOT EXISTS idx_vault_secrets_type ON vault_secrets(secret_type);

            CREATE TABLE IF NOT EXISTS vault_audit_log (
                id TEXT PRIMARY KEY,
                secret_id TEXT NOT NULL,
                secret_name TEXT,
                action TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'system',
                details JSON,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_vault_audit_secret ON vault_audit_log(secret_id);
            CREATE INDEX IF NOT EXISTS idx_vault_audit_action ON vault_audit_log(action);
            """
        )
        logger.info("F-Pulse: schema v19 — vault_secrets + vault_audit_log tables ready")

    def _migrate_v20_sandbox_runs(self, conn: sqlite3.Connection) -> None:
        """v19 → v20: PROD Sandbox (deploy-preview environment).

        Adds the `sandbox_runs` table — an ephemeral execution record tagged
        ``sandbox`` that reads PROD-class data via real connections but
        writes only to a scratch namespace. Triggered by an approver during
        the review of a pending deploy. Auto-purges 24h after creation OR
        on approve/reject (whichever comes first).

        Strictly additive — no existing tables touched. Rerun-safe via
        IF NOT EXISTS guards. See DESIGN_PROD_SANDBOX.md for the load-
        bearing invariants (I1-I10).
        """
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sandbox_runs (
                id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                workflow_version INTEGER NOT NULL,
                execution_id TEXT,
                scratch_namespace TEXT NOT NULL,
                scratch_paths TEXT,
                row_limit INTEGER NOT NULL DEFAULT 10000,
                status TEXT NOT NULL DEFAULT 'queued',
                triggered_by TEXT NOT NULL,
                triggered_at TEXT NOT NULL,
                finished_at TEXT,
                cleanup_at TEXT NOT NULL,
                cleaned_at TEXT,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sandbox_runs_approval ON sandbox_runs(approval_id);
            CREATE INDEX IF NOT EXISTS idx_sandbox_runs_cleanup
                ON sandbox_runs(cleanup_at) WHERE cleaned_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_sandbox_runs_status ON sandbox_runs(status);
            """
        )
        logger.info("F-Pulse: schema v20 — sandbox_runs table ready")

    def _migrate_v21_two_gate_approval(self, conn: sqlite3.Connection) -> None:
        """v20 → v21: PR11 — Two-Gate Approval Flow.

        Today's approval state lives as fields on the workflow JSON
        blob in workflow_versions.data. We don't need to migrate those;
        the new fields are forward-compatible — code reads them with
        ``.get(...)`` defaults so legacy workflows keep working.

        What this migration adds:

        * ``lifecycle_toggle_requests`` table — backs the Activate /
          Deactivate approval requests added in PR12. Created here so
          PR11 + PR12 can ship as a single schema bump.
        * ``workspace_settings`` table — holds per-workspace knobs like
          ``enforce_two_person_approval`` (PR11). Bootstrapped with
          defaults; tenants override via API.

        Strictly additive. IF NOT EXISTS makes the migration idempotent.

        State machine introduced (encoded in workflow.approval_stage in
        the JSON blob, NOT a column — keeps the migration cheap):

            draft
              ↓ submit
            pending_sandbox_approval     ← Gate 1 pending
              ↓ approve_sandbox
            sandbox_ready                ← Prod admin can run sandbox
              ↓ admin: submit-for-deploy (after >= 1 successful sandbox run)
            pending_deploy_approval      ← Gate 2 pending; sandbox evidence attached
              ↓ approve_deploy
            active                       ← Live in PROD
              ↓ request deactivate
            pending_lifecycle_toggle     ← Tracked in lifecycle_toggle_requests
              ↓ approve
            inactive
        """
        conn.executescript(
            """
            -- PR12: lifecycle_toggle_requests — Activate / Deactivate
            -- requests pending approval. One row per pending request;
            -- decided requests are kept for audit (NOT deleted).
            CREATE TABLE IF NOT EXISTS lifecycle_toggle_requests (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                workflow_version INTEGER NOT NULL,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                action TEXT NOT NULL,                  -- 'activate' | 'deactivate'
                target_env TEXT NOT NULL DEFAULT 'prod', -- always 'prod' for now (DEV is direct toggle)
                requested_by TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'approved' | 'rejected'
                decided_by TEXT,
                decided_at TEXT,
                decision_notes TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_lifecycle_toggle_workflow
                ON lifecycle_toggle_requests(workflow_id);
            CREATE INDEX IF NOT EXISTS idx_lifecycle_toggle_pending
                ON lifecycle_toggle_requests(status) WHERE status = 'pending';
            CREATE INDEX IF NOT EXISTS idx_lifecycle_toggle_workspace
                ON lifecycle_toggle_requests(workspace_id);

            -- PR11: workspace_settings — per-workspace knobs.
            -- Single row per workspace; key/value JSON lets us add
            -- settings without further migrations.
            CREATE TABLE IF NOT EXISTS workspace_settings (
                workspace_id TEXT PRIMARY KEY,
                settings JSON NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                updated_by TEXT
            );
            """
        )
        logger.info(
            "F-Pulse: schema v21 — lifecycle_toggle_requests "
            "+ workspace_settings tables ready (PR11/PR12 foundation)"
        )

    def _migrate_v22_pool_allocations(self, conn: sqlite3.Connection) -> None:
        """v21 → v22: PR14 — Worker Pool Allocation.

        Per-workspace logical pool split between PROD reserved, DEV reserved,
        and a shared burst lane. Defaults to 60/20/20. ExecutionManager's
        admit logic enforces the reservation; admins adjust live via the
        Pool page slider. CHECK constraint guarantees the three percentages
        sum to 100, so the runtime never has to silently round.
        """
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pool_allocations (
                workspace_id TEXT PRIMARY KEY,
                prod_reserved_pct INTEGER NOT NULL DEFAULT 60,
                dev_reserved_pct INTEGER NOT NULL DEFAULT 20,
                burst_pct INTEGER NOT NULL DEFAULT 20,
                updated_at TEXT NOT NULL,
                updated_by TEXT,
                CHECK (prod_reserved_pct >= 0 AND prod_reserved_pct <= 100),
                CHECK (dev_reserved_pct  >= 0 AND dev_reserved_pct  <= 100),
                CHECK (burst_pct         >= 0 AND burst_pct         <= 100),
                CHECK (prod_reserved_pct + dev_reserved_pct + burst_pct = 100)
            );
            """
        )
        logger.info(
            "F-Pulse: schema v22 — pool_allocations table ready (PR14)"
        )

    def _migrate_v23_pipeline_checkpoints(self, conn: sqlite3.Connection) -> None:
        """v22 → v23: Sprint 1 — pipeline_checkpoints table.

        Records, per (run_id, step_id), the success/failure status of every
        step in a pipeline run plus a pointer to the Parquet snapshot the
        existing StepCache wrote on success. Powers the executor's
        "Resume from step X" feature — on resume, the executor walks
        checkpoints for the failed run, registers each successful step's
        Parquet as a DuckDB relation under the original step_id, and
        starts execution from the first non-success step.

        Independent of StepCache.manifest.json which is keyed by
        workflow_id + effective_hash for "Rerun from here". The new
        table is keyed by run_id and tracks the per-run sequence of
        outcomes — two different concerns.

        Strictly additive — no data migration.

        Index strategy:
          • PRIMARY KEY (run_id, step_id) — natural lookup
          • idx_checkpoints_workflow — for "list failed runs for this
            workflow" admin queries
          • idx_checkpoints_status — for the cleanup sweeper that
            evicts old in_progress rows orphaned by crashes
        """
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pipeline_checkpoints (
                workflow_id    TEXT NOT NULL,
                run_id         TEXT NOT NULL,
                step_id        TEXT NOT NULL,
                status         TEXT NOT NULL CHECK (status IN ('success','failed','in_progress','skipped')),
                completed_at   TEXT,
                rows_in        INTEGER,
                rows_out       INTEGER,
                duration_ms    INTEGER,
                output_ref     TEXT,
                error_summary  TEXT,
                PRIMARY KEY (run_id, step_id)
            );

            CREATE INDEX IF NOT EXISTS idx_checkpoints_workflow
                ON pipeline_checkpoints(workflow_id, run_id);

            CREATE INDEX IF NOT EXISTS idx_checkpoints_status
                ON pipeline_checkpoints(status, completed_at);
            """
        )
        logger.info(
            "F-Pulse: schema v23 — pipeline_checkpoints table ready (Sprint 1 / Gate 1)"
        )

    def _migrate_v27_settings_updated_at(self, conn: sqlite3.Connection) -> None:
        """v26 → v27: add updated_at to the settings table.

        Drift fix (2026-05-27): writers in api/auth.py have been
        INSERT-ing four columns (id, data, created_at, updated_at)
        with an ON CONFLICT clause that touches updated_at, but the
        settings table was created with three columns. Upgrading
        installs would see ``sqlite3.OperationalError: table settings
        has no column named updated_at`` the first time anyone hit
        /api/auth/forgot-password — a launch blocker for the
        password-recovery flow.

        Strictly additive. Existing rows back-fill to '' (treated as
        "never updated since creation" by readers). Fresh installs
        get the column from the CREATE TABLE statement.
        """
        cols = conn.execute("PRAGMA table_info(settings)").fetchall()
        col_names = {
            c["name"] if isinstance(c, sqlite3.Row) else c[1]
            for c in cols
        }
        if "updated_at" not in col_names:
            try:
                conn.execute(
                    "ALTER TABLE settings ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError as exc:
                # Idempotent retry safety — duplicate-column means
                # another path already added it.
                if "duplicate column" not in str(exc).lower():
                    raise
        logger.info("F-Pulse: schema v27 — settings.updated_at ready")

    def _migrate_v26_storage_folder(self, conn: sqlite3.Connection) -> None:
        """v25 → v26: add folder_id to storage_objects.

        Strictly additive. Existing rows back-fill to ''
        (workspace-global or project-root). The Y15 upload dialog uses
        this column to scope files to a specific Folder under a
        Project. OSS folders are 1-level deep (see
        backend/fpulse/api/folders.py).
        """
        cols = conn.execute("PRAGMA table_info(storage_objects)").fetchall()
        col_names = {c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in cols}
        if "folder_id" not in col_names:
            try:
                conn.execute(
                    "ALTER TABLE storage_objects ADD COLUMN folder_id TEXT DEFAULT ''"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_storage_objects_folder "
            "ON storage_objects(folder_id)"
        )
        logger.info("F-Pulse: schema v26 — storage_objects.folder_id ready")

    def _migrate_v25_datastore(self, conn: sqlite3.Connection) -> None:
        """v24 → v25: workspace datastore index (Storage page foundation).

        Three new tables: storage_objects / storage_tables / storage_columns.
        The CREATE IF NOT EXISTS block at the top of the file already
        emits these for fresh installs; this migration runs them again
        for upgrading installs that landed on v24 before they shipped.

        Strictly additive — no existing column / table is touched. The
        datastore Python module reconciles files on disk into the new
        index on first boot via fpulse.datastore.reconcile.reconcile_all,
        gated by a `.datastore-reconciled` sentinel under FPULSE_DATA_DIR.
        """
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS storage_objects (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                kind TEXT NOT NULL DEFAULT 'file',
                project_id TEXT DEFAULT '',
                pipeline_id TEXT DEFAULT '',
                deleted_at TEXT DEFAULT '',
                data JSON NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_storage_objects_ws
                ON storage_objects(workspace_id);
            CREATE INDEX IF NOT EXISTS idx_storage_objects_kind
                ON storage_objects(workspace_id, kind);
            CREATE INDEX IF NOT EXISTS idx_storage_objects_pipeline
                ON storage_objects(pipeline_id);
            CREATE INDEX IF NOT EXISTS idx_storage_objects_deleted
                ON storage_objects(workspace_id, deleted_at);

            CREATE TABLE IF NOT EXISTS storage_tables (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                schema_name TEXT NOT NULL DEFAULT 'default',
                name TEXT NOT NULL,
                data JSON NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_storage_tables_name
                ON storage_tables(workspace_id, schema_name, name);
            CREATE INDEX IF NOT EXISTS idx_storage_tables_ws
                ON storage_tables(workspace_id);

            CREATE TABLE IF NOT EXISTS storage_columns (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                table_id TEXT NOT NULL DEFAULT '',
                object_id TEXT NOT NULL DEFAULT '',
                data JSON NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_storage_columns_table
                ON storage_columns(table_id);
            CREATE INDEX IF NOT EXISTS idx_storage_columns_object
                ON storage_columns(object_id);
            """
        )
        logger.info(
            "F-Pulse: schema v25 — workspace datastore (storage_objects / storage_tables / storage_columns) ready"
        )

    def _migrate_v24_folders(self, conn: sqlite3.Connection) -> None:
        """v23 → v24: project tree + folder hierarchy.

        Two additive changes:
          1. ADD COLUMN projects.parent_id — nullable, '' = root project.
          2. CREATE TABLE folders — nested grouping of pipelines inside
             a project. parent_folder_id='' (empty) = sits at project root.

        Both are strictly additive. The Workflow IR's new `folder_id`
        field lives inside the JSON blob in workflow_versions.data, so
        no column change is needed there — legacy rows simply default
        to folder_id=None on load.
        """
        cols = conn.execute("PRAGMA table_info(projects)").fetchall()
        col_names = {c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in cols}
        if "parent_id" not in col_names:
            try:
                conn.execute(
                    "ALTER TABLE projects ADD COLUMN parent_id TEXT DEFAULT ''"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS folders (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                project_id TEXT NOT NULL,
                parent_folder_id TEXT DEFAULT '',
                workspace_id TEXT DEFAULT 'default',
                data JSON NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_folders_project ON folders(project_id);
            CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_folder_id);
            CREATE INDEX IF NOT EXISTS idx_folders_workspace ON folders(workspace_id);
            """
        )
        logger.info(
            "F-Pulse: schema v24 — projects.parent_id + folders table ready"
        )

    def _migrate_v18_composite_indexes(self, conn: sqlite3.Connection) -> None:
        """v17 → v18: add "owner + recency" composite indexes on the
        four time-series tables (executions, audit_log, alert_logs,
        lifecycle_events).

        Why composites now: the existing indexes are FK-only. A query
        like "last 100 runs for workflow X ordered by started_at DESC"
        uses idx_executions_workflow to narrow, then does a separate
        filesort on started_at. At 1M+ rows the filesort dominates.
        A `(workflow_id, started_at DESC)` composite lets the SQLite
        planner satisfy both the WHERE and ORDER BY from one index
        scan — no sort, bounded memory.

        Strictly additive: CREATE INDEX IF NOT EXISTS means rerun-safe.
        No schema changes to the tables themselves."""
        indexes = [
            # Executions: "recent runs for workflow X" (admin + history pages).
            ("idx_executions_wf_started",
             "CREATE INDEX IF NOT EXISTS idx_executions_wf_started "
             "ON executions(workflow_id, started_at DESC)"),
            # Executions: "recent runs in workspace Y" (dashboard).
            ("idx_executions_ws_started",
             "CREATE INDEX IF NOT EXISTS idx_executions_ws_started "
             "ON executions(workspace_id, started_at DESC)"),
            # Audit log: "what did user X do recently" (compliance view).
            ("idx_audit_user_time",
             "CREATE INDEX IF NOT EXISTS idx_audit_user_time "
             "ON audit_log(user_id, timestamp DESC)"),
            # Audit log: "recent actions of type X" (security drill-down).
            ("idx_audit_action_time",
             "CREATE INDEX IF NOT EXISTS idx_audit_action_time "
             "ON audit_log(action, timestamp DESC)"),
            # Alert logs: "recent alerts for workflow X" (monitor page).
            ("idx_alert_logs_wf_triggered",
             "CREATE INDEX IF NOT EXISTS idx_alert_logs_wf_triggered "
             "ON alert_logs(workflow_id, triggered_at DESC)"),
            # Alert logs: "recent alerts in workspace Y" (admin overview).
            ("idx_alert_logs_ws_triggered",
             "CREATE INDEX IF NOT EXISTS idx_alert_logs_ws_triggered "
             "ON alert_logs(workspace_id, triggered_at DESC)"),
            # Lifecycle: "what happened to workflow X" (history drawer).
            ("idx_lifecycle_wf_time",
             "CREATE INDEX IF NOT EXISTS idx_lifecycle_wf_time "
             "ON lifecycle_events(workflow_id, timestamp DESC)"),
            # Lifecycle: "recent transitions in workspace Y" (dashboard).
            ("idx_lifecycle_ws_time",
             "CREATE INDEX IF NOT EXISTS idx_lifecycle_ws_time "
             "ON lifecycle_events(workspace_id, timestamp DESC)"),
        ]
        created = 0
        for name, ddl in indexes:
            try:
                conn.execute(ddl)
                created += 1
            except sqlite3.OperationalError as exc:
                logger.warning(
                    "F-Pulse schema v18: index %s failed (%s) — "
                    "underlying table may be missing a column from a "
                    "prior migration path; continuing.",
                    name, exc,
                )
        logger.info(
            "F-Pulse: schema v18 — %d/%d composite indexes created",
            created, len(indexes),
        )

    def _migrate_v17_execution_budget(self, conn: sqlite3.Connection) -> None:
        """v16 → v17: add PR5 step-7 budget + actual columns to
        `executions`. Strictly additive, NULL-tolerant. Existing rows
        keep NULL for every new column — no back-fill is run because
        (a) the values are unknown for historical runs and (b) NULL is
        the honest signal that the field wasn't captured. Callers that
        query these columns must tolerate NULL.

        Columns added:
          budget_memory_mb      — requested ceiling (int)
          budget_runtime_s      — requested wall-clock cap (int)
          budget_max_attempts   — retry limit (int)
          memory_peak_mb        — observed peak RSS (real)
          runtime_ms            — actual wall-clock (real)
          attempts              — how many tries this row represents (int)
          exit_reason           — ok | budget_memory | budget_runtime |
                                  cancelled | killed_throttle | error
        """
        cols = conn.execute("PRAGMA table_info(executions)").fetchall()
        col_names = {c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in cols}

        additions = [
            ("budget_memory_mb", "INTEGER"),
            ("budget_runtime_s", "INTEGER"),
            ("budget_max_attempts", "INTEGER"),
            ("memory_peak_mb", "REAL"),
            ("runtime_ms", "REAL"),
            ("attempts", "INTEGER"),
            ("exit_reason", "TEXT"),
        ]
        for name, coltype in additions:
            if name in col_names:
                continue
            try:
                conn.execute(f"ALTER TABLE executions ADD COLUMN {name} {coltype}")
                logger.info("F-Pulse: schema v17 — added executions.%s column", name)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        # Single index on exit_reason — the common query is "all runs
        # that timed out" or "all runs that OOM'd". The other columns
        # are analytics dimensions; avoid indexing them until we see
        # a slow query in practice.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_executions_exit_reason ON executions(exit_reason)"
        )

    def _migrate_v16_drift_events(self, conn: sqlite3.Connection) -> None:
        """v15 → v16: add the `drift_events` table.

        Pure additive — no back-fill, no column add to existing tables.
        The CREATE TABLE IF NOT EXISTS in the TABLES block already runs on
        every startup (fresh-install path), so this migration is the
        belt-and-braces idempotent safeguard for upgrade installs.
        Re-running is safe.
        """
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS drift_events (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                item_type TEXT NOT NULL,
                item_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'warning',
                detected_at TEXT NOT NULL,
                resolved_at TEXT,
                resolved_by TEXT,
                resolution TEXT,
                details JSON NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_drift_workspace ON drift_events(workspace_id);
            CREATE INDEX IF NOT EXISTS idx_drift_open ON drift_events(workspace_id, resolved_at);
            CREATE INDEX IF NOT EXISTS idx_drift_item ON drift_events(item_type, item_id);
            """
        )

    def _migrate_v29_backfill_runs(self, conn: sqlite3.Connection) -> None:
        """v28 → v29: add the `backfill_runs` table.

        Pure additive — no back-fill needed because there is no prior
        backfill state to recover. New backfills land directly in the
        table; pre-existing installs see an empty Backfills tab until
        a user kicks one off.

        The CREATE TABLE IF NOT EXISTS in the TABLES block runs on every
        startup (fresh-install path); this migration is the idempotent
        safeguard for upgrade installs that reach the version-gated
        branch before the top-level CREATE block.
        """
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS backfill_runs (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                pipeline_id TEXT NOT NULL,
                parent_backfill_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                data JSON NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_backfill_runs_ws ON backfill_runs(workspace_id);
            CREATE INDEX IF NOT EXISTS idx_backfill_runs_pipeline ON backfill_runs(pipeline_id);
            CREATE INDEX IF NOT EXISTS idx_backfill_runs_parent ON backfill_runs(parent_backfill_id);
            CREATE INDEX IF NOT EXISTS idx_backfill_runs_status ON backfill_runs(status);
            """
        )

    def _migrate_v32_ai_credential_ref(self, conn: sqlite3.Connection) -> None:
        """v31 → v32: add `credential_id` to the two AI-config tables.

        Lets a user/workspace AI provider point its API key at a row in
        the central `credentials` store instead of holding an inline
        encrypted copy. When the column is set, the resolver reads the
        key from that credential at request time (see
        fpulse/ai_config/store.py:resolve_active_config and the resolver
        wired in main.py).

        Strictly additive. Existing rows back-fill to '' (empty =
        "use the inline api_key_encrypted", i.e. unchanged behaviour).
        Fresh installs get the column from the CREATE TABLE statements in
        the TABLES block. Idempotent — PRAGMA-guarded so it can run both
        in the version-gated branch and unconditionally as a self-heal.
        """
        for table in ("user_ai_config", "workspace_ai_config"):
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
            col_names = {
                c["name"] if isinstance(c, sqlite3.Row) else c[1]
                for c in cols
            }
            if "credential_id" not in col_names:
                try:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN credential_id TEXT DEFAULT ''"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
        logger.info("F-Pulse: schema v32 — AI-config credential_id ready")

    def _migrate_v31_sync_state(self, conn: sqlite3.Connection) -> None:
        """v30 → v31: add the `sync_state` table for incremental cursor tracking.

        Pure additive — no back-fill needed. The table only fills as
        operators set ``sync_mode=incremental`` on source nodes and
        runs complete successfully. Pre-existing pipelines with the
        manual ``watermark_value`` field keep working unchanged; the
        store layers on top of the existing manual flow, only
        auto-populating ``watermark_value`` when it's blank.

        See fpulse/engine/sync_state_store.py for the read/write API
        and fpulse/nodes/db_source.py for the integration point.
        """
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sync_state (
                workflow_id    TEXT NOT NULL,
                step_id        TEXT NOT NULL,
                cursor_column  TEXT NOT NULL,
                last_cursor    TEXT,
                last_run_at    TIMESTAMP,
                rows_last_run  INTEGER DEFAULT 0,
                updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (workflow_id, step_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sync_state_workflow
                ON sync_state(workflow_id);
            """
        )

    def _migrate_v30_sink_idempotency(self, conn: sqlite3.Connection) -> None:
        """v29 → v30: add the `sink_idempotency` table.

        Pure additive — no back-fill needed. The table only fills as
        users start setting ``idempotency_key`` on external sinks
        (email/webhook/api/kafka/slack) and re-running pipelines.
        Pre-existing installs see an empty table until then; behaviour
        for sinks without the key set is unchanged.

        The CREATE TABLE IF NOT EXISTS in the TABLES block runs on
        every startup (fresh-install path); this migration is the
        idempotent safeguard for upgrade installs that reach the
        version-gated branch before the top-level CREATE block. Both
        paths converge on the same schema.

        See fpulse/sinks/dedupe_store.py for the lookup + record logic
        and fpulse/sinks/idempotency_helper.py for the per-row hash +
        skip decision used by every external-sink class.
        """
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sink_idempotency (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pipeline_id TEXT NOT NULL,
                sink_step_id TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                UNIQUE(pipeline_id, sink_step_id, key_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_sink_idempotency_lookup
                ON sink_idempotency(pipeline_id, sink_step_id, key_hash);
            CREATE INDEX IF NOT EXISTS idx_sink_idempotency_expires
                ON sink_idempotency(expires_at);
            """
        )

    def _migrate_v28_schema_history(self, conn: sqlite3.Connection) -> None:
        """v27 → v28: add the `schema_history` table.

        Pure additive — no back-fill needed. Each managed table will
        produce its first ``version=1`` row the next time its sink
        runs under a non-strict ``schema_policy``. Tables that never
        see another sink write keep their current shape in
        ``storage_columns`` without an entry here; that's intentional —
        an empty history means "never evolved under policy".

        The CREATE TABLE IF NOT EXISTS in the TABLES block runs on
        every startup (fresh-install path); this migration is the
        idempotent safeguard for upgrade installs that reach the
        version-gated branch before the top-level CREATE block.
        """
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_history (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                table_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                recorded_at TEXT NOT NULL,
                columns_json JSON NOT NULL,
                change_summary JSON NOT NULL DEFAULT '{}',
                applied_by_run_id TEXT DEFAULT '',
                policy TEXT NOT NULL DEFAULT 'add_columns'
            );
            CREATE INDEX IF NOT EXISTS idx_schema_history_table ON schema_history(table_id, version);
            CREATE INDEX IF NOT EXISTS idx_schema_history_ws ON schema_history(workspace_id);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_schema_history_table_version
                ON schema_history(table_id, version);
            """
        )

    def _migrate_v15_content_hash(self, conn: sqlite3.Connection) -> None:
        """v14 → v15: add `content_hash` column to workflow_versions.

        New rows written by WorkflowStore.save() compute and populate the
        hash. Legacy rows remain '' (empty string) and skip verification
        on rollback — intentional, avoids a back-fill pass over possibly
        thousands of versions. Pattern matches v4/v5's additive column
        migrations: PRAGMA-check, ALTER if missing, never raise.
        """
        try:
            cols = conn.execute("PRAGMA table_info(workflow_versions)").fetchall()
            if not any(c["name"] == "content_hash" for c in cols):
                conn.execute(
                    "ALTER TABLE workflow_versions ADD COLUMN content_hash TEXT DEFAULT ''"
                )
        except sqlite3.OperationalError:
            # Table may not exist on very old dbs; CREATE TABLE in TABLES
            # block (which runs before this migration) gets it right.
            pass

    def _migrate_v14_ai_config(self, conn: sqlite3.Connection) -> None:
        """v13 → v14: add `user_ai_config` and `workspace_ai_config`.

        Both tables are new — there is no back-fill. The CREATE TABLE
        IF NOT EXISTS statements in the TABLES block already run on
        every startup (fresh install path), so this migration is the
        belt-and-braces idempotent safeguard for upgrade installs that
        somehow reach this branch before TABLES has executed. Re-running
        is safe.
        """
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS user_ai_config (
                user_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                enabled INTEGER NOT NULL DEFAULT 0,
                provider TEXT DEFAULT '',
                model TEXT DEFAULT '',
                api_key_encrypted TEXT DEFAULT '',
                base_url TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_ai_config_ws ON user_ai_config(workspace_id);

            CREATE TABLE IF NOT EXISTS workspace_ai_config (
                workspace_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                provider TEXT DEFAULT '',
                model TEXT DEFAULT '',
                api_key_encrypted TEXT DEFAULT '',
                base_url TEXT DEFAULT '',
                allow_user_override INTEGER NOT NULL DEFAULT 0,
                monthly_budget_usd REAL NOT NULL DEFAULT 0,
                configured_by TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    def _migrate_v13_contracts_workspace(self, conn: sqlite3.Connection) -> None:
        """v12 → v13: workspace-scope schema_contracts.

        Back-fills the new column by inheriting from the parent workflow
        via workflow_versions (latest row). Contracts that can't be
        mapped default to 'default'.
        """
        self._add_workspace_id_column(conn, "schema_contracts", 13)
        try:
            conn.execute(
                """
                UPDATE schema_contracts
                SET workspace_id = COALESCE(
                    (SELECT wv.workspace_id
                     FROM workflow_versions wv
                     WHERE wv.workflow_id = schema_contracts.workflow_id
                     ORDER BY wv.version DESC LIMIT 1),
                    'default'
                )
                WHERE workspace_id = 'default' OR workspace_id IS NULL
                """
            )
        except sqlite3.OperationalError:
            pass

    def _migrate_v12_lifecycle_workspace(self, conn: sqlite3.Connection) -> None:
        """v11 → v12: workspace-scope the lifecycle_events audit log.

        Back-fills the new column by inheriting from the parent workflow
        (via workflow_versions latest row) and defaults to 'default' for
        orphans. Lifecycle events are immutable audit records; after this
        migration they carry a workspace_id forever.
        """
        self._add_workspace_id_column(conn, "lifecycle_events", 12)
        # Back-fill from parent workflow where possible
        try:
            conn.execute(
                """
                UPDATE lifecycle_events
                SET workspace_id = COALESCE(
                    (SELECT wv.workspace_id
                     FROM workflow_versions wv
                     WHERE wv.workflow_id = lifecycle_events.workflow_id
                     ORDER BY wv.version DESC LIMIT 1),
                    'default'
                )
                WHERE workspace_id = 'default' OR workspace_id IS NULL
                """
            )
        except sqlite3.OperationalError:
            # workflow_versions may not have workspace_id on very old DBs —
            # generic helper already defaulted all rows to 'default'.
            pass

    def _migrate_v11_variables_workspace(self, conn: sqlite3.Connection) -> None:
        """v10 → v11: workspace_id on `variables`."""
        self._add_workspace_id_column(conn, "variables", 11)

    def _migrate_v10_executions_workspace(self, conn: sqlite3.Connection) -> None:
        """v9 → v10: add workspace_id to `executions`, back-fill to 'default'.

        Same idempotent PRAGMA self-heal pattern as v6–v9. Crucially,
        we also attempt to back-fill the workspace from the parent
        workflow where possible: if the execution's workflow_id maps
        to a workflow whose latest version carries a workspace_id, we
        use that instead of blindly stamping 'default'. This keeps
        historical execution data correctly attributed after an
        upgrade on a multi-workspace install.
        """
        import json as _json
        cols = conn.execute("PRAGMA table_info(executions)").fetchall()
        col_names = {c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in cols}
        if "workspace_id" not in col_names:
            try:
                conn.execute(
                    "ALTER TABLE executions ADD COLUMN workspace_id TEXT DEFAULT 'default'"
                )
                logger.info(
                    "F-Pulse: schema v10 — added executions.workspace_id column"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_executions_workspace ON executions(workspace_id)"
        )

        # Build workflow_id → workspace_id map from workflow_versions
        # (the latest version's workspace_id is authoritative).
        wf_ws_map: dict[str, str] = {}
        try:
            wf_rows = conn.execute("""
                SELECT wv.workflow_id, wv.workspace_id
                FROM workflow_versions wv
                INNER JOIN (
                    SELECT workflow_id, MAX(version) as max_v
                    FROM workflow_versions GROUP BY workflow_id
                ) latest
                ON wv.workflow_id = latest.workflow_id
                   AND wv.version = latest.max_v
            """).fetchall()
            for wr in wf_rows:
                wid = wr["workflow_id"] if isinstance(wr, sqlite3.Row) else wr[0]
                ws = (wr["workspace_id"] if isinstance(wr, sqlite3.Row) else wr[1]) or "default"
                wf_ws_map[wid] = ws
        except Exception as exc:
            logger.warning(
                "F-Pulse: schema v10 — could not build workflow→workspace map: %s",
                exc,
            )

        # Back-fill column from map (fall back to 'default' if unknown)
        exe_rows = conn.execute(
            "SELECT id, workflow_id FROM executions WHERE workspace_id IS NULL OR workspace_id = ''"
        ).fetchall()
        for r in exe_rows:
            exe_id = r["id"] if isinstance(r, sqlite3.Row) else r[0]
            wf_id = r["workflow_id"] if isinstance(r, sqlite3.Row) else r[1]
            target_ws = wf_ws_map.get(wf_id, "default")
            conn.execute(
                "UPDATE executions SET workspace_id = ? WHERE id = ?",
                (target_ws, exe_id),
            )

        # Back-fill JSON blobs to match
        rows = conn.execute("SELECT id, workflow_id, data, workspace_id FROM executions").fetchall()
        for r in rows:
            try:
                blob = _json.loads(r["data"] if isinstance(r, sqlite3.Row) else r[2])
                if blob.get("workspace_id"):
                    continue
                blob["workspace_id"] = r["workspace_id"] if isinstance(r, sqlite3.Row) else r[3] or "default"
                conn.execute(
                    "UPDATE executions SET data = ? WHERE id = ?",
                    (_json.dumps(blob), r["id"] if isinstance(r, sqlite3.Row) else r[0]),
                )
            except Exception as exc:
                logger.warning(
                    "F-Pulse: schema v10 — could not back-fill execution blob %s: %s",
                    r["id"] if isinstance(r, sqlite3.Row) else r[0],
                    exc,
                )

    def _migrate_v9_alerts_workspace(self, conn: sqlite3.Connection) -> None:
        """v8 → v9: add workspace_id to `alert_rules` + `alert_logs`, back-fill.

        Two tables in one migration because alert_logs joins
        alert_rules conceptually — logs inherit workspace from parent
        rule. For legacy rows we back-fill both to 'default' (they
        were all single-tenant anyway).
        """
        import json as _json
        for table in ("alert_rules", "alert_logs"):
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
            col_names = {c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in cols}
            if "workspace_id" not in col_names:
                try:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN workspace_id TEXT DEFAULT 'default'"
                    )
                    logger.info(
                        "F-Pulse: schema v9 — added %s.workspace_id column", table
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_workspace ON {table}(workspace_id)"
            )
            conn.execute(
                f"UPDATE {table} SET workspace_id = 'default' WHERE workspace_id IS NULL OR workspace_id = ''"
            )
            rows = conn.execute(f"SELECT id, data FROM {table}").fetchall()
            for r in rows:
                try:
                    blob = _json.loads(r["data"] if isinstance(r, sqlite3.Row) else r[1])
                    if blob.get("workspace_id"):
                        continue
                    blob["workspace_id"] = "default"
                    conn.execute(
                        f"UPDATE {table} SET data = ? WHERE id = ?",
                        (_json.dumps(blob), r["id"] if isinstance(r, sqlite3.Row) else r[0]),
                    )
                except Exception as exc:
                    logger.warning(
                        "F-Pulse: schema v9 — could not back-fill %s blob %s: %s",
                        table,
                        r["id"] if isinstance(r, sqlite3.Row) else r[0],
                        exc,
                    )

    def _migrate_v8_schedules_workspace(self, conn: sqlite3.Connection) -> None:
        """v7 → v8: add workspace_id to `schedules` and back-fill.

        Mirrors v6/v7 exactly — PRAGMA self-heal on the column,
        `CREATE INDEX IF NOT EXISTS`, `UPDATE … WHERE workspace_id
        IS NULL OR workspace_id = ''`, then rewrite JSON blobs that
        don't already carry the field.
        """
        cols = conn.execute("PRAGMA table_info(schedules)").fetchall()
        col_names = {c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in cols}
        if "workspace_id" not in col_names:
            try:
                conn.execute(
                    "ALTER TABLE schedules ADD COLUMN workspace_id TEXT DEFAULT 'default'"
                )
                logger.info(
                    "F-Pulse: schema v8 — added schedules.workspace_id column"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_schedules_workspace ON schedules(workspace_id)"
        )
        conn.execute(
            "UPDATE schedules SET workspace_id = 'default' WHERE workspace_id IS NULL OR workspace_id = ''"
        )

        import json as _json
        rows = conn.execute("SELECT id, data FROM schedules").fetchall()
        for r in rows:
            try:
                blob = _json.loads(r["data"] if isinstance(r, sqlite3.Row) else r[1])
                if blob.get("workspace_id"):
                    continue
                blob["workspace_id"] = "default"
                conn.execute(
                    "UPDATE schedules SET data = ? WHERE id = ?",
                    (_json.dumps(blob), r["id"] if isinstance(r, sqlite3.Row) else r[0]),
                )
            except Exception as exc:
                logger.warning(
                    "F-Pulse: schema v8 — could not back-fill schedule blob %s: %s",
                    r["id"] if isinstance(r, sqlite3.Row) else r[0],
                    exc,
                )

    def _migrate_v7_credentials_workspace(self, conn: sqlite3.Connection) -> None:
        """v6 → v7: add workspace_id to `credentials` and back-fill.

        Two writes:
          1. Indexed column on the `credentials` table (fast
             workspace-scoped listing).
          2. JSON blob back-fill so Credential.workspace_id round-trips
             through model parsing.

        Safety: idempotent via PRAGMA check. Back-fill only touches
        rows that don't already carry a workspace_id, so an admin-set
        value survives re-runs. Mirrors v6 (connections) exactly.
        """
        cols = conn.execute("PRAGMA table_info(credentials)").fetchall()
        col_names = {c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in cols}
        if "workspace_id" not in col_names:
            try:
                conn.execute(
                    "ALTER TABLE credentials ADD COLUMN workspace_id TEXT DEFAULT 'default'"
                )
                logger.info(
                    "F-Pulse: schema v7 — added credentials.workspace_id column"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_credentials_workspace ON credentials(workspace_id)"
        )
        conn.execute(
            "UPDATE credentials SET workspace_id = 'default' WHERE workspace_id IS NULL OR workspace_id = ''"
        )

        # JSON blob back-fill — credentials.data IS the Credential dict
        # at the top level (same as connections). Don't overwrite
        # admin-set workspace_id values.
        import json as _json
        rows = conn.execute("SELECT id, data FROM credentials").fetchall()
        for r in rows:
            try:
                blob = _json.loads(r["data"] if isinstance(r, sqlite3.Row) else r[1])
                if blob.get("workspace_id"):
                    continue
                blob["workspace_id"] = "default"
                conn.execute(
                    "UPDATE credentials SET data = ? WHERE id = ?",
                    (_json.dumps(blob), r["id"] if isinstance(r, sqlite3.Row) else r[0]),
                )
            except Exception as exc:
                logger.warning(
                    "F-Pulse: schema v7 — could not back-fill credential blob %s: %s",
                    r["id"] if isinstance(r, sqlite3.Row) else r[0],
                    exc,
                )

    def _migrate_v4_heal_projects_workspace(self, conn: sqlite3.Connection) -> None:
        """v3 → v4: ensure projects.workspace_id exists.

        Repeats the v2 step under PRAGMA inspection so installs whose
        v2 marker got written without the column actually being added
        get fixed up. Idempotent — does nothing if the column is
        already present.
        """
        cols = conn.execute("PRAGMA table_info(projects)").fetchall()
        col_names = {c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in cols}
        if "workspace_id" in col_names:
            return
        try:
            conn.execute(
                "ALTER TABLE projects ADD COLUMN workspace_id TEXT DEFAULT 'default'"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_projects_workspace ON projects(workspace_id)"
            )
            conn.execute(
                "UPDATE projects SET workspace_id = 'default' WHERE workspace_id IS NULL OR workspace_id = ''"
            )
            logger.info(
                "F-Pulse: schema v4 — healed missing projects.workspace_id column"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

    # ── Generic helpers ──

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        """Execute a SQL statement and return cursor."""
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, params_list: list) -> sqlite3.Cursor:
        """Execute a SQL statement for multiple parameter sets."""
        return self.conn.executemany(sql, params_list)

    def commit(self):
        """Commit the current transaction."""
        self.conn.commit()

    def fetchone(self, sql: str, params: tuple | list = ()) -> dict | None:
        """Execute and fetch one row as dict."""
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return dict(row)

    def fetchall(self, sql: str, params: tuple | list = ()) -> list[dict]:
        """Execute and fetch all rows as list of dicts."""
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def insert_json(self, table: str, row_id: str, data: dict, **extra_columns):
        """Insert a row with JSON data blob + extra indexed columns."""
        cols = ["id", "data"] + list(extra_columns.keys())
        placeholders = ", ".join(["?"] * len(cols))
        col_names = ", ".join(cols)
        values = [row_id, json.dumps(data, default=str)] + list(extra_columns.values())

        self.conn.execute(
            f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})",
            values,
        )
        self.conn.commit()

    def get_json(self, table: str, row_id: str) -> dict | None:
        """Get a row's JSON data by ID."""
        row = self.fetchone(f"SELECT data FROM {table} WHERE id = ?", (row_id,))
        if row is None:
            return None
        return json.loads(row["data"])

    def delete_row(self, table: str, row_id: str) -> bool:
        """Delete a row by ID."""
        cursor = self.conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    def count(self, table: str, where: str = "", params: tuple = ()) -> int:
        """Count rows in a table."""
        sql = f"SELECT COUNT(*) as cnt FROM {table}"
        if where:
            sql += f" WHERE {where}"
        row = self.fetchone(sql, params)
        return row["cnt"] if row else 0

    def list_json(self, table: str, where: str = "", params: tuple = (), order_by: str = "") -> list[dict]:
        """List all JSON data blobs from a table."""
        sql = f"SELECT data FROM {table}"
        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        rows = self.fetchall(sql, params)
        return [json.loads(r["data"]) for r in rows]

    # ── Backup & Export ──

    def backup_to(self, dest_path: str):
        """Create a full backup of the database (safe, consistent snapshot)."""
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        dest = sqlite3.connect(dest_path)
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        logger.info("Database backed up to: %s", dest_path)

    def export_all(self) -> dict[str, Any]:
        """Export entire database as a JSON-serializable dict (for cloud backup)."""
        tables = [
            "projects", "workflow_versions", "schedules", "alert_rules",
            "alert_logs", "executions", "users", "sessions", "variables",
            "credentials", "connections", "connection_reports",
            "lifecycle_events", "schema_contracts",
        ]
        export = {"_version": SCHEMA_VERSION, "_tables": {}}
        for table in tables:
            rows = self.fetchall(f"SELECT * FROM {table}")
            export["_tables"][table] = rows
        return export

    def import_all(self, data: dict[str, Any]):
        """Import data from an export dict. Merges with existing data (INSERT OR REPLACE)."""
        tables = data.get("_tables", {})
        for table, rows in tables.items():
            if not rows:
                continue
            cols = list(rows[0].keys())
            placeholders = ", ".join(["?"] * len(cols))
            col_names = ", ".join(cols)
            for row in rows:
                values = [row.get(c) for c in cols]
                self.conn.execute(
                    f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})",
                    values,
                )
        self.conn.commit()
        logger.info("Imported %d tables", len(tables))

    def close(self):
        """Close ALL tracked thread-local connections (Stage 3a).

        Previously this only closed the calling thread's connection,
        which left worker / scheduler / backup-thread connections
        orphaned at process shutdown. SQLite then reported "database
        is locked" warnings on rapid restart because the OS hadn't
        yet flushed the WAL from those orphaned conns.

        We snapshot to a list first so we don't mutate the set while
        iterating it (a connection's close() can in principle trigger
        cleanup that touches the set). Errors closing one connection
        do not stop us from closing the others.
        """
        with self._all_conns_lock:
            conns = list(self._all_conns)
            self._all_conns.clear()

        # Best-effort final checkpoint on the calling thread's conn so
        # the WAL doesn't carry uncheckpointed pages into the next boot.
        # Wrap defensively — if WAL is already truncated this no-ops.
        if hasattr(self._local, "conn") and self._local.conn:
            try:
                self._local.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as exc:
                logger.debug("Final wal_checkpoint failed (non-fatal): %s", exc)

        closed = 0
        for c in conns:
            try:
                c.close()
                closed += 1
            except Exception as exc:
                logger.warning("Error closing SQLite connection: %s", exc)

        if hasattr(self._local, "conn"):
            self._local.conn = None

        logger.info("F-Pulse database: closed %d connection(s)", closed)
