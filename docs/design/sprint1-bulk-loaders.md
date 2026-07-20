# Design: Sprint 1 — Bulk loaders + Checkpoint + SCD2

**Status:** Design — implementation pending (blocked on F0.1 manifest v2 for sink-side schema)
**Owner:** F-Pulse core
**Estimated effort:** ~2 weeks total (one engineer, focused)

This is the production-credibility sprint. Today F-Pulse writes to every database via basic `INSERT` — fine for prototyping, slow and lock-prone in production. Sprint 1 closes that gap for the 8 certified dialects.

## Goals

1. **Bulk-load sinks** for the 8 certified dialects — dialect-native COPY/MERGE/load-job paths
2. **Checkpoint/resume** — pipelines that fail mid-run can resume from the last successful step on rerun
3. **SCD Type 2** — first-class slowly-changing-dimension node (effective-from / effective-to / current-flag)

## Non-goals

- Bulk loaders for SaaS connectors (those go via REST APIs, no bulk path exists)
- SCD Type 3 / Type 4 / Type 6 (Type 2 is the 80% case; others can be modeled as Transform + Upsert)
- Distributed checkpoint coordination (single-node only; F-Pulse+ Stage 5 adds queue-side coordination)

## Bulk loaders — per-dialect spec

### Snowflake — `COPY INTO` from Parquet on internal stage
- Buffer rows to a Parquet file in `data/staging/<run_id>.parquet` via PyArrow
- `PUT file://... @~/staging` → `COPY INTO target FROM @~/staging FILE_FORMAT = (TYPE=PARQUET) MATCH_BY_COLUMN_NAME=CASE_INSENSITIVE`
- Compression: ZSTD level 3 (default; tunable via node param)
- Concurrency: respect `FPULSE_DUCKDB_THREADS` for the buffer write; one COPY per node
- Idempotent re-run: `MERGE INTO target USING staging` when `primary_key` is declared in the F0.1 manifest

### BigQuery — `LOAD DATA OVERWRITE` from GCS
- Buffer to local Parquet then `gsutil-style` upload to `gs://<staging_bucket>/<run_id>.parquet`
- BQ `LOAD DATA` job; wait for completion; assert row count matches buffer
- Auth via Workload Identity if available, else service-account key from credential store
- Idempotent: `MERGE` with `primary_key` from manifest

### Redshift — `COPY` from S3 with `MAXERROR 0`
- Same Parquet→S3 pattern as BQ
- Use `MANIFEST` mode for multi-file batches (chunked Parquet for >2GB)
- `MERGE` via temp table + `DELETE … FROM target USING temp WHERE pk` + `INSERT INTO target SELECT * FROM temp`

### Databricks SQL — `COPY INTO` from Volumes
- Parquet → Unity Catalog Volume → `COPY INTO target_table FROM '/Volumes/.../staging' FILEFORMAT = PARQUET`
- Idempotent: `MERGE INTO target USING staging` (Databricks SQL supports MERGE natively)

### MS SQL Server — `BULK INSERT` from local file
- TSV/CSV (no Parquet support in MSSQL bulk path); use `bcp` semantics
- `BULK INSERT target FROM '/var/staging/<run_id>.tsv' WITH (FIELDTERMINATOR='\t', ROWTERMINATOR='\n', TABLOCK)`
- For Azure SQL: use `BULK INSERT` with Azure Blob staging + SAS token
- Idempotent: `MERGE` (T-SQL native)

### Oracle — `INSERT /*+ APPEND */` with direct-path
- Direct-path insert: `INSERT /*+ APPEND PARALLEL */ INTO target SELECT * FROM staging`
- Buffer rows in a temp DuckDB table, push via `cx_Oracle` `executemany` with `arraysize=1000`
- Idempotent: `MERGE` (Oracle native)

### MongoDB — `bulk_write` with `UpdateOne(upsert=True)`
- Batch into chunks of 1000 ops
- Use the `primary_key` from manifest as the filter key
- Native Mongo driver handles compression and write concern

### ClickHouse — native `INSERT INTO ... FORMAT Parquet`
- Direct Parquet stream over HTTP interface; no staging file needed
- ReplacingMergeTree handles dedup by primary key automatically
- For non-Replacing engines: `OPTIMIZE TABLE … FINAL` after insert (warn user about cost)

## Checkpoint / resume

