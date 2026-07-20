import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { api } from '../../api/client';
import { canonicalRole } from '../../auth/permissions';
import { navigateWithQuery } from '../../router';
import TierChip from '../shared/TierChip';
import PageHeader from '../shared/PageHeader';
import RuntimeDepsBanner from '../shared/RuntimeDepsBanner';
import { usePageContext } from '../../hooks/usePageContext';

/**
 * DashboardPage — v4 · welcoming greeting + dense KPIs + system usage
 * + admin-only row + smart empty states. DEV and PROD branches share
 * primitives but diverge on content.
 *
 * Design principles (ranked, 2026-04-21):
 *   1. MEANINGFUL — only real backend metrics. Never show a fake 100%
 *      success when there are zero runs; show "—" instead.
 *   2. NO REDUNDANCY — each metric appears once. If two blocks would
 *      both report the same number we drop one.
 *   3. DYNAMIC — empty states replace their row with useful content
 *      (system usage, CTA) instead of leaving a ghost chart.
 *   4. WELCOMING — the first thing the user sees is a greeting with
 *      date and time. "Good morning, Alex" beats "Admin's Workspace".
 *   5. DEV ≠ PROD — different KPIs, different charts, different feeds,
 *      different donut semantics.
 *   6. BARE-EYE READABLE — floor 14px labels, 16px rows, 24px+ numbers.
 *
 * Row layout:
 *   1. Greeting + date/time + status + primary CTA
 *   2. Hero KPIs (4 gradient, env-specific)
 *   3. Workspace inventory (DEV) / Operations (PROD) — 6 flat cards
 *   4. System usage — 6 flat cards (CPU, memory, threads, throughput,
 *      DB size, uptime) — same in both envs, always real
 *   5. Admin-only — 4 flat cards (seats, users, workspaces, your role)
 *      — only rendered when isAdmin
 *   6. Chart + donut — smart empty states when no activity
 *   7. Three feed tables — env-specific
 */

type Environment = 'dev' | 'prod';
type Tier = 'free' | 'plus';

interface DashboardPageProps {
  onNavigate: (page: string) => void;
  userName?: string;
  environment?: Environment;
  tier?: Tier;
}

interface Stats {
  pipelines: number;
  projects: number;
  connections: number;
  credentials: number;
  schedules: number;
  variables: number;
  executions: {
    total: number;
    success: number;
    failed: number;
    running: number;
    success_rate: number;
    avg_duration_ms: number;
    // 2026-05-28 — production-only sub-dict (scheduled / webhook /
    // replay triggers). The headline Success Rate KPI uses this so
    // manual test runs the user fires while iterating don't drag the
    // operational health number down. Optional for backward compat —
    // if the backend doesn't return it we fall back to the legacy
    // all-runs success_rate above.
    scheduled?: {
      total: number;
      success: number;
      failed: number;
      running: number;
      success_rate: number;
    };
  };
  recentExecutions: any[];
  activeSchedules: any[];
  failedPipelines: any[];
  pendingApprovals: any[];
  pool: { utilization_pct?: number; busy_workers?: number; total_workers?: number; queue_depth?: number; throughput_per_hour?: number; cpu_percent?: number } | null;
  system: { uptime_seconds?: number; rss_mb?: number; vms_mb?: number; threads?: number; host?: { cpu_count?: number; total_memory_mb?: number; available_memory_mb?: number }; db_files?: Array<{ path: string; size_bytes: number }> } | null;
  license: { tier?: string; is_plus?: boolean; seats?: number; org?: string } | null;
  users: any[];
  // 2026-05-25 — Storage rollup for the workspace inventory row.
  storage: { file_count?: number; table_count?: number; output_count?: number; file_size_bytes?: number; table_size_bytes?: number; output_size_bytes?: number; total_size_bytes?: number; trash_count?: number; trash_size_bytes?: number } | null;
}

