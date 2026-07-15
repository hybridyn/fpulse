/**
 * StatusPill — one palette + one icon set for every run / step / extraction
 * status across the app.
 *
 * 2026-05-19 (P1 #2 of PAGE_BY_PAGE_AUDIT.md): four separate palettes were
 * in flight before this primitive landed:
 *   - ExecutionsPage list: STATUS_STYLES_LIGHT/DARK (rounded pills,
 *     soft tints — bg-green-50 / text-green-600 etc.)
 *   - ExecutionPoolPage run history: inline conditionals
 *     (bg-emerald-100 text-emerald-700)
 *   - ExtractionPage: PHASE_COLOR (starting / list / enrichment /
 *     completed / failed) with its own icon glyphs
 *   - ExecutionsPage lineage detail: LINEAGE_STATUS_STYLES with a fifth
 *     `skipped` style
 *
 * A "success" run now reads the same everywhere. The vocabulary maps the
 * union of the four prior palettes onto a single status set.
 *
 * Adoption is incremental — pages can swap their inline pill for this
 * component one row at a time. New pages should use this from the start.
 */

import type { ReactNode } from 'react';
import { useDarkMode } from '../../hooks/useDarkMode';

export type StatusKind =
  | 'success'    // completed, run_succeeded, ok
  | 'error'      // failed, run_failed, timed_out
  | 'running'    // in-flight (animated dot)
  | 'pending'    // queued / not started
  | 'warning'    // partial-failure, soft-warning, schedule-miss
  | 'skipped'    // not run due to upstream / deactivation
  | 'cancelled'  // user-cancelled
  | 'info';      // neutral fallback

export interface StatusPillProps {
  status: StatusKind | string;
  /** Override the default label (e.g. backend-typed phase). */
  label?: string;
  /** Compact = shorter pill suitable for table rows. */
  size?: 'sm' | 'md';
  /** Optional title attribute for tooltips. */
  title?: string;
}

// Normalise free-form backend strings onto the canonical StatusKind set.
// Anything we don't recognise becomes `info` so the pill never renders blank.
export function normaliseStatus(raw: string | undefined | null): StatusKind {
  if (!raw) return 'info';
  const s = String(raw).toLowerCase();
  if (s === 'success' || s === 'succeeded' || s === 'completed' || s === 'ok' || s === 'pass' || s === 'passed') return 'success';
  if (s === 'error' || s === 'failed' || s === 'failure' || s === 'timed_out' || s === 'timeout' || s === 'fail') return 'error';
  if (s === 'running' || s === 'in_progress' || s === 'in-progress' || s === 'active' || s === 'starting') return 'running';
  if (s === 'pending' || s === 'queued' || s === 'waiting' || s === 'scheduled') return 'pending';
  if (s === 'warning' || s === 'warn' || s === 'partial' || s === 'partial_success' || s === 'schedule_miss') return 'warning';
  if (s === 'skipped' || s === 'shadowed' || s === 'deactivated') return 'skipped';
  if (s === 'cancelled' || s === 'canceled' || s === 'aborted') return 'cancelled';
  return 'info';
}

const STYLES_LIGHT: Record<StatusKind, { bg: string; fg: string; ring: string; dot: string }> = {
  success:   { bg: 'bg-emerald-50', fg: 'text-emerald-700', ring: 'ring-emerald-200', dot: 'bg-emerald-500' },
  error:     { bg: 'bg-red-50',     fg: 'text-red-700',     ring: 'ring-red-200',     dot: 'bg-red-500' },
  running:   { bg: 'bg-amber-50',   fg: 'text-amber-700',   ring: 'ring-amber-200',   dot: 'bg-amber-500' },
  pending:   { bg: 'bg-slate-50',   fg: 'text-slate-600',   ring: 'ring-slate-200',   dot: 'bg-slate-400' },
  warning:   { bg: 'bg-amber-50',   fg: 'text-amber-800',   ring: 'ring-amber-300',   dot: 'bg-amber-600' },
  skipped:   { bg: 'bg-slate-100',  fg: 'text-slate-500',   ring: 'ring-slate-300',   dot: 'bg-slate-400' },
  cancelled: { bg: 'bg-slate-100',  fg: 'text-slate-600',   ring: 'ring-slate-300',   dot: 'bg-slate-400' },
  info:      { bg: 'bg-blue-50',    fg: 'text-blue-700',    ring: 'ring-blue-200',    dot: 'bg-blue-500' },
};

const STYLES_DARK: Record<StatusKind, { bg: string; fg: string; ring: string; dot: string }> = {
  success:   { bg: 'bg-emerald-500/15', fg: 'text-emerald-300', ring: 'ring-emerald-500/20', dot: 'bg-emerald-400' },
  error:     { bg: 'bg-red-500/15',     fg: 'text-red-300',     ring: 'ring-red-500/20',     dot: 'bg-red-400' },
  running:   { bg: 'bg-amber-500/15',   fg: 'text-amber-300',   ring: 'ring-amber-500/20',   dot: 'bg-amber-400' },
  pending:   { bg: 'bg-slate-500/15',   fg: 'text-slate-300',   ring: 'ring-slate-500/20',   dot: 'bg-slate-400' },
  warning:   { bg: 'bg-amber-500/15',   fg: 'text-amber-200',   ring: 'ring-amber-500/20',   dot: 'bg-amber-300' },
  skipped:   { bg: 'bg-slate-700/30',   fg: 'text-slate-400',   ring: 'ring-slate-600/30',   dot: 'bg-slate-500' },
  cancelled: { bg: 'bg-slate-700/30',   fg: 'text-slate-400',   ring: 'ring-slate-600/30',   dot: 'bg-slate-500' },
  info:      { bg: 'bg-blue-500/15',    fg: 'text-blue-300',    ring: 'ring-blue-500/20',    dot: 'bg-blue-400' },
};

const DEFAULT_LABELS: Record<StatusKind, string> = {
  success: 'Success',
  error: 'Error',
  running: 'Running',
  pending: 'Pending',
  warning: 'Warning',
  skipped: 'Skipped',
  cancelled: 'Cancelled',
  info: 'Info',
};

export default function StatusPill({ status, label, size = 'md', title }: StatusPillProps): ReactNode {
  const dark = useDarkMode();
  const kind = normaliseStatus(status);
  const palette = dark ? STYLES_DARK[kind] : STYLES_LIGHT[kind];
  const text = label ?? (typeof status === 'string' && status !== kind ? status : DEFAULT_LABELS[kind]);
  const pad = size === 'sm' ? 'px-1.5 py-0.5 text-[10px]' : 'px-2.5 py-1 text-xs';
  const dotSize = size === 'sm' ? 'w-1 h-1' : 'w-1.5 h-1.5';
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full font-bold uppercase tracking-wider ring-1 ${pad} ${palette.bg} ${palette.fg} ${palette.ring}`}
      title={title}
    >
      <span
        className={`rounded-full ${dotSize} ${palette.dot} ${kind === 'running' ? 'animate-pulse' : ''}`}
      />
      {text}
    </span>
  );
}
