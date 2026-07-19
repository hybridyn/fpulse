/**
 * AIPage — consolidated hub for AI-related operator surfaces.
 *
 * Combines four previously-scattered surfaces under one nav entry:
 *   • Activity   — audit + agent-trace + execution timeline (from ActivityPage)
 *   • Trust      — security posture (from TrustPage)
 *   • Reports    — installation report generator (from ReportsPage)
 *   • Settings   — AI provider config (was a Settings page tab)
 *
 * Pattern matches SettingsPage exactly: sticky 78px DEV/PROD banner,
 * 3-col grid (title / tabs / actions), dark + light + PROD variants. Each
 * sub-page renders in `embedded` mode so we get one canonical chrome.
 *
 * Plus tier extends this hub with additional subtabs (Governance,
 * Retention, Audit Export, custom Reports scheduler) — those stay in
 * the Plus repo per Boundary Rule 2 and are not implemented here.
 */

import { useEffect, useRef, useState } from 'react';
import { useDarkMode } from '../../hooks/useDarkMode';
import TierChip from '../shared/TierChip';
import PageHeader from '../shared/PageHeader';
import AIProviderForm from '../ai/AIProviderForm';
import ProviderComparison from '../agent/ProviderComparison';
import ActivityPage from './ActivityPage';
import TrustPage from './TrustPage';
import ReportsPage from './ReportsPage';
import ConnectorAuthorPage from './ConnectorAuthorPage';
import ConnectorGalleryPage from './ConnectorGalleryPage';
import { getAgentStatus, type AgentStatus } from '../../api/agent';
import { getOllamaStatus, type OllamaStatus } from '../../api/ollama';
import { usePageContext } from '../../hooks/usePageContext';
import { navigateTo } from '../../router';

/**
 * Compact "what's currently connected" status card. Polls
 * `/api/ai/agent/status` so the user sees the active provider + model
 * + connection state at a glance without scrolling into the form
 * to find the dropdown values. Refreshes when the form fires
 * `fpulse:ai-provider-changed` so a Save in the form below updates
 * the card without needing a page reload.
 */
