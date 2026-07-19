# Dashboard — what you see, why, and what every number means

The Dashboard is the operator's home page. It answers three questions, in this order:

1. **Is anything broken right now?** → Welcome banner headline + Needs Attention block
2. **What's running and what just ran?** → Activity row + Run Volume sparkline + Recent Runs
3. **What do I have in this workspace?** → Workspace · Storage · System strips + Composition donut

The page never quizzes you with a wall of numbers; it lifts trouble to the top, shows current activity in the middle, and keeps inventory visible-but-compact below. Every metric on the strips is **clickable** — opens its native page (e.g. clicking `Pipelines 4` → Pipelines page; `Memory 191 MB` → Pool page) so the dashboard is a launchpad, not a dead end.

**DEV vs PROD layout difference** (in one sentence): PROD adds an Administration row and keeps the System metrics in a dedicated section because operators want CPU / memory always visible; DEV folds System into the inline strip alongside Workspace and Storage.

The math here is the **OSS Free** definition; F-Pulse+ adds the ability to override these formulas per workspace.

> **If a number ever surprises you, check this page first.** Most "discrepancies" turn out to be a different aggregation than the user expected (unique-pipelines vs raw-runs is the classic one).

## Welcome banner — the smart headline

The single sentence under your name adapts to what's going on:

| State | Headline | Tone dot |
|---|---|---|
| `failures > 0` (DEV) | "`N` failure(s) in the last 24h" | red |
| `running > 0` (DEV)  | "`N` pipeline(s) running now" | amber |
| `no runs at all`     | "Workspace is quiet — nothing running" | grey |
| healthy              | "All systems healthy" | green, pulsing |
| `incidents > 0` (PROD admin) | "`N` incident(s) need attention" | red |
| `approvalsPending > 0` (PROD admin) | "`N` approval(s) awaiting review" | amber |
| PROD healthy         | "Production is healthy" | green, pulsing |

The button to the right of the headline (the **hero CTA**) also adapts: admins on PROD with pending approvals see "Review N Approvals"; DEV defaults to "New Pipeline" (or "Team Activity" for admins). One click per dashboard load is the target.

## Needs Attention — the alert summary

Auto-shows **only when there is something actionable**:

- **Currently-failing pipelines** — a pipeline appears only when its **most recent run failed** (current-state, not history). One that failed and then ran clean drops off on the next refresh. Top 3 shown; the tail rolls into "+ N more failures — see all on Executions →". This is deliberately distinct from the **Failures (24h)** KPI above, which counts failures in a time window regardless of current state.
- Worker pool at ≥ 85% capacity (red — new runs will queue)
- Pending PROD deploy approvals (admin only)

If nothing's broken, the section doesn't render at all — the dashboard stays calm. When it does render it's **collapsed by default**: the header bar shows the count summary ("6 failures · Pool 92% · 2 approvals") so an operator can glance and assess severity without expanding. Click the header to open.

State is sticky per browser via `localStorage.fpulse_dashboard_attention_open`.

**Each failed-pipeline row** carries:

- the **pipeline name** — click to open the *actual failed run* (`#executions/<run_id>`: the error, the failed step, and the diagnosis), not a filtered list;
- **when it last failed** — relative ("2h ago"), with the exact timestamp on hover; a `×N` badge marks a consecutive-failure streak;
- a **View →** deep-link to the same run; and
- a **Clear (×)** action — *acknowledge and hide* that failure. It **reappears** if the pipeline fails again (a new run) and clears for good once it runs clean. A **Clear all** footer dismisses every shown failure at once.

Clear is an acknowledgement, not a delete — nothing in run history is removed. Acknowledgements are **per-browser** (`localStorage.fpulse.dashboard.dismissedFailures`), self-prune once they're no longer current, and the **welcome-banner headline reconciles with the card** — clearing the last failure flips the banner back to healthy in the same render.

## Empty state — new workspace

When `pipelines === 0`, the hero KPI row is replaced with a 3-card onboarding block ("Build a pipeline / Upload data / Connect a source"). Skips the misleading "—" / "—" Activity tiles a fresh workspace would otherwise show; gives the user the right next step instead of a blank canvas.

## Activity (last 24h)

These cards window on **executions whose `started_at` falls inside
the last 24 hours**, scoped to the caller's workspace. The 24h window
is rolling, not calendar-day.

