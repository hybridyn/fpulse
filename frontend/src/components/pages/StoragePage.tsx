/**
 * Storage page — workspace datastore browser (Y4-Y12, 2026-05-23).
 *
 * Three tabs:
 *   - Files          → uploads + soft-delete + replace + "show deleted"
 *   - Managed Tables → Parquet tables addressable by schema.name
 *   - Outputs        → pipeline-generated artifacts grouped by run
 *
 * Project scope (Y11): renders ProjectContextBar when a project is
 * active. Uploads default project_id = current project (or workspace-
 * global when none). Files/Tables tables show a Scope column.
 *
 * Usage tracking (Y12): each row carries a "Used by N" pill that opens
 * a popover listing the pipelines that reference the file/table.
 * Destructive actions (Delete, Drop, Replace) warn first when usage
 * exists so a user doesn't silently corrupt a downstream pipeline.
 *
 * Theme: canonical 78px sticky header, slate gradient, HeroCard KPIs,
 * RowActionButton for row actions — matches Pipelines + Connections.
 */

import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';
import { uiConfirm } from '../../ui/dialog';
import { useDarkMode } from '../../hooks/useDarkMode';
import { DelayedSkeleton } from '../shared/Skeleton';
import HeroCard from '../shared/HeroCard';
import TierChip from '../shared/TierChip';
import PageHeader from '../shared/PageHeader';
import RowActionButton from '../shared/RowActionButton';
import ProjectContextBar from '../layout/ProjectContextBar';
import TableToolbar, {
  useTableColumns,
  type TColumn,
  type TColumnGroup,
} from '../shared/TableToolbar';
import StoragePreviewDrawer from './StoragePreviewDrawer';
import StoragePromoteDialog from './StoragePromoteDialog';
import StorageUploadDialog from './StorageUploadDialog';
import StorageTableEditDialog from './StorageTableEditDialog';

type StorageTab = 'files' | 'tables' | 'outputs' | 'query';
type ScopeFilter = 'all' | 'global' | 'project';

interface StorageObject {
  id: string;
  workspace_id: string;
  kind: 'file' | 'output';
  name: string;
  path: string;
  format: string | null;
  size_bytes: number;
  row_count: number | null;
  column_count: number | null;
  project_id: string | null;
  pipeline_id: string | null;
  run_id: string | null;
  tags: string[];
  description: string;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

interface StorageTable {
  id: string;
  workspace_id: string;
  schema_name: string;
  name: string;
  path: string;
  row_count: number;
  column_count: number;
  size_bytes: number;
  part_count: number;
  description: string;
  tags: string[];
  created_at: string;
  // Z24 (2026-05-23) — provenance link. Set on the row at promote-time
  // (Storage → Files → Promote). Null for tables written by a pipeline's
  // local_table_sink (those use the usage scanner's sink-role refs as
  // their provenance signal instead).
  created_from_object_id?: string | null;
  // Z33 (2026-05-23) — Pipeline Data Prep provenance. Populated by the
  // local_table_sink when the table was produced by a Storage Z1
  // "Clean & Promote" pipeline. prep_recipe is the Wrangler step list,
  // prep_source_object_id back-links the file the prep ran on,
  // prep_workflow_id is the pipeline the user clicks "Edit recipe" on.
  prep_recipe?: Array<Record<string, unknown>> | null;
  prep_source_object_id?: string | null;
  prep_workflow_id?: string | null;
}

interface StorageSummary {
  workspace_id: string;
  file_count: number;
  file_size_bytes: number;
  output_count: number;
  output_size_bytes: number;
  table_count: number;
  table_size_bytes: number;
  trash_count: number;
  trash_size_bytes: number;
  total_size_bytes: number;
}

interface OutputGroup {
  pipeline_id: string;
  /** Human pipeline name resolved by the backend from pipeline_id.
   *  Null when the run had no pipeline (ad-hoc) or the pipeline is gone. */
  pipeline_name?: string | null;
  run_id: string;
  size_bytes: number;
  object_count: number;
  objects: StorageObject[];
}

interface UsageRef {
  workflow_id: string;
  name: string;
  role?: string;
  via_table?: string;
}

interface UsageMap {
  files: Record<string, UsageRef[]>;
  tables: Record<string, UsageRef[]>;
}

const TABS: Array<{
  key: StorageTab;
  label: string;
  subtitle: string;
  icon: React.ReactNode;
}> = [
  {
    key: 'files',
    label: 'Files',
    subtitle: 'Uploaded files. Use as Source-node inputs or promote to a managed table.',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
      </svg>
    ),
  },
  {
    key: 'tables',
    label: 'Managed Tables',
    subtitle: 'Parquet tables addressable as schema.name from local_table_source / local_table_sink.',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <ellipse cx="12" cy="5" rx="9" ry="3" />
        <path d="M3 5v6c0 1.66 4.03 3 9 3s9-1.34 9-3V5" />
        <path d="M3 11v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6" />
      </svg>
    ),
  },
  {
    key: 'outputs',
    label: 'Pipeline Outputs',
    subtitle: 'Files that pipeline runs produced. Pipeline + run shown per file.',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 3h18v18H3z" />
        <path d="M3 9h18" />
        <path d="M9 21V9" />
      </svg>
    ),
  },
  {
    key: 'query',
    label: 'Query',
    subtitle: 'Run read-only SELECT / WITH queries over your managed tables. Reference them by schema.name.',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="m18 16 4-4-4-4" />
        <path d="m6 8-4 4 4 4" />
        <path d="m14.5 4-5 16" />
      </svg>
    ),
  },
];

// ── Column configs for the canonical TableToolbar (Y14) ─────────────────
//
// Same shape Connections / Pipelines use — TableToolbar reads these to
// render the column-visibility picker and stay in sync with each
// <th>'s isVisible('key') guard below.

const FILE_COLUMN_GROUPS: TColumnGroup[] = [
  { key: 'core',     label: 'Core',     icon: '◆' },
  { key: 'details',  label: 'Details',  icon: '◇' },
  { key: 'metadata', label: 'Metadata', icon: '⚙' },
];
const FILE_COLUMNS: TColumn[] = [
  { key: 'name',     label: 'Name',     default: true,  group: 'core' },
  { key: 'format',   label: 'Format',   default: true,  group: 'core' },
  { key: 'scope',    label: 'Scope',    default: true,  group: 'core' },
  { key: 'size',     label: 'Size',     default: true,  group: 'core' },
  { key: 'used_by',  label: 'Used by',  default: true,  group: 'core' },
  { key: 'actions',  label: 'Actions',  default: true,  group: 'core' },
  { key: 'updated',  label: 'Updated',  default: true,  group: 'details' },
  { key: 'created',  label: 'Created',  default: false, group: 'details' },
  { key: 'rows',     label: 'Row count', default: false, group: 'details' },
  { key: 'columns',  label: 'Column count', default: false, group: 'details' },
  { key: 'path',     label: 'Path',     default: false, group: 'metadata' },
  { key: 'tags',     label: 'Tags',     default: false, group: 'metadata' },
];

const TABLE_COLUMN_GROUPS: TColumnGroup[] = [
  { key: 'core',     label: 'Core',     icon: '◆' },
  { key: 'stats',    label: 'Stats',    icon: '◇' },
  { key: 'metadata', label: 'Metadata', icon: '⚙' },
];
const TABLE_COLUMNS: TColumn[] = [
  { key: 'schema',   label: 'Schema',   default: true,  group: 'core' },
  { key: 'name',     label: 'Table',    default: true,  group: 'core' },
  { key: 'used_by',  label: 'Used by',  default: true,  group: 'core' },
  { key: 'actions',  label: 'Actions',  default: true,  group: 'core' },
  { key: 'rows',     label: 'Rows',     default: true,  group: 'stats' },
  { key: 'columns',  label: 'Columns',  default: true,  group: 'stats' },
  { key: 'size',     label: 'Size',     default: true,  group: 'stats' },
  { key: 'parts',    label: 'Parts',    default: true,  group: 'stats' },
  { key: 'created',  label: 'Created',  default: false, group: 'metadata' },
  { key: 'description', label: 'Description', default: false, group: 'metadata' },
];

function readInitialTab(): StorageTab {
  // 2026-05-25 — matched the cross-page convention used by Settings /
  // Insights / Help: navigating back into Storage lands on the default
  // sub-tab (Files), not whatever the user last clicked.
  // The previous implementation persisted to `localStorage.fpulse_storage_tab`
  // which made the page feel "stuck" — a user who used Managed Tables
  // once would land there forever, even when they came in fresh
  // expecting the file inventory.
  //
  // Two opt-ins preserved:
  //   1. Hash subroute (`#storage/tables`, `#storage/outputs`) — for
  //      shareable URLs and deep links from other pages.
  //   2. One-shot sessionStorage breadcrumb (`fpulse_storage_initial_tab`)
  //      cleared on read — matches the HelpPage pattern.
  try {
    const breadcrumb = sessionStorage.getItem('fpulse_storage_initial_tab');
    if (breadcrumb) {
      sessionStorage.removeItem('fpulse_storage_initial_tab');
      if (breadcrumb === 'files' || breadcrumb === 'tables' || breadcrumb === 'outputs' || breadcrumb === 'query') {
        return breadcrumb;
      }
    }
  } catch { /* sessionStorage disabled */ }
  try {
    const sub = (window.location.hash || '').split('/')[1];
    if (sub === 'files' || sub === 'tables' || sub === 'outputs' || sub === 'query') return sub;
  } catch { /* non-DOM context */ }
  return 'files';
}

