/**
 * Shared type definitions used across components.
 *
 * 2026-05-19 (OSS-9 of PAGE_BY_PAGE_AUDIT.md): single source of truth
 * for the `Page` union. Three separately-defined copies previously lived
 * in `App.tsx`, `Sidebar.tsx`, and `GlobalSearch.tsx`, bridged with
 * `as any` casts. Routing changes that added a new page had to be
 * synchronized across three files manually. Now they all import `Page`
 * from here, the cast is gone, and a missing entry is a type error at
 * the import site.
 *
 * The hash-routing constants (`VALID_PAGES`, the default landing page,
 * the hashchange listener) live in `frontend/src/router.ts`.
 *
 * 2026-05-23 (P0 Day 2 — route contract cleanup): removed three valid-
 * but-unrendered orphan pages flagged in the full product validation:
 *   - `admin` / `approvals` — Plus-only governance surfaces. The
 *     bouncer effects in App.tsx redirected to dashboard; no actual
 *     render branch ever existed in OSS. The Plus repo can add them
 *     back when it lands. RBAC helpers (`canAccessAdminInEnv`,
 *     `hasPermission`) stay — they're still used by the connection
 *     prod-permission gate and would be needed by Plus on add-back.
 *   - `variables` — was meant to be a workspace-variables manager,
 *     never materialised. The Variables button on the Editor toolbar
 *     opens an inline modal instead.
 * Deep-linking to these routes from the URL hash now falls through to
 * DEFAULT_PAGE (dashboard), the same behaviour as any other unknown id.
 *
 * Embedded-only pages (reports, trust, cert-matrix, activity, lineage,
 * extraction) remain in the union — they ARE rendered in App.tsx as
 * deep-linkable subroutes of Insights, but are intentionally absent
 * from the main sidebar nav.
 */

export type Page =
  | 'dashboard'
  | 'projects'
  | 'pipelines'
  | 'templates'
  | 'editor'
  | 'executions'
  | 'pool'
  | 'storage'
  | 'lineage'
  | 'extraction'
  | 'connections'
  | 'credentials'
  | 'settings'
  | 'account'
  | 'notifications'
  | 'help'
  | 'ai'
  | 'reports'
  | 'trust'
  | 'cert-matrix'
  | 'activity'
  | 'author'
  // 2026-05-29: 'gallery' is the community-built connectors directory,
  // a sibling deep-link of 'author' under the Insights hub. Listed here
  // for the navigateTo type-safety; rendering happens in AIPage as a
  // sub-tab.
  | 'gallery'
  | 'insights';

/** The full allow-list, in nav-strip display order.
 *  'insights' is an alias of 'ai' (sidebar label is "Insights" but the
 *  page id stayed 'ai' for back-compat with older deep links). Router
 *  normalizes 'insights' → 'ai' before rendering. */
export const VALID_PAGES: Page[] = [
  'dashboard',
  'projects',
  'pipelines',
  'templates',
  'editor',
  'executions',
  'pool',
  'storage',
  'lineage',
  'extraction',
  'connections',
  'credentials',
  'settings',
  'account',
  'notifications',
  'help',
  'ai',
  'reports',
  'trust',
  'cert-matrix',
  'activity',
  'author',
  'insights',
  // 2026-06-02 bug-fix: 'gallery' was declared in the Page type union
  // but missing from this runtime VALID_PAGES list AND from App.tsx's
  // route map. Result: `#gallery` URLs (used by the Insights → Gallery
  // tab and the home-page CTAs) fell back to DEFAULT_PAGE on refresh.
  // Adding here + wiring in App.tsx restores the deep link.
  'gallery',
];

/** Default landing page when the URL hash is empty or invalid.
 *  2026-05-25 — flipped from 'projects' to 'dashboard'. The Dashboard
 *  was redesigned as the operator's home page (answers "is anything
 *  broken / what's running / what do I have"); Projects is where you
 *  go to DO something, not where you arrive to SEE state. */
export const DEFAULT_PAGE: Page = 'dashboard';
