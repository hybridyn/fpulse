# Reliability features (1.2)

This guide covers the reliability + observability capabilities added in
the 1.2 line: data lineage, failure classification + retry policy,
cancellation, and backfill correctness (lookback, resume, merge,
soft-delete).

> **Status labels.** Each capability is marked:
> - **GA** — wired end-to-end and usable today.
> - **Foundation** — the storage / API / logic ships and is tested, but
>   the final load-bearing wire-in (e.g. into the executor hot loop or a
>   live connector) lands in a follow-up. Where a foundation isn't yet
>   doing work end-to-end, that's stated plainly.
>
> Design rationale + the precise remaining wire-ins for each item live
> in `docs/design/{lineage,executor-maturity,backfill-ux}-1.2.md`.

---

## Data lineage

### Runtime lineage — **GA (read API)**

Every successful step now records what it actually produced that run:
columns out, row count, and timing. This is distinct from the
*design-time* lineage graph (inferred from the pipeline definition) —
it's what really ran on a specific `run_id`.

```http
GET /api/lineage/runs/{run_id}
GET /api/lineage/workflow/{workflow_id}/runs    # list runs with lineage
```

`GET /api/lineage/runs/{run_id}` returns:

```json
{
  "run_id": "run-abc",
  "step_runs": [
    {
      "step_id": "s1", "step_label": "Load orders",
      "step_type": "db_source",
      "columns_in": [], "columns_out": ["id", "amount"],
      "rows_in": 0, "rows_out": 10000,
      "started_at": 1717000000.0, "completed_at": 1717000002.5,
      "error": ""
    }
  ]
}
```

The executor emits these automatically for successful steps; emission is
best-effort and never fails a run.

### OpenLineage export — **GA (manual / programmatic)**

