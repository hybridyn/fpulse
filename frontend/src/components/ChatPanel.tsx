import { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import { api } from '../api/client';
import { toast } from './Toast';
import { useDarkMode } from '../hooks/useDarkMode';
import { useAgentChatStore } from '../hooks/useAgentChatStore';
import ClarifyCard from './agent/ClarifyCard';

/* ======================================================================
   Guided Builder — Types & Config (Graph-based: multi-source, branches, joins, multi-output)
   ====================================================================== */

interface PNode {
  id: string;
  type: string;
  label: string;
  params: Record<string, any>;
  category: 'source' | 'transform' | 'output';
}
interface PEdge { from: string; to: string; condition: string; }

interface GuidedState {
  active: boolean;
  phase: 'overview' | 'pick_source' | 'config_source' | 'pick_transform' | 'config_transform' | 'pick_output' | 'config_output' | 'pick_connect' | 'review';
  pNodes: PNode[];
  pEdges: PEdge[];
  connectFrom: string | null;   // parent node id when adding a new node
  currentType?: string;         // type being configured
  joinTarget?: string;          // join node id to auto-connect new source to
}

const GUIDED_INITIAL: GuidedState = {
  active: false, phase: 'overview', pNodes: [], pEdges: [], connectFrom: null, currentType: undefined, joinTarget: undefined,
};

interface GuidedChatMsg {
  role: 'user' | 'assistant';
  content: string;
}
const uid = () => `n_${Math.random().toString(36).slice(2, 8)}`;

const GUIDED_SOURCES = [
  { type: 'csv_source', label: 'CSV File', icon: '📄', color: '#3b82f6', desc: 'Load from .csv', category: 'source' as const,
    fields: [{ key: 'file_path', label: 'File path', placeholder: 'e.g. data/orders.csv' }] },
  { type: 'json_source', label: 'JSON File', icon: '📋', color: '#d97706', desc: 'Load from .json', category: 'source' as const,
    fields: [{ key: 'file_path', label: 'File path', placeholder: 'e.g. data/events.json' }] },
  { type: 'parquet_source', label: 'Parquet', icon: '📦', color: '#10b981', desc: 'Load from .parquet', category: 'source' as const,
    fields: [{ key: 'file_path', label: 'File path', placeholder: 'e.g. lake/table.parquet' }] },
  { type: 'excel_source', label: 'Excel', icon: '📊', color: '#16a34a', desc: 'Load from .xlsx', category: 'source' as const,
    fields: [{ key: 'file_path', label: 'File path', placeholder: 'e.g. report.xlsx' }, { key: 'sheet_name', label: 'Sheet (optional)', placeholder: 'Sheet1' }] },
  { type: 'db_source', label: 'Database', icon: '🗄️', color: '#8b5cf6', desc: 'SQL query', category: 'source' as const,
    fields: [{ key: 'table_name', label: 'Table name', placeholder: 'e.g. public.orders' }, { key: 'connection_string', label: 'Connection', placeholder: 'postgresql://...' }] },
  { type: 'api_source', label: 'REST API', icon: '🌐', color: '#0ea5e9', desc: 'HTTP endpoint', category: 'source' as const,
    fields: [{ key: 'url', label: 'API URL', placeholder: 'https://api.example.com/data' }, { key: 'method', label: 'Method', placeholder: 'GET' }] },
  { type: 's3_source', label: 'S3 / MinIO', icon: '☁️', color: '#f97316', desc: 'Cloud storage', category: 'source' as const,
    fields: [{ key: 'bucket', label: 'Bucket', placeholder: 'my-data-bucket' }, { key: 'key', label: 'Object key', placeholder: 'raw/orders.csv' }] },
  { type: 'kafka_source', label: 'Kafka', icon: '⚡', color: 'rgba(255,255,255,0.08)', desc: 'Stream topic', category: 'source' as const,
    fields: [{ key: 'topic', label: 'Topic', placeholder: 'events.orders' }, { key: 'bootstrap_servers', label: 'Servers', placeholder: 'localhost:9092' }] },
];

const GUIDED_TRANSFORMS = [
  { type: 'filter', label: 'Filter', icon: '🔍', color: '#f59e0b', desc: 'Keep rows matching a condition', category: 'transform' as const,
    fields: [{ key: 'condition', label: 'Filter condition', placeholder: 'e.g. amount > 100' }] },
  { type: 'sort', label: 'Sort', icon: '↕️', color: '#8b5cf6', desc: 'Order rows by column', category: 'transform' as const,
    fields: [{ key: 'columns', label: 'Sort by (comma-separated)', placeholder: 'e.g. date, amount' }, { key: 'order', label: 'Order (ASC/DESC)', placeholder: 'ASC' }] },
  { type: 'deduplicate', label: 'Deduplicate', icon: '🧹', color: '#ec4899', desc: 'Remove duplicate rows', category: 'transform' as const,
    fields: [{ key: 'key', label: 'Deduplicate by key(s)', placeholder: 'e.g. order_id' }] },
  { type: 'aggregate', label: 'Aggregate', icon: '📊', color: '#06b6d4', desc: 'Group and summarize', category: 'transform' as const,
    fields: [{ key: 'group_by', label: 'Group by columns', placeholder: 'e.g. category, region' }, { key: 'metrics', label: 'Metrics', placeholder: 'e.g. sum(amount), count(*)' }] },
  { type: 'join', label: 'Join', icon: '🔗', color: '#f97316', desc: 'Combine two data paths', category: 'transform' as const,
    fields: [{ key: 'join_type', label: 'Join type', placeholder: 'INNER, LEFT, RIGHT, FULL' }, { key: 'join_key', label: 'Join key', placeholder: 'e.g. customer_id' }] },
  { type: 'conditional_split', label: 'Branch / Split', icon: '🔀', color: '#eab308', desc: 'Split into multiple paths', category: 'transform' as const,
    fields: [{ key: 'condition', label: 'Branch condition', placeholder: 'e.g. status = active' }] },
  { type: 'transform', label: 'SQL Transform', icon: '⚡', color: '#10b981', desc: 'Custom SQL expression', category: 'transform' as const,
    fields: [{ key: 'sql', label: 'SQL expression', placeholder: 'SELECT *, price * qty AS total FROM source_table' }] },
  { type: 'validate', label: 'Validate', icon: '✅', color: '#22c55e', desc: 'Data quality checks', category: 'transform' as const,
    fields: [{ key: 'rules', label: 'Validation rules', placeholder: 'e.g. amount > 0, email IS NOT NULL' }] },
  { type: 'rename', label: 'Rename', icon: '✏️', color: '#14b8a6', desc: 'Rename columns', category: 'transform' as const,
    fields: [{ key: 'mappings', label: 'Rename mappings', placeholder: 'e.g. old_name=new_name' }] },
  { type: 'derived_column', label: 'Add Column', icon: '➕', color: '#059669', desc: 'Compute new columns', category: 'transform' as const,
    fields: [{ key: 'expression', label: 'Expression', placeholder: 'e.g. total = price * quantity' }] },
];

const GUIDED_OUTPUTS = [
  { type: 'file_sink', label: 'Parquet', icon: '💾', color: '#6366f1', desc: 'Columnar format', category: 'output' as const,
    fields: [{ key: 'file_path', label: 'Output path', placeholder: 'e.g. output/results.parquet' }] },
  { type: 'csv_sink', label: 'CSV', icon: '📄', color: '#3b82f6', desc: 'Comma-separated', category: 'output' as const,
    fields: [{ key: 'file_path', label: 'Output path', placeholder: 'e.g. output/results.csv' }] },
  { type: 'json_sink', label: 'JSON', icon: '📋', color: '#d97706', desc: 'JSON format', category: 'output' as const,
    fields: [{ key: 'file_path', label: 'Output path', placeholder: 'e.g. output/results.json' }] },
  { type: 'db_sink', label: 'Database', icon: '🗃️', color: '#4f46e5', desc: 'Insert into table', category: 'output' as const,
    fields: [{ key: 'table_name', label: 'Table name', placeholder: 'e.g. public.results' }, { key: 'connection_string', label: 'Connection', placeholder: 'postgresql://...' }] },
  { type: 's3_sink', label: 'S3 / Cloud', icon: '☁️', color: '#ea580c', desc: 'Cloud storage', category: 'output' as const,
    fields: [{ key: 'bucket', label: 'Bucket', placeholder: 'my-output-bucket' }, { key: 'key', label: 'Object key', placeholder: 'processed/output.parquet' }] },
  { type: 'excel_sink', label: 'Excel', icon: '📊', color: '#16a34a', desc: 'Excel spreadsheet', category: 'output' as const,
    fields: [{ key: 'file_path', label: 'Output path', placeholder: 'e.g. output/report.xlsx' }] },
  { type: 'warehouse_sink', label: 'Data Warehouse', icon: '🏛️', color: '#7c3aed', desc: 'Analytics warehouse', category: 'output' as const,
    fields: [{ key: 'table_name', label: 'Table', placeholder: 'e.g. analytics.fact_orders' }, { key: 'schema', label: 'Schema', placeholder: 'analytics' }] },
];

/* ======================================================================
   Guided Builder — Helpers (Graph-based)
   ====================================================================== */

/** Convert graph nodes/edges to workflow format with auto-layout */
function graphToWorkflow(pNodes: PNode[], pEdges: PEdge[]) {
  // BFS-based column assignment for horizontal layout
  const outgoing = new Map<string, string[]>();
  const incoming = new Map<string, string[]>();
  for (const e of pEdges) {
    if (!outgoing.has(e.from)) outgoing.set(e.from, []);
    outgoing.get(e.from)!.push(e.to);
    if (!incoming.has(e.to)) incoming.set(e.to, []);
    incoming.get(e.to)!.push(e.from);
  }
  const roots = pNodes.filter(n => !incoming.has(n.id) || incoming.get(n.id)!.length === 0);
  const col = new Map<string, number>();
  const visited = new Set<string>();
  const queue = roots.map(r => ({ id: r.id, depth: 0 }));
  while (queue.length > 0) {
    const { id, depth } = queue.shift()!;
    if (visited.has(id)) { col.set(id, Math.max(col.get(id) || 0, depth)); continue; }
    visited.add(id);
    col.set(id, depth);
    for (const to of (outgoing.get(id) || [])) queue.push({ id: to, depth: depth + 1 });
  }
  // Assign unvisited nodes
  for (const n of pNodes) if (!col.has(n.id)) col.set(n.id, 0);

  // Group by column for y-positioning
  const maxCol = Math.max(...Array.from(col.values()), 0);
  const columns: string[][] = Array.from({ length: maxCol + 1 }, () => []);
  for (const [id, c] of col) columns[c].push(id);

  const xGap = 300, yGap = 180;
  const steps = pNodes.map(n => {
    const c = col.get(n.id) || 0;
    const row = columns[c].indexOf(n.id);
    const colSize = columns[c].length;
    const yOffset = (colSize - 1) * yGap / 2;
    return { id: n.id, type: n.type, label: n.label, params: { ...n.params }, position: { x: 100 + c * xGap, y: 150 + row * yGap - yOffset }, risk: 'low' as const };
  });
  const connections = pEdges.map(e => ({ from_step: e.from, to_step: e.to, condition: e.condition }));

  const sources = pNodes.filter(n => n.category === 'source');
  const outputs = pNodes.filter(n => n.category === 'output');
  const name = sources.length > 0
    ? `${sources.map(s => s.label.split(':')[0]).join(' + ')} → ${outputs.length > 0 ? outputs.map(o => o.label.split(':')[0]).join(' + ') : 'Pipeline'}`
    : 'New Pipeline';
  return { name, steps, connections };
}

function findGuidedMeta(type: string) {
  return [...GUIDED_SOURCES, ...GUIDED_TRANSFORMS, ...GUIDED_OUTPUTS].find((s) => s.type === type);
}

/** Get nodes that have no outgoing edges (leaf nodes — can attach more nodes) */
function getLeafNodes(pNodes: PNode[], pEdges: PEdge[]): PNode[] {
  const hasOutgoing = new Set(pEdges.map(e => e.from));
  return pNodes.filter(n => !hasOutgoing.has(n.id) && n.category !== 'output');
}

/** Get all nodes that can be a parent (not outputs) */
function getConnectableNodes(pNodes: PNode[]): PNode[] {
  return pNodes.filter(n => n.category !== 'output');
}

/* ======================================================================
   Guided Builder — Inline Card Sub-components
   ====================================================================== */

/** Grid of quick-pick option buttons */
function GuidedPickCard({
  items,
  onPick,
}: {
  items: Array<{ type: string; label: string; icon: string; color: string; desc?: string }>;
  onPick: (item: any) => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-1.5 mt-2">
      {items.map((item) => (
        <button
          key={item.type}
          onClick={() => onPick(item)}
          className="flex items-center gap-2 px-2.5 py-2 rounded-xl border border-slate-200 bg-white hover:border-indigo-300 hover:shadow-md transition-all group text-left"
        >
          <span
            className="w-6 h-6 rounded-lg flex items-center justify-center text-white text-[12px] font-bold shrink-0 shadow-sm"
            style={{ background: `linear-gradient(135deg, ${item.color}, ${item.color}cc)` }}
          >
            {item.icon}
          </span>
          <div className="min-w-0">
            <div className="text-[12px] font-semibold text-slate-700 group-hover:text-indigo-600 transition-colors truncate">{item.label}</div>
            {item.desc && <div className="text-[8px] text-slate-400 leading-tight truncate">{item.desc}</div>}
          </div>
        </button>
      ))}
    </div>
  );
}

/** Config form with fields and submit/back buttons */
function GuidedConfigForm({
  fields,
  onSubmit,
  onBack,
  submitLabel,
}: {
  fields: Array<{ key: string; label: string; placeholder: string }>;
  onSubmit: (values: Record<string, string>) => void;
  onBack?: () => void;
  submitLabel?: string;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  return (
    <div className="mt-2 space-y-2">
      {fields.map((f, idx) => (
        <div key={f.key}>
          <label className="text-[12px] font-semibold text-slate-500 block mb-0.5">{f.label}</label>
          <input
            type="text"
            className="w-full text-sm bg-white border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 placeholder:text-slate-300"
            placeholder={f.placeholder}
            value={values[f.key] || ''}
            onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); onSubmit(values); } }}
            autoFocus={idx === 0}
          />
        </div>
      ))}
      <div className="flex gap-2 pt-1">
        <button
          onClick={() => onSubmit(values)}
          className="px-4 py-1.5 rounded-lg text-sm font-bold text-white shadow-sm transition-all hover:shadow-md"
          style={{ background: 'linear-gradient(135deg, #6366f1, #4f46e5)' }}
        >
          {submitLabel || 'Continue'}
        </button>
        {onBack && (
          <button onClick={onBack} className="px-3 py-1.5 rounded-lg text-sm font-medium text-slate-400 hover:text-slate-600 hover:bg-slate-100 transition-colors">
            Back
          </button>
        )}
      </div>
    </div>
  );
}

