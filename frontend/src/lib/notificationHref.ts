// Maps a notification's link_type + link_id to an in-app hash route so
// every notification surfaces a meaningful "open this" target. Keep the
// mapping table in sync with what backend notifiers emit (see
// fpulse/notifications/service.py — long_running, schedule_miss, lifecycle,
// etc., all set link_type already).
//
// Returning null means the notification is informational with nowhere
// useful to land — the row stays unclickable rather than navigating to
// a misleading default page.

export type NotificationLike = {
  link_type?: string;
  link_id?: string;
  type?: string;
  metadata?: Record<string, any>;
};

export function notificationHref(n: NotificationLike): string | null {
  const t = (n.link_type || '').toLowerCase();
  const id = n.link_id || '';

  switch (t) {
    case 'executions':
    case 'execution':
      // /<id> opens the run on the Steps tab so users land on node-level
      // detail (input received / output produced per step).
      return id ? `#executions/${id}` : '#executions';

    case 'workflow':
    case 'pipeline':
      return id ? `#pipelines/${id}` : '#pipelines';

    case 'schedules':
    case 'schedule':
      // No standalone Schedules page in OSS — schedules are managed
      // inline on the Pipelines page; land there and let the user
      // pick the schedule via the pipeline row.
      return '#pipelines';

    case 'approvals':
    case 'approval':
      // Approvals page is Plus-only. In OSS the closest landing is
      // the pipeline detail.
      return id ? `#pipelines/${id}` : '#pipelines';

    case 'project':
      return id ? `#projects/${id}` : '#projects';

    case 'admin':
    case 'settings':
      return '#settings';

    // 2026-06-05 — Steward findings ping the bell when new or escalated.
    // There's no dedicated routed page yet (the Steward lives in a
    // header dropdown), so we land on the dashboard and dispatch an
    // event the StewardBadge listens for to auto-open itself with
    // the relevant finding scrolled into view.
    case 'steward':
      try {
        window.dispatchEvent(new CustomEvent('fpulse:steward-open', {
          detail: { finding_id: id },
        }));
      } catch {
        // No-op — worst case the user lands on dashboard with the bell still pinging.
      }
      return '#dashboard';

    default:
      return null;
  }
}
