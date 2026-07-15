/**
 * PrePublishCard — 7-section review card shown immediately before
 * Submit-for-Review or Deploy. Step 4 of the F-Pulse AI completion arc.
 *
 * Sections:
 *   1. Change diff against last successful publish
 *   2. Inventory (nodes / connections / alerts)
 *   3. Approval plan
 *   4. Security posture
 *   5. Severity summary (blocker / warning / info)
 *   6. SHA-256 snapshot hash
 *   7. Risk level badge
 *
 * Backend: POST /api/ai/pre-publish/{workflow_id}?target_env=prod
 *
 * Drop-in usage:
 *   <PrePublishCard
 *     workflowId={id}
 *     targetEnv="prod"
 *     onConfirm={(hash) => submitForReview(id, hash)}
 *     onCancel={() => setOpen(false)}
 *   />
 */

import { useEffect, useState } from 'react';

interface SeverityItem {
  severity: 'blocker' | 'warning' | 'info';
  section: string;
  message: string;
}

interface PrePublishResponse {
  workflow_id: string;
  workflow_name: string;
  target_environment: 'dev' | 'prod';
  snapshot_hash: string;
  risk_level: 'low' | 'medium' | 'high';
  sections: {
    change_diff: {
      has_baseline: boolean;
      summary: string;
      added_steps: string[];
      removed_steps: string[];
      modified_steps: string[];
      baseline_version?: number;
    };
    inventory: {
      node_count: number;
      connection_count: number;
      nodes_by_type: Record<string, number>;
      node_names: string[];
    };
    approval_plan: {
      required: boolean;
      gates: string[];
      approvers: string[];
      summary: string;
    };
    security_posture: {
      secrets_safe: boolean;
      connections_present: boolean;
      alerts_wired: boolean;
      issues: string[];
    };
  };
  severity_summary: { blocker: number; warning: number; info: number };
  blockers: SeverityItem[];
  warnings: SeverityItem[];
  infos: SeverityItem[];
}

interface PrePublishCardProps {
  workflowId: string;
  targetEnv?: 'dev' | 'prod';
  onConfirm?: (snapshotHash: string) => void;
  onCancel?: () => void;
  /** Fires once the card has fetched its data — exposes the SHA-256
   *  snapshot hash so a parent flow (e.g. PlanModal) can persist it
   *  alongside the approval submission. Called with `null` if the
   *  fetch fails. */
  onHashCaptured?: (snapshotHash: string | null) => void;
}

const RISK_TONES: Record<string, string> = {
  low: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  medium: 'bg-amber-50 text-amber-800 ring-amber-200',
  high: 'bg-red-50 text-red-700 ring-red-200',
};

const SEVERITY_ICON: Record<string, string> = {
  blocker: '🚫',
  warning: '⚠️',
  info: 'ℹ️',
};