### Success rate (24h)
- **Formula**: `success / (success + failed)` × 100 — running pipelines and queued ones are excluded from the denominator so the rate doesn't flicker while a long run is in flight.
- **Why no division-by-zero**: shown as `—` when there are no completed runs in the window.
- **Backend source**: `GET /api/monitor/stats?hours=24`.

### Runs (24h)
- **Formula**: total executions in the 24h window. Includes success, failed, running, queued, and cancelled.
- **Backend source**: same endpoint, `total` field.

### Failures (24h)
- **Formula on the value**: count of executions with `status = "error"` in the 24h window. **Each failed run counts once** — not deduped.
- **Formula on the trend label** ("`N` pipelines affected"): count of **unique pipelines** that have at least one failed run in the window. **De-duplicated by `workflow_id`.** A pipeline that fails 6 times appears as `1 pipeline affected`, not `6`.
- **Was a real bug**: before 2026-05-09 the trend label counted raw failed-run rows, so 6 failures of 1 pipeline read as "6 pipelines affected". The count is now per-pipeline.
- **Backend source**: the value comes from `/api/monitor/stats`. The "pipelines affected" trend count comes from the **dashboard summary's** windowed `top_failed` list (it follows the 24h / 7d / 30d toggle), falling back to the current-state `/api/monitor/failed` list when the summary isn't available.
- **Note (2026-06-17):** `/api/monitor/failed` is now *current-state* — pipelines whose **latest** run failed — so it's the live-health source for **Needs Attention**. The "pipelines affected" count here is intentionally different: it counts within the selected time window, so a pipeline that failed and recovered still contributes to this 24h count while correctly dropping off Needs Attention.

### Avg duration (24h)
- **Formula**: arithmetic mean of `duration_ms` across **only successful** runs in the window. Failed runs are excluded so a single timeout-killed run doesn't blow the average.
- **Display**: rendered with the smallest sensible unit (`ms`, `s`, or `m`).
- **Backend source**: `/api/monitor/stats`, `avg_duration_ms`.

## Workspace strip

A single compact row, identified by a **blue left rail**: `Pipelines · Projects · Connections · Credentials · Schedules · Variables`. These count entities, not runs. The data is point-in-time (snapshot at the moment the dashboard loaded — Refresh to update). Every metric is clickable → opens its native page.

The strip replaced a 6-card grid in the 2026-05-25 redesign: same information, ~80px tall instead of ~120px, no per-tile chrome competing with the Activity status row above.

### Pipelines
- **Formula**: count of workflows owned by the workspace. Includes drafts, published, failed, and archived.
- **Backend source**: `/api/workflows/`.

### Projects
- **Formula**: count of projects in the workspace.
- **Backend source**: `/api/projects/`.

### Connections
- **Formula**: count of connection records in the workspace (Global + Project-scoped).
- **Backend source**: `/api/connections/`.

### Credentials
- **Formula**: count of encrypted credential entries in the workspace vault.
- **Backend source**: `/api/credentials/`.

### Schedules
- **Formula**: count of schedules in the workspace. Sub-label "active" filters to `enabled = true`.
- **Backend source**: `/api/schedules/`.

### Variables
- **Formula**: count of pipeline-level variable definitions across all workflows.
- **Backend source**: `/api/variables/`.

## Storage strip

A single compact row, identified by an **emerald left rail**: `Files · Tables · Outputs · Trash`. Counts and sizes from `/api/storage/summary` (NOT the Files-tab listing call — the summary excludes the system-doc-row machinery used elsewhere so the count never disagrees with what the user sees on the Storage page).

Each metric is clickable → opens the Storage page on the matching tab.

### Files
- **Value**: count of user-uploaded files in the workspace; the suffix shows total size (`Files 4 · 172 KB`).
- **Backend source**: `/api/storage/summary`, `file_count` + `file_size_bytes`.

### Tables
- **Value**: count of managed Parquet tables. Suffix shows total size.
- **Backend source**: `/api/storage/summary`, `table_count` + `table_size_bytes`.

### Outputs
- **Value**: count of pipeline-output artifacts written by past runs. Suffix shows total size.
- **Backend source**: `/api/storage/summary`, `output_count` + `output_size_bytes`.
- **Includes**: any file the pipeline wrote via a sink node + any report generated from Insights → Reports (which auto-persist into the workspace storage tree on Phase 1 of the Reports wiring).

