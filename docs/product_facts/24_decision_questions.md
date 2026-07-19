# Decision questions — "When do I X vs Y?"

25+ comparison questions with honest F-Pulse-specific answers. Each
cites the source of truth (edition-matrix.md, a design doc, or a
specific env var / config).

---

## 1. SCD Type 2 vs Upsert — when to use each

**Q:** When should I use SCD Type 2 instead of Upsert?

**A:**
- **Upsert** when you only need the **latest version** of each row.
  The Upsert node does `INSERT ... ON CONFLICT DO UPDATE` on the key
  columns. Re-running never produces duplicates. Use for fact tables,
  staging tables, or any table where historical versions don't matter.

- **SCD Type 2** when you need to **track every historical version**
  of a row. The SCD2 node keeps previous versions with `is_current`,
  `valid_from`, `valid_to` columns. Use for dimension tables (customer,
  product, employee) where you need to answer "what was the value on
  date X?"

**Rule of thumb:** If your downstream queries ever need `WHERE
valid_from <= @date AND valid_to > @date`, use SCD2. Otherwise Upsert.

**Source:** `StepType.SCD2` and `StepType.UPSERT` in
`backend/fpulse/ir/schema.py`.

---

## 2. Bulk Loader vs Database Sink — at what row count

**Q:** When should I switch from Database Sink to Bulk Loader?

**A:**
- **Database Sink** uses row-by-row `INSERT INTO ... VALUES (...)`.
  Fine for <10,000 rows. Simple, works with any database.

- **Bulk Loader** uses dialect-native bulk paths (Postgres: `COPY FROM
  STDIN`; Snowflake: `PUT` + `COPY INTO`). 10-100x faster for >10,000
  rows. Supports modes: create, append, truncate, merge.

**Switch threshold:** ~10,000 rows. Below that, the overhead of bulk
setup isn't worth it. Above that, the per-row INSERT overhead dominates
and Bulk Loader wins decisively.

**Caveat:** Bulk Loader needs optional drivers (`psycopg2-binary` for
Postgres, `snowflake-connector-python` for Snowflake). Without them,
the executor falls back to row-by-row INSERT automatically.

**Source:** `backend/fpulse/engine/bulk_load/types.py` (protocol),
`edition-matrix.md` line 27 (OSS connectors).

---

## 3. Local Ollama vs cloud LLM provider

**Q:** When should I use local Ollama vs a cloud LLM?

**A:**
- **Local Ollama (default):** $0 operating cost, data never leaves the
  host, privacy-first. Recommended model: `qwen2.5:7b` (4.7 GB, ~6 GB
  RAM at Q4_K_M, 30–60 s/turn on CPU — the 2026-05-19 tool-use floor).
  Best for: solo developers, regulated environments, cost-sensitive
  setups.

- **Cloud provider (opt-in):** 1-3 s/turn latency, better quality on
  complex analytical questions, costs $/turn. Best for: users who need
  fast responses and can tolerate prompts leaving the host.

**Decision factors:**
| Factor | Local Ollama | Cloud |
|---|---|---|
| Privacy | Data stays on host | Prompts leave host |
| Latency | 30–60 s/turn on CPU at the floor | 1–5 s/turn |
| Cost | $0 | $/turn (shown in dock) |
| Tool quality | Reliable on `qwen2.5:7b`+ | Excellent on Claude/GPT-4o |
| GPU needed? | No (CPU works at the floor) | No (cloud inference) |

**Hybrid approach:** Use local Ollama as default + fast-lane for common
questions (sub-1 s, no LLM). Switch to cloud only for complex
analytical questions where you need high quality + fast answers.

**Source:** `edition-matrix.md` lines 83, 96-97. Provider config:
Insights → AI Provider.

---

## 4. DEV vs PROD — when do I need F-Pulse+?

**Q:** My pipelines work in DEV. When do I need PROD?

**A:** Per `edition-matrix.md` lines 60-61:

- **DEV** (OSS Free): the iterative sandbox. Source nodes cap at
  `DEV_SAMPLE_ROWS` (50 by default) in Sample mode. Full data runs
  are available in Live mode. Most write operations permitted. **This
  is the only contracted environment in OSS Free.**

- **PROD** (F-Pulse+ only): adds the two-gate approval workflow
  (Gate 1: developer requests → Gate 2: approver confirms after
  Sandbox dry-run), audit-log retention, and environment isolation.

