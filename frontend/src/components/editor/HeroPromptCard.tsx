/**
 * HeroPromptCard — replaces the bare "Build your first pipeline" empty
 * state on the Editor canvas with a one-shot generator card.
 *
 * Submit pre-fills the Copilot dock with a Build instruction; the existing
 * agent loop handles draft_pipeline_from_intent → ConfirmationCard → apply.
 * No core agent changes; this is purely an additive UX surface.
 *
 * The four example chips cover common F-Pulse pipeline shapes so users
 * who don't know what to ask have one-click starting points.
 */

import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { askCopilot } from '../../hooks/useAgentChatStore';
import { useWorkflowStore } from '../../stores/workflowStore';
import { TEMPLATE_CATALOG } from '../../templates/catalog';

const EXAMPLES = [
  {
    label: 'CSV → filter → Parquet',
    prompt: 'Build a pipeline that reads sales.csv, filters rows where status="active", and writes the result to a Parquet file.',
  },
  {
    label: 'API poll → Slack alert',
    prompt: 'Build a pipeline that polls a REST API every hour, filters items where severity > 2, and posts a summary to Slack.',
  },
  {
    label: 'Postgres → S3',
    prompt: 'Build a pipeline that reads from a Postgres orders table, deduplicates by order_id, aggregates total revenue by region, and writes the result to S3 as Parquet.',
  },
  {
    label: 'Snowflake → BigQuery',
    prompt: 'Build a pipeline that reads from Snowflake, applies a SQL transform to compute monthly revenue per customer, and writes to a BigQuery table.',
  },
];

