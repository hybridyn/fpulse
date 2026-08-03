import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { WORKSPACES_ENABLED } from '../config/edition';
import { canAccessAdmin, canAccessAdminInEnv, canAccessProd, hasPermission, roleLabel } from '../auth/permissions';
import { useDarkMode } from '../hooks/useDarkMode';
import { notificationHref } from '../lib/notificationHref';
// 2026-05-19 (OSS-9 of PAGE_BY_PAGE_AUDIT.md): `Page` is now imported
// from the canonical `../types` instead of being redefined here. The
// `as any` cast that App.tsx used to bridge the two unions is gone.
import type { Page } from '../types';
// 2026-06-05 — Steward (Archeologist) findings surface in the header next
// to the notification bell. The Steward is the OSS-tier reliability +
// learning layer; see docs/steward/overview.md.
import StewardBadge from './StewardBadge';

// Single source of truth for the user-facing version string. Bumped
// in lockstep with the backend's __version__ and the Docker tag. Imported
// by anywhere the version is shown (Sidebar chip, LoginPage footer,
// Settings → About, HelpPage footer).
const APP_VERSION = '1.0.0';

type Environment = 'dev' | 'prod';

interface HeaderNavProps {
  activePage: Page;
  onNavigate: (page: Page) => void;
  user?: { name: string; email: string; role: string } | null;
  onLogout?: () => void;
  environment?: Environment;
  onEnvironmentChange?: (env: Environment) => void;
  tier?: 'free' | 'plus';
}

interface NavItem {
  page: Page;
  label: string;
  prodLabel?: string;
  icon: React.ReactNode;
  section?: string;
  devOnly?: boolean;
  prodOnly?: boolean;
  adminOnly?: boolean;
  approverOnly?: boolean;
  /**
   * When set, this entry renders as a parent group with a hover-dropdown
   * containing the listed children. Clicking the parent navigates to
   * `page` (its default landing page); hovering reveals the children.
   * The parent's active-state lights up whenever the active page is the
   * parent's `page` or any child's `page`. Children themselves obey the
   * same `devOnly` / `prodOnly` / `adminOnly` filters as top-level items.
   * If after filtering the visible children count drops to 0 or 1, the
   * group collapses to a flat single button (no dropdown).
   */
  children?: NavItem[];
}

const NAV_ITEMS: NavItem[] = [
  // ── Home ──
  {
    page: 'dashboard',
    label: 'Dashboard',
    // Unified DEV + PROD label. Dashboard is the universal term; renaming to
    // "Overview" in PROD added cognitive tax for users who switch envs.
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" />
        <rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" />
      </svg>
    ),
  },
  // ── Author (DEV: Projects → Workflows → Editor) ──
  {
    page: 'projects',
    label: 'Projects',
    devOnly: true,
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
      </svg>
    ),
  },
  // ── Workflows group: Pipelines / Editor / Executions ──
  // Hover-dropdown grouping introduced May 9 2026. The parent's `page`
  // field is the default landing page when the user clicks the parent
  // (we keep 'pipelines' so the click-to-navigate still works the same
  // way it did when these were three top-level entries). Children carry
  // their own filters — Editor stays DEV-only, Executions stays in both.
  {
    page: 'pipelines',
    label: 'Workflows',
    prodLabel: 'Deployed',
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect width="8" height="8" x="3" y="3" rx="2" />
        <path d="M7 11v4a2 2 0 0 0 2 2h4" />
        <rect width="8" height="8" x="13" y="13" rx="2" />
      </svg>
    ),
    children: [
      {
        page: 'pipelines',
        label: 'Pipelines',
        prodLabel: 'Deployed',
        icon: (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect width="8" height="8" x="3" y="3" rx="2" />
            <path d="M7 11v4a2 2 0 0 0 2 2h4" />
            <rect width="8" height="8" x="13" y="13" rx="2" />
          </svg>
        ),
      },
      {
        page: 'editor',
        label: 'Editor',
        devOnly: true,
        icon: (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
          </svg>
        ),
      },
      {
        page: 'executions',
        label: 'Executions',
        icon: (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10" />
            <polygon points="10 8 16 12 10 16 10 8" />
          </svg>
        ),
      },
      {
        page: 'templates',
        label: 'Templates',
        devOnly: true,
        icon: (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="3" width="7" height="7" rx="1" />
            <rect x="14" y="3" width="7" height="7" rx="1" />
            <rect x="3" y="14" width="7" height="7" rx="1" />
            <path d="M14 17h7" />
            <path d="M17.5 14v7" />
          </svg>
        ),
      },
    ],
  },
  // ── Connections group: All Connections / Credentials ──
  // Same hover-dropdown pattern. On PROD, Credentials is hidden
  // (devOnly), which collapses the group to a single visible child —
  // the render falls back to a flat button with no dropdown so the
  // PROD nav stays uncluttered.
  {
    page: 'connections',
    label: 'Connections',
    section: 'divider',
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 11a9 9 0 0 1 9 9" /><path d="M4 4a16 16 0 0 1 16 16" /><circle cx="5" cy="19" r="1" />
      </svg>
    ),
    children: [
      {
        page: 'connections',
        label: 'All Connections',
        icon: (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 11a9 9 0 0 1 9 9" /><path d="M4 4a16 16 0 0 1 16 16" /><circle cx="5" cy="19" r="1" />
          </svg>
        ),
      },
      // Credentials lives in DEV for every tier. Free tier: it's the
      // only secret store they have. Plus tier DEV: kept for the
      // legacy-transition period so developers can see / migrate old
      // credentials. On PROD + Plus, the entry is hidden because Vault
      // is the clean, audited, rotation-capable replacement — and PROD
      // shouldn't expose two parallel stores.
      {
        page: 'credentials',
        label: 'Credentials',
        devOnly: true,
        icon: (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
            <path d="M7 11V7a5 5 0 0 1 10 0v4" />
          </svg>
        ),
      },
    ],
  },
  // Variables removed — pipeline-level in Editor, global in Settings
  // ── Admin & Ops ──
  // Pool is visible to EVERY authenticated user (including Free / DEV
  // developers) so they can see queue pressure affecting their own
  // runs. Only the create / edit alert-rule controls are admin-gated
  // (see ExecutionPoolPage: the "New Alert Rule" button checks
  // canAccessAdmin). Alerts themselves remain PROD-only — firing alerts
  // in DEV would train users to ignore them.
  {
    page: 'pool' as Page,
    label: 'Pool',
    section: 'divider',
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="2" y="7" width="20" height="14" rx="2" /><path d="M16 3h-8l-2 4h12z" />
      </svg>
    ),
  },
  // 2026-05-23 (Y4): Storage — workspace datastore. Files (uploads),
  // Managed Tables (Parquet, addressable by schema.name from
  // local_table_source/sink), and Pipeline Outputs grouped by run.
  // Sits between Pool and Insights because it's an operational
  // surface: "where is my data?".
  {
    page: 'storage' as Page,
    label: 'Storage',
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <ellipse cx="12" cy="5" rx="9" ry="3" />
        <path d="M3 5v6c0 1.66 4.03 3 9 3s9-1.34 9-3V5" />
        <path d="M3 11v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6" />
      </svg>
    ),
  },
  {
    // Insights — consolidates Activity / Trust / Reports / AI Provider
    // under one entry. Replaces three previous top-level entries
    // (Trust, Activity, Reports) and was briefly named "AI-Hub" until
    // PR 4 (May 17 2026): leading with "AI" in the top nav made OSS
    // visitors think the pipeline builder required an LLM, contrary to
    // the locked "AI assistance, not AI dependency" positioning. The
    // page id stays `ai` for back-compat with deep links — only the
    // sidebar label changes.
    page: 'ai' as Page,
    label: 'Insights',
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.582a.5.5 0 0 1 0 .962L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z" />
        <path d="M20 3v4" />
        <path d="M22 5h-4" />
        <path d="M4 17v2" />
        <path d="M5 18H3" />
      </svg>
    ),
  },
  {
    page: 'settings',
    label: 'Settings',
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
      </svg>
    ),
  },
  {
    page: 'help',
    label: 'Help',
    prodLabel: 'Runbook',
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10" />
        <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
    ),
  },
  // Trust + Activity entries removed — both live as subtabs under the new
  // AI hub above (May 1 2026 consolidation). Legacy /#trust and /#activity
  // routes still resolve via App.tsx (open AI hub on the matching tab).
];

