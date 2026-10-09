/**
 * HeroCard — KPI card (neutral-surface standard).
 *
 * 2026-10-06 redesign: per the house card-color standard, the card itself
 * is a NEUTRAL white surface with a thin border — colour lives ONLY in the
 * value (and its matching icon), not in a filled gradient background. The
 * old bold gradient-fill look was dropped; the `gradient` prop is kept for
 * backward compatibility (every call site still passes it) and is now used
 * only to derive the accent colour of the value from its colour family.
 *
 * Example (unchanged call site — just renders neutral now):
 *   <HeroCard
 *     gradient="from-emerald-400 to-emerald-500"  // → emerald accent on value
 *     icon={<MyIcon />}
 *     label="Succeeded"
 *     value="42"
 *     valueSuffix="runs"
 *     footer="↑ 12% vs last week"
 *     bar={85}
 *   />
 */

import type { ReactNode } from 'react';

interface HeroCardProps {
  /** Legacy gradient classes, e.g. "from-emerald-400 to-emerald-500". Only
   *  the colour family (emerald/indigo/…) is read, to tint the value. */
  gradient: string;
  /** SVG icon (or any node) centered at top of the card. */
  icon: ReactNode;
  /** Small uppercase label — the metric name. */
  label: string;
  /** Big value — the headline number. */
  value: string;
  /** Optional secondary unit (%, / hour, waiting, running, ...). */
  valueSuffix?: string;
  /** Optional supporting line below the value. */
  footer?: string;
  /** 0–100 progress bar rendered above the footer. Omit to hide. */
  bar?: number;
  /** If provided, renders the card as a button and fires on click. */
  onClick?: () => void;
  /** Compact variant — tighter padding + smaller value. Used where a
   *  page shows a dense KPI strip (e.g. Storage) and the default size
   *  feels too tall. Other pages keep the default by omitting it. */
  dense?: boolean;
}

/* Accent colour per family — literal class names so Tailwind's content
   scanner keeps them (never build these strings dynamically). The value
   gets the text colour; the progress bar gets the matching fill. slate is
   the neutral fallback for muted metrics (e.g. Archived). */
const ACCENT_TEXT: Record<string, string> = {
  indigo: 'text-indigo-600', emerald: 'text-emerald-600', blue: 'text-blue-600',
  sky: 'text-sky-600', violet: 'text-violet-600', purple: 'text-purple-600',
  amber: 'text-amber-600', rose: 'text-rose-600', red: 'text-red-600',
  teal: 'text-teal-600', cyan: 'text-cyan-600', green: 'text-green-600',
  orange: 'text-orange-600', fuchsia: 'text-fuchsia-600', pink: 'text-pink-600',
  slate: 'text-slate-700',
};
const ACCENT_BAR: Record<string, string> = {
  indigo: 'bg-indigo-500', emerald: 'bg-emerald-500', blue: 'bg-blue-500',
  sky: 'bg-sky-500', violet: 'bg-violet-500', purple: 'bg-purple-500',
  amber: 'bg-amber-500', rose: 'bg-rose-500', red: 'bg-red-500',
  teal: 'bg-teal-500', cyan: 'bg-cyan-500', green: 'bg-green-500',
  orange: 'bg-orange-500', fuchsia: 'bg-fuchsia-500', pink: 'bg-pink-500',
  slate: 'bg-slate-400',
};

export default function HeroCard({
  gradient,
  icon,
  label,
  value,
  valueSuffix,
  footer,
  bar,
  onClick,
  dense,
}: HeroCardProps) {
  const Element: any = onClick ? 'button' : 'div';

  const family = (gradient.match(/from-([a-z]+)-\d/)?.[1]) || 'slate';
  const accentText = ACCENT_TEXT[family] || 'text-slate-700';
  const accentBar = ACCENT_BAR[family] || 'bg-slate-400';

  return (
    <Element
      onClick={onClick}
      className={`relative overflow-hidden rounded-lg bg-white border border-slate-200 shadow-sm ${
        dense ? 'px-3 py-2' : 'px-4 py-2.5'
      } transition-all duration-200 hover:-translate-y-0.5 hover:shadow-md ${
        onClick ? 'cursor-pointer text-left w-full' : ''
      }`}
    >
      {/* Top row: icon + label inline — both neutral, so the value is the
          only coloured element on the card. */}
      <div className="relative flex items-center justify-center gap-1.5 text-slate-500">
        <span className="shrink-0">{icon}</span>
        <span className="text-xs font-bold uppercase tracking-wider">
          {label}
        </span>
      </div>

      {/* Value — the one place colour lives (card-colour standard). */}
      <div className="relative mt-1 flex items-baseline justify-center gap-1.5">
        <span className={`${dense ? 'text-2xl' : 'text-3xl'} font-extrabold tabular-nums leading-none ${accentText}`}>
          {value}
        </span>
        {valueSuffix && (
          <span className="text-xs font-semibold text-slate-500">{valueSuffix}</span>
        )}
      </div>

      {/* Optional progress bar — accent fill on a light neutral track. */}
      {typeof bar === 'number' && (
        <div className="relative w-full h-1 bg-slate-100 rounded-full mt-1.5 overflow-hidden">
          <div
            className={`h-full rounded-full ${accentBar} transition-all`}
            style={{ width: `${Math.max(0, Math.min(100, bar))}%` }}
          />
        </div>
      )}

      {/* Footer line — supporting text, neutral. */}
      {footer && (
        <div className="relative text-xs font-medium text-slate-500 mt-1 text-center">
          {footer}
        </div>
      )}
    </Element>
  );
}
