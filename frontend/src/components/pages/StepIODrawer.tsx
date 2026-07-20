/**
 * StepIODrawer — historical execution replay viewer.
 *
 * Bottom-anchored panel that opens when the user clicks a node on the
 * Executions lineage graph. Shows captured input and output data for
 * that step in that run, in three views (Schema / Table / JSON), with
 * client-side search and CSV/JSON export.
 *
 * Backend contract:
 *   GET /api/execute/execution/{id}/step/{step_id}/output
 *   GET /api/execute/execution/{id}/step/{step_id}/input
 *   GET /api/execute/execution/{id}/step/{step_id}/output/export?fmt=csv|json
 *
 * Capture caps (OSS): 100 rows / 1 MB per step, 30-day TTL on samples.
 */

import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';
import { StepTypeIcon } from '../shared/StepTypeIcon';

type View = 'schema' | 'table' | 'json';
type Direction = 'output' | 'input';

interface SchemaCol {
  name: string;
  dtype: string;
  null_count: number;
  distinct_count: number | null;
  from_sample: boolean;
  sample_size: number;
}

interface OutputPayload {
  step_id: string;
  step_type: string;
  label: string;
  status: string;
  row_count: number;
  sample_rows: Record<string, any>[];
  sample_truncated: boolean;
  sample_pruned: boolean;
  schema: SchemaCol[];
  captured_at: string;
}

interface InputSource {
  source_step_id: string;
  label: string;
  row_count: number;
  sample_rows: Record<string, any>[];
  sample_truncated: boolean;
  sample_pruned: boolean;
  schema: SchemaCol[];
  missing: boolean;
}

interface StepInfo {
  step_type?: string;
  status?: string;
  duration_ms?: number;
  rows_processed?: number;
}

interface Props {
  open: boolean;
  executionId: string | null;
  stepId: string | null;
  stepLabel?: string;
  /** Snapshot from the parent execution's step_logs — lets the header
   * match the canvas node card (icon + status colour + row count + duration)
   * before the per-step capture has finished loading. */
  stepInfo?: StepInfo;
  onClose: () => void;
}

const STATUS_STYLES: Record<string, {
  bg: string; border: string; text: string; icon: string;
}> = {
  success: { bg: 'bg-emerald-50',  border: 'border-emerald-300', text: 'text-emerald-700', icon: '✓' },
  error:   { bg: 'bg-rose-50',     border: 'border-rose-300',    text: 'text-rose-700',    icon: '✕' },
  running: { bg: 'bg-amber-50',    border: 'border-amber-300',   text: 'text-amber-700',   icon: '●' },
  pending: { bg: 'bg-slate-50',    border: 'border-slate-200',   text: 'text-slate-500',   icon: '○' },
  skipped: { bg: 'bg-slate-50',    border: 'border-slate-200',   text: 'text-slate-500',   icon: '→' },
};

function formatDurationMs(ms?: number): string {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

function formatRowCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toLocaleString();
}

function StatusDot({ status }: { status: string }) {
  const cls = status === 'success'
    ? 'bg-emerald-500'
    : status === 'error'
    ? 'bg-rose-500'
    : status === 'running'
    ? 'bg-amber-500'
    : 'bg-slate-400';
  return <span className={`inline-block w-2 h-2 rounded-full ${cls}`} aria-hidden="true" />;
}

function Banner({ tone, children }: { tone: 'info' | 'warn'; children: React.ReactNode }) {
  const cls = tone === 'warn'
    ? 'bg-amber-50 border-amber-200 text-amber-900'
    : 'bg-sky-50 border-sky-200 text-sky-900';
  return (
    <div className={`text-xs px-3 py-2 rounded-md border ${cls}`}>
      {children}
    </div>
  );
}

