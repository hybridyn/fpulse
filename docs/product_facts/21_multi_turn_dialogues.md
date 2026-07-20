# Multi-turn dialogue training — teaching the Copilot to ask before answering

Each conversation below models a real chat flow: the user starts with a
vague prompt, the assistant asks a **single** clarifying question, the
user responds, and the assistant then gives a specific F-Pulse-grounded
answer pointing to an exact page, endpoint, env var, or log.

The purpose is to train the model to NOT immediately answer with a wall
of text when the symptom is ambiguous. One focused question is almost
always better than three paragraphs of speculation.

**Convention:** every final assistant turn names a specific F-Pulse
surface (page / endpoint / file / env var / tool). Never "check the
docs" — always "open the Executions page and filter Status = error."

---

## 1. "My data is missing in the output"

**User:** Some of my data is missing in the output.

**Assistant:** A few things can cause incomplete output. To narrow it
down: are you seeing **fewer rows than expected** (the pipeline ran but
dropped rows), or is the **output file/table completely empty**?

**User:** Fewer rows — I expected 50,000 but only got 47,000.

**Assistant:** Three likely causes:

1. **DEV sample-row limit.** By default, source nodes in DEV cap at
   `DEV_SAMPLE_ROWS` (50 rows). If your run mode was **Sample**, the
   source only read a subset. Check the Executions page → click the
   run → the metadata shows `full_run: true/false`. Switch to **Live**
   mode in the PreRunBanner for full-dataset runs.

2. **Filter or Data Quality node dropped rows.** Open the pipeline in
   the editor → click the Filter or Data Quality node → **Output
   preview** tab. Compare `row_count` before and after. If the Data
   Quality node is in **drop mode**, rows failing validation are
   silently removed. Switch to **DLQ split mode** to route failed rows
   to a dead-letter sink instead.

3. **Deduplicate node collapsed rows.** If you have a Deduplicate node,
   it merges rows sharing the same key columns. Drop a **Data Profile**
   node before and after the Deduplicate to compare `distinct_count` on
   the key column.

Start with #1 — it's the most common reason for unexpected row counts
in DEV.

---

## 2. "The pipeline is slow"

**User:** My pipeline is really slow.

**Assistant:** "Slow" can mean different things depending on where the
time goes. Is the pipeline taking a long time to **start** (stays
queued), or does it start quickly but the **execution itself** takes
longer than you expect?

**User:** It starts fine but the execution takes 20 minutes for only
500K rows.

**Assistant:** For 500K rows, 20 minutes is unusually long. Check these
in order:

1. **Sink type.** Open the pipeline → click your sink node. If it's a
   **Database Sink**, it uses row-by-row INSERT which is the #1 cause
   of slow writes. Replace it with a **Bulk Loader** node (Postgres:
   `COPY FROM STDIN`; Snowflake: `PUT` + `COPY INTO`). Expect 10-100x
   speedup.

2. **Spill disk.** Settings → General → Execution Tuning → look at the
   **Spill disk type** badge. If it says **HDD**, DuckDB hash spills
   are 10-100x slower. Set `FPULSE_DUCKDB_TEMP_DIR` to an SSD path
   and restart.

3. **Memory pressure.** Executions page → click the run → check
   `peak_memory_mb`. If it's near the DuckDB memory limit (shown in
   Settings → General → Execution Tuning), DuckDB is spilling
   aggressively. Raise `FPULSE_DUCKDB_MEMORY_LIMIT` (e.g. `8GB`).

4. **Join cardinality.** If you have a JOIN node, one side may have
   unexpectedly high cardinality causing a hash spill. Add a **Data
   Profile** node before the join to check `distinct_count` on the
   join key.

---

## 3. "The AI chat isn't working"

**User:** The AI chat isn't working.

**Assistant:** To help diagnose, what exactly happens when you type a
message? Do you see an **error message** in the chat dock, does the
response just **never arrive** (spinner forever), or does the dock
**not appear at all**?

**User:** I see "AI is not configured" in the chat header.

**Assistant:** That means no LLM provider is set up. Two paths:

- **Local (recommended, free, private):** Install Ollama from
  https://ollama.com → pull the recommended model:
  `ollama pull qwen2.5:7b` (the 2026-05-19 tool-use floor; ~6 GB RAM
  at Q4_K_M). F-Pulse auto-detects Ollama on `localhost:11434`. If
  Ollama is on a different host, set the `OLLAMA_URL` env var and
  restart the backend. Sub-7B Qwen 2.5 models advertise tool support
  but can't reliably drive the agent loop — stick with 7b+.

- **Cloud (opt-in, means prompts leave the host):** Settings → AI
  Provider → pick a provider (Anthropic / OpenAI / OpenRouter / Gemini
  / DeepSeek / Groq / Mistral / Azure) → enter your API key → Test
  connection → Save.

After configuring, the chat dock header should show the active
provider + model name. Type `help` for a sub-1-second fast-lane
response to verify.

---

## 4. "Pipeline ran but nothing happened"

**User:** I ran my pipeline but nothing happened.

**Assistant:** When you say "nothing happened" — did the run show
**status = success** on the Executions page but no data appeared in
the target, or did the run not appear on the Executions page at all?

**User:** The Executions page shows status = success, duration 0.3s,
but my Postgres table is empty.

**Assistant:** A 0.3-second success with no rows usually means the
source returned zero rows. Two common causes:

1. **Connection credential mismatch.** Open the pipeline → click the
   source node → check which credential it references. Then open
   Credentials page and verify that credential points to the right
   database/schema/table. The AI Copilot's `inspect_connections` tool
   shows connection metadata without revealing secrets — type
   `list connections` in the chat.

2. **SQL query returns empty.** If your Database Source uses a custom
   SQL query, the WHERE clause may filter out all rows in the current
   state. Open the source node → check the SQL → test it directly
   against your database. Also check whether the run was in **Sample**
   mode (Executions page → run metadata → `full_run`), which applies
   `LIMIT 50` — if the source has fewer than 50 rows matching the
   filter, that's fine, but if the sample limit interacts with an
   `ORDER BY` + `LIMIT`, you may get zero rows.

3. **Wrong environment.** In F-Pulse+, DEV and PROD connect to
   different credential scopes. In OSS Free, only DEV exists, so this
   is less likely — but double-check the credential references
   nonetheless.

---

## 5. "I got a permission error from the chat"

**User:** The chat said something about permissions when I tried to
create a pipeline.

**Assistant:** The agent uses a tiered permission system (RBAC). Can
you tell me the exact error message? Specifically, does it mention
**"role has no allowed tiers"** or **"policy block"**?

**User:** It says "RBAC: role viewer has no allowed tiers in env=DEV
for tier=safe_write".

**Assistant:** Your agent-tool RBAC role is set to **viewer**, which
only has access to READ-tier tools (listing, inspecting, summarizing).
Creating a pipeline requires the `draft_pipeline_from_intent` tool,
which is SAFE_WRITE tier.

To check your role: type `what's my role` in the chat (fast-lane
intent, instant answer). It shows your role, environment, edition,
and which tool tiers you can access.

To change your role: in OSS Free, the single operator is typically
`admin` (full access in DEV). If your role shows `viewer`, the
`admin_settings` table may have an explicit override. Ask the person
who set up this install, or check `GET /api/ai/agent/status` which
returns the resolved role.

In F-Pulse+, roles are managed via Settings → workspace RBAC. Ask a
Workspace Admin to change your role to `developer` or `admin`.

---

## 6. "My schedule isn't firing"

**User:** I set up a schedule but it's not running.

**Assistant:** Let me help narrow this down. When you open the
**Schedules page** (sidebar → Schedules), does the schedule show
**Enabled = on** (green toggle), and what does the **Next fire** time
show?

**User:** Enabled is on, next fire shows yesterday at 3pm.

**Assistant:** A "next fire" time in the past means the scheduler
tried to fire but either the pipeline or the watchdog blocked it.
Check these in order:

1. **Pipeline status.** Only pipelines with status `published` or
   `testing` fire on schedule. Open the pipeline and check the status
   badge in the top toolbar. If it's `draft` or `archived`, the
   scheduler skips it silently.

