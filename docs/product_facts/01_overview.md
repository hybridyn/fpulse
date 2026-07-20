# What is F-Pulse?

F-Pulse is a self-hosted, single-tenant data workflow platform for teams whose data cannot leave their infrastructure. It runs on the operator's own machine or VPS and is delivered as a Docker image plus optional Ollama runtime for local AI.

## The three pillars

**Determinism.** The pipeline kernel is deterministic code. Every step (filter, transform, aggregate, sink) runs the same way on the same input every time. The AI Copilot suggests; the system enforces. Every action passes through RBAC, policy, schema validation, and idempotency checks before it touches data.

**Auditability.** Every run is traced and replayable. Each execution captures status, duration, rows processed, peak memory, CPU seconds, parameter values, and a SHA-256 snapshot of the IR that ran. An auditor can replay any pipeline 6 months later from the snapshot.

**Sovereignty.** Local models, local data, local control. By default no data leaves the host. The recommended local AI model is `qwen2.5:7b` running on Ollama (the 2026-05-19 tool-use floor — ~6 GB RAM, 30–60 s per agent turn on CPU). Sub-7B Qwen 2.5 models advertise tool support but can't reliably drive the agent loop. Cloud LLM providers (Anthropic, OpenAI, OpenRouter, etc.) are an explicit opt-in escape hatch — choosing one means prompts and tool inputs leave the host.

## Who is it for?

The OSS Free edition targets a single developer running pipelines on their laptop or a small VPS. Teams of 2+ users, multi-environment governance, audit-log retention, SSO, and approval gates require F-Pulse+, the commercial edition.

## What problem does F-Pulse solve?

Most data tools force a tradeoff between AI assistance and data sovereignty. F-Pulse keeps both: the AI Copilot helps you build pipelines but never moves your data anywhere by default. The execution engine is fully deterministic — no LLM in the data path, ever.

## Two editions in one binary

The OSS Free edition is Apache 2.0 licensed and includes the full execution engine, 40 node types (including Microsoft Graph Source, Managed Table Source, and Managed Table Sink added 2026-05-22 / 23), and the AI Copilot. F-Pulse+ is a paid extension that adds team-oriented governance — see [hybridyn.com/f-pulse](https://hybridyn.com/f-pulse).

The Plus features physically exist in the codebase but are gated by a license check. Operators upgrade by entering a license key; no separate install required.

## The data home — Storage page

Since 2026-05-23, F-Pulse OSS ships a first-class **Storage** page (sidebar, between Pool and Insights). It's the workspace's data estate: every uploaded file, every managed Parquet table, every pipeline output is visible there. Three sub-tabs:

* **Files** — uploaded data files with project/folder scope, replace-in-place, soft-delete + trash, and "Used by N pipelines" tracking that gates destructive actions.
* **Managed Tables** — Parquet-backed tables addressable by `schema.name` from `local_table_source` / `local_table_sink` nodes. Promote any uploaded file to a managed table in one click; pipelines reuse the data without re-uploading.
* **Pipeline Outputs** — files written by pipeline runs, grouped by `(pipeline_id, run_id)`.

Storage closes the **Connect → Store → Transform → Reuse** loop — the gap that previously made every pipeline output feel ephemeral.
