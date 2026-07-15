import { useCallback, useRef, useState, useEffect, useMemo } from 'react';
import {
  ReactFlow,
  Background,
  MiniMap,
  BackgroundVariant,
  MarkerType,
  SelectionMode,
  type NodeMouseHandler,
  type ReactFlowInstance,
  type Node,
  type Edge,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import { useWorkflowStore } from '../stores/workflowStore';
import { nodeTypes, edgeTypes } from './nodes';
import { useDarkMode } from '../hooks/useDarkMode';
import { useEditorPreferences, getEditorPreferences } from '../hooks/useEditorPreferences';
import { uiConfirm } from '../ui/dialog';
import { validateWorkflow, type ValidationIssue } from '../utils/validateWorkflow';
import { contractFor, sideEffectClassFor, sideEffectLabel } from '../utils/nodeArity';
import { wouldCreateCycle } from '../utils/graphCycles';
// 2026-05-19 (OSS-8): the standalone requireNamedWorkflow helper is
// gone — the store's `ensureWorkflow` runs the prompt loop internally
// and returns null on cancel. See the Sample-run handler below.
import { useEmbeddedAI, type AISuggestion } from '../hooks/useEmbeddedAI';
import { useExecutionWS, type ExecutionEvent } from '../hooks/useExecutionWS';
import { toast } from './Toast';
import ValidationPanel from './ValidationPanel';
import HeroPromptCard from './editor/HeroPromptCard';
import PipelineOutline from './PipelineOutline';
import CanvasDensityToggle from './CanvasDensityToggle';
import { HIDDEN_TYPES, remapHiddenType, VALID_GHOST_TYPES, isLabelForRemovedConnector } from './hiddenNodeTypes';

/* ── Stable objects (defined outside component to avoid re-creation) ──
   Canvas responsive tuning (Apr 18):
     padding 0.15   → tighter fit, fills more of the viewport
     minZoom 0.1    → allows very-zoomed-out bird's-eye on huge pipelines
     maxZoom 2.5    → allows inspecting a single node's details
   Users see the entire pipeline by default and can still zoom into
   tight detail when needed. */
const fitViewOptions = { padding: 0.15, minZoom: 0.25, maxZoom: 1.5, duration: 300 };
const proOptions = { hideAttribution: true };
const defaultEdgeOptions = {
  type: 'custom' as const,
  animated: true,
  style: { stroke: '#6366f1', strokeWidth: 2 },
  markerEnd: {
    type: MarkerType.ArrowClosed,
    width: 16,
    height: 16,
    color: '#6366f1',
  },
};

/* ── MiniMap node color by category ── */
const CATEGORY_COLORS: Record<string, string> = {
  source: '#3b82f6',    // blue
  transform: '#10b981', // green
  combine: '#10b981',   // green (same family as transform)
  output: '#8b5cf6',    // purple
  flow: '#eab308',      // yellow
  action: '#f97316',    // orange
};

const miniMapNodeColor = (n: any) => {
  const category = (n.data as any)?.category;
  if (category && CATEGORY_COLORS[category]) return CATEGORY_COLORS[category];
  return (n.data as any)?.color || '#6366f1';
};

/* ── Auto-Layout Algorithm (dagre-like topological sort) ── */
function autoLayout(nodes: Node[], edges: Edge[]): Node[] {
  if (nodes.length === 0) return nodes;

  // Build adjacency
  const inDegree: Record<string, number> = {};
  const children: Record<string, string[]> = {};
  const nodeMap: Record<string, Node> = {};

  for (const n of nodes) {
    inDegree[n.id] = 0;
    children[n.id] = [];
    nodeMap[n.id] = n;
  }
  for (const e of edges) {
    if (nodeMap[e.source] && nodeMap[e.target]) {
      inDegree[e.target] = (inDegree[e.target] || 0) + 1;
      children[e.source].push(e.target);
    }
  }

  // Topological sort with BFS to assign layers
  const queue: string[] = [];
  const layer: Record<string, number> = {};
  for (const id of Object.keys(inDegree)) {
    if (inDegree[id] === 0) {
      queue.push(id);
      layer[id] = 0;
    }
  }

  const visited = new Set<string>();
  while (queue.length > 0) {
    const current = queue.shift()!;
    if (visited.has(current)) continue;
    visited.add(current);
    for (const child of (children[current] || [])) {
      layer[child] = Math.max(layer[child] || 0, (layer[current] || 0) + 1);
      inDegree[child]--;
      if (inDegree[child] === 0) {
        queue.push(child);
      }
    }
  }

  // Assign positions for unvisited nodes (disconnected)
  for (const n of nodes) {
    if (!(n.id in layer)) {
      layer[n.id] = 0;
    }
  }

  // Group nodes by layer
  const layers: Record<number, string[]> = {};
  for (const [id, l] of Object.entries(layer)) {
    if (!layers[l]) layers[l] = [];
    layers[l].push(id);
  }

  // Position: 300px horizontal between layers, 120px vertical between nodes
  const H_SPACING = 300;
  const V_SPACING = 120;
  const START_X = 80;
  const START_Y = 80;

  const updated = nodes.map(n => ({ ...n }));
  for (const [layerIdx, nodeIds] of Object.entries(layers)) {
    const li = parseInt(layerIdx);
    const totalHeight = (nodeIds.length - 1) * V_SPACING;
    const startY = START_Y + (nodes.length > 1 ? -totalHeight / 2 + 200 : 0);
    nodeIds.forEach((id, idx) => {
      const node = updated.find(n => n.id === id);
      if (node) {
        node.position = {
          x: START_X + li * H_SPACING,
          y: startY + idx * V_SPACING,
        };
      }
    });
  }

  return updated;
}

/* ── Legacy ghost remap ──
 * The AI suggestion endpoint sometimes returns per-format node types
 * (csv_source, json_sink, …) that we collapsed into the generic
 * source/destination palette. Map them so the ghost always points to
 * a node the user can actually see in the Modules panel.
 *
 * Three filters in order:
 *   1. Drop suggestions whose LABEL names a removed connector ("Load CSV",
 *      "Write JSON" — even if the underlying type is generic).
 *   2. If the type is in HIDDEN_TYPES, remap to the generic equivalent
 *      (or drop if no safe remap exists).
 *   3. After remap, the final type MUST be in VALID_GHOST_TYPES — anything
 *      else is dropped so we never ghost a node that isn't in the palette.
 */
function remapLegacyGhost(s: AISuggestion): AISuggestion | null {
  if (isLabelForRemovedConnector(s.label)) return null;
  let out: AISuggestion = s;
  if (HIDDEN_TYPES.has(s.node_type)) {
    const m = remapHiddenType(s.node_type);
    if (!m) return null;
    out = { ...s, node_type: m.type, label: m.label };
  }
  if (!VALID_GHOST_TYPES.has(out.node_type)) return null;
  return out;
}

/* ── Ghost Node Component ── */
function GhostNode({
  suggestion,
  position,
  onAccept,
  onDismiss,
}: {
  suggestion: AISuggestion;
  position: { x: number; y: number };
  onAccept: () => void;
  onDismiss: () => void;
}) {
  return (
    <div
      className="absolute pointer-events-auto z-10 transition-all duration-300"
      style={{
        left: position.x,
        top: position.y,
        opacity: 0.4,
      }}
    >
      <div className="relative">
        <div
          className="w-[160px] rounded-xl border-2 border-dashed border-indigo-300 bg-white/60 backdrop-blur-sm p-3 cursor-pointer hover:opacity-80 transition-opacity"
          onClick={onAccept}
        >
          <div className="flex items-center gap-2 mb-1">
            <div className="w-6 h-6 rounded-lg bg-indigo-100 flex items-center justify-center">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 2l2.4 7.4H22l-6.2 4.5L18.2 22 12 17.5 5.8 22l2.4-8.1L2 9.4h7.6z" />
              </svg>
            </div>
            <span className="text-xs font-semibold text-indigo-600">{suggestion.label}</span>
          </div>
          <div className="text-[9px] text-slate-400 leading-snug">{suggestion.reason}</div>
          <div className="mt-2 text-[8px] text-indigo-400 font-medium">Click to add</div>
        </div>
        {/* Dismiss button */}
        <button
          onClick={(e) => { e.stopPropagation(); onDismiss(); }}
          className="absolute -top-2 -right-2 w-5 h-5 rounded-full bg-slate-200 hover:bg-slate-300 flex items-center justify-center transition-colors"
          title="Dismiss suggestion"
        >
          <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="#64748b" strokeWidth="3" strokeLinecap="round">
            <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>
    </div>
  );
}

/* ── Execution Overlay — shows progress badges on nodes ── */
function ExecutionOverlay({
  stepStates,
  isExecuting,
}: {
  stepStates: Record<string, ExecutionEvent>;
  isExecuting: boolean;
}) {
  if (!isExecuting && Object.keys(stepStates).length === 0) return null;

  return (
    <>
      {/* CSS for pulse animation is injected via style tag */}
      <style>{`
        @keyframes fpulse-glow {
          0%, 100% { box-shadow: 0 0 4px 2px rgba(34,197,94,0.3); }
          50% { box-shadow: 0 0 16px 6px rgba(34,197,94,0.6); }
        }
        .fpulse-executing {
          animation: fpulse-glow 1.5s ease-in-out infinite;
        }
        @keyframes fpulse-flow {
          from { stroke-dashoffset: 24; }
          to { stroke-dashoffset: 0; }
        }
        .react-flow__edge.executing .react-flow__edge-path {
          stroke: #22c55e;
          stroke-dasharray: 8 4;
          animation: fpulse-flow 0.6s linear infinite;
        }
      `}</style>
    </>
  );
}

export default function Canvas() {
  const dark = useDarkMode();
  // Live editor preferences from Settings → General. Reacts to
  // `fpulse-settings-changed` window event so the canvas updates without
  // a page reload when the user saves Settings.
  const editorPrefs = useEditorPreferences();
  // Use individual selectors to prevent unnecessary re-renders
  const nodes = useWorkflowStore((s) => s.nodes);
  const edges = useWorkflowStore((s) => s.edges);
  const pipelineParameters = useWorkflowStore((s) => s.parameters);
  const onNodesChange = useWorkflowStore((s) => s.onNodesChange);
  const onEdgesChange = useWorkflowStore((s) => s.onEdgesChange);
  const onConnect = useWorkflowStore((s) => s.onConnect);
  const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);
  const useTemplate = useWorkflowStore((s) => s.useTemplate);
  const addNode = useWorkflowStore((s) => s.addNode);
  const setReactFlowInstance = useWorkflowStore((s) => s.setReactFlowInstance);
  const workflowId = useWorkflowStore((s) => s.workflowId);
  const workflowStatus = useWorkflowStore((s) => s.status);
  const isRunning = useWorkflowStore((s) => s.isRunning);
  const stepResults = useWorkflowStore((s) => s.stepResults);
  const isDirty = useWorkflowStore((s) => s.isDirty);
  const ensureWorkflow = useWorkflowStore((s) => s.ensureWorkflow);
  const deleteNode = useWorkflowStore((s) => s.deleteNode);

  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const [rfInstance, setRfInstance] = useState<ReactFlowInstance | null>(null);

  // Edge-arity guard. ReactFlow calls `isValidConnection` on every
  // pointer-move during a connect drag and on the final drop. We use
  // it to PROACTIVELY refuse drops that would silently lose data:
  //
  //   • Target is a source-like node (no inputs at all)         → block
  //   • Target is a single-input node that already has an edge  → block
  //
  // ReactFlow doesn't tell us "this drop was rejected" via any single
  // callback, so we stash the latest rejection reason on a ref and
  // fire one toast from `onConnectEnd`. The validator still runs on
  // every Sample/Run as a safety net for workflows imported from JSON
  // or scripted in.
  const blockedReasonRef = useRef<string | null>(null);

  const isValidConnection = useCallback(
    (conn: { source: string | null; target: string | null }) => {
      blockedReasonRef.current = null;
      if (!conn.target) return true;
      // Self-loop guard — a node feeding itself produces a logical
      // cycle the executor can't unwind. Block at drop time so the
      // user gets immediate feedback rather than a runtime "cycle
      // detected" later.
      if (conn.source && conn.source === conn.target) {
        blockedReasonRef.current = "A node can't feed itself — that's a cycle.";
        return false;
      }
      // Duplicate-edge guard — the exact same source→target edge
      // already exists. Two identical edges between the same pair
      // would render on top of each other and add no semantics.
      if (
        conn.source &&
        edges.some((e) => e.source === conn.source && e.target === conn.target)
      ) {
        blockedReasonRef.current = 'These nodes are already connected.';
        return false;
      }
      // Indirect-cycle guard (2026-06-10) — A→B→C→A closed loops used
      // to pass this check (only direct self-loops were caught) and
      // weren't detected by validateWorkflow either, so they surfaced
      // as runtime executor failures. Block at drop time instead.
      if (conn.source && wouldCreateCycle(conn.source, conn.target, edges)) {
        blockedReasonRef.current =
          "This connection would close a loop back to an upstream node — pipelines can't cycle.";
        return false;
      }
      const targetNode = nodes.find((n) => n.id === conn.target);
      if (!targetNode) return true;
      const stepType: string = (targetNode.data as any)?.stepType || '';
      const label: string = (targetNode.data as any)?.label || stepType || conn.target;
      // 2026-06-10: switched from the coarse arityFor() to the formal
      // contractFor() so drop-time enforcement matches validateWorkflow
      // exactly. arityFor('many') returned true unconditionally, which
      // let e.g. SCD2 (max 2 inputs) accept a 3rd edge the validator
      // then rejected at Run — a drop-now-fail-later trap.
      const contract = contractFor(stepType);
      if (contract.arity === 'none') {
        blockedReasonRef.current = `${label} doesn't accept inputs — it's a source.`;
        return false;
      }
      // Count inbound edges excluding one being replaced from the same
      // source (drag-to-reconnect re-fires with the same source).
      const incomingCount = edges.filter(
        (e) => e.target === conn.target && e.source !== conn.source,
      ).length;
      const maxAccepted = contract.variadic
        ? Number.POSITIVE_INFINITY
        : contract.required + contract.optional;
      if (incomingCount >= maxAccepted) {
        blockedReasonRef.current =
          maxAccepted === 1
            ? `${label} only reads one input. Insert a Union or Join node first, or remove the existing connection.`
            : `${label} accepts at most ${maxAccepted} inputs and already has ${incomingCount}. Remove a connection first, or merge streams with a Union.`;
        return false;
      }
      return true;
    },
    [nodes, edges],
  );

  const onConnectEnd = useCallback(() => {
    const reason = blockedReasonRef.current;
    if (!reason) return;
    blockedReasonRef.current = null;
    toast.warning('Connection not allowed', reason);
  }, []);

  // Ghost node state
  const [ghostSuggestion, setGhostSuggestion] = useState<AISuggestion | null>(null);
  const [ghostPosition, setGhostPosition] = useState<{ x: number; y: number }>({ x: 0, y: 0 });
  const [lastAddedNodeId, setLastAddedNodeId] = useState<string | null>(null);
  const prevNodeCount = useRef(nodes.length);

  // AI hook
  const { suggestNextNode } = useEmbeddedAI();

  // WebSocket for execution tracking
  const { stepStates, isExecuting } = useExecutionWS(workflowId);

  // Detect when a new node is added and trigger ghost suggestion
  useEffect(() => {
    if (nodes.length > prevNodeCount.current && nodes.length > 0) {
      const newNode = nodes[nodes.length - 1];
      // Skip sticky notes
      if (newNode.type !== 'stickyNote') {
        setLastAddedNodeId(newNode.id);
        // Fetch AI suggestion for the next node
        suggestNextNode(nodes, edges, newNode.id).then(suggestion => {
          if (suggestion) {
            // The AI may suggest legacy per-format node types
            // (csv_source, json_sink, …) that we collapsed into the
            // generic source/destination/copy_data palette. Remap so
            // the ghost actually adds a node the user can see.
            const remapped = remapLegacyGhost(suggestion);
            if (!remapped) return;
            setGhostSuggestion(remapped);
            // Position ghost to the right of the new node
            setGhostPosition({
              x: newNode.position.x + 280,
              y: newNode.position.y,
            });
          }
        });

        // Auto-fit canvas when the user enables it in Settings →
        // General → "Auto-fit canvas view". Slight delay so React
        // Flow has rendered the new node before fitView measures.
        if (editorPrefs.autoFitView && rfInstance) {
          setTimeout(() => {
            try {
              rfInstance.fitView({ padding: 0.2, duration: 250 });
            } catch { /* ignore */ }
          }, 60);
        }
      }
    }
    prevNodeCount.current = nodes.length;
  }, [nodes.length, editorPrefs.autoFitView, rfInstance]);

  // Auto-save (Settings → General → "Auto-save pipelines"). Debounces
  // workflow-store changes by 2 seconds after the last edit, then calls
  // ensureWorkflow() to persist them. Skip while a run is in flight to
  // avoid racing with the executor's own pre-run save.
  //
  // IMPORTANT rules (each one prevents a real production-broken-by-
  // experiment failure mode):
  //
  //   1. NEVER auto-create. Autosave only *updates* an already-saved
  //      pipeline. Creation must be an explicit Save action so the
  //      user gets the name prompt + duplicate-name guard.
  //
  //   2. NEVER auto-overwrite a PUBLISHED pipeline (2026-05-28 fix).
  //      Reported by Siva: opened a published pipeline to test a node,
  //      didn't click Save, autosave silently persisted the test edits
  //      to the live version, scheduled run later picked up the broken
  //      version and failed with "Unknown error" + email alert.
  //      Auto-save on `published` is now a no-op — user must hit Save
  //      explicitly to overwrite live (which will route through the
  //      revoke→edit→republish UX with the appropriate confirms).
  //      Drafts continue to auto-save (that's why users turn the
  //      setting on — to not lose work-in-progress).
  useEffect(() => {
    if (!editorPrefs.autoSave) return;
    if (!workflowId) return;          // never autosave-create; require explicit Save first
    if (workflowStatus === 'published') return;  // never auto-clobber a live pipeline
    if (isRunning) return;
    if (!isDirty) return;
    if (nodes.length === 0) return;  // nothing to save yet
    const timer = setTimeout(() => {
      ensureWorkflow().catch(() => {
        // Best-effort. The explicit Save button is the user-visible
        // path of record; an auto-save failure here just means the
        // next explicit save (or the next debounce tick) will retry.
      });
    }, 2000);
    return () => clearTimeout(timer);
  }, [nodes, edges, pipelineParameters, isDirty, isRunning, editorPrefs.autoSave, ensureWorkflow, workflowId, workflowStatus]);

  // Ctrl/Cmd+A → select every node on the canvas. Skipped when the
  // active element is a text input / textarea / contenteditable so the
  // shortcut doesn't hijack normal text-editing select-all inside
  // ConfigPanel fields, code editors, or the chat box.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      if (e.key.toLowerCase() !== 'a') return;
      const t = e.target as HTMLElement | null;
      const tag = t?.tagName?.toLowerCase();
      if (tag === 'input' || tag === 'textarea' || t?.isContentEditable) return;
      if (nodes.length === 0) return;
      e.preventDefault();
      onNodesChange(
        nodes.map((n) => ({ id: n.id, type: 'select' as const, selected: true })),
      );
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [nodes, onNodesChange]);

  // Delete / Backspace → remove every selected node on the canvas.
  // ReactFlow's built-in delete is disabled (`deleteKeyCode={null}`)
  // so we own the keystroke and route it through the store's
  // `deleteNode` action — that path pushes an undo state and also
  // cleans up edges, matching the single-node delete from FPulseNode.
  // Same input-element guard as Ctrl+A so Backspace inside config
  // fields still edits text. Honours Settings → "Confirm before
  // delete" with a multi-node-aware message.
  useEffect(() => {
    const onKey = async (e: KeyboardEvent) => {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return;
      const t = e.target as HTMLElement | null;
      const tag = t?.tagName?.toLowerCase();
      if (tag === 'input' || tag === 'textarea' || t?.isContentEditable) return;
      const selected = nodes.filter((n) => (n as any).selected);
      if (selected.length === 0) return;
      e.preventDefault();
      if (getEditorPreferences().confirmDelete) {
        const ok = await uiConfirm({
          title: selected.length === 1 ? 'Delete this node?' : `Delete ${selected.length} nodes?`,
          message:
            selected.length === 1
              ? `Remove "${(selected[0].data as any)?.label || (selected[0].data as any)?.stepType || 'node'}" from the canvas. Edges connected to this node will also be removed.`
              : `Remove ${selected.length} selected nodes from the canvas. Any edges connected to them will also be removed.`,
          confirmLabel: 'Delete',
          danger: true,
        });
        if (!ok) return;
      }
      // Delete each — store actions handle edge cleanup + undo state.
      for (const n of selected) deleteNode(n.id);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [nodes, deleteNode]);

  // Apply execution state to nodes (add CSS classes for animation)
  const decoratedNodes = useMemo(() => {
    if (!isExecuting && Object.keys(stepStates).length === 0) return nodes;

    return nodes.map(node => {
      const stepState = stepStates[node.id];
      if (!stepState) return node;

      const className = stepState.type === 'step_started' ? 'fpulse-executing' : '';
      const badge = stepState.type === 'step_completed' && stepState.row_count !== undefined
        ? `${stepState.row_count} rows`
        : stepState.type === 'step_error'
        ? 'Error'
        : undefined;

      return {
        ...node,
        className,
        data: {
          ...node.data,
          executionBadge: badge,
          executionStatus: stepState.type === 'step_completed' ? 'success'
            : stepState.type === 'step_error' ? 'error'
            : stepState.type === 'step_started' ? 'running'
            : undefined,
        },
      };
    });
  }, [nodes, stepStates, isExecuting]);

  // Decorate edges with execution animation
  const decoratedEdges = useMemo(() => {
    if (!isExecuting) return edges;

    const runningStepIds = new Set(
      Object.entries(stepStates)
        .filter(([_, evt]) => evt.type === 'step_started')
        .map(([id]) => id)
    );
    const completedStepIds = new Set(
      Object.entries(stepStates)
        .filter(([_, evt]) => evt.type === 'step_completed')
        .map(([id]) => id)
    );

    return edges.map(edge => {
      // Animate edges whose source is completed and target is running
      if (completedStepIds.has(edge.source) && runningStepIds.has(edge.target)) {
        return {
          ...edge,
          className: 'executing',
          style: { ...edge.style, stroke: '#22c55e', strokeWidth: 3 },
        };
      }
      // Dim edges whose source completed
      if (completedStepIds.has(edge.source) && completedStepIds.has(edge.target)) {
        return {
          ...edge,
          style: { ...edge.style, stroke: '#22c55e', strokeWidth: 2 },
        };
      }
      return edge;
    });
  }, [edges, stepStates, isExecuting]);

  const handleInit = useCallback((instance: ReactFlowInstance) => {
    setRfInstance(instance);
    setReactFlowInstance(instance);
  }, [setReactFlowInstance]);

  /* ── Canvas responsiveness ──
     React Flow fits on initial mount but does NOT refit when the
     parent container's size changes (e.g. user toggles the Chat panel
     or resizes the browser). We attach a ResizeObserver so the fit
     recomputes whenever the wrapper's width/height actually changes.
     Debounced via requestAnimationFrame to avoid thrashing during a
     continuous drag-resize. */
  useEffect(() => {
    if (!rfInstance || !reactFlowWrapper.current) return;
    const el = reactFlowWrapper.current;
    let raf = 0;
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        // Only refit if there are nodes; empty canvas has nothing to fit
        const { nodes: current } = useWorkflowStore.getState();
        if (current.length > 0) {
          try { rfInstance.fitView({ padding: 0.15, minZoom: 0.25, maxZoom: 1.5, duration: 200 }); } catch {}
        }
      });
    });
    ro.observe(el);
    return () => { ro.disconnect(); cancelAnimationFrame(raf); };
  }, [rfInstance]);

  const onNodeClick: NodeMouseHandler = useCallback(
    (_event, node) => {
      setSelectedNode(node.id);
    },
    [setSelectedNode],
  );

  // 2026-06-02 — Double-click now means "open this node" (double-click-to-open behavior).
  // Single-click already selects + opens the side ConfigPanel, so the
  // dedicated double-click handler exists to:
  //   1. Be the discoverable gesture for "I want to edit this node",
  //      which is the most-frequent canvas action.
  //   2. Dispatch a `fpulse-node-opened` window event the ConfigPanel
  //      listens to so it can scroll itself into view + focus the
  //      first editable field when invoked explicitly (vs the passive
  //      selection from single-click).
  // Rename moves to F2 (handled in FPulseNode) + the existing
  // right-click → Rename context-menu item.
  const onNodeDoubleClick: NodeMouseHandler = useCallback(
    (_event, node) => {
      setSelectedNode(node.id);
      try {
        window.dispatchEvent(new CustomEvent('fpulse-node-opened', { detail: { id: node.id } }));
      } catch { /* ignore — event dispatch is a polish hook, never block selection */ }
    },
    [setSelectedNode],
  );

  const onPaneClick = useCallback(() => {
    setSelectedNode(null);
  }, [setSelectedNode]);

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
  }, []);

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      const type = e.dataTransfer.getData('application/fpulse-node');
      if (!type) return;

      // Convert screen coordinates to flow coordinates (handles zoom & pan)
      if (rfInstance) {
        const position = rfInstance.screenToFlowPosition({ x: e.clientX, y: e.clientY });
        addNode(type, position);
      } else {
        // Fallback: raw coordinates
        const bounds = reactFlowWrapper.current?.getBoundingClientRect();
        if (!bounds) return;
        addNode(type, { x: e.clientX - bounds.left, y: e.clientY - bounds.top });
      }
    },
    [addNode, rfInstance],
  );

  // Ghost node accept handler
  const handleGhostAccept = useCallback(() => {
    if (!ghostSuggestion) return;
    addNode(ghostSuggestion.node_type, ghostPosition);
    if (ghostSuggestion.params) {
      // Apply suggested params after a tick
      setTimeout(() => {
        const { nodes: latestNodes } = useWorkflowStore.getState();
        const newNode = latestNodes[latestNodes.length - 1];
        if (newNode) {
          useWorkflowStore.getState().updateNodeParams(newNode.id, ghostSuggestion.params!);
        }
      }, 50);
    }
    setGhostSuggestion(null);
    toast.info('AI Suggestion', `Added ${ghostSuggestion.label} node`);
  }, [ghostSuggestion, ghostPosition, addNode]);

  const handleGhostDismiss = useCallback(() => {
    setGhostSuggestion(null);
  }, []);

  // Connection inspector (2026-06-19) — click an edge to see exactly what it
  // wires (source -> target), the SQL-Transform table alias if any, and remove
  // it explicitly. Makes wiring legible/trustworthy instead of a hover-only line.
  const [inspectEdge, setInspectEdge] = useState<
    { id: string; source: string; target: string; alias?: string } | null
  >(null);
  const handleEdgeClick = useCallback((_e: unknown, edge: { id: string; source: string; target: string; data?: unknown }) => {
    const ns = useWorkflowStore.getState().nodes;
    const lbl = (id: string) => {
      const n = ns.find((x) => x.id === id);
      const d = (n?.data as Record<string, unknown>) || {};
      return (d.label as string) || (d.stepType as string) || id;
    };
    const data = (edge.data as Record<string, unknown> | undefined) || {};
    setInspectEdge({ id: edge.id, source: lbl(edge.source), target: lbl(edge.target), alias: data.alias as string | undefined });
  }, []);

  return (
    <div
      ref={reactFlowWrapper}
      className="flex-1 relative"
      style={{ minHeight: '100%', minWidth: 0 }}
      onDragOver={onDragOver}
      onDrop={onDrop}
    >
      {/* Execution animation styles */}
      <ExecutionOverlay stepStates={stepStates} isExecuting={isExecuting} />

      {/* Connection inspector — appears when an edge is clicked. */}
      {inspectEdge && (
        <div className="absolute top-3 right-3 z-20 w-64 rounded-lg border border-slate-200 bg-white shadow-lg p-3 text-xs">
          <div className="flex items-center justify-between mb-1.5">
            <span className="font-bold uppercase tracking-wider text-slate-500 text-[10px]">Connection</span>
            <button
              type="button"
              onClick={() => setInspectEdge(null)}
              className="text-slate-400 hover:text-slate-700 leading-none"
              title="Close"
            >✕</button>
          </div>
          <div className="text-slate-700">
            <div className="font-semibold truncate" title={inspectEdge.source}>{inspectEdge.source}</div>
            <div className="text-slate-400 my-0.5">↓ flows into</div>
            <div className="font-semibold truncate" title={inspectEdge.target}>{inspectEdge.target}</div>
          </div>
          {inspectEdge.alias && (
            <div className="mt-1.5 text-[11px] text-indigo-600">
              SQL table: <code className="font-mono">{inspectEdge.alias}</code>
            </div>
          )}
          <button
            type="button"
            onClick={() => {
              onEdgesChange([{ id: inspectEdge.id, type: 'remove' }]);
              setInspectEdge(null);
            }}
            className="mt-2 w-full px-2 py-1 text-xs font-semibold rounded-md bg-rose-50 text-rose-700 hover:bg-rose-100 ring-1 ring-rose-200"
          >
            Remove connection
          </button>
        </div>
      )}

      <ReactFlow
        nodes={decoratedNodes}
        edges={decoratedEdges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        isValidConnection={isValidConnection}
        onConnectEnd={onConnectEnd}
        onNodeClick={onNodeClick}
        onNodeDoubleClick={onNodeDoubleClick}
        onEdgeClick={handleEdgeClick}
        onPaneClick={onPaneClick}
        onInit={handleInit}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        fitView
        fitViewOptions={fitViewOptions}
        defaultEdgeOptions={defaultEdgeOptions}
        proOptions={proOptions}
        selectionOnDrag
        selectionMode={SelectionMode.Partial}
        multiSelectionKeyCode="Shift"
        deleteKeyCode={null}
        minZoom={0.1}
        maxZoom={2.5}
        panOnScroll={false}
        zoomOnScroll
        zoomOnPinch
        snapToGrid={editorPrefs.snapToGrid}
        snapGrid={[20, 20]}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color={dark ? 'rgba(148,163,184,0.1)' : 'rgba(148,163,184,0.15)'} />
        {editorPrefs.showMinimap && (
          <MiniMap
            nodeColor={miniMapNodeColor}
            maskColor={dark ? 'rgba(11,18,32,0.8)' : 'rgba(240,244,248,0.8)'}
            className={dark
              ? '!bg-[#111827]/90 !backdrop-blur-sm !border !border-white/[0.08] !rounded-xl !shadow-lg'
              : '!bg-white/90 !backdrop-blur-sm !border !border-slate-200 !rounded-xl !shadow-lg'
            }
            style={{ width: 160, height: 110 }}
            pannable
            zoomable
          />
        )}
      </ReactFlow>

      {/* Ghost Node (AI Suggestion) */}
      {ghostSuggestion && rfInstance && (
        <GhostNode
          suggestion={ghostSuggestion}
          position={rfInstance.flowToScreenPosition(ghostPosition)}
          onAccept={handleGhostAccept}
          onDismiss={handleGhostDismiss}
        />
      )}

      {/* Empty state — AI-driven hero card. Lets users describe a pipeline
          in plain English; routes the intent through the existing Copilot
          dock + draft_pipeline_from_intent flow (no agent-loop changes). */}
      {nodes.length === 0 && <HeroPromptCard />}

      {/* 2026-05-26 — canvas density toggle + outline panel. Both are
          opt-in "how much do I want to see?" controls for the canvas;
          density tames per-edge label noise on fan-in joins, outline
          gives a scannable top-down view for 30+ step pipelines. See
          CanvasLabelDensity in useEditorPreferences for the rationale. */}
      <CanvasDensityToggle />
      {editorPrefs.showPipelineOutline && nodes.length > 0 && (
        <PipelineOutline rfInstance={rfInstance} />
      )}

      {/* node-graph Bottom Toolbar */}
      <BottomToolbar rfInstance={rfInstance} />
    </div>
  );
}

