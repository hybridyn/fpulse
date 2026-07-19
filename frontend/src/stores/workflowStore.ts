import { create } from 'zustand';
import {
  type Node,
  type Edge,
  type OnNodesChange,
  type OnEdgesChange,
  type OnConnect,
  applyNodeChanges,
  applyEdgeChanges,
  addEdge,
  MarkerType,
} from '@xyflow/react';
import { api } from '../api/client';
import { toast } from '../components/Toast';
import { uiPrompt } from '../ui/dialog';
import { parsePipelineIntent, parseModification, classifyIntent } from '../ai/pipelineBuilder';
import { validateWorkflow, type ValidationIssue } from '../utils/validateWorkflow';
import { resolveSourceHandle } from '../utils/branchPorts';
import { navigateTo } from '../router';
import { buildConversationPayload } from '../util/conversationSummary';

// 2026-05-19 (OSS-8 of PAGE_BY_PAGE_AUDIT.md): name-prompt loop folded
// into the store so the standalone `utils/requireNamedWorkflow.ts` shim
// can be deleted. Single source of truth for the locked 2026-05-09 "no
// silent pipeline create" rule. Returns the cleaned name, or null if the
// user cancelled. Side-effect: writes the cleaned name to the store on
// success so the immediately-following `api.createWorkflow(name, ...)`
// call uses the right value.
const PLACEHOLDER_NAME = 'untitled pipeline';
async function promptForWorkflowName(
  getState: () => { workflowName: string; setWorkflowName: (n: string) => void },
): Promise<string | null> {
  // Pull existing names once so the duplicate check is client-side and
  // immediate. Failure is non-fatal — backend will still reject if
  // anything slips through.
  let existing: string[] = [];
  try {
    const wfs = await api.listWorkflows();
    existing = (wfs || [])
      .map((w: any) => String(w?.name || '').trim().toLowerCase())
      .filter(Boolean);
  } catch {
    /* network glitch — fall through with empty set */
  }
  let attempt = 0;
  let cleaned = '';
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const message = attempt === 0
      ? "Give this pipeline a name before we save it so it's easy to find later."
      : `"${cleaned}" is already used by another pipeline. Pick a different name.`;
    const proposed = await uiPrompt({
      title: 'Name your pipeline',
      message,
      placeholder: 'Customer sync',
      defaultValue: '',
      confirmLabel: 'Save',
    });
    if (!proposed) return null;
    cleaned = proposed.trim();
    if (!cleaned) return null;
    const lower = cleaned.toLowerCase();
    if (lower === PLACEHOLDER_NAME) { attempt += 1; continue; }
    if (existing.includes(lower)) { attempt += 1; continue; }
    break;
  }
  getState().setWorkflowName(cleaned);
  // Flush microtask so the immediate createWorkflow read sees the new value.
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
  return cleaned;
}

// 2026-05-19 (P2 #13 of PAGE_BY_PAGE_AUDIT.md): undo/redo snapshot
// cloning. `structuredClone` is ~2× faster than JSON round-trips on a
// 50-node canvas and preserves Map / Set / Date / typed arrays — JSON
// silently dropped those (today no callsite stores them, but the IR is
// growing). Falls back to JSON for environments where structuredClone
// isn't available (older jsdom test runners). The function is exported
// from this module's top scope so both `pushUndoState` and the undo /
// redo handlers can share one implementation.
function cloneSnap<T>(value: T): T {
  if (typeof structuredClone === 'function') {
    try {
      return structuredClone(value);
    } catch {
      /* fall through to JSON for non-serialisable edge cases */
    }
  }
  return JSON.parse(JSON.stringify(value));
}

/**
 * Walks the DAG forward from each seed node, returning the union of the
 * seeds + all reachable descendants. Used by the schema-cache scoped
 * invalidation in onNodesChange / onEdgesChange so structural changes
 * only flush the cache entries that actually changed semantically.
 *
 * `edges` is the pre-change edge list — callers must compute the scope
 * BEFORE applying the change, since e.g. a removed edge's descendant
 * tree is only walkable from the old edges. Cycles are safe (visited
 * set). Returns an empty Set if no seeds.
 */
function invalidationScope(seedIds: string[], edges: Edge[]): Set<string> {
  const out = new Set<string>();
  if (seedIds.length === 0) return out;
  const stack = [...seedIds];
  while (stack.length > 0) {
    const cur = stack.pop()!;
    if (out.has(cur)) continue;
    out.add(cur);
    for (const e of edges) {
      if (e.source === cur && !out.has(e.target)) {
        stack.push(e.target);
      }
    }
  }
  return out;
}

/** Returns a copy of `schemas` with the given step IDs removed. */
function pruneSchemas<T>(
  schemas: Record<string, T>,
  drop: Set<string>,
): Record<string, T> {
  if (drop.size === 0) return schemas;
  const out: Record<string, T> = {};
  for (const [k, v] of Object.entries(schemas)) {
    if (!drop.has(k)) out[k] = v;
  }
  return out;
}

export interface StepResult {
  step_id: string;
  status: string;
  row_count: number;
  columns: string[];
  sample_data: Record<string, any>[];
  schema_info: Array<{ name: string; type: string; nullable: boolean }>;
  error: string | null;
  duration_ms: number;
}

/** One sub-step's preview output inside a Data Wrangler node. */
export interface WranglerPreviewStep {
  index: number;
  op?: string;
  label: string;
  row_count: number;
  columns: Array<{ name: string; type: string }>;
  schema_delta: {
    added: Array<{ name: string; type: string }>;
    removed: string[];
    retyped: Array<{ name: string; from: string; to: string }>;
  };
  sample_data?: Array<Record<string, unknown>>;
}

/** Per-wrangler-node preview cache. selectedStepIndex defaults to the last step. */
export interface WranglerPreviewEntry {
  steps: WranglerPreviewStep[];
  selectedStepIndex: number;
  sample_rows: number;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  // Optional trace of which tools the agent invoked to produce this
  // reply. Rendered as a small footer beneath the assistant message
  // ("Inspecting pipeline · Validating · 2 steps") so the user can
  // see WHY the answer is grounded in real state vs. just guessed.
  // Only present on assistant turns that came from /api/ai/agent.
  tool_steps?: Array<{ tool_name: string; outcome: string; latency_ms?: number }>;
  // Phase 3.1 (May 18 2026) — when intent === 'clarify_first', the
  // backend returns a structured question set. The chat panel renders
  // <ClarifyCard /> instead of the markdown body so the user can
  // pick chips one-click. Submitted answers post back as a normal
  // chat message and the backend bridges via clarify_to_template.
  clarification?: {
    source_type: string | null;
    sink_type: string | null;
    detected_intent: string;
    question_count: number;
    questions: Array<{
      field: string;
      question: string;
      chips: string[];
      required: boolean;
    }>;
  };
}

/**
 * Pipeline parameter declaration. Mirrors backend `WorkflowParameter`:
 * a typed input variable that step params can reference via
 * `${param.<name>}`, overridable per-run via the API or scheduler.
 */
export interface WorkflowParameter {
  name: string;
  type: 'string' | 'int' | 'float' | 'bool' | 'json';
  default: string | number | boolean | null;
  description: string;
  required: boolean;
}

interface WorkflowState {
  // Canvas
  nodes: Node[];
  edges: Edge[];
  onNodesChange: OnNodesChange;
  onEdgesChange: OnEdgesChange;
  onConnect: OnConnect;
  /** Set/clear the per-edge table alias (SQL Transform table name) for the
   *  edge between two steps. Empty alias clears it. */
  updateEdgeAlias: (fromStepId: string, toStepId: string, alias: string) => void;

  // Workflow
  workflowId: string | null;
  workflowName: string;
  // Product surface currently using the editor canvas.
  // - pipeline: normal workflow authoring.
  // - file_data_prep: one-time/manual file preparation that loads an
  //   uploaded file into a managed table.
  // - pipeline_data_prep: source-dataset preparation that remains part
  //   of a pipeline so the cleaning steps run on every execution.
  editorSurface: 'pipeline' | 'file_data_prep' | 'pipeline_data_prep';
  setEditorSurface: (surface: WorkflowState['editorSurface']) => void;
  // Project this workflow lives in. null means "use the default
  // project" — only matters at create time (the toolbar's project
  // picker writes here, ensureWorkflow forwards it to the backend).
  projectId: string | null;
  setProjectId: (id: string | null) => void;
  // Folder under the chosen project. Picked via the EditorContextBar's
  // expandable project list. Persisted after createWorkflow by calling
  // moveWorkflowsToFolder so the new row lands in the right folder.
  folderId: string | null;
  setFolderId: (id: string | null) => void;
  version: number;
  // Lifecycle status — drives which actions are enabled in the
  // toolbar. 'draft' is the initial state; 'published' unlocks
  // scheduling (Free) and Deploy/PROD promotion (Plus). Backend
  // values: draft | testing | published | failed | archived.
  status: string;
  setStatus: (status: string) => void;
  // Pipeline-level parameters. Empty = pipeline takes no inputs.
  parameters: WorkflowParameter[];
  setParameters: (params: WorkflowParameter[]) => void;

  // Selection & Preview
  selectedNodeId: string | null;
  stepResults: Record<string, StepResult>;
  // N1 — Pinned data per node. When a node is pinned, its result here
  // overrides anything in stepResults so the user keeps the working
  // sample they tested with even after running again or tweaking
  // downstream. Backend integration (sending pinned overrides on the
  // next run so it doesn't re-execute the pinned node) is a follow-up;
  // round 1 just preserves the visible preview across upstream edits.
  pinnedResults: Record<string, StepResult>;
  pinNode: (nodeId: string) => void;
  unpinNode: (nodeId: string) => void;
  isPinned: (nodeId: string) => boolean;
  isRunning: boolean;

  // Schema Propagation Loop (PR 1) — cache of the column schema flowing
  // INTO each step, keyed by step id. Filled by `useUpstreamSchema`
  // (or by direct callers) and invalidated whenever the workflow IR
  // changes via `bumpSchemaVersion`. ConfigPanel + Data Wrangler dropdowns
  // read from this cache so column-name pickers always reflect the live
  // post-transformation column list.
  stepSchemas: Record<string, import('../api/client').StepSchemaResponse>;
  setStepSchema: (stepId: string, schema: import('../api/client').StepSchemaResponse) => void;
  clearStepSchemas: () => void;
  // Monotonic counter — useUpstreamSchema watches this to decide when to
  // refetch. Bumped by any state-setter that materially changes the IR.
  schemaVersion: number;
  bumpSchemaVersion: () => void;