export default function StepIODrawer({ open, executionId, stepId, stepLabel, stepInfo, onClose }: Props) {
  const [direction, setDirection] = useState<Direction>('output');
  const [view, setView] = useState<View>('table');
  const [search, setSearch] = useState('');
  const [output, setOutput] = useState<OutputPayload | null>(null);
  const [inputs, setInputs] = useState<InputSource[] | null>(null);
  const [activeInputIdx, setActiveInputIdx] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  // Panel height — null = use default (55vh capped at 560px). User
  // can drag the top edge to resize; the chosen height persists until
  // the panel is closed.
  const [panelHeight, setPanelHeight] = useState<number | null>(null);

  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = panelHeight ?? Math.min(window.innerHeight * 0.55, 560);
    const onMove = (ev: MouseEvent) => {
      const delta = startY - ev.clientY;
      const next = Math.max(240, Math.min(window.innerHeight * 0.9, startH + delta));
      setPanelHeight(next);
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
    };
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'ns-resize';
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };

  useEffect(() => {
    setSearch('');
    setActiveInputIdx(0);
    setDirection('output');
    setView('table');
  }, [executionId, stepId]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  // Click outside the panel closes it. Lineage node clicks are exempt so
  // users can swap between nodes without losing the panel. mousedown (not
  // click) so the swap doesn't race with the close.
  useEffect(() => {
    if (!open) return;
    const onMouseDown = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null;
      if (!target) return;
      const section = document.querySelector('section[aria-label="Step IO inspector"]');
      if (section && section.contains(target)) return;
      if (target.closest && target.closest('svg g[role="button"]')) return;
      onClose();
    };
    document.addEventListener('mousedown', onMouseDown);
    return () => document.removeEventListener('mousedown', onMouseDown);
  }, [open, onClose]);

  useEffect(() => {
    if (!open || !executionId || !stepId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([
      api.getStepOutput(executionId, stepId).catch((e) => {
        if (String(e).includes('404')) return null;
        throw e;
      }),
      api.getStepInput(executionId, stepId).catch(() => null),
    ])
      .then(([out, ins]) => {
        if (cancelled) return;
        setOutput(out as OutputPayload | null);
        setInputs(ins ? ins.inputs : null);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(String(e?.message || e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, executionId, stepId]);

  const active: {
    rows: Record<string, any>[];
    schema: SchemaCol[];
    rowCount: number;
    truncated: boolean;
    pruned: boolean;
    missing: boolean;
  } | null = useMemo(() => {
    if (direction === 'output') {
      if (!output) return null;
      return {
        rows: output.sample_rows,
        schema: output.schema,
        rowCount: output.row_count,
        truncated: output.sample_truncated,
        pruned: output.sample_pruned,
        missing: false,
      };
    }
    if (!inputs || inputs.length === 0) return null;
    const src = inputs[Math.min(activeInputIdx, inputs.length - 1)];
    return {
      rows: src.sample_rows,
      schema: src.schema,
      rowCount: src.row_count,
      truncated: src.sample_truncated,
      pruned: src.sample_pruned,
      missing: src.missing,
    };
  }, [direction, output, inputs, activeInputIdx]);

  const filteredRows = useMemo(() => {
    if (!active) return [];
    const q = search.trim().toLowerCase();
    if (!q) return active.rows;
    return active.rows.filter((row) =>
      Object.values(row).some((v) =>
        v != null && String(v).toLowerCase().includes(q),
      ),
    );
  }, [active, search]);

  const handleExport = async (fmt: 'csv' | 'json') => {
    if (!executionId || !stepId) return;
    setExporting(true);
    try {
      const blob = await api.exportStepOutput(executionId, stepId, fmt);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const safeLabel = (output?.label || stepId).replace(/[^a-zA-Z0-9_-]+/g, '_');
      a.href = url;
      a.download = `${safeLabel}_${executionId.slice(0, 8)}.${fmt}`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e: any) {
      toast.error('Export failed', e?.message || String(e));
    } finally {
      setExporting(false);
    }
  };

  const columns: string[] = useMemo(() => {
    if (!active) return [];
    if (active.schema.length > 0) return active.schema.map((c) => c.name);
    if (active.rows.length > 0) return Object.keys(active.rows[0]);
    return [];
  }, [active]);

  // Prefer fresh capture data; fall back to the step_log snapshot the
  // parent passed in (lets the header populate even before the capture
  // request resolves, and works on legacy runs where capture data is
  // missing entirely).
  const headerStepType = output?.step_type || stepInfo?.step_type || '';
  const headerStatus = output?.status || stepInfo?.status || 'pending';
  const headerRowCount = output?.row_count ?? stepInfo?.rows_processed ?? 0;
  const headerDurationMs = stepInfo?.duration_ms;
  const statusStyle = STATUS_STYLES[headerStatus] || STATUS_STYLES.pending;

  const showExport = direction === 'output' && output && output.sample_rows.length > 0;

  if (!open) return null;

  return (
    <section
      aria-label="Step IO inspector"
      className="bg-white shadow-2xl flex flex-col"
      style={{
        position: 'fixed',
        left: 0,
        right: 0,
        bottom: 0,
        zIndex: 50,
        height: panelHeight != null ? `${panelHeight}px` : 'min(55vh, 560px)',
        minHeight: '240px',
        borderTop: '3px solid',
        borderTopColor: headerStatus === 'success' ? '#10b981'
          : headerStatus === 'error' ? '#f43f5e'
          : headerStatus === 'running' ? '#f59e0b' : '#cbd5e1',
      }}
    >
      {/* Drag-to-resize handle along the top edge */}
      <div
        role="separator"
        aria-label="Resize panel"
        aria-orientation="horizontal"
        onMouseDown={startResize}
        title="Drag to resize"
        style={{
          position: 'absolute',
          top: -3,
          left: 0,
          right: 0,
          height: 8,
          cursor: 'ns-resize',
          zIndex: 51,
        }}
      />
      <header className={`relative px-3 py-1.5 border-b border-slate-200 flex items-center gap-2 shrink-0 ${statusStyle.bg}`}>
        {/* Step type icon — same glyph rendered on the canvas node card */}
        <div className={`shrink-0 w-8 h-8 rounded-md border ${statusStyle.border} bg-white flex items-center justify-center ${statusStyle.text}`}>
          {headerStepType ? <StepTypeIcon type={headerStepType} size={16} /> : <span>⬡</span>}
        </div>

        {/* Label + step_type — shrinks if header gets crowded */}
        <div className="min-w-0 flex-1 max-w-[200px]">
          <div className="text-sm font-semibold text-slate-800 truncate leading-tight">
            {stepLabel || output?.label || stepId || 'Step'}
          </div>
          <div className="text-[11px] font-mono text-slate-500 truncate leading-tight">
            {headerStepType || stepId}
          </div>
        </div>

        {/* View controls — direction, then view tabs, then search */}
        <div className="flex gap-0.5 p-0.5 bg-slate-100 rounded-md shrink-0">
          {(['output', 'input'] as Direction[]).map((d) => (
            <button
              key={d}
              type="button"
              onClick={() => setDirection(d)}
              className={`px-3 py-1 text-xs font-semibold rounded ${
                direction === d ? 'bg-white shadow text-slate-900' : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              {d === 'output' ? 'Output' : 'Input'}
              {d === 'input' && inputs && inputs.length > 1 && (
                <span className="ml-1 text-[10px] text-slate-500">({inputs.length})</span>
              )}
            </button>
          ))}
        </div>

        <div className="flex gap-0.5 shrink-0 ml-3 pl-3 border-l border-slate-200">
          {(['table', 'schema', 'json'] as View[]).map((v) => (
            <button
              key={v}
              type="button"
              onClick={() => setView(v)}
              className={`px-3 py-1 text-xs font-medium rounded border ${
                view === v
                  ? 'border-pipe-500 bg-pipe-50 text-pipe-700'
                  : 'border-transparent text-slate-500 hover:text-slate-700 hover:bg-white'
              }`}
            >
              {v.charAt(0).toUpperCase() + v.slice(1)}
            </button>
          ))}
        </div>

        {/* Spacer — pushes status / stats / export / close to the right edge */}
        <div className="flex-1" aria-hidden="true" />

        {/* Search — absolute-positioned at the geometric center of the
            header so it sits at the middle regardless of how wide the
            left/right clusters are. */}
        <div
          className="relative"
          style={{
            position: 'absolute',
            left: '50%',
            top: '50%',
            transform: 'translate(-50%, -50%)',
            width: 360,
          }}
        >
          <button
            type="button"
            onClick={() => {
              const el = document.getElementById('step-io-search') as HTMLInputElement | null;
              if (el) el.focus();
            }}
            aria-label="Focus search"
            className="absolute left-1.5 top-1/2 -translate-y-1/2 p-1 text-slate-400 hover:text-slate-600 z-10"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="11" cy="11" r="7" />
              <line x1="21" y1="21" x2="16.65" y2="16.65" />
            </svg>
          </button>
          <input
            id="step-io-search"
            type="search"
            placeholder="Search in this sample…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full text-xs pl-8 pr-14 py-1.5 border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-pipe-200 focus:border-pipe-400 bg-white"
          />
          {search && (
            <span className="absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-slate-500 pointer-events-none bg-white px-1 rounded">
              {filteredRows.length}/{active?.rows.length ?? 0}
            </span>
          )}
        </div>

        {/* Status badge — matches canvas card "✓ SUCCESS" pill */}
        <div className={`shrink-0 inline-flex items-center gap-1 px-2 py-0.5 rounded border ${statusStyle.border} ${statusStyle.text} bg-white text-[10px] font-bold uppercase tracking-wider`}>
          <span>{statusStyle.icon}</span>
          <span>{headerStatus}</span>
        </div>

        {/* Stats: rows + duration */}
        <div className="shrink-0 flex items-center gap-3 text-[11px] text-slate-600">
          <span className="inline-flex items-center gap-1" title="Rows produced by this step">
            <span className="text-slate-400">◈</span>
            <span className="tabular-nums">{formatRowCount(headerRowCount)}</span>
            <span className="text-slate-400">rows</span>
          </span>
          <span className="inline-flex items-center gap-1" title="Wall-clock duration">
            <span className="text-slate-400">⏱</span>
            <span className="tabular-nums font-mono">{formatDurationMs(headerDurationMs)}</span>
          </span>
        </div>

        {/* Export buttons — moved from footer so the Copilot widget doesn't cover them */}
        {showExport && (
          <div className="shrink-0 flex items-center gap-1">
            <button
              type="button"
              onClick={() => handleExport('csv')}
              disabled={exporting}
              title="Download captured sample as CSV"
              className="inline-flex items-center gap-1 px-2 py-1 text-[11px] font-medium rounded-md border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
              CSV
            </button>
            <button
              type="button"
              onClick={() => handleExport('json')}
              disabled={exporting}
              title="Download captured sample as JSON"
              className="inline-flex items-center gap-1 px-2 py-1 text-[11px] font-medium rounded-md border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
              JSON
            </button>
          </div>
        )}

        {/* Close */}
        <button
          type="button"
          onClick={onClose}
          aria-label="Close step inspector"
          className="shrink-0 p-1 rounded-md text-slate-400 hover:text-slate-700 hover:bg-white/60 transition-colors"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-3 min-h-0">
        {direction === 'input' && inputs && inputs.length > 1 && (
          <div className="flex flex-wrap gap-1 mb-3">
            {inputs.map((src, idx) => (
              <button
                key={src.source_step_id}
                type="button"
                onClick={() => setActiveInputIdx(idx)}
                className={`px-2.5 py-1 text-xs rounded border ${
                  idx === activeInputIdx
                    ? 'border-pipe-500 bg-pipe-50 text-pipe-800 font-semibold'
                    : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300'
                }`}
                title={`From ${src.source_step_id}`}
              >
                from {src.label}
              </button>
            ))}
          </div>
        )}

        {loading && (
          <div className="flex items-center justify-center py-12 text-slate-400 text-sm">
            Loading captured data…
          </div>
        )}
        {!loading && error && (
          <Banner tone="warn">Failed to load step data: {error}</Banner>
        )}
        {!loading && !error && !active && (
          <div className="text-center py-12 text-slate-400 text-sm">
            {direction === 'output'
              ? 'No output captured for this step. The pipeline may pre-date the replay capture or the sample may have aged out.'
              : 'This step has no captured inputs (root step, or upstream capture not available).'}
          </div>
        )}

        {!loading && active && (
          <>
            {active.missing && (
              <div className="mb-3">
                <Banner tone="warn">
                  Upstream capture not available — showing structural placeholder only.
                </Banner>
              </div>
            )}
            {active.pruned && (
              <div className="mb-3">
                <Banner tone="info">
                  Sample data was cleared (older than 30 days). Row count and schema below
                  still reflect the run.
                </Banner>
              </div>
            )}
            {!active.pruned && active.rows.length < active.rowCount && active.rows.length > 0 && (
              <div className="mb-3">
                <Banner tone="info">
                  Showing {active.rows.length} of {formatRowCount(active.rowCount)} rows
                  (capture sample). Export gives this sample, not the full dataset.
                </Banner>
              </div>
            )}

            {view === 'schema' && (
              <div className="border border-slate-200 rounded-md overflow-auto" style={{ maxHeight: '36vh' }}>
                {active.schema.length === 0 ? (
                  <div className="text-center py-10 text-sm text-slate-400">
                    No schema captured.
                  </div>
                ) : (
                  <>
                    {/* JSON-tree style schema — one field per line, types right-aligned.
                        Hover a row to see null + distinct stats. */}
                    <pre className="font-mono text-xs leading-relaxed text-slate-700 px-4 py-3 m-0">
                      <span className="text-slate-400">{'{'}</span>
                      {'\n'}
                      {active.schema.map((col, i) => {
                        const stats: string[] = [];
                        if (col.null_count > 0) stats.push(`${col.null_count} nulls`);
                        if (col.distinct_count != null) stats.push(`${col.distinct_count} distinct`);
                        const statsLabel = stats.length ? `  // ${stats.join(', ')}` : '';
                        return (
                          <span
                            key={col.name}
                            className="block hover:bg-slate-50 px-2 -mx-2 rounded"
                            title={statsLabel ? statsLabel.slice(3) : undefined}
                          >
                            <span className="text-slate-400">  </span>
                            <span className="text-slate-900 font-semibold">{col.name}</span>
                            <span className="text-slate-400">: </span>
                            <span className="text-violet-600">{col.dtype.toLowerCase()}</span>
                            {i < active.schema.length - 1 && <span className="text-slate-300">,</span>}
                            {statsLabel && <span className="text-slate-400">{statsLabel}</span>}
                          </span>
                        );
                      })}
                      <span className="text-slate-400">{'}'}</span>
                    </pre>
                    {active.schema[0]?.from_sample && (
                      <div className="text-[11px] text-slate-500 px-4 py-1.5 bg-slate-50 border-t border-slate-100">
                        {active.schema.length} fields · stats computed from {active.schema[0].sample_size}-row sample
                      </div>
                    )}
                  </>
                )}
              </div>
            )}

            {view === 'table' && (
              <div className="border border-slate-200 rounded-md overflow-auto" style={{ maxHeight: '40vh' }}>
                {filteredRows.length === 0 ? (
                  <div className="text-center py-10 text-sm text-slate-400">
                    {search ? 'No rows match the search.' : 'No sample rows captured.'}
                  </div>
                ) : (
                  <table className="w-full text-sm">
                    <thead className="bg-slate-50 sticky top-0 z-10 text-xs text-slate-600">
                      <tr>
                        <th className="px-2 py-2 text-right font-semibold w-10">#</th>
                        {columns.map((c) => (
                          <th key={c} className="px-3 py-2 text-left font-semibold whitespace-nowrap">{c}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {filteredRows.map((row, i) => (
                        <tr key={i} className="hover:bg-slate-50">
                          <td className="px-2 py-1.5 text-right text-slate-400 font-mono text-xs">{i + 1}</td>
                          {columns.map((c) => {
                            const v = row[c];
                            const display = v == null
                              ? <span className="text-slate-300 italic">null</span>
                              : typeof v === 'object'
                              ? <span className="font-mono text-xs">{JSON.stringify(v)}</span>
                              : String(v);
                            return (
                              <td key={c} className="px-3 py-1.5 align-top whitespace-pre-wrap break-words">
                                {display}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}

            {view === 'json' && (
              <pre className="text-xs font-mono bg-slate-900 text-slate-100 rounded-md p-3 overflow-auto whitespace-pre-wrap break-words" style={{ maxHeight: '40vh' }}>
                {JSON.stringify(filteredRows, null, 2)}
              </pre>
            )}
          </>
        )}
      </div>

      {output && (
        <div className="flex items-center justify-between gap-2 px-4 py-1.5 border-t border-slate-200 bg-slate-50 shrink-0 text-[11px] text-slate-500">
          <div>Captured {new Date(output.captured_at).toLocaleString()}</div>
          {exporting && <div className="text-pipe-700 font-medium">Exporting…</div>}
        </div>
      )}
    </section>
  );
}
