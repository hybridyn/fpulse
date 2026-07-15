# Node reference

F-Pulse ships **40 nodes** organized into 6 categories (per `frontend/src/components/hiddenNodeTypes.ts` `VALID_GHOST_TYPES`). Drag any node onto the canvas and click it to configure.

## Sources (5)

| Node | What it does |
|---|---|
| **CSV Source** | Read CSV / TSV / text files. Supports header detection, custom delimiters, encoding override. |
| **DB Source** | Query any configured database connection. SQL is full DuckDB-compatible. |
| **API Source** | Call a REST endpoint and emit rows. Handles pagination (`next_url`, offset, page-token), pre-flight auth. |
| **Microsoft Graph Source** *(2026-05-22)* | Generic reader for any Microsoft Graph endpoint. Reuses one Azure App Registration to fetch from `/users`, `/groups`, `/sites`, `/drives`, `/teams`, `/planner/*`, `/me/messages`, etc. Client-credentials OAuth, follows `@odata.nextLink` pagination automatically. |
| **Managed Table Source** *(2026-05-23)* | Read from a workspace-managed Parquet table addressable by `schema.name`. Tables live under `$FPULSE_DATA_DIR/tables/{ws}/{schema}/{name}/`. Promote any uploaded file to a managed table from **Storage → Files → Promote**, or write to one from a pipeline via Managed Table Sink. |

## Transforms (17)

| Node | What it does |
|---|---|
| **Filter** | Keep rows matching a SQL `WHERE` condition. |
| **Transform** | Custom SQL transformation. Full DuckDB syntax. |
| **Deduplicate** | Remove duplicates by one or more key columns. |
| **Aggregate** | `GROUP BY` with SUM / COUNT / AVG / MIN / MAX / STDDEV. |
| **Join** | Inner / left / right / full join between two upstream datasets. |
| **Sort** | Order by one or more columns asc/desc. |
| **Rename** | Rename columns or select a subset. |
| **Type Cast** | Convert column types (string → int, etc.). |
| **Derived Column** | Add calculated columns with SQL expressions. |
| **Lookup** | Enrich rows by joining a lookup dataset. |
| **Union** | Concatenate rows from multiple datasets (UNION ALL by default). |
| **Pivot** | Rotate rows into columns. |
| **Unpivot** | Rotate columns into rows. |
| **Window** | `ROW_NUMBER`, `RANK`, `LAG`, `LEAD`, running totals. |
| **Conditional Split** | Route rows to different outputs by predicate. |
| **Sample** | Take first N rows or a random sample. |
| **Validate** | Check data-quality rules (not-null, unique, range, regex); tag rows valid/invalid. |

## Outputs (3)

| Node | What it does |
|---|---|
| **Output** | Write to Parquet, CSV, or JSON files. |
| **Database Sink** | Write to a database table via a configured connection. |
| **Managed Table Sink** *(2026-05-23)* | Write to a workspace-managed Parquet table by `schema.name`. Three modes: `replace` (drop existing parts, write fresh part-000), `append` (timestamp-suffixed new part file alongside existing), `merge` (upsert on `merge_on` key columns; collapses to a single part). The Storage page tracks "Used by N pipelines" automatically so destructive actions surface warnings. |

## Flow Control

| Node | What it does |
|---|---|
| **ForEach** | Iterate over rows of an upstream dataset, executing a sub-pipeline per row. |
| **If/Else** | Conditional branch based on an expression. |
| **Append Variable** | Append a value to an array variable across iterations. |
| **Filter activity** | Filter array items by a condition. |
| **Validation** | Poll until a dataset is ready (file exists, row count meets threshold) before continuing. |
| **Fail** | Stop the pipeline with a custom error message — useful for short-circuiting upstream-check failures. |
| **Wait** | Pause for a duration or until a time. |

## Activities

| Node | What it does |
|---|---|
| **Get Metadata** | Return one row per metadata item — file size, last-modified, content type, child count. |
| **Copy Data** | Source-to-destination copy with column mapping. |
| **Delete** | Delete files / rows from a source matching a predicate. |
| **Execute SQL** | Run arbitrary SQL against a database connection — DDL, stored procs, admin statements. |
| **Execute Pipeline** | Invoke a sub-pipeline as a step. |

## Cloud Storage

| Node | What it does |
|---|---|
| **File System Task** | Copy, move, rename, delete files between filesystems (local, S3, Azure Blob, GCS). |
| **S3 Get/Put** | Direct S3 operations. |
| **Cloud Storage** | Generic cloud-storage helper covering S3 / Azure Blob / GCS. |

## Generic Source / Destination

In addition to the named nodes above, F-Pulse provides **Generic Source** and **Generic Destination** nodes. Pick a connector type inside the node config panel — they're the entry point for any of the 37 connectors (see [`connectors.md`](connectors.md)).

## Coming next (Sprint 1)

These nodes are in active development:

- **Bulk Load** — dialect-native COPY/MERGE for warehouses (today: basic INSERT)
- **SCD Type 2** — slowly-changing-dimension with effective-from/to + current-flag
- **Data Profile** — statistics + nullability + cardinality (just shipped — try it!)
- **Checkpoint** — resume from last successful step on failure

See the [Changelog](../CHANGELOG.md) for release notes.
