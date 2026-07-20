/**
 * SectionBand + ActionChip — small layout primitives.
 *
 * Phase 1 foundation.
 *
 * SectionBand: alternating background wrapper for breaking up vast page
 * content visually. Pairs of <SectionBand tone="white"> / <SectionBand
 * tone="muted"> on a long page give the eye landmarks.
 *
 * ActionChip: small inline pill button for primary row actions (Run,
 * Edit, Open). Uses the same tone vocabulary as KPITile.
 */

import { ReactNode } from 'react';

// ─────────────────────────────────────────────────────────────────────
// SectionBand
// ─────────────────────────────────────────────────────────────────────

export interface SectionBandProps {
  tone?: 'white' | 'muted';
  /** Tightens vertical padding when true (default false = py-6). */
  compact?: boolean;
  /** Container class to constrain reading width inside the band. Default
   *  none — content fills the band. Pass `max-w-[80ch]` for prose. */
  contentClassName?: string;
  children: ReactNode;
}

export function SectionBand({ tone = 'white', compact = false, contentClassName, children }: SectionBandProps) {
  const bg = tone === 'muted' ? 'bg-slate-50/40' : 'bg-white';
  const py = compact ? 'py-3' : 'py-6';
  return (
    <section className={`w-full ${bg} ${py}`}>
      <div className={`px-6 ${contentClassName ?? ''}`}>{children}</div>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────────────
// ActionChip
// ─────────────────────────────────────────────────────────────────────

export type ChipTone = 'primary' | 'success' | 'amber' | 'danger' | 'ghost';

export interface ActionChipProps {
  /** Visible label; keep short ("Run", "Edit", "Open"). */
  label: string;
  /** Optional icon (12-14px, before label). */
  icon?: ReactNode;
  /** Click handler. When omitted with `href`, renders as anchor. */
  onClick?: () => void;
  /** Render as anchor instead of button. */
  href?: string;
  /** Tone — drives color. */
  tone?: ChipTone;
  /** When true, render the chip as disabled. */
  disabled?: boolean;
  /** Title for hover tooltip. */
  title?: string;
}

const CHIP_TONES: Record<ChipTone, string> = {
  primary: 'border-indigo-200 text-indigo-700 hover:bg-indigo-50 hover:border-indigo-300',
  success: 'border-emerald-200 text-emerald-700 hover:bg-emerald-50 hover:border-emerald-300',
  amber:   'border-amber-200 text-amber-800 hover:bg-amber-50 hover:border-amber-300',
  danger:  'border-red-200 text-red-700 hover:bg-red-50 hover:border-red-300',
  ghost:   'border-slate-200 text-slate-600 hover:bg-slate-50 hover:border-slate-300',
};

export function ActionChip({
  label, icon, onClick, href, tone = 'ghost', disabled, title,
}: ActionChipProps) {
  const cls = [
    'inline-flex items-center gap-1 px-2 py-1 text-xs font-semibold rounded-md border bg-white transition-colors',
    CHIP_TONES[tone],
    disabled ? 'opacity-50 cursor-not-allowed pointer-events-none' : '',
  ].join(' ');

  if (href && !disabled) {
    return (
      <a href={href} className={cls} title={title}>
        {icon}
        <span>{label}</span>
      </a>
    );
  }
  return (
    <button type="button" onClick={onClick} disabled={disabled} className={cls} title={title}>
      {icon}
      <span>{label}</span>
    </button>
  );
}

export default SectionBand;