  // Data Wrangler step-preview cache. Populated by DataWranglerConfig as
  // the user edits sub-steps; consumed by the bottom PreviewPanel so it
  // can render any wrangler sub-step's output (rather than only the
  // node's final output) via a Step ▾ selector. Keyed by wrangler node
  // id. selectedStepIndex tracks which sub-step the user is inspecting.
  wranglerPreview: Record<string, WranglerPreviewEntry>;
  setWranglerPreview: (nodeId: string, entry: WranglerPreviewEntry | null) => void;
  setWranglerSelectedStep: (nodeId: string, index: number) => void;

  // Chat
  chatMessages: ChatMessage[];
  chatOpen: boolean;

  // Nodes palette panel open/closed. Stored in the workflow store
  // (instead of as ModulesPanel local state) so other surfaces — most
  // notably ConfigPanel, which wants to collapse the rail while a node
  // is being edited — can read/write it.
  nodesPanelOpen: boolean;
  setNodesPanelOpen: (open: boolean) => void;

  // Validation
  validationErrors: Record<string, string[]>;  // nodeId -> error messages
  setValidationErrors: (errors: Record<string, string[]>) => void;
  clearValidationErrors: () => void;

  // Validation panel (2026-06-10) — lifted out of Canvas-local state so
  // every validation surface (Toolbar Run/Publish, Canvas Sample) opens
  // the SAME issue list. Before this, Run showed a count-only toast,
  // Publish painted node badges, and only Sample opened the panel —
  // three different UIs for one validateWorkflow() result.
  validationPanelIssues: ValidationIssue[] | null;
  openValidationPanel: (issues: ValidationIssue[]) => void;
  closeValidationPanel: () => void;

  // Dirty-state tracking — any unsaved change flips this true.
  // Consumed by the Close button in Toolbar to decide whether to
  // prompt Save/Discard before navigating away.
  isDirty: boolean;
  setDirty: (dirty: boolean) => void;
  // Reset the per-pipeline state so re-entering the editor (after Close
  // or after navigating away) starts from a blank canvas instead of
  // showing the last-opened pipeline's data. Keeps cross-pipeline UI
  // preferences like nodesPanelOpen intact.
  resetWorkflow: () => void;

  // Derived: is this node disabled, or downstream of a disabled node?
  // Used by FPulseNode + CustomEdge to grey-out the blocked chain.
  isNodeDeactivated: (nodeId: string) => boolean;
  isNodeBlockedByUpstream: (nodeId: string) => boolean;

  // Code Editor
  codeEditorOpen: boolean;

  // Undo/Redo
  undoStack: Array<{ nodes: Node[]; edges: Edge[] }>;
  redoStack: Array<{ nodes: Node[]; edges: Edge[] }>;
  pushUndoState: () => void;
  undo: () => void;
  redo: () => void;
  canUndo: () => boolean;
  canRedo: () => boolean;

  // Actions
  setSelectedNode: (id: string | null) => void;
  setChatOpen: (open: boolean) => void;
  setCodeEditorOpen: (open: boolean) => void;
  setWorkflowName: (name: string) => void;
  loadWorkflow: (data: any) => void;
  loadExecutionIntoEditor: (execution: any) => void;
  runWorkflow: (fullRun?: boolean, parameterValues?: Record<string, unknown>) => Promise<void>;
  runStep: (stepId: string, opts?: { ephemeral?: boolean }) => Promise<void>;
  resumeFromStep: (stepId: string) => Promise<void>;
  sendChat: (message: string, opts?: { deep?: boolean }) => Promise<void>;
  useTemplate: (key: string) => Promise<void>;
  createFromIntent: (intent: string) => Promise<void>;
  updateNodeParams: (nodeId: string, params: Record<string, any>) => void;
  updateNodeLabel: (nodeId: string, label: string) => void;
  addNode: (type: string, position?: { x: number; y: number }) => void;
  deleteNode: (nodeId: string) => void;
  /**
   * Persist the canvas to the backend.
   *
   * Default behavior: **update only**. If no workflowId exists yet (fresh
   * canvas, user hasn't clicked Save), this is a no-op that returns null.
   * This enforces the "no silent pipeline create" rule (memory feedback
   * 2026-05-09): autosave / Test Node / Run must never create an
   * "Untitled Pipeline" row behind the user's back.
   *
   * Pass `{ allowCreate: true }` ONLY from the explicit Save button paths
   * (Toolbar Save, Save dialog, Save+Close modal) after the caller has
   * already validated the workflow name is non-placeholder AND unique.
   */
  ensureWorkflow: (opts?: { allowCreate?: boolean }) => Promise<string | null>;
  getUpstreamNodeIds: (nodeId: string) => string[];
  setReactFlowInstance: (instance: any) => void;
  reactFlowInstance: any;
}

const NODE_COLORS: Record<string, string> = {
  // Generic data movement nodes
  source: '#3b82f6', destination: '#6366f1', copy_data: '#0ea5e9',
  // Legacy specific sources/sinks — kept for backward compat
  file_source: '#3b82f6', file_sink: '#6366f1',
  csv_source: '#3b82f6', json_source: '#d97706', parquet_source: '#10b981',
  excel_source: '#16a34a', xml_source: '#e11d48', db_source: '#8b5cf6',
  api_source: '#0ea5e9', s3_source: '#f97316', kafka_source: '#334155',
  ftp_source: '#6366f1', gsheet_source: '#22c55e', delta_source: '#0891b2',
  // Row-level transforms
  filter: '#f59e0b', transform: '#10b981', deduplicate: '#ec4899',
  sort: '#8b5cf6', rename: '#14b8a6', typecast: '#a855f7', derived_column: '#059669',
  // Set-level transforms
  aggregate: '#06b6d4', join: '#f97316', lookup: '#ea580c', union: '#d946ef',
  pivot: '#0891b2', unpivot: '#0e7490', window: '#7c3aed',
  sample: '#84cc16', conditional_split: '#eab308',
  // Flow Control nodes
  if_condition: '#eab308', switch_case: '#f59e0b', foreach_loop: '#8b5cf6',
  until_loop: '#7c3aed', wait_delay: '#64748b', set_variable: '#14b8a6',
  execute_pipeline: '#6366f1', retry_handler: '#ef4444', fail: '#ef4444',
  // Actions
  http_request: '#0ea5e9', webhook_trigger: '#a855f7', code_script: '#334155',
  send_email: '#db2777', slack_notify: '#e11d48',
  delete_data: '#ef4444', get_metadata: '#8b5cf6',
  // Legacy destinations
  output: '#6366f1', csv_sink: '#3b82f6', json_sink: '#d97706',
  excel_sink: '#16a34a', db_sink: '#4f46e5', s3_sink: '#ea580c',
  kafka_sink: '#334155', api_sink: '#0284c7', webhook_sink: '#9333ea',
  email_sink: '#db2777', delta_sink: '#0e7490', warehouse_sink: '#7c3aed',
};

const NODE_ICONS: Record<string, string> = {
  // Generic data movement
  source: '📥', destination: '📤', copy_data: '📋',
  // Legacy sources
  file_source: '📂', file_sink: '📂',
  csv_source: '📄', json_source: '{ }', parquet_source: '▦',
  excel_source: '📊', xml_source: '</>',  db_source: '🗄️',
  api_source: '🌐', s3_source: '☁️', kafka_source: '⚡',
  ftp_source: '📡', gsheet_source: '📋', delta_source: '△',
  // Row-level transforms
  filter: '🔍', transform: '⚡', deduplicate: '🧹',
  sort: '↕️', rename: '✏️', typecast: '🔄', derived_column: '➕',
  // Set-level transforms
  aggregate: '📊', join: '🔗', lookup: '🔎', union: '⊕',
  pivot: '↩️', unpivot: '↪️', window: '🪟',
  sample: '🎲', conditional_split: '🔀',
  // Flow Control
  if_condition: '❓', switch_case: '🔀', foreach_loop: '🔁',
  until_loop: '🔄', wait_delay: '⏳', set_variable: '📝',
  execute_pipeline: '▶️', retry_handler: '🔄', fail: '⛔',
  // Actions
  http_request: '🌐', webhook_trigger: '🔔', code_script: '💻',
  send_email: '✉️', slack_notify: '💬',
  delete_data: '🗑️', get_metadata: '📑',
  // Legacy destinations
  output: '💾', csv_sink: '📄', json_sink: '{ }',
  excel_sink: '📊', db_sink: '🗃️', s3_sink: '☁️',
  kafka_sink: '⚡', api_sink: '🌐', webhook_sink: '🔔',
  email_sink: '✉️', delta_sink: '△', warehouse_sink: '🏛️',
};

const NODE_CATEGORY: Record<string, string> = {
  // Generic data movement
  source: 'source', destination: 'output', copy_data: 'action',
  // Legacy sources
  file_source: 'source', file_sink: 'output',
  csv_source: 'source', json_source: 'source', parquet_source: 'source',
  excel_source: 'source', xml_source: 'source', db_source: 'source',
  api_source: 'source', s3_source: 'source', kafka_source: 'source',
  ftp_source: 'source', gsheet_source: 'source', delta_source: 'source',
  filter: 'transform', transform: 'transform', deduplicate: 'transform',
  sort: 'transform', rename: 'transform', typecast: 'transform', derived_column: 'transform',
  aggregate: 'combine', join: 'combine', lookup: 'combine', union: 'combine',
  pivot: 'combine', unpivot: 'combine', window: 'combine',
  sample: 'transform', conditional_split: 'flow',
  // Flow Control
  if_condition: 'flow', switch_case: 'flow', foreach_loop: 'flow',
  until_loop: 'flow', wait_delay: 'flow', set_variable: 'flow',
  execute_pipeline: 'flow', retry_handler: 'flow', fail: 'flow',
  // Actions
  http_request: 'action', webhook_trigger: 'action', code_script: 'action',
  send_email: 'action', slack_notify: 'action',
  delete_data: 'action', get_metadata: 'action',
  // Legacy destinations
  output: 'output', csv_sink: 'output', json_sink: 'output',
  excel_sink: 'output', db_sink: 'output', s3_sink: 'output',
  kafka_sink: 'output', api_sink: 'output', webhook_sink: 'output',
  email_sink: 'output', delta_sink: 'output', warehouse_sink: 'output',
};

