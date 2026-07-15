/**
 * DiffPreview — shows the structured diff for an AI-drafted pipeline change
 * inside the ConfirmationCard. Backs the "I can see exactly what will
 * change before I click Confirm" UX guarantee that Review #2 called out
 * as the missing piece for trust.
 *
 * Loads on mount from /api/ai/agent/draft/{draft_id}/preview. Render
 * shape:
 *   ┌──────────────────────────────────────────────────────────────┐
 *   │ +3 steps    -1 step    ~2 modified                           │
 *   │ +4 connections                                               │
 *   ├──────────────────────────────────────────────────────────────┤
 *   │ + Filter on customer_id (data_quality)                       │
 *   │ - Old API source (api_source)                                │
 *   │ ~ Postgres sink — changed: connection_id, schema             │
 *   │ + nightly_ingest → filter_step                               │
 *   └──────────────────────────────────────────────────────────────┘
 *
 * For NEW pipelines (no before_ir), shows a collapsible summary by
 * default — "Will create N steps; expand to verify each one." Modifications
 * to existing pipelines render expanded by default because the user is
 * usually focused on the precise changes.
 */

import { useEffect, useState } from 'react';

type StepChange = {
  kind: 'add' | 'remove' | 'modify';
  step_id: string;
  step_type: string;
  label: string;
  changed_param_keys: string[];
};

type ConnectionChange = {
  kind: 'add' | 'remove';
  from_step: string;
  to_step: string;
};

type DraftDiff = {
  is_new_pipeline: boolean;
  steps_added: number;
  steps_removed: number;
  steps_modified: number;
  connections_added: number;
  connections_removed: number;
  step_changes: StepChange[];
  connection_changes: ConnectionChange[];
};

type PreviewResponse = {
  draft_id: string;
  target_pipeline_id: string | null;
  is_modification: boolean;
  after_pipeline_name: string;
  diff: DraftDiff;
};

export interface DiffPreviewProps {
  draftId: string;
  /** Optional callback so the parent can render a "loading…" skeleton elsewhere. */
  onLoaded?: (preview: PreviewResponse) => void;
}

const KIND_PREFIX: Record<StepChange['kind'], string> = {
  add: '+',
  remove: '−',
  modify: '~',
};

const KIND_COLORS: Record<StepChange['kind'], string> = {
  add: 'text-emerald-700 bg-emerald-50',
  remove: 'text-red-700 bg-red-50',
  modify: 'text-amber-700 bg-amber-50',
};

export default function DiffPreview({ draftId, onLoaded }: DiffPreviewProps) {
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Expanded by default for modifications (small, focused). Collapsed
  // for new pipelines (could be 5-20 steps; user can verify by clicking
  // "Show all" or just trust the headline counts).
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    setPreview(null);
    fetch(`/api/ai/agent/draft/${encodeURIComponent(draftId)}/preview`, {
      headers: { 'Content-Type': 'application/json' },
    })
      .then(async (r) => {
        if (!r.ok) {
          const detail = await r.json().catch(() => ({ detail: r.statusText }));
          throw new Error(detail?.detail || `Diff fetch failed: ${r.status}`);
        }
        return r.json() as Promise<PreviewResponse>;
      })
      .then((p) => {
        if (cancelled) return;
        setPreview(p);
        setExpanded(p.is_modification);
        onLoaded?.(p);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : 'Failed to load diff preview');
      });
    return () => {
      cancelled = true;
    };
  }, [draftId, onLoaded]);

  if (error) {
    return (
      <div className="text-xs px-3 py-2 rounded-lg bg-red-50 text-red-800 border border-red-200">
        Couldn't load diff preview: {error}
      </div>
    );
  }

  if (!preview) {
    return (
      <div className="text-xs px-3 py-2 rounded-lg bg-slate-50 text-slate-500 border border-slate-200">
        Loading change preview…
      </div>
    );
  }

  const d = preview.diff;
  const totalChanges =
    d.steps_added + d.steps_removed + d.steps_modified +
    d.connections_added + d.connections_removed;

  if (totalChanges === 0) {
    return (
      <div className="text-xs px-3 py-2 rounded-lg bg-slate-50 text-slate-600 border border-slate-200">
        No changes — the draft matches the existing pipeline exactly.
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-slate-200 overflow-hidden bg-white">
      {/* Headline counts */}
      <div className="px-3 py-2 bg-slate-50 border-b border-slate-200 flex flex-wrap items-center gap-2 text-xs">
        {d.is_new_pipeline ? (
          <span className="font-semibold text-slate-700">
            New pipeline — {d.steps_added} steps, {d.connections_added} connections
          </span>
        ) : (
          <>
            {d.steps_added > 0 && (
              <span className="px-2 py-0.5 rounded-md bg-emerald-100 text-emerald-800 font-semibold">+{d.steps_added} step{d.steps_added !== 1 ? 's' : ''}</span>
            )}
            {d.steps_removed > 0 && (
              <span className="px-2 py-0.5 rounded-md bg-red-100 text-red-800 font-semibold">−{d.steps_removed} step{d.steps_removed !== 1 ? 's' : ''}</span>
            )}
            {d.steps_modified > 0 && (
              <span className="px-2 py-0.5 rounded-md bg-amber-100 text-amber-800 font-semibold">~{d.steps_modified} modified</span>
            )}
            {(d.connections_added + d.connections_removed) > 0 && (
              <span className="px-2 py-0.5 rounded-md bg-slate-200 text-slate-700 font-semibold">
                {d.connections_added > 0 ? `+${d.connections_added}` : ''}
                {d.connections_removed > 0 ? ` −${d.connections_removed}` : ''} edges
              </span>
            )}
          </>
        )}
        <button
          type="button"
          className="ml-auto text-xs text-indigo-700 hover:underline"
          onClick={() => setExpanded((x) => !x)}
        >
          {expanded ? 'Hide details' : 'Show details'}
        </button>
      </div>

      {/* Per-step / per-edge list — collapsible */}
      {expanded && (
        <div className="divide-y divide-slate-100">
          {d.step_changes.map((c, i) => (
            <div key={`s-${i}`} className="px-3 py-2 flex items-start gap-2 text-xs">
              <span className={`inline-flex items-center justify-center w-5 h-5 rounded font-bold ${KIND_COLORS[c.kind]}`}>
                {KIND_PREFIX[c.kind]}
              </span>
              <span className="flex-1 min-w-0">
                <span className="font-medium text-slate-800">{c.label}</span>
                <span className="text-slate-500"> · {c.step_type}</span>
                {c.changed_param_keys.length > 0 && (
                  <span className="text-amber-700"> — changed: {c.changed_param_keys.join(', ')}</span>
                )}
              </span>
            </div>
          ))}
          {d.connection_changes.map((c, i) => (
            <div key={`c-${i}`} className="px-3 py-1.5 flex items-start gap-2 text-xs text-slate-500">
              <span className={`inline-flex items-center justify-center w-5 h-5 rounded font-bold ${c.kind === 'add' ? 'text-emerald-700 bg-emerald-50' : 'text-red-700 bg-red-50'}`}>
                {c.kind === 'add' ? '+' : '−'}
              </span>
              <span>
                edge: {c.from_step} → {c.to_step}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
