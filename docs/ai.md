# AI guide

F-Pulse has an embedded AI agent (the **Copilot**) and inline AI helpers throughout the editor. Both work with local LLMs (Ollama) or cloud providers — your choice.

## Two surfaces

### Inline helpers

Specific UI moments where the agent augments a single action:

- **SQL helper** — describe what you want, get a working SQL transform
- **Pre-publish card** — checks your pipeline against best practices before deploy
- **Diagnose error** — given a failure, suggests root cause + fix
- **Post-run summary** — natural-language description of what just ran
- **Cost estimate** — predicts row count + duration + LLM tokens for the next run
- **Anomaly detect** — flags unusual rows in output
- **Pre-run validate** — schema + connection sanity checks

### Copilot dock

The floating chat panel in the bottom-right corner. Opens to a full conversation with 25 tools the agent can call (per `backend/fpulse/ai/tools/`):

| Read tools (21) | Draft / Write tools (4) |
|---|---|
| `list_pipelines`, `list_projects`, `list_schedules`, `list_alerts`, `list_executions`, `list_catalog`, `list_storage`, `list_templates`, `inspect_connections`, `summarize_pipeline`, `query_metrics`, `compose_report` (draft-only), `recall_history`, `workspace_overview`, `get_user_role`, `get_user_role`, `get_installation_health`, `get_next_scheduled`, `get_running_executions`, `lookup_help_topic`, `explain_step`, `validate_pipeline` | `draft_pipeline_from_intent`, `apply_pipeline_draft` (always confirms), `modify_pipeline_step`, `draft_alert_rule` |

Slash commands: `/fix`, `/explain`, `/optimize`, `/test`. Type `/` to see them.

## Local LLM setup (recommended)

The May 3 2026 lock makes **local-only LLM the F-Pulse default** — your data never leaves your machine.

### 1. Install Ollama

[ollama.com/download](https://ollama.com/download) — one-click installer for macOS, Linux, Windows.

### 2. Pull a recommended model

| Model | Size | RAM | Best for |
|---|---|---|---|
| **`qwen2.5:7b`** | 4.7 GB | 8 GB | **CPU pick (floor)** — reliable tool-use, ~30–60 s per turn on CPU |
| `llama3.1:8b` | 4.9 GB | 8 GB | Alternative at the floor — strong tool-use |
| `phi-4` | 9.0 GB | 12 GB | 14B equivalent perf; needs more RAM |
| `mistral-nemo` | 7.1 GB | 10 GB | Tool-capable, 12B params, GPU recommended |
| `qwen2.5:1.5b` / `qwen2.5:3b` | 0.9 / 1.9 GB | 2 / 3 GB | **Below tool-use floor** — advertises tool support but returns greetings instead of calling tools. Not recommended. |
| `phi3` | 2.3 GB | 4 GB | Smallest, **text-only** (no agent tool support) |

> **Floor revision (2026-05-19):** the older `qwen2.5:1.5b` / `:3b` picks were demoted after they failed to drive the agent's tool-use loop reliably — the model would silently reply with greetings or empty text instead of calling a tool. The reliable floor is now ~7B (~6 GB RAM at Q4_K_M).

```bash
ollama pull qwen2.5:7b
```

You can also pull from inside F-Pulse: open **Insights → AI Provider** and click **Pull** next to the recommended model. Streams progress inline. The first-launch banner also catches you if you're on a sub-floor model and offers a one-click upgrade.

### 3. Verify

The Copilot's status banner should show `ollama / qwen2.5:7b · 25 tools · 0% of daily cap`. Type "give me a quick overview of my workspace" — expect a response in 30–60 seconds on CPU at the floor.

If it times out, you're likely on a model heavier than 7B on a CPU-only host. Switch back to `qwen2.5:7b` or pick a cloud provider for faster responses.

## Cloud providers

Open **Insights → AI Provider** to configure:

- **Anthropic** (Claude Haiku / Sonnet) — recommended cloud default
- **OpenAI** (GPT-4o, GPT-4o-mini) — function-calling support
- **OpenRouter** (100+ models behind one API key) — try multiple providers cheaply
- **Google Gemini** (Gemini 2.0 Flash) — fast, strong reasoning
- **DeepSeek**, **Groq**, **Mistral**, **Azure OpenAI**, **Custom** (any OpenAI-compatible endpoint)

Your API key is **stored encrypted on this machine** (Fernet — AES-128-CBC + HMAC-SHA256, per `backend/fpulse/security/encryptor.py`). It is never sent to any service except the provider you chose.

The provider price comparison table on the AI Provider page shows live per-token rates from OpenRouter.

## Governance & safety

OSS Free is single-user, so role-based gating doesn't apply — everything below runs against the bootstrap user. The protections are still active:

- **Tool tiering** — every agent tool is classified `read` / `safe_write` / `high_impact_write`. Confirmation cards + dry-run-by-default attach to the write tiers regardless of who's logged in.
- **Sanitization gateway** — PII and credentials are redacted before any payload reaches the LLM (per the [AI boundary contract](ai-boundary-contract.md)).
- **Idempotency cache** — write tools never duplicate side effects on retry.
- **Dry-run-by-default** — the first 3 runs of a new write tool are forced to dry-run mode.
- **Confirmation card** — every write tool shows what it'll do and waits for explicit confirmation.
- **Trace store** — every agent run is persisted with replay-safe step records (open the **History** toggle in the Copilot to browse).
- **Wallet caps** — daily token cap (default 100K user / 1M workspace) prevents denial-of-wallet attacks.
- **Prompt signing** — system prompt is HMAC-signed; the agent refuses to run if it's been tampered with.

F-Pulse+ adds: **4-role agent RBAC** (`viewer` / `developer` / `admin` / `super_admin`) so different team members see different tool tiers, **Llama-Guard safety classifier** on every turn, **cross-session conversational memory**, **proactive trend detection**, and **federated retrieval across workspaces**.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Agent times out at ~300 s on local | Model heavier than the floor on CPU | Stay at `qwen2.5:7b`, or move to cloud |
| Copilot returns greetings instead of acting | Model below the tool-use floor (e.g. `qwen2.5:1.5b`, `:3b`) | Pull `qwen2.5:7b` from the banner |
| "Text-only" badge on chat | Model isn't tool-trained (e.g. `phi3`, `mistral`, `codellama`) | Pull `qwen2.5:7b` or `llama3.1:8b` |
| "0 tokens" on every response | API key invalid or rate-limited | Check the AI Provider page; rotate the key |
| Tools never get called | Model is text-only; agent runs in chat-only mode | Use a tool-capable model |
| `Reached max iterations` | Agent looped on a tool call | Refresh chat; report the trace via Copilot **History** |

## See also

- [Trust posture](trust.md) — what we do and don't do with your data
- [AI boundary contract](ai-boundary-contract.md) — the 10 architecture invariants the agent code is required to satisfy
- [Performance budgets](performance.md) — latency budgets per tool