function ActiveProviderSummary({ dark }: { dark: boolean }) {
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [ollamaStatus, setOllamaStatus] = useState<OllamaStatus | null>(null);
  const [error, setError] = useState<string>('');
  const [loading, setLoading] = useState(true);

  const refresh = async () => {
    setLoading(true);
    try {
      const s = await getAgentStatus();
      setStatus(s);
      setError('');
      // /agent/status returns available=true once a provider is *configured*.
      // For Ollama specifically that's not enough — the local daemon can be
      // off while the saved config still names it. Probe /ollama/status so
      // the chip can distinguish "configured but unreachable" from "connected".
      if (s?.provider === 'ollama') {
        try {
          setOllamaStatus(await getOllamaStatus());
        } catch {
          setOllamaStatus({ running: false, models: [], url: '', error: 'probe_failed' });
        }
      } else {
        setOllamaStatus(null);
      }
    } catch (e: any) {
      setError(e?.message || 'Could not reach the agent status endpoint');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    const onChanged = () => { refresh(); };
    window.addEventListener('fpulse:ai-provider-changed', onChanged);
    return () => window.removeEventListener('fpulse:ai-provider-changed', onChanged);
  }, []);

  const configured = !!status?.available;
  const provider = status?.provider || '—';
  const model = status?.model || '—';
  const tools = status?.tool_count ?? 0;

  const isOllama = status?.provider === 'ollama';
  const reachable = isOllama ? !!ollamaStatus?.running : configured;
  type BadgeState = 'connected' | 'unreachable' | 'unconfigured';
  const badgeState: BadgeState = !configured
    ? 'unconfigured'
    : reachable
      ? 'connected'
      : 'unreachable';
  const badgeLabel = loading
    ? 'Checking…'
    : badgeState === 'connected'
      ? 'Connected'
      : badgeState === 'unreachable'
        ? 'Configured — Not Reachable'
        : 'Not Configured';
  const probeError = isOllama && ollamaStatus && !ollamaStatus.running ? ollamaStatus.error : undefined;
  const probeUrl = isOllama && ollamaStatus ? ollamaStatus.url : undefined;

  return (
    // Gradient border via wrapper: a 3px gradient layer wraps a solid inner
    // card. Tailwind has no first-class gradient-border utility, so the
    // wrapper-with-padding trick is the standard workaround. Inner radius
    // is one pixel smaller than outer so the corners stay clean.
    <div className={`rounded-xl p-[3px] shadow-lg bg-gradient-to-r from-violet-500 via-fuchsia-500 to-emerald-500 ${
      dark ? 'shadow-violet-900/30' : 'shadow-violet-200/60'
    }`}>
    <div className={`rounded-[10px] p-4 ${
      dark ? 'bg-[#111827]' : 'bg-white'
    }`}>
      <div className="flex items-center gap-3 flex-wrap">
        <div className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 ${
          badgeState === 'connected'
            ? (dark ? 'bg-emerald-500/15 border border-emerald-500/25' : 'bg-emerald-50 border border-emerald-200')
            : badgeState === 'unreachable'
              ? (dark ? 'bg-amber-500/15 border border-amber-500/25' : 'bg-amber-50 border border-amber-200')
              : (dark ? 'bg-slate-700/30 border border-slate-600' : 'bg-slate-100 border border-slate-200')
        }`}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className={
            badgeState === 'connected'
              ? (dark ? 'text-emerald-300' : 'text-emerald-600')
              : badgeState === 'unreachable'
                ? (dark ? 'text-amber-300' : 'text-amber-600')
                : (dark ? 'text-slate-500' : 'text-slate-400')
          }>
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`text-xs font-bold uppercase tracking-wider ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
              Active provider
            </span>
            <span className={`text-xs font-bold uppercase px-2 py-0.5 rounded-full ${
              badgeState === 'connected'
                ? (dark ? 'text-emerald-300 bg-emerald-500/15 border border-emerald-500/25' : 'text-emerald-700 bg-emerald-50 border border-emerald-200')
                : badgeState === 'unreachable'
                  ? (dark ? 'text-amber-300 bg-amber-500/15 border border-amber-500/25' : 'text-amber-700 bg-amber-50 border border-amber-200')
                  : (dark ? 'text-slate-400 bg-slate-700/30 border border-slate-600' : 'text-slate-500 bg-slate-100 border border-slate-200')
            }`}>
              {badgeLabel}
            </span>
          </div>
          <div className={`mt-0.5 text-sm font-bold flex items-center gap-2 flex-wrap ${dark ? 'text-slate-100' : 'text-slate-800'}`}>
            <span className="capitalize">{provider}</span>
            <span className={dark ? 'text-slate-600' : 'text-slate-300'}>/</span>
            <span className="font-mono">{model}</span>
            {badgeState === 'connected' && (
              <span className={`text-xs font-mono px-1.5 py-0.5 rounded ${dark ? 'bg-slate-800 text-slate-400 border border-slate-700' : 'bg-slate-100 text-slate-500 border border-slate-200'}`}>
                {tools} tools
              </span>
            )}
          </div>
          {badgeState === 'unreachable' && (probeError || probeUrl) && (
            <div className={`text-xs mt-1 font-mono truncate ${dark ? 'text-amber-300/90' : 'text-amber-700'}`} title={`${probeError || ''} ${probeUrl || ''}`.trim()}>
              {probeError ? `${probeError} · ` : ''}{probeUrl || ''}
            </div>
          )}
          {error && (
            <div className="text-xs text-red-500 mt-1 truncate" title={error}>{error}</div>
          )}
        </div>

        <button
          onClick={refresh}
          disabled={loading}
          title="Re-check provider status"
          className={`px-3 py-1.5 text-xs font-medium rounded-lg border transition-colors disabled:opacity-50 ${
            dark ? 'text-slate-300 bg-white/[0.06] border-white/[0.1] hover:bg-white/[0.1]' : 'text-slate-600 bg-white border-slate-200 hover:bg-slate-50'
          }`}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className={`inline mr-1 -mt-0.5 ${loading ? 'animate-spin' : ''}`}>
            <polyline points="23 4 23 10 17 10" /><polyline points="1 20 1 14 7 14" />
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
          </svg>
          Re-check
        </button>
      </div>
    </div>
    </div>
  );
}

