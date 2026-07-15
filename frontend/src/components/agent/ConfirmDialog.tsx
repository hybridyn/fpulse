/**
 * ConfirmDialog — generic in-app confirmation modal.
 *
 * Replaces `window.confirm()` for destructive actions. Reusable across
 * the agent panel (model delete) and any other destructive flows.
 *
 * Renders as a fixed full-screen overlay with a centered card. Click the
 * backdrop or Escape to cancel; explicit Confirm button to proceed.
 */

import { ReactNode, useEffect } from 'react';

export interface ConfirmDialogProps {
  isOpen: boolean;
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: 'danger' | 'primary';
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export default function ConfirmDialog({
  isOpen,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'primary',
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  // Escape closes the dialog
  useEffect(() => {
    if (!isOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onCancel();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [isOpen, busy, onCancel]);

  if (!isOpen) return null;

  const confirmClasses = variant === 'danger'
    ? 'bg-red-600 hover:bg-red-700'
    : 'bg-indigo-600 hover:bg-indigo-700';

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-dialog-title"
    >
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-slate-900/40 backdrop-blur-sm"
        onClick={() => { if (!busy) onCancel(); }}
        aria-hidden
      />

      {/* Card */}
      <div className="relative bg-white rounded-2xl shadow-2xl border border-slate-200 max-w-md w-full overflow-hidden">
        <div className={`px-5 py-4 border-b border-slate-100 ${variant === 'danger' ? 'bg-red-50' : 'bg-slate-50'}`}>
          <div className="flex items-start gap-3">
            {variant === 'danger' && (
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="text-red-600 shrink-0 mt-0.5">
                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                <line x1="12" y1="9" x2="12" y2="13" />
                <line x1="12" y1="17" x2="12.01" y2="17" />
              </svg>
            )}
            <h3
              id="confirm-dialog-title"
              className={`text-base font-bold ${variant === 'danger' ? 'text-red-900' : 'text-slate-900'}`}
            >
              {title}
            </h3>
          </div>
        </div>

        <div className="px-5 py-4 text-sm text-slate-700 leading-relaxed">
          {message}
        </div>

        <div className="px-5 py-3 bg-slate-50 border-t border-slate-100 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-200 rounded-lg transition-colors disabled:opacity-50"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className={`px-3 py-1.5 text-sm font-semibold text-white rounded-lg transition-colors disabled:opacity-50 ${confirmClasses}`}
          >
            {busy ? 'Working\u2026' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
