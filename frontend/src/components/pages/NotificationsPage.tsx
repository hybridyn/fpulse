/**
 * NotificationsPage — full notification history with filtering.
 *
 * The Sidebar bell shows the latest 20 for quick glances. This page
 * shows the complete history with type filters and bulk actions so
 * admins/developers can review their entire approval workflow trail.
 */

import { useCallback, useEffect, useState, type ReactElement } from 'react';
import { api } from '../../api/client';
import TierChip from '../shared/TierChip';
import HeroCard from '../shared/HeroCard';
import EmptyState from '../shared/EmptyState';
import { DelayedSkeleton, SkeletonCard } from '../shared/Skeleton';
import { notificationHref } from '../../lib/notificationHref';
import { usePageContext } from '../../hooks/usePageContext';
import { navigateTo } from '../../router';
import PageHeader from '../shared/PageHeader';

const TYPE_META: Record<string, { label: string; color: string; bg: string; icon: ReactElement }> = {
  approval_request: {
    label: 'Review Request',
    color: 'text-blue-700',
    bg: 'bg-blue-100',
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" />
      </svg>
    ),
  },
  approved: {
    label: 'Approved',
    color: 'text-emerald-700',
    bg: 'bg-emerald-100',
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="20 6 9 17 4 12" />
      </svg>
    ),
  },
  rejected: {
    label: 'Rejected',
    color: 'text-red-700',
    bg: 'bg-red-100',
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
      </svg>
    ),
  },
  deployed: {
    label: 'Deployed',
    color: 'text-purple-700',
    bg: 'bg-purple-100',
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="16 16 12 12 8 16" /><line x1="12" y1="12" x2="12" y2="21" />
        <path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3" />
      </svg>
    ),
  },
  alert_triggered: {
    label: 'Alert Triggered',
    color: 'text-red-700',
    bg: 'bg-red-100',
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" />
      </svg>
    ),
  },
  alert_resolved: {
    label: 'Alert Resolved',
    color: 'text-emerald-700',
    bg: 'bg-emerald-100',
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" />
        <polyline points="20 6 9 17 4 12" />
      </svg>
    ),
  },
  run_succeeded: {
    label: 'Run Succeeded',
    color: 'text-emerald-700',
    bg: 'bg-emerald-100',
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="20 6 9 17 4 12" />
      </svg>
    ),
  },
  run_failed: {
    label: 'Run Failed',
    color: 'text-red-700',
    bg: 'bg-red-100',
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
      </svg>
    ),
  },
  pipeline_published: {
    label: 'Published',
    color: 'text-emerald-700',
    bg: 'bg-emerald-100',
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="16 16 12 12 8 16" /><line x1="12" y1="12" x2="12" y2="21" />
        <path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3" />
      </svg>
    ),
  },
  pipeline_revoked: {
    label: 'Revoked',
    color: 'text-amber-700',
    bg: 'bg-amber-100',
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 12a9 9 0 1 0 9-9" /><polyline points="3 4 3 12 11 12" />
      </svg>
    ),
  },
  // 2026-05-19 (P1 #7 of PAGE_BY_PAGE_AUDIT.md): the backend wires two
  // watchdog detectors (long-running run + schedule miss) via the
  // `ApprovalNotifier.on_long_running` / `on_schedule_miss` pipeline, but
  // the frontend used to demote every fire to a generic `alert_triggered`
  // pill — operators monitoring SLA breaches couldn't tell a threshold
  // alert from a watchdog fire. Explicit types + dedicated filter chip
  // now distinguish them. Backend types are sourced from
  // backend/fpulse/notifications/service.py lines 371,428.
  long_running: {
    label: 'Long-Running',
    color: 'text-amber-700',
    bg: 'bg-amber-100',
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" />
      </svg>
    ),
  },
  schedule_miss: {
    label: 'Schedule Miss',
    color: 'text-orange-700',
    bg: 'bg-orange-100',
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="4" width="18" height="18" rx="2" /><line x1="16" y1="2" x2="16" y2="6" /><line x1="8" y1="2" x2="8" y2="6" /><line x1="3" y1="10" x2="21" y2="10" />
        <line x1="9" y1="14" x2="15" y2="20" /><line x1="15" y1="14" x2="9" y2="20" />
      </svg>
    ),
  },
  // 2026-06-05 — Steward (Archeologist) notifications. Two types:
  //   `steward_finding` — new finding at the user's notify_min_severity.
  //   `steward_finding_escalated` — a previously-known finding bumped
  //     to P1 via the learning layer's "ignored N times" escalation.
  // Clicking the row deep-links to the dashboard + auto-opens the
  // Steward dropdown via the `fpulse:steward-open` event (see
  // frontend/src/lib/notificationHref.ts).
  steward_finding: {
    label: 'Steward',
    color: 'text-violet-700',
    bg: 'bg-violet-100',
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z" /><circle cx="12" cy="12" r="3" />
      </svg>
    ),
  },
  steward_finding_escalated: {
    label: 'Steward — Escalated',
    color: 'text-red-700',
    bg: 'bg-red-100',
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
        <line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
    ),
  },
  info: {
    label: 'Info',
    color: 'text-slate-700',
    bg: 'bg-slate-100',
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10" /><line x1="12" y1="16" x2="12" y2="12" /><line x1="12" y1="8" x2="12.01" y2="8" />
      </svg>
    ),
  },
};

