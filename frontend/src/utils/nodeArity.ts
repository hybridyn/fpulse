/**
 * Input cardinality + capability classification for canvas nodes.
 *
 * Three surfaces consume this:
 *   1. Canvas `isValidConnection` — refuses edge drops at the source.
 *   2. `validateWorkflow` — same checks on workflows loaded from JSON.
 *   3. `FPulseNode` — renders a side-effect badge on nodes that have
 *      external consequences (HTTP calls, file deletes, SMTP sends, …)
 *      so the user knows "preview / retry may have real-world effects."
 *
 * Single-input is the default. Connecting a second upstream into a
 * single-input node would silently drop the extra at runtime, so the
 * editor blocks it proactively.
 */

/** Nodes whose backend execute method genuinely consumes 2+ inputs.
 *
 *  2026-05-30 (F6 hygiene): added `lookup`. The audit doc
 *  (docs/NODE_CONFIGURATION_VALIDATION.md §5) flagged that `lookup`
 *  was in `_CONTRACTS` (per-node arity = 2) but missing from this
 *  coarse set. Any canvas code that calls `arityFor()` (the cheap
 *  many/single check) instead of `contractFor()` would silently treat
 *  Lookup as single-input — preventing the second edge the executor
 *  needs.
 */
export const MULTI_INPUT_NODES: ReadonlySet<string> = new Set([
  'join',
  'union',
  'scd2',
  'lookup',
  // 2026-06-03 — Transform admitted to the multi-input club. The backend
  // (fpulse/nodes/transform.py) registers each directly-connected input's
  // output as a named DuckDB table — the first incoming edge becomes
  // `source_table` / `input`, additional incoming edges become
  // sanitized-label tables you can reference in SQL
  // (e.g. `SELECT a.*, b.col FROM source_table a JOIN local_table_source b ON a.id = b.id`).
  // The canvas was the bottleneck — its arity guard blocked second-input
  // drops with a "Connection not allowed" toast, contradicting what the
  // executor could actually do. ConfigPanel's TransformConfig already
  // renders the available-table chips for multi-input cases (lines
  // ~4504-4541) so the UX surface is already there.
  'transform',
]);

/** Source-like nodes that have no upstream — accepting an incoming
 *  edge would be meaningless. */
export const NO_INPUT_NODES: ReadonlySet<string> = new Set([
  'source',
  'csv_source', 'json_source', 'excel_source', 'xml_source',
  'parquet_source', 'db_source', 'api_source', 's3_source', 'kafka_source',
  'ftp_source', 'gsheet_source', 'delta_source', 'file_source',
  'sharepoint_source', 'onedrive_source', 'gdrive_source', 'dropbox_source',
  'box_source', 'azure_blob_source', 'adls_gen2_source', 'gcs_source',
  'webhook_trigger',
  // 2026-05-22 — Microsoft Graph (generic) source.
  'microsoft_graph_source',
  // 2026-05-23 (Y3) — managed local table source.
  'local_table_source',
]);

/** Nodes that touch the outside world: external writes, network calls,
 *  file deletes, message sends. Marked with a small ⚠ badge in the
 *  canvas renderer so the user knows preview / retry / resume may have
 *  real-world consequences. Sources are NOT side-effects (they read).
 *  Pure in-memory transforms are NOT side-effects.
 */
export const SIDE_EFFECT_NODES: ReadonlySet<string> = new Set([
  // Sinks — every sink writes externally
  'csv_sink', 'json_sink', 'excel_sink', 'parquet_sink',
  'db_sink', 's3_sink', 'kafka_sink', 'api_sink', 'email_sink',
  'delta_sink', 'warehouse_sink', 'file_sink',
  'sharepoint_sink', 'onedrive_sink', 'gdrive_sink', 'dropbox_sink',
  'box_sink', 'azure_blob_sink', 'adls_gen2_sink', 'gcs_sink',
  'output', 'destination', 'local_table_sink',
  // Actions — external calls / mutations
  'http_request', 'send_email', 'slack_notify', 'webhook_trigger',
  'copy_data', 'delete_data', 'execute_sql_task', 'file_system', 'fail',
  // Sub-workflow invocation can have arbitrary side effects
  'execute_pipeline',
]);

export type NodeArity = 'none' | 'one' | 'many';