function formatBytes(n: number): string {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = n;
  let unit = 0;
  while (v >= 1024 && unit < units.length - 1) {
    v /= 1024;
    unit += 1;
  }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[unit]}`;
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function StoragePage({
  projectId,
  projectName = '',
  onClearProject,
  onGoToProjects,
  environment = 'dev',
  tier = 'free',
}: {
  projectId?: string | null;
  projectName?: string;
  onClearProject?: () => void;
  onGoToProjects?: () => void;
  environment?: 'dev' | 'prod';
  tier?: string;
}) {
  const dark = useDarkMode();
  const isProd = environment === 'prod';

  const [tab, setTabState] = useState<StorageTab>(readInitialTab);
  // 2026-05-25 — sub-tab clicks update the URL hash subroute (`#storage`,
  // `#storage/tables`, `#storage/outputs`) so the browser back button
  // and shareable URLs both work. No persistent localStorage state —
  // re-entering the page from the sidebar starts on Files (default).
  const setTab = (next: StorageTab) => {
    setTabState(next);
    try {
      const target = next === 'files' ? 'storage' : `storage/${next}`;
      if (window.location.hash !== `#${target}`) {
        // Use replaceState so back button doesn't fill up with tab clicks.
        history.replaceState(null, '', `#${target}`);
      }
    } catch { /* non-DOM context */ }
  };

  // Honor runtime hash changes too (e.g. user pastes a different
  // #storage/tables URL or hits the sidebar Storage link mid-session).
  useEffect(() => {
    const onHashChange = () => {
      const sub = (window.location.hash || '').split('/')[1];
      const target: StorageTab = (sub === 'tables' || sub === 'outputs' || sub === 'query') ? sub : 'files';
      setTabState(target);
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  const [summary, setSummary] = useState<StorageSummary | null>(null);
  const [files, setFiles] = useState<StorageObject[]>([]);
  const [tables, setTables] = useState<StorageTable[]>([]);
  const [outputs, setOutputs] = useState<OutputGroup[]>([]);
  const [usage, setUsage] = useState<UsageMap>({ files: {}, tables: {} });
  const [loading, setLoading] = useState(true);
  const [showDeleted, setShowDeleted] = useState(false);
  const [scopeFilter, setScopeFilter] = useState<ScopeFilter>('all');
  // Y14 — one search box per tab; localStorage'd column visibility.
  const [filesSearch, setFilesSearch] = useState('');
  const [tablesSearch, setTablesSearch] = useState('');
  const [outputsSearch, setOutputsSearch] = useState('');
  const filesColState = useTableColumns('fpulse_storage_files', FILE_COLUMNS);
  const tablesColState = useTableColumns('fpulse_storage_tables', TABLE_COLUMNS);
  const [previewObj, setPreviewObj] = useState<StorageObject | null>(null);
  const [previewResourceKind, setPreviewResourceKind] = useState<'object' | 'table'>('object');
  // Z22 (2026-05-23) — Managed Table edit metadata dialog. Set when
  // user clicks the pencil icon on a Managed Tables row.
  const [editTable, setEditTable] = useState<StorageTable | null>(null);
  // Z5 (2026-05-23) — resizable bottom-panel height. Persisted in
  // localStorage so a user's preferred height sticks across navigations.
  // Clamped to [220, 80vh] inside the drawer; here we just store the
  // value the user dragged to so the page's bottom-padding tracks it.
  const [previewHeight, setPreviewHeight] = useState<number>(() => {
    try {
      const raw = localStorage.getItem('fpulse_storage_preview_height');
      const parsed = raw ? parseInt(raw, 10) : NaN;
      return Number.isFinite(parsed) && parsed >= 220 ? Math.min(parsed, 1200) : 440;
    } catch {
      return 440;
    }
  });
  const [promoteObj, setPromoteObj] = useState<StorageObject | null>(null);
  // Y15: upload dialog state (opens on "+ Upload file" click).
  const [showUpload, setShowUpload] = useState(false);
  const [usagePopover, setUsagePopover] = useState<{
    kind: 'file' | 'table';
    id: string;
    title: string;
    pipelines: UsageRef[];
  } | null>(null);

  const activeTab = TABS.find((t) => t.key === tab) ?? TABS[0];

  const refresh = async () => {
    setLoading(true);
    try {
      const [sum, fls, tbls, outs, usg] = await Promise.all([
        api.get<StorageSummary>('/api/storage/summary'),
        api.get<{ objects: StorageObject[]; count: number }>(
          // 2026-05-25 — DO NOT re-enable include_system_docs here.
          // Bundled docs belong on Help → Documentation, NOT in the
          // Storage Files tab. Surfacing them as "System · 0 B · MD"
          // rows pollutes the user's actual data inventory (the user
          // has flagged this twice). Z45 (2026-05-23) was the right
          // call; leaving it in place. The backend param still exists
          // for the `list_storage` AI tool which intentionally opts in.
          `/api/storage/files?include_deleted=${showDeleted ? 'true' : 'false'}`,
        ),
        api.get<{ tables: StorageTable[]; count: number }>('/api/storage/tables'),
        api.get<{ groups: OutputGroup[]; count: number }>('/api/storage/outputs'),
        api
          .get<UsageMap>('/api/storage/usage')
          .catch(() => ({ files: {}, tables: {} })),
      ]);
      setSummary(sum);
      setFiles(fls.objects || []);
      setTables(tbls.tables || []);
      setOutputs(outs.groups || []);
      setUsage(usg || { files: {}, tables: {} });
    } catch (err) {
      console.error('Storage refresh failed', err);
      toast.error(`Could not load storage: ${(err as Error).message || err}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showDeleted, projectId]);

  // ── Actions ────────────────────────────────────────────────────────────

  // Y15: opens the upload dialog instead of silently picking the current
  // project context. The dialog handles file selection + scope + folder
  // + description; calls the API on submit and refreshes here.
  const onUploadClick = () => setShowUpload(true);

  const onReplaceClick = async (obj: StorageObject) => {
    const usedBy = usage.files[obj.id] || [];
    if (usedBy.length > 0) {
      const ok = await uiConfirm({
        title: `Replace ${obj.name}?`,
        message: `This file is referenced by ${usedBy.length} pipeline${usedBy.length === 1 ? '' : 's'} (${usedBy
          .slice(0, 3)
          .map((p) => p.name)
          .join(', ')}${usedBy.length > 3 ? `, +${usedBy.length - 3} more` : ''}). The new bytes will apply on their next run. Continue?`,
        confirmLabel: 'Replace anyway',
      });
      if (!ok) return;
    }
    const input = document.createElement('input');
    input.type = 'file';
    // Same extension only — backend enforces this; we set the filter as hint.
    const ext = (obj.format ? `.${obj.format}` : '');
    input.accept = ext;
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return;
      const form = new FormData();
      form.append('file', file);
      try {
        await api.postRaw(`/api/storage/file/${obj.id}/replace`, form);
        toast.success(`Replaced ${obj.name}`);
        refresh();
      } catch (err) {
        toast.error(`Replace failed: ${(err as Error).message || err}`);
      }
    };
    input.click();
  };

  const onDeleteFile = async (obj: StorageObject) => {
    const usedBy = usage.files[obj.id] || [];
    const usageBlurb =
      usedBy.length > 0
        ? ` This file is referenced by ${usedBy.length} pipeline${usedBy.length === 1 ? '' : 's'} (${usedBy
            .slice(0, 3)
            .map((p) => p.name)
            .join(', ')}${usedBy.length > 3 ? `, +${usedBy.length - 3} more` : ''}) — they will fail on next run.`
        : '';
    const ok = await uiConfirm({
      title: 'Move to trash?',
      message: `${obj.name} will be moved to trash. You can restore it from the "Show deleted" view.${usageBlurb}`,
      confirmLabel: 'Move to trash',
      destructive: true,
    });
    if (!ok) return;
    try {
      await api.delete(`/api/storage/file/${obj.id}`);
      toast.success(`Moved ${obj.name} to trash`);
      refresh();
    } catch (err) {
      toast.error(`Delete failed: ${(err as Error).message || err}`);
    }
  };

  const onRestoreFile = async (obj: StorageObject) => {
    try {
      await api.post('/api/storage/move', { object_id: obj.id, to: 'uploads' });
      toast.success(`Restored ${obj.name}`);
      refresh();
    } catch (err) {
      toast.error(`Restore failed: ${(err as Error).message || err}`);
    }
  };

  // Z22 (2026-05-23) — open the data-prep pipeline that fills this
  // managed table. The backend usage scanner stamps each reference
  // with a `role` ∈ {source, sink, generic}; we pick sinks because
  // those are the writers (the recipe that PRODUCES the table). If
  // there's exactly one writer, navigate straight there. If multiple,
  // open the usage popover so the user can pick.
  const onOpenTableDataPrep = (table: StorageTable) => {
    // Z36 (2026-05-23) — if the table was produced by a Z1 wand pipeline,
    // prep_workflow_id is stamped on the row at sink-write time. Trust
    // that directly — it's faster than re-scanning workflows, and it
    // works even if the usage scanner missed the sink rule (e.g. a
    // generic destination step with connector_type=local_table that
    // doesn't perfectly match the schema.name shape).
    if (table.prep_workflow_id) {
      window.location.hash = `editor/${table.prep_workflow_id}`;
      return;
    }
    const refs = (usage.tables[table.id] || []) as Array<UsageRef & { role?: string }>;
    const writers = refs.filter((r) => r.role === 'sink');
    if (writers.length === 0) {
      toast.info('No data prep pipeline writes to this table yet. Promote a file or create one from Storage → Files.');
      return;
    }
    if (writers.length === 1) {
      window.location.hash = `editor/${writers[0].workflow_id}`;
      return;
    }
    // Multiple writers — surface the existing usage popover so the
    // user can pick. Filter to sinks only so it's a writer-only list.
    setUsagePopover({
      kind: 'table',
      id: table.id,
      title: `${table.schema_name}.${table.name} — pick a writer`,
      pipelines: writers,
    });
  };

  const onDropTable = async (table: StorageTable) => {
    const usedBy = usage.tables[table.id] || [];
    const usageBlurb =
      usedBy.length > 0
        ? ` ${usedBy.length} pipeline${usedBy.length === 1 ? '' : 's'} reference this table (${usedBy
            .slice(0, 3)
            .map((p) => p.name)
            .join(', ')}${usedBy.length > 3 ? `, +${usedBy.length - 3} more` : ''}) — they will fail on next run.`
        : '';
    const ok = await uiConfirm({
      title: `Drop ${table.schema_name}.${table.name}?`,
      message:
        'Its data is moved to trash and purged after the retention window.' +
        usageBlurb,
      confirmLabel: 'Drop table',
      destructive: true,
    });
    if (!ok) return;
    try {
      await api.delete(`/api/storage/tables/${table.id}`);
      toast.success(`Dropped ${table.schema_name}.${table.name} (recoverable from trash)`);
      refresh();
    } catch (err) {
      toast.error(`Drop failed: ${(err as Error).message || err}`);
    }
  };

  // Z1 (2026-05-23) — Prepare an uploaded file via the scaffold endpoint.
  // It still reuses the workflow runtime internally, but the imported
  // JSON carries metadata.scaffolded_from=storage_file so the Editor
  // chrome presents this as a one-time Data Prep workspace, not a
  // normal pipeline authoring session.
  const onPreviewObject = (obj: StorageObject) => {
    setPreviewResourceKind('object');
    setPreviewObj(obj);
  };

  const onPreviewTable = (table: StorageTable) => {
    setPreviewResourceKind('table');
    setPreviewObj({
      id: table.id,
      workspace_id: table.workspace_id,
      kind: 'output',
      name: `${table.schema_name}.${table.name}`,
      path: table.path,
      format: 'parquet',
      size_bytes: table.size_bytes,
      row_count: table.row_count,
      column_count: table.column_count,
      // V7 round 4 — surface the prep workflow so the drawer's
      // "View lineage" button can deep-link directly to the
      // pipeline that produced this table. usage.tables[table.id]
      // is the fallback for tables written by a local_table_sink
      // (no prep_workflow_id but writers tracked separately).
      pipeline_id: table.prep_workflow_id || (usage.tables[table.id] || []).find((u) => u.role === 'sink')?.workflow_id || null,
      project_id: null,
      run_id: null,
      tags: table.tags || [],
      description: table.description || '',
      created_at: table.created_at,
      updated_at: table.created_at,
      deleted_at: null,
    });
  };

  const onDeleteOutput = async (obj: StorageObject) => {
    const ok = await uiConfirm({
      title: `Delete output ${obj.name}?`,
      message: 'This permanently deletes the pipeline output artifact from storage. This cannot be undone.',
      confirmLabel: 'Delete output',
      destructive: true,
    });
    if (!ok) return;
    try {
      await api.delete(`/api/storage/outputs/${obj.id}`);
      toast.success(`Deleted output ${obj.name}`);
      if (previewObj?.id === obj.id) setPreviewObj(null);
      refresh();
    } catch (err) {
      toast.error(`Delete failed: ${(err as Error).message || err}`);
    }
  };

  const onCleanAndPromote = async (obj: StorageObject) => {
    try {
      const res = await api.post<{ workflow: Record<string, unknown> }>(
        '/api/storage/scaffold-cleanup',
        { object_id: obj.id, target_schema: 'default' },
      );
      if (!res?.workflow) {
        toast.error('Couldn\'t create a pipeline from this file.');
        return;
      }
      sessionStorage.setItem('fpulse_pending_import', JSON.stringify(res.workflow));
      sessionStorage.setItem(
        'fpulse_pending_import_source',
        `storage:clean:${obj.name}`,
      );
      toast.success(`Opening Data Prep for ${obj.name}...`);
      window.location.hash = 'editor';
    } catch (err) {
      toast.error(`Data Prep failed: ${(err as Error).message || err}`);
    }
  };

  const onCleanup = async () => {
    if (!summary) return;
    try {
      const dry = await api.post<{ purge_count: number; purge_bytes: number }>(
        '/api/storage/cleanup',
        { kind: 'trash', older_than_days: 30, dry_run: true },
      );
      if (!dry.purge_count) {
        toast.info('Nothing to clean up (trash is empty or younger than 30 days).');
        return;
      }
      const ok = await uiConfirm({
        title: `Permanently delete ${dry.purge_count} item${dry.purge_count === 1 ? '' : 's'}?`,
        message: `This will free ${formatBytes(dry.purge_bytes)} from trash. Cannot be undone.`,
        confirmLabel: 'Delete forever',
        destructive: true,
      });
      if (!ok) return;
      await api.post('/api/storage/cleanup', {
        kind: 'trash',
        older_than_days: 30,
        dry_run: false,
      });
      toast.success(`Cleaned up ${dry.purge_count} files`);
      refresh();
    } catch (err) {
      toast.error(`Cleanup failed: ${(err as Error).message || err}`);
    }
  };

  // Re-index files written directly to disk (external process / raw sink)
  // that aren't showing in the catalog yet. Then refresh the page data.
  const onRescan = async () => {
    try {
      const res = await api.storageRescan();
      refresh();
      if (res.total_indexed > 0) {
        toast.success(
          `Rescan complete — indexed ${res.total_indexed} new file${res.total_indexed === 1 ? '' : 's'}`,
        );
      } else {
        toast.info('Rescan complete — no new files found');
      }
    } catch (err) {
      toast.error(`Rescan failed: ${(err as Error).message || err}`);
    }
  };

  // ── Project-scope filtering (Y11) ─────────────────────────────────────

  const matchesScope = (objProjectId: string | null): boolean => {
    if (scopeFilter === 'all') return true;
    if (scopeFilter === 'global') return !objProjectId;
    // 'project' — only show items belonging to the active project.
    if (!projectId) return false;
    return objProjectId === projectId;
  };

  const visibleFiles = useMemo(() => {
    const q = filesSearch.trim().toLowerCase();
    return files.filter((f) => {
      if (!matchesScope(f.project_id)) return false;
      if (!q) return true;
      return (
        f.name.toLowerCase().includes(q) ||
        (f.format || '').toLowerCase().includes(q) ||
        (f.description || '').toLowerCase().includes(q) ||
        (f.tags || []).some((t) => t.toLowerCase().includes(q))
      );
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [files, scopeFilter, projectId, filesSearch]);

  const visibleTables = useMemo(() => {
    const q = tablesSearch.trim().toLowerCase();
    if (!q) return tables;
    return tables.filter(
      (t) =>
        t.name.toLowerCase().includes(q) ||
        t.schema_name.toLowerCase().includes(q) ||
        (t.description || '').toLowerCase().includes(q),
    );
  }, [tables, tablesSearch]);

  const visibleOutputs = useMemo(() => {
    const q = outputsSearch.trim().toLowerCase();
    if (!q) return outputs;
    return outputs
      .map((g) => ({
        ...g,
        objects: g.objects.filter(
          (o) =>
            o.name.toLowerCase().includes(q) ||
            (g.pipeline_id || '').toLowerCase().includes(q) ||
            (g.run_id || '').toLowerCase().includes(q),
        ),
      }))
      .filter((g) => g.objects.length > 0);
  }, [outputs, outputsSearch]);

  return (
    <div className={`flex-1 overflow-auto ${dark ? 'bg-[#0B1220]' : 'bg-canvas-bg'}`}>
      {/* ── Canonical page header (shared PageHeader shell) ─────────── */}
      <PageHeader
        environment={environment}
        icon={<span className={isProd ? 'text-red-400' : 'text-blue-500'}>{activeTab.icon}</span>}
        title={activeTab.label}
        titleAccessory={<TierChip tier={tier} environment={environment} />}
        subtitle={activeTab.subtitle}
        tabs={
          <div className="flex gap-0.5 justify-center items-center">
            {TABS.map((t) => {
              const active = tab === t.key;
              return (
                <button
                  key={t.key}
                  onClick={() => setTab(t.key)}
                  className={`flex items-center gap-2 px-4 py-2.5 text-sm font-semibold rounded-lg transition-all capitalize whitespace-nowrap [&>svg]:shrink-0 ${
                    active
                      ? dark
                        ? 'border-violet-400 text-violet-200 font-bold bg-gradient-to-b from-violet-400/30 to-violet-600/20 shadow-[inset_0_0_0_1.5px_rgba(167,139,250,0.55),inset_0_0_10px_rgba(139,92,246,0.30),inset_0_1px_0_rgba(255,255,255,0.22)]'
                        : 'text-white font-bold bg-gradient-to-b from-slate-600 to-slate-800 shadow-[inset_0_0_0_1.5px_rgba(148,163,184,0.65),inset_0_0_10px_rgba(100,116,139,0.35),inset_0_1px_0_rgba(255,255,255,0.22)]'
                      : dark
                        ? 'border-transparent text-slate-500 hover:text-slate-300 hover:bg-white/[0.03]'
                        : 'border-transparent text-slate-900 font-bold hover:text-violet-700 hover:bg-violet-50/50'
                  }`}
                >
                  {t.icon} {t.label}
                </button>
              );
            })}
          </div>
        }
        actions={
          <div className="flex items-center gap-2">
            <button
              onClick={onRescan}
              title="Index files written directly to disk that aren't showing yet."
              className={`px-3 py-2 text-sm font-semibold rounded-lg border transition-colors inline-flex items-center gap-1.5 ${
                dark
                  ? 'bg-white/[0.06] border-white/15 text-slate-200 hover:bg-white/10'
                  : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50'
              }`}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" />
                <path d="M21 3v5h-5" />
                <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" />
                <path d="M8 16H3v5" />
              </svg>
              Rescan
            </button>
            {tab === 'files' && (
              <button
                onClick={onUploadClick}
                className="px-4 py-2 text-white text-sm font-bold rounded-lg transition-all shadow-sm hover:shadow-md"
                style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
              >
                + Upload file
              </button>
            )}
          </div>
        }
      />

      {/* Y11: project context bar (only when a project is active) */}
      <ProjectContextBar
        projectId={projectId}
        projectName={projectName}
        onGoToProjects={onGoToProjects || (() => {})}
        onClear={onClearProject || (() => {})}
      />

      <div
        className="w-full max-w-[1500px] mx-auto px-6 py-5 transition-all"
        style={previewObj ? { paddingBottom: previewHeight + 24 } : undefined}
      >
        {/* D5 — Storage-as-data-home intro banner (round 1). Surfaces
            the Connect → Store → Transform → Reuse loop so first-time
            visitors understand how the four tabs fit together. The
            Where-am-I audit flagged this narrative as implicit. */}
        <div className={`rounded-lg border mb-4 px-4 py-2.5 flex items-center gap-3 text-xs ${
          dark
            ? 'bg-slate-800/40 border-slate-700/60 text-slate-300'
            : 'bg-gradient-to-r from-blue-50 via-emerald-50 to-indigo-50 border-slate-200 text-slate-700'
        }`}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={dark ? 'text-emerald-400 shrink-0' : 'text-emerald-600 shrink-0'}>
            <path d="M3 7V5a2 2 0 0 1 2-2h2" />
            <path d="M17 3h2a2 2 0 0 1 2 2v2" />
            <path d="M21 17v2a2 2 0 0 1-2 2h-2" />
            <path d="M7 21H5a2 2 0 0 1-2-2v-2" />
            <rect x="7" y="7" width="10" height="10" rx="1" />
          </svg>
          <span className="leading-relaxed">
            <strong className={dark ? 'text-slate-100' : 'text-slate-800'}>Workspace data home.</strong>{' '}
            <strong>Files</strong> are raw uploads · <strong>Managed Tables</strong> are reusable
            Parquet datasets built from a file — promoted as-is, or transformed first in the Data
            Wrangler — then read + written by pipelines ·{' '}
            <strong>Pipeline Outputs</strong> are per-run artefacts ·{' '}
            <strong>Trash</strong> holds soft-deleted files for 30 days. Promote a file → managed
            table to reuse data across pipelines without re-uploading.
          </span>
        </div>

        {/* KPI strip */}
        {summary && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5">
            <HeroCard
              gradient={isProd ? 'from-blue-500 to-sky-600' : 'from-blue-400 to-sky-500'}
              icon={
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                  <polyline points="14 2 14 8 20 8" />
                </svg>
              }
              dense
              label="Files"
              value={String(summary.file_count)}
              footer={formatBytes(summary.file_size_bytes)}
            />
            <HeroCard
              gradient={isProd ? 'from-emerald-500 to-emerald-600' : 'from-emerald-400 to-emerald-500'}
              icon={
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <ellipse cx="12" cy="5" rx="9" ry="3" />
                  <path d="M3 5v6c0 1.66 4.03 3 9 3s9-1.34 9-3V5" />
                  <path d="M3 11v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6" />
                </svg>
              }
              dense
              label="Managed Tables"
              value={String(summary.table_count)}
              footer={formatBytes(summary.table_size_bytes)}
            />
            <HeroCard
              gradient={isProd ? 'from-indigo-500 to-indigo-600' : 'from-indigo-400 to-indigo-500'}
              icon={
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M3 3h18v18H3z" />
                  <path d="M3 9h18" />
                  <path d="M9 21V9" />
                </svg>
              }
              dense
              label="Pipeline Outputs"
              value={String(summary.output_count)}
              footer={formatBytes(summary.output_size_bytes)}
            />
            <HeroCard
              gradient={isProd ? 'from-slate-400 to-slate-500' : 'from-slate-300 to-slate-400'}
              icon={
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="3 6 5 6 21 6" />
                  <path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6" />
                  <path d="M10 11v6" />
                  <path d="M14 11v6" />
                </svg>
              }
              dense
              label="Trash"
              value={String(summary.trash_count)}
              footer={formatBytes(summary.trash_size_bytes)}
            />
          </div>
        )}

        {loading ? (
          <DelayedSkeleton>
            <div className="h-64 rounded-lg bg-white border border-slate-200 animate-pulse" />
          </DelayedSkeleton>
        ) : tab === 'files' ? (
          <FilesTab
            files={visibleFiles}
            tables={tables}
            showDeleted={showDeleted}
            onToggleShowDeleted={setShowDeleted}
            scopeFilter={scopeFilter}
            onScopeFilterChange={setScopeFilter}
            projectId={projectId}
            projectName={projectName}
            usage={usage.files}
            searchValue={filesSearch}
            onSearchChange={setFilesSearch}
            colState={filesColState}
            onPreview={onPreviewObject}
            onPromote={setPromoteObj}
            onDelete={onDeleteFile}
            onReplace={onReplaceClick}
            onRestore={onRestoreFile}
            onCleanup={onCleanAndPromote}
            onShowUsage={(obj, pipelines) =>
              setUsagePopover({
                kind: 'file',
                id: obj.id,
                title: obj.name,
                pipelines,
              })
            }
          />
        ) : tab === 'tables' ? (
          <TablesTab
            tables={visibleTables}
            objects={files}
            usage={usage.tables}
            searchValue={tablesSearch}
            onSearchChange={setTablesSearch}
            colState={tablesColState}
            onDrop={onDropTable}
            onPreview={onPreviewTable}
            onShowUsage={(t, pipelines) =>
              setUsagePopover({
                kind: 'table',
                id: t.id,
                title: `${t.schema_name}.${t.name}`,
                pipelines,
              })
            }
            onEdit={setEditTable}
            onOpenDataPrep={onOpenTableDataPrep}
          />
        ) : tab === 'outputs' ? (
          <OutputsTab
            outputs={visibleOutputs}
            searchValue={outputsSearch}
            onSearchChange={setOutputsSearch}
            onPreview={onPreviewObject}
            onPromote={setPromoteObj}
            onDelete={onDeleteOutput}
            usage={usage.files}
            onShowUsage={(obj, pipelines) =>
              setUsagePopover({
                kind: 'file',
                id: obj.id,
                title: obj.name,
                pipelines,
              })
            }
          />
        ) : (
          <QueryTab tables={tables} />
        )}

        {summary && summary.trash_count > 0 && (
          <div className="mt-5 bg-white rounded-lg border border-slate-200/60 px-4 py-3 text-sm flex items-center justify-between shadow-sm">
            <span className="text-slate-500">
              {summary.trash_count} trashed file{summary.trash_count === 1 ? '' : 's'} ·{' '}
              {formatBytes(summary.trash_size_bytes)}
            </span>
            <button
              onClick={onCleanup}
              className="px-4 py-2 text-sm font-medium rounded-lg border bg-white text-slate-600 border-slate-200 hover:bg-slate-50 transition-colors"
            >
              Clean up files older than 30 days
            </button>
          </div>
        )}
      </div>

      {previewObj && (
        <StoragePreviewDrawer
          object={previewObj}
          resourceKind={previewResourceKind}
          onClose={() => setPreviewObj(null)}
          height={previewHeight}
          onHeightChange={(h) => {
            setPreviewHeight(h);
            try {
              localStorage.setItem('fpulse_storage_preview_height', String(h));
            } catch {
              // localStorage disabled — height resets next mount, harmless.
            }
          }}
        />
      )}
      {promoteObj && (
        <StoragePromoteDialog
          object={promoteObj}
          existingSchemas={Array.from(new Set(tables.map((t) => t.schema_name)))}
          onClose={() => setPromoteObj(null)}
          onPromoted={() => {
            setPromoteObj(null);
            refresh();
            setTab('tables');
          }}
        />
      )}
      {usagePopover && (
        <UsagePopover popover={usagePopover} onClose={() => setUsagePopover(null)} />
      )}
      {showUpload && (
        <StorageUploadDialog
          defaultProjectId={projectId}
          defaultProjectName={projectName}
          onClose={() => setShowUpload(false)}
          onUploaded={() => {
            setShowUpload(false);
            refresh();
          }}
        />
      )}
      {editTable && (
        <StorageTableEditDialog
          table={editTable}
          consumers={usage.tables[editTable.id] || []}
          sourceFile={
            editTable.created_from_object_id
              ? files.find((f) => f.id === editTable.created_from_object_id) || null
              : null
          }
          onClose={() => setEditTable(null)}
          onSaved={() => {
            setEditTable(null);
            refresh();
          }}
          onNavigate={(target) => {
            // Z24 — jump to the source file row. Switch to the Files
            // tab so the user can see it; the row scroll-into-view is
            // handled by Files tab's own focus logic if any.
            if (target.kind === 'file') {
              setTab('files');
              setEditTable(null);
            } else if (target.kind === 'pipeline') {
              window.location.hash = `editor/${target.workflow_id}`;
            }
          }}
        />
      )}
    </div>
  );
}

// ── Canonical table chrome (Y13) ─────────────────────────────────────────
//
// Shared between Files / Tables / Outputs panels so they read as one
// visual family with Pipelines and Connections. Matches the navy-blue
// gradient + amber-text pattern those pages established.

// Standard <th> class used in every Storage thead — matches Connections.
const TH_BASE =
  'text-xs font-bold text-amber-300 uppercase tracking-wider px-5 py-3 whitespace-nowrap';

// ── Scope filter chip strip (Y11) ────────────────────────────────────────
//
// Renders inside the dark TableToolbarStrip — uses white-on-dark
// styling instead of slate-on-light so it reads on the navy backdrop.

function ScopeFilterStrip({
  value,
  onChange,
  projectId,
  projectName,
}: {
  value: ScopeFilter;
  onChange: (v: ScopeFilter) => void;
  projectId: string | null | undefined;
  projectName?: string;
}) {
  const opts: Array<{ key: ScopeFilter; label: string; disabled?: boolean }> = [
    { key: 'all', label: 'All' },
    { key: 'global', label: 'Global' },
    {
      key: 'project',
      label: projectId ? `Project: ${projectName || 'current'}` : 'Project',
      disabled: !projectId,
    },
  ];
  return (
    <div className="flex items-center gap-1">
      {opts.map((o) => {
        const active = value === o.key;
        return (
          <button
            key={o.key}
            onClick={() => !o.disabled && onChange(o.key)}
            disabled={o.disabled}
            title={o.disabled ? 'Pick a project to enable this filter' : undefined}
            className={`px-2.5 py-1 text-xs font-semibold rounded-md transition-colors border ${
              active
                ? 'bg-amber-400 text-slate-900 border-amber-400'
                : o.disabled
                  ? 'text-white/30 cursor-not-allowed border-transparent'
                  : 'text-white/85 hover:bg-white/10 border-white/20'
            }`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

// ── Used-by pill (Y12) ───────────────────────────────────────────────────

function UsedByPill({
  count,
  onClick,
}: {
  count: number;
  onClick?: () => void;
}) {
  if (!count) {
    // Still clickable at 0 so the side panel opens and confirms "no
    // pipelines use this yet" — otherwise the column reads as inert.
    return (
      <button
        onClick={onClick}
        title="No pipelines use this yet — click to view"
        className="text-slate-400 hover:text-blue-600 text-xs hover:underline underline-offset-2"
      >
        —
      </button>
    );
  }
  return (
    <button
      onClick={onClick}
      className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-semibold rounded-full bg-blue-50 text-blue-700 hover:bg-blue-100 transition-colors"
    >
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
        <rect width="8" height="8" x="3" y="3" rx="2" />
        <path d="M7 11v4a2 2 0 0 0 2 2h4" />
        <rect width="8" height="8" x="13" y="13" rx="2" />
      </svg>
      {count}
    </button>
  );
}

// ── Used-by drill-down popover (Y12) ─────────────────────────────────────

function UsagePopover({
  popover,
  onClose,
}: {
  popover: {
    kind: 'file' | 'table';
    id: string;
    title: string;
    pipelines: UsageRef[];
  };
  onClose: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-slate-900/40 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      {/* Right-side slide-in panel (was a centred modal). Clicking a
          table's USED BY count lists the referencing pipelines here. */}
      <div className="bg-white shadow-2xl border-l border-slate-200 w-[420px] max-w-[92vw] h-full flex flex-col overflow-hidden">
        <div className="px-5 py-4 border-b border-slate-200/70 bg-gradient-to-b from-slate-50 to-white">
          <div className="text-xs uppercase tracking-wide text-slate-500 font-semibold">
            {popover.kind === 'file' ? 'File usage' : 'Table usage'}
          </div>
          <div className="text-base font-bold text-slate-900 font-mono mt-0.5">
            {popover.title}
          </div>
          <div className="text-xs text-slate-500 mt-1">
            {popover.pipelines.length} pipeline
            {popover.pipelines.length === 1 ? '' : 's'} reference{popover.pipelines.length === 1 ? 's' : ''} this {popover.kind}.
          </div>
        </div>
        <div className="px-5 py-3 flex-1 overflow-auto">
          {popover.pipelines.length === 0 ? (
            <div className="text-sm text-slate-500 text-center py-6">
              No pipelines reference this {popover.kind}. Safe to drop.
            </div>
          ) : (
            <ul className="divide-y divide-slate-100">
              {popover.pipelines.map((p, i) => (
                <li
                  key={`${p.workflow_id}_${i}`}
                  className="flex items-center justify-between py-2.5 gap-3"
                >
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-slate-900 truncate">{p.name}</div>
                    {p.via_table && (
                      <div className="text-xs text-slate-500 mt-0.5">
                        via managed table{' '}
                        <code className="font-mono px-1 rounded bg-slate-100 text-slate-700">
                          {p.via_table}
                        </code>
                      </div>
                    )}
                  </div>
                  {p.workflow_id && (
                    <button
                      onClick={() => {
                        window.location.hash = `editor/${p.workflow_id}`;
                        onClose();
                      }}
                      className="text-xs font-semibold text-blue-600 hover:text-blue-700 whitespace-nowrap"
                    >
                      Open →
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="px-5 py-3 border-t border-slate-200/70 bg-slate-50/60 flex justify-end">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium rounded-lg text-slate-600 hover:bg-slate-200/70"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Scope cell helper ────────────────────────────────────────────────────

function ScopeCell({
  objProjectId,
  activeProjectId,
  activeProjectName,
}: {
  objProjectId: string | null;
  activeProjectId?: string | null;
  activeProjectName?: string;
}) {
  if (!objProjectId) {
    return (
      <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full bg-slate-100 text-slate-600">
        Global
      </span>
    );
  }
  const label =
    objProjectId === activeProjectId && activeProjectName
      ? activeProjectName
      : 'Project';
  return (
    <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full bg-blue-50 text-blue-700">
      {label}
    </span>
  );
}

// ── Files tab ────────────────────────────────────────────────────────────

type FilesColState = ReturnType<typeof useTableColumns>;

function FilesTab({
  files,
  tables,
  showDeleted,
  onToggleShowDeleted,
  scopeFilter,
  onScopeFilterChange,
  projectId,
  projectName,
  usage,
  searchValue,
  onSearchChange,
  colState,
  onPreview,
  onPromote,
  onDelete,
  onReplace,
  onRestore,
  onShowUsage,
  onCleanup,
}: {
  files: StorageObject[];
  // Z33 (2026-05-23) — passed in so each file row can show a
  // "Prepared as schema.name" badge linking to the managed table that
  // was produced from this file by a Storage Z1 pipeline. The join is
  // on `table.prep_source_object_id === file.id`.
  tables: StorageTable[];
  showDeleted: boolean;
  onToggleShowDeleted: (v: boolean) => void;
  scopeFilter: ScopeFilter;
  onScopeFilterChange: (v: ScopeFilter) => void;
  projectId?: string | null;
  projectName?: string;
  usage: Record<string, UsageRef[]>;
  searchValue: string;
  onSearchChange: (v: string) => void;
  colState: FilesColState;
  onPreview: (obj: StorageObject) => void;
  onPromote: (obj: StorageObject) => void;
  onDelete: (obj: StorageObject) => void;
  onReplace: (obj: StorageObject) => void;
  onRestore: (obj: StorageObject) => void;
  onShowUsage: (obj: StorageObject, pipelines: UsageRef[]) => void;
  onCleanup: (obj: StorageObject) => void;
}) {
  const isV = colState.isVisible;
  // Z33: index of managed tables by their prep source file id. Built
  // once per render — workspace-scoped tables list is short. A single
  // file usually maps to a single prepared table (re-runs overwrite);
  // we surface the first match if there are several.
  const preparedTableByFile = new Map<string, StorageTable>();
  for (const t of tables) {
    if (t.prep_source_object_id && !preparedTableByFile.has(t.prep_source_object_id)) {
      preparedTableByFile.set(t.prep_source_object_id, t);
    }
  }
  return (
    <div className="bg-white rounded-lg border border-slate-200 overflow-x-auto shadow-sm">
      {/* Canonical TableToolbar — search + columns picker + multi-level
          export, in the navy-blue/amber theme. Custom children slot holds
          the "Show deleted" toggle + Scope filter chips. */}
      <TableToolbar
        data={files}
        columns={FILE_COLUMNS}
        columnGroups={FILE_COLUMN_GROUPS}
        visibleColumns={colState.visibleColumns}
        activeColumnCount={colState.activeColumns.length}
        onToggleColumn={colState.toggleColumn}
        onResetDefaults={colState.resetToDefaults}
        onSelectAll={colState.selectAll}
        searchValue={searchValue}
        onSearchChange={onSearchChange}
        searchPlaceholder="Search files..."
        exportRowBuilder={(o: StorageObject) => ({
          id: o.id,
          name: o.name,
          format: o.format ?? '',
          scope: o.project_id ? 'project' : 'global',
          size_bytes: o.size_bytes,
          row_count: o.row_count ?? '',
          column_count: o.column_count ?? '',
          used_by_count: (usage[o.id] || []).length,
          path: o.path,
          tags: (o.tags || []).join('; '),
          description: o.description,
          created_at: o.created_at,
          updated_at: o.updated_at,
          deleted_at: o.deleted_at ?? '',
        })}
        exportFilename="storage_files"
        recordLabel="file"
        projectGrouper={(o: StorageObject) => o.project_id || 'global'}
      >
        <label className="flex items-center gap-2 text-xs text-white/85 cursor-pointer mr-1 whitespace-nowrap">
          <input
            type="checkbox"
            checked={showDeleted}
            onChange={(e) => onToggleShowDeleted(e.target.checked)}
            className="rounded border-white/30 bg-white/10 text-amber-400 focus:ring-amber-400/40"
          />
          Show deleted
        </label>
        <ScopeFilterStrip
          value={scopeFilter}
          onChange={onScopeFilterChange}
          projectId={projectId}
          projectName={projectName}
        />
      </TableToolbar>

      {files.length === 0 ? (
        <div className="text-center py-16 px-6 text-sm text-slate-500">
          {showDeleted ? (
            'No deleted files.'
          ) : searchValue.trim() ? (
            <>No files match "<b>{searchValue}</b>".</>
          ) : (
            <>
              <div>No files yet. Click <b>+ Upload file</b> in the top right to add one.</div>
              <div className="text-xs text-slate-400 mt-3 max-w-md mx-auto">
                Storage holds data files (CSV, Parquet, Excel, records-shaped JSON).
                Pipeline definitions belong under <b>Workflows → Import</b>.
              </div>
            </>
          )}
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b-2 border-amber-400/40 bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900">
                {isV('name')    && <th className={TH_BASE + ' text-left'}>Name</th>}
                {isV('format')  && <th className={TH_BASE + ' text-left'}>Format</th>}
                {isV('scope')   && <th className={TH_BASE + ' text-left'}>Scope</th>}
                {isV('size')    && <th className={TH_BASE + ' text-right'}>Size</th>}
                {isV('rows')    && <th className={TH_BASE + ' text-right'}>Rows</th>}
                {isV('columns') && <th className={TH_BASE + ' text-right'}>Columns</th>}
                {isV('used_by') && <th className={TH_BASE + ' text-left'}>Used by</th>}
                {isV('updated') && <th className={TH_BASE + ' text-left'}>Updated</th>}
                {isV('created') && <th className={TH_BASE + ' text-left'}>Created</th>}
                {isV('path')    && <th className={TH_BASE + ' text-left'}>Path</th>}
                {isV('tags')    && <th className={TH_BASE + ' text-left'}>Tags</th>}
                {isV('actions') && <th className={TH_BASE + ' text-right'}>Actions</th>}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {files.map((obj) => {
                const pipelines = usage[obj.id] || [];
                // Z33 (2026-05-23) — back-link to the prepared managed
                // table (if any). The badge renders below the file name
                // with a clickable arrow that scrolls/navigates to the
                // table in the Managed Tables tab.
                const preparedTable = preparedTableByFile.get(obj.id);
                return (
                  <tr
                    key={obj.id}
                    className={`hover:bg-slate-50/60 transition-colors ${
                      obj.deleted_at ? 'opacity-60' : ''
                    }`}
                  >
                    {isV('name') && (
                      <td className="px-5 py-3 max-w-[300px]">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-medium text-slate-900 truncate" title={obj.name}>{obj.name}</span>
                          {preparedTable && (
                            <span
                              className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-semibold rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200"
                              title={`This file has a prepared managed table: ${preparedTable.schema_name}.${preparedTable.name}`}
                            >
                              <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                                <polyline points="20 6 9 17 4 12" />
                              </svg>
                              Prepared as&nbsp;
                              <span className="font-mono truncate max-w-[180px]">
                                {preparedTable.schema_name}.{preparedTable.name}
                              </span>
                            </span>
                          )}
                        </div>
                        {obj.description && (
                          <div className="text-xs text-slate-500 mt-0.5">{obj.description}</div>
                        )}
                      </td>
                    )}
                    {isV('format') && (
                      <td className="px-4 py-3 text-xs uppercase tracking-wide text-slate-500">
                        {obj.format || '—'}
                      </td>
                    )}
                    {isV('scope') && (
                      <td className="px-4 py-3">
                        <ScopeCell
                          objProjectId={obj.project_id}
                          activeProjectId={projectId}
                          activeProjectName={projectName}
                        />
                      </td>
                    )}
                    {isV('size') && (
                      <td className="px-4 py-3 text-right text-slate-600 tabular-nums">
                        {formatBytes(obj.size_bytes)}
                      </td>
                    )}
                    {isV('rows') && (
                      <td className="px-4 py-3 text-right text-slate-600 tabular-nums">
                        {obj.row_count != null ? obj.row_count.toLocaleString() : '—'}
                      </td>
                    )}
                    {isV('columns') && (
                      <td className="px-4 py-3 text-right text-slate-600 tabular-nums">
                        {obj.column_count != null ? obj.column_count : '—'}
                      </td>
                    )}
                    {isV('used_by') && (
                      <td className="px-4 py-3">
                        <UsedByPill
                          count={pipelines.length}
                          onClick={() => onShowUsage(obj, pipelines)}
                        />
                      </td>
                    )}
                    {isV('updated') && (
                      <td className="px-4 py-3 text-xs text-slate-500">
                        {formatDate(obj.updated_at || obj.created_at)}
                      </td>
                    )}
                    {isV('created') && (
                      <td className="px-4 py-3 text-xs text-slate-500">
                        {formatDate(obj.created_at)}
                      </td>
                    )}
                    {isV('path') && (
                      <td className="px-4 py-3 text-xs font-mono text-slate-500 truncate max-w-[280px]">
                        {obj.path}
                      </td>
                    )}
                    {isV('tags') && (
                      <td className="px-4 py-3 text-xs text-slate-500">
                        {(obj.tags && obj.tags.length > 0) ? obj.tags.join(', ') : '—'}
                      </td>
                    )}
                    {isV('actions') && (
                      <td className="px-5 py-3">
                        <div className="flex justify-end gap-0.5">
                          {obj.deleted_at ? (
                            <RowActionButton
                              title="Restore from trash"
                              tone="blue"
                              onClick={() => onRestore(obj)}
                            >
                              <RestoreIcon />
                            </RowActionButton>
                          ) : (
                            <>
                              <RowActionButton
                                title="Preview rows + schema"
                                tone="blue"
                                onClick={() => onPreview(obj)}
                              >
                                <EyeIcon />
                              </RowActionButton>
                              <RowActionButton
                                title="Replace bytes (same id)"
                                tone="indigo"
                                onClick={() => onReplace(obj)}
                              >
                                <UploadIcon />
                              </RowActionButton>
                              <RowActionButton
                                title="Prepare Data - clean this file and load it to a managed table"
                                tone="amber"
                                label="Prep"
                                onClick={() => onCleanup(obj)}
                              >
                                <CleanupIcon />
                              </RowActionButton>
                              <RowActionButton
                                title="Promote — convert the file straight to a managed Parquet table with no transformation"
                                tone="green"
                                label="Promote"
                                onClick={() => onPromote(obj)}
                              >
                                <PromoteIcon />
                              </RowActionButton>
                              <RowActionButton
                                title="Move to trash"
                                tone="red"
                                onClick={() => onDelete(obj)}
                              >
                                <TrashIcon />
                              </RowActionButton>
                            </>
                          )}
                        </div>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Managed Tables tab ────────────────────────────────────────────────────

function TablesTab({
  tables,
  objects,
  usage,
  searchValue,
  onSearchChange,
  colState,
  onDrop,
  onPreview,
  onShowUsage,
  onEdit,
  onOpenDataPrep,
}: {
  tables: StorageTable[];
  // Z33 (2026-05-23) — `objects` is the Files-tab data, threaded in so
  // the Managed Tables row can resolve `prep_source_object_id` → file
  // name for the "From {filename}" badge. Keeps the join client-side
  // (no API change).
  objects: StorageObject[];
  usage: Record<string, UsageRef[]>;
  searchValue: string;
  onSearchChange: (v: string) => void;
  colState: FilesColState;
  onDrop: (t: StorageTable) => void;
  onPreview: (t: StorageTable) => void;
  onShowUsage: (t: StorageTable, pipelines: UsageRef[]) => void;
  // Z22 (2026-05-23): Edit description + tags via PATCH /tables/{id}.
  onEdit: (t: StorageTable) => void;
  // Z22: jump to the writer pipeline (the data-prep recipe that fills
  // this table). Disabled when no workflow has a sink targeting it.
  onOpenDataPrep: (t: StorageTable) => void;
}) {
  // Z33: lookup map for the "From {filename}" badge. Computed once
  // per render (objects list is small — workspace-scoped uploads).
  const objectsById = new Map<string, StorageObject>(objects.map((o) => [o.id, o]));
  const isV = colState.isVisible;
  return (
    <div className="bg-white rounded-lg border border-slate-200 overflow-x-auto shadow-sm">
      <TableToolbar
        data={tables}
        columns={TABLE_COLUMNS}
        columnGroups={TABLE_COLUMN_GROUPS}
        visibleColumns={colState.visibleColumns}
        activeColumnCount={colState.activeColumns.length}
        onToggleColumn={colState.toggleColumn}
        onResetDefaults={colState.resetToDefaults}
        onSelectAll={colState.selectAll}
        searchValue={searchValue}
        onSearchChange={onSearchChange}
        searchPlaceholder="Search tables..."
        exportRowBuilder={(t: StorageTable) => ({
          id: t.id,
          schema: t.schema_name,
          name: t.name,
          row_count: t.row_count,
          column_count: t.column_count,
          size_bytes: t.size_bytes,
          part_count: t.part_count,
          used_by_count: (usage[t.id] || []).length,
          description: t.description,
          tags: (t.tags || []).join('; '),
          created_at: t.created_at,
        })}
        exportFilename="storage_managed_tables"
        recordLabel="table"
      />
      {tables.length === 0 ? (
        <div className="text-center py-16 text-sm text-slate-500">
          {searchValue.trim() ? (
            <>No tables match "<b>{searchValue}</b>".</>
          ) : (
            <>No managed tables yet. Upload a file in the Files tab, then click <b>Promote</b>.</>
          )}
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b-2 border-amber-400/40 bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900">
                {isV('schema')  && <th className={TH_BASE + ' text-left'}>Schema</th>}
                {isV('name')    && <th className={TH_BASE + ' text-left'}>Table</th>}
                {isV('rows')    && <th className={TH_BASE + ' text-right'}>Rows</th>}
                {isV('columns') && <th className={TH_BASE + ' text-right'}>Columns</th>}
                {isV('size')    && <th className={TH_BASE + ' text-right'}>Size</th>}
                {isV('parts')   && <th className={TH_BASE + ' text-right'}>Parts</th>}
                {isV('used_by') && <th className={TH_BASE + ' text-left'}>Used by</th>}
                {isV('created') && <th className={TH_BASE + ' text-left'}>Created</th>}
                {isV('description') && <th className={TH_BASE + ' text-left'}>Description</th>}
                {isV('actions') && <th className={TH_BASE + ' text-right'}>Actions</th>}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {tables.map((t) => {
                const pipelines = usage[t.id] || [];
                // Z33 (2026-05-23) — resolve the prep-source file's name
                // for the badge. The map covers files in the current
                // workspace; if the file was hard-deleted, we still show
                // the badge but with "deleted source" as the label so the
                // table still reads as "preparation result" not "manual
                // promote".
                const prepSourceObj = t.prep_source_object_id
                  ? objectsById.get(t.prep_source_object_id)
                  : undefined;
                const prepStepCount = Array.isArray(t.prep_recipe) ? t.prep_recipe.length : 0;
                const showPrepBadge = !!t.prep_source_object_id;
                // Z36 (2026-05-23) — sibling badge for the Y5 Promote
                // path (direct file → table copy, no Wrangler). Only
                // shown when the prep badge isn't already showing,
                // since both badges would carry the same source-file
                // info. Slate tone (less saturated than Z33's violet)
                // so the user can distinguish "promoted directly" from
                // "cleaned + promoted" at a glance.
                const promoteSourceObj = !showPrepBadge && t.created_from_object_id
                  ? objectsById.get(t.created_from_object_id)
                  : undefined;
                const showPromoteBadge = !showPrepBadge && !!t.created_from_object_id;
                return (
                  <tr
                    key={t.id}
                    className="hover:bg-slate-50/60 transition-colors"
                  >
                    {isV('schema') && (
                      <td className="px-5 py-3 text-slate-600 font-mono text-xs">{t.schema_name}</td>
                    )}
                    {isV('name') && (
                      <td className="px-4 py-3 max-w-[300px]">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-medium text-slate-900 font-mono truncate" title={t.name}>{t.name}</span>
                          {showPrepBadge && (
                            <span
                              className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-semibold rounded-full bg-violet-50 text-violet-700 border border-violet-200"
                              title={
                                prepStepCount > 0
                                  ? `Prepared from ${prepSourceObj?.name || 'a file (deleted)'} · ${prepStepCount} Wrangler step${prepStepCount === 1 ? '' : 's'}`
                                  : `Prepared from ${prepSourceObj?.name || 'a file (deleted)'}`
                              }
                            >
                              <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M3 7l9 6 9-6" />
                                <path d="M3 7v10l9 6 9-6V7" />
                                <line x1="3" y1="7" x2="12" y2="13" />
                              </svg>
                              From&nbsp;<span className="font-mono truncate max-w-[140px]">{prepSourceObj?.name || '(deleted)'}</span>
                              {prepStepCount > 0 && <span className="opacity-70">· {prepStepCount} step{prepStepCount === 1 ? '' : 's'}</span>}
                            </span>
                          )}
                          {showPromoteBadge && (
                            <span
                              className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-semibold rounded-full bg-slate-100 text-slate-600 border border-slate-200"
                              title={`Promoted directly from ${promoteSourceObj?.name || 'a file (deleted)'} (no Wrangler recipe)`}
                            >
                              <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M12 19V5" />
                                <path d="M5 12l7-7 7 7" />
                              </svg>
                              Promoted from&nbsp;<span className="font-mono truncate max-w-[140px]">{promoteSourceObj?.name || '(deleted)'}</span>
                            </span>
                          )}
                        </div>
                      </td>
                    )}
                    {isV('rows') && (
                      <td className="px-4 py-3 text-right text-slate-600 tabular-nums">
                        {t.row_count.toLocaleString()}
                      </td>
                    )}
                    {isV('columns') && (
                      <td className="px-4 py-3 text-right text-slate-600 tabular-nums">
                        {t.column_count}
                      </td>
                    )}
                    {isV('size') && (
                      <td className="px-4 py-3 text-right text-slate-600 tabular-nums">
                        {formatBytes(t.size_bytes)}
                      </td>
                    )}
                    {isV('parts') && (
                      <td className="px-4 py-3 text-right text-slate-600 tabular-nums">
                        {t.part_count}
                      </td>
                    )}
                    {isV('used_by') && (
                      <td className="px-4 py-3">
                        <UsedByPill
                          count={pipelines.length}
                          onClick={() => onShowUsage(t, pipelines)}
                        />
                      </td>
                    )}
                    {isV('created') && (
                      <td className="px-4 py-3 text-xs text-slate-500">
                        {formatDate(t.created_at)}
                      </td>
                    )}
                    {isV('description') && (
                      <td className="px-4 py-3 text-xs text-slate-500 truncate max-w-[240px]">
                        {t.description || '—'}
                      </td>
                    )}
                    {isV('actions') && (() => {
                      const refs = usage[t.id] || [];
                      // Writer = pipeline whose local_table_sink (or
                      // generic destination + connector_type=local_table)
                      // targets this table. Backend usage scanner stamps
                      // role per ref (Z22). Z36 (2026-05-23) — also
                      // honor the directly-stamped prep_workflow_id so
                      // the wand lights up for Z1-scaffolded tables even
                      // if the usage scanner's sink rule missed them.
                      const sinkRefs = refs.filter((r: any) => r?.role === 'sink');
                      const hasWriter = sinkRefs.length > 0 || !!t.prep_workflow_id;
                      const writerCount = sinkRefs.length;
                      return (
                        <td className="px-5 py-3">
                          <div className="flex justify-end gap-0.5">
                            <RowActionButton
                              title="View dataset"
                              tone="blue"
                              onClick={() => onPreview(t)}
                            >
                              <EyeIcon />
                            </RowActionButton>
                            <RowActionButton
                              title="Edit description + tags"
                              tone="indigo"
                              onClick={() => onEdit(t)}
                            >
                              <PencilIcon />
                            </RowActionButton>
                            <RowActionButton
                              title={
                                hasWriter
                                  ? writerCount === 1
                                    ? 'Open the data prep pipeline that fills this table'
                                    : `Pick which of ${writerCount} pipelines to open`
                                  : 'No data prep pipeline writes to this table yet'
                              }
                              tone="amber"
                              disabled={!hasWriter}
                              onClick={() => onOpenDataPrep(t)}
                            >
                              <CleanupIcon />
                            </RowActionButton>
                            <RowActionButton
                              title="Drop table"
                              tone="red"
                              onClick={() => onDrop(t)}
                            >
                              <TrashIcon />
                            </RowActionButton>
                          </div>
                        </td>
                      );
                    })()}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Outputs tab ───────────────────────────────────────────────────────────

function OutputsTab({
  outputs,
  searchValue,
  onSearchChange,
  onPreview,
  onPromote,
  onDelete,
  usage,
  onShowUsage,
}: {
  outputs: OutputGroup[];
  searchValue: string;
  onSearchChange: (v: string) => void;
  onPreview: (obj: StorageObject) => void;
  // Promote a pipeline output → managed table (reuses the Files-tab
  // promote dialog; output objects share the StorageObject shape).
  onPromote: (obj: StorageObject) => void;
  onDelete: (obj: StorageObject) => void;
  usage: Record<string, UsageRef[]>;
  onShowUsage: (obj: StorageObject, pipelines: UsageRef[]) => void;
}) {
  // Outputs are grouped by (pipeline, run), so a flat TableToolbar
  // doesn't fit. Slim search-only strip on top + an Export-all-runs
  // affordance instead.
  const totalObjects = outputs.reduce((sum, g) => sum + g.object_count, 0);
  const exportAll = () => {
    const rows: any[] = [];
    for (const g of outputs) {
      for (const o of g.objects) {
        rows.push({
          pipeline_id: g.pipeline_id,
          run_id: g.run_id,
          name: o.name,
          format: o.format ?? '',
          size_bytes: o.size_bytes,
          path: o.path,
          created_at: o.created_at,
        });
      }
    }
    if (rows.length === 0) return;
    const headers = Object.keys(rows[0]);
    const csv = [
      headers.join(','),
      ...rows.map((r) =>
        headers.map((h) => `"${String(r[h] ?? '').replace(/"/g, '""')}"`).join(','),
      ),
    ].join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `storage_outputs_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };
  const outputsToolbar = (
    <div className="flex items-center justify-between gap-3 px-4 py-2 bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900 border-b border-amber-400/20 rounded-t-lg">
      <span className="text-xs text-amber-200/90 font-medium">
        {outputs.length} run{outputs.length === 1 ? '' : 's'} · {totalObjects} file{totalObjects === 1 ? '' : 's'}
      </span>
      <div className="flex items-center gap-1.5">
        <div className="relative">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#fcd34d" strokeWidth="2"
            className="absolute left-2 top-1/2 -translate-y-1/2 pointer-events-none">
            <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
          <input
            value={searchValue}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="Search outputs..."
            className="pl-7 pr-2.5 py-1.5 text-xs rounded-lg outline-none w-44 bg-white/10 border border-white/20 text-white placeholder:text-white/50 focus:ring-2 focus:ring-amber-300/40 focus:border-amber-400"
          />
        </div>
        <button
          onClick={exportAll}
          disabled={outputs.length === 0}
          className="px-2.5 py-1.5 text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5 bg-white/10 border border-white/20 text-white hover:bg-white/20 disabled:opacity-40 disabled:cursor-not-allowed"
          title="Export all outputs as CSV"
        >
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="7 10 12 15 17 10" />
            <line x1="12" y1="15" x2="12" y2="3" />
          </svg>
          Export
        </button>
      </div>
    </div>
  );
  // 2026-06-08 — flattened to ONE table (toolbar + single <table>) to
  // match the Files / Managed Tables tabs. Was per-run nested cards, each
  // with its own header, which read as a confusing 3-band split. Run +
  // pipeline are now COLUMNS, so the page is structurally identical to its
  // siblings. Rows are flattened across runs; the Run column groups them.
  return (
    <div className="bg-white rounded-lg border border-slate-200 overflow-x-auto shadow-sm">
      {outputsToolbar}
      {outputs.length === 0 ? (
        <div className="text-center py-16 text-sm text-slate-500">
          {searchValue.trim() ? (
            <>No outputs match "<b>{searchValue}</b>".</>
          ) : (
            'No pipeline outputs yet. Files written by pipeline runs appear here.'
          )}
        </div>
      ) : (
        <table className="w-full border-collapse">
          <thead>
            <tr className="border-b-2 border-amber-400/40 bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900">
              <th className={TH_BASE + ' text-left'}>Pipeline</th>
              <th className={TH_BASE + ' text-left'}>Run</th>
              <th className={TH_BASE + ' text-left'}>Name</th>
              <th className={TH_BASE + ' text-left'}>Format</th>
              <th className={TH_BASE + ' text-right'}>Size</th>
              <th className={TH_BASE + ' text-left'}>Created</th>
              <th className={TH_BASE + ' text-left'}>Used by</th>
              <th className={TH_BASE + ' text-right'}>Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {outputs.flatMap((g) =>
              g.objects.map((obj) => (
                <tr key={obj.id} className="hover:bg-slate-50/60 transition-colors">
                  <td className="px-5 py-3 max-w-[220px]">
                    {g.pipeline_name
                      ? <a
                          href={`#editor/${g.pipeline_id}`}
                          className="text-blue-700 hover:text-blue-900 hover:underline underline-offset-2 truncate block"
                          title="Open the pipeline that produced this output"
                        >{g.pipeline_name}</a>
                      : g.pipeline_id
                        ? <span className="italic text-slate-500" title={`Pipeline ${g.pipeline_id} — not found (deleted)`}>Pipeline N/A (deleted)</span>
                        : <span className="italic text-slate-400">Ad-hoc (none)</span>}
                  </td>
                  <td className="px-4 py-3 w-32">
                    <span
                      className="inline-flex items-center px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200 font-mono text-[10px]"
                      title={g.run_id || ''}
                    >
                      {g.run_id ? g.run_id.slice(0, 10) : '—'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-900 max-w-[280px]">
                    <span className="truncate block" title={obj.name}>{obj.name}</span>
                  </td>
                  <td className="px-4 py-3 text-xs uppercase tracking-wide text-slate-500 w-20">
                    {obj.format || '—'}
                  </td>
                  <td className="px-4 py-3 text-right text-slate-600 tabular-nums w-28">
                    {formatBytes(obj.size_bytes)}
                  </td>
                  <td className="px-4 py-3 text-xs text-slate-500 w-40">
                    {formatDate(obj.created_at)}
                  </td>
                  <td className="px-4 py-3">
                    <UsedByPill
                      count={(usage[obj.id] || []).length}
                      onClick={() => onShowUsage(obj, usage[obj.id] || [])}
                    />
                  </td>
                  <td className="px-5 py-3">
                    <div className="flex justify-end gap-0.5">
                      <RowActionButton
                        title="View dataset"
                        tone="blue"
                        onClick={() => onPreview(obj)}
                      >
                        <EyeIcon />
                      </RowActionButton>
                      <RowActionButton
                        title="Promote — make this output reusable as a managed Parquet table"
                        tone="green"
                        label="Promote"
                        onClick={() => onPromote(obj)}
                      >
                        <PromoteIcon />
                      </RowActionButton>
                      <RowActionButton
                        title="Delete output"
                        tone="red"
                        onClick={() => onDelete(obj)}
                      >
                        <TrashIcon />
                      </RowActionButton>
                    </div>
                  </td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ── Icon glyphs used by row actions ──────────────────────────────────────

function EyeIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function UploadIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="17 8 12 3 7 8" />
      <line x1="12" y1="3" x2="12" y2="15" />
    </svg>
  );
}

function PromoteIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <ellipse cx="12" cy="5" rx="9" ry="3" />
      <path d="M3 5v6c0 1.66 4.03 3 9 3s9-1.34 9-3V5" />
      <path d="M3 11v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6" />
    </svg>
  );
}

// Z1 2026-05-23 — wand-with-sparkles icon used for file Data Prep.
// Conveys "fix + improve" without resorting to a literal broom (broom
// reads as "delete" to half the users; wand reads as "make it better").
// Z22 (2026-05-23) — Edit metadata icon for the Managed Tables row.
// Pencil-on-paper, recognisable as "edit text fields" rather than
// "edit the table structure". The dialog only changes description +
// tags; schema/name are non-editable in v1.0.
function PencilIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
    </svg>
  );
}

function CleanupIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      {/* wand */}
      <path d="M15 4V2" />
      <path d="M15 16v-2" />
      <path d="M8 9h2" />
      <path d="M20 9h2" />
      <path d="M17.8 11.8 19 13" />
      <path d="M15 9h0" />
      <path d="M17.8 6.2 19 5" />
      <path d="m3 21 9-9" />
      <path d="M12.2 6.2 11 5" />
    </svg>
  );
}

function RestoreIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 12a9 9 0 1 0 9-9" />
      <polyline points="3 5 3 12 10 12" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6" />
      <path d="M10 11v6" />
      <path d="M14 11v6" />
    </svg>
  );
}