F-Pulse can emit [OpenLineage](https://openlineage.io) 1.0-5 RunEvents
so pipelines appear in Marquez, DataHub, or any OpenLineage-aware tool.

Two exporters:

| Exporter | Use |
|---|---|
| `OpenLineageJSONLExporter(path)` | Append RunEvents to a JSONL file (dev / audit / bulk import) |
| `OpenLineageHTTPExporter(url)` | POST RunEvents to a Marquez / DataHub endpoint, with bounded retry |

```python
from fpulse.lineage.openlineage import OpenLineageHTTPExporter
exporter = OpenLineageHTTPExporter("https://marquez.example/api/v1/lineage")
summary = exporter.export_run(run_id, lineage_store)   # {"posted": 5, "failed": 0}
```

Env-var config (read by the formatter):

| Var | Effect |
|---|---|
| `FPULSE_LINEAGE_NAMESPACE` | Override the job namespace (default `f-pulse`) |
| `FPULSE_LINEAGE_PRODUCER` | Override the producer URI |
| `FPULSE_LINEAGE_REDACT_COLUMNS=1` | Drop column names from schema facets (privacy) |

*Deferred (L2.2):* automatic export-on-run-completion when
`FPULSE_LINEAGE_OPENLINEAGE_URL` is set — needs an executor hook.

### Output-to-consumer lineage — **GA (self-attestation)**

Downstream consumers of an F-Pulse output (another pipeline, a Snowflake
view, a dashboard) register themselves so you can answer "if I change
this output's schema, what breaks?"

```http
POST   /api/lineage/consumers        {output_id, consumer_id, consumer_type, last_read_at?, attested_by?, notes?}
GET    /api/lineage/consumers?output_id=...
GET    /api/lineage/consumers/_overview
DELETE /api/lineage/consumers        {output_id, consumer_id, consumer_type}
```

This is an honest protocol: it only knows about consumers that opt in.
Automatic discovery via the Snowflake query log is a Plus feature
(see the lineage design doc, track L4).

---

## Failure classification + retry policy

### Failure classification — **GA**

Every failed step is tagged with a `failure_class` so you can tell at a
glance whether a failure is worth retrying:

| Class | Meaning | Retry helps? |
|---|---|---|
| `transient` | timeout, 5xx, lock, network blip | yes |
| `dependency` | external system down, auth/cred expired | maybe (if it recovers) |
| `data_quality` | null in NOT-NULL, constraint, schema mismatch | no |
| `user_input` | invalid pipeline config | no |
| `fatal` | OOM, disk full, code bug | no |
| `unknown` | unclassified | conservative: no |

The class appears on each step's run result and renders as a coloured
chip in the run detail view (Executions page), beside the existing error
badge.

### Retry policy — **GA (opt-in)**

A workflow can declare a retry policy that the executor consults before
retrying a failed step — so it won't waste attempts retrying a
`data_quality` failure that can't change between runs.

Set `retry_policy` on the workflow IR:

```json
{
  "retry_policy": {
    "enabled": true,
    "max_attempts": 3,
    "initial_backoff_seconds": 2.0,
    "backoff_multiplier": 2.0,
    "backoff_max_seconds": 60.0,
    "retry_on": ["transient", "dependency"]
  }
}
```

- **Disabled by default** — existing pipelines behave exactly as before;
  the per-step retry settings continue to drive retries.
- When enabled, a failure whose class is **not** in `retry_on`
  short-circuits immediately (no wasted attempts).
- `backoff_multiplier: 1.0` gives a fixed delay; `> 1.0` is exponential,
  capped at `backoff_max_seconds`.

*Note:* there's no canvas editor for the policy yet — set it via the
workflow API / IR. A settings-tab editor is a planned frontend follow-up.

---

## Cancellation — **Foundation**

The executor already supports cooperative cancel (a stop flag checked at
step boundaries). 1.2 adds a `CancellationToken` that also fires
**driver-level** cancel callbacks, so a run blocked inside a long
database query can actually be interrupted (a flag alone can't reach a
thread parked in a driver call).

```python
from fpulse.engine.cancellation import get_or_create_token, cancel_run
token = get_or_create_token(run_id)
token.register_cancel_callback(conn.cancel)   # connector registers its native cancel
...
cancel_run(run_id)   # flips the flag AND fires conn.cancel()
```

*Deferred (E3.1):* the executor calling `token.raise_if_cancelled()` at
each step boundary, and each connector registering its driver
`.cancel()` / `.close()` on connect. Those need live connections to
verify (a real `pg_sleep(60)` cancelled mid-flight), so they land in a
focused session with that infrastructure.

---

## Backfill correctness

### Lookback window (late-arriving data) — **GA**

Strict incremental cursors miss rows that arrive at the source *after*
the watermark moved past (clock skew). Set a lookback to re-read the
last N seconds each run; the dedupe store handles the overlap so
downstream sees each row once.

On an incremental source, set **"Re-read last N seconds"**
(`lookback_seconds`) in the Incremental tab. Default `0` = strict cursor
(unchanged). Recommended `86400` (24h) for sources with known skew.

### Resume from window — **GA**

A backfill that failed partway can be resumed from the first window that
didn't complete — already-succeeded windows are skipped, not re-run.

```http
POST /api/executions/backfill/{id}/resume        # auto-detects first unfinished window
POST /api/executions/backfill/{id}/resume {"from_window": 17}
```

In the UI, a **Resume** button appears on `failed` / `partial` /
`cancelled` backfills (Backfills panel). It confirms before resuming,
since resuming assumes the skipped earlier windows completed correctly.

### Merge (upsert) write mode — **Foundation**

The warehouse sink now offers a **`merge`** write mode plus a
**Merge Key Column(s)** field. The per-dialect upsert SQL already exists
(postgres `ON CONFLICT`, SQL Server / Snowflake `MERGE`); 1.2 exposes
the UI to configure it.

*Deferred (B2.1):* wiring the configured `merge_key` through the sink's
execute path into `BulkLoadRequest.primary_key`. Until that lands,
selecting merge mode surfaces the key field but the upsert key isn't yet
passed to the loader — verify against a real warehouse before relying on
it.

### Soft-delete propagation — **Foundation**

A **Tombstone Column** field lets you name the source's soft-delete flag
(e.g. `is_deleted` / `deleted_at`). The partition helper
(`fpulse.sinks.tombstone`) splits a batch into live rows vs deleted keys.

*Deferred (B4.1):* per-dialect `DELETE` codegen in the sink to actually
propagate the deletes. For true hard-delete tracking, use the CDC
connector (Plus).

---

## Summary table

| Capability | Status | How to use |
|---|---|---|
| Runtime lineage | GA | `GET /api/lineage/runs/{run_id}` |
| OpenLineage JSONL export | GA | `OpenLineageJSONLExporter` |
| OpenLineage HTTP export | GA | `OpenLineageHTTPExporter` |
| Consumer self-attestation | GA | `/api/lineage/consumers` |
| Failure classification | GA | `failure_class` on run results + UI chip |
| Retry policy | GA (opt-in) | `workflow.retry_policy` |
| Cancellation (driver-level) | Foundation | `fpulse.engine.cancellation` |
| Lookback window | GA | `lookback_seconds` on incremental source |
| Backfill resume | GA | `POST /backfill/{id}/resume` + UI button |
| Merge / upsert mode | Foundation | warehouse sink `mode=merge` + `merge_key` |
| Soft-delete propagation | Foundation | `tombstone_column` + `fpulse.sinks.tombstone` |