// ── Live-registry derivation (kills frontend/backend contract drift) ─────────
// The backend publishes the authoritative input `arity` + `side_effects` for
// every node on /api/node-types, cached by the app at `window.__fpulse_node_types`.
// We derive from THAT when it's loaded, falling back to the hand-maps below only
// offline (unit tests). Same "registry-authoritative-when-loaded" pattern as
// validateWorkflow — so this file no longer has to be hand-synced with the
// backend forever (2026-06-16: closes the http_request / local_table_sink /
// fail drift at the root).
interface RegistryArity { required: number; optional: number; variadic: boolean }
interface RegistryNodeMeta { type: string; arity?: RegistryArity; side_effects?: string | null; output_kind?: string }

let _regRef: unknown = null;
let _regMap: Map<string, RegistryNodeMeta> | null = null;
function registryMeta(stepType: string): RegistryNodeMeta | undefined {
  let reg: RegistryNodeMeta[] | null = null;
  try {
    const g = typeof window !== 'undefined'
      ? (window as unknown as { __fpulse_node_types?: unknown }).__fpulse_node_types
      : undefined;
    if (Array.isArray(g)) reg = g as RegistryNodeMeta[];
  } catch { reg = null; }
  if (!reg) return undefined;
  if (reg !== _regRef) { _regRef = reg; _regMap = new Map(reg.map((t) => [t.type, t])); }
  return _regMap?.get(stepType);
}
function arityFromContract(a: RegistryArity): NodeArity {
  if (a.variadic) return 'many';
  const total = (a.required || 0) + (a.optional || 0);
  if (total >= 2) return 'many';
  if (total === 0) return 'none';
  return 'one';
}

/** Classify a step type by how many upstream connections it can take. */
export function arityFor(stepType: string): NodeArity {
  const m = registryMeta(stepType);
  if (m?.arity) return arityFromContract(m.arity);
  if (MULTI_INPUT_NODES.has(stepType)) return 'many';
  if (NO_INPUT_NODES.has(stepType)) return 'none';
  return 'one';
}

/** True when this node touches the outside world (write, network, send). */
export function hasSideEffect(stepType: string): boolean {
  const m = registryMeta(stepType);
  if (m && 'side_effects' in m) return m.side_effects != null;
  return SIDE_EFFECT_NODES.has(stepType);
}

/**
 * Formal input contract per stepType. Goes beyond the simple
 * `none/one/many` arity to surface required vs optional inputs and
 * variadic semantics. Used by validateWorkflow for precise errors and
 * by ConfigPanel for inline hints ("Expects 2 inputs: left and right").
 *
 * `required` — minimum incoming edges before the node can execute.
 * `optional` — extra inputs the node will USE if present (e.g. SCD2's
 *              optional current_target snapshot).
 * `variadic` — when true, the node accepts unbounded inputs above
 *              `required`. Union is the canonical example.
 */
export interface InputContract {
  arity: NodeArity;
  required: number;
  optional: number;
  variadic: boolean;
}

const CONTRACTS: Record<string, InputContract> = {
  // Multi-input combinatorics
  join:   { arity: 'many', required: 2, optional: 0, variadic: false },
  lookup: { arity: 'many', required: 2, optional: 0, variadic: false },
  union:  { arity: 'many', required: 2, optional: 0, variadic: true },
  scd2:   { arity: 'many', required: 1, optional: 1, variadic: false },
  // 2026-06-03 — Transform: 1 required (the primary table, registered as
  // `source_table` + `input`) + unbounded variadic extras (each registered
  // by sanitized node-label). Matches the backend's auto-register-all
  // behavior in fpulse/nodes/transform.py lines 50-66.
  transform: { arity: 'many', required: 1, optional: 0, variadic: true },
  // Control-flow nodes that can run with or without input data
  append_variable: { arity: 'one', required: 0, optional: 1, variadic: false },
  filter_array:    { arity: 'one', required: 0, optional: 1, variadic: false },
  validation:      { arity: 'one', required: 0, optional: 1, variadic: false },
  fail:            { arity: 'one', required: 0, optional: 1, variadic: false },
  // Action nodes — 0-or-1
  copy_data:        { arity: 'one', required: 0, optional: 1, variadic: false },
  file_system:      { arity: 'one', required: 0, optional: 1, variadic: false },
  execute_sql_task: { arity: 'one', required: 0, optional: 1, variadic: false },
  // http_request can build its request body from an optional upstream relation
  // (backend contract = required:0, optional:1). It is NOT source-like.
  http_request:     { arity: 'one', required: 0, optional: 1, variadic: false },
  // lookup_activity (Lookup activity): connection/query mode is self-contained (no
  // upstream); upstream mode uses an optional input. So required:0.
  lookup_activity:  { arity: 'one', required: 0, optional: 1, variadic: false },
};

