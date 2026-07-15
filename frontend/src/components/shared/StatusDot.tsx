/**
 * StatusDot — color-coded circle for run / pipeline / connection status.
 *
 * Phase 1 foundation. Replaces the ad-hoc colored badges scattered across
 * pages today. One canonical mapping of status → color, used everywhere
 * a row needs a status indicator.
 *
 * Variants:
 *   success    — emerald (last run succeeded / connection healthy)
 *   failed     — red    (last run failed / connection unreachable)
 *   running    — indigo + pulse (in flight)
 *   draft      — slate  (unpublished / not yet run)
 *   scheduled  — amber  (pending future trigger)
 *   warning    — amber  (degraded / stale)
 *   archived   — slate-300 (soft-archived)
 *
 * Ring + size variants for context — small (8x8) for table rows,
 * medium (10x10) for cards, large (14x14) for hero/header.
 */

export type StatusKind =
  | 'success' | 'failed' | 'running' | 'draft'
  | 'scheduled' | 'warning' | 'archived' | 'unknown';

export type StatusSize = 'sm' | 'md' | 'lg';

const STATUS_STYLES: Record<StatusKind, { dot: string; ring: string; label: string }> = {
  success:   { dot: 'bg-emerald-500',  ring: 'ring-emerald-200', label: 'Success'   },
  failed:    { dot: 'bg-red-500',      ring: 'ring-red-200',     label: 'Failed'    },
  running:   { dot: 'bg-indigo-500',   ring: 'ring-indigo-200',  label: 'Running'   },
  draft:     { dot: 'bg-slate-400',    ring: 'ring-slate-200',   label: 'Draft'     },
  scheduled: { dot: 'bg-amber-500',    ring: 'ring-amber-200',   label: 'Scheduled' },
  warning:   { dot: 'bg-amber-500',    ring: 'ring-amber-200',   label: 'Warning'   },
  archived:  { dot: 'bg-slate-300',    ring: 'ring-slate-200',   label: 'Archived'  },
  unknown:   { dot: 'bg-slate-300',    ring: 'ring-slate-200',   label: 'Unknown'   },
};

const SIZE_PX: Record<StatusSize, { dot: string; pad: string }> = {
  sm: { dot: 'w-2 h-2',     pad: 'p-0.5' },
  md: { dot: 'w-2.5 h-2.5', pad: 'p-0.5' },
  lg: { dot: 'w-3.5 h-3.5', pad: 'p-1'   },
};

export interface StatusDotProps {
  status: StatusKind;
  size?: StatusSize;
  /** When true, show a subtle outer ring around the dot. */
  ringed?: boolean;
  /** When true and status === 'running', adds a pulse animation. */
  pulse?: boolean;
  /** Show the status label next to the dot. */
  withLabel?: boolean;
  /** Override for the label text (defaults to canonical label). */
  labelOverride?: string;
  /** Title attribute for hover tooltip. Defaults to label. */
  hint?: string;
}

export function StatusDot({
  status, size = 'sm', ringed = false, pulse = true,
  withLabel = false, labelOverride, hint,
}: StatusDotProps) {
  const s = STATUS_STYLES[status] ?? STATUS_STYLES.unknown;
  const sz = SIZE_PX[size];
  const animateClass = pulse && status === 'running' ? 'animate-pulse' : '';

  const dot = (
    <span
      className={[
        'inline-block rounded-full',
        sz.dot,
        s.dot,
        animateClass,
        ringed ? `ring-2 ${s.ring}` : '',
      ].join(' ')}
      title={hint ?? s.label}
      aria-label={hint ?? s.label}
    />
  );

  if (!withLabel) return dot;

  return (
    <span className="inline-flex items-center gap-1.5 text-[12px] text-slate-700">
      {dot}
      <span>{labelOverride ?? s.label}</span>
    </span>
  );
}

/** Map free-form backend status strings → canonical StatusKind.
 *  Extend this as new status values appear. */
export function normalizeStatus(raw: string | undefined | null): StatusKind {
  if (!raw) return 'unknown';
  const s = String(raw).toLowerCase().trim();
  if (['success', 'succeeded', 'ok', 'completed', 'healthy', 'active', 'published'].includes(s)) return 'success';
  if (['failed', 'failure', 'error', 'errored', 'broken'].includes(s)) return 'failed';
  if (['running', 'in_progress', 'in-flight', 'queued', 'starting'].includes(s)) return 'running';
  if (['draft', 'unpublished', 'new'].includes(s)) return 'draft';
  if (['scheduled', 'pending'].includes(s)) return 'scheduled';
  if (['warning', 'degraded', 'stale'].includes(s)) return 'warning';
  if (['archived', 'deleted'].includes(s)) return 'archived';
  return 'unknown';
}

export default StatusDot;
