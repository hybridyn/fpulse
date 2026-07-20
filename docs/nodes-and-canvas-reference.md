# F-Pulse OSS — Nodes & Canvas Reference

**Status:** Synthesized 2026-05-18 from backend node registry + frontend Canvas/ConfigPanel sources.
**Scope:** Every node currently wired into the OSS registry (44+), plus the canvas architecture.

This is a structural reference, not a tutorial. Each node entry covers:
- **StepType** — the canonical identifier used by the store/IR/registry
- **Category + Arity** — palette section + how many upstream inputs it accepts
- **What it does** — derived from the actual `execute()` method
- **Fields** — every parameter, sourced from `param_schema` / hardcoded ConfigPanel

The canvas section covers the editor architecture: ReactFlow integration, the workflow store, the four execution paths, validation, schema propagation.

---

## Quick reference — all node types

| Category | StepType | Display name | Arity | One-line |
|---|---|---|---|---|
| **Data Movement / Source** | `csv_source` | CSV Source | 0 | DuckDB `read_csv` of a local file |
| | `json_source` | JSON Source | 0 | DuckDB `read_json`; supports records/lines/auto |
| | `parquet_source` | Parquet Source | 0 | DuckDB `read_parquet`, glob-friendly |
| | `excel_source` | Excel Source | 0 | openpyxl with sheet/header/skip-row control |
| | `xml_source` | XML Source | 0 | Element-tag → rows with attributes as columns |
| | `db_source` | Database Source | 0 | SQL or table-pick against saved DB connection |
| | `api_source` | API Source | 0 | REST GET with pagination + retries |
| | `s3_source` | S3 / MinIO Source | 0 | boto3 download → format-aware read |
| | `kafka_source` | Kafka Source | 0 | Consume N messages from topic |
| | `ftp_source` | FTP Source | 0 | FTP/FTPS download → format-aware read |
| | `gsheet_source` | Google Sheets Source | 0 | CSV export URL of a public sheet |
| | `delta_source` | Delta Lake Source | 0 | Time-travel-capable Delta read |
| | `webhook_trigger` | Webhook Trigger | 0 | HTTP entry point |
| | `http_request` | HTTP Request | 1 | Outbound call with response capture |
| **Data Movement / Sink** | `csv_sink` | CSV Sink | 1 | DuckDB `COPY ... TO CSV` |
| | `json_sink` | JSON Sink | 1 | array or newline-delimited |
| | `excel_sink` | Excel Sink | 1 | openpyxl with header freeze + auto-width |
| | `parquet_sink` | Parquet Sink | 1 | DuckDB `COPY ... TO PARQUET` |
| | `db_sink` | Database Sink | 1 | INSERT / overwrite / upsert |
| | `s3_sink` | S3 / MinIO Sink | 1 | Stage local → upload via boto3 |
| | `kafka_sink` | Kafka Sink | 1 | One message per row, batched |
| | `api_sink` | API Sink | 1 | POST/PUT/PATCH bulk or per-row |
| | `email_sink` | Email Sink | 1 | SMTP with attached file |
| | `delta_sink` | Delta Lake Sink | 1 | Append/overwrite Delta table |
| | `warehouse_sink` | Warehouse Sink | 1 | Snowflake/BigQuery/Redshift |
| | `file_sink` | File Sink | 1 | Auto-detect from extension |
| **Transform** | `filter` | Filter | 1 | WHERE clause (rules or expression) |
| | `transform` | Transform | 1 | Arbitrary SELECT against `source_table` |
| | `aggregate` | Aggregate | 1 | GROUP BY + N aggregates |
| | `deduplicate` | Deduplicate | 1 | Drop duplicates by key |
| | `flatten_explode` | Flatten / Explode | 1 | Expand struct or unnest array |
| | `materialize` | Materialize | 1 | Cache intermediate to temp table |
| | `sort` | Sort | 1 | ORDER BY one or more columns |
| | `rename` | Rename Columns | 1 | Column-name remap |
| | `typecast` | Type Cast | 1 | Per-column CAST |
| | `derived_column` | Derived Column | 1 | SELECT *, expr AS name |
| | `pivot` | Pivot | 1 | DuckDB PIVOT |
| | `unpivot` | Unpivot | 1 | DuckDB UNPIVOT |
| | `window` | Window | 1 | PARTITION BY + window functions |
| | `sample` | Sample | 1 | LIMIT or USING SAMPLE |
| | `validate` | Validate | 1 | Adds `is_valid` boolean |
| | `conditional_split` | Conditional Split | 1 | Route rows via `_split_output` label |
| | `data_quality` | Data Quality | 1 | drop/fail/tag/split on rule violations |
| | `upsert` | Upsert | 1 | Idempotent merge by key |
| | `schema_mapper` | Schema Mapper | 1 | Map + cast for manifest-driven sinks |
| | `data_wrangler` | Data Wrangler | 1 | 6-op chain (filter/select/rename/cast/derive/group_by) in one tile |
| | `retry_handler` | Retry / Error | 1 | Wraps upstream with retry/backoff |
| **Combine** (multi-input) | `join` | Join | 2 | INNER/LEFT/RIGHT/FULL/SEMI/ANTI/CROSS |
| | `lookup` | Lookup | 2 | LEFT JOIN enrichment |
| | `union` | Union | N≥2 | UNION ALL or UNION DISTINCT |
| | `scd2` | Slowly-Changing Dimension Type 2 | 1–2 | Versioned history with effective/expiration dates |
| **Control Flow** | `if_condition` | If Condition | 1 | Pass through rows where condition TRUE |
| | `switch_case` | Switch Case | 1 | Route by column value matching cases |
| | `foreach_loop` | ForEach Loop | 1 | Batch input and process per batch |
| | `until_loop` | Until Loop | 1 | Repeat until condition true (max 1000 iters) |
| | `wait_delay` | Wait / Delay | 1 | Sleep N seconds, pass through |
| | `set_variable` | Set Variable | 1 | Add columns = pipeline vars |
| | `execute_pipeline` | Execute Pipeline | 1 | Call another saved workflow |
| | `append_variable` | Append Variable | 0–1 | Push to array variable on ctx.vars |
| | `filter_array` | Filter Array | 0–1 | Predicate filter on array variable |
| | `validation` | Wait for File | 0–1 | Poll path until exists / non-empty |
| | `fail` | Fail | 0–1 | Stop pipeline with custom error |
| **Action** | `code_script` | Code Script | 1 | Sandboxed Python on pandas DataFrame |
| | `send_email` | Send Email | 1 | SMTP with HTML / per-row / CC/BCC |
| | `slack_notify` | Slack Notify | 1 | Webhook message with `{column}` placeholders |
| | `copy_data` | Copy Data | 0–1 | DB→DB or upstream→DB with mapping + staging |
| | `delete_data` | Delete Data | 1 | Drop rows OR delete files |
| | `get_metadata` | Get Metadata | 1 | Per-column stats (null %, distinct, min/max) |
| | `file_system` | File System | 0–1 | Local copy/move/rename/delete |
| | `execute_sql_task` | Execute SQL | 0–1 | DDL/DML against a DB connection |
| **AI / Semantic** | `embedder` | Embedder | 1 | Text → vector (hash/openai/cohere/sentence_transformers) |
| | `llm_guardrail` | LLM Guardrail | 1 | PII / prompt-injection / profanity detection |
| | `semantic_router` | Semantic Router | 1 | Classify into N labels via cosine similarity |

Arity is enforced at edge-drop time by `frontend/src/utils/nodeArity.ts`. MULTI_INPUT_NODES = `{join, union, scd2}`; everything else is single-input by default; sources/triggers are zero-input.

---

# Part 1 — Canvas architecture

## 1.1 Tech stack

- **ReactFlow** (`@xyflow/react` v12+) — node/edge rendering, pan/zoom, selection, connection validation, smooth-step edges, minimap, viewport persistence
- **Zustand** — single store at `frontend/src/stores/workflowStore.ts`; canvas nodes, edges, execution state, schema cache all live here
- **React 18** — concurrent rendering; portals used for context menus, dialogs, and the ConfigPanel modal
- **DuckDB** (backend) — every transform's `execute()` returns a `DuckDBPyRelation`; the engine fuses subqueries across the DAG

ReactFlow config in `Canvas.tsx`:
- `fitViewOptions: { padding: 0.15, minZoom: 0.25, maxZoom: 1.5, duration: 300 }`
- `defaultEdgeOptions` — animated, indigo (`#6366f1`), `strokeWidth: 2`, arrow marker
- `snapToGrid: [20, 20]` (toggleable via Settings → General)
- `selectionMode: Partial` (drag-select captures any node the box touches)

## 1.2 Layout

```
┌──────────────────────────────────────────────────────────────────┐
│ Toolbar (78px)  Project | Pipeline Name | Save | Run | Publish   │
├────────┬──────────────────────────────────────────┬──────────────┤
│ Modules│  Canvas (flex-1)                          │ Copilot      │
│ Palette│   ReactFlow + minimap + footer toolbar    │ (chat panel) │
│ (CSS   │                                           │              │
│ var    │                                           │ CSS var      │
│ width) │                                           │ width        │
├────────┴──────────────────────────────────────────┴──────────────┤
│ PreviewPanel (240px) — INPUT | OUTPUT | SCHEMA | JSON tabs       │
└──────────────────────────────────────────────────────────────────┘
```

Panel widths are published as CSS vars (`--fpulse-nodes-panel-width`, `--fpulse-chat-panel-width`) so overlays (ConfigPanel backdrop) reflow with them. A `ResizeObserver` on the canvas wrapper recomputes `rfInstance.fitView()` debounced via `requestAnimationFrame`.

## 1.3 Node model — one renderer for all node types

Every canvas node renders through `frontend/src/components/nodes/FPulseNode.tsx`. The node receives `data` with:

- `stepType` — the logical kind (`filter`, `aggregate`, `data_wrangler`, ...)
- `label` — user-renamed name (defaults to display name)
- `params` — config object the node's `execute()` reads
- `color` — gradient fill from `NODE_COLORS` map (workflowStore)
- `category` — palette grouping only; not used for topology
- `status` — `pending` / `running` / `success` / `error` (driven by `useExecutionWS`)

Sub-components:
- `NodeIcon` resolves the SVG glyph + gradient per stepType (60+ mini-icons hardcoded; fallback is a generic rect with lines)
- Status badge: animated amber spinner while running; green check on success; red X on error
- Error pills: when `validationErrors[nodeId]` is populated, red truncated messages render beneath the header

Handles:
- Left target (`Position.Left`) — single input
- Right source (`Position.Right`) — single output, colored to match node gradient
- Top target (`id="input-2"`) — appears on multi-input nodes (join/union/scd2) for the second input

## 1.4 Edge model

Edges render via `CustomEdge.tsx` with ReactFlow's `SmoothStepPath` (borderRadius 16).

Each edge carries a `data.condition` cycling through:
- `completion` (indigo) — fire regardless of upstream status
- `success` (green) — only if upstream succeeded
- `failure` (red) — only if upstream errored

Click the condition label (rendered at edge midpoint via `EdgeLabelRenderer`) to cycle. Blocked edges (source deactivated or blocked by ancestor) drop to 55% opacity, animation disabled, label "Blocked".

When the source step has completed, the edge also shows a `"N rows"` badge above the condition label — live cardinality feedback.

A small delete button (X circle at midpoint) removes the edge.

## 1.5 Connection validation

`isValidConnection` fires on every pointer move during a drag and at drop. It calls `arityFor(targetStepType)` from `utils/nodeArity.ts`:

```ts
MULTI_INPUT_NODES = new Set(['join', 'union', 'scd2']);
NO_INPUT_NODES    = new Set(['source', 'csv_source', 'db_source', ...all sources]);
arityFor(t) => MULTI_INPUT_NODES.has(t) ? 'many'
             : NO_INPUT_NODES.has(t) ? 'none'
             : 'one';
```

- `'many'` — accept any number of inputs
- `'none'` — reject all inputs (source / trigger)
- `'one'` — reject if another edge already targets this node

When a drop is rejected, `onConnectEnd` reads `blockedReasonRef` and fires a toast explaining why.

`utils/validateWorkflow.ts` re-runs the same arity check at Save/Deploy/Run time for workflows loaded from JSON or scripted via the API.

## 1.6 Workflow store (Zustand)

Single source of truth at `stores/workflowStore.ts`. Key slices:

**Canvas**
- `nodes: Node[]`, `edges: Edge[]`, `onNodesChange`, `onEdgesChange`, `onConnect`

**Workflow metadata**
- `workflowId: string | null` (null = unsaved)
- `workflowName: string` ("Untitled Pipeline" placeholder)
- `projectId: string | null`, `status`, `version`, `parameters: WorkflowParameter[]`

**Execution + results**
- `stepResults: Record<string, StepResult>` — `{step_id, status, row_count, columns, sample_data, schema_info, error, duration_ms}` per step
- `isRunning`

**Schema cache**
- `stepSchemas: Record<string, StepSchemaResponse>` — column lists per step
- `schemaVersion: number` — monotonic; bumped on any IR change to invalidate the cache
- `bumpSchemaVersion()`, `setStepSchema()`, `clearStepSchemas()`

**Wrangler preview** (2026-05-17)
- `wranglerPreview: Record<string, WranglerPreviewEntry>` — per-wrangler-node live preview, populated by `DataWranglerConfig` and consumed by `PreviewPanel` for the Step ▾ selector
- `setWranglerPreview(nodeId, entry|null)`, `setWranglerSelectedStep(nodeId, index)`

**Validation + dirty**
- `validationErrors: Record<string, string[]>`
- `isDirty: boolean`

**Deactivation chain**
- `isNodeDeactivated(nodeId)` reads `node.data.params._settings.deactivated`
- `isNodeBlockedByUpstream(nodeId)` checks if any ancestor is deactivated

**Undo/redo**
- `undoStack`, `redoStack` — snapshots of `{nodes, edges}`
- `pushUndoState()` called before every mutation; `undo()`, `redo()`, `canUndo()`, `canRedo()`

## 1.7 "No silent pipeline create" rule

`ensureWorkflow(opts?: { allowCreate?: boolean })`:
- Default (`allowCreate: false`) — updates the existing row if `workflowId` is set; otherwise returns `null` (does NOT create)
- `allowCreate: true` — creates a new row only if `workflowName` is non-placeholder; defense-in-depth guard rejects "Untitled Pipeline" silently and returns null

The locked rule (2026-05-09): every caller that may create a workflow row goes through `requireNamedWorkflow()` first, which runs the name-prompt + dup-check loop. Four call sites today: Toolbar Save (`Ctrl+S`), SaveDialog, ConfigPanel Test Node, Canvas Sample button. Autosave never creates.

## 1.8 Execution paths

**Run All** (Toolbar) — full validation → POST `/api/workflows/{id}/run` → WebSocket streams `step_started` / `step_completed` / `step_error` / `workflow_completed` events into `stepStates`.

**Test Node** (ConfigPanel button) — `requireNamedWorkflow()` → `ensureWorkflow({allowCreate:true})` → `runStep(nodeId)` → POST `/api/workflows/{id}/steps/{stepId}/run`. Runs the node + the minimum upstream chain needed to feed it.

**Sample** (Canvas footer) — validation → `requireNamedWorkflow()` → `ensureWorkflow({allowCreate:true})` → `runWorkflow(false)` (fullRun=false → backend applies row caps for fast preview).

**Resume from here** (right-click context menu) — cache-aware rerun; reuses upstream `stepResults` if their params are unchanged.

WebSocket lifecycle: `useExecutionWS(workflowId)` opens `ws://host:8001/ws/execution/{wfId}?token=...&workspace_id=...`, parses incoming events, applies `.fpulse-executing` CSS class to running nodes, animates edges from completed → running steps.

## 1.9 PreviewPanel (bottom 240px)

Four tabs:
- **INPUT** — selected node's upstream result. Multi-input nodes (join/union/scd2) get a dropdown to pick which ancestor to inspect.
- **OUTPUT** — selected node's `sample_data` as a scrollable table.
- **SCHEMA** — column list with types and nullability from `schema_info`.
- **JSON** — raw `StepResult` dump (timing, error details).

**Wrangler Step ▾ selector**: when the selected node is `data_wrangler` AND `wranglerPreview[nodeId]` is set, a `<select>` appears in the header listing all sub-steps. Selecting one calls `setWranglerSelectedStep(nodeId, index)`. The panel then synthesizes a `StepResult` from that sub-step via `wranglerStepToResult()` so the existing OUTPUT/SCHEMA/JSON paths render it unchanged.

