import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api/client';
import { renderMarkdown } from '../../utils/markdown';

// DocsReference — Help page "Reference" tab.
//
// Lists the minimum enterprise documentation set (served by
// /api/reports/docs/catalog) and renders the selected doc
// inline without leaving the app. Uses the shared markdown
// renderer (../../utils/markdown) so we don't add a new npm
// dependency and stay in sync with the Reports viewer.

interface DocEntry {
  path: string;
  title: string;
  audience: string;
  summary: string;
}

// Frontend-only categorization. The backend catalog is flat; we group by
// path/filename pattern so the sidebar collapses from ~25 entries to ~9
// categories. Order here is the render order. Any path that doesn't
// match falls into "Other" — keeps the page resilient to new docs.
const CATEGORY_ORDER = [
  'Getting Started',
  'Core Concepts',
  'User Guides',
  'Connectors',
  'Extending F-Pulse',
  'AI',
  'Operations',
  'Developer',
  'Trust & Compliance',
  'Admin & Plus',
  'Other',
] as const;
type Category = (typeof CATEGORY_ORDER)[number];

function categorize(path: string): Category {
  // 2026-06-02: customer-faq.md (buyer-facing) joins the operator faq.md
  // here. The two have distinct audiences but both belong at the "first
  // thing a new user reads" level.
  if (
    path === 'readme.md' ||
    path === 'quickstart.md' ||
    path === 'faq.md' ||
    path === 'customer-faq.md'
  ) {
    return 'Getting Started';
  }
  // 2026-05-29: vs-talend.md is positioning / evaluation content — lives
  // alongside Core Concepts so evaluators land on it next to
  // architecture / editions, not buried in Connectors or Other.
  if (path === 'architecture.md' || path === 'editions.md' || path === 'vs-talend.md') {
    return 'Core Concepts';
  }
  // 2026-06-05 — Steward docs (overview.md = user-facing,
  // architecture.md = builder/evaluator-facing) sit under Core
  // Concepts so they're discoverable next to the architecture +
  // editions docs. The Steward is the OSS headline differentiator
  // (read-only reliability + learning layer); burying it under
  // "Other" would defeat the positioning bet.
  if (path.startsWith('steward/')) {
    return 'Core Concepts';
  }
  if (path.startsWith('user-guides/') && !path.endsWith('users-and-access.md')) {
    return 'User Guides';
  }
  if (path === 'dashboard-metrics.md' || path === 'storage.md') {
    return 'User Guides';
  }
  if (path === 'connectors.md' || path === 'connector-authoring.md') {
    return 'Connectors';
  }
  // 2026-05-29: extensibility doc set lives in its own category so it
  // shows up as a first-class story in Help — the message "you can
  // build your own connector / node" needs to be visible at the same
  // level as the first-party catalog, not hidden under it.
  if (path.startsWith('extend/')) {
    return 'Extending F-Pulse';
  }
  if (
    path === 'ai.md' ||
    path === 'eval-harness.md' ||
    path === 'ai-boundary-contract.md' ||
    path === 'ai-ops-contract.md' ||
    path === 'supported-models.md'
  ) {
    return 'AI';
  }
  // 2026-06-02: Operations widened to include install/*, run-as-service,
  // releases/*, roadmap/*. All operator-facing material: how to install,
  // how to run, what shipped, what's coming.
  if (
    path === 'scaling.md' ||
    path === 'deployment.md' ||
    path === 'performance.md' ||
    path === 'changelog.md' ||
    path === 'run-as-service.md' ||
    path.startsWith('install/') ||
    path.startsWith('releases/') ||
    path.startsWith('roadmap/')
  ) {
    return 'Operations';
  }
  if (
    path === 'api.md' ||
    path === 'dev-guide.md' ||
    path === 'testing.md'
  ) {
    return 'Developer';
  }
  if (path === 'trust.md' || path === 'compliance.md' || path === 'security-deployment.md') {
    return 'Trust & Compliance';
  }
  if (path === 'user-guides/users-and-access.md' || path.startsWith('admin/')) {
    return 'Admin & Plus';
  }
  return 'Other';
}