export default function HeroPromptCard() {
  const [intent, setIntent] = useState('');
  const useTemplate = useWorkflowStore((s) => s.useTemplate);
  const [tplOpen, setTplOpen] = useState(false);
  const tplRef = useRef<HTMLDivElement>(null);
  // Portal-positioned dropdown — pinned above the button using a
  // recomputed rect so it survives the card's overflow-hidden clip.
  const [tplAnchor, setTplAnchor] = useState<{ left: number; bottom: number; width: number } | null>(null);

  // Recompute the anchor rect whenever the dropdown opens (or window
  // resizes while it's open).
  useEffect(() => {
    if (!tplOpen) { setTplAnchor(null); return; }
    const measure = () => {
      const r = tplRef.current?.getBoundingClientRect();
      if (!r) return;
      setTplAnchor({ left: r.right, bottom: window.innerHeight - r.top, width: r.width });
    };
    measure();
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
    };
  }, [tplOpen]);

  // Close template dropdown on outside click. Uses the document
  // `pointerdown` capture phase so ReactFlow (which `stopPropagation`s
  // on the canvas pane) can't swallow the event before we see it.
  useEffect(() => {
    if (!tplOpen) return;
    const onDoc = (e: PointerEvent) => {
      const target = e.target as Node | null;
      const inAnchor = tplRef.current && target && tplRef.current.contains(target);
      // The portal popover is rendered into document.body — match by
      // a data-attribute so we don't dismiss when the user clicks
      // INSIDE the dropdown itself.
      const inPopover = target && (target as HTMLElement).closest?.('[data-template-popover="true"]');
      if (!inAnchor && !inPopover) {
        setTplOpen(false);
      }
    };
    document.addEventListener('pointerdown', onDoc, true);
    return () => document.removeEventListener('pointerdown', onDoc, true);
  }, [tplOpen]);

  const submit = (text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    // Frame the request so the agent picks draft_pipeline_from_intent —
    // BUT skip the prefix when the text already opens with a
    // build-intent verb. Without this guard, clicking a template chip
    // whose content starts with "Build a pipeline that …" produced
    // the awkward double prefix "Build a pipeline: Build a pipeline
    // that …" (user-reported 2026-05-17), which also slightly bloated
    // the tokens the fast-lane scored against.
    const lower = trimmed.toLowerCase();
    const alreadyFramed = /^(build|create|draft|make|design|construct|scaffold|generate)\b/.test(lower);
    const message = alreadyFramed ? trimmed : `Build a pipeline: ${trimmed}`;
    askCopilot(message);
  };

  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      submit(intent);
    }
  };

  return (
    <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
      <div className="pointer-events-auto w-full max-w-2xl mx-6">
        {/* Header */}
        <div className="text-center mb-5">
          <div className="inline-flex items-center gap-2 mb-3 px-3 py-1 rounded-full bg-gradient-to-r from-indigo-500/10 to-purple-500/10 ring-1 ring-indigo-200">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="text-indigo-600">
              <path d="m21.64 3.64-1.28-1.28a1.21 1.21 0 0 0-1.72 0L2.36 18.64a1.21 1.21 0 0 0 0 1.72l1.28 1.28a1.2 1.2 0 0 0 1.72 0L21.64 5.36a1.2 1.2 0 0 0 0-1.72" />
              <path d="m14 7 3 3" />
              <path d="M5 6v4" />
              <path d="M19 14v4" />
              <path d="M10 2v2" />
              <path d="M7 8H3" />
              <path d="M21 16h-4" />
              <path d="M11 3H9" />
            </svg>
            <span className="text-xs font-bold uppercase tracking-wider text-indigo-700">Copilot builder</span>
          </div>
          <h2 className="text-2xl font-bold text-slate-800 mb-1">Describe your pipeline</h2>
          <p className="text-sm text-slate-500">Tell F-Pulse what you want in plain English. The Copilot drafts it; you review and apply.</p>
        </div>

        {/* Prompt textarea — `overflow-hidden` keeps the rounded corners
            crisp on the textarea + footer. The template dropdown
            escapes this clip via a React portal mounted on document.body
            (see `tplOpen && createPortal(...)` below). */}
        <div className="rounded-2xl bg-white border border-slate-200 shadow-lg overflow-hidden">
          <textarea
            value={intent}
            onChange={(e) => setIntent(e.target.value)}
            onKeyDown={onKey}
            placeholder="e.g. Read sales.csv, filter to active accounts, aggregate by region, write to Parquet."
            rows={3}
            className="w-full px-4 py-3 text-sm text-slate-800 placeholder-slate-400 outline-none resize-none border-0"
            autoFocus
          />
          <div className="flex items-center justify-between gap-3 px-3 py-2 bg-slate-50 border-t border-slate-200">
            <span className="text-xs text-slate-500">
              Cmd / Ctrl + Enter to submit
            </span>
            <div className="flex items-center gap-2">
              {/* Template picker — dropdown of every built-in template
                  by name. Replaces the previous direct-load button which
                  silently picked simple_etl. Selecting an item loads
                  it onto the canvas via useTemplate(id). */}
              <div className="relative" ref={tplRef}>
                <button
                  type="button"
                  onClick={() => setTplOpen((o) => !o)}
                  className="px-3 py-1.5 text-[12px] font-semibold text-slate-700 bg-white hover:bg-slate-100 rounded-lg ring-1 ring-slate-200 transition-colors flex items-center gap-1.5"
                >
                  Use template
                  <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="text-slate-400">
                    <polyline points="6 9 12 15 18 9" />
                  </svg>
                </button>
                {tplOpen && tplAnchor && createPortal(
                  <div
                    data-template-popover="true"
                    className="fixed z-[100] w-[420px] max-h-[420px] overflow-y-auto rounded-lg border border-slate-200 bg-white shadow-lg"
                    style={{
                      // Pin the popover above the button: align its right
                      // edge to the button's right edge, sit it 6px above
                      // the button's top.
                      left: tplAnchor.left - 420,
                      bottom: tplAnchor.bottom + 6,
                    }}
                  >
                    <div className="px-3 py-2 text-xs font-bold uppercase tracking-wider text-slate-500 border-b border-slate-100 bg-slate-50">
                      Pick a template
                    </div>
                    <ul className="py-1">
                      {TEMPLATE_CATALOG.map((t) => (
                        <li key={t.id}>
                          <button
                            type="button"
                            onClick={() => { useTemplate(t.id); setTplOpen(false); }}
                            className="w-full text-left px-3 py-2 hover:bg-slate-50 flex items-start gap-2"
                          >
                            <span className={`mt-0.5 shrink-0 text-[10px] font-bold uppercase px-1.5 py-0.5 rounded ${
                              t.complexity === 'simple' ? 'bg-slate-100 text-slate-600' : 'bg-violet-100 text-violet-700'
                            }`}>
                              {t.complexity}
                            </span>
                            <div className="min-w-0 flex-1">
                              {/* Names + taglines wrap rather than truncate so
                                  longer titles like "Multi-source join → warehouse"
                                  stay legible. */}
                              <div className="text-sm font-semibold text-slate-800 leading-snug">{t.name}</div>
                              <div className="mt-0.5 text-xs text-slate-500 leading-snug">{t.tagline}</div>
                            </div>
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>,
                  document.body,
                )}
              </div>
              <button
                type="button"
                onClick={() => submit(intent)}
                disabled={!intent.trim()}
                className="px-4 py-1.5 text-[12px] font-bold text-white rounded-lg bg-gradient-to-r from-indigo-600 to-purple-600 hover:from-indigo-700 hover:to-purple-700 disabled:from-slate-300 disabled:to-slate-300 transition-all shadow-sm"
              >
                Generate →
              </button>
            </div>
          </div>
        </div>

        {/* Example chips — one-click starting points */}
        <div className="mt-4">
          <div className="text-xs font-bold uppercase tracking-wider text-slate-400 mb-2 text-center">
            or try one of these
          </div>
          <div className="flex flex-wrap gap-2 justify-center">
            {EXAMPLES.map((ex) => (
              <button
                key={ex.label}
                type="button"
                onClick={() => submit(ex.prompt)}
                className="px-3 py-1.5 text-[12px] font-medium text-slate-700 bg-white hover:bg-indigo-50 hover:text-indigo-700 hover:border-indigo-300 rounded-full ring-1 ring-slate-200 transition-colors"
                title={ex.prompt}
              >
                {ex.label}
              </button>
            ))}
          </div>
        </div>

        {/* Footer hint */}
        <div className="mt-5 text-center text-xs text-slate-400">
          Drafts are previewed before anything is saved · the Copilot never modifies your canvas without confirmation
        </div>
      </div>
    </div>
  );
}
