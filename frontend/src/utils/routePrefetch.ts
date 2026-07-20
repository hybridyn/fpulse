/**
 * Route prefetch — closes the perceived-latency gap introduced by
 * lazy-loading the page chunks.
 *
 * The story:
 *   - App.tsx wraps every non-default page in React.lazy() so the main
 *     bundle stays at ~860 kB instead of ~2.5 MB.
 *   - The cost: the FIRST time a user clicks a tab in a session, the
 *     browser has to fetch that route's chunk before the page can
 *     render. On a fast connection that's ~50-300 ms — visible as a
 *     brief "Loading…" spinner flash. On slow connections it can be
 *     500 ms-1 s.
 *
 * Two prefetch strategies live here:
 *
 *   1. `prefetchRoute(page)` — fire-and-forget. Called from the
 *      Sidebar's `onMouseEnter` so by the time the user *clicks* a
 *      tab (typically 100-500 ms after hover), the chunk is already in
 *      the browser cache and the click renders instantly.
 *
 *   2. `prefetchAllRoutes()` — silent background fetch of every route.
 *      Called from App.tsx once after first paint via
 *      `requestIdleCallback`. After ~5 s of any user activity, every
 *      tab is warm and the Suspense fallback essentially never fires.
 *
 * Both strategies are idempotent — a `fired` set tracks in-flight /
 * completed fetches so a hover that lands on a tab after the idle
 * sweep already prefetched it is a no-op. Failed fetches drop out of
 * the set so a retry can happen if the user does hover later.
 *
 * Why no react-router preload library: F-Pulse uses a custom hash
 * router (~/router.ts) without a route table to introspect, so the
 * mapping below is the simplest source of truth. New lazy routes must
 * register here AND in App.tsx — annoying but small.
 */

import type { Page } from '../types';

// Map from page id → the dynamic import that loads the page's chunk.
// MUST stay in sync with the `lazy(() => import(...))` calls at the top
// of App.tsx. The keys are the values App.tsx checks against `page ===
// 'X'`; the values are bare `() => import(...)` thunks so calling them
// triggers a chunk fetch but ignores the resolved module (we don't need
// it — React.lazy + Suspense handle the actual render).
//
// Pages NOT in this map (e.g. 'dashboard', 'login', 'editor') are
// either eager imports or rendered via composed chunks (the editor's
// Canvas / ChatPanel / etc. each have their own dynamic import but a
// 'editor' hover fires the most useful three below).
const ROUTE_LOADERS: Partial<Record<Page, () => Promise<unknown>>> = {
  pipelines:     () => import('../components/pages/PipelinesPage'),
  templates:     () => import('../components/pages/TemplatesPage'),
  executions:    () => import('../components/pages/ExecutionsPage'),
  extraction:    () => import('../components/pages/ExtractionPage'),
  credentials:   () => import('../components/pages/CredentialsPage'),
  settings:      () => import('../components/pages/SettingsPage'),
  connections:   () => import('../components/pages/ConnectionsPage'),
  projects:      () => import('../components/pages/ProjectsPage'),
  help:          () => import('../components/pages/HelpPage'),
  trust:         () => import('../components/pages/AIPage'),
  'cert-matrix': () => import('../components/pages/AIPage'),  // folded into Insights → Trust
  activity:      () => import('../components/pages/AIPage'),
  reports:       () => import('../components/pages/AIPage'),
  ai:            () => import('../components/pages/AIPage'),
  insights:      () => import('../components/pages/AIPage'),
  author:        () => import('../components/pages/AIPage'),
  account:       () => import('../components/pages/AccountPage'),
  notifications: () => import('../components/pages/NotificationsPage'),
  pool:          () => import('../components/pages/ExecutionPoolPage'),
  storage:       () => import('../components/pages/StoragePage'),
  lineage:       () => import('../components/pages/LineagePage'),
};

// Editor route loads multiple chunks (Canvas, ConfigPanel, ChatPanel,
// ModulesPanel, PreviewPanel, CodeEditorPanel). Hovering the Editor
// tab kicks off the most expensive three in parallel.
const EDITOR_LOADERS: Array<() => Promise<unknown>> = [
  () => import('../components/Canvas'),
  () => import('../components/ConfigPanel'),
  () => import('../components/ModulesPanel'),
  () => import('../components/ChatPanel'),
  () => import('../components/PreviewPanel'),
];

const fired = new Set<string>();

/**
 * Prefetch the chunk(s) for a route. Idempotent — calling repeatedly
 * for the same page hits the in-flight / browser cache once and is a
 * no-op thereafter. Failures drop the entry so a later hover can
 * retry (e.g. transient network blip during a slow first-paint).
 *
 * Designed to be called from `onMouseEnter` on nav items. Cheap; no
 * await, no error propagation — a prefetch that fails just degrades
 * to the existing on-click load path.
 */
export function prefetchRoute(page: Page | 'editor'): void {
  if (fired.has(page)) return;
  fired.add(page);

  if (page === 'editor') {
    Promise.all(EDITOR_LOADERS.map((load) => load())).catch(() => {
      fired.delete(page);
    });
    return;
  }

  const loader = ROUTE_LOADERS[page];
  if (!loader) return;
  loader().catch(() => {
    fired.delete(page);
  });
}

/**
 * Background-warm every lazy route. Call once from App.tsx after first
 * paint. Uses `requestIdleCallback` so the prefetch never competes with
 * user interactions or the initial render; on browsers without that
 * API (Safari < 17, some embedded WebViews), falls back to a 2-second
 * `setTimeout` which lands well after first paint but doesn't block.
 *
 * Total prefetch payload at the time of writing is ~1.3 MB across ~20
 * chunks — large in absolute terms but tiny relative to the saved
 * initial-bundle size, and downloaded *idle*. The browser parallelises
 * the requests and HTTP/2 multiplexing keeps the wire usage modest.
 */
export function prefetchAllRoutes(): void {
  const sweep = () => {
    // Editor first — it's the second-most-likely first navigation
    // after Dashboard for power users authoring pipelines.
    prefetchRoute('editor');
    // Then every other route in declaration order.
    (Object.keys(ROUTE_LOADERS) as Page[]).forEach(prefetchRoute);
  };

  const win = window as typeof window & {
    requestIdleCallback?: (cb: () => void) => number;
  };

  if (typeof win.requestIdleCallback === 'function') {
    win.requestIdleCallback(sweep);
  } else {
    // Safari + a few WebView shells don't ship requestIdleCallback.
    // 2 s is a deliberately late window: long enough for the
    // dashboard's first useEffect chain (API fetches, WebSocket
    // connect) to settle, short enough that a fast user who switches
    // tabs in ~3 s still benefits.
    setTimeout(sweep, 2000);
  }
}
