# F-Pulse OSS — job semantics, delivery & data lifecycle

What actually happens when a pipeline runs, retries, is cancelled, is scheduled,
resumes, or is backed off under load — grounded in the code, with the honest
limitations called out. F-Pulse OSS is **single-node** (one process: API +
scheduler + worker pool + DuckDB); the guarantees below assume that.

## Delivery guarantee, in one line

**At-least-once, with opt-in exactly-once at the sink.** A run (or a scheduled
fire) can execute more than once across a crash; a **sink** only avoids
duplicate side effects when you configure an idempotency key. Plan for
re-delivery unless you've turned on dedup.

## Scheduling (`scheduling/scheduler.py`)

- The scheduler polls every **30s** (`check_interval_seconds=30`), running under
  the `ExecutionManager` (which counts it and can defer it under load).
- Due-ness is by schedule type — `interval`, `daily`, `weekly`, `cron`, `event`
  (event schedules are triggered externally, never by the poller).
- **Overlap is prevented per schedule** by an in-process run-lock
  (`_running_jobs`): if a schedule's previous run is still going, the tick
  **skips** it (no second concurrent run). A watchdog releases a lock only once
  the run thread is gone or has blown past a hard cap (5-min grace →
  liveness-checked → 6-hour backstop), recording a `cancelled`/`error` execution
  so the miss is visible.
- **Missed runs are NOT backfilled.** `daily`/`weekly`/`cron` fire only inside
  their wall-clock window and only if they haven't already run that day — if the
  app is down during the window, that occurrence is **skipped**, not replayed on
  restart. `interval` schedules fire **once** when overdue (they don't fire N
  times to "catch up"). For a genuine gap fill, use a **backfill** (below).
- A `daily`/`weekly`/`cron` schedule is evaluated in its own **timezone**;
  `interval` is pure elapsed UTC time.
- Scheduled runs target **PROD**; a workflow paused in PROD
  (`is_active_prod=False`) is skipped.
- **Overdue alert (opt-in):** an interval schedule ≥2× its interval overdue
  fires a one-shot schedule-miss notification if a notifier is wired.
- **Replicas double-fire.** The run-lock is in-process only — running two
  instances against one database would run every schedule on each. This is why
  OSS is single-node; do not scale it horizontally for HA.

## Run execution, checkpointing & resume (`engine/executor.py`)

- The executor runs steps in dependency order and **checkpoints each step**
  (`checkpoint_store` marks it `success` / `skipped` / `failed`).
- **Resume:** `execute_workflow(resume_from_run_id=…)` (and the convenience
  `execute_workflow_resume(run_id)`) loads the prior run's successful step ids
  and **skips them**, so a re-run continues from the **first non-successful
  step** rather than from the top. This is the "resume from where it failed"
  behavior — including a backfill that fails on window 59 resuming at 59.
- **Cancellation** is cooperative: a per-run `CancellationToken` is checked
  before each step, so a cancelled run stops at the next step boundary (an
  in-flight step's own I/O is not force-killed).

## Retry (`nodes/retry_handler.py`)

- Retry is **opt-in and visible**: drop a **Retry** node downstream of the step
  you want protected. The executor's `_find_retry_targets()` wraps that upstream
  step with the node's policy — `max_retries` (default 3), `delay_seconds`
  (default 2), `backoff_multiplier` (default 2.0), and `on_exhausted`
  (`fail` → stop the pipeline, `skip` → continue empty, `last_good` → last cached
  success).
- **There is no automatic whole-run retry.** A step with no Retry node fails the
  run at that step (resume then continues from it on the next run).

## Idempotency at the sink (`sinks/idempotency_helper.py`, `sinks/dedupe_store.py`)

- Each external sink can be given a **key template** (`{col}` substitution). The
  helper renders it, SHA-256s it, and asks the dedupe store whether that
  pipeline+sink+hash was already seen within a TTL — if so, the row's side effect
  is **skipped**.
- **Safety-first default:** if no key is configured, or the dedupe store is
  unavailable or errors, the sink **fires anyway**. So the default is
  at-least-once (possible duplicates on re-run); configure a key to get
  effective exactly-once within the TTL window.

## Backfill (`backfills/`)

- A backfill splits a range into **windows**; each window is an independent run
  binding `${param.window_start}` / `${param.window_end}` (names configurable).
  Sources must reference those params — a **preflight** (`backfills/preflight.py`)
  rejects a backfill whose sources ignore the cursor, so you can't silently
  re-load the same data every window.
- Windows run with bounded **concurrency** (a `ThreadPoolExecutor`), and the
  parent/child window rows (`backfills/store.py`) make a backfill **resumable
  from the failed window**. Combined with sink idempotency keys, a re-run is
  safe.

## Resource limits & backpressure (`engine/worker_pool.py`, `engine/global_governor.py`)

- The **worker pool** bounds concurrent runs (`max_workers`, default 8 or
  auto-sized) and assigns each run a **DuckDB `memory_limit`** by priority
  (default 512 MB; higher-priority runs get more). DuckDB **spills to disk**
  above its limit rather than OOMing.
- The **global governor** samples host memory + CPU (via `psutil`) and admits or
  defers every spawn on GREEN / 70 / 80 / 90 tiers; under pressure it degrades
  (lower DuckDB `memory_limit`, slow the scheduler poll). **If `psutil` is not
  installed the governor is GREEN unconditionally — no admission control.**
- At the container level, Compose caps memory (default 4 GB) and CPU; the
  in-process DuckDB limit spills against that ceiling.

## Data lifecycle & retention

| Data | Lifecycle |
|---|---|
| **Backup snapshots** | Pruned to `BACKUP_RETENTION_COUNT` (`storage/backup.py`). |
| **Step-output samples** | TTL-pruned (`step_output_store.prune_samples`, `SAMPLE_TTL_DAYS`). |
| **Managed tables / stored files** | **Soft-delete → trash** (`deleted_at`), then a time-based purge past a cutoff (`api/storage.py`, `datastore/store.py`). |
| **Workspaces / projects / generic rows** | **Hard delete** (`DELETE FROM …`), immediate. |
| **Execution / run history** | ⚠️ **No automatic retention** — run records accumulate unbounded. Operators manage DB size manually; F-Pulse+ adds a Parquet archive to offload old executions. |
| **Master key / `.env`** | Never touched by delete/retention; back them up separately (see [`product_facts/13_backup_recovery.md`](product_facts/13_backup_recovery.md)). |

**Known gap:** unbounded run history is the main lifecycle sharp edge on a
long-lived OSS install. Until an OSS retention policy ships, prune old executions
out-of-band or budget the SQLite growth.

## What this means for you

- **Design sinks to tolerate re-delivery**, or set an idempotency key.
- **Don't rely on a scheduler catch-up** — if a window matters, back it up
  explicitly; the poller won't replay a missed daily/cron slot.
- **Protect flaky steps with a Retry node**; nothing retries automatically.
- **A killed process is recoverable**: resume skips completed steps. But finish
  with a real backup discipline — run history and the DB are not self-trimming.

*Grounded in the code as of this writing. If a behavior here disagrees with the
code, the code wins — file an issue so this doc is corrected.*
