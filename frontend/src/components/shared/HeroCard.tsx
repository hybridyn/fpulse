/**
 * HeroCard — Dashboard-style KPI card.
 *
 * Bold gradient background, centred content, SVG icon at top, big value,
 * optional valueSuffix, footer line, optional progress bar. Visual shape
 * follows DashboardPage's HeroKPI so every page using this component
 * (Dashboard, Pool, Executions) feels like one visual family.
 *
 * Text is BLACK (slate-900 / slate-800) — design choice, per Siva's
 * feedback that white-on-gradient was less legible on darker shades.
 * Works on light-to-mid gradients (X-400 to X-600 range); don't push
 * the gradient darker than 700 or black text starts to struggle.
 *
 * Conventions:
 *   - DEV view: lighter gradients (X-400 → X-500)
 *   - PROD view: richer gradients (X-500 → X-600)
 *
 * Example:
 *   <HeroCard
 *     gradient="from-emerald-400 to-emerald-500"
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
  /** Tailwind gradient classes, e.g. "from-emerald-400 to-emerald-500". */
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

  return (
    <Element
      onClick={onClick}
      className={`relative overflow-hidden rounded-lg bg-gradient-to-br ${gradient} ${
        dense ? 'px-3 py-2' : 'px-4 py-2.5'
      } transition-all duration-300 hover:-translate-y-0.5 hover:shadow-lg ${
        onClick ? 'cursor-pointer text-left w-full' : ''
      }`}
      style={{
        boxShadow:
          '0 4px 10px -2px rgba(15,23,42,0.12), 0 12px 28px -6px rgba(15,23,42,0.25), inset 0 1px 0 rgba(255,255,255,0.18)',
      }}
    >
      {/* Decorative corner blooms for visual depth. Smaller now since the
          card itself is tighter — preserves the polished feel without
          dominating a low-density card. */}
      <div className="pointer-events-none absolute -top-4 -right-4 w-14 h-14 rounded-full bg-white/20 blur-xl" />
      <div className="pointer-events-none absolute -bottom-5 -left-3 w-16 h-16 rounded-full bg-white/10 blur-2xl" />

      {/* Top row: icon + label inline, side by side. Saves vertical space
          vs. the previous stacked layout while keeping both visible. */}
      <div className="relative flex items-center justify-center gap-1.5 text-slate-900">
        <span className="shrink-0">{icon}</span>
        <span className="text-xs font-bold uppercase tracking-wider">
          {label}
        </span>
      </div>

      {/* Value — bigger and bolder so the number is the visual anchor of
          the card, not the gradient backdrop. */}
      <div className="relative mt-1 flex items-baseline justify-center gap-1.5">
        <span className={`${dense ? 'text-2xl' : 'text-3xl'} font-extrabold tabular-nums leading-none text-slate-900`}>
          {value}
        </span>
        {valueSuffix && (
          <span className="text-xs font-semibold text-slate-800">{valueSuffix}</span>
        )}
      </div>

      {/* Optional progress bar — dark fill on a subtle dark track for
          on-black-text consistency. */}
      {typeof bar === 'number' && (
        <div className="relative w-full h-1 bg-slate-900/15 rounded-full mt-1.5 overflow-hidden">
          <div
            className="h-full rounded-full bg-slate-900/85 transition-all"
            style={{ width: `${Math.max(0, Math.min(100, bar))}%` }}
          />
        </div>
      )}

      {/* Footer line — supporting text, still legible. */}
      {footer && (
        <div className="relative text-xs font-medium text-slate-800 mt-1 text-center">
          {footer}
        </div>
      )}
    </Element>
  );
}
