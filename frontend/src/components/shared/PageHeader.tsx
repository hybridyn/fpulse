/**
 * PageHeader — canonical sticky 78px page-header shell.
 *
 * 2026-05-19 (P1 #1 of PAGE_BY_PAGE_AUDIT.md): every list / detail page in
 * the OSS shell builds its own 3-column header grid (`grid-cols-3` or
 * `[1fr_auto_1fr]`) with a title cluster on the left, an optional centered
 * tab strip, and an actions cluster on the right. Counted 15+ ad-hoc copies
 * across DashboardPage, ProjectsPage, PipelinesPage, TemplatesPage,
 * ConnectionsPage, CredentialsPage, SettingsPage, AccountPage, HelpPage,
 * AIPage, ReportsPage, NotificationsPage, ExecutionsPage, Pool, Lineage.
 *
 * As of the 2026-06-17 sweep this is the canonical header for every
 * top-level page. (The standalone CertMatrixPage was folded into the Trust
 * tab and removed.)
 *
 * Layout invariants (mirror what the inline copies built):
 *   - Sticky top with z-30 so it stays above scrolling content.
 *   - 78px fixed height; horizontal padding `px-8` matches the canvas spec.
 *   - DEV gradient = `from-slate-200 to-slate-300`, PROD = `slate-900`
 *     with a red PROD pill. Pages that don't take an `environment` prop
 *     default to DEV gradient.
 *   - Three columns: title cluster | optional centered slot | optional
 *     right-side actions. When `tabs` is omitted the layout collapses to
 *     `flex justify-between`.
 *
 * Adoption (2026-06-17 sweep complete): every top-level page renders through
 * this component — Dashboard, Pipelines, Projects, Templates, Connections,
 * Credentials, Settings, Account, Help, Insights (AIPage), Executions, Pool,
 * Lineage. Detail / back-nav sub-views (e.g. a run-detail or connection-detail
 * screen) intentionally keep their own compact headers and are out of scope.
 *
 * Migration recipe (for any NEW page): replace the bespoke
 * `sticky top-0 z-30 ... h-[78px]` block with
 * `<PageHeader environment icon title subtitle titleAccessory tabs actions />`,
 * moving the tab strip into `tabs`, right-side buttons into `actions`, and
 * trailing pills (TierChip etc.) into `titleAccessory`.
 */

import type { ReactNode } from 'react';
import { useDarkMode } from '../../hooks/useDarkMode';

export interface PageHeaderProps {
  /** Optional icon (SVG node) rendered to the left of the title. */
  icon?: ReactNode;
  /** Page title — rendered as an `<h1>` for SEO + screen-readers. */
  title: ReactNode;
  /** Optional one-line subtitle below the title. */
  subtitle?: ReactNode;
  /** Optional row rendered BELOW the subtitle (e.g. a filter/scope chip, a
   *  breadcrumb, a project-context pill). Lets pages with a bespoke header
   *  sub-row migrate to this shell without losing that control. Kept compact
   *  so the header stays within its fixed height. */
  belowTitle?: ReactNode;
  /** Optional trailing inline element (e.g. `<TierChip>`, version badge). */
  titleAccessory?: ReactNode;
  /** Centered slot — typically `<HubTabs>` or an inline tab strip. */
  tabs?: ReactNode;
  /** Right-side actions cluster (buttons, toggles). */
  actions?: ReactNode;
  /**
   * Environment determines the gradient. Free defaults to 'dev'; Plus
   * pages that opt-in pass 'prod' explicitly so the slate-900 + red PROD
   * pill render automatically.
   */
  environment?: 'dev' | 'prod';
  /** Hide the env badge entirely when caller wants a plain header. */
  hideEnvBadge?: boolean;
}

export default function PageHeader({
  icon,
  title,
  subtitle,
  belowTitle,
  titleAccessory,
  tabs,
  actions,
  environment = 'dev',
  hideEnvBadge = false,
}: PageHeaderProps) {
  const dark = useDarkMode();
  const isProd = environment === 'prod';

  const bg = isProd
    ? 'bg-slate-900 border-slate-800'
    : dark
      ? 'bg-[#0F172A] border-white/[0.06]'
      : 'bg-gradient-to-b from-slate-200 to-slate-300 border-slate-400/70';
  const titleColor = isProd || dark ? 'text-slate-100' : 'text-slate-800';
  const subtitleColor = isProd || dark ? 'text-slate-400' : 'text-slate-500';

  return (
    <div className={`sticky top-0 z-30 border-b ${bg}`}>
      <div
        className={`px-8 h-[78px] grid items-center gap-4 ${
          tabs ? 'grid-cols-[1fr_auto_1fr]' : 'grid-cols-[1fr_auto]'
        }`}
      >
        {/* ── Title cluster ──────────────────────────────────────── */}
        <div className="min-w-0 flex items-center gap-3">
          {icon && <div className="shrink-0">{icon}</div>}
          <div className="min-w-0">
            <h1 className={`text-xl font-bold flex items-center gap-2 truncate ${titleColor}`}>
              <span className="truncate">{title}</span>
              {isProd && !hideEnvBadge && (
                <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-red-500 text-white tracking-wider shrink-0">
                  PROD
                </span>
              )}
              {titleAccessory && <span className="shrink-0">{titleAccessory}</span>}
            </h1>
            {subtitle && <p className={`text-xs mt-0.5 truncate ${subtitleColor}`}>{subtitle}</p>}
            {belowTitle && <div className="mt-1">{belowTitle}</div>}
          </div>
        </div>

        {/* ── Centered tab strip slot ───────────────────────────── */}
        {tabs && <div className="justify-self-center">{tabs}</div>}

        {/* ── Right-side actions cluster ────────────────────────── */}
        <div className="justify-self-end flex items-center gap-2 shrink-0">{actions}</div>
      </div>
    </div>
  );
}
