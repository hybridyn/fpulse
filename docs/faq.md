# FAQ

## General

**Q: Is F-Pulse really free?**
Yes. F-Pulse OSS is Apache 2.0. No telemetry, no usage caps, no feature gating. Fork it, ship it, sell it (subject to the trademark policy in `trademark.md` if you redistribute under the F-Pulse name).

**Q: What kind of pipelines is F-Pulse for?**
F-Pulse is **visual-first** — you build pipelines on a canvas, not in Python. SQL-first transforms via DuckDB run in-process. The execution model is single-node by design, which keeps debugging simple and inspection immediate.

## Installation

**Q: What are the system requirements?**
Python 3.11+, Node 20+ (only for building from source). 4 GB RAM minimum without the AI assistant. With the AI assistant on `qwen2.5:7b` (the local tool-use floor) plan for 8 GB total — the model itself needs ~6 GB at Q4_K_M. Linux, macOS, and Windows are all supported.

**Q: Can I run F-Pulse on a Raspberry Pi?**
Yes for small workloads (under 1 GB pipelines, no AI assistant). The 64-bit Raspberry Pi OS works; pin `FPULSE_DUCKDB_MEMORY_LIMIT=512MB`.

**Q: Do I need Docker?**
No. Docker is convenient but optional — the single-binary path (`python -m fpulse serve`, or equivalently `fpulse serve` after `pip install -e .`) works fine.

## Pipelines

**Q: How big a dataset can F-Pulse handle?**
With proper tuning, large workloads run comfortably on a single tuned host. See the [scaling guide](scaling.md) for the volume-tier table.

**Q: Can I run multiple pipelines in parallel?**
Yes — set `FPULSE_MAX_CONCURRENT_RUNS=N`. Default is 4. Each pipeline gets its own DuckDB connection.

**Q: What happens if a pipeline fails mid-run?**
The execution is marked `failed` with a step-level traceback. Downstream steps don't run. Today you re-run the whole pipeline; richer recovery is an area of ongoing work.

**Q: Can pipelines depend on each other?**
Yes — use the **Execute Pipeline** node to invoke a sub-pipeline as a step. You can also express dependencies in the scheduler: pipeline B's schedule can list pipeline A as a dependency.

**Q: I edited a published pipeline but nothing saved. Why?**
Auto-save is intentionally suspended on published pipelines so live scheduled runs can't be broken by exploratory edits. Click **Save** in the editor to commit changes as a new draft (the published version stays running until you re-publish). The violet banner above the editor reminds you.

## Connectors

**Q: Why does this connector show a "BETA" badge?**
The connector works but has known gaps — typically partial pagination, basic INSERT instead of bulk-load, or incomplete test fixtures. The first time you click a Beta connector, F-Pulse shows a one-time dialog listing what to expect. See [connectors](connectors.md) for status definitions.

**Q: My favorite SaaS isn't listed. How do I connect to it?**
Use the **Generic Source / Destination** node — it wraps any REST API or JDBC database as a connector. Long-term, contribute a manifest at `connectors/<name>/manifest.yaml` and open a pull request.

**Q: How do I pick between connectors marked Certified and Beta?**
Certified connectors have production-grade auth, read, pagination, sink, and error handling. Beta connectors are usable for prototyping; verify behavior at smaller volumes before relying on them in scheduled production runs.

**Q: What's the difference between `oracle_api`, `oracle_fusion`, and `oracle_bip`?**
`oracle_api` is the legacy name (kept for back-compat) — it resolves to `oracle_fusion` automatically on load. New connections should pick **Oracle Fusion Cloud** (`oracle_fusion`) for the REST API or **Oracle BI Publisher** (`oracle_bip`) for report runs. They're different protocols against different Oracle products; lumping them under one name was a 2026-05-23 mis-step we corrected.

**Q: Same question for `sap` vs `sap_s4hana` vs `sap_successfactors`?**
Same answer. Legacy `sap` resolves to `sap_s4hana` (S/4HANA OData v2 / v4 with `sap_client` routing). For SuccessFactors HRIS pick `sap_successfactors` — it has its own login format (`<user>@<company_id>`) and pre-set HRIS entity catalog.

## Storage

**Q: Where do my uploaded files actually live on disk?**
Under `$FPULSE_DATA_DIR/uploads/{workspace_id}/{stored_filename}`. Managed Parquet tables live under `$FPULSE_DATA_DIR/tables/{ws}/{schema}/{name}/part-*.parquet`. Pipeline outputs under `$FPULSE_DATA_DIR/outputs/{ws}/{pipeline_id}/{run_id}/`. The **Storage** page is the indexed browse view of all of this.

**Q: I uploaded a pipeline JSON to Storage by mistake. What happens?**
Preview detects F-Pulse pipeline shape (`steps` array + `name` field) and shows an amber banner with **Open in Editor**. One click hands the bytes off to the Workflows import flow. The file stays in Storage; the banner just offers the recovery path. Long-term, pipeline definitions belong under **Workflows → Import**, not Storage.

