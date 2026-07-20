/**
 * Canonical page-icon registry.
 *
 * 2026-05-22 — single source of truth for the icon that represents
 * each page. The icon-consistency memory rule (2026-05-12) was being
 * silently violated because every surface (sidebar, page header,
 * Cmd-K palette, sub-tab strips) maintained its own copy of the
 * icon for each page. When the user pointed out the Editor header
 * showing the AI lightning bolt instead of the canonical pencil, the
 * underlying drift surface became unavoidable. This module fixes the
 * drift at the source.
 *
 * Anti-pattern this replaces:
 *   * Sidebar.tsx maintained 11 inline `<svg>` blocks for the same
 *     11 page icons.
 *   * Each PageName.tsx file had a separate <svg> in its header.
 *   * GlobalSearch.tsx had a third copy.
 *   * SettingsPage / HelpPage had section-divider icons that
 *     occasionally cribbed the lightning bolt for non-AI sections.
 *
 * New pattern:
 *   import { PageIcon } from '@/components/shared/PageIcons';
 *   <PageIcon page="editor" size={20} />
 *
 * Add a new page → add one entry here. Every surface picks it up
 * automatically.
 *
 * The lightning-bolt polygon `points="13 2 3 14 12 14 11 22 21 10 12
 * 10 13 2"` is reserved for:
 *   * The F-Pulse brand mark
 *   * AI-specific affordances (AI Assist, AI Fix, the Copilot
 *     avatar, AI-transform node type)
 * NEVER use it as a page icon or a section divider for non-AI
 * content. The Editor page used to do that and it misled users
 * into reading the Editor as "AI-powered Editor" (it isn't — the
 * Copilot is a separate side panel).
 */

import type { ReactNode } from 'react';

/**
 * All page ids referenced by the canonical registry. Kept as a
 * string union rather than importing the Page enum so this module
 * has zero coupling to the router — making it safe to render from
 * anywhere (sidebar, Cmd-K, page headers, breadcrumbs, etc.) without
 * a circular dependency risk.
 */
export type PageIconKey =
  | 'dashboard'
  | 'projects'
  | 'pipelines'      // a.k.a. Workflows
  | 'editor'
  | 'executions'
  | 'templates'
  | 'connections'
  | 'credentials'
  | 'pool'
  | 'storage'        // 2026-05-23 (Y4) — workspace datastore page
  | 'insights'       // page id is `ai` in App routing, but conceptually "Insights"
  | 'ai'             // alias to insights
  | 'settings'
  | 'help';

interface IconContent {
  /** SVG path / shape elements for the icon. Wrapped at render time. */
  children: ReactNode;
  /**
   * Human-readable description of why this icon. Helps future drift
   * audits — if someone proposes swapping the icon, the description
   * makes the existing semantic explicit.
   */
  meaning: string;
}

/**
 * Canonical icon SVG content per page. Sized + stroked at render
 * time via the <PageIcon> wrapper below; this map only carries the
 * geometry so the same path serves 16px / 17px / 20px / 32px
 * surfaces without duplicate SVG blocks.
 */
