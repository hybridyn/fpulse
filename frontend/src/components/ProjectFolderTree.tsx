import { useState, useEffect, useCallback, useMemo } from 'react';
import { api } from '../api/client';
import { toast } from './Toast';
import { uiConfirm, uiPrompt } from '../ui/dialog';

interface ProjectNode {
  id: string;
  name: string;
  description?: string;
  color?: string;
  icon?: string;
  parent_id?: string | null;
  pipeline_count?: number;
  // 2026-05-25 — Storage rollup attached by GET /api/projects/.
  storage?: {
    file_count: number; file_bytes: number;
    table_count: number; table_bytes: number;
    output_count: number; output_bytes: number;
  };
  updated_at?: string;
  children?: ProjectNode[];
  // Optional grouping fields surfaced by groupValueResolver in ProjectsPage.
  // Backend may include these in the projects payload depending on plus
  // features; treated as optional so the OSS-free path doesn't have to
  // populate them.
  owner?: string;
  created_by?: string;
  metadata?: { department?: string; priority?: string } & Record<string, unknown>;
}

interface FolderNode {
  id: string;
  name: string;
  project_id: string;
  parent_folder_id?: string | null;
  color?: string;
  icon?: string;
}

interface FolderTreeNode {
  folder: FolderNode;
  children: FolderTreeNode[];
}

// Turn a flat list of folders into a nested tree keyed by parent_folder_id.
// Folders whose parent is missing/unknown (orphans) are surfaced at the root
// so they remain visible — the alternative is silently dropping them.
function buildFolderTree(folders: FolderNode[]): FolderTreeNode[] {
  const byId = new Map<string, FolderTreeNode>();
  for (const f of folders) byId.set(f.id, { folder: f, children: [] });
  const roots: FolderTreeNode[] = [];
  for (const node of byId.values()) {
    const parentId = node.folder.parent_folder_id;
    if (parentId && parentId !== node.folder.id && byId.has(parentId)) {
      byId.get(parentId)!.children.push(node);
    } else {
      roots.push(node);
    }
  }
  const sortRec = (nodes: FolderTreeNode[]) => {
    nodes.sort((a, b) => a.folder.name.localeCompare(b.folder.name));
    nodes.forEach((n) => sortRec(n.children));
  };
  sortRec(roots);
  return roots;
}

interface WorkflowItem {
  id: string;
  name: string;
  project_id?: string | null;
  folder_id?: string | null;
  status?: string;
  schedule_cron?: string | null;
  last_run_at?: string | null;
  updated_at?: string;
}

type GroupBy = 'none' | 'department' | 'owner' | 'priority';

interface Props {
  onSelectProject: (projectId: string, projectName?: string) => void;
  onSelectPipeline?: (workflowId: string, workflowName?: string) => void;
  canEdit?: boolean;
  // When provided, only render projects whose id is in this set.
  // Null = show everything (no filtering applied).
  filteredProjectIds?: Set<string> | null;
  // 2026-05-21: folder-scope filter, owned by the parent page so the
  // dropdown chip + tree share one state. Value space:
  //   null / ""              → no filter
  //   "<folderId>"           → only render that folder + its pipelines
  //   "unfiled:<projectId>"  → only render <projectId>'s unfiled pipelines
  folderFilter?: string | null;
  // Setter that the tree calls when the user clicks "Filter to this folder"
  // on a folder row — keeps the page's filter chip + dropdown in sync.
  onFolderFilterChange?: (next: string) => void;
  // Bucket projects under collapsible group headers. 'none' = flat.
  groupBy?: GroupBy;
  // Page-side resolver — returns the value to bucket a project under
  // (e.g. department/owner/priority). Kept as a callback so the tree
  // doesn't need to know the page's grouping vocabulary.
  groupValueResolver?: (project: ProjectNode) => string;
  // Pinned project ids — pinned projects float to the top. Persisted
  // by the parent (localStorage).
  pinnedIds?: Set<string>;
  onTogglePin?: (projectId: string) => void;
  // 2026-05-19 (OSS-7): wired from ProjectsPage so the tree can offer an
  // Archive action per project. The handler is expected to run the
  // existing project archive flow (with its own confirm + toast). Tree
  // is the only project list today, so without this users had no way to
  // archive projects from the UI.
  onArchive?: (projectId: string, projectName: string) => void;
  // Export a project as a JSON bundle. Wired from ProjectsPage's
  // handleExport, which owns the API call + blob download + toast. Same
  // per-project delegation shape as onArchive.
  onExport?: (projectId: string, projectName: string) => void;
}

const UNASSIGNED_ID = '__unassigned__';

const FOLDER_EXPANSION_KEY = 'fpulse_folders_expanded';

