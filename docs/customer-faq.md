# F-Pulse Customer FAQ

The eight questions enterprise buyers ask, with concrete answers and links to evidence.

For deeper detail see the [Trust posture](trust.md), [AI boundary contract](ai-boundary-contract.md), and [Performance budgets](performance.md) documents. Security disclosure lives in the `security.md` file at the repository root.

---

## 1. What's the memory footprint?

| Mode | Steady-state RAM |
|---|---|
| F-Pulse OSS (no AI provider) | ~89 MB |
| F-Pulse OSS + AI layer | ~115-135 MB |
| F-Pulse+ | ~115-135 MB |
| F-Pulse+ on-prem Ollama (per active session) | ~20 GB VRAM @ 8K context (KV cache ~2.5 MB/token) |

The AI layer adds roughly 25-45 MB to the steady-state footprint. Per agent run consumes 100-200 KB transient with context discipline; up to 1 MB without (we enforce discipline — see the [AI boundary contract](ai-boundary-contract.md)).

**Evidence:** [Performance budgets](performance.md) §1.

---

## 2. Does my data go to OpenAI / Anthropic?

Only if you configure a managed-LLM provider, and only the fields declared in each tool's input schema. Specifically:

- **Zero-LLM mode (default for OSS):** nothing leaves the host.
- **BYO key (OSS):** your own provider key; you control the account.
- **Managed (F-Pulse+):** workspace-configured provider; F-Pulse routes calls.
- **On-prem (F-Pulse+ high-tier):** bundled Ollama; calls route to localhost; no network egress.

**What's never sent:** plaintext credentials, Vault values, sample row data, full execution logs, anything matching `(?i)(password|secret|token|api_key|private_key)`, customer data classified L3/L4. See the [AI boundary contract](ai-boundary-contract.md) §2-3 for the per-tool send rules.

**On training:** where a provider offers a zero-retention API tier, that's what F-Pulse defaults to, and it sets per-request `disable_training` / `store: false` flags where supported. What a third-party provider does with your data is governed by your contract with that provider, not by F-Pulse — we can't guarantee their behaviour. If that matters to you, run a local model and nothing leaves your network.

---

## 3. Who can do what?

OSS Free is a single-operator install — there is no multi-user role model. Team-oriented role-based access control ships in F-Pulse+ (see hybridyn.com/f-pulse).

For AI specifically (added in Step 1.5b):
- **Per-tool RBAC binding** — Viewer can call `query_metrics` (read-only); only Data Engineer+ can call `compose_report` (safe-derived-write); only Workspace Admin can call `create_schedule` / `send_to_destination` (high-impact write).
- **Env-aware permissions** — DEV permissive; PROD blocks high-impact writes without approval (F-Pulse+).
- **Workspace AI policy** — admin sets which providers allowed, which models, which tools agent can use.
- **User AI on/off toggle** — every user can disable AI for their account.

Capability split (R/W on connections) shipped Apr 22, 2026. F-Pulse+ adds IP restriction, session management, concurrent-session limits.

**Evidence:** existing `backend/tests/test_workflow_store.py` etc.; new tests in Step 1.5b for AI tool RBAC.

---

## 4. What happens if the AI does something wrong?

Five layers of defense:

1. **Confirmation gate** before every write tool. Agent's plan rendered as a card; user accepts/rejects before execution.
2. **Dry-run mode by default** for new high-impact write tools until they pass an internal reliability threshold.
3. **Idempotency keys** required on every write tool (format: `{tier}.{user_id}.{action}.{target_id}.{semver}`). Retries are safe.
4. **Full execution trace per run** — replay-safe shape with hashed inputs/outputs, decision reasons, and outcome classification.
5. **Deterministic fallback** when LLM fails or returns low confidence. F-Pulse never breaks because the LLM is unavailable.

**Evidence:** Architecture invariants Rules 9 and 10 in `backend/tests/architecture/test_invariants.py`. Trust pillars 1 and 3 in `docs/trust.md`.

---

## 5. Can we audit?

Yes — three audit surfaces:

- **Agent execution trace** — every step (`tool_name`, `input_hash`, `output_hash`, `decision_reason`, `outcome`, `redactions_applied` (counts only), token usage, latency). Durable 90 days minimum, configurable per workspace, exportable as CSV/JSON.
- **Approval snapshot SHA-256 hash** — captures pipeline state at the moment of approval. Answers "who approved this on which date?" (F-Pulse+)
- **Existing audit log** — append-only, covers all admin actions and pipeline lifecycle events.

