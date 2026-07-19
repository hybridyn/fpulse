/**
 * ParametersDialog — declare typed pipeline parameters.
 *
 * Editor UI for `Workflow.parameters`. Users add / remove / edit typed
 * input variables that step params can reference via `${param.<name>}`.
 * Resolved by the backend at execution time from either the API caller's
 * `parameter_values` body OR the parameter's declared default.
 *
 * Persisted via the workflow store — `setParameters()` flips `isDirty=true`
 * so the next Save round-trips them to the server.
 */

import { useEffect, useState } from 'react';
import { useWorkflowStore, type WorkflowParameter } from '../stores/workflowStore';

interface ParametersDialogProps {
  open: boolean;
  onClose: () => void;
}

const TYPES: WorkflowParameter['type'][] = ['string', 'int', 'float', 'bool', 'json'];

const _NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

function _coerceDefault(raw: string, type: WorkflowParameter['type']): WorkflowParameter['default'] {
  if (raw === '') return null;
  if (type === 'int') return Number.isFinite(Number(raw)) ? parseInt(raw, 10) : raw;
  if (type === 'float') return Number.isFinite(Number(raw)) ? parseFloat(raw) : raw;
  if (type === 'bool') return raw.toLowerCase() === 'true';
  return raw;
}

export default function ParametersDialog({ open, onClose }: ParametersDialogProps) {
  const storeParams = useWorkflowStore((s) => s.parameters);
  const setParameters = useWorkflowStore((s) => s.setParameters);

  // Local draft state — committed to the store on Save. Lets the user
  // experiment without mutating store on every keystroke.
  const [draft, setDraft] = useState<WorkflowParameter[]>(storeParams);
  const [errors, setErrors] = useState<Record<number, string>>({});

  useEffect(() => {
    if (open) {
      setDraft(storeParams);
      setErrors({});
    }
  }, [open, storeParams]);

  if (!open) return null;

  const addRow = () => {
    setDraft((d) => [
      ...d,
      { name: '', type: 'string', default: null, description: '', required: false },
    ]);
  };

  const removeRow = (i: number) => {
    setDraft((d) => d.filter((_, idx) => idx !== i));
  };

  const updateRow = (i: number, patch: Partial<WorkflowParameter>) => {
    setDraft((d) => d.map((p, idx) => (idx === i ? { ...p, ...patch } : p)));
  };

  const validate = (): boolean => {
    const errs: Record<number, string> = {};
    const seen = new Set<string>();
    draft.forEach((p, i) => {
      if (!p.name) errs[i] = 'Name is required.';
      else if (!_NAME_RE.test(p.name)) errs[i] = 'Name must match [A-Za-z_][A-Za-z0-9_]*';
      else if (seen.has(p.name)) errs[i] = 'Duplicate name.';
      seen.add(p.name);
      if (p.required && (p.default === null || p.default === undefined || p.default === '')) {
        // It's fine for required params to have no default — caller MUST pass.
        // Skip — not an error.
      }
    });
    setErrors(errs);
    return Object.keys(errs).length === 0;
  };

  const handleSave = () => {
    if (!validate()) return;
    setParameters(draft);
    onClose();
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-white rounded-2xl shadow-2xl w-full max-w-3xl mx-4 max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between shrink-0">
          <div>
            <h2 className="text-sm font-bold text-slate-800">Pipeline parameters</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Typed inputs the user (or an API caller) can override per run. Reference inside any
              step's params with <code className="bg-slate-100 px-1 rounded">{'${param.name}'}</code>.
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
          </button>
        </div>

        {/* Body */}
        <div className="px-5 py-4 overflow-auto flex-1">
          {draft.length === 0 && (
            <div className="text-center py-10">
              <div className="text-slate-400 text-sm mb-3">No parameters declared.</div>
              <button
                type="button"
                onClick={addRow}
                className="px-4 py-2 text-xs font-semibold bg-indigo-600 text-white rounded-lg hover:bg-indigo-700"
              >
                + Add your first parameter
              </button>
            </div>
          )}

          {draft.length > 0 && (
            <table className="w-full text-[12px]">
              <thead className="text-slate-500 uppercase tracking-wider text-xs font-semibold">
                <tr>
                  <th className="text-left pb-2 w-1/4">Name</th>
                  <th className="text-left pb-2 w-1/6">Type</th>
                  <th className="text-left pb-2 w-1/4">Default</th>
                  <th className="text-left pb-2">Description</th>
                  <th className="text-center pb-2 w-[70px]">Required</th>
                  <th className="w-[24px]"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {draft.map((p, i) => (
                  <tr key={i} className="align-top">
                    <td className="py-2 pr-2">
                      <input
                        type="text"
                        value={p.name}
                        onChange={(e) => updateRow(i, { name: e.target.value })}
                        placeholder="param_name"
                        className="w-full px-2 py-1 font-mono text-[12px] rounded border border-slate-200 focus:border-indigo-400 focus:outline-none"
                      />
                      {errors[i] && (
                        <div className="text-xs text-red-600 mt-1">{errors[i]}</div>
                      )}
                    </td>
                    <td className="py-2 pr-2">
                      <select
                        value={p.type}
                        onChange={(e) => {
                          // Re-coerce the existing default to the new type rather
                          // than nulling it — the old `default: null` silently wiped
                          // a default the user had already typed, so the run later
                          // resolved ${param.x} to empty and failed cryptically.
                          const newType = e.target.value as WorkflowParameter['type'];
                          const cur = p.default;
                          const nextDefault =
                            cur === null || cur === undefined || cur === ''
                              ? cur
                              : _coerceDefault(String(cur), newType);
                          updateRow(i, { type: newType, default: nextDefault });
                        }}
                        className="w-full px-2 py-1 text-[12px] rounded border border-slate-200 bg-white"
                      >
                        {TYPES.map((t) => (
                          <option key={t} value={t}>{t}</option>
                        ))}
                      </select>
                    </td>
                    <td className="py-2 pr-2">
                      {p.type === 'bool' ? (
                        <select
                          value={p.default === true ? 'true' : 'false'}
                          onChange={(e) => updateRow(i, { default: e.target.value === 'true' })}
                          className="w-full px-2 py-1 text-[12px] rounded border border-slate-200 bg-white"
                        >
                          <option value="false">false</option>
                          <option value="true">true</option>
                        </select>
                      ) : (
                        <input
                          type="text"
                          value={p.default === null || p.default === undefined ? '' : String(p.default)}
                          onChange={(e) => updateRow(i, { default: _coerceDefault(e.target.value, p.type) })}
                          placeholder={p.type === 'json' ? '{"k": "v"}' : ''}
                          className="w-full px-2 py-1 font-mono text-[12px] rounded border border-slate-200 focus:border-indigo-400 focus:outline-none"
                        />
                      )}
                    </td>
                    <td className="py-2 pr-2">
                      <input
                        type="text"
                        value={p.description}
                        onChange={(e) => updateRow(i, { description: e.target.value })}
                        placeholder="What is this for?"
                        className="w-full px-2 py-1 text-[12px] rounded border border-slate-200 focus:border-indigo-400 focus:outline-none"
                      />
                    </td>
                    <td className="py-2 text-center">
                      <input
                        type="checkbox"
                        checked={p.required}
                        onChange={(e) => updateRow(i, { required: e.target.checked })}
                        className="accent-indigo-600"
                      />
                    </td>
                    <td className="py-2">
                      <button
                        type="button"
                        onClick={() => removeRow(i)}
                        className="text-slate-400 hover:text-red-600"
                        title="Remove"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                          <line x1="18" y1="6" x2="6" y2="18" />
                          <line x1="6" y1="6" x2="18" y2="18" />
                        </svg>
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {draft.length > 0 && (
            <button
              type="button"
              onClick={addRow}
              className="mt-3 px-3 py-1.5 text-xs font-semibold text-indigo-700 bg-indigo-50 hover:bg-indigo-100 rounded-md"
            >
              + Add parameter
            </button>
          )}
        </div>

        {/* Footer */}
        <div className="px-5 py-3 border-t border-slate-200 bg-slate-50 flex items-center justify-between gap-3 shrink-0">
          <div className="text-xs text-slate-500">
            Use in step params: <code className="bg-white px-1 py-0.5 rounded border border-slate-200 font-mono">{'${param.dataset}'}</code>
            {' '} or system: <code className="bg-white px-1 py-0.5 rounded border border-slate-200 font-mono">{'${utcnow:%Y-%m-%d}'}</code>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1.5 text-xs font-semibold text-slate-700 bg-white hover:bg-slate-100 rounded-md ring-1 ring-slate-200"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSave}
              className="px-4 py-1.5 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-md"
            >
              Apply
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
