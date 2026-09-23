import { useState, useEffect, useMemo, useCallback } from 'react';
import { usePageContext } from '../../hooks/usePageContext';
import { api } from '../../api/client';
import { toast } from '../Toast';
import ReadOnlyBanner from '../../auth/ReadOnlyBanner';
import { useCan } from '../../auth/RoleGate';
import { canManageProjects } from '../../auth/permissions';
import TableToolbar, { useTableColumns, TColumn, TColumnGroup } from '../shared/TableToolbar';
import { uiConfirm } from '../../ui/dialog';
import TierChip from '../shared/TierChip';
import PageHeader from '../shared/PageHeader';
import { DelayedSkeleton, SkeletonTableRow } from '../shared/Skeleton';
import HeroCard from '../shared/HeroCard';
import EmptyState from '../shared/EmptyState';
import ProjectFolderTree from '../ProjectFolderTree';

// 2026-05-19 (P2 #1 of PAGE_BY_PAGE_AUDIT.md): `ViewMode` removed along
// with the flat-table render. Tree is the only view; keep the type alias
// commented so a future re-introduction has a place to hook in.
// type ViewMode = 'table' | 'tree';

/* ═══ Column definitions ═══ */
const PROJECT_COLUMNS: TColumn[] = [
  // core
  { key: 'name',        label: 'Project',     default: true,  group: 'core' },
  { key: 'description', label: 'Description', default: true,  group: 'core' },
  { key: 'pipelines',   label: 'Pipelines',   default: true,  group: 'core' },
  { key: 'owner',       label: 'Owner',       default: true,  group: 'core' },
  { key: 'actions',     label: 'Actions',     default: true,  group: 'core' },
  // metadata
  { key: 'department',  label: 'Department',  default: true,  group: 'metadata' },
  { key: 'priority',    label: 'Priority',    default: true,  group: 'metadata' },
  { key: 'team',        label: 'Team',        default: true,  group: 'metadata' },
  { key: 'members',     label: 'Members',     default: false, group: 'metadata' },
  { key: 'cost_center', label: 'Cost Center', default: false, group: 'metadata' },
  { key: 'sponsor',     label: 'Sponsor',     default: false, group: 'metadata' },
  { key: 'tags',        label: 'Tags',        default: false, group: 'metadata' },
  { key: 'notes',       label: 'Notes',       default: false, group: 'metadata' },
  // dates
  { key: 'created',     label: 'Created',     default: false, group: 'dates' },
  { key: 'modified',    label: 'Modified',    default: true,  group: 'dates' },
];

// Icon names map to entries in shared/Icon.tsx — TableToolbar's render
// detects the lowercase-kebab pattern and emits the matching SVG. Plain
// text glyphs (◆ ◇ ⚙ ▶ etc.) used by other pages still render as text.
const PROJECT_COLUMN_GROUPS: TColumnGroup[] = [
  { key: 'core',     label: 'Core',     icon: 'list' },
  { key: 'metadata', label: 'Metadata', icon: 'tag' },
  { key: 'dates',    label: 'Dates',    icon: 'calendar' },
];

const PROJECT_COLORS = [
  '#6366f1', '#3b82f6', '#10b981', '#f59e0b', '#ef4444',
  '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#06b6d4',
];