**You need PROD (= F-Pulse+) when:**
1. Multiple people need to approve pipeline changes before they run
2. You need Sandbox dry-runs against real data before production writes
3. Audit compliance requires retention + sigstore-signed export
4. You want environment isolation (DEV credentials vs PROD credentials)

**If you're a solo developer** running pipelines on your laptop or VPS,
DEV is sufficient. F-Pulse OSS runs full-dataset pipelines in Live
mode — DEV doesn't mean "limited."

---

## 5. Schedule vs manual run

**Q:** When should I schedule a pipeline vs run it manually?

**A:**
- **Manual run:** exploratory work, one-off data loads, debugging. Run
  from the editor (Run button → PreRunBanner) or Pipelines page →
  row → Run now.

- **Scheduled run:** recurring data loads (daily syncs, hourly
  aggregations). Pipeline → Settings → Schedule tab. Types: `cron`,
  `daily`, `hourly`, `interval`, `once`.

**Rule of thumb:** If you'll run it more than 3 times, schedule it.
The scheduler polls every 30 seconds; the first fire is within 30 s
of the configured time.

**Prerequisite:** Only pipelines with status `published` or `testing`
fire on schedule. `draft` and `archived` pipelines are skipped.

---

## 6. Custom REST connector vs SaaS Connector with manifest

**Q:** When should I use a REST/HTTP API source vs a SaaS Connector
with a manifest?

**A:**
- **SaaS Connector + manifest:** When a manifest exists for your SaaS
  source (37 manifests ship in OSS). The manifest handles auth,
  pagination, schema, and incremental sync declaratively. Check
  `GET /api/connectors/cert-matrix` for available manifests.

- **REST/HTTP API source:** When no manifest exists for your API, or
  when the API requires custom logic the manifest format can't express
  (e.g. GraphQL, SOAP, multi-step OAuth dance).

**When to write a new manifest instead:** If you're going to pull from
this API more than once, invest 30 minutes to write an F0.1 v2
manifest. The certify CLI validates it, and the SaaS Connector picks
it up automatically. See `23_code_generation.md` for manifest examples.

---

## 7. Data Quality drop mode vs DLQ split mode

**Q:** When should I use drop mode vs DLQ split mode on the Data
Quality node?

**A:**
- **Drop mode** (default): rows failing validation are silently removed.
  Use when bad rows are noise you can safely discard (e.g. test records,
  known-bad legacy data).

- **DLQ split mode:** failed rows are routed to a separate dead-letter
  output that you connect to a sink. Use when you need to investigate
  failures, when data loss is unacceptable, or for compliance (proving
  which rows were rejected and why).

**Recommendation:** Default to DLQ split mode. Connect the DLQ output
to a CSV Sink or JSON Sink for inspection. Drop mode should only be
used when you've verified the failure patterns are understood.

**Source:** `StepType.DATA_QUALITY` in `backend/fpulse/ir/schema.py`.
Data Quality node: `backend/fpulse/nodes/quality.py`.

---

## 8. Transform node vs Schema Mapper node

**Q:** When should I use a Transform node vs a Schema Mapper?

**A:**
- **Transform node:** Write DuckDB SQL. Full power — joins, window
  functions, aggregations, CTEs. Use for anything that requires logic.

- **Schema Mapper node:** Declarative source-to-target field mapping
  with type coercion. No SQL. Use when you're renaming/retyping columns
  from a SaaS source to match your target schema.

**Rule of thumb:** If you can express the transformation as "rename
column A to B, cast column C to INTEGER," use Schema Mapper. If you
need any computation (arithmetic, string functions, aggregation), use
Transform.

**Source:** `StepType.TRANSFORM` and `StepType.SCHEMA_MAPPER` in
`backend/fpulse/ir/schema.py`.

---

## 9. OSS Free vs F-Pulse+ — 8 trigger conditions for upgrade

**Q:** When should I switch from OSS Free to F-Pulse+?

**A:** Per `edition-matrix.md` and `15_workers_scaling.md`:

1. **More than 1 user** needs to edit pipelines → Plus adds workspace
   RBAC (5-tier: Super Admin → Viewer)
2. **DEV → PROD promotion** with approval gates → Plus only (lines
   61-62)
3. **Compliance scope** requiring audit log retention + sigstore-signed
   export → Plus only (line 141)
4. **SSO / SAML / OIDC** integration → Plus only (line 134)
5. **Vault-backed credentials** (HashiCorp Vault, AWS Secrets Manager,
   Azure Key Vault) → Plus only (line 145)
