/**
 * RuntimeLineagePanel (L1.1 / L2 frontend, 2026-06-08)
 *
 * Shows what ACTUALLY ran on a given execution: per-step columns in/out,
 * rows in/out, and timing — distinct from the design-time lineage graph.
 * Self-contained + prop-driven: pass a `runId` and it fetches
 * `GET /api/lineage/runs/{run_id}`. Mount it in the execution-detail
 * drawer (a one-line follow-up); the component itself is verified by
 * transpile + logic review here.
 */
import { useEffect, useState } from 'react';
import { api } from '../../api/client';

interface StepRun {
  id: string;
  step_id: string;
  step_label: string;
  step_type: string;
  columns_in: string[];
  columns_out: string[];
  rows_in: number;
  rows_out: number;
  started_at: number;
  completed_at: number;
  error: string;
}

interface Props {
  runId: string;
  dark?: boolean;
}

function _durationMs(sr: StepRun): number | null {
  if (!sr.started_at || !sr.completed_at) return null;
  return Math.max(0, Math.round((sr.completed_at - sr.started_at) * 1000));
}

export default function RuntimeLineagePanel({ runId, dark = false }: Props) {
  const [rows, setRows] = useState<StepRun[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (!runId) return;
    setLoading(true);
    setError(null);
    api
      .getRuntimeLineage(runId)
      .then((data) => {
        if (!cancelled) setRows(data.step_runs || []);
      })
      .catch((e: any) => {
        if (!cancelled) setError(e?.message || 'Failed to load runtime lineage');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  const sub = dark ? 'text-slate-400' : 'text-slate-500';

  if (loading) {
    return <div className={`text-xs ${sub}`}>Loading runtime lineage…</div>;
  }
  if (error) {
    return <div className="text-xs text-red-500">{error}</div>;
  }
  if (rows.length === 0) {
    return (
      <div className={`text-xs ${sub}`}>
        No runtime lineage recorded for this run. (Lineage is emitted for
        successful steps; older runs predate it.)
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className={`text-[10px] uppercase tracking-wider ${sub}`}>
        {rows.length} step{rows.length === 1 ? '' : 's'} · what actually ran
      </div>
      {rows.map((sr) => {
        const dur = _durationMs(sr);
        return (
          <div
            key={sr.id}
            className={`px-2.5 py-2 rounded border text-xs ${
              sr.error
                ? dark
                  ? 'bg-red-500/10 border-red-500/20'
                  : 'bg-red-50 border-red-100'
                : dark
                ? 'bg-slate-800 border-slate-700'
                : 'bg-white border-slate-200'
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className={`font-semibold ${dark ? 'text-slate-200' : 'text-slate-700'}`}>
                {sr.step_label || sr.step_id}
              </span>
              <span className={`font-mono text-[10px] ${sub}`}>{sr.step_type}</span>
            </div>
            <div className={`mt-1 flex flex-wrap gap-x-4 gap-y-0.5 ${sub}`}>
              <span>rows out: <strong>{sr.rows_out.toLocaleString()}</strong></span>
              {sr.rows_in > 0 && <span>rows in: {sr.rows_in.toLocaleString()}</span>}
              {dur !== null && <span>{dur} ms</span>}
              {sr.columns_out.length > 0 && (
                <span>cols: {sr.columns_out.slice(0, 6).join(', ')}{sr.columns_out.length > 6 ? '…' : ''}</span>
              )}
            </div>
            {sr.error && (
              <div className="mt-1 font-mono text-[10px] text-red-500 break-all">{sr.error}</div>
            )}
          </div>
        );
      })}
    </div>
  );
}