export default function DocsReference() {
  const [catalog, setCatalog] = useState<DocEntry[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState<string>('');
  const [loadingCatalog, setLoadingCatalog] = useState(true);
  const [loadingContent, setLoadingContent] = useState(false);
  const [error, setError] = useState<string>('');
  const [search, setSearch] = useState<string>('');
  // Accordion behavior — only ONE category is open at a time. The
  // category containing the currently-selected doc is the implicit
  // default; clicking another category swaps the open one (it does not
  // stack). While searching, every category with a match is shown
  // expanded (search overrides this).
  const [openCategory, setOpenCategory] = useState<Category | null>(null);
  // Set when a cross-doc click jumps to "doc.md#section" — the new doc's
  // headers won't be in the DOM at click time, so we stash the anchor id
  // and scroll once the content effect fires below.
  const pendingAnchorRef = useRef<string | null>(null);

  // Load catalog on mount. May 3 2026: support deep-link via the
  // `fpulse_docs_jump_to` sessionStorage breadcrumb. Other pages set
  // this before navigating to #help so the requested doc is pre-selected
  // (e.g. Settings → "scaling guide" link).
  useEffect(() => {
    (async () => {
      try {
        const data = await api.get<{ docs: DocEntry[] }>(
          '/api/reports/docs/catalog',
        );
        setCatalog(data.docs);

        // Resolve breadcrumb if present + the doc is in the catalog.
        let target: string | null = null;
        try {
          const bc = sessionStorage.getItem('fpulse_docs_jump_to');
          if (bc) {
            sessionStorage.removeItem('fpulse_docs_jump_to');
            if (data.docs.some((d) => d.path === bc)) target = bc;
          }
        } catch {
          // sessionStorage disabled — fall through to first-doc default
        }

        if (target) {
          setSelected(target);
        } else if (data.docs.length > 0 && !selected) {
          setSelected(data.docs[0].path);
        }
      } catch (err: any) {
        setError(err.message || 'Failed to load documentation catalog');
      } finally {
        setLoadingCatalog(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load content when selection changes — uses raw fetch so we get the
  // markdown text, not parsed JSON.
  useEffect(() => {
    if (!selected) return;
    setLoadingContent(true);
    setError('');
    const token = localStorage.getItem('fpulse_token');
    const workspaceId = localStorage.getItem('fpulse_workspace_id') || 'default';
    fetch(
      `/api/reports/docs/content?path=${encodeURIComponent(selected)}`,
      {
        headers: {
          Authorization: `Bearer ${token}`,
          'X-Workspace-Id': workspaceId,
        },
      },
    )
      .then(async (res) => {
        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: res.statusText }));
          throw new Error(err.detail || res.statusText);
        }
        return res.text();
      })
      .then((text) => setContent(text))
      .catch((err) => setError(err.message || 'Failed to load document'))
      .finally(() => setLoadingContent(false));
  }, [selected]);

  const rendered = useMemo(() => renderMarkdown(content), [content]);

  // Group docs into categories in the canonical CATEGORY_ORDER. Empty
  // categories are dropped so the sidebar only shows what's actually
  // installed (e.g. "Admin & Plus" disappears entirely on OSS Free
  // since the backend already filters those entries out).
  const grouped = useMemo(() => {
    const buckets = new Map<Category, DocEntry[]>();
    for (const doc of catalog) {
      const cat = categorize(doc.path);
      const list = buckets.get(cat) ?? [];
      list.push(doc);
      buckets.set(cat, list);
    }
    return CATEGORY_ORDER
      .map((cat) => ({ cat, docs: buckets.get(cat) ?? [] }))
      .filter((g) => g.docs.length > 0);
  }, [catalog]);

  // Filter applies to the visible doc list and is case-insensitive across
  // title + summary. A category is shown as long as it has at least one
  // matching doc; otherwise it disappears from the list while searching.
  const filteredGroups = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return grouped;
    return grouped
      .map((g) => ({
        cat: g.cat,
        docs: g.docs.filter((d) =>
          (d.title + ' ' + d.summary).toLowerCase().includes(q),
        ),
      }))
      .filter((g) => g.docs.length > 0);
  }, [grouped, search]);

  const selectedCategory = useMemo<Category | null>(() => {
    if (!selected) return null;
    const doc = catalog.find((d) => d.path === selected);
    return doc ? categorize(doc.path) : null;
  }, [catalog, selected]);

  const isSearching = search.trim().length > 0;
  // Effective open category — explicit user pick wins; otherwise fall
  // back to whichever category contains the selected doc (so deep-links
  // and first-load reveal the active doc without a manual click).
  const effectiveOpen = openCategory ?? selectedCategory;
  const isCategoryOpen = (cat: Category): boolean => {
    if (isSearching) return true;
    return cat === effectiveOpen;
  };
  const toggleCategory = (cat: Category) => {
    // Clicking the already-open category collapses everything;
    // clicking a different category swaps the open one (only ONE is
    // open at a time — no stacking).
    setOpenCategory((prev) => {
      const current = prev ?? selectedCategory;
      return current === cat ? null : cat;
    });
  };

  // After content renders, honor any pending cross-doc anchor. Two-frame
  // requestAnimationFrame so layout has settled before scrollIntoView —
  // a single rAF occasionally fires before the new headers are laid out.
  useEffect(() => {
    if (!content || !pendingAnchorRef.current) return;
    const id = pendingAnchorRef.current;
    pendingAnchorRef.current = null;
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const target = document.getElementById(id);
        if (target) target.scrollIntoView({ behavior: 'auto', block: 'start' });
      });
    });
  }, [content]);

  // Intercept clicks on relative `.md` and `#anchor` links so cross-doc
  // references and TOC entries resolve inside the viewer instead of
  // either 404'ing the SPA route or fighting the app's hash router.
  // Resolution rules:
  //   1. External http(s) links — let the browser handle them.
  //   2. `#section` links — preventDefault + scrollIntoView. We do NOT
  //      let the browser set window.location.hash because the app uses
  //      the hash for top-level page routing (#editor, #help, etc.) and
  //      a TOC click would otherwise look like a navigation attempt.
  //   3. `*.md` (with optional anchor) — resolve relative to the
  //      currently-selected doc, look up in the catalog, swap selection
  //      on hit (preserving the anchor for post-load scroll), or surface
  //      a banner when the target isn't part of the in-app set.
  const handleArticleClick = useCallback(
    (e: React.MouseEvent<HTMLElement>) => {
      const anchor = (e.target as HTMLElement).closest('a');
      if (!anchor) return;
      const href = anchor.getAttribute('href') || '';
      if (!href) return;
      if (/^https?:\/\//i.test(href)) return;

      // Pure in-page anchor — scroll without touching the URL.
      if (href.startsWith('#')) {
        e.preventDefault();
        const id = href.slice(1);
        if (!id) return;
        const target = document.getElementById(id);
        if (target) {
          target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
        return;
      }

      if (!/\.md(?:#.*)?$/i.test(href)) return;

      e.preventDefault();
      if (!selected) return;

      const resolved = resolveDocHref(selected, href);
      const [targetPath, anchorId] = resolved.split('#', 2);
      if (catalog.some((d) => d.path === targetPath)) {
        setSelected(targetPath);
        setError('');
        // Defer scroll until the new doc has rendered. Without the
        // anchor, scroll back to the top.
        if (anchorId) {
          // Stash for the post-render effect — the new content's headers
          // won't exist in the DOM yet at this synchronous point.
          pendingAnchorRef.current = anchorId;
        } else {
          window.scrollTo({ top: 0, behavior: 'auto' });
        }
      } else {
        setError(
          `"${targetPath}" is referenced from this guide but isn't part of the in-app documentation catalog. Open the docs/ folder in the repository to read it.`,
        );
      }
    },
    [catalog, selected],
  );

  return (
    <div className="flex gap-6 min-h-[500px]">
      {/* Sidebar list */}
      <aside className="w-80 shrink-0">
        <div className="rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="border-b border-slate-100 px-4 py-3.5">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-slate-600">
              Documentation
            </h3>
            <p className="mt-1 text-xs text-slate-500">
              Reference for everyday tasks — install, run, connect, schedule.
            </p>
          </div>
          {loadingCatalog ? (
            <div className="px-4 py-6 text-center text-sm text-slate-400">
              Loading catalog…
            </div>
          ) : (
            <>
              <div className="border-b border-slate-100 px-3 py-2.5">
                <div className="relative">
                  <svg
                    className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400"
                    width="16"
                    height="16"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <circle cx="11" cy="11" r="8" />
                    <path d="m21 21-4.3-4.3" />
                  </svg>
                  <input
                    type="search"
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder="Search documentation…"
                    className="w-full rounded-md border border-slate-200 bg-white py-2 pl-8 pr-2 text-sm text-slate-700 placeholder:text-slate-400 focus:border-violet-400 focus:outline-none focus:ring-1 focus:ring-violet-200"
                  />
                </div>
              </div>
              {filteredGroups.length === 0 ? (
                <div className="px-4 py-6 text-center text-sm text-slate-400">
                  {isSearching ? `No documents match "${search}".` : 'No documents available.'}
                </div>
              ) : (
                <div className="divide-y divide-slate-100">
                  {filteredGroups.map(({ cat, docs }) => {
                    const open = isCategoryOpen(cat);
                    return (
                      <div key={cat}>
                        <button
                          type="button"
                          onClick={() => toggleCategory(cat)}
                          className="flex w-full items-center justify-between gap-2 px-4 py-2.5 text-left text-xs font-semibold uppercase tracking-wider text-slate-600 transition hover:bg-slate-50"
                          aria-expanded={open}
                        >
                          <span className="flex items-center gap-2">
                            <svg
                              className={`text-slate-400 transition-transform ${open ? 'rotate-90' : ''}`}
                              width="12"
                              height="12"
                              viewBox="0 0 24 24"
                              fill="none"
                              stroke="currentColor"
                              strokeWidth="2.5"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                            >
                              <polyline points="9 18 15 12 9 6" />
                            </svg>
                            {cat}
                          </span>
                          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-500">
                            {docs.length}
                          </span>
                        </button>
                        {open && (
                          <ul className="divide-y divide-slate-100 border-t border-slate-100">
                            {docs.map((doc) => (
                              <li key={doc.path}>
                                <button
                                  type="button"
                                  onClick={() => setSelected(doc.path)}
                                  className={`block w-full px-4 py-3.5 text-left transition ${
                                    selected === doc.path
                                      ? 'bg-violet-50 border-l-4 border-l-violet-500 pl-3'
                                      : 'hover:bg-slate-50 border-l-4 border-l-transparent pl-3'
                                  }`}
                                >
                                  <div
                                    className={`text-base font-semibold ${
                                      selected === doc.path
                                        ? 'text-violet-900'
                                        : 'text-slate-900'
                                    }`}
                                  >
                                    {doc.title}
                                  </div>
                                  <div className="mt-1 text-xs font-medium uppercase tracking-wide text-slate-500">
                                    {doc.audience}
                                  </div>
                                  <div className="mt-1.5 text-sm leading-relaxed text-slate-600">
                                    {doc.summary}
                                  </div>
                                </button>
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </>
          )}
        </div>
      </aside>

      {/* Content viewer */}
      <main className="min-w-0 flex-1">
        <div className="rounded-xl border border-slate-200 bg-white p-8 shadow-sm">
          {error && (
            <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
              {error}
            </div>
          )}
          {loadingContent ? (
            <div className="py-10 text-center text-sm text-slate-400">
              Loading document…
            </div>
          ) : (
            <article
              className="docs-article prose-custom"
              onClick={handleArticleClick}
            >
              {rendered}
            </article>
          )}
        </div>
      </main>
    </div>
  );
}

// Resolve a relative `.md` href against the path of the doc currently in
// the viewer. Returns the catalog-form path (e.g. "user-guides/foo.md").
//   resolveDocHref("ai.md", "trust.md")                  → "trust.md"
//   resolveDocHref("user-guides/connections.md", "../faq.md")
//                                                        → "faq.md"
//   resolveDocHref("user-guides/pipelines.md", "../nodes.md#joins")
//                                                        → "nodes.md#joins"
//   resolveDocHref("ai.md", "../security.md")            → "security.md"
//      (escapes docs/ — caller treats as out-of-catalog)
function resolveDocHref(currentPath: string, href: string): string {
  const hashIdx = href.indexOf('#');
  const anchor = hashIdx >= 0 ? href.slice(hashIdx) : '';
  let cleanHref = hashIdx >= 0 ? href.slice(0, hashIdx) : href;

  // 2026-06-01: Authors often write `[text](docs/foo.md)` because that's
  // the right path when viewing the markdown on GitHub. Inside the in-app
  // Documentation tab the catalog is rooted at `docs/`, so the leading
  // `docs/` is redundant and would push the resolved path outside the
  // catalog. Strip it so both `docs/foo.md` and `foo.md` resolve to the
  // same catalog entry.
  if (cleanHref.startsWith('docs/')) {
    cleanHref = cleanHref.slice('docs/'.length);
    // After stripping, the href is now absolute from the docs root, not
    // relative to currentPath. Don't walk currentDir for this case.
    const out: string[] = [];
    for (const part of cleanHref.split('/')) {
      if (part === '' || part === '.') continue;
      if (part === '..') { out.pop(); continue; }
      out.push(part);
    }
    return out.join('/') + anchor;
  }

  const currentDir = currentPath.includes('/')
    ? currentPath.split('/').slice(0, -1)
    : [];
  const out: string[] = [...currentDir];
  for (const part of cleanHref.split('/')) {
    if (part === '' || part === '.') continue;
    if (part === '..') {
      out.pop();
      continue;
    }
    out.push(part);
  }
  return out.join('/') + anchor;
}
