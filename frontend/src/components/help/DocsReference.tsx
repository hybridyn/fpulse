import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api/client';

// DocsReference — Help page "Reference" tab.
//
// Lists the minimum enterprise documentation set (served by
// /api/reports/docs/catalog) and renders the selected doc
// inline without leaving the app. Uses a small self-contained
// markdown renderer so we don't add a new npm dependency.

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


// ═══════════════════════════════════════════════════════════════════════
// Inline markdown renderer
//
// Handles the shapes used by our docs:
//   # / ## / ### / #### headers
//   Bullet lists (- ) and numbered lists (1. )
//   Tables (GFM pipe tables with separator row)
//   **bold**, *italic*, `code`
//   ```fenced code blocks```
//   > blockquotes
//   Links [text](url)
//   Horizontal rules (---)
//
// Not a full CommonMark implementation — just enough for our docs.
// ═══════════════════════════════════════════════════════════════════════

function renderMarkdown(src: string): React.ReactNode {
  if (!src) return null;

  // Split by fenced code blocks first — easier than regex with escapes.
  const blocks: Array<{ kind: 'text' | 'code'; body: string; lang?: string }> = [];
  const lines = src.split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = /^```(\w+)?/.exec(line);
    if (fence) {
      // Start of code block — find matching close.
      const lang = fence[1] || '';
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) {
        codeLines.push(lines[i]);
        i++;
      }
      i++; // skip the closing ```
      blocks.push({ kind: 'code', body: codeLines.join('\n'), lang });
    } else {
      const textLines: string[] = [];
      while (
        i < lines.length &&
        !/^```(\w+)?/.test(lines[i])
      ) {
        textLines.push(lines[i]);
        i++;
      }
      blocks.push({ kind: 'text', body: textLines.join('\n') });
    }
  }

  return blocks.map((b, idx) =>
    b.kind === 'code' ? (
      <pre
        key={idx}
        className="my-4 overflow-x-auto rounded-lg bg-slate-900 p-4 text-xs leading-relaxed text-slate-100"
      >
        <code>{b.body}</code>
      </pre>
    ) : (
      <TextBlock key={idx} src={b.body} />
    ),
  );
}

// Renders a text block (non-code). Walks the lines and emits
// paragraphs, headers, lists, tables, blockquotes, horizontal rules.
function TextBlock({ src }: { src: string }): React.ReactNode {
  const lines = src.split('\n');
  const out: React.ReactNode[] = [];
  let i = 0;
  let key = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Blank line → skip
    if (!line.trim()) {
      i++;
      continue;
    }

    // Horizontal rule
    if (/^---+$/.test(line.trim())) {
      out.push(<hr key={key++} className="my-6 border-slate-200" />);
      i++;
      continue;
    }

    // Headers
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const level = h[1].length;
      const text = h[2].trim();
      out.push(renderHeader(level, text, key++));
      i++;
      continue;
    }

    // Table — a line starting with `|` followed by a separator row of `|---|`
    if (line.trim().startsWith('|') && i + 1 < lines.length &&
        /^\s*\|[\s\-\|:]+\|\s*$/.test(lines[i + 1])) {
      const tableLines: string[] = [line];
      i++; // header separator
      const sep = lines[i];
      tableLines.push(sep);
      i++;
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        tableLines.push(lines[i]);
        i++;
      }
      out.push(renderTable(tableLines, key++));
      continue;
    }

    // Blockquote
    if (line.startsWith('>')) {
      const quoteLines: string[] = [];
      while (i < lines.length && lines[i].startsWith('>')) {
        quoteLines.push(lines[i].replace(/^>\s?/, ''));
        i++;
      }
      out.push(
        <blockquote
          key={key++}
          className="my-4 border-l-4 border-violet-300 bg-violet-50/50 px-4 py-2 text-sm italic text-slate-700"
        >
          {renderInline(quoteLines.join(' '))}
        </blockquote>,
      );
      continue;
    }

    // Bullet list
    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ''));
        i++;
      }
      out.push(
        <ul key={key++} className="my-3 list-disc space-y-1.5 pl-6 text-base leading-relaxed text-slate-700">
          {items.map((it, k) => (
            <li key={k}>{renderInline(it)}</li>
          ))}
        </ul>,
      );
      continue;
    }

    // Numbered list. Blank lines BETWEEN numbered items must keep them in
    // the SAME <ol> (markdown "loose list") — splitting there gives every
    // item its own single-item <ol> whose CSS counter restarts, so a
    // 1./2./3. list renders as 1., 1., 1. The author's first number is
    // honored via <ol start> so a list interrupted by prose resumes
    // where it left off instead of snapping back to 1.
    if (/^\s*\d+\.\s+/.test(line)) {
      const start = parseInt(/^\s*(\d+)\.\s+/.exec(line)![1], 10);
      const items: string[] = [];
      while (i < lines.length) {
        if (/^\s*\d+\.\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*\d+\.\s+/, ''));
          i++;
          continue;
        }
        if (!lines[i].trim()) {
          // Bridge blank lines only when another numbered item follows.
          let j = i + 1;
          while (j < lines.length && !lines[j].trim()) j++;
          if (j < lines.length && /^\s*\d+\.\s+/.test(lines[j])) {
            i = j;
            continue;
          }
        }
        break;
      }
      out.push(
        <ol
          key={key++}
          start={start !== 1 ? start : undefined}
          className="my-3 list-decimal space-y-1.5 pl-6 text-base leading-relaxed text-slate-700"
        >
          {items.map((it, k) => (
            <li key={k}>{renderInline(it)}</li>
          ))}
        </ol>,
      );
      continue;
    }

    // Regular paragraph — accumulate until blank line or block marker
    const paragraphLines: string[] = [line];
    i++;
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,6})\s+/.test(lines[i]) &&
      !lines[i].startsWith('>') &&
      !/^\s*[-*]\s+/.test(lines[i]) &&
      !/^\s*\d+\.\s+/.test(lines[i]) &&
      !lines[i].trim().startsWith('|') &&
      !/^---+$/.test(lines[i].trim())
    ) {
      paragraphLines.push(lines[i]);
      i++;
    }
    out.push(
      <p key={key++} className="my-3 text-base leading-relaxed text-slate-700">
        {renderInline(paragraphLines.join(' '))}
      </p>,
    );
  }

  return <>{out}</>;
}

// GitHub-style header slug: lowercased, alphanumerics + dashes, runs of
// non-word chars collapsed to one dash. Matches the format markdown
// authors use in TOC anchor links like `[Section](#1-section-title)`.
function slugifyHeader(text: string): string {
  return text
    .toLowerCase()
    .replace(/[`*_~]/g, '')        // strip basic md inline markers
    .replace(/[^a-z0-9\s-]/g, '')  // drop punctuation
    .trim()
    .replace(/\s+/g, '-')          // spaces → dashes
    .replace(/-+/g, '-');          // collapse runs of dashes
}

function renderHeader(level: number, text: string, k: number): React.ReactNode {
  const inline = renderInline(text);
  const id = slugifyHeader(text);
  // `scroll-mt-16` keeps the heading clear of the sticky page header
  // when the browser jumps to the anchor. Tailwind utility shipped in
  // globals — same offset used elsewhere in the app.
  //
  // 2026-06-01: H2 was previously `text-violet-700` which looked like
  // a hyperlink (purple = visited-link color in browser defaults).
  // Users instinctively tried to click section headers ("Summary",
  // "Side-by-side", etc.) and nothing happened. Switched to
  // `text-slate-900` with a thin bottom border that matches the H1
  // treatment so the visual hierarchy is: H1 (page title) > H2
  // (section) > H3 > H4, all in neutral slate, none mistakable for
  // a link.
  if (level === 1) {
    return (
      <h1
        key={k}
        id={id}
        className="mt-6 mb-3 border-b border-slate-200 pb-2 text-2xl font-bold text-slate-900 scroll-mt-16"
      >
        {inline}
      </h1>
    );
  }
  if (level === 2) {
    return (
      <h2
        key={k}
        id={id}
        className="mt-8 mb-3 border-b border-slate-100 pb-1.5 text-xl font-semibold text-slate-900 scroll-mt-16"
      >
        {inline}
      </h2>
    );
  }
  if (level === 3) {
    return (
      <h3 key={k} id={id} className="mt-5 mb-2 text-base font-semibold text-slate-900 scroll-mt-16">
        {inline}
      </h3>
    );
  }
  return (
    <h4 key={k} id={id} className="mt-3 mb-1 text-sm font-semibold text-slate-700 scroll-mt-16">
      {inline}
    </h4>
  );
}

function renderTable(tableLines: string[], k: number): React.ReactNode {
  // Row 0: header cells, Row 1: separator, Rows 2+: body.
  const parseRow = (line: string) => {
    const trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    return trimmed.split('|').map((c) => c.trim());
  };
  const header = parseRow(tableLines[0]);
  const body = tableLines.slice(2).map(parseRow);

  return (
    <div key={k} className="my-4 overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-slate-300 bg-slate-50">
            {header.map((cell, i) => (
              <th
                key={i}
                className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-slate-600"
              >
                {renderInline(cell)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, ri) => (
            <tr key={ri} className="border-b border-slate-100 last:border-0">
              {row.map((cell, ci) => (
                <td key={ci} className="px-3 py-2 align-top text-slate-700">
                  {renderInline(cell)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Inline formatting: **bold**, *italic*, `code`, [text](url).
// Returns React nodes so anchors and code spans render correctly.
function renderInline(text: string): React.ReactNode {
  // Split on the inline markers; build tokens.
  // Order matters: code first (backticks) so ** inside ` ` is not parsed.
  const tokens: Array<{ kind: string; text: string; href?: string }> = [];
  let rest = text;

  while (rest.length > 0) {
    // Code span
    const codeMatch = /^([\s\S]*?)`([^`]+)`/.exec(rest);
    const boldMatch = /^([\s\S]*?)\*\*([^*]+)\*\*/.exec(rest);
    const italicMatch = /^([\s\S]*?)(?<![*\w])\*([^*]+)\*/.exec(rest);
    const linkMatch = /^([\s\S]*?)\[([^\]]+)\]\(([^)]+)\)/.exec(rest);

    // Find the earliest match
    const candidates = [
      codeMatch && { kind: 'code', m: codeMatch },
      boldMatch && { kind: 'bold', m: boldMatch },
      italicMatch && { kind: 'italic', m: italicMatch },
      linkMatch && { kind: 'link', m: linkMatch },
    ].filter(Boolean) as Array<{ kind: string; m: RegExpExecArray }>;

    if (candidates.length === 0) {
      tokens.push({ kind: 'text', text: rest });
      break;
    }
    candidates.sort((a, b) => a.m[1].length - b.m[1].length);
    const first = candidates[0];
    if (first.m[1]) tokens.push({ kind: 'text', text: first.m[1] });

    if (first.kind === 'code') {
      tokens.push({ kind: 'code', text: first.m[2] });
    } else if (first.kind === 'bold') {
      tokens.push({ kind: 'bold', text: first.m[2] });
    } else if (first.kind === 'italic') {
      tokens.push({ kind: 'italic', text: first.m[2] });
    } else if (first.kind === 'link') {
      tokens.push({ kind: 'link', text: first.m[2], href: first.m[3] });
    }
    rest = rest.slice(first.m[0].length);
  }

  return tokens.map((t, i) => {
    if (t.kind === 'code') {
      return (
        <code
          key={i}
          className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[12px] text-slate-800"
        >
          {t.text}
        </code>
      );
    }
    if (t.kind === 'bold') {
      return (
        <strong key={i} className="font-semibold text-slate-900">
          {t.text}
        </strong>
      );
    }
    if (t.kind === 'italic') {
      return <em key={i}>{t.text}</em>;
    }
    if (t.kind === 'link') {
      // External links open in new tab. Relative `.md` links keep their
      // raw href here; the article-level click handler in DocsReference
      // intercepts them and routes via the catalog.
      const isExternal = /^https?:\/\//.test(t.href || '');
      return (
        <a
          key={i}
          href={t.href}
          target={isExternal ? '_blank' : undefined}
          rel={isExternal ? 'noopener noreferrer' : undefined}
          className="text-violet-600 underline hover:text-violet-700"
        >
          {t.text}
        </a>
      );
    }
    return <span key={i}>{t.text}</span>;
  });
}
