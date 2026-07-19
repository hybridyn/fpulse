/**
 * CuratedModelPicker — provider-agnostic model picker for providers that
 * don't have a public, key-free model catalog (Claude / OpenAI / Gemini /
 * DeepSeek / Groq / Mistral). Reads the curated list from
 * `frontend/src/data/providerCatalog.ts`.
 *
 * UX mirrors OpenRouterModelPicker:
 *   - Selected-model summary chip by default (collapsed)
 *   - "Browse models" / "Change model" expands the list
 *   - Selecting a model auto-collapses back to the summary chip
 *   - Free / Tools badges, ctx + ~$/turn metadata
 *
 * Empty catalog → falls back to a free-text input so the user can still
 * type a model id manually (catches edge cases where the provider just
 * shipped a new model that's not in the curated list yet).
 */

import { useMemo, useState } from 'react';
import { CuratedModel, getCuratedCatalog } from '../../data/providerCatalog';
import { navigateTo } from '../../router';

interface Props {
  provider: string;
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

export default function CuratedModelPicker({ provider, value, onChange, disabled }: Props) {
  const catalog = useMemo(() => getCuratedCatalog(provider) || [], [provider]);
  const [browseOpen, setBrowseOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [toolsOnly, setToolsOnly] = useState(false);

  const selectedMeta = useMemo<CuratedModel | null>(
    () => catalog.find((m) => m.id === value) || null,
    [catalog, value],
  );

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return catalog.filter((m) => {
      if (toolsOnly && !m.supports_tools) return false;
      if (!q) return true;
      return m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q);
    });
  }, [catalog, search, toolsOnly]);

  // Empty catalog (provider without a static list) → free-text input
  if (catalog.length === 0) {
    return (
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        placeholder="model-id"
        className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-violet-300 focus:border-violet-300 outline-none disabled:bg-slate-50 font-mono"
      />
    );
  }

  return (
    <div className="space-y-2">
      {/* Collapsed summary chip — default state. Click to expand the list. */}
      {!browseOpen && (
        <div className="flex items-center justify-between gap-3 px-3 py-2.5 rounded-lg border border-slate-200 bg-white">
          <div className="flex items-center gap-2 min-w-0 flex-1">
            <code className="text-[12px] font-mono font-semibold text-slate-800 truncate">
              {value || '(no model selected)'}
            </code>
            {selectedMeta?.is_recommended && (
              <span className="text-[9px] font-bold uppercase tracking-wider text-amber-700 bg-amber-100 px-1.5 py-0.5 rounded shrink-0">
                Recommended
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

      {/* Browse mode — search + tools filter + list. The catalog is hardcoded
          so this list is instant; no loading state needed. */}
      {browseOpen && (
        <>
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <span className="text-xs font-semibold text-slate-700">
              Browse {catalog.length} {provider} model{catalog.length === 1 ? '' : 's'}
            </span>
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
              placeholder="Search…"
              disabled={disabled}
              className="flex-1 min-w-[160px] px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg focus:ring-2 focus:ring-violet-300 focus:border-violet-300 outline-none disabled:bg-slate-50"
            />
            <label className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-700 select-none cursor-pointer">
              <input
                type="checkbox"
                checked={toolsOnly}
                onChange={(e) => setToolsOnly(e.target.checked)}
                disabled={disabled}
                className="w-3.5 h-3.5"
              />
              Tool-capable only
            </label>
          </div>

          <div className="border border-slate-200 rounded-lg max-h-72 overflow-y-auto bg-white">
            {filtered.length === 0 && (
              <div className="p-3 text-xs text-slate-500">
                No models match. Clear filters or type the id manually:
                <input
                  type="text"
                  value={value}
                  onChange={(e) => onChange(e.target.value)}
                  disabled={disabled}
                  placeholder="model-id"
                  className="mt-2 w-full px-2 py-1.5 text-xs border border-slate-200 rounded font-mono"
                />
              </div>
            )}
            {filtered.map((m) => {
              const selected = m.id === value;
              return (
                <button
                  key={m.id}
                  type="button"
                  onClick={() => {
                    onChange(m.id);
                    setBrowseOpen(false);
                  }}
                  disabled={disabled}
                  className={`w-full text-left px-3 py-2 border-b border-slate-100 last:border-b-0 transition ${
                    selected ? 'bg-violet-50' : 'hover:bg-slate-50'
                  } disabled:opacity-50`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <code className="text-[12px] font-mono text-slate-800 truncate">{m.id}</code>
                    <div className="flex items-center gap-1.5 shrink-0">
                      {m.is_recommended && (
                        <span className="text-[9px] font-bold uppercase tracking-wider text-amber-700 bg-amber-100 px-1.5 py-0.5 rounded">
                          Recommended
                        </span>
                      )}
                      {m.supports_tools && (
                        <span className="text-[9px] font-bold uppercase tracking-wider text-violet-700 bg-violet-100 px-1.5 py-0.5 rounded">
                          Tools
                        </span>
                      )}
                      {!m.supports_tools && (
                        <span className="text-[9px] font-bold uppercase tracking-wider text-slate-500 bg-slate-100 px-1.5 py-0.5 rounded" title="Model accepts text only — agent will run in chat-only mode">
                          Text only
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="mt-0.5 flex items-center gap-3 text-xs text-slate-500">
                    <span className="truncate font-medium text-slate-600">{m.name}</span>
                    <span className="shrink-0">ctx {formatContext(m.context_length)}</span>
                    <span className="shrink-0">~{formatPrice(m.est_cost_per_turn_usd)}/turn</span>
                  </div>
                  {m.hint && (
                    <div className="mt-0.5 text-xs text-slate-400 italic truncate">{m.hint}</div>
                  )}
                </button>
              );
            })}
          </div>
          <div className="text-xs text-slate-400">
            Pricing is approximate · see <a className="underline" href="#help" onClick={(e) => { e.preventDefault(); try { sessionStorage.setItem('fpulse_help_target_guide', 'Costs, providers'); } catch { /* ignore */ } navigateTo('help'); }}>Help → Costs, providers & rate limits</a> for the source of truth
          </div>
        </>
      )}
    </div>
  );
}
