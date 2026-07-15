/**
 * PreRunBanner — the inline strip shown immediately above the canvas Run
 * button. Step 4c of the F-Pulse AI completion arc.
 *
 *   ┌─────────────────────────────────────────────────────────────────┐
 *   │ Last run · ✓ success · 12s · 14,231 rows · 2 hrs ago            │
 *   │ Estimate · ~13s · ~14,000 rows                                   │
 *   │ Run safely [ Live ] [ Sample ] [ Dry-run ] [ Validate-only ]     │
 *   └─────────────────────────────────────────────────────────────────┘
 *
 * Backend: GET /api/ai/pre-run/{workflow_id}
 *
 * Drop-in:
 *   <PreRunBanner workflowId={id} value={mode} onChange={setMode} />
 */

import { useEffect, useState } from 'react';
import { useEditorPreferences } from '../hooks/useEditorPreferences';

export type RunSafetyMode = 'live' | 'sample' | 'dry_run' | 'validate_only';

interface LastRun {
  status: string;
  completed_at: string | null;
  duration_ms: number;
  rows_processed: number;
  error_summary: string | null;
}

interface Estimate {
  avg_duration_ms: number;
  avg_rows: number;
  run_count: number;
  based_on_n: number;
}

interface CostEstimate {
  estimate_usd: number;
  estimate_range?: { min: number; max: number };
  cost_band: 'low' | 'medium' | 'high';
  confidence?: 'low' | 'medium' | 'high';
  method: string;
  factors?: {
    rows: number;
    duration_sec: number;
    complexity: 'low' | 'medium' | 'high';
    duration_multiplier: number;
    based_on_runs: number;
  };
  based_on_avg_rows: number;
  based_on_avg_duration_ms: number;
  notes: string[];
}

interface PreRunResponse {
  workflow_id: string;
  last_run: LastRun | null;
  estimated: Estimate | null;
  cost_estimate: CostEstimate | null;
  safety_modes: string[];
}

// Schema-drift preview (2026-05-27). Mirrors DriftPreviewSink /
// DriftPreviewResponse from backend/fpulse/api/schema_history.py.
// Kept inline rather than imported because the banner is the only
// consumer in v1.
interface DriftPreviewChange {
  kind: 'added' | 'dropped' | 'type_changed' | 'nullable_changed';
  column: string;
  from_type?: string | null;
  to_type?: string | null;
  from_nullable?: boolean | null;
  to_nullable?: boolean | null;
  policy_action: 'apply_add' | 'apply_widen' | 'apply_force' | 'reject' | 'ignore';
}

interface DriftPreviewSink {
  step_id: string;
  step_label: string;
  sink_type: string;
  table_display_name: string;
  policy: string;
  ok: boolean;
  has_drift: boolean;
  severity: 'info' | 'warning' | 'critical';
  rejection_reason?: string | null;
  changes: DriftPreviewChange[];
}

interface DriftPreviewResponse {
  workflow_id: string;
  has_any_drift: boolean;
  has_any_rejection: boolean;
  sinks: DriftPreviewSink[];
}

interface PreRunBannerProps {
  workflowId: string;
  value?: RunSafetyMode;
  onChange?: (mode: RunSafetyMode) => void;
  // Pipeline lifecycle status. When the pipeline isn't 'published' the
  // 'live' mode is hidden because Live writes to the configured
  // destinations — running it from a Draft would side-effect against
  // real sinks before the pipeline has been reviewed/approved.
  pipelineStatus?: string;
}

const STATUS_TONE: Record<string, string> = {
  success: 'text-emerald-700',
  error: 'text-red-700',
  cancelled: 'text-slate-600',
  running: 'text-blue-700',
};

const MODE_LABELS: Record<RunSafetyMode, string> = {
  live: 'Live',
  sample: 'Sample',
  dry_run: 'Dry-run',
  validate_only: 'Validate-only',
};

const MODE_TIPS: Record<RunSafetyMode, string> = {
  live: 'Run on full upstream data and write to the configured destinations.',
  sample: 'Run on the first 100 rows only. No effect on destinations.',
  dry_run: 'Plan only — validate the IR and produce previews without writing.',
  validate_only: 'Schema + connection sanity check. No execution.',
};