### Trash
- **Value**: count of soft-deleted files awaiting hard-delete. Renders **amber** when > 0 (actionable — the user can free disk on the Storage page); slate when 0.
- **Backend source**: `/api/storage/summary`, `trash_count`.

## System strip

A single compact row, identified by a **cyan left rail**: `Memory · Threads · DB size · Uptime` (plus `CPU` and `Throughput` when those have real readings — see below). Live-process telemetry from the running F-Pulse backend. Refreshes when the user clicks Refresh, not on a timer (avoids needless polling).

**Hide-when-empty rule**: CPU and Throughput are *hidden entirely* until the underlying value is non-`—`. Memory / Threads / DB size / Uptime always render — they're the system identity row even on a fresh install. This was a 2026-05-25 fix; before then, every system metric rendered as `—` on first boot and made the strip feel unfinished.

### CPU
- **Value**: process CPU% when available, otherwise hidden. The value is **alert-red at > 85%**, amber at > 60%, normal below.
- **Why not always shown**: the OSS build doesn't sample its own CPU% in the absence of a psutil background loop; Plus tier surfaces real utilisation via the Pool page.

### Memory
- **Value**: process RSS in MB. Alert-red at `> 80%` of total host memory, amber at `> 60%`.
- **Backend source**: `/api/health/memory`, `rss_mb` + `host.total_memory_mb`.

### Threads
- **Value**: number of live threads in the process. Always renders.
- **Backend source**: `/api/health/memory`, `threads`.

### Throughput
- **Value**: runs per hour over the last 24h (`runs_24h / 24`). **Hidden** when zero — the strip drops the chip entirely so the row doesn't carry a placeholder `—`.

### DB size
- **Value**: total bytes across the SQLite files in the data directory (`db_files[].size_bytes` summed). Click → Settings → Storage panel. Renders `—` (muted) when the directory is empty.
- **Note**: this is the index DB, not user-uploaded data. For uploaded-data totals see the **Storage strip** above (`Files · 172 KB`).

### Uptime
- **Value**: seconds since the process started, formatted as `2m`, `1h 23m`, `4d 3h`. Always renders.
- **Backend source**: `/api/health/memory`, `uptime_seconds`.

## Run volume chart

- **X-axis**: 24h window split into 30-minute buckets.
- **Y-axis**: count of executions started in each bucket, stacked by status (Success / Failed / Running).
- **Source**: aggregated from the same `/api/monitor/executions?hours=24` data the Executions page uses.

## Pipeline status donut

- **Slice values**: counts of pipelines by lifecycle status (Success, Running, Failed, Idle).
- **"Idle"** = no run in the last 24h, regardless of historical state.
- **Pipeline status** is independent of execution status — a pipeline marked `failed` here means its **last run** failed, not that the pipeline itself is broken.

## Failed pipelines list

The same source the Needs Attention card reads — `/api/monitor/failed`.

- **Rows**: pipelines whose **most recent run failed** (`limit=20`). One row per pipeline (its latest run). A pipeline that recovered (latest run succeeded) is **not** included — this is current-state, not "any failure in recent history".
- **`failure_count` field**: the **consecutive** failing streak counted back from the latest run to the last success — so `×6` means the last six runs in a row failed (it is *not* a 24h count). Rendered as a `×N` badge.
- **Timestamp**: each row shows when that latest run failed (relative, exact on hover).
- **2026-06-17 semantics change**: this endpoint used to return any pipeline with a failed run anywhere in the last 500 runs, deduped by `workflow_id`. It now returns current-state only, so a recovered pipeline drops off automatically.

## Recent runs list

- **Rows**: latest 6 executions across all pipelines, newest first. No deduplication.
- **Source**: `/api/monitor/executions?limit=30` (sliced to 6 client-side).

## Active schedules list

- **Rows**: schedules with `enabled = true`, sliced to top 6.
- **Source**: `/api/schedules/active`.

## Insights → Activity page

### Agent Runs

- **Formula**: count of `kind = "agent"` events in the visible window.

### Tokens (Input · Output · Total)

- **Formula**:
  - `input` = sum of `total_tokens_in` across agent events.
  - `output` = sum of `total_tokens_out` across agent events.
  - `total` = `input + output`.
