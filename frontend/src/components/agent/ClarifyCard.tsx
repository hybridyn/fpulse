/**
 * ClarifyCard — Phase 3.1 (May 18 2026).
 *
 * Renders a structured clarification ask from /api/chat's clarify_first
 * response. Each question shows as a labelled block with its chip
 * options as one-click buttons. The user picks one chip per question
 * (or types into the free-text override field), then clicks Send —
 * the component builds a structured answer string and submits it as
 * the next chat message.
 *
 * Backend response shape (from planner.py `/api/chat`):
 *   {
 *     "intent": "clarify_first",
 *     "clarification": {
 *       "source_type": "oracle_bip",
 *       "sink_type": "sql_server",
 *       "detected_intent": "Build a pipeline from oracle_bip to sql_server",
 *       "questions": [
 *         { field, question, chips: string[], required: boolean },
 *         ...
 *       ]
 *     }
 *   }
 *
 * Trust contract: this is purely presentational. The submitted answer
 * string goes through the same /api/chat endpoint as any other chat
 * message — the backend's clarify_to_template bridge parses it,
 * populates the matched template, and the existing
 * ConfirmationCard flow takes over from there.
 */

import { useState } from 'react';

export interface ClarifyQuestion {
  field: string;
  question: string;
  chips: string[];
  required: boolean;
}

export interface ClarificationPayload {
  source_type: string | null;
  sink_type: string | null;
  detected_intent: string;
  question_count: number;
  questions: ClarifyQuestion[];
}

export interface ClarifyCardProps {
  /** Structured clarification data from /api/chat response. */
  payload: ClarificationPayload;
  /**
   * Called with the assembled answer string when the user clicks Send.
   * The chat panel posts this as the next user message; the backend
   * parses it via clarify_to_template.parse_answers_freeform().
   */
  onSubmit: (answerText: string) => void;
  /** Optional cancel — closes the card without sending. */
  onCancel?: () => void;
  busy?: boolean;
}

export default function ClarifyCard({ payload, onSubmit, onCancel, busy }: ClarifyCardProps) {
  // Per-question selected chip (index into chips[]). Null = not yet
  // answered. Optional questions can be left null and the user can
  // still submit; required questions block Send until answered.
  const [selected, setSelected] = useState<Record<string, number | null>>(() => {
    const init: Record<string, number | null> = {};
    for (const q of payload.questions) init[q.field] = null;
    return init;
  });
  // Free-text override per question — wins over the chip choice
  // when non-empty. Lets users supply values that aren't in the
  // chip list (table name, connection name, etc.).
  const [overrides, setOverrides] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const q of payload.questions) init[q.field] = '';
    return init;
  });

  const allRequiredAnswered = payload.questions.every(
    (q) => !q.required || selected[q.field] !== null || overrides[q.field].trim() !== '',
  );

  const handleSend = () => {
    if (!allRequiredAnswered || busy) return;
    // Assemble a structured answer string. Format: one line per question,
    // "field: value". This matches what clarify_to_template's regex
    // patterns extract well (synonyms inside the value win, and the
    // field-name marker improves connection/table extraction).
    const lines: string[] = [];
    for (const q of payload.questions) {
      const override = overrides[q.field].trim();
      if (override) {
        lines.push(`${q.field}: ${override}`);
        continue;
      }
      const idx = selected[q.field];
      if (idx !== null && idx !== undefined) {
        lines.push(`${q.field}: ${q.chips[idx]}`);
      }
    }
    onSubmit(lines.join('\n'));
  };

  return (
    <div className="rounded-2xl bg-white ring-1 ring-indigo-200 shadow-sm overflow-hidden">
      <div className="px-4 py-3 border-b border-indigo-100 bg-indigo-50/40">
        <div className="flex items-center gap-2">
          <span className="text-xs font-bold uppercase tracking-wider px-2 py-0.5 rounded-md bg-indigo-100 text-indigo-800">
            Clarify
          </span>
          <h3 className="text-sm font-semibold text-slate-800">
            {payload.detected_intent}
          </h3>
        </div>
        <p className="mt-1 text-xs text-slate-600">
          A few quick questions before I draft it — pick a chip or type your own answer.
        </p>
      </div>

      <div className="px-4 py-3 space-y-4">
        {payload.questions.map((q, i) => (
          <div key={q.field}>
            <div className="text-xs font-semibold text-slate-700 mb-1.5">
              Q{i + 1}. {q.question}
              {!q.required && (
                <span className="ml-1.5 text-[10px] font-normal text-slate-400 uppercase tracking-wide">
                  optional
                </span>
              )}
            </div>
            <div className="flex flex-wrap gap-1.5 mb-1.5">
              {q.chips.map((chip, ci) => {
                const isSelected = selected[q.field] === ci;
                return (
                  <button
                    key={ci}
                    type="button"
                    onClick={() => {
                      setSelected((s) => ({ ...s, [q.field]: ci }));
                      setOverrides((o) => ({ ...o, [q.field]: '' }));
                    }}
                    disabled={busy}
                    className={`px-2.5 py-1 text-xs rounded-md transition-colors disabled:opacity-50 ${
                      isSelected
                        ? 'bg-indigo-600 text-white border border-indigo-700'
                        : 'bg-slate-50 text-slate-700 border border-slate-200 hover:bg-slate-100'
                    }`}
                  >
                    {chip}
                  </button>
                );
              })}
            </div>
            <input
              type="text"
              value={overrides[q.field]}
              onChange={(e) => {
                setOverrides((o) => ({ ...o, [q.field]: e.target.value }));
                if (e.target.value.trim()) {
                  setSelected((s) => ({ ...s, [q.field]: null }));
                }
              }}
              placeholder="Or type your own answer…"
              disabled={busy}
              className="w-full text-xs px-2 py-1 rounded-md border border-slate-200 focus:border-indigo-400 focus:outline-none focus:ring-1 focus:ring-indigo-200 disabled:opacity-50"
            />
          </div>
        ))}
      </div>

      <div className="px-4 py-3 bg-slate-50 border-t border-slate-100 flex items-center justify-end gap-2">
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-200 rounded-lg transition-colors disabled:opacity-50"
          >
            Cancel
          </button>
        )}
        <button
          type="button"
          onClick={handleSend}
          disabled={!allRequiredAnswered || busy}
          className="px-3 py-1.5 text-xs font-semibold text-white rounded-lg transition-colors disabled:opacity-50 bg-indigo-600 hover:bg-indigo-700"
        >
          {busy ? 'Sending…' : 'Send answers'}
        </button>
      </div>
    </div>
  );
}
