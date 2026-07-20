import { useState, useEffect, useCallback } from 'react';
import { api } from '../../api/client';
// TierChip import removed 2026-05-19 (P2 #8) — see comment in the
// header below for the rationale.
import ErrorBanner from '../shared/ErrorBanner';
import PageHeader from '../shared/PageHeader';
import { usePageContext } from '../../hooks/usePageContext';

// ── Types ──────────────────────────────────────────────────────────────

interface LineageNode {
  id: string;
  data: { step_id: string; label: string; step_type: string; columns: string[] };
  position: { x: number; y: number };
}

interface LineageEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  data: { source_column: string; target_column: string; transform_type: string };
}

interface LineageGraph {
  nodes: LineageNode[];
  edges: LineageEdge[];
  workflow_id: string;
}

interface Workflow {
  id: string;
  name: string;
  description: string;
}

// ── Color map for transform types ──────────────────────────────────────

const TRANSFORM_COLORS: Record<string, string> = {
  passthrough: '#94a3b8',
  filter: '#f59e0b',
  transform: '#3b82f6',
  aggregate: '#8b5cf6',
  join: '#10b981',
};

const NODE_COLORS: Record<string, string> = {
  csv_source: '#10b981', db_source: '#3b82f6', api_source: '#8b5cf6',
  filter: '#f59e0b', transform: '#6366f1', aggregate: '#a855f7',
  join: '#14b8a6', window: '#ec4899', output: '#ef4444',
  file_sink: '#ef4444', db_sink: '#ef4444', api_sink: '#ef4444',
};

// ── Component ──────────────────────────────────────────────────────────

