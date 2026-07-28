# F-Pulse AI Boundary Contract

**Status:** Locked 2026-04-29. Source of truth for what the AI layer may see, what it must never see, and how trust boundaries are enforced.

This contract is enforced by `backend/tests/architecture/test_invariants.py` (10 rules) and supported by the [Trust posture](TRUST.md) and [Performance budgets](PERFORMANCE.md) documents.

---

## 1. Trust boundary

There is exactly **one** trusted source of instructions: the F-Pulse system prompt, signed at startup.

Everything else is **untrusted data**. This includes:

- User chat input
- Tool call outputs
- RAG retrieved chunks (F-Pulse docs, connector docs)
- Pipeline configuration (node names, descriptions, comments)
- Connection metadata (host names, schema names, table names)
- Run logs and error messages
- External documents fetched via HTTP tools
- Any field originating outside the signed system prompt

Per OWASP LLM Prompt Injection Prevention guidance, untrusted data must be **wrapped and labelled as data, never instructions**, and the model must be told it cannot follow instructions found inside data sections.

> **Rule:** Signed system prompts do not make tool/RAG/external content trusted. The signature protects the rules; it does not bless the data.

---

## 2. What gets sent to the LLM (per tool)

Every tool in the registry declares its input schema. Only fields in the schema are sent. Anything not in the schema cannot reach the LLM, ever.

The send-rules table below is normative. New tools must extend this table before merging.

| Tool | Sent to LLM | Never sent |
|---|---|---|
| `summarize_pipeline(pipeline_id)` | Node count, node-type histogram, source/destination connector types, alert config presence, last run status | Connection credentials, full node config, sample data, row contents |
| `inspect_connections(pipeline_id)` | Connection name, connector type, last test status, last test timestamp | Username, password, API key, host, port, database name |
| `query_metrics(scope, keys, window)` | Metric key, value, window, scope label | Raw row data, dataset contents |
| `compose_report(template, data, format)` | Report template name, summary statistics | Underlying row data |
| `diagnose_error(execution_id)` | Error class, error message (first 500 chars, PII-redacted), node type, recent step names | Stack trace beyond top frame, full log file, credentials in any form |
| `explain_code(node_id)` | Node SQL or Python source, upstream column names + types | Sample row values, secret values, env vars |
| `generate_code(node_id, intent, upstream_schema)` | Intent string, upstream column names + types, target schema | Sample data, secrets |
| `auto_fill_config(node_type, upstream_schema)` | Node type, upstream column names + types | Sample values |
| `compute_approval_plan(pipeline_id, target_env)` | Pipeline ID, env, current approval state | (no LLM call — deterministic) |
| `check_connector_health(connector_id)` | Connector type, last test status, latency | Credential fields |
| `draft_connector_from_openapi(connector_id, openapi_url\|openapi_text\|openapi_spec)` | The OpenAPI spec's structure (paths, schemas, auth *scheme types*), discovered endpoint names | API keys / tokens — the manifest holds auth **templates** only (e.g. `Bearer {token}`); the real secret is entered later on the Connection |
| `web_fetch(url)` — opt-in | The public URL, and the fetched page text (≤1 MB, public hosts only) | Anything on a private/loopback/metadata host (SSRF-blocked); no request body is sent |
| `web_search(query, count)` — opt-in | The search query string | (query goes to the operator-configured search provider only when web access is explicitly enabled) |

### Opt-in web access (default OFF)

The Copilot has **no web access by default** — F-Pulse is local-first. The
`web_fetch` and `web_search` tools are registered only when an admin turns web
access on — either via **Settings → AI Provider → "Copilot web access"** (a
live toggle, no restart) or the `FPULSE_AI_WEB_ACCESS=1` env var; until then the
LLM never sees them. `web_fetch` is
SSRF-hardened (private/loopback/metadata hosts blocked unless
`FPULSE_AI_WEB_ALLOW_PRIVATE=1`) and capped at 1 MB. `web_search` calls only the
search provider the operator configures (`FPULSE_WEB_SEARCH_PROVIDER` +
`FPULSE_WEB_SEARCH_API_KEY`); with none configured it returns a "not configured"
message rather than reaching any network.

### Universal redaction (applied AFTER per-tool field selection, BEFORE LLM call)

Even within allowed fields, the following patterns are redacted to `[REDACTED:CATEGORY]`:

- Email addresses
- Phone numbers (E.164 + national formats)
- Credit card numbers (PCI patterns)
- Aadhaar numbers (12-digit Indian ID)
- Social security numbers
- IP addresses
- Bearer tokens / API keys (heuristic: 32+ char base64 / hex strings)
- Anything matching the workspace's configured PII regex list

Redaction is applied on **input AND output**. The output gate prevents the LLM from accidentally surfacing PII it inferred from context.

---

## 3. What never gets sent — global denylist

These never reach the LLM, regardless of tool or schema:

