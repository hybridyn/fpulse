import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { api } from '../api/client';

type CatalogItem = { name: string; kind: string; parent: string; metadata: Record<string, any> };
type CatalogResponse = {
  supported: boolean;
  reason: string;
  items: CatalogItem[];
  parents: string[];
  kinds: string[];
  category?: string;
  auth?: string;
  tier?: string;
  verification?: string;
  capabilities?: Record<string, boolean>;
};

interface Props {
  connectionId: string | null | undefined;
  /** Restrict to certain kinds (e.g. ['table','view']). Empty = all. */
  kinds?: string[];
  /** Called when user picks an item. */
  onPick: (item: CatalogItem) => void;
  /** Optional label override. */
  label?: string;
  /** Tighter inline button vs full-width row. */
  compact?: boolean;
}

const KIND_ICONS: Record<string, string> = {
  table: '▤', view: '◫', procedure: 'ƒ', synonym: '↔', function: 'ƒ',
  collection: '◰', topic: '⌥', index: '◇', bucket: '◇', object: '·',
  prefix: '▸', endpoint: '⇌', key: '◯',
};

const KIND_COLOR: Record<string, string> = {
  table: 'text-blue-600 bg-blue-50 border-blue-200',
  view: 'text-indigo-600 bg-indigo-50 border-indigo-200',
  procedure: 'text-purple-600 bg-purple-50 border-purple-200',
  function: 'text-purple-600 bg-purple-50 border-purple-200',
  synonym: 'text-amber-600 bg-amber-50 border-amber-200',
  collection: 'text-emerald-600 bg-emerald-50 border-emerald-200',
  topic: 'text-orange-600 bg-orange-50 border-orange-200',
  index: 'text-cyan-600 bg-cyan-50 border-cyan-200',
  bucket: 'text-slate-600 bg-slate-50 border-slate-200',
  object: 'text-slate-500 bg-slate-50 border-slate-200',
  prefix: 'text-slate-500 bg-slate-50 border-slate-200',
  endpoint: 'text-pink-600 bg-pink-50 border-pink-200',
  key: 'text-rose-600 bg-rose-50 border-rose-200',
};

// Capped page-size per schema group — production DBs can hold many
// thousand tables in a single schema, and rendering them all blows
// the render budget. Users hit "Show all" if they need the long tail
// (typed search is usually faster).
const GROUP_PAGE_SIZE = 60;

// Soft cap on the "Recent" group. Higher than 5 because power users
// pick from the same handful of tables a lot, but low enough that
// it doesn't dominate the popup.
const RECENT_MAX = 8;

function recentKey(connectionId: string): string {
  return `fpulse_catalog_recent_${connectionId}`;
}
function collapsedKey(connectionId: string): string {
  return `fpulse_catalog_collapsed_${connectionId}`;
}

function readRecent(connectionId: string): CatalogItem[] {
  try {
    const raw = localStorage.getItem(recentKey(connectionId));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.slice(0, RECENT_MAX) : [];
  } catch { return []; }
}
function pushRecent(connectionId: string, item: CatalogItem) {
  try {
    const cur = readRecent(connectionId);
    // De-dupe on (kind,parent,name) — the same table picked twice
    // shouldn't fill the list with duplicates.
    const sig = (i: CatalogItem) => `${i.kind}::${i.parent}::${i.name}`;
    const itemSig = sig(item);
    const next = [item, ...cur.filter((i) => sig(i) !== itemSig)].slice(0, RECENT_MAX);
    localStorage.setItem(recentKey(connectionId), JSON.stringify(next));
  } catch { /* quota errors ignored */ }
}

function readCollapsed(connectionId: string): Set<string> {
  try {
    const raw = localStorage.getItem(collapsedKey(connectionId));
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    return new Set(Array.isArray(parsed) ? parsed : []);
  } catch { return new Set(); }
}
function writeCollapsed(connectionId: string, set: Set<string>) {
  try {
    localStorage.setItem(collapsedKey(connectionId), JSON.stringify(Array.from(set)));
  } catch { /* ignore */ }
}

