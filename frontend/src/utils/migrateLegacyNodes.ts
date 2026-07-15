/**
 * Legacy-node migration — frontend half.
 *
 * Mirrors `backend/fpulse/ir/migrations.py`. Same remap policy:
 *
 *   * deprecated step types with a `replaced_by` get rewritten + any
 *     injected params merged into the step's params dict
 *   * legacy specific-source / specific-sink types collapse into the
 *     generic `source` / `destination` shape with a `connector_type`
 *     parameter
 *
 * Why this exists on the frontend (2026-05-22):
 *
 * The backend migration fires on the workflow-store load/save path —
 * which covers normal saves, template imports, AI drafts, programmatic
 * creates. It does NOT cover the Toolbar's "Import from JSON file"
 * flow, because that path loads the canvas directly without round-
 * tripping through the store. Result: when a user imports an older
 * sample JSON (e.g. `samples/free-api-pipelines/pipelines/01-*.json`
 * which still uses `api_source`), the legacy type makes it onto the
 * canvas and the ConfigPanel renders the legacy node form instead of
 * the modern generic Source form.
 *
 * Keep this table in lockstep with `backend/fpulse/ir/migrations.py`.
 * The backend conformance test (`tests/test_node_conformance.py`)
 * pins the StepType ↔ DEPRECATED_STEP_TYPES contract, so any new
 * deprecation that lands there should be mirrored here too. The
 * migration is idempotent — calling it on an already-modern
 * workflow is a no-op.
 */

export interface LegacyStep {
  id?: string;
  type?: string;
  params?: Record<string, any>;
  // Any other fields pass through untouched.
  [key: string]: any;
}

interface DeprecationEntry {
  reason: string;
  replaced_by?: string;
  injected_params?: Record<string, any>;
}

// Mirror of backend DEPRECATED_STEP_TYPES.
export const DEPRECATED_STEP_TYPES: Record<string, DeprecationEntry> = {
  webhook_trigger: {
    reason:
      "Inbound webhook receiver infrastructure isn't in v1.0. " +
      'Pull-style integration via api_source is the canonical path.',
    replaced_by: 'api_source',
  },
  webhook_sink: {
    reason: "Use the generic Destination node with connector_type='webhook'.",
    replaced_by: 'destination',
    injected_params: { connector_type: 'webhook' },
  },
  output: {
    reason:
      'Legacy generic-output node. Use the generic Destination node ' +
      'with a concrete connector_type (csv / parquet / etc).',
    replaced_by: 'destination',
    injected_params: { connector_type: 'csv' },
  },
};

// Mirror of backend _SOURCE_REMAP.
const SOURCE_REMAP: Record<string, [string, string]> = {
  csv_source:         ['source', 'csv'],
  json_source:        ['source', 'json'],
  parquet_source:     ['source', 'parquet'],
  excel_source:       ['source', 'excel'],
  xml_source:         ['source', 'xml'],
  db_source:          ['source', 'database'],
  api_source:         ['source', 'rest_api'],
  s3_source:          ['source', 's3'],
  azure_blob_source:  ['source', 'azure_blob'],
  gcs_source:         ['source', 'gcs'],
  sharepoint_source:  ['source', 'sharepoint'],
  onedrive_source:    ['source', 'onedrive'],
  // 2026-05-22 — generic Microsoft Graph source.
  microsoft_graph_source: ['source', 'microsoft_graph'],
  kafka_source:       ['source', 'kafka'],
  ftp_source:         ['source', 'ftp'],
  gsheet_source:      ['source', 'gsheet'],
  delta_source:       ['source', 'delta'],
};

