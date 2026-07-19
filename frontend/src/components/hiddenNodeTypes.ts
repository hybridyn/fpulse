// Single source of truth for legacy / collapsed node types that no longer
// appear in the Modules palette. We collapsed all per-format sources/sinks
// into the generic `source` / `destination` / `copy_data` nodes — but the
// backend AI suggester and older fixtures still emit the legacy keys, so
// every consumer (palette filter, ghost-suggestion remap, import path)
// needs the same list.

export const HIDDEN_TYPES: ReadonlySet<string> = new Set([
  'rest_connector',
  'file_source', 'file_sink', 'csv_source', 'json_source', 'parquet_source',
  'excel_source', 'xml_source', 'csv_sink', 'json_sink', 'excel_sink', 'output',
  'db_source', 'db_sink', 'api_source', 'api_sink',
  's3_source', 's3_sink', 'azure_blob_source', 'azure_blob_sink',
  'gcs_source', 'gcs_sink', 'sharepoint_source', 'sharepoint_sink',
  'onedrive_source', 'onedrive_sink', 'kafka_source', 'kafka_sink',
  'ftp_source', 'ftp_sink', 'gsheet_source', 'gsheet_sink',
  'delta_source', 'delta_sink', 'vector_sink', 'email_sink',
  'webhook_sink',
  'gdrive_source', 'gdrive_sink', 'dropbox_source', 'dropbox_sink',
  'box_source', 'box_sink',
  'adls_gen2_source', 'adls_gen2_sink',
  'rename', 'typecast',
  'validate',
  // 2026-06-15 (node-audit): 'upsert' ("Keep Latest") hidden — it runs the
  // exact same ROW_NUMBER(PARTITION BY key ORDER BY ...) WHERE rn=1 as
  // Deduplicate (keep last), so it's a strict subset. Deduplicate covers it
  // (with keep-first/last + dual unique/duplicate outputs). The backend node
  // stays registered so existing pipelines keep running.
  'upsert',
  // 2026-06-15 (control-flow alignment): 'switch_case' (old single-case filter) hidden —
  // "Switch" is now conditional_split (the real multi-output brancher).
  // 'split_out' hidden — duplicate preset of flatten_explode ("Flatten").
  // 'validation' ("Wait for File", the Validation activity) hidden — it
  // was leaking uncurated into Control Flow via the reconciliation extras
  // path; kept off the OSS palette to hold the clean control-flow set. Backend nodes
  // all stay registered for old pipelines.
  'switch_case', 'split_out', 'validation',
  // 2026-06-15: 'code_script' gated to Plus — it runs user Python in-process
  // (not a true sandbox), so it's a trusted-only/advanced feature. Hidden from
  // the OSS palette; backend node stays registered (Plus + old pipelines).
  'code_script',
  // 2026-06-11 — conditional_split UN-hidden: it's now the fully-wired
  // multi-output brancher (named output handles + central routing). It's
  // the designated way to branch a pipeline, so it belongs in the palette.
  'until_loop', 'filter_array', 'append_variable',
  'warehouse_sink', 'materialize',
  'saas_source', 'saas_sink', 'saas_connector',
  'warehouse_source', 'jdbc_source', 'jdbc_sink',
  'cdc_source', 'openapi_source', 'vector_source',
  'warehouse_source_jdbc', 'warehouse_sink_jdbc',
  'load_csv', 'load_json', 'load_parquet', 'load_excel',
  'write_csv', 'write_json', 'write_parquet', 'write_excel',
  // Z11 (2026-05-23) — Microsoft Graph is a CONNECTOR, not a dedicated
  // node. The Connections page is the right surface to create/manage it;
  // pipelines read from MS Graph via the generic `source` node with
  // `connector_type='microsoft_graph'`. The dedicated `microsoft_graph_source`
  // node still ships for back-compat (existing pipelines keep working)
  // but is hidden from the palette so users don't see it twice.
  'microsoft_graph_source',
]);

// Types that read data — remap to the generic `source` node.
const SOURCE_LIKE = /(_source|^load_|^read_)/;
// Types that write data — remap to the generic `destination` node.
const SINK_LIKE = /(_sink|^write_|^output$)/;

/** Map a hidden legacy type to the generic visible equivalent, or null
 *  if it cannot be safely remapped (caller should drop the suggestion). */
export function remapHiddenType(type: string): { type: string; label: string } | null {
  if (!HIDDEN_TYPES.has(type)) return null;
  if (SOURCE_LIKE.test(type)) return { type: 'source', label: 'Source' };
  if (SINK_LIKE.test(type)) return { type: 'destination', label: 'Destination' };
  return null;
}

/**
 * Strict whitelist of node types that are allowed to appear as a ghost
 * suggestion. The AI suggester occasionally returns labels like "Load CSV"
 * even when the backing type is generic — those slipped through the
 * HIDDEN_TYPES filter and confused users (we removed CSV from the palette).
 *
 * Mirrors the visible MODULES palette in ModulesPanel.tsx. Keep in sync.
 */
export const VALID_GHOST_TYPES: ReadonlySet<string> = new Set([
  // Data Movement
  'source', 'destination', 'copy_data',
  // Transform
  'transform', 'filter', 'derived_column', 'schema_mapper',
  'aggregate', 'sort', 'deduplicate', 'sample', 'pivot', 'unpivot',
  // Combine
  'join', 'union', 'lookup',
  'merge', 'cdc_merge', 'cross_join',
  // Control Flow (2026-06-15 control-flow alignment: Switch = conditional_split;
  // ForEach = foreach_pipeline; Lookup = lookup_activity)
  'foreach', 'foreach_pipeline', 'if_condition', 'conditional_split',
  'wait', 'wait_delay', 'set_variable', 'lookup_activity',
  'execute_pipeline', 'fail', 'retry_handler',
  // Action
  // NB: `webhook_trigger` is intentionally absent — it was removed from the
  // palette (ModulesPanel) and is deprecated on the backend (no registered
  // impl; replaced_by `api_source`). Leaving it in the ghost whitelist let
  // the AI suggest a node users can't actually add. 2026-06-08 node audit.
  'http_request', 'shell', 'sql_exec',
  'send_email', 'slack_notify', 'create_ticket',
  'sticky_note',
  // AI / Semantic
  'ai_enrich', 'embedder', 'guardrail',
]);

/**
 * Drop any AI ghost suggestion whose label hints at a removed connector
 * (CSV, JSON, Parquet, …). Cheap defence against AI labels overriding
 * the type-based remap.
 */
const REMOVED_LABEL_HINTS = /\b(csv|json|parquet|excel|xlsx|xml|avro|orc)\b/i;

export function isLabelForRemovedConnector(label: string | undefined): boolean {
  if (!label) return false;
  return REMOVED_LABEL_HINTS.test(label);
}
