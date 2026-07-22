/**
 * ExecutionPoolPage — Spark-style worker pool monitoring & admin.
 *
 * Admin page showing:
 *  - Pool summary cards (workers, utilization, throughput, queue)
 *  - Live worker grid (which pipeline on each worker)
 *  - Priority queue (waiting jobs)
 *  - Run history timeline with duration bars
 *  - Pool configuration
 *
 * Auto-refreshes every 2 seconds for live feel.
 */

import { useCallback, useEffect, useState } from 'react';
import { navigateToSubRoute } from '../../router';
import { api } from '../../api/client';
import TableToolbar, { useTableColumns, type TColumn, type TColumnGroup } from '../shared/TableToolbar';
import HeroCard from '../shared/HeroCard';
import { useDarkMode } from '../../hooks/useDarkMode';
import { canAccessAdmin } from '../../auth/permissions';
import TierChip from '../shared/TierChip';
import PageHeader from '../shared/PageHeader';
import StatusPill from '../shared/StatusPill';
import { usePageContext } from '../../hooks/usePageContext';
import { toast } from '../Toast';

/* 2026-06-03 — copy-to-clipboard helper for the read-only env-var
   tuning rows on Pool → Configuration. Produces the canonical bash
   shape (`export FPULSE_X=value`) since that's the format that copies
   cleanly into Dockerfiles, docker-compose `environment:` blocks,
   systemd `EnvironmentFile=` paths, and most CI runners. PowerShell
   users substitute `$env:FPULSE_X = "value"`; the toast tells them so. */
async function copyEnvAssignment(envName: string, value: string | number): Promise<void> {
  const cmd = `export ${envName}=${value}`;
  try {
    await navigator.clipboard.writeText(cmd);
    toast.success(
      `Copied: ${envName}`,
      `${cmd} — paste into your env file, systemd unit, or shell. PowerShell: $env:${envName} = "${value}"`,
    );
  } catch {
    toast.error(
      'Copy failed',
      'Clipboard access is blocked. Select the row text manually.',
    );
  }
}

// ── Priority colors ────────────────────────────────────────────────
const PRIORITY_COLORS: Record<number, { bg: string; text: string; ring: string; label: string }> = {
  1: { bg: 'bg-red-100', text: 'text-red-700', ring: 'ring-red-300', label: 'P1 Critical' },
  2: { bg: 'bg-orange-100', text: 'text-orange-700', ring: 'ring-orange-300', label: 'P2 High' },
  3: { bg: 'bg-blue-100', text: 'text-blue-700', ring: 'ring-blue-300', label: 'P3 Normal' },
  4: { bg: 'bg-slate-100', text: 'text-slate-600', ring: 'ring-slate-300', label: 'P4 Low' },
  5: { bg: 'bg-slate-50', text: 'text-slate-400', ring: 'ring-slate-200', label: 'P5 Background' },
};

function priorityStyle(p: number) {
  return PRIORITY_COLORS[p] || PRIORITY_COLORS[3];
}

function EnvBadge({ env }: { env: string }) {
  const isProd = env === 'prod';
  return (
    <span className={`text-xs font-bold uppercase tracking-wide px-1.5 py-0.5 rounded ${
      isProd ? 'bg-red-100 text-red-700' : 'bg-emerald-100 text-emerald-700'
    }`}>
      {isProd ? 'PROD' : 'DEV'}
    </span>
  );
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  if (ms < 3600000) return `${(ms / 60000).toFixed(1)}m`;
  return `${(ms / 3600000).toFixed(1)}h`;
}

