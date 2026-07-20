/**
 * TransformHelper — per-node AI strip shown above the SQL/expression input.
 * Step 4d-i of the F-Pulse AI completion arc (basics: Explain + Help me write
 * + Cost of query). Preview-output is deferred to Tier B.
 *
 * Drop-in usage from the SQL/transform config panel:
 *
 *   <TransformHelper
 *     nodeType="transform"
 *     expression={params.expression}
 *     upstreamSchema={upstreamSchema}
 *     upstreamRowCount={lastRunRows}
 *     onAcceptSql={(sql) => setParams({ ...params, expression: sql })}
 *   />
 *
 * Backend: POST /api/ai/transform/{explain,suggest-sql,cost-estimate}
 */

import { useEffect, useState } from 'react';
import { askCopilot } from '../hooks/useAgentChatStore';

interface ColumnDef {
  name: string;
  type: string;
  nullable?: boolean;
}

interface TransformHelperProps {
  nodeType: string;
  expression: string;
  params?: Record<string, unknown>;
  upstreamSchema: ColumnDef[];
  upstreamRowCount?: number;
  /** Called when the user accepts an AI-suggested SQL snippet. */
  onAcceptSql?: (sql: string) => void;
  /** When the expression failed validation, pass the error to enable Fix-error. */
  validationError?: string;
  /** Currently-highlighted selection from the editor (Explain selection). */
  selection?: string;
}

interface ExplainResp {
  explanation: string;
  ai_powered: boolean;
}

interface SuggestResp {
  sql: string;
  explanation: string;
  ai_powered: boolean;
}

interface CostResp {
  rough_rows_out: number;
  estimated_ms: number;
  cost_band: 'low' | 'medium' | 'high';
  notes: string[];
}

const COST_TONES: Record<string, string> = {
  low: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  medium: 'bg-amber-50 text-amber-800 ring-amber-200',
  high: 'bg-red-50 text-red-700 ring-red-200',
};

function _headers(): Record<string, string> {
  const token = localStorage.getItem('fpulse_token') || '';
  const ws = localStorage.getItem('fpulse_workspace_id') || 'default';
  const h: Record<string, string> = { 'Content-Type': 'application/json', 'X-Workspace-Id': ws };
  if (token) h['Authorization'] = `Bearer ${token}`;
  return h;
}

