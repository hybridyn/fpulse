# F-Pulse Trust Pillars

Three pillars that define how F-Pulse handles AI, why you can rely on it, and what evidence we provide for each claim.

This is the **user-facing** summary. For implementation details, see the [AI boundary contract](ai-boundary-contract.md) and [Performance budgets](performance.md).

The deterministic kernel, sanitization gateway, dry-run defaults, replay-safe trace, and confirmation cards are all live in this build.

---

## Pillar 1 — Deterministic core, probabilistic support

### The claim
F-Pulse is a **code-first** pipeline orchestrator. AI assists; AI never decides. Every action that touches your data passes through deterministic code paths. The AI layer suggests; humans confirm — via the inline confirmation card before any write.

### How it works
- The core execution engine is deterministic Python — no LLM in the data path. AI enters your data flow only if you explicitly add an AI node (e.g. AI Enrich / Embedder); every built-in source, transform, and sink runs no model.
- AI features (Builder Co-Pilot, transform helper, root-cause analysis, pre-publish review, mailing summaries) produce **suggestions** rendered as cards. Nothing is written to your environment without an explicit confirmation step.
- Every AI-callable tool falls back to a deterministic implementation when no LLM provider is configured. F-Pulse remains fully functional in zero-LLM mode.
- High-impact write tools (create_schedule, send_to_destination, publish, alert changes) default to **dry-run mode** until they pass an internal reliability threshold.

### Proof artifacts
| Artifact | Where |
|---|---|
| Tool tier registry showing which tools are read / safe-write / high-impact-write | `backend/fpulse/ai/tool_registry.py` |
| Confirmation card behavior in pre-publish review | `docs/user-guides/projects.md`, `docs/user-guides/pipelines.md` |
| Test suite proving deterministic fallback works without an LLM | `backend/tests/ai/` |
| Architecture invariant: no agent write without idempotency + confirmation | `backend/tests/architecture/test_invariants.py` |

---

## Pillar 2 — Data sovereignty

### The claim
Your data does not leave your environment unless you explicitly enable a managed LLM provider. When you do enable one, you control exactly what fields can be sent, you get the no-training guarantee, and you can switch back to a local model at any time.

### How it works
- **Two default deployment modes:**
  1. **Zero-LLM:** all AI features fall back to deterministic implementations. No external calls.
  2. **BYO key:** your own Anthropic / OpenAI / Ollama key. F-Pulse routes calls through your account using your credentials.
- **Per-tool field allowlist** enforced in `sanitize_for_llm()`. Only fields declared in each tool's schema can be sent. The full per-tool send-rules table lives in the [AI boundary contract](ai-boundary-contract.md).
- **PII redaction** applies on input AND output. Configurable redaction patterns.
- **No-training flags** set on every API call to managed providers. Provider list reviewed each release.
- Credentials are encrypted at rest; the same redaction rules keep them out of LLM payloads.

### Proof artifacts
| Artifact | Where |
|---|---|
| Per-tool send-rules table | `docs/ai-boundary-contract.md` section 2 |
| Universal denylist | `docs/ai-boundary-contract.md` section 3 |
| `sanitize_for_llm()` implementation | `backend/fpulse/ai/sanitize.py` |
| Provider no-training configuration | `backend/fpulse/planner/ai_client.py` |
| Architecture invariants for size caps and tenant isolation | `backend/tests/architecture/test_invariants.py` |

---

## Pillar 3 — Full observability

### The claim
Every AI suggestion and every tool call is logged with a trace ID. You can replay any agent run, see exactly what was sent and what came back (as hashes — never raw values), and inspect the trace inline.

### How it works
- **Replay-safe trace per agent run.** Every step records: `tool_name`, `tool_tier`, `input_hash` (SHA-256 of canonicalized input), `output_hash`, `timestamp`, `latency_ms`, `tokens_in`, `tokens_out`, `decision_reason` (up to 120 chars), `redactions_applied` (counts and categories only — never raw values), `outcome` (one of: success / llm_failure / tool_failure / policy_block / timeout / user_rejection), `policy_rules_fired`.
- **Tool-call audit log** is append-only.
- **Cost visibility per run** rendered inline (`~{N} tokens · ~${cost}`).
- **Trace retention** is best-effort, capped by SQLite size.

### Proof artifacts
| Artifact | Where |
|---|---|
| Trace schema definition | `docs/ai-boundary-contract.md` section 8 |
| Trace store implementation | `backend/fpulse/ai/trace_store.py` |
| Trace UI panel | `frontend/src/components/AgentTracePanel.tsx` |
| Cost indicator in agent response | inline in chat UI |
| Architecture invariant: no tool call without an audit event | `backend/tests/architecture/test_invariants.py` |

---

## What this trust framework gives you

| You ask | We answer | Evidence |
|---|---|---|
| "Is your AI deterministic?" | The kernel is. AI suggests; deterministic code executes. | Pillar 1 |
| "Where does my data go?" | Only what's in the per-tool send-rules table; with no-training flags set | Pillar 2 |
| "Can I audit?" | Every run produces a hashed, inspectable trace | Pillar 3 |
| "What if AI does the wrong thing?" | Confirmation gate before every write; full trace; idempotency on writes; dry-run mode default | Pillars 1 + 3 |
| "What if my LLM provider fails?" | Deterministic fallback runs; no degradation of pipeline execution | Pillar 1 |

---

## What this framework does **not** claim

- We do not claim AI suggestions are always correct. They are suggestions. Confirmation gates exist because we know suggestions can be wrong.
- We do not claim a local LLM matches frontier-model quality. Smaller models are weaker; the trade-off is sovereignty.
- We do not claim zero risk under prompt injection. We claim defense-in-depth: trust-boundary invariant, schema validation, output redaction, confirmation gates. Risk reduction, not elimination.
- We do not claim PII redaction is exhaustive. You should configure additional patterns specific to your data classification.
