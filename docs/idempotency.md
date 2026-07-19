# Idempotency keys for external sinks

External sinks (email, webhook, REST API, Kafka, Slack) fire **real-world
side effects** per row: an email gets sent, a webhook gets POSTed, a
Slack channel gets pinged. F-Pulse's frontend idempotency classifier
correctly marks these red — see [`docs/connectors.md`](connectors.md)
and the badge in the canvas — and backfills refuse to auto-replay
them.

But there are still three cases where duplicates can slip through:

1. The operator **acknowledges** the unsafe-sink warning and runs the
   backfill anyway (e.g. for a "send daily digest" pipeline where
   re-firing on every window is the whole point of the second window).
2. A pipeline **partially fails** mid-run — half the rows fired, then
   an error. The retry re-fires all rows including the ones the first
   pass already sent.
3. A scheduled run **double-fires** because of an operator restart or
   a watchdog re-dispatch.

Idempotency keys close that gap. Set an `idempotency_key` template on
the sink and F-Pulse will skip rows it has already fired for —
regardless of which run, which window, or how long ago.

## How it works

1. You set `idempotency_key` on the sink to a `{column}`-substituted
   string. Example: `"{user_id}|{event_type}|{event_date}"`.
2. For every row, F-Pulse renders the template against the row, hashes
   the result with SHA-256, and looks it up in the per-pipeline
   `sink_idempotency` table inside `fpulse.db`.
3. If the (pipeline, sink_step, hash) tuple was recorded within the
   TTL, the row is **skipped** — the side effect is not fired.
4. Otherwise the side effect fires and the hash is **recorded** with a
   TTL (default 30 days).

The skip is reported in the step log: `Sent 8 / Skipped 12 (idempotent
dedupe)`.

## Configuring the key

Every external sink (email, webhook, api, kafka, slack) accepts the
same two params:

| Param | Type | Default | Notes |
|---|---|---|---|
| `idempotency_key` | string | `""` (off) | `{column}` template; empty = no dedup |
| `idempotency_ttl_days` | int | `30` | Per-row TTL; rows seen this long ago are eligible to re-fire |

### Pick the right key

A good key uniquely identifies "the semantic event we don't want to
re-fire". Examples:

| Sink | Goal | `idempotency_key` |
|---|---|---|
| Order-confirmation email | One email per order, ever | `{order_id}` |
| Daily digest email | One per recipient per day | `{user_id}|{digest_date}` |
| Webhook for new signups | One POST per signup | `{signup_id}` |
| Slack "build broke" alert | One per failed build SHA | `{repo}|{commit_sha}` |
| Generic event stream → Kafka | At-most-once per source event id | `{event_id}` |

Bad keys:
- ❌ `static-string` — dedup catches everything after the first row, ever
- ❌ `{now}` or `{run_id}` — changes every run, dedup catches nothing
- ❌ A column that's `NULL` for some rows — all NULLs collide on the
  empty-string render

## Storage & retention

- **Location:** the operator's existing `fpulse.db` (table
  `sink_idempotency`). No new files.
- **Scope:** keyed by `(pipeline_id, sink_step_id, key_hash)` — two
  different sinks with the same key don't collide, two different
  pipelines don't collide either.
- **TTL:** `expires_at` per row. Lookup treats past `expires_at` as
  not-seen and lets the sink re-fire (and record a fresh marker).
  Stale rows are pruned lazily; there is no background sweeper today.
- **Reset:** to clear a sink's dedup memory, delete the rows directly:
  ```sql
  DELETE FROM sink_idempotency
   WHERE pipeline_id = 'wf_abc123'
     AND sink_step_id = 'snk_email_digest';
  ```

## Failure modes (and what F-Pulse does)

The store is **best-effort**: a failure to read or write the dedup
record never blocks a sink from firing. The reasoning: duplicates are
the failure mode this feature is designed to *prevent*, but a hard
failure of the dedup store must never *cause* a duplicate either. If
the lookup throws, F-Pulse logs a warning and fires the sink as if no
key were set.

This means the absolute worst case is the same as today: a duplicate
side effect on retry. The expected case — store is healthy — gives
exactly-once semantics per `(pipeline, sink_step, key_hash)` within
the TTL window.

## Status (as of v1.0.0-rc)

- ✅ Dedup store + helpers shipped (`fpulse/sinks/dedupe_store.py`,
  `fpulse/sinks/idempotency_helper.py`).
- ✅ SQLite schema migration shipped (`sink_idempotency` table).
- ✅ Main.py wiring shipped — store is bound on backend startup.
- ⏳ Sink integration: the `idempotency_key` param needs to be added
  to each of the 5 external sinks (`EmailSinkNode`, `WebhookSinkNode`,
  `ApiSinkNode`, `KafkaSinkNode`, `SlackNotifyNode`). Until that
  lands, configuring the param has no effect — the sink still fires
  every row. Tracked in the roadmap.

For the current per-sink wiring status check
`backend/fpulse/nodes/sinks.py` and `backend/fpulse/nodes/flow_control.py`.
