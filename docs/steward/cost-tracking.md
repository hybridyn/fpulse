# F-Pulse Steward — Cost / movement tracking

P5 of the reviewer audit. Event-driven recording surface for per-run
cost / movement metrics. Activates the **WAREHOUSE_WASTE** detector
(cost-level), which fires when a source has been read N times in a
row producing zero output rows downstream — "we're paying the read
cost but nothing's flowing through."

## What ships today (1.1.x)

| Endpoint | Purpose |
|---|---|
| `POST /api/steward/cost-event` | Record one per-run cost event (rows read/written, bytes, duration; optionally per-node observations via `workflow_id` + `node_id`) |
| `GET /api/steward/cost-summary` | Per-source rollup: total rows / bytes / run count, first_seen / last_seen |
| (automatic) `_run_scan` | Surfaces open WAREHOUSE_WASTE + EMPTY_OUTPUT findings via `/findings` |

**Two detectors activate from the same recording surface:**

| Detector | Level | Fires when |
|---|---|---|
| `WAREHOUSE_WASTE` | cost | Last 3 events with same `source_signature` had `rows_read > 0` AND `rows_written = 0` |
| `EMPTY_OUTPUT` | node | Last 3 events with same `(workflow_id, node_id)` had `rows_written = 0` — catches a specific broken filter/join/transform that produces nothing even when upstream is fine |

## What's deliberately deferred to 1.3 Cost Steward

| FindingKind | Why deferred |
|---|---|
| `cost_drift` | Needs the same statistical baseline machinery Rule 6 expects (running variance, seasonality awareness). Ship it once, ship it right. |
| `cost_recommendation` | Optimizer-class output ("you'd save $N/mo by switching this pipeline to incremental"). Belongs in the 2.0 Optimizer module. |
| `redundant_transfer` | Same shape as Archeologist's duplicate-source finding, just cost-flavored. Adding a third detector for the same pattern would double-fire; defer until Cost Steward owns the whole layer. |

## Recording a cost event

Typical caller is the F-Pulse executor at run completion, but any
external runner (CI job, sidecar instrumenting another framework)
can POST too:

```http
POST /api/steward/cost-event
Content-Type: application/json
Authorization: Bearer <token>

{
  "run_id":           "exec-1234",
  "pipeline_id":      "pl-abc",
  "pipeline_name":    "Daily orders ETL",
  "source_signature": "abc...",            // either source OR sink required
  "sink_signature":   "def...",
  "rows_read":        10000,
  "rows_written":     9876,
  "bytes_read":       1234567,             // 0 = unknown / not reported
  "bytes_written":    1234567,
  "duration_ms":      4523,
  "started_at":       "2026-06-07T10:00:00Z",
  "completed_at":     "2026-06-07T10:00:04Z"
}
```

Response:

```json
{
  "recorded": true,
  "finding_emitted": false,
  "finding_id": null
}
```

When the third consecutive zero-output event lands for the same source:

```json
{
  "recorded": true,
  "finding_emitted": true,
  "finding_id": "cost-ww-a1b2c3d4e5f60718"
}
```

## How WAREHOUSE_WASTE works

The rule (severity P2 — surfaced in-app but doesn't page):

```
For each new event with source_signature S:
  Look at the LAST 3 events for source S
  If ALL of them have rows_read > 0 AND rows_written = 0:
    → emit one WAREHOUSE_WASTE finding for S
```

A run with `rows_written > 0` (real work happened) **resets the streak**.
That's deliberate — a single productive day clears the wasted-cost
narrative, even if the next day is empty again. We don't want to
re-fire the warning on every empty day after every productive day.

A run with `rows_read = 0` (the pipeline didn't actually do any
reading) doesn't count toward the streak either. The pattern is "we
read AND wrote nothing," not "we did nothing."

## What rows_read / rows_written should mean for your runner

- **`rows_read`**: rows pulled from the source by this run, post any
  filter/predicate pushdown the connector applied
- **`rows_written`**: rows produced into the sink (or downstream
  consumer) by this run. If the sink is a managed table, count the
  rows actually written; if the sink is "no-op observer," report 0.
- **`bytes_*`**: optional. 0 means "unknown" — don't pass a fake
  number if you don't actually have it. The detector uses rows for
  its decision; bytes are only for the rollup view.

## Cost summary endpoint

```http
GET /api/steward/cost-summary
```

Returns per-source aggregates:

```json
{
  "workspace_id": "default",
  "event_count":  427,
  "source_count": 18,
  "by_source": {
    "src-orders-csv": {
      "rows_read":     2400000,
      "rows_written":  2399501,
      "bytes_read":    3214567890,
      "bytes_written": 3214567890,
      "run_count":     93,
      "first_seen":    "2026-04-01T00:00:00Z",
      "last_seen":     "2026-06-07T10:00:04Z"
    },
    ...
  }
}
```

Useful for a "top 10 most expensive sources" view in the UI or any
external dashboard.

## Suppression

Per-source — dismissing a WAREHOUSE_WASTE finding silences future
warehouse-waste warnings on that specific `source_signature`. Other
cost findings (cost_drift in 1.3, cost_recommendation in 2.0) won't
be silenced when they ship.

## What does NOT happen

- **F-Pulse never auto-pauses a wasteful pipeline.** Read-only Rule
  1 still holds. The fix is yours — disable the schedule, fix the
  upstream filter, or dismiss as intentional.
- **F-Pulse never queries your warehouse for cost estimates.** All
  numbers come from what the runner reports. We don't go ask Snowflake
  for its credit usage; that would be a side effect we deliberately
  avoid.
- **Bytes are not extrapolated from rows.** If you don't report
  bytes, the summary endpoint shows `bytes_read: 0` — honest absence
  is better than a fake estimate.

## What Plus adds

OSS gets the full recording surface, the rollup endpoint, the
WAREHOUSE_WASTE detector. Plus will add:

- A built-in **F-Pulse executor cost recorder** that auto-POSTs after
  every run (no manual instrumentation)
- The **Cost Steward module** with `cost_drift` baseline detection
- A **cost dashboard** rendering per-source trends, top-N spenders,
  and projected monthly movement
- Integration with cloud-billing APIs (Snowflake credit, BigQuery
  bytes-processed) to translate row counts into dollar estimates

Storage format stays identical between OSS and Plus.

## See also

- [`overview.md`](overview.md) — the 7-level Steward contract
- [`schema-drift.md`](schema-drift.md) — event-driven companion
- [`quality-checks.md`](quality-checks.md) — event-driven companion
- [`custom-rules.md`](custom-rules.md) — admins can layer additional
  cost checks via YAML (e.g. "flag any pipeline reading > 1B rows daily")
