# F-Pulse AI Copilot — what's in OSS, what's Plus

Per `edition-matrix.md` line 89-107, the agentic Copilot ships in OSS. Plus adds organizational guardrails.

## OSS Free includes

### The dock + chat

**Copilot dock** — resizable, page-aware floating chat panel. Slash commands (`/help`, `/explain`, `/diagnose`). Lives bottom-right, collapsible to a small button.

**Three-layer knowledge architecture** (May 4 2026):
- **Layer 1 — session context block** — every turn carries who the user is, role, environment, edition, current page, workspace counts, what they can/can't do. Cap ~600 tokens; never dropped under budget pressure.
- **Layer 2 — product knowledge RAG** — curated `docs/product_facts/*.md` chunks retrieved per turn. Auto-indexed at startup; admin reindex via `/api/ai/product-knowledge/reindex`.
- **Layer 3 — live workspace state** — the agent's tools (see below) fetch real workspace data when the LLM needs it.

### The fast lane (sub-1 s answers without LLM)

11+ rule-based intents that bypass the LLM entirely for the most-common questions:

- **Greetings** — "hi" / "hello"
- **Help** — "what can you do" / "help"
- **Product info** — "what is f-pulse" / "what tier am I on" / "what's my role"
- **Workspace state** — "give me an overview" / "list pipelines" / "list projects" / "list schedules" / "list alerts" / "list connections"
- **Recent activity** — "what failed today" / "what's running now"
- **Catalog** — "what node types are supported"

The fast lane uses a reasoning gate — prompts containing "why", "explain", "compare", "diagnose", "should I", "walk me through" fall through to the LLM. So "list pipelines" is instant; "why did pipeline X fail" goes to the LLM.

### The agent loop

Bounded tool-use loop with hard caps:
- **MAX_ITERATIONS = 6** — hard ceiling on tool-call cycles per run.
- **Wall-clock cap** — 300 s for local Ollama, 120 s for cloud providers (provider-aware since May 4 2026). Override via `FPULSE_AGENT_WALL_CLOCK_S`, clamped [10, 600].
- **Stop button** — user-cancellable mid-flight.

### The agent tools (canonical count: code in `backend/fpulse/ai/tools/`)

The actual tool count derived from the codebase is **25 tools** organised in three tiers. The SOURCE OF TRUTH is the registry in `backend/fpulse/ai/tools/`. The breakdown:

**21 READ tier** (permissive RBAC, no confirmation, standard audit):

- `workspace_overview` — top-level workspace counts
- `list_pipelines` — pipelines + status + step count
- `list_projects` — projects in the workspace
- `list_schedules` — schedules + cadence + enabled flag
- `list_alerts` — alert rules + channel + condition
- `list_executions` — recent runs with status filter
- `list_catalog` — available node types + connectors
- `inspect_connections` — connection metadata (NEVER credential values)
- `get_running_executions` — pipelines running right now
- `get_user_role` — caller's role + env + tier
- `get_next_scheduled` — upcoming schedule fires
- `get_installation_health` — installation health score + prioritised punch list (inline credentials, undeployed pipelines, top failing pipelines, 24h success rate, headline totals) in one call
- `summarize_pipeline` — plain-English summary of a pipeline
- `recall_history` — RAG search across executions, definitions, catalog, docs
- `query_metrics` — execution metrics aggregation

**4 SAFE_WRITE tier** (standard RBAC, inline preview, idempotency cache):

- `compose_report` — drafts a markdown/PDF report (no live data mutation)
- `draft_pipeline_from_intent` — turns plain English into a pipeline IR draft
- `draft_alert_rule` — turns "alert me if X" into an alert rule draft
- `modify_pipeline_step` — edits a specific step's params in an existing pipeline

**1 HIGH_IMPACT_WRITE tier** (strict RBAC, required confirmation card, dry-run by default for first 3 successful runs):

- `apply_pipeline_draft` — promotes a draft into the workspace

All three tiers are workspace-scoped + sanitization-gated + audit-logged. The dry-run-by-default behaviour means the first 3 invocations of any new HIGH_IMPACT_WRITE tool execute in dry-run mode regardless of what the user clicked, until the operator has seen 3 successful runs and the tool unlocks live mode.

