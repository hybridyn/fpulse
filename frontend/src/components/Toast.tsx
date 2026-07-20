import { useEffect, useState, useCallback } from 'react';
import { create } from 'zustand';

export interface ToastMessage {
  id: string;
  type: 'success' | 'error' | 'warning' | 'info';
  title: string;
  message?: string;
  duration?: number;
}

interface ToastStore {
  toasts: ToastMessage[];
  addToast: (toast: Omit<ToastMessage, 'id'>) => void;
  removeToast: (id: string) => void;
}

// 2026-05-19 (P1 #16 of PAGE_BY_PAGE_AUDIT.md): two changes to the toast
// pipeline.
//   1. Dedup key now includes the message body, not just type:title. Two
//      genuinely different errors that share a title ("Save failed —
//      Network" vs "Save failed — Validation") used to collapse to one
//      toast and the second cause was silently lost.
//   2. Maximum stack size is capped — a burst of 30 distinct errors used
//      to stack 30 cards top-right and obscure the page. The oldest
//      toasts now get evicted FIFO once the cap is reached. The cap is
//      generous (5) so legitimate workflows aren't truncated.
const _recentToasts = new Map<string, number>();
const DEDUP_COOLDOWN_MS = 2000;
const MAX_STACKED_TOASTS = 5;

export const useToastStore = create<ToastStore>((set) => ({
  toasts: [],
  addToast: (toast) => {
    // Deduplicate: same type + title + body within cooldown → skip
    const dedupeKey = `${toast.type}:${toast.title}:${toast.message || ''}`;
    const lastShown = _recentToasts.get(dedupeKey) || 0;
    if (Date.now() - lastShown < DEDUP_COOLDOWN_MS) return;
    _recentToasts.set(dedupeKey, Date.now());

    const id = Math.random().toString(36).slice(2, 10);
    set((s) => {
      // Evict oldest until under cap. We trim from the FRONT (oldest)
      // because the render site stacks top-down; users expect the most
      // recent message to be visible.
      const next = [...s.toasts, { ...toast, id }];
      while (next.length > MAX_STACKED_TOASTS) next.shift();
      return { toasts: next };
    });
    // Auto-remove after duration
    setTimeout(() => {
      set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
    }, toast.duration || 4000);
  },
  removeToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

// Convenience functions
export const toast = {
  success: (title: string, message?: string) => useToastStore.getState().addToast({ type: 'success', title, message }),
  error: (title: string, message?: string) => useToastStore.getState().addToast({ type: 'error', title, message, duration: 6000 }),
  warning: (title: string, message?: string) => useToastStore.getState().addToast({ type: 'warning', title, message }),
  info: (title: string, message?: string) => useToastStore.getState().addToast({ type: 'info', title, message }),
};

const ICONS: Record<string, { bg: string; border: string; icon: React.ReactNode }> = {
  success: {
    bg: 'bg-green-50',
    border: 'border-green-200',
    icon: (
      <div className="w-6 h-6 rounded-full bg-green-100 flex items-center justify-center shrink-0">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#16a34a" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="20 6 9 17 4 12" />
        </svg>
      </div>
    ),
  },
  error: {
    bg: 'bg-red-50',
    border: 'border-red-200',
    icon: (
      <div className="w-6 h-6 rounded-full bg-red-100 flex items-center justify-center shrink-0">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#dc2626" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </div>
    ),
  },
  warning: {
    bg: 'bg-amber-50',
    border: 'border-amber-200',
    icon: (
      <div className="w-6 h-6 rounded-full bg-amber-100 flex items-center justify-center shrink-0">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#d97706" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
          <line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
        </svg>
      </div>
    ),
  },
  info: {
    bg: 'bg-blue-50',
    border: 'border-blue-200',
    icon: (
      <div className="w-6 h-6 rounded-full bg-blue-100 flex items-center justify-center shrink-0">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#2563eb" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10" /><line x1="12" y1="16" x2="12" y2="12" /><line x1="12" y1="8" x2="12.01" y2="8" />
        </svg>
      </div>
    ),
  },
};

function ToastItem({ t, onClose }: { t: ToastMessage; onClose: () => void }) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    requestAnimationFrame(() => setVisible(true));
  }, []);

  const style = ICONS[t.type];

  return (
    <div
      className={`flex items-start gap-3 px-4 py-3 rounded-xl border shadow-lg backdrop-blur-sm transition-all duration-300 ${style.bg} ${style.border} ${
        visible ? 'translate-x-0 opacity-100' : 'translate-x-8 opacity-0'
      }`}
      style={{ minWidth: 280, maxWidth: 400 }}
    >
      {style.icon}
      <div className="flex-1 min-w-0">
        <div className="text-sm font-semibold text-slate-800">{t.title}</div>
        {t.message && <div className="text-xs text-slate-500 mt-0.5">{t.message}</div>}
      </div>
      <button onClick={onClose} className="text-slate-300 hover:text-slate-500 shrink-0 mt-0.5">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
    </div>
  );
}

export default function Toast() {
  const toasts = useToastStore((s) => s.toasts);
  const removeToast = useToastStore((s) => s.removeToast);

  if (toasts.length === 0) return null;

  return (
    <div className="fixed top-4 right-4 z-[100] flex flex-col gap-2">
      {toasts.map((t) => (
        <ToastItem key={t.id} t={t} onClose={() => removeToast(t.id)} />
      ))}
    </div>
  );
}
