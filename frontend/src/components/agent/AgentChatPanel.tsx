/**
 * AgentChatPanel — the chat UI for the F-Pulse AI agent (Step 1.5a-frontend).
 *
 * Differs from the existing AIAssistBar floating widget:
 *   AIAssistBar  → POST /api/ai/page-assist  (deterministic per-page text)
 *   AgentChatPanel → POST /api/ai/agent      (real LLM tool-use loop)
 *
 * Both can coexist; this panel is the upgrade path. Wiring it as the
 * default global agent widget is a separate change in App.tsx.
 *
 * What this component does:
 *   - Reads the current page context from usePageContext (frontend hook)
 *   - Posts {user_intent, page_context} to /api/ai/agent
 *   - Renders the agent's final text + tool steps + cost indicator
 *   - Shows ConfirmationCard for any safe-write step (draft confirmation)
 *   - Surfaces no_provider banner when AI isn't configured
 *
 * Trust contract — this component never invents data:
 *   - Final text comes verbatim from the backend response
 *   - Tool result hashes are shown (not raw values) for the trace shape
 *   - "ai_powered" badge is true only when the run actually used the LLM
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AgentBudget,
  AgentRunResponse,
  AgentStatus,
  AgentSuggestion,
  TraceListEntry,
  TraceStep,
  formatCost,
  getAgentBudget,
  getAgentStatus,
  getAgentSuggestions,
  listAgentTraces,
  refreshAgentProvider,
  runAgentStream,
} from '../../api/agent';
import {
  OllamaPullProgress,
  OllamaStatus,
  deleteOllamaModel,
  getOllamaStatus,
  pullOllamaModel,
} from '../../api/ollama';
import { useCurrentPageContext } from '../../hooks/usePageContext';
import { buildPageContextPayload } from './pageContext';
import { useAgentChatStore, type ChatTurn as StoreChatTurn } from '../../hooks/useAgentChatStore';
// CP-P3 (2026-06-16) — apply an AI-drafted pipeline straight onto the canvas.
import { useWorkflowStore } from '../../stores/workflowStore';
import { navigateTo } from '../../router';
import { toast } from '../Toast';
import { isOllamaToolCapable, isOllamaBelowToolUseFloor, OLLAMA_CPU_RECOMMENDATION } from '../../util/aiModels';
import { buildConversationPayload } from '../../util/conversationSummary';
import AIProviderForm from '../ai/AIProviderForm';
import { AgentSegments, parseAgentCards, type AgentAction } from './AgentCard';
import { postAgentAction } from '../../api/agent';

/** User-facing label that appears in the transcript for a clicked chip. */
function _labelForAction(a: AgentAction): string {
  switch (a.kind) {
    case 'slot_fill':
      return a.entity_name;
    case 'fast_action':
      return `${a.verb} "${a.entity_name}"`;
    case 'execute':
      return `Confirm: ${a.method || 'POST'} ${a.endpoint}`;
    case 'navigate':
      return `Open ${a.page}`;
    case 'ask':
      return a.prompt;
    default:
      return '…';
  }
}
import ConfirmationCard from './ConfirmationCard';
import ConfirmDialog from './ConfirmDialog';
import Modal from './Modal';
import ProviderComparison from './ProviderComparison';

// Chat turn type lives in the store so it persists across navigation.
type ChatTurn = StoreChatTurn;

const _outcomeBadge: Record<string, { label: string; tone: string }> = {
  success: { label: 'OK', tone: 'bg-emerald-50 text-emerald-700 ring-emerald-200' },
  llm_failure: { label: 'LLM error', tone: 'bg-red-50 text-red-700 ring-red-200' },
  tool_failure: { label: 'Tool error', tone: 'bg-red-50 text-red-700 ring-red-200' },
  policy_block: { label: 'Blocked', tone: 'bg-amber-50 text-amber-800 ring-amber-200' },
  timeout: { label: 'Timeout', tone: 'bg-amber-50 text-amber-800 ring-amber-200' },
  user_rejection: { label: 'Cancelled', tone: 'bg-slate-100 text-slate-700 ring-slate-200' },
};

// Human-readable status strings shown while the agent is mid-thought.
// Keys match tool names in backend/fpulse/ai/tools/__init__.py (INITIAL_TOOLS).
// The raw tool name is engineering jargon ("list_executions"); users want
// to know what the assistant is actually doing ("Checking recent executions…").
// Falls back to a prettified version of the tool name for any tool not listed.
const TOOL_STATUS_LABELS: Record<string, string> = {
  list_pipelines: 'Listing pipelines…',
  list_projects: 'Reading projects…',
  list_schedules: 'Reading schedules…',
  list_alerts: 'Reading alerts…',
  list_executions: 'Checking recent executions…',
  list_catalog: 'Reading node catalog…',
  list_templates: 'Reading templates…',
  inspect_connections: 'Checking connections…',
  summarize_pipeline: 'Reading pipeline definition…',
  validate_pipeline: 'Validating pipeline…',
  explain_step: 'Examining the step…',
  query_metrics: 'Querying metrics…',
  recall_history: 'Searching past conversations…',
  get_running_executions: "Checking what's running…",
  get_next_scheduled: 'Checking the schedule…',
  get_installation_health: 'Checking installation health…',
  get_user_role: 'Checking your role…',
  get_workspace_overview: 'Reading workspace overview…',
  draft_pipeline_from_intent: 'Drafting pipeline…',
  modify_pipeline_step: 'Editing the step…',
  draft_alert_rule: 'Drafting alert rule…',
  compose_report: 'Composing report…',
  apply_pipeline_draft: 'Applying changes…',
};

