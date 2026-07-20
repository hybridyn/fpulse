/**
 * PipelineOutline — collapsible right-rail list of every node in
 * the active pipeline, in topological order.
 *
 * Motivating UX problem (2026-05-26): at 30-40 steps the canvas
 * becomes a scroll-and-pan navigation problem — users lose their
 * place, can't see which steps already ran, and can't jump to a
 * specific step without zooming/panning. The outline collapses the
 * graph into a scannable vertical list, mirroring what every IDE
 * has done for file structure forever.
 *
 * Click a row → selects + centers that node in the canvas viewport.
 *
 * The outline does NOT replace the canvas — it's a *companion view*
 * answering "what's in this pipeline, in order, with status?" while
 * the canvas continues to answer "how is it wired?"
 */

import { useMemo } from 'react';
import { type ReactFlowInstance, type Node, type Edge } from '@xyflow/react';
import { useWorkflowStore } from '../stores/workflowStore';
import { setGeneralPreference } from '../hooks/useEditorPreferences';

interface PipelineOutlineProps {
  rfInstance: ReactFlowInstance | null;
}

/**
 * BFS topological sort matching the Canvas autoLayout pass — keeps
 * the outline order consistent with how the canvas lays nodes left
 * to right. Tie-break by current y-coordinate so siblings within a
 * layer follow their visual top-down order.
 */
function topoOrder(nodes: Node[], edges: Edge[]): Node[] {
  if (nodes.length === 0) return [];
  const inDegree: Record<string, number> = {};
  const children: Record<string, string[]> = {};
  const byId: Record<string, Node> = {};
  for (const n of nodes) {
    inDegree[n.id] = 0;
    children[n.id] = [];
    byId[n.id] = n;
  }
  for (const e of edges) {
    if (byId[e.source] && byId[e.target]) {
      inDegree[e.target] = (inDegree[e.target] || 0) + 1;
      children[e.source].push(e.target);
    }
  }
  // Layer assignment by BFS.
  const layer: Record<string, number> = {};
  const queue: string[] = [];
  for (const id of Object.keys(inDegree)) {
    if (inDegree[id] === 0) {
      layer[id] = 0;
      queue.push(id);
    }
  }
  const remaining = { ...inDegree };
  while (queue.length) {
    const cur = queue.shift()!;
    for (const child of children[cur]) {
      layer[child] = Math.max(layer[child] ?? 0, (layer[cur] ?? 0) + 1);
      remaining[child] -= 1;
      if (remaining[child] === 0) queue.push(child);
    }
  }
  // Sort by (layer ASC, y ASC, x ASC) so the list reads left-to-right
  // top-to-bottom as the user sees the canvas.
  return [...nodes].sort((a, b) => {
    const la = layer[a.id] ?? 999;
    const lb = layer[b.id] ?? 999;
    if (la !== lb) return la - lb;
    const ay = a.position?.y ?? 0;
    const by = b.position?.y ?? 0;
    if (ay !== by) return ay - by;
    return (a.position?.x ?? 0) - (b.position?.x ?? 0);
  });
}

function StatusDot({ status }: { status: string | undefined }) {
  const cls =
    status === 'success' ? 'bg-emerald-500' :
    status === 'error' ? 'bg-red-500' :
    status === 'running' ? 'bg-amber-400 animate-pulse' :
    status === 'skipped' ? 'bg-slate-300' :
    'bg-slate-200 border border-slate-300';
  return <span className={`w-2 h-2 rounded-full shrink-0 ${cls}`} />;
}

export default function PipelineOutline({ rfInstance }: PipelineOutlineProps) {
  const nodes = useWorkflowStore((s) => s.nodes);
  const edges = useWorkflowStore((s) => s.edges);
  const stepResults = useWorkflowStore((s) => s.stepResults);
  const selectedNodeId = useWorkflowStore((s) => s.selectedNodeId);
  const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);

  const ordered = useMemo(() => topoOrder(nodes, edges), [nodes, edges]);

  const onJumpTo = (n: Node) => {
    setSelectedNode(n.id);
    if (rfInstance) {
      // Center the node in the viewport at a comfortable zoom — keep
      // current zoom if user already chose one, but never zoom out
      // past 0.75 on a click-to-locate (loses context if too small).
      const zoom = Math.max(rfInstance.getZoom(), 0.75);
      rfInstance.setCenter(
        (n.position?.x ?? 0) + 90,  // approx half a node's width
        (n.position?.y ?? 0) + 30,
        { zoom, duration: 350 },
      );
    }
  };

  return (
    <aside
      className="absolute top-3 right-3 bottom-3 w-64 bg-white/95 backdrop-blur-sm border border-slate-200 rounded-xl shadow-lg flex flex-col z-20"
      onMouseDown={(e) => e.stopPropagation()}
      onWheel={(e) => e.stopPropagation()}
    >
      <header className="flex items-center justify-between px-3 py-2 border-b border-slate-100">
        <div className="flex items-center gap-2">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-500">
            <line x1="8" y1="6" x2="21" y2="6" />
            <line x1="8" y1="12" x2="21" y2="12" />
            <line x1="8" y1="18" x2="21" y2="18" />
            <line x1="3" y1="6" x2="3.01" y2="6" />
            <line x1="3" y1="12" x2="3.01" y2="12" />
            <line x1="3" y1="18" x2="3.01" y2="18" />
          </svg>
          <span className="text-xs font-bold text-slate-700 uppercase tracking-wider">Outline</span>
          <span className="text-[10px] text-slate-400 font-medium">{ordered.length}</span>
        </div>
        <button
          onClick={() => setGeneralPreference('showPipelineOutline', false)}
          className="w-5 h-5 rounded hover:bg-slate-100 flex items-center justify-center text-slate-400 hover:text-slate-600"
          title="Hide outline (toggle from canvas density widget)"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </header>
      <div className="flex-1 overflow-y-auto py-1">
        {ordered.length === 0 && (
          <div className="px-3 py-6 text-center text-[11px] text-slate-400">
            No steps yet. Drag a node onto the canvas to begin.
          </div>
        )}
        {ordered.map((n) => {
          const data = (n.data || {}) as any;
          const label = data.label || data.stepType || n.id;
          const stepType = (data.stepType || '') as string;
          const result = stepResults[n.id];
          const isSelected = n.id === selectedNodeId;
          const status = data.status as string | undefined;
          return (
            <button
              key={n.id}
              onClick={() => onJumpTo(n)}
              className={`w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-slate-50 transition-colors ${
                isSelected ? 'bg-amber-50 border-l-2 border-amber-400 pl-[10px]' : ''
              }`}
              title={`${stepType.replace(/_/g, ' ')} — click to jump`}
            >
              <StatusDot status={status} />
              <div className="flex-1 min-w-0">
                <div className="text-[11px] font-semibold text-slate-700 truncate leading-tight">{label}</div>
                <div className="text-[9px] text-slate-400 truncate">
                  {stepType.replace(/_/g, ' ')}
                </div>
              </div>
              {result?.status === 'success' && (
                <span className="text-[9px] text-emerald-600 font-medium tabular-nums shrink-0">
                  {result.row_count.toLocaleString()}
                </span>
              )}
              {result?.status === 'error' && (
                <span className="text-[9px] text-red-500 font-semibold shrink-0">err</span>
              )}
            </button>
          );
        })}
      </div>
    </aside>
  );
}
