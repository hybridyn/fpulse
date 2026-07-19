# F-Pulse AI Operations Contract

**Status:** Active 2026-05-01. Operator-facing companion to `ai-boundary-contract.md`.

`ai-boundary-contract.md` answers *what the LLM may see*. This document answers *how the runtime behaves, how long data lives, and what happens when something fails*. It is the contract between F-Pulse and the operator (admin, auditor, customer ops, on-call) — read by humans, not enforced by tests.

If `AI_BOUNDARY_CONTRACT` is the constitution, this is the operations manual.

---

## 1. Data classes

The AI layer handles six distinct classes of data. Each has its own sensitivity, retention, and audit posture. Mixing them is a defect.

| # | Class | Examples | Where it lives | Sensitivity |
|---|---|---|---|---|
| 1 | **Prompt text** | system prompt, user intent, RAG context block, page-context summary | in-memory only (per-request) | Low — system prompt is signed; user intent is treated as untrusted data |
| 2 | **Tool args** | `{pipeline_id: "wf-42", limit: 20}` passed to a tool handler | in-memory + trace store (hashed) | Medium — may name internal IDs; never raw values |
| 3 | **Tool results** | `{executions: [...], total: 12}` returned from a handler | sanitized → in-memory → LLM; full payload hashed in trace | Medium — sanitized to drop credentials, PII, oversize blobs |
| 4 | **RAG chunks** | sanitized text from execution failures, pipeline definitions, catalog, docs | `data/rag.db` (SQLite + sqlite-vec) | Medium — same sanitization gate as tool results |
| 5 | **Audit metadata** | provider, model, latency_ms, tokens_in/out, success, action | `audit_log` table | Low — no payload, only operational counters |
| 6 | **Trace records** | `run_id`, step list (tool_name, input_hash, output_hash, outcome), elapsed_ms | `agent_traces` table | Low — replay-safe; counts and hashes only, no raw values |

**Invariant (enforced by sanitize.py):** classes 3 and 4 NEVER reach class 6 raw. Trace records hold SHA-256 hashes of canonicalized JSON, not the JSON itself.

---

## 2. Retention rules

Each store has an explicit lifetime. Defaults shown; environment variables override.

| Store | Default retention | Env var | Deletion behavior |
|---|---|---|---|
| `agent_traces` (trace store) | **90 days** | `FPULSE_TRACE_RETENTION_DAYS` | Daily archive to Parquet under `data/archive/traces/`, then DELETE from SQLite. Set `0` to disable archive (purge only). |
| `audit_log` (AI calls + tool calls) | **90 days** | `FPULSE_AUDIT_RETENTION_DAYS` | Same archive-then-purge pattern as trace store. |
| `rag_docs` (vector store) | **rebuilt nightly** | `FPULSE_DISABLE_RAG=1` to disable | Daily 03:00 UTC indexer re-derives the corpus from execution_logs / workflows / catalog / docs. The store is the index, not the source — deleting `rag.db` is safe; it rebuilds. |
| `ai_wallet_usage` (token caps) | **rolling 30 days** | `FPULSE_WALLET_RETENTION_DAYS` | Older rows purged daily during retention sweep. Today's row is always preserved. |
| `ai_dry_run_counters` (per-user write tool counters) | **indefinite** | — | One row per `(user_id, tool_name)`. Cleared only when the user is deleted or via admin endpoint. |
| `idempotency_cache` (write-tool replay protection) | **24 hours** | `FPULSE_IDEMPOTENCY_TTL_S` | Bounded in-memory LRU + TTL; never on disk. Lost on restart (acceptable: re-issue of an old idempotency key on a fresh process is rare and the action re-executes). |
| `semantic_cache` (LLM response cache) | **30 minutes** | `FPULSE_SEMANTIC_CACHE_TTL_S` | In-memory only; lost on restart. Tenant-prefixed; never crosses workspaces. |
| `ai_config` (provider keys) | **until user clears** | — | Encrypted at rest with Fernet. Plaintext key never returned via HTTP. "Clear key" button writes empty string. |

### User-initiated deletion