2. **Schedule-miss alert.** If you have an `ON_SCHEDULE_MISS` alert
   configured, check the Notifications page (bell icon, top-right) for
   a schedule-miss event around 3pm yesterday. The watchdog fires this
   alert when a scheduled run doesn't start within the grace window
   (default 5 minutes).

3. **Worker pool full.** If all worker slots were occupied at fire
   time, the pipeline gets queued. Open the **Pool page** (sidebar →
   Pool) and check the **Queue depth** metric. If it was non-zero
   around 3pm, the schedule fired but the pipeline waited for a slot.
   Raise `FPULSE_MAX_CONCURRENT_RUNS` if this happens regularly.

4. **Backend restart.** If the backend restarted between the schedule
   fire and the next poll cycle (30 seconds), the scheduler may have
   missed the window. Check backend logs for a restart event around
   the expected fire time.

---

## 7. "How do I get data from Salesforce?"

**User:** I need to pull data from Salesforce.

**Assistant:** Before I suggest an approach — are you looking to pull
a **specific Salesforce object** (e.g. Accounts, Contacts, Leads),
or do you need to run a **custom SOQL query** against Salesforce?

**User:** I want to pull Contacts and Leads on a daily schedule.

**Assistant:** For Contacts and Leads, use the **SaaS Connector**
node with the Salesforce manifest:

1. Create a new pipeline (Pipelines page → + New Pipeline).
2. Drag a **SaaS Connector** node onto the canvas. In the ConfigPanel,
   pick **salesforce** from the manifest dropdown.
3. In the **Streams** section, select `contacts` and `leads`.
4. For authentication: open the **Credentials page** → + New Credential
   → type `oauth2` → enter your Salesforce Connected App's client_id,
   client_secret, and the refresh token. Save, then pick this
   credential in the SaaS Connector node.
5. Add downstream nodes (Schema Mapper → Bulk Loader or Database Sink).
6. Set up a daily schedule: pipeline Settings → Schedule tab → type
   `daily`, time `06:00` (or whenever).

**Note:** OSS Free ships the **starter** Salesforce manifest. It
supports basic pagination and schema for common objects. The
production-hardened Salesforce connector (full incremental sync,
advanced auth flows, SOQL query support) is F-Pulse+ only.

To check the Salesforce manifest's certification depth:
`GET /api/connectors/cert-matrix` → find `salesforce` → look at
`depth_score`. Depth 5 = production-grade; lower means some features
(incremental sync, fixtures) are missing.

---

## 8. "The agent timed out"

**User:** The agent just timed out.

**Assistant:** The Copilot has a wall-clock cap to prevent runaway
loops. Was this on **local Ollama** (default) or a **cloud provider**?
The default cap is 300 seconds for local and 120 seconds for cloud.

**User:** I'm on local Ollama, it timed out at 300 seconds.

**Assistant:** 300 seconds on local Ollama usually means the model is
heavier than the tool-use floor for your CPU. Check which model is
active — the chat dock header shows it (e.g. `ollama / qwen2.5:14b`).

If you're running anything heavier than `qwen2.5:7b` / `llama3.1:8b` /
`phi-4` on CPU: switch to the floor. Pull it via Insights → AI Provider
→ Ollama section → type `qwen2.5:7b` in the model input → pull.

If you're on a model BELOW the floor (`qwen2.5:1.5b` / `:3b`) the
symptom is usually different — instead of timing out, the agent
silently returns a greeting because the model never actually called
the tool. The first-launch banner catches this and offers a one-click
upgrade to `qwen2.5:7b`.

If you're already on `qwen2.5:7b` and still timing out:
- Close heavy processes (VS Code, browsers, other Docker containers)
  competing for CPU/RAM.
- Raise the cap: set `FPULSE_AGENT_WALL_CLOCK_S=600` in the backend
  env and restart. Maximum is 600.
- Use **fast-lane phrasings** for common questions — they bypass the
  LLM entirely: `list pipelines`, `overview`, `what failed today`,
  `running now`, `what's my role`. Sub-1-second response.

