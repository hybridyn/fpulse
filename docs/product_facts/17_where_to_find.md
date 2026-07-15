# Where do I find…? — F-Pulse navigation reference

UI navigation Q&A — every entry is a self-contained "where is X" question
with the exact path. The Copilot retrieves these chunks when asked about
locating any feature.

**Convention:** primary path first, alternatives second. Keyboard
shortcuts called out where they exist.

---

## Where is the Dashboard?

**Sidebar → first entry** (the home icon). URL hash: `#dashboard`. Also clickable from the F-Pulse logo at the top of the sidebar. The Dashboard is the home / landing page — greeting + date/time, hero KPIs (4 gradient cards, env-specific), workspace inventory or operations cards, system usage (CPU, memory, threads, throughput, DB size, uptime), admin-only cards (seats, users, workspaces, role) when isAdmin, a chart + donut, and three recent-activity feed tables. DEV and PROD render different KPIs and different feeds.

## Where is the Pipelines page?

**Sidebar** → **Pipelines** (clipboard / list icon). Keyboard: `g` then `p`. URL hash: `#pipelines`.

This is the per-pipeline list — every pipeline in the workspace with its status badge, step count, run button, and editor entry-point. **Distinct from the Dashboard**, which is the home / overview page above.

## Where do I see my projects?

**Sidebar** → **Projects** (folder icon). URL: `#projects`. Each project is a collection of pipelines sharing a logical purpose; pipelines belong to exactly one project.

## Where are my schedules?

**Sidebar** → **Schedules**. Each row shows the pipeline + cadence + next fire time + enabled toggle.

## Where are my alerts / notifications?

- **Alert RULES**: configured per-pipeline. Open the pipeline → **Alerts** tab in the editor.
- **Notification HISTORY** (the bell): top-right header → **🔔 bell icon** for the last 20; full history at **#notifications**.
- **Notification CHANNELS** (email SMTP, Slack webhook, etc.): Settings → **Notifications** tab.

## Where is Storage / where do I find my files and managed tables?

**Sidebar** → **Storage** (stacked-cylinders icon, between Pool and Insights). URL hash: `#storage`. Three sub-tabs:

* **Files** — uploaded files (CSV / JSON / Parquet / Excel / XML). Each row shows scope (Global vs project), size, "Used by N pipelines" pill, last-updated, and row actions (Preview, Replace bytes, Promote to managed table, Delete).
* **Managed Tables** — Parquet-backed tables addressable by `schema.name` from Managed Table Source / Managed Table Sink nodes. Each row shows rows / columns / size / part count / "Used by N" / Drop.
* **Pipeline Outputs** — files written by pipeline runs, grouped by `(pipeline_id, run_id)`.

The Storage page is workspace-scoped; the active project context (from Projects) auto-filters via the All / Global / Project chip strip.

## Where do I upload a data file?

**Storage page** → **+ Upload file** button (top right). Opens a dialog asking:

1. **Scope** — Global (visible to every project) or Project (scoped to one).
2. **Project** picker — appears when Project scope is chosen.
3. **Folder** picker — optional sub-folder within the project.
4. **Description** — optional note.
5. **File** — drag-and-drop or click-to-browse.

Files land under `$FPULSE_DATA_DIR/uploads/{workspace_id}/...` and are indexed in `storage_objects` so the Files tab can show them. Allowed formats: CSV, TSV, JSON, NDJSON, Parquet, Excel, XML.

## Where do I see what pipelines use a file or managed table?

**Storage page** → **Files** or **Managed Tables** tab. The **"Used by"** column shows a "Used by N" pill on each row. Click it to open a popover listing every referencing pipeline with name + "Open in Editor" link.