6. **Horizontal scaling** beyond a single host (containerized worker
   pool, Redis queue) → Plus only (line 55)
7. **Enterprise connectors** (SAP, NetSuite, Workday, Dynamics 365,
   ServiceNow, production-grade Salesforce) → Plus only (line 44)
8. **Cross-session chat memory** (the Copilot remembers previous
   conversations) → Plus only (line 110)

**If none of these apply**, OSS Free is the right answer. It includes
the full execution engine, all 25 agent tools, 33 connectors, and
unlimited pipelines/runs.

---

## 10. Fernet encryption vs Vault-Ref

**Q:** Should I use the built-in credential encryption or set up Vault?

**A:**
- **Built-in Fernet encryption (OSS Free):** Always-on, zero config.
  Credentials encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256),
  master key at `~/.fpulse/secret.key`. This is the ONLY path in OSS.

- **Vault-Ref pattern (F-Pulse+ only):** Credentials live in your
  external vault (HashiCorp Vault, AWS Secrets Manager, Azure Key Vault,
  Google Secret Manager). F-Pulse stores only a reference
  (`vault:secret/data/prod/postgres`). Secrets are never written to
  disk.

**Decision:** If you're on OSS Free, you have no choice — use the
built-in Credentials page. If you're on Plus and already operate
an external vault, Vault-Ref avoids duplicating secrets.

**Source:** `07_credentials.md`, `edition-matrix.md` lines 32, 145.

---

## 11. Data Profile vs Data Quality

**Q:** When do I use Data Profile vs Data Quality?

**A:**
- **Data Profile:** Outputs one row PER COLUMN with summary statistics
  (null %, distinct count, min/max, top value). Use for **exploration**
  — understanding the shape of your data before building the pipeline.

- **Data Quality:** Applies validation RULES to every row. Rows pass
  or fail. Use for **enforcement** — ensuring data meets requirements
  before it reaches the sink.

**Common pattern:** Data Profile first (understand the data) → Data
Quality downstream (enforce rules based on what you learned).

---

## 12. CSV Source vs File Source

**Q:** Should I use CSV Source or the universal File Source?

**A:**
- **CSV Source:** Dedicated CSV parser with header detection, custom
  delimiter, encoding override. Use when you know the input is CSV
  and need fine control over parsing.

- **File Source:** Universal node that auto-detects format from file
  extension (CSV, JSON, Parquet, Excel, XML, NDJSON, TSV). Use when
  you want flexibility or when the pipeline might receive different
  file formats.

**Source:** `StepType.CSV_SOURCE` and `StepType.FILE_SOURCE` in
`backend/fpulse/ir/schema.py`.

---

## 13. Materialize node vs checkpoint

**Q:** What's the difference between a Materialize node and the
checkpoint store?

**A:**
- **Materialize node:** Saves the intermediate result to a temp DuckDB
  table explicitly in the pipeline graph. Use when you want to query
  the intermediate result directly or when multiple downstream nodes
  need the same upstream data (avoids recomputation).

- **Checkpoint store:** Automatic per-step Parquet snapshots written
  by the executor after each successful step. Enables Resume-from-step
  on failed runs. You don't add this to the pipeline — it happens
  automatically. Files live in `data/checkpoints/<run_id>/<step_id>.parquet`
  with a 7-day TTL.

**They serve different purposes:** Materialize is a design choice;
checkpoints are an operational safety net.

---

## 14. Filter node vs Data Quality drop mode

**Q:** Both can remove rows — when use which?

**A:**
- **Filter node:** Simple predicate (SQL-style condition). Keeps rows
  matching the condition. No reporting, no DLQ. Use for business logic
  filtering ("only orders > $100").

- **Data Quality drop mode:** Declarative validation rules with
  reporting. Even in drop mode, the Data Quality node logs which rules
  failed and how many rows were affected. Use for data validation
  ("reject rows with null email").

**Rule of thumb:** If you'd express it as "keep rows where X" → Filter.
If you'd express it as "reject rows that violate rule Y" → Data Quality.

---

## 15. Webhook Sink vs API Sink

**Q:** When do I use Webhook Sink vs API Sink?

**A:**
- **Webhook Sink:** Sends the entire output as a single POST to a
  webhook URL. Use for notifications, triggers, or small payloads.

- **API Sink:** Sends each row as a separate POST to a REST endpoint.
  Use for per-record writes to an API (e.g. creating records in a CRM).

