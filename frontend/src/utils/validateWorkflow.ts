// validateWorkflow.ts — Pre-execution validation for F-Pulse pipeline nodes

import { contractFor, RESERVED_METADATA_COLUMNS, sideEffectClassFor } from './nodeArity';
import { findCycleNodeIds } from './graphCycles';

export interface ValidationIssue {
  nodeId: string;
  nodeLabel: string;
  level: 'error' | 'warning';
  message: string;
  field?: string;
}

// ---------------------------------------------------------------------------
// Field-requirement maps per stepType
// ---------------------------------------------------------------------------

/** Each entry: stepType -> array of required field names (from node.data.params).
 *
 * 2026-05-22 drift fix — the 2026-05-22 node audit caught several
 * frontend required-field maps that no longer matched the backend node
 * implementations. Each correction is noted at the callsite. The
 * long-term fix is to drive validation from /api/node-types (the
 * backend NodeDefinition.param_schema) — these maps are scheduled to
 * be deleted in the Phase 2 frontend-validation refactor.
 */
const SOURCE_REQUIREMENTS: Record<string, string[]> = {
  api_source: ['url'],
  rest_api: ['url'],
  // 2026-05-22: `db_source` actually requires either `connection_id`
  // (saved connection) OR `connection_string` (inline) plus a query
  // when source_mode='query', or a table when source_mode='table'.
  // Frontend validator can't enforce the OR cleanly from this map; the
  // generic-source / generic-destination branch at the bottom of this
  // file does the proper hasConn-or-hasFileInput check for the canonical
  // generic `source` / `destination` types. Leaving the legacy entries
  // empty here so they don't trip valid `connection_id`-driven flows.
  db_source: [],
  database: [],
  csv_source: ['file_path'],
  json_source: ['file_path'],
  excel_source: ['file_path'],
  xml_source: ['file_path'],
  parquet_source: ['file_path'],
  file_source: ['file_path'],
  file_sink: ['file_path'],
  file_upload: ['file_path'],
  csv: ['file_path'],
  excel: ['file_path'],
  json: ['file_path'],
  xml: ['file_path'],
  parquet: ['file_path'],
  ftp_source: ['host', 'path'],
  sftp: ['host', 'path'],
  s3_source: ['bucket', 'key'],
  s3: ['bucket', 'key'],
  // 2026-05-22: backend cloud-storage nodes (cloud_storage.py) all read
  // `container` + `path` for Azure variants, and `bucket` + `path` for
  // GCS — never `blob_path`, `filesystem`, or `object_path`. Old
  // labels left strict markers on fields that don't exist on the
  // node, blocking valid pipelines.
  azure_blob: ['container', 'path'],
  azure_blob_source: ['container', 'path'],
  azure_blob_sink: ['container', 'path'],
  adls_gen2_source: ['container', 'path'],
  adls_gen2_sink: ['container', 'path'],
  gcs: ['bucket', 'path'],
  gcs_source: ['bucket', 'path'],
  gcs_sink: ['bucket', 'path'],
  kafka_source: ['topic', 'bootstrap_servers'],
  kafka: ['topic', 'bootstrap_servers'],
  gsheet_source: ['spreadsheet_id'],
  delta_source: ['path'],
  graphql: ['url', 'query'],
  sharepoint_source: ['site_url'],
  sharepoint_sink: ['site_url'],
  onedrive_source: ['path'],
  onedrive_sink: ['path'],
  gdrive_source: ['file_id'],
  gdrive_sink: ['folder_id'],
  dropbox_source: ['path'],
  dropbox_sink: ['path'],
  box_source: ['file_id'],
  box_sink: ['folder_id'],
  // 2026-05-22: see db_source note above — db_sink also goes through
  // the generic `destination` codepath in nodes/generic.py and accepts
  // a saved `connection_id` instead of an inline connection_string.
  // The OR check is handled in the generic source/destination branch.
  db_sink: [],
  output: ['file_path'],
};

const TRANSFORM_REQUIREMENTS: Record<string, string[]> = {
  filter: ['condition'],
  // 2026-05-22: backend AggregateNode reads `functions` (not
  // `aggregations`) and treats `group_by` as OPTIONAL — an empty
  // group_by produces a single global-aggregate row, which is a
  // legitimate use case (e.g. "count all rows"). So the only
  // hard requirement is `functions`. See aggregate.py:53 + the
  // module docstring "No GROUP BY = global aggregation."
  aggregate: ['functions'],
  sql_transform: ['query'],
  // 2026-05-22: backend TransformNode (nodes/transform.py:33) reads
  // `expression`, not `query`. Old label here caused valid SQL
  // transforms to be flagged as missing-required.
  transform: ['expression'],
  formula: ['expression'],
  // 2026-05-22: backend DerivedColumnNode (nodes/activities.py:479)
  // reads `columns` (a list of {name, expression} objects), not a
  // single `expression` string.
  derived_column: ['columns'],
  // 2026-05-22: backend PivotNode (activities.py:587) reads
  // `pivot_column` + `value_column`, not a single `column`.
  pivot: ['pivot_column', 'value_column'],
  // 2026-05-22: backend UnpivotNode (activities.py:633) reads
  // `columns` (a list of value-columns), not `column`.
  unpivot: ['columns'],
  // 2026-06-11: removed stale `conditional_split: ['condition']` — the
  // backend ConditionalSplitNode reads `conditions` (an array of
  // {name, condition}), never a top-level `condition`. The real per-branch
  // validation now lives in the dedicated conditional_split block below.
  // 2026-06-11: backend FlattenExplodeNode raises without a column.
  flatten_explode: ['column'],
  // 2026-06-10: backend LookupNode (activities.py:802) reads ONLY
  // `lookup_key` — it raises "Lookup node requires a lookup_key" when
  // empty. The lookup table itself is the node's SECOND canvas input
  // (contractFor 'lookup' = 2 required inputs); `lookup_source` was a
  // phantom field the backend never read, so requiring it here blocked
  // users on a no-op field while the real requirement went unchecked.
  lookup: ['lookup_key'],
};

const FLOW_REQUIREMENTS: Record<string, string[]> = {
  if_condition: ['condition'],
  switch_case: ['expression'],
  switch: ['expression'],
  foreach_loop: ['items'],
  for_each: ['items'],
  execute_pipeline: ['pipeline_id'],
  // 2026-06-11 — per-item loop: needs a sub-pipeline to run for each row.
  foreach_pipeline: ['pipeline_id'],
  // 2026-06-11 — Lookup activity. output_var is the capture
  // name downstream steps reference as {{ $vars.<name>.firstRow.<col> }};
  // without it the lookup runs but nothing can consume the result.
  lookup_activity: ['output_var'],
};

