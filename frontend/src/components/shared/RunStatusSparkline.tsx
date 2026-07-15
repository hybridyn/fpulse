/**
 * RunStatusSparkline — N4 round 1.
 *
 * Compact dot-row sparkline of recent run statuses. Renders one
 * fixed-size dot per run, colored by status (green = success, red =
 * error, amber = warning/timeout, slate = running, light-slate =
 * skipped). Newest on the right (calendar-time order).
 *
 * Component is data-pure — caller passes the status array. The full
 * calendar-style grid view (dates × pipelines) is a separate round
 * built on top of a backend endpoint that aggregates this data
 * per-pipeline; this primitive renders the same colored-cell pattern
 * for inline use in any row.
 *
 * Usage:
 *
 *   <RunStatusSparkline
 *     statuses={['success', 'success', 'error', 'success']}
 *     onClickRun={(idx) => goToExecutionDetail(...)}
 *     compact
 *   />
 */

import { type ReactNode } from 'react';

export type RunStatusKind =
  | 'success'
  | 'error'
  | 'warning'
  | 'running'
  | 'skipped'
  | 'unknown';

interface Props {
  /** Status entries in time order (oldest first). The component renders newest-on-the-right. */
  statuses: RunStatusKind[];
  /** When set, slot-fills empty cells on the LEFT so every sparkline renders to the same width.
   * Useful when aligning across rows in a table. */
  pad?: number;
  /** Compact mode = 6px dots; default = 8px dots. */
  compact?: boolean;
  /** Optional handler — fires with the dot index (post-pad). Lets the
   * caller deep-link into the run detail page. */
  onClickRun?: (index: number) => void;
  /** Optional aria-label override; default summarises the sparkline. */
  ariaLabel?: string;
}

const COLOR_BY_STATUS: Record<RunStatusKind, string> = {
  success: 'bg-emerald-500',
  error: 'bg-red-500',
  warning: 'bg-amber-400',
  running: 'bg-blue-400 animate-pulse',
  skipped: 'bg-slate-300',
  unknown: 'bg-slate-200',
};

const STATUS_LABEL: Record<RunStatusKind, string> = {
  success: 'succeeded',
  error: 'failed',
  warning: 'warning',
  running: 'running',
  skipped: 'skipped',
  unknown: 'unknown',
};

export default function RunStatusSparkline({
  statuses,
  pad = 0,
  compact = false,
  onClickRun,
  ariaLabel,
}: Props): ReactNode {
  const dotCls = compact ? 'w-1.5 h-1.5' : 'w-2 h-2';
  const gapCls = compact ? 'gap-0.5' : 'gap-1';

  // Pad to the LEFT so the newest run is always at the right edge —
  // matches how operators read recency at a glance.
  const padCount = Math.max(0, pad - statuses.length);

  const totalRuns = statuses.length;
  const failed = statuses.filter((s) => s === 'error').length;
  const succeeded = statuses.filter((s) => s === 'success').length;
  const summary =
    ariaLabel
      ?? (totalRuns === 0
          ? 'No recent runs'
          : `${totalRuns} recent run${totalRuns === 1 ? '' : 's'}: ${succeeded} succeeded, ${failed} failed`);

  return (
    <div
      className={`inline-flex items-center ${gapCls}`}
      role="img"
      aria-label={summary}
      title={summary}
    >
      {/* Left-pad: render dimmed empty slots so the sparkline width
          stays uniform across rows. */}
      {Array.from({ length: padCount }).map((_, i) => (
        <span
          key={`pad-${i}`}
          className={`${dotCls} rounded-full bg-slate-100 ring-1 ring-slate-200`}
          aria-hidden="true"
        />
      ))}
      {statuses.map((s, i) => {
        const real = s in COLOR_BY_STATUS ? s : 'unknown';
        const cls = COLOR_BY_STATUS[real as RunStatusKind];
        const idx = i + padCount;
        return onClickRun ? (
          <button
            key={i}
            type="button"
            onClick={() => onClickRun(idx)}
            className={`${dotCls} rounded-full ${cls} cursor-pointer hover:scale-125 transition-transform focus:outline-none focus:ring-2 focus:ring-offset-1 focus:ring-amber-300`}
            title={`Run ${idx + 1} of ${totalRuns + padCount} — ${STATUS_LABEL[real as RunStatusKind]}`}
            aria-label={`Run ${idx + 1}: ${STATUS_LABEL[real as RunStatusKind]}`}
          />
        ) : (
          <span
            key={i}
            className={`${dotCls} rounded-full ${cls}`}
            title={`Run ${idx + 1} of ${totalRuns + padCount} — ${STATUS_LABEL[real as RunStatusKind]}`}
          />
        );
      })}
    </div>
  );
}