- Older agent traces (pre-2026-05-19, before the schema split) carry only the combined total; the page back-fills them into `input` so the totals reconcile.

### Est. Cost — the editable rate table

- **Formula**: `Σ (tokens_in × input_per_Mtok / 1,000,000) + (tokens_out × output_per_Mtok / 1,000,000)` across every agent event in the visible window.
- **Resolution order** for each event's `(provider, model)` tuple:
  1. Exact match in `models[<model>]` (most specific, e.g. `claude-haiku-4-5`).
  2. Provider match in `providers[<provider>]` (e.g. `anthropic`).
  3. `fallback` rate.
- **Ollama is always $0.** Local inference has no per-token bill; the page shows `$0.00` with the footer "local — no per-token cost" whenever every visible run was served by Ollama.

### Rate table seed (USD per 1M tokens, as of 2026-05-19)

Each entry has three rates: **Input** (uncached), **Cached input** (prompt-cache hits), and **Output**.

| Provider | Input | Cached | Output |
|---|---|---|---|
| ollama | 0.00 | 0.00 | 0.00 |
| anthropic | 3.00 | 0.30 | 15.00 |
| openai | 2.50 | 1.25 | 10.00 |
| openrouter | 1.00 | 0.50 | 3.00 |

| Per-model override | Input | Cached | Output |
|---|---|---|---|
| claude-haiku-4-5 | 0.80 | 0.08 | 4.00 |
| claude-sonnet-4-6 | 3.00 | 0.30 | 15.00 |
| claude-opus-4-7 | 15.00 | 1.50 | 75.00 |
| gpt-4o-mini | 0.15 | 0.075 | 0.60 |
| gpt-4o | 2.50 | 1.25 | 10.00 |

Default pricing (unknown provider/model): 0.30 input · 0.15 cached · 0.60 output per 1M tokens.

**Cached-input caveat:** the OSS trace store currently lumps cached + uncached input tokens together in `total_tokens_in`. The cached rate therefore contributes **$0** to the Activity tile today — every input token is priced at the full input rate. The rate field exists for the Cost Simulator (which can model cache savings) and is forward-compatible: when LLM clients are instrumented to report `cached_tokens_in` per run, the existing rate table will apply automatically.

### Editing the rates

`Settings → AI Pricing` exposes the full table:
- Workspace-scoped, persisted at `/api/v1/ai/cost-rates` (workspace_settings table, key `ai_cost_rates`).
- A **sticky "unsaved changes" banner** appears at the bottom of the section when the draft differs from the persisted state, with **Discard** and **Save** actions.
- A **search box** filters the per-model overrides; entries are grouped by inferred provider (ANTHROPIC / OPENAI / OLLAMA / OTHER).
- Numeric inputs are validated (min 0, max 1000, step 0.01); invalid cells render red.
- Saving emits the `fpulse-settings-changed` event so any open Activity page re-fetches and the tile updates without a reload.
- **Reset to defaults** discards all overrides and re-emits the broadcast.

### Cost simulator

Below the rate editor sits a compact simulator with four inputs:
- **Model / provider** — single dropdown with provider defaults plus per-model overrides (Ollama hidden — local is always $0).
- **Requests / day**
- **Input tokens / req**
- **Output tokens / req**

Output is a single headline: **Estimated $X / month** with a `≈ $Y / day · 30-day estimate` subtitle. The math uses the **current draft rates** so unsaved edits are reflected immediately. At the seed defaults (anthropic · 200 req/day · 1300 in · 800 out) the estimate is **~$95 / month**; switching to `gpt-4o-mini` drops it to ~$5 / month.

### Failed Runs

- **Formula**: count of agent events with `severity = "error"` (outcomes `llm_failure` and `tool_failure`).

---

## Plus-tier customisation

OSS Free uses the formulas above as fixed. F-Pulse+ adds a **Metric
Editor** under Settings → Workspace → Metrics where workspace admins
can override any KPI's formula — for example, redefine "Success rate"
to weight by row count, or compute "Avg duration" against a P95
percentile instead of the mean. The override is persisted per
workspace and applied at the API layer so all clients (Dashboard,
Workflows, Executions) see the same numbers.

See the [Editions guide](editions.md) for the full Free vs Plus matrix.
