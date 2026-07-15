/**
 * In-app replacement for native window.alert / window.confirm / window.prompt.
 *
 * Why: browser-native dialogs break the app's visual language (the old
 * "localhost:5174 says" boxes), can't be themed, can't render inside modals
 * reliably, and block the entire tab's JS thread. This module centralises
 * all three into one themed component mounted at the app root.
 *
 * Usage — direct imperative API (any file, async function):
 *
 *   import { uiAlert, uiConfirm, uiPrompt } from '../ui/dialog';
 *
 *   await uiAlert('Saved.');
 *   if (!(await uiConfirm('Delete this row?'))) return;
 *   const notes = await uiPrompt('Approval notes?');
 *
 * The imperative helpers are thin wrappers around a module-level handler
 * that the DialogProvider registers on mount. The helpers resolve to the
 * noop default if the provider isn't mounted, so tests that render a
 * component in isolation don't explode.
 *
 * Mount one DialogProvider at the app root (App.tsx wraps <DialogRoot/>
 * around everything) — it owns the portal, keyboard handling (Esc
 * cancels, Enter confirms), and focus trap.
 */

import {
  ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';

type DialogKind = 'alert' | 'confirm' | 'prompt';

export interface DialogOptions {
  title?: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  placeholder?: string;
  defaultValue?: string;
  // Renders the primary button in red (destructive action). The
  // ``destructive`` alias is the canonical name used by pages; ``danger``
  // is the older spelling kept for backward compatibility. Either works.
  danger?: boolean;
  destructive?: boolean;
  // Force the prompt input to be secret (password-style).
  secret?: boolean;
}

interface InternalDialog extends DialogOptions {
  kind: DialogKind;
  resolve: (value: any) => void;
}

// Module-level handler registered by DialogRoot. We avoid coupling
// callers to React context so they can be invoked from plain async
// utility functions, not just React components.
let openDialogImpl:
  | ((kind: DialogKind, opts: DialogOptions) => Promise<any>)
  | null = null;

function toOpts(input: string | DialogOptions): DialogOptions {
  return typeof input === 'string' ? { message: input } : input;
}

/** Themed alert — single OK button. Resolves when dismissed. */
export function uiAlert(input: string | DialogOptions): Promise<void> {
  if (!openDialogImpl) {
    // Degrade to the browser dialog only if the provider somehow failed
    // to mount (e.g. very early startup error before App renders).
    // Production paths always go through the themed version.
    window.alert(toOpts(input).message);
    return Promise.resolve();
  }
  return openDialogImpl('alert', toOpts(input)) as Promise<void>;
}

/** Themed confirm — OK + Cancel. Resolves to boolean. */
export function uiConfirm(input: string | DialogOptions): Promise<boolean> {
  if (!openDialogImpl) {
    return Promise.resolve(window.confirm(toOpts(input).message));
  }
  return openDialogImpl('confirm', toOpts(input)) as Promise<boolean>;
}

/** Themed prompt — text input + OK/Cancel. Resolves to string, or null on cancel. */
export function uiPrompt(input: string | DialogOptions): Promise<string | null> {
  if (!openDialogImpl) {
    const o = toOpts(input);
    return Promise.resolve(window.prompt(o.message, o.defaultValue || ''));
  }
  return openDialogImpl('prompt', toOpts(input)) as Promise<string | null>;
}

// ────────────────────────────────────────────────────────────────────────────
// Root component — mount once at the top of the tree.
// ────────────────────────────────────────────────────────────────────────────

export function DialogRoot({ children }: { children: ReactNode }) {
  const [active, setActive] = useState<InternalDialog | null>(null);
  const [inputValue, setInputValue] = useState('');
  const inputRef = useRef<HTMLInputElement | null>(null);

  const openDialog = useCallback(
    (kind: DialogKind, opts: DialogOptions): Promise<any> => {
      return new Promise((resolve) => {
        setActive({ kind, ...opts, resolve });
        setInputValue(opts.defaultValue || '');
      });
    },
    [],
  );

  // Register the module-level handler on mount so any file can call the
  // imperative helpers without a React hook. Unregister on unmount so
  // a re-mounted root (HMR) replaces the handler cleanly.
  useEffect(() => {
    openDialogImpl = openDialog;
    return () => {
      openDialogImpl = null;
    };
  }, [openDialog]);

  // Focus the primary control when a dialog opens. For prompts, that's
  // the text input; for alert/confirm, it's the confirm button so
  // Enter-to-dismiss works immediately.
  useEffect(() => {
    if (!active) return;
    const t = setTimeout(() => {
      if (active.kind === 'prompt' && inputRef.current) {
        inputRef.current.focus();
        inputRef.current.select();
      } else {
        const btn = document.querySelector<HTMLButtonElement>(
          '[data-dialog-confirm]',
        );
        btn?.focus();
      }
    }, 20);
    return () => clearTimeout(t);
  }, [active]);

  const closeWith = useCallback(
    (value: any) => {
      if (!active) return;
      active.resolve(value);
      setActive(null);
      setInputValue('');
    },
    [active],
  );

  const onCancel = useCallback(() => {
    // Alert has no cancel — dismissing via backdrop / Esc also resolves.
    if (!active) return;
    if (active.kind === 'alert') closeWith(undefined);
    else if (active.kind === 'confirm') closeWith(false);
    else closeWith(null); // prompt
  }, [active, closeWith]);

  const onConfirm = useCallback(() => {
    if (!active) return;
    if (active.kind === 'prompt') closeWith(inputValue);
    else if (active.kind === 'confirm') closeWith(true);
    else closeWith(undefined);
  }, [active, inputValue, closeWith]);

  // Keyboard: Esc cancels, Enter confirms (unless user is typing in a
  // textarea — single-line inputs auto-submit on Enter via the form
  // handler below).
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onCancel();
      } else if (e.key === 'Enter' && active.kind !== 'prompt') {
        e.preventDefault();
        onConfirm();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [active, onCancel, onConfirm]);

  return (
    <>
      {children}
      {active && <DialogView active={active} inputRef={inputRef} inputValue={inputValue} setInputValue={setInputValue} onConfirm={onConfirm} onCancel={onCancel} />}
    </>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// Presentational — inlined here rather than split into a file because it
// has no meaningful standalone use and keeping it co-located makes the
// keyboard + focus wiring above readable.
// ────────────────────────────────────────────────────────────────────────────

function DialogView({
  active,
  inputRef,
  inputValue,
  setInputValue,
  onConfirm,
  onCancel,
}: {
  active: InternalDialog;
  inputRef: React.MutableRefObject<HTMLInputElement | null>;
  inputValue: string;
  setInputValue: (s: string) => void;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const isPrompt = active.kind === 'prompt';
  const isAlert = active.kind === 'alert';
  // Accept either `destructive` (canonical) or `danger` (legacy alias) —
  // both render the primary button in red.
  const danger = !!(active.destructive || active.danger);
  const title =
    active.title ||
    (active.kind === 'alert'
      ? 'Notice'
      : active.kind === 'confirm'
        ? 'Confirm'
        : 'Input required');
  const confirmLabel = active.confirmLabel || (isAlert ? 'OK' : 'Confirm');
  const cancelLabel = active.cancelLabel || 'Cancel';

  return (
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="app-dialog-title"
    >
      {/* Backdrop — click outside cancels. Alert treats this as OK
          (nothing to reject). */}
      <div
        className="absolute inset-0 bg-slate-900/40 backdrop-blur-sm"
        onClick={onCancel}
      />
      <div className="relative z-10 w-full max-w-md rounded-xl bg-white shadow-2xl border border-slate-200 overflow-hidden animate-in fade-in zoom-in duration-150">
        <div className="px-5 pt-5 pb-4">
          <div
            id="app-dialog-title"
            className="text-base font-bold text-slate-800 flex items-center gap-2"
          >
            {danger && (
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#dc2626" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                <line x1="12" y1="9" x2="12" y2="13" />
                <line x1="12" y1="17" x2="12.01" y2="17" />
              </svg>
            )}
            {title}
          </div>
          <div className="mt-2 text-sm text-slate-600 whitespace-pre-wrap">
            {active.message}
          </div>
          {isPrompt && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                onConfirm();
              }}
              className="mt-3"
            >
              <input
                ref={inputRef}
                type={active.secret ? 'password' : 'text'}
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                placeholder={active.placeholder || ''}
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-amber-300 focus:border-amber-300 outline-none"
              />
            </form>
          )}
        </div>
        <div className="px-5 py-3 bg-slate-50 border-t border-slate-200 flex items-center justify-end gap-2">
          {!isAlert && (
            <button
              type="button"
              onClick={onCancel}
              className="px-4 py-2 text-sm font-semibold text-slate-700 bg-white border border-slate-300 rounded-lg hover:bg-slate-100 transition-colors"
            >
              {cancelLabel}
            </button>
          )}
          <button
            type="button"
            data-dialog-confirm
            onClick={onConfirm}
            className={`px-4 py-2 text-sm font-bold text-white rounded-lg shadow-sm transition-colors ${
              danger
                ? 'bg-red-600 hover:bg-red-700'
                : 'bg-amber-500 hover:bg-amber-600'
            }`}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