## 1.10 ConfigPanel mount + full-bleed

Clicking a node calls `setSelectedNode(nodeId)`, which triggers the ConfigPanel modal. Behavior:

- Centered modal, top 145px / bottom 240px (stops above PreviewPanel + below Toolbar)
- Backdrop: `bg-slate-900/40 backdrop-blur-sm`; clicking it closes
- Left/right inset tracks `--fpulse-nodes-panel-width` / `--fpulse-chat-panel-width` so the modal doesn't overlap the side panels
- **Full-bleed mode** (`fullBleed = stepType === 'data_wrangler'`): the modal extends edge-to-edge, side panels are hidden, the wrangler workspace owns the whole pane

Every node has a `Parameters | Settings` tab bar:
- **Parameters** — type-specific config (renders via hardcoded component if listed in `HARDCODED_CONFIGS`, otherwise via `DynamicConfig` reading `param_schema` from the backend node-types registry)
- **Settings** — execution policy common to every node:
  - Execute Once
  - Retry On Fail (with max retries, retry delay, retry strategy: fixed/linear/exponential)
  - On Error (stop / continue / continue using error output)
  - Timeout (seconds; default 300)
  - Always Output Data
  - Notes (textarea)
  - Display Note in Flow (renders the note beneath the node on canvas)
  - Node version stamp

## 1.11 Validation

`validateWorkflow.ts` runs on Save, Deploy, Run, Test Node. Checks:

1. Empty pipeline — must have ≥ 1 activity node
2. Required-fields-per-stepType — sources need `file_path` / `url` / `query`; transforms need `condition` / `expression`; etc.
3. Missing upstream — if a node has incoming edges, sources must exist
4. Connection arity — same rules as `isValidConnection`
5. Cycle detection — DFS to detect loops (DAG enforcement)
6. Undefined parameter refs — `${param.foo}` requires `foo` in `parameters[]`
7. Deactivation shadow — nodes downstream of a deactivated ancestor are marked "will skip" and exempt from required-field checks

Errors land in `store.validationErrors[nodeId]`; FPulseNode renders red error pills beneath the node header.

## 1.12 Schema propagation

`useUpstreamSchema(stepId)` is called by every ConfigPanel that needs to show column dropdowns (filter / aggregate / sort / rename / typecast / derived_column / window / etc.).

Flow:
1. Cache lookup: `workflowStore.stepSchemas[stepId]` — return immediately if present
2. Cache miss: build the unsaved IR from canvas state, POST to `/api/workflows/{wfId}/schema-for-step/{stepId}` with the IR in the body
3. Debounced 350ms — rapid edits don't spam the backend
4. Response: `{is_source, self_schema?, inputs?, error?}` — single-input nodes get a flat column list; multi-input get one entry per upstream

Result: column dropdowns reflect **live, post-transformation** column names without requiring Save or Run first. Renames, casts, and derivations propagate downstream as you edit upstream.

## 1.13 Auto-layout

Canvas footer "Auto-Layout" button applies a topological sort + layering pass:
1. Compute in-degrees
2. BFS layering (layer N = longest path from a source)
3. Position: `x = START_X + layer × H_SPACING (300px)`, `y = layer-index × V_SPACING (120px)`

Mimics a left-to-right Gantt-style flow. O(nodes + edges). Doesn't run unless explicitly invoked — manual positioning is preserved.

## 1.14 Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+S` / `Cmd+S` | Quick Save (runs `requireNamedWorkflow` + `ensureWorkflow({allowCreate:true})`) |
| `Ctrl+W` / `Cmd+W` | Close editor (prompts if dirty) |
| `Ctrl+Z` / `Cmd+Z` | Undo |
| `Ctrl+Shift+Z` / `Cmd+Shift+Z` | Redo |
| `Ctrl+A` / `Cmd+A` | Select all activity nodes (excluded inside text inputs) |
| `Delete` / `Backspace` | Delete selected (optional confirmation dialog) |
| `F2` | Rename selected node (inline) |
| `Space` | Open ConfigPanel for selected node |
| `D` | Toggle deactivation for right-clicked node |
| `Ctrl+D` | Duplicate right-clicked node |
| `Esc` | Close modal / cancel dialog |

## 1.15 Auto-save

If Settings → General → "Auto-save pipelines" is on, a `useEffect` watches `nodes`, `edges`, `pipelineParameters`, `isDirty`. On change, a 2000ms debounce timer fires `ensureWorkflow()` (no `allowCreate`) — update-only, never create. Skipped while `isRunning` to avoid racing with the executor's pre-run save.

## 1.16 Ghost nodes (AI suggestions)

When a node is added, `useEmbeddedAI.suggestNextNode()` queries `/api/ai/suggest-next`. If a suggestion is returned, a 40%-opacity dashed-border ghost node renders to the right. Click it to accept; click X to dismiss.

Legacy per-format types (csv_source, json_sink, etc.) are remapped to generic equivalents via `hiddenNodeTypes.ts` so ghosts never suggest a hidden type.

---

# Part 2 — Data Movement (Sources)

## csv_source — CSV Source

**Arity:** 0 inputs · **Category:** source · **Description:** Read data from a CSV file.

**Behavior:** DuckDB `read_csv()`. Respects the `full_run` context flag (sample vs full load). File size validated against `guardrails.check_file_size()`.

**Fields:**
- `file_path` (text, required) — path; resolved relative to `data_dir` if not absolute
- `delimiter` (select, default `,`) — `,` `;` `\t` `|`
- `header` (boolean, default true) — first row is column names
- Frontend bonus: file picker shows uploaded files with sizes; encoding select (utf-8 / utf-16 / latin-1 / ascii); skip-rows number

## json_source — JSON Source

**Arity:** 0 · **Category:** source · **Description:** Read data from a JSON file.

**Behavior:** DuckDB `read_json()`; auto-detects array vs newline-delimited. Falls back to manual parsing for deep nesting.

**Fields:**
- `file_path` (text, required)
- `format` (select, default `auto`) — `auto` / `records` (JSON array) / `lines` (NDJSON)
- `records_path` (text, optional) — dot path to the array (e.g., `data.items`)
- Frontend bonus: JSONPath helper, encoding select

## parquet_source — Parquet Source

**Arity:** 0 · **Category:** source · **Description:** Read data from a Parquet file.

**Behavior:** DuckDB `read_parquet()`; supports glob patterns (`data/*.parquet`). Optional column projection.

**Fields:**
- `file_path` (text, required) — supports glob
- `columns` (column_list, optional) — empty = all

## excel_source — Excel Source

**Arity:** 0 · **Category:** source · **Description:** Read data from an Excel spreadsheet.

**Behavior:** openpyxl (primary) with DuckDB spatial fallback. Skips fully-empty rows. Converts to relation via VALUES.

**Fields:**
- `file_path` (text, required) — .xlsx / .xls
- `sheet_name` (text, optional) — empty = first sheet
- `header_row` (number, default 1) — 1-based
- `skip_rows` (number, default 0)
- Frontend bonus: data-range text input (e.g., `A1:Z100`)

## xml_source — XML Source

**Arity:** 0 · **Category:** source · **Description:** Read data from an XML file.

**Behavior:** Parses XML, extracts elements matching `row_tag`. Element attributes become columns. Strips namespace prefixes.

**Fields:**
- `file_path` (text, required)
- `row_tag` (text, optional) — empty = root's children
- `encoding` (select, default `utf-8`)
- Frontend bonus: XPath text input

## db_source — Database Source

**Arity:** 0 · **Category:** source · **Description:** Read data from a database — pick a table or write your own SQL.

**Behavior:** Executes against saved connections (PostgreSQL, MySQL, MSSQL, SQLite) or in-memory DuckDB. Two modes: `query` (raw SQL) and `table` (auto-generated SELECT). Dialect-aware LIMIT/TOP for dev samples. Supports incremental loads via watermark column. Connection pooling for Postgres/MySQL/MSSQL.

**Fields:**
- `connection_id` (connection-picker, optional) — empty = DuckDB in-memory
- `source_mode` / `mode` (select, default `query`) — `query` / `table` (frontend also exposes `procedure`)
- `query` (sql) — shown when `mode=query`; required
- `schema` (text, optional) — shown when `mode=table`; catalog datalist
- `table` (text, required) — shown when `mode=table`; catalog datalist
- `columns` (text, optional) — shown when `mode=table`; empty = `*`
- `where` (text, optional) — shown when `mode=table`
- `order_by` (text, optional)
- `procedure` (text, required for procedure mode)
- `arguments_json` (textarea, optional) — JSON array of procedure args
- `watermark_column` (text, tab Incremental) — for incremental load
- `watermark_value` (text, tab Incremental) — last watermark
- `sample_rows` (number, default 1000, tab Settings) — dev cap (0 = no limit, ignored in Full Run)

## api_source — API Source

**Arity:** 0 · **Category:** source · **Description:** Read data from a REST API endpoint.

**Behavior:** GET against HTTP endpoints. Pagination strategies: none / offset / page / cursor / link. Auto-detects JSON response shape (looks for `data` / `results` / `items` keys). Retries on 429/5xx with exponential backoff.

**Fields:**
- `connection_id` (connection-picker, optional)
- `url` (text, optional) — direct URL alternative
- `path` (text, optional) — appended to connection's base_url
- `headers` (key-value list, optional)
- `data_key` (text, optional) — JSON key containing the array
- `file_path` (text, optional) — local JSON for testing
- `pagination` (select, default `none`) — `none` / `offset` / `page` / `cursor` / `link`
- `max_pages` (number, default 100)
- `page_size` (number, default 100, shown for offset/page)
- `offset_param` / `page_param` / `cursor_field` / `cursor_param` (text, conditional)
- `max_retries` (number, default 3, tab Reliability)
- `retry_delay` (number, default 1, tab Reliability)
- `timeout` (number, default 30, tab Reliability)
- Frontend bonus: auth type (none/bearer/basic/api_key) with conditional credential fields

## s3_source — S3 / MinIO Source

**Arity:** 0 · **Category:** source · **Description:** Read data from S3 or MinIO object storage.

**Behavior:** boto3 download to temp → format handler (CSV/JSON/Parquet). Falls back to unsigned HTTP for public buckets. Format detected from key extension.

**Fields:**
- `connection_id` (connection-picker, optional)
- `bucket` (text, required)
- `key` (text, required)
- `endpoint` (text, optional) — required for MinIO; empty for AWS
- `access_key` (text, optional)
- `secret_key` (password, optional)
- `region` (text, default `us-east-1`)
- `format` (select, default `auto`) — `auto` / `csv` / `json` / `parquet`
- Frontend bonus: operation select (read_file / read_folder / list_objects); wildcard filter for folder reads

## kafka_source — Kafka Source

**Arity:** 0 · **Category:** source · **Description:** Read messages from a Kafka topic.

**Behavior:** Consumes up to `max_messages` within `timeout_seconds`. Tries `confluent_kafka` then falls back to `kafka-python`. Each message JSON-parsed; adds `_kafka_offset` / `_kafka_partition` / `_kafka_topic` columns.

**Fields:**
- `connection_id` (connection-picker, optional)
- `topic` (text, required)
- `bootstrap_servers` (text, default `localhost:9092`)
- `group_id` (text, default `fpulse-consumer`)
- `max_messages` (number, default 100)
- `timeout_seconds` (number, default 10)
- Frontend bonus: operation select (consume_from_earliest / latest / offset)

## ftp_source — FTP Source

**Arity:** 0 · **Category:** source · **Description:** Read data from an FTP server.

**Behavior:** FTP/FTPS download to temp, then delegates to `S3SourceNode._read_file()` for format-aware parsing.

**Fields:**
- `connection_id` (connection-picker, optional)
- `host` (text, required)
- `port` (number, default 21)
- `username` (text, default `anonymous`)
- `password` (password, optional)
- `remote_path` (text, required)
- `format` (select, default `auto`)
- `use_tls` (boolean, default false) — FTPS explicit TLS

## gsheet_source — Google Sheets Source

**Arity:** 0 · **Category:** source · **Description:** Read data from a public Google Sheet.

**Behavior:** Constructs the CSV export URL from sheet ID + tab GID, downloads, reads with DuckDB. Requires "Anyone with the link" sharing.

**Fields:**
- `sheet_url` (text, optional) — full URL; ID extracted automatically
- `sheet_id` (text, optional) — just the ID
- `gid` (text, default `0`) — tab GID

## delta_source — Delta Lake Source

**Arity:** 0 · **Category:** source · **Description:** Read a Delta Lake table.

**Behavior:** `deltalake` Python library with Parquet-glob fallback. Time-travel via `version`.

**Fields:**
- `table_path` (text, required) — directory path
- `version` (number, optional) — empty = latest
- `columns` (column_list, optional)

---

# Part 3 — Data Movement (Sinks)

All sinks return the input relation unchanged so downstream preview keeps working.

## csv_sink — CSV Sink

`COPY ... TO CSV` via DuckDB.

**Fields:** `file_path` (text, default `output.csv`), `delimiter` (`,`/`;`/`\t`/`|`, default `,`), `header` (bool, default true), `quote_all` (bool, default false), `encoding`, write mode (`overwrite`/`append`).

## json_sink — JSON Sink

`COPY ... TO JSON` via DuckDB.

**Fields:** `file_path`, `format` (`array` / `lines`, default `array`), `indent` (`0`/`2`/`4`, default 2).

## excel_sink — Excel Sink

openpyxl with bolded header row, auto-sized columns (sampled to 100 rows), optional header freeze.

**Fields:** `file_path`, `sheet_name` (default `Sheet1`), `start_cell` (default `A1`), `freeze_header` (bool, default true), `header` (bool, default true).

## parquet_sink — Parquet Sink

`COPY ... TO PARQUET` via DuckDB. **Fields:** `file_path` (default `output.parquet`).

## db_sink — Database Sink

INSERT into a relational DB via saved connection. Modes: `append` / `overwrite` / `upsert`.

**Fields:** `connection_id` (required), `schema` (optional), `table` (required), `mode` (default `append`), `key_columns` (column_list, shown when mode=upsert), `batch_size` (default 5000), `pre_sql` / `post_sql` (optional textareas).

## s3_sink — S3 / MinIO Sink

Stages a temp file (Parquet/CSV/JSON), uploads via boto3.

**Fields:** `connection_id`, `bucket` (required), `key` (required), `endpoint`, `access_key`, `secret_key`, `region` (default `us-east-1`), `format` (default `parquet`).

## kafka_sink — Kafka Sink

One JSON message per row, batched flush.

**Fields:** `connection_id`, `topic` (required), `bootstrap_servers`, `key_column` (optional — message partitioning key), `batch_size` (default 500).

## api_sink — API Sink

POST/PUT/PATCH to an endpoint. Batch (single request) or per-record. Retries on 429/5xx.

**Fields:** `connection_id`, `url`, `path`, `method` (default `POST`), `headers`, `batch_mode` (`bulk`/`per_record`), `on_error` (`fail`/`continue` — per_record only), `max_retries` (default 3), `retry_delay` (default 1), `timeout` (default 30).

## email_sink — Email Sink

Writes the relation to a temp file and emails it via SMTP. Auto-generates body with row count + column list if `body_text` is empty.

**Fields:** `connection_id`, `smtp_host` (required), `smtp_port` (default 587), `smtp_user`, `smtp_password`, `use_tls` (default true), `from_address`, `to_addresses` (list, required), `subject` (default "F-Pulse Pipeline Output"), `body_text`, `attach_format` (`csv`/`json`/`parquet`, default `csv`).

## delta_sink — Delta Lake Sink

Append or overwrite a Delta table via `deltalake`.

**Fields:** `table_path` (required), `mode` (`append`/`overwrite`, default `append`).

## warehouse_sink — Warehouse Sink

Cloud DW writes (Snowflake / BigQuery / Redshift) via saved connection.

**Fields:** `connection_id` (required), `table` (required), `mode` (`append`/`overwrite`/`merge`), `merge_key` (column_list, shown for merge).

## file_sink — File Sink

Auto-detects format from file extension; delegates to format-specific handler.

**Fields:** `file_path` (required), `format` (`auto`/`csv`/`json`/`parquet`, default `auto`).

## Cloud-storage sinks (sharepoint / onedrive / gdrive / dropbox / box / adls_gen2 / azure_blob / gcs)