const ACTION_REQUIREMENTS: Record<string, string[]> = {
  http_request: ['url'],
  webhook_trigger: [],      // special: needs url OR path
  webhook: [],              // special: needs url OR path
  code_script: ['code'],
  code: ['code'],
  send_email: ['to'],
  email: ['to'],
  slack_notify: [],         // special: webhook_url required (channel optional) — see validate()
  slack: [],                // special: webhook_url required (channel optional) — see validate()
};

// ---------------------------------------------------------------------------
// Schema-driven required fields (C3 / Phase 2, 2026-06-15)
// ---------------------------------------------------------------------------
//
// The backend node registry (`window.__fpulse_node_types`, the same data the
// config panel renders from) is the SINGLE SOURCE OF TRUTH for which fields a
// node requires — each param_schema entry carries `required: true`. When the
// registry is loaded (always, in the running app) the validator derives
// required-field checks from it instead of the hand-maintained maps above,
// which is what kills the recurring drift the maps tried to chase by hand
// (e.g. `filter: ['condition']` / `api_source: ['url']` were over-strict —
// the backend marks NOTHING required there because each has a valid OR
// alternative). The maps remain only as an OFFLINE FALLBACK (e.g. unit tests
// or first paint before the registry fetch resolves) so behaviour without the
// registry is byte-identical to before.
function requiredFieldsFromSchema(stepType: string): string[] | null {
  try {
    const reg = (globalThis as any).__fpulse_node_types;
    const arr = Array.isArray(reg) ? reg : (reg && reg.node_types) || null;
    if (!arr) return null;
    const def = arr.find((n: any) => (n?.step_type ?? n?.type) === stepType);
    if (!def || !Array.isArray(def.param_schema)) return null;
    return def.param_schema
      .filter((p: any) => p?.required === true)
      .map((p: any) => String(p.name));
  } catch {
    return null;
  }
}

/** True when the backend registry has a param_schema for this stepType, so
 *  the legacy required-field maps should defer to it. */
function schemaLoadedFor(stepType: string): boolean {
  return requiredFieldsFromSchema(stepType) !== null;
}

// Universal nodes: F-Pulse no longer classifies nodes as source-only or
// sink-only. Any node can sit at any position in the graph, so we don't
// emit "no outgoing connection" or "no incoming connection" warnings
// based on the step type. Disconnected-subgraph detection below still
// catches nodes that were dropped but never wired.

// 2026-05-19 (P2 #19 of PAGE_BY_PAGE_AUDIT.md): single source of truth
// for "is this field required?" so config-panel renderers can decide
// whether to draw a `*` marker without hand-maintaining a parallel list.
// Before this helper a marker was hand-typed in JSX; drift was guaranteed
// as new node types were added.
//
// Pure function over the four requirement maps. Returns true if the
// validator considers `field` mandatory for `stepType` — either as an
// always-required field, or via the "one-of" specials (file vs url for
// file-based sources, group_by vs aggregations for aggregate, etc.) which
// are listed via `isOneOfFieldFor()` below.
export function isRequiredFieldFor(stepType: string, field: string): boolean {
  // C3: prefer the backend param_schema when loaded (single source of truth);
  // fall back to the hand-maps offline. Keeps `*` markers in sync with the
  // schema-driven validation below.
  const schemaReq = requiredFieldsFromSchema(stepType);
  if (schemaReq !== null) return schemaReq.includes(field);
  const lists = [SOURCE_REQUIREMENTS, TRANSFORM_REQUIREMENTS, FLOW_REQUIREMENTS, ACTION_REQUIREMENTS];
  for (const map of lists) {
    const arr = map[stepType];
    if (arr && arr.includes(field)) return true;
  }
  return false;
}

// One-of field groups handled by the special-case branches of `validate()`
// below. Useful for renderers that want to mark e.g. both `file_path` and
// `url` as "one of these is required" rather than two independent stars.
export const ONE_OF_FIELD_GROUPS: Record<string, string[][]> = {
  csv_source:        [['file_path', 'url']],
  json_source:       [['file_path', 'url']],
  parquet_source:    [['file_path', 'url']],
  excel_source:      [['file_path', 'url']],
  file_source:       [['file_path', 'url']],
  // 2026-05-22: removed stale [group_by, aggregations] OR-group —
  // backend treats `group_by` as OPTIONAL and reads aggregate
  // specs from `functions`. The single hard requirement
  // (`functions`) now lives in TRANSFORM_REQUIREMENTS above and
  // doesn't need a special branch.
  webhook:           [['url', 'path']],
  webhook_trigger:   [['url', 'path']],
  // slack_notify/slack: webhook_url is HARD-required (the node POSTs to it);
  // `channel` is an optional override. Not a one-of group — see the
  // slack branch in validate() and the `*` in SlackNotifyConfig. (2026-06-17)
};

export function isOneOfFieldFor(stepType: string, field: string): boolean {
  const groups = ONE_OF_FIELD_GROUPS[stepType];
  if (!groups) return false;
  return groups.some((g) => g.includes(field));
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function hasParam(params: Record<string, any> | undefined, field: string): boolean {
  if (!params) return false;
  const val = params[field];
  if (val === undefined || val === null || val === '') return false;
  if (Array.isArray(val) && val.length === 0) return false;
  return true;
}

function hasAnyParam(params: Record<string, any> | undefined, fields: string[]): boolean {
  return fields.some((f) => hasParam(params, f));
}

// ---------------------------------------------------------------------------
// Main validation
// ---------------------------------------------------------------------------

// ── Transform multi-input SQL helpers (2026-06-10 node-contract pass) ──
// Mirror the backend alias derivation (fpulse/nodes/transform.py): a node
// label becomes a DuckDB table name by lowercasing and replacing every
// non-[a-z0-9_] run with '_'. Used to validate that a Transform's SQL only
// references its directly-connected inputs.
function sanitizeAlias(label: string): string {
  return (label || '').toLowerCase().replace(/[^a-z0-9_]/g, '_').replace(/^_+|_+$/g, '');
}

// DuckDB table-valued functions that legitimately follow FROM/JOIN and must
// NOT be flagged as unknown tables.
const SQL_TABLE_FUNCS = new Set([
  'read_csv', 'read_csv_auto', 'read_parquet', 'read_json', 'read_json_auto',
  'read_ndjson', 'range', 'generate_series', 'unnest', 'values', 'glob', 'query',
]);

function extractSqlTableRefs(sql: string): string[] {
  const refs: string[] = [];
  const re = /\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(sql)) !== null) {
    if (!SQL_TABLE_FUNCS.has(m[1].toLowerCase())) refs.push(m[1]);
  }
  return refs;
}

function extractCteNames(sql: string): string[] {
  const names: string[] = [];
  const re = /(?:\bwith|,)\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(sql)) !== null) names.push(m[1]);
  return names;
}