const SOURCE_CONTRACT: InputContract = { arity: 'none', required: 0, optional: 0, variadic: false };
const TRANSFORM_CONTRACT: InputContract = { arity: 'one', required: 1, optional: 0, variadic: false };

/** Returns the formal input contract for a step type. */
export function contractFor(stepType: string): InputContract {
  const m = registryMeta(stepType);
  if (m?.arity) {
    const a = m.arity;
    return {
      arity: arityFromContract(a),
      required: a.required ?? 0,
      optional: a.optional ?? 0,
      variadic: !!a.variadic,
    };
  }
  if (CONTRACTS[stepType]) return CONTRACTS[stepType];
  if (NO_INPUT_NODES.has(stepType)) return SOURCE_CONTRACT;
  return TRANSFORM_CONTRACT;
}

// ────────────────────────────────────────────────────────────────────────────
// 3-class side-effect classification (Review 2 + 2026-05-18 hardening pass)
// ────────────────────────────────────────────────────────────────────────────
//
// A2 lifts the binary `SIDE_EFFECT_NODES` flag into three classes so the
// validator / UI / executor can reason precisely about each node:
//
//   'passthrough'  — input relation returned unchanged; side effect happens
//                    in parallel (every sink: csv_sink, db_sink, kafka_sink,
//                    email_sink, …). Safe to chain downstream off the
//                    "output" because it's the input.
//   'transforming' — produces a new relation that REFLECTS the side effect
//                    (http_request merges response cols; copy_data emits
//                    what was written; get_metadata emits per-column stats;
//                    delete_data files-mode emits per-file results;
//                    execute_sql_task with return_mode=full emits the
//                    result set; file_system emits per-op results).
//   'terminal'     — produces a small descriptive relation but downstream
//                    consumers can't meaningfully continue the original
//                    data chain (send_email / slack_notify / fail).
//
// Pure nodes (filter, aggregate, embedder, …) are not in this map.

export type SideEffectClass = 'passthrough' | 'transforming' | 'terminal';

const SIDE_EFFECT_CLASS: Record<string, SideEffectClass> = {
  // ── pass-through (sinks: input passes through unchanged) ──
  csv_sink: 'passthrough', json_sink: 'passthrough', excel_sink: 'passthrough',
  parquet_sink: 'passthrough', db_sink: 'passthrough', s3_sink: 'passthrough',
  kafka_sink: 'passthrough', api_sink: 'passthrough', email_sink: 'passthrough',
  delta_sink: 'passthrough', warehouse_sink: 'passthrough', file_sink: 'passthrough',
  sharepoint_sink: 'passthrough', onedrive_sink: 'passthrough',
  gdrive_sink: 'passthrough', dropbox_sink: 'passthrough', box_sink: 'passthrough',
  adls_gen2_sink: 'passthrough', azure_blob_sink: 'passthrough', gcs_sink: 'passthrough',
  output: 'passthrough', destination: 'passthrough', local_table_sink: 'passthrough',
  // ── transforming (output reflects the side effect's result) ──
  http_request: 'transforming',
  copy_data: 'transforming',
  delete_data: 'transforming',           // emits removed-row count (rows) or per-file results (files)
  get_metadata: 'transforming',
  execute_sql_task: 'transforming',
  file_system: 'transforming',
  execute_pipeline: 'transforming',      // returns sub-pipeline's final relation
  // ── terminal (no meaningful data continuation) ──
  send_email: 'terminal',
  slack_notify: 'terminal',
  fail: 'terminal',          // raises before returning — ends the pipeline
};

