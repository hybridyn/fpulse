// rebuild-marker-v2
import { useState, useEffect, useCallback, lazy, Suspense } from 'react';
import ErrorBoundary from './components/ErrorBoundary';
import { DialogRoot, uiConfirm } from './ui/dialog';
import BindWarningBanner from './components/BindWarningBanner';
import Toolbar from './components/Toolbar';
import EditorContextBar from './components/EditorContextBar';
import CanvasCopyrightMark from './components/CanvasCopyrightMark';
import PanelErrorBoundary from './components/PanelErrorBoundary';
// ─── Eager imports — only the dashboard route + chrome ────────────────
// DashboardPage is the default landing route, so loading it eagerly
// saves one round-trip on first paint. Sidebar, Toolbar, EditorContextBar,
// and the error boundaries are app-shell — they render on every page.
import Sidebar from './components/Sidebar';
import DashboardPage from './components/pages/DashboardPage';
import LoginPage from './components/pages/LoginPage';
// ─── Lazy imports — every other page + the editor's heavy panels ──────
// Cut the main bundle from ~2.5 MB to ~1 MB by deferring these until
// the user actually navigates to / opens the route that needs them.
// React.lazy + Suspense wrappers below in the render tree do the
// async loading; the fallback is the LoadingFallback component declared
// just before the App render block.
const Canvas = lazy(() => import('./components/Canvas'));
// ChatPanel restored 2026-06-17 — the Editor keeps its OWN docked Copilot
// (guided builder + canvas-aware workflow-store agent). The floating
// FloatingAgentWidget is suppressed on the editor route so the canvas has
// exactly one AI surface; every other page still uses the floating Copilot.
const ChatPanel = lazy(() => import('./components/ChatPanel'));
const ModulesPanel = lazy(() => import('./components/ModulesPanel'));
const ConfigPanel = lazy(() => import('./components/ConfigPanel'));
const PreviewPanel = lazy(() => import('./components/PreviewPanel'));
const CodeEditorPanel = lazy(() => import('./components/CodeEditorPanel'));
const PipelinesPage = lazy(() => import('./components/pages/PipelinesPage'));
const TemplatesPage = lazy(() => import('./components/pages/TemplatesPage'));
const ExecutionsPage = lazy(() => import('./components/pages/ExecutionsPage'));
const ExtractionPage = lazy(() => import('./components/pages/ExtractionPage'));
const CredentialsPage = lazy(() => import('./components/pages/CredentialsPage'));
const SettingsPage = lazy(() => import('./components/pages/SettingsPage'));
const ConnectionsPage = lazy(() => import('./components/pages/ConnectionsPage'));
const ProjectsPage = lazy(() => import('./components/pages/ProjectsPage'));
const HelpPage = lazy(() => import('./components/pages/HelpPage'));
const TrustPage = lazy(() => import('./components/pages/TrustPage'));
// CertMatrixPage folded into Insights → Trust (2026-06-17) as an expandable
// "full matrix" section; the #cert-matrix route now redirects to that tab.
const ActivityPage = lazy(() => import('./components/pages/ActivityPage'));
const ReportsPage = lazy(() => import('./components/pages/ReportsPage'));
const AIPage = lazy(() => import('./components/pages/AIPage'));
const AccountPage = lazy(() => import('./components/pages/AccountPage'));
const NotificationsPage = lazy(() => import('./components/pages/NotificationsPage'));
const ExecutionPoolPage = lazy(() => import('./components/pages/ExecutionPoolPage'));
const StoragePage = lazy(() => import('./components/pages/StoragePage'));
const LineagePage = lazy(() => import('./components/pages/LineagePage'));
// X2 (2026-05-30) — these five components don't render on first paint:
//   - OnboardingWizard fires only for first-time users (localStorage flag)
//   - OSSProductionPlaceholder shows on tier mismatch
//   - GlobalSearch is the Cmd+K modal — rendered always but only opens on demand
//   - OllamaRecommendationBanner shows only when Ollama isn't detected
//   - FloatingAgentWidget is the chat panel — opens on click
// Lazy them out of the main bundle to shave ~80 KB off first paint.
const OnboardingWizard = lazy(() => import('./components/OnboardingWizard'));
const OSSProductionPlaceholder = lazy(() => import('./components/OSSProductionPlaceholder'));
const GlobalSearch = lazy(() => import('./components/GlobalSearch'));
const OllamaRecommendationBanner = lazy(() => import('./components/OllamaRecommendationBanner'));
import Toast, { toast } from './components/Toast';
const FloatingAgentWidget = lazy(() => import('./components/agent/FloatingAgentWidget'));
import CopyrightFooter from './components/CopyrightFooter';
import { useWorkflowStore } from './stores/workflowStore';
import { api } from './api/client';
// 2026-05-23 (P0 Day 2): canAccessAdmin / canAccessAdminInEnv /
// hasPermission dropped from the import — they were only used by the
// admin/approvals page bouncers that came out alongside the dead
// route entries. canAccessProd stays — it gates the PROD env switch.
import { canAccessProd } from './auth/permissions';

// Page union — kept intentionally wide so admin/approvals route paths
// resolve without TS narrowing errors. The legacy `'admin'` + `'approvals'`
// values are recognised by the routing code in this file but the actual
// admin UI lives behind a Plus-only feature flag.
// 2026-05-19 (OSS-9 of PAGE_BY_PAGE_AUDIT.md): `Page` + `VALID_PAGES`
// now live in `./types` and routing helpers in `./router`. Imported
// below so the three separate copies of this union (App.tsx,
// Sidebar.tsx, GlobalSearch.tsx) collapse to one source of truth and
// the `as any` casts on Sidebar / Search go away.
import type { Page } from './types';
import { VALID_PAGES, DEFAULT_PAGE } from './types';
import { navigateTo as routerNavigateTo, navigateToSubRoute } from './router';
type Environment = 'dev' | 'prod';
type Tier = 'free' | 'plus';

/**
 * Reads the user's "show floating Copilot" preference from localStorage.
 *
 * Default ON for every tier (May 17 2026 PM). Previous PM default-off
 * for OSS came from a fear that the widget would imply AI was required;
 * in practice it just meant Free users never discovered the Copilot
 * (no in-product CTA pointed at it). The widget's own "Configure AI
 * Provider" banner already handles the unconfigured-LLM case
 * gracefully, so showing the dock by default is safe and aligns with
 * the "AI-assisted operational workspace" positioning.
 *
 * Explicit opt-out is preserved: setting the localStorage key to
 * "false" still hides the widget, so users who don't want it can
 * dismiss permanently. Editor pages still hide it at the call site.
 *
 * Settings page toggles this via key "fpulse.ui.showCopilotWidget"
 * (string "true" / "false"). Absence → ON.
 */
function shouldShowCopilotWidget(_tier: Tier): boolean {
  try {
    const stored = localStorage.getItem('fpulse.ui.showCopilotWidget');
    if (stored === 'true') return true;
    if (stored === 'false') return false;
  } catch {
    // localStorage unavailable (private mode) — fall through to default.
  }
  return true;
}

