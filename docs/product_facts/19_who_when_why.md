# Who / When / Why — F-Pulse Q&A reference

Permission, timing, and rationale questions in W5H1 format.

---

## WHO

### Who can edit a pipeline?

**OSS Free**: the single operator. There's no role hierarchy at the
workspace level.

**F-Pulse+**: workspace-RBAC roles — Workspace Admin and Data Engineer
can edit; Analyst and Viewer cannot. Super Admin (install-wide) always
can.

### Who can approve a PROD deploy?

**Plus only.** Roles with the `approve` permission — by default
Workspace Admin and Analyst (not Data Engineer, not Viewer). The
two-gate flow requires Gate 1 from the developer + Gate 2 from a
different approver if "two-person rule" is enabled in Settings →
Approvals.

OSS Free has no approval workflow.

### Who can read this credential's value?

**Nobody, including admins, via the UI.** Credentials are encrypted
at rest with Fernet; the frontend never sees plaintext. The runtime
backend reconstitutes the value at pipeline-run time. The agent's
`inspect_connections` tool returns key NAMES only.

The master encryption key file (`~/.fpulse/secret.key`) is the only
artifact that can decrypt credentials. Whoever has filesystem access
to the host can read it.

### Who can delete a pipeline?

OSS Free: the operator. Plus: Workspace Admin or Super Admin.

### Who is logged in as?

Top-right user menu shows the user. Or chat fast lane: `who am I`.

### Who can see my data?

Default config: **nobody outside this host**. F-Pulse is self-hosted
single-tenant. No outbound traffic in default config.

**Cloud LLM providers** (Anthropic / OpenAI / etc.) see prompts +
selected tool inputs ONLY when an admin opts in via Settings → AI
Provider. Even then, raw pipeline data is never sent — only the
prompt + sanitized tool outputs.

**Telemetry** is OFF by default. When enabled (admin opt-in only),
the planned telemetry sender (1.0.1) sends usage counters — no
customer data, no pipeline contents.

### Who can install F-Pulse+ features?

Anyone with a valid license key. Settings → License → paste the key.
Plus features unlock immediately without re-installing.

### Who maintains F-Pulse?

Hybridyn Data Labs. The OSS edition is Apache 2.0 licensed
— community contributions welcome via GitHub PR.

---

## WHEN

### When does the scheduler check for due pipelines?

Every **30 seconds**. So a `daily 09:00` schedule may fire any time
between 09:00:00 and 09:00:30.

### When does the watchdog detect long-running runs?

The watchdog polls execution rows every 30 seconds. A run is flagged
as long-running when `duration_ms` exceeds the configured threshold
(default 30 minutes). Alert fires within 30 s of the threshold cross.

### When does the watchdog detect a missed schedule?

When a scheduled run hasn't started within the configured grace window
(default 5 minutes after the expected fire time) the watchdog emits
`ON_SCHEDULE_MISS`.

### When does the RAG indexer run?

**Daily at 03:00 UTC.** Re-indexes failures from the last 30 days +
all pipelines + catalog + docs. Skipped when `FPULSE_DISABLE_RAG=1`.

To trigger manually: `POST /api/ai/rag/reindex`.

### When does the product knowledge indexer run?

**At app startup**. Re-runs whenever the backend starts. To trigger
without restart: Settings → Security → AI product knowledge → Reindex
button (admin only). Or `POST /api/ai/product-knowledge/reindex`.

### When does the agent's wall-clock cap fire?

300 seconds for local Ollama, 120 seconds for cloud providers. Per-call
override via env var `FPULSE_AGENT_WALL_CLOCK_S` (clamped 10-600).

### When are checkpoints garbage-collected?

Default TTL **7 days**. After that, the resume-from-step button is no
longer offered for the run.

### When does the credential encryption key get used?

On every credential save (encrypts) and every credential load
(decrypts). The key is read once at startup and held in memory.

### When does F-Pulse phone home?

