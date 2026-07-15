/**
 * Agent API client — typed wrappers around the /api/ai/agent surface.
 *
 * V8/V9 follow-up rounds 1 + 2 (2026-05-26): all JSON endpoints
 * (`runAgent`, `getAgentStatus`, `listAgentTraces`, `getAgentTrace`,
 * `getAgentBudget`, `getAgentSuggestions`, `postAgentAction`,
 * `refreshAgentProvider`) now route through `apiRequest` from
 * `./client.ts` so they share the global 401 handler, backend-
 * reachable signal, and standard error shape.
 *
 * The streaming endpoint (`runAgentStream`) stays on inline `fetch`
 * because `apiRequest` returns parsed JSON and can't surface an SSE
 * body. `_headers()` is preserved for that one call site only.
 *
 * Backend contract: backend/fpulse/api/agent.py
 */
import { apiRequest } from './client';

export type AgentOutcome =
  | 'success'
  | 'llm_failure'
  | 'tool_failure'
  | 'policy_block'
  | 'timeout'
  | 'user_rejection';

export interface VisibleItemPayload {
  id: string;
  name?: string;
  status?: string;
  kind?: string;
  meta?: Record<string, string | number | boolean | null>;
}

export interface PageContextPayload {
  page: string;
  visible_ids?: string[];
  selected_ids?: string[];
  filters?: Record<string, unknown>;
  environment?: 'dev' | 'prod';
  /** Rich snapshot of on-screen entities — lets the agent answer page-specific
   *  questions without a tool call to discover screen state. Capped to 50. */
  visible_items?: VisibleItemPayload[];
  /** Page-supplied richer context (active code surface, selected-node summary,
   *  step issues, …). The backend sanitizes + budget-caps this and renders it
   *  as a prompt section — see backend/fpulse/ai/context.py:to_extra_context_block. */
  extra_context?: Record<string, unknown>;
}

export interface CaseFile {
  active_entity: { kind: string; id: string; name: string } | null;
  active_intent: { name: string; missing_slot: string | null } | null;
}

export interface AgentRunRequest {
  user_intent: string;
  page_context: PageContextPayload;
  allow_safe_writes?: boolean;
  max_tokens?: number;
  /** Layer 2 dialogue state — sent on every turn so the backend can
   *  slot-fill short replies and substitute vague references. */
  dialogue_state?: CaseFile;
  /** Layer 3 rolling conversation memory (2026-05-22). Backend caps
   *  recent_turns at 20, summary at 1200 chars. */
  conversation?: {
    recent_turns?: Array<{ role: 'user' | 'assistant'; content: string }>;
    summary?: string;
  };
  /** Reasoning depth (2026-05-22). 'deep' skips fast-lane shortcuts
   *  and widens context budget; cloud handles in ~30-60 s, local
   *  Ollama 2-5 min per turn. Default 'standard'. */
  mode?: 'quick' | 'standard' | 'deep';
}

export interface TraceStep {
  step_id: string;
  tool_name: string;
  tool_tier: 'read' | 'safe_write' | 'high_impact_write' | 'unknown' | 'meta';
  input_hash: string;
  output_hash: string;
  timestamp: string;
  latency_ms: number;
  tokens_in: number;
  tokens_out: number;
  decision_reason: string;
  redactions_applied: Record<string, number>;
  outcome: AgentOutcome;
  policy_rules_fired: string[];
}

export interface CostInfo {
  tokens_in: number;
  tokens_out: number;
  estimated_usd: number;
  provider: string;
  model: string;
}

export interface AgentRunResponse {
  run_id: string;
  final_text: string;
  outcome: AgentOutcome;
  iterations: number;
  elapsed_ms: number;
  steps: TraceStep[];
  tool_results: Array<{ tool: string; result: unknown }>;
  cost: CostInfo;
  no_provider: boolean;
  // True when the answer came from the rule-based fast-lane pre-flight
  // instead of the LLM agent loop. Sub-1 s typical, deterministic. UI uses
  // this to show an "Instant" badge and skip the slow-path reassurance hint.
  instant?: boolean;
  instant_intent?: string;
  /** Router confidence in [0, 1]. Only meaningful when `instant` is true. */
  instant_confidence?: number;
  /** True when answered purely from the page's visible_items snapshot —
   *  zero backend I/O. Lets the UI add a "from this page" affordance. */
  served_from_page?: boolean;
  /** Updated dialogue state to persist client-side. Frontend writes this
   *  back into sessionStorage and includes it on the next request. */
  case_file?: CaseFile;
}

export interface AgentStatus {
  available: boolean;
  provider: string;
  model: string;
  tool_count: number;
}

