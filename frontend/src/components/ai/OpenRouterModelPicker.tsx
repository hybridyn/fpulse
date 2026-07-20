import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api/client';

/**
 * Model picker for OpenRouter — replaces the free-text input in
 * AIProviderForm when provider="openrouter". Hits /api/ai/openrouter/models,
 * which proxies the public OpenRouter catalog with 1h caching.
 *
 * Surfaces three filters the user actually cares about:
 *   - Search by name / id substring
 *   - Free-only toggle (badges :free models — zero-cost for testing)
 *   - Tools-only toggle (filters to tool-capable models for the agent loop)
 *
 * Falls back to a free-text input when the catalog is unreachable so the
 * user is never blocked from saving a model id manually.
 */

interface OpenRouterModelItem {
  id: string;
  name: string;
  context_length: number;
  prompt_price_per_mtok: number;
  completion_price_per_mtok: number;
  est_cost_per_turn_usd: number;
  is_free: boolean;
  supports_tools: boolean;
}

interface OpenRouterModelsResponse {
  items: OpenRouterModelItem[];
  total: number;
  source: string;
  refreshed_at: string;
}

interface Props {
  value: string;
  onChange: (modelId: string) => void;
  disabled?: boolean;
}

function formatPrice(perTurn: number): string {
  if (perTurn === 0) return 'Free';
  if (perTurn < 0.0001) return '<$0.0001';
  if (perTurn < 0.01) return `$${perTurn.toFixed(5)}`;
  return `$${perTurn.toFixed(4)}`;
}

