/**
 * ErrorBanner — inline, retry-able error display for any fetch failure
 * that should be visible to the user (vs. silently swallowed).
 *
 * 2026-05-19 (P1 #4 of PAGE_BY_PAGE_AUDIT.md): four error-UX models were
 * in flight before this primitive landed:
 *   - Dashboard: full-page centred "Could not load" block + Retry
 *   - Activity: red inline strip with the raw exception
 *   - LineagePage / Pool / Connections / Notifications / TrustPage:
 *     silent `catch {}` — empty list looks identical to "no data yet"
 *   - SettingsPage: blocking uiAlert dialog on the save outcome
 *
 * The audit recommended: "silent fetches in read-only views; inline error
 * banner on full failure; toast only for user-initiated actions." This
 * banner is the "inline error" choice — drop it above (or instead of) the
 * empty state so the user can distinguish "the backend died" from
 * "there's nothing here yet" and can retry without navigating away.
 *
 * Adoption is incremental — pages with silent catches should swap their
 * fallback render to this component.
 *
 * Usage:
 *   {error && <ErrorBanner message={error} onRetry={load} />}
 */

import type { ReactNode } from 'react';
import { useDarkMode } from '../../hooks/useDarkMode';

export interface ErrorBannerProps {
  /** Headline — usually a short "Couldn't load X" phrase. */
  title?: string;
  /** Detail — the raw error string or a hint. */
  message: string;
  /** Optional retry callback. When provided, a Retry button is rendered. */
  onRetry?: () => void;
  /** Optional second action ("Open Pool", "Reconfigure"). */
  secondary?: { label: string; onClick: () => void };
  /** Inline = sits inside a card (no full-width red banner). */
  inline?: boolean;
}

export default function ErrorBanner({
  title = 'Something went wrong',
  message,
  onRetry,
  secondary,
  inline = false,
}: ErrorBannerProps): ReactNode {
  const dark = useDarkMode();
  const bg = dark ? 'bg-red-500/10' : 'bg-red-50';
  const border = dark ? 'border-red-500/30' : 'border-red-200';
  const fg = dark ? 'text-red-200' : 'text-red-800';
  const subFg = dark ? 'text-red-200/80' : 'text-red-700';
  const btn = dark
    ? 'bg-red-500/20 text-red-200 border-red-500/30 hover:bg-red-500/30'
    : 'bg-white text-red-700 border-red-300 hover:bg-red-100';

  return (
    <div
      role="alert"
      className={`rounded-lg border ${bg} ${border} ${inline ? 'p-3' : 'p-4'} flex items-start gap-3`}
    >
      <svg
        width={inline ? 16 : 18}
        height={inline ? 16 : 18}
        viewBox="0 0 24 24"
        fill="none"
        stroke={dark ? '#fca5a5' : '#dc2626'}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="shrink-0 mt-0.5"
      >
        <circle cx="12" cy="12" r="10" />
        <line x1="12" y1="8" x2="12" y2="12" />
        <line x1="12" y1="16" x2="12.01" y2="16" />
      </svg>
      <div className="min-w-0 flex-1">
        <p className={`text-xs font-bold ${fg}`}>{title}</p>
        <p className={`text-xs mt-0.5 leading-relaxed break-words ${subFg}`}>{message}</p>
        {(onRetry || secondary) && (
          <div className="flex items-center gap-2 mt-2">
            {onRetry && (
              <button
                onClick={onRetry}
                className={`px-3 py-1 text-xs font-semibold rounded-md border transition-colors ${btn}`}
              >
                Retry
              </button>
            )}
            {secondary && (
              <button
                onClick={secondary.onClick}
                className={`px-3 py-1 text-xs font-semibold rounded-md border transition-colors ${
                  dark
                    ? 'bg-white/[0.06] text-slate-200 border-white/[0.08] hover:bg-white/[0.1]'
                    : 'bg-white text-slate-700 border-slate-300 hover:bg-slate-50'
                }`}
              >
                {secondary.label}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
