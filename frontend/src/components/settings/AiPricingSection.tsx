/**
 * Settings → AI Pricing — editable per-workspace AI cost-rate table.
 *
 * Rates apply to the Est. Cost tile on Insights → Activity. The same
 * resolver (provider+model → input/cached/output rate) lives on the backend
 * at fpulse/ai/cost_rates.py so any compute server can recover the same
 * USD cost from a stored agent trace.
 *
 * Saves emit `fpulse-settings-changed` so ActivityPage refetches and the
 * tile updates without a page reload.
 *
 * Cached-input rates are forward-compatible: the OSS trace store doesn't
 * count cached tokens separately yet, so cached contributes $0 in real
 * cost today — but the Cost Simulator models the savings, and the field
 * lands the data shape ready for when provider clients report it.
 */

import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api/client';
import { uiAlert, uiConfirm } from '../../ui/dialog';
import { computeCostUsd, type CostRate, type CostRates } from '../../lib/cost';

interface AiPricingSectionProps {
  dark: boolean;
}

interface RatesResponse {
  workspace_id: string;
  rates: CostRates;
  defaults: CostRates;
}

function emptyRate(): CostRate {
  return { input_per_mtok: 0, cached_input_per_mtok: 0, output_per_mtok: 0 };
}

// Tiny help-icon with native tooltip. Native title is the simplest path
// that works across browsers without dependency on a tooltip library.
function HelpIcon({ tip, dark }: { tip: string; dark: boolean }) {
  return (
    <span
      title={tip}
      className={`inline-flex items-center justify-center w-3.5 h-3.5 rounded-full text-xs cursor-help select-none ${
        dark ? 'bg-slate-700 text-slate-300' : 'bg-slate-200 text-slate-600'
      }`}
      aria-label={tip}
    >
      <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10" />
        <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
    </span>
  );
}

function RateInput({
  dark,
  value,
  readOnly,
  onChange,
  title,
}: {
  dark: boolean;
  value: number;
  readOnly?: boolean;
  onChange: (next: number) => void;
  title: string;
}) {
  const invalid = !Number.isFinite(value) || value < 0 || value > 1000;
  return (
    <input
      type="number"
      step="0.01"
      min="0"
      max="1000"
      value={Number.isFinite(value) ? value : 0}
      onChange={(e) => {
        const v = parseFloat(e.target.value);
        onChange(Number.isFinite(v) ? v : 0);
      }}
      readOnly={readOnly}
      title={title}
      className={`px-2 py-1.5 text-sm font-mono rounded-md ring-1 outline-none focus:ring-2 w-full ${
        invalid
          ? (dark ? 'bg-red-500/10 text-red-300 ring-red-500/40 focus:ring-red-400/60' : 'bg-red-50 text-red-700 ring-red-300 focus:ring-red-400')
          : (dark ? 'bg-[#0f1726] text-slate-200 ring-white/[0.08] focus:ring-amber-400/60' : 'bg-white text-slate-700 ring-slate-200 focus:ring-amber-400')
      } ${readOnly ? 'opacity-60 cursor-not-allowed' : ''}`}
    />
  );
}

function RateRow({
  dark,
  name,
  rate,
  onChange,
  onRemove,
  readOnly,
  hint,
}: {
  dark: boolean;
  name: string;
  rate: CostRate;
  onChange: (next: CostRate) => void;
  onRemove?: () => void;
  readOnly?: boolean;
  hint?: string;
}) {
  return (
    <div className={`grid grid-cols-[1fr_110px_110px_110px_auto] gap-2 items-center py-2 transition-colors ${dark ? 'border-white/[0.04] hover:bg-white/[0.02]' : 'border-slate-100 hover:bg-slate-50/60'} border-b last:border-b-0`}>
      <div className="min-w-0">
        <code className={`font-mono text-sm truncate block ${dark ? 'text-slate-200' : 'text-slate-700'}`}>{name}</code>
        {hint && <div className={`text-xs ${dark ? 'text-slate-500' : 'text-slate-500'}`}>{hint}</div>}
      </div>
      <RateInput
        dark={dark}
        value={rate.input_per_mtok}
        readOnly={readOnly}
        onChange={(v) => onChange({ ...rate, input_per_mtok: v })}
        title="USD per 1M input tokens"
      />
      <RateInput
        dark={dark}
        value={rate.cached_input_per_mtok}
        readOnly={readOnly}
        onChange={(v) => onChange({ ...rate, cached_input_per_mtok: v })}
        title="USD per 1M cached-input tokens (prompt cache hits)"
      />
      <RateInput
        dark={dark}
        value={rate.output_per_mtok}
        readOnly={readOnly}
        onChange={(v) => onChange({ ...rate, output_per_mtok: v })}
        title="USD per 1M output tokens"
      />
      <div className="w-7 text-right">
        {onRemove && !readOnly && (
          <button
            type="button"
            onClick={onRemove}
            className={`text-xs px-1.5 py-0.5 rounded ${dark ? 'text-slate-500 hover:text-red-300' : 'text-slate-400 hover:text-red-600'}`}
            aria-label={`Remove ${name}`}
            title="Remove this entry"
          >
            ✕
          </button>
        )}
      </div>
    </div>
  );
}