function getUserTimezone(): string {
  try {
    const raw = localStorage.getItem('fpulse-settings');
    if (raw) {
      const parsed = JSON.parse(raw);
      const tz = parsed?.general?.timezone;
      if (typeof tz === 'string' && tz) return tz;
    }
  } catch {}
  return 'UTC';
}
function formatTimezoneAbbr(tz: string): string {
  try {
    const parts = new Intl.DateTimeFormat('en-US', { timeZone: tz, timeZoneName: 'short' }).formatToParts(new Date());
    const p = parts.find(x => x.type === 'timeZoneName');
    return p ? p.value : tz;
  } catch { return tz; }
}
function formatDateLong(tz: string): string {
  return new Date().toLocaleDateString('en-US', {
    weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
    timeZone: tz,
  });
}
function formatClock(tz: string): string {
  return new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: tz });
}
function formatTimeShort(): string {
  return new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
}
function formatTimeAgo(ts?: string): string {
  if (!ts) return '—';
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}
function formatDuration(ms?: number): string {
  if (!ms) return '—';
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}m`;
}
function formatUptime(s: number): string {
  if (s <= 0) return '—';
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}
function formatMB(mb?: number): string {
  if (!mb || mb <= 0) return '—';
  if (mb < 1024) return `${Math.round(mb)} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
}
function formatBytes(b?: number): string {
  if (!b || b <= 0) return '—';
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(0)} KB`;
  if (b < 1024 * 1024 * 1024) return `${(b / (1024 * 1024)).toFixed(1)} MB`;
  return `${(b / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}
function getGreeting(): string {
  // Universal welcome — time-of-day variants ("Good night", "Working
  // late") could read as the app judging the user's schedule. "Welcome
  // back" works for any hour and feels consistent across shifts.
  return 'Welcome back';
}

// ── Palette tokens ───────────────────────────────────────────────────────
const palette = {
  dev: {
    canvas: 'bg-canvas-bg',
    card: 'bg-white border-slate-200',
    ink: 'text-slate-800',
    inkMuted: 'text-slate-500',
    accent: 'text-blue-600',
    accentBg: 'bg-blue-600 hover:bg-blue-700',
    greetingBg: 'bg-gradient-to-br from-blue-50 via-white to-indigo-50 border-blue-100',
    ok: 'text-emerald-600',  okBg: 'bg-emerald-500',
    warn: 'text-amber-600',  warnBg: 'bg-amber-500',
    bad: 'text-red-600',     badBg: 'bg-red-500',
  },
  prod: {
    canvas: 'bg-slate-50',
    card: 'bg-white border-slate-200',
    ink: 'text-slate-900',
    inkMuted: 'text-slate-500',
    accent: 'text-red-600',
    accentBg: 'bg-red-600 hover:bg-red-700',
    // Light greeting card for PROD — subtle rose tint signals the env
    // (it's the production environment) without going full-dark like
    // the old slate-900 treatment. Matches the light canvas underneath
    // and keeps the greeting welcoming rather than somber.
    greetingBg: 'bg-gradient-to-br from-white via-rose-50/40 to-slate-50 border-slate-200',
    ok: 'text-emerald-600',  okBg: 'bg-emerald-500',
    warn: 'text-amber-600',  warnBg: 'bg-amber-500',
    bad: 'text-red-600',     badBg: 'bg-red-500',
  },
};

// ── Sparkline ────────────────────────────────────────────────────────────
function Sparkline({ data, color = '#ffffff', fill = true, height = 28 }: { data: number[]; color?: string; fill?: boolean; height?: number }) {
  if (!data || data.length === 0) return <div style={{ height }} />;
  const W = 120;
  const H = height;
  const max = Math.max(1, ...data);
  const stepX = data.length > 1 ? W / (data.length - 1) : 0;
  const points = data.map((v, i) => `${i * stepX},${H - (v / max) * (H - 4) - 2}`).join(' ');
  const areaPath = `M 0,${H} L ${points.split(' ').join(' L ')} L ${W},${H} Z`;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none" aria-hidden="true">
      {fill && <path d={areaPath} fill={color} fillOpacity={0.15} />}
      <polyline points={points} fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ── Hero KPI ─────────────────────────────────────────────────────────────
// Glowing halo shadow colour-matched to the card's gradient family, plus
// a soft inner highlight for a 3D feel. Height collapses to number+trend
// only when no sparkline data is present, so "—" KPIs don't reserve
// empty real estate.
function glowFor(gradient: string): string {
  // Map the gradient family → rgba so the halo reads as an extension of
  // the card itself rather than a generic drop-shadow.
  if (gradient.includes('emerald')) return 'rgba(16,185,129,0.45)';
  if (gradient.includes('rose') || gradient.includes('red')) return 'rgba(239,68,68,0.45)';
  if (gradient.includes('orange') || gradient.includes('amber')) return 'rgba(245,158,11,0.45)';
  if (gradient.includes('purple') || gradient.includes('violet')) return 'rgba(139,92,246,0.45)';
  if (gradient.includes('indigo') || gradient.includes('blue')) return 'rgba(59,130,246,0.45)';
  if (gradient.includes('slate')) return 'rgba(100,116,139,0.25)';
  return 'rgba(100,116,139,0.2)';
}

function HeroKPI({
  label, value, suffix, sparkline, gradient, trend, onClick, lighter,
}: {
  label: string;
  value: string | number;
  suffix?: string;
  sparkline?: number[];
  gradient: string;
  trend?: { arrow: '▲' | '▼' | '—'; text: string; tone: 'ok' | 'bad' | 'muted' };
  onClick?: () => void;
  /** When true, shift every shade in the gradient one step lighter
   *  (500→400, 600→500, etc.). Used to distinguish the DEV dashboard
   *  (softer, building-friendly) from PROD (richer, operational). */
  lighter?: boolean;
}) {
  // Text colour branches on `lighter`: DEV (lighter gradients) gets black
  // text for crisp readability; PROD (richer 500-600 gradients) keeps
  // white for contrast — black on PROD's darker emerald/red would muddy.
  const darkText = !!lighter;
  const tone = darkText
    ? (trend?.tone === 'ok' ? 'text-emerald-900' : trend?.tone === 'bad' ? 'text-rose-900' : 'text-slate-700')
    : (trend?.tone === 'ok' ? 'text-emerald-100' : trend?.tone === 'bad' ? 'text-rose-100' : 'text-white/80');
  const hasSpark = sparkline && sparkline.length > 0 && sparkline.some(v => v > 0);
  // Shift shades one step lighter for DEV. Done via regex on the gradient
  // string so callers don't have to maintain two parallel palettes.
  const finalGradient = lighter
    ? gradient
        .replace(/-900\b/g, '-800')
        .replace(/-800\b/g, '-700')
        .replace(/-700\b/g, '-600')
        .replace(/-600\b/g, '-500')
        .replace(/-500\b/g, '-400')
    : gradient;
  const glow = glowFor(finalGradient);
  // Map the gradient color family to a matching darker border so each KPI
  // card carries its accent color on the edge as well as the fill.
  const borderClass =
    finalGradient.includes('emerald') ? 'border-emerald-600' :
    finalGradient.includes('rose') || finalGradient.includes('red') ? 'border-rose-600' :
    finalGradient.includes('amber') || finalGradient.includes('orange') ? 'border-orange-500' :
    finalGradient.includes('indigo') || finalGradient.includes('blue') ? 'border-indigo-600' :
    finalGradient.includes('violet') || finalGradient.includes('purple') ? 'border-purple-600' :
    finalGradient.includes('slate') ? 'border-slate-500' : 'border-slate-400';
  const Cmp: any = onClick ? 'button' : 'div';
  return (
    <Cmp
      onClick={onClick}
      // 2026-05-25 — toned down for enterprise-quieter look:
      // - dropped the colored glow shadow (was 28px blur w/ accent color)
      // - reduced base shadow opacity ~40%
      // - removed two decorative white blur circles
      // - thinned border to 1px so the card feels lighter
      // Status color is still carried by the gradient fill + border
      // accent; loss of glow doesn't reduce information density.
      className={`relative overflow-hidden rounded-lg border ${borderClass} bg-gradient-to-br ${finalGradient} px-4 py-3 transition-all duration-200 hover:-translate-y-0.5 ${onClick ? 'cursor-pointer text-left w-full' : ''}`}
      style={{
        boxShadow: '0 2px 6px -2px rgba(15,23,42,0.08), inset 0 1px 0 rgba(255,255,255,0.14)',
      }}
    >
      {/* Content stack — centered. Text colour follows `darkText`: DEV
          cards use slate-900, PROD uses white for readability on the
          richer gradient backgrounds. */}
      <div className={`relative text-xs font-bold uppercase tracking-wide text-center ${darkText ? 'text-slate-900' : 'text-white/95'}`}>{label}</div>
      <div className="relative mt-0.5 flex items-baseline justify-center gap-1.5">
        <span className={`text-2xl font-extrabold tabular-nums leading-none ${darkText ? 'text-slate-900' : 'text-white drop-shadow-sm'}`}>{value}</span>
        {suffix && <span className={`text-sm font-semibold ${darkText ? 'text-slate-800' : 'text-white/85'}`}>{suffix}</span>}
      </div>
      {trend && (
        <div className={`relative mt-1 text-xs font-semibold text-center ${tone}`}>
          <span className="mr-1">{trend.arrow}</span>{trend.text}
        </div>
      )}
      {hasSpark && (
        <div className="relative mt-1.5 -mx-1"><Sparkline data={sparkline!} color="#ffffff" fill height={22} /></div>
      )}
    </Cmp>
  );
}

// ── Flat KPI ─────────────────────────────────────────────────────────────
// Two visual variants so successive rows of compact KPIs don't look
// identical:
//   • `variant="gradient"` (default) — pale accent gradient wash fills
//     the card body. Used for Workspace Inventory row.
//   • `variant="striped"` — plain white body + bold left stripe in the
//     accent colour. Used for the System row so it reads as a different
//     category of information at a glance.
// Both share the coloured icon chip on the right.
function FlatKPI({
  label, value, accent = 'blue', suffix, onClick, hint, icon, variant = 'gradient',
  loading = false,
}: {
  label: string;
  value: string | number;
  accent?: 'blue' | 'emerald' | 'red' | 'amber' | 'violet' | 'cyan' | 'slate' | 'rose' | 'teal';
  suffix?: string;
  onClick?: () => void;
  hint?: string;
  icon?: React.ReactNode;
  variant?: 'gradient' | 'striped';
  /** K2 (2026-05-23): when true, the value renders as a pulsing
   *  skeleton bar instead of `0`. Use during the initial stats fetch
   *  so users see "loading" instead of a misleading zero count. */
  loading?: boolean;
}) {
  const stripeColor: Record<string, string> = {
    blue: 'bg-blue-500', emerald: 'bg-emerald-500', red: 'bg-red-500',
    amber: 'bg-amber-500', violet: 'bg-violet-500', cyan: 'bg-cyan-500',
    slate: 'bg-slate-400', rose: 'bg-rose-500', teal: 'bg-teal-500',
  };
  // Pale gradient wash fills the entire card body — no coloured border
  // needed because the card itself now carries the accent colour. Two-tier
  // hierarchy: hero KPIs use saturated 500→700 gradients, flat KPIs use
  // pale 50→100 gradients. Each row reads at a glance without fighting
  // the heroes for attention.
  const chip: Record<string, string> = {
    blue: 'bg-blue-100 text-blue-700',
    emerald: 'bg-emerald-100 text-emerald-700',
    red: 'bg-red-100 text-red-700',
    amber: 'bg-amber-100 text-amber-700',
    violet: 'bg-violet-100 text-violet-700',
    cyan: 'bg-cyan-100 text-cyan-700',
    slate: 'bg-slate-100 text-slate-700',
    rose: 'bg-rose-100 text-rose-700',
    teal: 'bg-teal-100 text-teal-700',
  };
  // Pale gradient per accent — tinted enough to read the category at a
  // glance, faint enough to not compete with the hero KPIs. The "from"
  // stop uses -50, the "to" stop uses -100; shifting the darker end to
  // the bottom-right so the label area (top-left) stays on the lighter
  // half for legibility.
  const wash: Record<string, string> = {
    blue: 'from-blue-50 to-blue-100',
    emerald: 'from-emerald-50 to-emerald-100',
    red: 'from-red-50 to-red-100',
    amber: 'from-amber-50 to-amber-100',
    violet: 'from-violet-50 to-violet-100',
    cyan: 'from-cyan-50 to-cyan-100',
    slate: 'from-slate-50 to-slate-100',
    rose: 'from-rose-50 to-rose-100',
    teal: 'from-teal-50 to-teal-100',
  };
  const Cmp: any = onClick ? 'button' : 'div';
  const isStriped = variant === 'striped';
  const bgClass = isStriped ? 'bg-white' : `bg-gradient-to-br ${wash[accent]}`;
  const pad = isStriped ? 'p-3 pl-4' : 'p-3';
  // N2 (2026-05-23): a11y — clickable cards get a real <button> with
  // an aria-label describing the action ("Open Connections — 5 saved
  // endpoints"), focus-visible ring matches the accent, and a clear
  // hint string is woven into the label for screen readers. Static
  // cards get role="group" + aria-label so the value reads as a
  // statistic rather than an isolated number.
  const accentColor = chip[accent].split(' ')[1] || 'text-slate-700';
  const ariaLabel = onClick
    ? `Open ${label}${typeof value === 'number' || typeof value === 'string' ? ` — ${value}${suffix ? ' ' + suffix : ''}` : ''}${hint ? `, ${hint}` : ''}`
    : `${label}: ${value}${suffix ? ' ' + suffix : ''}${hint ? ` (${hint})` : ''}`;
  return (
    <Cmp
      onClick={onClick}
      type={onClick ? 'button' : undefined}
      aria-label={ariaLabel}
      role={onClick ? undefined : 'group'}
      className={`relative group rounded-lg border border-slate-200 shadow-sm ${pad} overflow-hidden ${bgClass} ${onClick ? 'text-left hover:shadow-md hover:-translate-y-0.5 transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-amber-400' : ''}`}
    >
      {isStriped && (
        <span className={`absolute left-0 top-0 bottom-0 w-1.5 ${stripeColor[accent]}`} aria-hidden="true" />
      )}
      {icon && (
        <div className={`absolute top-2.5 right-2.5 w-7 h-7 rounded-lg flex items-center justify-center ${chip[accent]}`} aria-hidden="true">
          {icon}
        </div>
      )}
      <div className={`text-xs font-bold uppercase tracking-wide text-center ${accentColor}`}>{label}</div>
      <div className="mt-1 flex items-baseline justify-center gap-1" aria-hidden={loading ? 'true' : undefined}>
        {loading ? (
          // K2: pulsing skeleton bar so the user sees "loading", not "0".
          <span className="inline-block h-7 w-14 rounded-md bg-slate-200/80 animate-pulse" />
        ) : (
          <>
            <span className="text-2xl font-extrabold tabular-nums text-slate-900 leading-none">{value}</span>
            {suffix && <span className="text-sm font-semibold text-slate-500">{suffix}</span>}
          </>
        )}
      </div>
      {hint && (
        <div className="mt-1 text-xs font-medium text-slate-500 text-center truncate" aria-hidden="true">
          {hint}
        </div>
      )}
    </Cmp>
  );
}

// ── Tiny icons for KPI chips ────────────────────────────────────────────
const kpiIcons = {
  pipelines:  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>,
  projects:   <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>,
  connection: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>,
  credential: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>,
  schedule:   <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>,
  variable:   <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M4 17h16"/><path d="M10 12h4"/><path d="M7 7h10"/></svg>,
  cpu:        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/></svg>,
  memory:     <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="6" width="20" height="12" rx="2"/><line x1="6" y1="10" x2="6" y2="14"/><line x1="10" y1="10" x2="10" y2="14"/><line x1="14" y1="10" x2="14" y2="14"/><line x1="18" y1="10" x2="18" y2="14"/></svg>,
  threads:    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 1v6"/><path d="M12 17v6"/><path d="M4.22 4.22l4.24 4.24"/><path d="M15.54 15.54l4.24 4.24"/></svg>,
  throughput: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>,
  db:         <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>,
  uptime:     <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="12 2 12 6 16 6"/><path d="M12 6a10 10 0 1 0 10 10"/></svg>,
  seat:       <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>,
  user:       <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>,
  shield:     <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>,
  building:   <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><rect x="4" y="2" width="16" height="20" rx="2"/><line x1="9" y1="6" x2="15" y2="6"/><line x1="9" y1="10" x2="15" y2="10"/><line x1="9" y1="14" x2="15" y2="14"/></svg>,
};

// ── MetricStrip — compact inventory line (2026-05-25, polished) ──────
// Replaces the tiled card grids for Workspace / Storage / System.
// One shared component → identical height, spacing, title width across
// all three rows so they read as a single "Workspace Overview" block.
//
// Polished pass (2026-05-25 final):
//   - Subtle colored LEFT RAIL per section (blue/emerald/cyan) gives
//     identity without making them coloured cards.
//   - Metrics separated by vertical DIVIDERS (semantic grouping).
//   - Color reserved for actionable values (alert=amber/red), normal
//     values stay neutral so the eye picks out trouble at a glance.
//   - Label small + grey, value bold + dark.
//   - "—" muted values can be hidden via `hideEmpty` to avoid the strip
//     looking unfinished (System uses this).
type StripAccent = 'blue' | 'emerald' | 'violet' | 'slate';
function MetricStrip({
  icon, label, metrics, accent = 'slate', hideEmpty = false,
}: {
  icon: React.ReactNode;
  label: string;
  metrics: Array<{
    label: string;
    value: number | string;
    suffix?: string;
    onClick?: () => void;
    alert?: 'amber' | 'red' | false;
    muted?: boolean;
  }>;
  accent?: StripAccent;
  /** When true, hide any metric whose value is the em-dash placeholder
   *  ("—" or "-"). Lets a strip look complete even when the underlying
   *  numbers haven't surfaced yet (e.g. CPU on a fresh install). */
  hideEmpty?: boolean;
}) {
  // 2026-05-26 — Option A simplification:
  //   Identity color lives in BOTH the icon badge AND the tinted
  //   header strip (added v2, same day) so three side-by-side cards
  //   read as cohesive accent-coded panels beneath the hero KPIs.
  //   Removed: 4px saturated top rail, 2px colored outline, inner
  //   slate gradient. Reasoning is captured in chat 2026-05-26.
  //   Also swapped cyan → violet in the palette so System ties to
  //   the new brand accent (logo plate ring is also #A855F7).
  const badgeClass: Record<StripAccent, string> = {
    blue: 'bg-blue-100 text-blue-700 ring-blue-200',
    emerald: 'bg-emerald-100 text-emerald-700 ring-emerald-200',
    violet: 'bg-violet-100 text-violet-700 ring-violet-200',
    slate: 'bg-slate-100 text-slate-600 ring-slate-200',
  };
  // 2026-05-26 v3 — header bar uses the EXACT same silver gradient as
  // the FAILED PIPELINES / RECENT RUNS / ACTIVE SCHEDULES panels at
  // the bottom of the Dashboard. User feedback: per-accent header
  // tints fragmented the row visually; uniform silver ties every
  // titled card on the page into one family. Identity color still
  // lives in the icon badge so WORKSPACE / STORAGE / SYSTEM remain
  // visually distinguishable.
  const visible = hideEmpty
    ? metrics.filter((m) => m.value !== '—' && m.value !== '-' && m.value !== '')
    : metrics;
  return (
    <div className="relative rounded-xl border border-slate-300 bg-white overflow-hidden shadow-sm">
      {/* 2026-05-26 v3 — Header strip matches FAILED PIPELINES /
          RECENT RUNS / ACTIVE SCHEDULES exactly: silver gradient
          (slate-100 → slate-200) with slate-300 bottom border. Same
          family as every other titled card on the Dashboard. */}
      <div className="px-4 py-2.5 border-b border-slate-300 bg-gradient-to-b from-slate-100 to-slate-200">
        <div className="flex items-center gap-2">
          <span className={`h-7 w-7 rounded-md ring-1 flex items-center justify-center ${badgeClass[accent]}`}>{icon}</span>
          <span className="text-xs font-bold uppercase tracking-wider text-slate-700">{label}</span>
        </div>
      </div>
      {/* Body grid. `[&>*:nth-child(n+3)]:border-t` adds a 1px slate
          divider between consecutive rows (rows are 2-col, so item 3+
          starts row 2). Cleaner than relying on indexes from the map. */}
      <div className="px-4 py-2 grid grid-cols-2 gap-x-3 [&>*:nth-child(n+3)]:border-t [&>*:nth-child(n+3)]:border-slate-100">
          {visible.map((m, i) => {
            const Cmp: any = m.onClick ? 'button' : 'div';
            const valueClass =
              m.alert === 'red'   ? 'text-red-700' :
              m.alert === 'amber' ? 'text-amber-700' :
              m.muted             ? 'text-slate-400' :
                                    'text-slate-800';
            return (
              <Cmp
                key={i}
                onClick={m.onClick}
                type={m.onClick ? 'button' : undefined}
                className={`min-h-9 rounded-md px-2.5 py-1.5 flex items-baseline justify-between gap-3 text-left transition-colors ${m.onClick ? 'hover:bg-slate-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 cursor-pointer' : 'cursor-default'}`}
                title={m.onClick ? `Open ${m.label}` : m.label}
              >
                <span className={`text-[12px] leading-none truncate ${m.muted ? 'text-slate-400' : 'text-slate-500'}`}>{m.label}</span>
                <span className={`text-base font-bold tabular-nums leading-none whitespace-nowrap shrink-0 ${valueClass}`}>
                  {m.value}
                  {m.suffix ? <span className="text-[11px] font-semibold text-slate-400 ml-1">{m.suffix}</span> : null}
                </span>
              </Cmp>
            );
          })}
          {visible.length === 0 && (
            <span className="px-2.5 py-2 text-xs italic text-slate-400">No data yet</span>
          )}
      </div>
    </div>
  );
}

// ── Donut ────────────────────────────────────────────────────────────────
type OverviewMetricItem = {
  label: string;
  value: number | string;
  suffix?: string;
  onClick?: () => void;
  alert?: 'amber' | 'red' | false;
  muted?: boolean;
};

function WorkspaceOverview({
  rows,
}: {
  rows: Array<{
    icon: React.ReactNode;
    label: string;
    accent: StripAccent;
    metrics: OverviewMetricItem[];
  }>;
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-sm overflow-hidden">
      <div className="px-4 py-3 border-b border-slate-100 bg-slate-50/70">
        <div className="text-xs font-bold uppercase tracking-wider text-slate-700">Workspace Overview</div>
        <div className="text-xs text-slate-500 mt-0.5">Assets, storage, and runtime at a glance</div>
      </div>
      <div className="divide-y divide-slate-100">
        {rows.map((row) => (
          <OverviewRow key={row.label} {...row} />
        ))}
      </div>
    </section>
  );
}

function OverviewRow({
  icon, label, metrics, accent,
}: {
  icon: React.ReactNode;
  label: string;
  metrics: OverviewMetricItem[];
  accent: StripAccent;
}) {
  const tone: Record<StripAccent, string> = {
    blue: 'bg-blue-50 text-blue-700 ring-blue-100',
    emerald: 'bg-emerald-50 text-emerald-700 ring-emerald-100',
    violet: 'bg-violet-50 text-violet-700 ring-violet-100',
    slate: 'bg-slate-50 text-slate-600 ring-slate-100',
  };
  return (
    <div className="px-4 py-3 grid gap-3 lg:grid-cols-[132px_1fr] lg:items-center">
      <div className="flex items-center gap-2">
        <span className={`h-7 w-7 rounded-md ring-1 flex items-center justify-center ${tone[accent]}`} aria-hidden="true">
          {icon}
        </span>
        <span className="text-xs font-bold uppercase tracking-wider text-slate-700">{label}</span>
      </div>
      <div className="grid gap-2 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-6">
        {metrics.map((m, i) => (
          <OverviewMetric key={`${label}-${m.label}-${i}`} {...m} />
        ))}
      </div>
    </div>
  );
}

function OverviewMetric({
  label, value, suffix, onClick, alert, muted,
}: OverviewMetricItem) {
  const Cmp: any = onClick ? 'button' : 'div';
  const valueClass =
    alert === 'red'   ? 'text-red-700' :
    alert === 'amber' ? 'text-amber-700' :
    muted             ? 'text-slate-400' :
                        'text-slate-900';
  return (
    <Cmp
      onClick={onClick}
      type={onClick ? 'button' : undefined}
      className={`min-h-10 rounded-md px-3 py-2 text-left transition-colors ${onClick ? 'hover:bg-slate-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400' : ''}`}
      title={onClick ? `Open ${label}` : label}
      aria-label={`${label}: ${value}${suffix ? ` ${suffix}` : ''}`}
    >
      <div className="text-[11px] font-medium text-slate-500 leading-none">{label}</div>
      <div className={`mt-1 text-sm font-bold tabular-nums leading-none ${valueClass}`}>
        {value}
        {suffix ? <span className="ml-1 text-[11px] font-semibold text-slate-400">{suffix}</span> : null}
      </div>
    </Cmp>
  );
}

function StatusDonut({ segments, centerTop, centerBottom, ariaLabel }: {
  segments: { value: number; color: string; label: string }[];
  centerTop: string | number;
  centerBottom: string;
  ariaLabel: string;
}) {
  const total = Math.max(1, segments.reduce((a, s) => a + s.value, 0));
  const R = 38;
  const CIRC = 2 * Math.PI * R;
  let offset = 0;
  return (
    <div className="flex items-center gap-4" aria-label={ariaLabel}>
      <svg viewBox="0 0 100 100" width="110" height="110" className="shrink-0">
        <circle cx="50" cy="50" r={R} fill="none" stroke="#e2e8f0" strokeWidth="14" />
        {segments.map((s, i) => {
          if (s.value === 0) return null;
          const dash = (s.value / total) * CIRC;
          const el = (
            <circle key={i} cx="50" cy="50" r={R} fill="none" stroke={s.color} strokeWidth="14"
              strokeDasharray={`${dash} ${CIRC - dash}`} strokeDashoffset={-offset}
              transform="rotate(-90 50 50)"
              style={{ transition: 'stroke-dasharray 300ms ease' }} />
          );
          offset += dash;
          return el;
        })}
        <text x="50" y="46" textAnchor="middle" dominantBaseline="central" className="fill-slate-800 font-extrabold" fontSize="18">{centerTop}</text>
        <text x="50" y="62" textAnchor="middle" dominantBaseline="central" className="fill-slate-500 font-semibold" fontSize="8">{centerBottom}</text>
      </svg>
      <div className="space-y-1 text-sm">
        {segments.map((s, i) => (
          <div key={i} className="flex items-center gap-2 font-medium text-slate-600 tabular-nums">
            <span className="w-2.5 h-2.5 rounded-full" style={{ background: s.color }} />
            <span className="w-20 text-slate-500 truncate">{s.label}</span>
            <span className="font-bold text-slate-800">{s.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function DashboardPage({ onNavigate, userName, environment = 'dev', tier = 'free' }: DashboardPageProps) {
  const isProd = environment === 'prod';
  const p = isProd ? palette.prod : palette.dev;

  const user = (() => {
    try { return JSON.parse(localStorage.getItem('fpulse_user') || 'null'); }
    catch { return null; }
  })();
  const role = canonicalRole(user?.role);
  const isAdmin = role === 'admin' || role === 'super_admin';

  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState(formatTimeShort());
  const [now, setNow] = useState(Date.now());
  // 2026-05-25 — Dashboard layout final pass:
  //   - Status banner (existing) + smart headline that adapts to state
  //   - Needs Attention block (auto-shows on real signals; collapsible)
  //   - Activity hero row (4 status tiles)
  //   - Workspace / Storage / System as compact MetricStrips (not cards)
  //   - Bottom panels (sparkline / failures table / composition donut)
  // Collapsing the inventory behind a fold made the product feel smaller
  // than it is; metric strips give breadth at a glance without the
  // 16-tile chrome that made the earlier version feel heavy.
  // Default CLOSED — behaves as an alert summary, not a panel that
  // takes over the dashboard. Header shows the count + severity so an
  // operator can see "6 failures" at a glance without expanding;
  // expand only when triaging.
  const [attentionOpen, setAttentionOpen] = useState<boolean>(() => {
    try { return localStorage.getItem('fpulse_dashboard_attention_open') === '1'; }
    catch { return false; }
  });
  const toggleAttention = () => {
    setAttentionOpen((v) => {
      const next = !v;
      try { localStorage.setItem('fpulse_dashboard_attention_open', next ? '1' : '0'); } catch { /* ignore */ }
      return next;
    });
  };

  // 2026-06-17 — "Needs Attention" dismissal. A failure row can be cleared
  // (acknowledged) by the user. We remember the dismissed RUN id in
  // localStorage and hide that row. Because the key is the specific failed
  // run, the pipeline REAPPEARS the moment it fails again (new run id), and
  // it also drops naturally once it succeeds (current-state /monitor/failed).
  // So "Clear" means "I've seen this one" — not "mute forever". Per-browser
  // by design: this is a local-first acknowledgement, not shared team state.
  const DISMISSED_KEY = 'fpulse.dashboard.dismissedFailures';
  const failureKey = (f: any): string =>
    String(f?.id || f?.run_id || f?.execution_id || f?.workflow_id || f?.name || '');
  const [dismissedFailures, setDismissedFailures] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem(DISMISSED_KEY);
      return new Set<string>(raw ? JSON.parse(raw) : []);
    } catch { return new Set<string>(); }
  });
  const persistDismissed = (next: Set<string>) => {
    setDismissedFailures(next);
    try { localStorage.setItem(DISMISSED_KEY, JSON.stringify([...next])); } catch { /* ignore */ }
  };
  const dismissFailure = (f: any) => {
    const k = failureKey(f);
    if (!k) return;
    const next = new Set(dismissedFailures);
    next.add(k);
    persistDismissed(next);
  };
  const dismissFailures = (list: any[]) => {
    const next = new Set(dismissedFailures);
    list.forEach((f) => { const k = failureKey(f); if (k) next.add(k); });
    persistDismissed(next);
  };
  // Self-prune: forget dismissals whose failure is no longer current (the
  // pipeline recovered, or failed again with a NEW run id). Keeps the set
  // bounded and guarantees a brand-new failure is never silently pre-cleared.
  useEffect(() => {
    const live = stats?.failedPipelines;
    if (!Array.isArray(live)) return;
    const liveKeys = new Set(live.map((f: any) => failureKey(f)));
    setDismissedFailures((prev) => {
      let changed = false;
      const next = new Set<string>();
      prev.forEach((k) => { if (liveKeys.has(k)) next.add(k); else changed = true; });
      if (!changed) return prev;
      try { localStorage.setItem(DISMISSED_KEY, JSON.stringify([...next])); } catch { /* ignore */ }
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stats?.failedPipelines]);
  // Failures the user hasn't cleared — the SAME source the headline and the
  // Needs Attention card both read, so they always reconcile.
  const visibleFailedPipelines = (stats?.failedPipelines || []).filter(
    (f: any) => !dismissedFailures.has(failureKey(f))
  );
  // 2026-05-22 (audit J2 / K1) — parallel state for the new
  // /api/dashboard/summary endpoint. Carries per-section status so
  // the UI can show a "section failed" chip instead of silently
  // zeroing a KPI when one of the underlying stores hiccups. The
  // legacy `stats` chain stays as a fallback for sections the
  // summary doesn't cover (yet) and for the case where the summary
  // endpoint itself fails.
  const [summary, setSummary] = useState<any | null>(null);
  // 2026-05-22 (audit M2) — trend window control. 24h is the default;
  // the new summary endpoint accepts hours=24|168|720. Backend
  // re-buckets accordingly.
  const [trendHours, setTrendHours] = useState<24 | 168 | 720>(24);
  // L3 (2026-05-23) — project scope filter. 'all' = workspace-wide
  // (default); a project id narrows KPIs to that project. Backend
  // /api/dashboard/summary already accepts the project_id query param
  // (L1, 2026-05-22). The list is sourced from the same listProjects
  // call already happening in loadAll, but we hoist a small projects
  // state into the page so the selector can populate without an
  // extra fetch.
  const [projectFilter, setProjectFilter] = useState<string>('all');
  const [projectsList, setProjectsList] = useState<Array<{ id: string; name: string }>>([]);

  // 2026-05-22 (audit K3 + O1) — filter-aware navigation helper.
  // Delegates to the shared `navigateWithQuery` helper in router.ts
  // which writes the hash so both the App.tsx hash listener and the
  // destination page's `readHashQuery()` parse the same way. Pre-O1
  // we wrote the hash ourselves and the page-id split was broken.
  const navigateWithFilter = (page: string, filters?: Record<string, string | number>) => {
    if (!filters || Object.keys(filters).length === 0) {
      onNavigate(page);
      return;
    }
    try {
      navigateWithQuery(page as any, filters);
    } catch {
      onNavigate(page);
    }
  };

  // 2026-06-17 — "Needs Attention" rows now deep-link to the ACTUAL failed
  // run (#executions/<run_id> opens that run's detail: the error, the failed
  // step, and the suggested fix) rather than dumping the user on a filtered
  // list. `/api/monitor/failed` returns the most-recent failed execution per
  // pipeline, so `f.id` is that run's id. Falls back to the filtered list
  // only when no run id is present.
  const openFailedRun = (f: any) => {
    const runId = f?.id || f?.run_id || f?.execution_id;
    if (runId) {
      window.location.hash = `executions/${encodeURIComponent(String(runId))}`;
    } else {
      navigateWithFilter('executions', { status: 'failed', workflow_id: f?.workflow_id });
    }
  };

  // Publish a compact dashboard snapshot for the AI Copilot — lets the
  // agent answer "what's the failure rate this week?" / "what failed
  // overnight?" without calling the metrics tool.
  usePageContext({
    page: 'dashboard',
    environment,
    // 2026-05-22 (audit M3): payload extended with failed pipeline
    // names, active schedule count, pool utilization, queue depth,
    // memory pressure flag, and a generated_at timestamp so the
    // Copilot can answer "what failed today?" / "is the worker pool
    // saturated?" without making a fresh tool call.
    visible_items: stats
      ? [
          {
            id: 'workspace',
            name: 'Workspace summary',
            kind: 'summary',
            meta: {
              pipelines: stats.pipelines ?? 0,
              connections: stats.connections ?? 0,
              schedules: stats.schedules ?? 0,
              active_schedules: stats.activeSchedules?.length ?? 0,
              exec_total: stats.executions?.total ?? 0,
              exec_failed: stats.executions?.failed ?? 0,
              exec_running: stats.executions?.running ?? 0,
              success_rate: stats.executions?.success_rate ?? null,
              // Pool + system signals — capped so the payload stays
              // small. Copilot uses these to phrase "pool is busy"
              // style answers.
              pool_utilization_pct: stats.pool?.utilization_pct ?? null,
              pool_queue_depth: stats.pool?.queue_depth ?? null,
              memory_pressure: (() => {
                const rss = stats.system?.rss_mb || 0;
                const total = stats.system?.host?.total_memory_mb || 0;
                if (!total) return null;
                const pct = (rss / total) * 100;
                if (pct >= 80) return 'high';
                if (pct >= 60) return 'moderate';
                return 'low';
              })(),
              // Per-section dashboard-summary status so Copilot can
              // disclaim "some sections couldn't be loaded" rather
              // than reciting potentially-stale numbers.
              summary_section_status: summary
                ? {
                    inventory:  summary.inventory?.status ?? null,
                    executions: summary.executions?.status ?? null,
                    pool:       summary.pool?.status ?? null,
                    system:     summary.system?.status ?? null,
                  }
                : null,
              generated_at: summary?.generated_at ?? null,
            },
          },
          // Top failed pipelines (audit M3) — names only, no
          // step details, stays under the page-context cap.
          ...((summary?.top_failed?.data || stats.failedPipelines || []).slice(0, 5).map((f: any) => ({
            id: String(f.workflow_id || f.id || ''),
            name: f.workflow_name || f.name || 'pipeline',
            kind: 'failed_pipeline',
            meta: {
              failure_count: f.failure_count ?? null,
              last_failed_at: f.last_failed_at || f.completed_at || null,
            },
          }))),
          ...(stats.recentExecutions || []).slice(0, 10).map((e: any) => ({
            id: String(e.id ?? e.run_id ?? ''),
            name: e.workflow_name ?? 'execution',
            kind: 'execution',
            status: e.status,
            meta: {
              started_at: e.started_at ?? null,
              duration_ms: e.duration_ms ?? null,
            },
          })),
        ]
      : [],
  });
  // Dashboard-scoped timezone. Persisted under its OWN localStorage key
  // (`fpulse-dashboard-tz`) so changing it here doesn't override the global
  // Settings timezone. Falls back to the global Settings tz, then UTC.
  const [userTz, setUserTz] = useState<string>(() => {
    try {
      const dash = localStorage.getItem('fpulse-dashboard-tz');
      if (dash) return dash;
    } catch {}
    return getUserTimezone();
  });
  const [tzMenuOpen, setTzMenuOpen] = useState(false);
  const tzMenuRef = useRef<HTMLDivElement>(null);
  const handleTzChange = (tz: string) => {
    setUserTz(tz);
    setTzMenuOpen(false);
    try { localStorage.setItem('fpulse-dashboard-tz', tz); } catch {}
  };
  // Close the timezone popover on outside click
  useEffect(() => {
    if (!tzMenuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (tzMenuRef.current && !tzMenuRef.current.contains(e.target as Node)) {
        setTzMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [tzMenuOpen]);
  const TIMEZONE_OPTIONS: { value: string; label: string }[] = [
    { value: 'UTC', label: 'UTC' },
    { value: 'America/New_York', label: 'Eastern (ET)' },
    { value: 'America/Chicago', label: 'Central (CT)' },
    { value: 'America/Denver', label: 'Mountain (MT)' },
    { value: 'America/Los_Angeles', label: 'Pacific (PT)' },
    { value: 'Europe/London', label: 'London (GMT)' },
    { value: 'Europe/Berlin', label: 'Berlin (CET)' },
    { value: 'Asia/Dubai', label: 'UAE (GST)' },
    { value: 'Asia/Kolkata', label: 'India (IST)' },
    { value: 'Asia/Tokyo', label: 'Tokyo (JST)' },
    { value: 'Australia/Sydney', label: 'Sydney (AEST)' },
  ];
  // Dismissal state for the "Discover PROD" info card (DEV + Free only).
  // Persisted to localStorage so a user who closes it never sees it again
  // on this browser. Cleared only by clearing site data — intentional; a
  // soft reminder shouldn't need a re-opt-out every visit.
  const [prodCardDismissed, setProdCardDismissed] = useState<boolean>(() => {
    try { return localStorage.getItem('fpulse_prod_info_dismissed') === '1'; }
    catch { return false; }
  });
  const dismissProdCard = () => {
    try { localStorage.setItem('fpulse_prod_info_dismissed', '1'); } catch {}
    setProdCardDismissed(true);
  };

  // Tick the clock every 30s so the greeting time stays fresh without
  // forcing a refetch. A wall clock feels alive; staleness feels dead.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  const loadAll = useCallback(async () => {
    setLoading(true);
    // 2026-05-22 (audit J2 / L1) — fire the new summary endpoint in
    // parallel with the legacy chain. If the summary returns, the
    // KPIs read from it (with per-section status visibility); if it
    // fails entirely, we keep the legacy chain's last-known-good
    // values so the dashboard doesn't go blank.
    try {
      // L3 (2026-05-23) — pass project filter so the summary endpoint
      // narrows execution counts + KPI aggregates to that project.
      // 'all' (default) means workspace-wide.
      const summaryResp = await api.dashboardSummary({
        environment: environment === 'prod' ? 'prod' : 'dev',
        hours: trendHours,
        project_id: projectFilter !== 'all' ? projectFilter : undefined,
      } as any).catch(() => null);
      setSummary(summaryResp);
    } catch {
      setSummary(null);
    }

    try {
      const [
        pipelines, projects, connections, credentials, schedules, variables,
        storageSummary,
        execStats, recentExecs, activeScheds, failedPipes,
        pendingApprovals, poolStatus, sysMetrics,
        users, license,
      ] = await Promise.all([
        api.listWorkflows().catch(() => []),
        api.listProjects().catch(() => []),
        api.listConnections().catch(() => []),
        api.listCredentials().catch(() => []),
        api.listSchedules().catch(() => []),
        api.listVariables().catch(() => []),
        // 2026-05-25 — Storage rollup for the workspace inventory row.
        // Lightweight (counts + bytes, no row enumeration) and degrades
        // gracefully to nulls if the endpoint is unavailable.
        fetch('/api/storage/summary', {
          headers: {
            'X-Workspace-Id': localStorage.getItem('fpulse_workspace_id') || 'default',
            Authorization: `Bearer ${localStorage.getItem('fpulse_token') || ''}`,
          },
        }).then((r) => (r.ok ? r.json() : null)).catch(() => null),
        api.getMonitorStats(24).catch(() => ({ total: 0, success: 0, failed: 0, running: 0, success_rate: 0, avg_duration_ms: 0 })),
        api.listExecutions().catch(() => []),
        api.getActiveSchedules().catch(() => []),
        api.getFailedPipelines().catch(() => []),
        (api as any).listPendingProjects?.().catch(() => []) ?? Promise.resolve([]),
        (api as any).getPoolStatus?.().catch(() => null) ?? Promise.resolve(null),
        // Use /health/memory — returns rss_mb, uptime_seconds, threads,
        // host.cpu_count, host.total_memory_mb, db_files. No auth required.
        // The older getSystemMetrics() hit /system/metrics which is
        // auth-gated and returns 401 for unauthenticated calls, so the
        // System row was showing "—" everywhere. Switching to the open
        // snapshot endpoint makes the row actually populate.
        api.get('/api/health/memory').catch(() => null),
        // 2026-05-22 (audit N3) — admin-only data lazy-loaded. The
        // listUsers + license endpoints are admin-gated; non-admins
        // got 401/403 + empty array, which the dashboard then rendered
        // as "0 users / no license" — misleading. Now we only fire
        // these when both (a) the caller is an admin and (b) the
        // panel is going to render (PROD branch). Non-admins / DEV
        // skip the calls entirely.
        (isAdmin && environment === 'prod')
          ? ((api as any).listUsers?.().catch(() => []) ?? Promise.resolve([]))
          : Promise.resolve([]),
        (isAdmin && environment === 'prod')
          ? ((api as any).getLicenseStatus?.().catch(() => null) ?? Promise.resolve(null))
          : Promise.resolve(null),
      ]);

      // L3 — capture the projects list for the dashboard's filter
      // dropdown. Shape: { id, name }. Trim to keep the dropdown
      // readable; a workspace with >50 projects probably wants a
      // different UI anyway.
      if (Array.isArray(projects)) {
        setProjectsList(
          (projects as any[])
            .filter((p) => p && p.id && p.name)
            .map((p) => ({ id: String(p.id), name: String(p.name) }))
            .slice(0, 200),
        );
      }

      setStats({
        pipelines: Array.isArray(pipelines) ? pipelines.length : 0,
        projects: Array.isArray(projects) ? projects.length : 0,
        connections: Array.isArray(connections) ? connections.length : 0,
        credentials: Array.isArray(credentials) ? credentials.length : 0,
        schedules: Array.isArray(schedules) ? schedules.length : 0,
        variables: Array.isArray(variables) ? variables.length : 0,
        storage: storageSummary as any,
        executions: execStats as any,
        // P0 Day 3 polish (2026-05-23): the previous 30-entry slice was
        // tuned for the 24h bucket chart + "Recent runs" 6-row table.
        // With the trend toggle, 7d / 30d need more history to bucket
        // accurately. 500 is a safe upper bound (we still slice to 6
        // for the Recent table). The /executions endpoint already paginates
        // server-side; raise the request limit if it returns < 500.
        recentExecutions: Array.isArray(recentExecs) ? recentExecs.slice(0, 500) : [],
        activeSchedules: Array.isArray(activeScheds) ? activeScheds.slice(0, 6) : [],
        failedPipelines: Array.isArray(failedPipes) ? failedPipes.slice(0, 6) : [],
        pendingApprovals: Array.isArray(pendingApprovals) ? pendingApprovals.slice(0, 6) : [],
        pool: poolStatus || null,
        system: sysMetrics || null,
        license: license || null,
        users: Array.isArray(users) ? users : [],
      });
      setLastRefresh(formatTimeShort());
    } catch {
      setStats(null);
    }
    setLoading(false);
    // 2026-05-22 — deps include environment + trendHours + isAdmin
    // so the trend-period toggle, DEV/PROD switch, and admin-status
    // change (role refresh, sign-in) all trigger a re-fetch with the
    // right gating applied.
    // L3 (2026-05-23) — project filter joins the dep array; flipping
    // the selector re-fetches with project_id scoped on the summary.
  }, [environment, trendHours, isAdmin, projectFilter]);

  useEffect(() => { loadAll(); }, [loadAll]);

  // ── Trend buckets + sparklines (window-aware) ────────────────────────
  //
  // P0 Day 3 polish (2026-05-23): bucket count + granularity tracks the
  // trendHours toggle. Previously hardcoded to 24 hourly bins, so 7d/30d
  // visually stayed at 24h even though the KPI numbers refreshed.
  //
  //   24h   → 24 hourly bins, labels "HH:00"
  //   7d    → 7 daily bins,   labels short weekday (Mon, Tue, ...)
  //   30d   → 30 daily bins,  labels short date  (May 17, May 18, ...)
  //
  // Daily bins use local-midnight boundaries so a run at 23:59 stays in
  // its own day, not the next one.
  const { buckets, maxCount, spark } = useMemo(() => {
    const nowMs = Date.now();
    const isHourly = trendHours === 24;
    const binCount = isHourly ? 24 : trendHours === 168 ? 7 : 30;
    const binMs = isHourly ? 3600_000 : 86_400_000;

    // Day-bin anchor — local midnight of "today" — so binIdx maps to a
    // calendar day rather than a sliding 24h slice. Hour-bin path keeps
    // the existing sliding-window semantics (rolling 24h to now).
    const startOfToday = new Date();
    startOfToday.setHours(0, 0, 0, 0);
    const dayAnchorMs = startOfToday.getTime();

    type Bucket = { label: string; success: number; failed: number; running: number };
    const buckets: Bucket[] = [];
    for (let i = binCount - 1; i >= 0; i--) {
      let label: string;
      if (isHourly) {
        const end = nowMs - i * binMs;
        label = `${new Date(end).getHours()}:00`;
      } else {
        const d = new Date(dayAnchorMs - i * binMs);
        // Weekday for 7d (short, readable), MM/DD for 30d (compact).
        label = trendHours === 168
          ? d.toLocaleDateString(undefined, { weekday: 'short' })
          : `${d.getMonth() + 1}/${d.getDate()}`;
      }
      buckets.push({ label, success: 0, failed: 0, running: 0 });
    }

    const recent = stats?.recentExecutions || [];
    recent.forEach((e: any) => {
      const t = e.started_at || e.created_at || e.start_time;
      if (!t) return;
      const ts = new Date(t).getTime();
      let binIdx: number;
      if (isHourly) {
        binIdx = (binCount - 1) - Math.floor((nowMs - ts) / binMs);
      } else {
        // Day index from today (0 = today, 1 = yesterday, ...)
        const daysAgo = Math.floor((dayAnchorMs - new Date(ts).setHours(0, 0, 0, 0)) / binMs);
        binIdx = (binCount - 1) - daysAgo;
      }
      if (binIdx < 0 || binIdx >= binCount) return;
      // 2026-05-22 (audit J3 / K1): canonical status normalization.
      // The backend has fpulse.monitoring.status.normalize_status as
      // the source of truth; the frontend mirrors the failure aliases
      // here so the bucket counts agree with /monitor/stats and the
      // Executions page. New status writers should be added to the
      // backend module and reflected here.
      const s = (e.status || '').toLowerCase();
      if (s === 'success' || s === 'ok' || s === 'completed' || s === 'passed') {
        buckets[binIdx].success += 1;
      } else if (s === 'error' || s === 'failed' || s === 'failure' || s === 'timeout' || s === 'timed_out') {
        buckets[binIdx].failed += 1;
      } else if (s === 'running' || s === 'in_progress' || s === 'executing') {
        buckets[binIdx].running += 1;
      }
    });
    const maxCount = Math.max(1, ...buckets.map(b => b.success + b.failed + b.running));
    const spark = {
      runs: buckets.map(b => b.success + b.failed + b.running),
      success: buckets.map(b => b.success),
      failed: buckets.map(b => b.failed),
      slaPct: buckets.map(b => {
        const tot = b.success + b.failed;
        return tot === 0 ? 100 : Math.round((b.success / tot) * 100);
      }),
    };
    return { buckets, maxCount, spark };
  }, [stats?.recentExecutions, trendHours]);

  // 2026-05-22 (audit O2) — prefer the new /api/dashboard/summary
  // endpoint's data for KPIs that have a trend-window dependency
  // (failures / runs / success-rate / avg duration). The previous code
  // read these from the legacy `stats.executions` (a hardcoded 24h
  // `getMonitorStats(24)` fetch), so the trend toggle would re-fetch
  // the summary endpoint but the visible KPI cards never updated —
  // the chart label said "last 7d" while the numbers were still 24h.
  // Now: when the summary's executions section loaded, use that; fall
  // back to legacy stats only if the section failed.
  const summaryExecOk = summary?.executions?.status === 'loaded';
  const summaryExec = summaryExecOk ? summary.executions.data : null;
  const legacyExec = stats?.executions;
  const exec = summaryExec || legacyExec;
  const running = exec?.running || 0;
  // Variable names retained ("24h") to keep the diff small — the values
  // actually reflect the user's selected trend window (24h / 7d / 30d)
  // because the summary endpoint reads `hours: trendHours`. The labels
  // below use `windowLabel` so the UI text tracks the toggle.
  const failures24h = exec?.failed || 0;
  const runs24h = exec?.total || 0;
  // P0 Day 3 polish (2026-05-23): user reported that 24h/7d/30d toggle
  // refetched data but every visible label still said "24h". `windowLabel`
  // resolves the selected window to its short / long form once and gets
  // dropped into every KPI label, chart title, welcome line, and trend
  // copy. Single source of truth for the toggle-aware text.
  const windowLabel = trendHours === 24 ? '24h' : trendHours === 168 ? '7d' : '30d';
  const windowLong = trendHours === 24 ? 'last 24 hours' : trendHours === 168 ? 'last 7 days' : 'last 30 days';
  // 2026-05-28 — headline Success Rate is SCHEDULED runs only (or
  // webhook / replay — the "production" triggers). Manual test runs
  // the user fires while iterating on a pipeline must NOT drag the
  // operational health KPI down. Falls back to the legacy all-runs
  // success_rate when the backend hasn't shipped the new sub-dict
  // (e.g. running against an older OSS install).
  const scheduledExec = (exec as any)?.scheduled;
  const scheduledRuns24h = scheduledExec?.total ?? 0;
  const scheduledRate = scheduledExec
    ? Math.round(scheduledExec.success_rate || 0)
    : Math.round(exec?.success_rate || 0);
  // hasRuns drives the "—" empty state. We base it on SCHEDULED runs
  // when the sub-dict is present (the headline KPI is now scheduled-
  // only, so empty-state should match). Falls back to all-runs count
  // for backward compat.
  const hasRuns = scheduledExec ? scheduledRuns24h > 0 : runs24h > 0;
  const successRate = hasRuns ? scheduledRate : 0;
  const successDisplay = hasRuns ? successRate : '—';
  const avgDur = exec?.avg_duration_ms || 0;
  const approvalsPending = stats?.pendingApprovals?.length || 0;
  // 2026-05-22 (audit O2): prefer the summary endpoint's top_failed
  // count for "Active incidents" so it follows the trend window.
  // Legacy `failedPipelines` is always 24h-scoped.
  const summaryFailedCount =
    summary?.top_failed?.status === 'loaded' && Array.isArray(summary.top_failed.data)
      ? summary.top_failed.data.length
      : null;
  const incidentsCount = summaryFailedCount ?? stats?.failedPipelines?.length ?? 0;
  // Pool + system also have summary equivalents.
  const summaryPoolOk = summary?.pool?.status === 'loaded';
  const pool = (summaryPoolOk ? summary.pool.data : stats?.pool) || {};
  const poolUtil = Math.round(pool.utilization_pct ?? 0);
  const queueDepth = pool.queue_depth ?? 0;
  const throughput = Math.round(pool.throughput_per_hour ?? 0);
  const cpuPct = Math.round(pool.cpu_percent ?? 0);
  const summarySysOk = summary?.system?.status === 'loaded';
  const sys = (summarySysOk ? summary.system.data : stats?.system) || {};
  const rssMb = sys.rss_mb || 0;
  const totalMemMb = sys.host?.total_memory_mb || 0;
  const memPct = totalMemMb > 0 ? Math.round((rssMb / totalMemMb) * 100) : 0;
  const threadCount = sys.threads || 0;
  const uptimeSec = sys.uptime_seconds || 0;
  const dbBytes = (sys.db_files || []).reduce((a, f) => a + (f.size_bytes || 0), 0);

  // Headline — one sentence, different shape per env.
  const headline = (() => {
    if (!stats) return { text: 'Loading…', tone: 'ok' as const };
    // Pipelines whose most recent run failed — the SAME source the Needs
    // Attention card uses (`/api/monitor/failed`, deduped by pipeline over
    // recent runs, NOT the 24h window). The headline must reconcile with
    // that card: a workspace with failed pipelines is not "healthy" just
    // because the last 24h happened to be quiet. 2026-06-17.
    const attentionFailedCount = visibleFailedPipelines.length;
    if (isProd) {
      if (incidentsCount > 0) return { text: `${incidentsCount} incident${incidentsCount === 1 ? '' : 's'} need attention`, tone: 'bad' as const };
      if (approvalsPending > 0) return { text: `${approvalsPending} approval${approvalsPending === 1 ? '' : 's'} awaiting review`, tone: 'warn' as const };
      return { text: 'Production is healthy', tone: 'ok' as const };
    }
    if (failures24h > 0) return { text: `${failures24h} failure${failures24h === 1 ? '' : 's'} in the ${windowLong}`, tone: 'bad' as const };
    if (attentionFailedCount > 0) return { text: `${attentionFailedCount} pipeline${attentionFailedCount === 1 ? '' : 's'} need${attentionFailedCount === 1 ? 's' : ''} attention`, tone: 'warn' as const };
    if (running > 0) return { text: `${running} pipeline${running === 1 ? '' : 's'} running now`, tone: 'warn' as const };
    if (!hasRuns) return { text: 'Workspace is quiet — nothing running', tone: 'muted' as const };
    return { text: 'All systems healthy', tone: 'ok' as const };
  })();
  const toneClass = headline.tone === 'bad' ? p.bad : headline.tone === 'warn' ? p.warn : headline.tone === 'muted' ? p.inkMuted : p.ok;
  const toneDot = headline.tone === 'bad' ? p.badBg : headline.tone === 'warn' ? p.warnBg : headline.tone === 'muted' ? 'bg-slate-300' : p.okBg;

  const heroCTA: { label: string; onClick: () => void } = (() => {
    if (isProd) return isAdmin
      ? (approvalsPending > 0
          ? { label: `Review ${approvalsPending} Approval${approvalsPending === 1 ? '' : 's'}`, onClick: () => onNavigate('approvals') }
          : { label: 'View Deployments', onClick: () => onNavigate('pipelines') })
      : { label: 'My Deployments', onClick: () => onNavigate('pipelines') };
    return isAdmin
      ? { label: 'Team Activity', onClick: () => onNavigate('admin') }
      : { label: 'New Pipeline', onClick: () => onNavigate('templates') };
  })();

  const displayName = userName || user?.name || user?.email?.split('@')[0] || (isProd ? 'Operator' : 'there');

  return (
    <div className={`min-h-full overflow-y-auto ${p.canvas}`}>
      {/* ── Page header (sticky) — canonical shared PageHeader shell ── */}
      <PageHeader
        environment={environment}
        icon={
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className={isProd ? 'text-red-400' : 'text-blue-500'}>
            <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>
          </svg>
        }
        title={isProd ? 'Production' : 'Development'}
        titleAccessory={
          <>
            {/* Env pill is rendered by TierChip so every page header shows
                the same Dev / Live + Free / Plus pair. */}
            {isAdmin && (
              <span className={`text-xs font-bold px-2.5 py-0.5 rounded-full uppercase tracking-wider ${
                isProd ? 'bg-slate-700 text-slate-200 border border-slate-600' : 'bg-violet-100 text-violet-700 border border-violet-200'
              }`}>Admin</span>
            )}
            <TierChip tier={tier} environment={environment} />
          </>
        }
        subtitle={`Last refresh: ${lastRefresh}`}
        actions={
          /* Z35 (2026-05-23) — page-level controls cluster.
              Project picker + trend-window toggle were previously inside
              the "Activity · last 24h" section subheader, but they scope
              the entire page via loadAll's useEffect deps (trendHours +
              projectFilter), not just the Activity card row. Hoisted to
              the header next to Refresh so all page-level controls live
              together — same pattern as the other list pages.
              Z37 (2026-05-23): icon-prefixed pills + brand-accent active
              state so they read as deliberate dashboard chrome instead of
              stock form controls. Folder icon on the project pill, clock
              on the trend toggle; active time button switches to indigo
              (DEV) or slate-700 (PROD) instead of generic white. */
          <div className="flex items-center gap-2 flex-wrap justify-end">
            {projectsList.length > 0 && (
              <label
                className={`inline-flex items-center gap-1.5 pl-2.5 pr-1 py-1.5 rounded-md border cursor-pointer transition-colors ${
                  isProd
                    ? 'bg-slate-800 border-slate-700 text-slate-200 hover:bg-slate-700'
                    : 'bg-white border-slate-300 text-slate-700 hover:bg-slate-50'
                }`}
              >
                <svg
                  width="13"
                  height="13"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className={isProd ? 'text-slate-400' : 'text-slate-500'}
                  aria-hidden="true"
                >
                  <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
                </svg>
                <select
                  value={projectFilter}
                  onChange={(e) => setProjectFilter(e.target.value)}
                  aria-label="Filter dashboard by project"
                  className={`text-xs font-semibold bg-transparent cursor-pointer focus:outline-none pr-1 ${
                    isProd ? 'text-slate-200' : 'text-slate-700'
                  }`}
                >
                  <option value="all">All projects</option>
                  {projectsList.map((proj) => (
                    <option key={proj.id} value={proj.id}>
                      {proj.name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <div
              role="group"
              aria-label="Trend period"
              className={`inline-flex items-center gap-0.5 rounded-md p-0.5 pl-2 ${isProd ? 'bg-slate-800' : 'bg-slate-100'}`}
            >
              <svg
                width="13"
                height="13"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                className={`mr-1 ${isProd ? 'text-slate-400' : 'text-slate-500'}`}
                aria-hidden="true"
              >
                <circle cx="12" cy="12" r="10" />
                <polyline points="12 6 12 12 16 14" />
              </svg>
              {([24, 168, 720] as const).map((h) => (
                <button
                  key={h}
                  type="button"
                  onClick={() => setTrendHours(h)}
                  aria-pressed={trendHours === h}
                  aria-label={`Show ${h === 24 ? 'last 24 hours' : h === 168 ? 'last 7 days' : 'last 30 days'}`}
                  className={`px-2.5 py-1 text-xs font-semibold rounded transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 ${
                    trendHours === h
                      ? isProd
                        ? 'bg-slate-700 text-white shadow-sm'
                        : 'bg-indigo-600 text-white shadow-sm'
                      : isProd
                        ? 'text-slate-400 hover:text-slate-200'
                        : 'text-slate-500 hover:text-slate-700'
                  }`}
                >
                  {h === 24 ? '24h' : h === 168 ? '7d' : '30d'}
                </button>
              ))}
            </div>
            <div className={`w-px h-6 ${isProd ? 'bg-slate-700' : 'bg-slate-300/60'}`} />
            <button onClick={loadAll} disabled={loading}
              className={`px-4 py-2 text-sm font-semibold rounded-lg border transition-all ${
                isProd
                  ? 'text-slate-200 bg-slate-800 hover:bg-slate-700 border-slate-700'
                  : 'text-slate-700 bg-white hover:bg-slate-50 border-slate-300'
              } disabled:opacity-50`}
            >{loading ? 'Refreshing…' : 'Refresh'}</button>
          </div>
        }
      />

      <div className="w-full max-w-[1500px] mx-auto px-6 py-5 space-y-4">

        {/* ── 1. GREETING — compact welcome; date, time, status, CTA ──
            Both DEV and PROD now use light greeting cards — readable
            against the canvas and friendly. Env is conveyed by accent
            colour (blue for DEV, red for PROD) on the user's name and
            the status dot, not by a dark card wrapper. */}
        <section className={`rounded-lg border shadow-sm overflow-hidden ${p.greetingBg}`}>
          <div className="px-5 py-3.5 flex items-center justify-between gap-4 flex-wrap">
            <div className="min-w-0">
              <div className="text-xl font-bold leading-tight text-slate-800">
                {getGreeting()}, <span className={isProd ? 'text-red-600' : 'text-blue-600'}>{displayName}</span>
              </div>
              <div className="mt-0.5 text-sm font-medium text-slate-600">
                {formatDateLong(userTz)}
                <span className="text-slate-400 mx-1.5">·</span>
                <span className="tabular-nums">{formatClock(userTz)}</span>
                <span ref={tzMenuRef} className="relative inline-block ml-1.5">
                  <button
                    type="button"
                    onClick={() => setTzMenuOpen((o) => !o)}
                    className="inline-flex items-center gap-0.5 text-xs font-semibold text-slate-500 hover:text-slate-700 transition-colors"
                    title="Click to change dashboard timezone"
                  >
                    {formatTimezoneAbbr(userTz)}
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="opacity-60">
                      <polyline points="6 9 12 15 18 9" />
                    </svg>
                  </button>
                  {tzMenuOpen && (() => {
                    // Use fixed positioning so the popover escapes any
                    // ancestor with `overflow: hidden` (the welcome card).
                    const pill = tzMenuRef.current?.querySelector('button');
                    const r = pill?.getBoundingClientRect();
                    return (
                      <div
                        className="fixed z-50 w-48 bg-white border border-slate-200 rounded-lg shadow-lg py-1 max-h-72 overflow-auto"
                        style={r ? { top: r.bottom + 4, left: r.left } : undefined}
                      >
                        {TIMEZONE_OPTIONS.map((tz) => (
                          <button
                            key={tz.value}
                            type="button"
                            onClick={() => handleTzChange(tz.value)}
                            className={`w-full text-left px-3 py-1.5 text-xs font-medium transition-colors ${
                              userTz === tz.value
                                ? 'bg-pipe-50 text-pipe-700'
                                : 'text-slate-700 hover:bg-slate-50'
                            }`}
                          >
                            {tz.label}
                          </button>
                        ))}
                      </div>
                    );
                  })()}
                </span>
                <span className="text-slate-400 mx-1.5">·</span>
                <span className="inline-flex items-center gap-1.5">
                  <span className={`inline-block w-2 h-2 rounded-full ${toneDot} ${headline.tone === 'ok' ? 'animate-pulse' : ''}`} />
                  <span className={toneClass}>{headline.text}</span>
                </span>
                {tier === 'plus' && <span className="ml-2 text-xs font-bold px-1.5 py-0.5 rounded-full bg-violet-100 text-violet-700 border border-violet-200 uppercase">Plus</span>}
              </div>
            </div>
            <button onClick={heroCTA.onClick} className={`px-4 py-2 text-sm font-bold text-white rounded-lg shadow-sm transition-colors ${p.accentBg} shrink-0`}>
              {heroCTA.label}
            </button>
          </div>
        </section>

        {/* ── NEEDS ATTENTION — auto-shows on real signals only ─────────
            Renders ONLY when there is something actionable: recent
            failures, pool at capacity, or pending PROD approvals (admin).
            Hidden entirely when the workspace is clean. Collapsible —
            once triaged, the user can fold it shut. Capped at 3 failure
            rows (full list lives on Executions filtered by status=failed).
            Updated 2026-05-25 final pass. */}
        {(() => {
          const failedListFull = visibleFailedPipelines;
          const failedList = failedListFull.slice(0, 3);
          const failedHiddenCount = Math.max(0, failedListFull.length - failedList.length);
          const showPoolWarning = poolUtil >= 85;
          const showApprovals = isProd && isAdmin && approvalsPending > 0;
          const show = failedList.length > 0 || showPoolWarning || showApprovals;
          if (!show) return null;
          return (
            <section className="rounded-lg border border-amber-200 bg-amber-50/40 shadow-sm overflow-hidden">
              <button
                type="button"
                onClick={toggleAttention}
                aria-expanded={attentionOpen}
                className="w-full px-5 py-3 flex items-center justify-between border-b border-amber-200/60 bg-amber-50 hover:bg-amber-100/60 transition-colors"
                title={attentionOpen ? 'Collapse Needs Attention' : 'Expand Needs Attention'}
              >
                <div className="flex items-center gap-2">
                  <svg
                    width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
                    className={`text-amber-700 transition-transform ${attentionOpen ? 'rotate-90' : ''}`}
                  >
                    <polyline points="9 18 15 12 9 6" />
                  </svg>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#d97706" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
                    <line x1="12" y1="9" x2="12" y2="13" />
                    <line x1="12" y1="17" x2="12.01" y2="17" />
                  </svg>
                  <h2 className="text-sm font-bold text-amber-900 uppercase tracking-wider">Needs Attention</h2>
                </div>
                <span className="text-xs text-amber-700">
                  {failedListFull.length > 0 && `${failedListFull.length} failure${failedListFull.length === 1 ? '' : 's'}`}
                  {failedListFull.length > 0 && (showPoolWarning || showApprovals) && ' · '}
                  {showPoolWarning && `Pool ${poolUtil}%`}
                  {showPoolWarning && showApprovals && ' · '}
                  {showApprovals && `${approvalsPending} approval${approvalsPending === 1 ? '' : 's'}`}
                </span>
              </button>
              {attentionOpen && (
              <div className="divide-y divide-amber-100">
                {showPoolWarning && (
                  <div className="px-5 py-2.5 flex items-center justify-between">
                    <div className="flex items-center gap-2.5 min-w-0">
                      <span className="inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-red-100 text-red-700 border border-red-200">Pool</span>
                      <span className="text-sm text-slate-700">Worker pool at <strong className="text-red-700">{poolUtil}%</strong> capacity — new runs will queue</span>
                    </div>
                    <button onClick={() => onNavigate('pool')} className="text-xs font-semibold text-amber-700 hover:text-amber-900 underline shrink-0 ml-3">
                      Open Pool →
                    </button>
                  </div>
                )}
                {showApprovals && (
                  <div className="px-5 py-2.5 flex items-center justify-between">
                    <div className="flex items-center gap-2.5 min-w-0">
                      <span className="inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-violet-100 text-violet-700 border border-violet-200">Approvals</span>
                      <span className="text-sm text-slate-700"><strong>{approvalsPending}</strong> deploy{approvalsPending === 1 ? '' : 's'} awaiting your review</span>
                    </div>
                    <button onClick={() => onNavigate('approvals' as any)} className="text-xs font-semibold text-violet-700 hover:text-violet-900 underline shrink-0 ml-3">
                      Review →
                    </button>
                  </div>
                )}
                {/* Column header — labels the Failures / Last failed columns
                    so the count and the relative time are self-explanatory.
                    Same fixed-width grid as the rows below so they line up. */}
                {failedList.length > 0 && (
                  <div className="px-5 pt-2 pb-1 grid grid-cols-[3.25rem_minmax(0,1fr)_4rem_5rem_4.75rem] items-center gap-x-2 text-[10px] font-bold uppercase tracking-wider text-amber-700/70">
                    <span aria-hidden />
                    <span>Pipeline</span>
                    <span className="text-right">Failures</span>
                    <span className="text-right">Last&nbsp;failed</span>
                    <span aria-hidden />
                  </div>
                )}
                {failedList.map((f: any) => {
                  const failedAt = f.finished_at || f.completed_at || f.ended_at || f.last_failed_at || f.started_at || f.created_at;
                  const fails = Number(f.failure_count) > 0 ? Number(f.failure_count) : 1;
                  const errText = f.error_message || f.last_error;
                  return (
                  <div key={f.id || f.workflow_id || f.name} className="px-5 py-2.5 grid grid-cols-[3.25rem_minmax(0,1fr)_4rem_5rem_4.75rem] items-center gap-x-2 hover:bg-amber-50/60 transition-colors">
                    {/* status */}
                    <span className="justify-self-start inline-flex items-center text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-red-100 text-red-700 border border-red-200">Failed</span>
                    {/* pipeline name + error (stacked so the right columns stay aligned) */}
                    <div className="min-w-0">
                      <button
                        onClick={() => openFailedRun(f)}
                        className="block max-w-full truncate text-sm font-medium text-slate-800 text-left hover:text-rose-700"
                        title={errText || 'Open the failed run — error + failed step'}
                      >
                        {f.name || f.workflow_name || f.workflow_id}
                      </button>
                      {errText && (
                        <span className="block truncate text-xs text-slate-500" title={errText}>{errText}</span>
                      )}
                    </div>
                    {/* failures — consecutive streak, as a rose count pill */}
                    <div className="text-right">
                      <span
                        className="inline-flex items-center justify-center min-w-[1.75rem] px-1.5 py-0.5 rounded-full text-xs font-bold bg-rose-100 text-rose-700 tabular-nums"
                        title={fails > 1 ? `Failed ${fails} runs in a row (most recent included)` : 'Failed on its most recent run'}
                      >
                        {fails > 1 ? `${fails}×` : '1'}
                      </span>
                    </div>
                    {/* last failed — relative, exact on hover */}
                    <div
                      className="text-right text-xs text-slate-600 tabular-nums"
                      title={failedAt ? `Last failed ${new Date(failedAt).toLocaleString()}` : undefined}
                    >
                      {failedAt ? formatTimeAgo(failedAt) : '—'}
                    </div>
                    {/* actions */}
                    <div className="flex items-center justify-end gap-2">
                      <button
                        onClick={() => openFailedRun(f)}
                        className="text-xs font-semibold text-rose-700 hover:text-rose-900 underline"
                      >
                        View →
                      </button>
                      {/* Clear (acknowledge) — hides this failure until the
                          pipeline fails again or recovers. */}
                      <button
                        onClick={() => dismissFailure(f)}
                        aria-label="Clear this failure"
                        title="Clear — acknowledge and hide. Reappears if it fails again; clears for good once it runs clean."
                        className="inline-flex items-center justify-center w-5 h-5 rounded text-slate-400 hover:text-slate-700 hover:bg-amber-100 transition-colors"
                      >
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                          <line x1="18" y1="6" x2="6" y2="18" />
                          <line x1="6" y1="6" x2="18" y2="18" />
                        </svg>
                      </button>
                    </div>
                  </div>
                  );
                })}
                {failedHiddenCount > 0 && (
                  <button
                    onClick={() => navigateWithFilter('executions', { status: 'failed' })}
                    className="w-full px-5 py-2 text-xs font-semibold text-amber-800 hover:text-amber-900 hover:bg-amber-50 transition-colors text-left"
                  >
                    + {failedHiddenCount} more failure{failedHiddenCount === 1 ? '' : 's'} — see all on Executions →
                  </button>
                )}
                {/* Clear all currently-shown failures at once. */}
                {failedList.length > 1 && (
                  <button
                    onClick={() => dismissFailures(failedListFull)}
                    title="Clear all shown failures. Each reappears if its pipeline fails again."
                    className="w-full px-5 py-2 text-xs font-semibold text-slate-500 hover:text-slate-700 hover:bg-amber-50 transition-colors text-left"
                  >
                    Clear all {failedListFull.length} failure{failedListFull.length === 1 ? '' : 's'}
                  </button>
                )}
              </div>
              )}
            </section>
          );
        })()}

        {/* ── NEW-USER EMPTY STATE — replaces hero KPIs when workspace
            is brand-new (no pipelines yet). Generic "Welcome back" is
            useless on day 0; show concrete next-step CTAs instead.
            Added 2026-05-25. */}
        {!loading && stats && (stats.pipelines === 0) && !isProd ? (
          <section className="rounded-lg border border-blue-200 bg-gradient-to-br from-blue-50 to-indigo-50 shadow-sm p-6">
            <h2 className="text-base font-bold text-slate-800 mb-1">Get F-Pulse moving in 3 steps</h2>
            <p className="text-sm text-slate-600 mb-4">No pipelines yet. Pick the entry point that matches what you have in hand.</p>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <button onClick={() => onNavigate('editor')} className="text-left bg-white border border-slate-200 rounded-lg p-4 hover:border-blue-400 hover:shadow-sm transition-all">
                <div className="w-9 h-9 rounded-lg bg-blue-100 text-blue-700 flex items-center justify-center mb-2">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" /></svg>
                </div>
                <div className="text-sm font-bold text-slate-800">Build a pipeline</div>
                <div className="text-xs text-slate-500 mt-0.5">Drag nodes onto the canvas — or ask the Copilot in plain English.</div>
              </button>
              <button onClick={() => onNavigate('storage')} className="text-left bg-white border border-slate-200 rounded-lg p-4 hover:border-blue-400 hover:shadow-sm transition-all">
                <div className="w-9 h-9 rounded-lg bg-emerald-100 text-emerald-700 flex items-center justify-center mb-2">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v14a9 3 0 0 0 18 0V5" /><path d="M3 12a9 3 0 0 0 18 0" /></svg>
                </div>
                <div className="text-sm font-bold text-slate-800">Upload data</div>
                <div className="text-xs text-slate-500 mt-0.5">CSV, JSON, Parquet, Excel — promote to a managed table later.</div>
              </button>
              <button onClick={() => onNavigate('connections')} className="text-left bg-white border border-slate-200 rounded-lg p-4 hover:border-blue-400 hover:shadow-sm transition-all">
                <div className="w-9 h-9 rounded-lg bg-cyan-100 text-cyan-700 flex items-center justify-center mb-2">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" /><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" /></svg>
                </div>
                <div className="text-sm font-bold text-slate-800">Connect a source</div>
                <div className="text-xs text-slate-500 mt-0.5">Database, REST API, S3, or any of 33 connectors.</div>
              </button>
            </div>
          </section>
        ) : null}

        {isProd ? (
          /* ════════════════ PROD DASHBOARD ════════════════ */
          <>
            {/* ── 2. HERO KPIs — SLA / Incidents / Approvals / Uptime ── */}
            <section>
            <div className={`text-xs font-bold uppercase tracking-wider mb-2 ${p.inkMuted}`}>Performance · {windowLong}</div>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              <HeroKPI
                label={`SLA (${windowLabel})`}
                value={successDisplay}
                suffix={hasRuns ? '%' : ''}
                onClick={() => onNavigate('executions')}
                gradient={!hasRuns ? 'from-slate-500 to-slate-600' : successRate >= 95 ? 'from-emerald-500 to-emerald-600' : successRate >= 80 ? 'from-amber-500 to-orange-500' : 'from-red-500 to-rose-500'}
                sparkline={hasRuns ? spark.slaPct : undefined}
                trend={!hasRuns
                  ? { arrow: '—', text: `No runs in ${windowLong}`, tone: 'muted' }
                  : successRate >= 95 ? { arrow: '▲', text: 'Meeting target', tone: 'ok' }
                  : successRate >= 80 ? { arrow: '—', text: 'Below target', tone: 'muted' }
                  : { arrow: '▼', text: 'Breach', tone: 'bad' }}
              />
              <HeroKPI
                label="Active incidents"
                value={incidentsCount}
                onClick={() => navigateWithFilter('executions', { status: 'failed', hours: trendHours })}
                gradient={
                  incidentsCount > 0
                    ? 'from-red-500 to-rose-500'
                    : hasRuns
                      ? 'from-emerald-500 to-emerald-600'
                      : 'from-slate-500 to-slate-600'
                }
                trend={incidentsCount === 0
                  ? (hasRuns ? { arrow: '—', text: 'All clear', tone: 'ok' } : { arrow: '—', text: 'No activity', tone: 'muted' })
                  : { arrow: '▲', text: 'Review below', tone: 'bad' }}
              />
              <HeroKPI
                label="Approvals pending"
                value={approvalsPending}
                // P0 Day 2 removed the standalone 'approvals' page —
                // pending approvals surface inside the Pipelines page's
                // PROD lifecycle column. Route there instead.
                onClick={() => onNavigate('pipelines')}
                gradient={approvalsPending === 0 ? 'from-slate-500 to-slate-600' : 'from-amber-500 to-orange-500'}
                trend={approvalsPending === 0
                  ? { arrow: '—', text: 'Nothing queued', tone: 'muted' }
                  : { arrow: '▲', text: 'Awaiting review', tone: 'bad' }}
              />
              <HeroKPI
                label="Throughput"
                value={throughput}
                suffix="/hr"
                onClick={() => onNavigate('pool')}
                gradient={throughput === 0 ? 'from-blue-800 to-indigo-900' : 'from-blue-500 to-indigo-500'}
                trend={throughput === 0 ? { arrow: '—', text: 'Idle queue', tone: 'muted' } : { arrow: '▲', text: 'Runs per hour', tone: 'ok' }}
              />
              {/* PROD hero row keeps full-saturation gradients — PROD is the
                  serious, operational view; heavier colour matches that tone. */}
            </div>
            </section>

            {/* ── 3. OPERATIONS — 6 flat cards ── */}
            <section>
            <div className={`text-xs font-bold uppercase tracking-wider mb-2 ${p.inkMuted}`}>Operations</div>
            {/* 2026-05-25 — status tiles (Running / Queue / Pool util)
                keep gradient because color carries meaning; pure-inventory
                tiles (Deployed / Schedules / Connections) switch to
                striped so the row reads sharper-quieter. */}
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
              <FlatKPI variant="striped" label="Deployed"   icon={kpiIcons.pipelines} value={stats?.pipelines ?? 0} accent="blue"    onClick={() => onNavigate('pipelines')}   hint="pipelines live" />
              <FlatKPI label="Running now" icon={kpiIcons.throughput} value={running}              accent={running > 0 ? 'cyan' : 'slate'}    onClick={() => onNavigate('executions')} hint="executions in flight" />
              <FlatKPI label="Queue depth" icon={kpiIcons.schedule} value={queueDepth}             accent={queueDepth > 0 ? 'amber' : 'slate'}   onClick={() => onNavigate('pool')}        hint="waiting to start" />
              <FlatKPI label="Pool util"  icon={kpiIcons.cpu} value={poolUtil} suffix="%"          accent={poolUtil > 85 ? 'red' : poolUtil > 60 ? 'amber' : 'emerald'} onClick={() => onNavigate('pool')} hint={pool.busy_workers != null ? `${pool.busy_workers}/${pool.total_workers} workers` : 'workers'} />
              <FlatKPI variant="striped" label="Schedules"  icon={kpiIcons.schedule} value={stats?.schedules ?? 0}   accent="violet"  onClick={() => onNavigate('pipelines')}   hint={`${stats?.activeSchedules.length || 0} active`} />
              <FlatKPI variant="striped" label="Connections" icon={kpiIcons.connection} value={stats?.connections ?? 0} accent="slate" onClick={() => onNavigate('connections')} hint="saved endpoints" />
            </div>
            </section>
          </>
        ) : (
          /* ════════════════ DEV DASHBOARD ════════════════ */
          <>
            {/* ── 2. HERO KPIs — Success / Runs / Failures / Avg Duration ── */}
            <section>
            {/* Z35 (2026-05-23) — section subheader is now informational
                only. The project picker + 24h/7d/30d toggle moved to the
                page header (next to Refresh) so all page-level controls
                live together. The label below reflects whatever window is
                currently selected up there. */}
            <div className="flex items-center justify-between mb-2">
              <div className={`text-xs font-bold uppercase tracking-wider ${p.inkMuted}`}>
                Activity · last {trendHours === 24 ? '24h' : trendHours === 168 ? '7d' : '30d'}
              </div>
            </div>
            {/* P0 Day 3 (2026-05-23) — Runtime dependency health banner.
                Polls /api/system/dependencies and surfaces DuckDB / disk /
                local-LLM gaps so users see WHY downstream features fail.
                Renders nothing when everything is healthy. */}
            <RuntimeDepsBanner />

            {/* Section-failed badge — surfaces if any sub-section of the
                dashboard summary couldn't be loaded so the user doesn't
                read a zero as truth (audit K1). */}
            {summary && (() => {
              const failed: string[] = [];
              for (const key of ['inventory', 'executions', 'pool', 'system'] as const) {
                if (summary?.[key]?.status === 'failed') failed.push(key);
              }
              if (failed.length === 0) return null;
              return (
                <div className="mb-3 inline-flex items-center gap-2 px-3 py-1.5 rounded-md bg-amber-50 border border-amber-200 text-xs font-semibold text-amber-700">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <path d="M12 9v4M12 17h.01M10.29 3.86l-8.18 14.13a2 2 0 0 0 1.71 3.01h16.36a2 2 0 0 0 1.71-3.01L13.71 3.86a2 2 0 0 0-3.42 0z" />
                  </svg>
                  Some sections couldn't be loaded: {failed.join(', ')}. Numbers below may be incomplete.
                </div>
              );
            })()}
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              {/* DEV hero row — `lighter` shifts every shade one step
                  lighter (500→400 etc.) so DEV reads as softer / building
                  while PROD stays full saturation for the operational view. */}
              <HeroKPI
                lighter
                label="Success rate · scheduled"
                value={successDisplay}
                suffix={hasRuns ? '%' : ''}
                onClick={() => onNavigate('executions')}
                gradient={!hasRuns ? 'from-emerald-800 to-emerald-900' : successRate >= 95 ? 'from-emerald-500 to-emerald-600' : successRate >= 80 ? 'from-amber-500 to-orange-500' : 'from-red-500 to-rose-500'}
                sparkline={hasRuns ? spark.success : undefined}
                trend={(() => {
                  // 2026-05-28 — when there are no scheduled runs but
                  // the user has run pipelines manually (testing /
                  // iterating), surface the all-runs rate in the
                  // trend slot so the card is informative rather than
                  // a bare "Run a pipeline first" on a busy install.
                  const allRunsCount = runs24h;
                  const allRunsRate = Math.round(exec?.success_rate || 0);
                  if (!hasRuns) {
                    return allRunsCount > 0
                      ? { arrow: '—' as const, text: `No scheduled runs · all runs: ${allRunsRate}%`, tone: 'muted' as const }
                      : { arrow: '—' as const, text: 'Run a pipeline first', tone: 'muted' as const };
                  }
                  if (successRate >= 95) return { arrow: '▲' as const, text: 'Healthy', tone: 'ok' as const };
                  if (successRate >= 80) return { arrow: '—' as const, text: 'Degraded', tone: 'muted' as const };
                  return { arrow: '▼' as const, text: 'Fix failures', tone: 'bad' as const };
                })()}
              />
              <HeroKPI
                lighter
                label={`Runs (${windowLabel})`}
                value={runs24h}
                onClick={() => navigateWithFilter('executions', { hours: trendHours })}
                gradient={runs24h === 0 ? 'from-blue-800 to-indigo-900' : 'from-blue-500 to-indigo-500'}
                sparkline={hasRuns ? spark.runs : undefined}
                trend={running > 0
                  ? { arrow: '▲', text: `${running} running now`, tone: 'ok' }
                  : runs24h === 0 ? { arrow: '—', text: `No activity in ${windowLabel}`, tone: 'muted' }
                  : { arrow: '—', text: 'Idle', tone: 'muted' }}
              />
              <HeroKPI
                lighter
                label={`Failures (${windowLabel})`}
                value={failures24h}
                onClick={() => navigateWithFilter('executions', { status: 'failed', hours: trendHours })}
                // 2026-05-25 — color semantics tightened:
                //   failures>0 → red (alarm)
                //   hasRuns + 0 failures → emerald (good news)
                //   no runs at all → very light slate so the tile reads
                //     "neutral / nothing to report" instead of "heavy grey"
                gradient={
                  failures24h > 0
                    ? 'from-red-500 to-rose-500'
                    : hasRuns
                      ? 'from-emerald-500 to-emerald-600'
                      : 'from-slate-200 to-slate-300'
                }
                sparkline={failures24h > 0 ? spark.failed : undefined}
                trend={failures24h === 0
                  ? (hasRuns ? { arrow: '—', text: 'Nothing broken', tone: 'ok' } : { arrow: '—', text: 'No activity', tone: 'muted' })
                  : { arrow: '▲', text: `${incidentsCount} pipelines affected`, tone: 'bad' }}
              />
              <HeroKPI
                lighter
                label="Avg duration"
                value={avgDur > 0 ? formatDuration(avgDur) : '—'}
                onClick={() => onNavigate('executions')}
                gradient={avgDur > 0 ? 'from-violet-500 to-purple-500' : 'from-violet-800 to-purple-900'}
                trend={avgDur > 0 ? { arrow: '—', text: 'per successful run', tone: 'muted' } : { arrow: '—', text: 'No runs to average', tone: 'muted' }}
              />
            </div>
            </section>

            {/* ── INVENTORY STRIPS — always visible, compact ──────────
                Replaces the 16-tile card grid (2026-05-25 final).
                Three side-by-side cards: Workspace / Storage / System.
                Keeps the product BREADTH visible without burying it
                behind a fold OR wallpapering the page with cards. Each
                metric is clickable → jumps to its native page.

                2026-05-26 — Option A polish: dropped the outer
                slate-100/45 wrapper + bg-slate-50/90 header bar. The
                section title now sits as plain text above the three
                cards on the page background. Reasoning: the wrapper
                added a third stacked background layer (page → wrapper
                → card) that read as "nested heavy" without conveying
                hierarchy. Cleaner with the title floating above. */}
            <section>
              <div className="text-xs font-bold uppercase tracking-wider mb-2 text-slate-700">
                Workspace Overview
                <span className="ml-2 text-[11px] font-medium text-slate-500 normal-case tracking-normal">
                  · Assets, storage, and runtime at a glance
                </span>
              </div>
              <div className="grid gap-3 lg:grid-cols-3">
              <MetricStrip
                accent="blue"
                icon={kpiIcons.building}
                label="Workspace"
                metrics={[
                  { label: 'Pipelines',   value: stats?.pipelines ?? 0,   onClick: () => onNavigate('pipelines') },
                  { label: 'Projects',    value: stats?.projects ?? 0,    onClick: () => onNavigate('projects') },
                  { label: 'Connections', value: stats?.connections ?? 0, onClick: () => onNavigate('connections') },
                  { label: 'Credentials', value: stats?.credentials ?? 0, onClick: () => onNavigate('credentials') },
                  { label: 'Schedules',   value: stats?.schedules ?? 0,   onClick: () => onNavigate('pipelines') },
                  { label: 'Variables',   value: stats?.variables ?? 0,   onClick: () => onNavigate('settings') },
                ]}
              />
              <MetricStrip
                accent="emerald"
                icon={kpiIcons.db}
                label="Storage"
                metrics={[
                  { label: 'Files',    value: stats?.storage?.file_count ?? 0,   suffix: stats?.storage?.file_size_bytes ? formatBytes(stats.storage.file_size_bytes) : undefined,   onClick: () => onNavigate('storage') },
                  { label: 'Tables',   value: stats?.storage?.table_count ?? 0,  suffix: stats?.storage?.table_size_bytes ? formatBytes(stats.storage.table_size_bytes) : undefined,  onClick: () => onNavigate('storage') },
                  { label: 'Outputs',  value: stats?.storage?.output_count ?? 0, suffix: stats?.storage?.output_size_bytes ? formatBytes(stats.storage.output_size_bytes) : undefined, onClick: () => onNavigate('storage') },
                  // Trash is actionable when >0 (soft-deleted items
                  // taking disk space) → amber. Zero = muted.
                  { label: 'Trash', value: stats?.storage?.trash_count ?? 0, onClick: () => onNavigate('storage'),
                    alert: (stats?.storage?.trash_count ?? 0) > 0 ? 'amber' : false,
                    muted: (stats?.storage?.trash_count ?? 0) === 0 },
                ]}
              />
              <MetricStrip
                accent="violet"
                icon={kpiIcons.cpu}
                label="System"
                metrics={[
                  // Memory / Threads / DB size / Uptime always render —
                  // they're the system identity even when `—`. CPU and
                  // Throughput are hidden when not measured yet so the
                  // strip doesn't feel unfinished on a fresh install.
                  ...(cpuPct > 0 || pool.cpu_percent !== undefined ? [{
                    label: 'CPU',
                    value: cpuPct,
                    suffix: '%',
                    onClick: () => onNavigate('pool'),
                    alert: (cpuPct > 85 ? 'red' : cpuPct > 60 ? 'amber' : false) as 'red' | 'amber' | false,
                  }] : []),
                  { label: 'Memory', value: rssMb > 0 ? formatMB(rssMb) : '—',
                    muted: rssMb === 0,
                    onClick: () => onNavigate('pool'),
                    alert: memPct > 80 ? 'red' : memPct > 60 ? 'amber' : false },
                  { label: 'Threads', value: threadCount > 0 ? threadCount : '—', muted: threadCount === 0, onClick: () => onNavigate('pool') },
                  ...(throughput > 0 ? [{
                    label: 'Throughput',
                    value: throughput,
                    suffix: '/hr',
                    onClick: () => onNavigate('pool'),
                  }] : []),
                  { label: 'DB size', value: dbBytes > 0 ? formatBytes(dbBytes) : '—', muted: dbBytes === 0,
                    onClick: () => { try { sessionStorage.setItem('fpulse_settings_jump_to', 'storage'); } catch { /* ignore */ } onNavigate('settings'); } },
                  { label: 'Uptime', value: uptimeSec > 0 ? formatUptime(uptimeSec) : '—', muted: uptimeSec === 0, onClick: () => onNavigate('admin') },
                ]}
              />
              </div>
            </section>
          </>
        )}

        {/* ── 4. SYSTEM USAGE — PROD only (DEV embeds inside the
            collapsible inventory zone). PROD operators want CPU/mem
            visible at all times. */}
        {isProd && (
        <section>
          <div className={`text-xs font-bold uppercase tracking-wider mb-2 ${p.inkMuted}`}>System</div>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
            <FlatKPI
              variant="striped"
              label="CPU"
              icon={kpiIcons.cpu}
              value={cpuPct > 0 || pool.cpu_percent !== undefined ? cpuPct : '—'}
              suffix={cpuPct > 0 || pool.cpu_percent !== undefined ? '%' : ''}
              accent={cpuPct > 85 ? 'red' : cpuPct > 60 ? 'amber' : 'emerald'}
              onClick={() => onNavigate('pool')}
              hint={sys.host?.cpu_count ? `${sys.host.cpu_count} cores` : 'process'}
            />
            <FlatKPI
              variant="striped"
              label="Memory"
              icon={kpiIcons.memory}
              value={rssMb > 0 ? formatMB(rssMb) : '—'}
              accent={memPct > 80 ? 'red' : memPct > 50 ? 'amber' : 'teal'}
              onClick={() => onNavigate('pool')}
              hint={totalMemMb > 0 ? `${memPct}% of ${formatMB(totalMemMb)}` : 'process RSS'}
            />
            <FlatKPI
              variant="striped"
              label="Threads"
              icon={kpiIcons.threads}
              value={threadCount > 0 ? threadCount : '—'}
              accent="cyan"
              onClick={() => onNavigate('pool')}
              hint="live threads"
            />
            <FlatKPI
              variant="striped"
              label="Throughput"
              icon={kpiIcons.throughput}
              value={throughput > 0 ? throughput : '—'}
              suffix={throughput > 0 ? '/hr' : ''}
              accent="blue"
              onClick={() => onNavigate('pool')}
              hint="runs per hour"
            />
            <FlatKPI
              variant="striped"
              label="Storage"
              icon={kpiIcons.db}
              value={dbBytes > 0 ? formatBytes(dbBytes) : '—'}
              accent="slate"
              onClick={() => {
                if (environment !== 'prod') {
                  try { sessionStorage.setItem('fpulse_settings_jump_to', 'storage'); } catch {}
                }
                onNavigate(environment === 'prod' ? 'admin' : 'settings');
              }}
              hint={`${(sys.db_files || []).length} file(s)`}
            />
            <FlatKPI
              variant="striped"
              label="Uptime"
              icon={kpiIcons.uptime}
              value={uptimeSec > 0 ? formatUptime(uptimeSec) : '—'}
              accent="violet"
              onClick={() => onNavigate('admin')}
              hint="since process start"
            />
          </div>
        </section>
        )}

        {/* ── 5. ADMIN-ONLY ROW — PROD admins only ───────────────────
            Rendered only when (isAdmin && isProd). DEV is the building
            environment; governance data (seats, users, license) belongs
            in PROD where admins actually operate. Keeps the DEV dashboard
            focused on iteration and avoids leaking admin chrome to
            developers who happen to have admin role in a sandbox. */}
        {isAdmin && isProd && (
          <section>
            <div className={`text-xs font-bold uppercase tracking-wider mb-2 ${p.inkMuted}`}>Administration</div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <FlatKPI
                label="Seats used"
                icon={kpiIcons.seat}
                value={`${stats?.users?.length ?? 0}`}
                suffix={stats?.license?.seats ? `/ ${stats.license.seats}` : ''}
                accent={
                  stats?.license?.seats && stats.users.length >= stats.license.seats ? 'red'
                  : stats?.license?.seats && stats.users.length / stats.license.seats > 0.8 ? 'amber'
                  : 'blue'
                }
                onClick={() => onNavigate('admin')}
                hint={stats?.license?.is_plus ? 'Plus license' : 'Free tier'}
              />
              <FlatKPI
                label="Users"
                icon={kpiIcons.user}
                value={stats?.users?.length ?? 0}
                accent="violet"
                onClick={() => onNavigate('admin')}
                hint={`${(stats?.users || []).filter((u: any) => u.is_active !== false).length} active`}
              />
              <FlatKPI
                label="Your role"
                icon={kpiIcons.shield}
                value={role.replace('_', ' ')}
                accent="teal"
                onClick={() => onNavigate('account')}
                hint={user?.email || 'signed in'}
              />
              <FlatKPI
                label="Organisation"
                icon={kpiIcons.building}
                value={stats?.license?.org || '—'}
                accent="amber"
                onClick={() => onNavigate('admin')}
                hint={stats?.license?.tier || 'workspace'}
              />
            </div>
          </section>
        )}

        {/* ── 6. CHART + DONUT — smart empty states ──────────────────── */}
        <section className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* Chart (2/3 width) — silver header strip to match page chrome.
              Both DEV and PROD canvases are light, so a dark header was
              fighting the background. Silver keeps visual continuity with
              the navbar + page header without washing into the canvas. */}
          <div className="rounded-xl border border-slate-300 shadow-sm lg:col-span-2 bg-white overflow-hidden">
            <div className="flex items-center justify-between px-5 py-2.5 bg-gradient-to-b from-slate-100 to-slate-200 border-b border-slate-300">
              <div>
                <div className="text-xs font-bold uppercase tracking-wider text-slate-600">
                  {isProd ? `SLA trend · ${windowLong}` : `Run volume · ${windowLong}`}
                </div>
                <div className="text-sm font-bold text-slate-800 mt-0.5">
                  {hasRuns
                    ? (isProd ? `${successRate}% success rate` : `${runs24h} total runs`)
                    : 'No runs yet'}
                </div>
              </div>
              {hasRuns && (
                <div className="flex items-center gap-3 text-xs font-medium">
                  <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded bg-emerald-500" /><span className="text-slate-600">Success</span></span>
                  <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded bg-red-500" /><span className="text-slate-600">Failed</span></span>
                  <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded bg-blue-500" /><span className="text-slate-600">Running</span></span>
                </div>
              )}
            </div>
            <div className="p-5">
            {hasRuns ? (
              <>
                <div className="flex items-end gap-1 h-32">
                  {buckets.map((b, i) => {
                    const total = b.success + b.failed + b.running;
                    const fullHeight = total === 0 ? 4 : Math.max(8, (total / maxCount) * 128);
                    return (
                      <div key={i} className="flex-1 flex flex-col-reverse justify-start items-stretch group relative" style={{ height: '128px' }}
                        title={`${b.label} — ${total} run${total === 1 ? '' : 's'} (${b.success} ok, ${b.failed} failed, ${b.running} running)`}>
                        {total === 0 ? (
                          <div className="rounded-sm bg-slate-200" style={{ height: '4px' }} />
                        ) : (
                          <>
                            {b.success > 0 && <div className={p.okBg} style={{ height: `${(b.success / total) * fullHeight}px` }} />}
                            {b.failed > 0 && <div className={p.badBg} style={{ height: `${(b.failed / total) * fullHeight}px` }} />}
                            {b.running > 0 && <div className="bg-blue-500" style={{ height: `${(b.running / total) * fullHeight}px` }} />}
                          </>
                        )}
                      </div>
                    );
                  })}
                </div>
                <div className="flex items-start gap-1 mt-1">
                  {buckets.map((b, i) => {
                    // Show every Nth label so the x-axis stays readable.
                    // 24 bins → every 4th (6 labels), 7 bins → every label,
                    // 30 bins → every 4th (~8 labels).
                    const labelStride = buckets.length <= 7 ? 1 : Math.ceil(buckets.length / 8);
                    const showLabel = i % labelStride === 0;
                    return (
                      <div key={i} className="flex-1 text-center">
                        <span className={`text-xs font-medium ${p.inkMuted}`}>{showLabel ? b.label : ''}</span>
                      </div>
                    );
                  })}
                </div>
              </>
            ) : (
              // Empty state — compact CTA, no ghost chart reserving rows.
              <div className="flex items-center justify-between gap-3 py-2">
                <div className="min-w-0">
                  <div className={`text-sm font-semibold ${p.ink}`}>
                    {isProd ? 'No production runs in the last 24 hours' : 'Your workspace is ready — no runs yet'}
                  </div>
                  <div className={`text-xs font-medium ${p.inkMuted}`}>
                    {isProd ? 'Deploy a pipeline to begin monitoring activity' : 'Build a pipeline and run it to see volume'}
                  </div>
                </div>
                <button onClick={() => onNavigate(isProd ? 'pipelines' : 'editor')}
                  className={`px-3 py-1.5 text-xs font-bold text-white rounded-lg ${p.accentBg} transition-colors shrink-0`}>
                  {isProd ? 'Deployments' : 'Start'}
                </button>
              </div>
            )}
            </div>
          </div>

          {/* Donut (1/3 width) — silver header to match chart + feeds */}
          <div className="rounded-xl border border-slate-300 shadow-sm bg-white overflow-hidden">
            <div className="px-4 py-2.5 bg-gradient-to-b from-slate-100 to-slate-200 border-b border-slate-300">
              <div className="text-xs font-bold uppercase tracking-wider text-slate-600">
                {isProd
                  ? (hasRuns ? `Run outcome mix (${windowLabel})` : 'Workspace composition')
                  : (hasRuns ? 'Pipeline status' : 'Workspace composition')}
              </div>
            </div>
            <div className="p-5">
            {hasRuns ? (
              isProd ? (
                <StatusDonut
                  ariaLabel="Run outcome mix"
                  segments={[
                    { value: exec?.success || 0, color: '#10b981', label: 'Success' },
                    { value: running,             color: '#3b82f6', label: 'Running' },
                    { value: failures24h,         color: '#ef4444', label: 'Failed' },
                  ]}
                  centerTop={runs24h}
                  centerBottom="runs"
                />
              ) : (
                <StatusDonut
                  ariaLabel="Pipeline status"
                  segments={[
                    { value: exec?.success || 0,                                        color: '#10b981', label: 'Success' },
                    { value: running,                                                   color: '#3b82f6', label: 'Running' },
                    { value: failures24h,                                               color: '#ef4444', label: 'Failed' },
                    { value: Math.max(0, (stats?.pipelines || 0) - running - failures24h), color: '#cbd5e1', label: 'Idle' },
                  ]}
                  centerTop={stats?.pipelines ?? 0}
                  centerBottom="pipelines"
                />
              )
            ) : (
              // No runs — use the donut for composition breakdown so the
              // card isn't wasted on emptiness. Shows inventory distribution.
              <StatusDonut
                ariaLabel="Workspace composition"
                segments={[
                  { value: stats?.pipelines ?? 0,   color: '#3b82f6', label: 'Pipelines' },
                  { value: stats?.connections ?? 0, color: '#06b6d4', label: 'Connections' },
                  { value: stats?.credentials ?? 0, color: '#f59e0b', label: 'Credentials' },
                  { value: stats?.schedules ?? 0,   color: '#10b981', label: 'Schedules' },
                ]}
                centerTop={(stats?.pipelines ?? 0) + (stats?.connections ?? 0) + (stats?.credentials ?? 0) + (stats?.schedules ?? 0)}
                centerBottom="items"
              />
            )}
            </div>
          </div>
        </section>

        {/* ── 7. FEED TABLES — env-specific columns ── */}
        <section className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <FeedColumn
            title={isProd ? 'Active Incidents' : 'Failed Pipelines'}
            emptyText={isProd ? 'No active incidents' : 'Nothing to fix'}
            count={stats?.failedPipelines.length || 0}
            countTone="bad"
            ink={p.ink} inkMuted={p.inkMuted} card={p.card}
            rows={(stats?.failedPipelines || []).map((f: any) => ({
              dotColor: '#ef4444',
              title: f.name || f.workflow_name || 'Untitled',
              sub: formatTimeAgo(f.last_run_at || f.created_at),
              onClick: () => onNavigate('executions'),
            }))}
          />

          {isProd ? (
            <FeedColumn
              title="Pending Approvals"
              emptyText="Queue is clear"
              count={approvalsPending}
              countTone="warn"
              ink={p.ink} inkMuted={p.inkMuted} card={p.card}
              rows={(stats?.pendingApprovals || []).map((a: any) => ({
                dotColor: '#f59e0b',
                title: a.name || a.workflow_name || a.project_name || 'Pending item',
                sub: `Submitted ${formatTimeAgo(a.submitted_at || a.created_at)}`,
                onClick: () => onNavigate('approvals'),
              }))}
              footer={<button onClick={() => onNavigate('approvals')} className={`text-sm font-semibold ${p.accent} hover:underline`}>Review all →</button>}
            />
          ) : (
            <FeedColumn
              title="Recent Runs"
              emptyText="No runs yet"
              emptyCta={{ label: '+ Run a pipeline', onClick: () => onNavigate('pipelines') }}
              ink={p.ink} inkMuted={p.inkMuted} card={p.card}
              rows={(stats?.recentExecutions?.slice(0, 6) || []).map((e: any) => {
                const s = (e.status || '').toLowerCase();
                const color = s === 'success' ? '#10b981' : s === 'running' ? '#3b82f6' : s === 'error' || s === 'failed' ? '#ef4444' : '#cbd5e1';
                return {
                  dotColor: color,
                  title: e.workflow_name || e.name || 'Untitled',
                  sub: `${formatTimeAgo(e.started_at || e.created_at)} · ${formatDuration(e.duration_ms)}`,
                  onClick: () => onNavigate('executions'),
                };
              })}
              footer={<button onClick={() => onNavigate('executions')} className={`text-sm font-semibold ${p.accent} hover:underline`}>View all →</button>}
            />
          )}

          {isProd ? (
            <FeedColumn
              title="Live Runs"
              emptyText="No runs in progress"
              ink={p.ink} inkMuted={p.inkMuted} card={p.card}
              rows={(stats?.recentExecutions?.slice(0, 6) || []).map((e: any) => {
                const s = (e.status || '').toLowerCase();
                const color = s === 'success' ? '#10b981' : s === 'running' ? '#3b82f6' : s === 'error' || s === 'failed' ? '#ef4444' : '#cbd5e1';
                return {
                  dotColor: color,
                  title: e.workflow_name || e.name || 'Untitled',
                  sub: `${formatTimeAgo(e.started_at || e.created_at)} · ${formatDuration(e.duration_ms)}`,
                  onClick: () => onNavigate('executions'),
                };
              })}
              footer={<button onClick={() => onNavigate('executions')} className={`text-sm font-semibold ${p.accent} hover:underline`}>Monitor →</button>}
            />
          ) : (
            <FeedColumn
              title="Active Schedules"
              emptyText="No schedules yet"
              emptyCta={{ label: '+ Create schedule', onClick: () => onNavigate('pipelines') }}
              ink={p.ink} inkMuted={p.inkMuted} card={p.card}
              rows={(stats?.activeSchedules || []).map((s: any) => ({
                dotColor: '#3b82f6',
                title: s.workflow_name || s.name || 'Scheduled run',
                sub: s.cron || s.schedule || 'Recurring',
                onClick: () => onNavigate('pipelines'),
              }))}
              footer={<button onClick={() => onNavigate('pipelines')} className={`text-sm font-semibold ${p.accent} hover:underline`}>Manage →</button>}
            />
          )}
        </section>

        {!loading && !stats && (
          <div className="text-center py-12">
            <div className={`text-lg font-bold ${p.ink}`}>Could not load dashboard data.</div>
            <p className={`text-base font-medium mt-2 ${p.inkMuted}`}>Is the backend running?</p>
            <button onClick={loadAll} className={`text-base font-semibold mt-4 ${p.accent} hover:underline`}>Retry</button>
          </div>
        )}

        {/* Open-core info card — points OSS users at the commercial
            extension. Informational, not paywall. Designed with extra
            visual presence so it stands apart from the data sections
            above without feeling like an upsell.

            2026-05-22 (audit N1): hidden once dismissed and hidden
            on Plus tier. The dismiss state already existed but the
            card was rendered unconditionally — the audit flagged
            this as "permanent marketing card in an operational
            dashboard reduces seriousness." */}
        {!prodCardDismissed && tier !== 'plus' && (
        <section className="relative rounded-2xl overflow-hidden border border-amber-200 shadow-sm bg-gradient-to-br from-amber-50 via-white to-orange-50">
          {/* Accent stripe — amber/gold matches the F-Pulse brand mark */}
          <div className="absolute inset-x-0 top-0 h-1 bg-gradient-to-r from-amber-400 via-amber-500 to-orange-500" />

          {/* Dismiss button — top-right. Persists to localStorage so
              dismissal sticks across reloads. */}
          <button
            onClick={dismissProdCard}
            aria-label="Dismiss the F-Pulse+ info card"
            className="absolute top-3 right-3 w-7 h-7 rounded-md text-slate-400 hover:text-slate-600 hover:bg-amber-100 transition-colors flex items-center justify-center"
            title="Dismiss"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>

          <div className="p-6">
            <div className="flex items-start gap-4">
              <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-amber-400 to-orange-500 flex items-center justify-center shrink-0 text-white shadow-md">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" />
                </svg>
              </div>

              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <h3 className="text-lg font-bold text-slate-900">Running F-Pulse in a team?</h3>
                </div>
                <p className="mt-1.5 text-sm leading-relaxed text-slate-600">
                  F-Pulse+ is a paid extension built on top of this open-source core.
                  It adds team-oriented governance and operational features — without
                  changing the F-Pulse you already know.
                </p>
              </div>
            </div>

            {/* CTA row */}
            <div className="mt-6 flex flex-wrap items-center justify-between gap-4 pt-5 border-t border-amber-100">
              <p className="text-xs text-slate-500 max-w-md">
                The OSS you're using today stays free forever.
              </p>
              <a
                href="https://hybridyn.com/f-pulse"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-semibold text-white bg-gradient-to-r from-amber-500 to-orange-500 hover:from-amber-600 hover:to-orange-600 rounded-lg shadow-sm transition-colors"
              >
                Learn more
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>
                </svg>
              </a>
            </div>
          </div>
        </section>
        )}

        {/* Silence unused-var warning for `now` — rerender-trigger only. */}
        <span className="sr-only">{now}</span>
      </div>
    </div>
  );
}

// ── InfoBullet — one line in the "Discover PROD" feature list ───────────
function InfoBullet({ text }: { text: string }) {
  return (
    <div className="flex items-start gap-2 text-slate-200">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" className="mt-1 shrink-0">
        <polyline points="20 6 9 17 4 12" />
      </svg>
      <span className="font-medium">{text}</span>
    </div>
  );
}

// ── FeedColumn ──────────────────────────────────────────────────────────
function FeedColumn({
  title, rows, emptyText, emptyCta, count, countTone, footer, ink, inkMuted, card,
}: {
  title: string;
  rows: { dotColor: string; title: string; sub: string; onClick?: () => void }[];
  emptyText: string;
  // Optional inline CTA shown next to the empty-state text. Lets the
  // dashboard turn dead "no data" cards into one-click entry points
  // ("+ Create schedule") without growing the footprint of the card.
  emptyCta?: { label: string; onClick: () => void };
  count?: number;
  countTone?: 'bad' | 'warn' | 'ok';
  footer?: React.ReactNode;
  ink: string; inkMuted: string; card: string;
}) {
  const pillCls = countTone === 'bad'
    ? 'bg-red-50 border-red-200 text-red-700'
    : countTone === 'warn'
      ? 'bg-amber-50 border-amber-200 text-amber-700'
      : 'bg-emerald-50 border-emerald-200 text-emerald-700';
  return (
    <div className={`rounded-xl border border-slate-300 shadow-sm overflow-hidden bg-white`}>
      {/* Silver header strip — matches the metallic page-header family
          and reads cleanly against both DEV cream and PROD slate-50
          canvases. Dark headers (previous design) were too heavy for a
          light canvas. */}
      <div className="px-4 py-2.5 border-b border-slate-300 flex items-center justify-between bg-gradient-to-b from-slate-100 to-slate-200">
        <h3 className="text-sm font-bold text-slate-700 uppercase tracking-wide">{title}</h3>
        {count !== undefined && count > 0 && (
          <span className={`text-xs font-bold px-2 py-0.5 rounded-full border ${pillCls}`}>{count}</span>
        )}
      </div>
      <div className="divide-y divide-slate-100">
        {rows.length === 0 ? (
          // Compact empty state — single row matching the height of a
          // populated row (~44px) instead of the previous py-8 (~110px).
          // Stops empty cards from creating dashboard "bloat" the
          // reviewers flagged. Optional inline CTA on the right.
          <div className="px-4 py-3 flex items-center justify-between gap-3">
            <div className="flex items-center gap-2.5 min-w-0">
              <span className="w-2.5 h-2.5 rounded-full bg-slate-300 shrink-0" />
              <span className={`text-sm font-medium truncate ${inkMuted}`}>{emptyText}</span>
            </div>
            {emptyCta && (
              <button
                onClick={emptyCta.onClick}
                className="text-xs font-semibold text-amber-600 hover:text-amber-700 hover:underline shrink-0"
              >
                {emptyCta.label}
              </button>
            )}
          </div>
        ) : (
          rows.map((r, i) => {
            const Cmp: any = r.onClick ? 'button' : 'div';
            return (
              <Cmp key={i} onClick={r.onClick} className={`w-full text-left px-4 py-2.5 flex items-center gap-3 ${r.onClick ? 'hover:bg-slate-50 transition-colors' : ''}`}>
                <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ background: r.dotColor }} />
                <div className="min-w-0 flex-1">
                  <div className={`text-sm font-semibold truncate ${ink}`}>{r.title}</div>
                  <div className={`text-xs font-medium ${inkMuted}`}>{r.sub}</div>
                </div>
              </Cmp>
            );
          })
        )}
      </div>
      {footer && (
        <div className="px-4 py-2.5 border-t border-slate-100 flex justify-end">{footer}</div>
      )}
    </div>
  );
}
