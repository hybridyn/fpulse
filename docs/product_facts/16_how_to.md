# How do I…? — F-Pulse step-by-step reference

Each entry below is a self-contained Q&A pair. RAG retrieval scores best
when chunk text matches the question shape, so this file is structured
explicitly as questions the operator types into the Copilot.

**Convention:** every answer is a numbered step list using the EXACT UI
labels the user sees. No invented terminology. If a step doesn't exist
in the OSS Free build, it is marked **(Plus only)**.

---

## How do I add a pipeline?

1. Click **Pipelines** in the left sidebar (or press `g` then `p` from anywhere).
2. Click the **+ New Pipeline** button at the top-right of the Pipelines page.
3. Optionally pick a starter template (20 templates ship in OSS — simple ETL, dedup, aggregation, data quality, etc.). To start blank, click **Blank pipeline**.
4. The editor opens. The empty canvas shows a **HeroPromptCard** prompting "describe what you want to build".
5. **Either** type a description like *"Read sales.csv, dedupe on order_id, write to Parquet"* and press Enter — the Copilot drafts a pipeline. **Or** drag node types from the left palette onto the canvas and connect them by hand.
6. Configure each node by clicking it (opens **ConfigPanel** on the right with that node's params).
7. Save with `Ctrl+S` / `Cmd+S` or by clicking **Save** in the top toolbar. Auto-save fires 2 seconds after the last edit if **Auto-save canvas** is enabled in Settings → General → Editor Preferences (default ON).

The **Dashboard page** (sidebar → first entry) is the home / landing page — greeting, hero KPIs, system usage, recent-activity feeds. The **Pipelines page** is where you list, edit, run, and manage individual pipelines. They are two different pages.

## How do I upload a data file to F-Pulse?

1. Click **Storage** in the left sidebar (stacked-cylinders icon). URL: `#storage`.
2. Click **+ Upload file** at the top right of the page.
3. The upload dialog opens:
   - **Scope**: pick **Global** (visible to every project) or **Project** (scoped to one project).
   - If Project: pick from the **Project** dropdown (defaults to your active project context if one is set).
   - Optional: pick a **Folder** under that project (1-level deep).
   - Optional: add a short **Description**.
   - Drag a file into the drop zone, or click to browse.
4. Click **Upload**. Toast confirms the landing spot — e.g. *"Uploaded orders.csv to Sales / Q1 2026"*.
5. The file appears in the **Files** tab with its scope chip, size, and "Used by 0 pipelines" until something references it.

Allowed formats: CSV, TSV, JSON, NDJSON, Parquet, Excel (.xlsx / .xls), XML. Max size 100 MB by default (override via `FPULSE_UPLOAD_MAX_MB`).

**Storage holds data files only.** Pipeline definitions (`*.json` workflow exports) belong under **Workflows → Import**, not Storage. If you accidentally upload a pipeline JSON, the preview banner detects it and offers **Open in Editor** as a one-click recovery.

## How do I promote a file to a managed table?

A managed table is a Parquet-backed table that pipelines can read by `schema.name` (no file paths). Promote any uploaded file to one:

1. **Storage** page → **Files** tab.
2. Click the **Promote** icon on the file's row (the small cylinders icon, green hover).
3. The dialog asks:
   - **Schema** — pick an existing one (e.g. `default`, `sales`) or click **+ New schema** to create one.
   - **Table name** — auto-suggested from the filename (lowercase, snake-cased). Edit if you want.
   - **Description** — optional note shown on the table row.
   - **Column renames** — optional `old:new, old2:new2` pairs to clean up vendor-stamped column names.
4. Click **Promote to table**. Toast: *"Created sales.orders (1,250 rows)"*. The page switches to the **Managed Tables** tab so you see your new table.
5. The table lives at `$FPULSE_DATA_DIR/tables/{workspace_id}/{schema}/{name}/part-000.parquet`.

To use the table in a pipeline: drag a **Source** node, set `connector_type='local_table'`, set `schema_name + table_name`. Or use the dedicated **Managed Table Source** node from the palette. Same shape for writes via **Managed Table Sink** (modes: replace / append / merge).

## How do I clean a file (or source dataset) that has many issues?

Use **Clean & promote** instead of plain Promote.

**From an uploaded file (Storage):**

1. **Storage** → **Files** tab → click the **wand icon** (amber hover) on the file's row.
2. F-Pulse scaffolds a 3-node pipeline in the Editor: `Source → Data Wrangler → Managed Table Sink`. The Wrangler starts empty.
3. Add cleanup sub-steps to the Wrangler — `filter`, `select`, `rename`, `cast`, `derive` (`TRIM`, `COALESCE`, `LOWER`, `REGEXP_REPLACE`), `dedupe`, `group_by`, `sort`, `sample`, `flatten`. All compile to a single DuckDB query.
4. Click **Run**. The cleaned rows land in the managed table named by the sink (default: `default.<filename>`). Original file is untouched.

**From a database / SaaS source (Connections):**

There is no one-click wand on the Connections page. Build the same shape manually in the **Editor**:

1. Drag a **Source** node and pick the saved connection. Set its stream / table / query.
2. Drag a **Data Wrangler** node and connect it to the source.
3. Drag a **Managed Table Sink** node, set `schema_name + table_name`, connect it to the Wrangler.
4. Save and run.

The 4 clicks-add up the same shape the file-side wand emits. Backend endpoint for the file path: `POST /api/storage/scaffold-cleanup` — returns a workflow JSON; the frontend stashes it in `sessionStorage['fpulse_pending_import']` and navigates to `#editor` where the canvas renders it.

## How do I see which pipelines reference a file or managed table?

1. Open **Storage**. Each row on Files / Managed Tables has a **"Used by"** column.
2. Rows with references show a blue pill like **"Used by 3"**. Rows with zero references show an em-dash.
3. Click the pill. A popover opens listing every referencing pipeline with name + **Open →** link.
4. The scanner detects four reference shapes:
   - `local_table_source` / `local_table_sink` steps matching `schema.name`
   - Generic `source` / `destination` nodes with `connector_type='local_table'`
   - File-path-based sources matching `storage_object.path`
   - Promote-to-table provenance — a file that seeded a managed table inherits the table's pipeline list (so deleting the original file warns about downstream damage)

Destructive actions (Delete file, Drop table, Replace bytes) all surface the usage list before proceeding so a downstream pipeline doesn't break silently.

## How do I replace the bytes of an existing uploaded file?

1. **Storage** → **Files** tab.
2. Click the **Replace** icon on the file's row (upload-arrow icon, indigo hover).
3. Pick the new file in the browser dialog. Extension must match the original — switching `.csv` to `.parquet` would break downstream pipelines silently.
4. Toast confirms: *"Replaced orders.csv"*. Downstream pipelines see the new bytes on their next run; no `connection_id` or `object_id` changes.
5. If the file is referenced by N pipelines, the action shows a confirm first listing them.

## How do I see what pipeline templates / sample pipelines are available?

1. Click **Pipelines** in the left sidebar.
2. Click **+ New Pipeline** at the top-right.
3. The template chooser opens with all 20 OSS templates: simple ETL, dedup, aggregation, data quality + DLQ, SCD Type 2 dimension, API ingest with pagination, profile a source, daily aggregation with schedule, Slack alert on failure, database replication via Bulk Loader, and more.
4. Click any template to spawn a new pipeline pre-wired with that shape (edit freely from there). Click **Blank pipeline** at the bottom to start empty.

**Templates are different from pipelines:** templates are starters, pipelines are your actual workflows. The Pipelines page lists your pipelines; the template chooser inside "+ New Pipeline" lists templates. Plus adds a private template marketplace + parameter discovery on saved pipelines.

## How do I run a pipeline?

1. Open the pipeline (Pipelines page → click the row).
2. In the editor, click **Run** in the top toolbar. The PreRunBanner appears with a 4-mode safety picker: **Live** / **Sample** / **Dry-run** / **Validate-only**. Default is set in Settings → General → Default Run Behavior.
3. Pick a mode and click **Run**.
4. Watch live progress in the **Executions panel** at the bottom of the editor (or open Executions page for the full history).

To run from outside the editor: Pipelines page → row → **⋯** menu → **Run now**.

## How do I schedule a pipeline?

1. Open the pipeline.
2. Click **Settings** (gear icon in the editor toolbar) → **Schedule** tab.
3. Pick a schedule type: `cron` (full cron expression), `daily` (HH:MM), `hourly`, `interval` (every N minutes), `once`.
4. Set **Environment** — DEV or PROD (PROD is **F-Pulse+ only**; in OSS Free schedules fire in DEV).
5. **Save**. The schedule appears on the Schedules page.

The scheduler polls every 30 s; expect first fire within 30 s of the configured time.

## How do I add a credential (database password / API key)?

1. **Credentials** page in the sidebar (or inside a project: project → **Credentials** tab for project-scoped credentials).
2. Click **+ New Credential**.
3. **Name** — human-readable label, e.g. `prod-postgres`.
4. **Type** — `postgresql`, `mysql`, `mssql`, `sqlite`, `api_key`, `oauth2`, `bearer_token`, `ssh_key`.
5. **Config** — type-specific fields (host/port/database/user/password for databases; key + header for API keys; client_id/secret/scopes for OAuth).
6. **Save**. F-Pulse encrypts the secret at rest using **Fernet (AES-128-CBC + HMAC-SHA256)** with the master key at `~/.fpulse/secret.key`. The frontend never sees plaintext after save.

To use the credential: drop a Database Source / Sink or REST Connector node and pick the credential by name from the dropdown.

**External vaults (HashiCorp Vault, AWS Secrets Manager, Azure Key Vault) are F-Pulse+ only** via the Vault-Ref pattern. OSS Free uses the built-in Credentials page exclusively.

## How do I rotate a credential?

1. **Credentials** page → click the credential row → **Edit**.
2. Replace the secret value (other fields can be left as-is).
3. **Save**. F-Pulse re-encrypts. Pipelines pick up the new value on the next run — no pipeline edits needed.

## How do I add a database connection?

1. **Connections** page in the sidebar.
2. **+ New Connection**.
3. **Name** + **Type** (postgresql / mysql / mssql / sqlite).
4. Fill the connection config OR click **Use existing credential** to reference one from the Credentials page.
5. Click **Test Connection** to verify reachability before saving.
6. **Save**.

Pipelines reference connections by ID via the Database Source / Database Sink / Bulk Loader nodes.

## How do I configure the AI Copilot?

1. **Insights → AI Provider** (or click the **Configure »** chip in the chat dock header).
2. Choose a provider:
   - **Local** (recommended): pick **Ollama**, set the URL if Ollama isn't on `localhost:11434`, click **Save**. F-Pulse auto-detects the recommended model `qwen2.5:7b` (the 2026-05-19 tool-use floor). If not installed — or if your current Ollama install is on a sub-floor model like `qwen2.5:1.5b` / `:3b` — the **first-launch banner** at the top of the screen offers a one-click upgrade.
   - **Cloud opt-in**: pick Anthropic / OpenAI / OpenRouter / Gemini / DeepSeek / Groq / Mistral / Azure → enter API key → **Test connection** → **Save**. **Cloud means prompts and tool inputs leave the host** — you're consenting to that explicitly.
3. The dock header shows the active provider + model + tool count + daily token cap (% used).

## How do I switch AI models?

1. **Insights → AI Provider** → change **Model** dropdown → **Save**.
2. For Ollama: the dropdown lists every model your local Ollama daemon knows about. Use the **+ Pull model** input to download a new one (e.g. `qwen2.5:14b` if you have GPU/RAM).

## How do I set up an alert?

1. Open the pipeline → **Alerts** tab in the editor.
2. **+ New Alert Rule**.
3. **Condition** — `ON_FAILURE`, `ON_LONG_RUNNING`, `ON_SCHEDULE_MISS`.
4. **Channel** — in-app / email / Slack / Discord / generic webhook.
5. Channel-specific config (Slack webhook URL, etc. — pulled from the Notifications page).
6. **Save**.

The watchdog polls at the schedule cadence; expect first alert fire within ~30 s of the trigger.

**Plus-only alert features**: quiet hours, debounce, daily digest, escalation. Free fires every alert immediately to the configured channel.

## How do I configure email / Slack / Discord / webhook for notifications?

1. **Settings → Notifications** tab.
2. Pick the channel section (Email SMTP, Slack, Discord, Webhook).
3. Enter the channel-specific config (SMTP host/port/from for email; webhook URL for Slack/Discord/generic).
4. Click **Test** to verify.
5. **Save**.

Then any alert rule pointing at that channel will use the configured destination.

## How do I see what failed today / recent failures?

**Two paths**:
- **Chat fast lane**: type `what failed today` in the Copilot — instant answer, no LLM round-trip.
- **UI**: Executions page → filter **Status = error** → sort by Started At desc.

Each failed row shows `error_message`, `peak_memory_mb`, `cpu_seconds`, and a **Resume from step** button if the failure was mid-pipeline.

## How do I resume a failed pipeline run?

1. Executions page → click the failed run row.
2. **Resume from step X** button appears alongside **Re-run from start**. The "step X" is the first step that didn't complete successfully.
3. Click **Resume from step X**. F-Pulse loads each successful step's Parquet snapshot from `data/checkpoints/<run_id>/<step_id>.parquet` and re-executes from the failure point.

Checkpoints have a 7-day TTL by default — older runs can only be re-run from start.

## How do I back up F-Pulse?

**Manual** (OSS Free):
1. **Settings → Backup**.
2. **+ New backup destination** → pick local filesystem / S3 / Azure / GCS, configure auth via the Credentials page.
3. Click **Create backup now**.

**Automated** (recommended for production):
- Set a host-level cron that tarballs the data volume daily. Example in `docs/product_facts/13_backup_recovery.md`.
- **Always back up `~/.fpulse/secret.key` separately** — without it, encrypted credentials are unrecoverable.

**Plus** adds scheduled backups + retention policy + Parquet archive in the UI.

## How do I install F-Pulse?

1. Clone the repo: `git clone https://github.com/hybridyn/fpulse`
2. `cd fpulse`
3. Pick one:
   - **Docker (recommended)**: `docker compose up`
   - **From source — one command**: `pip install -e . && fpulse open` (starts the backend on a free port + opens your default browser; falls back to a printed URL in WSL/Docker/SSH)
   - **From source — manual**: see `docs/quickstart.md` for the backend (uvicorn) + frontend (vite) startup
4. Docker mode: open `http://127.0.0.1:8001` in a browser. Source mode (`fpulse open`): your browser opens automatically.
5. First-run wizard sets the admin password and creates the master encryption key at `~/.fpulse/secret.key`.

Backend binds to `127.0.0.1` (loopback) by default — the API is **invisible to your LAN**, so coworkers / hotel WiFi / conference networks cannot reach it. If you need LAN-visible binding for on-prem multi-user installs, set `FPULSE_ALLOW_LAN=1` or pass `--host 0.0.0.0`. See [`install/security-hardening.md`](../install/security-hardening.md).

## How do I upgrade F-Pulse?

1. `git pull` to get the latest tag.
2. Read `changelog.md`, especially the **Tested with** matrix.
3. **Update the pinned image tag** in `docker-compose.yml` (or `.env`): `FPULSE_IMAGE_TAG=1.0.0`.
4. `docker compose pull fpulse && docker compose up -d fpulse`.
5. Diff `.env.example` for any new variables your config might need.

## How do I switch DEV → PROD?

**OSS Free**: only DEV exists per the legal contract. The env-stripe at the top of the app may show a PROD toggle, but the DEV→PROD promotion workflow is **F-Pulse+ only**.

**F-Pulse+**:
1. Open the pipeline.
2. **Promote** button in the editor toolbar.
3. **Gate 1**: developer requests promotion. Reviewer is notified.
4. F-Pulse runs the pipeline in **Sandbox** (scratch namespace, real upstream data, no production writes).
5. **Gate 2**: approver reviews the Sandbox output and approves.
6. PROD activation happens automatically after Gate 2.

## How do I delete a pipeline?

Pipelines page → row → **⋯** menu → **Delete**. The pipeline IR + execution history is removed from SQLite. **There is no recycle bin** — restore from a backup if you need to recover.

## How do I share a pipeline with a teammate?

**OSS Free is single-user.** No sharing model exists.

**F-Pulse+** adds workspace-scoped sharing — every workspace member sees every pipeline; permissions follow the workspace RBAC roles (Super Admin / Workspace Admin / Data Engineer / Analyst / Viewer).

## How do I see what the AI Copilot can do?

Type `help` or `what can you do` in the chat — instant answer, no LLM round-trip. Lists the fast-lane intents and explains when the agent will use the LLM (analytical questions: "why did X fail", "compare A and B").

## How do I cancel a slow chat response?

The Copilot dock shows a **red Stop button** while a turn is in flight (replaces the Send button). Click it to abort. Any partial text already streamed in is preserved with a `_Stopped by user._` marker.

## How do I reindex the AI's product knowledge?

You only need this after editing `docs/product_facts/*.md`:
- **UI**: Settings → Security → **AI product knowledge** card → **Reindex now** button (admin only).
- **API**: `POST /api/ai/product-knowledge/reindex` with an admin token.
- **Or** restart the backend — the indexer runs at startup.

## How do I view audit logs?

**OSS Free**: Insights → **Activity** subtab. Combined view of audit + agent traces + execution history. Best-effort retention (no enforced policy).

**F-Pulse+**: same view + retention policy + sigstore-signed export at `POST /api/plus/audit/export`.

## How do I set credentials encryption / which algorithm?

You don't configure it — it's always-on. **Fernet (AES-128-CBC + HMAC-SHA256)**, master key at `~/.fpulse/secret.key`, chmod 600 on POSIX, fail-closed on world-readable. F-Pulse refuses to start if the key file is misconfigured.

To rotate the master key, see `docs/product_facts/13_backup_recovery.md` — it requires re-saving every credential after rotation.

## How do I migrate from a pre-1.0 OSS install (plaintext credentials)?

```bash
python -m fpulse.security.migrate_existing
```

This walks every row in `credentials`, `user_ai_config`, `workspace_ai_config` and re-encrypts. Idempotent. Takes a JSONL backup snapshot first under `data/migration_backups/`.

`--dry-run` to report without writing. Read `backend/fpulse/security/migrate_existing.py` for the full safety contract.

## How do I write my own connector?

1. Create a manifest under `backend/fpulse/connectors/manifests/<your_connector>.v2.json` following the F0.1 schema.
2. Run the validator: `python -m fpulse.connectors.certify <your_connector>` to see your depth score (0-5). The goal is 5 for production-grade.
3. The agent picks up the new manifest at next backend restart (or after `POST /api/saas/manifests/reload`).

OSS connectors stay OSS — Hybridyn Data Labs' commercial connectors are separately maintained and not affected by community contributions.

## How do I disable telemetry?

**It's already off by default in OSS.** No action needed.

To verify: `GET /api/trust/posture` — the `sovereignty.telemetry_currently_enabled` field shows the live state. Settings → Security → Privacy lets an operator opt in (informational only — the sender is not active).