Backend scanner walks all workflows for the workspace and detects four reference shapes: `local_table_source` / `local_table_sink`, generic `source`/`destination` with `connector_type='local_table'`, file-path matches against `storage_object.path`, and promote-to-table provenance (a file that seeded a managed table inherits the table's pipeline list).

Destructive actions (Delete, Drop, Replace) warn before proceeding when usage exists.

## Where do I manage credentials?

**Sidebar** → **Credentials** (key icon). URL: `#credentials`. Workspace-scoped list.

For project-scoped credentials: open the project → **Credentials** tab.

## Where do I manage connections?

**Sidebar** → **Connections** (plug icon). URL: `#connections`. Connections reference credentials by ID; you can configure a connection without leaving this page.

## Where do I configure the AI Copilot?

**Settings** → **AI Provider** tab. URL: `#settings` then click the AI Provider tab. Or click the **Configure »** chip in the chat dock header.

## Where do I see what the agent has done (audit trail)?

Insights → **Activity** subtab. Combined audit + agent trace + execution timeline.

For just the agent: Insights → Activity → filter source = **agent**.
For just pipeline runs: **Executions** page.

## Where is the Trust page?

**Sidebar** → **Insights** → **Trust** subtab. URL: `#trust` (legacy direct link still works). Shows the live posture, what F-Pulse never does, deployment modes, and the eval pass rate.

## Where do I find the eval harness output?

- **UI**: Trust page → **AI eval pass rate** section.
- **API**: `GET /api/trust/eval-summary` (returns the latest run's summary as JSON).
- **CLI**: results land under `eval_results/<timestamp>.json` after `python -m fpulse.eval.run`. Latest is always at `data/eval/latest.json`.

## Where do I find the connector certification matrix?

**Sidebar** → there's a direct entry, OR navigate to URL `#cert-matrix`. API: `GET /api/connectors/cert-matrix`.

Shows depth score (0-5) per manifest with category, vendor, validation status, and per-stream breakdown.

## Where is the Pool / Worker monitoring page?

**Sidebar** → **Pool**. Shows the governor banner, hardware presets, queue depth, throughput, spill-disk health (SSD/HDD badge).

## Where do I see lineage?

**F-Pulse+ only.** Sidebar → **Lineage**. Marquez-compatible cross-pipeline dataset provenance graph.

OSS Free has no lineage view; the closest substitutes are `recall_history` (chat fast lane) and `summarize_pipeline` for individual pipeline reasoning.

## Where do I configure approval policy?

**F-Pulse+ only.** Settings → **Approvals** tab. Configure who can approve, optional two-person rule, escalation policy.

OSS Free has no approval workflow.

## Where do I see my role / permissions?

**Two paths**:
- **Chat fast lane**: type `what's my role` → instant answer with role + environment + edition + tool tiers + can-approve + can-deploy-prod.
- **UI**: top-right user menu → **Account**.

## Where do I see my AI usage / token wallet?

- **Chat dock header**: shows `% of daily cap` next to the model name.
- **API**: `GET /api/ai/agent/budget` — returns user + workspace usage, cap, request count, today's cost USD.
- **UI**: Insights → **AI Provider** tab.

## Where is the Settings page?

**Sidebar** → **Settings** (gear icon). URL: `#settings`. Has tabs: **General**, **Security**, **Notifications**, **About**.

## Where do I configure DuckDB tuning / memory limits?

**Settings → General → Execution Tuning** section. Read-only display of the live values from `/api/pool/config`. To change, set the env var (`FPULSE_DUCKDB_MEMORY_LIMIT`, `FPULSE_DUCKDB_THREADS`, `FPULSE_MAX_CONCURRENT_RUNS`, `FPULSE_DUCKDB_TEMP_DIR`) and restart.

## Where do I configure CORS / IP allowlist / proxy trust?

**Settings → Security → Operator Config** section. Read-only display of env-var values: `FPULSE_CORS_ORIGINS`, `FPULSE_PLUS_IP_ALLOWLIST` (Plus only), `FPULSE_TRUSTED_PROXIES`. Restart to apply changes.

## Where do I configure telemetry?

**Settings → Security → Privacy** section. Default OFF. Opt-in is informational only — the telemetry sender is not active.

## Where do I configure backup destinations?

**Settings → Backup** (separate page or section). Configure local filesystem / S3 / Azure / GCS destinations.

## Where do I view the docs from inside the app?

**Sidebar** → **Help**. Has tabs:
- **Getting Started** — 6-step onboarding for new users
- **How-To Guides** — recipe-style step-by-step guides grouped by category
- **Shortcuts** — keyboard shortcuts reference
- **Nodes** — searchable catalog of every node type with descriptions
- **Reference** — full docs synced from `docs/*.md` (architecture, scaling, deployment, troubleshooting, API, etc.)

## Where is the deployment runbook?

`docs/deployment.md` (in-app: Help → Documentation → Deployment & Upgrade). Three-component upgrade model (F-Pulse / Ollama runtime / Ollama models, independently versioned).

## Where do I see the version of F-Pulse I'm running?

**Settings → About** tab. Shows F-Pulse version, schema version, edition (Free/Plus), backend hostname, build commit.

## Where do I find the master encryption key file?

`~/.fpulse/secret.key` on POSIX (or `$FPULSE_DATA_DIR/secret.key` if `FPULSE_DATA_DIR` is set). Created on first run; chmod 600. **Back this up** — without it, encrypted credentials are unrecoverable.

To override the location: set env var `FPULSE_MASTER_KEY_FILE=/path/to/secret.key`.

## Where do I find the SQLite database file?

`<data_dir>/fpulse.db` — typically `data/fpulse.db` for from-source installs or inside the `fpulse_data` Docker volume for compose deployments.

## Where do I find pipeline checkpoint Parquet files?

`<data_dir>/checkpoints/<run_id>/<step_id>.parquet` — written after every successful step. Used by the Resume-from-step feature. TTL 7 days by default.

## Where do I find logs?

- **Backend stdout**: from-source: terminal where uvicorn runs. Docker: `docker compose logs fpulse`.
- **Per-pipeline-run output**: Executions page → click the run → **Logs** tab.
- **Audit log**: Insights → Activity tab.

## Where do I find Ollama logs?

`docker compose logs ollama` (or the host service if Ollama is installed natively). Useful for debugging "why is the model slow" or "why did the pull fail".

## Where do I switch environments (DEV ↔ PROD)?

**Top header** → environment chip (next to the user menu). The chip color is **emerald for DEV** and **amber/red for PROD**. **PROD environment is F-Pulse+ only** — in OSS the chip may be visible but the contracted execution path is DEV.

## Where do I find the agent's tool list?

- **Chat fast lane**: type `what tools does the agent have` → instant answer.
- **API**: `GET /api/ai/agent/status` returns `tool_count`.
- **Code**: `backend/fpulse/ai/tools/` — one file per tool.
- **Doc**: `docs/product_facts/10_ai_copilot.md` enumerates all 25 tools by tier.

## Where do I see what's running right now?

- **Chat fast lane**: type `running now` → instant answer.
- **UI**: Executions page → filter **Status = running**.
- **API**: `GET /api/monitor/executions?status=running` or the agent tool `get_running_executions`.

## Where is the License key entered?

**Settings → License** (visible only on a Plus install or after entering a license key for the first time). Pasting a valid key flips `license_manager.is_plus = True` and unlocks Plus features immediately — no re-install.

## Where do I find pipeline parameters?

Open the pipeline → **Settings** (gear in editor toolbar) → **Parameters** tab. Define typed parameters that can be referenced from any node param via `${param.NAME}`.

## Where do I find pipeline templates / sample pipelines?

**Pipelines page → + New Pipeline** opens the template chooser. F-Pulse OSS ships **20 templates** covering the common starter shapes (simple ETL, dedup, aggregation, data quality, SCD2, API ingest with pagination, Slack alert on failure, etc.). Click any to start a pipeline pre-wired with that shape — you can edit it freely.

To start blank, click **Blank pipeline** at the bottom of the chooser.

Templates live under `data/templates/` (backend) + `frontend/src/templates/` (frontend). Drop new JSON files in those directories to add custom templates to the chooser. F-Pulse+ adds a **private template marketplace** — workspace-scoped templates shared across team members + parameter discovery for saved pipelines.

**Templates ≠ pipelines:** templates are starters; pipelines are your actual workflows. The Pipelines page lists your pipelines — the template chooser inside "+ New Pipeline" lists templates.