These are the evidence base for SOC2 Type II (audit roadmap'd Q3 2026) and DORA compliance.

---

## 6. What's your attack surface?

Documented in the repository's `security.md` file. For the AI layer specifically:

| Threat | Mitigation |
|---|---|
| Prompt injection from user data | Cryptographic prompt signing; trust-boundary invariant (all tool/RAG/external content treated as untrusted data, never instructions); per OWASP guidance |
| Data exfiltration via LLM | Per-tool field allowlist; PII redaction on input AND output; on-prem LLM option for regulated industries |
| Denial of Wallet (token bomb) | Per-user + per-workspace + per-request token caps; max-iterations cap (6); wall-clock timeout |
| Cross-tenant data exposure | Strict tenant isolation in cache keys (`{tenant_id}:{type}:{key}`); per-tenant trace storage; CI assertion (Rule 8) |
| Runaway agent loops | max_iterations=6 hard cap; timeout per execution; killable workers |
| Stale/poisoned RAG corpus | Embeddings shipped as signed build artifact; versioned; never user-mutable |

---

## 7. Data residency?

- **At rest:** stored credentials and AI provider keys are encrypted with Fernet (AES-128-CBC + HMAC-SHA256), using a master key generated per install. Same in Free and Plus — see [compliance.md](compliance.md).
- **In transit:** F-Pulse listens on plain HTTP and expects to sit behind your own TLS terminator (nginx / Caddy / a load balancer). The TLS version and cipher suite are yours to choose and enforce; F-Pulse does not terminate TLS itself.
- **Credentials:** Vault pattern with `credentials_ref` — secrets never in pipeline definitions, never in browser memory, fetched just-in-time.
- **AI traffic:** customer's choice — see Question 2. On-prem Ollama option keeps everything inside the customer VPC.
- **Hosting:** F-Pulse+ is **self-hosted**. We do not host customer data.

---

## 8. Compliance posture?

**F-Pulse the software is not certified against any of these** — certification and compliance apply to *your organisation and your deployment*, not to a tool. What F-Pulse provides is controls and evidence you can use in your own programme. Anything marked as planned is an intention, not a commitment.

| Standard | What F-Pulse gives you |
|---|---|
| **GDPR** | PII redaction layer (configurable per workspace) + credential isolation + audit trail. Trace export can feed your Article 30 records of processing — you remain the controller and own the obligation. |
| **SOC2 Type II** | Evidence base for your auditor: approval snapshots + agent traces + audit log. A Hybridyn Data Labs readiness audit is planned; ask sales for current status before relying on it. |
| **DORA (EU operational resilience)** | Approval snapshot artifacts + replay-safe traces + tier 5 RBAC + sandbox isolation — the evidence shape DORA auditors look for. DORA is a regulation, not a certification scheme: there is no DORA certificate for anyone to hold. |
| **HIPAA** | Not certified — there is no HIPAA certification. Self-hosting plus a local model keeps PHI inside your boundary, which is the deployment shape PHI handlers require. Ask sales about BAA availability. |
| **PCI** | Credit-card patterns in the default redaction denylist. Not a card processor; out of scope for direct certification. |

---

## 9. What's the Steward and does it touch our data?

The Steward is F-Pulse's read-only background reliability + learning
layer. It watches your pipeline definitions (workflow metadata —
connection IDs, table names, node types) and produces findings:
duplicate sources, duplicate pipelines, and (in later sub-agent
releases) failure patterns + schema drift + cost anomalies.

**What it does NOT do:**

- It never modifies your pipelines, connections, credentials, or
  schedules. Every action is "tell the user, let them decide."
- It never reads your *data* — only the pipeline definitions. Row
  content, file contents, query results, credential secrets: all out
  of scope.
- It never sends anything off-box. The detection runs in-process; the
  learning journal is a local JSONL file at
  `<data_dir>/steward/<workspace>/memory.jsonl`.
- It never uses an LLM for detection. The core is plain code
  (deterministic SHA-256 signatures over node identity fields). The
  LLM is only used to phrase findings in natural language in later
  sub-agents, and never gates correctness. The Steward works fully
  with the AI assistant disabled.

**Privacy posture:** treat the Steward's storage like the rest of
your F-Pulse data dir — it's covered by the same disk encryption
and the same access controls as your workflow database. Nothing
leaves the box.

**Compliance:** findings + the audit journal are exportable +
inspectable as plain JSON. The dismiss-with-reason field captures
user intent for "yes this duplicate is intentional, here's why" —
useful evidence under change-management audits.

**The F-Pulse Memory Layer** ([steward/memory-layer.md](steward/memory-layer.md))
is the durable lesson surface that sits alongside the operational
event journal. It's where typed, human-approved lessons accumulate:
source quirks, failure-fix pairs, retry rules, intentional
duplicates. Each lesson is stored as a YAML file on disk (one per
lesson) — auditable, hand-editable, and version-controllable if you
choose to commit them. PROPOSED lessons explicitly do NOT influence
future Steward reasoning until a human approves them. This is the
gated-learning invariant (Rule 3 in the architecture doc) and the
reason the Memory Layer is safe to leave running.

Full details: [steward/overview.md](steward/overview.md) (user-facing),
[steward/memory-layer.md](steward/memory-layer.md) (Memory Layer in
depth), and [steward/architecture.md](steward/architecture.md) (design).

---

## What we do not claim

To match the spirit of `docs/trust.md` §"What this framework does not claim":

- We do not claim AI suggestions are always correct. Confirmation gates exist because suggestions can be wrong.
- We do not claim the on-prem LLM matches frontier-model quality. Sovereignty trades capability.
- We do not claim PII redaction is exhaustive. Workspace admins should configure data-class-specific patterns.
- We do not claim zero risk under adversarial prompts. We claim defense-in-depth and reduced blast radius.

---

## Asking us a question not on this list

Email **info@hybridyn.com** for security and data-handling questions (subject prefix `[F-Pulse security]`). General product questions: see `docs/readme.md` or open a discussion in this repo.
