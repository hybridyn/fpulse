# Triggering pipelines from external systems

This guide is for integrators wiring F-Pulse pipelines into CI jobs, peer services, scheduled tasks in other tools, or public webhooks. If you just want to "run a pipeline by hand from the UI", that's the **Run** button on the Pipelines page — this guide isn't for you.

There are two ways to invoke an F-Pulse pipeline from outside the product:

1. **Authenticated API** — for systems that already authenticate against F-Pulse (CI workers with a service account, peer services on the same network, ops tooling).
2. **Public webhook (Gateway)** — for systems that need a stable URL + API key (SaaS webhooks, marketing automation, customer-facing form submissions).

Both can pass parameters that the pipeline uses at runtime. Both flow through the same execution engine with the same precedence rules.

---

## 1. Authenticated API

**Endpoint:** `POST /api/execute/workflow/{workflow_id}`

The request body is optional. The UI's "Run" button posts no body. External callers add a `params` object to drive the pipeline's variables for that one run.

### Minimal example

```bash
curl -X POST 'https://your-f-pulse.example.com/api/execute/workflow/wf_abc123' \
  -H 'Content-Type: application/json' \
  -H 'X-Workspace-Id: my-workspace' \
  -d '{
    "params": {
      "REGION": "eu-west-1",
      "RUN_DATE": "2026-05-12"
    }
  }'
```

### Step configuration can reference these via `{{ $vars.NAME }}`

```text
db_source:
  query: SELECT * FROM orders WHERE region = '{{ $vars.REGION }}' AND order_date = '{{ $vars.RUN_DATE }}'
```

### Query-string knobs (compatible with the UI)

These remain in the URL, not the body:

| Knob | Default | Effect |
|---|---|---|
| `full_run` | `false` | When `true`, source nodes skip the dev sample-row limit and process the entire dataset. |
| `preview_limit` | `50` | Rows to retain in each step's preview slot. |

### Response

Same shape as a UI-triggered run — see [API reference: Execution Response](API.md#execution-response).

---

## 2. Public webhook (Gateway)

If the calling system can't authenticate against F-Pulse (e.g., a SaaS posting a webhook, a marketing form), publish the pipeline as a public endpoint via the Gateway. The Gateway issues an API key and exposes a stable URL.

**Endpoint:** `POST /api/published/{your-custom-path}`

The request body **is** the params dict (no `{ "params": ... }` wrapper).

```bash
curl -X POST 'https://your-f-pulse.example.com/api/published/customer-onboarding' \
  -H 'X-API-Key: fpk_live_3a8c...' \
  -H 'Content-Type: application/json' \
  -d '{
    "CUSTOMER_ID": "cust_42",
    "TIER": "enterprise"
  }'
```

### When to use which

| Question | Use this path |
|---|---|
| Caller is a CI job using a service account on my network | Authenticated API |
| Caller is a third-party SaaS that only sends webhooks | Gateway |
| Need rate-limiting on the trigger | Gateway (per-key + per-endpoint) |
| Need to expose a stable URL like `/api/published/customer-onboarding` | Gateway |
| Caller can send the F-Pulse workspace header | Authenticated API |
| Caller is on the same machine as F-Pulse | Authenticated API |

---

## How parameters reach the pipeline

When a step references `{{ $vars.NAME }}`, F-Pulse looks up `NAME` in this order. The first non-empty value wins:

1. **Caller-supplied `params`** (this run only — never persisted)
2. **Stored variables** (Settings → Variables)
3. **The workflow's declared `inputs[*].default`** (if the pipeline defines an inputs schema)

Order matters: a caller passing `{"REGION": "eu-west-1"}` overrides a stored `REGION` value for that one invocation. The stored value isn't touched.

---

## Declaring inputs (optional but recommended)

A pipeline can declare what parameters it accepts. This adds two things:

1. **Self-documentation** — integrators reading the pipeline JSON see exactly what to send.
2. **Validation** — if `required: true` and the caller doesn't supply a value AND no stored variable or default exists, the run fails with a clean error before any step executes.

```json
{
  "name": "Regional sales pipeline",
  "inputs": [
    { "name": "REGION",     "type": "string",  "required": true,  "description": "AWS region for the source DB" },
    { "name": "BATCH_SIZE", "type": "number",  "required": false, "default": 1000 },
    { "name": "DRY_RUN",    "type": "boolean", "required": false, "default": false }
  ],
  "steps": [ /* ... */ ],
  "connections": [ /* ... */ ]
}
```

Inputs are optional. A pipeline with no `inputs` field accepts any keys the caller passes — useful for ad-hoc pipelines but harder for integrators to use.

### Error response when a required input is missing

```json
{
  "workflow_id": "wf_abc123",
  "status": "error",
  "step_results": {
    "inputs": {
      "step_id": "inputs",
      "status": "error",
      "error": "Required workflow input(s) missing: REGION"
    }
  }
}
```

---

## Idempotency and overlap

If two callers trigger the same pipeline at the same time, the execution engine's overlap policy (configured on the workflow's metadata) decides what happens:

| Policy | Behavior |
|---|---|
| `parallel` (default) | Both runs proceed. |
| `skip` | The second caller gets `status: "skipped"` with the running run ID. |
| `queue` | The second caller gets `status: "queued"`. The system runs it when the first completes. |
| `cancel_previous` | The first run is cancelled; the new caller proceeds. |

For external triggers, `skip` or `queue` is usually safer than `parallel`.

---

## Common patterns

### CI: trigger nightly with a date

```yaml
# Example CI step
- name: Run F-Pulse nightly pipeline
  run: |
    curl -fsS -X POST 'https://your-f-pulse.example.com/api/execute/workflow/wf_nightly?full_run=true' \
      -H 'Content-Type: application/json' \
      -H 'X-Workspace-Id: ${{ secrets.FPULSE_WORKSPACE }}' \
      -H 'Authorization: Bearer ${{ secrets.FPULSE_TOKEN }}' \
      -d '{"params": {"RUN_DATE": "${{ github.event.repository.updated_at }}"}}'
```

### Polling for completion

`POST /api/execute/workflow/{id}` is synchronous — the response returns when the run completes. If you need fire-and-forget, post and then poll `/api/monitor/executions?workflow_id=...` for the latest record.

### Multi-tenant pipelines

Same pipeline definition, different tenant per invocation:

```bash
curl -X POST '.../api/execute/workflow/wf_tenant_sync' \
  -d '{"params": {"TENANT_ID": "acme-corp"}}'
```

The variables store can hold tenant-agnostic defaults (database hosts, etc.). The caller fills in just `TENANT_ID`. The same pipeline serves every customer.

---

## What this is NOT

- **Not a queueing system.** F-Pulse runs the pipeline immediately when you POST. If you want long-running asynchronous workflows with retries, use the Gateway's rate-limiting + your own queue in front of it.
- **Not for high-frequency calls.** Source nodes spin up DuckDB; this is heavyweight per request. Don't call this from a per-event hot path.
- **Not for storing per-call state.** `params` are transient. If you need to remember a value across runs, set it as a variable (Settings → Variables) or write it from inside the pipeline.

---

## See also

- [API reference: Execution](API.md#execution) — every endpoint flag and the full response schema
- [API reference: Variables](API.md#variables) — CRUD endpoints and scoping rules
- [Pipelines guide](pipelines.md) — building and publishing pipelines
