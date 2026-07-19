/**
 * useUpstreamSchema — Schema Propagation Loop (PR 1)
 *
 * Returns the column schema flowing INTO a given step on the editor canvas.
 * Used by ConfigPanel and Data Wrangler to populate column-name dropdowns
 * with the live, post-transformation column list — fixing the silent
 * "broken pipeline" bug where a Rename step renames a column and the
 * downstream Typecast dropdown still shows the old name.
 *
 * Behaviour:
 *   - Returns cached schema from `workflowStore.stepSchemas` if present.
 *   - On cache miss: fetches via `api.getStepSchema`, populates the cache,
 *     re-renders the caller.
 *   - Invalidates on every `schemaVersion` bump (param edits, edge changes,
 *     node add/remove — see workflowStore for the trigger points).
 *   - Debounced — rapid keystrokes in ConfigPanel don't spam the backend.
 *   - Sends the in-memory canvas IR via POST so the lookup reflects the
 *     user's UNSAVED edits, not just the last persisted version.
 *
 * Failure mode: if the lookup errors (e.g. an upstream source is mid-edit
 * with no credentials), the hook returns `{ schema: null, error }`. The
 * caller should degrade gracefully — let the user type column names by
 * hand, just without autocomplete.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import { api, type StepSchemaResponse, type SchemaColumn } from '../api/client';
import { schemaStabilityFor } from '../utils/nodeArity';

const DEBOUNCE_MS = 350;

export interface UpstreamSchemaResult {
  /** Flattened column list across all inputs (most steps have 1 input). */
  columns: SchemaColumn[];
  /** Raw response — useful for join/union nodes that have multiple inputs. */
  raw: StepSchemaResponse | null;
  /** True while the first fetch for this (stepId, schemaVersion) is in flight. */
  loading: boolean;
  /** Non-null when the lookup failed; UI should fall back to free-text input. */
  error: string | null;
  /**
   * A4 — true when ANY ancestor (transitively) is a 'dynamic' schema
   * node (pivot, transform, api_source, webhook_trigger, code_script,
   * unpivot, flatten_explode). Downstream column dropdowns should
   * either degrade to a free-text input or display a "schema available
   * after first run" hint, because what they see now is at best a
   * cached approximation from the last successful run.
   */
  dynamic: boolean;
  /** When `dynamic` is true, the labels of the dynamic ancestor nodes
   *  (for a friendly hint like "pivot 'Region by month' produces
   *  columns that depend on data"). Empty array otherwise. */
  dynamicSources: string[];
}

