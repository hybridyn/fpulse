/**
 * FloatingAgentWidget — Copilot-style right-side dock for the F-Pulse agent.
 *
 * Behaviour mirrors Microsoft Copilot in M365 / Edge / Windows 11:
 *   - Always-on toggle pinned to the bottom-right (collapsed state)
 *   - Click → slides in a docked panel on the RIGHT edge, full viewport height
 *   - User-resizable width via a drag handle on the LEFT edge of the dock
 *   - Width persists to localStorage across sessions
 *   - Stays open across page navigation (chat state in useAgentChatStore)
 *   - Closes only on explicit X / Close button OR Escape
 *
 * The chat history is held in a module-level singleton so the conversation
 * survives navigation, the drawer being collapsed, and the editor visit.
 */

import { useEffect, useRef, useState } from 'react';
import AgentChatPanel from './AgentChatPanel';
import { useAgentChatStore } from '../../hooks/useAgentChatStore';

const WIDTH_KEY = 'fpulse_copilot_width';
const MIN_WIDTH = 320;
const MAX_WIDTH = 900;
const DEFAULT_WIDTH = 420;

function loadWidth(): number {
  try {
    const raw = localStorage.getItem(WIDTH_KEY);
    if (!raw) return DEFAULT_WIDTH;
    const v = parseInt(raw, 10);
    if (!Number.isFinite(v)) return DEFAULT_WIDTH;
    return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, v));
  } catch {
    return DEFAULT_WIDTH;
  }
}