function _statusForTool(toolName: string | undefined): string {
  if (!toolName) return 'Thinking…';
  const known = TOOL_STATUS_LABELS[toolName];
  if (known) return known;
  // Unknown tool — prettify snake_case to "Title Case…"
  const pretty = toolName
    .split('_')
    .map((w) => (w.length > 0 ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ');
  return `${pretty}…`;
}

function StepRow({ step }: { step: TraceStep }) {
  const badge = _outcomeBadge[step.outcome] || _outcomeBadge.success;
  const redactionCount = Object.values(step.redactions_applied).reduce((a, b) => a + b, 0);
  return (
    <div className="text-xs flex items-start gap-2 py-1">
      <span className={`shrink-0 px-1.5 py-0.5 rounded ring-1 ${badge.tone} font-semibold uppercase tracking-wider`}>
        {badge.label}
      </span>
      <div className="flex-1 min-w-0">
        <div className="text-slate-800 font-medium">
          {step.tool_name}
          <span className="ml-1 text-slate-400">({step.tool_tier})</span>
        </div>
        <div className="text-slate-500 truncate" title={step.decision_reason}>
          {step.decision_reason}
        </div>
        <div className="text-slate-400 mt-0.5">
          {step.latency_ms} ms
          {redactionCount > 0 && <span className="ml-2">redactions: {redactionCount}</span>}
        </div>
      </div>
    </div>
  );
}

const DEFAULT_PULL_MODEL = 'llama3.1';

// Tool-capable check is shared with AIProviderForm — see util/aiModels.ts
// for the canonical list (kept in sync with backend autoprobe).

interface ModelMeta {
  name: string;
  diskGB: number;
  ramGB: number;     // approx RAM during inference
  notes?: string;
  weight: 'light' | 'medium' | 'heavy' | 'massive';
  tools: boolean;    // Ollama tool-use support — only tool-trained models work for the agent
  recommended?: 'cpu' | 'gpu';  // Auto-recommend default for the runtime
}

// Curated. Disk/RAM are approximate — actual figures vary slightly per quantization.
// `tools: true` means the model is trained for tool-use (function calling).
// Without it the agent runs in text-only mode (still useful, but can't call tools).
// Source: Ollama docs — tool-trained families include Llama 3.1+, Mistral Nemo,
// Qwen 2.5, Firefunction v2, Command-R+.
// 2026-05-19 tool-use floor: small Qwen 2.5 models (0.5b/1.5b/3b) and small
// Llama 3.2 models (1b/3b) advertise tool schemas in the API but in practice
// silently return greetings instead of calling tools when the agent loop
// hands them tools. They're kept in the list (tools: false) so the dropdown
// still shows them with a clear "below tool-use floor" note, rather than
// implying they can drive the agent.
const KNOWN_MODELS: ModelMeta[] = [
  { name: 'qwen2.5:7b',   diskGB: 4.7,  ramGB: 8,  weight: 'medium',  tools: true,  notes: 'Recommended for CPU \u2014 reliable tool-use floor (2026-05-19), ~6 GB RAM at Q4_K_M, 30\u201360 s/turn on CPU.', recommended: 'cpu' },
  { name: 'llama3.1:8b',  diskGB: 4.9,  ramGB: 8,  weight: 'medium',  tools: true,  notes: 'CPU alternative at the floor \u2014 strong tool-use.' },
  { name: 'phi-4',        diskGB: 9.0,  ramGB: 12, weight: 'medium',  tools: true,  notes: 'CPU alternative \u2014 14B equivalent perf; needs more RAM.' },
  { name: 'qwen2.5:14b',  diskGB: 8.4,  ramGB: 16, weight: 'medium',  tools: true,  notes: 'Recommended for GPU \u2014 sub-3-s tool-use on a 12 GB+ GPU.', recommended: 'gpu' },
  { name: 'llama3.1',     diskGB: 4.7,  ramGB: 8,  weight: 'medium',  tools: true,  notes: 'Tool-capable 8B variant.' },
  { name: 'qwen2.5',      diskGB: 4.7,  ramGB: 8,  weight: 'medium',  tools: true,  notes: 'Tool-capable, strong reasoning (7B variant).' },
  { name: 'mistral-nemo', diskGB: 7.1,  ramGB: 10, weight: 'medium',  tools: true,  notes: 'Tool-capable, 12B params.' },
  { name: 'qwen2.5:3b',   diskGB: 1.9,  ramGB: 3,  weight: 'light',   tools: false, notes: 'Below the tool-use floor \u2014 advertises tools but returns greetings instead of calling them.' },
  { name: 'qwen2.5:1.5b', diskGB: 1.0,  ramGB: 2,  weight: 'light',   tools: false, notes: 'Below the tool-use floor \u2014 same silent-failure mode as the 3b.' },
  { name: 'gemma2:2b',    diskGB: 1.6,  ramGB: 3,  weight: 'light',   tools: false, notes: 'Below the tool-use floor \u2014 advertises tools but cannot drive the agent.' },
  { name: 'llama3.2:1b',  diskGB: 0.8,  ramGB: 1.5,weight: 'light',   tools: false, notes: 'Below the tool-use floor \u2014 too small for the agent loop.' },
  { name: 'phi3',         diskGB: 2.3,  ramGB: 4,  weight: 'light',   tools: false, notes: 'Smallest \u2014 text-only, no tools.' },
  { name: 'mistral',      diskGB: 4.1,  ramGB: 6,  weight: 'medium',  tools: false, notes: 'Older \u2014 prefer mistral-nemo for tools.' },
  { name: 'codellama',    diskGB: 3.8,  ramGB: 6,  weight: 'medium',  tools: false, notes: 'Tuned for code; no tool-use.' },
  { name: 'llama3.1:70b', diskGB: 40,   ramGB: 48, weight: 'massive', tools: true,  notes: 'Tool-capable but needs 64+ GB RAM.' },
];

const WEIGHT_STYLES: Record<ModelMeta['weight'], { dot: string; label: string }> = {
  light:   { dot: 'bg-emerald-500', label: 'Light' },
  medium:  { dot: 'bg-amber-500',   label: 'Medium' },
  heavy:   { dot: 'bg-orange-500',  label: 'Heavy' },
  massive: { dot: 'bg-red-600',     label: 'Massive' },
};

function fmtBytes(n: number): string {
  if (!n || n < 1024) return `${n || 0} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 10 ? 0 : 1)} ${units[i]}`;
}

export default function AgentChatPanel() {
  const pageCtx = useCurrentPageContext();
  // Chat history (turns) is a module-level singleton so the conversation
  // persists across page navigation, widget close/reopen, and editor visits.
  const {
    turns,
    setTurns,
    setOpen: setWidgetOpen,
    clear: clearTurns,
    pendingInput,
    clearPendingInput,
    caseFile,
    setCaseFile,
  } = useAgentChatStore();
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [ollamaStatus, setOllamaStatus] = useState<OllamaStatus | null>(null);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  // AbortController for the in-flight runAgentStream fetch. Set on submit,
  // .abort() on Stop button, cleared on completion. Lets the user bail
  // out of a slow local-CPU LLM run without waiting for the wall-clock cap.
  const abortRef = useRef<AbortController | null>(null);

  // Scroll container + sentinel for auto-scroll. The container is the
  // overflow-y-auto wrapper around the chat list; the sentinel is an empty
  // div pinned to the bottom that we scrollIntoView on new turns and during
  // streaming. Keeps the latest message + the live "Working… step N of 6"
  // status visible while the agent is still working.
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const [allowSafeWrites, setAllowSafeWrites] = useState(false);
  // 2026-06-17 — Deep mode now DEFAULTS ON: every turn sends `mode: 'deep'`
  // so the backend SKIPS the rule-based fast lane and always runs the full
  // reasoning agent (LLM + tools + workspace grounding). Per the product
  // owner: properly-analysed answers only, never a deterministic "best guess".
  // Trade-off: slower (cloud ~30-60 s, local CPU 2-5 min/turn) and it needs a
  // tool-capable model — a free/below-floor model will surface an [LLM failure]
  // here instead of falling back to a guess. The choice persists per browser;
  // a user can still untick "Deep" to use the fast lane.
  const [deepMode, setDeepMode] = useState<boolean>(() => {
    try { return localStorage.getItem('fpulse.ai.deepMode') !== 'false'; } catch { return true; }
  });
  const [pendingConfirm, setPendingConfirm] = useState<{
    response: AgentRunResponse;
    safeWriteStep: TraceStep;
  } | null>(null);

  // Handler for clickable cards (choices / next_actions / confirm).
  // POSTs the structured action to /api/ai/agent/action — NO router,
  // NO LLM. Response is appended to chat just like a normal turn.
  const handleAction = useCallback(async (action: AgentAction) => {
    // Navigate is purely client-side — fire the App's onNavigate via
    // localStorage hand-off (existing pattern for cross-component nav)
    // and just acknowledge in chat.
    if (action.kind === 'navigate') {
      const evt = new CustomEvent('fpulse-agent-navigate', { detail: action });
      window.dispatchEvent(evt);
      // If the action carries a follow-up prompt ("then_ask"), the bot
      // is mid-conversation: navigate + auto-continue with the next
      // question on the new page. Wait a beat so the destination page
      // mounts and publishes its visible_items before the router runs.
      const followup = (action as { then_ask?: string }).then_ask;
      if (followup && followup.trim()) {
        // 700ms is enough for the page mount + first data fetch on
        // typical Workflows / Executions / Connections pages. If a
        // page is slow we'll just clarify again, which is harmless.
        setTimeout(() => handleSubmit(followup), 700);
      }
      return;
    }
    // Empty 'ask' (used for "Cancel" / "Keep running" buttons) — just no-op.
    if (action.kind === 'ask' && (!action.prompt || action.prompt.trim() === '')) {
      return;
    }
    // Non-empty 'ask' is a follow-up question chip — re-submit through the
    // normal /agent/stream flow as a fresh user turn so the router can
    // route it cleanly (instead of the action endpoint which only acks).
    if (action.kind === 'ask' && action.prompt) {
      handleSubmit(action.prompt);
      return;
    }

    const userTurnId = `user-action-${Date.now()}`;
    const liveTurnId = `agent-action-${Date.now()}`;
    const userTurn: ChatTurn = {
      id: userTurnId,
      role: 'user',
      text: _labelForAction(action),
    };
    const liveTurn: ChatTurn = {
      id: liveTurnId,
      role: 'agent',
      text: '',
      streaming: true,
      liveStatus: 'Working…',
    };
    setTurns((prev) => [...prev, userTurn, liveTurn]);

    try {
      const resp = await postAgentAction({
        action: action as unknown as Record<string, unknown>,
        page_context: buildPageContextPayload(pageCtx),
        dialogue_state: caseFile,
      });
      if (resp.case_file) setCaseFile(resp.case_file);
      setTurns((prev) =>
        prev.map((t) =>
          t.id === liveTurnId
            ? { ...t, text: resp.final_text, streaming: false, liveStatus: undefined, response: resp }
            : t,
        ),
      );
    } catch (err) {
      setTurns((prev) =>
        prev.map((t) =>
          t.id === liveTurnId
            ? { ...t, text: err instanceof Error ? err.message : 'Action failed.', streaming: false, liveStatus: undefined }
            : t,
        ),
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageCtx, caseFile, setTurns, setCaseFile]);

  // Pull state — tracks active llama3 pull progress
  const [pullProgress, setPullProgress] = useState<OllamaPullProgress | null>(null);
  const [pullModel, setPullModel] = useState<string>(DEFAULT_PULL_MODEL);
  const [pullError, setPullError] = useState<string | null>(null);
  const pullAbortRef = useRef<AbortController | null>(null);
  // When user explicitly clicks "Pull another model" in State C, expand the picker
  const [showPickerInStateC, setShowPickerInStateC] = useState<boolean>(false);

  // Delete state — which model is currently being removed (for the spinner / disable)
  const [deletingModel, setDeletingModel] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // In-app confirmation dialog state — replaces window.confirm()
  const [pendingDelete, setPendingDelete] = useState<{ name: string; size: number } | null>(null);

  // Provider configuration modal — embeds the same AIProviderForm Settings uses.
  // Save through this modal writes to /api/ai/config/me; both surfaces share the
  // backend store so changes propagate automatically.
  const [providerConfigOpen, setProviderConfigOpen] = useState(false);

  // Tabbed surface inside the dock — Copilot pattern.
  type DockTab = 'chat' | 'suggest' | 'history';
  const [dockTab, setDockTab] = useState<DockTab>('chat');

  // Prompt-type help popover above the input.
  const [promptHelpOpen, setPromptHelpOpen] = useState(false);
  // Clear-chat confirmation.
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false);

  // History panel — recent agent runs from the durable trace store
  const [historyOpen, setHistoryOpen] = useState(false);
  const [traces, setTraces] = useState<TraceListEntry[]>([]);
  const [tracesLoading, setTracesLoading] = useState(false);

  // Page-aware suggestions for the Suggestions tab + chat empty state.
  const [suggestions, setSuggestions] = useState<AgentSuggestion[]>([]);
  const [suggestionsLoading, setSuggestionsLoading] = useState(false);

  const loadTraces = useCallback(async () => {
    setTracesLoading(true);
    try {
      const list = await listAgentTraces(20);
      // Guard against a non-array success body (null/scalar) — the
      // catch below only handles thrown errors, and traces.map() would
      // crash on a non-array. Contract is TraceListEntry[].
      setTraces(Array.isArray(list) ? list : []);
    } catch {
      setTraces([]);
    } finally {
      setTracesLoading(false);
    }
  }, []);

  // Refresh history whenever the panel toggles open
  useEffect(() => {
    if (historyOpen) loadTraces();
  }, [historyOpen, loadTraces]);

  // Refetch suggestions whenever the user navigates to a different page
  // OR opens the Suggestions tab. Cheap deterministic endpoint — safe.
  // After fetch, re-order by per-user prompt-click frequency (localStorage)
  // so the user's most-used prompts float to the top — personalised UX
  // with zero backend cost.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      setSuggestionsLoading(true);
      try {
        const list = await getAgentSuggestions(
          pageCtx?.page || 'unknown',
          pageCtx?.selected_ids?.length || 0,
        );
        let counts: Record<string, number> = {};
        try {
          counts = JSON.parse(localStorage.getItem('fpulse_prompt_frequency') || '{}');
        } catch { /* ignore */ }
        const ordered = [...list].sort((a, b) => {
          const ca = counts[a.prompt] || 0;
          const cb = counts[b.prompt] || 0;
          return cb - ca; // most-used first; stable for equal counts
        });
        if (!cancelled) setSuggestions(ordered);
      } catch {
        if (!cancelled) setSuggestions([]);
      } finally {
        if (!cancelled) setSuggestionsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pageCtx?.page, pageCtx?.selected_ids?.length]);

  // Auto-enable draft permissions on the Editor page — that's the one
  // surface where SAFE_WRITE (draft_pipeline_from_intent,
  // modify_pipeline_step) is the whole point of opening the Copilot.
  // Without this, build-intent prompts like "Build a pipeline that
  // reads sales.csv …" hit the agent loop but the LLM can't pick the
  // draft tool because it's filtered out by tier, and falls back to a
  // read-only answer (user-reported 2026-05-17). User can still
  // uncheck the box to revert to read-only.
  //
  // Only runs ONCE per page transition (the ref guard) so we don't
  // override a deliberate user toggle made afterwards.
  const _editorAutoEnabledRef = useRef<string | null>(null);
  useEffect(() => {
    const page = pageCtx?.page || '';
    if (!page) return;
    if (_editorAutoEnabledRef.current === page) return;
    _editorAutoEnabledRef.current = page;
    if (page.startsWith('editor')) {
      setAllowSafeWrites(true);
    }
  }, [pageCtx?.page]);

  // Also refresh after each new agent turn so the most-recent run shows up
  useEffect(() => {
    if (historyOpen && turns.length > 0) loadTraces();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turns.length]);

  const [budget, setBudget] = useState<AgentBudget | null>(null);

  const refreshStatus = useCallback(async (forceProviderReprobe = false) => {
    // After a pull/delete, force the backend to invalidate its 5-min
    // autoprobe cache so the new model state is picked up immediately.
    if (forceProviderReprobe) {
      try {
        await refreshAgentProvider();
      } catch {
        // Ignore — falls back to the regular status endpoint below.
      }
    }
    const [a, o, b] = await Promise.all([
      getAgentStatus().catch(() => ({ available: false, provider: 'none', model: '', tool_count: 0 } as AgentStatus)),
      getOllamaStatus().catch(() => ({ running: false, models: [], url: '' } as OllamaStatus)),
      getAgentBudget().catch(() => null),
    ]);
    setStatus(a);
    setOllamaStatus(o);
    setBudget(b);
  }, []);

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus]);

  // Cross-component sync: when AIProviderForm saves a new config it
  // dispatches `fpulse:ai_config_changed` on `window`. We listen and
  // refresh the dock's provider/model/budget header so the user sees the
  // new model immediately — no full page reload required.
  useEffect(() => {
    const onConfigChanged = () => { void refreshStatus(true); };
    window.addEventListener('fpulse:ai_config_changed', onConfigChanged);
    return () => window.removeEventListener('fpulse:ai_config_changed', onConfigChanged);
  }, [refreshStatus]);

  const handlePull = useCallback(async () => {
    setPullError(null);
    setPullProgress({ status: 'starting' });
    const ctrl = new AbortController();
    pullAbortRef.current = ctrl;
    try {
      await pullOllamaModel(pullModel, (p) => setPullProgress(p), ctrl.signal);
      // Force provider re-probe so the agent picks up the new model RIGHT
      // NOW (no 5-min cache wait) AND refresh both UI statuses.
      await refreshStatus(true);
      // Collapse the State-C picker once the pull lands so the user sees the
      // updated installed-models list (with the new pill).
      setShowPickerInStateC(false);
      setPullProgress(null);
    } catch (err) {
      setPullError(err instanceof Error ? err.message : 'Pull failed');
    } finally {
      pullAbortRef.current = null;
    }
  }, [pullModel, refreshStatus]);

  const handlePullCancel = useCallback(() => {
    pullAbortRef.current?.abort();
    pullAbortRef.current = null;
    setPullProgress(null);
  }, []);

  // Step 1: open the confirmation dialog (replaces native window.confirm())
  const handleRequestDelete = useCallback((name: string, sizeBytes: number) => {
    setDeleteError(null);
    setPendingDelete({ name, size: sizeBytes });
  }, []);

  // Step 2: user confirmed in the dialog → actually delete
  const handleConfirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    const name = pendingDelete.name;
    setDeletingModel(name);
    try {
      await deleteOllamaModel(name);
      // Same cache-invalidation reason as pull — agent must re-probe so the
      // active model rolls over (or unconfigures) immediately.
      await refreshStatus(true);
      setPendingDelete(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : `Delete failed: ${name}`;
      // Special-case the Plus admin-only response so users see a clear reason
      const friendly = /admin|forbidden|403/i.test(msg)
        ? `This model could not be removed (the server rejected the request).`
        : msg;
      setDeleteError(friendly);
      setPendingDelete(null);
    } finally {
      setDeletingModel(null);
    }
  }, [pendingDelete, refreshStatus]);

  const handleCancelDelete = useCallback(() => {
    setPendingDelete(null);
  }, []);

  // Track elapsed time while a turn is in-flight so the user gets
  // feedback ("Thinking… 12s") instead of a static spinner during slow
  // local-Ollama inference. Reset to 0 each new turn.
  const [elapsedSec, setElapsedSec] = useState(0);

  useEffect(() => {
    if (!busy) {
      setElapsedSec(0);
      return;
    }
    const tickStart = Date.now();
    const id = window.setInterval(() => {
      setElapsedSec(Math.floor((Date.now() - tickStart) / 1000));
    }, 1000);
    return () => window.clearInterval(id);
  }, [busy]);

  // Cancel the in-flight agent run. No-op if nothing is running.
  // Triggered by the Stop button rendered in place of Send while `busy`.
  const handleStop = useCallback(() => {
    const controller = abortRef.current;
    if (controller && !controller.signal.aborted) {
      try {
        controller.abort();
      } catch {
        // Defensive: AbortController.abort() never throws on modern browsers,
        // but stub environments might. Swallow and continue.
      }
    }
  }, []);

  const handleSubmit = useCallback(async (explicitPrompt?: string) => {
    // Caller can pass an explicit prompt (e.g., from a chip click) — bypasses
    // the input field. Otherwise reads the input field as normal.
    const text = (explicitPrompt ?? input).trim();
    if (!text || busy) return;
    setBusy(true);
    const userTurn: ChatTurn = {
      id: `u-${Date.now()}`,
      role: 'user',
      text,
    };
    // Live agent turn — receives streaming updates until `final` lands.
    const liveTurnId = `a-${Date.now()}`;
    const liveTurn: ChatTurn = {
      id: liveTurnId,
      role: 'agent',
      text: '',
      liveSteps: [],
      liveStatus: 'Thinking…',
      streaming: true,
    };
    setTurns((prev) => [...prev, userTurn, liveTurn]);
    if (explicitPrompt === undefined) setInput('');

    const updateLive = (patch: Partial<ChatTurn>) => {
      setTurns((prev) =>
        prev.map((t) => (t.id === liveTurnId ? { ...t, ...patch } : t)),
      );
    };

    let finalResponse: AgentRunResponse | null = null;
    let streamError: string | null = null;
    let aborted = false;
    const collectedSteps: TraceStep[] = [];
    const conversation = buildConversationPayload([
      ...turns
        .filter((t) => !t.streaming && (t.text || '').trim())
        .map((t) => ({
          role: t.role === 'agent' ? 'assistant' : 'user',
          content: t.text || '',
        })),
      { role: 'user', content: text },
    ]);

    // Per-request AbortController. Stop button calls abortRef.current.abort()
    // to interrupt the streaming fetch. Cleared on completion (success or
    // error) so the next turn starts with a fresh controller.
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await runAgentStream(
        {
          user_intent: text,
          page_context: buildPageContextPayload(pageCtx),
          allow_safe_writes: allowSafeWrites,
          dialogue_state: caseFile,
          conversation,
          mode: deepMode ? 'deep' : 'standard',
        },
        {
          signal: controller.signal,
          onEvent: (evt) => {
            if (evt.event === 'iteration') {
              // `max` is provider-aware (3 for local Ollama, 6 for cloud) —
              // see _resolve_max_iterations in backend/fpulse/ai/agent.py.
              // Older backends may not send it; fall back to 6 in that case.
              const cap = evt.max ?? 6;
              // Only show the bare "Thinking…" string when no tool_start
              // has fired yet for this iteration. tool_start always wins
              // because it tells the user what the assistant is actually
              // doing (e.g. "Checking recent executions…") instead of the
              // abstract "step N of M" that reads as "the system is slow."
              updateLive({ liveStatus: `Thinking through step ${evt.iteration} of ${cap}…` });
            } else if (evt.event === 'tool_start') {
              updateLive({ liveStatus: _statusForTool(evt.tool_name) });
            } else if (evt.event === 'token') {
              // Append the token delta to the live turn's text so the user
              // sees the response stream in word-by-word.
              setTurns((prev) =>
                prev.map((t) =>
                  t.id === liveTurnId
                    ? { ...t, text: (t.text || '') + evt.delta, liveStatus: undefined }
                    : t,
                ),
              );
            } else if (evt.event === 'step') {
              collectedSteps.push(evt.step);
              updateLive({ liveSteps: [...collectedSteps] });
            } else if (evt.event === 'final') {
              finalResponse = evt as AgentRunResponse;
              // Layer 2 — persist updated dialogue state for the next turn.
              if (finalResponse.case_file) {
                setCaseFile(finalResponse.case_file);
              }
            } else if (evt.event === 'error') {
              streamError = evt.error;
            }
          },
        },
      );
    } catch (err) {
      // AbortError is the user pressing Stop — handle it as a clean
      // cancellation, not a hard failure. Both the controller's signal
      // and the underlying fetch raise AbortError when .abort() fires.
      if (err instanceof Error && (err.name === 'AbortError' || controller.signal.aborted)) {
        aborted = true;
      } else {
        streamError = err instanceof Error ? err.message : 'Agent stream failed.';
      }
    } finally {
      // Clear the controller so a stale .abort() from a re-render doesn't
      // affect the NEXT turn.
      if (abortRef.current === controller) {
        abortRef.current = null;
      }
    }

    if (aborted && !finalResponse) {
      // Preserve any partial text the user already saw streaming in;
      // append a clear "stopped" marker so they know it was their action.
      setTurns((prev) =>
        prev.map((t) => {
          if (t.id !== liveTurnId) return t;
          const partial = (t.text || '').trim();
          return {
            ...t,
            text: partial
              ? `${partial}\n\n_Stopped by user._`
              : '_Stopped by user before any response._',
            liveStatus: undefined,
            streaming: false,
          };
        }),
      );
      setBusy(false);
      return;
    }

    if (streamError && !finalResponse) {
      updateLive({
        text: streamError,
        liveStatus: undefined,
        streaming: false,
      });
      setBusy(false);
      return;
    }

    const response = finalResponse as AgentRunResponse | null;
    if (!response) {
      updateLive({
        text: '[No response.] The stream closed without a final event.',
        liveStatus: undefined,
        streaming: false,
      });
      setBusy(false);
      return;
    }

    let displayText = response.final_text || '';
    if (!displayText.trim()) {
      const currentModel = status?.model || 'this model';
      const isLocal = status?.provider === 'ollama';
      const isBelowFloor = isLocal && isOllamaBelowToolUseFloor(currentModel);
      if (response.outcome === 'timeout') {
        const seconds = Math.round((response.elapsed_ms || 0) / 1000);
        // The default wall-clock is 300 s for local Ollama (raised from 120 s
        // on May 4 2026) and 120 s for cloud providers. Surface the right
        // number so the message matches what actually happened.
        const cap = isLocal ? 300 : 120;
        // 2026-05-21: recommendations rewritten. The prior message told
        // users to switch DOWN to qwen2.5:1.5b — but the 2026-05-19
        // floor revision raised the bar to ~7B (1.5b/3b can't drive the
        // tool loop). The two failure modes here are distinct:
        //   • below-floor  → silent garbage / no tool call → llm_failure or empty
        //   • too-heavy    → slow on CPU → timeout
        // We tell the user which one they hit so the fix is unambiguous.
        displayText = (
          `[Agent timed out after ~${seconds}s of ${cap}s cap.]\n\n`
          + `The LLM did not respond within the wall-clock cap. Common causes:\n`
          + (isBelowFloor
              ? `• ${currentModel} is BELOW the reliable tool-use floor (~7B). Tiny models advertise tool support but can't drive a real agent loop — switch to ${OLLAMA_CPU_RECOMMENDATION} (or llama3.1:8b / phi-4). Pull it from the AI Provider config.\n`
              : isLocal
                ? `• Local Ollama on CPU is slow for tool-using agents — 30–90 s per turn is typical at the ~7B floor. Wait it out, simplify the prompt, or switch to a cloud provider for interactive work.\n`
                : `• The cloud provider may be rate-limited or having an outage. Check provider status.\n`)
          + `• First inference after a model swap takes longer than subsequent ones — try sending again\n`
          + `• Other heavy processes are competing for RAM/CPU\n\n`
          + `Power users: set FPULSE_AGENT_WALL_CLOCK_S=600 (max 600) in the backend env and restart for a longer cap.`
        );
      } else if (response.outcome === 'llm_failure') {
        // 2026-05-21: when the provider is a too-small local Ollama,
        // the failure is almost always the model returning empty text
        // or skipping the tool call. Call it out explicitly so users
        // stop guessing.
        displayText = isBelowFloor
          ? (`[LLM failure.] ${currentModel} returned no usable response.\n\n`
             + `This model is BELOW the reliable tool-use floor (~7B). Small models like 0.5b/1.5b/3b advertise tool support but in practice return greetings or empty text instead of calling tools — exactly the failure you're seeing.\n\n`
             + `Fix: pull ${OLLAMA_CPU_RECOMMENDATION} (or llama3.1:8b / phi-4) via Settings → AI Provider, then retry. Expect ~6 GB RAM and 30–60 s per turn on CPU at the floor.`)
          : (`[LLM failure.] ${currentModel || 'The selected model'} returned no usable response.\n\n`
             + `Free / smaller cloud models often advertise tool support but don't reliably complete tool loops — they return empty text or stall mid-call, which is what happened here.\n\n`
             + `Fix: switch to a stronger model in Insights → AI Provider — Claude Haiku, GPT-4o-mini, or DeepSeek V3 / Llama 3.3 70B (OpenRouter) all close tool loops reliably — then retry.`);
      } else if (response.outcome === 'tool_failure') {
        displayText = `[Tool failure.] One of the agent's tool calls errored. Check the trace below for the specific tool and reason.`;
      } else if (response.outcome === 'policy_block') {
        displayText = `[Blocked by policy.] Your role or workspace policy prevented this action. See the trace for the rule that fired.`;
      } else {
        displayText = `[No response.] The agent finished without producing text. See the trace below.`;
      }
    }

    updateLive({
      id: response.run_id,
      text: displayText,
      response,
      liveSteps: undefined,
      liveStatus: undefined,
      streaming: false,
    });

    getAgentBudget().then(setBudget).catch(() => {});

    const safeWrite = response.steps.find(
      (s) => s.tool_tier === 'safe_write' && s.outcome === 'success',
    );
    if (safeWrite) {
      setPendingConfirm({ response, safeWriteStep: safeWrite });
    }
    setBusy(false);
  }, [input, busy, pageCtx, allowSafeWrites, status?.model, turns, caseFile, deepMode]);

  const textOnlyMode = !!(status?.available && status.provider === 'ollama' && !isOllamaToolCapable(status.model));

  // One-click suggestion → load into input + switch to chat tab.
  // Also bumps a localStorage frequency counter so the most-used prompts
  // float to the top on subsequent visits — personalised ordering with
  // zero backend cost.
  const useSuggestion = useCallback((s: AgentSuggestion) => {
    setInput(s.prompt);
    setDockTab('chat');
    try {
      const raw = localStorage.getItem('fpulse_prompt_frequency') || '{}';
      const counts = JSON.parse(raw) as Record<string, number>;
      counts[s.prompt] = (counts[s.prompt] || 0) + 1;
      localStorage.setItem('fpulse_prompt_frequency', JSON.stringify(counts));
    } catch { /* localStorage unavailable — non-fatal */ }
  }, []);


  // Consume pending-input deep links (e.g. ConfigPanel "Ask Copilot about
  // this SQL"). Switches to the Chat tab so the user sees the pre-filled
  // prompt immediately.
  useEffect(() => {
    if (pendingInput) {
      setInput(pendingInput);
      setDockTab('chat');
      clearPendingInput();
    }
  }, [pendingInput, clearPendingInput]);

  // Auto-scroll to the latest message whenever a turn is added, the live
  // streaming text grows, or the live status changes. The view stays
  // pinned to the bottom while the agent is working so the user sees
  // fresh tokens without manual scrolling.
  //
  // We DON'T force-scroll if the user has scrolled up to read history —
  // that breaks the "let me look at this old message" intent. The pinning
  // logic compares scrollTop to scrollHeight - clientHeight with a 100px
  // grace zone; only auto-scrolls when the user is "near the bottom" OR
  // has just sent a new message (turns.length increased).
  const lastTurnsLenRef = useRef(0);
  useEffect(() => {
    const container = scrollContainerRef.current;
    const end = messagesEndRef.current;
    if (!container || !end) return;

    const userJustSent = turns.length > lastTurnsLenRef.current;
    lastTurnsLenRef.current = turns.length;

    const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    const nearBottom = distanceFromBottom < 100;

    if (userJustSent || nearBottom) {
      // Use 'auto' (instant) on new sends so the user's prompt is immediately
      // anchored to the bottom; 'smooth' during streaming for a calmer feel.
      end.scrollIntoView({ behavior: userJustSent ? 'auto' : 'smooth', block: 'end' });
    }
  }, [turns, turns.map((t) => `${t.text?.length || 0}-${t.liveStatus || ''}`).join('|')]);

  return (
    <div className="flex flex-col h-full bg-white overflow-hidden">
      <div className="px-4 py-3 border-b border-slate-100 bg-gradient-to-r from-indigo-500 to-purple-500 text-white shrink-0">
        <div className="flex items-center justify-between">
          <div className="min-w-0">
            <div className="text-sm font-bold flex items-center gap-2 flex-wrap">
              <span>F-Pulse Copilot</span>
              <button
                type="button"
                onClick={() => setProviderConfigOpen(true)}
                className="text-xs font-semibold text-white hover:text-white bg-white/20 hover:bg-white/30 px-2 py-0.5 rounded transition-colors"
                title="Switch provider, add a new one, or change the active model"
              >
                Configure &raquo;
              </button>
            </div>
            <div className="text-xs text-white/95 mt-0.5 font-medium">
              {status?.available
                ? (
                    <>
                      {status.provider}{status.model ? ' / ' + status.model : ''}
                      {' \u00b7 '}
                      {textOnlyMode
                        ? <span className="text-amber-100 font-semibold">text-only (no tool-use)</span>
                        : <>{status.tool_count} tools</>}
                      {budget && budget.user.tokens_cap > 0 && (
                        <>
                          {' \u00b7 '}
                          <span
                            className={
                              budget.user.percent_used >= 90 ? 'text-red-100 font-bold'
                              : budget.user.percent_used >= 80 ? 'text-amber-100 font-semibold'
                              : 'text-white'
                            }
                            title={`Today: ${budget.user.tokens_used_today.toLocaleString()} / ${budget.user.tokens_cap.toLocaleString()} tokens (${budget.user.request_count_today} req)`}
                          >
                            {budget.user.percent_used.toFixed(0)}% of daily cap
                          </span>
                        </>
                      )}
                    </>
                  )
                : 'AI provider not configured'}
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {/* Deep-mode toggle (2026-05-22). When on, the next call
                sends mode='deep' — backend skips fast-lane, widens
                context, raises iteration cap. Worth ~30-60 s extra
                on cloud and 2-5 minutes on local Ollama 7B. The
                amber pill below the bar surfaces the cost so users
                don't trip into it accidentally. */}
            <label
              className="flex items-center gap-1.5 text-xs text-white font-medium cursor-pointer"
              title={
                deepMode
                  ? 'Deep mode: full reasoning, slower. Cloud ~30-60 s, local Ollama 2-5 min per turn.'
                  : 'Enable deep mode for hard reasoning questions (skips shortcuts, slower).'
              }
            >
              <input
                type="checkbox"
                checked={deepMode}
                onChange={(e) => {
                  setDeepMode(e.target.checked);
                  try { localStorage.setItem('fpulse.ai.deepMode', String(e.target.checked)); } catch { /* ignore */ }
                }}
                className="accent-white"
              />
              Deep
              {deepMode && (
                <span className="ml-1 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide rounded bg-amber-300 text-amber-900">
                  Slow
                </span>
              )}
            </label>
            <label className="flex items-center gap-1.5 text-xs text-white font-medium cursor-pointer">
              <input
                type="checkbox"
                checked={allowSafeWrites}
                onChange={(e) => setAllowSafeWrites(e.target.checked)}
                className="accent-white"
              />
              Allow drafts
            </label>
            <button
              type="button"
              onClick={() => setWidgetOpen(false)}
              aria-label="Close Copilot"
              title="Close (Esc, or click outside)"
              className="text-white bg-white/20 hover:bg-red-500 hover:ring-red-400 rounded-md p-1.5 transition-colors ring-1 ring-white/20"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </div>
        </div>
      </div>

      {/* Tab bar — Chat / Suggestions / History (Copilot pattern). */}
      <div className="border-b border-slate-100 bg-white shrink-0 flex items-stretch text-xs font-semibold">
        {([
          { key: 'chat' as const, label: 'Chat', count: turns.length },
          { key: 'suggest' as const, label: 'Suggestions', count: suggestions.length },
          { key: 'history' as const, label: 'History', count: traces.length },
        ]).map((t) => {
          const active = dockTab === t.key;
          return (
            <button
              key={t.key}
              type="button"
              onClick={() => {
                setDockTab(t.key);
                if (t.key === 'history') setHistoryOpen(true);
                else setHistoryOpen(false);
              }}
              className={`flex-1 px-3 py-2 border-b-2 transition-colors ${
                active
                  ? 'border-indigo-500 text-indigo-700 bg-indigo-50/40'
                  : 'border-transparent text-slate-500 hover:text-slate-700 hover:bg-slate-50'
              }`}
            >
              {t.label}
              {t.count > 0 && (
                <span className={`ml-1 inline-flex items-center justify-center min-w-[18px] h-[18px] px-1.5 text-xs font-bold rounded-full leading-none ${
                  active ? 'bg-indigo-100 text-indigo-700' : 'bg-slate-200 text-slate-600'
                }`}>
                  {t.count}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* Sub-toolbar: clear chat, etc. — only on Chat tab when there are turns. */}
      {dockTab === 'chat' && turns.length > 0 && (
        <div className="border-b border-slate-100 bg-slate-50/60 shrink-0 px-3 py-1.5 flex items-center justify-between text-xs">
          <span className="text-slate-500">
            {turns.length} message{turns.length === 1 ? '' : 's'}
          </span>
          <button
            type="button"
            onClick={() => setClearConfirmOpen(true)}
            disabled={busy}
            title="Clear this conversation"
            className="inline-flex items-center gap-1 px-2 py-1 rounded text-slate-600 hover:text-red-700 hover:bg-red-50 disabled:opacity-40 transition-colors font-semibold"
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <polyline points="3 6 5 6 21 6" />
              <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
              <path d="M10 11v6M14 11v6" />
              <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
            </svg>
            Clear chat
          </button>
        </div>
      )}

      <div ref={scrollContainerRef} className="flex-1 overflow-y-auto p-3 space-y-3">
        {historyOpen && (
          <div className="rounded-xl bg-slate-50 border border-slate-200 overflow-hidden">
            <div className="px-3 py-2 border-b border-slate-200 flex items-center justify-between bg-white">
              <div className="text-xs font-bold uppercase tracking-wider text-slate-600">
                Recent agent runs
              </div>
              <button
                type="button"
                onClick={loadTraces}
                disabled={tracesLoading}
                className="text-xs text-indigo-600 hover:text-indigo-800 disabled:opacity-50"
              >
                {tracesLoading ? 'Loading…' : 'Refresh'}
              </button>
            </div>
            <div className="max-h-64 overflow-y-auto divide-y divide-slate-100">
              {traces.length === 0 && !tracesLoading && (
                <div className="text-[12px] text-slate-500 px-3 py-3">
                  No agent runs yet. Send a message to start.
                </div>
              )}
              {traces.map((t) => {
                const badge = _outcomeBadge[t.outcome] || _outcomeBadge.success;
                const when = new Date(t.created_at).toLocaleString(undefined, {
                  month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
                });
                const totalTokens = t.total_tokens_in + t.total_tokens_out;
                return (
                  <div key={t.run_id} className="px-3 py-2 text-xs hover:bg-white">
                    <div className="flex items-start justify-between gap-2 mb-0.5">
                      <span
                        className={`shrink-0 px-1.5 py-0.5 rounded ring-1 ${badge.tone} font-semibold uppercase tracking-wider text-[9px]`}
                      >
                        {badge.label}
                      </span>
                      <span className="text-slate-400 text-xs shrink-0">{when}</span>
                    </div>
                    <div className="text-slate-800 truncate" title={t.user_intent}>
                      {t.user_intent || <span className="italic text-slate-400">(no intent)</span>}
                    </div>
                    <div className="text-slate-500 text-xs mt-0.5 flex items-center gap-2 flex-wrap">
                      <span>{t.step_count} step{t.step_count === 1 ? '' : 's'}</span>
                      <span>&middot;</span>
                      <span>{t.elapsed_ms} ms</span>
                      {totalTokens > 0 && (
                        <>
                          <span>&middot;</span>
                          <span>{totalTokens.toLocaleString()} tok</span>
                        </>
                      )}
                      {t.page && (
                        <>
                          <span>&middot;</span>
                          <code className="bg-slate-100 px-1 rounded">{t.page}</code>
                        </>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {textOnlyMode && !pullProgress && (
          <div className="rounded-xl bg-amber-50 border border-amber-300 px-3 py-2.5">
            <div className="flex items-start gap-2">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="text-amber-700 shrink-0 mt-0.5">
                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                <line x1="12" y1="9" x2="12" y2="13" />
                <line x1="12" y1="17" x2="12.01" y2="17" />
              </svg>
              <div className="text-[12px] text-amber-900 leading-snug flex-1">
                <div className="font-semibold mb-0.5">
                  Text-only mode &mdash; <code className="font-mono">{status?.model}</code> can&rsquo;t call tools.
                </div>
                <p className="mb-2">
                  The agent will reply with prose only and may invent tool-call syntax that
                  does NOT actually run. For real tool-use, the fastest path is a cloud
                  provider (Anthropic / OpenAI). Tool-capable Ollama models (llama3.1,
                  qwen2.5) work too but are <strong>slow on CPU-only machines</strong> (30&ndash;120 s per turn).
                </p>
                <div className="flex items-center gap-2 flex-wrap">
                  <button
                    type="button"
                    onClick={() => setProviderConfigOpen(true)}
                    className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-md transition-colors"
                  >
                    Use a cloud provider
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setPullModel('llama3.1');
                      setShowPickerInStateC(true);
                      handlePull();
                    }}
                    className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-semibold text-emerald-800 bg-emerald-100 hover:bg-emerald-200 rounded-md transition-colors"
                    title="4.7 GB download. Slow on CPU; needs a GPU for usable speed."
                  >
                    Or pull llama3.1 (slow on CPU)
                  </button>
                  <a
                    href="https://ollama.com/library"
                    target="_blank"
                    rel="noreferrer"
                    className="text-xs underline text-amber-800 hover:text-amber-900"
                  >
                    More models
                  </a>
                </div>
              </div>
            </div>
          </div>
        )}

        {status && !status.available && (
          <div className="rounded-xl bg-amber-50 border border-amber-300 ring-1 ring-amber-200/60 overflow-hidden">
            <div className="px-4 py-3 border-b border-amber-200 bg-amber-100/60">
              <div className="flex items-start gap-2">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="text-amber-700 shrink-0 mt-0.5">
                  <circle cx="12" cy="12" r="10" />
                  <line x1="12" y1="8" x2="12" y2="12" />
                  <line x1="12" y1="16" x2="12.01" y2="16" />
                </svg>
                <div>
                  <div className="text-sm font-semibold text-amber-900">AI provider not configured</div>
                  <div className="text-[12px] text-amber-800 mt-0.5 leading-snug">
                    Pick one of the two options below to enable the agent.
                  </div>
                </div>
              </div>
            </div>

            <div className="px-4 py-3 space-y-3">
              {/* Recommended: paid cloud provider — fast first response. */}
              <div>
                <div className="flex items-center gap-2 mb-1.5">
                  <span className="text-xs font-bold text-indigo-700 uppercase tracking-wider">
                    Recommended &mdash; Cloud provider (Anthropic / OpenAI)
                  </span>
                  <span className="text-xs font-semibold text-indigo-700 bg-indigo-100 px-1.5 py-0.5 rounded">
                    fast
                  </span>
                </div>
                <div className="text-sm text-slate-700 leading-relaxed mb-2">
                  Add a Claude or OpenAI key in <strong>Settings &rarr; AI Provider</strong> for snappy 1&ndash;3 s
                  responses with full tool-use. Keys stay on your machine; F-Pulse never proxies them.
                </div>
                <div className="space-y-1 text-[12px] font-mono mb-2">
                  <div className="bg-slate-900 text-emerald-300 px-2.5 py-1.5 rounded">
                    <span className="text-slate-400">$ </span>ANTHROPIC_API_KEY=sk-ant-...
                  </div>
                  <div className="bg-slate-900 text-emerald-300 px-2.5 py-1.5 rounded">
                    <span className="text-slate-400">$ </span>OPENAI_API_KEY=sk-...
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => setProviderConfigOpen(true)}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg transition-colors"
                >
                  Configure provider
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <line x1="5" y1="12" x2="19" y2="12" />
                    <polyline points="12 5 19 12 12 19" />
                  </svg>
                </button>
              </div>

              {/* Live price comparison + recommendation across cloud providers.
                  Replaces the old Ollama install/pull flow, which is no longer
                  user-facing per the OSS direction shift away from CPU local LLMs. */}
              <div className="border-t border-amber-200/70 pt-3">
                <ProviderComparison onConfigure={() => setProviderConfigOpen(true)} />
                {/* (Ollama install / pull / model-picker UI removed in favor of
                    cloud-only provider recommendation. Backend Ollama support
                    is preserved for users who configure it directly via env
                    var or Insights → AI Provider.) */}

                {/* (B/B'/C Ollama states removed)
                    Old block began here and ran ~200 lines: model picker,
                    pull-progress bar, installed-model pill list, and an env
                    var instruction. All replaced by ProviderComparison above. */}
                {false && (
                  <>
                    {/* Resource warning */}
                    <div className="rounded-lg bg-orange-50 border border-orange-200 px-3 py-2 mb-3">
                      <div className="flex items-start gap-2">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="text-orange-700 shrink-0 mt-0.5">
                          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                          <line x1="12" y1="9" x2="12" y2="13" />
                          <line x1="12" y1="17" x2="12.01" y2="17" />
                        </svg>
                        <div className="text-[12px] text-orange-900 leading-snug">
                          <div className="font-semibold mb-0.5">Local LLMs use real memory + compute.</div>
                          During inference each model holds its weights in RAM. On a 16 GB machine, the medium models
                          (~6&ndash;8 GB) leave little room for everything else &mdash; expect slow CPU response (5&ndash;10 tok/s)
                          unless you have a dedicated GPU. Start with <code className="bg-white border border-orange-200 px-1 rounded text-xs">phi3</code> if unsure.
                        </div>
                      </div>
                    </div>

                    {/* Model cards */}
                    <div className="space-y-1.5 mb-3">
                      {KNOWN_MODELS.map((m) => {
                        const w = WEIGHT_STYLES[m.weight];
                        const selected = pullModel === m.name;
                        return (
                          <button
                            key={m.name}
                            type="button"
                            onClick={() => setPullModel(m.name)}
                            className={`w-full text-left rounded-lg border px-3 py-2 transition-all ${
                              selected
                                ? 'border-emerald-500 bg-emerald-50 ring-1 ring-emerald-200'
                                : 'border-slate-200 bg-white hover:border-slate-300'
                            }`}
                          >
                            <div className="flex items-center justify-between gap-2">
                              <div className="flex items-center gap-2 min-w-0">
                                <span className={`w-2 h-2 rounded-full shrink-0 ${w.dot}`} aria-hidden />
                                <code className="text-[12px] font-mono font-semibold text-slate-800 truncate">{m.name}</code>
                                <span className="text-xs uppercase tracking-wider text-slate-500">{w.label}</span>
                                {m.tools ? (
                                  <span
                                    className="text-[9px] font-bold uppercase tracking-wider text-emerald-700 bg-emerald-100 px-1.5 py-0.5 rounded"
                                    title="Supports tool-use (full agent functionality)"
                                  >
                                    Tools
                                  </span>
                                ) : (
                                  <span
                                    className="text-[9px] font-bold uppercase tracking-wider text-slate-500 bg-slate-100 px-1.5 py-0.5 rounded"
                                    title="Text-only \u2014 cannot call tools; agent runs in chat mode"
                                  >
                                    Text only
                                  </span>
                                )}
                                {m.recommended === 'cpu' && (
                                  <span
                                    className="text-[9px] font-bold uppercase tracking-wider text-violet-700 bg-violet-100 px-1.5 py-0.5 rounded"
                                    title="Recommended default for CPU-only laptops"
                                  >
                                    CPU pick
                                  </span>
                                )}
                                {m.recommended === 'gpu' && (
                                  <span
                                    className="text-[9px] font-bold uppercase tracking-wider text-blue-700 bg-blue-100 px-1.5 py-0.5 rounded"
                                    title="Recommended for GPU-accelerated systems"
                                  >
                                    GPU pick
                                  </span>
                                )}
                              </div>
                              <div className="text-xs text-slate-500 shrink-0 tabular-nums">
                                {m.diskGB} GB disk &middot; ~{m.ramGB} GB RAM
                              </div>
                            </div>
                            {m.notes && <div className="text-xs text-slate-500 mt-0.5 ml-4">{m.notes}</div>}
                          </button>
                        );
                      })}
                    </div>

                    {/* Custom model input */}
                    <div className="border-t border-slate-200 pt-3 mb-3">
                      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1">
                        Custom model name
                      </label>
                      <input
                        type="text"
                        value={pullModel}
                        onChange={(e) => setPullModel(e.target.value)}
                        placeholder="e.g. qwen2.5:7b, deepseek-coder:6.7b"
                        className="w-full text-[12px] font-mono px-2.5 py-1.5 rounded-lg border border-slate-300 focus:border-emerald-400 focus:outline-none"
                      />
                      <div className="text-xs text-slate-500 mt-1">
                        Any model from the{' '}
                        <a href="https://ollama.com/library" target="_blank" rel="noreferrer" className="text-indigo-600 hover:underline">
                          Ollama library
                        </a>
                        . Check its size before pulling.
                      </div>
                    </div>

                    <button
                      type="button"
                      onClick={handlePull}
                      disabled={!pullModel.trim()}
                      className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-white bg-emerald-600 hover:bg-emerald-700 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                        <polyline points="7 10 12 15 17 10" />
                        <line x1="12" y1="15" x2="12" y2="3" />
                      </svg>
                      Pull <code className="font-mono ml-0.5">{pullModel || '\u2026'}</code>
                    </button>
                    {pullError && (
                      <div className="text-xs text-red-700 bg-red-50 border border-red-200 rounded px-2 py-1 mt-2">
                        {pullError}
                      </div>
                    )}
                  </>
                )}

                {/* State B' — Pull in progress */}
                {pullProgress && (
                  <div className="rounded-lg bg-slate-50 border border-slate-200 p-3">
                    <div className="flex items-center justify-between mb-2">
                      <div className="text-[12px] font-semibold text-slate-800">
                        Pulling <code className="bg-white border border-slate-200 px-1 rounded">{pullModel}</code>
                      </div>
                      <button
                        type="button"
                        onClick={handlePullCancel}
                        className="text-xs text-slate-500 hover:text-red-600"
                      >
                        Cancel
                      </button>
                    </div>
                    <div className="text-xs text-slate-600">{pullProgress.status}</div>
                    {pullProgress.total && pullProgress.total > 0 && (
                      <div className="mt-2">
                        <div className="h-2 rounded-full bg-slate-200 overflow-hidden">
                          <div
                            className="h-full bg-emerald-500 transition-all"
                            style={{
                              width: `${Math.min(100, ((pullProgress.completed || 0) / pullProgress.total) * 100).toFixed(1)}%`,
                            }}
                          />
                        </div>
                        <div className="text-xs text-slate-500 mt-1">
                          {fmtBytes(pullProgress.completed || 0)} of {fmtBytes(pullProgress.total)}
                        </div>
                      </div>
                    )}
                    {pullError && (
                      <div className="text-xs text-red-700 bg-red-50 border border-red-200 rounded px-2 py-1 mt-2">
                        {pullError}
                      </div>
                    )}
                  </div>
                )}

                {/* State C — Ollama running with at least one model AND picker is collapsed */}
                {ollamaStatus && ollamaStatus.running && ollamaStatus.models.length > 0 && !pullProgress && !showPickerInStateC && (
                  <>
                    <div className="flex items-center justify-between mb-2 gap-2">
                      <div className="text-sm text-slate-700 leading-relaxed">
                        <span className="font-semibold text-emerald-700">&#10003; Ollama is ready</span> &mdash;
                        {' '}{ollamaStatus.models.length} model{ollamaStatus.models.length === 1 ? '' : 's'} available.
                      </div>
                      <button
                        type="button"
                        onClick={() => setShowPickerInStateC(true)}
                        className="text-xs text-emerald-700 hover:text-emerald-900 font-semibold underline shrink-0"
                      >
                        + Pull another
                      </button>
                    </div>
                    <div className="flex flex-wrap gap-1.5 mb-2">
                      {ollamaStatus.models.map((m) => {
                        const busy = deletingModel === m.name;
                        return (
                          <span
                            key={m.name}
                            className={`inline-flex items-center gap-1 text-xs font-mono bg-emerald-50 text-emerald-800 border border-emerald-200 pl-2 pr-1 py-0.5 rounded ${busy ? 'opacity-60' : ''}`}
                            title={fmtBytes(m.size)}
                          >
                            <span>{m.name}</span>
                            <button
                              type="button"
                              onClick={() => handleRequestDelete(m.name, m.size)}
                              disabled={busy || deletingModel !== null}
                              className="ml-0.5 w-4 h-4 flex items-center justify-center rounded text-emerald-700 hover:bg-red-100 hover:text-red-700 disabled:cursor-not-allowed transition-colors"
                              aria-label={`Remove ${m.name}`}
                              title={busy ? 'Removing\u2026' : `Remove ${m.name} (frees ${fmtBytes(m.size)})`}
                            >
                              {busy ? (
                                <svg className="animate-spin" width="10" height="10" viewBox="0 0 24 24">
                                  <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" fill="none" opacity="0.3" />
                                  <path d="M4 12a8 8 0 018-8" stroke="currentColor" strokeWidth="3" fill="none" strokeLinecap="round" />
                                </svg>
                              ) : (
                                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                                  <line x1="18" y1="6" x2="6" y2="18" />
                                  <line x1="6" y1="6" x2="18" y2="18" />
                                </svg>
                              )}
                            </button>
                          </span>
                        );
                      })}
                    </div>
                    {deleteError && (
                      <div className="text-xs text-red-700 bg-red-50 border border-red-200 rounded px-2 py-1 mb-2">
                        {deleteError}
                      </div>
                    )}
                    <div className="text-[12px] text-slate-700 leading-relaxed">
                      Last step: set the env var and restart the F-Pulse backend &mdash; or restart now if it&rsquo;s
                      already set.
                    </div>
                    <div className="bg-slate-900 text-emerald-300 px-2.5 py-1.5 rounded text-[12px] font-mono mt-1">
                      <span className="text-slate-400">$ </span>OLLAMA_URL={ollamaStatus.url}
                    </div>
                  </>
                )}
              </div>

            </div>
          </div>
        )}

        {turns.length === 0 && dockTab === 'chat' && (
          <div className="space-y-3">
            <div className="text-xs text-slate-600 px-3 py-2 leading-relaxed">
              <div className="font-semibold text-slate-800 mb-1">
                Hi — I'm your F-Pulse Copilot.
              </div>
              Ask anything about your pipelines, connections, executions, or metrics.
              I can also help build, diagnose failures, and summarize runs. Try one of
              the prompts below or type your own.
            </div>
            {/* Suggested prompts — proactive Copilot pattern. */}
            {suggestions.length > 0 && (
              <div className="px-3 space-y-1.5">
                {suggestions.slice(0, 4).map((s, i) => (
                  <button
                    key={i}
                    type="button"
                    onClick={() => useSuggestion(s)}
                    className="w-full text-left px-3 py-2 text-[12px] rounded-lg border border-slate-200 bg-white hover:bg-indigo-50 hover:border-indigo-200 hover:shadow-sm transition-all group"
                  >
                    <div className="flex items-start gap-2">
                      <span
                        className={`shrink-0 mt-0.5 inline-flex items-center justify-center w-4 h-4 rounded ${
                          s.category === 'diagnose' ? 'bg-red-100 text-red-700'
                          : s.category === 'build' ? 'bg-emerald-100 text-emerald-700'
                          : s.category === 'monitor' ? 'bg-amber-100 text-amber-700'
                          : 'bg-indigo-100 text-indigo-700'
                        }`}
                      >
                        <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                          <polyline points="9 18 15 12 9 6" />
                        </svg>
                      </span>
                      <span className="flex-1 font-medium text-slate-800 group-hover:text-indigo-800">
                        {s.label}
                      </span>
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Suggestions tab — full grid of contextual prompts */}
        {dockTab === 'suggest' && (
          <div className="space-y-3 px-2">
            <div className="text-xs text-slate-500 px-2">
              Page-aware prompts for{' '}
              <code className="bg-slate-100 px-1 py-0.5 rounded text-xs">{pageCtx?.page || 'this page'}</code>.
              {' '}Click any to load it into the chat.
            </div>
            {suggestionsLoading && (
              <div className="text-xs text-slate-400 px-2">Loading suggestions…</div>
            )}
            {!suggestionsLoading && suggestions.length === 0 && (
              <div className="text-xs text-slate-400 px-2">No suggestions for this page yet.</div>
            )}
            {suggestions.map((s, i) => {
              const tone =
                s.category === 'diagnose' ? 'border-red-200 bg-red-50/40 hover:bg-red-50' :
                s.category === 'build' ? 'border-emerald-200 bg-emerald-50/40 hover:bg-emerald-50' :
                s.category === 'monitor' ? 'border-amber-200 bg-amber-50/40 hover:bg-amber-50' :
                'border-slate-200 bg-white hover:bg-indigo-50';
              return (
                <button
                  key={i}
                  type="button"
                  onClick={() => useSuggestion(s)}
                  className={`w-full text-left px-3 py-2 rounded-lg border transition-colors ${tone}`}
                >
                  <div className="text-[12px] font-semibold text-slate-800 mb-0.5">{s.label}</div>
                  <div className="text-xs text-slate-500 leading-snug">{s.prompt}</div>
                  <div className="mt-1.5">
                    <span className="text-[9px] uppercase tracking-wider font-bold text-slate-400">
                      {s.category}
                    </span>
                  </div>
                </button>
              );
            })}
          </div>
        )}

        {dockTab === 'chat' && turns.map((turn) => (
          <div
            key={turn.id}
            className={`text-sm flex ${turn.role === 'user' ? 'justify-end' : 'justify-start'} gap-2 items-start`}
          >
            {turn.role === 'agent' && (
              /* Bot avatar — small gradient pill so the eye finds the agent
                 turn fast without reading. Only on agent side. */
              <div className="shrink-0 w-7 h-7 rounded-full bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center text-white text-xs font-bold shadow-sm mt-0.5">
                ✦
              </div>
            )}
            <div
              className={`inline-block max-w-[85%] px-3.5 py-2.5 rounded-2xl shadow-sm ${
                turn.role === 'user'
                  ? 'bg-gradient-to-br from-indigo-600 to-indigo-700 text-white rounded-br-sm'
                  : 'bg-white text-slate-800 rounded-bl-sm border border-slate-200'
              }`}
            >
              {turn.streaming && !turn.text ? (
                <div className="flex flex-col gap-1">
                  {/* Animated three-dot typing indicator — feels alive,
                      not just a spinner. Tailwind animate-bounce with
                      staggered delays via inline style. */}
                  <div className="flex items-center gap-1.5 text-slate-500 py-0.5">
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-indigo-400 animate-bounce" style={{ animationDelay: '0ms' }} />
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-indigo-400 animate-bounce" style={{ animationDelay: '150ms' }} />
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-indigo-400 animate-bounce" style={{ animationDelay: '300ms' }} />
                    {turn.liveStatus && turn.liveStatus !== 'Thinking…' && (
                      <span className="ml-1 text-xs italic">{turn.liveStatus}</span>
                    )}
                    {elapsedSec > 5 && (
                      <span className="text-slate-400 text-xs ml-1">{elapsedSec}s</span>
                    )}
                  </div>
                  {/* Local-CPU reassurance — surfaces after 30 s on Ollama
                      and points the user at the instant fast-lane prompts
                      they can use instead. Fast lane returns in <1 s and
                      never hits this hint, so showing it means the prompt
                      genuinely went to the LLM. Hint is actionable, not
                      passive: tells them what to type for instant answers. */}
                  {elapsedSec >= 30 && status?.provider === 'ollama' && (
                    <div className="text-xs text-slate-500 leading-snug pl-5 mt-1">
                      This question went to the local AI provider and will
                      take 1–3 min on CPU. For instant answers next time, try:{' '}
                      <span className="font-mono text-slate-700">"list pipelines"</span>,{' '}
                      <span className="font-mono text-slate-700">"overview"</span>,{' '}
                      <span className="font-mono text-slate-700">"failures today"</span>,{' '}
                      <span className="font-mono text-slate-700">"running now"</span>.
                    </div>
                  )}
                </div>
              ) : turn.role === 'agent' ? (
                <AgentSegments
                  segments={parseAgentCards(turn.text)}
                  onAction={handleAction}
                  disabled={busy || turn.streaming}
                />
              ) : (
                <div className="whitespace-pre-wrap leading-relaxed">{turn.text}</div>
              )}
            </div>
            {turn.streaming && turn.liveSteps && turn.liveSteps.length > 0 && (
              <div className="mt-2 text-xs text-slate-400">
                <div className="bg-slate-50 rounded-lg p-2 text-left">
                  <div className="text-slate-600 text-xs uppercase tracking-wider font-semibold mb-1">
                    {turn.liveSteps.length} tool call{turn.liveSteps.length === 1 ? '' : 's'} so far
                  </div>
                  <div className="divide-y divide-slate-100">
                    {turn.liveSteps.map((step) => (
                      <StepRow key={step.step_id} step={step} />
                    ))}
                  </div>
                </div>
              </div>
            )}
            {turn.response && (
              <div className="mt-2 text-xs text-slate-400 space-y-1">
                {/* CP-P4 — no-provider honesty + nudge. The backend answers
                    these turns from a canned fast-path; make that explicit and
                    give a one-click way to set a provider up (same in-dock
                    config the top banner uses), instead of a plain text line. */}
                {turn.response.no_provider && (
                  <div className="rounded-lg bg-blue-50 border border-blue-200 px-3 py-2.5 mb-1">
                    <div className="text-[13px] font-semibold text-blue-900">No AI provider is configured</div>
                    <div className="text-[12px] text-blue-800 mt-0.5 leading-snug">
                      The copilot needs a local (Ollama) or cloud LLM to answer this. Quick
                      shortcuts like "list pipelines" or "overview" still work without one.
                    </div>
                    <button
                      type="button"
                      onClick={() => setProviderConfigOpen(true)}
                      className="mt-2 inline-flex items-center gap-1 px-2.5 py-1 text-[12px] font-semibold text-white bg-blue-600 hover:bg-blue-700 rounded-md transition-colors"
                    >
                      Set up a provider →
                    </button>
                  </div>
                )}
                <div className="flex items-center gap-2 flex-wrap">
                  {turn.response.instant && (() => {
                    // Confidence-band UX (May 6 2026 review): show a
                    // friendlier "Best guess" badge with a "rephrase
                    // hint" subtitle when the router matched but at
                    // borderline confidence (0.6 ≤ conf < 0.85). High
                    // confidence (≥0.85) keeps the green Instant badge.
                    const conf = turn.response.instant_confidence ?? 1.0;
                    const isBestGuess = conf > 0 && conf < 0.85;
                    const badgeClass = isBestGuess
                      ? 'bg-amber-50 border-amber-200 text-amber-700'
                      : 'bg-emerald-50 border-emerald-200 text-emerald-700';
                    const dotClass = isBestGuess ? 'text-amber-600' : 'text-emerald-600';
                    const label = isBestGuess ? 'Best guess' : 'Instant';
                    const tooltip = isBestGuess
                      ? `Matched at ${(conf * 100).toFixed(0)}% confidence (intent: ${turn.response.instant_intent || 'unknown'}). If this isn't what you meant, just rephrase.`
                      : turn.response.instant_intent
                        ? `Answered by the rule-based fast lane (intent: ${turn.response.instant_intent}). No LLM was called.`
                        : 'Answered by the rule-based fast lane. No LLM was called.';
                    return (
                      <span
                        className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-xs font-semibold ${badgeClass}`}
                        title={tooltip}
                      >
                        <svg className="w-2.5 h-2.5" fill="currentColor" viewBox="0 0 24 24" aria-hidden>
                          <path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z" />
                        </svg>
                        {label}
                        {turn.response.elapsed_ms > 0 && (
                          <span className={`${dotClass} ml-0.5 font-normal`}>· {turn.response.elapsed_ms}ms</span>
                        )}
                      </span>
                    );
                  })()}
                  <span>{formatCost(turn.response.cost)}</span>
                </div>

                {/* Inline action chips — Copilot pattern. We map each
                    successful tool call to a navigation shortcut so the
                    user can jump to the relevant page with one click. */}
                {(() => {
                  const TOOL_NAV: Record<string, { label: string; hash: string }> = {
                    list_pipelines:        { label: 'Open Pipelines',   hash: '#pipelines' },
                    list_projects:         { label: 'Open Projects',    hash: '#projects' },
                    list_executions:       { label: 'Open Executions',  hash: '#executions' },
                    list_alerts:           { label: 'Open Alerts',      hash: '#alerts' },
                    list_schedules:        { label: 'Open Schedules',   hash: '#schedules' },
                    inspect_connections:   { label: 'Open Connections', hash: '#connections' },
                    query_metrics:         { label: 'Open Dashboard',   hash: '#dashboard' },
                    summarize_pipeline:    { label: 'Open Pipelines',   hash: '#pipelines' },
                    compose_report:        { label: 'Open Reports',     hash: '#reports' },
                    workspace_overview:    { label: 'Open Dashboard',   hash: '#dashboard' },
                  };
                  const seen = new Set<string>();
                  const chips: { label: string; hash: string }[] = [];
                  for (const s of turn.response.steps) {
                    if (s.outcome !== 'success') continue;
                    const nav = TOOL_NAV[s.tool_name];
                    if (nav && !seen.has(nav.hash)) {
                      seen.add(nav.hash);
                      chips.push(nav);
                    }
                  }
                  if (chips.length === 0) return null;
                  return (
                    <div className="flex flex-wrap items-center gap-1.5 mt-1">
                      <span
                        className="text-[10px] uppercase tracking-wider text-slate-400 font-semibold"
                        title="This answer is grounded in the tool calls listed below — open the source it used"
                      >
                        Grounded in
                      </span>
                      {chips.map((c) => (
                        <button
                          key={c.hash}
                          type="button"
                          onClick={() => {
                            window.location.hash = c.hash;
                          }}
                          className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-semibold rounded-full bg-indigo-50 text-indigo-700 hover:bg-indigo-100 ring-1 ring-indigo-200"
                          title={`Navigate to ${c.label}`}
                        >
                          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                            <line x1="5" y1="12" x2="19" y2="12" />
                            <polyline points="12 5 19 12 12 19" />
                          </svg>
                          {c.label}
                        </button>
                      ))}
                      <button
                        type="button"
                        onClick={() => {
                          setInput('Tell me more about this. ');
                        }}
                        className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-semibold rounded-full bg-slate-100 text-slate-700 hover:bg-slate-200"
                        title="Ask a follow-up question"
                      >
                        Ask follow-up
                      </button>
                    </div>
                  );
                })()}

                {turn.response.steps.length > 0 && (
                  <details className="mt-1 bg-slate-50 rounded-lg p-2 text-left">
                    <summary className="cursor-pointer text-slate-600 text-xs uppercase tracking-wider font-semibold">
                      {turn.response.steps.length} tool call{turn.response.steps.length === 1 ? '' : 's'}
                    </summary>
                    <div className="mt-1 divide-y divide-slate-100">
                      {turn.response.steps.map((step) => (
                        <StepRow key={step.step_id} step={step} />
                      ))}
                    </div>
                  </details>
                )}
              </div>
            )}
          </div>
        ))}

        {pendingConfirm && (() => {
          // Extract draft_id from the tool result for this safe-write step.
          // draft_pipeline_from_intent + modify_pipeline_step both return
          // {draft_id, ...} in their payloads — DiffPreview uses it to
          // fetch the structured diff via /api/ai/agent/draft/{id}/preview.
          const draftToolResult = pendingConfirm.response.tool_results.find(
            (tr) => tr.tool === pendingConfirm.safeWriteStep.tool_name,
          );
          const draftResult =
            draftToolResult && typeof draftToolResult.result === 'object' && draftToolResult.result
              ? (draftToolResult.result as Record<string, unknown>)
              : undefined;
          const draftId = draftResult?.draft_id as string | undefined;
          // CP-P3 — draft_pipeline_from_intent returns the full Workflow IR
          // under `ir`. When present we can apply it straight to the canvas
          // (loadWorkflow → nodes/edges) instead of only acknowledging a
          // server-side draft. Other safe-write tools (e.g. draft_alert_rule)
          // carry no IR and keep the acknowledge-only behaviour.
          const draftIR = draftResult?.ir as { steps?: unknown[]; name?: string } | undefined;
          const canApplyToCanvas = !!draftIR && Array.isArray(draftIR.steps) && draftIR.steps.length > 0;
          const stepCount = canApplyToCanvas ? (draftIR!.steps as unknown[]).length : 0;
          const existingNodeCount = useWorkflowStore.getState().nodes.length;

          const applyDraftToCanvas = () => {
            try {
              useWorkflowStore.getState().loadWorkflow({ workflow: draftIR });
              navigateTo('editor');
              toast.success(
                'Draft applied to canvas',
                `${stepCount} step${stepCount === 1 ? '' : 's'} ready to edit`,
              );
            } catch {
              toast.error('Could not apply draft', 'The drafted pipeline could not be loaded onto the canvas.');
            }
            setPendingConfirm(null);
          };

          return (
            <ConfirmationCard
              title={canApplyToCanvas ? 'Pipeline drafted' : 'Draft created'}
              summary={
                canApplyToCanvas
                  ? `The agent drafted a ${stepCount}-step pipeline${draftIR!.name ? ` ("${draftIR!.name}")` : ''}. Review it below, then apply it to the canvas to start editing — or cancel to discard.`
                  : `The agent created a draft via ${pendingConfirm.safeWriteStep.tool_name}. Review the changes below, then acknowledge to keep the draft or cancel to discard.`
              }
              tier={pendingConfirm.safeWriteStep.tool_tier as 'safe_write'}
              details={[
                { label: 'Tool', value: pendingConfirm.safeWriteStep.tool_name },
                { label: 'Outcome', value: pendingConfirm.safeWriteStep.outcome },
                { label: 'Latency', value: `${pendingConfirm.safeWriteStep.latency_ms} ms` },
              ]}
              warnings={
                canApplyToCanvas && existingNodeCount > 0
                  ? [`Applying replaces the ${existingNodeCount} node(s) currently on the canvas. Save your current pipeline first if you want to keep it.`]
                  : undefined
              }
              draftId={draftId}
              onConfirm={canApplyToCanvas ? applyDraftToCanvas : () => setPendingConfirm(null)}
              onCancel={() => setPendingConfirm(null)}
              confirmLabel={canApplyToCanvas ? 'Apply to canvas' : 'Keep draft'}
            />
          );
        })()}
        {/* Sentinel: auto-scroll target. The effect above scrollIntoView()s
            this on every new turn / streaming update so the latest message
            stays visible without manual scrolling. */}
        <div ref={messagesEndRef} aria-hidden className="h-px shrink-0" />
      </div>

      <Modal
        isOpen={providerConfigOpen}
        onClose={() => {
          setProviderConfigOpen(false);
          // Saved-from-modal config takes effect on next agent call — pull
          // a fresh status so the header + banner reflect any switch.
          refreshStatus(true);
        }}
        title="AI Provider"
        subtitle="Switch provider, add a new one, or change the active model. Changes also appear in Settings."
        width="lg"
      >
        <AIProviderForm
          mode="user"
          onSaved={() => {
            // Don't auto-close on save — user might want to test connection
            // or tweak more. Refresh the status banner so the header updates
            // immediately when the modal does close.
            refreshStatus(true);
          }}
        />
      </Modal>

      <ConfirmDialog
        isOpen={pendingDelete !== null}
        title="Remove Ollama model?"
        message={
          pendingDelete && (
            <>
              <p>
                You&rsquo;re about to remove <code className="font-mono bg-slate-100 px-1.5 py-0.5 rounded text-[12px]">{pendingDelete.name}</code> from
                this machine.
              </p>
              <p className="mt-2 text-slate-600 text-sm">
                This frees{' '}
                <span className="font-semibold text-slate-900">{fmtBytes(pendingDelete.size)}</span>{' '}
                of disk space. You can pull it again later from the picker &mdash; the data is just the
                model weights, not your pipelines.
              </p>
            </>
          )
        }
        confirmLabel="Remove"
        cancelLabel="Keep it"
        variant="danger"
        busy={deletingModel === pendingDelete?.name}
        onConfirm={handleConfirmDelete}
        onCancel={handleCancelDelete}
      />

      <ConfirmDialog
        isOpen={clearConfirmOpen}
        title="Clear this conversation?"
        message={
          <p className="text-slate-700 text-sm">
            Removes the current chat from this dock. The agent's history of
            past runs is kept on the History tab.
          </p>
        }
        confirmLabel="Clear chat"
        cancelLabel="Keep it"
        variant="danger"
        onConfirm={() => {
          clearTurns();
          setClearConfirmOpen(false);
        }}
        onCancel={() => setClearConfirmOpen(false)}
      />

      <div className="border-t border-slate-100 p-3 shrink-0 relative">
        {/* Prompt-type help popover \u2014 opens via the "?" button in the input.
            Lists the kinds of things the Copilot can do, with one-tap examples. */}
        {promptHelpOpen && (
          <div className="absolute bottom-full left-3 right-3 mb-1 bg-white border border-slate-200 rounded-xl shadow-lg overflow-hidden z-20 max-h-80 overflow-y-auto">
            <div className="px-3 py-2 bg-gradient-to-r from-indigo-50 to-purple-50 border-b border-slate-100 flex items-center justify-between">
              <div className="text-xs uppercase tracking-wider font-bold text-slate-700">
                What can I ask?
              </div>
              <button
                type="button"
                onClick={() => setPromptHelpOpen(false)}
                aria-label="Close prompt help"
                className="text-slate-400 hover:text-slate-700"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>
            <div className="p-2 space-y-2 text-[12px]">
              {([
                {
                  group: 'Ask',
                  tone: 'indigo',
                  items: [
                    'List my pipelines',
                    'Give me an overview of this workspace',
                    'How do I add a new connection?',
                  ],
                },
                {
                  group: 'Diagnose',
                  tone: 'red',
                  items: [
                    'Why did my last pipeline fail?',
                    'What broke in the last 24 hours?',
                    'Why is this run slower than usual?',
                  ],
                },
                {
                  group: 'Build',
                  tone: 'emerald',
                  items: [
                    'Build me a pipeline that loads orders.csv and filters by status',
                    'What node should I add next?',
                    'Suggest optimizations for this expression',
                  ],
                },
                {
                  group: 'Slash commands',
                  tone: 'slate',
                  items: ['/explain', '/diagnose', '/sql', '/fix', '/cost', '/help'],
                },
              ] as const).map((cat) => (
                <div key={cat.group}>
                  <div className={`text-[9px] uppercase tracking-wider font-bold mb-1 ${
                    cat.tone === 'red' ? 'text-red-600'
                    : cat.tone === 'emerald' ? 'text-emerald-600'
                    : cat.tone === 'slate' ? 'text-slate-500'
                    : 'text-indigo-600'
                  }`}>
                    {cat.group}
                  </div>
                  <div className="space-y-1">
                    {cat.items.map((ex) => (
                      <button
                        key={ex}
                        type="button"
                        onClick={() => {
                          setInput(ex.startsWith('/') ? ex + ' ' : ex);
                          setPromptHelpOpen(false);
                        }}
                        className={`w-full text-left px-2 py-1.5 rounded-md text-[11.5px] transition-colors ${
                          ex.startsWith('/')
                            ? 'font-mono bg-slate-50 hover:bg-indigo-50 text-slate-700'
                            : 'hover:bg-indigo-50 text-slate-700'
                        }`}
                      >
                        {ex}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
              <div className="text-xs text-slate-400 px-2 pt-1 border-t border-slate-100">
                Tip \u00b7 type <code className="font-mono bg-slate-100 px-1 rounded">/</code> in the input for the full command list.
              </div>
            </div>
          </div>
        )}

        {/* Slash-command palette \u2014 opens when input begins with "/" and the
            user hasn't typed a space yet. Click a command to expand it into
            the input. Mirrors Microsoft 365 Copilot's "/" shortcut menu. */}
        {(() => {
          const trimmed = input.trimStart();
          if (!trimmed.startsWith('/') || trimmed.includes(' ')) return null;
          const query = trimmed.slice(1).toLowerCase();
          // When a code surface is active (transform/SQL node selected),
          // auto-include a snippet of the active expression so /sql /fix
          // /explain-code commands have something to act on.
          const codeCtx = pageCtx?.code;
          const activeExpr = codeCtx?.expression || '';
          const exprPreview = activeExpr ? `\n\n\`\`\`${codeCtx?.language || 'sql'}\n${activeExpr.slice(0, 1000)}\n\`\`\`` : '';
          const lastErr = codeCtx?.last_error || '';

          const COMMANDS: { cmd: string; label: string; expand: string; tone: string; disabled?: boolean }[] = [
            { cmd: '/explain',  label: 'Explain this page',                expand: 'Explain what I can do on this page.', tone: 'indigo' },
            { cmd: '/list',     label: 'List items',                       expand: 'List the items on this page.', tone: 'indigo' },
            { cmd: '/summarize', label: 'Summarize selection / page',      expand: 'Summarize what I have selected on this page.', tone: 'indigo' },
            { cmd: '/diagnose', label: 'Diagnose latest failure',          expand: 'Diagnose the most recent failed pipeline run.', tone: 'red' },
            { cmd: '/health',   label: 'Workspace health check',           expand: 'Give me a one-paragraph health summary for this workspace.', tone: 'amber' },
            // Code-context commands — only pre-fill code when an editor is active
            {
              cmd: '/sql',
              label: activeExpr ? 'Help me write SQL (uses active node)' : 'Help me write SQL (open a SQL/transform node first)',
              expand: activeExpr
                ? `Help me write SQL. My current expression is below. I want to: ${exprPreview}`
                : 'Help me write SQL for: ',
              tone: 'emerald',
              disabled: !activeExpr,
            },
            {
              cmd: '/fix',
              label: lastErr ? 'Fix this error in my code' : (activeExpr ? 'Fix issues in active expression' : 'Fix code (no active expression)'),
              expand: lastErr
                ? `Fix this error in my SQL/expression: ${lastErr}${exprPreview}`
                : (activeExpr ? `Review and fix any issues in this expression:${exprPreview}` : 'Fix this code — paste the error: '),
              tone: 'red',
              disabled: !activeExpr && !lastErr,
            },
            {
              cmd: '/explain-code',
              label: activeExpr ? 'Explain this expression in plain English' : 'Explain code (open a SQL/transform node first)',
              expand: activeExpr
                ? `Explain in plain English what this does:${exprPreview}`
                : 'Explain this code: ',
              tone: 'emerald',
              disabled: !activeExpr,
            },
            {
              cmd: '/optimize',
              label: activeExpr ? 'Suggest optimizations for the active expression' : 'Optimize SQL (open a SQL/transform node first)',
              expand: activeExpr
                ? `Suggest optimizations for this expression. Preserve correctness, prefer cheaper alternatives:${exprPreview}`
                : 'Suggest optimizations for: ',
              tone: 'emerald',
              disabled: !activeExpr,
            },
            { cmd: '/cost',     label: 'Estimate cost of a query',         expand: 'Estimate the cost (rows + duration) of: ', tone: 'amber' },
            { cmd: '/help',     label: 'What can the Copilot do?',         expand: 'What can you help me with on this page?', tone: 'slate' },
          ];
          const matches = COMMANDS.filter(c => c.cmd.toLowerCase().startsWith('/' + query));
          if (matches.length === 0) return null;
          return (
            <div className="absolute bottom-full left-3 right-3 mb-1 bg-white border border-slate-200 rounded-lg shadow-lg overflow-hidden z-10 max-h-72 overflow-y-auto">
              <div className="px-3 py-1.5 bg-slate-50 border-b border-slate-100 text-xs uppercase tracking-wider font-bold text-slate-500">
                Commands
              </div>
              {matches.map((m) => (
                <button
                  key={m.cmd}
                  type="button"
                  disabled={m.disabled}
                  onClick={() => {
                    if (m.disabled) return;
                    setInput(m.expand);
                  }}
                  className={`w-full text-left px-3 py-2 flex items-center justify-between gap-2 text-[12px] border-b border-slate-50 last:border-b-0 ${
                    m.disabled
                      ? 'opacity-40 cursor-not-allowed'
                      : 'hover:bg-indigo-50'
                  }`}
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <code
                      className={`shrink-0 font-mono text-xs px-1.5 py-0.5 rounded font-bold ${
                        m.tone === 'red' ? 'bg-red-100 text-red-700'
                        : m.tone === 'amber' ? 'bg-amber-100 text-amber-700'
                        : m.tone === 'slate' ? 'bg-slate-100 text-slate-700'
                        : 'bg-indigo-100 text-indigo-700'
                      }`}
                    >
                      {m.cmd}
                    </code>
                    <span className="text-slate-700 truncate">{m.label}</span>
                  </div>
                  <span className="text-xs text-slate-400 shrink-0">\u21b5</span>
                </button>
              ))}
            </div>
          );
        })()}

        {/* Persistent fast-lane chips — appear above the Ask input whenever
            the user has cleared it AND a chat history exists. Empty-state
            (turns.length === 0) already shows the larger suggestion buttons
            above, so we skip there to avoid duplication. Each chip routes
            into the fast-lane intent map (see backend/fpulse/ai/fast_router.py),
            so clicking returns a sub-second answer without the LLM loop. */}
        {turns.length > 0 && !busy && !input.trim() && suggestions.length > 0 && (
          <div
            className="flex gap-1.5 overflow-x-auto px-1 pb-2 -mb-1 scrollbar-thin"
            role="list"
            aria-label="Suggested prompts"
          >
            {suggestions.slice(0, 4).map((s, i) => (
              <button
                key={`chip-${i}`}
                type="button"
                role="listitem"
                onClick={() => useSuggestion(s)}
                title={s.prompt}
                className="shrink-0 inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded-full border border-slate-200 bg-white text-slate-700 hover:bg-indigo-50 hover:border-indigo-300 hover:text-indigo-800 transition-colors"
              >
                <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <polyline points="9 18 15 12 9 6" />
                </svg>
                <span className="truncate max-w-[160px]">{s.label}</span>
              </button>
            ))}
          </div>
        )}

        <form
          onSubmit={(e) => {
            e.preventDefault();
            handleSubmit();
          }}
          className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white shadow-sm focus-within:border-indigo-400 focus-within:ring-2 focus-within:ring-indigo-100 transition-all px-1.5 py-1"
        >
          <button
            type="button"
            onClick={() => setPromptHelpOpen((v) => !v)}
            disabled={!status?.available}
            aria-label="What can I ask?"
            title="What can I ask?"
            className={`shrink-0 inline-flex items-center justify-center w-8 h-8 rounded-full transition-colors ${
              promptHelpOpen
                ? 'bg-indigo-100 text-indigo-700'
                : 'text-slate-400 hover:text-indigo-600 hover:bg-indigo-50'
            } disabled:opacity-40`}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <circle cx="12" cy="12" r="10" />
              <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
              <line x1="12" y1="17" x2="12.01" y2="17" />
            </svg>
          </button>
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && input.trimStart().startsWith('/') && !input.includes(' ')) {
                // Treat lone command as the literal /help / /explain etc. \u2014 let it fall through
              }
            }}
            disabled={busy || !status?.available}
            placeholder={status?.available ? 'Ask the Copilot, or type / for commands\u2026' : 'AI not configured'}
            className="flex-1 min-w-0 px-1 py-2 text-sm bg-transparent border-0 focus:outline-none disabled:text-slate-400 placeholder:text-slate-400"
          />
          {busy ? (
            <button
              type="button"
              onClick={handleStop}
              title="Stop the in-flight agent run"
              className="shrink-0 px-3.5 h-9 text-sm font-semibold text-white bg-red-600 rounded-full hover:bg-red-700 inline-flex items-center justify-center gap-1.5 transition-colors"
            >
              <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
                <rect x="1" y="1" width="8" height="8" rx="1" fill="currentColor" />
              </svg>
              {elapsedSec > 0 ? `Stop \u00b7 ${elapsedSec}s` : 'Stop'}
            </button>
          ) : (
            <button
              type="submit"
              disabled={!input.trim() || !status?.available}
              aria-label="Send"
              className="shrink-0 inline-flex items-center justify-center w-9 h-9 rounded-full bg-gradient-to-br from-indigo-500 to-purple-500 text-white shadow-sm hover:from-indigo-600 hover:to-purple-600 hover:shadow-md disabled:opacity-40 disabled:hover:from-indigo-500 disabled:hover:to-purple-500 disabled:shadow-sm transition-all"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <line x1="22" y1="2" x2="11" y2="13" />
                <polygon points="22 2 15 22 11 13 2 9 22 2" />
              </svg>
            </button>
          )}
        </form>
      </div>
    </div>
  );
}
