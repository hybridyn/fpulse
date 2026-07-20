import { useState, useEffect, useRef, useCallback, type ReactElement } from 'react';
// 2026-05-19 (OSS-9 of PAGE_BY_PAGE_AUDIT.md): canonical Page union.
import type { Page } from '../types';

interface GlobalSearchProps {
  open: boolean;
  onClose: () => void;
  onNavigate: (page: Page) => void;
  onAction: (action: string) => void;
}

interface SearchItem {
  id: string;
  label: string;
  category: 'recent' | 'pages' | 'actions';
  icon: string;
  page?: Page;
  action?: string;
  shortcut?: string;
}

// 2026-05-19 (P0 #9 of PAGE_BY_PAGE_AUDIT.md): the previous `shortcut`
// labels (G P / G W / G E / G S) were aspirational — no keydown listener
// implemented a sequence detector, so the chips lied. Stripped until a
// real sequence handler ships (tracked under P2 keyboard hygiene).
const PAGES: SearchItem[] = [
  { id: 'p-projects', label: 'Projects', category: 'pages', icon: 'folder', page: 'projects' },
  { id: 'p-pipelines', label: 'Workflows', category: 'pages', icon: 'git-branch', page: 'pipelines' },
  { id: 'p-editor', label: 'Editor', category: 'pages', icon: 'layout', page: 'editor' },
  { id: 'p-connections', label: 'Connections', category: 'pages', icon: 'plug', page: 'connections' },
  { id: 'p-credentials', label: 'Credentials', category: 'pages', icon: 'key', page: 'credentials' },
  { id: 'p-executions', label: 'Executions', category: 'pages', icon: 'play-circle', page: 'executions' },
  { id: 'p-dashboard', label: 'Dashboard', category: 'pages', icon: 'layout', page: 'dashboard' },
  { id: 'p-notifications', label: 'Notifications', category: 'pages', icon: 'bell', page: 'notifications' },
  { id: 'p-settings', label: 'Settings', category: 'pages', icon: 'settings', page: 'settings' },
  { id: 'p-help', label: 'Help', category: 'pages', icon: 'help-circle', page: 'help' },
];

const ACTIONS: SearchItem[] = [
  { id: 'a-new-project', label: 'New Project', category: 'actions', icon: 'plus-circle', action: 'new-project' },
  { id: 'a-new-pipeline', label: 'New Pipeline', category: 'actions', icon: 'plus', action: 'new-pipeline' },
  { id: 'a-run-workflow', label: 'Run Workflow', category: 'actions', icon: 'play', action: 'run-workflow' },
  // 2026-05-19 (P2 #11 of PAGE_BY_PAGE_AUDIT.md): "Toggle Dark Mode"
  // removed — main.tsx force-clears the theme on every boot, so the
  // action worked only for the current session and silently undid
  // itself on reload. Re-add when full dark-mode support ships.
  // { id: 'a-toggle-dark', label: 'Toggle Dark Mode', category: 'actions', icon: 'moon', action: 'toggle-dark-mode' },
  { id: 'a-export', label: 'Export Pipeline', category: 'actions', icon: 'download', action: 'export-pipeline' },
];

const CATEGORY_LABELS: Record<string, string> = {
  recent: 'Recent',
  pages: 'Pages',
  actions: 'Actions',
};