function formatContext(n: number): string {
  if (!n) return '—';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1000)}K`;
  return String(n);
}

export default function OpenRouterModelPicker({ value, onChange, disabled }: Props) {
  const [models, setModels] = useState<OpenRouterModelItem[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [freeOnly, setFreeOnly] = useState(false);
  const [toolsOnly, setToolsOnly] = useState(false);
  const [refreshedAt, setRefreshedAt] = useState<string>('');
  // Browse mode: list collapsed by default. The user sees their currently
  // selected model + a "Browse models" / "Change" affordance, click to
  // expand. Hides the long catalog behind one click — the always-visible
  // scrollable wall of models that the previous version had pushed real
  // settings below the fold. May 2 2026.
  const [browseOpen, setBrowseOpen] = useState(false);

  async function load(force = false) {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (force) params.set('force', '1');
      const path = `/api/ai/openrouter/models${params.toString() ? `?${params}` : ''}`;
      const resp = await api.get<OpenRouterModelsResponse>(path);
      if (resp.source === 'unavailable') {
        setError("OpenRouter catalog unreachable — type the model id manually below.");
        setModels([]);
      } else {
        setModels(resp.items);
        setRefreshedAt(resp.refreshed_at);
      }
    } catch (e) {
      setError("Couldn't load OpenRouter models — type the id manually below.");
      setModels([]);
    } finally {
      setLoading(false);
    }
  }

  // Lazy-load: only fetch the catalog when the user actually opens browse
  // mode. No need to pull 100+ models on mount if they never click. If
  // already loaded, opening is instant.
  useEffect(() => {
    if (browseOpen && models === null && !loading) {
      load(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [browseOpen]);

  // Find the metadata for the currently-selected model to render a richer
  // summary chip (price + context + tools badges). Falls back to id-only
  // when the model isn't in the loaded catalog yet.
  const selectedMeta = useMemo(
    () => models?.find((m) => m.id === value) || null,
    [models, value],
  );

  const filtered = useMemo(() => {
    if (!models) return [];
    const q = search.trim().toLowerCase();
    return models.filter((m) => {
      if (freeOnly && !m.is_free) return false;
      if (toolsOnly && !m.supports_tools) return false;
      if (!q) return true;
      return m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q);
    });
  }, [models, search, freeOnly, toolsOnly]);

  const showFallback: boolean = !loading && (models === null || (models.length === 0 && !!error));

  return (
    <div className="space-y-2">
      {/* Collapsed-by-default summary chip + Browse toggle. The full model
          list is hidden until the user clicks Browse — avoids the wall of
          100+ models that the always-visible version showed. */}
      {!showFallback && !browseOpen && (
        <div className="flex items-center justify-between gap-3 px-3 py-2.5 rounded-lg border border-slate-200 bg-white">
          <div className="flex items-center gap-2 min-w-0 flex-1">
            <code className="text-[12px] font-mono font-semibold text-slate-800 truncate">
              {value || '(no model selected)'}
            </code>
            {selectedMeta?.is_free && (
              <span className="text-[9px] font-bold uppercase tracking-wider text-emerald-700 bg-emerald-100 px-1.5 py-0.5 rounded shrink-0">
                Free
              </span>
            )}
            {selectedMeta?.supports_tools && (
              <span className="text-[9px] font-bold uppercase tracking-wider text-violet-700 bg-violet-100 px-1.5 py-0.5 rounded shrink-0">
                Tools
              </span>
            )}
            {selectedMeta && (
              <span className="text-xs text-slate-500 shrink-0">
                ctx {formatContext(selectedMeta.context_length)} · ~{formatPrice(selectedMeta.est_cost_per_turn_usd)}/turn
              </span>
            )}
          </div>
          <button
            type="button"
            onClick={() => setBrowseOpen(true)}
            disabled={disabled}
            className="shrink-0 px-3 py-1 text-xs font-semibold text-violet-700 bg-violet-50 hover:bg-violet-100 border border-violet-200 rounded-md transition-colors disabled:opacity-50"
          >
            {value ? 'Change model' : 'Browse models'}
          </button>
        </div>
      )}

      {/* Browse mode — filter row + scrollable list. Only rendered when the
          user has explicitly opened it. */}
      {!showFallback && browseOpen && (
        <>
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <span className="text-xs font-semibold text-slate-700">Browse OpenRouter catalog</span>
            <button
              type="button"
              onClick={() => setBrowseOpen(false)}
              className="text-xs font-medium text-slate-500 hover:text-slate-700"
            >
              ✕ Close
            </button>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search models (e.g. claude, llama, free)…"
              disabled={disabled || loading}
              className="flex-1 min-w-[200px] px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg focus:ring-2 focus:ring-violet-300 focus:border-violet-300 outline-none disabled:bg-slate-50"
            />
            <label className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-700 select-none cursor-pointer">
              <input
                type="checkbox"
                checked={freeOnly}
                onChange={(e) => setFreeOnly(e.target.checked)}
                disabled={disabled || loading}
                className="w-3.5 h-3.5"
              />
              Free only
            </label>
            <label className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-700 select-none cursor-pointer">
              <input
                type="checkbox"
                checked={toolsOnly}
                onChange={(e) => setToolsOnly(e.target.checked)}
                disabled={disabled || loading}
                className="w-3.5 h-3.5"
              />
              Tool-capable
            </label>
            <button
              type="button"
              onClick={() => load(true)}
              disabled={disabled || loading}
              className="px-2 py-1 text-xs font-medium text-slate-700 bg-slate-100 hover:bg-slate-200 rounded disabled:opacity-50"
              title="Bypass cache and refetch from OpenRouter"
            >
              {loading ? 'Loading…' : 'Refresh'}
            </button>
          </div>
        </>
      )}

      {/* Result list OR free-text fallback. The list is only rendered in
          browse mode; in collapsed mode the user just sees the summary chip
          above. Selecting a model auto-closes browse mode. */}
      {showFallback ? (
        <div>
          <input
            type="text"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            disabled={disabled}
            placeholder="openai/gpt-4o-mini"
            className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-violet-300 focus:border-violet-300 outline-none disabled:bg-slate-50 font-mono"
          />
          {error && <div className="mt-1 text-xs text-amber-700">{error}</div>}
        </div>
      ) : browseOpen ? (
        <div className="border border-slate-200 rounded-lg max-h-72 overflow-y-auto bg-white">
          {loading && (
            <div className="p-3 text-xs text-slate-500">Loading OpenRouter catalog…</div>
          )}
          {!loading && filtered.length === 0 && (
            <div className="p-3 text-xs text-slate-500">
              No models match. Clear filters or type the id manually:
              <input
                type="text"
                value={value}
                onChange={(e) => onChange(e.target.value)}
                disabled={disabled}
                placeholder="openai/gpt-4o-mini"
                className="mt-2 w-full px-2 py-1.5 text-xs border border-slate-200 rounded font-mono"
              />
            </div>
          )}
          {!loading && filtered.map((m) => {
            const selected = m.id === value;
            return (
              <button
                key={m.id}
                type="button"
                onClick={() => {
                  onChange(m.id);
                  setBrowseOpen(false);  // collapse back to summary chip after pick
                }}
                disabled={disabled}
                className={`w-full text-left px-3 py-2 border-b border-slate-100 last:border-b-0 transition ${
                  selected ? 'bg-violet-50' : 'hover:bg-slate-50'
                } disabled:opacity-50`}
              >
                <div className="flex items-center justify-between gap-2">
                  <code className="text-[12px] font-mono text-slate-800 truncate">{m.id}</code>
                  <div className="flex items-center gap-1.5 shrink-0">
                    {m.is_free && (
                      <span className="text-[9px] font-bold uppercase tracking-wider text-emerald-700 bg-emerald-100 px-1.5 py-0.5 rounded">
                        Free
                      </span>
                    )}
                    {m.supports_tools && (
                      <span className="text-[9px] font-bold uppercase tracking-wider text-violet-700 bg-violet-100 px-1.5 py-0.5 rounded">
                        Tools
                      </span>
                    )}
                  </div>
                </div>
                <div className="mt-0.5 flex items-center gap-3 text-xs text-slate-500">
                  <span className="truncate">{m.name}</span>
                  <span className="shrink-0">ctx {formatContext(m.context_length)}</span>
                  <span className="shrink-0">~{formatPrice(m.est_cost_per_turn_usd)}/turn</span>
                </div>
              </button>
            );
          })}
        </div>
      ) : null}

      {refreshedAt && !showFallback && browseOpen && (
        <div className="text-xs text-slate-400">
          Catalog refreshed {new Date(refreshedAt).toLocaleString()} · cached 1 h
        </div>
      )}
    </div>
  );
}
