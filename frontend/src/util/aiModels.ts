/**
 * Shared AI model metadata. Used by:
 *   - AgentChatPanel (text-only banner detection + below-floor warning)
 *   - AIProviderForm (Settings page badge + dropdown)
 *   - OllamaRecommendationBanner (first-launch suggestion)
 *
 * Keep this list in sync with backend `_TOOL_CAPABLE_PREFIXES` in
 * planner/ai_client.py:_autoprobe_local_ollama. Diverging is a UX bug
 * waiting to happen.
 *
 * 2026-05-19 local-LLM floor revision (memory:
 * project_fpulse_local_llm_floor_revision_2026-05-19): the previous
 * qwen2.5:1.5b / :3b sweet-spot pick FAILED the Editor Phase 1
 * tool-use loop. The reasoning thesis needs a ~7B floor. Recommended
 * picks now: qwen2.5:7b, phi-4, llama3.1:8b (~6 GB RAM Q4_K_M).
 * Anything below that is technically tool-capable (the model
 * advertises tool schemas) but unreliable in practice — the local
 * 1.5b model returns greetings or empty responses when it should
 * call a tool.
 */

export const OLLAMA_TOOL_CAPABLE_PREFIXES = [
  'llama3.1',
  'llama3.2',
  'llama3.3',
  'qwen2.5',
  'mistral-nemo',
  'firefunction',
  'command-r',
  'phi-4',
  'phi4',
] as const;

export function isOllamaToolCapable(modelName: string): boolean {
  if (!modelName) return false;
  // Strip the :tag suffix (e.g. 'llama3.1:8b' → 'llama3.1') for prefix match
  const head = modelName.split(':')[0].toLowerCase();
  return OLLAMA_TOOL_CAPABLE_PREFIXES.some((p) => head === p || head.startsWith(p));
}

/**
 * Models too heavy for CPU-only laptops (>= 14B params). These complete tool
 * turns in 60-300s on CPU which is unusable for an interactive agent.
 *
 * **2026-05-19 update** — qwen2.5:7b is NO LONGER in this list; it's the
 * recommended floor now. Anything below 7B is in the "below floor" bucket
 * (see ``isOllamaBelowToolUseFloor``) which fails for a different reason
 * (can't reliably drive the tool-use loop). The two failure modes are
 * distinct: too-large = times out, below-floor = silently returns garbage
 * or greetings instead of calling tools.
 */
const OLLAMA_TOO_LARGE_FOR_CPU_PATTERN = /^(llama3\.[123]:(70b|72b)|qwen2\.5:(14b|32b|72b)|mixtral|deepseek-r1)/i;

export function isOllamaTooLargeForCPU(modelName: string): boolean {
  if (!modelName) return false;
  const lower = modelName.toLowerCase();
  return OLLAMA_TOO_LARGE_FOR_CPU_PATTERN.test(lower);
}

/**
 * Models technically tool-capable but BELOW the reliable tool-use floor.
 * The 0.5b–3b range advertises tool schemas and the autoprobe lets them
 * through, but in practice they hallucinate tool args, return empty
 * responses, or skip the tool call entirely and reply with a greeting
 * — exactly the failures the 2026-05-21 user report captured.
 *
 * Surface this via a banner + an actionable error message so users
 * don't waste hours wondering why their 1.5b install "doesn't answer."
 */
const OLLAMA_BELOW_TOOL_FLOOR_PATTERN = /^(qwen2\.5:(0\.5b|1\.5b|3b)|llama3\.2:(1b|3b)|phi-?3|gemma2:2b|tinyllama)/i;

export function isOllamaBelowToolUseFloor(modelName: string): boolean {
  if (!modelName) return false;
  return OLLAMA_BELOW_TOOL_FLOOR_PATTERN.test(modelName.toLowerCase());
}

/**
 * Recommended Ollama picks for CPU-bound deployments. First entry is the
 * primary; others are equally-supported alternatives the user can pick
 * based on what their RAM budget allows.
 *
 * **2026-05-19 floor revision** (was `qwen2.5:1.5b` then `qwen2.5:3b`):
 * three independent reviews converged that anything below ~7B can't
 * reliably drive the agent's tool-use loop. ~6 GB RAM (Q4_K_M) is the
 * new floor.
 */
export const OLLAMA_RECOMMENDED_MODELS = [
  'qwen2.5:7b',     // primary — best tool-use reliability at the floor
  'llama3.1:8b',    // strong all-rounder; slightly heavier (~7 GB)
  'phi-4',          // 14B equivalent perf at smaller footprint
] as const;

export const OLLAMA_CPU_RECOMMENDATION = OLLAMA_RECOMMENDED_MODELS[0];