export default function FloatingAgentWidget() {
  const { open, setOpen, turns } = useAgentChatStore();
  const [width, setWidth] = useState<number>(() => loadWidth());
  const [resizing, setResizing] = useState(false);
  const dragStartRef = useRef<{ startX: number; startWidth: number } | null>(null);
  // Hide the collapsed toggle when another right-side drawer is open
  // (connection details, credential details, etc.) so its action buttons
  // are not occluded by the floating Copilot button.
  const [rightDrawerOpen, setRightDrawerOpen] = useState(false);
  useEffect(() => {
    const check = () => {
      const asides = document.querySelectorAll('aside,[role="dialog"]');
      const vw = window.innerWidth;
      let found = false;
      asides.forEach((el) => {
        const aria = el.getAttribute('aria-label') || '';
        if (aria === 'F-Pulse Copilot') return;
        const r = (el as HTMLElement).getBoundingClientRect();
        if (r.width > 200 && r.height > 200 && r.right >= vw - 4 && r.bottom >= window.innerHeight - 4) {
          found = true;
        }
      });
      setRightDrawerOpen(found);
    };
    check();
    const obs = new MutationObserver(check);
    obs.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style', 'aria-hidden'] });
    window.addEventListener('resize', check);
    return () => { obs.disconnect(); window.removeEventListener('resize', check); };
  }, []);

  // Escape closes the dock (chat is preserved in the store).
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [open, setOpen]);

  // Click outside the dock minimizes it (chat preserved). We exclude the
  // resize-in-progress case so dragging the handle past the edge doesn't
  // collapse the dock mid-drag.
  //
  // CAPTURE phase (third arg = true) — bubble phase doesn't work because
  // React Flow nodes, form inputs, modal libraries routinely call
  // e.stopPropagation() in their own mousedown handlers, which would
  // suppress a bubble-phase document listener. Capture runs FIRST, before
  // any descendant handler can stop the event. We're not preventing default
  // or stopping propagation ourselves, so the user's actual click still
  // reaches its intended target normally.
  useEffect(() => {
    if (!open || resizing) return;
    const onMouseDown = (e: MouseEvent) => {
      const aside = document.querySelector('aside[aria-label="F-Pulse Copilot"]');
      if (!aside) return;
      const target = e.target as Node | null;
      if (!target) return;
      if (aside.contains(target)) return;
      // Don't collapse on clicks inside any open dialog/modal (radix, custom
      // modals) — those overlay the dock semantically. Use role="dialog".
      const targetEl = target as HTMLElement;
      const inDialog = targetEl?.closest?.('[role="dialog"], [data-headlessui-state]');
      if (inDialog) return;
      // Don't collapse on clicks on the collapsed-state toggle button or
      // the page-edge tab — the toggle path closes naturally when those
      // re-open the dock; collapsing on click here would create a flicker.
      if (targetEl?.closest?.('[aria-label="Open Copilot dock"], [aria-label="Open AI agent"]')) return;
      setOpen(false);
    };
    document.addEventListener('mousedown', onMouseDown, /* capture */ true);
    return () => document.removeEventListener('mousedown', onMouseDown, /* capture */ true);
  }, [open, resizing, setOpen]);

  // Drag-to-resize. The handle is on the LEFT edge of the dock (the dock is
  // anchored right) — dragging LEFT increases width, RIGHT decreases.
  useEffect(() => {
    if (!resizing) return;
    const onMove = (e: MouseEvent) => {
      const start = dragStartRef.current;
      if (!start) return;
      const dx = start.startX - e.clientX; // left-drag → positive dx → wider
      const next = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, start.startWidth + dx));
      setWidth(next);
    };
    const onUp = () => {
      setResizing(false);
      dragStartRef.current = null;
      try { localStorage.setItem(WIDTH_KEY, String(width)); } catch { /* ignore */ }
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    // Disable text selection while dragging so the cursor doesn't pick up text.
    const prevSelect = document.body.style.userSelect;
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'ew-resize';
    return () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.body.style.userSelect = prevSelect;
      document.body.style.cursor = '';
    };
  }, [resizing, width]);

  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    dragStartRef.current = { startX: e.clientX, startWidth: width };
    setResizing(true);
  };

  // Double-click the handle to reset to the default width — discoverable via
  // the title tooltip; cheap escape hatch when the user has dragged too far.
  const resetWidth = () => {
    setWidth(DEFAULT_WIDTH);
    try { localStorage.setItem(WIDTH_KEY, String(DEFAULT_WIDTH)); } catch { /* ignore */ }
  };

  return (
    <>
      {/* Collapsed-state toggle button — pinned to the bottom-right corner.
          Hidden when another right-side drawer is open so its action bar is
          not covered. */}
      {!open && !rightDrawerOpen && (
        <button
          onClick={() => setOpen(true)}
          aria-label="Open AI agent"
          className="fixed bottom-6 right-6 z-50 flex items-center gap-2 px-4 py-2.5 rounded-2xl text-sm font-semibold shadow-lg bg-gradient-to-r from-indigo-500 to-purple-500 text-white hover:from-indigo-600 hover:to-purple-600 hover:shadow-xl hover:scale-105 transition-all"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
          </svg>
          F-Pulse Copilot
          {turns.length > 0 && (
            <span
              className="ml-1 inline-flex items-center justify-center min-w-[20px] h-5 px-1.5 text-xs font-bold bg-white text-indigo-600 rounded-full"
              title={`${turns.length} message${turns.length === 1 ? '' : 's'} in this chat`}
            >
              {turns.length}
            </span>
          )}
        </button>
      )}

      {/* Side-docked drawer — slides in from the right edge.
          User-resizable via the left-edge drag handle (width state above). */}
      <aside
        className={`fixed top-0 right-0 z-50 h-screen max-w-[100vw] bg-transparent
          ${resizing ? '' : 'transition-transform duration-200 ease-out'}
          ${open ? 'translate-x-0' : 'translate-x-full pointer-events-none'}
        `}
        style={{ width: `${width}px` }}
        aria-hidden={!open}
        aria-label="F-Pulse Copilot"
      >
        <div className="relative h-full flex flex-col shadow-2xl border-l border-slate-200 bg-white">
          {/* Resize handle — 6px-wide invisible strip on the left edge.
              The visible 1px line is the dock border; the handle just makes
              it grabbable. Cursor switches to ew-resize on hover. Double-click
              resets to default width. */}
          <div
            onMouseDown={startResize}
            onDoubleClick={resetWidth}
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize Copilot dock (drag, or double-click to reset)"
            title="Drag to resize · double-click to reset"
            className={`absolute top-0 left-0 h-full w-1.5 -ml-0.5 cursor-ew-resize z-10
              ${resizing ? 'bg-indigo-400/50' : 'hover:bg-indigo-400/30'}
              transition-colors`}
          />
          <AgentChatPanel />
        </div>
      </aside>
    </>
  );
}