- **DELETE workspace** → cascade-deletes all six stores' rows scoped to that workspace_id.
- **DELETE user** → cascade-deletes per-user rows in `ai_wallet_usage`, `ai_dry_run_counters`, `ai_config` (user row).
- **POST /api/ai/agent/traces/{run_id}/forget** → admin-only, removes one trace + its archive entry.

### What we do NOT keep

- **Raw tool-call payloads.** Never persisted. Hashed in trace; original drops out of memory at request end.
- **Raw LLM completion text** beyond `final_text` in the trace store (which is sanitized via `sanitize_for_llm`).
- **User chat history across sessions.** No conversational memory. Page context + RAG are the only "what's relevant now" inputs.
- **Embedding training data.** F-Pulse never ships customer data to a model trainer. Provider clients send `store: false` (OpenAI), and we do not consent to retention on Anthropic.

---

## 3. Failure modes & degradation behavior

What happens when something breaks. Every row is a tested degradation path, not an aspiration.

| Failure | Behavior | User sees |
|---|---|---|
| **Provider timeout** (LLM call > 120s) | Run aborts with `outcome="timeout"`, trace persisted, wallet usage recorded for partial tokens | Chat shows "Agent timed out — try a more specific question" |
| **Provider error** (5xx, auth, network) | `outcome="llm_failure"`, decision_reason includes exception type + 160 chars of message | Chat shows the error type so users can act ("network error", "model does not support tools") |
| **Malformed tool result** (schema validation fails) | Step recorded as `outcome="tool_failure"`, decision_reason="Schema violation: …", LLM is told the tool errored and asked to recover | Run continues; user sees "tool returned unexpected shape" if it propagates |
| **Tool not in allowed_tiers** | Step recorded as `outcome="policy_block"`, `policy_rules_fired=["tool_not_in_allowed_tiers"]`, run continues | Tool result shows as denied; LLM may pick another approach |
| **RBAC denial** | Same as above with `policy_rules_fired=["rbac:role_X_cannot_invoke_Y_in_Z"]` | "You don't have permission to do that in PROD" |
| **Wallet cap exceeded** | Run never starts; returns `outcome="policy_block"`, decision_reason="daily token cap exceeded" | "You've hit today's AI budget — try again tomorrow or raise the cap in Settings" |
| **Embedder unavailable** (Ollama down, model not pulled) | RAG silently disabled for this turn; agent runs without retrieved context. No error surfaced. | No visible change; logs note "Embedding provider not available; skipping RAG indexing" |
| **RAG miss** (≥3 strong matches not found) | Context block omitted; agent proceeds with tool-only access | No visible change |
| **Cache poisoning** (semantic cache returns stale or wrong response) | Mitigated by tenant-prefixed keys + tool-using responses bypassing cache + 30-min TTL. If suspected, restart F-Pulse to clear. | N/A unless suspected |
| **Approval expiry** (PROD write requires fresh approval; >24h old) | Policy engine fires `policy_block`, run halts before write | "This change needs a fresh approval — submit again" |
| **Duplicate write** (same idempotency key replayed) | Cache hit returns prior result; handler is NOT re-invoked | "Already done" rather than a re-run |
| **Prompt signature mismatch** (someone monkeypatched SYSTEM_PROMPT_TEMPLATE) | Run halts before any LLM call; trace records `prompt_signature_mismatch` | "Agent halted: system prompt integrity check failed" — operator-visible alarm |
| **Trace store write fails** | Best-effort path; the run result is returned to the user normally; the trace is lost | No user impact; an operator monitoring SQLite logs would see the warning |
| **RAG indexer hits an exception mid-run** | Workspace's index may be partial; next-day run rebuilds | None — recall_history just returns whatever is indexed |

**Hard rule:** no failure path silently writes data. A failed write either succeeds, returns a clean error, or replays from the idempotency cache. There is no "partial write" outcome.

---

## 4. Operator mental model

The shortest version a new operator needs to be useful in 10 minutes.

### One sentence

> F-Pulse AI is 16 tools the user can authorize an LLM to call on their behalf, with sanitization at the boundary, governance gates between the LLM and any side effect, and a daily-rebuilt retrieval layer that grounds answers in the user's own pipeline history.

### One diagram

