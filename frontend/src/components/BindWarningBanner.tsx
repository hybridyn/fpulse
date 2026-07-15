/**
 * BindWarningBanner — sticky banner shown when the backend is bound to
 * a non-loopback interface (i.e. LAN-visible).
 *
 * Background: F-Pulse OSS defaults to binding 127.0.0.1 since 2026-06-02
 * for safety. Operators who explicitly opt into LAN binding
 * (FPULSE_ALLOW_LAN=1 / --host 0.0.0.0) get this red banner at the top
 * of every page so they can never accidentally forget the API is
 * reachable from coworkers, hotel WiFi, conference networks, etc.
 *
 * Behaviour:
 *   - Fetches /api/health/bind-info ONCE on mount (bind state can't
 *     change without a restart; no polling needed)
 *   - Renders null when loopback_only === true (the common case)
 *   - Renders a dismissible red bar otherwise
 *   - Dismissal is session-only (sessionStorage); banner re-appears
 *     on the next page load so the operator can't permanently silence
 *     a real security signal
 *
 * Discoverability note: this is wired into <App> just inside the root
 * <div>, before the sidebar/tabs, so the banner sits across the full
 * width and pushes everything else down. See App.tsx.
 */
import { useEffect, useState } from 'react';

const DISMISS_KEY = 'fpulse:lan-banner-dismissed';

interface BindInfo {
  bind_host: string;
  loopback_only: boolean;
  allow_lan_flag: boolean;
  warning: string | null;
}

export function BindWarningBanner() {
  const [info, setInfo] = useState<BindInfo | null>(null);
  const [dismissed, setDismissed] = useState<boolean>(() => {
    try {
      return sessionStorage.getItem(DISMISS_KEY) === '1';
    } catch {
      return false;
    }
  });

  useEffect(() => {
    // Single fetch — bind state is determined at server boot and
    // doesn't change during a session. No need to poll.
    let cancelled = false;
    fetch('/api/health/bind-info', { credentials: 'same-origin' })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!cancelled && data && typeof data === 'object') {
          setInfo(data as BindInfo);
        }
      })
      .catch(() => {
        // Endpoint absent or 401 → treat as "unknown, assume safe" and
        // render nothing. The middleware-level loopback bind is the
        // real defense; the banner is a visibility aid, not a control.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!info || info.loopback_only || dismissed) return null;

  const handleDismiss = () => {
    try {
      sessionStorage.setItem(DISMISS_KEY, '1');
    } catch {
      // Private-window mode or storage quota — ignore; banner just
      // returns on the next page-load. That's the desired failure mode.
    }
    setDismissed(true);
  };

  return (
    <div
      role="alert"
      aria-live="polite"
      className="w-full bg-red-700 text-white text-sm px-4 py-2 flex items-center gap-3"
      data-testid="lan-binding-warning"
    >
      <svg
        xmlns="http://www.w3.org/2000/svg"
        className="h-5 w-5 flex-shrink-0"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={2}
        aria-hidden="true"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"
        />
      </svg>
      <div className="flex-1">
        <span className="font-semibold">F-Pulse is exposed on your local network</span>
        {' — '}
        <span>
          backend is bound to <code className="bg-red-900 px-1 rounded">{info.bind_host}</code>.
          Anyone on this network can hit the API. Set{' '}
          <code className="bg-red-900 px-1 rounded">FPULSE_BIND_HOST=127.0.0.1</code>{' '}
          (or unset <code className="bg-red-900 px-1 rounded">FPULSE_ALLOW_LAN</code>) and
          restart to restrict to loopback.
        </span>
      </div>
      <button
        type="button"
        onClick={handleDismiss}
        className="ml-2 px-2 py-1 text-xs bg-red-900 hover:bg-red-950 rounded transition-colors"
        aria-label="Dismiss for this session"
      >
        Dismiss for session
      </button>
    </div>
  );
}

export default BindWarningBanner;