**Source:** `StepType.WEBHOOK_SINK` and `StepType.API_SINK` in
`backend/fpulse/ir/schema.py`.

---

## 16. Join node vs Lookup node

**Q:** When should I use Join vs Lookup?

**A:**
- **Join node:** Full SQL join semantics (INNER, LEFT, RIGHT, FULL,
  ANTI, SEMI). Both sides are full datasets. Use when both inputs are
  comparably sized.

- **Lookup node:** One side is the main dataset, the other is a
  reference table. Semantically similar to a LEFT JOIN but optimized
  for the case where the lookup table is small (fits in memory as a
  hash map).

**Rule of thumb:** If the reference table is <100K rows, Lookup is
simpler to configure. For larger reference tables or complex join
conditions, use Join.

---

## 17. In-app notification vs email vs Slack vs Discord

**Q:** Which notification channel should I use?

**A:**
All four are OSS Free. Pick based on your workflow:

- **In-app bell:** Always-on. Good for low-urgency alerts you'll check
  when you're in the F-Pulse UI.
- **Email:** For alerts you need in your inbox. Requires SMTP config
  in Settings → Notifications.
- **Slack / Discord:** For team channels where pipeline alerts should
  be visible. Configure the webhook URL in Settings → Notifications.
- **Generic webhook:** For integration with any external system
  (PagerDuty, OpsGenie, custom scripts). Send the alert payload to
  any HTTP endpoint.

**Configure:** Settings → Notifications → pick channel → enter config
→ Test → Save.

**Source:** `edition-matrix.md` line 64.

---

## 18. Deduplicate node vs Transform with ROW_NUMBER

**Q:** Should I use the Deduplicate node or write ROW_NUMBER SQL?

**A:**
- **Deduplicate node:** No-code. Configure key columns + order_by.
  Keeps the row with the highest order_by value per key. Simpler,
  less error-prone.

- **Transform with ROW_NUMBER:** Full SQL control. Use when the dedup
  logic is complex (e.g. keep the row with the highest value in column
  A, but if tied, break by column B descending).

**Recommendation:** Start with the Deduplicate node. Switch to SQL
only if you need tie-breaking logic or conditional dedup.

---

## 19. Sample mode vs Live mode vs Dry-run vs Validate-only

**Q:** What's the difference between the four run modes?

**A:** The PreRunBanner offers four modes (configurable default in
Settings → General → Default Run Behavior):

- **Live:** Full dataset, real writes to sinks. Production behavior.
- **Sample:** Sources capped at `DEV_SAMPLE_ROWS` (default 50). Real
  writes but on a tiny dataset. For fast iteration.
- **Dry-run:** Full dataset through transforms but sinks are skipped.
  Shows what WOULD be written. For validation without side effects.
- **Validate-only:** Checks node configs + connection reachability
  without running any data. Fastest — sub-second for most pipelines.

**Decision flow:**
1. Building a new pipeline? → **Sample** (fast feedback)
2. Pipeline works on sample, ready for full data? → **Dry-run** (verify
   at scale without writing)
3. Dry-run looks good? → **Live** (real execution)
4. Just checking configs after a credential change? → **Validate-only**

---

## 20. Database Source with SQL vs SaaS Connector

**Q:** I have a Postgres database. Should I use Database Source or SaaS
Connector?

**A:**
- **Database Source:** Direct SQL query against Postgres / MySQL / MSSQL
  / SQLite. You write the SQL. Use for databases you control.

- **SaaS Connector:** Manifest-driven REST connector for SaaS APIs. NOT
  for direct database connections.

**If your data is in a database** (Postgres, MySQL, etc.), always use
Database Source. SaaS Connector is for APIs (HubSpot, Stripe, etc.).

---

## 21. `$vars.NAME` vs `${param.NAME}` vs env var

**Q:** When should I use workspace variables vs pipeline parameters vs
environment variables?

**A:**
- **`$vars.NAME`** — workspace variable. Shared across all pipelines in
  the workspace. Set in sidebar → Variables page. Use for
  environment-specific values (bucket name, schema name) that multiple
  pipelines share.

- **`${param.NAME}`** — pipeline parameter. Scoped to one pipeline.
  Overridable at run time via the Run dialog or API. Use for values
  that change per run (date range, target table name).

- **Environment variables (`FPULSE_*`)** — backend configuration.
  NOT available in expressions. Set at deployment time, require restart.
  Use for system-level config only (memory limits, ports, feature flags).

