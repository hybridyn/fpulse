/**
 * UpdateAvailableBanner — proactive, dismissible "new version available" bar.
 *
 * Complements the manual "Check for updates" button in Help & Feedback: most
 * users never open Help, so a new release would otherwise go unnoticed. This
 * surfaces one quiet, dismissible banner in the app shell when a newer version
 * exists.
 *
 * Privacy / air-gap: reuses the opt-in `/api/app/update-check` endpoint, which
 * fetches ONLY the project's public release metadata (PyPI + GitHub) and sends
 * no usage data. Any network failure is swallowed — offline installs see
 * nothing, never an error.
 *
 * Cost: the result is cached in localStorage for 24h, so the shell doesn't hit
 * the network on every page load — at most once a day per browser.
 *
 * Dismissal is per-version (the key carries the offered version), so dismissing
 * v1.0.2 still lets v1.0.3 surface later. `enabled={false}` hides it entirely —
 * F-Pulse+ passes an admin-only check here so non-admins in a shared deployment
 * aren't nudged to an upgrade they can't perform.
 */
import { useEffect, useState } from 'react';
import { api } from '../api/client';

interface UpdateResult {
  checked: boolean;
  available?: boolean;
  current: string;
  latest?: string | null;
  url?: string;
  notes?: string;
  offline?: boolean;
  reason?: string;
  releases_url?: string;
  channel?: string; // pip | docker | source | unknown (from the backend)
  upgrade_hint?: string; // e.g. "pip install --upgrade fpulse"
}

const CACHE_KEY = 'fpulse.updateCheck.cache';
const DISMISS_KEY = 'fpulse.updateCheck.dismissed'; // stores the dismissed version string
const TTL_MS = 24 * 60 * 60 * 1000; // 24h — at most one network check per browser per day

function readCache(): UpdateResult | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const { ts, result } = JSON.parse(raw);
    if (typeof ts !== 'number' || Date.now() - ts > TTL_MS) return null;
    return result as UpdateResult;
  } catch {
    return null;
  }
}

function writeCache(result: UpdateResult) {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({ ts: Date.now(), result }));
  } catch {
    /* localStorage unavailable (private mode) — the check just re-runs next load */
  }
}

export default function UpdateAvailableBanner({ enabled = true }: { enabled?: boolean }) {
  const [upd, setUpd] = useState<UpdateResult | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;

    // Fresh cache (<24h) — reuse it, don't touch the network on every page load.
    const cached = readCache();
    if (cached) {
      if (!cancelled) setUpd(cached);
      return;
    }

    (async () => {
      try {
        const r = await api.get<UpdateResult>('/api/app/update-check');
        if (cancelled) return;
        writeCache(r);
        setUpd(r);
      } catch {
        /* offline / air-gapped / endpoint missing — stay silent, it's a nudge */
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [enabled]);

  if (!enabled || !upd || !upd.available || !upd.latest) return null;

  // Per-version sticky dismissal: dismissing v1.0.2 still lets v1.0.3 surface later.
  try {
    if (localStorage.getItem(DISMISS_KEY) === upd.latest) return null;
  } catch {
    /* ignore */
  }

  const dismiss = () => {
    try {
      if (upd.latest) localStorage.setItem(DISMISS_KEY, upd.latest);
    } catch {
      /* ignore */
    }
    setUpd(null);
  };

  const notesUrl = upd.url || upd.releases_url;

  return (
    <div className="shrink-0 px-4 py-2 bg-emerald-50 border-b border-emerald-200 text-emerald-900 text-sm flex items-center gap-3">
      <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v12m0 0l-4-4m4 4l4-4M4 20h16" />
      </svg>
      <span className="flex-1 leading-snug">
        <strong>F-Pulse v{upd.latest}</strong> is available — you have v{upd.current}.
        {upd.upgrade_hint && (
          <>
            {' '}Update with{' '}
            <code className="px-1 rounded bg-emerald-100 font-mono text-[12px]">{upd.upgrade_hint}</code>.
          </>
        )}
        {notesUrl && (
          <>
            {' '}
            <a className="underline hover:no-underline" href={notesUrl} target="_blank" rel="noreferrer">
              Release notes →
            </a>
          </>
        )}
      </span>
      <button
        type="button"
        onClick={dismiss}
        title="Dismiss"
        aria-label="Dismiss update notice"
        className="text-emerald-700 hover:text-emerald-900"
      >
        ✕
      </button>
    </div>
  );
}
