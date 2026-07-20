/**
 * BackfillModal — kick off a chunked re-execution of a pipeline over a date range.
 *
 * Backed by the POST /api/executions/backfill endpoint. The modal:
 *   • collects start/end dates, window size, cursor param names, concurrency,
 *     on-failure policy, and any extra parameter overrides
 *   • shows the live count of windows the range will produce so the user
 *     doesn't accidentally request 8,760 hourly windows over a year
 *   • when the canvas has an append_risky or external sink, surfaces a
 *     red guardrail with an explicit "I understand" checkbox the user
 *     must tick before Submit becomes clickable
 *
 * Idempotency check runs client-side from the canvas nodes via
 * classifyIdempotency — same classifier the canvas badges use, so the
 * warning shown here matches what the user already sees on the sink node.
 */

import { useEffect, useMemo, useState } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import { classifyIdempotency } from '../utils/idempotency';
import { api } from '../api/client';
import { toast } from './Toast';

interface BackfillModalProps {
  open: boolean;
  onClose: () => void;
  /** Called after a successful submit with the new backfill_id. */
  onSubmitted?: (backfillId: string) => void;
}

type WindowSize = 'daily' | 'weekly' | 'monthly' | 'hourly' | 'custom';
type OnFailure = 'stop' | 'continue' | 'retry_once';

