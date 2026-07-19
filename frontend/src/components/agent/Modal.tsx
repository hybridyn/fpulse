/**
 * Modal — generic content overlay.
 *
 * For destructive confirmations use ConfirmDialog instead — it has the
 * Confirm/Cancel button pair built in. This Modal is for arbitrary content
 * (provider config form, trace replay, etc.).
 */

import { ReactNode, useEffect } from 'react';

interface Props {
  isOpen: boolean;
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
  /** Card width. Defaults to a comfortable form width. */
  width?: 'sm' | 'md' | 'lg' | 'xl';
}

const WIDTH_CLASSES: Record<NonNullable<Props['width']>, string> = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-2xl',
  xl: 'max-w-4xl',
};

export default function Modal({ isOpen, title, subtitle, onClose, children, width = 'lg' }: Props) {
  useEffect(() => {
    if (!isOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="modal-title"
    >
      <div
        className="absolute inset-0 bg-slate-900/40 backdrop-blur-sm"
        onClick={onClose}
        aria-hidden
      />

      <div className={`relative bg-white rounded-2xl shadow-2xl border border-slate-200 ${WIDTH_CLASSES[width]} w-full max-h-[85vh] overflow-hidden flex flex-col`}>
        <div className="px-5 py-4 border-b border-slate-100 bg-gradient-to-r from-indigo-500 to-purple-500 text-white shrink-0 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 id="modal-title" className="text-base font-bold truncate">{title}</h2>
            {subtitle && <p className="text-xs text-white/75 mt-0.5">{subtitle}</p>}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="shrink-0 w-7 h-7 flex items-center justify-center rounded-lg text-white hover:bg-white/15 transition-colors"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-5">
          {children}
        </div>
      </div>
    </div>
  );
}