/**
 * Build the full header set for the *streaming* endpoint and the
 * non-migrated JSON endpoints below. The migrated endpoints
 * (`runAgent`, `getAgentStatus`) get the auth + workspace headers
 * from `apiRequest` directly and only pass the AI-safety header
 * via `aiSafetyHeader()` below.
 */
function _headers(): Record<string, string> {
  const token = localStorage.getItem('fpulse_token') || '';
  const workspaceId = localStorage.getItem('fpulse_workspace_id') || 'default';
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Workspace-Id': workspaceId,
  };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  Object.assign(headers, aiSafetyHeader());
  return headers;
}

/**
 * Just the AI-safety header. When AI safety mode is on (Settings →
 * General → AI Assistant), backend blocks write tools
 * (apply_pipeline_draft, draft_alert_rule) even if the user's role
 * permits them and allow_safe_writes=true on the request body.
 *
 * Header-based so we don't have to thread the flag through every
 * endpoint call site. Best-effort read — missing / corrupt
 * localStorage falls back to "off" (returns an empty object).
 */
function aiSafetyHeader(): Record<string, string> {
  try {
    const raw = localStorage.getItem('fpulse-settings');
    if (raw) {
      const parsed = JSON.parse(raw) as { ai?: { safetyMode?: boolean } };
      if (parsed.ai?.safetyMode === true) {
        return { 'X-FPulse-AI-Safety': '1' };
      }
    }
  } catch {
    /* ignore — header simply omitted */
  }
  return {};
}

export async function runAgent(req: AgentRunRequest): Promise<AgentRunResponse> {
  // V8/V9 follow-up — routes through apiRequest so the global 401
  // handler fires on stale tokens and the backend-reachable signal
  // flips on TypeError. Auth + workspace headers come from apiRequest;
  // we only inject the AI-safety header here.
  return apiRequest<AgentRunResponse>('/ai/agent', {
    method: 'POST',
    headers: aiSafetyHeader(),
    body: JSON.stringify(req),
  });
}

/**
 * SSE event shapes emitted by POST /api/ai/agent/stream.
 *
 * Event order (happy path):
 *   run_start  → iteration → tool_start → step  (repeat per iteration)  → final
 * Error path:
 *   run_start? → ... → error
 *
 * EventSource is NOT used here because the endpoint requires a POST body +
 * Authorization header — those aren't supported by the browser's
 * EventSource. We use fetch() with a streaming body reader instead.
 */
export type AgentStreamEvent =
  | { event: 'run_start'; run_id: string; page: string }
  // `max` is the iteration cap for THIS run, resolved per-provider on the
  // backend (3 for local Ollama, 6 for cloud — see _resolve_max_iterations
  // in backend/fpulse/ai/agent.py). Older backends omit it, so it's
  // optional and the UI must fall back to a sane default (6).
  | { event: 'iteration'; iteration: number; max?: number }
  | { event: 'tool_start'; tool_name: string }
  | { event: 'token'; delta: string }
  | { event: 'step'; step: TraceStep }
  | ({ event: 'final' } & AgentRunResponse)
  | { event: 'error'; error: string };

export interface RunAgentStreamHandlers {
  onEvent: (event: AgentStreamEvent) => void;
  signal?: AbortSignal;
}