// Frontend node presets (2026-06-11): a palette tile that drops an existing
// node pre-configured for a named use case. Keeps the canvas to one real
// backend node while giving users the name they search for.
const NODE_PRESETS: Record<string, { base: string; label: string; params: Record<string, any> }> = {
  // "Split Out" = Flatten/Explode defaulted to explode an array → rows.
  split_out: { base: 'flatten_explode', label: 'Split Out', params: { mode: 'explode' } },
  // The palette tile relabels foreach_loop → "Batch Rows" (the chunker), but the
  // backend registry display_name is still "ForEach Loop". Pin the label here so
  // a dropped node matches its tile (preset label wins over the backend meta).
  foreach_loop: { base: 'foreach_loop', label: 'Batch Rows', params: {} },
};

function workflowToNodesEdges(workflow: any): { nodes: Node[]; edges: Edge[] } {
  // Seed nodes with dimensions matching `.fpulse-node` CSS (width: 150px,
  // typical rendered height ~46px). xyflow v12 won't render edges until
  // every endpoint node has known dimensions; without these fields it
  // waits on a ResizeObserver round-trip, and on first paint after
  // loadWorkflow the gate stays closed in the case where measurement
  // dispatches don't reach the store before the user sees the canvas.
  // The real measurement still flows through onNodesChange and
  // overrides these once it arrives.
  const nodes: Node[] = (workflow.steps || []).map((step: any) => ({
    id: step.id,
    type: 'fpulseNode',
    position: { x: step.position?.x ?? 300, y: step.position?.y ?? 0 },
    width: 150,
    height: 46,
    measured: { width: 150, height: 46 },
    data: {
      label: step.label || step.type,
      stepType: step.type,
      params: step.params || {},
      color: NODE_COLORS[step.type] || '#94a3b8',
      icon: NODE_ICONS[step.type] || '⬜',
      category: NODE_CATEGORY[step.type] || 'transform',
      risk: step.risk || 'low',
    },
  }));

  const CONDITION_COLORS: Record<string, string> = { completion: '#6366f1', success: '#22c55e', failure: '#ef4444' };

  // 2026-06-11 (multi-output): per-source-step type+params so a saved branch
  // edge reattaches to the right output handle (and legacy `output` edges on
  // a branch node remap onto its first branch instead of dangling).
  const stepById: Record<string, any> = {};
  for (const s of (workflow.steps || [])) stepById[s.id] = s;

  const edges: Edge[] = (workflow.connections || []).map((conn: any, i: number) => {
    const condition = conn.condition || 'completion';
    const color = CONDITION_COLORS[condition] || '#6366f1';
    const srcStep = stepById[conn.from_step];
    const sourceHandle = resolveSourceHandle(srcStep?.type, conn.from_port, srcStep?.params);
    const targetHandle = conn.to_port && conn.to_port !== 'input' ? conn.to_port : undefined;
    return {
      id: `e-${conn.from_step}-${conn.to_step}${sourceHandle ? `-${sourceHandle}` : ''}`,
      source: conn.from_step,
      target: conn.to_step,
      ...(sourceHandle ? { sourceHandle } : {}),
      ...(targetHandle ? { targetHandle } : {}),
      type: 'custom',
      animated: true,
      // `alias` — optional per-edge table name a multi-input node (SQL Transform)
      // registers its incoming relation under. Round-trips via StepConnection.alias.
      data: { condition, ...(conn.alias ? { alias: conn.alias } : {}) },
      style: { stroke: color, strokeWidth: 2 },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        width: 16,
        height: 16,
        color,
      },
    };
  });

  return { nodes, edges };
}

// Exported so non-store callers (e.g. DataWranglerConfig's ephemeral
// preview) can build the inline workflow IR without duplicating this
// logic. Pure function — no store reads inside.
export function nodesToWorkflow(nodes: Node[], edges: Edge[], name: string, id: string, parameters?: WorkflowParameter[]): any {
  return {
    id,
    name,
    steps: nodes.map((n) => ({
      id: n.id,
      type: n.data.stepType,
      label: n.data.label,
      params: n.data.params || {},
      position: { x: n.position.x, y: n.position.y },
      risk: n.data.risk || 'low',
    })),
    connections: edges.map((e) => ({
      from_step: e.source,
      to_step: e.target,
      // 2026-06-11 (multi-output): persist which branch handle the edge
      // leaves from. Without this the executor always saw from_port='output'
      // and branch routing never fired. Defaults keep single-output edges
      // identical to before.
      from_port: e.sourceHandle || 'output',
      to_port: e.targetHandle || 'input',
      condition: (e.data as any)?.condition || 'completion',
      // Per-edge user alias (table name for SQL Transform). Omitted when unset.
      ...((e.data as any)?.alias ? { alias: (e.data as any).alias } : {}),
    })),
    parameters: parameters || [],
  };
}