**Never in default config.** The only outbound traffic is what you
explicitly opt into:
1. Cloud LLM provider API (if configured)
2. Ollama model pulls (when you pull a new model)
3. Backup uploads (if backup destinations are S3/Azure/GCS)
4. Telemetry sender (1.0.1+, off by default, opt-in only)

### When was this pipeline last successful?

Chat fast lane — phrase doesn't have a direct intent yet, falls
through to LLM which reads `list_executions(workflow_id=X, status=success, limit=1).completed_at`.

### When does an auto-save fire?

2 seconds after the last canvas edit, when **Auto-save canvas changes
to the backend** is enabled in Settings → General → Editor Preferences
(default ON).

### When did the master key file get created?

On first F-Pulse startup (or after the file is deleted, on next start).
File mtime tells you the date: `ls -la ~/.fpulse/secret.key`.

---

## WHY

### Why qwen2.5:7b? (and why not qwen2.5:3b?)

The CPU recommendation has moved twice in 2026. Up to April it was
`llama3.1:8b`, which took 30-60 s per turn — at the time that was
considered too slow for interactive tool-use, so the May 5 lock moved
to `qwen2.5:3b` for speed.

On 2026-05-19 a real-world failure surfaced: `qwen2.5:1.5b` and `:3b`
advertise tool schemas, but they silently reply with greetings or empty
text instead of calling tools when the agent loop hands them a tool
choice. Three independent reviews on that day converged: the reliable
tool-use floor is ~7B. `qwen2.5:7b`, `llama3.1:8b`, and `phi-4` are now
the equally-supported picks (~6 GB RAM at Q4_K_M, 30–60 s per turn on
CPU).

Per `docs/supported-models.md`, `qwen2.5:7b` is the locked CPU default.
For GPU users with 12+ GB VRAM, `qwen2.5:14b` is recommended.

### Why is local-only the default?

**Privacy is non-negotiable for the target buyer** (regulated
industries, sovereignty-bound enterprises). Cloud LLM = data leaves
the host = disqualifying for the segment that justifies Plus pricing.

The local-first lock is documented in
`project_fpulse_local_only_lock_2026-05-03.md` (project memory) and
mirrored in `edition-matrix.md` + the trust posture endpoint.

### Why is the chat sometimes slow?

Local CPU LLM inference is the bottleneck. `qwen2.5:7b` (the
2026-05-19 tool-use floor) runs at ~3-6 tokens/second on a typical
laptop CPU. A tool-using turn is 2-3 LLM round-trips, each 30-60 s,
so 1–5 minutes per turn end-to-end is normal.

For instant answers, use the fast-lane phrasings (`list pipelines`,
`overview`, `failures today`, `running now`, `what's my role`,
`what is f-pulse`) — these bypass the LLM entirely.

### Why does the chat refuse "Set up SSO"?

Because SSO is **F-Pulse+ only**. The session context block tells the
LLM the user's edition; the LLM correctly routes "set up SSO" to "this
is a Plus feature" instead of hallucinating an OSS path.

### Why is PROD execution Plus-only?

The PROD execution path requires the **two-gate approval workflow**
+ **Sandbox isolated dry-run** + **audit log retention** that
production deployments need to be safe. These are F-Pulse+ features.

OSS Free is positioned for the solo developer building pipelines on
their laptop or a small VPS where DEV-only is sufficient.

### Why does F-Pulse refuse to start with a world-readable secret.key?

**Fail-closed security.** A world-readable encryption key is
indistinguishable from no encryption — anyone with read access to
the host filesystem can decrypt every credential. Refusing to start
forces the operator to fix the permissions before damage accumulates.

To fix: `chmod 600 ~/.fpulse/secret.key`.

### Why is bulk-loader faster than INSERT?

`Database Sink` does row-by-row `INSERT INTO ... VALUES (...)` with
per-row commit. At >10k rows the per-row overhead dominates.

**Bulk Loader** uses dialect-native paths:
- Postgres: `COPY FROM STDIN` — single message, no per-row
  round-trips, native binary protocol. 10-100× faster.
