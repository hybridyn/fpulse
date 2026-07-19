/**
 * Unsaved-changes prompt shown when the user tries to leave the editor
 * with a dirty workflow. Three explicit choices — no guessing.
 *
 * Why this is its own component (not inline in Toolbar):
 *   - The same modal fires from the Close button AND from the browser's
 *     beforeunload path when App-level nav catches a mid-flight route
 *     change. Centralising it here avoids two subtly different modals.
 *   - Keyboard: Enter = Save & Close (primary), Esc = Cancel. Matches
 *     VS Code / Figma convention.
 */
import { useEffect, useRef } from 'react';

interface Props {
  open: boolean;
  /** true = "Save changes before closing" flow with Save/Discard/Cancel.
   *  false = plain "Close editor?" confirm with just Close/Cancel.
   *  Having one component handle both keeps keyboard shortcuts and
   *  focus management consistent — the only thing that changes is copy
   *  + whether the Discard button is rendered. */
  dirty: boolean;
  onSaveAndClose: () => void;
  onDiscard: () => void;
  onCancel: () => void;
  saving?: boolean;
  workflowName?: string;
  itemLabel?: string;
  returnLabel?: string;
  saveActionLabel?: string;
  closeActionLabel?: string;
}

export default function UnsavedChangesModal({
  open,
  dirty,
  onSaveAndClose,
  onDiscard,
  onCancel,
  saving = false,
  workflowName,
  itemLabel = 'pipeline',
  returnLabel = 'Workflows list',
  saveActionLabel = 'Save & Close',
  closeActionLabel = 'Close Editor',
}: Props) {
  const saveBtnRef = useRef<HTMLButtonElement>(null);

  // Autofocus the primary action so Enter commits the safe choice
  // (save), not the destructive one (discard).
  useEffect(() => {
    if (open && saveBtnRef.current) {
      saveBtnRef.current.focus();
    }
  }, [open]);

  // Keyboard shortcuts — Esc cancels; Enter triggers the primary
  // action (Save when dirty, Close when clean).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onCancel();
      } else if (e.key === 'Enter' && !saving) {
        e.preventDefault();
        if (dirty) onSaveAndClose();
        else onDiscard(); // "Close" path when there's nothing to save
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, saving, dirty, onCancel, onSaveAndClose, onDiscard]);

  if (!open) return null;

  return (
    <>
      <div className="fixed inset-0 bg-black/40 z-[100]" onClick={saving ? undefined : onCancel} />
      <div className="fixed inset-0 z-[101] flex items-center justify-center pointer-events-none">
        <div className="pointer-events-auto w-[440px] max-w-[95vw] bg-white rounded-2xl shadow-2xl border border-slate-200/60 overflow-hidden">
          {/* Header — copy + icon swap by dirty state.
              Dirty  → amber warning triangle ("you might lose work").
              Clean  → slate info circle ("just confirming"). */}
          <div className="px-6 pt-6 pb-4 flex items-start gap-3">
            <div className={`w-10 h-10 rounded-full flex items-center justify-center shrink-0 ${
              dirty ? 'bg-amber-100' : 'bg-slate-100'
            }`}>
              {dirty ? (
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#d97706" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                  <line x1="12" y1="9" x2="12" y2="13" />
                  <line x1="12" y1="17" x2="12.01" y2="17" />
                </svg>
              ) : (
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M18 6L6 18M6 6l12 12" />
                </svg>
              )}
            </div>
            <div className="flex-1 min-w-0">
              <h2 className="text-base font-bold text-slate-800 leading-tight">
                {dirty ? 'Save changes before closing?' : `Close this ${itemLabel}?`}
              </h2>
              <p className="text-xs text-slate-500 mt-1.5 leading-relaxed">
                {dirty ? (
                  <>
                    You have unsaved changes{workflowName ? ` in "${workflowName}"` : ''}. If you
                    discard them, they can't be recovered.
                  </>
                ) : (
                  <>
                    You'll be returned to {returnLabel}. You can reopen
                    {workflowName ? ` "${workflowName}"` : ` this ${itemLabel}`} any
                    time - nothing will be lost.
                  </>
                )}
              </p>
            </div>
          </div>

          {/* Actions — Discard button only renders when there's actually
              something to discard. Clean state collapses to a simple
              Close / Cancel pair. */}
          <div className="px-6 pb-5 pt-2 flex items-center justify-end gap-2">
            <button
              onClick={onCancel}
              disabled={saving}
              className="px-3.5 py-2 text-xs font-semibold text-slate-600 rounded-lg hover:bg-slate-100 transition-colors disabled:opacity-50"
            >
              Cancel
            </button>
            {dirty && (
              <button
                onClick={onDiscard}
                disabled={saving}
                className="px-3.5 py-2 text-xs font-semibold text-red-600 bg-red-50 border border-red-200 rounded-lg hover:bg-red-100 hover:border-red-300 transition-colors disabled:opacity-50"
              >
                Discard Changes
              </button>
            )}
            {dirty ? (
              <button
                ref={saveBtnRef}
                onClick={onSaveAndClose}
                disabled={saving}
                className="px-3.5 py-2 text-xs font-semibold text-white bg-emerald-600 rounded-lg hover:bg-emerald-700 transition-colors disabled:opacity-50 flex items-center gap-1.5 min-w-[120px] justify-center"
              >
                {saving ? (
                  <>
                    <span className="w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin" />
                    Saving...
                  </>
                ) : (
                  <>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
                      <polyline points="17 21 17 13 7 13 7 21" />
                      <polyline points="7 3 7 8 15 8" />
                    </svg>
                    {saveActionLabel}
                  </>
                )}
              </button>
            ) : (
              <button
                ref={saveBtnRef}
                onClick={onDiscard}
                className="px-3.5 py-2 text-xs font-semibold text-white bg-slate-700 rounded-lg hover:bg-slate-800 transition-colors flex items-center gap-1.5 justify-center"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="19" y1="12" x2="5" y2="12" />
                  <polyline points="12 19 5 12 12 5" />
                </svg>
                {closeActionLabel}
              </button>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
