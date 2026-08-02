import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';
import TierChip from '../shared/TierChip';
import Icon from '../shared/Icon';
import { askCopilot } from '../../hooks/useAgentChatStore';
import { usePageContext } from '../../hooks/usePageContext';
import PageHeader from '../shared/PageHeader';
import { renderMarkdown } from '../../utils/markdown';

// System Inventory Report page — Apr 2026
//
// Generates a single, beautiful document describing the live state of
// this F-Pulse installation. Users pick Word (.docx) or PDF and
// Admin or User scope, then download. A preview panel shows the totals
// and health score before download so users know what they're getting.
//
// Endpoint: GET /api/reports/inventory?format=<docx|pdf|json>&scope=<admin|user>&env=<dev>
// Preview:  GET /api/reports/inventory/summary?scope=<admin|user>&env=<dev>

interface InventorySummary {
  workspace_name: string;
  generated_at: string;
  generated_by: string;
  scope: string;
  tier: 'plus' | 'free';
  env_filter: 'all' | 'dev' | 'prod';
  fpulse_version: string;
  schema_version: number;
  totals: Record<string, number>;
  health: { score: number; issues: string[]; issue_count: number };
  project_count: number;
  connection_count: number;
  user_count: number;
  // Governance envelope — tells the UI whether to allow PROD selection.
  env_restrictions?: {
    can_see_prod: boolean;
    role: string;
  };
  operational?: {
    window_hours: number;
    total_executions: number;
    successful_executions: number;
    failed_executions: number;
    success_rate_pct: number;
    recent_failure_count: number;
    upcoming_run_count: number;
    next_run_at: string;
  };
}

type ReportFormat = 'docx' | 'pdf';
// Extended scopes (Apr 27 2026): in addition to System (admin) and User-self,
// reports can be downloaded scoped to a specific Project, Pipeline, or User.
// Backend honors the matching scope_type + scope_id query params.
type ReportScope = 'admin' | 'user' | 'project' | 'pipeline';
// Apr 27 2026 second pass: dropped 'all' — DEV and PROD reports show
// fundamentally different sections (build phase vs operate phase / audit),
// merging them into one document is incoherent. Force a binary pick.
type ReportEnv = 'dev' | 'prod';

interface User {
  id: string;
  email: string;
  role: string;
  name?: string;
}

