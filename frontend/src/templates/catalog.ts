/**
 * Template catalog — 27 curated workflows that span the full surface of
 * F-Pulse OSS. Each template covers a distinct business motion (file
 * ingest, API extract, warehouse sync, monitoring, multi-source join,
 * regional fan-out, AI classification, data quality, reverse ETL,
 * webhook enrichment), plus a Storage family (#13 / V13) that lands
 * data in Managed Tables for cross-pipeline reuse (file/API/SharePoint/
 * Oracle → Managed Table, plus a named SQL Server → Postgres cross-DB
 * sync), plus the round-2 Ingest patterns (incremental DB extract,
 * daily-sync shape, API cursor-pagination ingest), plus round-3
 * additions (failure-alert flow, file-cleanup → managed-table,
 * webhook → managed-table), plus 5 widely-adopted architectural
 * patterns (Medallion, Kimball, Inmon, Data Vault, OBT). Together
 * they exercise every node category the OSS edition ships with.
 *
 * Templates are referenced by `id` from `useTemplate(id)` in the workflow
 * store, and rendered as cards on the Templates gallery page.
 *
 * Node-type rule: templates MUST only use types that are visible in the
 * Modules palette (see ModulesPanel.tsx → MODULES). Specifically:
 *   • Data Movement: source, destination, copy_data
 *     — the per-format types (csv_source, db_source, api_source,
 *       csv_sink, db_sink, api_sink, warehouse_sink, …) have been
 *       collapsed into the two generic nodes. The connector is picked
 *       inside the node's config via `connector_type` + (optionally) a
 *       saved `connection_id`.
 *   • Transform/Combine/Control/Action/AI — use the visible types only.
 *     Replacements applied here: `validate` → `data_quality`,
 *     `conditional_split` → `switch_case`, `if_condition` stays.
 */

export type TemplateComplexity = 'simple' | 'complex';

export interface TemplateNode {
  id: string;
  type: string;            // StepType value (source, transform, …)
  label: string;
  params: Record<string, unknown>;
  position: { x: number; y: number };
}

export interface TemplateConnection {
  from_step: string;
  to_step: string;
  condition?: string;
}

export interface TemplateDefinition {
  id: string;                          // useTemplate(id) key
  name: string;                        // canvas workflow name on load
  tagline: string;                     // one-liner under card title
  description: string;                 // 1-2 sentence card body
  complexity: TemplateComplexity;
  category: string;                    // grouping label
  gradient: string;                    // Tailwind gradient classes for card header
  accent: string;                      // Tailwind text color matching the gradient
  icon: string;                        // SVG path-data shorthand (rendered in TemplateCard)
  tags: string[];                      // node-category chips
  steps: TemplateNode[];
  connections: TemplateConnection[];
}

const X_STEP = 320;   // horizontal spacing between nodes
const Y_BASE = 200;