/** Returns the side-effect class for a stepType, or null if pure (no side effect). */
export function sideEffectClassFor(stepType: string): SideEffectClass | null {
  const m = registryMeta(stepType);
  if (m && 'side_effects' in m) {
    const se = m.side_effects;
    return se === 'passthrough' || se === 'transforming' || se === 'terminal' ? se : null;
  }
  return SIDE_EFFECT_CLASS[stepType] ?? null;
}

// ────────────────────────────────────────────────────────────────────────────
// Output-kind classification (2026-06-18) — what the node PRODUCES.
// Mirrors backend/fpulse/ir/node_metadata.py:OUTPUT_KIND. Registry-driven when
// /api/node-types is loaded; this fallback map only covers offline/unit-test.
// ────────────────────────────────────────────────────────────────────────────

export type OutputKind =
  | 'dataset'      // a table of rows for the next node
  | 'variable'     // writes a runtime variable; input passes through
  | 'report'       // statistics / metadata, not a transformable dataset
  | 'branch'       // routes rows / flow to multiple named outputs
  | 'side_effect'  // writes externally (sink) or sends a notification
  | 'terminal'     // ends the run
  | 'control';     // orchestrates execution (loops, waits); no dataset out

const OUTPUT_KIND: Record<string, OutputKind> = {
  set_variable: 'variable', append_variable: 'variable',
  filter_array: 'variable', lookup_activity: 'variable',
  data_profile: 'report', get_metadata: 'report',
  if_condition: 'branch', switch_case: 'branch', conditional_split: 'branch',
  fail: 'terminal',
  wait_delay: 'control', foreach_loop: 'control',
  foreach_pipeline: 'control', until_loop: 'control',
  send_email: 'side_effect', slack_notify: 'side_effect',
};

/** What this node produces downstream. Defaults: external writers (sinks /
 *  passthrough side-effect) are 'side_effect'; everything else 'dataset'. */
export function outputKindFor(stepType: string): OutputKind {
  const m = registryMeta(stepType);
  const ok = m?.output_kind;
  if (ok === 'dataset' || ok === 'variable' || ok === 'report' || ok === 'branch'
      || ok === 'side_effect' || ok === 'terminal' || ok === 'control') {
    return ok;
  }
  if (OUTPUT_KIND[stepType]) return OUTPUT_KIND[stepType];
  if (sideEffectClassFor(stepType) === 'passthrough') return 'side_effect';
  return 'dataset';
}

// ────────────────────────────────────────────────────────────────────────────
// A8 — Humanized side-effect labels (per-action text for the badge tooltip)
// ────────────────────────────────────────────────────────────────────────────
//
// Replaces the generic "preview/retry may have consequences" tooltip with
// a specific phrase the user can read at a glance. Sinks get a single
// "Writes data" line; per-vendor flavor is in the node label already.

const SIDE_EFFECT_LABEL: Record<string, string> = {
  // sinks
  csv_sink: 'Writes a CSV file',
  json_sink: 'Writes a JSON file',
  excel_sink: 'Writes an Excel file',
  parquet_sink: 'Writes a Parquet file',
  db_sink: 'Writes rows to a database',
  s3_sink: 'Uploads to S3 / MinIO',
  kafka_sink: 'Publishes messages to Kafka',
  api_sink: 'POSTs data to an API',
  email_sink: 'Sends an email with attachment',
  delta_sink: 'Writes to a Delta Lake table',
  warehouse_sink: 'Writes to a data warehouse',
  file_sink: 'Writes a file',
  sharepoint_sink: 'Uploads to SharePoint',
  onedrive_sink: 'Uploads to OneDrive',
  gdrive_sink: 'Uploads to Google Drive',
  dropbox_sink: 'Uploads to Dropbox',
  box_sink: 'Uploads to Box',
  adls_gen2_sink: 'Writes to Azure Data Lake',
  azure_blob_sink: 'Writes to Azure Blob Storage',
  gcs_sink: 'Writes to Google Cloud Storage',
  output: 'Writes data',
  destination: 'Writes data',
  local_table_sink: 'Writes a managed table',
  // transforming
  http_request: 'Calls an external API',
  copy_data: 'Copies data to a database',
  delete_data: 'Deletes rows or files',
  get_metadata: 'Inspects schema and row stats',
  execute_sql_task: 'Runs SQL on a database',
  file_system: 'Modifies the filesystem',
  execute_pipeline: 'Runs another pipeline',
  // terminal
  send_email: 'Sends an email',
  slack_notify: 'Posts to a Slack channel',
  fail: 'Fails the pipeline',
};

