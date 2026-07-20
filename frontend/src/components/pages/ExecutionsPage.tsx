import { useState, useEffect, useRef, useCallback, useMemo, Fragment } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';
import ReadOnlyBanner from '../../auth/ReadOnlyBanner';
import TableToolbar, { useTableColumns, TColumn, TColumnGroup } from '../shared/TableToolbar';
import Icon, { type IconName } from '../shared/Icon';
import HubTabs, { WORKFLOWS_TABS } from '../HubTabs';
import HeroCard from '../shared/HeroCard';
import { useDarkMode } from '../../hooks/useDarkMode';
import { useWorkflowStore } from '../../stores/workflowStore';
import ProjectContextBar from '../layout/ProjectContextBar';
import TierChip from '../shared/TierChip';
import PageHeader from '../shared/PageHeader';
// 2026-05-19 (P1 #11 of PAGE_BY_PAGE_AUDIT.md): density toggle removed per
// feedback_no_density_toggle.md — Workflows / Executions / Connections must
// not ship a Compact/Comfortable/Spacious picker. The import + hook + Tailwind
// branch were left over from an earlier iteration.
import TimeAgo from '../shared/TimeAgo';
import { usePageContext } from '../../hooks/usePageContext';
import { uiConfirm } from '../../ui/dialog';
import StatusPill from '../shared/StatusPill';
import RuntimeLineagePanel from '../lineage/RuntimeLineagePanel';
import StepIODrawer from './StepIODrawer';
import StepGantt, { type GanttStep } from '../shared/StepGantt';
import BackfillsPanel from './BackfillsPanel';



// ─────────────────────────────────────────────────────────────────────
// PR 7 — Reason-code badge for step-level failures.
//
// The backend ``StepRunResult.error_type`` is now a typed enum
// (StepErrorType in fpulse/ir/schema.py). We render a small coloured
// chip ahead of the error message so users can scan a list of failed
// runs and tell "credential expired" apart from "DuckDB OOM" without
// reading every error string. The label is friendly; the colour is
// tied to the kind of action the user typically takes.
// ─────────────────────────────────────────────────────────────────────
const ERROR_TYPE_META: Record<string, { label: string; cls: string }> = {
  invalid_config:      { label: 'Config',     cls: 'bg-amber-100 text-amber-800 border-amber-200' },
  network_error:       { label: 'Network',    cls: 'bg-blue-100 text-blue-800 border-blue-200' },
  duckdb_oom:          { label: 'Memory',     cls: 'bg-purple-100 text-purple-800 border-purple-200' },
  credential_expired:  { label: 'Auth',       cls: 'bg-rose-100 text-rose-800 border-rose-200' },
  timeout:             { label: 'Timeout',    cls: 'bg-orange-100 text-orange-800 border-orange-200' },
  upstream_failed:     { label: 'Upstream',   cls: 'bg-slate-100 text-slate-700 border-slate-200' },
  validation_failed:   { label: 'Validation', cls: 'bg-yellow-100 text-yellow-800 border-yellow-200' },
  unknown:             { label: 'Error',      cls: 'bg-red-100 text-red-800 border-red-200' },
};

function ErrorTypeBadge({ type }: { type: string }) {
  const meta = ERROR_TYPE_META[type] || ERROR_TYPE_META.unknown;
  return (
    <span
      className={`inline-block mr-1.5 px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider border ${meta.cls}`}
      title={`Failure category: ${type}`}
    >
      {meta.label}
    </span>
  );
}

// E1.1 (2026-06-08) — broader retry-policy classification chip.
// Distinct palette from ErrorTypeBadge so the two taxonomies don't
// visually collide on the same row. Hover-tooltip explains what the
// class means in retry-policy terms ("this is the kind a retry would
// fix" vs "won't change between attempts").
const FAILURE_CLASS_META: Record<string, { label: string; cls: string; hint: string }> = {
  transient:    { label: 'TRANSIENT',    cls: 'bg-blue-100 text-blue-800 border-blue-200',
                  hint: 'Likely fixed by retry (timeout / 5xx / lock / network blip)' },
  dependency:   { label: 'DEPENDENCY',   cls: 'bg-indigo-100 text-indigo-800 border-indigo-200',
                  hint: 'External system unreachable (auth / DNS / 503) — retry may fix if external recovers' },
  data_quality: { label: 'DATA QUALITY', cls: 'bg-fuchsia-100 text-fuchsia-800 border-fuchsia-200',
                  hint: "Retry won't fix this — the data itself violates a constraint" },
  user_input:   { label: 'USER INPUT',   cls: 'bg-amber-100 text-amber-800 border-amber-200',
                  hint: "Retry won't fix this — the pipeline configuration is wrong" },
  fatal:        { label: 'FATAL',        cls: 'bg-rose-200 text-rose-900 border-rose-300',
                  hint: 'OOM / disk full / code bug — retry may make it worse' },
  unknown:      { label: 'UNKNOWN',      cls: 'bg-slate-200 text-slate-800 border-slate-300',
                  hint: 'Unclassified failure — conservative default: do not retry' },
};

function FailureClassBadge({ cls }: { cls: string }) {
  const meta = FAILURE_CLASS_META[cls] || FAILURE_CLASS_META.unknown;
  return (
    <span
      className={`inline-block mr-1.5 px-1.5 py-0.5 rounded text-[10px] font-semibold tracking-wider border ${meta.cls}`}
      title={`Retry-policy class: ${cls} — ${meta.hint}`}
    >
      {meta.label}
    </span>
  );
}

// L1.1 frontend (2026-06-08) — lazy collapsible wrapper around the
// RuntimeLineagePanel so the lineage fetch only fires when the operator
// expands the section (not on every run-detail open).
function CollapsibleLineage({ runId, dark }: { runId: string; dark?: boolean }) {
  const [open, setOpen] = useState(false);
  if (!runId) return null;
  return (
    <div className="mt-3">
      <button
        onClick={() => setOpen((o) => !o)}
        className={`text-xs font-semibold flex items-center gap-1.5 ${dark ? 'text-slate-300 hover:text-white' : 'text-slate-600 hover:text-slate-900'}`}
      >
        <span className={`transition-transform ${open ? 'rotate-90' : ''}`}>▸</span>
        Runtime lineage
        <span className={`font-normal ${dark ? 'text-slate-500' : 'text-slate-400'}`}>— what actually ran</span>
      </button>
      {open && (
        <div className="mt-2">
          <RuntimeLineagePanel runId={runId} dark={dark} />
        </div>
      )}
    </div>
  );
}

/* ═══ Column definitions for execution list table ═══ */
const EXEC_COLUMNS: TColumn[] = [
  // Core columns (default visible)
  { key: 'status',    label: 'Status',    default: true,  group: 'core' },
  { key: 'workflow',  label: 'Pipeline',  default: true,  group: 'core' },
  { key: 'env',       label: 'Env',       default: true,  group: 'core' },
  { key: 'trigger',   label: 'Trigger',   default: true,  group: 'core' },
  { key: 'started',   label: 'Started',   default: true,  group: 'core' },
  { key: 'duration',  label: 'Duration',  default: true,  group: 'core' },
  { key: 'steps',     label: 'Steps',     default: true,  group: 'core' },
  // Detail columns (optional)
  { key: 'id',        label: 'Run ID',       default: false, group: 'details' },
  { key: 'completed', label: 'Completed',    default: false, group: 'details' },
  { key: 'error',     label: 'Error Message', default: false, group: 'details' },
];

const EXEC_GROUPS: TColumnGroup[] = [
  // 'list' resolves to a line-art SVG via shared/Icon.tsx; '▶' stays as
  // a monochrome text glyph (it renders consistently and matches the
  // visual weight other column-group headers use).
  { key: 'core',    label: 'Core',    icon: '▶' },
  { key: 'details', label: 'Details', icon: 'list' },
];

interface WorkflowStep {
  id: string;
  type: string;
  label: string;
  position?: { x: number; y: number };
  params?: Record<string, any>;
}

interface WorkflowConnection {
  from_step: string;
  to_step: string;
  condition?: string;
}

interface WorkflowDef {
  steps: WorkflowStep[];
  connections: WorkflowConnection[];
  /** Latest version of this pipeline (from /api/workflows/{id} response). */
  version?: number;
  /** Deployed version (Plus tier — null/undefined on Free). */
  deployed_version?: number | null;
  /** Pipeline status (draft / published / failed / archived). */
  status?: string;
}

interface LogEntry {
  timestamp: string;
  level: 'info' | 'warn' | 'error' | 'debug';
  message: string;
  step_id?: string;
}

interface StepLog {
  step_id: string;
  step_name: string;
  step_type: string;
  status: string;
  started_at?: string;
  completed_at?: string;
  duration_ms: number;
  rows_processed: number;
  error_message?: string;
  /** Classified error category (e.g. `NetworkError`, `SchemaError`). Added
   * by the executor when an exception is captured; absent on success rows
   * and on older runs that predate the classifier. */
  error_type?: string;
  /** E1.1 (2026-06-08) — broader retry-policy classification. One of
   * "transient" | "dependency" | "data_quality" | "user_input" |
   * "fatal" | "unknown". Distinct from `error_type` (the executor's
   * narrow taxonomy) — this is the wider "is this retryable in
   * principle?" category. Absent on success rows + on runs older
   * than 2026-06-08. */
  failure_class?: string;
  /** Sample of the step's output rows (first N records) the executor
   * captured for the replay viewer. Shape is array of row objects;
   * absent when the step didn't emit data or when capture was off. */
  output_preview?: any[];
  logs?: LogEntry[];
}

interface Execution {
  id: string;
  workflow_id: string;
  workflow_name: string;
  /** Project the workflow belongs to. Populated when the executor or
   * monitoring store joins through to `workflows.project_id`. Optional
   * because older rows + standalone runs (no project) may omit it. */
  project_id?: string;
  status: string;
  started_at: string;
  completed_at?: string;
  duration_ms?: number;
  steps_total?: number;
  steps_completed?: number;
  steps_failed?: number;
  error_message?: string;
  triggered_by?: string;
  step_logs?: StepLog[];
  log_entries?: LogEntry[];
  environment?: 'dev' | 'prod';
  /** Full workflow IR captured at run start — powers "Open in Editor" / replay. */
  workflow_snapshot?: Record<string, any> | null;
  /** Per-execution metadata bag. Compute usage (peak_memory_mb,
   * cpu_seconds), resolved parameter_values, and any feature flags
   * captured at run start live here. Optional — older runs predate it. */
  metadata?: Record<string, any> | null;
  /** Storage outputs produced by this run. Joined server-side from the
   * storage_objects table (rows with kind=output AND run_id={execution_id}).
   * Empty array if the run wrote nothing into Storage; older runs predate
   * the join and may have it absent. */
  storage_outputs?: Array<{
    id: string;
    name: string;
    path: string;
    size_bytes: number;
    format: string | null;
  }>;
}

interface Stats {
  total: number;
  success: number;
  failed: number;
  running: number;
  queued: number;
  success_rate: number;
  avg_duration_ms: number;
  period_hours: number;
}

const STATUS_STYLES_LIGHT: Record<string, string> = {
  success: 'bg-green-50 text-green-600',
  error: 'bg-red-50 text-red-500',
  running: 'bg-amber-50 text-amber-600',
  pending: 'bg-slate-50 text-slate-400',
  cancelled: 'bg-slate-50 text-slate-400',
  queued: 'bg-blue-50 text-blue-500',
};
const STATUS_STYLES_DARK: Record<string, string> = {
  success: 'bg-green-500/15 text-green-400',
  error: 'bg-red-500/15 text-red-400',
  running: 'bg-amber-500/15 text-amber-400',
  pending: 'bg-white/5 text-slate-500',
  cancelled: 'bg-white/5 text-slate-500',
  queued: 'bg-blue-500/15 text-blue-400',
};

const STEP_STATUS_ICON: Record<string, { icon: string; color: string }> = {
  success: { icon: '\u2713', color: 'text-green-500 bg-green-50 border-green-200' },
  error: { icon: '\u2717', color: 'text-red-500 bg-red-50 border-red-200' },
  running: { icon: '\u25CB', color: 'text-amber-500 bg-amber-50 border-amber-200' },
  pending: { icon: '\u25CB', color: 'text-slate-300 bg-slate-50 border-slate-200' },
  skipped: { icon: '\u2192', color: 'text-slate-400 bg-slate-50 border-slate-200' },
};

type DetailTab = 'lineage' | 'steps' | 'logs';
type LogLevel = 'all' | 'info' | 'warn' | 'error' | 'debug';

// Step-type icon map + component moved to shared/StepTypeIcon.tsx so the
// step-IO inspector drawer renders the same glyph the lineage node does.
import { StepTypeIcon } from '../shared/StepTypeIcon';

// 2026-05-19 (OSS-2 of PAGE_BY_PAGE_AUDIT.md): the canonical
// <StatusPill> intentionally does NOT replace this palette. The lineage
// graph also needs `border` + `glow` (drop-shadow on the currently-
// selected node card) and `icon` glyphs ('✓ / ✕ / ● / ○ / →') that the
// shared pill doesn't model — they're load-bearing visual signals in
// the SVG graph layout, not just decorative. If StatusPill grows an
// `iconGlyph` + `withGlow` slot in a future refactor, this can fold in.
const LINEAGE_STATUS_STYLES: Record<string, { bg: string; border: string; text: string; glow: string; icon: string }> = {
  success: { bg: 'bg-emerald-50', border: 'border-emerald-300', text: 'text-emerald-700', glow: 'shadow-emerald-100', icon: '✓' },
  error: { bg: 'bg-red-50', border: 'border-red-300', text: 'text-red-600', glow: 'shadow-red-100', icon: '✕' },
  running: { bg: 'bg-amber-50', border: 'border-amber-300', text: 'text-amber-600', glow: 'shadow-amber-100', icon: '●' },
  pending: { bg: 'bg-slate-50', border: 'border-slate-200', text: 'text-slate-400', glow: '', icon: '○' },
  skipped: { bg: 'bg-slate-50', border: 'border-slate-200', text: 'text-slate-400', glow: '', icon: '→' },
};

/** Single active-filter chip rendered in the filter bar's "Active"
 *  row. The × button clears just this filter without nuking the
 *  others — much friendlier than the original blanket "Clear filters"
 *  link when the user only wants to drop one constraint. */