All follow the same pattern: pick a saved connection or fill in inline auth, name the bucket/container/library, give a path/key/file name, pick a format. ADLS Gen2 + Azure Blob + GCS use DuckDB's native extensions (`SECRET` registration); Office-365-flavored sinks (SharePoint/OneDrive/Box/Dropbox) upload via vendor APIs.

---

# Part 4 — Transform

## filter — Filter

**Arity:** 1 · **Behavior:** WHERE clause. Two modes:
- `expression` — raw SQL WHERE (e.g., `amount > 100 AND status = 'active'`)
- `rules` — visual builder with AND/OR combinator; ops: `=` `!=` `>` `<` `>=` `<=` `LIKE` `NOT LIKE` `IN` `NOT IN` `IS NULL` `IS NOT NULL` `BETWEEN` `contains` `not_contains` `starts_with` `ends_with`

**Fields:** `mode` (default `expression`), `condition` (textarea, required for expression), `rules` (rule list), `combinator` (`AND`/`OR`), `rule_groups` + `group_combinator` (advanced).

## transform — Transform

**Arity:** 1 · **Behavior:** Arbitrary SELECT. Upstream registered as `source_table` and `input`. All ancestors also registered by step ID (hyphens → underscores) and by sanitized display label, so the user can join across the DAG.

**Fields:** `expression` (sql, required). The TransformHelper widget renders inline above the SQL textarea showing upstream columns as chips you can click to insert.

## aggregate — Aggregate

**Arity:** 1 · **Behavior:** GROUP BY + N aggregates. Empty group_by = global aggregation. HAVING for post-aggregation filter; ORDER BY for result ordering.

**Fields:**
- `group_by` (column_list) — empty = global
- `functions` / `aggregations` (aggregate_list, required) — each row: `{column, function, alias, [percentile]}`. Functions: `COUNT` `SUM` `AVG` `MIN` `MAX` `COUNT_DISTINCT` `MEDIAN` `STDDEV` `VARIANCE` `PERCENTILE_CONT` `PERCENTILE_DISC` `STRING_AGG` `ARRAY_AGG` `FIRST` `LAST` `CUSTOM`
- `having` (expression, tab Filter) — e.g., `COUNT(*) > 10`
- `order_by` (text, tab Filter)

## deduplicate — Deduplicate

**Arity:** 1 · **Behavior:** Drops duplicates by key columns. `keep_first` = `ROW_NUMBER()` window; `keep_last` = `DISTINCT ON`. Optional ORDER BY for ordering within duplicates.

**Fields:** `key` (column_list, required), `strategy` (`keep_first`/`keep_last`, default `keep_first`), `order_by` (text, optional).

## flatten_explode — Flatten / Explode

**Arity:** 1 · **Behavior:** Two modes:
- `flatten` — expand STRUCT or JSON fields into top-level columns (`struct.*` first, json_extract fallback)
- `explode` — UNNEST array column so each element becomes a row

**Fields:** `mode` (default `flatten`), `column` (text, required), `prefix` (text, optional — column-name prefix for expansions), `keep_original` (bool, default false).

## materialize — Materialize

**Arity:** 1 · **Behavior:** Checkpoint node — caches upstream relation to a DuckDB temp table. Downstream reads from the snapshot instead of re-executing the upstream chain. Critical for diamond DAGs and iterative development.

**Fields:** `table_name` (text, optional — auto-generated if empty).

## sort — Sort

**Arity:** 1 · **Behavior:** `ORDER BY` via SQL. Unified direction across all sort columns.

**Fields:** `sort_by` (column_list, required), `direction` (`ASC`/`DESC`, default `ASC`). Frontend's "Sort By" text input accepts free-form comma-separated `col ASC, col DESC`.

## rename — Rename Columns

**Arity:** 1 · **Behavior:** `SELECT ... AS ...` renames via a `{old → new}` map.

**Fields:** `mappings` (key-value list, required) or `rename_map` (JSON `{"old": "new"}`).

## typecast — Type Cast

**Arity:** 1 · **Behavior:** Per-column `CAST` using DuckDB type names.

**Fields:** `casts` (key-value list, required) — `{column → type}`. Types: `INTEGER` `BIGINT` `DOUBLE` `DECIMAL(p,s)` `VARCHAR` `BOOLEAN` `DATE` `TIMESTAMP`.

## derived_column — Derived Column

**Arity:** 1 · **Behavior:** `SELECT *, <expr> AS <name>` — adds computed columns.

**Fields:** `columns` / `derived` (list, required) — each row: `{name, expression}`.

## pivot — Pivot

**Arity:** 1 · **Behavior:** DuckDB `PIVOT`. Pivots distinct values of `pivot_column` into separate columns, aggregating `value_column`.

**Fields:** `pivot_column` (text, required), `value_column` (text, required), `agg_function` (default `SUM`; options `SUM` `COUNT` `AVG` `MIN` `MAX`), `group_by` (column_list, optional).

## unpivot — Unpivot

**Arity:** 1 · **Behavior:** DuckDB `UNPIVOT`. Converts columns into name/value rows.

**Fields:** `columns` (column_list, required), `name_column` (text, default `attribute`/`variable`), `value_column` (text, default `value`).

## window — Window

**Arity:** 1 · **Behavior:** Window functions with PARTITION BY / ORDER BY / frame. Multiple functions per node.

**Fields:**
- `partition_by` (column_list, optional, tab Window)
- `order_by` (column_list, optional, tab Window)
- `order_direction` (`ASC`/`DESC`, default `ASC`, tab Window)
- `frame` (text, optional, tab Window) — e.g., `ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING`
- `window_functions` (list, required, tab Functions) — each row: `{function, column?, alias, [offset], [n]}`. Functions: `ROW_NUMBER` `RANK` `DENSE_RANK` `NTILE` `LAG` `LEAD` `FIRST_VALUE` `LAST_VALUE` `NTH_VALUE` `SUM` `AVG` `MIN` `MAX` `COUNT` `PERCENT_RANK` `CUME_DIST`. Column hidden for ROW_NUMBER/RANK/DENSE_RANK/PERCENT_RANK/CUME_DIST. NTILE requires `n`; LAG/LEAD accept `offset`.

## sample — Sample

**Arity:** 1 · **Behavior:** `LIMIT N` (first) or `USING SAMPLE N` (random).

**Fields:** `count` (number, default 100), `method` (`first`/`random`, default `first`). Frontend also exposes `fraction` (0–1) for percentage sampling.

## validate — Validate

**Arity:** 1 · **Behavior:** Adds `is_valid` boolean column. All rules must pass for `is_valid = true`.

**Fields:** `rules` (rule_list, required) — each rule: `{name, condition}` where condition is a SQL boolean expression. Frontend's rule builder offers check types: `not_null`, `unique`, `positive`, `in_range`, `regex`, `min_length`.

## conditional_split — Conditional Split

**Arity:** 1 · **Behavior:** Routes rows by adding a `_split_output` label column. Modes:
- `first_match` — first matching condition wins (one output per row)
- `all_match` — row appears in every matching output
- `filter` — return only rows matching `active_output`

**Fields:** `conditions` (rule_list, required, tab Conditions) — `{name, condition}`. `default_output` (text, default `default`). `mode` (default `first_match`). `active_output` (text, tab Behavior) — used when mode=filter.

## data_quality — Data Quality

**Arity:** 1 · **Behavior:** Validates rows against declarative rules. Modes:
- `drop` — silently remove failed rows
- `fail` — abort if any row fails
- `tag` — add `__dq_passed` boolean
- `split` — separate passing/failing into two outputs

**Fields:**
- `rules` (rule_list, required, tab Rules) — operators: `not_null` `is_null` `eq` `ne` `gt` `lt` `gte` `lte` `in` `not_in` `regex` `between` `min_length` `max_length`
- `mode` (default `drop`)
- `quality_threshold` (number, default 0, tab Quality) — minimum pass rate %; 0 = disabled
- `include_score` (bool, default false, tab Quality) — adds `__dq_score` 0–100
- `include_profile` (bool, default false, tab Quality) — logs per-column null % + distinct counts

## upsert — Upsert

**Arity:** 1 · **Behavior:** Idempotent merge by key columns. Rows with the same key collapse to the latest (per ORDER BY). Prevents duplicates on re-run.

**Fields:** `key_columns` (column_list, required), `order_by` (text, optional — e.g., `updated_at DESC`).

## schema_mapper — Schema Mapper

**Arity:** 1 · **Behavior:** Field-level mapping + type coercion. Used to bridge manifest-driven SaaS connectors to generic sinks.

**Fields:** `mappings` (mapping_list, required) — each row: `{source_column, target_column, target_type}`.

## data_wrangler — Data Wrangler

**Arity:** 1 · **Behavior:** Multi-step transform on one canvas node. Six sub-op types: `filter` `select` `rename` `cast` `derive` `group_by`. Each step can be enabled/disabled, reordered, duplicated. Steps compile to a single SQL statement at runtime (subselects fused by DuckDB).

**Fields:** `steps` (step_list) — each step: `{id, op, enabled, label, config}`. The config shape depends on `op`:
- `filter`: `{mode: 'expression'|'rules', expression?, rules?, combinator?}`
- `select`: `{columns: string[]}`
- `rename`: `{rename_map: {old: new}}`
- `cast`: `{casts: [{column, to_type}]}`  — types: `INTEGER` `BIGINT` `DOUBLE` `VARCHAR` `BOOLEAN` `DATE` `TIMESTAMP` `DECIMAL(18,2)`
- `derive`: `{derived: [{name, expression}]}`
- `group_by`: `{keys: string[], aggregations: [{func, column, alias}]}` — funcs: `SUM` `COUNT` `AVG` `MIN` `MAX` `COUNT_DISTINCT`

**UI:** 3-pane full-bleed workspace (Steps list 260px / Step config form 380px / Step profile pane flex-1). Per-step delta strip (row count + schema delta). Live preview debounced 800ms after edits via `POST /api/workflows/{wid}/nodes/{nid}/data-wrangler/preview`. Generated SQL panel collapsible at the bottom. Per-step inspection mirrored into the bottom OUTPUT panel via `wranglerPreview` + Step ▾ selector.

## retry_handler — Retry / Error

**Arity:** 1 · **Behavior:** Pass-through node that configures retry behavior for the upstream node. The executor scans for `retry_handler` and wraps the upstream `execute()` with retry + backoff.

**Fields:** `max_retries` (number, default 3), `delay_seconds` (number, default 2), `backoff_multiplier` (number, default 2.0 — exponential), `on_exhausted` (`fail`/`skip`, default `fail`).

---

# Part 5 — Combine (multi-input)

## join — Join

**Arity:** 2 · **Behavior:** SQL join — INNER / LEFT / RIGHT / FULL OUTER / SEMI / ANTI / CROSS. SEMI returns left rows with a match in right; ANTI returns left rows without a match. Three key modes:
- `same_key` — shared column names
- `mapped_keys` — `{left, right, operator}` pairs; operators: `=` `>` `<` `>=` `<=` `!=`
- `custom` — raw SQL ON clause; tables aliased as `__join_left` / `__join_right`

**Fields:**
- `join_type` (select, default `INNER`)
- `key_mode` (select, default `same_key`, hidden when join_type=CROSS)
- `join_key` (column_list, shown for same_key) — comma-separated `id, customer_id`
- `key_pairs` (key_pair_list, shown for mapped_keys)
- `custom_on` (expression, shown for custom)
- `select_left` (text, tab Columns) — empty = all
- `select_right` (text, tab Columns) — empty = all

## lookup — Lookup

**Arity:** 2 · **Behavior:** LEFT JOIN enrichment. Rows from main pass through; missing-match lookup columns are NULL.

**Fields:** `lookup_key` (text, required).

## union — Union

**Arity:** N≥2 · **Behavior:** `UNION ALL` (with duplicates) or `UNION` (distinct) across all inputs.

**Fields:** `mode` (`all`/`distinct` (backend) or `all`/`by_name` (frontend variant), default `all`).

## scd2 — Slowly-Changing Dimension Type 2

**Arity:** 1–2 (incoming required, current_target optional) · **Behavior:** Type-2 SCD. For new business keys, inserts as current. For existing keys with unchanged tracked columns, keeps current. For changed tracked columns, closes the old version (`is_current=false`, `valid_to=run_time`) and opens a new current version. Deterministic SHA-256 surrogate keys (`sha256(business_key||valid_from)`) for idempotency.

**Fields:**
- `business_key` (list[str], required)
- `tracked_columns` (list[str], required) — changes trigger new version
- `effective_from_column` (str, default `valid_from`)
- `effective_to_column` (str, default `valid_to`)
- `current_flag_column` (str, default `is_current`)
- `surrogate_key_column` (str, default `scd_id`)
- `null_high_water` (str, default `9999-12-31`) — sentinel valid_to for active version
- `passthrough_columns` (list[str], default `[]`) — carried through but NOT used for change detection

---

# Part 6 — Control Flow

## if_condition — If Condition

**Arity:** 1 · **Behavior:** Boolean gate — rows where condition is TRUE pass; others dropped. Single output (no branching — use `conditional_split` or `switch_case` for that).

**Fields:** `condition` (expression, required, default `1=1`).

## switch_case — Switch Case

**Arity:** 1 · **Behavior:** Routes rows by column value matching predefined cases. `active_case` selects the active branch; falls back to `default_case` if no match.

**Fields:** `column` (text, required), `cases` (rule_list — `{value, label}`), `active_case` (text), `default_case` (expression).

## foreach_loop — ForEach Loop

**Arity:** 1 · **Behavior:** Splits input into equal-sized batches (or processes all as one if `batch_size=0`). Tags each batch with `_batch_index` / `_batch_total`. Sequential or parallel.

**Fields:** `batch_size` (number, default 0), `mode` (`sequential`/`parallel`, default `sequential`), `on_error` (`fail`/`continue`, default `fail`).

## until_loop — Until Loop

**Arity:** 1 · **Behavior:** Iteratively applies a SQL condition, removing rows that satisfy stop condition. Repeats until all rows satisfy OR `max_iterations` reached. Useful for polling.

**Fields:** `condition` (expression, required), `max_iterations` (number, default 10, hard cap 1000).

## wait_delay — Wait / Delay

**Arity:** 1 · **Behavior:** Sleeps N seconds, passes input through unchanged. Hard cap 300 seconds.

**Fields:** `seconds` (number, default 1).

## set_variable — Set Variable

**Arity:** 1 · **Behavior:** Adds computed columns to the data, each representing a pipeline variable. Variables persist across steps via `ctx.vars`.

**Fields:** `variables` (derived_list, required) — `{name, expression}`.

## execute_pipeline — Execute Pipeline

**Arity:** 1 · **Behavior:** Invokes a sub-workflow by ID. Optionally passes parameters into the sub-pipeline's variable scope. Returns final result if available, otherwise passes input through.

**Fields:** `pipeline_id` (workflow-picker, required), `wait_for_completion` (bool, default true), `on_failure` (`fail`/`skip`/`continue`, default `fail`), `parameters` (key-value map, default `{}`).

## append_variable — Append Variable

**Arity:** 0–1 · **Behavior:** Pushes a value onto an array variable on `ctx.vars`. Creates the array if missing.

**Fields:** `variable_name` (text, required), `value` (text — literal or @-expression).

## filter_array — Filter Array

**Arity:** 0–1 · **Behavior:** Filters an array variable using a safe Python boolean expression where each element is `item`. Stores the result back into `output_variable`.

**Fields:** `input_variable` (text, required), `output_variable` (text, defaults to input), `condition` (expression, required — e.g., `item > 100`).

## validation — Wait for File

**Arity:** 0–1 · **Behavior:** Polls a path until it exists, meets minimum size, or contains items. Raises TimeoutError on deadline. Passes input through.

**Fields:** `path` (text, required), `timeout` (number, default 60), `sleep` (number, default 5, min 1), `minimum_size` (number, default 0 — bytes; 0 = skip), `child_items` (bool, default false — wait for non-empty dir).

## fail — Fail

**Arity:** 0–1 · **Behavior:** Explicitly fails the pipeline with a custom message + error code. Optional `condition` to gate failure. Supports `{column}` placeholders rendered from the first upstream row.

**Fields:** `message` (text, required), `error_code` (text, default `USER_FAIL`), `condition` (expression, optional).

---

# Part 7 — Action

## code_script — Code Script

**Arity:** 1 · **Behavior:** Executes user Python on a pandas DataFrame named `df`. Code is statically scanned for dangerous patterns (file I/O, exec, eval, system calls) and runs in a restricted namespace with only safe builtins + pandas/numpy/math/json imports. Configurable timeout (max 300 s).

