import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import { getAgentStatus, refreshAgentProvider } from '../../api/agent';
import { getOllamaStatus, OllamaModel, pullOllamaModel } from '../../api/ollama';
import { uiConfirm } from '../../ui/dialog';
import { isOllamaToolCapable } from '../../util/aiModels';
import OpenRouterModelPicker from './OpenRouterModelPicker';
import CuratedModelPicker from './CuratedModelPicker';
import { getCuratedCatalog } from '../../data/providerCatalog';

// Recommended Ollama models surfaced inline on the AI Provider form
// (May 3 2026; floor revised 2026-05-19). Lets users on Insights →
// AI Provider pull the right model without having to open the Copilot
// dock. The 2026-05-19 revision raised the CPU floor to ~7B — sub-7B
// Qwen 2.5 models advertise tool schemas but can't reliably drive the
// agent loop.
const RECOMMENDED_OLLAMA_MODELS: { name: string; sizeGB: number; runtime: 'cpu' | 'gpu'; note: string }[] = [
  { name: 'qwen2.5:7b', sizeGB: 4.7, runtime: 'cpu', note: 'CPU pick (floor) — reliable tool-use, ~6 GB RAM at Q4_K_M, 30–60 s/turn' },
  { name: 'llama3.1:8b', sizeGB: 4.9, runtime: 'cpu', note: 'CPU alternative at the floor — strong tool-use' },
  { name: 'phi-4',      sizeGB: 9.0, runtime: 'cpu', note: 'CPU alternative — 14B equivalent perf, needs more RAM' },
  { name: 'qwen2.5:14b', sizeGB: 8.4, runtime: 'gpu', note: 'GPU pick — sub-3-s tool-use on a 12 GB+ GPU' },
];

/**
 * AIProviderForm — shared form for AI provider configuration.
 *
 * Used in two places:
 *   • AccountPage → mode="user"       — Free/OSS tier, per-user row
 *   • AdminPage   → mode="workspace"  — Plus tier, workspace-wide row
 *
 * Workspace mode adds two governance fields (allow_user_override,
 * monthly_budget_usd) and binds to /api/ai/config/workspace. User mode
 * binds to /api/ai/config/me.
 *
 * Key handling: the backend never returns the plaintext key. The server
 * response carries `has_key: boolean`. On this form, leaving the key
 * field blank on save means "keep existing key". Typing a new value
 * replaces it. The "Clear key" button explicitly zeroes it.
 */

type Mode = 'user' | 'workspace';

interface Props {
  mode: Mode;
  /** Disable all inputs — shown to non-admin users when Plus admin has
   *  locked per-user overrides, or to anyone when the backend reports
   *  the config store is unavailable. */
  disabled?: boolean;
  /** Shown above the form — "Your admin disabled this" or similar. */
  disabledReason?: string;
  /** Called after a successful Save. Used by parents that want to close
   *  a containing modal once the user has applied changes. */
  onSaved?: () => void;
}

interface ProviderDef {
  id: string;
  label: string;
  defaultModel: string;
  needsBaseUrl: boolean;
  needsKey: boolean;
  docLink?: string;
}

// Z38 (2026-05-23) — per-provider accent color used by the gallery
// cards (color tile = first two letters of the provider id on this
// background). Hues track each vendor's brand without bundling logo
// SVGs for every option. Custom / unknown providers fall back to
// slate.
const PROVIDER_ACCENTS: Record<string, string> = {
  azure:      '#0078d4', // Azure blue
  claude:     '#cc785c', // Anthropic terracotta
  custom:     '#64748b', // neutral slate
  deepseek:   '#4d6bfe', // DeepSeek indigo
  gemini:     '#1a73e8', // Google blue
  groq:       '#f55036', // Groq orange-red
  mistral:    '#fa520f', // Mistral orange
  ollama:     '#0ea5e9', // sky blue — local-first vibe
  openai:     '#10a37f', // OpenAI green
  openrouter: '#8b5cf6', // OpenRouter purple
};

// 2026-05-23 — provider list ordered A→Z by label so the dropdown reads
// like a phone book; previously hand-ordered "cloud-first" entries
// forced users to scan ten lines to find their provider. OpenRouter
// keeps its inline comment because the model-id convention is
// non-obvious.
const PROVIDERS: ProviderDef[] = [
  { id: 'azure',      label: 'Azure OpenAI',        defaultModel: '',                          needsBaseUrl: true,  needsKey: true,  docLink: 'https://portal.azure.com/' },
  { id: 'claude',     label: 'Claude (Anthropic)',  defaultModel: 'claude-haiku-4-5-20251001', needsBaseUrl: false, needsKey: true,  docLink: 'https://console.anthropic.com/' },
  { id: 'custom',     label: 'Custom (OpenAI-compatible)', defaultModel: '', needsBaseUrl: true, needsKey: true },
  { id: 'deepseek',   label: 'DeepSeek',            defaultModel: 'deepseek-chat',             needsBaseUrl: false, needsKey: true,  docLink: 'https://platform.deepseek.com/' },
  { id: 'gemini',     label: 'Google Gemini',       defaultModel: 'gemini-2.0-flash',          needsBaseUrl: false, needsKey: true,  docLink: 'https://aistudio.google.com/app/apikey' },
  { id: 'groq',       label: 'Groq (fast)',         defaultModel: 'llama-3.3-70b-versatile',   needsBaseUrl: false, needsKey: true,  docLink: 'https://console.groq.com/keys' },
  { id: 'mistral',    label: 'Mistral',             defaultModel: 'mistral-small-latest',      needsBaseUrl: false, needsKey: true,  docLink: 'https://console.mistral.ai/' },
  { id: 'ollama',     label: 'Ollama (local)',      defaultModel: 'llama3',                    needsBaseUrl: true,  needsKey: false, docLink: 'https://ollama.com/' },
  { id: 'openai',     label: 'OpenAI',              defaultModel: 'gpt-4o-mini',               needsBaseUrl: false, needsKey: true,  docLink: 'https://platform.openai.com/api-keys' },
  // OpenRouter — single key, 100+ models. Model id MUST be namespaced
  // (e.g. "openai/gpt-4o-mini", "anthropic/claude-sonnet-4",
  // "meta-llama/llama-3.1-70b-instruct").
  { id: 'openrouter', label: 'OpenRouter (100+ models)', defaultModel: 'openai/gpt-4o-mini', needsBaseUrl: false, needsKey: true,  docLink: 'https://openrouter.ai/keys' },
];

