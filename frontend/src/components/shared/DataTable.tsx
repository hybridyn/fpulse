/**
 * DataTable — Reusable table with configurable columns, column picker, and multi-level export.
 *
 * Usage:
 *   <DataTable
 *     storageKey="fpulse_connections_columns"
 *     columns={COLUMNS}
 *     data={filteredData}
 *     renderCell={(row, col) => <span>...</span>}
 *     exportFilename="connections"
 *     exportRowBuilder={(row) => ({ id: row.id, name: row.name, ... })}
 *     emptyIcon={<SvgIcon />}
 *     emptyTitle="No connections"
 *     emptyDesc="Create a connection to get started"
 *   />
 */

import { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import Icon, { type IconName } from './Icon';

export interface TableColumn {
  key: string;
  label: string;
  width: string;             // CSS width: '1fr', '80px', '120px', etc.
  default: boolean;          // visible by default
  align?: 'left' | 'center' | 'right';
  group: string;             // e.g. 'core', 'metadata', 'dates'
}

export interface ColumnGroup {
  key: string;
  label: string;
  icon: string;
}

interface DataTableProps<T> {
  storageKey: string;
  columns: TableColumn[];
  columnGroups: ColumnGroup[];
  data: T[];
  renderCell: (row: T, col: TableColumn, index: number) => React.ReactNode;
  rowKey: (row: T) => string;
  exportFilename?: string;
  exportRowBuilder?: (row: T) => Record<string, any>;
  emptyIcon?: React.ReactNode;
  emptyTitle?: string;
  emptyDesc?: string;
  onRowClick?: (row: T) => void;
  headerExtra?: React.ReactNode;
  pageName?: string;             // for page-level export naming
  projectGrouper?: (row: T) => string; // for project-level export
  searchValue?: string;
  onSearchChange?: (value: string) => void;
  searchPlaceholder?: string;
}

export default function DataTable<T>({
  storageKey,
  columns,
  columnGroups,
  data,
  renderCell,
  rowKey,
  exportFilename = 'export',
  exportRowBuilder,
  emptyIcon,
  emptyTitle = 'No data',
  emptyDesc,
  onRowClick,
  headerExtra,
  pageName,
  projectGrouper,
}: DataTableProps<T>) {
  const [showColumnPicker, setShowColumnPicker] = useState(false);
  const [showExportMenu, setShowExportMenu] = useState(false);
  const [visibleColumns, setVisibleColumns] = useState<Set<string>>(() => {
    try {
      const saved = localStorage.getItem(storageKey);
      if (saved) return new Set(JSON.parse(saved));
    } catch {}
    return new Set(columns.filter(c => c.default).map(c => c.key));
  });

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

  // ── Export helpers ──
  const downloadFile = useCallback((content: string, filename: string, type = 'text/csv') => {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename; a.click();
    URL.revokeObjectURL(url);
  }, []);

  const doExport = useCallback((level: 'visible' | 'full' | 'project', format: 'csv' | 'json') => {
    if (!exportRowBuilder) return;
    const rows = data.map(exportRowBuilder);
    if (rows.length === 0) return;

    let filename = exportFilename;
    let exportData: any = rows;

    if (level === 'visible') {
      // Only export visible columns
      const visKeys = new Set(activeColumns.map(c => c.key));
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
  }, [data, exportRowBuilder, exportFilename, activeColumns, projectGrouper, downloadFile]);

  return (
    <div className="bg-white rounded-lg border border-slate-200 overflow-hidden">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-4 py-2 bg-slate-50 border-b border-slate-200">
        <span className="text-xs text-slate-400 font-medium">
          {data.length} record{data.length !== 1 ? 's' : ''} · {activeColumns.length}/{columns.length} columns
        </span>
        <div className="flex items-center gap-1.5">
          {headerExtra}

          {/* Column picker */}
          <div className="relative" ref={columnPickerRef}>
            <button onClick={() => { setShowColumnPicker(!showColumnPicker); setShowExportMenu(false); }}
              className="px-2.5 py-1.5 text-xs font-semibold text-slate-500 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors flex items-center gap-1.5">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></svg>
              Columns
              <span className="text-[8px] bg-slate-200 px-1 py-0.5 rounded-full text-slate-500">{activeColumns.length}</span>
            </button>
            {showColumnPicker && (
              <div className="absolute right-0 top-full mt-1 w-64 bg-white rounded-xl shadow-xl border border-slate-200 z-50 overflow-hidden">
                <div className="px-3 py-2 border-b border-slate-100 flex items-center justify-between">
                  <span className="text-xs font-bold text-slate-700">Columns</span>
                  <div className="flex items-center gap-2">
                    <button onClick={selectAll} className="text-[9px] text-blue-500 hover:text-blue-600 font-medium">All</button>
                    <span className="text-slate-300">|</span>
                    <button onClick={resetToDefaults} className="text-[9px] text-slate-400 hover:text-slate-600 font-medium">Default</button>
                  </div>
                </div>
                <div className="max-h-[320px] overflow-y-auto py-0.5">
                  {columnGroups.map(group => {
                    const cols = columns.filter(c => c.group === group.key);
                    if (cols.length === 0) return null;
                    return (
                      <div key={group.key}>
                        <div className="px-3 py-1 text-[8px] font-bold text-slate-400 uppercase tracking-wider bg-slate-50 flex items-center gap-1.5">
                          {/^[a-z][a-z0-9-]*$/.test(group.icon)
                            ? <Icon name={group.icon as IconName} size={10} />
                            : <span>{group.icon}</span>}
                          <span>{group.label}</span>
                        </div>
                        {cols.map(col => (
                          <label key={col.key} className="flex items-center gap-2.5 px-3 py-1.5 hover:bg-slate-50 cursor-pointer">
                            <input type="checkbox" checked={visibleColumns.has(col.key)} onChange={() => toggleColumn(col.key)}
                              className="w-3 h-3 rounded border-slate-300 text-blue-500 focus:ring-blue-500/20" />
                            <span className="text-xs text-slate-700 flex-1">{col.label}</span>
                            {col.default && <span className="text-[7px] text-slate-300 font-medium">DEFAULT</span>}
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
                className="px-2.5 py-1.5 text-xs font-semibold text-slate-500 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors flex items-center gap-1.5">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>
                Export
              </button>
              {showExportMenu && (
                <div className="absolute right-0 top-full mt-1 w-52 bg-white rounded-xl shadow-xl border border-slate-200 z-50 overflow-hidden py-1">
                  <div className="px-3 py-1 text-[8px] font-bold text-slate-400 uppercase tracking-wider">Current View</div>
                  <button onClick={() => { doExport('visible', 'csv'); setShowExportMenu(false); }}
                    className="w-full px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50 flex items-center gap-2">
                    <span className="text-xs">📄</span> Visible Columns (CSV)
                  </button>
                  <button onClick={() => { doExport('visible', 'json'); setShowExportMenu(false); }}
                    className="w-full px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50 flex items-center gap-2">
                    <span className="text-xs">📄</span> Visible Columns (JSON)
                  </button>
                  <div className="border-t border-slate-100 my-0.5" />
                  <div className="px-3 py-1 text-[8px] font-bold text-slate-400 uppercase tracking-wider">Full Metadata</div>
                  <button onClick={() => { doExport('full', 'csv'); setShowExportMenu(false); }}
                    className="w-full px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50 flex items-center gap-2">
                    <span className="text-xs">📋</span> All Fields (CSV)
                  </button>
                  <button onClick={() => { doExport('full', 'json'); setShowExportMenu(false); }}
                    className="w-full px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50 flex items-center gap-2">
                    <span className="text-xs">📋</span> All Fields (JSON)
                  </button>
                  {projectGrouper && (
                    <>
                      <div className="border-t border-slate-100 my-0.5" />
                      <div className="px-3 py-1 text-[8px] font-bold text-slate-400 uppercase tracking-wider">Grouped</div>
                      <button onClick={() => { doExport('project', 'csv'); setShowExportMenu(false); }}
                        className="w-full px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50 flex items-center gap-2">
                        <span className="text-xs">📁</span> By Project (CSV)
                      </button>
                      <button onClick={() => { doExport('project', 'json'); setShowExportMenu(false); }}
                        className="w-full px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50 flex items-center gap-2">
                        <span className="text-xs">📁</span> By Project (JSON)
                      </button>
                    </>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Table */}
      {data.length === 0 ? (
        <div className="text-center py-14">
          {emptyIcon && <div className="w-14 h-14 mx-auto mb-3 rounded-2xl bg-slate-50 flex items-center justify-center">{emptyIcon}</div>}
          <p className="text-sm font-semibold text-slate-600">{emptyTitle}</p>
          {emptyDesc && <p className="text-xs text-slate-400 mt-1">{emptyDesc}</p>}
        </div>
      ) : (
        <div className="overflow-x-auto">
          {/* Header */}
          <div className="flex gap-0 px-4 py-2 bg-slate-50 border-b border-slate-200 text-[9px] font-bold text-slate-400 uppercase tracking-wider min-w-fit">
            {activeColumns.map(col => (
              <div key={col.key} style={{ width: col.width === '1fr' ? undefined : col.width, flex: col.width === '1fr' ? '1 1 0%' : `0 0 ${col.width}` }}
                className={col.align === 'right' ? 'text-right' : col.align === 'center' ? 'text-center' : 'text-left'}>
                {col.label}
              </div>
            ))}
          </div>
          {/* Rows */}
          {data.map((row, ri) => (
            <div key={rowKey(row)}
              onClick={() => onRowClick?.(row)}
              className={`flex gap-0 px-4 py-2.5 items-center transition-colors hover:bg-slate-50/80 min-w-fit ${
                onRowClick ? 'cursor-pointer' : ''
              } ${ri < data.length - 1 ? 'border-b border-slate-100' : ''}`}>
              {activeColumns.map(col => (
                <div key={col.key}
                  style={{ width: col.width === '1fr' ? undefined : col.width, flex: col.width === '1fr' ? '1 1 0%' : `0 0 ${col.width}` }}
                  className={`${col.align === 'right' ? 'text-right flex justify-end' : col.align === 'center' ? 'text-center flex justify-center' : 'text-left'} min-w-0`}>
                  {renderCell(row, col, ri)}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
