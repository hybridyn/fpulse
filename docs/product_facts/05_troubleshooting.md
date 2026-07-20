# F-Pulse troubleshooting reference

## "Agent timed out after ~Ns"

The AI Copilot uses a wall-clock cap to prevent runaway loops. Default: 300 seconds for local Ollama, 120 seconds for cloud providers. When you hit the cap, the cause is usually one of:

- **Local CPU on a model heavier than the tool-use floor.** Models bigger than ~8B on CPU regularly run past the wall-clock cap. Stay at the recommended floor (`qwen2.5:7b` / `llama3.1:8b` / `phi-4` — ~6 GB RAM, 30–60 s per turn). Pull from Insights → AI Provider.
- **Local model BELOW the tool-use floor.** `qwen2.5:1.5b` and `qwen2.5:3b` advertise tool schemas but silently reply with greetings or empty text instead of calling tools. The agent loops until the cap fires with nothing to show. The first-launch banner catches this — click **Install qwen2.5:7b** there, or pull manually from Insights → AI Provider.
- **First inference after a model swap.** First call after Ollama loads a different model is 2-3x slower than subsequent calls. Try sending the prompt again.
- **Other heavy processes** competing for RAM/CPU. Close VS Code, browsers, anything large.

Power users can raise the cap to up to 600s with `FPULSE_AGENT_WALL_CLOCK_S=600` in the backend env.

For instant answers (no LLM), use the Copilot fast-lane phrasings: "list pipelines", "give me an overview", "what failed today", "running now", "what's my role". Sub-1 second response, no LLM required.

## "AI is not configured"

The Copilot needs an active provider. Two paths:

- **Local (default, recommended for privacy):** install Ollama from https://ollama.com, pull `qwen2.5:7b` (the 2026-05-19 tool-use floor), F-Pulse auto-detects. Set `OLLAMA_URL` env var if Ollama isn't on `localhost:11434`.
- **Cloud:** Insights → AI Provider → pick Anthropic / OpenAI / OpenRouter / Gemini / DeepSeek / Groq / Mistral / Azure → enter API key. Cloud means prompts and tool inputs leave the host — opt-in only.

## "Database is locked" warnings on restart

Caused by Stage 3a's connection-tracking system not draining all connections at shutdown. Usually self-heals on the next start. If it persists: `docker compose down`, wait 5s, `docker compose up`.

## A pipeline ran out of memory

Every execution captures `peak_memory_mb`. Check the Executions page → click the run → view the metadata.

DuckDB defaults to 80% of host RAM. To cap it, set `FPULSE_DUCKDB_MEMORY_LIMIT=4GB` (or whatever value) in the backend env. Settings → General → Execution Tuning shows the live value.

If the spill directory is on an HDD instead of SSD, performance drops 10-100×. Settings → General → Execution Tuning shows the spill disk type. Set `FPULSE_DUCKDB_TEMP_DIR=/path/to/ssd` to override.

## "RBAC: role X has no allowed tiers in env Y"

Plus only. The role doesn't have AI tool access in the requested environment. Either:

- Switch environments (top-right env stripe)
- Ask an admin to grant your role write access in that env via Settings → Approval Policy

In OSS Free, this error shouldn't appear since the single-user model grants read everywhere + write in DEV.

## "Connector validation failed"

The cert matrix at `/api/connectors/cert-matrix` shows depth scores per manifest. Score 0 = stub, 1-2 = alpha, 3-4 = beta, 5 = production-grade. Connectors below depth-5 may have missing pagination, missing primary key, or incomplete fixture coverage.

To migrate a v1 manifest to v2: `python -m fpulse.connectors.certify --migrate <connector_id>` produces a v2 skeleton.

## "Pipeline failed mid-run"

Sprint 1 added the `pipeline_checkpoints` table. Failed runs leave per-step outcomes in this table; the executor can resume from the first non-success step instead of from scratch.

The frontend's Executions page shows a "Resume from step X" button on failed runs alongside "Re-run from start." On resume, F-Pulse re-loads each successful step's Parquet snapshot from `data/checkpoints/<run_id>/<step_id>.parquet` and skips its `execute()`.

Checkpoints have a default 7-day TTL; configurable via the cleanup sweeper.

## Bulk loader fails with "psycopg2-binary is not installed"

The bulk-load runner is dialect-aware. The Postgres path needs `psycopg2-binary` or `psycopg` v3 installed. Add to `requirements.txt`:

    psycopg2-binary>=2.9

Without the driver the bulk-loader raises `BulkLoaderNotAvailable` and the caller falls back to row-by-row INSERT. Functionally identical, just slower.

## "Why is my pipeline slow on a 50M-row source?"

Most likely the pipeline uses row-by-row INSERT instead of Bulk Loader. Replace `Database Sink` with `Bulk Loader` (Postgres or Snowflake) and pick `mode=append` or `mode=merge`. Order-of-magnitude speedup at scale.

If the source is the slow part, check Data Profile node output to see if a column has unexpectedly high cardinality forcing a hash spill.

## Telemetry — what does it send?

Default: nothing. Telemetry is OFF in OSS unless an admin opts in via Settings → Security → Privacy. Even then, F-Pulse v1.0 only stores the consent flag — the telemetry sender itself ships in v1.0.1. The opt-in is informational at this point.

When the sender lands, the payload schema will be public at `/docs/TELEMETRY.md`. No customer data, no pipeline contents — just usage counters.