/* ---- Inline SVG icons ---- */
const icons: Record<string, ReactElement> = {
  'folder':       <path d="M3 7V5a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />,
  'git-branch':   <><circle cx="6" cy="6" r="2" /><circle cx="18" cy="18" r="2" /><circle cx="6" cy="18" r="2" /><path d="M6 8v8M18 16V8a4 4 0 0 0-4-4H8" /></>,
  'layout':       <><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18M9 9v12" /></>,
  'plug':         <><path d="M12 22v-5M9 8V2M15 8V2M7 8h10a2 2 0 0 1 2 2v2a5 5 0 0 1-5 5h-4a5 5 0 0 1-5-5v-2a2 2 0 0 1 2-2z" /></>,
  'key':          <><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zM15.5 7.5l3 3L22 7l-3-3" /></>,
  'braces':       <><path d="M8 3H6a2 2 0 0 0-2 2v4a2 2 0 0 1-2 2 2 2 0 0 1 2 2v4a2 2 0 0 0 2 2h2M16 3h2a2 2 0 0 1 2 2v4a2 2 0 0 1 2 2 2 2 0 0 1-2 2v4a2 2 0 0 1-2 2h-2" /></>,
  'play-circle':  <><circle cx="12" cy="12" r="10" /><polygon points="10,8 16,12 10,16" /></>,
  'clock':        <><circle cx="12" cy="12" r="10" /><polyline points="12,6 12,12 16,14" /></>,
  'bell':         <><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 0 1-3.46 0" /></>,
  'settings':     <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1.08 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1.08 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1.08z" /></>,
  'help-circle':  <><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></>,
  'plus-circle':  <><circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="16" /><line x1="8" y1="12" x2="16" y2="12" /></>,
  'plus':         <><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></>,
  'play':         <polygon points="5,3 19,12 5,21" />,
  'moon':         <path d="M21 12.79A9 9 0 1 1 11.21 3a7 7 0 0 0 9.79 9.79z" />,
  'download':     <><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7,10 12,15 17,10" /><line x1="12" y1="15" x2="12" y2="3" /></>,
  'search':       <><circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" /></>,
  'file-x':       <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14,2 14,8 20,8" /><line x1="9.5" y1="12.5" x2="14.5" y2="17.5" /><line x1="14.5" y1="12.5" x2="9.5" y2="17.5" /></>,
};

function Icon({ name, className = '' }: { name: string; className?: string }) {
  return (
    <svg
      className={className}
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {icons[name] ?? icons['file-x']}
    </svg>
  );
}

/* ---- helpers ---- */
function getRecentPages(): SearchItem[] {
  try {
    const raw = localStorage.getItem('fpulse_recent_pages');
    if (!raw) return [];
    const pages: string[] = JSON.parse(raw);
    return pages
      .slice(0, 3)
      .map((p) => {
        const match = PAGES.find((pg) => pg.page === p);
        if (!match) return null;
        return { ...match, id: `r-${match.page}`, category: 'recent' as const };
      })
      .filter(Boolean) as SearchItem[];
  } catch {
    return [];
  }
}

function matchItem(item: SearchItem, query: string): boolean {
  return item.label.toLowerCase().includes(query);
}

