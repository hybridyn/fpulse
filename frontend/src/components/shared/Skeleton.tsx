/**
 * Skeleton — placeholder loader that preserves layout while data loads.
 *
 * Phase 1 foundation. Replaces "click → spinner → layout shift when data
 * lands" with "click → skeleton in correct shape → content fades in".
 * Per design decision D-004: render skeletons after 200ms of waiting,
 * never sooner (avoids flicker on fast responses).
 *
 * Variants:
 *   - SkeletonText        : multiline text placeholder
 *   - SkeletonKPI         : single KPI tile placeholder (matches KPITile shape)
 *   - SkeletonKPIStrip    : 4 KPI tiles in the same responsive grid as KPIStrip
 *   - SkeletonTableRow    : one row of a table; pass column count
 *   - SkeletonCard        : a generic card placeholder (matches card-grid layouts)
 *
 * All variants use a CSS-only pulse animation (no JS).
 */

const PULSE = 'animate-pulse bg-slate-200/70 rounded';

export function SkeletonText({ lines = 3, lastWidth = '60%' }: { lines?: number; lastWidth?: string }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: lines }).map((_, i) => (
        <div
          key={i}
          className={`h-3.5 ${PULSE}`}
          style={{ width: i === lines - 1 ? lastWidth : '100%' }}
        />
      ))}
    </div>
  );
}

export function SkeletonKPI() {
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 ring-1 ring-slate-100">
      <div className={`h-3 w-16 mb-2 ${PULSE}`} />
      <div className={`h-7 w-24 mb-2 ${PULSE}`} />
      <div className={`h-3 w-12 ${PULSE}`} />
    </div>
  );
}

export function SkeletonKPIStrip({ count = 4 }: { count?: number }) {
  return (
    <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4">
      {Array.from({ length: count }).map((_, i) => <SkeletonKPI key={i} />)}
    </div>
  );
}

export function SkeletonTableRow({ columns = 6 }: { columns?: number }) {
  return (
    <tr>
      {Array.from({ length: columns }).map((_, i) => (
        <td key={i} className="px-3 py-3">
          <div className={`h-3.5 w-full max-w-[140px] ${PULSE}`} />
        </td>
      ))}
    </tr>
  );
}

export function SkeletonCard({ height = 120 }: { height?: number }) {
  return (
    <div
      className="rounded-xl border border-slate-200 bg-white p-4"
      style={{ height }}
    >
      <div className={`h-4 w-1/2 mb-3 ${PULSE}`} />
      <div className={`h-3 w-full mb-2 ${PULSE}`} />
      <div className={`h-3 w-3/4 mb-2 ${PULSE}`} />
      <div className={`h-3 w-2/3 ${PULSE}`} />
    </div>
  );
}

export interface DelayedSkeletonProps {
  /** Don't render anything until at least this many ms have passed.
   *  Per D-004, default 200 to avoid flicker on fast responses. */
  delayMs?: number;
  /** Whatever skeleton variant to show. */
  children: React.ReactNode;
}

import { useEffect, useState } from 'react';

/** Wraps a skeleton so it only appears after `delayMs` of waiting. Useful
 *  when you want skeleton-after-delay semantics without a spinner first. */
export function DelayedSkeleton({ delayMs = 200, children }: DelayedSkeletonProps) {
  const [show, setShow] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setShow(true), delayMs);
    return () => clearTimeout(t);
  }, [delayMs]);
  return show ? <>{children}</> : null;
}
