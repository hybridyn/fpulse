# F-Pulse v1.0.0 — API Reference

**Base URL:** `http://localhost:8001/api`
**Interactive Docs:** `http://localhost:8001/docs` (Swagger UI)

## Health & Utility

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check with scheduler & notifier status |
| GET | `/scheduler/status` | Background scheduler status & active jobs |
| GET | `/node-types` | Available node types for canvas palette |
| POST | `/upload` | Upload CSV/JSON/Parquet file |
| GET | `/files` | List available data files |

## Workflows

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/workflows/` | List all workflows (latest version) |
| POST | `/workflows/` | Create new workflow |
| GET | `/workflows/{id}` | Get workflow (latest or specific version) |
| PUT | `/workflows/{id}` | Update workflow (creates new version) |
| DELETE | `/workflows/{id}` | Delete workflow and all versions |
| GET | `/workflows/{id}/versions` | List all versions |
| GET | `/workflows/{id}/diff?v1=N&v2=M` | Diff two versions |
| POST | `/workflows/{id}/validate` | Validate workflow structure |
| POST | `/workflows/{id}/test` | Test run (auto-publishes on success) |
| POST | `/workflows/{id}/publish` | Publish workflow |
| POST | `/workflows/{id}/archive` | Archive workflow |
| POST | `/workflows/{id}/restore` | Restore archived workflow |
| GET | `/workflows/{id}/lifecycle` | Get lifecycle event history |

### Create Workflow
```json
POST /api/workflows/
{
  "name": "My Pipeline",
  "description": "Loads and transforms data",
  "project_id": "default",
  "steps": [
    {"id": "s1", "type": "csv_source", "label": "Load CSV", "params": {"file_path": "data.csv"}},
    {"id": "s2", "type": "filter", "label": "Filter", "params": {"condition": "amount > 100"}}
  ],
  "connections": [
    {"from_step": "s1", "to_step": "s2"}
  ]
}
```

## Execution

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/execute/workflow/{id}` | Execute entire workflow |
| POST | `/execute/workflow/{id}/step/{step_id}` | Execute single step (with deps) |
| POST | `/execute/workflow/{id}/pre-validate` | Pre-execution data validation |
| POST | `/preview` | Preview data file |

### Execution Response
```json
{
  "workflow_id": "abc123",
  "status": "success",
  "duration_ms": 145.2,
  "step_results": {
    "s1": {
      "step_id": "s1",
      "status": "success",
      "row_count": 1000,
      "columns": ["id", "name", "amount"],
      "sample_data": [{"id": 1, "name": "Alice", "amount": 100}],
      "schema_info": [{"name": "id", "type": "BIGINT"}, ...],
      "duration_ms": 23.5
    }
  }
}
```

## Projects

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/projects/` | List projects with pipeline counts |
| POST | `/projects/` | Create project |
| GET | `/projects/{id}` | Get project |
| PUT | `/projects/{id}` | Update project |
| DELETE | `/projects/{id}` | Delete project (not "default") |
| GET | `/projects/{id}/pipelines` | List pipelines in project |

## Schedules

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/schedules/` | List schedules (filter by workflow/project) |
| POST | `/schedules/` | Create schedule |
| GET | `/schedules/{id}` | Get schedule |
| PUT | `/schedules/{id}` | Update schedule |
| DELETE | `/schedules/{id}` | Delete schedule |
| POST | `/schedules/{id}/toggle` | Enable/disable schedule |
| POST | `/schedules/{id}/run-now` | Manually trigger |

### Schedule Types
- **interval** — Run every N minutes (`interval_minutes`)
- **daily** — Run at specific time (`daily_time: "HH:MM"`)
- **weekly** — Run on specific days (`weekly_days: [0-6]`, `weekly_time`)
- **cron** — Full cron expression (`cron_expression: "0 */6 * * *"`)
- **event** — Triggered externally (`event_trigger`, `event_source_id`)

## Alerts

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/alerts/rules` | List alert rules |
| POST | `/alerts/rules` | Create alert rule |
| GET | `/alerts/rules/{id}` | Get rule |
| PUT | `/alerts/rules/{id}` | Update rule |
| DELETE | `/alerts/rules/{id}` | Delete rule |
| POST | `/alerts/rules/{id}/test` | Send test notification |
| GET | `/alerts/logs` | List alert logs |

### Alert Channels
- **email** — SMTP delivery (dry-run if SMTP_HOST not set)
- **slack** — Incoming webhook
- **teams** — Adaptive Card via webhook
- **webhook** — Generic JSON POST

### Alert Conditions
- `on_failure` — Pipeline failed
- `on_success` — Pipeline succeeded
- `on_any` — Any completion
- `on_long_running` — Duration exceeds threshold
- `on_sla_breach` — SLA violation

## Monitor

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/monitor/executions` | List executions (filter by workflow/project) |
| GET | `/monitor/executions/{id}` | Get execution detail with step logs |
| GET | `/monitor/stats` | 24h execution statistics |
| GET | `/monitor/stats/multi` | 24h/7d/30d stats |
| GET | `/monitor/active-schedules` | Enabled schedules |
| GET | `/monitor/failed` | Currently-failing pipelines (latest run failed; `failure_count` = consecutive streak) |

