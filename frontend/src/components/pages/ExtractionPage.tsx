import { useEffect, useRef, useState } from 'react';
import PageHeader from '../shared/PageHeader';
import EmptyState from '../shared/EmptyState';
import ErrorBanner from '../shared/ErrorBanner';
import StatusPill from '../shared/StatusPill';
import { api } from '../../api/client';
import { usePageContext } from '../../hooks/usePageContext';
import { navigateTo, navigateToSubRoute } from '../../router';

/**
 * Extraction runs monitor.
 *
 * Two views in one page (driven by the URL hash):
 *   #extraction              — list of runs (pulled from /api/extraction/runs)
 *   #extraction/<run_id>     — live monitor for one run (SSE stream)
 *
 * The monitor view subscribes to /api/extraction/runs/<id>/stream and
 * renders five panels: phase + freshness, progress + ETA, AIMD
 * concurrency + counters, audit log, DLQ link. Closes itself when the
 * server sends an `end` message (run reached completed/failed).
 */

interface RunSnapshot {
  run_id: string;
  profile: string;
  started_at: number;
  completed_at: number | null;
  phase: string;
  listed: number;
  extracted: number;
  failed: number;
  skipped_resumed: number;
  concurrency: number;
  rate_limited_count: number;
  auth_refreshed_count: number;
  last_event_at: number;
  error: string | null;
  progress: number | null;
  eta_seconds: number | null;
}

interface RunEvent {
  run_id: string;
  profile: string;
  kind: string;
  ts: number;
  payload: Record<string, any>;
}

// 2026-05-19 (P1 #2 of PAGE_BY_PAGE_AUDIT.md): phase colours now route
// through the shared <StatusPill> so the Extraction monitor reads with
// the same palette + icon set as Executions / Pool / Lineage. Backend
// `phase` strings (starting / list / enrichment / completed / failed)
// are normalised by `normaliseStatus()` in StatusPill — `list` and
// `enrichment` fall through to `info`, which is the right neutral tone
// for "in progress, no specific signal yet".

