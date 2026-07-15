import { useEffect, useState } from 'react';
import { api } from '../api/client';
import PrePublishCard from './PrePublishCard';

/**
 * Plan-stage modal — surfaces the structured diff a save / submit /
 * deploy will produce, alongside validator output and a recent-runs
 * baseline. Built from the prod-readiness roadmap item #2.
 *
 * Memory shape: a single fetch on open, single state object held until
 * the modal closes. No polling, no streaming subscriptions.
 */
interface PlanModalProps {
  open: boolean;
  onClose: () => void;
  workflowId: string;
  workflowName?: string;
  /** What baseline to compare against. `latest` = save preview;
   *  `deployed` = submit/deploy preview. */
  against: 'latest' | 'deployed';
  /** Label for the green primary action (e.g. "Submit for Review"). */
  confirmLabel: string;
  /** Called when the user clicks the primary action. The modal stays
   *  open so the caller can run its own follow-up (toast, refetch).
   *  When the AI PrePublishCard rendered (against === 'deployed'),
   *  the user-visible SHA-256 snapshot hash is forwarded so the caller
   *  can persist it with the approval submission. */
  onConfirm: (snapshotHash?: string) => void;
  /** When true, primary action is disabled — used after submit fires. */
  busy?: boolean;
}

type PlanResponse = {
  workflow_id: string;
  baseline_kind: string;
  baseline_version: number;
  current_version: number;
  deployed_version: number | null;
  current_hash: string;
  proposed_hash: string;
  hash_changed: boolean;
  diff: {
    steps: {
      added: Array<{ step_id: string; type: string; name: string }>;
      removed: Array<{ step_id: string; type: string; name: string }>;
      modified: Array<{ step_id: string; type: string; name: string; fields: string[] }>;
      truncated: boolean;
    };
    connections: {
      added: Array<{ from_step: string; to_step: string }>;
      removed: Array<{ from_step: string; to_step: string }>;
      truncated: boolean;
    };
    connection_refs: {
      added: Array<{ id: string; name: string; type: string }>;
      removed: Array<{ id: string; name: string; type: string }>;
    };
    summary: {
      added_steps: number;
      removed_steps: number;
      modified_steps: number;
      added_connections: number;
      removed_connections: number;
      added_connection_refs: number;
      removed_connection_refs: number;
    };
  };
  validator: {
    errors: Array<{ step_id: string; message: string; severity: string }>;
    warnings: Array<{ step_id: string; message: string; severity: string }>;
  };
  baseline: {
    runs_analyzed: number;
    avg_duration_ms?: number;
    p95_duration_ms?: number;
    avg_rows_processed?: number;
    last_run_status?: string;
    last_run_at?: string;
  };
};

