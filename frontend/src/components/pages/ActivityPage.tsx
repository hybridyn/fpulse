/**
 * ActivityPage — causal-lineage timeline view.
 *
 * Renders the unified feed from /api/v1/activity: every audit-log entry,
 * agent run, and pipeline execution in chronological order. Cycode/Wiz-
 * inspired pattern — converts the trust artifacts we already store into
 * a visible enterprise feature.
 *
 * Filter chips for kind (audit / agent / execution) and severity. Click
 * an event row to see the kind-specific details inline.
 */

import { Fragment, useEffect, useMemo, useState } from 'react';
import { useDarkMode } from '../../hooks/useDarkMode';
import HeroCard from '../shared/HeroCard';
import { api } from '../../api/client';
import { computeCostUsd, DEFAULT_RATES, type CostRates } from '../../lib/cost';

interface ActivityEvent {
  kind: 'audit' | 'agent' | 'execution';
  timestamp: string;
  actor: string;
  subject: string;
  summary: string;
  severity: 'info' | 'warning' | 'error';
  details: Record<string, unknown>;
}

interface ActivityResponse {
  events: ActivityEvent[];
  count: number;
  kind_counts: Record<string, number>;
  severity_counts: Record<string, number>;
  filter: Record<string, unknown>;
}

interface TraceStep {
  step_id: string;
  tool_name: string;
  tool_tier: string;
  outcome: string;
  decision_reason: string;
  latency_ms: number;
  redactions_applied?: Record<string, number>;
  policy_rules_fired?: string[];
}

interface TraceDetail {
  run_id: string;
  user_intent?: string;
  outcome: string;
  iterations: number;
  total_tokens_in: number;
  total_tokens_out: number;
  elapsed_ms: number;
  steps: TraceStep[];
  final_text?: string;
}

const KIND_LABELS: Record<string, string> = {
  audit: 'Audit',
  agent: 'Agent',
  execution: 'Execution',
};

const KIND_TONES_LIGHT: Record<string, string> = {
  audit: 'bg-slate-100 text-slate-700 ring-slate-200',
  agent: 'bg-indigo-100 text-indigo-700 ring-indigo-200',
  execution: 'bg-emerald-100 text-emerald-700 ring-emerald-200',
};

const KIND_TONES_DARK: Record<string, string> = {
  audit: 'bg-slate-700/40 text-slate-200 ring-slate-600',
  agent: 'bg-indigo-500/20 text-indigo-200 ring-indigo-500/30',
  execution: 'bg-emerald-500/20 text-emerald-200 ring-emerald-500/30',
};

const SEVERITY_DOT_LIGHT: Record<string, string> = {
  info: 'bg-slate-400',
  warning: 'bg-amber-500',
  error: 'bg-red-500',
};

// 2026-05-19 (P1 #10 of PAGE_BY_PAGE_AUDIT.md): the `_headers` helper
// used to hand-roll `Authorization` + `X-Workspace-Id` for raw fetch
// calls — bypassing the api client's 401 interceptor and
// backend-reachable signal. Both call sites now route through `api.get()`
// so this page inherits the global behaviour. The helper is retained as
// a no-op shim only because external callers may import it; remove in a
// follow-up sweep if unused.
function _headers(): Record<string, string> {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const token = localStorage.getItem('fpulse_token') || '';
  const ws = localStorage.getItem('fpulse_workspace_id') || 'default';
  return { 'X-Workspace-Id': ws, ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

function fmtTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  } catch {
    return iso;
  }
}

function fmtRelative(iso: string): string {
  const t = new Date(iso).getTime();
  if (!t) return '';
  const sec = Math.max(0, (Date.now() - t) / 1000);
  if (sec < 60) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
  return `${Math.round(sec / 86400)}d ago`;
}

