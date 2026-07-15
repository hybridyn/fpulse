/**
 * TimeAgo — relative time renderer with hover-tooltip showing absolute.
 *
 * Phase 1 foundation. Used for "last run", "modified", "started",
 * "scheduled at" cells across every list page. Auto-refreshes every
 * 60 seconds so "3m ago" doesn't go stale while the page is open.
 *
 * Examples:
 *   "just now"      — < 30s
 *   "32s ago"       — < 60s
 *   "5m ago"        — < 60m
 *   "3h ago"        — < 24h
 *   "2d ago"        — < 30d
 *   "Apr 12"        — same year
 *   "Apr 12, 2024"  — different year
 *
 * Hover shows the full ISO timestamp in localized form.
 */

import { useEffect, useState } from 'react';

export interface TimeAgoProps {
  /** Date input — Date | ISO string | epoch seconds | epoch ms. */
  value: Date | string | number | null | undefined;
  /** Refresh interval in ms; default 60_000. Set 0 to disable auto-refresh. */
  refreshMs?: number;
  /** When true, show absolute date if older than 7 days; otherwise always relative. */
  switchToAbsAfterDays?: number;
  /** Additional class names. */
  className?: string;
  /** Override placeholder text when value is null/undefined. */
  emptyText?: string;
}

function toDate(value: TimeAgoProps['value']): Date | null {
  if (value == null) return null;
  if (value instanceof Date) return isNaN(value.getTime()) ? null : value;
  if (typeof value === 'number') {
    // Heuristic: epoch seconds < 10^12, epoch ms otherwise.
    const ms = value < 1e12 ? value * 1000 : value;
    const d = new Date(ms);
    return isNaN(d.getTime()) ? null : d;
  }
  if (typeof value === 'string') {
    const d = new Date(value);
    return isNaN(d.getTime()) ? null : d;
  }
  return null;
}

function formatRelative(then: Date, switchAfterDays: number): string {
  const now = Date.now();
  const diffMs = now - then.getTime();
  const diffS = Math.round(diffMs / 1000);
  if (diffS < 0) return 'in the future';
  if (diffS < 30) return 'just now';
  if (diffS < 60) return `${diffS}s ago`;
  const diffM = Math.round(diffS / 60);
  if (diffM < 60) return `${diffM}m ago`;
  const diffH = Math.round(diffM / 60);
  if (diffH < 24) return `${diffH}h ago`;
  const diffD = Math.round(diffH / 24);
  if (diffD < switchAfterDays) return `${diffD}d ago`;
  // Switch to absolute after threshold
  const sameYear = then.getFullYear() === new Date().getFullYear();
  const opts: Intl.DateTimeFormatOptions = sameYear
    ? { month: 'short', day: 'numeric' }
    : { month: 'short', day: 'numeric', year: 'numeric' };
  return then.toLocaleDateString(undefined, opts);
}

function formatAbsolute(d: Date): string {
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    timeZoneName: 'short',
  });
}

export function TimeAgo({
  value,
  refreshMs = 60_000,
  switchToAbsAfterDays = 30,
  className,
  emptyText = '—',
}: TimeAgoProps) {
  const [, tick] = useState(0);

  // Auto-refresh tick to keep the relative label current.
  useEffect(() => {
    if (!refreshMs || refreshMs < 1000) return;
    const id = setInterval(() => tick(n => (n + 1) % 1_000_000), refreshMs);
    return () => clearInterval(id);
  }, [refreshMs]);

  const date = toDate(value);
  if (!date) {
    return <span className={`text-slate-400 ${className ?? ''}`}>{emptyText}</span>;
  }
  return (
    <span
      className={`text-slate-700 tabular-nums ${className ?? ''}`}
      title={formatAbsolute(date)}
    >
      {formatRelative(date, switchToAbsAfterDays)}
    </span>
  );
}

export default TimeAgo;