export function validateWorkflow(nodes: any[], edges: any[], pipelineParameters?: Array<{ name: string }>, currentPipelineId?: string | null): ValidationIssue[] {
  const issues: ValidationIssue[] = [];

  // Pre-compute the set of declared parameter names so we can flag any
  // ${param.<name>} reference in step params that doesn't have a backing
  // declaration. Pipelines that don't use parameters are unaffected.
  const declaredParams = new Set((pipelineParameters || []).map((p) => p.name));

  // Filter out sticky notes — they don't count as pipeline activities
  const activityNodes = nodes.filter(n => n.type !== 'stickyNote' && n.data?.stepType !== 'sticky_note');

  if (activityNodes.length === 0) {
    issues.push({
      nodeId: '__pipeline__',
      nodeLabel: 'Pipeline',
      level: 'error',
      message: 'Pipeline must have at least one activity. Add nodes from the Activities panel.',
    });
    return issues;
  }

  // Build adjacency lookups
  const outgoing = new Map<string, string[]>();
  const incoming = new Map<string, string[]>();
  for (const node of nodes) {
    outgoing.set(node.id, []);
    incoming.set(node.id, []);
  }
  for (const edge of edges) {
    outgoing.get(edge.source)?.push(edge.target);
    incoming.get(edge.target)?.push(edge.source);
  }
  const nodeById = new Map<string, any>();
  for (const n of nodes) nodeById.set(n.id, n);

  // --- Deactivation shadow ---
  // A node is "shadowed" when every one of its incoming edges comes from a
  // deactivated or already-shadowed node. Shadowed nodes will be skipped at
  // runtime (the backend returns `skipped` for them), so the UI surfaces a
  // warning here and we also skip required-field checks below — complaining
  // about a missing file_path on a node that can't ever run is noise.
  const deactivated = new Set<string>();
  for (const node of nodes) {
    if (node.data?.params?._settings?.deactivated) {
      deactivated.add(node.id);
    }
  }
  const shadowed = new Set<string>();
  // Topological-ish pass: repeat until fixed point. N is small (< a few hundred
  // nodes in practice) so the O(N²) worst case is fine.
  let changed = true;
  while (changed) {
    changed = false;
    for (const node of nodes) {
      if (deactivated.has(node.id) || shadowed.has(node.id)) continue;
      const parents = incoming.get(node.id) || [];
      if (parents.length === 0) continue;
      const allBlocked = parents.every(
        (p) => deactivated.has(p) || shadowed.has(p),
      );
      if (allBlocked) {
        shadowed.add(node.id);
        changed = true;
      }
    }
  }

  // Per-node validation
  for (const node of nodes) {
    const stepType: string = node.data?.stepType || '';
    const params: Record<string, any> | undefined = node.data?.params;
    const label: string = node.data?.label || stepType || node.id;

    // Deactivated nodes don't execute — don't flood the user with
    // "missing required field" errors on something they turned off.
    // We also don't need to recurse shadowed warnings here; those are
    // appended as their own issues below the per-node loop.
    if (deactivated.has(node.id)) {
      continue;
    }

    // Shadowed nodes won't execute either — same reasoning as above.
    // The single "upstream deactivated" warning is added later.
    if (shadowed.has(node.id)) {
      continue;
    }

    // --- Source requirements ---
    if (SOURCE_REQUIREMENTS[stepType]) {
      // file-based sources accept either file_path or url
      const fileBased = new Set([
        'file_upload', 'file_source', 'file_sink', 'csv', 'excel', 'json', 'xml', 'parquet',
        'csv_source', 'json_source', 'excel_source', 'xml_source', 'parquet_source', 'output',
      ]);
      if (fileBased.has(stepType)) {
        if (!hasAnyParam(params, ['file_path', 'url'])) {
          issues.push({
            nodeId: node.id,
            nodeLabel: label,
            level: 'error',
            message: 'Missing required field: file_path or url',
            field: 'file_path',
          });
        }
      } else if (!schemaLoadedFor(stepType)) {
        // Legacy fallback only — when the registry is loaded the schema-driven
        // check at the end of the loop owns required-field validation (C3).
        for (const field of SOURCE_REQUIREMENTS[stepType]) {
          if (!hasParam(params, field)) {
            issues.push({
              nodeId: node.id,
              nodeLabel: label,
              level: 'error',
              message: `Missing required field: ${field}`,
              field,
            });
          }
        }
      }
    }

    // --- Transform requirements ---
    // 2026-05-22: aggregate no longer needs a special-case branch. The
    // hard requirement (`functions`) is now in TRANSFORM_REQUIREMENTS
    // and the optional `group_by` is enforced (or not) by the backend.
    if (TRANSFORM_REQUIREMENTS[stepType] !== undefined && !schemaLoadedFor(stepType)) {
      // Legacy fallback only — schema-driven check (C3) owns this when loaded.
      for (const field of TRANSFORM_REQUIREMENTS[stepType]) {
        if (!hasParam(params, field)) {
          issues.push({
            nodeId: node.id,
            nodeLabel: label,
            level: 'error',
            message: `Missing required field: ${field}`,
            field,
          });
        }
      }
    }

    // --- Generic Source / Destination / Copy Data ---
    // These are "smart" nodes whose field set adapts to the picked
    // input. The contract (locked 2026-05-22, audit R1):
    //
    //   * File-shaped connectors (csv / json / parquet / excel / xml)
    //     accept a saved connection OR a file_path. No inline URL.
    //   * Network connectors (rest_api / database / s3 / azure_blob /
    //     gcs / kafka / sharepoint / ...) REQUIRE a saved connection.
    //     Inline url / inline credentials were a legacy escape hatch
    //     that bypassed the Connections page's audit + RBAC + env
    //     scoping — removed now so every network-side source is
    //     trace-able through the Connections page.
    //
    // Migration note: any pipeline that previously used `params.url`
    // directly on a `source` node will fail validation here. The
    // user should: (a) create a REST API connection with that URL +
    // auth_type=none, (b) set the step's connection_id to the new
    // connection. The sample bundles ship paired connections so
    // freshly-imported pipelines work without manual steps.
    const FILE_CONNECTORS = new Set([
      'csv', 'json', 'parquet', 'excel', 'xml',
    ]);
    if (stepType === 'source' || stepType === 'destination') {
      const connector = String(params?.connector_type || '').toLowerCase();
      const hasConn = hasParam(params, 'connection_id');
      // Inline file references (file_path / dataset_id / file_id)
      // remain valid for file-shaped connectors. The Source UI's
      // "Data File" picker writes file_path; uploaded datasets land
      // in dataset_id / file_id.
      const hasFileInput = hasAnyParam(params, [
        'file_path', 'dataset_id', 'file_id',
      ]);
      const isFileConnector = !connector || FILE_CONNECTORS.has(connector);

      if (isFileConnector) {
        if (!hasConn && !hasFileInput) {
          issues.push({
            nodeId: node.id,
            nodeLabel: label,
            level: 'error',
            message: 'Pick a connection or upload a data file — open the node and configure one of them.',
            field: 'connection_id',
          });
        }
      } else {
        // Network connector — connection_id required. inline URL is
        // no longer a valid alternative (audit R1).
        if (!hasConn) {
          issues.push({
            nodeId: node.id,
            nodeLabel: label,
            level: 'error',
            message:
              `Pick a saved connection — ${connector} sources must reference a connection from the Connections page. ` +
              `(Inline URL/credentials are no longer accepted; create a connection once and reuse it.)`,
            field: 'connection_id',
          });
        }
      }

      if (hasConn) {
        // Only run the kind-specific checks when we're in the
        // connection-driven flow. File-uploaded sources don't have a
        // schema/table — the per-format sub-config validates them via
        // SOURCE_REQUIREMENTS above (csv_source, json_source, etc.).
        const kind = (params?.kind || 'table') as string;
        if (kind === 'table' && !hasParam(params, 'table')) {
          issues.push({
            nodeId: node.id,
            nodeLabel: label,
            level: 'error',
            message: 'Missing required field: table (Source/Destination is in table mode).',
            field: 'table',
          });
        } else if (kind === 'query' && !hasParam(params, 'query')) {
          issues.push({
            nodeId: node.id,
            nodeLabel: label,
            level: 'error',
            message: 'Missing required field: query (Source/Destination is in query mode).',
            field: 'query',
          });
        }
      }
    }

    if (stepType === 'copy_data') {
      const checkSide = (side: 'source' | 'sink') => {
        const connKey = `${side}_connection_id`;
        if (!hasParam(params, connKey)) {
          issues.push({
            nodeId: node.id,
            nodeLabel: label,
            level: 'error',
            message: `Pick a ${side} connection — open the node and choose one under the ${side[0].toUpperCase() + side.slice(1)} tab.`,
            field: connKey,
          });
          return;
        }
        const kind = (params?.[`${side}_kind`] || params?.[`${side}_source_type`] || 'table') as string;
        if (kind === 'table' && !hasParam(params, `${side}_table`)) {
          issues.push({
            nodeId: node.id,
            nodeLabel: label,
            level: 'error',
            message: `Missing required field: ${side}_table (${side[0].toUpperCase() + side.slice(1)} is in table mode).`,
            field: `${side}_table`,
          });
        } else if (kind === 'query' && !hasParam(params, `${side}_query`)) {
          issues.push({
            nodeId: node.id,
            nodeLabel: label,
            level: 'error',
            message: `Missing required field: ${side}_query (${side[0].toUpperCase() + side.slice(1)} is in query mode).`,
            field: `${side}_query`,
          });
        } else if (kind === 'stored_procedure' && !hasParam(params, `${side}_procedure`)) {
          issues.push({
            nodeId: node.id,
            nodeLabel: label,
            level: 'error',
            message: `Missing required field: ${side}_procedure (${side[0].toUpperCase() + side.slice(1)} is in stored-procedure mode).`,
            field: `${side}_procedure`,
          });
        }
      };
      checkSide('source');
      checkSide('sink');
    }

    // --- Input contract enforcement ---
    // The contract per stepType is the single source of truth for
    // required / optional / variadic upstream cardinality. Both the
    // canvas's proactive drop-time guard (isValidConnection) and this
    // post-load safety net consult it so they can't drift.
    const contract = contractFor(stepType);
    const incomingCount = incoming.get(node.id)?.length || 0;
    if (incomingCount < contract.required) {
      issues.push({
        nodeId: node.id,
        nodeLabel: label,
        level: 'error',
        message:
          `${label} needs at least ${contract.required} input` +
          `${contract.required === 1 ? '' : 's'} but only has ${incomingCount}. ` +
          `Connect upstream node${contract.required === 1 ? '' : 's'} before running.`,
      });
    }
    const maxAccepted = contract.variadic
      ? Number.POSITIVE_INFINITY
      : contract.required + contract.optional;
    if (incomingCount > maxAccepted) {
      issues.push({
        nodeId: node.id,
        nodeLabel: label,
        level: 'error',
        message:
          `${label} accepts at most ${maxAccepted} input` +
          `${maxAccepted === 1 ? '' : 's'} but has ${incomingCount} connected. ` +
          `Insert a Union or Join, or remove the extra connection — ` +
          `otherwise the extras are silently dropped at runtime.`,
      });
    }

    // --- Transform multi-input alias validation (2026-06-10) ---
    // Transform registers each DIRECTLY-connected input as a named DuckDB
    // table (the first also as `source_table` / `input`; the rest by
    // sanitized node label). Catch the failure modes the executor would
    // otherwise only surface at runtime.
    if (stepType === 'transform') {
      const expr = String(params?.expression || '');
      const upstreamIds = incoming.get(node.id) || [];
      const inputAliases = upstreamIds
        .map((sid) => sanitizeAlias(nodeById.get(sid)?.data?.label || nodeById.get(sid)?.id || ''))
        .filter(Boolean);
      const namedAliases = inputAliases.filter((a) => a !== 'source_table' && a !== 'input');

      // 1. Duplicate sanitized aliases — the later input silently clobbers.
      const seenAlias = new Set<string>();
      const dupeAlias = new Set<string>();
      for (const a of namedAliases) {
        if (seenAlias.has(a)) dupeAlias.add(a);
        seenAlias.add(a);
      }
      if (dupeAlias.size > 0) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'error', field: 'expression',
          message:
            `Two connected inputs resolve to the same SQL table name ` +
            `(${[...dupeAlias].join(', ')}). Rename one upstream node so every ` +
            `input has a unique alias.`,
        });
      }

      // 2. Multiple inputs connected but the SQL only reads `source_table`
      //    (the first input) — the extras are connected yet unused.
      if (incomingCount >= 2 && expr && namedAliases.length > 0) {
        const lower = expr.toLowerCase();
        const anyUsed = namedAliases.some(
          (a) => new RegExp(`(^|[^a-z0-9_])${a}([^a-z0-9_]|$)`).test(lower),
        );
        if (!anyUsed) {
          issues.push({
            nodeId: node.id, nodeLabel: label, level: 'warning', field: 'expression',
            message:
              `${incomingCount} inputs are connected but the SQL only references ` +
              `\`source_table\` (the first input). Reference the others by name ` +
              `(${namedAliases.join(', ')}) or remove the extra connections.`,
          });
        }
      }

      // 3. Unknown table reference — a FROM/JOIN name that is neither a
      //    reserved alias, a connected input, nor a CTE defined in the SQL.
      if (expr) {
        const known = new Set<string>(['source_table', 'input']);
        for (const a of inputAliases) known.add(a);
        for (const sid of upstreamIds) known.add(String(sid).replace(/-/g, '_').toLowerCase());
        for (const c of extractCteNames(expr)) known.add(c.toLowerCase());
        const unknown = [...new Set(extractSqlTableRefs(expr).map((r) => r.toLowerCase()))]
          .filter((r) => !known.has(r));
        if (unknown.length > 0) {
          issues.push({
            nodeId: node.id, nodeLabel: label, level: 'warning', field: 'expression',
            message:
              `Transform SQL references ${unknown.length === 1 ? 'a table' : 'tables'} that ` +
              `${unknown.length === 1 ? "isn't" : "aren't"} a connected input: ${unknown.join(', ')}. ` +
              `It can only read ${[...known].filter((k) => k !== 'input').join(', ')}. ` +
              `Check the name, or connect the missing source.`,
          });
        }
      }
    }

    // --- Flow control requirements ---
    if (FLOW_REQUIREMENTS[stepType] !== undefined && !schemaLoadedFor(stepType)) {
      // Legacy fallback only — schema-driven check (C3) owns this when loaded.
      for (const field of FLOW_REQUIREMENTS[stepType]) {
        if (!hasParam(params, field)) {
          issues.push({
            nodeId: node.id,
            nodeLabel: label,
            level: 'error',
            message: `Missing required field: ${field}`,
            field,
          });
        }
      }
    }

    // --- Action requirements ---
    if (ACTION_REQUIREMENTS[stepType] !== undefined) {
      if (stepType === 'webhook_trigger' || stepType === 'webhook') {
        if (!hasAnyParam(params, ['url', 'path'])) {
          issues.push({
            nodeId: node.id,
            nodeLabel: label,
            level: 'error',
            message: 'Missing required field: url or path',
            field: 'url',
          });
        }
      } else if (stepType === 'slack_notify' || stepType === 'slack') {
        if (!hasAnyParam(params, ['webhook_url'])) {
          issues.push({
            nodeId: node.id,
            nodeLabel: label,
            level: 'error',
            message: 'Missing required field: webhook_url',
            field: 'webhook_url',
          });
        }
      } else if (!schemaLoadedFor(stepType)) {
        // Legacy fallback only — schema-driven check (C3) owns this when loaded.
        for (const field of ACTION_REQUIREMENTS[stepType]) {
          if (!hasParam(params, field)) {
            issues.push({
              nodeId: node.id,
              nodeLabel: label,
              level: 'error',
              message: `Missing required field: ${field}`,
              field,
            });
          }
        }
      }
    }

    // --- Per-node param sanity (2026-06-11 node audit) ---
    // Param-only checks that don't need the upstream schema: these
    // mirror the exact errors the backend nodes raise, so the user
    // sees them before Run instead of as runtime failures.
    if (stepType === 'deduplicate') {
      const keys = params?.key ?? params?.columns;
      const hasKeys = Array.isArray(keys) ? keys.length > 0 : Boolean(keys);
      if (!hasKeys) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'error', field: 'key',
          message: 'At least one key column is required — pick which columns make a row unique.',
        });
      } else if (!hasParam(params, 'order_by')) {
        const strategy = params?.strategy || 'keep_first';
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'warning', field: 'order_by',
          message:
            `Deduplication order is non-deterministic: "${strategy}" without an Order By ` +
            `keeps an engine-arbitrary row per key. Set Order By (e.g. created_at DESC) ` +
            `for predictable results.`,
        });
      }
    }

    if (stepType === 'upsert') {
      // "Keep Latest" (stepType upsert for back-compat) — same shape as
      // deduplicate: keys required, order needed for determinism.
      const keys = params?.key;
      const hasKeys = Array.isArray(keys) ? keys.length > 0 : Boolean(keys);
      if (!hasKeys) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'error', field: 'key',
          message: 'At least one key column is required — pick which columns identify a record.',
        });
      } else if (!hasParam(params, 'order_by')) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'warning', field: 'order_by',
          message:
            '"Latest wins" is non-deterministic without an Order By — set one ' +
            '(e.g. updated_at DESC) so the same row survives on every run.',
        });
      }
    }

    if (stepType === 'scd2') {
      const bk = params?.business_key;
      const tracked = params?.tracked_columns;
      const hasBk = Array.isArray(bk) ? bk.length > 0 : Boolean(bk);
      const hasTracked = Array.isArray(tracked) ? tracked.length > 0 : Boolean(tracked);
      if (!hasBk) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'error', field: 'business_key',
          message: 'Business key is required — the column(s) that identify an entity across versions.',
        });
      }
      if (!hasTracked) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'error', field: 'tracked_columns',
          message: 'At least one tracked column is required — the columns whose changes create a new version.',
        });
      }
      if (hasBk && hasTracked && Array.isArray(bk) && Array.isArray(tracked)) {
        const overlap = bk.filter((k: string) => tracked.includes(k));
        if (overlap.length > 0) {
          issues.push({
            nodeId: node.id, nodeLabel: label, level: 'warning', field: 'tracked_columns',
            message:
              `Business key column${overlap.length > 1 ? 's' : ''} (${overlap.join(', ')}) ` +
              `also listed as tracked — a key should identify the entity, not trigger new versions. ` +
              `Remove it from Tracked Columns.`,
          });
        }
      }
    }

    if (stepType === 'sample') {
      const mode = params?.mode
        || (params?.fraction != null && params?.count == null ? 'percent' : 'rows');
      if (mode === 'percent') {
        const p = params?.percent ?? (params?.fraction != null ? Number(params.fraction) * 100 : null);
        if (p == null || p === '') {
          issues.push({
            nodeId: node.id, nodeLabel: label, level: 'error', field: 'percent',
            message: 'Percent mode needs a percentage — set a value between 0 and 100.',
          });
        } else if (!(Number(p) > 0 && Number(p) <= 100)) {
          issues.push({
            nodeId: node.id, nodeLabel: label, level: 'error', field: 'percent',
            message: `Percent must be between 0 and 100 (got ${p}).`,
          });
        }
      } else {
        const c = params?.count ?? 100;
        if (!(Number(c) > 0)) {
          issues.push({
            nodeId: node.id, nodeLabel: label, level: 'error', field: 'count',
            message: `Row count must be greater than 0 (got ${c}).`,
          });
        }
      }
    }

    if (stepType === 'delete_data') {
      if (params?.target_kind === 'files') {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'error', field: 'target_kind',
          message:
            "'files' mode is not implemented in this edition — NO files would be deleted. " +
            "Use a File System node for file cleanup, or switch Delete Target to 'rows'.",
        });
      } else if (!hasParam(params, 'condition')) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'warning', field: 'condition',
          message: 'No delete condition set — the node will remove nothing and pass all rows through.',
        });
      }
    }

    if (stepType === 'switch_case' || stepType === 'switch') {
      const cases: any[] = Array.isArray(params?.cases) ? params.cases : [];
      const values = cases.map((c) => String(c?.value ?? '').trim());
      if (values.some((v) => v === '')) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'error', field: 'cases',
          message: 'A case has an empty value — every case needs the value it matches on.',
        });
      }
      const seenVals = new Set<string>();
      const dupVals = new Set<string>();
      for (const v of values) {
        if (v && seenVals.has(v)) dupVals.add(v);
        seenVals.add(v);
      }
      if (dupVals.size > 0) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'error', field: 'cases',
          message: `Duplicate case value${dupVals.size > 1 ? 's' : ''}: ${[...dupVals].join(', ')}. Only the first match would ever fire.`,
        });
      }
      if (cases.length > 0 && !hasParam(params, 'default_case') && !hasParam(params, 'default_label')) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'warning', field: 'default_case',
          message: 'No default branch configured — rows matching none of the cases will be dropped silently.',
        });
      }
    }

    if (stepType === 'wait_delay') {
      const secs = params?.seconds ?? (Number(params?.duration || 0) *
        ({ seconds: 1, minutes: 60, hours: 3600 } as Record<string, number>)[String(params?.unit || 'seconds').toLowerCase()] || 0);
      const n = Number(secs);
      if (!(n > 0)) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'error', field: 'seconds',
          message: `Delay must be greater than 0 seconds (got ${secs ?? 'nothing'}).`,
        });
      } else if (n > 300) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'warning', field: 'seconds',
          message: `Delay of ${n}s will be capped at 300s (5 min) by the engine — longer waits belong in a schedule.`,
        });
      }
    }

    // --- ForEach (Run Pipeline) / Execute Pipeline — control-flow guardrails
    //     (2026-06-16). The per-row loop requires items + an inner activity and
    //     blocks self-invocation; we mirror that for the sub-pipeline runners. ---
    if (stepType === 'foreach_pipeline' || stepType === 'execute_pipeline') {
      const isForEach = stepType === 'foreach_pipeline';
      const subId = params?.pipeline_id ? String(params.pipeline_id) : '';
      const noun = isForEach ? 'ForEach' : 'Execute Pipeline';

      // (a) Recursion guard — a pipeline that runs ITSELF loops forever; we
      //     block self-invocation. Only checkable when we know the current id.
      if (subId && currentPipelineId && subId === String(currentPipelineId)) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'error', field: 'pipeline_id',
          message: `${noun} runs THIS pipeline — that recurses forever. Pick a different sub-pipeline.`,
        });
      }

      if (isForEach) {
        // (b) ForEach iterates its INPUT rows — no upstream means nothing to
        //     loop over (the per-row loop needs items).
        if ((incoming.get(node.id) || []).length === 0) {
          issues.push({
            nodeId: node.id, nodeLabel: label, level: 'error', field: 'input',
            message: 'ForEach has no input — connect the rows to iterate over (it runs the sub-pipeline once per row).',
          });
        }
        // (c) Iteration cap sanity — 0/negative never runs; very large means a
        //     full sub-pipeline per row (slow). Mirrors a bounded batch count.
        const cap = params?.max_iterations;
        if (cap !== undefined && cap !== null && cap !== '') {
          const n = Number(cap);
          if (!(n > 0)) {
            issues.push({
              nodeId: node.id, nodeLabel: label, level: 'error', field: 'max_iterations',
              message: `Max iterations must be greater than 0 (got ${cap}) — the loop would never run.`,
            });
          } else if (n > 1000) {
            issues.push({
              nodeId: node.id, nodeLabel: label, level: 'warning', field: 'max_iterations',
              message: `Max iterations ${n} runs a full sub-pipeline per row — that can be very slow. Keep it tight or use a set-based transform.`,
            });
          }
        }
      }
    }

    if (stepType === 'sort') {
      const raw = params?.sort_rules || params?.sort_by || [];
      const cols = (Array.isArray(raw) ? raw : [])
        .map((it: any) => (it && typeof it === 'object' ? String(it.column || '') : String(it).trim().split(/\s+/)[0]))
        .filter(Boolean);
      const seenCols = new Set<string>();
      const dupCols = new Set<string>();
      for (const c of cols) {
        if (seenCols.has(c)) dupCols.add(c);
        seenCols.add(c);
      }
      if (dupCols.size > 0) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'error', field: 'sort_by',
          message: `Duplicate sort column${dupCols.size > 1 ? 's' : ''}: ${[...dupCols].join(', ')}. Each column can appear only once.`,
        });
      }
    }

    // --- Aggregate alias / column sanity (2026-06-11 node audit) ---
    if (stepType === 'aggregate') {
      const fns: any[] = Array.isArray(params?.functions) ? params.functions : [];
      const aliases = fns.map((f) => String(f?.alias ?? '').trim());
      // Duplicate output aliases → later column silently shadows the earlier.
      const seenA = new Set<string>(), dupA = new Set<string>();
      for (const a of aliases) {
        if (a && seenA.has(a)) dupA.add(a);
        seenA.add(a);
      }
      if (dupA.size > 0) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'error', field: 'functions',
          message: `Duplicate aggregate alias${dupA.size > 1 ? 'es' : ''}: ${[...dupA].join(', ')}. Each output column needs a unique name.`,
        });
      }
      for (const f of fns) {
        const fn = String(f?.function ?? '').toUpperCase();
        const alias = String(f?.alias ?? '').trim();
        const col = String(f?.column ?? '').trim();
        if (alias && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(alias)) {
          issues.push({
            nodeId: node.id, nodeLabel: label, level: 'error', field: 'functions',
            message: `Invalid aggregate alias '${alias}' — use letters, digits and underscores; no spaces or leading digit.`,
          });
        }
        // COUNT(*) is the only aggregate that legitimately has no column.
        if (fn && fn !== 'COUNT' && (!col || col === '*')) {
          issues.push({
            nodeId: node.id, nodeLabel: label, level: 'error', field: 'functions',
            message: `${fn} needs a column — only COUNT can aggregate over * (all rows).`,
          });
        }
      }
    }

    // --- Window: order-dependent functions require ORDER BY (2026-06-11) ---
    if (stepType === 'window') {
      const fns: any[] = Array.isArray(params?.window_functions)
        ? params.window_functions
        : (Array.isArray(params?.functions) ? params.functions : []);
      const order = params?.order_by;
      const hasOrder = Array.isArray(order) ? order.length > 0 : Boolean(order);
      const NEEDS_ORDER = new Set([
        'ROW_NUMBER', 'RANK', 'DENSE_RANK', 'NTILE', 'LAG', 'LEAD',
        'PERCENT_RANK', 'CUME_DIST', 'FIRST_VALUE', 'LAST_VALUE', 'NTH_VALUE',
      ]);
      const offenders = [...new Set(
        fns.map((f) => String(f?.function ?? '').toUpperCase()).filter((fn) => NEEDS_ORDER.has(fn)),
      )];
      if (offenders.length > 0 && !hasOrder) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'error', field: 'order_by',
          message: `${offenders.join(', ')} need an Order By to produce deterministic results — add at least one order column.`,
        });
      }
      const winAliases = fns.map((f) => String(f?.alias ?? '').trim());
      const seenW = new Set<string>(), dupW = new Set<string>();
      for (const a of winAliases) {
        if (a && seenW.has(a)) dupW.add(a);
        seenW.add(a);
      }
      if (dupW.size > 0) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'error', field: 'window_functions',
          message: `Duplicate window alias${dupW.size > 1 ? 'es' : ''}: ${[...dupW].join(', ')}. Each new column needs a unique name.`,
        });
      }
    }

    // --- Conditional Split branches (2026-06-11 multi-output) ---
    if (stepType === 'conditional_split') {
      const conds: any[] = Array.isArray(params?.conditions) ? params.conditions : [];
      if (conds.length === 0) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'warning', field: 'conditions',
          message: 'No split conditions — every row goes to the default output. Add a condition to actually branch.',
        });
      }
      const seenNames = new Set<string>();
      const dupNames = new Set<string>();
      for (const c of conds) {
        const name = String(c?.name ?? '').trim();
        const cond = String(c?.condition ?? '').trim();
        if (!name) {
          issues.push({
            nodeId: node.id, nodeLabel: label, level: 'error', field: 'conditions',
            message: 'A split branch has no name — each branch needs an output label to wire from.',
          });
        } else {
          if (seenNames.has(name)) dupNames.add(name);
          seenNames.add(name);
        }
        if (name && !cond) {
          issues.push({
            nodeId: node.id, nodeLabel: label, level: 'error', field: 'conditions',
            message: `Branch '${name}' has no condition — give it a SQL predicate or remove it.`,
          });
        }
      }
      if (dupNames.size > 0) {
        issues.push({
          nodeId: node.id, nodeLabel: label, level: 'error', field: 'conditions',
          message: `Duplicate branch name${dupNames.size > 1 ? 's' : ''}: ${[...dupNames].join(', ')}. Output labels must be unique.`,
        });
      }
    }

    // --- Schema-driven required fields (C3 / Phase 2) ---
    // The backend param_schema is authoritative. Runs when the registry is
    // loaded; the legacy SOURCE/TRANSFORM/FLOW/ACTION map blocks above defer
    // to it (gated on schemaLoadedFor). Excludes:
    //   * one-of fields (file_path/url, webhook url/path, slack channel/...)
    //     which have dedicated OR-blocks above, and
    //   * the mode-driven generic source / destination / copy_data nodes,
    //     whose requirements are validated by their own special blocks.
    // De-duped by field against issues already raised for this node, so the
    // richer dedicated messages (deduplicate key, scd2 business_key, …) win
    // and a field is never reported twice.
    const schemaRequired = requiredFieldsFromSchema(stepType);
    if (
      schemaRequired &&
      stepType !== 'source' &&
      stepType !== 'destination' &&
      stepType !== 'copy_data'
    ) {
      const oneOf = new Set((ONE_OF_FIELD_GROUPS[stepType] || []).flat());
      const alreadyFlagged = new Set(
        issues.filter((i) => i.nodeId === node.id && i.field).map((i) => i.field),
      );
      for (const field of schemaRequired) {
        if (oneOf.has(field)) continue;
        if (alreadyFlagged.has(field)) continue;
        if (!hasParam(params, field)) {
          issues.push({
            nodeId: node.id,
            nodeLabel: label,
            level: 'error',
            message: `Missing required field: ${field}`,
            field,
          });
        }
      }
    }

    // --- Connection warnings ---
    // Universal nodes: we no longer warn based on step type. A node with no
    // edges at all will still be flagged by the disconnected-subgraph pass.
  }

  // --- Disconnected subgraph detection ---
  if (nodes.length > 1) {
    const nodeIds = new Set(nodes.map((n) => n.id));
    const visited = new Set<string>();

    // BFS from the first node using undirected edges
    const adjacency = new Map<string, Set<string>>();
    for (const id of nodeIds) {
      adjacency.set(id, new Set());
    }
    for (const edge of edges) {
      if (nodeIds.has(edge.source) && nodeIds.has(edge.target)) {
        adjacency.get(edge.source)!.add(edge.target);
        adjacency.get(edge.target)!.add(edge.source);
      }
    }

    const queue: string[] = [nodes[0].id];
    visited.add(nodes[0].id);
    while (queue.length > 0) {
      const current = queue.shift()!;
      for (const neighbor of adjacency.get(current) || []) {
        if (!visited.has(neighbor)) {
          visited.add(neighbor);
          queue.push(neighbor);
        }
      }
    }

    if (visited.size < nodeIds.size) {
      const disconnected = nodes.filter((n) => !visited.has(n.id));
      for (const node of disconnected) {
        issues.push({
          nodeId: node.id,
          nodeLabel: node.data?.label || node.id,
          level: 'warning',
          message: 'Node is in a disconnected subgraph',
        });
      }
    }
  }

  // --- Loop / split metadata leaking into a sink (A5) ---
  // foreach_loop adds `_batch_index`/`_batch_total`, conditional_split
  // adds `_split_output`, data_quality (tag/split) adds `__dq_passed`,
  // etc. If a downstream pass-through sink writes that schema verbatim
  // to a target table, the table picks up engine-internal columns the
  // user didn't ask for. Warn so the user can drop them via a `select`
  // step (or `rename` / `schema_mapper`) before the sink.
  const METADATA_PRODUCERS = new Set([
    'foreach_loop',
    'conditional_split',
    'data_quality',
    'validate',
    'llm_guardrail',
    'kafka_source',
    // 2026-06-15: set_variable removed — it was repurposed to write
    // {{ $vars }} on the run context and now passes input rows through
    // UNCHANGED (it no longer appends columns), so it injects no metadata.
  ]);
  for (const node of nodes) {
    const stepType: string = node.data?.stepType || '';
    if (sideEffectClassFor(stepType) !== 'passthrough') continue;  // sinks only
    if (deactivated.has(node.id) || shadowed.has(node.id)) continue;
    // Walk ancestors looking for metadata producers; collect the closest one.
    const producers: string[] = [];
    const seen = new Set<string>();
    const stack = [...(incoming.get(node.id) || [])];
    while (stack.length > 0) {
      const cur = stack.pop()!;
      if (seen.has(cur)) continue;
      seen.add(cur);
      const n = nodes.find((x) => x.id === cur);
      const t: string = n?.data?.stepType || '';
      if (METADATA_PRODUCERS.has(t)) {
        const label = n?.data?.label || t || cur;
        producers.push(String(label));
      }
      for (const p of incoming.get(cur) || []) stack.push(p);
    }
    if (producers.length === 0) continue;
    const label = node.data?.label || stepType || node.id;
    issues.push({
      nodeId: node.id,
      nodeLabel: label,
      level: 'warning',
      message:
        `${label} writes upstream output verbatim, but ${producers.length === 1 ? 'an upstream node' : 'upstream nodes'} ` +
        `(${producers.slice(0, 3).join(', ')}${producers.length > 3 ? ', …' : ''}) ` +
        `inject internal metadata columns (${Array.from(RESERVED_METADATA_COLUMNS).slice(0, 4).join(', ')}, …). ` +
        `Drop them with a Rename or Select step before this sink so they don't end up in your output.`,
    });
  }

  // --- Execute-Once inside a loop scope ---
  // _settings.execute_once tells the executor to run the node exactly
  // once regardless of input cardinality. Placing such a node downstream
  // of a foreach_loop or until_loop subverts the loop's per-iteration
  // semantics: the node fires only on the first iteration, then the
  // loop's later iterations see no output from it. Warn at validation
  // time so the user catches the conflict before it produces silently
  // wrong runs.
  const LOOP_TYPES = new Set(['foreach_loop', 'until_loop']);
  const loopAncestors = (nodeId: string): string[] => {
    const seen = new Set<string>();
    const stack = [...(incoming.get(nodeId) || [])];
    const hits: string[] = [];
    while (stack.length > 0) {
      const cur = stack.pop()!;
      if (seen.has(cur)) continue;
      seen.add(cur);
      const node = nodes.find((n) => n.id === cur);
      if (node && LOOP_TYPES.has(node.data?.stepType)) {
        hits.push(node.data?.label || node.data?.stepType || cur);
      }
      for (const p of incoming.get(cur) || []) stack.push(p);
    }
    return hits;
  };
  for (const node of nodes) {
    if (!node.data?.params?._settings?.execute_once) continue;
    if (deactivated.has(node.id) || shadowed.has(node.id)) continue;
    const loops = loopAncestors(node.id);
    if (loops.length === 0) continue;
    const label = node.data?.label || node.data?.stepType || node.id;
    issues.push({
      nodeId: node.id,
      nodeLabel: label,
      level: 'warning',
      message:
        `"Execute Once" is on for this node, but it sits downstream of ` +
        `${loops.length === 1 ? 'a loop' : 'loops'} (${loops.join(', ')}). ` +
        `It will only run on the first iteration — later iterations will skip it. ` +
        `Turn off Execute Once in Settings, or move the node outside the loop scope.`,
    });
  }

  // --- Deactivation-shadow warnings ---
  // Tell the user *why* a downstream node will be skipped at runtime —
  // without this they'd just see "skipped" in the run log and have to
  // hunt for the deactivated ancestor.
  for (const node of nodes) {
    if (!shadowed.has(node.id)) continue;
    const label = node.data?.label || node.data?.stepType || node.id;
    issues.push({
      nodeId: node.id,
      nodeLabel: label,
      level: 'warning',
      message: 'Will be skipped — an upstream node is deactivated',
    });
  }

  // --- Undeclared ${param.X} references ---
  // Walk every step's params and surface any ${param.<name>} placeholder
  // whose name isn't in the pipeline's declared parameter list. Catches
  // typos like ${param.dataet} when the user meant ${param.dataset}, and
  // forgotten declarations after copy-pasting steps between pipelines.
  // System placeholders (${utcnow}, ${run_id}) are NOT flagged.
  const PARAM_REF_RE = /\$\{param\.([A-Za-z_][A-Za-z0-9_]*)\}/g;
  const collectRefs = (value: any, out: Set<string>): void => {
    if (typeof value === 'string') {
      let m: RegExpExecArray | null;
      PARAM_REF_RE.lastIndex = 0;
      while ((m = PARAM_REF_RE.exec(value)) !== null) {
        out.add(m[1]);
      }
    } else if (Array.isArray(value)) {
      for (const v of value) collectRefs(v, out);
    } else if (value && typeof value === 'object') {
      for (const v of Object.values(value)) collectRefs(v, out);
    }
  };

  for (const node of activityNodes) {
    const params = node.data?.params || {};
    const refs = new Set<string>();
    collectRefs(params, refs);
    if (refs.size === 0) continue;
    const undeclared = Array.from(refs).filter((n) => !declaredParams.has(n));
    if (undeclared.length === 0) continue;
    const label = node.data?.label || node.data?.stepType || node.id;
    issues.push({
      nodeId: node.id,
      nodeLabel: label,
      level: 'warning',
      message:
        `Step references undeclared parameter${undeclared.length === 1 ? '' : 's'}: ` +
        undeclared.map((n) => `\${param.${n}}`).join(', ') +
        '. Open Parameters in the toolbar to declare them, or fix the typo.',
    });
  }

  // --- Directed-cycle detection (2026-06-10) ---
  // The canvas blocks cycle-closing edge drops proactively, but JSON
  // imports / scripted workflows bypass that guard — and the executor
  // can't unwind a loop. Until this pass, NO frontend layer caught an
  // indirect cycle (A→B→C→A); it surfaced as a runtime failure.
  const cycleNodes = findCycleNodeIds(
    activityNodes.map((n) => n.id),
    edges.map((e) => ({ source: e.source, target: e.target })),
  );
  for (const node of activityNodes) {
    if (!cycleNodes.has(node.id)) continue;
    const label = node.data?.label || node.data?.stepType || node.id;
    issues.push({
      nodeId: node.id,
      nodeLabel: label,
      level: 'error',
      message:
        'Node is part of a cycle — the pipeline loops back on itself and ' +
        'can never finish. Remove one of the connections in the loop ' +
        '(use a Foreach / Until loop node for intentional iteration).',
    });
  }

  return issues;
}

// ---------------------------------------------------------------------------
// Color helper for node validation badges
// ---------------------------------------------------------------------------

export function getNodeValidationColor(issues: ValidationIssue[]): string | null {
  if (issues.length === 0) return null;
  const hasError = issues.some((i) => i.level === 'error');
  if (hasError) return '#ef4444';
  return '#f59e0b';
}