export default function PrePublishCard({
  workflowId,
  targetEnv = 'prod',
  onConfirm,
  onCancel,
  onHashCaptured,
}: PrePublishCardProps) {
  const [data, setData] = useState<PrePublishResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const headers: Record<string, string> = { 'Content-Type': 'application/json' };
        const token = localStorage.getItem('fpulse_token') || '';
        const ws = localStorage.getItem('fpulse_workspace_id') || 'default';
        if (token) headers['Authorization'] = `Bearer ${token}`;
        headers['X-Workspace-Id'] = ws;
        const res = await fetch(
          `/api/ai/pre-publish/${encodeURIComponent(workflowId)}?target_env=${encodeURIComponent(targetEnv)}`,
          { method: 'POST', headers },
        );
        if (!res.ok) {
          const body = await res.json().catch(() => ({ detail: res.statusText }));
          throw new Error(body.detail || `HTTP ${res.status}`);
        }
        const json = (await res.json()) as PrePublishResponse;
        if (!cancelled) {
          setData(json);
          onHashCaptured?.(json.snapshot_hash);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : 'Failed to build preview');
          onHashCaptured?.(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workflowId, targetEnv]);

  if (loading) {
    return (
      <div className="rounded-xl border border-slate-200 bg-white p-4 text-sm text-slate-600">
        Building pre-publish preview…
      </div>
    );
  }
  if (error || !data) {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
        {error || 'No preview available.'}
        {onCancel && (
          <button
            onClick={onCancel}
            className="ml-3 px-2 py-0.5 rounded bg-red-100 hover:bg-red-200 text-red-800 text-xs"
          >
            Close
          </button>
        )}
      </div>
    );
  }

  const { sections } = data;
  const hasBlockers = data.blockers.length > 0;

  return (
    <div className="rounded-2xl border border-slate-200 bg-white shadow-lg overflow-hidden">
      {/* Header */}
      <div className="px-5 py-3 border-b border-slate-100 bg-gradient-to-r from-slate-50 to-white flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-bold text-slate-800 truncate">
            Pre-publish review · {data.workflow_name}
          </div>
          <div className="text-xs text-slate-500 mt-0.5">
            Target env:{' '}
            <code className="font-mono bg-slate-100 px-1 rounded">{data.target_environment}</code>
            {' · '}Snapshot{' '}
            <code className="font-mono bg-slate-100 px-1 rounded text-xs" title={data.snapshot_hash}>
              {data.snapshot_hash.slice(0, 12)}…
            </code>
          </div>
        </div>
        <span
          className={`shrink-0 px-2.5 py-1 rounded-full text-xs uppercase tracking-wider font-bold ring-1 ${
            RISK_TONES[data.risk_level] || RISK_TONES.low
          }`}
        >
          Risk: {data.risk_level}
        </span>
      </div>

      <div className="p-5 space-y-4 max-h-[60vh] overflow-y-auto">
        {/* Severity summary */}
        <div className="flex flex-wrap gap-2">
          {data.severity_summary.blocker > 0 && (
            <span className="px-2 py-1 rounded text-xs font-semibold bg-red-50 text-red-700 ring-1 ring-red-200">
              {data.severity_summary.blocker} blocker{data.severity_summary.blocker === 1 ? '' : 's'}
            </span>
          )}
          {data.severity_summary.warning > 0 && (
            <span className="px-2 py-1 rounded text-xs font-semibold bg-amber-50 text-amber-800 ring-1 ring-amber-200">
              {data.severity_summary.warning} warning{data.severity_summary.warning === 1 ? '' : 's'}
            </span>
          )}
          {data.severity_summary.info > 0 && (
            <span className="px-2 py-1 rounded text-xs font-semibold bg-slate-100 text-slate-700 ring-1 ring-slate-200">
              {data.severity_summary.info} info
            </span>
          )}
          {data.severity_summary.blocker === 0 &&
            data.severity_summary.warning === 0 &&
            data.severity_summary.info === 0 && (
              <span className="px-2 py-1 rounded text-xs font-semibold bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200">
                ✓ No issues detected
              </span>
            )}
        </div>

        {[...data.blockers, ...data.warnings, ...data.infos].map((s, i) => (
          <div
            key={i}
            className={`text-[12px] px-3 py-2 rounded-lg ring-1 ${
              s.severity === 'blocker'
                ? 'bg-red-50 ring-red-200 text-red-800'
                : s.severity === 'warning'
                ? 'bg-amber-50 ring-amber-200 text-amber-900'
                : 'bg-slate-50 ring-slate-200 text-slate-700'
            }`}
          >
            <span className="mr-1">{SEVERITY_ICON[s.severity]}</span>
            <span className="font-semibold uppercase tracking-wider text-xs mr-1">{s.section}:</span>
            {s.message}
          </div>
        ))}

        {/* Section 1 — Change diff */}
        <Section title="Change diff">
          <div className="text-[12px] text-slate-700 mb-1">{sections.change_diff.summary}</div>
          {sections.change_diff.has_baseline && (
            <div className="text-xs text-slate-500">
              vs. deployed v{sections.change_diff.baseline_version}: +{sections.change_diff.added_steps.length} added,
              -{sections.change_diff.removed_steps.length} removed, ~{sections.change_diff.modified_steps.length} modified
            </div>
          )}
        </Section>

        {/* Section 2 — Inventory */}
        <Section title="Inventory">
          <div className="text-[12px] text-slate-700">
            <strong>{sections.inventory.node_count}</strong> nodes ·{' '}
            <strong>{sections.inventory.connection_count}</strong> connections
          </div>
          <div className="text-xs text-slate-500 mt-1">
            {Object.entries(sections.inventory.nodes_by_type).map(([t, n]) => (
              <span key={t} className="inline-block mr-2">
                <code className="bg-slate-100 px-1 rounded">{t}</code> ×{n}
              </span>
            ))}
          </div>
        </Section>

        {/* Section 3 — Approval plan */}
        <Section title="Approval plan">
          <div className="text-[12px] text-slate-700">{sections.approval_plan.summary}</div>
          {sections.approval_plan.required && (
            <div className="text-xs text-slate-500 mt-1">
              Gates: {sections.approval_plan.gates.join(', ')} · Approvers:{' '}
              {sections.approval_plan.approvers.join(', ')}
            </div>
          )}
        </Section>

        {/* Section 4 — Security posture */}
        <Section title="Security posture">
          <div className="text-[12px] space-y-0.5">
            <Check ok={sections.security_posture.secrets_safe} label="No inline credentials" />
            <Check ok={sections.security_posture.connections_present} label="Connections present" />
            <Check ok={sections.security_posture.alerts_wired} label="Alerts wired" />
          </div>
          {sections.security_posture.issues.length > 0 && (
            <ul className="mt-2 text-xs text-red-700 list-disc list-inside">
              {sections.security_posture.issues.map((i, idx) => (
                <li key={idx}>{i}</li>
              ))}
            </ul>
          )}
        </Section>

        {/* Section 6 — Snapshot hash */}
        <Section title="Approval snapshot (SHA-256)">
          <code className="text-xs font-mono break-all text-slate-600 bg-slate-50 p-2 rounded block">
            {data.snapshot_hash}
          </code>
          <div className="text-xs text-slate-500 mt-1">
            This hash uniquely identifies the IR you're publishing. Reviewers can verify the
            deployed version matches the one you approved.
          </div>
        </Section>
      </div>

      {/* Footer */}
      <div className="px-5 py-3 border-t border-slate-100 bg-slate-50 flex items-center justify-end gap-2">
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="px-3 py-1.5 text-xs font-semibold text-slate-700 bg-white hover:bg-slate-100 rounded-md ring-1 ring-slate-200"
          >
            Cancel
          </button>
        )}
        {onConfirm && (
          <button
            type="button"
            disabled={hasBlockers}
            onClick={() => onConfirm(data.snapshot_hash)}
            className={`px-3 py-1.5 text-xs font-semibold rounded-md ${
              hasBlockers
                ? 'bg-slate-200 text-slate-400 cursor-not-allowed'
                : 'bg-indigo-600 text-white hover:bg-indigo-700'
            }`}
            title={hasBlockers ? 'Resolve blockers before publishing' : ''}
          >
            {data.target_environment === 'prod' ? 'Submit for review' : 'Publish'}
          </button>
        )}
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-t border-slate-100 pt-3">
      <div className="text-xs uppercase tracking-wider font-bold text-slate-500 mb-1.5">{title}</div>
      {children}
    </div>
  );
}

function Check({ ok, label }: { ok: boolean; label: string }) {
  return (
    <div className={`flex items-center gap-2 ${ok ? 'text-emerald-700' : 'text-red-700'}`}>
      <span className="font-bold">{ok ? '✓' : '✗'}</span>
      <span>{label}</span>
    </div>
  );
}