interface TestResult {
  ok: boolean;
  latency_ms: number;
  detail: string;
}

interface SavedState {
  enabled: boolean;
  provider: string;
  model: string;
  base_url: string;
  has_key: boolean;
  // v32 — id of a Credentials-store row the key is imported from ('' = inline).
  credential_id?: string;
  // workspace-only
  allow_user_override?: boolean;
  monthly_budget_usd?: number;
}

// Minimal shape of a credential as returned by GET /api/credentials
// (secrets are masked there — we only need id/name/type for the picker).
interface CredentialOption {
  id: string;
  name: string;
  type?: string;
}

export default function AIProviderForm({ mode, disabled, disabledReason, onSaved }: Props) {
  // ── State ─────────────────────────────────────────────────────────────
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<SavedState | null>(null);

  // Form-local state (may differ from `saved` until user hits Save)
  const [enabled, setEnabled] = useState(false);
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [allowUserOverride, setAllowUserOverride] = useState(false);
  const [monthlyBudget, setMonthlyBudget] = useState(0);
  const [clearKeyIntent, setClearKeyIntent] = useState(false);

  // v32 — key source: type it inline here, or import it from a saved
  // credential in the central Credentials store (one governed home for
  // the secret). `credentials` backs the picker; loaded once on mount.
  const [keySource, setKeySource] = useState<'inline' | 'credential'>('inline');
  const [credentialId, setCredentialId] = useState('');
  const [credentials, setCredentials] = useState<CredentialOption[]>([]);

  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [saveMsg, setSaveMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  // Admin-lock state (user mode only): when the workspace admin has
  // configured a workspace-wide AI provider AND disabled per-user
  // overrides, this form becomes read-only. We still render the form so
  // the user can see WHAT the admin has chosen, but every input is
  // disabled and an explanatory banner sits above.
  const [adminLocked, setAdminLocked] = useState(false);
  const [adminLockReason, setAdminLockReason] = useState('');

  // Live state from the agent + Ollama side. Used by:
  //  - "Use what's connected" pre-fill button
  //  - Ollama installed-models dropdown (when provider === 'ollama')
  const [liveAgent, setLiveAgent] = useState<{ available: boolean; provider: string; model: string } | null>(null);
  const [ollamaModels, setOllamaModels] = useState<OllamaModel[]>([]);

  // Inline pull state for the recommended-models section (May 3 2026;
  // extended 2026-05-22 to also drive a "Pull a custom model" input
  // so users can fetch any Ollama model from this page — not just the
  // hardcoded recommendation set).
  const [pullingModel, setPullingModel] = useState<string | null>(null);
  const [pullProgress, setPullProgress] = useState<{ status?: string; total?: number; completed?: number } | null>(null);
  const [pullError, setPullError] = useState<string | null>(null);
  const [customModelInput, setCustomModelInput] = useState('');

  // Shared pull driver. Used by both the recommended-models row and the
  // custom-model input. Refreshes the installed list when complete and
  // auto-selects the freshly-pulled model.
  const handlePullModel = async (name: string) => {
    const trimmed = (name || '').trim();
    if (!trimmed) {
      setPullError('Type an Ollama model name (e.g. "qwen2.5:7b" or "mistral-nemo")');
      return;
    }
    setPullingModel(trimmed);
    setPullProgress(null);
    setPullError(null);
    try {
      await pullOllamaModel(trimmed, (p) => {
        setPullProgress({ status: p.status, total: p.total, completed: p.completed });
      });
      // Refresh installed list and select the new model.
      const o = await getOllamaStatus();
      if (o.running) {
        setOllamaModels(o.models);
        // Find the actual installed name (Ollama may append `:latest` if
        // the user typed the bare model id).
        const head = trimmed.split(':')[0].toLowerCase();
        const match = o.models.find((m) => m.name.split(':')[0].toLowerCase() === head);
        if (match) setModel(match.name);
        else setModel(trimmed);
      }
      // Clear the custom input once a pull succeeds so the user can
      // queue another one immediately if they want a multi-model setup.
      setCustomModelInput('');
    } catch (err: any) {
      setPullError(err?.message || 'Pull failed');
    } finally {
      setPullingModel(null);
      setPullProgress(null);
    }
  };

  // Back-compat alias — keeps the existing call sites in the recommended
  // models loop pointing at the same driver.
  const handlePullRecommended = handlePullModel;

  const providerDef = PROVIDERS.find((p) => p.id === provider);

  // ── Load current config ───────────────────────────────────────────────
  const reload = async () => {
    setLoading(true);
    setError(null);
    try {
      const resp: any = mode === 'user'
        ? await api.get('/api/ai/config/me')
        : await api.get('/api/ai/config/workspace');

      const row: SavedState = mode === 'user' ? resp.user : resp.workspace;
      setSaved(row);
      setEnabled(row.enabled);
      setProvider(row.provider);
      setModel(row.model);
      setBaseUrl(row.base_url);
      setAllowUserOverride(!!row.allow_user_override);
      setMonthlyBudget(row.monthly_budget_usd || 0);
      setApiKey('');
      setClearKeyIntent(false);
      setCredentialId(row.credential_id || '');
      setKeySource(row.credential_id ? 'credential' : 'inline');
      setTestResult(null);

      // On the user form, detect the Plus-admin-locked state so we can
      // render the whole form read-only instead of letting the user
      // submit changes the backend will 403.
      if (mode === 'user') {
        const isPlus = !!resp.is_plus;
        const wsEnabled = !!resp.workspace_enabled;
        const overrideAllowed = !!resp.workspace_allows_override;
        const locked = isPlus && wsEnabled && !overrideAllowed;
        setAdminLocked(locked);
        setAdminLockReason(
          locked
            ? 'Your workspace admin has set a shared AI provider and disabled per-user overrides. The configuration below is read-only. Ask your admin to enable "Allow users to override" in Admin → AI if you need to use your own provider.'
            : ''
        );
      } else {
        setAdminLocked(false);
        setAdminLockReason('');
      }
    } catch (e: any) {
      setError(e?.message || 'Failed to load AI configuration.');
    }
    setLoading(false);
  };

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

  // Load saved credentials so the user can import a key instead of typing
  // it inline (v32). Best-effort — the inline path still works if this
  // fails or the user has no credentials yet. AI-provider-tagged
  // credentials sort first so they're easy to find.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list: any = await api.get('/api/credentials');
        if (cancelled || !Array.isArray(list)) return;
        const opts: CredentialOption[] = list
          .map((c: any) => ({ id: c.id, name: c.name, type: c.type }))
          .filter((c: CredentialOption) => !!c.id);
        opts.sort((a, b) => {
          const aAi = a.type === 'ai_provider' ? 0 : 1;
          const bAi = b.type === 'ai_provider' ? 0 : 1;
          if (aAi !== bAi) return aAi - bAi;
          return (a.name || '').localeCompare(b.name || '');
        });
        setCredentials(opts);
      } catch { /* ignore — inline entry remains available */ }
    })();
    return () => { cancelled = true; };
  }, []);

  // Pull live agent + Ollama state on mount and whenever the form provider
  // changes (so switching to Ollama immediately fetches its model list).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const a = await getAgentStatus();
        if (!cancelled) setLiveAgent({ available: a.available, provider: a.provider, model: a.model });
      } catch {
        if (!cancelled) setLiveAgent(null);
      }
      if (provider === 'ollama' || (!provider && !cancelled)) {
        try {
          const o = await getOllamaStatus();
          if (!cancelled) setOllamaModels(o.running ? o.models : []);
        } catch {
          if (!cancelled) setOllamaModels([]);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [provider]);

  // Pre-fill the form with whatever the agent has actually connected to.
  // Useful when the user installed Ollama via the AI Agent panel and just
  // wants Settings to mirror that without retyping.
  const onUseWhatsConnected = () => {
    if (!liveAgent || !liveAgent.available) return;
    setEnabled(true);
    setProvider(liveAgent.provider);
    setModel(liveAgent.model);
    if (liveAgent.provider === 'ollama') setBaseUrl('http://127.0.0.1:11434');
    setTestResult(null);
  };

  // When the provider changes, preload the default model (unless the user
  // has already typed a custom one) and default base_url hints.
  //
  // Cross-provider id incompatibility: OpenRouter ids always have `org/model`
  // format ("openai/gpt-4o-mini"). Ollama / Claude / OpenAI ids never do. So
  // switching providers carries the previous id forward only when it could
  // plausibly be valid for the new provider — otherwise reset to the default.
  // Without this, switching Ollama→OpenRouter strands the user with "llama3.1:latest"
  // which isn't a valid OpenRouter id and silently fails on save.
  const isLikelyValidFor = (modelId: string, providerId: string): boolean => {
    if (!modelId) return false;
    const hasSlash = modelId.includes('/');
    const hasOllamaTag = modelId.includes(':'); // Ollama tag form, e.g. "llama3.1:8b"
    if (providerId === 'openrouter') return hasSlash;   // ids are "org/model"
    if (providerId === 'ollama') return !hasSlash;      // bare name or name:tag
    if (providerId === 'custom') return true;           // custom gateway — anything goes
    // Cloud providers (openai / claude / gemini / groq / mistral / deepseek /
    // azure / …) never use an Ollama ":tag" or an OpenRouter "org/" prefix, so
    // a leftover Ollama model like "llama3.1:8b" should reset to the default.
    return !hasSlash && !hasOllamaTag;
  };

  const onProviderChange = (id: string) => {
    setProvider(id);
    const def = PROVIDERS.find((p) => p.id === id);
    if (def && (!model || PROVIDERS.some((p) => p.defaultModel === model) || !isLikelyValidFor(model, id))) {
      setModel(def.defaultModel);
    }
    // base_url is provider-specific (Ollama loopback / Azure deployment
    // endpoint / custom gateway). Carrying a leftover URL across a provider
    // switch produces a mismatched config — e.g. selecting Azure while the
    // Ollama URL stays in the field, which then fails Test with a confusing
    // "azure requires base_url". Reset to the new provider's value, but
    // preserve a URL the user actually hand-typed (not a known default).
    const isLeftoverDefault =
      !baseUrl || /^https?:\/\/(127\.0\.0\.1|localhost):11434\/?$/.test(baseUrl.trim());
    if (id === 'ollama') {
      if (isLeftoverDefault) setBaseUrl('http://127.0.0.1:11434');
    } else if (isLeftoverDefault) {
      setBaseUrl('');
    }
    setTestResult(null);
  };

  // ── Save ──────────────────────────────────────────────────────────────
  const onSave = async () => {
    setSaving(true);
    setSaveMsg(null);
    try {
      // v32 — when importing from a credential, don't touch the inline key
      // (the backend clears it and resolves from the credential instead).
      // When entering inline, clear any credential reference so the secret
      // has one home.
      const usingCredential = keySource === 'credential' && !!credentialId;
      const apiKeyPayload: string | null = usingCredential
        ? null
        : (clearKeyIntent ? '' : (apiKey ? apiKey : null)); // null = keep existing
      const credentialPayload: string = usingCredential ? credentialId : '';

      const body: any = {
        enabled,
        provider,
        model,
        base_url: baseUrl,
        api_key: apiKeyPayload,
        credential_id: credentialPayload,
      };
      if (mode === 'workspace') {
        body.allow_user_override = allowUserOverride;
        body.monthly_budget_usd = Number(monthlyBudget) || 0;
      }

      const path = mode === 'user' ? '/api/ai/config/me' : '/api/ai/config/workspace';
      const resp: any = await api.put(path, body);
      const row: SavedState = mode === 'user' ? resp.user : resp.workspace;
      setSaved(row);
      setApiKey('');
      setClearKeyIntent(false);
      setCredentialId(row.credential_id || '');
      setKeySource(row.credential_id ? 'credential' : 'inline');
      setSaveMsg({ type: 'ok', text: 'Saved.' });

      // Reset the backend's autoprobe + per-process provider cache so the
      // newly-saved config is read fresh on the very next agent call.
      // Best-effort — if the endpoint isn't reachable, the regular 5-min
      // cache TTL will still pick up the change eventually.
      try {
        await refreshAgentProvider();
      } catch { /* ignore */ }

      // Re-fetch the live agent status locally so the "auto-detected" banner
      // either disappears (matches the saved config) or updates (if the
      // detected provider differs from what was just saved).
      try {
        const a = await getAgentStatus();
        setLiveAgent({ available: a.available, provider: a.provider, model: a.model });
      } catch { /* ignore */ }

      // Broadcast across the app so any other mounted component (most
      // importantly the floating Copilot dock's AgentChatPanel) refreshes
      // its status header without a full page reload.
      try {
        window.dispatchEvent(new CustomEvent('fpulse:ai_config_changed', {
          detail: { provider: row.provider, model: row.model },
        }));
      } catch { /* ignore */ }

      onSaved?.();
    } catch (e: any) {
      setSaveMsg({ type: 'err', text: e?.message || 'Save failed.' });
    }
    setSaving(false);
  };

  // ── Test connection (dry-run) ─────────────────────────────────────────
  const onTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const body: any = { scope: mode };
      // Only include fields that differ from saved OR if the form has a
      // brand-new key typed in. The backend falls back to the stored row
      // for any omitted field.
      if (provider !== saved?.provider) body.provider = provider;
      if (model !== saved?.model) body.model = model;
      if (baseUrl !== saved?.base_url) body.base_url = baseUrl;
      if (apiKey) body.api_key = apiKey;
      else body.api_key = null; // signal "use stored"

      // If nothing changed and the form has a key-less state, still send
      // provider so the backend knows which stored config to probe.
      if (!body.provider) body.provider = provider;

      const resp = await api.post<TestResult>('/api/ai/config/test', body);
      setTestResult(resp);
    } catch (e: any) {
      setTestResult({ ok: false, latency_ms: 0, detail: e?.message || 'Test failed' });
    }
    setTesting(false);
  };

  // ── Delete (revert to defaults) ───────────────────────────────────────
  const onReset = async () => {
    if (!(await uiConfirm({
      message: mode === 'user'
        ? 'Delete your AI provider config? You will fall back to workspace or env defaults.'
        : 'Delete the workspace-wide AI provider config? Users will fall back to their own or env defaults.',
      danger: true,
      confirmLabel: 'Delete',
    }))) return;
    setSaving(true);
    try {
      const path = mode === 'user' ? '/api/ai/config/me' : '/api/ai/config/workspace';
      await api.delete(path);
      await reload();
      setSaveMsg({ type: 'ok', text: 'Configuration cleared.' });
    } catch (e: any) {
      setSaveMsg({ type: 'err', text: e?.message || 'Reset failed.' });
    }
    setSaving(false);
  };

  // ── Render ────────────────────────────────────────────────────────────
  if (loading) {
    return <div className="text-sm text-slate-400 py-8 text-center">Loading AI configuration…</div>;
  }

  if (error) {
    return (
      <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-xs text-red-700">
        {error}
      </div>
    );
  }

  const readOnly = !!disabled || adminLocked;
  const isOllama = provider === 'ollama';
  const isOpenRouter = provider === 'openrouter';
  // Curated catalog is the static-list path used by Claude / OpenAI /
  // Gemini / DeepSeek / Groq / Mistral. Returns null for providers that
  // don't have a catalog (Azure / Custom keep the free-text input).
  const curatedCatalog = getCuratedCatalog(provider);
  const hasCuratedCatalog = curatedCatalog !== null && curatedCatalog.length > 0;
  const canTest = !readOnly && !!provider && !testing;

  // Pick the first applicable reason — caller-supplied takes precedence
  // (e.g. a page could disable for any other reason), otherwise show the
  // admin-lock banner the form discovered on its own.
  const effectiveReason = (disabled && disabledReason) || (adminLocked && adminLockReason) || '';

  // Show the "Use what's connected" hint when the agent has auto-detected a
  // provider that differs from what the form currently has. Hidden when the
  // form already matches the live state (no action needed).
  const showUseConnectedHint =
    !readOnly
    && liveAgent !== null
    && liveAgent.available
    && (provider !== liveAgent.provider || model !== liveAgent.model);

  return (
    <div className="space-y-5">
      {showUseConnectedHint && (
        <div className="rounded-lg bg-emerald-50 border border-emerald-200 p-3 flex items-center justify-between gap-3">
          <div className="text-xs text-emerald-900 leading-snug">
            <span className="font-semibold">F-Pulse auto-detected</span>{' '}
            <code className="font-mono bg-white border border-emerald-200 px-1 rounded">
              {liveAgent.provider}{liveAgent.model ? ' / ' + liveAgent.model : ''}
            </code>
            {' '}from your AI Agent panel. Want to mirror it here?
          </div>
          <button
            type="button"
            onClick={onUseWhatsConnected}
            className="shrink-0 px-3 py-1.5 text-xs font-semibold text-white bg-emerald-600 hover:bg-emerald-700 rounded-lg transition-colors"
          >
            Use what&rsquo;s connected
          </button>
        </div>
      )}

      {readOnly && effectiveReason && (
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 text-xs text-amber-800 flex items-start gap-2">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 mt-0.5">
            <rect x="3" y="11" width="18" height="11" rx="2" />
            <path d="M7 11V7a5 5 0 0 1 10 0v4" />
          </svg>
          <div>
            <strong>Read-only.</strong> {effectiveReason}
          </div>
        </div>
      )}

      {/* Master enable toggle */}
      <div className="flex items-center justify-between p-4 border border-slate-200 rounded-lg bg-slate-50">
        <div>
          <div className="text-sm font-bold text-slate-800">Enable AI</div>
          <div className="text-xs text-slate-500 mt-0.5">
            When off, all AI features return deterministic fallbacks. F-Pulse
            remains fully functional without an LLM.
          </div>
        </div>
        <button
          type="button"
          onClick={() => setEnabled(!enabled)}
          disabled={readOnly}
          className={`relative inline-flex h-6 w-11 items-center rounded-full transition ${
            enabled ? 'bg-emerald-500' : 'bg-slate-300'
          } ${readOnly ? 'opacity-50 cursor-not-allowed' : ''}`}
        >
          <span
            className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition ${
              enabled ? 'translate-x-5' : 'translate-x-0.5'
            }`}
          />
        </button>
      </div>

      {/* Z38 (2026-05-23) — provider gallery replaces the single-line
          select. Cards expose the canonical identity (color tile +
          name), capability (needs API key vs local), and a "selected"
          ring on the active one. Pricing is intentionally NOT inlined
          on the card yet (defers a /api/ai/providers/compare fetch
          here; the existing PriceComparison table on the same page
          already covers detailed numbers and stays available below).
          The "Get an API key →" link moved next to the chosen card
          on the status sidebar — see ActiveProviderRecap below. */}
      <Field label="Provider">
        <div
          role="radiogroup"
          aria-label="AI provider"
          className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-2"
        >
          {PROVIDERS.map((p) => {
            const selected = provider === p.id;
            const disabled = readOnly || !enabled;
            const isLocal = p.id === 'ollama' || p.id === 'custom';
            return (
              <button
                key={p.id}
                type="button"
                role="radio"
                aria-checked={selected}
                disabled={disabled}
                onClick={() => onProviderChange(p.id)}
                className={`group relative text-left rounded-lg border p-2.5 transition-all ${
                  selected
                    ? 'border-violet-400 bg-violet-50 ring-2 ring-violet-200'
                    : 'border-slate-200 bg-white hover:border-violet-300 hover:bg-violet-50/40'
                } ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
              >
                <div className="flex items-center gap-2">
                  {/* Color tile — first two letters of the provider's
                      id, on a per-provider hue so the cards have visual
                      identity without bundling logo SVGs for every
                      provider. Cheap, recognizable. */}
                  <span
                    aria-hidden="true"
                    className="inline-flex h-7 w-7 items-center justify-center rounded-md text-[10px] font-bold text-white shrink-0"
                    style={{ background: PROVIDER_ACCENTS[p.id] || '#64748b' }}
                  >
                    {p.id.slice(0, 2).toUpperCase()}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="text-xs font-bold text-slate-800 truncate">{p.label}</div>
                    <div className="text-[10px] font-semibold uppercase tracking-wider mt-0.5">
                      {isLocal ? (
                        <span className="text-emerald-700">Local</span>
                      ) : p.needsKey ? (
                        <span className="text-slate-500">Needs API key</span>
                      ) : (
                        <span className="text-slate-500">Cloud</span>
                      )}
                    </div>
                  </div>
                </div>
                {selected && (
                  <span
                    className="absolute top-1.5 right-1.5 inline-flex h-4 w-4 items-center justify-center rounded-full bg-violet-600 text-white"
                    aria-hidden="true"
                  >
                    <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round">
                      <polyline points="20 6 9 17 4 12" />
                    </svg>
                  </span>
                )}
              </button>
            );
          })}
        </div>
        {/* Only link to provider docs when an API key is actually needed.
            Ollama et al. have no API keys — pre-fix-#1 this rendered as
            "Get an API key →" pointing at ollama.com which was misleading. */}
        {providerDef?.docLink && providerDef.needsKey && (
          <a
            href={providerDef.docLink}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs font-medium text-violet-700 hover:text-violet-900 underline underline-offset-2 mt-2 inline-block"
          >
            Get an API key for {providerDef.label} →
          </a>
        )}
      </Field>

      {/* Model — for Ollama, render as a dropdown of installed models when
          available, falling back to text input. For all other providers it
          stays a free-text input. Tools/Text-only badge appears for Ollama
          regardless. */}
      <Field label="Model">
        {isOpenRouter ? (
          <OpenRouterModelPicker
            value={model}
            onChange={setModel}
            disabled={readOnly || !enabled}
          />
        ) : hasCuratedCatalog ? (
          <CuratedModelPicker
            provider={provider}
            value={model}
            onChange={setModel}
            disabled={readOnly || !enabled}
          />
        ) : isOllama ? (
          // 2026-05-22: render unconditionally (previously gated on
          // `ollamaModels.length > 0`, which dropped users with zero
          // installed models back to a bare text input with no Pull path).
          // The block now always shows: installed dropdown (or empty
          // state) + recommended-models pull list + a "Pull a custom
          // model" input so users can fetch any Ollama model by id.
          <div className="space-y-1.5">
            {ollamaModels.length > 0 ? (
              <>
                <select
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  disabled={readOnly || !enabled}
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none disabled:bg-slate-50 disabled:text-slate-400 font-mono"
                >
                  {!ollamaModels.find((m) => m.name === model) && model && (
                    <option value={model}>{model} (not installed)</option>
                  )}
                  {ollamaModels.map((m) => (
                    <option key={m.name} value={m.name}>
                      {m.name}{isOllamaToolCapable(m.name) ? '  \u2022  Tools' : '  \u2022  Text only'}
                    </option>
                  ))}
                </select>
                <div className="text-xs text-slate-500">
                  Showing models installed on this Ollama instance.
                </div>
              </>
            ) : (
              // Empty state \u2014 Ollama is reachable (or we couldn't probe)
              // but no models are installed. Surface the situation
              // explicitly rather than falling through to a text input
              // the user has to guess at.
              <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                No models installed on this Ollama instance yet. Pull one of
                the recommended models below, or fetch a custom model by id.
              </div>
            )}

            {/* Recommended models \u2014 inline pull. Lets the user pull
                qwen2.5:7b / llama3.1:8b / phi-4 / qwen2.5:14b without
                leaving this page. Always visible (was previously hidden
                when no models were missing); when ALL recommendations are
                installed, the section collapses to a single-line "all
                installed" status so the operator can see the curated set
                without expanding their existing setup. */}
            {(() => {
              const installed = new Set(ollamaModels.map((m) => m.name.split(':')[0].toLowerCase()));
              const missing = RECOMMENDED_OLLAMA_MODELS.filter((rec) => {
                const head = rec.name.split(':')[0].toLowerCase();
                return !installed.has(head);
              });
              if (missing.length === 0) {
                return (
                  <div className="mt-2 rounded-lg border border-emerald-200 bg-emerald-50/60 px-3 py-2 text-xs text-emerald-800">
                    All recommended models are already installed on this Ollama instance.
                  </div>
                );
              }
              return (
                <div className="mt-2 rounded-lg border border-violet-200 bg-violet-50/50 p-2.5">
                  <div className="text-xs font-bold uppercase tracking-wider text-violet-700 mb-1.5">
                    Recommended models
                  </div>
                  <div className="space-y-1.5">
                    {missing.map((rec) => {
                      const pulling = pullingModel === rec.name;
                      const progress = pulling ? pullProgress : null;
                      const pct =
                        progress?.total && progress?.completed
                          ? Math.min(100, Math.round((progress.completed / progress.total) * 100))
                          : null;
                      return (
                        <div key={rec.name} className="flex items-center gap-2">
                          <code className="text-xs font-mono text-slate-800 shrink-0">{rec.name}</code>
                          <span className={`text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${
                            rec.runtime === 'cpu' ? 'bg-violet-100 text-violet-700' : 'bg-blue-100 text-blue-700'
                          }`}>
                            {rec.runtime === 'cpu' ? 'CPU pick' : 'GPU pick'}
                          </span>
                          <span className="text-xs text-slate-500 truncate flex-1">{rec.note} ({rec.sizeGB} GB)</span>
                          <button
                            type="button"
                            onClick={() => handlePullRecommended(rec.name)}
                            disabled={pulling || !!pullingModel || readOnly}
                            className="text-xs font-bold uppercase tracking-wider px-2.5 py-1 rounded shrink-0 transition-colors disabled:cursor-not-allowed disabled:opacity-60 bg-violet-600 hover:bg-violet-700 text-white"
                          >
                            {pulling
                              ? (pct !== null ? `${pct}%` : (progress?.status || 'Pulling\u2026'))
                              : 'Pull'}
                          </button>
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })()}

            {/* Pull a custom model (2026-05-22). For users who want a
                model that ISN'T in the recommended set \u2014 anything in the
                Ollama library is valid input here (mistral-nemo,
                gemma2:9b, qwen2.5:32b-instruct-q4_K_M, etc.). Empty
                input shows a hint; a non-empty input enables Pull. */}
            <div className="mt-2 rounded-lg border border-slate-200 bg-white p-2.5">
              <div className="text-xs font-bold uppercase tracking-wider text-slate-600 mb-1.5">
                Pull a custom model
              </div>
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={customModelInput}
                  onChange={(e) => setCustomModelInput(e.target.value)}
                  placeholder="e.g. mistral-nemo, gemma2:9b, qwen2.5:32b"
                  disabled={readOnly || !!pullingModel}
                  className="flex-1 px-2.5 py-1.5 text-xs font-mono border border-slate-200 rounded focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none disabled:bg-slate-50"
                />
                <button
                  type="button"
                  onClick={() => handlePullModel(customModelInput)}
                  disabled={readOnly || !customModelInput.trim() || !!pullingModel}
                  className="text-xs font-bold uppercase tracking-wider px-2.5 py-1.5 rounded shrink-0 transition-colors disabled:cursor-not-allowed disabled:opacity-60 bg-slate-700 hover:bg-slate-800 text-white"
                >
                  {pullingModel === customModelInput.trim()
                    ? (() => {
                        const pct = pullProgress?.total && pullProgress?.completed
                          ? Math.min(100, Math.round((pullProgress.completed / pullProgress.total) * 100))
                          : null;
                        return pct !== null ? `${pct}%` : (pullProgress?.status || 'Pulling\u2026');
                      })()
                    : 'Pull'}
                </button>
              </div>
              <div className="mt-1 text-[11px] text-slate-500 leading-snug">
                Any model id from <a className="text-blue-600 hover:underline" href="https://ollama.com/library" target="_blank" rel="noreferrer">ollama.com/library</a> works.
                Reliable agent loops require a ~7B+ model \u2014 see the recommended list above for the floor picks.
              </div>
            </div>

            {pullError && (
              <div className="mt-1.5 text-xs text-red-600">{pullError}</div>
            )}
          </div>
        ) : (
          <input
            type="text"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            disabled={readOnly || !enabled}
            placeholder={providerDef?.defaultModel || 'model-name'}
            className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none disabled:bg-slate-50 disabled:text-slate-400 font-mono"
          />
        )}
        {/* Tools / Text-only badge for Ollama — surfaces text-only mode
            BEFORE the user saves and gets confused by hallucinated tool calls. */}
        {isOllama && model && (
          isOllamaToolCapable(model) ? (
            <div className="mt-1.5 inline-flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider text-emerald-700 bg-emerald-100 px-2 py-0.5 rounded">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" aria-hidden />
              Tool-capable
            </div>
          ) : (
            <div className="mt-1.5 inline-flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider text-amber-800 bg-amber-100 px-2 py-0.5 rounded" title="Model can't call tools — agent will run in chat-only mode">
              <span className="w-1.5 h-1.5 rounded-full bg-amber-500" aria-hidden />
              Text-only (no tools)
            </div>
          )
        )}
      </Field>

      {/* Base URL — only for ollama / azure / custom */}
      {providerDef?.needsBaseUrl && (
        <Field
          label="Base URL"
          hint={isOllama ? 'e.g. http://127.0.0.1:11434 (local) or http://ollama:11434 (docker)' : undefined}
        >
          <input
            type="text"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            disabled={readOnly || !enabled}
            placeholder={isOllama ? 'http://127.0.0.1:11434' : 'https://your-endpoint'}
            className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none disabled:bg-slate-50 disabled:text-slate-400 font-mono"
          />
        </Field>
      )}

      {/* API Key — hidden for Ollama. v32: enter inline OR import from a
          saved credential in the central Credentials store (one governed
          home for the secret). */}
      {providerDef && providerDef.needsKey && (
        <Field
          label="API Key"
          hint={
            keySource === 'credential'
              ? 'Read from the selected credential at request time — never copied here.'
              : (saved?.has_key && !saved?.credential_id
                  ? 'A key is already saved. Leave blank to keep it, or enter a new one to replace.'
                  : 'Stored encrypted at rest.')
          }
        >
          {/* Source toggle — type it here, or reuse a governed secret. */}
          <div className="inline-flex rounded-lg border border-slate-200 p-0.5 bg-slate-50 mb-2">
            {([
              { id: 'inline', label: 'Enter key inline' },
              { id: 'credential', label: 'Use a saved credential' },
            ] as const).map((opt) => (
              <button
                key={opt.id}
                type="button"
                onClick={() => setKeySource(opt.id)}
                disabled={readOnly || !enabled}
                className={`px-3 py-1 text-xs font-semibold rounded-md transition-colors ${
                  keySource === opt.id
                    ? 'bg-white text-violet-700 shadow-sm border border-slate-200'
                    : 'text-slate-500 hover:text-slate-700 border border-transparent'
                } disabled:opacity-50 disabled:cursor-not-allowed`}
              >
                {opt.label}
              </button>
            ))}
          </div>

          {keySource === 'inline' ? (
            <div className="flex items-center gap-2">
              <input
                type="password"
                value={apiKey}
                onChange={(e) => { setApiKey(e.target.value); setClearKeyIntent(false); }}
                disabled={readOnly || !enabled || clearKeyIntent}
                placeholder={saved?.has_key && !saved?.credential_id ? '••••••••••••  (key on file)' : 'sk-…'}
                autoComplete="new-password"
                className="flex-1 px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none disabled:bg-slate-50 disabled:text-slate-400 font-mono"
              />
              {saved?.has_key && !saved?.credential_id && !readOnly && (
                <button
                  type="button"
                  onClick={() => { setClearKeyIntent((v) => !v); setApiKey(''); }}
                  className={`text-xs font-semibold px-3 py-2 rounded-lg border ${
                    clearKeyIntent
                      ? 'bg-red-100 text-red-700 border-red-200'
                      : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50'
                  }`}
                >
                  {clearKeyIntent ? 'Will clear on save' : 'Clear key'}
                </button>
              )}
            </div>
          ) : (
            <div className="space-y-1.5">
              <select
                value={credentialId}
                onChange={(e) => setCredentialId(e.target.value)}
                disabled={readOnly || !enabled}
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none disabled:bg-slate-50 disabled:text-slate-400"
              >
                <option value="">— Select a saved credential —</option>
                {credentials.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}{c.type ? `  ·  ${c.type}` : ''}
                  </option>
                ))}
              </select>
              {credentials.length === 0 ? (
                <div className="text-xs text-amber-700">
                  No saved credentials yet. Create one with the <span className="font-semibold">AI Provider</span> category in Insights &rarr; Credentials, then pick it here.
                </div>
              ) : (
                <div className="text-xs text-slate-500">
                  Tip: create a credential with the <span className="font-semibold">AI Provider</span> category in Insights &rarr; Credentials so it carries the key plus provider / base URL.
                </div>
              )}
              {credentialId && credentials.length > 0 && !credentials.find((c) => c.id === credentialId) && (
                <div className="text-xs text-amber-700">
                  The previously-selected credential is no longer available. Pick another or switch to inline entry.
                </div>
              )}
            </div>
          )}
        </Field>
      )}

      {/* Workspace-only governance fields */}
      {mode === 'workspace' && (
        <div className="p-4 border border-violet-200 rounded-lg bg-violet-50/40 space-y-4">
          <div className="text-xs font-bold uppercase tracking-wide text-violet-700">
            Workspace governance (Plus)
          </div>

          <div className="flex items-start gap-3">
            <input
              type="checkbox"
              id="allow-override"
              checked={allowUserOverride}
              onChange={(e) => setAllowUserOverride(e.target.checked)}
              disabled={readOnly}
              className="mt-0.5"
            />
            <label htmlFor="allow-override" className="text-xs">
              <span className="font-bold text-slate-800">Allow users to override</span>
              <div className="text-slate-500 mt-0.5">
                When OFF, every user in this workspace uses the config above.
                When ON, users can configure their own provider from Account →
                AI Provider and the workspace config becomes a fallback.
              </div>
            </label>
          </div>

          <Field
            label="Monthly budget cap (USD)"
            hint="Soft alert at 80% · hard stop at 100% · 0 = no cap"
          >
            <input
              type="number"
              min={0}
              step={1}
              value={monthlyBudget}
              onChange={(e) => setMonthlyBudget(parseFloat(e.target.value) || 0)}
              disabled={readOnly}
              className="w-40 px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none disabled:bg-slate-50 disabled:text-slate-400"
            />
          </Field>
        </div>
      )}

      {/* Test connection */}
      <div className="space-y-2 pt-2">
        <div className="flex items-center gap-3 flex-wrap">
          <button
            type="button"
            onClick={onTest}
            disabled={!canTest}
            className="px-4 py-2 text-xs font-bold text-white bg-gradient-to-b from-violet-500 to-violet-600 hover:from-violet-600 hover:to-violet-700 border border-violet-700/30 rounded-lg shadow-sm disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {testing ? 'Testing…' : 'Test connection'}
          </button>
          {testResult && (
            <div
              className={`px-3 py-1.5 rounded-lg text-xs font-semibold border flex items-center gap-2 ${
                testResult.ok
                  ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                  : 'bg-red-50 text-red-700 border-red-200'
              }`}
              title={testResult.detail}
            >
              <span className={`inline-block w-2 h-2 rounded-full ${testResult.ok ? 'bg-emerald-500' : 'bg-red-500'}`} />
              {testResult.ok ? `OK · ${testResult.latency_ms}ms` : `Failed · ${testResult.detail.slice(0, 80)}`}
            </div>
          )}
        </div>

        {/* Rate-limit recovery chip — when Test fails with 429, suggest a
            free model from a different upstream so the user can recover in
            one click. Only shown for OpenRouter (where the upstream-routing
            issue actually applies). Picks the suggestion based on the
            current model's prefix so we don't suggest the same upstream. */}
        {testResult && !testResult.ok && provider === 'openrouter' &&
          /(^|\s)429\b|rate.?limit/i.test(testResult.detail) && (() => {
            const cur = (model || '').toLowerCase();
            const suggestions: Array<{ id: string; label: string }> = [];
            const all = [
              { id: 'deepseek/deepseek-chat-v3:free', label: 'DeepSeek V3 (free)' },
              { id: 'qwen/qwen-2.5-72b-instruct:free', label: 'Qwen 2.5 72B (free)' },
              { id: 'meta-llama/llama-3.3-70b-instruct:free', label: 'Llama 3.3 70B (free)' },
              { id: 'mistralai/mistral-7b-instruct:free', label: 'Mistral 7B (free)' },
            ];
            for (const s of all) {
              const upstream = s.id.split('/')[0];
              const curUpstream = cur.split('/')[0];
              if (upstream !== curUpstream) suggestions.push(s);
              if (suggestions.length >= 2) break;
            }
            return (
              <div className="flex items-start gap-2 px-3 py-2 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-900">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="mt-0.5 shrink-0 text-amber-600">
                  <circle cx="12" cy="12" r="10" />
                  <line x1="12" y1="8" x2="12" y2="12" />
                  <line x1="12" y1="16" x2="12.01" y2="16" />
                </svg>
                <div className="flex-1">
                  <div className="font-semibold">Hit a rate limit on this model.</div>
                  <div className="mt-0.5 text-amber-800">
                    Free models share quotas with their upstream provider. Try a free model from a different upstream — they reset independently:
                  </div>
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {suggestions.map((s) => (
                      <button
                        key={s.id}
                        type="button"
                        onClick={() => {
                          setModel(s.id);
                          setTestResult(null);
                        }}
                        className="inline-flex items-center gap-1.5 px-2 py-1 text-xs font-semibold bg-white hover:bg-amber-100 border border-amber-300 rounded text-amber-900 transition-colors"
                      >
                        Use <code className="font-mono">{s.label}</code>
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            );
          })()}
      </div>

      {/* Save / Reset */}
      <div className="flex items-center justify-between pt-4 border-t border-slate-100">
        <button
          type="button"
          onClick={onReset}
          disabled={readOnly || saving || !saved?.enabled}
          className="text-xs font-semibold text-red-600 hover:text-red-700 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          Reset to defaults
        </button>
        <div className="flex items-center gap-3">
          {saveMsg && (
            <span className={`text-xs ${saveMsg.type === 'ok' ? 'text-emerald-600' : 'text-red-600'}`}>
              {saveMsg.text}
            </span>
          )}
          <button
            type="button"
            onClick={onSave}
            disabled={readOnly || saving}
            className="px-4 py-2 text-sm font-bold text-white bg-amber-500 hover:bg-amber-600 rounded-lg shadow-sm disabled:opacity-50"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}

// Small helper — keeps field spacing consistent with AccountPage's other tabs.
function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      {/* Label contrast bumped slate-500 → slate-700 (May 1) — was washed out
          on the cream `bg-canvas-bg` body. slate-700 is WCAG-AA compliant
          for small text on that background. */}
      <label className="text-xs font-bold text-slate-700 uppercase tracking-wide">{label}</label>
      <div className="mt-1.5">{children}</div>
      {hint && <div className="text-xs text-slate-500 mt-1">{hint}</div>}
    </div>
  );
}
