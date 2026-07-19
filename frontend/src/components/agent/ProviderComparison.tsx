/**
 * ProviderComparison — live price/latency comparison across cloud LLM
 * providers. Backend: GET /api/ai/providers/compare.
 *
 * Renders a compact table sorted by per-turn cost, marks the currently-
 * configured providers, and highlights the system's recommendation.
 *
 * Drop-in usage from the agent panel empty state:
 *   <ProviderComparison onConfigure={() => setProviderConfigOpen(true)} />
 */

import { useEffect, useState } from 'react';

interface CompareItem {
  provider: string;
  label: string;
  model: string;
  input_per_mtok_usd: number;
  output_per_mtok_usd: number;
  est_cost_per_turn_usd: number;
  latency_band: 'low' | 'medium' | 'high';
  configured: boolean;
  notes: string;
  recommend: boolean;
}

interface CompareResponse {
  items: CompareItem[];
  source: string;
  refreshed_at: string;
  recommendation_reason: string;
}

const LATENCY_TONE: Record<string, string> = {
  low: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  medium: 'bg-amber-50 text-amber-800 ring-amber-200',
  high: 'bg-red-50 text-red-700 ring-red-200',
};

function fmtUsd(v: number): string {
  if (v === 0) return 'Free';
  if (v < 0.0001) return `$${v.toFixed(6)}`;
  if (v < 0.01) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(2)}`;
}

interface Props {
  onConfigure?: () => void;
}

export default function ProviderComparison({ onConfigure }: Props) {
  const [data, setData] = useState<CompareResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchData = async (isRefresh = false) => {
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    setError(null);
    try {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      const token = localStorage.getItem('fpulse_token') || '';
      const ws = localStorage.getItem('fpulse_workspace_id') || 'default';
      if (token) headers['Authorization'] = `Bearer ${token}`;
      headers['X-Workspace-Id'] = ws;
      // `force=1` tells the backend to bypass its 1 h price-cache and
      // re-fetch from OpenRouter. The `_=ts` param defeats any browser
      // /CDN caching layer in front.
      const url = '/api/ai/providers/compare' + (isRefresh ? `?force=1&_=${Date.now()}` : '');
      const res = await fetch(url, { headers });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = (await res.json()) as CompareResponse;
      setData(json);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load comparison');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (cancelled) return;
      await fetchData(false);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (loading) {
    return (
      <div className="rounded-xl border border-slate-200 bg-white p-3 text-[12px] text-slate-500">
        Loading provider comparison…
      </div>
    );
  }
  if (error || !data) {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-[12px] text-red-700">
        {error || 'Comparison unavailable.'}
      </div>
    );
  }

  // Cheapest first.
  const sorted = [...data.items].sort((a, b) => a.est_cost_per_turn_usd - b.est_cost_per_turn_usd);

  return (
    <div className="rounded-xl border border-slate-200 bg-white overflow-hidden">
      <div className="px-3 py-2 border-b border-slate-100 bg-slate-50 flex items-center justify-between gap-2">
        <div>
          <div className="text-xs font-bold uppercase tracking-wider text-slate-600">
            Provider price comparison
          </div>
          <div className="text-xs text-slate-500">
            Source: {data.source.includes('openrouter') ? 'OpenRouter live + fallback' : 'fallback'} ·
            {' '}refreshed {new Date(data.refreshed_at).toLocaleTimeString()}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => fetchData(true)}
            disabled={refreshing}
            className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-semibold text-slate-700 bg-white hover:bg-slate-100 ring-1 ring-slate-300 rounded-md disabled:opacity-50"
            title="Force a fresh fetch from OpenRouter (1 h cache override)"
          >
            <svg
              width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
              className={refreshing ? 'animate-spin' : ''}
            >
              <path d="M23 4v6h-6" />
              <path d="M1 20v-6h6" />
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10" />
              <path d="M20.49 15a9 9 0 0 1-14.85 3.36L1 14" />
            </svg>
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
          {onConfigure && (
            <button
              type="button"
              onClick={onConfigure}
              className="px-2.5 py-1 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-md"
            >
              Configure
            </button>
          )}
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="bg-slate-50 text-slate-600">
            <tr>
              <th className="text-left px-3 py-1.5 font-semibold">Provider</th>
              <th className="text-right px-2 py-1.5 font-semibold" title="USD per million input tokens">In $/M</th>
              <th className="text-right px-2 py-1.5 font-semibold" title="USD per million output tokens">Out $/M</th>
              <th className="text-right px-2 py-1.5 font-semibold" title="Estimated cost per agent turn (3000 in / 300 out)">Est / turn</th>
              <th className="text-center px-2 py-1.5 font-semibold">Latency</th>
              <th className="text-center px-2 py-1.5 font-semibold">Status</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((row, i) => (
              <tr
                key={i}
                className={`border-t border-slate-100 ${row.recommend ? 'bg-indigo-50/50' : ''}`}
              >
                <td className="px-3 py-1.5">
                  <div className="font-semibold text-slate-800">{row.label}</div>
                  <code className="text-xs text-slate-500">{row.model}</code>
                </td>
                <td className="text-right px-2 py-1.5 tabular-nums">{fmtUsd(row.input_per_mtok_usd)}</td>
                <td className="text-right px-2 py-1.5 tabular-nums">{fmtUsd(row.output_per_mtok_usd)}</td>
                <td className="text-right px-2 py-1.5 tabular-nums font-semibold">
                  {fmtUsd(row.est_cost_per_turn_usd)}
                </td>
                <td className="text-center px-2 py-1.5">
                  <span
                    className={`px-1.5 py-0.5 rounded text-[9px] font-semibold uppercase tracking-wider ring-1 ${LATENCY_TONE[row.latency_band] || LATENCY_TONE.low}`}
                  >
                    {row.latency_band}
                  </span>
                </td>
                <td className="text-center px-2 py-1.5">
                  {row.recommend ? (
                    <span className="px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider bg-indigo-600 text-white">
                      Recommended
                    </span>
                  ) : row.configured ? (
                    <span className="px-1.5 py-0.5 rounded text-[9px] font-semibold uppercase tracking-wider bg-emerald-100 text-emerald-700">
                      Configured
                    </span>
                  ) : (
                    <span className="text-slate-400 text-xs">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="px-3 py-2 border-t border-slate-100 bg-indigo-50 text-xs text-indigo-900">
        <span className="font-semibold">Recommendation:</span> {data.recommendation_reason}
      </div>
    </div>
  );
}