export default function Sidebar({ activePage, onNavigate, user, onLogout, environment = 'dev', onEnvironmentChange, tier = 'free' }: HeaderNavProps) {
  const isPlus = tier === 'plus';
  const isProd = environment === 'prod';
  const dark = useDarkMode();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  // ── Workspace switcher ──
  const [workspaces, setWorkspaces] = useState<any[]>([]);
  const [currentWsId, setCurrentWsId] = useState(() => localStorage.getItem('fpulse_workspace_id') || 'default');
  const [wsDropdownOpen, setWsDropdownOpen] = useState(false);
  const wsDropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // OSS is single-operator — no workspace switcher, so don't even fetch the
    // list. Gated on the edition flag so a Plus build restores it.
    if (!user || !WORKSPACES_ENABLED) return;
    api.listWorkspaces().then(ws => setWorkspaces(ws || [])).catch(() => {});
  }, [user]);

  // Close workspace dropdown on outside click
  useEffect(() => {
    if (!wsDropdownOpen) return;
    const handler = (e: MouseEvent) => {
      if (wsDropdownRef.current && !wsDropdownRef.current.contains(e.target as Node)) {
        setWsDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [wsDropdownOpen]);

  const switchWorkspace = (wsId: string) => {
    setCurrentWsId(wsId);
    localStorage.setItem('fpulse_workspace_id', wsId);
    setWsDropdownOpen(false);
    // Reload the page to pick up the new workspace context
    window.location.reload();
  };

  const currentWs = workspaces.find(w => w.id === currentWsId);
  const currentWsName = currentWs?.name || 'Default';

  // User popover — avatar click opens a small card with identity + logout.
  // Replaces the old "avatar = logout button" pattern which was easy to hit
  // by accident and hid who you were actually logged in as.
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const userMenuRef = useRef<HTMLDivElement>(null);

  // Close the popover on outside click / Escape. Ref-scoped so clicks inside
  // the popover (e.g. on the Logout button) aren't swallowed.
  useEffect(() => {
    if (!userMenuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (userMenuRef.current && !userMenuRef.current.contains(e.target as Node)) {
        setUserMenuOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setUserMenuOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [userMenuOpen]);

  // ── Notification bell ──
  const [unreadCount, setUnreadCount] = useState(0);
  const [notifOpen, setNotifOpen] = useState(false);
  const [notifications, setNotifications] = useState<any[]>([]);
  const notifRef = useRef<HTMLDivElement>(null);

  const refreshUnread = useCallback(() => {
    if (!user) return;
    api.getUnreadCount().then(r => setUnreadCount(r.unread ?? 0)).catch(() => {});
  }, [user]);

  // Poll unread count every 30s — but only while the tab is visible.
  // 2026-05-19 (P1 #15 of PAGE_BY_PAGE_AUDIT.md): without this gate, a tab
  // left open overnight fires ~2 880 /api/notifications/unread-count calls
  // to no purpose. App.tsx already uses this exact pattern for the license
  // cache; the bell missed it. We also refresh immediately on a hidden→
  // visible transition so the user sees an accurate count the instant
  // they return to the tab.
  // Also listens for `fpulse:notifications-changed` (dispatched by the
  // NotificationsPage on mark/clear/delete) so the bell count reflects
  // mutations without waiting for the next poll. Per P1 #6.
  useEffect(() => {
    if (!user) return;
    refreshUnread();
    let interval: number | undefined;
    const startPolling = () => {
      if (interval !== undefined) return;
      interval = window.setInterval(refreshUnread, 30000);
    };
    const stopPolling = () => {
      if (interval === undefined) return;
      clearInterval(interval);
      interval = undefined;
    };
    const onVisibility = () => {
      if (document.hidden) {
        stopPolling();
      } else {
        refreshUnread();
        startPolling();
      }
    };
    if (!document.hidden) startPolling();
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('fpulse:notifications-changed', refreshUnread);
    return () => {
      stopPolling();
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('fpulse:notifications-changed', refreshUnread);
    };
  }, [refreshUnread, user]);

  // Load notifications when bell opened
  useEffect(() => {
    if (notifOpen && user) {
      api.listNotifications(false, 20).then(setNotifications).catch(() => {});
    }
  }, [notifOpen, user]);

  // Close notification dropdown on outside click / Escape (Z14, 2026-05-23).
  //
  // The previous bubble-phase mousedown listener didn't fire when the user
  // clicked inside the React Flow canvas (or any component that calls
  // stopPropagation on mousedown — there are several across the app).
  // Capture-phase ('true' arg) sees the event BEFORE any handler can stop
  // it, so the dropdown closes reliably no matter where outside the user
  // clicks. Pointerdown picks up touch input.
  useEffect(() => {
    if (!notifOpen) return;
    const handler = (e: Event) => {
      if (notifRef.current && !notifRef.current.contains(e.target as Node)) {
        setNotifOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setNotifOpen(false);
    };
    document.addEventListener('mousedown', handler, true);
    document.addEventListener('pointerdown', handler, true);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', handler, true);
      document.removeEventListener('pointerdown', handler, true);
      document.removeEventListener('keydown', onKey);
    };
  }, [notifOpen]);

  const handleMarkRead = async (id: string) => {
    await api.markNotificationRead(id).catch(() => {});
    setNotifications(prev => prev.map(n => n.id === id ? { ...n, is_read: true } : n));
    refreshUnread();
  };

  const handleMarkAllRead = async () => {
    await api.markAllNotificationsRead().catch(() => {});
    setNotifications(prev => prev.map(n => ({ ...n, is_read: true })));
    setUnreadCount(0);
  };

  // RBAC gating.
  //
  // History note: this used to be `isPlus && !!user` so the entire role
  // model only kicked in once a Plus license was active. That made sense
  // when OSS had no user accounts at all, but we now seed real users with
  // real roles in OSS too — and the Admin / Approvals pages still expose
  // sensitive surface in free tier (user management, license activation,
  // audit trail). So role gating runs whenever there is a logged-in user.
  //
  // No-user case (dev autologin bypass): show everything — there is no
  // identity to gate on, and the backend will still 401/403 anything that
  // really requires auth.
  const rbacActive = !!user;
  // On Plus, Admin lives in PROD — hide the nav link while the user is
  // in DEV so they don't click through to a forced redirect. On Free/OSS
  // there is no PROD, so Admin stays available in DEV.
  const showAdmin = !rbacActive || canAccessAdminInEnv(user, environment, isPlus);
  // Approvals page is only meaningful when there's a PROD environment +
  // an approval workflow in play — i.e. F-Pulse. On Free tier, no
  // approvals exist (per project_free_vs_plus_approval_matrix), so the
  // page would be permanently empty. Hide the nav link entirely.
  const showApprover = isPlus && (!rbacActive || hasPermission(user, 'prod', 'approve'));
  // PROD button visibility/interactivity is split into TWO concepts now:
  //   prodLicensed   → does the server have a Plus license?
  //   prodRoleAllowed → does THIS user's role include PROD access?
  //
  // The button is always rendered. The click handler then routes:
  //   • free tier        → open the upgrade modal
  //   • Plus + role no   → noop with tooltip (role-blocked)
  //   • Plus + role yes  → actually switch environment
  const prodLicensed = isPlus;
  const prodRoleAllowed = !rbacActive || canAccessProd(user);
  // True only if both license and role allow it. Used for the "active" styling.
  const prodAllowed = prodLicensed && prodRoleAllowed;
  // Reason the PROD modal is open, so we can render the right message:
  //   'free' → server has no Plus license; user needs to upgrade
  //   'role' → Plus is active but THIS user's role / env allow-list denies PROD
  //   null   → modal closed
  // Prior bug: the role-denied branch was a silent no-op, so a developer
  // sandboxed to `environments=['dev']` on a Plus server would click PROD
  // and see absolutely nothing happen. The tooltip explained it but only
  // on hover — users clicking from touch devices (or just not hovering
  // long enough) thought the button was broken. Routing both blocked
  // states through the same modal gives every click visible feedback.
  const [blockedReason, setBlockedReason] = useState<'free' | 'role' | null>(null);
  const upgradeModalOpen = blockedReason !== null;
  const closeBlockedModal = () => setBlockedReason(null);

  const handleProdClick = () => {
    // Free tier — show the upgrade prompt instead of silently failing
    if (!prodLicensed) {
      setBlockedReason('free');
      return;
    }
    // Plus tier but role / environment allow-list can't reach PROD —
    // explain *why* in a modal so the user knows to contact their admin.
    if (!prodRoleAllowed) {
      setBlockedReason('role');
      return;
    }
    onEnvironmentChange?.('prod');
  };

  // Top-level + child filter share the same env / role rules.
  const passesFilters = (item: NavItem): boolean => {
    if (isProd && item.devOnly) return false;
    if (!isProd && item.prodOnly) return false;
    if (item.adminOnly && !showAdmin) return false;
    if (item.approverOnly && !showApprover) return false;
    return true;
  };
  const visibleItems = NAV_ITEMS.filter(passesFilters).map(item => {
    // Recurse into children so devOnly / prodOnly children are hidden in
    // the wrong env. The top nav itself renders the parent as a flat
    // button regardless of children count — the in-page tab strip is the
    // sole secondary-nav surface. We still need the visible-children list
    // available so the parent's active-state highlights correctly when
    // the user is on a descendant page.
    if (!item.children) return item;
    const visibleChildren = item.children.filter(passesFilters);
    if (visibleChildren.length === 0) {
      return { ...item, children: undefined };
    }
    return { ...item, children: visibleChildren };
  });

  return (
    <div className={`h-16 flex items-center px-4 gap-1 shrink-0 shadow-sm relative transition-colors ${
      isProd
        ? dark
          ? 'bg-[#1e3a5f] border-b-2 border-blue-400'
          : 'bg-slate-900 border-b border-slate-700'
        : dark
          ? 'bg-slate-900 border-b border-slate-700'
          : 'bg-gradient-to-b from-slate-100 to-slate-200 border-b border-transparent'
    }`}>
      {/* Brand plate — 2026-05-25 (v5: bg matches header):
            Plate background now mirrors the surrounding header bg so it
            visually "embeds" in the menu strip — the purple ring + glow
            become the only boundary marker, like a labelled region
            carved out of the header. Inner logo div KEEPS its white bg
            so the brand mark stays crisp against any plate color.
            ONLY the logo flips. PROD swaps the ring to red. */}
      <div
        className={`flex items-center gap-2 pl-1 pr-3 py-1 rounded-xl mr-3 shrink-0 cursor-pointer transition-shadow ${
          isProd
            ? dark
              ? 'bg-[#1e3a5f] ring-2 ring-red-400/50 shadow-[0_0_12px_rgba(239,68,68,0.25)]'
              : 'bg-slate-900 ring-2 ring-red-400/50 shadow-[0_0_12px_rgba(239,68,68,0.25)]'
            : dark
              ? 'bg-slate-900 ring-2 ring-[#A855F7] shadow-[0_0_10px_rgba(168,85,247,0.45)] hover:shadow-[0_0_14px_rgba(168,85,247,0.65)]'
              : 'bg-gradient-to-b from-slate-100 to-slate-200 ring-2 ring-[#A855F7] shadow-[0_0_10px_rgba(168,85,247,0.45)] hover:shadow-[0_0_14px_rgba(168,85,247,0.65)]'
        }`}
        onClick={() => onNavigate('dashboard')}
        title={`F-Pulse OSS · v${APP_VERSION}`}
      >
        {/* Logo — flips on its own inside the plate. KEEPS white bg so
            the brand image stays crisp regardless of plate color
            (the plate may now be slate gradient or dark slate). */}
        <div className="logo-flip w-9 h-9 rounded-lg overflow-hidden bg-white shrink-0">
          <img src="/fpulse-logo-mark.png" alt="F-Pulse OSS" className="w-full h-full object-cover" />
        </div>

        {/* Brand text — adapts to plate bg. Dark plate (PROD or dark
            mode) → light text. Light plate (DEV light) → dark text.
            OSS chip background also flips. */}
        <span className={`text-xl font-bold hidden sm:inline tracking-tight leading-none ${
          isProd || dark ? 'text-white' : 'text-slate-800'
        }`}>
          F-Pulse{isPlus
            ? <span className="text-amber-500">+</span>
            : <span className={`ml-1 text-[10px] font-bold uppercase tracking-wider align-middle px-1.5 py-0.5 rounded ${
                isProd || dark ? 'bg-white/10 text-slate-200' : 'bg-slate-200 text-slate-700'
              }`}>OSS</span>}
        </span>
      </div>

      {/* Version chip — REMOVED from the top nav (2026-05-25).
          The version label was visual noise next to the brand plate
          and Settings → About already shows it. We keep an empty
          spacer of the same width so the brand plate doesn't crowd
          the first nav item. APP_VERSION is still surfaced via the
          brand plate's hover title for power users who want it. */}
      {/* 2026-06-18 — brand spacer removed to reclaim ~52px of header width
          so the full text labels fit at 1536 alongside the right-side
          controls (bell + avatar were clipping at maximized). */}

      {/* Workspace Switcher — Plus-only (edition-gated).
          F-Pulse OSS is single-operator: every pipeline/connection lives in
          the one shared `default` workspace, so a switcher only invited
          confusion — and landing in an empty Personal workspace made
          pipelines look like they'd vanished. Multi-workspace switching is a
          Plus capability; WORKSPACES_ENABLED is false in the OSS build, so
          this never renders (and OSS pins every scope to `default`, so the
          "unreachable data" case can't arise). See src/config/edition.ts. */}
      {user && WORKSPACES_ENABLED && workspaces.length > 1 && (
        <div className="relative hidden sm:block mr-2 shrink-0" ref={wsDropdownRef}>
          <button
            onClick={() => setWsDropdownOpen(!wsDropdownOpen)}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-bold rounded-lg transition-all border ${
              isProd
                ? 'text-violet-200 bg-violet-900/40 border-violet-700/50 hover:bg-violet-800/50'
                : 'text-violet-700 bg-violet-50 border-violet-200 hover:bg-violet-100'
            }`}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="shrink-0 opacity-70"><rect x="2" y="3" width="20" height="18" rx="3" /><path d="M8 3v18" /></svg>
            <span className="truncate max-w-[120px]">{currentWsName}</span>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className={`shrink-0 transition-transform ${wsDropdownOpen ? 'rotate-180' : ''}`}><polyline points="6 9 12 15 18 9" /></svg>
          </button>
          {wsDropdownOpen && (
            <div className={`absolute top-full left-0 mt-1.5 w-60 rounded-xl shadow-2xl border z-50 py-1.5 max-h-72 overflow-auto ${
              isProd ? 'bg-slate-800 border-slate-600' : 'bg-white border-slate-200'
            }`}>
              <div className={`px-3 py-1.5 text-[9px] font-bold uppercase tracking-wider ${isProd ? 'text-slate-500' : 'text-slate-400'}`}>Switch Workspace</div>
              {workspaces.map((ws: any) => {
                const isActive = ws.id === currentWsId;
                return (
                <button
                  key={ws.id}
                  onClick={() => switchWorkspace(ws.id)}
                  className={`w-full text-left px-3 py-2.5 text-xs flex items-center gap-2.5 transition-colors ${
                    isActive
                      ? isProd
                        ? 'bg-violet-900/50 text-violet-200 font-bold'
                        : 'bg-violet-50 text-violet-700 font-bold border-l-[3px] border-violet-500'
                      : isProd
                        ? 'text-slate-300 hover:bg-slate-700/60'
                        : 'text-slate-600 hover:bg-slate-50'
                  }`}
                >
                  <span className={`w-2.5 h-2.5 rounded-full shrink-0 ring-2 ${
                    isActive
                      ? 'bg-violet-500 ring-violet-300'
                      : isProd
                        ? 'bg-slate-500 ring-slate-600'
                        : 'bg-slate-300 ring-slate-200'
                  }`} />
                  <span className="truncate flex-1">{ws.name}</span>
                  {isActive && (
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" className="shrink-0 text-violet-500"><polyline points="20 6 9 17 4 12" /></svg>
                  )}
                  {ws.is_personal && <span className={`text-[9px] ml-auto shrink-0 ${isProd ? 'text-slate-500' : 'text-slate-400'}`}>Personal</span>}
                  {ws.member_count != null && !ws.is_personal && !isActive && (
                    <span className={`text-[9px] ml-auto shrink-0 ${isProd ? 'text-slate-500' : 'text-slate-400'}`}>{ws.member_count} members</span>
                  )}
                </button>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Divider — hide on mobile. 2026-05-25 — widened gap (mr-2.5→mr-5)
          so the brand cluster (logo + name + version) breathes apart from
          the first nav item ("Dashboard") instead of bumping into it. */}
      <div className={`w-px h-6 ml-2 mr-5 shrink-0 hidden sm:block ${isProd ? 'bg-slate-600' : 'bg-slate-200'}`} />

      {/* Mobile hamburger */}
      <button
        className={`md:hidden p-1.5 rounded-lg ${isProd ? 'text-slate-300 hover:bg-slate-700' : 'text-slate-500 hover:bg-slate-100'}`}
        onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
        title="Menu"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          {mobileMenuOpen ? (
            <><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></>
          ) : (
            <><line x1="3" y1="6" x2="21" y2="6" /><line x1="3" y1="12" x2="21" y2="12" /><line x1="3" y1="18" x2="21" y2="18" /></>
          )}
        </svg>
      </button>

      {/*
       * Nav items — hidden on mobile (use hamburger).
       *
       * We intentionally do NOT set `overflow-x-auto` here: doing so makes
       * this element a scroll container, which implicitly clips its
       * descendants' box-shadows on all four edges. That ate the top
       * edge of the active-menu glow and the left edge of the first
       * item (Dashboard) — see Apr 18 feedback. Without the clip, the
       * active button's halo can paint into its neighbours (logo,
       * divider) and above/below the items cleanly.
       *
       * Narrow-viewport fallback: below md (768px) we render the mobile
       * dropdown below this block, and at md+ the 10–12 nav items at
       * ~100px each comfortably fit within a standard ≥1024px layout.
       */}
      <div className="hidden md:flex items-center gap-0.5">
        {visibleItems.filter(i => i.page !== 'settings' && i.page !== 'help').map((item) => {
          const label = isProd && item.prodLabel ? item.prodLabel : item.label;
          // A parent-with-children lights up whenever the active page is
          // the parent's own `page` OR any visible child's `page` — the
          // submenu lives inside the page chrome (matching the Insights
          // pattern), so the top nav stays a flat row of single buttons
          // while still highlighting correctly when the user is on a
          // descendant. Click navigates to the parent's default `page`
          // (e.g. Workflows → Pipelines), and the in-page tab strip then
          // lets the user switch between sibling sub-pages.
          // Z40 (2026-05-23) — Insights is a single nav button (`page: 'ai'`)
          // but its body renders 5 sub-pages via internal tabs that each
          // own their own router hash: `activity`, `reports`, `trust`,
          // `author`. When the user lands on any of those, `activePage`
          // is the sub-page name, so the strict `activePage === item.page`
          // check missed and the Insights button stayed unhighlighted.
          // We treat the AI hub like a children-bearing parent for the
          // active-state computation. Mirror this set if more sub-tabs
          // land — single source of truth lives in AIPage.tsx AITab type.
          // Keep in sync with AIPage.tsx AITab + the App.tsx routes that
          // render AIPage. Drifted twice (author, then gallery) — when you
          // add an Insights sub-tab with its own hash, add it here too.
          const AI_HUB_SUBPAGES = new Set<Page>(['activity', 'reports', 'trust', 'author', 'gallery', 'insights'] as Page[]);
          const isParentActive = item.children
            ? activePage === item.page || item.children.some(c => c.page === activePage)
            : item.page === 'ai'
              ? activePage === 'ai' || AI_HUB_SUBPAGES.has(activePage)
              : activePage === item.page;
          const buttonClass = `flex items-center gap-1 px-2.5 py-2 rounded-lg text-sm font-medium transition-all relative group ${
            isParentActive
              ? 'menu-active-fx bg-gradient-to-b from-amber-500/80 to-amber-700/65 text-white font-bold shadow-[inset_0_0_0_2px_rgba(251,191,36,0.90),inset_0_-2px_0_0_rgba(180,83,9,1),inset_0_0_12px_rgba(217,119,6,0.45),inset_0_1px_0_rgba(255,255,255,0.35)]'
              : isProd && dark
                ? 'text-blue-100/70 hover:text-white hover:bg-blue-500/20'
                : isProd
                  ? 'text-slate-400 hover:bg-slate-700'
                  : dark
                    ? 'text-slate-400 hover:text-white hover:bg-slate-700/60'
                    : 'text-slate-900 font-semibold hover:text-black hover:bg-slate-300/70'
          }`;
          return (
            <div key={item.page} className="flex items-center shrink-0">
              <button
                onClick={() => onNavigate(item.page)}
                onMouseEnter={() => {
                  // 2026-05-26 — hover prefetch. Kicks off the route's
                  // lazy chunk download before the user clicks, so the
                  // typical 100-500 ms hover→click delay covers the
                  // chunk fetch and the click renders instantly.
                  // routePrefetch caches in-flight calls so this is
                  // safe to spam-fire on every hover.
                  import('../utils/routePrefetch').then(({ prefetchRoute }) =>
                    prefetchRoute(item.page),
                  );
                }}
                className={buttonClass}
                title={label}
              >
                {item.icon}
                <span className="hidden min-[1430px]:inline">{label}</span>
              </button>
            </div>
          );
        })}
      </div>

      {/* Mobile dropdown menu — flatten parent groups so every leaf
          page is one tap away. The grid layout doesn't have room for a
          two-level menu; instead the parent appears as a section header
          and its children render below it as regular tappable buttons. */}
      {mobileMenuOpen && (
        <div className={`absolute top-full left-0 right-0 border-b shadow-lg z-50 md:hidden py-2 px-3 grid grid-cols-3 gap-1 ${
          isProd ? 'bg-slate-800 border-slate-700' : 'bg-white border-slate-200'
        }`}>
          {visibleItems.flatMap((item) => {
            const label = isProd && item.prodLabel ? item.prodLabel : item.label;
            const renderButton = (target: NavItem, useLabel: string) => (
              <button
                key={target.page + ':' + useLabel}
                onClick={() => { onNavigate(target.page); setMobileMenuOpen(false); }}
                className={`flex items-center gap-2 px-3.5 py-2.5 rounded-lg text-sm font-medium transition-all ${
                  activePage === target.page
                    ? 'menu-active-fx bg-gradient-to-b from-amber-500/80 to-amber-700/65 text-white font-bold shadow-[inset_0_0_0_2px_rgba(251,191,36,0.90),inset_0_-2px_0_0_rgba(180,83,9,1),inset_0_0_12px_rgba(217,119,6,0.45),inset_0_1px_0_rgba(255,255,255,0.35)]'
                    : isProd ? 'text-slate-400 hover:bg-slate-700' : 'text-slate-500 hover:bg-slate-50'
                }`}
              >
                {target.icon}
                {useLabel}
              </button>
            );
            if (!item.children) return [renderButton(item, label)];
            // Group: render the children inline as siblings. We drop the
            // parent button itself because every visible child already
            // covers all the navigation targets, and a duplicate
            // "Workflows" button (whose default lands on Pipelines) on
            // mobile would mean tapping it skips the menu rather than
            // showing the choice.
            return item.children.map(child => {
              const childLabel = isProd && child.prodLabel ? child.prodLabel : child.label;
              return renderButton(child, childLabel);
            });
          })}
        </div>
      )}

      <div className="flex-1" />

      {/* ── Right-end nav cluster: Settings + Help (2026-05-25) ──────
          Moved off the main left-aligned strip so the primary nav reads
          as feature pages (Dashboard / Projects / Workflows / …) while
          utility surfaces (Settings, Help) live with the other top-right
          chrome (env switcher, notifications, avatar). Active-state
          styling mirrors the main nav block so they still glow amber
          when selected. */}
      <div className="hidden md:flex items-center gap-0.5 mr-1.5 shrink-0">
        {visibleItems.filter(i => i.page === 'settings' || i.page === 'help').map((item) => {
          const label = isProd && item.prodLabel ? item.prodLabel : item.label;
          const isActive = activePage === item.page;
          const cls = `flex items-center gap-1 px-2.5 py-2 rounded-lg text-sm font-medium transition-all relative ${
            isActive
              ? 'menu-active-fx bg-gradient-to-b from-amber-500/80 to-amber-700/65 text-white font-bold shadow-[inset_0_0_0_2px_rgba(251,191,36,0.90),inset_0_-2px_0_0_rgba(180,83,9,1),inset_0_0_12px_rgba(217,119,6,0.45),inset_0_1px_0_rgba(255,255,255,0.35)]'
              : isProd && dark
                ? 'text-blue-100/70 hover:text-white hover:bg-blue-500/20'
                : isProd
                  ? 'text-slate-400 hover:bg-slate-700'
                  : dark
                    ? 'text-slate-400 hover:text-white hover:bg-slate-700/60'
                    : 'text-slate-900 font-semibold hover:text-black hover:bg-slate-300/70'
          }`;
          return (
            <button
              key={item.page}
              onClick={() => onNavigate(item.page)}
              onMouseEnter={() => {
                // Hover prefetch — see main-nav loop for rationale.
                import('../utils/routePrefetch').then(({ prefetchRoute }) =>
                  prefetchRoute(item.page),
                );
              }}
              className={cls}
              title={label}
            >
              {item.icon}
              <span className="hidden min-[1430px]:inline">{label}</span>
            </button>
          );
        })}
      </div>

      {/* Slim divider between utility nav and env switcher so the eye
          parses the right-end as two clusters instead of one long row. */}
      <div className={`w-px h-6 mr-2 shrink-0 hidden md:block ${isProd ? 'bg-slate-600' : 'bg-slate-200'}`} />

      {/* Environment Switcher — kept on Free as a marketing surface.
          On Free the PROD button is locked: clicking it opens the
          upgrade modal (the handler routes free-tier clicks to
          PlanModal). So users see what Plus unlocks without us
          shipping any actual PROD pipeline / env behaviour into OSS.
          Distinct from the data-table no-PROD-chrome rule, which
          still applies to env columns/badges/filter-chips in tables. */}
      {/* Steward Badge — duplicate detection + reliability findings.
          2026-06-05 — moved from after the env toggle (next to the bell)
          to BEFORE it. Rationale: the Steward is a *workspace state*
          surface (what's true about your pipelines right now), which
          sits naturally with the env switcher (which workspace you're
          looking at). The bell is a *runtime alert* surface for
          executions / approvals — different category, lives at the
          far right with the avatar. Keeping the user/avatar/runtime
          cluster intact while putting the Steward with the workspace
          cluster matches the user's mental model. */}
      {user && <StewardBadge signedIn={!!user} isProd={isProd} />}

      {onEnvironmentChange && (
        <div className={`flex items-center gap-px rounded-full border p-[3px] mr-2 ml-2 shrink-0 ${
          isProd
            ? dark ? 'border-blue-400/40 bg-blue-800/40' : 'border-slate-600 bg-slate-800'
            : dark ? 'border-slate-600 bg-slate-800' : 'border-slate-300 bg-white/90 shadow-sm'
        }`}>
          {/* DEV — active state glows emerald so it reads above the silver
              navbar. In DEV the silver bg drowned a flat emerald button
              previously; the halo shadow pulls the eye back to it. */}
          <button
            onClick={() => onEnvironmentChange('dev')}
            className={`px-3.5 py-1.5 text-xs font-bold tracking-wide rounded-full transition-all ${
              !isProd
                ? 'bg-emerald-500 text-white shadow-[0_0_14px_rgba(16,185,129,0.55),inset_0_1px_0_rgba(255,255,255,0.3)] ring-1 ring-emerald-400/60'
                : isProd ? 'text-slate-400 hover:text-slate-200' : 'text-slate-500 hover:text-slate-700'
            }`}
            title="Development environment"
          >
            DEV
          </button>
          {/* PROD — always rendered, always clickable. The handler decides
              what to actually do based on tier × role. Free tier shows a lock
              icon and opens the upgrade modal. Plus + role-denied ALSO opens
              a modal (the "PROD access denied" branch) so the user sees
              visible feedback instead of a silent no-op. Plus + role-OK
              switches environment like before.

              Styling rule: the button must stay VISIBLE in every blocked
              state — a ghost-faded button looks broken / "hidden". We use
              a rose tint + subtle background so a locked PROD reads as
              "present but protected", not "missing". */}
          <button
            onClick={handleProdClick}
            className={`px-3.5 py-1.5 text-xs font-bold tracking-wide rounded-full transition-all flex items-center gap-1 ${
              isProd
                ? 'bg-red-500 text-white shadow-[0_0_14px_rgba(239,68,68,0.6),inset_0_1px_0_rgba(255,255,255,0.3)] ring-1 ring-red-400/60'
                : prodAllowed
                  ? 'text-slate-500 hover:text-slate-700'
                  : prodLicensed
                    ? 'text-rose-500 bg-rose-50 hover:bg-rose-100 ring-1 ring-rose-200' // role-blocked — visible + clickable
                    : 'text-amber-600 hover:text-amber-700 hover:bg-amber-50' // free tier — invites the upsell
            }`}
            title={
              !prodAllowed
                ? `PROD not available for your role (${roleLabel(user?.role)})`
                : 'Production environment'
            }
          >
            PROD
            {/* Show the lock icon whenever PROD is unreachable, for ANY reason:
                   - server is free tier (prodLicensed = false), OR
                   - user's env allow-list / role blocks PROD (prodRoleAllowed = false)
                Without this, an Admin Free account on a Plus server saw the
                button styled `cursor-not-allowed` but with no icon, which made
                it hard to distinguish from a clickable PROD button. The tooltip
                still differentiates *why* it's locked. */}
            {!prodAllowed && (
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="11" width="18" height="11" rx="2" />
                <path d="M7 11V7a5 5 0 0 1 10 0v4" />
              </svg>
            )}
          </button>
        </div>
      )}

      {/* Tier badge — intentionally removed.
          The server tier is already communicated by the brand mark
          ("F-Pulse" vs "F-Pulse") at the far left of the nav. A separate
          PLUS pill next to the env switcher was redundant and, worse,
          misleading for accounts like `dev_free@fpulse.local`: a developer
          named "Dev Free" seeing a gold "PLUS" badge next to their avatar
          read as "this user is Plus tier", which doesn't exist as a concept.
          Tier is server-global, not per-user. If we ever need to surface
          license state to admins (seat counter, expiry warning), the Admin
          page is the right home for it — not the header. */}

      {/* Notification Bell */}
      {user && (
        <div className="relative shrink-0" ref={notifRef}>
          <button
            onClick={() => setNotifOpen(v => !v)}
            className={`w-9 h-9 rounded-lg flex items-center justify-center relative border transition-all ${
              isProd
                ? notifOpen
                  ? 'bg-slate-700 text-white border-slate-600 shadow-sm'
                  : 'bg-slate-800/60 text-slate-300 border-slate-700 hover:bg-slate-700 hover:text-white'
                : notifOpen
                  ? 'bg-amber-50 text-amber-700 border-amber-300 shadow-sm'
                  : 'bg-white text-slate-600 border-slate-300 hover:bg-amber-50 hover:text-amber-700 hover:border-amber-300'
            }`}
            title="Notifications"
            aria-label="Notifications"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
              <path d="M13.73 21a2 2 0 0 1-3.46 0" />
            </svg>
            {unreadCount > 0 && (
              <span className="absolute -top-1 -right-1 min-w-[20px] h-[20px] px-1 rounded-full bg-red-500 text-white text-xs font-bold flex items-center justify-center ring-2 ring-white shadow-sm leading-none">
                {unreadCount > 9 ? '9+' : unreadCount}
              </span>
            )}
          </button>

          {notifOpen && (
            <div className="absolute right-0 top-full mt-2 w-[420px] bg-white rounded-xl shadow-xl border border-slate-200 overflow-hidden z-50">
              {/* Header */}
              <div className="px-5 py-3.5 border-b border-slate-100 flex items-center justify-between">
                <span className="text-sm font-bold text-slate-800">Notifications</span>
                {unreadCount > 0 && (
                  <button
                    onClick={handleMarkAllRead}
                    className="text-xs text-amber-600 hover:text-amber-700 font-semibold"
                  >
                    Mark all read
                  </button>
                )}
              </div>
              {/* List */}
              <div className="max-h-[480px] overflow-y-auto">
                {notifications.length === 0 ? (
                  <div className="px-5 py-12 text-center text-sm text-slate-400">
                    No notifications yet
                  </div>
                ) : (
                  notifications.map((n: any) => (
                    <div
                      key={n.id}
                      className={`px-5 py-3.5 border-b border-slate-50 hover:bg-slate-50 transition-colors cursor-pointer ${
                        !n.is_read ? 'bg-amber-50/50' : ''
                      }`}
                      onClick={() => {
                        if (!n.is_read) handleMarkRead(n.id);
                        // Hash-based deep-link so the receiving page can
                        // open the specific entity (execution, pipeline,
                        // …). onNavigate alone drops the link_id and
                        // lands on the page index.
                        const href = notificationHref(n);
                        if (href) window.location.hash = href;
                        setNotifOpen(false);
                      }}
                    >
                      <div className="flex items-start gap-3">
                        {/* Type icon */}
                        <div className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 mt-0.5 ${
                          n.type === 'approval_request' ? 'bg-blue-100 text-blue-600' :
                          n.type === 'approved' ? 'bg-emerald-100 text-emerald-600' :
                          n.type === 'rejected' ? 'bg-red-100 text-red-600' :
                          n.type === 'deployed' ? 'bg-purple-100 text-purple-600' :
                          'bg-slate-100 text-slate-600'
                        }`}>
                          {n.type === 'approved' ? (
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                          ) : n.type === 'rejected' ? (
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                          ) : n.type === 'deployed' ? (
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="16 16 12 12 8 16" /><line x1="12" y1="12" x2="12" y2="21" /><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3" /></svg>
                          ) : (
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /></svg>
                          )}
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="text-sm font-semibold text-slate-800 truncate">{n.title}</div>
                          <div className="text-xs text-slate-500 mt-1 line-clamp-2 leading-relaxed">{n.message}</div>
                          <div className="text-xs text-slate-400 mt-1.5">
                            {n.created_at ? new Date(n.created_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''}
                          </div>
                        </div>
                        {!n.is_read && (
                          <span className="w-2.5 h-2.5 rounded-full bg-amber-500 shrink-0 mt-2" />
                        )}
                      </div>
                    </div>
                  ))
                )}
              </div>
              {/* View all link */}
              <button
                onClick={() => { setNotifOpen(false); onNavigate('notifications' as Page); }}
                className="w-full px-5 py-3 text-xs font-semibold text-amber-600 hover:bg-amber-50 text-center border-t border-slate-100 transition-colors"
              >
                View all notifications
              </button>
            </div>
          )}
        </div>
      )}

      {/* User avatar + popover.
          Click toggles a card showing who you're signed in as, what role you
          have, which environments are reachable, and a Logout button. We
          deliberately separated the popover from the click target so a
          mis-click on the avatar can't sign you out by accident. */}
      {user && (
        <div className="relative ml-2 shrink-0" ref={userMenuRef}>
          <button
            onClick={() => setUserMenuOpen((v) => !v)}
            className={`w-9 h-9 rounded-lg flex items-center justify-center text-sm font-bold border transition-all ${
              isProd
                ? userMenuOpen
                  ? 'bg-slate-700 text-white border-slate-500 shadow-sm ring-2 ring-amber-400/40'
                  : 'bg-slate-800/60 text-slate-200 border-slate-700 hover:bg-slate-700 hover:text-white'
                : userMenuOpen
                  ? 'bg-gradient-to-br from-amber-400 to-orange-500 text-white border-amber-400/40 shadow-sm ring-2 ring-amber-300/60'
                  : 'bg-gradient-to-br from-amber-400 to-orange-500 text-white border-amber-500/40 shadow-sm hover:from-amber-500 hover:to-orange-600'
            }`}
            title={`${user.name || user.email} · Account menu`}
            aria-haspopup="menu"
            aria-expanded={userMenuOpen}
          >
            {(user.name || user.email || '?')[0].toUpperCase()}
          </button>

          {userMenuOpen && (
            <div
              role="menu"
              className="absolute right-0 top-full mt-2 w-80 bg-white rounded-xl shadow-xl border border-slate-200 overflow-hidden z-50 animate-in fade-in slide-in-from-top-1 duration-100"
            >
              {/* Identity card */}
              <div className="px-5 py-4 bg-gradient-to-br from-amber-50 to-orange-50 border-b border-amber-100">
                <div className="flex items-center gap-3">
                  <div
                    className="w-12 h-12 rounded-xl flex items-center justify-center text-base font-bold text-white shadow-sm shrink-0"
                    style={{ background: 'linear-gradient(135deg, #F5A623, #D4880A)' }}
                  >
                    {(user.name || user.email || '?')[0].toUpperCase()}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="text-base font-bold text-slate-800 truncate">
                      {user.name || user.email}
                    </div>
                    <div className="text-xs text-slate-500 truncate">{user.email}</div>
                  </div>
                </div>
                {/* Role + env chips. NOTE: we deliberately do NOT render a
                    "Plus" badge next to the role here. `isPlus` reflects the
                    server's license state, not a per-user tier — showing it
                    on a user card would mis-label every account on a Plus
                    server as "Plus Developer". The server tier is already
                    communicated by the brand mark and the PLUS pill in the
                    top nav; the user card stays scoped to the user. */}
                {isPlus && (
                  <div className="flex items-center gap-2 mt-3">
                    <span className="text-xs font-bold uppercase tracking-wide bg-white text-amber-700 px-2 py-1 rounded border border-amber-200">
                      {roleLabel(user.role) || 'member'}
                    </span>
                    <span className="text-xs font-semibold text-slate-400 ml-auto">
                      env: <span className={isProd ? 'text-red-500' : 'text-emerald-600'}>{environment.toUpperCase()}</span>
                    </span>
                  </div>
                )}
              </div>

              {/* Quick actions */}
              <div className="py-2">
                {/* Account — self-service profile / password / sessions. */}
                <button
                  onClick={() => { setUserMenuOpen(false); onNavigate('account' as Page); }}
                  className="w-full flex items-center gap-3 px-5 py-3 text-sm text-slate-700 hover:bg-slate-50 transition-colors"
                  role="menuitem"
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
                    <circle cx="12" cy="7" r="4" />
                  </svg>
                  Account
                </button>
              </div>

              {/* Logout — separated by divider so it can't be hit by accident */}
              <div className="border-t border-slate-100">
                <button
                  onClick={() => { setUserMenuOpen(false); onLogout?.(); }}
                  className="w-full flex items-center gap-3 px-5 py-3.5 text-sm font-semibold text-red-600 hover:bg-red-50 transition-colors"
                  role="menuitem"
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
                    <polyline points="16 17 21 12 16 7" />
                    <line x1="21" y1="12" x2="9" y2="12" />
                  </svg>
                  Sign out
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* PROD blocked modal — opened for BOTH reasons the PROD toggle may
          refuse to switch:
            1. blockedReason === 'free' → PROD is not part of this build.
            2. blockedReason === 'role' → the caller's role (or per-user
               `environments` allow-list) denies PROD — explain exactly
               why and point them at their admin.
          Kept inside Sidebar (not extracted to its own file) because it's
          tightly coupled to the env-switcher behaviour. If a second caller
          appears, lift it into a BlockedProdModal.tsx and import. */}
      {upgradeModalOpen && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-900/50 backdrop-blur-sm"
          onClick={closeBlockedModal}
        >
          <div
            className="bg-white rounded-2xl shadow-2xl border border-slate-200 max-w-md w-full mx-4 overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Header strip — red gradient mirrors the PROD environment styling
                so the modal feels visually connected to the button that opened it */}
            <div className="bg-gradient-to-br from-red-500 via-rose-500 to-orange-500 px-6 py-5 text-white relative">
              <button
                onClick={closeBlockedModal}
                className="absolute top-3 right-3 w-7 h-7 rounded-lg flex items-center justify-center text-white/80 hover:text-white hover:bg-white/10 transition-colors"
                aria-label="Close"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18" />
                  <line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
              <div className="flex items-center gap-3">
                <div className="w-12 h-12 rounded-xl bg-white/15 backdrop-blur flex items-center justify-center shrink-0">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="3" y="11" width="18" height="11" rx="2" />
                    <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                  </svg>
                </div>
                <div>
                  <div className="text-xs font-bold uppercase tracking-widest text-white/80">
                    {blockedReason === 'role' ? 'Access restricted' : 'Not available'}
                  </div>
                  <h2 className="text-lg font-bold leading-tight">
                    {blockedReason === 'role' ? 'PROD access denied' : 'Production environment'}
                  </h2>
                </div>
              </div>
            </div>

            <div className="px-6 py-5">
              <p className="text-sm text-slate-600 leading-relaxed">
                F-Pulse OSS is the place where you author, run, and iterate on pipelines.
                A separate production environment for deployed, governed workloads is part
                of the commercial F-Pulse+ extension.
              </p>

              <p className="mt-4 text-xs text-slate-500 leading-relaxed">
                Everything you build here is portable. Keep using F-Pulse freely — it's the
                full open-source product.
              </p>
            </div>
            <div className="px-6 py-4 bg-slate-50 border-t border-slate-100 flex items-center justify-between gap-3">
              <a
                href="https://hybridyn.com/f-pulse"
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs font-semibold text-amber-700 hover:text-amber-900 inline-flex items-center gap-1"
              >
                Learn more about F-Pulse+
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="5" y1="12" x2="19" y2="12" />
                  <polyline points="12 5 19 12 12 19" />
                </svg>
              </a>
              <button
                onClick={closeBlockedModal}
                className="text-xs font-semibold text-white bg-slate-700 hover:bg-slate-800 rounded-lg px-4 py-2 transition-colors"
              >
                Got it
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