const PAGE_ICON_CONTENT: Record<PageIconKey, IconContent> = {
  dashboard: {
    meaning: '4-rect grid — operational cockpit / overview at a glance.',
    children: (
      <>
        <rect x="3" y="3" width="7" height="7" />
        <rect x="14" y="3" width="7" height="7" />
        <rect x="3" y="14" width="7" height="7" />
        <rect x="14" y="14" width="7" height="7" />
      </>
    ),
  },
  projects: {
    meaning: 'Folder — group of related pipelines + connections.',
    children: (
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
    ),
  },
  pipelines: {
    meaning: 'Two-node DAG — saved workflows / pipelines list.',
    children: (
      <>
        <rect width="8" height="8" x="3" y="3" rx="2" />
        <path d="M7 11v4a2 2 0 0 0 2 2h4" />
        <rect width="8" height="8" x="13" y="13" rx="2" />
      </>
    ),
  },
  editor: {
    meaning: 'Pencil — the page where you AUTHOR a pipeline. Never the lightning bolt (that means AI / Copilot).',
    children: (
      <>
        <path d="M12 20h9" />
        <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
      </>
    ),
  },
  executions: {
    meaning: 'Clock + play head — past + in-flight run history.',
    children: (
      <>
        <circle cx="12" cy="12" r="10" />
        <polygon points="10 8 16 12 10 16 10 8" />
      </>
    ),
  },
  templates: {
    meaning: '4-rect grid with lines — pick-from-library affordance.',
    children: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" />
        <path d="M14 17h7" />
        <path d="M17.5 14v7" />
      </>
    ),
  },
  connections: {
    meaning: 'Broadcast wave — saved external connections.',
    children: (
      <>
        <path d="M4 11a9 9 0 0 1 9 9" />
        <path d="M4 4a16 16 0 0 1 16 16" />
        <circle cx="5" cy="19" r="1" />
      </>
    ),
  },
  credentials: {
    meaning: 'Padlock — secrets store.',
    children: (
      <>
        <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
        <path d="M7 11V7a5 5 0 0 1 10 0v4" />
      </>
    ),
  },
  pool: {
    meaning: 'Inbox tray — worker pool / queue depth.',
    children: (
      <>
        <rect x="2" y="7" width="20" height="14" rx="2" />
        <path d="M16 3h-8l-2 4h12z" />
      </>
    ),
  },
  storage: {
    meaning: 'Stacked cylinders — workspace datastore: files, managed tables, pipeline outputs.',
    children: (
      <>
        <ellipse cx="12" cy="5" rx="9" ry="3" />
        <path d="M3 5v6c0 1.66 4.03 3 9 3s9-1.34 9-3V5" />
        <path d="M3 11v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6" />
      </>
    ),
  },
  insights: {
    meaning: 'Sparkles — Activity / Trust / Reports / AI Provider hub. The sparkle here represents "AI-assisted insights" so the lightning-bolt family is actually appropriate.',
    children: (
      <>
        <path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.582a.5.5 0 0 1 0 .962L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z" />
        <path d="M20 3v4" />
        <path d="M22 5h-4" />
        <path d="M4 17v2" />
        <path d="M5 18H3" />
      </>
    ),
  },
  ai: {
    meaning: 'Alias for "insights" (Page id stays `ai` for back-compat deep links).',
    children: (
      <>
        <path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.582a.5.5 0 0 1 0 .962L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z" />
        <path d="M20 3v4" />
        <path d="M22 5h-4" />
        <path d="M4 17v2" />
        <path d="M5 18H3" />
      </>
    ),
  },
  settings: {
    meaning: 'Cog — workspace + system preferences.',
    children: (
      <>
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
      </>
    ),
  },
  help: {
    meaning: 'Question circle — runbook + docs.',
    children: (
      <>
        <circle cx="12" cy="12" r="10" />
        <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </>
    ),
  },
};

interface PageIconProps {
  page: PageIconKey;
  size?: number;
  className?: string;
  /** Stroke width override — defaults to 2. Larger sizes (32px+)
   *  look better at 1.6; smaller sizes (12px) sometimes want 2.5. */
  strokeWidth?: number;
  title?: string;
}

/**
 * Renders the canonical icon for a page. Use everywhere the user
 * sees a "this is page X" affordance — sidebar nav, page-title
 * header, Cmd-K palette, breadcrumbs, sub-tab strips.
 */
export function PageIcon({
  page,
  size = 17,
  className,
  strokeWidth = 2,
  title,
}: PageIconProps) {
  const content = PAGE_ICON_CONTENT[page];
  if (!content) {
    // Defensive — render nothing if a caller asks for an unknown
    // page id. The TS type guards against this in production but
    // dynamic strings from deep links can slip through.
    return null;
  }
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-label={title}
      role={title ? 'img' : 'presentation'}
    >
      {content.children}
    </svg>
  );
}

/**
 * Quick lookup of the semantic meaning of a page's icon. Helpful
 * for accessibility tooltips and developer onboarding.
 */
export function pageIconMeaning(page: PageIconKey): string | undefined {
  return PAGE_ICON_CONTENT[page]?.meaning;
}