export default function ReportsPage({
  user,
  embedded = false,
  tier = 'free',
  environment = 'dev',
}: {
  user: User | null;
  embedded?: boolean;
  // 2026-05-19 (P0 #6 of PAGE_BY_PAGE_AUDIT.md): the standalone header used
  // to hardcode `<TierChip tier="free" environment="dev" />`, ignoring the
  // real instance state. Plumbed both as props with safe defaults; the
  // standalone path now honours them.
  tier?: 'plus' | 'free';
  environment?: 'dev' | 'prod';
}) {
  const [summary, setSummary] = useState<InventorySummary | null>(null);
  const [scope, setScope] = useState<ReportScope>(
    user && (user.role === 'super_admin' || user.role === 'admin') ? 'admin' : 'user',
  );
  // Default to the current environment so the user lands on the right
  // template — DEV from the dev nav, PROD from the prod nav. Falls back
  // to 'dev' for cold sessions / Free tier (no PROD).
  const [env, setEnv] = useState<ReportEnv>(() => {
    try {
      const stored = (localStorage.getItem('fpulse_env') || 'dev').toLowerCase();
      return stored === 'prod' ? 'prod' : 'dev';
    } catch { return 'dev'; }
  });
  const [format, setFormat] = useState<ReportFormat>('pdf');
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  // Inline preview — "View" fetches the report as JSON and renders it as
  // real HTML in a modal, so it displays in every browser AND the Electron
  // desktop app (an embedded PDF frame renders blank there). The user reads
  // the report — including each pipeline's purpose / README — without
  // downloading a file; PDF / Word remain as downloads.
  const [viewing, setViewing] = useState(false);
  const [viewData, setViewData] = useState<any | null>(null);
  // Scope target — when scope is 'project' / 'pipeline' / 'user', this
  // is the id of the picked entity. For 'user' on non-admins, defaults
  // to self. For 'admin', unused.
  const [scopeTargetId, setScopeTargetId] = useState<string>('');
  // Pickers for the three new scopes. Loaded lazily when the scope flips.
  const [projects, setProjects] = useState<Array<{ id: string; name: string }>>([]);
  const [pipelines, setPipelines] = useState<Array<{ id: string; name: string }>>([]);
  const [users, setUsers] = useState<Array<{ id: string; email: string; name?: string }>>([]);

  const isAdmin = user && (user.role === 'super_admin' || user.role === 'admin');

  const loadSummary = async () => {
    setLoading(true);
    try {
      const data = await api.get<InventorySummary>(
        `/api/reports/inventory/summary?scope=${scope}&env=${env}`,
      );
      setSummary(data);
    } catch (err: any) {
      toast.error(err.message || 'Failed to load inventory summary');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadSummary();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, env]);

  // Load the picker list whenever the scope flips into a list-driven mode.
  useEffect(() => {
    if (scope === 'project' && projects.length === 0) {
      api.listProjects().then((rows: any[]) => {
        setProjects((rows || []).map((p: any) => ({ id: p.id, name: p.name || p.id })));
      }).catch(() => {});
    } else if (scope === 'pipeline' && pipelines.length === 0) {
      api.listWorkflows().then((rows: any[]) => {
        setPipelines((rows || []).map((p: any) => ({ id: p.id, name: p.name || p.id })));
      }).catch(() => {});
    } else if (scope === 'user' && users.length === 0 && isAdmin) {
      (api as any).listUsers?.().then((rows: any[]) => {
        setUsers((rows || []).map((u: any) => ({ id: u.id, email: u.email, name: u.name })));
      }).catch(() => {});
    }
    // Reset the target id when scope changes — the previous target is
    // for the wrong entity type.
    setScopeTargetId('');
  }, [scope, isAdmin]);  // eslint-disable-line react-hooks/exhaustive-deps

  // OSS has no PROD environment, so the report always operates against
  // DEV. The setEnv setter is retained because it ships in the same
  // hook tuple useState gives back; we just never call it.
  void setEnv;

  // FOLLOW-3 (2026-05-19) — publish report-shape context so the Copilot
  // can answer "what scope am I about to download?" without re-asking.
  usePageContext({
    page: 'reports',
    filters: { scope, format, scope_target_id: scopeTargetId || null },
  });

  const handleDownload = async () => {
    setDownloading(true);
    try {
      const token = localStorage.getItem('fpulse_token');
      const workspaceId = localStorage.getItem('fpulse_workspace_id') || 'default';
      // Build URL with optional scope_id for project/pipeline/user-pick scopes.
      // The backend report generator reads `scope_type` (=scope) and
      // `scope_id` to filter; legacy scopes (admin/user) ignore scope_id.
      const params = new URLSearchParams({ format, scope, env });
      if ((scope === 'project' || scope === 'pipeline' || scope === 'user') && scopeTargetId) {
        params.set('scope_id', scopeTargetId);
      }
      const res = await fetch(
        `/api/reports/inventory?${params.toString()}`,
        {
          headers: {
            Authorization: `Bearer ${token}`,
            'X-Workspace-Id': workspaceId,
          },
        },
      );
      if (!res.ok) {
        let msg = `Download failed (${res.status})`;
        try {
          const err = await res.json();
          msg = err.detail || msg;
        } catch {
          /* ignore */
        }
        throw new Error(msg);
      }
      const blob = await res.blob();
      const disposition = res.headers.get('content-disposition') || '';
      const m = /filename="([^"]+)"/.exec(disposition);
      const filename = m ? m[1] : `fpulse-inventory.${format}`;
      // Storage wiring Phase 1 — backend writes a copy of the report
      // into the workspace Storage tree and returns the new object id
      // in X-Storage-Object-Id. The download still fires immediately
      // (same UX as before); the toast adds a one-click jump to find
      // the saved row on the Storage page later.
      const storageObjectId = res.headers.get('x-storage-object-id') || '';
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      if (storageObjectId) {
        try { sessionStorage.setItem('fpulse_storage_highlight_object', storageObjectId); } catch { /* ignore */ }
        toast.success(`Downloaded ${filename}`, 'Saved to Storage — open the Storage page to find it later');
      } else {
        toast.success(`Downloaded ${filename}`);
      }
    } catch (err: any) {
      toast.error(err.message || 'Download failed');
    } finally {
      setDownloading(false);
    }
  };

  // Fetch the report as JSON and open the inline HTML preview.
  const handleView = async () => {
    setViewing(true);
    try {
      const token = localStorage.getItem('fpulse_token');
      const workspaceId = localStorage.getItem('fpulse_workspace_id') || 'default';
      const params = new URLSearchParams({ format: 'json', scope, env });
      if ((scope === 'project' || scope === 'pipeline' || scope === 'user') && scopeTargetId) {
        params.set('scope_id', scopeTargetId);
      }
      const res = await fetch(`/api/reports/inventory?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token}`, 'X-Workspace-Id': workspaceId },
      });
      if (!res.ok) throw new Error(`Preview failed (${res.status})`);
      setViewData(await res.json());
    } catch (err: any) {
      toast.error(err.message || 'Could not open report preview');
    } finally {
      setViewing(false);
    }
  };

  const closeView = () => setViewData(null);

  const healthColor =
    summary && summary.health.score >= 80
      ? 'text-emerald-600 bg-emerald-50 border-emerald-200'
      : summary && summary.health.score >= 50
      ? 'text-amber-600 bg-amber-50 border-amber-200'
      : 'text-red-600 bg-red-50 border-red-200';

  // When embedded inside the AI hub the parent page provides the chrome
  // (sticky DEV/PROD banner + tab bar) — render only the body via a slim
  // wrapper. Otherwise render the canonical standalone page header.
  const Wrapper: React.FC<{ children: React.ReactNode }> = ({ children }) => (
    embedded ? (
      <div className="space-y-4">{children}</div>
    ) : (
      /* FOLLOW-1 (2026-05-19) — migrated to shared <PageHeader>. */
      <div className="flex-1 flex flex-col overflow-hidden">
        <PageHeader
          environment={environment}
          icon={(
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-indigo-500">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <polyline points="14 2 14 8 20 8" />
              <line x1="16" y1="13" x2="8" y2="13" />
              <line x1="16" y1="17" x2="8" y2="17" />
              <polyline points="10 9 9 9 8 9" />
            </svg>
          )}
          title="Reports"
          subtitle="Download a formatted document describing the live state of your F-Pulse installation."
          titleAccessory={<TierChip tier={tier} environment={environment} />}
        />
        <div className="flex-1 overflow-auto bg-canvas-bg">
          <div className="w-full max-w-[1500px] mx-auto px-6 py-5 space-y-4">{children}</div>
        </div>
      </div>
    )
  );

  return (
    <Wrapper>

      <div>
{/* (max-width wrapper removed — host page already constrains width) */}
        {/* Z41 (2026-05-23) — reframed from "Quick reports" (sounded like
            a competing download path) to "Quick answers from Copilot"
            with a contrasting violet tint, so the user reads it as a
            sibling shortcut to Q&A rather than a different shape of
            the same report. The actual document generator lives in the
            REPORT OPTIONS card below — these are inline-answer
            shortcuts that open the Copilot dock instead. */}
        <div className="mb-6 rounded-xl border border-violet-200 bg-violet-50/40 p-6 shadow-sm">
          <div className="flex items-start gap-3 mb-4">
            <span className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-violet-100 text-violet-700 shrink-0">
              <Icon name="zap" size={16} />
            </span>
            <div className="flex-1 min-w-0">
              <h2 className="text-sm font-bold uppercase tracking-wide text-violet-700">
                Quick answers from Copilot
              </h2>
              <p className="mt-0.5 text-xs text-slate-600">
                Just want a focused answer? Click one of these — Copilot opens with the question pre-filled. Use the <b>Report options</b> below for a downloadable PDF or Word document.
              </p>
            </div>
          </div>
          <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
            {([
              {
                label: 'Show failures',
                hint: 'Recent failed runs + most common error',
                icon: 'alert-triangle' as const,
                tone: 'text-amber-600',
                prompt:
                  'Show me the failures in the last 24 hours. Group by pipeline, include the most common error message, and tell me which one to fix first.',
              },
              {
                label: 'Slow pipelines',
                hint: 'Average and p95 run time per pipeline',
                icon: 'clock' as const,
                tone: 'text-violet-600',
                prompt:
                  'Which pipelines are slow? List average and p95 run duration per pipeline over the last 7 days, and flag any regressions vs. the prior week.',
              },
              {
                label: 'Needs attention',
                hint: 'Health score + top issues to fix',
                icon: 'activity' as const,
                tone: 'text-emerald-600',
                prompt:
                  'What needs my attention right now? Use the installation health score and inventory to give me a prioritised punch list — failures first, then config risks (inline credentials, missing schedules, idle alerts).',
              },
            ]).map((q) => (
              <button
                key={q.label}
                type="button"
                onClick={() => askCopilot(q.prompt)}
                className="rounded-lg border border-slate-200 bg-white p-3 text-left text-sm transition hover:border-violet-400 hover:bg-violet-50/40"
              >
                <div className="flex items-center gap-2 font-medium text-slate-800">
                  <Icon name={q.icon} size={16} className={q.tone} />
                  {q.label}
                </div>
                <div className="mt-0.5 text-xs text-slate-500">{q.hint}</div>
              </button>
            ))}
          </div>
        </div>

        {/* Z39 (2026-05-23) — 2-column layout. LEFT = the configurator
            (scope + format picker), RIGHT = sticky preview panel that
            holds the Download action so the user sees what they're
            about to generate right next to the button. Below `lg` it
            collapses to a single column, preview reading below options.
            All existing fetch logic (loadSummary on prop change,
            handleDownload, scope-target validation) is unchanged — only
            the JSX containers move. */}
        <div className="grid grid-cols-1 lg:grid-cols-[minmax(320px,0.75fr)_minmax(0,1.7fr)] gap-4 items-start">
        {/* ── LEFT column — Report options ─────────────────────────── */}
        <div className="space-y-4 min-w-0">
        {/* Controls */}
        <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <div className="mb-4 flex items-center gap-2">
            <h2 className="text-sm font-bold uppercase tracking-wide text-slate-800">
              Report options
            </h2>
            <span className="text-[10px] font-bold uppercase tracking-wider text-slate-500">
              · Downloadable PDF / Word document
            </span>
          </div>

          <div className="grid grid-cols-1 gap-6 md:grid-cols-3">
            {/* Scope — Z41 (2026-05-23): 2x2 grid so each card has room
                for its description without 5-line wrap. */}
            <div className="md:col-span-3">
              <label className="mb-2 flex items-center gap-2 text-sm font-semibold text-slate-700">
                <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-violet-100 text-violet-700 text-xs font-bold">1</span>
                Pick a scope
              </label>
              <div className="grid grid-cols-2 gap-2">
                <button
                  type="button"
                  disabled={!isAdmin}
                  onClick={() => setScope('admin')}
                  className={`rounded-lg border p-3 text-left text-sm transition ${
                    scope === 'admin'
                      ? 'border-violet-500 bg-violet-50 text-violet-900'
                      : 'border-slate-200 bg-white text-slate-700 hover:border-slate-300'
                  } ${!isAdmin ? 'cursor-not-allowed opacity-50' : ''}`}
                >
                  <div className="font-medium">System</div>
                  <div className="mt-0.5 text-xs text-slate-500">
                    Full workspace inventory
                  </div>
                  {!isAdmin && (
                    <div className="mt-1 text-xs italic text-slate-400">Admin-only</div>
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => setScope('project')}
                  className={`rounded-lg border p-3 text-left text-sm transition ${
                    scope === 'project'
                      ? 'border-violet-500 bg-violet-50 text-violet-900'
                      : 'border-slate-200 bg-white text-slate-700 hover:border-slate-300'
                  }`}
                >
                  <div className="font-medium">Project</div>
                  <div className="mt-0.5 text-xs text-slate-500">
                    All pipelines + assets in one project
                  </div>
                </button>
                <button
                  type="button"
                  onClick={() => setScope('pipeline')}
                  className={`rounded-lg border p-3 text-left text-sm transition ${
                    scope === 'pipeline'
                      ? 'border-violet-500 bg-violet-50 text-violet-900'
                      : 'border-slate-200 bg-white text-slate-700 hover:border-slate-300'
                  }`}
                >
                  <div className="font-medium">Pipeline</div>
                  <div className="mt-0.5 text-xs text-slate-500">
                    Single pipeline — config, runs, schedule
                  </div>
                </button>
                <button
                  type="button"
                  onClick={() => setScope('user')}
                  className={`rounded-lg border p-3 text-left text-sm transition ${
                    scope === 'user'
                      ? 'border-violet-500 bg-violet-50 text-violet-900'
                      : 'border-slate-200 bg-white text-slate-700 hover:border-slate-300'
                  }`}
                >
                  <div className="font-medium">User</div>
                  <div className="mt-0.5 text-xs text-slate-500">
                    {isAdmin ? 'Pick a user (or yourself)' : 'Just your view'}
                  </div>
                </button>
              </div>

              {/* Conditional target picker for Project / Pipeline / User scopes. */}
              {scope === 'project' && (
                <div className="mt-3">
                  <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-slate-500">Pick a project</label>
                  <select
                    value={scopeTargetId}
                    onChange={(e) => setScopeTargetId(e.target.value)}
                    className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-violet-300"
                  >
                    <option value="">— Select a project —</option>
                    {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select>
                </div>
              )}
              {scope === 'pipeline' && (
                <div className="mt-3">
                  <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-slate-500">Pick a pipeline</label>
                  <select
                    value={scopeTargetId}
                    onChange={(e) => setScopeTargetId(e.target.value)}
                    className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-violet-300"
                  >
                    <option value="">— Select a pipeline —</option>
                    {pipelines.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select>
                </div>
              )}
              {scope === 'user' && isAdmin && (
                <div className="mt-3">
                  <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-slate-500">Pick a user (optional — leave blank for yourself)</label>
                  <select
                    value={scopeTargetId}
                    onChange={(e) => setScopeTargetId(e.target.value)}
                    className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-violet-300"
                  >
                    <option value="">— Yourself ({user?.email}) —</option>
                    {users.map((u) => <option key={u.id} value={u.id}>{u.name ? `${u.name} (${u.email})` : u.email}</option>)}
                  </select>
                </div>
              )}
            </div>

            {/* Format — Z39 (2026-05-23) tightened from card-with-description
                to a compact segmented control. PDF and Word are just two
                options; the long explanation read awkwardly inside the
                narrower left column. Per-format hint moves to a single
                line below the segment so the picker stays one row tall. */}
            <div>
              <label className="mb-2 flex items-center gap-2 text-sm font-semibold text-slate-700">
                <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-violet-100 text-violet-700 text-xs font-bold">2</span>
                Pick a download format
              </label>
              <div role="group" aria-label="Download format" className="inline-flex items-center rounded-lg border border-slate-200 bg-slate-50 p-0.5">
                <button
                  type="button"
                  onClick={() => setFormat('pdf')}
                  aria-pressed={format === 'pdf'}
                  className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-semibold rounded-md transition ${
                    format === 'pdf'
                      ? 'bg-white text-violet-700 shadow-sm border border-violet-200'
                      : 'text-slate-600 hover:text-slate-800'
                  }`}
                >
                  <Icon name="file-text" size={14} />
                  PDF
                </button>
                <button
                  type="button"
                  onClick={() => setFormat('docx')}
                  aria-pressed={format === 'docx'}
                  className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-semibold rounded-md transition ${
                    format === 'docx'
                      ? 'bg-white text-violet-700 shadow-sm border border-violet-200'
                      : 'text-slate-600 hover:text-slate-800'
                  }`}
                >
                  <Icon name="file-edit" size={14} />
                  Word
                </button>
              </div>
              <p className="mt-1.5 text-xs text-slate-500">
                {format === 'pdf'
                  ? 'Print-ready · best for archival and sharing.'
                  : 'Editable .docx · best for internal review cycles.'}
              </p>
            </div>
          </div>

          {/* Block download if Project / Pipeline scope but no target picked. */}
          {(scope === 'project' || scope === 'pipeline') && !scopeTargetId && (
            <p className="mt-4 text-xs text-amber-600 italic">
              Pick a {scope === 'project' ? 'project' : 'pipeline'} above to enable download.
            </p>
          )}
          {/* Z39 (2026-05-23) — download + refresh moved into the preview
              panel header on the right so the action sits next to what
              the user is about to generate. */}
        </div>
        </div>
        {/* ── RIGHT column — Sticky preview ─────────────────────────── */}
        <aside className="space-y-4 min-w-0 lg:sticky lg:top-[80px]">
          {/* Preview header — title + Download action. Stays visible
              alongside the body so the user keeps the CTA in reach
              while reading the preview. */}
          <div className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
            <div className="px-5 py-3 bg-gradient-to-b from-slate-50 to-white border-b border-slate-200/70 flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-violet-100 text-violet-700 text-xs font-bold">3</span>
                  <div className="text-xs font-bold uppercase tracking-wider text-slate-800">Preview & download</div>
                </div>
                <div className="text-[11px] text-slate-500 mt-0.5">
                  {summary
                    ? 'This is what your downloaded report will contain.'
                    : 'Pick scope + format on the left, then download here.'}
                </div>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <button
                  type="button"
                  onClick={loadSummary}
                  disabled={loading}
                  title="Re-fetch the inventory summary from the backend"
                  className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-700 hover:border-slate-300 disabled:opacity-50"
                >
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10" /><polyline points="1 20 1 14 7 14" /><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10" /><path d="M20.49 15A9 9 0 0 1 5.64 18.36L1 14" /></svg>
                  {loading ? 'Loading…' : 'Refresh'}
                </button>
                <button
                  type="button"
                  onClick={handleView}
                  disabled={
                    viewing || !summary ||
                    ((scope === 'project' || scope === 'pipeline') && !scopeTargetId)
                  }
                  title="View the report in the browser without downloading"
                  className="inline-flex items-center gap-1.5 rounded-md border border-violet-200 bg-white px-3 py-1.5 text-xs font-bold text-violet-700 transition hover:bg-violet-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {viewing ? (
                    <>
                      <svg className="h-3.5 w-3.5 animate-spin" viewBox="0 0 24 24" fill="none">
                        <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity="0.25" strokeWidth="4" />
                        <path d="M4 12a8 8 0 018-8" stroke="currentColor" strokeWidth="4" strokeLinecap="round" />
                      </svg>
                      Opening…
                    </>
                  ) : (
                    <>
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" />
                      </svg>
                      View
                    </>
                  )}
                </button>
                <button
                  type="button"
                  onClick={handleDownload}
                  disabled={
                    downloading || !summary ||
                    ((scope === 'project' || scope === 'pipeline') && !scopeTargetId)
                  }
                  className="inline-flex items-center gap-1.5 rounded-md bg-violet-600 px-3 py-1.5 text-xs font-bold text-white shadow-sm transition hover:bg-violet-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {downloading ? (
                    <>
                      <svg className="h-3.5 w-3.5 animate-spin" viewBox="0 0 24 24" fill="none">
                        <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity="0.25" strokeWidth="4" />
                        <path d="M4 12a8 8 0 018-8" stroke="currentColor" strokeWidth="4" strokeLinecap="round" />
                      </svg>
                      Generating…
                    </>
                  ) : (
                    <>
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                        <polyline points="7 10 12 15 17 10" />
                        <line x1="12" y1="15" x2="12" y2="3" />
                      </svg>
                      Download {format.toUpperCase()}
                    </>
                  )}
                </button>
              </div>
            </div>
            <p className="px-5 py-2 text-[11px] text-slate-400 bg-slate-50/40 border-b border-slate-100">
              Report is generated fresh on every download.
            </p>
          </div>

        {/* Preview */}
        {loading && !summary && (
          <div className="rounded-xl border border-slate-200 bg-white p-10 text-center shadow-sm">
            <div className="text-sm text-slate-500">Loading inventory preview…</div>
          </div>
        )}

        {summary && (
          <>
            {/* Redaction notice — trust signal that the downloaded doc is safe to share.
                Free vs Plus: OSS has no Vault, so credentials always render as
                [INLINE — MIGRATE] markers. Plus also masks Vault-backed refs. */}
            <div className="mb-6 flex items-start gap-3 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
              <Icon name="lock" size={18} className="text-emerald-700 mt-0.5" />
              <div>
                <div className="font-semibold">
                  Credentials are redacted in the downloaded report.
                </div>
                <div className="mt-0.5 text-xs text-emerald-700">
                  {summary.tier === 'free' ? (
                    <>
                      Secrets are flagged as
                      <span className="mx-1 font-mono">[INLINE — MIGRATE]</span>
                      so the report is safe to share via email, ticketing, or print.
                    </>
                  ) : (
                    <>
                      Secrets are shown as masked Vault references or flagged as
                      <span className="mx-1 font-mono">[INLINE — MIGRATE]</span>.
                      The report is safe to share via email, ticketing, or print.
                    </>
                  )}
                </div>
              </div>
            </div>

            {/* Z42 (2026-05-23) — preview is now a TABLE OF CONTENTS for
                the downloaded document, not a duplicate Dashboard.
                Previous version showed live pipeline/connection/op
                snapshot/health stats — but those already live on the
                Dashboard page. Here the user wants to know
                "what sections will my PDF contain?" before they click
                Download. Section list mirrors the 8 canonical sections
                emitted by backend/fpulse/reports/inventory_pdf.py (and
                the matching docx renderer). Plus-only sections render
                with a violet "F-Pulse+" chip when the install is free
                so the user can see what they'd unlock. */}
            <div className="mb-6 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <div className="mb-3 flex items-center justify-between gap-2">
                <h3 className="text-sm font-bold uppercase tracking-wide text-slate-800">
                  What's in this report
                </h3>
                <span className="text-[10px] font-bold uppercase tracking-wider text-slate-500">
                  {summary.tier === 'free' ? 'OSS · System scope' : 'Plus · ' + (summary.scope === 'admin' ? 'Admin' : 'User')}
                </span>
              </div>
              <ol className="space-y-1.5">
                {(() => {
                  const isFree = summary.tier === 'free';
                  // Single source of truth — order, titles, and counts
                  // mirror inventory_pdf.py's `_add_*_section` chain.
                  const sections: Array<{
                    num: string;
                    title: string;
                    hint: string;
                    count?: number | string;
                    plus?: boolean;
                  }> = [
                    {
                      num: '1',
                      title: 'Executive summary',
                      hint: 'Health score · top issues · run-duration + failure analysis (30d)',
                      count: `${summary.health.score}/100`,
                    },
                    {
                      num: '2',
                      title: 'Operational audit',
                      hint: '24h execution health · failed pipelines · next scheduled runs',
                      count: summary.operational
                        ? `${summary.operational.total_executions} runs · ${summary.operational.recent_failure_count} fails`
                        : '—',
                    },
                    {
                      num: '3',
                      title: 'Projects',
                      hint: 'Project list with attached pipelines + assets',
                      count: summary.project_count ?? 0,
                    },
                    {
                      num: '4',
                      title: 'Connections',
                      hint: 'Connection details · type · scope · last test',
                      count: summary.connection_count ?? 0,
                    },
                    {
                      num: '5',
                      title: 'Users',
                      hint: 'Roster + by-role breakdown',
                      count: isFree ? '—' : (summary.user_count ?? 0),
                      plus: true,
                    },
                    {
                      num: '6',
                      title: 'Schedules',
                      hint: 'Cron / event triggers · enabled state · last + next run',
                      count: summary.totals.schedules ?? 0,
                    },
                    {
                      num: '7',
                      title: 'Alert rules',
                      hint: 'Channels · severity · attached pipelines',
                      count: summary.totals.alerts ?? 0,
                    },
                    {
                      num: '8',
                      title: 'Approval gates',
                      hint: 'PROD deploys gated by reviewer + audit trail',
                      count: isFree ? '—' : (summary.totals.approval_gates ?? 0),
                      plus: true,
                    },
                  ];
                  return sections.filter((s) => !(s.plus && isFree)).map((s) => {
                    const plusGated = s.plus && isFree;
                    return (
                      <li
                        key={s.num}
                        className={`flex items-start gap-3 rounded-md px-2 py-1.5 ${plusGated ? 'opacity-60' : ''}`}
                      >
                        <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-slate-100 text-slate-600 text-[11px] font-bold shrink-0">
                          {s.num}
                        </span>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-sm font-semibold text-slate-800">{s.title}</span>
                            {plusGated && (
                              <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-violet-100 text-violet-700">
                                F-Pulse+
                              </span>
                            )}
                          </div>
                          <div className="text-xs text-slate-500 mt-0.5">{s.hint}</div>
                        </div>
                        <span className="text-xs font-mono font-semibold text-slate-700 shrink-0 self-start mt-0.5 tabular-nums">
                          {s.count}
                        </span>
                      </li>
                    );
                  });
                })()}
                {/* Upgrade appendix — hidden in single-operator OSS */}
                {false && summary?.tier === 'free' && (
                  <li className="flex items-start gap-3 rounded-md px-2 py-1.5 mt-1 border-t border-slate-100 pt-3">
                    <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-violet-100 text-violet-700 text-[11px] font-bold shrink-0">
                      +
                    </span>
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-semibold text-slate-800">Upgrade appendix</div>
                      <div className="text-xs text-slate-500 mt-0.5">
                        OSS reports include a section explaining Plus features (Users, Vault, Approval Gates).
                      </div>
                    </div>
                  </li>
                )}
              </ol>
              <p className="mt-3 pt-3 border-t border-slate-100 text-[11px] text-slate-500">
                Numbers above are the live counts that will appear in the document. Want to track
                workspace metrics over time? That's on the <b>Dashboard</b>, not here.
              </p>
            </div>

            {/* Metadata */}
            <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
              <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
                Report metadata
              </h3>
              <dl className="grid grid-cols-1 gap-3 text-sm md:grid-cols-2">
                <MetaRow label="Workspace" value={summary.workspace_name} />
                <MetaRow
                  label="Scope"
                  value={
                    summary.tier === 'free'
                      ? 'Workspace (full)'
                      : summary.scope === 'admin'
                      ? 'Administrator (full)'
                      : 'User (ACL-filtered)'
                  }
                />
                <MetaRow label="Generated by" value={summary.generated_by} />
                <MetaRow label="F-Pulse version" value={`${summary.fpulse_version} (schema v${summary.schema_version})`} />
              </dl>
            </div>
          </>
        )}
        </aside>
        </div>
      </div>

      {/* Inline report preview (View) — real HTML, renders everywhere
          (browser + Electron desktop), never a blank PDF frame. */}
      {viewData && (
        <>
          <div className="fixed inset-0 z-[60] bg-black/40" onClick={closeView} />
          <div className="fixed inset-0 z-[65] flex items-center justify-center p-4 pointer-events-none">
            <div className="pointer-events-auto flex h-[90vh] w-[960px] max-w-[96vw] flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl">
              <div className="flex shrink-0 items-center justify-between border-b border-slate-200 px-5 py-3">
                <div className="text-sm font-bold text-slate-800">Report preview</div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={handleDownload}
                    className="inline-flex items-center gap-1.5 rounded-md bg-violet-600 px-3 py-1.5 text-xs font-bold text-white hover:bg-violet-700"
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
                    </svg>
                    Download {format.toUpperCase()}
                  </button>
                  <button
                    type="button"
                    onClick={closeView}
                    className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 hover:bg-slate-100 hover:text-slate-600"
                  >
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                      <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                    </svg>
                  </button>
                </div>
              </div>
              <div className="flex-1 overflow-y-auto p-6">
                <ReportView data={viewData} />
              </div>
            </div>
          </div>
        </>
      )}
    </Wrapper>
  );
}

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <dt className="min-w-[140px] text-xs uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="text-slate-900">{value}</dd>
    </div>
  );
}

