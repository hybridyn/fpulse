# Architecture overview

F-Pulse is a single-process Python application with a React frontend. Pipelines are described as engine-agnostic IR (intermediate representation) and executed by an in-process DuckDB engine. Everything you need lives in one binary.

```
+-----------------------------------------------------------------+
|  Frontend (React 18 + Vite + React Flow + Zustand)              |
|  Canvas -> ConfigPanel -> PreviewPanel -> Copilot               |
+--------------------------+--------------------------------------+
                           | REST API + WebSocket
+--------------------------v--------------------------------------+
|  Backend (FastAPI + SQLite + DuckDB)                            |
|                                                                 |
|  +---------+  +----------+  +---------+  +------------------+   |
|  |   API   |  | Planner  |  | Engine  |  |       IR         |   |
|  | Routes  |->| (Rule +  |->| DuckDB  |  |  Schema + Ver +  |   |
|  |         |  |  AI opt.)|  | Executor|  |  Workflow snap   |   |
|  +---------+  +----------+  +---------+  +------------------+   |
|                                                                 |
|  +------------------+  +--------------+  +-----------------+    |
|  |   Worker Pool    |  |  Scheduler   |  | Notification    |    |
|  | (priority queue, |  | (cron, dep,  |  |   Service       |    |
|  |  governor, P1-P5)|  |  backfill)   |  | (email, Slack..)|    |
|  +------------------+  +--------------+  +-----------------+    |
|                                                                 |
|  +------------------+  +--------------+  +-----------------+    |
|  |   AI Agent       |  |  AI Helpers  |  |   Trace Store   |    |
|  | (governed tools) |  |  (inline)    |  | (per-run logs)  |    |
|  +------------------+  +--------------+  +-----------------+    |
+-----------------------------------------------------------------+
```

## Key design decisions

- **IR-first.** Every pipeline is an engine-agnostic JSON document with a versioned schema (currently v22). The IR is what's persisted, exported, executed, and audited — the canvas is just one view of it.
- **DuckDB execution.** All data processing happens in-process via DuckDB. Fast, zero-config, scales comfortably on a tuned single node. See [scaling](scaling.md).
- **Rule-based planner.** Natural language to pipeline IR happens via deterministic rules — no LLM required. AI is an optional accelerator.
- **Per-node preview.** Each node can be executed independently for instant feedback in the editor.
- **SQLite persistence.** A small set of stores (workflows, projects, schedules, alerts, executions, users, variables, credentials, connections, AI config, schema contracts) live in a single database file at `$FPULSE_DATA_DIR/fpulse.db`.
- **WebSocket for live data.** Execution logs stream through a single WebSocket per pipeline run.
- **Priority-aware worker pool.** Pipelines run on a thread-backed pool with five priority lanes (P1 highest, P5 lowest) and a resource governor.

## Lifespan

`fpulse.main:app` boots via FastAPI lifespan in this order:

1. Architecture invariant test (worker-role guard, etc.)
2. Database open and schema migrate (currently v22)
3. Storage layer (Workflow / Project / Schedule / Alert / Execution / User / Variable / Credential / Connection / AI config / Schema contract stores)
4. Engine layer (Worker Pool + Resource Governor + Execution Manager)
5. Scheduler + Backup Scheduler
6. Notification Service
7. AI subsystem (Trace Store + Wallet Guard + Idempotency + Prompt Signer + Agent Runner)
8. API routes registered
9. Health endpoints come online

Shutdown reverses the order: worker pool drain, scheduler stop, backup scheduler stop, DB close. This prevents mid-write-to-closed-DB crashes during container `stop` signals.

## Repository layout

Top-level directories you'll encounter:

- `backend/fpulse/` — Python package: API routes, planner, engine, storage, scheduler, AI subsystem
- `frontend/` — React 18 + Vite app
- `docs/` — user and developer documentation (you are here)
- `tests/` — Python test suite, including architecture-invariant tests
- `connectors/` — connector manifests and per-connector resources

## Storage schema versioning

Every backwards-incompatible schema change is a numbered migration in `storage/database.py`. The current head is **v22**. Migrations are forward-only and idempotent — running F-Pulse against an old database always upgrades automatically.

## Threading model

F-Pulse uses one Python process with a small set of long-running threads:

- **Main async loop** — FastAPI request handlers, WebSockets
- **Scheduler thread** — checks schedules every 30 seconds
- **Worker pool threads** — sized by `FPULSE_MAX_CONCURRENT_RUNS` (default 4)
- **Worker pool watchdog** — checks deadlines and long-running thresholds every 5 seconds
- **Backup scheduler** — daily snapshots

Each pipeline gets its own DuckDB connection so concurrent runs don't contend for memory. DuckDB itself is multithreaded; the per-pipeline thread cap is `FPULSE_DUCKDB_THREADS` (default = half of available CPUs).

## Why single-node?

Two reasons:

1. **Simplicity.** Most data teams aren't running petabytes — they're running gigabytes nightly. A single-node engine is faster, easier to debug, and easier to reason about than a distributed cluster.
2. **DuckDB.** DuckDB is among the fastest single-node analytical engines available. Building on it gives F-Pulse credible performance at any scale below the lakehouse threshold.

For team and multi-host deployments, F-Pulse+ is a paid extension for teams; see [hybridyn.com/f-pulse](https://hybridyn.com/f-pulse) for details.

## The Steward layer (above execution)

Sitting **above** the executor — never blocking it — is the
**F-Pulse Steward**: a read-only background reliability + learning
layer that consumes workflow-store snapshots + the audit log and
produces findings (duplicate sources, duplicate pipelines, with
planned sub-agents for failure RCA, schema drift, cost anomalies).

The Steward is composed of sub-agents that share a single surface
(eye-icon header dropdown + notification bell), a single memory
journal (JSONL at `<data_dir>/steward/<ws>/memory.jsonl`), and a
single per-workspace settings file. New sub-agents inherit the
surface, memory, notifications, suppression model, and settings —
they only have to produce `StewardFinding` records.

Five hard architectural rules:
1. **Read-only** — never mutates workflows, connections, credentials, schedules.
2. **Out-of-band** — never blocks the executor.
3. **Deterministic core, LLM-narration shell** — detection is plain code; LLM only phrases findings, never gates correctness.
4. **Explicit provenance** — every finding carries the input IDs it inspected.
5. **OSS-first** — core detection ships in OSS; Plus adds team-scale features around it.

Full design rationale: [steward/architecture.md](steward/architecture.md).
User-facing docs: [steward/overview.md](steward/overview.md).

## See also

- [Developer guide](dev-guide.md) — code conventions and how to extend F-Pulse
- [AI boundary contract](ai-boundary-contract.md) — agent invariants
- [Editions guide](editions.md) — what's in this build
- [Steward overview](steward/overview.md) — read-only reliability + learning layer
- [Steward architecture](steward/architecture.md) — design rationale + extension model
