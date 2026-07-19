# F-Pulse Steward — Native data-quality checks

P4 of the reviewer audit: a native surface for data-quality assertion
results. Event-driven, same architecture as `schema-drift` and
`connector-health` — an external runner (F-Pulse executor, dbt test,
Great Expectations checkpoint, Soda scan, or a hand-rolled probe)
evaluates an assertion against a dataset and posts the result to
`POST /api/steward/quality-check`. Failed assertions become standard
`StewardFinding` records flowing through the same surface as every
other detector.

F-Pulse does **not** evaluate the assertion itself. That's the
runner's job. Steward is the place those runners report into so
failures land alongside duplicate / schema-drift / connector-health
findings — and pick up the same alert-fatigue guarantees for free.

## Supported assertion types

| `check` name | FindingKind | Default severity |
|---|---|---|
| `not_null` | `null_spike` | P1 on any failure (integrity) |
| `unique` | `duplicate_key_spike` | P1 on any failure (integrity) |
| `duplicate_key` | `duplicate_key_spike` | P1 on any failure (integrity) |
| `referential_integrity` | `quality_check_failed` | P1 on any failure (integrity) |
| `row_count_min` | `volume_anomaly` | P2 (P1 if >50% rows missed) |
| `row_count_max` | `volume_anomaly` | P2 (P1 if >50% rows over) |
| `freshness` | `freshness_miss` | P2 (P1 if severely stale) |
| `partition_missing` | `partition_missing` | P2 |
| `accepted_values` | `quality_check_failed` | P2 (P1 if >50%) |
| `range` | `quality_check_failed` | P2 (P1 if >50%) |
| `regex` | `quality_check_failed` | P2 (P1 if >50%) |
| `custom` | `quality_check_failed` | P2 (P1 if >50%) |

**Integrity checks are P1 on any failure** because one duplicate
primary key or one null in a NOT-NULL column breaks every downstream
JOIN or constraint. **Non-integrity checks scale severity by failure
rate** — 3 invalid values out of 10,000 is P2 noise; 6,000 out of
10,000 is a structural problem promoted to P1.

## Recording an assertion result

```http
POST /api/steward/quality-check
Content-Type: application/json
Authorization: Bearer <token>

{
  "source_signature": "abc123def456",    // required - same key Archeologist uses
  "source_label":     "orders.csv",      // optional, human-readable
  "run_id":           "exec-9876",       // optional, links to a pipeline run
  "assertions": [
    {
      "check":        "not_null",
      "column":       "customer_id",
      "failed_count": 0,                 // 0 = passed
      "total_rows":   10000
    },
    {
      "check":        "unique",
      "column":       "order_id",
      "failed_count": 2,                 // > 0 = FAILED → finding
      "total_rows":   10000,
      "message":      "Found 2 duplicates; investigating upstream loader"
    },
    {
      "check":        "accepted_values",
      "column":       "status",
      "failed_count": 12,
      "total_rows":   10000,
      "message":      "12 rows have status='archived' (not in {active,pending,cancelled})"
    }
  ]
}
```

Response:

```json
{
  "recorded": true,
  "assertions_total": 3,
  "findings_emitted": 2,
  "finding_ids": ["qc-a1b2...", "qc-c3d4..."]
}
```

Passing assertions (`failed_count: 0`) record nothing. Only failures
produce findings.

## Granular suppression — per (source, check, column)

Each finding's `source_signature` is a hash of `(source, check, column)`.
That means dismissing **"this dataset always has known nulls in
`zip_code`"** silences only that one combo. Other null checks on the
same source, and other column constraints on the same source, all
keep firing.

This is intentional. The alternative — one signature per source —
would let a single dismiss accidentally take down every quality check
on that dataset.

## Same alert-fatigue guarantees as every other detector

A quality finding behaves identically to a duplicate-source or schema-
drift finding once it hits the journal:

- Time-clamped escalation: severity grows with persistent recurrence,
  but never on a 30-second blip
- Notification de-dup: at-most-one per (user, finding, severity, rebound)
- Dismiss-with-reason: sanitised (AWS keys / passwords / etc.) before
  journal write
- Resolve-with-fix-note: becomes a `PROPOSED` lesson in the Memory Layer

## Integrating with existing DQ tools

| Tool | How to wire it |
|---|---|
| **dbt** | Add a post-hook macro that posts the test result for each `tests:` block. The `node.unique_id` makes a good `source_signature`. |
| **Great Expectations** | A `StoreValidationResultAction` action variant that POSTs the validation result. Each expectation maps to one assertion. |
| **Soda Core** | After the scan call `soda scan --result-output-json`, parse, and POST. Soda's check IDs map to `source_signature`. |
| **Custom runners** | Anything that produces a true/false answer per row over a dataset can POST. The `message` field is free text for human context. |
| **F-Pulse executor** | Will record built-in node assertions automatically in a future release (1.2 — same pattern as connector-health's auto-record from Test Connection). |

## What does NOT happen

- **F-Pulse never runs your assertions itself.** Reading a Snowflake
  table to count nulls is not free, and we don't get to do it silently
  on someone else's behalf. The runner you already use is the right
  place; Steward is the place the result lands.
- **Steward never auto-fixes the data.** Read-only Rule 1 still
  holds. A `not_null` failure surfaces a finding; the fix is yours.
- **Passing assertions are not stored.** Only failures hit the
  journal. (Re-recording the same assertion later — failed or passed
  — is an idempotent operation; finding ids are deterministic per
  `(source, check, column)`.)

## What Plus adds

OSS gets the full recording surface, the journal, the detector. Plus
will add:

- A built-in **F-Pulse executor DQ node** that runs not_null /
  unique / etc. against any source-shaped node in the canvas and
  auto-POSTs the result
- A **DQ rule library** sharing common checks across workspaces
- An **assertion-history viewer** in the UI rendering pass/fail trends

Storage format stays identical between OSS and Plus.

## See also

- [`overview.md`](overview.md) — the 7-level Steward contract
- [`schema-drift.md`](schema-drift.md) — companion event-driven detector
- [`connector-health.md`](connector-health.md) — companion event-driven detector
- [`custom-rules.md`](custom-rules.md) — admins can layer additional checks via YAML
