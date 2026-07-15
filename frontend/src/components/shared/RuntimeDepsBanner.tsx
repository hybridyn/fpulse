/**
 * RuntimeDepsBanner — surfaces missing runtime dependencies.
 *
 * P0 Day 3 (2026-05-23) addition. Polls /api/system/dependencies and
 * renders a compact banner when DuckDB / data-dir disk / local LLM
 * are degraded. Stays out of the way when everything is fine — the
 * banner only mounts content when at least one check is non-ok.
 *
 * Two render modes:
 *   - `compact` (default): one-line summary suitable for Dashboard /
 *     Storage / Editor pages. Click to expand to the full check list.
 *   - `full`: always-expanded list. Used by Settings → Runtime card.
 *
 * Cached for 30s; the caller can pass a key change (e.g. `refreshTick`)
 * to force a re-fetch on demand.
 */

import { useEffect, useState } from 'react';
import { api } from '../../api/client';

interface DepCheck {
  id: string;
  label: string;
  status: 'ok' | 'warn' | 'missing' | 'error';
  detail: string;
  required: boolean;
  blocks: string[];
  extra?: Record<string, unknown>;
}

interface DepsResponse {
  checks: DepCheck[];
  summary: { ok: number; warn: number; missing: number; total: number };
}

let _cached: { ts: number; data: DepsResponse } | null = null;
const CACHE_MS = 30_000;

async function fetchDeps(force = false): Promise<DepsResponse | null> {
  if (!force && _cached && Date.now() - _cached.ts < CACHE_MS) return _cached.data;
  try {
    const data = await api.get<DepsResponse>('/api/system/dependencies');
    _cached = { ts: Date.now(), data };
    return data;
  } catch {
    return null;
  }
}

export default function RuntimeDepsBanner({
  mode = 'compact',
  refreshTick = 0,
}: {
  mode?: 'compact' | 'full';
  refreshTick?: number;
}) {
  const [data, setData] = useState<DepsResponse | null>(null);
  const [expanded, setExpanded] = useState(mode === 'full');

  useEffect(() => {
    let alive = true;
    fetchDeps(refreshTick > 0).then((d) => {
      if (alive) setData(d);
    });
    return () => {
      alive = false;
    };
  }, [refreshTick]);

  if (!data) return null;
  // No degraded checks → render nothing (compact mode) or a calm
  // all-clear row (full mode).
  const degraded = data.checks.filter((c) => c.status !== 'ok');
  if (degraded.length === 0) {
    if (mode === 'full') {
      return (
        <div className="rounded-lg border border-emerald-200 bg-emerald-50/60 px-3 py-2 text-xs text-emerald-700 font-medium flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-emerald-500" />
          All {data.summary.total} runtime dependencies healthy.
        </div>
      );
    }
    return null;
  }

  const requiredMissing = degraded.filter((c) => c.required && (c.status === 'missing' || c.status === 'error'));
  const tone = requiredMissing.length > 0 ? 'red' : 'amber';
  const cls = tone === 'red'
    ? 'border-red-200 bg-red-50/70 text-red-800'
    : 'border-amber-200 bg-amber-50/70 text-amber-900';
  const dotCls = tone === 'red' ? 'bg-red-500' : 'bg-amber-500';
  const headline = requiredMissing.length > 0
    ? `${requiredMissing.length} required dependenc${requiredMissing.length === 1 ? 'y' : 'ies'} missing`
    : `${degraded.length} runtime warning${degraded.length === 1 ? '' : 's'}`;

  return (
    <div className={`mb-4 rounded-lg border ${cls}`}>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-3 px-4 py-2.5 text-left"
      >
        <span className={`w-2 h-2 rounded-full ${dotCls} shrink-0`} />
        <span className="text-sm font-semibold">{headline}</span>
        <span className="text-xs opacity-80 flex-1 truncate">
          {degraded.map((c) => c.label).join(' · ')}
        </span>
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={`transition-transform ${expanded ? 'rotate-180' : ''}`}
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>
      {expanded && (
        <div className="border-t border-current/10 px-4 py-3 space-y-2">
          {degraded.map((c) => (
            <div key={c.id} className="flex items-start gap-3">
              <span
                className={`inline-block w-1.5 h-1.5 rounded-full mt-1.5 shrink-0 ${
                  c.status === 'ok'
                    ? 'bg-emerald-500'
                    : c.status === 'warn'
                      ? 'bg-amber-500'
                      : 'bg-red-500'
                }`}
              />
              <div className="flex-1 min-w-0">
                <div className="flex items-baseline gap-2 flex-wrap">
                  <span className="text-sm font-semibold">{c.label}</span>
                  <span className="text-[10px] font-bold uppercase tracking-wider opacity-70">
                    {c.status}
                  </span>
                  {c.required && (
                    <span className="text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-slate-200/80 text-slate-700">
                      required
                    </span>
                  )}
                </div>
                <div className="text-xs opacity-80 mt-0.5">{c.detail}</div>
                {c.blocks.length > 0 && (
                  <div className="text-[11px] opacity-70 mt-1">
                    Blocks: {c.blocks.join(' · ')}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