/** Returns a per-action human label, or null if the node has no side effect. */
export function sideEffectLabel(stepType: string): string | null {
  return SIDE_EFFECT_LABEL[stepType] ?? null;
}

// ────────────────────────────────────────────────────────────────────────────
// A3 — Schema stability (Review 2 + Review 3 #A)
// ────────────────────────────────────────────────────────────────────────────
//
// Tells the frontend how it can rely on the upstream schema cache:
//
//   'static'    — output schema is deterministic from params alone.
//                 The schema can be cached and reused indefinitely.
//                 Trivial transforms (rename, select, typecast,
//                 derived_column) plus most well-defined ops (filter,
//                 sort, dedup, schema_mapper) live here.
//   'inference' — schema is DuckDB-inferred from a data sample at first
//                 read. Safe to cache after the first successful run;
//                 may change if upstream data shape changes (e.g. a CSV
//                 column type changes from int → string after a fresh
//                 source file is uploaded).
//   'dynamic'   — output schema is fully data-dependent and only known
//                 at runtime. Column dropdowns downstream of these
//                 nodes must either rebuild after every run or fall back
//                 to a free-text input until at least one execution has
//                 produced a cached schema.
//
// Sources without param-driven schemas (csv_source, json_source, …) are
// 'inference'. Sources with param-driven schemas (db_source query,
// parquet_source columns) are 'static' in principle but treated as
// 'inference' because the user's query/columns string is opaque to the
// frontend until the backend parses it.

export type SchemaStability = 'static' | 'inference' | 'dynamic';

const SCHEMA_STABILITY: Record<string, SchemaStability> = {
  // dynamic — schema only known after running against real data
  pivot: 'dynamic',                 // distinct values of pivot_column become cols
  unpivot: 'dynamic',               // depends on `columns` matching actual upstream
  transform: 'dynamic',             // arbitrary SQL projection
  api_source: 'dynamic',            // response shape unknown until first call
  webhook_trigger: 'dynamic',       // body shape unknown until first POST
  code_script: 'dynamic',           // user Python returns arbitrary df
  flatten_explode: 'dynamic',       // nested fields unknown until probed
  // inference — typed at first read, DuckDB sample-driven
  csv_source: 'inference',
  json_source: 'inference',
  xml_source: 'inference',
  excel_source: 'inference',
  gsheet_source: 'inference',
  ftp_source: 'inference',
  s3_source: 'inference',
  db_source: 'inference',           // user query is opaque to the frontend
  // typed sources (Parquet, Delta, Kafka) are inference too — type info
  // exists but reaches the frontend only after the first probe
  parquet_source: 'inference',
  delta_source: 'inference',
  kafka_source: 'inference',
};

/** Returns the schema-stability tier for a stepType (defaults to 'static'). */
export function schemaStabilityFor(stepType: string): SchemaStability {
  return SCHEMA_STABILITY[stepType] ?? 'static';
}

// ────────────────────────────────────────────────────────────────────────────
// Reserved metadata column registry (A1, Review 2)
// ────────────────────────────────────────────────────────────────────────────
//
// Single canonical list of system-injected columns. Any node that wants
// to emit metadata MUST register its column here so downstream consumers
// (validators, sinks, schema mappers) can detect and optionally strip
// them. Underscore prefix is reserved at the policy level.

export const RESERVED_METADATA_COLUMNS: ReadonlySet<string> = new Set([
  // foreach_loop
  '_batch_index', '_batch_total',
  // conditional_split
  '_split_output',
  // data_quality (tag/split modes)
  '__dq_passed', '__dq_score',
  // llm_guardrail (tag mode)
  '__guardrail_flags',
  // validate
  'is_valid',
  // kafka_source
  '_kafka_offset', '_kafka_partition', '_kafka_topic',
]);

/** True if `column` is a system-reserved metadata column. */
export function isReservedMetadataColumn(column: string): boolean {
  return RESERVED_METADATA_COLUMNS.has(column);
}

/** Returns the subset of `columns` that are reserved metadata. */
export function reservedMetadataIn(columns: string[]): string[] {
  return columns.filter((c) => RESERVED_METADATA_COLUMNS.has(c));
}
