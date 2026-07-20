/**
 * RowActionButton — standardised per-row action chip used across list pages.
 *
 * Why this exists:
 *   - The Pipelines / Connections / Credentials / Executions tables each
 *     defined their own actions cell with slightly different sizes
 *     (`w-7 h-7` vs `w-8 h-8`), icon sizes (13px / 14px), and resting
 *     colours (`text-slate-400` vs `text-slate-500`). On a light bg,
 *     `text-slate-400` was the user-visible regression: the buttons
 *     looked invisible until you hovered.
 *   - Centralising the geometry + base colour means every row-action chip
 *     in the app reads the same way, and lets us tune the resting
 *     contrast in one place.
 *
 * Sizes:
 *   - default: 28×28 button, 14px icon slot — matches the existing
 *     `w-7 h-7` chips so dropping this in doesn't shift table rows.
 *   - md:      32×32 button, 16px icon slot — for taller rows / drawer
 *     headers where the chip needs a bit more presence.
 *
 * Tones:
 *   - neutral / blue / red / green / indigo / amber. Resting colour is
 *     a single `text-slate-500` regardless of tone; the tone applies on
 *     hover (background tint + icon-colour shift) so the resting state
 *     stays calm.
 *
 * Children: pass an inline SVG sized to `iconSize` (default 14). The
 * SVG should `stroke="currentColor"` so it inherits the button's
 * tone-on-hover.
 */
import React from 'react';

export type RowActionTone =
  | 'neutral'
  | 'blue'
  | 'red'
  | 'green'
  | 'indigo'
  | 'amber';

const TONE_MAP: Record<RowActionTone, { hover: string; activeText: string; activeBg: string }> = {
  neutral: { hover: 'hover:text-slate-700 hover:bg-slate-100',     activeText: 'text-slate-700',  activeBg: 'bg-slate-100' },
  blue:    { hover: 'hover:text-blue-600 hover:bg-blue-50',         activeText: 'text-blue-600',   activeBg: 'bg-blue-50' },
  red:     { hover: 'hover:text-red-600 hover:bg-red-50',           activeText: 'text-red-600',    activeBg: 'bg-red-50' },
  green:   { hover: 'hover:text-emerald-600 hover:bg-emerald-50',   activeText: 'text-emerald-600',activeBg: 'bg-emerald-50' },
  indigo:  { hover: 'hover:text-indigo-600 hover:bg-indigo-50',     activeText: 'text-indigo-600', activeBg: 'bg-indigo-50' },
  amber:   { hover: 'hover:text-amber-600 hover:bg-amber-50',       activeText: 'text-amber-600',  activeBg: 'bg-amber-50' },
};

interface RowActionButtonProps {
  onClick?: (e: React.MouseEvent<HTMLButtonElement>) => void;
  title: string;
  tone?: RowActionTone;
  /** When true, renders in the tone's "active" colour (e.g. green for a
   *  successful Test result) instead of the slate-500 resting colour. */
  active?: boolean;
  /** Show a label next to the icon — used by the Credentials "Test" /
   *  "Connected!" chip where the action has a textual outcome. */
  label?: React.ReactNode;
  size?: 'default' | 'md';
  disabled?: boolean;
  children?: React.ReactNode;
}

export default function RowActionButton({
  onClick,
  title,
  tone = 'blue',
  active = false,
  label,
  size = 'default',
  disabled = false,
  children,
}: RowActionButtonProps) {
  const dim = size === 'md' ? 'h-8 min-w-[2rem]' : 'h-7 min-w-[1.75rem]';
  const t = TONE_MAP[tone];
  // Icon-only buttons collapse to a square; icon+label expands width but
  // keeps the same height so they line up with the icon-only siblings.
  const padding = label ? 'px-2.5 gap-1.5' : '';
  const baseColour = active
    ? `${t.activeText} ${t.activeBg}`
    : `text-slate-500 ${t.hover}`;
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-label={title}
      disabled={disabled}
      className={`${dim} ${padding} rounded-md flex items-center justify-center transition-colors ${baseColour} ${
        disabled ? 'opacity-40 cursor-not-allowed' : ''
      }`}
    >
      {children}
      {label && <span className="text-xs font-medium leading-none">{label}</span>}
    </button>
  );
}