/** Mini pipeline graph preview showing nodes + edges */
function MiniPipelinePreview({ pNodes, pEdges }: { pNodes: PNode[]; pEdges: PEdge[] }) {
  if (pNodes.length === 0) return null;
  // Group by category for a simple layered view
  const sources = pNodes.filter(n => n.category === 'source');
  const transforms = pNodes.filter(n => n.category === 'transform');
  const outputs = pNodes.filter(n => n.category === 'output');
  const layers = [sources, transforms, outputs].filter(l => l.length > 0);

  return (
    <div className="mt-2 mb-1 p-2.5 rounded-xl border border-indigo-200/60 bg-white/80 overflow-x-auto">
      <div className="text-[8px] uppercase tracking-widest text-slate-400 mb-1.5 font-bold">Pipeline Graph</div>
      <div className="flex items-start gap-1.5 min-w-0">
        {layers.map((layer, li) => (
          <div key={li} className="flex items-center gap-1.5 shrink-0">
            <div className="flex flex-col gap-1">
              {layer.map(n => {
                const meta = findGuidedMeta(n.type);
                const edgesIn = pEdges.filter(e => e.to === n.id).length;
                const edgesOut = pEdges.filter(e => e.from === n.id).length;
                return (
                  <div key={n.id} className="flex items-center gap-1.5 px-2 py-1 rounded-lg border border-slate-200 bg-white shadow-sm" style={{ borderLeftColor: meta?.color || '#94a3b8', borderLeftWidth: 3 }}>
                    <span className="text-[12px]">{meta?.icon || '⬜'}</span>
                    <div className="min-w-0">
                      <div className="text-[9px] font-semibold text-slate-700 truncate max-w-[90px]">{n.label.split(':')[0]}</div>
                      {edgesIn > 1 && <span className="text-[7px] text-indigo-400">{edgesIn} inputs</span>}
                    </div>
                  </div>
                );
              })}
            </div>
            {li < layers.length - 1 && (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#c7d2fe" strokeWidth="2.5" className="shrink-0"><path d="M5 12h14m-7-7 7 7-7 7" /></svg>
            )}
          </div>
        ))}
      </div>
      <div className="mt-1.5 text-[9px] text-slate-400">
        {pNodes.length} node{pNodes.length !== 1 ? 's' : ''} · {pEdges.length} connection{pEdges.length !== 1 ? 's' : ''}
        {sources.length > 1 && ' · Multi-source'}
        {outputs.length > 1 && ' · Multi-output'}
      </div>
    </div>
  );
}