type AITab = 'settings' | 'activity' | 'reports' | 'trust' | 'author' | 'gallery';

// Each sub-tab maps to a top-level page id so clicking a tab updates the
// URL hash. Refresh / share-URL / browser-back then preserve the tab.
// 'settings' → 'ai' because the AI Provider tab is the default landing for
// the Insights hub; the page id stayed 'ai' for back-compat with older deep
// links.
// 2026-05-29: 'gallery' added so the community-connector browse view
// has a stable deep-link (#ai?tab=gallery). Paired with 'author' as
// the build vs browse halves of the OSS extensibility loop.
const TAB_TO_PAGE = {
  settings: 'ai',
  activity: 'activity',
  reports: 'reports',
  trust: 'trust',
  author: 'author',
  gallery: 'gallery',
} as const;

interface Props {
  environment?: 'dev' | 'prod';
  // Narrow to the canonical edition union so ReportsPage (which takes
  // `tier?: 'free' | 'plus'`) accepts the prop without a cast.
  tier?: 'free' | 'plus';
  // ReportsPage defines its own User shape; pass-through accepts any object
  // or null — we don't introspect it here.
  user: any;
  initialTab?: AITab;
}

export default function AIPage({
  environment = 'dev',
  tier = 'free',
  user,
  initialTab = 'settings',
}: Props) {
  const dark = useDarkMode();
  const isProd = environment === 'prod';
  const [tab, setTab] = useState<AITab>(initialTab);

  // OSS-4 (2026-05-19) — publish context so the Copilot has a handle on
  // which AI hub sub-surface the user is viewing.
  usePageContext({ page: 'ai', filters: { tab } });

  // Ref to the actual scrolling container (the body div with overflow-auto).
  // 2026-05-19 (P2 #4 of PAGE_BY_PAGE_AUDIT.md): `scrollToProviderForm`
  // was declared but no JSX wires it to a button — the ProviderComparison
  // card "Configure" button was removed in the May 2 cleanup and the
  // handler was never garbage-collected. Removed. `bodyScrollContainerRef`
  // is kept because it's referenced by the scroll container below; the
  // form anchor ref is no longer needed.
  const bodyScrollContainerRef = useRef<HTMLDivElement | null>(null);

  // Tab order: AI Provider → Activity → Reports → Trust.
  // Reasoning: users configure the provider first (nothing else works without
  // it), then look at what the agent has been doing (Activity), then export /
  // share results (Reports), then the Trust posture is a "read once" reference.
  // Each tab carries a `subtitle` so the page header (title + subline) updates
  // to describe the active section instead of staying on a generic "Insights"
  // line — added May 10 2026.
  const TABS: { id: AITab; label: string; subtitle: string; icon: React.ReactNode }[] = [
    {
      id: 'settings',
      label: 'AI Provider',
      subtitle: 'Pick a cloud LLM or run locally with Ollama. Your data stays on your machine.',
      icon: (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 2v4" /><path d="M12 18v4" />
          <path d="M4.93 4.93l2.83 2.83" /><path d="M16.24 16.24l2.83 2.83" />
          <path d="M2 12h4" /><path d="M18 12h4" />
          <path d="M4.93 19.07l2.83-2.83" /><path d="M16.24 7.76l2.83-2.83" />
          <circle cx="12" cy="12" r="3" />
        </svg>
      ),
    },
    {
      id: 'activity',
      label: 'Activity',
      subtitle: 'Unified timeline of agent runs, audit events, and pipeline executions.',
      icon: (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
        </svg>
      ),
    },
    {
      id: 'reports',
      label: 'Reports',
      subtitle: 'Snapshot the live state of your installation as a PDF or Word document.',
      icon: (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
          <line x1="16" y1="13" x2="8" y2="13" />
          <line x1="16" y1="17" x2="8" y2="17" />
        </svg>
      ),
    },
    {
      id: 'trust',
      label: 'Trust',
      subtitle: 'Verifiable evidence behind F-Pulse’s security claims — invariants, certs, audit endpoints.',
      icon: (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
        </svg>
      ),
    },
    {
      id: 'author',
      // 2026-06-03 — shortened from "Author Connector" (which wrapped
      // to two lines in the Insights tab rail at common viewport widths)
      // to single-word "Author". The action context is already obvious
      // from the surrounding tab cluster + the subtitle below.
      label: 'Author',
      subtitle: 'Generate a connector definition from an OpenAPI spec or sample API responses.',
      icon: (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 20h9" />
          <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
        </svg>
      ),
    },
    {
      id: 'gallery',
      label: 'Gallery',
      subtitle: 'Browse community-built connectors and curated starting points you can deploy in 90 seconds.',
      icon: (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="7" height="7" rx="1" />
          <rect x="14" y="3" width="7" height="7" rx="1" />
          <rect x="3" y="14" width="7" height="7" rx="1" />
          <rect x="14" y="14" width="7" height="7" rx="1" />
        </svg>
      ),
    },
  ];

  const activeTab = TABS.find((t) => t.id === tab) ?? TABS[0];

  return (
    <div className={`flex-1 flex flex-col overflow-hidden ${dark ? 'bg-[#0B1220]' : 'bg-canvas-bg'}`}>
      {/* Header — canonical shared PageHeader shell */}
      <PageHeader
        environment={environment}
        icon={<span className={isProd ? 'text-red-400' : 'text-blue-500'}>{activeTab.icon}</span>}
        title={activeTab.label}
        titleAccessory={<TierChip tier={tier} environment={environment} />}
        subtitle={activeTab.subtitle}
        tabs={
          <div className="flex justify-center items-center gap-0.5">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => { setTab(t.id); navigateTo(TAB_TO_PAGE[t.id]); }}
                className={`flex items-center gap-2 px-4 py-2.5 text-sm font-semibold rounded-lg transition-all capitalize ${
                  tab === t.id
                    ? dark
                      ? 'border-violet-400 text-violet-200 font-bold bg-gradient-to-b from-violet-400/30 to-violet-600/20 shadow-[inset_0_0_0_1.5px_rgba(167,139,250,0.55),inset_0_0_10px_rgba(139,92,246,0.30),inset_0_1px_0_rgba(255,255,255,0.22)]'
                      : 'text-white font-bold bg-gradient-to-b from-slate-600 to-slate-800 shadow-[inset_0_0_0_1.5px_rgba(148,163,184,0.65),inset_0_0_10px_rgba(100,116,139,0.35),inset_0_1px_0_rgba(255,255,255,0.22)]'
                    : dark
                      ? 'border-transparent text-slate-500 hover:text-slate-300 hover:bg-white/[0.03]'
                      : 'border-transparent text-slate-900 font-bold hover:text-violet-700 hover:bg-violet-50/50'
                }`}
              >
                {t.icon}
                {t.label}
              </button>
            ))}
          </div>
        }
      />

      {/* Content */}
      <div ref={bodyScrollContainerRef} className="flex-1 overflow-auto p-6">
        {/* Cap reading-column at 1300px so Insights sections (Activity / Trust /
            Reports / Provider) feel composed on wide screens but use enough
            horizontal space to be comfortably readable on 1920px monitors. */}
        <div className="w-full max-w-[1300px] mx-auto">
          {tab === 'activity' && <ActivityPage embedded />}

          {tab === 'trust' && <TrustPage embedded />}

          {tab === 'reports' && <ReportsPage user={user} embedded tier={tier} environment={environment} />}

          {tab === 'author' && <ConnectorAuthorPage embedded />}

          {tab === 'gallery' && <ConnectorGalleryPage embedded />}

          {tab === 'settings' && (
            <div className="space-y-4">
              {/* "What's connected right now" summary — fetches
                  /api/ai/agent/status once on mount and listens for the
                  fpulse:ai-provider-changed event so saving the form
                  below updates this card without a reload. Saves the
                  user from scrolling into the dropdown to figure out
                  which provider is active. */}
              <ActiveProviderSummary dark={dark} />

              {/* Section header removed (May 2) — duplicated the active tab
                  name "AI Provider" right above it. The tab pill + intro
                  card give enough orientation; the extra header was vertical
                  noise. The "Compare providers" sub-section header below
                  stays because it distinguishes reference data from the form. */}

              {/* "Your AI provider" intro card removed 2026-05-09 per user
                  request — the Active Provider badge above already
                  communicates connection state, and the cost / trust links
                  remain accessible via the Help page and the Trust tab. */}

              {/* Provider form — wrapped in a unified white card so all
                  the inner sections (Enable AI / Provider / Model / API Key /
                  Test connection / Save) live inside one container instead
                  of sitting bare on the cream `bg-canvas-bg`. Matches how
                  the legacy SettingsPage tab rendered it. */}
              <div
                className={`rounded-xl border shadow-sm p-5 ${
                  dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'
                }`}
              >
                <AIProviderForm mode="user" />
              </div>

              {/* Eval-harness verify CTA — closes the discovery loop.
                  2026-05-19 (P1 #12 of PAGE_BY_PAGE_AUDIT.md): a user
                  configuring a provider here had no in-page link to the
                  eval harness, even though the harness exists as the
                  canonical "is this model good enough" signal. The Trust
                  tab already shows the pass-rate; this CTA tells the user
                  HOW to populate it. */}
              <div
                className={`mt-4 rounded-xl border shadow-sm p-4 flex items-start gap-3 ${
                  dark ? 'bg-emerald-500/10 border-emerald-500/20' : 'bg-emerald-50 border-emerald-200'
                }`}
              >
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke={dark ? '#34d399' : '#059669'} strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 mt-0.5">
                  <polyline points="22 11.08 12 14 2 11.08" />
                  <path d="M12 2L2 7l10 5 10-5-10-5z" />
                  <polyline points="2 17 12 22 22 17" />
                  <polyline points="2 12 12 17 22 12" />
                </svg>
                <div className="min-w-0 flex-1">
                  <p className={`text-xs font-bold ${dark ? 'text-emerald-200' : 'text-emerald-800'}`}>
                    Verify this provider with the eval harness
                  </p>
                  <p className={`text-xs mt-0.5 ${dark ? 'text-emerald-200/80' : 'text-emerald-700'}`}>
                    Run <code className={`px-1 py-0.5 rounded ${dark ? 'bg-black/30 text-emerald-200' : 'bg-white text-emerald-800'}`}>python -m fpulse.eval.run</code> to score the model on tool-use, classification, and intent across 14 cases × 5 categories. The pass-rate surfaces on the Trust tab.
                  </p>
                  <a
                    href="#help/docs/eval-harness.md"
                    onClick={(e) => {
                      e.preventDefault();
                      try { sessionStorage.setItem('fpulse_docs_jump_to', 'eval-harness.md'); } catch { /* noop */ }
                      navigateTo('help');
                    }}
                    className={`inline-flex items-center gap-1 text-xs font-semibold mt-1.5 ${dark ? 'text-emerald-300 hover:text-emerald-200' : 'text-emerald-700 hover:text-emerald-900'}`}
                  >
                    Open eval-harness.md
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                      <line x1="7" y1="17" x2="17" y2="7" />
                      <polyline points="7 7 17 7 17 17" />
                    </svg>
                  </a>
                </div>
              </div>

              {/* Provider price comparison — reference data below the form.
                  Framed as a sub-section with its own header so the user
                  understands it's reference material, not another action.
                  No Configure button — the whole page IS the configuration;
                  the button was redundant (May 2 cleanup). */}
              <div
                className={`flex items-center gap-2 mt-6 mb-3 pb-2 border-b ${
                  dark ? 'border-white/[0.06]' : 'border-slate-300'
                }`}
              >
                <span className={dark ? 'text-violet-300' : 'text-violet-600'}>
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <line x1="12" y1="1" x2="12" y2="23" />
                    <path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" />
                  </svg>
                </span>
                <div className="flex-1 min-w-0">
                  <h3 className={`text-sm font-bold uppercase tracking-wider ${dark ? 'text-slate-200' : 'text-slate-800'}`}>
                    Compare providers
                  </h3>
                  <p className={`text-xs mt-0.5 ${dark ? 'text-slate-400' : 'text-slate-600'}`}>
                    Reference pricing across providers — pick one in the form above. Per-token rates and estimated cost per agent turn at typical workload.
                  </p>
                </div>
              </div>
              <ProviderComparison />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
