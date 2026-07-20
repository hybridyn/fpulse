/**
 * BackfillsPanel — list of historical backfills + per-window Gantt strip.
 *
 * Lives inside ExecutionsPage as a sibling view to the Runs table. Each
 * backfill row expands into a horizontal Gantt of its windows; clicking
 * a window opens its underlying execution detail (the same flow the Runs
 * table uses), so backfills don't need a parallel detail panel — they
 * route into the existing one.
 */

import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';
import TimeAgo from '../shared/TimeAgo';

interface BackfillRow {
  id: string;
  pipeline_id: string;
  status: string;
  window_start: string;
  window_end: string;
  total_windows: number;
  succeeded_windows: number;
  failed_windows: number;
  skipped_windows: number;
  window_size: string;
  concurrency: number;
  on_failure: string;
  cursor_param_names?: string[];
  created_at: string;
  completed_at?: string;
  started_at?: string;
  parent_backfill_id?: string;
  execution_id?: string;
  error_message?: string;
}

interface BackfillsPanelProps {
  /** Optional pipeline filter — pass to restrict to one pipeline's backfills. */
  pipelineId?: string;
  /** Called when the user clicks a window cell — caller routes to that exec. */
  onOpenExecution?: (executionId: string) => void;
  dark?: boolean;
}

const STATUS_COLORS: Record<string, { bg: string; text: string; dot: string }> = {
  pending:   { bg: 'bg-slate-100',   text: 'text-slate-600',   dot: 'bg-slate-400' },
  running:   { bg: 'bg-blue-100',    text: 'text-blue-700',    dot: 'bg-blue-500' },
  success:   { bg: 'bg-emerald-100', text: 'text-emerald-700', dot: 'bg-emerald-500' },
  failed:    { bg: 'bg-red-100',     text: 'text-red-700',     dot: 'bg-red-500' },
  partial:   { bg: 'bg-amber-100',   text: 'text-amber-700',   dot: 'bg-amber-500' },
  cancelled: { bg: 'bg-slate-100',   text: 'text-slate-500',   dot: 'bg-slate-400' },
  skipped:   { bg: 'bg-slate-100',   text: 'text-slate-500',   dot: 'bg-slate-300' },
};

