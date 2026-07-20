/**
 * TableToolbar — Drop-in toolbar that adds Column Picker + Multi-Level Export
 * to any existing table/list page. Does NOT require rewriting the table.
 *
 * Usage:
 *   <TableToolbar
 *     storageKey="fpulse_connections"
 *     data={connections}
 *     exportRowBuilder={(c) => ({ id: c.id, name: c.name, type: c.type, ... })}
 *     exportFilename="connections"
 *     columns={CONN_COLUMNS}
 *     columnGroups={CONN_GROUPS}
 *     visibleColumns={visibleColumns}
 *     onToggleColumn={toggleColumn}
 *     projectGrouper={(c) => c.project_id || 'global'}
 *     recordLabel="connection"
 *   />
 *
 * Or use the hook for state management:
 *   const { visibleColumns, activeColumns, toggleColumn, ... } = useTableColumns(storageKey, columns);
 */

import { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import Icon, { type IconName } from './Icon';

// Group-icon strings can be either:
//   • a single text glyph (◆ ◇ ◈ ⚙ ▶ ○ — monochrome, renders consistently
//     across systems), or
//   • a known IconName from the shared Icon set, which will render as a
//     line-art SVG. Pages that previously used colorful emoji (📋 🏷️ 📅)
//     should switch to the SVG-name form so the icon renders identically
//     on every OS.
function isIconName(s: string): s is IconName {
  // Cheap rule: IconName strings are kebab-case ASCII (start with a-z,
  // contain only a-z, 0-9, '-'). Any single-char glyph or non-ASCII bypasses.
  return /^[a-z][a-z0-9-]*$/.test(s);
}

export interface TColumn {
  key: string;
  label: string;
  default: boolean;
  group: string;
}

export interface TColumnGroup {
  key: string;
  label: string;
  icon: string;
}

/* ═══ Hook: useTableColumns ═══ */
export function useTableColumns(storageKey: string, columns: TColumn[]) {
  const [visibleColumns, setVisibleColumns] = useState<Set<string>>(() => {
    try {
      const saved = localStorage.getItem(storageKey);
      if (saved) return new Set(JSON.parse(saved));
    } catch {}
    return new Set(columns.filter(c => c.default).map(c => c.key));
  });

  const toggleColumn = useCallback((key: string) => {
    setVisibleColumns(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      localStorage.setItem(storageKey, JSON.stringify([...next]));
      return next;
    });
  }, [storageKey]);

  const resetToDefaults = useCallback(() => {
    const defaults = new Set(columns.filter(c => c.default).map(c => c.key));
    setVisibleColumns(defaults);
    localStorage.setItem(storageKey, JSON.stringify([...defaults]));
  }, [columns, storageKey]);

  const selectAll = useCallback(() => {
    const all = new Set(columns.map(c => c.key));
    setVisibleColumns(all);
    localStorage.setItem(storageKey, JSON.stringify([...all]));
  }, [columns, storageKey]);

  const activeColumns = useMemo(() =>
    columns.filter(c => visibleColumns.has(c.key)),
  [columns, visibleColumns]);

  const isVisible = useCallback((key: string) => visibleColumns.has(key), [visibleColumns]);

  return { visibleColumns, activeColumns, toggleColumn, resetToDefaults, selectAll, isVisible };
}

/* ═══ Component: TableToolbar ═══ */
interface TableToolbarProps<T> {
  data: T[];
  columns: TColumn[];
  columnGroups: TColumnGroup[];
  visibleColumns: Set<string>;
  activeColumnCount: number;
  onToggleColumn: (key: string) => void;
  onResetDefaults: () => void;
  onSelectAll: () => void;
  exportRowBuilder?: (row: T) => Record<string, any>;
  exportFilename?: string;
  projectGrouper?: (row: T) => string;
  recordLabel?: string;
  searchValue?: string;
  onSearchChange?: (value: string) => void;
  searchPlaceholder?: string;
  children?: React.ReactNode;
  /**
   * Theme v2 — drives the toolbar chrome.
   *   'dev'  → pale lavender strip + slate text (matches DEV lavender thead below).
   *   'prod' → solid navy + white text (matches PROD navy thead below).
   * Defaults to 'dev' so pages that don't pass env yet render as DEV
   * (the common case; PROD pages must opt in explicitly).
   */
  env?: 'dev' | 'prod';
}

/* Toolbar-chrome class sets keyed by env. Kept as two concrete strings
   because Tailwind JIT can't resolve `bg-${env}-100` dynamically. */