/* ── node-graph Bottom Toolbar ── */
function BottomToolbar({ rfInstance }: { rfInstance: ReactFlowInstance | null }) {
  const runWorkflow = useWorkflowStore((s) => s.runWorkflow);
  const isRunning = useWorkflowStore((s) => s.isRunning);
  const undo = useWorkflowStore((s) => s.undo);
  const redo = useWorkflowStore((s) => s.redo);
  const nodes = useWorkflowStore((s) => s.nodes);
  const edges = useWorkflowStore((s) => s.edges);
  const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);
  // Pipeline parameter declarations — fed to validateWorkflow so the
  // ${param.<name>} undeclared-reference check has the source of truth.
  // Without this the Sample button's onClick threw a ReferenceError
  // ("pipelineParameters is not defined"), which React swallowed
  // silently — the visible symptom was a Sample click that did nothing.
  const pipelineParameters = useWorkflowStore((s) => s.parameters);
  const workflowId = useWorkflowStore((s) => s.workflowId);
  // Push validation errors back to the store so red node-borders show
  // up immediately, matching the Toolbar's Run/Publish UX. Without
  // this the user only saw a panel they had to scroll to read.
  const setStoreValidationErrors = useWorkflowStore((s) => s.setValidationErrors);
  // Sample button creates the workflow silently on first run so the
  // user doesn't get a "Save the pipeline first" toast for what they
  // explicitly asked to preview. Backend's `ensure_unique_name`
  // handles the placeholder-name dedup ("Untitled Pipeline (2)" etc.)
  // so we don't need a name prompt here — the Toolbar's Save button
  // still requires a real name for non-placeholder commits.
  const ensureWorkflow = useWorkflowStore((s) => s.ensureWorkflow);

  const canUndo = useWorkflowStore((s) => s.undoStack.length > 0);
  const canRedo = useWorkflowStore((s) => s.redoStack.length > 0);

  // 2026-06-10: panel visibility lives in the store now (not Canvas
  // state) so Toolbar Run/Publish can open the same panel the Sample
  // flow uses — one validation UI for every entry point.
  const validationPanelIssues = useWorkflowStore((s) => s.validationPanelIssues);
  const openValidationPanel = useWorkflowStore((s) => s.openValidationPanel);
  const closeValidationPanel = useWorkflowStore((s) => s.closeValidationPanel);

  // Push per-node error messages to the store so FPulseNode renders
  // each failing node with a red ring + tooltip listing the issues.
  // Used by both handleExecute and the Sample-button onClick.
  const highlightIssuesOnNodes = (errors: ValidationIssue[]) => {
    const byNode: Record<string, string[]> = {};
    for (const e of errors) {
      if (!byNode[e.nodeId]) byNode[e.nodeId] = [];
      byNode[e.nodeId].push(e.message);
    }
    setStoreValidationErrors(byNode);
  };

  const handleFitView = () => rfInstance?.fitView({ padding: 0.3, duration: 300 });
  const handleZoomIn = () => rfInstance?.zoomIn({ duration: 200 });
  const handleZoomOut = () => rfInstance?.zoomOut({ duration: 200 });

  const handleAutoLayout = () => {
    if (nodes.length === 0) return;
    const layouted = autoLayout(nodes, edges);
    useWorkflowStore.getState().pushUndoState();
    useWorkflowStore.setState({ nodes: layouted });
    // Fit view after layout
    setTimeout(() => rfInstance?.fitView({ padding: 0.3, duration: 400 }), 50);
    toast.success('Auto-Layout', 'Nodes arranged automatically');
  };

  const handleExecute = () => {
    const issues = validateWorkflow(nodes, edges, pipelineParameters, workflowId);
    if (issues.length > 0) {
      openValidationPanel(issues);
      return;
    }
    runWorkflow();
  };

  return (
    <>
      <div className="absolute bottom-4 left-1/2 -translate-x-1/2 flex items-center gap-1 bg-white rounded-xl shadow-lg border border-slate-200/80 px-2 py-1.5 z-20">
        {/* Zoom controls */}
        <ToolbarBtn onClick={handleZoomOut} title="Zoom Out" disabled={false}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" /><line x1="8" y1="11" x2="14" y2="11" />
          </svg>
        </ToolbarBtn>
        <ToolbarBtn onClick={handleZoomIn} title="Zoom In" disabled={false}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" /><line x1="8" y1="11" x2="14" y2="11" /><line x1="11" y1="8" x2="11" y2="14" />
          </svg>
        </ToolbarBtn>
        <ToolbarBtn onClick={handleFitView} title="Fit View" disabled={false}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M8 3H5a2 2 0 0 0-2 2v3" /><path d="M21 8V5a2 2 0 0 0-2-2h-3" />
            <path d="M3 16v3a2 2 0 0 0 2 2h3" /><path d="M16 21h3a2 2 0 0 0 2-2v-3" />
          </svg>
        </ToolbarBtn>

        <div className="w-px h-5 bg-slate-200 mx-1" />

        {/* Auto-Layout */}
        <ToolbarBtn onClick={handleAutoLayout} title="Auto-Layout (arrange nodes)" disabled={nodes.length === 0}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="3" width="7" height="7" rx="1" />
            <rect x="14" y="3" width="7" height="7" rx="1" />
            <rect x="8" y="14" width="7" height="7" rx="1" />
            <line x1="6.5" y1="10" x2="6.5" y2="14" />
            <line x1="17.5" y1="10" x2="17.5" y2="14" />
          </svg>
        </ToolbarBtn>

        <div className="w-px h-5 bg-slate-200 mx-1" />

        {/* Undo/Redo */}
        <ToolbarBtn onClick={undo} title="Undo (Ctrl+Z)" disabled={!canUndo}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="1 4 1 10 7 10" /><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
          </svg>
        </ToolbarBtn>
        <ToolbarBtn onClick={redo} title="Redo (Ctrl+Shift+Z)" disabled={!canRedo}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.13-9.36L23 10" />
          </svg>
        </ToolbarBtn>

        {/* Add Note */}
        <ToolbarBtn onClick={() => {
          const { nodes: currentNodes } = useWorkflowStore.getState();
          const maxX = currentNodes.length > 0 ? Math.max(...currentNodes.map(n => n.position.x)) + 200 : 100;
          const centerY = currentNodes.length > 0 ? currentNodes.reduce((sum, n) => sum + n.position.y, 0) / currentNodes.length : 200;
          const newNote = {
            id: `note_${Date.now()}`,
            type: 'stickyNote',
            position: { x: maxX + 40, y: centerY - 40 },
            data: { text: '', colorIdx: 0 },
            style: { width: 180, height: 120 },
          };
          useWorkflowStore.setState({ nodes: [...currentNodes, newNote as any] });
        }} title="Add Sticky Note" disabled={false}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
            <line x1="12" y1="18" x2="12" y2="12" /><line x1="9" y1="15" x2="15" y2="15" />
          </svg>
        </ToolbarBtn>

        <div className="w-px h-5 bg-slate-200 mx-1" />

        {/* Run Sample — quick preview with limited rows.
            Reports live progress: spinner + "Running n/N…" while the
            backend executes (5–15s typical). Without this the user
            saw a transient toast, then nothing for ~10s and assumed
            the button was broken. */}
        {(() => {
          const totalSteps = nodes.length;
          const completedSteps = nodes.filter(
            (n) => n.data?.status === 'success' || n.data?.status === 'error',
          ).length;
          const runningSteps = nodes.filter((n) => n.data?.status === 'running').length;
          return (
            <button
              onClick={async () => {
                const issues = validateWorkflow(nodes, edges, pipelineParameters, workflowId);
                const errors = issues.filter((i) => i.level === 'error');
                if (errors.length > 0) {
                  // 1. Highlight failing nodes on canvas (red ring +
                  //    tooltip), 2. open the bottom validation panel
                  //    so the user sees the message list, 3. toast
                  //    summary so the count is obvious even if the
                  //    panel scrolls past their attention.
                  highlightIssuesOnNodes(errors);
                  openValidationPanel(issues);
                  toast.error(
                    "Can't run sample",
                    `${errors.length} ${errors.length === 1 ? 'node has an issue' : 'nodes have issues'} — fix the highlighted nodes and try again.`,
                  );
                  return;
                }
                // No errors — clear any stale red borders before
                // kicking off the sample run.
                setStoreValidationErrors({});
                // A6 — Sample-run side-effect guard. Sample is meant
                // for fast preview; running side-effect nodes (sinks,
                // HTTP calls, email/Slack sends, file deletes) on a
                // sample dataset can produce REAL writes / messages /
                // mutations against external systems. Require explicit
                // confirmation listing the impacted nodes.
                const sideEffectHits = nodes
                  .filter((n) => {
                    const t: string = (n.data as any)?.stepType || '';
                    return sideEffectClassFor(t) !== null;
                  })
                  .map((n) => {
                    const t: string = (n.data as any)?.stepType || '';
                    const lbl = (n.data as any)?.label || t;
                    return `• ${lbl} — ${sideEffectLabel(t) || 'has external side effects'}`;
                  });
                if (sideEffectHits.length > 0) {
                  const proceed = await uiConfirm({
                    title: 'Sample run will trigger real-world side effects',
                    danger: true,
                    confirmLabel: 'Run anyway',
                    cancelLabel: 'Cancel',
                    message:
                      `This pipeline has ${sideEffectHits.length} step` +
                      `${sideEffectHits.length === 1 ? '' : 's'} that touch the outside world. ` +
                      `Sample runs execute them for real — they don't dry-run:\n\n` +
                      sideEffectHits.slice(0, 8).join('\n') +
                      (sideEffectHits.length > 8 ? `\n… and ${sideEffectHits.length - 8} more` : '') +
                      `\n\nDeactivate side-effect nodes from their context menu (right-click → Deactivate) if you only want to preview the data path.`,
                  });
                  if (!proceed) return;
                }
                // No silent create — Sample runs the canvas IR directly
                // via the ephemeral endpoint (2026-05-22). The store's
                // runWorkflow handler picks the ephemeral path automatically
                // when the canvas is unsaved or dirty. Save remains the
                // only gesture that adds a row to the Pipelines list.
                runWorkflow(false);
                toast.info('Sample Run', 'Running with sample mode (limited rows for fast preview)');
              }}
              disabled={isRunning || nodes.length === 0}
              title={isRunning ? 'Sample run in progress — please wait' : 'Run Sample — preview with limited rows'}
              className="h-8 px-2.5 rounded-lg flex items-center gap-1.5 text-xs font-medium text-emerald-700 bg-emerald-50 hover:bg-emerald-100 border border-emerald-200 disabled:opacity-60 disabled:cursor-wait transition-colors min-w-[110px] justify-center"
            >
              {isRunning ? (
                <>
                  <span className="w-3 h-3 border-2 border-emerald-300 border-t-emerald-600 rounded-full animate-spin shrink-0" />
                  <span className="tabular-nums">
                    Running {Math.min(completedSteps + (runningSteps > 0 ? 1 : 0), totalSteps)}/{totalSteps}…
                  </span>
                </>
              ) : (
                <>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polygon points="5 3 19 12 5 21 5 3" fill="currentColor" />
                  </svg>
                  Sample
                </>
              )}
            </button>
          );
        })()}

      </div>

      {/* Validation Panel */}
      {validationPanelIssues && validationPanelIssues.length > 0 && (
        <ValidationPanel
          issues={validationPanelIssues}
          onSelectNode={(id) => { setSelectedNode(id); }}
          onRunAnyway={() => { closeValidationPanel(); runWorkflow(); }}
          onClose={closeValidationPanel}
        />
      )}
    </>
  );
}

function ToolbarBtn({ onClick, title, disabled, children }: { onClick: () => void; title: string; disabled: boolean; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      className="w-8 h-8 rounded-lg flex items-center justify-center text-slate-500 hover:text-slate-700 hover:bg-slate-100 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
    >
      {children}
    </button>
  );
}