function formatDuration(ms: number): string {
  if (!ms) return '—';
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

function formatTimeAgo(iso: string | null): string {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (!t) return '';
  const sec = Math.max(0, (Date.now() - t) / 1000);
  if (sec < 60) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
  return `${Math.round(sec / 86400)}d ago`;
}

export default function PreRunBanner({ workflowId, value, onChange, pipelineStatus }: PreRunBannerProps) {
  // May 3 2026: read the user's default safety mode via the shared
  // useEditorPreferences hook so changes in Settings → Default Run
  // Behavior reflect live (no page reload). When the parent passes a
  // controlled `value` we honor it; otherwise we use the saved
  // preference as the initial mode.
  const editorPrefs = useEditorPreferences();
  const liveAllowed = !pipelineStatus || pipelineStatus === 'published';
  const initialMode: RunSafetyMode = value ?? editorPrefs.defaultRunSafetyMode;
  const effectiveValue: RunSafetyMode = value ?? initialMode;
  const [data, setData] = useState<PreRunResponse | null>(null);
  const [loading, setLoading] = useState(true);
  // Schema-drift preview fetched alongside the pre-run summary. A null
  // value means "not loaded yet"; a response with sinks=[] means "no
  // managed-table sinks in this workflow" — either way we don't render
  // the drift panel.
  const [drift, setDrift] = useState<DriftPreviewResponse | null>(null);
  // Per-sink policy override for THIS run only. Keyed by step_id. Used
  // to let the operator pick "Allow all (warning)" once without
  // permanently changing the sink's saved schema_policy.
  // Sent to the executor via the run mutate API when present.
  const [policyOverrides, setPolicyOverrides] = useState<Record<string, string>>({});

  // On mount, if the parent didn't pass a controlled value AND the
  // user's preferred mode differs from the parent's default 'live',
  // notify the parent so the actual run uses the preferred mode (not
  // just the toggle UI).
  useEffect(() => {
    if (value === undefined && initialMode !== 'live' && onChange) {
      onChange(initialMode);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // When the user changes Settings → Default Run Behavior live (and
  // hasn't already overridden this banner's mode for this run), pick
  // up the new preference so the new default takes effect immediately.
  useEffect(() => {
    if (value === undefined && onChange) {
      onChange(editorPrefs.defaultRunSafetyMode);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editorPrefs.defaultRunSafetyMode]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const headers: Record<string, string> = {};
        const token = localStorage.getItem('fpulse_token') || '';
        const ws = localStorage.getItem('fpulse_workspace_id') || 'default';
        if (token) headers['Authorization'] = `Bearer ${token}`;
        headers['X-Workspace-Id'] = ws;
        const res = await fetch(`/api/ai/pre-run/${encodeURIComponent(workflowId)}`, {
          method: 'GET',
          headers,
        });
        if (!res.ok) throw new Error(String(res.status));
        const json = (await res.json()) as PreRunResponse;
        if (!cancelled) setData(json);
      } catch {
        if (!cancelled) setData(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workflowId]);

  // Drift preview fetched in parallel with the run summary. The endpoint
  // is cheap (preview-only — no sink writes), but a workflow with many
  // sinks could still take seconds. We fail open: a fetch error leaves
  // the panel hidden rather than blocking the run button.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const headers: Record<string, string> = {};
        const token = localStorage.getItem('fpulse_token') || '';
        const ws = localStorage.getItem('fpulse_workspace_id') || 'default';
        if (token) headers['Authorization'] = `Bearer ${token}`;
        headers['X-Workspace-Id'] = ws;
        const res = await fetch(
          `/api/schema/preview-drift/${encodeURIComponent(workflowId)}`,
          { method: 'GET', headers },
        );
        if (!res.ok) throw new Error(String(res.status));
        const json = (await res.json()) as DriftPreviewResponse;
        if (!cancelled) setDrift(json);
      } catch {
        if (!cancelled) setDrift(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workflowId]);

  // Drop 'live' for non-published pipelines (see liveAllowed above).
  const modes: RunSafetyMode[] = liveAllowed
    ? ['live', 'sample', 'dry_run', 'validate_only']
    : ['sample', 'dry_run', 'validate_only'];

  // If 'live' is the current effective mode but no longer allowed,
  // bump the parent to a safe default so the Run button doesn't fire a
  // Live execution against an unpublished pipeline.
  useEffect(() => {
    if (!liveAllowed && effectiveValue === 'live' && onChange) {
      onChange('sample');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveAllowed]);

  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-[12px] text-slate-700 space-y-1.5">
      {/* Last run */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs font-bold uppercase tracking-wider text-slate-500">Last run:</span>
        {loading ? (
          <span className="text-slate-400 italic">loading…</span>
        ) : data?.last_run ? (
          <>
            <span className={`font-semibold ${STATUS_TONE[data.last_run.status] || 'text-slate-700'}`}>
              {data.last_run.status === 'success' ? '✓' : data.last_run.status === 'error' ? '✗' : '•'}{' '}
              {data.last_run.status}
            </span>
            <span className="text-slate-500">·</span>
            <span>{formatDuration(data.last_run.duration_ms)}</span>
            {data.last_run.rows_processed > 0 && (
              <>
                <span className="text-slate-500">·</span>
                <span>{data.last_run.rows_processed.toLocaleString()} rows</span>
              </>
            )}
            <span className="text-slate-500">·</span>
            <span className="text-slate-500">{formatTimeAgo(data.last_run.completed_at)}</span>
            {data.last_run.error_summary && (
              <span className="text-red-600 font-mono text-xs ml-1 truncate" title={data.last_run.error_summary}>
                {data.last_run.error_summary}
              </span>
            )}
          </>
        ) : (
          <span className="text-slate-400 italic">never run</span>
        )}
      </div>

      {/* Estimate */}
      {data?.estimated && (
        <div className="flex items-center gap-2 flex-wrap text-slate-600">
          <span className="text-xs font-bold uppercase tracking-wider text-slate-500">Estimate:</span>
          <span>~{formatDuration(data.estimated.avg_duration_ms)}</span>
          {data.estimated.avg_rows > 0 && (
            <>
              <span className="text-slate-400">·</span>
              <span>~{data.estimated.avg_rows.toLocaleString()} rows</span>
            </>
          )}
          {data.cost_estimate && (
            <>
              <span className="text-slate-400">·</span>
              <span
                className={`px-1.5 py-0.5 rounded text-xs font-semibold ring-1 ${
                  data.cost_estimate.cost_band === 'high'
                    ? 'bg-red-50 text-red-700 ring-red-200'
                    : data.cost_estimate.cost_band === 'medium'
                    ? 'bg-amber-50 text-amber-800 ring-amber-200'
                    : 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                }`}
                title={[
                  `Method: ${data.cost_estimate.method}`,
                  data.cost_estimate.factors
                    ? `Rows: ${data.cost_estimate.factors.rows.toLocaleString()} · Duration: ${data.cost_estimate.factors.duration_sec}s · Complexity: ${data.cost_estimate.factors.complexity}`
                    : '',
                  ...data.cost_estimate.notes,
                ].filter(Boolean).join('\n')}
              >
                ~${data.cost_estimate.estimate_usd < 0.0001
                  ? data.cost_estimate.estimate_usd.toFixed(6)
                  : data.cost_estimate.estimate_usd.toFixed(4)}
                {data.cost_estimate.estimate_range && (
                  <span className="ml-1 opacity-70 font-normal text-[9px]">
                    (${data.cost_estimate.estimate_range.min.toFixed(4)}–${data.cost_estimate.estimate_range.max.toFixed(4)})
                  </span>
                )}
              </span>
              {data.cost_estimate.confidence && (
                <span
                  className={`text-[9px] uppercase tracking-wider font-bold ${
                    data.cost_estimate.confidence === 'high'
                      ? 'text-emerald-600'
                      : data.cost_estimate.confidence === 'medium'
                      ? 'text-amber-700'
                      : 'text-slate-500'
                  }`}
                  title={`Confidence ${data.cost_estimate.confidence} — based on ${data.cost_estimate.factors?.based_on_runs || 0} prior successful run(s)`}
                >
                  {data.cost_estimate.confidence} conf
                </span>
              )}
            </>
          )}
          <span className="text-slate-400 text-xs">
            (avg of {data.estimated.run_count} successful run{data.estimated.run_count === 1 ? '' : 's'})
          </span>
        </div>
      )}

      {/* Run safely toggle */}
      <div className="flex items-center gap-1.5 flex-wrap">
        <span className="text-xs font-bold uppercase tracking-wider text-slate-500 mr-1">Run safely:</span>
        {modes.map((m) => {
          const active = effectiveValue === m;
          return (
            <button
              key={m}
              type="button"
              onClick={() => onChange?.(m)}
              title={MODE_TIPS[m]}
              className={`px-2.5 py-0.5 rounded-md text-xs font-semibold transition-colors ${
                active
                  ? m === 'live'
                    ? 'bg-emerald-600 text-white'
                    : 'bg-amber-500 text-white'
                  : 'bg-white text-slate-600 ring-1 ring-slate-200 hover:bg-slate-100'
              }`}
            >
              {MODE_LABELS[m]}
            </button>
          );
        })}
      </div>

      {/* Schema-drift preview (2026-05-27).
          Renders only when the backend reports at least one sink with drift.
          A rejection turns the strip red ("Run will fail") so the operator
          notices BEFORE clicking Run. A drift-with-no-rejection turns it
          amber ("Run will modify X"). Per-sink overrides flow back into the
          run via policyOverrides — applied in the run-button click handler
          one layer up. */}
      {drift && drift.sinks.length > 0 && drift.has_any_drift && (
        <SchemaDriftPanel
          drift={drift}
          overrides={policyOverrides}
          onOverride={(stepId, policy) =>
            setPolicyOverrides((cur) => ({ ...cur, [stepId]: policy }))}
        />
      )}
    </div>
  );
}

// ── Schema drift panel ────────────────────────────────────────────────
//
// One row per sink that would drift on the next run. Renders the diff
// (added / dropped / type-changed columns) plus the policy's verdict
// ("This run will: add column email, widen id from INT to BIGINT").
// Per-run policy overrides live here so the operator can flip an
// individual sink without editing the saved pipeline.

const POLICY_LABELS: Record<string, string> = {
  strict: 'Strict',
  add_columns: 'Add columns',
  compatible: 'Compatible',
  allow_all_with_warning: 'Allow all (warning)',
};

function changeLine(c: DriftPreviewChange): string {
  switch (c.kind) {
    case 'added':
      return `+ ${c.column}${c.to_type ? ` (${c.to_type})` : ''}`;
    case 'dropped':
      return `− ${c.column}`;
    case 'type_changed':
      return `${c.column}: ${c.from_type ?? '?'} → ${c.to_type ?? '?'}`;
    case 'nullable_changed':
      return `${c.column}: nullability ${c.from_nullable ?? '?'} → ${c.to_nullable ?? '?'}`;
  }
}

function actionVerb(action: DriftPreviewChange['policy_action']): string {
  switch (action) {
    case 'apply_add': return 'add';
    case 'apply_widen': return 'widen';
    case 'apply_force': return 'force';
    case 'reject': return 'reject';
    case 'ignore': return 'ignore';
  }
}

function SchemaDriftPanel({
  drift,
  overrides,
  onOverride,
}: {
  drift: DriftPreviewResponse;
  overrides: Record<string, string>;
  onOverride: (stepId: string, policy: string) => void;
}) {
  // Strip tone tracks the worst severity across all sinks.
  const tone = drift.has_any_rejection
    ? { wrap: 'bg-red-50 border-red-200', label: 'text-red-700', accent: 'text-red-600' }
    : { wrap: 'bg-amber-50 border-amber-200', label: 'text-amber-800', accent: 'text-amber-700' };

  return (
    <div className={`rounded-lg border ${tone.wrap} px-3 py-2 text-xs space-y-1.5`}>
      <div className={`flex items-center gap-2 font-semibold ${tone.label}`}>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
          <line x1="12" y1="9" x2="12" y2="13" />
          <line x1="12" y1="17" x2="12.01" y2="17" />
        </svg>
        Schema drift detected
        <span className="text-slate-500 font-normal">
          · {drift.sinks.filter((s) => s.has_drift).length} sink
          {drift.sinks.filter((s) => s.has_drift).length === 1 ? '' : 's'}
        </span>
      </div>

      <div className="space-y-1.5">
        {drift.sinks.filter((s) => s.has_drift).map((sink) => {
          const effectivePolicy = overrides[sink.step_id] || sink.policy;
          return (
            <div key={sink.step_id} className="bg-white/60 border border-slate-200 rounded-md px-2 py-1.5 space-y-1">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-semibold text-slate-700 truncate">{sink.step_label}</span>
                <span className="text-slate-400">→</span>
                <span className="font-mono text-slate-600 truncate">{sink.table_display_name}</span>
                <span className="ml-auto" />
                <select
                  value={effectivePolicy}
                  onChange={(e) => onOverride(sink.step_id, e.target.value)}
                  className="text-xs bg-white border border-slate-200 rounded px-1.5 py-0.5 focus:outline-none focus:ring-2 focus:ring-pipe-300"
                  title="Override the saved schema policy for this run only"
                >
                  {Object.entries(POLICY_LABELS).map(([v, l]) => (
                    <option key={v} value={v}>{l}{v === sink.policy ? ' (saved)' : ''}</option>
                  ))}
                </select>
              </div>
              {sink.rejection_reason && (
                <div className="text-red-700 leading-tight">{sink.rejection_reason}</div>
              )}
              {sink.changes.length > 0 && (
                <ul className="text-slate-700 space-y-0.5">
                  {sink.changes.map((c, ci) => (
                    <li key={ci} className="flex items-center gap-1.5">
                      <span className="font-mono">{changeLine(c)}</span>
                      <span className={`text-[10px] uppercase tracking-wider ${
                        c.policy_action === 'reject' ? 'text-red-600'
                        : c.policy_action === 'apply_force' ? 'text-amber-700'
                        : 'text-emerald-600'
                      }`}>
                        ({actionVerb(c.policy_action)})
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </div>

      <div className={`text-[10px] ${tone.accent}`}>
        {drift.has_any_rejection
          ? 'At least one sink will fail. Switch to a more permissive policy above to allow the change.'
          : 'Review each change. Per-run overrides do NOT modify the saved pipeline.'}
      </div>
    </div>
  );
}