**Fields:** `code` (code, language `python`, required) — receives `df`, must return `df`.

## send_email — Send Email

**Arity:** 1 · **Behavior:** SMTP send (inline or from a connection). Optional HTML body, CC/BCC, `{column}` placeholders. Batch (one email) or per-row.

**Fields (Message tab):** `to` (text, required), `cc`, `bcc`, `subject` (default "F-Pulse Notification"), `body_type` (`plain`/`html`, default `plain`), `body` (code).
**Behavior tab:** `per_row` (bool, default false), `on_error` (`fail`/`continue`, default `fail`).
**SMTP (inline) tab:** `security` (`tls`/`ssl`/`none`, default `tls`), `smtp_host`, `smtp_port`, `smtp_user`, `smtp_pass`, `from`.
**Connection tab:** `connection_id` (connection-picker).

## slack_notify — Slack Notify

**Arity:** 1 · **Behavior:** Posts to a Slack channel via Incoming Webhook. Supports `{column}` placeholders rendered from the first input row.

**Fields:** `webhook_url` (text, required), `message` (code — supports `{column}`), `channel` (text — override, e.g., `#general`).

## copy_data — Copy Data

**Arity:** 0–1 · **Behavior:** Three execution shapes:
1. `source_connection` set → read from source DB, write to sink DB (DB→DB)
2. Only `sink_connection` set → take upstream input, write to sink (in→DB)
3. Neither → pass-through

Supports column mapping (auto/explicit), table actions (none/create/truncate/recreate), bulk loading, pre/post-copy scripts, staging.

**Fields:**
**Source tab:** `source_connection_id`, `source_kind` (`table`/`query`, default `table`), `source_table`, `source_query`, `source_filter` (expression).
**Sink tab:** `sink_connection_id`, `sink_table` (required), `table_action` (`none`/`create`/`truncate`/`recreate`), `write_behavior` (`append`/`overwrite`/`upsert`/`merge`), `key_columns` (column_list for upsert/merge).
**Mapping tab:** `mapping_mode` (`auto`/`explicit`), `mappings` (list of `{source, target, type}` for explicit).
**Settings tab:** `max_rows` (default 0 = unlimited), `batch_size` (0 = auto), `enable_staging` (bool, default false), `pre_copy_script`, `post_copy_script`, `skip_on_error` (bool, default false).

## delete_data — Delete Data

**Arity:** 1 · **Behavior:** Removes rows matching a SQL condition (inverse of filter), OR deletes files from disk (files mode with wildcard + recursive options).

**Fields (Source tab):** `target_kind` (`rows`/`files`, required), `condition` (expression — for rows), `target_path` (text — for files), `wildcard` (text — for files), `recursive` (bool, default true — for files).
**Settings tab:** `max_concurrent` (number, default 1).
**Logging tab:** `enable_logging` (bool, default false), `log_path` (text).

## get_metadata — Get Metadata

**Arity:** 1 · **Behavior:** Returns one row per column with `name`, `type`, `nullable`, `null_count`, `distinct_count`, `min_value`, `max_value`, plus total `row_count`. Can also stat files/directories.

**Fields (Dataset tab):** `dataset_kind` (`upstream`/`file`/`directory`, default `upstream`), `dataset_path` (text — for file/directory mode).
**Field-list tab:** `fields` (multi-select — which metadata fields to compute), `include_row_count` (bool, default true), `include_size` (bool, default false).

## file_system — File System

**Arity:** 0–1 · **Behavior:** Local filesystem ops. Returns a relation with `operation` / `source` / `destination` / `status` / `error` columns.

**Fields:** `operation` (select, required) — `copy_file` / `move_file` / `rename_file` / `delete_file` / `copy_directory` / `move_directory` / `delete_directory` / `create_directory`; `source` (text, required); `destination` (text — required for copy/move/rename); `overwrite` (bool, default false); `recursive` (bool, default true); `continue_on_error` (bool, default false).

## execute_sql_task — Execute SQL

**Arity:** 0–1 · **Behavior:** Arbitrary SQL (DDL / DML / anonymous blocks) against a real database connection — unlike Transform, which is SELECT on in-memory data. Returns row count or full result set per `return_mode`. Supports `{column}` placeholders rendered from the first upstream row.

**Fields:** `connection_id` (connection-picker — empty = in-memory DuckDB), `sql` (code, required), `return_mode` (`rowcount`/`full`, default `rowcount`), `timeout` (number, default 60).

## http_request — HTTP Request

**Arity:** 1 · **Behavior:** Makes HTTP calls (GET / POST / PUT / PATCH / DELETE) and captures JSON responses. Batch (single call) or per-row (one call per input row with `{column}` placeholders rendered per row). Merges response with input columns.

**Fields:** `url` (text, required — supports `{column}` per-row), `method` (default `GET`), `headers` (key-value list), `body` (code — JSON; supports `{column}` per-row), `per_row` (bool, default false), `timeout` (number, default 0 — 0 means use 30 s or `FPULSE_HTTP_DEFAULT_TIMEOUT` env).

---

# Part 8 — AI / Semantic

## embedder — Embedder

**Arity:** 1 · **Behavior:** Text column → numeric vector via a configurable provider. Providers:
- `hash` — deterministic, free, offline (default fallback)
- `openai` — `text-embedding-3-small` default
- `cohere` — `embed-english-v3.0` default
- `sentence_transformers` — `all-MiniLM-L6-v2` default

Batches embeddings; appends a vector column.

**Fields:** `text_column` (column, required), `provider` (default `hash`), `model` (text — overrides provider default), `dim` (number, default 384 — for hash), `output_column` (text, default `embedding`), `batch_size` (number, default 64).

## llm_guardrail — LLM Guardrail

**Arity:** 1 · **Behavior:** Scans a text column for PII (email, phone, SSN, credit card, IP), prompt-injection patterns, and profanity via regex. Three actions: tag (add `__guardrail_flags` column), block (drop offending rows), mask (replace detected content).

**Fields:** `text_column` (column, required), `checks` (multi-select — `pii` / `prompt_injection` / `profanity`), `mode` (`tag`/`block`/`mask`, default `tag`), `extra_patterns` (string_list — custom regexes).

## semantic_router — Semantic Router

**Arity:** 1 · **Behavior:** Classifies rows into one of N labels by computing per-row embeddings and comparing (cosine similarity) against prototype embeddings built from label examples. Threshold gating routes low-confidence rows to a default label.

**Fields:** `text_column` (column, required), `labels` (label_list, required — `{name, examples[]}`), `provider` (default `hash`), `model` (text, optional), `dim` (number, default 384), `threshold` (number, default 0.0), `default_label` (text, default `other`).

---

# Part 9 — Per-node Settings tab (every node)

When you switch to the **Settings** tab in any node's ConfigPanel:

| Field | Type | Default | Effect |
|---|---|---|---|
| Execute Once | Toggle | off | Run once regardless of input rows |
| Retry On Fail | Toggle | off | Enables the retry block below |
| Max Retries | Number | 3 | (Shown when Retry On Fail) |
| Retry Delay (ms) | Number | 1000 | (Shown when Retry On Fail) |
| Retry Strategy | Select | exponential | fixed / linear / exponential |
| On Error | Select | stop | Stop Workflow / Continue (ignore) / Continue using error output |
| Timeout (seconds) | Number | 300 | Per-node execution timeout |
| Always Output Data | Toggle | off | Output even on empty result |
| Notes | Textarea | — | Free-text notes about this node |
| Display Note in Flow | Toggle | off | Render note text below node on canvas |
| Node version | Read-only | v1.0 (Latest) | Stamp at the bottom |

These settings are stored on `params._settings.*` and read by the executor. Deactivation is also stored here (`_settings.deactivated`).

---

# Part 10 — What's NOT in OSS

Per memory rules, the following are Plus-only:
- RBAC + roles (both agent-tool 4-role and workspace 5-tier)
- PROD environment chrome (env badges, env filters, env columns)
- Workspace governance (approvals, SSO, OIDC, sandbox, drift, vault, lifecycle gates)
- Live shadow runs (DEFERRED 2026-05-12 — PROD-Sandbox is the answer)
- Schedule-level alerts, per-tenant alert email domain allowlist, custom KPI formulas (Metric Editor)
- Pool runtime intelligence dashboards (failure heatmap, event stream, scaling recs)
- Tumbling-window scheduling + all event-based triggers (file-arrival/CDC/queue-message)
- Bulk loaders, CDC, SCD2 backfill, checkpointing (Plus paid gates)

Every connector ships unlocked in OSS — Plus differentiates on governance, not connector access.

---

# Appendix — File index

| Concern | File |
|---|---|
| Node registry (backend) | `backend/fpulse/nodes/*.py` + `backend/fpulse/ir/schema.py` |
| Node IR types | `backend/fpulse/ir/types.py` |
| Canvas | `frontend/src/components/Canvas.tsx` |
| Node renderer | `frontend/src/components/nodes/FPulseNode.tsx` |
| Edge renderer | `frontend/src/components/nodes/CustomEdge.tsx` |
| Modules palette | `frontend/src/components/ModulesPanel.tsx` |
| ConfigPanel modal | `frontend/src/components/ConfigPanel.tsx` |
| Data Wrangler workspace | `frontend/src/components/data-wrangler/DataWranglerConfig.tsx` |
| Bottom PreviewPanel | `frontend/src/components/PreviewPanel.tsx` |
| Editor toolbar | `frontend/src/components/Toolbar.tsx` |
| Workflow store | `frontend/src/stores/workflowStore.ts` |
| Connection-arity rules | `frontend/src/utils/nodeArity.ts` |
| Workflow validation | `frontend/src/utils/validateWorkflow.ts` |
| Upstream-schema cache | `frontend/src/hooks/useUpstreamSchema.ts` |
| Name-prompt guard | `frontend/src/utils/requireNamedWorkflow.ts` |
| Execution WebSocket | `frontend/src/hooks/useExecutionWS.ts` |
| Dynamic config (schema-driven) | `frontend/src/components/DynamicConfig.tsx` |
| Dialog primitives (uiAlert/uiConfirm/uiPrompt) | `frontend/src/ui/dialog.tsx` |

---

# Addendum — 2026-05-18 review hardening pass

Three external reviews of this doc landed concrete actionable items. Code changes that landed:

## Bucket A — confirmed bugs / gaps (now fixed)

- **A1 — Execute-Once-inside-loop validation guard.** [`validateWorkflow.ts`](../frontend/src/utils/validateWorkflow.ts) now walks ancestors of any node with `_settings.execute_once = true` and warns if it sits downstream of a `foreach_loop` or `until_loop`. Without this, "Execute Once" inside a loop fires only on the first iteration and silently skips on later ones.
- **A2 — Self-loop rejection.** [`Canvas.tsx`](../frontend/src/components/Canvas.tsx) `isValidConnection` blocks `source === target` drops at the source. Prevents trivial cycles before they reach the runtime.
- **A3 — Duplicate-edge rejection.** Same call site blocks identical `source→target` edges (two edges between the same pair carried no semantics; they rendered on top of each other).
- **A4 — SCD2 `delete_detection` param.** [`scd2.py`](../backend/fpulse/nodes/scd2.py) gained a new `delete_detection` param with values `ignore` (default — preserves prior behavior) and `soft_close` (closes orphan current rows the same way a tracked-column change does: `is_current=false`, `valid_to=run_time`). Standard "Type 2 with deletes" semantics common in finance/regulatory dimensions.

## Bucket B — semantic refinements

- **B5 — Side-effect badge.** New `SIDE_EFFECT_NODES` set and `hasSideEffect(stepType)` helper in [`nodeArity.ts`](../frontend/src/utils/nodeArity.ts). [`FPulseNode.tsx`](../frontend/src/components/nodes/FPulseNode.tsx) renders a small amber ⚠ at the icon's bottom-right for every sink, HTTP/email/Slack call, file delete, `execute_sql_task`, and `execute_pipeline`. Tooltip explains that preview / retry / resume may have real-world consequences.
- **B6 — Formal input contracts.** [`nodeArity.ts`](../frontend/src/utils/nodeArity.ts) now exposes `contractFor(stepType): {arity, required, optional, variadic}`. Replaces the implicit "1 or 2" check with explicit required/optional/variadic semantics:

  | Step type | Required | Optional | Variadic |
  |---|---|---|---|
  | `join`, `lookup` | 2 | 0 | false |
  | `union` | 2 | 0 | **true** |
  | `scd2` | 1 | 1 | false |
  | `append_variable`, `filter_array`, `validation`, `fail` | 0 | 1 | false |
  | `copy_data`, `file_system`, `execute_sql_task` | 0 | 1 | false |
  | sources (csv_source, db_source, …) | 0 | 0 | false |
  | everything else | 1 | 0 | false |

  [`validateWorkflow.ts`](../frontend/src/utils/validateWorkflow.ts) consumes this for precise error messages ("X needs at least 2 inputs but only has 1" vs. "X accepts at most 1 input but has 3"). The previous coarse `MULTI_INPUT_NODES` check is dropped.
- **B7 — Wrangler step-dependency check.** [`DataWranglerConfig.tsx`](../frontend/src/components/data-wrangler/DataWranglerConfig.tsx) gained `computeStepWarnings(steps)` that, for each enabled sub-step, scans the columns it references and flags any that are produced only by a disabled-or-later step. Renders an amber inline warning beneath the step in the list. Catches the "disable step 2, step 4 still uses its derived column" footgun.

## Bucket C — performance / correctness

- **C8 — Dependency-scoped schema invalidation.** [`workflowStore.ts`](../frontend/src/stores/workflowStore.ts) no longer wipes the entire `stepSchemas` cache on every structural change. New helpers:
  - `invalidationScope(seedIds, edges)` — BFS from seeds through current edges, returns the seed + all reachable descendants
  - `pruneSchemas(schemas, drop)` — copy-on-write removal of dropped entries

  `onNodesChange` invalidates only the removed node + its descendants; `onEdgesChange` invalidates the changed edge's target + descendants. Untouched nodes keep their cached schemas — eliminates the full-cache-refetch cascade Review 3 flagged.
- **C10 — Local mock-eval for trivial transforms.** [`useUpstreamSchema.ts`](../frontend/src/hooks/useUpstreamSchema.ts) gained `tryLocalMockEval(stepId, store)`. For a node whose single upstream is a "trivial" transform (`rename`, `select`, `typecast`, `derived_column`) and whose upstream has cached input schema, the output schema is derived purely client-side from the upstream's params — no backend round-trip. `derived_column` expressions are conservatively typed as `VARCHAR` (we don't parse SQL); callers can always force a backend re-fetch by clearing the cache. Saves 350ms × N latency on long chains of trivial nodes.

## Bucket C9 — two-dimensional edges (designed, not built)