export const useWorkflowStore = create<WorkflowState>((set, get) => ({
  nodes: [],
  edges: [],
  workflowId: null,
  workflowName: 'Untitled Pipeline',
  editorSurface: 'pipeline',
  setEditorSurface: (surface) => set({ editorSurface: surface }),
  projectId: null,
  setProjectId: (id) => set({ projectId: id }),
  folderId: null,
  setFolderId: (id) => set({ folderId: id }),
  version: 0,
  status: 'draft',
  setStatus: (status) => set({ status }),
  parameters: [],
  setParameters: (params) => set({ parameters: params, isDirty: true }),
  selectedNodeId: null,
  stepResults: {},
  // N1 — pin map initialised empty. Pins don't survive reload in
  // round 1; localStorage persistence is a follow-up.
  pinnedResults: {},
  pinNode: (nodeId) => set((s) => {
    const current = s.stepResults[nodeId];
    if (!current) return {};  // Can't pin a node with no result yet.
    return { pinnedResults: { ...s.pinnedResults, [nodeId]: current } };
  }),
  unpinNode: (nodeId) => set((s) => {
    const next = { ...s.pinnedResults };
    delete next[nodeId];
    return { pinnedResults: next };
  }),
  isPinned: (nodeId) => Boolean(get().pinnedResults?.[nodeId]),
  isRunning: false,
  stepSchemas: {},
  setStepSchema: (stepId, schema) =>
    set((s) => ({ stepSchemas: { ...s.stepSchemas, [stepId]: schema } })),
  clearStepSchemas: () => set({ stepSchemas: {} }),
  schemaVersion: 0,
  bumpSchemaVersion: () => set((s) => ({ schemaVersion: s.schemaVersion + 1, stepSchemas: {} })),
  wranglerPreview: {},
  setWranglerPreview: (nodeId, entry) =>
    set((s) => {
      const next = { ...s.wranglerPreview };
      if (entry === null) delete next[nodeId];
      else next[nodeId] = entry;
      return { wranglerPreview: next };
    }),
  setWranglerSelectedStep: (nodeId, index) =>
    set((s) => {
      const cur = s.wranglerPreview[nodeId];
      if (!cur) return s;
      return {
        wranglerPreview: {
          ...s.wranglerPreview,
          [nodeId]: { ...cur, selectedStepIndex: index },
        },
      };
    }),
  // Empty by default — ChatPanel renders a structured empty-state with
  // 3 clickable prompts when chatMessages is empty. A seeded welcome
  // message would suppress that empty-state and dump the user back to a
  // single passive paragraph (the old behaviour).
  chatMessages: [],
  chatOpen: true,
  nodesPanelOpen: true,
  validationErrors: {},
  setValidationErrors: (errors) => set({ validationErrors: errors }),
  clearValidationErrors: () => set({ validationErrors: {} }),
  validationPanelIssues: null,
  openValidationPanel: (issues) => set({ validationPanelIssues: issues }),
  closeValidationPanel: () => set({ validationPanelIssues: null }),
  codeEditorOpen: false,

  isDirty: false,
  setDirty: (dirty) => set({ isDirty: dirty }),
  resetWorkflow: () =>
    set({
      nodes: [],
      edges: [],
      workflowId: null,
      workflowName: 'Untitled Pipeline',
      editorSurface: 'pipeline',
      projectId: null,
      folderId: null,
      version: 0,
      status: 'draft',
      parameters: [],
      selectedNodeId: null,
      stepResults: {},
      stepSchemas: {},
      wranglerPreview: {},
      isDirty: false,
    }),

  // Flat disabled check — reads the same flag the executor uses
  isNodeDeactivated: (nodeId) => {
    const n = get().nodes.find((x) => x.id === nodeId);
    return !!(n?.data as any)?.params?._settings?.deactivated;
  },

  // Walks upstream edges. A node is "blocked" if any transitive
  // ancestor is deactivated — the executor will skip it at run
  // time, so the canvas should show it as a shadow too.
  isNodeBlockedByUpstream: (nodeId) => {
    const { nodes, edges } = get();
    const nodeMap = new Map(nodes.map((n) => [n.id, n]));
    const incoming = new Map<string, string[]>();
    for (const e of edges) {
      if (!incoming.has(e.target)) incoming.set(e.target, []);
      incoming.get(e.target)!.push(e.source);
    }
    const seen = new Set<string>();
    const stack = [...(incoming.get(nodeId) || [])];
    while (stack.length) {
      const id = stack.pop()!;
      if (seen.has(id)) continue;
      seen.add(id);
      const n = nodeMap.get(id);
      if ((n?.data as any)?.params?._settings?.deactivated) return true;
      stack.push(...(incoming.get(id) || []));
    }
    return false;
  },

  // Undo/Redo stacks (max 30 states).
  // 2026-05-19 (P2 #13 of PAGE_BY_PAGE_AUDIT.md): snapshot cloning moved
  // from `JSON.parse(JSON.stringify(...))` to `structuredClone()` — modern
  // browsers ship a native implementation that's ~2× faster on a 50-node
  // canvas and isn't constrained by JSON-serialisability (so a future
  // node param holding `Date` / `Map` / `Set` survives the round-trip
  // instead of being silently lost). The fallback `cloneSnap` keeps the
  // JSON path for any environment where `structuredClone` is missing
  // (older test runners, jsdom <22).
  undoStack: [],
  redoStack: [],
  pushUndoState: () => {
    const { nodes, edges, undoStack } = get();
    const snapshot = { nodes: cloneSnap(nodes), edges: cloneSnap(edges) };
    // Any mutation that snapshots for undo is, by definition, a change
    // the user will want to save. Mark the workflow dirty here so every
    // write path (addNode / deleteNode / onConnect / drag-drop) flows
    // through a single source of truth — no chance of forgetting one.
    set({ undoStack: [...undoStack.slice(-29), snapshot], redoStack: [], isDirty: true });
  },
  undo: () => {
    const { undoStack, nodes, edges } = get();
    if (undoStack.length === 0) return;
    const prev = undoStack[undoStack.length - 1];
    set((s) => ({
      undoStack: s.undoStack.slice(0, -1),
      redoStack: [...s.redoStack, { nodes: cloneSnap(nodes), edges: cloneSnap(edges) }],
      nodes: prev.nodes,
      edges: prev.edges,
    }));
  },
  redo: () => {
    const { redoStack, nodes, edges } = get();
    if (redoStack.length === 0) return;
    const next = redoStack[redoStack.length - 1];
    set((s) => ({
      redoStack: s.redoStack.slice(0, -1),
      undoStack: [...s.undoStack, { nodes: cloneSnap(nodes), edges: cloneSnap(edges) }],
      nodes: next.nodes,
      edges: next.edges,
    }));
  },
  canUndo: () => get().undoStack.length > 0,
  canRedo: () => get().redoStack.length > 0,

  onNodesChange: (changes) =>
    set((s) => {
      // Node drag end / resize / position changes all flow through here.
      // A "select" or "dimensions" change is NOT a user edit worth
      // prompting about, but a position change IS — they moved a node
      // and would expect the save prompt on close. We only flip dirty
      // for the edits that actually change persisted state.
      const persistedChange = changes.some(
        (c: any) => c.type === 'position' || c.type === 'remove' || c.type === 'add',
      );
      // Structural changes (add/remove) invalidate the schema cache —
      // but ONLY for the affected node and its descendants. Pre-2026-05-18
      // we wiped the whole cache on every structural change, forcing a
      // re-fetch per dropdown for every node on the canvas. Dependency-
      // scoped invalidation keeps the cache hot for untouched nodes.
      const removedIds: string[] = changes
        .filter((c: any) => c.type === 'remove' && typeof c.id === 'string')
        .map((c: any) => c.id);
      const structuralChange = changes.some(
        (c: any) => c.type === 'remove' || c.type === 'add',
      );
      const scopedSchemas = structuralChange
        ? pruneSchemas(s.stepSchemas, invalidationScope(removedIds, s.edges))
        : s.stepSchemas;
      return {
        nodes: applyNodeChanges(changes, s.nodes),
        ...(persistedChange ? { isDirty: true } : {}),
        ...(structuralChange
          ? { stepSchemas: scopedSchemas, schemaVersion: s.schemaVersion + 1 }
          : {}),
      };
    }),

  onEdgesChange: (changes) =>
    set((s) => {
      const persistedChange = changes.some(
        (c: any) => c.type === 'remove' || c.type === 'add',
      );
      // Edge add/remove changes input_map for the target + its
      // descendants only — not the whole graph.
      const removedEdgeTargets: string[] = changes
        .filter((c: any) => c.type === 'remove' && typeof c.id === 'string')
        .map((c: any) => s.edges.find((e) => e.id === c.id)?.target)
        .filter((t: any): t is string => !!t);
      const addedEdgeTargets: string[] = changes
        .filter((c: any) => c.type === 'add' && c.item?.target)
        .map((c: any) => c.item.target as string);
      const scopedSchemas = persistedChange
        ? pruneSchemas(
            s.stepSchemas,
            invalidationScope(
              [...removedEdgeTargets, ...addedEdgeTargets],
              s.edges,
            ),
          )
        : s.stepSchemas;
      return {
        edges: applyEdgeChanges(changes, s.edges),
        ...(persistedChange
          ? {
              isDirty: true,
              stepSchemas: scopedSchemas,
              schemaVersion: s.schemaVersion + 1,
            }
          : {}),
      };
    }),

  onConnect: (connection) => {
    get().pushUndoState();
    // Check if the source node is a flow-control type (conditional branches)
    const sourceNode = get().nodes.find((n) => n.id === connection.source);
    const isFlowNode = sourceNode && ['if_condition', 'switch_case', 'conditional_split', 'foreach_loop', 'until_loop'].includes(String((sourceNode.data as any)?.stepType ?? ''));
    set((s) => ({
      edges: addEdge(
        {
          ...connection,
          type: 'custom',
          animated: true,
          data: { condition: isFlowNode ? 'success' : 'completion' },
          style: { stroke: '#6366f1', strokeWidth: 2 },
          markerEnd: {
            type: MarkerType.ArrowClosed,
            width: 16,
            height: 16,
            color: '#6366f1',
          },
        },
        s.edges,
      ),
    }));
  },

  updateEdgeAlias: (fromStepId, toStepId, alias) => {
    get().pushUndoState();
    const clean = (alias || '').trim();
    set((s) => ({
      isDirty: true,
      // Bump so useUpstreamSchema refetches — the registered table name changed.
      schemaVersion: s.schemaVersion + 1,
      edges: s.edges.map((e) =>
        e.source === fromStepId && e.target === toStepId
          ? { ...e, data: { ...((e.data as any) || {}), alias: clean || undefined } }
          : e,
      ),
    }));
  },

  setSelectedNode: (id) => set({ selectedNodeId: id }),
  setChatOpen: (open) => set({ chatOpen: open }),
  setNodesPanelOpen: (open) => set({ nodesPanelOpen: open }),
  setCodeEditorOpen: (open) => set({ codeEditorOpen: open }),
  setWorkflowName: (name) => set({ workflowName: name, isDirty: true }),

  loadWorkflow: (data) => {
    const wf = data.workflow || data;
    const { nodes, edges } = workflowToNodesEdges(wf);
    const scaffoldedFrom = wf?.metadata?.scaffolded_from;
    const editorSurface =
      scaffoldedFrom === 'storage_file'
        ? 'file_data_prep'
        : scaffoldedFrom === 'connection_stream'
          ? 'pipeline_data_prep'
          : 'pipeline';
    set({
      nodes,
      edges,
      workflowId: wf.id,
      workflowName: wf.name,
      editorSurface,
      version: data.version || 1,
      // Lifecycle status — defaults to 'draft' if the backend didn't
      // send one (older saves, fresh creations). Toolbar buttons
      // (Plus Deploy, schedule UI) gate on this value.
      status: typeof wf.status === 'string' && wf.status ? wf.status : 'draft',
      // Pipeline parameters — empty array if the saved workflow
      // didn't have any (older versions). Defaults are coerced server-side.
      parameters: Array.isArray(wf.parameters) ? wf.parameters : [],
      stepResults: {},
      wranglerPreview: {},
      // A freshly loaded workflow matches the server — nothing to save.
      // Also reset undo/redo history so the user doesn't accidentally
      // undo INTO the previous pipeline's state.
      isDirty: false,
      undoStack: [],
      redoStack: [],
    });
  },

  loadExecutionIntoEditor: (execution) => {
    // Replay a past run in the editor:
    //  1. Hydrate the canvas from the workflow_snapshot stored at run
    //     time (falls back to the current workflow if the snapshot
    //     wasn't captured — pre-feature history).
    //  2. Rebuild stepResults from the step_logs so every node paints
    //     with the exact status it ended in (success / error / skipped).
    //  3. Navigate to the editor hash so the canvas becomes visible.
    const snapshot = execution?.workflow_snapshot;
    if (!snapshot) {
      toast.error(
        'No snapshot',
        'This execution predates snapshot storage — open the workflow directly.',
      );
      return;
    }
    const { nodes, edges } = workflowToNodesEdges(snapshot);
    const stepResults: Record<string, StepResult> = {};
    for (const log of (execution.step_logs || [])) {
      stepResults[log.step_id] = {
        step_id: log.step_id,
        status: log.status,
        row_count: log.rows_processed || 0,
        columns: [],
        sample_data: log.output_preview || [],
        schema_info: [],
        error: log.error_message || null,
        duration_ms: log.duration_ms || 0,
      };
    }
    // Mirror step statuses onto the node data so the canvas colors match
    // what actually happened (green/red/amber).
    const paintedNodes = nodes.map((n) => {
      const sr = stepResults[n.id];
      return sr
        ? { ...n, data: { ...n.data, status: sr.status } }
        : n;
    });
    set({
      nodes: paintedNodes,
      edges,
      workflowId: snapshot.id || execution.workflow_id,
      workflowName:
        (snapshot.name || execution.workflow_name || 'Pipeline')
        + ` — replay of ${execution.id}`,
      version: 0,
      stepResults,
    });
    toast.success(
      'Loaded past run',
      `Canvas shows the workflow as it ran on ${
        execution.started_at ? new Date(execution.started_at).toLocaleString() : 'that run'
      }`,
    );
    try { navigateTo('editor'); } catch { /* noop */ }
  },

  runWorkflow: async (fullRun = false, parameterValues?: Record<string, unknown>) => {
    // Run/Sample work against the live canvas — no save required (2026-05-22).
    // Save remains the only path that creates a row in the Pipelines list,
    // preserving the no-silent-create rule (2026-05-09). When the canvas
    // is saved AND clean we still prefer the stored-workflow endpoint (it's
    // cheaper — no IR round-trip). When the canvas is dirty OR unsaved we
    // use the ephemeral endpoint which accepts the in-memory IR.
    const { workflowId, isDirty, nodes, edges, workflowName, parameters } = get();

    if (!nodes || nodes.length === 0) {
      toast.error(
        'Canvas is empty',
        'Add at least one node before running.',
      );
      return;
    }

    set({ isRunning: true });

    // Mark all nodes as running
    set({
      nodes: nodes.map((n) => ({
        ...n,
        data: { ...n.data, status: 'running' },
      })),
    });

    try {
      // Build the IR from the current canvas — used for ephemeral runs and
      // also as the source of truth even when we route through the stored
      // endpoint (the stored version equals the canvas when !isDirty).
      const wf = nodesToWorkflow(nodes, edges, workflowName, workflowId || '', parameters);

      const useEphemeral = !workflowId || isDirty;
      let result: any;
      try {
        result = useEphemeral
          ? await api.runWorkflowEphemeral(wf, fullRun, 'dev', 'live', parameterValues)
          : await api.runWorkflow(workflowId, fullRun, 'dev', 'live', parameterValues);
      } catch (err: any) {
        // 2026-05-22 — fallback for the "saved but actually unsaved"
        // edge case. An imported pipeline can land in the store with
        // a workflowId set (from the file's `id`) but no matching row
        // in the local backend. The saved-path POST then 404s
        // "Workflow not found" even though the canvas itself is fine.
        // Retry the run via the ephemeral path so the user isn't
        // stuck staring at a 404.
        const msg = String(err?.message || err || '').toLowerCase();
        const looksLikeMissing =
          msg.includes('workflow not found') ||
          msg.includes('404') ||
          msg.includes('not found');
        if (!useEphemeral && looksLikeMissing) {
          // Mark dirty so subsequent runs use ephemeral directly
          // without the 404 round-trip.
          set({ isDirty: true });
          toast.warning(
            'Pipeline not saved yet — running a temporary copy',
            'This pipeline isn\'t saved yet, so it ran without a Pipelines list entry. Click Save to add it.',
          );
          result = await api.runWorkflowEphemeral(wf, fullRun, 'dev', 'live', parameterValues);
        } else {
          throw err;
        }
      }

      // Handle overlap detection responses
      if (result.overlap_detected) {
        set({ isRunning: false, nodes: get().nodes.map((n) => ({ ...n, data: { ...n.data, status: 'pending' } })) });
        if (result.status === 'skipped') {
          toast.warning('Execution Skipped', 'Previous execution is still running. Pipeline overlap policy is set to Skip.');
        } else if (result.status === 'queued') {
          toast.info('Execution Queued', 'Previous execution is still running. Your run has been queued.');
        }
        return;
      }

      const stepResults: Record<string, StepResult> = result.step_results || {};
      const errorCount = Object.values(stepResults).filter((r) => r.status === 'error').length;
      set({
        stepResults,
        isRunning: false,
        nodes: get().nodes.map((n) => ({
          ...n,
          data: {
            ...n.data,
            status: stepResults[n.id]?.status || 'pending',
          },
        })),
      });

      // Timeout exceeded alert
      if (result.timeout_exceeded) {
        toast.error(
          'Pipeline Timeout Exceeded',
          `Took ${result.actual_runtime_minutes} min (limit: ${result.max_runtime_minutes} min)`
        );
      } else if (errorCount > 0) {
        toast.warning('Pipeline completed with errors', `${errorCount} step(s) failed`);
      } else {
        toast.success('Pipeline executed successfully', `${Object.keys(stepResults).length} steps completed`);
      }
    } catch (err: any) {
      set({
        isRunning: false,
        nodes: get().nodes.map((n) => ({
          ...n,
          data: { ...n.data, status: 'error' },
        })),
      });
      toast.error('Pipeline execution failed', err.message);
    }
  },

  runStep: async (stepId, opts) => {
    const { workflowId, nodes, edges, workflowName } = get();

    try {
      // Z10 / Z12 (2026-05-23) — Two paths:
      //   1. Ephemeral (no workflowId OR opts.ephemeral): build the IR
      //      inline from the canvas state and run it via the no-
      //      persistence endpoint. Used by Test Node before the
      //      pipeline has been named/saved — honors the no-silent-
      //      create rule (2026-05-09).
      //   2. Persisted: PUT the canvas into the existing row, then POST
      //      the step run. Required for replay/lineage to see the
      //      exact IR used.
      //
      // Z12 fallback: if the store carries a stale workflowId pointing
      // to a row that was deleted (e.g. user deleted the pipeline from
      // the Pipelines list but never reloaded the editor), the
      // persisted path returns 404 "Workflow not found". Clear the id
      // and silently retry via the ephemeral path so Test still works.
      const useEphemeral = opts?.ephemeral || !workflowId;
      const wf = nodesToWorkflow(
        nodes,
        edges,
        workflowName,
        workflowId || 'ephemeral',
        get().parameters,
      );

      let result;
      if (useEphemeral) {
        result = await api.runStepEphemeral(wf, stepId);
      } else {
        try {
          await api.updateWorkflow(workflowId!, wf, 'Pre-step save');
          result = await api.runStep(workflowId!, stepId);
        } catch (persistedErr: any) {
          // 404 (workflow row gone) → fall back to ephemeral run so the
          // user's test still completes. Reset the stale id in the
          // store so subsequent Save shows the name prompt again.
          const msg = String(persistedErr?.message || persistedErr || '');
          const isMissing = /not found|404/i.test(msg);
          if (!isMissing) throw persistedErr;
          // eslint-disable-next-line no-console
          console.warn('[runStep] Persisted path 404 — falling back to ephemeral.', msg);
          set({ workflowId: null });
          result = await api.runStepEphemeral(wf, stepId);
        }
      }
      const returnedStepResults = result?.step_results && typeof result.step_results === 'object'
        ? result.step_results as Record<string, StepResult>
        : { [stepId]: result as StepResult };
      set((s) => ({
        stepResults: { ...s.stepResults, ...returnedStepResults },
        nodes: s.nodes.map((n) => {
          const stepResult = returnedStepResults[n.id];
          return stepResult
            ? { ...n, data: { ...n.data, status: stepResult.status } }
            : n;
        }),
      }));
    } catch (err: any) {
      set((s) => ({
        stepResults: {
          ...s.stepResults,
          [stepId]: { step_id: stepId, status: 'error', error: err.message, row_count: 0, columns: [], sample_data: [], schema_info: [], duration_ms: 0 },
        },
      }));
    }
  },

  resumeFromStep: async (stepId) => {
    const { workflowId, nodes, edges, workflowName } = get();
    if (!workflowId) return;

    try {
      const wf = nodesToWorkflow(nodes, edges, workflowName, workflowId, get().parameters);
      await api.updateWorkflow(workflowId, wf, 'Pre-resume save');

      const result = await api.resumeFromStep(workflowId, stepId);
      set((s) => ({
        stepResults: { ...s.stepResults, [stepId]: result },
        nodes: s.nodes.map((n) =>
          n.id === stepId
            ? { ...n, data: { ...n.data, status: result.status } }
            : n,
        ),
      }));
      if (result.status === 'success') {
        toast.success('Rerun complete', 'Used cached upstream where unchanged');
      } else if (result.status === 'error') {
        toast.error('Rerun failed', result.error || 'Unknown error');
      }
    } catch (err: any) {
      set((s) => ({
        stepResults: {
          ...s.stepResults,
          [stepId]: { step_id: stepId, status: 'error', error: err.message, row_count: 0, columns: [], sample_data: [], schema_info: [], duration_ms: 0 },
        },
      }));
      toast.error('Rerun failed', err.message);
    }
  },

  sendChat: async (message, opts) => {
    const msgs = [...get().chatMessages, { role: 'user' as const, content: message }];
    set({ chatMessages: msgs });

    const intent = classifyIntent(message);
    const deepRequested = !!opts?.deep;

    // CREATE / MODIFY intents go to the deterministic rule-planner FIRST.
    // User-reported 2026-05-17: "Build a pipeline that reads sales.csv,
    // filters rows where status='active', writes Parquet" was returning
    // a list of recent executions instead of a draft. Root cause: the
    // agent loop's tool-selection on small models (qwen2.5:1.5b) picks
    // list_executions when the prompt contains both 'pipeline' and
    // 'status' tokens. The rule-planner has no such ambiguity — it
    // recognises "build/create a pipeline that..." as a construction
    // intent and produces an IR directly.
    //
    // Falls through to the agent loop on backend failure or unrecognised
    // input — so "explain this step" / "why did X fail" still reach the
    // tool-using agent.
    if (intent === 'create' || intent === 'modify') {
      try {
        const response = await api.chat(msgs.map((m) => ({ role: m.role, content: m.content })));
        // Capture the structured clarification payload when present so
        // <ClarifyCard /> can render chips instead of the markdown fallback.
        const clarification = (response as any).clarification;
        set((s) => ({
          chatMessages: [
            ...s.chatMessages,
            {
              role: 'assistant',
              content: response.reply,
              ...(clarification && Array.isArray(clarification.questions)
                ? { clarification }
                : {}),
            },
          ],
        }));
        if (response.workflow) {
          get().loadWorkflow({ workflow: response.workflow, version: 1 });
          toast.success('Pipeline draft created');
        }
        return;
      } catch {
        // Rule planner unavailable / errored — fall through to agent loop.
      }
    }

    // Tool-using agent — same endpoint the FloatingAgentWidget uses.
    // Right for "explain X" / "why did Y fail" / "validate this pipeline"
    // and anything that benefits from live workspace introspection via
    // the 24-tool registry.
    //
    // Fall through to the deterministic client-side handlers when:
    //  - no AI provider is configured (no_provider=true), OR
    //  - the call exceeds 60s — local models below the 2026-05-19 tool-use
    //    floor (e.g. Ollama qwen2.5:1.5b / :3b) can stall the multi-iteration
    //    tool loop entirely. Even at the floor (qwen2.5:7b), a single turn
    //    runs 30–60 s on CPU. Without this timeout the user sees nothing for
    //    the full backend wall-clock (120s cloud / 300s ollama). The
    //    deterministic handlers are canvas-aware and answer most questions
    //    usefully.
    try {
      const wfId = get().workflowId;
      const wfName = get().workflowName;
      const allNodes = get().nodes;
      const allEdges = get().edges;
      const selectedNodeId = get().selectedNodeId;
      const stepResults = get().stepResults || {};
      const visibleNodeItems = allNodes.slice(0, 50).map((n: any) => ({
        id: n.id,
        name: (n.data as any)?.label || n.id,
        kind: (n.data as any)?.stepType || n.type || 'node',
        meta: { type: (n.data as any)?.stepType || n.type || '' },
      }));

      // Build extra_context (2026-05-22) — the editor's richer payload.
      // The backend sanitizes + budget-caps this; we send a SUMMARY of
      // the pipeline, not the full IR, so the prompt stays manageable
      // for local 7B models.
      const selectedNode = selectedNodeId
        ? allNodes.find((n) => n.id === selectedNodeId)
        : null;
      const selectedResult = selectedNodeId ? stepResults[selectedNodeId] : null;
      const stepIssues = Object.entries(stepResults)
        .filter(([, r]: any) => r?.error || r?.status === 'error')
        .map(([id, r]: any) => ({
          step_id: id,
          error: (r?.error || '').toString().slice(0, 200),
        }));
      const extra_context: Record<string, unknown> = {
        workflow: wfId ? {
          id: wfId,
          name: wfName,
          step_count: allNodes.length,
          edge_count: allEdges.length,
          steps: allNodes.slice(0, 30).map((n: any) => ({
            id: n.id,
            label: (n.data as any)?.label,
            type: (n.data as any)?.stepType,
          })),
        } : null,
        ...(selectedNode ? {
          selected_node: {
            id: selectedNode.id,
            label: (selectedNode.data as any)?.label,
            type: (selectedNode.data as any)?.stepType,
            // Params can carry secrets; the backend's sanitize layer
            // strips known credential keys before the LLM sees them.
            params: (selectedNode.data as any)?.params || {},
            last_result: selectedResult ? {
              status: selectedResult.status,
              row_count: selectedResult.row_count,
              error: selectedResult.error,
            } : null,
          },
        } : {}),
        ...(stepIssues.length > 0 ? { step_issues: stepIssues.slice(0, 10) } : {}),
      };

      // Build conversation memory from the in-store chat (2026-05-22).
      // Send the last 10 turns verbatim PLUS a deterministic bullet
      // summary of older history that fell out of the window. The
      // summarizer is client-side only (no LLM round-trip) so the
      // assistant has continuity without the latency cost of a
      // separate summarize call. Backend caps the summary at 1200 chars.
      const allMsgs = get().chatMessages || [];
      const { recent_turns, summary } = buildConversationPayload(
        allMsgs as any,
        10,
      );

      const AGENT_TIMEOUT_MS = 60_000;
      const agentPromise = api.runAgent({
        user_intent: message,
        page_context: {
          // 2026-05-22: use the dotted form so the tool_selector's
          // page-bucket lookup ("editor" → editor tool pack) resolves.
          page: 'editor.canvas',
          // Pass the open pipeline as both visible + selected so tools
          // like validate_pipeline / summarize_pipeline / explain_step
          // can default-resolve to it without the LLM threading IDs.
          visible_ids: wfId ? [wfId] : [],
          selected_ids: wfId ? [wfId] : [],
          filters: {},
          environment: 'dev',
          visible_items: visibleNodeItems,
          extra_context,
        },
        conversation: { recent_turns, summary },
        mode: deepRequested ? 'deep' : 'standard',
        allow_safe_writes: false,
        max_tokens: 4000,
        dialogue_state: {},
      });
      const timeoutPromise = new Promise<null>((resolve) => {
        setTimeout(() => resolve(null), AGENT_TIMEOUT_MS);
      });
      const agent = await Promise.race([agentPromise, timeoutPromise]);
      if (agent === null) {
        // Agent timed out (small local LLM stuck in tool loop). Fall
        // through to deterministic handlers — the agentPromise is left
        // dangling on purpose; if it eventually returns, its result is
        // discarded since the user has already received an answer.
      } else if (agent?.no_provider) {
        // No AI configured — let the deterministic handlers below run.
      } else if (agent?.final_text) {
        set((s) => ({
          chatMessages: [...s.chatMessages, {
            role: 'assistant',
            content: agent.final_text,
            tool_steps: (agent.steps || [])
              .filter((st) => st.tool_name && st.tool_name !== '(no tools)')
              .map((st) => ({ tool_name: st.tool_name, outcome: st.outcome, latency_ms: st.latency_ms })),
          }],
        }));
        return;
      }
    } catch {
      // Network error / endpoint missing — fall through to deterministic handlers.
    }

    // Client-side handlers for greeting / question intents, and as
    // fallback for create/modify when both rule-planner AND agent loop
    // are unreachable. (The rule-planner is now tried FIRST at the top
    // of this function for create/modify intents — no second backend
    // attempt needed here.)

    if (intent === 'greeting') {
      set((s) => ({
        chatMessages: [...s.chatMessages, {
          role: 'assistant',
          content: 'Hello! I\'m your **Pipeline Builder AI**. Tell me what data pipeline you need and I\'ll create it instantly.\n\nTry: *"Load orders.csv, filter amount > 100, deduplicate by order_id, output to parquet"*',
        }],
      }));
      return;
    }

    if (intent === 'question') {
      const nodes = get().nodes;
      const edges = get().edges;
      const params = get().parameters || [];
      const wfName = get().workflowName || '(unsaved)';
      const wfId = get().workflowId;
      const status = get().status || 'draft';
      const version = get().version || 0;
      const stepResults = get().stepResults || {};
      const lower = message.toLowerCase();

      // Canvas-aware reply helper. Every question gets a reply that
      // STARTS from the actual editor state — nodes, edges, params,
      // validation, last-run results — so the user never sees a
      // generic "could not determine data source" again on a question
      // about the canvas they're looking at.
      const canvasSummary = (): string => {
        if (nodes.length === 0) return '**Canvas:** empty — drag a node from the left panel, or describe what you want me to build.';
        const lines: string[] = [];
        lines.push(`**Pipeline:** ${wfName} ${wfId ? `(\`${wfId.slice(0, 8)}\`)` : '(unsaved)'} · status \`${status}\`${version ? ` · v${version}` : ''}`);
        lines.push(`**Canvas:** ${nodes.length} step${nodes.length === 1 ? '' : 's'}, ${edges.length} connection${edges.length === 1 ? '' : 's'}.`);
        return lines.join('\n');
      };

      const nodesList = (): string =>
        nodes.map((n) => {
          const t = (n.data as any)?.stepType || n.type || '?';
          const lbl = (n.data as any)?.label || t;
          const ran = stepResults[n.id];
          const tag = ran?.status === 'success' ? ' ✓' : ran?.status === 'error' ? ' ✗' : '';
          return `• **${lbl}** — \`${t}\`${tag}`;
        }).join('\n');

      // Run the validator so questions like "issues" / "errors" /
      // "valid" / "wrong" / "broken" report ACTUAL findings instead
      // of a canned welcome.
      const issues = (() => {
        try { return validateWorkflow(nodes, edges, params, wfId); } catch { return [] as any[]; }
      })();
      const errors = issues.filter((i: any) => i.level === 'error');
      const warnings = issues.filter((i: any) => i.level === 'warning');

      // 1) "issues" / "errors" / "wrong" / "broken" / "valid"
      if (/\b(issue|issues|error|errors|wrong|broken|problem|problems|invalid|valid|validate|validation|fail|failing)\b/.test(lower)) {
        if (nodes.length === 0) {
          set((s) => ({ chatMessages: [...s.chatMessages, { role: 'assistant', content: 'The canvas is empty — there\'s nothing to validate yet. Drag a Source from the left panel to start.' }] }));
          return;
        }
        if (errors.length === 0 && warnings.length === 0) {
          const reply = `${canvasSummary()}\n\n✓ **No validation issues.** All ${nodes.length} step${nodes.length === 1 ? ' is' : 's are'} configured correctly.`;
          set((s) => ({ chatMessages: [...s.chatMessages, { role: 'assistant', content: reply }] }));
          return;
        }
        const list = [
          ...errors.slice(0, 8).map((i: any) => `• **error** ${i.step_id ? `(${i.step_id.slice(0,8)})` : ''}: ${i.message}`),
          ...warnings.slice(0, 4).map((i: any) => `• **warning** ${i.step_id ? `(${i.step_id.slice(0,8)})` : ''}: ${i.message}`),
        ].join('\n');
        const reply = `${canvasSummary()}\n\n**${errors.length} error${errors.length === 1 ? '' : 's'}, ${warnings.length} warning${warnings.length === 1 ? '' : 's'}:**\n\n${list}\n\n${errors.length > 0 ? 'Fix the errors before publishing.' : 'You can publish now — warnings won\'t block it.'}`;
        set((s) => ({ chatMessages: [...s.chatMessages, { role: 'assistant', content: reply }] }));
        return;
      }

      // 2) "what does this do" / "explain" / "describe" → step-by-step
      if (nodes.length > 0 && /\b(what does|explain|describe|tell me about|walk me through)\b/.test(lower)) {
        const reply = `${canvasSummary()}\n\n**Steps:**\n${nodesList()}\n\nData flows top-to-bottom through the connections. Click any node on the canvas to see/edit its parameters.`;
        set((s) => ({ chatMessages: [...s.chatMessages, { role: 'assistant', content: reply }] }));
        return;
      }

      // 3) "nodes" / "steps" / "list"
      if (nodes.length > 0 && /\b(nodes|steps|list)\b/.test(lower)) {
        const reply = `${canvasSummary()}\n\n${nodesList()}`;
        set((s) => ({ chatMessages: [...s.chatMessages, { role: 'assistant', content: reply }] }));
        return;
      }

      // 4) "connections" / "edges" / "flow"
      if (nodes.length > 0 && /\b(connection|connections|edge|edges|flow|wired|wiring|connected)\b/.test(lower)) {
        if (edges.length === 0) {
          const reply = `${canvasSummary()}\n\nNo connections yet — nodes are disconnected. Drag from a node's right handle to another node's left handle to wire them up.`;
          set((s) => ({ chatMessages: [...s.chatMessages, { role: 'assistant', content: reply }] }));
          return;
        }
        const labelOf = (id: string) => (nodes.find((n) => n.id === id)?.data as any)?.label || id;
        const list = edges.map((e) => `• ${labelOf(e.source)} → ${labelOf(e.target)}${(e as any).label ? ` (${(e as any).label})` : ''}`).join('\n');
        const reply = `${canvasSummary()}\n\n**Connections:**\n${list}`;
        set((s) => ({ chatMessages: [...s.chatMessages, { role: 'assistant', content: reply }] }));
        return;
      }

      // 5) "parameters" / "params" / "variables"
      if (/\b(parameter|parameters|params|variable|variables)\b/.test(lower)) {
        if (params.length === 0) {
          const reply = `${canvasSummary()}\n\nThis pipeline has **no parameters defined**. Click **Parameters** in the toolbar to declare runtime inputs.`;
          set((s) => ({ chatMessages: [...s.chatMessages, { role: 'assistant', content: reply }] }));
          return;
        }
        const list = params.map((p: any) => `• \`${p.name}\` (${p.type})${p.default !== undefined ? ` — default: \`${JSON.stringify(p.default)}\`` : ''}${p.required ? ' · required' : ''}`).join('\n');
        const reply = `${canvasSummary()}\n\n**Parameters (${params.length}):**\n${list}`;
        set((s) => ({ chatMessages: [...s.chatMessages, { role: 'assistant', content: reply }] }));
        return;
      }

      // 6) "last run" / "history" / "status" / "executed"
      if (/\b(last run|history|executed|run result|results)\b/.test(lower)) {
        const ran = Object.values(stepResults).filter((r: any) => r);
        if (ran.length === 0) {
          const reply = `${canvasSummary()}\n\nNo step results in memory for this session. Click **Run** to execute, or visit **Workflows → Executions** for past runs.`;
          set((s) => ({ chatMessages: [...s.chatMessages, { role: 'assistant', content: reply }] }));
          return;
        }
        const list = nodes.map((n) => {
          const r = stepResults[n.id] as any;
          if (!r) return null;
          const lbl = (n.data as any)?.label || n.id;
          const tag = r.status === 'success' ? '✓' : r.status === 'error' ? '✗' : '•';
          const dur = r.duration_ms ? ` · ${r.duration_ms}ms` : '';
          const rows = r.row_count ? ` · ${r.row_count.toLocaleString()} rows` : '';
          const err = r.error ? ` — \`${String(r.error).slice(0, 80)}\`` : '';
          return `${tag} **${lbl}**${dur}${rows}${err}`;
        }).filter(Boolean).join('\n');
        const reply = `${canvasSummary()}\n\n**Last-run results (this session):**\n${list}`;
        set((s) => ({ chatMessages: [...s.chatMessages, { role: 'assistant', content: reply }] }));
        return;
      }

      // 7) "sql" / "query" / "code" — pseudo-SQL representation
      if (nodes.length > 0 && /\b(sql|query)\b/.test(lower)) {
        const sources = nodes.filter((n) => /source|webhook/.test((n.data as any)?.stepType || ''));
        const sinks = nodes.filter((n) => /sink|destination/.test((n.data as any)?.stepType || ''));
        const middle = nodes.filter((n) => !sources.includes(n) && !sinks.includes(n));
        const fmt = (xs: any[]) => xs.map((x) => `\`${(x.data as any)?.label || (x.data as any)?.stepType}\``).join(', ');
        const lines = [
          `${canvasSummary()}`,
          '',
          'Approximate flow:',
          '```',
          `FROM   ${fmt(sources) || '(no source)'}`,
          ...middle.map((n) => `       → ${(n.data as any)?.label} [${(n.data as any)?.stepType}]`),
          `WRITE  ${fmt(sinks) || '(no destination)'}`,
          '```',
          'The engine executes each step; this isn\'t literal SQL.',
        ];
        set((s) => ({ chatMessages: [...s.chatMessages, { role: 'assistant', content: lines.join('\n') }] }));
        return;
      }

      // 8) Context-aware fallback. Anything that didn't hit a specific
      // branch — instead of dropping the user into a generic welcome
      // screen, give them the canvas summary + a hint at what they
      // can ask. This is the user's "context-aware" expectation:
      // every question reply STARTS from the editor's current state.
      const hints = [
        '• "What does this pipeline do?" — step-by-step explanation',
        '• "What are the issues?" — run validation and report errors / warnings',
        '• "List the nodes" / "Show the connections"',
        '• "What parameters does it take?"',
        '• "Show last run results"',
        '• "Show the SQL"',
      ].join('\n');
      const fallback = nodes.length === 0
        ? 'The canvas is empty. Describe a pipeline (e.g. *"Load orders.csv, deduplicate by order_id, write to Parquet"*) and I\'ll build it.'
        : `${canvasSummary()}\n\nI can answer questions about this pipeline. Try:\n\n${hints}`;
      set((s) => ({ chatMessages: [...s.chatMessages, { role: 'assistant', content: fallback }] }));
      return;
    }

    if (intent === 'modify') {
      const nodes = get().nodes;
      const edges = get().edges;
      const result = parseModification(message, nodes);
      if (result.action === 'add' && result.nodeType) {
        // Add node and optionally connect it after the specified node
        get().addNode(result.nodeType);
        if (result.afterNodeId) {
          // Find the newly added node (the last one) and connect + position it
          setTimeout(() => {
            const currentNodes = get().nodes;
            const newNode = currentNodes[currentNodes.length - 1];
            if (newNode && result.afterNodeId) {
              const afterNode = currentNodes.find(n => n.id === result.afterNodeId);
              if (afterNode) {
                // Move the new node near the after node
                set((s) => ({
                  nodes: s.nodes.map(n =>
                    n.id === newNode.id
                      ? { ...n, position: { x: afterNode.position.x + 250, y: afterNode.position.y + 100 } }
                      : n
                  ),
                }));
              }
              // Connect it
              const edgeId = `e-${result.afterNodeId}-${newNode.id}`;
              set((s) => ({
                edges: addEdge(
                  { id: edgeId, source: result.afterNodeId!, target: newNode.id, sourceHandle: 'output', targetHandle: 'input', type: 'custom', data: { condition: 'completion' } },
                  s.edges,
                ),
              }));
            }
            get().reactFlowInstance?.fitView({ padding: 0.3, duration: 300 });
          }, 50);
        } else {
          setTimeout(() => get().reactFlowInstance?.fitView({ padding: 0.3, duration: 300 }), 100);
        }
        set((s) => ({
          chatMessages: [...s.chatMessages, { role: 'assistant', content: result.reply }],
        }));
        toast.success('Node added');
        return;
      }
      if (result.action === 'remove' && result.nodeId) {
        get().deleteNode(result.nodeId);
        set((s) => ({
          chatMessages: [...s.chatMessages, { role: 'assistant', content: result.reply }],
        }));
        toast.info('Node removed');
        return;
      }
      if (result.action === 'modify' && result.nodeId && result.params) {
        get().updateNodeParams(result.nodeId, result.params);
        set((s) => ({
          chatMessages: [...s.chatMessages, { role: 'assistant', content: result.reply }],
        }));
        toast.success('Node updated');
        return;
      }
      // Action is 'none' but has a reply (suggestions/prompts)
      if (result.action === 'none' && result.reply) {
        set((s) => ({
          chatMessages: [...s.chatMessages, { role: 'assistant', content: result.reply }],
        }));
        return;
      }
    }

    // Create pipeline
    const pipeline = parsePipelineIntent(message);
    if (pipeline) {
      get().loadWorkflow({
        workflow: {
          id: null,
          name: pipeline.name,
          steps: pipeline.steps,
          connections: pipeline.connections,
        },
        version: 1,
      });
      set((s) => ({
        chatMessages: [...s.chatMessages, { role: 'assistant', content: pipeline.reply }],
        workflowName: pipeline.name,
      }));
      toast.success('Pipeline created!', `${pipeline.steps.length} steps generated`);
      // Auto-fit view
      setTimeout(() => get().reactFlowInstance?.fitView({ padding: 0.3, duration: 300 }), 200);
    } else {
      set((s) => ({
        chatMessages: [...s.chatMessages, {
          role: 'assistant',
          content: 'I wasn\'t able to understand that pipeline description. Try something like:\n\n• *"Load orders.csv, filter by status = active, output to parquet"*\n• *"Read sales.csv, join with products.csv on product_id, aggregate by category, save to database"*\n• *"Import data from API, deduplicate by id, validate, export to S3"*',
        }],
      }));
    }
  },

  useTemplate: async (key) => {
    // User templates — IDs prefixed with `ut_` come from user_templates
    // and are fetched from the backend, then loaded just like a built-in.
    if (key.startsWith('ut_')) {
      try {
        const list = await api.listUserTemplates();
        const found = (list?.templates || []).find((t: any) => t.id === key);
        if (found) {
          // Preserve the current pipeline name — see naming rule on
          // the built-in branch below.
          const preservedName = get().workflowName;
          get().loadWorkflow({
            name: preservedName,
            steps: found.steps || [],
            connections: found.connections || [],
          } as any);
          set((s) => ({
            chatMessages: [
              ...s.chatMessages,
              { role: 'assistant', content: `Loaded **${found.name}** (your template, ${(found.steps || []).length} nodes). Rename the pipeline if you'd like, then click **Save**.` },
            ],
          }));
          return;
        }
      } catch (err) {
        console.error('Failed to load user template:', err);
      }
    }

    // Templates catalog — single source of truth for the gallery + the
    // legacy in-canvas "Use template" buttons. New templates land in
    // src/templates/catalog.ts; this method just resolves them.
    //
    // Naming rule: a template loads its NODES and CONNECTIONS but NEVER
    // overwrites the pipeline name. The user picks the name themselves
    // (or keeps the existing one). Without this, picking "CSV → Postgres"
    // would silently rename the user's "Q3 Sales Load" pipeline to
    // "CSV → Postgres", which feels like data loss.
    try {
      const { getTemplate } = await import('../templates/catalog');
      // Backwards-compat alias: HeroPromptCard still calls
      // useTemplate('simple_etl'); the new catalog uses 'csv_to_postgres'
      // for the same shape, so map it.
      const aliased = key === 'simple_etl' ? 'csv_to_postgres' : key;
      const fromCatalog = getTemplate(aliased);
      if (fromCatalog) {
        const preservedName = get().workflowName;
        get().loadWorkflow({
          name: preservedName,
          steps: fromCatalog.steps,
          connections: fromCatalog.connections,
        } as any);
        set((s) => ({
          chatMessages: [
            ...s.chatMessages,
            { role: 'assistant', content: `Loaded **${fromCatalog.name}** template (${fromCatalog.steps.length} nodes). Rename the pipeline if you'd like, then click **Save**.` },
          ],
        }));
        return;
      }
    } catch {
      // Fall through to legacy templates if the dynamic import fails.
    }

    // Legacy hand-written templates — kept as a fallback so anything
    // referencing keys outside the new catalog still resolves.
    const CLIENT_TEMPLATES: Record<string, any> = {
      simple_etl: {
        name: 'Simple ETL Pipeline',
        steps: [
          { id: 't1', type: 'source', label: 'Source', params: { connector_type: 'csv', file_path: 'orders.csv' }, position: { x: 0, y: 100 } },
          { id: 't2', type: 'transform', label: 'Transform', params: { expression: "SELECT *, CURRENT_TIMESTAMP AS loaded_at FROM source_table" }, position: { x: 350, y: 100 } },
          { id: 't3', type: 'destination', label: 'Destination', params: { connector_type: 'parquet', file_path: 'output/etl_result.parquet' }, position: { x: 700, y: 100 } },
        ],
        connections: [
          { from_step: 't1', to_step: 't2' },
          { from_step: 't2', to_step: 't3' },
        ],
      },
      dedup_pipeline: {
        name: 'Deduplication Pipeline',
        steps: [
          { id: 't1', type: 'source', label: 'Source', params: { connector_type: 'csv', file_path: 'orders.csv' }, position: { x: 0, y: 100 } },
          { id: 't2', type: 'deduplicate', label: 'Deduplicate', params: { key: ['order_id'], strategy: 'keep_first' }, position: { x: 350, y: 100 } },
          { id: 't3', type: 'destination', label: 'Destination', params: { connector_type: 'parquet', file_path: 'output/dedup_result.parquet' }, position: { x: 700, y: 100 } },
        ],
        connections: [
          { from_step: 't1', to_step: 't2' },
          { from_step: 't2', to_step: 't3' },
        ],
      },
      aggregation: {
        name: 'Aggregation Report',
        steps: [
          { id: 't1', type: 'source', label: 'Source', params: { connector_type: 'csv', file_path: 'orders.csv' }, position: { x: 0, y: 100 } },
          { id: 't2', type: 'filter', label: 'Filter Valid Rows', params: { condition: 'amount IS NOT NULL AND amount > 0' }, position: { x: 350, y: 100 } },
          { id: 't3', type: 'aggregate', label: 'Aggregate', params: { group_by: ['category'], aggregations: [{ column: 'amount', function: 'SUM', alias: 'total_amount' }, { column: '*', function: 'COUNT', alias: 'count' }] }, position: { x: 700, y: 100 } },
          { id: 't4', type: 'destination', label: 'Destination', params: { connector_type: 'csv', file_path: 'output/aggregation_report.csv' }, position: { x: 1050, y: 100 } },
        ],
        connections: [
          { from_step: 't1', to_step: 't2' },
          { from_step: 't2', to_step: 't3' },
          { from_step: 't3', to_step: 't4' },
        ],
      },
      data_quality: {
        name: 'Data Quality Pipeline',
        steps: [
          { id: 't1', type: 'source', label: 'Source', params: { connector_type: 'csv', file_path: 'orders.csv' }, position: { x: 0, y: 100 } },
          { id: 't2', type: 'filter', label: 'Remove Nulls', params: { condition: 'order_id IS NOT NULL' }, position: { x: 350, y: 100 } },
          { id: 't3', type: 'deduplicate', label: 'Deduplicate', params: { key: ['order_id'], strategy: 'keep_first' }, position: { x: 700, y: 100 } },
          { id: 't4', type: 'transform', label: 'Validate & Tag', params: { expression: "SELECT *, CASE WHEN order_id IS NOT NULL THEN 'valid' ELSE 'invalid' END AS quality_flag FROM source_table" }, position: { x: 1050, y: 100 } },
          { id: 't5', type: 'destination', label: 'Destination', params: { connector_type: 'parquet', file_path: 'output/quality_result.parquet' }, position: { x: 1400, y: 100 } },
        ],
        connections: [
          { from_step: 't1', to_step: 't2' },
          { from_step: 't2', to_step: 't3' },
          { from_step: 't3', to_step: 't4' },
          { from_step: 't4', to_step: 't5' },
        ],
      },
    };

    // Use client-side templates (always current) — no backend dependency
    const tpl = CLIENT_TEMPLATES[key];
    if (tpl) {
      get().loadWorkflow(tpl);
      set((s) => ({
        chatMessages: [
          ...s.chatMessages,
          { role: 'assistant', content: `Created pipeline from **${key}** template. Edit any node on the canvas or click **Run All**.` },
        ],
      }));
      return;
    }

    // Fallback to backend for custom/unknown templates
    try {
      const result = await api.useTemplate(key);
      get().loadWorkflow(result);
      set((s) => ({
        chatMessages: [
          ...s.chatMessages,
          { role: 'assistant', content: `Created pipeline from **${key}** template. Edit any node on the canvas or click **Run All**.` },
        ],
      }));
    } catch (err: any) {
      console.error('Template error:', err);
    }
  },

  createFromIntent: async (intent) => {
    try {
      const result = await api.generatePlan(intent);
      if (result.workflow) {
        get().loadWorkflow({ workflow: result.workflow, version: result.version || 1 });
      }
      return result;
    } catch (err: any) {
      console.error('Plan error:', err);
    }
  },

  updateNodeParams: (nodeId, params) => {
    set((s) => {
      // Clear validation errors for this node when params change
      const { [nodeId]: _, ...remainingErrors } = s.validationErrors;
      return {
        nodes: s.nodes.map((n) => {
          if (n.id !== nodeId) return n;
          // xyflow Node.data is Record<string, unknown> at the library level;
          // narrow to our shape so the spread of `data.params` typechecks.
          const data = n.data as { params?: Record<string, any> } & Record<string, unknown>;
          return {
            ...n,
            data: { ...data, params: { ...(data.params || {}), ...params } },
          };
        }),
        validationErrors: remainingErrors,
        isDirty: true,
        // PR 1: param changes alter what a node outputs (Rename renames
        // columns, Typecast changes types, Derived adds columns). Every
        // downstream step's schema cache is invalid until refetched.
        stepSchemas: {},
        schemaVersion: s.schemaVersion + 1,
      };
    });
  },

  updateNodeLabel: (nodeId, label) => {
    set((s) => ({
      nodes: s.nodes.map((n) =>
        n.id === nodeId ? { ...n, data: { ...n.data, label } } : n,
      ),
      isDirty: true,
    }));
  },

  addNode: (type, position) => {
    get().pushUndoState();
    const id = Math.random().toString(36).slice(2, 10);

    // Resolve virtual connector types (e.g. "rest:salesforce") to their backing
    // StepType + default params from the cached /api/node-types registry.
    const preset = NODE_PRESETS[type];
    const lookupType = preset?.base ?? type;
    let stepType = lookupType;
    let initialParams: Record<string, any> = {};
    let label = preset?.label
      ?? type.replace(/_/g, ' ').replace(/\b\w/g, (c: string) => c.toUpperCase());
    let category = NODE_CATEGORY[lookupType] || 'transform';
    try {
      const backendTypes: any[] = (window as any).__fpulse_node_types || [];
      const meta = backendTypes.find((t) => t.type === lookupType);
      if (meta) {
        if (meta.base_type) stepType = meta.base_type;
        if (meta.default_params) initialParams = { ...meta.default_params };
        if (meta.label && !preset) label = meta.label;
        if (meta.category) category = meta.category;
      }
    } catch {}
    // Preset params win over backend defaults (e.g. mode='explode').
    if (preset) initialParams = { ...initialParams, ...preset.params };

    // Smart positioning: place near the last node or in a reasonable grid
    let pos = position;
    if (!pos) {
      const existing = get().nodes;
      if (existing.length > 0) {
        const maxX = Math.max(...existing.map((n) => n.position.x));
        const avgY = existing.reduce((s, n) => s + n.position.y, 0) / existing.length;
        pos = { x: maxX + 280, y: avgY };
      } else {
        pos = { x: 100, y: 100 };
      }
    }
    const node: Node = {
      id,
      type: 'fpulseNode',
      position: pos,
      data: {
        label,
        stepType,
        params: initialParams,
        color: NODE_COLORS[stepType] || '#94a3b8',
        icon: NODE_ICONS[stepType] || '⬜',
        category,
        risk: 'low',
      },
    };
    set((s) => ({ nodes: [...s.nodes, node] }));
  },

  deleteNode: (nodeId) => {
    get().pushUndoState();
    set((s) => ({
      nodes: s.nodes.filter((n) => n.id !== nodeId),
      edges: s.edges.filter((e) => e.source !== nodeId && e.target !== nodeId),
      selectedNodeId: s.selectedNodeId === nodeId ? null : s.selectedNodeId,
    }));
  },

  // Persist canvas. UPDATE-ONLY by default. Creates only when the caller
  // explicitly opts in via `{ allowCreate: true }` — see the type-level
  // docstring above. Enforces the 2026-05-09 "no silent pipeline create"
  // rule: only the Save button creates rows.
  //
  // 2026-05-19 (P2 #18 of PAGE_BY_PAGE_AUDIT.md): the name-prompt loop is
  // now embedded in this action via a dynamic import of
  // `requireNamedWorkflow`. Previously the prompt lived as a policy on
  // callers — three sites called it correctly, but any new direct caller
  // of `ensureWorkflow({ allowCreate: true })` (a future feature, an
  // import path, an AI agent action) could slip an "Untitled Pipeline"
  // row past the rule. Now the rule is in the store; callers may still
  // call `requireNamedWorkflow()` ahead of time for a one-shot prompt
  // gate, but they no longer have to.
  ensureWorkflow: async (opts) => {
    const { workflowId, nodes, edges, workflowName } = get();
    if (workflowId) {
      // Update existing — always safe, never creates.
      const wf = nodesToWorkflow(nodes, edges, workflowName, workflowId, get().parameters);
      await api.updateWorkflow(workflowId, wf, 'Auto-save');
      // Server now matches local state → clear dirty flag so the Close
      // button stops prompting for unsaved-changes. Any edit returns
      // the pipeline to draft locally — the user must re-publish before
      // schedules (Free) or Deploy/PROD promotion (Plus) is allowed.
      set({ isDirty: false, status: 'draft' });
      return workflowId;
    }
    // No workflowId — and caller didn't opt in to create. Silently no-op
    // and return null so callers (autosave, Test Node, Run) fail-fast
    // with a clear UX message instead of producing an "Untitled Pipeline"
    // row the user never asked for.
    if (!opts?.allowCreate) {
      return null;
    }
    // Explicit-create path. The store owns the name-prompt loop so every
    // callsite enforces the 2026-05-09 "no silent pipeline create" rule
    // without relying on the caller to pre-prompt. OSS-8 (2026-05-19):
    // replaces the previous dynamic import of utils/requireNamedWorkflow
    // with the inline `promptForWorkflowName` helper (defined at the top
    // of this module) — the helper file is now deleted.
    let trimmedName = (workflowName || '').trim();
    if (!trimmedName || trimmedName.toLowerCase() === PLACEHOLDER_NAME) {
      const proposed = await promptForWorkflowName(() => get());
      if (!proposed) return null;
      trimmedName = proposed;
    }
    // Pass the active project so the row lands in the right project on
    // first save. Pull the freshly-prompted name from the store so the
    // create row reflects whatever the user just typed (the captured
    // `workflowName` destructure at the top of this action predates the
    // prompt loop).
    const { projectId, folderId } = get();
    const nameForCreate = (get().workflowName || '').trim();
    const result = await api.createWorkflow(nameForCreate, projectId || undefined);
    const newId = result.id || result.workflow?.id;
    if (newId) {
      const wf = nodesToWorkflow(nodes, edges, nameForCreate, newId, get().parameters);
      // The IR carries project_id too — keep it in sync with the
      // create call so a subsequent updateWorkflow doesn't overwrite
      // the project back to "default".
      if (projectId) (wf as any).project_id = projectId;
      await api.updateWorkflow(newId, wf, 'Initial save');
      // Sub-folder placement — the createWorkflow API only takes a
      // project_id, so any folder choice has to be a follow-up call.
      // Skipped when folderId is null (project root).
      if (folderId) {
        try {
          await api.moveWorkflowsToFolder({ workflow_ids: [newId], folder_id: folderId });
        } catch (err) {
          // Folder move is best-effort — the workflow is already saved
          // at the project root, so a folder-move failure shouldn't
          // block the save.
          console.warn('Initial folder placement failed:', err);
        }
      }
      set({ workflowId: newId, isDirty: false, status: 'draft' });
      return newId;
    }
    return null;
  },

  // Get upstream node IDs for a given node
  getUpstreamNodeIds: (nodeId: string) => {
    const { edges } = get();
    return edges.filter((e) => e.target === nodeId).map((e) => e.source);
  },

  reactFlowInstance: null,
  setReactFlowInstance: (instance) => set({ reactFlowInstance: instance }),
}));
