import { useEffect, useRef, useState } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import { api } from '../api/client';

/**
 * Live preview for expression-style `{{ }}` expressions (C4, 2026-06-15).
 *
 * Renders nothing unless `value` contains `{{`. Otherwise it debounces and
 * resolves the expression server-side via `/api/expression/preview` — the SAME
 * resolver the executor runs per-step — so the in-editor preview can never
 * drift from runtime behaviour.
 *
 * Context fed to the resolver, read FRESH from the store at fire time (so a
 * test-run that populates sample data refreshes the preview without prop
 * churn):
 *   - `$json`       → the selected node's first upstream sample row
 *   - `$('Label')`  → every node's sample rows (after a test/run), by label
 *   - `$now`, `$itemIndex` → always available
 *   - `$vars`       → unknown until runtime; expressions reading it show a hint
 */
export default function ExpressionPreview({ value }: { value: string }) {
  const selectedNodeId = useWorkflowStore((s) => s.selectedNodeId);
  const [state, setState] = useState<{
    status: 'idle' | 'loading' | 'ok' | 'error';
    result?: string;
    type?: string;
    error?: string;
  }>({ status: 'idle' });
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const hasExpr = typeof value === 'string' && value.includes('{{');

  useEffect(() => {
    if (!hasExpr) {
      setState({ status: 'idle' });
      return;
    }
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      const { edges, nodes, stepResults } = useWorkflowStore.getState();
      // $json — the first upstream node's first sample row.
      const upstreamId = edges.find((e) => e.target === selectedNodeId)?.source;
      const sampleRow = (upstreamId && stepResults[upstreamId]?.sample_data?.[0]) || {};
      // $('Label') refs — every node's sample rows (capped), keyed by label.
      const nodeSamples: Record<string, any[]> = {};
      for (const n of nodes) {
        const sr = stepResults[n.id];
        const label = (n.data as any)?.label;
        if (label && sr?.sample_data?.length) {
          nodeSamples[label] = sr.sample_data.slice(0, 5);
        }
      }
      setState((s) => ({ ...s, status: 'loading' }));
      try {
        const res = await api.previewExpression({
          expression: value,
          sample_row: sampleRow,
          node_samples: nodeSamples,
        });
        if (res.ok) setState({ status: 'ok', result: res.result, type: res.value_type });
        else setState({ status: 'error', error: res.error });
      } catch (e: any) {
        setState({ status: 'error', error: e?.message || 'preview failed' });
      }
    }, 350);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [value, hasExpr, selectedNodeId]);

  if (!hasExpr) return null;

  return (
    <div className="mt-1 text-[11px] leading-snug" data-testid="expr-preview">
      {state.status === 'loading' && <span className="text-slate-400">Resolving…</span>}
      {state.status === 'ok' && (
        <span className="text-emerald-600">
          <span className="text-slate-400">→ </span>
          <span className="font-mono break-all">
            {state.result === '' ? '(empty)' : state.result}
          </span>
          {state.type && state.type !== 'str' && (
            <span className="text-slate-400"> · {state.type}</span>
          )}
        </span>
      )}
      {state.status === 'error' && <span className="text-amber-600">⚠ {state.error}</span>}
    </div>
  );
}