function readFolderExpansion(): Set<string> {
  try {
    const raw = localStorage.getItem(FOLDER_EXPANSION_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    return new Set(Array.isArray(parsed) ? parsed : []);
  } catch {
    return new Set();
  }
}

function writeFolderExpansion(set: Set<string>) {
  try {
    localStorage.setItem(FOLDER_EXPANSION_KEY, JSON.stringify(Array.from(set)));
  } catch {
    /* ignore quota errors */
  }
}

function relativeTime(iso?: string | null): string {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (!t || Number.isNaN(t)) return '';
  const diff = Date.now() - t;
  if (diff < 0) return 'just now';
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return 'just now';
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day}d ago`;
  const mo = Math.floor(day / 30);
  if (mo < 12) return `${mo}mo ago`;
  const yr = Math.floor(mo / 12);
  return `${yr}y ago`;
}

export default function ProjectFolderTree({
  onSelectProject, onSelectPipeline, canEdit = true,
  filteredProjectIds = null,
  folderFilter = null,
  onFolderFilterChange,
  groupBy = 'none',
  groupValueResolver,
  pinnedIds,
  onTogglePin,
  onArchive,
  onExport,
}: Props) {
  const [tree, setTree] = useState<ProjectNode[]>([]);
  const [allWorkflows, setAllWorkflows] = useState<WorkflowItem[]>([]);
  const [foldersByProject, setFoldersByProject] = useState<Record<string, FolderNode[]>>({});
  const [loading, setLoading] = useState(true);
  // Projects default to collapsed every page load. Users expand intentionally;
  // expansion is intentionally NOT persisted so the tree never shows a
  // pre-expanded project that hasn't actually fetched its folders yet —
  // which used to leave "Loading folders…" stuck because loadFolders only
  // fires on user-click toggle.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // Per-folder expansion (separate from project expansion). Sub-folders
  // start collapsed so the tree opens shallow and the user can drill in
  // intentionally. Persisted across sessions.
  const [folderExpanded, setFolderExpanded] = useState<Set<string>>(() => readFolderExpansion());
  const toggleFolderExpand = useCallback((folderId: string) => {
    setFolderExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(folderId)) next.delete(folderId);
      else next.add(folderId);
      writeFolderExpansion(next);
      return next;
    });
  }, []);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const [treeData, workflows] = await Promise.all([
        api.projectTree(),
        api.listWorkflows(),
      ]);
      setTree(Array.isArray(treeData) ? treeData : []);
      setAllWorkflows(Array.isArray(workflows) ? workflows : []);
    } catch (err: any) {
      toast.error('Failed to load projects', err.message);
      setTree([]);
      setAllWorkflows([]);
    }
    setLoading(false);
  }, []);

  useEffect(() => { reload(); }, [reload]);

  // Folders are loaded lazily on first expand so an instance with 50
  // projects doesn't fan out into 50 folder requests on page load.
  // 2026-05-21: when a `folderFilter` lands, we eagerly fetch the owning
  // project's folders below so the filtered view doesn't sit stuck on
  // "Loading folders…".
  const loadFolders = useCallback(async (projectId: string) => {
    if (projectId === UNASSIGNED_ID) return;
    if (foldersByProject[projectId]) return;
    try {
      const folders = await api.listFolders(projectId);
      setFoldersByProject(prev => ({ ...prev, [projectId]: Array.isArray(folders) ? folders : [] }));
    } catch (err: any) {
      toast.error('Failed to load folders', err.message);
      setFoldersByProject(prev => ({ ...prev, [projectId]: [] }));
    }
  }, [foldersByProject]);

  const toggleExpand = useCallback(async (projectId: string) => {
    const next = new Set(expanded);
    if (next.has(projectId)) next.delete(projectId);
    else {
      next.add(projectId);
      await loadFolders(projectId);
    }
    setExpanded(next);
  }, [expanded, loadFolders]);

  // 2026-05-21: resolve the owning project for the active folder filter so
  // we can both (a) hide non-matching projects from the tree and (b) decide
  // which workflows are in-scope.
  const folderFilterProjectId = useMemo(() => {
    if (!folderFilter) return null;
    if (folderFilter.startsWith('unfiled:')) {
      return folderFilter.slice('unfiled:'.length) || null;
    }
    for (const [pid, folders] of Object.entries(foldersByProject)) {
      if (folders.some(f => f.id === folderFilter)) return pid;
    }
    return null;
  }, [folderFilter, foldersByProject]);

  // When the filter changes, auto-expand the owning project + the folder
  // itself so the user lands on the in-scope rows without an extra click.
  useEffect(() => {
    if (!folderFilter || !folderFilterProjectId) return;
    setExpanded(prev => {
      if (prev.has(folderFilterProjectId)) return prev;
      const next = new Set(prev);
      next.add(folderFilterProjectId);
      return next;
    });
    void loadFolders(folderFilterProjectId);
    if (!folderFilter.startsWith('unfiled:')) {
      setFolderExpanded(prev => {
        if (prev.has(folderFilter)) return prev;
        const next = new Set(prev);
        next.add(folderFilter);
        writeFolderExpansion(next);
        return next;
      });
    }
  }, [folderFilter, folderFilterProjectId, loadFolders]);

  const createFolder = useCallback(async (projectId: string, parent: FolderNode | null = null) => {
    const name = await uiPrompt({
      title: parent ? `New sub-folder in "${parent.name}"` : 'New folder',
      message: parent
        ? 'Sub-folders nest inside an existing folder for finer grouping.'
        : 'Folders group pipelines inside a project. Typical buckets: Ingestion, Transforms, Reports, Archive.',
      placeholder: parent ? `e.g. ${parent.name} / Phase 1` : 'e.g. Ingestion, Reports, Archive',
      confirmLabel: parent ? 'Create sub-folder' : 'Create folder',
    });
    if (!name || !name.trim()) return;
    try {
      await api.createFolder({
        name: name.trim(),
        project_id: projectId,
        parent_folder_id: parent?.id ?? null,
      });
      toast.success(parent ? 'Sub-folder created' : 'Folder created', name.trim());
      // Refetch folders for this project + workflows (count may have shifted).
      setFoldersByProject(prev => { const next = { ...prev }; delete next[projectId]; return next; });
      await loadFolders(projectId);
    } catch (err: any) {
      toast.error('Could not create folder', err.message);
    }
  }, [loadFolders]);

  const deleteFolder = useCallback(async (folder: FolderNode) => {
    const confirmed = await uiConfirm({
      title: `Delete folder "${folder.name}"?`,
      message: 'Every pipeline inside this folder will be permanently deleted. This cannot be undone.',
      danger: true,
      confirmLabel: 'Delete folder + pipelines',
    });
    if (!confirmed) return;
    try {
      const result: any = await api.deleteFolder(folder.id);
      const removed = result?.deleted_workflow_count || 0;
      toast.success(
        'Folder deleted',
        removed === 0 ? folder.name : `Removed "${folder.name}" and ${removed} pipeline${removed === 1 ? '' : 's'}`,
      );
      setFoldersByProject(prev => { const next = { ...prev }; delete next[folder.project_id]; return next; });
      await Promise.all([loadFolders(folder.project_id), reload()]);
    } catch (err: any) {
      toast.error('Failed', err.message);
    }
  }, [loadFolders, reload]);

  // Flatten the visible project ids so we can detect orphan pipelines —
  // workflows whose project_id is null, "default" with no Default project,
  // or points to a project the caller can't see via ACL.
  const visibleProjectIds = useMemo(() => {
    const ids = new Set<string>();
    const walk = (nodes: ProjectNode[]) => {
      for (const n of nodes) {
        ids.add(n.id);
        if (n.children?.length) walk(n.children);
      }
    };
    walk(tree);
    return ids;
  }, [tree]);

  const orphanWorkflows = useMemo(
    () => allWorkflows.filter(w => !w.project_id || !visibleProjectIds.has(w.project_id)),
    [allWorkflows, visibleProjectIds],
  );

  // Apply page-side filters (search + dept/owner/priority) by dropping
  // projects whose id isn't in the allow-set. Then sort pinned to top,
  // then bucket under group headers when groupBy is set. These hooks
  // must live above the early returns below — Rules of Hooks require
  // identical hook call order on every render.
  const visibleTree = useMemo(() => {
    let out = tree;
    if (filteredProjectIds) {
      out = out.filter(p => filteredProjectIds.has(p.id));
    }
    // 2026-05-21: folder-scope filter trumps project filters — when the
    // user picks a specific folder (or "(unfiled)" bucket), only its
    // owning project is rendered. The chip near the page heading carries
    // the escape hatch.
    if (folderFilterProjectId) {
      out = out.filter(p => p.id === folderFilterProjectId);
    }
    return out;
  }, [tree, filteredProjectIds, folderFilterProjectId]);

  // Workflows currently in scope under the folder filter. The tree already
  // re-runs `w.folder_id === folder.id` per folder, but when "(unfiled)"
  // is selected we need to project the root-pipeline list and drop folder
  // contents entirely.
  const scopedWorkflows = useMemo(() => {
    if (!folderFilter) return allWorkflows;
    if (folderFilter.startsWith('unfiled:')) {
      const pid = folderFilter.slice('unfiled:'.length);
      return allWorkflows.filter(w => w.project_id === pid && !w.folder_id);
    }
    return allWorkflows.filter(w => w.folder_id === folderFilter);
  }, [allWorkflows, folderFilter]);

  const sortedTree = useMemo(() => {
    const pin = pinnedIds ?? new Set<string>();
    return [...visibleTree].sort((a, b) => {
      const pa = pin.has(a.id) ? 0 : 1;
      const pb = pin.has(b.id) ? 0 : 1;
      if (pa !== pb) return pa - pb;
      return 0; // preserve original order within each bucket
    });
  }, [visibleTree, pinnedIds]);

  const groupedTree = useMemo(() => {
    if (groupBy === 'none' || !groupValueResolver) {
      return [{ key: '', label: '', projects: sortedTree }];
    }
    const buckets = new Map<string, ProjectNode[]>();
    for (const p of sortedTree) {
      const v = groupValueResolver(p) || '—';
      const arr = buckets.get(v);
      if (arr) arr.push(p);
      else buckets.set(v, [p]);
    }
    return Array.from(buckets.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([key, projects]) => ({ key, label: key, projects }));
  }, [sortedTree, groupBy, groupValueResolver]);

  const filteredAwayAll = filteredProjectIds !== null && visibleTree.length === 0;

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="w-6 h-6 border-2 border-indigo-300 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (tree.length === 0 && orphanWorkflows.length === 0) {
    return (
      <div className="text-center py-12 text-sm text-slate-500 bg-white rounded-lg border border-slate-200">
        <p className="mb-1">No projects yet.</p>
        <p className="text-sm text-slate-400">
          Click <b>New Project</b> above to create one — you'll then be able to add folders inside it.
        </p>
      </div>
    );
  }

  const renderCard = (project: ProjectNode) => {
    // 2026-05-21: when a folder filter is active, hide every folder in
    // every other project so the card only renders the in-scope folder.
    // For the "(unfiled)" bucket we hide ALL folders so the project's
    // root-pipeline list is the only thing visible inside the card.
    const projectFolders = foldersByProject[project.id];
    const scopedFolders = (() => {
      if (!folderFilter || !projectFolders) return projectFolders;
      if (folderFilter.startsWith('unfiled:')) {
        // Unfiled scope means: no folders shown, only root pipelines.
        return [];
      }
      return projectFolders.filter(f => f.id === folderFilter);
    })();
    // Workflows passed in are already restricted to this project; scope
    // them further by the folder filter so we don't render anything
    // outside the chip's stated scope.
    const cardWorkflows = scopedWorkflows.filter(w => w.project_id === project.id);
    return (
      <ProjectCard
        key={project.id}
        project={project}
        workflows={cardWorkflows}
        folders={scopedFolders}
        expanded={expanded.has(project.id)}
        onToggle={() => toggleExpand(project.id)}
        onOpen={() => onSelectProject(project.id, project.name)}
        onSelectPipeline={onSelectPipeline}
        onCreateFolder={(parent) => createFolder(project.id, parent ?? null)}
        onDeleteFolder={deleteFolder}
        canEdit={canEdit}
        pinned={pinnedIds?.has(project.id) ?? false}
        onTogglePin={onTogglePin ? () => onTogglePin(project.id) : undefined}
        onArchive={onArchive ? () => onArchive(project.id, project.name) : undefined}
        onExport={onExport ? () => onExport(project.id, project.name) : undefined}
        folderExpanded={folderExpanded}
        onToggleFolder={toggleFolderExpand}
        folderFilter={folderFilter}
        onFolderFilterChange={onFolderFilterChange}
      />
    );
  };

  return (
    <div className="space-y-3">
      {filteredAwayAll && (
        <div className="text-center py-8 text-sm text-slate-500 bg-white rounded-lg border border-dashed border-slate-200">
          No projects match your filters. Clear the search or filter chips to see everything.
        </div>
      )}
      {groupedTree.map(group => (
        <div key={group.key || '__nogroup__'} className="space-y-3">
          {group.label && (
            <div className="flex items-center gap-2 px-1 pt-2">
              <span className="text-xs font-semibold tracking-wider text-slate-500 uppercase">
                {group.label}
              </span>
              <span className="text-xs text-slate-400">
                {group.projects.length} project{group.projects.length === 1 ? '' : 's'}
              </span>
              <div className="flex-1 border-t border-slate-200" />
            </div>
          )}
          {group.projects.map(renderCard)}
        </div>
      ))}

      {orphanWorkflows.length > 0 && (
        <ProjectCard
          project={{
            id: UNASSIGNED_ID,
            name: 'Unassigned',
            description: "Pipelines that aren't attached to a project. Open one and pick a project to file it away.",
            color: '#94a3b8',
            pipeline_count: orphanWorkflows.length,
          }}
          workflows={orphanWorkflows}
          folders={[]}
          expanded={expanded.has(UNASSIGNED_ID)}
          onToggle={() => toggleExpand(UNASSIGNED_ID)}
          onOpen={() => onSelectProject(UNASSIGNED_ID, 'Unassigned')}
          onSelectPipeline={onSelectPipeline}
          onCreateFolder={() => { /* no folders inside Unassigned */ }}
          onDeleteFolder={() => { /* not reachable */ }}
          canEdit={false}
          unassigned
        />
      )}
    </div>
  );
}

interface CardProps {
  project: ProjectNode;
  workflows: WorkflowItem[];
  folders: FolderNode[] | undefined;
  expanded: boolean;
  onToggle: () => void;
  onOpen: () => void;
  onSelectPipeline?: (id: string, name?: string) => void;
  onCreateFolder: (parent?: FolderNode | null) => void;
  onDeleteFolder: (folder: FolderNode) => void;
  canEdit: boolean;
  unassigned?: boolean;
  pinned?: boolean;
  onTogglePin?: () => void;
  // 2026-05-19 (OSS-7) — see Props.onArchive for the rationale.
  onArchive?: () => void;
  // Export this project as a JSON bundle — see Props.onExport.
  onExport?: () => void;
  folderExpanded?: Set<string>;
  onToggleFolder?: (folderId: string) => void;
  // 2026-05-21: folder-scope filter shared with the parent page. The card
  // forwards both into FolderSection so the per-folder "Filter" button
  // can write back to the page chip + dropdown.
  folderFilter?: string | null;
  onFolderFilterChange?: (next: string) => void;
}

function ProjectCard({
  project, workflows, folders, expanded,
  onToggle, onOpen, onSelectPipeline,
  onCreateFolder, onDeleteFolder, canEdit, unassigned,
  pinned, onTogglePin, onArchive, onExport,
  folderExpanded, onToggleFolder,
  folderFilter, onFolderFilterChange,
}: CardProps) {
  const accent = project.color || '#6366f1';
  const folderCount = folders?.length ?? 0;
  const pipelineCount = project.pipeline_count ?? workflows.length;
  const updated = relativeTime(project.updated_at);

  // Pipelines that live directly under the project (no folder).
  const rootWorkflows = workflows.filter(w => !w.folder_id);

  // Project-level dashboard chips — cheap rollups computed from the
  // workflow list this component already loads. Avoids per-project
  // network calls. Last run is the most recent successful or failed
  // run across all pipelines in the project.
  const scheduledCount = workflows.filter(w => w.schedule_cron).length;
  const failedCount = workflows.filter(w => w.status === 'failed').length;
  const lastRunRel = (() => {
    let latest = 0;
    for (const w of workflows) {
      const t = w.last_run_at ? new Date(w.last_run_at).getTime() : 0;
      if (t > latest) latest = t;
    }
    if (!latest) return '';
    return relativeTime(new Date(latest).toISOString());
  })();

  // Build the nested folder tree from the flat list. Sub-folders render
  // indented under their parent instead of as siblings.
  const folderTree = useMemo(
    () => (folders ? buildFolderTree(folders) : []),
    [folders],
  );

  return (
    <div
      className="bg-white rounded-lg border border-slate-200 shadow-sm overflow-hidden"
    >
      <div className="flex">
        <div className="w-1 shrink-0" style={{ background: accent }} aria-hidden />
        <div className="flex-1 min-w-0">
          {/* Header — clickable expands; primary action sits on the right. */}
          <div className="flex items-center gap-3 px-4 py-3 hover:bg-slate-50/60">
            <button
              onClick={onToggle}
              className="w-5 h-5 flex items-center justify-center rounded hover:bg-slate-200 shrink-0"
              aria-label={expanded ? 'Collapse' : 'Expand'}
              aria-expanded={expanded}
            >
              <svg
                width="12" height="12" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" strokeWidth="2.5"
                className={`transition-transform ${expanded ? 'rotate-90' : ''}`}
              >
                <polyline points="9 18 15 12 9 6" />
              </svg>
            </button>

            <span
              className="w-8 h-8 rounded-md flex items-center justify-center shrink-0"
              style={{ background: `${accent}1a`, color: accent }}
              aria-hidden
            >
              <ProjectGlyph icon={project.icon} unassigned={unassigned} />
            </span>

            <button
              onClick={onToggle}
              className="text-sm font-semibold text-slate-800 hover:text-indigo-600 truncate text-left"
              title={project.description || project.name}
            >
              {project.name}
            </button>

            <span className="text-sm text-slate-500 truncate">
              {pipelineCount} pipeline{pipelineCount === 1 ? '' : 's'}
              {folderCount > 0 && ` · ${folderCount} folder${folderCount === 1 ? '' : 's'}`}
              {updated && ` · Updated ${updated}`}
            </span>

            {/* Inline dashboard chips — pre-computed rollups so the user
                doesn't have to open each project to see basic health.
                Hidden on the Unassigned bucket where they'd be noise. */}
            {!unassigned && (scheduledCount > 0 || failedCount > 0 || lastRunRel || (project.storage && (project.storage.file_count + project.storage.table_count + project.storage.output_count) > 0)) && (
              <div className="hidden md:flex items-center gap-1.5 shrink-0">
                {scheduledCount > 0 && (
                  <span
                    className="text-xs px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-100 flex items-center gap-1"
                    title={`${scheduledCount} scheduled pipeline${scheduledCount === 1 ? '' : 's'}`}
                  >
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>
                    {scheduledCount}
                  </span>
                )}
                {failedCount > 0 && (
                  <span
                    className="text-xs px-2 py-0.5 rounded-full bg-rose-50 text-rose-700 border border-rose-100"
                    title={`${failedCount} pipeline${failedCount === 1 ? '' : 's'} in failed state`}
                  >
                    {failedCount} failed
                  </span>
                )}
                {/* 2026-05-25 — Storage rollup chip. Counts come from the
                    project list endpoint via the StorageRollup join.
                    Tooltip breaks the total down by kind. */}
                {project.storage && (project.storage.file_count + project.storage.table_count + project.storage.output_count) > 0 && (
                  <span
                    className="text-xs px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-100 flex items-center gap-1"
                    title={`${project.storage.file_count} file${project.storage.file_count === 1 ? '' : 's'} · ${project.storage.table_count} table${project.storage.table_count === 1 ? '' : 's'} · ${project.storage.output_count} output${project.storage.output_count === 1 ? '' : 's'}`}
                  >
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4"><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v6c0 1.66 4.03 3 9 3s9-1.34 9-3V5" /><path d="M3 11v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6" /></svg>
                    {project.storage.file_count + project.storage.table_count + project.storage.output_count}
                  </span>
                )}
                {lastRunRel && (
                  <span
                    className="text-xs px-2 py-0.5 rounded-full bg-slate-50 text-slate-600 border border-slate-200"
                    title="Most recent run across pipelines in this project"
                  >
                    Last run {lastRunRel}
                  </span>
                )}
              </div>
            )}

            <div className="ml-auto flex items-center gap-1 shrink-0">
              {onTogglePin && !unassigned && (
                <button
                  onClick={onTogglePin}
                  title={pinned ? 'Unpin from top of list' : 'Pin to top of list'}
                  aria-label={pinned ? 'Unpin project' : 'Pin project'}
                  aria-pressed={pinned}
                  className={`w-7 h-7 rounded flex items-center justify-center transition-colors ${
                    pinned ? 'text-amber-500 hover:text-amber-600 hover:bg-amber-50' : 'text-slate-300 hover:text-amber-500 hover:bg-amber-50'
                  }`}
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill={pinned ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
                  </svg>
                </button>
              )}
              {canEdit && !unassigned && (
                <button
                  // onCreateFolder takes an optional parent FolderNode; a
                  // bare click on this top-level "Add folder" button
                  // means "no parent" (root of this project). Wrap so
                  // React's MouseEvent doesn't shadow the parent arg.
                  onClick={() => onCreateFolder()}
                  title={`Add a folder inside "${project.name}".`}
                  className="text-sm font-medium text-slate-600 hover:text-slate-900 px-2 py-1 rounded hover:bg-slate-100 flex items-center gap-1"
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                    <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
                    <line x1="12" y1="11" x2="12" y2="17" />
                    <line x1="9" y1="14" x2="15" y2="14" />
                  </svg>
                  Folder
                </button>
              )}
              {/* Export Project — download a JSON bundle of the project.
                  Read-only action (no canEdit gate), so anyone who can see
                  the project can export it. Hidden on the Unassigned bucket
                  because it isn't a real project. */}
              {!unassigned && onExport && (
                <button
                  onClick={onExport}
                  title={`Export "${project.name}" as a JSON bundle`}
                  aria-label={`Export project ${project.name}`}
                  className="w-7 h-7 rounded flex items-center justify-center text-slate-400 hover:text-indigo-600 hover:bg-indigo-50 transition-colors"
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                    <polyline points="7 10 12 15 17 10" />
                    <line x1="12" y1="15" x2="12" y2="3" />
                  </svg>
                </button>
              )}
              {/* 2026-05-19 (OSS-7 of PAGE_BY_PAGE_AUDIT.md) — Archive
                  action. Wired from ProjectsPage's `handleArchive`, which
                  owns the confirm + retention copy + toast. Hidden on the
                  Unassigned bucket because it isn't a real project. */}
              {canEdit && !unassigned && onArchive && (
                <button
                  onClick={onArchive}
                  title={`Archive "${project.name}" (move to Archived for 30 days, then auto-delete)`}
                  aria-label={`Archive project ${project.name}`}
                  className="w-7 h-7 rounded flex items-center justify-center text-slate-400 hover:text-amber-600 hover:bg-amber-50 transition-colors"
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="21 8 21 21 3 21 3 8" />
                    <rect x="1" y="3" width="22" height="5" />
                    <line x1="10" y1="12" x2="14" y2="12" />
                  </svg>
                </button>
              )}
              <button
                onClick={onOpen}
                className="text-sm font-semibold text-indigo-600 hover:text-indigo-700 px-3 py-1.5 rounded-md hover:bg-indigo-50 flex items-center gap-1"
                title={`Go to the Pipelines page filtered to "${project.name}".`}
              >
                View pipelines
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <polyline points="9 18 15 12 9 6" />
                </svg>
              </button>
            </div>
          </div>

          {expanded && (
            <div className="border-t border-slate-100">
              {project.description && (
                <p className="px-4 pt-3 text-sm text-slate-500">{project.description}</p>
              )}

              {folders === undefined && !unassigned ? (
                <div className="px-4 py-3 text-sm text-slate-500">Loading folders…</div>
              ) : (
                <>
                  {folderTree.length > 0 && folderTree.map(node => (
                    <FolderSection
                      key={node.folder.id}
                      node={node}
                      depth={0}
                      workflows={workflows}
                      canEdit={canEdit}
                      onDelete={onDeleteFolder}
                      onCreateSubfolder={onCreateFolder}
                      onSelectPipeline={onSelectPipeline}
                      expandedFolders={folderExpanded}
                      onToggleFolder={onToggleFolder}
                      folderFilter={folderFilter}
                      onFolderFilterChange={onFolderFilterChange}
                    />
                  ))}

                  {rootWorkflows.length > 0 && (
                    <PipelineGroup
                      label={folders && folders.length > 0 ? 'Unfiled' : undefined}
                      workflows={rootWorkflows}
                      onSelectPipeline={onSelectPipeline}
                    />
                  )}

                  {pipelineCount === 0 && folderCount === 0 && !unassigned && (
                    <div className="px-4 py-5 text-sm">
                      <div className="rounded border border-dashed border-slate-300 bg-slate-50 px-3 py-3">
                        <div className="text-slate-700 font-medium mb-1">This project has no pipelines yet.</div>
                        <p className="text-slate-500 leading-relaxed">
                          Open the project to create a pipeline, or add a folder to group pipelines by purpose
                          (e.g.&nbsp;<span className="font-mono text-slate-700">Ingestion</span>,&nbsp;
                          <span className="font-mono text-slate-700">Reports</span>).
                        </p>
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

interface FolderSectionProps {
  node: FolderTreeNode;
  depth: number;
  workflows: WorkflowItem[];
  canEdit: boolean;
  onDelete: (folder: FolderNode) => void;
  onCreateSubfolder: (parent: FolderNode) => void;
  onSelectPipeline?: (id: string, name?: string) => void;
  expandedFolders?: Set<string>;
  onToggleFolder?: (folderId: string) => void;
  // 2026-05-21: folder-scope filter shared with the parent page. When the
  // user clicks "Filter to this folder" on the row, the section flips the
  // page-level chip + dropdown via onFolderFilterChange.
  folderFilter?: string | null;
  onFolderFilterChange?: (next: string) => void;
}

function FolderSection({
  node, depth, workflows, canEdit,
  onDelete, onCreateSubfolder, onSelectPipeline,
  expandedFolders, onToggleFolder,
  folderFilter, onFolderFilterChange,
}: FolderSectionProps) {
  const { folder, children } = node;
  const folderWorkflows = workflows.filter(w => w.folder_id === folder.id);
  // Sub-folder indent: 16px base + 18px per depth level.
  const indent = 16 + depth * 18;
  // Folders start collapsed unless explicitly opened by the user; mirror
  // the project card chevron behaviour so the tree opens shallow and
  // gets deeper only on intent. A leaf folder has nothing to expand.
  const hasContent = folderWorkflows.length > 0 || children.length > 0;
  const isExpanded = (expandedFolders?.has(folder.id) ?? false) && hasContent;
  // Count every descendant pipeline / sub-folder so a parent's badge
  // reflects the whole sub-tree, not just its own immediate contents.
  const descendantCounts = (() => {
    let pipelines = folderWorkflows.length;
    let subFolders = children.length;
    const walk = (nodes: FolderTreeNode[]) => {
      for (const n of nodes) {
        pipelines += workflows.filter(w => w.folder_id === n.folder.id).length;
        subFolders += n.children.length;
        walk(n.children);
      }
    };
    walk(children);
    return { pipelines, subFolders };
  })();
  const isEmpty = folderWorkflows.length === 0 && children.length === 0;
  const accent = folder.color || '#64748b';
  return (
    <div className="border-t border-slate-100 group/folder">
      {/* Structured folder row: icon · name · smart chips · actions.
          Matches the project card layout so a folder reads as a smaller
          peer, not a stray line of text. */}
      <div className="flex items-center gap-2 pr-4 py-2.5" style={{ paddingLeft: indent }}>
        {/* Chevron toggle. Leaf folders show a spacer so the icon column
            stays vertically aligned with their content-having siblings. */}
        {hasContent && onToggleFolder ? (
          <button
            onClick={() => onToggleFolder(folder.id)}
            className="w-5 h-5 flex items-center justify-center rounded hover:bg-slate-200 shrink-0"
            aria-label={isExpanded ? 'Collapse folder' : 'Expand folder'}
            aria-expanded={isExpanded}
            title={isExpanded ? 'Collapse' : 'Expand'}
          >
            <svg
              width="11" height="11" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" strokeWidth="2.5"
              className={`transition-transform ${isExpanded ? 'rotate-90' : ''}`}
            >
              <polyline points="9 18 15 12 9 6" />
            </svg>
          </button>
        ) : (
          <span className="w-5 h-5 shrink-0" aria-hidden />
        )}
        <span
          className="w-6 h-6 rounded-md flex items-center justify-center shrink-0"
          style={{ background: `${accent}1a`, color: accent }}
          aria-hidden
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
          </svg>
        </span>
        {hasContent && onToggleFolder ? (
          <button
            onClick={() => onToggleFolder(folder.id)}
            className="text-sm font-semibold text-slate-700 hover:text-indigo-600 truncate text-left"
            title={folder.name}
          >
            {folder.name}
          </button>
        ) : (
          <span
            className="text-sm font-semibold text-slate-700 truncate"
            title={folder.name}
          >
            {folder.name}
          </span>
        )}
        {/* Smart chips: drop zero counts so leaf folders read clean.
            Direct counts use solid pills; sub-tree totals appear in a
            lighter tone when they differ from the immediate counts. */}
        <div className="flex items-center gap-1.5 shrink-0">
          {folderWorkflows.length > 0 && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-100">
              {folderWorkflows.length} pipeline{folderWorkflows.length === 1 ? '' : 's'}
            </span>
          )}
          {children.length > 0 && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-violet-50 text-violet-700 border border-violet-100">
              {children.length} sub-folder{children.length === 1 ? '' : 's'}
            </span>
          )}
          {(descendantCounts.pipelines > folderWorkflows.length || descendantCounts.subFolders > children.length) && (
            <span
              className="text-xs px-2 py-0.5 rounded-full bg-slate-50 text-slate-600 border border-slate-200"
              title={`Across the whole sub-tree: ${descendantCounts.pipelines} pipeline${descendantCounts.pipelines === 1 ? '' : 's'}, ${descendantCounts.subFolders} sub-folder${descendantCounts.subFolders === 1 ? '' : 's'}`}
            >
              Sub-tree {descendantCounts.pipelines}/{descendantCounts.subFolders}
            </span>
          )}
          {isEmpty && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-slate-100 text-slate-500 border border-slate-200 italic">
              Empty
            </span>
          )}
        </div>
        {/* Action group — split into read-only (Filter) + mutating (Sub-folder, Delete).
            Filter shows regardless of canEdit since it's a view-only action,
            the others gate on edit permission as before. */}
        <div className="ml-auto flex items-center gap-1 opacity-0 group-hover/folder:opacity-100 transition-opacity">
          {/* 2026-05-21: per-folder shortcut into the page-level folder filter. */}
          {onFolderFilterChange && folderFilter !== folder.id && (
            <button
              onClick={() => onFolderFilterChange(folder.id)}
              title={`Scope the Projects view to only "${folder.name}" and its pipelines.`}
              className="text-sm text-slate-500 hover:text-indigo-600 px-2 py-1 rounded hover:bg-indigo-50 inline-flex items-center gap-1"
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
              </svg>
              Filter
            </button>
          )}
          {canEdit && (
            <>
              {/* OSS limits folder nesting to one level (see backend/fpulse/api/folders.py).
                  Only show the "+ Sub-folder" affordance on top-level folders;
                  showing it on an already-nested folder promises an action the
                  backend will reject with "Sub-folders are not supported" toast. */}
              {!folder.parent_folder_id && (
                <button
                  onClick={() => onCreateSubfolder(folder)}
                  title={`Add a sub-folder inside "${folder.name}".`}
                  className="text-sm text-slate-500 hover:text-indigo-600 px-2 py-1 rounded hover:bg-indigo-50"
                >
                  + Sub-folder
                </button>
              )}
              <button
                onClick={() => onDelete(folder)}
                title="Delete this folder and every pipeline inside it. This cannot be undone."
                className="text-sm text-slate-400 hover:text-red-600 px-2 py-1 rounded hover:bg-red-50"
              >
                Delete
              </button>
            </>
          )}
        </div>
      </div>
      {isExpanded && folderWorkflows.length > 0 && (
        <ul className="pb-1">
          {folderWorkflows.map(wf => (
            <PipelineRow key={wf.id} workflow={wf} onSelect={onSelectPipeline} indent={indent + 28} />
          ))}
        </ul>
      )}
      {isExpanded && children.map(child => (
        <FolderSection
          key={child.folder.id}
          node={child}
          depth={depth + 1}
          workflows={workflows}
          canEdit={canEdit}
          onDelete={onDelete}
          onCreateSubfolder={onCreateSubfolder}
          onSelectPipeline={onSelectPipeline}
          expandedFolders={expandedFolders}
          onToggleFolder={onToggleFolder}
          folderFilter={folderFilter}
          onFolderFilterChange={onFolderFilterChange}
        />
      ))}
    </div>
  );
}

interface PipelineGroupProps {
  label?: string;
  workflows: WorkflowItem[];
  onSelectPipeline?: (id: string, name?: string) => void;
}

function PipelineGroup({ label, workflows, onSelectPipeline }: PipelineGroupProps) {
  return (
    <div className="border-t border-slate-100">
      {label && (
        <div className="flex items-baseline gap-2 px-4 pt-3 pb-1">
          <span className="text-xs font-semibold tracking-wider text-slate-500 uppercase">
            {label}
          </span>
          <span className="text-sm text-slate-400">
            {workflows.length} pipeline{workflows.length === 1 ? '' : 's'}
          </span>
        </div>
      )}
      <ul className="py-1">
        {workflows.map(wf => (
          <PipelineRow key={wf.id} workflow={wf} onSelect={onSelectPipeline} />
        ))}
      </ul>
    </div>
  );
}

interface RowProps {
  workflow: WorkflowItem;
  onSelect?: (id: string, name?: string) => void;
  indent?: number;
}

function PipelineRow({ workflow, onSelect, indent }: RowProps) {
  const lastRun = relativeTime(workflow.last_run_at);
  return (
    <li>
      <button
        onClick={() => onSelect?.(workflow.id, workflow.name)}
        className="w-full flex items-center gap-3 pr-4 py-1.5 hover:bg-slate-50 text-left"
        style={{ paddingLeft: indent ?? 16 }}
      >
        <StatusDot status={workflow.status} />
        <span className="text-sm text-slate-700 truncate group-hover:text-indigo-600">
          {workflow.name}
        </span>
        <div className="ml-auto flex items-center gap-3 shrink-0 text-sm text-slate-400">
          {workflow.schedule_cron && (
            <span title={`Cron: ${workflow.schedule_cron}`} className="flex items-center gap-1">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                <circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" />
              </svg>
              Scheduled
            </span>
          )}
          {lastRun && <span>Last run {lastRun}</span>}
          {workflow.status && workflow.status !== 'draft' && <StatusLabel status={workflow.status} />}
        </div>
      </button>
    </li>
  );
}

function StatusDot({ status }: { status?: string }) {
  const color =
    status === 'active' ? 'bg-emerald-500'
    : status === 'paused' ? 'bg-amber-500'
    : status === 'failed' ? 'bg-rose-500'
    : 'bg-slate-300';
  const label =
    status === 'active' ? 'Active'
    : status === 'paused' ? 'Paused'
    : status === 'failed' ? 'Failed'
    : 'Draft';
  return (
    <span
      className={`w-2 h-2 rounded-full shrink-0 ${color}`}
      title={label}
      aria-label={label}
    />
  );
}

function StatusLabel({ status }: { status: string }) {
  const cls =
    status === 'active' ? 'text-emerald-600'
    : status === 'paused' ? 'text-amber-600'
    : status === 'failed' ? 'text-rose-600'
    : 'text-slate-400';
  return <span className={cls}>{status}</span>;
}

function ProjectGlyph({ icon, unassigned }: { icon?: string; unassigned?: boolean }) {
  if (unassigned) {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
      </svg>
    );
  }
  // Project's stored icon id maps to a small SVG. Default = folder.
  switch (icon) {
    case 'database':
      return (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <ellipse cx="12" cy="5" rx="9" ry="3" />
          <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
          <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
        </svg>
      );
    case 'chart':
      return (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <line x1="18" y1="20" x2="18" y2="10" /><line x1="12" y1="20" x2="12" y2="4" /><line x1="6" y1="20" x2="6" y2="14" />
        </svg>
      );
    case 'rocket':
      return (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z" />
          <path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" />
        </svg>
      );
    case 'briefcase':
      return (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <rect x="2" y="7" width="20" height="14" rx="2" />
          <path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" />
        </svg>
      );
    case 'pulse':
      return (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
        </svg>
      );
    case 'folder':
    default:
      return (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
        </svg>
      );
  }
}