// Mirror of backend _SINK_REMAP.
const SINK_REMAP: Record<string, [string, string]> = {
  csv_sink:         ['destination', 'csv'],
  json_sink:        ['destination', 'json'],
  excel_sink:       ['destination', 'excel'],
  file_sink:        ['destination', 'parquet'],
  db_sink:          ['destination', 'database'],
  s3_sink:          ['destination', 's3'],
  azure_blob_sink:  ['destination', 'azure_blob'],
  gcs_sink:         ['destination', 'gcs'],
  sharepoint_sink:  ['destination', 'sharepoint'],
  onedrive_sink:    ['destination', 'onedrive'],
  kafka_sink:       ['destination', 'kafka'],
  api_sink:         ['destination', 'rest_api'],
  email_sink:       ['destination', 'email'],
  delta_sink:       ['destination', 'delta'],
  warehouse_sink:   ['destination', 'warehouse'],
};

/**
 * Result of running migration over a step list. The remap count + entries
 * let the caller surface a "we rewrote N legacy nodes on import" toast
 * so users understand why the file looks different from what's on disk.
 */
export interface MigrationResult<T extends LegacyStep = LegacyStep> {
  steps: T[];
  remapCount: number;
  remaps: Array<{ stepId?: string; from: string; to: string; reason?: string }>;
}

/**
 * Rewrite legacy step types into the modern generic shape.
 *
 * Idempotent — a workflow that's already on the modern shape is
 * returned unchanged.
 *
 * Step-array is shallow-copied; each remapped step is also shallow-
 * copied with a merged params dict. Existing user params win over
 * injected defaults so a partially-configured legacy node survives
 * the migration with its data intact.
 */
export function migrateLegacySteps<T extends LegacyStep>(
  steps: T[] | undefined | null,
): MigrationResult<T> {
  if (!Array.isArray(steps)) {
    return { steps: [], remapCount: 0, remaps: [] };
  }

  const remaps: MigrationResult<T>['remaps'] = [];
  const out: T[] = [];

  for (const step of steps) {
    if (!step || typeof step !== 'object') {
      out.push(step);
      continue;
    }
    const originalType = step.type;
    if (!originalType) {
      out.push(step);
      continue;
    }

    // 1. Deprecation remap.
    const dep = DEPRECATED_STEP_TYPES[originalType];
    if (dep) {
      if (dep.replaced_by) {
        const params = { ...(step.params || {}) };
        if (dep.injected_params) {
          for (const [k, v] of Object.entries(dep.injected_params)) {
            if (params[k] === undefined) params[k] = v;
          }
        }
        const next = { ...step, type: dep.replaced_by, params } as T;
        remaps.push({
          stepId: step.id,
          from: originalType,
          to: dep.replaced_by,
          reason: dep.reason,
        });
        out.push(next);
        continue;
      }
      // Deprecated with no replacement — still pass through, but flag.
      remaps.push({
        stepId: step.id,
        from: originalType,
        to: originalType,
        reason: `${dep.reason} (no automatic replacement available)`,
      });
      out.push(step);
      continue;
    }

    // 2. Source consolidation.
    if (originalType in SOURCE_REMAP) {
      const [targetType, connectorType] = SOURCE_REMAP[originalType];
      const params = { ...(step.params || {}) };
      if (params.connector_type === undefined) {
        params.connector_type = connectorType;
      }
      const next = { ...step, type: targetType, params } as T;
      remaps.push({
        stepId: step.id,
        from: originalType,
        to: `${targetType} (connector_type=${connectorType})`,
      });
      out.push(next);
      continue;
    }

    // 3. Sink consolidation.
    if (originalType in SINK_REMAP) {
      const [targetType, connectorType] = SINK_REMAP[originalType];
      const params = { ...(step.params || {}) };
      if (params.connector_type === undefined) {
        params.connector_type = connectorType;
      }
      const next = { ...step, type: targetType, params } as T;
      remaps.push({
        stepId: step.id,
        from: originalType,
        to: `${targetType} (connector_type=${connectorType})`,
      });
      out.push(next);
      continue;
    }

    // Pass-through.
    out.push(step);
  }

  return { steps: out, remapCount: remaps.length, remaps };
}

/**
 * Convenience predicate used by tests / dev tools.
 */
export function isDeprecatedStepType(stepType: string): boolean {
  return stepType in DEPRECATED_STEP_TYPES;
}