export function PlanModal({
  open, onClose, workflowId, workflowName, against, confirmLabel,
  onConfirm, busy,
}: PlanModalProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [openSection, setOpenSection] = useState<string | null>(null);
  // Captured from PrePublishCard so we can forward to onConfirm.
  const [snapshotHash, setSnapshotHash] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !workflowId) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      setPlan(null);
      try {
        const wf = await api.getWorkflow(workflowId);
        const result = await api.planWorkflow(workflowId, wf.workflow, against);
        if (!cancelled) setPlan(result);
      } catch (e: any) {
        if (!cancelled) setError(e?.message || 'Failed to compute plan');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [open, workflowId, against]);

  if (!open) return null;

  const sum = plan?.diff?.summary;
  const hasErrors = (plan?.validator?.errors?.length || 0) > 0;
  const noChanges = sum
    && !sum.added_steps && !sum.removed_steps && !sum.modified_steps
    && !sum.added_connections && !sum.removed_connections;

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 max-h-[88vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="px-6 py-4 border-b border-slate-200 flex items-center justify-between shrink-0">
          <div>
            <h2 className="text-sm font-bold text-slate-800">
              Review changes
              {workflowName && <span className="text-slate-400 font-normal"> &middot; {workflowName}</span>}
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Comparing against {against === 'deployed' ? 'currently deployed' : 'latest saved'} version
              {plan && ` (v${plan.baseline_version})`}
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
          </button>
        </div>

        {/* Body */}
        <div className="px-6 py-5 space-y-4 overflow-auto flex-1">
          {/* AI pre-publish review card — 7-section preview rendered above
              the existing diff/validator/baseline so reviewers see the
              security posture, severity summary, and SHA-256 snapshot hash
              before approving. against="deployed" implies prod target. */}
          {workflowId && against === 'deployed' && (
            <PrePublishCard
              workflowId={workflowId}
              targetEnv="prod"
              onHashCaptured={(hash) => setSnapshotHash(hash)}
            />
          )}
          {loading && (
            <div className="flex items-center justify-center py-8 text-xs text-slate-400">
              Computing plan…
            </div>
          )}
          {error && (
            <div className="bg-rose-50 border border-rose-200 rounded-lg px-3 py-2 text-xs text-rose-700">
              {error}
            </div>
          )}

          {plan && (
            <>
              {/* Hash pills */}
              <div className="flex items-center gap-2 text-xs">
                <HashPill label={`v${plan.baseline_version}`} hash={plan.current_hash} />
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-400"><polyline points="9 18 15 12 9 6" /></svg>
                <HashPill label="proposed" hash={plan.proposed_hash} accent={plan.hash_changed} />
                {!plan.hash_changed && (
                  <span className="text-slate-400">— identical content</span>
                )}
              </div>

              {/* Summary */}
              {sum && (
                <div className="grid grid-cols-3 gap-2 text-center">
                  <SummaryStat label="Added" value={sum.added_steps} tone="green" hint="steps" />
                  <SummaryStat label="Modified" value={sum.modified_steps} tone="amber" hint="steps" />
                  <SummaryStat label="Removed" value={sum.removed_steps} tone="rose" hint="steps" />
                </div>
              )}

              {noChanges && (
                <div className="bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-xs text-slate-500">
                  No structural changes versus baseline. {confirmLabel} will still record an action in the audit log.
                </div>
              )}

              {/* Validator */}
              {(plan.validator.errors.length > 0 || plan.validator.warnings.length > 0) && (
                <div>
                  <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">Validation</div>
                  <div className="space-y-1">
                    {plan.validator.errors.map((e, i) => (
                      <div key={`e${i}`} className="bg-rose-50 border border-rose-200 rounded px-2 py-1 text-xs text-rose-700">
                        <span className="font-mono text-xs text-rose-400 mr-1.5">{e.step_id || '·'}</span>
                        {e.message}
                      </div>
                    ))}
                    {plan.validator.warnings.map((w, i) => (
                      <div key={`w${i}`} className="bg-amber-50 border border-amber-200 rounded px-2 py-1 text-xs text-amber-700">
                        <span className="font-mono text-xs text-amber-400 mr-1.5">{w.step_id || '·'}</span>
                        {w.message}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Step diffs */}
              {plan.diff.steps.added.length > 0 && (
                <Section title={`Added steps (${plan.diff.steps.added.length})`} tone="green" sectionKey="added" openKey={openSection} setOpenKey={setOpenSection}>
                  {plan.diff.steps.added.map((s) => (
                    <DiffRow key={s.step_id} stepId={s.step_id} type={s.type} name={s.name} accent="green" />
                  ))}
                </Section>
              )}
              {plan.diff.steps.modified.length > 0 && (
                <Section title={`Modified steps (${plan.diff.steps.modified.length})`} tone="amber" sectionKey="modified" openKey={openSection} setOpenKey={setOpenSection}>
                  {plan.diff.steps.modified.map((s) => (
                    <DiffRow key={s.step_id} stepId={s.step_id} type={s.type} name={s.name} accent="amber" fields={s.fields} />
                  ))}
                </Section>
              )}
              {plan.diff.steps.removed.length > 0 && (
                <Section title={`Removed steps (${plan.diff.steps.removed.length})`} tone="rose" sectionKey="removed" openKey={openSection} setOpenKey={setOpenSection}>
                  {plan.diff.steps.removed.map((s) => (
                    <DiffRow key={s.step_id} stepId={s.step_id} type={s.type} name={s.name} accent="rose" />
                  ))}
                </Section>
              )}

              {/* Edge diffs */}
              {(plan.diff.connections.added.length > 0 || plan.diff.connections.removed.length > 0) && (
                <Section title={`Edge changes (${plan.diff.connections.added.length} added, ${plan.diff.connections.removed.length} removed)`} tone="slate" sectionKey="edges" openKey={openSection} setOpenKey={setOpenSection}>
                  {plan.diff.connections.added.map((c, i) => (
                    <div key={`ea${i}`} className="text-xs font-mono text-emerald-700">
                      + {c.from_step} → {c.to_step}
                    </div>
                  ))}
                  {plan.diff.connections.removed.map((c, i) => (
                    <div key={`er${i}`} className="text-xs font-mono text-rose-700">
                      − {c.from_step} → {c.to_step}
                    </div>
                  ))}
                </Section>
              )}

              {/* Connection refs */}
              {(plan.diff.connection_refs.added.length > 0 || plan.diff.connection_refs.removed.length > 0) && (
                <Section title={`Saved connections referenced (${plan.diff.connection_refs.added.length} added, ${plan.diff.connection_refs.removed.length} removed)`} tone="indigo" sectionKey="conns" openKey={openSection} setOpenKey={setOpenSection}>
                  {plan.diff.connection_refs.added.map((c) => (
                    <div key={`ca${c.id}`} className="text-xs text-emerald-700">
                      + {c.name} <span className="text-slate-400">({c.type})</span>
                    </div>
                  ))}
                  {plan.diff.connection_refs.removed.map((c) => (
                    <div key={`cr${c.id}`} className="text-xs text-rose-700">
                      − {c.name} <span className="text-slate-400">({c.type})</span>
                    </div>
                  ))}
                </Section>
              )}

              {/* Baseline */}
              {plan.baseline.runs_analyzed > 0 && (
                <div>
                  <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">
                    Recent runs ({plan.baseline.runs_analyzed} analyzed)
                  </div>
                  <div className="bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 grid grid-cols-3 gap-2 text-center">
                    <BaselineStat label="Avg duration" value={fmtMs(plan.baseline.avg_duration_ms)} />
                    <BaselineStat label="p95 duration" value={fmtMs(plan.baseline.p95_duration_ms)} />
                    <BaselineStat label="Avg rows" value={fmtRows(plan.baseline.avg_rows_processed)} />
                  </div>
                  {plan.baseline.last_run_status && (
                    <div className="text-xs text-slate-400 mt-1.5 text-center">
                      Last run {plan.baseline.last_run_status}
                      {plan.baseline.last_run_at && ` · ${new Date(plan.baseline.last_run_at).toLocaleString()}`}
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-slate-200 flex items-center justify-between gap-3 shrink-0">
          <div className="text-xs text-slate-400">
            {hasErrors && <span className="text-rose-600 font-semibold">Validation errors must be fixed before this can proceed.</span>}
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={onClose}
              className="px-4 py-2 text-xs font-semibold text-slate-600 hover:bg-slate-50 rounded-lg"
            >
              Cancel
            </button>
            <button
              onClick={() => onConfirm(snapshotHash || undefined)}
              disabled={loading || hasErrors || busy}
              className="px-5 py-2 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-500 rounded-lg disabled:opacity-50 transition-colors"
            >
              {busy ? 'Working…' : confirmLabel}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function HashPill({ label, hash, accent }: { label: string; hash: string; accent?: boolean }) {
  const short = (hash || '').slice(0, 8) || '—';
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md font-mono ${accent ? 'bg-indigo-50 border border-indigo-200 text-indigo-700' : 'bg-slate-50 border border-slate-200 text-slate-600'}`}>
      <span className="text-[9px] uppercase tracking-wide opacity-70">{label}</span>
      <span>{short}</span>
    </span>
  );
}

function SummaryStat({ label, value, tone, hint }: { label: string; value: number; tone: 'green' | 'amber' | 'rose'; hint: string }) {
  const colors = {
    green: 'bg-emerald-50 border-emerald-200 text-emerald-700',
    amber: 'bg-amber-50 border-amber-200 text-amber-700',
    rose: 'bg-rose-50 border-rose-200 text-rose-700',
  }[tone];
  return (
    <div className={`rounded-lg border px-3 py-2 ${colors}`}>
      <div className="text-lg font-bold">{value}</div>
      <div className="text-xs uppercase tracking-wide opacity-80">{label} {hint}</div>
    </div>
  );
}

function Section({
  title, tone, sectionKey, openKey, setOpenKey, children,
}: {
  title: string;
  tone: 'green' | 'amber' | 'rose' | 'slate' | 'indigo';
  sectionKey: string;
  openKey: string | null;
  setOpenKey: (k: string | null) => void;
  children: React.ReactNode;
}) {
  const isOpen = openKey === sectionKey;
  const headerColor = {
    green: 'text-emerald-700',
    amber: 'text-amber-700',
    rose: 'text-rose-700',
    slate: 'text-slate-700',
    indigo: 'text-indigo-700',
  }[tone];
  return (
    <div className="border border-slate-200 rounded-lg">
      <button
        onClick={() => setOpenKey(isOpen ? null : sectionKey)}
        className={`w-full flex items-center justify-between px-3 py-2 text-xs font-semibold ${headerColor}`}
      >
        <span>{title}</span>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ transform: isOpen ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s' }}>
          <polyline points="9 18 15 12 9 6" />
        </svg>
      </button>
      {isOpen && (
        <div className="px-3 pb-3 pt-1 space-y-1 border-t border-slate-200">
          {children}
        </div>
      )}
    </div>
  );
}

function DiffRow({ stepId, type, name, accent, fields }: { stepId: string; type: string; name: string; accent: 'green' | 'amber' | 'rose'; fields?: string[] }) {
  const sigil = { green: '+', amber: '~', rose: '−' }[accent];
  const tone = { green: 'text-emerald-700', amber: 'text-amber-700', rose: 'text-rose-700' }[accent];
  return (
    <div className="flex items-start gap-2 text-xs">
      <span className={`font-mono ${tone}`}>{sigil}</span>
      <div className="flex-1 min-w-0">
        <div className="text-slate-700 truncate">
          <span className="font-semibold">{name}</span>
          <span className="text-slate-400"> &middot; {type}</span>
        </div>
        {fields && fields.length > 0 && (
          <div className="text-xs text-slate-400 mt-0.5">
            Changed: {fields.join(', ')}
          </div>
        )}
      </div>
      <span className="text-[9px] text-slate-300 font-mono shrink-0">{stepId}</span>
    </div>
  );
}

function BaselineStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-sm font-bold text-slate-700">{value}</div>
      <div className="text-xs text-slate-400 uppercase tracking-wide">{label}</div>
    </div>
  );
}

function fmtMs(ms?: number): string {
  if (!ms) return '—';
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

function fmtRows(n?: number): string {
  if (!n) return '—';
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}
