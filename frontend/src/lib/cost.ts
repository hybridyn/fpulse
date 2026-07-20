/**
 * AI cost-rate helpers — mirror of backend/fpulse/ai/cost_rates.py.
 *
 * Loaded from `/api/v1/ai/cost-rates` so admins can override per-workspace.
 * The DEFAULT_RATES constant below is only a fallback for use before the
 * fetch resolves; the source of truth is the API response.
 *
 * `cached_input_per_mtok` is the prompt-cache hit rate (cheaper than the
 * uncached input rate). The OSS trace store doesn't yet count cached
 * tokens separately, so today `cachedTokensIn` defaults to 0 and the
 * cached rate contributes $0 in real cost — but the Cost Simulator can
 * model the savings, and the field is forward-compatible for when
 * provider clients start reporting the breakdown.
 */

export interface CostRate {
  input_per_mtok: number;
  cached_input_per_mtok: number;
  output_per_mtok: number;
  label?: string;
}

export interface CostRates {
  providers: Record<string, CostRate>;
  models: Record<string, CostRate>;
  fallback: CostRate;
}

export const DEFAULT_RATES: CostRates = {
  providers: {
    ollama:     { input_per_mtok: 0.0, cached_input_per_mtok: 0.0,  output_per_mtok: 0.0,  label: 'Local — no per-token cost' },
    anthropic:  { input_per_mtok: 3.0, cached_input_per_mtok: 0.30, output_per_mtok: 15.0, label: 'Anthropic (Sonnet-tier default)' },
    openai:     { input_per_mtok: 2.5, cached_input_per_mtok: 1.25, output_per_mtok: 10.0, label: 'OpenAI (GPT-4o-tier default)' },
    openrouter: { input_per_mtok: 1.0, cached_input_per_mtok: 0.50, output_per_mtok: 3.0,  label: 'OpenRouter (blended default)' },
  },
  // Per-model overrides — kept in sync with backend/fpulse/ai/cost_rates.py.
  // Without these, a cheap model like gpt-4o-mini falls back to the generic
  // OpenAI provider rate (2.5/10.0), overstating cost ~16x.
  models: {
    'claude-haiku-4-5':  { input_per_mtok: 0.80, cached_input_per_mtok: 0.08,  output_per_mtok: 4.0 },
    'claude-sonnet-4-6': { input_per_mtok: 3.0,  cached_input_per_mtok: 0.30,  output_per_mtok: 15.0 },
    'claude-opus-4-7':   { input_per_mtok: 15.0, cached_input_per_mtok: 1.50,  output_per_mtok: 75.0 },
    'gpt-4o-mini':       { input_per_mtok: 0.15, cached_input_per_mtok: 0.075, output_per_mtok: 0.60 },
    'gpt-4o':            { input_per_mtok: 2.50, cached_input_per_mtok: 1.25,  output_per_mtok: 10.0 },
  },
  fallback: { input_per_mtok: 0.3, cached_input_per_mtok: 0.15, output_per_mtok: 0.6, label: 'Default pricing — applied when neither provider nor model is recognised' },
};

/** Resolve the most-specific rate for (provider, model). */
export function resolveRate(
  rates: CostRates,
  provider: string | null | undefined,
  model: string | null | undefined,
): CostRate {
  if (model) {
    const models = rates.models ?? {};
    // 1. Exact match.
    if (models[model]) return models[model];
    // 2. Strip a provider namespace prefix ("openai/gpt-4o-mini" → "gpt-4o-mini").
    const tail = model.includes('/') ? model.split('/').pop()! : model;
    if (tail !== model && models[tail]) return models[tail];
    // 3. Longest registered key that is a base of the id, so dated variants
    // resolve to their family ("gpt-4o-mini-2024-07-18" → "gpt-4o-mini").
    let best: string | null = null;
    for (const key of Object.keys(models)) {
      if (tail === key || tail.startsWith(key + '-') || tail.startsWith(key + ':')) {
        if (best === null || key.length > best.length) best = key;
      }
    }
    if (best) return models[best];
  }
  if (provider) {
    const p = rates.providers?.[provider.toLowerCase()];
    if (p) return p;
    if (provider.toLowerCase() === 'ollama') {
      return { input_per_mtok: 0, cached_input_per_mtok: 0, output_per_mtok: 0, label: 'Local — no per-token cost' };
    }
  }
  return rates.fallback ?? { input_per_mtok: 0, cached_input_per_mtok: 0, output_per_mtok: 0 };
}

/** USD cost for one agent run. `cachedTokensIn` is optional and defaults to 0. */
export function computeCostUsd(
  rates: CostRates,
  provider: string | null | undefined,
  model: string | null | undefined,
  tokensIn: number,
  tokensOut: number,
  cachedTokensIn = 0,
): number {
  const r = resolveRate(rates, provider, model);
  const uncached = Math.max(0, tokensIn - cachedTokensIn);
  const cached = Math.max(0, cachedTokensIn);
  return (
    (uncached / 1_000_000) * r.input_per_mtok +
    (cached / 1_000_000) * r.cached_input_per_mtok +
    (tokensOut / 1_000_000) * r.output_per_mtok
  );
}