// Project icon set — line-style SVGs that match the rest of the app's
// iconography (sidebar nav, KPI chips, table actions). Inline SVG keeps
// the bundle small and lets the icons inherit currentColor so they
// recolour with the project tile's accent without a separate asset
// pipeline. Replaces the previous emoji set per the 2026-05-09 review.
const _icon = (path: React.ReactNode) => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    {path}
  </svg>
);
const PROJECT_ICONS: { id: string; label: string; icon: React.ReactNode }[] = [
  { id: 'folder',    label: 'Folder',      icon: _icon(<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />) },
  { id: 'database',  label: 'Database',    icon: _icon(<><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" /><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" /></>) },
  { id: 'chart',     label: 'Analytics',   icon: _icon(<><line x1="18" y1="20" x2="18" y2="10" /><line x1="12" y1="20" x2="12" y2="4" /><line x1="6" y1="20" x2="6" y2="14" /></>) },
  { id: 'gear',      label: 'Engineering', icon: _icon(<><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></>) },
  { id: 'rocket',    label: 'Launch',      icon: _icon(<><path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z" /><path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" /><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0" /><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" /></>) },
  { id: 'star',      label: 'Featured',    icon: _icon(<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />) },
  { id: 'lightning', label: 'Fast',        icon: _icon(<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />) },
  { id: 'globe',     label: 'Global',      icon: _icon(<><circle cx="12" cy="12" r="10" /><line x1="2" y1="12" x2="22" y2="12" /><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" /></>) },
  { id: 'briefcase', label: 'Business',    icon: _icon(<><rect x="2" y="7" width="20" height="14" rx="2" ry="2" /><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" /></>) },
  { id: 'shield',    label: 'Security',    icon: _icon(<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />) },
  { id: 'box',       label: 'Inventory',   icon: _icon(<><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" /><polyline points="3.27 6.96 12 12.01 20.73 6.96" /><line x1="12" y1="22.08" x2="12" y2="12" /></>) },
  { id: 'pulse',     label: 'Activity',    icon: _icon(<polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />) },
];

// 2026-05-22: RETENTION_DAYS constant retired. OSS server-side archive
// keeps archived projects indefinitely until an admin /delete's them.
// (Plus is the right home for a retention scheduler — audit C1.)

interface ProjectMetadata {
  cost_center?: string;
  sponsor?: string;
  department?: string;
  priority?: 'low' | 'medium' | 'high' | 'critical';
  tags?: string[];
  notes?: string;
}

interface Project {
  id: string;
  name: string;
  description: string;
  owner: string;
  owner_id?: string;
  color: string;
  icon: string;
  pipeline_count?: number;
  // 2026-05-25 — Storage rollup attached by GET /api/projects/.
  storage?: {
    file_count: number; file_bytes: number;
    table_count: number; table_bytes: number;
    output_count: number; output_bytes: number;
  };
  created_by?: string;
  team?: string;
  members?: string[];
  metadata?: ProjectMetadata;
  status?: 'active' | 'archived';
  archived_at?: string;
  created_at: string;
  updated_at: string;
}

// 2026-05-22: localStorage-backed archive is being phased out (audit
// C1 / C2). The functions below are kept ONLY for a one-time migration
// of any data that landed in localStorage before the backend lifecycle
// shipped. After migration the keys are deleted and the helpers stop
// being read.
function readLegacyArchivedProjects(): Record<string, { archived_at: string; original: Project }> {
  try { return JSON.parse(localStorage.getItem('fpulse_archived_projects') || '{}'); } catch { return {}; }
}
function clearLegacyArchivedProjects() {
  try { localStorage.removeItem('fpulse_archived_projects'); } catch { /* ignore */ }
}

export default function ProjectsPage({ onSelectProject, environment = 'dev', tier = 'free' }: { onSelectProject: (id: string, name?: string) => void; environment?: 'dev' | 'prod'; tier?: string }) {
  const [projects, setProjects] = useState<Project[]>([]);
  // workspace-wide pipeline total — used by the Total Pipelines stat so
  // orphan pipelines (no project_id, or project the caller can't see)
  // are counted instead of being silently dropped. Sum of project
  // pipeline_count alone undercounts whenever orphans exist.
  const [workspaceWorkflowCount, setWorkspaceWorkflowCount] = useState<number>(0);
  const [loading, setLoading] = useState(true);
  // Project creation is an admin-only concern — we override the generic
  // `create` permission check here because the backend now gates POST
  // /api/projects behind require_admin. A developer still has `create`
  // for workflows inside an existing project, but can't spawn new
  // projects themselves. See `canManageProjects` for the mirrored rule.
  const currentUser = (() => { try { return JSON.parse(localStorage.getItem('fpulse_user') || 'null'); } catch { return null; } })();
  const canCreate = canManageProjects(currentUser);
  const canEdit = useCan('edit', environment) && canManageProjects(currentUser);
  const canDelete = useCan('delete', environment) && canManageProjects(currentUser);
  const [showCreate, setShowCreate] = useState(false);
  const [editProject, setEditProject] = useState<Project | null>(null);
  const [form, setForm] = useState({
    name: '', description: '', color: '#6366f1', icon: 'folder', created_by: '', team: '',
    members: [] as string[],
    metadata: { cost_center: '', sponsor: '', department: '', priority: 'medium', tags: [] as string[], notes: '' } as ProjectMetadata,
  });
  const [allUsers, setAllUsers] = useState<any[]>([]);
  const [tagInput, setTagInput] = useState('');
  const [showArchived, setShowArchived] = useState(false);
  // 2026-05-22: archived projects now come from the backend with
  // `status === "archived"` and an `archived_at` field set server-side.
  // Was previously a localStorage map keyed by project id.
  const [archivedProjects, setArchivedProjects] = useState<Project[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  // Filter chips — populated from the metadata of existing projects so
  // we never offer a value that filters to nothing. Empty string = "all".
  const [filterDepartment, setFilterDepartment] = useState('');
  const [filterPriority, setFilterPriority] = useState('');
  const [filterOwner, setFilterOwner] = useState('');
  // 2026-05-21: sub-folder scope filter. Value space:
  //   ""                      → no folder filter (current default)
  //   "<folderId>"            → only show this folder + its pipelines
  //   "unfiled:<projectId>"   → only show pipelines in <projectId> with no folder
  // Persisted to localStorage so the scope survives a reload — same shape
  // the Group by selection uses for parity.
  const [filterFolder, setFilterFolder] = useState<string>(() => {
    try { return localStorage.getItem('fpulse_projects_folder_filter') || ''; } catch { return ''; }
  });
  useEffect(() => {
    try { localStorage.setItem('fpulse_projects_folder_filter', filterFolder); } catch { /* ignore */ }
  }, [filterFolder]);
  // All folders fetched eagerly per project so the dropdown can render
  // hierarchical paths immediately. Keyed by project id. OSS uses 1-level
  // folders (the backend rejects parent_folder_id on create) so flat is fine.
  const [foldersByProject, setFoldersByProject] = useState<Record<string, Array<{ id: string; name: string; project_id: string }>>>({});
  // Group projects by a metadata field. 'none' = render flat (current
  // behaviour); 'department' / 'owner' / 'priority' = bucket under
  // collapsible headers in the tree.
  type GroupBy = 'none' | 'department' | 'owner' | 'priority';
  const [groupBy, setGroupBy] = useState<GroupBy>(() => {
    try { return (localStorage.getItem('fpulse_projects_groupby') as GroupBy) || 'none'; } catch { return 'none'; }
  });
  useEffect(() => {
    try { localStorage.setItem('fpulse_projects_groupby', groupBy); } catch { /* ignore */ }
  }, [groupBy]);
  // Pinned project IDs — persisted per browser so the user's favourites
  // float to the top of the tree across sessions.
  const [pinnedIds, setPinnedIds] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem('fpulse_projects_pinned');
      const arr = raw ? JSON.parse(raw) : [];
      return new Set(Array.isArray(arr) ? arr : []);
    } catch { return new Set(); }
  });
  const togglePin = useCallback((id: string) => {
    setPinnedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      try { localStorage.setItem('fpulse_projects_pinned', JSON.stringify(Array.from(next))); } catch { /* ignore */ }
      return next;
    });
  }, []);
  // Table view = the classic grid you already had.
  // Tree view = nested project / folder / pipeline browser, with inline
  // "+ Folder" and "+ Sub-folder" actions per node. Persisted so the
  // user's last choice survives reloads.
  // 2026-05-19 (P2 #1 of PAGE_BY_PAGE_AUDIT.md): `viewMode` and the flat-
  // table render dropped. Tree is the only view today; projects are
  // inherently a hierarchy (project → folder → pipeline) and the table
  // duplicated the data without the structural context. `useTableColumns`
  // stays because table-column visibility is also used by some other
  // tooltip / export surfaces.
  const { visibleColumns, activeColumns, toggleColumn, resetToDefaults, selectAll, isVisible } = useTableColumns('fpulse_projects_columns', PROJECT_COLUMNS);

  const fetchProjects = async () => {
    setLoading(true);
    try {
      const [list, workflows] = await Promise.all([
        // 2026-05-22: pull archived too so the Archived tab can render
        // server-stamped archived projects without a second round-trip.
        api.listProjects({ include_archived: true }),
        api.listWorkflows().catch(() => []),
      ]);
      const allProjects = Array.isArray(list) ? list : [];
      // 2026-05-22: split active vs archived using the server-stamped
      // `status` field. The previous localStorage filter is gone (audit C2).
      const active = allProjects.filter(p => (p as any).status !== 'archived');
      const archived = allProjects.filter(p => (p as any).status === 'archived');
      setProjects(active);
      setArchivedProjects(archived);
      setWorkspaceWorkflowCount(Array.isArray(workflows) ? workflows.length : 0);
    } catch { setProjects([]); setArchivedProjects([]); setWorkspaceWorkflowCount(0); toast.error('Failed to load projects'); }
    setLoading(false);
  };

  useEffect(() => {
    fetchProjects();
    api.listUsers().then(u => setAllUsers(Array.isArray(u) ? u : [])).catch(() => {});
  }, []);

  // 2026-05-22: one-time migration of any legacy localStorage archive
  // data into the backend. Runs once on mount. After the migration the
  // localStorage key is cleared so future loads short-circuit.
  //
  // Behaviour delta vs the old client-local expiry sweep:
  //   * No browser-side retention countdown — OSS does NOT auto-delete
  //     archived projects. An admin can still /delete them explicitly.
  //     The "X days left" copy is gone (audit C1 noted server should
  //     own retention; OSS scope is "archive, restore, delete" only —
  //     Plus is the right home for a retention scheduler).
  //   * One coalesced migration toast instead of per-expiry warnings.
  useEffect(() => {
    const legacy = readLegacyArchivedProjects();
    const ids = Object.keys(legacy);
    if (ids.length === 0) return;
    (async () => {
      const migrated: string[] = [];
      for (const id of ids) {
        try {
          await api.archiveProject(id);
          migrated.push(id);
        } catch {
          // Project may have already been deleted, or be in another
          // workspace now. Skip silently — the legacy local row will
          // be cleared anyway.
        }
      }
      clearLegacyArchivedProjects();
      if (migrated.length > 0) {
        toast.info(
          'Migrated archive to server',
          `${migrated.length} project(s) carried over to the new server-side archive. Future archive actions are auditable.`,
        );
        fetchProjects();
      } else {
        clearLegacyArchivedProjects();
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const defaultForm = () => ({
    name: '', description: '', color: '#6366f1', icon: 'folder', created_by: '', team: '',
    members: [] as string[],
    metadata: { cost_center: '', sponsor: '', department: '', priority: 'medium' as const, tags: [] as string[], notes: '' },
  });

  const handleCreate = async () => {
    if (!form.name.trim()) return;
    try {
      await api.createProject({
        name: form.name, description: form.description, color: form.color, icon: form.icon,
        members: form.members,
        metadata: form.metadata,
      });
      toast.success('Project created', form.name);
      setShowCreate(false);
      setForm(defaultForm());
      fetchProjects();
    } catch (err: any) { toast.error('Failed', err.message); }
  };

  const handleUpdate = async () => {
    if (!editProject) return;
    try {
      await api.updateProject(editProject.id, {
        name: form.name, description: form.description, color: form.color, icon: form.icon,
        members: form.members,
        metadata: form.metadata,
      });
      toast.success('Project updated');
      setEditProject(null);
      fetchProjects();
    } catch (err: any) { toast.error('Failed', err.message); }
  };

  // Export a project as a JSON bundle. Mirrors the single-pipeline export
  // in PipelinesPage (handleExport): call the API, then trigger a browser
  // download of the pretty-printed JSON. Filename is the slugified project
  // name so exports are self-describing on disk.
  const handleExport = async (p: Project) => {
    try {
      const data = await api.exportProject(p.id);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const slug = (p.name || 'project').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'project';
      a.href = url;
      a.download = `${slug}-project-export.json`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success('Project exported', `Downloaded ${a.download}`);
    } catch (err: any) {
      toast.error('Export failed', err?.message || 'Could not export project');
    }
  };

  const handleArchive = async (p: Project) => {
    // 2026-05-22: archive is server-side now. The backend stamps
    // archived_at / archived_by and audit-logs the event, so multiple
    // users / browsers see a consistent archived view (audit C1/C2).
    try {
      await api.archiveProject(p.id);
      toast.success('Project archived', `"${p.name}" moved to Archived. Restore anytime.`);
      fetchProjects();
    } catch (err: any) {
      toast.error('Archive failed', err?.message || 'Could not archive project');
    }
  };

  const handleRestore = async (id: string) => {
    const target = archivedProjects.find(p => p.id === id);
    try {
      await api.restoreProject(id);
      toast.success('Project restored', target ? `"${target.name}" is active again` : 'Project restored');
      fetchProjects();
    } catch (err: any) {
      toast.error('Restore failed', err?.message || 'Could not restore project');
    }
  };

  const handlePermanentDelete = async (id: string, name: string) => {
    if (!(await uiConfirm({ title: `Delete "${name}"?`, message: 'This permanently deletes the project. Refuses if the project still contains pipelines, connections, or credentials — move them to another project first.', danger: true, confirmLabel: 'Delete permanently' }))) return;
    try {
      await api.deleteProject(id);
      toast.success('Project permanently deleted');
    } catch (err: any) {
      // Backend returns 409 with {message, pipelines, connections, credentials}
      // when the project still has children. Surface those counts so the
      // user knows exactly what to move out before retrying.
      let detail = err?.message || 'Failed';
      try {
        const parsed = typeof err?.message === 'string' && err.message.trim().startsWith('{')
          ? JSON.parse(err.message)
          : null;
        if (parsed && (parsed.pipelines || parsed.connections || parsed.credentials)) {
          const bits: string[] = [];
          if (parsed.pipelines) bits.push(`${parsed.pipelines} pipeline${parsed.pipelines === 1 ? '' : 's'}`);
          if (parsed.connections) bits.push(`${parsed.connections} connection${parsed.connections === 1 ? '' : 's'}`);
          if (parsed.credentials) bits.push(`${parsed.credentials} credential${parsed.credentials === 1 ? '' : 's'}`);
          detail = `Still contains ${bits.join(', ')}. Move them to another project first.`;
        }
      } catch {}
      toast.error('Cannot delete project', detail);
    }
  };

  const openEdit = (p: Project) => {
    setEditProject(p);
    const m = p.metadata || {};
    setForm({
      name: p.name, description: p.description, color: p.color, icon: p.icon,
      created_by: p.created_by || '', team: p.team || '',
      members: p.members || [],
      metadata: {
        cost_center: m.cost_center || '', sponsor: m.sponsor || '',
        department: m.department || '', priority: m.priority || 'medium',
        tags: m.tags || [], notes: m.notes || '',
      },
    });
  };

  const getIcon = (iconId: string) => PROJECT_ICONS.find(i => i.id === iconId)?.icon || PROJECT_ICONS[0].icon;

  // 2026-05-22: getDaysLeft / days_left removed. OSS no longer
  // auto-deletes archived projects after a retention window — the
  // operator can /delete explicitly. (Server-side retention scheduler
  // is a Plus feature per the audit's C1 note.) The countdown UI is
  // dropped so the user isn't lied to about an automatic deletion
  // that won't happen.
  const archivedList = archivedProjects.map(p => ({
    ...p,
    // archived_at is on the project itself now — kept here for the
    // sort order code below that reads `.archived_at`.
    archived_at: (p as any).archived_at || '',
  }));

  const filteredProjects = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return projects.filter(p => {
      if (q && !(
        (p.name || '').toLowerCase().includes(q) ||
        (p.id || '').toLowerCase().includes(q) ||
        (p.description || '').toLowerCase().includes(q) ||
        (p.metadata?.department || '').toLowerCase().includes(q)
      )) return false;
      if (filterDepartment && (p.metadata?.department || '') !== filterDepartment) return false;
      if (filterPriority && (p.metadata?.priority || '') !== filterPriority) return false;
      if (filterOwner && (p.owner || p.created_by || '') !== filterOwner) return false;
      return true;
    });
  }, [projects, searchQuery, filterDepartment, filterPriority, filterOwner]);

  // Build chip option lists from the projects' own metadata so we never
  // offer a value that filters to zero rows. Sorted alphabetically.
  const departmentOptions = useMemo(() => {
    const s = new Set<string>();
    for (const p of projects) { const v = p.metadata?.department?.trim(); if (v) s.add(v); }
    return Array.from(s).sort();
  }, [projects]);
  const ownerOptions = useMemo(() => {
    const s = new Set<string>();
    for (const p of projects) { const v = (p.owner || p.created_by || '').trim(); if (v) s.add(v); }
    return Array.from(s).sort();
  }, [projects]);
  const priorityOptions = useMemo(() => {
    const order = ['low', 'medium', 'high', 'critical'];
    const present = new Set<string>();
    for (const p of projects) { const v = p.metadata?.priority; if (v) present.add(v); }
    return order.filter(o => present.has(o));
  }, [projects]);

  // 2026-05-21: eager-fetch folders per project so the Folder filter
  // dropdown can render hierarchical paths. N+1 requests on first paint
  // — fine at OSS scale (typically <20 projects). When this becomes a
  // hot path, add a /api/folders/all endpoint and swap in.
  useEffect(() => {
    if (projects.length === 0) return;
    let cancelled = false;
    Promise.all(
      projects.map(p =>
        api.listFolders(p.id)
          .then(rows => ({ pid: p.id, rows: Array.isArray(rows) ? rows : [] }))
          .catch(() => ({ pid: p.id, rows: [] })),
      ),
    ).then(results => {
      if (cancelled) return;
      const next: Record<string, Array<{ id: string; name: string; project_id: string }>> = {};
      for (const { pid, rows } of results) {
        next[pid] = rows.map((f: any) => ({ id: f.id, name: f.name, project_id: pid }));
      }
      setFoldersByProject(next);
    });
    return () => { cancelled = true; };
  }, [projects]);

  // Hierarchical paths for the Folder filter dropdown. Each option's value
  // is the folder id (or "unfiled:<projectId>" for the catch-all bucket);
  // the label reads "Project / Folder" so siblings with the same folder
  // name in different projects stay distinguishable.
  type FolderFilterOption = { value: string; label: string; projectName: string; folderName: string };
  const folderFilterOptions = useMemo<FolderFilterOption[]>(() => {
    const opts: FolderFilterOption[] = [];
    const projectsById = new Map(projects.map(p => [p.id, p]));
    const sortedProjects = [...projects].sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    for (const p of sortedProjects) {
      const folders = foldersByProject[p.id] || [];
      const sortedFolders = [...folders].sort((a, b) => a.name.localeCompare(b.name));
      for (const f of sortedFolders) {
        opts.push({
          value: f.id,
          label: `${p.name} / ${f.name}`,
          projectName: p.name,
          folderName: f.name,
        });
      }
      // Always offer the unfiled bucket per project so users can isolate
      // pipelines that haven't been bucketed yet.
      opts.push({
        value: `unfiled:${p.id}`,
        label: `${p.name} / (unfiled)`,
        projectName: p.name,
        folderName: '(unfiled)',
      });
    }
    return opts;
  }, [projects, foldersByProject]);

  // Pretty label for the active-filter chip — reuses the dropdown labels.
  const activeFolderLabel = useMemo(() => {
    if (!filterFolder) return '';
    const hit = folderFilterOptions.find(o => o.value === filterFolder);
    return hit?.label || '';
  }, [filterFolder, folderFilterOptions]);

  const hasActiveFilter = !!(searchQuery || filterDepartment || filterPriority || filterOwner || filterFolder);
  const clearFilters = useCallback(() => {
    setSearchQuery('');
    setFilterDepartment('');
    setFilterPriority('');
    setFilterOwner('');
    setFilterFolder('');
  }, []);

  const modal = showCreate || editProject;

  // Theme v2 — env-aware table chrome (docs/DESIGN_THEME_V2.md)
  // DEV  = soft lavender header + violet text + violet border
  // PROD = solid navy header + NAPLES yellow text + navy border
  const isProdEnv = environment === 'prod';
  // Text colour lives on the <tr> so every <th> inherits — lets us strip the
  // old hard-coded `text-amber-300` off each th without per-th template literals.
  const theadRow  = isProdEnv
    ? 'border-b-2 border-thead-prod-border bg-thead-prod-bg text-thead-prod-text'
    : 'border-b-2 border-thead-dev-border bg-thead-dev-bg text-thead-dev-text';
  const tableCard = isProdEnv
    ? 'rounded-lg border-2 border-thead-prod-border overflow-x-auto shadow-sm bg-white'
    : 'rounded-lg border-2 border-thead-dev-border overflow-x-auto shadow-sm bg-white';

  // 2026-05-19 (P1 #8 of PAGE_BY_PAGE_AUDIT.md): publish the current page
  // context to the Copilot so questions like "which projects are about
  // to expire?" or "summarise my finance projects" can answer without a
  // re-fetch. Visible IDs are the filtered set, not the full list.
  usePageContext({
    page: 'projects',
    visible_ids: filteredProjects.map((p) => p.id),
    filters: {
      search: searchQuery || null,
      department: filterDepartment || null,
      priority: filterPriority || null,
      owner: filterOwner || null,
      folder: filterFolder || null,
      group_by: groupBy || null,
    },
  });

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <ReadOnlyBanner environment={environment} />
      <div className="flex-1 overflow-auto bg-canvas-bg">
      {/* Header — canonical DEV/PROD pattern (see feedback_fpulse_page_header_standard).
          DEV header bg = metallic silver gradient so it reads continuous with
          the silver top nav — distinct from the cream canvas below. Apr 21
          2026 update: swapped from bg-chrome amber to slate-200→slate-300. */}
      <PageHeader
        environment={environment}
        icon={(
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-indigo-500">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
          </svg>
        )}
        title="Projects"
        titleAccessory={<TierChip tier={tier} environment={environment} />}
        subtitle={`${projects.length} active${archivedList.length > 0 ? ` · ${archivedList.length} archived` : ''} · Organize your pipelines`}
        belowTitle={filterFolder && activeFolderLabel ? (
          <button
            onClick={() => setFilterFolder('')}
            className="inline-flex items-center gap-1.5 px-2 py-0.5 text-xs font-medium rounded-full border transition-colors bg-indigo-50 text-indigo-700 border-indigo-200 hover:bg-indigo-100"
            title="Click to clear the folder filter and show all projects"
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
            </svg>
            Filtered: {activeFolderLabel}
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        ) : undefined}
        actions={(
          <>
            {archivedList.length > 0 && (
              <button
                onClick={() => setShowArchived(!showArchived)}
                className={`px-4 py-2 text-sm font-medium rounded-lg transition-colors flex items-center gap-1.5 ${
                  showArchived
                    ? 'bg-slate-200 text-slate-700'
                    : 'bg-slate-100 text-slate-500 hover:bg-slate-200'
                }`}
              >
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <polyline points="21 8 21 21 3 21 3 8" /><rect x="1" y="3" width="22" height="5" /><line x1="10" y1="12" x2="14" y2="12" />
                </svg>
                Archived ({archivedList.length})
              </button>
            )}
            <button
              onClick={() => { setShowCreate(true); setForm(defaultForm()); setTagInput(''); }}
              disabled={!canCreate}
              title={canCreate ? 'Create a new project' : 'Only admins can create projects. Ask a workspace admin or super_admin.'}
              className={`px-4 py-2 text-white text-sm font-bold rounded-lg shadow-sm hover:shadow-md transition-all flex items-center gap-2 ${
                canCreate ? '' : 'opacity-50 cursor-not-allowed'
              }`}
              style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
              New Project
            </button>
          </>
        )}
      />

      <div className="w-full max-w-[1500px] mx-auto px-8 py-6">
        {/* Hero KPI cards — matches Pipelines / Connections / Executions
            visual family (HeroCard gradient + centered icon + value). */}
        {!loading && (
          (() => {
            const isProdEnv = environment === 'prod';
            const total = projects.length;
            // Total Pipelines reads from the workspace-wide workflow list
            // (workspaceWorkflowCount) so it includes orphan pipelines —
            // ones with no project_id or whose project the caller can't
            // see. Sum of project.pipeline_count alone would undercount.
            const totalPipelines = workspaceWorkflowCount;
            const recentlyEdited = projects.filter(p => {
              const t = p.updated_at ? new Date(p.updated_at).getTime() : 0;
              return t && (Date.now() - t) < 7 * 24 * 3600 * 1000;
            }).length;
            const archived = archivedList.length;
            return (
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5">
                <HeroCard
                  gradient={isProdEnv ? 'from-indigo-500 to-indigo-600' : 'from-indigo-400 to-indigo-500'}
                  icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" /></svg>}
                  label="Active Projects"
                  value={String(total)}
                />
                <HeroCard
                  gradient={isProdEnv ? 'from-emerald-500 to-emerald-600' : 'from-emerald-400 to-emerald-500'}
                  icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" /></svg>}
                  label="Total Pipelines"
                  value={String(totalPipelines)}
                />
                <HeroCard
                  gradient={isProdEnv ? 'from-blue-500 to-sky-600' : 'from-blue-400 to-sky-500'}
                  icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>}
                  label="Edited (7d)"
                  value={String(recentlyEdited)}
                />
                <HeroCard
                  gradient={isProdEnv ? 'from-slate-400 to-slate-500' : 'from-slate-300 to-slate-400'}
                  icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="4" rx="1" /><path d="M5 7v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7" /><line x1="10" y1="11" x2="14" y2="11" /></svg>}
                  label="Archived"
                  value={String(archived)}
                />
              </div>
            );
          })()
        )}

        {loading ? (
          <DelayedSkeleton>
            <div className="bg-white rounded-2xl border border-slate-200 overflow-hidden">
              <table className="w-full">
                <tbody className="divide-y divide-slate-100">
                  {Array.from({ length: 5 }).map((_, i) => (
                    <SkeletonTableRow key={i} columns={6} />
                  ))}
                </tbody>
              </table>
            </div>
          </DelayedSkeleton>
        ) : (
          <>
            {/* 2026-05-19 (P2 #1 of PAGE_BY_PAGE_AUDIT.md): the previous
                `viewMode === 'tree' ? (tree) : (flat-table)` ternary
                always took the tree branch (the setter was never called),
                so ~345 lines of flat-table render code never executed.
                Removed. The Archive section that lived inside the
                flat-table branch is now a sibling of the tree so the
                "Archived (N)" toggle continues to work. */}
            {/* Filter / group bar — search + 3 metadata filters + group-by
                + clear-all. Sits above the tree so the same UX works
                whether the user is hunting for a project or browsing. */}
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <div className="relative flex-1 min-w-[220px] max-w-md">
                <input
                  type="text"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder="Search projects..."
                  className="w-full pl-8 pr-3 py-1.5 text-sm bg-white border border-slate-300 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-300 focus:border-indigo-400 placeholder:text-slate-400"
                />
                <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" /></svg>
              </div>
              {departmentOptions.length > 0 && (
                <select
                  value={filterDepartment}
                  onChange={(e) => setFilterDepartment(e.target.value)}
                  className={`px-2.5 py-1.5 text-sm bg-white border rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-300 ${filterDepartment ? 'border-indigo-400 text-indigo-700 font-medium' : 'border-slate-300 text-slate-700'}`}
                  title="Filter by department"
                >
                  <option value="">All departments</option>
                  {departmentOptions.map(o => <option key={o} value={o}>{o}</option>)}
                </select>
              )}
              {priorityOptions.length > 0 && (
                <select
                  value={filterPriority}
                  onChange={(e) => setFilterPriority(e.target.value)}
                  className={`px-2.5 py-1.5 text-sm bg-white border rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-300 ${filterPriority ? 'border-indigo-400 text-indigo-700 font-medium' : 'border-slate-300 text-slate-700'}`}
                  title="Filter by priority"
                >
                  <option value="">All priorities</option>
                  {priorityOptions.map(o => <option key={o} value={o}>{o.charAt(0).toUpperCase() + o.slice(1)}</option>)}
                </select>
              )}
              {ownerOptions.length > 1 && (
                <select
                  value={filterOwner}
                  onChange={(e) => setFilterOwner(e.target.value)}
                  className={`px-2.5 py-1.5 text-sm bg-white border rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-300 ${filterOwner ? 'border-indigo-400 text-indigo-700 font-medium' : 'border-slate-300 text-slate-700'}`}
                  title="Filter by owner"
                >
                  <option value="">All owners</option>
                  {ownerOptions.map(o => <option key={o} value={o}>{o}</option>)}
                </select>
              )}
              {/* 2026-05-21: Folder scope dropdown. Renders only when at
                  least one project has at least one folder (otherwise it'd
                  just list "(unfiled)" per project — noise). */}
              {folderFilterOptions.some(o => !o.value.startsWith('unfiled:')) && (
                <select
                  value={filterFolder}
                  onChange={(e) => setFilterFolder(e.target.value)}
                  className={`px-2.5 py-1.5 text-sm bg-white border rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-300 ${filterFolder ? 'border-indigo-400 text-indigo-700 font-medium' : 'border-slate-300 text-slate-700'}`}
                  title="Scope to a specific folder. '(unfiled)' isolates pipelines that aren't bucketed into any folder yet."
                >
                  <option value="">All folders</option>
                  {folderFilterOptions.map(o => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
              )}
              <div className="ml-auto flex items-center gap-2">
                <label className="text-xs text-slate-500">Group by</label>
                <select
                  value={groupBy}
                  onChange={(e) => setGroupBy(e.target.value as GroupBy)}
                  className={`px-2.5 py-1.5 text-sm bg-white border rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-300 ${groupBy !== 'none' ? 'border-indigo-400 text-indigo-700 font-medium' : 'border-slate-300 text-slate-700'}`}
                  title="Group projects by metadata"
                >
                  <option value="none">None</option>
                  <option value="department">Department</option>
                  <option value="owner">Owner</option>
                  <option value="priority">Priority</option>
                </select>
                {hasActiveFilter && (
                  <button
                    onClick={clearFilters}
                    className="px-2.5 py-1.5 text-xs font-medium text-slate-600 hover:text-slate-900 hover:bg-slate-100 rounded-md"
                    title="Clear search + filters"
                  >
                    Clear
                  </button>
                )}
              </div>
            </div>
            <ProjectFolderTree
              onSelectProject={onSelectProject}
              canEdit={canEdit}
              filteredProjectIds={hasActiveFilter ? new Set(filteredProjects.map(p => p.id)) : null}
              folderFilter={filterFolder || null}
              onFolderFilterChange={setFilterFolder}
              groupBy={groupBy}
              groupValueResolver={(p) => {
                if (groupBy === 'department') return p?.metadata?.department || '';
                if (groupBy === 'owner') return p?.owner || p?.created_by || '';
                if (groupBy === 'priority') return p?.metadata?.priority || '';
                return '';
              }}
              pinnedIds={pinnedIds}
              onTogglePin={togglePin}
              onArchive={(projectId) => {
                // 2026-05-19 (OSS-7 of PAGE_BY_PAGE_AUDIT.md): the tree
                // view's Archive button delegates back into the same
                // handler the (deleted) flat-table branch used so the
                // confirm + retention copy + toast stay in one place.
                const p = projects.find((proj) => proj.id === projectId);
                if (p) handleArchive(p);
              }}
              onExport={(projectId) => {
                // Export Project — same delegation pattern as onArchive so
                // the API call + blob download + toast live in one place
                // (handleExport). Tree is the only project list today.
                const p = projects.find((proj) => proj.id === projectId);
                if (p) handleExport(p);
              }}
            />

            {/* Archived Projects Section — moved here from the deleted
                flat-table branch so the "Archived (N)" toggle keeps
                working. Renders only when the toggle is on AND there's
                at least one archived row. */}
            {showArchived && archivedList.length > 0 && (
              <div className="mt-6">
                <div className="flex items-center gap-2 mb-3">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-400">
                    <polyline points="21 8 21 21 3 21 3 8" /><rect x="1" y="3" width="22" height="5" /><line x1="10" y1="12" x2="14" y2="12" />
                  </svg>
                  <h2 className="text-sm font-bold text-slate-600">Archived Projects</h2>
                  <span className="text-xs text-slate-400">Server-side archive — restore or delete manually</span>
                </div>

                <div className={tableCard}>
                  <table className="w-full border-collapse">
                    <thead>
                      <tr className={theadRow}>
                        <th className="text-left text-xs font-bold uppercase tracking-wider px-5 py-2.5">Project</th>
                        <th className="text-left text-xs font-bold uppercase tracking-wider px-4 py-2.5">Archived On</th>
                        <th className="text-left text-xs font-bold uppercase tracking-wider px-4 py-2.5">Archived By</th>
                        <th className="text-right text-xs font-bold uppercase tracking-wider px-5 py-2.5">Actions</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {archivedList.map(p => (
                        <tr key={p.id} className="hover:bg-slate-50/60 transition-colors">
                          <td className="px-5 py-3 max-w-[300px]">
                            <div className="flex items-center gap-3 min-w-0">
                              <div
                                className="w-8 h-8 rounded-lg flex items-center justify-center text-sm shrink-0 opacity-50"
                                style={{ background: `${p.color}15`, color: p.color }}
                              >
                                {getIcon(p.icon)}
                              </div>
                              <div className="min-w-0">
                                <span className="text-sm font-medium text-slate-500 line-through truncate block" title={p.name}>{p.name}</span>
                                {p.pipeline_count ? (
                                  <span className="text-xs text-slate-400">{p.pipeline_count} pipeline{p.pipeline_count !== 1 ? 's' : ''}</span>
                                ) : null}
                              </div>
                            </div>
                          </td>
                          <td className="px-4 py-3">
                            <span className="text-xs text-slate-400">
                              {p.archived_at ? new Date(p.archived_at).toLocaleDateString() : '—'}
                            </span>
                          </td>
                          <td className="px-4 py-3">
                            <span className="text-xs text-slate-500">
                              {(p as any).archived_by || '—'}
                            </span>
                          </td>
                          <td className="px-5 py-3">
                            <div className="flex gap-1 justify-end">
                              <button
                                onClick={() => handleRestore(p.id)}
                                className="px-3 py-1.5 text-xs font-semibold text-emerald-600 bg-emerald-50 border border-emerald-200 rounded-lg hover:bg-emerald-100 transition-colors flex items-center gap-1"
                                title="Restore to active"
                              >
                                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="1 4 1 10 7 10" /><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" /></svg>
                                Restore
                              </button>
                              {canDelete && (
                              <button
                                onClick={() => handlePermanentDelete(p.id, p.name)}
                                className="px-3 py-1.5 text-xs font-semibold text-red-500 bg-red-50 border border-red-200 rounded-lg hover:bg-red-100 transition-colors flex items-center gap-1"
                                title="Permanently delete now"
                              >
                                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6" /><path d="M19 6l-2 14H7L5 6" /><path d="M9 6V4h6v2" /></svg>
                                Delete Now
                              </button>
                              )}
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <div className="mt-2 flex items-center gap-2 px-1">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-400 shrink-0">
                    <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
                  </svg>
                  <p className="text-xs text-slate-400">
                    Archive is server-side and auditable. Archived projects stay in storage with their pipelines/connections/credentials intact until you Delete Now. Restore to bring a project back to the active list.
                  </p>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {/* ───── DEAD FLAT-TABLE BRANCH (~350 lines) REMOVED 2026-05-19 (P2 #1).
          The previous `viewMode === 'tree' ? (tree) : (flat-table)` ternary
          always took the tree branch (setter was never called). The whole
          else arm — table render + nested archive section + duplicate modal
          opener — has been deleted. The archive section now lives at the
          bottom of the tree branch above so the "Archived (N)" toggle keeps
          working. See PAGE_BY_PAGE_AUDIT.md P2 #1 for the audit ticket. */}

      {/* Create/Edit Modal */}
      {modal && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" onClick={() => { setShowCreate(false); setEditProject(null); }}>
          <div className="bg-white rounded-2xl shadow-xl w-full max-w-2xl p-6 max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <h2 className="text-lg font-bold text-slate-800 mb-4">
              {editProject ? 'Edit Project' : 'New Project'}
            </h2>

            <div className="space-y-4">
              {/* ── Basic info ── */}
              <div>
                <label className="text-xs font-semibold text-slate-600 mb-1 block">Name *</label>
                <input
                  type="text" value={form.name}
                  onChange={e => setForm({ ...form, name: e.target.value })}
                  placeholder="My Pipeline Project"
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 outline-none"
                  autoFocus
                />
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 mb-1 block">Description</label>
                <textarea
                  value={form.description}
                  onChange={e => setForm({ ...form, description: e.target.value })}
                  placeholder="What is this project for?"
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 outline-none resize-none h-16"
                />
              </div>

              {/* ── Color + Icon row ── */}
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="text-xs font-semibold text-slate-600 mb-1 block">Color</label>
                  <div className="flex gap-2">
                    {PROJECT_COLORS.map(c => (
                      <button
                        key={c}
                        onClick={() => setForm({ ...form, color: c })}
                        className={`w-7 h-7 rounded-lg transition-all ${form.color === c ? 'ring-2 ring-offset-2 ring-slate-400 scale-110' : 'hover:scale-105'}`}
                        style={{ background: c }}
                      />
                    ))}
                  </div>
                </div>
                <div>
                  <label className="text-xs font-semibold text-slate-600 mb-1 block">Icon</label>
                  <div className="flex gap-2 flex-wrap">
                    {PROJECT_ICONS.map(i => (
                      <button
                        key={i.id}
                        onClick={() => setForm({ ...form, icon: i.id })}
                        className={`w-8 h-8 rounded-lg flex items-center justify-center text-sm transition-all ${form.icon === i.id ? 'bg-indigo-50 ring-2 ring-indigo-300' : 'bg-slate-50 hover:bg-slate-100'}`}
                        title={i.label}
                      >
                        {i.icon}
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              {/* ── Divider: Governance ── */}
              <div className="border-t border-slate-200 pt-3">
                <h3 className="text-xs font-bold text-slate-500 uppercase tracking-wider mb-3">Project Governance <span className="text-slate-300 font-normal normal-case">(optional)</span></h3>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="text-xs font-semibold text-slate-500 mb-1 block">Cost Center</label>
                    <input
                      type="text" value={form.metadata.cost_center || ''}
                      onChange={e => setForm({ ...form, metadata: { ...form.metadata, cost_center: e.target.value } })}
                      placeholder="e.g. CC-4500"
                      className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 outline-none"
                    />
                  </div>
                  <div>
                    <label className="text-xs font-semibold text-slate-500 mb-1 block">Project Sponsor</label>
                    <input
                      type="text" value={form.metadata.sponsor || ''}
                      onChange={e => setForm({ ...form, metadata: { ...form.metadata, sponsor: e.target.value } })}
                      placeholder="e.g. VP Engineering"
                      className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 outline-none"
                    />
                  </div>
                  <div>
                    <label className="text-xs font-semibold text-slate-500 mb-1 block">Department</label>
                    <input
                      type="text" value={form.metadata.department || ''}
                      onChange={e => setForm({ ...form, metadata: { ...form.metadata, department: e.target.value } })}
                      placeholder="e.g. Data Engineering"
                      className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 outline-none"
                    />
                  </div>
                  <div>
                    <label className="text-xs font-semibold text-slate-500 mb-1 block">Priority</label>
                    <select
                      value={form.metadata.priority || 'medium'}
                      onChange={e => setForm({ ...form, metadata: { ...form.metadata, priority: e.target.value as any } })}
                      className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 outline-none bg-white"
                    >
                      <option value="low">Low</option>
                      <option value="medium">Medium</option>
                      <option value="high">High</option>
                      <option value="critical">Critical</option>
                    </select>
                  </div>
                </div>

                {/* Tags */}
                <div className="mt-3">
                  <label className="text-xs font-semibold text-slate-500 mb-1 block">Tags</label>
                  <div className="flex flex-wrap gap-1.5 mb-1.5">
                    {(form.metadata.tags || []).map((tag, i) => (
                      <span key={i} className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium bg-indigo-50 text-indigo-600 border border-indigo-200 rounded-full">
                        {tag}
                        <button onClick={() => setForm({ ...form, metadata: { ...form.metadata, tags: (form.metadata.tags || []).filter((_, idx) => idx !== i) } })} className="text-indigo-400 hover:text-indigo-600">&times;</button>
                      </span>
                    ))}
                  </div>
                  <div className="flex gap-2">
                    <input
                      type="text" value={tagInput}
                      onChange={e => setTagInput(e.target.value)}
                      onKeyDown={e => {
                        if (e.key === 'Enter' && tagInput.trim()) {
                          e.preventDefault();
                          setForm({ ...form, metadata: { ...form.metadata, tags: [...(form.metadata.tags || []), tagInput.trim()] } });
                          setTagInput('');
                        }
                      }}
                      placeholder="Type tag and press Enter"
                      className="flex-1 px-3 py-1.5 text-xs border border-slate-200 rounded-lg focus:ring-1 focus:ring-indigo-300 outline-none"
                    />
                  </div>
                </div>

                {/* Notes */}
                <div className="mt-3">
                  <label className="text-xs font-semibold text-slate-500 mb-1 block">Notes</label>
                  <textarea
                    value={form.metadata.notes || ''}
                    onChange={e => setForm({ ...form, metadata: { ...form.metadata, notes: e.target.value } })}
                    placeholder="Internal notes, compliance requirements, etc."
                    className="w-full px-3 py-2 text-xs border border-slate-200 rounded-lg focus:ring-2 focus:ring-indigo-300 focus:border-indigo-300 outline-none resize-none h-14"
                  />
                </div>
              </div>

              {/* Per-project access control is a multi-user (F-Pulse+) capability.
                  Single-operator OSS has no team, so this is hidden unless more
                  than one account exists. */}
              {allUsers.length > 1 && (
              <div className="border-t border-slate-200 pt-3">
                <h3 className="text-xs font-bold text-slate-500 uppercase tracking-wider mb-3">Project access</h3>

                {/* Current members */}
                {form.members.length > 0 && (
                  <div className="flex flex-wrap gap-1.5 mb-3">
                    {form.members.map(uid => {
                      const u = allUsers.find(u => u.id === uid);
                      return (
                        <span key={uid} className="inline-flex items-center gap-1.5 pl-1.5 pr-2 py-1 text-xs font-medium bg-emerald-50 text-emerald-700 border border-emerald-200 rounded-full">
                          <span className="w-5 h-5 rounded-full bg-emerald-200 text-emerald-700 flex items-center justify-center text-[9px] font-bold">
                            {(u?.name || u?.email || uid).charAt(0).toUpperCase()}
                          </span>
                          {u?.name || u?.email || uid}
                          <button onClick={() => setForm({ ...form, members: form.members.filter(m => m !== uid) })} className="text-emerald-400 hover:text-red-500 ml-0.5">&times;</button>
                        </span>
                      );
                    })}
                  </div>
                )}

                {/* Add members */}
                {allUsers.length > 0 ? (
                  <div className="flex flex-wrap gap-1.5">
                    {allUsers.filter(u => !form.members.includes(u.id)).map(u => (
                      <button
                        key={u.id}
                        onClick={() => setForm({ ...form, members: [...form.members, u.id] })}
                        className="inline-flex items-center gap-1.5 pl-1.5 pr-2 py-1 text-xs font-medium bg-slate-50 text-slate-500 border border-slate-200 rounded-full hover:bg-indigo-50 hover:text-indigo-600 hover:border-indigo-200 transition-colors"
                      >
                        <span className="w-5 h-5 rounded-full bg-slate-200 text-slate-500 flex items-center justify-center text-[9px] font-bold">
                          {(u.name || u.email || '?').charAt(0).toUpperCase()}
                        </span>
                        {u.name || u.email}
                        <span className="text-slate-300">+</span>
                      </button>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-slate-400">No other users.</p>
                )}
              </div>
              )}
            </div>

            <div className="flex justify-end gap-2 mt-6 pt-4 border-t border-slate-100">
              <button
                onClick={() => { setShowCreate(false); setEditProject(null); }}
                className="px-4 py-2 text-sm text-slate-600 hover:text-slate-800 rounded-lg hover:bg-slate-50"
              >
                Cancel
              </button>
              <button
                onClick={editProject ? handleUpdate : handleCreate}
                className="px-4 py-2 text-sm text-white font-semibold rounded-lg"
                style={{ background: 'linear-gradient(135deg, #6366f1, #4f46e5)' }}
              >
                {editProject ? 'Save Changes' : 'Create Project'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
    </div>
  );
}
