# Analytical questions about pipelines, data, schema

This file teaches the Copilot how to **think about** analytical
questions and which **F-Pulse-specific tools / surfaces** to use.
Every Q&A includes the right answer path so the agent doesn't have to
guess.

**Convention:** for every analytical question, name the precise data
source (tool, page, table, env var, log line) the agent should consult
before formulating a response. Generic "you might want to check…"
answers are an anti-pattern.

---

## About a single pipeline

### What does this pipeline do?

Use the `summarize_pipeline` tool with the pipeline ID. It returns a
plain-English summary of the IR (sources → transforms → sinks) plus
the schedule + alerts. For deeper "why is this designed this way" the
LLM may also call `recall_history` to surface past commit messages and
any failures that informed the design.

### What's the last run status of pipeline X?

Call `list_executions` with `workflow_id=X, limit=1`. The first row
has `status` (success / error / running / cancelled / queued),
`duration_ms`, `started_at`, `completed_at`, `total_rows_processed`,
`peak_memory_mb`, `cpu_seconds`, `error_message` (if error).

### How long does pipeline X typically take?

Call `list_executions` with `workflow_id=X, limit=20, status=success`.
Aggregate `duration_ms` (median, p90, max). If recent runs are
significantly different from older ones, flag the inflection point —
that's usually a config or upstream-data change.

### Why did pipeline X fail?

1. Call `list_executions` with `workflow_id=X, status=error, limit=1`.
2. Read the row's `error_message`, `failed_step`, `peak_memory_mb`,
   `exit_reason` (`ok` / `budget_memory` / `budget_runtime` /
   `cancelled` / `killed_throttle` / `error`).
3. If `exit_reason=budget_memory`: out-of-memory; recommend bulk-load
   for the sink + check `FPULSE_DUCKDB_MEMORY_LIMIT`.
4. If `exit_reason=budget_runtime`: timeout; recommend Resume-from-step
   or splitting the pipeline.
5. If `error_message` mentions a specific connector error: call
   `recall_history` to see if other pipelines hit the same error
   recently — often it's a credential rotation / API rate limit /
   schema change in the upstream.
6. Surface the `failed_step` name + the concrete `error_message` first
   sentence; offer to open the Executions page → run row for full logs.

### How much memory does pipeline X use?

Aggregate `peak_memory_mb` from `list_executions`. Report the median
+ max + the run that spiked (if there's an outlier 2× the median,
worth a flag — usually a hash spill on a wide JOIN or unexpected
high-cardinality column).

### How much CPU does pipeline X use?

Same shape — `cpu_seconds` from `list_executions`. CPU seconds ≠
wall-clock duration; if `cpu_seconds >> duration_ms`, the pipeline is
parallelizing across DuckDB threads (good). If `cpu_seconds << duration_ms`,
the pipeline is I/O-bound (waiting on upstream DB or network).

### What changed in pipeline X recently?

`recall_history` finds historical commits + the last few executions.
For deeper diff: open the pipeline → **Versions** tab → click two
versions to see the IR diff (added / removed steps, connection changes).
Each version has a `content_hash` (SHA-256) so tampering is detectable.

### How many rows does pipeline X process?

`total_rows_processed` on the execution row. Track over time via
`list_executions` to see growth trends.

### When did pipeline X last succeed?

`list_executions(workflow_id=X, status=success, limit=1)` — read
`completed_at`. If it's been more than 1× the schedule cadence,
something's wrong; check `failed_step` on the runs since.

---

## About the workspace as a whole

### What's the state of my workspace?

Use `workspace_overview` for top-level counts (pipelines, projects,
schedules, alerts, connections). Combine with `list_executions(limit=20)`
for "recent activity". Format the response as a KPI strip card
(see `10_ai_copilot.md`).

### How healthy is my workspace?

Compute three indicators:
1. **Failure rate**: count of `status=error` runs in last 7 days /
   total runs. <5% is healthy; >20% is concerning.
2. **Long-running runs**: count of runs exceeding their pipeline's
   `long_running_threshold_min` (default 30 min). Flag the top 3
   offenders.
3. **Schedule misses**: count of `ON_SCHEDULE_MISS` alert events in
   last 7 days. Should be 0.

Report all three with the actual numbers.

### What's the most expensive pipeline (memory / CPU / runtime)?

Aggregate `list_executions` over the last 7 days, group by
`workflow_id`, sort by max `peak_memory_mb` (or `cpu_seconds` or
`duration_ms`). Surface the top 3 with the metric value.

### Which pipeline failed most recently?

`list_executions(status=error, limit=1)` returns the most recent
failure across the whole workspace. Read `workflow_name` +
`error_message` + `started_at`.

---

## About data flowing through

### What does the source data look like?

Drop a **Data Profile** node downstream of the source. It emits one
row per column with: `column`, `data_type`, `row_count`, `null_count`,
`null_pct`, `distinct_count`, `distinct_pct`, `min_value`, `max_value`,
`top_value`, `top_value_count`. Run the pipeline in Sample mode for
fast feedback.

