/**
 * HubTabs — centered tab strip used inside a page's existing 78px
 * banner so sibling sub-pages share secondary nav without spawning a
 * second header.
 *
 * Each member of a hub family (Workflows: Pipelines / Editor /
 * Executions; Connections: All Connections / Credentials) renders this
 * strip in the CENTER slot of its own page header. The page's title +
 * subtitle stay on the LEFT (driven by the active page itself), and
 * the page's actions stay on the RIGHT — same layout Insights and
 * Settings already use, but with a per-page title instead of a hub
 * family title. There is no separate "hub header" wrapping the page.
 *
 * Click → calls `onNavigate(page)` (the same handler the top sidebar
 * uses) so deep-linking to any tab still works through the URL hash.
 */
import React from 'react';

export interface HubTab<P extends string> {
  page: P;
  label: string;
  icon?: React.ReactNode;
  devOnly?: boolean;
  prodOnly?: boolean;
}

interface HubTabsProps<P extends string> {
  tabs: HubTab<P>[];
  active: P;
  onNavigate: (page: P) => void;
  environment?: 'dev' | 'prod';
  dark?: boolean;
}

export default function HubTabs<P extends string>({
  tabs,
  active,
  onNavigate,
  environment = 'dev',
  dark = false,
}: HubTabsProps<P>) {
  const isProd = environment === 'prod';
  const visibleTabs = tabs.filter((t) => {
    if (isProd && t.devOnly) return false;
    if (!isProd && t.prodOnly) return false;
    return true;
  });
  // One visible tab is the same as no tab strip — render nothing.
  if (visibleTabs.length <= 1) return null;

  return (
    <div className="flex justify-center items-center gap-0.5">
      {visibleTabs.map((t) => {
        const isActive = t.page === active;
        return (
          <button
            key={t.page}
            type="button"
            onClick={() => onNavigate(t.page)}
            title={t.label}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm font-semibold rounded-lg transition-all capitalize ${
              isActive
                ? dark
                  ? 'border-violet-400 text-violet-200 font-bold bg-gradient-to-b from-violet-400/30 to-violet-600/20 shadow-[inset_0_0_0_1.5px_rgba(167,139,250,0.55),inset_0_0_10px_rgba(139,92,246,0.30),inset_0_1px_0_rgba(255,255,255,0.22)]'
                  : 'text-white font-bold bg-gradient-to-b from-slate-600 to-slate-800 shadow-[inset_0_0_0_1.5px_rgba(148,163,184,0.65),inset_0_0_10px_rgba(100,116,139,0.35),inset_0_1px_0_rgba(255,255,255,0.22)]'
                : dark
                  ? 'border-transparent text-slate-500 hover:text-slate-300 hover:bg-white/[0.03]'
                  : 'border-transparent text-slate-900 font-bold hover:text-violet-700 hover:bg-violet-50/50'
            }`}
          >
            {t.icon}
            {/* 2026-06-10: label hides below xl so the strip shrinks to
                icon chips instead of colliding with the page-header
                action cluster at laptop / half-snapped widths. */}
            <span className="hidden xl:inline">{t.label}</span>
          </button>
        );
      })}
    </div>
  );
}

/* ── Pre-baked tab sets used across the app ─────────────────────────── */

const ICON_PIPELINES = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect width="8" height="8" x="3" y="3" rx="2" />
    <path d="M7 11v4a2 2 0 0 0 2 2h4" />
    <rect width="8" height="8" x="13" y="13" rx="2" />
  </svg>
);
const ICON_EDITOR = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 20h9" />
    <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
  </svg>
);
const ICON_EXECUTIONS = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="10" />
    <polygon points="10 8 16 12 10 16 10 8" />
  </svg>
);
const ICON_TEMPLATES = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="3" width="7" height="7" rx="1" />
    <rect x="14" y="3" width="7" height="7" rx="1" />
    <rect x="3" y="14" width="7" height="7" rx="1" />
    <path d="M14 17h7" />
    <path d="M17.5 14v7" />
  </svg>
);
const ICON_CONNECTIONS = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 11a9 9 0 0 1 9 9" />
    <path d="M4 4a16 16 0 0 1 16 16" />
    <circle cx="5" cy="19" r="1" />
  </svg>
);
const ICON_CREDENTIALS = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
    <path d="M7 11V7a5 5 0 0 1 10 0v4" />
  </svg>
);

/** Workflows family — Pipelines / Editor / Executions / Templates. */
export const WORKFLOWS_TABS: HubTab<'pipelines' | 'editor' | 'executions' | 'templates'>[] = [
  { page: 'pipelines',  label: 'Pipelines',  icon: ICON_PIPELINES },
  { page: 'editor',     label: 'Editor', devOnly: true, icon: ICON_EDITOR },
  { page: 'executions', label: 'Executions', icon: ICON_EXECUTIONS },
  { page: 'templates',  label: 'Templates',  devOnly: true, icon: ICON_TEMPLATES },
];

/** Connections family — All Connections / Credentials. */
export const CONNECTIONS_TABS: HubTab<'connections' | 'credentials'>[] = [
  { page: 'connections', label: 'All Connections', icon: ICON_CONNECTIONS },
  { page: 'credentials', label: 'Credentials', devOnly: true, icon: ICON_CREDENTIALS },
];
