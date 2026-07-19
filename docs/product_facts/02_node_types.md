# F-Pulse node types reference

A pipeline is a directed graph of nodes. Each node reads from upstream nodes, transforms data, and emits a relation that downstream nodes can read. F-Pulse ships 40 node types organised by category.

**Edition badges:** OSS = available in F-Pulse Free. Plus = F-Pulse+ only. Most nodes are OSS.

## Source nodes (where data comes from)

**CSV Source** — OSS. Read a local CSV file. Supports header detection, custom delimiter, encoding override.

**Database Source** — OSS. Connect to PostgreSQL / MySQL / MS SQL Server / SQLite. Pulls rows via a SQL query you provide. Supports three sync modes (2026-05-30): `full_refresh` (re-read every row), `incremental` (cursor-tracked — the engine auto-persists the last `watermark_column` value between runs so the next run only reads new rows; manual override available for backfill), and `cdc` (informational — use the dedicated CDC Source for log-based replication).

**REST/HTTP API** — OSS. Generic REST connector for any HTTP+JSON source. Configure auth (api_key / bearer / oauth2), pagination, and response schema.

**SaaS Connector** — OSS. Universal node that picks any installed manifest at runtime. F-Pulse ships ~31 SaaS manifests (HubSpot, Stripe, Shopify, Zendesk, Jira, Notion, Salesforce starter, etc.) — part of the 37 total connectors. Each is graded 0-5 on the certification matrix. Current state (May 4 2026): 8 v2 beta (validation in progress), 27 v1 functional, 2 v1 basic. The v1→v2 + fixture-authoring uplift is the post-1.0 connector roadmap.

**Cloud Storage** — OSS. Read files from S3, GCS, or Azure Blob.

**Microsoft Graph Source** — OSS (2026-05-22). Generic Microsoft Graph reader for any Graph endpoint: `/users`, `/groups`, `/sites`, `/drives`, `/teams`, `/planner/*`, `/me/messages`, etc. Auth via client-credentials OAuth against an Azure App Registration. SharePoint / OneDrive / Teams nodes stay for their file-flavored UX; this is the general-purpose JSON-rows reader for arbitrary Graph endpoints.

**Managed Table Source** — OSS (2026-05-23). Read from a managed Parquet table addressable by `schema.name`. Tables live under `$FPULSE_DATA_DIR/tables/{ws}/{schema}/{name}/part-*.parquet` and are managed from the Storage page (Files → Promote, or local_table_sink writes). Reuses a file across pipelines without re-uploading.

**OpenAPI Source** — OSS. Auto-generate a connector from any OpenAPI/Swagger spec.

**JDBC Source** — Plus. Generic JDBC for warehouses (Snowflake, BigQuery, Redshift, Databricks SQL via the dialect registry — Plus only). OSS users connect to Postgres / MySQL / MSSQL / SQLite via the regular Database Source instead.

**CDC Source** — Plus. Debezium-style change-data-capture from Postgres/MySQL. Required for streaming-replication pipelines.

**Vector Source** — Plus. Read from Pinecone, Weaviate, Qdrant, Chroma, pgvector. Vector DB integrations are Plus-only per EDITION_MATRIX.

**Enterprise SaaS connectors** — Plus only. SAP, SAP HANA, NetSuite, Workday, Dynamics 365, ServiceNow, the production-grade Salesforce connector, Informix, Teradata, DB2.

## Transform nodes (manipulate data)

**Filter** — drop rows that don't match a predicate. SQL-style condition.

**Transform** — generic SQL expression for derived columns.

**Derived Column** — add new columns by writing per-row expressions (e.g. `amount * 1.18`, `LOWER(name)`). Each `columns[]` entry is `{name, expression}`. From 2026-05-30 each entry also accepts an optional `window: {partition_by: [...], order_by: [...]}` for cross-row references — `LAG(amount, 1)`, `SUM(amount) OVER`, running totals — without dropping into the full Window node. Expressions already containing `OVER (...)` pass through unchanged so power users keep control.

**Window** — full SQL window-function node for ranking (ROW_NUMBER / RANK), lag/lead with custom offsets, frame specifications (ROWS BETWEEN N PRECEDING AND M FOLLOWING), per-function partition / order. Use this when Derived Column's `window` shortcut isn't expressive enough.

