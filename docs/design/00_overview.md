# F-Pulse OSS — System Architecture Overview

**Audience:** engineers joining the codebase, integrators evaluating
embedding paths, security reviewers building a threat model.

**Status:** 1.0-rc. This document reflects the architecture as of
2026-05-30, after schema v31 (`sync_state` table) and the macro /
preview-mode / per-row formula sweep.

---

## 1. The 30-second pitch

F-Pulse is a **local-first data pipeline orchestrator**. A single process
hosts:

- a **React canvas** where users draw DAGs of nodes (sources →
  transforms → sinks);
- a **FastAPI** backend that persists those DAGs, schedules them,
  serves run history, exposes connector + lineage + AI APIs;
- a **DuckDB-powered execution engine** that runs each saved DAG
  topologically, with per-step caching, retries, dry-run preview,
  schema-policy enforcement, and per-source cursor tracking;
- a **SQLite operational store** (one file under `FPULSE_DATA_DIR`)
  for workflows, runs, schedules, alerts, audit log, sync cursors,
  managed-table metadata.

Everything ships in one container. No external services required to
start; AI, cloud connectors, and JDBC drivers all degrade gracefully
when their providers/credentials are absent.

---

## 2. Process model (deployment view)

```
┌──────────────────────────────────────────────────────────────────┐
│   Browser (or hybrid-OS desktop wrapper)                          │
│   • React + Vite SPA at http://localhost:5174                     │
│   • Talks to backend via /api/* (same-origin or CORS)             │
└─────────────────────────────────┬────────────────────────────────┘
                                  │  HTTP/JSON + WebSocket
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│   F-Pulse backend (single uvicorn process at port 8001)           │
│   ┌─────────────────────────────────────────────────────────┐    │
│   │  FastAPI app                                            │    │
│   │   • 60+ /api/* routers                                  │    │
│   │   • lifespan: starts all stores + scheduler             │    │
│   │  Auth: cookies + bearer, RBAC by role rank              │    │
│   │  Static SPA: serves frontend dist/                      │    │
│   └──────────────────────┬──────────────────────────────────┘    │
│                          │                                       │
│   ┌──────────────────────▼──────────────────────────────────┐    │
│   │  WorkflowExecutor                                       │    │
│   │   • topological sort                                    │    │
│   │   • retry / timeout / on_error policy (Settings tab)    │    │
│   │   • preview_mode short-circuit for SIDE_EFFECT_CLASS    │    │
│   │   • step cache + checkpoint store for resume            │    │
│   │   • workflow-level retry budget                         │    │
│   └──────────────────────┬──────────────────────────────────┘    │
│                          │                                       │
│   ┌──────────────────────▼──────────────────────────────────┐    │
│   │  Node registry (99 BaseNode subclasses)                 │    │
│   │   sources / transforms / sinks / actions / control      │    │
│   │   AI / semantic / connector framework                   │    │
│   └──────────────────────┬──────────────────────────────────┘    │
│                          │                                       │
│   ┌──────────────────────▼─────────────────┬────────────────┐    │
│   │ DuckDB (in-process)                    │ Per-run worker │    │
│   │ • Vectorised analytics on relations    │ threads        │    │
│   │ • Reads CSV/Parquet/JSON; writes same  │                │    │
│   │ • The data plane.                      │                │    │
│   └────────────────────────────────────────┴────────────────┘    │
│                          │                                       │
│   ┌──────────────────────▼──────────────────────────────────┐    │
│   │  SQLite operational store (FPULSE_DATA_DIR/fpulse.db)   │    │
│   │   workflows, runs, schedules, alerts, audit, sync_state,│    │
│   │   storage_tables/columns/objects, vault_secrets, ...    │    │
│   │   Schema version: 31 (auto-migrated on boot)            │    │
│   └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│   ┌─────────────────────────────────────────────────────────┐    │
│   │  Optional sidecars (all degrade gracefully)             │    │
│   │   • Ollama (local LLM)        — qwen2.5:7b floor        │    │
│   │   • Cloud LLM providers       — Anthropic/OpenAI/...    │    │
│   │   • External DBs (Postgres/MySQL/MSSQL/...)              │    │
│   │   • Object storage (S3/GCS/Azure Blob/...)              │    │
│   │   • SaaS APIs via manifest framework                    │    │
│   └─────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. The four canonical entities

Every part of the system pivots around these. They map 1:1 between
backend Pydantic models, SQLite rows, and frontend types.

| Entity | Backend type | Persisted as | Frontend type |
|---|---|---|---|
| **Workflow** | `fpulse.ir.schema.Workflow` | `workflow_versions` (JSON blob + indexed cols) | `Workflow` (`frontend/src/types.ts`) |
| **Step** | `fpulse.ir.schema.Step` | nested in Workflow blob | `FPulseNode` (React Flow node) |
| **Connection (edge)** | `fpulse.ir.schema.StepConnection` | nested in Workflow blob | React Flow edge |
| **Run** | `fpulse.ir.schema.WorkflowRunResult` + per-step `StepRunResult` | `executions` + `execution_logs` + `pipeline_checkpoints` | response payload + lineage view |

Every other table (connections, credentials, schedules, alerts,
variables, sync_state, storage_tables, …) hangs off these four via
explicit FKs.

---

## 4. Trust boundaries

```
┌───────────────────────┐   1. Anonymous read of /api/health, /docs
│ Anonymous              │
└─────────┬─────────────┘
          │ 401 / 403 / 402 elsewhere
          ▼
