/**
 * EnvTableHead — renders a <thead> styled per the active environment.
 *
 * Part of the theme v2 rollout (docs/DESIGN_THEME_V2.md). Centralises the
 * DEV/PROD thead treatment so we don't duplicate class strings across every
 * page with a table.
 *
 *   DEV  — soft lavender bg + deep violet text (light theme)
 *   PROD — solid navy bg + NAPLES yellow text
 *
 * Both environments share the same typography (uppercase, bold, small-caps
 * tracking) so the shape of a row scans the same regardless of env; only the
 * colour changes. Pair with the matching `envTableBorderClass(env)` helper
 * below when you need the outer table wrapper to pick up the per-env border.
 */
import type { ReactNode } from 'react';

export type Env = 'dev' | 'prod';

interface Props {
  env: Env;
  children: ReactNode;
  className?: string;
  sticky?: boolean;
}

const DEV_CLASSES =
  'bg-thead-dev-bg text-thead-dev-text border-b-2 border-thead-dev-border ' +
  'text-xs uppercase tracking-wider font-bold';

const PROD_CLASSES =
  'bg-thead-prod-bg text-thead-prod-text border-b-2 border-thead-prod-border ' +
  'text-xs uppercase tracking-wider font-bold';

export function EnvTableHead({ env, children, className = '', sticky = false }: Props) {
  const base = env === 'prod' ? PROD_CLASSES : DEV_CLASSES;
  const stickyClass = sticky ? 'sticky top-0 z-10' : '';
  return <thead className={`${base} ${stickyClass} ${className}`.trim()}>{children}</thead>;
}

/**
 * Helper for the outer table wrapper. Returns the border classes to apply
 * to the <div> or <table> that contains the rows, so the thicker env-coloured
 * rule is visible on all four sides, not just under the header.
 *
 * Tailwind can't resolve `border-thead-${env}-border` dynamically at build
 * time — the safelist would get unwieldy fast — so we hand back the two
 * concrete class strings here and let the page pick one.
 */
export function envTableBorderClass(env: Env): string {
  return env === 'prod'
    ? 'border-2 border-thead-prod-border'
    : 'border-2 border-thead-dev-border';
}

/**
 * Helper for the "card" wrapper style used by most list pages today. Most
 * pages wrap their <table> in a <div className="bg-white rounded-xl border
 * border-slate-200 ..."> — this returns the env-aware replacement to keep
 * the rounded-corner + shadow + white-background pattern but with the
 * thicker, coloured rule.
 */
export function envTableCardClass(env: Env): string {
  const border = envTableBorderClass(env);
  return `bg-white rounded-xl shadow-sm overflow-hidden ${border}`;
}