const TOOLBAR_CHROME = {
  dev: {
    // Unified with DEV thead — toolbar + thead read as a single charcoal strip.
    // Amber-tinted text hints at the amber thead labels below; same semi-transparent
    // white control pattern as PROD for consistency.
    bar:      'bg-thead-dev-bg rounded-t-lg',
    summary:  'text-amber-200/90',
    button:   'bg-white/10 border border-white/20 text-white hover:bg-white/20',
    search:   'bg-white/10 border border-white/20 text-white placeholder:text-white/50 focus:ring-2 focus:ring-amber-300/40 focus:border-amber-400',
    searchIconStroke: '#fcd34d',
  },
  prod: {
    bar:      'bg-thead-prod-bg rounded-t-lg',
    summary:  'text-white/85',
    button:   'bg-white/10 border border-white/20 text-white hover:bg-white/20',
    search:   'bg-white/10 border border-white/20 text-white placeholder:text-white/50 focus:ring-2 focus:ring-naples/40 focus:border-naples',
    searchIconStroke: '#cbd5e1',
  },
} as const;

export default function TableToolbar<T>({
  data,
  columns,
  columnGroups,
  visibleColumns,
  activeColumnCount,
  onToggleColumn,
  onResetDefaults,
  onSelectAll,
  exportRowBuilder,
  exportFilename = 'export',
  projectGrouper,
  recordLabel = 'record',
  searchValue,
  onSearchChange,
  searchPlaceholder,
  children,
  env = 'dev',
}: TableToolbarProps<T>) {
  const chrome = TOOLBAR_CHROME[env];
  const [showColumnPicker, setShowColumnPicker] = useState(false);
  const [showExportMenu, setShowExportMenu] = useState(false);
  const columnPickerRef = useRef<HTMLDivElement>(null);
  const exportMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (columnPickerRef.current && !columnPickerRef.current.contains(e.target as Node)) setShowColumnPicker(false);
      if (exportMenuRef.current && !exportMenuRef.current.contains(e.target as Node)) setShowExportMenu(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const downloadFile = useCallback((content: string, filename: string, type = 'text/csv') => {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename; a.click();
    URL.revokeObjectURL(url);
  }, []);

  const doExport = useCallback((level: 'visible' | 'full' | 'project', format: 'csv' | 'json') => {
    if (!exportRowBuilder || data.length === 0) return;
    const rows = data.map(exportRowBuilder);
    let filename = exportFilename;
    let exportData: any = rows;

    if (level === 'visible') {
      const visKeys = visibleColumns;
      exportData = rows.map(r => {
        const out: any = {};
        for (const k of Object.keys(r)) {
          if (visKeys.has(k) || k === 'id' || k === 'name') out[k] = r[k];
        }
        return out;
      });
      filename += '_visible';
    } else if (level === 'project' && projectGrouper) {
      const groups: Record<string, any[]> = {};
      data.forEach((row, i) => {
        const proj = projectGrouper(row);
        if (!groups[proj]) groups[proj] = [];
        groups[proj].push(rows[i]);
      });
      if (format === 'json') {
        downloadFile(JSON.stringify({ projects: Object.entries(groups).map(([k, v]) => ({ project: k, count: v.length, items: v })) }, null, 2),
          `${filename}_by_project.json`, 'application/json');
        return;
      }
      filename += '_by_project';
    }

    const dateStr = new Date().toISOString().slice(0, 10);
    if (format === 'json') {
      downloadFile(JSON.stringify(exportData, null, 2), `${filename}_${dateStr}.json`, 'application/json');
    } else {
      const headers = Object.keys(exportData[0]);
      const csvHeader = headers.join(',');
      const csvRows = exportData.map((r: any) => headers.map(h => `"${String(r[h] ?? '').replace(/"/g, '""')}"`).join(','));
      downloadFile([csvHeader, ...csvRows].join('\n'), `${filename}_${dateStr}.csv`);
    }
  }, [data, exportRowBuilder, exportFilename, visibleColumns, projectGrouper, downloadFile]);

  // Summary strip — chrome colour comes from TOOLBAR_CHROME[env] (theme v2).
  //   DEV  → pale lavender, slate-ish text. Softer than the thead lavender so the thead remains the loud element.
  //   PROD → solid navy + white text. Same navy as the thead; visually unified as one "production table" container.
  return (
    <div className={`flex items-center justify-between px-4 py-2 ${chrome.bar}`}>
      <span className={`text-xs ${chrome.summary} font-medium`}>
        {data.length} {recordLabel}{data.length !== 1 ? 's' : ''} · {activeColumnCount}/{columns.length} columns
      </span>
      <div className="flex items-center gap-1.5">
        {children}

        {/* Search */}
        {onSearchChange && (
          <div className="relative">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke={chrome.searchIconStroke} strokeWidth="2"
              className="absolute left-2 top-1/2 -translate-y-1/2 pointer-events-none">
              <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
            </svg>
            <input value={searchValue || ''} onChange={e => onSearchChange(e.target.value)}
              placeholder={searchPlaceholder || `Search ${recordLabel}s...`}
              className={`pl-7 pr-2.5 py-1.5 text-xs rounded-lg outline-none w-36 ${chrome.search}`} />
          </div>
        )}

        {/* Column Picker */}
        <div className="relative" ref={columnPickerRef}>
          <button onClick={() => { setShowColumnPicker(!showColumnPicker); setShowExportMenu(false); }}
            className={`px-2.5 py-1.5 text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5 ${chrome.button}`}
            title="Configure visible columns">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></svg>
            Columns
          </button>
          {showColumnPicker && (
            <div className="absolute right-0 top-full mt-1 w-60 bg-white rounded-xl shadow-xl border border-slate-200 z-50 overflow-hidden">
              <div className="px-3 py-2 border-b border-slate-100 flex items-center justify-between">
                <span className="text-xs font-bold text-slate-700">Columns</span>
                <div className="flex items-center gap-2">
                  <button onClick={onSelectAll} className="text-xs text-blue-500 hover:text-blue-600 font-medium">All</button>
                  <span className="text-slate-300">|</span>
                  <button onClick={onResetDefaults} className="text-xs text-slate-400 hover:text-slate-600 font-medium">Default</button>
                </div>
              </div>
              <div className="max-h-[320px] overflow-y-auto py-0.5">
                {columnGroups.map(group => {
                  const cols = columns.filter(c => c.group === group.key);
                  if (cols.length === 0) return null;
                  return (
                    <div key={group.key}>
                      <div className="px-3 py-1 text-xs font-bold text-slate-400 uppercase tracking-wider bg-slate-50 flex items-center gap-1.5">
                        {isIconName(group.icon)
                          ? <Icon name={group.icon} size={11} />
                          : <span>{group.icon}</span>}
                        <span>{group.label}</span>
                      </div>
                      {cols.map(col => (
                        <label key={col.key} className="flex items-center gap-2.5 px-3 py-1.5 hover:bg-slate-50 cursor-pointer">
                          <input type="checkbox" checked={visibleColumns.has(col.key)} onChange={() => onToggleColumn(col.key)}
                            className="w-3 h-3 rounded border-slate-300 text-blue-500 focus:ring-blue-500/20" />
                          <span className="text-xs text-slate-700 flex-1">{col.label}</span>
                          {col.default && <span className="text-xs text-slate-300 font-medium">DEFAULT</span>}
                        </label>
                      ))}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>

        {/* Export */}
        {exportRowBuilder && (
          <div className="relative" ref={exportMenuRef}>
            <button onClick={() => { setShowExportMenu(!showExportMenu); setShowColumnPicker(false); }}
              className={`px-2.5 py-1.5 text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5 ${chrome.button}`}
              title="Export data">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>
              Export
            </button>
            {showExportMenu && (
              <div className="absolute right-0 top-full mt-1 w-52 bg-white rounded-xl shadow-xl border border-slate-200 z-50 overflow-hidden py-1">
                <div className="px-3 py-1 text-xs font-bold text-slate-400 uppercase tracking-wider">Current View</div>
                <button onClick={() => { doExport('visible', 'csv'); setShowExportMenu(false); }}
                  className="w-full px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50 flex items-center gap-2">
                  📄 Visible Columns (CSV)
                </button>
                <button onClick={() => { doExport('visible', 'json'); setShowExportMenu(false); }}
                  className="w-full px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50 flex items-center gap-2">
                  📄 Visible Columns (JSON)
                </button>
                <div className="border-t border-slate-100 my-0.5" />
                <div className="px-3 py-1 text-xs font-bold text-slate-400 uppercase tracking-wider">Full Metadata</div>
                <button onClick={() => { doExport('full', 'csv'); setShowExportMenu(false); }}
                  className="w-full px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50 flex items-center gap-2">
                  📋 All Fields (CSV)
                </button>
                <button onClick={() => { doExport('full', 'json'); setShowExportMenu(false); }}
                  className="w-full px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50 flex items-center gap-2">
                  📋 All Fields (JSON)
                </button>
                {projectGrouper && (
                  <>
                    <div className="border-t border-slate-100 my-0.5" />
                    <button onClick={() => { doExport('project', 'csv'); setShowExportMenu(false); }}
                      className="w-full px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50 flex items-center gap-2">
                      📁 By Project (CSV)
                    </button>
                  </>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