/** Node selector — pick which existing node to connect FROM */
function NodeSelector({ nodes, onSelect, label }: { nodes: PNode[]; onSelect: (id: string) => void; label: string }) {
  return (
    <div className="mt-2">
      <div className="text-[12px] font-semibold text-slate-500 mb-1.5">{label}</div>
      <div className="space-y-1">
        {nodes.map(n => {
          const meta = findGuidedMeta(n.type);
          return (
            <button key={n.id} onClick={() => onSelect(n.id)}
              className="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg border border-slate-200 bg-white hover:border-indigo-300 hover:shadow-sm transition-all text-left"
            >
              <span className="w-5 h-5 rounded flex items-center justify-center text-white text-[9px]" style={{ background: meta?.color || '#94a3b8' }}>{meta?.icon || '⬜'}</span>
              <div className="min-w-0 flex-1">
                <div className="text-[12px] font-semibold text-slate-700 truncate">{n.label}</div>
                <div className="text-[8px] text-slate-400">{n.type}</div>
              </div>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#a5b4fc" strokeWidth="2.5" className="shrink-0"><path d="M5 12h14m-7-7 7 7-7 7" /></svg>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/* ======================================================================
   Chat — Standard helpers (unchanged)
   ====================================================================== */

const TEMPLATES = [
  { key: 'simple_etl', label: 'Simple ETL', desc: 'File \u2192 Transform \u2192 Parquet', icon: '\u{1F4C2}', color: '#3b82f6' },
  { key: 'dedup_pipeline', label: 'Dedup Pipeline', desc: 'Load \u2192 Deduplicate \u2192 Output', icon: '\u{1F9F9}', color: '#8b5cf6' },
  { key: 'aggregation', label: 'Aggregation', desc: 'Load \u2192 Filter \u2192 Aggregate', icon: '\u{1F4CA}', color: '#f59e0b' },
  { key: 'data_quality', label: 'Data Quality', desc: 'Load \u2192 Clean \u2192 Validate', icon: '\u2705', color: '#10b981' },
];

const INITIAL_SUGGESTIONS = [
  { text: 'Load orders.csv, deduplicate by order_id, output to parquet', icon: '\u{1F4E6}' },
  { text: 'Read sales.csv, filter amount > 100, aggregate by category', icon: '\u{1F4CA}' },
  { text: 'Create a pipeline to clean and validate customer data', icon: '\u{1F9F9}' },
];

function getSmartSuggestions(nodes: any[], edges: any[]): Array<{ text: string; icon: string; label: string }> {
  if (nodes.length === 0) return [];
  const types = nodes.map((n) => n.data.stepType as string);
  const hasSources = types.some((t) => t.includes('source'));
  const hasOutput = types.some((t) => t.includes('sink') || t === 'output');
  const suggestions: Array<{ text: string; icon: string; label: string }> = [];
  if (hasSources && !hasOutput) {
    suggestions.push({ text: 'Add output to parquet', icon: '\u{1F4BE}', label: 'Add output' });
    suggestions.push({ text: 'Add output to database', icon: '\u{1F5C3}\uFE0F', label: 'Save to DB' });
  }
  if (hasSources && !types.includes('filter')) suggestions.push({ text: 'Add a filter step', icon: '\u{1F50D}', label: 'Add filter' });
  if (hasSources && !types.includes('deduplicate')) suggestions.push({ text: 'Add a deduplicate step', icon: '\u{1F9F9}', label: 'Add dedup' });
  if (hasSources && !types.includes('aggregate')) suggestions.push({ text: 'Add an aggregate step', icon: '\u{1F4CA}', label: 'Add aggregate' });
  if (hasSources && !types.includes('validate')) suggestions.push({ text: 'Add a validate step', icon: '\u2705', label: 'Add validate' });
  if (hasSources && !types.includes('join')) suggestions.push({ text: 'Add a join with another source', icon: '\u{1F517}', label: 'Add join' });
  if (nodes.length > 2) suggestions.push({ text: 'What does this pipeline do?', icon: '\u2753', label: 'Explain pipeline' });
  return suggestions.slice(0, 4);
}

function getActionsForMessage(content: string, nodes: any[]): Array<{ text: string; label: string }> {
  if (content.includes('created a pipeline') || content.includes('steps generated') || content.includes('built your pipeline')) {
    const actions: Array<{ text: string; label: string }> = [];
    if (!nodes.some((n) => n.data.stepType === 'validate')) actions.push({ text: 'Add a validate step before the output', label: '+ Validate' });
    if (!nodes.some((n) => n.data.stepType === 'sort')) actions.push({ text: 'Add a sort step', label: '+ Sort' });
    actions.push({ text: 'What SQL would this pipeline generate?', label: 'Show SQL' });
    return actions.slice(0, 3);
  }
  return [];
}

/* ======================================================================
   Main ChatPanel Component
   ====================================================================== */

// User-resizable panel — same pattern as ModulesPanel on the right.
// Width persists across reloads via localStorage. Min/max keep the chat
// usable (too narrow → wrapped messages; too wide → eats the canvas).
const CHAT_PANEL_WIDTH_KEY = 'fpulse.chatPanelWidth';
const CHAT_PANEL_WIDTH_DEFAULT = 320;
const CHAT_PANEL_WIDTH_MIN = 240;
const CHAT_PANEL_WIDTH_MAX = 560;

function loadChatPanelWidth(): number {
  try {
    const raw = localStorage.getItem(CHAT_PANEL_WIDTH_KEY);
    if (!raw) return CHAT_PANEL_WIDTH_DEFAULT;
    const n = parseInt(raw, 10);
    if (Number.isNaN(n)) return CHAT_PANEL_WIDTH_DEFAULT;
    return Math.max(CHAT_PANEL_WIDTH_MIN, Math.min(CHAT_PANEL_WIDTH_MAX, n));
  } catch { return CHAT_PANEL_WIDTH_DEFAULT; }
}

export default function ChatPanel() {
  const dark = useDarkMode();
  const { chatMessages, chatOpen, setChatOpen, sendChat, useTemplate, loadWorkflow, nodes, edges } = useWorkflowStore();
  // The FloatingAgentWidget is hidden on the editor page (see App.tsx),
  // so `askCopilot(prompt)` from places like HeroPromptCard / ConfigPanel
  // / TransformHelper would otherwise push into a void. We subscribe to
  // the agent-chat store's pendingInput too, so any code asking the
  // Copilot lands in THIS panel's input box on the editor page. The
  // AgentChatPanel uses the same hook elsewhere; since it isn't mounted
  // on the editor, there's no double-consumer conflict.
  const { pendingInput, clearPendingInput } = useAgentChatStore();
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [aiAvailable, setAiAvailable] = useState<boolean | null>(null);
  const [showHelperMenu, setShowHelperMenu] = useState(false);
  // Deep-mode toggle (2026-05-22). Threads `mode: 'deep'` through
  // sendChat into the editor's agent call. Slow but better at
  // pipeline-design reasoning. See AgentChatPanel for the same UI.
  const [deepMode, setDeepMode] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Panel width + drag-to-resize. Handle on the RIGHT edge (chat is the
  // left panel, so dragging the right edge LEFT/RIGHT shrinks/grows it).
  const [panelWidth, setPanelWidth] = useState<number>(loadChatPanelWidth);
  const dragStateRef = useRef<{ startX: number; startWidth: number } | null>(null);

  const onResizeStart = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    dragStateRef.current = { startX: e.clientX, startWidth: panelWidth };
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  }, [panelWidth]);

  const onResizeMove = useCallback((e: React.PointerEvent) => {
    const s = dragStateRef.current;
    if (!s) return;
    // Panel sits on the RIGHT edge of the viewport — dragging the
    // handle LEFT grows the panel, so subtract dx from start width
    // (mirrors the ModulesPanel pattern from when ModulesPanel was
    // the right panel).
    const dx = e.clientX - s.startX;
    const next = Math.max(CHAT_PANEL_WIDTH_MIN, Math.min(CHAT_PANEL_WIDTH_MAX, s.startWidth - dx));
    setPanelWidth(next);
  }, []);

  const onResizeEnd = useCallback((e: React.PointerEvent) => {
    if (!dragStateRef.current) return;
    dragStateRef.current = null;
    try { (e.target as HTMLElement).releasePointerCapture(e.pointerId); } catch {}
    try { localStorage.setItem(CHAT_PANEL_WIDTH_KEY, String(panelWidth)); } catch {}
  }, [panelWidth]);

  // Debounced persist + ReactFlow re-fit on every width tick (matches the
  // ModulesPanel pattern so the canvas doesn't lag the resize).
  useEffect(() => {
    const t = setTimeout(() => {
      try { localStorage.setItem(CHAT_PANEL_WIDTH_KEY, String(panelWidth)); } catch {}
    }, 250);
    return () => clearTimeout(t);
  }, [panelWidth]);

  useEffect(() => {
    try { window.dispatchEvent(new Event('resize')); } catch {}
  }, [panelWidth]);

  // Publish the live width as a CSS variable on :root so fixed-position
  // overlays (ConfigPanel) can match the right edge of the canvas
  // column instead of using a hardcoded offset. Mirrors the
  // `--fpulse-nodes-panel-width` variable ModulesPanel publishes.
  // When chatOpen=false the panel renders its 40px (w-10) rail, not
  // `panelWidth` — publish the effective width so ConfigPanel's
  // modal-centering overlay doesn't keep a ~320px ghost margin on the
  // right after the node modal collapses this panel.
  useEffect(() => {
    try {
      const effectiveWidth = chatOpen ? panelWidth : 40;
      document.documentElement.style.setProperty('--fpulse-chat-panel-width', `${effectiveWidth}px`);
    } catch {}
  }, [panelWidth, chatOpen]);

  // Consume pending-input deep links coming from `askCopilot(prompt)`
  // (HeroPromptCard "Generate", ConfigPanel "Ask Copilot about this SQL",
  // TransformHelper, etc.). On the editor page the FloatingAgentWidget
  // isn't mounted, so without this hook those callers had nowhere to
  // land. We pre-fill the input box, open the panel if collapsed, and
  // clear the pending state so the next deep-link round-trips cleanly.
  useEffect(() => {
    if (!pendingInput) return;
    setInput(pendingInput);
    if (!chatOpen) setChatOpen(true);
    clearPendingInput();
    // Scroll the chat into view + focus the input so the user can
    // tweak before sending. Slight delay so the panel-open transition
    // settles first.
    setTimeout(() => {
      try { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' }); } catch {}
    }, 100);
  }, [pendingInput, chatOpen, setChatOpen, clearPendingInput]);

  // ── Guided Builder state (graph-based) ──
  const [guided, setGuided] = useState<GuidedState>({ ...GUIDED_INITIAL });
  const [guidedChat, setGuidedChat] = useState<GuidedChatMsg[]>([]);

  const addGuidedMsg = useCallback((role: 'user' | 'assistant', content: string) => {
    setGuidedChat((prev) => [...prev, { role, content }]);
  }, []);

  const scrollToBottom = useCallback(() => {
    setTimeout(() => scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' }), 80);
  }, []);

  // ── Guided Builder actions (graph-based) ──

  const startGuidedMode = useCallback(() => {
    setGuidedChat([]);
    setGuided({ active: true, phase: 'pick_source', pNodes: [], pEdges: [], connectFrom: null, currentType: undefined });
    scrollToBottom();
  }, [scrollToBottom]);

  const cancelGuided = useCallback(() => {
    setGuided({ ...GUIDED_INITIAL });
    setGuidedChat([]);
  }, []);

  /** Pick a source/transform/output type */
  const handlePickType = useCallback((item: any, category: 'source' | 'transform' | 'output') => {
    const meta = [...GUIDED_SOURCES, ...GUIDED_TRANSFORMS, ...GUIDED_OUTPUTS].find(s => s.type === item.type)!;
    addGuidedMsg('user', `${meta.icon} ${meta.label}`);
    const configPhase = category === 'source' ? 'config_source' : category === 'transform' ? 'config_transform' : 'config_output';
    setGuided(g => ({ ...g, phase: configPhase, currentType: item.type }));
    scrollToBottom();
  }, [addGuidedMsg, scrollToBottom]);

  /** Submit config for a node — add it to the graph */
  const handleConfig = useCallback((values: Record<string, string>) => {
    const type = guided.currentType!;
    const allMeta = [...GUIDED_SOURCES, ...GUIDED_TRANSFORMS, ...GUIDED_OUTPUTS];
    const meta = allMeta.find(s => s.type === type)!;
    const params = Object.fromEntries(Object.entries(values).filter(([, v]) => v));
    const paramDesc = Object.entries(params).map(([k, v]) => `${k}: ${v}`).join(', ');
    addGuidedMsg('user', paramDesc || '(default settings)');

    const label = meta.label + (params.file_path ? `: ${params.file_path}` : params.table_name ? `: ${params.table_name}` : (Object.values(params)[0] ? `: ${String(Object.values(params)[0]).substring(0, 25)}` : ''));
    const id = uid();
    const category = meta.category;
    const newNode: PNode = { id, type, label, params, category };

    setGuided(g => {
      const newNodes = [...g.pNodes, newNode];
      const newEdges = [...g.pEdges];

      // Auto-connect: if connectFrom is set, create edge
      if (g.connectFrom) {
        newEdges.push({ from: g.connectFrom, to: id, condition: 'completion' });
      }
      // Auto-connect to join target: new source → join node
      if (g.joinTarget) {
        newEdges.push({ from: id, to: g.joinTarget, condition: 'completion' });
      }
      return { ...g, phase: 'overview', pNodes: newNodes, pEdges: newEdges, connectFrom: null, currentType: undefined, joinTarget: undefined };
    });
    scrollToBottom();
  }, [guided.currentType, guided.connectFrom, addGuidedMsg, scrollToBottom]);

  /** When user picks "connect from" a specific node, then picks what to add */
  const handleSelectConnectFrom = useCallback((nodeId: string) => {
    const node = guided.pNodes.find(n => n.id === nodeId);
    if (node) addGuidedMsg('user', `Connect from: ${node.label}`);
    setGuided(g => ({ ...g, connectFrom: nodeId, phase: 'pick_transform' }));
    scrollToBottom();
  }, [guided.pNodes, addGuidedMsg, scrollToBottom]);

  /** Add a second connection to a join node */
  const handleJoinSecondInput = useCallback((joinId: string, fromId: string) => {
    const fromNode = guided.pNodes.find(n => n.id === fromId);
    if (fromNode) addGuidedMsg('user', `Also connect ${fromNode.label} → Join`);
    setGuided(g => ({
      ...g, pEdges: [...g.pEdges, { from: fromId, to: joinId, condition: 'completion' }],
    }));
    scrollToBottom();
  }, [guided.pNodes, addGuidedMsg, scrollToBottom]);

  /** Build the pipeline from graph */
  const handleBuildPipeline = useCallback(() => {
    const wf = graphToWorkflow(guided.pNodes, guided.pEdges);
    loadWorkflow({ workflow: { id: null, ...wf }, version: 1 });
    const sources = guided.pNodes.filter(n => n.category === 'source').map(s => s.label).join(', ');
    const outputs = guided.pNodes.filter(n => n.category === 'output').map(o => o.label).join(', ');
    const store = useWorkflowStore.getState();
    useWorkflowStore.setState({
      chatMessages: [
        ...store.chatMessages,
        { role: 'user' as const, content: `[Guided Builder] Create pipeline: ${guided.pNodes.map(n => n.label).join(' → ')}` },
        { role: 'assistant' as const, content: `I've built your pipeline with **${guided.pNodes.length} nodes** and **${guided.pEdges.length} connections**:\n\n**Sources:** ${sources || 'None'}\n**Outputs:** ${outputs || 'None'}\n**Total edges:** ${guided.pEdges.length}\n\nThe pipeline is on the canvas. Click any node to configure, or hit **Run All** to execute.` },
      ],
      workflowName: wf.name,
    });
    toast.success('Pipeline created!', `${guided.pNodes.length} nodes from Guided Builder`);
    setGuided({ ...GUIDED_INITIAL });
    setGuidedChat([]);
    setTimeout(() => useWorkflowStore.getState().reactFlowInstance?.fitView({ padding: 0.3, duration: 300 }), 200);
  }, [guided.pNodes, guided.pEdges, loadWorkflow]);

  // ── Standard hooks ──

  useEffect(() => {
    scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight);
  }, [chatMessages, guided, guidedChat]);

  useEffect(() => {
    api.aiStatus().then((r) => setAiAvailable(r.ai_available)).catch(() => setAiAvailable(false));
  }, []);

  const smartSuggestions = useMemo(() => getSmartSuggestions(nodes, edges), [nodes, edges]);

  if (!chatOpen) {
    return (
      <div data-fpulse-panel="chat" className="w-10 bg-slate-50 border-l border-pipe-200 flex flex-col items-center py-2 gap-1 shrink-0">
        <button
          onClick={() => setChatOpen(true)}
          className="w-8 h-8 rounded-lg flex items-center justify-center text-slate-400 hover:text-slate-600 hover:bg-slate-100 transition-all"
          title="Expand assistant panel"
        >
          {/* Chevron points LEFT — panel sits on the right edge, so
              "expand" means it grows leftward. */}
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="15 18 9 12 15 6" />
          </svg>
        </button>
        <div className="w-8 h-8 rounded-lg flex items-center justify-center text-indigo-500" title="Assistant">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          </svg>
        </div>
      </div>
    );
  }

  // Detect guided-mode trigger phrases
  const handleSend = async () => {
    if (!input.trim() || sending) return;
    const lower = input.trim().toLowerCase();
    if (!guided.active && (
      lower === 'create a pipeline' || lower === 'guided' || lower === 'guided builder' ||
      lower.startsWith('guide me') || lower === 'wizard' || lower === 'step by step' ||
      lower === 'build pipeline' || lower.startsWith('help me build') ||
      lower.startsWith('i need to build') || lower.startsWith('i want to build') ||
      lower.startsWith('i want to create') || lower.startsWith('i need to create') ||
      lower === 'build a pipeline' || lower === 'create pipeline' ||
      lower === 'new pipeline' || lower.startsWith('build me') ||
      lower.startsWith('create a new') || lower.startsWith('make a pipeline') ||
      lower.startsWith('start a pipeline') || lower.startsWith('set up a pipeline') ||
      lower.startsWith('setup a pipeline')
    )) {
      useWorkflowStore.setState((s) => ({
        chatMessages: [...s.chatMessages, { role: 'user' as const, content: input.trim() }],
      }));
      setInput('');
      startGuidedMode();
      return;
    }
    setSending(true);
    const msg = input;
    setInput('');
    setShowHelperMenu(false);
    await sendChat(msg, { deep: deepMode });
    setSending(false);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
    if (e.key === '/') setShowHelperMenu(true);
  };

  const handleSuggestion = async (text: string) => {
    if (sending) return;
    setSending(true);
    await sendChat(text, { deep: deepMode });
    setSending(false);
  };

  // Hide the QUICK START / Guided Builder block as soon as the user
  // engages with the chat (any message in the conversation). Once a
  // chat is in flight those starter cards are visual clutter — the
  // user wants the chat to take the panel. Also hidden whenever the
  // canvas already has nodes or the Guided Builder is active.
  const showTemplates = nodes.length === 0 && !guided.active && chatMessages.length === 0;

  const helperOptions = [
    { cmd: '/source', desc: 'Add a data source', fill: 'Load data from ' },
    { cmd: '/filter', desc: 'Filter rows', fill: 'Add a filter where ' },
    { cmd: '/join', desc: 'Join datasets', fill: 'Join with ' },
    { cmd: '/aggregate', desc: 'Group & aggregate', fill: 'Aggregate by ' },
    { cmd: '/output', desc: 'Add output', fill: 'Output to ' },
    { cmd: '/transform', desc: 'SQL transform', fill: 'Transform ' },
    { cmd: '/deduplicate', desc: 'Remove duplicates', fill: 'Deduplicate by ' },
    { cmd: '/sort', desc: 'Sort data', fill: 'Sort by ' },
    { cmd: '/validate', desc: 'Validate quality', fill: 'Add a validate step' },
  ];

  /* ── Render the current guided phase card (graph-based) ── */
  const renderGuidedPhase = () => {
    if (!guided.active) return null;
    const { phase, pNodes, pEdges, connectFrom, currentType } = guided;
    const sources = pNodes.filter(n => n.category === 'source');
    const transforms = pNodes.filter(n => n.category === 'transform');
    const outputs = pNodes.filter(n => n.category === 'output');
    const leafNodes = getLeafNodes(pNodes, pEdges);
    const connectableNodes = getConnectableNodes(pNodes);
    // For joins: nodes that have no edge TO the join yet
    const joinNodes = pNodes.filter(n => n.type === 'join');

    return (
      <div className="flex gap-2 justify-start">
        {/* AI avatar */}
        <div className="w-6 h-6 rounded-lg flex items-center justify-center shrink-0 mt-0.5" style={{ background: 'linear-gradient(135deg, #6366f1, #4f46e5)' }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5">
            <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
          </svg>
        </div>

        {/* Card */}
        <div className="flex-1 min-w-0 rounded-2xl rounded-bl-md border border-indigo-200 p-3 shadow-sm" style={{ background: dark ? '#1a2332' : 'linear-gradient(135deg, #eef2ff, #f8faff)' }}>

          {/* ── OVERVIEW: The pipeline hub with mini preview + action buttons ── */}
          {phase === 'overview' && (
            <>
              {pNodes.length === 0 ? (
                <>
                  <div className="text-sm font-semibold text-slate-700 mb-0.5">Start building your pipeline</div>
                  <div className="text-[12px] text-slate-400 mb-2">Add sources, transforms, branches, and outputs</div>
                </>
              ) : (
                <>
                  <div className="text-sm font-semibold text-slate-700 mb-0.5">Pipeline Builder</div>
                  <div className="text-[12px] text-slate-400">
                    {sources.length} source{sources.length !== 1 ? 's' : ''} · {transforms.length} transform{transforms.length !== 1 ? 's' : ''} · {outputs.length} output{outputs.length !== 1 ? 's' : ''}
                  </div>
                  <MiniPipelinePreview pNodes={pNodes} pEdges={pEdges} />
                </>
              )}

              {/* Action buttons grid */}
              <div className="grid grid-cols-2 gap-1.5 mt-2">
                {/* Add Source */}
                <button onClick={() => setGuided(g => ({ ...g, phase: 'pick_source', connectFrom: null }))}
                  className="flex items-center gap-2 px-2.5 py-2.5 rounded-xl border-2 border-dashed border-blue-200 hover:border-blue-400 hover:bg-blue-50/50 transition-all group text-left">
                  <div className="w-7 h-7 rounded-lg bg-blue-100 flex items-center justify-center text-blue-600 group-hover:bg-blue-200 transition-colors text-sm">📥</div>
                  <div>
                    <div className="text-[12px] font-bold text-slate-700 group-hover:text-blue-600">+ Source</div>
                    <div className="text-[8px] text-slate-400">{sources.length > 0 ? 'Add another' : 'Data input'}</div>
                  </div>
                </button>

                {/* Add Transform — requires a source first */}
                <button
                  onClick={() => {
                    if (leafNodes.length === 1) {
                      setGuided(g => ({ ...g, phase: 'pick_transform', connectFrom: leafNodes[0].id }));
                      addGuidedMsg('user', `Connect from: ${leafNodes[0].label}`);
                    } else if (leafNodes.length > 1) {
                      setGuided(g => ({ ...g, phase: 'pick_connect' }));
                    } else if (connectableNodes.length > 0) {
                      setGuided(g => ({ ...g, phase: 'pick_connect' }));
                    }
                  }}
                  disabled={pNodes.length === 0}
                  className="flex items-center gap-2 px-2.5 py-2.5 rounded-xl border-2 border-dashed border-amber-200 hover:border-amber-400 hover:bg-amber-50/50 transition-all group text-left disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <div className="w-7 h-7 rounded-lg bg-amber-100 flex items-center justify-center text-amber-600 group-hover:bg-amber-200 transition-colors text-sm">⚡</div>
                  <div>
                    <div className="text-[12px] font-bold text-slate-700 group-hover:text-amber-600">+ Transform</div>
                    <div className="text-[8px] text-slate-400">Filter, sort, join...</div>
                  </div>
                </button>

                {/* Add Output */}
                <button
                  onClick={() => {
                    if (leafNodes.length === 1) {
                      setGuided(g => ({ ...g, phase: 'pick_output', connectFrom: leafNodes[0].id }));
                      addGuidedMsg('user', `Output from: ${leafNodes[0].label}`);
                    } else if (leafNodes.length > 1 || connectableNodes.length > 0) {
                      // Show connector picker first, then output picker
                      setGuided(g => ({ ...g, phase: 'pick_connect', currentType: '__output__' }));
                    }
                  }}
                  disabled={pNodes.length === 0}
                  className="flex items-center gap-2 px-2.5 py-2.5 rounded-xl border-2 border-dashed border-emerald-200 hover:border-emerald-400 hover:bg-emerald-50/50 transition-all group text-left disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <div className="w-7 h-7 rounded-lg bg-emerald-100 flex items-center justify-center text-emerald-600 group-hover:bg-emerald-200 transition-colors text-sm">📤</div>
                  <div>
                    <div className="text-[12px] font-bold text-slate-700 group-hover:text-emerald-600">+ Output</div>
                    <div className="text-[8px] text-slate-400">{outputs.length > 0 ? 'Add another' : 'Destination'}</div>
                  </div>
                </button>

                {/* Add Branch — create a second connection from an existing node */}
                <button
                  onClick={() => setGuided(g => ({ ...g, phase: 'pick_connect' }))}
                  disabled={pNodes.length < 2}
                  className="flex items-center gap-2 px-2.5 py-2.5 rounded-xl border-2 border-dashed border-purple-200 hover:border-purple-400 hover:bg-purple-50/50 transition-all group text-left disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <div className="w-7 h-7 rounded-lg bg-purple-100 flex items-center justify-center text-purple-600 group-hover:bg-purple-200 transition-colors text-sm">🔀</div>
                  <div>
                    <div className="text-[12px] font-bold text-slate-700 group-hover:text-purple-600">+ Branch</div>
                    <div className="text-[8px] text-slate-400">Split / parallel path</div>
                  </div>
                </button>
              </div>

              {/* Join hint — if there's a join node missing a second input */}
              {joinNodes.map(jn => {
                const inputCount = pEdges.filter(e => e.to === jn.id).length;
                if (inputCount < 2) {
                  const available = pNodes.filter(n => n.id !== jn.id && !pEdges.some(e => e.from === n.id && e.to === jn.id));
                  return (
                    <div key={jn.id} className="mt-2 p-2 rounded-lg border border-orange-200 bg-orange-50/50">
                      <div className="text-[12px] font-semibold text-orange-700 mb-1">🔗 Join needs a second input</div>
                      <div className="text-[9px] text-orange-600 mb-1.5">"{jn.label}" has {inputCount} input — joins typically need 2</div>
                      <div className="flex flex-wrap gap-1">
                        {available.filter(n => n.category !== 'output').map(n => (
                          <button key={n.id} onClick={() => handleJoinSecondInput(jn.id, n.id)}
                            className="text-[9px] px-2 py-1 rounded-lg bg-white border border-orange-200 text-orange-700 hover:bg-orange-100 transition-colors font-medium">
                            + {n.label.split(':')[0]}
                          </button>
                        ))}
                        <button onClick={() => setGuided(g => ({ ...g, phase: 'pick_source', connectFrom: null, joinTarget: jn.id }))}
                          className="text-[9px] px-2 py-1 rounded-lg bg-white border border-blue-200 text-blue-600 hover:bg-blue-50 transition-colors font-medium">
                          + New Source
                        </button>
                      </div>
                    </div>
                  );
                }
                return null;
              })}

              {/* Build button — visible when we have at least a source + output */}
              {sources.length > 0 && (
                <div className="flex gap-2 mt-3">
                  {outputs.length > 0 ? (
                    <button onClick={() => setGuided(g => ({ ...g, phase: 'review' }))}
                      className="flex-1 py-2.5 rounded-xl text-[12px] font-bold text-white shadow-md hover:shadow-lg transition-all flex items-center justify-center gap-2"
                      style={{ background: 'linear-gradient(135deg, #6366f1, #4f46e5)' }}
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" /></svg>
                      Review & Build ({pNodes.length} nodes)
                    </button>
                  ) : (
                    <div className="flex-1 py-2 rounded-xl text-[12px] text-center text-slate-400 border border-dashed border-slate-200">
                      Add at least one output to build
                    </div>
                  )}
                </div>
              )}
            </>
          )}

          {/* ── PICK SOURCE ── */}
          {phase === 'pick_source' && (
            <>
              <div className="text-sm font-semibold text-slate-700 mb-0.5">
                {sources.length === 0 ? 'What data source do you want to use?' : 'Add another data source'}
              </div>
              <div className="text-[12px] text-slate-400 mb-1">Pick the input for your pipeline</div>
              <GuidedPickCard items={GUIDED_SOURCES} onPick={(item) => handlePickType(item, 'source')} />
              {pNodes.length > 0 && (
                <button onClick={() => setGuided(g => ({ ...g, phase: 'overview' }))}
                  className="mt-2 text-[12px] text-slate-400 hover:text-slate-600 transition-colors font-medium">← Back to overview</button>
              )}
            </>
          )}

          {/* ── CONFIG SOURCE ── */}
          {phase === 'config_source' && (() => {
            const meta = GUIDED_SOURCES.find(s => s.type === currentType);
            return meta ? (
              <>
                <div className="text-sm font-semibold text-slate-700 mb-0.5 flex items-center gap-1.5">
                  <span className="w-5 h-5 rounded flex items-center justify-center text-white text-[12px]" style={{ background: meta.color }}>{meta.icon}</span>
                  Configure {meta.label}
                  {guided.joinTarget && <span className="text-[9px] text-orange-500 font-medium ml-1">(→ Join)</span>}
                </div>
                <GuidedConfigForm fields={meta.fields} onSubmit={handleConfig}
                  onBack={() => setGuided(g => ({ ...g, phase: pNodes.length > 0 ? 'overview' : 'pick_source', currentType: undefined, joinTarget: undefined }))}
                  submitLabel="Add Source"
                />
              </>
            ) : null;
          })()}

          {/* ── PICK CONNECT FROM (choose parent node) ── */}
          {phase === 'pick_connect' && (
            <>
              <div className="text-sm font-semibold text-slate-700 mb-0.5">Connect from which node?</div>
              <div className="text-[12px] text-slate-400 mb-1">Pick the upstream node to connect from</div>
              <NodeSelector nodes={connectableNodes} onSelect={(id) => {
                const targetPhase = guided.currentType === '__output__' ? 'pick_output' : 'pick_transform';
                addGuidedMsg('user', `Connect from: ${pNodes.find(n => n.id === id)?.label || id}`);
                setGuided(g => ({ ...g, phase: targetPhase, connectFrom: id, currentType: targetPhase === 'pick_output' ? undefined : g.currentType }));
              }} label="Available nodes" />
              <button onClick={() => setGuided(g => ({ ...g, phase: 'overview', currentType: undefined }))}
                className="mt-2 text-[12px] text-slate-400 hover:text-slate-600 transition-colors font-medium">← Back to overview</button>
            </>
          )}

          {/* ── PICK TRANSFORM ── */}
          {phase === 'pick_transform' && (
            <>
              <div className="text-sm font-semibold text-slate-700 mb-0.5">What transformation do you need?</div>
              <div className="text-[12px] text-slate-400 mb-1">
                {connectFrom && `Connecting from: ${pNodes.find(n => n.id === connectFrom)?.label || ''}`}
              </div>
              <GuidedPickCard items={GUIDED_TRANSFORMS} onPick={(item) => handlePickType(item, 'transform')} />
              <button onClick={() => setGuided(g => ({ ...g, phase: 'overview', connectFrom: null }))}
                className="mt-2 text-[12px] text-slate-400 hover:text-slate-600 transition-colors font-medium">← Back to overview</button>
            </>
          )}

          {/* ── CONFIG TRANSFORM ── */}
          {phase === 'config_transform' && (() => {
            const meta = GUIDED_TRANSFORMS.find(s => s.type === currentType);
            return meta ? (
              <>
                <div className="text-sm font-semibold text-slate-700 mb-0.5 flex items-center gap-1.5">
                  <span className="w-5 h-5 rounded flex items-center justify-center text-white text-[12px]" style={{ background: meta.color }}>{meta.icon}</span>
                  Configure {meta.label}
                </div>
                <div className="text-[12px] text-slate-400 mb-1">{meta.desc}</div>
                <GuidedConfigForm
                  fields={meta.fields}
                  onSubmit={handleConfig}
                  onBack={() => setGuided(g => ({ ...g, phase: 'pick_transform', currentType: undefined }))}
                  submitLabel={`Add ${meta.label}`}
                />
              </>
            ) : null;
          })()}

          {/* ── PICK OUTPUT ── */}
          {phase === 'pick_output' && (
            <>
              <div className="text-sm font-semibold text-slate-700 mb-0.5">Where should the output go?</div>
              <div className="text-[12px] text-slate-400 mb-1">
                {connectFrom && `From: ${pNodes.find(n => n.id === connectFrom)?.label || ''}`}
              </div>
              <GuidedPickCard items={GUIDED_OUTPUTS} onPick={(item) => handlePickType(item, 'output')} />
              <button onClick={() => setGuided(g => ({ ...g, phase: 'overview', connectFrom: null }))}
                className="mt-2 text-[12px] text-slate-400 hover:text-slate-600 transition-colors font-medium">← Back to overview</button>
            </>
          )}

          {/* ── CONFIG OUTPUT ── */}
          {phase === 'config_output' && (() => {
            const meta = GUIDED_OUTPUTS.find(s => s.type === currentType);
            return meta ? (
              <>
                <div className="text-sm font-semibold text-slate-700 mb-0.5 flex items-center gap-1.5">
                  <span className="w-5 h-5 rounded flex items-center justify-center text-white text-[12px]" style={{ background: meta.color }}>{meta.icon}</span>
                  Configure {meta.label} Output
                </div>
                <GuidedConfigForm
                  fields={meta.fields}
                  onSubmit={handleConfig}
                  onBack={() => setGuided(g => ({ ...g, phase: 'pick_output', currentType: undefined }))}
                  submitLabel="Set Output"
                />
              </>
            ) : null;
          })()}

          {/* ── REVIEW ── */}
          {phase === 'review' && (
            <>
              <div className="text-sm font-semibold text-slate-700 mb-0.5">Review Your Pipeline</div>
              <div className="text-[12px] text-slate-400 mb-1">
                {pNodes.length} nodes · {pEdges.length} connections
                {sources.length > 1 ? ' · Multi-source' : ''}
                {outputs.length > 1 ? ' · Multi-output' : ''}
              </div>
              <MiniPipelinePreview pNodes={pNodes} pEdges={pEdges} />

              {/* Node list */}
              <div className="mt-2 space-y-1">
                {pNodes.map((n, i) => {
                  const meta = findGuidedMeta(n.type);
                  const inEdges = pEdges.filter(e => e.to === n.id);
                  const outEdgeCount = pEdges.filter(e => e.from === n.id).length;
                  return (
                    <div key={n.id} className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg bg-white border border-slate-200 shadow-sm">
                      <span className="w-5 h-5 rounded flex items-center justify-center text-white text-[9px] font-bold shrink-0" style={{ background: meta?.color || '#94a3b8' }}>{meta?.icon || '⬜'}</span>
                      <div className="flex-1 min-w-0">
                        <div className="text-[12px] font-semibold text-slate-700 truncate">{n.label}</div>
                        <div className="text-[8px] text-slate-400 truncate">
                          {Object.entries(n.params).filter(([,v]) => v).map(([k,v]) => `${k}: ${v}`).join(' · ') || n.type}
                        </div>
                      </div>
                      <div className="flex items-center gap-1 shrink-0">
                        {inEdges.length > 0 && <span className="text-[8px] text-indigo-400">{inEdges.length}↓</span>}
                        {outEdgeCount > 0 && <span className="text-[8px] text-emerald-400">{outEdgeCount}→</span>}
                      </div>
                    </div>
                  );
                })}
              </div>

              <div className="flex gap-2 mt-3">
                <button onClick={handleBuildPipeline}
                  className="flex-1 py-2.5 rounded-xl text-[12px] font-bold text-white shadow-md hover:shadow-lg transition-all flex items-center justify-center gap-2"
                  style={{ background: 'linear-gradient(135deg, #6366f1, #4f46e5)' }}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></svg>
                  Build Pipeline
                </button>
                <button onClick={() => setGuided(g => ({ ...g, phase: 'overview' }))}
                  className="px-4 py-2.5 rounded-xl text-sm font-medium text-slate-400 hover:text-slate-600 hover:bg-slate-100 transition-colors border border-slate-200"
                >
                  Edit
                </button>
              </div>
            </>
          )}

          {/* Footer: Guided Builder label + cancel */}
          <div className="mt-3 pt-2 border-t border-indigo-100 flex items-center justify-between">
            <span className="text-[9px] text-indigo-300 font-medium">Guided Builder</span>
            <button onClick={cancelGuided} className="text-[9px] text-slate-400 hover:text-red-400 transition-colors font-medium">Cancel</button>
          </div>
        </div>
      </div>
    );
  };

  /* ── Render ── */
  return (
    <div
      data-fpulse-panel="chat"
      className={`flex flex-col shrink-0 border-l overflow-hidden relative ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-slate-50 border-pipe-200'}`}
      style={{ width: panelWidth }}
    >
      {/* Resize handle — 4px-wide invisible strip on the LEFT edge
          because the chat now sits on the right side of the editor.
          Drag the handle LEFT to grow the panel, RIGHT to shrink. */}
      <div
        onPointerDown={onResizeStart}
        onPointerMove={onResizeMove}
        onPointerUp={onResizeEnd}
        onPointerCancel={onResizeEnd}
        className="absolute left-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-indigo-400/40 transition-colors z-10"
        title="Drag to resize"
      />
      {/* Header — accurate name + capability subline */}
      <div className="px-4 py-3 border-b border-slate-200/60 shrink-0" style={{ background: dark ? '#0f1726' : 'linear-gradient(135deg, #f8fafc, #eef2ff)' }}>
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-xl flex items-center justify-center shadow-sm" style={{ background: 'linear-gradient(135deg, #6366f1, #4f46e5)' }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
            </svg>
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-bold text-slate-800">Assistant</div>
            <div className="text-[12px] text-slate-500">Build · Explain · Fix</div>
          </div>
          {aiAvailable !== null && (
            <span
              className={`text-[9px] px-2 py-1 rounded-full font-bold cursor-help ${
                aiAvailable ? 'bg-emerald-50 text-emerald-600 border border-emerald-200' : 'bg-slate-50 text-slate-500 border border-slate-200'
              }`}
              title={
                aiAvailable
                  ? 'AI provider connected — answers use your configured LLM (Account → AI Provider).'
                  : 'No AI provider — falling back to deterministic rule-based planner. Configure a provider in Account → AI Provider for richer answers.'
              }
            >
              {aiAvailable ? '\u26A1 AI' : '\u{1F4D0} Rules'}
            </span>
          )}
          {/* Hide / collapse button */}
          <button
            onClick={() => setChatOpen(false)}
            className={`ml-1 w-7 h-7 rounded-lg flex items-center justify-center transition-colors ${
              dark ? 'hover:bg-white/10 text-slate-400' : 'hover:bg-slate-200/60 text-slate-400 hover:text-slate-600'
            }`}
            title="Hide assistant panel (toggle from toolbar)"
          >
            {/* Chevron points RIGHT — panel sits on the right edge, so
                "collapse" means it folds back rightward. Mirrors the
                rail's expand-arrow which points LEFT. */}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="9 18 15 12 9 6" />
              <polyline points="9 18 15 12 9 6" />
            </svg>
          </button>
        </div>
      </div>

      {/* Templates + Guided Builder button (when canvas empty) */}
      {showTemplates && (
        <div className="p-3 border-b border-slate-100 shrink-0" style={{ background: dark ? '#0f1726' : 'linear-gradient(180deg, #fafbff, white)' }}>
          <div className="text-sm uppercase tracking-wider text-slate-600 mb-2.5 font-bold flex items-center gap-1.5">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></svg>
            Quick Start
          </div>
          <div className="grid grid-cols-2 gap-2">
            {TEMPLATES.map((t) => (
              <button
                key={t.key}
                onClick={() => useTemplate(t.key)}
                className="text-left p-2.5 rounded-xl border border-slate-200 hover:border-indigo-300 hover:shadow-md transition-all group bg-white"
              >
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-base">{t.icon}</span>
                  <span className="text-[12px] font-bold text-slate-800 group-hover:text-indigo-700 transition-colors">{t.label}</span>
                </div>
                <div className="text-sm text-slate-600 leading-snug">{t.desc}</div>
              </button>
            ))}
          </div>
          {/* Guided Builder CTA */}
          <button
            onClick={startGuidedMode}
            className="mt-2.5 w-full flex items-center gap-3 p-3 rounded-xl border-2 border-dashed border-indigo-200 hover:border-indigo-400 hover:bg-indigo-50/50 transition-all group"
          >
            <div className="w-9 h-9 rounded-xl flex items-center justify-center shrink-0 shadow-sm" style={{ background: 'linear-gradient(135deg, #818cf8, #6366f1)' }}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
              </svg>
            </div>
            <div className="text-left">
              <div className="text-[12px] font-bold text-slate-800 group-hover:text-indigo-700 transition-colors">Guided Builder</div>
              <div className="text-sm text-slate-600 leading-snug">Step-by-step wizard &mdash; pick source, transforms, output</div>
            </div>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#818cf8" strokeWidth="2.5" className="shrink-0 ml-auto group-hover:stroke-indigo-600 transition-colors">
              <path d="M5 12h14m-7-7 7 7-7 7" />
            </svg>
          </button>
        </div>
      )}

      {/* Messages area */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-3 space-y-3">
        {/* Empty-state — concrete clickable prompts instead of marketing copy.
            Each one is a one-tap shortcut to a real assistant capability so the
            user discovers what the panel actually does without reading docs.
            Sizes bumped Apr 22 after readability feedback. */}
        {chatMessages.length === 0 && !guided.active && (
          <div className="py-3 px-1">
            <div className="text-[12px] font-bold text-slate-700 mb-1">How can I help?</div>
            <div className="text-sm text-slate-600 mb-2.5 leading-snug">Pick one to get started, or describe your own pipeline below.</div>
            <div className="space-y-1.5">
              {[
                {
                  label: 'Build a CSV \u2192 Postgres pipeline',
                  hint: 'Generates a 3-node pipeline you can edit',
                  prompt: 'Build a pipeline that reads a CSV file and loads it into a Postgres table',
                  icon: (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></svg>
                  ),
                },
                {
                  label: nodes.length > 0 ? 'Explain this canvas' : 'Explain pipeline concepts',
                  hint: nodes.length > 0 ? 'Walk me through what each node does' : 'Sources, transforms, sinks, edges',
                  prompt: nodes.length > 0 ? 'Explain what this pipeline does, node by node' : 'Explain the basic concepts: sources, transforms, sinks, and how edges connect them',
                  icon: (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
                  ),
                },
                {
                  label: nodes.length > 0 ? "What's wrong with my pipeline?" : 'Show me what slash-commands exist',
                  hint: nodes.length > 0 ? 'Validate config + suggest fixes' : 'Type / in the input to see them all',
                  prompt: nodes.length > 0 ? 'Check this pipeline for misconfigured nodes, missing connections, or invalid params' : 'List the slash-command shortcuts I can type to add nodes (e.g. /source, /filter, /join)',
                  icon: (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2L2 7l10 5 10-5-10-5z" /><polyline points="2 17 12 22 22 17" /><polyline points="2 12 12 17 22 12" /></svg>
                  ),
                },
              ].map((s) => (
                <button
                  key={s.label}
                  onClick={() => handleSuggestion(s.prompt)}
                  className="w-full text-left p-3 rounded-xl border border-slate-200 hover:border-indigo-300 hover:bg-indigo-50/40 hover:shadow-sm transition-all group bg-white"
                >
                  <div className="flex items-start gap-2.5">
                    <span className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0 bg-indigo-50 text-indigo-600 group-hover:bg-indigo-100">
                      {s.icon}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="text-[12px] font-bold text-slate-800 group-hover:text-indigo-700 leading-snug">{s.label}</div>
                      <div className="text-sm text-slate-600 mt-0.5 leading-snug">{s.hint}</div>
                    </div>
                  </div>
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Existing chat messages */}
        {chatMessages.map((msg, i) => {
          const actions = msg.role === 'assistant' ? getActionsForMessage(msg.content, nodes) : [];
          const isUser = msg.role === 'user';
          const isLastAssistant = !isUser && i === chatMessages.length - 1;
          // Phase 3.1 (May 18 2026) — when the backend returned a
          // structured clarification, render <ClarifyCard /> instead
          // of the plain markdown. Only on the LATEST assistant
          // message so prior cards in history aren't re-interactive.
          const clarify = (msg as any).clarification;
          if (
            !isUser
            && isLastAssistant
            && clarify
            && Array.isArray(clarify.questions)
            && clarify.questions.length > 0
          ) {
            return (
              <div key={i} className="ml-8">
                <ClarifyCard
                  payload={clarify}
                  onSubmit={(answerText) => {
                    // Send the assembled answer string as a normal
                    // chat message. The backend's clarify_to_template
                    // bridge will parse it, match a template, and
                    // populate placeholders.
                    sendChat(answerText);
                  }}
                />
              </div>
            );
          }
          return (
            <div key={i}>
              <div className={`flex gap-2 ${isUser ? 'justify-end' : 'justify-start'}`}>
                {!isUser && (
                  <div className="w-6 h-6 rounded-lg flex items-center justify-center shrink-0 mt-0.5" style={{ background: 'linear-gradient(135deg, #6366f1, #4f46e5)' }}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></svg>
                  </div>
                )}
                <div className={`max-w-[85%] px-3.5 py-2.5 text-sm leading-relaxed ${
                  isUser ? 'bg-indigo-600 text-white rounded-2xl rounded-br-md shadow-sm' : 'bg-slate-50 text-slate-600 rounded-2xl rounded-bl-md border border-slate-200'
                }`}>
                  {msg.content.split('\n').map((line, j) => (
                    <span key={j}>
                      {line.split(/(\*\*.*?\*\*)/g).map((part, k) =>
                        part.startsWith('**') && part.endsWith('**') ? (
                          <strong key={k} className={isUser ? 'text-white' : 'text-slate-700'}>{part.slice(2, -2)}</strong>
                        ) : part.startsWith('*') && part.endsWith('*') ? (
                          <em key={k}>{part.slice(1, -1)}</em>
                        ) : (
                          <span key={k}>{part}</span>
                        ),
                      )}
                      {j < msg.content.split('\n').length - 1 && <br />}
                    </span>
                  ))}
                </div>
                {isUser && (
                  <div className="w-6 h-6 rounded-lg flex items-center justify-center shrink-0 mt-0.5 bg-indigo-100 text-indigo-600 text-[9px] font-bold">U</div>
                )}
              </div>
              {/* Tool-step trace — operator-console signal that the reply
                  was grounded in real workspace state via tool calls.
                  Only rendered when the assistant message came from the
                  agent loop (messages have a `tool_steps` array). */}
              {!isUser && Array.isArray((msg as any).tool_steps) && (msg as any).tool_steps.length > 0 && (
                <div className="ml-8 mt-1 flex flex-wrap gap-1.5 items-center">
                  <span className="text-xs text-slate-400 font-medium">via</span>
                  {((msg as any).tool_steps as Array<{ tool_name: string; outcome: string; latency_ms?: number }>).map((s, idx) => {
                    const ok = s.outcome === 'success';
                    return (
                      <span
                        key={idx}
                        className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-xs font-medium border ${
                          ok
                            ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                            : 'bg-amber-50 text-amber-700 border-amber-200'
                        }`}
                        title={`${s.outcome}${s.latency_ms ? ` · ${s.latency_ms}ms` : ''}`}
                      >
                        <span className="font-mono">{s.tool_name.replace(/_/g, ' ')}</span>
                        {ok ? (
                          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3"><polyline points="20 6 9 17 4 12" /></svg>
                        ) : (
                          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                        )}
                      </span>
                    );
                  })}
                </div>
              )}
              {actions.length > 0 && (
                <div className="flex gap-1.5 mt-2 ml-8 flex-wrap">
                  {actions.map((a, j) => (
                    <button key={j} onClick={() => handleSuggestion(a.text)}
                      className="text-[12px] px-2.5 py-1.5 rounded-lg bg-indigo-50 text-indigo-600 border border-indigo-200 hover:bg-indigo-100 hover:border-indigo-300 transition-all font-semibold shadow-sm"
                    >{a.label}</button>
                  ))}
                </div>
              )}
            </div>
          );
        })}

        {/* Guided chat conversation bubbles (user selections appear as messages) */}
        {guided.active && guidedChat.map((msg, i) => {
          const isUser = msg.role === 'user';
          return (
            <div key={`gc-${i}`} className={`flex gap-2 ${isUser ? 'justify-end' : 'justify-start'}`}>
              {!isUser && (
                <div className="w-6 h-6 rounded-lg flex items-center justify-center shrink-0 mt-0.5" style={{ background: 'linear-gradient(135deg, #6366f1, #4f46e5)' }}>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></svg>
                </div>
              )}
              <div className={`max-w-[85%] px-3.5 py-2 text-sm leading-relaxed ${
                isUser ? 'bg-indigo-500 text-white rounded-2xl rounded-br-md shadow-sm' : 'bg-slate-50 text-slate-600 rounded-2xl rounded-bl-md border border-slate-200'
              }`}>
                {msg.content}
              </div>
              {isUser && (
                <div className="w-6 h-6 rounded-lg flex items-center justify-center shrink-0 mt-0.5 bg-indigo-100 text-indigo-600 text-[9px] font-bold">U</div>
              )}
            </div>
          );
        })}

        {/* Active guided phase card (inline in chat) */}
        {guided.active && renderGuidedPhase()}

        {/* Typing indicator */}
        {sending && (
          <div className="flex gap-2 justify-start">
            <div className="w-6 h-6 rounded-lg flex items-center justify-center shrink-0" style={{ background: 'linear-gradient(135deg, #6366f1, #4f46e5)' }}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></svg>
            </div>
            <div className="bg-slate-50 border border-slate-200 px-4 py-3 rounded-2xl rounded-bl-md">
              <div className="flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-bounce" style={{ animationDelay: '0ms' }} />
                <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-bounce" style={{ animationDelay: '150ms' }} />
                <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-bounce" style={{ animationDelay: '300ms' }} />
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Smart Suggestions */}
      {!guided.active && smartSuggestions.length > 0 && chatMessages.length > 2 && !input && (
        <div className="px-3 pb-2 shrink-0">
          <div className="text-[8px] uppercase tracking-widest text-slate-400 mb-1.5 font-bold">Suggestions</div>
          <div className="flex flex-wrap gap-1">
            {smartSuggestions.map((s, i) => (
              <button key={i} onClick={() => handleSuggestion(s.text)}
                className="text-[12px] text-slate-500 hover:text-indigo-600 px-2.5 py-1.5 rounded-lg hover:bg-indigo-50 transition-all border border-slate-200 hover:border-indigo-200 flex items-center gap-1 font-medium"
              ><span>{s.icon}</span>{s.label}</button>
            ))}
          </div>
        </div>
      )}

      {/* Quick Actions — pinned permanently above input. Always visible
          regardless of canvas state so the user can always reach Templates
          / Guided Builder / slash-command discovery in one tap. Hidden
          only while the guided builder phase card is active (the card
          provides its own controls). Sizes bumped Apr 22 — the 10px chips
          looked decorative; 11px font-bold reads as actionable. */}
      {!guided.active && (
        <div className="px-3 pb-2 shrink-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <button
              onClick={startGuidedMode}
              className="text-sm font-bold text-indigo-700 bg-indigo-50 hover:bg-indigo-100 border border-indigo-200 rounded-lg px-2.5 py-1.5 flex items-center gap-1.5 transition-all"
              title="Step-by-step pipeline wizard"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" /></svg>
              Guided
            </button>
            <button
              onClick={() => { setInput('/'); setShowHelperMenu(true); }}
              className="text-sm font-bold text-slate-700 bg-white hover:bg-slate-50 border border-slate-300 hover:border-slate-400 rounded-lg px-2.5 py-1.5 flex items-center gap-1.5 transition-all"
              title="Browse slash-command shortcuts"
            >
              <span className="font-mono text-indigo-600 text-[12px]">/</span>
              Commands
            </button>
            <button
              onClick={() => handleSuggestion('Show me 3 example pipeline prompts I can copy')}
              className="text-sm font-bold text-slate-700 bg-white hover:bg-slate-50 border border-slate-300 hover:border-slate-400 rounded-lg px-2.5 py-1.5 flex items-center gap-1.5 transition-all"
              title="Get example prompts"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" /></svg>
              Examples
            </button>
          </div>
        </div>
      )}

      {/* Helper Menu */}
      {showHelperMenu && (
        <div className="mx-3 mb-2 bg-white border border-slate-200 rounded-xl shadow-xl overflow-hidden max-h-52 overflow-y-auto shrink-0">
          <div className="px-3 py-2 bg-slate-50 border-b border-slate-200 sticky top-0">
            <span className="text-[9px] uppercase tracking-widest text-slate-400 font-bold">Quick Commands</span>
          </div>
          {helperOptions
            .filter((o) => !input || o.cmd.includes(input.replace('/', '')))
            .map((o, i) => (
              <button key={i} onClick={() => { setInput(o.fill); setShowHelperMenu(false); }}
                className="w-full text-left px-3 py-2.5 flex items-center gap-3 hover:bg-indigo-50 transition-colors border-b border-slate-100 last:border-0"
              >
                <span className="text-sm font-mono font-bold text-indigo-500 bg-indigo-50 px-1.5 py-0.5 rounded">{o.cmd}</span>
                <span className="text-[12px] text-slate-500">{o.desc}</span>
              </button>
            ))}
        </div>
      )}

      {/* Input area */}
      <div className="p-3 border-t border-slate-200/60 shrink-0" style={{ background: dark ? '#0f1726' : 'linear-gradient(180deg, white, #fafbff)' }}>
        <div className="flex gap-2 items-end">
          <div className="flex-1 relative">
            <textarea
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                if (e.target.value === '/') setShowHelperMenu(true);
                else if (!e.target.value.startsWith('/')) setShowHelperMenu(false);
              }}
              onKeyDown={handleKeyDown}
              placeholder={guided.active ? 'Type or use the buttons above...' : 'Describe your pipeline...'}
              rows={2}
              className="w-full text-sm text-slate-700 bg-white border border-slate-200 rounded-xl px-3 py-2.5 resize-none focus:outline-none focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 placeholder:text-slate-400 shadow-sm"
            />
          </div>
          <button
            onClick={handleSend}
            disabled={!input.trim() || sending}
            className="w-9 h-9 rounded-xl text-white flex items-center justify-center disabled:opacity-30 transition-all shadow-sm hover:shadow-md shrink-0"
            style={{ background: input.trim() && !sending ? 'linear-gradient(135deg, #6366f1, #4f46e5)' : '#cbd5e1' }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <line x1="22" y1="2" x2="11" y2="13" />
              <polygon points="22 2 15 22 11 13 2 9 22 2" />
            </svg>
          </button>
        </div>
        <div className="flex items-center gap-1.5 mt-2 px-1 flex-wrap">
          <kbd className="text-[12px] bg-slate-100 text-slate-700 px-1.5 py-0.5 rounded font-mono border border-slate-300 font-semibold">Enter</kbd>
          <span className="text-sm text-slate-600">send</span>
          <span className="text-sm text-slate-400 mx-1">&middot;</span>
          <kbd className="text-[12px] bg-slate-100 text-slate-700 px-1.5 py-0.5 rounded font-mono border border-slate-300 font-semibold">Shift+Enter</kbd>
          <span className="text-sm text-slate-600">new line</span>
          <span className="ml-auto" />
          {/* Deep-mode toggle (2026-05-22). Off by default; opt in for
              hard reasoning questions about pipelines. Slower per turn
              (cloud ~30-60 s, local Ollama 2-5 min) but skips fast-lane
              shortcuts and widens the context budget. */}
          <label
            className="flex items-center gap-1 text-xs text-slate-600 cursor-pointer select-none ml-1"
            title={
              deepMode
                ? 'Deep mode is ON — slower per turn but reasons over the full pipeline context.'
                : 'Turn on deep mode for hard reasoning questions. Slower per turn.'
            }
          >
            <input
              type="checkbox"
              checked={deepMode}
              onChange={(e) => setDeepMode(e.target.checked)}
              className="accent-indigo-500"
            />
            Deep
            {deepMode && (
              <span className="ml-0.5 px-1 py-0.5 text-[9px] font-bold uppercase tracking-wide rounded bg-amber-100 text-amber-800 border border-amber-200">
                Slow
              </span>
            )}
          </label>
        </div>
      </div>
    </div>
  );
}