export async function runAgentStream(
  req: AgentRunRequest,
  { onEvent, signal }: RunAgentStreamHandlers,
): Promise<void> {
  const res = await fetch('/api/ai/agent/stream', {
    method: 'POST',
    headers: _headers(),
    body: JSON.stringify(req),
    signal,
  });
  // Older backends don't have /agent/stream — fall back to the synchronous
  // endpoint so the UX works against any backend that has /agent.
  if (res.status === 404 || res.status === 405) {
    const sync = await runAgent(req);
    onEvent({ event: 'final', ...sync });
    return;
  }
  if (!res.ok || !res.body) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `Agent stream failed: ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  // SSE frames are delimited by a blank line (\n\n). Lines starting with
  // ":" are comments (used here as keepalive pings). Lines starting with
  // "data: " carry one JSON payload — concatenated across consecutive
  // data lines if the server splits them, though we don't currently do that.
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const dataLines: string[] = [];
      for (const line of frame.split('\n')) {
        if (line.startsWith('data: ')) {
          dataLines.push(line.slice(6));
        }
      }
      if (dataLines.length === 0) continue;
      try {
        const evt = JSON.parse(dataLines.join('\n')) as AgentStreamEvent;
        onEvent(evt);
      } catch {
        // Ignore malformed frame; keep streaming.
      }
    }
  }
}

export async function getAgentStatus(): Promise<AgentStatus> {
  // V8/V9 follow-up — see comment on runAgent for migration rationale.
  return apiRequest<AgentStatus>('/ai/agent/status', {
    headers: aiSafetyHeader(),
  });
}

export interface TraceListEntry {
  run_id: string;
  user_id: string | null;
  workspace_id: string;
  page: string;
  user_intent: string;
  outcome: AgentOutcome;
  iterations: number;
  total_tokens_in: number;
  total_tokens_out: number;
  elapsed_ms: number;
  step_count: number;
  created_at: string;
}

export interface TraceDetail extends TraceListEntry {
  final_text: string;
  steps: TraceStep[];
  tool_results: Array<{ tool: string; result: unknown }>;
}

export async function listAgentTraces(limit: number = 20): Promise<TraceListEntry[]> {
  const body = await apiRequest<{ traces?: TraceListEntry[] }>(
    `/ai/agent/traces?limit=${encodeURIComponent(String(limit))}`,
    { headers: aiSafetyHeader() },
  );
  return body.traces || [];
}

export async function getAgentTrace(runId: string): Promise<TraceDetail> {
  return apiRequest<TraceDetail>(
    `/ai/agent/trace/${encodeURIComponent(runId)}`,
    { headers: aiSafetyHeader() },
  );
}

export interface BudgetScope {
  tokens_used_today: number;
  tokens_cap: number;
  percent_used: number;
  request_count_today: number;
  cost_usd_today: number;
}

export interface AgentBudget {
  user: BudgetScope & { user_id: string | null };
  workspace: BudgetScope & { workspace_id: string };
  rate_limit_per_minute: number;
}

export async function getAgentBudget(): Promise<AgentBudget> {
  return apiRequest<AgentBudget>('/ai/agent/budget', { headers: aiSafetyHeader() });
}

/**
 * Tell the backend to invalidate its provider autoprobe cache and re-resolve.
 * Called immediately after Ollama pull/delete so the agent picks up the new
 * model state without waiting up to 5 min for the cache TTL.
 *
 * Returns the freshly-resolved AgentStatus, so callers can use the response
 * directly without a second /agent/status round-trip.
 */
export interface AgentSuggestion {
  label: string;
  prompt: string;
  icon?: string | null;
  category: 'general' | 'diagnose' | 'build' | 'monitor';
}

/**
 * Per-page contextual quick prompts shown in the dock's empty state +
 * Suggestions tab. Cheap deterministic endpoint — safe to call on every
 * page navigation.
 */
export async function getAgentSuggestions(
  page: string,
  selectedCount: number = 0,
): Promise<AgentSuggestion[]> {
  const params = new URLSearchParams({ page, selected_count: String(selectedCount) });
  // Suggestions are a soft enhancement — on any error (404, 5xx,
  // network) we want an empty list, not an exception. Wrap apiRequest
  // in try/catch to preserve the pre-migration behaviour.
  try {
    const body = await apiRequest<{ suggestions?: AgentSuggestion[] }>(
      `/ai/agent/suggestions?${params.toString()}`,
      { headers: aiSafetyHeader() },
    );
    return Array.isArray(body.suggestions) ? body.suggestions : [];
  } catch {
    return [];
  }
}

/**
 * POST /api/ai/agent/action — submit a structured action (chip click).
 * Returns the same AgentRunResponse shape as the regular /agent endpoint
 * so the chat panel renders both identically. NO LLM is invoked server-side.
 */
export interface AgentActionRequest {
  action: Record<string, unknown>;
  page_context: PageContextPayload;
  dialogue_state?: CaseFile;
}

export async function postAgentAction(req: AgentActionRequest): Promise<AgentRunResponse> {
  return apiRequest<AgentRunResponse>('/ai/agent/action', {
    method: 'POST',
    headers: aiSafetyHeader(),
    body: JSON.stringify(req),
  });
}

export async function refreshAgentProvider(): Promise<AgentStatus> {
  return apiRequest<AgentStatus>('/ai/agent/refresh-provider', {
    method: 'POST',
    headers: aiSafetyHeader(),
  });
}

/**
 * Format a cost estimate as the inline indicator string the chat UI shows
 * after every agent response: "~240 tokens · ~$0.0016".
 */
export function formatCost(cost: CostInfo): string {
  const totalTokens = cost.tokens_in + cost.tokens_out;
  const usd = cost.estimated_usd;
  if (usd === 0) {
    return `~${totalTokens.toLocaleString()} tokens`;
  }
  // Use 4 sig figs; trim trailing zeros
  const usdStr = usd < 0.01 ? usd.toFixed(6).replace(/0+$/, '').replace(/\.$/, '') : usd.toFixed(4);
  return `~${totalTokens.toLocaleString()} tokens · ~$${usdStr}`;
}
