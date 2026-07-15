# F-Pulse OSS — Node Reference

In-depth, code-grounded reference for every node in the visible OSS palette. Each
entry is derived directly from the node's `execute()`, `param_schema()`,
`default_params()`, `display_name`, and `description` — not from marketing copy.

Generated 2026-06-15. When a node changes, regenerate the affected section from code.

> **ADF-alignment update (2026-06-15).** Names + control-flow semantics were aligned to Azure Data Factory. Some section headers below still carry their old labels — the current palette names are:
> | Section header (old) | Current palette name | Note |
> |---|---|---|
> | Conditional Split (`conditional_split`) | **Switch** | the multi-output brancher |
> | Switch (`switch_case`) | *retired* | hidden; superseded by Switch above |
> | If Condition (`if_condition`) | **If Condition** | now a **True/False** brancher (was a filter) |
> | For Each (Run Pipeline) (`foreach_pipeline`) | **ForEach** | the one ForEach |
> | ForEach (Batch) (`foreach_loop`) | **Batch Rows** | Advanced |
> | Exec Pipeline (`execute_pipeline`) | **Execute Pipeline** | |
> | Wait / Delay (`wait_delay`) | **Wait** | |
> | Retry / Error (`retry_handler`) | **Retry** | |
> | Flatten / Explode (`flatten_explode`) | **Flatten** | Split Out tile removed (use explode mode) |
> | Lookup (`lookup`) | **Lookup Join** | enrichment join |
> | Lookup (Activity) (`lookup_activity`) | **Lookup** | ADF Lookup activity |
>
> The Appendix security/honest-config items have since been **fixed** — see the Appendix for status.