function FilterChip({
  dark, label, value, onClear,
}: { dark: boolean; label: string; value: string; onClear: () => void }) {
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium border ${
      dark
        ? 'bg-blue-500/15 text-blue-200 border-blue-500/25'
        : 'bg-blue-50 text-blue-700 border-blue-200'
    }`}>
      <span className={`text-xs font-bold uppercase ${dark ? 'text-blue-400' : 'text-blue-500'}`}>{label}:</span>
      <span className="truncate max-w-[160px]">{value}</span>
      <button
        onClick={onClear}
        title={`Clear ${label} filter`}
        className={`ml-0.5 w-3.5 h-3.5 rounded-full flex items-center justify-center transition-colors ${
          dark ? 'hover:bg-blue-500/30 text-blue-300' : 'hover:bg-blue-200 text-blue-600'
        }`}
      >
        <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
          <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
    </span>
  );
}

export default function ExecutionsPage({ projectId, projectName = '', onClearProject, onGoToProjects, environment, tier = 'free' }: { projectId?: string | null; projectName?: string; onClearProject?: () => void; onGoToProjects?: () => void; environment?: 'dev' | 'prod'; tier?: string } = {}) {
  const dark = useDarkMode();
  const loadExecutionIntoEditor = useWorkflowStore((s) => s.loadExecutionIntoEditor);
  const [executions, setExecutions] = useState<Execution[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<'all' | 'success' | 'error' | 'running'>('all');
  const [selectedExec, setSelectedExec] = useState<Execution | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [stats, setStats] = useState<{ '24h': Stats; '7d': Stats; '30d': Stats } | null>(null);
  // Time range — now the single source of truth for BOTH the KPI
  // cards above AND the execution table below. 2026-05-27: previously
  // this only drove the stats display, while the table showed
  // everything regardless. That created two parallel timelines
  // (cards say "24h, 12 runs" while the table lists 116 runs).
  // 'custom' surfaces the From/To date pickers and disables the
  // rolling-window filter — the user picks a closed interval instead.
  const [selectedPeriod, setSelectedPeriod] = useState<'24h' | '7d' | '30d' | 'custom'>('24h');
  const [showDashboard, setShowDashboard] = useState(true);
  const [detailTab, setDetailTab] = useState<DetailTab>('lineage');
  // Per-step expand state in the Steps tab — clicking a row reveals
  // the step's input configuration, what it received from upstream
  // steps, and a sample of its output rows. Lets a user debug a
  // failed run at the node level without having to open the editor.
  const [expandedSteps, setExpandedSteps] = useState<Record<string, boolean>>({});
  const toggleStepExpand = (stepId: string) =>
    setExpandedSteps((prev) => ({ ...prev, [stepId]: !prev[stepId] }));
  // Step-IO drawer — opened by clicking a node in the lineage tab to
  // inspect that step's captured input/output for the selected run.
  const [ioStep, setIOStep] = useState<{ stepId: string; label?: string } | null>(null);
  // Per-edge row counts for the lineage view labels ("18 items" / "1.2K items").
  // Keyed by `${from_step}→${to_step}`. Empty for pre-capture runs.
  const [edgeCounts, setEdgeCounts] = useState<Map<string, { row_count: number; from_status: string }>>(new Map());

  useEffect(() => {
    if (!selectedExec?.id) {
      setEdgeCounts(new Map());
      return;
    }
    let cancelled = false;
    api
      .getExecutionEdges(selectedExec.id)
      .then((data) => {
        if (cancelled) return;
        const m = new Map<string, { row_count: number; from_status: string }>();
        for (const e of data.edges) {
          m.set(`${e.from_step}→${e.to_step}`, {
            row_count: e.row_count,
            from_status: e.from_status,
          });
        }
        setEdgeCounts(m);
      })
      .catch(() => {
        // Pre-capture runs / network hiccup — fall through to no labels.
        if (!cancelled) setEdgeCounts(new Map());
      });
    return () => {
      cancelled = true;
    };
  }, [selectedExec?.id]);
  const [logLevel, setLogLevel] = useState<LogLevel>('all');
  const [logSearch, setLogSearch] = useState('');
  const [workflowDef, setWorkflowDef] = useState<WorkflowDef | null>(null);
  const [failureSummaryOpen, setFailureSummaryOpen] = useState(true);
  const [aiDiagnosis, setAiDiagnosis] = useState<{
    diagnosis: string;
    suggestion: string;
    severity: string;
    ai_powered: boolean;
  } | null>(null);
  const [aiDiagnosisLoading, setAiDiagnosisLoading] = useState(false);
  const [aiDiagnosisError, setAiDiagnosisError] = useState<string | null>(null);
  const [resumeLoading, setResumeLoading] = useState(false);
  // Per-execution-id state so the Cancel + Re-run buttons can show a
  // pending label without ambiguity when a user clicks repeatedly or
  // navigates between runs mid-flight.
  // Per docs/PAGE_BY_PAGE_AUDIT.md (P0 #3, 2026-05-19).
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const [rerunningId, setRerunningId] = useState<string | null>(null);
  // D1 — Replay UI state: which run is currently replaying, and the
  // result payload of the most recent replay (cleared when the user
  // navigates to a different execution).
  const [replayingId, setReplayingId] = useState<string | null>(null);
  const [replayResult, setReplayResult] = useState<{
    original_id: string;
    replay_id: string;
    ir_sha: string | null;
    status: string;
    diff: {
      status_changed: boolean;
      ir_sha_match: boolean | null;
      duration_delta_ms: number;
      rows_delta: number;
      steps: Array<{
        step_id: string;
        step_name: string;
        a_status: string | null;
        b_status: string | null;
        a_rows: number;
        b_rows: number;
        a_duration_ms: number | null;
        b_duration_ms: number | null;
        changed: boolean;
      }>;
      added_steps: string[];
      removed_steps: string[];
    };
  } | null>(null);
  const [prodView, setProdView] = useState<'runs' | 'logs'>('runs');
  // 2026-05-27 — Runs vs Backfills sub-view inside DEV. PROD still shows
  // only Runs/Logs (backfills are a DEV-time workflow). Stored in
  // sessionStorage so navigating away and back doesn't snap to Runs.
  const [mainView, setMainView] = useState<'runs' | 'backfills'>(() => {
    try {
      const stashed = sessionStorage.getItem('fpulse_executions_main_view');
      return stashed === 'backfills' ? 'backfills' : 'runs';
    } catch {
      return 'runs';
    }
  });
  useEffect(() => {
    try { sessionStorage.setItem('fpulse_executions_main_view', mainView); } catch { /* ignore */ }
    // If Toolbar just submitted a backfill it stashed the id; auto-flip
    // to Backfills so the user sees their new run.
    if (mainView === 'runs') {
      try {
        if (sessionStorage.getItem('fpulse_focused_backfill_id')) {
          setMainView('backfills');
        }
      } catch { /* ignore */ }
    }
  }, [mainView]);
  const [exportOpen, setExportOpen] = useState(false);
  const [tableSearch, setTableSearch] = useState('');
  // Filter bar state — Project / Workflow / time range. Empty string
  // = no filter on that dimension. Time inputs use the native `date`
  // input (YYYY-MM-DD); start-of-day / end-of-day boundaries are
  // applied at filter time so a single-day pick captures everything
  // that ran on that day in the user's local timezone.
  const [projectFilter, setProjectFilter] = useState<string>('');
  const [workflowFilter, setWorkflowFilter] = useState<string>('');
  const [dateFrom, setDateFrom] = useState<string>('');
  const [dateTo, setDateTo] = useState<string>('');
  const [projectOptions, setProjectOptions] = useState<Array<{ id: string; name: string }>>([]);
  const [workflowOptions, setWorkflowOptions] = useState<Array<{ id: string; name: string; project_id?: string }>>([]);
  // Load dropdown options once on mount; refresh if the project filter
  // changes so the Workflow dropdown only shows pipelines in that
  // project.
  useEffect(() => {
    api.listProjects()
      .then((p) => setProjectOptions(Array.isArray(p) ? p.map((x: any) => ({ id: x.id, name: x.name || x.id })) : []))
      .catch(() => setProjectOptions([]));
  }, []);
  useEffect(() => {
    api.listWorkflows(projectFilter ? { project_id: projectFilter } : undefined)
      .then((w) => setWorkflowOptions(Array.isArray(w) ? w.map((x: any) => ({ id: x.id, name: x.name || x.id, project_id: x.project_id })) : []))
      .catch(() => setWorkflowOptions([]));
    // If a workflow was selected and the project filter narrowed the
    // list to exclude it, drop the workflow filter so the table doesn't
    // silently show zero results.
    if (workflowFilter && projectFilter) {
      // Lazy check — let the next render's options decide; we don't
      // need to block here.
    }
  }, [projectFilter]);
  const { visibleColumns, activeColumns, toggleColumn, resetToDefaults, selectAll, isVisible } = useTableColumns('fpulse_executions_columns', EXEC_COLUMNS);

  useEffect(() => {
    loadData();
  }, [projectId]);

  // Deep-link: `#executions/<execution_id>` opens that run's detail
  // panel on the Steps tab so users arriving from a notification land
  // on node-level state (input/output per step) rather than the run
  // list. Hash changes also trigger this so the user can jump between
  // runs without a full reload.
  useEffect(() => {
    const openFromHash = async () => {
      const raw = window.location.hash.replace('#', '');
      const parts = raw.split('/');
      if (parts[0] !== 'executions' || !parts[1]) return;
      const execId = parts[1];
      if (selectedExec?.id === execId) return;
      try {
        const exec = await api.getExecution(execId);
        if (exec) {
          setDetailTab('steps');
          await loadDetail(exec as Execution);
        }
      } catch {
        // Bad/expired ID or wrong workspace — leave the user on the
        // list view rather than throwing a toast on every hashchange.
      }
    };
    openFromHash();
    window.addEventListener('hashchange', openFromHash);
    return () => window.removeEventListener('hashchange', openFromHash);
  }, [selectedExec?.id]);

  // Publish page context for the AI Copilot — agent answers
  // "what failed?" / "longest run today?" without a tool call.
  usePageContext({
    page: 'executions.list',
    visible_ids: executions.map((e) => e.id),
    filters: { search: tableSearch },
    environment,
    visible_items: executions.map((e) => ({
      id: e.id,
      name: e.workflow_name,
      kind: 'execution',
      status: e.status,
      meta: {
        workflow_id: e.workflow_id,
        started_at: e.started_at ?? null,
        duration_ms: e.duration_ms ?? null,
        steps_failed: e.steps_failed ?? null,
        triggered_by: e.triggered_by ?? null,
        environment: e.environment ?? null,
      },
    })),
  });

  const loadData = async () => {
    setLoading(true);
    try {
      const params: { project_id?: string } = {};
      if (projectId) params.project_id = projectId;
      const [data, s] = await Promise.all([
        api.listExecutions(params),
        api.getMultiStats().catch(() => null),
      ]);
      setExecutions(Array.isArray(data) ? data : []);
      if (s) setStats(s);
    } catch {
      setExecutions([]);
      toast.error('Failed to load executions', 'Check that the backend is running');
    }
    setLoading(false);
  };

  const loadDetail = async (exec: Execution) => {
    setSelectedExec(exec);
    // D1 — clear any prior replay result so a stale diff card from
    // the previous selection doesn't bleed into this run's view.
    setReplayResult(null);
    setDetailLoading(true);
    setWorkflowDef(null);
    // Reset any prior diagnosis when opening a different run.
    setAiDiagnosis(null);
    setAiDiagnosisError(null);
    try {
      const [detailRes, wfData] = await Promise.all([
        api.getExecution(exec.id).catch(() => null),
        api.getWorkflow(exec.workflow_id).catch(() => null),
      ]);
      if (detailRes) setSelectedExec(detailRes);
      if (wfData) {
        // API may return { workflow: {...}, version } or flat. Capture
        // the version + lifecycle fields so the detail view can show
        // which pipeline version this run belonged to (the Workflow IR
        // itself doesn't carry the version number — it sits on the
        // WorkflowVersion wrapper one level up).
        const wf = wfData.workflow || wfData;
        setWorkflowDef({
          steps: wf.steps || [],
          connections: wf.connections || [],
          version: typeof wfData.version === 'number' ? wfData.version : undefined,
          deployed_version: wf.deployed_version ?? null,
          status: wf.status,
        });
      }
    } catch {
      // keep basic info
    }
    setDetailLoading(false);
  };

  // 2026-05-19 (P1 #5 of PAGE_BY_PAGE_AUDIT.md): live polling for a
  // running execution. Without this the detail pane of an active run was
  // a snapshot — the user had to click Refresh to see step progress. We
  // poll /api/monitor/executions/:id every 2s while the run is in flight,
  // and stop the moment the status flips to a terminal value (success /
  // error / cancelled / timed_out). Gated on `document.visibilitychange`
  // so a hidden tab doesn't burn the backend with 30 polls/min for free.
  // Caches the workflow_id reference so a row swap immediately retargets.
  useEffect(() => {
    const execId = selectedExec?.id;
    const status = selectedExec?.status;
    if (!execId) return;
    if (status !== 'running' && status !== 'pending') return;
    let cancelled = false;
    let interval: number | undefined;
    const tick = async () => {
      if (cancelled || document.hidden) return;
      try {
        const fresh = await api.getExecution(execId);
        if (cancelled) return;
        if (fresh) setSelectedExec(fresh as Execution);
      } catch {
        // Transient — try again on the next tick. The 401 interceptor
        // already handles auth failure globally.
      }
    };
    const start = () => {
      if (interval !== undefined) return;
      interval = window.setInterval(tick, 2000);
    };
    const stop = () => {
      if (interval === undefined) return;
      clearInterval(interval);
      interval = undefined;
    };
    const onVisibility = () => {
      if (document.hidden) {
        stop();
      } else {
        tick();
        start();
      }
    };
    if (!document.hidden) start();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      cancelled = true;
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [selectedExec?.id, selectedExec?.status]);

  /** Auto-fire AI Diagnose whenever the user opens a failed execution.
   *  The user shouldn't have to click a button to find out why their
   *  pipeline broke — the diagnosis is the answer they came for. The
   *  manual "Re-diagnose" button stays for re-running after edits. */
  useEffect(() => {
    if (!selectedExec) return;
    if (selectedExec.status !== 'error') return;
    if (aiDiagnosis || aiDiagnosisLoading) return;
    const failedSteps = (selectedExec.step_logs || []).filter((s: any) => s.status === 'error');
    const rootCause = failedSteps[0];
    if (!rootCause?.error_message) return;
    setAiDiagnosisError(null);
    setAiDiagnosisLoading(true);
    (async () => {
      try {
        const headers: Record<string, string> = { 'Content-Type': 'application/json' };
        const token = localStorage.getItem('fpulse_token') || '';
        const ws = localStorage.getItem('fpulse_workspace_id') || 'default';
        if (token) headers['Authorization'] = `Bearer ${token}`;
        headers['X-Workspace-Id'] = ws;
        const res = await fetch('/api/ai/diagnose-error', {
          method: 'POST',
          headers,
          body: JSON.stringify({
            error_message: rootCause.error_message || '',
            node_type: rootCause.step_type || '',
            node_params: {},
            upstream_schema: [],
          }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        setAiDiagnosis(await res.json());
      } catch (e) {
        setAiDiagnosisError(e instanceof Error ? e.message : 'Diagnose failed');
      } finally {
        setAiDiagnosisLoading(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedExec?.id, selectedExec?.status]);

  // Map the time-range segment to a rolling window in hours.
  // 'custom' returns null — the From/To date pickers take over.
  const periodHours = useMemo(() => {
    if (selectedPeriod === '24h') return 24;
    if (selectedPeriod === '7d') return 7 * 24;
    if (selectedPeriod === '30d') return 30 * 24;
    return null;
  }, [selectedPeriod]);

  const filtered = useMemo(() => {
    let list = filter === 'all' ? executions : executions.filter((e) => e.status === filter);
    // Rolling time-range window (24h / 7d / 30d). Skipped when the
    // user picked 'custom' — they get exact From/To control instead.
    if (periodHours !== null) {
      const cutoffMs = Date.now() - periodHours * 3_600_000;
      list = list.filter(e => {
        const t = e.started_at ? new Date(e.started_at).getTime() : 0;
        return t >= cutoffMs;
      });
    }
    if (tableSearch.trim()) {
      const q = tableSearch.toLowerCase();
      list = list.filter(e =>
        (e.workflow_name || e.workflow_id || '').toLowerCase().includes(q) ||
        (e.status || '').toLowerCase().includes(q) ||
        (e.triggered_by || '').toLowerCase().includes(q)
      );
    }
    if (projectFilter) {
      list = list.filter(e => (e.project_id || '') === projectFilter);
    }
    if (workflowFilter) {
      list = list.filter(e => e.workflow_id === workflowFilter);
    }
    if (dateFrom) {
      // Parse the YYYY-MM-DD picker value as start-of-day in the
      // user's local timezone. Anything that started before this
      // moment is filtered out.
      const fromMs = new Date(dateFrom + 'T00:00:00').getTime();
      if (!Number.isNaN(fromMs)) {
        list = list.filter(e => {
          const t = e.started_at ? new Date(e.started_at).getTime() : 0;
          return t >= fromMs;
        });
      }
    }
    if (dateTo) {
      // End-of-day for the To picker so a single-day range like
      // From=2026-05-08, To=2026-05-08 captures the full day.
      const toMs = new Date(dateTo + 'T23:59:59.999').getTime();
      if (!Number.isNaN(toMs)) {
        list = list.filter(e => {
          const t = e.started_at ? new Date(e.started_at).getTime() : 0;
          return t <= toMs;
        });
      }
    }
    return list;
  }, [executions, filter, tableSearch, projectFilter, workflowFilter, dateFrom, dateTo, periodHours]);

  // Status filter + project/pipeline/date filters all count as
  // "user-applied" filters worth chipping. Time range is always
  // present (24h default) so it gets its own chip but doesn't
  // contribute to the count badge — it's never "off".
  const anyFilterActive = !!(filter !== 'all' || projectFilter || workflowFilter || dateFrom || dateTo);
  const clearAllFilters = () => {
    setFilter('all');
    setProjectFilter('');
    setWorkflowFilter('');
    setDateFrom('');
    setDateTo('');
    setSelectedPeriod('24h');
  };

  const formatDuration = (ms?: number) => {
    if (!ms) return '\u2014';
    if (ms < 1000) return `${ms}ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
    return `${(ms / 60000).toFixed(1)}m`;
  };

  const formatTime = (ts?: string) => {
    if (!ts) return '\u2014';
    const d = new Date(ts);
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  };

  const currentStats = stats?.[selectedPeriod];

  // Derive inline stats from loaded executions
  const successCount = executions.filter(e => e.status === 'success').length;
  const errorCount = executions.filter(e => e.status === 'error').length;
  const runningCount = executions.filter(e => e.status === 'running').length;

  return (
    <div className="flex-1 flex flex-col overflow-hidden bg-canvas-bg">
      <ReadOnlyBanner environment={environment || 'dev'} />
      <div className="flex-1 flex overflow-hidden">
      {/* Main list — hidden while an execution detail is open so the
          detail view can take over the same content area instead of
          covering the whole app with a fixed overlay (which used to
          hide the sidebar + top nav, breaking the app template). */}
      <div className={`flex-1 overflow-auto ${selectedExec ? 'hidden' : ''}`}>
        {/* Header — 3-col grid (matches Insights / Settings):
            • LEFT:   page title cluster ("Executions")
            • CENTER: HubTabs — sibling tabs in the Workflows family
            • RIGHT:  page-specific actions (PROD Runs/Logs, Stats, Refresh) */}
        <PageHeader
          environment={environment}
          icon={
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className={environment === 'prod' ? 'text-red-400' : 'text-blue-500'}>
              <circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" />
            </svg>
          }
          title="Executions"
          titleAccessory={<TierChip tier={tier} environment={environment} />}
          subtitle={environment === 'prod'
            ? prodView === 'logs' ? 'Production execution logs, error traces, and diagnostics' : 'Real-time production pipeline runs and execution history'
            : 'Pipeline run history, monitoring, and step-by-step logs'}
          tabs={
            <HubTabs
              tabs={WORKFLOWS_TABS}
              active="executions"
              onNavigate={(p) => { window.location.hash = p; }}
              environment={environment}
            />
          }
          actions={
            <div className="flex justify-end items-center gap-2">
              {/* PROD: Runs (blue) / Logs (amber) — two independent buttons.
                  Active = brand-colored fill. Inactive = same dark slate
                  ghost as Refresh so the three read as one button family. */}
              {environment === 'prod' && (
                <>
                  <button
                    onClick={() => setProdView('runs')}
                    style={prodView === 'runs' ? { background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' } : undefined}
                    className={`px-4 py-2 text-sm font-semibold rounded-lg transition-all flex items-center gap-1.5 border ${
                      prodView === 'runs'
                        ? 'text-white border-blue-700 shadow-sm'
                        : 'bg-slate-800 text-slate-200 border-slate-700 hover:bg-slate-700'
                    }`}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polygon points="5 3 19 12 5 21 5 3" /></svg>
                    Runs
                  </button>
                  <button
                    onClick={() => setProdView('logs')}
                    className={`px-4 py-2 text-sm font-semibold rounded-lg transition-all flex items-center gap-1.5 border ${
                      prodView === 'logs'
                        ? 'bg-gradient-to-r from-indigo-500 to-indigo-700 text-white border-indigo-700 shadow-sm hover:from-indigo-600 hover:to-indigo-800'
                        : 'bg-slate-800 text-slate-200 border-slate-700 hover:bg-slate-700'
                    }`}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /></svg>
                    Logs
                  </button>
                </>
              )}
              {/* Stats toggle — DEV only (PROD has Overview page) */}
              {environment !== 'prod' && (
                <button
                  onClick={() => setShowDashboard(!showDashboard)}
                  className={`px-4 py-2 text-sm font-medium rounded-lg border transition-colors ${
                    showDashboard ? 'bg-blue-50 text-blue-600 border-blue-200' : 'bg-white text-slate-400 border-slate-200 hover:text-slate-600'
                  }`}
                >
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="inline mr-1 -mt-0.5">
                    <rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18" /><path d="M9 21V9" />
                  </svg>
                  Stats
                </button>
              )}
              {/* Duplicate header Export removed Apr 18 — the canonical
                  Export lives in the table summary strip (TableToolbar
                  component, navy-blue bar). Users get one Export per
                  page; the page-header chrome stays clean. */}
              <button
                onClick={loadData}
                disabled={loading}
                title="Reload executions from the server (does not change anything in the running pipeline)"
                className={`px-4 py-2 text-sm font-semibold rounded-lg transition-colors flex items-center gap-1.5 disabled:opacity-60 disabled:cursor-wait ${
                  environment === 'prod'
                    ? 'bg-slate-800 text-slate-200 border border-slate-700 hover:bg-slate-700'
                    : 'bg-white text-slate-500 border border-slate-200 hover:bg-slate-50'
                }`}
              >
                {loading ? (
                  <span className="w-3 h-3 border-2 border-slate-400/40 border-t-slate-500 rounded-full animate-spin" />
                ) : (
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="23 4 23 10 17 10" /><polyline points="1 20 1 14 7 14" />
                    <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
                  </svg>
                )}
                {loading ? 'Refreshing…' : 'Refresh'}
              </button>
            </div>
          }
        />

        <ProjectContextBar
          projectId={projectId}
          projectName={projectName}
          onGoToProjects={onGoToProjects || (() => {})}
          onClear={onClearProject || (() => {})}
        />

        {/* PROD Logs view — embedded inline */}
        {environment === 'prod' && prodView === 'logs' && (
          <div className="w-full max-w-[1500px] mx-auto px-8 py-6 space-y-4">
            {/* Log filters */}
            <div className="flex items-center gap-3">
              <div className="relative flex-1 max-w-md">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2" className="absolute left-3 top-1/2 -translate-y-1/2">
                  <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
                </svg>
                <input
                  type="text"
                  value={logSearch}
                  onChange={e => setLogSearch(e.target.value)}
                  placeholder="Search logs by workflow or message..."
                  className="w-full pl-9 pr-3 py-2.5 text-sm rounded-lg border border-slate-200 bg-white text-slate-700 focus:outline-none focus:ring-2 focus:ring-red-200"
                />
              </div>
              <div className="flex bg-white border border-slate-200 rounded-lg p-0.5">
                {(['all', 'error', 'running'] as const).map(level => (
                  <button
                    key={level}
                    onClick={() => setFilter(level)}
                    className={`px-3 py-1.5 text-xs font-semibold rounded-md transition-all capitalize ${
                      filter === level ? 'bg-slate-900 text-white shadow-sm' : 'text-slate-500 hover:text-slate-700'
                    }`}
                  >
                    {level === 'all' ? 'All' : level === 'error' ? 'Errors' : 'Running'}
                  </button>
                ))}
              </div>
            </div>

            {/* Log entries from executions */}
            {loading ? (
              <div className="flex items-center justify-center py-16">
                <div className="w-6 h-6 border-2 border-red-300 border-t-transparent rounded-full animate-spin" />
              </div>
            ) : filtered.length === 0 ? (
              <div className="text-center py-16">
                <div className="w-14 h-14 mx-auto mb-3 rounded-2xl bg-slate-100 flex items-center justify-center">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="1.5">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" />
                  </svg>
                </div>
                <p className="text-sm font-medium text-slate-500">No execution logs found</p>
                <p className="text-xs text-slate-400 mt-1">Run a pipeline to see logs here</p>
              </div>
            ) : (
              <div className="rounded-lg border border-slate-200 shadow-sm bg-white overflow-hidden">
                <div className="bg-slate-50 border-b border-slate-200 px-4 py-2.5 flex items-center justify-between">
                  <span className="text-xs font-bold text-slate-500 uppercase tracking-wider">Execution Log</span>
                  <span className="text-xs text-slate-400">{filtered.length} entries</span>
                </div>
                <div className="divide-y divide-slate-100 max-h-[600px] overflow-auto font-mono text-xs">
                  {filtered.map(exec => {
                    const isError = exec.status === 'error';
                    const isRunning = exec.status === 'running';
                    return (
                      <div key={exec.id} className={`px-4 py-3 flex items-start gap-3 hover:bg-slate-50 transition-colors ${
                        isError ? 'bg-red-50/30' : ''
                      }`}>
                        <span className="text-xs text-slate-400 shrink-0 mt-0.5 w-[110px]">
                          {new Date(exec.started_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                        </span>
                        <span className={`text-xs font-bold uppercase shrink-0 w-[60px] ${
                          isError ? 'text-red-500' : isRunning ? 'text-blue-500' : 'text-emerald-500'
                        }`}>
                          {exec.status}
                        </span>
                        <div className="flex-1 min-w-0">
                          <span className="text-slate-700 font-semibold">{exec.workflow_name}</span>
                          {exec.error_message && (
                            <p className="text-red-500 mt-0.5 text-xs">{exec.error_message}</p>
                          )}
                        </div>
                        <span className="text-slate-400 shrink-0">
                          {exec.duration_ms ? formatDuration(exec.duration_ms) : isRunning ? '...' : '—'}
                        </span>
                        <button
                          onClick={() => loadDetail(exec)}
                          className="text-slate-400 hover:text-blue-500 shrink-0"
                          title="View details"
                        >
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="9 18 15 12 9 6" /></svg>
                        </button>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Main runs view (default for both DEV and PROD) */}
        {(environment !== 'prod' || prodView === 'runs') && (
        <div className="w-full max-w-[1500px] mx-auto px-8 py-6 space-y-6">
          {/* Runs vs Backfills sub-tabs — DEV only. PROD keeps Runs/Logs
              chrome at the page header level; backfills are a DEV-time
              workflow. The two pills sit just above the dashboard / filter
              bar so the user lands in Runs by default and can jump to
              Backfills with one click. */}
          {environment !== 'prod' && (
            <div className="flex items-center gap-1 border-b border-slate-200">
              <button
                onClick={() => setMainView('runs')}
                className={`px-4 py-2 text-sm font-semibold transition-colors border-b-2 -mb-px ${
                  mainView === 'runs'
                    ? 'text-blue-700 border-blue-500'
                    : 'text-slate-500 border-transparent hover:text-slate-700'
                }`}
              >
                Runs
              </button>
              <button
                onClick={() => setMainView('backfills')}
                className={`px-4 py-2 text-sm font-semibold transition-colors border-b-2 -mb-px flex items-center gap-1.5 ${
                  mainView === 'backfills'
                    ? 'text-violet-700 border-violet-500'
                    : 'text-slate-500 border-transparent hover:text-slate-700'
                }`}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="3" y="4" width="18" height="18" rx="2" />
                  <line x1="16" y1="2" x2="16" y2="6" />
                  <line x1="8" y1="2" x2="8" y2="6" />
                  <line x1="3" y1="10" x2="21" y2="10" />
                </svg>
                Backfills
              </button>
            </div>
          )}

          {/* Backfills sub-view — list of historical backfills + per-window
              Gantt strip. Clicking a window opens its execution in the
              existing detail panel via deep-link to #executions/<id>. */}
          {environment !== 'prod' && mainView === 'backfills' && (
            <BackfillsPanel
              dark={dark}
              onOpenExecution={(execId) => {
                window.location.hash = `executions/${execId}`;
                setMainView('runs');
              }}
            />
          )}

          {/* Runs sub-view — existing table + dashboard. Wrap the whole
              block in `mainView === 'runs'` so DEV/Backfills switches
              clean. PROD always shows runs (mainView toggle is hidden). */}
          {(environment === 'prod' || mainView === 'runs') && (
          <>
          {/* Dashboard — collapsible stats overview.
              2026-05-27 (filter consolidation): the period selector
              that used to live here moved into the unified filter
              bar below. The KPI cards now read `currentStats` which
              is keyed off the same `selectedPeriod` state, so the
              cards stay in sync with the table without a second
              selector duplicating the choice. */}
          {showDashboard && currentStats && (
            <div className="space-y-4">
              {/* HeroCard-style KPI grid — matches DashboardPage + Pool.
                  DEV uses lighter 400→500 gradients; PROD uses richer
                  500→600. Content centered, SVG icons, black text. */}
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
                <HeroCard
                  gradient={environment === 'prod' ? 'from-indigo-500 to-indigo-600' : 'from-indigo-400 to-indigo-500'}
                  icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polygon points="5 3 19 12 5 21 5 3" /></svg>}
                  label="Total Runs"
                  value={String(currentStats.total)}
                />
                <HeroCard
                  gradient={environment === 'prod' ? 'from-emerald-500 to-emerald-600' : 'from-emerald-400 to-emerald-500'}
                  icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>}
                  label="Succeeded"
                  value={String(currentStats.success)}
                />
                <HeroCard
                  gradient={environment === 'prod' ? 'from-red-500 to-rose-600' : 'from-red-400 to-rose-500'}
                  icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>}
                  label="Failed"
                  value={String(currentStats.failed)}
                />
                <HeroCard
                  gradient={environment === 'prod' ? 'from-blue-500 to-sky-600' : 'from-blue-400 to-sky-500'}
                  icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><circle cx="12" cy="12" r="3" fill="currentColor" /></svg>}
                  label="Running"
                  value={String(currentStats.running)}
                />
                <HeroCard
                  gradient={
                    environment === 'prod'
                      ? (currentStats.success_rate >= 95 ? 'from-emerald-500 to-emerald-600'
                          : currentStats.success_rate >= 80 ? 'from-amber-500 to-orange-600'
                          : 'from-red-500 to-rose-600')
                      : (currentStats.success_rate >= 95 ? 'from-emerald-400 to-emerald-500'
                          : currentStats.success_rate >= 80 ? 'from-amber-400 to-orange-500'
                          : 'from-red-400 to-rose-500')
                  }
                  icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" /></svg>}
                  label="Success Rate"
                  value={String(currentStats.success_rate)}
                  valueSuffix="%"
                  bar={currentStats.success_rate}
                />
                <HeroCard
                  gradient={environment === 'prod' ? 'from-violet-500 to-purple-600' : 'from-violet-400 to-purple-500'}
                  icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>}
                  label="Avg Duration"
                  value={formatDuration(currentStats.avg_duration_ms)}
                />
              </div>

            </div>
          )}

          {/* Inline status summary (always visible) */}
          {!showDashboard && executions.length > 0 && (
            <div className="flex items-center gap-4 text-xs text-slate-500">
              <span>{executions.length} total</span>
              {successCount > 0 && <span className="text-green-600">{successCount} succeeded</span>}
              {errorCount > 0 && <span className="text-red-500">{errorCount} failed</span>}
              {runningCount > 0 && <span className="text-blue-500">{runningCount} running</span>}
            </div>
          )}

          {/* ─── Unified filter bar (2026-05-27) ─────────────────
              One bar above the table that owns every "what am I
              looking at?" lever — time range, status, project,
              pipeline, custom date range. Replaces three separate
              filter zones (period selector inside the dashboard,
              status tabs, project/pipeline/date bar). Active filters
              chip below the controls so the applied state stays
              legible without re-scanning the controls themselves.
              Reset returns the whole bar to defaults (24h, All).
          */}
          {(() => {
            const activeCount = [
              filter !== 'all',
              !!projectFilter,
              !!workflowFilter,
              !!dateFrom,
              !!dateTo,
            ].filter(Boolean).length;
            const projName = projectOptions.find(p => p.id === projectFilter)?.name;
            const wfName = workflowOptions.find(w => w.id === workflowFilter)?.name;
            const inputCls = (active: boolean) =>
              `px-2.5 py-1.5 text-xs rounded-md border outline-none transition-colors focus:ring-2 ${
                active
                  ? (dark
                      ? 'bg-blue-500/10 border-blue-500/40 text-blue-200 focus:ring-blue-500/40'
                      : 'bg-blue-50 border-blue-300 text-blue-800 focus:ring-blue-300')
                  : (dark
                      ? 'bg-[#0d1422] border-white/[0.1] text-slate-200 focus:ring-blue-500/30'
                      : 'bg-white border-slate-200 text-slate-700 focus:ring-blue-200')
              }`;
            const segmentBtn = (active: boolean) =>
              `px-3 py-1.5 text-xs font-semibold rounded-md transition-colors ${
                active
                  ? (dark ? 'bg-white/10 text-white shadow-sm' : 'bg-slate-800 text-white shadow-sm')
                  : (dark ? 'text-slate-400 hover:text-slate-200' : 'text-slate-500 hover:text-slate-700')
              }`;
            const statusBtn = (active: boolean) =>
              `px-3 py-1.5 text-xs font-semibold rounded-md transition-colors capitalize ${
                active
                  ? (dark ? 'bg-white/10 text-blue-400' : 'bg-pipe-100 text-pipe-700')
                  : (dark ? 'text-slate-400 hover:text-slate-200' : 'text-slate-500 hover:text-slate-700')
              }`;
            return (
              <div
                className={`rounded-xl border-l-4 border-y border-r shadow-sm overflow-hidden ${
                  dark
                    ? 'bg-gradient-to-r from-indigo-500/[0.08] via-[#111827] to-[#111827] border-l-indigo-400/60 border-y-white/[0.08] border-r-white/[0.08]'
                    : 'bg-gradient-to-r from-indigo-50/80 via-white to-white border-l-indigo-400 border-y-slate-200 border-r-slate-200'
                }`}
              >
                {/* Row 1 — controls */}
                <div className="flex items-center gap-3 flex-wrap p-3">
                  {/* Filter label + active count */}
                  <div className={`flex items-center gap-2 pr-3 mr-1 border-r self-stretch ${dark ? 'border-white/[0.06]' : 'border-slate-200'}`}>
                    <div className={`w-7 h-7 rounded-lg flex items-center justify-center ${dark ? 'bg-indigo-500/15' : 'bg-indigo-100'}`}>
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={dark ? 'text-indigo-300' : 'text-indigo-600'}>
                        <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
                      </svg>
                    </div>
                    <span className={`text-xs font-bold uppercase tracking-wider ${dark ? 'text-slate-200' : 'text-slate-700'}`}>Filter</span>
                    {activeCount > 0 && (
                      <span className={`text-xs font-bold px-1.5 py-0.5 rounded-full ${dark ? 'bg-blue-500/15 text-blue-300' : 'bg-blue-100 text-blue-700'}`}>
                        {activeCount}
                      </span>
                    )}
                  </div>

                  {/* Time range — segmented control, drives KPIs AND table */}
                  <div className={`flex gap-1 rounded-lg border p-1 ${dark ? 'bg-[#0d1422] border-white/[0.1]' : 'bg-white border-slate-200'}`}>
                    {(['24h', '7d', '30d', 'custom'] as const).map(p => (
                      <button
                        key={p}
                        onClick={() => setSelectedPeriod(p)}
                        className={segmentBtn(selectedPeriod === p)}
                        title={p === 'custom' ? 'Use the From/To pickers for a closed interval' : `Last ${p}`}
                      >
                        {p === 'custom' ? 'Custom' : p}
                      </button>
                    ))}
                  </div>

                  {/* Status — segmented control */}
                  <div className={`flex gap-1 rounded-lg border p-1 ${dark ? 'bg-[#0d1422] border-white/[0.1]' : 'bg-white border-slate-200'}`}>
                    {(['all', 'success', 'error', 'running'] as const).map((f) => (
                      <button
                        key={f}
                        onClick={() => setFilter(f)}
                        className={statusBtn(filter === f)}
                      >
                        {f}
                        {f === 'error' && errorCount > 0 && (
                          <span className={`ml-1 text-[9px] px-1.5 py-0.5 rounded-full ${dark ? 'bg-red-500/20 text-red-400' : 'bg-red-100 text-red-500'}`}>{errorCount}</span>
                        )}
                        {f === 'running' && runningCount > 0 && (
                          <span className={`ml-1 text-[9px] px-1.5 py-0.5 rounded-full ${dark ? 'bg-blue-500/20 text-blue-400' : 'bg-blue-100 text-blue-500'}`}>{runningCount}</span>
                        )}
                      </button>
                    ))}
                  </div>

                  {/* Project */}
                  <select
                    value={projectFilter}
                    onChange={(e) => { setProjectFilter(e.target.value); setWorkflowFilter(''); }}
                    className={`${inputCls(!!projectFilter)} min-w-[160px]`}
                    aria-label="Project"
                  >
                    <option value="">All projects</option>
                    {projectOptions.map(p => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>

                  {/* Pipeline */}
                  <select
                    value={workflowFilter}
                    onChange={(e) => setWorkflowFilter(e.target.value)}
                    className={`${inputCls(!!workflowFilter)} min-w-[200px]`}
                    aria-label="Pipeline"
                  >
                    <option value="">All pipelines{projectFilter ? ' in this project' : ''}</option>
                    {workflowOptions.map(w => (
                      <option key={w.id} value={w.id}>{w.name}</option>
                    ))}
                  </select>

                  {/* From / To — only visible when the user picked 'custom'.
                      Keeps the bar clean for the rolling-window cases that
                      cover ~95% of usage. */}
                  {selectedPeriod === 'custom' && (
                    <>
                      <input
                        type="date"
                        value={dateFrom}
                        onChange={(e) => setDateFrom(e.target.value)}
                        className={inputCls(!!dateFrom)}
                        aria-label="From date"
                      />
                      <span className={`text-xs ${dark ? 'text-slate-500' : 'text-slate-400'}`}>→</span>
                      <input
                        type="date"
                        value={dateTo}
                        onChange={(e) => setDateTo(e.target.value)}
                        min={dateFrom || undefined}
                        className={inputCls(!!dateTo)}
                        aria-label="To date"
                      />
                    </>
                  )}

                  {/* Reset — returns everything to defaults (24h, all) */}
                  {(activeCount > 0 || selectedPeriod !== '24h') && (
                    <button
                      onClick={clearAllFilters}
                      className={`ml-auto px-3 py-1.5 text-xs font-semibold rounded-md border transition-colors ${dark ? 'text-slate-300 bg-white/[0.06] border-white/[0.1] hover:bg-white/[0.1]' : 'text-slate-600 bg-slate-50 border-slate-200 hover:bg-slate-100'}`}
                      title="Reset all filters to defaults (24h, All)"
                    >
                      Reset
                    </button>
                  )}
                </div>

                {/* Row 2 — active filter chips. The time range chip
                    is always shown (24h is the default but worth
                    being visible) so the user always sees the
                    window they're inside of. */}
                <div className={`flex items-center gap-2 flex-wrap px-3 py-2 border-t ${dark ? 'border-white/[0.04] bg-[#0d1422]' : 'border-slate-100 bg-slate-50/60'}`}>
                  <span className={`text-xs font-bold uppercase tracking-wider ${dark ? 'text-slate-500' : 'text-slate-500'}`}>Active</span>
                  <FilterChip
                    dark={dark}
                    label="Time"
                    value={selectedPeriod === 'custom' ? `${dateFrom || '∞'} → ${dateTo || 'now'}` : `Last ${selectedPeriod}`}
                    onClear={() => { setSelectedPeriod('24h'); setDateFrom(''); setDateTo(''); }}
                  />
                  {filter !== 'all' && (
                    <FilterChip dark={dark} label="Status" value={filter} onClear={() => setFilter('all')} />
                  )}
                  {projectFilter && (
                    <FilterChip dark={dark} label="Project" value={projName || projectFilter} onClear={() => { setProjectFilter(''); setWorkflowFilter(''); }} />
                  )}
                  {workflowFilter && (
                    <FilterChip dark={dark} label="Pipeline" value={wfName || workflowFilter} onClear={() => setWorkflowFilter('')} />
                  )}
                </div>
              </div>
            );
          })()}

          {loading ? (
            <div className="flex items-center justify-center py-20">
              <div className="w-6 h-6 border-2 border-pipe-300 border-t-transparent rounded-full animate-spin" />
            </div>
          ) : filtered.length === 0 ? (
            <div className="text-center py-20">
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#cbd5e1" strokeWidth="1.5" className="mx-auto mb-4">
                <circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" />
              </svg>
              <h2 className="text-lg font-bold text-slate-700 mb-2">No executions yet</h2>
              <p className="text-sm text-slate-500">
                Run a pipeline to see execution history here.
              </p>
            </div>
          ) : (
            <>
            <div className={`rounded-lg border shadow-sm overflow-x-auto ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
              <TableToolbar<Execution>
                data={filtered}
                columns={EXEC_COLUMNS}
                columnGroups={EXEC_GROUPS}
                visibleColumns={visibleColumns}
                activeColumnCount={activeColumns.length}
                onToggleColumn={toggleColumn}
                onResetDefaults={resetToDefaults}
                onSelectAll={selectAll}
                searchValue={tableSearch}
                onSearchChange={setTableSearch}
                searchPlaceholder="Search by pipeline, status, trigger..."
                recordLabel="execution"
                exportFilename="executions"
                exportRowBuilder={(e) => ({
                  id: e.id,
                  status: e.status,
                  workflow: e.workflow_name || e.workflow_id,
                  env: e.environment || 'dev',
                  trigger: e.triggered_by || 'manual',
                  started: e.started_at,
                  completed: e.completed_at || '',
                  duration: e.duration_ms ? `${e.duration_ms}ms` : '',
                  steps: `${e.steps_completed ?? 0}/${e.steps_total ?? 0}`,
                  error: e.error_message || '',
                })}
              />
              <table className="w-full text-sm border-collapse">
                <thead>
                  {/* Canonical navy-blue/amber header */}
                  <tr className="border-b-2 border-amber-400/40 bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900">
                    {isVisible('status') && <th className="px-4 py-2.5 text-left text-xs font-bold text-amber-300 uppercase tracking-wider">Status</th>}
                    {isVisible('workflow') && <th className="px-4 py-2.5 text-left text-xs font-bold text-amber-300 uppercase tracking-wider">Pipeline</th>}
                    {isVisible('env') && <th className="px-4 py-2.5 text-left text-xs font-bold text-amber-300 uppercase tracking-wider">Env</th>}
                    {isVisible('trigger') && <th className="px-4 py-2.5 text-left text-xs font-bold text-amber-300 uppercase tracking-wider">Trigger</th>}
                    {isVisible('started') && <th className="px-4 py-2.5 text-left text-xs font-bold text-amber-300 uppercase tracking-wider">Started</th>}
                    {isVisible('duration') && <th className="px-4 py-2.5 text-left text-xs font-bold text-amber-300 uppercase tracking-wider">Duration</th>}
                    {isVisible('steps') && <th className="px-4 py-2.5 text-left text-xs font-bold text-amber-300 uppercase tracking-wider">Steps</th>}
                    {isVisible('id') && <th className="px-4 py-2.5 text-left text-xs font-bold text-amber-300 uppercase tracking-wider">Run ID</th>}
                    {isVisible('completed') && <th className="px-4 py-2.5 text-left text-xs font-bold text-amber-300 uppercase tracking-wider">Completed</th>}
                    {isVisible('error') && <th className="px-4 py-2.5 text-left text-xs font-bold text-amber-300 uppercase tracking-wider">Error</th>}
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((exec) => (
                    <tr
                      key={exec.id}
                      onClick={() => loadDetail(exec)}
                      className={`border-b transition-colors cursor-pointer ${
                        dark
                          ? `border-white/[0.04] hover:bg-white/[0.03] ${selectedExec?.id === exec.id ? 'bg-white/[0.05]' : ''}`
                          : `border-slate-100 hover:bg-slate-50/50 ${selectedExec?.id === exec.id ? 'bg-pipe-50/30' : ''}`
                      }`}
                    >
                      {isVisible('status') && (
                        <td className="px-4 py-3">
                          {/* OSS-2 (2026-05-19) — unified <StatusPill> replaces
                              the per-page STATUS_STYLES_LIGHT/DARK palette. */}
                          <StatusPill status={exec.status || 'pending'} size="sm" />
                        </td>
                      )}
                      {isVisible('workflow') && (
                        <td className="px-4 py-3 max-w-[260px]">
                          <span
                            className="font-medium text-slate-700 truncate block"
                            title={exec.workflow_name || exec.workflow_id}
                          >
                            {exec.workflow_name || exec.workflow_id}
                          </span>
                        </td>
                      )}
                      {isVisible('env') && (
                        <td className="px-4 py-3">
                          <span className={`text-xs font-semibold uppercase px-1.5 py-0.5 rounded-full ${
                            exec.environment === 'prod' ? (dark ? 'bg-emerald-500/15 text-emerald-400' : 'bg-emerald-50 text-emerald-600') :
                            exec.environment === 'dev' ? (dark ? 'bg-amber-500/15 text-amber-400' : 'bg-amber-50 text-amber-600') :
                            (dark ? 'bg-white/5 text-slate-500' : 'bg-slate-50 text-slate-400')
                          }`}>
                            {exec.environment || 'dev'}
                          </span>
                        </td>
                      )}
                      {isVisible('trigger') && (
                        <td className="px-4 py-3 text-xs">
                          {(() => {
                            // Map every backend trigger string to one of four buckets so
                            // the user can tell at a glance: manual run, scheduled run,
                            // pre-publish test, or replay/api/websocket. The raw value
                            // stays in the title attribute for power users.
                            const raw = (exec.triggered_by || 'manual').toLowerCase();
                            const kind =
                              raw === 'schedule' ? 'schedule' :
                              raw === 'test' ? 'test' :
                              raw.startsWith('replay') ? 'replay' :
                              raw === 'manual' ? 'manual' :
                              'api';
                            const styles: Record<string, { bg: string; text: string; label: string }> = {
                              manual:   { bg: 'bg-blue-50',    text: 'text-blue-700',    label: 'Manual' },
                              schedule: { bg: 'bg-purple-50',  text: 'text-purple-700',  label: 'Scheduled' },
                              test:     { bg: 'bg-emerald-50', text: 'text-emerald-700', label: 'Pre-publish test' },
                              replay:   { bg: 'bg-amber-50',   text: 'text-amber-700',   label: 'Replay' },
                              api:      { bg: 'bg-slate-100',  text: 'text-slate-700',   label: 'API / Webhook' },
                            };
                            const s = styles[kind];
                            return (
                              <span
                                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full font-medium text-xs ${s.bg} ${s.text}`}
                                title={`Trigger: ${exec.triggered_by || 'manual'}`}
                              >
                                {s.label}
                              </span>
                            );
                          })()}
                        </td>
                      )}
                      {isVisible('started') && <td className="px-4 py-3"><TimeAgo value={exec.started_at} className="text-xs !text-slate-500" /></td>}
                      {isVisible('duration') && <td className="px-4 py-3 text-slate-500 text-xs font-mono">{formatDuration(exec.duration_ms)}</td>}
                      {isVisible('steps') && (
                        <td className="px-4 py-3 text-slate-500 text-xs">
                          <span className={exec.steps_failed ? 'text-red-500' : ''}>
                            {exec.steps_completed ?? 0}/{exec.steps_total ?? 0}
                          </span>
                          {(exec.steps_failed || 0) > 0 && (
                            <span className="ml-1 text-red-400 text-xs">({exec.steps_failed} failed)</span>
                          )}
                        </td>
                      )}
                      {isVisible('id') && <td className="px-4 py-3 text-slate-400 text-xs font-mono">{exec.id.slice(0, 8)}...</td>}
                      {isVisible('completed') && <td className="px-4 py-3"><TimeAgo value={exec.completed_at} className="text-xs !text-slate-500" /></td>}
                      {isVisible('error') && (
                        <td className="px-4 py-3 text-xs text-red-500 max-w-[200px] truncate" title={exec.error_message || ''}>
                          {exec.error_message || '\u2014'}
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            </>
          )}
          </>
          )}
        </div>
        )}
      </div>

      {/* Execution Detail — embedded in the page content area so the
          sidebar + top nav stay visible (app-template compliance). */}
      {selectedExec && (
        <div className={`flex-1 flex flex-col overflow-auto ${dark ? 'bg-[#0B1220]' : 'bg-canvas-bg'}`}>
          {/* ─── Standard page header (matches list view template) ─── */}
          <div className={`sticky top-0 z-30 border-b ${environment === 'prod' ? 'bg-slate-900 border-slate-700' : 'bg-gradient-to-b from-slate-200 to-slate-300 border-slate-400/70'}`}>
            <div className="px-8 h-[78px] flex items-center justify-between">
              <div>
                <h1 className={`text-xl font-bold flex items-center gap-2 ${environment === 'prod' ? 'text-white' : 'text-slate-800'}`}>
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className={environment === 'prod' ? 'text-red-400' : 'text-blue-500'}>
                    <circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" />
                  </svg>
                  Execution Summary
                  <TierChip tier={tier} environment={environment} />
                </h1>
                <p className={`text-xs mt-0.5 ${environment === 'prod' ? 'text-slate-400' : 'text-slate-500'}`}>
                  Run details, lineage, and step-by-step output
                </p>
              </div>
            </div>
          </div>

          {/* ─── Breadcrumb row — Back lives here as a real button, not
              a tiny text link, so it's findable on a busy page. The
              execution id sits next to it for quick scanning. */}
          <div className={`border-b shrink-0 px-8 py-3 flex items-center gap-3 ${dark ? 'bg-[#0d1422] border-white/[0.04]' : 'bg-slate-50 border-slate-200'}`}>
            <button
              onClick={() => {
                setSelectedExec(null);
                // Clear the deep-link run id from the hash. The openFromHash
                // effect re-runs when selectedExec changes; if the hash still
                // held `executions/<id>` it would immediately re-open this run,
                // making Back a no-op for anyone who arrived via a deep-link
                // (e.g. Dashboard → Needs Attention → View →). Resetting to the
                // bare `executions` route keeps the user on the list.
                try {
                  const h = window.location.hash.replace('#', '');
                  if (h.startsWith('executions/')) window.location.hash = 'executions';
                } catch { /* ignore */ }
              }}
              className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-semibold rounded-lg border transition-colors ${dark ? 'text-slate-200 bg-white/[0.06] border-white/[0.1] hover:bg-white/[0.1] hover:border-white/[0.15]' : 'text-slate-700 bg-white border-slate-300 hover:bg-slate-50 hover:border-slate-400'}`}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="15 18 9 12 15 6" /></svg>
              Back to Executions
            </button>
            <span className={dark ? 'text-slate-600' : 'text-slate-300'}>/</span>
            <span className={`text-xs font-mono ${dark ? 'text-slate-400' : 'text-slate-500'}`}>{selectedExec.id}</span>
          </div>

          {/* ─── Run summary card — workflow name, status, version, meta + actions ─── */}
          <div className={`border-b shrink-0 px-8 py-4 ${dark ? 'bg-gradient-to-br from-[#141d31] to-[#111827] border-white/[0.06]' : 'bg-gradient-to-br from-white via-white to-indigo-50/50 border-slate-200'}`}>
            <div className="flex items-start gap-4">
              <div className={`w-10 h-10 rounded-lg flex items-center justify-center shrink-0 ${dark ? 'bg-indigo-500/15 border border-indigo-500/20' : 'bg-indigo-50 border border-indigo-200'}`}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" /></svg>
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <h2 className={`text-base font-bold truncate ${dark ? 'text-slate-100' : 'text-slate-800'}`}>{selectedExec.workflow_name}</h2>
                  {/* OSS-2 (2026-05-19) — unified <StatusPill>. The
                      animated dot for the 'running' state is built into
                      the shared pill, so the inline span wrapper is gone. */}
                  <StatusPill status={selectedExec.status || 'pending'} />
                  {workflowDef?.version != null && (
                    <span
                      className={`inline-flex items-center gap-1 text-xs font-mono font-bold px-2 py-0.5 rounded ${dark ? 'bg-slate-800 text-slate-300 border border-slate-700' : 'bg-slate-100 text-slate-600 border border-slate-200'}`}
                      title={`Pipeline is currently at v${workflowDef.version}${workflowDef.deployed_version ? ` (deployed: v${workflowDef.deployed_version})` : ''}. This run's exact IR is preserved in the snapshot.`}
                    >
                      v{workflowDef.version}
                    </span>
                  )}
                </div>
                <p className="text-xs text-slate-400 font-mono mt-0.5">{selectedExec.workflow_id}</p>
              </div>

              {/* Actions — kept on the right of the summary card, not in
                  the page header. Per docs/PAGE_BY_PAGE_AUDIT.md (P0 #3,
                  2026-05-19): Cancel was a stub, the extra Export button
                  was dead UI (real CSV export lives in the list-view
                  TableToolbar), and Re-run shared the Open-in-Editor
                  handler so its label lied. Cancel and Re-run are now
                  real; Export is removed. */}
              <div className="flex items-center gap-2 shrink-0">
                {selectedExec.status === 'running' ? (
                  <button
                    onClick={async () => {
                      if (!selectedExec.workflow_id || cancellingId === selectedExec.id) return;
                      const ok = await uiConfirm({
                        title: 'Cancel this run?',
                        message: 'The pipeline will stop at the next safe checkpoint. Partial writes that already completed remain on the target.',
                        confirmLabel: 'Cancel run',
                        destructive: true,
                      });
                      if (!ok) return;
                      setCancellingId(selectedExec.id);
                      try {
                        await api.cancelExecution(selectedExec.workflow_id);
                        toast.info('Cancelling…', 'The worker will stop at the next safe checkpoint.');
                      } catch (err: any) {
                        toast.error('Cancel failed', err?.message || 'The worker did not accept the cancel.');
                      } finally {
                        setCancellingId(null);
                      }
                    }}
                    disabled={cancellingId === selectedExec.id}
                    className="px-3 py-1.5 text-xs font-medium text-red-600 bg-red-50 border border-red-200 rounded-lg hover:bg-red-100 transition-colors flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="6" y="6" width="12" height="12" /></svg>
                    {cancellingId === selectedExec.id ? 'Cancelling…' : 'Cancel'}
                  </button>
                ) : (
                  <>
                    <button
                      onClick={() => loadExecutionIntoEditor(selectedExec)}
                      disabled={!selectedExec.workflow_snapshot}
                      title={selectedExec.workflow_snapshot
                        ? 'Load the exact pipeline from this run into the editor for inspection or re-run'
                        : 'No snapshot — this run predates snapshot storage'}
                      className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors flex items-center gap-1.5 disabled:opacity-40 disabled:cursor-not-allowed ${dark ? 'text-emerald-400 bg-emerald-500/15 border border-emerald-500/20 hover:bg-emerald-500/25' : 'text-emerald-700 bg-emerald-50 border border-emerald-200 hover:bg-emerald-100'}`}
                    >
                      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><path d="m9 18 3-3-3-3" /><path d="M15 15h-3" /></svg>
                      Open in Editor
                    </button>
                    <button
                      onClick={async () => {
                        if (!selectedExec.workflow_id || rerunningId === selectedExec.id) return;
                        const ok = await uiConfirm({
                          title: 'Re-run this pipeline?',
                          message: 'A fresh execution will start in DEV using the pipeline\'s current draft (not necessarily the snapshot captured for this past run). To re-run the exact past version, use Open in Editor and run from there.',
                          confirmLabel: 'Re-run',
                        });
                        if (!ok) return;
                        setRerunningId(selectedExec.id);
                        try {
                          const res = await api.runWorkflow(selectedExec.workflow_id, true, 'dev', 'live');
                          toast.success('Re-run started', `Execution ${String(res?.execution_id || res?.id || '').slice(0, 8)} kicked off — refresh in a moment.`);
                        } catch (err: any) {
                          toast.error('Re-run failed', err?.message || 'The worker did not accept the re-run.');
                        } finally {
                          setRerunningId(null);
                        }
                      }}
                      disabled={rerunningId === selectedExec.id || !selectedExec.workflow_id}
                      title="Start a fresh run of the current draft of this pipeline. To re-run the exact past version, click Open in Editor and use the Run button there."
                      className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors flex items-center gap-1.5 disabled:opacity-40 disabled:cursor-not-allowed ${dark ? 'text-blue-400 bg-blue-500/15 border border-blue-500/20 hover:bg-blue-500/25' : 'text-blue-600 bg-blue-50 border border-blue-200 hover:bg-blue-100'}`}
                    >
                      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M1 4v6h6" /><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" /></svg>
                      {rerunningId === selectedExec.id ? 'Re-running…' : 'Re-run'}
                    </button>
                    {/* D1 — Replay: re-execute the EXACT historical IR
                        snapshot (not the current draft). A capability that
                        tools which don't capture a deterministic per-run IR
                        snapshot can't offer. Disabled when the run predates
                        snapshot storage. */}
                    <button
                      onClick={async () => {
                        if (!selectedExec.id || replayingId === selectedExec.id) return;
                        setReplayingId(selectedExec.id);
                        setReplayResult(null);
                        try {
                          const res = await api.replayExecution(selectedExec.id);
                          setReplayResult(res);
                          if (res.diff.status_changed) {
                            toast.info(
                              'Replay complete — status changed',
                              `Original was ${selectedExec.status || '?'}, replay is ${res.status}. See diff card below.`,
                            );
                          } else {
                            toast.success(
                              'Replay complete',
                              `Same status, ${res.diff.rows_delta >= 0 ? '+' : ''}${res.diff.rows_delta} rows vs original.`,
                            );
                          }
                        } catch (err: any) {
                          toast.error('Replay failed', err?.message || 'The replay endpoint rejected the request.');
                        } finally {
                          setReplayingId(null);
                        }
                      }}
                      disabled={replayingId === selectedExec.id || !selectedExec.workflow_snapshot}
                      title={selectedExec.workflow_snapshot
                        ? 'Re-execute the EXACT IR snapshot from this run. Compares result with the original.'
                        : 'No IR snapshot — this run predates snapshot storage'}
                      className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors flex items-center gap-1.5 disabled:opacity-40 disabled:cursor-not-allowed ${dark ? 'text-purple-400 bg-purple-500/15 border border-purple-500/20 hover:bg-purple-500/25' : 'text-purple-700 bg-purple-50 border border-purple-200 hover:bg-purple-100'}`}
                    >
                      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>
                      {replayingId === selectedExec.id ? 'Replaying…' : 'Replay'}
                    </button>
                    {/* Resume from failed step — skips successful steps,
                        re-executes from the first failed step. Backend uses
                        existing workflow-resume primitive that loads upstream
                        outputs from parquet snapshots. */}
                    {selectedExec.status === 'error' && (
                      <button
                        onClick={async () => {
                          if (!selectedExec.workflow_id || resumeLoading) return;
                          const ok = await uiConfirm({
                            title: 'Resume from failed step?',
                            message: 'Successful steps will be skipped and loaded from cached output. Execution starts at the first failed step. Use this when you fixed the upstream config and want to avoid re-running expensive successful work.',
                            confirmLabel: 'Resume',
                          });
                          if (!ok) return;
                          setResumeLoading(true);
                          try {
                            const res = await api.resumeWorkflow(selectedExec.workflow_id, selectedExec.id);
                            toast.success('Resume started', `Execution ${String(res?.execution_id || res?.id || '').slice(0, 8)} resumed from first failed step.`);
                          } catch (err: any) {
                            toast.error('Resume failed', err?.message || 'The worker did not accept the resume.');
                          } finally {
                            setResumeLoading(false);
                          }
                        }}
                        disabled={resumeLoading || !selectedExec.workflow_id}
                        title="Re-run starting from the first failed step. Skips successful steps using cached output."
                        className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors flex items-center gap-1.5 disabled:opacity-40 disabled:cursor-not-allowed ${dark ? 'text-amber-400 bg-amber-500/15 border border-amber-500/20 hover:bg-amber-500/25' : 'text-amber-700 bg-amber-50 border border-amber-200 hover:bg-amber-100'}`}
                      >
                        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <polyline points="13 17 18 12 13 7" />
                          <polyline points="6 17 11 12 6 7" />
                        </svg>
                        {resumeLoading ? 'Resuming…' : 'Resume from failed'}
                      </button>
                    )}
                  </>
                )}
                {/* Export button removed — the real CSV export lives in the
                    list-view TableToolbar. A second Export button next to
                    Cancel / Re-run was dead UI with no onClick. */}
              </div>
            </div>

            {/* D1 — Replay diff card. Renders after a successful
                replay; shows status delta + rows delta + per-step diff.
                Dismissed when the user navigates to a different run
                (replayResult is cleared in the row-click handler) or
                clicks the small × in the corner. */}
            {replayResult && replayResult.original_id === selectedExec.id && (
              <div className={`mt-3 rounded-lg border p-3 ${
                replayResult.diff.status_changed
                  ? (dark ? 'bg-amber-500/10 border-amber-500/30' : 'bg-amber-50 border-amber-300')
                  : (dark ? 'bg-emerald-500/10 border-emerald-500/30' : 'bg-emerald-50 border-emerald-300')
              }`}>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className={`text-xs font-bold uppercase tracking-wider ${
                      replayResult.diff.status_changed
                        ? (dark ? 'text-amber-300' : 'text-amber-700')
                        : (dark ? 'text-emerald-300' : 'text-emerald-700')
                    }`}>
                      {replayResult.diff.status_changed ? 'Replay differs' : 'Replay matches'}
                    </div>
                    <div className={`text-xs mt-1 ${dark ? 'text-slate-300' : 'text-slate-700'}`}>
                      <span className="font-semibold">Original</span>{' '}{selectedExec.status || '?'} →{' '}
                      <span className="font-semibold">Replay</span>{' '}{replayResult.status}
                      <span className="mx-2 text-slate-400">·</span>
                      <span className="font-mono">{replayResult.diff.rows_delta >= 0 ? '+' : ''}{replayResult.diff.rows_delta}</span> rows
                      <span className="mx-2 text-slate-400">·</span>
                      <span className="font-mono">{replayResult.diff.duration_delta_ms >= 0 ? '+' : ''}{Math.round(replayResult.diff.duration_delta_ms)}</span> ms
                      {replayResult.diff.ir_sha_match && (
                        <>
                          <span className="mx-2 text-slate-400">·</span>
                          <span className={dark ? 'text-emerald-300' : 'text-emerald-700'}>IR sha matches</span>
                        </>
                      )}
                    </div>
                    {replayResult.diff.steps.some((s) => s.changed) && (
                      <ul className="mt-2 space-y-0.5">
                        {replayResult.diff.steps.filter((s) => s.changed).slice(0, 6).map((s) => (
                          <li key={s.step_id} className={`text-[11px] font-mono ${dark ? 'text-slate-300' : 'text-slate-700'}`}>
                            <span className="text-slate-400">{s.step_name}:</span>{' '}
                            {s.a_status} → {s.b_status}
                            {s.a_rows !== s.b_rows && (
                              <span className="text-slate-500"> ({s.a_rows} → {s.b_rows} rows)</span>
                            )}
                          </li>
                        ))}
                      </ul>
                    )}
                    <div className="mt-2 text-[10px] text-slate-400 font-mono">
                      Replay id: {replayResult.replay_id}
                    </div>
                  </div>
                  <button
                    onClick={() => setReplayResult(null)}
                    className={`shrink-0 w-6 h-6 inline-flex items-center justify-center rounded ${dark ? 'text-slate-400 hover:text-slate-200 hover:bg-white/[0.06]' : 'text-slate-400 hover:text-slate-700 hover:bg-slate-100'}`}
                    title="Dismiss"
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                  </button>
                </div>
              </div>
            )}

            {/* L1.1 frontend — runtime lineage for this run (lazy;
                fetches only when expanded). Surfaces the per-step
                columns/rows/timing recorded by the executor. */}
            {selectedExec.id && (
              <CollapsibleLineage runId={selectedExec.id} dark={dark} />
            )}

            {/* Meta strip — compact stat cards (Duration / Started / Trigger
                / Version / Snapshot + memory & cpu when sampled). Reads as a
                stats bar rather than a row of floating grey text. */}
            <div className="mt-3 flex items-stretch gap-2 flex-wrap">
              {([
                { label: 'Duration', value: formatDuration(selectedExec.duration_ms), mono: true },
                { label: 'Started', value: formatTime(selectedExec.started_at) },
                { label: 'Trigger', value: selectedExec.triggered_by || 'manual', cap: true },
                {
                  label: 'Pipeline version',
                  value: workflowDef?.version != null
                    ? `v${workflowDef.version}${selectedExec.workflow_snapshot ? '' : ' (no snapshot)'}`
                    : selectedExec.workflow_snapshot ? 'snapshot only' : '—',
                  mono: true,
                },
                {
                  label: 'Snapshot',
                  value: selectedExec.workflow_snapshot ? 'Captured' : 'Legacy',
                  tone: selectedExec.workflow_snapshot ? 'emerald' : 'muted',
                },
                ...(selectedExec.metadata?.peak_memory_mb
                  ? [{ label: 'Peak memory', value: `${Number(selectedExec.metadata.peak_memory_mb).toFixed(0)} MB`, mono: true }]
                  : []),
                ...(selectedExec.metadata?.cpu_seconds !== undefined && Number(selectedExec.metadata.cpu_seconds) > 0
                  ? [{ label: 'CPU', value: `${Number(selectedExec.metadata.cpu_seconds).toFixed(1)}s`, mono: true }]
                  : []),
              ] as Array<{ label: string; value: string; mono?: boolean; cap?: boolean; tone?: string }>).map((stat) => (
                <div
                  key={stat.label}
                  className={`rounded-lg border px-3 py-1.5 min-w-[84px] ${dark ? 'bg-white/[0.03] border-white/[0.07]' : 'bg-slate-50/80 border-slate-200'}`}
                >
                  <div className="text-[10px] text-slate-400 uppercase tracking-wider">{stat.label}</div>
                  <div className={`text-xs font-semibold ${stat.mono ? 'font-mono' : ''} ${stat.cap ? 'capitalize' : ''} ${
                    stat.tone === 'emerald' ? (dark ? 'text-emerald-400' : 'text-emerald-600')
                    : stat.tone === 'muted' ? (dark ? 'text-slate-500' : 'text-slate-400')
                    : (dark ? 'text-slate-200' : 'text-slate-700')
                  }`}>{stat.value}</div>
                </div>
              ))}
              {/* 2026-05-25 — Storage outputs link (interactive card). Joined
                  server-side from storage_objects WHERE run_id={execution_id};
                  hidden when the run wrote nothing to Storage. */}
              {selectedExec.storage_outputs && selectedExec.storage_outputs.length > 0 && (
                <button
                  type="button"
                  onClick={() => {
                    try { localStorage.setItem('fpulse_storage_tab', 'outputs'); } catch { /* ignore */ }
                    window.location.hash = 'storage';
                  }}
                  title={selectedExec.storage_outputs.map((o) => o.name).join('\n')}
                  className={`rounded-lg border px-3 py-1.5 text-left transition-colors ${dark ? 'bg-emerald-500/10 border-emerald-500/20 hover:bg-emerald-500/20' : 'bg-emerald-50 border-emerald-200 hover:bg-emerald-100'}`}
                >
                  <div className="text-[10px] text-slate-400 uppercase tracking-wider">Storage outputs</div>
                  <div className={`text-xs font-semibold ${dark ? 'text-emerald-300' : 'text-emerald-700'}`}>
                    {selectedExec.storage_outputs.length} file{selectedExec.storage_outputs.length === 1 ? '' : 's'} →
                  </div>
                </button>
              )}
            </div>
          </div>

          {/* ─── Sidebar + content split — opens here so it spans the
              Failure Summary, Parameter Values, AND the per-tab body.
              Earlier the sidebar started below the Failure Summary,
              which made it look like an orphaned strip next to the
              Steps table; full-height fixes that. */}
          <div className="flex-1 flex overflow-hidden">
            <nav
              aria-label="Execution detail sections"
              className={`shrink-0 w-44 border-r flex flex-col pt-3 pb-14 ${dark ? 'bg-[#111827] border-white/[0.06]' : 'bg-gradient-to-b from-slate-50 to-white border-slate-200'}`}
            >
              <div className={`px-4 pb-2 text-[10px] font-bold uppercase tracking-wider ${dark ? 'text-slate-500' : 'text-slate-400'}`}>Views</div>
              {([
                { key: 'lineage' as const, label: 'Lineage', icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" /></svg> },
                { key: 'steps' as const, label: `Steps${selectedExec.step_logs?.length ? ` (${selectedExec.step_logs.length})` : ''}`, icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" /><line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" /></svg> },
                { key: 'logs' as const, label: 'Logs', icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /></svg> },
              ]).map(tab => {
                const active = detailTab === tab.key;
                return (
                  <button
                    key={tab.key}
                    onClick={() => setDetailTab(tab.key)}
                    aria-current={active ? 'page' : undefined}
                    className={`mx-2 mb-1 flex items-center gap-2.5 px-3 py-2 text-sm font-semibold rounded-lg border-l-2 transition-colors ${
                      active
                        ? dark
                          ? 'text-indigo-300 bg-indigo-500/10 border-indigo-400'
                          : 'text-indigo-700 bg-indigo-50 border-indigo-500'
                        : dark
                          ? 'text-slate-400 border-transparent hover:text-slate-200 hover:bg-white/[0.04]'
                          : 'text-slate-500 border-transparent hover:text-slate-700 hover:bg-slate-50'
                    }`}
                  >
                    {tab.icon}
                    {tab.label}
                  </button>
                );
              })}

              {/* Run mini-summary pinned to the bottom so the rail reads as
                  intentional rather than an empty strip. */}
              <div className={`mt-auto mx-3 pt-3 border-t ${dark ? 'border-white/[0.06]' : 'border-slate-200'}`}>
                <div className="mb-2"><StatusPill status={selectedExec.status || 'pending'} /></div>
                <div className="space-y-1 text-[11px]">
                  <div className="flex items-center justify-between">
                    <span className="text-slate-400">Steps</span>
                    <span className={`font-mono font-semibold ${dark ? 'text-slate-300' : 'text-slate-600'}`}>
                      {(selectedExec.step_logs || []).filter(s => s.status === 'success').length}/{selectedExec.step_logs?.length || 0}
                    </span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-slate-400">Duration</span>
                    <span className={`font-mono font-semibold ${dark ? 'text-slate-300' : 'text-slate-600'}`}>{formatDuration(selectedExec.duration_ms)}</span>
                  </div>
                </div>
              </div>
            </nav>

            {/* Right pane — Failure Summary, Parameter Values, then the
                per-tab body all share this scroll container. */}
            <div className="flex-1 overflow-auto">

          {/* ===== FAILURE SUMMARY ===== */}
          {(() => {
            const failedSteps = selectedExec.step_logs?.filter(s => s.status === 'error') || [];
            if (failedSteps.length === 0) return null;

            // Build downstream impact from connections
            const connections = workflowDef?.connections || [];
            const outgoing = new Map<string, string[]>();
            for (const c of connections) {
              if (!outgoing.has(c.from_step)) outgoing.set(c.from_step, []);
              outgoing.get(c.from_step)!.push(c.to_step);
            }
            // If no connections, use linear chain for downstream
            const allStepIds = selectedExec.step_logs?.map(s => s.step_id) || [];
            if (connections.length === 0) {
              for (let i = 0; i < allStepIds.length - 1; i++) {
                if (!outgoing.has(allStepIds[i])) outgoing.set(allStepIds[i], []);
                outgoing.get(allStepIds[i])!.push(allStepIds[i + 1]);
              }
            }
            const stepNameMap = new Map((selectedExec.step_logs || []).map(s => [s.step_id, s.step_name || s.step_id]));

            // BFS to find all downstream steps from a given step
            const getDownstream = (stepId: string): string[] => {
              const result: string[] = [];
              const visited = new Set<string>();
              const queue = [...(outgoing.get(stepId) || [])];
              while (queue.length > 0) {
                const id = queue.shift()!;
                if (visited.has(id)) continue;
                visited.add(id);
                result.push(id);
                for (const next of (outgoing.get(id) || [])) queue.push(next);
              }
              return result;
            };

            const rootCause = failedSteps[0];

            return (
              <div className={`border-b shrink-0 ${dark ? 'bg-red-500/10 border-red-500/20' : 'bg-red-50/80 border-red-200'}`}>
                <div className="w-full px-6 py-3">
                  {/* Collapsible header */}
                  <button
                    onClick={() => setFailureSummaryOpen(!failureSummaryOpen)}
                    className="w-full flex items-center justify-between group"
                  >
                    <div className="flex items-center gap-2.5">
                      <div className={`w-7 h-7 rounded-lg flex items-center justify-center ${dark ? 'bg-red-500/15 border border-red-500/20' : 'bg-red-100 border border-red-300'}`}>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#dc2626" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                          <line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
                        </svg>
                      </div>
                      <div className="text-left">
                        <span className={`text-sm font-bold ${dark ? 'text-red-400' : 'text-red-800'}`}>Failure Summary</span>
                        <span className={`ml-2 text-xs font-semibold px-2 py-0.5 rounded-full ${dark ? 'text-red-400 bg-red-500/15' : 'text-red-500 bg-red-100'}`}>
                          {failedSteps.length} failed step{failedSteps.length !== 1 ? 's' : ''}
                        </span>
                      </div>
                    </div>
                    <svg
                      width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#dc2626" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                      className={`transition-transform ${failureSummaryOpen ? 'rotate-180' : ''}`}
                    >
                      <polyline points="6 9 12 15 18 9" />
                    </svg>
                  </button>

                  {failureSummaryOpen && (
                    <div className="mt-3 space-y-3">
                      {/* Root Cause hint */}
                      <div className={`flex items-start gap-2 px-3 py-2 rounded-lg border ${dark ? 'bg-red-500/10 border-red-500/15' : 'bg-red-100/60 border-red-200/60'}`}>
                        <span className={`text-xs font-bold shrink-0 mt-px ${dark ? 'text-red-400' : 'text-red-700'}`}>Root Cause:</span>
                        <div className="min-w-0 flex-1">
                          <span className={`text-xs font-semibold inline-flex items-center gap-1.5 ${dark ? 'text-red-300' : 'text-red-800'}`}>
                            <StepTypeIcon type={rootCause.step_type} size={12} />
                            {rootCause.step_name || rootCause.step_id}
                          </span>
                          {/* Inline `— <error_message>` removed — the failed-
                              step card below already renders the error in a
                              styled box. Showing it here too duplicated the
                              string for the user. */}
                        </div>
                        <div className="shrink-0 flex items-center gap-1.5">
                          <button
                            type="button"
                            disabled={resumeLoading || !selectedExec.id || !selectedExec.workflow_id}
                            onClick={async () => {
                              setResumeLoading(true);
                              try {
                                await api.resumeWorkflow(selectedExec.workflow_id, selectedExec.id);
                                toast.success('Resume started — successful steps loaded from snapshot');
                              } catch (e) {
                                toast.error(`Resume failed: ${e instanceof Error ? e.message : String(e)}`);
                              } finally {
                                setResumeLoading(false);
                              }
                            }}
                            className={`px-2.5 py-1 text-xs font-semibold rounded-md transition-colors ${
                              dark
                                ? 'bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-300 disabled:opacity-50'
                                : 'bg-emerald-100 hover:bg-emerald-200 text-emerald-700 disabled:opacity-50'
                            }`}
                            title="Resume this run from the first failed step — successful steps load from snapshot"
                          >
                            {resumeLoading ? 'Resuming…' : 'Resume'}
                          </button>
                          <button
                            type="button"
                            disabled={aiDiagnosisLoading || !rootCause.error_message}
                            onClick={async () => {
                              setAiDiagnosis(null);
                              setAiDiagnosisError(null);
                              setAiDiagnosisLoading(true);
                              try {
                                const headers: Record<string, string> = { 'Content-Type': 'application/json' };
                                const token = localStorage.getItem('fpulse_token') || '';
                                const ws = localStorage.getItem('fpulse_workspace_id') || 'default';
                                if (token) headers['Authorization'] = `Bearer ${token}`;
                                headers['X-Workspace-Id'] = ws;
                                const res = await fetch('/api/ai/diagnose-error', {
                                  method: 'POST',
                                  headers,
                                  body: JSON.stringify({
                                    error_message: rootCause.error_message || '',
                                    node_type: rootCause.step_type || '',
                                    node_params: {},
                                    upstream_schema: [],
                                  }),
                                });
                                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                                const data = await res.json();
                                setAiDiagnosis(data);
                              } catch (e) {
                                setAiDiagnosisError(e instanceof Error ? e.message : 'Diagnose failed');
                              } finally {
                                setAiDiagnosisLoading(false);
                              }
                            }}
                            className={`px-2.5 py-1 text-xs font-semibold rounded-md transition-colors ${
                              dark
                                ? 'bg-indigo-500/20 hover:bg-indigo-500/30 text-indigo-300 disabled:opacity-50'
                                : 'bg-indigo-100 hover:bg-indigo-200 text-indigo-700 disabled:opacity-50'
                            }`}
                            title="Ask AI to diagnose the root cause of this failure"
                          >
                            {aiDiagnosisLoading ? 'Diagnosing…' : aiDiagnosis ? 'Re-diagnose' : 'AI Diagnose'}
                          </button>
                        </div>
                      </div>
                      {aiDiagnosisError && (
                        <div className={`px-3 py-2 rounded-lg border text-xs ${dark ? 'bg-red-500/10 border-red-500/20 text-red-300' : 'bg-red-50 border-red-200 text-red-700'}`}>
                          AI diagnose failed: {aiDiagnosisError}
                        </div>
                      )}
                      {aiDiagnosisLoading && !aiDiagnosis && (
                        <div className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-xs ${dark ? 'bg-indigo-500/10 border-indigo-500/20 text-indigo-300' : 'bg-indigo-50 border-indigo-200 text-indigo-700'}`}>
                          <span className="w-3 h-3 border-2 border-indigo-300 border-t-indigo-600 rounded-full animate-spin shrink-0" />
                          Diagnosing failure with AI…
                        </div>
                      )}
                      {aiDiagnosis && (
                        <div className={`px-3 py-2.5 rounded-lg border ${dark ? 'bg-indigo-500/10 border-indigo-500/20' : 'bg-indigo-50 border-indigo-200'}`}>
                          <div className="flex items-center gap-2 mb-1.5">
                            <span className={`text-xs font-bold uppercase tracking-wider ${dark ? 'text-indigo-300' : 'text-indigo-700'}`}>
                              {aiDiagnosis.ai_powered ? 'AI Diagnosis' : 'Rule-based diagnosis'}
                            </span>
                            <span className={`text-[9px] font-semibold px-1.5 py-0.5 rounded ${
                              aiDiagnosis.severity === 'error'
                                ? (dark ? 'text-red-300 bg-red-500/15' : 'text-red-700 bg-red-100')
                                : (dark ? 'text-amber-300 bg-amber-500/15' : 'text-amber-700 bg-amber-100')
                            }`}>
                              {aiDiagnosis.severity}
                            </span>
                            {!aiDiagnosis.ai_powered && (
                              <span className={`text-[9px] ${dark ? 'text-slate-500' : 'text-slate-500'}`}>
                                (LLM unavailable — used deterministic fallback)
                              </span>
                            )}
                          </div>
                          <div className={`text-xs font-semibold mb-1 ${dark ? 'text-slate-100' : 'text-slate-800'}`}>
                            {aiDiagnosis.diagnosis}
                          </div>
                          <div className={`text-xs ${dark ? 'text-slate-300' : 'text-slate-700'}`}>
                            <span className="font-semibold">Suggestion: </span>
                            {aiDiagnosis.suggestion}
                          </div>
                        </div>
                      )}

                      {/* Failed step cards */}
                      <div className="grid gap-2">
                        {failedSteps.map((step, idx) => {
                          const downstream = getDownstream(step.step_id);
                          const skippedDownstream = downstream.filter(id => {
                            const s = selectedExec.step_logs?.find(sl => sl.step_id === id);
                            return s && (s.status === 'skipped' || s.status === 'pending');
                          });

                          return (
                            <div
                              key={step.step_id}
                              className={`rounded-lg border p-3 ${dark ? 'bg-white/[0.04] border-red-500/20' : 'bg-white border-red-200'}`}
                            >
                              <div className="flex items-start justify-between gap-3">
                                <div className="flex items-center gap-2 min-w-0">
                                  <div className={`w-7 h-7 rounded-lg flex items-center justify-center shrink-0 ${dark ? 'bg-red-500/10 border border-red-500/20 text-red-300' : 'bg-red-50 border border-red-200 text-red-600'}`}>
                                    <StepTypeIcon type={step.step_type} size={14} />
                                  </div>
                                  <div className="min-w-0">
                                    <div className="flex items-center gap-2">
                                      <span className={`text-xs font-bold truncate ${dark ? 'text-slate-100' : 'text-slate-800'}`}>
                                        {step.step_name || step.step_id}
                                      </span>
                                      {idx === 0 && (
                                        <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded uppercase shrink-0 ${dark ? 'text-red-400 bg-red-500/15' : 'text-red-600 bg-red-100'}`}>
                                          Root Cause
                                        </span>
                                      )}
                                    </div>
                                    <span className="text-xs font-mono text-slate-400">{step.step_type}</span>
                                  </div>
                                </div>
                                <div className="flex items-center gap-3 shrink-0 text-xs text-slate-500">
                                  {step.rows_processed > 0 && (
                                    <span title="Rows processed before failure">
                                      {step.rows_processed.toLocaleString()} rows
                                    </span>
                                  )}
                                  <span className="font-mono">{formatDuration(step.duration_ms)}</span>
                                </div>
                              </div>

                              {step.error_message && (
                                <div className={`mt-2 px-2.5 py-1.5 rounded border text-xs font-mono break-all leading-relaxed ${dark ? 'bg-red-500/10 border-red-500/15 text-red-400' : 'bg-red-50 border-red-100 text-red-700'}`}>
                                  {/* PR 7 — coloured reason-code badge.
                                      Falls back to plain text when the
                                      backend didn't classify (legacy or
                                      unknown error type). */}
                                  {step.error_type && (
                                    <ErrorTypeBadge type={step.error_type} />
                                  )}
                                  {/* E1.1 (2026-06-08) — broader retry-
                                      policy classification chip. Same
                                      row as ErrorTypeBadge but with a
                                      different colour palette so the
                                      two taxonomies don't visually
                                      collide. */}
                                  {step.failure_class && (
                                    <FailureClassBadge cls={step.failure_class} />
                                  )}
                                  {step.error_message}
                                </div>
                              )}

                              {skippedDownstream.length > 0 && (
                                <div className="mt-2 flex items-center gap-1.5 flex-wrap">
                                  <span className="text-xs font-semibold text-slate-500">Impact:</span>
                                  <span className="text-xs text-amber-600 bg-amber-50 px-1.5 py-0.5 rounded border border-amber-200">
                                    {skippedDownstream.length} downstream step{skippedDownstream.length !== 1 ? 's' : ''} skipped
                                  </span>
                                  {skippedDownstream.slice(0, 5).map(id => (
                                    <span key={id} className="text-xs text-slate-400 font-mono">
                                      {stepNameMap.get(id) || id}
                                    </span>
                                  ))}
                                  {skippedDownstream.length > 5 && (
                                    <span className="text-xs text-slate-400">+{skippedDownstream.length - 5} more</span>
                                  )}
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}
                </div>
              </div>
            );
          })()}

          {/* Parameter values for this run — rendered above the Tab
              Bar when the execution captured them. Audit trail: the user can
              see exactly what was passed in (vs the pipeline's declared defaults). */}
          {selectedExec.metadata?.parameter_values && Object.keys(selectedExec.metadata.parameter_values).length > 0 && (
            <div className={`border-b shrink-0 ${dark ? 'bg-emerald-500/5 border-emerald-500/15' : 'bg-emerald-50/50 border-emerald-100'}`}>
              <div className="w-full px-6 py-2 flex items-center gap-3 flex-wrap">
                <span className={`text-xs font-bold uppercase tracking-wider ${dark ? 'text-emerald-300' : 'text-emerald-700'}`}>
                  Parameter values
                </span>
                {Object.entries(selectedExec.metadata.parameter_values).map(([k, v]) => (
                  <span
                    key={k}
                    className={`text-xs font-mono inline-flex items-center gap-1 px-2 py-0.5 rounded ring-1 ${
                      dark
                        ? 'bg-emerald-500/10 text-emerald-200 ring-emerald-500/20'
                        : 'bg-white text-emerald-800 ring-emerald-200'
                    }`}
                    title={`${k}: ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`}
                  >
                    <span className={dark ? 'text-emerald-400' : 'text-emerald-600'}>{k}</span>
                    <span className={dark ? 'text-emerald-500' : 'text-emerald-400'}>=</span>
                    <span className="truncate max-w-[180px]">
                      {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                    </span>
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* (Sidebar + content pane already opened above the Failure
              Summary — only the per-tab content lives below.) */}
            {/* ===== LINEAGE VIEW — Horizontal step-by-step graph ===== */}
            {detailTab === 'lineage' && (
              <div className="px-6 py-6 min-h-full">
                {detailLoading ? (
                  <div className="flex items-center justify-center py-20">
                    <div className="w-8 h-8 border-2 border-pipe-300 border-t-transparent rounded-full animate-spin" />
                  </div>
                ) : !selectedExec.step_logs?.length ? (
                  <div className="text-center py-20">
                    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#cbd5e1" strokeWidth="1.5" className="mx-auto mb-4">
                      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
                    </svg>
                    <h2 className="text-lg font-bold text-slate-700 mb-2">No lineage data</h2>
                    <p className="text-sm text-slate-500">Step-level execution data is not available for this run.</p>
                  </div>
                ) : (() => {
                  // Build graph layout from workflow edges
                  const stepMap = new Map(selectedExec.step_logs!.map(s => [s.step_id, s]));
                  const connections = workflowDef?.connections || [];

                  // Build adjacency: from -> [{to, condition}]
                  const outgoing = new Map<string, Array<{ to: string; condition: string }>>();
                  const incoming = new Map<string, string[]>();
                  for (const c of connections) {
                    if (!outgoing.has(c.from_step)) outgoing.set(c.from_step, []);
                    outgoing.get(c.from_step)!.push({ to: c.to_step, condition: c.condition || 'completion' });
                    if (!incoming.has(c.to_step)) incoming.set(c.to_step, []);
                    incoming.get(c.to_step)!.push(c.from_step);
                  }

                  // If no connections data, fallback to linear chain
                  const allStepIds = selectedExec.step_logs!.map(s => s.step_id);
                  if (connections.length === 0) {
                    for (let i = 0; i < allStepIds.length - 1; i++) {
                      const from = allStepIds[i], to = allStepIds[i + 1];
                      if (!outgoing.has(from)) outgoing.set(from, []);
                      outgoing.get(from)!.push({ to, condition: 'completion' });
                      if (!incoming.has(to)) incoming.set(to, []);
                      incoming.get(to)!.push(from);
                    }
                  }

                  // ── Layered DAG layout ────────────────────────────────
                  // Old layout was a plain BFS that assigned a node its FIRST
                  // depth-from-root and used midpoint bezier controls, so any
                  // edge skipping a column (e.g. a union pulling from a node
                  // three columns back) drew a long curve swooping through the
                  // middle of the graph, and mis-aligned rows crossed. Now:
                  //   1. longest-path columns (a node sits one past its DEEPEST
                  //      parent) so edges always flow left→right, and
                  //   2. barycenter row ordering so connected nodes line up
                  //      vertically — which removes the swoops and crossings.

                  // 1a. Topological order (Kahn); tolerant of cycles — any
                  //     leftover nodes are appended so nothing is dropped.
                  const indegWork = new Map<string, number>(
                    allStepIds.map(id => [id, (incoming.get(id) || []).length]),
                  );
                  const ready = allStepIds.filter(id => (indegWork.get(id) || 0) === 0);
                  const topo: string[] = [];
                  const inTopo = new Set<string>();
                  while (ready.length) {
                    const id = ready.shift()!;
                    if (inTopo.has(id)) continue;
                    topo.push(id); inTopo.add(id);
                    for (const e of (outgoing.get(id) || [])) {
                      indegWork.set(e.to, (indegWork.get(e.to) || 0) - 1);
                      if ((indegWork.get(e.to) || 0) <= 0 && !inTopo.has(e.to)) ready.push(e.to);
                    }
                  }
                  for (const id of allStepIds) if (!inTopo.has(id)) { topo.push(id); inTopo.add(id); }

                  // 1b. Longest-path column = max(parent columns) + 1. Parents
                  //     precede children in topo order, so one pass is exact.
                  const col = new Map<string, number>(allStepIds.map(id => [id, 0]));
                  for (const id of topo) {
                    const base = col.get(id) || 0;
                    for (const e of (outgoing.get(id) || [])) {
                      col.set(e.to, Math.max(col.get(e.to) || 0, base + 1));
                    }
                  }

                  // 1c. Insert DUMMY waypoint nodes on every edge that skips a
                  //     column (Sugiyama). Each long edge becomes a chain
                  //     u → d → … → v with one dummy per intermediate column, so
                  //     the edge gets its OWN reserved row-slot ("lane") instead
                  //     of cutting diagonally across the cards in between. The
                  //     routing graph (routePred/routeSucc over real+dummy) drives
                  //     ordering; edgeChain remembers the path for rendering.
                  const isDummy = (id: string) => id.startsWith('__d:');
                  const routePred = new Map<string, string[]>();
                  const routeSucc = new Map<string, string[]>();
                  const link = (a: string, b: string) => {
                    (routeSucc.get(a) ?? routeSucc.set(a, []).get(a)!).push(b);
                    (routePred.get(b) ?? routePred.set(b, []).get(b)!).push(a);
                  };
                  const edgeChain = new Map<string, string[]>();
                  const dummyCol = new Map<string, number>();
                  for (const u of allStepIds) {
                    for (const e of (outgoing.get(u) || [])) {
                      const v = e.to;
                      const cu = col.get(u) || 0;
                      const cv = col.get(v) || 0;
                      if (cv - cu <= 1) { link(u, v); edgeChain.set(`${u}→${v}`, [u, v]); continue; }
                      const chain = [u];
                      let prev = u;
                      for (let c = cu + 1; c < cv; c++) {
                        const d = `__d:${u}>${v}:${c}`;
                        dummyCol.set(d, c);
                        link(prev, d);
                        prev = d;
                        chain.push(d);
                      }
                      link(prev, v);
                      chain.push(v);
                      edgeChain.set(`${u}→${v}`, chain);
                    }
                  }

                  // 1d. Group into columns (real nodes + dummy lanes).
                  const maxCol = Math.max(0, ...Array.from(col.values()));
                  const columns: string[][] = Array.from({ length: maxCol + 1 }, () => []);
                  for (const id of topo) columns[col.get(id) || 0].push(id);
                  for (const [d, c] of dummyCol) columns[c].push(d);

                  // 1e. Barycenter row ordering over the ROUTING graph — line each
                  //     node (and dummy) up with the average row of its neighbours
                  //     over a few alternating sweeps. This straightens edges and
                  //     parks each skip-edge's lane clear of the real cards.
                  const orderIdx = new Map<string, number>();
                  columns.forEach(cn => cn.forEach((id, i) => orderIdx.set(id, i)));
                  for (let sweep = 0; sweep < 6; sweep++) {
                    const l2r = sweep % 2 === 0;
                    const colOrder = columns.map((_, i) => i);
                    if (!l2r) colOrder.reverse();
                    for (const c of colOrder) {
                      const bary = (id: string) => {
                        const ns = (l2r ? (routePred.get(id) || []) : (routeSucc.get(id) || []))
                          .map(n => orderIdx.get(n)).filter((v): v is number => v !== undefined);
                        return ns.length ? ns.reduce((a, b) => a + b, 0) / ns.length : (orderIdx.get(id) || 0);
                      };
                      columns[c] = [...columns[c]].sort((a, b) => bary(a) - bary(b));
                      columns[c].forEach((id, i) => orderIdx.set(id, i));
                    }
                  }

                  // Layout constants — sized to surface label, type,
                  // status, row count, and duration without wasted space.
                  // Earlier 260×120 cards left ~30% of the inner area
                  // empty between the type line and the metrics line.
                  const NODE_W = 200;
                  const NODE_H = 80;
                  const COL_GAP = 60;
                  const ROW_GAP = 24;
                  const PADDING = 32;

                  // Calculate positions. `positions` holds REAL card top-lefts;
                  // `waypoint` holds the routing centre for EVERY node incl.
                  // dummies (so skip-edges can be drawn through the lane centres).
                  const positions = new Map<string, { x: number; y: number }>();
                  const waypoint = new Map<string, { x: number; y: number }>();
                  const maxRows = Math.max(...columns.map(c => c.length));
                  const totalH = maxRows * NODE_H + (maxRows - 1) * ROW_GAP;

                  for (let c = 0; c < columns.length; c++) {
                    const rows = columns[c];
                    const colHeight = rows.length * NODE_H + (rows.length - 1) * ROW_GAP;
                    const offsetY = (totalH - colHeight) / 2;
                    for (let r = 0; r < rows.length; r++) {
                      const id = rows[r];
                      const x = PADDING + c * (NODE_W + COL_GAP);
                      const y = PADDING + offsetY + r * (NODE_H + ROW_GAP);
                      if (!isDummy(id)) positions.set(id, { x, y });
                      waypoint.set(id, { x: x + NODE_W / 2, y: y + NODE_H / 2 });
                    }
                  }

                  const svgW = PADDING * 2 + columns.length * NODE_W + (columns.length - 1) * COL_GAP;
                  const svgH = PADDING * 2 + totalH;

                  const CONDITION_COLORS: Record<string, string> = {
                    completion: '#6366f1', success: '#22c55e', failure: '#ef4444',
                  };

                  return (
                    <div>
                      {/* Summary bar */}
                      <div className={`flex items-center gap-4 mb-5 p-3 rounded-lg border shadow-sm ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
                        <div className="flex items-center gap-4 flex-1">
                          <div className="flex items-center gap-1.5">
                            <div className="w-2.5 h-2.5 rounded-full bg-emerald-400" />
                            <span className="text-xs font-semibold text-slate-600">
                              {selectedExec.step_logs!.filter(s => s.status === 'success').length} passed
                            </span>
                          </div>
                          {selectedExec.step_logs!.some(s => s.status === 'error') && (
                            <div className="flex items-center gap-1.5">
                              <div className="w-2.5 h-2.5 rounded-full bg-red-400" />
                              <span className="text-xs font-semibold text-slate-600">
                                {selectedExec.step_logs!.filter(s => s.status === 'error').length} failed
                              </span>
                            </div>
                          )}
                          {selectedExec.step_logs!.some(s => s.status === 'running') && (
                            <div className="flex items-center gap-1.5">
                              <div className="w-2.5 h-2.5 rounded-full bg-amber-400 animate-pulse" />
                              <span className="text-xs font-semibold text-slate-600">
                                {selectedExec.step_logs!.filter(s => s.status === 'running').length} running
                              </span>
                            </div>
                          )}
                          {selectedExec.step_logs!.some(s => s.status === 'skipped') && (
                            <div className="flex items-center gap-1.5" title="Skipped — an upstream node was deactivated, or a branch routed rows elsewhere.">
                              <div className="w-2.5 h-2.5 rounded-full bg-slate-300" />
                              <span className="text-xs font-semibold text-slate-600">
                                {selectedExec.step_logs!.filter(s => s.status === 'skipped').length} skipped
                              </span>
                            </div>
                          )}
                        </div>
                        <span className="text-xs text-slate-400">
                          {selectedExec.step_logs!.length} steps · {formatDuration(selectedExec.duration_ms)} total
                        </span>
                      </div>

                      {/* Horizontal graph canvas */}
                      <div className={`rounded-lg border overflow-auto ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
                        <svg width={svgW} height={svgH} className="min-w-full">
                          {/* Grid dots pattern */}
                          <defs>
                            <pattern id="dots" x="0" y="0" width="20" height="20" patternUnits="userSpaceOnUse">
                              <circle cx="1" cy="1" r="0.8" fill={dark ? 'rgba(255,255,255,0.06)' : '#e2e8f0'} />
                            </pattern>
                          </defs>
                          <rect width="100%" height="100%" fill="url(#dots)" />

                          {/* Draw edges first (behind nodes) */}
                          {allStepIds.flatMap(fromId => {
                            const edges = outgoing.get(fromId) || [];
                            return edges.map((edge, ei) => {
                              const fromPos = positions.get(fromId);
                              const toPos = positions.get(edge.to);
                              if (!fromPos || !toPos) return null;

                              const x1 = fromPos.x + NODE_W;
                              const y1 = fromPos.y + NODE_H / 2;
                              const x2 = toPos.x;
                              const y2 = toPos.y + NODE_H / 2;
                              const mx = x1 + (x2 - x1) / 2;
                              const color = CONDITION_COLORS[edge.condition] || '#6366f1';

                              // Build the edge path. Direct edges (adjacent
                              // columns) → one horizontal-tangent bezier. Skip
                              // edges → a smooth multi-segment spline routed
                              // through their reserved dummy-lane centres, so they
                              // flow cleanly past the cards instead of cutting
                              // diagonally across them.
                              const chain = edgeChain.get(`${fromId}→${edge.to}`) || [fromId, edge.to];
                              const pts: Array<{ x: number; y: number }> = chain.length > 2
                                ? [{ x: x1, y: y1 },
                                   ...chain.slice(1, -1)
                                     .map(d => waypoint.get(d))
                                     .filter((w): w is { x: number; y: number } => !!w),
                                   { x: x2, y: y2 }]
                                : [{ x: x1, y: y1 }, { x: x2, y: y2 }];
                              let pathD = `M ${pts[0].x} ${pts[0].y}`;
                              if (pts.length === 2) {
                                const cdx = Math.max(24, Math.min(64, (x2 - x1) * 0.4));
                                pathD += ` C ${x1 + cdx} ${y1}, ${x2 - cdx} ${y2}, ${x2} ${y2}`;
                              } else {
                                for (let i = 1; i < pts.length; i++) {
                                  const a = pts[i - 1], b = pts[i];
                                  const cmx = (a.x + b.x) / 2;
                                  pathD += ` C ${cmx} ${a.y}, ${cmx} ${b.y}, ${b.x} ${b.y}`;
                                }
                              }

                              // Condition label sits BELOW the edge so it
                              // never collides with the row count label
                              // that lives above the edge near the source.
                              const labelX = mx;
                              const labelY = (y1 + y2) / 2 + 14;

                              const edgeMeta = edgeCounts.get(`${fromId}→${edge.to}`);
                              // Rows flowing along this edge. Prefer the captured
                              // per-edge count; when it's missing/0 (older runs
                              // didn't record per-edge counts) fall back to the
                              // SOURCE node's output rows so the label shows the
                              // real volume instead of a misleading "0 items".
                              const flowRows = edgeMeta && edgeMeta.row_count > 0
                                ? edgeMeta.row_count
                                : (stepMap.get(fromId)?.rows_processed || 0);
                              const labelStatus = edgeMeta?.from_status ?? stepMap.get(fromId)?.status;
                              const countLabel = flowRows > 0
                                ? flowRows >= 1_000_000
                                  ? `${(flowRows / 1_000_000).toFixed(1)}M items`
                                  : flowRows >= 1_000
                                  ? `${(flowRows / 1_000).toFixed(1)}K items`
                                  : `${flowRows.toLocaleString()} items`
                                : null;
                              return (
                                <g key={`edge-${fromId}-${edge.to}-${ei}`}>
                                  {/* Edge path — single bezier (direct) or a
                                      lane-routed spline (skip-edges). */}
                                  <path
                                    d={pathD}
                                    fill="none"
                                    stroke={color}
                                    strokeWidth="2"
                                    strokeDasharray="6 3"
                                    opacity="0.7"
                                  />
                                  {/* Arrow head */}
                                  <polygon
                                    points={`${x2},${y2} ${x2 - 8},${y2 - 4} ${x2 - 8},${y2 + 4}`}
                                    fill={color}
                                    opacity="0.8"
                                  />
                                  {/* Per-edge row count label — source-side, above the line */}
                                  {countLabel && (
                                    <g>
                                      <rect
                                        x={x1 + 8}
                                        y={y1 - 20}
                                        width={Math.max(48, countLabel.length * 6)}
                                        height="14"
                                        rx="7"
                                        fill={dark ? '#0f172a' : '#ffffff'}
                                        stroke={labelStatus === 'error' ? '#ef4444' : (dark ? '#334155' : '#cbd5e1')}
                                        strokeWidth="1"
                                      />
                                      <text
                                        x={x1 + 8 + Math.max(48, countLabel.length * 6) / 2}
                                        y={y1 - 10}
                                        textAnchor="middle"
                                        fill={labelStatus === 'error' ? '#ef4444' : (dark ? '#cbd5e1' : '#475569')}
                                        fontSize="10"
                                        fontWeight="600"
                                        fontFamily="system-ui"
                                      >
                                        {countLabel}
                                      </text>
                                    </g>
                                  )}
                                  {/* Condition label — only render when it's
                                      a non-default branch; suppress for the
                                      common 'on_completion' edge to cut
                                      visual noise. Always sits below the
                                      edge line so the row-count badge above
                                      has clean space. */}
                                  {edge.condition && !['completion', 'on_completion', 'success', 'on_success'].includes(edge.condition.toLowerCase()) && (
                                    <>
                                      <rect x={labelX - 34} y={labelY - 7} width="68" height="14" rx="3" fill={dark ? '#1e293b' : 'white'} stroke={color} strokeWidth="1" opacity="0.9" />
                                      <text x={labelX} y={labelY + 3} textAnchor="middle" fill={color} fontSize="8" fontWeight="700" fontFamily="system-ui" style={{ textTransform: 'uppercase' as const, letterSpacing: '0.05em' }}>
                                        ON {edge.condition.toUpperCase()}
                                      </text>
                                    </>
                                  )}
                                </g>
                              );
                            });
                          })}

                          {/* Draw nodes */}
                          {allStepIds.map(stepId => {
                            const step = stepMap.get(stepId);
                            if (!step) return null;
                            const pos = positions.get(stepId);
                            if (!pos) return null;

                            const style = LINEAGE_STATUS_STYLES[step.status] || LINEAGE_STATUS_STYLES.pending;
                            const statusColor = step.status === 'success' ? '#10b981' :
                              step.status === 'error' ? '#ef4444' :
                              step.status === 'running' ? '#f59e0b' : '#94a3b8';
                            const bgColor = dark
                              ? (step.status === 'success' ? '#064e3b' : step.status === 'error' ? '#450a0a' : step.status === 'running' ? '#451a03' : '#1e293b')
                              : (step.status === 'success' ? '#ecfdf5' : step.status === 'error' ? '#fef2f2' : step.status === 'running' ? '#fffbeb' : '#f8fafc');
                            const borderColor = dark
                              ? (step.status === 'success' ? '#059669' : step.status === 'error' ? '#dc2626' : step.status === 'running' ? '#d97706' : 'rgba(255,255,255,0.1)')
                              : (step.status === 'success' ? '#6ee7b7' : step.status === 'error' ? '#fca5a5' : step.status === 'running' ? '#fcd34d' : '#cbd5e1');

                            return (
                              <g
                                key={stepId}
                                onClick={() => setIOStep({ stepId, label: step.step_name || step.step_id })}
                                style={{ cursor: 'pointer' }}
                                role="button"
                                aria-label={`Inspect input and output for ${step.step_name || stepId}`}
                              >
                                {/* Native hover tooltip — full label + type, since
                                    the card title is truncated to fit the card width. */}
                                <title>{`${step.step_name || step.step_id} — ${step.step_type}`}</title>
                                {/* Card shadow */}
                                <rect x={pos.x + 2} y={pos.y + 2} width={NODE_W} height={NODE_H} rx="10" fill="#00000008" />
                                {/* Card bg */}
                                <rect x={pos.x} y={pos.y} width={NODE_W} height={NODE_H} rx="10" fill={bgColor} stroke={borderColor} strokeWidth="1.5" />
                                {/* Left status bar */}
                                <rect x={pos.x} y={pos.y + 6} width="3" height={NODE_H - 12} rx="1.5" fill={statusColor} />

                                {/* Icon circle — uses foreignObject so the
                                    line-art SVG icon component renders the
                                    same in the lineage diagram as it does
                                    elsewhere in the app, regardless of OS
                                    emoji-font support. */}
                                <rect x={pos.x + 10} y={pos.y + 10} width="28" height="28" rx="6" fill={dark ? '#1e293b' : 'white'} stroke={borderColor} strokeWidth="1" />
                                <foreignObject x={pos.x + 10} y={pos.y + 10} width="28" height="28">
                                  <div
                                    style={{ width: 28, height: 28, display: 'flex', alignItems: 'center', justifyContent: 'center', color: statusColor }}
                                  >
                                    <StepTypeIcon type={step.step_type} size={14} />
                                  </div>
                                </foreignObject>

                                {/* Step name — truncated with an ellipsis so it
                                    stops cleanly before the status badge instead
                                    of running underneath it (full name in the
                                    <title> tooltip above). */}
                                <text x={pos.x + 44} y={pos.y + 23} fill={dark ? '#e2e8f0' : '#111827'} fontSize="12" fontWeight="700" fontFamily="system-ui">
                                  {(() => {
                                    const full = step.step_name || step.step_id;
                                    return full.length > 14 ? full.slice(0, 14).trimEnd() + '…' : full;
                                  })()}
                                </text>
                                {/* Step type */}
                                <text x={pos.x + 44} y={pos.y + 36} fill="#64748b" fontSize="10" fontFamily="monospace">
                                  {step.step_type}
                                </text>

                                {/* Status badge */}
                                <rect x={pos.x + NODE_W - 60} y={pos.y + 8} width="54" height="15" rx="4" fill={bgColor} stroke={borderColor} strokeWidth="1" />
                                <text x={pos.x + NODE_W - 33} y={pos.y + 18} textAnchor="middle" fill={statusColor} fontSize="9" fontWeight="800" fontFamily="system-ui">
                                  {style.icon} {step.status.toUpperCase()}
                                </text>

                                {/* Metrics — single inline row with both rows + duration */}
                                {(step.rows_processed > 0 || step.duration_ms > 0) && (
                                  <text x={pos.x + 12} y={pos.y + 58} fill="#475569" fontSize="11" fontFamily="system-ui">
                                    {step.rows_processed > 0 && (
                                      <tspan fontWeight="600">◈ {step.rows_processed.toLocaleString()} rows</tspan>
                                    )}
                                    {step.rows_processed > 0 && step.duration_ms > 0 && (
                                      <tspan dx="6" fill="#cbd5e1">·</tspan>
                                    )}
                                    {step.duration_ms > 0 && (
                                      <tspan dx="6" fill="#64748b" fontFamily="monospace">⏱ {formatDuration(step.duration_ms)}</tspan>
                                    )}
                                  </text>
                                )}

                                {/* Progress bar */}
                                {step.status !== 'pending' && step.status !== 'skipped' && (
                                  <>
                                    <rect x={pos.x + 10} y={pos.y + NODE_H - 8} width={NODE_W - 20} height="3" rx="1.5" fill={dark ? 'rgba(255,255,255,0.08)' : '#e2e8f0'} />
                                    <rect
                                      x={pos.x + 10} y={pos.y + NODE_H - 8}
                                      width={(NODE_W - 20) * (step.status === 'success' ? 1 : step.status === 'running' ? 0.6 : 1)}
                                      height="3" rx="1.5" fill={statusColor}
                                    />
                                  </>
                                )}

                                {/* Connection dots (ports) */}
                                {/* Left port (input) */}
                                {(incoming.has(stepId) || connections.length === 0 && allStepIds.indexOf(stepId) > 0) && (
                                  <circle cx={pos.x} cy={pos.y + NODE_H / 2} r="5" fill={dark ? '#1e293b' : 'white'} stroke={statusColor} strokeWidth="2" />
                                )}
                                {/* Right port (output) */}
                                {(outgoing.has(stepId) || connections.length === 0 && allStepIds.indexOf(stepId) < allStepIds.length - 1) && (
                                  <circle cx={pos.x + NODE_W} cy={pos.y + NODE_H / 2} r="5" fill={statusColor} stroke={dark ? '#1e293b' : 'white'} strokeWidth="2" />
                                )}
                              </g>
                            );
                          })}
                        </svg>
                      </div>
                    </div>
                  );
                })()}
              </div>
            )}

            {/* ===== STEPS VIEW ===== */}
            {detailTab === 'steps' && (
              <div className="w-full px-6 py-8">
                {detailLoading ? (
                  <div className="flex items-center justify-center py-20">
                    <div className="w-8 h-8 border-2 border-pipe-300 border-t-transparent rounded-full animate-spin" />
                  </div>
                ) : !selectedExec.step_logs?.length ? (
                  <div className="text-center py-20 text-slate-400 text-sm">No step logs available for this execution.</div>
                ) : (() => {
                  // Pull workflow IR snapshot once so we can read each step's
                  // configured params and figure out which upstream steps fed
                  // into a given step. Without the snapshot (legacy runs),
                  // the panel falls back to "no input data captured".
                  const snapshot = (selectedExec.workflow_snapshot || {}) as any;
                  const snapSteps: any[] = Array.isArray(snapshot.steps) ? snapshot.steps : [];
                  const snapConns: any[] = Array.isArray(snapshot.connections) ? snapshot.connections : [];
                  const stepById = new Map<string, any>(snapSteps.map((s) => [s.id, s]));
                  const stepLogById = new Map<string, any>(
                    (selectedExec.step_logs || []).map((s: any) => [s.step_id, s]),
                  );
                  const upstreamOf = (sid: string) =>
                    snapConns.filter((c) => c.to_step === sid).map((c) => c.from_step);

                  // N5 — Build GanttStep[] from the step_logs.
                  // Filter out rows without a start_time (rare;
                  // happens on never-started skipped steps).
                  const ganttSteps: GanttStep[] = (selectedExec.step_logs || [])
                    .filter((s) => !!s.started_at)
                    .map((s) => ({
                      step_id: s.step_id,
                      step_name: s.step_name,
                      status: s.status,
                      start_time: s.started_at!,
                      end_time: s.completed_at,
                      row_count: s.rows_processed,
                    }));

                  return (
                  <>
                    {/* N5 — Gantt overview above the detailed table.
                        Lets the user spot slow steps without scanning
                        the duration column row-by-row. */}
                    {ganttSteps.length > 1 && (
                      <div className="rounded-lg border border-slate-200 shadow-sm bg-white p-4 mb-4">
                        <div className="text-xs font-bold text-slate-700 uppercase tracking-wider mb-2">
                          Timing overview
                        </div>
                        <StepGantt
                          steps={ganttSteps}
                          width={Math.min(1200, window.innerWidth - 96)}
                          rowHeight={20}
                          onSelect={(stepId) => setExpandedSteps((prev) => ({ ...prev, [stepId]: !prev[stepId] }))}
                        />
                      </div>
                    )}
                  <div className="rounded-lg border border-slate-200 shadow-sm bg-white overflow-hidden">
                    <table className="w-full text-sm border-collapse">
                      <thead>
                        {/* Canonical navy/amber header (Apr 18) */}
                        <tr className="bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900 border-b-2 border-amber-400/40">
                          <th className="px-2 py-3 w-8" aria-label="Expand"></th>
                          <th className="px-5 py-3 text-left text-xs font-bold text-amber-300 uppercase tracking-wider">Status</th>
                          <th className="px-4 py-3 text-left text-xs font-bold text-amber-300 uppercase tracking-wider">Step</th>
                          <th className="px-4 py-3 text-left text-xs font-bold text-amber-300 uppercase tracking-wider">Type</th>
                          <th className="px-4 py-3 text-right text-xs font-bold text-amber-300 uppercase tracking-wider">Rows</th>
                          <th className="px-4 py-3 text-right text-xs font-bold text-amber-300 uppercase tracking-wider">Duration</th>
                          <th className="px-4 py-3 text-left text-xs font-bold text-amber-300 uppercase tracking-wider">Started</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-100">
                        {selectedExec.step_logs.map((step) => {
                          const info = STEP_STATUS_ICON[step.status] || STEP_STATUS_ICON.pending;
                          const expanded = !!expandedSteps[step.step_id];
                          const irStep = stepById.get(step.step_id);
                          const upstreams = upstreamOf(step.step_id);
                          const upstreamLogs = upstreams
                            .map((id) => stepLogById.get(id))
                            .filter(Boolean);
                          return (
                            <Fragment key={step.step_id}>
                              <tr
                                onClick={() => toggleStepExpand(step.step_id)}
                                className="hover:bg-slate-50/60 transition-colors cursor-pointer"
                                title="Click to inspect inputs / outputs"
                              >
                                <td className="px-2 py-3 text-center">
                                  <svg
                                    width="12" height="12" viewBox="0 0 24 24" fill="none"
                                    stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
                                    className={`text-slate-400 transition-transform inline-block ${expanded ? 'rotate-90' : ''}`}
                                  >
                                    <polyline points="9 18 15 12 9 6" />
                                  </svg>
                                </td>
                                <td className="px-5 py-3">
                                  <div className={`w-7 h-7 rounded-full border flex items-center justify-center text-xs font-bold ${info.color}`}>
                                    {info.icon}
                                  </div>
                                </td>
                                <td className="px-4 py-3">
                                  <div className="flex items-center gap-2">
                                    <StepTypeIcon type={step.step_type} size={16} className="text-slate-500" />
                                    <div>
                                      <div className="text-xs font-semibold text-slate-700">{step.step_name || step.step_id}</div>
                                      {step.error_message && (
                                        <div className="text-xs text-red-500 mt-0.5 truncate max-w-[250px]">{step.error_message}</div>
                                      )}
                                    </div>
                                  </div>
                                </td>
                                <td className="px-4 py-3">
                                  <span className="text-xs font-mono bg-slate-100 text-slate-500 px-2 py-0.5 rounded">{step.step_type}</span>
                                </td>
                                <td className="px-4 py-3 text-right text-xs text-slate-600 font-semibold">
                                  {step.rows_processed > 0 ? step.rows_processed.toLocaleString() : '—'}
                                </td>
                                <td className="px-4 py-3 text-right text-xs text-slate-500 font-mono">
                                  {formatDuration(step.duration_ms)}
                                </td>
                                <td className="px-4 py-3 text-xs text-slate-400">
                                  {formatTime(step.started_at)}
                                </td>
                              </tr>
                              {expanded && (
                                <tr key={`${step.step_id}-debug`} className="bg-slate-50">
                                  <td colSpan={7} className="px-6 py-4">
                                    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                                      {/* ── Configuration / params (the step's input config) ── */}
                                      <div className="rounded-lg border border-slate-200 bg-white">
                                        <div className="px-3 py-2 border-b border-slate-100 flex items-center gap-2">
                                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-500"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>
                                          <span className="text-xs font-bold uppercase tracking-wider text-slate-500">Configuration</span>
                                        </div>
                                        <div className="p-3 max-h-60 overflow-auto">
                                          {irStep?.params && Object.keys(irStep.params).length > 0 ? (
                                            <pre className="text-xs font-mono text-slate-700 whitespace-pre-wrap break-all">
                                              {JSON.stringify(irStep.params, null, 2)}
                                            </pre>
                                          ) : (
                                            <div className="text-xs text-slate-400 italic">No parameters configured.</div>
                                          )}
                                        </div>
                                      </div>

                                      {/* ── Input received (upstream output samples) ── */}
                                      <div className="rounded-lg border border-slate-200 bg-white">
                                        <div className="px-3 py-2 border-b border-slate-100 flex items-center gap-2">
                                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-blue-500"><polyline points="15 10 20 15 15 20" /><path d="M4 4v7a4 4 0 0 0 4 4h12" /></svg>
                                          <span className="text-xs font-bold uppercase tracking-wider text-slate-500">Input received</span>
                                          {upstreamLogs.length > 0 && (
                                            <span className="text-[9px] font-mono text-slate-400">from {upstreamLogs.length} upstream</span>
                                          )}
                                        </div>
                                        <div className="p-3 max-h-60 overflow-auto">
                                          {upstreamLogs.length === 0 ? (
                                            <div className="text-xs text-slate-400 italic">
                                              {upstreams.length > 0 ? 'Upstream step did not produce captured output.' : 'Source step — no upstream input.'}
                                            </div>
                                          ) : upstreamLogs.map((u: any) => (
                                            <div key={u.step_id} className="mb-2 last:mb-0">
                                              <div className="text-xs font-mono text-slate-400 mb-1">
                                                ← {u.step_name || u.step_id} ({(u.rows_processed ?? 0).toLocaleString()} rows)
                                              </div>
                                              {Array.isArray(u.output_preview) && u.output_preview.length > 0 ? (
                                                <pre className="text-xs font-mono text-slate-700 bg-slate-50 rounded px-2 py-1.5 whitespace-pre-wrap break-all">
                                                  {JSON.stringify(u.output_preview.slice(0, 3), null, 2)}
                                                </pre>
                                              ) : (
                                                <div className="text-xs text-slate-400 italic">No sample data captured.</div>
                                              )}
                                            </div>
                                          ))}
                                        </div>
                                      </div>

                                      {/* ── Output produced (this step's output sample) ── */}
                                      <div className="rounded-lg border border-slate-200 bg-white">
                                        <div className="px-3 py-2 border-b border-slate-100 flex items-center gap-2">
                                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-emerald-500"><polyline points="9 10 4 15 9 20" /><path d="M20 4v7a4 4 0 0 1-4 4H4" /></svg>
                                          <span className="text-xs font-bold uppercase tracking-wider text-slate-500">Output produced</span>
                                          {step.rows_processed > 0 && (
                                            <span className="text-[9px] font-mono text-slate-400">{step.rows_processed.toLocaleString()} rows</span>
                                          )}
                                        </div>
                                        <div className="p-3 max-h-60 overflow-auto">
                                          {step.status === 'error' ? (
                                            <div className="text-xs text-red-500 break-all whitespace-pre-wrap font-mono">
                                              {step.error_message || 'Step errored — no output produced.'}
                                            </div>
                                          ) : Array.isArray(step.output_preview) && step.output_preview.length > 0 ? (
                                            <pre className="text-xs font-mono text-slate-700 whitespace-pre-wrap break-all">
                                              {JSON.stringify(step.output_preview.slice(0, 5), null, 2)}
                                            </pre>
                                          ) : step.status === 'skipped' ? (
                                            <div className="text-xs text-slate-400 italic">Step was skipped.</div>
                                          ) : (
                                            <div className="text-xs text-slate-400 italic">No output sample captured.</div>
                                          )}
                                        </div>
                                      </div>
                                    </div>
                                  </td>
                                </tr>
                              )}
                            </Fragment>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                  </>
                  );
                })()}
              </div>
            )}

            {/* ===== LOGS VIEW ===== */}
            {detailTab === 'logs' && (
              <div className="flex flex-col h-full">
                {/* Log toolbar */}
                <div className="px-6 py-3 border-b border-slate-200 bg-white flex items-center gap-3 shrink-0">
                  <div className="flex gap-0.5 bg-slate-100 rounded-lg p-0.5">
                    {(['all', 'error', 'warn', 'info', 'debug'] as const).map((lvl) => (
                      <button
                        key={lvl}
                        onClick={() => setLogLevel(lvl)}
                        className={`px-3 py-1.5 text-xs font-bold rounded-md uppercase transition-all ${
                          logLevel === lvl
                            ? lvl === 'error' ? 'bg-red-500 text-white' : lvl === 'warn' ? 'bg-amber-500 text-white' : 'bg-white text-slate-700 shadow-sm'
                            : 'text-slate-400 hover:text-slate-600'
                        }`}
                      >
                        {lvl}
                      </button>
                    ))}
                  </div>
                  <input
                    value={logSearch}
                    onChange={(e) => setLogSearch(e.target.value)}
                    placeholder="Search logs..."
                    className="flex-1 max-w-sm px-3 py-1.5 text-xs border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300"
                  />
                </div>
                {/* Log output */}
                <div className="flex-1 overflow-auto bg-slate-900 p-4 font-mono text-xs leading-[20px]">
                  {(() => {
                    const allLogs: LogEntry[] = selectedExec.log_entries || [];
                    if (!allLogs.length && selectedExec.step_logs) {
                      for (const step of selectedExec.step_logs) {
                        allLogs.push({ timestamp: step.started_at || selectedExec.started_at, level: 'info', message: `[${step.step_name || step.step_id}] Starting ${step.step_type}...`, step_id: step.step_id });
                        if (step.rows_processed > 0) {
                          allLogs.push({ timestamp: step.completed_at || step.started_at || '', level: 'info', message: `[${step.step_name || step.step_id}] Processed ${step.rows_processed.toLocaleString()} rows in ${formatDuration(step.duration_ms)}`, step_id: step.step_id });
                        }
                        if (step.status === 'success') {
                          allLogs.push({ timestamp: step.completed_at || '', level: 'info', message: `[${step.step_name || step.step_id}] \u2713 Completed successfully`, step_id: step.step_id });
                        }
                        if (step.error_message) {
                          allLogs.push({ timestamp: step.completed_at || '', level: 'error', message: `[${step.step_name || step.step_id}] ${step.error_message}`, step_id: step.step_id });
                        }
                        if (step.logs) allLogs.push(...step.logs);
                      }
                    }
                    const filteredLogs = allLogs.filter((log) => {
                      if (logLevel !== 'all' && log.level !== logLevel) return false;
                      if (logSearch && !(log.message || '').toLowerCase().includes(logSearch.toLowerCase())) return false;
                      return true;
                    });
                    if (!filteredLogs.length) {
                      return <div className="text-slate-500 text-center py-12">No log entries{logLevel !== 'all' ? ` at level "${logLevel}"` : ''}.</div>;
                    }
                    const LOG_COLORS: Record<string, string> = {
                      info: 'text-blue-400', warn: 'text-amber-400', error: 'text-red-400', debug: 'text-slate-500',
                    };
                    return filteredLogs.map((log, i) => (
                      <div key={i} className="flex gap-3 hover:bg-slate-800/50 px-2 py-0.5 rounded">
                        <span className="text-slate-600 shrink-0 select-none">{log.timestamp ? new Date(log.timestamp).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '--:--:--'}</span>
                        <span className={`shrink-0 uppercase w-12 text-right font-bold ${LOG_COLORS[log.level] || 'text-slate-400'}`}>{log.level}</span>
                        <span className="text-slate-200 break-all whitespace-pre-wrap">{log.message}</span>
                      </div>
                    ));
                  })()}
                </div>
              </div>
            )}
          </div>
          </div>
        </div>
      )}
      </div>

      {/* Step-IO drawer (lineage node click → input/output replay) */}
      <StepIODrawer
        open={ioStep !== null}
        executionId={selectedExec?.id || null}
        stepId={ioStep?.stepId || null}
        stepLabel={ioStep?.label}
        stepInfo={(() => {
          const log = selectedExec?.step_logs?.find((s: any) => s.step_id === ioStep?.stepId);
          if (!log) return undefined;
          return {
            step_type: log.step_type,
            status: log.status,
            duration_ms: log.duration_ms,
            rows_processed: log.rows_processed,
          };
        })()}
        onClose={() => setIOStep(null)}
      />
    </div>
  );
}
