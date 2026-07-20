/**
 * providerCatalog — curated list of LLM models the AI provider picker
 * surfaces by default.
 *
 * 2026-05-26 — STUB. The full catalog (with hand-curated entries for
 * OpenAI / Anthropic / OpenRouter etc.) went missing from disk
 * mid-session, blocking the dev server with:
 *
 *   Failed to resolve import "../../data/providerCatalog"
 *
 * Both AIProviderForm and CuratedModelPicker import from here. To
 * unblock the rest of the app, this stub returns an empty catalog
 * for every provider — CuratedModelPicker already handles that case
 * by falling back to a free-text model-id input (see
 * `if (catalog.length === 0)` branch around line 64 of
 * CuratedModelPicker.tsx).
 *
 * TODO: Restore the real catalog with the actual recommended-model
 * lists per provider.
 */

export interface CuratedModel {
  /** Provider-native model id (e.g. "gpt-4o-mini", "claude-3-7-sonnet"). */
  id: string;
  /** Human-friendly display name. */
  name: string;
  /** Whether the model supports tool/function calling. */
  supports_tools: boolean;
  /** Whether F-Pulse marks this model as a recommended default. */
  is_recommended: boolean;
  /** Context window in tokens. */
  context_length: number;
  /** Estimated USD spend per typical agent turn (4-tool roundtrip). */
  est_cost_per_turn_usd: number;
  /** Optional short note shown under the model name. */
  hint?: string;
}

/**
 * Returns the curated model catalog for the given provider. Stubbed
 * to an empty array — the picker degrades to free-text input.
 *
 * @param provider Provider identifier (e.g. "openai", "anthropic").
 */
export function getCuratedCatalog(_provider: string): CuratedModel[] {
  return [];
}