- Plaintext credentials (username, password, token, key, secret)
- Vault references resolved to values
- Sample row data (use schema-only)
- Full execution logs (truncate to first 500 chars per error, PII-redacted)
- Approval signatures, license signing keys, HMAC secrets
- Any field whose name matches `(?i)(password|secret|token|api_key|private_key|signing_secret)`
- Customer data classified as L3 or L4 (see DataClassification module)

Enforcement: `sanitize_for_llm(payload)` runs at every LLM-call site. Bypass requires explicit `_unsanitized=True` kwarg + audit log entry + workspace-admin role + dev-only env var. There is no production bypass.

---

## 4. No-train guarantee

F-Pulse sends LLM requests with provider-specific no-training flags set:

| Provider | Mechanism |
|---|---|
| Anthropic | API request includes `metadata: {disable_training: true}` where supported; default zero-retention for API tier |
| OpenAI | Account-level data-sharing opt-out; per-request `store: false` |
| Ollama | Local — never leaves the host |
| Azure OpenAI | Tenant-bound; default no-training |

We do not use providers that train on API traffic by default. The provider list is reviewed each release.

---

## 5. On-prem LLM option (Plus high-tier)

For customers in regulated industries (healthcare, finance, EU public sector), F-Pulse Plus offers an on-prem LLM path:

- Bundled Ollama runner with vetted small models (Llama 3 8B, Mistral 7B)
- All AI calls route to `localhost`; no network egress for LLM traffic
- Hardware requirement: ~20 GB VRAM per active session at 8K context (KV cache ~2.5 MB/token)
- Reduced capability — small models are weaker than frontier models, but acceptable for the per-tool scope above

Selection is a workspace setting (`ai_provider: ollama_onprem`). The boundary contract above is identical; only the delivery is local.

---

## 6. Data minimization gateway

`sanitize_for_llm(payload)` is the single chokepoint. It applies, in order:

1. **Field allowlist** — drop any field not in the tool's input schema
2. **Size caps** — truncate to the tool's per-output token cap (2K Free / 4K Plus)
3. **PII redaction** — apply universal denylist patterns
4. **Workspace policy** — apply workspace-configured PII regex list
5. **Schema normalization** — `normalize_tool_output(tool_name, result)` reshapes to the tool's declared output schema

Same gateway also runs on tool **output** before it returns to the agent loop. Output that fails schema validation is rejected; agent sees a `tool_failure` outcome, not a malformed payload.

---

## 7. Cache policy

Caches obey strict tenant isolation. Key prefix is always `{tenant_id}:{cache_type}:{key}`. Cross-tenant key collision is a CI assertion failure (Rule 8).

What may be cached:
- Safe derived summaries (node counts, type histograms, schema digests)
- LLM responses for deterministic prompts (input_hash → output)
- Connector metadata, schema introspection

What may **not** be cached:
- Raw tool returns containing operational secrets
- Sample row data (always re-fetch)
- Anything originating from a Vault resolution

TTLs:
- Schema introspection: 6h, invalidate on connection update
- Pipeline summary: 1h, invalidate on pipeline save
- Metrics summary: 5min, invalidate on run completion
- Connector metadata: 24h, invalidate on connector config change
- Connection health: 15min, invalidate on test re-run
- LLM response (deterministic prompts): 30min, invalidate on prompt template version bump

---

## 8. Trace contract

Every agent run produces a trace with the following per-step fields:

- `step_id` (UUID)
- `tool_name`
- `tool_tier` (`read` / `safe_write` / `high_impact_write`)
- `input_hash` (SHA-256 of canonicalized input — never raw input)
- `output_hash` (SHA-256 of canonicalized output)
- `timestamp` (ISO 8601 UTC)
- `latency_ms`
- `tokens_in` / `tokens_out`
- `decision_reason` (≤120 chars)
- `redactions_applied` (`{count, categories[]}` — counts only, never values)
- `outcome` (`success` / `llm_failure` / `tool_failure` / `policy_block` / `timeout` / `user_rejection`)
- `policy_rules_fired` (string[] when outcome=policy_block)

Traces are durable for 90 days minimum (configurable per workspace), exportable as evidence for SOC2 / DORA reviews, and contain no raw PII.

The trace is the audit artifact that backs every customer claim in `docs/TRUST.md`.

---

## 9. Boundary review process

This contract is reviewed:
- Before any new tool is added to the registry
- Before any new LLM provider is enabled
- Quarterly even if no changes
- Whenever a security report references AI data flow

Changes require a PR with:
- Updated send-rules table
- Updated denylist if expanding what's never sent
- Updated `tests/architecture/test_invariants.py` if rules shift
- Workspace-admin sign-off if the change loosens defaults

---

## 10. What this contract does **not** cover

- Customer-supplied prompts to their own BYO LLM key — F-Pulse cannot police what a customer's own provider does with their own key. The contract above applies to F-Pulse-managed providers only. Customers using BYO keys should review their provider's TOS independently.
- Network-layer attacks between F-Pulse and the LLM provider — TLS is enforced, but link compromise is the customer's network problem.
- Adversarial prompts inside customer pipeline configurations — defended by the trust-boundary invariant above; cannot be defended against perfectly.