// ── Query tab (read-only SQL over managed tables) ────────────────────────
//
// Additive surface wired to the existing POST /api/storage/query endpoint
// (api.storageQuery). Read-only: the backend only accepts SELECT / WITH
// over the workspace's managed tables, referenced by schema.name (e.g.
// `SELECT * FROM default.sales`). The result-table styling mirrors the
// StoragePreviewDrawer's PreviewTable so it reads as one visual family.

interface QueryResult {
  columns: Array<{ name: string; type: string }>;
  rows: Array<Record<string, unknown>>;
  row_count: number;
  limit: number;
  truncated: boolean;
  tables_available: string[];
}

// Local copy of the numeric-type check + cell renderer from the preview
// drawer so numbers line up right-aligned and null/objects render sanely.
function isNumericQueryType(type: string): boolean {
  return /INT|DOUBLE|FLOAT|DECIMAL|NUMERIC|REAL|SERIAL/.test((type || '').toUpperCase());
}

function renderQueryCell(v: unknown): string {
  if (v === null || v === undefined) return '∅';
  if (typeof v === 'string') return v.length > 200 ? `${v.slice(0, 200)}…` : v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  if (Array.isArray(v) || typeof v === 'object') {
    try {
      const s = JSON.stringify(v);
      return s.length > 200 ? `${s.slice(0, 200)}…` : s;
    } catch {
      return String(v);
    }
  }
  return String(v);
}

