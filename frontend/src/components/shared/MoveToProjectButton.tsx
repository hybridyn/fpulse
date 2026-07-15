import type { ReactNode } from 'react';
import { useEffect, useRef, useState } from 'react';
import { api } from '../../api/client';

// Per-row "Move to project" action used by Pipelines, Connections, and
// Credentials pages. Renders a small folder-arrow icon button; clicking
// opens a popover with the workspace's project list, plus an optional
// "Global" entry for resources that support cross-project visibility.
//
// When `withFolders` is true (Pipelines page) each project row also has
// a chevron to expand its folder list — the user can move a pipeline
// straight to a specific folder under a specific project in one click.
// Folders are fetched lazily on first expand and cached per session.

interface FolderEntry {
  id: string;
  name: string;
  parent_folder_id: string | null;
}

interface FolderTreeEntry {
  folder: FolderEntry;
  children: FolderTreeEntry[];
}

let _cachedProjects: Array<{ id: string; name: string }> | null = null;
const _folderCache = new Map<string, FolderEntry[]>();

// Build a parent → children tree from a flat folder list. Orphans
// (parent_folder_id points to a missing/unknown folder) surface at the
// root so the picker never silently loses entries.
function buildPickerFolderTree(folders: FolderEntry[]): FolderTreeEntry[] {
  const byId = new Map<string, FolderTreeEntry>();
  for (const f of folders) byId.set(f.id, { folder: f, children: [] });
  const roots: FolderTreeEntry[] = [];
  for (const node of byId.values()) {
    const parentId = node.folder.parent_folder_id;
    if (parentId && parentId !== node.folder.id && byId.has(parentId)) {
      byId.get(parentId)!.children.push(node);
    } else {
      roots.push(node);
    }
  }
  const sortRec = (nodes: FolderTreeEntry[]) => {
    nodes.sort((a, b) => a.folder.name.localeCompare(b.folder.name));
    nodes.forEach((n) => sortRec(n.children));
  };
  sortRec(roots);
  return roots;
}

interface Props {
  currentProjectId: string | null | undefined;
  // Pipelines page passes a callback that accepts both projectId AND
  // folderId. Connections/Credentials pass the legacy single-arg form
  // and ignore the second argument.
  onMove: (targetProjectId: string, targetFolderId: string | null) => Promise<void> | void;
  // Connections + credentials may live "globally" (no project_id);
  // pipelines must always live in a project. Passing true adds the
  // "(Global)" entry at the top.
  allowGlobal?: boolean;
  // When true, each project row is expandable to reveal its folders.
  // Default false so the Connections / Credentials menus stay flat.
  withFolders?: boolean;
  // Optional current folder so we can mark it "current" when the
  // user reopens the menu on a pipeline that's already inside a folder.
  currentFolderId?: string | null;
  size?: 'sm' | 'md';
}

