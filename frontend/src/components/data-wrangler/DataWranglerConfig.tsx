/**
 * DataWranglerConfig — full-bleed three-pane workspace for the Data
 * Wrangler node (stepwise visible transform).
 *
 * See docs/design-data-wrangler-node.md for the design.
 *
 * Layout:
 *   ┌──────────────┬───────────────────┬──────────────────────────────┐
 *   │ STEPS        │ STEP CONFIG       │ STEP PROFILE                 │
 *   │ (260px)      │ (380px)           │ (flex-1)                     │
 *   │              │                   │   rows: 120 → 95   +1 col    │
 *   │ ① Filter     │ [filter form]     │   ┌─────────┬────┬────┬───┐  │
 *   │ ② Cast       │                   │   │ col     │TYP │null│dst│  │
 *   │ ③ Derive     │                   │   │ amount  │NUM │ 0% │47 │  │
 *   │              │                   │   │ status  │STR │ 5% │ 3 │  │
 *   │ + Add step ▾ │                   │   └─────────┴────┴────┴───┘  │
 *   └──────────────┴───────────────────┴──────────────────────────────┘
 *   [Open compiled SQL]            Preview after step ②: 120 → 95 rows
 *
 * The actual sample data table lives in the bottom OUTPUT panel — it
 * auto-syncs to the currently-selected wrangler sub-step (Step ▾ selector
 * on PreviewPanel). This pane owns schema/profile inspection only.
 *
 * Auto-preview: runs 800ms after the last steps change. No "Run preview"
 * button needed for the common case — the preview is always live. Empty
 * state shows clickable starter templates instead of a dead "No steps yet".
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useWorkflowStore, nodesToWorkflow } from '../../stores/workflowStore';
import { authHeaders } from '../../api/client';
import ResizeHandle from '../shared/ResizeHandle';

// Z9 (2026-05-23) — drag-to-resize for the Wrangler 3-pane workspace.
// Width of the leftmost step-list and middle config-form panes are
// persisted in localStorage so the user's split sticks across visits.
// The right preview pane auto-flexes to fill the remainder.
const WRANGLER_STEPLIST_KEY = 'fpulse_wrangler_steplist_width';
const WRANGLER_CONFIG_KEY = 'fpulse_wrangler_configform_width';
const WRANGLER_STEPLIST_MIN = 200;
const WRANGLER_STEPLIST_MAX = 480;
const WRANGLER_STEPLIST_DEFAULT = 260;
const WRANGLER_CONFIG_MIN = 280;
const WRANGLER_CONFIG_MAX = 640;
const WRANGLER_CONFIG_DEFAULT = 380;

function loadStoredPx(key: string, fallback: number, min: number, max: number): number {
  try {
    const raw = localStorage.getItem(key);
    const parsed = raw ? parseInt(raw, 10) : NaN;
    if (Number.isFinite(parsed) && parsed >= min && parsed <= max) return parsed;
  } catch {
    // localStorage disabled — fall through
  }
  return fallback;
}

// ── Types ────────────────────────────────────────────────────────────────────

// P2-B (2026-05-18): four new sub-step types so users can do common
// single-relation transforms (sort / dedupe / sample / flatten) inside
// the Wrangler instead of chaining standalone canvas nodes for them.
// The standalone palette items remain available; the Wrangler now
// absorbs the most-common flow ("import → wash → publish" reads
// linearly in one tile).
type SubStepOp =
  | 'filter'
  | 'select'
  | 'rename'
  | 'cast'
  | 'derive'
  | 'group_by'
  | 'sort'
  | 'dedupe'
  | 'sample'
  | 'flatten'
  | 'fill_nulls'
  | 'replace_values'
  | 'split_column';

interface SubStep {
  id: string;
  op: SubStepOp;
  enabled: boolean;
  label?: string;
  config: Record<string, any>;
}

interface DataWranglerConfigProps {
  params: Record<string, any>;
  nodeId: string;
  workflowId?: string;
  onChange: (nodeId: string, patch: Record<string, any>) => void;
}

interface PreviewStep {
  index: number;
  op?: string;
  label: string;
  // 2026-06-15: per-step run status. 'error' means this sub-step failed on
  // the sample — `error` carries the message; steps after it didn't run.
  status?: 'ok' | 'error';
  error?: string;
  row_count: number;
  columns: { name: string; type: string }[];
  schema_delta: {
    added: { name: string; type: string }[];
    removed: string[];
    retyped: { name: string; from: string; to: string }[];
  };
  sample_data?: Array<Record<string, unknown>>;
}

interface PreviewResponse {
  node_id: string;
  predecessor_id: string;
  sample_rows: number;
  steps: PreviewStep[];
  generated_sql: string;
}

// ── Op metadata ──────────────────────────────────────────────────────────────

const OP_LABELS: Record<SubStepOp, string> = {
  filter:   'Filter rows',
  select:   'Select columns',
  rename:   'Rename columns',
  cast:     'Cast types',
  derive:   'Derive columns',
  group_by: 'Group by',
  sort:     'Sort rows',
  dedupe:   'Deduplicate',
  sample:   'Sample rows',
  flatten:  'Flatten nested',
  fill_nulls:     'Fill nulls',
  replace_values: 'Replace values',
  split_column:   'Split column',
};

const OP_DESCRIPTIONS: Record<SubStepOp, string> = {
  filter:   "Drop rows that don't match your condition",
  select:   'Keep only the listed columns',
  rename:   'Rename columns (old → new)',
  cast:     'Change column data types',
  derive:   'Add a computed column from an expression',
  group_by: 'Aggregate by one or more keys',
  sort:     'Sort rows by one or more columns',
  dedupe:   'Drop duplicate rows by key',
  sample:   'Take a sample of rows (first N or random N)',
  flatten:  'Expand a STRUCT column into top-level columns',
  fill_nulls:     'Replace NULLs in a column with a value',
  replace_values: 'Swap an exact value for another (e.g. "N/A" → NULL)',
  split_column:   'Split a column by a delimiter into new columns',
};

// Short capital-letter badge used on the starter cards + step list.
const OP_BADGE: Record<SubStepOp, string> = {
  filter:   'F',
  select:   'S',
  rename:   'R',
  cast:     'T',
  derive:   'D',
  group_by: 'G',
  sort:     'O',  // (O)rder
  dedupe:   'U',  // (U)nique
  sample:   '#',
  flatten:  '⊞',
  fill_nulls:     '∅',
  replace_values: '↔',
  split_column:   '✂',
};

function defaultConfigFor(op: SubStepOp): Record<string, any> {
  switch (op) {
    case 'filter':   return { mode: 'expression', expression: '', rules: [], combinator: 'AND' };
    case 'select':   return { columns: [] };
    case 'rename':   return { rename_map: {} };
    case 'cast':     return { casts: [] };
    case 'derive':   return { derived: [] };
    case 'group_by': return { keys: [], aggregations: [] };
    case 'sort':     return { sort_by: [], direction: 'ASC' };
    case 'dedupe':   return { key: [], strategy: 'keep_first' };
    case 'sample':   return { method: 'first', count: 100 };
    case 'flatten':  return { column: '', prefix: '', keep_original: false };
    case 'fill_nulls':     return { fills: [{ column: '', value: '' }] };
    case 'replace_values': return { replacements: [{ column: '', find: '', replace: '' }] };
    case 'split_column':   return { column: '', delimiter: ',', into: [] };
  }
}

function summarizeStep(step: SubStep): string {
  const c = step.config || {};
  switch (step.op) {
    case 'filter':
      if (c.mode === 'rules') {
        const n = (c.rules || []).length;
        return n ? `${n} rule${n === 1 ? '' : 's'}` : 'no rules';
      }
      return c.expression ? String(c.expression).slice(0, 40) : 'no condition';
    case 'select':
      return (c.columns || []).length ? `${(c.columns || []).length} cols` : 'no columns';
    case 'rename':
      return `${Object.keys(c.rename_map || {}).length} renames`;
    case 'cast':
      return `${(c.casts || []).length} casts`;
    case 'derive':
      return `${(c.derived || []).length} new cols`;
    case 'group_by':
      return `${(c.keys || []).length} keys, ${(c.aggregations || []).length} agg`;
    case 'sort':
      return (c.sort_by || []).length ? `by ${(c.sort_by || []).join(', ')} ${c.direction || 'ASC'}` : 'no sort key';
    case 'dedupe':
      return (c.key || []).length ? `by ${(c.key || []).join(', ')} (${c.strategy || 'keep_first'})` : 'no key';
    case 'sample':
      return `${c.method || 'first'} ${c.count ?? 100} rows`;
    case 'flatten':
      return c.column ? `expand ${c.column}` : 'no column';
    case 'fill_nulls':
      return (c.fills || []).length ? `${(c.fills || []).length} column(s)` : 'no columns';
    case 'replace_values':
      return (c.replacements || []).length ? `${(c.replacements || []).length} rule(s)` : 'no rules';
    case 'split_column':
      return c.column ? `split ${c.column}` : 'no column';
  }
}

function newId(): string {
  return 's_' + Math.random().toString(36).slice(2, 8);
}

/**
 * Wrangler step-dependency check (B7 hardening).
 *
 * Returns warnings for enabled steps that reference column names which
 * only exist because of a step that is disabled or comes later in the
 * chain. Pure SQL identifier matching — fast, conservative; misses
 * cases where the user computed the column via `derived.expression` and
 * referenced it inside a backtick-quoted string, but covers the common
 * "disable step 2, step 4 still uses its derived column" footgun.
 */
function collectStepProducedColumns(step: SubStep): string[] {
  const c = step.config || {};
  switch (step.op) {
    case 'rename':
      return Object.values(c.rename_map || {}).map(String);
    case 'derive':
      return ((c.derived || []) as Array<{ name?: string }>).map((d) => String(d.name || '')).filter(Boolean);
    case 'group_by':
      return [
        ...((c.aggregations || []) as Array<{ alias?: string }>).map((a) => String(a.alias || '')).filter(Boolean),
      ];
    default:
      return [];
  }
}

function collectStepReferencedColumns(step: SubStep): string[] {
  const c = step.config || {};
  const refs: string[] = [];
  switch (step.op) {
    case 'filter':
      if (c.mode === 'rules') {
        for (const r of c.rules || []) if (r?.column) refs.push(String(r.column));
      } else if (c.expression) {
        // Conservative tokenize on identifiers.
        const tokens = String(c.expression).match(/\b[A-Za-z_][A-Za-z0-9_]*\b/g) || [];
        refs.push(...tokens);
      }
      break;
    case 'cast':
      for (const cast of c.casts || []) if (cast?.column) refs.push(String(cast.column));
      break;
    case 'rename':
      for (const k of Object.keys(c.rename_map || {})) refs.push(k);
      break;
    case 'derive':
      for (const d of c.derived || []) {
        if (d?.expression) {
          const tokens = String(d.expression).match(/\b[A-Za-z_][A-Za-z0-9_]*\b/g) || [];
          refs.push(...tokens);
        }
      }
      break;
    case 'group_by':
      for (const k of c.keys || []) refs.push(String(k));
      for (const a of c.aggregations || []) if (a?.column && a.column !== '*') refs.push(String(a.column));
      break;
    case 'select':
      for (const col of c.columns || []) refs.push(String(col));
      break;
  }
  return refs;
}