## Auth

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/auth/login` | Login (email + password) → token |
| POST | `/auth/register` | Register new user → token |
| GET | `/auth/me` | Get current user (requires Bearer token) |
| POST | `/auth/logout` | Invalidate session |
| GET | `/auth/users` | List all users |
| POST | `/auth/invite` | Invite user with temp password |
| DELETE | `/auth/users/{id}` | Delete user (not admin) |

## Variables

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/variables/` | List variables (filter by scope/project) |
| POST | `/variables/` | Create variable |
| GET | `/variables/{id}` | Get variable |
| PUT | `/variables/{id}` | Update variable |
| DELETE | `/variables/{id}` | Delete variable |
| POST | `/variables/resolve` | Resolve variable value |

### Variable Types
- `string` — Plain text
- `secret` — Masked in responses
- `number` — Numeric value
- `boolean` — True/false

### Scoping
- `global` — Available everywhere
- `project` — Scoped to a project (overrides global with same key)

## Credentials

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/credentials/` | List credentials (secrets masked) |
| POST | `/credentials/` | Create credential |
| GET | `/credentials/{id}` | Get credential (masked) |
| PUT | `/credentials/{id}` | Update credential |
| DELETE | `/credentials/{id}` | Delete credential |
| POST | `/credentials/{id}/test` | Test connectivity |

## Connections

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/connections/types` | List supported connection types |
| GET | `/connections/` | List connections |
| POST | `/connections/` | Create connection |
| GET | `/connections/{id}` | Get connection |
| PUT | `/connections/{id}` | Update connection |
| DELETE | `/connections/{id}` | Delete connection |
| POST | `/connections/{id}/test` | Test connection |
| POST | `/connections/{id}/reports` | Create parameterized report |
| POST | `/connections/{id}/reports/{report_id}/run` | Run report |

## Intelligence

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/intelligence/detect-schema` | Detect schema from CSV/JSON/XML |
| POST | `/intelligence/flatten` | Flatten nested data |
| POST | `/intelligence/suggest-pipeline` | Suggest pipeline from schema |

## Schema Contracts

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/contracts/` | Create schema contract |
| GET | `/contracts/{id}` | Get contract |
| POST | `/contracts/{id}/validate` | Validate schema against contract |
| GET | `/contracts/{workflow_id}/{step_id}/drift` | Check drift |

## Planner

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/planner/generate` | Generate workflow from intent |
| GET | `/planner/templates` | List pipeline templates |
| POST | `/planner/chat` | AI chat for pipeline building |

## Steward

Read-only background reliability layer. Detects duplicate sources,
duplicate pipelines, and (in later sub-agent releases) failure
patterns + schema drift. Ships in OSS — full design rationale in
[steward/architecture.md](steward/architecture.md). All endpoints
auth-gated, all responses workspace-scoped.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/steward/findings?status=open` | Run a fresh scan and return findings matching `status` (`open` default; pass `status=all` to include dismissed / resolved / stale). Sub-50ms on typical OSS workspaces. |
| POST | `/steward/scan` | Force a re-scan. Backs the UI "Re-scan" button. Same detection path as `GET /findings`, returns `{scanned: true, count: N}`. |
| POST | `/steward/findings/{id}/dismiss` | Mark a finding as intentional and permanently suppress its signature. Optional body `{"reason": "..."}` is recorded in the memory journal for the future Curator sub-agent. Also marks related notification-bell rows as read. |
| POST | `/steward/findings/{id}/resolve` | Close a finding because action was taken. Does NOT permanently suppress (if the pattern recurs, it re-emits as `(rebounded)`). Also marks related notification-bell rows as read. |
| GET | `/steward/settings` | Per-workspace settings (enabled, min_severity, scan_on_save, auto_stale_days, escalate_after_n_occurrences, notify_on_finding, notify_min_severity). |
| PUT | `/steward/settings` | Partial-update friendly — pass only fields to change. Server merges, re-validates, persists to `<data_dir>/steward/<ws>/settings.json`. |
| GET | `/steward/memory?limit=200` | Recent journal events newest-first + persistent occurrence counts per signature. Backs the "Memory" tab and proves the learning behaviour to users. |
| GET | `/steward/memory/stats` | Aggregate counters (`total_events`, `total_scans`, `total_emits`, `total_dismisses`, `total_resolves`, `distinct_signatures_seen`). |