**Deduplicate** — collapse rows by key columns; keeps the row with the highest `order_by` value.

**Aggregate** — GROUP BY with SUM / COUNT / MIN / MAX / AVG / median / approx_count_distinct.

**Join** — INNER / LEFT / RIGHT / FULL / ANTI / SEMI joins between two upstreams.

**Schema Mapper** — source-to-target field mapping with type coercion. Lets a manifest-driven SaaS source feed any sink without writing SQL.

**Flatten/Explode** — flatten nested JSON or explode arrays into rows.

**Data Quality** — declarative row-level validation rules. Pass rows downstream; failed rows can be sent to a dead-letter sink.

**Data Profile** — emits one row per column with summary statistics: null %, distinct count, min / max, top value. Useful immediately after a source to verify shape.

**SCD Type 2** — slowly-changing-dimension Type 2. Tracks historical versions per business key. Each change closes the previous version (`is_current=false`, `valid_to=run_time`) and inserts a new one (`is_current=true`, `valid_from=run_time`). Hash-based change detection skips no-op rows.

**Upsert** — idempotent merge into a relation by key columns. Re-running never produces duplicates.

**Materialize** — save intermediate result to a temp table for caching / checkpoint.

**Embedder** — OSS. Text column → vector column (openai / cohere / sentence-transformers / hash).

**LLM Guardrail** — OSS. PII / profanity / prompt-injection routing.

**Semantic Router** — OSS. Classify rows into labels via embeddings or LLM.

There is no built-in run-your-own-Python node in either edition. For custom
logic, use the **SQL Transform** node (DuckDB — joins, window functions, CTEs,
PIVOT, UNNEST, JSON path), or write a first-class node type in Python and
register it (see `docs/extend/build-a-node.md`). To run code that genuinely
can't fit those, run it outside F-Pulse and ingest the output via a CSV/JSON
source.

## Sink nodes (where data goes)

**CSV Sink / JSON Sink / Excel Sink** — OSS. Write to local files.

**Database Sink** — OSS. Write to PostgreSQL / MySQL / MS SQL Server / SQLite via row-by-row INSERT.

**Warehouse Sink** — OSS. Write to a warehouse with schema evolution (auto-add new columns).

**Bulk Loader** — OSS. Fast bulk writes via dialect-native paths. Postgres uses `COPY FROM STDIN`; Snowflake uses `PUT` to user stage + `COPY INTO`. 10-100× faster than INSERT loops at scale. Modes: create / append / truncate / merge (idempotent UPSERT on primary key). Other warehouse dialects (BigQuery, Redshift, Databricks, MSSQL, Oracle, MongoDB, ClickHouse) are designed and on the post-1.0 roadmap.

**S3 Sink / Kafka Sink / API Sink / Email Sink / Delta Sink** — OSS.

**Managed Table Sink** — OSS (2026-05-23). Write to a managed Parquet table by `schema.name`. Three write modes: `replace` (drop existing parts, write fresh part-000), `append` (timestamp-suffixed new part file alongside existing), `merge` (upsert by `merge_on` key columns; rewrites to a single part). Pairs with Managed Table Source for cheap intra-workspace data sharing.

**Vector Sink** — Plus. Pinecone, Weaviate, Qdrant integrations.

**JDBC Sink** — Plus. Companion to JDBC Source via the Plus dialect registry.

## Control flow

**Schedule** — runs the pipeline at fixed times you specify (every hour, daily at 9 AM, every Monday, etc.).

**Retry Handler** — wrap upstream nodes with retry logic + exponential backoff.

**Branch / If-Then-Else / Switch** — conditional execution.

**Pre-SQL / Post-SQL** — run arbitrary SQL before or after a sink.

## When to pick which node

For **dimension table** loads where history matters → use SCD Type 2 + Bulk Loader (merge mode).

For **simple ETL** (read CSV → transform → write CSV) → CSV Source + Transform + CSV Sink.

For **API ingestion** → REST Connector OR SaaS Connector + Schema Mapper + Bulk Loader.

For **data validation** before a fragile sink → Data Quality (drop or fail mode) + Data Profile.

For **idempotent re-runs** → Upsert OR Bulk Loader in merge mode with primary_key declared.

For **change-tracking** in a dimension → SCD Type 2.