function QueryTab({ tables }: { tables: StorageTable[] }) {
  const [sql, setSql] = useState('SELECT *\nFROM default.your_table\nLIMIT 50;');
  const [limit, setLimit] = useState<string>('200');
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<QueryResult | null>(null);

  // Available tables to reference. Prefer the last response's
  // tables_available (authoritative from the backend); fall back to the
  // page's managed-tables list so the hint is populated before the first
  // run. Both are `schema.name` identifiers.
  const tableHints = useMemo<string[]>(() => {
    if (result?.tables_available && result.tables_available.length > 0) {
      return result.tables_available;
    }
    return tables.map((t) => `${t.schema_name}.${t.name}`);
  }, [result, tables]);

  const run = async () => {
    const trimmed = sql.trim();
    if (!trimmed) {
      setError('Enter a SELECT / WITH query to run.');
      return;
    }
    setRunning(true);
    setError(null);
    try {
      const parsedLimit = limit.trim() ? parseInt(limit, 10) : undefined;
      const res = await api.storageQuery(
        trimmed,
        Number.isFinite(parsedLimit as number) ? (parsedLimit as number) : undefined,
      );
      setResult(res);
    } catch (err) {
      // The backend returns HTTP 400 with a `detail` message for bad /
      // non-read-only SQL; request() surfaces that as the Error message.
      setResult(null);
      setError((err as Error).message || 'Query failed.');
    } finally {
      setRunning(false);
    }
  };

  // Ctrl/Cmd+Enter runs — matches the "run" affordance users expect in a
  // SQL editor without needing to reach for the mouse.
  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      if (!running) run();
    }
  };

  const lastColIdx = result ? result.columns.length - 1 : -1;

  return (
    <div className="space-y-4">
      {/* Editor card — dark navy/amber toolbar header to match the other
          Storage tabs, then a monospace textarea + run controls. */}
      <div className="bg-white rounded-lg border border-slate-200 overflow-hidden shadow-sm">
        <div className="flex items-center justify-between gap-3 px-4 py-2 bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900 border-b border-amber-400/20">
          <span className="text-xs text-amber-200/90 font-medium flex items-center gap-2">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#fcd34d" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="m18 16 4-4-4-4" />
              <path d="m6 8-4 4 4 4" />
              <path d="m14.5 4-5 16" />
            </svg>
            Run SQL — read-only SELECT / WITH over your managed tables
          </span>
          <div className="flex items-center gap-2">
            <label className="text-[11px] text-white/70 flex items-center gap-1.5 whitespace-nowrap">
              Row limit
              <input
                type="number"
                min={1}
                max={5000}
                value={limit}
                onChange={(e) => setLimit(e.target.value)}
                className="w-20 px-2 py-1 text-xs rounded-md bg-white/10 border border-white/20 text-white placeholder:text-white/40 focus:ring-2 focus:ring-amber-300/40 focus:border-amber-400 outline-none"
              />
            </label>
            <button
              onClick={run}
              disabled={running}
              className="px-4 py-1.5 text-xs font-bold rounded-lg text-slate-900 bg-amber-400 hover:bg-amber-300 disabled:opacity-50 disabled:cursor-not-allowed transition-colors inline-flex items-center gap-1.5"
              title="Run query (Ctrl/Cmd+Enter)"
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" stroke="none">
                <polygon points="5 3 19 12 5 21 5 3" />
              </svg>
              {running ? 'Running…' : 'Run'}
            </button>
          </div>
        </div>

        <div className="p-4 space-y-3">
          <textarea
            value={sql}
            onChange={(e) => setSql(e.target.value)}
            onKeyDown={onKeyDown}
            spellCheck={false}
            rows={7}
            placeholder="SELECT * FROM default.sales LIMIT 50;"
            className="w-full px-3 py-2.5 text-[13px] font-mono leading-relaxed rounded-lg border border-slate-300 bg-slate-50/60 text-slate-800 focus:ring-2 focus:ring-amber-300/50 focus:border-amber-400 outline-none resize-y"
          />

          {/* Available-tables hint. Chips are clickable — inserting the
              identifier at the caret keeps the write-a-query flow fast. */}
          <div className="text-xs text-slate-500 flex flex-wrap items-center gap-1.5">
            <span className="font-semibold text-slate-600">
              Reference tables by <code className="px-1 rounded bg-slate-100 text-slate-700 font-mono">schema.name</code>.
            </span>
            {tableHints.length > 0 ? (
              <>
                <span className="text-slate-400">Available:</span>
                {tableHints.slice(0, 12).map((t) => (
                  <button
                    key={t}
                    type="button"
                    onClick={() => setSql((cur) => (cur.trim() ? `${cur} ${t}` : `SELECT * FROM ${t} LIMIT 50;`))}
                    className="px-1.5 py-0.5 text-[11px] font-mono rounded bg-blue-50 text-blue-700 border border-blue-200 hover:bg-blue-100 transition-colors"
                    title={`Insert ${t}`}
                  >
                    {t}
                  </button>
                ))}
                {tableHints.length > 12 && (
                  <span className="text-slate-400">+{tableHints.length - 12} more</span>
                )}
              </>
            ) : (
              <span className="text-slate-400">
                No managed tables yet — promote a file to a table first.
              </span>
            )}
            <span className="text-slate-400 ml-auto">Ctrl/Cmd+Enter to run</span>
          </div>
        </div>
      </div>

      {/* Inline error (HTTP 400 detail) — never crashes the page. */}
      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 flex items-start gap-2">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 mt-0.5">
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <div className="min-w-0 break-words font-mono text-[13px]">{error}</div>
        </div>
      )}

      {/* Results card — mirrors StoragePreviewDrawer's PreviewTable. */}
      {result && (
        <div className="bg-white rounded-lg border border-slate-200 overflow-hidden shadow-sm">
          <div className="px-4 py-2 border-b border-slate-200 bg-slate-50 text-xs text-slate-600 flex items-center gap-3 flex-wrap">
            <span className="font-semibold text-slate-700">
              {result.row_count.toLocaleString()} row{result.row_count === 1 ? '' : 's'}
            </span>
            <span className="text-slate-400">·</span>
            <span>{result.columns.length} column{result.columns.length === 1 ? '' : 's'}</span>
            {result.truncated && (
              <span className="inline-flex items-center px-2 py-0.5 text-[11px] font-semibold rounded-full bg-amber-100 text-amber-800 border border-amber-200">
                Showing first {result.limit.toLocaleString()} rows
              </span>
            )}
          </div>
          {result.rows.length === 0 ? (
            <div className="p-8 text-sm text-slate-500 text-center">Query returned no rows.</div>
          ) : (
            <div className="overflow-auto max-h-[520px]">
              <table className="text-xs w-full border-collapse">
                <thead className="sticky top-0 z-10 bg-gradient-to-b from-slate-100 to-slate-50 border-b-2 border-slate-200 shadow-[0_2px_4px_-2px_rgba(15,23,42,0.08)]">
                  <tr>
                    {result.columns.map((c, idx) => (
                      <th
                        key={c.name}
                        className={`px-4 py-2.5 whitespace-nowrap align-bottom ${
                          isNumericQueryType(c.type) ? 'text-right w-[1%]' : 'text-left'
                        } ${idx < lastColIdx ? 'border-r border-slate-300' : ''}`}
                      >
                        <div className="text-[11px] font-bold text-slate-800 uppercase tracking-wide">
                          {c.name}
                        </div>
                        <div className="mt-1">
                          <span className="inline-flex items-center px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider rounded border bg-slate-100 text-slate-600 border-slate-200">
                            {(c.type || 'UNKNOWN').toUpperCase()}
                          </span>
                        </div>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="bg-white">
                  {result.rows.map((row, i) => (
                    <tr
                      key={i}
                      className={`border-b border-slate-200 last:border-b-0 hover:bg-amber-50 transition-colors ${
                        i % 2 === 1 ? 'bg-slate-50' : 'bg-white'
                      }`}
                    >
                      {result.columns.map((c, idx) => (
                        <td
                          key={c.name}
                          className={`px-4 py-2 whitespace-nowrap text-[12px] max-w-[280px] truncate ${
                            isNumericQueryType(c.type)
                              ? 'text-right tabular-nums font-mono text-slate-700 w-[1%]'
                              : 'text-left text-slate-800'
                          } ${idx < lastColIdx ? 'border-r border-slate-200' : ''}`}
                          title={
                            row[c.name] === null || row[c.name] === undefined
                              ? ''
                              : String(row[c.name])
                          }
                        >
                          {renderQueryCell(row[c.name])}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
