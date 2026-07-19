# Backfill / incremental / idempotency UX — 1.2 design

## What the reviewer asked for

> "If F-Pulse wants to be taken seriously as ETL, users must configure:
> full refresh / append / upsert/merge / incremental cursor / watermark
> / backfill range / retry behavior / dedupe key / late-arriving data
> handling."

## What actually ships today

| Capability | Status | Where |
|---|---|---|
| **Sync mode declaration** (full / incremental / append) | ✓ | `backend/fpulse/nodes/_sync_mode_decl.py` + `db_source.py` |
| **Incremental cursor + watermark** | ✓ | `backend/fpulse/engine/sync_state_store.py` + `backend/fpulse/api/sync_state.py` |
| **Backfill orchestrator** (chunked re-execution over date range) | ✓ | `backend/fpulse/backfills/orchestrator.py` |
| **Backfill window generation** (configurable window size) | ✓ | `backend/fpulse/backfills/windows.py` |
| **Backfill preflight modal** (row-count estimate, confirm-before-run) | ✓ | `backend/fpulse/backfills/preflight.py` + frontend |
| **Backfill idempotency keys** | ✓ | `backend/fpulse/backfills/idempotency.py` |
| **Dedupe store** | ✓ | `backend/fpulse/sinks/dedupe_store.py` + `backend/fpulse/nodes/deduplicate.py` |
| **Sink idempotency helper** | ✓ | `backend/fpulse/sinks/idempotency_helper.py` |
| **SCD2 (slowly-changing dimension type 2)** | ✓ | `backend/fpulse/nodes/scd2.py` |
| **Upsert / merge in db_sink** | ✓ | via `sync_mode` config on the sink node |
| **Per-pipeline backfill UI** (start/end date range, window size, on-failure policy) | ✓ | `backend/fpulse/api/backfills.py` + frontend |
| **Late-arriving data handling** ("data arrived after the watermark moved past") | ✗ | Nothing explicit; user has to manually run a backfill |
| **Dedupe-key UX in db_sink** (configure which column(s) are the natural key for upsert) | partial | The capability exists; the canvas UI for setting it is minimal |
| **Resume-from-failure on a partial backfill** | partial | `backfill_runs` table records per-window state; UI doesn't expose "resume from window N" |
| **Soft-delete propagation** (downstream knows a row was deleted upstream) | ✗ | Sink modes don't carry "deleted" markers |
| **Per-cursor lookback window** ("re-read the last 24h on every incremental run to catch late data") | ✗ | Cursor moves strictly forward today |

So the real 1.2 work is **four targeted UX/feature additions**, not a
rebuild of the backfill story.

## The four targeted items

### Gap 1 — Per-cursor lookback window (the late-arriving data fix)

The classic problem: at 10:00 you read all rows where
`updated_at <= 10:00`. At 10:00:30 a row updated at 09:59 (clock skew
on the source) lands in the source table. Your next incremental read
starts from `updated_at > 10:00` and misses it.

Proposed solution: a `lookback_seconds` field on the sync state.
Every incremental read starts from `last_watermark - lookback_seconds`
instead of strict `>`. The dedupe store (already exists) handles the
overlap.

```python
class SyncMode(BaseModel):
    mode: Literal["full", "incremental", "append"] = "full"
    watermark_column: str = ""
    lookback_seconds: int = 0        # NEW — default 0 = strict cursor (current behaviour)
    dedupe_key: list[str] = []       # used to dedupe the overlap window
```

UX surface: a single number field "Re-read last N seconds on each
incremental run (catches late-arriving data)" in the source node's
Sync Mode panel. Default 0 (current behaviour); recommended 86400
(24h) for sources with known clock skew.

### Gap 2 — Dedupe-key UX in db_sink config

Today: dedupe is technically supported (via `dedupe_store` +
`deduplicate` node) but configuring "for this sink, the natural key
is (customer_id, order_date)" needs a separate node.

Proposed: the `db_sink` node gets a `merge_key: list[str]` field. When
`sync_mode = "upsert"`, the executor uses MERGE / UPSERT semantics
with this key. No standalone dedupe node needed.

UX surface: in the db_sink node's Mapping tab, a multi-select for
"Merge key columns" once `Sync mode` is set to `upsert`. Validation
warns if the chosen columns aren't unique-indexed in the destination.

### Gap 3 — Resume-from-window on a failed backfill

Today: a backfill with 30 windows runs them. If window 17 fails and
`on_failure: stop` is set, the backfill stops. Restarting requires
either re-running from the start (`window 1`) or manually editing the
`backfill_runs` table.

Proposed: failed-backfill detail page gets a `Resume from window N`
button. Posts to `/backfills/{id}/resume?from_window=N`. Implementation:
the orchestrator just skips windows < N when scheduling.