### Storage
- New SQLite table `pipeline_checkpoints` (schema migration v23):
  ```sql
  CREATE TABLE pipeline_checkpoints (
    workflow_id    TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    step_id        TEXT NOT NULL,
    status         TEXT NOT NULL,        -- 'success' | 'failed' | 'in_progress'
    completed_at   TEXT,
    rows_in        INTEGER,
    rows_out       INTEGER,
    duration_ms    INTEGER,
    output_ref     TEXT,                 -- path to Parquet snapshot for resume
    PRIMARY KEY (run_id, step_id)
  );
  ```

### Snapshot strategy
- After every successful step, write the step's output relation to `data/checkpoints/<run_id>/<step_id>.parquet` (DuckDB `COPY (SELECT * FROM rel) TO '<path>' (FORMAT 'parquet', COMPRESSION 'zstd')`)
- Mark step success in `pipeline_checkpoints`
- On pipeline failure: leave the partial checkpoint set in place

### Resume flow
- New API: `POST /api/execute/workflow/{id}/resume?run_id=...`
- Executor reads the failed run's checkpoints, registers each completed step's Parquet as a DuckDB relation under the original step_id, and starts execution from the first failed step
- Cleanup: TTL on `pipeline_checkpoints` (default 7 days, configurable); successful runs delete checkpoints automatically when the next run starts

### UI
- Pipelines page: a failed run shows a "Resume from step X" button alongside "Re-run from start"
- Editor: a banner on a pipeline with active checkpoints reminds the user

### Edge cases
- **Source-node resume:** if step 1 (a Source) succeeded but downstream failed, we resume from the cached source snapshot — NOT re-fetch from the source. This matters for incremental sources because re-fetching could pull different data.
- **Side-effect-producing steps:** if a Database Sink succeeded for some rows and failed for others, resume must NOT re-run the partial sink. Bulk-load nodes guarantee atomicity (all-or-nothing) so this is safe; basic INSERT does not. Resume from the *next* step after a sink that had any partial success.

## SCD Type 2

### Node spec
```yaml
type: scd2
display_name: SCD Type 2
category: transform
params:
  business_key: ["customer_id"]            # logical primary key
  tracked_columns: ["name", "email", "tier"]  # changes here trigger a new version
  effective_from_column: "valid_from"
  effective_to_column: "valid_to"
  current_flag_column: "is_current"
  surrogate_key_column: "scd_id"           # auto-incremented or hash-based
  null_high_water: "9999-12-31"            # value used for "currently active"
```

### Behavior
- Input: a stream of rows with `business_key` + tracked columns
- Existing target (passed via `current_target` upstream join): reads current rows for those keys
- Output: 3 streams the executor merges into the sink:
  1. **Insert** — new business keys (never seen before)
  2. **Insert** — new versions of changed business keys (with `valid_from = run_time`, `is_current = true`)
  3. **Update** — close out previous current versions (`valid_to = run_time - 1ms`, `is_current = false`)
- Hash-based change detection: SHA-256 of concatenated tracked-column values; skip if hash matches current version

### Sink integration
- The SCD2 node emits a special "multi-stream" output that the executor recognizes
- Database Sinks check for this and route each stream to the right SQL operation
- For dialects with native MERGE: a single MERGE statement with WHEN MATCHED AND hash≠ AND WHEN NOT MATCHED clauses

## Schema migration plan

- v23: `pipeline_checkpoints` table
- No other schema changes; SCD2 is a node, not a storage type

## Test plan

- **Bulk load:** per-dialect smoke test against a real connection (CI uses Docker containers for Postgres / MySQL / MS SQL; the rest are tested via mock + manual)
- **Checkpoint:** pipeline of (Source → Transform → Sink) with deliberate failure at Sink → verify resume picks up at Sink
- **SCD2:** golden-file test with 3 runs (initial load, no-change, change in tracked column) → assert correct version table state

## Effort breakdown

| Task | Effort | Dependencies |
|---|---|---|
| Bulk-load infra (staging, runner) | 2 days | F0.1 manifest v2 (sink schema declaration) |
| Per-dialect bulk loaders × 8 | 4 days | Above |
| Checkpoint store + snapshot writer | 2 days | None |
| Checkpoint resume API + UI | 1 day | Above |
| SCD2 node | 2 days | Hash-based change detection helper (reusable) |
| Tests + docs | 1 day | All above |
| **Total** | **~2 weeks** | |

## Status

Design locked. Implementation kickoff post-OSS-launch. The bulk-load schema requirements need F0.1 (DESIGN_F01_MANIFEST_V2.md) to land first since the sink path reads PK + column types from the manifest.