/**
 * Computes per-step warnings about references to columns produced by
 * disabled (or later) steps. Returns a Map<stepId, warning>.
 */
function computeStepWarnings(steps: SubStep[]): Map<string, string> {
  const warnings = new Map<string, string>();
  // For each step i, the columns available to it are: upstream columns
  // (unknown here — we only catch wrangler-produced columns) + columns
  // produced by enabled steps j<i.
  for (let i = 0; i < steps.length; i++) {
    const step = steps[i];
    if (step.enabled === false) continue;
    const refs = new Set(collectStepReferencedColumns(step));
    if (refs.size === 0) continue;
    // Find each ref that's produced ONLY by a disabled-or-later step.
    const blocked: string[] = [];
    for (const ref of refs) {
      // Was this ref produced by some step at all?
      let producedByEnabledEarlier = false;
      let producedByDisabledOrLater = false;
      for (let j = 0; j < steps.length; j++) {
        if (j === i) continue;
        const producer = steps[j];
        const produced = collectStepProducedColumns(producer);
        if (!produced.includes(ref)) continue;
        if (j < i && producer.enabled !== false) producedByEnabledEarlier = true;
        else producedByDisabledOrLater = true;
      }
      if (!producedByEnabledEarlier && producedByDisabledOrLater) {
        blocked.push(ref);
      }
    }
    if (blocked.length > 0) {
      const unique = Array.from(new Set(blocked)).slice(0, 3);
      warnings.set(
        step.id,
        `References column${unique.length === 1 ? '' : 's'} ${unique.map((c) => `"${c}"`).join(', ')}${blocked.length > 3 ? `, +${blocked.length - 3} more` : ''} produced by a disabled or later step. Enable the producer step, or remove the reference.`,
      );
    }
  }
  return warnings;
}

// Frontend-only column profile from the 10-row sample the backend returns.
// Stats are approximate (small sample) but indicative. A future backend
// extension can return authoritative null_count / distinct_count per step.
function colProfile(rows: Array<Record<string, unknown>>, col: string) {
  if (!rows.length) return { nullPct: 0, distinct: 0, sampleSize: 0 };
  const values = rows.map((r) => r[col]);
  const nullCount = values.filter((v) => v === null || v === undefined).length;
  const nonNull = values.filter((v) => v !== null && v !== undefined);
  return {
    nullPct: Math.round((nullCount / values.length) * 100),
    distinct: new Set(nonNull.map((v) => String(v))).size,
    sampleSize: values.length,
  };
}

// ── Main component ───────────────────────────────────────────────────────────

