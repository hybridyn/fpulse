/**
 * Shared resize handle (Z9, 2026-05-23).
 *
 * Single source of truth for the drag-to-resize affordance used across
 * the app — bottom panels (Storage drawer, Editor preview), side panels
 * (AI chat dock, node config panel), Data Wrangler column splits, etc.
 * Every surface gets the same look + feel + keyboard a11y for free.
 *
 * Two orientations:
 *   - ``horizontal`` — splits two side-by-side panels. Drag left ↔ right.
 *     Cursor is ew-resize. The handle is rendered as a 6px wide vertical
 *     pill sitting on the parent's edge (typically ``absolute top-0
 *     bottom-0 left-0`` or ``right-0``).
 *   - ``vertical`` — splits two stacked panels. Drag up ↕ down. Cursor
 *     is ns-resize. Rendered as a 6px tall horizontal pill on the top or
 *     bottom edge (``absolute left-0 right-0 top-0`` etc.).
 *
 * The component is presentational + drag-listener glue only. The
 * containing component owns the state — pass the current size in, accept
 * the new size out via ``onResize``. Persistence (localStorage), clamping
 * across viewport changes, and CSS variable propagation are caller
 * responsibilities so the primitive stays composable.
 *
 * Keyboard a11y: focus the handle (tabIndex={0}) and use:
 *   - Arrow keys: ±step (default 24)
 *   - PageUp/Down: ±largeStep (default 120)
 *   - Home / End: snap to min / max
 *
 * Aria: role="separator" with valuemin/max/now so screen readers report
 * the current split.
 */

import React, { useState } from 'react';

export type ResizeOrientation = 'horizontal' | 'vertical';

interface ResizeHandleProps {
  /** Layout axis the handle controls. */
  orientation: ResizeOrientation;
  /** Current pixel size of the panel this handle resizes. */
  value: number;
  /** New pixel size after a drag move / keyboard nudge. */
  onResize: (next: number) => void;
  /** Lower bound; the handle won't let value drop below this. */
  min: number;
  /** Upper bound. Pass a function if the cap should track viewport size. */
  max: number | (() => number);
  /**
   * Coordinate transform for horizontal handles.
   *   - 'right-edge' (default): dragging right grows the panel. Use for
   *     panels anchored on the LEFT (panel + handle on its right edge).
   *   - 'left-edge': dragging LEFT grows the panel. Use for panels
   *     anchored on the RIGHT (e.g. the AI chat dock).
   * For vertical handles, semantics are:
   *   - 'top-edge' (default for vertical): dragging UP grows the panel.
   *     Use for panels anchored at the BOTTOM of the viewport.
   *   - 'bottom-edge': dragging DOWN grows the panel. Use for panels
   *     anchored at the TOP.
   */
  edge?: 'left-edge' | 'right-edge' | 'top-edge' | 'bottom-edge';
  /** Arrow-key nudge size in px. Default 24. */
  step?: number;
  /** PageUp/PageDown nudge size in px. Default 120. */
  largeStep?: number;
  /** Hover label shown on the handle. Default lists the keybinds. */
  title?: string;
  /** Aria label for screen readers. Default "Drag to resize panel". */
  ariaLabel?: string;
  /** Extra Tailwind classes — typically positional (absolute / inset / z). */
  className?: string;
}