type FilterType = 'all' | 'runs' | 'approval_request' | 'approved' | 'rejected' | 'deployed' | 'alerts' | 'watchdog';

export default function NotificationsPage({ environment = 'dev', tier = 'free' }: { environment?: 'dev' | 'prod'; tier?: string }) {
  // 2026-05-19 (P2 #8 of PAGE_BY_PAGE_AUDIT.md): PROD chrome must be
  // gated on tier AND environment per the feedback_oss_no_prod_chrome
  // rule. OSS Free has no PROD environment; the previous `isProd` test
  // looked at environment alone, which a future DEV-tools tweak could
  // flip on a Free install. Defence-in-depth.
  const isProd = environment === 'prod' && tier === 'plus';
  const isFree = tier !== 'plus';
  const [notifications, setNotifications] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<FilterType>('all');
  const [showUnreadOnly, setShowUnreadOnly] = useState(false);
  const [unreadCount, setUnreadCount] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [notifs, countRes] = await Promise.all([
        api.listNotifications(showUnreadOnly, 200),
        api.getUnreadCount(),
      ]);
      setNotifications(notifs || []);
      setUnreadCount(countRes.unread ?? 0);
    } catch {
      // ignore
    }
    setLoading(false);
  }, [showUnreadOnly]);

  useEffect(() => { load(); }, [load]);

  // 2026-05-19 (P1 #6 of PAGE_BY_PAGE_AUDIT.md): broadcast on any mutation
  // so the sidebar bell (and any future consumer) can refresh its unread
  // count without waiting for the 30s poll. Listener lives in Sidebar.tsx.
  const broadcastChange = () => {
    try { window.dispatchEvent(new CustomEvent('fpulse:notifications-changed')); } catch { /* no-op */ }
  };

  const handleMarkRead = async (id: string) => {
    await api.markNotificationRead(id).catch(() => {});
    setNotifications(prev => prev.map(n => n.id === id ? { ...n, is_read: true } : n));
    setUnreadCount(c => Math.max(0, c - 1));
    broadcastChange();
  };

  // Click a row → mark read + navigate to the linked entity. Falls back
  // silently when the notification is purely informational (no link_type).
  const handleRowClick = (n: any) => {
    const href = notificationHref(n);
    if (!n.is_read) handleMarkRead(n.id);
    if (href) window.location.hash = href;
  };

  const handleMarkAllRead = async () => {
    await api.markAllNotificationsRead().catch(() => {});
    setNotifications(prev => prev.map(n => ({ ...n, is_read: true })));
    setUnreadCount(0);
    broadcastChange();
  };

  const handleDelete = async (id: string) => {
    // Optimistic remove — if the API fails the row pops back via reload.
    const prev = notifications;
    const wasUnread = !prev.find(n => n.id === id)?.is_read;
    setNotifications(p => p.filter(n => n.id !== id));
    if (wasUnread) setUnreadCount(c => Math.max(0, c - 1));
    try {
      await api.deleteNotification(id);
      broadcastChange();
    } catch {
      setNotifications(prev);
    }
  };

  const handleClearAll = async (onlyRead = false) => {
    if (!notifications.length) return;
    const message = onlyRead
      ? 'Clear all read notifications? Unread ones stay.'
      : 'Clear all notifications? This cannot be undone.';
    if (!window.confirm(message)) return;
    const prev = notifications;
    const prevUnread = unreadCount;
    if (onlyRead) {
      setNotifications(p => p.filter(n => !n.is_read));
    } else {
      setNotifications([]);
      setUnreadCount(0);
    }
    try {
      await api.clearNotifications(onlyRead);
      broadcastChange();
    } catch {
      setNotifications(prev);
      setUnreadCount(prevUnread);
    }
  };

  const filtered = filter === 'all'
    ? notifications
    : filter === 'alerts'
    ? notifications.filter(n => n.type === 'alert_triggered' || n.type === 'alert_resolved')
    : filter === 'runs'
    ? notifications.filter(n => n.type === 'run_succeeded' || n.type === 'run_failed')
    : filter === 'watchdog'
    // 2026-05-19 (P1 #7): the Watchdog chip aggregates both long-running
    // detector fires and schedule-miss fires so operators can isolate SLA
    // signals from explicit threshold alerts.
    ? notifications.filter(n => n.type === 'long_running' || n.type === 'schedule_miss')
    : notifications.filter(n => n.type === filter);

  const typeCounts = notifications.reduce<Record<string, number>>((acc, n) => {
    acc[n.type] = (acc[n.type] || 0) + 1;
    return acc;
  }, {});

  // 2026-05-19 (P1 #8 of PAGE_BY_PAGE_AUDIT.md): publish context so the
  // Copilot can answer "what's unread?" / "show me alerts from today"
  // without re-fetching. We pass IDs (handles) only — never message
  // bodies, which can contain user data.
  usePageContext({
    page: 'notifications',
    visible_ids: filtered.map((n: any) => n.id),
    filters: {
      filter,
      unread_only: showUnreadOnly,
      unread_count: unreadCount,
    },
  });

  return (
    <div className="flex-1 flex flex-col overflow-hidden bg-canvas-bg">
      {/* FOLLOW-1 (2026-05-19) — migrated to shared <PageHeader>. The
          bespoke `isProd ? 'Production Notifications'` rename is dropped
          (the PROD pill in the title accessory already conveys the env).
          Unread chip is part of the subtitle when relevant. */}
      <PageHeader
        environment={environment}
        icon={(
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-blue-500">
            <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" />
          </svg>
        )}
        title="Notifications"
        subtitle={(
          <>
            Approvals, pipeline alerts, and status updates
            {unreadCount > 0 && (
              <span className="ml-2 text-amber-600 font-semibold">{unreadCount} unread</span>
            )}
          </>
        )}
        titleAccessory={<TierChip tier={tier} environment={environment} />}
        actions={(
          <>
            <label className="flex items-center gap-2 text-xs text-slate-600 cursor-pointer">
              <input
                type="checkbox"
                checked={showUnreadOnly}
                onChange={(e) => setShowUnreadOnly(e.target.checked)}
                className="rounded border-slate-300"
              />
              Unread only
            </label>
            {unreadCount > 0 && (
              <button
                onClick={handleMarkAllRead}
                className={`text-sm font-semibold px-4 py-2 rounded-lg transition-colors ${isProd ? 'text-amber-300 hover:text-amber-200 hover:bg-white/[0.05]' : 'text-amber-600 hover:text-amber-700 hover:bg-amber-50'}`}
              >
                Mark all read
              </button>
            )}
            {notifications.length > 0 && (
              <>
                {notifications.some(n => n.is_read) && (
                  <button
                    onClick={() => handleClearAll(true)}
                    className={`text-sm font-semibold px-3 py-2 rounded-lg transition-colors ${isProd ? 'text-slate-300 hover:text-white hover:bg-white/[0.05]' : 'text-slate-600 hover:text-slate-800 hover:bg-slate-100'}`}
                    title="Remove notifications you've already read"
                  >
                    Clear read
                  </button>
                )}
                <button
                  onClick={() => handleClearAll(false)}
                  className={`text-sm font-semibold px-3 py-2 rounded-lg transition-colors ${isProd ? 'text-red-300 hover:text-red-200 hover:bg-red-500/10' : 'text-red-600 hover:text-red-700 hover:bg-red-50'}`}
                  title="Permanently delete every notification"
                >
                  Clear all
                </button>
              </>
            )}
          </>
        )}
      />

      {/* Content */}
      <div className="flex-1 overflow-auto">
      <div className="w-full max-w-[1500px] mx-auto px-8 py-6">
      {/* Hero KPI cards — matches Executions / Pipelines / Connections / Pool
          visual family. */}
      {(() => {
        const isProd = environment === 'prod';
        const unread = notifications.filter(n => !n.is_read).length;
        const last24 = notifications.filter(n => {
          const t = n.created_at ? new Date(n.created_at).getTime() : 0;
          return t && (Date.now() - t) < 24 * 3600 * 1000;
        }).length;
        const alerts = (typeCounts['alert_triggered'] || 0) + (typeCounts['alert_resolved'] || 0);
        const total = notifications.length;
        return (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5">
            <HeroCard
              gradient={isProd ? 'from-amber-500 to-orange-600' : 'from-amber-400 to-orange-500'}
              icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" /></svg>}
              label="Unread"
              value={String(unread)}
            />
            <HeroCard
              gradient={isProd ? 'from-indigo-500 to-indigo-600' : 'from-indigo-400 to-indigo-500'}
              icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>}
              label="Last 24h"
              value={String(last24)}
            />
            <HeroCard
              gradient={isProd ? 'from-red-500 to-rose-600' : 'from-red-400 to-rose-500'}
              icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>}
              label="Alerts"
              value={String(alerts)}
            />
            <HeroCard
              gradient={isProd ? 'from-violet-500 to-purple-600' : 'from-violet-400 to-purple-500'}
              icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" /><line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" /></svg>}
              label="Total"
              value={String(total)}
            />
          </div>
        );
      })()}

      {/* Filter chips */}
      <div className="flex flex-wrap gap-2 mb-5">
        {([
          { id: 'all' as FilterType, label: 'All', count: notifications.length },
          { id: 'runs' as FilterType, label: 'Pipeline Runs', count: (typeCounts['run_succeeded'] || 0) + (typeCounts['run_failed'] || 0) },
          // 2026-05-19 (P2 #8 of PAGE_BY_PAGE_AUDIT.md): approval / deploy
          // chips are Plus-only — they correspond to lifecycle features
          // (Review → Approve → Deploy) that don't exist on OSS Free.
          // Hide the chips entirely on Free so they don't render with
          // permanent zero counts.
          ...(!isFree ? [
            { id: 'approval_request' as FilterType, label: 'Review Requests', count: typeCounts['approval_request'] || 0 },
            { id: 'approved' as FilterType, label: 'Approved', count: typeCounts['approved'] || 0 },
            { id: 'rejected' as FilterType, label: 'Rejected', count: typeCounts['rejected'] || 0 },
            { id: 'deployed' as FilterType, label: 'Deployed', count: typeCounts['deployed'] || 0 },
          ] : []),
          { id: 'alerts' as FilterType, label: 'Alerts', count: (typeCounts['alert_triggered'] || 0) + (typeCounts['alert_resolved'] || 0) },
          // P1 #7 (2026-05-19) — Watchdog aggregates SLA-style detector
          // fires (long-running + schedule-miss) so users can isolate them
          // from explicit threshold-based alert rules.
          { id: 'watchdog' as FilterType, label: 'Watchdog', count: (typeCounts['long_running'] || 0) + (typeCounts['schedule_miss'] || 0) },
        ]).map((f) => (
          <button
            key={f.id}
            onClick={() => setFilter(f.id)}
            className={`px-3 py-1.5 text-xs font-semibold rounded-full border transition-colors ${
              filter === f.id
                ? 'bg-slate-800 text-white border-slate-800'
                : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50'
            }`}
          >
            {f.label}
            {f.count > 0 && (
              <span className={`ml-1.5 ${filter === f.id ? 'text-slate-400' : 'text-slate-400'}`}>
                {f.count}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Notification list */}
      {loading ? (
        <DelayedSkeleton>
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => <SkeletonCard key={i} height={72} />)}
          </div>
        </DelayedSkeleton>
      ) : filtered.length === 0 ? (
        <EmptyState
          icon={
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
              <path d="M13.73 21a2 2 0 0 1-3.46 0" />
            </svg>
          }
          title={showUnreadOnly ? 'All caught up' : 'No notifications yet'}
          body={
            showUnreadOnly
              ? "You've cleared every unread notification."
              : 'Notifications will appear here when pipelines run, fail, or alert rules fire.'
          }
          secondaryCtas={[
            { label: 'Configure alert rules', onClick: () => navigateTo('settings') },
          ]}
          hint={notifications.length > 0 ? `${notifications.length} total in history` : undefined}
        />
      ) : (
        <div className="rounded-lg border border-slate-200 shadow-sm bg-white overflow-hidden divide-y divide-slate-100">
          {filtered.map((n: any) => {
            const meta = TYPE_META[n.type] || TYPE_META.info;
            const href = notificationHref(n);
            const clickable = !!href;
            return (
              <div
                key={n.id}
                onClick={clickable ? () => handleRowClick(n) : undefined}
                role={clickable ? 'button' : undefined}
                tabIndex={clickable ? 0 : undefined}
                onKeyDown={clickable ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleRowClick(n); } } : undefined}
                className={`px-5 py-4 transition-colors ${
                  clickable ? 'cursor-pointer hover:bg-slate-50' : ''
                } ${!n.is_read ? 'bg-amber-50/40' : ''}`}
              >
                <div className="flex items-start gap-4">
                  {/* Type icon */}
                  <div className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 ${meta.bg} ${meta.color}`}>
                    {meta.icon}
                  </div>

                  {/* Content */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-0.5">
                      <span className="text-sm font-semibold text-slate-800">{n.title}</span>
                      <span className={`text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded ${meta.bg} ${meta.color}`}>
                        {meta.label}
                      </span>
                      {!n.is_read && (
                        <span className="w-2 h-2 rounded-full bg-amber-500 shrink-0" />
                      )}
                    </div>
                    <p className="text-xs text-slate-600 leading-relaxed">{n.message}</p>

                    {/* Metadata row */}
                    <div className="flex items-center gap-4 mt-2 text-xs text-slate-400">
                      <span>
                        {n.created_at
                          ? new Date(n.created_at).toLocaleString(undefined, {
                              month: 'short', day: 'numeric', year: 'numeric',
                              hour: '2-digit', minute: '2-digit',
                            })
                          : ''}
                      </span>
                      {n.metadata?.workflow_name && (
                        <span className="text-slate-500">
                          Pipeline: <strong>{n.metadata.workflow_name}</strong>
                        </span>
                      )}
                      {n.metadata?.submitted_by && (
                        <span>by {n.metadata.submitted_by}</span>
                      )}
                      {n.metadata?.approved_by && (
                        <span>by {n.metadata.approved_by}</span>
                      )}
                      {n.metadata?.rejected_by && (
                        <span>by {n.metadata.rejected_by}</span>
                      )}
                      {n.metadata?.deployed_by && (
                        <span>by {n.metadata.deployed_by}</span>
                      )}
                    </div>

                    {/* Notes */}
                    {n.metadata?.notes && (
                      <div className="mt-2 px-3 py-2 bg-slate-50 rounded-lg text-xs text-slate-600 border-l-2 border-slate-300">
                        {n.metadata.notes}
                      </div>
                    )}
                  </div>

                  {/* Actions */}
                  <div className="shrink-0 flex items-center gap-1">
                    {!n.is_read && (
                      <button
                        onClick={(e) => { e.stopPropagation(); handleMarkRead(n.id); }}
                        className="text-xs font-semibold text-slate-500 hover:text-slate-700 px-2 py-1 rounded hover:bg-slate-100 transition-colors"
                        title="Mark as read"
                      >
                        Mark read
                      </button>
                    )}
                    <button
                      onClick={(e) => { e.stopPropagation(); handleDelete(n.id); }}
                      className="text-slate-400 hover:text-red-600 hover:bg-red-50 p-1 rounded transition-colors"
                      title="Delete notification"
                      aria-label="Delete notification"
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <polyline points="3 6 5 6 21 6" />
                        <path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6" />
                        <path d="M10 11v6" />
                        <path d="M14 11v6" />
                        <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
                      </svg>
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
      </div>
      </div>
    </div>
  );
}