Review 2's strongest architectural critique: today's edge `data.condition` field overloads **two** concepts — data dependency (which upstream's output flows into this node) and execution dependency (whether this node runs only if upstream succeeded / failed / completed).

### The split

Proposed model:
```ts
interface EdgeDataDep {
  kind: 'data';
  source: string;
  target: string;
  source_port?: string;   // for split nodes — which output
  target_port?: string;   // for join — left vs right
}

interface EdgeExecDep {
  kind: 'exec';
  source: string;
  target: string;
  fire_when: 'success' | 'failure' | 'completion' | 'skipped';
}
```

Two visual treatments:
- Data edges: solid line, arrow, animated, carries row-count badge
- Exec-only edges: dashed line, no arrow, condition pill at midpoint

A normal data flow has only a data edge between nodes. A control-flow chain (e.g., "run notify after the pipeline completes regardless of status") uses an exec-only edge. Mixed (most common today) = both edges between the same pair.

### Why not now

This is an IR refactor touching:
- Edge type discriminator + ReactFlow custom edge renderers
- `validateWorkflow.ts` (different rules per edge kind)
- Backend executor (currently every edge is treated as a data + exec dep simultaneously)
- Workflow JSON schema + migration for existing saved workflows
- ConfigPanel "On Error" Settings — today this is per-NODE (continue/stop) and may want to migrate to per-EDGE for fine-grained downstream routing

Estimated 2–3 weeks including backend, migrations, UI, validation. Worth doing in a dedicated cycle after the OSS launch settles, not bolted on now.

## Bucket D11/12/13 — progressive disclosure (shipped)

[`modulesPanelData.ts`](../frontend/src/components/modulesPanelData.ts) `ModuleItem` gained an optional `level: 'beginner' | 'intermediate' | 'advanced'` (default `intermediate`). [`ModulesPanel.tsx`](../frontend/src/components/ModulesPanel.tsx) renders a compact `Basic | Standard | All` chip selector above the search; choice persists in `localStorage` (`fpulse.paletteLevel`). Default is `Standard`.

Per-tier visibility:

| Level | Visible items |
|---|---|
| **Basic** (beginner-only) | Source, Destination, Data Wrangler, SQL Transform, Join, Aggregate |
| **Standard** (beginner + intermediate) | + Filter, Derived Column, Sort, Deduplicate, Sample, Copy Data, Lookup, Union, If/Else, Switch, ForEach, Wait, HTTP Request, Send Email, Slack/Teams, Webhook, Embedder |
| **All** (everything) | + Schema Mapper, Data Quality, Flatten/Explode, Upsert, Pivot, Unpivot, Window, Set Variable, Exec Pipeline, Retry/Error, Fail, Code/Script, Get Metadata, LLM Guardrail, Semantic Router |

Reviewer 1's biggest call was to push toward stage-first UX (named stages like "Import → Clean → Validate → Aggregate → Publish"). The level filter is a step in that direction without rewriting the entire palette model — once Basic + Wrangler is the default first-time experience, the stage-first migration becomes additive (group beginner nodes by stage) rather than a forklift change.

## Bucket D14 — code_script sandbox hardening path

Review 3 correctly called out that the current `code_script` security model — regex scan for dangerous patterns + restricted namespace — is "notoriously thin." Real attackers can bypass regex with obfuscation, dynamic attribute access, or imports via `__builtins__` reachables.

### Current state

[`code_script` node](../backend/fpulse/nodes/) scans for `exec` / `eval` / `import os` / `__import__` / file-I/O patterns and rejects matches; runtime exec uses a stripped `__builtins__` with only pandas/numpy/math/json available. Hard timeout (max 300s). Operates on a pandas DataFrame named `df`; returns the modified `df`.

### Hardening path (planned, not built)

1. **Short-term (1–2 days):** Add an AST-based scanner (`ast.walk`) to catch obfuscation the regex misses (`getattr(builtins, "ex"+"ec")`, dynamic imports, `__class__` chain access). Reject any `ast.Attribute` whose chain reaches `__class__`, `__bases__`, `__subclasses__`, `__globals__`. Still in-process but materially better than regex.
2. **Medium-term (1 week):** Move execution to a subprocess with `seccomp-bpf` (Linux) or [`subinterpreters`](https://peps.python.org/pep-0734/) (Python 3.12+) to enforce a CPU + memory cap and deny syscalls. Subprocess IPC is via JSON over stdin/stdout — slow for large frames but adequate for the sub-MB DataFrames typical of `code_script` use.
3. **Long-term (2–3 weeks):** Pyodide WASM in-browser execution for client-side use, gVisor microVM for server-side use. Pyodide gives the best UX (instant feedback, no server round-trip, true sandbox); gVisor is the heavyweight server fallback for self-hosted deployments running in untrusted contexts.

### Self-hosting note

OSS users running on a personal laptop with their own pipelines are not the threat model — the current sandbox is "thin but not zero." The threat model is **multi-tenant deployments** where untrusted user code runs in a shared process. Plus tier already has its own isolated worker pool, so the regression risk is bounded.

## Bucket D15 — SKIPPED (conflicts with locked rule)

Review 3 suggested allowing autosave to provision an ephemeral hidden `is_draft: true` row regardless of name to prevent local-canvas-state divergence from backend.

This **directly conflicts with the locked 2026-05-09 rule** ("autosave never creates; only Save button creates after name-prompt loop"). That rule was set because the user explicitly caught 4+ "Untitled Pipeline" rows piling up in the workflow list — exactly the symptom an ephemeral-draft policy would re-introduce.

The reviewer's concern (catastrophic local/server divergence on network failure) is valid but addressable without breaking the lock:

- Local canvas state is already serialized into `localStorage` via the workflow store's persistence layer
- `beforeunload` handler warns the user about unsaved changes on tab close
- The "Save and close" modal path handles dirty-close cleanly

If the divergence remains a real-world problem, the right fix is **stronger client-side persistence** (IndexedDB snapshot of the canvas every N seconds), not silent backend rows. Both honor the lock; the IndexedDB path also survives a fully offline tab close.

## Skipped from reviewer triage

- **Reviewer 3's "undo stack memory leak during drag"** — REJECTED on inspection. `onNodesChange` in [`workflowStore.ts`](../frontend/src/stores/workflowStore.ts) doesn't push undo snapshots on position changes; only discrete user actions (`onConnect`, `handleAutoLayout`, etc.) do. The existing code already does the right thing.

---

## File index updates

| Concern | File |
|---|---|
| Side-effect classification + formal contracts | `frontend/src/utils/nodeArity.ts` |
| Wrangler step-dependency warnings | `frontend/src/components/data-wrangler/DataWranglerConfig.tsx` (`computeStepWarnings`) |
| Dependency-scoped schema invalidation | `frontend/src/stores/workflowStore.ts` (`invalidationScope`, `pruneSchemas`) |
| Local mock-eval for trivial nodes | `frontend/src/hooks/useUpstreamSchema.ts` (`tryLocalMockEval`, `applyTrivialTransform`) |
| Palette level filter (Basic / Standard / All) | `frontend/src/components/ModulesPanel.tsx` (`PaletteLevel`, `levelAllows`) |
| Per-item level taxonomy | `frontend/src/components/modulesPanelData.ts` (`ModuleLevel`, `level` field on `ModuleItem`) |

---

# Part 11 — Per-node inputs & outputs

Every node in F-Pulse OSS speaks the same wire type: a **DuckDB relation** (`DuckDBPyRelation`) — a typed row stream with named columns. There is no JSON / protobuf / event-vs-row dichotomy. Even side-effecting nodes (sinks, HTTP, email) consume a relation and produce one (typically the input passed through unchanged so downstream preview keeps working).

Conventions used in the tables:

- **Inputs** column lists `n` (cardinality from §B6 input contract), required upstream columns, and any constraints on those columns' types.
- **Output columns** describes the column-level shape of the returned relation: `passthrough` = same columns and types as the input; `+col` = adds a new column; `~col` = retypes a column; `−col` = drops a column.
- **Side effect** lists anything the node does outside the dataflow (write a file, send a message, mutate a DB, etc.). Pure transforms have no side effect.

All sinks return the input relation unchanged so downstream nodes can chain off them for debugging / preview — the side effect is a parallel concern.

## 11.1 Sources (0 inputs)

| Node | Inputs | Output columns | Side effect |
|---|---|---|---|
| `csv_source` | none | DuckDB-inferred from CSV header + sample. Empty `header=false` produces `column0..N`. | none (read-only) |
| `json_source` | none | Top-level JSON object keys → columns. With `records_path`, columns are keys of array elements. Types DuckDB-inferred. | none |
| `parquet_source` | none | Native Parquet schema (columns + types preserved). With `columns=[...]`, output is a column-projection of that. | none |
| `excel_source` | none | Columns named per `header_row`. Types DuckDB-inferred from sample (typically `VARCHAR` for mixed cells). | none |
| `xml_source` | none | One column per attribute + one per child element of `row_tag`. All `VARCHAR`. | none |
| `db_source` | none | `query` mode: columns of the SELECT result; `table` mode: columns of the named table (optionally filtered by `columns` param). Types from the DB's catalog. | none (issues a SELECT) |
| `api_source` | none | Keys of the JSON array elements at `data_key`. Types DuckDB-inferred. With cursor/page pagination, rows are concatenated across pages. | none (issues N GETs) |
| `s3_source` | none | Format-dependent — delegates to the format handler matching `format` (csv/json/parquet). | none (downloads + reads) |
| `kafka_source` | none | Keys of the JSON-parsed message body + 3 metadata cols: `_kafka_offset` (BIGINT), `_kafka_partition` (INT), `_kafka_topic` (VARCHAR). | none (consumer reads only) |
| `ftp_source` | none | Format-handler dependent (same as s3_source). | none |
| `gsheet_source` | none | Sheet's first row as headers; types inferred from CSV export. | none |
| `delta_source` | none | Delta table schema (Parquet types preserved). With `version`, time-travel reads. | none |
| `webhook_trigger` | none | At runtime, derived from the inbound POST body. In editor preview, synthetic sample columns. | none (HTTP listener) |
| `source` (generic) | none | Driven by the picked connector — file path / dataset / table. Same shape as the corresponding typed source. | none |

## 11.2 Sinks (1 input each)

| Node | Inputs | Output columns | Side effect |
|---|---|---|---|
| `csv_sink` | 1 (any cols, any types) | passthrough | Writes CSV to `file_path` (`COPY ... TO`) |
| `json_sink` | 1 | passthrough | Writes `array` or `lines` JSON |
| `excel_sink` | 1 | passthrough | Writes `.xlsx`; bolds header, auto-widths cols, optional header freeze |
| `parquet_sink` | 1 | passthrough | Writes Parquet to `file_path` |
| `db_sink` | 1 (column names must align with target table when `mode=append`/`upsert`) | passthrough | INSERT / TRUNCATE+INSERT / UPSERT to the named table |
| `s3_sink` | 1 | passthrough | Uploads format-encoded file to `bucket/key` |
| `kafka_sink` | 1 (`key_column` if set must exist) | passthrough | Publishes one JSON message per row |
| `api_sink` | 1 | passthrough | POSTs all rows (bulk) or one HTTP call per row (per_record) |
| `email_sink` | 1 | passthrough | Sends SMTP email with the relation attached as csv/json/parquet |
| `delta_sink` | 1 (schema compatible with existing Delta table for `mode=append`) | passthrough | Append or overwrite Delta table at `table_path` |
| `warehouse_sink` | 1 (cols align with target) | passthrough | Writes to Snowflake / BigQuery / Redshift; `merge` mode uses `merge_key` |
| `file_sink` | 1 | passthrough | Format detected from file extension; delegates to csv/json/parquet sink |
| Cloud-storage sinks (`sharepoint_sink`, `onedrive_sink`, `gdrive_sink`, `dropbox_sink`, `box_sink`, `adls_gen2_sink`, `azure_blob_sink`, `gcs_sink`) | 1 | passthrough | Format-encode the relation locally; upload to the vendor API |

## 11.3 Transforms (1 input each)

| Node | Inputs | Output columns | Notes |
|---|---|---|---|
| `filter` | 1 (cols referenced by `condition` or `rules` must exist) | passthrough, fewer rows | Rules-mode operators must match column types (`LIKE` only on text; `BETWEEN` only on ordered types) |
| `transform` | 1 (registered as `source_table` + `input`; all ancestors also registered by stepId + sanitized label) | Whatever the SELECT produces | Free-form SQL — output is whatever the SELECT projection is |
| `aggregate` | 1 (cols referenced in `group_by` + `functions` must exist) | `group_by` cols + one col per `functions` row (named by `alias` or auto). If `group_by` is empty, output is one row of global aggregates. | `HAVING` operates on aliases or aggregate exprs |
| `deduplicate` | 1 (`key` cols must exist) | passthrough, fewer rows | `order_by` ties-break which duplicate is kept (`keep_first` vs `keep_last`) |
| `flatten_explode` | 1 (`column` must exist; must be a STRUCT/MAP for `flatten` or a LIST for `explode`) | `flatten`: drops the source column, adds one col per nested field (prefixed if `prefix` set); `keep_original=true` retains the source col alongside. `explode`: cols other than `column` pass through + a single new col `{prefix}{column}` carrying the unnested element type, one row per array element (the original `column` is replaced by the unnested version) | `keep_original` applies to `flatten` mode only |
| `materialize` | 1 | passthrough | Persists upstream to a temp DuckDB table; downstream reads from the snapshot |
| `sort` | 1 (cols in `sort_by` must exist) | passthrough, reordered rows | `direction` applies to all sort cols uniformly |
| `rename` | 1 (cols in `mappings` keys must exist) | Renames per `mappings`; other cols pass through unchanged | Conflict (renaming A→B where B already exists) is a runtime error |
| `typecast` | 1 (cols in `casts` keys must exist; cast value must be coercible) | passthrough col-set, types swapped per `casts` | DuckDB type names — `INTEGER` / `BIGINT` / `DOUBLE` / `DECIMAL(p,s)` / `VARCHAR` / `BOOLEAN` / `DATE` / `TIMESTAMP` |
| `derived_column` | 1 (cols referenced by `expression` must exist) | passthrough + one col per entry in `columns` (`name`, type inferred from expression) | Expression evaluated against the source row |
| `pivot` | 1 (`pivot_column`, `value_column`, `group_by` must exist) | `group_by` cols + one col per distinct value in `pivot_column` (aggregated by `agg_function`) | Distinct values are read at runtime — schema depends on data |
| `unpivot` | 1 (`columns` to unpivot must exist) | Cols NOT in `columns` pass through + `name_column` (VARCHAR) + `value_column` (type of unioned source cols) | Source cols' types must be compatible |
| `window` | 1 (cols in `partition_by`/`order_by`/`window_functions[].column` must exist) | passthrough + one col per `window_functions` row (named by `alias`) | Window funcs see the optional `frame` spec |
| `sample` | 1 | passthrough, fewer rows | `count` rows (first or random) |
| `validate` | 1 (cols referenced in `rules` must exist) | passthrough + `is_valid` (BOOLEAN) | All rules must pass for `is_valid=true` |
| `conditional_split` | 1 (cols referenced in `conditions` must exist) | passthrough + `_split_output` (VARCHAR) marking the destination branch | `mode=filter` returns only rows where `_split_output == active_output` |
| `data_quality` | 1 (cols in rules must exist) | Depends on `mode`: `drop` = passing rows only, no new col; `fail` = raises before returning if any row fails; `tag` = passthrough + `__dq_passed` (BOOLEAN); `split` = passing rows only as the main output, **failing rows stashed in `ctx._dq_failures_<step_id>`** for a downstream sink to read | Optional `__dq_score` (DOUBLE 0–100) per row with `include_score=true` |
| `upsert` | 1 (`key_columns` must exist) | passthrough col-set, fewer rows (one row per unique key) | `order_by` determines which row's values win per key |
| `schema_mapper` | 1 (cols in `mappings[].source_column` must exist) | One col per `mappings` row (`target_column`, type from `target_type`) — input cols not in `mappings` are DROPPED | Explicit-only — no auto pass-through |
| `data_wrangler` | 1 | Result of fused sub-step SQL compiled from `steps` (filter / select / rename / cast / derive / group_by). Output schema = last sub-step's projection. | Steps marked `enabled: false` are skipped during compilation |
| `retry_handler` | 1 | passthrough | Pure metadata node; wraps upstream `execute()` with retry/backoff |

## 11.4 Combine (multi-input)

| Node | Inputs | Output columns | Notes |
|---|---|---|---|
| `join` | 2 (left + right; `join_key`/`key_pairs`/`custom_on` cols must exist on both) | INNER/LEFT/RIGHT/FULL: union of `select_left` ∪ `select_right` cols (empty = both sides' cols). SEMI: left cols only. ANTI: left cols only. CROSS: left + right cols. | Column-name collisions resolved by user via `select_left`/`select_right` aliases |
| `lookup` | 2 (main + reference; `lookup_key` must exist on both) | main cols + reference cols whose names don't collide. Missing-match rows have NULL for reference cols. | Equivalent to LEFT JOIN |
| `union` | 2..N | Column set must be identical across inputs (same names + compatible types). Output schema = first input's. `all` keeps duplicates; `distinct` drops them. | `by_name` mode (frontend variant) aligns columns by name across inputs |
| `scd2` | 1 (`incoming` required) + 1 optional (`current_target` snapshot) | `surrogate_key_column` (VARCHAR) + business_key cols + tracked cols + passthrough cols + `effective_from_column` + `effective_to_column` + `current_flag_column` (BOOLEAN) | `delete_detection=soft_close` (2026-05-18) closes orphan rows that disappeared from incoming |

## 11.5 Control Flow

| Node | Inputs | Output columns | Side effect |
|---|---|---|---|
| `if_condition` | 1 | passthrough, fewer rows (rows where `condition` is FALSE are dropped) | none |
| `switch_case` | 1 (`column` must exist) | passthrough, fewer rows — filtered to `active_case` value or `default_case` predicate | none |
| `foreach_loop` | 1 | passthrough + `_batch_index` (INT) + `_batch_total` (INT). When `batch_size=0`, single batch with index=0 total=1. | none — but downstream is invoked per batch |
| `until_loop` | 1 (`condition` must reference existing cols) | passthrough, rows where `condition` is satisfied are filtered out; loop terminates when empty or `max_iterations` reached | none |
| `wait_delay` | 1 | passthrough | Blocks for `seconds` |
| `set_variable` | 1 | passthrough + one col per `variables` entry (`name`, value from `expression`). Also writes to `ctx.vars[name]` for downstream access. | Mutates `ctx.vars` |
| `execute_pipeline` | 1 | Final relation of the sub-pipeline if available, else passthrough | Invokes sub-workflow with optional params (full arbitrary side effects) |
| `append_variable` | 0 or 1 | passthrough (if input present) | Mutates `ctx.vars[variable_name]` (array push) |
| `filter_array` | 0 or 1 | passthrough | Mutates `ctx.vars[output_variable]` (filtered array) |
| `validation` (Wait for File) | 0 or 1 | passthrough | Blocks until `path` exists / meets size / has children (or TimeoutError) |
| `fail` | 0 or 1 | (raises before returning) | Raises a pipeline failure with `message` + `error_code` |

## 11.6 Action

| Node | Inputs | Output columns | Side effect |
|---|---|---|---|
| `http_request` | 1 | `bulk` mode: response JSON keys merged into a single result row appended to input. `per_row` mode: input cols + response JSON keys per row. | External HTTP call(s) |
| `code_script` | 1 (`df` is the pandas DataFrame view) | Whatever the user's Python returns (must be a DataFrame). | Sandboxed Python execution |
| `send_email` | 1 (`{column}` placeholders in subject/body resolve against first row in bulk mode, per-row in per-row mode) | passthrough | Sends SMTP email |
| `slack_notify` | 1 (`{column}` placeholders resolve against first row) | passthrough | POSTs to Slack webhook |
| `copy_data` | 0 or 1 (depends on configuration shape — see §7 Copy Data) | Returns the written relation so downstream can chain off the copied data | Writes to `sink_connection` table (with optional staging + pre/post SQL) |
| `delete_data` | 1 (`condition` cols must exist for `target_kind=rows`) | `rows` mode: passthrough minus matching rows. `files` mode: one row per delete attempt with `path`, `status`, `error` cols. | `rows` is pure transform; `files` deletes from disk |
| `get_metadata` | 1 (`upstream` mode) or 0 (`file`/`directory` modes) | One row per column with `name`, `type`, `nullable`, `null_count`, `distinct_count`, `min_value`, `max_value`, `row_count` (cols controlled by `fields` + `include_*` flags) | none (`upstream`); file stat (`file`/`directory`) |
| `file_system` | 0 or 1 | One row per operation with `operation`, `source`, `destination`, `status`, `error` cols | Performs the filesystem op (copy/move/delete) |
| `execute_sql_task` | 0 or 1 (`{column}` placeholders resolve against first row) | `return_mode=rowcount`: single row `{rows_affected: INT}`. `return_mode=full`: the actual result set. | Executes arbitrary SQL on the chosen connection |

## 11.7 AI / Semantic

| Node | Inputs | Output columns | Side effect |
|---|---|---|---|
| `embedder` | 1 (`text_column` must exist; cast to VARCHAR if not) | passthrough + `output_column` (DOUBLE[]) of length `dim` (hash provider) or provider-default | External API call when provider ≠ `hash` |
| `llm_guardrail` | 1 (`text_column` must exist) | `tag` mode: passthrough + `__guardrail_flags` (VARCHAR — comma-separated detection types). `block` mode: passthrough, fewer rows. `mask` mode: passthrough but `text_column` content is replaced where matches found. | none (regex-only by default) |
| `semantic_router` | 1 (`text_column` must exist) | passthrough + `route` (VARCHAR — picked label or `default_label`) + `score` (DOUBLE — cosine similarity) | External API call when provider ≠ `hash` |

---

## 11.8 Cross-cutting I/O notes

- **Pass-through vs. mutation.** Every sink and most actions return their input unchanged so downstream nodes (and the PreviewPanel) keep showing data. The side-effect column tells you what runs outside that pure-data path.
- **Empty input behavior.** Single-input transforms with `_settings.always_output=true` emit a 0-row relation matching the expected output schema; with `always_output=false` (default) the node returns `None` and the run skips that subtree.
- **Type inference.** Sources read from typed backends (Parquet, DB, Delta) preserve native types. Untyped sources (CSV, JSON, XML) hand types to DuckDB's inference; downstream nodes can use `typecast` to coerce.
- **Metadata columns.** Three nodes intentionally inject metadata columns into the output:
  - `foreach_loop` → `_batch_index`, `_batch_total`
  - `conditional_split` → `_split_output`
  - `data_quality` (tag/split modes) → `__dq_passed`, optionally `__dq_score`
  - `llm_guardrail` (tag mode) → `__guardrail_flags`
  - `validate` → `is_valid`
  - Kafka source → `_kafka_offset`, `_kafka_partition`, `_kafka_topic`

  Underscore-prefixed columns are reserved — downstream SELECTs that `*`-pick will carry them through unless explicitly stripped.
- **Multi-input semantics.** `join`/`lookup` use input order to assign left vs right; `union` accepts unbounded inputs (variadic); `scd2` uses `inputs[0]` as incoming and `inputs[1]` as current snapshot if present. The order is captured in `_input_step_ids` (params), which is populated from the edge order at execution time.
- **Side-effect badge.** The amber ⚠ on a canvas node (from B5) means the node has an external side effect — preview / retry / resume may have real-world consequences. Side-effect classification lives in `SIDE_EFFECT_NODES` in `frontend/src/utils/nodeArity.ts`.

---

# Addendum 2 — 2026-05-18 contract hardening (review-pass #2)

Three reviewers (product-UX, engineering-clarity, runtime-semantics) converged on the same direction: the relational runtime contract is strong, but the surface area around metadata, schema stability, and side-effect classification needs more explicit policy so the executor / validator / UI / docs can't drift. The following items landed:

## A1 — Reserved-metadata-column registry

Single canonical list of system-injected columns lives at `frontend/src/utils/nodeArity.ts` in `RESERVED_METADATA_COLUMNS`:

| Column | Injected by | Type | Meaning |
|---|---|---|---|
| `_batch_index` | `foreach_loop` | INT | 0-based index of current batch |
| `_batch_total` | `foreach_loop` | INT | Total batches |
| `_split_output` | `conditional_split` | VARCHAR | Branch label this row was routed to |
| `__dq_passed` | `data_quality` (tag/split) | BOOLEAN | Whether the row passed all rules |
| `__dq_score` | `data_quality` (with `include_score`) | DOUBLE | Per-row pass rate 0–100 |
| `__guardrail_flags` | `llm_guardrail` (tag mode) | VARCHAR | Comma-separated detection types |
| `is_valid` | `validate` | BOOLEAN | All rules passed |
| `_kafka_offset` | `kafka_source` | BIGINT | Source partition offset |
| `_kafka_partition` | `kafka_source` | INT | Source partition number |
| `_kafka_topic` | `kafka_source` | VARCHAR | Source topic name |

Helper functions `isReservedMetadataColumn(col)` and `reservedMetadataIn(cols)` available for any consumer that needs to detect or strip these. Underscore-prefix is reserved at the policy level for future expansion.

## A2 — 3-class side-effect classification

The binary `SIDE_EFFECT_NODES` flag is now layered with `SideEffectClass = 'passthrough' | 'transforming' | 'terminal'` (via `sideEffectClassFor(stepType)` in `nodeArity.ts`):

| Class | Behavior | Examples |
|---|---|---|
| `passthrough` | Input relation returned unchanged; side effect runs in parallel. Safe to chain downstream off the "output" because it IS the input. | All sinks: `csv_sink`, `db_sink`, `kafka_sink`, `email_sink`, … |
| `transforming` | Returns a NEW relation that reflects the side effect's result (response cols, written rows, per-file results, per-column stats). | `http_request`, `copy_data`, `delete_data`, `get_metadata`, `execute_sql_task`, `file_system`, `execute_pipeline` |
| `terminal` | Returns a small descriptive relation but the dataflow is effectively done — downstream can't meaningfully continue the original data chain. | `send_email`, `slack_notify`, `fail` |

This replaces R2's complaint that "all sinks return input unchanged" was loose: action nodes that emit enriched relations are no longer lumped with sinks.

## A3 — Schema-stability annotation

Three tiers via `schemaStabilityFor(stepType) → 'static' | 'inference' | 'dynamic'`:

- **`static`** — output schema is deterministic from params alone. Cache indefinitely. Most transforms (rename / select / typecast / derived_column / filter / sort / dedup / schema_mapper / aggregate / window / sample / validate / conditional_split / lookup / union / scd2 / data_quality / upsert / wrangler).
- **`inference`** — DuckDB-inferred from a sample at first read. Safe to cache after first run; may shift if upstream data shape changes (CSV column type drift across uploads). All file/network sources: csv_source / json_source / xml_source / excel_source / gsheet_source / ftp_source / s3_source / db_source / parquet_source / delta_source / kafka_source.
- **`dynamic`** — output schema is fully data-dependent and only known at runtime. Column dropdowns downstream MUST either rebuild after every run OR fall back to free-text input. Currently: `pivot`, `unpivot`, `transform`, `api_source`, `webhook_trigger`, `code_script`, `flatten_explode`.

## A4 — Dynamic-schema downstream UI banner

`useUpstreamSchema` now returns `{ dynamic: boolean, dynamicSources: string[] }`. `ConfigPanel` renders an amber banner above the config body when ANY ancestor is dynamic, listing the dynamic ancestors by label and explaining that column dropdowns are best-effort until the next run completes. Single point of integration — every config that uses upstream columns picks this up automatically.

## A5 — Loop / split metadata leaking into a sink (validator warning)

`validateWorkflow.ts` walks ancestors of every pass-through sink. If ANY ancestor is in `METADATA_PRODUCERS` (`foreach_loop`, `conditional_split`, `data_quality`, `validate`, `llm_guardrail`, `kafka_source`, `set_variable`), it emits a warning telling the user to drop the metadata columns via a `rename` or `select` step before the sink — otherwise engine-internal columns land in their output table.

## A6 — Sample-run side-effect guard

The Canvas footer "Sample" button now scans the pipeline for any node with `sideEffectClassFor(t) !== null` BEFORE invoking `runWorkflow(false)`. If side-effect nodes exist, a danger-styled confirmation dialog lists each affected node with its humanized description ("Posts to a Slack channel", "Calls an external API", "Writes a CSV file") and offers Run-anyway / Cancel. Users can deactivate side-effect nodes from the context menu first if they only want to preview the data path.

## A7 — `'skipped'` node status

`FPulseNode` gained a third terminal status (`'skipped'`) rendered as a slate-grey ⏵⏵ chevron-pair badge, visually distinct from green ✓ (success) and red ✗ (error). Triggered when an upstream returned 0 rows and `always_output=false`, OR when the A6 guard elides a side-effect node in sample mode. Tooltip explains why.

## A8 — Humanized side-effect badge tooltips

`sideEffectLabel(stepType)` returns per-action text. The amber ⚠ badge tooltip in `FPulseNode` no longer reads generic "side-effect node — preview/retry may have consequences"; it now says specifically what each node does:

- `csv_sink` → "Writes a CSV file"
- `db_sink` → "Writes rows to a database"
- `kafka_sink` → "Publishes messages to Kafka"
- `email_sink` / `send_email` → "Sends an email…"
- `slack_notify` → "Posts to a Slack channel"
- `http_request` → "Calls an external API"
- `delete_data` → "Deletes rows or files"
- `execute_sql_task` → "Runs SQL on a database"
- `execute_pipeline` → "Runs another pipeline"
- (one per side-effect stepType — full map in `SIDE_EFFECT_LABEL`)

## B4 — `wait_delay` re-tagged advanced

Per R1's "hide pure runtime nodes from primary palette" — `wait_delay` moved from `intermediate` → `advanced` level. Now hidden from Basic + Standard palette; visible in All only.

## C1 — User-intent palette grouping (Basic + Standard)

The Basic + Standard palette levels now group nodes by user intent (Import / Prepare / Analyze / Automate / Publish) instead of engine semantics. All-mode keeps the original engine categories (Data Movement / Transform / Combine / Control Flow / Action / AI) for power users who think in primitives.

Intent classification (full map in `INTENT_FOR_STEP_TYPE` in `modulesPanelData.ts`):

| Intent | What | Stepst types |
|---|---|---|
| **Import** | Read data in | sources + `webhook_trigger` |
| **Prepare** | Clean / shape / validate | `data_wrangler`, `filter`, `derived_column`, `rename`, `typecast`, `schema_mapper`, `sort`, `deduplicate`, `sample`, `data_quality`, `flatten_explode`, `upsert`, `validate`, `llm_guardrail` |
| **Analyze** | Combine / aggregate / classify | `transform`, `join`, `lookup`, `union`, `aggregate`, `pivot`, `unpivot`, `window`, `embedder`, `semantic_router`, `conditional_split`, `scd2` |
| **Automate** | Calls / messages / control flow | `http_request`, `send_email`, `slack_notify`, `code_script`, `copy_data`, `execute_pipeline`, `execute_sql_task`, `file_system`, `delete_data`, `get_metadata`, control-flow nodes |
| **Publish** | Write data out | all sinks + `output` / `destination` |

`activeCategory` resets to "Import" when the user switches to Basic/Standard, "Data Movement" when they switch to All — so the first-render accordion always shows something open.

## C2 — Edge schema-delta storytelling

`CustomEdge` now annotates each edge with row-count delta + schema delta when BOTH endpoints have run:

```
1,234 rows · −245 · +1 col · ~1 type
```

- Row delta colored by direction (green for +, red for −)
- `+N col` (emerald) = columns added downstream
- `−N col` (red) = columns dropped
- `~N type` (amber) = type cast count
- Hover any chip for the exact column list

Reads from existing `stepResults` (no new state). Renders only when both source and target have completed a run.

## C3 — Per-node schema-delta chip (opt-in)

New `showSchemaDeltas` preference in `EditorPreferences` (default OFF — opt-in via Settings → General once the toggle is wired into SettingsPage). When enabled, each canvas node renders a small "+N/~N/−N" chip beneath the header showing how it changed its upstream's schema. Hidden for multi-input nodes (join/union/scd2) and for nodes that haven't run.

## SCD2 doc clarity

The `scd2.py` docstring gained an explicit IMPORTANT note that SCD2 is RELATION-PRODUCING, not write-performing. It does NOT issue UPDATE / DELETE against an external table. All change tracking is computed in-memory; the sink is solely responsible for committing the new state via truncate-and-replace OR merge keyed by `surrogate_key_column`. Reviewer 3's "deletion leak" concern was a misreading — the close-out rows are rendered into the emitted relation.

---

## File index updates (round 2)

| Concern | File |
|---|---|
| 3-class side-effect taxonomy | `frontend/src/utils/nodeArity.ts` (`SideEffectClass`, `sideEffectClassFor`) |
| Humanized side-effect labels | `frontend/src/utils/nodeArity.ts` (`sideEffectLabel`) |
| Schema-stability annotation | `frontend/src/utils/nodeArity.ts` (`SchemaStability`, `schemaStabilityFor`) |
| Reserved-metadata-column registry | `frontend/src/utils/nodeArity.ts` (`RESERVED_METADATA_COLUMNS`, `isReservedMetadataColumn`) |
| Dynamic-schema downstream signal | `frontend/src/hooks/useUpstreamSchema.ts` (`UpstreamSchemaResult.dynamic` / `dynamicSources`) |
| Dynamic-schema UI banner | `frontend/src/components/ConfigPanel.tsx` (banner above config body) |
| Loop-metadata leak warning | `frontend/src/utils/validateWorkflow.ts` |
| Sample-run side-effect guard | `frontend/src/components/Canvas.tsx` (Sample button onClick) |
| `'skipped'` status badge | `frontend/src/components/nodes/FPulseNode.tsx` |
| User-intent regrouping | `frontend/src/components/modulesPanelData.ts` (`INTENT_FOR_STEP_TYPE`, `regroupByIntent`) |
| Edge schema-delta badges | `frontend/src/components/nodes/CustomEdge.tsx` |
| Per-node delta chip (opt-in pref) | `frontend/src/components/nodes/FPulseNode.tsx` + `useEditorPreferences.ts` (`showSchemaDeltas`) |

---

# Part 12 — Frontend per-node field reference + UX audit

**Per-node field detail** — every field with type / default / placeholder / required / show-when conditions — is defined in the frontend config components themselves; read those when validating an individual node.

This Part 12 is the **summary + action list** — the cross-cutting findings that span multiple nodes and the prioritized fix-it backlog.

## 12.1 What's hardcoded vs. schema-driven

- **Hardcoded** components in `frontend/src/components/ConfigPanel.tsx` (~3000 lines, ~30 components) handle every user-visible step type in the palette. The dispatcher block (`{stepType === 'X' && <XConfig />}`) runs from line ~820 onward.
- **DynamicConfig** at `frontend/src/components/DynamicConfig.tsx` is the **graceful-degradation surface** for legacy / experimental / hidden node types whose backend `param_schema` exists but whose frontend component hasn't been added. No user-visible OSS palette item currently falls into this path.
- The **universal Settings tab** (every node, 11 fields) is defined inline in ConfigPanel.tsx (~line 633-776) — see Part 1.10 of this doc.
- The **Data Wrangler workspace** is the only full-bleed config (`frontend/src/components/data-wrangler/DataWranglerConfig.tsx`) and owns its own 3-pane layout instead of using the standard Parameters/Settings tab structure (Settings is still available as a tab — restored 2026-05-18).

## 12.2 22 UX flags consolidated

Pulled from the raw audit, ranked by user impact. Top 6 are concrete bugs; the rest are design polish.

### Tier 1 — Real bugs (silent data loss, broken auth, leaked credentials)

| # | Where | Issue | Why it matters |
|---|---|---|---|
| **1** | `api_source`, `api_sink`, `s3_source/sink`, `kafka_source`, `ftp_source`, `email_sink`, `send_email` (no-connection branch), `http_request` | Password / Token / Secret / Key fields use plain `<input type="text">` — credentials visible on screen, captured in screenshots, copy-pasted into clipboard history. Only `SaaSConnectorConfig` honors a `secret` flag. | Security regression. Trivial fix: swap `type="text"` for `type="password"` and add an eye-toggle. |
| **2** | `api_sink` Auth Type dropdown | `oauth2` is selectable but renders **no follow-up fields** — user picks OAuth and ends up with broken auth silently. | Pipeline appears configured but 401s at runtime. |
| **3** | `webhook_trigger` Authentication dropdown | `basic_auth` selectable, **no Username/Password fields render** — same as #2. | Same as #2. |
| **4** | `validate` node (rule `in_range`, `regex`, `min_length`) | When the check requires a value, **no value field appears at all**. Rule schema is incomplete on the frontend. | User can't actually configure these checks. |
| **5** | `email_sink`, `send_email`, `slack_notify`, `ftp_source`, `kafka_source`, `warehouse_sink` | Both a **Connection picker** AND inline credential fields render. If both are filled, **no UI indicates which wins**. | Silent precedence bugs; hard to debug. |
| **6** | `code_script` sandbox | Static regex + restricted namespace only. (Already documented as Bucket D14 hardening path — 3 phases planned.) | Already triaged in [Addendum — Bucket D14](#bucket-d14--code_script-sandbox-hardening-path). |

### Tier 2 — Required-marker drift (`*` mark inconsistency)

Several fields that **fail at runtime when blank** carry no `*` marker. From the raw audit:

- `transform` SQL Expression
- `filter` Condition (rules-mode only)
- `derived_column` Column Name + Expression (both required)
- `csv_source` Data File (mandatory, no `*`)
- `join` keys/clause when `key_mode` switches (the conditional required fields)
- `db_source` Schema/Table — no visual disabling when connection is empty

**Fix pattern:** every component that uses `<Field label="X">` should append ` *` to the label string when the field is functionally required. The existing components mix the two styles inconsistently.

### Tier 3 — Three retry surfaces, no precedence

Retry handling is configured in **three places**:
- Universal **Settings tab** → Retry On Fail / Max Retries / Retry Delay / Retry Strategy
- **`retry_handler` node** → Max Retries / Initial Delay / Backoff Multiplier / On Exhausted
- **Per-node retry selects** on `api_source`, `api_sink`, `http_request`, `webhook_sink` (Retry Count / Max Retries)

All three default to **3**, but they have different field names, different backoff models, and no documented interaction. The runtime executor honors one of them per call site — no UI hint tells the user which.

### Tier 4 — Duplicate / colliding concepts

- `validate` (column rule transform) and `validation` (Wait-for-File control node) — confusingly similar names; users picking one frequently mean the other.
- `output` (legacy node) is NOT in `HARDCODED_CONFIGS` but the `OutputConfig` function exists — orphaned fallback path; either retire or wire it.
- `copy_data` has both Source/Sink tabs AND a Mapping tab; the Mapping tab's `mapping_mode` is the **same param** as the SaaS sink's Field Mapping Mode — they silently overwrite each other.
- Per-node `On Error` (used by `api_sink`, `email_sink`, `slack_notify`, `foreach_loop`) is a **separate setting** from the universal Settings tab's `On Error`. Two selects can disagree.
- HTTP auth-token param is named `auth_token` in `http_request`, `bearer_token` in `api_source` / `api_sink` — same concept, three names.

### Tier 5 — Missing affordances on transforms

- `sort` — no column-picker chips; user types `amount DESC, name ASC` as free-form text even when upstream columns are known. Inconsistent with other transforms.
- `typecast` — JSON-only editor; no structured column→type editor even though upstream columns are known. Compare with Schema Mapper or Data Wrangler Cast which both offer structured editors.
- `lookup` — no connection picker, no file picker; user types table/file name as free text.
- `data_wrangler.select` sub-step — comma-separated text input only; no column picker even though upstream columns are known to the workspace.
- `delta_source` — no connection picker; requires inline `storage_options` JSON instead of reusing saved S3/Azure connections.
- `flatten_explode.column` — no dropdown to pick the nested column from upstream schema.

### Tier 6 — Conditional rendering quirks

- `ftp_source` SSH Private Key field shows even when Protocol is `ftp`/`ftps` (only used for `sftp`).
- `embedder` Hash Dimension shows always but is only meaningful when Provider is `hash`.
- `sample` — both Count and Fraction are optional; if both are blank, the node has no effect and no validation warning fires.
- `db_source` no-connection state shows an info banner but doesn't visually disable Schema / Table inputs.

### Tier 7 — Empty states / discoverability

- `saas_connector` shows grey italic "No SaaS manifests loaded on the backend." with no link/help/CTA when zero manifests exist.
- `source` and `destination` (generic): picking a connection re-renders the panel with another node's full config stacked inline — two levels of "what node am I in?"
- `validate` rule editor — when picking a check, the value input doesn't auto-focus.

## 12.3 Recommended prioritized fixes (~14 hours total)

| Priority | Item | Effort |
|---|---|---|
| **P0** | Tier 1 #1: swap `type="text"` → `type="password"` on all credential fields (~8 components, one-line per field) | 1 hour |
| **P0** | Tier 1 #2, #3, #4: either render the missing auth/value fields, or remove the broken dropdown options | 2 hours |
| **P1** | Tier 1 #5: when both Connection picker AND inline credentials are filled, hide the inline ones (or show "Inline overrides connection") | 1 hour |
| **P1** | Tier 2: add `*` markers to every label whose backing field is required (`transform.expression`, `derived_column.name`/`expression`, `csv_source.file_path`, etc.) | 1 hour |
| **P1** | Tier 3: pick a single source-of-truth for retry config — recommend the universal Settings tab; remove the duplicate per-node retry selects on `api_*` / `http_request` / `webhook_sink`; keep `retry_handler` node for the override-this-step pattern, document precedence in its tooltip | 2 hours |
| **P2** | Tier 4: rename `validation` → `wait_for_file` to disambiguate from `validate`; retire orphaned `OutputConfig`; normalize auth-token param name across all HTTP-ish nodes | 2 hours |
| **P2** | Tier 5: wire column-picker chips into `sort`, `typecast`, `lookup`, `flatten_explode.column`, `data_wrangler.select` | 3 hours |
| **P2** | Tier 6: hide the conditional cruft (`ftp_source` SSH key under sftp only, `embedder` Hash Dimension under hash only) | 1 hour |
| **P3** | Tier 7: add empty-state CTAs to `saas_connector`, `validate` rule editor | 1 hour |

## 12.4 Per-node field reference — see companion

The full per-node field detail (every field, every default, every show-when condition) lives in the frontend config components. They cover:

```
Universal Settings Tab (every node)
Sources (csv_source, db_source, api_source, json_source, parquet_source,
         excel_source, xml_source, s3_source, kafka_source, ftp_source,
         gsheet_source, delta_source, adls_gen2_source, azure_blob_source,
         gcs_source, file_source, sharepoint_source, onedrive_source,
         gdrive_source, dropbox_source, box_source)
Sinks   (csv_sink, json_sink, excel_sink, s3_sink, kafka_sink, api_sink,
         webhook_sink, email_sink, delta_sink, warehouse_sink, db_sink,
         output, plus the *_sink variants of cloud-storage connectors)
Transforms (transform, filter, deduplicate, aggregate, sort, rename, typecast,
            derived_column, lookup, pivot, unpivot, validate, sample,
            conditional_split, flatten_explode, schema_mapper, data_quality,
            upsert, materialize)
Combine (join, union, window)
Control Flow (if_condition, switch_case, foreach_loop, until_loop, wait_delay,
              set_variable, execute_pipeline, retry_handler, fail,
              append_variable, filter_array, validation)
Action (http_request, webhook_trigger, code_script, copy_data, delete_data,
        get_metadata, send_email, slack_notify, file_system, execute_sql_task)
Generic Source / Destination
SaaS Connector
AI / Semantic (embedder, llm_guardrail, semantic_router)
Data Wrangler Workspace (filter / select / rename / cast / derive / group_by sub-steps)
Schema-driven (DynamicConfig) — graceful-degradation surface, no current OSS palette items
```

Each entry: tab layout, field count, full field table, optional UX flag at the bottom.

---

## File index updates (round 3)

| Concern | File |
|---|---|
| UX-flag triage + prioritized fix list | This doc, Part 12 |

---

# Addendum 3 — 2026-05-18 Tier-1 & Tier-2 UX fixes (review-pass #3)

Four external reviews of Part 12 converged on the same priorities. All Tier-1 (P0) and Tier-2 (P1) items shipped this session. Tier-2+ refactors documented as P2 deferred work.

## Shipped this round (8 items)

| # | Item | Effort | Surface |
|---|---|---|---|
| **P0-1** | `<SecretInput>` primitive + 18 credential sites swapped (passwords, tokens, secret keys, webhook URLs) | 30 min | ConfigPanel.tsx |
| **P0-2** | Removed `oauth2` from api_sink Auth Type + `basic_auth` from webhook_trigger Auth (both were selectable but rendered no follow-up fields → false-positive saved config) | 5 min | ConfigPanel.tsx |
| **P0-3** | Connection precedence — `email_sink` + `slack_notify` now hide inline credentials when a Connection is picked, with a banner explaining where to edit them | 20 min | ConfigPanel.tsx |
| **P0-4** | Removed 5 redundant per-node retry surfaces (`api_source`, `api_sink`, `webhook_sink`, `http_request`). Two retry tiers remain: Universal Settings tab (default) + `retry_handler` node (Advanced palette only — explicit override) | 20 min | ConfigPanel.tsx |
| **P0-5** | `*` markers added: `derived_column.{name,expression}`, `csv_source.file_path`, `filter.condition`, `transform.expression`, `join.{join_key,mapped_keys,custom_on}` per key_mode | 15 min | ConfigPanel.tsx |
| **P1-6** | `ValidateConfig` now renders a value input below the column/check row when the check requires one (`in_range`, `regex`, `min_length`, `max_length`, comparator ops). Added `max_length` to the check picker too. | 15 min | ConfigPanel.tsx |
| **P1-7** | `switchAuthType()` state-sanitation helper — when user changes `auth_type`, ALL auth keys (`bearer_token`, `username`, `password`, `api_key_*`, `auth_token`, `auth_user`, `auth_pass`) get `undefined`'d in the same set call. Prevents the dirty-payload bug Review 2 flagged. Wired into all 3 auth_type selects. | 15 min | ConfigPanel.tsx |
| **P1-8** | Removed both `Field Mapping Mode` selectors from `copy_data` Sink-tab SaaS branches. Mapping tab is now the single source of truth for `mapping_mode`; no more silent overwrites. | 10 min | ConfigPanel.tsx |

## Deferred to P2 (documented for next planning cycle)

| Item | Why deferred | Effort |
|---|---|---|
| **P2-A — Node Form SDK** (composable field primitives: `<AuthSection>`, `<MappingRepeater>`, `<KeyValueRepeater>`, `<SqlEditor>`, `<ColumnMultiSelect>`) | Multi-day refactor across 30 components; needs design alignment on API surface | ~1 week + 1-2 weeks migration |
| **P2-B — Move ad-hoc transforms INTO Data Wrangler** (`filter`, `sort`, `rename`, `typecast`, `derived_column`, `deduplicate`, `sample`, `flatten_explode` as sub-steps instead of canvas nodes) | Strategic product call; tightens palette by 8 items but breaks user muscle memory; migration story needed for existing pipelines | ~2 weeks |
| **P2-C — Schema-aware Data Wrangler sub-steps** (column-picker chips, propagate sub-step schema diffs forward, warn on broken refs to dropped columns) | Bounded but needs careful state management; B7 already handles the disabled-step case but not active-step propagation | ~3-5 days |
| **P2-D — Replace remaining raw JSON textareas** (`typecast` column→type, `headers` on API/HTTP nodes, `storage_options` on delta, `namespaces` on xml_source, fallback `rename` JSON) | Best bundled with P2-A so structured editors come from the Form SDK | ~2-3 days as part of P2-A |
| **P2-E — Canonical field vocabulary** (`auth.type` / `auth.bearer_token` / `auth.password` namespace instead of inconsistent `token`/`auth_token`/`bearer_token`) | Needs migration shim for existing saved workflows that use legacy names | ~1-2 days |

## Connection-precedence remainders

The P0-3 fix covered `email_sink` and `slack_notify` (the worst-flagged). Three more nodes still show both Connection picker + inline credentials simultaneously:

- `ftp_source` — Host/Port/Username/Password are always shown
- `kafka_source` — Bootstrap Servers + SASL credentials always shown
- `warehouse_sink` — Connection String + Credentials JSON always shown

These have more complex layouts (some inline fields are NOT credentials and should stay visible). Slated for the same pass as P2-A when the `<AuthSection>` primitive is extracted.

---

## File index updates (round 4)

| Concern | File |
|---|---|
| `<SecretInput>` primitive | `frontend/src/components/ConfigPanel.tsx` (function near top, after `TextInput`) |
| `switchAuthType()` state-sanitation helper | `frontend/src/components/ConfigPanel.tsx` (above `SecretInput`) |
| Per-fix code locations | Each marked with `/* P0-X (2026-05-18): ... */` comments in `ConfigPanel.tsx` |

---

# Addendum 4 — 2026-05-18 Continuation pass

Three follow-on items from Addendum 3's deferred backlog landed in this session.

## P0-3 finisher — connection precedence on the remaining 3 nodes

`ftp_source`, `kafka_source`, and `warehouse_sink` had the "Connection picker + inline credentials both visible" problem. Each had its own layout subtleties — non-credential inline fields that should stay visible — so they couldn't be batch-fixed in Addendum 3. Now gated:

| Node | When connection set, hidden | Always visible |
|---|---|---|
| `ftp_source` | Host, Port, Username, Password, SSH Private Key | Remote Path, Protocol, File Format |
| `kafka_source` | Bootstrap Servers, Security Protocol, SASL Mechanism, SASL Username, SASL Password | Topic, Operation, Consumer Group, Offset config, Max Messages, Value Deserializer |
| `warehouse_sink` | Warehouse Type, Connection String / DSN, Credentials JSON | Schema, Table, Write Mode, Primary Keys, Batch Size, Pre-SQL, Post-SQL, Column Mapping |

Each shows the same explanatory banner pattern as Addendum 3's `email_sink`:
> _"Using bootstrap servers + SASL credentials from this connection. Topic + consumer-side knobs below still apply per-pipeline."_

Also fixed the `ftp_source` SSH Private Key tier-6 issue at the same time — it's now only visible when **Protocol = `sftp`** (was always visible; ignored for ftp/ftps).

## P2-C — Schema-aware Data Wrangler sub-steps

Three of the six sub-step forms (`select`, `rename`, `cast`) now consume the workspace's `incomingColumns` array. The Data Wrangler workspace already passes it to `<IncomingColumnsStrip>` above each step's form — extending the same prop to the per-op forms turned out cheap.

| Sub-step | Before | After |
|---|---|---|
| `select` | Comma-separated text input ("customer_id, order_date, amount") | Column-chip multi-select with **Select all** / **Clear** buttons; falls back to text mode when no upstream columns are known yet (pre first-preview). Shows "N of M columns selected" footer. |
| `rename` | Two free-text inputs per row | Left input now has a `<datalist>` of upstream columns for autocomplete; right stays free-text (it's a new name) |
| `cast` | Free-text "column" input | `<select>` populated from upstream columns with type hint (`amount (DOUBLE)`); falls back to text input when no columns are known |
| `filter` | SQL textarea | unchanged — the `IncomingColumnsStrip` above already lets users click-to-copy column names |
| `derive` | Two free-text inputs (name + expression) | unchanged — name is a NEW column so no picker applies; expression stays free-form SQL |
| `group_by` | Comma-separated keys + aggregations repeater | unchanged for v1 — keys are comma-separated text, aggregations have function + alias inputs only |

`derive` and `group_by` are intentionally left for the next pass — they're more complex (expressions need column chips below the textarea like Transform; group_by aggregations need a column dropdown per row). Tracked as residual P2-C work.

## P2-E — Canonical auth-field vocabulary (foundation)

New file: [`frontend/src/utils/authVocabulary.ts`](../frontend/src/utils/authVocabulary.ts).

Single canonical name per auth concept:

| Canonical (nested) | Legacy names absorbed |
|---|---|
| `auth.type` | `auth_type` |
| `auth.bearer_token` | `bearer_token`, `auth_token`, `token` |
| `auth.username` | `username`, `auth_user`, `smtp_user` |
| `auth.password` | `password`, `auth_pass`, `smtp_pass`, `sasl_password` |
| `auth.api_key_header` | `api_key_header` |
| `auth.api_key_value` | `api_key_value`, `api_key` |

Two idempotent migration shims:
- `normalizeOnLoad(params)` — flat legacy → nested canonical. Strips the absorbed legacy keys so the IR stays clean. Infers `auth.type` from which credential field is populated when missing.
- `denormalizeForBackend(params)` — nested canonical → flat legacy. Called at save time so the backend executor (which still reads the old names) keeps working unchanged. Zero-impact wire-format migration.

Plus `isCanonical(params)` and `summarizeAuth(auth)` helpers for dev tools.

**Status: foundation-only.** No call sites consume it yet. The value lands when the P2-A `<AuthSection />` primitive ships and switches to the canonical shape, OR when a future migration pass normalizes the existing nodes one at a time. Both `normalizeOnLoad` and `denormalizeForBackend` are idempotent, so they can be inserted into `loadWorkflow` / save paths without breaking anything currently working.

## Still deferred

| Item | Why | Effort |
|---|---|---|
| **P2-A — Form SDK** (`<AuthSection>`, `<MappingRepeater>`, `<KeyValueRepeater>`, `<SqlEditor>`, `<ColumnMultiSelect>` extracted as reusable primitives) | Genuine 1–3 week refactor across 30 components; needs design alignment on the API surface | ~1 week + 1–2 weeks migration |
| **P2-B — Move ad-hoc transforms INTO Data Wrangler** (`sort`, `dedupe`, `sample`, `flatten` as new sub-step types; retire from canvas palette) | 2-week UX direction shift; affects user muscle memory; needs migration for existing pipelines that chain these as canvas nodes | ~2 weeks |
| **P2-C residual** — Data Wrangler `derive` expression column chips + `group_by` aggregation column dropdowns | Bounded but better done together with a small Form-SDK extraction | ~1 day |
| **P2-D — Raw JSON textareas replaced with structured editors** (typecast standalone node, headers, storage_options, namespaces) | Best bundled with P2-A so the structured editors come from the Form SDK | ~2–3 days as part of P2-A |

---

## File index updates (round 5)

| Concern | File |
|---|---|
| Canonical auth-field vocabulary + migration shims | `frontend/src/utils/authVocabulary.ts` |
| Sub-step column awareness | `frontend/src/components/data-wrangler/DataWranglerConfig.tsx` (`SelectForm`, `RenameForm`, `CastForm` now accept `incomingColumns`) |
| Connection precedence (ftp/kafka/warehouse) | `frontend/src/components/ConfigPanel.tsx` (each marked with `/* P0-3 finisher */`) |

---

# Addendum 5 — 2026-05-18 Form-SDK starter + Wrangler expansion

The previously-deferred P2 items shipped this pass.

## P2-C residual — Wrangler `derive` + `group_by` now schema-aware

- **`DeriveForm`** — beneath each expression input, renders clickable upstream column chips (truncated to 12 + "+N more"). Clicking a chip inserts the column name at the current cursor position (preserves selection). Big DX upgrade — `split_part(email, '@', 2)` builds without retyping.
- **`GroupByForm`** — keys are now a column-chip multi-select when upstream columns are known (falls back to comma-separated text). Aggregation rows have a column `<select>` (with `*` for COUNT) instead of free text.

All six Wrangler sub-step forms (`filter`, `select`, `rename`, `cast`, `derive`, `group_by`) are now schema-aware.

## P2-D — Raw JSON textareas replaced with structured editors

New shared primitive `<KeyValueRepeater>` (drop-in replacement for "edit this JSON object as text" textareas). Renders Key | Value rows with add/remove buttons; round-trips to `Record<string, string>`. Pass `secretValues={true}` to mask values via `<SecretInput>` (used for storage_options).

Applied:

| Site | Before | After |
|---|---|---|
| `api_source` Headers | JSON textarea with `{"Accept": "application/json"}` placeholder | `<KeyValueRepeater>` |
| `api_sink` Headers | JSON textarea | `<KeyValueRepeater>` |
| `webhook_sink` Headers | JSON textarea | `<KeyValueRepeater>` |
| `xml_source` Namespaces | JSON textarea with `{"ns": "http://example.com/ns"}` | `<KeyValueRepeater>` (prefix/URI pairs) |
| `delta_source` Storage Options | JSON textarea with credentials in plain text | `<KeyValueRepeater secretValues={true}>` |
| `delta_sink` Storage Options | same | same |
| `typecast` (standalone node) | JSON textarea `{"amount": "DOUBLE", "id": "INTEGER"}` | structured column→type table with column dropdown + type select per row (matches Data Wrangler's `cast` sub-step) |

Webhook_sink's `secret` field also swapped TextInput → SecretInput (was missed in the P0-1 sweep).

## P2-A — `<AuthSection>` primitive + migration

New primitive `<AuthSection params onChange excludeTypes?>`:

- Renders Auth Type select + matching credential fields (Bearer / Username+Password / API Key Header+Value)
- Uses `switchAuthType()` for state sanitation (stale credential keys nulled on switch)
- Uses `<SecretInput>` for all credential masking
- Single source of truth — replaces ~25 lines of conditional inline-auth rendering per consumer

Migrated:

- **`api_source`** — inline auth block (lines 3947-3966 → 1 line)
- **`api_sink`** — inline auth block (lines 5705-5728 → 1 line)

Not yet migrated:

- **`http_request`** — uses non-canonical param names (`auth_token` / `auth_user` / `auth_pass` instead of `bearer_token` / `username` / `password`). Needs a key-translation wrapper. Tracked as residual.

## P2-B — `sort` / `dedupe` / `sample` / `flatten` as Wrangler sub-step types

The Wrangler now hosts 10 sub-step types (up from 6). Four new ops:

| Op | Backend SQL | Frontend form |
|---|---|---|
| `sort` | `SELECT * FROM (…) ORDER BY col1 ASC, col2 ASC` | Column-chip multi-select + Direction picker (ASC/DESC, applies to all) |
| `dedupe` | `SELECT * EXCLUDE (__dedup_rn) FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY key ORDER BY 1 {ASC\|DESC}) AS __dedup_rn FROM …) WHERE __dedup_rn = 1` | Column-chip key multi-select + Strategy (keep_first / keep_last) |
| `sample` | `LIMIT N` (first) or `USING SAMPLE N ROWS` (random) | Method select (first/random) + count number input |
| `flatten` | `SELECT * EXCLUDE (col), (col).* FROM …` (with optional `keep_original` to keep both) | Column dropdown with STRUCT/MAP highlighted as optgroup, optional prefix, keep-original checkbox |

Both frontend forms + backend compilers wired. `compile_wrangle` registers all 10 ops in `_COMPILERS`.

Standalone canvas nodes for `sort` / `deduplicate` / `sample` / `flatten_explode` remain in the palette for backwards compatibility — they're not retired. Going forward, users building a typical "wash my CSV" flow should reach for the Wrangler.

## Net change

| File | Lines added | Lines removed |
|---|---|---|
| `frontend/src/components/ConfigPanel.tsx` | ~190 | ~95 (auth blocks collapsed via AuthSection; headers JSON textareas removed) |
| `frontend/src/components/data-wrangler/DataWranglerConfig.tsx` | ~340 | ~10 |
| `backend/fpulse/nodes/data_wrangler.py` | ~80 | 0 |

## Verification

- ✓ Zero console errors after HMR of all 3 changed files
- ✓ All 4 new Wrangler form components defined (`SortForm`, `DedupeForm`, `SampleForm`, `FlattenForm`)
- ✓ All 4 backend compilers defined (`_compile_sort`, `_compile_dedupe`, `_compile_sample`, `_compile_flatten`) and registered in `_COMPILERS`
- ✓ `AuthSection` + `KeyValueRepeater` primitives defined in ConfigPanel.tsx
- ✓ api_source + api_sink no longer contain inline auth blocks (verified by grep)

## Still residual

- `http_request` AuthSection migration (needs key-translation wrapper) — ~30 min
- Form-SDK extraction to a dedicated `components/forms/` module — current primitives live inline in ConfigPanel.tsx. The migration to a real module is mechanical (cut-paste + add imports) but adds churn across 30+ consumers. Slated for a focused refactor pass.
- `flatten` runtime prefix handling — the compiler currently ignores the `prefix` field because field names aren't statically known at compile time. The executor wrapper should apply the prefix at runtime when it sees the resolved struct's actual field set.

---

## File index updates (round 6)

| Concern | File |
|---|---|
| `<AuthSection>` primitive | `frontend/src/components/ConfigPanel.tsx` (above `KeyValueRepeater`) |
| `<KeyValueRepeater>` primitive | `frontend/src/components/ConfigPanel.tsx` (above `SecretInput`) |
| 4 new Wrangler sub-step forms | `frontend/src/components/data-wrangler/DataWranglerConfig.tsx` (`SortForm`, `DedupeForm`, `SampleForm`, `FlattenForm`) |
| 4 new Wrangler backend compilers | `backend/fpulse/nodes/data_wrangler.py` (`_compile_sort`, `_compile_dedupe`, `_compile_sample`, `_compile_flatten`) |

---

# Addendum 6 — 2026-05-19 P2 residual closeout

Three residuals from Addendum 5 closed. **Form-SDK extraction** explicitly deferred to **Plus v2.0** per memory note (NOT Plus v1.0 GA) — keep the primitives inline in OSS forever.

## #1 — `http_request` migrated to `<AuthSection>` (with key-translation wrapper)

http_request's backend reads non-canonical names (`auth_token` / `auth_user` / `auth_pass`) instead of the AuthSection vocabulary (`bearer_token` / `username` / `password`). Rather than fork the primitive, the consumer adapts:

```ts
// View layer: surface legacy values to AuthSection under canonical names
const adaptedParams = {
  ...params,
  bearer_token: params.bearer_token ?? params.auth_token,
  username: params.username ?? params.auth_user,
  password: params.password ?? params.auth_pass,
};

// Write layer: rewrite canonical writes back to http_request's legacy keys
const HTTP_AUTH_KEY_MAP = {
  bearer_token: 'auth_token',
  username: 'auth_user',
  password: 'auth_pass',
};
const adaptedOnChange = (patch) => {
  const out = {};
  for (const [k, v] of Object.entries(patch)) {
    const legacy = HTTP_AUTH_KEY_MAP[k];
    if (legacy) { out[legacy] = v; out[k] = undefined; }
    else out[k] = v;
  }
  onChange(nodeId, out);
};

return <AuthSection params={adaptedParams} onChange={adaptedOnChange} />;
```

Same UX as api_source / api_sink — single dropdown + matching credential fields, masked secrets, switchAuthType state sanitation. Backend wire format unchanged: http_request still emits `auth_token`, the executor reads `auth_token`. Zero migration needed on existing pipelines.

All three HTTP-ish nodes (api_source, api_sink, http_request) now share one auth UI implementation.

## #3 — `flatten` runtime prefix resolved

`_compile_flatten` previously ignored the `prefix` field because struct field names aren't statically known at SQL compile time. Now resolves via DuckDB introspection:

```python
def _compile_flatten(prev_sql, config, alias, *, conn=None):
    ...
    if prefix and conn is not None:
        probe = conn.sql(f"SELECT {expansion} FROM ({prev_sql}) LIMIT 0")
        field_names = list(probe.columns)
        if field_names:
            aliased = ", ".join(f"{_q(column)}.{_q(f)} AS {_q(prefix + f)}" for f in field_names)
            return f"SELECT * EXCLUDE ({_q(column)}), {aliased} FROM ({prev_sql}) AS {alias}"
    # Fallback: un-prefixed expansion if no conn or struct introspection fails
    ...
```

`compile_wrangle` signature extended with `conn` kwarg (default `None` for graceful degradation in dry-run / test contexts). Both call sites in `DataWranglerNode.execute()` and `preview_steps()` now pass `ctx.conn` / `conn` respectively.

Zero-row probe is cheap (DuckDB short-circuits LIMIT 0) so prod runs don't take a perf hit. If introspection fails (column isn't actually a STRUCT, conn unavailable), falls back to the un-prefixed `(col).*` path — flatten still works, prefix just gets ignored. Production wrangler runs always carry a conn so the prefix is honored.

## #4 — Tier-7 empty-state CTAs

### `saas_connector` empty state

Was: grey one-liner "No SaaS manifests loaded on the backend." with no help.

Now: structured empty card with:
- Explanation that manifests live in `backend/connectors/manifests/` and OSS doesn't bundle pre-built ones (every connector is open source)
- Link to `/help#docs/connectors.md` ("Read the connector manifest guide")
- Link to the GitHub manifest directory ("Browse community manifests")
- Fallback hint pointing users to the HTTP Request node for one-off API calls that don't need a manifest

### `validate` rule value field auto-focus

When the user picks a value-requiring check (`in_range` / `regex` / `min_length` / `max_length` / comparator ops), the just-rendered value input auto-focuses (only when value is empty — doesn't steal focus from existing values during re-render). Saves a tab keypress per rule.

## Plus v2.0 deferral (form-SDK extraction)

The reusable primitives (`<AuthSection>`, `<KeyValueRepeater>`, `<SecretInput>`, the four new Wrangler sub-step forms, `authVocabulary`, etc.) stay **inline in OSS** indefinitely. Extraction to a dedicated `frontend/src/components/forms/` module is **slated for Plus v2.0** — NOT shipped with Plus v1.0 GA (Nov 2026).

Why: Plus v1.0 ships paid value (governance / RBAC / CDC / SCD2 backfill / checkpointing / OIDC / vault / sandbox / drift). Plus v2.0 is where custom-node-form authoring + white-label theming + an `@fpulse/forms` npm package become real customer-facing features that justify the refactor.

Until then: don't touch primitive file locations. The Plus v2.0 form-SDK extraction decision is tracked in the internal Plus roadmap.

---

## File index updates (round 7)

| Concern | File |
|---|---|
| http_request AuthSection wrapper (legacy key translation) | `frontend/src/components/ConfigPanel.tsx` (inside `HttpRequestConfig`) |
| `_compile_flatten` runtime prefix resolution + `compile_wrangle` `conn` kwarg | `backend/fpulse/nodes/data_wrangler.py` |
| `saas_connector` empty-state CTAs | `frontend/src/components/ConfigPanel.tsx` (inside `SaaSConnectorConfig`) |
| `validate` rule value auto-focus | `frontend/src/components/ConfigPanel.tsx` (inside `ValidateConfig`) |


