# Executor maturity — 1.2 design

## What the reviewer asked for

> "Mature tools have: durable run state, retries, cancellation,
> concurrency limits, work pools/queues, resumability, schedules,
> run logs, artifact storage, failure classification. F-Pulse has
> pieces, but it needs a cleaner execution contract."

## What actually ships today

| Capability | Status | Where |
|---|---|---|
| **Executor + execution_manager** | ✓ | `backend/fpulse/engine/executor.py`, `execution_manager.py` |
| **Worker pool** | ✓ | `backend/fpulse/engine/worker_pool.py` |
| **Realtime execution events** | ✓ | `backend/fpulse/engine/realtime.py` |
| **Checkpoint store** (resume from last successful step) | ✓ | `backend/fpulse/engine/checkpoint_store.py` |
| **Schedules** | ✓ | `backend/fpulse/api/schedules.py` + `backend/fpulse/scheduling/models.py` |
| **Run logs + artifact storage** | ✓ | `step_output_store.py` + monitoring tables |
| **Retry — visual node** (`RetryHandlerNode`) | ✓ | `backend/fpulse/nodes/retry_handler.py` |
| **Failure classification** | partial | `backend/fpulse/intelligence/execution_intel.py` exists; categories not standardized |
| **Programmatic retry policy** (per-pipeline default, not per-node) | ✗ | Today retry is opt-in via a canvas node |
| **Cancellation semantics** | partial | execution_manager exposes a stop, but propagation through long-running queries isn't guaranteed |
| **Work pool / queue management** | partial | `worker_pool.py` exists; the Plus split-container worker is a separate `cmd_worker` CLI |
| **Failure classification taxonomy** (transient / fatal / dependency / data-quality) | ✗ | No structured taxonomy surfaced to the operator |
| **Resumability of partially-failed runs** | partial | Checkpoint store has the data; UI affordance for "resume from step N" is missing |

So the real 1.2 work is **four targeted items**, not a "build a real
executor" rewrite.

## The four targeted gaps

### Gap 1 — Per-pipeline retry policy (not just per-node)

Today: `RetryHandlerNode` is a canvas node operators drag onto the
graph. Powerful but heavyweight — you can't say "retry the entire
pipeline 3 times" without restructuring the canvas.

Proposed:

```python
class RetryPolicy(BaseModel):
    enabled: bool = False
    max_attempts: int = 3
    initial_backoff_seconds: float = 2.0
    backoff_multiplier: float = 2.0
    backoff_max_seconds: float = 60.0
    retry_on: list[str] = Field(default_factory=lambda: ["transient", "dependency"])
    # ^ matches FailureClass enum values; "fatal" never retries
```

- Set per-workflow in workflow IR
- Optionally overridden per-execution (via API: `POST /runs` with
  `retry_policy: {...}` in body)
- Executor wraps each whole-pipeline attempt; on failure it
  consults `RetryPolicy` + the `FailureClass` (gap 4 below) before
  scheduling the next attempt
- Visible in run history: "attempt 2 of 3, scheduled for 14:23:08"

### Gap 2 — Cancellation that actually propagates

Today: stopping a run signals the executor, which sets a flag. Long-
running queries (a 30-minute Snowflake SELECT) ignore the flag because
they're blocked on the DB cursor.

Proposed:

- Every connector adapter (postgres / sqlserver / snowflake / bigquery)
  exposes a `cancel()` that calls the driver's native cancel
  (`pyodbc.Connection.close()`, `psycopg2.connection.cancel()`,
  Snowflake's `query_cancel`)
- Executor's cancellation flag is checked at every step boundary AND
  the active connector's `cancel()` is invoked
- A new `RunStatus.CANCELLED` distinct from `FAILED`

Test pin: spawn a Snowflake `SELECT ... pg_sleep(60)`, send cancel
after 2s, assert run terminates within 5s.

### Gap 3 — Failure classification taxonomy

Today: a failed run has `error_message` (free text). The retry policy
(gap 1) can't decide whether to retry without knowing if the failure
was transient or fatal.

Proposed enum:

```python
class FailureClass(str, Enum):
    TRANSIENT      = "transient"        # network blip, 5xx, lock timeout
    DEPENDENCY     = "dependency"       # connector unreachable, credential expired
    DATA_QUALITY   = "data_quality"     # null in non-null column, schema mismatch
    USER_INPUT     = "user_input"       # invalid pipeline config
    FATAL          = "fatal"            # OOM, disk full, code bug
    UNKNOWN        = "unknown"
```

Each connector + node classifies its own errors via a `_classify(e)
-> FailureClass` helper. The executor stores the classification with
each failed run in `monitoring.executions.failure_class`.

Retry policy (gap 1) reads this — `retry_on: ["transient",
"dependency"]` means a `data_quality` failure doesn't retry (because
retrying won't change the data).

This composes with Steward's connector-health classifier
(`steward/connector_health.py:classify_error`) — same shape but the
node-level version is finer-grained.

### Gap 4 — Resume-from-step UI affordance

The data is already there: `checkpoint_store.py` records the last
successful step per run. The UI just doesn't surface "Resume from
step 7" on a failed run.

Proposed: failed-run detail page gets a `Resume from last checkpoint`
button. Posts `POST /runs/{id}/resume` which:
1. Looks up the checkpoint
2. Spawns a new run with `parent_run_id` + `resume_from_step_id`
3. Loads the cached output of all steps up to that point
4. Resumes execution from `resume_from_step_id + 1`

Pinned by a test that:
1. Runs a 3-step pipeline; step 2 fails
2. Confirms checkpoint exists for step 1
3. POSTs `/resume`
4. Confirms the new run skips step 1 (uses cached output) and
   starts executing step 2

## Proposed phased milestones

| Milestone | Scope | Effort |
|---|---|---|
| **E1** | Gap 3: `FailureClass` enum + per-connector `_classify` helpers + storage column + monitoring rollup ("78% of failures last week were transient") | 3-4 days |
| **E2** | Gap 1: `RetryPolicy` model + per-workflow config + executor wrapper that reads `FailureClass` and decides | 4-5 days |
| **E3** | Gap 2: Native cancel propagation in pg / mssql / snowflake / bigquery adapters + `RunStatus.CANCELLED` + test pin | 3-4 days |
| **E4** | Gap 4: Resume-from-checkpoint UI button + API + test | 2-3 days |

**Total ~2.5 weeks for OSS 1.2.** Each milestone ships independently
— a customer can use Gap 3 (failure classification) before Gap 2
(cancellation) lands.

## Open questions for human review

1. **Retry policy precedence** — if both `RetryHandlerNode` (canvas)
   AND `RetryPolicy` (workflow-level) are set, which wins? Proposal:
   canvas node overrides workflow-level — explicit beats implicit.
2. **Cancellation grace period** — how long do we wait for a graceful
   cancel before force-killing the worker? Proposal: 30s configurable
   via `FPULSE_CANCEL_GRACE_SECONDS`, default 30.
3. **Failure-class auto-classification accuracy** — how do we tune the
   regex/error-class mappers without a labelled dataset? Proposal: ship
   a conservative default; users can `POST /runs/{id}/reclassify
   {failure_class: "..."}` to correct, and we learn from the manual
   corrections.
4. **Resume vs retry distinction** — "resume from checkpoint" is
   subtly different from "retry the whole pipeline." Should they
   be separate UI buttons, or one with a toggle? Recommend separate
   — they answer different operator questions.

## What this design explicitly does NOT do

- **Replace the executor.** The current `executor.py` works; the four
  gaps are bolt-ons that respect the existing contract.
- **Add a job queue (Celery / Sidekiq style).** Worker pool exists for
  this. A queue is a Plus / multi-machine concern.
- **Ship distributed execution.** OSS stays single-machine. The
  retry / cancel / resume work all assumes one host.

## Decision log

| Considered | Rejected because |
|---|---|
| Build retry into every node (override every connector to support `RetryPolicy`) | Too invasive; the wrapping pattern at executor level is cheaper and keeps connectors simple |
| Skip failure classification, just retry every error N times | Wastes time on `data_quality` failures that won't change between attempts; risks masking real bugs |
| Use OS-level signal handling for cancel (SIGTERM the worker) | Database drivers ignore SIGTERM mid-query; need to actually call the driver's cancel API |
| Make resume the default behaviour of retry | Conflates two operations: retry means "do it again from the top", resume means "pick up where we crashed." Different operator intents. |