function CostSimulator({ dark, rates }: { dark: boolean; rates: CostRates }) {
  // Build a single combined picker: provider defaults + per-model overrides.
  // Avoids the two-dropdown ceremony of the previous version — users pick
  // one thing, the cost calc resolves it via the rate table.
  const providerOptions = Object.keys(rates.providers || {}).filter((p) => p !== 'ollama');
  const modelOptions = Object.keys(rates.models || {});

  const [selection, setSelection] = useState<string>(() => {
    if (providerOptions.includes('anthropic')) return 'provider:anthropic';
    if (providerOptions[0]) return `provider:${providerOptions[0]}`;
    return '';
  });
  const [requestsPerDay, setRequestsPerDay] = useState(200);
  const [avgInTokens, setAvgInTokens] = useState(1300);
  const [avgOutTokens, setAvgOutTokens] = useState(800);

  const cost = useMemo(() => {
    const [kind, name] = selection.split(':');
    const provider = kind === 'provider' ? name : null;
    const model = kind === 'model' ? name : null;
    const perReq = computeCostUsd(rates, provider, model, avgInTokens, avgOutTokens);
    const perDay = perReq * Math.max(0, requestsPerDay);
    const perMonth = perDay * 30;
    return { perDay, perMonth };
  }, [rates, selection, requestsPerDay, avgInTokens, avgOutTokens]);

  const labelCls = `text-xs font-medium ${dark ? 'text-slate-400' : 'text-slate-500'}`;
  const inputCls = `mt-1 w-full px-2 py-1.5 text-sm font-mono rounded-md ring-1 outline-none focus:ring-2 ${
    dark ? 'bg-[#0f1726] text-slate-200 ring-white/[0.08] focus:ring-amber-400/60' : 'bg-white text-slate-700 ring-slate-200 focus:ring-amber-400'
  }`;

  return (
    <div className={`rounded-lg border shadow-sm p-3 ${dark ? 'bg-[#0d1322] border-white/[0.06]' : 'bg-slate-50/80 border-slate-200'}`}>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        <div>
          <label className={labelCls}>Model / provider</label>
          <select value={selection} onChange={(e) => setSelection(e.target.value)} className={inputCls}>
            <optgroup label="Provider defaults">
              {providerOptions.map((p) => <option key={`p-${p}`} value={`provider:${p}`}>{p}</option>)}
            </optgroup>
            {modelOptions.length > 0 && (
              <optgroup label="Per-model rates">
                {modelOptions.map((m) => <option key={`m-${m}`} value={`model:${m}`}>{m}</option>)}
              </optgroup>
            )}
          </select>
        </div>
        <div>
          <label className={labelCls}>Requests / day</label>
          <input type="number" min="0" step="1" value={requestsPerDay} onChange={(e) => setRequestsPerDay(parseInt(e.target.value || '0', 10))} className={inputCls} />
        </div>
        <div>
          <label className={labelCls}>Input tokens / req</label>
          <input type="number" min="0" step="50" value={avgInTokens} onChange={(e) => setAvgInTokens(parseInt(e.target.value || '0', 10))} className={inputCls} />
        </div>
        <div>
          <label className={labelCls}>Output tokens / req</label>
          <input type="number" min="0" step="50" value={avgOutTokens} onChange={(e) => setAvgOutTokens(parseInt(e.target.value || '0', 10))} className={inputCls} />
        </div>
      </div>
      <div className={`flex items-baseline justify-between gap-3 pt-3 mt-3 border-t ${dark ? 'border-white/[0.06]' : 'border-slate-200'}`}>
        <div className="flex items-baseline gap-2">
          <span className={`text-xs uppercase tracking-wider ${dark ? 'text-slate-500' : 'text-slate-500'}`}>Estimated</span>
          <span className={`text-2xl font-bold tabular-nums ${dark ? 'text-slate-100' : 'text-slate-800'}`}>${cost.perMonth.toFixed(2)}</span>
          <span className={`text-sm ${dark ? 'text-slate-400' : 'text-slate-500'}`}>/ month</span>
        </div>
        <span className={`text-xs tabular-nums ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
          ≈ ${cost.perDay.toFixed(2)} / day · 30-day estimate
        </span>
      </div>
    </div>
  );
}

// Infer the provider prefix from a model name so we can group the per-model
// list visually. Heuristic only — users can name a model anything.
function inferProvider(modelName: string): string {
  const m = modelName.toLowerCase();
  if (m.startsWith('claude') || m.includes('anthropic')) return 'anthropic';
  if (m.startsWith('gpt') || m.startsWith('o1') || m.startsWith('o3')) return 'openai';
  if (m.includes('llama') || m.includes('qwen') || m.includes('mistral')) return 'ollama';
  return 'other';
}

export default function AiPricingSection({ dark }: AiPricingSectionProps) {
  const [draft, setDraft] = useState<CostRates | null>(null);
  const [lastSaved, setLastSaved] = useState<CostRates | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [newModelName, setNewModelName] = useState('');
  const [newProviderName, setNewProviderName] = useState('');
  const [modelSearch, setModelSearch] = useState('');

  useEffect(() => {
    let cancelled = false;
    api.get<RatesResponse>('/api/v1/ai/cost-rates')
      .then((r) => {
        if (!cancelled && r) {
          setDraft(r.rates);
          setLastSaved(r.rates);
        }
      })
      .catch(() => {
        /* leave nulls; render shows loading state */
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  const isDirty = useMemo(() => {
    if (!draft || !lastSaved) return false;
    return JSON.stringify(draft) !== JSON.stringify(lastSaved);
  }, [draft, lastSaved]);

  async function handleSave() {
    if (!draft) return;
    setSaving(true);
    try {
      const updated = await api.put<RatesResponse>('/api/v1/ai/cost-rates', {
        providers: draft.providers,
        models: draft.models,
        fallback: draft.fallback,
      });
      if (updated?.rates) {
        setDraft(updated.rates);
        setLastSaved(updated.rates);
        try { window.dispatchEvent(new Event('fpulse-settings-changed')); } catch { /* noop */ }
        await uiAlert('AI pricing saved. Activity tile now uses the new rates.');
      }
    } catch (e) {
      await uiAlert(`Failed to save: ${e instanceof Error ? e.message : 'unknown error'}`);
    } finally {
      setSaving(false);
    }
  }

  function handleDiscard() {
    if (lastSaved) setDraft(lastSaved);
  }

  async function handleReset() {
    const ok = await uiConfirm('Reset all AI pricing rates to defaults? Your overrides will be discarded.');
    if (!ok) return;
    setSaving(true);
    try {
      const reset = await api.delete<RatesResponse>('/api/v1/ai/cost-rates');
      if (reset?.rates) {
        setDraft(reset.rates);
        setLastSaved(reset.rates);
        try { window.dispatchEvent(new Event('fpulse-settings-changed')); } catch { /* noop */ }
        await uiAlert('Rates reset to defaults.');
      }
    } catch (e) {
      await uiAlert(`Failed to reset: ${e instanceof Error ? e.message : 'unknown error'}`);
    } finally {
      setSaving(false);
    }
  }

  if (loading || !draft) {
    return (
      <div className={`rounded-lg border shadow-sm px-4 py-3 ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
        <p className={`text-xs italic ${dark ? 'text-slate-500' : 'text-slate-400'}`}>Loading rate table…</p>
      </div>
    );
  }

  const providerEntries = Object.entries(draft.providers);

  // Filter + group per-model overrides by inferred provider so dense lists stay browsable.
  const filteredModelEntries = Object.entries(draft.models).filter(([name]) => {
    if (!modelSearch.trim()) return true;
    return name.toLowerCase().includes(modelSearch.trim().toLowerCase());
  });
  const modelGroups = filteredModelEntries.reduce((acc, [name, rate]) => {
    const grp = inferProvider(name);
    if (!acc[grp]) acc[grp] = [];
    acc[grp].push([name, rate]);
    return acc;
  }, {} as Record<string, [string, CostRate][]>);

  return (
    <div className={`rounded-lg border shadow-sm px-4 py-3 space-y-5 ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
      <p className={`text-xs leading-relaxed ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
        Rates apply to the <strong className={dark ? 'text-slate-300' : 'text-slate-700'}>Est. Cost</strong> tile on{' '}
        <strong className={dark ? 'text-slate-300' : 'text-slate-700'}>Insights → Activity</strong>. Set USD per 1M tokens, split by input,
        cached input, and output. Ollama / local inference is always free regardless of these numbers.
        {' '}<HelpIcon dark={dark} tip="Cached input = prompt-cache hits, charged at a lower rate by most cloud providers (Anthropic ≈ 10% of input, OpenAI ≈ 50%). OSS run history doesn't separate cached vs uncached tokens yet, so the cached rate currently affects only the Cost Simulator below." />
      </p>

      {/* Header row — labels matching the user's clarity ask: "($ / 1M tokens)" */}
      <div className={`grid grid-cols-[1fr_110px_110px_110px_auto] gap-2 text-xs font-bold uppercase tracking-wider pb-1 ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
        <div>Entry</div>
        <div>Input ($ / 1M)</div>
        <div className="flex items-center gap-1">
          Cached ($ / 1M)
          <HelpIcon dark={dark} tip="Prompt-cache hit rate. Lower than the full input rate for providers that support prompt caching." />
        </div>
        <div>Output ($ / 1M)</div>
        <div className="w-7" />
      </div>

      {/* Providers — user-extensible. The defaults seed a few common ones,
          but the space is large (Gemini, DeepSeek, Groq, Mistral, Together,
          Fireworks, Cohere, Azure, …), so add whichever you actually use. */}
      <div>
        <div className={`text-xs font-semibold uppercase tracking-wider mb-1 flex items-center gap-1 ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
          Providers
          <HelpIcon dark={dark} tip="Add any provider you use — the list isn't fixed. The name should match what you set in Insights → AI Provider (e.g. gemini, deepseek, groq, mistral). Unknown providers fall back to Default pricing until you add a rate here." />
        </div>
        {providerEntries.map(([name, rate]) => (
          <RateRow
            key={name}
            dark={dark}
            name={name}
            rate={rate}
            hint={rate.label}
            readOnly={name === 'ollama'}
            onChange={(next) => setDraft({ ...draft, providers: { ...draft.providers, [name]: next } })}
            onRemove={() => {
              const { [name]: _removed, ...rest } = draft.providers;
              void _removed;
              setDraft({ ...draft, providers: rest });
            }}
          />
        ))}
        <div className="flex items-center gap-2 mt-2">
          <input
            type="text"
            value={newProviderName}
            onChange={(e) => setNewProviderName(e.target.value)}
            placeholder="Add a provider (e.g. gemini, deepseek, groq, mistral, together)"
            className={`flex-1 px-2 py-1.5 text-sm rounded-md ring-1 outline-none focus:ring-2 ${
              dark
                ? 'bg-[#0f1726] text-slate-200 ring-white/[0.08] focus:ring-amber-400/60 placeholder:text-slate-500'
                : 'bg-white text-slate-700 ring-slate-200 focus:ring-amber-400 placeholder:text-slate-400'
            }`}
          />
          <button
            type="button"
            disabled={!newProviderName.trim() || newProviderName.trim().toLowerCase() in draft.providers}
            onClick={() => {
              const name = newProviderName.trim().toLowerCase();
              setDraft({ ...draft, providers: { ...draft.providers, [name]: emptyRate() } });
              setNewProviderName('');
            }}
            className="px-3 py-1.5 text-xs font-semibold rounded-md bg-pipe-500 text-white hover:bg-pipe-600 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Add provider
          </button>
        </div>
      </div>

      {/* Per-model overrides — grouped by inferred provider, with search */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <div className={`text-xs font-semibold uppercase tracking-wider ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
            Per-model overrides
            <HelpIcon dark={dark} tip="Per-model rates take precedence over the provider default. Use these for premium tiers (Opus, GPT-4o) where the provider average isn't accurate." />
          </div>
          <div className="flex items-center gap-1.5">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={dark ? 'text-slate-500' : 'text-slate-400'}>
              <circle cx="11" cy="11" r="7" />
              <line x1="21" y1="21" x2="16.65" y2="16.65" />
            </svg>
            <input
              type="text"
              value={modelSearch}
              onChange={(e) => setModelSearch(e.target.value)}
              placeholder="Search models…"
              className={`px-2 py-1 text-xs rounded-md ring-1 outline-none focus:ring-2 w-44 ${
                dark
                  ? 'bg-[#0f1726] text-slate-200 ring-white/[0.08] focus:ring-amber-400/60 placeholder:text-slate-500'
                  : 'bg-white text-slate-700 ring-slate-200 focus:ring-amber-400 placeholder:text-slate-400'
              }`}
            />
          </div>
        </div>
        {filteredModelEntries.length === 0 && (
          <p className={`text-xs italic ${dark ? 'text-slate-500' : 'text-slate-400'}`}>
            {modelSearch ? `No models match "${modelSearch}"` : 'No model-specific rates configured.'}
          </p>
        )}
        {Object.entries(modelGroups).map(([groupName, entries]) => (
          <div key={groupName} className="mt-2">
            <div className={`text-xs font-bold uppercase tracking-wider pl-1 pb-0.5 ${dark ? 'text-slate-600' : 'text-slate-400'}`}>
              {groupName === 'other' ? 'Other / custom' : groupName}
            </div>
            {entries.map(([name, rate]) => (
              <RateRow
                key={name}
                dark={dark}
                name={name}
                rate={rate}
                hint={rate.label}
                onChange={(next) => setDraft({ ...draft, models: { ...draft.models, [name]: next } })}
                onRemove={() => {
                  const { [name]: _removed, ...rest } = draft.models;
                  void _removed;
                  setDraft({ ...draft, models: rest });
                }}
              />
            ))}
          </div>
        ))}
        <div className="flex items-center gap-2 mt-2">
          <input
            type="text"
            value={newModelName}
            onChange={(e) => setNewModelName(e.target.value)}
            placeholder="Add model name (e.g. claude-haiku-4-5)"
            className={`flex-1 px-2 py-1.5 text-sm rounded-md ring-1 outline-none focus:ring-2 ${
              dark
                ? 'bg-[#0f1726] text-slate-200 ring-white/[0.08] focus:ring-amber-400/60 placeholder:text-slate-500'
                : 'bg-white text-slate-700 ring-slate-200 focus:ring-amber-400 placeholder:text-slate-400'
            }`}
          />
          <button
            type="button"
            disabled={!newModelName.trim() || newModelName.trim() in draft.models}
            onClick={() => {
              const name = newModelName.trim();
              setDraft({ ...draft, models: { ...draft.models, [name]: emptyRate() } });
              setNewModelName('');
            }}
            className="px-3 py-1.5 text-xs font-semibold rounded-md bg-pipe-500 text-white hover:bg-pipe-600 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Add model
          </button>
        </div>
      </div>

      {/* Default pricing (renamed from "Fallback") */}
      <div>
        <div className={`text-xs font-semibold uppercase tracking-wider mb-1 ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
          Default pricing
          <HelpIcon dark={dark} tip="Used when a request's provider AND model both fall outside the tables above. Acts as a safety net rather than a hard error." />
        </div>
        <RateRow
          dark={dark}
          name="default"
          rate={draft.fallback}
          hint={draft.fallback.label}
          onChange={(next) => setDraft({ ...draft, fallback: next })}
        />
      </div>

      {/* Cost Simulator */}
      <CostSimulator dark={dark} rates={draft} />

      {/* Sticky save bar — only appears when there are unsaved changes.
          Position-sticky keeps it visible while scrolling the rate editor. */}
      {isDirty && (
        <div
          className={`sticky bottom-0 -mx-4 -mb-3 px-4 py-3 mt-4 flex items-center justify-between gap-3 border-t-2 shadow-lg backdrop-blur ${
            dark ? 'bg-amber-500/15 border-amber-400/40 text-amber-100' : 'bg-amber-50 border-amber-400 text-amber-900'
          }`}
        >
          <div className="flex items-center gap-2 text-sm font-medium">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
            You have unsaved pricing changes.
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handleDiscard}
              disabled={saving}
              className={`px-3 py-1.5 text-sm font-semibold rounded-md ring-1 ${
                dark ? 'ring-amber-400/40 hover:bg-amber-400/10 text-amber-100' : 'ring-amber-400 hover:bg-amber-100 text-amber-900'
              }`}
            >
              Discard
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={saving}
              className="px-4 py-1.5 text-sm font-semibold rounded-md bg-pipe-500 text-white hover:bg-pipe-600 disabled:opacity-50"
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      )}

      {/* Footer — Reset link, only visible when no dirty banner is showing
          so the two action contexts don't compete. */}
      {!isDirty && (
        <div className={`flex items-center justify-between gap-3 pt-3 border-t ${dark ? 'border-white/[0.06]' : 'border-slate-100'}`}>
          <p className={`text-xs ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
            Defaults sourced from public pricing on 2026-05-19.{' '}
            <button type="button" onClick={handleReset} disabled={saving} className={`underline hover:no-underline ${dark ? 'text-violet-300' : 'text-violet-600'}`}>
              Reset to defaults
            </button>
            .
          </p>
          <span className={`text-xs ${dark ? 'text-slate-600' : 'text-slate-400'}`}>No unsaved changes</span>
        </div>
      )}
    </div>
  );
}