### What columns are in this source?

After running once: open the source node in the editor → **Output
preview** tab → top of the table shows column headers + types. Or use
Data Profile (above) for the same info plus stats.

### Is column X null-heavy?

Add Data Profile downstream. The output row for column X has
`null_count` + `null_pct`. >20% null typically means the column is
optional in the source schema (or a join is producing nulls upstream).

### Why is column X high-cardinality?

Data Profile shows `distinct_count` + `distinct_pct`. If
`distinct_pct > 90%`, the column is probably a primary key or a hash
— that's expected high cardinality. If it's a categorical column
(country, status, etc.) showing high distinct count, the source data
may be inconsistent (case differences, whitespace, etc.).

Suggest a downstream **Transform** with `LOWER(TRIM(col))` to normalize.

### What's the schema of pipeline X's output?

Run the pipeline in Sample mode. The final node's **Output preview**
tab shows the schema + first 50 rows. Or save the output to a
**Materialize** node for a temp DuckDB table you can query directly.

### What's in this CSV / file?

Drop a CSV Source node with the file path → **Run** in Sample mode →
**Output preview** tab. For the schema only (no data): use Data
Profile downstream.

### How big is the data?

- **Row count** — `total_rows_processed` on the execution row.
- **Bytes** — F-Pulse doesn't track output bytes directly; for files
  it's whatever the OS reports on the destination. For warehouse
  sinks, query the warehouse.
- **Memory at peak** — `peak_memory_mb` per run.

---

## About schemas + types

### What are F-Pulse's data types?

DuckDB's type system: `INTEGER`, `BIGINT`, `DOUBLE`, `BOOLEAN`,
`VARCHAR`, `DATE`, `TIMESTAMP`, `BLOB`, `LIST<T>`, `STRUCT<...>`,
`MAP<K,V>`, `JSON`. The Schema Mapper node coerces between them with
explicit conversion rules.

### What happens when source schema changes?

The **Warehouse Sink** node has `auto_evolve` (default on) — it adds
new columns automatically (typed VARCHAR by default) and ALTER TABLE
on append. For other sinks: a schema-mismatch fails the run with a
clear error message naming the offending column.

To proactively detect drift, use the **Data Quality** node with
schema-shape rules (column exists, column is not null, column type
matches expected).

### How do I track schema changes over time?

**F-Pulse+ only**: drift detection feature scans schema on a schedule
and fires an alert when columns appear/disappear or types change.

OSS Free: Data Profile output captures the schema + types as a
snapshot. Store the output rows in a dedicated table; diff between
runs to detect changes.

### What's a primary key in F-Pulse?

The **F0.1 manifest** declares `primary_key: ["customer_id"]` for
each stream. The Bulk Loader uses this for idempotent merge mode
(`INSERT … ON CONFLICT DO UPDATE`). The SCD Type 2 node uses this as
the `business_key` for tracking historical versions.

### What's an incremental column?

The F0.1 manifest declares `incremental_field` (typically
`updated_at` or a monotonic ID). The connector resumes from the last
seen value across runs — only new/changed rows are pulled. Required
for production-grade (depth-5) connectors.

---

## About connectors

### Which connectors are production-grade?

Hit `GET /api/connectors/cert-matrix`. Filter rows where
`depth_score == 5`. The 18 OSS production-grade connectors as of 1.0
include the major SaaS sources (HubSpot, Stripe, Shopify, etc.) — the
exact list is what the live cert matrix returns; don't guess.

### Why is connector X only depth-3?

Depth scores break down by capability:
- 0: stub
- 1: schema declared
- 2: + pagination handled
- 3: + incremental sync wired
- 4: + primary key declared
- 5: + full fixture coverage (5 fixture types)

Hit `GET /api/connectors/cert-matrix/{connector_id}` for the
per-stream detail — it shows exactly which capability is missing.

### How do I make connector X production-grade?

