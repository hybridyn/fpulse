/**
 * DetailDrawer — right-side slide-in panel for master-detail patterns.
 *
 * Phase 1 foundation. Used by Pipelines / Executions / Connections /
 * Credentials list pages so clicking a row reveals details on the right
 * without navigating away from the list.
 *
 * Behaviour:
 *   - Slides in from the right edge with a 250ms ease-out
 *   - Width is user-configurable (default 480px) and persisted per-key
 *   - Click outside drawer (the backdrop) closes it
 *   - Escape key closes it
 *   - Drawer content scrolls; header and footer are sticky
 *   - Backdrop is subtle (bg-black/10 with backdrop-blur-sm) so the list
 *     stays visible behind — that's the whole point of master-detail
 *
 * NOT a modal — modals dim heavily and trap focus. The drawer is
 * deliberately lightweight so users can context-switch between the list
 * and the detail without losing either.
 */

import { ReactNode, useEffect, useRef } from 'react';

export interface DetailDrawerProps {
  open: boolean;
  onClose: () => void;
  /** Drawer header — title + optional subtitle + close button slot. */
  title: ReactNode;
  subtitle?: ReactNode;
  /** Main content (scrollable). */
  children: ReactNode;
  /** Optional sticky footer (action bar). */
  footer?: ReactNode;
  /** Drawer width in pixels (default 480). */
  widthPx?: number;
  /** When true, clicking the backdrop closes the drawer. Default true. */
  closeOnBackdrop?: boolean;
  /** When true, pressing Escape closes the drawer. Default true. */
  closeOnEsc?: boolean;
  /** ARIA label for the panel. */
  ariaLabel?: string;
  /**
   * Z31 (2026-05-23) — push the page content instead of overlaying it.
   *
   * When true the drawer publishes its width as a CSS variable
   * (``--fp-drawer-w``) on ``document.documentElement`` while open;
   * consuming pages bind a ``paddingRight: var(--fp-drawer-w, 0px)``
   * on their main scroll wrapper so the table reflows instead of being
   * chopped by the drawer. The semi-opaque backdrop is also suppressed
   * by default in this mode — there's nothing to dim, the table sits
   * next to the drawer rather than behind it.
   *
   * Default false to keep every existing call site (and modals built
   * on this drawer) behaving exactly as before.
   */
  pushContent?: boolean;
}

export function DetailDrawer({
  open, onClose, title, subtitle, children, footer,
  widthPx = 480, closeOnBackdrop = true, closeOnEsc = true,
  ariaLabel = 'Details panel', pushContent = false,
}: DetailDrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);

  // Escape closes
  useEffect(() => {
    if (!open || !closeOnEsc) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [open, closeOnEsc, onClose]);

  // Z31: publish drawer width as a CSS variable so consuming pages can
  // push their content into the remaining width. Self-cleaning on close
  // + unmount. No-op in overlay mode (pushContent=false).
  useEffect(() => {
    if (!pushContent) return;
    const root = document.documentElement;
    if (open) {
      root.style.setProperty('--fp-drawer-w', `${widthPx}px`);
    } else {
      root.style.removeProperty('--fp-drawer-w');
    }
    return () => { root.style.removeProperty('--fp-drawer-w'); };
  }, [open, widthPx, pushContent]);

  // Z34 (2026-05-23) — outside-click closes the drawer even in push
  // mode. The original overlay mode caught these clicks via the
  // backdrop element, but push mode suppresses the backdrop (the page
  // is reflowed, not occluded), so we instead attach a document-level
  // mousedown listener that closes when the click target is outside
  // the panel. Using `mousedown` (not `click`) so a press-and-drag
  // selection that ends inside the drawer doesn't accidentally
  // close it. The `e.composedPath()` check honors shadow DOM if any
  // consumer later wraps the page in a shadow root.
  useEffect(() => {
    if (!open || !closeOnBackdrop || !pushContent) return;
    const onMouseDown = (e: MouseEvent) => {
      const panel = panelRef.current;
      if (!panel) return;
      const target = e.target as Node | null;
      // Honor composedPath for shadow DOM consumers; fall back to a
      // node-contains check for plain DOM.
      const path = typeof (e as any).composedPath === 'function'
        ? ((e as any).composedPath() as EventTarget[])
        : [];
      if (path.length > 0) {
        if (path.includes(panel)) return;
      } else if (target && panel.contains(target)) {
        return;
      }
      onClose();
    };
    // Capture phase so we catch the click before any inner handler
    // (e.g. a row's `onClick`) gets a chance to swap drawer content.
    document.addEventListener('mousedown', onMouseDown, true);
    return () => document.removeEventListener('mousedown', onMouseDown, true);
  }, [open, closeOnBackdrop, pushContent, onClose]);

  // In push mode the backdrop is suppressed — the page is reflowed,
  // not occluded, so dimming it would be a lie. Outside-click is
  // handled by the document-level listener above.
  const showBackdrop = open && closeOnBackdrop && !pushContent;

  return (
    <>
      {/* Backdrop — light, no full dim. List stays readable. */}
      {showBackdrop && (
        <div
          className="fixed inset-0 z-40 bg-black/10 backdrop-blur-[1px]"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      {/* Drawer panel */}
      <aside
        ref={panelRef}
        className={`
          fixed top-0 right-0 z-50 h-screen bg-white shadow-2xl border-l border-slate-200
          flex flex-col transition-transform duration-250 ease-out
          ${open ? 'translate-x-0' : 'translate-x-full pointer-events-none'}
        `}
        style={{ width: `${widthPx}px`, maxWidth: '95vw' }}
        aria-label={ariaLabel}
        aria-hidden={!open}
      >
        {/* Sticky header */}
        <header className="px-5 py-3 border-b border-slate-200 flex items-start justify-between gap-3 shrink-0 bg-white">
          <div className="min-w-0 flex-1">
            <div className="text-base font-semibold text-slate-800 truncate">{title}</div>
            {subtitle && (
              <div className="text-xs text-slate-500 mt-0.5 truncate">{subtitle}</div>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close details"
            className="shrink-0 -mr-1 p-1.5 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </header>

        {/* Scrollable body */}
        <div className="flex-1 overflow-y-auto p-5">{children}</div>

        {/* Optional sticky footer */}
        {footer && (
          <footer className="px-5 py-3 border-t border-slate-200 shrink-0 bg-white">
            {footer}
          </footer>
        )}
      </aside>
    </>
  );
}

export default DetailDrawer;