Each finding payload has this shape:

```json
{
  "id": "dup-src-a1b2c3d4e5f6g7h8",
  "kind": "duplicate_source",
  "severity": "p2",
  "status": "open",
  "title": "3 pipelines read the same source",
  "body": "The same source object is read by 3 pipelines...",
  "evidence": {
    "source_signature": "a1b2c3d4e5f6g7h8",
    "workflows": [{"id": "...", "name": "..."}, ...]
  },
  "proposed_actions": [
    {"label": "Consolidate via Managed Table", "action": "...", "params": {...}}
  ],
  "first_seen": "2026-06-05T10:14:32.000Z",
  "last_seen": "2026-06-05T10:14:32.000Z",
  "occurrences": 3
}
```

Severity enums: `p1` / `p2` / `p3`. Kind enums: `duplicate_source`,
`duplicate_pipeline` (1.1 — shipped); `failure_rca` (1.2),
`volume_anomaly` / `schema_drift` (1.3), `cost_recommendation` (2.0)
land progressively.

## F-Pulse Memory Layer (Lessons)

Durable, human-approved lessons distilled from operator decisions.
Distinct from the operational event journal at `/api/steward/memory*`
above. Full conceptual description in
[steward/memory-layer.md](steward/memory-layer.md).

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/steward/lessons?status=approved&lesson_type=failure_pattern&source=X&pipeline=Y` | List lessons with optional filters. Newest first by `last_validated`. Auto-ages APPROVED lessons whose `validity_days` have elapsed. |
| GET | `/steward/lessons/stats` | Counters: `total_lessons`, `by_status` (proposed/approved/rejected/stale), `by_type` (10 categories). |
| GET | `/steward/lessons/{id}` | Single lesson by ID. |
| POST | `/steward/lessons` | Propose a new lesson. Body: `{lesson_type, issue, approved_fix, source?, pipeline?, symptom?, root_cause?, evidence?, tags?}`. Starts in `PROPOSED` status — does NOT influence Steward reasoning until approved. |
| POST | `/steward/lessons/{id}/approve` | Body: `{"approver": "..."}`. PROPOSED → APPROVED. The lesson now influences search and recommendations. |
| POST | `/steward/lessons/{id}/reject` | Body: `{"reviewer": "...", "reason": "..."}`. Marks the proposal incorrect; reason stored in evidence for audit. |
| POST | `/steward/lessons/{id}/revalidate` | Body: `{"reviewer": "..."}`. Bumps `occurrence_count`, refreshes `last_validated`, may promote confidence LOW→MEDIUM→HIGH. Revives STALE → APPROVED. |
| POST | `/steward/lessons/search` | Body: `{"source": "...", "error": "...", "max_results": 5}`. Step 2 of the failure-recovery workflow — returns matching APPROVED lessons ranked by confidence + `occurrence_count`. |
| DELETE | `/steward/lessons/{id}` | Hard-delete. Only suitable for REJECTED proposals — APPROVED lessons should be marked STALE so the audit trail survives. |

Lesson categories (`lesson_type` enum): `source_quirk`, `schema_drift`,
`failure_pattern`, `transformation_rule`, `retry_rule`, `cost_anomaly`,
`duplicate_warning`, `sla_pattern`, `user_fix`, `security_finding`.

Status enum: `proposed` / `approved` / `rejected` / `stale`.
Confidence enum: `low` / `medium` / `high`.

Lesson on-disk storage at
`<data_dir>/steward/<workspace_id>/lessons/<id>.{yaml,json}` — both
files are written together inside a file lock and cannot drift.

## Node Types

| Type | Category | Required Params |
|------|----------|-----------------|
| `csv_source` | source | `file_path` |
| `db_source` | source | `query` |
| `api_source` | source | `url` |
| `filter` | transform | `condition` |
| `transform` | transform | `expression` |
| `aggregate` | transform | `group_by`, `functions` |
| `join` | transform | `join_key` (2 inputs required) |
| `deduplicate` | transform | `key` |
| `sort` | transform | `column` |
| `rename` | transform | `columns` |
| `output` | sink | `format` |
| `db_sink` | sink | `connection_id`, `table` |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FPULSE_PORT` | `8001` | API server port |
| `FPULSE_DATA_DIR` | `./data` | Data file directory |
| `SMTP_HOST` | _(empty)_ | SMTP server for email alerts |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USER` | _(empty)_ | SMTP username |
| `SMTP_PASS` | _(empty)_ | SMTP password |
| `SMTP_FROM` | `fpulse@localhost` | Sender address |
| `SMTP_TLS` | `true` | Enable STARTTLS |