export default function ActivityPage({ embedded = false }: { embedded?: boolean } = {}) {
  const dark = useDarkMode();
  const [data, setData] = useState<ActivityResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeKinds, setActiveKinds] = useState<Set<string>>(new Set(['audit', 'agent', 'execution']));
  const [activeSeverities, setActiveSeverities] = useState<Set<string>>(new Set(['info', 'warning', 'error']));
  const [searchTerm, setSearchTerm] = useState('');
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [traceDetails, setTraceDetails] = useState<Record<string, TraceDetail | 'loading' | 'error'>>({});
  const [rates, setRates] = useState<CostRates>(DEFAULT_RATES);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const json = await api.get<ActivityResponse>('/api/v1/activity?limit=200');
      setData(json);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load activity');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  // Fetch AI cost-rate table. Workspace-scoped so admin overrides apply.
  // We also re-fetch when SettingsPage broadcasts a save, so the tile
  // updates without requiring a page reload.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await api.get<{ rates: CostRates }>('/api/v1/ai/cost-rates');
        if (!cancelled && r?.rates) setRates(r.rates);
      } catch {
        /* fall back to DEFAULT_RATES already in state */
      }
    };
    load();
    const onChange = () => { load(); };
    window.addEventListener('fpulse-settings-changed', onChange);
    return () => {
      cancelled = true;
      window.removeEventListener('fpulse-settings-changed', onChange);
    };
  }, []);

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = searchTerm.trim().toLowerCase();
    return data.events.filter((e) => {
      if (!activeKinds.has(e.kind) || !activeSeverities.has(e.severity)) return false;
      if (!q) return true;
      const d = e.details as { model?: string; user_intent?: string } | undefined;
      const hay = `${e.actor} ${e.summary} ${d?.model || ''} ${d?.user_intent || ''}`.toLowerCase();
      return hay.includes(q);
    });
  }, [data, activeKinds, activeSeverities, searchTerm]);

  // AI activity summary — derived from agent events in the visible window.
  // Cost uses the workspace's rate table (per provider+model) so the number
  // reflects what was actually billed. Ollama → $0 (no per-token cost);
  // cloud providers → input + output priced separately at their published
  // rates. Users can override rates in Settings → AI Pricing.
  const aiSummary = useMemo(() => {
    if (!data) {
      return { runs: 0, tokens: 0, tokens_in: 0, tokens_out: 0, est_cost_usd: 0, errors: 0, all_local: true };
    }
    let runs = 0;
    let tokens_in = 0;
    let tokens_out = 0;
    let est_cost_usd = 0;
    let errors = 0;
    let any_ollama_seen = false;
    let any_non_ollama_seen = false;
    for (const e of data.events) {
      if (e.kind !== 'agent') continue;
      runs += 1;
      const d = e.details as { tokens?: number; tokens_in?: number; tokens_out?: number; provider?: string; model?: string } | undefined;
      const ti = Number(d?.tokens_in || 0);
      const to = Number(d?.tokens_out || 0);
      if (ti || to) {
        tokens_in += ti;
        tokens_out += to;
      } else {
        tokens_in += Number(d?.tokens || 0);
      }
      est_cost_usd += computeCostUsd(rates, d?.provider, d?.model, ti, to);
      const p = d?.provider?.toLowerCase();
      if (p === 'ollama') any_ollama_seen = true;
      else if (p) any_non_ollama_seen = true;
      // Unknown/null provider stays out of both buckets — we don't claim
      // "local" without positive evidence, and we don't claim "cloud" either.
      if (e.severity === 'error') errors += 1;
    }
    const tokens = tokens_in + tokens_out;
    // "Local" badge only shows when every classified run was Ollama
    // (and at least one event was classified). Legacy null-provider runs
    // do not unlock the badge — they get costed at the fallback rate.
    const all_local = any_ollama_seen && !any_non_ollama_seen;
    return { runs, tokens, tokens_in, tokens_out, est_cost_usd, errors, all_local };
  }, [data, rates]);

  // Export the currently-filtered events as CSV. The point is operator
  // visibility — admins need to hand auditors / finance a flat table of what
  // the AI did, what it cost, and which model served the request. Honours
  // active KINDS/SEVERITY/search filters so users export what they see.
  function exportCsv() {
    const esc = (v: unknown): string => {
      const s = v == null ? '' : String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const headers = ['kind', 'actor', 'model', 'provider', 'tokens_in', 'tokens_out', 'tokens_total', 'summary', 'timestamp', 'severity'];
    const lines = [headers.join(',')];
    for (const ev of filtered) {
      const d = ev.details as { model?: string; provider?: string; tokens_in?: number; tokens_out?: number; tokens?: number } | undefined;
      lines.push([
        ev.kind,
        ev.actor,
        d?.model || '',
        d?.provider || '',
        d?.tokens_in ?? '',
        d?.tokens_out ?? '',
        d?.tokens ?? '',
        ev.summary,
        ev.timestamp,
        ev.severity,
      ].map(esc).join(','));
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const a = document.createElement('a');
    a.href = url;
    a.download = `fpulse-activity-${stamp}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  // Fetch the rich trace detail (with steps) when an agent row is expanded.
  async function fetchTraceDetail(runId: string) {
    setTraceDetails((cur) => ({ ...cur, [runId]: 'loading' }));
    try {
      const trace = await api.get<TraceDetail>(`/api/ai/agent/trace/${encodeURIComponent(runId)}`);
      setTraceDetails((cur) => ({ ...cur, [runId]: trace }));
    } catch {
      setTraceDetails((cur) => ({ ...cur, [runId]: 'error' }));
    }
  }

  const toggleKind = (k: string) => {
    setActiveKinds((s) => {
      const n = new Set(s);
      if (n.has(k)) n.delete(k); else n.add(k);
      // Don't allow zero kinds — keeping at least one selected.
      return n.size === 0 ? s : n;
    });
  };

  const toggleSev = (s: string) => {
    setActiveSeverities((cur) => {
      const n = new Set(cur);
      if (n.has(s)) n.delete(s); else n.add(s);
      return n.size === 0 ? cur : n;
    });
  };

  const KIND_TONES = dark ? KIND_TONES_DARK : KIND_TONES_LIGHT;

  // Body content — used both for standalone page and as embedded section
  // in AIPage. When `embedded`, the parent page provides the chrome (sticky
  // PROD/DEV banner, tab bar, max-width wrapper) so we render the inner
  // content only.
  const Body = (
    <>
      {/* HeroCard KPI strip — matches Executions / Pool style: 4 gradient
          cards with icons. Replaces the previous compact white tiles
          (which were too understated for the Insights landing). */}
      <div className={embedded ? '' : 'w-full px-8 pt-4'}>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <HeroCard
            gradient="from-violet-400 to-indigo-500"
            icon={
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 2l2.09 6.26L20 10l-5.91 1.74L12 18l-2.09-6.26L4 10l5.91-1.74z" />
              </svg>
            }
            label="Agent Runs"
            value={String(aiSummary.runs)}
          />
          <HeroCard
            gradient="from-sky-400 to-blue-500"
            icon={
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="4" y1="9" x2="20" y2="9" /><line x1="4" y1="15" x2="20" y2="15" />
                <line x1="10" y1="3" x2="8" y2="21" /><line x1="16" y1="3" x2="14" y2="21" />
              </svg>
            }
            label="Tokens"
            value={aiSummary.tokens.toLocaleString()}
            footer={`Input ${aiSummary.tokens_in.toLocaleString()} · Output ${aiSummary.tokens_out.toLocaleString()}`}
          />
          <HeroCard
            gradient="from-emerald-400 to-teal-500"
            icon={
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="12" y1="1" x2="12" y2="23" /><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" />
              </svg>
            }
            label="Est. Cost"
            value={aiSummary.all_local ? '$0.00' : `$${aiSummary.est_cost_usd.toFixed(4)}`}
            footer={aiSummary.all_local ? 'local — no per-token cost' : 'per-model rates · edit in Settings'}
          />
          <HeroCard
            gradient={aiSummary.errors > 0 ? 'from-red-400 to-rose-500' : 'from-slate-400 to-slate-500'}
            icon={
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10" /><line x1="15" y1="9" x2="9" y2="15" /><line x1="9" y1="9" x2="15" y2="15" />
              </svg>
            }
            label="Failed Runs"
            value={String(aiSummary.errors)}
          />
        </div>
      </div>

      {/* Filters + Table */}
      <div className={embedded ? 'pt-4 space-y-3' : 'w-full px-8 py-4 space-y-3'}>
        {/* Filter toolbar — same card style as the KPI tiles for consistency. */}
        <div
          className={`rounded-xl border shadow-sm px-3 py-2.5 flex items-center gap-2 flex-wrap ${
            dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'
          }`}
        >
          <span className={`text-xs font-bold uppercase tracking-wider ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
            Kinds:
          </span>
          {(['audit', 'agent', 'execution'] as const).map((k) => {
            const active = activeKinds.has(k);
            const count = data?.kind_counts[k] || 0;
            return (
              <button
                key={k}
                type="button"
                onClick={() => toggleKind(k)}
                className={`px-2.5 py-1 text-xs font-semibold rounded-md ring-1 transition-colors ${
                  active ? KIND_TONES[k] : (dark ? 'bg-transparent text-slate-500 ring-white/[0.08]' : 'bg-white text-slate-400 ring-slate-200')
                }`}
              >
                {KIND_LABELS[k]}
                <span className="ml-1.5 opacity-70">{count}</span>
              </button>
            );
          })}
          <span className={`mx-2 text-xs ${dark ? 'text-slate-600' : 'text-slate-300'}`}>·</span>
          <span className={`text-xs font-bold uppercase tracking-wider ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
            Severity:
          </span>
          {(['info', 'warning', 'error'] as const).map((s) => {
            const active = activeSeverities.has(s);
            const count = data?.severity_counts[s] || 0;
            return (
              <button
                key={s}
                type="button"
                onClick={() => toggleSev(s)}
                className={`inline-flex items-center gap-1 px-2 py-1 text-xs font-semibold rounded-md ring-1 transition-colors ${
                  active
                    ? (dark ? 'bg-white/[0.06] text-slate-200 ring-white/[0.1]' : 'bg-white text-slate-700 ring-slate-300')
                    : (dark ? 'text-slate-500 ring-white/[0.05]' : 'text-slate-400 ring-slate-200')
                }`}
              >
                <span className={`inline-block w-2 h-2 rounded-full ${SEVERITY_DOT_LIGHT[s]}`} />
                {s}
                <span className="ml-0.5 opacity-70">{count}</span>
              </button>
            );
          })}
          <div className="ml-auto flex items-center gap-1.5">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={dark ? 'text-slate-500' : 'text-slate-400'}>
              <circle cx="11" cy="11" r="7" />
              <line x1="21" y1="21" x2="16.65" y2="16.65" />
            </svg>
            <input
              type="text"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              placeholder="Search actor, summary, model…"
              className={`px-2 py-1 text-xs rounded-md ring-1 outline-none focus:ring-2 w-56 ${
                dark
                  ? 'bg-white/[0.04] text-slate-200 ring-white/[0.08] focus:ring-amber-400/60 placeholder:text-slate-500'
                  : 'bg-white text-slate-700 ring-slate-200 focus:ring-amber-400 placeholder:text-slate-400'
              }`}
            />
            {searchTerm && (
              <button
                type="button"
                onClick={() => setSearchTerm('')}
                className={`text-xs px-1.5 py-0.5 rounded ${dark ? 'text-slate-400 hover:text-slate-200' : 'text-slate-500 hover:text-slate-800'}`}
                aria-label="Clear search"
              >
                ✕
              </button>
            )}
          </div>
        </div>

        {error && (
          <div className={`rounded-lg border p-3 text-[12px] ${dark ? 'bg-red-500/10 border-red-500/20 text-red-300' : 'bg-red-50 border-red-200 text-red-700'}`}>
            Failed to load activity: {error}
          </div>
        )}

        {!loading && !error && filtered.length === 0 && (
          <div className={`rounded-lg border p-6 text-center text-sm ${dark ? 'bg-[#111827] border-white/[0.08] text-slate-400' : 'bg-white border-slate-200 text-slate-500'}`}>
            No events in this window. Trigger a pipeline or talk to the Copilot to see entries here.
          </div>
        )}

        {/* Timeline table — uses the canonical thead-dev tokens (slate-700
            header bg + amber-300 text) so it matches Projects / Executions /
            every other table in the app. tableCard token = `border-2
            border-thead-dev-border rounded-lg overflow-hidden shadow-sm bg-white`. */}
        <div className="rounded-lg border-2 border-thead-dev-border overflow-hidden shadow-sm bg-white">
          {/* Toolbar strip — matches TableToolbar pattern: dark band with
              counts (left) and actions (right). Mirrors Projects "1 project ·
              9/15 columns" toolbar. */}
          <div className="bg-thead-dev-bg px-4 py-2.5 flex items-center justify-between gap-3 flex-wrap">
            <div className="text-[12px] text-white/85 font-medium">
              {data ? `${filtered.length} of ${data.count} events` : 'Loading…'}
              <span className="text-white/40 mx-1.5">·</span>
              <span className="text-white/60">workspace-scoped · last 7 days</span>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={exportCsv}
                disabled={filtered.length === 0}
                title={filtered.length === 0 ? 'Nothing to export' : `Export ${filtered.length} event${filtered.length === 1 ? '' : 's'} as CSV`}
                className="px-2.5 py-1 text-xs font-semibold text-white bg-white/10 hover:bg-white/20 rounded ring-1 ring-white/15 transition-colors disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-1.5"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                  <polyline points="7 10 12 15 17 10" />
                  <line x1="12" y1="15" x2="12" y2="3" />
                </svg>
                Export CSV
              </button>
              <button
                type="button"
                onClick={refresh}
                disabled={loading}
                className="px-2.5 py-1 text-xs font-semibold text-white bg-white/10 hover:bg-white/20 rounded ring-1 ring-white/15 transition-colors disabled:opacity-50"
              >
                {loading ? 'Loading…' : 'Refresh'}
              </button>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-[12px] table-fixed">
              <colgroup>
                <col className="w-[80px]" />
                <col className="w-[120px]" />
                <col className="w-[150px]" />
                <col />
                <col className="w-[140px]" />
                <col className="w-[140px]" />
                <col className="w-[80px]" />
              </colgroup>
              <thead className="bg-thead-dev-bg text-thead-dev-text border-b-2 border-thead-dev-border">
                <tr className="text-xs font-bold uppercase tracking-wider">
                  <th className="text-left px-3 py-2">Kind</th>
                  <th className="text-left px-3 py-2">Actor</th>
                  <th className="text-left px-3 py-2">Model</th>
                  <th className="text-left px-3 py-2">Summary</th>
                  <th className="text-left px-3 py-2">Tokens</th>
                  <th className="text-left px-3 py-2">When</th>
                  <th className="text-left px-3 py-2">Severity</th>
                </tr>
              </thead>
              <tbody className={dark ? 'divide-y divide-white/[0.04]' : 'divide-y divide-slate-100'}>
                {filtered.map((ev, i) => {
                  const isExpanded = expanded.has(i);
                  return (
                    <Fragment key={i}>
                      <tr
                        className={`cursor-pointer transition-colors ${
                          dark ? 'hover:bg-white/[0.02]' : 'hover:bg-slate-50/60'
                        }`}
                        onClick={() => {
                          setExpanded((s) => {
                            const n = new Set(s);
                            if (n.has(i)) n.delete(i); else n.add(i);
                            return n;
                          });
                          if (ev.kind === 'agent' && ev.subject && !traceDetails[ev.subject]) {
                            fetchTraceDetail(ev.subject);
                          }
                        }}
                      >
                        <td className="px-3 py-2">
                          <span className={`text-xs font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ring-1 ${KIND_TONES[ev.kind]}`}>
                            {KIND_LABELS[ev.kind]}
                          </span>
                        </td>
                        <td className="px-3 py-2">
                          <code className={`font-mono text-xs truncate block ${dark ? 'text-slate-400' : 'text-slate-600'}`}>
                            {ev.actor}
                          </code>
                        </td>
                        <td className="px-3 py-2">
                          {(() => {
                            const d = ev.details as { model?: string; provider?: string } | undefined;
                            const label = d?.model || d?.provider;
                            if (!label) {
                              return <span className={dark ? 'text-slate-600' : 'text-slate-400'}>—</span>;
                            }
                            const title = d?.model && d?.provider ? `${d.provider} · ${d.model}` : label;
                            return (
                              <code
                                title={title}
                                className={`font-mono text-xs truncate block ${dark ? 'text-slate-300' : 'text-slate-700'}`}
                              >
                                {label}
                              </code>
                            );
                          })()}
                        </td>
                        <td className={`px-3 py-2 truncate ${dark ? 'text-slate-200' : 'text-slate-800'}`}>
                          {ev.summary}
                        </td>
                        <td className="px-3 py-2">
                          {(() => {
                            const d = ev.details as { tokens?: number; tokens_in?: number; tokens_out?: number } | undefined;
                            const total = Number(d?.tokens || 0);
                            const ti = Number(d?.tokens_in || 0);
                            const to = Number(d?.tokens_out || 0);
                            if (!total && !ti && !to) {
                              return <span className={dark ? 'text-slate-600' : 'text-slate-400'}>—</span>;
                            }
                            return (
                              <div className="leading-tight">
                                <div className={`text-xs font-semibold tabular-nums ${dark ? 'text-slate-200' : 'text-slate-800'}`}>
                                  {(total || ti + to).toLocaleString()}
                                </div>
                                <div className={`text-xs tabular-nums ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
                                  in {ti.toLocaleString()} · out {to.toLocaleString()}
                                </div>
                              </div>
                            );
                          })()}
                        </td>
                        <td className={`px-3 py-2 text-xs tabular-nums ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
                          <div>{fmtRelative(ev.timestamp)}</div>
                          <div className={`text-xs ${dark ? 'text-slate-600' : 'text-slate-400'}`}>{fmtTime(ev.timestamp)}</div>
                        </td>
                        <td className="px-3 py-2">
                          <span className="inline-flex items-center gap-1.5">
                            <span className={`inline-block w-2 h-2 rounded-full ${SEVERITY_DOT_LIGHT[ev.severity]}`} />
                            <span className={`text-xs capitalize ${dark ? 'text-slate-300' : 'text-slate-600'}`}>
                              {ev.severity}
                            </span>
                          </span>
                        </td>
                      </tr>
                      {isExpanded && (
                        <tr className={dark ? 'bg-white/[0.02]' : 'bg-slate-50/40'}>
                          <td colSpan={7} className="px-3 py-3">
                            {ev.kind === 'agent' && ev.subject ? (
                        (() => {
                          const td = traceDetails[ev.subject as string];
                          if (td === 'loading') {
                            return <div className={`text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`}>Loading trace…</div>;
                          }
                          if (td === 'error' || !td) {
                            return td === 'error'
                              ? <div className={`text-xs ${dark ? 'text-amber-300' : 'text-amber-700'}`}>Couldn't load trace details.</div>
                              : null;
                          }
                          const trace = td as TraceDetail;
                          return (
                            <div className="space-y-2">
                              {trace.user_intent && (
                                <div className={`text-xs italic ${dark ? 'text-slate-400' : 'text-slate-600'}`}>
                                  "{trace.user_intent}"
                                </div>
                              )}
                              <div className={`flex flex-wrap items-center gap-2 text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
                                <span>iter <b className={dark ? 'text-slate-200' : 'text-slate-700'}>{trace.iterations}</b></span>
                                <span>·</span>
                                <span>tok in <b className={dark ? 'text-slate-200' : 'text-slate-700'}>{trace.total_tokens_in}</b></span>
                                <span>·</span>
                                <span>tok out <b className={dark ? 'text-slate-200' : 'text-slate-700'}>{trace.total_tokens_out}</b></span>
                                <span>·</span>
                                <span>elapsed <b className={dark ? 'text-slate-200' : 'text-slate-700'}>{trace.elapsed_ms}ms</b></span>
                                <span>·</span>
                                <span>outcome <b className={trace.outcome === 'success' ? 'text-emerald-500' : 'text-amber-500'}>{trace.outcome}</b></span>
                              </div>
                              {trace.steps && trace.steps.length > 0 && (
                                <div className={`rounded border ${dark ? 'border-white/[0.05]' : 'border-slate-200'} divide-y ${dark ? 'divide-white/[0.04]' : 'divide-slate-100'}`}>
                                  {trace.steps.map((st) => (
                                    <div key={st.step_id} className="px-2 py-1.5 flex items-center gap-2 flex-wrap">
                                      <code className={`font-mono text-xs ${dark ? 'text-indigo-300' : 'text-indigo-700'}`}>{st.tool_name}</code>
                                      <span className={`text-[9px] uppercase font-bold tracking-wider px-1.5 py-0.5 rounded ${st.tool_tier === 'read' ? (dark ? 'bg-slate-700/40 text-slate-300' : 'bg-slate-100 text-slate-600') : (dark ? 'bg-amber-500/20 text-amber-300' : 'bg-amber-100 text-amber-700')}`}>{st.tool_tier}</span>
                                      <span className={`text-xs font-semibold ${st.outcome === 'success' ? (dark ? 'text-emerald-300' : 'text-emerald-700') : (dark ? 'text-red-300' : 'text-red-600')}`}>{st.outcome}</span>
                                      <span className={`text-xs ${dark ? 'text-slate-500' : 'text-slate-400'}`}>{st.latency_ms}ms</span>
                                      {st.policy_rules_fired && st.policy_rules_fired.length > 0 && (
                                        <span className={`text-xs ${dark ? 'text-amber-300' : 'text-amber-700'}`}>
                                          policy: {st.policy_rules_fired.join(', ')}
                                        </span>
                                      )}
                                      <span className={`flex-1 truncate text-xs ${dark ? 'text-slate-500' : 'text-slate-500'}`} title={st.decision_reason}>
                                        {st.decision_reason}
                                      </span>
                                    </div>
                                  ))}
                                </div>
                              )}
                              {trace.final_text && (
                                <div className={`rounded p-2 text-xs ${dark ? 'bg-slate-900 text-slate-300' : 'bg-slate-50 text-slate-700 border border-slate-200'}`}>
                                  {trace.final_text.slice(0, 800)}
                                  {trace.final_text.length > 800 && '…'}
                                </div>
                              )}
                            </div>
                          );
                        })()
                      ) : (
                        <pre className={`p-2 rounded text-xs font-mono whitespace-pre-wrap break-words ${dark ? 'bg-slate-900 text-emerald-300' : 'bg-slate-50 text-slate-700 border border-slate-200'}`}>
{JSON.stringify(ev.details, null, 2)}
                        </pre>
                      )}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        {data && (
          <div className={`text-center text-xs pt-2 ${dark ? 'text-slate-600' : 'text-slate-400'}`}>
            {filtered.length} of {data.count} events shown · workspace-scoped · last 7 days by default
          </div>
        )}
      </div>
    </>
  );

  // Embedded mode: just the body. Parent provides chrome (page header + tab
  // bar). The table card has its own toolbar with the Refresh action so we
  // don't duplicate it at the top.
  if (embedded) {
    return <div className="space-y-3">{Body}</div>;
  }

  // Standalone mode: full page with own header (legacy /#activity route).
  return (
    <div className={`flex-1 overflow-auto ${dark ? 'bg-[#0b1120]' : 'bg-canvas-bg'}`}>
      <div className={`border-b ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
        <div className="w-full max-w-[1500px] mx-auto px-8 py-5">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div>
              <h1 className={`text-xl font-bold ${dark ? 'text-slate-100' : 'text-slate-800'}`}>Activity timeline</h1>
              <p className={`text-xs mt-1 max-w-2xl ${dark ? 'text-slate-400' : 'text-slate-600'}`}>
                Every audit-log entry, agent run, and pipeline execution in chronological order — workspace-scoped causal trace.
              </p>
            </div>
            <button
              type="button"
              onClick={refresh}
              disabled={loading}
              className={`px-3 py-1.5 text-xs font-semibold rounded-lg ring-1 ${
                dark
                  ? 'bg-white/[0.06] hover:bg-white/[0.1] text-slate-200 ring-white/[0.08]'
                  : 'bg-white hover:bg-slate-100 text-slate-700 ring-slate-300'
              } ${loading ? 'opacity-50' : ''}`}
            >
              {loading ? 'Loading…' : 'Refresh'}
            </button>
          </div>
        </div>
      </div>
      {Body}
    </div>
  );
}
