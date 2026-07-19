# Example: a webhook-triggered pipeline

Fire a pipeline the instant an external system POSTs to it, and use the JSON
body as pipeline inputs. This is the event-driven ("near-real-time") trigger
in F-Pulse — an upstream app calls a URL, F-Pulse runs the pipeline right then.

**How it works, end to end:**

```
POST /api/published/orders-webhook          (external system, with an API key)
  → gateway validates the API key + rate limit
  → JSON body becomes the run's parameter_values
  → ${param.<name>} placeholders in the pipeline resolve to those values
  → the pipeline executes; rows land in a managed table
```

The body-to-pipeline binding is verified: a body of
`{"order_id":"ORD-1001","customer":"Acme Corp","amount":1500}` resolves the
source query to `SELECT 'ORD-1001' AS order_id, 'Acme Corp' AS customer,
CAST(1500.0 AS DOUBLE) AS amount, ...` and the run returns `status: success`.

---

## 1. The pipeline

Two nodes: a **DB Source** in inline-SQL mode (no connection → runs on the
built-in in-memory DuckDB engine) turns the webhook body into a row and does
the light transform; a **Local Table Sink** appends it to a managed table.

Save this as `webhook-order-pipeline.json` and import it (Pipelines →
Import), or build the two nodes in the editor and copy the params.

```json
{
  "id": "webhook-order-demo",
  "name": "Webhook: Order Event → Managed Table",
  "description": "Fired by POST /api/published/orders-webhook; the JSON body binds to ${param.*}.",
  "parameters": [
    { "name": "order_id", "type": "string", "required": true },
    { "name": "customer", "type": "string", "required": true },
    { "name": "amount",   "type": "float",  "default": 0 }
  ],
  "steps": [
    {
      "id": "src",
      "type": "db_source",
      "label": "Webhook payload → row",
      "params": {
        "source_mode": "query",
        "query": "SELECT '${param.order_id}' AS order_id, '${param.customer}' AS customer, CAST(${param.amount} AS DOUBLE) AS amount, CASE WHEN ${param.amount} >= 1000 THEN 'high' ELSE 'standard' END AS tier, now() AS received_at"
      }
    },
    {
      "id": "sink",
      "type": "local_table_sink",
      "label": "Append to webhook_orders",
      "params": { "schema_name": "default", "table_name": "webhook_orders", "mode": "append" }
    }
  ],
  "connections": [
    { "from_step": "src", "to_step": "sink" }
  ]
}
```

> **Parameters** are the contract with the webhook. Each `${param.name}` in a
> node's config is replaced at run time by the matching key from the JSON body
> (or its `default` if absent). Types are coerced: `string` / `int` / `float`
> / `bool` / `json`. A `required` parameter that's missing fails the run
> loudly. Defaults may also use system placeholders: `${utcnow}`,
> `${utcnow:%Y-%m-%d}`, `${run_id}`.

---

## 2. Create an API key

The gateway endpoints below need your session token (`Authorization: Bearer
<token>` — grab it from the browser DevTools → Application → Local Storage →
`fpulse_token`, or from the login API). Assume the server is on the default
`http://127.0.0.1:8001`.

```bash
TOKEN="<your session JWT>"

curl -sX POST http://127.0.0.1:8001/api/gateway/keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"orders-webhook","scopes":["execute"],"rate_limit_rpm":120}'
# → {"id":"...", "key":"fpk_live_...", ...}   ← copy `key`; it is shown once
```

## 3. Publish the pipeline at a path

```bash
curl -sX POST http://127.0.0.1:8001/api/gateway/endpoints \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "workflow_id": "webhook-order-demo",
        "path": "orders-webhook",
        "method": "POST",
        "require_api_key": true,
        "rate_limit_rpm": 120,
        "timeout_seconds": 60
      }'
```

The pipeline is now live at `POST /api/published/orders-webhook`.

## 4. Fire the webhook

This is the only call the **external system** makes — it needs just the API
key (no user login):

```bash
APIKEY="fpk_live_..."   # from step 2

curl -sX POST http://127.0.0.1:8001/api/published/orders-webhook \
  -H "X-API-Key: $APIKEY" \
  -H "Content-Type: application/json" \
  -d '{"order_id":"ORD-1001","customer":"Acme Corp","amount":1500}'
# → {"status":"success","endpoint":"/orders-webhook","workflow_id":"webhook-order-demo","result":{...}}
```

Each call appends one row to the `webhook_orders` managed table (see the
**Storage** page). The API key can also be passed as `?api_key=...` if a header
is inconvenient for the caller.

---

## How this fits the "runs without stopping" model

F-Pulse's engine is batch/micro-batch, not a continuous stream processor. The
**server + scheduler stay up 24/7** (run it as a service with `fpulse
install-service`, or in Docker with a restart policy). "Real-time" comes from
two triggers:

- **Webhook (this example)** — the upstream system pushes an event and the
  pipeline fires immediately. Best when you control the source.
- **Interval schedule** — for sources you must poll (a table, a queue, files
  landing), schedule the pipeline every N seconds/minutes for micro-batch.

## Notes for production

- **Security:** keep `require_api_key: true`. The gateway also supports
  per-key `rate_limit_rpm` and an `ip_allowlist`. Because parameter values are
  substituted as text, treat a webhook whose caller you don't control as
  untrusted input — prefer typed parameters (`int`/`float`/`bool`) and avoid
  interpolating free-text straight into raw SQL; the DB Source already blocks
  `;` and validates identifiers, but validate business fields yourself.
- **Idempotency:** if the caller may retry, add an idempotency key on the sink
  or use `mode: "merge"` with `merge_on: ["order_id"]` so repeats upsert
  instead of duplicating.
- **Persisting elsewhere:** swap `local_table_sink` for `db_sink`, `s3_sink`,
  `api_sink`, etc. to land the event in your warehouse/lake instead of a
  managed table.
