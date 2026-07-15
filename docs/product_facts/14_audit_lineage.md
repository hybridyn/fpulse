# F-Pulse audit, lineage, and governance

Per `edition-matrix.md` line 137-144 (audit, governance, compliance) and line 96-107 (agent observability).

## Three audit surfaces in OSS Free

### 1. Application `audit_log` table — basic

Captures every authenticated action: who, what, when, IP, status. Used for security review and "who changed this pipeline?" investigations.

- Persists in the SQLite database
- Indexed by `(user_id, timestamp DESC)` for "recent activity by user" queries
- Retention is **best-effort in OSS** — no enforced retention policy. Operators rotate manually if disk pressure becomes an issue.

**Where to view:** Insights → Activity tab. Filters by user, action type, date range.

### 2. Agent trace store — every Copilot run

Per `edition-matrix.md` line 103: "Trace store (every run persisted with replay-safe step records) ✅ ✅".

Every agent run captures:
- `run_id` (UUID)
- `user_intent` (the prompt)
- `outcome` (success / llm_failure / tool_failure / policy_block / timeout / user_rejection)
- Per-step: `tool_name`, input/output **hashes only** (never raw values per AI_BOUNDARY_CONTRACT §2), latency, tokens in/out, redactions count
- Total tokens + estimated cost

**API:** `GET /api/ai/agent/traces?limit=20`, `GET /api/ai/agent/trace/{run_id}` for the full detail.

**OSS retention:** in-process, kept in SQLite alongside the audit log. No retention policy enforced.

### 3. Execution log — every pipeline run

Per `04_concepts.md` Execution section. Each run captures status, duration_ms, peak_memory_mb, cpu_seconds, parameter_values, workflow_snapshot, exit_reason. Immutable once written.

**Where to view:** Executions page. Click a row for the metadata.

## F-Pulse+ adds

### Audit log retention + export + sigstore signing

- **Retention policy** — operator sets per-store retention (e.g. "keep audit_log for 7 years for SOC 2"). Auto-archives to Parquet on S3/GCS before deletion.
- **Export** — `POST /api/plus/audit/export` returns a sigstore-signed tarball. SIEM-compatible formats (JSON Lines, CSV).
- **Sigstore signing** — every export tarball is signed via a Hybridyn-issued sigstore certificate. Auditors can verify integrity without trusting the Hybridyn API.

### Trace store extended retention (1 year+)

Same trace data as OSS, but retained 1 year+ with admin-controlled retention overrides per store.

### Lineage (Marquez-compatible)

Cross-pipeline dataset provenance graph. For each pipeline run, F-Pulse+ emits an OpenLineage event to a configured backend (default: a built-in Marquez instance). Lets you answer "where did the rows in this dashboard come from?" by walking upstream.

**Compatible with:** Marquez, DataHub, OpenMetadata, any OpenLineage-compliant backend.

### Vault (AES-256, references, rotation)

Per `edition-matrix.md` line 142. Plus replaces local-encrypted credentials with **Vault-Ref** — credentials live in HashiCorp Vault / AWS Secrets Manager / Azure Key Vault / Google Secret Manager. F-Pulse+ stores only a reference (`vault:secret/data/foo`) that resolves at runtime.

Plus also adds **rotation hooks** — F-Pulse+ subscribes to vault rotation events and updates running pipelines without restart.

### Export to other platforms

F-Pulse+ can export pipelines as **signed migration bundles** via a documented bundle format.

### Compliance posture document

OSS ships `docs/ai-ops-contract.md` and `docs/compliance.md` (the compliance one-pager). Plus adds **SOC 2 Type II attestation** and a Data Processing Agreement (DPA) suitable for procurement reviews.

## Anti-patterns

- ❌ Telling an OSS user "F-Pulse is SOC 2 certified" — the SOFTWARE isn't certified; the OPERATOR's deployment may bring F-Pulse into scope of the operator's own SOC 2. Plus customers get the additional Hybridyn SOC 2 Type II attestation report on request.
- ❌ "Use the Activity timeline as your forensic audit log" — it's the right starting point, but it's the SHARED audit + agent + execution feed combined. For forensics on the agent specifically, use the trace store API. For pipeline state changes, the audit_log table.
- ❌ Promising lineage in OSS — it's Plus only. The OSS path for "where did this data come from" is `recall_history` against the RAG index plus `summarize_pipeline` on each upstream node.
- ❌ Confusing audit log with execution log — they are different stores. Audit log = who did what (auth-scoped). Execution log = pipeline runs (system-scoped).

## Where to look for evidence

| Question | Where (OSS) | Plus extension |
| --- | --- | --- |
| Who changed pipeline X? | `audit_log` table, filter by entity_id | + sigstore-signed export |
| What did the agent do at 14:32? | `GET /api/ai/agent/traces?limit=20` | + 1-year retention |
| Why did this run OOM? | Executions page → run row → `peak_memory_mb` | (same — retention extended) |
| Where did this row come from? | `recall_history` + `summarize_pipeline` | + Marquez lineage graph |
| Are credentials encrypted? | `GET /api/trust/posture` → `security_baseline.credential_encryption.status = ok` | + Vault-Ref pattern |
