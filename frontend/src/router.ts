/**
 * Tiny hash-based router for the F-Pulse shell.
 *
 * 2026-05-19 (OSS-9 of PAGE_BY_PAGE_AUDIT.md): extracted from App.tsx so
 * the navigate machinery is a single import instead of inline glue. 71
 * sites in 22 files set `window.location.hash = X` today; over time
 * each should migrate to `navigateTo(X)` for a single audit-able
 * navigation surface. Subroute parsing (`#extraction/<id>`,
 * `#help/shortcuts`) belongs here too.
 *
 * The full move-everything-to-this-module sweep is mechanical follow-up.
 * What ships today: the helpers exist, App.tsx + Sidebar.tsx +
 * GlobalSearch.tsx import the canonical `Page` union from `./types`,
 * and the `as any` casts are gone.
 */

import { VALID_PAGES, DEFAULT_PAGE, type Page } from './types';

/**
 * Strip the leading `#` and split the hash into (pagePart, subRoute, queryString).
 *
 * 2026-05-22 (audit O1) — the previous parsers used `raw.split('/')[0]`
 * which made `#executions?status=failed` return the WHOLE string as the
 * "first segment", so the page guard rejected it as invalid and the
 * filter-aware chips from the Dashboard never routed. The fix: split
 * page identity on BOTH `/` and `?` so the page id is just `executions`
 * and the rest is treated as subroute / query.
 *
 * Examples:
 *   "executions"                          → page=executions, sub=null,   qs=""
 *   "executions/12345"                    → page=executions, sub=12345,  qs=""
 *   "executions?status=failed&hours=24"   → page=executions, sub=null,   qs="status=failed&hours=24"
 *   "extraction/abc?tab=logs"             → page=extraction, sub=abc,    qs="tab=logs"
 */
function _parseHash(raw: string): { page: string; subRoute: string | null; queryString: string } {
  const hash = (raw || '').replace('#', '') || DEFAULT_PAGE;
  // 1. Split query off the right side first so a `?` inside a subroute
  //    doesn't confuse the / split.
  const qIdx = hash.indexOf('?');
  const path = qIdx >= 0 ? hash.slice(0, qIdx) : hash;
  const queryString = qIdx >= 0 ? hash.slice(qIdx + 1) : '';
  // 2. Now split the path on '/'.
  const parts = path.split('/');
  const page = parts[0];
  const subRoute = parts.length > 1 ? parts.slice(1).join('/') : null;
  return { page, subRoute, queryString };
}

/** Read the current page from the URL hash. */
export function readCurrentPage(): Page {
  const raw = typeof window !== 'undefined' ? window.location.hash : '';
  const { page } = _parseHash(raw);
  return (VALID_PAGES as readonly string[]).includes(page) ? (page as Page) : DEFAULT_PAGE;
}

/** Read a sub-route (e.g. `/<id>` after the page name). */
export function readSubRoute(): string | null {
  const raw = typeof window !== 'undefined' ? window.location.hash : '';
  return _parseHash(raw).subRoute;
}

/**
 * Read the URL hash's query string as a parsed map.
 *
 * 2026-05-22 (audit O1) — supports the filter-aware navigation pattern
 * (e.g. `#executions?status=failed&hours=24`). Destination pages call
 * this on mount to pick up filters set by the navigator.
 *
 * Returns an empty object when there's no query string. Repeated keys
 * use the last value (URLSearchParams default).
 */
export function readHashQuery(): Record<string, string> {
  const raw = typeof window !== 'undefined' ? window.location.hash : '';
  const qs = _parseHash(raw).queryString;
  if (!qs) return {};
  const out: Record<string, string> = {};
  try {
    const params = new URLSearchParams(qs);
    params.forEach((value, key) => {
      out[key] = value;
    });
  } catch {
    // Malformed query — return what we got, never throw.
  }
  return out;
}

/** Navigate to a top-level page (no subroute). Idempotent. */
export function navigateTo(page: Page): void {
  if (typeof window === 'undefined') return;
  if (window.location.hash === `#${page}`) return;
  window.location.hash = page;
}

/** Navigate to a sub-route under a page (e.g. `extraction/<run_id>`). */
export function navigateToSubRoute(page: Page, subRoute: string): void {
  if (typeof window === 'undefined') return;
  const target = `#${page}/${subRoute}`;
  if (window.location.hash === target) return;
  window.location.hash = `${page}/${subRoute}`;
}

/**
 * Navigate to a page with a query-string filter.
 *
 * Used by the Dashboard's filter-aware drill-downs (audit K3 + O1).
 * Destination page can read the filter via `readHashQuery()`.
 */
export function navigateWithQuery(page: Page, query: Record<string, string | number | undefined>): void {
  if (typeof window === 'undefined') return;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue;
    params.set(k, String(v));
  }
  const qs = params.toString();
  const target = qs ? `#${page}?${qs}` : `#${page}`;
  if (window.location.hash === target) return;
  window.location.hash = qs ? `${page}?${qs}` : page;
}

/** Type guard for runtime input. */
export function isValidPage(s: string): s is Page {
  return (VALID_PAGES as readonly string[]).includes(s);
}