export default function TransformHelper({
  nodeType,
  expression,
  params = {},
  upstreamSchema,
  upstreamRowCount,
  onAcceptSql,
  validationError,
  selection,
}: TransformHelperProps) {
  const [explainResp, setExplainResp] = useState<ExplainResp | null>(null);
  const [explainLoading, setExplainLoading] = useState(false);
  const [explainError, setExplainError] = useState<string | null>(null);

  const [intent, setIntent] = useState('');
  const [suggestion, setSuggestion] = useState<SuggestResp | null>(null);
  const [suggestLoading, setSuggestLoading] = useState(false);
  const [suggestError, setSuggestError] = useState<string | null>(null);

  const [cost, setCost] = useState<CostResp | null>(null);

  const [selectionExplain, setSelectionExplain] = useState<ExplainResp | null>(null);
  const [selectionLoading, setSelectionLoading] = useState(false);

  const [fix, setFix] = useState<{
    fixed_expression: string;
    explanation: string;
    diff_added_lines: string[];
    diff_removed_lines: string[];
    ai_powered: boolean;
  } | null>(null);
  const [fixLoading, setFixLoading] = useState(false);
  const [fixError, setFixError] = useState<string | null>(null);

  // Cost estimate is cheap + free, so debounce-fire it on every expression change.
  useEffect(() => {
    if (!expression || expression.length < 4) {
      setCost(null);
      return;
    }
    const id = window.setTimeout(async () => {
      try {
        const res = await fetch('/api/ai/transform/cost-estimate', {
          method: 'POST',
          headers: _headers(),
          body: JSON.stringify({
            sql: expression,
            upstream_row_count: upstreamRowCount || 0,
            upstream_column_count: upstreamSchema.length,
          }),
        });
        if (!res.ok) throw new Error(String(res.status));
        setCost((await res.json()) as CostResp);
      } catch {
        setCost(null);
      }
    }, 400);
    return () => window.clearTimeout(id);
  }, [expression, upstreamRowCount, upstreamSchema.length]);

  const runExplain = async () => {
    setExplainLoading(true);
    setExplainError(null);
    setExplainResp(null);
    try {
      const res = await fetch('/api/ai/transform/explain', {
        method: 'POST',
        headers: _headers(),
        body: JSON.stringify({
          node_type: nodeType,
          expression,
          params,
          upstream_schema: upstreamSchema,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setExplainResp((await res.json()) as ExplainResp);
    } catch (e) {
      setExplainError(e instanceof Error ? e.message : 'Failed');
    } finally {
      setExplainLoading(false);
    }
  };

  const runExplainSelection = async () => {
    if (!selection || !selection.trim()) return;
    setSelectionLoading(true);
    setSelectionExplain(null);
    try {
      const res = await fetch('/api/ai/transform/explain-selection', {
        method: 'POST',
        headers: _headers(),
        body: JSON.stringify({
          selection,
          full_expression: expression,
          upstream_schema: upstreamSchema,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setSelectionExplain((await res.json()) as ExplainResp);
    } catch {
      // best-effort
    } finally {
      setSelectionLoading(false);
    }
  };

  const runFixError = async () => {
    if (!validationError || !validationError.trim()) return;
    setFixLoading(true);
    setFixError(null);
    setFix(null);
    try {
      const res = await fetch('/api/ai/transform/fix-error', {
        method: 'POST',
        headers: _headers(),
        body: JSON.stringify({
          expression,
          error_message: validationError,
          upstream_schema: upstreamSchema,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setFix(await res.json());
    } catch (e) {
      setFixError(e instanceof Error ? e.message : 'Fix failed');
    } finally {
      setFixLoading(false);
    }
  };

  const runSuggest = async () => {
    if (!intent.trim()) return;
    setSuggestLoading(true);
    setSuggestError(null);
    setSuggestion(null);
    try {
      const res = await fetch('/api/ai/transform/suggest-sql', {
        method: 'POST',
        headers: _headers(),
        body: JSON.stringify({
          natural_language: intent,
          upstream_schema: upstreamSchema,
          table_name: 'src',
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setSuggestion((await res.json()) as SuggestResp);
    } catch (e) {
      setSuggestError(e instanceof Error ? e.message : 'Failed');
    } finally {
      setSuggestLoading(false);
    }
  };

  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 space-y-2 text-[12px]">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="text-xs font-bold uppercase tracking-wider text-slate-500">
            AI helper
          </span>
          {/* Deep-link into the Copilot dock with the active code pre-loaded
              into the chat input. Uses the askCopilot store helper which
              opens the dock + sets pendingInput for AgentChatPanel. */}
          <button
            type="button"
            onClick={() => {
              const langTag = nodeType === 'execute_sql_task' ? 'sql' : 'expression';
              const snippet = expression ? `\n\n\`\`\`${langTag}\n${expression.slice(0, 1500)}\n\`\`\`` : '';
              askCopilot(
                `Help me with this ${langTag === 'sql' ? 'SQL' : 'expression'}:${snippet || '\n(open a SQL/transform node to provide context)'}`
              );
            }}
            className="text-xs font-semibold px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-700 hover:bg-indigo-100 ring-1 ring-indigo-200"
            title="Open the Copilot dock with this code preloaded"
          >
            Ask Copilot →
          </button>
        </div>
        {cost && (
          <span
            className={`px-2 py-0.5 rounded text-xs font-semibold ring-1 ${
              COST_TONES[cost.cost_band] || COST_TONES.low
            }`}
            title={cost.notes.join(' ') || 'Heuristic cost estimate'}
          >
            Cost: {cost.cost_band} · ~{cost.estimated_ms}ms · ~{cost.rough_rows_out.toLocaleString()} rows
          </span>
        )}
      </div>

      {/* Explain */}
      <div className="flex items-start gap-2">
        <button
          type="button"
          onClick={runExplain}
          disabled={explainLoading}
          className="shrink-0 px-2.5 py-1 text-xs font-semibold rounded-md bg-indigo-100 hover:bg-indigo-200 text-indigo-700 disabled:opacity-50"
        >
          {explainLoading ? 'Explaining…' : 'Explain'}
        </button>
        <div className="flex-1 min-w-0">
          {explainError && <div className="text-red-700 text-xs">Explain failed: {explainError}</div>}
          {explainResp && (
            <div className="text-slate-700 leading-snug">
              <span className="text-[9px] font-bold uppercase tracking-wider mr-1.5 text-slate-500">
                {explainResp.ai_powered ? 'AI' : 'Rules'}
              </span>
              {explainResp.explanation}
            </div>
          )}
        </div>
      </div>

      {/* Explain selection — only when there's a current selection */}
      {selection && selection.trim() && (
        <div className="flex items-start gap-2">
          <button
            type="button"
            onClick={runExplainSelection}
            disabled={selectionLoading}
            className="shrink-0 px-2.5 py-1 text-xs font-semibold rounded-md bg-slate-100 hover:bg-slate-200 text-slate-700 disabled:opacity-50"
            title="Explain only the highlighted span"
          >
            {selectionLoading ? 'Explaining…' : 'Explain selection'}
          </button>
          <div className="flex-1 min-w-0">
            <code className="text-xs font-mono bg-slate-200 text-slate-700 px-1 py-0.5 rounded inline-block max-w-full truncate">
              {selection.length > 80 ? selection.slice(0, 80) + '…' : selection}
            </code>
            {selectionExplain && (
              <div className="text-slate-700 leading-snug mt-1">
                <span className="text-[9px] font-bold uppercase tracking-wider mr-1.5 text-slate-500">
                  {selectionExplain.ai_powered ? 'AI' : 'Rules'}
                </span>
                {selectionExplain.explanation}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Fix error — only when there's a validator error */}
      {validationError && (
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={runFixError}
              disabled={fixLoading}
              className="shrink-0 px-2.5 py-1 text-xs font-semibold rounded-md bg-red-100 hover:bg-red-200 text-red-800 disabled:opacity-50"
            >
              {fixLoading ? 'Patching…' : 'Fix error'}
            </button>
            <span className="text-xs text-red-700 truncate" title={validationError}>
              {validationError}
            </span>
          </div>
          {fixError && <div className="text-red-700 text-xs">Fix failed: {fixError}</div>}
          {fix && (
            <div className="rounded-md bg-white border border-slate-200 p-2 space-y-1.5">
              <div className="text-xs font-bold uppercase tracking-wider text-slate-500 flex items-center gap-2">
                <span>{fix.ai_powered ? 'AI patch' : 'No-op patch'}</span>
              </div>
              {fix.diff_removed_lines.length > 0 && (
                <pre className="text-xs font-mono bg-red-50 border border-red-200 px-2 py-1 rounded whitespace-pre-wrap break-words">
                  {fix.diff_removed_lines.map((l) => '- ' + l).join('\n')}
                </pre>
              )}
              {fix.diff_added_lines.length > 0 && (
                <pre className="text-xs font-mono bg-emerald-50 border border-emerald-200 px-2 py-1 rounded whitespace-pre-wrap break-words">
                  {fix.diff_added_lines.map((l) => '+ ' + l).join('\n')}
                </pre>
              )}
              <div className="text-xs text-slate-600">{fix.explanation}</div>
              {onAcceptSql && fix.ai_powered && (
                <button
                  type="button"
                  onClick={() => onAcceptSql(fix.fixed_expression)}
                  className="px-2 py-0.5 text-xs font-semibold rounded-md bg-emerald-600 hover:bg-emerald-700 text-white"
                >
                  Apply patch
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {/* Help me write */}
      <div className="space-y-1">
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={intent}
            onChange={(e) => setIntent(e.target.value)}
            placeholder="Help me write… (e.g. 'top 10 customers by revenue last month')"
            className="flex-1 px-2.5 py-1 text-[12px] rounded-md border border-slate-300 focus:border-indigo-400 focus:outline-none"
          />
          <button
            type="button"
            onClick={runSuggest}
            disabled={suggestLoading || !intent.trim()}
            className="shrink-0 px-2.5 py-1 text-xs font-semibold rounded-md bg-indigo-600 hover:bg-indigo-700 text-white disabled:opacity-50"
          >
            {suggestLoading ? 'Generating…' : 'Suggest'}
          </button>
        </div>
        {suggestError && <div className="text-red-700 text-xs">Suggest failed: {suggestError}</div>}
        {suggestion && (
          <div className="rounded-md bg-white border border-slate-200 p-2 space-y-1.5">
            <div className="text-xs font-bold uppercase tracking-wider text-slate-500 flex items-center gap-2">
              <span>{suggestion.ai_powered ? 'AI suggestion' : 'Default suggestion'}</span>
              {!suggestion.ai_powered && (
                <span className="text-slate-400 normal-case font-normal">
                  (LLM unavailable — used deterministic fallback)
                </span>
              )}
            </div>
            <pre className="text-xs font-mono bg-slate-900 text-emerald-300 px-2 py-1.5 rounded whitespace-pre-wrap break-words">
              {suggestion.sql}
            </pre>
            <div className="text-xs text-slate-600">{suggestion.explanation}</div>
            {onAcceptSql && (
              <button
                type="button"
                onClick={() => onAcceptSql(suggestion.sql)}
                className="px-2 py-0.5 text-xs font-semibold rounded-md bg-emerald-600 hover:bg-emerald-700 text-white"
              >
                Use this
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