export default function ResizeHandle({
  orientation,
  value,
  onResize,
  min,
  max,
  edge,
  step = 24,
  largeStep = 120,
  title,
  ariaLabel,
  className = '',
}: ResizeHandleProps) {
  const [dragging, setDragging] = useState(false);
  const resolvedMax = () => (typeof max === 'function' ? max() : max);
  const isHorizontal = orientation === 'horizontal';
  const effectiveEdge =
    edge ??
    (isHorizontal ? 'right-edge' : 'top-edge');

  // Compute the new size given the latest pointer coordinate. Direction
  // depends on which edge of the panel the handle sits on — see the
  // ResizeHandleProps.edge comment for the four cases.
  const sizeFromPointer = (clientX: number, clientY: number, rect: DOMRect): number => {
    if (isHorizontal) {
      if (effectiveEdge === 'right-edge') return clientX - rect.left;
      // left-edge: panel anchored on the RIGHT, handle on its left side.
      // window.innerWidth - clientX gives the width from the right.
      return window.innerWidth - clientX;
    }
    // vertical
    if (effectiveEdge === 'top-edge') {
      // Panel anchored at BOTTOM: height = viewportH - clientY.
      return window.innerHeight - clientY;
    }
    // bottom-edge: panel anchored at TOP, handle below it.
    return clientY - rect.top;
  };

  const startDrag = (clientXStart: number, clientYStart: number) => {
    setDragging(true);
    // Stash the parent's rect so right-edge / bottom-edge drags get a
    // stable reference frame even if the layout shifts mid-drag.
    const parentRect = (typeof document !== 'undefined' && document.body)
      ? document.body.getBoundingClientRect()
      : { left: 0, top: 0, right: 0, bottom: 0 } as DOMRect;
    const onMove = (x: number, y: number) => {
      const raw = sizeFromPointer(x, y, parentRect);
      const clamped = Math.max(min, Math.min(raw, resolvedMax()));
      onResize(clamped);
    };
    const onMouseMove = (e: MouseEvent) => onMove(e.clientX, e.clientY);
    const onTouchMove = (e: TouchEvent) => {
      if (e.touches[0]) onMove(e.touches[0].clientX, e.touches[0].clientY);
    };
    const stop = () => {
      setDragging(false);
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', stop);
      document.removeEventListener('touchmove', onTouchMove);
      document.removeEventListener('touchend', stop);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', stop);
    document.addEventListener('touchmove', onTouchMove, { passive: true });
    document.addEventListener('touchend', stop);
    document.body.style.cursor = isHorizontal ? 'ew-resize' : 'ns-resize';
    document.body.style.userSelect = 'none';
    void clientXStart;
    void clientYStart;
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const cap = resolvedMax();
    let next = value;
    if (e.key === 'ArrowUp' || e.key === 'ArrowRight') next = Math.min(cap, value + step);
    else if (e.key === 'ArrowDown' || e.key === 'ArrowLeft') next = Math.max(min, value - step);
    else if (e.key === 'PageUp') next = Math.min(cap, value + largeStep);
    else if (e.key === 'PageDown') next = Math.max(min, value - largeStep);
    else if (e.key === 'Home') next = min;
    else if (e.key === 'End') next = cap;
    else return;
    e.preventDefault();
    onResize(next);
  };

  // Layout — horizontal handles are a 6px-wide vertical pill, vertical
  // handles are a 6px-tall horizontal pill. The inner span is the visible
  // affordance (resting slate, hover/drag amber).
  const hitClass = isHorizontal
    ? 'h-full w-2 cursor-ew-resize'
    : 'w-full h-2 cursor-ns-resize';

  const innerRestClass = isHorizontal
    ? 'w-1 h-16 rounded-full'
    : 'h-1 w-16 rounded-full';

  const innerActiveClass = isHorizontal
    ? (dragging ? 'bg-amber-500 h-24 shadow-md' : 'bg-slate-300 group-hover:bg-amber-400 group-hover:h-20 group-focus:bg-amber-400')
    : (dragging ? 'bg-amber-500 w-24 shadow-md' : 'bg-slate-300 group-hover:bg-amber-400 group-hover:w-20 group-focus:bg-amber-400');

  return (
    <div
      role="separator"
      tabIndex={0}
      aria-orientation={isHorizontal ? 'vertical' : 'horizontal'}
      aria-label={ariaLabel || 'Drag to resize panel'}
      aria-valuemin={min}
      aria-valuemax={resolvedMax()}
      aria-valuenow={value}
      title={title || 'Drag to resize · Arrow keys ±24px · PageUp/Down ±120px'}
      onMouseDown={(e) => { e.preventDefault(); startDrag(e.clientX, e.clientY); }}
      onTouchStart={(e) => { if (e.touches[0]) startDrag(e.touches[0].clientX, e.touches[0].clientY); }}
      onKeyDown={onKeyDown}
      className={`${hitClass} flex items-center justify-center group select-none ${className}`}
    >
      <span className={`${innerRestClass} transition-all duration-150 ${innerActiveClass}`} />
    </div>
  );
}
