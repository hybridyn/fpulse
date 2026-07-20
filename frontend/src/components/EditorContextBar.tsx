/**
 * EditorContextBar — the small "Project | Pipeline Name" ribbon that
 * sits at the top of the canvas column in the Editor.
 *
 * Mounted inside the canvas-column flex-col (between the chat panel on
 * the left and the modules panel on the right) so the bar's width
 * tracks the canvas — resize either side panel and the ribbon resizes
 * with it. Previously this lived inside Toolbar.tsx and spanned the
 * full window, which covered the side panels.
 *
 * State is self-contained: project list + dropdown open state are
 * local; pipeline name + project id come from useWorkflowStore so any
 * mutation here propagates everywhere the store is read.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import { useDarkMode } from '../hooks/useDarkMode';
import { api } from '../api/client';
import { validateWorkflow } from '../utils/validateWorkflow';

export default function EditorContextBar() {
  const dark = useDarkMode();
  const workflowName = useWorkflowStore((s) => s.workflowName);
  const setWorkflowName = useWorkflowStore((s) => s.setWorkflowName);
  const editorSurface = useWorkflowStore((s) => s.editorSurface);
  const workflowId = useWorkflowStore((s) => s.workflowId);
  const projectId = useWorkflowStore((s) => s.projectId);
  const setProjectId = useWorkflowStore((s) => s.setProjectId);
  const folderId = useWorkflowStore((s) => s.folderId);
  const setFolderId = useWorkflowStore((s) => s.setFolderId);
  const version = useWorkflowStore((s) => s.version);
  const isDirty = useWorkflowStore((s) => s.isDirty);
  // Validation chip inputs — moved here from Toolbar on 2026-05-11 so
  // the chip stops overlapping the Templates sub-tab in the top nav.
  const nodes = useWorkflowStore((s) => s.nodes);
  const edges = useWorkflowStore((s) => s.edges);
  const parameters = useWorkflowStore((s) => s.parameters);
  // 2026-05-19 (P2 #15 of PAGE_BY_PAGE_AUDIT.md): the validator was
  // recomputed on every node-position change (~60×/sec mid-drag) and its
  // deactivation-shadow pass is O(N²). We now debounce by ~150 ms so an
  // in-flight drag fires ~6 validations/sec instead of 60. The first
  // render runs synchronously so the chip shows the real count on paint
  // rather than a flash of zero. `useMemo` is kept removed in favour of
  // a stateful debounced result.
  const [validationIssues, setValidationIssues] = useState<ReturnType<typeof validateWorkflow>>(() => {
    try { return validateWorkflow(nodes, edges, parameters, workflowId); }
    catch { return []; }
  });
  useEffect(() => {
    const handle = window.setTimeout(() => {
      try { setValidationIssues(validateWorkflow(nodes, edges, parameters, workflowId)); }
      catch { setValidationIssues([]); }
    }, 150);
    return () => window.clearTimeout(handle);
  }, [nodes, edges, parameters, workflowId]);
  const validationStepCount = (nodes || []).filter((n: any) => n.type !== 'sticky_note').length;
  const validationErrorCount = validationIssues.filter((i: any) => i.level === 'error').length;
  const validationWarnCount = validationIssues.filter((i: any) => i.level === 'warning').length;

  const [projects, setProjects] = useState<Array<{ id: string; name: string }>>([]);
  const [projectMenuOpen, setProjectMenuOpen] = useState(false);
  // Folder-tree expansion state for the project picker — mirrors the
  // chevron-driven behaviour on MoveToProjectButton so users can save
  // a new pipeline straight into a sub-folder.
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(new Set());
  const [foldersByProject, setFoldersByProject] = useState<Record<string, Array<{ id: string; name: string; parent_folder_id: string | null }>>>({});
  const toggleProject = async (pid: string) => {
    setExpandedProjects((prev) => {
      const next = new Set(prev);
      if (next.has(pid)) next.delete(pid);
      else next.add(pid);
      return next;
    });
    if (foldersByProject[pid] !== undefined) return;
    try {
      const folders = await api.listFolders(pid);
      const mapped = (Array.isArray(folders) ? folders : []).map((f: any) => ({
        id: f.id,
        name: f.name || f.id,
        parent_folder_id: f.parent_folder_id ?? null,
      }));
      setFoldersByProject((m) => ({ ...m, [pid]: mapped }));
    } catch {
      setFoldersByProject((m) => ({ ...m, [pid]: [] }));
    }
  };
  // Build a parent → children tree for nested folder display.
  function buildFolderTree(folders: Array<{ id: string; name: string; parent_folder_id: string | null }>): Array<{ folder: { id: string; name: string }; children: any[] }> {
    const byId = new Map<string, any>();
    for (const f of folders) byId.set(f.id, { folder: f, children: [] });
    const roots: any[] = [];
    for (const node of byId.values()) {
      const pid = node.folder.parent_folder_id;
      if (pid && pid !== node.folder.id && byId.has(pid)) byId.get(pid).children.push(node);
      else roots.push(node);
    }
    const sortRec = (nodes: any[]) => { nodes.sort((a, b) => a.folder.name.localeCompare(b.folder.name)); nodes.forEach((n) => sortRec(n.children)); };
    sortRec(roots);
    return roots;
  }
  const renderFolderRows = (nodes: any[], depth: number, pid: string): React.ReactNode[] =>
    nodes.flatMap((node: any) => {
      const isCurrent = (projectId || 'default') === pid && folderId === node.folder.id;
      return [
        <button
          key={node.folder.id}
          type="button"
          onClick={() => {
            setProjectId(pid === 'default' ? null : pid);
            setFolderId(node.folder.id);
            setProjectMenuOpen(false);
          }}
          className={`w-full text-left pr-3 py-1.5 text-sm flex items-center gap-2 hover:bg-slate-50 ${isCurrent ? 'text-amber-700 font-semibold' : 'text-slate-600'}`}
          style={{ paddingLeft: 28 + depth * 12 }}
        >
          <span className="text-slate-400">↳</span>
          <span className="flex-1 truncate">{node.folder.name}</span>
          {isCurrent && <span className="text-xs font-semibold px-1.5 py-0.5 rounded bg-amber-100 text-amber-700">current</span>}
        </button>,
        ...renderFolderRows(node.children, depth + 1, pid),
      ];
    });
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState(workflowName);
  const projectMenuRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const isFileDataPrep = editorSurface === 'file_data_prep';
  const nameLabel = isFileDataPrep ? 'Query Name' : 'Pipeline Name';
  const nameTitle = isFileDataPrep ? 'Click to rename data prep query' : 'Click to rename';

  // Save indicator state (moved from Toolbar). Tracks the last
  // dirty→clean transition so we can show "Saved just now / Xm ago".
  const [lastSavedAt, setLastSavedAt] = useState<Date | null>(null);
  const prevDirtyRef = useRef(isDirty);
  useEffect(() => {
    if (prevDirtyRef.current && !isDirty) {
      setLastSavedAt(new Date());
    }
    prevDirtyRef.current = isDirty;
  }, [isDirty]);
  // Tick once a minute so the relative-time label stays fresh.
  const [, _setSaveTick] = useState(0);
  useEffect(() => {
    if (!lastSavedAt) return;
    const id = setInterval(() => _setSaveTick((n) => (n + 1) % 1_000_000), 60_000);
    return () => clearInterval(id);
  }, [lastSavedAt]);

  // Project list — same one-shot fetch the Toolbar used to do.
  useEffect(() => {
    api.listProjects()
      .then((p: any[]) => setProjects(Array.isArray(p) ? p.map((x: any) => ({ id: x.id, name: x.name || x.id })) : []))
      .catch(() => setProjects([]));
  }, []);

  // Close project dropdown on outside click.
  //
  // 2026-05-22 — listener uses CAPTURE phase (`true`) so we get the
  // mousedown before xyflow's canvas wrapper calls
  // `event.stopPropagation()` on its own pointer handlers. Without
  // capture, clicking on the React Flow canvas (the most common
  // "outside" target since the canvas fills the screen) silently
  // discarded the event before it reached this listener and the
  // dropdown stayed open until the user clicked the trigger button
  // again. Also listen on `document` rather than `window` so the
  // event chain reaches us regardless of bubbling target.
  // Touchstart added too so the dropdown closes on mobile / pen
  // input the same way it closes on mouse.
  useEffect(() => {
    if (!projectMenuOpen) return;
    const onDoc = (e: MouseEvent | TouchEvent) => {
      const target = e.target as Node | null;
      if (projectMenuRef.current && target && !projectMenuRef.current.contains(target)) {
        setProjectMenuOpen(false);
      }
    };
    // Capture phase + escape key as a belt-and-braces close path.
    document.addEventListener('mousedown', onDoc, true);
    document.addEventListener('touchstart', onDoc, true);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setProjectMenuOpen(false);
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc, true);
      document.removeEventListener('touchstart', onDoc, true);
      document.removeEventListener('keydown', onKey);
    };
  }, [projectMenuOpen]);

  useEffect(() => {
    setEditValue(workflowName);
  }, [workflowName]);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  const activeProject = projects.find((p) => p.id === (projectId || 'default')) || { id: projectId || 'default', name: projectId || 'Default' };
  // Resolve the active folder's display name from any cached folder list
  // (the user just expanded that project; the cache survives close/re-open).
  const activeFolder = folderId
    ? Object.values(foldersByProject).flat().find((f) => f.id === folderId)
    : null;
  const activeLabel = activeFolder
    ? `${activeProject.name} / ${activeFolder.name}`
    : activeProject.name;

  const commitRename = () => {
    const trimmed = editValue.trim();
    if (trimmed && trimmed !== workflowName) {
      setWorkflowName(trimmed);
    }
    setEditing(false);
  };

  return (
    // 3-zone grid — Project on the left, Pipeline Name centered (with
    // baseline of the row), version chip on the right. Slate background
    // visually separates the ribbon from the cream canvas below.
    <div className={`px-4 py-2 grid grid-cols-[1fr_auto_1fr] items-center gap-3 shrink-0 border-b ${
      dark ? 'bg-[#0f1726]/60 border-white/[0.05]' : 'bg-slate-100 border-slate-200'
    }`}>
      {/* LEFT — Project picker */}
      <div className="flex items-center min-w-0">
        <div className="relative" ref={projectMenuRef}>
          <button
            type="button"
            onClick={() => { if (!workflowId) setProjectMenuOpen((o) => !o); }}
            disabled={!!workflowId}
            className={`flex items-center gap-2 px-3 py-1.5 rounded-xl border shadow-sm text-xs font-semibold transition-colors ${
              dark
                ? 'bg-white/[0.06] border-white/[0.1] text-slate-200'
                : 'bg-white border-slate-300 text-slate-700'
            } ${!workflowId ? 'hover:bg-slate-50 cursor-pointer' : 'opacity-80 cursor-default'}`}
            title={workflowId ? 'Project is fixed once saved — use Pipelines → Move on the row to reassign.' : 'Pick the project this pipeline lives in'}
          >
            <span className={`text-xs font-bold uppercase tracking-wider ${dark ? 'text-slate-500' : 'text-slate-500'}`}>Project</span>
            <span className={`w-px h-3.5 ${dark ? 'bg-white/[0.1]' : 'bg-slate-300'}`} />
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-amber-500">
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
            </svg>
            <span className="truncate max-w-[200px]" title={activeLabel}>{activeLabel}</span>
            {!workflowId && (
              <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="text-slate-400">
                <polyline points="6 9 12 15 18 9" />
              </svg>
            )}
          </button>
          {projectMenuOpen && (
            // 2026-05-22: bumped min-w 260 → 320 and row text from
            // text-xs → text-sm per the human-readable-text-sizes
            // floor (body content ≥ 14px). The dropdown was reported
            // as "too small" in user testing — every row is a destination
            // the user is actively choosing, so it's body content, not
            // secondary. Italic placeholder hints stay text-xs (still
            // secondary), and the "current" tag stays text-xs (it's
            // a chip not body).
            <div className="absolute left-0 top-full mt-1 z-50 min-w-[320px] rounded-lg border border-slate-200 bg-white shadow-lg overflow-hidden">
              <div className="px-3 py-2 text-xs font-bold uppercase tracking-wider text-slate-500 border-b border-slate-100 bg-slate-50">
                Save into project / folder
              </div>
              <div className="max-h-80 overflow-y-auto py-1">
                {projects.length === 0 ? (
                  <div className="px-3 py-3 text-sm text-slate-500 italic">No projects available</div>
                ) : (
                  projects.map((p) => {
                    const isCurrentProjectRoot = (projectId || 'default') === p.id && !folderId;
                    const isOpen = expandedProjects.has(p.id);
                    const folderList = foldersByProject[p.id];
                    return (
                      <div key={p.id}>
                        <div className="flex items-center hover:bg-slate-50">
                          <button
                            type="button"
                            onClick={(e) => { e.stopPropagation(); toggleProject(p.id); }}
                            className="w-8 h-8 flex items-center justify-center text-slate-400 hover:text-slate-700"
                            aria-label={isOpen ? 'Collapse folders' : 'Expand folders'}
                            title={isOpen ? 'Collapse folders' : 'Show folders in this project'}
                          >
                            <svg
                              width="12" height="12" viewBox="0 0 24 24" fill="none"
                              stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
                              className={`transition-transform ${isOpen ? 'rotate-90' : ''}`}
                            >
                              <polyline points="9 18 15 12 9 6" />
                            </svg>
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              setProjectId(p.id === 'default' ? null : p.id);
                              setFolderId(null);
                              setProjectMenuOpen(false);
                            }}
                            className={`flex-1 text-left pl-1 pr-3 py-2 text-sm flex items-center gap-2 ${isCurrentProjectRoot ? 'text-amber-700 font-semibold' : 'text-slate-700'}`}
                          >
                            <span className="w-2 h-2 rounded-full bg-amber-400" />
                            <span className="flex-1 truncate">{p.name}</span>
                            {isCurrentProjectRoot && (
                              <span className="text-xs font-semibold px-1.5 py-0.5 rounded bg-amber-100 text-amber-700">current</span>
                            )}
                          </button>
                        </div>
                        {isOpen && (
                          <div className="pr-1 pb-1">
                            {folderList === undefined ? (
                              <div className="px-3 py-1.5 text-xs text-slate-400 italic" style={{ paddingLeft: 28 }}>Loading folders…</div>
                            ) : (
                              <>
                                <button
                                  type="button"
                                  onClick={() => {
                                    setProjectId(p.id === 'default' ? null : p.id);
                                    setFolderId(null);
                                    setProjectMenuOpen(false);
                                  }}
                                  className={`w-full text-left py-1.5 text-sm flex items-center gap-2 hover:bg-slate-50 ${isCurrentProjectRoot ? 'text-amber-700 font-medium' : 'text-slate-600'}`}
                                  style={{ paddingLeft: 28 }}
                                >
                                  <span className="text-slate-400">↳</span>
                                  <span className="flex-1 italic">Project root (no folder)</span>
                                  {isCurrentProjectRoot && (
                                    <span className="text-xs font-semibold px-1.5 py-0.5 rounded bg-amber-100 text-amber-700">current</span>
                                  )}
                                </button>
                                {folderList.length === 0 ? (
                                  <div className="py-1 text-xs text-slate-400 italic" style={{ paddingLeft: 28 }}>No folders in this project</div>
                                ) : (
                                  renderFolderRows(buildFolderTree(folderList), 0, p.id)
                                )}
                              </>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* CENTER — Pipeline Name chip */}
      <div className={`flex items-center gap-2 min-w-0 px-3 py-1.5 rounded-xl border shadow-sm ${
        dark ? 'bg-white/[0.06] border-white/[0.1]' : 'bg-white border-slate-300'
      }`}>
        <span className={`text-xs font-semibold shrink-0 ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
          {nameLabel}
        </span>
        <span className={`w-px h-4 ${dark ? 'bg-white/[0.1]' : 'bg-slate-300'} shrink-0`} />
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={dark ? 'text-indigo-400 shrink-0' : 'text-pipe-600 shrink-0'}>
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
        </svg>
        {editing ? (
          <input
            ref={inputRef}
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
            onBlur={commitRename}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commitRename();
              if (e.key === 'Escape') { setEditValue(workflowName); setEditing(false); }
            }}
            className={`text-base font-semibold rounded-md px-2 py-0.5 focus:outline-none focus:ring-2 min-w-[120px] max-w-[260px] ${
              dark ? 'text-slate-200 bg-white/[0.06] border border-white/[0.1] focus:ring-blue-500/30' : 'text-slate-800 bg-pulse-50 border border-pulse-300 focus:ring-pipe-300'
            }`}
          />
        ) : (
          <button
            onClick={() => setEditing(true)}
            className={`text-base font-semibold rounded-md transition-colors truncate max-w-[260px] flex items-center gap-1.5 ${
              dark ? 'text-slate-200 hover:text-white' : 'text-slate-800 hover:text-slate-900'
            }`}
            title={nameTitle}
          >
            {workflowName}
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-400 shrink-0">
              <path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
            </svg>
          </button>
        )}
      </div>

      {/* RIGHT — validation chip + save status + version badge */}
      <div className="flex items-center justify-end gap-2 min-w-0">
        <ValidationChip
          dark={dark}
          stepCount={validationStepCount}
          errorCount={validationErrorCount}
          warnCount={validationWarnCount}
          issues={validationIssues}
        />
        <SaveIndicator dark={dark} isDirty={isDirty} lastSavedAt={lastSavedAt} />
        {version > 0 && (
          <span className="text-xs text-pulse-700 bg-pulse-100 px-2 py-1 rounded-md font-bold border border-pulse-200">
            v{version}
          </span>
        )}
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────────
   ValidationChip — live step-count / issue-count derived from store.
   Lifted out of the Toolbar on 2026-05-11; it was overlapping the
   Templates sub-tab in the top nav row. Lives in the canvas-column
   ribbon now next to the Save indicator.
   ───────────────────────────────────────────────────────────────────── */
function ValidationChip({
  dark, stepCount, errorCount, warnCount, issues,
}: {
  dark: boolean;
  stepCount: number;
  errorCount: number;
  warnCount: number;
  issues: Array<{ level?: string; severity?: string; message?: string }>;
}) {
  if (stepCount <= 0) return null;
  const allClean = errorCount === 0 && warnCount === 0;
  const dotClass = allClean
    ? 'bg-emerald-500'
    : errorCount > 0 ? 'bg-red-500' : 'bg-amber-500';
  const textClass = allClean
    ? (dark ? 'text-emerald-300' : 'text-emerald-700')
    : errorCount > 0
      ? (dark ? 'text-red-300' : 'text-red-700')
      : (dark ? 'text-amber-300' : 'text-amber-700');
  const bgClass = allClean
    ? (dark ? 'bg-emerald-500/10 border-emerald-500/30' : 'bg-emerald-50 border-emerald-200')
    : errorCount > 0
      ? (dark ? 'bg-red-500/10 border-red-500/30' : 'bg-red-50 border-red-200')
      : (dark ? 'bg-amber-500/10 border-amber-500/30' : 'bg-amber-50 border-amber-200');
  const label = allClean
    ? `${stepCount} step${stepCount === 1 ? '' : 's'} valid`
    : errorCount > 0
      ? `${errorCount} issue${errorCount === 1 ? '' : 's'}`
      : `${warnCount} warning${warnCount === 1 ? '' : 's'}`;
  const tooltip = allClean
    ? 'All steps validated successfully'
    : issues.slice(0, 5).map(i => `[${i.severity || i.level}] ${i.message}`).join('\n');
  return (
    <div
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-xs font-semibold shrink-0 ${bgClass} ${textClass}`}
      title={tooltip}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${dotClass} ${!allClean ? 'animate-pulse' : ''}`} />
      <span>{label}</span>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────────
   SaveIndicator — Unsaved (amber, pulsing) / Saved Xm ago (emerald).
   Lifted out of the Toolbar in May 10 2026 so it sits next to the
   pipeline name where users actually look for save state.
   ───────────────────────────────────────────────────────────────────── */
function SaveIndicator({
  dark, isDirty, lastSavedAt,
}: {
  dark: boolean;
  isDirty: boolean;
  lastSavedAt: Date | null;
}) {
  if (isDirty) {
    return (
      <div
        className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-xs font-semibold shrink-0 ${
          dark
            ? 'bg-amber-500/10 border-amber-500/30 text-amber-300'
            : 'bg-amber-50 border-amber-200 text-amber-700'
        }`}
        title="You have unsaved changes. Use Ctrl/Cmd+S or the Save button to persist."
      >
        <span className="w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
        <span>Unsaved</span>
      </div>
    );
  }
  if (!lastSavedAt) return null;
  const diffMs = Date.now() - lastSavedAt.getTime();
  const diffS = Math.round(diffMs / 1000);
  const label = diffS < 5 ? 'Saved just now'
    : diffS < 60 ? `Saved ${diffS}s ago`
    : diffS < 3600 ? `Saved ${Math.round(diffS / 60)}m ago`
    : `Saved ${Math.round(diffS / 3600)}h ago`;
  return (
    <div
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-xs font-semibold shrink-0 ${
        dark
          ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300'
          : 'bg-emerald-50 border-emerald-200 text-emerald-700'
      }`}
      title={`Last saved at ${lastSavedAt.toLocaleTimeString()}`}
    >
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="20 6 9 17 4 12" />
      </svg>
      <span>{label}</span>
    </div>
  );
}