┌───────────────────────┐   2. Any logged-in user can READ scoped data
│ Authenticated          │      (their workspace's pipelines, runs, ...)
│ (role >= viewer)       │
└─────────┬─────────────┘
          │ require_min_rank("developer")
          ▼
┌───────────────────────┐   3. developer+ can WRITE pipelines, schedules
│ Author tier            │      variables, alerts
│ (role >= developer)    │
└─────────┬─────────────┘
          │ require_role("super_admin","admin","workspace_admin")
          ▼
┌───────────────────────┐   4. admin+ can mutate credentials, settings,
│ Admin tier             │      projects, deploy/rollback
│ (role >= admin)        │
└───────────────────────┘
```

Cross-cutting concerns:
- **Workspace isolation** — every store query filters by
  `workspace_id` resolved from the request via
  `current_workspace_id`; a member of workspace A literally cannot
  see workspace B's rows.
- **Project ACL** — within a workspace, projects can have explicit
  access lists; the workflow create / move path enforces it.
- **Side-effect classification** —
  `fpulse.ir.node_metadata.SIDE_EFFECT_CLASS` tags every node as
  `passthrough` / `transforming` / `terminal` / pure. The UI shows
  a badge, the executor uses it to short-circuit preview runs.

---

## 5. Pipeline lifecycle (state machine)

```
                   ┌──────────┐
                   │  draft   │  ← create_workflow returns here
                   └────┬─────┘
                        │ POST /test
                        ▼
                   ┌──────────┐  test_results.pass=true
                   │ tested   │ ──────────────────────┐
                   └────┬─────┘                       │
                        │ POST /publish               │
                        ▼                             │
                   ┌──────────┐                       │
                   │published │                       │
                   └────┬─────┘                       │
                        │ POST /submit-for-review     │
                        ▼                             │
                   ┌──────────┐  POST /approve        │
                   │  review  │ ────────┐             │
                   └──────────┘         ▼             │
                                  ┌──────────┐        │
                                  │ approved │        │
                                  └────┬─────┘        │
                                       │ POST /submit-for-deploy
                                       ▼              │
                                  ┌──────────────┐    │
                                  │deploy-pending│    │
                                  └────┬─────────┘    │
                                       │ POST /approve-deploy
                                       ▼              │
                                  ┌──────────┐        │
                                  │ deployed │ ←──────┘ (also: rollback)
                                  └────┬─────┘
                                       │ POST /archive
                                       ▼
                                  ┌──────────┐
                                  │ archived │
                                  └──────────┘
```

The two-gate approval (review + deploy) is enforced server-side; UI
buttons disable based on the current state + caller's role.

---

## 6. Where to go next

- **`01_backend_lld.md`** — backend module map, class diagrams,
  pseudo code for executor + nodes + stores.
- **`02_frontend_lld.md`** — component tree, Zustand store,
  React Flow integration, ConfigPanel pattern.
- **`03_functional_flows.md`** — end-to-end sequence diagrams for
  the 5 most-traveled flows (create+run, backfill, incremental sync,
  AI Q&A, publish-as-macro).