```
USER
  │ types in Copilot
  ▼
[PageContext snapshot]            ← what's visible right now
  │
  ▼
POST /api/ai/agent  ─→  AgentRunner
                          │
                          ├─ verify prompt signature
                          ├─ wallet pre-flight
                          ├─ build prompt (intent + ctx + RAG)
                          ├─ LOOP (≤6 iters, ≤120s):
                          │     │
                          │     ├─ call LLM (provider-agnostic)
                          │     │
                          │     └─ for each tool_use:
                          │           RBAC → policy → idempotency
                          │              → sanitize-in → handler
                          │              → schema validate → sanitize-out
                          │
                          ├─ record token usage in wallet
                          ├─ persist trace
                          └─ audit_log row
                          ▼
SSE: token | step | done events  ─→  Copilot UI
                          ▼
ConfirmationCard (for any write)  ─→  user clicks Confirm
                          ▼
                   side effect happens
```

### Three rules of thumb

1. **The LLM is a stateless reasoning consultant, never a database user.** It sees only what tools return, after sanitization.
2. **Every side effect is user-confirmed.** The agent drafts; the user applies. Dry-run-by-default backstops new write tools.
3. **Every cost is recorded.** Tokens, latency, decision_reason, policy_rules_fired. If you can't see it on `/ai/activity` or in `audit_log`, it didn't happen — open a ticket.

### What to look at when something is wrong

| Symptom | First place to look |
|---|---|
| User says "agent didn't do X" | `/ai/activity` → filter by user → expand the run → check `outcome` and `policy_rules_fired` |
| Costs spiking | `audit_log` aggregated by `(provider, model, source)` over last 24h |
| Provider errors | `audit_log` rows with `success=false` — error column has type + first 500 chars |
| RAG returning bad context | Restart with `FPULSE_DISABLE_RAG=1` to confirm RAG is the cause; rebuild `data/rag.db` |
| Slow responses | `audit_log.latency_ms` distribution; check semantic_cache hit rate; confirm provider region |
| Trace gaps | trace_store writes are best-effort — check stderr for "Trace persistence failures must never propagate" warnings |

### When to escalate to engineering

- Prompt signature mismatch in production (system tampered or upgrade botched)
- Wallet usage growing without traffic (loop bug — denial of wallet)
- Schema violations on the same tool repeatedly (handler returning wrong shape — tool needs a fix, not a retry)
- Idempotency cache returning stale results across restarts (shouldn't happen — in-memory store)

---

## 5. Tool sensitivity tiers (current)

The current tier system is mutability-based: `READ` / `SAFE_WRITE` / `HIGH_IMPACT_WRITE`.

**Known gap (post-launch refactor):** sensitivity-based tiers — `sensitive_read`, `derived_read`, `write_with_external_side_effect` — would let governance rules express "list_executions is fine but inspect_connections needs admin even though both are READ." Today this is enforced via per-tool RBAC in `rbac.py`, not via a tier. We will lift it into the tier enum once we have a real second sensitive-read tool to motivate the taxonomy.

---

## 6. Compliance posture

What an auditor needs to know in one paragraph.

> F-Pulse runs the LLM as a tool-using consultant, never as a data principal. All payloads sent to the LLM pass through `sanitize_for_llm` which drops credentials, PII, and oversize blobs and applies tenant-scoped redaction patterns. Every LLM call is logged to `audit_log` with provider, model, latency, token count, and success status. Every agent run is logged to `agent_traces` with replay-safe step records (input/output hashes only, never raw values). Workspace data is isolated at every store via `workspace_id` indexing. Encryption at rest uses Fernet for provider keys; the master key lives in `~/.fpulse/secret.key`. Retention defaults to 90 days for traces and audit, 30 days rolling for wallet usage, with explicit override env vars and DELETE-on-workspace-delete cascade. The system never trains models, never sends data to embedding training feeds, and respects `store: false` on OpenAI / no-retention agreements on Anthropic.

---

## 7. Document maintenance

This is a **living** document. Update it when:

- A new store is added (rows in §1, §2)
- A new failure path is observed in production (row in §3)
- Retention defaults change (§2)
- A new provider is added with different retention behavior (§6)

**Owner:** AI architecture lead.
**Last reviewed:** 2026-05-01.
**Cadence:** Reviewed at every minor release; mandatory review before any v2.0 release.
