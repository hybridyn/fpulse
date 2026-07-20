import { useState, useRef, useEffect } from 'react';
import { useWorkflowStore, type StepResult } from '../stores/workflowStore';
import { toast } from './Toast';

/**
 * Full-panel Code Editor.
 * Opens as an overlay on top of the canvas when editing a Transform/SQL node.
 * Layout: Schema Browser (left) | Code Editor (center) | Output Preview (right)
 */
export default function CodeEditorPanel() {
  const {
    selectedNodeId, nodes, edges, stepResults,
    updateNodeParams, setCodeEditorOpen, runStep, ensureWorkflow,
  } = useWorkflowStore();

  const node = nodes.find((n) => n.id === selectedNodeId);
  if (!node) return null;

  const params = node.data.params as any;
  const expression = params?.expression || '';

  // Find upstream columns and node info
  const upstreamEdges = edges.filter((e) => e.target === node.id);
  const upstreamColumns: string[] = [];
  const upstreamResults: StepResult[] = [];
  const upstreamNodeInfo: Array<{ id: string; label: string; alias: string; columns: string[] }> = [];
  for (const edge of upstreamEdges) {
    const upNode = nodes.find((n) => n.id === edge.source);
    const upResult = stepResults[edge.source];
    if (upResult?.columns) {
      upstreamColumns.push(...upResult.columns);
      upstreamResults.push(upResult);
    }
    if (upNode) {
      // xyflow's Node.data is typed as Record<string, unknown> — neither
      // `label` nor `stepType` are guaranteed strings at the type level.
      // Coerce to string here so `.toLowerCase()` below is sound. Empty
      // fallback keeps the alias well-formed even on malformed nodes.
      const label = String(upNode.data.label || upNode.data.stepType || '');
      upstreamNodeInfo.push({
        id: upNode.id,
        label,
        alias: label.toLowerCase().replace(/[^a-z0-9_]/g, '_').replace(/^_+|_+$/g, ''),
        columns: upResult?.columns || [],
      });
    }
  }

  const result = selectedNodeId ? stepResults[selectedNodeId] : null;
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [testing, setTesting] = useState(false);
  const [activeTab, setActiveTab] = useState<'input' | 'output'>('input');

  // Auto-focus editor
  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  const insertAtCursor = (text: string) => {
    const el = textareaRef.current;
    if (!el) return;
    const start = el.selectionStart;
    const end = el.selectionEnd;
    const newVal = expression.slice(0, start) + text + expression.slice(end);
    updateNodeParams(node.id, { expression: newVal });
    setTimeout(() => {
      el.focus();
      el.setSelectionRange(start + text.length, start + text.length);
    }, 0);
  };

  const handleTest = async () => {
    setTesting(true);
    try {
      const wfId = await ensureWorkflow();
      if (!wfId) {
        toast.error(
          'Save the pipeline first',
          'Click Save and give the pipeline a name before testing this step.',
        );
        return;
      }
      await runStep(node.id);
      setActiveTab('output');
    } catch (err) {
      console.error(err);
    }
    setTesting(false);
  };

  const SQL_KEYWORDS = [
    'SELECT', 'FROM', 'WHERE', 'GROUP BY', 'ORDER BY', 'HAVING',
    'LIMIT', 'JOIN', 'LEFT JOIN', 'INNER JOIN', 'ON',
    'AS', 'AND', 'OR', 'NOT', 'IN', 'BETWEEN', 'LIKE', 'IS NULL',
    'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
    'COUNT', 'SUM', 'AVG', 'MIN', 'MAX',
    'CAST', 'COALESCE', 'NULLIF',
    'UPPER', 'LOWER', 'TRIM', 'SUBSTR', 'LENGTH', 'REPLACE', 'CONCAT',
    'CURRENT_TIMESTAMP', 'CURRENT_DATE', 'STRFTIME',
    'ROW_NUMBER', 'RANK', 'DENSE_RANK', 'LAG', 'LEAD',
    'OVER', 'PARTITION BY', 'WINDOW',
    'UNION', 'UNION ALL', 'EXCEPT', 'INTERSECT',
    'WITH', 'DISTINCT',
  ];

  const lines = expression.split('\n');

  return (
    <div className="absolute inset-0 z-[55] bg-white flex flex-col">
      {/* Header bar */}
      <div className="h-12 bg-gradient-to-b from-slate-200 to-slate-300 border-b border-slate-400/70 flex items-center px-4 gap-3 shrink-0">
        <button
          onClick={() => setCodeEditorOpen(false)}
          className="w-8 h-8 rounded-lg flex items-center justify-center text-slate-400 hover:text-slate-600 hover:bg-slate-100 transition-colors"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="19" y1="12" x2="5" y2="12" /><polyline points="12 19 5 12 12 5" />
          </svg>
        </button>
        <div
          className="w-7 h-7 rounded-md flex items-center justify-center shadow-sm shrink-0"
          style={{ background: node.data.color as string }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
          </svg>
        </div>
        <div className="min-w-0">
          <span className="text-sm font-bold text-slate-800">{node.data.label as string}</span>
          <span className="text-xs text-slate-400 ml-2 uppercase">
            {(node.data.stepType as string).replace(/_/g, ' ')}
          </span>
        </div>

        <div className="flex-1" />

        <span className="text-[9px] text-slate-300 font-mono mr-2">DuckDB SQL</span>

        <button
          onClick={handleTest}
          disabled={testing}
          className="px-4 py-1.5 text-white text-xs font-bold rounded-lg disabled:opacity-50 transition-all shadow-sm hover:shadow-md flex items-center gap-1.5"
          style={{ background: testing ? '#94a3b8' : 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
        >
          {testing ? (
            <>
              <span className="w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin" />
              Running...
            </>
          ) : (
            <>
              <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" stroke="none">
                <polygon points="5 3 19 12 5 21 5 3" />
              </svg>
              Execute
            </>
          )}
        </button>
      </div>

      {/* Main 3-column layout */}
      <div className="flex-1 flex overflow-hidden">
        {/* LEFT: Schema Browser */}
        <div className="w-56 border-r border-slate-200 bg-slate-50/50 flex flex-col overflow-hidden shrink-0">
          <div className="px-3 py-2.5 border-b border-slate-200 shrink-0">
            <div className="text-xs font-bold text-slate-500 uppercase tracking-wider">Input Schema</div>
          </div>
          <div className="flex-1 overflow-auto p-3">
            {/* Dataset names */}
            {upstreamNodeInfo.length > 0 && (
              <div className="mb-3 space-y-1">
                <div className="text-[9px] font-bold text-blue-500 uppercase tracking-wider mb-1">Datasets</div>
                {upstreamNodeInfo.map((info, idx) => (
                  <button
                    key={info.id}
                    onClick={() => insertAtCursor(idx === 0 ? 'source_table' : info.alias)}
                    className="w-full flex items-center gap-2 px-2 py-1.5 text-left rounded-lg hover:bg-blue-50 border border-transparent hover:border-blue-200 transition-colors group"
                  >
                    <span className={`w-2 h-2 rounded-full shrink-0 ${idx === 0 ? 'bg-blue-500' : 'bg-indigo-400'}`} />
                    <span className="text-xs font-semibold text-blue-700 group-hover:text-blue-900 truncate">{info.label}</span>
                    <code className="ml-auto text-[8px] font-mono text-blue-400 group-hover:text-blue-600">{idx === 0 ? 'source_table' : info.alias}</code>
                  </button>
                ))}
              </div>
            )}

            {/* Columns */}
            {upstreamColumns.length === 0 ? (
              <div className="text-xs text-slate-400 italic">
                Run the upstream node to see columns.
              </div>
            ) : (
              <div className="space-y-1">
                <div className="text-[9px] font-bold text-slate-400 uppercase tracking-wider mb-1">Columns</div>
                {upstreamColumns.map((col) => (
                  <button
                    key={col}
                    onClick={() => insertAtCursor(col)}
                    className="w-full flex items-center gap-2 px-2 py-1.5 text-left rounded-lg hover:bg-pipe-50 hover:border-pipe-200 border border-transparent transition-colors group"
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2" className="shrink-0 group-hover:stroke-pipe-500">
                      <rect x="3" y="3" width="18" height="18" rx="2" /><line x1="3" y1="9" x2="21" y2="9" />
                    </svg>
                    <span className="text-xs font-mono text-slate-600 group-hover:text-pipe-700 truncate">{col}</span>
                  </button>
                ))}
              </div>
            )}

            {/* SQL keywords */}
            <div className="mt-4 pt-3 border-t border-slate-200">
              <div className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">Functions</div>
              <div className="flex flex-wrap gap-1">
                {SQL_KEYWORDS.slice(0, 20).map((kw) => (
                  <button
                    key={kw}
                    onClick={() => insertAtCursor(kw + ' ')}
                    className="text-[8px] px-1.5 py-0.5 bg-pulse-50 text-pulse-700 rounded border border-pulse-200 hover:bg-pulse-100 transition-colors font-mono font-medium"
                  >
                    {kw}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>

        {/* CENTER: Code Editor */}
        <div className="flex-1 flex flex-col overflow-hidden">
          <div className="flex-1 flex overflow-hidden">
            {/* Line numbers */}
            <div className="w-10 bg-slate-50 border-r border-slate-200 flex flex-col items-end pt-3 pr-2 overflow-hidden select-none shrink-0">
              {lines.map((_, i) => (
                <span key={i} className="text-xs text-slate-300 leading-[20px] font-mono">{i + 1}</span>
              ))}
              {lines.length === 0 && <span className="text-xs text-slate-300 leading-[20px] font-mono">1</span>}
            </div>
            {/* Editor */}
            <textarea
              ref={textareaRef}
              value={expression}
              onChange={(e) => updateNodeParams(node.id, { expression: e.target.value })}
              placeholder="SELECT *&#10;FROM source_table&#10;WHERE condition"
              className="flex-1 p-3 text-sm text-slate-800 font-mono leading-[20px] resize-none focus:outline-none bg-white"
              spellCheck={false}
              style={{ tabSize: 2 }}
            />
          </div>

          {/* Bottom hint */}
          <div className="px-4 py-2 bg-slate-50 border-t border-slate-200 flex items-center gap-4 shrink-0">
            <div className="flex items-center gap-1.5 text-xs text-slate-400">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-300"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
              {upstreamNodeInfo.length > 1
                ? <>Datasets: {upstreamNodeInfo.map((u, i) => <code key={u.id} className="px-1 py-0.5 bg-white border border-slate-200 rounded text-pipe-600 mx-0.5">{i === 0 ? 'source_table' : u.alias}</code>)}</>
                : <>Use <code className="px-1 py-0.5 bg-white border border-slate-200 rounded text-pipe-600 mx-0.5">source_table</code> to reference upstream data</>
              }
            </div>
            <div className="flex-1" />
            <span className="text-xs text-slate-300 font-mono">
              {lines.length} line{lines.length !== 1 ? 's' : ''} · {expression.length} chars
            </span>
          </div>
        </div>

        {/* RIGHT: Output Preview */}
        <div className="w-80 border-l border-slate-200 bg-white flex flex-col overflow-hidden shrink-0">
          {/* Tabs */}
          <div className="flex items-center gap-1 px-3 py-2 border-b border-slate-200 shrink-0">
            {(['input', 'output'] as const).map((t) => (
              <button
                key={t}
                onClick={() => setActiveTab(t)}
                className={`px-3 py-1.5 text-xs font-semibold rounded-md transition-colors uppercase tracking-wider ${
                  activeTab === t
                    ? 'bg-pipe-100 text-pipe-700'
                    : 'text-slate-400 hover:text-slate-600 hover:bg-slate-50'
                }`}
              >
                {t}
              </button>
            ))}
          </div>

          {/* Panel content */}
          <div className="flex-1 overflow-auto">
            {activeTab === 'input' ? (
              upstreamResults.length > 0 ? (
                <MiniTable result={upstreamResults[0]} />
              ) : (
                <div className="flex items-center justify-center h-full text-slate-400 text-xs p-4 text-center">
                  Run the upstream node to see input data.
                </div>
              )
            ) : result ? (
              result.status === 'error' ? (
                <div className="p-4 text-red-500 text-xs font-mono whitespace-pre-wrap">{result.error}</div>
              ) : (
                <MiniTable result={result} />
              )
            ) : (
              <div className="flex items-center justify-center h-full text-slate-400 text-xs p-4 text-center">
                Click "Execute" to see output.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function MiniTable({ result }: { result: StepResult }) {
  if (!result.sample_data.length) {
    return <div className="p-4 text-slate-400 text-xs">No data</div>;
  }
  return (
    <div className="overflow-auto h-full">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-pulse-50 z-10">
          <tr>
            {result.columns.map((col) => (
              <th key={col} className="px-2.5 py-1.5 text-left text-[9px] font-semibold text-slate-500 uppercase tracking-wider border-b border-amber-200/30 whitespace-nowrap">
                {col}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {result.sample_data.map((row, i) => (
            <tr key={i} className="hover:bg-slate-50/50 border-b border-slate-100">
              {result.columns.map((col) => (
                <td key={col} className="px-2.5 py-1.5 text-slate-600 whitespace-nowrap max-w-32 truncate font-mono text-xs">
                  {row[col] === null ? <span className="text-slate-300 italic">null</span> : String(row[col])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="px-3 py-2 bg-slate-50 border-t border-slate-200 text-[9px] text-slate-400">
        {result.row_count.toLocaleString()} rows · {result.columns.length} columns · {result.duration_ms}ms
      </div>
    </div>
  );
}