**Q: Can I rename or version a managed table?**
v1.0 doesn't support rename or version history on managed tables. Drop + re-promote is the workaround. Time-travel + MERGE INTO with statistics-based skip arrives with Plus's Delta Lake managed tables (post v1.0 GA).

**Q: How do I know if it's safe to delete a file or drop a table?**
The Storage page's **Used by** column shows a "Used by N pipelines" pill per row. Click it to see the list. Destructive actions (Delete, Drop, Replace) all surface the usage list before proceeding so a downstream pipeline doesn't break silently.

## AI

**Q: Does F-Pulse send my data to OpenAI / Anthropic?**
Only if you configure a cloud provider. The default is **local-only** via Ollama — your data never leaves your machine. See [AI guide](ai.md).

**Q: The AI assistant times out. Why?**
You're probably running a model heavier than the local tool-use floor on a CPU host. The floor is `qwen2.5:7b` (~6 GB RAM, 30–60 s per turn on CPU); anything bigger needs a discrete GPU. Open Insights → AI Provider → click **Pull** next to `qwen2.5:7b` (or pick a cloud provider for sub-second responses).

**Q: The AI assistant returns greetings instead of doing anything. Why?**
You're on a model below the tool-use floor (e.g. `qwen2.5:1.5b` / `:3b`). Small Qwen models advertise tool schemas but silently reply with chat text instead of calling tools. The first-launch banner catches this and offers a one-click upgrade to `qwen2.5:7b`. Manual fix: Insights → AI Provider → pull `qwen2.5:7b`.

**Q: Can I use my own API key for cloud models?**
Yes — every cloud provider in **Insights → AI Provider** uses **bring-your-own-key**. F-Pulse never proxies your tokens through Hybridyn Data Labs.

**Q: What does "Tools" / "Text only" mean on the model dropdown?**
Tool-trained models (Llama 3.1+, Qwen 2.5, Mistral Nemo, Firefunction, Command-R) can call the agent's tools. Text-only models (phi3, mistral, codellama) chat but can't take actions. The dropdown labels each.

## Operations

**Q: Where does F-Pulse store data?**
SQLite database at `$FPULSE_DATA_DIR/fpulse.db` (default `./data/fpulse.db`). Uploads live in `$FPULSE_DATA_DIR/uploads/`. DuckDB spill in `$FPULSE_DATA_DIR/duckdb_spill/`.

**Q: How do I back up F-Pulse?**
Local snapshots happen automatically (Settings → Storage → Backups). For off-site backup, configure S3, Azure Blob, or GCS in **Settings → Backups** — the rotation policy is configurable.

**Q: Can I run F-Pulse behind a reverse proxy?**
Yes. See the [Deployment runbook](deployment.md) for the canonical install and upgrade flow. Reverse-proxy templates for TLS termination, security headers, and rate limiting are documented there.

**Q: How do I scale beyond one machine?**
F-Pulse OSS is single-node by design. F-Pulse+ is a paid extension for teams; see [hybridyn.com/f-pulse](https://hybridyn.com/f-pulse) for details.

**Q: How do I prevent duplicate emails / webhook posts / Slack pings on pipeline retries?**
Set `idempotency_key` on the sink — see [`idempotency.md`](idempotency.md). F-Pulse hashes the rendered key per row and skips rows that were already sent within the TTL (default 30 days).

## Privacy and trust

**Q: Does F-Pulse phone home?**
**No telemetry, no analytics, no usage tracking** — confirmed in the [trust posture](TRUST.md). The only outbound traffic is to providers you configure (database connections you create, AI provider API calls if you set one up, etc.).

**Q: Where are my credentials stored?**
Encrypted on disk in the credentials store. F-Pulse uses **Fernet (AES-128-CBC + HMAC-SHA256)** — randomly generated 32-byte master key, no password-derivation step. The key file lives at `<FPULSE_DATA_DIR>/secret.key` (Docker: `/data/secret.key`; source install: wherever you point `FPULSE_DATA_DIR`), or at a custom path set via `FPULSE_MASTER_KEY_FILE`. POSIX permissions are enforced to mode `0600` on startup — the server fails closed if the perms are wider.

**Q: I found a security vulnerability. Where do I report it?**
See the `security.md` file at the repository root for the responsible-disclosure policy. Please do **not** open a public GitHub issue.

## Migration and portability

**Q: Can I move my pipelines between F-Pulse installs?**
Yes — every pipeline exports to a portable zip bundle from **Pipelines → Export**. Import the same bundle on another F-Pulse install to recreate the pipeline, its nodes, and its configuration.

**Q: What format are pipelines stored in?**
An engine-agnostic JSON IR (intermediate representation) with a versioned schema. The IR is what's persisted, exported, executed, and audited. The canvas is just one view of it.

## Contributing

**Q: How do I contribute a connector?**
See the `contributing.md` file at the repository root for the contribution flow. Each connector ships as a manifest under `connectors/<name>/manifest.yaml`.

**Q: What's a CLA and why do I need to sign it?**
The Contributor License Agreement grants Hybridyn Data Labs the right to relicense your contribution. This is the standard open-core pattern; without it we can't accept code.