/* ---- Component ---- */
export default function GlobalSearch({ open, onClose, onNavigate, onAction }: GlobalSearchProps) {
  const [query, setQuery] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Build filtered results
  const buildResults = useCallback((): SearchItem[] => {
    const q = query.trim().toLowerCase();
    const recent = getRecentPages();

    if (!q) {
      // No query: show recent (if any) then pages then actions
      return [...recent, ...PAGES, ...ACTIONS];
    }

    const filteredRecent = recent.filter((item) => matchItem(item, q));
    const filteredPages = PAGES.filter((item) => matchItem(item, q));
    const filteredActions = ACTIONS.filter((item) => matchItem(item, q));

    return [...filteredRecent, ...filteredPages, ...filteredActions];
  }, [query]);

  const results = buildResults();

  // Reset selection when query changes
  useEffect(() => {
    setSelectedIndex(0);
  }, [query]);

  // Focus input on open, reset state
  useEffect(() => {
    if (open) {
      setQuery('');
      setSelectedIndex(0);
      // Small delay so the DOM is rendered before focusing
      requestAnimationFrame(() => {
        inputRef.current?.focus();
      });
    }
  }, [open]);

  // Scroll selected item into view
  useEffect(() => {
    if (!listRef.current) return;
    const selected = listRef.current.querySelector('[data-selected="true"]');
    selected?.scrollIntoView({ block: 'nearest' });
  }, [selectedIndex]);

  const executeItem = useCallback(
    (item: SearchItem) => {
      if (item.page) {
        onNavigate(item.page);
      } else if (item.action) {
        onAction(item.action);
      }
      onClose();
    },
    [onNavigate, onAction, onClose],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSelectedIndex((prev) => (prev + 1) % Math.max(results.length, 1));
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSelectedIndex((prev) => (prev - 1 + results.length) % Math.max(results.length, 1));
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (results[selectedIndex]) {
          executeItem(results[selectedIndex]);
        }
      } else if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    },
    [results, selectedIndex, executeItem, onClose],
  );

  if (!open) return null;

  // Group results by category preserving order
  const grouped: { category: string; items: SearchItem[] }[] = [];
  let lastCat = '';
  for (const item of results) {
    if (item.category !== lastCat) {
      grouped.push({ category: item.category, items: [] });
      lastCat = item.category;
    }
    grouped[grouped.length - 1].items.push(item);
  }

  // Compute flat index for each item
  let flatIdx = 0;

  return (
    <div
      className="fixed inset-0 z-[70] flex items-start justify-center pt-[15vh] bg-black/40 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-xl max-w-md w-full shadow-2xl mx-4 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={handleKeyDown}
      >
        {/* Search input */}
        <div className="flex items-center gap-2 px-4 py-3 border-b border-slate-100">
          <Icon name="search" className="text-slate-400 shrink-0" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search or jump to..."
            className="w-full text-sm bg-transparent outline-none placeholder:text-slate-400 text-slate-800"
          />
          <kbd className="hidden sm:inline-flex items-center px-1.5 py-0.5 text-xs font-medium text-slate-400 bg-slate-100 rounded">
            esc
          </kbd>
        </div>

        {/* Results */}
        <div ref={listRef} className="max-h-80 overflow-y-auto py-1">
          {results.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-slate-400">
              <Icon name="file-x" className="w-8 h-8 mb-2 opacity-40" />
              <span className="text-sm">No results</span>
            </div>
          ) : (
            grouped.map((group) => (
              <div key={group.category}>
                <div className="px-4 pt-3 pb-1">
                  <span className="text-xs uppercase font-bold text-slate-400 tracking-wider">
                    {CATEGORY_LABELS[group.category] ?? group.category}
                  </span>
                </div>
                {group.items.map((item) => {
                  const idx = flatIdx++;
                  const isSelected = idx === selectedIndex;
                  return (
                    <button
                      key={item.id}
                      data-selected={isSelected}
                      className={`w-full flex items-center gap-3 px-4 py-2 text-left text-sm transition-colors cursor-pointer ${
                        isSelected
                          ? 'bg-amber-50 border-l-2 border-amber-400'
                          : 'border-l-2 border-transparent hover:bg-slate-50'
                      }`}
                      onMouseEnter={() => setSelectedIndex(idx)}
                      onClick={() => executeItem(item)}
                    >
                      <Icon name={item.icon} className="text-slate-500 shrink-0" />
                      <span className="flex-1 text-slate-700 truncate">{item.label}</span>
                      <span
                        className={`text-xs px-1.5 py-0.5 rounded font-medium ${
                          item.category === 'pages'
                            ? 'bg-blue-50 text-blue-500'
                            : item.category === 'actions'
                              ? 'bg-violet-50 text-violet-500'
                              : 'bg-slate-100 text-slate-400'
                        }`}
                      >
                        {CATEGORY_LABELS[item.category]}
                      </span>
                      {item.shortcut && (
                        <kbd className="hidden sm:inline-flex items-center px-1.5 py-0.5 text-xs font-medium text-slate-400 bg-slate-100 rounded ml-1">
                          {item.shortcut}
                        </kbd>
                      )}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>

        {/* Footer hint */}
        <div className="flex items-center gap-3 px-4 py-2 border-t border-slate-100 text-xs text-slate-400">
          <span>
            <kbd className="px-1 py-0.5 bg-slate-100 rounded">&#8593;&#8595;</kbd> navigate
          </span>
          <span>
            <kbd className="px-1 py-0.5 bg-slate-100 rounded">&#9166;</kbd> select
          </span>
          <span>
            <kbd className="px-1 py-0.5 bg-slate-100 rounded">esc</kbd> close
          </span>
        </div>
      </div>
    </div>
  );
}