- [Execution model — how data, parameters & variables are handled](#execution-model)
- [Data Movement](#data-movement)
- [Transform](#transform)
- [Combine](#combine)
- [Control Flow](#control-flow)
- [Action](#action)
- [AI / Semantic](#ai--semantic)
- [Appendix — known implementation gaps & caveats](#appendix--known-implementation-gaps--caveats)

---

## Execution model

This section is cross-cutting: it explains the mechanics every node entry below
relies on, so the per-node sections can stay short on the shared parts.

### Pipeline & data flow
- A pipeline is a DAG of **steps**. The `WorkflowExecutor` topologically sorts the
  graph and calls each node's `execute(ctx)` once, in dependency order.
- The engine is **DuckDB**. Data flows as `DuckDBPyRelation` objects through the
  shared connection `ctx.conn`. Most transform nodes register their input as a
  temp view (e.g. `__filter_input`) and return `ctx.conn.sql("SELECT … ")` — the
  relation is lazy, so layered nodes fuse into one optimized query.
- The executor injects `_input_step_ids` (and `_input_step_ports`) into each
  node's params. Nodes read upstream data via `ctx.get_inputs(...)`,
  `ctx.get_input(id)`, or `ctx.get_routed_inputs(ids, ports)`.

### Parameters — `${param.NAME}`
- **Typed pipeline inputs** declared once (Editor → **Parameters**): each has a
  `name`, `type` (string/int/float/bool/json), optional `default`, and `required`.
- Resolved **pre-run** by `engine/parameters.py`. A whole-string reference
  (`"${param.batch_size}"`) preserves the declared type; an embedded reference
  (`"/data/${param.dataset}.csv"`) is string-substituted.
- **System placeholders** also resolve anywhere: `${utcnow}`, `${utcnow:%Y-%m-%d}`,
  `${run_id}`.
- **Supplied per run** from: the Editor **Run** dialog (prompts when the pipeline
  declares parameters), the **Pipelines** page Run dialog, the API
  (`POST /api/execute/workflow/{id}` body `{parameter_values: {...}}`), schedules,
  and backfills. An empty value falls back to the declared default.

### Expressions — `{{ ... }}`
- n8n-style expressions, resolved **per step in topological order** by
  `expression/resolver.py` **before** that step's `execute()` runs (so a step sees
  values produced by upstream steps).
- Available helpers: `{{ $json.field }}` (current row), `{{ $now }}` / `{{ $today }}`
  (a Luxon-like `DateHelper` with `.startOf/.endOf/.plus/.minus/.toFormat`),
  `{{ $vars.NAME }}`, `{{ $('Node Name').first().col }}` / `.all()` / `.item(i)`,
  `{{ $itemIndex }}`, and `{{ $env.FPULSE_* }}`.
- Object literals accept **quoted or unquoted keys**: `{{ $now.minus({ days: 7 }) }}`
  and `{{ $now.minus({'days': 7}) }}` are equivalent.

### Runtime variables — `$vars`
- A run-scoped dict on `ExecutionContext.vars`. Written by:
  - **Set Variable** — a scalar per entry (`MAX(updated_at)`, `'prod'`, `42`).
  - **Lookup (Activity)** — `{ firstRow, rows, count, isEmpty }`.
  - **Append Variable** — pushes onto an array (hidden from the OSS palette).
- Read anywhere via `{{ $vars.NAME }}` (or `{{ $vars.NAME.firstRow.col }}` etc.).

### Where a field accepts what
Unless a node entry says otherwise, **every string/expression/sql/code field is
resolved for `${param.x}` and `{{ }}` before `execute()`**. A node only does
something *special* with variables when it explicitly reads or writes `ctx.vars`
(Set Variable, Lookup (Activity), Data Quality `split` mode). Some Action nodes
additionally support an in-node `{column}` substitution from the (first) input
row — that is a separate mechanism, noted per node.

### Multi-output (branch routing)
A brancher emits a `_split_output` text column; the executor routes each row to the
named output port/handle of the same name. The branchers in OSS are
**Conditional Split**, **Data Quality** (reject mode → `pass`/`reject`), and
**Deduplicate** (emit-duplicates → `unique`/`duplicate`). `If` and `Switch` are
single-output **filters**, not branchers.

### Config UI
The config panel is rendered from `param_schema()`. Required fields are shown
up-front with a `*` and an inline "Required" hint when empty.

---

## Data Movement

The Data Movement nodes move rows between storage systems and the in-pipeline DuckDB engine.

### Source — `source`
**Purpose.** Single palette entry that reads data from a file, database, API, or storage location. ADF *Source dataset* / generic reader, collapsing every typed reader into one configurable node.
**Design.** `execute()` reads `params["connector_type"]`, lowercases/strips it, looks it up in `SOURCE_MAP` to get a concrete `StepType` (e.g. `csv → CSV_SOURCE`, `database → DB_SOURCE`, `rest_api → API_SOURCE`, `s3 → S3_SOURCE`, `microsoft_graph → MS_GRAPH_SOURCE`, `local_table → LOCAL_TABLE_SOURCE`), then `_delegate()` instantiates that concrete node via `NodeRegistry.get(target)` with the *same* `params` and returns `node.execute(ctx)`. It is a pure router — no I/O logic of its own; the saved Connection carries auth + endpoint.
**Inputs.** None (source). All data originates from the delegated connector.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| connector_type | select | Yes | `csv` | Picks the backend reader (keys of `SOURCE_MAP`: `csv, json, parquet, excel, xml, database, rest_api, s3, azure_blob, gcs, sharepoint, onedrive, kafka, ftp, gsheet, delta, microsoft_graph, local_table`). The delegated node contributes its own config fields. |

**Output.** Whatever the delegated `*_SOURCE` node returns — a relation with the source schema and rows. No transformation by the router.
**Variables & parameters.** `connector_type` is a resolvable string (usually static); all real config belongs to the delegated node and is resolved by the executor. Does not read/write `$vars`.
**Fails when.** `connector_type` empty → `ValueError`; not a key in `SOURCE_MAP` → `ValueError` (lists supported types). Delegated-node errors propagate.

### Destination — `destination`
**Purpose.** Single palette entry that writes data to a file, database, or storage location. ADF *Sink dataset* equivalent.
**Design.** Same routing pattern as Source, via `DEST_MAP` (e.g. `parquet → FILE_SINK` — picks the Parquet writer by extension; `webhook → API_SINK`; plus `database → DB_SINK`, `s3 → S3_SINK`, `email → EMAIL_SINK`, `warehouse → WAREHOUSE_SINK`, `local_table → LOCAL_TABLE_SINK`).
**Inputs.** Effectively 1 — the delegated sink consumes the upstream relation. The router forwards `params` + `ctx`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| connector_type | select | Yes | `csv` | Picks the backend writer (keys of `DEST_MAP`: `csv, json, parquet, excel, database, s3, azure_blob, gcs, sharepoint, onedrive, kafka, rest_api, webhook, email, delta, warehouse, local_table`). The delegated sink contributes its own write-mode/target fields. |

**Output.** Whatever the delegated `*_SINK` returns. By convention sinks pass-through (return the same relation) so downstream nodes can chain; the real effect is the write.
**Variables & parameters.** As Source — `connector_type` resolvable but usually static; substantive fields on the delegated node. Does not read/write `$vars`.
**Fails when.** `connector_type` empty / not in `DEST_MAP` → `ValueError`. Delegated-node errors propagate.

### Copy Data — `copy_data`
**Purpose.** A self-contained Copy Activity: read from a source DB and write to a sink DB (with column mapping, retries, scripts, bulk loading) in one node. Direct analogue of ADF's **Copy activity**.
**Design.** Four phases. (1) **Acquire source:** with `source_connection_id`, builds a query — `source_query` (kind=query) or `SELECT * FROM <source_table>` + optional `WHERE <source_filter>` (kind=table) — runs it via a `DbSourceNode` helper and materializes into a DuckDB temp table `__copy_src`; otherwise takes the first upstream input. (2) **Mapping** via `_apply_mapping()`. (3) **Cap rows** with `LIMIT max_rows` when `max_rows>0`. (4) **Write:** with `sink_connection_id`, writes; otherwise identity pass-through. `(table_action × write_behavior)` maps to a unified mode (`upsert`/`merge → merge`, `recreate → create`, `truncate → truncate`, `overwrite → create`, else `append`); order is **Table Action → Pre-Copy script → Write → Post-Copy script**. With `enable_staging` it tries a per-dialect **bulk loader** (Postgres COPY, Snowflake stage, BigQuery load, Redshift COPY, MSSQL bcp) at an effective batch size; if no bulk plugin exists it falls back to row-by-row INSERT (other bulk failures propagate). Always returns the relation written/passed.
**Inputs.** 0 or 1. With `source_connection_id` it reads its own source and ignores upstream; otherwise consumes the first upstream relation.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| source_connection_id | connection_picker | No | `""` | Source DB connection. Empty → copy from the upstream node. |
| source_kind | select (`table`,`query`) | No | `table` | Read a whole table (+filter) or a custom query. |
| source_table | text | No | `""` | Source `schema.table`; used when kind=table. |
| source_query | sql | No | `""` | Custom `SELECT`; used when kind=query. |
| source_filter | text | No | `""` | WHERE appended in table mode. |
| sink_connection_id | connection_picker | No | `""` | Sink DB connection. Empty → identity pass-through. |
| sink_table | text | Yes (to write) | `""` | Target `schema.table`. |
| table_action | select (`none`,`autocreate`,`recreate`,`truncate`) | No | `none` | DDL before the write. |
| write_behavior | select (`append`,`overwrite`,`upsert`,`merge`) | No | `append` | Row-write semantics. |
| key_columns | column_list | No | `[]` | Match keys; required for upsert/merge. |
| pre_copy_script | sql | No | `""` | SQL on the sink before write. |
| post_copy_script | sql | No | `""` | SQL on the sink after write. |
| batch_size | number | No | `0` | Rows per bulk chunk; 0 = per-dialect default. |
| mapping_mode | select (`auto`,`explicit`) | No | `auto` | Pass-through vs apply `mappings`. |
| mappings | schema_map | No | `[]` | `{source,target,type}` → `CAST("source" AS type) AS "target"`. |
| parallel_copies | number | No | `1` | Declared parallelism. |
| skip_on_error | boolean | No | `false` | Swallow + log write exceptions. |
| max_rows | number | No | `0` | Cap copied rows; 0 = unlimited. |
| log_path | text | No | `""` | Path for skipped-row logging. |
| enable_staging | boolean | No | `false` | Enables the bulk-loader path. |

**Output.** Returns the relation it wrote (or the input in pass-through mode). Side effects: rows written to the sink; optional pre/post SQL on the sink connection.
**Variables & parameters.** All text/sql fields resolved for `${param.x}` / `{{ }}` before execute. Does not read/write `$vars`.
**Fails when.** kind=query but `source_query` empty; kind=table but `source_table` empty; a connection id not found; no source connection AND no upstream input; `sink_table` empty when writing; upsert/merge on the bulk path with no `key_columns`; a non-`BulkLoaderNotAvailable` bulk failure. Write errors propagate unless `skip_on_error`.

### Managed Table Source — `local_table_source`
**Purpose.** Reads a managed workspace Parquet table addressed by `schema.name` (not a raw path) — F-Pulse's internal datastore reader.
**Design.** Sanitizes `schema_name` (default `default`)/`table_name`, resolves the workspace id, looks the table up in the datastore, globs `part-*.parquet` under `{DATA_DIR}/tables/{ws}/{schema}/{name}/`, and returns `SELECT * FROM read_parquet('<glob>', union_by_name=true)` (so part files written under different schema versions read as one table). A registered-but-empty table yields a typed zero-row relation (not an error).
**Inputs.** None (source).
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| schema_name | string | No | `default` | Logical schema of the managed table; sanitized. |
| table_name | string | Yes | `""` | Managed table name; sanitized; required. |

**Output.** A relation over all part files (union-by-name), or a typed zero-row relation for an empty table.
**Variables & parameters.** `schema_name`/`table_name` resolvable strings; does not read/write `$vars`.
**Fails when.** `table_name` empty → `ValueError`; no matching managed table → `ValueError`.

### Managed Table Sink — `local_table_sink`
**Purpose.** Writes the upstream relation to a managed workspace Parquet table in `replace`/`append`/`merge` mode, refreshing datastore metadata and enforcing a schema-drift policy. ADF managed-table sink with built-in upsert.
**Design.** Sanitizes names, validates `mode`, requires `merge_on` for merge. **Before touching disk** it evaluates `schema_policy` against the existing columns; a rejected decision publishes a rejected-drift event and raises with zero side effects. On acceptance: **replace** deletes existing parts then writes `part-000.parquet`; **append** writes a timestamped part; **merge** is last-writer-wins on `merge_on` (existing minus matching keys `UNION ALL` incoming → staging part → rotate). Then `_refresh_metadata()` recomputes row/column/size/part counts and upserts `storage_tables`/`storage_columns`; accepted real drift records a schema-version row + `SchemaDriftDetected` event. Captures Pipeline-Data-Prep provenance when the single upstream step is a `data_wrangler`.
**Inputs.** 1 — the single upstream relation.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| schema_name | string | No | `default` | Target managed schema; sanitized. |
| table_name | string | Yes | `""` | Target managed table; sanitized; required. |
| mode | select | No | `replace` | `replace` / `append` / `merge` (upsert on key). |
| merge_on | array | No | `[]` | Key columns; required when mode=merge. |
| schema_policy | select | No | `add_columns` | `strict` (fail on any change) / `add_columns` (allow new nullable cols) / `compatible` (adds + lossless widening) / `allow_all_with_warning`. |

**Output.** Pass-through (returns the upstream relation). Side effects: Parquet parts written/rotated; metadata upserted; drift events; provenance stamping.
**Variables & parameters.** `schema_name`/`table_name` resolvable; does not read/write `$vars`.
**Fails when.** `table_name` empty; invalid `mode`; merge with empty `merge_on`; no upstream relation; schema-policy rejection (raised before any bytes written).

---

## Transform

### Data Wrangler — `data_wrangler`
**Purpose.** A single tile hosting an ordered list of small sub-steps (filter, select, rename, cast, derive, group, sort, dedupe, sample, flatten) — an inline mini-pipeline. Comparable to ADF Data Flow with stacked transformations.
**Design.** `execute()` registers the input as `__wrangler_input`, then `compile_wrangle()` folds every enabled sub-step into ONE SQL `SELECT` by wrapping each prior fragment as a subquery (`_w0`, `_w1`, …) — the optimizer fuses them; nothing is materialized at run time. Per-op compilers: filter → `WHERE`; select → projection; rename → `SELECT * RENAME (...)`; cast → `SELECT * REPLACE (CAST(...))` with a type allowlist; derive → `SELECT *, (expr) AS name`; group_by → aggregation with a func allowlist (`SUM/COUNT/AVG/MIN/MAX/COUNT_DISTINCT`); sort → `ORDER BY`; dedupe → `ROW_NUMBER()` keep `__dedup_rn=1`; sample → `LIMIT`/`USING SAMPLE`; flatten → `(col).*` struct expansion (or prefixed explicit fields). Disabled/unknown sub-steps skipped; empty recipe → `SELECT * FROM __wrangler_input`.
**Inputs.** One; only `inputs[0]` (`ValueError` if none).
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `steps` | `wrangler_steps` | no | `[]` | Ordered list of `{op, config, enabled?, label?}` sub-steps; edited via the DataWranglerConfig component (drag to reorder, toggle to disable). |

**Output.** Schema/rows depend on the compiled sub-steps; single `output` port. Pass-through when empty/all-disabled.
**Variables & parameters.** Sub-step `config` string fields resolve `${param.x}` / `{{ }}` before execute. Does not read/write `ctx.vars`.
**Fails when.** No input; invalid input identifier; cast with missing/unsupported `to_type`; group_by with an unsupported aggregation func.

### Transform (SQL) — `transform`
**Purpose.** Reshape data with raw SQL against the upstream relation, exposed as `source_table` (and `input`). ADF Data Flow custom SQL / a SQL/Code node.
**Design.** Resolves routed inputs, registers the first as both `source_table` and `input`, and (2026-06-10 hardening) registers ONLY directly-wired inputs as additional named tables — by sanitized step-id and by sanitized node label — then returns `ctx.conn.sql(expression)` verbatim.
**Inputs.** One or more; `inputs[0]` is the primary `source_table`; others addressable by step-id/label inside the SQL.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `expression` | `sql` | yes | `SELECT *, CURRENT_DATE AS processed_at FROM source_table` | The SQL executed against the registered input tables. |

**Output.** Whatever the SQL produces (arbitrary schema/rows). Single `output` port.
**Variables & parameters.** `expression` resolves `${param.x}` / `{{ }}` before execute. Does not read/write `ctx.vars`.
**Fails when.** No input; empty `expression`; any DuckDB error from the SQL.

### Filter — `filter`
**Purpose.** Keep only rows matching a condition — raw SQL `WHERE` or visual rules. ADF Filter / n8n Filter.
**Design.** In `rules` mode, `rule_groups` (or flat `rules`) compile via `_rules_to_condition`: `contains/starts_with/ends_with → LIKE`, `in/not_in → IN(...)`, `between → BETWEEN`, `is_null/is_not_null → bare predicate`, scalars quoted vs unquoted by `float()` probe. In `expression` mode it uses `condition` directly. Empty/`TRUE` → pass-through; else `SELECT * FROM __filter_input WHERE <condition>`.
**Inputs.** One; `inputs[0]`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `mode` | `select` (`expression`,`rules`) | no | `expression` | Raw-SQL vs visual-rule input. |
| `condition` | `expression` | no | `column_name IS NOT NULL` | The WHERE clause (expression mode). |
| `combinator` | `select` (`AND`,`OR`) | no | `AND` | Joins rules in the flat list. |
| `rules` | `rule_list` | no | `[]` | Flat `{column,op,value}` rules. |
| `rule_groups` | `rule_groups` | no | `[]` | Multiple groups, each with its own combinator; overrides flat `rules`. |
| `group_combinator` | `select` (`AND`,`OR`) | no | `AND` | Joins the rule groups. |

**Output.** Same columns, fewer/equal rows. Single `output` port. Pass-through when condition empty/`TRUE`.
**Variables & parameters.** `condition` + rule `value` fields resolve `${param.x}` / `{{ }}`. Does not read/write `ctx.vars`.
**Fails when.** No input. (Rule compilation is defensive; a malformed raw `condition` surfaces as a DuckDB error.)

### Derived Column — `derived_column`
**Purpose.** Add (or replace) computed columns from row-local expressions, or cross-row expressions (LAG/LEAD/running totals) via an optional per-column window. ADF Derived Column / n8n Set with formulas.
**Design.** Registers `__derived_input`. Per `columns` entry: if a `window` dict is present and the expression lacks `OVER`, wraps it as `expr OVER (PARTITION BY … ORDER BY …)`. Add-vs-replace: a `name` colliding with an input column requires `replace: true` (then `SELECT * EXCLUDE (...), <extras>`); a collision without `replace` raises; no collision → `SELECT *, <extras>`.
**Inputs.** One; `inputs[0]`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `columns` | `derived_list` | yes | `[{"name":"new_col","expression":"1"}]` | `{name, expression}`, optionally `{replace: bool}` and `{window: {partition_by:[…], order_by:[…]}}`. Empty → input unchanged. |

**Output.** Input columns plus one column per entry (or replaced in place). Single `output` port.
**Variables & parameters.** `expression` (and window column refs) resolve `${param.x}` / `{{ }}`. Does not read/write `ctx.vars`.
**Fails when.** No input; a derived name collides without `replace`; bad expression → DuckDB error.

### Sort — `sort`
**Purpose.** Order rows by one or more columns, each with direction and NULLS placement. ADF Sort / n8n Sort.
**Design.** Reads `sort_rules` (fallback `sort_by`); entries may be `"amount"`, `"amount DESC"`, `"amount DESC NULLS LAST"`, or dicts. Validates triples `(column, direction, nulls)`, checks for duplicate/missing columns, then `ORDER BY "<col>" <dir> [NULLS <pos>], …`. No rules → input unchanged.
**Inputs.** One; `inputs[0]`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `sort_by` | `column_list` | yes | `[]` | Entries `'<column> [ASC|DESC] [NULLS FIRST|LAST]'` or dicts (alias: `sort_rules`). |
| `direction` | `select` (`ASC`,`DESC`) | no | `ASC` | Default direction for entries without an inline one. |

**Output.** Same columns, reordered rows. Single `output` port.
**Variables & parameters.** Entry strings resolve `${param.x}` / `{{ }}`. Does not read/write `ctx.vars`.
**Fails when.** No input; invalid entry format/direction/NULLS; duplicate column; column not found.

### Sample — `sample`
**Purpose.** Return a subset by fixed count or percentage, deterministically (first N) or randomly (optionally seeded). ADF Sample / n8n Limit + random.
**Design.** Resolves a mutually-exclusive `mode` (`rows`/`percent`, inferred from legacy `count`/`fraction` if unset) and `method` (`first`/`random`). Percent: `LIMIT round(total*pct/100)` (first) or `USING SAMPLE pct PERCENT` (`bernoulli, seed` when seeded). Rows: `LIMIT n` (first) or `USING SAMPLE n ROWS` (`reservoir(n ROWS) REPEATABLE (seed)` when seeded).
**Inputs.** One; `inputs[0]`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `mode` | `select` (`rows`,`percent`) | no | `rows` | Fixed count vs proportion. |
| `count` | `number` | no | `100` | Rows (rows mode). |
| `percent` | `number` | no | `None` | Proportion 0–100 (percent mode). |
| `method` | `select` (`first`,`random`) | no | `first` | Deterministic prefix vs statistical sample. |
| `seed` | `number` | no | `None` | Makes random sampling reproducible. |

**Output.** Same columns, fewer rows. Single `output` port.
**Variables & parameters.** Resolve `${param.x}` / `{{ }}` before execute. Does not read/write `ctx.vars`.
**Fails when.** No input; invalid `method`; non-integer `seed`; percent missing/non-numeric/out of (0,100]; non-numeric or `<=0` `count`.

### Deduplicate — `deduplicate`
**Purpose.** Remove duplicate rows by key column(s), keeping first or last per key, with optional dual-output for the removed rows. ADF dedup / n8n Remove Duplicates.
**Design.** Accepts `key`/`columns`; validates each key exists. Parses `order_by` into `(column, direction)` pairs (each must exist); for `keep_last` every direction is reversed. Computes `ROW_NUMBER() OVER (PARTITION BY <keys> [ORDER BY …]) AS __rn`. Normal mode keeps `__rn=1` and drops the helper; `emit_duplicates` tags `CASE WHEN __rn=1 THEN 'unique' ELSE 'duplicate' END AS _split_output`.
**Inputs.** One; `inputs[0]`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `key` | `column_list` | yes | `[]` | Columns defining uniqueness (alias `columns`). |
| `strategy` | `select` (`keep_first`,`keep_last`) | no | `keep_first` | Which row per key survives, by `order_by`. |
| `order_by` | `text` | no | `""` | `'<column> [ASC|DESC]'` ordering within each key partition. |
| `emit_duplicates` | `boolean` | no | `False` | Dual-output: tag rows `unique`/`duplicate` instead of dropping. |

**Output.** Normal: deduped rows. Dual-output: all rows + `_split_output` routed to **Unique** + **Duplicate** ports. Without `order_by`, the survivor is engine-arbitrary.
**Variables & parameters.** `order_by` resolves `${param.x}` / `{{ }}`. Does not read/write `ctx.vars`.
**Fails when.** No input; neither `key`/`columns`; invalid `strategy`; a key/`order_by` column not found; invalid `order_by` entry.

### Schema Mapper — `schema_mapper`
**Purpose.** Map source columns to a target schema with rename, reorder, type coercion, and per-field defaults. ADF Select/mapping with cast.
**Design.** Per `{source, target, type, default}`: skips no-target rows; resolves `type` via `_SQL_TYPES` (default `VARCHAR`); builds `CAST([COALESCE("src", default)] AS type) AS "target"` (COALESCE only with a default), or `CAST(<default> AS type)` / `CAST(NULL AS type)` when source missing. `keep_unmapped` appends untouched source columns. Output order follows the mappings list. (Frontend adds an **Auto-map** action that bootstraps a straight-through grid or fuzzy-fills blank sources by name.)
**Inputs.** One; `inputs[0]`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `mappings` | `schema_map` | yes | `[]` | `{source,target,type,default}` — rename/reorder/cast/default. |
| `keep_unmapped` | `boolean` | no | `False` | Append source columns not referenced by any mapping. |

**Output.** Target schema in mapping order (+ unmapped when enabled). Single `output` port. Pass-through when empty.
**Variables & parameters.** Mapping string fields resolve `${param.x}` / `{{ }}`. Does not read/write `ctx.vars`.
**Fails when.** No input. (Mapping is non-raising — unknown types → VARCHAR, missing sources → NULL/default; cast failures surface from DuckDB.)

### Data Quality — `data_quality`
**Purpose.** Validate rows against declarative rules, then drop / fail / tag / reject (two ports) / split failures aside. ADF Assert / quality split.
**Design.** Each rule compiles via `_DQ_OPS` (`not_null, is_null, eq, ne, gt, lt, gte, lte, in, not_in, regex, between, min_length, max_length`); the row predicate is their `AND`. `quality_threshold>0` computes pass rate (raises in fail mode / logs otherwise); `include_score` adds `__dq_score`. Modes: **fail** → raise on any failing row; **tag** → add `(predicate) AS __dq_passed`, keep all; **reject** → `CASE WHEN predicate THEN 'pass' ELSE 'reject' END AS _split_output`; **split** → store failing rows in `ctx.vars["_dq_failures_<step_id>"]`, emit only passing; **drop** (default) → emit only passing.
**Inputs.** One; `inputs[0]`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `rules` | `rule_list` | yes | `[]` | `{column,op,value}` checks. Empty → pass-through. |
| `mode` | `select` (`drop`,`fail`,`tag`,`reject`,`split`) | no | `drop` | What happens to failing rows. |
| `quality_threshold` | `number` | no | `0` | If >0, treat below-threshold pass rate as failure per mode. |
| `include_score` | `boolean` | no | `False` | Add `__dq_score` (0–100) per row. |
| `include_profile` | `boolean` | no | `False` | Log per-column null%/distinct (logs only). |

**Output.** drop/split → passing rows; tag → all + `__dq_passed`; reject → all + `_split_output` routed to **Pass**/**Reject**; fail → all rows or aborts. Single `output` except in reject mode.
**Variables & parameters.** Rule `value` fields resolve `${param.x}` / `{{ }}`. **Writes** `ctx.vars["_dq_failures_<step_id>"]` in `split` mode.
**Fails when.** No input; `mode='fail'` with any failing row or pass rate below `quality_threshold`.

### Data Profile — `data_profile`
**Purpose.** Emit one row per source column with summary statistics (null %, distinct, min/max, top value, mean/median/stddev, length stats). A profiling/audit node.
**Design.** Registers input, materializes `__profile_src` (`USING SAMPLE n` when `sample_rows>0`, else full). Zero rows → fixed-shape empty sentinel. Reads types via `DESCRIBE`, applies `include_columns` then `exclude_columns`. Per column builds one `SELECT` of the stat set (min/max via `TRY_CAST … AS VARCHAR`; mean/median/stddev via `AVG/QUANTILE_CONT/STDDEV_POP` over `TRY_CAST … AS DOUBLE`; length stats; optional top value via `GROUP BY … LIMIT 1`); all joined with `UNION ALL`. On failure (e.g. STRUCT columns) it falls back to a degraded profile nulling those stats.
**Inputs.** One; `inputs[0]`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `sample_rows` | `int` | no | `0` | Cap to N rows (`USING SAMPLE`); 0 = full scan. |
| `include_top_value` | `bool` | no | `True` | Compute the most-common value per column. |
| `include_columns` | list[str] | no | `[]` | Allowlist — profile only these columns (empty = all). |
| `exclude_columns` | list[str] | no | `[]` | Skip these columns (applied after `include_columns`). |
| `passthrough_data` | `bool` | no | `False` | **Dual-output (C2):** also emit the original rows on a second `data` port. |

**Output.** Primary `output` port = the stats report — one row per profiled column: `column, data_type, row_count, null_count, null_pct, distinct_count, distinct_pct, min_value, max_value, top_value, top_value_count, mean_value, median_value, stddev_value, avg_length, max_length`. When `passthrough_data` is on, a secondary `data` port emits the **original input rows unchanged** (heterogeneous multi-output — a *different* schema per port, via `ctx.set_named_output`), so you can profile a dataset *and* keep building from the same rows (wire `Report`→a sink, `Data`→the next transform). Default (off) = report only, exactly as before.
**Variables & parameters.** Resolve `${param.x}` / `{{ }}` before execute. Does not read/write `ctx.vars`.
**Fails when.** No input. (Zero rows / empty filters → empty sentinel; stat failures → degraded profile.)

### Flatten / Explode — `flatten_explode`
**Purpose.** Expand nested data — flatten a STRUCT/JSON column into top-level columns, or explode a LIST/ARRAY column into one row per element. ADF Flatten / n8n Split Out + Item Lists.
**Design.** Registers `__flatten_input`. `column` supports dot-notation (first segment real column, deeper become bracket access; dotted = explode-only). **explode**: rejects non-LIST/JSON/VARCHAR; `SELECT <other cols>, UNNEST(<list_expr>) AS "<prefix?leaf>"`; `keep_empty` wraps NULL/empty as `[NULL]`; `add_index` adds a parallel `UNNEST(range(...))` index; UNNEST failures fall back to `LATERAL FLATTEN`. **flatten**: probes struct fields via `LIMIT 0` and builds `"col"."field" AS "<pfx>field"`; falls back to JSON-key extraction.
**Inputs.** One; `inputs[0]`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `mode` | `select` (`flatten`,`explode`) | no | `flatten` | Struct→columns vs array→rows. |
| `column` | `text` | yes | `""` | The nested column (struct/JSON/array); dot-notation for nested arrays (explode-only). |
| `prefix` | `text` | no | `""` | Prefix for expanded/exploded names (default: original column name). |
| `keep_original` | `boolean` | no | `False` | Keep the nested column (flatten). |
| `keep_empty` | `boolean` | no | `False` | Keep rows whose array is NULL/empty (explode → element NULL). |
| `add_index` | `boolean` | no | `False` | Add `<alias>_index` (1-based) (explode). |

**Output.** flatten → original (minus nested unless kept) + expanded fields; explode → other columns + unnested element (+ optional index), rows multiplied. Single `output` port.
**Variables & parameters.** `column`/`prefix` resolve `${param.x}` / `{{ }}`. Does not read/write `ctx.vars`.
**Fails when.** No input; `column` missing/not found; dotted path in flatten mode; explode on a non-array column; keep_empty/add_index on a non-LIST column; final explode/flatten failure after fallbacks.

### Split Out — `split_out` (frontend preset of `flatten_explode`)
**Purpose.** Explode one array column into one row per element — the n8n "Split Out" workflow. No separate backend node.
**Design.** A FRONTEND palette preset (`workflowStore.ts` `NODE_PRESETS`): drops a `flatten_explode` labeled "Split Out" pre-set with `params: { mode: 'explode' }`. One backend node, two tiles. Execution, inputs, output, variables, and failures are exactly those of **Flatten / Explode** in explode mode.

### SCD Type 2 — `scd2`
**Purpose.** Maintain a Type-2 slowly-changing dimension — versioned history per business key, emitting the full new dimension state. Equivalent to ADF's SCD data-flow pattern / dbt snapshots.
**Design.** Python-side change detection (not pure SQL). For each business key it compares `tracked_columns` against the current version via a deterministic hash: unchanged → keep; changed → close the prior version (set `effective_to_column` = run time, `current_flag_column` = false) and open a new one (`effective_from_column` = run time, `effective_to_column` = `null_high_water` sentinel, `current_flag_column` = true) with a deterministic SHA-256 `surrogate_key_column`. `delete_detection` handles business keys present in the dimension but missing from the feed (`ignore` keeps the orphan as-is; `soft_close` marks it `is_current=false`, `valid_to=run_time`). Emits the full new dimension state via a `__scd2_out` temp table.
**Inputs.** 1 or 2 — the incoming feed, plus optionally the current dimension (target) to version against.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `business_key` | list[str] | yes | `[]` | Column(s) identifying a single dimension entity. |
| `tracked_columns` | list[str] | yes | `[]` | Columns whose change triggers a new version. |
| `effective_from_column` | str | no | `valid_from` | Version-start timestamp column. |
| `effective_to_column` | str | no | `valid_to` | Version-end timestamp column. |
| `current_flag_column` | str | no | `is_current` | Boolean flag for the latest active version. |
| `surrogate_key_column` | str | no | `scd_id` | Deterministic SHA-256 surrogate key column. |
| `null_high_water` | str | no | `9999-12-31` | Sentinel placed in `valid_to` for the active version. |
| `passthrough_columns` | list[str] | no | `[]` | Carried through but NOT used for change detection. |
| `delete_detection` | str | no | `ignore` | `ignore` (keep orphans) / `soft_close` (close keys missing from the feed). |

**Output.** The full versioned dimension (existing + closed + newly opened versions) with the effective-date, current-flag, and surrogate-key columns.
**Variables & parameters.** String fields resolve `${param.x}` / `{{ }}` before execute. Does not read/write `ctx.vars`.
**Fails when.** `business_key` or `tracked_columns` missing; a required column absent on the incoming relation.

---

## Combine

### Join — `join`
**Purpose.** Combine two datasets by matching key column(s), with full enterprise join-type support. ADF Join / n8n Merge (by-key).
**Design.** Registers the two inputs as views `__join_left` / `__join_right`. The ON clause follows `key_mode`: `same_key` → `__join_left."k" = __join_right."k"` AND-joined; `mapped_keys` → one `__join_left."left" <op> __join_right."right"` per `key_pairs`; `custom` → `custom_on` verbatim. `FULL` → `FULL OUTER`; `SEMI`/`ANTI` → `WHERE [NOT] EXISTS`. Projection: explicit `select_left`/`select_right` if set; otherwise `_default_projection()` keeps all left columns, collapses `same_key` keys to a single `COALESCE(left,right) AS k` (survives RIGHT/FULL null-left rows), and suffixes any clashing right column with `dup_column_suffix`. `left_input_id` pins the left side (else index 0).
**Inputs.** Exactly 2 (raises if fewer); left/right by `left_input_id`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| join_type | select | No | `INNER` | INNER/LEFT/RIGHT/FULL/SEMI/ANTI/CROSS. |
| key_mode | select | No | `same_key` | same_key / mapped_keys / custom. |
| join_key | column_list | No | `[]` | same_key columns (list or comma-string). |
| key_pairs | key_pair_list | No | `[]` | mapped_keys `{left,right,operator}` (=,>,<,>=,<=,!=). |
| custom_on | expression | No | `""` | Full SQL ON; tables `__join_left`/`__join_right`. |
| select_left | text | No | `""` | Explicit left projection; empty = all. |
| select_right | text | No | `""` | Explicit right projection; empty = all. |
| dup_column_suffix | text | No | `_right` | Suffix for clashing right non-key columns (default projection). |

**Output.** Rows per join semantics (or left-only for SEMI/ANTI); collision-safe default projection unless overridden.
**Variables & parameters.** All string fields resolve `${param.x}` / `{{ }}`. Does not read/write `$vars`.
**Fails when.** Fewer than 2 inputs; a missing input; mapped_keys with empty `key_pairs`; same_key with no `join_key`. Bad `custom_on`/`select_*` or missing columns → DuckDB errors.

### Aggregate — `aggregate`
**Purpose.** Group rows and compute aggregate functions (`GROUP BY` + `HAVING`/`ORDER BY`); empty Group By = one global row. ADF Aggregate / n8n Summarize.
**Design.** Registers `__agg_input`, builds `SELECT <group cols>, <agg exprs> FROM __agg_input [GROUP BY …] [HAVING …] [ORDER BY …]`. `functions` accepts dict-shorthand, bare strings (→ COUNT of col), or normalized dicts; each builds an aliased expr (`COUNT(*)`, `COUNT(DISTINCT)`, `MEDIAN`, `PERCENTILE_CONT/_DISC(p) WITHIN GROUP`, `STRING_AGG(col, sep)`, `FIRST`/`LAST`, `CUSTOM` raw, else `FUNC(col)`). No exprs → `COUNT(*) AS "count"`.
**Inputs.** 1; `inputs[0]`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| group_by | column_list | No | `[]` | Grouping columns; empty = global aggregation. |
| functions | aggregate_list | Yes | `[{column:"*",function:"COUNT",alias:"count"}]` | `{column,function,alias,…}`; extras: `percentile`, `separator`, `expression`. |
| having | expression | No | `""` | Post-aggregation group filter. |
| order_by | text | No | `""` | Raw ORDER BY on the result. |

**Output.** One row per group (or one global row); group columns then aliased aggregates.
**Variables & parameters.** `having`/`order_by` + function strings resolve `${param.x}` / `{{ }}`. Does not touch `$vars`.
**Fails when.** No input. Invalid SQL / missing columns / non-numeric `percentile` raise at runtime.

### Lookup — `lookup`
**Purpose.** Enrich the main stream with selected columns from a reference dataset matched on a key — a true lookup, NOT the control-flow Lookup activity. ADF Lookup-for-enrichment / n8n Merge (Enrich).
**Design.** Resolves the reference by `lookup_input_id` (default = second connection); the other input is the main stream. Return columns = `return_columns` minus the key, or all reference columns except `lookup_key`; each projected as `__ref."col" AS "col"` (aliased `col_lookup` on collision). `multiple_match='first'` wraps the ref in `QUALIFY ROW_NUMBER() OVER (PARTITION BY key)=1`. `no_match='drop'` → INNER JOIN; `'keep'` → LEFT JOIN. `main_key` defaults to `lookup_key`.
**Inputs.** Exactly 2 (raises if fewer); reference chosen by `lookup_input_id`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| lookup_key | column | Yes | `""` | Key column on the reference dataset. |
| main_key | column | No | `""` (→ lookup_key) | Key on the main stream. |
| lookup_input_id | text | No | `""` | Step id of the reference input; default = second connection. |
| no_match | select | No | `keep` | keep = LEFT JOIN; drop = INNER JOIN. |
| multiple_match | select | No | `all` | all = every match (may duplicate); first = one per key. |
| return_columns | text | No | `[]` | Reference columns to append; empty = all except the key. |

**Output.** Main columns + selected reference columns (collision-aliased `_lookup`). Row count varies with `no_match`/`multiple_match`.
**Variables & parameters.** All key/column string fields resolve `${param.x}` / `{{ }}`. Does not read/write `$vars`.
**Fails when.** Fewer than 2 inputs; a missing input; empty `lookup_key`; `main_key`/`lookup_key` absent on its side; a `return_columns` entry not in the reference.

### Union — `union`
**Purpose.** Stack rows from two or more datasets into one. ADF Union / n8n Merge (Append).
**Design.** Registers each input as `__union_<i>`, joins `SELECT * FROM __union_<i>` with the chosen operator: `all → UNION ALL` (keep dups, positional), `distinct → UNION` (dedupe, positional), `by_name → UNION ALL BY NAME` (match by name, NULL-fill).
**Inputs.** N (≥2 required); all inputs in id order.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| mode | select | No | `all` | all / distinct / by_name (schema union). |

**Output.** Concatenation of all input rows; positional schema for all/distinct, name-merged superset for by_name.
**Variables & parameters.** `mode` resolves `${param.x}` / `{{ }}`. Does not touch `$vars`.
**Fails when.** Fewer than 2 inputs; invalid mode. Positional unions on mismatched columns raise from DuckDB.

### Pivot — `pivot`
**Purpose.** Turn distinct values of one column into separate columns, aggregating a value column per group. ADF Pivot / spreadsheet pivot.
**Design.** `PIVOT __pivot_input ON "<pivot_col>"[ IN (...)] USING <agg>("<value_col>") <group clause>`. `group_by` set → `GROUP BY "c", …`; else `GROUP BY ALL`. `pivot_values` (list/comma-string) freezes the output columns via an explicit `IN (...)`. `fill_value` (non-empty) wraps every value column (non-group) in `COALESCE("c", <literal>)` — numeric bare, else quoted.
**Inputs.** 1; `inputs[0]`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| pivot_column | text | Yes | `""` | Column whose distinct values become output columns. |
| value_column | text | Yes | `""` | Column aggregated into each pivot cell. |
| agg_function | select | No | `SUM` | SUM/COUNT/AVG/MIN/MAX. |
| group_by | column_list | No | `[]` | Row-grouping columns; empty = GROUP BY ALL. |
| pivot_values | text | No | `""` | Comma-separated freeze list (pins output columns). |
| fill_value | text | No | `""` | Replacement for empty cells via COALESCE. |

**Output.** One row per group; group columns + one column per pivot value; NULL cells optionally COALESCEd.
**Variables & parameters.** All fields resolve `${param.x}` / `{{ }}`. Does not read/write `$vars`.
**Fails when.** No input; missing pivot/value column; either column not present. Invalid `agg_function` → DuckDB error.

### Unpivot — `unpivot`
**Purpose.** Turn columns back into rows (melt) — the inverse of Pivot. ADF Unpivot / pandas melt.
**Design.** With `id_columns`, pre-projects to `id_columns + columns`; empty = keep all non-unpivoted columns. `SELECT * FROM <src> UNPIVOT [INCLUDE NULLS ]("<value_col>" FOR "<name_col>" IN (<columns>))`. `include_nulls` toggles `INCLUDE NULLS` (the FROM-expression form is used because that keyword is only valid there).
**Inputs.** 1; `inputs[0]`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| columns | column_list | Yes | `[]` | Columns to unpivot into rows. |
| id_columns | column_list | No | `[]` | Identifier columns carried onto every output row; empty = keep all non-unpivoted. |
| name_column | text | No | `attribute` | Output column holding the source column name. |
| value_column | text | No | `value` | Output column holding the value. |
| include_nulls | boolean | No | `False` | Keep rows whose value is NULL. |

**Output.** One row per (kept identifier row × unpivoted column) + the name/value pair; NULL rows excluded unless `include_nulls`.
**Variables & parameters.** Field strings resolve `${param.x}` / `{{ }}`. Does not touch `$vars`.
**Fails when.** No input; empty `columns`; a `columns`/`id_columns` entry not in the input.

### Window — `window`
**Purpose.** Add window-function columns (ranks, running totals, neighbor values) while keeping all rows. ADF Window / SQL `OVER (...)`.
**Design.** Builds one window spec from `partition_by`, `order_by` (inline direction wins over `order_direction`/legacy `sort_direction`), and `frame` (verbatim). Per `window_functions` entry the expr is built by function name (`ROW_NUMBER/RANK/DENSE_RANK/CUME_DIST/PERCENT_RANK`; `NTILE(n)`; `LAG/LEAD(col, offset)`; `FIRST_VALUE/LAST_VALUE`; `NTH_VALUE(col, n)`; `SUM/AVG/MIN/MAX/COUNT`; else custom) → `<expr> OVER (<spec>) AS "<alias>"`; final `SELECT *, <window exprs>`.
**Inputs.** 1; `inputs[0]`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| partition_by | column_list | No | `[]` | Partitioning columns. |
| order_by | column_list | No | `[]` | Sort columns within a partition; entries may carry inline ASC/DESC. |
| order_direction | select | No | `ASC` | Default direction for plain column names. |
| frame | text | No | `""` | Optional ROWS/RANGE BETWEEN frame (raw). |
| window_functions | window_function_list | Yes | `[{function:"ROW_NUMBER",alias:"row_num"}]` | `{function,column,alias,offset,n}`. |

**Output.** All input rows/columns + one column per window function; row count unchanged.
**Variables & parameters.** `frame`, order-by entries, function strings resolve `${param.x}` / `{{ }}`. Does not read/write `$vars`.
**Fails when.** No input; an order-by entry whose direction token isn't ASC/DESC; ranking/nav functions without ORDER BY (DuckDB error); non-integer `offset`/`n`.

---

## Control Flow

### If Condition — `if_condition`  *(palette: **If Condition**)*
**Purpose.** A true ADF-style two-way branch (2026-06-15): routes each row to a **True** or **False** output by a condition.
**Design.** Reads the single routed input, registers `__if_input`, runs `SELECT *, CASE WHEN ({condition}) THEN 'true' ELSE 'false' END AS _split_output FROM __if_input`. The executor routes rows to the True/False port and strips the tag. `condition` falls back to legacy `expression`, then `1=1`. `default_branch_port = 'true'`; legacy `output`-port edges are migrated to `true` (migrate_legacy_node_types), so old keep-matching-rows pipelines behave identically.
**Inputs.** Exactly one (routed by port).
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `condition` | expression | Yes | `1=1` | SQL boolean. Rows where it's TRUE go to the True output; the rest to False. (Legacy `expression` also read.) |

**Output.** Two output handles — **True** / **False** — each carrying the original columns (the `_split_output` tag is stripped on routing).
**Variables & parameters.** `condition` resolves `${param.x}` / `{{ }}`. Reads/writes no `$vars`.
**Fails when.** No input; invalid SQL / unknown columns in `condition`.

### ~~Switch~~ (retired) — `switch_case`
**Status.** Retired from the palette (2026-06-15). "Switch" is now `conditional_split` (the real multi-output brancher, below). The backend `switch_case` node stays registered for back-compat with old pipelines (single-case filter; case values are now SQL-escaped). Do not use for new pipelines.

### Switch — `conditional_split`  *(palette: **Switch**)*
**Purpose.** The true multi-output brancher (ADF Switch): tags each row with a `_split_output` label naming the matched condition, so the executor routes rows to named output ports.
**Design.** Registers `__split_input`. No conditions → `SELECT *, '{default_output}' AS _split_output`. Default mode builds `CASE WHEN {cond} THEN '{name}' … ELSE '{default_output}' END AS _split_output` (first match wins). `filter` mode with `active_output` returns only that branch's rows (`WHERE {cond}`, or `WHERE NOT (cond1 OR cond2 …)` for the default). `mode`/`active_output` have legacy fallbacks. (Docstring mentions an `all_match` mode that is not implemented.)
**Inputs.** Exactly one.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `conditions` | rule_list | Yes | `[{"name":"match","condition":"1=1"}]` | Ordered `{name, condition}`; name = output port; first match wins. |
| `default_output` | text | No | `default` | Label for rows matching no condition. |
| `mode` | select (`first_match`,`filter`) | No | `first_match` | Label all rows vs return one named output. |
| `active_output` | text | No | `""` | In filter mode, which output to return. |

**Output.** All columns + a `_split_output` text column (first_match); in filter mode only the selected branch. The executor routes by `_split_output` to the named ports.
**Variables & parameters.** Condition strings + outputs resolve `${param.x}` / `{{ }}`. Reads/writes no `$vars`.
**Fails when.** No input; invalid condition SQL. (Labels/conditions interpolated unescaped — see appendix.)

### ForEach (Batch) — `foreach_loop`
**Purpose.** A batch processor: chunks the input into fixed-size batches and tags each row with its batch index/total, then UNIONs them back. NOT a per-item sub-pipeline runner (see For Each (Run Pipeline)).
**Design.** Counts rows; 0 → empty relation with `_batch_index`/`_batch_total`. `batch_size<=0` or `>=total` → one batch. Else `num_batches=ceil(total/batch_size)`; per batch a temp table `__foreach_b{i}` via `SELECT *, {i} AS _batch_index, {n} AS _batch_total … LIMIT … OFFSET …`, all `UNION ALL`'d. `on_error="continue"` skips failed batches. `mode` is recorded but execution is sequential.
**Inputs.** Exactly one; re-emitted in batch order.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `batch_size` | number | No | `0` | Rows per batch; 0 (or ≥ total) = single batch. |
| `mode` | select (`sequential`,`parallel`) | No | `sequential` | Recorded; engine runs sequentially. |
| `on_error` | select (`fail`,`continue`) | No | `fail` | Abort on first batch error vs skip failed batches. |

**Output.** All rows + `_batch_index` (0-based) and `_batch_total`. Single relation. No `$vars`.
**Variables & parameters.** No expression fields. Reads/writes no `$vars`.
**Fails when.** No input; with `on_error="fail"`, a batch-build error.

### Wait / Delay — `wait_delay`
**Purpose.** Pause execution for a fixed duration, then pass input through unchanged. ADF Wait / n8n Wait.
**Design.** Computes `seconds` (canonical `seconds`, else legacy `duration`×`unit`), clamps to `0..300`, `time.sleep()` if >0, returns the input.
**Inputs.** Exactly one, returned unchanged.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `seconds` | number | No | `1` | Seconds to pause, capped at 300. (Legacy `duration`+`unit` converted when absent.) |

**Output.** The input relation, unchanged.
**Variables & parameters.** `seconds` numeric. Reads/writes no `$vars`.
**Fails when.** No input. (Duration is hard-capped, not rejected.)

### Set Variable — `set_variable`
**Purpose.** Evaluate each `{name, expression}` once and store the scalar on `ctx.vars[name]`, read downstream as `{{ $vars.NAME }}`; input passes through unchanged. ADF Set Variable. Repurposed 2026-06-15 — it no longer appends columns (use Derived Column for that).
**Design.** Reads the routed input (optional). Per entry: `SELECT ({expr}) AS __v FROM __setvar_input LIMIT 1` (with input, so expressions can reference columns / aggregates) or `SELECT ({expr}) AS __v` (no input); the first cell is written to `ctx.vars[name]`. Returns the input unchanged (or an empty relation when no input).
**Inputs.** Zero or one.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `variables` | derived_list | Yes | `[{"name":"my_var","expression":"'default'"}]` | Each `{name, expression}` is evaluated once and stored to `ctx.vars[name]`. Input rows are NOT modified. |

**Output.** Pass-through; the real product is the `$vars` writes (one per entry).
**Variables & parameters.** Each `expression` resolves `${param.x}` / `{{ }}` then is evaluated by DuckDB. **Writes** `ctx.vars[name]` (read as `{{ $vars.NAME }}`).
**Fails when.** An expression fails to evaluate. Blank `name`/`expression` entries are skipped. No input is not an error.

### Execute Pipeline — `execute_pipeline`
**Purpose.** Run another saved pipeline as a sub-workflow, optionally passing parameters. ADF Execute Pipeline / n8n Execute Workflow.
**Design.** No `pipeline_id` → pass-through. Fetches the child from `ctx.app_state["store"]`, parses via `Workflow.from_dict`, merges `parameters` over `metadata.parameters`, runs the child on an isolated DuckDB connection via a new `WorkflowExecutor(...).execute_workflow(wf, preview_limit=0, full_run=…)`. On child error honors `on_failure`. On success, returns the child's last-step preview rebuilt as a relation; else passes the parent input through.
**Inputs.** Exactly one (used for pass-through fallback). Input rows are NOT injected into the child — only `parameters`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `pipeline_id` | workflow_picker | Yes | `""` | Saved workflow to run as a sub-pipeline. |
| `wait_for_completion` | boolean | No | `True` | Read but unused — execution is always synchronous. |
| `on_failure` | select (`fail`,`skip`,`continue`) | No | `fail` | Raise/stop vs log and continue with parent input. |
| `parameters` | key_value_map | No | `{}` | Merged into the child's `metadata.parameters` (child reads as `${param.NAME}`). |

**Output.** Child's last-step rows (if any) or the parent input (pass-through). The child runs on a separate connection/`ctx.vars`.
**Variables & parameters.** `pipeline_id` + `parameters` values resolve `${param.x}` / `{{ }}`; injected `parameters` become `${param.NAME}` in the child. Does not read/write parent `$vars`.
**Fails when.** No input; pipeline not found; parse failure; child error with `on_failure="fail"`.

### For Each (Run Pipeline) — `foreach_pipeline`
**Purpose.** A true per-item loop: run a saved sub-pipeline ONCE PER input row, injecting that row's columns as parameters. ADF ForEach / n8n Loop Over Items. Distinct from `foreach_loop` (batching).
**Design.** Validates `pipeline_id`, `on_item_error`, `max_iterations`, `item_param`, static `parameters`. Materializes all rows; refuses inputs larger than `max_iterations`. Per row builds `overrides = {**static, **row, item_param: row}` and calls `_run_subpipeline`, which filters overrides to the child's DECLARED params and runs the child with `parameter_values=filtered`. Per-item errors raise (`fail`) or count-and-continue. Sequential.
**Inputs.** Exactly one — the rows to iterate; the relation passes through.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `pipeline_id` | workflow_picker | Yes | `""` | Sub-pipeline run once per row. |
| `item_param` | text | No | `item` | Name under which the whole row is passed as one JSON parameter. |
| `max_iterations` | number | No | `100` | Safety cap; refuses inputs exceeding it. |
| `on_item_error` | select (`fail`,`continue`) | No | `fail` | Stop on first failed item vs skip and finish. |
| `parameters` | key_value_map | No | `{}` | Fixed params for every iteration; per-row columns override these. |

**Output.** The input relation unchanged (child outputs not collected).
**Variables & parameters.** `pipeline_id`/`item_param`/static `parameters` resolve `${param.x}` / `{{ }}`; each iteration writes row+static into the CHILD's `parameter_values`. Does not read/write parent `$vars`.
**Fails when.** No input; missing `pipeline_id`; invalid `on_item_error`; non-numeric or `<=0` `max_iterations`; row count exceeding `max_iterations`; store/pipeline unavailable; a failing item with `on_item_error="fail"`.

### Lookup (Activity) — `lookup_activity`
**Purpose.** The orchestration-layer Lookup (ADF "Lookup activity"): read reference row(s) into a named variable for control flow (watermarks, config lookups, row-count gates). Distinct from the Combine **Lookup** transformation. *(Note: an ADF-exact connection+query mode is planned — today it reads its wired upstream relation.)*
**Design.** Requires one input. Builds `SELECT * FROM {view} [WHERE {filter}] [ORDER BY {order_by}] LIMIT {1 if first_row_only else max_rows}`, materializes into a per-step temp table (stable, preserves types). Writes `ctx.vars[output_var] = {firstRow, rows, count, isEmpty}` and also returns the rows.
**Inputs.** Exactly one — the reference dataset.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `output_var` | text | Yes | `lookup_result` | Captured as `$vars.<name>` (`.firstRow`, `.rows`, `.count`, `.isEmpty`). |
| `first_row_only` | boolean | No | `True` | Capture a single row (`LIMIT 1`) vs up to `max_rows`. |
| `order_by` | text | No | `""` | Which row wins for first-row-only (e.g. `updated_at DESC`). |
| `filter` | expression | No | `""` | Optional WHERE over the reference data. |
| `max_rows` | number | No | `5000` | Cap on rows captured into `.rows`. |
| `on_empty` | select (`fail`,`empty`) | No | `fail` | Stop on 0 rows vs continue with `firstRow={}`. |

**Output.** Returns the looked-up rows; **writes** `$vars.<output_var>` = `{firstRow, rows, count, isEmpty}`.
**Variables & parameters.** `filter`/`order_by`/`output_var` resolve `${param.x}` / `{{ }}`. **Writes** the structured `$vars.<output_var>`.
**Fails when.** No input; non-numeric or `<=0` `max_rows`; invalid `on_empty`; `count==0` with `on_empty="fail"`; invalid WHERE/ORDER BY.

### Fail — `fail`
**Purpose.** Explicitly abort the pipeline with a custom message + error code, optionally only when a SQL condition matches. ADF Fail / n8n Stop and Error.
**Design.** Reads `message`, `error_code` (default `USER_FAIL`), optional `condition`. With a `condition` + input, runs `SELECT COUNT(*) … WHERE {condition}`; 0 → returns input unchanged. Otherwise renders `{col}` placeholders in `message` from the first row and raises `RuntimeError(f"[{code}] {message}")`.
**Inputs.** Zero or one (for the gating condition + `{col}` rendering).
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `message` | text | Yes | `Pipeline failed by Fail activity` | Failure message; supports `{column}` from the first row. |
| `error_code` | text | No | `USER_FAIL` | Code prefixed as `[{code}]`. |
| `condition` | expression | No | `""` | Fail only if some upstream rows match (else pass through). |

**Output.** Raises `RuntimeError` on the fail path; returns the input unchanged when the condition matches no rows.
**Variables & parameters.** `message`/`condition` resolve `${param.x}` / `{{ }}`; `message` also supports `{column}` row-substitution. Reads/writes no `$vars`.
**Fails when.** By design — unconditionally, or when `condition` matches ≥1 row. Invalid condition SQL also raises.

### Retry / Error — `retry_handler`
**Purpose.** Makes retry/backoff for an upstream node a visible canvas node. n8n retry-on-fail / try-catch-with-retry.
**Design.** A CONFIG node — `execute()` is pass-through. The executor's `_find_retry_targets()` detects `retry_handler` nodes downstream of others and wraps the upstream node's execution with these params. `execute()` returns the upstream result when present, else a tiny diagnostic relation.
**Inputs.** Zero or one (pass-through).
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `max_retries` | number | No | `3` | Attempts before giving up. |
| `delay_seconds` | number | No | `2` | Wait before the first retry. |
| `backoff_multiplier` | number | No | `2.0` | Factor applied to the delay after each retry. |
| `on_exhausted` | select (`fail`,`skip`) | No | `fail` | Stop the pipeline vs continue with an empty result. |

**Output.** Pass-through of the upstream relation, or a 1-row diagnostic. The retry effect is delivered by the executor.
**Variables & parameters.** Numeric/select config; no expression fields. Reads/writes no `$vars`.
**Fails when.** `execute()` does not raise; stop-on-exhausted behavior is governed by the executor per `on_exhausted`.

---

## Action

These nodes perform side effects (HTTP, email/Slack, file ops, SQL, deletes) or return metadata. `$vars` is not written by any of them.

### HTTP Request — `http_request`
**Purpose.** Call a web API and capture the JSON response, as one batch request or one request per input row. ADF Web/Web Hook / n8n HTTP Request.
**Design.** Batch mode ignores input and calls once; per-row mode materializes the input to a DataFrame and substitutes `{column}` placeholders into URL/body per row. `_do_request` builds a `urllib` request (default `Accept: application/json`, `User-Agent: F-Pulse/0.6.0`; JSON body → `Content-Type: application/json`). **Before opening any socket** it runs the SSRF guard `check_url(url, allow_private_env="FPULSE_HTTP_ALLOW_PRIVATE")` (blocks loopback/private/link-local/metadata unless allowed). Response parsing: a JSON list → rows; a dict unwrapped on `data/results/items/records/rows/values`, else one row; scalars → `{"response": …}`. Timeout: per-step `timeout` > 0 else `FPULSE_HTTP_DEFAULT_TIMEOUT` else 30s. Per-row capped at `FPULSE_HTTP_PER_ROW_MAX` (default 1000).
**Inputs.** 0 in batch mode (valid no-input entry node); exactly 1 in per-row mode.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| url | text | Yes | "" | Target URL; `{column}` placeholders in per-row mode. |
| method | select (GET/POST/PUT/PATCH/DELETE) | No | GET | HTTP method. |
| headers | key_value | No | {} | Extra headers, merged over defaults. |
| body | code | No | "" | Request body (JSON); `{column}` in per-row mode. |
| per_row | boolean | No | false | One request per input row. |
| timeout | number | No | 0 | Per-request timeout; 0 = default. |

**Output.** Batch: response rows. Per-row: each input row merged with the first response row; empty → `{**row, "_http_status":"empty"}`; per-row exceptions captured as `{**row, "_http_error":…}` (do not abort). No rows → input unchanged.
**Variables & parameters.** `url`/`body`/`headers`/`method`/`timeout` resolve `${param.x}` / `{{ }}`; `url`/`body` also support `{column}` in per-row mode. Does not read/write `$vars`.
**Fails when.** `url` empty; SSRF block; per-row input exceeds the cap; batch HTTP error / unreachable host.

### Code / Script — `code_script`
**Purpose.** Run user Python over the input as a pandas DataFrame. ADF custom transform / n8n Code node.
**Design.** Reads `code` + `timeout` (max 300s). Kill-switch `FPULSE_DISABLE_CODE_SCRIPT`. Security layering: (1) string-match `_BLOCKED` list (e.g. `import os/sys/subprocess/socket`, `__import__`, `eval(`, `exec(`, `open(`, dunder access); (2) AST import allowlist `_ALLOWED_IMPORTS` (`re, json, math, statistics, datetime, decimal, itertools, functools, collections, csv, io, pandas, numpy, duckdb`). Input → DataFrame `df`; restricted globals (`_SAFE_BUILTINS`, `__builtins__` swapped, no `__import__`/`open`); pre-binds `df`, `pd`, `np`, `json`, `math`; runs `exec()` on a **daemon thread** with a wall-clock timeout. **NOT a sandbox** — runs in-process; `pd`/`np` still expose host I/O; a timed-out script keeps running on an un-killed daemon thread. Treat as trusted code.
**Inputs.** Exactly 1, as `df`.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| code | code (python) | Yes | starter template | Python executed with `df` in scope; reassign result to `df`. Helpers: `pd`, `np`, `json`, `math`. |

**Output.** The resulting `df` (defaults to the original if not reassigned).
**Variables & parameters.** `code` resolves `${param.x}` / `{{ }}` before the AST/blocklist checks see it. Does not read/write `$vars`.
**Fails when.** Disabled via env; a blocked pattern; an import outside the allowlist; syntax error; timeout; or the user code raises.

### Send Email — `send_email`
**Purpose.** Send email over SMTP (plain/HTML, CC/BCC, optional per-row), via a connection or inline credentials. ADF email step / n8n Send Email.
**Design.** In preview/dry-run it logs "would send N email(s)" and returns input. Else resolves SMTP via `_resolve_smtp` (connection `config` or inline `smtp_*`/`from`/`security`). Per-row renders `subject`/`body`/`to` with `{column}`; single mode renders from the first row. `_send_one` builds `MIMEMultipart("alternative")` with a `MIMEText` of `html`/`plain`; **BCC is omitted from headers but included in the recipient list**. Transport: `SMTP_SSL` (ssl) or `SMTP` + `starttls()` (tls), 15s timeout. **No SMTP host configured → the email is logged, not sent**; SMTP exceptions log then re-raise.
**Inputs.** Exactly 1.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| connection_id | connection_picker | No | "" | SMTP connection; empty → inline fields. |
| to | text | Yes | "" | Recipients (comma-separated); `{column}` per-row. |
| cc | text | No | "" | CC recipients. |
| bcc | text | No | "" | BCC (envelope only, not headers). |
| subject | text | No | "F-Pulse Notification" | Subject; `{column}`. |
| body_type | select (plain/html) | No | plain | Body MIME subtype. |
| body | code | No | "Pipeline completed successfully." | Body; `{column}`; HTML when body_type=html. |
| per_row | boolean | No | false | One email per input row. |
| on_error | select (fail/continue) | No | fail | Abort vs log and continue. |
| security | select (tls/ssl/none) | No | tls | Transport security (inline). |
| smtp_host | text | No | "" | Inline SMTP host. |
| smtp_port | number | No | 587 | Inline SMTP port. |
| smtp_user | text | No | "" | Inline SMTP username. |
| smtp_pass | password | No | "" | Inline SMTP password. |
| from | text | No | "" | From (falls back to user, then `fpulse@localhost`). |

**Output.** No data output — pass-through side effect.
**Variables & parameters.** All string fields resolve `${param.x}` / `{{ }}`; `to`/`subject`/`body` also support `{column}`. Does not read/write `$vars`.
**Fails when.** Send fails and `on_error=fail`. With `continue`, failures are logged and the node succeeds.

### Slack / Teams — `slack_notify`
**Purpose.** Post a message to Slack/Teams via an Incoming Webhook. ADF Web activity to a webhook / n8n Slack.
**Design.** Renders `{column}` in the message from the **first input row only**, POSTs JSON `{"text": message}` (adds `channel` when set) via `urllib` (10s timeout). A failed POST is caught and logged (does not raise). No webhook → logged only. **Does NOT route through the SSRF guard** (unlike HTTP Request).
**Inputs.** Exactly 1; only the first row is used. A single message is posted (not per-row).
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| webhook_url | text | Yes | "" | Slack Incoming Webhook URL. |
| message | code | No | "F-Pulse pipeline completed." | Message; `{column}` from the first row. |
| channel | text | No | "" | Override the default channel. |

**Output.** No data output — pass-through.
**Variables & parameters.** `webhook_url`/`message`/`channel` resolve `${param.x}` / `{{ }}`; `message` also supports `{column}`. Does not read/write `$vars`.
**Fails when.** Never raises on delivery failure (errors are caught and logged).

### Delete Data — `delete_data`
**Purpose.** Remove rows matching a condition (inverse of Filter); a `files` mode is advertised but intentionally not implemented. ADF Delete activity (files) / a delete in a data flow.
**Design.** `target_kind=files` raises immediately (not implemented — fail loud over silent no-op). `rows` mode: empty `condition` → input unchanged; else `SELECT * FROM __delete_input WHERE NOT (<condition>)` (rows where the condition is TRUE are removed). Condition interpolated directly (no binding).
**Inputs.** Exactly 1 (rows mode).
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| target_kind | select (rows/files) | Yes | rows | rows: filter the relation. files: raises (not implemented). |
| condition | expression | No | "id IS NULL" | Predicate; matching rows are REMOVED (rows mode). |
| target_path | text | No | "" | files mode — unused. |
| wildcard | text | No | "" | files mode — unused. |
| recursive | boolean | No | false | files mode — unused. |
| max_concurrent | number | No | 1 | Settings; not used in rows mode. |
| enable_logging | boolean | No | false | Logging; not used in rows mode. |
| log_path | text | No | "" | Logging; not used in rows mode. |

**Output.** Surviving rows (input minus matched). With no condition, the input unchanged.
**Variables & parameters.** `condition` resolves `${param.x}` / `{{ }}`. Does not read/write `$vars`.
**Fails when.** `target_kind=files`; invalid `condition` SQL.

### Get Metadata — `get_metadata`
**Purpose.** Return schema + basic statistics about the input instead of the data. ADF Get Metadata / a column-profile step.
**Design.** Counts rows via `COUNT(*)`, reads `columns`/`types`, and per column runs an aggregate computing null count, distinct count, MIN/MAX (cast to VARCHAR; unsupported types tolerated). Builds one row per column with `column_name, column_type, nullable, null_count, distinct_count, min_value, max_value, total_rows`. (The `dataset_kind`, `fields`, `include_row_count`, `include_size` params are present in the schema but unused — see appendix.)
**Inputs.** Exactly 1.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| dataset_kind | select (upstream/file/directory) | No | upstream | Intended source; execute only profiles the upstream relation. |
| dataset_path | text | No | "" | Path for file/directory mode (unused). |
| fields | multi_select | No | (the 7 fields above) | Intended subset; execute emits the fixed set. |
| include_row_count | boolean | No | true | execute always includes `total_rows`. |
| include_size | boolean | No | false | Byte size not computed. |

**Output.** One row per input column (the fields above) + `total_rows`; an empty typed relation when there are no columns.
**Variables & parameters.** String params resolve `${param.x}` / `{{ }}`. Does not read/write `$vars`.
**Fails when.** No explicit raise (per-column stat failures are swallowed); fails only if the initial `COUNT(*)`/registration errors.

### File System — `file_system`
**Purpose.** Local filesystem operations — copy/move/rename/delete/create — on files and directories on the runtime host. ADF Copy/Delete against a file system / n8n Read/Write/Move Files.
**Design.** `operation` must be in `_OPS`. Mapping: `copy_file` → `FileExistsError` if dest exists and overwrite off, else `shutil.copy2`; `move_file`/`move_directory` → `shutil.move`; `rename_file` → `os.replace`; `delete_file` → `Path.unlink(missing_ok=True)`; `copy_directory` → `shutil.copytree(dirs_exist_ok=overwrite)`; `delete_directory` → `shutil.rmtree(ignore_errors=not recursive)`; `create_directory` → `mkdir(parents=True, exist_ok=True)`. Builds a one-row result; on error records `status="error"` and re-raises unless `continue_on_error`. Local FS only (no path sandboxing).
**Inputs.** Takes no input data — acts on configured paths.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| operation | select (copy_file/move_file/rename_file/delete_file/copy_directory/move_directory/delete_directory/create_directory) | Yes | copy_file | The operation. |
| source | text | Yes | "" | Source path (also the target for delete/create). |
| destination | text | No | "" | Destination; required for copy/move/rename. |
| overwrite | boolean | No | false | Allow overwriting an existing destination. |
| recursive | boolean | No | true | For delete_directory, controls `ignore_errors`. |
| continue_on_error | boolean | No | false | Record the error and do not raise. |

**Output.** A single result row: `operation, source, destination, status, error`.
**Variables & parameters.** `source`/`destination` resolve `${param.x}` / `{{ }}`. Does not read/write `$vars`.
**Fails when.** Unknown operation; `copy_file` with an existing destination and overwrite off; any underlying FS error (re-raised unless `continue_on_error`).

### Execute SQL — `execute_sql_task`
**Purpose.** Run an arbitrary SQL statement (DDL/DML/blocks) against a real DB connection or the in-memory DuckDB engine. ADF Stored Procedure/Script / n8n DB execute.
**Design.** Empty SQL raises. Optionally renders `{column}` from the **first upstream row**, escaping single quotes (`'`→`''`) — this is string substitution, not parameter binding (review SQL using `{col}` for identifiers). No connection → in-memory DuckDB (`full` → `ctx.conn.sql(sql)`; else `{"affected":-1,"status":"ok"}`). With a connection (Postgres/psycopg2, MySQL/pymysql, SQLite/sqlite3), `full` returns the result set; else `{"affected": rowcount, "status":"ok"}`.
**Inputs.** 0 or 1 (the first row fills `{column}` placeholders).
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| connection_id | connection | No | "" | Target connection; empty = in-memory DuckDB. |
| sql | code (sql) | Yes | "" | Statement; `{column}` placeholders from the first row (single-quote-escaped). |
| return_mode | select (rowcount/full) | No | rowcount | Affected-row summary vs result set. |
| timeout | number | No | 60 | Connection/statement timeout. |

**Output.** `full` → the result set (or empty); `rowcount` → a one-row summary.
**Variables & parameters.** `sql`/`connection_id` resolve `${param.x}` / `{{ }}`; `sql` also supports `{column}`. Does not read/write `$vars`.
**Fails when.** Empty `sql`; DuckDB execution error; connection not found; unsupported connection kind; any driver/DB error.

---

## AI / Semantic

### Embedder — `embedder`
**Purpose.** Convert a text column into a fixed-dimension vector column for similarity (Semantic Router, vector search). LangChain `Embeddings` / n8n Embeddings sub-node.
**Design.** Materializes the input to a DataFrame, converts the column to strings (`None`→`""`), and embeds in slices of `batch_size` via `_embed_batch(provider, model, texts, dim)`. Provider dispatch: `openai` (`text-embedding-3-small`), `cohere` (`embed-english-v3.0`, `input_type="search_document"`), `sentence_transformers` (`all-MiniLM-L6-v2`, normalized), else `hash`. Every real-provider branch is wrapped in `try/except` and **silently falls back to `_hash_embed`** (deterministic SHA-256 → `dim` floats, L2-normalized) on any failure. `dim` is honored only by the hash fallback; real providers return native dimensionality.
**Inputs.** Exactly one; the whole text column, vectors appended in row order.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `text_column` | column | Yes | `""` | Column whose values are embedded. |
| `provider` | select (`hash`,`openai`,`cohere`,`sentence_transformers`) | No | `hash` | Backend; failures fall back to hash. |
| `model` | text | No | `""` | Provider model; empty uses each default. |
| `dim` | number | No | `384` | Vector dimension — hash fallback only. |
| `output_column` | text | No | `embedding` | New vector column name. |
| `batch_size` | number | No | `64` | Texts per provider call. |

(`dim`/`output_column`/`batch_size` are in `default_params()`/`execute()` but NOT in `param_schema()` — see appendix.)
**Output.** All rows pass through + a list-of-float column (`output_column`, default `embedding`).
**Variables & parameters.** String fields resolve `${param.x}` / `{{ }}`. API keys come from env (`OPENAI_API_KEY`, `COHERE_API_KEY`). Does not read/write `$vars`.
**Fails when.** No input; empty `text_column`; `text_column` absent upstream. Provider failures do NOT raise (hash fallback).

### LLM Guardrail — `llm_guardrail`
**Purpose.** Deterministic regex-based screening of a text column for PII, prompt-injection, and profanity, then tag / mask / drop offending rows. Rule-based (no LLM call); like a moderation guardrail.
**Design.** Normalizes `checks` (default `["pii","prompt_injection"]`) and `extra_patterns`. `_scan_text`: `pii` → five `_PII_PATTERNS` (`email/phone/ssn/credit_card/ip_address`) tagged `pii:<name>`; `prompt_injection` → six patterns tagged `prompt_injection`; `profanity` → word-set substring; each `extra_patterns` regex (invalid ones skipped) tagged `custom:<…>`. `mode`: `tag` records flags; `mask` replaces PII matches with `***` and overwrites `text_col`; `block` drops flagged rows.
**Inputs.** Exactly one; the column scanned row-by-row.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `text_column` | column | Yes | `""` | Column to scan. |
| `checks` | multi_select (`pii`,`prompt_injection`,`profanity`) | No | `["pii","prompt_injection"]` | Detector families to run. |
| `mode` | select (`tag`,`block`,`mask`) | No | `tag` | Annotate / drop / redact PII. |
| `extra_patterns` | string_list | No | `[]` | Extra case-insensitive regexes; matches tagged `custom:<…>`. |

(`profanity_words` is read by `execute()` to override the default set but is NOT in the schema — see appendix.)
**Output.** Always adds `__guardrail_flags` (comma-joined hits, empty when clean). `mask` rewrites `text_column`; `block` removes flagged rows; `tag` keeps all.
**Variables & parameters.** String fields resolve `${param.x}` / `{{ }}`. Does not read/write `$vars`.
**Fails when.** No input; empty `text_column`; `text_column` absent upstream. Detection never raises (bad custom regexes skipped).

### Semantic Router — `semantic_router`
**Purpose.** Classify each row into one of N labels by cosine-comparing the row's embedding against per-label prototype embeddings built from example texts. Zero-shot/`semantic-router`-style classifier / LangChain `RouterChain` (no runtime LLM call).
**Design.** Per label, concatenates `examples` and embeds once (same `_embed_batch` + hash fallback as Embedder) → `(name, vector)`. Embeds all row texts in one call, picks the prototype with the highest `_cosine` (dot product over L2-normalized vectors); below `threshold` → `default_label`. Scores rounded to 4 dp.
**Inputs.** Exactly one; the column embedded + classified row-by-row.
**Input fields.**

| Field | Type | Required | Default | What it does |
|---|---|---|---|---|
| `text_column` | column | Yes | `""` | Column classified. |
| `labels` | label_list | Yes | `[]` | `{name, examples}`; examples become each label's prototype. |
| `provider` | select (`hash`,`openai`,`cohere`,`sentence_transformers`) | No | `hash` | Embedding backend (failures fall back to hash). |
| `threshold` | number | No | `0.0` | Min cosine; below → `default_label`. |
| `default_label` | text | No | `other` | Label when no prototype clears the threshold. |

(`model`/`dim`/`output_column` are in `default_params()`/`execute()` but NOT in `param_schema()` — see appendix.)
**Output.** All rows pass through + `output_column` (default `__route`) with the chosen label + `<output_column>_score` (default `__route_score`).
**Variables & parameters.** String fields resolve `${param.x}` / `{{ }}`. API keys from env when a real provider is used. Does not read/write `$vars`.
**Fails when.** No input; empty `text_column`; empty `labels`; zero usable prototypes (every label missing a name/examples); `text_column` absent upstream. Provider failures fall back to hash.

---

## Appendix — known implementation gaps & caveats

Surfaced while documenting from code, then triaged 2026-06-15 (each claim re-verified against the code — several were stale). ✅ = fixed, ⚠️ = open, ❌ = false alarm (no issue).

**Schema/UI exposure gaps** (param read by `execute()` but missing from `param_schema()`):
- ✅ **Data Profile** — WAS double-defined (correcting the earlier triage): a stale 2-field `default_params`/`param_schema` pair appeared *after* the real 4-field pair in the same class, so Python's later-binding rule shadowed it — hiding `include_columns`/`exclude_columns` from the UI. Fixed 2026-06-15 (C2): removed the stale pair (now exposes all four) and added `passthrough_data` for dual-output.
- ❌ **Embedder** — already exposes `dim`/`output_column`/`batch_size`/`model`. No issue.
- ✅ **Semantic Router** — `model`/`dim`/`output_column` added to the schema.
- ✅ **LLM Guardrail** — `profanity_words` added to the schema.

**Security-relevant:**
- ✅ **Slack / Teams** — now runs the SSRF `check_url` guard (raises on loopback/private/metadata hosts), matching HTTP Request.
- ✅ **Switch / Conditional Split (+ retired `switch_case`)** — user-supplied labels/case values are now SQL-escaped (`'`→`''`, identifiers `"`→`""`). (Conditions stay raw SQL by design — author-only, never row data.)
- ⚠️ **Code / Script** is explicitly **not a sandbox** — runs in-process via `exec`; `pd`/`np` still expose host I/O; a timed-out script keeps running on an un-killed daemon thread. Disable with `FPULSE_DISABLE_CODE_SCRIPT=1`. (Open — real sandbox is a separate Plus item.)
- ⚠️ **Execute SQL** uses single-quote-escaped `{column}` substitution, not true parameter binding (documented caveat; author-only SQL).

**Declared-but-unused params:**
- ✅ **Execute Pipeline** — `wait_for_completion` removed (it always runs synchronously).
- ✅ **Get Metadata** — schema trimmed to `[]`; the file/directory + field/size toggles `execute()` never read are gone (it profiles the upstream relation, fixed output).
- ✅ **ForEach / Batch Rows** — the fake `mode=parallel` removed (always sequential).
- ⚠️ **Switch (`conditional_split`)** — docstring still mentions an `all_match` mode that isn't implemented (docstring-only; not in schema).
- ⚠️ **Retry** — docstring lists a `last_good` `on_exhausted` option not exposed in the schema (docstring-only).

**Naming / ADF alignment:**
- ✅ Control-flow names aligned to ADF (If Condition True/False, Switch, ForEach, Execute Pipeline, Lookup, Wait, Retry) and the two ForEach variants collapsed (ForEach + Batch Rows). See the banner at the top.
- ✅ **Lookup ("Lookup", the ADF Lookup activity)** — now self-contained: set `source_mode=connection` + a `connection_id` + `query` and it fetches its own reference data (no upstream wiring), exactly like ADF. Without a connection it still reads the wired upstream (back-compat). Driver matrix shared with Execute SQL via `_run_connection_sql`. Output gained an ADF `value` alias (= `rows`).