function StatusPill({ status }: { status: string }) {
  const cls = STATUS_COLORS[status] || STATUS_COLORS.pending;
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-semibold ${cls.bg} ${cls.text}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${cls.dot}`} />
      {status}
    </span>
  );
}

function _fmtDate(iso: string): string {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toISOString().slice(0, 10);
  } catch {
    return iso;
  }
}

export default function BackfillsPanel({ pipelineId, onOpenExecution, dark = false }: BackfillsPanelProps) {
  const [rows, setRows] = useState<BackfillRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [windowsByParent, setWindowsByParent] = useState<Record<string, BackfillRow[]>>({});
  const [loadingWindows, setLoadingWindows] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.listBackfills(pipelineId ? { pipeline_id: pipelineId } : undefined);
      setRows(Array.isArray(data) ? (data as BackfillRow[]) : []);
    } catch (err: any) {
      toast.error('Failed to load backfills', err?.message || 'Check that the backend is running.');
      setRows([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // Poll every 10s while a row is RUNNING so users see progress
    // without a manual refresh.
    const interval = setInterval(() => {
      const anyRunning = rows.some((r) => r.status === 'running' || r.status === 'pending');
      if (anyRunning) load();
    }, 10_000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipelineId]);

  // Auto-open the focused backfill if Toolbar stashed one in sessionStorage.
  useEffect(() => {
    let focused: string | null = null;
    try {
      focused = sessionStorage.getItem('fpulse_focused_backfill_id');
      if (focused) sessionStorage.removeItem('fpulse_focused_backfill_id');
    } catch { /* ignore */ }
    if (focused) {
      setExpanded(focused);
      loadWindows(focused);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadWindows = async (parentId: string) => {
    setLoadingWindows(parentId);
    try {
      const data = await api.getBackfill(parentId);
      setWindowsByParent((m) => ({ ...m, [parentId]: data.windows as BackfillRow[] }));
    } catch (err: any) {
      toast.error('Failed to load windows', err?.message || 'Open backfill detail failed.');
    } finally {
      setLoadingWindows(null);
    }
  };

  const handleToggle = (parentId: string) => {
    if (expanded === parentId) {
      setExpanded(null);
      return;
    }
    setExpanded(parentId);
    if (!windowsByParent[parentId]) {
      loadWindows(parentId);
    }
  };

  const handleCancel = async (parentId: string) => {
    try {
      await api.cancelBackfill(parentId);
      toast.info('Cancellation requested', 'In-flight windows will finish; new windows are skipped.');
      load();
    } catch (err: any) {
      toast.error('Cancel failed', err?.message || 'Backend rejected the cancel request.');
    }
  };

  // B3 (2026-06-08) — resume a failed / cancelled / partial backfill.
  // The backend auto-detects the first non-successful window, so
  // already-succeeded windows are NOT re-run. We surface a confirm
  // first because resuming SKIPS the earlier windows on the assumption
  // they completed correctly (per the design doc's open question).
  const handleResume = async (parentId: string) => {
    const ok = window.confirm(
      'Resume this backfill from the first window that did not succeed?\n\n' +
      'Already-succeeded windows are skipped (not re-run). Failed or ' +
      'pending windows from that point onward will be re-attempted.',
    );
    if (!ok) return;
    try {
      const res = await api.resumeBackfill(parentId);
      toast.info(
        'Backfill resumed',
        `Restarting from window ${res.from_window + 1}; ${res.skipped_windows} earlier window(s) skipped.`,
      );
      load();
    } catch (err: any) {
      toast.error('Resume failed', err?.message || 'Backend rejected the resume request.');
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h2 className={`text-base font-bold ${dark ? 'text-white' : 'text-slate-800'}`}>
            Backfills
          </h2>
          <p className={`text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
            Historical re-runs of pipelines over date ranges. Each row expands to a per-window strip.
          </p>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className={`px-3 py-1.5 text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5 disabled:opacity-60 ${
            dark ? 'bg-slate-800 text-slate-200 border border-slate-700 hover:bg-slate-700' : 'bg-white text-slate-500 border border-slate-200 hover:bg-slate-50'
          }`}
        >
          {loading ? (
            <span className="w-3 h-3 border-2 border-slate-400/40 border-t-slate-500 rounded-full animate-spin" />
          ) : (
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="23 4 23 10 17 10" /><polyline points="1 20 1 14 7 14" />
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
            </svg>
          )}
          Refresh
        </button>
      </div>

      {loading && rows.length === 0 ? (
        <div className={`px-4 py-10 text-center text-xs rounded-xl ${dark ? 'bg-slate-800 text-slate-400' : 'bg-slate-50 text-slate-400'}`}>
          Loading backfills…
        </div>
      ) : rows.length === 0 ? (
        <div className={`px-4 py-10 text-center text-xs rounded-xl ${dark ? 'bg-slate-800 text-slate-400' : 'bg-slate-50 text-slate-400'}`}>
          No backfills yet. Open a pipeline in the editor and use{' '}
          <code className={`px-1 rounded ${dark ? 'bg-slate-700 text-slate-200' : 'bg-slate-200 text-slate-700'}`}>Pipeline menu → Backfill…</code>{' '}
          to kick one off.
        </div>
      ) : (
        <div className="space-y-2">
          {rows.map((bf) => {
            const isOpen = expanded === bf.id;
            const windows = windowsByParent[bf.id] || [];
            const progress = bf.total_windows > 0
              ? Math.round(((bf.succeeded_windows + bf.failed_windows + bf.skipped_windows) / bf.total_windows) * 100)
              : 0;
            return (
              <div
                key={bf.id}
                className={`rounded-xl border ${dark ? 'border-white/[0.08] bg-[#111827]' : 'border-slate-200 bg-white'}`}
              >
                {/* Row header */}
                <button
                  type="button"
                  onClick={() => handleToggle(bf.id)}
                  className={`w-full px-4 py-3 flex items-center gap-3 text-left transition-colors ${
                    isOpen
                      ? (dark ? 'bg-white/[0.04]' : 'bg-slate-50')
                      : (dark ? 'hover:bg-white/[0.02]' : 'hover:bg-slate-50/60')
                  }`}
                >
                  <svg
                    width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
                    className={`shrink-0 transition-transform ${isOpen ? 'rotate-90' : ''} ${dark ? 'text-slate-500' : 'text-slate-400'}`}
                  >
                    <polyline points="9 18 15 12 9 6" />
                  </svg>
                  <StatusPill status={bf.status} />
                  <div className="flex-1 min-w-0">
                    <div className={`text-sm font-semibold truncate ${dark ? 'text-slate-100' : 'text-slate-800'}`}>
                      {_fmtDate(bf.window_start)} → {_fmtDate(bf.window_end)}
                    </div>
                    <div className={`text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
                      Pipeline <span className="font-mono">{bf.pipeline_id}</span> · {bf.window_size}
                      {' · '}
                      <span className="font-mono">{bf.id}</span>
                    </div>
                  </div>
                  <div className="hidden md:flex items-center gap-3 shrink-0">
                    <div className="text-right">
                      <div className={`text-xs font-semibold ${dark ? 'text-slate-200' : 'text-slate-700'}`}>
                        {bf.succeeded_windows}/{bf.total_windows} ok
                      </div>
                      {bf.failed_windows > 0 && (
                        <div className="text-[10px] text-red-500 font-semibold">{bf.failed_windows} failed</div>
                      )}
                    </div>
                    <div className="w-32">
                      <div className={`h-1.5 rounded-full overflow-hidden ${dark ? 'bg-slate-700' : 'bg-slate-200'}`}>
                        <div
                          className={`h-full rounded-full ${
                            bf.status === 'success' ? 'bg-emerald-500'
                              : bf.status === 'failed' ? 'bg-red-500'
                              : bf.status === 'partial' ? 'bg-amber-500'
                              : 'bg-blue-500'
                          }`}
                          style={{ width: `${progress}%` }}
                        />
                      </div>
                      <div className={`text-[10px] mt-0.5 text-right ${dark ? 'text-slate-500' : 'text-slate-400'}`}>{progress}%</div>
                    </div>
                  </div>
                  <div className={`hidden lg:block text-xs ${dark ? 'text-slate-500' : 'text-slate-400'}`}>
                    <TimeAgo value={bf.created_at} />
                  </div>
                  {(bf.status === 'running' || bf.status === 'pending') && (
                    <button
                      onClick={(e) => { e.stopPropagation(); handleCancel(bf.id); }}
                      className="px-2 py-1 text-[10px] font-semibold rounded text-red-700 bg-red-50 border border-red-200 hover:bg-red-100 transition-colors"
                      title="Stop the backfill — in-flight windows finish naturally"
                    >
                      Cancel
                    </button>
                  )}
                  {/* B3 (2026-06-08) — resume from the first unfinished
                      window. Shown for terminal-but-incomplete states. */}
                  {(bf.status === 'failed' || bf.status === 'partial' || bf.status === 'cancelled') && (
                    <button
                      onClick={(e) => { e.stopPropagation(); handleResume(bf.id); }}
                      className="px-2 py-1 text-[10px] font-semibold rounded text-emerald-700 bg-emerald-50 border border-emerald-200 hover:bg-emerald-100 transition-colors"
                      title="Resume from the first window that didn't succeed — completed windows are skipped"
                    >
                      Resume
                    </button>
                  )}
                </button>

                {/* Expanded: per-window Gantt strip */}
                {isOpen && (
                  <div className={`border-t px-4 py-3 ${dark ? 'border-white/[0.06] bg-[#0d1422]' : 'border-slate-200 bg-slate-50/40'}`}>
                    {loadingWindows === bf.id && windows.length === 0 ? (
                      <div className={`text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`}>Loading windows…</div>
                    ) : windows.length === 0 ? (
                      <div className={`text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`}>No windows recorded yet.</div>
                    ) : (
                      <div>
                        <div className={`text-[10px] uppercase tracking-wider mb-2 ${dark ? 'text-slate-500' : 'text-slate-400'}`}>
                          {windows.length} window{windows.length === 1 ? '' : 's'} · click a cell to open its execution
                        </div>
                        {/* Gantt strip — one column per window, colored by status */}
                        <div
                          className="grid gap-0.5 mb-3"
                          style={{ gridTemplateColumns: `repeat(${Math.min(windows.length, 60)}, minmax(0, 1fr))` }}
                        >
                          {windows.map((w) => {
                            const cls = STATUS_COLORS[w.status] || STATUS_COLORS.pending;
                            return (
                              <button
                                key={w.id}
                                type="button"
                                onClick={() => {
                                  if (w.execution_id && onOpenExecution) onOpenExecution(w.execution_id);
                                }}
                                disabled={!w.execution_id}
                                title={`${_fmtDate(w.window_start)} → ${_fmtDate(w.window_end)} · ${w.status}${w.error_message ? '\n' + w.error_message : ''}`}
                                className={`h-6 rounded-sm transition-opacity hover:opacity-80 disabled:cursor-default ${cls.dot}`}
                                aria-label={`Window ${w.window_start}: ${w.status}`}
                              />
                            );
                          })}
                        </div>
                        {/* Window list (compact rows for the first 12 windows) */}
                        <div className="space-y-1">
                          {windows.slice(0, 12).map((w) => (
                            <div
                              key={w.id}
                              className={`flex items-center gap-3 text-xs px-2 py-1 rounded ${dark ? 'hover:bg-white/[0.04]' : 'hover:bg-white'}`}
                            >
                              <StatusPill status={w.status} />
                              <span className={`flex-1 font-mono ${dark ? 'text-slate-200' : 'text-slate-700'}`}>
                                {_fmtDate(w.window_start)} → {_fmtDate(w.window_end)}
                              </span>
                              {w.execution_id && onOpenExecution && (
                                <button
                                  onClick={() => onOpenExecution(w.execution_id!)}
                                  className={`underline ${dark ? 'text-blue-300 hover:text-blue-200' : 'text-blue-700 hover:text-blue-900'}`}
                                >
                                  Open run
                                </button>
                              )}
                              {w.error_message && (
                                <span className={`truncate max-w-md ${dark ? 'text-red-400' : 'text-red-600'}`} title={w.error_message}>
                                  {w.error_message}
                                </span>
                              )}
                            </div>
                          ))}
                          {windows.length > 12 && (
                            <div className={`text-[10px] ${dark ? 'text-slate-500' : 'text-slate-400'}`}>
                              + {windows.length - 12} more window{windows.length - 12 === 1 ? '' : 's'} above (Gantt strip)
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
