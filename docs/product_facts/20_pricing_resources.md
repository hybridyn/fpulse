# How much / Pricing / Resources — Q&A

Cost, sizing, and capacity-planning questions.

---

## Pricing

### How much does F-Pulse cost?

**OSS Free** — $0. Apache 2.0 license. Free forever. No usage caps,
no feature gating beyond the documented Plus boundary (`edition-matrix.md`).
Run on your laptop or a VPS at no cost.

**F-Pulse+** — commercial subscription, per-workspace per-month. Contact
sales (or the website) for current rates. Plus is positioned as
*production convenience* — the operator pays for governance, audit
retention, SSO, multi-user collaboration, enterprise connectors,
horizontal scaling.

### Is there a free trial of F-Pulse+?

Contact sales — historically Hybridyn Data Labs offers a 14-day or 30-day trial
on request. Configurable license keys.

### What does F-Pulse+ replace?

Per the positioning lock: the Plus pitch is **consolidation** of:
- ETL platform spend
- AI seats (Copilot / ChatGPT Team / Cursor) — F-Pulse Copilot subsumes pipeline-building AI use
- Cloud LLM API spend — local Ollama replaces cloud calls in many use cases
- Compliance review friction — the SOC 2 / DPA / sigstore-signed audit export bundle

Don't quote specific competitor dollar figures as universal — the
honest framing is "common combined spend categories", not "saves you $X
per month".

### Are there any usage limits?

**OSS Free**: none. Run as many pipelines, schedules, and runs as
the host can handle.

**F-Pulse+**: depends on the subscription tier. Pro / Enterprise tiers
differ on seat limits (per workspace), workspace count (per install),
and SLA response time (4h on Pro, 1h on Enterprise).

### How much does the AI Copilot cost to run?

**Local Ollama**: $0 in operating cost — runs on your CPU or GPU. The
only cost is the electricity to run inference (negligible at the
typical agent-call rate).

**Cloud LLM** (opt-in): pay-as-you-go to the provider. Per-call cost
is shown in the chat dock after each turn (`~240 tokens · ~$0.0016`).
Daily wallet caps prevent runaway costs.

### How much does OpenRouter free-tier cost?

$0 — OpenRouter routes to open-source models like Llama 3.1 hosted on
free-tier infrastructure. Some free-tier models support tool use; the
OpenRouter model browser in F-Pulse has a "free-tier" filter +
"tools-only" filter to find them.

### Why is local AI free?

Because Ollama is open-source software running on hardware you already
own. F-Pulse uses Ollama via its `/api/chat` and `/api/embeddings`
endpoints — no vendor in the middle. Your data never leaves the host.

---

## Resource sizing

### How much RAM do I need?

**Minimum for OSS Free**: 4 GB. F-Pulse backend ~500 MB, DuckDB working
set typically <2 GB for small pipelines.

**Recommended**: 8 GB to comfortably run F-Pulse + Ollama + a couple
of concurrent pipelines.

**For local AI**: add ~6 GB for `qwen2.5:7b` (the 2026-05-19 tool-use
floor, Q4_K_M quant). Total recommended for the local-first posture:
**12 GB RAM** on the host.

**Production VPS**: 16-32 GB. Reference deployment: Hetzner GEX44
(~$200/mo, 32 GB RAM + RTX 4000 SFF Ada 20 GB VRAM) is the recommended
solo-dev / small-team always-on host.

### How much CPU do I need?

**OSS Free**: any modern x86_64 / ARM64 CPU works. F-Pulse + DuckDB
saturate CPU during pipeline runs; agent inference saturates ~4 cores
during local LLM generation.

**Recommended**: 4+ cores so pipeline runs and agent inference don't
contend.

### How much disk?

- F-Pulse install (Docker image): ~500 MB
- Ollama runtime: ~150 MB
- `qwen2.5:7b` model: 4.7 GB (the 2026-05-19 tool-use floor)
- SQLite database: typically <100 MB
- Pipeline checkpoints (7-day TTL): 10-100 MB per active pipeline
- RAG vector store: ~10 MB

**Recommend 5 GB minimum, 20+ GB for active production with execution
history.**

### How much VRAM for GPU AI?

- `qwen2.5:7b` on GPU: 6 GB VRAM (RTX 3060 / 4060 / etc.) — instant tool-use
- `qwen2.5:14b` on GPU: 12 GB VRAM (RTX 3060 / 4060 Ti / etc.)
- `llama3.1:70b-q4` on GPU: 48 GB VRAM (RTX 4090 + offload, or A100
  / H100)

For the OSS-default local CPU use, no GPU is needed. The first-launch
banner recommends `qwen2.5:7b` (the 2026-05-19 tool-use floor) and
won't push GPU models on CPU hosts.

### How much network bandwidth?

**Default**: near-zero. F-Pulse has no outbound traffic in default
config.

**With cloud LLM**: typical agent turn is 1-3 KB up + 1-5 KB down per
LLM call. A typical busy day = a few MB total.

**Pipeline ingestion**: depends on the source. A SaaS API connector
pulling 1M rows/day might be 100 MB - 1 GB depending on per-row size.

### How long does a pipeline run typically take?

Highly variable by data shape. Anchors:
- Small CSV (10k rows) → CSV sink: <1 second.
- Database extraction (1M rows) → Bulk Loader: 10-30 seconds.
- API connector with pagination (100k rows) → schema mapper → DB sink:
  1-5 minutes (most time is API rate limit waiting).
- 100M-row hash join: 30 seconds to 5 minutes depending on RAM.

Track your specific pipelines via `list_executions` aggregation; flag
runs >2× the median as outliers.

### How many pipelines can OSS Free handle?