export default function App() {
  const [page, setPage] = useState<Page>(DEFAULT_PAGE);
  const [user, setUser] = useState<any>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null);
  const [activeProjectName, setActiveProjectName] = useState<string>('');
  // Environment is intentionally NOT restored from localStorage on page load.
  // Persisting it meant a developer who clicked PROD once would land in PROD
  // on every subsequent reload — wrong default for shared machines, and very
  // wrong when the next user signing in is a developer who shouldn't see
  // production data first thing. Always start in DEV; users can opt into PROD
  // explicitly each session by clicking the PROD button.
  const [environment, setEnvironment] = useState<Environment>('dev');
  // Always boot in 'free' — never seed from localStorage. The cached value
  // can be stale (e.g. previous session was on a Plus server, current server
  // is Free) and would briefly paint the PLUS pill + gold framing on first
  // render, fooling the user into thinking they're on Plus. The license
  // refresh effect below is the ONLY source of truth and runs immediately on
  // mount, so the worst case is one paint of the safe-default 'free' UI
  // before snapping to the real tier ~50ms later.
  const [tier, setTier] = useState<Tier>('free');
  const [showOnboarding, setShowOnboarding] = useState(() => !localStorage.getItem('fpulse_onboarding_done'));
  const [searchOpen, setSearchOpen] = useState(false);
  const codeEditorOpen = useWorkflowStore((s) => s.codeEditorOpen);
  // 2026-05-28 — subscribe to workflow status + dirty so the editor
  // banner "Published — autosave suspended" can react when either
  // changes. The banner only renders when both are true.
  const editorWorkflowStatus = useWorkflowStore((s) => s.status);
  const editorIsDirty = useWorkflowStore((s) => s.isDirty);

  // Check license tier on mount, on tab focus, and on visibility change.
  //
  // Why three triggers: tier was previously fetched only on mount, so any tab
  // opened while the server was free-tier would keep showing the PROD lock
  // forever even after an admin activated the license in another tab. Refetch
  // on focus / visibilitychange catches that case without polling, and the
  // mount fetch still handles the cold-start path.
  // License/tier loading — industry-standard pattern (Stripe / Notion /
  // Linear / Vercel all converge on the same shape):
  //
  //   1. Cached tier + timestamp lives in localStorage with a 5-minute TTL.
  //   2. On mount: warm from cache for instant render, then revalidate
  //      asynchronously if the cache is stale.
  //   3. visibilitychange revalidates ONLY if the tab was hidden long
  //      enough for the cache to expire — catches the "left tab open
  //      overnight, license changed" case without polling.
  //   4. No window focus listener — focus fires on every Alt-Tab, every
  //      DevTools toggle, every multi-monitor click. Counted hundreds of
  //      duplicate /api/plus/license calls per session before this fix.
  //   5. Explicit invalidation via a `fpulse:license-changed` custom event
  //      so AdminPage's activate/deactivate handlers (and any future plan
  //      mutations) can force a refetch immediately.
  //   6. Logout already clears the cache (see handleLogout below).
  useEffect(() => {
    // Versioned cache key — bumping the suffix in future drops every
    // browser's stale entry without explicit migration code.
    const CACHE_KEY = 'fpulse_tier_cache_v1';
    const TTL_MS = 5 * 60 * 1000;

    // Bind cache to {userId, workspaceId} so a logout/login swap (or a
    // workspace switch via the workspace picker) doesn't briefly paint
    // the previous user's tier. Without this, an Admin signing out and
    // a Developer signing in on the same machine could see ~1 paint of
    // PLUS chrome before the post-login refetch corrected things —
    // exactly the wrong-tier-flash class of bug review #3 flagged.
    const ownerKey = (): string => {
      try {
        const u = JSON.parse(localStorage.getItem('fpulse_user') || 'null');
        const ws = localStorage.getItem('fpulse_workspace_id') || 'default';
        const uid = (u && (u.id || u.email)) || 'anon';
        return `${uid}::${ws}`;
      } catch { return 'anon::default'; }
    };

    type Cached = { tier: Tier; ts: number; owner: string };
    const readCache = (): Cached | null => {
      try {
        const raw = localStorage.getItem(CACHE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw) as Cached;
        if (parsed?.tier !== 'plus' && parsed?.tier !== 'free') return null;
        if (typeof parsed.ts !== 'number') return null;
        // Reject cross-owner cache hits — they'd paint the wrong tier
        // briefly during user/workspace switch.
        if (parsed.owner !== ownerKey()) return null;
        return parsed;
      } catch { return null; }
    };
    const writeCache = (t: Tier) => {
      try {
        localStorage.setItem(
          CACHE_KEY,
          JSON.stringify({ tier: t, ts: Date.now(), owner: ownerKey() }),
        );
      } catch {}
    };
    const isFresh = (c: Cached | null) => !!c && Date.now() - c.ts < TTL_MS;

    // In-flight request dedup — if multiple components/effects mount in
    // the same tick and all see a stale cache, they'd each fire a fetch.
    // The shared promise collapses them into one.
    let inflight: Promise<void> | null = null;
    const fetchAndCache = (): Promise<void> => {
      if (inflight) return inflight;
      inflight = api.getLicenseStatus()
        .then((status) => {
          const t: Tier = status.is_plus ? 'plus' : 'free';
          setTier(t);
          writeCache(t);
        })
        .catch(() => {
          // Grace fallback — keep the last-known tier rather than
          // forcibly downgrading to 'free' on a transient API blip.
          // Only if there's no cache at all do we land on the safe
          // 'free' default; that guarantees premium UI is never shown
          // to a user with no validation history at all.
          const stale = readCache();
          if (stale) {
            setTier(stale.tier);
            // Don't rewrite ts — keep the original "this was last
            // confirmed at X" timestamp so the next visibility tick
            // tries to refresh again.
          } else {
            setTier('free');
            writeCache('free');
          }
        })
        .finally(() => {
          inflight = null;
        });
      return inflight;
    };

    // Step 1 — warm from cache for instant render.
    const cached = readCache();
    if (cached) setTier(cached.tier);

    // Step 2 — revalidate only if cache is missing or stale.
    if (!isFresh(cached)) fetchAndCache();

    // Step 3 — visibility-aware revalidation. Refetches only when the
    // tab becomes visible AND the cache has gone stale since last write.
    const onVisible = () => {
      if (document.hidden) return;
      if (!isFresh(readCache())) fetchAndCache();
    };
    document.addEventListener('visibilitychange', onVisible);

    // Step 5 — explicit invalidation event. Any code path that mutates
    // the license should dispatch this; we drop the cache and refetch.
    const onLicenseChanged = () => {
      try { localStorage.removeItem(CACHE_KEY); } catch {}
      fetchAndCache();
    };
    window.addEventListener('fpulse:license-changed', onLicenseChanged);

    return () => {
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('fpulse:license-changed', onLicenseChanged);
    };
  }, []);

  // Persist environment choice & redirect from env-specific pages.
  //
  // `admin` is PROD-only: deployments, vaults, audit and
  // license are production surfaces and should be managed from PROD. On
  // Free/OSS there is no PROD, so Admin stays accessible in DEV.
  //
  // The deps include `page` and `tier` so the redirect fires when the
  // user navigates into Admin from DEV (old code only fired on env
  // change, which missed the click-Admin-from-DEV path).
  const DEV_ONLY_PAGES: Page[] = ['editor', 'projects'];
  useEffect(() => {
    localStorage.setItem('fpulse_env', environment);
    if (environment === 'prod' && DEV_ONLY_PAGES.includes(page)) {
      navigate('dashboard');
      return;
    }
    // 2026-05-23 (P0 Day 2): admin/approvals removed from the Page
    // union — orphan-route bouncers gone with them. The Plus repo
    // will reinstate this branch + the matching render when it adds
    // those pages back. canAccessProd / canAccessAdminInEnv stay in
    // imports — Sidebar still gates a future Admin item, and the
    // env-switch effect below still uses canAccessProd.
  }, [environment, page, tier]);

  // RBAC guard — runs whenever a user is logged in, regardless of tier.
  // OSS now seeds real user accounts with real roles, and the Admin /
  // Approvals pages still expose sensitive surface in free tier (user
  // management, license activation, audit trail), so role gating must
  // not be tier-conditional. The PROD environment redirect below stays
  // Plus-aware because PROD itself is a Plus-only feature.
  const rbacActive = !!user;

  // If current role cannot access PROD, snap back to DEV.
  // Plus-conditional: in free tier the env button is already gated at the
  // backend (402) and the per-role PROD policy doesn't apply, so we don't
  // want to forcibly bounce a free-tier developer who toggled PROD just to
  // see the role-restricted message.
  useEffect(() => {
    if (rbacActive && tier === 'plus' && environment === 'prod' && !canAccessProd(user)) {
      setEnvironment('dev');
    }
  }, [rbacActive, tier, environment, user]);

  // 2026-05-23 (P0 Day 2): the admin/approvals page bouncers were
  // removed alongside their entries in the Page union. Routes don't
  // resolve, so there's nothing to gate. The RBAC helpers remain
  // imported for Sidebar's adminOnly nav filter + the env-switch
  // effect above; reinstating these branches is a one-revert when
  // Plus lands the rendered pages.

  // Logs tab lives inside AdminPage (application-level logs)

  // 2026-05-19 (P2 #11 of PAGE_BY_PAGE_AUDIT.md): the
  // restore-from-localStorage effect was dead code — main.tsx wipes the
  // `fpulse_theme` key on every boot, so this branch could never fire.
  // Removed. Dark mode is currently unsupported. The `dark:` Tailwind classes
  // have since been stripped; fully removing the useDarkMode hook + `dark ? `
  // ternaries remains a separate refactor under the same audit item.

  // Check stored session on mount — validate token with backend
  useEffect(() => {
    const stored = localStorage.getItem('fpulse_user');
    const token = localStorage.getItem('fpulse_token');
    if (stored && token) {
      // Optimistically set user from cache for instant render
      setUser(JSON.parse(stored));
      // Then validate the session is still alive on the backend.
      // If backend returns 401, the token is stale — clear it and
      // show the login page instead of showing errors everywhere.
      fetch('/api/auth/me', {
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
      })
        .then((res) => {
          if (res.status === 401 || res.status === 403) {
            // Session expired or invalid — clear stale credentials
            localStorage.removeItem('fpulse_token');
            localStorage.removeItem('fpulse_user');
            setUser(null);
          }
        })
        .catch(() => {
          // Backend not reachable — keep the cached user so offline
          // development still works. API calls will fail individually.
        });
    }
    setAuthChecked(true);
  }, []);

  // Path normalization — the SPA only uses `/` as its base. If the user
  // arrived via something like `/quickstart.md#projects` (deep-link to a
  // doc, refresh on a stale URL, etc.), Vite's catch-all serves
  // index.html and the hash router still works, but the address bar
  // keeps showing the bogus path. Strip it back to `/` once on mount so
  // every page reads `localhost:5174/#<page>` cleanly.
  useEffect(() => {
    if (window.location.pathname !== '/') {
      window.history.replaceState(null, '', `/${window.location.hash}`);
    }
  }, []);

  // Seed the workflow store's projectId from the current project context
  // whenever the user enters the Editor on a fresh canvas (no workflowId).
  // Without this, a "+ New Pipeline" started from inside a project lands
  // in "default" because the toolbar's picker has no signal about where
  // the user came from. Editing an existing pipeline already carries its
  // project_id in the loaded IR, so we leave it alone.
  useEffect(() => {
    if (page !== 'editor') return;
    const { workflowId, setProjectId } = useWorkflowStore.getState();
    if (!workflowId) {
      setProjectId(activeProjectId);
    }
  }, [page, activeProjectId]);

  // Hash-based routing.
  // First segment selects the Page; everything after the first slash
  // is a subroute owned by the page (e.g. `#extraction/<run_id>`
  // routes to the extraction monitor for that specific run).
  useEffect(() => {
    const onHash = () => {
      // 2026-05-22 (audit O1) — page identity is everything up to the
      // first `/` OR `?`. The previous `split('/')[0]` treated
      // `executions?status=failed` as one big page id, so the
      // Dashboard's filter-aware chips never routed. The page split now
      // happens via the shared parser in router.ts so this listener
      // and `readCurrentPage()` agree.
      const raw = window.location.hash.replace('#', '') || DEFAULT_PAGE;
      const path = raw.split('?')[0];
      const first = path.split('/')[0];
      if (VALID_PAGES.includes(first as Page)) {
        setPage(first as Page);
      }
    };
    window.addEventListener('hashchange', onHash);
    onHash();
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  // 2026-05-26 — Idle prefetch of every lazy route. Closes the
  // "first-click flash" gap introduced when App was split from a
  // 2.5 MB monolith into ~20 per-route chunks (~860 kB main + ~1.3 MB
  // deferred). After ~5 s of idle the user's browser has every chunk
  // warm in cache, so Suspense's "Loading…" fallback essentially never
  // fires. Hover prefetch in Sidebar.tsx handles the first few seconds.
  // See utils/routePrefetch.ts for the rationale + per-route map.
  useEffect(() => {
    let cancelled = false;
    // Defer once more after the first effect tick so we run AFTER the
    // dashboard's own fetches / WebSocket connect kick off. Both fight
    // for the same network if we fire too early on a slow connection.
    const t = setTimeout(() => {
      if (!cancelled) {
        import('./utils/routePrefetch').then(({ prefetchAllRoutes }) => {
          if (!cancelled) prefetchAllRoutes();
        });
      }
    }, 500);
    return () => { cancelled = true; clearTimeout(t); };
  }, []);

  const navigate = useCallback((p: Page) => {
    routerNavigateTo(p);
    setPage(p);
  }, []);

  // Copilot navigate-chip handler. Backend emits navigate actions with
  // page IDs from its own vocabulary ("workflows", "alerts", "logs");
  // map them to this app's Page enum and fire navigate().
  useEffect(() => {
    const PAGE_MAP: Record<string, Page> = {
      // backend term → frontend Page
      workflows: 'pipelines',
      pipelines: 'pipelines',
      executions: 'executions',
      logs: 'executions',
      connections: 'connections',
      credentials: 'credentials',
      dashboard: 'dashboard',
      projects: 'projects',
      settings: 'settings',
      help: 'help',
      alerts: 'notifications',
      notifications: 'notifications',
      pool: 'pool',
      lineage: 'lineage',
      reports: 'reports',
      trust: 'trust',
      activity: 'activity',
      ai: 'ai',
      account: 'account',
      'cert-matrix': 'cert-matrix',
      editor: 'editor',
      schedules: 'pipelines',  // no dedicated schedules page; pipelines hosts the schedule tab
    };
    const onNav = (e: Event) => {
      const detail = (e as CustomEvent).detail || {};
      const target = String(detail.page || '').trim().toLowerCase();
      if (!target) return;
      const mapped = PAGE_MAP[target];
      if (!mapped) {
        // Unknown destination — log but don't crash the chat.
        console.warn('[Copilot navigate] unknown page:', target);
        return;
      }
      navigate(mapped);
    };
    window.addEventListener('fpulse-agent-navigate', onNav);
    return () => window.removeEventListener('fpulse-agent-navigate', onNav);
  }, [navigate]);

  // Plus-tier consolidation: on PROD, Credentials is superseded by Vault.
  // Redirect only fires on PROD+Plus — DEV keeps Credentials available for
  // every tier (legacy transition, debugging, experimentation). Free users
  // and DEV Plus users never see this redirect.
  useEffect(() => {
    if (page === 'credentials' && tier === 'plus' && environment === 'prod') {
      const NOTICE_KEY = 'fpulse_creds_moved_notice_shown';
      const notified = sessionStorage.getItem(NOTICE_KEY);
      if (!notified) {
        toast.info(
          'Credentials has moved to Vault in PROD',
          'Plus tier uses the Vault for PROD secrets — rotation, audit, AES-256. Use Credentials on DEV for legacy / transitional workflows.',
        );
        sessionStorage.setItem(NOTICE_KEY, '1');
      }
      // Vault is part of the commercial extension; stay on credentials.
    }
  }, [page, tier, environment, navigate]);

  const handleLogin = (userData: any) => {
    // Always start a fresh session in DEV. Without this, the previous user's
    // `fpulse_env` lingers in localStorage and the next sign-in lands directly
    // in PROD — which is wrong on a shared machine and especially wrong when
    // the new user is a developer who shouldn't be deploying anything.
    setEnvironment('dev');
    localStorage.setItem('fpulse_env', 'dev');
    setUser(userData);
    // Re-fetch the license tier on login. The login form may have been sitting
    // open across a license activate/deactivate, and we don't want the new
    // session to start with a stale tier value.
    api.getLicenseStatus()
      .then((status) => {
        const t = status.is_plus ? 'plus' : 'free';
        setTier(t);
      })
      .catch(() => { /* keep whatever we had */ });
  };

  // Full logout flow:
  //   1. Tell the backend to invalidate the session token (best-effort — we
  //      still clear local state even if the call fails so a broken network
  //      can't leave the user trapped in a half-logged-in UI).
  //   2. Wipe every auth-adjacent key from localStorage, INCLUDING fpulse_tier
  //      and fpulse_env. We used to leave these around as "preferences", but
  //      that meant when an admin signed out and a developer signed in on the
  //      same machine, the developer landed in PROD with a stale `tier=plus`
  //      already in state — making the dashboard show Plus widgets for ~1
  //      paint cycle before the refetch corrected things. Wipe everything.
  //   3. Force a full page reload. This is the only safe way to drop every
  //      Zustand store, every React Query cache, and every component-level
  //      `useState` holding the previous user's pipelines/dashboard/project
  //      data. Trying to surgically reset each one is whack-a-mole.
  const handleLogout = async () => {
    // 2026-05-19 (P0 #8 of PAGE_BY_PAGE_AUDIT.md): the in-app Logout button
    // bypassed Toolbar.tsx's beforeunload guard and silently discarded any
    // unsaved canvas edit / ConfigPanel draft / Copilot chat. Check the
    // workflow-store dirty flag first and let the user save or explicitly
    // discard before we wipe state.
    try {
      const { isDirty, workflowId, ensureWorkflow } = useWorkflowStore.getState();
      if (isDirty && workflowId) {
        const choice = await uiConfirm({
          title: 'You have unsaved changes',
          message: 'Save your pipeline before logging out? Unsaved canvas edits will be lost otherwise.',
          confirmLabel: 'Save & log out',
          cancelLabel: 'Discard & log out',
        });
        if (choice) {
          try { await ensureWorkflow(); } catch { /* still log out even if save failed */ }
        }
      }
    } catch {
      /* if the store throws, fall through to logout regardless */
    }
    try {
      await api.logout();
    } catch {
      // Swallow — the token may already be expired or the backend down.
      // Local cleanup below is the source of truth for the UI.
    }
    localStorage.removeItem('fpulse_token');
    localStorage.removeItem('fpulse_auth_token');
    localStorage.removeItem('fpulse_user');
    localStorage.removeItem('fpulse_tier');
    localStorage.removeItem('fpulse_tier_cache');
    localStorage.removeItem('fpulse_tier_cache_v1');
    localStorage.removeItem('fpulse_env');
    // Workspace context (schema v2). Wipe both the cached membership list
    // AND the current-workspace pointer so the next login starts clean —
    // otherwise an admin who logs out leaves their personal workspace id
    // in storage, and the developer who logs in next would briefly see
    // "no projects" because the api client would send the wrong header
    // until LoginPage's success handler overwrites it.
    localStorage.removeItem('fpulse_workspace_id');
    localStorage.removeItem('fpulse_workspaces');
    // Hard reload — see comment above for why surgical resets aren't enough.
    window.location.href = '/';
  };

  // Session-expiry listener — fires when api/client.ts catches a 401 and
  // dispatches `fpulse:session-expired`. We attempt to autosave the canvas
  // before reloading so the user doesn't lose work to an expired token.
  // The fallback timeout in client.ts guarantees a reload even if this
  // handler crashes. Per docs/PAGE_BY_PAGE_AUDIT.md (P0 #8, 2026-05-19).
  useEffect(() => {
    const onSessionExpired = async () => {
      try {
        const { isDirty, workflowId, ensureWorkflow } = useWorkflowStore.getState();
        if (isDirty && workflowId) {
          // Best-effort autosave. We don't prompt the user here — by the
          // time the 401 fires the token is already gone, so a Save call
          // through ensureWorkflow would itself fail. Instead we let the
          // user know via a banner and reload to LoginPage. Future work
          // (P1): persist the dirty IR to localStorage so the user can
          // restore it after re-auth.
          try { await ensureWorkflow(); } catch { /* token's gone — expected */ }
        }
      } catch {
        /* swallow — reload below */
      }
      // The localStorage clear happened in client.ts already; we just
      // navigate to dashboard so LoginPage mounts cleanly.
      routerNavigateTo('dashboard');
      window.location.reload();
    };
    window.addEventListener('fpulse:session-expired', onSessionExpired);
    return () => window.removeEventListener('fpulse:session-expired', onSessionExpired);
  }, []);

  // Track recent pages for global search
  useEffect(() => {
    const recent = JSON.parse(localStorage.getItem('fpulse_recent_pages') || '[]') as string[];
    const updated = [page, ...recent.filter((p: string) => p !== page)].slice(0, 5);
    localStorage.setItem('fpulse_recent_pages', JSON.stringify(updated));
  }, [page]);

  // 2026-05-19 (P1 #14 of PAGE_BY_PAGE_AUDIT.md): centralised
  // backend-unreachable banner. Listens for `fpulse:backend-reachable`
  // dispatched from api/client.ts — flips the state and renders a
  // sticky banner at the very top of the shell when the backend is
  // unreachable, with explanatory copy + a Retry that probes /api/health.
  // Replaces the previous per-page kaleidoscope of silent empties and
  // mystery toasts.
  const [backendDown, setBackendDown] = useState<{ down: boolean; reason?: string }>({ down: false });
  useEffect(() => {
    const onReachable = (e: Event) => {
      const detail = (e as CustomEvent).detail as { reachable: boolean; reason?: string };
      setBackendDown({ down: !detail.reachable, reason: detail.reason });
    };
    window.addEventListener('fpulse:backend-reachable', onReachable);
    return () => window.removeEventListener('fpulse:backend-reachable', onReachable);
  }, []);
  const retryBackend = async () => {
    try {
      // Any 2xx response will clear the flag via the api client's emitter.
      await fetch('/api/health', { method: 'GET' });
      // If we didn't actually reach a 2xx the emitter will keep it down.
    } catch {
      /* still down — banner stays */
    }
  };

  // Global keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Cmd+K / Ctrl+K — Global Search
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        setSearchOpen((prev) => !prev);
        return;
      }
      // 2026-05-19 (P2 #20 of PAGE_BY_PAGE_AUDIT.md): "?" or Ctrl+/ opens
      // the Help → Shortcuts cheat-sheet. Previously the only way to find
      // the cheat-sheet was to navigate Help → click the Shortcuts tab.
      // The hash form `#help/shortcuts` is read by HelpPage's mount-time
      // tab init so a direct click on the link lands the user on the
      // right tab. We ignore the chord when an editable element has focus
      // so it doesn't intercept the obvious "type ?" use case.
      const editable =
        document.activeElement instanceof HTMLInputElement ||
        document.activeElement instanceof HTMLTextAreaElement ||
        (document.activeElement as HTMLElement | null)?.isContentEditable;
      if (!editable && (e.key === '?' || ((e.ctrlKey || e.metaKey) && e.key === '/'))) {
        e.preventDefault();
        navigateToSubRoute('help', 'shortcuts');
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        const { workflowId, ensureWorkflow } = useWorkflowStore.getState();
        if (workflowId) ensureWorkflow();
      }
      if (e.key === 'Escape') {
        const store = useWorkflowStore.getState();
        if (store.codeEditorOpen) {
          store.setCodeEditorOpen(false);
        } else {
          store.setSelectedNode(null);
          // Deselect all nodes and edges
          useWorkflowStore.setState({
            nodes: store.nodes.map((n) => ({ ...n, selected: false })),
            edges: store.edges.map((e) => ({ ...e, selected: false })),
          });
        }
      }
      // Undo/Redo
      if ((e.ctrlKey || e.metaKey) && e.key === 'z' && !e.shiftKey && !['INPUT', 'TEXTAREA'].includes((e.target as HTMLElement).tagName)) {
        e.preventDefault();
        useWorkflowStore.getState().undo();
      }
      if ((e.ctrlKey || e.metaKey) && (e.key === 'y' || (e.key === 'z' && e.shiftKey)) && !['INPUT', 'TEXTAREA'].includes((e.target as HTMLElement).tagName)) {
        e.preventDefault();
        useWorkflowStore.getState().redo();
      }
      if ((e.key === 'Delete' || e.key === 'Backspace') && !['INPUT', 'TEXTAREA', 'SELECT'].includes((e.target as HTMLElement).tagName)) {
        const store = useWorkflowStore.getState();
        // Multi-select delete: remove all selected nodes + orphaned edges
        const selectedNodes = store.nodes.filter((n) => n.selected);
        if (selectedNodes.length > 1) {
          e.preventDefault();
          store.pushUndoState();
          const selectedIds = new Set(selectedNodes.map((n) => n.id));
          useWorkflowStore.setState({
            nodes: store.nodes.filter((n) => !selectedIds.has(n.id)),
            edges: store.edges.filter((edge) => !selectedIds.has(edge.source) && !selectedIds.has(edge.target)),
            selectedNodeId: null,
          });
        } else if (store.selectedNodeId) {
          e.preventDefault();
          store.deleteNode(store.selectedNodeId);
        }
      }
      // Select All (Ctrl+A) — select all nodes + edges on canvas
      if ((e.ctrlKey || e.metaKey) && e.key === 'a' && !['INPUT', 'TEXTAREA', 'SELECT'].includes((e.target as HTMLElement).tagName)) {
        e.preventDefault();
        const store = useWorkflowStore.getState();
        if (store.nodes.length > 0) {
          useWorkflowStore.setState({
            nodes: store.nodes.map((n) => ({ ...n, selected: true })),
            edges: store.edges.map((e) => ({ ...e, selected: true })),
          });
        }
      }
      // Copy (Ctrl+C) — copy selected nodes + their edges as JSON
      if ((e.ctrlKey || e.metaKey) && e.key === 'c' && !['INPUT', 'TEXTAREA', 'SELECT'].includes((e.target as HTMLElement).tagName)) {
        const store = useWorkflowStore.getState();
        const selectedNodes = store.nodes.filter((n) => n.selected);
        if (selectedNodes.length === 0) return;
        const selectedIds = new Set(selectedNodes.map((n) => n.id));
        const selectedEdges = store.edges.filter((edge) => selectedIds.has(edge.source) && selectedIds.has(edge.target));
        const clipboard = {
          fpulse_clipboard: true,
          nodes: selectedNodes.map((n) => ({
            id: n.id,
            type: n.data.stepType,
            label: n.data.label,
            params: n.data.params || {},
            position: { x: n.position.x, y: n.position.y },
          })),
          connections: selectedEdges.map((edge) => ({
            from_step: edge.source,
            to_step: edge.target,
            condition: (edge.data as any)?.condition || 'completion',
          })),
        };
        navigator.clipboard.writeText(JSON.stringify(clipboard, null, 2));
      }
      // Paste (Ctrl+V) — paste nodes from JSON clipboard
      if ((e.ctrlKey || e.metaKey) && e.key === 'v' && !['INPUT', 'TEXTAREA', 'SELECT'].includes((e.target as HTMLElement).tagName)) {
        e.preventDefault();
        navigator.clipboard.readText().then((text) => {
          try {
            const data = JSON.parse(text);
            if (!data.fpulse_clipboard || !data.nodes?.length) return;
            const store = useWorkflowStore.getState();
            store.pushUndoState();
            const idMap: Record<string, string> = {};
            const offset = 60;
            // Deselect existing nodes
            const updatedNodes = store.nodes.map((n) => ({ ...n, selected: false }));
            // Create new nodes with new IDs, offset position
            const newNodes: any[] = data.nodes.map((n: any) => {
              const newId = `${n.type}_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
              idMap[n.id] = newId;
              return {
                id: newId,
                type: 'fpulseNode',
                position: { x: n.position.x + offset, y: n.position.y + offset },
                selected: true,
                data: {
                  label: n.label,
                  stepType: n.type,
                  params: n.params || {},
                  color: (store as any).nodes[0]?.data?.color || '#94a3b8', // will be set by addNode colors
                },
              };
            });
            // Re-apply colors/icons from NODE_COLORS etc. — use addNode pattern
            // Actually, let's just import from the store's addNode internals
            const { NODE_COLORS, NODE_ICONS, NODE_CATEGORY } = (() => {
              // Read from a dummy addNode call — but simpler to just set them from existing maps
              // We'll use the store's internal maps via a utility
              return { NODE_COLORS: null, NODE_ICONS: null, NODE_CATEGORY: null };
            })();
            // Simpler: just use the addNode function in a loop
            const addedNodes: any[] = [];
            for (const n of data.nodes) {
              const newId = idMap[n.id];
              store.addNode(n.type, { x: n.position.x + offset, y: n.position.y + offset });
              // Get the latest added node and update its label/params
              const latestNodes = useWorkflowStore.getState().nodes;
              const added = latestNodes[latestNodes.length - 1];
              if (added) {
                idMap[n.id] = added.id;
                store.updateNodeLabel(added.id, n.label);
                store.updateNodeParams(added.id, n.params || {});
                addedNodes.push(added);
              }
            }
            // Add edges between pasted nodes
            if (data.connections?.length) {
              const { edges: currentEdges } = useWorkflowStore.getState();
              const CONDITION_COLORS: Record<string, string> = { completion: '#6366f1', success: '#22c55e', failure: '#ef4444' };
              const newEdges = data.connections
                .filter((c: any) => idMap[c.from_step] && idMap[c.to_step])
                .map((c: any) => {
                  const condition = c.condition || 'completion';
                  const color = CONDITION_COLORS[condition] || '#6366f1';
                  return {
                    id: `e-${idMap[c.from_step]}-${idMap[c.to_step]}`,
                    source: idMap[c.from_step],
                    target: idMap[c.to_step],
                    type: 'custom',
                    animated: true,
                    data: { condition },
                    style: { stroke: color, strokeWidth: 2 },
                    markerEnd: { type: 'arrowclosed', width: 16, height: 16, color },
                  };
                });
              useWorkflowStore.setState({ edges: [...currentEdges, ...newEdges] });
            }
            // Select only pasted nodes
            const finalNodes = useWorkflowStore.getState().nodes;
            const pastedIds = new Set(Object.values(idMap));
            useWorkflowStore.setState({
              nodes: finalNodes.map((n) => ({ ...n, selected: pastedIds.has(n.id) })),
            });
          } catch {
            // Not F-Pulse clipboard data — ignore
          }
        });
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  // Show nothing until auth check completes
  if (!authChecked) return null;

  // Auth gate — anyone without a valid session sees the LoginPage.
  //
  // Escape hatch for local development: setting `VITE_FPULSE_DEV_AUTOLOGIN=1`
  // (or `localStorage.fpulse_dev_autologin=1`) skips the gate so devs working
  // on UI that doesn't need auth aren't forced through login on every HMR.
  // This flag is deliberately NOT read in production builds — Vite inlines
  // `import.meta.env` at build time, so shipping without the env var gives
  // you the real gate automatically.
  //
  // 2026-05-19 (P2 #16 of PAGE_BY_PAGE_AUDIT.md): the env-var path is
  // build-time and safe. The localStorage path, however, persists across
  // production builds — a curious dev who toggles it in DevTools and
  // forgets is then in bypass mode silently. We now distinguish the two
  // sources so the chrome can render a "DEV AUTOLOGIN" banner when the
  // bypass is active, with one-click "Disable" to clear the localStorage
  // flag.
  const devAutoLoginFromEnv = import.meta.env.VITE_FPULSE_DEV_AUTOLOGIN === '1';
  const devAutoLoginFromLocal = localStorage.getItem('fpulse_dev_autologin') === '1';
  const devAutoLogin = devAutoLoginFromEnv || devAutoLoginFromLocal;

  if (!user && !devAutoLogin) {
    return (
      <>
        <LoginPage onLogin={handleLogin} />
        <Toast />
      </>
    );
  }

  const handleSelectProject = (id: string, name?: string) => {
    setActiveProjectId(id);
    setActiveProjectName(name || id);
    navigate('pipelines');
  };

  const clearProject = () => {
    setActiveProjectId(null);
    setActiveProjectName('');
  };

  const handleOnboardingComplete = async (projectName: string, template?: string) => {
    setShowOnboarding(false);
    // Persist the project so it survives reload and appears on the
    // Projects page. We only attempt the API call when a session token
    // exists — that covers the normal first-run path (bootstrap user
    // signed in) and avoids a noisy 401 toast on an anonymous preview.
    const trimmed = projectName.trim() || 'My First Project';
    setActiveProjectName(trimmed);
    if (localStorage.getItem('fpulse_token')) {
      try {
        const created = await api.createProject({ name: trimmed });
        setActiveProjectId(created.id);
        setActiveProjectName(created.name);
      } catch (err: any) {
        toast.error('Could not create project', err?.message || 'Try again from the Projects page.');
      }
    }
    if (template) {
      navigate('editor');
      setTimeout(() => useWorkflowStore.getState().useTemplate(template), 300);
    } else {
      navigate('projects');
    }
  };

  const handleSearchAction = (action: string) => {
    switch (action) {
      case 'new_project': navigate('projects'); break;
      case 'new_pipeline': navigate('templates'); break;
      case 'run_workflow': useWorkflowStore.getState().runWorkflow(); break;
      // 2026-05-19 (P2 #11 of PAGE_BY_PAGE_AUDIT.md): the dark-mode
      // toggle case is left as a no-op (no class toggle, no localStorage
      // write) because main.tsx force-clears `fpulse_theme` on every
      // boot — the previous body worked for the current session and
      // then silently undid itself on reload, which was worse than the
      // action being broken. The matching GlobalSearch row that emitted
      // this action has also been removed.
      case 'toggle_dark':
        break;
      case 'export_pipeline': {
        const { nodes: n, edges: ed, workflowName: wn } = useWorkflowStore.getState();
        const blob = new Blob([JSON.stringify({ name: wn, steps: n.map(nd => ({ id: nd.id, type: nd.data.stepType, label: nd.data.label, params: nd.data.params || {}, position: nd.position })), connections: ed.map(e => ({ from_step: e.source, to_step: e.target })) }, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = url; a.download = `${wn.replace(/\s+/g, '_').toLowerCase()}.fpulse.json`; a.click(); URL.revokeObjectURL(url);
        break;
      }
    }
  };

  return (
    <ErrorBoundary>
    <DialogRoot>
    {/* data-env drives theme v2 CSS overrides (see globals.css → "Theme v2 env-aware overrides").
        Lets the old navy-gradient theads + bg-white page headers resolve to different colours
        per environment without touching every page's JSX. */}
    <div data-env={environment} className="h-screen w-screen flex flex-col overflow-hidden bg-canvas-bg">
      {/* 2026-06-02 LAN-binding warning — visible only when the backend
          actually resolved to a non-loopback bind (FPULSE_ALLOW_LAN=1).
          Renders null in the common (loopback-default) case. */}
      <BindWarningBanner />
      {/* Onboarding wizard — first time only */}
      {showOnboarding && (
        <OnboardingWizard
          onComplete={handleOnboardingComplete}
          onSkip={() => setShowOnboarding(false)}
        />
      )}

      {/* Global Search (Cmd+K) */}
      <GlobalSearch
        open={searchOpen}
        onClose={() => setSearchOpen(false)}
        onNavigate={(p) => { navigate(p as Page); setSearchOpen(false); }}
        onAction={(a) => { handleSearchAction(a); setSearchOpen(false); }}
      />

      {/* Environment accent stripe */}
      <div className={`h-[3px] shrink-0 ${
        environment === 'prod'
          ? 'bg-gradient-to-r from-red-500 via-red-400 to-orange-500'
          : 'bg-gradient-to-r from-emerald-400 via-emerald-500 to-teal-500'
      }`} />

      {/* 2026-05-19 (P2 #16 of PAGE_BY_PAGE_AUDIT.md): DEV AUTOLOGIN
          banner. Surfaces whenever the localStorage bypass is active so a
          dev who toggled it in DevTools and forgot doesn't sit in
          permanent bypass mode. The env-var-driven path doesn't show the
          banner (operators know they set it; bothering them on every page
          load would be noise). Click Disable to clear the flag + reload
          into the real login. */}
      {devAutoLoginFromLocal && (
        <div className="bg-amber-500 text-amber-950 px-6 py-1.5 flex items-center justify-between gap-4">
          <div className="flex items-center gap-2 min-w-0">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
              <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
              <line x1="12" y1="9" x2="12" y2="13" />
              <line x1="12" y1="17" x2="12.01" y2="17" />
            </svg>
            <span className="text-xs font-bold uppercase tracking-wider shrink-0">Dev autologin active</span>
            <span className="text-xs truncate">
              <code className="bg-amber-100/60 px-1 rounded">localStorage.fpulse_dev_autologin = "1"</code> is bypassing the login screen on this browser. Real RBAC still applies at the API.
            </span>
          </div>
          <button
            onClick={() => {
              try { localStorage.removeItem('fpulse_dev_autologin'); } catch { /* noop */ }
              window.location.reload();
            }}
            className="px-2.5 py-0.5 text-xs font-bold rounded-md bg-amber-950 text-amber-100 hover:bg-amber-900 shrink-0"
          >
            Disable
          </button>
        </div>
      )}

      {/* 2026-05-19 (P1 #14 of PAGE_BY_PAGE_AUDIT.md): global
          backend-unreachable banner. Renders when api/client.ts has hit a
          true network error (TypeError, not a 4xx/5xx). Sits ABOVE the
          Ollama recommendation + Sidebar so it's the first thing the user
          notices. The banner clears itself the moment any /api/* call
          succeeds. */}
      {backendDown.down && (
        <div role="alert" className="bg-red-600 text-white px-6 py-2 flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
            <span className="text-xs font-bold uppercase tracking-wider shrink-0">Backend unreachable</span>
            <span className="text-xs truncate">
              The F-Pulse API is not responding ({backendDown.reason || 'network error'}). The UI is showing the last data it loaded.
            </span>
          </div>
          <button
            onClick={retryBackend}
            className="px-3 py-1 text-xs font-bold rounded-md bg-white text-red-700 hover:bg-red-50 shrink-0"
          >
            Retry now
          </button>
        </div>
      )}

      {/* First-launch nudge: recommend qwen2.5:7b (the 2026-05-19 tool-use
          floor) when Ollama is the active provider but no model is
          installed, the active model is below the floor (1.5b/3b), or the
          active model is too heavy for CPU. Dismissible; sticky via
          localStorage. */}
      <OllamaRecommendationBanner />

      {/* Top header nav */}
      {/* OSS-9 (2026-05-19): Sidebar + App now both import `Page` from
          the canonical `./types`, so the previous `as any` casts on
          activePage / onNavigate are gone. */}
      <Sidebar activePage={page} onNavigate={navigate} user={user} onLogout={handleLogout} environment={environment} onEnvironmentChange={setEnvironment} tier={tier} />

      {/* Project context breadcrumb was here — it now renders INSIDE
          each project-scoped page (Pipelines, Executions, Credentials,
          Connections) via <ProjectContextBar/>, sitting right below the
          page header with sticky-top so it stays visible while scrolling.
          Global surfaces (Dashboard, Admin, Help, Runbook, Settings,
          Account) no longer show it — they aren't project-scoped. */}

      {/* Main content */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* V10 — OSS Production placeholder. When a Free-tier user
            switches env to PROD, replace the page content with a
            single clean CTA instead of greying out every page. Plus
            users keep the existing PROD experience (canvas turns
            read-only via the opacity-75 + pointer-events-none on the
            editor row below). */}
        {tier === 'free' && environment === 'prod' ? (
          <OSSProductionPlaceholder onSwitchToDev={() => setEnvironment('dev')} />
        ) : (
          <Suspense fallback={
            <div className="flex-1 flex items-center justify-center text-slate-400">
              <div className="text-sm flex items-center gap-2">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="animate-spin">
                  <circle cx="12" cy="12" r="10" strokeDasharray="60" strokeDashoffset="20" />
                </svg>
                Loading…
              </div>
            </div>
          }>
          <>
        {page === 'editor' && (
          <>
            {/* Editor's page header lives in the Toolbar component
                itself — it's already a 78px banner with title +
                workflow-name input + Save/Run/Publish buttons. The
                HubTabs strip is rendered inside the Toolbar (center
                slot) so the Editor matches the same chrome Insights /
                Settings use, with no duplicate stacked headers. */}
            {/* PROD guard: canvas is view-only in PROD — all edits happen in DEV */}
            {environment === 'prod' && (
              <div className="px-4 py-2.5 bg-amber-50 border-b border-amber-200 flex items-center gap-2 shrink-0">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-amber-600">
                  <rect x="3" y="11" width="18" height="11" rx="2" />
                  <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                </svg>
                <span className="text-xs font-medium text-amber-800">
                  PROD Read-Only · Pipelines are edited in DEV, tested, then deployed via the Approvals workflow. Switch to DEV to make changes.
                </span>
              </div>
            )}
            {/* 2026-05-28 — Published-pipeline edit banner.
                Reported in internal testing: dragged a node onto a published
                pipeline canvas to test, didn't click Save, auto-save
                silently committed the half-baked change to the live
                version, the scheduled run later failed with "Unknown
                error" + alert email. Auto-save is now suspended on
                published pipelines (see Canvas.tsx line ~440); this
                banner explains the suspension so the user understands
                why their edits aren't persisting and what to do. */}
            {environment !== 'prod' && editorWorkflowStatus === 'published' && (
              <div className="px-4 py-2.5 bg-violet-50 border-b border-violet-200 flex items-center gap-2 shrink-0">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-violet-600 shrink-0">
                  <circle cx="12" cy="12" r="10" />
                  <line x1="12" y1="8" x2="12" y2="12" />
                  <line x1="12" y1="16" x2="12.01" y2="16" />
                </svg>
                <span className="text-xs font-medium text-violet-800">
                  <strong>Published pipeline.</strong> Auto-save is suspended so live runs aren't broken by exploratory edits.
                  {editorIsDirty
                    ? ' You have unsaved changes — click Save to commit them as a new draft (the published version stays untouched until you Publish again).'
                    : ' Click Save to commit any edits as a new draft.'}
                </span>
              </div>
            )}
            <Toolbar tier={tier} environment={environment} />
            {/* Editor row — nodes | (canvas + preview) | chat. Chat moved
                to the right (May 10 2026) so it aligns with the floating
                F-Pulse Copilot button's bottom-right position on every
                other page — one consistent "AI lives on the right"
                mental model across the app. */}
            <div className={`flex-1 flex overflow-hidden ${environment === 'prod' ? 'pointer-events-none opacity-75' : ''}`}>
              {/* 2026-05-19 (P1 #13 of PAGE_BY_PAGE_AUDIT.md): each editor
                  panel is wrapped in its own <PanelErrorBoundary> so a
                  render bug in one (e.g. ModulesPanel palette failing to
                  group, ChatPanel agent state going sideways, ConfigPanel
                  blowing up on an unknown step type) keeps the others
                  alive instead of collapsing the whole shell to the
                  app-level fallback. */}
              {/* Node palette — LEFT */}
              <PanelErrorBoundary name="Node palette">
                <ModulesPanel />
              </PanelErrorBoundary>
              {/* Canvas + Preview — CENTER. The EditorContextBar sits at
                  the top of this column so its width tracks the canvas,
                  not the full window — resizing either side panel
                  resizes the bar with them. */}
              <div className="flex-1 flex flex-col overflow-hidden min-w-0 isolate relative">
                <EditorContextBar />
                <div className="flex-1 flex overflow-hidden relative">
                  <Canvas />
                  {codeEditorOpen && <CodeEditorPanel />}
                  {/* Brand mark — pinned to the bottom-left of the canvas
                      column so it stays visible even when a side panel
                      covers the global CopyrightFooter. */}
                  <CanvasCopyrightMark />
                </div>
                <PreviewPanel />
              </div>
              {/* AI Copilot — DOCKED on the right of the editor (restored
                  2026-06-17 per user request). The Editor keeps its own
                  canvas-aware Copilot (guided builder + workflow-store agent);
                  the floating FloatingAgentWidget is suppressed on the editor
                  route (see the mount guard below) so the canvas shows exactly
                  one AI surface. */}
              <PanelErrorBoundary name="Copilot">
                <ChatPanel />
              </PanelErrorBoundary>
            </div>
            {!codeEditorOpen && (
              <PanelErrorBoundary name="Config panel">
                <ConfigPanel />
              </PanelErrorBoundary>
            )}
          </>
        )}
        {page === 'projects' && <ProjectsPage onSelectProject={handleSelectProject} environment={environment} tier={tier} />}
        {page === 'pipelines' && <PipelinesPage onOpenEditor={() => navigate('editor')} projectId={activeProjectId} projectName={activeProjectName} onClearProject={clearProject} onGoToProjects={() => { clearProject(); navigate('projects'); }} environment={environment} tier={tier} />}
        {page === 'templates' && <TemplatesPage onOpenEditor={() => navigate('editor')} environment={environment} tier={tier} />}
        {page === 'executions' && <ExecutionsPage projectId={activeProjectId} projectName={activeProjectName} onClearProject={clearProject} onGoToProjects={() => { clearProject(); navigate('projects'); }} environment={environment} tier={tier} />}
        {/* Schedules removed — scheduling is pipeline-level via Quick Schedule */}
        {/* AlertsPage removed — alerts are pipeline-level, history in Notifications */}
        {page === 'credentials' && <CredentialsPage projectId={activeProjectId} projectName={activeProjectName} onClearProject={clearProject} onGoToProjects={() => { clearProject(); navigate('projects'); }} environment={environment} tier={tier} />}
        {page === 'connections' && <ConnectionsPage projectId={activeProjectId} projectName={activeProjectName} onClearProject={clearProject} onGoToProjects={() => { clearProject(); navigate('projects'); }} environment={environment} tier={tier} />}
        {page === 'dashboard' && <DashboardPage onNavigate={(p) => navigate(p as Page)} userName={user?.name} environment={environment} tier={tier} />}
        {/* Reports → AI hub (Reports tab). Standalone reports route still works (legacy bookmarks). */}
        {page === 'reports' && <AIPage environment={environment} tier={tier} user={user} initialTab="reports" />}
        {page === 'account' && <AccountPage onProfileUpdated={(u) => setUser(u)} onNavigate={(p) => navigate(p as Page)} environment={environment} tier={tier} />}
        {page === 'notifications' && <NotificationsPage environment={environment} tier={tier} />}
        {page === 'pool' && <ExecutionPoolPage environment={environment} tier={tier} />}
        {/* 2026-05-23 (Y4) — workspace datastore (Files / Managed Tables / Outputs). */}
        {page === 'storage' && <StoragePage projectId={activeProjectId} projectName={activeProjectName} onClearProject={clearProject} onGoToProjects={() => { clearProject(); navigate('projects'); }} environment={environment} tier={tier} />}
        {/* Intelligence page removed */}
        {/* Monitor removed — Dashboard covers operational overview */}
        {page === 'lineage' && <LineagePage environment={environment} tier={tier} />}
        {/* Marketplace page removed */}
        {/* Gateway and Plugins removed from nav — accessible via Admin */}
        {page === 'settings' && <SettingsPage environment={environment} tier={tier} />}
        {page === 'help' && <HelpPage environment={environment} tier={tier} user={user} />}
        {/* Insights — primary nav entry (consolidates Activity + Trust + Reports + AI Provider).
            Legacy /#trust and /#activity routes resolve to the hub with the right initial tab. */}
        {page === 'ai' && <AIPage environment={environment} tier={tier} user={user} />}
        {page === 'insights' && <AIPage environment={environment} tier={tier} user={user} />}
        {page === 'trust' && <AIPage environment={environment} tier={tier} user={user} initialTab="trust" />}
        {page === 'activity' && <AIPage environment={environment} tier={tier} user={user} initialTab="activity" />}
        {page === 'author' && <AIPage environment={environment} tier={tier} user={user} initialTab="author" />}
        {page === 'gallery' && <AIPage environment={environment} tier={tier} user={user} initialTab="gallery" />}
        {/* #cert-matrix folded into Insights → Trust — redirect legacy/bookmarked
            URLs to the Trust tab, where the full matrix is an expandable section. */}
        {page === 'cert-matrix' && <AIPage environment={environment} tier={tier} user={user} initialTab="trust" />}
        {page === 'extraction' && <ExtractionPage />}
          </>
          </Suspense>
        )}
      </div>
      {/* Floating AI Agent widget — the canonical Copilot on every page
          EXCEPT the Editor, which has its own docked ChatPanel (restored
          2026-06-17). Suppressing it on the editor route keeps exactly one
          AI surface on the canvas. Default ON elsewhere for every tier;
          explicit opt-out via localStorage `fpulse.ui.showCopilotWidget=false`;
          the widget's own "Configure AI Provider" banner handles the
          unconfigured-LLM case. */}
      {page !== 'editor' && shouldShowCopilotWidget(tier) && <FloatingAgentWidget />}

      {/* @hybridyn copyright — fixed bottom-right, omitted on editor
          where canvas real-estate is precious. */}
      {/* CopyrightFooter accepts `environment` only; `tier` is read from a
          context provider in newer revs. Kept here as a noop prop until the
          canonical fix lands in 1.0.1. */}
      {page !== 'editor' && <CopyrightFooter environment={environment} {...({ tier } as any)} />}

      <Toast />
    </div>
    </DialogRoot>
    </ErrorBoundary>
  );
}