Test pin: run a 5-window backfill that fails at window 3; assert
resume from window 3 picks up correctly and leaves windows 1-2 alone.

### Gap 4 — Soft-delete propagation

Today: sources read rows that exist. If a row gets DELETEd from
postgres, the incremental cursor doesn't notice. The downstream
table accumulates stale rows.

This is genuinely hard to do well — CDC tools (Debezium, AWS DMS) are
the right tool for true delete propagation, and we already point at
the `pgoutput` connector for that.

Proposed minimal version: a `tombstone_column` config on sources that
support it (postgres soft-delete tables with an `is_deleted` flag).
When the incremental read sees `is_deleted = true`, the sink either
deletes or marks-deleted on the destination based on `sync_mode`:
- `upsert` → MERGE with `WHEN MATCHED AND tombstone THEN DELETE`
- `append` → row inserted with the tombstone flag carried through

Note: this only works for sources with explicit soft-delete columns.
True hard-delete tracking ships via CDC (`pgoutput` / `cdc.py`) in
Plus.

## Proposed phased milestones

| Milestone | Scope | Effort |
|---|---|---|
| **B1** | Gap 1: `lookback_seconds` field + executor wires it into the source query + dedupe store handles the overlap + UX field in the source node panel + test pin (run incremental twice, plant a late row, confirm it gets picked up) | 4-5 days |
| **B2** | Gap 2: `merge_key` field on `db_sink` + executor MERGE semantics + UX in Mapping tab + per-dialect MERGE generation (postgres ON CONFLICT, mssql MERGE, snowflake MERGE) | 5-7 days |
| **B3** | Gap 3: `Resume from window N` button + API + skip-logic in orchestrator + test | 3-4 days |
| **B4** | Gap 4 minimal: `tombstone_column` field + propagation through `upsert` / `append` sink modes + doc explaining why true CDC is the right answer for hard-deletes | 4-5 days |

**Total ~3 weeks for OSS 1.2.** B1 + B2 are highest-leverage; B3 is
quality-of-life; B4 is a bridge to the real CDC story.

## Open questions for human review

1. **Lookback default** — the spec recommends 86400 (24h) for sources
   with known clock skew, but it could surprise users with double-counted
   rows on the first migration. Should the default be 0 (safe, current
   behaviour) or 300 (5 minutes — catches typical clock-skew without
   weirdness)? Recommend keeping default 0; users opt in.
2. **Merge-key validation** — if the chosen merge key isn't unique on
   the destination, an upsert silently overwrites multiple rows. Do we
   require a unique index on the destination, or just warn? Recommend
   warn (don't refuse) — the user might be intentionally writing into
   a staging table.
3. **Per-dialect MERGE compatibility** — Snowflake MERGE is well-
   defined; SQL Server MERGE has known correctness bugs (Microsoft
   docs recommend INSERT + UPDATE instead). Do we use MERGE on all,
   or dialect-specific patterns? Recommend dialect-specific —
   correctness > consistency.
4. **Resume-vs-restart on backfill** — should "resume from window N"
   require the user to confirm "windows 1..N-1 are correct"? If those
   earlier windows had partial writes that got rolled back, resuming
   skips them. Recommend a confirmation modal that says "Resuming
   skips windows 1..N-1 — confirm they completed successfully."

## What this design explicitly does NOT do

- **Replace the existing backfill orchestrator.** It works; the four
  gaps are bolt-ons.
- **Build a true CDC system.** That's `pgoutput.py` (experimental
  today; production-tier in Plus). The soft-delete gap (B4) is a
  bridge until CDC is generally available.
- **Add Airflow-style task-instance retries to backfill windows.**
  Per-window retry is handled by the executor's retry policy
  (executor-maturity-1.2.md gap 1), not the backfill orchestrator.
- **Solve append-only-with-tombstone for every sink type.** Some
  sinks (CSV file, append-only S3) can't easily express "this row
  is deleted." Document the limitation.

## Decision log

| Considered | Rejected because |
|---|---|
| Rebuild the backfill orchestrator to use Airflow / Dagster under the hood | Adds a heavy dependency for a single-machine OSS install. Current orchestrator is fine. |
| Ship lookback as default-on with a sensible window | Breaks first-time-migration semantics. Better as opt-in. |
| One MERGE codegen for all dialects | SQL Server's MERGE has documented correctness issues. Per-dialect codegen is necessary. |
| Auto-detect dedupe keys from primary-key inspection | Brittle when sources lack PKs or the user wants a non-PK merge key. Make it explicit. |
| Add row-version tracking to enable hard-delete detection | Real CDC is the right answer; we don't want to half-build it. |