### Governance (always-on, OSS + Plus)

- **Sanitization gateway** — PII / credentials / API keys / connection strings stripped before LLM sees data
- **Idempotency cache** — write tools key on (tool_name + args + tenant); duplicate calls within TTL replay the cached result instead of re-executing
- **Dry-run-by-default** — new HIGH_IMPACT_WRITE tools force dry-run for the first 3 successful runs before unlocking live mode
- **Confirmation card** — every write surfaces a before/after preview the user must approve
- **Trace store** — every run persisted with replay-safe step records (input/output hashes, never raw values)
- **Activity timeline** — audit + agent + execution feed combined into one chronological view
- **Prompt signing** — HMAC system prompt integrity check, refuses tampered prompts at module-load time
- **Tool-tier RBAC** — 4 roles × 2 envs × 3 tiers gating who can call which tier
- **Wallet caps** — per-user daily token cap, per-request cap, max-iterations cap, wall-clock cap. Layered defense against runaway loops + denial-of-wallet.

### Inline AI helpers (separate from the agent)

These are AI-augmented UI moments that don't go through the agent loop:

- **SQL helper** — Transform node has an "Ask AI" button that translates plain English to a DuckDB query
- **Pre-publish card** — pipeline detail surfaces "this pipeline looks risky because…" before deploy
- **Diagnose error** — failed run row shows a "Why?" button that explains the error
- **Post-run summary** — successful run shows "you processed 12,400 rows in 2.4s; here's what changed"
- **Cost estimate** — pipeline list surfaces an estimated $/run before triggering
- **Anomaly detect** — execution history flags outlier runs (memory, duration, row count)
- **Pre-run validate** — before kicking a pipeline, AI sanity-checks node configs

### Provider config

- **9 cloud providers supported** as opt-in escape hatches: Anthropic, OpenAI, OpenRouter, Gemini, DeepSeek, Groq, Mistral, Azure, Custom (OpenAI-compatible)
- **Local default** — Ollama with `qwen2.5:7b` (4.7 GB on disk, ~6 GB RAM at Q4_K_M, 30–60 s/turn on CPU; the 2026-05-19 tool-use floor)
- **OpenRouter model browser** — free-tier filter, tools-only filter, browse 100+ models from one dashboard
- **Token wallet caps** — per-user daily, env-tunable
- **Audit log** — every LLM call (provider, model, tokens, latency, success/error)
- **Provider price comparison** — `GET /api/ai/providers/compare` returns live pricing for budgeting

### RAG layer

- **Local embeddings** via Ollama `nomic-embed-text` (768-dim)
- **sqlite-vec** workspace-scoped vector store
- **`recall_history` tool** — searches execution failures, pipeline definitions, catalog, docs
- **Daily 03:00 UTC indexer** — re-indexes failures (last 30 days), pipelines, catalog, docs
- **`/api/ai/rag/reindex`** — on-demand reindex endpoint (admin-only)

## F-Pulse+ adds

### Org-level governance

- **Llama-Guard / safety classifier** — runs on every agent turn; blocks unsafe outputs
- **Cross-session conversational memory** — managed context that persists across sessions, deletable
- **Workspace-wide AI provider config** — admin-managed, with `monthly_budget_usd` workspace cap and `allow_user_override` toggle
- **Workspace-wide token wallet caps** — alongside per-user
- **Audit log retention + export** — extended retention for the LLM call audit log
- **Trace store extended retention** — 1-year+ with export

### RAG enhancements

- **Proactive trend detection** — "this pipeline fails every Monday" surfaces unprompted
- **Cross-workspace federated retrieval** — search across multiple workspaces (only for users with access)

## Anti-patterns

- ❌ Suggesting fine-tuning to a user — F-Pulse uses prompt-injection of context (Layers 1+2+3), not fine-tuning. The model isn't trained on F-Pulse-specific data; it has the docs RAG'd in.
- ❌ Telling a Free user "set up Llama-Guard" — Plus only.
- ❌ Promising "the agent remembers your previous chats" — Free has no cross-session memory; each chat session is independent.
- ❌ "Just paste your AWS credentials in" — never. The agent's tools never accept raw credentials. Credentials go through the Credentials page.
