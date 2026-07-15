/**
 * ConfirmationCard — renders an agent's proposed action with explicit
 * Confirm / Cancel buttons. Per docs/TRUST.md Pillar 1: "AI suggests;
 * humans confirm. Nothing is written without an explicit confirmation."
 *
 * Step 1.5a scaffold. Step 1.5b governance wires this into the agent loop
 * so high-impact-write tools render the card BEFORE execution and only
 * proceed on user confirm. Today the card is purely presentational —
 * AgentChatPanel uses it to display tool results and let the user
 * acknowledge or reject the agent's plan.
 */

import { ReactNode } from 'react';
import DiffPreview from './DiffPreview';

export interface ConfirmationDetail {
  label: string;
  value: ReactNode;
}

export interface ConfirmationCardProps {
  /** Short title — e.g. "Create monthly metrics report". */
  title: string;
  /** One-sentence summary of what will happen. */
  summary: string;
  /** Tool tier — drives color + warning prefix. */
  tier: 'read' | 'safe_write' | 'high_impact_write' | 'unknown';
  /** Key/value pairs the agent wants the user to verify. */
  details?: ConfirmationDetail[];
  /** Optional warnings to surface above the confirm button. */
  warnings?: string[];
  /** Disable the Confirm button (e.g. while a parent component is busy). */
  busy?: boolean;
  /** Called when user clicks Confirm. */
  onConfirm: () => void;
  /** Called when user clicks Cancel. */
  onCancel: () => void;
  /** Optional label override for the Confirm button. Defaults to "Confirm". */
  confirmLabel?: string;
  /**
   * When set, render a DiffPreview block above the action buttons that
   * fetches GET /api/ai/agent/draft/{draftId}/preview. This is the
   * "AI suggests, humans confirm" trust surface — users see exactly
   * which steps will be added/removed/modified BEFORE clicking Confirm.
   * Only meaningful for safe_write / high_impact_write drafts.
   */
  draftId?: string;
}

const TIER_STYLES: Record<ConfirmationCardProps['tier'], { ring: string; badge: string; pill: string; label: string }> = {
  read: {
    ring: 'ring-slate-200',
    badge: 'bg-slate-100 text-slate-700',
    pill: 'bg-slate-50 text-slate-600',
    label: 'Read',
  },
  safe_write: {
    ring: 'ring-amber-200',
    badge: 'bg-amber-100 text-amber-800',
    pill: 'bg-amber-50 text-amber-700',
    label: 'Draft',
  },
  high_impact_write: {
    ring: 'ring-red-300',
    badge: 'bg-red-100 text-red-800',
    pill: 'bg-red-50 text-red-700',
    label: 'High impact',
  },
  unknown: {
    ring: 'ring-slate-200',
    badge: 'bg-slate-100 text-slate-700',
    pill: 'bg-slate-50 text-slate-600',
    label: 'Unknown',
  },
};

export default function ConfirmationCard(props: ConfirmationCardProps) {
  const styles = TIER_STYLES[props.tier];
  const isHighImpact = props.tier === 'high_impact_write';

  return (
    <div className={`rounded-2xl bg-white ring-1 ${styles.ring} shadow-sm overflow-hidden`}>
      <div className="px-4 py-3 border-b border-slate-100 flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className={`text-xs font-bold uppercase tracking-wider px-2 py-0.5 rounded-md ${styles.badge}`}>
              {styles.label}
            </span>
            <h3 className="text-sm font-semibold text-slate-800 truncate">{props.title}</h3>
          </div>
          <p className="mt-1 text-xs text-slate-600 leading-relaxed">{props.summary}</p>
        </div>
      </div>

      {props.details && props.details.length > 0 && (
        <div className="px-4 py-3 space-y-1.5">
          {props.details.map((d, i) => (
            <div key={i} className="flex items-start text-xs gap-3">
              <span className={`shrink-0 px-2 py-0.5 rounded-md text-xs uppercase tracking-wider font-semibold ${styles.pill}`}>
                {d.label}
              </span>
              <span className="text-slate-700 break-words">{d.value}</span>
            </div>
          ))}
        </div>
      )}

      {props.warnings && props.warnings.length > 0 && (
        <div className="px-4 py-2 border-t border-amber-100 bg-amber-50/50 space-y-1">
          {props.warnings.map((w, i) => (
            <div key={i} className="text-xs text-amber-800 flex items-start gap-1.5">
              <span aria-hidden>&#9888;</span>
              <span>{w}</span>
            </div>
          ))}
        </div>
      )}

      {props.draftId && (
        <div className="px-4 py-3 border-t border-slate-100">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-500">Change preview</div>
          <DiffPreview draftId={props.draftId} />
        </div>
      )}

      <div className="px-4 py-3 bg-slate-50 border-t border-slate-100 flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={props.onCancel}
          className="px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-200 rounded-lg transition-colors"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={props.onConfirm}
          disabled={props.busy}
          className={`px-3 py-1.5 text-xs font-semibold text-white rounded-lg transition-colors disabled:opacity-50 ${
            isHighImpact ? 'bg-red-600 hover:bg-red-700' : 'bg-indigo-600 hover:bg-indigo-700'
          }`}
        >
          {props.busy ? 'Working\u2026' : props.confirmLabel || 'Confirm'}
        </button>
      </div>
    </div>
  );
}
