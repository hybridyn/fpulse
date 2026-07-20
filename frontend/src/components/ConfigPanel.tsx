import { useState, useEffect, useRef, useMemo, lazy, Suspense, createContext, useContext } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import { api } from '../api/client';
import { ModuleIcon } from './ModulesPanel';
import { useEmbeddedAI, type DiagnoseResult } from '../hooks/useEmbeddedAI';
import { useUpstreamSchema } from '../hooks/useUpstreamSchema';
import { toast } from './Toast';
import DynamicConfig from './DynamicConfig';
import { DataInBand, DataOutBand } from './NodeConfigFrame';
import { buildDataIn, buildDataOut, deriveOutputColumns } from '../utils/nodeUiContract';
import ExpressionPreview from './ExpressionPreview';
// P5 (2026-05-21): cast-safety classifier mirrors the backend taxonomy
// so the Mapping tab renders ✓ / ⚠ / ✕ glyphs without a network hop.
import { classifyCastUI, TIER_META, type CastTier } from '../lib/castSafety';
// 2026-05-19 (OSS-6 of PAGE_BY_PAGE_AUDIT.md) + FOLLOW-5 (2026-05-19):
// TransformHelper and CatalogPicker join DataWranglerConfig in the
// lazy-load tier. Each is a substantial component that only renders for
// specific step types, so shipping them out of the initial ConfigPanel
// bundle drops mount-time cost on most node selections. Pattern is
// identical to the DataWranglerConfig migration: lazy() + a Suspense
// wrapper. We export thin Suspense-wrapped wrappers (TransformHelper /
// CatalogPicker) so the ~10 call sites below stay unchanged.
//
// FOLLOW-5 status — the remaining ~58 inline per-step configs (each is
// a `{stepType === 'X' && (...)}` JSX block in this file) follow the
// same recipe but need extraction-to-file first:
//   1. Cut the JSX block (say, the `database` config at line ~1900) to
//      a new file: `components/config-panels/database.tsx`.
//   2. Hoist the closure-captured props (params, onChange, columns,
//      upstreamColumns, etc.) into a typed Props interface.
//   3. Import here as `const DatabaseConfig = lazy(() => import('./config-panels/database'))`.
//   4. Wrap the render site in `<Suspense fallback={<ConfigPanelLazyFallback />}>`.
//
// Held back from this round because each per-step extraction is its
// own commit + screenshot diff. Three lazy components proves the pattern;
// the rest is mechanical follow-up that benefits from per-config code
// review.
const LazyTransformHelper = lazy(() => import('./TransformHelper'));
const LazyCatalogPicker = lazy(() => import('./CatalogPicker'));
function TransformHelper(props: any) {
  return (
    <Suspense fallback={<ConfigPanelLazyFallback />}>
      <LazyTransformHelper {...props} />
    </Suspense>
  );
}
function CatalogPicker(props: any) {
  return (
    <Suspense fallback={<ConfigPanelLazyFallback />}>
      <LazyCatalogPicker {...props} />
    </Suspense>
  );
}
// 2026-05-19 (OSS-3 of PAGE_BY_PAGE_AUDIT.md): auto-derived required-
// field markers. See the `RequiredMark` + `Field` helpers below.
import { isRequiredFieldFor, isOneOfFieldFor } from '../utils/validateWorkflow';
// FOLLOW-6 (2026-05-19): canonical navigate helper.
import { navigateTo } from '../router';
// 2026-05-19 (P2 #14 of PAGE_BY_PAGE_AUDIT.md): DataWranglerConfig is the
// first per-step-type config to migrate to React.lazy. It's the largest
// already-extracted external config (~1500 lines of its own + a wide
// surface of sub-step types) and proves the lazy-load pattern works end
// to end. The remaining ~60 inline configs inside this file are tracked
// for the same treatment in a dedicated follow-up sweep — each one
// becomes a `components/config-panels/<stepType>.tsx` module and gets
// wrapped in `lazy(() => import(...))` here, then rendered inside the
// shared <Suspense> boundary below. The fallback is intentionally tiny
// (a one-line "Loading…" stub) so the user notices nothing on a hit and
// sees a friendly placeholder on a miss.
const DataWranglerConfig = lazy(() => import('./data-wrangler/DataWranglerConfig'));
// 2026-05-19 (OSS-8): the standalone requireNamedWorkflow helper is
// gone — the store's `ensureWorkflow` runs the prompt loop internally
// and returns null on cancel. See the Test Node handler below.
import { usePageContext } from '../hooks/usePageContext';
import { setOpen as setAgentDockOpen, setTurns as setAgentTurns } from '../hooks/useAgentChatStore';

// Shared Suspense fallback for lazy-loaded per-step-type configs. Kept
// minimal so a fast network shows zero flicker; sized to match the
// approximate footprint of the config so the panel doesn't jump.
function ConfigPanelLazyFallback() {
  return (
    <div className="p-4 text-xs text-slate-400">Loading config…</div>
  );
}

/**
 * Node Config Editor — centered modal overlay.
 * Shows when a node is selected with config on top, data preview on bottom.
 */

/** Step types with hardcoded config functions — DynamicConfig skips these. */
const HARDCODED_CONFIGS = new Set([
  'csv_source', 'db_source', 'filter', 'transform', 'data_wrangler',
  'local_table_source', 'local_table_sink',
  'deduplicate', 'aggregate',
  'join', 'output', 'api_source', 'sort', 'rename', 'typecast', 'derived_column',
  'lookup', 'union', 'pivot', 'unpivot', 'window', 'sample', 'validate',
  'conditional_split', 'db_sink', 'json_source', 'parquet_source', 'excel_source',
  'xml_source', 's3_source', 'adls_gen2_source', 'adls_gen2_sink',
  'azure_blob_source', 'azure_blob_sink', 'gcs_source', 'gcs_sink',
  'file_source', 'file_sink', 'sharepoint_source', 'sharepoint_sink',
  'onedrive_source', 'onedrive_sink', 'gdrive_source', 'gdrive_sink',
  'dropbox_source', 'dropbox_sink', 'box_source', 'box_sink',
  'kafka_source', 'ftp_source', 'gsheet_source', 'delta_source',
  'saas_connector', 'data_quality', 'upsert', 'schema_mapper', 'scd2',
  'embedder', 'llm_guardrail', 'semantic_router',
  'flatten_explode', 'materialize', 'retry_handler',
  'csv_sink', 'json_sink', 'excel_sink', 's3_sink', 'kafka_sink',
  'api_sink', 'webhook_sink', 'email_sink', 'delta_sink', 'warehouse_sink',
  'if_condition', 'switch_case', 'foreach_loop', 'until_loop',
  'wait_delay', 'set_variable', 'execute_pipeline',
  'http_request', 'webhook_trigger', 'code_script',
  'copy_data', 'delete_data', 'send_email', 'slack_notify',
  'append_variable', 'filter_array', 'validation', 'fail',
  'file_system', 'execute_sql_task',
  'source', 'destination',
]);

/** Nodes whose config genuinely needs a wide canvas — grids, source↔sink
 *  column mappings, multi-pane / repeater-heavy UIs. Everything NOT here (and
 *  not data_wrangler, which is wider still) opens as a compact ~620px
 *  properties-panel modal so simple nodes don't render mostly whitespace. */
const WIDE_CONFIGS = new Set([
  'copy_data', 'schema_mapper', 'data_quality', 'join',
  'warehouse_sink', 'db_sink', 'destination',
  'embedder', 'semantic_router', 'llm_guardrail',
  'aggregate', 'window', 'pivot', 'unpivot',
]);

/** Shared control style for the Advanced tab — byte-for-byte the same as the
 *  Parameters-tab TextInput (size, padding, border, pipe accent) so the two
 *  tabs read as one consistent surface with identical fonts. */
const SETTINGS_INPUT =
  'w-full px-3 py-2 text-sm text-slate-800 bg-white border border-slate-300 rounded-lg ' +
  'focus:outline-none focus:ring-2 focus:ring-pipe-300 focus:border-pipe-400 placeholder:text-slate-400';
/** Shared label style — matches the Parameters-tab Field label exactly. */
const SETTINGS_LABEL = 'block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1.5';

type ConfigTab = 'parameters' | 'mapping' | 'settings';

export default function ConfigPanel() {
  const { selectedNodeId, nodes, edges, updateNodeParams, updateNodeLabel, stepResults, setSelectedNode, runStep } = useWorkflowStore();
  const node = nodes.find((n) => n.id === selectedNodeId);
  const [activeTab, setActiveTab] = useState<ConfigTab>('parameters');
  const [testing, setTesting] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const renameRef = useRef<HTMLInputElement>(null);

  // ── Modal width state — driven by left/right edge resize handles ──────────
  // Native CSS `resize: both` only exposes a bottom-right corner handle. For
  // wider editor-style configs (especially Data Wrangler), users want to
  // grab either side. We mirror the corner-resize idea with two slim
  // mouse-drag handles glued to the modal's left + right edges.
  //
  // Default width is wider for the Data Wrangler node — it hosts a three-pane
  // workspace (Steps | Config | Preview) and needs the room. Other nodes get
  // the original 920px form-style width.
  const [modalWidth, setModalWidth] = useState<number>(920);
  const resizingRef = useRef<{ side: 'left' | 'right'; startX: number; startWidth: number } | null>(null);

  const startResize = (side: 'left' | 'right') => (e: React.MouseEvent) => {
    e.preventDefault();
    resizingRef.current = { side, startX: e.clientX, startWidth: modalWidth };
    document.body.style.cursor = 'ew-resize';
    document.body.style.userSelect = 'none';
  };

  useEffect(() => {
    const onMove = (ev: MouseEvent) => {
      const r = resizingRef.current;
      if (!r) return;
      const delta = ev.clientX - r.startX;
      // Dragging the LEFT handle right = shrink; dragging it left = grow.
      // Dragging the RIGHT handle right = grow; dragging it left = shrink.
      const next = r.side === 'right' ? r.startWidth + delta : r.startWidth - delta;
      const max = Math.floor(window.innerWidth * 0.95);
      setModalWidth(Math.max(520, Math.min(max, next)));
    };
    const onUp = () => {
      if (!resizingRef.current) return;
      resizingRef.current = null;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, []);

  // 2026-06-03 — select-vs-open separation between SELECTION and OPENING.
  //
  //   Single-click on canvas  →  selects the node (highlights it,
  //                              setSelectedNode populates the store
  //                              for chat/copilot context) but does
  //                              NOT open the config modal. User can
  //                              pan, scan, hover without an overlay
  //                              blocking the canvas.
  //
  //   Double-click on canvas  →  dispatches `fpulse-node-opened`;
  //                              we set `open` and the modal renders.
  //
  //   Right-click "Open Settings" / "Fix configuration"
  //                            →  also dispatches the event
  //                              (handled in FPulseNode).
  //
  //   Background click        →  clears selectedNodeId; the effect
  //                              below auto-closes the modal so the
  //                              user doesn't get a stale-node modal.
  //
  //   Switching nodes inside modal (upstream/downstream/prev/next
  //   buttons) just calls setSelectedNode without dispatching the
  //   event — modal stays open, content swaps to the new node.
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const handler = (e: Event) => {
      const id = (e as CustomEvent).detail?.id as string | undefined;
      if (!id) return;
      setOpen(true);
      // After React commits + paints, scroll to top and focus the
      // first editable field. Two RAFs because focus() races mount
      // and silently no-ops otherwise.
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          const body = document.querySelector('[data-fpulse-config-body]') as HTMLElement | null;
          if (!body) return;
          body.scrollTop = 0;
          const candidates = body.querySelectorAll<HTMLElement>(
            'input:not([type="hidden"]):not([disabled]):not([readonly]), textarea:not([disabled]):not([readonly]), select:not([disabled])',
          );
          let first: HTMLElement | null = null;
          for (const el of candidates) {
            if (el.closest('[data-skip-autofocus]')) continue;
            if (el.classList.contains('fpulse-rename-input')) continue;
            first = el;
            break;
          }
          first?.focus();
          if (first instanceof HTMLInputElement && (first.type === 'text' || first.type === 'search' || first.type === 'url' || first.type === 'email')) {
            first.select();
          }
        });
      });
    };
    window.addEventListener('fpulse-node-opened', handler);
    return () => window.removeEventListener('fpulse-node-opened', handler);
  }, []);

  // Auto-close the modal when the user deselects (background click on canvas).
  // Without this, the modal would orphan and need an explicit X click to
  // dismiss after `setSelectedNode(null)` runs.
  useEffect(() => {
    if (!selectedNodeId) setOpen(false);
  }, [selectedNodeId]);

  // ── Focus mode: collapse the side rails while a node config is open ──
  // The config modal needs the room, and mid-edit you're not pulling from
  // the left Nodes palette or chatting with the right-side Copilot (the
  // node has its own AI Assist button). So collapse BOTH rails when the
  // modal opens and restore exactly what the user had when it closes.
  // Capture-on-open / restore-on-close via a ref so a rail the user had
  // deliberately collapsed stays collapsed afterward. Reads the store
  // imperatively (getState) so the effect depends only on `open` and
  // doesn't re-fire when it flips the panels itself.
  const prevPanelsRef = useRef<{ nodes: boolean; chat: boolean } | null>(null);
  useEffect(() => {
    const store = useWorkflowStore.getState();
    if (open) {
      if (prevPanelsRef.current === null) {
        prevPanelsRef.current = {
          nodes: store.nodesPanelOpen ?? true,
          chat: store.chatOpen ?? true,
        };
      }
      store.setNodesPanelOpen?.(false);
      store.setChatOpen?.(false);
    } else if (prevPanelsRef.current) {
      store.setNodesPanelOpen?.(prevPanelsRef.current.nodes);
      store.setChatOpen?.(prevPanelsRef.current.chat);
      prevPanelsRef.current = null;
    }
  }, [open]);
  // Safety net: if the panel unmounts while still open (e.g. navigating
  // away from the editor mid-edit), restore the rails so they aren't left
  // stuck collapsed the next time the editor mounts.
  useEffect(() => () => {
    const prev = prevPanelsRef.current;
    if (!prev) return;
    const store = useWorkflowStore.getState();
    store.setNodesPanelOpen?.(prev.nodes);
    store.setChatOpen?.(prev.chat);
  }, []);

  // AI hooks
  const { autoFillConfig, diagnoseError, loading: aiLoading } = useEmbeddedAI();
  const [aiDiagnosis, setAiDiagnosis] = useState<DiagnoseResult | null>(null);
  const [showDiagnosis, setShowDiagnosis] = useState(false);
  const [autoFilling, setAutoFilling] = useState(false);
  const [aiAssistOpen, setAiAssistOpen] = useState(false);

  // Find all workflow nodes (for prev/next navigation) — hooks before early return
  const sortedNodes = useMemo(() => {
    return [...nodes].sort((a, b) => (a.position?.x ?? 0) - (b.position?.x ?? 0));
  }, [nodes]);

  const currentIndex = useMemo(() => {
    return sortedNodes.findIndex((n) => n.id === selectedNodeId);
  }, [sortedNodes, selectedNodeId]);

  // Publish code context to the Copilot whenever a SQL/transform node is
  // selected. The code-aware slash commands (/sql /fix /explain-code) read
  // this from useCurrentPageContext() to inject the active expression.
  // The hook MUST run unconditionally — placed BEFORE the early return.
  const _activeNodeData = node?.data as any;
  const _activeStepType = _activeNodeData?.stepType || '';
  const _activeParams = _activeNodeData?.params || {};
  const _isCodeNode = _activeStepType === 'transform' || _activeStepType === 'execute_sql_task';
  const _activeExpr = _isCodeNode
    ? String(_activeParams.expression || _activeParams.sql || _activeParams.query || '')
    : '';
  // Visible IDs — every node currently on the canvas. Lets the agent
  // answer "summarize this pipeline" / "validate the canvas" / "what's
  // here?" without the user naming things. Bounded to 50 to keep the
  // page-context payload under the 2KB budget cap.
  const _visibleNodeIds = useMemo(
    () => sortedNodes.slice(0, 50).map((n) => n.id),
    [sortedNodes],
  );
  usePageContext({
    page: 'editor.canvas',
    selected_ids: selectedNodeId ? [selectedNodeId] : [],
    visible_ids: _visibleNodeIds,
    code: _isCodeNode && _activeExpr
      ? {
          node_id: selectedNodeId || undefined,
          node_type: _activeStepType,
          language: _activeStepType === 'execute_sql_task' ? 'sql' : 'expression',
          expression: _activeExpr.slice(0, 4000),  // bounded
          // `result` is declared further down — read directly from
          // stepResults here to avoid the use-before-declaration error.
          last_error: stepResults[selectedNodeId!]?.error || undefined,
        }
      : undefined,
  });

  // Reset modal width to the canonical default when switching between nodes.
  // Placed AFTER `_activeStepType` is initialized above so we don't hit the
  // temporal-dead-zone error. Width scales to how much the node actually
  // needs — simple nodes (a handful of fields) open as a compact
  // properties-panel-width modal so they don't show 70% whitespace; only
  // grid / mapping / multi-pane nodes get the wide canvas. Users can still
  // drag the left/right edge handles for more room.
  useEffect(() => {
    if (!_activeStepType) return;
    setModalWidth(
      _activeStepType === 'data_wrangler' ? 1280
        : WIDE_CONFIGS.has(_activeStepType) ? 920
        : 620,   // simple nodes → compact centered modal
    );
  }, [_activeStepType]);

  // 2026-06-17 — the config modal no longer collapses the side rails on
  // open. Collapsing them resized the canvas, which made the selected node
  // visibly jump on every open/close (user-reported: "why are you moving
  // the node?"). The modal is a fixed overlay that centers itself between
  // the rails via CSS vars, so it needs no extra room — leaving the rails
  // untouched keeps the canvas geometry, and the node, perfectly stable.

  // Schema Propagation hook — must run on every render in the same order,
  // so it lives ABOVE the `if (!node) return null;` early return.
  // useUpstreamSchema accepts null and degrades gracefully to empty
  // columns when no node is selected. (The previous placement after the
  // early return tripped "Rendered more hooks than during the previous
  // render" whenever the panel mounted briefly with no node, then
  // received one on the next tick.)
  const liveSchema = useUpstreamSchema(selectedNodeId);

  // 2026-06-03 — gate on BOTH selection AND explicit open state.
  // Single-click selects (sets selectedNodeId) but does not open the
  // modal; only double-click / context-menu / explicit "Open Settings"
  // dispatch the `fpulse-node-opened` event that sets `open`.
  if (!node || !open) return null;

  const { stepType, params, label, category } = node.data as any;
  const settings = params?._settings || {};
  const result = stepResults[selectedNodeId!];
  const isSource = category === 'source';

  // NodeConfigFrame (2026-06-16) — Data In / Data Out bands derived from the
  // live upstream schema + the registry, rendered around EVERY node's
  // Processing form so configs read consistently (Data In → Processing →
  // Data Out → Settings/Advanced). Skipped for full-bleed nodes (Data Wrangler
  // owns its own surface). Join orders inputs Left-first by left_input_id.
  const _frameInputs = ((liveSchema.raw?.inputs ?? []) as any[])
    .slice()
    .sort((a, b) => {
      if (stepType !== 'join' || !params?.left_input_id) return 0;
      const L = params.left_input_id;
      return a.upstream_step_id === L ? -1 : b.upstream_step_id === L ? 1 : 0;
    })
    .map((i: any) => ({
      label: i.upstream_label || i.upstream_step_id || '',
      columns: ((i.schema?.columns ?? []) as any[]).map((c) => c.name as string),
    }));
  const frameDataIn = buildDataIn(stepType, _frameInputs);
  // Show the real resulting columns when they're deterministically knowable
  // (filter/derived/rename/… ); deriveOutputColumns returns [] for dynamic or
  // collision-prone nodes so the band falls back to its disposition summary.
  const frameOutColumns = deriveOutputColumns(stepType, _frameInputs, (params || {}) as Record<string, any>);
  const frameDataOut = buildDataOut(stepType, (params || {}) as Record<string, any>, frameOutColumns);

  // Full-bleed mode — the node owns the whole modal surface (no tab bar,
  // no Available-Columns chip strip, no outer padding). Today only the
  // Data Wrangler claims this; future workspace-style nodes can opt in by
  // adding their step type here.
  const isWrangler = stepType === 'data_wrangler';
  const fullBleed = isWrangler;
  // 2026-06-15 — user chose CENTERED modals for every node (sized to content:
  // compact for simple, wide for complex). The right-docked properties-panel
  // path is parked behind this flag (kept false) rather than ripped out, so it
  // can be re-enabled if we revisit the docked-properties-panel layout.
  const dockedPanel = false;

  // Find upstream columns from executed results.
  //
  // Hybrid strategy (PR 1 — Schema Propagation Loop):
  //   1. If `stepResults[upstream]` is populated (user ran the pipeline),
  //      use those column names — they're the ground-truth post-run state.
  //   2. Otherwise fall back to `liveSchema` from `useUpstreamSchema`,
  //      which probes the backend via `SELECT * LIMIT 0` so column-name
  //      dropdowns work BEFORE the user ever clicks Run. This is the fix
  //      for the silent-broken-pipeline class of bugs.
  const upstreamEdges = edges.filter((e) => e.target === node.id);
  const upstreamColumns: string[] = [];
  // column_name → DuckDB type string (e.g. "VARCHAR", "INTEGER", "DOUBLE").
  // Populated from the upstream step's schema_info when the pipeline has
  // been run; the live-schema fallback below adds entries from the
  // SELECT * LIMIT 0 probe when the user hasn't run yet. Surfaced by the
  // ColumnMapper as type chips next to the source column names.
  const upstreamSourceTypes: Record<string, string> = {};
  const upstreamNodes: Array<{ id: string; label: string; stepType: string; file?: string }> = [];
  for (const edge of upstreamEdges) {
    const upNode = nodes.find((n) => n.id === edge.source);
    if (upNode) {
      // xyflow Node.data is typed as Record<string, unknown> at the
      // library level — narrow to F-Pulse's node-data shape here.
      // Pre-existing untyped pattern; a proper FpulseNodeData type can
      // replace these casts in a follow-up refactor.
      const ud = upNode.data as { label?: string; stepType?: string; params?: Record<string, string | undefined> };
      upstreamNodes.push({
        id: upNode.id,
        label: ud.label || ud.stepType || '',
        stepType: ud.stepType || '',
        file: ud.params?.file || ud.params?.path || ud.params?.url,
      });
    }
    const upResult = stepResults[edge.source];
    if (upResult?.columns) {
      upstreamColumns.push(...upResult.columns);
    }
    if (upResult?.schema_info && Array.isArray(upResult.schema_info)) {
      for (const s of upResult.schema_info) {
        if (s?.name && s?.type && !(s.name in upstreamSourceTypes)) {
          upstreamSourceTypes[s.name] = s.type;
        }
      }
    }
  }

  // Find ALL ancestor nodes (not just direct parents)
  const allAncestorNodes: Array<{ id: string; label: string; stepType: string; file?: string; columns: string[]; depth: number }> = [];
  const visitedAnc = new Set<string>();
  function walkAncestors(currentId: string, depth: number) {
    const incoming = edges.filter((e) => e.target === currentId);
    for (const edge of incoming) {
      if (visitedAnc.has(edge.source)) continue;
      visitedAnc.add(edge.source);
      const n = nodes.find((nd) => nd.id === edge.source);
      if (n) {
        const res = stepResults[edge.source];
        // Same xyflow Node.data narrowing as the upstreamNodes loop above.
        const nd = n.data as { label?: string; stepType?: string; params?: Record<string, string | undefined> };
        allAncestorNodes.push({
          id: n.id,
          label: nd.label || nd.stepType || '',
          stepType: nd.stepType || '',
          file: nd.params?.file || nd.params?.path || nd.params?.url,
          columns: res?.columns || [],
          depth,
        });
        walkAncestors(edge.source, depth + 1);
      }
    }
  }
  walkAncestors(node.id, 1);

  // PR 1: live schema fallback. When stepResults has no columns yet
  // (user hasn't run the pipeline), use `liveSchema` (declared above the
  // early return so hook order stays stable). Merged AFTER
  // upstreamColumns so the run-time result wins when both are available.
  if (upstreamColumns.length === 0 && liveSchema.columns.length > 0) {
    for (const col of liveSchema.columns) {
      if (!upstreamColumns.includes(col.name)) {
        upstreamColumns.push(col.name);
      }
      if (col.name && col.type && !(col.name in upstreamSourceTypes)) {
        upstreamSourceTypes[col.name] = col.type;
      }
    }
  }

  // Connected nodes — actual edge-based lineage (not position-sorted)
  const connectedUpstream = upstreamEdges.map(e => {
    const n = nodes.find(nd => nd.id === e.source);
    return n ? { id: n.id, label: (n.data as any).label || (n.data as any).stepType, stepType: (n.data as any).stepType, condition: (e.data as any)?.condition || 'completion' } : null;
  }).filter(Boolean) as Array<{ id: string; label: string; stepType: string; condition: string }>;

  const downstreamEdges = edges.filter(e => e.source === node.id);
  const connectedDownstream = downstreamEdges.map(e => {
    const n = nodes.find(nd => nd.id === e.target);
    return n ? { id: n.id, label: (n.data as any).label || (n.data as any).stepType, stepType: (n.data as any).stepType, condition: (e.data as any)?.condition || 'completion' } : null;
  }).filter(Boolean) as Array<{ id: string; label: string; stepType: string; condition: string }>;

  // Only show prev/next for actually connected nodes — never fall back to
  // position-based. These slots are scaffolded UI for a future "navigate to
  // prev/next connected node" affordance; today they're null.
  //
  // 2026-05-22 v2 — TS strict mode treats `const x: T | null = null` as
  // narrowed to `null`, which then makes the JSX guards collapse to `never`.
  // A small helper with an explicit return type breaks the narrowing chain
  // exactly like the `_toEnv` pattern in PipelinesPage. When the real
  // prev/next computation lands, this helper is the single insertion point.
  type _AdjacentNode = { id: string; data: Record<string, unknown> } | null;
  const _resolveAdjacent = (): _AdjacentNode => null;
  const prevNode: _AdjacentNode = _resolveAdjacent();
  const nextNode: _AdjacentNode = _resolveAdjacent();

  const COND_COLORS: Record<string, { bg: string; text: string; border: string }> = {
    completion: { bg: 'bg-indigo-50', text: 'text-indigo-600', border: 'border-indigo-200' },
    success: { bg: 'bg-green-50', text: 'text-green-600', border: 'border-green-200' },
    failure: { bg: 'bg-red-50', text: 'text-red-600', border: 'border-red-200' },
  };

  const updateSettings = (key: string, value: any) => {
    updateNodeParams(node.id, { _settings: { ...settings, [key]: value } });
  };

  const handleTestNode = async () => {
    setTesting(true);
    try {
      // Z10 (2026-05-23) — Test should NEVER force a save. Users want
      // to iterate on a node's config; making them name + persist the
      // whole pipeline first was bad friction (the 2026-05-09 "no
      // silent create" rule applied to Save, not to Test).
      //
      // Two cases, both go through workflowStore.runStep which now
      // dispatches on ephemeral vs persisted internally:
      //   - Unsaved pipeline → ephemeral path (no DB row, no row in
      //     Pipelines list, no execution_history entry).
      //   - Saved pipeline → existing PUT-then-run path (so the IR
      //     stays in sync with what the user sees on canvas).
      await runStep(selectedNodeId!);
    } catch (err) {
      console.error('Test node error:', err);
    } finally {
      // MUST live in `finally` — a throw from runStep would otherwise
      // leave the button stuck in "Testing…" state.
      setTesting(false);
    }
  };

  return (
    <>
    {/* Backdrop overlay — constrained to canvas area (excludes chat + modules
        panels on sides, toolbar above, preview panel below) so the dim stays
        strictly inside the canvas. Bottom stops AT the preview card's top
        edge (not overlapping its mt-1 / border-2).

        `left` tracks the live ModulesPanel (LEFT) width via
        `--fpulse-nodes-panel-width`; `right` tracks the live ChatPanel
        (RIGHT) width via `--fpulse-chat-panel-width`. Both are published
        from their respective panel components on every resize tick so
        this overlay reflows with them. */}
    {/* Backdrop — dims AND blurs the canvas behind the config so the editor
        feels foregrounded. Top stops at the editor toolbar (145px), bottom
        stops at the PreviewPanel (240px), so the bottom data grid + canvas
        zoom toolbar both stay visible and clickable. Click anywhere on the
        backdrop to close the config.
        In full-bleed mode (Wrangler) the side panels are hidden, so the
        backdrop extends edge-to-edge (`left-0 right-0`). */}
    <div
      className={`fixed top-[145px] z-40 transition-opacity ${
        dockedPanel ? 'bg-slate-900/10' : 'bg-slate-900/40 backdrop-blur-sm'
      } ${
        fullBleed
          ? 'left-0 right-0'
          : 'left-[var(--fpulse-nodes-panel-width,290px)] right-[var(--fpulse-chat-panel-width,320px)]'
      }`}
      style={{ bottom: 'var(--fpulse-preview-panel-height, 240px)' }}
      onClick={() => setSelectedNode(null)}
    />

    {/* Centered modal — positioned inside the canvas area, above the
        preview panel's top edge. Z12 (2026-05-23): bottom now tracks
        the resizable PreviewPanel height via the CSS variable the
        panel publishes on every drag tick. Default 240px matches the
        legacy fixed height (h-60) so pre-Z8 sessions are unchanged. */}
    <div
      className={`fixed top-[145px] z-50 flex pointer-events-none ${
        dockedPanel ? 'items-stretch justify-end' : 'items-center justify-center'
      } ${
        fullBleed
          ? 'left-0 right-0'
          : 'left-[var(--fpulse-nodes-panel-width,290px)] right-[var(--fpulse-chat-panel-width,320px)]'
      }`}
      style={{ bottom: 'var(--fpulse-preview-panel-height, 240px)' }}
    >
      {/* Left side — connected upstream nodes (centered-modal layout only;
          the docked properties panel has no room for the side rails). */}
      {!dockedPanel && (
      <div className="pointer-events-auto mr-3 flex flex-col gap-1.5 w-[150px] shrink-0 items-end">
        {connectedUpstream.length > 0 ? connectedUpstream.map(up => {
          const cond = COND_COLORS[up.condition] || COND_COLORS.completion;
          return (
            <button
              key={up.id}
              onClick={() => setSelectedNode(up.id)}
              className="flex items-center gap-2 px-3 py-2 bg-white rounded-xl shadow-lg border border-slate-200 hover:border-blue-300 hover:shadow-xl transition-all group w-full"
              title={`← ${up.label}`}
            >
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="text-slate-300 group-hover:text-blue-400 shrink-0">
                <polyline points="15 18 9 12 15 6" />
              </svg>
              <ModuleIcon type={up.stepType} size={18} />
              <div className="min-w-0 text-left flex-1">
                <div className="text-xs font-semibold text-slate-600 group-hover:text-blue-600 truncate">{up.label}</div>
                <div className="text-[8px] text-slate-400 uppercase truncate">{up.stepType.replace(/_/g, ' ')}</div>
              </div>
            </button>
          );
        }) : prevNode ? (
          <button
            onClick={() => setSelectedNode(prevNode.id)}
            className="flex items-center gap-2 px-3 py-2 bg-white rounded-xl shadow-lg border border-slate-200 hover:border-blue-300 hover:shadow-xl transition-all group w-full"
            title={`← ${(prevNode.data as any)?.label || 'Previous'}`}
          >
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="text-slate-300 group-hover:text-blue-400 shrink-0">
              <polyline points="15 18 9 12 15 6" />
            </svg>
            <ModuleIcon type={(prevNode.data as any)?.stepType} size={18} />
            <div className="min-w-0 text-left flex-1">
              <div className="text-xs font-semibold text-slate-600 group-hover:text-blue-600 truncate">{(prevNode.data as any)?.label || 'Previous'}</div>
              <div className="text-[8px] text-slate-400 uppercase truncate">{((prevNode.data as any)?.stepType || '').replace(/_/g, ' ')}</div>
            </div>
          </button>
        ) : null}
      </div>
      )}

      {/* Modal container — user-resizable on three edges.
          Width is state-driven (modalWidth) and dragged via the LEFT/RIGHT
          edge handles below. Height keeps the native CSS bottom-edge handle
          via `resize-y`. Bounded by min-w/min-h (so users can't shrink it
          to nothing) and max-w/max-h (so it never escapes the viewport).
          The inner body has its own scroll so a double-scroll never appears.

          Outer wrapper provides the brand-gradient border (1.5px) — the
          inner div holds the actual content on a white background. The
          gradient is the same indigo→purple→pink as the F-Pulse Copilot
          pill so the panel reads as part of the same product family. */}
      {/*
        2026-05-22 — height bound. The parent wrapper is already
        `top-[145px] bottom-[240px]` (canvas region between the
        editor toolbar and the preview panel). Using `max-h-full`
        here makes the modal honor that bound — previously the
        `max-h-[92vh]` cap pulled from the full viewport and
        overflowed into the preview panel below.
        min-h dropped from 400 → 300 so the modal still fits on
        short windows where the bounded region is < 400px tall.
      */}
      <div
        className={`pointer-events-auto relative max-w-[95vw] p-[3px] shadow-2xl bg-gradient-to-br from-indigo-500 via-purple-500 to-pink-500 overflow-auto ${
          dockedPanel
            ? 'h-full max-h-full min-w-[360px] rounded-l-2xl'   // right-docked: full height, rounded left edge only
            // 2026-06-15 — no forced min-height for normal nodes: the modal now
            // HUGS its content (the white panel is h-full of this box), so short
            // tabs like an empty Mapping no longer leave a big gradient-border
            // gap below, and switching nodes re-sizes to each node's content.
            // Data Wrangler keeps a min-height because its 3-pane workspace
            // needs a definite height to lay out.
            : `resize-y min-w-[520px] max-h-full rounded-2xl ${fullBleed ? 'min-h-[420px]' : ''}`
        }`}
        style={{ width: modalWidth }}
      >
      {/* LEFT edge resize handle — invisible 6px strip pinned to the modal's
          left border. Becomes a faint white bar on hover so users discover it. */}
      <div
        onMouseDown={startResize('left')}
        className="absolute left-0 top-4 bottom-4 w-1.5 cursor-ew-resize hover:bg-white/50 rounded-full z-20 transition-colors"
        title="Drag to resize"
      />
      {/* RIGHT edge resize handle — only on the centered modal. The docked
          panel sits flush to the viewport's right edge, so it widens by
          dragging its LEFT handle. */}
      {!dockedPanel && (
        <div
          onMouseDown={startResize('right')}
          className="absolute right-0 top-4 bottom-4 w-1.5 cursor-ew-resize hover:bg-white/50 rounded-full z-20 transition-colors"
          title="Drag to resize"
        />
      )}
      <div
        data-fpulse-config-body
        className="w-full h-full bg-white rounded-[13px] flex flex-col overflow-auto"
      >

      {/* Header — icon, name, nav, close */}
      <div className="px-5 py-3.5 border-b border-slate-200/60 flex items-center gap-3 shrink-0 bg-gradient-to-r from-white to-slate-50/50">
        <ModuleIcon type={stepType} size={36} />
        <div className="flex-1 min-w-0">
          {renaming ? (
            <input
              ref={renameRef}
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              onBlur={() => {
                const trimmed = renameValue.trim();
                if (trimmed && trimmed !== label) updateNodeLabel(node.id, trimmed);
                setRenaming(false);
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') { e.currentTarget.blur(); }
                if (e.key === 'Escape') { setRenameValue(label); setRenaming(false); }
              }}
              className="text-base font-semibold text-slate-800 bg-blue-50 border border-blue-300 rounded-md px-2 py-0.5 w-full focus:outline-none focus:ring-2 focus:ring-blue-300"
              autoFocus
            />
          ) : (
            <div
              className="text-base font-semibold text-slate-800 truncate cursor-text hover:text-blue-600 hover:underline hover:decoration-dashed hover:underline-offset-2 transition-colors"
              onClick={() => { setRenameValue(label); setRenaming(true); }}
              title="Click to rename"
            >
              {label}
            </div>
          )}
          <div className="text-xs text-slate-400 uppercase tracking-wider font-medium">{stepType.replace(/_/g, ' ')}</div>
        </div>

        {/* Single adaptive AI button. Two separate pills (AI Fix + AI
            Assist) were redundant — they shared the same intent ("AI,
            help me with this node") and added clutter. The button now
            2026-05-19 (P1 #17 of PAGE_BY_PAGE_AUDIT.md): previously this
            slot rendered ONE button — AI Fix when an error existed, else
            AI Assist — so a user with an error in their node had no way
            to reach the Assist helper panel. We now render both buttons
            when an error is present (Fix first because it's the more
            urgent affordance), and just Assist when the node is clean. */}
        {result?.error && (
          <button
            onClick={async () => {
              try {
                // Use the rich liveSchema (real DuckDB types) when available
                // so the diagnoser sees actual column names + types instead of
                // a list of strings typed as 'string'. Local LLMs at the
                // 2026-05-19 tool-use floor (qwen2.5:7b) still hallucinate
                // positional names like 'col0' / 'column0' when they don't
                // have real schema to anchor on; sub-floor models fail even
                // more aggressively.
                const upSchema = liveSchema.columns.length > 0
                  ? liveSchema.columns.map((c) => ({ name: c.name, type: c.type || 'string' }))
                  : upstreamColumns.map((c) => ({ name: c, type: 'string' }));
                const diagnosis = await diagnoseError(result.error!, stepType, params, upSchema);
                setAiDiagnosis(diagnosis);
                setShowDiagnosis(true);
              } catch {
                toast.error('AI Diagnose', 'Failed to diagnose error');
              }
            }}
            disabled={aiLoading === 'diagnose'}
            className="px-3 py-1.5 text-xs font-bold rounded-lg disabled:opacity-50 transition-all shadow-sm hover:shadow-md flex items-center gap-1.5 shrink-0 text-white"
            style={{ background: 'linear-gradient(135deg, #ef4444, #db2777)' }}
            title="AI Fix: diagnose this error and suggest changes"
          >
            {aiLoading === 'diagnose' ? (
              <><span className="w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin" />Diagnosing…</>
            ) : (
              <><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" /></svg>AI Fix</>
            )}
          </button>
        )}
        <button
          onClick={() => setAiAssistOpen((v) => !v)}
          className="px-3 py-1.5 text-white text-xs font-bold rounded-lg transition-all shadow-sm hover:shadow-md flex items-center gap-1.5 shrink-0"
          style={{ background: aiAssistOpen
            ? 'linear-gradient(135deg, #7c3aed, #db2777)'
            : 'linear-gradient(135deg, #8b5cf6, #ec4899, #f59e0b)' }}
          title={result?.error
            ? 'AI Assist: ask AI to fill or improve this node (works even when the node has an error — Fix targets the error itself)'
            : 'AI Assist: ask AI to fill or improve this node'}
        >
          <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" stroke="none">
            <path d="M12 2l2.4 7.4H22l-6.2 4.5L18.2 22 12 17.5 5.8 22l2.4-8.1L2 9.4h7.6z" />
          </svg>
          AI Assist
        </button>

        {/* Test Node button */}
        <button
          onClick={handleTestNode}
          disabled={testing}
          className="px-3 py-1.5 text-white text-xs font-bold rounded-lg disabled:opacity-50 transition-all shadow-sm hover:shadow-md flex items-center gap-1.5 shrink-0"
          style={{ background: testing ? '#94a3b8' : 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
        >
          {testing ? (
            <><span className="w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin" />Testing...</>
          ) : (
            <><svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3" /></svg>Test Node</>
          )}
        </button>

        {/* Close */}
        <button
          onClick={() => setSelectedNode(null)}
          className="w-8 h-8 rounded-lg flex items-center justify-center text-slate-400 hover:text-slate-600 hover:bg-slate-100 transition-colors shrink-0"
          title="Close (Esc)"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>

      {/* Tab Bar — Parameters | Advanced. Always rendered (including
          full-bleed wrangler) so every node exposes the same execution
          policy (timeout / retry / on-error / notes). "Advanced" (not
          "Settings") + per-node error handling keeps the mental model
          aligned with classic data-pipeline tools rather than flow-automation tools. */}
      {(
        <div className="flex border-b border-slate-200/60 shrink-0">
          <button
            onClick={() => setActiveTab('parameters')}
            className={`flex-1 py-2.5 text-sm font-semibold transition-colors border-b-2 ${
              activeTab === 'parameters'
                ? 'text-blue-600 border-blue-500'
                : 'text-slate-500 border-transparent hover:text-slate-700'
            }`}
          >
            Parameters
          </button>
          {nodeHasMappingTab(stepType, params) && (
            <button
              onClick={() => setActiveTab('mapping')}
              className={`flex-1 py-2.5 text-sm font-semibold transition-colors border-b-2 ${
                activeTab === 'mapping'
                  ? 'text-blue-600 border-blue-500'
                  : 'text-slate-500 border-transparent hover:text-slate-700'
              }`}
            >
              Mapping
            </button>
          )}
          <button
            onClick={() => setActiveTab('settings')}
            className={`flex-1 py-2.5 text-sm font-semibold transition-colors border-b-2 ${
              activeTab === 'settings'
                ? 'text-blue-600 border-blue-500'
                : 'text-slate-500 border-transparent hover:text-slate-700'
            }`}
          >
            Advanced
          </button>
        </div>
      )}

      {/* Run-error banner — surfaces the last-run error inline at the
          top of the config panel so the user sees WHICH node failed and
          WHY without having to look at the Output tab below the canvas.
          Always visible while result.error exists; the AI Diagnosis
          card below appears on top when the user clicks AI Fix. */}
      {result?.error && (
        <div className="mx-4 mt-3 mb-1 px-3 py-2 rounded-lg border border-red-200 bg-red-50 text-red-800 shrink-0">
          <div className="flex items-start gap-2">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="text-red-500 shrink-0 mt-0.5">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="13" />
              <line x1="12" y1="16" x2="12" y2="16" />
            </svg>
            <div className="min-w-0 flex-1">
              <div className="text-[10px] font-bold uppercase tracking-wider text-red-600 mb-0.5">
                Last run failed on this node
              </div>
              <div className="text-xs font-mono break-words whitespace-pre-wrap leading-snug">
                {result.error}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* AI Diagnosis Card — shown when AI Fix has a result */}
      {showDiagnosis && aiDiagnosis && (
        <div className="mx-4 mt-3 mb-1 p-3 rounded-xl border bg-gradient-to-r from-red-50 to-orange-50 border-red-200/60 shrink-0">
          <div className="flex items-start justify-between gap-2 mb-2">
            <div className="flex items-center gap-1.5">
              <div className={`w-2 h-2 rounded-full ${
                aiDiagnosis.severity === 'high' ? 'bg-red-500' :
                aiDiagnosis.severity === 'medium' ? 'bg-amber-500' : 'bg-yellow-400'
              }`} />
              <span className="text-xs font-bold text-slate-600 uppercase tracking-wider">AI Diagnosis</span>
              <span className={`text-[8px] px-1.5 py-0.5 rounded-full font-bold uppercase ${
                aiDiagnosis.severity === 'high' ? 'bg-red-100 text-red-600' :
                aiDiagnosis.severity === 'medium' ? 'bg-amber-100 text-amber-600' : 'bg-yellow-100 text-yellow-600'
              }`}>{aiDiagnosis.severity}</span>
            </div>
            <button
              onClick={() => setShowDiagnosis(false)}
              className="text-slate-300 hover:text-slate-500 transition-colors"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </div>
          <div className="text-xs text-slate-700 mb-1.5">{aiDiagnosis.diagnosis}</div>
          <div className="text-xs text-slate-500 italic mb-2">{aiDiagnosis.suggestion}</div>
          {aiDiagnosis.auto_fix && Object.keys(aiDiagnosis.auto_fix).length > 0 && (
            <button
              onClick={() => {
                updateNodeParams(node.id, aiDiagnosis.auto_fix!);
                toast.success('AI Fix Applied', 'Parameters updated based on diagnosis');
                setShowDiagnosis(false);
                setAiDiagnosis(null);
              }}
              className="px-3 py-1.5 text-xs font-bold rounded-lg bg-emerald-500 text-white hover:bg-emerald-600 transition-colors shadow-sm flex items-center gap-1.5"
            >
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="20 6 9 17 4 12" />
              </svg>
              Apply Fix
            </button>
          )}
        </div>
      )}

      {/* Advanced Tab — execution policy common to every node:
          timeout, retry (count + interval), on-error, notes. The
          flow-automation-isms (Execute Once, Always Output Data) were removed — they confused
          ETL users; the engine still defaults them off for old pipelines. */}
      {activeTab === 'settings' && (() => {
        const retryCount = settings.retry_on_fail ? (Number(settings.max_retries) || 1) : 0;
        const retryIntervalSec = Math.round((Number(settings.retry_delay_ms) || 1000) / 1000);
        const setRetryCount = (n: number) =>
          updateNodeParams(node.id, { _settings: { ...settings, retry_on_fail: n > 0, max_retries: Math.max(0, n) } });
        const onError = settings.on_error || 'stop';
        return (
        <div className="flex-1 overflow-y-auto px-4 py-3.5 space-y-3.5">
          {/* Timeout */}
          <div>
            <label className={SETTINGS_LABEL}>Timeout (seconds)</label>
            <input
              type="number" min="0" step="10"
              value={settings.timeout_sec ?? 300}
              onChange={e => updateSettings('timeout_sec', parseInt(e.target.value) || 0)}
              className={SETTINGS_INPUT}
              placeholder="300 (5 min default)"
            />
            <p className="text-[11px] text-slate-400 mt-1">Max wall-clock before this node is cancelled. 0 = no limit.</p>
          </div>

          {/* Retry — count + interval (0 = no retry) */}
          <div className="grid grid-cols-2 gap-2.5">
            <div>
              <label className={SETTINGS_LABEL}>Retry count</label>
              <input
                type="number" min="0" max="10"
                value={retryCount}
                onChange={e => setRetryCount(parseInt(e.target.value) || 0)}
                className={SETTINGS_INPUT}
              />
            </div>
            <div>
              <label className={SETTINGS_LABEL}>Retry interval (sec)</label>
              <input
                type="number" min="0" step="1"
                value={retryIntervalSec}
                disabled={retryCount === 0}
                onChange={e => updateSettings('retry_delay_ms', (parseInt(e.target.value) || 0) * 1000)}
                className={`${SETTINGS_INPUT} ${retryCount === 0 ? 'opacity-50 bg-slate-50' : ''}`}
              />
            </div>
          </div>
          {retryCount === 0 && (
            <p className="text-[11px] text-slate-400 -mt-1.5">Set a retry count above 0 to retry this node on failure.</p>
          )}

          {/* On Error — per-node failure handling */}
          <div>
            <label className={SETTINGS_LABEL}>On error</label>
            <select
              value={onError}
              onChange={e => updateSettings('on_error', e.target.value)}
              className={SETTINGS_INPUT}
            >
              <option value="stop">Fail pipeline</option>
              <option value="continue">Skip this node &amp; continue</option>
              <option value="continue_error_output">Continue, keep error output</option>
            </select>
            <p className="text-[11px] text-slate-400 mt-1">
              {onError === 'continue'
                ? 'Downstream nodes still run, but this node passes no rows.'
                : onError === 'continue_error_output'
                ? 'Pipeline continues; this node is flagged failed so you can branch on it.'
                : 'Any error here stops the whole pipeline (default).'}
            </p>
          </div>

          {/* Notes + show-on-canvas */}
          <div>
            <label className={SETTINGS_LABEL}>Notes</label>
            <textarea
              value={settings.notes || ''}
              onChange={e => updateSettings('notes', e.target.value)}
              rows={3}
              placeholder="Add notes about this node..."
              className={`${SETTINGS_INPUT} resize-none`}
            />
            <label className="mt-2 flex items-center gap-2 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={!!settings.display_note}
                onChange={e => updateSettings('display_note', e.target.checked)}
                className="rounded border-slate-300 text-pipe-600 focus:ring-pipe-300"
              />
              <span className="text-[11px] text-slate-500">Show this note under the node on the canvas</span>
            </label>
          </div>

          {/* Node version */}
          <div className="pt-2.5 border-t border-slate-100">
            <span className="text-[11px] text-slate-400">
              {stepType.replace(/_/g, ' ').replace(/\b\w/g, (c: string) => c.toUpperCase())} · v1.0 (latest)
            </span>
          </div>
        </div>
        );
      })()}

      {/* Mapping Tab — surfaces the source→destination ColumnMapper as
          its own workspace for sink nodes. The Available Columns chip
          strip is reproduced here so the user can see what fields are
          mappable without flipping back to Parameters. */}
      {activeTab === 'mapping' && nodeHasMappingTab(stepType, params) && (
        <div className="flex-1 flex flex-col overflow-hidden min-h-0">
          {!fullBleed && upstreamColumns.length > 0 && (
            <div className="px-5 py-3 bg-slate-50 border-b border-slate-200 shrink-0">
              <div className="text-xs font-bold text-pipe-700 uppercase tracking-wider mb-2 flex items-center gap-1.5">
                <span>Available Columns</span>
                <span className="text-xs font-medium text-slate-500 normal-case tracking-normal">
                  ({upstreamColumns.length})
                </span>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {upstreamColumns.map((col) => (
                  <span
                    key={col}
                    className="text-xs px-2.5 py-1 bg-white text-slate-700 rounded-md border border-slate-300 font-mono cursor-default"
                    title={col}
                  >
                    {col}
                  </span>
                ))}
              </div>
            </div>
          )}
          <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
            {stepType === 'db_sink' && (
              <DbSinkConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} sourceTypes={upstreamSourceTypes} mappingOnly="show" />
            )}
            {stepType === 'warehouse_sink' && (
              <WarehouseSinkConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} sourceTypes={upstreamSourceTypes} mappingOnly="show" />
            )}
            {stepType === 'destination' && (
              <GenericDestinationConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} sourceTypes={upstreamSourceTypes} mappingOnly="show" />
            )}
          </div>
        </div>
      )}

      {/* Parameters Tab — gates on tab selection for every node now,
          including full-bleed (Data Wrangler) where the workspace
          renders inside this block when the Parameters tab is active. */}
      {activeTab === 'parameters' && (
      <div className="flex-1 flex flex-col overflow-hidden min-h-0">
      {/* Available-Columns strip retired 2026-06-16 — the NodeConfigFrame
          "Data In" band (rendered inside the body below) now shows per-input
          columns, so the standalone strip would just duplicate it. */}

      {/* AI Assist — interactive helper. Only mounts when the header pill
          is active so the panel doesn't carry a redundant always-visible
          strip (the pill is the single entry point). */}
      {aiAssistOpen && (
        <AiAssistSection stepType={stepType} params={params} nodeId={node.id} onChange={updateNodeParams} expanded={aiAssistOpen} setExpanded={setAiAssistOpen} />
      )}

      {/* A4 — Dynamic-schema banner. When ANY upstream ancestor produces
          schema only at runtime (pivot, transform, api_source, webhook,
          code_script, …), warn the user that column dropdowns below are
          best-effort and may be incomplete until they've run the pipeline
          once. Suppressed for full-bleed (Data Wrangler owns its own UI). */}
      {!fullBleed && liveSchema.dynamic && (
        <div className="mx-4 mt-2 mb-1 px-3 py-2 rounded-md border border-amber-200 bg-amber-50 text-xs text-amber-800 flex items-start gap-2 shrink-0">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="shrink-0 mt-0.5">
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <span className="leading-snug">
            Upstream <strong>{liveSchema.dynamicSources.slice(0, 2).join(', ')}</strong>
            {liveSchema.dynamicSources.length > 2 && ` (+${liveSchema.dynamicSources.length - 2} more)`}
            {' '}produces columns at runtime. Column lists below are best-effort —
            type column names by hand if a dropdown doesn't show what you expect,
            and re-open after the next run to refresh.
          </span>
        </div>
      )}

      {/* Config fields. Full-bleed nodes (Data Wrangler) manage their own
          padding/scroll internally — drop the outer p-4 + space-y-4 so the
          three-pane workspace can paint edge-to-edge. */}
      <div className={`flex-1 overflow-y-auto${fullBleed ? '' : ' p-4 space-y-4'}`}>
        {!fullBleed && <DataInBand d={frameDataIn} />}
        {stepType === 'csv_source' && (
          <CsvSourceConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {(stepType === 'local_table_source' || stepType === 'local_table_sink') && (
          <LocalTableConfig
            params={params}
            nodeId={node.id}
            onChange={updateNodeParams}
            isSink={stepType === 'local_table_sink'}
          />
        )}
        {stepType === 'db_source' && (
          <DbSourceConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'filter' && (
          <FilterConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'data_wrangler' && (
          <Suspense fallback={<ConfigPanelLazyFallback />}>
            <DataWranglerConfig
              params={params}
              nodeId={node.id}
              workflowId={useWorkflowStore.getState().workflowId || undefined}
              onChange={updateNodeParams}
            />
          </Suspense>
        )}
        {stepType === 'transform' && (
          <>
            <div className="mb-3">
              <TransformHelper
                nodeType="transform"
                expression={String(params.expression || '')}
                params={params}
                upstreamSchema={upstreamColumns.map((c: any) => ({
                  name: c.name || c,
                  type: c.type || 'string',
                  nullable: c.nullable !== false,
                }))}
                upstreamRowCount={0}
                onAcceptSql={(sql) => updateNodeParams(node.id, { expression: sql })}
              />
            </div>
            <TransformConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} upstreamNodes={upstreamNodes} allAncestors={allAncestorNodes} />
          </>
        )}
        {stepType === 'deduplicate' && (
          <DeduplicateConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'aggregate' && (
          <AggregateConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'join' && (
          <JoinConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'output' && (
          <OutputConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'api_source' && (
          <ApiSourceConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'sort' && (
          <SortConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'rename' && (
          <RenameConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'typecast' && (
          <TypecastConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'derived_column' && (
          <DerivedColumnConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'lookup' && (
          <LookupConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'union' && (
          <UnionConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'pivot' && (
          <PivotConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'unpivot' && (
          <UnpivotConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'window' && (
          <WindowConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'sample' && (
          <SampleConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'validate' && (
          <ValidateConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'conditional_split' && (
          <ConditionalSplitConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'db_sink' && (
          <DbSinkConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} sourceTypes={upstreamSourceTypes} mappingOnly="hide" />
        )}
        {stepType === 'json_source' && (
          <JsonSourceConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'parquet_source' && (
          <ParquetSourceConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'excel_source' && (
          <ExcelSourceConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'xml_source' && (
          <XmlSourceConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 's3_source' && (
          <S3SourceConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {(stepType === 'adls_gen2_source' || stepType === 'adls_gen2_sink') && (
          <AdlsGen2Config params={params} nodeId={node.id} onChange={updateNodeParams} isSink={stepType === 'adls_gen2_sink'} />
        )}
        {(stepType === 'azure_blob_source' || stepType === 'azure_blob_sink') && (
          <AzureBlobConfig params={params} nodeId={node.id} onChange={updateNodeParams} isSink={stepType === 'azure_blob_sink'} />
        )}
        {(stepType === 'gcs_source' || stepType === 'gcs_sink') && (
          <GcsConfig params={params} nodeId={node.id} onChange={updateNodeParams} isSink={stepType === 'gcs_sink'} />
        )}
        {(stepType === 'file_source' || stepType === 'file_sink') && (
          <FileNodeConfig params={params} nodeId={node.id} onChange={updateNodeParams} isSink={stepType === 'file_sink'} />
        )}
        {(stepType === 'sharepoint_source' || stepType === 'sharepoint_sink') && (
          <SharePointConfig params={params} nodeId={node.id} onChange={updateNodeParams} isSink={stepType === 'sharepoint_sink'} />
        )}
        {(stepType === 'onedrive_source' || stepType === 'onedrive_sink') && (
          <OneDriveConfig params={params} nodeId={node.id} onChange={updateNodeParams} isSink={stepType === 'onedrive_sink'} />
        )}
        {(stepType === 'gdrive_source' || stepType === 'gdrive_sink') && (
          <GoogleDriveConfig params={params} nodeId={node.id} onChange={updateNodeParams} isSink={stepType === 'gdrive_sink'} />
        )}
        {(stepType === 'dropbox_source' || stepType === 'dropbox_sink') && (
          <DropboxConfig params={params} nodeId={node.id} onChange={updateNodeParams} isSink={stepType === 'dropbox_sink'} />
        )}
        {(stepType === 'box_source' || stepType === 'box_sink') && (
          <BoxConfig params={params} nodeId={node.id} onChange={updateNodeParams} isSink={stepType === 'box_sink'} />
        )}
        {stepType === 'kafka_source' && (
          <KafkaSourceConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'ftp_source' && (
          <FtpSourceConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'gsheet_source' && (
          <GSheetSourceConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'delta_source' && (
          <DeltaSourceConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'saas_connector' && (
          <SaaSConnectorConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'data_quality' && (
          <DataQualityConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'upsert' && (
          <UpsertConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'schema_mapper' && (
          <SchemaMapperConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'scd2' && (
          <SCD2Config params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'embedder' && (
          <EmbedderConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'llm_guardrail' && (
          <LlmGuardrailConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'semantic_router' && (
          <SemanticRouterConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'flatten_explode' && (
          <FlattenExplodeConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'materialize' && (
          <MaterializeConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'retry_handler' && (
          <RetryHandlerConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'csv_sink' && (
          <CsvSinkConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'json_sink' && (
          <JsonSinkConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'excel_sink' && (
          <ExcelSinkConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 's3_sink' && (
          <S3SinkConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'kafka_sink' && (
          <KafkaSinkConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'api_sink' && (
          <ApiSinkConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'webhook_sink' && (
          <WebhookSinkConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'email_sink' && (
          <EmailSinkConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'delta_sink' && (
          <DeltaSinkConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'warehouse_sink' && (
          <WarehouseSinkConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} sourceTypes={upstreamSourceTypes} mappingOnly="hide" />
        )}
        {/* ── Flow Control ── */}
        {stepType === 'if_condition' && (
          <IfConditionConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'switch_case' && (
          <SwitchCaseConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'foreach_loop' && (
          <ForEachLoopConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'until_loop' && (
          <UntilLoopConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'wait_delay' && (
          <WaitDelayConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'set_variable' && (
          <SetVariableConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'execute_pipeline' && (
          <ExecutePipelineConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {/* ── Action ── */}
        {stepType === 'http_request' && (
          <HttpRequestConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'webhook_trigger' && (
          <WebhookTriggerConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'code_script' && (
          <CodeScriptConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'copy_data' && (
          <CopyDataConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'delete_data' && (
          <DeleteDataConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {/* get_metadata now renders via DynamicConfig: the backend
            param_schema is [] (it always profiles the upstream and ignored
            dataset_kind/path/fields), so DynamicConfig shows the honest
            "No configurable parameters" state instead of dead fields. */}
        {stepType === 'send_email' && (
          <SendEmailConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'slack_notify' && (
          <SlackNotifyConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {/* ── Control flow & integration primitives ── */}
        {stepType === 'append_variable' && (
          <AppendVariableConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'filter_array' && (
          <FilterArrayConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'validation' && (
          <ValidationConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'fail' && (
          <FailConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'file_system' && (
          <FileSystemConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
        )}
        {stepType === 'execute_sql_task' && (
          <>
            <div className="mb-3">
              <TransformHelper
                nodeType="execute_sql_task"
                expression={String(params.sql || params.query || '')}
                params={params}
                upstreamSchema={upstreamColumns.map((c: any) => ({
                  name: c.name || c,
                  type: c.type || 'string',
                  nullable: c.nullable !== false,
                }))}
                upstreamRowCount={0}
                onAcceptSql={(sql) => updateNodeParams(node.id, { sql })}
              />
            </div>
            <ExecuteSqlTaskConfig params={params} nodeId={node.id} onChange={updateNodeParams} />
          </>
        )}
        {/* ── Generic Source / Destination (connector picker inside config) ── */}
        {stepType === 'source' && (
          <GenericSourceConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {stepType === 'destination' && (
          <GenericDestinationConfig params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} sourceTypes={upstreamSourceTypes} mappingOnly="hide" />
        )}
        {/* Dynamic schema fallback — renders config from backend param_schema for any node type without a hardcoded config above */}
        {!HARDCODED_CONFIGS.has(stepType) && (
          <DynamicConfig stepType={stepType} params={params} nodeId={node.id} onChange={updateNodeParams} columns={upstreamColumns} />
        )}
        {!fullBleed && <DataOutBand d={frameDataOut} />}
      </div>
      </div>
      )}

      </div>{/* End inner white panel */}
      </div>{/* End modal container (gradient outer border) */}

      {/* Right side — connected downstream nodes (centered-modal layout only) */}
      {!dockedPanel && (
      <div className="pointer-events-auto ml-3 flex flex-col gap-1.5 w-[150px] shrink-0">
        {connectedDownstream.length > 0 ? connectedDownstream.map(down => {
          const cond = COND_COLORS[down.condition] || COND_COLORS.completion;
          return (
            <div key={down.id} className="flex flex-col gap-0.5">
              {/* Condition label */}
              <div className="flex items-center gap-1 pl-1">
                <div className={`h-px w-3 ${down.condition === 'success' ? 'bg-green-300' : down.condition === 'failure' ? 'bg-red-300' : 'bg-indigo-300'}`} />
                <span className={`text-[7px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${cond.bg} ${cond.text} ${cond.border} border`}>
                  on {down.condition}
                </span>
              </div>
              <button
                onClick={() => setSelectedNode(down.id)}
                className="flex items-center gap-2 px-3 py-2 bg-white rounded-xl shadow-lg border border-slate-200 hover:border-blue-300 hover:shadow-xl transition-all group w-full"
                title={`→ ${down.label}`}
              >
                <div className="min-w-0 text-right flex-1">
                  <div className="text-xs font-semibold text-slate-600 group-hover:text-blue-600 truncate">{down.label}</div>
                  <div className="text-[8px] text-slate-400 uppercase truncate">{down.stepType.replace(/_/g, ' ')}</div>
                </div>
                <ModuleIcon type={down.stepType} size={18} />
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="text-slate-300 group-hover:text-blue-400 shrink-0">
                  <polyline points="9 18 15 12 9 6" />
                </svg>
              </button>
            </div>
          );
        }) : nextNode ? (
          <button
            onClick={() => setSelectedNode(nextNode.id)}
            className="flex items-center gap-2 px-3 py-2 bg-white rounded-xl shadow-lg border border-slate-200 hover:border-blue-300 hover:shadow-xl transition-all group w-full"
            title={`→ ${(nextNode.data as any)?.label || 'Next'}`}
          >
            <div className="min-w-0 text-right flex-1">
              <div className="text-xs font-semibold text-slate-600 group-hover:text-blue-600 truncate">{(nextNode.data as any)?.label || 'Next'}</div>
              <div className="text-[8px] text-slate-400 uppercase truncate">{((nextNode.data as any)?.stepType || '').replace(/_/g, ' ')}</div>
            </div>
            <ModuleIcon type={(nextNode.data as any)?.stepType} size={18} />
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="text-slate-300 group-hover:text-blue-400 shrink-0">
              <polyline points="9 18 15 12 9 6" />
            </svg>
          </button>
        ) : null}
      </div>
      )}
    </div>{/* End flex center */}
    </>
  );
}

/* ── AI Assist ── */

function AiAssistSection({ stepType, params, nodeId, onChange, expanded: controlledExpanded, setExpanded: controlledSetExpanded }: { stepType: string; params: any; nodeId: string; onChange: (id: string, p: Record<string, any>) => void; expanded?: boolean; setExpanded?: (v: boolean | ((prev: boolean) => boolean)) => void }) {
  const [internalExpanded, setInternalExpanded] = useState(false);
  const expanded = controlledExpanded ?? internalExpanded;
  const setExpanded = controlledSetExpanded ?? setInternalExpanded;
  const [prompt, setPrompt] = useState('');
  const [loading, setLoading] = useState(false);
  const [response, setResponse] = useState<string | null>(null);
  // null = no answer yet; false = the model did NOT run (offline/no provider)
  // so we label the text as a deterministic tip instead of a real AI answer.
  const [aiPowered, setAiPowered] = useState<boolean | null>(null);
  const [appliedParams, setAppliedParams] = useState<Record<string, any> | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const tips = getAiTips(stepType, params);

  const handleAsk = async () => {
    if (!prompt.trim() || loading) return;
    setLoading(true);
    setResponse(null);
    setAppliedParams(null);
    setAiPowered(null);
    try {
      const res = await api.aiAssistNode({ stepType, params, prompt: prompt.trim(), nodeId });
      // Honesty: the backend flags ai_powered=false when no model ran and it
      // returned a deterministic tip. Only treat an explicit false as offline.
      setAiPowered(res.ai_powered !== false);
      if (res.params) {
        setAppliedParams(res.params);
        setResponse(res.message || 'Here are the suggested parameters:');
      } else {
        setResponse(res.message || res.suggestion || 'No suggestions available.');
      }
    } catch {
      // Network/endpoint failure — deterministic tip, clearly marked offline.
      setAiPowered(false);
      setResponse(`For ${stepType.replace(/_/g, ' ')} nodes, configure the required fields above, then use Test Node to preview.`);
    }
    setLoading(false);
  };

  const applyParams = () => {
    if (appliedParams) {
      onChange(nodeId, appliedParams);
      toast.success('AI suggestions applied');
      setAppliedParams(null);
    }
  };

  return (
    <div className="px-4 py-2 border-b border-amber-200/30 bg-gradient-to-r from-pipe-50/40 to-transparent shrink-0">
      <button
        onClick={() => { setExpanded(!expanded); setTimeout(() => inputRef.current?.focus(), 100); }}
        className="flex items-center gap-1.5 text-[12px] font-semibold text-pipe-700 w-full hover:text-pipe-800 transition-colors"
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-pipe-500">
          <path d="M12 2l2.4 7.4H22l-6.2 4.5L18.2 22 12 17.5 5.8 22l2.4-8.1L2 9.4h7.6z" />
        </svg>
        AI Assist
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className={`ml-auto transition-transform ${expanded ? 'rotate-180' : ''}`}>
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>
      {expanded && (
        <div className="mt-2 space-y-2">
          {/* Interactive chat input */}
          <div className="flex gap-1.5">
            <input
              ref={inputRef}
              type="text"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') handleAsk(); }}
              placeholder={`Ask about ${stepType.replace(/_/g, ' ')}... e.g. "connect to sales DB"`}
              className="flex-1 px-2.5 py-1.5 text-xs text-slate-700 bg-white border border-pipe-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 placeholder:text-slate-400"
              disabled={loading}
            />
            <button
              onClick={handleAsk}
              disabled={!prompt.trim() || loading}
              className="px-3 py-1.5 text-xs font-semibold text-white bg-pipe-500 rounded-lg hover:bg-pipe-600 disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
            >
              {loading ? (
                <svg className="animate-spin h-3 w-3" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" /></svg>
              ) : 'Ask'}
            </button>
          </div>

          {/* AI Response */}
          {response && (
            <div className={`p-2.5 rounded-lg border text-xs text-slate-600 ${
              aiPowered === false ? 'bg-amber-50 border-amber-200' : 'bg-pipe-50 border-pipe-100'
            }`}>
              {aiPowered === false && (
                <div className="flex items-center gap-1.5 mb-1.5 text-[11px] font-semibold text-amber-700">
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
                  </svg>
                  <span>Offline tip — no AI provider reachable</span>
                  <button
                    onClick={() => { window.location.hash = '#ai?tab=settings'; }}
                    className="ml-auto text-amber-800 underline underline-offset-2 hover:text-amber-900"
                  >
                    Configure AI →
                  </button>
                </div>
              )}
              <div className="flex items-start gap-1.5">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className={`shrink-0 mt-0.5 ${aiPowered === false ? 'text-amber-500' : 'text-pipe-500'}`}>
                  <path d="M12 2l2.4 7.4H22l-6.2 4.5L18.2 22 12 17.5 5.8 22l2.4-8.1L2 9.4h7.6z" />
                </svg>
                <span>{response}</span>
              </div>
              {appliedParams && (
                <button
                  onClick={applyParams}
                  className="mt-2 px-3 py-1 text-xs font-semibold text-white bg-pipe-500 rounded-md hover:bg-pipe-600 transition-colors"
                >
                  Apply Suggestions
                </button>
              )}
            </div>
          )}

          {/* Quick tips */}
          {tips.length > 0 && (
            <>
              <div className="text-[9px] font-semibold text-slate-400 uppercase tracking-wider pt-1">Quick Actions</div>
              {tips.map((tip, i) => (
                <button
                  key={i}
                  onClick={() => {
                    if (tip.apply) {
                      onChange(nodeId, tip.apply);
                      toast.success('Applied: ' + tip.text.slice(0, 40));
                    }
                  }}
                  className="w-full text-left p-2 rounded-lg bg-pipe-50/50 border border-pipe-100 hover:border-pipe-200 hover:bg-pipe-50 transition-colors group"
                >
                  <div className="text-xs text-slate-600">{tip.text}</div>
                  {tip.apply && (
                    <div className="text-[9px] text-pipe-500 font-medium mt-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                      Click to apply
                    </div>
                  )}
                </button>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function getAiTips(stepType: string, params: any): Array<{ text: string; apply?: Record<string, any> }> {
  switch (stepType) {
    case 'csv_source':
      return [
        { text: 'Set delimiter to ";" for European CSVs', apply: { delimiter: ';' } },
        { text: 'Enable header row detection', apply: { header: true } },
        { text: 'Set encoding to UTF-8 with BOM', apply: { encoding: 'utf-8-sig' } },
      ];
    case 'filter':
      return [
        !params.condition ? { text: 'Add a filter condition like: amount > 100' } : null,
        { text: 'Use AND/OR for complex conditions', apply: { condition: (params.condition || '') + ' AND ' } },
        { text: 'Filter NULL values: column IS NOT NULL', apply: { condition: 'column IS NOT NULL' } },
      ].filter(Boolean) as any;
    case 'deduplicate':
      return [
        { text: 'Keep first occurrence (default)', apply: { keep: 'first' } },
        { text: 'Keep last occurrence', apply: { keep: 'last' } },
        !params.key?.length ? { text: 'Specify a dedup key column like: id, email' } : null,
      ].filter(Boolean) as any;
    case 'aggregate':
      return [
        { text: 'Add SUM aggregation', apply: { aggregations: [...(params.aggregations || []), { column: 'amount', function: 'SUM', alias: 'total' }] } },
        { text: 'Add COUNT aggregation', apply: { aggregations: [...(params.aggregations || []), { column: '*', function: 'COUNT', alias: 'count' }] } },
        { text: 'Add AVG aggregation', apply: { aggregations: [...(params.aggregations || []), { column: 'value', function: 'AVG', alias: 'average' }] } },
      ];
    case 'join':
      return [
        { text: 'Switch to LEFT JOIN (keep all left rows)', apply: { join_type: 'LEFT' } },
        { text: 'Switch to FULL OUTER JOIN', apply: { join_type: 'FULL' } },
        !params.join_key ? { text: 'Specify a join key column like: customer_id' } : null,
      ].filter(Boolean) as any;
    case 'transform':
      return [
        { text: 'Use "SELECT *, col1 + col2 AS total FROM source_table"' },
        { text: 'Add a CASE expression for categorization' },
        { text: 'Use CAST(col AS INTEGER) for type conversion' },
      ];
    case 'sort':
      return [
        { text: 'Sort descending', apply: { order: 'desc' } },
        { text: 'Sort ascending', apply: { order: 'asc' } },
      ];
    case 'db_source':
      return [
        { text: 'Use a parameterized query for security' },
        { text: 'Add LIMIT for testing: SELECT * FROM table LIMIT 100', apply: { query: 'SELECT * FROM table_name LIMIT 100' } },
      ];
    case 'output':
      return [
        { text: 'Switch to Parquet format (best compression)', apply: { format: 'parquet' } },
        { text: 'Switch to CSV format (universal)', apply: { format: 'csv' } },
      ];
    case 'db_sink':
      return [
        { text: 'Use append mode (add to existing)', apply: { mode: 'append' } },
        { text: 'Use overwrite mode (replace table)', apply: { mode: 'overwrite' } },
        { text: 'Use upsert mode (update or insert)', apply: { mode: 'upsert' } },
      ];
    case 'validate':
      return [
        { text: 'Add NOT NULL check', apply: { rules: [...(params.rules || []), { column: '*', rule: 'not_null' }] } },
        { text: 'Add data type check', apply: { rules: [...(params.rules || []), { column: '*', rule: 'type_check' }] } },
        { text: 'Add range check', apply: { rules: [...(params.rules || []), { column: 'value', rule: 'range', min: 0, max: 1000 }] } },
      ];
    case 'sharepoint_source':
    case 'sharepoint_sink':
      return [
        { text: 'Set format to auto-detect from file extension', apply: { format: 'auto' } },
        { text: 'Read CSV file from SharePoint', apply: { format: 'csv', item_path: 'Shared Documents/data.csv' } },
        { text: 'Read Excel file from SharePoint', apply: { format: 'xlsx', item_path: 'Shared Documents/report.xlsx' } },
      ];
    case 'onedrive_source':
    case 'onedrive_sink':
      return [
        { text: 'Set format to auto-detect', apply: { format: 'auto' } },
        { text: 'Read from Documents folder', apply: { item_path: 'Documents/data.csv' } },
      ];
    case 's3_source':
    case 's3_sink':
      return [
        { text: 'Use Parquet format (best for analytics)', apply: { format: 'parquet' } },
        { text: 'Read CSV from S3', apply: { format: 'csv' } },
        { text: 'Enable recursive folder scan', apply: { recursive: true } },
      ];
    case 'rest_api':
    case 'api_source':
      return [
        { text: 'Set GET method', apply: { method: 'GET' } },
        { text: 'Set POST method with JSON body', apply: { method: 'POST', content_type: 'application/json' } },
        { text: 'Add pagination (offset-based)', apply: { pagination: 'offset', page_size: 100 } },
      ];
    case 'file_source':
    case 'file_sink':
      return [
        { text: 'Auto-detect format from extension', apply: { format: 'auto' } },
        { text: 'Set to Parquet format', apply: { format: 'parquet' } },
        { text: 'Enable header row', apply: { header: true } },
      ];
    case 'rename':
      return [
        { text: 'Rename columns using mapping format: old_name → new_name' },
      ];
    case 'typecast':
      return [
        { text: 'Cast to INTEGER', apply: { target_type: 'INTEGER' } },
        { text: 'Cast to VARCHAR', apply: { target_type: 'VARCHAR' } },
        { text: 'Cast to DATE', apply: { target_type: 'DATE' } },
      ];
    case 'derived_column':
      return [
        { text: 'Add a calculated column using SQL expression' },
        // 2026-06-10: apply the canonical `columns` array — the backend
        // executes only params.columns; the old {expression, alias}
        // shape was silently ignored.
        { text: 'Example: price * quantity AS total', apply: { columns: [{ name: 'total', expression: 'price * quantity' }] } },
      ];
    case 'window':
      return [
        { text: 'ROW_NUMBER over partition', apply: { function: 'ROW_NUMBER', partition_by: 'category', order_by: 'date DESC' } },
        { text: 'Running SUM', apply: { function: 'SUM', order_by: 'date' } },
      ];
    case 'sample':
      return [
        { text: 'Sample 10% of rows', apply: { percent: 10 } },
        { text: 'Sample first 1000 rows', apply: { count: 1000, method: 'first' } },
      ];
    default:
      // No quick tips for unrecognized types — the chat input is always available
      return [];
  }
}

/* ── Field Components ── */

/**
 * Auto-derived required marker. When a hardcoded config block can give
 * us its stepType + the field's param name, this helper looks up
 * `validateWorkflow.SOURCE_REQUIREMENTS` (and the one-of groups) and
 * renders the right indicator — single `*` for hard-required, `*†` for
 * one-of-N. Drops the hand-typed `*` problem (139 of them in this file
 * as of 2026-05-19) where the JSX marker and the validator silently
 * drift apart whenever someone adds a node type.
 *
 * OSS-3 (2026-05-19) + FOLLOW-2 (2026-05-19): the helper is exported and
 * the `Field` component accepts the new `stepType` + `fieldName` props.
 * The bulk per-site migration (139 hand-typed `*` markers in this file)
 * is intentionally left as a follow-up sweep because most of the
 * Source / Destination configs use a `k(...)` namespace wrapper that
 * depends on the parent surface (db_source vs db_sink vs copy_data) —
 * each call site needs domain-aware mapping from the namespaced param
 * key back to the canonical fieldName the validator knows. Doing it
 * blindly risks silently dropping existing `*` markers. The
 * `requireStepType` helper below is the migration path: when a config
 * block knows its stepType statically, swap `<Field label="X *">` to
 * `<Field label="X" stepType="..." fieldName="..." />`.
 */
function RequiredMark({ stepType, field }: { stepType: string; field: string }) {
  if (isRequiredFieldFor(stepType, field)) {
    return <span className="text-red-500" title="Required"> *</span>;
  }
  if (isOneOfFieldFor(stepType, field)) {
    return (
      <span className="text-amber-500" title="One of these fields is required">
        {' '}*†
      </span>
    );
  }
  return null;
}

function Field({
  label,
  children,
  stepType,
  fieldName,
}: {
  label: string;
  children: React.ReactNode;
  // Optional pair — when both are provided, the auto-derived RequiredMark
  // is appended to the label. Callers that haven't migrated still work
  // because both default to undefined.
  stepType?: string;
  fieldName?: string;
}) {
  return (
    <div>
      <label className="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1.5">
        {label}
        {stepType && fieldName && <RequiredMark stepType={stepType} field={fieldName} />}
      </label>
      {children}
    </div>
  );
}

// 2026-06-11 — lightweight parameter-insert affordance. The engine already
// resolves ${param.x} in EVERY field (global pre-run pass), so this is purely
// a discoverability/typing aid — no per-node wiring. Shows ONLY when the
// pipeline declares parameters, so simple pipelines stay clutter-free.
function insertAtCursor(
  el: HTMLInputElement | HTMLTextAreaElement | null,
  value: string,
  onChange: (v: string) => void,
  token: string,
) {
  if (el && typeof el.selectionStart === 'number') {
    const s = el.selectionStart;
    const e = el.selectionEnd ?? s;
    onChange(value.slice(0, s) + token + value.slice(e));
    setTimeout(() => {
      try { el.focus(); el.setSelectionRange(s + token.length, s + token.length); } catch { /* noop */ }
    }, 0);
  } else {
    onChange((value || '') + token);
  }
}

function ParamInsert({ onInsert }: { onInsert: (token: string) => void }) {
  const parameters = useWorkflowStore((s) => s.parameters);
  const [open, setOpen] = useState(false);
  if (!parameters || parameters.length === 0) return null;
  return (
    <div className="absolute right-1.5 top-1.5">
      <button
        type="button"
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => setOpen((o) => !o)}
        title="Insert a pipeline parameter"
        className="text-[10px] font-mono leading-none px-1 py-0.5 rounded text-pipe-600 border border-pipe-200 bg-white/90 hover:bg-pipe-50"
      >{'${ }'}</button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute right-0 mt-1 z-20 bg-white border border-slate-200 rounded-lg shadow-lg py-1 min-w-[160px] max-h-52 overflow-auto">
            <div className="px-2 py-1 text-[9px] uppercase tracking-wide text-slate-400">Insert parameter</div>
            {parameters.map((p: any) => (
              <button
                key={p.name}
                type="button"
                onClick={() => { onInsert('${param.' + p.name + '}'); setOpen(false); }}
                className="w-full text-left px-2 py-1 text-xs font-mono text-slate-700 hover:bg-pipe-50"
                title={p.description || ''}
              >{'${param.' + p.name + '}'}</button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function TextInput({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  const ref = useRef<HTMLInputElement>(null);
  return (
    <>
      <div className="relative">
        <input
          ref={ref}
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className="w-full px-3 py-2 text-sm text-slate-800 bg-white border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 focus:border-pipe-400 placeholder:text-slate-400"
        />
        <ParamInsert onInsert={(tok) => insertAtCursor(ref.current, value, onChange, tok)} />
      </div>
      <ExpressionPreview value={value} />
    </>
  );
}

/**
 * Secret-grade input for passwords, tokens, secret keys, API keys, etc.
 * Drop-in replacement for TextInput in any credential field.
 *
 * Behavior:
 *   - type="password" so the value masks in the DOM (no plaintext leak
 *     to browser extensions, console snapshots, screen-share viewers).
 *   - autoComplete="off" + spellCheck={false} so browser password
 *     managers don't auto-suggest unrelated saved values.
 *   - Eye toggle (👁) on the right reveals the value temporarily. Auto-
 *     reverts to masked when the input loses focus, so a glance-to-verify
 *     doesn't accidentally leave secrets visible on the screen.
 *
 * Created 2026-05-18 per P0 review consensus: 13+ credential fields
 * were rendering as plain TextInput, exposing secrets in screenshots,
 * clipboard history, and devtools.
 */
/**
 * P1-7 state-sanitation helper for auth-type switches (2026-05-18).
 *
 * When a user switches auth_type from e.g. `bearer` → `basic`, the
 * stale `bearer_token` param sticks around in the node's JSON payload.
 * On export / re-render / debug, both tokens appear set — making the
 * pipeline output dirty and forcing the executor to guess which one
 * to use. This helper builds a partial-update object that BOTH sets
 * the new auth_type AND nulls out every other auth-related key, so
 * the JSON payload only contains the keys the active auth mode needs.
 *
 * Use as: `onChange={(v) => onChange(nodeId, switchAuthType(v))}`.
 */
function switchAuthType(nextAuthType: string): Record<string, any> {
  const AUTH_KEYS = ['bearer_token', 'username', 'password', 'api_key_header', 'api_key_value', 'auth_token', 'auth_user', 'auth_pass'];
  const patch: Record<string, any> = { auth_type: nextAuthType };
  for (const k of AUTH_KEYS) patch[k] = undefined;
  return patch;
}

/**
 * AuthSection — reusable auth-configuration primitive (P2-A, 2026-05-18).
 *
 * Renders a unified "Auth Type" select + the matching credential fields
 * for whatever type is selected. Powered by `switchAuthType()` for state
 * sanitation (stale credential keys get nulled when auth type changes)
 * and `<SecretInput>` for credential masking.
 *
 * Reads/writes directly on `params.{auth_type, bearer_token, username,
 * password, api_key_header, api_key_value}` for backwards compatibility
 * with the existing backend wire format. A future migration to the
 * canonical `auth.X` namespace (see `utils/authVocabulary.ts`) would
 * swap this primitive to read/write the nested shape — the backend
 * `denormalizeForBackend` shim handles the conversion at save time.
 *
 * Pass `excludeTypes` to drop options (e.g. `['oauth2']` until OAuth
 * wiring ships — but defaults already match the post-P0-2 shape:
 * none / bearer / basic / api_key).
 *
 * Single source of truth for auth UX across api_source, api_sink,
 * http_request, etc. Replaces ~30 lines of copy-pasted conditional
 * fields per node.
 */
function AuthSection({
  params, onChange,
  excludeTypes = [],
}: {
  params: Record<string, any>;
  onChange: (patch: Record<string, any>) => void;
  excludeTypes?: string[];
}) {
  const allTypes = ['none', 'bearer', 'basic', 'api_key'];
  const types = allTypes.filter((t) => !excludeTypes.includes(t));
  const authType = params.auth_type || 'none';
  return (
    <>
      <Field label="Auth Type">
        <Select
          value={authType}
          onChange={(v) => onChange(switchAuthType(v))}
          options={types}
        />
      </Field>
      {authType === 'bearer' && (
        <Field label="Bearer Token">
          <SecretInput
            value={params.bearer_token || ''}
            onChange={(v) => onChange({ bearer_token: v })}
            placeholder="eyJ..."
          />
        </Field>
      )}
      {authType === 'basic' && (
        <>
          <Field label="Username">
            <TextInput
              value={params.username || ''}
              onChange={(v) => onChange({ username: v })}
              placeholder="user"
            />
          </Field>
          <Field label="Password">
            <SecretInput
              value={params.password || ''}
              onChange={(v) => onChange({ password: v })}
              placeholder="••••••••"
            />
          </Field>
        </>
      )}
      {authType === 'api_key' && (
        <>
          <Field label="API Key Header">
            <TextInput
              value={params.api_key_header || 'X-API-Key'}
              onChange={(v) => onChange({ api_key_header: v })}
              placeholder="X-API-Key"
            />
          </Field>
          <Field label="API Key Value">
            <SecretInput
              value={params.api_key_value || ''}
              onChange={(v) => onChange({ api_key_value: v })}
              placeholder="your-api-key"
            />
          </Field>
        </>
      )}
    </>
  );
}

/**
 * Reusable key/value repeater (P2-D-1, 2026-05-18).
 *
 * Drop-in replacement for every "edit this JSON object as text" textarea
 * scattered across the config panel — HTTP headers, request namespaces,
 * cloud storage options, key-value variables, etc. The user adds rows
 * (Key | Value) one at a time, and the component round-trips them to a
 * plain `Record<string, string>` for the underlying param.
 *
 * Pre-existing UX: users typed `{"X-Auth-Token": "abc"}` into a textarea
 * and hoped they hadn't broken JSON. The repeater turns that into a
 * structured form — no quote/brace/comma trivia for them to get wrong.
 *
 * Pass `secretValues={true}` when the value column holds credentials —
 * uses `<SecretInput>` for the value field so tokens mask on screen.
 */
function KeyValueRepeater({
  value, onChange, keyPlaceholder = 'Key', valuePlaceholder = 'Value', secretValues = false, addLabel = '+ Add',
}: {
  value: Record<string, string> | undefined;
  onChange: (next: Record<string, string>) => void;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
  secretValues?: boolean;
  addLabel?: string;
}) {
  const entries = Object.entries(value || {});
  const setKey = (idx: number, newKey: string) => {
    const next: Record<string, string> = {};
    entries.forEach(([k, v], i) => { if (i === idx) next[newKey] = v; else next[k] = v; });
    onChange(next);
  };
  const setValue = (idx: number, newValue: string) => {
    const next: Record<string, string> = {};
    entries.forEach(([k, v], i) => { next[k] = (i === idx ? newValue : v); });
    onChange(next);
  };
  const removeRow = (idx: number) => {
    const next: Record<string, string> = {};
    entries.forEach(([k, v], i) => { if (i !== idx) next[k] = v; });
    onChange(next);
  };
  const addRow = () => onChange({ ...(value || {}), '': '' });
  return (
    <div className="flex flex-col gap-1">
      {entries.map(([k, v], idx) => (
        <div key={idx} className="flex items-center gap-1.5">
          <input
            type="text"
            value={k}
            onChange={(e) => setKey(idx, e.target.value)}
            placeholder={keyPlaceholder}
            className="flex-1 min-w-0 px-2 py-1 text-sm font-mono border border-slate-200 rounded-md"
          />
          <span className="text-slate-300 text-xs">=</span>
          <div className="flex-1 min-w-0">
            {secretValues ? (
              <SecretInput value={v} onChange={(nv) => setValue(idx, nv)} placeholder={valuePlaceholder} />
            ) : (
              <input
                type="text"
                value={v}
                onChange={(e) => setValue(idx, e.target.value)}
                placeholder={valuePlaceholder}
                className="w-full px-2 py-1 text-sm font-mono border border-slate-200 rounded-md"
              />
            )}
          </div>
          <button
            type="button"
            onClick={() => removeRow(idx)}
            className="text-slate-400 hover:text-red-600 px-1"
            title="Remove row"
          >
            ✕
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={addRow}
        className="self-start mt-1 px-2 py-1 text-xs border border-slate-300 hover:bg-slate-50 rounded-md text-slate-700"
      >
        {addLabel}
      </button>
    </div>
  );
}

function SecretInput({
  value, onChange, placeholder,
}: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  const [revealed, setRevealed] = useState(false);
  return (
    <div className="relative">
      <input
        type={revealed ? 'text' : 'password'}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onBlur={() => setRevealed(false)}
        placeholder={placeholder}
        autoComplete="off"
        spellCheck={false}
        className="w-full px-3 py-2 pr-9 text-sm text-slate-800 bg-white border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 focus:border-pipe-400 placeholder:text-slate-400 font-mono"
      />
      <button
        type="button"
        onMouseDown={(e) => { e.preventDefault(); setRevealed((v) => !v); }}
        title={revealed ? 'Hide value' : 'Show value (auto-hides on blur)'}
        className="absolute right-2 top-1/2 -translate-y-1/2 w-6 h-6 flex items-center justify-center text-slate-400 hover:text-slate-700 transition-colors"
      >
        {revealed ? (
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
            <line x1="1" y1="1" x2="23" y2="23" />
          </svg>
        ) : (
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
            <circle cx="12" cy="12" r="3" />
          </svg>
        )}
      </button>
    </div>
  );
}

function TextArea({ value, onChange, placeholder, rows = 3 }: { value: string; onChange: (v: string) => void; placeholder?: string; rows?: number }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  return (
    <>
      <div className="relative">
        <textarea
          ref={ref}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          rows={rows}
          className="w-full px-3 py-2 text-sm text-slate-800 bg-white border border-slate-300 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-pipe-300 focus:border-pipe-400 placeholder:text-slate-400 resize-y"
        />
        <ParamInsert onInsert={(tok) => insertAtCursor(ref.current, value, onChange, tok)} />
      </div>
      <ExpressionPreview value={value} />
    </>
  );
}

function Select({ value, onChange, options, labels, placeholder }: { value: string; onChange: (v: string) => void; options: string[]; labels?: Record<string, string>; placeholder?: string }) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-full px-3 py-2 text-sm text-slate-800 bg-white border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 focus:border-pipe-400"
    >
      {placeholder && <option value="">{placeholder}</option>}
      {options.map((opt) => (
        <option key={opt} value={opt}>{labels?.[opt] || opt}</option>
      ))}
    </select>
  );
}

function Toggle({ value, onChange, label }: { value: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <label className="flex items-center gap-2.5 cursor-pointer">
      <div
        onClick={() => onChange(!value)}
        className={`w-10 h-5 rounded-full transition-colors ${value ? 'bg-pipe-600' : 'bg-slate-300'} relative shrink-0`}
      >
        <div className={`w-4 h-4 bg-white rounded-full absolute top-0.5 transition-transform shadow-sm ${value ? 'translate-x-5' : 'translate-x-0.5'}`} />
      </div>
      <span className="text-sm text-slate-700">{label}</span>
    </label>
  );
}

/* ── Connection type → family map. Field sets in the Copy Data Source tab
   change based on the picked connection's family so users only see the
   options relevant to that system. ── */

type ConnFamily =
  | 'sql'
  | 'warehouse'
  | 'nosql'
  | 'search'
  | 'storage'
  | 'streaming'
  | 'api'
  | 'saas'
  | 'vector'
  | 'other';

const FAMILY_BY_TYPE: Record<string, ConnFamily> = {
  postgresql: 'sql', mysql: 'sql', mssql: 'sql', oracle: 'sql', sqlite: 'sql', mariadb: 'sql', cockroachdb: 'sql',
  snowflake: 'warehouse', bigquery: 'warehouse', redshift: 'warehouse', databricks: 'warehouse', synapse: 'warehouse', clickhouse: 'warehouse', trino: 'warehouse',
  mongodb: 'nosql', cassandra: 'nosql', couchbase: 'nosql', dynamodb: 'nosql', cosmosdb: 'nosql', neo4j: 'nosql', firebase: 'nosql',
  elasticsearch: 'search', opensearch: 'search', redis: 'search',
  s3: 'storage', azure_blob: 'storage', adls_gen2: 'storage', gcs: 'storage', minio: 'storage',
  sharepoint: 'storage', onedrive: 'storage', gdrive: 'storage', dropbox: 'storage', ftp: 'storage', gsheet: 'storage',
  kafka: 'streaming', rabbitmq: 'streaming', pulsar: 'streaming', eventhub: 'streaming', kinesis: 'streaming',
  rest_api: 'api', graphql: 'api', odata: 'api', oracle_api: 'api',
  salesforce: 'saas', dynamics365: 'saas', sap: 'saas', servicenow: 'saas',
  hubspot: 'saas', zendesk: 'saas', jira: 'saas', workday: 'saas', netsuite: 'saas',
  pinecone: 'vector', weaviate: 'vector', qdrant: 'vector', chroma: 'vector', pgvector: 'vector',
};

function getConnectionFamily(type: string | null | undefined): ConnFamily {
  if (!type) return 'other';
  return FAMILY_BY_TYPE[type] || 'other';
}

/** Look up the type for a saved connection ID. Loads connections lazily and
 *  caches them for the lifetime of this hook instance. */
function useConnectionType(connectionId: string | undefined | null): { type: string | null; loading: boolean } {
  const [type, setType] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!connectionId) { setType(null); return; }
    let cancelled = false;
    setLoading(true);
    api.listConnections().then((list: any[]) => {
      if (cancelled) return;
      const c = list.find((x) => x.id === connectionId);
      setType(c?.type || null);
      setLoading(false);
    }).catch(() => { if (!cancelled) { setType(null); setLoading(false); } });
    return () => { cancelled = true; };
  }, [connectionId]);
  return { type, loading };
}

/* ── Family-aware Source / Sink field renderers.
   Used by Copy Data's Source + Sink tabs AND by the standalone Source /
   Destination nodes, so all four surfaces show the same family-specific
   field set whenever the picked connection's type changes. The `prefix`
   prop lets Copy Data scope its params under `source_` / `sink_` while
   the standalone nodes use unprefixed keys.
── */

interface FamilyFieldsProps {
  family: ConnFamily;
  params: Record<string, any>;
  set: (patch: Record<string, any>) => void;
  prefix?: string;
}

function FamilyAwareSourceFields({ family, params, set, prefix = '' }: FamilyFieldsProps) {
  const k = (key: string) => `${prefix}${key}`;
  return (
    <>
      {(family === 'sql' || family === 'warehouse') && (
        <>
          <Field label="Source Mode">
            <Select
              value={params[k('kind')] || 'table'}
              onChange={(v) => set({ [k('kind')]: v })}
              options={['table', 'query', 'stored_procedure']}
              labels={{ table: 'Table', query: 'Query', stored_procedure: 'Stored Procedure' }}
            />
          </Field>
          {(params[k('kind')] || 'table') === 'table' && (
            <>
              <div className="flex justify-end -mb-1">
                <CatalogPicker
                  connectionId={params[k('connection_id')] || params.connection_id}
                  kinds={['table', 'view']}
                  label="Browse tables"
                  compact
                  onPick={(item) => set({ [k('schema')]: item.parent, [k('table')]: item.name })}
                />
              </div>
              <div className="grid grid-cols-2 gap-2">
                <Field label="Schema">
                  <TextInput value={params[k('schema')] || ''} onChange={(v) => set({ [k('schema')]: v })} placeholder="dbo" />
                </Field>
                <Field label="Table *">
                  <TextInput value={params[k('table')] || ''} onChange={(v) => set({ [k('table')]: v })} placeholder="orders" />
                </Field>
              </div>
              <Field label="Filter (WHERE clause)">
                <TextInput value={params[k('filter')] || ''} onChange={(v) => set({ [k('filter')]: v })} placeholder="updated_at > '2025-01-01'" />
              </Field>
              <Field label="Partition Column (optional)">
                <TextInput value={params.partition_column || ''} onChange={(v) => set({ partition_column: v })} placeholder="id" />
              </Field>
            </>
          )}
          {params[k('kind')] === 'query' && (
            <Field label="SQL Query *">
              <textarea
                value={params[k('query')] || ''}
                onChange={(e) => set({ [k('query')]: e.target.value })}
                placeholder="SELECT * FROM orders WHERE created_at >= CURRENT_DATE - 1"
                rows={5}
                className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
              />
            </Field>
          )}
          {params[k('kind')] === 'stored_procedure' && (
            <>
              <div className="flex justify-end -mb-1">
                <CatalogPicker
                  connectionId={params[k('connection_id')] || params.connection_id}
                  kinds={['procedure', 'function']}
                  label="Browse procedures"
                  compact
                  onPick={(item) => set({ stored_procedure: item.parent ? `${item.parent}.${item.name}` : item.name })}
                />
              </div>
              <Field label="Procedure Name *">
                <TextInput value={params.stored_procedure || ''} onChange={(v) => set({ stored_procedure: v })} placeholder="dbo.GetOrders" />
              </Field>
              <Field label="Parameters (JSON)">
                <textarea
                  value={params.proc_params || ''}
                  onChange={(e) => set({ proc_params: e.target.value })}
                  placeholder='{ "@StartDate": "2025-01-01" }'
                  rows={3}
                  className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
                />
              </Field>
            </>
          )}
          <div className="grid grid-cols-2 gap-2">
            <Field label="Query Timeout (s)">
              <TextInput value={String(params.query_timeout || '')} onChange={(v) => set({ query_timeout: v })} placeholder="30" />
            </Field>
            <Field label="Isolation Level">
              <Select
                value={params.isolation_level || 'read_committed'}
                onChange={(v) => set({ isolation_level: v })}
                options={['read_uncommitted', 'read_committed', 'repeatable_read', 'serializable', 'snapshot']}
              />
            </Field>
          </div>
          {family === 'warehouse' && (
            <Field label="Bulk Extract">
              <Select value={params.bulk_extract || 'auto'} onChange={(v) => set({ bulk_extract: v })} options={['auto', 'on', 'off']} />
            </Field>
          )}
        </>
      )}

      {family === 'nosql' && (
        <>
          <div className="flex justify-end -mb-1">
            <CatalogPicker
              connectionId={params[k('connection_id')] || params.connection_id}
              kinds={['collection']}
              label="Browse collections"
              compact
              onPick={(item) => set({ [k('database')]: item.parent, [k('collection')]: item.name })}
            />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Database">
              <TextInput value={params[k('database')] || ''} onChange={(v) => set({ [k('database')]: v })} placeholder="appdb" />
            </Field>
            <Field label="Collection / Table *">
              <TextInput value={params[k('collection')] || ''} onChange={(v) => set({ [k('collection')]: v })} placeholder="orders" />
            </Field>
          </div>
          <Field label="Filter (JSON)">
            <textarea
              value={params[k('filter_json')] || ''}
              onChange={(e) => set({ [k('filter_json')]: e.target.value })}
              placeholder='{ "status": "active" }'
              rows={3}
              className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
            />
          </Field>
          <Field label="Projection (comma-separated fields)">
            <TextInput value={params[k('projection')] || ''} onChange={(v) => set({ [k('projection')]: v })} placeholder="_id, name, total" />
          </Field>
        </>
      )}

      {family === 'search' && (
        <>
          <Field label="Index / Key Pattern *">
            <TextInput value={params[k('index')] || ''} onChange={(v) => set({ [k('index')]: v })} placeholder="logs-*" />
          </Field>
          <Field label="Query (DSL / pattern)">
            <textarea
              value={params[k('query')] || ''}
              onChange={(e) => set({ [k('query')]: e.target.value })}
              placeholder='{ "query": { "match_all": {} } }'
              rows={5}
              className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
            />
          </Field>
          <Field label="Batch Size">
            <TextInput value={String(params.batch_size || '')} onChange={(v) => set({ batch_size: v })} placeholder="1000" />
          </Field>
        </>
      )}

      {family === 'storage' && (
        <>
          <Field label="Path / Prefix *">
            <TextInput value={params[k('path')] || ''} onChange={(v) => set({ [k('path')]: v })} placeholder="bucket/folder/*.parquet" />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="File Format">
              <Select
                value={params.file_format || 'parquet'}
                onChange={(v) => set({ file_format: v })}
                options={['parquet', 'csv', 'json', 'jsonl', 'avro', 'orc', 'xlsx']}
              />
            </Field>
            <Field label="Compression">
              <Select
                value={params.compression || 'auto'}
                onChange={(v) => set({ compression: v })}
                options={['auto', 'none', 'gzip', 'snappy', 'zstd', 'bzip2']}
              />
            </Field>
          </div>
          <Field label="Recursive Scan">
            <Select
              value={params.recursive ? 'true' : 'false'}
              onChange={(v) => set({ recursive: v === 'true' })}
              options={['false', 'true']}
              labels={{ false: 'No', true: 'Yes' }}
            />
          </Field>
          {(params.file_format === 'csv' || !params.file_format) && (
            <div className="grid grid-cols-2 gap-2">
              <Field label="Delimiter">
                <TextInput value={params.csv_delimiter || ''} onChange={(v) => set({ csv_delimiter: v })} placeholder="," />
              </Field>
              <Field label="Header">
                <Select
                  value={params.csv_header ? 'true' : 'false'}
                  onChange={(v) => set({ csv_header: v === 'true' })}
                  options={['true', 'false']}
                  labels={{ true: 'Yes', false: 'No' }}
                />
              </Field>
            </div>
          )}
        </>
      )}

      {family === 'streaming' && (
        <>
          <Field label="Topic / Stream *">
            <TextInput value={params[k('topic')] || ''} onChange={(v) => set({ [k('topic')]: v })} placeholder="orders.events" />
          </Field>
          <Field label="Consumer Group / Subscription">
            <TextInput value={params.consumer_group || ''} onChange={(v) => set({ consumer_group: v })} placeholder="fpulse-1" />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Starting Offset">
              <Select value={params.start_offset || 'latest'} onChange={(v) => set({ start_offset: v })} options={['latest', 'earliest', 'timestamp']} />
            </Field>
            <Field label="Max Records">
              <TextInput value={String(params.max_records || '')} onChange={(v) => set({ max_records: v })} placeholder="10000" />
            </Field>
          </div>
          {params.start_offset === 'timestamp' && (
            <Field label="Start Timestamp (ISO)">
              <TextInput value={params.start_timestamp || ''} onChange={(v) => set({ start_timestamp: v })} placeholder="2025-01-01T00:00:00Z" />
            </Field>
          )}
        </>
      )}

      {family === 'api' && (
        <>
          <Field label="Endpoint Path *">
            <TextInput value={params.endpoint_path || ''} onChange={(v) => set({ endpoint_path: v })} placeholder="/v1/orders" />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Method">
              <Select value={params.method || 'GET'} onChange={(v) => set({ method: v })} options={['GET', 'POST']} />
            </Field>
            <Field label="Pagination">
              <Select value={params.pagination || 'none'} onChange={(v) => set({ pagination: v })} options={['none', 'offset', 'page', 'cursor', 'link_header']} />
            </Field>
          </div>
          <Field label="Headers (JSON)">
            <textarea
              value={params.headers_json || ''}
              onChange={(e) => set({ headers_json: e.target.value })}
              placeholder='{ "Accept": "application/json" }'
              rows={3}
              className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
            />
          </Field>
          {params.method === 'POST' && (
            <Field label="Body (JSON)">
              <textarea
                value={params.body_json || ''}
                onChange={(e) => set({ body_json: e.target.value })}
                placeholder="{}"
                rows={3}
                className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
              />
            </Field>
          )}
          <Field label="Response Path (JSONPath to records)">
            <TextInput value={params.records_path || ''} onChange={(v) => set({ records_path: v })} placeholder="$.data.items" />
          </Field>
        </>
      )}

      {family === 'saas' && (
        <>
          <Field label="Object / Entity *">
            <TextInput value={params[k('entity')] || ''} onChange={(v) => set({ [k('entity')]: v })} placeholder="Account" />
          </Field>
          <Field label="Query Mode">
            <Select value={params.saas_query_mode || 'all'} onChange={(v) => set({ saas_query_mode: v })} options={['all', 'incremental', 'custom']} />
          </Field>
          {params.saas_query_mode === 'incremental' && (
            <div className="grid grid-cols-2 gap-2">
              <Field label="Watermark Field">
                <TextInput value={params.watermark_field || ''} onChange={(v) => set({ watermark_field: v })} placeholder="LastModifiedDate" />
              </Field>
              <Field label="Since (ISO)">
                <TextInput value={params.watermark_value || ''} onChange={(v) => set({ watermark_value: v })} placeholder="2025-01-01T00:00:00Z" />
              </Field>
            </div>
          )}
          {params.saas_query_mode === 'custom' && (
            <Field label="Custom Query *">
              <textarea
                value={params[k('query')] || ''}
                onChange={(e) => set({ [k('query')]: e.target.value })}
                placeholder="SELECT Id, Name FROM Account WHERE IsActive = TRUE"
                rows={4}
                className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
              />
            </Field>
          )}
          <Field label="Fields (comma-separated, blank = all)">
            <TextInput value={params[k('fields')] || ''} onChange={(v) => set({ [k('fields')]: v })} placeholder="Id, Name, CreatedDate" />
          </Field>
        </>
      )}

      {family === 'vector' && (
        <>
          <Field label="Index / Collection *">
            <TextInput value={params[k('index')] || ''} onChange={(v) => set({ [k('index')]: v })} placeholder="prod-embeddings" />
          </Field>
          <Field label="Mode">
            <Select
              value={params.vector_mode || 'scan'}
              onChange={(v) => set({ vector_mode: v })}
              options={['scan', 'similarity']}
              labels={{ scan: 'Full scan', similarity: 'Similarity search' }}
            />
          </Field>
          {params.vector_mode === 'similarity' && (
            <>
              <Field label="Query Vector / Text">
                <textarea
                  value={params.query_vector || ''}
                  onChange={(e) => set({ query_vector: e.target.value })}
                  placeholder="[0.12, -0.34, ...]   or text to embed"
                  rows={3}
                  className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
                />
              </Field>
              <Field label="Top K">
                <TextInput value={String(params.top_k || '')} onChange={(v) => set({ top_k: v })} placeholder="10" />
              </Field>
            </>
          )}
          <Field label="Metadata Filter (JSON)">
            <textarea
              value={params.metadata_filter || ''}
              onChange={(e) => set({ metadata_filter: e.target.value })}
              placeholder='{ "tenant": "acme" }'
              rows={3}
              className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
            />
          </Field>
        </>
      )}

      {family === 'other' && (
        <>
          <Field label="Source Type">
            <Select value={params[k('kind')] || 'table'} onChange={(v) => set({ [k('kind')]: v })} options={['table', 'query']} />
          </Field>
          {(params[k('kind')] || 'table') === 'table' ? (
            <>
              <Field label="Source Table *">
                <TextInput value={params[k('table')] || ''} onChange={(v) => set({ [k('table')]: v })} placeholder="schema.table_name" />
              </Field>
              <Field label="Filter (WHERE clause)">
                <TextInput value={params[k('filter')] || ''} onChange={(v) => set({ [k('filter')]: v })} placeholder="updated_at > '2025-01-01'" />
              </Field>
            </>
          ) : (
            <Field label="Source Query *">
              <textarea
                value={params[k('query')] || ''}
                onChange={(e) => set({ [k('query')]: e.target.value })}
                placeholder="SELECT * FROM orders WHERE created_at >= CURRENT_DATE - 1"
                rows={4}
                className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
              />
            </Field>
          )}
        </>
      )}
    </>
  );
}

interface FamilySinkFieldsProps extends FamilyFieldsProps {
  columns?: string[];
  keyColumns?: string[];
  toggleKey?: (col: string) => void;
}

function FamilyAwareSinkFields({ family, params, set, prefix = '', columns = [], keyColumns = [], toggleKey }: FamilySinkFieldsProps) {
  const k = (key: string) => `${prefix}${key}`;
  return (
    <>
      {(family === 'sql' || family === 'warehouse') && (
        <>
          <div className="flex justify-end -mb-1">
            <CatalogPicker
              connectionId={params[k('connection_id')] || params.connection_id}
              kinds={['table', 'view']}
              label="Browse tables"
              compact
              onPick={(item) => set({ [k('schema')]: item.parent, [k('table')]: item.name })}
            />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Schema">
              <TextInput value={params[k('schema')] || ''} onChange={(v) => set({ [k('schema')]: v })} placeholder="dbo" />
            </Field>
            <Field label="Table *">
              <TextInput value={params[k('table')] || ''} onChange={(v) => set({ [k('table')]: v })} placeholder="orders_loaded" />
            </Field>
          </div>
          <Field label="Table Action">
            <Select
              value={params.table_action || 'none'}
              onChange={(v) => set({ table_action: v })}
              options={['none', 'autocreate', 'recreate', 'truncate']}
              labels={{ none: 'None (table must exist)', autocreate: 'Create if missing', recreate: 'Drop & recreate', truncate: 'Truncate before load' }}
            />
          </Field>
          <Field label="Write Behavior">
            <Select value={params.write_behavior || 'append'} onChange={(v) => set({ write_behavior: v })} options={['append', 'overwrite', 'upsert', 'merge']} />
          </Field>
          {(params.write_behavior === 'upsert' || params.write_behavior === 'merge') && toggleKey && (
            <Field label="Key Columns *">
              {columns.length > 0 ? (
                <div className="flex flex-wrap gap-1">
                  {columns.map((c) => (
                    <button
                      key={c}
                      onClick={() => toggleKey(c)}
                      className={`text-xs px-2 py-1 rounded-lg border transition-colors ${
                        keyColumns.includes(c) ? 'bg-pipe-100 text-pipe-700 border-pipe-300' : 'bg-white text-slate-400 border-slate-200'
                      }`}
                    >
                      {keyColumns.includes(c) && '✓ '}{c}
                    </button>
                  ))}
                </div>
              ) : (
                <TextInput
                  value={keyColumns.join(', ')}
                  onChange={(v) => set({ key_columns: v.split(',').map((s) => s.trim()).filter(Boolean) })}
                  placeholder="id, tenant_id"
                />
              )}
            </Field>
          )}
          {family === 'warehouse' && (
            <Field label="Bulk Load">
              <Select value={params.bulk_load || 'auto'} onChange={(v) => set({ bulk_load: v })} options={['auto', 'on', 'off']} />
            </Field>
          )}
          <Field label="Pre-Copy Script">
            <textarea
              value={params.pre_copy_script || ''}
              onChange={(e) => set({ pre_copy_script: e.target.value })}
              placeholder="TRUNCATE TABLE staging.target"
              rows={3}
              className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
            />
          </Field>
          <Field label="Post-Copy Script">
            <textarea
              value={params.post_copy_script || ''}
              onChange={(e) => set({ post_copy_script: e.target.value })}
              placeholder="UPDATE STATISTICS dbo.orders"
              rows={3}
              className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
            />
          </Field>
          <Field label="Batch Size">
            <TextInput value={String(params.batch_size ?? 1000)} onChange={(v) => set({ batch_size: parseInt(v) || 1000 })} placeholder="1000" />
          </Field>
        </>
      )}

      {family === 'nosql' && (
        <>
          <div className="flex justify-end -mb-1">
            <CatalogPicker
              connectionId={params[k('connection_id')] || params.connection_id}
              kinds={['collection']}
              label="Browse collections"
              compact
              onPick={(item) => set({ [k('database')]: item.parent, [k('collection')]: item.name })}
            />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Database">
              <TextInput value={params[k('database')] || ''} onChange={(v) => set({ [k('database')]: v })} placeholder="appdb" />
            </Field>
            <Field label="Collection / Table *">
              <TextInput value={params[k('collection')] || ''} onChange={(v) => set({ [k('collection')]: v })} placeholder="orders" />
            </Field>
          </div>
          <Field label="Write Mode">
            <Select value={params.write_behavior || 'insert'} onChange={(v) => set({ write_behavior: v })} options={['insert', 'upsert', 'replace']} />
          </Field>
          {(params.write_behavior === 'upsert' || params.write_behavior === 'replace') && (
            <Field label="Key Field *">
              <TextInput value={params.key_field || ''} onChange={(v) => set({ key_field: v })} placeholder="_id" />
            </Field>
          )}
          <Field label="Ordered Writes">
            <Select
              value={params.ordered ? 'true' : 'false'}
              onChange={(v) => set({ ordered: v === 'true' })}
              options={['true', 'false']}
              labels={{ true: 'Yes (stop on first error)', false: 'No (continue on errors)' }}
            />
          </Field>
          <Field label="Batch Size">
            <TextInput value={String(params.batch_size ?? 500)} onChange={(v) => set({ batch_size: parseInt(v) || 500 })} placeholder="500" />
          </Field>
        </>
      )}

      {family === 'search' && (
        <>
          <Field label="Index *">
            <TextInput value={params[k('index')] || ''} onChange={(v) => set({ [k('index')]: v })} placeholder="orders-2025" />
          </Field>
          <Field label="Document ID Field">
            <TextInput value={params.id_field || ''} onChange={(v) => set({ id_field: v })} placeholder="order_id" />
          </Field>
          <Field label="Write Mode">
            <Select value={params.write_behavior || 'index'} onChange={(v) => set({ write_behavior: v })} options={['index', 'create', 'update', 'upsert']} />
          </Field>
          <Field label="Refresh Policy">
            <Select
              value={params.refresh_policy || 'false'}
              onChange={(v) => set({ refresh_policy: v })}
              options={['false', 'wait_for', 'true']}
              labels={{ false: 'No refresh', wait_for: 'Wait for next refresh', true: 'Refresh immediately' }}
            />
          </Field>
          <Field label="Bulk Batch Size">
            <TextInput value={String(params.batch_size ?? 1000)} onChange={(v) => set({ batch_size: parseInt(v) || 1000 })} placeholder="1000" />
          </Field>
        </>
      )}

      {family === 'storage' && (
        <>
          <Field label="Output Path / Prefix *">
            <TextInput value={params[k('path')] || ''} onChange={(v) => set({ [k('path')]: v })} placeholder="bucket/loaded/orders/" />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="File Format">
              <Select
                value={params.file_format || 'parquet'}
                onChange={(v) => set({ file_format: v })}
                options={['parquet', 'csv', 'json', 'jsonl', 'avro', 'orc', 'xlsx']}
              />
            </Field>
            <Field label="Compression">
              <Select value={params.compression || 'auto'} onChange={(v) => set({ compression: v })} options={['auto', 'none', 'gzip', 'snappy', 'zstd', 'bzip2']} />
            </Field>
          </div>
          <Field label="Write Mode">
            <Select value={params.write_behavior || 'append'} onChange={(v) => set({ write_behavior: v })} options={['append', 'overwrite', 'error_if_exists']} />
          </Field>
          <Field label="Partition By (comma-separated)">
            <TextInput value={params.partition_by || ''} onChange={(v) => set({ partition_by: v })} placeholder="year, month" />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Max File Size (MB)">
              <TextInput value={String(params.max_file_size_mb || '')} onChange={(v) => set({ max_file_size_mb: parseInt(v) || '' })} placeholder="128" />
            </Field>
            <Field label="Filename Pattern">
              <TextInput value={params.filename_pattern || ''} onChange={(v) => set({ filename_pattern: v })} placeholder="part-{i}.parquet" />
            </Field>
          </div>
          {(params.file_format === 'csv' || !params.file_format) && (
            <div className="grid grid-cols-2 gap-2">
              <Field label="Delimiter">
                <TextInput value={params.csv_delimiter || ''} onChange={(v) => set({ csv_delimiter: v })} placeholder="," />
              </Field>
              <Field label="Header">
                <Select
                  value={params.csv_header ? 'true' : 'false'}
                  onChange={(v) => set({ csv_header: v === 'true' })}
                  options={['true', 'false']}
                  labels={{ true: 'Yes', false: 'No' }}
                />
              </Field>
            </div>
          )}
        </>
      )}

      {family === 'streaming' && (
        <>
          <Field label="Topic / Stream *">
            <TextInput value={params[k('topic')] || ''} onChange={(v) => set({ [k('topic')]: v })} placeholder="orders.events" />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Key Field">
              <TextInput value={params.key_field || ''} onChange={(v) => set({ key_field: v })} placeholder="order_id" />
            </Field>
            <Field label="Value Format">
              <Select value={params.value_format || 'json'} onChange={(v) => set({ value_format: v })} options={['json', 'avro', 'protobuf', 'string']} />
            </Field>
          </div>
          <Field label="Acks">
            <Select
              value={params.acks || 'all'}
              onChange={(v) => set({ acks: v })}
              options={['0', '1', 'all']}
              labels={{ '0': '0 — fire and forget', '1': '1 — leader only', all: 'all — full ISR' }}
            />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Compression">
              <Select value={params.compression || 'none'} onChange={(v) => set({ compression: v })} options={['none', 'gzip', 'snappy', 'lz4', 'zstd']} />
            </Field>
            <Field label="Batch Size">
              <TextInput value={String(params.batch_size ?? 500)} onChange={(v) => set({ batch_size: parseInt(v) || 500 })} placeholder="500" />
            </Field>
          </div>
        </>
      )}

      {family === 'api' && (
        <>
          <Field label="Endpoint Path *">
            <TextInput value={params.endpoint_path || ''} onChange={(v) => set({ endpoint_path: v })} placeholder="/v1/orders" />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Method">
              <Select value={params.method || 'POST'} onChange={(v) => set({ method: v })} options={['POST', 'PUT', 'PATCH']} />
            </Field>
            <Field label="Send Mode">
              <Select
                value={params.send_mode || 'one_per_record'}
                onChange={(v) => set({ send_mode: v })}
                options={['one_per_record', 'batched']}
                labels={{ one_per_record: 'One request per record', batched: 'Batched requests' }}
              />
            </Field>
          </div>
          <Field label="Headers (JSON)">
            <textarea
              value={params.headers_json || ''}
              onChange={(e) => set({ headers_json: e.target.value })}
              placeholder='{ "Content-Type": "application/json" }'
              rows={3}
              className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
            />
          </Field>
          <Field label="Body Template">
            <textarea
              value={params.body_template || ''}
              onChange={(e) => set({ body_template: e.target.value })}
              placeholder='{ "id": "{{ row.id }}", "amount": {{ row.amount }} }'
              rows={4}
              className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
            />
          </Field>
          {params.send_mode === 'batched' && (
            <Field label="Batch Size">
              <TextInput value={String(params.batch_size ?? 100)} onChange={(v) => set({ batch_size: parseInt(v) || 100 })} placeholder="100" />
            </Field>
          )}
        </>
      )}

      {family === 'saas' && (
        <>
          <Field label="Object / Entity *">
            <TextInput value={params[k('entity')] || ''} onChange={(v) => set({ [k('entity')]: v })} placeholder="Account" />
          </Field>
          <Field label="Write Mode">
            <Select value={params.write_behavior || 'insert'} onChange={(v) => set({ write_behavior: v })} options={['insert', 'update', 'upsert', 'delete']} />
          </Field>
          {(params.write_behavior === 'upsert' || params.write_behavior === 'update' || params.write_behavior === 'delete') && (
            <Field label="External ID Field *">
              <TextInput value={params.external_id_field || ''} onChange={(v) => set({ external_id_field: v })} placeholder="External_Id__c" />
            </Field>
          )}
          {/* P1-8 (2026-05-18): Field Mapping Mode selector removed from
              Sink tab to prevent silent overwrites of the Mapping tab's
              setting. The Mapping tab is now the single source of truth
              for `mapping_mode`. (The two used to coexist and write the
              same param key; toggling one silently clobbered the other.) */}
          <Field label="Batch Size">
            <TextInput value={String(params.batch_size ?? 200)} onChange={(v) => set({ batch_size: parseInt(v) || 200 })} placeholder="200" />
          </Field>
        </>
      )}

      {family === 'vector' && (
        <>
          <Field label="Index / Collection *">
            <TextInput value={params[k('index')] || ''} onChange={(v) => set({ [k('index')]: v })} placeholder="prod-embeddings" />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="ID Field *">
              <TextInput value={params.id_field || ''} onChange={(v) => set({ id_field: v })} placeholder="doc_id" />
            </Field>
            <Field label="Vector Field *">
              <TextInput value={params.vector_field || ''} onChange={(v) => set({ vector_field: v })} placeholder="embedding" />
            </Field>
          </div>
          <Field label="Metadata Fields (comma-separated)">
            <TextInput value={params.metadata_fields || ''} onChange={(v) => set({ metadata_fields: v })} placeholder="title, source, tenant" />
          </Field>
          <Field label="Write Mode">
            <Select value={params.write_behavior || 'upsert'} onChange={(v) => set({ write_behavior: v })} options={['insert', 'upsert', 'replace']} />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Namespace">
              <TextInput value={params.namespace || ''} onChange={(v) => set({ namespace: v })} placeholder="(optional)" />
            </Field>
            <Field label="Batch Size">
              <TextInput value={String(params.batch_size ?? 100)} onChange={(v) => set({ batch_size: parseInt(v) || 100 })} placeholder="100" />
            </Field>
          </div>
        </>
      )}

      {family === 'other' && (
        <>
          <Field label="Sink Table *">
            <TextInput value={params[k('table')] || ''} onChange={(v) => set({ [k('table')]: v })} placeholder="schema.target_table" />
          </Field>
          <Field label="Table Action">
            <Select value={params.table_action || 'none'} onChange={(v) => set({ table_action: v })} options={['none', 'autocreate', 'recreate', 'truncate']} />
          </Field>
          <Field label="Write Behavior">
            <Select value={params.write_behavior || 'append'} onChange={(v) => set({ write_behavior: v })} options={['append', 'overwrite', 'upsert', 'merge']} />
          </Field>
          <Field label="Batch Size">
            <TextInput value={String(params.batch_size ?? 1000)} onChange={(v) => set({ batch_size: parseInt(v) || 1000 })} placeholder="1000" />
          </Field>
        </>
      )}
    </>
  );
}

/* ── Connection Picker: select a saved connection ── */

/**
 * Set true inside the generic Source/Destination sub-config dispatch so the
 * embedded per-connector ConnectionPicker self-suppresses — the generic
 * config's top ConnectorConnectionPicker already owns the connection. Kills
 * the dual connection-control (two pickers writing the same connection_id)
 * across all ~19 embedded sub-configs in ONE place.
 */
const EmbeddedConnectionContext = createContext(false);

function ConnectionPicker({
  value, onChange, filter, requireCapability,
}: {
  value: string;
  onChange: (v: string) => void;
  filter?: string[];
  /** When set, hides connections whose capabilities array doesn't
   *  include this role. Source nodes pass 'read', sink nodes 'write'.
   *  Empty / missing capabilities array on the connection = legacy row,
   *  treated as both (always shown). */
  requireCapability?: 'read' | 'write';
}) {
  const embedded = useContext(EmbeddedConnectionContext);
  const [connections, setConnections] = useState<any[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [testStatus, setTestStatus] = useState<'idle' | 'testing' | 'ok' | 'fail'>('idle');
  // AbortController lets the user cancel a hanging test (e.g. a JDBC
  // driver stuck on DNS lookup with no timeout). Clicking the button
  // while it's in 'testing' state calls abort() instead of starting a
  // second test. Cleared on unmount so a closed modal doesn't leak
  // the in-flight fetch.
  const testAbortRef = useRef<AbortController | null>(null);
  useEffect(() => () => testAbortRef.current?.abort(), []);

  const applyFilters = (list: any[]) => {
    let next = filter ? list.filter((c: any) => filter.includes(c.type)) : list;
    if (requireCapability) {
      next = next.filter((c: any) => {
        const caps = c.capabilities;
        if (!caps || caps.length === 0) return true;
        return caps.includes(requireCapability);
      });
    }
    return next;
  };

  useEffect(() => {
    if (!loaded) {
      api.listConnections().then((list) => {
        setConnections(applyFilters(list));
        setLoaded(true);
      }).catch(() => setLoaded(true));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded, filter, requireCapability]);

  useEffect(() => { setTestStatus('idle'); }, [value]);

  const refresh = () => {
    api.listConnections().then((list) => {
      setConnections(applyFilters(list));
    }).catch(() => {});
  };

  const cancelTest = () => {
    // Abort the in-flight test. The catch in testConnection treats the
    // resulting DOMException as just-cancel (not 'fail') so the button
    // returns to the neutral idle state.
    testAbortRef.current?.abort();
    testAbortRef.current = null;
    setTestStatus('idle');
  };

  const testConnection = async () => {
    if (!value) return;
    // If a previous test is still running, treat the click as a cancel.
    if (testStatus === 'testing') {
      cancelTest();
      return;
    }
    const controller = new AbortController();
    testAbortRef.current = controller;
    setTestStatus('testing');
    try {
      const result = await api.testConnection(value, controller.signal);
      // Only commit the result if THIS controller is still the active
      // one — a stale response from a cancelled test must not overwrite
      // the user's current state.
      if (testAbortRef.current !== controller) return;
      setTestStatus(result.success === true || result.status === 'ok' ? 'ok' : 'fail');
    } catch (err: any) {
      if (controller.signal.aborted) {
        // User cancelled — already reset to 'idle' by cancelTest().
        return;
      }
      if (testAbortRef.current !== controller) return;
      setTestStatus('fail');
    } finally {
      if (testAbortRef.current === controller) testAbortRef.current = null;
    }
  };

  const openConnectionsPage = () => {
    try { sessionStorage.setItem('fpulse_return_to', window.location.hash); } catch {}
    navigateTo('connections');
  };

  // Embedded under a generic Source/Destination — the top connection picker
  // already owns the connection; don't render a second, redundant one.
  if (embedded) return null;

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1.5">
        <select
          value={value || ''}
          onChange={(e) => {
            if (e.target.value === '__new__') {
              openConnectionsPage();
              return;
            }
            onChange(e.target.value);
          }}
          className={`flex-1 px-2.5 py-1.5 text-xs text-slate-700 bg-white border rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 transition-colors ${
            testStatus === 'ok'
              ? 'border-emerald-400 ring-1 ring-emerald-200'
              : testStatus === 'fail'
                ? 'border-red-400 ring-1 ring-red-200'
                : 'border-slate-200'
          }`}
        >
          <option value="">— No connection (DuckDB in-memory) —</option>
          {connections.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name} ({c.type})
            </option>
          ))}
          <option value="__new__">+ New connection…</option>
        </select>
        <button
          type="button"
          onClick={testConnection}
          disabled={!value}
          title={
            testStatus === 'testing'
              ? 'Test running — click to stop'
              : 'Test selected connection'
          }
          className={`shrink-0 h-7 px-2.5 rounded-lg border text-xs font-bold flex items-center gap-1.5 transition-all disabled:opacity-30 ${
            testStatus === 'ok'
              ? 'bg-emerald-500 border-emerald-600 text-white shadow-sm'
              : testStatus === 'fail'
                ? 'bg-red-500 border-red-600 text-white shadow-sm'
                : testStatus === 'testing'
                  // Testing state doubles as a stop button — pair the spinner
                  // with a red-tone hover so the user discovers it's clickable.
                  ? 'bg-amber-50 border-amber-300 text-amber-700 hover:bg-red-50 hover:border-red-300 hover:text-red-700 group'
                  : 'border-slate-200 bg-white text-slate-500 hover:text-emerald-600 hover:border-emerald-300 hover:bg-emerald-50'
          }`}
        >
          {testStatus === 'testing' ? (
            <>
              {/* Spinner on idle hover, stop-square on hover so the
                  affordance for clicking is visible. */}
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="animate-spin group-hover:hidden"><circle cx="12" cy="12" r="10" strokeDasharray="32" strokeDashoffset="12" /></svg>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" stroke="none" className="hidden group-hover:block"><rect x="6" y="6" width="12" height="12" rx="1" /></svg>
            </>
          ) : testStatus === 'ok' ? (
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="20 6 9 17 4 12" /></svg>
          ) : testStatus === 'fail' ? (
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
          ) : (
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" /></svg>
          )}
          {testStatus === 'ok' ? 'Connected' : testStatus === 'fail' ? 'Failed' : testStatus === 'testing' ? (
            <>
              <span className="group-hover:hidden">Testing…</span>
              <span className="hidden group-hover:inline">Stop</span>
            </>
          ) : 'Test'}
        </button>
        <button
          type="button"
          onClick={refresh}
          title="Refresh connections list"
          className="shrink-0 w-7 h-7 rounded-lg border border-slate-200 bg-white text-slate-400 hover:text-pipe-600 hover:border-pipe-300 flex items-center justify-center transition-colors"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="23 4 23 10 17 10" />
            <polyline points="1 20 1 14 7 14" />
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
          </svg>
        </button>
      </div>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={openConnectionsPage}
          className="text-xs text-pipe-600 hover:text-pipe-800 hover:underline"
        >
          Manage connections →
        </button>
        {testStatus === 'ok' && (
          <span className="text-xs font-semibold text-emerald-600 flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
            Connection is live
          </span>
        )}
        {testStatus === 'fail' && (
          <span className="text-xs font-semibold text-red-500 flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-red-500" />
            Connection failed
          </span>
        )}
      </div>
    </div>
  );
}

/* ── Column Picker: clickable chips from upstream columns ── */

function ColumnPicker({ columns, onInsert }: { columns: string[]; onInsert: (col: string) => void }) {
  if (!columns.length) return null;
  return (
    <div className="mb-3">
      <div className="text-[9px] font-semibold text-pipe-600 uppercase tracking-wider mb-1.5">
        Click to insert column
      </div>
      <div className="flex flex-wrap gap-1">
        {columns.map((col) => (
          <button
            key={col}
            onClick={() => onInsert(col)}
            className="text-[9px] px-1.5 py-0.5 bg-pipe-50 text-pipe-600 rounded border border-pipe-200 font-mono hover:bg-pipe-100 hover:border-pipe-300 transition-colors cursor-pointer active:scale-95"
          >
            {col}
          </button>
        ))}
      </div>
    </div>
  );
}

/* ── Column Mapper: source → destination visual mapping ── */

interface DestinationColumn {
  name: string;
  type?: string;
}

// P5 (2026-05-21): module-level cache of cast-policy dropdown options.
// Populated by the first ColumnMapper that needs it; subsequent mounts
// reuse the value to keep the Mapping tab snappy.
let _castPolicyCache: Array<{ value: string; label: string }> = [];

/** Per-row cast-safety badge. Falls back to a plain arrow when either
 *  side's type isn't known. Mirror of the backend ``classify_cast``
 *  tier mapping; the runtime always re-classifies authoritatively at
 *  execute time so this is a UX hint, not a contract. */
function CastGlyph({ cast }: { cast: ReturnType<typeof classifyCastUI> }) {
  if (!cast) {
    return <span className="text-xs text-slate-400 select-none">→</span>;
  }
  const meta = TIER_META[cast.tier];
  // Tier-specific color so the glyph reads as info / warn / error at a
  // glance. Tooltip carries the human reason.
  const colorByTone: Record<string, string> = {
    ok: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    warn: 'bg-amber-50 text-amber-700 border-amber-200',
    error: 'bg-rose-50 text-rose-700 border-rose-200',
  };
  const cls = colorByTone[meta.tone] || colorByTone.ok;
  const tooltip = cast.reason
    ? `${meta.label} — ${cast.reason}`
    : meta.label;
  return (
    <span
      className={`inline-flex items-center justify-center w-5 h-5 rounded-full text-xs font-bold border ${cls}`}
      title={tooltip}
    >
      {meta.glyph}
    </span>
  );
}

function ColumnMapper({
  columns,
  mappings,
  onChange,
  sourceTypes,
  destinationColumns,
  onImportSchema,
  importing,
  skipped,
  onSkippedChange,
  castPolicy,
  onCastPolicyChange,
}: {
  columns: string[];
  mappings: Record<string, string>;
  onChange: (mappings: Record<string, string>) => void;
  /** Source-column → DuckDB/source data type (optional — when available, surfaced as a type chip). */
  sourceTypes?: Record<string, string>;
  /** Imported destination-table columns (name + type). When provided, the
   *  destination cell renders a typeahead list so users can pick from
   *  real existing columns, and matched columns show their DB type. */
  destinationColumns?: DestinationColumn[];
  /** Triggers the parent to fetch the destination table's columns and pass them back via destinationColumns. */
  onImportSchema?: () => Promise<void> | void;
  /** Visual indicator that the import is in flight. */
  importing?: boolean;
  /** Source columns the user has chosen NOT to write to the destination. */
  skipped?: string[];
  onSkippedChange?: (next: string[]) => void;
  /** P5 (2026-05-21): sink-side cast policy. Drives whether the runtime
   *  blocks or proceeds on each source→target cast. Value space matches
   *  backend ``CastPolicy`` enum (safe / coerce / truncate / strict / learn). */
  castPolicy?: string;
  onCastPolicyChange?: (next: string) => void;
}) {
  if (!columns.length) {
    return (
      <div className="text-xs text-slate-400 italic">
        Run the upstream node first to see available columns.
      </div>
    );
  }

  const skippedSet = new Set(skipped || []);
  const destByName = new Map<string, DestinationColumn>(
    (destinationColumns || []).map((c) => [c.name.toLowerCase(), c]),
  );
  const hasImported = !!(destinationColumns && destinationColumns.length > 0);

  // Per-row override: switch from dropdown to free-text input so users
  // can map to a column name that's not in the imported schema (e.g.
  // they're about to CREATE the destination table). Tracked locally so
  // the choice persists for the current open-panel session.
  const [customRows, setCustomRows] = useState<Set<string>>(new Set());

  // P5 (2026-05-21): pull policy options from the backend so the
  // dropdown matches the canonical type system's enum values. Module-
  // level cache keeps the request to one fetch per page load even when
  // several ColumnMappers render simultaneously.
  const [policyOptions, setPolicyOptions] = useState<Array<{ value: string; label: string }>>(_castPolicyCache);
  useEffect(() => {
    if (_castPolicyCache.length > 0 || !onCastPolicyChange) return;
    let alive = true;
    fetch('/api/types/policies')
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!alive || !data?.cast_policy?.options) return;
        _castPolicyCache = data.cast_policy.options;
        setPolicyOptions(_castPolicyCache);
      })
      .catch(() => { /* network error — dropdown stays hidden */ });
    return () => { alive = false; };
  }, [onCastPolicyChange]);
  const toggleCustomMode = (col: string) => {
    setCustomRows((prev) => {
      const next = new Set(prev);
      if (next.has(col)) next.delete(col);
      else next.add(col);
      return next;
    });
  };

  const toggleSkip = (col: string) => {
    if (!onSkippedChange) return;
    const next = new Set(skippedSet);
    if (next.has(col)) next.delete(col);
    else next.add(col);
    onSkippedChange(Array.from(next));
  };

  const skipAll = () => onSkippedChange && onSkippedChange([...columns]);
  const restoreAll = () => onSkippedChange && onSkippedChange([]);

  return (
    <div className="space-y-2">
      {/* P5: cast policy selector. Tells the runtime how strict to be
          on source→target mismatches. Only renders when the parent
          supplies onCastPolicyChange (sink configs). */}
      {onCastPolicyChange && policyOptions.length > 0 && (
        <div className="flex items-center gap-2 px-2 py-1.5 rounded-md border border-slate-200 bg-slate-50">
          <span className="text-xs font-medium text-slate-600 whitespace-nowrap">Cast policy:</span>
          <select
            value={castPolicy || 'coerce'}
            onChange={(e) => onCastPolicyChange(e.target.value)}
            className="flex-1 px-2 py-1 text-xs bg-white border border-slate-200 rounded focus:outline-none focus:ring-1 focus:ring-pipe-300"
          >
            {policyOptions.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>
      )}

      {/* Toolbar — import + bulk actions + summary */}
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="text-xs text-slate-500">
          {columns.length} source columns
          {skippedSet.size > 0 && (
            <> · <span className="text-amber-700 font-medium">{skippedSet.size} skipped</span></>
          )}
          {hasImported && (
            <> · <span className="text-emerald-700 font-medium">{destinationColumns!.length} destination columns imported</span></>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          {onSkippedChange && skippedSet.size < columns.length && (
            <button
              type="button"
              onClick={skipAll}
              title="Mark every source column as skipped — nothing will be written"
              className="inline-flex items-center gap-1 px-2 py-1 text-[11px] font-medium rounded-md border border-rose-200 bg-white text-rose-600 hover:bg-rose-50"
            >
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
              </svg>
              Skip all
            </button>
          )}
          {onSkippedChange && skippedSet.size > 0 && (
            <button
              type="button"
              onClick={restoreAll}
              title="Restore every skipped column to the write"
              className="inline-flex items-center gap-1 px-2 py-1 text-[11px] font-medium rounded-md border border-emerald-200 bg-white text-emerald-700 hover:bg-emerald-50"
            >
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M1 4v6h6" /><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
              </svg>
              Restore all
            </button>
          )}
          {onImportSchema && (
            <button
              type="button"
              onClick={() => { void onImportSchema(); }}
              disabled={importing}
              title="Fetch the destination table's columns and their data types"
              className="inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-semibold rounded-md border border-pipe-300 bg-pipe-50 text-pipe-700 hover:bg-pipe-100 disabled:opacity-50"
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="17 8 12 3 7 8" />
                <line x1="12" y1="3" x2="12" y2="15" />
              </svg>
              {importing ? 'Importing…' : 'Import destination schema'}
            </button>
          )}
        </div>
      </div>

      {/* Column headers */}
      <div className="flex gap-1 text-[9px] font-semibold text-slate-400 uppercase tracking-wider px-1">
        <span className="flex-1">Source</span>
        <span className="w-4" />
        <span className="flex-1">Destination</span>
        <span className="w-6" />
      </div>

      {columns.map((col) => {
        const isSkipped = skippedSet.has(col);
        const destName = mappings[col] || col;
        const matchedDest = destByName.get(destName.toLowerCase());
        const srcType = sourceTypes?.[col];
        const destInputId = `colmap-${col}`;
        const listId = destinationColumns && destinationColumns.length > 0 ? `${destInputId}-list` : undefined;
        // P5: classify the source→target cast for this row.
        // Returns null when either side is unknown — fall back to a plain arrow.
        const cast = !isSkipped ? classifyCastUI(srcType, matchedDest?.type) : null;
        return (
          <div
            key={col}
            className={`flex items-center gap-1 ${isSkipped ? 'opacity-40' : ''}`}
          >
            {/* Source cell — name + optional type chip */}
            <div className="flex-1 flex items-center gap-1 text-xs font-mono bg-slate-50 px-2 py-1 rounded min-w-0">
              <span className={`truncate ${isSkipped ? 'line-through text-slate-400' : 'text-slate-700'}`}>{col}</span>
              {srcType && (
                <span className="shrink-0 text-[9px] font-sans font-semibold uppercase tracking-wider text-violet-600 bg-violet-50 border border-violet-200 px-1 rounded">
                  {srcType.toLowerCase()}
                </span>
              )}
            </div>

            {/* P5: cast-safety glyph replaces the plain arrow when both
                types are known. Tooltip carries the reason so operators
                see exactly why a cast is flagged. */}
            <CastGlyph cast={cast} />


            {/* Destination cell — dropdown when schema imported, free
                text input otherwise. The pencil toggle lets users break
                out of the dropdown to type a custom name (e.g. mapping
                to a column the destination table doesn't have yet). */}
            <div className="flex-1 flex items-center gap-1 min-w-0">
              {hasImported && !customRows.has(col) ? (
                <select
                  id={destInputId}
                  value={destByName.has(destName.toLowerCase()) ? destName : '__unmapped__'}
                  onChange={(e) => {
                    const v = e.target.value;
                    if (v === '__unmapped__') {
                      // 'Pick a column…' sentinel — clear any prior mapping
                      const next = { ...mappings };
                      delete next[col];
                      onChange(next);
                    } else {
                      onChange({ ...mappings, [col]: v });
                    }
                  }}
                  disabled={isSkipped}
                  className="flex-1 px-2 py-1 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded focus:outline-none focus:ring-1 focus:ring-pipe-300 disabled:bg-slate-50 min-w-0"
                >
                  <option value="__unmapped__">— pick destination column —</option>
                  {(destinationColumns || []).map((d) => (
                    <option key={d.name} value={d.name}>
                      {d.name}{d.type ? `  (${d.type.toLowerCase()})` : ''}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id={destInputId}
                  value={destName}
                  onChange={(e) => onChange({ ...mappings, [col]: e.target.value })}
                  disabled={isSkipped}
                  className="flex-1 px-2 py-1 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded focus:outline-none focus:ring-1 focus:ring-pipe-300 disabled:bg-slate-50 min-w-0"
                />
              )}
              {matchedDest?.type && !isSkipped && (
                <span className="shrink-0 text-[9px] font-sans font-semibold uppercase tracking-wider text-emerald-700 bg-emerald-50 border border-emerald-200 px-1 rounded">
                  {matchedDest.type.toLowerCase()}
                </span>
              )}
              {hasImported && !isSkipped && (
                <button
                  type="button"
                  onClick={() => toggleCustomMode(col)}
                  title={customRows.has(col) ? 'Switch back to dropdown picker' : 'Type a custom destination name (not in the imported schema)'}
                  className="shrink-0 w-5 h-5 rounded flex items-center justify-center text-slate-400 hover:text-slate-700 hover:bg-slate-100"
                >
                  {customRows.has(col) ? (
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <polyline points="6 9 12 15 18 9" />
                    </svg>
                  ) : (
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
                    </svg>
                  )}
                </button>
              )}
            </div>

            {/* Skip / restore button */}
            {onSkippedChange && (
              <button
                type="button"
                onClick={() => toggleSkip(col)}
                title={isSkipped ? 'Restore this column to the write' : 'Skip this column when writing'}
                className={`shrink-0 w-6 h-6 rounded flex items-center justify-center transition-colors ${
                  isSkipped
                    ? 'text-emerald-600 hover:bg-emerald-50'
                    : 'text-slate-400 hover:text-rose-600 hover:bg-rose-50'
                }`}
              >
                {isSkipped ? (
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M1 4v6h6" /><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
                  </svg>
                ) : (
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                    <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                  </svg>
                )}
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ── Volume Tier Badge ── */
// Shows file size + volume tier (green/amber/orange/red) when a source file is selected.
// Fetches from /api/workflows/source-info. Includes scale-up hint for large files.

const TIER_COLORS: Record<string, string> = {
  green: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  amber: 'bg-amber-50 text-amber-700 border-amber-200',
  orange: 'bg-orange-50 text-orange-700 border-orange-200',
  red: 'bg-red-50 text-red-700 border-red-200',
};

function VolumeTierBadge({ filePath }: { filePath: string }) {
  const [info, setInfo] = useState<any>(null);

  useEffect(() => {
    if (!filePath) { setInfo(null); return; }
    api.getSourceInfo(filePath).then(setInfo).catch(() => setInfo(null));
  }, [filePath]);

  if (!info || !info.tier) return null;

  const cls = TIER_COLORS[info.tier_color] || TIER_COLORS.green;

  return (
    <div className={`px-2.5 py-2 rounded-lg border text-xs space-y-1 ${cls}`}>
      <div className="flex items-center justify-between">
        <span className="font-semibold">{info.size_label}</span>
        <span className="px-1.5 py-0.5 rounded text-xs font-medium bg-white/60">{info.tier_label}</span>
      </div>
      {info.warning && <p className="text-xs opacity-80">{info.warning}</p>}
      {info.scale_up_hint && (
        <p className="text-xs font-medium mt-1 flex items-center gap-1">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M7 17l9.2-9.2M17 17V7H7"/></svg>
          {info.scale_up_hint}
        </p>
      )}
    </div>
  );
}

/* ── Node-Specific Configs ── */

interface ConfigProps {
  params: Record<string, any>;
  nodeId: string;
  onChange: (nodeId: string, params: Record<string, any>) => void;
  columns?: string[];
  upstreamNodes?: Array<{ id: string; label: string; stepType: string; file?: string }>;
  allAncestors?: Array<{ id: string; label: string; stepType: string; file?: string; columns: string[]; depth: number }>;
  /** Column-mapping tab control for sink/destination configs:
   *  - 'show'  → render ONLY the source→destination mapping widget
   *  - 'hide'  → render everything EXCEPT the mapping widget
   *  - undefined → render everything (legacy single-tab behaviour) */
  mappingOnly?: 'show' | 'hide';
  /** Upstream source-column → DuckDB type (VARCHAR, INTEGER, …). Surfaced
   *  by ColumnMapper as a type chip on each source row. Optional. */
  sourceTypes?: Record<string, string>;
  /** When a sink config is EMBEDDED inside GenericDestinationConfig, the
   *  generic Destination already owns the connection via its top-level
   *  ConnectorConnectionPicker — so the embedded sink must NOT render its own
   *  duplicate connection dropdown. Standalone sinks (db_sink, etc.) leave
   *  this undefined and keep their picker. (2026-06-15 dual-control fix.) */
  hideConnection?: boolean;
}

// Whether a given node renders a dedicated "Mapping" tab on ConfigPanel.
// Scoped to nodes that surface a source→destination ColumnMapper today:
// the two named sinks, plus the generic Destination node when its
// connector_type is a database or warehouse (those dispatch to
// DbSinkConfig / WarehouseSinkConfig internally).
function nodeHasMappingTab(stepType: string, params: Record<string, any>): boolean {
  if (stepType === 'db_sink' || stepType === 'warehouse_sink') return true;
  if (stepType === 'destination') {
    const ct = params?.connector_type;
    return ct === 'database' || ct === 'warehouse';
  }
  return false;
}

function CsvSourceConfig({ params, nodeId, onChange }: ConfigProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  // 2026-05-25 — listing source flipped from the legacy `/api/files`
  // (which scanned the install's `samples/` dir and missed every
  // user-uploaded Storage object) to `/api/storage/files`. Now a file
  // uploaded via the Storage page → Files tab appears here on the
  // next config-panel render. Sample-folder files still appear as a
  // fallback below so existing pipelines that referenced them keep
  // working.
  const [files, setFiles] = useState<Array<{ name: string; size: number; path?: string }>>([]);
  const [uploading, setUploading] = useState(false);

  const refresh = () => {
    Promise.all([
      api.listStorageFiles().catch(() => ({ objects: [] as any[] })),
      api.listFiles().catch(() => [] as any[]),
    ]).then(([storage, legacy]) => {
      const fromStorage = (storage.objects || []).map((o: any) => ({
        name: o.path,                 // backend resolves uploads/<ws>/file.csv when the source runs
        size: o.size_bytes,
        path: o.path,
      }));
      const fromLegacy = (legacy as any[]).map((f: any) => ({ name: f.name, size: f.size }));
      // Dedupe by name: Storage rows take precedence (have richer metadata).
      const seen = new Set(fromStorage.map((f) => f.name));
      const merged = [...fromStorage, ...fromLegacy.filter((f) => !seen.has(f.name))];
      setFiles(merged);
    });
  };

  useEffect(() => { refresh(); }, []);

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      // Pass the previous file_path so the backend cleans it up after a
      // successful upload — keeps the data dir from accumulating orphans
      // when a user swaps a node's source file.
      const previous = (params.file_path as string | undefined) || undefined;
      const result = await api.uploadFile(file, { replaces: previous });
      onChange(nodeId, { file_path: result.filename });
      refresh();
    } catch (err: any) {
      console.error('Upload error:', err);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  return (
    <>
      <Field label="Data File *">
        <div className="flex gap-1.5 mb-1.5">
          <select
            value={params.file_path || ''}
            onChange={(e) => onChange(nodeId, { file_path: e.target.value })}
            className="flex-1 px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 bg-white"
          >
            <option value="">Select a file...</option>
            {files.map((f) => (
              <option key={f.name} value={f.name}>
                {f.name} ({(f.size / 1024).toFixed(1)} KB)
              </option>
            ))}
          </select>
        </div>
        <input
          ref={fileRef}
          type="file"
          accept=".csv,.tsv,.json,.parquet,.txt"
          onChange={handleUpload}
          className="hidden"
        />
        <button
          onClick={() => fileRef.current?.click()}
          disabled={uploading}
          className="w-full px-2.5 py-1.5 text-xs border border-dashed border-slate-300 rounded-lg text-slate-500 hover:border-pulse-400 hover:text-pipe-600 hover:bg-pulse-50 transition-colors disabled:opacity-50"
        >
          {uploading ? 'Uploading...' : '+ Upload New File'}
        </button>
        {params.file_path && <VolumeTierBadge filePath={params.file_path} />}
      </Field>
      <Field label="Delimiter">
        <Select
          value={params.delimiter || ','}
          onChange={(v) => onChange(nodeId, { delimiter: v })}
          options={[',', ';', '|', '\t']}
        />
      </Field>
      <Field label="Header">
        <Toggle
          value={params.header !== false}
          onChange={(v) => onChange(nodeId, { header: v })}
          label="First row is header"
        />
      </Field>
    </>
  );
}

// Shared catalog cache for the Schema / Table dropdowns. Keyed by
// connectionId so two nodes pointing at the same connection don't
// duplicate the fetch. `version` is bumped by the refresh button so
// the hook re-reads from the network without invalidating other
// callers' subscriptions.
const _catalogCache = new Map<string, { items: Array<{ name: string; kind: string; parent: string }>; ts: number }>();
const _catalogVersion = new Map<string, number>();
const _catalogSubs = new Set<() => void>();

function useConnectionCatalog(connectionId: string | undefined | null) {
  const [version, setVersion] = useState(() => connectionId ? (_catalogVersion.get(connectionId) || 0) : 0);
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<Array<{ name: string; kind: string; parent: string }>>(() => {
    if (!connectionId) return [];
    return _catalogCache.get(connectionId)?.items || [];
  });

  // Subscribe to cache invalidations so two components viewing the
  // same connection share refresh ticks.
  useEffect(() => {
    const onTick = () => {
      if (!connectionId) return;
      setVersion(_catalogVersion.get(connectionId) || 0);
      setItems(_catalogCache.get(connectionId)?.items || []);
    };
    _catalogSubs.add(onTick);
    return () => { _catalogSubs.delete(onTick); };
  }, [connectionId]);

  // Fetch when connection changes or version bumps; cached responses
  // less than 60s old are reused silently.
  useEffect(() => {
    if (!connectionId) { setItems([]); return; }
    const cached = _catalogCache.get(connectionId);
    if (cached && Date.now() - cached.ts < 60_000 && version === (_catalogVersion.get(connectionId) || 0)) {
      setItems(cached.items);
      return;
    }
    let cancelled = false;
    setLoading(true);
    api.getConnectionCatalog(connectionId)
      .then((res: any) => {
        if (cancelled) return;
        const next = Array.isArray(res?.items) ? res.items : [];
        _catalogCache.set(connectionId, { items: next, ts: Date.now() });
        setItems(next);
      })
      .catch(() => { if (!cancelled) setItems([]); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [connectionId, version]);

  const refresh = () => {
    if (!connectionId) return;
    _catalogVersion.set(connectionId, (_catalogVersion.get(connectionId) || 0) + 1);
    _catalogCache.delete(connectionId);
    _catalogSubs.forEach((fn) => fn());
  };

  return { items, loading, refresh };
}

interface CatalogDatalistInputProps {
  connectionId: string | undefined | null;
  /** 'schema' shows distinct parent values; 'table' shows item names
   *  filtered by `schema` (when set) and by `kinds`. */
  field: 'schema' | 'table';
  /** Only matters when field='table'. Defaults to ['table','view']. */
  kinds?: string[];
  /** When field='table', filter suggestions to this schema. */
  schema?: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}

function CatalogDatalistInput({
  connectionId, field, kinds = ['table', 'view'], schema, value, onChange, placeholder,
}: CatalogDatalistInputProps) {
  const { items, loading, refresh } = useConnectionCatalog(connectionId);

  // Stable id per (connectionId, field, schema) so two inputs on screen
  // don't share a datalist by accident.
  const listId = `cat-${field}-${connectionId || 'none'}-${schema || ''}`;

  const options = useMemo(() => {
    if (field === 'schema') {
      const set = new Set<string>();
      for (const it of items) {
        if (kinds.length > 0 && !kinds.includes(it.kind)) continue;
        if (it.parent) set.add(it.parent);
      }
      return Array.from(set).sort();
    }
    // tables: optionally scoped to schema
    return items
      .filter((i) => (kinds.length === 0 || kinds.includes(i.kind)))
      .filter((i) => (!schema || i.parent === schema))
      .map((i) => i.name)
      .sort();
  }, [items, field, kinds, schema]);

  return (
    <div className="flex gap-1.5">
      <input
        type="text"
        list={listId}
        value={value || ''}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="flex-1 px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 focus:border-transparent"
      />
      <datalist id={listId}>
        {options.map((o) => <option key={o} value={o} />)}
      </datalist>
      <button
        type="button"
        onClick={refresh}
        disabled={!connectionId || loading}
        title={connectionId ? `Refresh ${field === 'schema' ? 'schemas' : 'tables'}` : 'Pick a connection first'}
        className="shrink-0 w-8 h-[30px] rounded-lg flex items-center justify-center text-slate-500 hover:text-slate-700 hover:bg-slate-100 border border-slate-200 disabled:opacity-40 disabled:cursor-not-allowed"
      >
        <svg
          width="13" height="13" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
          className={loading ? 'animate-spin' : ''}
        >
          <polyline points="23 4 23 10 17 10" /><polyline points="1 20 1 14 7 14" />
          <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
        </svg>
      </button>
    </div>
  );
}

// ─── Managed Table Source / Sink ────────────────────────────────────────
// 2026-05-25 — picker UI for the workspace's managed Parquet tables
// (the rows from Storage → Managed Tables). Previously the node fell
// through to DynamicConfig and users had to type `schema.name` from
// memory; now schema + table are cascading dropdowns sourced from
// `/api/storage/tables`. Sinks add a Mode picker and a Merge-keys
// chip-input that only appears when mode = merge.
function LocalTableConfig({
  params, nodeId, onChange, isSink = false,
}: {
  params: any;
  nodeId: string;
  onChange: (nodeId: string, patch: Record<string, any>) => void;
  isSink?: boolean;
}) {
  type T = { id: string; schema_name: string; name: string; row_count: number | null; column_count: number | null; size_bytes: number };
  const [tables, setTables] = useState<T[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = () => {
    setLoading(true);
    api.listStorageTables()
      .then((res) => setTables(res.tables || []))
      .catch(() => setTables([]))
      .finally(() => setLoading(false));
  };
  useEffect(() => { refresh(); }, []);

  const schemas = Array.from(new Set(tables.map((t) => t.schema_name))).sort();
  const currentSchema = (params.schema_name as string) || 'default';
  const tablesInSchema = tables.filter((t) => t.schema_name === currentSchema);
  const currentTable = (params.table_name as string) || '';
  const tableMeta = tablesInSchema.find((t) => t.name === currentTable);

  const mode = (params.mode as string) || 'replace';
  const mergeKeys: string[] = Array.isArray(params.merge_on) ? params.merge_on : [];
  const [newKey, setNewKey] = useState('');

  return (
    <>
      <div className="bg-blue-50 rounded-md px-3 py-1.5 border border-blue-100 flex items-center justify-between">
        <span className="text-[10px] text-blue-700">
          {isSink ? 'Writes to a managed Parquet table — pick or create one on Storage → Managed Tables.' : 'Reads from a managed Parquet table in this workspace.'}
        </span>
        <button
          type="button"
          onClick={refresh}
          className="text-[10px] font-semibold text-blue-700 hover:text-blue-900 underline"
          title="Re-fetch tables from /api/storage/tables"
        >
          Refresh
        </button>
      </div>

      <Field label="Schema *">
        <select
          value={currentSchema}
          onChange={(e) => onChange(nodeId, { schema_name: e.target.value, table_name: '' })}
          className="w-full px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 bg-white"
        >
          {schemas.length === 0 ? (
            <option value="default">default</option>
          ) : (
            schemas.map((s) => <option key={s} value={s}>{s}</option>)
          )}
          {!schemas.includes(currentSchema) && currentSchema && (
            <option value={currentSchema}>{currentSchema} (not in catalog)</option>
          )}
        </select>
      </Field>

      <Field label="Table *">
        <div className="flex gap-1.5 mb-1.5">
          <select
            value={currentTable}
            onChange={(e) => onChange(nodeId, { table_name: e.target.value })}
            className="flex-1 px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 bg-white"
          >
            <option value="">{loading ? 'Loading tables…' : (tablesInSchema.length === 0 ? '— no tables in this schema —' : 'Select a table…')}</option>
            {tablesInSchema.map((t) => (
              <option key={t.id} value={t.name}>
                {t.name}{t.row_count != null ? ` (${t.row_count.toLocaleString()} rows)` : ''}
              </option>
            ))}
            {currentTable && !tablesInSchema.find((t) => t.name === currentTable) && (
              <option value={currentTable}>{currentTable} (not in catalog)</option>
            )}
          </select>
        </div>
        {isSink && (
          <input
            type="text"
            value={currentTable}
            onChange={(e) => onChange(nodeId, { table_name: e.target.value.replace(/[^a-zA-Z0-9_]/g, '') })}
            placeholder="…or type a new table name (sink will create it)"
            className="w-full px-2.5 py-1.5 text-xs border border-dashed border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
          />
        )}
        {tableMeta && !isSink && (
          <div className="mt-1 text-[10px] text-slate-500">
            {tableMeta.row_count != null && <>{tableMeta.row_count.toLocaleString()} rows · </>}
            {tableMeta.column_count != null && <>{tableMeta.column_count} cols · </>}
            {(tableMeta.size_bytes / 1024).toFixed(1)} KB
          </div>
        )}
      </Field>

      {isSink && (
        <>
          <Field label="Write Mode">
            <Select
              value={mode}
              onChange={(v) => onChange(nodeId, { mode: v })}
              options={['replace', 'append', 'merge']}
            />
          </Field>
          {(mode === 'replace' || mode === 'merge') && (
            <div className="flex items-start gap-1.5 rounded-md border border-amber-300 bg-amber-50 px-2 py-1.5 -mt-1">
              <span aria-hidden className="mt-px text-sm leading-none text-amber-600">⚠</span>
              <p className="text-[11px] leading-snug text-amber-800">
                {mode === 'replace' ? (
                  <>
                    <b>Replace</b> drops the existing contents of{' '}
                    <code className="font-mono">{currentSchema}.{currentTable || 'this table'}</code>{' '}
                    and writes fresh — previous rows are lost.
                  </>
                ) : (
                  <>
                    <b>Merge</b> upserts on the key columns — existing rows whose keys match are
                    overwritten in place.
                  </>
                )}
              </p>
            </div>
          )}
          {mode === 'merge' && (
            <Field label="Merge Keys *">
              <div className="space-y-1.5">
                <div className="flex flex-wrap gap-1">
                  {mergeKeys.map((k) => (
                    <span key={k} className="inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-semibold bg-violet-100 text-violet-700 rounded">
                      {k}
                      <button
                        type="button"
                        onClick={() => onChange(nodeId, { merge_on: mergeKeys.filter((x) => x !== k) })}
                        className="text-violet-500 hover:text-violet-900"
                        aria-label={`Remove ${k}`}
                      >×</button>
                    </span>
                  ))}
                </div>
                <div className="flex gap-1.5">
                  <input
                    value={newKey}
                    onChange={(e) => setNewKey(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && newKey.trim()) {
                        e.preventDefault();
                        const k = newKey.trim();
                        if (!mergeKeys.includes(k)) onChange(nodeId, { merge_on: [...mergeKeys, k] });
                        setNewKey('');
                      }
                    }}
                    placeholder="Column name + Enter"
                    className="flex-1 px-2.5 py-1 text-xs border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
                  />
                  <button
                    type="button"
                    disabled={!newKey.trim()}
                    onClick={() => {
                      const k = newKey.trim();
                      if (!k) return;
                      if (!mergeKeys.includes(k)) onChange(nodeId, { merge_on: [...mergeKeys, k] });
                      setNewKey('');
                    }}
                    className="px-2 py-1 text-xs font-semibold rounded-lg bg-violet-600 text-white hover:bg-violet-700 disabled:opacity-50"
                  >Add</button>
                </div>
                <p className="text-[10px] text-slate-500">Rows with matching keys are replaced; non-matching rows are kept.</p>
              </div>
            </Field>
          )}
        </>
      )}
    </>
  );
}

/**
 * SyncModeField — incremental-sync UI for source nodes (F1, 2026-05-30).
 *
 * Backed by:
 *   - `db_source.execute()` auto-loads/saves cursor via SyncStateStore
 *   - `GET /api/sync-state/{wf}/{step}` for the "Last cursor" display
 *   - `DELETE /api/sync-state/{wf}/{step}` for the Reset State button
 *
 * Behaviour:
 *   - On mount, fetch the persisted cursor (best-effort; silent on fail).
 *   - Write canonical `sync_mode` enum + `watermark_column` to params.
 *   - Keep manual `watermark_value` as an optional override (backfill aid).
 *   - Back-compat: legacy `params.incremental === true` boolean renders
 *     as sync_mode=incremental so old saved pipelines don't regress.
 */
function SyncModeField({ params, nodeId, onChange }: ConfigProps) {
  const workflowId = useWorkflowStore((s) => s.workflowId);
  const [stored, setStored] = useState<{ last_cursor: string | null; last_run_at: string | null; rows_last_run: number } | null>(null);
  const [resetting, setResetting] = useState(false);

  // Resolve current sync_mode with back-compat for the legacy
  // `incremental: true` boolean shape.
  const syncMode: 'full_refresh' | 'incremental' | 'cdc' = (() => {
    const sm = params.sync_mode;
    if (sm === 'full_refresh' || sm === 'incremental' || sm === 'cdc') return sm;
    if (params.incremental === true) return 'incremental';
    return 'full_refresh';
  })();

  const watermarkCol = params.watermark_column || '';

  useEffect(() => {
    if (!workflowId || !nodeId || syncMode !== 'incremental') {
      setStored(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/sync-state/${encodeURIComponent(workflowId)}/${encodeURIComponent(nodeId)}`, {
          headers: { 'Authorization': `Bearer ${localStorage.getItem('fpulse_token') || ''}` },
        });
        if (!res.ok) return;
        const body = await res.json();
        if (!cancelled) setStored(body.state || null);
      } catch {
        // Best-effort — leave the panel empty if the call fails.
      }
    })();
    return () => { cancelled = true; };
  }, [workflowId, nodeId, syncMode, watermarkCol]);

  const onResetState = async () => {
    if (!workflowId || !nodeId) return;
    if (!window.confirm('Reset cursor for this step? The next run will read everything from the source again.')) return;
    setResetting(true);
    try {
      await fetch(`/api/sync-state/${encodeURIComponent(workflowId)}/${encodeURIComponent(nodeId)}`, {
        method: 'DELETE',
        headers: { 'Authorization': `Bearer ${localStorage.getItem('fpulse_token') || ''}` },
      });
      setStored(null);
    } finally {
      setResetting(false);
    }
  };

  return (
    <>
      <Field label="Sync Mode">
        <Select
          value={syncMode}
          onChange={(v) => onChange(nodeId, {
            sync_mode: v,
            // Clear the legacy boolean so a future round-trip doesn't reintroduce drift.
            incremental: undefined,
          })}
          options={['full_refresh', 'incremental', 'cdc']}
          labels={{
            full_refresh: 'Full refresh (re-read every row)',
            incremental: 'Incremental (cursor-tracked)',
            cdc: 'CDC (use dedicated CDC Source node)',
          }}
        />
      </Field>
      {syncMode === 'incremental' && (
        <>
          <Field label="Cursor Column">
            <TextInput
              value={watermarkCol}
              onChange={(v) => onChange(nodeId, { watermark_column: v })}
              placeholder="updated_at"
            />
          </Field>
          <Field label="Manual cursor override (optional)">
            <TextInput
              value={params.watermark_value || ''}
              onChange={(v) => onChange(nodeId, { watermark_value: v })}
              placeholder="leave blank for auto-tracked cursor"
            />
          </Field>
          <div className="bg-emerald-50 border border-emerald-200 rounded-md px-3 py-2 text-[11px] text-emerald-900 space-y-1">
            <div className="flex items-baseline justify-between gap-2">
              <span className="font-semibold uppercase tracking-wider text-[9px] text-emerald-700">Sync state</span>
              {workflowId && (
                <button
                  type="button"
                  onClick={onResetState}
                  disabled={resetting || !stored}
                  className="text-[10px] font-medium text-red-700 hover:text-red-900 disabled:opacity-40"
                >
                  {resetting ? 'Resetting…' : 'Reset state'}
                </button>
              )}
            </div>
            {stored ? (
              <>
                <div>Last cursor: <code className="px-1 rounded bg-white border border-emerald-200 font-mono text-[10px]">{stored.last_cursor || '—'}</code></div>
                <div>Last run: {stored.last_run_at ? new Date(stored.last_run_at).toLocaleString() : '—'}</div>
                <div>Rows last run: {stored.rows_last_run ?? 0}</div>
              </>
            ) : (
              <div className="text-emerald-700/70">No prior sync — first run will read everything and stamp the cursor.</div>
            )}
          </div>
        </>
      )}
      {syncMode === 'cdc' && (
        <div className="bg-amber-50 border border-amber-200 rounded-md px-3 py-2 text-[11px] text-amber-900">
          Log-based replication lives in the dedicated <strong>CDC Source</strong> node — use that instead of DB Source for true CDC. This option is informational here.
        </div>
      )}
    </>
  );
}

function DbSourceConfig({ params, nodeId, onChange, hideConnection }: ConfigProps) {
  // The backend (`fpulse/nodes/db_source.py`) reads `params.source_mode`
  // with values `query` / `table` (and `procedure` for call mode), NOT
  // the older `operation` key with `execute_query` / `read_table` etc.
  // The drift caused Test Node to fail with "no SQL query provided"
  // even though the user had picked `read_table` and filled in
  // schema + table. We migrate the value here so both old saved
  // pipelines (with `operation`) and new pipelines (with `source_mode`)
  // render the same UI, and we write the canonical `source_mode` on
  // every change so the backend sees what it expects.
  const sourceMode: 'query' | 'table' | 'procedure' = (() => {
    const sm = params.source_mode;
    if (sm === 'query' || sm === 'table' || sm === 'procedure') return sm;
    const legacy = params.operation;
    if (legacy === 'read_table') return 'table';
    if (legacy === 'call_procedure') return 'procedure';
    return 'query';
  })();
  const onChangeMode = (next: string) => {
    // Write the canonical key the backend expects, and clear the legacy
    // `operation` key so a future round-trip doesn't reintroduce the
    // drift.
    onChange(nodeId, { source_mode: next, operation: undefined });
  };
  return (
    <>
      {/* Connection picker — suppressed when embedded in the generic Source,
          which already owns the connection via its top-level picker (no dual
          control). Standalone db_source keeps its own. */}
      {!hideConnection && (
        <>
          <Field label="Connection">
            <ConnectionPicker
              value={params.connection_id || ''}
              onChange={(v) => onChange(nodeId, { connection_id: v })}
              filter={['postgresql', 'mysql', 'mssql', 'sqlite', 'oracle']}
              requireCapability="read"
            />
          </Field>
          {!params.connection_id && (
            <div className="bg-blue-50 rounded-lg px-3 py-1.5 border border-blue-100">
              <span className="text-[9px] text-blue-500">No connection selected — query runs against DuckDB in-memory.</span>
            </div>
          )}
        </>
      )}
      {/* Mode + a contextual "Browse …" button on the same row. The
          button used to live in its own floated row beneath the Mode
          field which left a lot of empty space on screen. Placing it
          inline keeps the field count down and the visual flow tight. */}
      <div className="flex items-end gap-2">
        <div className="flex-1 min-w-0">
          <Field label="Mode *">
            <Select
              value={sourceMode}
              onChange={onChangeMode}
              options={['query', 'table', 'procedure']}
              labels={{ query: 'SQL Query', table: 'Table', procedure: 'Stored Procedure' }}
            />
          </Field>
        </div>
        {params.connection_id && sourceMode === 'table' && (
          <CatalogPicker
            connectionId={params.connection_id}
            kinds={['table', 'view']}
            label="Browse tables"
            compact
            onPick={(item) => onChange(nodeId, { schema: item.parent, table: item.name })}
          />
        )}
        {params.connection_id && sourceMode === 'procedure' && (
          <CatalogPicker
            connectionId={params.connection_id}
            kinds={['procedure', 'function']}
            label="Browse procedures"
            compact
            onPick={(item) => onChange(nodeId, { schema: item.parent, procedure: item.name })}
          />
        )}
      </div>
      {sourceMode === 'query' && (
        <Field label="SQL Query *">
          <TextArea
            value={params.query || ''}
            onChange={(v) => onChange(nodeId, { query: v })}
            placeholder="SELECT * FROM table_name WHERE id = {{ $json.id }}"
            rows={5}
          />
        </Field>
      )}
      {sourceMode === 'table' && (
        <div className="grid grid-cols-2 gap-2">
          <Field label="Schema">
            <CatalogDatalistInput
              connectionId={params.connection_id}
              field="schema"
              value={params.schema || ''}
              onChange={(v) => onChange(nodeId, { schema: v })}
              placeholder="public"
            />
          </Field>
          <Field label="Table *">
            <CatalogDatalistInput
              connectionId={params.connection_id}
              field="table"
              schema={params.schema || ''}
              value={params.table || ''}
              onChange={(v) => onChange(nodeId, { table: v })}
              placeholder="orders"
            />
          </Field>
        </div>
      )}
      {sourceMode === 'procedure' && (
        <>
          {/* Schema + Procedure as catalog-backed datalists scoped to
              kinds=['procedure','function'] — same pattern as Table
              mode so users can pick from the live catalog or type a
              name to create one. */}
          <div className="grid grid-cols-2 gap-2">
            <Field label="Schema">
              <CatalogDatalistInput
                connectionId={params.connection_id}
                field="schema"
                kinds={['procedure', 'function']}
                value={params.schema || ''}
                onChange={(v) => onChange(nodeId, { schema: v })}
                placeholder="dbo"
              />
            </Field>
            <Field label="Procedure *">
              <CatalogDatalistInput
                connectionId={params.connection_id}
                field="table"
                kinds={['procedure', 'function']}
                schema={params.schema || ''}
                value={params.procedure || ''}
                onChange={(v) => onChange(nodeId, { procedure: v })}
                placeholder="sp_refresh_orders"
              />
            </Field>
          </div>
          <Field label="Arguments (JSON array)">
            <TextArea
              value={params.proc_args ? JSON.stringify(params.proc_args, null, 2) : '[]'}
              onChange={(v) => { try { onChange(nodeId, { proc_args: JSON.parse(v) }); } catch {} }}
              placeholder='["{{ $json.id }}", 42]'
              rows={3}
            />
          </Field>
        </>
      )}
      {/* 2026-05-30 (P2 + F1): first-class sync_mode contract.
          full_refresh = re-read every row each run.
          incremental = backend auto-loads cursor from sync_state table
                        and writes the new max back when the run succeeds;
                        the operator only types the cursor column once.
          cdc          = informational — the dedicated `cdc_source` node
                        does log-based replication for the few dialects
                        that support it.
          Back-compat: an old workflow with `incremental=true` (boolean)
          renders as sync_mode=incremental so the existing UI doesn't
          regress. */}
      <SyncModeField params={params} nodeId={nodeId} onChange={onChange} />
      <Field label="Dev Sample Limit">
        <TextInput value={String(params.dev_sample_rows ?? '')} onChange={(v) => onChange(nodeId, { dev_sample_rows: v ? parseInt(v) : undefined })}
          placeholder="1000 (blank = no limit)" />
      </Field>
    </>
  );
}

function FilterConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const mode = params.mode || 'expression';
  const rules = Array.isArray(params.rules) ? params.rules : [];
  // Operator VALUES must be the backend's snake_case keys (filter_node.py
  // _OPERATORS) — the old SQL tokens (LIKE/IN/IS NULL/BETWEEN) silently
  // miscompiled to `=`. (2026-06-15 drift fix.)
  const FILTER_OPS: Array<{ value: string; label: string }> = [
    { value: '=', label: '=' }, { value: '!=', label: '≠' },
    { value: '>', label: '>' }, { value: '<', label: '<' },
    { value: '>=', label: '≥' }, { value: '<=', label: '≤' },
    { value: 'contains', label: 'contains' }, { value: 'not_contains', label: 'not contains' },
    { value: 'starts_with', label: 'starts with' }, { value: 'ends_with', label: 'ends with' },
    { value: 'in', label: 'in (a,b,c)' }, { value: 'not_in', label: 'not in (a,b,c)' },
    { value: 'is_null', label: 'is null' }, { value: 'is_not_null', label: 'is not null' },
    { value: 'between', label: 'between (a,b)' },
  ];
  const textareaId = `filter-${nodeId}`;
  const insertColumn = (col: string) => {
    const el = document.getElementById(textareaId) as HTMLTextAreaElement | null;
    if (el) {
      const start = el.selectionStart;
      const end = el.selectionEnd;
      const val = params.condition || '';
      const newVal = val.slice(0, start) + col + val.slice(end);
      onChange(nodeId, { condition: newVal });
      setTimeout(() => { el.focus(); el.setSelectionRange(start + col.length, start + col.length); }, 0);
    } else {
      onChange(nodeId, { condition: (params.condition || '') + col });
    }
  };
  const updateRule = (idx: number, patch: Record<string, any>) => {
    onChange(nodeId, { rules: rules.map((r: any, i: number) => i === idx ? { ...r, ...patch } : r) });
  };

  return (
    <>
      <Field label="Filter Mode">
        <Select value={mode} onChange={(v) => onChange(nodeId, { mode: v })} options={['expression', 'rules']} />
      </Field>
      <p className="text-[10px] text-slate-400 -mt-1">
        {mode === 'expression'
          ? 'Expression mode needs one SQL-style condition (required) — use the columns below.'
          : 'Rules mode needs at least one rule; each rule’s value field depends on the operator (none for is null / is not null).'}
      </p>
      {mode === 'expression' && (
        <>
          <ColumnPicker columns={columns} onInsert={insertColumn} />
          <Field label="Filter Condition *">
            <textarea
              id={textareaId}
              value={params.condition || ''}
              onChange={(e) => onChange(nodeId, { condition: e.target.value })}
              placeholder="amount > 100 AND status = 'active'"
              rows={3}
              className="w-full px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-pipe-300 focus:border-transparent resize-none"
            />
          </Field>
        </>
      )}
      {mode === 'rules' && (
        <>
          <Field label="Combine Rules With">
            <Select value={params.combinator || 'AND'} onChange={(v) => onChange(nodeId, { combinator: v })} options={['AND', 'OR']} />
          </Field>
          <Field label={`Rules (${rules.length})`}>
            <div className="space-y-1.5">
              {rules.map((rule: any, idx: number) => (
                <div key={idx} className="p-2 bg-slate-50 rounded-lg border border-slate-100 space-y-1">
                  <div className="flex items-center gap-1">
                    {columns.length > 0 ? (
                      <select value={rule.column || ''} onChange={(e) => updateRule(idx, { column: e.target.value })}
                        className="flex-1 px-2 py-1 text-xs border border-slate-200 rounded bg-white font-mono">
                        <option value="">— Column —</option>
                        {columns.map((c) => <option key={c} value={c}>{c}</option>)}
                      </select>
                    ) : (
                      <input value={rule.column || ''} onChange={(e) => updateRule(idx, { column: e.target.value })}
                        placeholder="column" className="flex-1 px-2 py-1 text-xs border border-slate-200 rounded font-mono" />
                    )}
                    <select value={rule.op || '='} onChange={(e) => updateRule(idx, { op: e.target.value })}
                      className="w-28 px-1.5 py-1 text-xs border border-slate-200 rounded bg-white">
                      {FILTER_OPS.map((op) => <option key={op.value} value={op.value}>{op.label}</option>)}
                    </select>
                    <button onClick={() => onChange(nodeId, { rules: rules.filter((_: any, i: number) => i !== idx) })}
                      className="text-red-400 hover:text-red-600 text-xs px-1 shrink-0">×</button>
                  </div>
                  {!['is_null', 'is_not_null'].includes(rule.op) && (
                    <input value={rule.value ?? ''} onChange={(e) => updateRule(idx, { value: e.target.value })}
                      placeholder="Value" className="w-full px-2 py-1 text-xs border border-slate-200 rounded font-mono" />
                  )}
                </div>
              ))}
              <button onClick={() => onChange(nodeId, { rules: [...rules, { column: columns[0] || '', op: '=', value: '' }] })}
                className="text-xs text-pipe-600 hover:text-pipe-800 font-medium">+ Add Rule</button>
            </div>
          </Field>
        </>
      )}
    </>
  );
}

function TransformConfig({ params, nodeId, onChange, columns = [], upstreamNodes = [], allAncestors = [] }: ConfigProps) {
  const textareaId = `transform-${nodeId}`;
  const [showSnippets, setShowSnippets] = useState(false);
  const [showAllInputs, setShowAllInputs] = useState(false);
  const [editingNodeId, setEditingNodeId] = useState<string | null>(null);
  const [editingLabel, setEditingLabel] = useState('');
  const setCodeEditorOpen = useWorkflowStore((s) => s.setCodeEditorOpen);
  const updateNodeLabel = useWorkflowStore((s) => s.updateNodeLabel);
  const updateEdgeAlias = useWorkflowStore((s) => s.updateEdgeAlias);
  const edges = useWorkflowStore((s) => s.edges);

  // Per-edge alias editing — the SQL table name THIS Transform reads each input
  // as. Defaults to the sanitized upstream label; the user can override it so
  // SQL reads a stable, self-chosen name regardless of the node's label.
  const [editingAliasId, setEditingAliasId] = useState<string | null>(null);
  const [editingAlias, setEditingAlias] = useState('');
  const sanitizeName = (s: string) => s.toLowerCase().replace(/[^a-z0-9_]/g, '_').replace(/^_+|_+$/g, '');
  const edgeAliasFor = (upId: string): string =>
    ((edges.find((e) => e.source === upId && e.target === nodeId)?.data as any)?.alias as string) || '';
  const tableNameFor = (up: { id: string; label: string }): string =>
    edgeAliasFor(up.id) || sanitizeName(up.label);

  // Derive display info from upstream nodes
  const inputSource = upstreamNodes[0];

  const insertAtCursor = (text: string) => {
    const el = document.getElementById(textareaId) as HTMLTextAreaElement | null;
    if (el) {
      const start = el.selectionStart;
      const end = el.selectionEnd;
      const val = params.expression || '';
      const newVal = val.slice(0, start) + text + val.slice(end);
      onChange(nodeId, { expression: newVal });
      setTimeout(() => { el.focus(); el.setSelectionRange(start + text.length, start + text.length); }, 0);
    } else {
      onChange(nodeId, { expression: (params.expression || '') + text });
    }
  };

  // Build smart snippets using actual column names
  const col1 = columns[0] || 'col1';
  const col2 = columns[1] || 'col2';
  const colList = columns.length > 0 ? columns.slice(0, 4).join(', ') : 'col1, col2';

  const SQL_SNIPPETS = [
    { label: 'SELECT *', sql: 'SELECT * FROM source_table' },
    { label: 'SELECT cols', sql: `SELECT ${colList} FROM source_table` },
    { label: 'WHERE', sql: ` WHERE ${col1} > 0` },
    { label: 'GROUP BY', sql: ` GROUP BY ${col1}` },
    { label: 'ORDER BY', sql: ` ORDER BY ${col1} DESC` },
    { label: 'HAVING', sql: ` HAVING COUNT(*) > 1` },
    { label: 'LIMIT', sql: ' LIMIT 100' },
    { label: 'CASE', sql: `CASE WHEN ${col1} > 0 THEN 'yes' ELSE 'no' END AS ${col1}_flag` },
    { label: 'CAST', sql: `CAST(${col1} AS INTEGER)` },
    { label: 'COALESCE', sql: `COALESCE(${col1}, 'default')` },
    { label: 'COUNT', sql: `COUNT(*) AS total` },
    { label: 'SUM', sql: `SUM(${col1}) AS total_${col1}` },
    { label: 'AVG', sql: `AVG(${col1}) AS avg_${col1}` },
    { label: 'CONCAT', sql: `${col1} || ' ' || ${col2} AS combined` },
    { label: 'SUBSTR', sql: `SUBSTR(${col1}, 1, 10)` },
    { label: 'UPPER', sql: `UPPER(${col1})` },
    { label: 'LOWER', sql: `LOWER(${col1})` },
    { label: 'TRIM', sql: `TRIM(${col1})` },
    { label: 'DATE', sql: `STRFTIME('%Y-%m-%d', ${col1})` },
    { label: 'NOW', sql: 'CURRENT_TIMESTAMP AS loaded_at' },
  ];

  return (
    <div className="space-y-3">
      {/* Open full editor button */}
      <button
        onClick={() => setCodeEditorOpen(true)}
        className="w-full flex items-center justify-center gap-2 px-3 py-2 bg-pipe-50 text-pipe-700 text-xs font-semibold rounded-lg border border-pipe-200 hover:bg-pipe-100 hover:border-pipe-300 transition-colors"
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="15 3 21 3 21 9" /><polyline points="9 21 3 21 3 15" />
          <line x1="21" y1="3" x2="14" y2="10" /><line x1="3" y1="21" x2="10" y2="14" />
        </svg>
        Open Full Editor
      </button>

      {/* Input Datasets — shows all connected upstream nodes as named SQL tables */}
      {upstreamNodes.length > 0 && (
        <div className="bg-blue-50 rounded-lg px-3 py-2.5 border border-blue-200">
          <div className="text-[9px] font-bold text-blue-600 uppercase tracking-wider mb-2 flex items-center gap-1">
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M5 12h14"/><path d="M12 5l7 7-7 7"/></svg>
            Input Datasets
            <span className="ml-1 text-[8px] font-normal text-blue-400">({upstreamNodes.length} connected)</span>
          </div>

          {/* Each direct upstream as a named dataset */}
          <div className="space-y-1.5">
            {upstreamNodes.map((up, idx) => {
              const labelAlias = sanitizeName(up.label);
              const customAlias = edgeAliasFor(up.id);
              const alias = customAlias || labelAlias;  // effective SQL table name
              const fileName = up.file ? up.file.replace(/^.*[\\/]/, '') : null;
              const isPrimary = idx === 0;
              const isEditing = editingNodeId === up.id;
              const isEditingAlias = editingAliasId === up.id;
              return (
                <div key={up.id} className="flex items-center gap-2 bg-white rounded-md px-2.5 py-1.5 border border-blue-200/60">
                  <span className={`w-2 h-2 rounded-full shrink-0 ${isPrimary ? 'bg-blue-500' : 'bg-indigo-400'}`} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      {isEditing ? (
                        <input
                          autoFocus
                          value={editingLabel}
                          onChange={(e) => setEditingLabel(e.target.value)}
                          onBlur={() => {
                            if (editingLabel.trim() && editingLabel.trim() !== up.label) {
                              updateNodeLabel(up.id, editingLabel.trim());
                            }
                            setEditingNodeId(null);
                          }}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') {
                              if (editingLabel.trim() && editingLabel.trim() !== up.label) {
                                updateNodeLabel(up.id, editingLabel.trim());
                              }
                              setEditingNodeId(null);
                            }
                            if (e.key === 'Escape') setEditingNodeId(null);
                          }}
                          className="text-xs font-semibold text-blue-800 bg-blue-50 border border-blue-300 rounded px-1.5 py-0.5 outline-none focus:ring-1 focus:ring-blue-400 w-full"
                        />
                      ) : (
                        <span
                          className="text-xs font-semibold text-blue-800 truncate cursor-pointer hover:text-blue-600 border-b border-dashed border-transparent hover:border-blue-300 transition-colors"
                          onDoubleClick={() => { setEditingNodeId(up.id); setEditingLabel(up.label); }}
                          title="Double-click to rename (changes SQL alias)"
                        >{up.label}</span>
                      )}
                      <span className="text-[8px] text-blue-400 uppercase shrink-0">{up.stepType.replace(/_/g, ' ')}</span>
                    </div>
                    {fileName && (
                      <div className="text-[9px] text-blue-500 font-mono truncate">{fileName}</div>
                    )}
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    {isPrimary && (
                      <code
                        className="bg-blue-100 px-1.5 py-0.5 rounded border border-blue-200 text-blue-700 font-mono text-[9px] font-semibold cursor-pointer hover:bg-blue-200 transition-colors"
                        onClick={() => insertAtCursor('source_table')}
                        title="Primary input — click to insert"
                      >source_table</code>
                    )}
                    {isEditingAlias ? (
                      <input
                        autoFocus
                        value={editingAlias}
                        placeholder={labelAlias}
                        onChange={(e) => setEditingAlias(e.target.value)}
                        onBlur={() => { updateEdgeAlias(up.id, nodeId, editingAlias); setEditingAliasId(null); }}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') { updateEdgeAlias(up.id, nodeId, editingAlias); setEditingAliasId(null); }
                          if (e.key === 'Escape') setEditingAliasId(null);
                        }}
                        className="w-24 bg-white px-1.5 py-0.5 rounded border border-indigo-300 text-indigo-700 font-mono text-[9px] outline-none focus:ring-1 focus:ring-indigo-400"
                      />
                    ) : (
                      <code
                        className={`px-1.5 py-0.5 rounded border font-mono text-[9px] cursor-pointer transition-colors ${customAlias ? 'bg-indigo-100 border-indigo-300 text-indigo-700 font-semibold hover:bg-indigo-200' : 'bg-white border-blue-200 text-blue-600 hover:bg-blue-100'}`}
                        onClick={() => insertAtCursor(alias)}
                        onDoubleClick={() => { setEditingAliasId(up.id); setEditingAlias(customAlias); }}
                        title={`Click to insert "${alias}" · double-click to set a custom table name`}
                      >{alias}</code>
                    )}
                    <button
                      onClick={() => { setEditingAliasId(up.id); setEditingAlias(customAlias); }}
                      className="w-5 h-5 flex items-center justify-center rounded text-blue-300 hover:text-indigo-600 hover:bg-indigo-50 transition-colors"
                      title="Set SQL table name (per-edge alias)"
                    >
                      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>
                    </button>
                    <button
                      onClick={() => { setEditingNodeId(up.id); setEditingLabel(up.label); }}
                      className="w-5 h-5 flex items-center justify-center rounded text-blue-300 hover:text-blue-600 hover:bg-blue-100 transition-colors"
                      title="Rename dataset"
                    >
                      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
                        <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
                      </svg>
                    </button>
                  </div>
                </div>
              );
            })}
          </div>

          {/* All ancestor nodes (indirect) — expandable */}
          {allAncestors.length > upstreamNodes.length && (
            <>
              <button
                onClick={() => setShowAllInputs(!showAllInputs)}
                className="mt-2 text-[8px] font-medium text-blue-500 hover:text-blue-700 bg-white px-2 py-1 rounded border border-blue-200 hover:bg-blue-100 transition-colors w-full text-center"
              >
                {showAllInputs ? 'Hide indirect ancestors' : `Show all ancestors (${allAncestors.length})`}
              </button>
              {showAllInputs && (
                <div className="mt-1.5 space-y-1">
                  {allAncestors.filter((anc) => !upstreamNodes.find((u) => u.id === anc.id)).map((anc) => {
                    const ancAlias = anc.label.toLowerCase().replace(/[^a-z0-9_]/g, '_').replace(/^_+|_+$/g, '');
                    return (
                      <div key={anc.id} className="flex items-center gap-2 text-xs bg-white/60 rounded px-2 py-1 border border-blue-100">
                        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${anc.columns.length > 0 ? 'bg-emerald-400' : 'bg-slate-300'}`} />
                        <span className="font-medium text-blue-600 truncate">{anc.label}</span>
                        {anc.columns.length > 0 && (
                          <span className="text-[8px] text-blue-400">{anc.columns.length} cols</span>
                        )}
                        <code
                          className="ml-auto text-[8px] font-mono text-blue-500 bg-white px-1.5 py-0.5 rounded border border-blue-200 hover:bg-blue-100 cursor-pointer transition-colors"
                          onClick={() => insertAtCursor(ancAlias)}
                          title={`Insert "${ancAlias}"`}
                        >{ancAlias}</code>
                      </div>
                    );
                  })}
                </div>
              )}
            </>
          )}

          {/* SQL reference hint */}
          <div className="mt-2 text-[8px] text-blue-400 bg-white/50 rounded px-2 py-1.5 border border-blue-100">
            {upstreamNodes.length === 1 ? (
              <span>Use <code className="font-mono font-semibold text-blue-600">source_table</code> or <code className="font-mono font-semibold text-blue-600">{tableNameFor(upstreamNodes[0])}</code> in your SQL. Double-click a table name above to rename it.</span>
            ) : (
              <span>Use dataset names in SQL. Example: <code className="font-mono font-semibold text-blue-600">SELECT a.*, b.col FROM {tableNameFor(upstreamNodes[0])} a JOIN {tableNameFor(upstreamNodes[1])} b ON a.id = b.id</code>. Double-click a table name to rename it.</span>
            )}
          </div>
        </div>
      )}

      {/* Column chips — click to insert */}
      {columns.length > 0 && (
        <div>
          <div className="text-[9px] font-semibold text-pipe-600 uppercase tracking-wider mb-1.5 flex items-center gap-1">
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>
            Columns (click to insert)
          </div>
          <div className="flex flex-wrap gap-1">
            {columns.map((col) => (
              <button
                key={col}
                onClick={() => insertAtCursor(col)}
                className="text-[9px] px-1.5 py-0.5 bg-pipe-50 text-pipe-600 rounded border border-pipe-200 font-mono hover:bg-pipe-100 hover:border-pipe-300 transition-colors cursor-pointer active:scale-95"
              >
                {col}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* SQL snippets toggle */}
      <div>
        <button
          onClick={() => setShowSnippets(!showSnippets)}
          className="text-[9px] font-semibold text-pulse-600 uppercase tracking-wider flex items-center gap-1 hover:text-pulse-700 transition-colors"
        >
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
          </svg>
          SQL Snippets
          <svg
            width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3"
            className={`transition-transform ${showSnippets ? 'rotate-180' : ''}`}
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>
        {showSnippets && (
          <div className="flex flex-wrap gap-1 mt-1.5">
            {SQL_SNIPPETS.map((s) => (
              <button
                key={s.label}
                onClick={() => insertAtCursor(s.sql)}
                className="text-[9px] px-1.5 py-0.5 bg-pulse-50 text-pulse-700 rounded border border-pulse-200 hover:bg-pulse-100 hover:border-pulse-300 transition-colors cursor-pointer active:scale-95 font-medium"
                title={s.sql}
              >
                {s.label}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* SQL Editor */}
      <div>
        <div className="flex items-center justify-between mb-1">
          <label className="text-xs font-semibold text-slate-400 uppercase tracking-wider">SQL Expression *</label>
          <span className="text-[8px] text-slate-300 font-mono">DuckDB SQL</span>
        </div>
        <div className="relative rounded-lg border border-slate-200 overflow-hidden focus-within:ring-2 focus-within:ring-pipe-300 focus-within:border-pipe-300">
          {/* Line numbers gutter */}
          <div className="absolute left-0 top-0 bottom-0 w-7 bg-slate-50 border-r border-slate-200 flex flex-col items-end pt-[7px] pr-1.5 pointer-events-none select-none">
            {(params.expression || '').split('\n').map((_: string, i: number) => (
              <span key={i} className="text-[9px] text-slate-300 leading-[18px] font-mono">{i + 1}</span>
            ))}
            {!(params.expression || '') && <span className="text-[9px] text-slate-300 leading-[18px] font-mono">1</span>}
          </div>
          <textarea
            id={textareaId}
            value={params.expression || ''}
            onChange={(e) => onChange(nodeId, { expression: e.target.value })}
            placeholder={columns.length > 0
              ? `SELECT ${columns.slice(0, 3).join(', ')}\nFROM source_table\nWHERE ${columns[0]} IS NOT NULL`
              : "SELECT *, col1 + col2 AS total\nFROM source_table\nWHERE amount > 0"
            }
            rows={8}
            className="w-full pl-9 pr-2.5 py-1.5 text-xs text-slate-700 bg-white font-mono focus:outline-none resize-y leading-[18px]"
            spellCheck={false}
          />
        </div>
      </div>

      {/* Helper text */}
      <div className="bg-slate-50 rounded-lg px-3 py-2 border border-slate-100">
        <div className="text-xs text-slate-500 space-y-1">
          <div className="flex items-center gap-1.5">
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-300 shrink-0"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
            {upstreamNodes.length > 1
              ? <>Reference datasets by name: {upstreamNodes.map((u) => <code key={u.id} className="bg-white px-1 rounded border border-slate-200 text-pipe-600 mx-0.5">{u.label.toLowerCase().replace(/[^a-z0-9_]/g, '_').replace(/^_+|_+$/g, '')}</code>)}</>
              : <>Reference upstream data as <code className="bg-white px-1 rounded border border-slate-200 text-pipe-600">source_table</code> or by node name</>
            }
          </div>
          <div className="flex items-center gap-1.5">
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-300 shrink-0"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
            Uses DuckDB SQL syntax — supports window functions, CTEs, JSON
          </div>
        </div>
      </div>
    </div>
  );
}

function DeduplicateConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const selectedKeys = Array.isArray(params.key) ? params.key : [];
  const keysText = selectedKeys.join(', ');

  const toggleKey = (col: string) => {
    const newKeys = selectedKeys.includes(col)
      ? selectedKeys.filter((k: string) => k !== col)
      : [...selectedKeys, col];
    onChange(nodeId, { key: newKeys });
  };

  return (
    <>
      <Field label="Key Columns">
        {columns.length > 0 ? (
          <div className="flex flex-wrap gap-1.5 mb-1">
            {columns.map((col) => (
              <button
                key={col}
                onClick={() => toggleKey(col)}
                className={`text-xs px-2 py-1 rounded-lg font-mono border transition-colors ${
                  selectedKeys.includes(col)
                    ? 'bg-pipe-100 text-pipe-700 border-pipe-300'
                    : 'bg-white text-slate-500 border-slate-200 hover:border-pipe-200'
                }`}
              >
                {selectedKeys.includes(col) && '✓ '}{col}
              </button>
            ))}
          </div>
        ) : (
          <TextInput
            value={keysText}
            onChange={(v) => onChange(nodeId, { key: v.split(',').map((s: string) => s.trim()).filter(Boolean) })}
            placeholder="order_id, email"
          />
        )}
      </Field>
      <Field label="Strategy">
        <Select
          value={params.strategy || 'keep_first'}
          onChange={(v) => onChange(nodeId, { strategy: v })}
          options={['keep_first', 'keep_last']}
        />
      </Field>
      <Field label="Order By (optional)">
        <TextInput
          value={params.order_by || ''}
          onChange={(v) => onChange(nodeId, { order_by: v })}
          placeholder="created_at DESC"
        />
      </Field>
      <Field label="Outputs">
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={params.emit_duplicates || false}
            onChange={(e) => onChange(nodeId, { emit_duplicates: e.target.checked })}
            className="rounded border-slate-300"
          />
          <span className="text-xs text-slate-600">
            Output duplicates separately — adds <span className="font-semibold text-green-600">Unique</span> and{' '}
            <span className="font-semibold text-amber-600">Duplicate</span> handles (audit removed rows)
          </span>
        </label>
      </Field>
    </>
  );
}

function AggregateConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const AGG_FUNCS = ['COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'COUNT_DISTINCT', 'MEDIAN',
    'PERCENTILE_CONT', 'PERCENTILE_DISC', 'STRING_AGG', 'FIRST', 'LAST', 'CUSTOM'];
  const selectedGroupBy = Array.isArray(params.group_by) ? params.group_by : [];
  const groupByText = selectedGroupBy.join(', ');
  const functions = params.functions || [{ column: '*', function: 'COUNT', alias: 'count' }];

  const toggleGroupBy = (col: string) => {
    const newGroup = selectedGroupBy.includes(col)
      ? selectedGroupBy.filter((k: string) => k !== col)
      : [...selectedGroupBy, col];
    onChange(nodeId, { group_by: newGroup });
  };
  const updateFn = (i: number, patch: Record<string, any>) => {
    const updated = [...functions];
    updated[i] = { ...functions[i], ...patch };
    onChange(nodeId, { functions: updated });
  };
  const removeFn = (i: number) => onChange(nodeId, { functions: functions.filter((_: any, idx: number) => idx !== i) });

  return (
    <>
      <Field label="Group By">
        {columns.length > 0 ? (
          <div className="flex flex-wrap gap-1.5 mb-1">
            {columns.map((col) => (
              <button
                key={col}
                onClick={() => toggleGroupBy(col)}
                className={`text-xs px-2 py-1 rounded-lg font-mono border transition-colors ${
                  selectedGroupBy.includes(col)
                    ? 'bg-pipe-100 text-pipe-700 border-pipe-300'
                    : 'bg-white text-slate-500 border-slate-200 hover:border-pipe-200'
                }`}
              >
                {selectedGroupBy.includes(col) && '✓ '}{col}
              </button>
            ))}
          </div>
        ) : (
          <TextInput
            value={groupByText}
            onChange={(v) => onChange(nodeId, { group_by: v.split(',').map((s: string) => s.trim()).filter(Boolean) })}
            placeholder="category, date (blank = global aggregate)"
          />
        )}
      </Field>
      <div className="text-[12px] font-semibold text-slate-700 uppercase tracking-wider mb-1">
        Aggregations
      </div>
      {functions.map((f: any, i: number) => (
        <div key={i} className="p-2 bg-slate-50 rounded-lg border border-slate-100 mb-1.5 space-y-1">
          <div className="flex gap-1.5 items-center">
            <select value={f.function} onChange={(e) => updateFn(i, { function: e.target.value })}
              className="w-28 px-1.5 py-1 text-xs border border-slate-200 rounded bg-white">
              {AGG_FUNCS.map((fn) => <option key={fn} value={fn}>{fn}</option>)}
            </select>
            {columns.length > 0 ? (
              <select value={f.column} onChange={(e) => updateFn(i, { column: e.target.value })}
                className="flex-1 px-1.5 py-1 text-xs border border-slate-200 rounded font-mono bg-white">
                <option value="*">*</option>
                {columns.map((col) => <option key={col} value={col}>{col}</option>)}
              </select>
            ) : (
              <input value={f.column} onChange={(e) => updateFn(i, { column: e.target.value })}
                placeholder="column" className="flex-1 px-1.5 py-1 text-xs border border-slate-200 rounded font-mono" />
            )}
            <input value={f.alias || ''} onChange={(e) => updateFn(i, { alias: e.target.value })}
              placeholder="alias" className="w-20 px-1.5 py-1 text-xs border border-slate-200 rounded font-mono" />
            <button onClick={() => removeFn(i)} className="text-red-400 hover:text-red-600 text-xs px-1 shrink-0">×</button>
          </div>
          {f.function === 'CUSTOM' && (
            <input value={f.expression || ''} onChange={(e) => updateFn(i, { expression: e.target.value })}
              placeholder="Custom SQL expression, e.g. SUM(CASE WHEN ...)" className="w-full px-2 py-1 text-xs border border-slate-200 rounded font-mono" />
          )}
          {(f.function === 'PERCENTILE_CONT' || f.function === 'PERCENTILE_DISC') && (
            <input value={f.percentile ?? 0.5} onChange={(e) => updateFn(i, { percentile: parseFloat(e.target.value) || 0.5 })}
              placeholder="0.5" className="w-24 px-2 py-1 text-xs border border-slate-200 rounded" />
          )}
        </div>
      ))}
      <button
        onClick={() => onChange(nodeId, { functions: [...functions, { column: '*', function: 'COUNT', alias: `agg_${functions.length + 1}` }] })}
        className="text-xs text-pipe-600 hover:text-pipe-700 font-medium"
      >+ Add Aggregation</button>
      <Field label="HAVING (optional)">
        <TextInput value={params.having || ''} onChange={(v) => onChange(nodeId, { having: v })}
          placeholder="COUNT(*) > 10 AND SUM(amount) > 1000" />
      </Field>
      <Field label="ORDER BY (optional)">
        <TextInput value={params.order_by || ''} onChange={(v) => onChange(nodeId, { order_by: v })}
          placeholder="total_amount DESC" />
      </Field>
    </>
  );
}

/** Explicit Left/Right dataset assignment for Join — pinned by step id,
 *  swappable with one click. Replaces the "first edge drawn = left" rule
 *  (2026-06-11 node-audit; edge order is layout, not semantics). */
function JoinSidesPicker({ params, nodeId, onChange }: ConfigProps) {
  const edges = useWorkflowStore((s) => s.edges);
  const allNodes = useWorkflowStore((s) => s.nodes);
  const upstream = edges
    .filter((e) => e.target === nodeId)
    .map((e) => {
      const n = allNodes.find((x) => x.id === e.source);
      return { id: e.source, label: String((n?.data as any)?.label || e.source) };
    });

  if (upstream.length < 2) {
    return (
      <div className="text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-2.5 py-2 leading-relaxed -mt-1">
        Join needs <span className="font-semibold">2 inputs</span>.{' '}
        {upstream.length === 0 ? 'None are' : 'Only 1 is'} connected — wire both datasets in.
      </div>
    );
  }

  const leftId =
    params.left_input_id && upstream.some((u) => u.id === params.left_input_id)
      ? (params.left_input_id as string)
      : upstream[0].id;
  const left = upstream.find((u) => u.id === leftId)!;
  const right = upstream.find((u) => u.id !== leftId)!;

  return (
    <div className="bg-slate-50 rounded-lg px-3 py-2 border border-slate-200 -mt-1 flex items-center gap-2">
      <div className="flex-1 min-w-0">
        <div className="text-[9px] font-semibold text-slate-400 uppercase tracking-wide">Left</div>
        <div className="text-[11px] font-mono text-slate-700 truncate" title={left.label}>{left.label}</div>
      </div>
      <button
        onClick={() => onChange(nodeId, { left_input_id: right.id })}
        className="shrink-0 px-2 py-1 text-[11px] font-semibold text-pipe-600 border border-pipe-300 rounded-lg hover:bg-pipe-50 transition-colors"
        title="Swap which input is the left side of the join"
      >
        ⇄ Swap
      </button>
      <div className="flex-1 min-w-0 text-right">
        <div className="text-[9px] font-semibold text-slate-400 uppercase tracking-wide">Right</div>
        <div className="text-[11px] font-mono text-slate-700 truncate" title={right.label}>{right.label}</div>
      </div>
    </div>
  );
}

function JoinConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  // 2026-05-30: this component used to write three field names that the
  // backend join.py executor doesn't read:
  //   * mapped_keys   (backend reads: key_pairs)
  //   * pair.op       (backend reads: pair.operator)
  //   * select_columns (backend reads: select_left + select_right)
  // Result: mapped_keys mode silently failed, operator dropdown was
  // ignored (always '='), and the "Select Columns" field was a no-op.
  // Names below now match backend/fpulse/nodes/join.py param_schema.
  const keyMode = params.key_mode || 'same_key';
  const keys = Array.isArray(params.join_key) ? params.join_key.join(', ') : (params.join_key || '');
  // Read either name during the transition — `mapped_keys` may live in
  // workflows authored before this fix landed; we surface them on read,
  // and write only the canonical `key_pairs` on every change.
  const keyPairs: any[] = Array.isArray(params.key_pairs)
    ? params.key_pairs
    : (Array.isArray(params.mapped_keys) ? params.mapped_keys : []);

  // Normalise legacy `op` to canonical `operator` on read so the dropdown
  // shows what the user originally picked even on a pre-fix workflow.
  const pairOperator = (k: any) => k.operator ?? k.op ?? '=';

  const updatePair = (idx: number, patch: Record<string, any>) => {
    onChange(nodeId, {
      key_pairs: keyPairs.map((k: any, i: number) =>
        i === idx ? { ...k, ...patch } : k
      ),
    });
  };

  return (
    <>
      <Field label="Join Type">
        <Select
          value={params.join_type || 'INNER'}
          onChange={(v) => onChange(nodeId, { join_type: v })}
          options={['INNER', 'LEFT', 'RIGHT', 'FULL', 'SEMI', 'ANTI', 'CROSS']}
        />
      </Field>
      {/* 2026-06-11 (node-audit): explicit side assignment replaces the
          old "Left = first edge you drew" rule. Edge order is canvas
          layout, not semantics — deleting and re-drawing a connection
          silently swapped the join. The sides are now pinned by step id
          (left_input_id) and swappable with one click. */}
      <JoinSidesPicker params={params} nodeId={nodeId} onChange={onChange} />
      {params.join_type !== 'CROSS' && (
        <>
          <Field label="Key Mode">
            <Select value={keyMode} onChange={(v) => onChange(nodeId, { key_mode: v })} options={['same_key', 'mapped_keys', 'custom']} />
          </Field>
          {keyMode === 'same_key' && (
            <Field label="Join Key columns *">
              {/* 2026-05-30: chip toggle when upstream LEFT schema is
                  known. Free-text fallback for cases where schema
                  isn't available yet (e.g. ad-hoc connector + first
                  edit). Multi-select because composite keys are real
                  (e.g. join on tenant_id AND user_id). */}
              {columns.length > 0 ? (
                <div className="flex flex-wrap gap-1.5 mb-1">
                  {columns.map((col) => {
                    const list: string[] = Array.isArray(params.join_key)
                      ? params.join_key
                      : (params.join_key ? String(params.join_key).split(',').map((s) => s.trim()) : []);
                    const active = list.includes(col);
                    return (
                      <button
                        key={col}
                        onClick={() => {
                          const next = active
                            ? list.filter((k) => k !== col)
                            : [...list, col];
                          onChange(nodeId, { join_key: next });
                        }}
                        className={`text-xs px-2 py-1 rounded-lg font-mono border transition-colors ${
                          active
                            ? 'bg-pipe-100 text-pipe-700 border-pipe-300'
                            : 'bg-white text-slate-500 border-slate-200 hover:border-pipe-200'
                        }`}
                      >{active && '✓ '}{col}</button>
                    );
                  })}
                </div>
              ) : (
                <TextInput value={keys}
                  onChange={(v) => onChange(nodeId, { join_key: v.split(',').map((s: string) => s.trim()).filter(Boolean) })}
                  placeholder="id, customer_id" />
              )}
              <div className="text-[11px] text-slate-500 mt-1">
                Shows columns from the LEFT input. The column must also exist with the same name on the right.
              </div>
            </Field>
          )}
          {keyMode === 'mapped_keys' && (
            <Field label="Key Mappings (left col → right col) *">
              <div className="space-y-1.5">
                {keyPairs.map((k: any, idx: number) => (
                  <div key={idx} className="flex items-center gap-1">
                    <input value={k.left || ''} onChange={(e) => updatePair(idx, { left: e.target.value })}
                      placeholder="left_col" className="flex-1 px-2 py-1 text-xs border border-slate-200 rounded font-mono" />
                    <select value={pairOperator(k)} onChange={(e) => updatePair(idx, { operator: e.target.value })}
                      className="w-12 px-1 py-1 text-xs border border-slate-200 rounded bg-white">
                      {['=', '!=', '>', '<', '>=', '<='].map((op) => <option key={op} value={op}>{op}</option>)}
                    </select>
                    <input value={k.right || ''} onChange={(e) => updatePair(idx, { right: e.target.value })}
                      placeholder="right_col" className="flex-1 px-2 py-1 text-xs border border-slate-200 rounded font-mono" />
                    <button
                      onClick={() => onChange(nodeId, {
                        key_pairs: keyPairs.filter((_: any, i: number) => i !== idx),
                      })}
                      className="text-red-400 hover:text-red-600 text-xs px-1"
                    >×</button>
                  </div>
                ))}
                <button
                  onClick={() => onChange(nodeId, {
                    key_pairs: [...keyPairs, { left: '', right: '', operator: '=' }],
                  })}
                  className="text-xs text-pipe-600 hover:text-pipe-800 font-medium"
                >+ Add Key Pair</button>
              </div>
            </Field>
          )}
          {keyMode === 'custom' && (
            <Field label="Custom ON Clause *">
              <TextArea value={params.custom_on || ''} onChange={(v) => onChange(nodeId, { custom_on: v })}
                placeholder='__join_left."customer_id" = __join_right."id" AND __join_left."date" >= __join_right."start_date"' rows={3} />
              <div className="text-[11px] text-slate-500 mt-1">
                Use <code className="font-mono">__join_left</code> and <code className="font-mono">__join_right</code> for the two tables.
              </div>
            </Field>
          )}
        </>
      )}
      {/* Column selection — split into left + right to match the
          backend's two separate fields. Each accepts a comma list or
          the wildcard form like `__join_left."id", __join_left."name"`. */}
      <Field label="Left columns to keep (optional)">
        <TextInput
          value={params.select_left || ''}
          onChange={(v) => onChange(nodeId, { select_left: v })}
          placeholder='__join_left."id", __join_left."name" — blank = all'
        />
      </Field>
      <Field label="Right columns to keep (optional)">
        <TextInput
          value={params.select_right || ''}
          onChange={(v) => onChange(nodeId, { select_right: v })}
          placeholder='__join_right."amount", __join_right."date" — blank = all'
        />
      </Field>
    </>
  );
}

function OutputConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  return (
    <>
      <Field label="Format">
        <Select
          value={params.format || 'parquet'}
          onChange={(v) => onChange(nodeId, { format: v })}
          options={['parquet', 'csv', 'json']}
        />
      </Field>
      <Field label="File Path (optional)">
        <TextInput
          value={params.file_path || ''}
          onChange={(v) => onChange(nodeId, { file_path: v })}
          placeholder="output/results.parquet"
        />
      </Field>
    </>
  );
}

/* ── New Activity Configs ── */

function ApiSourceConfig({ params, nodeId, onChange }: ConfigProps) {
  const useConnection = !!params.connection_id;
  // 2026-05-22 (audit R2) — legacy inline URL detection. Old
  // workflows imported from earlier instances may carry a `url`
  // parameter without a `connection_id`. The new contract requires
  // a saved connection for any network-side source, so we render
  // the legacy URL as read-only with a "convert to connection" hint
  // instead of letting users keep editing it inline.
  const hasLegacyUrl = !useConnection && !!params.url;
  return (
    <>
      {/* Connection picker is now the primary path, not optional. */}
      <Field label="Connection *">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={[
            'rest_api', 'graphql',
            // 2026-05-23 (T4+U1/U2+V1/V2): Oracle / SAP product families
            // are HTTP-flavoured and route through the API Source node.
            'oracle_api', 'oracle_fusion', 'oracle_bip',
            'sap_s4hana', 'sap_successfactors',
          ]}
          requireCapability="read"
        />
      </Field>
      {useConnection ? (
        <>
          <div className="bg-green-50 rounded-lg px-3 py-1.5 border border-green-100">
            <span className="text-xs text-green-700">Using saved connection — auth is handled automatically.</span>
          </div>
          <Field label="API Path">
            <TextInput value={params.path || ''} onChange={(v) => onChange(nodeId, { path: v })} placeholder="/users?limit=100" />
          </Field>
        </>
      ) : hasLegacyUrl ? (
        <>
          {/* 2026-05-22 (audit R2): legacy inline URL kept for read-
              only display so the user can copy it into a new
              Connection. The form no longer accepts new edits via
              this path. */}
          <div className="bg-amber-50 rounded-lg px-3 py-2 border border-amber-200">
            <div className="text-xs font-semibold text-amber-800 mb-1">Legacy inline URL — please migrate</div>
            <div className="text-xs text-amber-700 mb-2">
              This pipeline references an API URL directly. The new contract requires a saved connection for any API source so credentials and audit trails stay in the Connections page.
            </div>
            <div className="text-xs text-amber-900 font-mono break-all px-2 py-1 bg-white border border-amber-200 rounded">
              {String(params.url)}
            </div>
            <div className="mt-2 text-xs text-amber-700">
              Create a connection with this URL above (auth type = none for public APIs), then select it from the Connection picker. The url field will be cleared automatically once a connection is selected.
            </div>
          </div>
        </>
      ) : (
        <>
          <div className="bg-slate-50 rounded-lg px-3 py-2 border border-slate-200">
            <div className="text-xs text-slate-700">
              Pick a saved connection from the picker above, or click <strong>Create new connection</strong> in the Connections page to add a REST API endpoint. Inline URLs were removed in v1.0 so every API source flows through the Connections audit trail.
            </div>
          </div>
        </>
      )}
      <Field label="Operation *">
        <Select
          value={params.operation || 'fetch'}
          onChange={(v) => onChange(nodeId, { operation: v })}
          options={['fetch', 'paginate', 'stream']}
        />
      </Field>
      <div className="bg-slate-50 rounded-md px-3 py-1.5 border border-slate-200 -mt-1">
        <span className="text-[9px] text-slate-500">
          API Source is read-only (HTTP GET). To POST/PUT/PATCH/DELETE, use <strong>API Sink</strong> or <strong>HTTP Request</strong>.
        </span>
      </div>
      {params.operation === 'paginate' && (
        <>
          <Field label="Pagination Style">
            <Select value={params.pagination || 'page_number'} onChange={(v) => onChange(nodeId, { pagination: v })}
              options={['page_number', 'offset_limit', 'cursor', 'link_header']} />
          </Field>
          <Field label="Page Size">
            <TextInput value={String(params.page_size ?? 100)} onChange={(v) => onChange(nodeId, { page_size: parseInt(v) || 100 })} placeholder="100" />
          </Field>
          <Field label="Max Pages">
            <TextInput value={String(params.max_pages ?? 10)} onChange={(v) => onChange(nodeId, { max_pages: parseInt(v) || 10 })} placeholder="10" />
          </Field>
        </>
      )}
      <Field label="Headers">
        {/* P2-D-2 (2026-05-18): JSON textarea replaced with KeyValueRepeater */}
        <KeyValueRepeater
          value={params.headers || {}}
          onChange={(next) => onChange(nodeId, { headers: next })}
          keyPlaceholder="Header name"
          valuePlaceholder="Value"
          addLabel="+ Add header"
        />
      </Field>
      <Field label="Data Key">
        <TextInput value={params.data_key || ''} onChange={(v) => onChange(nodeId, { data_key: v })}
          placeholder="data or results (JSON key containing the array)" />
      </Field>
      {/* P0-4 (2026-05-18): per-node retry select removed. Retries are
          configured once in the Universal Settings tab (Retry On Fail +
          Max Retries + Retry Delay + Retry Strategy). Three retry
          surfaces with conflicting defaults caused real precedence bugs. */}
      <Field label="Timeout (seconds)">
        <TextInput value={String(params.timeout ?? 30)} onChange={(v) => onChange(nodeId, { timeout: parseInt(v) || 30 })} placeholder="30" />
      </Field>
    </>
  );
}

function SortConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  // 2026-05-30: replaced the comma-separated "amount DESC, name ASC"
  // text input with a structured per-column repeater (column dropdown
  // + ASC/DESC) when upstream columns are known. Falls back to the
  // original free-text input only when the upstream schema is
  // unavailable. The new shape stores entries as either:
  //   - {column: "amount", direction: "DESC"}  (structured)
  //   - "amount DESC"                          (legacy string)
  // Backend's Sort node (nodes/transforms.py) accepts both — sortBy
  // strings are split on whitespace, so structured entries get
  // serialised as "<col> <dir>" on save.
  const rawSortBy = params.sort_by;
  const sortText = Array.isArray(rawSortBy) ? rawSortBy.join(', ') : (rawSortBy || '');

  // Parse the persisted list into structured rows for the rich editor.
  // 2026-06-11: tokens now carry optional NULLS placement —
  // "amount DESC NULLS LAST" — matching the backend SortNode parser.
  const entries: { column: string; direction: 'ASC' | 'DESC'; nulls: '' | 'FIRST' | 'LAST' }[] = (() => {
    const list = Array.isArray(rawSortBy) ? rawSortBy : (rawSortBy ? String(rawSortBy).split(',') : []);
    return list
      .map((it: string) => String(it).trim())
      .filter(Boolean)
      .map((tok: string) => {
        const parts = tok.split(/\s+/);
        const upper = parts.map((p) => p.toUpperCase());
        const nullsIdx = upper.indexOf('NULLS');
        const nulls = nullsIdx >= 0 && (upper[nullsIdx + 1] === 'FIRST' || upper[nullsIdx + 1] === 'LAST')
          ? (upper[nullsIdx + 1] as 'FIRST' | 'LAST')
          : '';
        return {
          column: parts[0] || '',
          direction: (upper[1] === 'DESC' ? 'DESC' : 'ASC') as 'ASC' | 'DESC',
          nulls,
        };
      });
  })();

  const writeEntries = (next: { column: string; direction: string; nulls?: string }[]) => {
    // Re-serialise to the canonical "<col> <dir> [NULLS <pos>]" string
    // list — the same token format the backend parser accepts.
    const serialised = next
      .filter((e) => e.column.trim())
      .map((e) => `${e.column.trim()} ${e.direction}${e.nulls ? ` NULLS ${e.nulls}` : ''}`);
    onChange(nodeId, { sort_by: serialised });
  };

  const updateRow = (i: number, patch: Partial<{ column: string; direction: string; nulls: string }>) => {
    writeEntries(entries.map((e, idx) => (idx === i ? { ...e, ...patch } : e)));
  };

  // Duplicate sort columns produce an engine error at run time — flag
  // them inline the moment they appear.
  const dupCols = (() => {
    const seen = new Set<string>(), dup = new Set<string>();
    for (const e of entries) {
      if (e.column && seen.has(e.column)) dup.add(e.column);
      seen.add(e.column);
    }
    return [...dup];
  })();

  return (
    <>
      {columns.length > 0 ? (
        <Field label="Sort by">
          <div className="space-y-1.5">
            {entries.map((e, i) => (
              <div key={i} className="flex items-center gap-1">
                <select
                  value={e.column}
                  onChange={(ev) => updateRow(i, { column: ev.target.value })}
                  className="flex-1 px-2 py-1 text-xs border border-slate-200 rounded bg-white font-mono"
                >
                  <option value="">— Column —</option>
                  {columns.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
                <select
                  value={e.direction}
                  onChange={(ev) => updateRow(i, { direction: ev.target.value })}
                  className="w-20 px-1 py-1 text-xs border border-slate-200 rounded bg-white"
                >
                  <option value="ASC">ASC</option>
                  <option value="DESC">DESC</option>
                </select>
                <select
                  value={e.nulls}
                  onChange={(ev) => updateRow(i, { nulls: ev.target.value })}
                  className="w-24 px-1 py-1 text-xs border border-slate-200 rounded bg-white"
                  title="Where NULL values sort"
                >
                  <option value="">Nulls: auto</option>
                  <option value="FIRST">Nulls first</option>
                  <option value="LAST">Nulls last</option>
                </select>
                <button
                  onClick={() => writeEntries(entries.filter((_, idx) => idx !== i))}
                  className="text-red-400 hover:text-red-600 text-xs px-1"
                >×</button>
              </div>
            ))}
            {dupCols.length > 0 && (
              <div className="text-[10px] text-red-500">
                Duplicate sort column{dupCols.length > 1 ? 's' : ''}: {dupCols.join(', ')} — remove the extra rule.
              </div>
            )}
            <button
              onClick={() => writeEntries([...entries, { column: columns[0] || '', direction: 'ASC', nulls: '' }])}
              className="text-xs text-pipe-600 hover:text-pipe-800 font-medium"
            >+ Add sort column</button>
          </div>
        </Field>
      ) : (
        <Field label="Sort by (comma-separated)">
          <TextInput
            value={sortText}
            onChange={(v) => onChange(nodeId, { sort_by: v.split(',').map((s: string) => s.trim()).filter(Boolean) })}
            placeholder="amount DESC, name ASC"
          />
        </Field>
      )}
    </>
  );
}

function RenameConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  if (columns.length > 0) {
    return (
      <Field label="Column Mapping">
        <ColumnMapper
          columns={columns}
          mappings={params.mappings || {}}
          onChange={(mappings) => onChange(nodeId, { mappings })}
        />
      </Field>
    );
  }
  return (
    <Field label="Mappings (JSON: old → new)">
      <TextArea
        value={params.mappings ? JSON.stringify(params.mappings, null, 2) : '{}'}
        onChange={(v) => { try { onChange(nodeId, { mappings: JSON.parse(v) }); } catch {} }}
        placeholder='{"old_name": "new_name"}'
        rows={4}
      />
    </Field>
  );
}

function TypecastConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  // P2-D-4 (2026-05-18): JSON textarea swapped for a structured
  // column→type editor matching the Data Wrangler `cast` sub-step.
  // When upstream columns are known, the LEFT side is a column
  // dropdown; otherwise free-text.
  const casts = (params.casts && typeof params.casts === 'object') ? params.casts as Record<string, string> : {};
  const entries = Object.entries(casts);
  const CAST_TYPES = ['INTEGER', 'BIGINT', 'DOUBLE', 'VARCHAR', 'BOOLEAN', 'DATE', 'TIMESTAMP', 'DECIMAL(18,2)'];
  const setEntry = (idx: number, newKey: string, newVal: string) => {
    const next: Record<string, string> = {};
    entries.forEach(([k, v], i) => {
      if (i === idx) next[newKey] = newVal;
      else next[k] = v;
    });
    onChange(nodeId, { casts: next });
  };
  const removeRow = (idx: number) => {
    const next: Record<string, string> = {};
    entries.forEach(([k, v], i) => { if (i !== idx) next[k] = v; });
    onChange(nodeId, { casts: next });
  };
  const addRow = () => onChange(nodeId, { casts: { ...casts, '': 'VARCHAR' } });
  return (
    <Field label="Cast columns to type">
      <div className="flex flex-col gap-1">
        {entries.map(([col, type], idx) => (
          <div key={idx} className="flex items-center gap-1.5">
            {columns.length > 0 ? (
              <select
                value={col}
                onChange={(e) => setEntry(idx, e.target.value, type)}
                className="flex-1 min-w-0 px-2 py-1 text-sm font-mono border border-slate-200 rounded-md bg-white"
              >
                <option value="">— column —</option>
                {columns.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            ) : (
              <input
                type="text"
                value={col}
                onChange={(e) => setEntry(idx, e.target.value, type)}
                placeholder="column"
                className="flex-1 min-w-0 px-2 py-1 text-sm font-mono border border-slate-200 rounded-md"
              />
            )}
            <span className="text-slate-300 text-xs">→</span>
            <select
              value={type}
              onChange={(e) => setEntry(idx, col, e.target.value)}
              className="px-2 py-1 text-sm font-mono border border-slate-200 rounded-md bg-white"
            >
              {CAST_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            <button
              type="button"
              onClick={() => removeRow(idx)}
              className="text-slate-400 hover:text-red-600 px-1"
              title="Remove cast"
            >
              ✕
            </button>
          </div>
        ))}
        <button
          type="button"
          onClick={addRow}
          className="self-start mt-1 px-2 py-1 text-xs border border-slate-300 hover:bg-slate-50 rounded-md text-slate-700"
        >
          + Add cast
        </button>
      </div>
    </Field>
  );
}

function DerivedColumnConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  // 2026-06-10: rewritten to edit `params.columns` — the array of
  // {name, expression} entries the backend DerivedColumnNode actually
  // executes (nodes/activities.py reads ONLY params.columns). The
  // previous form wrote flat `params.name` / `params.expression`,
  // which the backend never reads — so the node silently ran the
  // default columns:[{new_col, 1}] while the user's input was ignored.
  //
  // Legacy seed: a pipeline configured through the old flat form has
  // top-level name/expression — fold them into the array once so the
  // user's original intent finally takes effect on next save.
  const rows: Array<{ name?: string; expression?: string; window?: any }> =
    Array.isArray(params.columns) && params.columns.length > 0
      ? params.columns
      : (params.name || params.expression)
        ? [{ name: params.name || '', expression: params.expression || '' }]
        : [{ name: '', expression: '' }];

  // Canonical write path: always the `columns` array. Clearing the
  // legacy flat keys stops them from re-seeding stale values later.
  const writeRows = (next: typeof rows) => {
    onChange(nodeId, { columns: next, name: undefined, expression: undefined });
  };
  const setRow = (i: number, patch: { name?: string; expression?: string; replace?: boolean }) => {
    writeRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  };

  // 2026-06-11 (node-audit): column-name validation. Invalid identifiers
  // and SQL keywords fail at run time with engine-speak; duplicates
  // against the input schema silently produced duplicate column names
  // until the backend gained Add-vs-Replace semantics — surface all of
  // it here, at typing time.
  const SQL_KEYWORDS = new Set([
    'select', 'from', 'where', 'group', 'order', 'by', 'join', 'on', 'as',
    'case', 'when', 'then', 'else', 'end', 'and', 'or', 'not', 'null',
    'true', 'false', 'table', 'union', 'having', 'limit', 'distinct',
    'insert', 'update', 'delete', 'create', 'drop',
  ]);
  const nameProblem = (name: string, i: number): string | null => {
    const n = (name || '').trim();
    if (!n) return null;  // emptiness is the required-field star's job
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(n)) {
      return 'Use letters, digits and underscores; must not start with a digit or contain spaces.';
    }
    if (SQL_KEYWORDS.has(n.toLowerCase())) return `'${n}' is a SQL keyword — pick another name.`;
    if (rows.some((r, j) => j !== i && (r.name || '').trim() === n)) {
      return `'${n}' is already used by another derived column below.`;
    }
    return null;
  };

  // Column-chip insertion targets the last-focused expression textarea
  // (cursor-position aware, same UX FilterConfig has).
  const [activeRow, setActiveRow] = useState(0);
  const insertColumn = (col: string) => {
    const i = Math.min(activeRow, rows.length - 1);
    const el = document.getElementById(`derived-${nodeId}-${i}`) as HTMLTextAreaElement | null;
    const val = rows[i]?.expression || '';
    if (el) {
      const start = el.selectionStart;
      const end = el.selectionEnd;
      setRow(i, { expression: val.slice(0, start) + col + val.slice(end) });
      setTimeout(() => { el.focus(); el.setSelectionRange(start + col.length, start + col.length); }, 0);
    } else {
      setRow(i, { expression: val + col });
    }
  };

  return (
    <>
      {columns.length > 0 && (
        <ColumnPicker columns={columns} onInsert={insertColumn} />
      )}
      {rows.map((row, i) => (
        <div key={i} className="border border-slate-200 rounded-lg p-2.5 mb-2 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-semibold text-slate-400 uppercase tracking-wide">Derived column {i + 1}</span>
            {rows.length > 1 && (
              <button
                onClick={() => writeRows(rows.filter((_, j) => j !== i))}
                className="text-[10px] text-slate-400 hover:text-red-500 font-medium"
                title="Remove this derived column"
              >
                Remove
              </button>
            )}
          </div>
          <Field label="Column Name *">
            <TextInput
              value={row.name || ''}
              onChange={(v) => setRow(i, { name: v })}
              placeholder="total_price"
            />
            {nameProblem(row.name || '', i) && (
              <div className="text-[10px] text-red-500 mt-0.5">{nameProblem(row.name || '', i)}</div>
            )}
            {columns.includes((row.name || '').trim()) && (
              <label className="flex items-center gap-1.5 cursor-pointer mt-1">
                <input
                  type="checkbox"
                  checked={(row as any).replace || false}
                  onChange={(e) => setRow(i, { replace: e.target.checked })}
                  className="rounded border-slate-300"
                />
                <span className="text-[10px] text-amber-700">
                  '{(row.name || '').trim()}' already exists on the input — replace it with this value
                  (unchecked = error at run time)
                </span>
              </label>
            )}
          </Field>
          <Field label="Expression *">
            <textarea
              id={`derived-${nodeId}-${i}`}
              value={row.expression || ''}
              onFocus={() => setActiveRow(i)}
              onChange={(e) => setRow(i, { expression: e.target.value })}
              placeholder="quantity * unit_price"
              rows={3}
              className="w-full px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-pipe-300 focus:border-transparent resize-none"
            />
          </Field>
        </div>
      ))}
      <button
        onClick={() => writeRows([...rows, { name: '', expression: '' }])}
        className="w-full px-2.5 py-1.5 text-xs font-medium text-pipe-600 border border-dashed border-pipe-300 rounded-lg hover:bg-pipe-50 transition-colors"
      >
        + Add another column
      </button>
    </>
  );
}

function LookupConfig({ params, nodeId, onChange }: ConfigProps) {
  // 2026-06-11: real Lookup configuration. The previous form exposed a
  // single shared key and silently assumed "connection #2 is the
  // reference table" — input order is canvas layout, not semantics.
  // Now the user explicitly picks WHICH input is the lookup dataset,
  // keys on each side, no-match / multiple-match policy, and which
  // reference columns to append. Mirrors the upgraded backend
  // LookupNode params (nodes/activities.py).
  const edges = useWorkflowStore((s) => s.edges);
  const allNodes = useWorkflowStore((s) => s.nodes);
  const { raw } = useUpstreamSchema(nodeId);

  const upstream = edges
    .filter((e) => e.target === nodeId)
    .map((e) => {
      const n = allNodes.find((x) => x.id === e.source);
      return { id: e.source, label: String((n?.data as any)?.label || e.source) };
    });

  // Reference input: explicit param when valid, else the second
  // connection (matches the backend's legacy fallback).
  const refId =
    params.lookup_input_id && upstream.some((u) => u.id === params.lookup_input_id)
      ? (params.lookup_input_id as string)
      : (upstream[1]?.id || '');
  const mainInput = upstream.find((u) => u.id !== refId) || null;
  const refInput = upstream.find((u) => u.id === refId) || null;

  const schemaFor = (id: string | null | undefined) =>
    (id && raw?.inputs?.find((i) => i.upstream_step_id === id)?.schema?.columns) || [];
  const mainSchema = schemaFor(mainInput?.id);
  const refSchema = schemaFor(refInput?.id);

  const mainKey = (params.main_key || params.lookup_key || '') as string;
  const lookupKey = (params.lookup_key || '') as string;
  const returnCols: string[] = Array.isArray(params.return_columns) ? params.return_columns : [];

  const mainKeyType = mainSchema.find((c) => c.name === mainKey)?.type;
  const refKeyType = refSchema.find((c) => c.name === lookupKey)?.type;
  const typeMismatch = !!(mainKeyType && refKeyType && mainKeyType !== refKeyType);

  const selectCls =
    'w-full px-2.5 py-1.5 text-sm border border-slate-200 rounded-lg bg-white font-mono focus:outline-none focus:ring-2 focus:ring-pipe-300';

  const KeyPicker = ({ value, cols, onPick, placeholder }: {
    value: string; cols: { name: string; type?: string }[]; onPick: (v: string) => void; placeholder: string;
  }) => (
    cols.length > 0 ? (
      <select value={value} onChange={(e) => onPick(e.target.value)} className={selectCls}>
        <option value="">— Pick a column —</option>
        {cols.map((c) => (
          <option key={c.name} value={c.name}>{c.name}{c.type ? ` (${c.type})` : ''}</option>
        ))}
      </select>
    ) : (
      <TextInput value={value} onChange={onPick} placeholder={placeholder} />
    )
  );

  const toggleReturnCol = (col: string) => {
    const next = returnCols.includes(col)
      ? returnCols.filter((c) => c !== col)
      : [...returnCols, col];
    onChange(nodeId, { return_columns: next });
  };

  return (
    <>
      {upstream.length < 2 && (
        <div className="text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-2.5 py-2 leading-relaxed">
          Lookup needs <span className="font-semibold">2 inputs</span>: the main stream and the
          reference dataset. {upstream.length === 0 ? 'None are' : 'Only 1 is'} connected —
          wire both in before running.
        </div>
      )}

      <Field label="Lookup Dataset (reference) *">
        <select
          value={refId}
          onChange={(e) => onChange(nodeId, { lookup_input_id: e.target.value })}
          className={selectCls}
          disabled={upstream.length === 0}
        >
          {upstream.length === 0 && <option value="">— Connect inputs first —</option>}
          {upstream.map((u) => (
            <option key={u.id} value={u.id}>{u.label}</option>
          ))}
        </select>
        {mainInput && (
          <div className="text-[10px] text-slate-400 mt-1">
            Main stream: <span className="font-semibold text-slate-500">{mainInput.label}</span> — every
            other connected input is the main stream.
          </div>
        )}
      </Field>

      <Field label="Main Stream Key *">
        <KeyPicker
          value={mainKey}
          cols={mainSchema}
          onPick={(v) => onChange(nodeId, { main_key: v })}
          placeholder="customer_id"
        />
      </Field>

      <Field label="Lookup Dataset Key *">
        <KeyPicker
          value={lookupKey}
          cols={refSchema}
          onPick={(v) => onChange(nodeId, { lookup_key: v })}
          placeholder="customer_id"
        />
      </Field>

      {typeMismatch && (
        <div className="text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-2.5 py-2 leading-relaxed">
          Key types differ: <span className="font-mono">{mainKeyType}</span> vs{' '}
          <span className="font-mono">{refKeyType}</span>. Values are compared with an implicit
          cast — rows may silently fail to match. Add a Typecast step if matches look wrong.
        </div>
      )}

      <Field label="If No Match">
        <select
          value={params.no_match || 'keep'}
          onChange={(e) => onChange(nodeId, { no_match: e.target.value })}
          className={selectCls}
        >
          <option value="keep">Keep row — added columns are NULL (left join)</option>
          <option value="drop">Drop row (inner join)</option>
        </select>
      </Field>

      <Field label="If Multiple Matches">
        <select
          value={params.multiple_match || 'all'}
          onChange={(e) => onChange(nodeId, { multiple_match: e.target.value })}
          className={selectCls}
        >
          <option value="all">Return all matches — main rows may duplicate</option>
          <option value="first">First match only — row count preserved</option>
        </select>
      </Field>

      <Field label="Columns to Add from Lookup">
        {refSchema.length > 0 ? (
          <>
            <div className="flex flex-wrap gap-1.5">
              {refSchema.filter((c) => c.name !== lookupKey).map((c) => {
                const active = returnCols.includes(c.name);
                return (
                  <button
                    key={c.name}
                    onClick={() => toggleReturnCol(c.name)}
                    className={`px-2 py-1 text-[10px] font-mono rounded-md border transition-colors ${
                      active
                        ? 'bg-pipe-50 text-pipe-700 border-pipe-300'
                        : 'bg-white text-slate-500 border-slate-200 hover:border-slate-300'
                    }`}
                    title={c.type || ''}
                  >
                    {active ? '✓ ' : ''}{c.name}
                  </button>
                );
              })}
            </div>
            <div className="text-[10px] text-slate-400 mt-1">
              None selected = all columns except the key. A column whose name already exists on
              the main stream is appended as <span className="font-mono">name_lookup</span>.
            </div>
          </>
        ) : (
          <div className="text-[10px] text-slate-400">
            Schema not available yet — all reference columns except the key will be added.
          </div>
        )}
      </Field>
    </>
  );
}

function UnionConfig({ params, nodeId, onChange }: ConfigProps) {
  // 2026-06-11 (node-audit): the old dropdown offered 'by_name' but the
  // engine treated everything except 'all' as UNION DISTINCT (positional)
  // — a silent dead option. The three modes now match the backend, and
  // by_name is a true schema union (UNION ALL BY NAME).
  const MODE_HELP: Record<string, string> = {
    all: 'Stack rows, keep duplicates. Columns matched by POSITION — inputs must have the same shape.',
    distinct: 'Stack rows, drop exact duplicate rows. Columns matched by position.',
    by_name: 'Schema union — columns matched by NAME; a column missing from one input becomes NULL. Best for combining files with drifting schemas.',
  };
  const mode = params.mode || 'all';

  // Surface the datasets being stacked so the user can confirm BOTH inputs
  // are wired and eyeball schema drift before choosing a mode. Previously
  // this config showed only the mode dropdown — nothing about the two
  // inputs it combines.
  const edges = useWorkflowStore((s) => s.edges);
  const allNodes = useWorkflowStore((s) => s.nodes);
  const { raw } = useUpstreamSchema(nodeId);
  const inputs = edges
    .filter((e) => e.target === nodeId)
    .map((e) => {
      const n = allNodes.find((x) => x.id === e.source);
      const cols = (raw?.inputs?.find((i) => i.upstream_step_id === e.source)?.schema?.columns || [])
        .map((c) => c.name);
      return { id: e.source, label: String((n?.data as any)?.label || e.source), cols };
    });
  // Schema-drift heads-up for positional modes (all/distinct match by column
  // POSITION; mismatched names usually means the user wants by_name).
  const namesA = inputs[0]?.cols.join('|');
  const drift = inputs.length >= 2 && inputs.some((i) => i.cols.join('|') !== namesA);

  return (
    <>
      <Field label={`Input Datasets (${inputs.length})`}>
        {inputs.length === 0 ? (
          <div className="text-[11px] text-amber-600">Connect two or more sources into this node to union them.</div>
        ) : (
          <div className="space-y-1.5">
            {inputs.map((inp, i) => (
              <div key={inp.id} className="rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-semibold text-slate-700 truncate">{i + 1}. {inp.label}</span>
                  <span className="text-[10px] text-slate-400 shrink-0">{inp.cols.length} cols</span>
                </div>
                {inp.cols.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1">
                    {inp.cols.slice(0, 14).map((c) => (
                      <span key={c} className="text-[10px] px-1.5 py-0.5 rounded bg-white border border-slate-200 font-mono text-slate-600">{c}</span>
                    ))}
                    {inp.cols.length > 14 && <span className="text-[10px] text-slate-400 pt-0.5">+{inp.cols.length - 14}</span>}
                  </div>
                )}
              </div>
            ))}
            {inputs.length === 1 && (
              <div className="text-[11px] text-amber-600">Union needs 2+ inputs — connect another source.</div>
            )}
            {drift && mode !== 'by_name' && (
              <div className="text-[11px] text-amber-600">
                Inputs have different columns. "{mode === 'all' ? 'Union All' : 'Union Distinct'}" matches by position — switch to <span className="font-semibold">Union By Name</span> to align by column name.
              </div>
            )}
          </div>
        )}
      </Field>

      <Field label="Union Mode">
        <select
          value={mode}
          onChange={(e) => onChange(nodeId, { mode: e.target.value })}
          className="w-full px-2.5 py-1.5 text-sm border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-pipe-300"
        >
          <option value="all">Union All (keep duplicates)</option>
          <option value="distinct">Union Distinct (drop duplicate rows)</option>
          <option value="by_name">Union By Name (schema union, NULL-fill missing)</option>
        </select>
        <div className="text-[10px] text-slate-400 mt-1">{MODE_HELP[mode] || ''}</div>
      </Field>
    </>
  );
}

function PivotConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  // 2026-05-30: Pivot/Value columns are dropdowns when the upstream
  // schema is known. Previously both were free-text inputs, which
  // forced the user to type column names from memory and silently
  // ran SELECT * SUM('typo') when they made an error.
  const ColumnField = ({ label, value, paramKey, placeholder }: {
    label: string; value: string; paramKey: string; placeholder: string;
  }) => (
    <Field label={label}>
      {columns.length > 0 ? (
        <select
          value={value}
          onChange={(e) => onChange(nodeId, { [paramKey]: e.target.value })}
          className="w-full px-2.5 py-1.5 text-sm border border-slate-200 rounded-lg bg-white font-mono focus:outline-none focus:ring-2 focus:ring-pipe-300"
        >
          <option value="">— Pick a column —</option>
          {columns.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      ) : (
        <TextInput
          value={value}
          onChange={(v) => onChange(nodeId, { [paramKey]: v })}
          placeholder={placeholder}
        />
      )}
    </Field>
  );

  // 2026-06-11 (node-audit): Row Columns exposed. The backend has
  // always supported `group_by` (falling back to GROUP BY ALL), but the
  // UI never showed it — so users couldn't answer "what are we pivoting
  // BY?" The implicit ALL fallback often grouped by unexpected columns.
  const rowCols: string[] = Array.isArray(params.group_by) ? params.group_by : [];
  const toggleRowCol = (col: string) => {
    const next = rowCols.includes(col) ? rowCols.filter((c) => c !== col) : [...rowCols, col];
    onChange(nodeId, { group_by: next });
  };

  return (
    <>
      <Field label="Row Columns (group by)">
        {columns.length > 0 ? (
          <div className="flex flex-wrap gap-1.5 mb-1">
            {columns.map((col) => (
              <button key={col} onClick={() => toggleRowCol(col)}
                className={`text-xs px-2 py-1 rounded-lg font-mono border transition-colors ${
                  rowCols.includes(col)
                    ? 'bg-pipe-100 text-pipe-700 border-pipe-300'
                    : 'bg-white text-slate-500 border-slate-200 hover:border-pipe-200'
                }`}
              >{rowCols.includes(col) && '✓ '}{col}</button>
            ))}
          </div>
        ) : (
          <TextInput
            value={rowCols.join(', ')}
            onChange={(v) => onChange(nodeId, { group_by: v.split(',').map((s: string) => s.trim()).filter(Boolean) })}
            placeholder="month"
          />
        )}
        <div className="text-[10px] text-slate-400 mt-0.5">
          The columns each output row is keyed by (e.g. month). Empty = group by every remaining column.
        </div>
      </Field>
      <ColumnField label="Pivot Column" value={params.pivot_column || ''}
        paramKey="pivot_column" placeholder="category" />
      <ColumnField label="Value Column" value={params.value_column || ''}
        paramKey="value_column" placeholder="amount" />
      <Field label="Aggregate Function">
        <Select
          value={params.agg_function || 'SUM'}
          onChange={(v) => onChange(nodeId, { agg_function: v })}
          options={['SUM', 'COUNT', 'AVG', 'MIN', 'MAX']}
        />
      </Field>
    </>
  );
}

function UnpivotConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  // 2026-05-30: chip-toggle picker matching Aggregate Group By + Dedup
  // Keys patterns. The user picks columns by tapping them rather than
  // typing the names with commas.
  const selectedCols: string[] = Array.isArray(params.columns)
    ? params.columns
    : (params.columns ? String(params.columns).split(',').map((s) => s.trim()).filter(Boolean) : []);

  const toggleCol = (col: string) => {
    const next = selectedCols.includes(col)
      ? selectedCols.filter((c) => c !== col)
      : [...selectedCols, col];
    onChange(nodeId, { columns: next });
  };

  return (
    <>
      <Field label="Columns to unpivot">
        {columns.length > 0 ? (
          <div className="flex flex-wrap gap-1.5 mb-1">
            {columns.map((col) => (
              <button
                key={col}
                onClick={() => toggleCol(col)}
                className={`text-xs px-2 py-1 rounded-lg font-mono border transition-colors ${
                  selectedCols.includes(col)
                    ? 'bg-pipe-100 text-pipe-700 border-pipe-300'
                    : 'bg-white text-slate-500 border-slate-200 hover:border-pipe-200'
                }`}
              >
                {selectedCols.includes(col) && '✓ '}{col}
              </button>
            ))}
          </div>
        ) : (
          <TextInput
            value={selectedCols.join(', ')}
            onChange={(v) => onChange(nodeId, { columns: v.split(',').map((s: string) => s.trim()).filter(Boolean) })}
            placeholder="q1, q2, q3, q4"
          />
        )}
      </Field>
      {/* 2026-06-11 (node-audit): explicit identifier columns. Empty = keep
          all non-unpivoted columns (DuckDB default). */}
      {columns.length > 0 && (() => {
        const ids: string[] = Array.isArray(params.id_columns) ? params.id_columns : [];
        const toggleId = (col: string) => onChange(nodeId, {
          id_columns: ids.includes(col) ? ids.filter((c) => c !== col) : [...ids, col],
        });
        const candidates = columns.filter((c) => !selectedCols.includes(c));
        return (
          <Field label="Identifier columns (keep)">
            <div className="flex flex-wrap gap-1.5 mb-1">
              {candidates.map((col) => (
                <button key={col} onClick={() => toggleId(col)}
                  className={`text-xs px-2 py-1 rounded-lg font-mono border transition-colors ${
                    ids.includes(col)
                      ? 'bg-pipe-100 text-pipe-700 border-pipe-300'
                      : 'bg-white text-slate-500 border-slate-200 hover:border-pipe-200'
                  }`}
                >{ids.includes(col) && '✓ '}{col}</button>
              ))}
            </div>
            <div className="text-[10px] text-slate-400">None selected = keep every column that isn't being unpivoted.</div>
          </Field>
        );
      })()}
      <Field label="Name Column">
        <TextInput
          value={params.name_column || 'variable'}
          onChange={(v) => onChange(nodeId, { name_column: v })}
          placeholder="variable"
        />
      </Field>
      <Field label="Value Column">
        <TextInput
          value={params.value_column || 'value'}
          onChange={(v) => onChange(nodeId, { value_column: v })}
          placeholder="value"
        />
      </Field>
      <Field label="Null Handling">
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={params.include_nulls || false}
            onChange={(e) => onChange(nodeId, { include_nulls: e.target.checked })}
            className="rounded border-slate-300"
          />
          <span className="text-xs text-slate-600">Keep rows where the value is NULL (dropped by default)</span>
        </label>
      </Field>
    </>
  );
}

function WindowConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const WIN_FUNCS = ['ROW_NUMBER', 'RANK', 'DENSE_RANK', 'NTILE', 'LAG', 'LEAD',
    'SUM', 'AVG', 'COUNT', 'MIN', 'MAX', 'FIRST_VALUE', 'LAST_VALUE', 'NTH_VALUE', 'PERCENT_RANK', 'CUME_DIST'];
  // 2026-05-30 audit fix: canonical write key is `window_functions`
  // (matches backend WindowNode.execute). Read both `window_functions`
  // and legacy `functions` so saved pipelines still load. Order direction:
  // canonical `order_direction`; legacy `sort_direction`.
  const functions = Array.isArray(params.window_functions)
    ? params.window_functions
    : (Array.isArray(params.functions) ? params.functions : [
        { function: params.function || 'ROW_NUMBER', column: params.column || '', alias: params.alias || 'window_result' }
      ]);
  const partBy = Array.isArray(params.partition_by) ? params.partition_by.join(', ') : (params.partition_by || '');

  const updateFn = (i: number, patch: Record<string, any>) => {
    const updated = functions.map((f: any, idx: number) => idx === i ? { ...f, ...patch } : f);
    onChange(nodeId, { window_functions: updated });
  };
  const removeFn = (i: number) => onChange(nodeId, { window_functions: functions.filter((_: any, idx: number) => idx !== i) });
  const needsColumn = (fn: string) => !['ROW_NUMBER', 'RANK', 'DENSE_RANK', 'PERCENT_RANK', 'CUME_DIST'].includes(fn);

  // 2026-05-30: partition/order columns now render as chip toggles
  // when upstream schema is known — same pattern as Aggregate Group By.
  // Free-text fallback preserved when schema unavailable.
  const selectedPart: string[] = Array.isArray(params.partition_by)
    ? params.partition_by
    : (params.partition_by ? String(params.partition_by).split(',').map((s) => s.trim()).filter(Boolean) : []);
  // 2026-06-11 (node-audit): order_by entries carry per-column direction
  // ("col DIR"), matching the Sort node + the backend WindowNode parser.
  // Replaces the old global "Sort Direction" select, which conflicted with
  // per-column intent (you can't sort col A asc and col B desc with one
  // global toggle).
  const ordEntries: { column: string; direction: 'ASC' | 'DESC' }[] =
    (Array.isArray(params.order_by) ? params.order_by
      : (params.order_by ? String(params.order_by).split(',') : []))
      .map((t: string) => String(t).trim()).filter(Boolean)
      .map((tok: string) => {
        const parts = tok.split(/\s+/);
        return { column: parts[0] || '', direction: ((parts[1] || '').toUpperCase() === 'DESC' ? 'DESC' : 'ASC') as 'ASC' | 'DESC' };
      });
  const writeOrd = (next: { column: string; direction: string }[]) =>
    onChange(nodeId, { order_by: next.filter((e) => e.column.trim()).map((e) => `${e.column.trim()} ${e.direction}`) });
  const updateOrd = (i: number, patch: Partial<{ column: string; direction: string }>) =>
    writeOrd(ordEntries.map((e, idx) => (idx === i ? { ...e, ...patch } : e)));

  const togglePart = (col: string) => {
    const next = selectedPart.includes(col)
      ? selectedPart.filter((c: string) => c !== col)
      : [...selectedPart, col];
    onChange(nodeId, { partition_by: next });
  };

  return (
    <>
      <Field label="Partition by">
        {columns.length > 0 ? (
          <div className="flex flex-wrap gap-1.5 mb-1">
            {columns.map((col) => (
              <button key={col} onClick={() => togglePart(col)}
                className={`text-xs px-2 py-1 rounded-lg font-mono border transition-colors ${
                  selectedPart.includes(col)
                    ? 'bg-pipe-100 text-pipe-700 border-pipe-300'
                    : 'bg-white text-slate-500 border-slate-200 hover:border-pipe-200'
                }`}
              >{selectedPart.includes(col) && '✓ '}{col}</button>
            ))}
          </div>
        ) : (
          <TextInput value={partBy}
            onChange={(v) => onChange(nodeId, { partition_by: v.split(',').map((s: string) => s.trim()).filter(Boolean) })}
            placeholder="category, region" />
        )}
      </Field>
      <Field label="Order by">
        <div className="space-y-1.5">
          {ordEntries.map((e, i) => (
            <div key={i} className="flex items-center gap-1">
              {columns.length > 0 ? (
                <select value={e.column} onChange={(ev) => updateOrd(i, { column: ev.target.value })}
                  className="flex-1 px-2 py-1 text-xs border border-slate-200 rounded bg-white font-mono">
                  <option value="">— Column —</option>
                  {columns.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              ) : (
                <input value={e.column} onChange={(ev) => updateOrd(i, { column: ev.target.value })}
                  placeholder="column" className="flex-1 px-2 py-1 text-xs border border-slate-200 rounded font-mono" />
              )}
              <select value={e.direction} onChange={(ev) => updateOrd(i, { direction: ev.target.value })}
                className="w-20 px-1 py-1 text-xs border border-slate-200 rounded bg-white">
                <option value="ASC">ASC</option>
                <option value="DESC">DESC</option>
              </select>
              <button onClick={() => writeOrd(ordEntries.filter((_, idx) => idx !== i))}
                className="text-red-400 hover:text-red-600 text-xs px-1">×</button>
            </div>
          ))}
          <button onClick={() => writeOrd([...ordEntries, { column: columns[0] || '', direction: 'ASC' }])}
            className="text-xs text-pipe-600 hover:text-pipe-800 font-medium">+ Add order column</button>
          <div className="text-[10px] text-slate-400">Ranking and LAG/LEAD/running-total functions need an Order By.</div>
        </div>
      </Field>
      <div className="text-[12px] font-semibold text-slate-700 uppercase tracking-wider mb-1 mt-2">Window Functions</div>
      {functions.map((f: any, i: number) => (
        <div key={i} className="p-2 bg-slate-50 rounded-lg border border-slate-100 mb-1.5 space-y-1">
          <div className="flex gap-1.5 items-center">
            <select value={f.function} onChange={(e) => updateFn(i, { function: e.target.value })}
              className="w-28 px-1.5 py-1 text-xs border border-slate-200 rounded bg-white">
              {WIN_FUNCS.map((fn) => <option key={fn} value={fn}>{fn}</option>)}
            </select>
            {needsColumn(f.function) && (
              columns.length > 0 ? (
                <select value={f.column || ''} onChange={(e) => updateFn(i, { column: e.target.value })}
                  className="flex-1 px-1.5 py-1 text-xs border border-slate-200 rounded font-mono bg-white">
                  <option value="">— Column —</option>
                  {columns.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              ) : (
                <input value={f.column || ''} onChange={(e) => updateFn(i, { column: e.target.value })}
                  placeholder="column" className="flex-1 px-1.5 py-1 text-xs border border-slate-200 rounded font-mono" />
              )
            )}
            <input value={f.alias || ''} onChange={(e) => updateFn(i, { alias: e.target.value })}
              placeholder="alias" className="w-20 px-1.5 py-1 text-xs border border-slate-200 rounded font-mono" />
            <button onClick={() => removeFn(i)} className="text-red-400 hover:text-red-600 text-xs px-1 shrink-0">×</button>
          </div>
          {f.function === 'NTILE' && (
            <input type="number" value={f.n ?? 4} onChange={(e) => updateFn(i, { n: parseInt(e.target.value) || 4 })}
              placeholder="N buckets" className="w-24 px-2 py-1 text-xs border border-slate-200 rounded" />
          )}
          {(f.function === 'LAG' || f.function === 'LEAD') && (
            <input type="number" value={f.offset ?? 1} onChange={(e) => updateFn(i, { offset: parseInt(e.target.value) || 1 })}
              placeholder="Offset" className="w-24 px-2 py-1 text-xs border border-slate-200 rounded" />
          )}
        </div>
      ))}
      <button onClick={() => onChange(nodeId, { window_functions: [...functions, { function: 'ROW_NUMBER', column: '', alias: `win_${functions.length + 1}` }] })}
        className="text-xs text-pipe-600 hover:text-pipe-700 font-medium">+ Add Window Function</button>
      <Field label="Frame (optional)">
        <TextInput value={params.frame || ''} onChange={(v) => onChange(nodeId, { frame: v })}
          placeholder="ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING" />
      </Field>
    </>
  );
}

function SampleConfig({ params, nodeId, onChange }: ConfigProps) {
  // 2026-06-11 (node-audit): redesigned around an explicit, mutually-
  // exclusive Sampling Mode. The old form showed Count AND Fraction side
  // by side with undefined precedence — worse, `fraction` was a dead
  // param the engine never read, so a fraction-only config silently
  // sampled the default 100 rows. Mode/method/seed now mirror the
  // backend SampleNode contract exactly.
  const mode: string = params.mode || (params.fraction != null && params.count == null ? 'percent' : 'rows');
  const method: string = params.method || 'first';
  const percentVal = params.percent != null ? params.percent
    : params.fraction != null ? params.fraction * 100 : '';

  const count = params.count;
  const countInvalid = count != null && (!Number.isFinite(Number(count)) || Number(count) <= 0);
  const pct = percentVal === '' ? null : Number(percentVal);
  const pctInvalid = pct != null && (!Number.isFinite(pct) || pct <= 0 || pct > 100);

  const radioCls = (active: boolean) =>
    `flex-1 px-2.5 py-1.5 text-xs font-medium rounded-lg border text-center transition-colors ${
      active ? 'bg-pipe-50 text-pipe-700 border-pipe-300' : 'bg-white text-slate-500 border-slate-200 hover:border-slate-300'
    }`;

  return (
    <>
      <Field label="Sampling Mode">
        <div className="flex gap-1.5">
          <button className={radioCls(mode === 'rows')} onClick={() => onChange(nodeId, { mode: 'rows' })}>
            Fixed Rows
          </button>
          <button className={radioCls(mode === 'percent')} onClick={() => onChange(nodeId, { mode: 'percent' })}>
            Percentage
          </button>
        </div>
      </Field>
      {mode === 'percent' ? (
        <Field label="Percent (0–100) *">
          <TextInput
            value={String(percentVal)}
            onChange={(v) => onChange(nodeId, { percent: v === '' ? undefined : parseFloat(v), fraction: undefined })}
            placeholder="10"
          />
          {pctInvalid && (
            <div className="text-[10px] text-red-500 mt-0.5">Percent must be between 0 and 100.</div>
          )}
        </Field>
      ) : (
        <Field label="Row Count *">
          <TextInput
            value={String(count ?? '')}
            onChange={(v) => onChange(nodeId, { count: v === '' ? undefined : parseInt(v) })}
            placeholder="100"
          />
          {countInvalid && (
            <div className="text-[10px] text-red-500 mt-0.5">Count must be a number greater than 0.</div>
          )}
        </Field>
      )}
      <Field label="Sampling Method">
        <div className="flex gap-1.5">
          <button className={radioCls(method === 'first')} onClick={() => onChange(nodeId, { method: 'first' })}>
            First Rows
          </button>
          <button className={radioCls(method === 'random')} onClick={() => onChange(nodeId, { method: 'random' })}>
            Random
          </button>
        </div>
        <div className="text-[10px] text-slate-400 mt-1">
          {method === 'first'
            ? 'Deterministic — takes the first rows in input order. Fast; same result every run.'
            : 'Statistical sample — different rows each run unless a seed is set.'}
        </div>
      </Field>
      {method === 'random' && (
        <Field label="Random Seed (optional)">
          <TextInput
            value={String(params.seed ?? '')}
            onChange={(v) => onChange(nodeId, { seed: v === '' ? undefined : parseInt(v) })}
            placeholder="42"
          />
          <div className="text-[10px] text-slate-400 mt-0.5">
            Set a seed to get the same sample on every run — useful for tests and ML experiments.
          </div>
        </Field>
      )}
    </>
  );
}

function ValidateConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const rules = params.rules || [];
  // P1-6 (2026-05-18): for checks that require a value (in_range, regex,
  // min_length, max_length), render a value input below the column/check
  // row. Previously the value field was missing entirely — users could
  // configure a regex check with no pattern, and the rule silently no-op'd.
  const VALUE_REQUIRED = new Set(['in_range', 'regex', 'min_length', 'max_length', 'eq', 'ne', 'gt', 'lt', 'gte', 'lte']);
  return (
    <>
      <div className="text-[12px] font-semibold text-slate-700 uppercase tracking-wider mb-1">
        Validation Rules
      </div>
      {rules.map((r: any, i: number) => {
        const check = r.check || 'not_null';
        const needsValue = VALUE_REQUIRED.has(check);
        return (
        <div key={i} className="mb-2 p-2 bg-slate-50 rounded-lg">
          {/* 2026-05-30: column dropdown when upstream schema is known. */}
          {columns.length > 0 ? (
            <select
              value={r.column || ''}
              onChange={(e) => {
                const updated = [...rules];
                updated[i] = { ...r, column: e.target.value };
                onChange(nodeId, { rules: updated });
              }}
              className="w-full px-2 py-1 text-xs border border-slate-200 rounded mb-1 bg-white font-mono"
            >
              <option value="">— Column —</option>
              {columns.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          ) : (
            <input
              value={r.column || ''}
              onChange={(e) => {
                const updated = [...rules];
                updated[i] = { ...r, column: e.target.value };
                onChange(nodeId, { rules: updated });
              }}
              placeholder="column"
              className="w-full px-2 py-1 text-xs border border-slate-200 rounded mb-1 font-mono"
            />
          )}
          <select
            value={check}
            onChange={(e) => {
              const updated = [...rules];
              updated[i] = { ...r, check: e.target.value };
              onChange(nodeId, { rules: updated });
            }}
            className="w-full px-2 py-1 text-xs border border-slate-200 rounded bg-white"
          >
            {['not_null', 'unique', 'positive', 'in_range', 'regex', 'min_length', 'max_length'].map((c) => (
              <option key={c} value={c}>{c.replace(/_/g, ' ')}</option>
            ))}
          </select>
          {needsValue && (
            <input
              value={r.value || ''}
              onChange={(e) => {
                const updated = [...rules];
                updated[i] = { ...r, value: e.target.value };
                onChange(nodeId, { rules: updated });
              }}
              placeholder={
                check === 'in_range' ? 'min..max (e.g. 0..100)'
                  : check === 'regex' ? '^[a-z]+$'
                  : check === 'min_length' || check === 'max_length' ? 'character count (e.g. 5)'
                  : 'value'
              }
              // #4 / Tier-7 (2026-05-19): auto-focus the value input on
              // first render when the check is value-required and value
              // is still empty. The input newly mounts each time the
              // user picks a value-requiring check, so autoFocus runs.
              autoFocus={!r.value}
              className="w-full mt-1 px-2 py-1 text-xs border border-slate-200 rounded font-mono"
            />
          )}
        </div>
      );})}
      <button
        onClick={() => onChange(nodeId, { rules: [...rules, { column: '', check: 'not_null' }] })}
        className="text-xs text-pipe-600 hover:text-pipe-700 font-medium"
      >
        + Add Rule
      </button>
    </>
  );
}

function ConditionalSplitConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const conditions = params.conditions || [];
  const updateCond = (i: number, patch: Record<string, any>) => {
    onChange(nodeId, { conditions: conditions.map((c: any, idx: number) => idx === i ? { ...c, ...patch } : c) });
  };
  // 2026-05-30 audit fix: canonical fields are `mode` + `active_output`
  // (matches backend ConditionalSplitNode.execute). Legacy frontend
  // wrote `split_mode` + `filter_output` which the executor silently
  // ignored. Read either, write canonical. Also re-label 'all' → 'first_match'
  // so the option value matches what the backend dispatches on.
  const modeValue = params.mode ?? params.split_mode ?? 'first_match';
  const filterOutputValue = params.active_output ?? params.filter_output ?? '';
  return (
    <>
      <Field label="Mode">
        <Select value={modeValue} onChange={(v) => onChange(nodeId, { mode: v })}
          options={['first_match', 'filter']} />
      </Field>
      <div className="text-xs text-slate-500 -mt-1 mb-2">
        {modeValue === 'filter'
          ? 'Filter mode: returns rows matching a specific output only.'
          : 'first_match: each row gets the first matching branch label (via _split_output column).'}
      </div>
      <div className="text-[12px] font-semibold text-slate-700 uppercase tracking-wider mb-1">Branches</div>
      {conditions.map((c: any, i: number) => (
        <div key={i} className="mb-2 p-2 bg-slate-50 rounded-lg space-y-1">
          <div className="flex items-center gap-1">
            <input value={c.name || ''} onChange={(e) => updateCond(i, { name: e.target.value })}
              placeholder="Branch name (output label)" className="flex-1 px-2 py-1 text-xs border border-slate-200 rounded" />
            <button onClick={() => onChange(nodeId, { conditions: conditions.filter((_: any, idx: number) => idx !== i) })}
              className="text-red-400 hover:text-red-600 text-xs px-1 shrink-0">×</button>
          </div>
          <textarea value={c.condition || ''} onChange={(e) => updateCond(i, { condition: e.target.value })}
            placeholder="amount > 100" rows={2}
            className="w-full px-2 py-1 text-xs border border-slate-200 rounded font-mono resize-none" />
        </div>
      ))}
      <button onClick={() => onChange(nodeId, { conditions: [...conditions, { name: `Branch ${conditions.length + 1}`, condition: '' }] })}
        className="text-xs text-pipe-600 hover:text-pipe-700 font-medium">+ Add Branch</button>
      <Field label="Default Output Label">
        <TextInput value={params.default_output || 'Default'} onChange={(v) => onChange(nodeId, { default_output: v })}
          placeholder="Default" />
      </Field>
      {modeValue === 'filter' && (
        <Field label="Active Output (branch to return)">
          <TextInput value={filterOutputValue} onChange={(v) => onChange(nodeId, { active_output: v })}
            placeholder="Branch name to return" />
        </Field>
      )}
    </>
  );
}

/* ── Source Configs ── */

function JsonSourceConfig({ params, nodeId, onChange }: ConfigProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<Array<{ name: string; size: number }>>([]);
  const [uploading, setUploading] = useState(false);

  useEffect(() => {
    api.listFiles().then(setFiles).catch(() => {});
  }, []);

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      // Pass the previous file_path so the backend cleans it up after a
      // successful upload — keeps the data dir from accumulating orphans
      // when a user swaps a node's source file.
      const previous = (params.file_path as string | undefined) || undefined;
      const result = await api.uploadFile(file, { replaces: previous });
      onChange(nodeId, { file_path: result.filename });
      const updated = await api.listFiles();
      setFiles(updated);
    } catch (err: any) {
      console.error('Upload error:', err);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  return (
    <>
      <Field label="JSON File">
        <select
          value={params.file_path || ''}
          onChange={(e) => onChange(nodeId, { file_path: e.target.value })}
          className="w-full px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 bg-white mb-1.5"
        >
          <option value="">Select a file...</option>
          {files.filter(f => f.name.endsWith('.json') || f.name.endsWith('.jsonl')).map((f) => (
            <option key={f.name} value={f.name}>{f.name} ({(f.size / 1024).toFixed(1)} KB)</option>
          ))}
        </select>
        <input ref={fileRef} type="file" accept=".json,.jsonl" onChange={handleUpload} className="hidden" />
        <button onClick={() => fileRef.current?.click()} disabled={uploading}
          className="w-full px-2.5 py-1.5 text-xs border border-dashed border-slate-300 rounded-lg text-slate-500 hover:border-pulse-400 hover:text-pipe-600 hover:bg-pulse-50 transition-colors disabled:opacity-50">
          {uploading ? 'Uploading...' : '+ Upload JSON File'}
        </button>
        {params.file_path && <VolumeTierBadge filePath={params.file_path} />}
      </Field>
      <Field label="JSON Path (optional)">
        <TextInput value={params.json_path || ''} onChange={(v) => onChange(nodeId, { json_path: v })} placeholder="$.data[*]" />
      </Field>
      <Field label="Format">
        <Select value={params.format || 'json'} onChange={(v) => onChange(nodeId, { format: v })} options={['json', 'jsonl', 'json_array']} />
      </Field>
      <Field label="Encoding">
        <Select value={params.encoding || 'utf-8'} onChange={(v) => onChange(nodeId, { encoding: v })} options={['utf-8', 'utf-16', 'ascii', 'latin-1']} />
      </Field>
    </>
  );
}

function ParquetSourceConfig({ params, nodeId, onChange }: ConfigProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<Array<{ name: string; size: number }>>([]);
  const [uploading, setUploading] = useState(false);

  useEffect(() => {
    api.listFiles().then(setFiles).catch(() => {});
  }, []);

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      // Pass the previous file_path so the backend cleans it up after a
      // successful upload — keeps the data dir from accumulating orphans
      // when a user swaps a node's source file.
      const previous = (params.file_path as string | undefined) || undefined;
      const result = await api.uploadFile(file, { replaces: previous });
      onChange(nodeId, { file_path: result.filename });
      const updated = await api.listFiles();
      setFiles(updated);
    } catch (err: any) {
      console.error('Upload error:', err);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  return (
    <>
      <Field label="Parquet File">
        <select
          value={params.file_path || ''}
          onChange={(e) => onChange(nodeId, { file_path: e.target.value })}
          className="w-full px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 bg-white mb-1.5"
        >
          <option value="">Select a file...</option>
          {files.filter(f => f.name.endsWith('.parquet') || f.name.endsWith('.pq')).map((f) => (
            <option key={f.name} value={f.name}>{f.name} ({(f.size / 1024).toFixed(1)} KB)</option>
          ))}
        </select>
        <input ref={fileRef} type="file" accept=".parquet,.pq" onChange={handleUpload} className="hidden" />
        <button onClick={() => fileRef.current?.click()} disabled={uploading}
          className="w-full px-2.5 py-1.5 text-xs border border-dashed border-slate-300 rounded-lg text-slate-500 hover:border-pulse-400 hover:text-pipe-600 hover:bg-pulse-50 transition-colors disabled:opacity-50">
          {uploading ? 'Uploading...' : '+ Upload Parquet File'}
        </button>
        {params.file_path && <VolumeTierBadge filePath={params.file_path} />}
      </Field>
      <Field label="Row Group (optional)">
        <TextInput value={String(params.row_group ?? '')} onChange={(v) => onChange(nodeId, { row_group: v ? parseInt(v) : undefined })} placeholder="0" />
      </Field>
      <Field label="Columns (comma-separated, blank = all)">
        <TextInput value={params.columns_select || ''} onChange={(v) => onChange(nodeId, { columns_select: v })} placeholder="id, name, amount" />
      </Field>
    </>
  );
}

function ExcelSourceConfig({ params, nodeId, onChange }: ConfigProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<Array<{ name: string; size: number }>>([]);
  const [uploading, setUploading] = useState(false);

  useEffect(() => {
    api.listFiles().then(setFiles).catch(() => {});
  }, []);

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      // Pass the previous file_path so the backend cleans it up after a
      // successful upload — keeps the data dir from accumulating orphans
      // when a user swaps a node's source file.
      const previous = (params.file_path as string | undefined) || undefined;
      const result = await api.uploadFile(file, { replaces: previous });
      onChange(nodeId, { file_path: result.filename });
      const updated = await api.listFiles();
      setFiles(updated);
    } catch (err: any) {
      console.error('Upload error:', err);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  return (
    <>
      <Field label="Excel File">
        <select
          value={params.file_path || ''}
          onChange={(e) => onChange(nodeId, { file_path: e.target.value })}
          className="w-full px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 bg-white mb-1.5"
        >
          <option value="">Select a file...</option>
          {files.filter(f => f.name.endsWith('.xlsx') || f.name.endsWith('.xls')).map((f) => (
            <option key={f.name} value={f.name}>{f.name} ({(f.size / 1024).toFixed(1)} KB)</option>
          ))}
        </select>
        <input ref={fileRef} type="file" accept=".xlsx,.xls" onChange={handleUpload} className="hidden" />
        <button onClick={() => fileRef.current?.click()} disabled={uploading}
          className="w-full px-2.5 py-1.5 text-xs border border-dashed border-slate-300 rounded-lg text-slate-500 hover:border-pulse-400 hover:text-pipe-600 hover:bg-pulse-50 transition-colors disabled:opacity-50">
          {uploading ? 'Uploading...' : '+ Upload Excel File'}
        </button>
        {params.file_path && <VolumeTierBadge filePath={params.file_path} />}
      </Field>
      <Field label="Sheet Name">
        <TextInput value={params.sheet_name || ''} onChange={(v) => onChange(nodeId, { sheet_name: v })} placeholder="Sheet1" />
      </Field>
      <Field label="Header Row">
        <TextInput value={String(params.header_row ?? 1)} onChange={(v) => onChange(nodeId, { header_row: parseInt(v) || 1 })} placeholder="1" />
      </Field>
      <Field label="Data Range (optional)">
        <TextInput value={params.range || ''} onChange={(v) => onChange(nodeId, { range: v })} placeholder="A1:Z100" />
      </Field>
      <Field label="Skip Rows">
        <TextInput value={String(params.skip_rows ?? 0)} onChange={(v) => onChange(nodeId, { skip_rows: parseInt(v) || 0 })} placeholder="0" />
      </Field>
    </>
  );
}

function XmlSourceConfig({ params, nodeId, onChange }: ConfigProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<Array<{ name: string; size: number }>>([]);
  const [uploading, setUploading] = useState(false);

  useEffect(() => {
    api.listFiles().then(setFiles).catch(() => {});
  }, []);

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      // Pass the previous file_path so the backend cleans it up after a
      // successful upload — keeps the data dir from accumulating orphans
      // when a user swaps a node's source file.
      const previous = (params.file_path as string | undefined) || undefined;
      const result = await api.uploadFile(file, { replaces: previous });
      onChange(nodeId, { file_path: result.filename });
      const updated = await api.listFiles();
      setFiles(updated);
    } catch (err: any) {
      console.error('Upload error:', err);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  return (
    <>
      <Field label="XML File">
        <select
          value={params.file_path || ''}
          onChange={(e) => onChange(nodeId, { file_path: e.target.value })}
          className="w-full px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 bg-white mb-1.5"
        >
          <option value="">Select a file...</option>
          {files.filter(f => f.name.endsWith('.xml')).map((f) => (
            <option key={f.name} value={f.name}>{f.name} ({(f.size / 1024).toFixed(1)} KB)</option>
          ))}
        </select>
        <input ref={fileRef} type="file" accept=".xml" onChange={handleUpload} className="hidden" />
        <button onClick={() => fileRef.current?.click()} disabled={uploading}
          className="w-full px-2.5 py-1.5 text-xs border border-dashed border-slate-300 rounded-lg text-slate-500 hover:border-pulse-400 hover:text-pipe-600 hover:bg-pulse-50 transition-colors disabled:opacity-50">
          {uploading ? 'Uploading...' : '+ Upload XML File'}
        </button>
        {params.file_path && <VolumeTierBadge filePath={params.file_path} />}
      </Field>
      <Field label="Row XPath">
        <TextInput value={params.row_xpath || ''} onChange={(v) => onChange(nodeId, { row_xpath: v })} placeholder="//record" />
      </Field>
      <Field label="Root Element">
        <TextInput value={params.root_element || ''} onChange={(v) => onChange(nodeId, { root_element: v })} placeholder="data" />
      </Field>
      <Field label="Namespaces (optional)">
        {/* P2-D-3 (2026-05-18): JSON textarea replaced with KeyValueRepeater */}
        <KeyValueRepeater
          value={(params.namespaces && typeof params.namespaces === 'object') ? params.namespaces : {}}
          onChange={(next) => onChange(nodeId, { namespaces: Object.keys(next).length > 0 ? next : undefined })}
          keyPlaceholder="prefix (e.g. ns)"
          valuePlaceholder="URI (http://...)"
          addLabel="+ Add namespace"
        />
      </Field>
    </>
  );
}

/* ── Cloud Object Storage: ADLS Gen2 / Azure Blob / GCS ── */

type CloudConfigProps = ConfigProps & { isSink?: boolean };

function AdlsGen2Config({ params, nodeId, onChange, isSink }: CloudConfigProps) {
  const hasConn = !!params.connection_id;
  return (
    <>
      <Field label="Connection (optional)">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['azure_datalake', 'adls_gen2', 'azure']}
          requireCapability={isSink ? 'write' : 'read'}
        />
      </Field>
      <Field label="Storage Account *">
        <TextInput value={params.account_name || ''} onChange={(v) => onChange(nodeId, { account_name: v })} placeholder="mystorageacct" />
      </Field>
      <Field label="Container / Filesystem *">
        <TextInput value={params.container || ''} onChange={(v) => onChange(nodeId, { container: v })} placeholder="raw" />
      </Field>
      <Field label={isSink ? 'Output Path *' : 'Path / Glob *'}>
        <TextInput
          value={params.path || ''}
          onChange={(v) => onChange(nodeId, { path: v })}
          placeholder={isSink ? 'exports/2026/04/data.parquet' : 'year=2026/month=04/*.parquet'}
        />
      </Field>
      <div className="bg-blue-50 rounded-md px-3 py-1.5 border border-blue-100">
        <span className="text-[9px] text-blue-700">URI: <code>abfss://{params.container || '<container>'}@{params.account_name || '<account>'}.dfs.core.windows.net/{params.path || '<path>'}</code></span>
      </div>
      {hasConn && (
        <div className="text-[11px] text-slate-500 bg-slate-50 border border-slate-200 rounded-md px-3 py-2">
          Using credentials from this connection. Edit the connection in <strong>Connections</strong> to change account key / SAS token.
        </div>
      )}
      {!hasConn && <>
        <Field label="Connection String (optional)">
          <TextInput value={params.connection_string || ''} onChange={(v) => onChange(nodeId, { connection_string: v })} placeholder="DefaultEndpointsProtocol=https;..." />
        </Field>
        <Field label="Account Key">
          <SecretInput value={params.account_key || ''} onChange={(v) => onChange(nodeId, { account_key: v })} placeholder="••••••••" />
        </Field>
        <Field label="SAS Token">
          <SecretInput value={params.sas_token || ''} onChange={(v) => onChange(nodeId, { sas_token: v })} placeholder="?sv=..." />
        </Field>
      </>}
      <Field label="File Format">
        <Select
          value={params.format || (isSink ? 'parquet' : 'auto')}
          onChange={(v) => onChange(nodeId, { format: v })}
          options={isSink ? ['parquet', 'csv', 'json'] : ['auto', 'parquet', 'csv', 'json']}
        />
      </Field>
    </>
  );
}

function AzureBlobConfig({ params, nodeId, onChange, isSink }: CloudConfigProps) {
  const hasConn = !!params.connection_id;
  return (
    <>
      <Field label="Connection (optional)">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['azure_blob', 'azure']}
          requireCapability={isSink ? 'write' : 'read'}
        />
      </Field>
      <Field label="Storage Account *">
        <TextInput value={params.account_name || ''} onChange={(v) => onChange(nodeId, { account_name: v })} placeholder="mystorageacct" />
      </Field>
      <Field label="Container *">
        <TextInput value={params.container || ''} onChange={(v) => onChange(nodeId, { container: v })} placeholder="data" />
      </Field>
      <Field label={isSink ? 'Output Blob Path *' : 'Blob Path / Glob *'}>
        <TextInput
          value={params.path || ''}
          onChange={(v) => onChange(nodeId, { path: v })}
          placeholder={isSink ? 'exports/2026/data.parquet' : 'exports/2026/*.csv'}
        />
      </Field>
      <div className="bg-blue-50 rounded-md px-3 py-1.5 border border-blue-100">
        <span className="text-[9px] text-blue-700">URI: <code>az://{params.container || '<container>'}/{params.path || '<path>'}</code></span>
      </div>
      {hasConn && (
        <div className="text-[11px] text-slate-500 bg-slate-50 border border-slate-200 rounded-md px-3 py-2">
          Using credentials from this connection. Edit the connection in <strong>Connections</strong> to change account key / SAS token.
        </div>
      )}
      {!hasConn && <>
        <Field label="Connection String (optional)">
          <TextInput value={params.connection_string || ''} onChange={(v) => onChange(nodeId, { connection_string: v })} placeholder="DefaultEndpointsProtocol=https;..." />
        </Field>
        <Field label="Account Key">
          <SecretInput value={params.account_key || ''} onChange={(v) => onChange(nodeId, { account_key: v })} placeholder="••••••••" />
        </Field>
        <Field label="SAS Token">
          <SecretInput value={params.sas_token || ''} onChange={(v) => onChange(nodeId, { sas_token: v })} placeholder="?sv=..." />
        </Field>
      </>}
      <Field label="File Format">
        <Select
          value={params.format || (isSink ? 'parquet' : 'auto')}
          onChange={(v) => onChange(nodeId, { format: v })}
          options={isSink ? ['parquet', 'csv', 'json'] : ['auto', 'parquet', 'csv', 'json']}
        />
      </Field>
    </>
  );
}

function GcsConfig({ params, nodeId, onChange, isSink }: CloudConfigProps) {
  const hasConn = !!params.connection_id;
  return (
    <>
      <Field label="Connection (optional)">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['gcs', 'google_cloud_storage']}
          requireCapability={isSink ? 'write' : 'read'}
        />
      </Field>
      <Field label="Bucket *">
        <TextInput value={params.bucket || ''} onChange={(v) => onChange(nodeId, { bucket: v })} placeholder="my-gcs-bucket" />
      </Field>
      <Field label={isSink ? 'Output Path *' : 'Object Path / Glob *'}>
        <TextInput
          value={params.path || ''}
          onChange={(v) => onChange(nodeId, { path: v })}
          placeholder={isSink ? 'exports/2026/data.parquet' : 'exports/*.parquet'}
        />
      </Field>
      <div className="bg-emerald-50 rounded-md px-3 py-1.5 border border-emerald-100">
        <span className="text-[9px] text-emerald-700">URI: <code>gs://{params.bucket || '<bucket>'}/{params.path || '<path>'}</code></span>
      </div>
      {hasConn && (
        <div className="text-[11px] text-slate-500 bg-slate-50 border border-slate-200 rounded-md px-3 py-2">
          Using HMAC credentials from this connection. Edit the connection in <strong>Connections</strong> to change keys.
        </div>
      )}
      {!hasConn && <>
        <div className="bg-slate-50 rounded-md px-3 py-1.5 border border-slate-200">
          <span className="text-[9px] text-slate-600">Generate HMAC interop keys: GCP Console → Cloud Storage → Settings → Interoperability</span>
        </div>
        <Field label="HMAC Key ID *">
          <TextInput value={params.hmac_key_id || ''} onChange={(v) => onChange(nodeId, { hmac_key_id: v })} placeholder="GOOG1E..." />
        </Field>
        <Field label="HMAC Secret *">
          <SecretInput value={params.hmac_secret || ''} onChange={(v) => onChange(nodeId, { hmac_secret: v })} placeholder="••••••••" />
        </Field>
      </>}
      <Field label="File Format">
        <Select
          value={params.format || (isSink ? 'parquet' : 'auto')}
          onChange={(v) => onChange(nodeId, { format: v })}
          options={isSink ? ['parquet', 'csv', 'json'] : ['auto', 'parquet', 'csv', 'json']}
        />
      </Field>
    </>
  );
}

/* ── Universal File node (auto-detect format from extension) ── */

function LocalFileUploader({ currentPath, onUploaded }: { currentPath: string; onUploaded: (path: string) => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);

  const ACCEPT = '.csv,.tsv,.txt,.json,.ndjson,.jsonl,.parquet,.pq,.xlsx,.xls,.xml';

  const pickFile = () => inputRef.current?.click();

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    setUploading(true);
    try {
      const form = new FormData();
      form.append('file', file);
      const token = localStorage.getItem('fpulse_token');
      const workspaceId = localStorage.getItem('fpulse_workspace_id') || 'default';
      const headers: Record<string, string> = { 'X-Workspace-Id': workspaceId };
      if (token) headers['Authorization'] = `Bearer ${token}`;
      // If this node already has a workspace upload, ask the backend to
      // delete it after the new upload succeeds — keeps the uploads
      // directory from accumulating orphans.
      const url = currentPath && currentPath.startsWith('uploads/')
        ? `/api/uploads/file?replaces=${encodeURIComponent(currentPath)}`
        : '/api/uploads/file';
      const res = await fetch(url, { method: 'POST', headers, body: form });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || `Upload failed (${res.status})`);
      }
      onUploaded(data.file_path);
      toast.success('Uploaded', `${data.original_name || file.name} → ${data.file_path}`);
    } catch (err: any) {
      toast.error('Upload failed', err?.message || 'Could not upload file');
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="bg-emerald-50 rounded-md px-3 py-2 border border-emerald-100 space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs text-emerald-800 font-medium">Upload from this computer</div>
        <button
          type="button"
          onClick={pickFile}
          disabled={uploading}
          className="px-2.5 py-1 text-xs font-medium text-white bg-emerald-600 hover:bg-emerald-700 rounded disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {uploading ? 'Uploading…' : 'Choose file'}
        </button>
      </div>
      <div className="text-[9px] text-emerald-700 leading-tight">
        CSV, TSV, JSON, NDJSON, Parquet, Excel, XML. File is saved to the F-Pulse data directory and the File Path below is filled in automatically.
      </div>
      {currentPath && currentPath.startsWith('uploads/') && (
        <div className="text-[9px] text-emerald-600 truncate" title={currentPath}>
          Current upload: {currentPath}
        </div>
      )}
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        className="hidden"
        onChange={handleUpload}
      />
    </div>
  );
}

function FileNodeConfig({ params, nodeId, onChange, isSink }: CloudConfigProps) {
  const fmt = params.format || 'auto';
  const showCsv = fmt === 'auto' || fmt === 'csv' || fmt === 'tsv';
  const showExcel = fmt === 'auto' || fmt === 'excel';
  const showXml = fmt === 'auto' || fmt === 'xml';
  const showParquet = fmt === 'auto' || fmt === 'parquet';
  return (
    <>
      <Field label="Connection (optional)">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['s3', 'minio', 'local', 'nfs', 'smb']}
          requireCapability={isSink ? 'write' : 'read'}
        />
      </Field>
      <div className="bg-indigo-50 rounded-md px-3 py-1.5 border border-indigo-100">
        <span className="text-[9px] text-indigo-700">
          Format is detected from the file extension. Override below only if needed.
        </span>
      </div>
      {!isSink && (
        <LocalFileUploader
          currentPath={params.file_path || ''}
          onUploaded={(p) => onChange(nodeId, { file_path: p })}
        />
      )}
      <Field label={isSink ? 'Output File *' : 'File *'}>
        <TextInput
          value={params.file_path || ''}
          onChange={(v) => onChange(nodeId, { file_path: v })}
          placeholder={isSink ? 'out/result.parquet' : 'data/input.csv (or .json/.parquet/.xlsx/.xml)'}
        />
      </Field>
      <Field label="Format Override">
        <Select
          value={fmt}
          onChange={(v) => onChange(nodeId, { format: v })}
          options={['auto', 'csv', 'tsv', 'json', 'ndjson', 'parquet', 'excel', 'xml']}
        />
      </Field>
      {showCsv && (
        <>
          <Field label="Delimiter (CSV only)">
            <Select value={params.delimiter || ','} onChange={(v) => onChange(nodeId, { delimiter: v })} options={[',', ';', '\\t', '|']} />
          </Field>
          <Field label={isSink ? 'Write Header' : 'Has Header'}>
            <Toggle value={params.header !== false} onChange={(v) => onChange(nodeId, { header: v })} label={params.header !== false ? 'Yes' : 'No'} />
          </Field>
          {!isSink && (
            <>
              <Field label="Encoding">
                <Select value={params.encoding || 'utf-8'} onChange={(v) => onChange(nodeId, { encoding: v })}
                  options={['utf-8', 'utf-16', 'latin-1', 'iso-8859-1', 'cp1252', 'ascii']} />
              </Field>
              <Field label="Quote Character">
                <Select value={params.quote_char || '"'} onChange={(v) => onChange(nodeId, { quote_char: v })}
                  options={['"', "'", 'none']} />
              </Field>
              <Field label="NULL String">
                <TextInput value={params.null_string || ''} onChange={(v) => onChange(nodeId, { null_string: v })}
                  placeholder="\\N or NULL or empty" />
              </Field>
            </>
          )}
        </>
      )}
      {showParquet && isSink && (
        <Field label="Compression (Parquet)">
          <Select value={params.compression || 'snappy'} onChange={(v) => onChange(nodeId, { compression: v })}
            options={['snappy', 'zstd', 'gzip', 'uncompressed']} />
        </Field>
      )}
      {showExcel && (
        <>
          <Field label="Sheet Name (Excel)">
            <TextInput value={params.sheet_name || ''} onChange={(v) => onChange(nodeId, { sheet_name: v })} placeholder="Sheet1" />
          </Field>
          {!isSink && (
            <>
              <Field label="Header Row (Excel)">
                <TextInput value={String(params.header_row ?? 1)} onChange={(v) => onChange(nodeId, { header_row: parseInt(v) || 1 })} placeholder="1" />
              </Field>
              <Field label="Skip Rows (Excel)">
                <TextInput value={String(params.skip_rows ?? 0)} onChange={(v) => onChange(nodeId, { skip_rows: parseInt(v) || 0 })} placeholder="0" />
              </Field>
            </>
          )}
        </>
      )}
      {showXml && !isSink && (
        <>
          <Field label="Row XPath (XML)">
            <TextInput value={params.row_xpath || '//record'} onChange={(v) => onChange(nodeId, { row_xpath: v })} placeholder="//record" />
          </Field>
          <Field label="Namespaces (XML)">
            <TextInput value={params.namespaces || ''} onChange={(v) => onChange(nodeId, { namespaces: v })} placeholder='{"ns": "http://example.com/ns"}' />
          </Field>
        </>
      )}
      {showXml && isSink && (
        <>
          <Field label="Root Tag (XML)">
            <TextInput value={params.root_tag || 'data'} onChange={(v) => onChange(nodeId, { root_tag: v })} placeholder="data" />
          </Field>
          <Field label="Row Tag (XML)">
            <TextInput value={params.row_tag || 'record'} onChange={(v) => onChange(nodeId, { row_tag: v })} placeholder="record" />
          </Field>
        </>
      )}
      {!isSink && (
        <Field label="Dev Sample Limit">
          <TextInput value={String(params.dev_sample_rows ?? '')} onChange={(v) => onChange(nodeId, { dev_sample_rows: v ? parseInt(v) : undefined })}
            placeholder="1000 (blank = no limit)" />
        </Field>
      )}
    </>
  );
}

/* ── SaaS document storage: SharePoint / OneDrive / Google Drive / Dropbox / Box ── */

function SharePointConfig({ params, nodeId, onChange, isSink }: CloudConfigProps) {
  return (
    <>
      <Field label="SharePoint Connection *">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['sharepoint', 'microsoft_graph']}
          requireCapability={isSink ? 'write' : 'read'}
        />
      </Field>
      <Field label="Site ID">
        <TextInput
          value={params.site_id || ''}
          onChange={(v) => onChange(nodeId, { site_id: v })}
          placeholder="contoso.sharepoint.com,abc123,def456"
        />
      </Field>
      <Field label="Drive ID (optional)">
        <TextInput
          value={params.drive_id || ''}
          onChange={(v) => onChange(nodeId, { drive_id: v })}
          placeholder="Leave blank for default Documents library"
        />
      </Field>
      <Field label={isSink ? 'Destination Path *' : 'Item Path *'}>
        <TextInput
          value={params.item_path || ''}
          onChange={(v) => onChange(nodeId, { item_path: v })}
          placeholder={isSink ? 'Reports/2026/output.parquet' : 'Reports/2026/sales.csv'}
        />
      </Field>
      {!isSink && (
        <Field label="Format Override">
          <Select
            value={params.format || 'auto'}
            onChange={(v) => onChange(nodeId, { format: v })}
            options={['auto', 'csv', 'json', 'parquet', 'excel', 'xml']}
          />
        </Field>
      )}
      <div className="bg-blue-50 rounded-md px-3 py-1.5 border border-blue-100">
        <span className="text-[9px] text-blue-700">
          Auth: client_credentials flow via Microsoft Graph. Connection holds tenant_id + client_id + client_secret.
        </span>
      </div>
    </>
  );
}

function OneDriveConfig({ params, nodeId, onChange, isSink }: CloudConfigProps) {
  return (
    <>
      <Field label="OneDrive Connection *">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['onedrive', 'microsoft_graph']}
          requireCapability={isSink ? 'write' : 'read'}
        />
      </Field>
      <Field label="User ID (optional)">
        <TextInput
          value={params.user_id || ''}
          onChange={(v) => onChange(nodeId, { user_id: v })}
          placeholder="Leave blank for /me/drive (delegated auth)"
        />
      </Field>
      <Field label={isSink ? 'Destination Path *' : 'Item Path *'}>
        <TextInput
          value={params.item_path || ''}
          onChange={(v) => onChange(nodeId, { item_path: v })}
          placeholder={isSink ? 'Documents/output.parquet' : 'Documents/data.xlsx'}
        />
      </Field>
      {!isSink && (
        <Field label="Format Override">
          <Select
            value={params.format || 'auto'}
            onChange={(v) => onChange(nodeId, { format: v })}
            options={['auto', 'csv', 'json', 'parquet', 'excel', 'xml']}
          />
        </Field>
      )}
    </>
  );
}

function GoogleDriveConfig({ params, nodeId, onChange, isSink }: CloudConfigProps) {
  return (
    <>
      <Field label="Google Drive Connection *">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['gdrive', 'google_drive']}
          requireCapability={isSink ? 'write' : 'read'}
        />
      </Field>
      {isSink ? (
        <>
          <Field label="File Name *">
            <TextInput value={params.file_name || ''} onChange={(v) => onChange(nodeId, { file_name: v })} placeholder="output.parquet" />
          </Field>
          <Field label="Parent Folder ID (optional)">
            <TextInput value={params.parent_folder_id || ''} onChange={(v) => onChange(nodeId, { parent_folder_id: v })} placeholder="root if blank" />
          </Field>
        </>
      ) : (
        <>
          <Field label="File ID *">
            <TextInput value={params.file_id || ''} onChange={(v) => onChange(nodeId, { file_id: v })} placeholder="From Drive share link" />
          </Field>
          <Field label="File Name (for format detection)">
            <TextInput value={params.file_name || ''} onChange={(v) => onChange(nodeId, { file_name: v })} placeholder="report.csv" />
          </Field>
          <Field label="Format Override">
            <Select
              value={params.format || 'auto'}
              onChange={(v) => onChange(nodeId, { format: v })}
              options={['auto', 'csv', 'json', 'parquet', 'excel', 'xml']}
            />
          </Field>
        </>
      )}
      <div className="bg-emerald-50 rounded-md px-3 py-1.5 border border-emerald-100">
        <span className="text-[9px] text-emerald-700">
          Auth: OAuth refresh_token via Drive API v3, or direct access_token on the connection.
        </span>
      </div>
    </>
  );
}

function DropboxConfig({ params, nodeId, onChange, isSink }: CloudConfigProps) {
  return (
    <>
      <Field label="Dropbox Connection *">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['dropbox']}
          requireCapability={isSink ? 'write' : 'read'}
        />
      </Field>
      <Field label={isSink ? 'Destination Path *' : 'Path *'}>
        <TextInput
          value={params.path || ''}
          onChange={(v) => onChange(nodeId, { path: v })}
          placeholder={isSink ? '/Reports/output.parquet' : '/Reports/data.csv'}
        />
      </Field>
      {!isSink && (
        <Field label="Format Override">
          <Select
            value={params.format || 'auto'}
            onChange={(v) => onChange(nodeId, { format: v })}
            options={['auto', 'csv', 'json', 'parquet', 'excel', 'xml']}
          />
        </Field>
      )}
    </>
  );
}

function BoxConfig({ params, nodeId, onChange, isSink }: CloudConfigProps) {
  return (
    <>
      <Field label="Box Connection *">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['box']}
          requireCapability={isSink ? 'write' : 'read'}
        />
      </Field>
      {isSink ? (
        <>
          <Field label="File Name *">
            <TextInput value={params.file_name || ''} onChange={(v) => onChange(nodeId, { file_name: v })} placeholder="output.parquet" />
          </Field>
          <Field label="Parent Folder ID">
            <TextInput value={params.parent_folder_id || '0'} onChange={(v) => onChange(nodeId, { parent_folder_id: v })} placeholder="0 = root" />
          </Field>
        </>
      ) : (
        <>
          <Field label="File ID *">
            <TextInput value={params.file_id || ''} onChange={(v) => onChange(nodeId, { file_id: v })} placeholder="Box file ID" />
          </Field>
          <Field label="File Name (for format detection)">
            <TextInput value={params.file_name || ''} onChange={(v) => onChange(nodeId, { file_name: v })} placeholder="report.csv" />
          </Field>
          <Field label="Format Override">
            <Select
              value={params.format || 'auto'}
              onChange={(v) => onChange(nodeId, { format: v })}
              options={['auto', 'csv', 'json', 'parquet', 'excel', 'xml']}
            />
          </Field>
        </>
      )}
    </>
  );
}

function S3SourceConfig({ params, nodeId, onChange }: ConfigProps) {
  const hasConn = !!params.connection_id;
  return (
    <>
      <Field label="Connection">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['s3', 'minio', 'aws_s3']}
          requireCapability="read"
        />
      </Field>
      <Field label="Operation *">
        <Select
          value={params.operation || 'read_file'}
          onChange={(v) => onChange(nodeId, { operation: v })}
          options={['read_file', 'read_folder', 'list_objects']}
        />
      </Field>
      <Field label="Bucket *">
        <TextInput value={params.bucket || ''} onChange={(v) => onChange(nodeId, { bucket: v })} placeholder="my-data-bucket" />
      </Field>
      <Field label={params.operation === 'list_objects' ? 'Prefix *' : params.operation === 'read_folder' ? 'Folder Prefix *' : 'Key *'}>
        <TextInput value={params.key || ''} onChange={(v) => onChange(nodeId, { key: v })}
          placeholder={params.operation === 'read_folder' ? 'data/input/' : 'data/input/orders.csv'} />
      </Field>
      {params.operation === 'read_folder' && (
        <Field label="File Pattern">
          <TextInput value={params.file_pattern || '*'} onChange={(v) => onChange(nodeId, { file_pattern: v })} placeholder="*.parquet" />
        </Field>
      )}
      {hasConn && (
        <div className="text-[11px] text-slate-500 bg-slate-50 border border-slate-200 rounded-md px-3 py-2">
          Using region + AWS credentials + endpoint from this connection. Edit the connection in <strong>Connections</strong> to change.
        </div>
      )}
      {!hasConn && <>
        <Field label="Region">
          <Select value={params.region || 'us-east-1'} onChange={(v) => onChange(nodeId, { region: v })}
            options={['us-east-1', 'us-east-2', 'us-west-1', 'us-west-2', 'eu-west-1', 'eu-west-2', 'eu-central-1', 'ap-south-1', 'ap-southeast-1', 'ap-northeast-1']} />
        </Field>
        <Field label="Access Key ID">
          <TextInput value={params.access_key_id || ''} onChange={(v) => onChange(nodeId, { access_key_id: v })} placeholder="AKIA..." />
        </Field>
        <Field label="Secret Access Key">
          <SecretInput value={params.secret_access_key || ''} onChange={(v) => onChange(nodeId, { secret_access_key: v })} placeholder="••••••••" />
        </Field>
        <Field label="Endpoint URL (MinIO / custom)">
          <TextInput value={params.endpoint_url || ''} onChange={(v) => onChange(nodeId, { endpoint_url: v })} placeholder="http://localhost:9000" />
        </Field>
      </>}
      <Field label="File Format">
        <Select value={params.file_format || 'csv'} onChange={(v) => onChange(nodeId, { file_format: v })} options={['csv', 'json', 'parquet', 'jsonl', 'tsv']} />
      </Field>
    </>
  );
}

function KafkaSourceConfig({ params, nodeId, onChange }: ConfigProps) {
  const hasConn = !!params.connection_id;
  return (
    <>
      <Field label="Connection">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['kafka', 'redpanda']}
          requireCapability="read"
        />
      </Field>
      {hasConn && (
        // P0-3 finisher (2026-05-18): bootstrap + SASL credentials come
        // from the saved connection. Inline overrides hidden.
        <div className="text-[11px] text-slate-500 bg-slate-50 border border-slate-200 rounded-md px-3 py-2">
          Using bootstrap servers + SASL credentials from this connection.
          Topic + consumer-side knobs below still apply per-pipeline.
        </div>
      )}
      {!hasConn && (
        <Field label="Bootstrap Servers *">
          <TextInput value={params.bootstrap_servers || ''} onChange={(v) => onChange(nodeId, { bootstrap_servers: v })} placeholder="localhost:9092" />
        </Field>
      )}
      <Field label="Topic *">
        <TextInput value={params.topic || ''} onChange={(v) => onChange(nodeId, { topic: v })} placeholder="orders-topic" />
      </Field>
      <Field label="Operation *">
        <Select
          value={params.operation || 'consume_from_latest'}
          onChange={(v) => {
            const patch: any = { operation: v };
            if (v === 'consume_from_earliest') patch.auto_offset_reset = 'earliest';
            if (v === 'consume_from_latest') patch.auto_offset_reset = 'latest';
            onChange(nodeId, patch);
          }}
          options={['consume_from_earliest', 'consume_from_latest', 'consume_from_offset']}
        />
      </Field>
      <Field label="Consumer Group ID *">
        <TextInput value={params.group_id || ''} onChange={(v) => onChange(nodeId, { group_id: v })} placeholder="fpulse-consumer-group" />
      </Field>
      {params.operation === 'consume_from_offset' && (
        <>
          <Field label="Partition">
            <TextInput value={String(params.partition ?? 0)} onChange={(v) => onChange(nodeId, { partition: parseInt(v) || 0 })} placeholder="0" />
          </Field>
          <Field label="Starting Offset *">
            <TextInput value={String(params.starting_offset ?? 0)} onChange={(v) => onChange(nodeId, { starting_offset: parseInt(v) || 0 })} placeholder="0" />
          </Field>
        </>
      )}
      {params.operation !== 'consume_from_offset' && (
        <Field label="Auto Offset Reset">
          <Select value={params.auto_offset_reset || 'earliest'} onChange={(v) => onChange(nodeId, { auto_offset_reset: v })} options={['earliest', 'latest', 'none']} />
        </Field>
      )}
      <Field label="Max Messages">
        <TextInput value={String(params.max_messages ?? 1000)} onChange={(v) => onChange(nodeId, { max_messages: parseInt(v) || 1000 })} placeholder="1000" />
      </Field>
      {/* Security + SASL fields only shown when there's no connection.
          With a saved connection, the SASL credentials are part of the
          connection record. */}
      {!hasConn && <>
        <Field label="Security Protocol">
          <Select value={params.security_protocol || 'PLAINTEXT'} onChange={(v) => onChange(nodeId, { security_protocol: v })} options={['PLAINTEXT', 'SSL', 'SASL_PLAINTEXT', 'SASL_SSL']} />
        </Field>
        <Field label="SASL Mechanism">
          <Select value={params.sasl_mechanism || ''} onChange={(v) => onChange(nodeId, { sasl_mechanism: v })} options={['', 'PLAIN', 'SCRAM-SHA-256', 'SCRAM-SHA-512']} />
        </Field>
        <Field label="SASL Username">
          <TextInput value={params.sasl_username || ''} onChange={(v) => onChange(nodeId, { sasl_username: v })} placeholder="username" />
        </Field>
        <Field label="SASL Password">
          <SecretInput value={params.sasl_password || ''} onChange={(v) => onChange(nodeId, { sasl_password: v })} placeholder="••••••••" />
        </Field>
      </>}
      <Field label="Value Deserializer">
        <Select value={params.value_deserializer || 'json'} onChange={(v) => onChange(nodeId, { value_deserializer: v })} options={['json', 'avro', 'string', 'bytes']} />
      </Field>
    </>
  );
}

function FtpSourceConfig({ params, nodeId, onChange }: ConfigProps) {
  const hasConn = !!params.connection_id;
  const protocol = params.protocol || 'ftp';
  return (
    <>
      <Field label="Connection">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['ftp', 'sftp', 'ftps']}
          requireCapability="read"
        />
      </Field>
      {hasConn && (
        // P0-3 (2026-05-18): host/port/credentials come from the saved
        // connection record when one is picked. Inline overrides hidden.
        <div className="text-[11px] text-slate-500 bg-slate-50 border border-slate-200 rounded-md px-3 py-2">
          Using host + credentials from this connection. Edit the connection in
          <strong> Connections</strong> to change host / port / username / password / SSH key.
        </div>
      )}
      {!hasConn && <>
        <Field label="Host *">
          <TextInput value={params.host || ''} onChange={(v) => onChange(nodeId, { host: v })} placeholder="ftp.example.com" />
        </Field>
        <Field label="Port">
          <TextInput value={String(params.port ?? 21)} onChange={(v) => onChange(nodeId, { port: parseInt(v) || 21 })} placeholder="21" />
        </Field>
        <Field label="Username">
          <TextInput value={params.username || ''} onChange={(v) => onChange(nodeId, { username: v })} placeholder="ftp_user" />
        </Field>
        <Field label="Password">
          <SecretInput value={params.password || ''} onChange={(v) => onChange(nodeId, { password: v })} placeholder="••••••••" />
        </Field>
      </>}
      <Field label="Remote Path *">
        <TextInput value={params.remote_path || ''} onChange={(v) => onChange(nodeId, { remote_path: v })} placeholder="/data/exports/orders.csv" />
      </Field>
      <Field label="Protocol">
        <Select value={protocol} onChange={(v) => onChange(nodeId, { protocol: v })} options={['ftp', 'sftp', 'ftps']} />
      </Field>
      <Field label="File Format">
        {/* 2026-06-17 — write `format` (not `file_format`): FtpSourceNode /
            FtpSinkNode read params["format"]. Options limited to what the
            node actually reads/writes (csv/json/parquet). */}
        <Select value={params.format || 'csv'} onChange={(v) => onChange(nodeId, { format: v })} options={['csv', 'json', 'parquet']} />
      </Field>
      {/* Tier-6 fix (2026-05-18): SSH Private Key only appears when
          Protocol is sftp. Was always visible; ftp / ftps protocols
          ignore it entirely so showing it caused user confusion. */}
      {!hasConn && protocol === 'sftp' && (
        <Field label="SSH Private Key (SFTP)">
          {/* 2026-06-17 — write `private_key` (not `ssh_key`): the FTP/SFTP
              nodes read params["private_key"]. */}
          <TextArea value={params.private_key || ''} onChange={(v) => onChange(nodeId, { private_key: v })} placeholder="-----BEGIN RSA PRIVATE KEY-----" rows={3} />
        </Field>
      )}
    </>
  );
}

function GSheetSourceConfig({ params, nodeId, onChange }: ConfigProps) {
  return (
    <>
      <Field label="Connection">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['gsheet', 'google_sheets']}
          requireCapability="read"
        />
      </Field>
      <Field label="Spreadsheet ID *">
        <TextInput value={params.spreadsheet_id || ''} onChange={(v) => onChange(nodeId, { spreadsheet_id: v })} placeholder="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74Og..." />
      </Field>
      <Field label="Sheet Name">
        <TextInput value={params.sheet_name || ''} onChange={(v) => onChange(nodeId, { sheet_name: v })} placeholder="Sheet1" />
      </Field>
      <Field label="Range (optional)">
        <TextInput value={params.range || ''} onChange={(v) => onChange(nodeId, { range: v })} placeholder="A1:Z1000" />
      </Field>
      <Field label="Service Account JSON">
        <TextArea value={params.credentials_json || ''} onChange={(v) => onChange(nodeId, { credentials_json: v })} placeholder='{"type":"service_account","project_id":"..."}' rows={4} />
      </Field>
      <Field label="Header Row">
        <Toggle value={params.header !== false} onChange={(v) => onChange(nodeId, { header: v })} label="First row is header" />
      </Field>
    </>
  );
}

function DeltaSourceConfig({ params, nodeId, onChange }: ConfigProps) {
  return (
    <>
      <Field label="Table Path *">
        <TextInput value={params.table_path || ''} onChange={(v) => onChange(nodeId, { table_path: v })} placeholder="s3://bucket/delta/orders" />
      </Field>
      <Field label="Version (optional)">
        <TextInput value={String(params.version ?? '')} onChange={(v) => onChange(nodeId, { version: v ? parseInt(v) : undefined })} placeholder="Latest" />
      </Field>
      <Field label="Timestamp (optional)">
        <TextInput value={params.timestamp || ''} onChange={(v) => onChange(nodeId, { timestamp: v })} placeholder="2024-01-15T00:00:00Z" />
      </Field>
      <Field label="Predicate Filter (optional)">
        <TextInput value={params.predicate || ''} onChange={(v) => onChange(nodeId, { predicate: v })} placeholder="date >= '2024-01-01'" />
      </Field>
      <Field label="Storage Options">
        {/* P2-D-3: JSON textarea replaced with KeyValueRepeater (secretValues
            so credentials in storage options also mask on screen) */}
        <KeyValueRepeater
          value={(params.storage_options && typeof params.storage_options === 'object') ? params.storage_options : {}}
          onChange={(next) => onChange(nodeId, { storage_options: Object.keys(next).length > 0 ? next : undefined })}
          keyPlaceholder="AWS_ACCESS_KEY_ID"
          valuePlaceholder="value"
          secretValues={true}
          addLabel="+ Add storage option"
        />
      </Field>
    </>
  );
}

/* ── Sink/Output Configs ── */

function CsvSinkConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  return (
    <>
      <Field label="File Path *">
        <TextInput value={params.file_path || ''} onChange={(v) => onChange(nodeId, { file_path: v })} placeholder="output/results.csv" />
      </Field>
      <Field label="Delimiter">
        <Select value={params.delimiter || ','} onChange={(v) => onChange(nodeId, { delimiter: v })} options={[',', ';', '|', '\t']} />
      </Field>
      <Field label="Header">
        <Toggle value={params.header !== false} onChange={(v) => onChange(nodeId, { header: v })} label="Include header row" />
      </Field>
      <Field label="Quote Character">
        <Select value={params.quote_char || '"'} onChange={(v) => onChange(nodeId, { quote_char: v })} options={['"', "'", 'none']} />
      </Field>
      <Field label="Encoding">
        <Select value={params.encoding || 'utf-8'} onChange={(v) => onChange(nodeId, { encoding: v })} options={['utf-8', 'utf-16', 'ascii', 'latin-1']} />
      </Field>
      <Field label="Write Mode">
        <Select value={params.write_mode || 'overwrite'} onChange={(v) => onChange(nodeId, { write_mode: v })} options={['overwrite', 'append']} />
      </Field>
      {columns.length > 0 && (
        <Field label="Column Selection">
          <div className="flex flex-wrap gap-1">
            {columns.map((col) => {
              const selected = !params.exclude_columns || !params.exclude_columns.includes(col);
              return (
                <button key={col} onClick={() => {
                  const excluded = params.exclude_columns || [];
                  const updated = selected ? [...excluded, col] : excluded.filter((c: string) => c !== col);
                  onChange(nodeId, { exclude_columns: updated });
                }}
                  className={`text-xs px-2 py-1 rounded-lg font-mono border transition-colors ${selected ? 'bg-pipe-100 text-pipe-700 border-pipe-300' : 'bg-white text-slate-400 border-slate-200 line-through'}`}>
                  {col}
                </button>
              );
            })}
          </div>
        </Field>
      )}
    </>
  );
}

function JsonSinkConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  return (
    <>
      <Field label="File Path *">
        <TextInput value={params.file_path || ''} onChange={(v) => onChange(nodeId, { file_path: v })} placeholder="output/results.json" />
      </Field>
      <Field label="Format">
        <Select value={params.format || 'json'} onChange={(v) => onChange(nodeId, { format: v })} options={['json', 'jsonl', 'json_array']} />
      </Field>
      <Field label="Indent">
        <Select value={String(params.indent ?? 2)} onChange={(v) => onChange(nodeId, { indent: parseInt(v) })} options={['0', '2', '4']} />
      </Field>
      <Field label="Write Mode">
        <Select value={params.write_mode || 'overwrite'} onChange={(v) => onChange(nodeId, { write_mode: v })} options={['overwrite', 'append']} />
      </Field>
      <Field label="Encoding">
        <Select value={params.encoding || 'utf-8'} onChange={(v) => onChange(nodeId, { encoding: v })} options={['utf-8', 'utf-16', 'ascii']} />
      </Field>
    </>
  );
}

function ExcelSinkConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  return (
    <>
      <Field label="File Path *">
        <TextInput value={params.file_path || ''} onChange={(v) => onChange(nodeId, { file_path: v })} placeholder="output/report.xlsx" />
      </Field>
      <Field label="Sheet Name">
        <TextInput value={params.sheet_name || 'Sheet1'} onChange={(v) => onChange(nodeId, { sheet_name: v })} placeholder="Sheet1" />
      </Field>
      <Field label="Start Cell">
        <TextInput value={params.start_cell || 'A1'} onChange={(v) => onChange(nodeId, { start_cell: v })} placeholder="A1" />
      </Field>
      <Field label="Header">
        <Toggle value={params.header !== false} onChange={(v) => onChange(nodeId, { header: v })} label="Include header row" />
      </Field>
      <Field label="Auto-fit Columns">
        <Toggle value={params.autofit !== false} onChange={(v) => onChange(nodeId, { autofit: v })} label="Auto-fit column widths" />
      </Field>
      <Field label="Write Mode">
        <Select value={params.write_mode || 'overwrite'} onChange={(v) => onChange(nodeId, { write_mode: v })} options={['overwrite', 'append', 'new_sheet']} />
      </Field>
    </>
  );
}

function S3SinkConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const hasConn = !!params.connection_id;
  return (
    <>
      <Field label="Connection">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['s3', 'minio', 'aws_s3']}
          requireCapability="write"
        />
      </Field>
      <Field label="Bucket *">
        <TextInput value={params.bucket || ''} onChange={(v) => onChange(nodeId, { bucket: v })} placeholder="my-output-bucket" />
      </Field>
      <Field label="Key / Path *">
        <TextInput value={params.key || ''} onChange={(v) => onChange(nodeId, { key: v })} placeholder="output/results/data.parquet" />
      </Field>
      {hasConn && (
        <div className="text-[11px] text-slate-500 bg-slate-50 border border-slate-200 rounded-md px-3 py-2">
          Using region + AWS credentials + endpoint from this connection. Edit the connection in <strong>Connections</strong> to change.
        </div>
      )}
      {!hasConn && <>
        <Field label="Region">
          <Select value={params.region || 'us-east-1'} onChange={(v) => onChange(nodeId, { region: v })}
            options={['us-east-1', 'us-east-2', 'us-west-1', 'us-west-2', 'eu-west-1', 'eu-west-2', 'eu-central-1', 'ap-south-1', 'ap-southeast-1', 'ap-northeast-1']} />
        </Field>
        <Field label="Access Key ID">
          <TextInput value={params.access_key_id || ''} onChange={(v) => onChange(nodeId, { access_key_id: v })} placeholder="AKIA..." />
        </Field>
        <Field label="Secret Access Key">
          <SecretInput value={params.secret_access_key || ''} onChange={(v) => onChange(nodeId, { secret_access_key: v })} placeholder="••••••••" />
        </Field>
        <Field label="Endpoint URL (MinIO / custom)">
          <TextInput value={params.endpoint_url || ''} onChange={(v) => onChange(nodeId, { endpoint_url: v })} placeholder="http://localhost:9000" />
        </Field>
      </>}
      <Field label="File Format">
        <Select value={params.file_format || 'parquet'} onChange={(v) => onChange(nodeId, { file_format: v })} options={['parquet', 'csv', 'json', 'jsonl']} />
      </Field>
      <Field label="Write Mode">
        <Select value={params.write_mode || 'overwrite'} onChange={(v) => onChange(nodeId, { write_mode: v })} options={['overwrite', 'append']} />
      </Field>
    </>
  );
}

function KafkaSinkConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  return (
    <>
      <Field label="Connection">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['kafka', 'redpanda']}
          requireCapability="write"
        />
      </Field>
      <Field label="Bootstrap Servers *">
        <TextInput value={params.bootstrap_servers || ''} onChange={(v) => onChange(nodeId, { bootstrap_servers: v })} placeholder="localhost:9092" />
      </Field>
      <Field label="Topic *">
        <TextInput value={params.topic || ''} onChange={(v) => onChange(nodeId, { topic: v })} placeholder="output-topic" />
      </Field>
      <Field label="Key Column (optional)">
        <TextInput value={params.key_column || ''} onChange={(v) => onChange(nodeId, { key_column: v })} placeholder="id" />
      </Field>
      <Field label="Value Serializer">
        <Select value={params.value_serializer || 'json'} onChange={(v) => onChange(nodeId, { value_serializer: v })} options={['json', 'avro', 'string']} />
      </Field>
      <Field label="Security Protocol">
        <Select value={params.security_protocol || 'PLAINTEXT'} onChange={(v) => onChange(nodeId, { security_protocol: v })} options={['PLAINTEXT', 'SSL', 'SASL_PLAINTEXT', 'SASL_SSL']} />
      </Field>
      <Field label="SASL Mechanism">
        <Select value={params.sasl_mechanism || ''} onChange={(v) => onChange(nodeId, { sasl_mechanism: v })} options={['', 'PLAIN', 'SCRAM-SHA-256', 'SCRAM-SHA-512']} />
      </Field>
      <Field label="Compression">
        <Select value={params.compression || 'none'} onChange={(v) => onChange(nodeId, { compression: v })} options={['none', 'gzip', 'snappy', 'lz4', 'zstd']} />
      </Field>
      <Field label="Batch Size">
        <TextInput value={String(params.batch_size ?? 16384)} onChange={(v) => onChange(nodeId, { batch_size: parseInt(v) || 16384 })} placeholder="16384" />
      </Field>
    </>
  );
}

function ApiSinkConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  return (
    <>
      <Field label="Connection">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['rest_api', 'http', 'api']}
          requireCapability="write"
        />
      </Field>
      <Field label="URL *">
        <TextInput value={params.url || ''} onChange={(v) => onChange(nodeId, { url: v })} placeholder="https://api.example.com/ingest" />
      </Field>
      <Field label="Method">
        <Select value={params.method || 'POST'} onChange={(v) => onChange(nodeId, { method: v })} options={['POST', 'PUT', 'PATCH']} />
      </Field>
      <Field label="Headers">
        {/* P2-D-2: JSON textarea replaced with KeyValueRepeater */}
        <KeyValueRepeater
          value={params.headers || {}}
          onChange={(next) => onChange(nodeId, { headers: next })}
          keyPlaceholder="Header name"
          valuePlaceholder="Value"
          addLabel="+ Add header"
        />
      </Field>
      {/* P2-A: inline auth replaced with <AuthSection> primitive. */}
      <AuthSection params={params} onChange={(patch) => onChange(nodeId, patch)} />
      <Field label="Batch Size (rows per request)">
        <TextInput value={String(params.batch_size ?? 100)} onChange={(v) => onChange(nodeId, { batch_size: parseInt(v) || 100 })} placeholder="100" />
      </Field>
      {/* P0-4: per-node Retry Count + On Error removed — use the
          Universal Settings tab for retries + error handling. */}
      <Field label="Timeout (seconds)">
        <TextInput value={String(params.timeout ?? 30)} onChange={(v) => onChange(nodeId, { timeout: parseInt(v) || 30 })} placeholder="30" />
      </Field>
      <div className="text-xs text-slate-500 -mt-1">
        Retry uses exponential backoff on 429/5xx. Honors Retry-After header.
      </div>
    </>
  );
}

function WebhookSinkConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  return (
    <>
      <Field label="Connection">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['webhook', 'slack', 'teams', 'pagerduty', 'discord']}
          requireCapability="write"
        />
      </Field>
      <Field label="Webhook URL *">
        <TextInput value={params.url || ''} onChange={(v) => onChange(nodeId, { url: v })} placeholder="https://hooks.slack.com/services/..." />
      </Field>
      <Field label="Method">
        <Select value={params.method || 'POST'} onChange={(v) => onChange(nodeId, { method: v })} options={['POST', 'PUT']} />
      </Field>
      <Field label="Content Type">
        <Select value={params.content_type || 'application/json'} onChange={(v) => onChange(nodeId, { content_type: v })} options={['application/json', 'application/x-www-form-urlencoded', 'text/plain']} />
      </Field>
      <Field label="Secret / Signing Key (optional)">
        <SecretInput value={params.secret || ''} onChange={(v) => onChange(nodeId, { secret: v })} placeholder="whsec_..." />
      </Field>
      <Field label="Headers">
        {/* P2-D-2: JSON textarea replaced with KeyValueRepeater (P0-1: secret also masked) */}
        <KeyValueRepeater
          value={params.headers || {}}
          onChange={(next) => onChange(nodeId, { headers: next })}
          keyPlaceholder="Header name"
          valuePlaceholder="Value"
          addLabel="+ Add header"
        />
      </Field>
      <Field label="Payload Template (optional)">
        <TextArea value={params.payload_template || ''} onChange={(v) => onChange(nodeId, { payload_template: v })}
          placeholder='{"text": "Pipeline complete: {{row_count}} rows processed"}'
          rows={3}
        />
      </Field>
      {/* P0-4: per-node Retry Count removed — use Universal Settings tab. */}
    </>
  );
}

function EmailSinkConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const hasConn = !!params.connection_id;
  return (
    <>
      <Field label="Connection">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['smtp', 'email', 'sendgrid', 'ses', 'mailgun']}
          requireCapability="write"
        />
      </Field>
      {hasConn && (
        // P0-3 (2026-05-18): connection takes precedence — hide inline
        // SMTP/auth fields so the user doesn't fill in conflicting
        // credentials. Username/password/from come from the saved
        // connection record; if the user needs to override, they edit
        // the connection in Connections page.
        <div className="text-[11px] text-slate-500 bg-slate-50 border border-slate-200 rounded-md px-3 py-2">
          Using credentials from this connection. Inline SMTP fields are
          hidden. Edit the connection in <strong>Connections</strong> to change
          host / port / credentials.
        </div>
      )}
      {!hasConn && <>
      <Field label="SMTP Host *">
        <TextInput value={params.smtp_host || ''} onChange={(v) => onChange(nodeId, { smtp_host: v })} placeholder="smtp.gmail.com" />
      </Field>
      <Field label="SMTP Port">
        <TextInput value={String(params.smtp_port ?? 587)} onChange={(v) => onChange(nodeId, { smtp_port: parseInt(v) || 587 })} placeholder="587" />
      </Field>
      <Field label="Username *">
        <TextInput value={params.username || ''} onChange={(v) => onChange(nodeId, { username: v })} placeholder="sender@example.com" />
      </Field>
      <Field label="Password *">
        <SecretInput value={params.password || ''} onChange={(v) => onChange(nodeId, { password: v })} placeholder="••••••••" />
      </Field>
      <Field label="From Address *">
        <TextInput value={params.from_address || ''} onChange={(v) => onChange(nodeId, { from_address: v })} placeholder="pipeline@company.com" />
      </Field>
      </>}
      <Field label="To Addresses * (comma-separated)">
        <TextInput value={params.to_addresses || ''} onChange={(v) => onChange(nodeId, { to_addresses: v })} placeholder="team@company.com, lead@company.com" />
      </Field>
      <Field label="Subject">
        <TextInput value={params.subject || ''} onChange={(v) => onChange(nodeId, { subject: v })} placeholder="Pipeline Results - {{date}}" />
      </Field>
      <Field label="Body Template">
        <TextArea value={params.body || ''} onChange={(v) => onChange(nodeId, { body: v })}
          placeholder="Pipeline completed. {{row_count}} rows processed."
          rows={3}
        />
      </Field>
      <Field label="Attachment Format">
        <Select value={params.attachment_format || 'csv'} onChange={(v) => onChange(nodeId, { attachment_format: v })} options={['csv', 'json', 'xlsx', 'none']} />
      </Field>
      <Field label="Use TLS">
        <Toggle value={params.use_tls !== false} onChange={(v) => onChange(nodeId, { use_tls: v })} label="Enable TLS/STARTTLS" />
      </Field>
    </>
  );
}

function DeltaSinkConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  return (
    <>
      <Field label="Table Path *">
        <TextInput value={params.table_path || ''} onChange={(v) => onChange(nodeId, { table_path: v })} placeholder="s3://bucket/delta/output" />
      </Field>
      <Field label="Write Mode">
        <Select value={params.write_mode || 'overwrite'} onChange={(v) => onChange(nodeId, { write_mode: v })} options={['overwrite', 'append', 'merge']} />
      </Field>
      {/* #8 — overwrite-mode warning (Delta sink). Same rationale as
          the WarehouseSink warning above: overwrite replaces every
          existing row on each run. */}
      {(params.write_mode || 'overwrite') === 'overwrite' && (
        <div className="mt-1.5 mb-2 px-3 py-2 rounded-md bg-amber-50 border border-amber-300 text-[11px] text-amber-900 leading-snug">
          <span className="font-bold uppercase tracking-wider text-amber-700">⚠ Overwrite</span>{' '}
          The existing Delta table at this path is <strong>replaced</strong> on every run.
          Switch to <code className="px-1 rounded bg-amber-100 text-amber-900">merge</code> for idempotent re-runs or
          {' '}<code className="px-1 rounded bg-amber-100 text-amber-900">append</code> to preserve history.
        </div>
      )}
      {params.write_mode === 'merge' && (
        <>
          <Field label="Merge Key Columns (comma-separated)">
            <TextInput value={params.merge_keys || ''} onChange={(v) => onChange(nodeId, { merge_keys: v })} placeholder="id, date" />
          </Field>
          <Field label="Merge Predicate">
            <TextInput value={params.merge_predicate || ''} onChange={(v) => onChange(nodeId, { merge_predicate: v })} placeholder="source.id = target.id" />
          </Field>
        </>
      )}
      <Field label="Partition By (comma-separated)">
        <TextInput value={params.partition_by || ''} onChange={(v) => onChange(nodeId, { partition_by: v })} placeholder="year, month" />
      </Field>
      <Field label="Storage Options">
        {/* P2-D-3: JSON textarea replaced with KeyValueRepeater (secretValues
            so credentials in storage options also mask on screen) */}
        <KeyValueRepeater
          value={(params.storage_options && typeof params.storage_options === 'object') ? params.storage_options : {}}
          onChange={(next) => onChange(nodeId, { storage_options: Object.keys(next).length > 0 ? next : undefined })}
          keyPlaceholder="AWS_ACCESS_KEY_ID"
          valuePlaceholder="value"
          secretValues={true}
          addLabel="+ Add storage option"
        />
      </Field>
      <Field label="Schema Mode">
        <Select value={params.schema_mode || 'overwrite'} onChange={(v) => onChange(nodeId, { schema_mode: v })} options={['overwrite', 'merge']} />
      </Field>
    </>
  );
}

function WarehouseSinkConfig({ params, nodeId, onChange, columns = [], sourceTypes, mappingOnly }: ConfigProps) {
  const hasConn = !!params.connection_id;
  const [destSchema, setDestSchema] = useState<DestinationColumn[] | undefined>(undefined);
  const [importingSchema, setImportingSchema] = useState(false);

  const importDestSchema = async () => {
    const connId = params.connection_id;
    const tbl = params.table_name;
    if (!connId || !tbl) {
      toast.error('Cannot import schema', 'Pick a connection and table first.');
      return;
    }
    setImportingSchema(true);
    try {
      const res = await api.getConnectionTableColumns(connId, tbl, params.schema || '');
      const cols: DestinationColumn[] = (res?.columns || []).map((c) => ({
        name: c.name,
        type: c.type,
      }));
      if (cols.length === 0) {
        const fullName = params.schema ? `${params.schema}.${tbl}` : tbl;
        toast.info('No columns found', `Table ${fullName} may not exist yet — write mode 'create' will define it from the source schema.`);
      }
      setDestSchema(cols);
    } catch (e: any) {
      toast.error('Schema import failed', e?.message || String(e));
    } finally {
      setImportingSchema(false);
    }
  };

  if (mappingOnly === 'show') {
    if (columns.length === 0) {
      return (
        <div className="text-xs text-slate-500 px-1">
          No upstream columns available — connect an upstream node first.
        </div>
      );
    }
    return (
      <Field label="Column Mapping">
        <ColumnMapper
          columns={columns}
          mappings={params.column_mappings || {}}
          onChange={(mappings) => onChange(nodeId, { column_mappings: mappings })}
          sourceTypes={sourceTypes}
          destinationColumns={destSchema}
          onImportSchema={importDestSchema}
          importing={importingSchema}
          skipped={params.skipped_columns || []}
          onSkippedChange={(next) => onChange(nodeId, { skipped_columns: next })}
          castPolicy={params.cast_policy}
          onCastPolicyChange={(next) => onChange(nodeId, { cast_policy: next })}
        />
      </Field>
    );
  }
  return (
    <>
      <Field label="Connection">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['snowflake', 'bigquery', 'redshift', 'databricks', 'trino', 'clickhouse']}
          requireCapability="write"
        />
      </Field>
      {hasConn && (
        // P0-3 finisher (2026-05-18): warehouse type, DSN, and credentials
        // JSON come from the saved connection. Only schema / table /
        // write mode / batch / SQL hooks remain editable per-pipeline.
        <div className="text-[11px] text-slate-500 bg-slate-50 border border-slate-200 rounded-md px-3 py-2">
          Using warehouse type, connection string, and credentials from this connection.
          Edit the connection in <strong>Connections</strong> to change them.
        </div>
      )}
      {!hasConn && <>
        <Field label="Warehouse Type *">
          <Select value={params.warehouse_type || 'snowflake'} onChange={(v) => onChange(nodeId, { warehouse_type: v })}
            options={['snowflake', 'bigquery', 'redshift', 'databricks', 'trino', 'clickhouse']} />
        </Field>
        <Field label="Connection String / DSN *">
          <TextInput value={params.connection_string || ''} onChange={(v) => onChange(nodeId, { connection_string: v })}
            placeholder={params.warehouse_type === 'bigquery' ? 'project_id.dataset' : 'host:port/database'} />
        </Field>
      </>}
      <Field label="Schema">
        <TextInput value={params.schema || ''} onChange={(v) => onChange(nodeId, { schema: v })} placeholder="public" />
      </Field>
      <Field label="Table Name *">
        <TextInput value={params.table_name || ''} onChange={(v) => onChange(nodeId, { table_name: v })} placeholder="output_table" />
      </Field>
      <Field label="Write Mode">
        <Select value={params.write_mode || 'append'} onChange={(v) => onChange(nodeId, { write_mode: v })} options={['append', 'overwrite', 'upsert', 'merge']} />
      </Field>
      {/* #8 — overwrite-mode warning for the Bulk Loader / Warehouse
          sink. Overwrite means DROP + RECREATE of the target table
          on every run; the user vision flagged this as a destructive
          op that needs a visible warning. Rendered inline under the
          mode selector so it's impossible to miss while configuring. */}
      {params.write_mode === 'overwrite' && (
        <div className="mt-1.5 mb-2 px-3 py-2 rounded-md bg-amber-50 border border-amber-300 text-[11px] text-amber-900 leading-snug">
          <span className="font-bold uppercase tracking-wider text-amber-700">⚠ Overwrite</span>{' '}
          Existing rows in this table are <strong>deleted</strong> on every run before
          the new rows land. Re-running this pipeline against a populated
          target will lose data. Switch to <code className="px-1 rounded bg-amber-100 text-amber-900">merge</code> or
          {' '}<code className="px-1 rounded bg-amber-100 text-amber-900">upsert</code> if you want idempotent re-runs.
        </div>
      )}
      {(params.write_mode === 'upsert' || params.write_mode === 'merge') && (() => {
        // 2026-05-30: chip-toggle picker for primary keys when the
        // upstream schema is known. Free-text fallback is preserved so
        // operators on first-write tables (no upstream schema yet) can
        // still type the column name. Back-compat reads both array and
        // comma-string shapes so existing pipelines keep loading.
        const pkCols: string[] = Array.isArray(params.primary_keys)
          ? params.primary_keys
          : (params.primary_keys ? String(params.primary_keys).split(',').map((s) => s.trim()).filter(Boolean) : []);
        const togglePk = (col: string) => {
          const next = pkCols.includes(col)
            ? pkCols.filter((c) => c !== col)
            : [...pkCols, col];
          onChange(nodeId, { primary_keys: next });
        };
        return (
          <Field label="Primary Key Columns">
            {columns.length > 0 ? (
              <div className="flex flex-wrap gap-1.5 mb-1">
                {columns.map((col) => (
                  <button
                    key={col}
                    type="button"
                    onClick={() => togglePk(col)}
                    className={`text-xs px-2 py-1 rounded-lg font-mono border transition-colors ${
                      pkCols.includes(col)
                        ? 'bg-pipe-100 text-pipe-700 border-pipe-300'
                        : 'bg-white text-slate-500 border-slate-200 hover:border-pipe-200'
                    }`}
                  >
                    {pkCols.includes(col) && '✓ '}{col}
                  </button>
                ))}
              </div>
            ) : (
              <TextInput
                value={pkCols.join(', ')}
                onChange={(v) => onChange(nodeId, { primary_keys: v.split(',').map((s: string) => s.trim()).filter(Boolean) })}
                placeholder="id, tenant_id"
              />
            )}
          </Field>
        );
      })()}
      {!hasConn && (
        <Field label="Credentials (JSON)">
          <TextArea
            value={params.credentials ? JSON.stringify(params.credentials, null, 2) : ''}
            onChange={(v) => { try { onChange(nodeId, { credentials: v ? JSON.parse(v) : undefined }); } catch {} }}
            placeholder='{"user":"...","password":"...","account":"..."}'
            rows={3}
          />
        </Field>
      )}
      <Field label="Batch Size">
        <TextInput value={String(params.batch_size ?? 10000)} onChange={(v) => onChange(nodeId, { batch_size: parseInt(v) || 10000 })} placeholder="10000" />
      </Field>
      <Field label="Pre-SQL (optional)">
        <TextArea value={params.pre_sql || ''} onChange={(v) => onChange(nodeId, { pre_sql: v })}
          placeholder="TRUNCATE TABLE staging.tmp;" rows={2} />
      </Field>
      <Field label="Post-SQL (optional)">
        <TextArea value={params.post_sql || ''} onChange={(v) => onChange(nodeId, { post_sql: v })}
          placeholder="CALL refresh_summary();" rows={2} />
      </Field>
      {mappingOnly !== 'hide' && columns.length > 0 && (
        <Field label="Column Mapping">
          <ColumnMapper
            columns={columns}
            mappings={params.column_mappings || {}}
            onChange={(mappings) => onChange(nodeId, { column_mappings: mappings })}
            sourceTypes={sourceTypes}
            destinationColumns={destSchema}
            onImportSchema={importDestSchema}
            importing={importingSchema}
            skipped={params.skipped_columns || []}
            onSkippedChange={(next) => onChange(nodeId, { skipped_columns: next })}
          />
        </Field>
      )}
    </>
  );
}

function DbSinkConfig({ params, nodeId, onChange, columns = [], sourceTypes, mappingOnly, hideConnection }: ConfigProps) {
  const [destSchema, setDestSchema] = useState<DestinationColumn[] | undefined>(undefined);
  const [importingSchema, setImportingSchema] = useState(false);

  const importDestSchema = async () => {
    const connId = params.connection_id;
    const tbl = params.table || params.table_name;
    if (!connId || !tbl) {
      toast.error('Cannot import schema', 'Pick a connection and table first.');
      return;
    }
    setImportingSchema(true);
    try {
      const res = await api.getConnectionTableColumns(connId, tbl, params.schema || '');
      const cols: DestinationColumn[] = (res?.columns || []).map((c) => ({
        name: c.name,
        type: c.type,
      }));
      if (cols.length === 0) {
        const fullName = params.schema ? `${params.schema}.${tbl}` : tbl;
        toast.info('No columns found', `Table ${fullName} may not exist yet — write mode 'create' will define it from the source schema.`);
      }
      setDestSchema(cols);
    } catch (e: any) {
      toast.error('Schema import failed', e?.message || String(e));
    } finally {
      setImportingSchema(false);
    }
  };

  // Mapping-only render: surfaced by the standalone "Mapping" tab on
  // ConfigPanel so the source→destination ColumnMapper has its own
  // workspace instead of sitting at the bottom of the Parameters tab.
  if (mappingOnly === 'show') {
    if (columns.length === 0) {
      return (
        <div className="text-xs text-slate-500 px-1">
          No upstream columns available — connect an upstream node first.
        </div>
      );
    }
    return (
      <Field label="Column Mapping (Source → Destination)">
        <ColumnMapper
          columns={columns}
          mappings={params.column_mappings || {}}
          onChange={(mappings) => onChange(nodeId, { column_mappings: mappings })}
          sourceTypes={sourceTypes}
          destinationColumns={destSchema}
          onImportSchema={importDestSchema}
          importing={importingSchema}
          skipped={params.skipped_columns || []}
          onSkippedChange={(next) => onChange(nodeId, { skipped_columns: next })}
          castPolicy={params.cast_policy}
          onCastPolicyChange={(next) => onChange(nodeId, { cast_policy: next })}
        />
      </Field>
    );
  }
  return (
    <>
      {/* Connection picker — suppressed when embedded in the generic
          Destination, which already owns the connection above (no dual
          control). Standalone db_sink keeps its own picker. */}
      {!hideConnection && (
        <>
          <Field label="Connection">
            <ConnectionPicker
              value={params.connection_id || ''}
              onChange={(v) => onChange(nodeId, { connection_id: v })}
              filter={['postgresql', 'mysql', 'mssql', 'sqlite']}
              requireCapability="write"
            />
          </Field>
          {!params.connection_id && (
            <div className="bg-blue-50 rounded-lg px-3 py-1.5 border border-blue-100">
              <span className="text-[9px] text-blue-500">No connection selected — writes to DuckDB in-memory.</span>
            </div>
          )}
        </>
      )}
      {/* Browse-tables popup + inline catalog-backed datalists. Picking
          from any of these fills schema + table. Typing a name that
          isn't in the suggestions is also allowed — the write mode
          'create' below will create the new table on first run. */}
      {params.connection_id && (
        <div className="flex items-center justify-between gap-2 -mb-1">
          <span className="text-[10px] text-slate-500">Pick an existing table or type a new name below to create it.</span>
          <CatalogPicker
            connectionId={params.connection_id}
            kinds={['table', 'view']}
            label="Browse tables"
            compact
            onPick={(item) => onChange(nodeId, { schema: item.parent, table: item.name })}
          />
        </div>
      )}
      <div className="grid grid-cols-2 gap-2">
        <Field label="Schema (optional)">
          <CatalogDatalistInput
            connectionId={params.connection_id}
            field="schema"
            value={params.schema || ''}
            onChange={(v) => onChange(nodeId, { schema: v })}
            placeholder="public"
          />
        </Field>
        <Field label="Table Name *">
          <CatalogDatalistInput
            connectionId={params.connection_id}
            field="table"
            schema={params.schema || ''}
            value={params.table || params.table_name || ''}
            onChange={(v) => onChange(nodeId, { table: v })}
            placeholder="output_table"
          />
        </Field>
      </div>
      <Field label="Write Mode">
        <Select
          value={params.mode || params.write_mode || 'create'}
          onChange={(v) => onChange(nodeId, { mode: v })}
          options={['create', 'append', 'truncate']}
        />
      </Field>
      <Field label="Batch Size">
        <TextInput value={String(params.batch_size ?? 5000)} onChange={(v) => onChange(nodeId, { batch_size: parseInt(v) || 5000 })}
          placeholder="5000" />
      </Field>
      <Field label="Pre-SQL (optional)">
        <TextArea value={params.pre_sql || ''} onChange={(v) => onChange(nodeId, { pre_sql: v })}
          placeholder="TRUNCATE TABLE staging.tmp;" rows={2} />
      </Field>
      <Field label="Post-SQL (optional)">
        <TextArea value={params.post_sql || ''} onChange={(v) => onChange(nodeId, { post_sql: v })}
          placeholder="CALL refresh_summary();" rows={2} />
      </Field>
      {mappingOnly !== 'hide' && columns.length > 0 && (
        <Field label="Column Mapping (Source → Destination)">
          <ColumnMapper
            columns={columns}
            mappings={params.column_mappings || {}}
            onChange={(mappings) => onChange(nodeId, { column_mappings: mappings })}
            sourceTypes={sourceTypes}
            destinationColumns={destSchema}
            onImportSchema={importDestSchema}
            importing={importingSchema}
            skipped={params.skipped_columns || []}
            onSkippedChange={(next) => onChange(nodeId, { skipped_columns: next })}
          />
        </Field>
      )}
    </>
  );
}

/* ═══════════════════════════════════════════════════════
   Flow Control Configs
   ═══════════════════════════════════════════════════════ */

function IfConditionConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  // 2026-05-30 audit fix: canonical field is `condition` (matches backend
  // IfConditionNode.execute + param_schema). Old saved pipelines used
  // `expression`; read either, write only the canonical name.
  const conditionValue = params.condition ?? params.expression ?? '';
  return (
    <>
      <Field label="Condition Expression *">
        <TextArea
          value={conditionValue}
          onChange={(v) => onChange(nodeId, { condition: v })}
          placeholder="status = 'active' AND amount > 0"
          rows={3}
        />
      </Field>
      {columns.length > 0 && <ColumnPicker columns={columns} onInsert={(col) => onChange(nodeId, { condition: conditionValue + col })} />}
      <Field label="True Branch Label">
        <TextInput value={params.true_label || 'True'} onChange={(v) => onChange(nodeId, { true_label: v })} placeholder="True" />
      </Field>
      <Field label="False Branch Label">
        <TextInput value={params.false_label || 'False'} onChange={(v) => onChange(nodeId, { false_label: v })} placeholder="False" />
      </Field>
      <div className="bg-blue-50 rounded-lg px-3 py-2 border border-blue-100">
        <div className="text-[9px] text-blue-500 space-y-1">
          <div>Use <code className="bg-white px-1 rounded border border-blue-200">@equals(a, b)</code>, <code className="bg-white px-1 rounded border border-blue-200">@greater(a, b)</code>, <code className="bg-white px-1 rounded border border-blue-200">@and(x, y)</code></div>
          <div>Connect <strong>On Success</strong> edge for True, <strong>On Failure</strong> edge for False</div>
        </div>
      </div>
    </>
  );
}

function SwitchCaseConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const cases = Array.isArray(params.cases) ? params.cases : [{ value: '', label: 'Case 1' }];

  const updateCase = (idx: number, field: string, value: string) => {
    const updated = cases.map((c: any, i: number) => i === idx ? { ...c, [field]: value } : c);
    onChange(nodeId, { cases: updated });
  };

  const addCase = () => onChange(nodeId, { cases: [...cases, { value: '', label: `Case ${cases.length + 1}` }] });
  const removeCase = (idx: number) => onChange(nodeId, { cases: cases.filter((_: any, i: number) => i !== idx) });

  // 2026-05-30 audit fix: canonical fields are `column` + `default_case`
  // (matches backend SwitchCaseNode.execute). Legacy keys `on` /
  // `default_label` are read for old pipelines but new writes go to the
  // canonical names so the executor actually sees them.
  const columnValue = params.column ?? params.on ?? '';
  const defaultValue = params.default_case ?? params.default_label ?? 'Default';
  return (
    <>
      <Field label="Switch On (Column) *">
        {columns.length > 0 ? (
          <select
            value={columnValue}
            onChange={(e) => onChange(nodeId, { column: e.target.value })}
            className="w-full px-2.5 py-1.5 text-sm border border-slate-200 rounded-lg bg-white font-mono focus:outline-none focus:ring-2 focus:ring-pipe-300"
          >
            <option value="">— Pick a column —</option>
            {columns.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        ) : (
          <TextInput value={columnValue} onChange={(v) => onChange(nodeId, { column: v })} placeholder="status" />
        )}
      </Field>
      <Field label="Cases">
        <div className="space-y-2">
          {cases.map((c: any, i: number) => (
            <div key={i} className="flex items-center gap-1.5">
              <TextInput value={c.value} onChange={(v) => updateCase(i, 'value', v)} placeholder={`Value ${i + 1}`} />
              <TextInput value={c.label} onChange={(v) => updateCase(i, 'label', v)} placeholder="Label" />
              {cases.length > 1 && (
                <button onClick={() => removeCase(i)} className="text-red-400 hover:text-red-600 shrink-0" title="Remove">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                </button>
              )}
            </div>
          ))}
          <button onClick={addCase} className="text-xs text-pipe-600 hover:text-pipe-800 font-medium">+ Add Case</button>
        </div>
      </Field>
      <Field label="Default Case (fallback SQL condition)">
        <TextInput value={defaultValue} onChange={(v) => onChange(nodeId, { default_case: v })} placeholder="e.g. 1=1 (always match)" />
      </Field>
    </>
  );
}

function ForEachLoopConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  return (
    <>
      <Field label="Batch Size (rows per batch)">
        <TextInput value={String(params.batch_size ?? 100)} onChange={(v) => onChange(nodeId, { batch_size: parseInt(v) || 100 })} placeholder="100" />
      </Field>
      {columns.length > 0 && <ColumnPicker columns={columns} onInsert={(col) => onChange(nodeId, { items: col })} />}
      <Field label="Mode">
        <Select value={params.mode || 'sequential'} onChange={(v) => onChange(nodeId, { mode: v })}
          options={['sequential', 'parallel']} />
      </Field>
      <Field label="On Error">
        <Select value={params.on_error || 'fail'} onChange={(v) => onChange(nodeId, { on_error: v })}
          options={['fail', 'continue']} />
      </Field>
      <div className="bg-slate-50 rounded-lg px-3 py-2 border border-slate-100">
        <div className="text-xs text-slate-500">
          Splits input into batches of N rows. Each batch gets <code className="bg-white px-1 rounded border border-slate-200 text-pipe-600">_batch_index</code> and <code className="bg-white px-1 rounded border border-slate-200 text-pipe-600">_batch_total</code> metadata columns. Results are UNION ALL'd.
        </div>
      </div>
    </>
  );
}

function UntilLoopConfig({ params, nodeId, onChange }: ConfigProps) {
  // 2026-05-30 audit fix: canonical fields are `condition` +
  // `max_iterations` (matches backend UntilLoopNode.execute). Legacy
  // keys `expression` / `limit` are read for old pipelines but new
  // writes go to the canonical names.
  const conditionValue = params.condition ?? params.expression ?? '';
  const iterValue = params.max_iterations ?? params.limit ?? 10;
  return (
    <>
      <Field label="Stop Condition *">
        <TextArea
          value={conditionValue}
          onChange={(v) => onChange(nodeId, { condition: v })}
          placeholder="retry_count >= 3"
          rows={3}
        />
      </Field>
      <Field label="Max Iterations">
        <TextInput value={String(iterValue)} onChange={(v) => onChange(nodeId, { max_iterations: parseInt(v) || 10 })} placeholder="10" />
      </Field>
      <div className="bg-amber-50 rounded-lg px-3 py-2 border border-amber-100">
        <div className="text-[9px] text-amber-600">Loop stops when ALL rows satisfy the condition, or after max iterations (hard cap 1000).</div>
      </div>
    </>
  );
}

function WaitDelayConfig({ params, nodeId, onChange }: ConfigProps) {
  // 2026-05-30 audit fix: canonical field is `seconds` (matches backend
  // WaitDelayNode.execute). Legacy keys `duration`+`unit` were silently
  // ignored. We compute seconds on write and read legacy values back
  // when loading old pipelines. Max 300s hard cap (backend enforces).
  const legacyMul: Record<string, number> = { seconds: 1, minutes: 60, hours: 3600 };
  const legacySec = params.duration != null
    ? Number(params.duration) * (legacyMul[String(params.unit || 'seconds')] || 1)
    : null;
  const secondsValue = params.seconds ?? legacySec ?? 1;
  return (
    <>
      <Field label="Delay (seconds) *">
        <TextInput
          value={String(secondsValue)}
          onChange={(v) => onChange(nodeId, { seconds: Math.max(0, Math.min(parseInt(v) || 0, 300)) })}
          placeholder="5"
        />
      </Field>
      <div className="bg-slate-50 rounded-lg px-3 py-2 border border-slate-100">
        <div className="text-xs text-slate-500">Pauses pipeline execution for N seconds (capped at 300s / 5min).</div>
      </div>
    </>
  );
}

function SetVariableConfig({ params, nodeId, onChange }: ConfigProps) {
  const variables = Array.isArray(params.variables) ? params.variables : [{ name: '', expression: '' }];

  const updateVar = (idx: number, field: string, value: string) => {
    const updated = variables.map((v: any, i: number) => i === idx ? { ...v, [field]: value } : v);
    onChange(nodeId, { variables: updated });
  };

  const addVar = () => onChange(nodeId, { variables: [...variables, { name: '', expression: '' }] });
  const removeVar = (idx: number) => onChange(nodeId, { variables: variables.filter((_: any, i: number) => i !== idx) });

  return (
    <>
      <Field label="Variables">
        <div className="space-y-2">
          {variables.map((v: any, i: number) => (
            <div key={i} className="flex items-center gap-1.5">
              <TextInput value={v.name} onChange={(val) => updateVar(i, 'name', val)} placeholder="var_name" />
              <span className="text-slate-400 text-xs">=</span>
              {/* Backend reads `expression` (a SQL/constant expr evaluated once),
                  NOT `value`. Read the legacy `value` key so old pipelines migrate
                  on first edit. (2026-06-15 drift fix.) */}
              <TextInput value={v.expression ?? v.value ?? ''} onChange={(val) => updateVar(i, 'expression', val)} placeholder="'prod', 42, MAX(updated_at)" />
              {variables.length > 1 && (
                <button onClick={() => removeVar(i)} className="text-red-400 hover:text-red-600 shrink-0" title="Remove">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                </button>
              )}
            </div>
          ))}
          <button onClick={addVar} className="text-xs text-pipe-600 hover:text-pipe-800 font-medium">+ Add Variable</button>
        </div>
      </Field>
      <div className="bg-slate-50 rounded-lg px-3 py-2 border border-slate-100">
        <div className="text-xs text-slate-500">Each value is a SQL/constant expression evaluated once. Read downstream via <code className="bg-white px-1 rounded border border-slate-200 text-pipe-600">{'{{ $vars.var_name }}'}</code>. Input rows pass through unchanged.</div>
      </div>
    </>
  );
}

function ExecutePipelineConfig({ params, nodeId, onChange }: ConfigProps) {
  const currentWorkflowId = useWorkflowStore((s) => s.workflowId);
  const [workflows, setWorkflows] = useState<Array<{ id: string; name: string }>>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.listWorkflows()
      .then((rows: any[]) => {
        if (cancelled) return;
        // Drop the current workflow — calling yourself is an instant
        // infinite loop and the executor will reject it anyway.
        const filtered = (Array.isArray(rows) ? rows : [])
          .filter((w) => w.id !== currentWorkflowId)
          .map((w) => ({ id: w.id, name: w.name || w.id }));
        setWorkflows(filtered);
        setLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e?.message || 'Could not load workflows');
        setLoading(false);
      });
    return () => { cancelled = true; };
  }, [currentWorkflowId]);

  // Parameter pairs editor — key/value list. Internally
  // stored as a flat object on params.parameters to match the backend
  // contract (params.parameters is a dict that gets merged into
  // wf.metadata.parameters by ExecutePipelineNode).
  const paramPairs: Array<[string, string]> = (() => {
    const obj = (params.parameters && typeof params.parameters === 'object') ? params.parameters : {};
    const entries = Object.entries(obj as Record<string, any>);
    return entries.length > 0
      ? entries.map(([k, v]) => [k, typeof v === 'string' ? v : JSON.stringify(v)])
      : [];
  })();

  const writeParams = (pairs: Array<[string, string]>) => {
    const obj: Record<string, any> = {};
    for (const [k, v] of pairs) {
      if (!k) continue;
      // Try JSON-parse so numbers/booleans round-trip; fall back to string.
      try {
        obj[k] = v === '' ? '' : JSON.parse(v);
      } catch {
        obj[k] = v;
      }
    }
    onChange(nodeId, { parameters: obj });
  };

  const setPair = (idx: number, key: string, value: string) => {
    const next = [...paramPairs];
    next[idx] = [key, value];
    writeParams(next);
  };
  const addPair = () => writeParams([...paramPairs, ['', '']]);
  const removePair = (idx: number) => writeParams(paramPairs.filter((_, i) => i !== idx));

  return (
    <>
      <Field label="Sub-Pipeline *">
        {loading ? (
          <div className="text-xs text-slate-400 italic">Loading workflows…</div>
        ) : error ? (
          <div className="text-xs text-red-500">{error}</div>
        ) : workflows.length === 0 ? (
          <div className="text-xs text-slate-400 italic">
            No other workflows in this workspace. Create one first to call it from here.
          </div>
        ) : (
          <select
            value={params.pipeline_id || ''}
            onChange={(e) => onChange(nodeId, { pipeline_id: e.target.value })}
            className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300 bg-white"
          >
            <option value="">— Select a sub-pipeline —</option>
            {workflows.map((w) => (
              <option key={w.id} value={w.id}>{w.name}</option>
            ))}
          </select>
        )}
        <p className="text-xs text-slate-400 mt-1">
          Pipelines call themselves = infinite loop, so the current pipeline is excluded.
        </p>
      </Field>

      <Field label="Wait on Completion">
        <Toggle
          value={params.wait_for_completion !== false}
          onChange={(v) => onChange(nodeId, { wait_for_completion: v })}
          label="Block until the sub-pipeline finishes"
        />
      </Field>

      <Field label="Parameters">
        <div className="space-y-1.5">
          {paramPairs.length === 0 && (
            <p className="text-xs text-slate-400 italic">
              No parameters. Click + to pass values to the sub-pipeline.
            </p>
          )}
          {paramPairs.map((pair, idx) => (
            <div key={idx} className="flex gap-1.5 items-center">
              <input
                type="text"
                value={pair[0]}
                onChange={(e) => setPair(idx, e.target.value, pair[1])}
                placeholder="key"
                className="flex-1 text-xs px-2 py-1.5 border border-slate-200 rounded-md focus:outline-none focus:ring-1 focus:ring-blue-300 font-mono"
              />
              <span className="text-slate-300 text-xs">=</span>
              <input
                type="text"
                value={pair[1]}
                onChange={(e) => setPair(idx, pair[0], e.target.value)}
                placeholder='value (or "string", 42, true, [...])'
                className="flex-1 text-xs px-2 py-1.5 border border-slate-200 rounded-md focus:outline-none focus:ring-1 focus:ring-blue-300 font-mono"
              />
              <button
                onClick={() => removePair(idx)}
                className="text-slate-400 hover:text-red-500 px-1.5 py-1 rounded"
                title="Remove"
              >
                ×
              </button>
            </div>
          ))}
          <button
            onClick={addPair}
            className="text-xs font-semibold text-blue-600 hover:text-blue-700 mt-1"
          >
            + Add parameter
          </button>
        </div>
      </Field>

      <Field label="On Failure">
        <Select value={params.on_failure || 'fail'} onChange={(v) => onChange(nodeId, { on_failure: v })} options={['fail', 'skip', 'continue']} />
      </Field>
    </>
  );
}

/* ═══════════════════════════════════════════════════════
   SaaS Connector — universal manifest-driven node
   ═══════════════════════════════════════════════════════ */

interface SaasManifest {
  id: string;
  name: string;
  description?: string;
  category?: string;
  params?: Array<{ name: string; label?: string; type?: string; required?: boolean; default?: any; secret?: boolean }>;
  streams?: Array<{ name: string; label?: string }>;
}

function SaaSConnectorConfig({ params, nodeId, onChange }: ConfigProps) {
  const [manifests, setManifests] = useState<SaasManifest[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch('/api/saas/manifests')
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data) => {
        if (cancelled) return;
        setManifests(Array.isArray(data) ? data : []);
        setLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(String(e?.message || e));
        setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  const selected = manifests.find((m) => m.id === params.connector_id);
  const streams = selected?.streams || [];
  const manifestParams = selected?.params || [];

  if (loading) {
    return <div className="text-xs text-slate-400 italic">Loading SaaS connectors…</div>;
  }
  if (error) {
    return <div className="text-xs text-red-500">Failed to load connector definitions: {error}</div>;
  }
  if (manifests.length === 0) {
    // #4 / Tier-7 (2026-05-19): give the empty state real affordances
    // instead of a one-liner. SaaS manifests live in the backend's
    // `connectors/manifests/` directory; the user has 3 paths forward
    // depending on what they want to do.
    return (
      <div className="text-xs text-slate-600 bg-slate-50 border border-slate-200 rounded-md p-3 space-y-2">
        <div className="font-semibold text-slate-800">No SaaS connectors yet</div>
        <p className="text-slate-500 leading-snug">
          SaaS connector manifests (Salesforce, HubSpot, Stripe, etc.)
          live in the backend's <code className="text-[10px] bg-white px-1 rounded border border-slate-200">connectors/manifests/</code> directory.
          F-Pulse OSS doesn't bundle pre-built ones — every connector is open
          source, so you add the ones you need.
        </p>
        <div className="flex flex-wrap gap-2 pt-1">
          <a
            href="/help#docs/connectors.md"
            className="inline-flex items-center gap-1 text-[11px] font-medium text-pipe-700 hover:text-pipe-900 hover:underline"
          >
            Read the connector manifest guide
            <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <path d="M5 12h14" /><path d="M12 5l7 7-7 7" />
            </svg>
          </a>
          <span className="text-slate-300">·</span>
          <a
            href="https://github.com/hybridyn/fpulse/tree/main/backend/fpulse/connectors/manifests"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-[11px] font-medium text-slate-700 hover:text-slate-900 hover:underline"
          >
            Browse community manifests
            <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
              <polyline points="15 3 21 3 21 9" />
              <line x1="10" y1="14" x2="21" y2="3" />
            </svg>
          </a>
        </div>
        <p className="text-[10px] text-slate-400 pt-1">
          Already have an HTTP API endpoint to call? Use the
          <strong className="text-slate-500"> HTTP Request</strong> node — no
          manifest required.
        </p>
      </div>
    );
  }

  return (
    <>
      <Field label="SaaS Connector *">
        <select
          value={params.connector_id || ''}
          onChange={(e) => {
            const id = e.target.value;
            // Reset stream + manifest params when switching connector
            const next: Record<string, any> = { connector_id: id, stream: '' };
            // Pre-fill defaults from the new manifest
            const m = manifests.find((mm) => mm.id === id);
            for (const p of m?.params || []) {
              if (p.default !== undefined && params[p.name] === undefined) {
                next[p.name] = p.default;
              }
            }
            onChange(nodeId, next);
          }}
          className="w-full px-2.5 py-1.5 text-xs text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
        >
          <option value="">— Select a connector —</option>
          {manifests.map((m) => (
            <option key={m.id} value={m.id}>{m.name}</option>
          ))}
        </select>
      </Field>

      {selected && selected.description && (
        <div className="text-xs text-slate-400 -mt-2 mb-1">{selected.description}</div>
      )}

      {selected && (
        <Field label="Stream / Endpoint *">
          <select
            value={params.stream || ''}
            onChange={(e) => onChange(nodeId, { stream: e.target.value })}
            className="w-full px-2.5 py-1.5 text-xs text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
          >
            <option value="">— Select a stream —</option>
            {streams.map((s) => (
              <option key={s.name} value={s.name}>{s.label || s.name}</option>
            ))}
          </select>
        </Field>
      )}

      {selected && manifestParams.length > 0 && (
        <div className="mt-2 pt-2 border-t border-slate-100">
          <div className="text-[9px] uppercase tracking-wider text-slate-400 mb-1.5 font-semibold">Connector Parameters</div>
          {manifestParams.map((p) => (
            <Field key={p.name} label={`${p.label || p.name}${p.required ? ' *' : ''}`}>
              <input
                type={p.secret ? 'password' : 'text'}
                value={params[p.name] ?? ''}
                onChange={(e) => onChange(nodeId, { [p.name]: e.target.value })}
                placeholder={p.default !== undefined ? String(p.default) : ''}
                className="w-full px-2.5 py-1.5 text-xs text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
              />
            </Field>
          ))}
        </div>
      )}
    </>
  );
}

/* ═══════════════════════════════════════════════════════
   Quality / Shape Configs (Sprint B)
   ═══════════════════════════════════════════════════════ */

const DQ_OPS = [
  'not_null', 'is_null', 'eq', 'ne', 'gt', 'lt', 'gte', 'lte',
  'in', 'not_in', 'regex', 'between', 'min_length', 'max_length',
];

function DataQualityConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const rules = Array.isArray(params.rules) ? params.rules : [];

  const update = (idx: number, patch: Record<string, any>) => {
    const next = rules.map((r: any, i: number) => i === idx ? { ...r, ...patch } : r);
    onChange(nodeId, { rules: next });
  };
  const addRule = () => onChange(nodeId, { rules: [...rules, { column: columns[0] || '', op: 'not_null', value: '' }] });
  const removeRule = (idx: number) => onChange(nodeId, { rules: rules.filter((_: any, i: number) => i !== idx) });

  return (
    <>
      <Field label="On Failure">
        <Select
          value={params.mode || 'drop'}
          onChange={(v) => onChange(nodeId, { mode: v })}
          options={['drop', 'fail', 'tag', 'reject', 'split']}
        />
        {params.mode === 'reject' && (
          <div className="text-[10px] text-slate-400 mt-1">
            Two outputs: wire the <span className="font-semibold text-green-600">Pass</span> and{' '}
            <span className="font-semibold text-red-500">Reject</span> handles to separate paths
            (e.g. reject → a quarantine sink).
          </div>
        )}
      </Field>
      <Field label={`Validation Rules (${rules.length})`}>
        <div className="flex flex-col gap-1.5">
          {rules.map((rule: any, idx: number) => (
            <div key={idx} className="flex items-center gap-1">
              {columns.length > 0 ? (
                <Select
                  value={rule.column || columns[0]}
                  onChange={(v) => update(idx, { column: v })}
                  options={columns}
                />
              ) : (
                <input
                  value={rule.column || ''}
                  onChange={(e) => update(idx, { column: e.target.value })}
                  placeholder="column"
                  className="w-24 px-2 py-1 text-xs border border-slate-200 rounded"
                />
              )}
              <Select
                value={rule.op || 'not_null'}
                onChange={(v) => update(idx, { op: v })}
                options={DQ_OPS}
              />
              {rule.op !== 'not_null' && rule.op !== 'is_null' && (
                <input
                  value={rule.value ?? ''}
                  onChange={(e) => update(idx, { value: e.target.value })}
                  placeholder="value"
                  className="flex-1 px-2 py-1 text-xs border border-slate-200 rounded"
                />
              )}
              <button
                onClick={() => removeRule(idx)}
                className="text-xs text-red-500 hover:text-red-700 px-1"
                title="Remove rule"
              >✕</button>
            </div>
          ))}
          <button
            onClick={addRule}
            className="text-xs text-pipe-600 hover:text-pipe-800 self-start"
          >+ Add rule</button>
        </div>
      </Field>
      <Field label="Quality Score Threshold (%)">
        <TextInput value={String(params.quality_threshold ?? 0)} onChange={(v) => onChange(nodeId, { quality_threshold: parseFloat(v) || 0 })}
          placeholder="0 = disabled, e.g. 95 = require 95% pass rate" />
      </Field>
      <Field label="Per-Row Quality Score">
        <Toggle value={!!params.include_score} onChange={(v) => onChange(nodeId, { include_score: v })}
          label="Add __dq_score column (0-100%)" />
      </Field>
      <Field label="Column Profiling">
        <Toggle value={!!params.include_profile} onChange={(v) => onChange(nodeId, { include_profile: v })}
          label="Log null %, distinct count per column" />
      </Field>
    </>
  );
}

function UpsertConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const keys: string[] = Array.isArray(params.key) ? params.key : [];
  const toggleKey = (col: string) => {
    const next = keys.includes(col) ? keys.filter((k) => k !== col) : [...keys, col];
    onChange(nodeId, { key: next });
  };

  return (
    <>
      <Field label="Key Columns *">
        {columns.length > 0 ? (
          <div className="flex flex-wrap gap-1">
            {columns.map((c) => (
              <button
                key={c}
                onClick={() => toggleKey(c)}
                className={`text-xs px-2 py-1 rounded-lg border transition-colors ${
                  keys.includes(c)
                    ? 'bg-pipe-100 text-pipe-700 border-pipe-300'
                    : 'bg-white text-slate-400 border-slate-200'
                }`}
              >
                {keys.includes(c) && '✓ '}{c}
              </button>
            ))}
          </div>
        ) : (
          <TextInput
            value={Array.isArray(params.key) ? params.key.join(',') : (params.key || '')}
            onChange={(v) => onChange(nodeId, { key: v.split(',').map((s) => s.trim()).filter(Boolean) })}
            placeholder="id, tenant_id"
          />
        )}
      </Field>
      <Field label="Order By (latest wins)">
        <TextInput
          value={params.order_by || ''}
          onChange={(v) => onChange(nodeId, { order_by: v })}
          placeholder="updated_at DESC"
        />
      </Field>
      <div className="text-xs text-slate-400 italic">
        Collapses duplicate keys to one row. Pipeline becomes idempotent — safe to re-run.
      </div>
    </>
  );
}

/** Toggleable column-chip picker used by the guided SCD2 form. Falls back to
 *  a comma-separated text input when the upstream schema isn't known yet. */
function ScdChipPicker({
  label, hint, selected, columns, exclude = [], onToggle, onTextChange, placeholder,
}: {
  label: string;
  hint?: string;
  selected: string[];
  columns: string[];
  exclude?: string[];
  onToggle: (col: string) => void;
  onTextChange: (cols: string[]) => void;
  placeholder?: string;
}) {
  const avail = columns.filter((c) => !exclude.includes(c));
  return (
    <Field label={label}>
      {hint && <div className="text-[10px] text-slate-400 mb-1 leading-snug">{hint}</div>}
      {avail.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {avail.map((c) => (
            <button
              key={c}
              onClick={() => onToggle(c)}
              className={`text-xs px-2 py-1 rounded-lg border transition-colors ${
                selected.includes(c)
                  ? 'bg-pipe-100 text-pipe-700 border-pipe-300'
                  : 'bg-white text-slate-400 border-slate-200'
              }`}
            >
              {selected.includes(c) && '✓ '}{c}
            </button>
          ))}
        </div>
      ) : (
        <TextInput
          value={selected.join(', ')}
          onChange={(v) => onTextChange(v.split(',').map((s) => s.trim()).filter(Boolean))}
          placeholder={placeholder}
        />
      )}
    </Field>
  );
}

/** Guided SCD Type-2 form — turns the flat param_schema (DynamicConfig's
 *  "N defaults applied" chip) into a task-shaped flow: business key →
 *  tracked attributes → passthrough → delete policy → (advanced) output
 *  column names. Schema-aware chip pickers when the upstream columns are
 *  known; text fallback otherwise. */
function SCD2Config({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const businessKey: string[] = Array.isArray(params.business_key) ? params.business_key : [];
  const tracked: string[] = Array.isArray(params.tracked_columns) ? params.tracked_columns : [];
  const passthrough: string[] = Array.isArray(params.passthrough_columns) ? params.passthrough_columns : [];
  const [showAdvanced, setShowAdvanced] = useState(false);

  const toggle = (field: string, current: string[], col: string) => {
    const next = current.includes(col) ? current.filter((k) => k !== col) : [...current, col];
    onChange(nodeId, { [field]: next });
  };

  const colFrom = params.effective_from_column || 'valid_from';
  const colTo = params.effective_to_column || 'valid_to';
  const colCurrent = params.current_flag_column || 'is_current';
  const colSk = params.surrogate_key_column || 'scd_id';
  const nullHi = params.null_high_water || '9999-12-31';
  return (
    <>
      <div className="text-xs text-slate-500 mb-1 leading-snug">
        Keeps change history per entity. <span className="font-medium text-slate-600">Input 1</span> = new feed,{' '}
        <span className="font-medium text-slate-600">Input 2</span> = existing snapshot (optional → initial load).
      </div>

      <ScdChipPicker
        label="Business Key *"
        hint="Identifies one entity (e.g. customer_id)."
        selected={businessKey}
        columns={columns}
        onToggle={(c) => toggle('business_key', businessKey, c)}
        onTextChange={(cols) => onChange(nodeId, { business_key: cols })}
        placeholder="customer_id"
      />

      <ScdChipPicker
        label="Tracked Columns *"
        hint="A change here opens a new version (e.g. address, tier)."
        selected={tracked}
        columns={columns}
        exclude={businessKey}
        onToggle={(c) => toggle('tracked_columns', tracked, c)}
        onTextChange={(cols) => onChange(nodeId, { tracked_columns: cols })}
        placeholder="address, tier"
      />

      <ScdChipPicker
        label="Passthrough Columns"
        hint="Carried along, not version-tracked."
        selected={passthrough}
        columns={columns}
        exclude={[...businessKey, ...tracked]}
        onToggle={(c) => toggle('passthrough_columns', passthrough, c)}
        onTextChange={(cols) => onChange(nodeId, { passthrough_columns: cols })}
        placeholder="source_system"
      />

      <Field label="When a key disappears from the feed">
        <Select
          value={params.delete_detection || 'ignore'}
          onChange={(v) => onChange(nodeId, { delete_detection: v })}
          options={['ignore', 'soft_close']}
          labels={{
            ignore: 'Ignore — keep the row current (orphan)',
            soft_close: 'Soft close — end the version (is_current=false)',
          }}
        />
      </Field>

      <button
        onClick={() => setShowAdvanced((s) => !s)}
        className="text-xs text-pipe-600 hover:text-pipe-800 self-start mt-1"
      >
        {showAdvanced ? '− Hide' : '+ Show'} version column names
      </button>

      {showAdvanced && (
        <div className="flex flex-col gap-2 border-l-2 border-slate-100 pl-3 mt-1">
          <div className="text-[10px] text-slate-400 leading-snug">
            These columns are added to the output. Defaults suit most warehouses — change only to match an existing table.
          </div>
          <Field label="Surrogate Key Column">
            <TextInput value={colSk} onChange={(v) => onChange(nodeId, { surrogate_key_column: v })} placeholder="scd_id" />
          </Field>
          <Field label="Valid-From Column">
            <TextInput value={colFrom} onChange={(v) => onChange(nodeId, { effective_from_column: v })} placeholder="valid_from" />
          </Field>
          <Field label="Valid-To Column">
            <TextInput value={colTo} onChange={(v) => onChange(nodeId, { effective_to_column: v })} placeholder="valid_to" />
          </Field>
          <Field label="Current-Flag Column">
            <TextInput value={colCurrent} onChange={(v) => onChange(nodeId, { current_flag_column: v })} placeholder="is_current" />
          </Field>
          <Field label="Active Sentinel (valid_to for current rows)">
            <TextInput value={nullHi} onChange={(v) => onChange(nodeId, { null_high_water: v })} placeholder="9999-12-31" />
          </Field>
        </div>
      )}

      {(businessKey.length === 0 || tracked.length === 0) && (
        <div className="text-[11px] text-amber-600 mt-1">
          {businessKey.length === 0 && 'Pick at least one business key. '}
          {tracked.length === 0 && 'Pick at least one tracked column.'}
        </div>
      )}
    </>
  );
}

const SCHEMA_TYPES = ['string', 'int', 'bigint', 'float', 'double', 'decimal', 'bool', 'date', 'datetime', 'json'];

function SchemaMapperConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const mappings: any[] = Array.isArray(params.mappings) ? params.mappings : [];

  const update = (idx: number, patch: Record<string, any>) => {
    const next = mappings.map((m, i) => i === idx ? { ...m, ...patch } : m);
    onChange(nodeId, { mappings: next });
  };
  const addRow = () => onChange(nodeId, {
    mappings: [...mappings, { source: columns[0] || '', target: columns[0] || '', type: 'string', default: '' }],
  });
  const removeRow = (idx: number) => onChange(nodeId, { mappings: mappings.filter((_, i) => i !== idx) });
  const autofill = () => onChange(nodeId, {
    mappings: columns.map((c) => ({ source: c, target: c, type: 'string', default: '' })),
  });

  return (
    <>
      <Field label={`Field Mappings (${mappings.length})`}>
        <div className="flex flex-col gap-2">
          {mappings.map((m, idx) => (
            <div key={idx} className="grid grid-cols-[1fr_auto_1fr_1fr_1fr_auto] items-center gap-2">
              {columns.length > 0 ? (
                <Select
                  value={m.source || columns[0]}
                  onChange={(v) => update(idx, { source: v })}
                  options={['', ...columns]}
                />
              ) : (
                <input
                  value={m.source || ''}
                  onChange={(e) => update(idx, { source: e.target.value })}
                  placeholder="source"
                  className="w-full px-2 py-1.5 text-sm border border-slate-200 rounded"
                />
              )}
              <span className="text-sm text-slate-400">→</span>
              <input
                value={m.target || ''}
                onChange={(e) => update(idx, { target: e.target.value })}
                placeholder="target"
                className="w-full px-2 py-1.5 text-sm border border-slate-200 rounded"
              />
              <Select
                value={m.type || 'string'}
                onChange={(v) => update(idx, { type: v })}
                options={SCHEMA_TYPES}
              />
              <input
                value={m.default ?? ''}
                onChange={(e) => update(idx, { default: e.target.value })}
                placeholder="default"
                className="w-full px-2 py-1.5 text-sm border border-slate-200 rounded"
              />
              <button
                onClick={() => removeRow(idx)}
                className="text-sm text-red-500 hover:text-red-700 px-2"
                title="Remove mapping"
              >✕</button>
            </div>
          ))}
          <div className="flex flex-wrap items-center gap-2 mt-1">
            <button
              onClick={addRow}
              className="text-xs font-medium text-pipe-700 hover:text-pipe-900 hover:bg-pipe-50 border border-pipe-200 rounded-md px-3 py-1.5 transition-colors"
            >
              + Add mapping
            </button>
            {columns.length > 0 && (
              <button
                onClick={autofill}
                className="text-xs font-semibold text-white bg-pipe-600 hover:bg-pipe-700 rounded-md px-3 py-1.5 transition-colors inline-flex items-center gap-1.5"
                title={`Adds a row for every upstream column (${columns.length})`}
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
                Select all columns ({columns.length})
              </button>
            )}
            {mappings.length > 0 && (
              <button
                onClick={() => onChange(nodeId, { mappings: [] })}
                className="text-xs font-medium text-red-600 hover:text-red-800 hover:bg-red-50 border border-red-200 rounded-md px-3 py-1.5 transition-colors ml-auto"
                title="Remove every mapping (you can re-add individually or with Select all)"
              >
                Clear all
              </button>
            )}
          </div>
          {columns.length > 0 && (
            <p className="text-xs text-slate-500 mt-2 leading-relaxed">
              Tip: <strong>Select all columns</strong> brings every upstream column in.
              Then <strong>✕</strong> any row you don't need.
            </p>
          )}
        </div>
      </Field>
      <Field label="Keep unmapped columns">
        <Toggle
          value={!!params.keep_unmapped}
          onChange={(v) => onChange(nodeId, { keep_unmapped: v })}
          label="Pass through source columns not in mapping"
        />
      </Field>
    </>
  );
}

/* ═══════════════════════════════════════════════════════
   AI Primitive Configs (Sprint C)
   ═══════════════════════════════════════════════════════ */

const EMBED_PROVIDERS = ['hash', 'openai', 'cohere', 'sentence_transformers'];
const GUARDRAIL_CHECKS = ['pii', 'prompt_injection', 'profanity'];

function EmbedderConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  return (
    <>
      <Field label="Text Column *">
        {columns.length > 0 ? (
          <Select value={params.text_column || columns[0]} onChange={(v) => onChange(nodeId, { text_column: v })} options={columns} />
        ) : (
          <TextInput value={params.text_column || ''} onChange={(v) => onChange(nodeId, { text_column: v })} placeholder="content" />
        )}
      </Field>
      <Field label="Provider">
        <Select value={params.provider || 'hash'} onChange={(v) => onChange(nodeId, { provider: v })} options={EMBED_PROVIDERS} />
      </Field>
      {params.provider && params.provider !== 'hash' && (
        <Field label="Model">
          <TextInput value={params.model || ''} onChange={(v) => onChange(nodeId, { model: v })}
            placeholder={params.provider === 'openai' ? 'text-embedding-3-small' : 'all-MiniLM-L6-v2'} />
        </Field>
      )}
      <Field label="Output Column">
        <TextInput value={params.output_column || 'embedding'} onChange={(v) => onChange(nodeId, { output_column: v })} placeholder="embedding" />
      </Field>
      <Field label="Hash Dimension">
        <TextInput value={String(params.dim ?? 384)} onChange={(v) => onChange(nodeId, { dim: parseInt(v) || 384 })} placeholder="384" />
      </Field>
      <div className="text-xs text-slate-400 italic">
        Falls back to deterministic hash embeddings if the provider SDK or API key is missing — pipeline never breaks.
      </div>
    </>
  );
}

function LlmGuardrailConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const checks: string[] = Array.isArray(params.checks) ? params.checks : ['pii', 'prompt_injection'];
  const toggleCheck = (c: string) => {
    const next = checks.includes(c) ? checks.filter((x) => x !== c) : [...checks, c];
    onChange(nodeId, { checks: next });
  };
  const extras: string[] = Array.isArray(params.extra_patterns) ? params.extra_patterns : [];

  return (
    <>
      <Field label="Text Column *">
        {columns.length > 0 ? (
          <Select value={params.text_column || columns[0]} onChange={(v) => onChange(nodeId, { text_column: v })} options={columns} />
        ) : (
          <TextInput value={params.text_column || ''} onChange={(v) => onChange(nodeId, { text_column: v })} placeholder="content" />
        )}
      </Field>
      <Field label="Checks">
        <div className="flex flex-wrap gap-1">
          {GUARDRAIL_CHECKS.map((c) => (
            <button key={c} onClick={() => toggleCheck(c)}
              className={`text-xs px-2 py-1 rounded-lg border transition-colors ${
                checks.includes(c) ? 'bg-pipe-100 text-pipe-700 border-pipe-300' : 'bg-white text-slate-400 border-slate-200'
              }`}>
              {checks.includes(c) && '✓ '}{c}
            </button>
          ))}
        </div>
      </Field>
      <Field label="Mode">
        <Select value={params.mode || 'tag'} onChange={(v) => onChange(nodeId, { mode: v })} options={['tag', 'block', 'mask']} />
      </Field>
      <Field label="Extra Regex Patterns">
        <TextInput
          value={extras.join(', ')}
          onChange={(v) => onChange(nodeId, { extra_patterns: v.split(',').map((s) => s.trim()).filter(Boolean) })}
          placeholder="api[_-]?key, secret"
        />
      </Field>
      <div className="text-xs text-slate-400 italic">
        tag = add __guardrail_flags column; block = drop flagged rows; mask = replace PII with ***
      </div>
    </>
  );
}

function SemanticRouterConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const labels: any[] = Array.isArray(params.labels) ? params.labels : [];

  const update = (idx: number, patch: Record<string, any>) => {
    const next = labels.map((l, i) => i === idx ? { ...l, ...patch } : l);
    onChange(nodeId, { labels: next });
  };
  const addLabel = () => onChange(nodeId, { labels: [...labels, { name: '', examples: '' }] });
  const removeLabel = (idx: number) => onChange(nodeId, { labels: labels.filter((_, i) => i !== idx) });

  return (
    <>
      <Field label="Text Column *">
        {columns.length > 0 ? (
          <Select value={params.text_column || columns[0]} onChange={(v) => onChange(nodeId, { text_column: v })} options={columns} />
        ) : (
          <TextInput value={params.text_column || ''} onChange={(v) => onChange(nodeId, { text_column: v })} placeholder="content" />
        )}
      </Field>
      <Field label={`Labels (${labels.length})`}>
        <div className="flex flex-col gap-1.5">
          {labels.map((lbl, idx) => (
            <div key={idx} className="flex items-center gap-1">
              <input
                value={lbl.name || ''}
                onChange={(e) => update(idx, { name: e.target.value })}
                placeholder="label"
                className="w-20 px-2 py-1 text-xs border border-slate-200 rounded"
              />
              <input
                value={Array.isArray(lbl.examples) ? lbl.examples.join(' | ') : (lbl.examples || '')}
                onChange={(e) => update(idx, { examples: e.target.value.split('|').map((s) => s.trim()).filter(Boolean) })}
                placeholder="example1 | example2"
                className="flex-1 px-2 py-1 text-xs border border-slate-200 rounded"
              />
              <button onClick={() => removeLabel(idx)} className="text-xs text-red-500 hover:text-red-700 px-1">✕</button>
            </div>
          ))}
          <button onClick={addLabel} className="text-xs text-pipe-600 hover:text-pipe-800 self-start">
            + Add label
          </button>
        </div>
      </Field>
      <Field label="Provider">
        <Select value={params.provider || 'hash'} onChange={(v) => onChange(nodeId, { provider: v })} options={EMBED_PROVIDERS} />
      </Field>
      <Field label="Min Confidence">
        <TextInput value={String(params.threshold ?? 0)} onChange={(v) => onChange(nodeId, { threshold: parseFloat(v) || 0 })} placeholder="0.0" />
      </Field>
      <Field label="Default Label">
        <TextInput value={params.default_label || 'other'} onChange={(v) => onChange(nodeId, { default_label: v })} placeholder="other" />
      </Field>
    </>
  );
}

/* ═══════════════════════════════════════════════════════
   High-Impact Additions
   ═══════════════════════════════════════════════════════ */

function FlattenExplodeConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  return (
    <>
      <Field label="Mode">
        <Select
          value={params.mode || 'flatten'}
          onChange={(v) => onChange(nodeId, { mode: v })}
          options={['flatten', 'explode']}
        />
        <p className="text-xs text-slate-400 mt-1">
          {params.mode === 'explode'
            ? 'Unnest array column — each element becomes a separate row'
            : 'Expand struct/JSON fields into individual columns'}
        </p>
      </Field>
      <Field label="Column *">
        {columns.length > 0 ? (
          <Select value={params.column || ''} onChange={(v) => onChange(nodeId, { column: v })} options={['', ...columns]} />
        ) : (
          <TextInput value={params.column || ''} onChange={(v) => onChange(nodeId, { column: v })} placeholder="data" />
        )}
      </Field>
      <Field label="Column Prefix">
        <TextInput value={params.prefix || ''} onChange={(v) => onChange(nodeId, { prefix: v })} placeholder="user_" />
        <p className="text-xs text-slate-400 mt-0.5">Prefix for new column names (default: original column name)</p>
      </Field>
      <Field label="Keep Original Column">
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={params.keep_original || false}
            onChange={(e) => onChange(nodeId, { keep_original: e.target.checked })}
            className="rounded border-slate-300"
          />
          <span className="text-xs text-slate-600">Keep the nested column alongside expanded columns</span>
        </label>
      </Field>
      {/* 2026-06-11 (node-audit): explode-only behaviors the user must
          choose, not inherit silently — what happens to a row whose
          array is empty, and whether elements carry their position. */}
      {params.mode === 'explode' && (
        <Field label="Explode Options">
          <label className="flex items-center gap-2 cursor-pointer mb-1.5">
            <input
              type="checkbox"
              checked={params.keep_empty || false}
              onChange={(e) => onChange(nodeId, { keep_empty: e.target.checked })}
              className="rounded border-slate-300"
            />
            <span className="text-xs text-slate-600">
              Keep empty arrays — the row survives with a NULL element (otherwise it's dropped)
            </span>
          </label>
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={params.add_index || false}
              onChange={(e) => onChange(nodeId, { add_index: e.target.checked })}
              className="rounded border-slate-300"
            />
            <span className="text-xs text-slate-600">
              Add index column — each element's 1-based position as <span className="font-mono">{(params.prefix ? params.prefix + (params.column || 'items') : (params.column || 'items')) + '_index'}</span>
            </span>
          </label>
        </Field>
      )}
    </>
  );
}

function MaterializeConfig({ params, nodeId, onChange }: ConfigProps) {
  return (
    <>
      <div className="px-3 py-2.5 bg-violet-50 border border-violet-200 rounded-lg text-xs text-violet-700 space-y-1">
        <div className="font-semibold flex items-center gap-1.5">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><circle cx="12" cy="16" r="1" fill="currentColor"/></svg>
          Checkpoint Node
        </div>
        <p>Saves upstream results to a temp table. Downstream nodes read the cached snapshot instead of re-executing upstream.</p>
      </div>
      <Field label="Cache Table Name">
        <TextInput
          value={params.table_name || ''}
          onChange={(v) => onChange(nodeId, { table_name: v })}
          placeholder="orders_cleaned"
        />
        <p className="text-xs text-slate-400 mt-0.5">Leave blank for auto-generated name</p>
      </Field>
    </>
  );
}

function RetryHandlerConfig({ params, nodeId, onChange }: ConfigProps) {
  return (
    <>
      <div className="px-3 py-2.5 bg-red-50 border border-red-200 rounded-lg text-xs text-red-700 space-y-1">
        <div className="font-semibold flex items-center gap-1.5">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
          Error Handler
        </div>
        <p>Catches upstream failures and retries with configurable backoff. Place after any node that may fail.</p>
      </div>
      <Field label="Max Retries">
        <TextInput
          value={String(params.max_retries ?? 3)}
          onChange={(v) => onChange(nodeId, { max_retries: parseInt(v) || 3 })}
          placeholder="3"
        />
      </Field>
      <Field label="Initial Delay (seconds)">
        <TextInput
          value={String(params.delay_seconds ?? 2)}
          onChange={(v) => onChange(nodeId, { delay_seconds: parseInt(v) || 2 })}
          placeholder="2"
        />
      </Field>
      <Field label="Backoff Multiplier">
        <TextInput
          value={String(params.backoff_multiplier ?? 2.0)}
          onChange={(v) => onChange(nodeId, { backoff_multiplier: parseFloat(v) || 2.0 })}
          placeholder="2.0"
        />
        <p className="text-xs text-slate-400 mt-0.5">Delay doubles after each retry (2s → 4s → 8s)</p>
      </Field>
      <Field label="On All Retries Exhausted">
        <Select
          value={params.on_exhausted || 'fail'}
          onChange={(v) => onChange(nodeId, { on_exhausted: v })}
          options={['fail', 'skip']}
        />
        <p className="text-xs text-slate-400 mt-0.5">
          {params.on_exhausted === 'skip'
            ? 'Continue pipeline with empty result'
            : 'Stop pipeline and report the error'}
        </p>
      </Field>
    </>
  );
}

/* ═══════════════════════════════════════════════════════
   Action Configs
   ═══════════════════════════════════════════════════════ */

function HttpRequestConfig({ params, nodeId, onChange }: ConfigProps) {
  const headers = Array.isArray(params.headers) ? params.headers : [];

  const updateHeader = (idx: number, field: string, value: string) => {
    const updated = headers.map((h: any, i: number) => i === idx ? { ...h, [field]: value } : h);
    onChange(nodeId, { headers: updated });
  };

  const addHeader = () => onChange(nodeId, { headers: [...headers, { key: '', value: '' }] });
  const removeHeader = (idx: number) => onChange(nodeId, { headers: headers.filter((_: any, i: number) => i !== idx) });

  return (
    <>
      <Field label="Connection">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['rest_api', 'http', 'api']}
        />
      </Field>
      <Field label="Method *">
        <Select value={params.method || 'GET'} onChange={(v) => onChange(nodeId, { method: v })} options={['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']} />
      </Field>
      <Field label="URL *">
        <TextInput value={params.url || ''} onChange={(v) => onChange(nodeId, { url: v })} placeholder="https://api.example.com/endpoint" />
      </Field>
      <Field label="Headers">
        <div className="space-y-1.5">
          {headers.map((h: any, i: number) => (
            <div key={i} className="flex items-center gap-1.5">
              <TextInput value={h.key} onChange={(v) => updateHeader(i, 'key', v)} placeholder="Header" />
              <TextInput value={h.value} onChange={(v) => updateHeader(i, 'value', v)} placeholder="Value" />
              <button onClick={() => removeHeader(i)} className="text-red-400 hover:text-red-600 shrink-0">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
            </div>
          ))}
          <button onClick={addHeader} className="text-xs text-pipe-600 hover:text-pipe-800 font-medium">+ Add Header</button>
        </div>
      </Field>
      <Field label="Body">
        <TextArea
          value={params.body || ''}
          onChange={(v) => onChange(nodeId, { body: v })}
          placeholder='{"key":"value"}'
          rows={5}
        />
      </Field>
      <Field label="Content Type">
        <Select value={params.content_type || 'application/json'} onChange={(v) => onChange(nodeId, { content_type: v })}
          options={['application/json', 'application/x-www-form-urlencoded', 'multipart/form-data', 'text/plain', 'application/xml']} />
      </Field>
      {/* P2-A residual (2026-05-19): migrate http_request to <AuthSection>
          with a key-translation wrapper. http_request's backend executor
          reads auth_token / auth_user / auth_pass (non-canonical names),
          so we adapt:
            - View layer: derive a synthetic-canonical params object
              ({bearer_token, username, password}) from the legacy keys
              for AuthSection to read.
            - Write layer: convert AuthSection's canonical writes back to
              the http_request-specific legacy keys before storing. */}
      {(() => {
        const adaptedParams = {
          ...params,
          // Pull legacy values into canonical names AuthSection expects
          bearer_token: params.bearer_token ?? params.auth_token,
          username: params.username ?? params.auth_user,
          password: params.password ?? params.auth_pass,
        };
        const HTTP_AUTH_KEY_MAP: Record<string, string> = {
          bearer_token: 'auth_token',
          username: 'auth_user',
          password: 'auth_pass',
          // api_key_header / api_key_value pass through unchanged
        };
        const adaptedOnChange = (patch: Record<string, any>) => {
          const out: Record<string, any> = {};
          for (const [k, v] of Object.entries(patch)) {
            const legacy = HTTP_AUTH_KEY_MAP[k];
            if (legacy) {
              out[legacy] = v;
              // Also clear the canonical name so we don't accumulate both
              out[k] = undefined;
            } else {
              out[k] = v;
            }
          }
          onChange(nodeId, out);
        };
        return <AuthSection params={adaptedParams} onChange={adaptedOnChange} />;
      })()}
      <Field label="Timeout (seconds)">
        <TextInput value={String(params.timeout ?? 30)} onChange={(v) => onChange(nodeId, { timeout: parseInt(v) || 30 })} placeholder="30" />
      </Field>
      {/* P0-4: per-node "Retry on Failure" + Max Retries removed —
          use the Universal Settings tab's retry block, or add a
          retry_handler node for an explicit override. */}
    </>
  );
}

function WebhookTriggerConfig({ params, nodeId, onChange }: ConfigProps) {
  return (
    <>
      <Field label="Webhook Path *">
        <TextInput value={params.path || ''} onChange={(v) => onChange(nodeId, { path: v })} placeholder="/webhook/my-trigger" />
      </Field>
      <Field label="HTTP Method">
        <Select value={params.method || 'POST'} onChange={(v) => onChange(nodeId, { method: v })} options={['GET', 'POST', 'PUT', 'PATCH', 'DELETE']} />
      </Field>
      <Field label="Authentication">
        {/* P0-2 (2026-05-18): 'basic_auth' was selectable but rendered no
            Username/Password fields. Removed until the basic-auth UI ships
            (rare use case for webhook receivers; header-auth covers most
            shared-secret patterns). */}
        <Select value={params.auth || 'none'} onChange={(v) => onChange(nodeId, { auth: v })} options={['none', 'header_auth']} />
      </Field>
      {params.auth === 'header_auth' && (
        <>
          <Field label="Header Name">
            <TextInput value={params.auth_header || 'X-Webhook-Secret'} onChange={(v) => onChange(nodeId, { auth_header: v })} placeholder="X-Webhook-Secret" />
          </Field>
          <Field label="Header Value">
            <SecretInput value={params.auth_value || ''} onChange={(v) => onChange(nodeId, { auth_value: v })} placeholder="secret-value" />
          </Field>
        </>
      )}
      <Field label="Response Code">
        <Select value={String(params.response_code ?? 200)} onChange={(v) => onChange(nodeId, { response_code: parseInt(v) })}
          options={['200', '201', '202', '204']} />
      </Field>
      <Field label="Response Body (optional)">
        <TextArea value={params.response_body || ''} onChange={(v) => onChange(nodeId, { response_body: v })}
          placeholder='{"status":"received"}' rows={3} />
      </Field>
    </>
  );
}

function CodeScriptConfig({ params, nodeId, onChange }: ConfigProps) {
  return (
    <>
      <Field label="Language *">
        <Select value={params.language || 'python'} onChange={(v) => onChange(nodeId, { language: v })} options={['python', 'javascript', 'sql']} />
      </Field>
      <Field label="Code *">
        <TextArea
          value={params.code || ''}
          onChange={(v) => onChange(nodeId, { code: v })}
          placeholder={params.language === 'javascript'
            ? '// Access input data via $input\nconst items = $input.all();\nreturn items.map(item => ({\n  ...item,\n  processed: true\n}));'
            : '# Access input data via df (pandas DataFrame)\nimport pandas as pd\n\n# Transform data\ndf["new_col"] = df["col1"] * 2\nreturn df'}
          rows={12}
        />
      </Field>
      <Field label="Timeout (seconds)">
        <TextInput value={String(params.timeout ?? 60)} onChange={(v) => onChange(nodeId, { timeout: parseInt(v) || 60 })} placeholder="60" />
      </Field>
      <div className="bg-slate-50 rounded-lg px-3 py-2 border border-slate-100">
        <div className="text-xs text-slate-500 space-y-1">
          <div><strong>Python:</strong> Input as <code className="bg-white px-1 rounded border border-slate-200 text-pipe-600">df</code> (DataFrame), return a DataFrame</div>
          <div><strong>JavaScript:</strong> Input via <code className="bg-white px-1 rounded border border-slate-200 text-pipe-600">$input.all()</code>, return array of objects</div>
        </div>
      </div>
    </>
  );
}

/* ── Copy Data activity ─────────────────────────────────────────────
   Source-to-destination copy with column mapping:
     Source / Sink / Mapping / Execution tabs
   "Execution" was previously labelled "Settings" but collided visually
   with the outer top-level "Settings" tab on the same panel — renamed
   2026-05-09 to disambiguate. Same pattern applied to Delete Data below.
   Backend: fpulse/nodes/flow_control.py CopyDataNode
*/
const COPY_TABS = ['Source', 'Sink', 'Mapping', 'Execution'] as const;
type CopyTab = typeof COPY_TABS[number];

function CopyDataConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const [tab, setTab] = useState<CopyTab>('Source');
  const set = (patch: Record<string, any>) => onChange(nodeId, patch);

  // Resolve the picked source connection's type so the field set adapts to
  // the system being read from (SQL vs NoSQL vs storage vs streaming, etc.).
  const { type: sourceConnType } = useConnectionType(params.source_connection_id);
  const sourceFamily = getConnectionFamily(sourceConnType);
  // Same for the sink — write options are very different across families.
  const { type: sinkConnType } = useConnectionType(params.sink_connection_id);
  const sinkFamily = getConnectionFamily(sinkConnType);

  const mappings: any[] = Array.isArray(params.mappings) ? params.mappings : [];
  const updateMapping = (idx: number, patch: Record<string, any>) => {
    set({ mappings: mappings.map((m, i) => i === idx ? { ...m, ...patch } : m) });
  };
  const addMapping = () => set({
    mappings: [...mappings, { source: columns[0] || '', target: columns[0] || '', type: 'VARCHAR' }],
  });
  const removeMapping = (idx: number) => set({ mappings: mappings.filter((_, i) => i !== idx) });
  const autofillMappings = () => set({
    mappings: columns.map((c) => ({ source: c, target: c, type: 'VARCHAR' })),
  });

  const keyColumns: string[] = Array.isArray(params.key_columns) ? params.key_columns : [];
  const toggleKey = (col: string) => {
    const next = keyColumns.includes(col) ? keyColumns.filter((k) => k !== col) : [...keyColumns, col];
    set({ key_columns: next });
  };

  // Wizard guidance — Copy Data mixes source, sink, mapping and write behaviour,
  // so each tab gets a one-line "what this step is for", and tabs that still
  // need required input get an amber dot so the path through is obvious.
  const COPY_TAB_HELP: Record<string, string> = {
    Source: 'Where to read from. Leave the connection empty to copy from the upstream node instead.',
    Sink: 'Where to write, and how (table action + append / overwrite / upsert / merge). Connection is required.',
    Mapping: 'Optional — map source columns to target columns. Auto-fill from the upstream schema, then tweak.',
    Execution: 'Optional — batch size, parallelism and on-error behaviour for the copy.',
  };
  const tabNeedsSetup = (t: CopyTab): boolean => {
    if (t === 'Sink') return !params.sink_connection_id;  // the one hard requirement
    return false;
  };

  return (
    <>
      {/* Tab strip — multi-pane config */}
      <div className="flex border-b border-slate-200 -mx-4 px-4 mb-2">
        {COPY_TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`text-xs font-semibold uppercase tracking-wider px-3 py-2 border-b-2 transition-colors inline-flex items-center gap-1 ${
              tab === t
                ? 'border-pipe-500 text-pipe-700'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}
          >
            {t}
            {tabNeedsSetup(t) && (
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400" title="Needs setup" />
            )}
          </button>
        ))}
      </div>
      {/* Per-step guidance line — turns the dense multi-pane form into a
          read-as-you-go wizard. */}
      <p className="text-[11px] text-slate-500 mb-2 -mt-0.5">{COPY_TAB_HELP[tab]}</p>

      {/* ── Source tab ── */}
      {tab === 'Source' && (
        <>
          <Field label="Source Connection">
            <ConnectionPicker
              value={params.source_connection_id || ''}
              onChange={(v) => set({ source_connection_id: v })}
              requireCapability="read"
            />
          </Field>
          <div className="text-xs text-slate-400 italic -mt-1 mb-1">
            Leave empty to copy from the upstream node instead of a database.
          </div>

          <FamilyAwareSourceFields family={sourceFamily} params={params} set={set} prefix="source_" />
        </>
      )}


      {/* ── Sink tab ── */}
      {tab === 'Sink' && (
        <>
          <Field label="Sink Connection *">
            <ConnectionPicker
              value={params.sink_connection_id || ''}
              onChange={(v) => set({ sink_connection_id: v })}
              requireCapability="write"
            />
          </Field>

          {/* SQL & Warehouse — table + table action + write behavior + keys */}
          {(sinkFamily === 'sql' || sinkFamily === 'warehouse') && (
            <>
              <div className="grid grid-cols-2 gap-2">
                <Field label="Schema">
                  <TextInput
                    value={params.sink_schema || ''}
                    onChange={(v) => set({ sink_schema: v })}
                    placeholder="dbo"
                  />
                </Field>
                <Field label="Table *">
                  <TextInput
                    value={params.sink_table || ''}
                    onChange={(v) => set({ sink_table: v })}
                    placeholder="orders_loaded"
                  />
                </Field>
              </div>
              <Field label="Table Action">
                <Select
                  value={params.table_action || 'none'}
                  onChange={(v) => set({ table_action: v })}
                  options={['none', 'autocreate', 'recreate', 'truncate']}
                  labels={{ none: 'None (table must exist)', autocreate: 'Create if missing', recreate: 'Drop & recreate', truncate: 'Truncate before load' }}
                />
              </Field>
              <Field label="Write Behavior">
                <Select
                  value={params.write_behavior || 'append'}
                  onChange={(v) => set({ write_behavior: v })}
                  options={['append', 'overwrite', 'upsert', 'merge']}
                />
              </Field>
              {(params.write_behavior === 'upsert' || params.write_behavior === 'merge') && (
                <Field label="Key Columns *">
                  {columns.length > 0 ? (
                    <div className="flex flex-wrap gap-1">
                      {columns.map((c) => (
                        <button
                          key={c}
                          onClick={() => toggleKey(c)}
                          className={`text-xs px-2 py-1 rounded-lg border transition-colors ${
                            keyColumns.includes(c)
                              ? 'bg-pipe-100 text-pipe-700 border-pipe-300'
                              : 'bg-white text-slate-400 border-slate-200'
                          }`}
                        >
                          {keyColumns.includes(c) && '✓ '}{c}
                        </button>
                      ))}
                    </div>
                  ) : (
                    <TextInput
                      value={keyColumns.join(', ')}
                      onChange={(v) => set({ key_columns: v.split(',').map((s) => s.trim()).filter(Boolean) })}
                      placeholder="id, tenant_id"
                    />
                  )}
                </Field>
              )}
              {sinkFamily === 'warehouse' && (
                <Field label="Bulk Load">
                  <Select
                    value={params.bulk_load || 'auto'}
                    onChange={(v) => set({ bulk_load: v })}
                    options={['auto', 'on', 'off']}
                  />
                </Field>
              )}
              <Field label="Pre-Copy Script">
                <textarea
                  value={params.pre_copy_script || ''}
                  onChange={(e) => set({ pre_copy_script: e.target.value })}
                  placeholder="TRUNCATE TABLE staging.target"
                  rows={3}
                  className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
                />
              </Field>
              <Field label="Post-Copy Script">
                <textarea
                  value={params.post_copy_script || ''}
                  onChange={(e) => set({ post_copy_script: e.target.value })}
                  placeholder="UPDATE STATISTICS dbo.orders"
                  rows={3}
                  className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
                />
              </Field>
              <Field label="Batch Size">
                <TextInput
                  value={String(params.batch_size ?? 1000)}
                  onChange={(v) => set({ batch_size: parseInt(v) || 1000 })}
                  placeholder="1000"
                />
              </Field>
            </>
          )}

          {/* NoSQL — collection + write mode + key field */}
          {sinkFamily === 'nosql' && (
            <>
              <div className="grid grid-cols-2 gap-2">
                <Field label="Database">
                  <TextInput
                    value={params.sink_database || ''}
                    onChange={(v) => set({ sink_database: v })}
                    placeholder="appdb"
                  />
                </Field>
                <Field label="Collection / Table *">
                  <TextInput
                    value={params.sink_collection || ''}
                    onChange={(v) => set({ sink_collection: v })}
                    placeholder="orders"
                  />
                </Field>
              </div>
              <Field label="Write Mode">
                <Select
                  value={params.write_behavior || 'insert'}
                  onChange={(v) => set({ write_behavior: v })}
                  options={['insert', 'upsert', 'replace']}
                />
              </Field>
              {(params.write_behavior === 'upsert' || params.write_behavior === 'replace') && (
                <Field label="Key Field *">
                  <TextInput
                    value={params.key_field || ''}
                    onChange={(v) => set({ key_field: v })}
                    placeholder="_id"
                  />
                </Field>
              )}
              <Field label="Ordered Writes">
                <Select
                  value={params.ordered ? 'true' : 'false'}
                  onChange={(v) => set({ ordered: v === 'true' })}
                  options={['true', 'false']}
                  labels={{ true: 'Yes (stop on first error)', false: 'No (continue on errors)' }}
                />
              </Field>
              <Field label="Batch Size">
                <TextInput
                  value={String(params.batch_size ?? 500)}
                  onChange={(v) => set({ batch_size: parseInt(v) || 500 })}
                  placeholder="500"
                />
              </Field>
            </>
          )}

          {/* Search — index + doc id field + bulk size */}
          {sinkFamily === 'search' && (
            <>
              <Field label="Index *">
                <TextInput
                  value={params.sink_index || ''}
                  onChange={(v) => set({ sink_index: v })}
                  placeholder="orders-2025"
                />
              </Field>
              <Field label="Document ID Field">
                <TextInput
                  value={params.id_field || ''}
                  onChange={(v) => set({ id_field: v })}
                  placeholder="order_id"
                />
              </Field>
              <Field label="Write Mode">
                <Select
                  value={params.write_behavior || 'index'}
                  onChange={(v) => set({ write_behavior: v })}
                  options={['index', 'create', 'update', 'upsert']}
                />
              </Field>
              <Field label="Refresh Policy">
                <Select
                  value={params.refresh_policy || 'false'}
                  onChange={(v) => set({ refresh_policy: v })}
                  options={['false', 'wait_for', 'true']}
                  labels={{ false: 'No refresh', wait_for: 'Wait for next refresh', true: 'Refresh immediately' }}
                />
              </Field>
              <Field label="Bulk Batch Size">
                <TextInput
                  value={String(params.batch_size ?? 1000)}
                  onChange={(v) => set({ batch_size: parseInt(v) || 1000 })}
                  placeholder="1000"
                />
              </Field>
            </>
          )}

          {/* Storage — path + format + compression + partitioning */}
          {sinkFamily === 'storage' && (
            <>
              <Field label="Output Path / Prefix *">
                <TextInput
                  value={params.sink_path || ''}
                  onChange={(v) => set({ sink_path: v })}
                  placeholder="bucket/loaded/orders/"
                />
              </Field>
              <div className="grid grid-cols-2 gap-2">
                <Field label="File Format">
                  <Select
                    value={params.file_format || 'parquet'}
                    onChange={(v) => set({ file_format: v })}
                    options={['parquet', 'csv', 'json', 'jsonl', 'avro', 'orc', 'xlsx']}
                  />
                </Field>
                <Field label="Compression">
                  <Select
                    value={params.compression || 'auto'}
                    onChange={(v) => set({ compression: v })}
                    options={['auto', 'none', 'gzip', 'snappy', 'zstd', 'bzip2']}
                  />
                </Field>
              </div>
              <Field label="Write Mode">
                <Select
                  value={params.write_behavior || 'append'}
                  onChange={(v) => set({ write_behavior: v })}
                  options={['append', 'overwrite', 'error_if_exists']}
                />
              </Field>
              <Field label="Partition By (comma-separated)">
                <TextInput
                  value={params.partition_by || ''}
                  onChange={(v) => set({ partition_by: v })}
                  placeholder="year, month"
                />
              </Field>
              <div className="grid grid-cols-2 gap-2">
                <Field label="Max File Size (MB)">
                  <TextInput
                    value={String(params.max_file_size_mb || '')}
                    onChange={(v) => set({ max_file_size_mb: parseInt(v) || '' })}
                    placeholder="128"
                  />
                </Field>
                <Field label="Filename Pattern">
                  <TextInput
                    value={params.filename_pattern || ''}
                    onChange={(v) => set({ filename_pattern: v })}
                    placeholder="part-{i}.parquet"
                  />
                </Field>
              </div>
              {(params.file_format === 'csv' || !params.file_format) && (
                <div className="grid grid-cols-2 gap-2">
                  <Field label="Delimiter">
                    <TextInput
                      value={params.csv_delimiter || ''}
                      onChange={(v) => set({ csv_delimiter: v })}
                      placeholder=","
                    />
                  </Field>
                  <Field label="Header">
                    <Select
                      value={params.csv_header ? 'true' : 'false'}
                      onChange={(v) => set({ csv_header: v === 'true' })}
                      options={['true', 'false']}
                      labels={{ true: 'Yes', false: 'No' }}
                    />
                  </Field>
                </div>
              )}
            </>
          )}

          {/* Streaming — topic + key field + serialization */}
          {sinkFamily === 'streaming' && (
            <>
              <Field label="Topic / Stream *">
                <TextInput
                  value={params.sink_topic || ''}
                  onChange={(v) => set({ sink_topic: v })}
                  placeholder="orders.events"
                />
              </Field>
              <div className="grid grid-cols-2 gap-2">
                <Field label="Key Field">
                  <TextInput
                    value={params.key_field || ''}
                    onChange={(v) => set({ key_field: v })}
                    placeholder="order_id"
                  />
                </Field>
                <Field label="Value Format">
                  <Select
                    value={params.value_format || 'json'}
                    onChange={(v) => set({ value_format: v })}
                    options={['json', 'avro', 'protobuf', 'string']}
                  />
                </Field>
              </div>
              <Field label="Acks">
                <Select
                  value={params.acks || 'all'}
                  onChange={(v) => set({ acks: v })}
                  options={['0', '1', 'all']}
                  labels={{ '0': '0 — fire and forget', '1': '1 — leader only', all: 'all — full ISR' }}
                />
              </Field>
              <div className="grid grid-cols-2 gap-2">
                <Field label="Compression">
                  <Select
                    value={params.compression || 'none'}
                    onChange={(v) => set({ compression: v })}
                    options={['none', 'gzip', 'snappy', 'lz4', 'zstd']}
                  />
                </Field>
                <Field label="Batch Size">
                  <TextInput
                    value={String(params.batch_size ?? 500)}
                    onChange={(v) => set({ batch_size: parseInt(v) || 500 })}
                    placeholder="500"
                  />
                </Field>
              </div>
            </>
          )}

          {/* API — endpoint + method + body template */}
          {sinkFamily === 'api' && (
            <>
              <Field label="Endpoint Path *">
                <TextInput
                  value={params.endpoint_path || ''}
                  onChange={(v) => set({ endpoint_path: v })}
                  placeholder="/v1/orders"
                />
              </Field>
              <div className="grid grid-cols-2 gap-2">
                <Field label="Method">
                  <Select
                    value={params.method || 'POST'}
                    onChange={(v) => set({ method: v })}
                    options={['POST', 'PUT', 'PATCH']}
                  />
                </Field>
                <Field label="Send Mode">
                  <Select
                    value={params.send_mode || 'one_per_record'}
                    onChange={(v) => set({ send_mode: v })}
                    options={['one_per_record', 'batched']}
                    labels={{ one_per_record: 'One request per record', batched: 'Batched requests' }}
                  />
                </Field>
              </div>
              <Field label="Headers (JSON)">
                <textarea
                  value={params.headers_json || ''}
                  onChange={(e) => set({ headers_json: e.target.value })}
                  placeholder='{ "Content-Type": "application/json" }'
                  rows={3}
                  className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
                />
              </Field>
              <Field label="Body Template (Jinja-style)">
                <textarea
                  value={params.body_template || ''}
                  onChange={(e) => set({ body_template: e.target.value })}
                  placeholder='{ "id": "{{ row.id }}", "amount": {{ row.amount }} }'
                  rows={4}
                  className="w-full px-2.5 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300"
                />
              </Field>
              {params.send_mode === 'batched' && (
                <Field label="Batch Size">
                  <TextInput
                    value={String(params.batch_size ?? 100)}
                    onChange={(v) => set({ batch_size: parseInt(v) || 100 })}
                    placeholder="100"
                  />
                </Field>
              )}
            </>
          )}

          {/* SaaS — entity + write mode + external id (for upsert) */}
          {sinkFamily === 'saas' && (
            <>
              <Field label="Object / Entity *">
                <TextInput
                  value={params.sink_entity || ''}
                  onChange={(v) => set({ sink_entity: v })}
                  placeholder="Account"
                />
              </Field>
              <Field label="Write Mode">
                <Select
                  value={params.write_behavior || 'insert'}
                  onChange={(v) => set({ write_behavior: v })}
                  options={['insert', 'update', 'upsert', 'delete']}
                />
              </Field>
              {(params.write_behavior === 'upsert' || params.write_behavior === 'update' || params.write_behavior === 'delete') && (
                <Field label="External ID Field *">
                  <TextInput
                    value={params.external_id_field || ''}
                    onChange={(v) => set({ external_id_field: v })}
                    placeholder="External_Id__c"
                  />
                </Field>
              )}
              {/* P1-8: see Sink-tab removal note above. The Mapping tab
                  is now the single source of truth for mapping_mode. */}
              <Field label="Batch Size">
                <TextInput
                  value={String(params.batch_size ?? 200)}
                  onChange={(v) => set({ batch_size: parseInt(v) || 200 })}
                  placeholder="200"
                />
              </Field>
            </>
          )}

          {/* Vector — index + vector field + metadata fields */}
          {sinkFamily === 'vector' && (
            <>
              <Field label="Index / Collection *">
                <TextInput
                  value={params.sink_index || ''}
                  onChange={(v) => set({ sink_index: v })}
                  placeholder="prod-embeddings"
                />
              </Field>
              <div className="grid grid-cols-2 gap-2">
                <Field label="ID Field *">
                  <TextInput
                    value={params.id_field || ''}
                    onChange={(v) => set({ id_field: v })}
                    placeholder="doc_id"
                  />
                </Field>
                <Field label="Vector Field *">
                  <TextInput
                    value={params.vector_field || ''}
                    onChange={(v) => set({ vector_field: v })}
                    placeholder="embedding"
                  />
                </Field>
              </div>
              <Field label="Metadata Fields (comma-separated)">
                <TextInput
                  value={params.metadata_fields || ''}
                  onChange={(v) => set({ metadata_fields: v })}
                  placeholder="title, source, tenant"
                />
              </Field>
              <Field label="Write Mode">
                <Select
                  value={params.write_behavior || 'upsert'}
                  onChange={(v) => set({ write_behavior: v })}
                  options={['insert', 'upsert', 'replace']}
                />
              </Field>
              <div className="grid grid-cols-2 gap-2">
                <Field label="Namespace">
                  <TextInput
                    value={params.namespace || ''}
                    onChange={(v) => set({ namespace: v })}
                    placeholder="(optional)"
                  />
                </Field>
                <Field label="Batch Size">
                  <TextInput
                    value={String(params.batch_size ?? 100)}
                    onChange={(v) => set({ batch_size: parseInt(v) || 100 })}
                    placeholder="100"
                  />
                </Field>
              </div>
            </>
          )}

          {/* Other / unknown / no connection picked — fall back to the
              generic fields so the node is still usable. */}
          {sinkFamily === 'other' && (
            <>
              <Field label="Sink Table *">
                <TextInput
                  value={params.sink_table || ''}
                  onChange={(v) => set({ sink_table: v })}
                  placeholder="schema.target_table"
                />
              </Field>
              <Field label="Table Action">
                <Select
                  value={params.table_action || 'none'}
                  onChange={(v) => set({ table_action: v })}
                  options={['none', 'autocreate', 'recreate', 'truncate']}
                />
              </Field>
              <Field label="Write Behavior">
                <Select
                  value={params.write_behavior || 'append'}
                  onChange={(v) => set({ write_behavior: v })}
                  options={['append', 'overwrite', 'upsert', 'merge']}
                />
              </Field>
              <Field label="Batch Size">
                <TextInput
                  value={String(params.batch_size ?? 1000)}
                  onChange={(v) => set({ batch_size: parseInt(v) || 1000 })}
                  placeholder="1000"
                />
              </Field>
            </>
          )}
        </>
      )}

      {/* ── Mapping tab ── */}
      {tab === 'Mapping' && (
        <>
          <Field label="Schema Mapping">
            <Select
              value={params.mapping_mode || 'auto'}
              onChange={(v) => set({ mapping_mode: v })}
              options={['auto', 'explicit']}
            />
          </Field>
          {params.mapping_mode === 'explicit' && (
            <Field label={`Field Mappings (${mappings.length})`}>
              <div className="flex flex-col gap-1.5">
                {mappings.map((m, idx) => (
                  <div key={idx} className="flex items-center gap-1">
                    {columns.length > 0 ? (
                      <Select
                        value={m.source || columns[0]}
                        onChange={(v) => updateMapping(idx, { source: v })}
                        options={['', ...columns]}
                      />
                    ) : (
                      <input
                        value={m.source || ''}
                        onChange={(e) => updateMapping(idx, { source: e.target.value })}
                        placeholder="source"
                        className="w-20 px-2 py-1 text-xs border border-slate-200 rounded"
                      />
                    )}
                    <span className="text-xs text-slate-300">→</span>
                    <input
                      value={m.target || ''}
                      onChange={(e) => updateMapping(idx, { target: e.target.value })}
                      placeholder="target"
                      className="w-20 px-2 py-1 text-xs border border-slate-200 rounded"
                    />
                    <Select
                      value={m.type || 'VARCHAR'}
                      onChange={(v) => updateMapping(idx, { type: v })}
                      options={['VARCHAR', 'BIGINT', 'DOUBLE', 'BOOLEAN', 'DATE', 'TIMESTAMP', 'JSON']}
                    />
                    <button
                      onClick={() => removeMapping(idx)}
                      className="text-xs text-red-500 hover:text-red-700 px-1"
                    >✕</button>
                  </div>
                ))}
                <div className="flex gap-2">
                  <button onClick={addMapping} className="text-xs text-pipe-600 hover:text-pipe-800">
                    + Add mapping
                  </button>
                  {columns.length > 0 && (
                    <button onClick={autofillMappings} className="text-xs text-pipe-600 hover:text-pipe-800">
                      Auto-fill from upstream
                    </button>
                  )}
                </div>
              </div>
            </Field>
          )}
          {(params.mapping_mode || 'auto') === 'auto' && (
            <div className="text-xs text-slate-400 italic">
              Auto: source columns flow to sink columns by exact name match.
            </div>
          )}
        </>
      )}

      {/* ── Execution tab ── */}
      {tab === 'Execution' && (
        <>
          <Field label="Parallel Copies">
            <TextInput
              value={String(params.parallel_copies ?? 1)}
              onChange={(v) => set({ parallel_copies: parseInt(v) || 1 })}
              placeholder="1"
            />
          </Field>
          <Field label="Fault Tolerance">
            <Toggle
              value={!!params.skip_on_error}
              onChange={(v) => set({ skip_on_error: v })}
              label="Skip incompatible rows on error"
            />
          </Field>
          <Field label="Max Rows (0 = unlimited)">
            <TextInput
              value={String(params.max_rows ?? 0)}
              onChange={(v) => set({ max_rows: parseInt(v) || 0 })}
              placeholder="0"
            />
          </Field>
          <Field label="Log Path (skipped rows)">
            <TextInput
              value={params.log_path || ''}
              onChange={(v) => set({ log_path: v })}
              placeholder="logs/copy_errors.csv"
            />
          </Field>
          <Field label="Enable Staging">
            <Toggle
              value={!!params.enable_staging}
              onChange={(v) => set({ enable_staging: v })}
              label="Use interim staging copy"
            />
          </Field>
        </>
      )}
    </>
  );
}

/* ── Delete Activity (Source / Execution / Logging tabs) ── */
const DELETE_TABS = ['Source', 'Execution', 'Logging'] as const;
type DeleteTab = typeof DELETE_TABS[number];

function DeleteDataConfig({ params, nodeId, onChange }: ConfigProps) {
  const [tab, setTab] = useState<DeleteTab>('Source');
  const set = (patch: Record<string, any>) => onChange(nodeId, patch);
  const kind = params.target_kind || 'rows';
  return (
    <>
      <div className="flex border-b border-slate-200 -mx-4 px-4 mb-2">
        {DELETE_TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`text-xs font-semibold uppercase tracking-wider px-3 py-2 border-b-2 transition-colors ${
              tab === t
                ? 'border-pipe-500 text-pipe-700'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === 'Source' && (
        <>
          <Field label="Delete Target *">
            <Select
              value={kind}
              onChange={(v) => set({ target_kind: v })}
              options={['rows', 'files']}
            />
          </Field>
          {kind === 'rows' ? (
            <Field label="Delete Condition *">
              <TextInput
                value={params.condition || ''}
                onChange={(v) => set({ condition: v })}
                placeholder="status = 'expired' AND created_at < '2024-01-01'"
              />
            </Field>
          ) : (
            <>
              <Field label="File / Folder Path *">
                <TextInput
                  value={params.target_path || ''}
                  onChange={(v) => set({ target_path: v })}
                  placeholder="/data/incoming/"
                />
              </Field>
              <Field label="Wildcard Filter">
                <TextInput
                  value={params.wildcard || ''}
                  onChange={(v) => set({ wildcard: v })}
                  placeholder="*.csv"
                />
              </Field>
              <Field label="Recursive">
                <Toggle
                  value={!!params.recursive}
                  onChange={(v) => set({ recursive: v })}
                  label="Recurse into subfolders"
                />
              </Field>
            </>
          )}
        </>
      )}

      {tab === 'Execution' && (
        <Field label="Max Concurrent Connections">
          <TextInput
            value={String(params.max_concurrent ?? 1)}
            onChange={(v) => set({ max_concurrent: parseInt(v) || 1 })}
            placeholder="1"
          />
        </Field>
      )}

      {tab === 'Logging' && (
        <>
          <Field label="Enable Logging">
            <Toggle
              value={!!params.enable_logging}
              onChange={(v) => set({ enable_logging: v })}
              label="Write CSV record of every deletion"
            />
          </Field>
          {params.enable_logging && (
            <Field label="Log Folder">
              <TextInput
                value={params.log_path || ''}
                onChange={(v) => set({ log_path: v })}
                placeholder="logs/delete/"
              />
            </Field>
          )}
        </>
      )}

      <div className="bg-red-50 rounded-lg px-3 py-2 border border-red-100">
        <div className="text-[9px] text-red-500 font-medium">
          This action permanently deletes data. Use with caution.
        </div>
      </div>
    </>
  );
}

/* get_metadata renders via DynamicConfig (backend param_schema is [] — it
   always profiles the upstream). The old hand-built Dataset/Field-list form
   was removed 2026-06-16: it exposed fields the executor ignored. */

function SendEmailConfig({ params, nodeId, onChange }: ConfigProps) {
  return (
    <>
      <Field label="Connection">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
          filter={['smtp', 'email', 'sendgrid', 'ses', 'mailgun']}
          requireCapability="write"
        />
      </Field>
      {!params.connection_id && (
        <>
          <Field label="SMTP Host">
            <TextInput value={params.smtp_host || ''} onChange={(v) => onChange(nodeId, { smtp_host: v })} placeholder="smtp.gmail.com" />
          </Field>
          <Field label="SMTP Port">
            <TextInput value={String(params.smtp_port ?? 587)} onChange={(v) => onChange(nodeId, { smtp_port: parseInt(v) || 587 })} placeholder="587" />
          </Field>
          <Field label="Security">
            <Select value={params.security || 'tls'} onChange={(v) => onChange(nodeId, { security: v })}
              options={['tls', 'ssl', 'none']} />
          </Field>
          <Field label="Username">
            <TextInput value={params.smtp_user || ''} onChange={(v) => onChange(nodeId, { smtp_user: v })} placeholder="user@gmail.com" />
          </Field>
          <Field label="Password">
            <SecretInput value={params.smtp_pass || ''} onChange={(v) => onChange(nodeId, { smtp_pass: v })} placeholder="app-password" />
          </Field>
        </>
      )}
      <Field label="To *">
        <TextInput value={params.to || ''} onChange={(v) => onChange(nodeId, { to: v })} placeholder="user@example.com, team@example.com" />
      </Field>
      <Field label="CC">
        <TextInput value={params.cc || ''} onChange={(v) => onChange(nodeId, { cc: v })} placeholder="manager@example.com" />
      </Field>
      <Field label="BCC">
        <TextInput value={params.bcc || ''} onChange={(v) => onChange(nodeId, { bcc: v })} placeholder="audit@example.com" />
      </Field>
      <Field label="Subject *">
        <TextInput value={params.subject || ''} onChange={(v) => onChange(nodeId, { subject: v })} placeholder="Pipeline {{pipeline_name}} completed" />
      </Field>
      <Field label="Body Type">
        <Select value={params.body_type || 'plain'} onChange={(v) => onChange(nodeId, { body_type: v })}
          options={['plain', 'html']} />
      </Field>
      <Field label="Body *">
        <TextArea
          value={params.body || ''}
          onChange={(v) => onChange(nodeId, { body: v })}
          placeholder={params.body_type === 'html' ? '<h1>Report</h1>\n<p>Rows: {{row_count}}</p>' : 'Pipeline completed.\n\nRows: {{row_count}}\nDuration: {{duration}}'}
          rows={6}
        />
      </Field>
      <Field label="Per-Row Mode">
        <Toggle value={!!params.per_row} onChange={(v) => onChange(nodeId, { per_row: v })}
          label="Send one email per input row (use {column} placeholders)" />
      </Field>
      <Field label="On Error">
        <Select value={params.on_error || 'fail'} onChange={(v) => onChange(nodeId, { on_error: v })}
          options={['fail', 'continue']} />
      </Field>
    </>
  );
}

function SlackNotifyConfig({ params, nodeId, onChange }: ConfigProps) {
  // 2026-06-17 honesty pass: the backend SlackNotifyNode.execute reads ONLY
  // webhook_url, message, channel (flow_control.py). The old form's
  // Connection picker + Bot Name + Notification Type + Mention Users were all
  // dead — and worse, picking a Connection HID the webhook field, so the node
  // silently sent nothing. Form now matches the real contract.
  return (
    <>
      <Field label="Webhook URL *">
        <SecretInput value={params.webhook_url || ''} onChange={(v) => onChange(nodeId, { webhook_url: v })} placeholder="https://hooks.slack.com/services/..." />
        <div className="text-[9px] text-gray-400 mt-0.5">Slack (or Teams) Incoming Webhook URL — create one at api.slack.com → Incoming Webhooks. Required.</div>
      </Field>
      <Field label="Channel">
        <TextInput value={params.channel || ''} onChange={(v) => onChange(nodeId, { channel: v })} placeholder="#data-alerts" />
        <div className="text-[9px] text-gray-400 mt-0.5">Optional — overrides the channel the webhook is bound to.</div>
      </Field>
      <Field label="Message *">
        <TextArea
          value={params.message || ''}
          onChange={(v) => onChange(nodeId, { message: v })}
          placeholder={'Pipeline finished :white_check_mark:\nRows: {row_count}'}
          rows={5}
        />
        <div className="text-[9px] text-gray-400 mt-0.5">{'{column}'} placeholders are replaced from the first input row.</div>
      </Field>
    </>
  );
}

/* ═══════════════════════════════════════════════════════
   Control flow & integration primitives
   Backend: fpulse/nodes/control_extras.py
   ═══════════════════════════════════════════════════════ */

function AppendVariableConfig({ params, nodeId, onChange }: ConfigProps) {
  return (
    <>
      <Field label="Variable Name *">
        <TextInput
          value={params.variable_name || ''}
          onChange={(v) => onChange(nodeId, { variable_name: v })}
          placeholder="my_array"
        />
      </Field>
      <Field label="Value to Append">
        <TextInput
          value={params.value || ''}
          onChange={(v) => onChange(nodeId, { value: v })}
          placeholder="literal or @{expression}"
        />
      </Field>
      <div className="bg-indigo-50 rounded-lg px-3 py-2 border border-indigo-100">
        <div className="text-[9px] text-indigo-600">
          Append a value to an array variable. The variable accumulates
          across iterations of a ForEach loop.
        </div>
      </div>
    </>
  );
}

function FilterArrayConfig({ params, nodeId, onChange }: ConfigProps) {
  return (
    <>
      <Field label="Input Variable *">
        <TextInput
          value={params.input_variable || ''}
          onChange={(v) => onChange(nodeId, { input_variable: v })}
          placeholder="my_array"
        />
      </Field>
      <Field label="Output Variable">
        <TextInput
          value={params.output_variable || ''}
          onChange={(v) => onChange(nodeId, { output_variable: v })}
          placeholder="my_array_filtered"
        />
      </Field>
      <Field label="Filter Condition *">
        <TextArea
          value={params.condition || ''}
          onChange={(v) => onChange(nodeId, { condition: v })}
          placeholder="item > 100"
          rows={3}
        />
      </Field>
      <div className="bg-teal-50 rounded-lg px-3 py-2 border border-teal-100">
        <div className="text-[9px] text-teal-600">
          Filter an array of items by a condition. Use <code className="bg-white px-1 rounded border border-teal-200">item</code> to reference each element. Items where the condition is TRUE are kept.
        </div>
      </div>
    </>
  );
}

function ValidationConfig({ params, nodeId, onChange }: ConfigProps) {
  return (
    <>
      <Field label="File / Directory Path *">
        <TextInput
          value={params.path || ''}
          onChange={(v) => onChange(nodeId, { path: v })}
          placeholder="/data/incoming/orders.csv"
        />
      </Field>
      <Field label="Timeout (seconds)">
        <TextInput
          value={String(params.timeout ?? 60)}
          onChange={(v) => onChange(nodeId, { timeout: parseInt(v) || 60 })}
          placeholder="60"
        />
      </Field>
      <Field label="Poll Interval (seconds)">
        <TextInput
          value={String(params.sleep ?? 5)}
          onChange={(v) => onChange(nodeId, { sleep: parseInt(v) || 5 })}
          placeholder="5"
        />
      </Field>
      <Field label="Minimum Size (bytes)">
        <TextInput
          value={String(params.minimum_size ?? 0)}
          onChange={(v) => onChange(nodeId, { minimum_size: parseInt(v) || 0 })}
          placeholder="0"
        />
      </Field>
      <Field label="Require Child Items">
        <Toggle
          value={!!params.child_items}
          onChange={(v) => onChange(nodeId, { child_items: v })}
          label="Wait until directory is non-empty"
        />
      </Field>
      <div className="bg-cyan-50 rounded-lg px-3 py-2 border border-cyan-100">
        <div className="text-[9px] text-cyan-600">
          Polls until the dataset is present (file exists, row count meets
          threshold) before continuing. Aborts on timeout.
        </div>
      </div>
    </>
  );
}

function FailConfig({ params, nodeId, onChange }: ConfigProps) {
  return (
    <>
      <Field label="Failure Message *">
        <TextArea
          value={params.message || ''}
          onChange={(v) => onChange(nodeId, { message: v })}
          placeholder="Row count below threshold ({row_count})"
          rows={3}
        />
      </Field>
      <Field label="Error Code">
        <TextInput
          value={params.error_code || ''}
          onChange={(v) => onChange(nodeId, { error_code: v })}
          placeholder="USER_FAIL"
        />
      </Field>
      <Field label="Fail When (optional)">
        <TextInput
          value={params.condition || ''}
          onChange={(v) => onChange(nodeId, { condition: v })}
          placeholder="row_count < 100"
        />
      </Field>
      <div className="bg-red-50 rounded-lg px-3 py-2 border border-red-100">
        <div className="text-[9px] text-red-600">
          Stops the pipeline with a custom error message. Use to short-circuit
          when an upstream check fails. If the condition is empty, the pipeline
          always fails when this node runs. Supports <code className="bg-white px-1 rounded border border-red-200">{'{column}'}</code> placeholders.
        </div>
      </div>
    </>
  );
}

function FileSystemConfig({ params, nodeId, onChange }: ConfigProps) {
  const op = params.operation || 'copy_file';
  const needsDest = ['copy_file', 'move_file', 'rename_file', 'copy_directory', 'move_directory'].includes(op);
  return (
    <>
      <Field label="Operation *">
        <Select
          value={op}
          onChange={(v) => onChange(nodeId, { operation: v })}
          options={[
            'copy_file', 'move_file', 'rename_file', 'delete_file',
            'copy_directory', 'move_directory', 'delete_directory',
            'create_directory',
          ]}
        />
      </Field>
      <Field label="Source Path *">
        <TextInput
          value={params.source || ''}
          onChange={(v) => onChange(nodeId, { source: v })}
          placeholder="/data/incoming/orders.csv"
        />
      </Field>
      {needsDest && (
        <Field label="Destination Path *">
          <TextInput
            value={params.destination || ''}
            onChange={(v) => onChange(nodeId, { destination: v })}
            placeholder="/data/processed/orders.csv"
          />
        </Field>
      )}
      <Field label="Overwrite if Exists">
        <Toggle
          value={!!params.overwrite}
          onChange={(v) => onChange(nodeId, { overwrite: v })}
          label="Replace existing files / merge directories"
        />
      </Field>
      {op.includes('director') && (
        <Field label="Recursive">
          <Toggle
            value={params.recursive !== false}
            onChange={(v) => onChange(nodeId, { recursive: v })}
            label="Recurse into subfolders"
          />
        </Field>
      )}
      <Field label="Continue on Error">
        <Toggle
          value={!!params.continue_on_error}
          onChange={(v) => onChange(nodeId, { continue_on_error: v })}
          label="Don't fail the pipeline on file errors"
        />
      </Field>
      <div className="bg-amber-50 rounded-lg px-3 py-2 border border-amber-100">
        <div className="text-[9px] text-amber-600">
          Copy, move, rename, or delete files on the local F-Pulse runtime
          filesystem.
        </div>
      </div>
    </>
  );
}

function ExecuteSqlTaskConfig({ params, nodeId, onChange }: ConfigProps) {
  return (
    <>
      <Field label="Connection">
        <ConnectionPicker
          value={params.connection_id || ''}
          onChange={(v) => onChange(nodeId, { connection_id: v })}
        />
      </Field>
      <div className="text-xs text-slate-400 italic -mt-1 mb-1">
        Leave empty to run against the in-memory DuckDB.
      </div>
      <Field label="SQL Statement *">
        <TextArea
          value={params.sql || ''}
          onChange={(v) => onChange(nodeId, { sql: v })}
          placeholder={'TRUNCATE TABLE staging.events;\nCALL refresh_summary();'}
          rows={8}
        />
      </Field>
      <Field label="Return Mode">
        <Select
          value={params.return_mode || 'rowcount'}
          onChange={(v) => onChange(nodeId, { return_mode: v })}
          options={['rowcount', 'full']}
        />
      </Field>
      <Field label="Timeout (seconds)">
        <TextInput
          value={String(params.timeout ?? 60)}
          onChange={(v) => onChange(nodeId, { timeout: parseInt(v) || 60 })}
          placeholder="60"
        />
      </Field>
      <div className="bg-sky-50 rounded-lg px-3 py-2 border border-sky-100">
        <div className="text-[9px] text-sky-600">
          Run arbitrary SQL against a database connection. Use for DDL,
          stored procedures, or admin statements that don't fit Transform.
          Supports <code className="bg-white px-1 rounded border border-sky-200">{'{column}'}</code> placeholders from upstream first row.
        </div>
      </div>
    </>
  );
}

/* ────────────────────────────────────────────────
 * Connection-driven Source & Destination
 *
 * Flow: Pick connection → auto-detect type → show relevant config.
 * For storage connections → file format dropdown.
 * "Create new connection" → redirects to Connections page.
 * ──────────────────────────────────────────────── */

// Map connection.type → backend connector_type for routing
const CONNECTION_TO_CONNECTOR: Record<string, string> = {
  // Databases
  postgresql: 'database', mysql: 'database', mssql: 'database', oracle: 'database',
  sqlite: 'database', mariadb: 'database', db2: 'database', sap_hana: 'database',
  teradata: 'database', informix: 'database', cockroachdb: 'database',
  // NoSQL
  mongodb: 'database', cassandra: 'database', couchbase: 'database',
  dynamodb: 'database', cosmosdb: 'database', neo4j: 'database',
  arangodb: 'database', firebase: 'database',
  // Cloud Warehouses
  snowflake: 'database', bigquery: 'database', redshift: 'database',
  databricks: 'database', synapse: 'database', clickhouse: 'database',
  duckdb: 'database', trino: 'database', presto: 'database', athena: 'database',
  // Cloud Storage
  s3: 's3', gcs: 'gcs', azure_blob: 'azure_blob', adls_gen2: 'azure_blob', minio: 's3',
  // File Systems
  ftp: 'ftp', sftp: 'ftp', hdfs: 'ftp', local_file: 'csv',
  // Streaming
  kafka: 'kafka', rabbitmq: 'kafka', pulsar: 'kafka',
  kinesis: 'kafka', eventhub: 'kafka', sqs: 'kafka', pubsub: 'kafka', nats: 'kafka',
  // APIs
  rest_api: 'rest_api', graphql: 'rest_api', odata: 'rest_api', soap: 'rest_api',
  // SaaS
  salesforce: 'rest_api', hubspot: 'rest_api', zendesk: 'rest_api', jira: 'rest_api',
  servicenow: 'rest_api', sap: 'rest_api', netsuite: 'rest_api',
  dynamics365: 'rest_api', workday: 'rest_api',
  // Collaboration (storage-like)
  sharepoint: 'sharepoint', onedrive: 'onedrive', gdrive: 'csv',
  dropbox: 'csv', box: 'csv', gsheet: 'gsheet',
  // Notifications
  smtp: 'email', sendgrid: 'email', slack: 'webhook',
  teams: 'webhook', twilio: 'webhook', pagerduty: 'webhook',
  // Search
  redis: 'database', elasticsearch: 'database', opensearch: 'database',
  // Vector
  pinecone: 'database', weaviate: 'database', qdrant: 'database',
  chroma: 'database', pgvector: 'database', milvus: 'database',
  // Custom
  custom: 'rest_api',
};

// Storage types show file format dropdown
const STORAGE_CONN_TYPES = new Set([
  's3', 'gcs', 'azure_blob', 'adls_gen2', 'minio',
  'ftp', 'sftp', 'hdfs', 'local_file',
  'sharepoint', 'onedrive', 'gdrive', 'dropbox', 'box',
]);

// Connector types that read/write a local or uploaded file directly — no saved
// connection is needed (the file picker handles it). The generic Source/Dest
// defaults to `csv`, so the "Connection *" marker would otherwise lie about
// file mode requiring a connection.
const FILE_LOCAL_CONNECTORS = new Set(['csv', 'json', 'parquet', 'excel', 'xml']);

const FILE_FORMAT_OPTIONS = [
  { value: 'csv', label: 'CSV / Delimited' },
  { value: 'json', label: 'JSON' },
  { value: 'parquet', label: 'Parquet' },
  { value: 'excel', label: 'Excel / XLSX' },
  { value: 'xml', label: 'XML' },
  { value: 'avro', label: 'Avro' },
  { value: 'orc', label: 'ORC' },
  { value: 'delta', label: 'Delta Lake' },
];

function ConnectorConnectionPicker({ params, nodeId, onChange, connections, label, optional }: {
  params: Record<string, any>; nodeId: string;
  onChange: (id: string, p: Record<string, any>) => void;
  connections: any[]; label: string; optional?: boolean;
}) {
  // 2026-06-15 — collapsed the old expandable "YOUR SAVED CONNECTIONS" list
  // (search box + scrollable card list that ate ~35% of the dialog) down to a
  // single native dropdown. One control, no nested scroll area.
  const selectedId = params.connection_id || '';
  const selected = connections.find((c: any) => c.id === selectedId);

  // Group by type → <optgroup>s in the dropdown.
  const grouped = connections.reduce((acc: Record<string, any[]>, c: any) => {
    const cat = c.type || 'other';
    if (!acc[cat]) acc[cat] = [];
    acc[cat].push(c);
    return acc;
  }, {});

  return (
    <div className="space-y-1.5">
      <label className="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1.5">
        {label}{optional ? '' : ' *'}
      </label>
      {optional && !selected && (
        <p className="text-[11px] text-slate-400 -mt-1">
          Optional for local / uploaded files — required for databases, APIs, and cloud storage.
        </p>
      )}

      {selected ? (
        /* Compact selected badge with a Change action. */
        <div className="flex items-center gap-2 bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2">
          <span className="w-2 h-2 rounded-full bg-emerald-500 shrink-0" />
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold text-slate-800 truncate">{selected.name}</div>
            <div className="text-xs text-slate-500">{selected.type}</div>
          </div>
          <button
            onClick={() => onChange(nodeId, { connection_id: '', connector_type: 'csv', _connection_type: '' })}
            className="text-xs font-medium text-slate-400 hover:text-red-500"
          >Change</button>
        </div>
      ) : (
        /* Single dropdown — no nested scroll, no 35%-tall list. */
        <>
          <div className="flex items-center gap-2">
            <select
              value=""
              onChange={(e) => {
                const c = connections.find((x: any) => x.id === e.target.value);
                if (c) onChange(nodeId, {
                  connection_id: c.id,
                  connector_type: CONNECTION_TO_CONNECTOR[c.type] || 'rest_api',
                  _connection_type: c.type,
                });
              }}
              className="flex-1 px-3 py-2 text-sm bg-white border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 focus:border-pipe-400 text-slate-700"
            >
              <option value="">
                {connections.length ? `Select a connection… (${connections.length})` : 'No saved connections yet'}
              </option>
              {Object.entries(grouped).map(([type, conns]) => (
                <optgroup key={type} label={type}>
                  {(conns as any[]).map((c: any) => (
                    <option key={c.id} value={c.id}>{c.name}</option>
                  ))}
                </optgroup>
              ))}
            </select>
            <a
              href="#connections"
              className="text-xs font-medium text-violet-600 hover:text-violet-700 whitespace-nowrap"
            >Manage →</a>
          </div>
          <button
            onClick={() => { navigateTo('connections'); }}
            className="text-xs font-medium text-violet-600 hover:text-violet-700"
          >+ Create new connection</button>
        </>
      )}
    </div>
  );
}

function GenericSourceConfig({ params, nodeId, onChange, columns = [] }: ConfigProps) {
  const [connections, setConnections] = useState<any[]>([]);
  useEffect(() => { api.listConnections().then(setConnections).catch(() => {}); }, []);

  const connectorType = params.connector_type || '';
  const connType = params._connection_type || '';
  const isStorage = STORAGE_CONN_TYPES.has(connType);

  return (
    <>
      <ConnectorConnectionPicker
        params={params} nodeId={nodeId} onChange={onChange}
        connections={connections} label="Source Connection"
        optional={!params.connection_id && FILE_LOCAL_CONNECTORS.has(connectorType || 'csv')}
      />

      {/* File format for storage connections */}
      {params.connection_id && isStorage && (
        <Field label="File Format *">
          <Select
            value={params.file_format || 'csv'}
            onChange={(v) => onChange(nodeId, { file_format: v })}
            options={FILE_FORMAT_OPTIONS.map(f => f.value)}
            labels={FILE_FORMAT_OPTIONS.reduce((acc, f) => ({ ...acc, [f.value]: f.label }), {} as Record<string, string>)}
            placeholder="Select file format..."
          />
        </Field>
      )}

      {/* Type-specific config — reuse existing sub-configs. The embedded
          context suppresses each sub-config's redundant ConnectionPicker; the
          top ConnectorConnectionPicker above already owns the connection. */}
      <EmbeddedConnectionContext.Provider value={true}>
      {connectorType === 'csv' && <CsvSourceConfig params={params} nodeId={nodeId} onChange={onChange} />}
      {connectorType === 'json' && <JsonSourceConfig params={params} nodeId={nodeId} onChange={onChange} />}
      {connectorType === 'parquet' && <ParquetSourceConfig params={params} nodeId={nodeId} onChange={onChange} />}
      {connectorType === 'excel' && <ExcelSourceConfig params={params} nodeId={nodeId} onChange={onChange} />}
      {connectorType === 'xml' && <XmlSourceConfig params={params} nodeId={nodeId} onChange={onChange} />}
      {connectorType === 'database' && <DbSourceConfig params={params} nodeId={nodeId} onChange={onChange} hideConnection />}
      {connectorType === 'rest_api' && <ApiSourceConfig params={params} nodeId={nodeId} onChange={onChange} />}
      {connectorType === 's3' && <S3SourceConfig params={params} nodeId={nodeId} onChange={onChange} />}
      {connectorType === 'azure_blob' && <AzureBlobConfig params={params} nodeId={nodeId} onChange={onChange} isSink={false} />}
      {connectorType === 'gcs' && <GcsConfig params={params} nodeId={nodeId} onChange={onChange} isSink={false} />}
      {connectorType === 'sharepoint' && <SharePointConfig params={params} nodeId={nodeId} onChange={onChange} isSink={false} />}
      {connectorType === 'onedrive' && <OneDriveConfig params={params} nodeId={nodeId} onChange={onChange} isSink={false} />}
      {connectorType === 'kafka' && <KafkaSourceConfig params={params} nodeId={nodeId} onChange={onChange} />}
      {connectorType === 'ftp' && <FtpSourceConfig params={params} nodeId={nodeId} onChange={onChange} />}
      {connectorType === 'gsheet' && <GSheetSourceConfig params={params} nodeId={nodeId} onChange={onChange} />}
      {connectorType === 'delta' && <DeltaSourceConfig params={params} nodeId={nodeId} onChange={onChange} />}
      </EmbeddedConnectionContext.Provider>
    </>
  );
}

/* ────────────────────────────────────────────────
 * Generic Destination — connection-driven
 * ──────────────────────────────────────────────── */
function GenericDestinationConfig({ params, nodeId, onChange, columns = [], sourceTypes, mappingOnly }: ConfigProps) {
  const [connections, setConnections] = useState<any[]>([]);
  useEffect(() => { api.listConnections().then(setConnections).catch(() => {}); }, []);

  const connectorType = params.connector_type || '';
  const connType = params._connection_type || '';
  const isStorage = STORAGE_CONN_TYPES.has(connType);

  // Mapping-only render: forward to the dispatched sink config when
  // it supports a source→destination mapping. Other connector types
  // (csv/json/s3/...) collapse to a clear empty-state since they
  // don't expose per-column mapping.
  if (mappingOnly === 'show') {
    if (connectorType === 'database') {
      return <DbSinkConfig params={params} nodeId={nodeId} onChange={onChange} columns={columns} sourceTypes={sourceTypes} mappingOnly="show" />;
    }
    if (connectorType === 'warehouse') {
      return <WarehouseSinkConfig params={params} nodeId={nodeId} onChange={onChange} columns={columns} sourceTypes={sourceTypes} mappingOnly="show" />;
    }
    return (
      <div className="text-xs text-slate-500 px-1">
        Column mapping is only available for database and warehouse destinations.
      </div>
    );
  }

  return (
    <>
      <ConnectorConnectionPicker
        params={params} nodeId={nodeId} onChange={onChange}
        connections={connections} label="Destination Connection"
        optional={!params.connection_id && FILE_LOCAL_CONNECTORS.has(connectorType || 'csv')}
      />

      {/* File format for storage connections */}
      {params.connection_id && isStorage && (
        <Field label="File Format *">
          <Select
            value={params.file_format || 'csv'}
            onChange={(v) => onChange(nodeId, { file_format: v })}
            options={FILE_FORMAT_OPTIONS.map(f => f.value)}
            labels={FILE_FORMAT_OPTIONS.reduce((acc, f) => ({ ...acc, [f.value]: f.label }), {} as Record<string, string>)}
            placeholder="Select file format..."
          />
        </Field>
      )}

      {/* Type-specific config — embedded context suppresses each sub-config's
          redundant ConnectionPicker (the top picker above owns the connection). */}
      <EmbeddedConnectionContext.Provider value={true}>
      {connectorType === 'csv' && <CsvSinkConfig params={params} nodeId={nodeId} onChange={onChange} columns={columns} />}
      {connectorType === 'json' && <JsonSinkConfig params={params} nodeId={nodeId} onChange={onChange} columns={columns} />}
      {connectorType === 'excel' && <ExcelSinkConfig params={params} nodeId={nodeId} onChange={onChange} columns={columns} />}
      {connectorType === 'database' && <DbSinkConfig params={params} nodeId={nodeId} onChange={onChange} columns={columns} sourceTypes={sourceTypes} mappingOnly={mappingOnly} hideConnection />}
      {connectorType === 's3' && <S3SinkConfig params={params} nodeId={nodeId} onChange={onChange} columns={columns} />}
      {connectorType === 'azure_blob' && <AzureBlobConfig params={params} nodeId={nodeId} onChange={onChange} isSink={true} />}
      {connectorType === 'gcs' && <GcsConfig params={params} nodeId={nodeId} onChange={onChange} isSink={true} />}
      {connectorType === 'sharepoint' && <SharePointConfig params={params} nodeId={nodeId} onChange={onChange} isSink={true} />}
      {connectorType === 'onedrive' && <OneDriveConfig params={params} nodeId={nodeId} onChange={onChange} isSink={true} />}
      {connectorType === 'kafka' && <KafkaSinkConfig params={params} nodeId={nodeId} onChange={onChange} columns={columns} />}
      {connectorType === 'rest_api' && <ApiSinkConfig params={params} nodeId={nodeId} onChange={onChange} columns={columns} />}
      {connectorType === 'webhook' && <WebhookSinkConfig params={params} nodeId={nodeId} onChange={onChange} columns={columns} />}
      {connectorType === 'email' && <EmailSinkConfig params={params} nodeId={nodeId} onChange={onChange} columns={columns} />}
      {connectorType === 'delta' && <DeltaSinkConfig params={params} nodeId={nodeId} onChange={onChange} columns={columns} />}
      {connectorType === 'warehouse' && <WarehouseSinkConfig params={params} nodeId={nodeId} onChange={onChange} columns={columns} sourceTypes={sourceTypes} mappingOnly={mappingOnly} />}
      </EmbeddedConnectionContext.Provider>
    </>
  );
}
