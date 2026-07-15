# F-Pulse FAQ

## Is F-Pulse free?

Yes. The OSS Free edition is Apache 2.0 licensed — free forever, no usage caps, no feature gating beyond the documented Plus boundary. Run it on your laptop or a VPS with no payment required.

F-Pulse+ is the commercial edition for teams. Pricing is per-workspace per-month. Contact sales (or see the website) for current rates.

## Does my data leave my machine?

No, by default. F-Pulse is self-hosted single-tenant. The only outbound traffic in the default config is when you opt into a cloud LLM provider (Anthropic, OpenAI, etc.) — and even then, only the prompt + selected tool inputs are sent, never raw pipeline data.

The recommended posture is **local-only**: use Ollama with `qwen2.5:7b` (the 2026-05-19 tool-use floor) and your data never leaves the host. See `/api/trust/posture` for live verification.

## What's the difference between F-Pulse and a managed ETL service?

Managed services run YOUR pipelines on THEIR infrastructure. Your data passes through their servers. F-Pulse runs YOUR pipelines on YOUR infrastructure. Your data stays where it is. Trade-off: you operate the infrastructure (or a VPS) yourself.

## Do I need a GPU?

No. F-Pulse runs on CPU-only hardware. The recommended local AI model `qwen2.5:7b` (the 2026-05-19 tool-use floor) is 4.7 GB on disk, needs ~6 GB RAM at Q4_K_M, and produces tool-using turns in 30–60 s on a typical laptop CPU. A GPU speeds up the AI Copilot but the execution engine is CPU-only.

For larger local models (`qwen2.5:14b` for sub-3-second tool calls) you need a workstation with 12+ GB VRAM. For `llama3.1:70b-q4` you need a server with 48+ GB VRAM.

## Why qwen2.5:7b and not qwen2.5:3b?

`qwen2.5:3b` was the recommended local model from May 5 to May 19 2026. It produces fast turns on CPU (5–15 s) but it sits below the reliable tool-use floor — it advertises tool schemas yet silently returns greetings or empty text instead of calling tools. Three independent reviews on 2026-05-19 converged that ~7B is the floor where local tool-use becomes dependable. `qwen2.5:7b`, `llama3.1:8b`, and `phi-4` are all equally-supported picks at the floor (~6 GB RAM, 30–60 s per turn on CPU).

## How do I do incremental sync — only load new rows?

Set the Database Source to `sync_mode=incremental` and pick a `Cursor Column` (typically `updated_at` or an auto-increment id). On the first run F-Pulse reads everything and stamps the max cursor value into the `sync_state` table. On every subsequent run it auto-loads that value, appends a `WHERE cursor_col > ?` clause, and stamps the new max when the run succeeds. The Manual Cursor Override field lets you backfill from a specific date without resetting state. The "Reset State" button on the Source config drops the row so the next run behaves like a full refresh again. (Added 2026-05-30 — earlier versions required hand-typing the watermark value between runs.)

## Can I run pipelines without AI?

Yes. AI is the Copilot for *building* pipelines and answering operational questions. It is NOT in the data path — your pipelines run via the deterministic execution engine regardless of whether AI is configured. Set `FPULSE_DISABLE_OLLAMA_AUTOPROBE=1` and don't configure a cloud provider, and zero traffic leaves the host.

## How do I install F-Pulse?

The recommended path is Docker:

    git clone https://github.com/hybridyn/fpulse
    cd f-pulse
    docker compose up

Read `docs/deployment.md` for the operator runbook including upgrades, backup, disaster recovery.

For from-source development: clone, install backend deps, install frontend deps, run uvicorn + vite. See `docs/quickstart.md`.

## How do I upgrade F-Pulse versions?

F-Pulse is delivered as Docker images pinned in `docker-compose.yml`. The three components (F-Pulse / Ollama runtime / Ollama models) are independently versioned per the Tested-with matrix in `changelog.md`.

Standard flow: `git pull` → read CHANGELOG → `docker compose pull fpulse` → `docker compose up -d fpulse`. Diff `.env.example` for new variables.

## How do I back up F-Pulse?

The data lives in the `fpulse_data` Docker volume (or `data/` on disk for from-source installs). Tarball it daily on a schedule (your OS task scheduler — Task Scheduler on Windows, launchd on macOS, cron on Linux) with 7/4/12 rotation (7 daily, 4 weekly, 12 monthly). Treat the tarball like credentials — it contains your encryption key material.

For full DR: backup the data volume + `.env` file + the version tag (`FPULSE_IMAGE_TAG`) so you can restore on a new host.

## What runs on my server vs in the browser?

- **Backend (FastAPI on uvicorn):** all pipeline execution, the agent loop, the API, SQLite database, optional Ollama runtime. CPU-bound; needs 4+ GB RAM.
- **Frontend (Vite/React):** the editor canvas, dashboards, settings UI. Runs in the browser; the backend serves the bundled assets.

The browser never directly talks to your databases or files. Every action goes through the backend's API where RBAC + policy + audit fire.

## Is F-Pulse SOC 2 / HIPAA / PCI compliant?

The F-Pulse software itself is not separately certified. Compliance is a property of the deployment, not the software. F-Pulse provides the controls (encryption at rest, audit logs in Plus, RBAC in Plus, no outbound traffic by default) that an operator needs to bring their deployment into scope of their own attestation.

The compliance one-pager (`docs/COMPLIANCE.md`) lists every claim with a verifiable artifact.

## Where do I report bugs / get help?

GitHub issues for the OSS edition. Hybridyn support contracts cover F-Pulse+ customers. Security issues: see `security.md` for responsible disclosure.

## Can I contribute?

Yes. F-Pulse OSS is Apache 2.0 — fork, edit, PR. Look for "good first issue" labels. The contributor guide is `contributing.md`.

## Why is the Copilot sometimes slow?

When using local Ollama on CPU, the AI Copilot takes 30–60 s per turn for tool-using prompts at the recommended floor (`qwen2.5:7b`), and a multi-step task can take 1–5 minutes end-to-end. This is normal — the floor is what sovereignty costs on CPU.

For instant answers, use the fast-lane phrasings: "list pipelines", "give me an overview", "what failed today", "running now", "what is f-pulse". These bypass the LLM entirely and return in under a second.

For analytical questions ("why did X fail?", "compare A and B"), the LLM is needed. On CPU expect 1-3 minutes. Use the Stop button if you change your mind mid-turn.