function _isoDayString(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function _approxWindowCount(
  start: string,
  end: string,
  size: WindowSize,
  customHours: number,
): number {
  if (!start || !end) return 0;
  const s = new Date(`${start}T00:00:00Z`);
  const e = new Date(`${end}T00:00:00Z`);
  if (isNaN(s.getTime()) || isNaN(e.getTime()) || e < s) return 0;
  const days = Math.floor((e.getTime() - s.getTime()) / (24 * 3600 * 1000)) + 1;
  switch (size) {
    case 'daily':
      return days;
    case 'weekly':
      return Math.max(1, Math.ceil(days / 7));
    case 'monthly':
      return Math.max(1, Math.ceil(days / 30));
    case 'hourly':
      return Math.max(1, days * 24);
    case 'custom':
      if (customHours <= 0) return 0;
      return Math.max(1, Math.ceil((days * 24) / customHours));
  }
}

export default function BackfillModal({ open, onClose, onSubmitted }: BackfillModalProps) {
  const workflowId = useWorkflowStore((s) => s.workflowId);
  const workflowName = useWorkflowStore((s) => s.workflowName);
  const nodes = useWorkflowStore((s) => s.nodes);
  const declaredParams = useWorkflowStore((s) => s.parameters);

  const today = _isoDayString(new Date());
  const sevenDaysAgo = _isoDayString(new Date(Date.now() - 6 * 24 * 3600 * 1000));

  const [startDate, setStartDate] = useState<string>(sevenDaysAgo);
  const [endDate, setEndDate] = useState<string>(today);
  const [windowSize, setWindowSize] = useState<WindowSize>('daily');
  const [customHours, setCustomHours] = useState<number>(6);
  const [concurrency, setConcurrency] = useState<number>(1);
  const [onFailure, setOnFailure] = useState<OnFailure>('stop');

  // Cursor param names — default to window_start/window_end. If the
  // pipeline declares parameters with these names already, use them;
  // otherwise the names are advisory and the user can rename.
  const declaredStartName = useMemo(
    () => declaredParams.find((p) => p.name === 'window_start')?.name || 'window_start',
    [declaredParams],
  );
  const declaredEndName = useMemo(
    () => declaredParams.find((p) => p.name === 'window_end')?.name || 'window_end',
    [declaredParams],
  );
  const [startParam, setStartParam] = useState<string>(declaredStartName);
  const [endParam, setEndParam] = useState<string>(declaredEndName);

  const [acknowledgeSideEffects, setAcknowledgeSideEffects] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  // F3 (2026-05-30) — server-side preflight: cursor usage, sink safety,
  // window count, recommendations. Backed by POST /api/executions/backfill/preflight.
  // Complements the local idempotency scan with backend-authoritative checks.
  const [preflightResult, setPreflightResult] = useState<{
    ok: boolean;
    total_windows: number;
    unsafe_sinks: Array<{ id?: string; step_id?: string; type?: string; reason?: string }>;
    cursor_violation: { code?: string; message?: string } | null;
    warnings: string[];
    recommendations: string[];
  } | null>(null);
  const [preflightLoading, setPreflightLoading] = useState(false);

  // Idempotency scan — walk every node and surface the risky sinks.
  const unsafeSinks = useMemo(() => {
    const out: Array<{ id: string; type: string; cls: string; label: string }> = [];
    for (const n of nodes) {
      const stepType = (n.data as any)?.stepType || n.type || '';
      const params = (n.data as any)?.params || {};
      const info = classifyIdempotency(stepType, params);
      if (info && (info.cls === 'append_risky' || info.cls === 'external')) {
        out.push({
          id: n.id,
          type: stepType,
          cls: info.cls,
          label: (n.data as any)?.label || stepType,
        });
      }
    }
    return out;
  }, [nodes]);

  const windowCount = useMemo(
    () => _approxWindowCount(startDate, endDate, windowSize, customHours),
    [startDate, endDate, windowSize, customHours],
  );

  // F3 — debounced preflight call. Triggers when the form is far enough
  // along to give a meaningful answer (workflow + dates set + non-zero
  // window estimate). Backend's authoritative window count + cursor
  // check overrides the local approximation when both are present.
  useEffect(() => {
    if (!open || !workflowId || !startDate || !endDate || windowCount <= 0) {
      setPreflightResult(null);
      return;
    }
    let cancelled = false;
    setPreflightLoading(true);
    const handle = setTimeout(() => {
      api.post<typeof preflightResult>('/api/executions/backfill/preflight', {
        pipeline_id: workflowId,
        start_date: startDate,
        end_date: endDate,
        window_size: windowSize,
        window_size_hours: customHours,
        cursor_param_names: [startParam, endParam],
        concurrency,
        on_failure: onFailure,
        parameter_values: {},
        acknowledge_side_effects: acknowledgeSideEffects,
      })
        .then((res) => { if (!cancelled) setPreflightResult(res); })
        .catch(() => { if (!cancelled) setPreflightResult(null); })
        .finally(() => { if (!cancelled) setPreflightLoading(false); });
    }, 350);
    return () => { cancelled = true; clearTimeout(handle); };
  }, [open, workflowId, startDate, endDate, windowSize, customHours, startParam, endParam, concurrency, onFailure, acknowledgeSideEffects, windowCount]);

  if (!open) return null;

  const datesValid = !!startDate && !!endDate && new Date(endDate) >= new Date(startDate);
  const sideEffectGateOpen = unsafeSinks.length === 0 || acknowledgeSideEffects;
  const canSubmit =
    !!workflowId && datesValid && windowCount > 0 && windowCount < 100_000 &&
    sideEffectGateOpen && !submitting;

  const handleSubmit = async () => {
    if (!workflowId) {
      toast.error('Save first', 'Save the pipeline before launching a backfill.');
      return;
    }
    setSubmitting(true);
    try {
      const res = await api.createBackfill({
        pipeline_id: workflowId,
        start_date: startDate,
        end_date: endDate,
        window_size: windowSize,
        window_size_hours: customHours,
        cursor_param_names: [startParam, endParam],
        concurrency,
        on_failure: onFailure,
        parameter_values: {},
        acknowledge_side_effects: acknowledgeSideEffects,
      });
      toast.success(
        'Backfill started',
        `${res.total_windows} window${res.total_windows === 1 ? '' : 's'} queued. Track progress on the Executions page.`,
      );
      onSubmitted?.(res.backfill_id);
      onClose();
    } catch (err: any) {
      const msg = err?.message || 'The backfill API rejected the request.';
      toast.error('Backfill failed', msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 max-h-[90vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between shrink-0">
          <div>
            <h2 className="text-sm font-bold text-slate-800 flex items-center gap-2">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-violet-500">
                <rect x="3" y="4" width="18" height="18" rx="2" />
                <line x1="16" y1="2" x2="16" y2="6" />
                <line x1="8" y1="2" x2="8" y2="6" />
                <line x1="3" y1="10" x2="21" y2="10" />
                <polyline points="9 14 11 16 15 12" />
              </svg>
              Backfill {workflowName ? `"${workflowName}"` : 'pipeline'}
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Re-execute this pipeline once per time window over a historical date range.
              Each window binds <code className="bg-slate-100 px-1 rounded">${'${param.' + startParam + '}'}</code> and{' '}
              <code className="bg-slate-100 px-1 rounded">${'${param.' + endParam + '}'}</code>.
            </p>
          </div>
          <button
            onClick={onClose}
            className="w-7 h-7 rounded-lg flex items-center justify-center text-slate-400 hover:text-slate-600 hover:bg-slate-100"
            title="Close"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-auto p-5 space-y-4">
          {/* Date range */}
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-xs font-semibold text-slate-600">Start date (inclusive)</span>
              <input
                type="date"
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
                className="mt-1 w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-violet-300"
              />
            </label>
            <label className="block">
              <span className="text-xs font-semibold text-slate-600">End date (inclusive)</span>
              <input
                type="date"
                value={endDate}
                onChange={(e) => setEndDate(e.target.value)}
                className="mt-1 w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-violet-300"
              />
            </label>
          </div>

          {/* Window size */}
          <div className="grid grid-cols-[1fr_auto] gap-3 items-end">
            <label className="block">
              <span className="text-xs font-semibold text-slate-600">Window size</span>
              <select
                value={windowSize}
                onChange={(e) => setWindowSize(e.target.value as WindowSize)}
                className="mt-1 w-full px-3 py-2 text-sm border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-violet-300"
              >
                <option value="hourly">Hourly</option>
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
                <option value="custom">Custom (hours)</option>
              </select>
            </label>
            {windowSize === 'custom' && (
              <label className="block w-32">
                <span className="text-xs font-semibold text-slate-600">Hours</span>
                <input
                  type="number"
                  min={1}
                  value={customHours}
                  onChange={(e) => setCustomHours(Math.max(1, parseInt(e.target.value || '1', 10)))}
                  className="mt-1 w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-violet-300"
                />
              </label>
            )}
          </div>

          {/* Cursor params */}
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-xs font-semibold text-slate-600">Window-start param</span>
              <input
                type="text"
                value={startParam}
                onChange={(e) => setStartParam(e.target.value.replace(/[^a-zA-Z0-9_]/g, ''))}
                className="mt-1 w-full px-3 py-2 text-sm border border-slate-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-violet-300"
                title="Each window binds this parameter to its start timestamp."
              />
            </label>
            <label className="block">
              <span className="text-xs font-semibold text-slate-600">Window-end param</span>
              <input
                type="text"
                value={endParam}
                onChange={(e) => setEndParam(e.target.value.replace(/[^a-zA-Z0-9_]/g, ''))}
                className="mt-1 w-full px-3 py-2 text-sm border border-slate-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-violet-300"
                title="Each window binds this parameter to its (exclusive) end timestamp."
              />
            </label>
          </div>

          {/* Concurrency + on_failure */}
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-xs font-semibold text-slate-600">Concurrency</span>
              <input
                type="number"
                min={1}
                max={32}
                value={concurrency}
                onChange={(e) => setConcurrency(Math.max(1, parseInt(e.target.value || '1', 10)))}
                className="mt-1 w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-violet-300"
              />
            </label>
            <label className="block">
              <span className="text-xs font-semibold text-slate-600">On window failure</span>
              <select
                value={onFailure}
                onChange={(e) => setOnFailure(e.target.value as OnFailure)}
                className="mt-1 w-full px-3 py-2 text-sm border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-violet-300"
              >
                <option value="stop">Stop the backfill</option>
                <option value="continue">Continue (mark window failed)</option>
                <option value="retry_once">Retry once, then continue</option>
              </select>
            </label>
          </div>

          {/* Live window count */}
          <div className="px-3 py-2 bg-violet-50 border border-violet-100 rounded-lg text-xs text-violet-700 flex items-center justify-between">
            <span>
              {datesValid ? (
                <>
                  <span className="font-semibold">{windowCount.toLocaleString()}</span>{' '}
                  window{windowCount === 1 ? '' : 's'} will be dispatched.
                </>
              ) : (
                'Pick a valid start ≤ end date to see the window count.'
              )}
            </span>
            {windowCount > 1000 && (
              <span className="text-amber-700 font-semibold">⚠ large run</span>
            )}
          </div>

          {/* Idempotency warning */}
          {unsafeSinks.length > 0 && (
            <div className="border-2 border-red-300 bg-red-50 rounded-xl p-4 space-y-2">
              <div className="flex items-start gap-2">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-red-600 shrink-0">
                  <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                  <line x1="12" y1="9" x2="12" y2="13" />
                  <line x1="12" y1="17" x2="12.01" y2="17" />
                </svg>
                <div className="text-sm font-bold text-red-800">
                  This pipeline has {unsafeSinks.length} sink{unsafeSinks.length === 1 ? '' : 's'} that aren't safe to re-run.
                </div>
              </div>
              <ul className="text-xs text-red-700 space-y-1 ml-7">
                {unsafeSinks.map((s) => (
                  <li key={s.id} className="flex items-center gap-2">
                    <span className="inline-block px-1.5 py-0.5 rounded bg-red-200 text-red-900 font-bold text-[10px] uppercase tracking-wider">
                      {s.cls === 'append_risky' ? 'duplicates' : 'side effect'}
                    </span>
                    <span className="font-mono">{s.label}</span>
                    <span className="text-red-500">({s.type})</span>
                  </li>
                ))}
              </ul>
              <p className="text-xs text-red-700 ml-7">
                Each window will re-run every sink. <strong>Append</strong> sinks
                will duplicate data; <strong>external</strong> sinks (email, webhook,
                API) will fire side effects on every window. Consider switching to
                merge / replace mode before continuing.
              </p>
              <label className="ml-7 flex items-center gap-2 text-sm font-semibold text-red-800">
                <input
                  type="checkbox"
                  checked={acknowledgeSideEffects}
                  onChange={(e) => setAcknowledgeSideEffects(e.target.checked)}
                  className="w-4 h-4 accent-red-600"
                />
                I understand and want to proceed anyway
              </label>
            </div>
          )}
        </div>

        {/* F3 (2026-05-30) — Server-side preflight panel. Renders the
            warnings + recommendations the backend's /preflight endpoint
            returns: cursor usage check, server-authoritative window
            count, idempotency-class breakdown. Updates live as the
            user adjusts dates / window size (debounced 350ms). */}
        {(preflightLoading || preflightResult) && (
          <div className="mx-5 mb-3 px-3 py-2 rounded-lg border bg-slate-50 border-slate-200 text-[11px]">
            <div className="flex items-baseline justify-between">
              <span className="font-semibold uppercase tracking-wider text-[9px] text-slate-500">
                Server preflight
              </span>
              {preflightLoading && (
                <span className="text-[9px] text-slate-400">checking…</span>
              )}
              {!preflightLoading && preflightResult && (
                <span className={`text-[9px] font-bold uppercase ${preflightResult.ok ? 'text-emerald-700' : 'text-amber-700'}`}>
                  {preflightResult.ok ? 'OK to launch' : 'Review before launch'}
                </span>
              )}
            </div>
            {preflightResult && (
              <>
                {preflightResult.total_windows > 0 && (
                  <div className="text-slate-600 mt-1">
                    Backend will enumerate <strong>{preflightResult.total_windows}</strong> window{preflightResult.total_windows === 1 ? '' : 's'}.
                  </div>
                )}
                {preflightResult.warnings.length > 0 && (
                  <ul className="mt-1 space-y-0.5 text-amber-800 list-disc list-inside">
                    {preflightResult.warnings.map((w, i) => <li key={i}>{w}</li>)}
                  </ul>
                )}
                {preflightResult.recommendations.length > 0 && (
                  <ul className="mt-1 space-y-0.5 text-blue-800 list-disc list-inside">
                    {preflightResult.recommendations.map((r, i) => <li key={i}>{r}</li>)}
                  </ul>
                )}
              </>
            )}
          </div>
        )}

        {/* Footer */}
        <div className="px-5 py-3 border-t border-slate-200 flex items-center justify-end gap-2 shrink-0">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-semibold rounded-lg text-slate-600 bg-slate-100 hover:bg-slate-200 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={!canSubmit}
            className="px-4 py-2 text-sm font-semibold rounded-lg text-white bg-violet-600 hover:bg-violet-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-1.5"
            title={!workflowId ? 'Save the pipeline first' : !datesValid ? 'Pick a valid date range' : !sideEffectGateOpen ? 'Acknowledge side effects to continue' : 'Dispatch backfill'}
          >
            {submitting ? (
              <>
                <span className="w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin" />
                Starting…
              </>
            ) : (
              <>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" stroke="none">
                  <polygon points="5 3 19 12 5 21 5 3" />
                </svg>
                Start backfill
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