function _fmtTs(s: string): string {
  if (!s) return '—';
  if (s.includes('T')) {
    const [d, r] = s.split('T');
    return `${d} ${(r || '').slice(0, 5)}`.trim();
  }
  return s.slice(0, 16);
}

// Inline HTML rendering of the inventory report (from the JSON payload). Real
// DOM — renders in every browser and the Electron desktop app, unlike an
// embedded PDF frame. Mirrors the key blocks the PDF/Word renderers emit.
function ReportView({ data }: { data: any }) {
  const t = data.totals || {};
  const health = data.health || {};
  const projects: any[] = data.projects || [];
  const score = health.score ?? 0;
  const scoreClass = score >= 80 ? 'text-emerald-600' : score >= 50 ? 'text-amber-600' : 'text-red-600';
  return (
    <div className="text-slate-700">
      <h1 className="text-2xl font-bold text-slate-900">
        {data.workspace_name || 'Workspace'} — inventory report
      </h1>
      <p className="mt-1 text-xs text-slate-500">
        Generated {_fmtTs(data.generated_at)} · scope {data.scope} · F-Pulse {data.fpulse_version}
        {data.env_filter ? ` · ${String(data.env_filter).toUpperCase()}` : ''}
      </p>

      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <RvStat label="Health" value={`${score}/100`} valueClass={scoreClass} />
        <RvStat label="Pipelines" value={t.pipelines ?? 0} />
        <RvStat label="Connections" value={t.connections ?? 0} />
        <RvStat label="Projects" value={t.projects ?? 0} />
      </div>

      {Array.isArray(health.issues) && health.issues.length > 0 && (
        <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-800">
          <div className="font-semibold">Top issues</div>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {health.issues.slice(0, 6).map((iss: string, i: number) => <li key={i}>{iss}</li>)}
          </ul>
        </div>
      )}

      {projects.length === 0 && (
        <p className="mt-6 text-sm text-slate-400">No projects or published pipelines in this scope yet.</p>
      )}

      {projects.map((proj: any) => (
        <section key={proj.id} className="mt-6">
          <h2 className="border-b border-slate-100 pb-1.5 text-lg font-semibold text-slate-900">
            {proj.name}
            <span className="ml-2 text-xs font-normal text-slate-400">
              · {(proj.pipelines || []).length} pipeline(s)
            </span>
          </h2>
          {(proj.pipelines || []).length === 0 ? (
            <p className="mt-2 text-sm text-slate-400">No published pipelines in this project.</p>
          ) : (
            (proj.pipelines || []).map((p: any) => (
              <div key={p.id} className="mt-4 rounded-xl border border-slate-200 p-4">
                <div className="flex items-center justify-between gap-2">
                  <h3 className="text-base font-semibold text-slate-900">{p.name}</h3>
                  <span className="shrink-0 text-xs text-slate-500">
                    {(p.environments || []).join(' + ')} · {p.status}
                  </span>
                </div>
                {p.business_purpose && (
                  <p className="mt-1.5 text-sm">
                    <span className="font-semibold text-slate-800">Purpose:</span> {p.business_purpose}
                  </p>
                )}
                <table className="mt-3 w-full text-sm">
                  <tbody>
                    <RvRow label="Status" value={p.status} />
                    <RvRow
                      label="Version"
                      value={`v${p.latest_version}${p.deployed_version ? ` (deployed v${p.deployed_version})` : ''}`}
                    />
                    <RvRow label="Owner" value={p.owner || '—'} />
                    {p.description ? <RvRow label="Description" value={p.description} /> : null}
                    <RvRow
                      label="Nodes"
                      value={`${p.step_count} steps${(p.node_types || []).length ? ` — ${(p.node_types).join(', ')}` : ''}`}
                    />
                    {(p.tags || []).length > 0 ? <RvRow label="Tags" value={(p.tags).join(', ')} /> : null}
                    <RvRow
                      label="Last run"
                      value={p.last_run_status ? `${p.last_run_status}${p.last_run_at ? ` (${_fmtTs(p.last_run_at)})` : ''}` : 'never run'}
                    />
                  </tbody>
                </table>
                {p.readme && (
                  <div className="mt-3">
                    <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">Notes (README)</div>
                    <div className="mt-1 rounded-lg border border-slate-100 bg-slate-50/50 px-3 py-2 text-sm">
                      {renderMarkdown(p.readme)}
                    </div>
                  </div>
                )}
              </div>
            ))
          )}
        </section>
      ))}
    </div>
  );
}

function RvStat({ label, value, valueClass = 'text-slate-900' }: { label: string; value: any; valueClass?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2">
      <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">{label}</div>
      <div className={`text-lg font-bold ${valueClass}`}>{value}</div>
    </div>
  );
}

function RvRow({ label, value }: { label: string; value: any }) {
  return (
    <tr className="border-b border-slate-100 last:border-0">
      <td className="w-40 py-1.5 pr-3 align-top text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</td>
      <td className="py-1.5 align-top text-slate-700">{value}</td>
    </tr>
  );
}

function OpsStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | number;
  tone?: 'good' | 'warn' | 'bad' | 'neutral';
}) {
  const valColor =
    tone === 'good'
      ? 'text-emerald-600'
      : tone === 'warn'
      ? 'text-amber-600'
      : tone === 'bad'
      ? 'text-red-600'
      : 'text-slate-900';
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${valColor}`}>{value}</div>
    </div>
  );
}