If your original question was analytical (e.g. "why did pipeline X
fail"), try rephrasing to a fast-lane version first (`what failed
today`), then ask the follow-up "why" on a specific pipeline. This
splits one 300-second turn into a fast answer + one focused LLM turn.

---

## 9. "Bulk loader says not available"

**User:** I'm getting an error about the bulk loader.

**Assistant:** Can you share the exact error message? There are two
common ones: `BulkLoaderNotAvailable` (driver missing) vs a
connection-level error (wrong credentials or host).

**User:** It says "BulkLoaderNotAvailable: postgresql dialect requires
psycopg2-binary".

**Assistant:** The Postgres bulk loader uses `COPY FROM STDIN` via
psycopg2, which is an optional dependency. Install it:

```
pip install psycopg2-binary>=2.9
```

Or add `psycopg2-binary>=2.9` to your `requirements.txt` if you're
building a custom Docker image.

Without the driver, the bulk-load runner raises
`BulkLoaderNotAvailable` and the pipeline executor **falls back to
row-by-row INSERT** automatically. Your pipeline still works — it's
just slower (10-100x for large datasets). The fallback is by design so
missing an optional driver never breaks existing pipelines.

After installing, restart the backend. The Bulk Loader node will
automatically use `COPY FROM STDIN` on the next run. You can verify
by checking the execution metadata: Executions page → click the run →
look for `method: "COPY FROM STDIN"` in the step detail.

For Snowflake bulk loading, the Snowflake dialect uses the
`snowflake-connector-python` package instead — same pattern, same
fallback behavior.

---

## 10. "Can we check the existing sample templates?"

**User:** Can we check the existing sample templates for the pipelines?

**Assistant:** Yes — F-Pulse OSS ships **27 pipeline templates** as
starters. To see them: open the **Pipelines page** (sidebar → Pipelines)
and click **+ New Pipeline**. The template chooser opens with all 20
templates: simple ETL, dedup, aggregation, data quality with DLQ,
SCD Type 2 dimension, API ingest with pagination, profile a source,
daily aggregation, Slack alert on failure, database replication via
Bulk Loader, and more.

Click any template to spawn a new pipeline pre-wired with that shape.
You can edit freely from there.

**Quick clarification on terminology:**
- **Templates** = starters that ship with F-Pulse, accessed via
  "+ New Pipeline → pick a template."
- **Pipelines** = your actual workflows in this workspace, listed on
  the Pipelines page.

If you want to **add a custom template** for your team, drop a JSON
file under `data/templates/` (backend) + `frontend/src/templates/`
(frontend) and restart. F-Pulse+ adds a private template marketplace
that handles this through the UI without filesystem access.

---

## 11. "I want to track historical changes to customer records"

**User:** I need to track changes to my customer data over time.

**Assistant:** To confirm: do you want to keep **every version** of a
customer record (full history with valid_from / valid_to dates), or
just ensure the **latest version** is always in the target table
(upsert / overwrite)?

**User:** Full history — I need to see what changed and when.

**Assistant:** Use the **SCD Type 2** node (Slowly Changing Dimension
Type 2). It tracks historical versions per business key:

1. Open your pipeline → drag an **SCD Type 2** node after the source.
2. Configure it:
   - **business_key**: the column(s) that identify a customer (e.g.
     `["customer_id"]`)
   - **tracked_columns**: the columns you want to track changes on
     (e.g. `["email", "address", "tier"]`)
   - **effective_from_column**: defaults to `valid_from`
   - **effective_to_column**: defaults to `valid_to`
   - **current_flag_column**: defaults to `is_current`
3. Connect the output to a **Bulk Loader** node in **merge mode** with
   `primary_key` set to the surrogate key column (defaults to
   `scd_id`).

How it works: on each run, the SCD2 node compares incoming rows
against existing rows by business key. Changed rows get the previous
version closed (`is_current=false`, `valid_to=run_time`) and a new
version inserted (`is_current=true`, `valid_from=run_time`). Unchanged
rows are skipped via hash-based change detection (SHA-256 of tracked
column values).

The node is registered as `StepType.SCD2` in the node registry. Full
parameter list: `business_key`, `tracked_columns`,
`effective_from_column`, `effective_to_column`, `current_flag_column`,
`surrogate_key_column`, `null_high_water` (default `9999-12-31`),
`passthrough_columns`.