export default function MoveToProjectButton({
  currentProjectId,
  onMove,
  allowGlobal = false,
  withFolders = false,
  currentFolderId = null,
  size = 'sm',
}: Props) {
  const [open, setOpen] = useState(false);
  const [projects, setProjects] = useState<Array<{ id: string; name: string }>>(_cachedProjects || []);
  const [busy, setBusy] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [foldersByProject, setFoldersByProject] = useState<Record<string, FolderEntry[]>>({});
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    if (_cachedProjects) { setProjects(_cachedProjects); return; }
    api.listProjects().then((p: any[]) => {
      const mapped = (Array.isArray(p) ? p : []).map(x => ({ id: x.id, name: x.name || x.id }));
      _cachedProjects = mapped;
      setProjects(mapped);
    }).catch(() => setProjects([]));
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener('mousedown', handler);
    return () => window.removeEventListener('mousedown', handler);
  }, [open]);

  // Match RowActionButton's geometry + resting colour so this trigger
  // sits flush next to the other per-row action chips (Edit, Test,
  // Delete) without looking like a different control. 14px icon,
  // h-7 (28px) button, text-slate-500 resting → indigo on hover.
  const dim = size === 'md' ? { btn: 'h-8 min-w-[2rem]', icon: 16 } : { btn: 'h-7 min-w-[1.75rem]', icon: 14 };

  const handlePickProject = async (targetId: string) => {
    if (busy) return;
    // Picking the same project (no folder change) is a no-op.
    if (targetId === (currentProjectId || '') && !currentFolderId) {
      setOpen(false);
      return;
    }
    setBusy(targetId);
    try {
      await onMove(targetId, null);
      setOpen(false);
    } finally {
      setBusy(null);
    }
  };

  const handlePickFolder = async (projectId: string, folderId: string | null) => {
    if (busy) return;
    const folderKey = folderId || `__root__:${projectId}`;
    setBusy(folderKey);
    try {
      await onMove(projectId, folderId);
      setOpen(false);
    } finally {
      setBusy(null);
    }
  };

  const toggleProject = async (projectId: string) => {
    const next = new Set(expanded);
    if (next.has(projectId)) {
      next.delete(projectId);
      setExpanded(next);
      return;
    }
    next.add(projectId);
    setExpanded(next);
    // Lazy-load folders on first expand.
    if (foldersByProject[projectId] !== undefined) return;
    if (_folderCache.has(projectId)) {
      setFoldersByProject((m) => ({ ...m, [projectId]: _folderCache.get(projectId)! }));
      return;
    }
    try {
      const folders = await api.listFolders(projectId);
      const mapped: FolderEntry[] = (Array.isArray(folders) ? folders : []).map((f: any) => ({
        id: f.id,
        name: f.name || f.id,
        parent_folder_id: f.parent_folder_id ?? null,
      }));
      _folderCache.set(projectId, mapped);
      setFoldersByProject((m) => ({ ...m, [projectId]: mapped }));
    } catch {
      setFoldersByProject((m) => ({ ...m, [projectId]: [] }));
    }
  };

  return (
    <div className="relative inline-block" ref={ref} onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className={`${dim.btn} rounded-md flex items-center justify-center text-slate-500 hover:text-indigo-600 hover:bg-indigo-50 transition-colors`}
        title={withFolders ? 'Move to another project or folder' : 'Move to another project'}
        aria-label={withFolders ? 'Move to another project or folder' : 'Move to another project'}
      >
        <svg width={dim.icon} height={dim.icon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
          <polyline points="9 14 12 11 15 14" />
          <line x1="12" y1="11" x2="12" y2="18" />
        </svg>
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1 z-50 min-w-[240px] rounded-lg border border-slate-200 bg-white shadow-lg overflow-hidden">
          <div className="px-3 py-2 text-xs font-bold uppercase tracking-wider text-slate-500 border-b border-slate-100 bg-slate-50">
            {withFolders ? 'Move to project / folder' : 'Move to project'}
          </div>
          <div className="max-h-72 overflow-y-auto py-1">
            {allowGlobal && (
              <button
                type="button"
                onClick={() => handlePickProject('')}
                disabled={busy !== null}
                className={`w-full text-left px-3 py-1.5 text-xs hover:bg-slate-50 flex items-center gap-2 ${(currentProjectId || '') === '' ? 'text-indigo-700 font-semibold' : 'text-slate-700'} ${busy === '' ? 'opacity-60' : ''}`}
              >
                <span className="w-2 h-2 rounded-full bg-slate-300" />
                <span className="flex-1">Global (no project)</span>
                {(currentProjectId || '') === '' && <span className="text-xs">current</span>}
              </button>
            )}
            {projects.length === 0 ? (
              <div className="px-3 py-3 text-xs text-slate-500 italic">No projects available</div>
            ) : (
              projects.map(p => {
                const isCurrentProject = currentProjectId === p.id;
                const isOpen = expanded.has(p.id);
                const folders = foldersByProject[p.id];
                const projectKey = p.id;
                return (
                  <div key={p.id}>
                    <div className="flex items-center hover:bg-slate-50">
                      {withFolders && (
                        <button
                          type="button"
                          onClick={(e) => { e.stopPropagation(); toggleProject(p.id); }}
                          className="w-6 h-6 flex items-center justify-center text-slate-400 hover:text-slate-700"
                          aria-label={isOpen ? 'Collapse folders' : 'Expand folders'}
                          title={isOpen ? 'Collapse folders' : 'Show folders'}
                        >
                          <svg
                            width="10" height="10" viewBox="0 0 24 24" fill="none"
                            stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
                            className={`transition-transform ${isOpen ? 'rotate-90' : ''}`}
                          >
                            <polyline points="9 18 15 12 9 6" />
                          </svg>
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => handlePickProject(p.id)}
                        disabled={busy !== null || (isCurrentProject && !currentFolderId)}
                        className={`flex-1 text-left ${withFolders ? 'pl-1' : 'px-3'} pr-3 py-1.5 text-xs flex items-center gap-2 ${isCurrentProject && !currentFolderId ? 'text-indigo-700 font-semibold cursor-default' : 'text-slate-700'} ${busy === projectKey ? 'opacity-60' : ''}`}
                      >
                        <span className="w-2 h-2 rounded-full bg-indigo-400" />
                        <span className="flex-1 truncate">{p.name}</span>
                        {isCurrentProject && !currentFolderId && <span className="text-xs">current</span>}
                      </button>
                    </div>
                    {withFolders && isOpen && (
                      <div className="pl-7 pr-1 pb-1">
                        {folders === undefined ? (
                          <div className="px-3 py-1.5 text-xs text-slate-400 italic">Loading folders…</div>
                        ) : (
                          <>
                            {/* "Project root" entry — moves the pipeline
                                into the project but out of any folder.
                                Useful for resetting a misplaced item. */}
                            <button
                              type="button"
                              onClick={() => handlePickFolder(p.id, null)}
                              disabled={busy !== null || (isCurrentProject && !currentFolderId)}
                              className={`w-full text-left px-3 py-1 text-xs flex items-center gap-2 hover:bg-slate-50 rounded ${isCurrentProject && !currentFolderId ? 'text-indigo-700 font-medium' : 'text-slate-600'}`}
                            >
                              <span className="text-slate-400">↳</span>
                              <span className="flex-1 italic">Project root (no folder)</span>
                              {isCurrentProject && !currentFolderId && <span className="text-xs">current</span>}
                            </button>
                            {folders.length === 0 ? (
                              <div className="px-3 py-1 text-xs text-slate-400 italic">No folders in this project</div>
                            ) : (
                              renderFolderTree(buildPickerFolderTree(folders), 0, {
                                projectId: p.id,
                                busy,
                                isCurrentProject,
                                currentFolderId,
                                onPick: handlePickFolder,
                              })
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
  );
}

interface FolderRenderCtx {
  projectId: string;
  busy: string | null;
  isCurrentProject: boolean;
  currentFolderId: string | null;
  onPick: (projectId: string, folderId: string) => void;
}

// Recursive render so sub-folders display indented under their parent
// instead of as siblings. Depth controls left-padding.
function renderFolderTree(nodes: FolderTreeEntry[], depth: number, ctx: FolderRenderCtx): ReactNode {
  return nodes.map((node) => {
    const { folder, children } = node;
    const isCurrentFolder = ctx.isCurrentProject && ctx.currentFolderId === folder.id;
    const pad = 12 + depth * 12;
    return (
      <div key={folder.id}>
        <button
          type="button"
          onClick={() => ctx.onPick(ctx.projectId, folder.id)}
          disabled={ctx.busy !== null || isCurrentFolder}
          className={`w-full text-left pr-3 py-1 text-xs flex items-center gap-2 hover:bg-slate-50 rounded ${isCurrentFolder ? 'text-indigo-700 font-medium cursor-default' : 'text-slate-600'} ${ctx.busy === folder.id ? 'opacity-60' : ''}`}
          style={{ paddingLeft: pad }}
        >
          <span className="text-slate-400">↳</span>
          <span className="flex-1 truncate">{folder.name}</span>
          {isCurrentFolder && <span className="text-xs">current</span>}
        </button>
        {children.length > 0 && renderFolderTree(children, depth + 1, ctx)}
      </div>
    );
  });
}

// Bust the in-memory project + folder caches. Call after creating /
// renaming / deleting a project or folder so the next menu open re-fetches.
export function invalidateMoveToProjectCache() {
  _cachedProjects = null;
  _folderCache.clear();
}