1. Run `python -m fpulse.connectors.certify <connector_id>`.
2. The validator reports specific missing pieces (e.g. "no
   incremental_field on stream X").
3. Edit the manifest under `backend/fpulse/connectors/manifests/<X>.v2.json`.
4. Re-run certify until depth-5.
5. Add fixture files under `manifests/<X>/fixtures/` for the 5
   required fixture types.

---

## About scheduling + timing

### When does pipeline X next run?

The agent has a `get_next_scheduled` tool. Call it with a workflow ID
or no argument (returns the next N upcoming fires across the workspace).

### Why didn't my schedule fire?

1. Schedule disabled? Schedules page → row → enabled toggle.
2. Pipeline status = `archived` or `draft`? Only `published` /
   `testing` pipelines fire on schedule.
3. Watchdog working? Check backend logs for `_timeout_watchdog_loop`.
4. Workspace_id mismatch between the schedule and the pipeline? Rare
   but possible after a workspace migration.

If the schedule should have fired but didn't, an `ON_SCHEDULE_MISS`
alert (if configured) would have surfaced — check the Notifications
page.

### How often is the scheduler checking?

Every **30 seconds** by default. The interval is hardcoded for OSS;
Plus exposes a knob.

---

## About AI / Copilot internals

### Why is the chat slow?

Most likely you're on local Ollama with a CPU-only host. Each tool-using
turn is 30–60 s on `qwen2.5:7b` (the recommended floor as of 2026-05-19).
For instant answers (no LLM), use the fast-lane phrasings: `list pipelines`,
`overview`, `failures today`, `running now`, `what's my role`.

For analytical questions ("why did X fail", "compare A and B") the LLM
is needed; expect 1–5 minutes end-to-end on CPU across multiple iterations.
Use the **Stop button** to cancel mid-flight.

### Why did the agent refuse to do X?

Three possible reasons:
1. **RBAC**: the role × env × tier intersection didn't permit the tool.
   Check `what's my role` in the chat.
2. **Policy block**: a governance rule fired (e.g. PROD writes need
   approval). The trace step's `policy_rules_fired` list names the
   exact rule.
3. **Wallet cap**: per-user or per-workspace daily token cap reached.
   Check `GET /api/ai/agent/budget`.

### What does the agent know about my workspace?

Three layers:
1. **Layer 1 — session context** (always-on): user, role, environment,
   edition, workspace counts, page state, allowed tool tiers, can_approve,
   can_deploy_prod.
2. **Layer 2 — product knowledge RAG**: 17+ curated `docs/product_facts/`
   files retrieved per turn based on the user's query.
3. **Layer 3 — tools**: 25 tools that fetch live workspace state when
   the LLM picks them.

The agent does NOT know the contents of pipeline rows, individual
credential values, or anything outside the workspace it's in.

---

## About performance + scale

### Why is pipeline X slow on a large source?

Several common causes:
1. **Row-by-row INSERT** sink. Replace `Database Sink` with **Bulk
   Loader** (`COPY FROM STDIN` for Postgres, `PUT + COPY INTO` for
   Snowflake) — 10-100× speedup at scale.
2. **Hash spill on JOIN**. Check the spill directory's SSD/HDD badge
   in Settings → General → Execution Tuning. HDD spill is 10-100×
   slower than SSD.
3. **Too-strict memory limit**. If `peak_memory_mb` ≈ DuckDB memory
   limit on every run, raise `FPULSE_DUCKDB_MEMORY_LIMIT`.
4. **Parallelism**: DuckDB defaults to detected CPU cores. If
   `cpu_seconds << duration_ms`, the pipeline is I/O-bound — adding
   threads won't help.

### Why is my Copilot using 100% CPU?

Local Ollama running inference. `qwen2.5:7b` (the tool-use floor)
saturates all available cores during generation. Consider:
- Cloud provider opt-in if the host can't spare the cycles — sub-second
  responses, but prompts leave the host.
- A smaller model like `qwen2.5:1.5b` is sometimes proposed for trivial
  text classification, but it sits BELOW the tool-use floor — it can't
  drive the agent loop. Use it only outside the Copilot path.
- Disable AI entirely with `FPULSE_DISABLE_OLLAMA_AUTOPROBE=1`.

### How much disk does F-Pulse need?

- **F-Pulse install**: ~500 MB Docker image.
- **Ollama runtime**: ~150 MB.
- **`qwen2.5:7b` model**: 4.7 GB (the recommended local tool-use floor).
- **SQLite database**: typically <100 MB even at hundreds of pipelines.
- **Pipeline checkpoint Parquets** (7-day TTL): scales with
  workspace size. Estimate 10-100 MB per active pipeline.
- **RAG vector store**: ~10 MB at the OSS scale (workspace + docs).

Recommend at least **5 GB free disk** for a comfortable install,
**10 GB+** if you're keeping a large execution history.

---

## About audit + compliance

### Who edited pipeline X?

Audit log query: Insights → Activity → filter `entity_id=<pipeline_id>`,
`action=*pipeline*`. Each row has `user_id`, `timestamp`, `action`,
`source_ip`, `status`.

### How long are audit logs kept?

**OSS Free**: best-effort, no enforced retention. Operators rotate
manually if disk pressure hits.

**F-Pulse+**: configurable retention policy with sigstore-signed
archive to Parquet on S3/GCS before deletion.

### Where do I export audit logs for SIEM ingestion?

**F-Pulse+ only**: `POST /api/plus/audit/export` returns a
sigstore-signed tarball. SIEM-compatible formats: JSON Lines, CSV.

OSS Free has no first-class export; operators query the SQLite
`audit_log` table directly.

### Is my deployment SOC 2 compliant?

The F-Pulse software itself is not separately certified. **Your
deployment** may bring F-Pulse into the scope of your own SOC 2
attestation by configuring the controls F-Pulse provides (encryption
at rest, audit log, RBAC in Plus, no outbound traffic by default).

The compliance one-pager at `docs/COMPLIANCE.md` lists every claim
with a verifiable artifact link.

F-Pulse+ customers get a Hybridyn Data Labs SOC 2 Type II attestation report on
request as supporting evidence.