export const TEMPLATE_CATALOG: TemplateDefinition[] = [
  // ═══════════════════════════════════════════════════════════════════════
  //  GOLDEN FIRST PIPELINE — zero external dependencies
  //  ─────────────────────────────────────────────────────────────────────
  //  This is the "click Run and see output" template that ships seeded.
  //  Backend's _seed_demo_data() copies fpulse/seed_data/orders.csv into
  //  ${FPULSE_DATA_DIR}/samples/orders.csv on first startup, so the source
  //  path below resolves out-of-the-box on both Docker and native installs.
  //  Output lands in ${FPULSE_DATA_DIR}/samples/output/active_orders.csv.
  //
  //  Keep this template FIRST so it's the first card a new user sees in
  //  the Templates gallery — it sets the bar for "F-Pulse just works."
  // ═══════════════════════════════════════════════════════════════════════
  {
    id: 'first_pipeline_csv',
    name: 'First pipeline — CSV in, CSV out',
    tagline: 'Click Run and see real output. Zero setup.',
    description:
      'The golden demo. Reads the bundled sample orders.csv (seeded on first start), filters to active orders only, adds a load timestamp, and writes the result as CSV. No external databases, no API keys, no cloud accounts — runs end-to-end on a fresh install.',
    complexity: 'simple',
    category: 'Get Started',
    gradient: 'from-emerald-500 via-teal-500 to-cyan-500',
    accent: 'text-emerald-700',
    icon: 'M5 13l4 4L19 7',
    tags: ['File', 'Transform', 'Demo'],
    steps: [
      // Param names match the actual CSV_SOURCE / CSV_SINK schemas in
      // backend/fpulse/nodes/csv_source.py + sinks.py — `header` (not
      // `has_header`), no `mode` field (DuckDB COPY overwrites by default,
      // and the sink auto-creates the output directory).
      { id: 's1', type: 'source',         label: 'Read orders.csv', params: { connector_type: 'csv', file_path: 'samples/orders.csv', header: true },                            position: { x: 0,           y: Y_BASE } },
      { id: 's2', type: 'filter',         label: 'Active only',     params: { condition: "status = 'active'" },                                                                    position: { x: X_STEP,      y: Y_BASE } },
      { id: 's3', type: 'derived_column', label: 'Add loaded_at',   params: { expression: 'SELECT *, CURRENT_TIMESTAMP AS loaded_at FROM source_table' },                         position: { x: X_STEP * 2,  y: Y_BASE } },
      { id: 's4', type: 'destination',    label: 'Write CSV',       params: { connector_type: 'csv', file_path: 'samples/output/active_orders.csv', header: true },                position: { x: X_STEP * 3,  y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's3', to_step: 's4' },
    ],
  },

  // ═══════════════════════════════════════════════════════════════════════
  //  SIMPLE — linear, single-source ETL building blocks
  // ═══════════════════════════════════════════════════════════════════════
  {
    id: 'csv_to_postgres',
    name: 'CSV → Postgres',
    tagline: 'File ingestion, the hello-world of ETL',
    description:
      'Read a CSV file, clean nulls and add a load timestamp, then upsert into a Postgres table. The smallest production-grade pipeline shape.',
    complexity: 'simple',
    category: 'Ingest',
    gradient: 'from-violet-500 via-indigo-500 to-blue-500',
    accent: 'text-indigo-700',
    icon: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M9 13h6 M9 17h6',
    tags: ['File', 'Transform', 'Database'],
    steps: [
      { id: 's1', type: 'source',          label: 'Read CSV',        params: { connector_type: 'csv',      file_path: 'data/orders.csv', has_header: true }, position: { x: 0,           y: Y_BASE } },
      { id: 's2', type: 'filter',          label: 'Drop null IDs',   params: { condition: 'order_id IS NOT NULL' },                                          position: { x: X_STEP,      y: Y_BASE } },
      { id: 's3', type: 'derived_column',  label: 'Add loaded_at',   params: { expression: 'SELECT *, CURRENT_TIMESTAMP AS loaded_at FROM source_table' },   position: { x: X_STEP * 2,  y: Y_BASE } },
      { id: 's4', type: 'destination',     label: 'Postgres',        params: { connector_type: 'database', table: 'orders', mode: 'upsert', key_columns: ['order_id'] }, position: { x: X_STEP * 3, y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's3', to_step: 's4' },
    ],
  },
  // ═══════════════════════════════════════════════════════════════════════
  //  STORAGE-FAMILY TEMPLATES (#13, V13 of the F-Pulse product vision)
  //  ─────────────────────────────────────────────────────────────────────
  //  Each writes to a Managed Table via `local_table_sink` so the resulting
  //  output appears in the Storage page's Managed Tables sub-tab — the
  //  intended "data home" pattern. Pairs with the cross-pipeline reuse
  //  story (any other pipeline can read these via `local_table_source`).
  // ═══════════════════════════════════════════════════════════════════════
  {
    id: 'file_to_managed_table',
    name: 'Upload file → Clean → Managed Table',
    tagline: 'One-click landing into the data home',
    description:
      'Read an uploaded CSV from Storage → Files, drop empty rows and trim whitespace, then write to a Parquet-backed Managed Table addressable by schema.name. Subsequent pipelines read this table without re-uploading the file.',
    complexity: 'simple',
    category: 'Storage',
    gradient: 'from-amber-500 via-orange-500 to-rose-500',
    accent: 'text-orange-700',
    icon: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4 M17 8l-5-5-5 5 M12 3v12',
    tags: ['File', 'Clean', 'Managed Table'],
    steps: [
      { id: 's1', type: 'source',           label: 'Read uploaded CSV', params: { connector_type: 'csv', file_path: 'samples/orders.csv', header: true },                       position: { x: 0,          y: Y_BASE } },
      { id: 's2', type: 'data_quality',     label: 'Drop empty rows',   params: { rules: [{ type: 'not_null', columns: ['order_id'] }], on_fail: 'drop' },                       position: { x: X_STEP,     y: Y_BASE } },
      { id: 's3', type: 'transform',        label: 'Trim whitespace',   params: { expression: 'SELECT TRIM(customer) AS customer, * EXCLUDE customer FROM source_table' },     position: { x: X_STEP * 2, y: Y_BASE } },
      { id: 's4', type: 'local_table_sink', label: 'Managed Table',     params: { schema: 'default', name: 'orders_clean', mode: 'replace' },                                   position: { x: X_STEP * 3, y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's3', to_step: 's4' },
    ],
  },
  {
    id: 'api_to_managed_table',
    name: 'REST API → Flatten → Managed Table',
    tagline: 'SaaS extract into the local data home',
    description:
      'Fetch from a paginated REST endpoint, flatten the nested response, and append to a Managed Table. Lets analysts query the data with SQL via DuckDB without standing up a separate warehouse.',
    complexity: 'simple',
    category: 'Storage',
    gradient: 'from-sky-500 via-blue-500 to-indigo-500',
    accent: 'text-blue-700',
    icon: 'M5 12h14 M12 5l7 7-7 7 M3 3v18',
    tags: ['API', 'Flatten', 'Managed Table'],
    steps: [
      { id: 's1', type: 'source',           label: 'Fetch records',  params: { connector_type: 'rest_api', url: 'https://api.example.com/v1/records', method: 'GET', auth_type: 'bearer', pagination: { strategy: 'cursor', cursor_param: 'after', cursor_field: '$.next' } }, position: { x: 0, y: Y_BASE } },
      { id: 's2', type: 'flatten_explode',  label: 'Flatten nested', params: { columns: ['data'] },                                                                                                                                                                            position: { x: X_STEP,     y: Y_BASE } },
      { id: 's3', type: 'derived_column',   label: 'Cast types',     params: { expression: 'SELECT *, CAST(id AS BIGINT) AS id, CAST(updated_at AS TIMESTAMP) AS updated_at FROM source_table' },                                                                              position: { x: X_STEP * 2, y: Y_BASE } },
      { id: 's4', type: 'local_table_sink', label: 'Managed Table',  params: { schema: 'default', name: 'api_records', mode: 'merge', merge_on: ['id'] },                                                                                                                       position: { x: X_STEP * 3, y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's3', to_step: 's4' },
    ],
  },
  {
    id: 'sharepoint_to_managed_table',
    name: 'SharePoint file → Managed Table',
    tagline: 'Pull from Microsoft 365 into the data home',
    description:
      'Read a CSV stored on SharePoint via Microsoft Graph (client-credentials OAuth against an Azure App Registration), validate schema, and load to a Managed Table. Replaces ad-hoc Excel downloads with a refreshable pipeline.',
    complexity: 'simple',
    category: 'Storage',
    gradient: 'from-indigo-500 via-violet-500 to-purple-500',
    accent: 'text-violet-700',
    icon: 'M12 2L2 7v10c0 5.55 3.84 9.74 9 11 5.16-1.26 9-5.45 9-11V7l-10-5z',
    tags: ['SharePoint', 'Microsoft Graph', 'Managed Table'],
    steps: [
      { id: 's1', type: 'source',           label: 'Read SharePoint CSV', params: { connector_type: 'microsoft_graph', endpoint: '/sites/{site_id}/drives/{drive_id}/items/{item_id}/content', format: 'csv', header: true },               position: { x: 0,          y: Y_BASE } },
      { id: 's2', type: 'data_quality',     label: 'Validate schema',     params: { rules: [{ type: 'not_null', columns: ['id'] }, { type: 'unique', columns: ['id'] }], on_fail: 'fail' },                                                  position: { x: X_STEP,     y: Y_BASE } },
      { id: 's3', type: 'derived_column',   label: 'Add load time',       params: { expression: 'SELECT *, CURRENT_TIMESTAMP AS loaded_at FROM source_table' },                                                                              position: { x: X_STEP * 2, y: Y_BASE } },
      { id: 's4', type: 'local_table_sink', label: 'Managed Table',       params: { schema: 'default', name: 'sharepoint_data', mode: 'merge', merge_on: ['id'] },                                                                          position: { x: X_STEP * 3, y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's3', to_step: 's4' },
    ],
  },
  {
    id: 'oracle_to_managed_table',
    name: 'Oracle Fusion → Managed Table',
    tagline: 'Pull FSCM / HCM rows into the data home',
    description:
      'Query an Oracle Fusion Cloud REST endpoint (FSCM / HCM / CRM), cast types, and write to a Managed Table. The Oracle Fusion connector covers the Fusion REST surface; a separate connector (oracle_bip) handles BI Publisher reports.',
    complexity: 'simple',
    category: 'Storage',
    gradient: 'from-red-500 via-orange-600 to-yellow-500',
    accent: 'text-red-700',
    icon: 'M12 6c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6-2.69-6-6-6z M12 2v4 M12 18v4 M4 12H2 M22 12h-4',
    tags: ['Oracle', 'Database', 'Managed Table'],
    steps: [
      { id: 's1', type: 'source',           label: 'Oracle Fusion',  params: { connector_type: 'oracle_fusion', endpoint: '/fscmRestApi/resources/11.13.18.05/invoices', method: 'GET' }, position: { x: 0,          y: Y_BASE } },
      { id: 's2', type: 'derived_column',   label: 'Cast types',     params: { expression: 'SELECT *, CAST(invoice_id AS BIGINT) AS invoice_id, CAST(invoice_date AS DATE) AS invoice_date FROM source_table' }, position: { x: X_STEP,     y: Y_BASE } },
      { id: 's3', type: 'local_table_sink', label: 'Managed Table',  params: { schema: 'default', name: 'oracle_invoices', mode: 'merge', merge_on: ['invoice_id'] },                    position: { x: X_STEP * 2, y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
    ],
  },
  {
    id: 'sqlserver_to_postgres',
    name: 'SQL Server → Postgres (cross-database sync)',
    tagline: 'Named cross-DB sync with DQ in the middle',
    description:
      'Pull rows from MS SQL Server with an incremental cursor, run a data-quality check (drop rows missing required keys), and upsert into Postgres by primary key. The canonical "moving rows between two databases" pattern.',
    complexity: 'simple',
    category: 'Ingest',
    gradient: 'from-slate-600 via-blue-600 to-indigo-600',
    accent: 'text-blue-700',
    icon: 'M21 12c0 4.97-4.03 9-9 9s-9-4.03-9-9 4.03-9 9-9 9 4.03 9 9z M3 12h18 M12 3v18',
    tags: ['Database', 'Incremental', 'Cross-DB'],
    steps: [
      { id: 's1', type: 'source',         label: 'MS SQL Server', params: { connector_type: 'database', dialect: 'mssql', query: 'SELECT * FROM orders WHERE updated_at > :last_run', incremental: { cursor_field: 'updated_at', initial_value: '1970-01-01T00:00:00Z' } }, position: { x: 0,          y: Y_BASE } },
      { id: 's2', type: 'data_quality',   label: 'Drop null IDs', params: { rules: [{ type: 'not_null', columns: ['order_id'] }], on_fail: 'drop' },                                                                                                                       position: { x: X_STEP,     y: Y_BASE } },
      { id: 's3', type: 'derived_column', label: 'Add synced_at', params: { expression: 'SELECT *, CURRENT_TIMESTAMP AS synced_at FROM source_table' },                                                                                                                    position: { x: X_STEP * 2, y: Y_BASE } },
      { id: 's4', type: 'destination',    label: 'Postgres',      params: { connector_type: 'database', dialect: 'postgres', table: 'orders', mode: 'upsert', key_columns: ['order_id'] },                                                                                  position: { x: X_STEP * 3, y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's3', to_step: 's4' },
    ],
  },
  // ═══════════════════════════════════════════════════════════════════════
  //  ROUND 2 — INGEST PATTERNS (#13 round 2, 2026-05-26)
  //  ─────────────────────────────────────────────────────────────────────
  //  Three named templates filling gaps the user vision called out:
  //  incremental DB extract (named, distinct from the API-flavored
  //  api_incremental), a clean daily-sync shape ready to attach a
  //  schedule + alert, and an API-with-explicit-pagination ingest that
  //  showcases the cursor + page-size knobs.
  // ═══════════════════════════════════════════════════════════════════════
  {
    id: 'incremental_db_extract',
    name: 'Postgres → Managed Table (incremental)',
    tagline: 'Cursor-driven extract — each run only pulls new rows',
    description:
      'Read from Postgres with an updated_at cursor so each run pulls only what changed since the last successful run, cast types defensively, and merge into a Managed Table by primary key. The default shape for warehouse-style incremental loads.',
    complexity: 'simple',
    category: 'Ingest',
    gradient: 'from-teal-500 via-cyan-500 to-sky-600',
    accent: 'text-teal-700',
    icon: 'M3 3v18h18 M7 17V9 M11 17V5 M15 17v-8 M19 17V11',
    tags: ['Database', 'Incremental', 'Managed Table'],
    steps: [
      { id: 's1', type: 'source',           label: 'Postgres extract', params: { connector_type: 'database', dialect: 'postgres', query: 'SELECT * FROM events WHERE updated_at > :last_run', incremental: { cursor_field: 'updated_at', initial_value: '1970-01-01T00:00:00Z' } }, position: { x: 0,          y: Y_BASE } },
      { id: 's2', type: 'derived_column',   label: 'Cast types',       params: { expression: 'SELECT *, CAST(event_id AS BIGINT) AS event_id, CAST(updated_at AS TIMESTAMP) AS updated_at FROM source_table' },                                                                  position: { x: X_STEP,     y: Y_BASE } },
      { id: 's3', type: 'local_table_sink', label: 'Managed Table',    params: { schema: 'default', name: 'events', mode: 'merge', merge_on: ['event_id'] },                                                                                                                       position: { x: X_STEP * 2, y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
    ],
  },
  {
    id: 'daily_sync_pipeline',
    name: 'Daily sync — DB → Validate → Upsert',
    tagline: 'Clean shape for the most common scheduled job',
    description:
      'A three-step pipeline ready to wire a daily cron and an ON_FAILURE alert. Source pulls overnight rows, data-quality drops null keys, and an upsert into Postgres makes the run idempotent. After loading the template, open the pipeline\'s Schedule + Alerts tabs to attach `0 2 * * *` and a Slack notification.',
    complexity: 'simple',
    category: 'Schedule',
    gradient: 'from-fuchsia-500 via-pink-500 to-rose-500',
    accent: 'text-pink-700',
    icon: 'M21 12a9 9 0 1 1-9-9 M12 7v5l3 3 M21 4v6h-6',
    tags: ['Schedule', 'Validate', 'Database'],
    steps: [
      { id: 's1', type: 'source',         label: 'Read source',    params: { connector_type: 'database', dialect: 'postgres', query: 'SELECT * FROM source_table WHERE created_at >= CURRENT_DATE - INTERVAL \'1 day\'' }, position: { x: 0,          y: Y_BASE } },
      { id: 's2', type: 'data_quality',   label: 'Validate rows',  params: { rules: [{ type: 'not_null', columns: ['id'] }, { type: 'unique', columns: ['id'] }], on_fail: 'drop' },                                       position: { x: X_STEP,     y: Y_BASE } },
      { id: 's3', type: 'destination',    label: 'Upsert target', params: { connector_type: 'database', dialect: 'postgres', table: 'daily_target', mode: 'upsert', key_columns: ['id'] },                                position: { x: X_STEP * 2, y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
    ],
  },
  {
    id: 'failure_alert_flow',
    name: 'Pipeline failure → Slack alert',
    tagline: 'Route a failed run to a focused channel',
    description:
      'A two-node shape ready to attach to any failed-pipeline trigger. Reads execution metadata, formats a short summary message, and posts to Slack. After loading, wire this to the upstream pipeline\'s ON_FAILURE alert via the Alerts tab.',
    complexity: 'simple',
    category: 'Monitor',
    gradient: 'from-red-500 via-rose-500 to-pink-500',
    accent: 'text-red-700',
    icon: 'M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z M12 9v4 M12 17h.01',
    tags: ['Alert', 'Monitor', 'Slack'],
    steps: [
      { id: 's1', type: 'transform',   label: 'Format alert',  params: { expression: 'SELECT \'Pipeline \' || workflow_name || \' failed at \' || started_at || \' — \' || COALESCE(error, \'unknown error\') AS message FROM source_table' }, position: { x: 0,      y: Y_BASE } },
      { id: 's2', type: 'destination', label: 'Slack channel', params: { connector_type: 'slack', channel: '#alerts', mode: 'append' },                                                                                                            position: { x: X_STEP, y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
    ],
  },
  {
    id: 'file_cleanup_to_table',
    name: 'CSV cleanup → Managed Table',
    tagline: 'Trim, deduplicate, validate, load',
    description:
      'The canonical file-prep shape: read a CSV, drop empty rows, deduplicate by primary key, validate required columns, then write to a Managed Table. Replaces ad-hoc Excel cleanup with a refreshable pipeline.',
    complexity: 'simple',
    category: 'Storage',
    gradient: 'from-orange-500 via-amber-500 to-yellow-500',
    accent: 'text-amber-700',
    icon: 'M19 11H5m14 0a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2m14 0V9a2 2 0 0 0-2-2H7a2 2 0 0 0-2 2v2 M12 15v3 M9 15h6',
    tags: ['File', 'Clean', 'Managed Table'],
    steps: [
      { id: 's1', type: 'source',           label: 'Read CSV',           params: { connector_type: 'csv', file_path: 'samples/customers.csv', header: true },                            position: { x: 0,          y: Y_BASE } },
      { id: 's2', type: 'data_quality',     label: 'Drop empty rows',    params: { rules: [{ type: 'not_null', columns: ['customer_id'] }], on_fail: 'drop' },                            position: { x: X_STEP,     y: Y_BASE } },
      { id: 's3', type: 'deduplicate',      label: 'Dedupe by ID',       params: { key: ['customer_id'], order_by: 'updated_at' },                                                       position: { x: X_STEP * 2, y: Y_BASE } },
      { id: 's4', type: 'data_quality',     label: 'Validate required',  params: { rules: [{ type: 'not_null', columns: ['email'] }, { type: 'unique', columns: ['customer_id'] }], on_fail: 'fail' }, position: { x: X_STEP * 3, y: Y_BASE } },
      { id: 's5', type: 'local_table_sink', label: 'Managed Table',      params: { schema: 'default', name: 'customers_clean', mode: 'replace' },                                        position: { x: X_STEP * 4, y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's3', to_step: 's4' },
      { from_step: 's4', to_step: 's5' },
    ],
  },
  {
    id: 'webhook_to_managed_table',
    name: 'Webhook → Flatten → Managed Table',
    tagline: 'Capture inbound events to a queryable table',
    description:
      'Triggered by a Gateway webhook (publish this pipeline to get a stable URL + API key). Flattens the JSON payload and appends to a Managed Table so the events are queryable via DuckDB. Pairs naturally with a downstream daily-sync.',
    complexity: 'simple',
    category: 'Trigger',
    gradient: 'from-emerald-500 via-teal-500 to-cyan-500',
    accent: 'text-emerald-700',
    icon: 'M22 11.08V12a10 10 0 1 1-5.93-9.14 M22 4L12 14.01l-3-3',
    tags: ['Webhook', 'Trigger', 'Managed Table'],
    steps: [
      { id: 's1', type: 'source',           label: 'Webhook payload', params: { connector_type: 'http_trigger', body_param: 'payload' },                                                position: { x: 0,          y: Y_BASE } },
      { id: 's2', type: 'flatten_explode',  label: 'Flatten JSON',    params: { columns: ['payload'] },                                                                                  position: { x: X_STEP,     y: Y_BASE } },
      { id: 's3', type: 'derived_column',   label: 'Add received_at', params: { expression: 'SELECT *, CURRENT_TIMESTAMP AS received_at FROM source_table' },                            position: { x: X_STEP * 2, y: Y_BASE } },
      { id: 's4', type: 'local_table_sink', label: 'Managed Table',   params: { schema: 'default', name: 'webhook_events', mode: 'append' },                                             position: { x: X_STEP * 3, y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's3', to_step: 's4' },
    ],
  },
  {
    id: 'api_paginated_ingest',
    name: 'REST API → Table (with cursor pagination)',
    tagline: 'Explicit pagination — handles 10K+ records cleanly',
    description:
      'Pull every page from a REST endpoint using cursor pagination (after-token in query, next.after in response), flatten the JSON response, and upsert into a Managed Table. Use this when the API returns nested pages of records and the data set is too large for a single fetch.',
    complexity: 'simple',
    category: 'Ingest',
    gradient: 'from-purple-500 via-indigo-500 to-blue-500',
    accent: 'text-indigo-700',
    icon: 'M21 12a9 9 0 1 1-9-9 M3 12h6 M15 12h6 M12 3v6 M12 15v6',
    tags: ['API', 'Pagination', 'Managed Table'],
    steps: [
      { id: 's1', type: 'source',           label: 'Paginated API', params: { connector_type: 'rest_api', url: 'https://api.example.com/v1/records', method: 'GET', auth_type: 'bearer', pagination: { strategy: 'cursor', cursor_param: 'after', cursor_field: '$.next.after', page_size: 100, page_size_param: 'limit', max_pages: 1000 } }, position: { x: 0,          y: Y_BASE } },
      { id: 's2', type: 'flatten_explode',  label: 'Flatten data',  params: { columns: ['data'] },                                                                                                                                                                                                                                                position: { x: X_STEP,     y: Y_BASE } },
      { id: 's3', type: 'local_table_sink', label: 'Managed Table', params: { schema: 'default', name: 'api_paged_records', mode: 'merge', merge_on: ['id'] },                                                                                                                                                                                     position: { x: X_STEP * 2, y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
    ],
  },
  {
    id: 'api_incremental',
    name: 'API → DB (incremental)',
    tagline: 'SaaS extract with cursor pagination',
    description:
      'Pull from a paginated REST API using the last successful run as a cursor, flatten nested JSON, and append to a Postgres table. The most common production pattern for SaaS data.',
    complexity: 'simple',
    category: 'Ingest',
    gradient: 'from-cyan-500 via-sky-500 to-blue-600',
    accent: 'text-sky-700',
    icon: 'M21 12a9 9 0 1 1-9-9 M21 12h-9 M12 3l9 9-9 9',
    tags: ['API', 'Incremental', 'Database'],
    steps: [
      { id: 's1', type: 'source',         label: 'Fetch records',  params: { connector_type: 'rest_api', url: 'https://api.example.com/v1/records', method: 'GET', auth_type: 'bearer', pagination: { strategy: 'cursor', cursor_param: 'after', cursor_field: '$.next' }, incremental: { cursor_field: 'updated_at', initial_value: '1970-01-01T00:00:00Z' } }, position: { x: 0, y: Y_BASE } },
      { id: 's2', type: 'flatten_explode', label: 'Flatten nested', params: { columns: ['data'] },                                                                                                                                                                                                                                                                  position: { x: X_STEP,     y: Y_BASE } },
      { id: 's3', type: 'derived_column',  label: 'Cast types',     params: { expression: 'SELECT *, CAST(id AS BIGINT) AS id, CAST(updated_at AS TIMESTAMP) AS updated_at FROM source_table' },                                                                                                                                                                  position: { x: X_STEP * 2, y: Y_BASE } },
      { id: 's4', type: 'destination',     label: 'Postgres',       params: { connector_type: 'database', table: 'records', mode: 'upsert', key_columns: ['id'] },                                                                                                                                                                                                position: { x: X_STEP * 3, y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's3', to_step: 's4' },
    ],
  },
  {
    id: 'db_to_warehouse',
    name: 'DB → Warehouse sync',
    tagline: 'Operational database to analytical store',
    description:
      'Query an operational Postgres table, roll up to daily aggregates, and write to a warehouse (Parquet on S3 by default — swap the connector for Snowflake or BigQuery). Foundation for any analytics layer.',
    complexity: 'simple',
    category: 'Replicate',
    gradient: 'from-emerald-500 via-teal-500 to-cyan-600',
    accent: 'text-emerald-700',
    icon: 'M3 12a9 3 0 1 0 18 0 9 3 0 1 0-18 0z M3 5a9 3 0 1 0 18 0 9 3 0 1 0-18 0z M3 5v14a9 3 0 0 0 18 0V5',
    tags: ['Database', 'Aggregate', 'Warehouse'],
    steps: [
      { id: 's1', type: 'source',      label: 'Read orders',  params: { connector_type: 'database', query: 'SELECT order_id, customer_id, region, amount, created_at FROM orders WHERE created_at >= CURRENT_DATE - INTERVAL 7 DAY' }, position: { x: 0,           y: Y_BASE } },
      { id: 's2', type: 'aggregate',   label: 'Daily rollup', params: { group_by: ['DATE(created_at) AS day', 'region'], aggregations: [{ column: 'amount', function: 'SUM', alias: 'revenue' }, { column: '*', function: 'COUNT', alias: 'order_count' }] }, position: { x: X_STEP, y: Y_BASE } },
      { id: 's3', type: 'destination', label: 'Warehouse',    params: { connector_type: 'warehouse', file_path: 's3://warehouse/orders_daily.parquet', mode: 'append', schema_evolution: 'add_columns' }, position: { x: X_STEP * 2, y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
    ],
  },
  {
    id: 'scheduled_alert',
    name: 'Scheduled alert',
    tagline: 'Operational monitoring → notify on threshold',
    description:
      'Run a KPI query on a schedule, branch when the result crosses a threshold, and post to Slack with the offending rows. The starter pattern for pager-style operational alerting.',
    complexity: 'simple',
    category: 'Monitor',
    gradient: 'from-amber-500 via-orange-500 to-red-500',
    accent: 'text-orange-700',
    icon: 'M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z M12 9v4 M12 17h.01',
    tags: ['Database', 'Conditional', 'Slack'],
    steps: [
      { id: 's1', type: 'source',       label: 'Failed jobs query', params: { connector_type: 'database', query: "SELECT job_name, COUNT(*) AS failures FROM job_runs WHERE status='failed' AND ts > NOW() - INTERVAL 1 HOUR GROUP BY job_name HAVING COUNT(*) > 5" }, position: { x: 0,          y: Y_BASE } },
      { id: 's2', type: 'if_condition', label: 'Has failures?',     params: { condition: 'row_count > 0' },                                                                                                                                                       position: { x: X_STEP,     y: Y_BASE } },
      { id: 's3', type: 'slack_notify', label: 'Page on-call',      params: { channel: '#oncall', message: ':rotating_light: {{row_count}} jobs failed in the last hour. Check the runbook.' },                                                                  position: { x: X_STEP * 2, y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3', condition: 'true' },
    ],
  },

  // ═══════════════════════════════════════════════════════════════════════
  //  COMPLEX — branching, multi-source, AI-augmented
  // ═══════════════════════════════════════════════════════════════════════
  {
    id: 'multi_source_join',
    name: 'Multi-source join → warehouse',
    tagline: 'Customer 360 from DB + SaaS API',
    description:
      'Pull customer records from Postgres, fetch lifetime-value scores from a billing API, join on customer_id, enrich with a derived segment, and load into a warehouse table. The canonical Customer 360 shape.',
    complexity: 'complex',
    category: 'Multi-source',
    gradient: 'from-fuchsia-500 via-purple-500 to-violet-600',
    accent: 'text-purple-700',
    icon: 'M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M23 21v-2a4 4 0 0 0-3-3.87 M16 3.13a4 4 0 0 1 0 7.75',
    tags: ['Multi-source', 'Join', 'Enrich', 'Warehouse'],
    steps: [
      { id: 's1', type: 'source',         label: 'Customers (DB)',     params: { connector_type: 'database', query: 'SELECT customer_id, name, email, created_at FROM customers' },                                                              position: { x: 0,           y: Y_BASE - 100 } },
      { id: 's2', type: 'source',         label: 'LTV scores (API)',   params: { connector_type: 'rest_api', url: 'https://api.billing.example.com/v1/ltv', method: 'GET', auth_type: 'bearer' },                                                  position: { x: 0,           y: Y_BASE + 100 } },
      { id: 's3', type: 'join',           label: 'Join on customer_id', params: { left_key: 'customer_id', right_key: 'customer_id', join_type: 'left' },                                                                                          position: { x: X_STEP,      y: Y_BASE } },
      { id: 's4', type: 'derived_column', label: 'Segment',            params: { expression: "SELECT *, CASE WHEN ltv > 1000 THEN 'enterprise' WHEN ltv > 100 THEN 'smb' ELSE 'self_serve' END AS segment FROM source_table" },                    position: { x: X_STEP * 2,  y: Y_BASE } },
      { id: 's5', type: 'destination',    label: 'Warehouse',          params: { connector_type: 'warehouse', file_path: 's3://warehouse/customer_360.parquet', mode: 'overwrite' },                                                              position: { x: X_STEP * 3,  y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's3' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's3', to_step: 's4' },
      { from_step: 's4', to_step: 's5' },
    ],
  },
  {
    id: 'fanout_regional',
    name: 'Fan-out by region + union',
    tagline: 'Parallel processing per region, then merge',
    description:
      'Split incoming events by region, run a region-specific aggregate on each branch in parallel, union the results back together, and write a single combined report. Multi-tenant / regional processing pattern.',
    complexity: 'complex',
    category: 'Branching',
    gradient: 'from-rose-500 via-pink-500 to-fuchsia-500',
    accent: 'text-pink-700',
    icon: 'M3 6h18 M3 12h18 M3 18h18',
    tags: ['Switch', 'Aggregate', 'Union'],
    steps: [
      { id: 's1',  type: 'source',      label: 'Read events',     params: { connector_type: 'csv', file_path: 'data/events.csv', has_header: true },                                                                position: { x: 0,           y: Y_BASE } },
      { id: 's2',  type: 'switch_case', label: 'Split by region', params: { expression: 'region', cases: ['EMEA', 'AMER', 'APAC'] },                                                                                position: { x: X_STEP,      y: Y_BASE } },
      { id: 's3a', type: 'aggregate',   label: 'EMEA rollup',     params: { group_by: ['country'], aggregations: [{ column: 'amount', function: 'SUM', alias: 'total' }] },                                         position: { x: X_STEP * 2,  y: Y_BASE - 160 } },
      { id: 's3b', type: 'aggregate',   label: 'AMER rollup',     params: { group_by: ['country'], aggregations: [{ column: 'amount', function: 'SUM', alias: 'total' }] },                                         position: { x: X_STEP * 2,  y: Y_BASE } },
      { id: 's3c', type: 'aggregate',   label: 'APAC rollup',     params: { group_by: ['country'], aggregations: [{ column: 'amount', function: 'SUM', alias: 'total' }] },                                         position: { x: X_STEP * 2,  y: Y_BASE + 160 } },
      { id: 's4',  type: 'union',       label: 'Union all',       params: { distinct: false },                                                                                                                      position: { x: X_STEP * 3,  y: Y_BASE } },
      { id: 's5',  type: 'destination', label: 'Combined report', params: { connector_type: 'csv', file_path: 'output/regional_rollup.csv' },                                                                       position: { x: X_STEP * 4,  y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1',  to_step: 's2' },
      { from_step: 's2',  to_step: 's3a', condition: 'EMEA' },
      { from_step: 's2',  to_step: 's3b', condition: 'AMER' },
      { from_step: 's2',  to_step: 's3c', condition: 'APAC' },
      { from_step: 's3a', to_step: 's4' },
      { from_step: 's3b', to_step: 's4' },
      { from_step: 's3c', to_step: 's4' },
      { from_step: 's4',  to_step: 's5' },
    ],
  },
  {
    id: 'ai_classify_route',
    name: 'AI classify + conditional route',
    tagline: 'LLM categorization → switch to right destination',
    description:
      'Read incoming items (tickets, leads, alerts), classify each with an LLM into urgent / standard / spam, then route to three different destinations. Demonstrates AI-augmented workflows with deterministic fallback.',
    complexity: 'complex',
    category: 'AI',
    gradient: 'from-violet-600 via-fuchsia-500 to-amber-400',
    accent: 'text-fuchsia-700',
    icon: 'M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.582a.5.5 0 0 1 0 .962L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z',
    tags: ['AI / Semantic', 'Switch', 'Multi-destination'],
    steps: [
      { id: 's1',  type: 'source',          label: 'Incoming tickets', params: { connector_type: 'json', file_path: 'data/tickets.json' },                                                                                                         position: { x: 0,           y: Y_BASE } },
      { id: 's2',  type: 'semantic_router', label: 'AI classify',      params: { input_column: 'subject', categories: ['urgent', 'standard', 'spam'], output_column: 'category' },                                                                 position: { x: X_STEP,      y: Y_BASE } },
      { id: 's3',  type: 'switch_case',     label: 'Route by category', params: { expression: 'category', cases: ['urgent', 'standard', 'spam'] },                                                                                                 position: { x: X_STEP * 2,  y: Y_BASE } },
      { id: 's4a', type: 'slack_notify',    label: 'Page on-call',     params: { channel: '#urgent', message: 'Urgent ticket: {{subject}}' },                                                                                                      position: { x: X_STEP * 3,  y: Y_BASE - 160 } },
      { id: 's4b', type: 'destination',     label: 'Standard queue',   params: { connector_type: 'database', table: 'tickets', mode: 'append' },                                                                                                   position: { x: X_STEP * 3,  y: Y_BASE } },
      { id: 's4c', type: 'destination',     label: 'Spam archive',     params: { connector_type: 'csv', file_path: 'archive/spam.csv', mode: 'append' },                                                                                           position: { x: X_STEP * 3,  y: Y_BASE + 160 } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's3', to_step: 's4a', condition: 'urgent' },
      { from_step: 's3', to_step: 's4b', condition: 'standard' },
      { from_step: 's3', to_step: 's4c', condition: 'spam' },
    ],
  },
  {
    id: 'data_quality_dlq',
    name: 'Data quality + dead-letter queue',
    tagline: 'Trustworthy data — bad rows quarantined, not dropped',
    description:
      'Validate every row against a schema and business rules. Passing rows write to the clean table; failing rows write to a dead-letter table with the failure reason so nothing silently disappears.',
    complexity: 'complex',
    category: 'Quality',
    gradient: 'from-emerald-500 via-green-500 to-lime-500',
    accent: 'text-emerald-700',
    icon: 'M22 11.08V12a10 10 0 1 1-5.93-9.14 M22 4 12 14.01l-3-3',
    tags: ['Data Quality', 'Switch', 'DLQ'],
    steps: [
      { id: 's1',  type: 'source',       label: 'Read raw events', params: { connector_type: 'csv', file_path: 'data/events.csv', has_header: true },                                                                                                                                                          position: { x: 0,          y: Y_BASE } },
      { id: 's2',  type: 'data_quality', label: 'Schema + rules',  params: { rules: [{ column: 'event_id', check: 'not_null' }, { column: 'amount', check: 'between', min: 0, max: 1000000 }, { column: 'email', check: 'regex', pattern: '^[^@]+@[^@]+\\.[^@]+$' }], on_failure: 'tag' },                       position: { x: X_STEP,     y: Y_BASE } },
      { id: 's3',  type: 'switch_case',  label: 'Pass / fail',     params: { expression: 'is_valid', cases: ['true', 'false'] },                                                                                                                                                                              position: { x: X_STEP * 2, y: Y_BASE } },
      { id: 's4a', type: 'destination',  label: 'Clean table',     params: { connector_type: 'database', table: 'events', mode: 'append' },                                                                                                                                                                    position: { x: X_STEP * 3, y: Y_BASE - 130 } },
      { id: 's4b', type: 'destination',  label: 'Dead-letter',     params: { connector_type: 'database', table: 'events_dlq', mode: 'append' },                                                                                                                                                                position: { x: X_STEP * 3, y: Y_BASE + 130 } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's3', to_step: 's4a', condition: 'true' },
      { from_step: 's3', to_step: 's4b', condition: 'false' },
    ],
  },
  {
    id: 'reverse_etl',
    name: 'Reverse ETL: warehouse → SaaS',
    tagline: 'Operationalize analytics — push insights to where revenue teams live',
    description:
      'Query the warehouse for the latest customer scores, shape into the destination API\'s payload format, and POST in batches with retry. The mirror of ingestion — get analytics back into ops tools.',
    complexity: 'complex',
    category: 'Egress',
    gradient: 'from-indigo-500 via-blue-500 to-cyan-500',
    accent: 'text-blue-700',
    icon: 'M14 16l-4-4 4-4 M21 12H10',
    tags: ['Warehouse', 'Reshape', 'API write'],
    steps: [
      { id: 's1', type: 'source',         label: 'Warehouse query', params: { connector_type: 'database', query: 'SELECT customer_id, segment, lifetime_value, last_seen_at FROM customer_scores WHERE updated_at > NOW() - INTERVAL 1 DAY' },                                                                                                                     position: { x: 0,           y: Y_BASE } },
      { id: 's2', type: 'derived_column', label: 'Reshape payload', params: { expression: "SELECT customer_id AS external_id, JSON_OBJECT('segment', segment, 'ltv', lifetime_value, 'last_seen', last_seen_at) AS traits FROM source_table" },                                                                                                                  position: { x: X_STEP,      y: Y_BASE } },
      { id: 's3', type: 'destination',    label: 'POST to CRM',     params: { connector_type: 'rest_api', url: 'https://api.crm.example.com/v1/people', method: 'POST', batch_size: 100, retry: { max_attempts: 3, backoff: 'exponential' }, auth_type: 'bearer' },                                                                                              position: { x: X_STEP * 2,  y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
    ],
  },
  {
    id: 'webhook_enrich',
    name: 'Webhook → enrich → DB',
    tagline: 'Real-time event ingestion with API-side enrichment',
    description:
      'Receive an inbound webhook, validate the payload shape, enrich each record by looking up additional context from a reference table, and persist to a database. The push-based mirror of polling-based API ingestion.',
    complexity: 'complex',
    category: 'Real-time',
    gradient: 'from-yellow-400 via-amber-500 to-orange-500',
    accent: 'text-amber-700',
    icon: 'M13 2L3 14h9l-1 8 10-12h-9l1-8z',
    tags: ['Webhook', 'Lookup', 'Database'],
    steps: [
      { id: 's1', type: 'webhook_trigger', label: 'Receive webhook',  params: { path: '/hooks/incoming', secret_env: 'WEBHOOK_SECRET' },                                                                       position: { x: 0,           y: Y_BASE } },
      { id: 's2', type: 'data_quality',    label: 'Validate payload', params: { rules: [{ column: 'event_type', check: 'not_null' }, { column: 'user_id', check: 'not_null' }], on_failure: 'fail' },          position: { x: X_STEP,      y: Y_BASE } },
      { id: 's3', type: 'lookup',          label: 'Enrich from API',  params: { lookup_source: 'users_directory', join_field: 'user_id', merge_fields: ['name', 'email', 'team'] },                            position: { x: X_STEP * 2,  y: Y_BASE } },
      { id: 's4', type: 'destination',     label: 'Persist event',    params: { connector_type: 'database', table: 'events', mode: 'append' },                                                                 position: { x: X_STEP * 3,  y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's3', to_step: 's4' },
    ],
  },

  // ═══════════════════════════════════════════════════════════════════════
  //  ARCHITECTURE — widely-adopted modern data architecture patterns.
  //  Each template demonstrates a distinct modelling philosophy.
  // ═══════════════════════════════════════════════════════════════════════
  {
    id: 'medallion_multi_hop',
    name: 'Medallion (Bronze → Silver → Gold)',
    tagline: 'Lakehouse multi-hop: raw → conformed → analytics',
    description:
      'Three curation zones in a single DAG. Bronze stores raw-typed history, Silver dedupes and validates conformance, Gold aggregates business metrics ready for BI.',
    complexity: 'complex',
    category: 'Architecture',
    gradient: 'from-amber-500 via-orange-500 to-yellow-500',
    accent: 'text-amber-700',
    icon: 'M12 2 2 7l10 5 10-5-10-5z M2 17l10 5 10-5 M2 12l10 5 10-5',
    tags: ['Medallion', 'Lakehouse', 'Multi-hop'],
    steps: [
      { id: 's1', type: 'source',         label: 'Raw ingest',         params: { connector_type: 'csv', file_path: 'data/raw/orders.csv', has_header: true },                                                                                                                            position: { x: 0,           y: Y_BASE } },
      { id: 's2', type: 'derived_column', label: 'Light typing',       params: { expression: 'SELECT *, CAST(id AS BIGINT) AS id, CAST(updated_at AS TIMESTAMP) AS updated_at, CURRENT_TIMESTAMP AS bronze_loaded_at FROM source_table' },                                              position: { x: X_STEP,      y: Y_BASE } },
      { id: 's3', type: 'destination',    label: 'Bronze parquet',     params: { connector_type: 'warehouse', file_path: 'lake/bronze/orders.parquet', mode: 'append' },                                                                                                                position: { x: X_STEP * 2,  y: Y_BASE - 160 } },
      { id: 's4', type: 'deduplicate',    label: 'Deduplicate',        params: { key: ['id'], strategy: 'keep_last' },                                                                                                                                                                  position: { x: X_STEP * 2,  y: Y_BASE + 60 } },
      { id: 's5', type: 'data_quality',   label: 'Validate conformance', params: { rules: [{ column: 'id', check: 'not_null' }, { column: 'amount', check: 'between', min: 0, max: 1000000 }], on_failure: 'tag' },                                                                     position: { x: X_STEP * 3,  y: Y_BASE + 60 } },
      { id: 's6', type: 'destination',    label: 'Silver parquet',     params: { connector_type: 'warehouse', file_path: 'lake/silver/orders.parquet', mode: 'append' },                                                                                                                position: { x: X_STEP * 4,  y: Y_BASE - 100 } },
      { id: 's7', type: 'aggregate',      label: 'Business metrics',   params: { group_by: ['category', "DATE_TRUNC('month', updated_at) AS month"], aggregations: [{ column: 'amount', function: 'SUM', alias: 'total' }, { column: '*', function: 'COUNT', alias: 'row_count' }] }, position: { x: X_STEP * 4,  y: Y_BASE + 160 } },
      { id: 's8', type: 'destination',    label: 'Gold parquet',       params: { connector_type: 'warehouse', file_path: 'lake/gold/orders_monthly.parquet', mode: 'overwrite' },                                                                                                       position: { x: X_STEP * 5,  y: Y_BASE + 160 } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's2', to_step: 's4' },
      { from_step: 's4', to_step: 's5' },
      { from_step: 's5', to_step: 's6' },
      { from_step: 's5', to_step: 's7' },
      { from_step: 's7', to_step: 's8' },
    ],
  },
  {
    id: 'kimball_dimensional',
    name: 'Kimball dimensional (star schema)',
    tagline: 'Conformed dimensions + fact, surrogate keys, SCD2',
    description:
      'Build a star schema: a customer dimension with SCD2 surrogate keys, a product dimension, and a fact table joined to both. The classic warehouse modelling pattern.',
    complexity: 'complex',
    category: 'Architecture',
    gradient: 'from-violet-500 via-purple-500 to-fuchsia-500',
    accent: 'text-violet-700',
    icon: 'M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z',
    tags: ['Kimball', 'Star schema', 'SCD2'],
    steps: [
      { id: 's1', type: 'source',         label: 'Raw customers',     params: { connector_type: 'csv', file_path: 'data/raw/customers.csv', has_header: true },                                                                                                          position: { x: 0,           y: Y_BASE - 160 } },
      { id: 's2', type: 'source',         label: 'Raw products',      params: { connector_type: 'csv', file_path: 'data/raw/products.csv', has_header: true },                                                                                                           position: { x: 0,           y: Y_BASE } },
      { id: 's3', type: 'source',         label: 'Raw orders (fact)', params: { connector_type: 'csv', file_path: 'data/raw/orders.csv', has_header: true },                                                                                                             position: { x: 0,           y: Y_BASE + 160 } },
      { id: 's4', type: 'derived_column', label: 'Build dim_customer (SCD2)', params: { expression: "SELECT *, ROW_NUMBER() OVER (ORDER BY customer_id, effective_from) AS customer_sk, COALESCE(end_date, TIMESTAMP '9999-12-31') AS effective_to, CASE WHEN end_date IS NULL THEN TRUE ELSE FALSE END AS is_current FROM source_table" }, position: { x: X_STEP, y: Y_BASE - 160 } },
      { id: 's5', type: 'derived_column', label: 'Build dim_product', params: { expression: 'SELECT *, ROW_NUMBER() OVER (ORDER BY product_id) AS product_sk FROM source_table' },                                                                                       position: { x: X_STEP,      y: Y_BASE } },
      { id: 's6', type: 'join',           label: 'Fact + customer_sk', params: { left_key: 'customer_id', right_key: 'customer_id', join_type: 'left' },                                                                                                                  position: { x: X_STEP * 2,  y: Y_BASE + 80 } },
      { id: 's7', type: 'join',           label: 'Fact + product_sk', params: { left_key: 'product_id', right_key: 'product_id', join_type: 'left' },                                                                                                                     position: { x: X_STEP * 3,  y: Y_BASE + 160 } },
      { id: 's8', type: 'destination',    label: 'dim_customer',      params: { connector_type: 'warehouse', file_path: 'warehouse/dim_customer.parquet', mode: 'overwrite' },                                                                                            position: { x: X_STEP * 2,  y: Y_BASE - 160 } },
      { id: 's9', type: 'destination',    label: 'dim_product',       params: { connector_type: 'warehouse', file_path: 'warehouse/dim_product.parquet', mode: 'overwrite' },                                                                                             position: { x: X_STEP * 2,  y: Y_BASE - 20 } },
      { id: 's10', type: 'destination',   label: 'fact_orders',       params: { connector_type: 'warehouse', file_path: 'warehouse/fact_orders.parquet', mode: 'append' },                                                                                                position: { x: X_STEP * 4,  y: Y_BASE + 160 } },
    ],
    connections: [
      { from_step: 's1', to_step: 's4' },
      { from_step: 's2', to_step: 's5' },
      { from_step: 's4', to_step: 's8' },
      { from_step: 's5', to_step: 's9' },
      { from_step: 's3', to_step: 's6' },
      { from_step: 's4', to_step: 's6' },
      { from_step: 's6', to_step: 's7' },
      { from_step: 's5', to_step: 's7' },
      { from_step: 's7', to_step: 's10' },
    ],
  },
  {
    id: 'inmon_3nf_edw',
    name: 'Inmon 3NF enterprise warehouse',
    tagline: 'Top-down normalised EDW with PK/FK integrity',
    description:
      'Canonicalise operational records, decompose into normalised entities (customer, order header, order line), validate referential integrity, and load to EDW tables. Inmon\'s "single version of truth".',
    complexity: 'complex',
    category: 'Architecture',
    gradient: 'from-slate-500 via-blue-600 to-indigo-700',
    accent: 'text-blue-700',
    icon: 'M6 22V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v18Z M6 12H4a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2 M18 9h2a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-2 M10 6h4 M10 10h4 M10 14h4 M10 18h4',
    tags: ['Inmon', '3NF', 'EDW'],
    steps: [
      { id: 's1', type: 'source',         label: 'OLTP records',         params: { connector_type: 'database', query: 'SELECT * FROM operational.orders_denormalised' },                                                                                                                                                                            position: { x: 0,           y: Y_BASE } },
      { id: 's2', type: 'derived_column', label: 'Canonicalise + types', params: { expression: 'SELECT CAST(customer_id AS BIGINT) AS customer_id, CAST(order_id AS BIGINT) AS order_id, CAST(line_no AS INTEGER) AS line_no, customer_name, customer_email, product_id, quantity, amount, CAST(order_date AS TIMESTAMP) AS order_date FROM source_table' }, position: { x: X_STEP,      y: Y_BASE } },
      { id: 's3', type: 'derived_column', label: 'Project entity_customer', params: { expression: 'SELECT DISTINCT customer_id, customer_name, customer_email FROM source_table' },                                                                                                                                                                  position: { x: X_STEP * 2,  y: Y_BASE - 160 } },
      { id: 's4', type: 'derived_column', label: 'Project order_header',   params: { expression: 'SELECT DISTINCT order_id, customer_id, order_date FROM source_table' },                                                                                                                                                                            position: { x: X_STEP * 2,  y: Y_BASE } },
      { id: 's5', type: 'derived_column', label: 'Project order_line',     params: { expression: 'SELECT order_id, line_no, product_id, quantity, amount FROM source_table' },                                                                                                                                                                       position: { x: X_STEP * 2,  y: Y_BASE + 160 } },
      { id: 's6', type: 'data_quality',   label: 'Validate customer PK',   params: { rules: [{ column: 'customer_id', check: 'not_null' }], on_failure: 'fail' },                                                                                                                                                                                     position: { x: X_STEP * 3,  y: Y_BASE - 160 } },
      { id: 's7', type: 'data_quality',   label: 'Validate order FK',      params: { rules: [{ column: 'order_id', check: 'not_null' }, { column: 'customer_id', check: 'not_null' }], on_failure: 'fail' },                                                                                                                                          position: { x: X_STEP * 3,  y: Y_BASE } },
      { id: 's8', type: 'destination',    label: 'edw.customer',           params: { connector_type: 'database', table: 'edw.customer', mode: 'append' },                                                                                                                                                                                              position: { x: X_STEP * 4,  y: Y_BASE - 160 } },
      { id: 's9', type: 'destination',    label: 'edw.order_header',       params: { connector_type: 'database', table: 'edw.order_header', mode: 'append' },                                                                                                                                                                                          position: { x: X_STEP * 4,  y: Y_BASE } },
      { id: 's10', type: 'destination',   label: 'edw.order_line',         params: { connector_type: 'database', table: 'edw.order_line', mode: 'append' },                                                                                                                                                                                            position: { x: X_STEP * 4,  y: Y_BASE + 160 } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's2', to_step: 's4' },
      { from_step: 's2', to_step: 's5' },
      { from_step: 's3', to_step: 's6' },
      { from_step: 's4', to_step: 's7' },
      { from_step: 's6', to_step: 's8' },
      { from_step: 's7', to_step: 's9' },
      { from_step: 's5', to_step: 's10' },
    ],
  },
  {
    id: 'data_vault_2',
    name: 'Data Vault 2.0 (Hubs / Links / Satellites)',
    tagline: 'Hash keys, hubs, links, and hashdiff-tracked satellites',
    description:
      'Raw Data Vault load pattern: hash business keys with md5, materialise hub_customer + hub_product (business keys), link_order (relationship), and matching satellites with hashdiff for change detection.',
    complexity: 'complex',
    category: 'Architecture',
    gradient: 'from-rose-500 via-pink-600 to-red-600',
    accent: 'text-rose-700',
    icon: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z M9 12l2 2 4-4',
    tags: ['Data Vault', 'Hub-Link-Sat', 'Hashdiff'],
    steps: [
      { id: 's1', type: 'source',         label: 'Source records',       params: { connector_type: 'csv', file_path: 'data/raw/orders.csv', has_header: true },                                                                                                                                                                                                                                                                                                                       position: { x: 0,           y: Y_BASE } },
      { id: 's2', type: 'derived_column', label: 'Generate hash keys',   params: { expression: "SELECT *, md5(UPPER(TRIM(customer_id))) AS hub_customer_hk, md5(UPPER(TRIM(product_id))) AS hub_product_hk, md5(CONCAT_WS('|', customer_id, product_id, order_id)) AS link_order_hk, md5(CONCAT_WS('|', name, email, address)) AS sat_customer_hashdiff, CURRENT_TIMESTAMP AS load_dts, 'ORDERS_SRC' AS record_source FROM source_table" }, position: { x: X_STEP,      y: Y_BASE } },
      { id: 's3', type: 'deduplicate',    label: 'Dedupe hub_customer',  params: { key: ['hub_customer_hk'], strategy: 'keep_first' },                                                                                                                                                                                                                                                                                                                                              position: { x: X_STEP * 2,  y: Y_BASE - 220 } },
      { id: 's4', type: 'deduplicate',    label: 'Dedupe hub_product',   params: { key: ['hub_product_hk'], strategy: 'keep_first' },                                                                                                                                                                                                                                                                                                                                                position: { x: X_STEP * 2,  y: Y_BASE - 60 } },
      { id: 's5', type: 'deduplicate',    label: 'Dedupe link_order',    params: { key: ['link_order_hk'], strategy: 'keep_first' },                                                                                                                                                                                                                                                                                                                                                  position: { x: X_STEP * 2,  y: Y_BASE + 100 } },
      { id: 's6', type: 'deduplicate',    label: 'Dedupe sat_customer',  params: { key: ['hub_customer_hk', 'sat_customer_hashdiff'], strategy: 'keep_first' },                                                                                                                                                                                                                                                                                                                       position: { x: X_STEP * 2,  y: Y_BASE + 260 } },
      { id: 's7', type: 'destination',    label: 'hub_customer',         params: { connector_type: 'warehouse', file_path: 'vault/hub_customer.parquet', mode: 'append' },                                                                                                                                                                                                                                                                                                            position: { x: X_STEP * 3,  y: Y_BASE - 220 } },
      { id: 's8', type: 'destination',    label: 'hub_product',          params: { connector_type: 'warehouse', file_path: 'vault/hub_product.parquet', mode: 'append' },                                                                                                                                                                                                                                                                                                              position: { x: X_STEP * 3,  y: Y_BASE - 60 } },
      { id: 's9', type: 'destination',    label: 'link_order',           params: { connector_type: 'warehouse', file_path: 'vault/link_order.parquet', mode: 'append' },                                                                                                                                                                                                                                                                                                               position: { x: X_STEP * 3,  y: Y_BASE + 100 } },
      { id: 's10', type: 'destination',   label: 'sat_customer',         params: { connector_type: 'warehouse', file_path: 'vault/sat_customer.parquet', mode: 'append' },                                                                                                                                                                                                                                                                                                             position: { x: X_STEP * 3,  y: Y_BASE + 260 } },
    ],
    connections: [
      { from_step: 's1', to_step: 's2' },
      { from_step: 's2', to_step: 's3' },
      { from_step: 's2', to_step: 's4' },
      { from_step: 's2', to_step: 's5' },
      { from_step: 's2', to_step: 's6' },
      { from_step: 's3', to_step: 's7' },
      { from_step: 's4', to_step: 's8' },
      { from_step: 's5', to_step: 's9' },
      { from_step: 's6', to_step: 's10' },
    ],
  },
  {
    id: 'one_big_table',
    name: 'One Big Table (wide analytical)',
    tagline: 'Modern denormalised: join everything, derive metrics, one Parquet',
    description:
      'Join orders + customers + products into a single wide table, add business-derived columns (revenue band, days since order, customer segment), and write one analytical Parquet ready for BI / column-store engines.',
    complexity: 'complex',
    category: 'Architecture',
    gradient: 'from-teal-500 via-cyan-500 to-sky-500',
    accent: 'text-teal-700',
    icon: 'M9 3H5a2 2 0 0 0-2 2v4 M9 3h10a2 2 0 0 1 2 2v4 M9 3v18 M9 21h10a2 2 0 0 0 2-2V9 M9 21H5a2 2 0 0 1-2-2V9 M3 9h18',
    tags: ['OBT', 'Wide table', 'Analytics'],
    steps: [
      { id: 's1', type: 'source',         label: 'Orders',             params: { connector_type: 'csv', file_path: 'data/raw/orders.csv', has_header: true },                                                                                                                                                                                                                                                                                                                                                                                                                                  position: { x: 0,           y: Y_BASE - 160 } },
      { id: 's2', type: 'source',         label: 'Customers',          params: { connector_type: 'csv', file_path: 'data/raw/customers.csv', has_header: true },                                                                                                                                                                                                                                                                                                                                                                                                                              position: { x: 0,           y: Y_BASE } },
      { id: 's3', type: 'source',         label: 'Products',           params: { connector_type: 'csv', file_path: 'data/raw/products.csv', has_header: true },                                                                                                                                                                                                                                                                                                                                                                                                                                position: { x: 0,           y: Y_BASE + 160 } },
      { id: 's4', type: 'join',           label: 'Orders + customers', params: { left_key: 'customer_id', right_key: 'customer_id', join_type: 'left' },                                                                                                                                                                                                                                                                                                                                                                                                                                       position: { x: X_STEP,      y: Y_BASE - 80 } },
      { id: 's5', type: 'join',           label: '+ products',         params: { left_key: 'product_id', right_key: 'product_id', join_type: 'left' },                                                                                                                                                                                                                                                                                                                                                                                                                                          position: { x: X_STEP * 2,  y: Y_BASE } },
      { id: 's6', type: 'derived_column', label: 'Analytical columns', params: { expression: "SELECT *, CASE WHEN amount >= 1000 THEN 'high' WHEN amount >= 100 THEN 'mid' ELSE 'low' END AS revenue_band, DATEDIFF('day', order_date, CURRENT_DATE) AS days_since_order, CASE WHEN total_spent >= 5000 THEN 'platinum' WHEN total_spent >= 1000 THEN 'gold' ELSE 'standard' END AS customer_segment FROM source_table" },                                                                                                                                                                       position: { x: X_STEP * 3,  y: Y_BASE } },
      { id: 's7', type: 'destination',    label: 'Wide analytical parquet', params: { connector_type: 'warehouse', file_path: 'analytics/obt_orders_enriched.parquet', mode: 'overwrite' },                                                                                                                                                                                                                                                                                                                                                                                                       position: { x: X_STEP * 4,  y: Y_BASE } },
    ],
    connections: [
      { from_step: 's1', to_step: 's4' },
      { from_step: 's2', to_step: 's4' },
      { from_step: 's4', to_step: 's5' },
      { from_step: 's3', to_step: 's5' },
      { from_step: 's5', to_step: 's6' },
      { from_step: 's6', to_step: 's7' },
    ],
  },
];

export function getTemplate(id: string): TemplateDefinition | undefined {
  return TEMPLATE_CATALOG.find((t) => t.id === id);
}
