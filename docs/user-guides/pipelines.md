# Pipelines — User Guide

**Audience:** All users
**Prerequisites:** Optionally [a project](projects.md) and [a connection](connections.md)

A **Pipeline** in F-Pulse (internally called a *workflow*) is a directed graph of nodes that reads from sources, transforms data, and writes to sinks. Pipelines are the primary unit of work in F-Pulse.

---

## Table of contents

1. [Anatomy of a pipeline](#1-anatomy-of-a-pipeline)
2. [Pipeline lifecycle (OSS)](#2-pipeline-lifecycle-oss)
3. [Creating a pipeline](#3-creating-a-pipeline)
4. [Building the graph on the canvas](#4-building-the-graph-on-the-canvas)
5. [Validating a pipeline](#5-validating-a-pipeline)
6. [Running a pipeline](#6-running-a-pipeline)
7. [Scheduling](#7-scheduling)
8. [Monitoring execution](#8-monitoring-execution)
9. [Archiving and restoring](#9-archiving-and-restoring)
10. [Cloning a pipeline](#10-cloning-a-pipeline)
11. [Exporting and importing](#11-exporting-and-importing)
12. [Versioning model](#12-versioning-model)
13. [API reference](#13-api-reference)
14. [Troubleshooting](#14-troubleshooting)
15. [F-Pulse+: governance & approvals](#15-f-pulse-governance--approvals)

---

## 1. Anatomy of a pipeline

A pipeline has four parts:

| Part | Description |
|---|---|
| **Metadata** | `name`, `description`, `project_id`, owner, tags, deployed version |
| **Steps** (nodes) | Individual operations — one per box on the canvas. Each step has a `type` (e.g. `csv_source`, `filter`, `db_sink`) and `params`. |
| **Connections** (edges) | Directed edges between steps. Each edge carries the output of the source step to the input of the destination step. |
| **Schema contract** (optional) | Declares the expected columns and types at the final sink. Enables drift detection. |

Under the hood, the pipeline is stored as a Pydantic `Workflow` object, serialised to JSON in the `workflow_versions.data` column. Every save creates a new immutable version row.

### 1.1 Node categories

F-Pulse ships with **40 nodes across 6 categories**:

| Category | Examples | Purpose |
|---|---|---|
| **Sources** (3) | `csv_source`, `db_source`, `api_source` | Read data in |
| **Transforms** (17) | `filter`, `join`, `aggregate`, `sql_transform`, `deduplicate`, `pivot`, `window`, `validate` | Change the shape of data |
| **Outputs** (2) | `output` (Parquet/CSV/JSON), `db_sink` | Write data out |
| **Flow Control** | `foreach`, `if_else`, `wait`, `fail` | Conditional and looping logic |
| **Activities** | `copy_data`, `delete`, `execute_sql`, `execute_pipeline`, `get_metadata` | Operational primitives |
| **Cloud Storage** | `file_system_task`, S3/Azure/GCS helpers | Cloud file operations |

For each connector, see the [connector catalog](../connectors.md). For each node, open the **Nodes** tab in this Help page.

---

## 2. Pipeline lifecycle (OSS)

In F-Pulse OSS, every pipeline moves through a simple set of states:

```
   ┌─────────┐   save    ┌─────────┐   /test passes   ┌───────────┐
   │  DRAFT  │ ────────▶ │  DRAFT  │ ───────────────▶ │ PUBLISHED │
   └─────────┘           └─────────┘                  └─────┬─────┘
        ▲                                                    │
        │ /restore                                           │ runs on
        │                                                    │ schedule
   ┌─────────┐                                               ▼
   │ARCHIVED │                                          ┌─────────┐
   └─────────┘                                          │ RUNNING │
                                                        └─────────┘
```

- **DRAFT** is editable by the author. Runnable as a test.
- **PUBLISHED** means at least one test passed. Runnable on a schedule.
- **ARCHIVED** is hidden from normal lists but preserved. Restore to DRAFT at any time.

> **F-Pulse+** adds two more states (`PENDING APPROVAL`, `APPROVED`, `REJECTED`) and a separate `DEPLOYED` notion that pins a specific version for PROD scheduled runs. See [section 15](#15-f-pulse-governance--approvals).

---

## 3. Creating a pipeline

### 3.1 Via the UI

1. Go to the **Pipelines** page.
2. Optionally select a project from the project switcher.
3. Click **+ New Pipeline**.
4. Enter a name and description.
5. Click **Create**. You land on the canvas.

### 3.2 Via the API

```bash
curl -X POST http://localhost:8001/api/workflows/ \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Orders ETL",
    "description": "Nightly load of orders from Postgres to S3"
  }'
```

A pipeline is created in `DRAFT` status, empty of steps.

---

## 4. Building the graph on the canvas

The canvas is where you assemble the pipeline.

### 4.1 Adding nodes

- **Drag** from the palette (left) onto the canvas, or **click** a tile to add it at the current cursor.
- Click a node to open the configuration panel (right).
- Fill in required fields (marked with `*`).
- Source and sink nodes need a **Connection** — select it from the connection picker. See [Connections Guide](connections.md).

### 4.2 Wiring nodes

- Click and drag from the **output port** (right side / bottom of a node) to the **input port** (left side / top of the next node).
- Edges show the column count at their midpoint once the upstream node has sample data.
- You cannot connect a node to itself or create cycles. The validator refuses.

### 4.3 The ghost node (AI-assisted)

After you add a node, F-Pulse proposes the next node as a translucent **ghost node** on the canvas based on common patterns. Click to accept; ignore to proceed manually.

### 4.4 Saving

Press `Ctrl+S` (`Cmd+S` on macOS) to save. Every save creates a **new version** in the backing store.

```bash
curl -X PUT http://localhost:8001/api/workflows/{id} \
  -H "Content-Type: application/json" \
  -d '{
    "workflow": { /* full Workflow object */ },
    "change_summary": "Added filter after source"
  }'
```

---

## 5. Validating a pipeline

Validation is automatic on save. To run it explicitly:

```bash
curl -X POST http://localhost:8001/api/workflows/{id}/validate
```

Validation checks:

1. **Structure** — no broken edges, no orphan nodes, no cycles
2. **Required params** — every node has its required params filled
3. **Type consistency** — source nodes use read-capable connections; sink nodes use write-capable connections
4. **Schema mapping** — if a schema contract is attached, columns line up

Structural errors block save + test. Warnings do not.

---

## 6. Running a pipeline

A test executes the pipeline end-to-end with a sample row limit.

### 6.1 Via the UI

- Click **Run** in the canvas top bar.
- Watch the live log stream at the bottom.
- On success → status becomes `PUBLISHED`, the test summary is saved to the pipeline.
- On failure → the error is shown inline with suggested fixes.

### 6.2 Via the API

```bash
curl -X POST "http://localhost:8001/api/workflows/{id}/test?preview_limit=50"
```

`preview_limit` caps rows read from each source. Default 50 in dev sample mode, unbounded in full mode.

> ⚠️ **Tests run against real connections.** A source node with a Postgres connection will actually query Postgres. Use a dev/staging database for testing.

### 6.3 Run safety modes

The toolbar **Run** button has four safety modes:

| Mode | What it does |
|---|---|
| **Live** | Run on full upstream data and write to configured destinations |
| **Sample** | Run on the first 100 rows; no effect on destinations |
| **Dry-run** | Plan only — validate the IR and produce previews without writing |
| **Validate-only** | Schema + connection sanity check; no execution |

---

## 7. Scheduling

Schedules are managed through `/api/schedules/*`.

- A schedule has a cron expression, a workflow ID, and a trigger pattern.
- Scheduler runs inside the backend process; admitted through the resource governor.

Common cron patterns:

| Cron | Meaning |
|---|---|
| `0 2 * * *` | Nightly at 2 AM |
| `*/15 * * * *` | Every 15 minutes |
| `0 0 1 * *` | First of each month at midnight |
| `0 9 * * 1-5` | 9 AM on weekdays |

Schedules also support interval / daily / weekly / monthly shorthand modes — see the **Scheduling** page in the UI.

---

## 8. Monitoring execution

Every pipeline run creates an **ExecutionRecord** with:

- `id`, `workflow_id`, `workflow_name`, `project_id`
- `status` — `success`, `error`, `timeout`, `cancelled`
- `triggered_by` — `manual`, `schedule`, `event`, `test`
- `started_at`, `completed_at`, `duration_ms`
- `steps_total`, `steps_completed`, `steps_failed`
- `step_logs[]` — per-step log with row count, duration, error
- `rows_processed_total`
- `workflow_snapshot` — the full Workflow IR at execution time

Access via the **Executions** page (filter by workflow, status, time range) or the API:

```bash
curl "http://localhost:8001/api/executions?workflow_id=wf_..."
```

### 8.1 Cancelling a running pipeline

```bash
curl -X POST http://localhost:8001/api/admin/execution/{handle_id}/cancel
```

The execution's subprocess is sent SIGTERM, then SIGKILL after the grace period.

### 8.2 Logs

Per-step logs are truncated to 1 MB by default (configurable via `FPULSE_STEP_LOG_MAX_MB`). Full raw logs live on disk under `exec_logs/`.

---

## 9. Archiving and restoring

Archive hides a pipeline from normal lists while preserving all versions and history.

```bash
curl -X POST http://localhost:8001/api/workflows/{id}/archive
curl -X POST http://localhost:8001/api/workflows/{id}/restore
```

An archived pipeline cannot be tested, disappears from the default Pipelines page (toggle **Show archived** to see it), and keeps all its version rows + execution history. Schedules become inert. Restore to `DRAFT` at any time.

---

## 10. Cloning a pipeline

Cloning creates a new pipeline with a fresh ID, copying all steps and connections but none of the history.

```bash
curl -X POST "http://localhost:8001/api/workflows/{id}/clone?name=Orders+ETL+v2"
```

Use clone when you want to iterate on a variant without affecting the original.

---

## 11. Exporting and importing

Export produces portable JSON that can be imported into any F-Pulse instance.

### 11.1 Export

```bash
curl http://localhost:8001/api/workflows/{id}/export > orders-etl.json
```

Internal IDs are stripped. Connection references are kept as **names**, not IDs, so the importer can remap them.

### 11.2 Import

```bash
curl -X POST http://localhost:8001/api/workflows/import \
  -H "Content-Type: application/json" \
  -d '{
    "pipeline": { /* the exported pipeline object */ },
    "rename": "Orders ETL (imported)",
    "connection_map": {
      "orders-dev-pg": "conn_abc",
      "s3-dev-bucket": "conn_xyz"
    }
  }'
```

The `connection_map` replaces connection-name references with connection IDs. If you omit it, the import still works but source/sink nodes will be unconfigured until you set connections manually.

---

## 12. Versioning model

Every save creates a new version row in `workflow_versions`.

- `version` — monotonically increasing integer per workflow
- `created_at`, `change_summary` — audit metadata
- `data` — the full Workflow IR as JSON
- `content_hash` — SHA-256 of the canonicalised IR (v15+)

| Endpoint | Effect |
|---|---|
| `GET /api/workflows/{id}` | Returns latest version |
| `GET /api/workflows/{id}?version=N` | Returns version N |
| `GET /api/workflows/{id}/versions` | Lists all versions |
| `GET /api/workflows/{id}/diff?v1=N&v2=M` | Diffs two versions |

Versions are **immutable**. Deleting the workflow deletes all its versions; individual versions cannot be deleted.

---

## 13. API reference

Key endpoints (full list in the [API reference](../api.md)):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/workflows/` | List workflows |
| `POST` | `/api/workflows/` | Create a workflow |
| `GET` | `/api/workflows/{id}` | Get latest version |
| `PUT` | `/api/workflows/{id}` | Save a new version |
| `DELETE` | `/api/workflows/{id}` | Delete workflow and all versions |
| `GET` | `/api/workflows/{id}/versions` | List all versions |
| `POST` | `/api/workflows/{id}/validate` | Validate structure |
| `POST` | `/api/workflows/{id}/test` | Run a test |
| `POST` | `/api/workflows/{id}/archive` | Archive |
| `POST` | `/api/workflows/{id}/restore` | Restore from archive |
| `POST` | `/api/workflows/{id}/clone` | Clone a pipeline |
| `GET` | `/api/workflows/{id}/export` | Export as portable JSON |
| `POST` | `/api/workflows/import` | Import from portable JSON |

---

## 14. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Test execution failed: connection refused" | Source/sink connection unreachable | Test the connection under **Connections → Test Connection** |
| "Cannot test an archived pipeline" | Pipeline is in ARCHIVED state | `POST /{id}/restore` first |
| "Pipeline has not been tested yet" | Test has never passed | Run `/test` at least once |
| Pipeline saves but won't run on schedule | Pipeline is in DRAFT (test never passed) | Run a successful test first |
| Schedule doesn't fire on time | System clock skew or scheduler stalled | Check the **Pool** page Governor banner |
| "Cannot rollback to vN: content hash mismatch" | The version's `data` row was modified post-save | This is a tamper-evidence signal — investigate |
| Imported pipeline has unconfigured connections | `connection_map` was incomplete | Edit each source/sink node and pick a connection |

---

## 15. F-Pulse+: governance & approvals

F-Pulse+ adds production governance on top of the OSS lifecycle:

| Feature | Effect |
|---|---|
| **DEV/PROD environments** | Pipelines exist in DEV by default. Promotion to PROD requires the approval workflow below. |
| **Submit for review** | Sets `approval_status = "pending"`, notifies approvers via in-app + email/Slack/Teams |
| **Plan stage** | Diff modal showing exactly what will change vs the currently-deployed version, with baseline run statistics |
| **Two-gate approval** | Gate 1: Sandbox approval — run against PROD connections in an isolated namespace. Gate 2: Deploy approval — review sandbox evidence and approve. |
| **Pinned `deployed_version`** | PROD schedules execute the approved version, not "whatever is latest" |
| **Pre-deploy checklist** | 8+ automated checks: structural validation, approval, test history, connections mapped, schedule set, alerts configured |
| **Rollback with hash verification** | Roll back to any prior version; the stored content hash is re-verified before pinning |
| **Approval gates** | Per-pipeline / per-project / global approver lists; supports two-person rule |
| **Activate/Deactivate** | Pause a deployed pipeline without un-deploying; approval-gated in PROD |
| **Audit log** | Every approval, deploy, rollback, activate event is persisted with retention controls |

[Learn more about F-Pulse+ →](https://hybridyn.com/f-pulse#fpulse-plus)

---

**Document change log**

| Date | Change |
|---|---|
| 2026-05-03 | Rewritten for F-Pulse OSS audience. DEV→PROD approval flow + admin-tier features consolidated into section 15 (F-Pulse+ section). |
| 2026-04-22 | Initial publication. |