export default function CatalogPicker({ connectionId, kinds, onPick, label = 'Browse', compact = false }: Props) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [catalog, setCatalog] = useState<CatalogResponse | null>(null);
  const [error, setError] = useState<string>('');
  const [search, setSearch] = useState('');
  const [activeKind, setActiveKind] = useState<string>('');
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [shownPerGroup, setShownPerGroup] = useState<Record<string, number>>({});
  const [recent, setRecent] = useState<CatalogItem[]>([]);
  const popRef = useRef<HTMLDivElement | null>(null);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  // The popover is portaled to <body> (so the ConfigPanel's overflow-y-auto
  // scroll container can't clip its right edge — that clipping shaved the
  // right-aligned group counts and the Recent "schema" label). Its position is
  // computed from the trigger button's rect and tracked on scroll/resize.
  const [pos, setPos] = useState<{ top: number; left: number; width: number; maxHeight: number } | null>(null);

  // Refetch whenever the popup opens or the connection changes.
  useEffect(() => {
    if (!open || !connectionId) return;
    setLoading(true);
    setError('');
    setCollapsedGroups(readCollapsed(connectionId));
    setRecent(readRecent(connectionId));
    setShownPerGroup({});
    api.getConnectionCatalog(connectionId)
      .then((res) => setCatalog(res))
      .catch((e: any) => setError(e?.message || 'Failed to load catalog'))
      .finally(() => setLoading(false));
  }, [open, connectionId]);

  // Reset shown-count when filters change so "Show more" doesn't
  // leave stale offsets behind a filter that hid most items.
  useEffect(() => {
    setShownPerGroup({});
  }, [search, activeKind]);

  // Position the portaled popover under the trigger, right-aligned to it but
  // clamped to the viewport. CONTINUOUS tracking via requestAnimationFrame:
  // a one-shot measure goes STALE the moment the modal re-centers, scrolls,
  // or grows (catalog load / fields appearing), which strands the popover far
  // from its button. The rAF loop re-reads the button rect every frame and
  // only setState()s when the rounded position actually changes (no churn).
  useLayoutEffect(() => {
    if (!open) return;
    let last = '';
    const measure = () => {
      const b = btnRef.current;
      if (!b) return;
      const r = b.getBoundingClientRect();
      const margin = 8;
      // clientWidth/Height exclude the scrollbar — steadier than innerWidth.
      const vw = document.documentElement.clientWidth || window.innerWidth;
      const vh = document.documentElement.clientHeight || window.innerHeight;
      const width = Math.min(600, vw - margin * 2);
      let left = r.right - width;            // right-align to the button
      if (left < margin) left = margin;      // don't run off the left edge
      if (left + width > vw - margin) left = vw - margin - width;
      const top = r.bottom + 6;
      // Clamp height so a tall list never spills past the viewport bottom;
      // the popover body scrolls internally instead.
      const maxHeight = Math.max(180, Math.min(520, vh - top - margin));
      const key = `${Math.round(top)}|${Math.round(left)}|${Math.round(width)}|${Math.round(maxHeight)}`;
      if (key !== last) {
        last = key;
        setPos({ top, left, width, maxHeight });
      }
    };
    // Place synchronously BEFORE paint (not rAF-dependent — rAF is paused in
    // background tabs, which left the popover unplaced). Then track layout
    // shifts (modal re-center, scroll, catalog load growing the modal) via a
    // light interval + instant scroll/resize listeners.
    measure();
    const id = window.setInterval(measure, 100);
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => {
      clearInterval(id);
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
    };
  }, [open]);

  // Click-outside close (button + portaled popover are both excluded).
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node;
      if (popRef.current?.contains(t)) return;
      if (btnRef.current?.contains(t)) return;
      setOpen(false);
    };
    setTimeout(() => document.addEventListener('mousedown', onDoc), 0);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const buttonDisabled = !connectionId;

  // Items the caller's `kinds` prop allows — we'll filter against this
  // before the search / active-tab filters so kind counts are honest.
  const allowedItems = useMemo(() => {
    if (!catalog?.items) return [] as CatalogItem[];
    if (!kinds || kinds.length === 0) return catalog.items;
    return catalog.items.filter((i) => kinds.includes(i.kind));
  }, [catalog, kinds]);

  // Kind counts for the tab strip. Computed before the active-tab
  // filter (so "Tables (12)" stays stable when you click "Views").
  const kindCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const item of allowedItems) {
      counts[item.kind] = (counts[item.kind] || 0) + 1;
    }
    return counts;
  }, [allowedItems]);

  const availableKinds = useMemo(() => {
    const kindList = (catalog?.kinds || []).filter(
      (k) => !kinds || kinds.length === 0 || kinds.includes(k),
    );
    // Order: tables, views, procedures, functions, then alphabetical for
    // the long tail. Production DBs almost always reach for these first.
    const priority = ['table', 'view', 'procedure', 'function'];
    return kindList.sort((a, b) => {
      const ai = priority.indexOf(a);
      const bi = priority.indexOf(b);
      if (ai === -1 && bi === -1) return a.localeCompare(b);
      if (ai === -1) return 1;
      if (bi === -1) return -1;
      return ai - bi;
    });
  }, [catalog, kinds]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return allowedItems.filter((i) => {
      if (activeKind && i.kind !== activeKind) return false;
      if (!q) return true;
      return i.name.toLowerCase().includes(q) || (i.parent || '').toLowerCase().includes(q);
    });
  }, [allowedItems, activeKind, search]);

  // Group by parent (schema). When searching we keep groups so the
  // user can still see which schema each match belongs to.
  const grouped = useMemo(() => {
    const out: Record<string, CatalogItem[]> = {};
    for (const item of filtered) {
      const key = item.parent || '(root)';
      (out[key] ||= []).push(item);
    }
    return out;
  }, [filtered]);

  const groupedEntries = useMemo(() => Object.entries(grouped), [grouped]);

  const handlePick = (item: CatalogItem) => {
    if (connectionId) pushRecent(connectionId, item);
    onPick(item);
    setOpen(false);
  };

  const toggleGroup = (parent: string) => {
    if (!connectionId) return;
    const next = new Set(collapsedGroups);
    if (next.has(parent)) next.delete(parent);
    else next.add(parent);
    setCollapsedGroups(next);
    writeCollapsed(connectionId, next);
  };

  const showMore = (parent: string, totalForGroup: number) => {
    setShownPerGroup((prev) => ({
      ...prev,
      [parent]: Math.min(totalForGroup, (prev[parent] || GROUP_PAGE_SIZE) + GROUP_PAGE_SIZE),
    }));
  };

  const showAll = (parent: string, totalForGroup: number) => {
    setShownPerGroup((prev) => ({ ...prev, [parent]: totalForGroup }));
  };

  // Recent group — filter against `kinds`, then the active-kind tab
  // and the search query, so the user's recents stay relevant even
  // when they switch tabs or type.
  const recentFiltered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return recent.filter((i) => {
      if (kinds && kinds.length > 0 && !kinds.includes(i.kind)) return false;
      if (activeKind && i.kind !== activeKind) return false;
      if (!q) return true;
      return i.name.toLowerCase().includes(q) || (i.parent || '').toLowerCase().includes(q);
    });
  }, [recent, kinds, activeKind, search]);

  return (
    <div className="relative inline-block">
      <button
        ref={btnRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
        disabled={buttonDisabled}
        title={buttonDisabled ? 'Pick a connection first' : 'Browse catalog'}
        className={`flex items-center gap-1.5 text-xs font-semibold rounded-lg border transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
          compact ? 'px-2 py-1' : 'px-2.5 py-1.5'
        } bg-white text-slate-600 border-slate-300 hover:bg-slate-50 hover:text-blue-600 hover:border-blue-300`}
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M3 7h6l2 2h10v9a2 2 0 0 1-2 2H3z" />
        </svg>
        {label}
      </button>

      {open && createPortal(
        <div
          ref={popRef}
          style={{
            position: 'fixed',
            top: pos?.top ?? -9999,
            left: pos?.left ?? -9999,
            width: pos?.width ?? 600,
            maxHeight: pos?.maxHeight ?? 520,
            // Hidden until the first measurement lands, so it never flashes at
            // the stale -9999 origin before rAF places it.
            visibility: pos ? 'visible' : 'hidden',
          }}
          className="z-[1000] bg-white rounded-xl shadow-2xl border border-slate-200 overflow-hidden flex flex-col"
        >
          {/* Header — verification / category / tier badges. */}
          {catalog && (catalog.category || catalog.tier || catalog.verification) && (
            <div className="px-3 py-1.5 border-b border-slate-100 bg-slate-50/50 flex items-center gap-1.5 text-xs flex-wrap shrink-0">
              {catalog.category && (
                <span className="px-1.5 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-200 font-semibold capitalize">
                  {catalog.category.replace(/_/g, ' ')}
                </span>
              )}
              {catalog.verification && catalog.verification !== 'n_a' && (
                <span
                  className={`px-1.5 py-0.5 rounded-full border font-semibold ${
                    catalog.verification === 'integration_verified' || catalog.verification === 'production_ready'
                      ? 'bg-emerald-50 text-emerald-700 border-emerald-200' :
                    catalog.verification === 'sandbox_verified'
                      ? 'bg-blue-50 text-blue-700 border-blue-200' :
                    catalog.verification === 'sdk_validated'
                      ? 'bg-amber-50 text-amber-700 border-amber-200' :
                    'bg-slate-100 text-slate-500 border-slate-200'
                  }`}
                  title={
                    catalog.verification === 'integration_verified' ? 'Verified against a real instance' :
                    catalog.verification === 'production_ready' ? 'Verified, documented, error UX checked' :
                    catalog.verification === 'sandbox_verified' ? 'Verified against an emulator / vendor container' :
                    catalog.verification === 'sdk_validated' ? 'Code follows the canonical API — not yet run live' :
                    catalog.verification
                  }
                >
                  {catalog.verification.replace(/_/g, ' ')}
                </span>
              )}
              {catalog.tier && (
                <span
                  className={`px-1.5 py-0.5 rounded-full border font-semibold uppercase ${
                    catalog.tier === 'tier1' ? 'bg-emerald-50 text-emerald-700 border-emerald-200' :
                    catalog.tier === 'tier2' ? 'bg-amber-50 text-amber-700 border-amber-200' :
                    catalog.tier === 'tier3' ? 'bg-orange-50 text-orange-700 border-orange-200' :
                    'bg-slate-100 text-slate-500 border-slate-200'
                  }`}
                  title={
                    catalog.tier === 'tier1' ? 'Tier 1 — no extra dependencies' :
                    catalog.tier === 'tier2' ? 'Tier 2 — lightweight SDK' :
                    catalog.tier === 'tier3' ? 'Tier 3 — vendor SDK / opt-in install' :
                    'Tier 4 — limited support'
                  }
                >
                  {catalog.tier}
                </span>
              )}
              {catalog.auth && (
                <span className="text-slate-400">· auth: {catalog.auth.replace(/_/g, ' ')}</span>
              )}
              {!loading && allowedItems.length > 0 && (
                <span className="ml-auto text-slate-400">{allowedItems.length.toLocaleString()} total</span>
              )}
            </div>
          )}

          {/* Search — sticky at top. */}
          <div className="px-3 py-2 border-b border-slate-100 shrink-0">
            <input
              autoFocus
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by name or schema…"
              className="w-full text-sm px-2.5 py-1.5 border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-300"
            />
          </div>

          {/* Kind tabs — always visible when more than one kind exists,
              so users see exactly what categories the source exposes
              (tables, views, procedures, …) instead of guessing from a
              flat list. Counts are pre-filter so they don't jitter. */}
          {availableKinds.length > 1 && (
            <div className="px-3 pt-1.5 pb-2 border-b border-slate-100 shrink-0">
              <div className="flex gap-1 flex-wrap">
                <button
                  onClick={() => setActiveKind('')}
                  className={`text-xs px-2 py-1 rounded-full border transition-colors ${
                    activeKind === ''
                      ? 'bg-blue-100 text-blue-700 border-blue-300 font-semibold'
                      : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50'
                  }`}
                >
                  All {allowedItems.length > 0 && <span className="opacity-70">({allowedItems.length})</span>}
                </button>
                {availableKinds.map((k) => (
                  <button
                    key={k}
                    onClick={() => setActiveKind(k === activeKind ? '' : k)}
                    className={`text-xs px-2 py-1 rounded-full border capitalize transition-colors flex items-center gap-1 ${
                      activeKind === k
                        ? 'bg-blue-100 text-blue-700 border-blue-300 font-semibold'
                        : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50'
                    }`}
                  >
                    <span>{KIND_ICONS[k] || '·'}</span>
                    {k}
                    {kindCounts[k] !== undefined && <span className="opacity-70">({kindCounts[k]})</span>}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Body */}
          <div className="flex-1 overflow-auto">
            {loading && (
              <div className="px-4 py-6 text-xs text-slate-400 text-center">Loading catalog…</div>
            )}
            {!loading && error && (
              <div className="px-4 py-3 text-xs text-red-500">{error}</div>
            )}
            {!loading && !error && catalog && !catalog.supported && (
              <div className="px-4 py-3 text-xs text-slate-500 italic">{catalog.reason}</div>
            )}
            {!loading && !error && catalog && catalog.supported && filtered.length === 0 && recentFiltered.length === 0 && (
              <div className="px-4 py-6 text-xs text-slate-400 text-center">
                {search ? 'No matches.' : 'Catalog is empty.'}
              </div>
            )}
            {!loading && !error && catalog && catalog.supported && (filtered.length > 0 || recentFiltered.length > 0) && (
              <div>
                {/* Recent — pinned to the top. Hidden when searching
                    actively unless the search itself matches recents. */}
                {recentFiltered.length > 0 && (
                  <RecentSection items={recentFiltered} onPick={handlePick} />
                )}

                {/* Schema groups */}
                {groupedEntries.map(([parent, items]) => {
                  const isCollapsed = collapsedGroups.has(parent);
                  const total = items.length;
                  const shown = shownPerGroup[parent] || GROUP_PAGE_SIZE;
                  const visible = items.slice(0, Math.min(shown, total));
                  const remaining = total - visible.length;
                  return (
                    <div key={parent}>
                      <button
                        onClick={() => toggleGroup(parent)}
                        className="w-full px-3 py-1.5 text-xs font-bold uppercase tracking-wider text-slate-500 bg-slate-50/80 sticky top-0 flex items-center gap-1.5 hover:bg-slate-100 border-b border-slate-100"
                      >
                        <svg
                          width="10" height="10" viewBox="0 0 24 24" fill="none"
                          stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
                          className={`transition-transform ${isCollapsed ? '' : 'rotate-90'}`}
                        >
                          <polyline points="9 18 15 12 9 6" />
                        </svg>
                        <span className="truncate">{parent}</span>
                        <span className="ml-auto text-slate-400 normal-case tracking-normal font-medium">
                          {total.toLocaleString()}
                        </span>
                      </button>
                      {!isCollapsed && (
                        <>
                          {visible.map((item, i) => (
                            <button
                              key={`${parent}-${item.kind}-${item.name}-${i}`}
                              onClick={() => handlePick(item)}
                              className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-blue-50 group border-b border-slate-50 last:border-b-0"
                            >
                              <span className={`text-xs font-bold uppercase tracking-wide px-1.5 py-0.5 rounded border w-[68px] text-center shrink-0 ${KIND_COLOR[item.kind] || 'text-slate-500 bg-slate-50 border-slate-200'}`}>
                                {KIND_ICONS[item.kind] || '·'} {item.kind}
                              </span>
                              <span className="text-sm text-slate-700 group-hover:text-blue-700 truncate">
                                {item.name}
                              </span>
                            </button>
                          ))}
                          {remaining > 0 && (
                            <div className="px-3 py-1.5 flex items-center gap-2 text-xs bg-slate-50/60 border-b border-slate-100">
                              <span className="text-slate-500">
                                Showing {visible.length.toLocaleString()} of {total.toLocaleString()}
                              </span>
                              <button
                                onClick={() => showMore(parent, total)}
                                className="ml-auto px-2 py-0.5 rounded-md border border-slate-200 text-slate-600 hover:bg-white hover:border-blue-300 hover:text-blue-600 font-semibold"
                              >
                                Show {Math.min(GROUP_PAGE_SIZE, remaining).toLocaleString()} more
                              </button>
                              <button
                                onClick={() => showAll(parent, total)}
                                className="px-2 py-0.5 rounded-md border border-slate-200 text-slate-600 hover:bg-white hover:border-blue-300 hover:text-blue-600 font-semibold"
                                title="Render every remaining item — may briefly lag for very large schemas"
                              >
                                Show all
                              </button>
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}

interface RecentSectionProps {
  items: CatalogItem[];
  onPick: (item: CatalogItem) => void;
}

function RecentSection({ items, onPick }: RecentSectionProps) {
  return (
    <div className="border-b border-slate-100">
      <div className="px-3 py-1.5 text-xs font-bold uppercase tracking-wider text-amber-700 bg-amber-50/60 flex items-center gap-1.5">
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" />
        </svg>
        Recent
        <span className="ml-auto text-amber-600/70 normal-case tracking-normal font-medium">
          {items.length}
        </span>
      </div>
      {items.map((item, i) => (
        <button
          key={`recent-${item.kind}-${item.parent}-${item.name}-${i}`}
          onClick={() => onPick(item)}
          className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-amber-50/40 group border-b border-slate-50 last:border-b-0"
        >
          <span className={`text-xs font-bold uppercase tracking-wide px-1.5 py-0.5 rounded border w-[68px] text-center shrink-0 ${KIND_COLOR[item.kind] || 'text-slate-500 bg-slate-50 border-slate-200'}`}>
            {KIND_ICONS[item.kind] || '·'} {item.kind}
          </span>
          <span className="text-sm text-slate-700 group-hover:text-blue-700 truncate flex-1">
            {item.name}
          </span>
          {item.parent && (
            <span className="text-xs text-slate-400 truncate max-w-[100px]">{item.parent}</span>
          )}
        </button>
      ))}
    </div>
  );
}