**Common mistake:** Trying to use env vars in node params. F-Pulse
expressions don't resolve env vars. Set the value as a workspace
variable first, then reference `$vars.NAME`.

---

## 22. Warehouse Sink vs Bulk Loader

**Q:** Both write to databases — when use which?

**A:**
- **Warehouse Sink:** Row-by-row INSERT with `auto_evolve` (auto-adds
  new columns as VARCHAR). Use for small datasets or when you need
  automatic schema evolution.

- **Bulk Loader:** Dialect-native bulk path. 10-100x faster. Supports
  merge mode (idempotent upsert on primary key). No auto-evolve. Use
  for >10K rows.

**Recommendation:** Start with Warehouse Sink for prototyping (handles
schema changes gracefully). Switch to Bulk Loader for production loads.

---

## 23. Agent READ tools vs the Executions page

**Q:** Should I ask the Copilot or check the Executions page directly?

**A:**
- **Ask the Copilot** when you want a quick answer without leaving your
  current context. Fast-lane phrasings: `what failed today`, `running
  now`, `overview`. For analytical questions: "why did pipeline X fail",
  "which pipeline uses the most memory".

- **Executions page** when you want to browse, filter, sort, and drill
  into specific runs. Better for: bulk operations, comparing multiple
  runs side by side, using the Resume-from-step button.

**Both use the same data source** — the Copilot's `list_executions`
tool queries the same SQLite table the Executions page reads from.

---

## 24. Single pipeline vs Execute Pipeline node (sub-pipelines)

**Q:** When should I split logic into sub-pipelines?

**A:**
- **Single pipeline:** Simple, easy to debug, one execution record.
  Use when the logic is linear (source → transform → sink).

- **Execute Pipeline node:** Calls another pipeline as a step. Use
  when you have reusable logic (e.g. a "clean customer data" pipeline
  called by 5 different ingest pipelines), or when you want isolated
  failure handling (a sub-pipeline failure doesn't kill the parent if
  the Retry Handler wraps it).

**Rule of thumb:** Don't split prematurely. A single pipeline with 10
nodes is easier to maintain than 5 pipelines with 2 nodes each.

**Source:** `StepType.EXECUTE_PIPELINE` in `backend/fpulse/ir/schema.py`.

---

## 25. Retry Handler node vs pipeline-level re-run

**Q:** Should I use a Retry Handler node or just re-run the pipeline?

**A:**
- **Retry Handler node:** Wraps a specific node with retry logic +
  exponential backoff. Use for transient failures in a single step
  (API rate limits, network timeouts). The rest of the pipeline
  continues normally.

- **Pipeline re-run (Resume-from-step):** Re-runs the entire pipeline
  from the first failed step using checkpoints. Use when the failure
  is unexpected and you want to retry the whole remaining pipeline
  after fixing the root cause.

**Recommendation:** Add Retry Handler nodes around API sources and
database sinks that face transient errors. Use pipeline re-run for
one-off failures.

---

## 26. OpenRouter free-tier vs paid cloud provider

**Q:** Should I use OpenRouter's free tier or pay for Anthropic/OpenAI?

**A:**
- **OpenRouter free-tier:** $0, routes to open-source models (Llama,
  Qwen). Latency is 5-20 s/turn (rate-limited). Tool support varies
  by model — use the "free-tier + tools-only" filter in Settings →
  AI Provider to find compatible models.

- **Paid cloud (Anthropic, OpenAI, etc.):** 1-3 s/turn, highest
  quality, costs per turn. The chat dock shows per-turn cost after
  each response.

**Decision:** If you're evaluating F-Pulse and want cloud-quality
responses for free, start with OpenRouter free tier. If you need
fast, reliable, high-quality responses for production use, pay for
Anthropic or OpenAI.

**Source:** `edition-matrix.md` lines 83-87, `20_pricing_resources.md`.

---

## 27. Interval schedule vs cron schedule

**Q:** When should I use interval vs cron scheduling?

**A:**
- **Interval** (`every N minutes`): Use for near-real-time ingestion
  (every 5 min, every 15 min). The scheduler fires every N minutes
  from the time the schedule is created.

- **Cron** (full cron expression): Use for time-of-day or day-of-week
  schedules ("every weekday at 6am", "first Monday of the month").

- **Daily / Hourly / Once:** Shorthand presets for the most common
  cron patterns.

**Note:** The scheduler polls every 30 seconds, so the minimum
effective interval is ~30 seconds regardless of what you set.