export default function LineagePage({ environment = 'dev', tier = 'free' }: { environment?: 'dev' | 'prod'; tier?: string }) {
  // 2026-05-19 (P2 #8 of PAGE_BY_PAGE_AUDIT.md): PROD chrome gated on
  // tier + environment per the OSS-no-PROD-chrome rule. The page
  // delivers identical functionality on Free and Plus today — the
  // TierChip in the header implied a tier distinction that didn't exist
  // and has been removed below.
  const isProd = environment === 'prod' && tier === 'plus';
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [selectedWf, setSelectedWf] = useState('');
  const [graph, setGraph] = useState<LineageGraph | null>(null);
  const [loading, setLoading] = useState(false);
  const [highlightCol, setHighlightCol] = useState('');
  const [columnTrace, setColumnTrace] = useState<any>(null);
  // 2026-05-19 (P1 #4 of PAGE_BY_PAGE_AUDIT.md): a failed /api/lineage/:id
  // used to silently render "No lineage data" — identical to "lineage not
  // built yet". Reviewers couldn't tell whether to click Rebuild or report
  // a bug. We now capture the error so the body can render a real banner.
  const [lineageError, setLineageError] = useState<string | null>(null);

  useEffect(() => {
    api.get('/api/workflows').then(r => {
      if (Array.isArray(r)) setWorkflows(r);
    }).catch(() => {});
  }, []);

  // V7 round 4 — deep-link via ?entity=workflow:<id> in the page hash.
  // Lets external callers (StoragePreviewDrawer's "View lineage"
  // button, Pipelines row, Copilot suggestions) open this page focused
  // on a specific entity. Resolved once on mount + on hashchange.
  useEffect(() => {
    const resolveEntity = () => {
      const hash = window.location.hash;  // e.g. "#lineage?entity=workflow:wf_abc123"
      const qIdx = hash.indexOf('?');
      if (qIdx < 0) return;
      const search = new URLSearchParams(hash.slice(qIdx + 1));
      const entity = search.get('entity');
      if (!entity) return;
      const [kind, ref] = entity.split(':', 2);
      if (kind === 'workflow' && ref) {
        setSelectedWf(ref);
      }
      // `table:schema.name` is reserved for a future round once the
      // backend exposes table-centric lineage; ignore unknown kinds.
    };
    resolveEntity();
    window.addEventListener('hashchange', resolveEntity);
    return () => window.removeEventListener('hashchange', resolveEntity);
  }, []);

  const loadLineage = useCallback(async (wfId: string) => {
    if (!wfId) return;
    setLoading(true);
    setLineageError(null);
    setHighlightCol('');
    setColumnTrace(null);
    try {
      const data = await api.get(`/api/lineage/${wfId}`);
      // Auto-layout: arrange nodes left-to-right
      const layouted = autoLayout(data);
      setGraph(layouted);
    } catch (err: any) {
      setGraph(null);
      setLineageError(err?.message || 'Failed to load lineage');
    } finally {
      setLoading(false);
    }
  }, []);

  const traceColumn = async (col: string) => {
    if (!selectedWf || !col) return;
    setHighlightCol(col);
    try {
      const trace = await api.get(`/api/lineage/${selectedWf}/column/${encodeURIComponent(col)}`);
      setColumnTrace(trace);
    } catch {
      setColumnTrace(null);
    }
  };

  const rebuildLineage = async () => {
    if (!selectedWf) return;
    setLoading(true);
    try {
      const data = await api.post(`/api/lineage/${selectedWf}/rebuild`);
      setGraph(autoLayout(data));
    } catch {}
    setLoading(false);
  };

  // All unique columns across all nodes
  const allColumns = graph ? [...new Set(graph.nodes.flatMap(n => n.data.columns))] : [];

  const tracedNodeIds = columnTrace ? new Set(columnTrace.nodes?.map((n: any) => n.id) || []) : null;
  const tracedEdgeIds = columnTrace ? new Set(columnTrace.edges?.map((e: any) => e.id) || []) : null;

  // OSS-4 (2026-05-19) — publish context so the Copilot can answer
  // "which column flows into the sink?" without re-fetching the lineage.
  usePageContext({
    page: 'lineage',
    visible_items: [{
      id: 'lineage',
      kind: 'lineage',
      meta: {
        selected_workflow_id: selectedWf || null,
        node_count: graph?.nodes.length || 0,
        column_count: allColumns.length,
        highlighted_column: highlightCol || null,
      },
    }],
  });

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <PageHeader
        environment={environment}
        icon={(
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-blue-500"><path d="M22 12h-4l-3 9L9 3l-3 9H2" /></svg>
        )}
        title="Data Lineage"
        subtitle="Column-level data flow visualization across pipelines"
        actions={(
          <>
            <select
              className="text-sm border border-slate-200 rounded-lg px-3 py-1.5 bg-white text-slate-700 focus:ring-2 focus:ring-emerald-500/20 focus:border-emerald-400"
              value={selectedWf}
              onChange={e => { setSelectedWf(e.target.value); loadLineage(e.target.value); }}
            >
              <option value="">Select pipeline...</option>
              {workflows.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
            </select>
            {selectedWf && (
              <button onClick={rebuildLineage} className="text-sm px-4 py-2 bg-emerald-50 text-emerald-700 rounded-lg hover:bg-emerald-100 border border-emerald-200">
                Rebuild
              </button>
            )}
          </>
        )}
      />

      {/* Content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Graph panel — bounded at 1400px on wide screens so the graph
            doesn't stretch edge-to-edge on 4K displays. Matches the pattern
            used by every other page in the app. */}
        <div className="flex-1 overflow-auto bg-canvas-bg">
          <div className="w-full max-w-[1500px] mx-auto p-6">
          {loading && <div className="text-center py-12 text-slate-400">Loading lineage...</div>}
          {!loading && lineageError && (
            <div className="mb-4">
              <ErrorBanner
                title="Couldn't load lineage"
                message={`${lineageError} — this is different from "lineage hasn't been built yet". Retry; if the error persists check the backend logs for /api/lineage/${selectedWf}.`}
                onRetry={() => loadLineage(selectedWf)}
                secondary={selectedWf ? { label: 'Try Rebuild', onClick: rebuildLineage } : undefined}
              />
            </div>
          )}
          {!loading && !graph && !lineageError && (
            <div className="text-center py-20 text-slate-400">
              <svg className="mx-auto mb-4" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M22 12h-4l-3 9L9 3l-3 9H2" /></svg>
              Select a pipeline to visualize its data lineage
            </div>
          )}
          {!loading && graph && graph.nodes.length === 0 && (
            <div className="text-center py-20 text-slate-400">
              No lineage data yet. Click "Rebuild" above to generate it from the pipeline definition.
            </div>
          )}
          {!loading && graph && graph.nodes.length > 0 && (
            <div className="relative" style={{ minHeight: 400 }}>
              {/* SVG edges */}
              <svg className="absolute inset-0 pointer-events-none" style={{ width: '100%', height: '100%' }}>
                {graph.edges.map(edge => {
                  const srcNode = graph.nodes.find(n => n.id === edge.source);
                  const tgtNode = graph.nodes.find(n => n.id === edge.target);
                  if (!srcNode || !tgtNode) return null;
                  const sx = srcNode.position.x + 200;
                  const sy = srcNode.position.y + 40;
                  const tx = tgtNode.position.x;
                  const ty = tgtNode.position.y + 40;
                  const mx = (sx + tx) / 2;
                  const dimmed = tracedEdgeIds && !tracedEdgeIds.has(edge.id);
                  const color = TRANSFORM_COLORS[edge.data.transform_type] || '#94a3b8';
                  return (
                    <g key={edge.id} opacity={dimmed ? 0.15 : 1}>
                      <path d={`M ${sx} ${sy} C ${mx} ${sy}, ${mx} ${ty}, ${tx} ${ty}`}
                        stroke={color} strokeWidth={2} fill="none" />
                      {edge.label && (
                        <text x={mx} y={(sy + ty) / 2 - 8} textAnchor="middle"
                          className="text-xs fill-slate-400">{edge.label}</text>
                      )}
                    </g>
                  );
                })}
              </svg>
              {/* Nodes */}
              {graph.nodes.map(node => {
                const dimmed = tracedNodeIds && !tracedNodeIds.has(node.id);
                const nodeColor = NODE_COLORS[node.data.step_type] || '#64748b';
                return (
                  <div key={node.id}
                    className={`absolute bg-white rounded-lg border shadow-sm transition-opacity ${dimmed ? 'opacity-20' : ''}`}
                    style={{ left: node.position.x, top: node.position.y, width: 200, borderColor: nodeColor + '40' }}>
                    <div className="px-3 py-2 border-b text-xs font-medium flex items-center gap-1.5"
                      style={{ borderColor: nodeColor + '20', color: nodeColor }}>
                      <span className="w-2 h-2 rounded-full" style={{ background: nodeColor }} />
                      {node.data.label || node.data.step_type}
                    </div>
                    <div className="px-3 py-1.5 max-h-32 overflow-y-auto">
                      {node.data.columns.length > 0 ? node.data.columns.map(col => (
                        <button key={col} onClick={() => traceColumn(col)}
                          className={`block w-full text-left text-xs py-0.5 px-1.5 rounded hover:bg-slate-50 transition-colors ${
                            highlightCol === col ? 'bg-emerald-50 text-emerald-700 font-medium' : 'text-slate-600'
                          }`}>
                          {col}
                        </button>
                      )) : (
                        <div className="text-xs text-slate-400 italic py-1">No columns detected</div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          </div>
        </div>

        {/* Sidebar: Column explorer */}
        {graph && graph.nodes.length > 0 && (
          <div className="w-64 border-l border-slate-200/60 bg-white overflow-y-auto">
            <div className="px-4 py-3 border-b border-slate-100">
              <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Columns</h3>
            </div>
            <div className="p-2">
              {allColumns.map(col => (
                <button key={col} onClick={() => traceColumn(col)}
                  className={`w-full text-left text-xs px-3 py-1.5 rounded-md transition-colors ${
                    highlightCol === col ? 'bg-emerald-50 text-emerald-700 font-medium' : 'text-slate-600 hover:bg-slate-50'
                  }`}>
                  {col}
                </button>
              ))}
              {allColumns.length === 0 && (
                <div className="text-xs text-slate-400 px-3 py-2">No columns in lineage graph</div>
              )}
            </div>

            {/* Column trace details */}
            {columnTrace && (
              <div className="border-t border-slate-100 px-4 py-3">
                <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2">
                  Trace: {highlightCol}
                </h3>
                <div className="space-y-1.5">
                  {columnTrace.nodes?.map((n: any) => (
                    <div key={n.id} className="text-xs bg-slate-50 rounded px-2 py-1">
                      <span className="font-medium text-slate-700">{n.label || n.type}</span>
                      <span className="text-slate-400 ml-1">({n.type})</span>
                    </div>
                  ))}
                </div>
                <div className="mt-2 space-y-1">
                  {columnTrace.edges?.map((e: any) => (
                    <div key={e.id} className="text-xs text-slate-400 flex items-center gap-1">
                      <span className="w-1.5 h-1.5 rounded-full" style={{background: TRANSFORM_COLORS[e.transform] || '#94a3b8'}} />
                      {e.source_col || '?'} → {e.target_col || '?'} ({e.transform})
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Legend */}
            <div className="border-t border-slate-100 px-4 py-3">
              <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2">Transform Types</h3>
              {Object.entries(TRANSFORM_COLORS).map(([type, color]) => (
                <div key={type} className="flex items-center gap-2 text-xs text-slate-600 py-0.5">
                  <span className="w-3 h-0.5 rounded" style={{ background: color }} />
                  {type}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Auto-layout: arrange nodes in topological columns ─────────────────

function autoLayout(graph: LineageGraph): LineageGraph {
  if (!graph || !graph.nodes.length) return graph;

  // Build adjacency
  const inDegree = new Map<string, number>();
  const children = new Map<string, string[]>();
  for (const n of graph.nodes) {
    inDegree.set(n.id, 0);
    children.set(n.id, []);
  }
  for (const e of graph.edges) {
    inDegree.set(e.target, (inDegree.get(e.target) || 0) + 1);
    children.get(e.source)?.push(e.target);
  }

  // Topological sort into layers
  const layers: string[][] = [];
  let current = [...inDegree.entries()].filter(([, d]) => d === 0).map(([id]) => id);
  const visited = new Set<string>();

  while (current.length > 0) {
    layers.push(current);
    const next: string[] = [];
    for (const nid of current) {
      visited.add(nid);
      for (const child of (children.get(nid) || [])) {
        const deg = (inDegree.get(child) || 1) - 1;
        inDegree.set(child, deg);
        if (deg === 0 && !visited.has(child)) next.push(child);
      }
    }
    current = next;
  }

  // Assign positions
  const X_GAP = 280;
  const Y_GAP = 120;
  const positioned = new Map<string, { x: number; y: number }>();
  for (let col = 0; col < layers.length; col++) {
    const layer = layers[col];
    const totalHeight = (layer.length - 1) * Y_GAP;
    const startY = Math.max(20, (400 - totalHeight) / 2);
    for (let row = 0; row < layer.length; row++) {
      positioned.set(layer[row], { x: 40 + col * X_GAP, y: startY + row * Y_GAP });
    }
  }

  // Any unpositioned nodes (cycles, disconnected)
  let extraY = 20;
  for (const n of graph.nodes) {
    if (!positioned.has(n.id)) {
      positioned.set(n.id, { x: 40 + layers.length * X_GAP, y: extraY });
      extraY += Y_GAP;
    }
  }

  return {
    ...graph,
    nodes: graph.nodes.map(n => ({ ...n, position: positioned.get(n.id) || n.position })),
  };
}