function formatTime(iso: string): string {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

// HeroCard now lives in components/shared/HeroCard.tsx — imported above.
// Both Pool and Executions pages render identical-looking KPI cards
// from that single definition, so visual drift between pages is impossible.


// ── Run History column config ──────────────────────────────────────
const RUN_COLUMNS: TColumn[] = [
  { key: 'pipeline', label: 'Pipeline', default: true, group: 'core' },
  { key: 'priority', label: 'Priority', default: true, group: 'core' },
  { key: 'environment', label: 'Environment', default: true, group: 'core' },
  { key: 'worker', label: 'Worker', default: true, group: 'core' },
  { key: 'status', label: 'Status', default: true, group: 'core' },
  { key: 'trigger', label: 'Trigger', default: true, group: 'core' },
  { key: 'duration', label: 'Duration', default: true, group: 'core' },
  { key: 'wait', label: 'Wait Time', default: false, group: 'details' },
  { key: 'queued', label: 'Queued At', default: false, group: 'details' },
  { key: 'started', label: 'Started', default: true, group: 'details' },
  { key: 'finished', label: 'Finished', default: false, group: 'details' },
  { key: 'steps', label: 'Steps', default: false, group: 'details' },
  { key: 'rows', label: 'Rows Processed', default: false, group: 'details' },
  { key: 'error', label: 'Error', default: false, group: 'details' },
  // Pass 2 (server-side join with executions table) columns:
  { key: 'memory', label: 'Peak Memory (MB)', default: true, group: 'resources' },
  { key: 'cpu', label: 'CPU (sec)', default: true, group: 'resources' },
];
const RUN_GROUPS: TColumnGroup[] = [
  { key: 'core', label: 'Core', icon: '◆' },
  { key: 'details', label: 'Details', icon: '◇' },
  { key: 'resources', label: 'Resources', icon: '⚙' },
];

export default function ExecutionPoolPage({
  environment = 'dev',
  // Safer default — fall back to OSS Free behavior if a caller forgets
  // to pass `tier`. Previously defaulted to 'plus', which would leak
  // PROD UI (env filter chips, env badges) on Free installs if the
  // prop wasn't wired through.
  tier = 'free',
}: {
  environment?: 'dev' | 'prod';
  tier?: 'free' | 'plus';
}) {
  // OSS view: no PROD environment exists, so the env-split panel is
  // hidden when tier === 'free'.
  const isFree = tier === 'free';
  const isProd = environment === 'prod';
  const dark = useDarkMode();
  const [status, setStatus] = useState<any>(null);
  const [history, setHistory] = useState<any[]>([]);
  const [config, setConfig] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  // 2026-06-01: read initial tab from URL hash + sync on Back/Forward
  // so deep-links like `#pool/history` work and browser Back steps
  // through visited tabs instead of jumping to Dashboard.
  const [tab, setTab] = useState<'overview' | 'history' | 'config' | 'alerts'>(() => {
    try {
      const seg = (window.location.hash || '').split('/')[1];
      if (seg === 'overview' || seg === 'history' || seg === 'config' || seg === 'alerts') {
        return seg;
      }
    } catch { /* SSR */ }
    return 'overview';
  });
  useEffect(() => {
    const onHash = () => {
      const seg = (window.location.hash || '').split('/')[1];
      if (seg === 'overview' || seg === 'history' || seg === 'config' || seg === 'alerts') {
        setTab(seg);
      }
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  // ── Pool compute-usage alerts (PROD only, Plus surface) ──
  // Users in PROD configure rules like "notify when utilization > 80% for
  // 10 minutes" or "queue depth > 20".
  //
  // 2026-05-19 (P0 #4 of PAGE_BY_PAGE_AUDIT.md): rules are PREVIEW-ONLY in
  // this build — they live in component state and do NOT persist across
  // reloads, and the watchdog daemon does NOT evaluate them. A persistent
  // alert-rule API is a Plus v1.0 deliverable. Until then the surface is
  // kept visible so admins can shape the rule vocabulary, but every entry
  // path is labelled as preview and the "Create Rule" CTA pops an explicit
  // confirm that this is session-only.
  interface PoolAlertRule {
    id: string;
    name: string;
    metric: 'utilization_pct' | 'queue_depth' | 'throughput_per_hour' | 'error_rate_pct' | 'busy_workers';
    operator: '>' | '<' | '>=' | '<=' | '==';
    threshold: number;
    window_minutes: number;
    channels: string[];  // 'email' | 'slack' | 'webhook'
    enabled: boolean;
    created_at?: string;
    created_by?: string;
    last_triggered?: string;
  }
  const [poolAlerts, setPoolAlerts] = useState<PoolAlertRule[]>([]);
  const [newAlertOpen, setNewAlertOpen] = useState(false);
  const [draftAlert, setDraftAlert] = useState<Partial<PoolAlertRule>>({
    name: '',
    metric: 'utilization_pct',
    operator: '>',
    threshold: 80,
    window_minutes: 10,
    channels: ['email'],
    enabled: true,
  });
  const [envFilter, setEnvFilter] = useState<'all' | 'dev' | 'prod'>('all');
  // Run-history filter chips (Pass 1, May 10 2026): status / priority /
  // trigger filters complement the existing search + env. All four
  // compose with AND-semantics. Defaults are 'all' so the page reads
  // identically until a chip is clicked.
  const [statusFilter, setStatusFilter] = useState<'all' | 'success' | 'error' | 'timed_out' | 'cancelled'>('all');
  const [priorityFilter, setPriorityFilter] = useState<'all' | 1 | 2 | 3 | 4 | 5>('all');
  const [triggerFilter, setTriggerFilter] = useState<'all' | string>('all');
  const runColState = useTableColumns('fpulse_pool_runs_cols', RUN_COLUMNS);
  const [searchQuery, setSearchQuery] = useState('');
  // Pool is visible to every authenticated user (developers need to see
  // queue pressure on their runs), but creating / editing alert rules is
  // admin-only. `isAdmin` gates the "New Alert Rule" button below and
  // the per-row delete action in the rules list.
  const currentUser = (() => {
    try { return JSON.parse(localStorage.getItem('fpulse_user') || 'null'); }
    catch { return null; }
  })();
  const isAdmin = canAccessAdmin(currentUser);

  // PR14 — pool allocation slider state. Plus-only; on Free we render
  // a simple "all DEV" readout instead of the slider.
  const [allocation, setAllocation] = useState<{
    prod_reserved_pct: number;
    dev_reserved_pct: number;
    burst_pct: number;
    total_workers: number;
    slots: { prod: number; dev: number; burst: number };
  } | null>(null);
  // Pending edits before the user clicks Save. Mirrors `allocation`
  // when not editing; diverges while sliders are dragged.
  const [allocDraft, setAllocDraft] = useState<{ prod: number; dev: number; burst: number } | null>(null);
  const [allocSaving, setAllocSaving] = useState(false);

  // Live-poll toggle — when paused, the 2s interval is skipped and the
  // user updates with the manual Refresh button. Reduces network noise
  // when reading the page or debugging a frozen state.
  const [livePolling, setLivePolling] = useState(true);
  // Idle-worker grid collapse — when no workers are busy, render a single
  // strip ("All N workers idle") instead of a 20-card grid. User can
  // expand to see per-worker job-count detail. Density fix for solo-dev
  // laptops where the pool is quiet most of the time.
  const [idleExpanded, setIdleExpanded] = useState(false);

  // Connection-pool stats — separate from worker pool. The connection
  // pool caches DB driver connections per-run (Critical #5 / Phase 2-5)
  // so multi-step pipelines amortise the 50-150ms DB connection setup
  // cost. Refreshes alongside the worker-pool poll.
  const [connPool, setConnPool] = useState<{
    installed: boolean;
    total_entries: number;
    by_connection: Record<string, number>;
    by_run: Record<string, number>;
    max_per_connection: number;
  } | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, h, cp] = await Promise.all([
        api.getPoolStatus(),
        api.getPoolHistory(200),
        api.getConnectionPoolStats().catch(() => null),
      ]);
      setStatus(s);
      setHistory(h || []);
      setConnPool(cp);
    } catch { /* ignore */ }
    setLoading(false);
  }, []);

  const refreshAllocation = useCallback(async () => {
    if (isFree) return;  // Free has no PROD/DEV split to manage.
    try {
      const a = await api.getPoolAllocation();
      setAllocation({
        prod_reserved_pct: a.prod_reserved_pct,
        dev_reserved_pct: a.dev_reserved_pct,
        burst_pct: a.burst_pct,
        total_workers: a.total_workers ?? 0,
        slots: a.slots || { prod: 0, dev: 0, burst: 0 },
      });
    } catch { /* tolerant — admin card hides if it fails */ }
  }, [isFree]);

  // Auto-rebalance the other two sliders to maintain sum=100. Drag prod
  // → dev shrinks proportionally, burst takes the rounding remainder.
  // Same UX pattern as Vercel / AWS Console proportional sliders.
  const setProdPct = (next: number) => {
    if (!allocDraft) return;
    const clamped = Math.max(0, Math.min(100, Math.round(next)));
    const others = 100 - clamped;
    const oldOthers = allocDraft.dev + allocDraft.burst;
    let dev: number;
    let burst: number;
    if (oldOthers <= 0) {
      // Even split for the remainder.
      dev = Math.floor(others / 2);
      burst = others - dev;
    } else {
      dev = Math.round(others * (allocDraft.dev / oldOthers));
      burst = others - dev;
    }
    setAllocDraft({ prod: clamped, dev: Math.max(0, dev), burst: Math.max(0, burst) });
  };
  const setDevPct = (next: number) => {
    if (!allocDraft) return;
    const clamped = Math.max(0, Math.min(100 - allocDraft.prod, Math.round(next)));
    const burst = 100 - allocDraft.prod - clamped;
    setAllocDraft({ prod: allocDraft.prod, dev: clamped, burst: Math.max(0, burst) });
  };

  const allocDirty = allocation !== null && allocDraft !== null && (
    allocDraft.prod !== allocation.prod_reserved_pct ||
    allocDraft.dev !== allocation.dev_reserved_pct ||
    allocDraft.burst !== allocation.burst_pct
  );
  const allocValid = allocDraft !== null && (allocDraft.prod + allocDraft.dev + allocDraft.burst === 100);

  const saveAllocation = async () => {
    if (!allocDraft || !allocValid) return;
    setAllocSaving(true);
    try {
      const r = await api.updatePoolAllocation({
        prod_reserved_pct: allocDraft.prod,
        dev_reserved_pct: allocDraft.dev,
        burst_pct: allocDraft.burst,
      });
      setAllocation({
        prod_reserved_pct: r.prod_reserved_pct,
        dev_reserved_pct: r.dev_reserved_pct,
        burst_pct: r.burst_pct,
        total_workers: r.total_workers ?? 0,
        slots: r.slots || { prod: 0, dev: 0, burst: 0 },
      });
    } catch (e: any) {
      // Toast is the user's signal; we let `allocation` keep the prior value.
      // eslint-disable-next-line no-console
      console.error('Pool allocation save failed:', e);
    } finally {
      setAllocSaving(false);
    }
  };

  const cancelAllocEdit = () => {
    if (!allocation) return;
    setAllocDraft({
      prod: allocation.prod_reserved_pct,
      dev: allocation.dev_reserved_pct,
      burst: allocation.burst_pct,
    });
  };

  // Load the allocation once on mount + refresh every 10s (slot capacity
  // changes when admins drag the slider in another tab; busy counts also
  // shift). Skip the auto-refresh on Free tier — no allocation card there.
  useEffect(() => {
    refreshAllocation();
    if (isFree) return;
    const interval = setInterval(refreshAllocation, 10_000);
    return () => clearInterval(interval);
  }, [refreshAllocation, isFree]);

  // Sync draft when allocation lands or refreshes.
  useEffect(() => {
    if (allocation && !allocDraft) {
      setAllocDraft({
        prod: allocation.prod_reserved_pct,
        dev: allocation.dev_reserved_pct,
        burst: allocation.burst_pct,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allocation]);

  // Initial + config load
  useEffect(() => {
    refresh();
    api.getPoolConfig().then(setConfig).catch(() => {});
  }, [refresh]);

  // Auto-refresh every 2s — pauses when livePolling is off.
  useEffect(() => {
    if (!livePolling) return;
    const interval = setInterval(refresh, 2000);
    return () => clearInterval(interval);
  }, [refresh, livePolling]);

  const pool = status?.pool || {};
  const counters = status?.counters || {};
  const throughput = status?.throughput || {};
  const envs = status?.environments || {};
  const workers = status?.workers || [];
  const queue = status?.queue || [];
  const active = status?.active || [];

  // "Running long" detection (2026-06-16): per-pipeline duration baselines from
  // run history. A running job whose elapsed exceeds ~1.5× its own pipeline's
  // p95 (with >=3 successful prior runs) gets a badge — so an abnormally slow /
  // stuck run is obvious without watching the clock. Frontend-only: reuses the
  // history the page already polls; no backend change.
  const LONG_RUN_FACTOR = 1.5;
  const pipelineBaselines: Record<string, number> = (() => {
    const byKey: Record<string, number[]> = {};
    for (const h of history) {
      if ((h.status && h.status !== 'success') || !h.duration_ms) continue;
      const key = h.workflow_id || h.workflow_name;
      if (!key) continue;
      (byKey[key] = byKey[key] || []).push(h.duration_ms);
    }
    const out: Record<string, number> = {};
    for (const [key, arr] of Object.entries(byKey)) {
      if (arr.length < 3) continue;
      const sorted = arr.slice().sort((a, b) => a - b);
      out[key] = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))];
    }
    return out;
  })();
  const runningLong = (w: any): { over: boolean; ratio: number } => {
    const key = w.current_workflow_id || w.current_workflow_name;
    const base = key ? pipelineBaselines[key] : undefined;
    if (!base || !w.elapsed_ms) return { over: false, ratio: 0 };
    const ratio = w.elapsed_ms / base;
    return { over: ratio >= LONG_RUN_FACTOR, ratio };
  };
  const longRunningCount = workers.filter((w: any) => w.status === 'busy' && runningLong(w).over).length;

  // OSS-4 (2026-05-19) — publish page context so the Copilot can answer
  // "what's saturated?" / "why is the queue backed up?" without firing
  // its own /pool/status call.
  usePageContext({
    page: 'pool',
    visible_items: [{
      id: 'pool',
      kind: 'pool',
      meta: {
        active_workers: active.length,
        queued: status?.queue?.length || 0,
        utilization_pct: status?.utilization_pct || 0,
        tab,
      },
    }],
  });

  // 2026-05-22 — H1 reflects the ACTIVE sub-tab (icon + label + subtitle).
  // Each sub-tab carries its own icon: Overview = pulse, History = clock,
  // Configuration = sliders, Alerts = bell.
  const POOL_TAB_META: Record<string, { label: string; subtitle: string; icon: React.ReactNode }> = {
    overview: {
      label: 'Overview',
      subtitle: 'Live worker status, priority queue, and current load.',
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
        </svg>
      ),
    },
    history: {
      label: 'Run History',
      subtitle: 'Recent run timeline with duration, outcome, and resource use.',
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10" />
          <polyline points="12 6 12 12 16 14" />
        </svg>
      ),
    },
    config: {
      label: 'Configuration',
      subtitle: 'Worker counts, queue tiers, and DuckDB tuning knobs.',
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="4" y1="21" x2="4" y2="14" /><line x1="4" y1="10" x2="4" y2="3" />
          <line x1="12" y1="21" x2="12" y2="12" /><line x1="12" y1="8" x2="12" y2="3" />
          <line x1="20" y1="21" x2="20" y2="16" /><line x1="20" y1="12" x2="20" y2="3" />
          <line x1="1" y1="14" x2="7" y2="14" /><line x1="9" y1="8" x2="15" y2="8" /><line x1="17" y1="16" x2="23" y2="16" />
        </svg>
      ),
    },
    alerts: {
      label: 'Alerts',
      subtitle: 'Compute-usage alert rules — only available in PROD.',
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.73 21a2 2 0 0 1-3.46 0" />
        </svg>
      ),
    },
  };
  const headerMeta = POOL_TAB_META[tab] || POOL_TAB_META.overview;

  return (
    <div className="h-full flex flex-col">
      {/* Header — canonical shared PageHeader shell */}
      <PageHeader
        environment={environment}
        icon={<span className={isProd ? 'text-red-400' : 'text-blue-500'}>{headerMeta.icon}</span>}
        title={headerMeta.label}
        titleAccessory={<TierChip tier={tier} environment={environment} />}
        subtitle={headerMeta.subtitle}
        tabs={
          /* Inline tabs — 'alerts' is PROD-only (compute-usage alert rules). */
          <div className="flex gap-0.5 justify-center items-center">
            {(isProd
              ? ['overview', 'history', 'config', 'alerts'] as const
              : ['overview', 'history', 'config'] as const
            ).map(t => (
              <button
                key={t}
                onClick={() => { navigateToSubRoute('pool', t); setTab(t); }}
                className={`px-4 py-2.5 text-sm font-semibold rounded-lg transition-all ${
                  tab === t
                    ? isProd
                      ? 'border-red-400 text-slate-900 font-bold bg-gradient-to-b from-slate-200 to-slate-400 shadow-[inset_0_0_0_1.5px_rgba(203,213,225,0.70),inset_0_0_10px_rgba(148,163,184,0.30),inset_0_1px_0_rgba(255,255,255,0.85)]'
                      : 'text-white font-bold bg-gradient-to-b from-slate-600 to-slate-800 shadow-[inset_0_0_0_1.5px_rgba(148,163,184,0.65),inset_0_0_10px_rgba(100,116,139,0.35),inset_0_1px_0_rgba(255,255,255,0.22)]'
                    : isProd
                      ? 'border-transparent text-slate-400 hover:text-slate-200 hover:bg-white/[0.03]'
                      : 'border-transparent text-slate-900 font-bold hover:text-black hover:bg-slate-100'
                }`}
              >
                {t === 'overview' ? 'Overview' : t === 'history' ? 'Run History' : t === 'config' ? 'Configuration' : 'Alerts'}
              </button>
            ))}
          </div>
        }
        actions={
          <div className={`flex items-center gap-2 text-xs font-semibold justify-end ${isProd ? 'text-slate-400' : 'text-slate-500'}`}>
            <span className={`w-2 h-2 rounded-full ${
              !livePolling ? 'bg-amber-500' : pool.busy_workers > 0 ? 'bg-emerald-500 animate-pulse' : 'bg-slate-300'
            }`} />
            <span>
              {!livePolling ? 'Paused' : pool.busy_workers > 0 ? 'Active' : 'Idle'}
              {livePolling ? ' · auto 2s' : ''}
            </span>
            {/* Live toggle — pauses the 2s poll. Useful when reading
                a frozen state or stepping through a debug session. */}
            <button
              onClick={() => setLivePolling(p => !p)}
              className={`ml-1 px-2.5 py-2 text-xs font-semibold rounded-lg transition-all flex items-center gap-1 border ${
                isProd
                  ? 'text-slate-200 bg-slate-800 hover:bg-slate-700 border-slate-700'
                  : 'text-slate-700 bg-white hover:bg-slate-50 border-slate-300'
              }`}
              title={livePolling ? 'Pause auto-refresh' : 'Resume auto-refresh'}
            >
              {livePolling ? (
                <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" /><rect x="14" y="5" width="4" height="14" /></svg>
              ) : (
                <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="6,4 20,12 6,20" /></svg>
              )}
              {livePolling ? 'Pause' : 'Live'}
            </button>
            {/* Canonical refresh button — matches DashboardPage reference:
                px-3 py-2, text-xs, rounded-lg, SVG arrow icon, PROD dark
                slate / DEV white. See Apr 18 feedback. */}
            <button
              onClick={() => refresh()}
              disabled={loading}
              className={`px-4 py-2 text-sm font-semibold rounded-lg transition-all flex items-center gap-1.5 border disabled:opacity-50 ${
                isProd
                  ? 'text-slate-200 bg-slate-800 hover:bg-slate-700 border-slate-700'
                  : 'text-slate-700 bg-white hover:bg-slate-50 border-slate-300'
              }`}
              title="Refresh now"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className={loading ? 'animate-spin' : ''}><path d="M21 12a9 9 0 1 1-3-6.7L21 8" /><path d="M21 3v5h-5" /></svg>
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
          </div>
        }
      />

      {/* Content — padding + vertical rhythm matched to DashboardPage so
          Activity, Dashboard, and other operational pages feel like one
          visual family. See DashboardPage line 613 for the source pattern. */}
      <div className="flex-1 overflow-auto">
      <div className="w-full max-w-[1500px] mx-auto px-6 py-5">

      {/* ════════════════════════════ OVERVIEW TAB ════════════════════════════ */}
      {tab === 'overview' && (
        <div className="space-y-4">
          {/* Governor tier banner — explains throttling behavior in plain English.
              Solves "where is my job?" support tickets. Reads from /api/pool/config
              which now includes the GlobalResourceGovernor's live tier. */}
          {config?.governor && config.governor.tier && config.governor.tier !== 'unknown' && (
            <div className={`rounded-lg border px-4 py-3 flex items-center gap-3 ${
              config.governor.tier === 'green' ? 'bg-emerald-50 border-emerald-200' :
              config.governor.tier === 'yellow' ? 'bg-amber-50 border-amber-200' :
              config.governor.tier === 'orange' ? 'bg-orange-50 border-orange-300' :
              'bg-red-50 border-red-300'
            }`}>
              <span className={`w-3 h-3 rounded-full shrink-0 ${
                config.governor.tier === 'green' ? 'bg-emerald-500' :
                config.governor.tier === 'yellow' ? 'bg-amber-500' :
                config.governor.tier === 'orange' ? 'bg-orange-500' :
                'bg-red-500 animate-pulse'
              }`} />
              <div className="flex-1 min-w-0">
                <div className={`text-xs font-bold uppercase tracking-wider ${
                  config.governor.tier === 'green' ? 'text-emerald-700' :
                  config.governor.tier === 'yellow' ? 'text-amber-700' :
                  config.governor.tier === 'orange' ? 'text-orange-700' :
                  'text-red-700'
                }`}>
                  Governor: {config.governor.tier}
                </div>
                <div className={`text-xs mt-0.5 ${
                  config.governor.tier === 'green' ? 'text-emerald-700' :
                  config.governor.tier === 'yellow' ? 'text-amber-700' :
                  config.governor.tier === 'orange' ? 'text-orange-700' :
                  'text-red-700'
                }`}>
                  {config.governor.explanation}
                </div>
              </div>
              <div className="text-right shrink-0">
                <div className="text-xs uppercase tracking-wider text-slate-500 font-semibold">Memory · CPU</div>
                <div className="text-sm font-bold text-slate-800">{config.governor.mem_pct}% · {config.governor.cpu_pct}%</div>
              </div>
            </div>
          )}

          {/* PR14 — Pool allocation card. Plus + admin only (allocation
              page is hidden on Free; backend gates the PUT endpoint to admin).
              Shows the live PROD/DEV/burst split with three sliders that
              auto-rebalance to maintain sum=100. */}
          {!isFree && allocation && allocDraft && (
            <section>
              <div className={`text-xs font-bold uppercase tracking-wider mb-2 flex items-center justify-between ${isProd ? 'text-slate-400' : 'text-slate-500'}`}>
                <span>Worker allocation · {allocation.total_workers} workers</span>
                {allocDirty && (
                  <span className={`text-xs font-semibold ${allocValid ? 'text-amber-600' : 'text-red-500'}`}>
                    {allocValid ? 'Unsaved changes' : `Sum = ${allocDraft.prod + allocDraft.dev + allocDraft.burst} (must be 100)`}
                  </span>
                )}
              </div>
              <div className={`rounded-lg border shadow-sm p-4 ${isProd ? 'bg-slate-800 border-slate-700' : 'bg-white border-slate-200'}`}>
                {/* Three rows of {label, slider, percent, slot count} */}
                {([
                  { key: 'prod', label: 'PROD reserved', color: '#ef4444', setter: setProdPct },
                  { key: 'dev', label: 'DEV reserved', color: '#10b981', setter: setDevPct },
                  { key: 'burst', label: 'Shared burst', color: '#8b5cf6', setter: null as any },
                ] as const).map((row) => {
                  const pct = (allocDraft as any)[row.key] as number;
                  const slotCount = Math.floor((allocation.total_workers * pct) / 100);
                  const editable = isAdmin && row.setter !== null;
                  return (
                    <div key={row.key} className="flex items-center gap-3 py-1.5">
                      <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ background: row.color }} />
                      <span className={`text-xs font-semibold w-32 shrink-0 ${isProd ? 'text-slate-200' : 'text-slate-700'}`}>{row.label}</span>
                      <input
                        type="range"
                        min={0} max={100}
                        value={pct}
                        onChange={(e) => editable && row.setter && row.setter(Number(e.target.value))}
                        disabled={!editable}
                        className="flex-1 accent-blue-600 disabled:opacity-50"
                      />
                      <span className={`text-xs font-mono w-10 text-right tabular-nums ${isProd ? 'text-slate-200' : 'text-slate-700'}`}>{pct}%</span>
                      <span className={`text-xs w-16 text-right tabular-nums ${isProd ? 'text-slate-400' : 'text-slate-500'}`}>{slotCount} workers</span>
                    </div>
                  );
                })}
                {/* Burst row read-only (auto-computed) — but show as a hint */}
                <p className={`text-xs mt-2 ${isProd ? 'text-slate-500' : 'text-slate-400'}`}>
                  Burst is auto-computed from PROD + DEV. PROD always has its reserved share available; DEV the same. Burst lane is grabbed first-come-first-served when reserved is full.
                </p>
                {/* Admin save row */}
                {isAdmin && allocDirty && (
                  <div className="mt-3 pt-3 border-t border-slate-200/60 flex items-center justify-end gap-2">
                    <button
                      onClick={cancelAllocEdit}
                      disabled={allocSaving}
                      className="text-xs font-semibold px-3 py-1.5 rounded-lg text-slate-500 bg-white border border-slate-200 hover:bg-slate-50 disabled:opacity-50"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={saveAllocation}
                      disabled={!allocValid || allocSaving}
                      className="text-xs font-bold px-4 py-1.5 rounded-lg text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50 shadow-sm"
                    >
                      {allocSaving ? 'Saving…' : 'Save allocation'}
                    </button>
                  </div>
                )}
                {!isAdmin && (
                  <p className={`text-xs mt-2 italic ${isProd ? 'text-slate-500' : 'text-slate-400'}`}>
                    Read-only — admin role required to change the split.
                  </p>
                )}
              </div>
            </section>
          )}

          {/* Summary cards */}
          <section>
            <div className={`text-xs font-bold uppercase tracking-wider mb-2 ${isProd ? 'text-slate-400' : 'text-slate-500'}`}>
              Worker status · live
            </div>
            {/* Hero-style KPI cards — centered content, bold gradients,
                SVG icons. Shape mirrors DashboardPage's HeroKPI so the
                two pages feel like one family. DEV uses the lighter
                400→500 gradient variant; PROD uses the richer 500→600. */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              {/* Utilization — colour shifts by severity (emerald/amber/red) */}
              <HeroCard
                gradient={
                  (pool.utilization_pct || 0) > 80
                    ? (isProd ? 'from-red-500 to-rose-600'    : 'from-red-400 to-rose-500')
                    : (pool.utilization_pct || 0) > 50
                      ? (isProd ? 'from-amber-500 to-orange-600' : 'from-amber-400 to-orange-500')
                      : (isProd ? 'from-emerald-500 to-emerald-600' : 'from-emerald-400 to-emerald-500')
                }
                icon={
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M12 2v4" /><path d="M5.6 5.6l2.9 2.9" /><path d="M2 12h4" /><path d="M5.6 18.4l2.9-2.9" /><circle cx="12" cy="12" r="3" />
                  </svg>
                }
                label="Utilization"
                value={`${pool.utilization_pct || 0}%`}
                footer={`${pool.busy_workers || 0} / ${pool.max_workers || 0} workers busy`}
                bar={pool.utilization_pct || 0}
              />

              {/* Queue Depth */}
              <HeroCard
                gradient={
                  (pool.queue_depth || 0) > 5
                    ? (isProd ? 'from-red-500 to-rose-600' : 'from-red-400 to-rose-500')
                    : (pool.queue_depth || 0) > 0
                      ? (isProd ? 'from-amber-500 to-orange-600' : 'from-amber-400 to-orange-500')
                      : (isProd ? 'from-slate-500 to-slate-600' : 'from-slate-400 to-slate-500')
                }
                icon={
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" />
                  </svg>
                }
                label="Queue"
                value={String(pool.queue_depth || 0)}
                valueSuffix="waiting"
                footer={`Avg wait: ${formatDuration(throughput.avg_wait_ms || 0)}`}
              />

              {/* Throughput */}
              <HeroCard
                gradient={isProd ? 'from-blue-500 to-indigo-600' : 'from-blue-400 to-indigo-500'}
                icon={
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="22 7 13.5 15.5 8.5 10.5 2 17" /><polyline points="16 7 22 7 22 13" />
                  </svg>
                }
                label="Throughput"
                value={String(throughput.last_1h || 0)}
                valueSuffix="/ hour"
                footer={
                  throughput.p95_duration_ms
                    ? `${throughput.last_5m || 0} in 5m · avg ${formatDuration(throughput.avg_duration_ms || 0)} · p95 ${formatDuration(throughput.p95_duration_ms)}`
                    : `${throughput.last_5m || 0} in last 5 min · avg ${formatDuration(throughput.avg_duration_ms || 0)}`
                }
              />

              {/* Totals */}
              <HeroCard
                gradient={isProd ? 'from-violet-500 to-purple-600' : 'from-violet-400 to-purple-500'}
                icon={
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                }
                label="Total"
                value={String(counters.total_completed || 0)}
                valueSuffix="completed"
                footer={`${counters.total_errors || 0} errors · ${counters.success_rate || 0}% success`}
              />
            </div>
          </section>

          {/* Environment Split — Plus-tier only. On Free the PROD card would
              always read 0/0, which is dead info. Hidden to reduce noise. */}
          {!isFree && (
          <section>
            <div className={`text-xs font-bold uppercase tracking-wider mb-2 ${isProd ? 'text-slate-400' : 'text-slate-500'}`}>
              Environment split · live
            </div>
            <div className="grid grid-cols-2 gap-3">
              {/* DEV */}
              <HeroCard
                gradient={isProd ? 'from-emerald-500 to-teal-600' : 'from-emerald-400 to-teal-500'}
                icon={
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" />
                  </svg>
                }
                label="DEV"
                value={String(envs.active_dev || 0)}
                valueSuffix="running"
                footer={`${envs.history_dev || 0} total runs`}
                bar={pool.busy_workers > 0 ? Math.round((envs.active_dev || 0) / pool.busy_workers * 100) : 0}
              />
              {/* PROD */}
              <HeroCard
                gradient={isProd ? 'from-red-500 to-rose-600' : 'from-red-400 to-rose-500'}
                icon={
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M12 2L2 7l10 5 10-5-10-5z" /><path d="M2 17l10 5 10-5" /><path d="M2 12l10 5 10-5" />
                  </svg>
                }
                label="PROD"
                value={String(envs.active_prod || 0)}
                valueSuffix="running"
                footer={`${envs.history_prod || 0} total runs`}
                bar={pool.busy_workers > 0 ? Math.round((envs.active_prod || 0) / pool.busy_workers * 100) : 0}
              />
            </div>
          </section>
          )}

          {/* Connection cache (Critical #5 / Phases 2-5).
              Live state of the per-run DB connection pool. Reuses driver
              connections across steps to amortise the 50-150ms connect
              cost. Three render modes:
                - Active entries  → full breakdown card (by connection / by run).
                - Empty + installed → compact one-liner (was a full card before).
                - Not installed   → small warning strip.
              Compacting the empty state cuts a ~80px dead section on a
              quiet laptop where most pipelines aren't DB-touching. */}
          {connPool && connPool.installed && connPool.total_entries > 0 && (
            <section className="rounded-lg border border-slate-200 shadow-sm bg-white p-5">
              <div className="flex items-start justify-between mb-4 flex-wrap gap-3">
                <div>
                  <h2 className="text-base font-bold text-slate-800 flex items-center gap-2">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-indigo-500"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" /><polyline points="3.27 6.96 12 12.01 20.73 6.96" /><line x1="12" y1="22.08" x2="12" y2="12" /></svg>
                    Connection Cache
                  </h2>
                  <p className="text-xs text-slate-500 mt-0.5">
                    Per-run DB driver pool — caps at {connPool.max_per_connection} concurrent per connection_id (FPULSE_CONNECTION_POOL_SIZE).
                  </p>
                </div>
                <div className="text-right shrink-0">
                  <div className="text-2xl font-bold tabular-nums text-slate-800">{connPool.total_entries}</div>
                  <div className="text-xs text-slate-500 uppercase tracking-wider font-semibold">cached connections</div>
                </div>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div className="rounded-md border border-slate-200 p-3">
                  <div className="text-xs font-semibold uppercase tracking-wider text-slate-500 mb-2">By connection</div>
                  <div className="space-y-1">
                    {Object.entries(connPool.by_connection).slice(0, 8).map(([cid, count]) => (
                      <div key={cid} className="flex items-center justify-between text-[12px]">
                        <span className="font-mono text-slate-700 truncate">{cid.slice(0, 24)}</span>
                        <span className={`tabular-nums font-semibold ${count >= connPool.max_per_connection ? 'text-amber-700' : 'text-slate-700'}`}>
                          {count}{count >= connPool.max_per_connection ? ' (cap)' : ''}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
                <div className="rounded-md border border-slate-200 p-3">
                  <div className="text-xs font-semibold uppercase tracking-wider text-slate-500 mb-2">By run</div>
                  <div className="space-y-1">
                    {Object.entries(connPool.by_run).slice(0, 8).map(([rid, count]) => (
                      <div key={rid} className="flex items-center justify-between text-[12px]">
                        <span className="font-mono text-slate-700">{rid.slice(0, 16)}</span>
                        <span className="tabular-nums font-semibold text-slate-700">{count}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </section>
          )}
          {/* Connection-cache empty + not-installed states dropped as noise —
              the full card above renders only when pooling is actually active
              (total_entries > 0), so the card earns its space. */}

          {/* Worker Grid — busy workers always rendered as cards; idle
              workers collapsed into a single strip when the pool is fully
              idle. Density fix: an idle 8/16/20-card grid is most of the
              page on a quiet laptop and carries no signal. Click "Show all"
              to expand — useful for debugging "why is W3 not picking up".
              When at least one worker is busy, every worker renders as a
              card so the operator sees who's free for preemption. */}
          {(() => {
            const busyWorkers = workers.filter((w: any) => w.status === 'busy');
            const idleWorkers = workers.filter((w: any) => w.status !== 'busy');
            const allIdle = busyWorkers.length === 0 && idleWorkers.length > 0;
            const totalJobsDone = workers.reduce((sum: number, w: any) => sum + (w.jobs_completed || 0), 0);
            const showFullGrid = !allIdle || idleExpanded;
            return (
              <div className="rounded-lg border border-slate-200 shadow-sm bg-white p-5">
                <div className="flex items-center justify-between mb-3">
                  <h3 className="text-sm font-bold text-slate-700 flex items-center gap-2">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="7" width="20" height="14" rx="2" /><path d="M16 3h-8l-2 4h12z" /></svg>
                    Workers ({pool.max_workers || 0})
                  </h3>
                  {allIdle && (
                    <button
                      onClick={() => setIdleExpanded(v => !v)}
                      className="text-xs font-semibold text-slate-500 hover:text-slate-800"
                    >
                      {idleExpanded ? 'Hide idle' : 'Show all'}
                    </button>
                  )}
                </div>
                {allIdle && !idleExpanded ? (
                  <div className="flex items-center gap-3 px-3 py-2.5 rounded-lg bg-slate-50 border border-slate-100">
                    <span className="w-2 h-2 rounded-full bg-slate-300 shrink-0" />
                    <span className="text-xs font-semibold text-slate-600">
                      All {idleWorkers.length} worker{idleWorkers.length === 1 ? '' : 's'} idle
                    </span>
                    <span className="text-xs text-slate-400">
                      · {totalJobsDone} job{totalJobsDone === 1 ? '' : 's'} completed lifetime
                    </span>
                  </div>
                ) : (
                  <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 xl:grid-cols-8 gap-2">
                    {workers.map((w: any) => {
                      const ps = priorityStyle(w.current_priority);
                      return (
                        <div
                          key={w.id}
                          className={`rounded-lg border-2 p-3 transition-all ${
                            w.status === 'busy'
                              ? `${ps.bg} ${ps.ring} ring-1 border-transparent`
                              : 'border-slate-100 bg-slate-50/50'
                          }`}
                        >
                          <div className="flex items-center justify-between mb-1">
                            <span className="text-xs font-bold text-slate-400">W{w.id}</span>
                            <span className={`w-2 h-2 rounded-full ${
                              w.status === 'busy' ? 'bg-emerald-500 animate-pulse' : 'bg-slate-300'
                            }`} />
                          </div>
                          {w.status === 'busy' ? (
                            <>
                              <div className="text-xs font-semibold text-slate-700 truncate" title={w.current_workflow_name}>
                                {w.current_workflow_name || w.current_workflow_id}
                              </div>
                              <div className="flex items-center gap-1 mt-1 flex-wrap">
                                <span className={`text-xs font-bold px-1 py-0.5 rounded ${ps.bg} ${ps.text}`}>
                                  P{w.current_priority}
                                </span>
                                {!isFree && <EnvBadge env={w.current_environment || 'dev'} />}
                                <span className="text-xs text-slate-500">
                                  {formatDuration(w.elapsed_ms)}
                                </span>
                                {(() => {
                                  const rl = runningLong(w);
                                  return rl.over ? (
                                    <span
                                      className="text-[10px] font-bold px-1 py-0.5 rounded bg-amber-100 text-amber-700"
                                      title={`Running ~${rl.ratio.toFixed(1)}× this pipeline's usual time — it may be stuck or processing more data than normal.`}
                                    >
                                      {rl.ratio.toFixed(1)}× long
                                    </span>
                                  ) : null;
                                })()}
                              </div>
                            </>
                          ) : (
                            <div className="text-xs text-slate-400 mt-1">Idle</div>
                          )}
                          <div className="text-xs text-slate-400 mt-1.5">
                            {w.jobs_completed} jobs done
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })()}

          {/* Live queue — the single place the execution queue is shown (the
              separate Queue tab was folded in here). Counts + every waiting job
              with cancel; running detail is in the Workers grid above. */}
          <div className="rounded-lg border border-slate-200 shadow-sm bg-white p-5">
            <h3 className="text-sm font-bold text-slate-700 flex items-center gap-2 mb-3">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" />
                <line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" />
              </svg>
              Live queue
            </h3>
            <div className="flex items-center gap-4 text-xs mb-3">
              <span className="text-slate-600"><span className="font-bold text-slate-800 tabular-nums">{pool.busy_workers || 0}</span> running</span>
              {longRunningCount > 0 && (
                <span className="text-amber-700 font-semibold" title="Runs exceeding ~1.5× their pipeline's usual time">
                  {longRunningCount} running long
                </span>
              )}
              <span className="text-slate-600"><span className="font-bold text-slate-800 tabular-nums">{pool.queue_depth || 0}</span> waiting</span>
              {throughput.avg_wait_ms ? (
                <span className="text-slate-400">avg wait {formatDuration(throughput.avg_wait_ms)}</span>
              ) : null}
            </div>
            {queue.length === 0 ? (
              <div className="flex items-center gap-2.5 px-3 py-2.5 rounded-lg bg-slate-50 border border-slate-100">
                <span className="w-2 h-2 rounded-full bg-slate-300 shrink-0" />
                <span className="text-xs font-semibold text-slate-600">No jobs waiting</span>
                <span className="text-xs text-slate-400">· runs start as soon as a worker is free</span>
              </div>
            ) : (
              <div className="space-y-1.5">
                {queue.map((j: any, i: number) => {
                  const ps = priorityStyle(j.priority);
                  return (
                    <div key={j.id} className="flex items-center gap-2.5 px-3 py-1.5 rounded-lg bg-slate-50 border border-slate-100 text-xs">
                      <span className="font-bold text-slate-400 w-4">#{i + 1}</span>
                      <span className={`font-bold px-1.5 py-0.5 rounded ${ps.bg} ${ps.text}`}>P{j.priority}</span>
                      {!isFree && <EnvBadge env={j.environment || 'dev'} />}
                      <span className="font-semibold text-slate-700 flex-1 truncate">{j.workflow_name}</span>
                      <span className="text-slate-400">waiting {formatDuration(j.wait_ms)}</span>
                      <button
                        onClick={async () => { await api.cancelPoolJob(j.id).catch(() => {}); refresh(); }}
                        className="text-red-500 hover:text-red-700 font-semibold"
                      >
                        Cancel
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {!isProd ? (
            <div className="flex items-center gap-2.5 px-4 py-2.5 rounded-lg border border-blue-200 bg-blue-50">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#2563eb" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
                <circle cx="12" cy="12" r="10" />
                <line x1="12" y1="16" x2="12" y2="12" />
                <line x1="12" y1="8" x2="12.01" y2="8" />
              </svg>
              <span className="text-xs text-blue-700">
                <span className="font-bold">Live worker pool</span> &middot; Utilization, queue, and
                throughput metrics on this page are updated in real time.
              </span>
            </div>
          ) : null}
        </div>
      )}

      {/* ════════════════════════════ HISTORY TAB ════════════════════════════ */}
      {tab === 'history' && (() => {
        // Compose filters: env (Plus only) → status → priority → trigger → search.
        // Each step narrows in turn so the chip counts above reflect what
        // would be visible if just that one chip changed.
        const envRuns = history.filter((h: any) => envFilter === 'all' || (h.environment || 'dev') === envFilter);
        const statusRuns = statusFilter === 'all' ? envRuns : envRuns.filter((h: any) => (h.status || '') === statusFilter);
        const priorityRuns = priorityFilter === 'all' ? statusRuns : statusRuns.filter((h: any) => h.priority === priorityFilter);
        const triggerRuns = triggerFilter === 'all' ? priorityRuns : priorityRuns.filter((h: any) => (h.triggered_by || '') === triggerFilter);
        const filteredRuns = searchQuery.trim()
          ? triggerRuns.filter((h: any) => { const q = searchQuery.toLowerCase(); return (h.workflow_name || h.workflow_id || '').toLowerCase().includes(q) || (h.status || '').toLowerCase().includes(q) || (h.triggered_by || '').toLowerCase().includes(q) || (h.error || '').toLowerCase().includes(q); })
          : triggerRuns;
        // Distinct trigger values across all history (for the trigger chip set).
        const triggerSet = Array.from(new Set(history.map((h: any) => h.triggered_by || '').filter(Boolean))).sort();
        return (
        <div className="space-y-4">
          {/* Run History — restructured (May 17 2026) so the page reads
              as a stack of three pieces only:
                1. status tab pills (below)
                2. filter card (below)
                3. ONE table card with the search/columns/export
                   toolbar folded inside it.
              The previous layout had a fourth card on top that contained
              just the toolbar + a redundant "Run History" h3, which read
              as visual noise — the page tab already says "Run History",
              and OSS Free hides the env chips that used to live in that
              row. Plus tier still gets the env split via the
              ProdSplitPanel rendered above the History tab. */}

          {/* Z44 (2026-05-23) — single-row toolbar: status pill strip
              on the LEFT, Priority + Trigger dropdowns as compact
              icon-prefixed pills on the RIGHT. Previously these were
              two stacked blocks both leaving the right half empty.
              Same pattern as Dashboard Z37 (header filters). */}
          {history.length > 0 && (() => {
            const activeFilterCount = (priorityFilter !== 'all' ? 1 : 0) + (triggerFilter !== 'all' ? 1 : 0);
            return (
              <div className="flex items-center gap-2 flex-wrap">
                {/* LEFT — Status pill strip */}
                <div className="flex items-center gap-1 rounded-lg border shadow-sm p-1 bg-white border-slate-200">
                  {(['all', 'success', 'error', 'timed_out', 'cancelled'] as const).map((s) => {
                    const count = s === 'all' ? envRuns.length : envRuns.filter((h: any) => (h.status || '') === s).length;
                    const active = statusFilter === s;
                    const label = s === 'all' ? 'All' : s.replace('_', ' ');
                    const badgeCls = s === 'error'
                      ? 'bg-red-100 text-red-500'
                      : s === 'timed_out'
                        ? 'bg-amber-100 text-amber-600'
                        : s === 'cancelled'
                          ? 'bg-slate-200 text-slate-600'
                          : s === 'success'
                            ? 'bg-emerald-100 text-emerald-600'
                            : 'bg-slate-200 text-slate-600';
                    return (
                      <button
                        key={s}
                        onClick={() => setStatusFilter(s)}
                        className={`px-3 py-1.5 text-xs font-semibold rounded-lg transition-colors capitalize flex items-center gap-1.5 ${
                          active ? 'bg-pipe-100 text-pipe-700' : 'text-slate-400 hover:text-slate-600'
                        }`}
                      >
                        {label}
                        {count > 0 && s !== 'all' && (
                          <span className={`text-[9px] px-1.5 py-0.5 rounded-full ${badgeCls}`}>{count}</span>
                        )}
                      </button>
                    );
                  })}
                </div>

                {/* RIGHT — Priority + Trigger compact pills, pushed to
                    the far right via ml-auto. Icon prefix + transparent
                    select for the same pill styling as the Dashboard
                    project / time-window pills (Z37). */}
                <div className="flex items-center gap-2 flex-wrap ml-auto">
                  <label className={`inline-flex items-center gap-1.5 pl-2.5 pr-1 py-1.5 rounded-md border cursor-pointer transition-colors ${
                    priorityFilter !== 'all'
                      ? 'bg-blue-50 border-blue-300 text-blue-800'
                      : 'bg-white border-slate-200 text-slate-700 hover:bg-slate-50'
                  }`}>
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={priorityFilter !== 'all' ? 'text-blue-600' : 'text-slate-500'} aria-hidden="true">
                      <line x1="4" y1="9" x2="20" y2="9" />
                      <line x1="4" y1="15" x2="14" y2="15" />
                    </svg>
                    <select
                      value={String(priorityFilter)}
                      onChange={(e) => setPriorityFilter(e.target.value === 'all' ? 'all' : (Number(e.target.value) as 1 | 2 | 3 | 4 | 5))}
                      aria-label="Filter by priority"
                      className="text-xs font-semibold bg-transparent cursor-pointer focus:outline-none pr-1"
                    >
                      <option value="all">All priorities ({envRuns.length})</option>
                      {[1, 2, 3, 4, 5].map((p) => (
                        <option key={p} value={p}>
                          {`P${p} (${envRuns.filter((h: any) => h.priority === p).length})`}
                        </option>
                      ))}
                    </select>
                  </label>

                  {triggerSet.length > 0 && (
                    <label className={`inline-flex items-center gap-1.5 pl-2.5 pr-1 py-1.5 rounded-md border cursor-pointer transition-colors ${
                      triggerFilter !== 'all'
                        ? 'bg-blue-50 border-blue-300 text-blue-800'
                        : 'bg-white border-slate-200 text-slate-700 hover:bg-slate-50'
                    }`}>
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={triggerFilter !== 'all' ? 'text-blue-600' : 'text-slate-500'} aria-hidden="true">
                        <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
                      </svg>
                      <select
                        value={triggerFilter}
                        onChange={(e) => setTriggerFilter(e.target.value)}
                        aria-label="Filter by trigger"
                        className="text-xs font-semibold bg-transparent cursor-pointer focus:outline-none pr-1"
                      >
                        <option value="all">All triggers ({envRuns.length})</option>
                        {triggerSet.map((t: string) => (
                          <option key={t} value={t}>
                            {`${t} (${envRuns.filter((h: any) => (h.triggered_by || '') === t).length})`}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}

                  {activeFilterCount > 0 && (
                    <button
                      onClick={() => { setPriorityFilter('all'); setTriggerFilter('all'); }}
                      className="text-xs font-semibold text-slate-500 hover:text-slate-800 px-2.5 py-1.5 rounded-md hover:bg-slate-100"
                      title={`Clear ${activeFilterCount} filter${activeFilterCount === 1 ? '' : 's'}`}
                    >
                      Clear
                    </button>
                  )}
                </div>
              </div>
            );
          })()}

          {/* Run-history TABLE — single card holding the toolbar +
              dark+amber header + rows. Folding the toolbar in (instead
              of giving it its own card above) cuts a visual seam that
              previously left a near-empty top card on screen. Matches
              the Pipelines / Connections / Executions pattern. */}
          <div className="rounded-lg border border-slate-200 shadow-sm bg-white overflow-hidden">
            <TableToolbar
              data={filteredRuns}
              columns={RUN_COLUMNS}
              columnGroups={RUN_GROUPS}
              visibleColumns={runColState.visibleColumns}
              activeColumnCount={runColState.activeColumns.length}
              onToggleColumn={runColState.toggleColumn}
              onResetDefaults={runColState.resetToDefaults}
              onSelectAll={runColState.selectAll}
              searchValue={searchQuery}
              onSearchChange={setSearchQuery}
              searchPlaceholder="Search runs..."
              exportRowBuilder={(h: any) => ({
                pipeline: h.workflow_name || h.workflow_id,
                priority: `P${h.priority}`,
                // `environment` only emitted on Plus tier — OSS Free has
                // no PROD, so leaking the column into CSV exports would
                // be misleading.
                ...(isFree ? {} : { environment: h.environment || 'dev' }),
                worker: `W${h.worker_id}`,
                status: h.status,
                trigger: h.triggered_by,
                duration: formatDuration(h.duration_ms),
                wait: h.wait_ms > 100 ? formatDuration(h.wait_ms) : '',
                queued: h.queued_at || '',
                started: h.started_at || '',
                finished: h.completed_at || '',
                steps: h.steps ?? '',
                rows: h.rows_processed ?? '',
                peak_memory_mb: h.peak_memory_mb ?? '',
                cpu_seconds: h.cpu_seconds ?? '',
                error: h.error || '',
              })}
              exportFilename="execution_runs"
              recordLabel="run"
            />
            {history.length === 0 ? (
              <div className="px-5 py-12 text-center text-xs text-slate-400">
                No execution history yet. Run a pipeline to see it here.
              </div>
            ) : (
              <div className="divide-y divide-slate-50">
                {/* Header — dark + amber, matches every other data
                    table in the app (Pipelines, Executions, Templates,
                    Connections). Previously this row used a light
                    slate-50 background which made the Pool history
                    visually inconsistent with the rest of the product. */}
                <div className="flex gap-2 px-5 py-2.5 text-xs font-bold text-amber-300 uppercase tracking-wider bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900 border-b-2 border-amber-400/40">
                  {runColState.isVisible('pipeline') && <div className="flex-[2.5] min-w-0">Pipeline</div>}
                  {runColState.isVisible('priority') && <div className="flex-[0.7]">Priority</div>}
                  {!isFree && runColState.isVisible('environment') && <div className="flex-[0.5]">Env</div>}
                  {runColState.isVisible('worker') && <div className="flex-[0.6]">Worker</div>}
                  {runColState.isVisible('status') && <div className="flex-[0.6]">Status</div>}
                  {runColState.isVisible('trigger') && <div className="flex-[0.5]">Trigger</div>}
                  {runColState.isVisible('duration') && <div className="flex-[1.5]">Duration</div>}
                  {runColState.isVisible('wait') && <div className="flex-[0.6]">Wait</div>}
                  {runColState.isVisible('queued') && <div className="flex-[0.8]">Queued</div>}
                  {runColState.isVisible('started') && <div className="flex-[0.8]">Started</div>}
                  {runColState.isVisible('finished') && <div className="flex-[0.8]">Finished</div>}
                  {runColState.isVisible('steps') && <div className="flex-[0.5]">Steps</div>}
                  {runColState.isVisible('rows') && <div className="flex-[0.7]">Rows</div>}
                  {runColState.isVisible('memory') && <div className="flex-[0.7]" title="Peak resident memory observed during the run">Memory (MB)</div>}
                  {runColState.isVisible('cpu') && <div className="flex-[0.6]" title="CPU-seconds consumed (user+system across cores). Can exceed wall-clock when parallelism is in play.">CPU (s)</div>}
                  {runColState.isVisible('error') && <div className="flex-[1.5] min-w-0">Error</div>}
                </div>

                {filteredRuns.map((h: any) => {
                  const ps = priorityStyle(h.priority);
                  const maxDuration = Math.max(...history.map((x: any) => x.duration_ms || 1), 1);
                  const barWidth = Math.max(2, Math.round((h.duration_ms / maxDuration) * 100));

                  return (
                    <div key={h.id} className="flex gap-2 px-5 py-2.5 items-center hover:bg-slate-50 transition-colors text-xs">
                      {/* Pipeline name */}
                      {runColState.isVisible('pipeline') && (
                        <div className="flex-[2.5] min-w-0 font-semibold text-slate-700 truncate" title={h.workflow_name}>
                          {h.workflow_name || h.workflow_id}
                        </div>
                      )}

                      {/* Priority badge */}
                      {runColState.isVisible('priority') && (
                        <div className="flex-[0.7]">
                          <span className={`text-xs font-bold px-1.5 py-0.5 rounded ${ps.bg} ${ps.text}`}>
                            P{h.priority}
                          </span>
                        </div>
                      )}

                      {/* Environment — hidden on OSS Free (no PROD exists). */}
                      {!isFree && runColState.isVisible('environment') && (
                        <div className="flex-[0.5]">
                          <EnvBadge env={h.environment || 'dev'} />
                        </div>
                      )}

                      {/* Worker */}
                      {runColState.isVisible('worker') && (
                        <div className="flex-[0.6] text-xs text-slate-500">W{h.worker_id}</div>
                      )}

                      {/* Status — OSS-2 (2026-05-19) — unified <StatusPill>
                          replaces the inline emerald/red/slate switch. */}
                      {runColState.isVisible('status') && (
                        <div className="flex-[0.6]">
                          <StatusPill status={h.status || 'pending'} size="sm" />
                        </div>
                      )}

                      {/* Trigger */}
                      {runColState.isVisible('trigger') && (
                        <div className="flex-[0.5] text-xs text-slate-400">{h.triggered_by}</div>
                      )}

                      {/* Duration bar */}
                      {runColState.isVisible('duration') && (
                        <div className="flex-[1.5] flex items-center gap-2">
                          <div className="flex-1 h-3 bg-slate-100 rounded-full overflow-hidden">
                            <div
                              className={`h-full rounded-full transition-all ${
                                h.status === 'success' ? 'bg-emerald-400' :
                                h.status === 'error' ? 'bg-red-400' : 'bg-slate-300'
                              }`}
                              style={{ width: `${barWidth}%` }}
                            />
                          </div>
                          <span className="text-xs text-slate-500 whitespace-nowrap w-12 text-right">
                            {formatDuration(h.duration_ms)}
                          </span>
                        </div>
                      )}

                      {/* Wait time */}
                      {runColState.isVisible('wait') && (
                        <div className="flex-[0.6] text-xs text-slate-400">
                          {h.wait_ms > 100 ? formatDuration(h.wait_ms) : '-'}
                        </div>
                      )}

                      {/* Queued At */}
                      {runColState.isVisible('queued') && (
                        <div className="flex-[0.8] text-xs text-slate-400">
                          {formatTime(h.queued_at)}
                        </div>
                      )}

                      {/* Started */}
                      {runColState.isVisible('started') && (
                        <div className="flex-[0.8] text-xs text-slate-400">
                          {formatTime(h.started_at)}
                        </div>
                      )}

                      {/* Finished */}
                      {runColState.isVisible('finished') && (
                        <div className="flex-[0.8] text-xs text-slate-400">
                          {formatTime(h.completed_at)}
                        </div>
                      )}

                      {/* Steps */}
                      {runColState.isVisible('steps') && (
                        <div className="flex-[0.5] text-xs text-slate-500">
                          {h.steps ?? '-'}
                        </div>
                      )}

                      {/* Rows Processed */}
                      {runColState.isVisible('rows') && (
                        <div className="flex-[0.7] text-xs text-slate-500">
                          {h.rows_processed != null ? h.rows_processed.toLocaleString() : '-'}
                        </div>
                      )}

                      {/* Peak Memory (Pass 2 — populated by backend join with
                          ExecutionRecord.metadata.peak_memory_mb captured by
                          ResourceMonitor). Older runs that pre-date the metric
                          show '-'. */}
                      {runColState.isVisible('memory') && (
                        <div
                          className="flex-[0.7] text-xs text-slate-500"
                          title={h.peak_memory_mb != null ? `Peak ${h.peak_memory_mb.toFixed(1)} MB sampled during this run` : 'Not tracked for this run'}
                        >
                          {h.peak_memory_mb != null ? `${h.peak_memory_mb.toFixed(1)} MB` : '-'}
                        </div>
                      )}

                      {/* CPU-seconds — user+system time across all cores. Can
                          exceed wall-clock under parallelism, which is the
                          intent (multi-core utilization is the signal). */}
                      {runColState.isVisible('cpu') && (
                        <div
                          className="flex-[0.6] text-xs text-slate-500"
                          title={h.cpu_seconds != null ? `${h.cpu_seconds.toFixed(1)} CPU-seconds (user+system, summed across cores)` : 'Not tracked for this run'}
                        >
                          {h.cpu_seconds != null ? `${h.cpu_seconds.toFixed(1)}s` : '-'}
                        </div>
                      )}

                      {/* Error message — only shown when status indicates failure */}
                      {runColState.isVisible('error') && (
                        <div className="flex-[1.5] min-w-0 text-xs text-red-600 truncate" title={h.error || undefined}>
                          {h.error || '-'}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
        );
      })()}

      {/* ════════════════════════════ CONFIG TAB ════════════════════════════ */}
      {tab === 'config' && config && (
        <div className="space-y-4">
          <div className="rounded-lg border border-slate-200 shadow-sm bg-white p-5">
            <h3 className="text-sm font-bold text-slate-700 mb-4">Pool Configuration</h3>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-4">
              {[
                { label: 'Workers', value: config.max_workers, desc: 'Concurrent pipeline slots' },
                { label: 'CPU Cores', value: config.cpu_cores, desc: 'Available on this machine' },
                { label: 'CPU Usage', value: `${config.cpu_percent}%`, desc: 'Current system CPU' },
                { label: 'DuckDB Memory', value: config.duckdb_memory_limit, desc: 'Per-worker ceiling' },
                { label: 'DuckDB Threads', value: config.duckdb_threads || 'auto', desc: 'Threads per worker' },
                { label: 'Mode', value: config.mode?.toUpperCase(), desc: 'Runtime mode' },
              ].map(item => (
                <div key={item.label} className="bg-slate-50 rounded-lg p-3">
                  <div className="text-xs font-bold text-slate-400 uppercase tracking-wide">{item.label}</div>
                  <div className="text-lg font-bold text-slate-800 mt-1">{item.value}</div>
                  <div className="text-xs text-slate-500 mt-0.5">{item.desc}</div>
                </div>
              ))}
            </div>
          </div>

          {/* RAM breakdown */}
          {config.ram && (
            <div className="rounded-lg border border-slate-200 shadow-sm bg-white p-5">
              <h3 className="text-sm font-bold text-slate-700 mb-4">Memory</h3>
              <div className="grid grid-cols-3 gap-4">
                <div className="bg-slate-50 rounded-lg p-3">
                  <div className="text-xs font-bold text-slate-400 uppercase tracking-wide">Total RAM</div>
                  <div className="text-lg font-bold text-slate-800 mt-1">{config.ram.total_gb} GB</div>
                </div>
                <div className="bg-slate-50 rounded-lg p-3">
                  <div className="text-xs font-bold text-slate-400 uppercase tracking-wide">Used</div>
                  <div className="text-lg font-bold text-amber-600 mt-1">{config.ram.used_gb} GB</div>
                </div>
                <div className="bg-slate-50 rounded-lg p-3">
                  <div className="text-xs font-bold text-slate-400 uppercase tracking-wide">Available</div>
                  <div className="text-lg font-bold text-emerald-600 mt-1">{config.ram.available_gb} GB</div>
                </div>
              </div>
              <div className="mt-3 px-3 py-2 bg-violet-50 rounded-lg text-xs text-violet-700">
                Theoretical max: {config.theoretical_max_ram}
              </div>
            </div>
          )}

          {/* Environment variables.
              2026-06-03 — added a Read-only pill + explainer + docs link.
              The page is named "Configuration" so users naturally land here
              looking for editable knobs. These values are env vars read at
              backend startup; they CANNOT be live-edited (the worker pool
              is sized at boot, the DuckDB memory ceiling is passed to each
              query handle at construction). Make that explicit instead of
              letting users hunt for a Save button. The capacity-notes card
              below was also reworded — "Tune the values above" implied an
              in-UI editor that doesn't exist. */}
          <div className="rounded-lg border border-slate-200 shadow-sm bg-white p-5">
            <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-bold text-slate-700">Tuning (Environment Variables)</h3>
                <span className="text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded border bg-slate-100 text-slate-600 border-slate-300">
                  Read-only
                </span>
              </div>
              <a
                href="https://github.com/hybridyn/fpulse/blob/main/docs/scaling.md#the-4-vertical-scaling-knobs"
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-violet-600 hover:text-violet-800 font-semibold flex items-center gap-1"
                title="Open the deployment guide section on per-env-var setup"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                  <polyline points="15 3 21 3 21 9" />
                  <line x1="10" y1="14" x2="21" y2="3" />
                </svg>
                How to change these
              </a>
            </div>
            <p className="text-xs text-slate-500 mb-3 leading-relaxed">
              These values come from environment variables set <strong>before</strong> the backend starts.
              Changing them requires updating your env file / systemd unit / Docker compose, then restarting F-Pulse
              (the worker pool is sized at boot; DuckDB memory ceilings are bound to each query handle at construction).
            </p>
            <div className="space-y-2 text-xs">
              {[
                { env: 'FPULSE_MAX_CONCURRENT_RUNS', desc: 'Max concurrent pipeline executions (0 = unlimited)', current: config.max_workers },
                { env: 'FPULSE_DUCKDB_MEMORY_LIMIT', desc: 'Per-worker DuckDB memory ceiling', current: config.duckdb_memory_limit },
                { env: 'FPULSE_DUCKDB_THREADS', desc: 'DuckDB thread cap per worker (0 = auto)', current: config.duckdb_threads },
                { env: 'FPULSE_MODE', desc: 'Runtime mode: dev (permissive) or prod (guardrails)', current: config.mode },
              ].map(item => (
                <div key={item.env} className="group flex items-center gap-3 px-3 py-2 rounded-lg bg-slate-50 hover:bg-slate-100 transition-colors">
                  <code className="text-xs font-mono text-violet-600 bg-violet-50 px-1.5 py-0.5 rounded shrink-0">
                    {item.env}
                  </code>
                  <span className="text-slate-500 flex-1">{item.desc}</span>
                  <span className="text-slate-700 font-semibold shrink-0">{item.current}</span>
                  {/* 2026-06-03 — copy-as-`export FPULSE_X=value` button.
                      Hover-revealed so the row layout stays clean at rest.
                      Closes the read-only loop: user sees value → one click
                      → ready to paste into env file / docker-compose /
                      systemd / shell. */}
                  <button
                    type="button"
                    onClick={() => copyEnvAssignment(item.env, item.current)}
                    className="shrink-0 ml-1 opacity-0 group-hover:opacity-100 transition-opacity p-1 rounded text-slate-400 hover:text-violet-600 hover:bg-violet-50"
                    title={`Copy: export ${item.env}=${item.current}`}
                  >
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <rect x="9" y="9" width="13" height="13" rx="2" />
                      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                    </svg>
                  </button>
                </div>
              ))}
            </div>
          </div>

          {/* Spill-disk health card — surfaces the #1 vertical-scaling footgun.
              DuckDB perf drops 10-100x when spill writes hit a slow disk. */}
          {config.spill && (
            <div className="rounded-lg border border-slate-200 shadow-sm bg-white p-5">
              <h3 className="text-sm font-bold text-slate-700 mb-3 flex items-center gap-2">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 12H2" /><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" /></svg>
                Spill-Disk Health
              </h3>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div className={`rounded-lg p-3 border ${
                  config.spill.disk_type === 'ssd' ? 'bg-emerald-50 border-emerald-200' :
                  config.spill.disk_type === 'hdd' ? 'bg-red-50 border-red-300' :
                  'bg-slate-50 border-slate-200'
                }`}>
                  <div className="text-xs font-bold uppercase tracking-wide text-slate-500">Disk Type</div>
                  <div className={`text-lg font-bold mt-1 ${
                    config.spill.disk_type === 'ssd' ? 'text-emerald-700' :
                    config.spill.disk_type === 'hdd' ? 'text-red-700' :
                    'text-slate-600'
                  }`}>
                    {config.spill.disk_type === 'ssd' ? 'SSD ✓' :
                     config.spill.disk_type === 'hdd' ? 'HDD ⚠' :
                     'Unknown'}
                  </div>
                  <div className="text-xs text-slate-500 mt-0.5">
                    {config.spill.disk_type === 'ssd' ? 'Optimal for spill workloads' :
                     config.spill.disk_type === 'hdd' ? 'Move spill to SSD/NVMe — performance impact 10-100x' :
                     'Could not detect (Linux only)'}
                  </div>
                </div>
                <div className={`rounded-lg p-3 border ${
                  config.spill.io_wait_status === 'healthy' ? 'bg-emerald-50 border-emerald-200' :
                  config.spill.io_wait_status === 'elevated' ? 'bg-amber-50 border-amber-200' :
                  'bg-red-50 border-red-300'
                }`}>
                  <div className="text-xs font-bold uppercase tracking-wide text-slate-500">IO-Wait</div>
                  <div className={`text-lg font-bold mt-1 ${
                    config.spill.io_wait_status === 'healthy' ? 'text-emerald-700' :
                    config.spill.io_wait_status === 'elevated' ? 'text-amber-700' :
                    'text-red-700'
                  }`}>
                    {config.spill.io_wait_percent}%
                  </div>
                  <div className="text-xs text-slate-500 mt-0.5">
                    {config.spill.io_wait_status === 'healthy' ? 'Disk keeping up with workers' :
                     config.spill.io_wait_status === 'elevated' ? 'Workers waiting on disk' :
                     'Disk saturated — bottleneck'}
                  </div>
                </div>
                <div className="rounded-lg bg-slate-50 p-3 border border-slate-200">
                  <div className="text-xs font-bold uppercase tracking-wide text-slate-500">Spill Directory</div>
                  <div className="text-xs font-mono text-slate-700 mt-1 break-all">{config.spill.directory}</div>
                </div>
              </div>
            </div>
          )}

          {/* Capacity notes — single-node engine, vertical scaling primer. */}
          <div className="rounded-lg border border-violet-200 shadow-sm bg-gradient-to-br from-violet-50 to-indigo-50 p-5">
            <div className="flex items-start gap-3">
              <div className="w-10 h-10 rounded-lg bg-violet-200 flex items-center justify-center shrink-0">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#7c3aed" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" /></svg>
              </div>
              <div className="flex-1 min-w-0">
                <h3 className="text-sm font-bold text-violet-900">Capacity on this node</h3>
                {/* 2026-06-03 — was "Tune the values above" which implied an
                    in-UI editor. The values are env vars (read-only display);
                    point to the deployment doc + restart sequence instead. */}
                <p className="text-xs text-violet-700 mt-1 leading-relaxed">
                  F-Pulse runs as a single-node engine. Vertical scaling
                  (worker count + DuckDB memory ceiling + spill directory)
                  handles pipelines well into the hundreds of gigabytes on a
                  tuned VPS. To change these, set the matching
                  <code className="mx-1 font-mono bg-violet-100 px-1.5 py-0.5 rounded text-[11px]">FPULSE_*</code>
                  environment variables before starting the backend and restart
                  — see <a href="https://github.com/hybridyn/fpulse/blob/main/docs/scaling.md#the-4-vertical-scaling-knobs" target="_blank" rel="noopener noreferrer" className="font-semibold underline hover:text-violet-900">scaling.md → vertical-scaling knobs</a>.
                </p>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ════════════════════════════ ALERTS TAB (PROD only) ════════════════════════════ */}
      {tab === 'alerts' && isProd && (
        <div className="space-y-4">
          {/* Honesty banner — alert-rule persistence ships in Plus v1.0.
              Per docs/PAGE_BY_PAGE_AUDIT.md (P0 #4, 2026-05-19). */}
          <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 flex items-start gap-3">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#d97706" strokeWidth="2" className="shrink-0 mt-0.5">
              <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
              <line x1="12" y1="9" x2="12" y2="13" />
              <line x1="12" y1="17" x2="12.01" y2="17" />
            </svg>
            <div className="min-w-0">
              <p className="text-xs font-bold text-amber-800">Preview — rules are session-only</p>
              <p className="text-xs text-amber-700 mt-0.5 leading-relaxed">
                You can shape rules here to evaluate the workflow, but they live in this browser tab only — a reload clears them and the
                watchdog daemon does not yet evaluate them.
              </p>
            </div>
          </div>
          {/* Header with + New Alert Rule */}
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-base font-bold text-slate-800">Compute-Usage Alert Rules</h2>
              <p className="text-xs text-slate-500 mt-0.5">Get notified when pool utilization, queue depth, throughput, or errors cross your thresholds.</p>
            </div>
            {isAdmin ? (
              <button
                onClick={() => setNewAlertOpen(true)}
                title="Preview only — rules won't survive a reload until Plus v1.0"
                className="px-4 py-2 text-white text-sm font-bold rounded-lg shadow-sm hover:shadow-md transition-all flex items-center gap-2"
                style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
                New preview rule
              </button>
            ) : (
              <div
                title="Only admins can create alert rules"
                className="text-xs font-semibold text-slate-400 bg-slate-50 border border-slate-200 px-3 py-2 rounded-lg"
              >
                Read-only — admins can create alert rules
              </div>
            )}
          </div>

          {/* Existing alert rules list */}
          <div className="rounded-lg border border-slate-200 shadow-sm bg-white">
            {poolAlerts.length === 0 ? (
              <div className="px-6 py-10 text-center">
                <div className="w-12 h-12 mx-auto mb-3 rounded-full bg-slate-100 flex items-center justify-center">
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" />
                  </svg>
                </div>
                <p className="text-sm font-semibold text-slate-700">No alert rules yet</p>
                <p className="text-xs text-slate-500 mt-1 max-w-md mx-auto">Define thresholds on compute metrics &mdash; e.g. "notify when utilization &gt; 80% for 10 minutes" &mdash; so your team catches saturation before pipelines fail.</p>
              </div>
            ) : (
              <div className="divide-y divide-slate-100">
                {poolAlerts.map((rule) => (
                  <div key={rule.id} className="px-4 py-3 flex items-center gap-3">
                    <div className={`w-2 h-2 rounded-full shrink-0 ${rule.enabled ? 'bg-emerald-500' : 'bg-slate-300'}`} />
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-semibold text-slate-800">{rule.name}</div>
                      <div className="text-xs text-slate-500 mt-0.5 font-mono">
                        {rule.metric} {rule.operator} {rule.threshold} <span className="text-slate-400">for {rule.window_minutes}min</span>
                        <span className="text-slate-400 ml-2">· channels: {rule.channels.join(', ')}</span>
                      </div>
                      {rule.last_triggered && (
                        <div className="text-xs text-amber-600 mt-0.5">Last triggered {new Date(rule.last_triggered).toLocaleString()}</div>
                      )}
                    </div>
                    {/* Enable/Disable + Delete are admin-only. Non-admins
                        see the rule (read-only) with a status pill instead
                        of a toggle, and no delete button. Matches the
                        "New Alert Rule" gate at the top of the tab. */}
                    {isAdmin ? (
                      <>
                        <button
                          onClick={() => setPoolAlerts(prev => prev.map(r => r.id === rule.id ? { ...r, enabled: !r.enabled } : r))}
                          className={`px-2.5 py-1 text-xs font-semibold rounded-md border transition-colors ${
                            rule.enabled ? 'bg-emerald-50 text-emerald-700 border-emerald-200' : 'bg-slate-50 text-slate-500 border-slate-200'
                          }`}
                        >
                          {rule.enabled ? 'Enabled' : 'Disabled'}
                        </button>
                        <button
                          onClick={() => setPoolAlerts(prev => prev.filter(r => r.id !== rule.id))}
                          className="w-7 h-7 rounded-lg flex items-center justify-center text-slate-300 hover:text-red-500 hover:bg-red-50 transition-all"
                          title="Delete rule"
                        >
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6" /><path d="M19 6l-2 14H7L5 6" /><path d="M9 6V4h6v2" /></svg>
                        </button>
                      </>
                    ) : (
                      <span
                        className={`px-2.5 py-1 text-xs font-semibold rounded-md border ${
                          rule.enabled ? 'bg-emerald-50 text-emerald-700 border-emerald-200' : 'bg-slate-50 text-slate-500 border-slate-200'
                        }`}
                        title="Only admins can modify alert rules"
                      >
                        {rule.enabled ? 'Enabled' : 'Disabled'}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Metric reference card */}
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-4">
            <h3 className="text-xs font-bold text-slate-500 uppercase tracking-wider mb-2">Available Metrics</h3>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 text-xs">
              {[
                { k: 'utilization_pct', label: 'Utilization %', hint: 'busy workers / max workers' },
                { k: 'queue_depth', label: 'Queue Depth', hint: 'jobs waiting to start' },
                { k: 'throughput_per_hour', label: 'Throughput / hr', hint: 'completed runs per hour' },
                { k: 'error_rate_pct', label: 'Error Rate %', hint: 'failed / total over window' },
                { k: 'busy_workers', label: 'Busy Workers', hint: 'concurrent active runs' },
              ].map(m => (
                <div key={m.k} className="px-2.5 py-1.5 bg-white rounded border border-slate-200">
                  <div className="font-mono text-slate-700 font-semibold">{m.k}</div>
                  <div className="text-slate-500 text-xs mt-0.5">{m.hint}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ── New Alert Rule modal ── */}
      {newAlertOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" onClick={() => setNewAlertOpen(false)}>
          <div className="absolute inset-0 bg-black/40" />
          <div className="relative bg-white rounded-2xl shadow-2xl border border-slate-200 w-full max-w-lg mx-4" onClick={e => e.stopPropagation()}>
            <div className="px-5 py-4 border-b border-slate-200 flex items-center justify-between">
              <div>
                <h2 className="text-sm font-bold text-slate-800">New Compute-Usage Alert</h2>
                <p className="text-xs text-slate-500 mt-0.5">Triggered when the condition holds continuously for the chosen window.</p>
              </div>
              <button onClick={() => setNewAlertOpen(false)} className="text-slate-400 hover:text-slate-600">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
              </button>
            </div>

            <div className="p-5 space-y-3">
              <div>
                <label className="text-xs font-bold text-slate-500 uppercase tracking-wider mb-1 block">Rule Name</label>
                <input
                  type="text"
                  value={draftAlert.name || ''}
                  onChange={e => setDraftAlert({ ...draftAlert, name: e.target.value })}
                  placeholder="e.g. Pool saturation"
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300"
                />
              </div>

              <div className="grid grid-cols-3 gap-2">
                <div>
                  <label className="text-xs font-bold text-slate-500 uppercase tracking-wider mb-1 block">Metric</label>
                  <select
                    value={draftAlert.metric}
                    onChange={e => setDraftAlert({ ...draftAlert, metric: e.target.value as PoolAlertRule['metric'] })}
                    className="w-full px-2 py-2 text-xs border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-blue-300"
                  >
                    <option value="utilization_pct">Utilization %</option>
                    <option value="queue_depth">Queue Depth</option>
                    <option value="throughput_per_hour">Throughput / hr</option>
                    <option value="error_rate_pct">Error Rate %</option>
                    <option value="busy_workers">Busy Workers</option>
                  </select>
                </div>
                <div>
                  <label className="text-xs font-bold text-slate-500 uppercase tracking-wider mb-1 block">Operator</label>
                  <select
                    value={draftAlert.operator}
                    onChange={e => setDraftAlert({ ...draftAlert, operator: e.target.value as PoolAlertRule['operator'] })}
                    className="w-full px-2 py-2 text-xs border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-blue-300"
                  >
                    <option value=">">&gt;</option>
                    <option value=">=">&gt;=</option>
                    <option value="<">&lt;</option>
                    <option value="<=">&lt;=</option>
                    <option value="==">=</option>
                  </select>
                </div>
                <div>
                  <label className="text-xs font-bold text-slate-500 uppercase tracking-wider mb-1 block">Threshold</label>
                  <input
                    type="number"
                    value={draftAlert.threshold || 0}
                    onChange={e => setDraftAlert({ ...draftAlert, threshold: parseFloat(e.target.value) || 0 })}
                    className="w-full px-2 py-2 text-xs border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300"
                  />
                </div>
              </div>

              <div>
                <label className="text-xs font-bold text-slate-500 uppercase tracking-wider mb-1 block">Window (minutes)</label>
                <input
                  type="number"
                  min={1}
                  max={1440}
                  value={draftAlert.window_minutes || 10}
                  onChange={e => setDraftAlert({ ...draftAlert, window_minutes: parseInt(e.target.value) || 10 })}
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300"
                />
                <p className="text-xs text-slate-400 mt-1">Condition must hold continuously for this window before firing.</p>
              </div>

              <div>
                <label className="text-xs font-bold text-slate-500 uppercase tracking-wider mb-1 block">Notification Channels</label>
                <div className="flex flex-wrap gap-1.5">
                  {['email', 'slack', 'webhook'].map(ch => {
                    const active = draftAlert.channels?.includes(ch);
                    return (
                      <button
                        key={ch}
                        onClick={() => {
                          const cs = new Set(draftAlert.channels || []);
                          active ? cs.delete(ch) : cs.add(ch);
                          setDraftAlert({ ...draftAlert, channels: Array.from(cs) });
                        }}
                        className={`px-3 py-1.5 text-xs font-semibold rounded-lg border transition-colors ${
                          active
                            ? 'bg-blue-50 border-blue-300 text-blue-700'
                            : 'bg-white border-slate-200 text-slate-500 hover:bg-slate-50'
                        }`}
                      >
                        {ch}
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>

            <div className="px-5 py-3 border-t border-slate-200 bg-slate-50 rounded-b-2xl flex items-center justify-end gap-2">
              <button
                onClick={() => setNewAlertOpen(false)}
                className="px-4 py-2 text-xs font-semibold text-slate-600 hover:text-slate-800 rounded-lg hover:bg-slate-100 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  if (!draftAlert.name || !draftAlert.metric) return;
                  const rule: PoolAlertRule = {
                    id: `pool-alert-${Date.now()}`,
                    name: draftAlert.name!,
                    metric: draftAlert.metric!,
                    operator: draftAlert.operator || '>',
                    threshold: draftAlert.threshold ?? 0,
                    window_minutes: draftAlert.window_minutes ?? 10,
                    channels: draftAlert.channels || ['email'],
                    enabled: true,
                    created_at: new Date().toISOString(),
                  };
                  setPoolAlerts(prev => [...prev, rule]);
                  setNewAlertOpen(false);
                  setDraftAlert({ name: '', metric: 'utilization_pct', operator: '>', threshold: 80, window_minutes: 10, channels: ['email'], enabled: true });
                }}
                disabled={!draftAlert.name}
                className="px-4 py-2 text-white text-sm font-bold rounded-lg shadow-sm hover:shadow-md transition-all disabled:opacity-50"
                style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
              >
                Create Rule
              </button>
            </div>
          </div>
        </div>
      )}

      </div>
      </div>
    </div>
  );
}