export default function DataWranglerConfig({ params, nodeId, workflowId, onChange }: DataWranglerConfigProps) {
  const editorSurface = useWorkflowStore((s) => s.editorSurface);
  const edges = useWorkflowStore((s) => s.edges);
  const stepResults = useWorkflowStore((s) => s.stepResults);
  const isFileDataPrep = editorSurface === 'file_data_prep';
  const steps: SubStep[] = useMemo(
    () => (params?.steps || []) as SubStep[],
    [params?.steps],
  );
  const upstreamIds = useMemo(() => {
    const fromParams = Array.isArray(params?._input_step_ids)
      ? params._input_step_ids.map((id: unknown) => String(id)).filter(Boolean)
      : [];
    const fromEdges = edges
      .filter((edge) => edge.target === nodeId)
      .map((edge) => edge.source)
      .filter(Boolean);
    return Array.from(new Set([...fromParams, ...fromEdges]));
  }, [params?._input_step_ids, edges, nodeId]);
  const upstreamColumns = useMemo(() => {
    for (const upstreamId of upstreamIds) {
      const result = stepResults[upstreamId] as any;
      if (!result) continue;
      if (Array.isArray(result.schema_info) && result.schema_info.length > 0) {
        return result.schema_info
          .map((column: any) => ({
            name: String(column?.name ?? ''),
            type: String(column?.type ?? 'string'),
          }))
          .filter((column: { name: string; type: string }) => column.name);
      }
      if (Array.isArray(result.columns) && result.columns.length > 0) {
        return result.columns
          .map((name: unknown) => ({ name: String(name), type: 'string' }))
          .filter((column: { name: string; type: string }) => column.name);
      }
    }
    return [];
  }, [upstreamIds, stepResults]);

  // 2026-06-15: sample rows + row count from the executed upstream node, so
  // the empty state can show WHAT data is flowing in before the user picks a
  // step. (Previously the empty state was a bare step picker — "where is the
  // input data?" — the data was only reachable via the bottom INPUT tab.)
  const upstreamInput = useMemo(() => {
    for (const upstreamId of upstreamIds) {
      const result = stepResults[upstreamId] as any;
      if (!result) continue;
      const sample = Array.isArray(result.sample_data) ? result.sample_data : [];
      const rowCount =
        typeof result.row_count === 'number' ? result.row_count : sample.length;
      if (sample.length > 0 || rowCount > 0) {
        return { sample: sample as Record<string, unknown>[], rowCount };
      }
    }
    return { sample: [] as Record<string, unknown>[], rowCount: 0 };
  }, [upstreamIds, stepResults]);

  const [selectedId, setSelectedId] = useState<string | null>(
    steps.length ? steps[0].id : null,
  );
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [showSql, setShowSql] = useState(false);
  // Z18 (2026-05-23): previewBlockedReason removed. The Refresh preview
  // button now works regardless of save state — runPreview routes to
  // the ephemeral endpoint when workflowId is missing.

  // Z9 — Resizable pane widths (step-list + middle config form). The
  // right preview pane auto-flexes; users who want a wider profile view
  // shrink either of the two left panes.
  const [stepListWidth, setStepListWidth] = useState<number>(() =>
    loadStoredPx(WRANGLER_STEPLIST_KEY, WRANGLER_STEPLIST_DEFAULT, WRANGLER_STEPLIST_MIN, WRANGLER_STEPLIST_MAX),
  );
  const [configFormWidth, setConfigFormWidth] = useState<number>(() =>
    loadStoredPx(WRANGLER_CONFIG_KEY, WRANGLER_CONFIG_DEFAULT, WRANGLER_CONFIG_MIN, WRANGLER_CONFIG_MAX),
  );
  useEffect(() => {
    try { localStorage.setItem(WRANGLER_STEPLIST_KEY, String(stepListWidth)); } catch {}
  }, [stepListWidth]);
  useEffect(() => {
    try { localStorage.setItem(WRANGLER_CONFIG_KEY, String(configFormWidth)); } catch {}
  }, [configFormWidth]);

  // Mirror the preview into the workflow store so the bottom PreviewPanel
  // can render any sub-step's output (via a Step ▾ selector) using the
  // existing OutputView/SchemaView/JsonView pipeline. Modal owns per-step
  // delta inspection; bottom panel owns the full data grid.
  const setWranglerPreview = useWorkflowStore((s) => s.setWranglerPreview);
  const setWranglerSelectedStep = useWorkflowStore((s) => s.setWranglerSelectedStep);

  // Keep selectedId valid if the user deletes the selected step
  useEffect(() => {
    if (!selectedId || !steps.find((s) => s.id === selectedId)) {
      setSelectedId(steps[0]?.id ?? null);
    }
  }, [steps, selectedId]);

  const selectedStep = steps.find((s) => s.id === selectedId) ?? null;
  const selectedIndex = selectedStep ? steps.indexOf(selectedStep) : -1;

  // ── Step list mutations ────────────────────────────────────────────────────
  function patchSteps(next: SubStep[]) {
    onChange(nodeId, { ...params, steps: next });
  }

  function addStep(op: SubStepOp) {
    const step: SubStep = {
      id: newId(),
      op,
      enabled: true,
      label: OP_LABELS[op],
      config: defaultConfigFor(op),
    };
    patchSteps([...steps, step]);
    setSelectedId(step.id);
  }

  function updateStep(id: string, patch: Partial<SubStep>) {
    patchSteps(steps.map((s) => (s.id === id ? { ...s, ...patch } : s)));
  }

  function updateStepConfig(id: string, configPatch: Record<string, any>) {
    patchSteps(
      steps.map((s) =>
        s.id === id ? { ...s, config: { ...s.config, ...configPatch } } : s,
      ),
    );
  }

  function removeStep(id: string) {
    patchSteps(steps.filter((s) => s.id !== id));
  }

  function duplicateStep(id: string) {
    const idx = steps.findIndex((s) => s.id === id);
    if (idx < 0) return;
    const orig = steps[idx];
    const copy: SubStep = {
      ...orig,
      id: newId(),
      label: orig.label ? `${orig.label} (copy)` : undefined,
      config: JSON.parse(JSON.stringify(orig.config || {})),
    };
    const next = [...steps];
    next.splice(idx + 1, 0, copy);
    patchSteps(next);
  }

  function moveStep(id: string, direction: -1 | 1) {
    const idx = steps.findIndex((s) => s.id === id);
    if (idx < 0) return;
    const newIdx = idx + direction;
    if (newIdx < 0 || newIdx >= steps.length) return;
    const next = [...steps];
    [next[idx], next[newIdx]] = [next[newIdx], next[idx]];
    patchSteps(next);
  }

  // ── Auto-preview ───────────────────────────────────────────────────────────
  //
  // Runs 800ms after the LAST steps mutation — debounces typing in config
  // forms (filter expression, rename map, etc.) and only fires once when
  // the user stops editing. Structural changes (add / delete / reorder /
  // toggle) also flow through this same path so they get the same 800ms
  // settle period. No explicit "Run preview" button needed.
  const previewAbortRef = useRef<AbortController | null>(null);

  async function runPreview() {
    if (steps.length === 0) {
      setPreview(null);
      setPreviewError(null);
      return;
    }
    // Cancel any in-flight preview before starting a new one.
    previewAbortRef.current?.abort();
    const ac = new AbortController();
    previewAbortRef.current = ac;

    setPreviewLoading(true);
    setPreviewError(null);
    try {
      // Z18 (2026-05-23): dispatch on workflowId presence. Saved
      // workflows hit the persisted endpoint (existing behaviour);
      // unsaved recipes hit /workflows/ephemeral/data-wrangler/preview
      // with the inline IR built from the current canvas state. Same
      // response shape so the rest of the file is unchanged.
      const useEphemeral = !workflowId;
      const url = useEphemeral
        ? '/api/workflows/ephemeral/data-wrangler/preview'
        : `/api/workflows/${workflowId}/nodes/${nodeId}/data-wrangler/preview`;
      let body: string;
      if (useEphemeral) {
        const { nodes, edges, workflowName, parameters } = useWorkflowStore.getState();
        const inlineWf = nodesToWorkflow(
          nodes,
          edges,
          workflowName,
          'ephemeral',
          parameters,
        );
        body = JSON.stringify({
          workflow: inlineWf,
          node_id: nodeId,
          steps,
          sample_rows: 100,
        });
      } else {
        body = JSON.stringify({ steps, sample_rows: 100 });
      }
      // Raw fetch (not the api client) → must carry the same auth, else the
      // backend returns 401 "Authentication required". (2026-06-15 fix.)
      const res = await fetch(url, {
        method: 'POST',
        headers: authHeaders(),
        body,
        signal: ac.signal,
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `HTTP ${res.status}`);
      }
      const data = (await res.json()) as PreviewResponse;
      setPreview(data);
    } catch (err: any) {
      if (err?.name === 'AbortError') return;  // superseded by newer call
      setPreviewError(err?.message || String(err));
    } finally {
      setPreviewLoading(false);
    }
  }

  // Debounced preview trigger — fires 800ms after the last steps change.
  // Z18: previously gated on workflowId. Now fires for unsaved recipes
  // too — runPreview itself routes to the ephemeral endpoint.
  useEffect(() => {
    if (steps.length === 0) {
      setPreview(null);
      setPreviewError(null);
      return;
    }
    const t = setTimeout(() => {
      runPreview();
    }, 800);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [steps, workflowId, nodeId]);

  // Mirror preview to the workflow store (powers the bottom PreviewPanel).
  // Clears on unmount / empty so a stale wrangler doesn't keep showing
  // data after the user deletes all sub-steps.
  //
  // Z20 (2026-05-23): off-by-one fix. The backend's preview_steps array
  // includes a synthetic INPUT entry at position 0 (label: "input",
  // index: -1) representing the pre-recipe rows. The user's first sub-
  // step lands at position 1, second at 2, etc. The bottom panel reads
  // wranglerEntry.steps[selectedStepIndex] directly, so we add +1 to
  // skip the input pseudo-step. Otherwise selecting "Filter rows" in
  // the modal showed unfiltered rows in the bottom panel.
  useEffect(() => {
    if (!preview || preview.steps.length === 0) {
      setWranglerPreview(nodeId, null);
      return;
    }
    // selectedIndex < 0 = no user-step selection → default to LAST step
    // so Refresh preview always lands on the freshest output.
    const target = selectedIndex < 0
      ? preview.steps.length - 1
      : Math.min(preview.steps.length - 1, selectedIndex + 1);
    setWranglerPreview(nodeId, {
      steps: preview.steps,
      sample_rows: preview.sample_rows,
      selectedStepIndex: Math.max(0, target),
    });
  }, [preview, nodeId, selectedIndex, setWranglerPreview]);

  // Keep the store's selected step in sync with the modal's selection.
  // Same +1 offset applies — modal's selectedIndex 0 = user's first step
  // = preview.steps[1] in the bottom panel.
  useEffect(() => {
    if (preview && selectedIndex >= 0) {
      const clamped = Math.min(preview.steps.length - 1, selectedIndex + 1);
      setWranglerSelectedStep(nodeId, clamped);
    }
  }, [selectedIndex, nodeId, preview, setWranglerSelectedStep]);

  // ── Selected step preview lookup ───────────────────────────────────────────
  const selectedPreview: PreviewStep | null = (() => {
    if (!preview) return null;
    if (selectedIndex < 0) return preview.steps[0] ?? null;
    return preview.steps.find((s) => s.index === selectedIndex) ?? null;
  })();
  const inputPreview = preview?.steps[0] ?? null;

  // PR 2: the columns flowing INTO the currently-selected step. Prefer the
  // wrangler preview, then fall back to the executed upstream/source node so
  // the config forms are usable as soon as Test Node has populated input data.
  const incomingColumns: { name: string; type: string }[] = (() => {
    if (!preview) return upstreamColumns;
    if (selectedIndex <= 0) {
      return inputPreview?.columns ?? upstreamColumns;
    }
    const prior = preview.steps.find((s) => s.index === selectedIndex - 1);
    return prior?.columns ?? inputPreview?.columns ?? upstreamColumns;
  })();

  // B7: per-step warnings about references to disabled-or-later steps.
  // Computed every render; cheap (small list of small ops).
  const stepWarnings = useMemo(() => computeStepWarnings(steps), [steps]);
  // 2026-06-15: the step in the recipe whose preview run failed (mapped back
  // to its id) so the list can flag it red.
  const errorStepId = useMemo(() => {
    const errStep = preview?.steps?.find((s) => s.status === 'error');
    if (!errStep || errStep.index < 0) return null;
    return steps[errStep.index]?.id ?? null;
  }, [preview, steps]);

  // ── Render ─────────────────────────────────────────────────────────────────
  if (steps.length === 0) {
    return (
      <StarterEmptyState
        onChoose={(op) => addStep(op)}
        workflowSaved={!!workflowId}
        isFileDataPrep={isFileDataPrep}
        incomingColumns={incomingColumns}
        sampleRows={upstreamInput.sample}
        rowCount={upstreamInput.rowCount}
      />
    );
  }

  return (
    <div className="flex flex-col h-full min-h-0 bg-slate-50">
      {/* Top — 3-pane workspace. Two ResizeHandles let the user drag the
          step-list and config-form widths; the preview pane auto-flexes. */}
      <div className="flex flex-1 min-h-0">
        {/* LEFT: step list */}
        <div
          className="shrink-0 bg-white overflow-y-auto border-r border-slate-200"
          style={{ width: stepListWidth }}
        >
          <StepListPanel
            steps={steps}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onAdd={addStep}
            onToggle={(id) =>
              updateStep(id, { enabled: !steps.find((s) => s.id === id)?.enabled })
            }
            onRemove={removeStep}
            onDuplicate={duplicateStep}
            onMove={moveStep}
            stepWarnings={stepWarnings}
            errorStepId={errorStepId}
          />
        </div>

        <ResizeHandle
          orientation="horizontal"
          edge="right-edge"
          value={stepListWidth}
          onResize={setStepListWidth}
          min={WRANGLER_STEPLIST_MIN}
          max={WRANGLER_STEPLIST_MAX}
          ariaLabel="Drag to resize step list"
          className="shrink-0"
        />

        {/* MIDDLE: step config form */}
        <div
          className="shrink-0 bg-white overflow-y-auto border-r border-slate-200"
          style={{ width: configFormWidth }}
        >
          {selectedStep ? (
            <StepConfigForm
              step={selectedStep}
              incomingColumns={incomingColumns}
              onLabelChange={(label) => updateStep(selectedStep.id, { label })}
              onConfigChange={(patch) => updateStepConfig(selectedStep.id, patch)}
            />
          ) : (
            <div className="p-4 text-sm text-slate-500">
              Click a step on the left to edit it.
            </div>
          )}
        </div>

        <ResizeHandle
          orientation="horizontal"
          edge="right-edge"
          value={configFormWidth}
          onResize={setConfigFormWidth}
          min={WRANGLER_CONFIG_MIN}
          max={WRANGLER_CONFIG_MAX}
          ariaLabel="Drag to resize step config form"
          className="shrink-0"
        />

        {/* RIGHT: live preview */}
        <div className="flex-1 min-w-0 bg-white flex flex-col overflow-hidden">
          <LivePreviewPane
            loading={previewLoading}
            error={previewError}
            inputPreview={inputPreview}
            selectedPreview={selectedPreview}
            selectedStep={selectedStep}
            previewExists={!!preview}
          />
        </div>
      </div>

      {/* Footer — slim action bar */}
      <div className="shrink-0 px-3 py-2 bg-white border-t border-slate-200 flex items-center gap-2 text-xs">
        <button
          type="button"
          className={[
            'px-2.5 py-1 border rounded-md',
            preview
              ? 'border-slate-300 hover:bg-slate-50 text-slate-700'
              : 'border-slate-200 text-slate-400 bg-slate-50 cursor-not-allowed',
          ].join(' ')}
          onClick={() => setShowSql((v) => !v)}
          disabled={!preview}
          title={preview ? 'Show compiled SQL for this wrangler' : 'Preview will load shortly'}
        >
          {showSql ? 'Hide compiled SQL' : 'Open compiled SQL'}
        </button>
        <button
          type="button"
          className={[
            'px-2.5 py-1 border rounded-md',
            previewLoading
              ? 'border-slate-200 text-slate-400 bg-slate-50 cursor-not-allowed'
              : 'border-slate-300 hover:bg-slate-50 text-slate-700',
          ].join(' ')}
          onClick={runPreview}
          disabled={previewLoading}
          title="Re-run preview now (auto-runs ~1s after edits)"
        >
          {previewLoading ? 'Refreshing...' : 'Refresh preview'}
        </button>
        <span className="ml-auto text-slate-500">
          {steps.filter((s) => s.enabled !== false).length} of {steps.length} step
          {steps.length === 1 ? '' : 's'} enabled
          {preview ? (
            <> · sample {preview.sample_rows} rows</>
          ) : previewLoading ? (
            <> · preview running…</>
          ) : null}
        </span>
      </div>

      {/* Generated SQL panel — bottom collapsible */}
      {showSql && preview && (
        <pre className="shrink-0 text-xs bg-slate-900 text-slate-100 p-3 max-h-[200px] overflow-auto whitespace-pre-wrap border-t border-slate-700">
          {preview.generated_sql}
        </pre>
      )}
    </div>
  );
}

// ── Starter empty state ─────────────────────────────────────────────────────

function StarterEmptyState({
  onChoose,
  workflowSaved,
  isFileDataPrep,
  incomingColumns = [],
  sampleRows = [],
  rowCount = 0,
}: {
  onChoose: (op: SubStepOp) => void;
  workflowSaved: boolean;
  isFileDataPrep?: boolean;
  incomingColumns?: { name: string; type: string }[];
  sampleRows?: Record<string, unknown>[];
  rowCount?: number;
}) {
  const starters: SubStepOp[] = [
    'filter',
    'select',
    'rename',
    'cast',
    'derive',
    'group_by',
    'sort',
    'dedupe',
    'sample',
    'flatten',
  ];
  const hasInput = incomingColumns.length > 0;
  const fmtCell = (v: unknown): string => {
    if (v === null || v === undefined) return '∅';
    if (typeof v === 'object') return JSON.stringify(v);
    const s = String(v);
    return s.length > 60 ? s.slice(0, 57) + '…' : s;
  };
  return (
    <div className="flex flex-col items-center h-full min-h-[400px] overflow-y-auto bg-slate-50 p-8">
      <div className="max-w-[760px] w-full">
        {/* INPUT DATA preview — show WHAT is flowing in before the user picks
            a transformation. (2026-06-15: empty state used to be a bare step
            picker; the incoming data was only visible via the bottom INPUT
            tab. A data wrangler should anchor on the data.) */}
        {hasInput ? (
          <div className="mb-6 rounded-xl border border-slate-200 bg-white overflow-hidden">
            <div className="px-4 py-2.5 border-b border-slate-200 flex items-center justify-between">
              <span className="text-xs font-bold text-slate-600 uppercase tracking-wider">
                Your input data
              </span>
              <span className="text-xs text-slate-500">
                {incomingColumns.length} column{incomingColumns.length === 1 ? '' : 's'}
                {rowCount > 0
                  ? ` · ${rowCount.toLocaleString()} row${rowCount === 1 ? '' : 's'}`
                  : ''}
              </span>
            </div>
            {sampleRows.length > 0 ? (
              <div className="overflow-x-auto max-h-[200px]">
                <table className="text-xs w-full">
                  <thead className="bg-slate-50 sticky top-0">
                    <tr>
                      {incomingColumns.map((c) => (
                        <th
                          key={c.name}
                          className="text-left font-semibold text-slate-600 px-3 py-1.5 whitespace-nowrap border-b border-slate-200"
                        >
                          {c.name}{' '}
                          <span className="text-[10px] text-slate-400 font-normal uppercase">
                            {c.type}
                          </span>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {sampleRows.slice(0, 5).map((row, i) => (
                      <tr key={i} className={i % 2 ? 'bg-slate-50/60' : 'bg-white'}>
                        {incomingColumns.map((c) => (
                          <td
                            key={c.name}
                            className="px-3 py-1.5 text-slate-700 whitespace-nowrap max-w-[220px] truncate"
                          >
                            {fmtCell(row[c.name])}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="px-4 py-3 flex flex-wrap gap-1.5">
                {incomingColumns.map((c) => (
                  <span
                    key={c.name}
                    className="px-2 py-0.5 rounded bg-slate-100 text-xs text-slate-600"
                  >
                    {c.name}{' '}
                    <span className="text-[10px] text-slate-400 uppercase">{c.type}</span>
                  </span>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div className="mb-6 rounded-xl border border-dashed border-slate-300 bg-white px-4 py-3 text-xs text-slate-500">
            No input preview yet. Connect a source and click{' '}
            <span className="font-semibold text-slate-700">Test Node</span> (or Run
            Sample) to see your incoming columns and rows here.
          </div>
        )}

        <div className="text-center mb-6">
          <div className="text-lg font-semibold text-slate-800 mb-1">
            {hasInput ? 'Add your first step' : 'Start your transformation'}
          </div>
          <div className="text-sm text-slate-500">
            Pick any cleanup step — you can add more, rearrange, disable, or remove them anytime.
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          {starters.map((op) => (
            <button
              key={op}
              type="button"
              onClick={() => onChoose(op)}
              className="flex items-start gap-3 p-4 bg-white border border-slate-200 hover:border-emerald-400 hover:shadow-md rounded-xl text-left transition-all group"
            >
              <div className="w-9 h-9 rounded-lg bg-emerald-100 group-hover:bg-emerald-200 flex items-center justify-center font-bold text-emerald-700 shrink-0 transition-colors">
                {OP_BADGE[op]}
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-sm font-semibold text-slate-800 group-hover:text-emerald-700 transition-colors">
                  {OP_LABELS[op]}
                </div>
                <div className="text-xs text-slate-500 mt-0.5">
                  {OP_DESCRIPTIONS[op]}
                </div>
              </div>
            </button>
          ))}
        </div>

        {/* Z18 (2026-05-23): the "save first" nag chip was removed.
            Per-step preview now works against the inline IR via the
            ephemeral endpoint — no save required. Adding the first
            step is enough; the auto-debounce fires preview after 800ms. */}
      </div>
    </div>
  );
}

// ── Step list (left column) ─────────────────────────────────────────────────

function StepListPanel({
  steps,
  selectedId,
  onSelect,
  onAdd,
  onToggle,
  onRemove,
  onDuplicate,
  onMove,
  stepWarnings,
  errorStepId = null,
}: {
  steps: SubStep[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onAdd: (op: SubStepOp) => void;
  onToggle: (id: string) => void;
  onRemove: (id: string) => void;
  onDuplicate: (id: string) => void;
  onMove: (id: string, direction: -1 | 1) => void;
  stepWarnings: Map<string, string>;
  errorStepId?: string | null;
}) {
  const [addOpen, setAddOpen] = useState(false);

  return (
    <div className="flex flex-col h-full">
      <div className="sticky top-0 z-10 px-3 py-2.5 bg-white border-b border-slate-200 flex items-center justify-between">
        <span className="text-xs font-bold text-slate-600 uppercase tracking-wider">
          Steps
        </span>
        <div className="relative">
          <button
            type="button"
            className="px-2 py-1 text-xs font-semibold bg-emerald-600 hover:bg-emerald-700 text-white rounded-md transition-colors"
            onClick={() => setAddOpen((v) => !v)}
          >
            + Add step
          </button>
          {addOpen && (
            <div
              className="absolute right-0 top-full mt-1 z-30 bg-white border border-slate-200 rounded-lg shadow-xl py-1 min-w-[220px]"
              onMouseLeave={() => setAddOpen(false)}
            >
              {(Object.keys(OP_LABELS) as SubStepOp[]).map((op) => (
                <button
                  key={op}
                  type="button"
                  className="w-full text-left px-3 py-2 hover:bg-emerald-50 text-sm flex items-start gap-2.5"
                  onClick={() => {
                    onAdd(op);
                    setAddOpen(false);
                  }}
                >
                  <div className="w-6 h-6 rounded bg-emerald-100 text-emerald-700 font-bold text-xs flex items-center justify-center shrink-0 mt-0.5">
                    {OP_BADGE[op]}
                  </div>
                  <div className="flex-1">
                    <div className="font-medium text-slate-800">{OP_LABELS[op]}</div>
                    <div className="text-xs text-slate-500">{OP_DESCRIPTIONS[op]}</div>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-2 space-y-1">
        {steps.map((step, idx) => {
          const selected = step.id === selectedId;
          const disabled = step.enabled === false;
          const errored = step.id === errorStepId;
          return (
            <div
              key={step.id}
              className={[
                'rounded-md px-2 py-1.5 cursor-pointer border transition-colors',
                errored
                  ? 'bg-red-50 border-red-300 ring-1 ring-red-300'
                  : selected
                  ? 'bg-emerald-50 border-emerald-300'
                  : 'bg-white border-slate-200 hover:border-slate-300 hover:bg-slate-50',
                disabled ? 'opacity-50' : '',
              ].join(' ')}
              onClick={() => onSelect(step.id)}
            >
              <div className="flex items-center gap-1.5 text-sm">
                <div className="w-5 h-5 rounded bg-emerald-100 text-emerald-700 font-bold text-[10px] flex items-center justify-center shrink-0">
                  {OP_BADGE[step.op]}
                </div>
                <span className="text-xs text-slate-400 w-4 shrink-0 font-mono">
                  {idx + 1}
                </span>
                <span className="flex-1 truncate font-medium text-slate-800">
                  {step.label || OP_LABELS[step.op]}
                </span>
                <button
                  type="button"
                  title="Move up"
                  className="text-slate-300 hover:text-slate-700 px-0.5 disabled:opacity-30 disabled:hover:text-slate-300"
                  onClick={(e) => {
                    e.stopPropagation();
                    onMove(step.id, -1);
                  }}
                  disabled={idx === 0}
                >
                  ▲
                </button>
                <button
                  type="button"
                  title="Move down"
                  className="text-slate-300 hover:text-slate-700 px-0.5 disabled:opacity-30 disabled:hover:text-slate-300"
                  onClick={(e) => {
                    e.stopPropagation();
                    onMove(step.id, 1);
                  }}
                  disabled={idx === steps.length - 1}
                >
                  ▼
                </button>
                <button
                  type="button"
                  title={disabled ? 'Enable' : 'Disable'}
                  className="text-slate-400 hover:text-slate-700 px-0.5"
                  onClick={(e) => {
                    e.stopPropagation();
                    onToggle(step.id);
                  }}
                >
                  {disabled ? '○' : '●'}
                </button>
                <button
                  type="button"
                  title="Duplicate"
                  className="text-slate-400 hover:text-slate-700 px-0.5"
                  onClick={(e) => {
                    e.stopPropagation();
                    onDuplicate(step.id);
                  }}
                >
                  ⎘
                </button>
                <button
                  type="button"
                  title="Delete"
                  className="text-slate-400 hover:text-red-600 px-0.5"
                  onClick={(e) => {
                    e.stopPropagation();
                    onRemove(step.id);
                  }}
                >
                  ✕
                </button>
              </div>
              <div className="text-xs text-slate-500 pl-7 truncate mt-0.5">
                {summarizeStep(step)}
              </div>
              {stepWarnings.get(step.id) && (
                <div
                  className="text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-1.5 py-1 mt-1 ml-7 flex items-start gap-1"
                  title={stepWarnings.get(step.id)}
                >
                  <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="shrink-0 mt-0.5">
                    <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                    <line x1="12" y1="9" x2="12" y2="13" />
                    <line x1="12" y1="17" x2="12.01" y2="17" />
                  </svg>
                  <span className="leading-tight">{stepWarnings.get(step.id)}</span>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Step config form dispatcher ──────────────────────────────────────────────

function StepConfigForm({
  step,
  incomingColumns,
  onLabelChange,
  onConfigChange,
}: {
  step: SubStep;
  incomingColumns: { name: string; type: string }[];
  onLabelChange: (label: string) => void;
  onConfigChange: (patch: Record<string, any>) => void;
}) {
  // Track the last <input>/<textarea> that had focus inside this form so
  // the column chips above can drop a column name at the cursor when
  // clicked. The chip's click itself moves focus to the button, so we
  // can't read activeElement at click time — we have to remember the
  // *previous* focused input.
  const lastFocusedInputRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);
  const onFormFocusCapture = (e: React.FocusEvent<HTMLDivElement>) => {
    const t = e.target as HTMLElement;
    if (t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement) {
      // Skip the step-label input — column names don't belong in labels.
      if (t.dataset.wranglerSkipInsert === '1') return;
      lastFocusedInputRef.current = t;
    }
  };
  const handleInsertColumn = (name: string): boolean => {
    const input = lastFocusedInputRef.current;
    if (!input || !document.body.contains(input)) return false;
    const quoted = /[^a-zA-Z0-9_]/.test(name) ? `"${name}"` : name;
    const start = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? input.value.length;
    const before = input.value.slice(0, start);
    const after = input.value.slice(end);
    // Add a leading space if the previous char isn't whitespace/operator.
    const needsSpace = before.length > 0 && !/[\s(.,=<>!+\-*/%]$/.test(before);
    const inserted = (needsSpace ? ' ' : '') + quoted;
    const next = before + inserted + after;
    // Use the native setter so React picks up the change event.
    const proto = input instanceof HTMLTextAreaElement
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    setter?.call(input, next);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    // Restore focus + position cursor after the inserted token.
    const cursor = start + inserted.length;
    input.focus();
    try { input.setSelectionRange(cursor, cursor); } catch { /* type=number etc. */ }
    return true;
  };

  return (
    <div
      className="flex flex-col h-full"
      onFocusCapture={onFormFocusCapture}
    >
      <div className="sticky top-0 z-10 px-3 py-2.5 bg-white border-b border-slate-200 flex items-center gap-2">
        <div className="w-6 h-6 rounded bg-emerald-100 text-emerald-700 font-bold text-xs flex items-center justify-center shrink-0">
          {OP_BADGE[step.op]}
        </div>
        <span className="text-xs font-bold text-slate-600 uppercase tracking-wider">
          {OP_LABELS[step.op]}
        </span>
      </div>

      {/* Inbound columns strip — chip behaviour:
          (1) If a text input below has focus, clicking inserts the column
              name at the cursor position (quoted if needed). This is the
              fast path for building filter expressions / derive formulas.
          (2) Otherwise, clipboard copy as fallback (legacy behaviour). */}
      <IncomingColumnsStrip
        columns={incomingColumns}
        onInsert={handleInsertColumn}
      />

      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        <div>
          <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
            Step label
          </label>
          <input
            type="text"
            value={step.label || ''}
            onChange={(e) => onLabelChange(e.target.value)}
            placeholder={OP_LABELS[step.op]}
            data-wrangler-skip-insert="1"
            className="mt-1 w-full px-2 py-1.5 text-sm border border-slate-300 rounded-md focus:outline-none focus:ring-2 focus:ring-emerald-500"
          />
        </div>

        <div className="border-t border-slate-200 pt-3">
          {/* P2-C (2026-05-18): every sub-step form receives the running
              column list so dropdowns / multi-selects / autocomplete
              source from real upstream + earlier-step-produced columns
              instead of forcing the user to remember + retype them. */}
          {step.op === 'filter' && (
            <FilterForm config={step.config} onChange={onConfigChange} incomingColumns={incomingColumns} />
          )}
          {step.op === 'select' && (
            <SelectForm config={step.config} onChange={onConfigChange} incomingColumns={incomingColumns} />
          )}
          {step.op === 'rename' && (
            <RenameForm config={step.config} onChange={onConfigChange} incomingColumns={incomingColumns} />
          )}
          {step.op === 'cast' && (
            <CastForm config={step.config} onChange={onConfigChange} incomingColumns={incomingColumns} />
          )}
          {step.op === 'derive' && (
            <DeriveForm config={step.config} onChange={onConfigChange} incomingColumns={incomingColumns} />
          )}
          {step.op === 'group_by' && (
            <GroupByForm config={step.config} onChange={onConfigChange} incomingColumns={incomingColumns} />
          )}
          {step.op === 'sort' && (
            <SortForm config={step.config} onChange={onConfigChange} incomingColumns={incomingColumns} />
          )}
          {step.op === 'dedupe' && (
            <DedupeForm config={step.config} onChange={onConfigChange} incomingColumns={incomingColumns} />
          )}
          {step.op === 'sample' && (
            <SampleForm config={step.config} onChange={onConfigChange} />
          )}
          {step.op === 'flatten' && (
            <FlattenForm config={step.config} onChange={onConfigChange} incomingColumns={incomingColumns} />
          )}
          {step.op === 'fill_nulls' && (
            <FillNullsForm config={step.config} onChange={onConfigChange} incomingColumns={incomingColumns} />
          )}
          {step.op === 'replace_values' && (
            <ReplaceValuesForm config={step.config} onChange={onConfigChange} incomingColumns={incomingColumns} />
          )}
          {step.op === 'split_column' && (
            <SplitColumnForm config={step.config} onChange={onConfigChange} incomingColumns={incomingColumns} />
          )}
        </div>
      </div>
    </div>
  );
}

// ── Incoming columns strip (PR 2) ───────────────────────────────────────────

/**
 * Type-aware chip strip showing the columns flowing INTO the selected
 * step. Each chip displays the column name + a compact type tag (INT,
 * VARCHAR, TS, …). Clicking a chip copies the name to the clipboard so
 * the user can paste it straight into an expression input.
 *
 * Empty state: when the wrangler hasn't run its preview yet, this
 * collapses to a single helpful line instead of taking up a 40px row.
 */
function IncomingColumnsStrip({
  columns,
  onInsert,
}: {
  columns: { name: string; type: string }[];
  /** When provided, the chip prefers to insert the column name into the
   *  caller's focused text input (returns true on success). On false /
   *  not-provided, the chip falls back to clipboard copy. */
  onInsert?: (name: string) => boolean;
}) {
  const [flashedAs, setFlashedAs] = useState<{ name: string; kind: 'inserted' | 'copied' } | null>(null);

  if (!columns.length) {
    return (
      <div className="px-3 py-2 text-xs text-slate-400 border-b border-slate-200 bg-slate-50/60">
        Input columns will appear after the source/upstream step runs or the preview loads.
      </div>
    );
  }

  const flash = (name: string, kind: 'inserted' | 'copied') => {
    setFlashedAs({ name, kind });
    setTimeout(() => setFlashedAs((c) => (c?.name === name ? null : c)), 1200);
  };

  const handleClick = async (name: string) => {
    // 1. Prefer insert-at-cursor when a target input is focused.
    if (onInsert) {
      const ok = onInsert(name);
      if (ok) {
        flash(name, 'inserted');
        return;
      }
    }
    // 2. Fall back to clipboard copy (legacy behaviour).
    try {
      await navigator.clipboard.writeText(name);
      flash(name, 'copied');
    } catch {
      // Older browsers without clipboard API — silent fallback.
    }
  };

  return (
    <div className="px-3 py-2 border-b border-slate-200 bg-slate-50/60">
      <div className="flex items-center justify-between mb-1">
        <span className="text-[10px] font-bold text-slate-500 uppercase tracking-wider">
          Incoming columns ({columns.length})
        </span>
        <span className="text-[10px] text-slate-400">
          Click to insert · auto-quotes special names
        </span>
      </div>
      <div className="flex flex-wrap gap-1">
        {columns.map((c) => {
          const isFlash = flashedAs?.name === c.name;
          const flashKind = isFlash ? flashedAs!.kind : null;
          const title = `${c.name} · ${c.type} (click to insert into the focused input, or copy if none)`;
          return (
            <button
              key={c.name}
              type="button"
              onClick={() => handleClick(c.name)}
              title={title}
              className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs border transition-colors ${
                flashKind === 'inserted'
                  ? 'bg-amber-50 border-amber-300 text-amber-800'
                  : flashKind === 'copied'
                    ? 'bg-emerald-50 border-emerald-300 text-emerald-700'
                    : 'bg-white border-slate-200 hover:border-indigo-300 hover:bg-indigo-50 text-slate-700'
              }`}
            >
              <span className="font-mono">{c.name}</span>
              <span className="text-[9px] uppercase font-semibold text-slate-400">
                {compactType(c.type)}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/** Shorten DuckDB types to 3-4 letters so they fit inside a chip. */
function compactType(t: string): string {
  const u = (t || '').toUpperCase();
  if (u.startsWith('VARCHAR')) return 'STR';
  if (u.startsWith('BIGINT') || u === 'INTEGER' || u === 'INT') return 'INT';
  if (u.startsWith('DOUBLE') || u.startsWith('FLOAT') || u.startsWith('DECIMAL')) return 'NUM';
  if (u.startsWith('BOOLEAN')) return 'BOOL';
  if (u.startsWith('TIMESTAMP')) return 'TS';
  if (u === 'DATE') return 'DATE';
  if (u === 'TIME') return 'TIME';
  if (u.startsWith('STRUCT') || u.startsWith('MAP') || u.startsWith('LIST')) return 'OBJ';
  return u.slice(0, 4);
}

// ── Per-op forms ────────────────────────────────────────────────────────────

// ── B3 (2026-06-15): cleaning sub-step editors ──────────────────────────────

type FormProps = { config: any; onChange: (p: any) => void; incomingColumns?: { name: string; type: string }[] };

const _wColSelect = "flex-1 px-2 py-1 text-xs border border-slate-300 rounded focus:outline-none focus:ring-2 focus:ring-emerald-500";
const _wInput = "flex-1 px-2 py-1 text-xs font-mono border border-slate-300 rounded focus:outline-none focus:ring-2 focus:ring-emerald-500";

function FillNullsForm({ config, onChange, incomingColumns = [] }: FormProps) {
  const fills: Array<{ column: string; value: string }> = config.fills || [];
  const set = (next: any[]) => onChange({ ...config, fills: next });
  return (
    <div className="flex flex-col gap-2">
      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">Fill NULLs with a value</label>
      {fills.map((f, i) => (
        <div key={i} className="flex items-center gap-1">
          <select value={f.column || ''} className={_wColSelect}
            onChange={(e) => set(fills.map((x, j) => (j === i ? { ...x, column: e.target.value } : x)))}>
            <option value="">— column —</option>
            {incomingColumns.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
          </select>
          <input value={f.value ?? ''} placeholder="value (NULL, 0, N/A)" className={_wInput}
            onChange={(e) => set(fills.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))} />
          <button type="button" className="text-slate-400 hover:text-red-500 px-1"
            onClick={() => set(fills.filter((_, j) => j !== i))}>×</button>
        </div>
      ))}
      <button type="button" className="text-xs text-emerald-600 hover:underline self-start"
        onClick={() => set([...fills, { column: '', value: '' }])}>+ Add column</button>
    </div>
  );
}

function ReplaceValuesForm({ config, onChange, incomingColumns = [] }: FormProps) {
  const repls: Array<{ column: string; find: string; replace: string }> = config.replacements || [];
  const set = (next: any[]) => onChange({ ...config, replacements: next });
  return (
    <div className="flex flex-col gap-2">
      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">Replace exact values</label>
      {repls.map((r, i) => (
        <div key={i} className="flex items-center gap-1">
          <select value={r.column || ''} className={_wColSelect}
            onChange={(e) => set(repls.map((x, j) => (j === i ? { ...x, column: e.target.value } : x)))}>
            <option value="">— column —</option>
            {incomingColumns.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
          </select>
          <input value={r.find ?? ''} placeholder="find" className={_wInput}
            onChange={(e) => set(repls.map((x, j) => (j === i ? { ...x, find: e.target.value } : x)))} />
          <input value={r.replace ?? ''} placeholder="replace (NULL = null)" className={_wInput}
            onChange={(e) => set(repls.map((x, j) => (j === i ? { ...x, replace: e.target.value } : x)))} />
          <button type="button" className="text-slate-400 hover:text-red-500 px-1"
            onClick={() => set(repls.filter((_, j) => j !== i))}>×</button>
        </div>
      ))}
      <button type="button" className="text-xs text-emerald-600 hover:underline self-start"
        onClick={() => set([...repls, { column: '', find: '', replace: '' }])}>+ Add rule</button>
    </div>
  );
}

function SplitColumnForm({ config, onChange, incomingColumns = [] }: FormProps) {
  const into: string[] = config.into || [];
  return (
    <div className="flex flex-col gap-2">
      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">Split column by delimiter</label>
      <div className="flex items-center gap-1">
        <select value={config.column || ''} className={_wColSelect}
          onChange={(e) => onChange({ ...config, column: e.target.value })}>
          <option value="">— column —</option>
          {incomingColumns.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
        </select>
        <input value={config.delimiter ?? ','} placeholder="delimiter"
          onChange={(e) => onChange({ ...config, delimiter: e.target.value })}
          className="w-24 px-2 py-1 text-xs font-mono border border-slate-300 rounded focus:outline-none focus:ring-2 focus:ring-emerald-500" />
      </div>
      <label className="text-[10px] text-slate-500">New column names (comma-separated, in order)</label>
      <input value={into.join(', ')} placeholder="first, last"
        onChange={(e) => onChange({ ...config, into: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })}
        className="w-full px-2 py-1 text-xs font-mono border border-slate-300 rounded focus:outline-none focus:ring-2 focus:ring-emerald-500" />
    </div>
  );
}

function FilterForm({
  config, onChange, incomingColumns = [],
}: { config: any; onChange: (p: any) => void; incomingColumns?: { name: string; type: string }[] }) {
  // 2026-05-30: matches the main ConfigPanel FilterConfig expression-mode
  // UX — clickable column chips beneath the textarea so users can
  // insert column names at the cursor instead of typing them by hand.
  // The Data Wrangler had this for Derive but was missing it here.
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const insertAtCursor = (text: string) => {
    const ref = textareaRef.current;
    const current = config.expression || '';
    if (!ref) {
      onChange({ mode: 'expression', expression: current + text });
      return;
    }
    const start = ref.selectionStart ?? current.length;
    const end = ref.selectionEnd ?? current.length;
    const next = current.slice(0, start) + text + current.slice(end);
    onChange({ mode: 'expression', expression: next });
    setTimeout(() => {
      ref.focus();
      const pos = start + text.length;
      ref.setSelectionRange(pos, pos);
    }, 0);
  };

  return (
    <div className="flex flex-col gap-2">
      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
        Filter expression (SQL)
      </label>
      <textarea
        ref={textareaRef}
        value={config.expression || ''}
        onChange={(e) => onChange({ mode: 'expression', expression: e.target.value })}
        placeholder="amount > 100 AND status = 'active'"
        rows={3}
        className="w-full px-2 py-1.5 text-sm font-mono border border-slate-300 rounded-md focus:outline-none focus:ring-2 focus:ring-emerald-500"
      />
      {incomingColumns.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {incomingColumns.slice(0, 16).map((c) => (
            <button
              key={c.name}
              type="button"
              onClick={() => insertAtCursor(c.name)}
              title={`Insert "${c.name}" at cursor · ${c.type}`}
              className="text-[10px] font-mono px-1.5 py-0.5 rounded border border-slate-200 bg-white hover:bg-emerald-50 hover:border-emerald-300 text-slate-600"
            >
              {c.name}
            </button>
          ))}
          {incomingColumns.length > 16 && (
            <span className="text-[10px] text-slate-400 pt-0.5">
              +{incomingColumns.length - 16} more
            </span>
          )}
        </div>
      )}
      <p className="text-xs text-slate-500">
        SQL WHERE expression. Click a chip above to insert a column name at the cursor.
      </p>
    </div>
  );
}

function SelectForm({
  config, onChange, incomingColumns = [],
}: { config: any; onChange: (p: any) => void; incomingColumns?: { name: string; type: string }[] }) {
  const cols: string[] = config.columns || [];
  const selected = new Set(cols);
  const toggle = (name: string) => {
    if (selected.has(name)) selected.delete(name); else selected.add(name);
    onChange({ columns: Array.from(selected) });
  };
  // P2-C (2026-05-18): replaced the free-form comma-separated text
  // input with a column-chip multi-select when upstream columns are
  // known. Falls back to text mode when no incoming columns yet (e.g.
  // before first preview run).
  if (incomingColumns.length === 0) {
    return (
      <div className="flex flex-col gap-2">
        <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
          Columns to keep (comma-separated)
        </label>
        <input
          type="text"
          value={cols.join(', ')}
          onChange={(e) => {
            const parsed = e.target.value.split(',').map((s) => s.trim()).filter(Boolean);
            onChange({ columns: parsed });
          }}
          placeholder="customer_id, order_date, amount"
          className="w-full px-2 py-1.5 text-sm font-mono border border-slate-300 rounded-md focus:outline-none focus:ring-2 focus:ring-emerald-500"
        />
        <p className="text-[10px] text-slate-400">Run preview once for click-to-pick column chips.</p>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
          Columns to keep
        </label>
        <div className="flex gap-1.5 text-[10px]">
          <button
            type="button"
            onClick={() => onChange({ columns: incomingColumns.map((c) => c.name) })}
            className="px-1.5 py-0.5 border border-slate-200 rounded hover:bg-slate-50"
          >
            Select all
          </button>
          <button
            type="button"
            onClick={() => onChange({ columns: [] })}
            className="px-1.5 py-0.5 border border-slate-200 rounded hover:bg-slate-50"
          >
            Clear
          </button>
        </div>
      </div>
      <div className="flex flex-wrap gap-1">
        {incomingColumns.map((c) => {
          const on = selected.has(c.name);
          return (
            <button
              key={c.name}
              type="button"
              onClick={() => toggle(c.name)}
              className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs border transition-colors ${
                on
                  ? 'bg-emerald-100 border-emerald-300 text-emerald-800'
                  : 'bg-white border-slate-200 hover:border-emerald-300 text-slate-600'
              }`}
              title={`${c.name} · ${c.type}`}
            >
              <span className="font-mono">{c.name}</span>
            </button>
          );
        })}
      </div>
      <p className="text-[10px] text-slate-400">
        {cols.length} of {incomingColumns.length} columns selected
        {cols.length === 0 && ' (empty = keep all)'}
      </p>
    </div>
  );
}

function RenameForm({
  config, onChange, incomingColumns = [],
}: { config: any; onChange: (p: any) => void; incomingColumns?: { name: string; type: string }[] }) {
  const map = (config.rename_map || {}) as Record<string, string>;
  const entries = Object.entries(map);

  function setEntry(idx: number, oldKey: string, value: string) {
    const next: Record<string, string> = {};
    entries.forEach(([k, v], i) => {
      if (i === idx) next[oldKey] = value;
      else next[k] = v;
    });
    onChange({ rename_map: next });
  }

  function setSourceCol(idx: number, newSource: string) {
    const next: Record<string, string> = {};
    entries.forEach(([k, v], i) => {
      if (i === idx) next[newSource] = v;
      else next[k] = v;
    });
    onChange({ rename_map: next });
  }

  function addPair() {
    onChange({ rename_map: { ...map, ['']: '' } });
  }

  function removePair(idx: number) {
    const next: Record<string, string> = {};
    entries.forEach(([k, v], i) => {
      if (i !== idx) next[k] = v;
    });
    onChange({ rename_map: next });
  }

  // P2-C: when upstream columns are known, render a select for "from"
  // (and a datalist for autocomplete). Falls back to free text otherwise.
  const datalistId = 'wrangler-rename-cols';
  return (
    <div className="flex flex-col gap-2">
      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
        Rename columns
      </label>
      {incomingColumns.length > 0 && (
        <datalist id={datalistId}>
          {incomingColumns.map((c) => <option key={c.name} value={c.name} />)}
        </datalist>
      )}
      <div className="flex flex-col gap-1.5">
        {entries.map(([fromCol, toCol], idx) => (
          <div key={idx} className="flex items-center gap-1.5">
            <input
              type="text"
              list={incomingColumns.length > 0 ? datalistId : undefined}
              value={fromCol}
              onChange={(e) => setSourceCol(idx, e.target.value)}
              placeholder={incomingColumns.length > 0 ? 'pick a column' : 'from'}
              className="flex-1 min-w-0 px-2 py-1 text-sm font-mono border border-slate-300 rounded-md"
            />
            <span className="text-slate-400">→</span>
            <input
              type="text"
              value={toCol}
              onChange={(e) => setEntry(idx, fromCol, e.target.value)}
              placeholder="to"
              className="flex-1 min-w-0 px-2 py-1 text-sm font-mono border border-slate-300 rounded-md"
            />
            <button
              type="button"
              className="text-slate-400 hover:text-red-600 px-1"
              onClick={() => removePair(idx)}
            >
              ✕
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        className="self-start px-2 py-1 text-xs border border-slate-300 hover:bg-slate-50 rounded-md"
        onClick={addPair}
      >
        + Add rename
      </button>
    </div>
  );
}

const CAST_TYPES = [
  'INTEGER', 'BIGINT', 'DOUBLE', 'VARCHAR',
  'BOOLEAN', 'DATE', 'TIMESTAMP', 'DECIMAL(18,2)',
];

function CastForm({
  config, onChange, incomingColumns = [],
}: { config: any; onChange: (p: any) => void; incomingColumns?: { name: string; type: string }[] }) {
  const casts: { column: string; to_type: string }[] = config.casts || [];

  function update(idx: number, patch: Partial<{ column: string; to_type: string }>) {
    onChange({ casts: casts.map((c, i) => (i === idx ? { ...c, ...patch } : c)) });
  }
  function add() { onChange({ casts: [...casts, { column: '', to_type: 'VARCHAR' }] }); }
  function remove(idx: number) { onChange({ casts: casts.filter((_, i) => i !== idx) }); }

  return (
    <div className="flex flex-col gap-2">
      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
        Cast columns to type
      </label>
      <div className="flex flex-col gap-1.5">
        {casts.map((c, idx) => (
          <div key={idx} className="flex items-center gap-1.5">
            {/* P2-C: column picker when upstream cols are known, free text otherwise */}
            {incomingColumns.length > 0 ? (
              <select
                value={c.column}
                onChange={(e) => update(idx, { column: e.target.value })}
                className="flex-1 min-w-0 px-2 py-1 text-sm font-mono border border-slate-300 rounded-md bg-white"
              >
                <option value="">— column —</option>
                {incomingColumns.map((col) => (
                  <option key={col.name} value={col.name}>
                    {col.name}{col.type ? ` (${col.type})` : ''}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={c.column}
                onChange={(e) => update(idx, { column: e.target.value })}
                placeholder="column"
                className="flex-1 min-w-0 px-2 py-1 text-sm font-mono border border-slate-300 rounded-md"
              />
            )}
            <span className="text-slate-400">→</span>
            <select
              value={c.to_type}
              onChange={(e) => update(idx, { to_type: e.target.value })}
              className="px-2 py-1 text-sm border border-slate-300 rounded-md"
            >
              {CAST_TYPES.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
            <button
              type="button"
              className="text-slate-400 hover:text-red-600 px-1"
              onClick={() => remove(idx)}
            >
              ✕
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        className="self-start px-2 py-1 text-xs border border-slate-300 hover:bg-slate-50 rounded-md"
        onClick={add}
      >
        + Add cast
      </button>
    </div>
  );
}

function DeriveForm({
  config, onChange, incomingColumns = [],
}: { config: any; onChange: (p: any) => void; incomingColumns?: { name: string; type: string }[] }) {
  const derived: { name: string; expression: string }[] = config.derived || [];
  const inputRefs = useRef<Array<HTMLInputElement | null>>([]);

  function update(idx: number, patch: Partial<{ name: string; expression: string }>) {
    onChange({ derived: derived.map((d, i) => (i === idx ? { ...d, ...patch } : d)) });
  }
  function add() { onChange({ derived: [...derived, { name: '', expression: '' }] }); }
  function remove(idx: number) { onChange({ derived: derived.filter((_, i) => i !== idx) }); }

  // P2-C residual (2026-05-18): clickable column chips beneath each
  // expression input. Clicking inserts the column name at the current
  // cursor position (or appends if no cursor). Big DX upgrade — the
  // user can build `split_part(email, '@', 2)` without retyping
  // "email" by hand. Falls back to a static list when no upstream
  // columns are known yet.
  function insertAtCursor(idx: number, text: string) {
    const ref = inputRefs.current[idx];
    const current = derived[idx]?.expression || '';
    if (!ref) {
      update(idx, { expression: current + text });
      return;
    }
    const start = ref.selectionStart ?? current.length;
    const end = ref.selectionEnd ?? current.length;
    const next = current.slice(0, start) + text + current.slice(end);
    update(idx, { expression: next });
    // Restore cursor just after the inserted text.
    setTimeout(() => {
      ref.focus();
      const pos = start + text.length;
      ref.setSelectionRange(pos, pos);
    }, 0);
  }

  return (
    <div className="flex flex-col gap-2">
      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
        Derived columns
      </label>
      <div className="flex flex-col gap-1.5">
        {derived.map((d, idx) => (
          <div key={idx} className="flex flex-col gap-1 p-2 bg-slate-50 rounded-md">
            <div className="flex items-center gap-1.5">
              <input
                type="text"
                value={d.name}
                onChange={(e) => update(idx, { name: e.target.value })}
                placeholder="new column name"
                className="flex-1 min-w-0 px-2 py-1 text-sm font-mono border border-slate-300 rounded-md bg-white"
              />
              <button
                type="button"
                className="text-slate-400 hover:text-red-600 px-1"
                onClick={() => remove(idx)}
              >
                ✕
              </button>
            </div>
            <input
              ref={(el) => { inputRefs.current[idx] = el; }}
              type="text"
              value={d.expression}
              onChange={(e) => update(idx, { expression: e.target.value })}
              placeholder="= e.g. split_part(email, '@', 2)"
              className="w-full px-2 py-1 text-sm font-mono border border-slate-300 rounded-md bg-white"
            />
            {incomingColumns.length > 0 && (
              <div className="flex flex-wrap gap-1 pt-0.5">
                {incomingColumns.slice(0, 12).map((c) => (
                  <button
                    key={c.name}
                    type="button"
                    onClick={() => insertAtCursor(idx, c.name)}
                    title={`Insert "${c.name}" at cursor · ${c.type}`}
                    className="text-[10px] font-mono px-1.5 py-0.5 rounded border border-slate-200 bg-white hover:bg-emerald-50 hover:border-emerald-300 text-slate-600"
                  >
                    {c.name}
                  </button>
                ))}
                {incomingColumns.length > 12 && (
                  <span className="text-[10px] text-slate-400 pt-0.5">+{incomingColumns.length - 12} more</span>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
      <button
        type="button"
        className="self-start px-2 py-1 text-xs border border-slate-300 hover:bg-slate-50 rounded-md"
        onClick={add}
      >
        + Add derived column
      </button>
    </div>
  );
}

const AGG_FUNCS = ['SUM', 'COUNT', 'AVG', 'MIN', 'MAX', 'COUNT_DISTINCT'];

function GroupByForm({
  config, onChange, incomingColumns = [],
}: { config: any; onChange: (p: any) => void; incomingColumns?: { name: string; type: string }[] }) {
  const keys: string[] = config.keys || [];
  const aggs: { func: string; column: string; alias: string }[] = config.aggregations || [];

  function updateAgg(idx: number, patch: Partial<{ func: string; column: string; alias: string }>) {
    onChange({ aggregations: aggs.map((a, i) => (i === idx ? { ...a, ...patch } : a)) });
  }
  function addAgg() { onChange({ aggregations: [...aggs, { func: 'SUM', column: '', alias: '' }] }); }
  function removeAgg(idx: number) { onChange({ aggregations: aggs.filter((_, i) => i !== idx) }); }

  const keysSet = new Set(keys);
  function toggleKey(name: string) {
    if (keysSet.has(name)) keysSet.delete(name); else keysSet.add(name);
    onChange({ keys: Array.from(keysSet) });
  }

  return (
    <div className="flex flex-col gap-3">
      <div>
        <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
          Group by keys
        </label>
        {/* P2-C residual (2026-05-18): chip multi-select when upstream
            cols are known, falls back to comma-separated text otherwise. */}
        {incomingColumns.length > 0 ? (
          <div className="mt-1 flex flex-wrap gap-1">
            {incomingColumns.map((c) => {
              const on = keysSet.has(c.name);
              return (
                <button
                  key={c.name}
                  type="button"
                  onClick={() => toggleKey(c.name)}
                  title={`${c.name} · ${c.type}`}
                  className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs border transition-colors ${
                    on
                      ? 'bg-emerald-100 border-emerald-300 text-emerald-800'
                      : 'bg-white border-slate-200 hover:border-emerald-300 text-slate-600'
                  }`}
                >
                  <span className="font-mono">{c.name}</span>
                </button>
              );
            })}
          </div>
        ) : (
          <input
            type="text"
            value={keys.join(', ')}
            onChange={(e) =>
              onChange({
                keys: e.target.value
                  .split(',')
                  .map((s) => s.trim())
                  .filter(Boolean),
              })
            }
            placeholder="customer_id, fiscal_year"
            className="mt-1 w-full px-2 py-1.5 text-sm font-mono border border-slate-300 rounded-md"
          />
        )}
      </div>
      <div>
        <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
          Aggregations
        </label>
        <div className="mt-1 flex flex-col gap-1.5">
          {aggs.map((a, idx) => (
            <div key={idx} className="flex items-center gap-1.5">
              <select
                value={a.func}
                onChange={(e) => updateAgg(idx, { func: e.target.value })}
                className="px-2 py-1 text-sm border border-slate-300 rounded-md"
              >
                {AGG_FUNCS.map((f) => (
                  <option key={f} value={f}>{f}</option>
                ))}
              </select>
              {/* P2-C residual: aggregation column as dropdown (with * for COUNT) */}
              {incomingColumns.length > 0 ? (
                <select
                  value={a.column}
                  onChange={(e) => updateAgg(idx, { column: e.target.value })}
                  className="flex-1 min-w-0 px-2 py-1 text-sm font-mono border border-slate-300 rounded-md bg-white"
                >
                  <option value="*">*</option>
                  {incomingColumns.map((c) => (
                    <option key={c.name} value={c.name}>{c.name}</option>
                  ))}
                </select>
              ) : (
                <input
                  type="text"
                  value={a.column}
                  onChange={(e) => updateAgg(idx, { column: e.target.value })}
                  placeholder="column (or *)"
                  className="flex-1 min-w-0 px-2 py-1 text-sm font-mono border border-slate-300 rounded-md"
                />
              )}
              <span className="text-slate-400 text-xs">AS</span>
              <input
                type="text"
                value={a.alias}
                onChange={(e) => updateAgg(idx, { alias: e.target.value })}
                placeholder="alias"
                className="w-24 px-2 py-1 text-sm font-mono border border-slate-300 rounded-md"
              />
              <button
                type="button"
                className="text-slate-400 hover:text-red-600 px-1"
                onClick={() => removeAgg(idx)}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
        <button
          type="button"
          className="mt-1.5 self-start px-2 py-1 text-xs border border-slate-300 hover:bg-slate-50 rounded-md"
          onClick={addAgg}
        >
          + Add aggregation
        </button>
      </div>
    </div>
  );
}

// ── P2-B (2026-05-18) new sub-step forms ────────────────────────────────────

function SortForm({
  config, onChange, incomingColumns = [],
}: { config: any; onChange: (p: any) => void; incomingColumns?: { name: string; type: string }[] }) {
  const sortBy: string[] = config.sort_by || [];
  const direction: string = config.direction || 'ASC';
  const set = new Set(sortBy);
  const toggle = (name: string) => {
    if (set.has(name)) set.delete(name); else set.add(name);
    onChange({ sort_by: Array.from(set), direction });
  };
  return (
    <div className="flex flex-col gap-2">
      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
        Sort by columns
      </label>
      {incomingColumns.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {incomingColumns.map((c) => {
            const on = set.has(c.name);
            return (
              <button
                key={c.name}
                type="button"
                onClick={() => toggle(c.name)}
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs border transition-colors ${
                  on
                    ? 'bg-emerald-100 border-emerald-300 text-emerald-800'
                    : 'bg-white border-slate-200 hover:border-emerald-300 text-slate-600'
                }`}
              >
                <span className="font-mono">{c.name}</span>
              </button>
            );
          })}
        </div>
      ) : (
        <input
          type="text"
          value={sortBy.join(', ')}
          onChange={(e) => onChange({ sort_by: e.target.value.split(',').map((s) => s.trim()).filter(Boolean), direction })}
          placeholder="amount, name"
          className="w-full px-2 py-1.5 text-sm font-mono border border-slate-300 rounded-md"
        />
      )}
      <div className="flex items-center gap-2">
        <label className="text-xs text-slate-500">Direction</label>
        <select
          value={direction}
          onChange={(e) => onChange({ sort_by: sortBy, direction: e.target.value })}
          className="px-2 py-1 text-xs border border-slate-300 rounded-md bg-white"
        >
          <option value="ASC">Ascending</option>
          <option value="DESC">Descending</option>
        </select>
      </div>
    </div>
  );
}

function DedupeForm({
  config, onChange, incomingColumns = [],
}: { config: any; onChange: (p: any) => void; incomingColumns?: { name: string; type: string }[] }) {
  const key: string[] = config.key || [];
  const strategy: string = config.strategy || 'keep_first';
  const set = new Set(key);
  const toggle = (name: string) => {
    if (set.has(name)) set.delete(name); else set.add(name);
    onChange({ key: Array.from(set), strategy });
  };
  return (
    <div className="flex flex-col gap-2">
      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
        Dedupe key columns
      </label>
      {incomingColumns.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {incomingColumns.map((c) => {
            const on = set.has(c.name);
            return (
              <button
                key={c.name}
                type="button"
                onClick={() => toggle(c.name)}
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs border transition-colors ${
                  on
                    ? 'bg-emerald-100 border-emerald-300 text-emerald-800'
                    : 'bg-white border-slate-200 hover:border-emerald-300 text-slate-600'
                }`}
              >
                <span className="font-mono">{c.name}</span>
              </button>
            );
          })}
        </div>
      ) : (
        <input
          type="text"
          value={key.join(', ')}
          onChange={(e) => onChange({ key: e.target.value.split(',').map((s) => s.trim()).filter(Boolean), strategy })}
          placeholder="order_id, email"
          className="w-full px-2 py-1.5 text-sm font-mono border border-slate-300 rounded-md"
        />
      )}
      <div className="flex items-center gap-2">
        <label className="text-xs text-slate-500">Strategy</label>
        <select
          value={strategy}
          onChange={(e) => onChange({ key, strategy: e.target.value })}
          className="px-2 py-1 text-xs border border-slate-300 rounded-md bg-white"
        >
          <option value="keep_first">Keep first</option>
          <option value="keep_last">Keep last</option>
        </select>
      </div>
    </div>
  );
}

function SampleForm({ config, onChange }: { config: any; onChange: (p: any) => void }) {
  const method: string = config.method || 'first';
  const count: number = config.count ?? 100;
  return (
    <div className="flex flex-col gap-2">
      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
        Sample rows
      </label>
      <div className="flex items-center gap-2">
        <select
          value={method}
          onChange={(e) => onChange({ method: e.target.value, count })}
          className="px-2 py-1 text-sm border border-slate-300 rounded-md bg-white"
        >
          <option value="first">First N</option>
          <option value="random">Random N</option>
        </select>
        <input
          type="number"
          value={count}
          min={1}
          onChange={(e) => onChange({ method, count: parseInt(e.target.value) || 1 })}
          className="w-24 px-2 py-1 text-sm font-mono border border-slate-300 rounded-md"
        />
        <span className="text-xs text-slate-500">rows</span>
      </div>
    </div>
  );
}

function FlattenForm({
  config, onChange, incomingColumns = [],
}: { config: any; onChange: (p: any) => void; incomingColumns?: { name: string; type: string }[] }) {
  const column: string = config.column || '';
  const prefix: string = config.prefix || '';
  const keepOriginal: boolean = !!config.keep_original;
  // Highlight columns whose declared type is STRUCT/MAP — those are
  // the meaningful targets for flatten. Plain VARCHAR/INT can be
  // selected but won't produce nested expansion.
  const structColumns = incomingColumns.filter((c) =>
    /STRUCT|MAP|OBJECT|RECORD/.test(c.type.toUpperCase()),
  );
  return (
    <div className="flex flex-col gap-2">
      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
        Column to expand
      </label>
      {incomingColumns.length > 0 ? (
        <select
          value={column}
          onChange={(e) => onChange({ column: e.target.value, prefix, keep_original: keepOriginal })}
          className="w-full px-2 py-1.5 text-sm font-mono border border-slate-300 rounded-md bg-white"
        >
          <option value="">— column —</option>
          {structColumns.length > 0 && (
            <optgroup label="STRUCT / MAP (recommended)">
              {structColumns.map((c) => (
                <option key={c.name} value={c.name}>{c.name} ({c.type})</option>
              ))}
            </optgroup>
          )}
          <optgroup label="Other columns">
            {incomingColumns.filter((c) => !structColumns.includes(c)).map((c) => (
              <option key={c.name} value={c.name}>{c.name} ({c.type})</option>
            ))}
          </optgroup>
        </select>
      ) : (
        <input
          type="text"
          value={column}
          onChange={(e) => onChange({ column: e.target.value, prefix, keep_original: keepOriginal })}
          placeholder="data"
          className="w-full px-2 py-1.5 text-sm font-mono border border-slate-300 rounded-md"
        />
      )}
      <label className="text-xs text-slate-500 mt-1">Column prefix (optional)</label>
      <input
        type="text"
        value={prefix}
        onChange={(e) => onChange({ column, prefix: e.target.value, keep_original: keepOriginal })}
        placeholder="user_"
        className="w-full px-2 py-1 text-sm font-mono border border-slate-200 rounded-md"
      />
      <label className="flex items-center gap-1.5 text-xs text-slate-600 mt-1">
        <input
          type="checkbox"
          checked={keepOriginal}
          onChange={(e) => onChange({ column, prefix, keep_original: e.target.checked })}
        />
        Keep the original nested column alongside the expanded fields
      </label>
    </div>
  );
}

// ── Live preview pane ────────────────────────────────────────────────────────

function LivePreviewPane({
  loading,
  error,
  inputPreview,
  selectedPreview,
  selectedStep,
  previewExists,
}: {
  loading: boolean;
  error: string | null;
  inputPreview: PreviewStep | null;
  selectedPreview: PreviewStep | null;
  selectedStep: SubStep | null;
  previewExists: boolean;
}) {
  const inRows = inputPreview?.row_count ?? 0;
  const outRows = selectedPreview?.row_count ?? 0;
  const delta = selectedPreview?.schema_delta;
  const hasSchemaChange =
    delta && (delta.added.length > 0 || delta.removed.length > 0 || delta.retyped.length > 0);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="sticky top-0 z-10 px-3 py-2 bg-white border-b border-slate-200">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-xs font-bold text-slate-600 uppercase tracking-wider shrink-0">
              Preview
            </span>
            {selectedStep && (
              <span className="text-xs text-slate-500 truncate">
                after step: <span className="font-semibold text-slate-700">{selectedStep.label || selectedStep.op}</span>
              </span>
            )}
            {loading && (
              <span className="text-xs text-emerald-600 flex items-center gap-1">
                <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
                running…
              </span>
            )}
          </div>
          {selectedPreview && (
            <span className="text-xs text-slate-600 shrink-0 font-mono">
              {inRows} → {outRows} rows
              {outRows !== inRows && (
                <span className={`ml-1 ${outRows - inRows >= 0 ? 'text-emerald-600' : 'text-red-600'}`}>
                  ({outRows - inRows >= 0 ? '+' : ''}{outRows - inRows})
                </span>
              )}
            </span>
          )}
        </div>

        {/* Per-step error (2026-06-15): the sub-step that broke is shown here
            with its message; later steps don't run. */}
        {selectedPreview?.status === 'error' && (
          <div className="mt-1.5 px-2 py-1.5 rounded bg-red-50 border border-red-200 text-xs text-red-700">
            <span className="font-semibold">This step failed:</span> {selectedPreview.error || 'error'}
            <div className="text-[10px] text-red-500 mt-0.5">Fix this step — the steps after it didn't run.</div>
          </div>
        )}

        {/* Schema delta line */}
        {selectedPreview && hasSchemaChange && (
          <div className="mt-1.5 flex flex-wrap gap-x-2 gap-y-0.5 text-xs">
            {delta!.added.map((c) => (
              <span key={`+${c.name}`} className="text-emerald-700">
                + {c.name} <span className="text-slate-400">({c.type})</span>
              </span>
            ))}
            {delta!.removed.map((n) => (
              <span key={`-${n}`} className="text-red-700">− {n}</span>
            ))}
            {delta!.retyped.map((r) => (
              <span key={`~${r.name}`} className="text-amber-700">
                ~ {r.name} <span className="text-slate-400">{r.from} → {r.to}</span>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Body — column profile chips only. Full data table lives in the
          bottom OUTPUT panel (auto-syncs to the selected wrangler step). */}
      <div className="flex-1 min-h-0 overflow-auto">
        {error ? (
          <div className="p-4 m-3 text-sm text-red-700 bg-red-50 border border-red-200 rounded-md">
            Preview failed: {error}
          </div>
        ) : !previewExists && !loading ? (
          <div className="p-4 text-sm text-slate-500">
            Preview will appear shortly after you add a step.
          </div>
        ) : !selectedPreview ? (
          <div className="p-4 text-sm text-slate-500">
            Select a step on the left to see its output profile.
          </div>
        ) : (
          <StepColumnProfile step={selectedPreview} />
        )}
      </div>
    </div>
  );
}

// Column profile strip — one row per column with type + null% + distinct
// in the sample. No data rows here; the bottom OUTPUT panel renders the
// actual sample data (and the schema tab on that panel renders the same
// types). This pane is for step-by-step schema inspection only.
function StepColumnProfile({ step }: { step: PreviewStep }) {
  const rows = step.sample_data || [];
  if (step.columns.length === 0) {
    return <div className="p-4 text-sm text-slate-500">No columns at this step.</div>;
  }
  return (
    <div className="p-3 space-y-2">
      <div className="text-[10px] uppercase tracking-wider font-bold text-slate-500 flex items-center justify-between">
        <span>{step.columns.length} columns at this step</span>
        <span className="text-slate-400 normal-case font-normal">
          Sample data → OUTPUT panel below
        </span>
      </div>
      <div className="border border-slate-200 rounded-md overflow-hidden">
        <table className="text-xs w-full">
          <thead className="bg-slate-50 border-b border-slate-200">
            <tr className="text-left text-slate-500">
              <th className="px-2 py-1.5 font-semibold">Column</th>
              <th className="px-2 py-1.5 font-semibold">Type</th>
              <th className="px-2 py-1.5 font-semibold w-16">Nulls</th>
              <th className="px-2 py-1.5 font-semibold w-20">Distinct</th>
            </tr>
          </thead>
          <tbody>
            {step.columns.map((c, i) => {
              const prof = colProfile(rows, c.name);
              return (
                <tr
                  key={c.name}
                  className={i % 2 === 0 ? 'bg-white' : 'bg-slate-50/60'}
                >
                  <td className="px-2 py-1 font-mono text-slate-800">{c.name}</td>
                  <td className="px-2 py-1 font-mono text-[10px] uppercase text-slate-500">
                    {c.type}
                  </td>
                  <td className="px-2 py-1">
                    <span
                      className={`px-1 rounded text-[10px] font-semibold ${
                        prof.nullPct === 0
                          ? 'bg-emerald-50 text-emerald-700'
                          : prof.nullPct < 20
                            ? 'bg-amber-50 text-amber-700'
                            : 'bg-red-50 text-red-700'
                      }`}
                      title={`${prof.nullPct}% null in sample of ${prof.sampleSize}`}
                    >
                      {prof.nullPct}%
                    </span>
                  </td>
                  <td className="px-2 py-1 text-slate-600">{prof.distinct}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
