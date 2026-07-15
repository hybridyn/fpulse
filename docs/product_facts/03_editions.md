# F-Pulse editions: OSS Free vs F-Pulse+

F-Pulse ships in two editions packaged as one binary. The OSS Free edition is fully featured for a single developer. F-Pulse+ adds team / enterprise governance.

## OSS Free (Apache 2.0)

The OSS edition is the full deterministic execution engine plus AI assistance. Includes:

- **Pipelines, projects, schedules, alerts, connections** — full CRUD, versioned IR, content-hash signing
- **40 node types** — sources, transforms (incl. SCD Type 2, Data Profile, Data Quality), sinks (incl. Bulk Loader for Postgres + Snowflake). NOTE: a small set of nodes are **Plus-only** — see `02_node_types.md` for the breakdown.
- **37 OSS connectors** — 4 database dialects (postgresql/mysql/mssql/sqlite via Database Source/Sink) + 2 bulk-load dialects (Postgres COPY, Snowflake PUT+COPY) + ~31 SaaS manifests. Cert matrix at `/api/connectors/cert-matrix` shows current depth scores: 8 v2 beta in validation, 27 v1 functional, 2 v1 basic. The Salesforce manifest in OSS is the starter version; the production-hardened enterprise Salesforce connector is Plus-only.
- **DEV environment** — pipelines run in DEV with sample-row limits on sources for fast iteration. **PROD environment + DEV→PROD promotion is F-Pulse+ only** — OSS Free has no PROD execution path through the legal contract (see edition-matrix.md).
- **Workflow versioning** with SHA-256 content hash for tamper detection
- **Credentials encrypted at rest** — Fernet (AES-128-CBC + HMAC-SHA256) with a master key file at `~/.fpulse/secret.key` (chmod 600, fail-closed on world-readable). Always-on for both Free and Plus from May 4 2026.
- **AI provider API keys encrypted at rest** — same Fernet encryptor wraps Anthropic / OpenAI / OpenRouter etc. keys
- **20 OSS pipeline templates** — simple ETL, dedup, aggregation, data-quality starters
- **Expression engine** — `$json`, `$now`, `$('Node')` references in node params
- **AI Copilot** with local Ollama (default) or cloud provider opt-in
- **Agent-tool RBAC** — 4 roles × 2 envs × 3 tiers gating what the LLM can call. (Distinct from workspace RBAC which is Plus-only.)
- **Inline AI helpers** — SQL helper, transform helper, diagnose-error, post-run summary, pre-run validate, anomaly detect
- **OpenRouter model browser** with free-tier filter and tools-only filter
- **Token wallet caps** — per-user daily, env-tunable
- **LLM call audit log** — every provider/model/tokens/latency entry
- **RAG** — local Ollama nomic-embed-text embeddings, sqlite-vec workspace-scoped store, `recall_history` tool, daily 03:00 UTC indexer
- **Eval harness** — 14 deterministic AI quality cases across 5 categories
- **Connector certification matrix** — depth scores per manifest, public via `/api/connectors/cert-matrix`
- **Trust posture endpoint** — `/api/trust/posture`, `/api/trust/eval-summary`
- **Insights** — Activity / Trust / Reports / AI Provider subtabs
- **Worker pool monitoring** — governor banner, spill-disk health, hardware presets, queue depth, throughput
- **Vertical scaling** — workers + DuckDB tuning + spill + global governor
- **Notifications** — in-app + email + Slack + Discord + webhook; long-running threshold + schedule-miss detection; browser desktop alerts
- **Manual backup** to S3 / Azure Blob / GCS
- **Plugins SDK + CLI**
- **Telemetry off by default**, opt-in only

## F-Pulse+ (commercial)

Adds team-scale governance and enterprise execution. Operators install the same Docker image and unlock with a license key. Plus features:

F-Pulse+ is a paid extension that adds team-oriented governance, enterprise connectors, multi-worker horizontal scaling, a workspace credential vault, SSO, audit-log retention, and richer notification / alerting / collaboration surfaces on top of the OSS engine. See [hybridyn.com/f-pulse](https://hybridyn.com/f-pulse) for the full Plus feature list and pricing.

## What's NOT in either edition

F-Pulse does not offer:

- **SaaS / hosted version** — F-Pulse is self-hosted only. There is no cloud-hosted F-Pulse to sign up for.
- **Multi-tenant isolation between separate organisations** — F-Pulse is single-tenant; one install serves one team.
- **AI fine-tuning** — the AI Copilot uses prompt-injection of context (Layer 1 + Layer 2 + tools) to know the product. No model fine-tuning is offered.
- **Distributed crash-resilient queue coordination** — single-node only in OSS.

## How to upgrade

Operators upgrade by entering a license key in **Settings → License**. The key flips `license_manager.is_plus = True` and the Plus features become available immediately. No re-install.

## What if I'm asked something Plus-only on a Free install?

The AI Copilot is aware of the edition (via the session context block) and won't suggest Plus features to a Free user. If a Free user asks about a team-governance capability, the assistant will explain that it is F-Pulse+ and link to the upgrade path.
