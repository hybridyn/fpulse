/**
 * RunWithParametersDialog — modal UI for the "run with overrides" flow.
 *
 * Shown when the user clicks Run on a pipeline that declares parameters
 * (`workflow.parameters` non-empty). Pre-fills each input with the
 * declared default; user can edit, leave blank (uses default), or set a
 * fresh value. On confirm, calls the parent's `onRun(values)` which
 * passes them to `api.runWorkflow(id, fullRun, env, mode, parameter_values)`.
 *
 * If the pipeline has no parameters, callers should NOT mount this dialog
 * — they'd present an empty body. Render conditionally on
 * `workflow.parameters.length > 0`.
 */

import { useEffect, useState } from 'react';
import type { WorkflowParameter } from '../stores/workflowStore';

interface RunWithParametersDialogProps {
  open: boolean;
  onClose: () => void;
  workflowName: string;
  parameters: WorkflowParameter[];
  /** Called when the user clicks Run with the resolved values dict. */
  onRun: (parameterValues: Record<string, unknown>) => void;
  /** Truthy while the parent's run call is in flight — disables Run + shows spinner. */
  busy?: boolean;
}

function _coerce(raw: string, type: WorkflowParameter['type']): unknown {
  if (raw === '') return undefined; // Treat empty as "use default" — backend resolves.
  if (type === 'int') return Number.isFinite(Number(raw)) ? parseInt(raw, 10) : raw;
  if (type === 'float') return Number.isFinite(Number(raw)) ? parseFloat(raw) : raw;
  if (type === 'bool') return raw.toLowerCase() === 'true';
  if (type === 'json') {
    try {
      return JSON.parse(raw);
    } catch {
      return raw;  // Let backend fail with a clear error
    }
  }
  return raw;
}

export default function RunWithParametersDialog({
  open,
  onClose,
  workflowName,
  parameters,
  onRun,
  busy = false,
}: RunWithParametersDialogProps) {
  // Each row holds the user's typed string. Coerced to the declared type
  // only on submit so partial input (e.g. typing a number) doesn't blow up.
  const [values, setValues] = useState<Record<string, string>>({});
  const [touched, setTouched] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (open) {
      // Reset to declared defaults whenever the dialog opens.
      const seed: Record<string, string> = {};
      for (const p of parameters) {
        seed[p.name] = p.default === null || p.default === undefined ? '' : String(p.default);
      }
      setValues(seed);
      setTouched(new Set());
    }
  }, [open, parameters]);

  if (!open) return null;

  const missingRequired = parameters
    .filter((p) => p.required && !values[p.name])
    .map((p) => p.name);

  const canRun = missingRequired.length === 0;

  const handleRun = () => {
    if (!canRun || busy) {
      // Mark all required-missing as touched so the user sees the red.
      setTouched((s) => new Set([...s, ...missingRequired]));
      return;
    }
    // Build the final dict — only include keys the user actually changed
    // OR that have non-empty values. Keys absent from the dict fall through
    // to the parameter's declared default at the backend.
    const out: Record<string, unknown> = {};
    for (const p of parameters) {
      const raw = values[p.name];
      if (raw === '' || raw === undefined) continue;
      const coerced = _coerce(raw, p.type);
      if (coerced !== undefined) out[p.name] = coerced;
    }
    onRun(out);
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-white rounded-2xl shadow-2xl w-full max-w-xl mx-4 max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between shrink-0">
          <div>
            <h2 className="text-sm font-bold text-slate-800">Run with parameters</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              <code className="bg-slate-100 px-1 py-0.5 rounded font-mono">{workflowName}</code>
              {' '}declares {parameters.length} parameter{parameters.length === 1 ? '' : 's'}.
              Override below or accept the defaults.
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
          </button>
        </div>

        {/* Body */}
        <div className="px-5 py-4 overflow-auto flex-1 space-y-3">
          {parameters.map((p) => {
            const isMissing = p.required && !values[p.name] && touched.has(p.name);
            return (
              <div key={p.name}>
                <label className="block text-xs font-semibold text-slate-700 mb-1">
                  <code className="font-mono text-slate-800">{p.name}</code>
                  <span className="ml-2 text-xs text-slate-500 uppercase tracking-wider">{p.type}</span>
                  {p.required && (
                    <span className="ml-2 text-xs text-red-600 font-bold uppercase tracking-wider">Required</span>
                  )}
                </label>
                {p.description && (
                  <div className="text-xs text-slate-500 mb-1.5">{p.description}</div>
                )}
                {p.type === 'bool' ? (
                  <select
                    value={values[p.name] === 'true' ? 'true' : 'false'}
                    onChange={(e) => {
                      setValues((v) => ({ ...v, [p.name]: e.target.value }));
                      setTouched((s) => new Set([...s, p.name]));
                    }}
                    className="w-full px-3 py-1.5 text-[12px] rounded border border-slate-200 bg-white"
                  >
                    <option value="false">false</option>
                    <option value="true">true</option>
                  </select>
                ) : (
                  <input
                    type="text"
                    value={values[p.name] || ''}
                    onChange={(e) => {
                      setValues((v) => ({ ...v, [p.name]: e.target.value }));
                      setTouched((s) => new Set([...s, p.name]));
                    }}
                    placeholder={
                      p.default !== null && p.default !== undefined
                        ? `default: ${String(p.default)}`
                        : (p.type === 'json' ? '{"k": "v"}' : '')
                    }
                    className={`w-full px-3 py-1.5 font-mono text-[12px] rounded border focus:outline-none ${
                      isMissing
                        ? 'border-red-300 bg-red-50 focus:border-red-400'
                        : 'border-slate-200 focus:border-indigo-400'
                    }`}
                  />
                )}
                {isMissing && (
                  <div className="text-xs text-red-600 mt-0.5">Required parameter is empty.</div>
                )}
              </div>
            );
          })}

          {parameters.length === 0 && (
            <div className="text-center py-6 text-sm text-slate-500">
              This pipeline declares no parameters. The Run button works directly.
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-5 py-3 border-t border-slate-200 bg-slate-50 flex items-center justify-between gap-3 shrink-0">
          <div className="text-xs text-slate-500">
            Empty fields fall back to declared defaults. The resolved values are recorded
            on the execution log for audit.
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="px-3 py-1.5 text-xs font-semibold text-slate-700 bg-white hover:bg-slate-100 rounded-md ring-1 ring-slate-200 disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleRun}
              disabled={busy || !canRun}
              className="px-4 py-1.5 text-xs font-semibold text-white bg-emerald-600 hover:bg-emerald-700 rounded-md disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-1.5"
            >
              <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="5 3 19 12 5 21 5 3" /></svg>
              {busy ? 'Running…' : 'Run'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
