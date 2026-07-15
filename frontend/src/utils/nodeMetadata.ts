/**
 * Canonical per-node contract metadata — frontend mirror.
 *
 * Mirrors `backend/fpulse/ir/node_metadata.py`. Same input-contract
 * (required / optional / variadic) and side-effect class shape. This
 * file exists so the two halves of the node contract are side-by-side
 * machine-readable (audit O5).
 *
 * Why two copies:
 *   * The backend version is authoritative; the conformance test
 *     (`backend/tests/test_node_conformance.py`) pins it.
 *   * The frontend ships separately and needs the same data at
 *     runtime to draw correct input handles, validate workflows
 *     before save, and classify side-effects in the editor.
 *   * A future build step can codegen this from the backend module
 *     when `/api/node-types?detail=true` is wired into the build —
 *     until then, keep both files in lockstep when adding a new
 *     node type and the backend conformance test will fail the CI
 *     build if you miss the backend half.
 *
 * Note: there's already `utils/nodeArity.ts` in the codebase. It's
 * the older shape; this file is the new authoritative one. Migration
 * of consumers from nodeArity to this module is a separate sweep.
 */

export interface InputContract {
  /** Minimum incoming edges required before the node can execute. */
  required: number;
  /** Extra inputs the node USES if present. */
  optional: number;
  /** When true, the node accepts unbounded inputs above `required`. */
  variadic: boolean;
}

/** Nodes that genuinely consume 2+ inputs. */
export const MULTI_INPUT_NODES: ReadonlySet<string> = new Set([
  'join', 'union', 'scd2', 'lookup',
]);

/** Source-like nodes that take no upstream. */
export const NO_INPUT_NODES: ReadonlySet<string> = new Set([
  'source',
  'csv_source', 'json_source', 'excel_source', 'xml_source',
  'parquet_source', 'db_source', 'api_source', 's3_source', 'kafka_source',
  'ftp_source', 'gsheet_source', 'delta_source', 'file_source',
  'sharepoint_source', 'onedrive_source', 'gdrive_source', 'dropbox_source',
  'box_source', 'azure_blob_source', 'adls_gen2_source', 'gcs_source',
  'webhook_trigger', 'http_request',
  // 2026-05-22 — Microsoft Graph (generic) source.
  'microsoft_graph_source',
  // 2026-05-23 (Y3) — managed local table source.
  'local_table_source',
]);

/** Explicit overrides for nodes that don't fit "1 in, 1 out". */
const CONTRACTS: Record<string, InputContract> = {
  // Set-combiners
  join:   { required: 2, optional: 0, variadic: false },
  lookup: { required: 2, optional: 0, variadic: false },
  union:  { required: 2, optional: 0, variadic: true },
  scd2:   { required: 1, optional: 1, variadic: false },
  // Control-flow that can run with or without data
  append_variable:  { required: 0, optional: 1, variadic: false },
  filter_array:     { required: 0, optional: 1, variadic: false },
  validation:       { required: 0, optional: 1, variadic: false },
  fail:             { required: 0, optional: 1, variadic: false },
  // Action nodes — input is optional
  copy_data:        { required: 0, optional: 1, variadic: false },
  file_system:      { required: 0, optional: 1, variadic: false },
  execute_sql_task: { required: 0, optional: 1, variadic: false },
  http_request:     { required: 0, optional: 1, variadic: false },
};

const SOURCE_CONTRACT: InputContract = { required: 0, optional: 0, variadic: false };
const TRANSFORM_CONTRACT: InputContract = { required: 1, optional: 0, variadic: false };

/**
 * Return the input contract for a step type. Mirrors
 * `backend/fpulse/ir/node_metadata.py:contract_for`.
 */
export function contractFor(stepType: string): InputContract {
  if (stepType in CONTRACTS) return CONTRACTS[stepType];
  if (NO_INPUT_NODES.has(stepType)) return SOURCE_CONTRACT;
  return TRANSFORM_CONTRACT;
}

// ── Side-effect classification ─────────────────────────────────────────
//
// Three classes:
//   passthrough  — input relation passes through unchanged; the side
//                  effect (write/send/publish) happens in parallel.
//   transforming — produces a new relation that reflects the side
//                  effect.
//   terminal     — emits a small descriptive relation; downstream
//                  can't meaningfully chain.
// Pure nodes (filter, aggregate, …) are NOT in this map.

export type SideEffectClass = 'passthrough' | 'transforming' | 'terminal';

export const SIDE_EFFECT_CLASS: Record<string, SideEffectClass> = {
  // Sinks — passthrough
  csv_sink: 'passthrough', json_sink: 'passthrough', excel_sink: 'passthrough',
  parquet_sink: 'passthrough', db_sink: 'passthrough', s3_sink: 'passthrough',
  kafka_sink: 'passthrough', api_sink: 'passthrough', email_sink: 'passthrough',
  delta_sink: 'passthrough', warehouse_sink: 'passthrough', file_sink: 'passthrough',
  sharepoint_sink: 'passthrough', onedrive_sink: 'passthrough',
  gdrive_sink: 'passthrough', dropbox_sink: 'passthrough', box_sink: 'passthrough',
  adls_gen2_sink: 'passthrough', azure_blob_sink: 'passthrough', gcs_sink: 'passthrough',
  webhook_sink: 'passthrough',
  output: 'passthrough', destination: 'passthrough',
  // 2026-05-23 (Y3): managed local Parquet sink — passthrough.
  local_table_sink: 'passthrough',
  // Transforming — output reflects the side effect
  http_request: 'transforming',
  copy_data: 'transforming',
  delete_data: 'transforming',
  get_metadata: 'transforming',
  execute_sql_task: 'transforming',
  file_system: 'transforming',
  execute_pipeline: 'transforming',
  // Terminal — no meaningful data continuation
  send_email: 'terminal',
  slack_notify: 'terminal',
  fail: 'terminal',
};

export function sideEffectClassFor(stepType: string): SideEffectClass | null {
  return SIDE_EFFECT_CLASS[stepType] ?? null;
}

export function hasSideEffect(stepType: string): boolean {
  return stepType in SIDE_EFFECT_CLASS;
}