export function useUpstreamSchema(stepId: string | null): UpstreamSchemaResult {
  const workflowId = useWorkflowStore((s) => s.workflowId);
  const nodes = useWorkflowStore((s) => s.nodes);
  const edges = useWorkflowStore((s) => s.edges);
  const parameters = useWorkflowStore((s) => s.parameters);
  const cached = useWorkflowStore((s) => (stepId ? s.stepSchemas[stepId] : undefined));
  const setStepSchema = useWorkflowStore((s) => s.setStepSchema);
  const schemaVersion = useWorkflowStore((s) => s.schemaVersion);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Track which (stepId, schemaVersion) tuple we last fetched. Lets us
  // skip refetches when nothing changed.
  const lastFetchKey = useRef<string | null>(null);

  useEffect(() => {
    if (!stepId) return;

    const fetchKey = `${stepId}::${schemaVersion}`;
    if (cached && lastFetchKey.current === fetchKey) {
      // Cache hit AND we already attempted this exact version — nothing to do.
      return;
    }

    // C10 (2026-05-18): try the local mock-eval first. For "trivial"
    // single-input transforms (rename / select / typecast / derived_column)
    // we can derive the output schema purely from the upstream's cached
    // schema without a backend round-trip. Saves the latency hit on
    // every column-name dropdown rehydration in long chains of these.
    const store = useWorkflowStore.getState();
    const mock = tryLocalMockEval(stepId, store);
    if (mock) {
      lastFetchKey.current = fetchKey;
      setStepSchema(stepId, mock);
      return;
    }

    // Build the unsaved IR from canvas state. The backend POST endpoint
    // re-runs the upstream subgraph against this exact snapshot, so the
    // user's mid-edit changes are honoured even before they hit Save.
    const unsavedWorkflow = nodesToWorkflow(workflowId, nodes, edges, parameters);

    const handle = setTimeout(async () => {
      setLoading(true);
      setError(null);
      try {
        const wfIdForCall = workflowId || 'unsaved';
        const resp = await api.getStepSchema(wfIdForCall, stepId, unsavedWorkflow);
        lastFetchKey.current = fetchKey;
        setStepSchema(stepId, resp);
        if (resp.error) setError(resp.error);
      } catch (e: any) {
        setError(e?.message || 'Schema lookup failed');
      } finally {
        setLoading(false);
      }
    }, DEBOUNCE_MS);

    return () => clearTimeout(handle);
    // We intentionally do NOT depend on `cached` here — re-rendering when
    // the cache fills would re-trigger the effect and reset lastFetchKey.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stepId, schemaVersion, workflowId]);

  // Flatten the response into a single column list for the common case
  // (single-input steps). Multi-input callers can read `raw.inputs[]`.
  const columns: SchemaColumn[] = (() => {
    if (!cached) return [];
    if (cached.is_source && cached.self_schema) {
      return cached.self_schema.columns;
    }
    const out: SchemaColumn[] = [];
    for (const input of cached.inputs) {
      for (const col of input.schema.columns) {
        if (!out.find((c) => c.name === col.name)) {
          out.push(col);
        }
      }
    }
    return out;
  })();

  // A4 — compute dynamic ancestor set. Walks all ancestors of stepId
  // via current edges (not just immediate upstream) and collects the
  // ones whose stepType is tagged 'dynamic' in schemaStabilityFor.
  // Memoized on stepId + schemaVersion so it only recomputes when the
  // graph structurally changes.
  const dynamicSources = useMemo<string[]>(() => {
    if (!stepId) return [];
    const out: string[] = [];
    const seen = new Set<string>();
    const stack = edges.filter((e) => e.target === stepId).map((e) => e.source);
    while (stack.length > 0) {
      const cur = stack.pop()!;
      if (seen.has(cur)) continue;
      seen.add(cur);
      const node = nodes.find((n) => n.id === cur);
      const stepType: string = (node?.data as any)?.stepType || '';
      if (schemaStabilityFor(stepType) === 'dynamic') {
        const label = (node?.data as any)?.label || stepType || cur;
        out.push(String(label));
      }
      for (const e of edges) {
        if (e.target === cur && !seen.has(e.source)) stack.push(e.source);
      }
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stepId, schemaVersion]);

  return {
    columns,
    raw: cached || null,
    loading,
    error,
    dynamic: dynamicSources.length > 0,
    dynamicSources,
  };
}

/**
 * C10 — Local mock-evaluator for "trivial" single-input transforms.
 *
 * For a node whose stepType is in {rename, select, typecast,
 * derived_column}, the OUTPUT schema is a pure function of:
 *   1. the upstream node's output schema (which is the input to this
 *      node and so is the cached schema flowing INTO this stepId), and
 *   2. this node's params.
 *
 * That means we can derive the schema flowing INTO any node X whose
 * single upstream is a trivial node T, given:
 *   - cache hit on T's input schema (already computed earlier)
 *   - T's params on the canvas
 *
 * Returns a synthetic StepSchemaResponse on success, or null to fall
 * back to the backend POST. Strict: gives up on anything non-trivial
 * (multi-input nodes, joins, opaque transforms, missing cache, etc.).
 *
 * Conservative type inference: `derived_column` expressions are typed
 * as 'VARCHAR' (we don't parse SQL); `typecast` honors the user's
 * declared `to_type`. Downstream consumers can always re-fetch from
 * the backend if they need exact types.
 */
const TRIVIAL_TYPES = new Set([
  'rename', 'select', 'typecast', 'derived_column',
]);

function tryLocalMockEval(
  stepId: string,
  store: ReturnType<typeof useWorkflowStore.getState>,
): StepSchemaResponse | null {
  const { nodes, edges, stepSchemas } = store;
  const incomingEdges = edges.filter((e) => e.target === stepId);
  // Mock-eval only handles single-upstream nodes.
  if (incomingEdges.length !== 1) return null;
  const upstreamId = incomingEdges[0].source;
  const upstreamNode = nodes.find((n) => n.id === upstreamId);
  if (!upstreamNode) return null;
  const upstreamType: string = (upstreamNode.data as any)?.stepType || '';
  if (!TRIVIAL_TYPES.has(upstreamType)) return null;

  // Need the upstream's INPUT schema cached so we can apply its transform.
  const upstreamCached = stepSchemas[upstreamId];
  if (!upstreamCached) return null;
  const upstreamInputCols: SchemaColumn[] = (() => {
    if (upstreamCached.is_source && upstreamCached.self_schema) {
      return upstreamCached.self_schema.columns;
    }
    const out: SchemaColumn[] = [];
    for (const inp of upstreamCached.inputs || []) {
      for (const c of inp.schema.columns) {
        if (!out.find((x) => x.name === c.name)) out.push(c);
      }
    }
    return out;
  })();
  if (upstreamInputCols.length === 0) return null;

  const upParams = (upstreamNode.data as any)?.params || {};
  const transformedCols = applyTrivialTransform(upstreamType, upstreamInputCols, upParams);
  if (!transformedCols) return null;

  // Synthesize a StepSchemaResponse shaped like a single-input non-source.
  // 2026-05-22: include `step_id` (the calling step's id) and use the
  // canonical `upstream_step_id` + `upstream_label` field names from the
  // UpstreamInputSchema interface — the prior shape used a non-canonical
  // `source_step_id` key that wouldn't satisfy the type.
  const upstreamLabel = String((upstreamNode.data as any)?.label || upstreamId);
  return {
    step_id: stepId,
    is_source: false,
    inputs: [
      {
        upstream_step_id: upstreamId,
        upstream_label: upstreamLabel,
        schema: { columns: transformedCols },
      },
    ],
  } as StepSchemaResponse;
}

function applyTrivialTransform(
  stepType: string,
  inputCols: SchemaColumn[],
  params: Record<string, any>,
): SchemaColumn[] | null {
  switch (stepType) {
    case 'rename': {
      const map: Record<string, string> = params.mappings || params.rename_map || {};
      return inputCols.map((c) => ({
        ...c,
        name: typeof map[c.name] === 'string' && map[c.name] ? map[c.name] : c.name,
      }));
    }
    case 'select': {
      const keep: string[] = Array.isArray(params.columns) ? params.columns : [];
      if (keep.length === 0) return inputCols;  // empty = all
      const set = new Set(keep.map(String));
      return inputCols.filter((c) => set.has(c.name));
    }
    case 'typecast': {
      const casts: Record<string, string> = params.casts || {};
      return inputCols.map((c) =>
        typeof casts[c.name] === 'string' && casts[c.name]
          ? { ...c, type: casts[c.name] }
          : c,
      );
    }
    case 'derived_column': {
      const out = [...inputCols];
      const derived = Array.isArray(params.columns) ? params.columns : [];
      for (const d of derived) {
        const name = String(d?.name || '').trim();
        if (!name) continue;
        if (!out.find((c) => c.name === name)) {
          // We can't parse the SQL expression — type as VARCHAR. Caller
          // can always re-fetch from backend for exact type inference.
          out.push({ name, type: 'VARCHAR', nullable: true });
        }
      }
      return out;
    }
    default:
      return null;
  }
}

/**
 * Reshape the canvas (ReactFlow nodes + edges) into the Workflow IR shape
 * the backend expects. Mirrors `workflowStore.buildWorkflowPayload`, kept
 * inline here to avoid a circular import and to keep this hook self-contained.
 *
 * Only the fields the backend uses for schema lookup are populated.
 */
function nodesToWorkflow(
  workflowId: string | null,
  nodes: any[],
  edges: any[],
  parameters: any[],
): any {
  return {
    id: workflowId || 'unsaved',
    name: 'Unsaved',
    description: '',
    project_id: 'default',
    workspace_id: 'default',
    parameters,
    steps: nodes.map((n) => ({
      id: n.id,
      type: n.data?.stepType || n.data?.type || 'transform',
      label: n.data?.label || '',
      params: n.data?.params || {},
      position: { x: n.position?.x || 0, y: n.position?.y || 0 },
    })),
    connections: edges.map((e) => ({
      from_step: e.source,
      to_step: e.target,
      from_port: e.sourceHandle || 'output',
      to_port: e.targetHandle || 'input',
      // Carry the per-edge alias so the schema preview reflects the same
      // table names the executor will register for SQL Transform.
      ...((e.data as any)?.alias ? { alias: (e.data as any).alias } : {}),
    })),
  };
}