Comfortably: **100-1000 pipelines** on a typical VPS. Beyond that,
SQLite write contention starts to bite and Plus's containerized worker
pool + optional Postgres becomes the right answer.

The hard limits are:
- SQLite WAL: ~thousands of writes/second peak
- DuckDB memory limit: 80% of host RAM by default
- Single-process concurrency: capped via `FPULSE_MAX_CONCURRENT_RUNS`

### How many concurrent pipeline runs?

Default cap: detected CPU cores. Override via `FPULSE_MAX_CONCURRENT_RUNS=8`
(or whatever fits your host). Settings → General → Execution Tuning
shows the live value.

Plus customers running horizontal scaling get unlimited concurrency
across the worker pool (one pipeline per worker, scale workers
horizontally).

### How fast is the chat fast lane?

Typically **5-50 ms** per query. No LLM in the path; just Python
function calls hitting the workspace state. The "list pipelines"
intent does one tool call (`list_pipelines`) which queries SQLite
once.

### How fast is local Ollama AI?

**`qwen2.5:7b` on CPU** (the 2026-05-19 tool-use floor): 3-6 tokens/second
generation. A typical tool-using turn is 2 LLM calls × ~200 tokens output
= 30-60 seconds per turn. End-to-end agentic flow: 1-5 minutes.

**`qwen2.5:14b` on a 12 GB GPU**: 30-50 tokens/second. ~3-5 seconds
per turn.

**`llama3.1:70b-q4` on a 48 GB GPU**: 40-70 tokens/second. ~1-3
seconds per turn.

### How fast is cloud AI?

Depends on the provider:
- Anthropic Claude Sonnet: 1-3 seconds per tool-using turn
- OpenAI GPT-4o-mini: <1 second per turn
- Groq (Llama 3.3 70B): <1 second per turn
- OpenRouter free-tier: 5-20 seconds per turn (usually rate-limited)

The agent dock shows live latency for transparency.

---

## Capacity planning

### When should I switch from OSS Free to F-Pulse+?

Per `15_workers_scaling.md`, the trigger conditions are:
- More than 1 user needs to edit pipelines
- You need DEV → PROD promotion with approval gates
- You're in a compliance scope requiring audit log retention
- You need SSO / SAML / OIDC integration
- You need vault-backed credentials (HashiCorp / AWS / Azure / GCP)
- You need horizontal scaling beyond a single host
- Connector requirements include enterprise sources (SAP, NetSuite,
  Workday, etc.)

If none of these apply, OSS Free is the right answer.

### When do I need a GPU?

Only for the AI Copilot, only if you want sub-3-second tool-using
turns. The pipeline execution engine is CPU-only — a GPU does
nothing for it.

If you're happy with 30-180 s tool-using turns on CPU (using the fast
lane for common questions), you don't need a GPU.

### How big does the SQLite database get?

Typical OSS install: **10-100 MB**. The biggest contributors are:
- Execution history (a few KB per run × runs/day × retention days)
- Audit log
- RAG vector store (~10 MB at OSS scale)

Workspaces with hundreds of pipelines and weeks of execution history
can hit 500 MB - 1 GB. SQLite handles this fine.

### How long do executions stay in the database?

OSS Free: indefinitely. No retention enforcement. Operators delete
manually if needed (the Executions page has a bulk-delete tool).

F-Pulse+: configurable per-store retention with archive-to-Parquet
on S3/GCS before deletion.

### How big can a single pipeline run be?

Limited by host RAM (DuckDB processes everything in memory; spill to
disk is slower but unbounded). A pipeline processing 100M rows fits
comfortably in 32 GB RAM with bulk-load sinks. 1B rows works on a
production server with 128 GB.

For larger-than-RAM data: ensure the spill directory is on SSD (HDD
spill is 10-100× slower) and consider partitioning the pipeline into
smaller batches.

### How long can a pipeline run?

There's no hard cap — long-running pipelines are bounded only by
host availability. The notification watchdog flags runs exceeding
the configured threshold (default 30 minutes) so the operator knows.

For interactive/agentic-driven pipelines (e.g. AI Copilot triggering
a pipeline run on user request), the agent's own wall-clock cap is
300 s for local Ollama / 120 s for cloud — that's separate from the
pipeline run itself.

---

## Cost optimization

### How do I reduce my AI costs?

1. Use the **fast lane** for common questions (list/overview/failures/
   running) — sub-1 s, no LLM cost.
2. Configure **local Ollama** as the default; cloud providers are
   opt-in only.
3. Set the **per-user daily token cap** (`FPULSE_AGENT_DAILY_TOKENS_USER`)
   to a budget you're comfortable with.
4. Use the **OpenRouter free-tier filter** to find $0/turn models
   for cloud workloads.
5. Use the **Stop button** to abort long runs you decide aren't
   worth waiting for.

### How do I reduce pipeline runtime?

See `18_analytical_questions.md` → "Why is pipeline X slow on a large
source?" — the four common causes are row-by-row INSERT, hash spill
on HDD, too-strict memory limit, and I/O-bound (no parallelism gain).

Replacing `Database Sink` with `Bulk Loader` is the highest-leverage
single change for >10k-row pipelines.

### How do I reduce hosting cost?

The reference low-cost deployment is **Hetzner GEX44 — ~$200/mo** for
a 32 GB RAM + RTX 4000 SFF Ada 20 GB VRAM dedicated server. Runs
F-Pulse + Ollama (with `qwen2.5:14b` on the GPU for sub-3-second
tool-use) + 100s of active pipelines comfortably.

For laptop / cheap VPS deployment without GPU, plan for the
30-180 s/turn local CPU AI latency and use the fast lane aggressively.