function formatSeconds(s: number | null): string {
  if (s == null) return '—';
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

function formatRelative(ts: number): string {
  const diff = Date.now() / 1000 - ts;
  if (diff < 5) return 'just now';
  if (diff < 60) return `${Math.round(diff)}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  return `${(diff / 3600).toFixed(1)}h ago`;
}

// ─── List view ────────────────────────────────────────────────────────

function RunsList({ onOpen }: { onOpen: (id: string) => void }) {
  const [runs, setRuns] = useState<RunSnapshot[]>([]);
  const [loading, setLoading] = useState(true);
  // 2026-05-19 (P1 #4 of PAGE_BY_PAGE_AUDIT.md): list view had no error
  // UI — a backend hiccup left the prior run list on screen forever. We
  // now surface fetch failures via the shared <ErrorBanner>.
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const tick = async () => {
      try {
        // 2026-05-19 (P1 #10 of PAGE_BY_PAGE_AUDIT.md): migrated from raw
        // `fetch` to the shared api client for auth / workspace / 401 +
        // backend-reachable signal consistency.
        const j = await api.get<{ runs: RunSnapshot[] }>('/api/extraction/runs');
        setRuns(j.runs || []);
        setError(null);
      } catch (err: any) {
        setError(err?.message || 'Failed to load extraction runs');
      } finally {
        setLoading(false);
      }
    };
    tick();
    const id = window.setInterval(tick, 2000);
    return () => window.clearInterval(id);
  }, []);

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* 2026-05-19 (P1 #1 of PAGE_BY_PAGE_AUDIT.md): adopted the canonical
          sticky 78px PageHeader shell — the page previously had no app
          chrome at all (no header, no HubTabs, no env badge) and looked
          like a developer scratch page that escaped into shipping UI. */}
      <PageHeader
        icon={(
          <div className="w-10 h-10 rounded-lg bg-blue-50 flex items-center justify-center border border-blue-200">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#1e40af" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2v20M2 12h20" />
              <circle cx="12" cy="12" r="4" />
            </svg>
          </div>
        )}
        title="Extraction Runs"
        subtitle="Live + recent runs from the in-memory event bus. Active runs stream; completed runs are evicted LRU after the cap."
      />
      <div className="flex-1 overflow-auto">
        <div className="p-6 max-w-[1400px] mx-auto">
          {error && (
            <div className="mb-4">
              <ErrorBanner
                title="Couldn't load extraction runs"
                message={error}
                onRetry={() => { setError(null); setLoading(true); /* next interval tick refreshes */ }}
              />
            </div>
          )}
          {loading && <div className="text-xs text-slate-400">Loading…</div>}
          {!loading && runs.length === 0 && !error && (
            <EmptyState
              icon={(
                <div className="w-10 h-10 rounded-full bg-slate-100 flex items-center justify-center">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
                  </svg>
                </div>
              )}
              title="No extraction runs in memory"
              body={(
                <>Kick off a run via <code className="bg-slate-100 px-1 rounded">await ExtractionEngine(profile=…).run()</code> and it'll appear here in real time.</>
              )}
            />
          )}
      {!loading && runs.length > 0 && (
        <div className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
          <table className="w-full text-xs">
            <thead className="bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900">
              <tr>
                <th className="text-left px-4 py-2 text-amber-300 uppercase tracking-wider font-bold">Run</th>
                <th className="text-left px-4 py-2 text-amber-300 uppercase tracking-wider font-bold">Profile</th>
                <th className="text-left px-4 py-2 text-amber-300 uppercase tracking-wider font-bold">Phase</th>
                <th className="text-right px-4 py-2 text-amber-300 uppercase tracking-wider font-bold">Progress</th>
                <th className="text-right px-4 py-2 text-amber-300 uppercase tracking-wider font-bold">Concurrency</th>
                <th className="text-right px-4 py-2 text-amber-300 uppercase tracking-wider font-bold">ETA</th>
                <th className="text-right px-4 py-2 text-amber-300 uppercase tracking-wider font-bold">Last event</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {runs.map(r => (
                <tr key={r.run_id} onClick={() => onOpen(r.run_id)}
                     className="cursor-pointer hover:bg-blue-50/40 transition-colors">
                  <td className="px-4 py-2 font-mono text-slate-700">{r.run_id.slice(0, 8)}</td>
                  <td className="px-4 py-2 text-slate-700">{r.profile}</td>
                  <td className="px-4 py-2">
                    <StatusPill status={r.phase} size="sm" />
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {r.progress != null ? `${(r.progress * 100).toFixed(0)}%` : '—'}
                    <span className="text-slate-400 ml-1">({r.extracted}/{r.listed || '?'})</span>
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums text-slate-700">{r.concurrency || '—'}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-slate-500">{formatSeconds(r.eta_seconds)}</td>
                  <td className="px-4 py-2 text-right text-xs text-slate-400">{formatRelative(r.last_event_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
        </div>
      </div>
    </div>
  );
}

// ─── Monitor view ──────────────────────────────────────────────────────

function RunMonitor({ runId, onBack }: { runId: string; onBack: () => void }) {
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [error, setError] = useState<string>('');
  const [streamClosed, setStreamClosed] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    const es = new EventSource(`/api/extraction/runs/${runId}/stream?poll_ms=500`);
    esRef.current = es;

    es.onmessage = (msg) => {
      try {
        const parsed = JSON.parse(msg.data);
        if (parsed.type === 'tick') {
          setSnapshot(parsed.snapshot);
          if (Array.isArray(parsed.new_events) && parsed.new_events.length > 0) {
            setEvents(prev => [...prev, ...parsed.new_events].slice(-200));
          }
        } else if (parsed.type === 'end') {
          setStreamClosed(true);
          es.close();
        } else if (parsed.type === 'error') {
          setError(parsed.reason || 'unknown');
          es.close();
        }
      } catch {
        // ignore malformed frames
      }
    };

    es.onerror = () => {
      setError('Connection to event stream lost.');
    };

    return () => {
      es.close();
      esRef.current = null;
    };
  }, [runId]);

  if (error) {
    return (
      <div className="p-6 max-w-[1400px] mx-auto">
        <button onClick={onBack} className="text-xs text-blue-600 mb-4 hover:underline">← Back to runs</button>
        <div className="rounded-lg border border-red-200 bg-red-50 p-4">
          <p className="text-sm font-bold text-red-700 mb-1">Stream error</p>
          <p className="text-xs text-red-500">{error}</p>
        </div>
      </div>
    );
  }

  if (!snapshot) {
    return <div className="p-6 text-xs text-slate-400">Connecting to run {runId}…</div>;
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto space-y-4">
      <button onClick={onBack} className="text-xs text-blue-600 hover:underline">← Back to runs</button>

      {/* Header */}
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-bold text-slate-800">{snapshot.profile}</h1>
        <StatusPill status={snapshot.phase} />
        {streamClosed && (
          <span className="text-xs text-slate-400">stream closed</span>
        )}
        <span className="font-mono text-xs text-slate-400 ml-auto">{snapshot.run_id}</span>
      </div>

      {/* Progress + ETA + concurrency */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
        <Card label="Progress" value={
          snapshot.progress != null ? `${(snapshot.progress * 100).toFixed(0)}%` : '—'
        } sub={`${snapshot.extracted} / ${snapshot.listed || '?'}`} />
        <Card label="ETA" value={formatSeconds(snapshot.eta_seconds)} sub="time remaining" />
        <Card label="Concurrency" value={String(snapshot.concurrency || 0)} sub="AIMD current" />
        <Card label="Failed (DLQ)" value={String(snapshot.failed)} sub="dead-letter records"
                tone={snapshot.failed > 0 ? 'warn' : 'normal'} />
      </div>

      {/* Progress bar */}
      {snapshot.progress != null && (
        <div className="rounded-lg border border-slate-200 bg-white p-3">
          <div className="h-2 bg-slate-100 rounded-full overflow-hidden">
            <div className={`h-full rounded-full transition-all ${
              snapshot.phase === 'failed' ? 'bg-red-500'
                : snapshot.phase === 'completed' ? 'bg-emerald-500'
                : 'bg-blue-500'
            }`} style={{ width: `${(snapshot.progress * 100).toFixed(1)}%` }} />
          </div>
        </div>
      )}

      {/* Counters */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <Mini label="Listed" value={snapshot.listed} />
        <Mini label="Extracted" value={snapshot.extracted} />
        <Mini label="Failed" value={snapshot.failed} tone={snapshot.failed > 0 ? 'warn' : 'normal'} />
        <Mini label="Resumed" value={snapshot.skipped_resumed} />
        <Mini label="Rate-limit hits" value={snapshot.rate_limited_count}
               tone={snapshot.rate_limited_count > 0 ? 'warn' : 'normal'} />
      </div>

      {/* Error */}
      {snapshot.error && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-3">
          <p className="text-xs font-bold text-red-700 mb-1">Run failed</p>
          <p className="text-xs text-red-500 font-mono">{snapshot.error}</p>
        </div>
      )}

      {/* Audit log */}
      <div className="rounded-lg border border-slate-200 bg-white">
        <div className="px-4 py-2 border-b border-slate-100 flex items-center justify-between">
          <span className="text-xs font-bold text-slate-700">Audit log</span>
          <span className="text-xs text-slate-400">{events.length} events</span>
        </div>
        <div className="max-h-[320px] overflow-auto divide-y divide-slate-50">
          {events.length === 0 && (
            <div className="px-4 py-3 text-xs text-slate-400 italic">No events yet — waiting on stream…</div>
          )}
          {events.slice().reverse().map((e, i) => (
            <div key={`${e.ts}-${i}`} className="px-4 py-1.5 flex items-start gap-3">
              <span className="text-xs font-mono text-slate-400 w-[60px] shrink-0">
                {new Date(e.ts * 1000).toLocaleTimeString()}
              </span>
              <span className="text-xs font-bold text-slate-700 w-[150px] shrink-0">{e.kind}</span>
              <span className="text-xs text-slate-500 truncate">
                {Object.keys(e.payload).length > 0 ? JSON.stringify(e.payload) : ''}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ─── Tiny presentation helpers ────────────────────────────────────────

function Card({ label, value, sub, tone = 'normal' }: {
  label: string; value: string; sub?: string; tone?: 'normal' | 'warn'
}) {
  const valueColor = tone === 'warn' ? 'text-amber-600' : 'text-slate-800';
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3">
      <div className="text-xs font-bold uppercase tracking-wider text-slate-400">{label}</div>
      <div className={`text-2xl font-bold tabular-nums ${valueColor}`}>{value}</div>
      {sub && <div className="text-xs text-slate-400 mt-0.5">{sub}</div>}
    </div>
  );
}

function Mini({ label, value, tone = 'normal' }: {
  label: string; value: number; tone?: 'normal' | 'warn'
}) {
  const valueColor = tone === 'warn' ? 'text-amber-600' : 'text-slate-700';
  return (
    <div className="rounded-md border border-slate-200 bg-white px-3 py-1.5">
      <div className="text-[9px] font-bold uppercase tracking-wider text-slate-400">{label}</div>
      <div className={`text-sm font-bold tabular-nums ${valueColor}`}>{value}</div>
    </div>
  );
}

// ─── Page wrapper ─────────────────────────────────────────────────────

export default function ExtractionPage() {
  // Hash subroute: #extraction or #extraction/<run_id>
  const [runId, setRunId] = useState<string | null>(() => {
    const m = window.location.hash.match(/^#extraction\/(.+)$/);
    return m ? m[1] : null;
  });

  useEffect(() => {
    const onHashChange = () => {
      const m = window.location.hash.match(/^#extraction\/(.+)$/);
      setRunId(m ? m[1] : null);
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  // FOLLOW-3 (2026-05-19) — publish extraction context so the Copilot
  // knows which run (if any) the user is monitoring without re-fetching.
  usePageContext({
    page: 'extraction',
    filters: { view: runId ? 'monitor' : 'list', run_id: runId },
  });

  if (runId) {
    return <RunMonitor runId={runId} onBack={() => navigateTo('extraction')} />;
  }
  return <RunsList onOpen={(id) => navigateToSubRoute('extraction', id)} />;
}