- Snowflake: `PUT` to user stage + `COPY INTO target`. Compresses
  + bulk-loads in one shot. Even faster ratio at large scale.

### Why are credentials encrypted but the workflow IR is not?

Workflow IR contains node configs + connections by ID — never raw
secrets. Encrypting the IR would add operator pain (can't read
pipeline definitions in DB tooling) for zero security benefit.
Credentials, by contrast, contain reversible secrets that need
encryption.

### Why does the agent never run a pipeline directly?

Per the trust posture: **AI is a suggestion layer, never a writer.**
The agent can DRAFT a pipeline (SAFE_WRITE) and APPLY a draft
(HIGH_IMPACT_WRITE with confirmation card + dry-run-by-default), but
it cannot trigger a pipeline run on its own. The user (or scheduler)
clicks Run.

This is enforced at the tool registry — there is no `run_pipeline`
tool. The agent loop has no path to start an execution.

### Why are there two RBAC systems?

They serve different purposes:
- **Agent-tool RBAC** (OSS + Plus, always-on) gates what the LLM can
  call on the user's behalf. 4 roles × 2 envs × 3 tool tiers.
- **Workspace RBAC** (Plus only) gates who can edit what in the
  workspace. 5 tiers with per-environment permissions.

Both checks fire on every action. Mixing them in error messages
confuses users; treat them as distinct.

### Why use Fernet over raw AES-GCM?

Fernet is the high-level recommended interface in the `cryptography`
library — it's hard to misuse, isn't subject to AEAD nonce-reuse
foot-guns, and the format is versioned (`ENC:v1:`) so we can rotate
without breaking old ciphertexts.

Raw AES-GCM is faster but a single nonce-reuse bug catastrophically
breaks confidentiality. Not worth the risk for credential storage.

### Why is the connector count 37 instead of 60+ or 45?

Earlier marketing claimed 60+. The May 3 2026 batch corrected this to
45 (8 db + 37 SaaS), then the May 4 audit corrected to 43, and the
2026-05-28 SSOT pass (driven by `frontend/src/components/hiddenNodeTypes.ts`)
landed on **37**: the "8 database/warehouse dialects" was overcounted —
actual is 4 database dialects (postgresql / mysql / mssql / sqlite via
Database Source/Sink) + 2 bulk-load dialects (Postgres `COPY FROM STDIN`,
Snowflake `PUT` + `COPY INTO`) + ~31 SaaS manifests. Per
`edition-matrix.md` line 27.

### Why are some "starter" connectors duplicated in Plus?

The OSS Salesforce / NetSuite / Workday / etc. manifests are
**starter versions** with basic schema + pagination. The
production-grade Plus versions add full incremental sync, BAPI/RFC
support (SAP), hardened authentication paths, and depth-5
certification.

OSS users can use the starter; Plus customers get production-grade.

### Why doesn't the chat remember my previous conversations?

Cross-session conversational memory is **F-Pulse+ only** per
`edition-matrix.md` line 107. OSS Free starts each session fresh.

The trace store keeps a per-run record but it's not used as
conversational memory in OSS — surfaces in the History tab of the
chat dock for replay only.

### Why is my pipeline schedule not firing?

Check the four common causes in `docs/product_facts/18_analytical_questions.md`
under "Why didn't my schedule fire?".

### Why does F-Pulse use SQLite?

Single-binary local execution is the OSS posture. SQLite + WAL mode
is the right fit for single-node + a few thousand pipelines:
- No server to operate
- ACID transactions
- Free from Hybridyn Data Labs' perspective (Apache 2.0 dependency)
- 64 MB page cache covers most working sets

F-Pulse+ adds optional Postgres for multi-worker horizontal scaling.
OSS users almost never need it.

### Why does the master key live at ~/.fpulse/secret.key?

POSIX convention puts user-scoped state under `~/.<app>/`. The location
is overridable via `FPULSE_MASTER_KEY_FILE` or `FPULSE_DATA_DIR` env
vars for operators who want it elsewhere (e.g. on a separate encrypted
volume).
